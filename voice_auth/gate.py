from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

import numpy as np

from .service import VoiceAuthService


ALLOW_RE = re.compile(
    r"\b(?:that's okay|thats okay|that is okay|allow(?: it)?|"
    r"you can answer|answer everyone|answer them|it's okay|its okay|"
    r"okay answer|go ahead and answer)\b",
    re.IGNORECASE,
)

REJECTION_TEXT = "I won't answer your questions cause you are not my owner."


@dataclass
class VoiceGateState:
    owner_verified: bool = False
    open_for_everyone: bool = False


class VoiceAuthGate:
    """Utterance-level gate. Audio is verified locally before it reaches Gemini."""

    def __init__(self, service: VoiceAuthService, speak_rejection, loop):
        self.service = service
        self.state = VoiceGateState()
        self._speak_rejection = speak_rejection
        self._loop = loop
        self._buffer = bytearray()
        self._speaking = False
        self._silence_seconds = 0.0
        self._last_rejection = 0.0
        self._processing = False

    @property
    def bypassed(self) -> bool:
        return self.state.open_for_everyone

    def reset(self) -> None:
        self.state = VoiceGateState()
        self._buffer.clear()
        self._speaking = False
        self._silence_seconds = 0.0

    def add_pcm(self, data: bytes, level: float, frame_seconds: float) -> None:
        if self._processing:
            return

        self._buffer.extend(data)
        if level >= 0.025:
            self._speaking = True
            self._silence_seconds = 0.0
            return

        if self._speaking:
            self._silence_seconds += frame_seconds
            if self._silence_seconds >= 0.65:
                utterance = bytes(self._buffer)
                self._buffer.clear()
                self._speaking = False
                self._silence_seconds = 0.0
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._finish_utterance(utterance))
                )

        # Prevent an idle mic from growing the buffer forever.
        if len(self._buffer) > 16000 * 2 * 14:
            self._buffer.clear()

    async def _finish_utterance(self, pcm_bytes: bytes) -> None:
        self._processing = True
        try:
            audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            if len(audio) < 16000 * 1.2:
                return

            if self.state.open_for_everyone:
                await self._send_to_gemini(pcm_bytes)
                return

            if self.state.owner_verified:
                # Re-check every utterance. This prevents a different speaker from
                # inheriting the owner's authorization after the owner stops talking.
                result = await asyncio.to_thread(self.service.verify, audio, 16000)
                if result.authenticated:
                    await self._send_to_gemini(pcm_bytes)
                    return
            else:
                result = await asyncio.to_thread(self.service.verify, audio, 16000)
                if result.authenticated:
                    self.state.owner_verified = True
                    await self._send_to_gemini(pcm_bytes)
                    return

            # The non-owner may explicitly open the assistant for everyone.
            transcript = await self._local_transcribe(audio)
            if ALLOW_RE.search(transcript):
                self.state.open_for_everyone = True
                await self._send_to_gemini(pcm_bytes)
                return

            now = time.monotonic()
            if now - self._last_rejection > 2.0:
                self._last_rejection = now
                await asyncio.to_thread(self._speak_rejection, REJECTION_TEXT)
        finally:
            self._processing = False

    async def _local_transcribe(self, audio: np.ndarray) -> str:
        def transcribe():
            from core.stt import WhisperSTT
            engine = getattr(self, "_whisper", None)
            if engine is None:
                engine = WhisperSTT(model_name="base", language="en")
                self._whisper = engine
            return engine.transcribe(audio)

        try:
            return await asyncio.to_thread(transcribe)
        except Exception:
            return ""

    async def _send_to_gemini(self, pcm_bytes: bytes) -> None:
        # This callback is replaced by main.py after the Live session is created.
        if self._send_audio is not None:
            await self._send_audio(pcm_bytes)

    _send_audio = None
