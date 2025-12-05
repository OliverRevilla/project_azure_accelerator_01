import asyncio
from typing import Optional, List, Dict, Any

class SessionState:
    # Represents the state of an active assistant session
    def __init__(self):
        self.state = "idle"
        self.message = "Select 'Start Session' to begin"
        self.last_error: Optional[str] = None
        self.connected = False

        self.assistant_task: Optional[asyncio.Task] = None
        self.assistant_instance: Any = None

        # Queues for Server-Sent Events (SSE)
        self.sse_queues: List[asyncio.Queue] = []

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
