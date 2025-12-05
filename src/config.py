import os
import logging

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("voice_assistant")


def validate_env() -> tuple[bool, str]:
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


def get_env_display():
    """Return safe-to-display env vars for the UI."""
    return {
        "VOICE_LIVE_MODEL": os.environ.get("VOICE_LIVE_MODEL"),
        "VOICE_LIVE_VOICE": os.environ.get("VOICE_LIVE_VOICE"),
        "AZURE_VOICE_LIVE_ENDPOINT": os.environ.get("AZURE_VOICE_LIVE_ENDPOINT"),
        "VOICE_LIVE_INSTRUCTIONS": os.environ.get("VOICE_LIVE_INSTRUCTIONS")
    }