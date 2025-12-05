from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import traceback
from typing import Optional, List, Dict, Any, Union, cast

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

# ==============================================================================
# CONFIGURATION & LOGGING
# ==============================================================================

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("voice_assistant")

app = FastAPI(title="Voice Assistant")

# Mount static files and templates
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ==============================================================================
# GLOBAL STATE
# ==============================================================================

class GlobalState:
    def __init__(self):
        self.state = "idle"
        self.message = "Select 'Start Session' to begin."
        self.last_error: Optional[str] = None
        self.connected = False
        self.assistant_task: Optional[asyncio.Task] = None
        self.assistant_instance: Optional['BasicVoiceAssistant'] = None
        # Queues for Server-Sent Events (SSE)
        self.sse_queues: List[asyncio.Queue] = []

    def update(self, state: str, message: str, error: Optional[str] = None):
        self.state = state
        self.message = message
        if error:
            self.last_error = error
        
        # Logic to determine connection status
        if state in {"ready", "listening", "processing", "assistant_speaking"}:
            self.connected = True
        elif state in {"stopped", "idle", "error"}:
            self.connected = False
            
        self.broadcast_status()

    def broadcast_status(self):
        """Send status update to all connected SSE clients."""
        payload = {
            "type": "status",
            "state": self.state,
            "message": self.message,
            "last_error": self.last_error,
            "connected": self.connected
        }
        self.broadcast_event(payload)

    def broadcast_event(self, data: Dict[str, Any]):
        """Push a message to all active SSE queues."""
        # We iterate over a copy of the list to allow safe removal during iteration if needed
        # (though removal happens in the endpoint logic)
        for q in self.sse_queues:
            q.put_nowait(data)

# Singleton instance
GLOBAL_STATE = GlobalState()

# ==============================================================================
# ASSISTANT LOGIC (AZURE)
# ==============================================================================

def _validate_env() -> tuple[bool, str]:
    required_vars = [
        "VOICE_LIVE_MODEL",
        "VOICE_LIVE_VOICE", 
        "AZURE_VOICE_LIVE_API_KEY",
        "AZURE_VOICE_LIVE_ENDPOINT"
    ]
    missing = [var for var in required_vars if not os.environ.get(var)]
    if missing:
        return False, f"Missing env vars: {', '.join(missing)}"
    return True, "Valid"

class BasicVoiceAssistant:
    """Async wrapper for Azure Voice Live."""
    
    def __init__(self, endpoint: str, key: str, model: str, voice: str, instructions: str):
        self.endpoint = endpoint
        self.key = key
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.connection = None
        self._response_cancelled = False
        self._stopping = False

    async def run(self):
        from azure.ai.voicelive.aio import connect # type: ignore
        from azure.ai.voicelive.models import ( # type: ignore
            RequestSession, ServerVad, AzureStandardVoice, Modality, 
            InputAudioFormat, OutputAudioFormat, ServerEventType
        )
        from azure.core.credentials import AzureKeyCredential

        credential = AzureKeyCredential(self.key)
        
        try:
            GLOBAL_STATE.broadcast_event({"type": "log", "msg": f"Connecting to {self.endpoint}..."})
            
            async with connect(
                endpoint=self.endpoint,
                credential=credential,
                model=self.model,
                connection_options={"max_msg_size": 10 * 1024 * 1024}
            ) as conn:
                self.connection = conn
                self._response_cancelled = False

                # Configure Session
                voice_cfg = AzureStandardVoice(name=self.voice) if "-" in self.voice else self.voice
                
                await conn.session.update(session=RequestSession(
                    modalities=[Modality.TEXT, Modality.AUDIO],
                    instructions=self.instructions,
                    voice=voice_cfg,
                    input_audio_format=InputAudioFormat.PCM16,
                    output_audio_format=OutputAudioFormat.PCM16,
                    turn_detection=ServerVad(threshold=0.5, prefix_padding_ms=300, silence_duration_ms=500),
                ))

                GLOBAL_STATE.update("ready", "Session Ready. Speak now.")

                # Event Loop
                async for event in conn:
                    if self._stopping:
                        break
                    
                    if event.type == ServerEventType.SESSION_UPDATED:
                         GLOBAL_STATE.update("ready", "Ready")
                    
                    elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                        GLOBAL_STATE.update("listening", "Listening...")
                        GLOBAL_STATE.broadcast_event({"type": "control", "action": "stop_playback"})
                        # Interrupt if speaking
                        if GLOBAL_STATE.state in {"assistant_speaking", "processing"}:
                            self._response_cancelled = True
                            await conn.response.cancel()
                    
                    elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
                        GLOBAL_STATE.update("processing", "Processing...")
                    
                    elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
                        if not self._response_cancelled:
                            if GLOBAL_STATE.state != "assistant_speaking":
                                GLOBAL_STATE.update("assistant_speaking", "Assistant Speaking...")
                            
                            if hasattr(event, "delta") and event.delta:
                                b64 = base64.b64encode(event.delta).decode("utf-8")
                                GLOBAL_STATE.broadcast_event({"type": "audio", "audio": b64})
                    
                    elif event.type == ServerEventType.RESPONSE_AUDIO_DONE:
                        self._response_cancelled = False
                        GLOBAL_STATE.update("ready", "Finished speaking.")
                    
                    elif event.type == ServerEventType.ERROR:
                        msg = getattr(event.error, "message", "Unknown Error")
                        GLOBAL_STATE.update("error", f"Azure Error: {msg}", error=msg)

        except Exception as e:
            logger.error(f"Assistant Error: {e}")
            GLOBAL_STATE.update("error", f"Crash: {str(e)}", error=str(e))
        finally:
            self.connection = None
            GLOBAL_STATE.update("stopped", "Session Ended")

    async def stop(self):
        self._stopping = True
        # Logic to break the async for loop is handled by _stopping flag check

    async def send_audio(self, b64_audio: str):
        if self.connection:
            await self.connection.input_audio_buffer.append(audio=b64_audio)

    async def interrupt(self):
        self._response_cancelled = True
        if self.connection:
            try:
                await self.connection.response.cancel()
            except Exception:
                pass # Best effort

