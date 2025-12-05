import asyncio
import base64
import json
import os
from typing import cast, Annotated

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Response, Cookie, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

# Modular Imports
from config import logger, validate_env, get_env_display
from session_manager import manager
from state import SessionState
from assistant import BasicVoiceAssistant

app = FastAPI(title="Voice Assistant Multi-User")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ==============================================================================
# DEPENDENCY
# ==============================================================================
async def get_session_state(
    request: Request,
    session_id: Annotated[str | None, Cookie()] = None
) -> SessionState:
    """
    Robust extraction:
    1. Tries to get 'session_id' from Cookies.
    2. If missing, manually pulls 'session_id' from Query Parameters.
    """
    # Manual fallback to avoid FastAPI alias conflicts
    query_id = request.query_params.get("session_id")
    final_id = session_id or query_id
    
    if not final_id:
        # Log the exact request details for debugging
        logger.error(f"Session ID missing. Cookies: {request.cookies.keys()} | Query: {request.query_params}")
        raise HTTPException(status_code=400, detail="No session ID provided in Cookie or Query")
    
    return manager.get_session(final_id)

# ==============================================================================
# HTTP ROUTES
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session_id: Annotated[str | None, Cookie()] = None):
    # Logic: If no cookie, generate ID. 
    # We must set the cookie on the TemplateResponse object itself.
    new_session_id = None
    if not session_id:
        session_id = manager.create_session_id()
        new_session_id = session_id
        logger.info(f"Generated new session_id: {session_id}")
    
    # 1. Create the response object
    response = templates.TemplateResponse("index.html", {
        "request": request, 
        "env": get_env_display(),
        "session_id": session_id
    })
    
    # 2. Set the cookie ON the response object if needed
    if new_session_id:
        response.set_cookie(key="session_id", value=new_session_id, samesite="lax")
    
    return response

@app.get("/events")
async def sse_endpoint(request: Request, state: SessionState = Depends(get_session_state)):
    async def event_generator():
        queue = asyncio.Queue()
        state.sse_queues.append(queue)
        
        yield {
            "data": json.dumps({
                "type": "status",
                "state": state.state,
                "message": state.message,
                "connected": state.connected
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
            if queue in state.sse_queues:
                state.sse_queues.remove(queue)

    return EventSourceResponse(event_generator())

@app.post("/start-session")
async def start_session(state: SessionState = Depends(get_session_state)):
    if state.assistant_task and not state.assistant_task.done():
         return JSONResponse({"started": False, "reason": "Already running"})

    ok, msg = validate_env()
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    state.assistant_instance = BasicVoiceAssistant(
        state_manager=state,
        endpoint=os.environ.get("AZURE_VOICE_LIVE_ENDPOINT"),
        key=os.environ.get("AZURE_VOICE_LIVE_API_KEY"),
        model=os.environ.get("VOICE_LIVE_MODEL"),
        voice=os.environ.get("VOICE_LIVE_VOICE"),
        instructions=os.environ.get("VOICE_LIVE_INSTRUCTIONS") or "You are a helpful assistant."
    )
    
    state.update("starting", "Starting session...")
    state.assistant_task = asyncio.create_task(state.assistant_instance.run())
    
    return {"started": True}

@app.post("/stop-session")
async def stop_session(state: SessionState = Depends(get_session_state)):
    if state.assistant_instance:
        assistant = cast(BasicVoiceAssistant, state.assistant_instance)
        await assistant.stop()
    
    if state.assistant_task:
        try:
            await asyncio.wait_for(state.assistant_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
            
    state.assistant_task = None
    state.assistant_instance = None
    state.update("stopped", "Session stopped manually.")
    return {"stopped": True}

@app.post("/interrupt")
async def interrupt_session(state: SessionState = Depends(get_session_state)):
    if state.assistant_instance:
        assistant = cast(BasicVoiceAssistant, state.assistant_instance)
        await assistant.interrupt()
        state.broadcast_event({"type": "control", "action": "stop_playback"})
        return {"interrupted": True}
    return JSONResponse({"interrupted": False, "reason": "No session"}, status_code=400)

@app.post("/audio-chunk")
async def audio_chunk(request: Request, state: SessionState = Depends(get_session_state)):
    data = await request.json()
    b64 = data.get("audio")
    if state.assistant_instance and b64:
        assistant = cast(BasicVoiceAssistant, state.assistant_instance)
        await assistant.send_audio(b64)
        return {"accepted": True}
    return JSONResponse({"accepted": False}, status_code=400)

@app.websocket("/ws-audio")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    # WebSocket Manual Extraction
    session_id = websocket.cookies.get("session_id")
    if not session_id:
        session_id = websocket.query_params.get("session_id")

    if not session_id:
        logger.warning("WebSocket attempt without session_id")
        await websocket.close(code=1008)
        return

    state = manager.get_session(session_id)
    
    try:
        while True:
            data = await websocket.receive_bytes()
            if state.assistant_instance:
                assistant = cast(BasicVoiceAssistant, state.assistant_instance)
                b64 = base64.b64encode(data).decode("utf-8")
                await assistant.send_audio(b64)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket Error: {e}")

if __name__ == "__main__":
    import uvicorn
    print(f"Starting server on http://127.0.0.1:8000/")
    uvicorn.run(app, host="0.0.0.0", port=8000)