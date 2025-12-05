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
            # PASS session_id to the constructor
            self._sessions[session_id] = SessionState(session_id)
        return self._sessions[session_id]

    def create_session_id(self) -> str:
        """Generates a unique ID for a new user."""
        return str(uuid.uuid4())

# Create a singleton manager instance to be imported by main.py
manager = SessionManager()