import asyncio
import base64
import json
import os
from typing import cast, Annotated, Optional

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Response, Cookie, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import func

from config import logger, validate_env, get_env_display
from session_manager import manager
from state import SessionState
from assistant import BasicVoiceAssistant
# NEW: Import DB init
from database import init_db, SessionLocal, ChatMessageModel 

# Initialize Tables on startup
init_db()

app = FastAPI(title="Voice Assistant Multi-User")

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Log the incoming request for easier debugging when a 422 occurs
    body = None
    try:
        body = await request.body()
    except Exception:
        body = b"<unreadable>"
    logger.error(
        "Request validation failed: %s | Path: %s | Body: %s",
        exc.errors(),
        request.url.path,
        body,
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": exc.errors(),
            "body": body.decode(errors='replace') if isinstance(body, (bytes, bytearray)) else str(body),
        },
    )

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

        # 2. NEW: Send existing Chat History to the frontend on reconnect
        for msg in state.chat_history:
            yield {
                "data": json.dumps({
                    "type": "chat_message",
                    "message": msg
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

class StartSessionRequest(BaseModel):
    voice: Optional[str] = "alloy"
    instructions: Optional[str] = ""
    max_tokens: Optional[int] = Field(500, alias="maxTokens")

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Response, Cookie, Depends, Body

@app.post("/start-session")
async def start_session(req: Optional[StartSessionRequest] = Body(None), state: SessionState = Depends(get_session_state)):
    if state.assistant_task and not state.assistant_task.done():
         return JSONResponse({"started": False, "reason": "Already running"})

    ok, msg = validate_env()
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    # Completely decoupled from .env for Voice and Instructions
    voice_to_use = req.voice if req and req.voice else "alloy"
    instructions_to_use = req.instructions if req and req.instructions else "You are a helpful assistant."
    max_tokens_to_use = req.max_tokens if req and req.max_tokens else 500

    state.assistant_instance = BasicVoiceAssistant(
        state_manager=state,
        endpoint=os.environ.get("AZURE_VOICE_LIVE_ENDPOINT"),
        key=os.environ.get("AZURE_VOICE_LIVE_API_KEY"),
        model=os.environ.get("VOICE_LIVE_MODEL"),
        voice=voice_to_use,
        instructions=instructions_to_use,
        max_tokens=max_tokens_to_use
    )
    
    state.update("starting", "Starting session...")
    state.assistant_task = asyncio.create_task(state.assistant_instance.run())
    
    return {"started": True}

@app.post("/stop-session")
async def stop_session(state: SessionState = Depends(get_session_state)):
    if state.assistant_instance:
        assistant = cast(BasicVoiceAssistant, state.assistant_instance)
        await assistant.interrupt()  # Cut audio playback immediately
        await assistant.stop()

    if state.assistant_task and not state.assistant_task.done():
        state.assistant_task.cancel()
        try:
            await asyncio.wait_for(state.assistant_task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    state.assistant_task = None
    state.assistant_instance = None
    state.update("stopped", "Session stopped manually.")
    return {"stopped": True}

@app.post("/update-session")
async def update_session(req: Optional[StartSessionRequest] = Body(None), state: SessionState = Depends(get_session_state)):
    if state.assistant_instance and req:
        assistant = cast(BasicVoiceAssistant, state.assistant_instance)
        await assistant.update_session_config(
            voice=req.voice,
            instructions=req.instructions,
            max_tokens=req.max_tokens
        )
        return {"updated": True}
    return JSONResponse({"updated": False, "reason": "No active session or empty payload"}, status_code=400)

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

@app.get("/export-transcript")
async def export_transcript(request: Request, session_id: Annotated[str | None, Cookie()] = None):
    query_id = request.query_params.get("session_id")
    final_id = session_id or query_id
    if not final_id:
        raise HTTPException(status_code=400, detail="No session ID")
    
    db = SessionLocal()
    msgs = db.query(ChatMessageModel).filter(ChatMessageModel.session_id == final_id).order_by(ChatMessageModel.created_at).all()
    db.close()
    
    content = f"Transcript for Session: {final_id}\n"
    content += "="*50 + "\n\n"
    for m in msgs:
        content += f"[{m.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {m.role.upper()}:\n{m.content}\n\n"
        
    headers = {"Content-Disposition": f"attachment; filename=transcript_{final_id[:8]}.txt"}
    return PlainTextResponse(content, headers=headers)

@app.get("/history", response_class=HTMLResponse)
async def view_history(request: Request):
    db = SessionLocal()
    # Get distinct session ids and their last message time
    sessions = db.query(
        ChatMessageModel.session_id, 
        func.max(ChatMessageModel.created_at).label('last_active'),
        func.count(ChatMessageModel.id).label('msg_count')
    ).group_by(ChatMessageModel.session_id).order_by(func.max(ChatMessageModel.created_at).desc()).all()
    db.close()
    
    return templates.TemplateResponse("history.html", {
        "request": request,
        "sessions": sessions
    })

@app.websocket("/ws-audio")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
# ==============================================================================
# WEB SOCKET AUDIO STREAMING
# ==============================================================================
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

# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    import uvicorn
    print(f"Starting server on http://127.0.0.1:8000/")
    uvicorn.run(app, host="0.0.0.0", port=8000)