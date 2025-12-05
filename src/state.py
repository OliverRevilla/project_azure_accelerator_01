import asyncio
from typing import Optional, List, Dict, Any
from database import SessionLocal, ChatMessageModel

class SessionState:
    # Represents the state of an active assistant session
    def __init__(self, session_id: str):
        self.session_id = session_id  # Store the ID
        self.state = "idle"
        self.message = "Select 'Start Session' to begin"
        self.last_error: Optional[str] = None
        self.connected = False
        self.assistant_task: Optional[asyncio.Task] = None
        self.assistant_instance: Any = None
        self.sse_queues: List[asyncio.Queue] = [] # Queues for Server-Sent Events (SSE)
        self.chat_history: List[Dict[str, str]] = [] # NEW: Load history from Database immediately
        self._load_history_from_db()        

    def _load_history_from_db(self):
        """Fetch past messages for this session_id from SQL."""
        try:
            db = SessionLocal()
            # Query messages sorted by time
            msgs = db.query(ChatMessageModel).filter(
                ChatMessageModel.session_id == self.session_id
            ).order_by(ChatMessageModel.created_at).all()
            
            # Convert SQL models to simple dicts for the UI
            for m in msgs:
                self.chat_history.append({"role": m.role, "text": m.content})
            
            db.close()
        except Exception as e:
            print(f"DB Load Error: {e}")

    def update(self, state:str, message: str, error: Optional[str]=None):
        # Update internal state and notify clients
        self.state = state
        self.message = message
        if error:
            self.last_error = error

        # Determine connection status based on state
        if state in {"ready","listening","processing","assistant_speaking"}:
            self.connected = True
        elif state in {"stopped","idle","error"}:
            self.connected = False

        self.broadcast_status()
        
    def add_chat_message(self, role: str, text: str):
        """
        Adds a message to history, SAVES TO DB, and broadcasts it.
        """
        if not text:
            return
            
        # 1. Update In-Memory
        msg_obj = {"role": role, "text": text}
        self.chat_history.append(msg_obj)
        
        # 2. Save to Database
        try:
            db = SessionLocal()
            db_msg = ChatMessageModel(
                session_id=self.session_id,
                role=role,
                content=text
            )
            db.add(db_msg)
            db.commit()
            db.close()
        except Exception as e:
            print(f"DB Save Error: {e}")
        
        # 3. Broadcast to UI
        self.broadcast_event({
            "type": "chat_message",
            "message": msg_obj
        })

    def broadcast_status(self):
        # Send status update to all connected SSE clients
        payload = {
            "type":"status",
            "state":self.state,
            "message":self.message,
            "last_error":self.last_error,
            "connected":self.connected
        }
        self.broadcast_event(payload)

    def broadcast_event(self, data: Dict[str, Any]):
        # Push a message to all active SSE queues
        for q in self.sse_queues:
            q.put_nowait(data)
