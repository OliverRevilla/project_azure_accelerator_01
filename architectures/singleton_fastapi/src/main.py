import asyncio
import base64
import json
import os
from typing import cast # Added for type casting

# NEW: Load environment variables from .env file automatically
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass # python-dotenv not installed, assuming env vars set manually

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

# Modular Imports
from config import logger, validate_env, get_env_display
from state import GLOBAL_STATE
from assistant import BasicVoiceAssistant

# App Setup
app = FastAPI(title="Voice Assistant")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ==============================================================================
# HTTP ROUTES
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "env": get_env_display()})

@app.get("/events")
async def sse_endpoint(request: Request):
    """Server-Sent Events endpoint."""
    async def event_generator():
        queue = asyncio.Queue()
        GLOBAL_STATE.sse_queues.append(queue)
        
        # Send initial state immediately
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

    ok, msg = validate_env()
    if not ok:
        # This is where your 400 error comes from
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
        # Explicit cast to help IDE recognize the method
        assistant = cast(BasicVoiceAssistant, GLOBAL_STATE.assistant_instance)
        await assistant.stop()
    
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
        # Explicit cast to help IDE recognize the method
        assistant = cast(BasicVoiceAssistant, GLOBAL_STATE.assistant_instance)
        await assistant.interrupt()
        
        GLOBAL_STATE.broadcast_event({"type": "control", "action": "stop_playback"})
        return {"interrupted": True}
    return JSONResponse({"interrupted": False, "reason": "No session"}, status_code=400)

@app.post("/audio-chunk")
async def audio_chunk(request: Request):
    """HTTP Fallback for audio upload."""
    data = await request.json()
    b64 = data.get("audio")
    if GLOBAL_STATE.assistant_instance and b64:
        # Explicit cast to help IDE recognize the method
        assistant = cast(BasicVoiceAssistant, GLOBAL_STATE.assistant_instance)
        await assistant.send_audio(b64)
        
        return {"accepted": True}
    return JSONResponse({"accepted": False}, status_code=400)

# ==============================================================================
# WEBSOCKET ROUTE
# ==============================================================================

@app.websocket("/ws-audio")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            # Receive binary PCM16 data
            data = await websocket.receive_bytes()
            if GLOBAL_STATE.assistant_instance:
                # Explicit cast to help IDE recognize the method
                assistant = cast(BasicVoiceAssistant, GLOBAL_STATE.assistant_instance)
                b64 = base64.b64encode(data).decode("utf-8")
                await assistant.send_audio(b64)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket Error: {e}")

if __name__ == "__main__":
    import uvicorn
    # If running directly, we print a hint about the server address
    print(f"Starting server on http://0.0.0.0:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)