# ==============================================================================
# HTTP ENDPOINTS
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    env_vars = {
        "VOICE_LIVE_MODEL": os.environ.get("VOICE_LIVE_MODEL") or "(not set)",
        "VOICE_LIVE_VOICE": os.environ.get("VOICE_LIVE_VOICE") or "(not set)",
        "AZURE_VOICE_LIVE_ENDPOINT": os.environ.get("AZURE_VOICE_LIVE_ENDPOINT") or "(not set)",
    }
    return templates.TemplateResponse("index.html", {"request": request, "env": env_vars})

@app.get("/events")
async def sse_endpoint(request: Request):
    """Server-Sent Events endpoint."""
    async def event_generator():
        queue = asyncio.Queue()
        GLOBAL_STATE.sse_queues.append(queue)
        
        # Send initial state
        yield {
            "data": json.dumps({
                "type": "status",
                "state": GLOBAL_STATE.state,
                "message": GLOBAL_STATE.message,
                "connected": GLOBAL_STATE.connected
            })
        }

        try:
            while True:
                # Wait for data or client disconnect
                if await request.is_disconnected():
                    break
                data = await queue.get()
                yield {"data": json.dumps(data)}
        except asyncio.CancelledError:
            pass
        finally:
            if queue in GLOBAL_STATE.sse_queues:
                GLOBAL_STATE.sse_queues.remove(queue)

    return EventSourceResponse(event_generator())

@app.post("/start-session")
async def start_session():
    if GLOBAL_STATE.assistant_task and not GLOBAL_STATE.assistant_task.done():
         return JSONResponse({"started": False, "reason": "Already running"})

    ok, msg = _validate_env()
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    # Initialize Assistant
    GLOBAL_STATE.assistant_instance = BasicVoiceAssistant(
        endpoint=os.environ.get("AZURE_VOICE_LIVE_ENDPOINT"),
        key=os.environ.get("AZURE_VOICE_LIVE_API_KEY"),
        model=os.environ.get("VOICE_LIVE_MODEL"),
        voice=os.environ.get("VOICE_LIVE_VOICE"),
        instructions=os.environ.get("VOICE_LIVE_INSTRUCTIONS") or "You are a helpful assistant."
    )
    
    # Run in background task
    GLOBAL_STATE.update("starting", "Starting session...")
    GLOBAL_STATE.assistant_task = asyncio.create_task(GLOBAL_STATE.assistant_instance.run())
    
    return {"started": True}

@app.post("/stop-session")
async def stop_session():
    if GLOBAL_STATE.assistant_instance:
        await GLOBAL_STATE.assistant_instance.stop()
    
    # Wait briefly for task to cleanup
    if GLOBAL_STATE.assistant_task:
        try:
            await asyncio.wait_for(GLOBAL_STATE.assistant_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
            
    GLOBAL_STATE.assistant_task = None
    GLOBAL_STATE.assistant_instance = None
    GLOBAL_STATE.update("stopped", "Session stopped manually.")
    return {"stopped": True}

@app.post("/interrupt")
async def interrupt_session():
    if GLOBAL_STATE.assistant_instance:
        await GLOBAL_STATE.assistant_instance.interrupt()
        GLOBAL_STATE.broadcast_event({"type": "control", "action": "stop_playback"})
        return {"interrupted": True}
    return JSONResponse({"interrupted": False, "reason": "No session"}, status_code=400)

@app.post("/audio-chunk")
async def audio_chunk(request: Request):
    """HTTP Fallback for audio upload."""
    data = await request.json()
    b64 = data.get("audio")
    if GLOBAL_STATE.assistant_instance and b64:
        await GLOBAL_STATE.assistant_instance.send_audio(b64)
        return {"accepted": True}
    return JSONResponse({"accepted": False}, status_code=400)

# ==============================================================================
# WEBSOCKET (Audio Stream)
# ==============================================================================

@app.websocket("/ws-audio")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            # Receive binary PCM16 data
            data = await websocket.receive_bytes()
            if GLOBAL_STATE.assistant_instance:
                # Encode to base64 for Azure Buffer
                b64 = base64.b64encode(data).decode("utf-8")
                await GLOBAL_STATE.assistant_instance.send_audio(b64)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket Error: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)