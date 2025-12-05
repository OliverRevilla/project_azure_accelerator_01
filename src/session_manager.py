import uuid
from typing import Dict
from state import SessionState

class SessionManager:
    # Manages multiple assistant sessions
    def __init__(self):
        self._sessions: Dict[str, SessionState] = {}

    def get_session(self, session_id: str) -> SessionState:
        # Retrieve existing session or create a new one
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState()
        return self._sessions[session_id]
    
    def create_session_id(self) -> str:
        # Generate a new unique session ID
        return str(uuid.uuid4())

manager = SessionManager()