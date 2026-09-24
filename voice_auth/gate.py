from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from .service import VoiceAuthService


ALLOW_RE = re.compile(
    r"\b(?:that's okay|thats okay|that is okay|allow(?: it)?|"
    r"you can answer|you may answer|answer everyone|answer them|let them ask|"
    r"answer anyone|anyone can talk|dont restrict|don't restrict|"
    r"it's okay|its okay|okay answer|go ahead and answer)\b",
    re.IGNORECASE,
)


REJECTION_TEXT = "I can’t respond until I recognize your authorized voice."


@dataclass
class VoiceGateState:
    owner_verified: bool = False
    open_for_everyone: bool = False


class VoiceAuthGate:
    """Local utterance gate placed in front of Gemini Live.

    Audio never reaches Gemini until it is either:
    - accepted by local speaker verification, or
    - explicitly released by an already-authenticated owner.

    The gate also uses a short pre-roll and a bounded utterance length so idle
    microphone silence cannot contaminate the next speaker embedding.
    """

    def __init__(
        self,
        service: VoiceAuthService,
        speak_rejection,
        loop,
        *,
        pre_roll_seconds: float = 0.25,
        silence_seconds: float = 0.65,
        max_utterance_seconds: float = 12.0,
        rejection_cooldown_seconds: float = 2.0,
    ):
        self.service = service
        self.state = VoiceGateState()
        self._speak_rejection = speak_rejection
        self._loop = loop
        self._pre_roll_seconds = max(0.0, pre_roll_seconds)
        self._silence_limit = max(0.2, silence_seconds)
        self._max_utterance_seconds = max(2.0, max_utterance_seconds)
        self._rejection_cooldown = max(0.5, rejection_cooldown_seconds)

        pre_roll_bytes = int(16000 * 2 * self._pre_roll_seconds)
        self._pre_roll = deque(maxlen=max(160, pre_roll_bytes))
        self._buffer = bytearray()
        self._speaking = False
        self._speech_seconds = 0.0
        self._silence_seconds = 0.0
        self._last_rejection = 0.0
        self._ignore_until = 0.0
        self._processing = False

    @property
    def bypassed(self) -> bool:
        return self.state.open_for_everyone

    def grant_public_access(self) -> None:
        """Called only after an utterance has already passed voice auth."""
        self.state.open_for_everyone = True

    def reset(self) -> None:
        self.state = VoiceGateState()
        self._pre_roll.clear()
        self._buffer.clear()
        self._speaking = False
        self._speech_seconds = 0.0
        self._silence_seconds = 0.0
        self._last_rejection = 0.0
        self._ignore_until = 0.0
        self._processing = False

    def add_pcm(self, data: bytes, level: float, frame_seconds: float) -> None:
        if not data or self._processing:
            return
        if time.monotonic() < self._ignore_until:
            return

        frame_seconds = max(0.0, float(frame_seconds))
        self._pre_roll.extend(data)

        if level >= 0.025:
            if not self._speaking:
                # First speech frame: include only a short pre-roll, not the
                # arbitrary amount of idle silence that preceded it.
                self._buffer = bytearray(self._pre_roll)
                self._speaking = True
                self._speech_seconds = 0.0
            else:
                self._buffer.extend(data)
            self._speech_seconds += frame_seconds
            self._silence_seconds = 0.0
        elif self._speaking:
            self._buffer.extend(data)
            self._silence_seconds += frame_seconds

        if not self._speaking:
            return

        if self._speech_seconds >= self._max_utterance_seconds:
            self._finish_later(bytes(self._buffer))
            self._clear_current_utterance()
        elif self._silence_seconds >= self._silence_limit:
            utterance = bytes(self._buffer)
            self._clear_current_utterance()
            self._finish_later(utterance)

    def _clear_current_utterance(self) -> None:
        self._buffer.clear()
        self._speaking = False
        self._speech_seconds = 0.0
        self._silence_seconds = 0.0

    def _finish_later(self, utterance: bytes) -> None:
        if not utterance or self._processing:
            return
        self._processing = True
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._finish_utterance(utterance))
        )

    async def _finish_utterance(self, pcm_bytes: bytes) -> None:
        try:
            audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            if len(audio) < 16000 * 1.2:
                return

            if self.state.open_for_everyone:
                await self._send_to_gemini(pcm_bytes)
                return

            result = await asyncio.to_thread(self.service.verify, audio, 16000)
            if result.authenticated:
                self.state.owner_verified = True
                await self._send_to_gemini(pcm_bytes)
                return

            # Do not offer an unauthenticated voice command that can disable the
            # security gate. Public-access override is granted only from main.py
            # after Gemini has received this already-authenticated utterance.
            now = time.monotonic()
            if now - self._last_rejection >= self._rejection_cooldown:
                self._last_rejection = now
                self._ignore_until = now + self._rejection_cooldown + 0.5
                await asyncio.to_thread(self._speak_rejection, REJECTION_TEXT)
        finally:
            self._processing = False

    async def _send_to_gemini(self, pcm_bytes: bytes) -> None:
        if self._send_audio is None:
            return
        result = self._send_audio(pcm_bytes)
        if inspect.isawaitable(result):
            await result

    _send_audio = None
