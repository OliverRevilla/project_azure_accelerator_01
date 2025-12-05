import asyncio
import base64
import os
from typing import Optional
from config import logger

from azure.core.credentials import AzureKeyCredential
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    RequestSession,
    ServerVad,
    AzureStandardVoice,
    Modality,
    InputAudioFormat,
    OutputAudioFormat,
    ServerEventType
)

class BasicVoiceAssistant:
    # Async wrapper for Azure Voice Live
    def __init__(self, state_manager,endpoint:str, key:str, model:str, voice:str, instructions: str):
        self.state_manager = state_manager
        self.endpoint = endpoint
        self.key = key
        self.voice = voice
        self.model = model
        self.instructions = instructions
        self.connection = None
        self._response_cancelled = False,
        self._stopping = False

    async def run(self):
        credential = AzureKeyCredential(self.key)

        try:
            self.state_manager.broadcast_event({
                "type":"log",
                "msg":f"Connecting to {self.endpoint}..."
            })
            async with connect(
                endpoint=self.endpoint,
                credential=credential,
                model=self.model,
                connection_options={"max_msg_size": 10 * 1024 * 1024}
            ) as conn:
                self.connection = conn
                self._response_cancelled = False


                voice_cfg = AzureStandardVoice(name=self.voice) if "-" in self.voice else self.voice

                await conn.session.update(session=RequestSession(
                    modalities=[Modality.TEXT, Modality.AUDIO],
                    instructions=self.instructions,
                    voice=voice_cfg,
                    input_audio_format=InputAudioFormat.PCM16,
                    output_audio_format=OutputAudioFormat.PCM16,
                    turn_detection=ServerVad(threshold=0.5, prefix_padding_ms=300, silence_duration_ms=500)
                ))

                self.state_manager.update("ready", "Session Ready. Speak now.")

                async for event in conn:
                    if self._stopping:
                        break
                    await self._handle_event(event, conn, ServerEventType)
       
        except Exception as e:
            logger.error(f"Assistant Error: {e}")
            self.state_manager.update("error", f"Crash: {str(e)}", error=str(e))
        finally:
            self.connection = None
            self.state_manager.update("stopped", "Session Ended")
        
    async def _handle_event(self, event, conn, ServerEventType):
        # Route events to specfic handlers
        if event.type == ServerEventType.SESSION_UPDATED:
            self.state_manager.update("ready", "Ready")
        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            self.state_manager.update("listening", "Listening...")
            self.state_manager.broadcast_event({"type": "control", "action": "stop_playback"})
            # Interrupt if speaking
            if self.state_manager.state in {"assistant_speaking", "processing"}:
                self._response_cancelled = True
                await conn.response.cancel()
        
        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            self.state_manager.update("processing", "Processing...")
        
        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            if not self._response_cancelled:
                if self.state_manager.state != "assistant_speaking":
                    self.state_manager.update("assistant_speaking", "Assistant Speaking...")
                
                if hasattr(event, "delta") and event.delta:
                    b64 = base64.b64encode(event.delta).decode("utf-8")
                    self.state_manager.broadcast_event({"type": "audio", "audio": b64})
        
        elif event.type == ServerEventType.RESPONSE_AUDIO_DONE:
            self._response_cancelled = False
            self.state_manager.update("ready", "Finished speaking.")
        
        elif event.type == ServerEventType.ERROR:
            msg = getattr(event.error, "message", "Unknown Error")
            self.state_manager.update("error", f"Azure Error: {msg}", error=msg)

    async def stop(self):
        self._stopping = True
    
    async def send_audio(self, b64_audio: str):
        if self.connection:
            await self.connection.input_audio_buffer.append(audio=b64_audio)
    
    async def interrupt(self):
        self._response_cancelled = True
        if self.connection:
            try:
                await self.connection.response.cancel()
            except Exception:
                pass