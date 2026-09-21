from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .config import VoiceAuthConfig
from .speaker_encoder import ECAPATDNNEncoder
from .storage import VoiceprintStore


@dataclass(frozen=True)
class VoiceAuthResult:
    authenticated: bool
    score: float
    threshold: float
    enrolled: bool
    reason: str


class VoiceAuthService:
    """Enrollment and speaker verification for CHIDVI-556."""

    def __init__(self, config: VoiceAuthConfig | None = None):
        self.config = config or VoiceAuthConfig.from_env()
        self.encoder = ECAPATDNNEncoder()
        self.store = VoiceprintStore(self.config.embedding_path)

    def _validate_audio(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if sample_rate != self.config.sample_rate:
            raise ValueError(
                f"Voice authentication expects {self.config.sample_rate} Hz mono audio"
            )

        data = np.asarray(audio, dtype=np.float32).reshape(-1)
        duration = len(data) / sample_rate

        if duration < self.config.min_duration_seconds:
            raise ValueError("Voice sample is too short")
        if duration > self.config.max_duration_seconds:
            data = data[: int(self.config.max_duration_seconds * sample_rate)]

        peak = float(np.max(np.abs(data))) if data.size else 0.0
        if peak < 0.01:
            raise ValueError("Voice sample is too quiet")

        return np.clip(data, -1.0, 1.0)

    def enroll(
        self, samples: Iterable[np.ndarray], sample_rate: int = 16000
    ) -> None:
        embeddings = []
        for sample in samples:
            audio = self._validate_audio(sample, sample_rate)
            embeddings.append(self.encoder.embed(audio))

        if len(embeddings) < self.config.enroll_samples:
            raise ValueError(
                f"Enrollment requires at least {self.config.enroll_samples} good samples"
            )

        self.store.save(self.encoder.centroid(embeddings))

    def verify(
        self, sample: np.ndarray, sample_rate: int = 16000
    ) -> VoiceAuthResult:
        if not self.store.exists():
            return VoiceAuthResult(
                False, 0.0, self.config.threshold, False, "not_enrolled"
            )

        try:
            audio = self._validate_audio(sample, sample_rate)
            probe = self.encoder.embed(audio)
            enrolled = self.store.load()
            score = float(np.dot(probe, enrolled))

            return VoiceAuthResult(
                authenticated=score >= self.config.threshold,
                score=score,
                threshold=self.config.threshold,
                enrolled=True,
                reason="match" if score >= self.config.threshold else "no_match",
            )
        except Exception as exc:
            return VoiceAuthResult(
                False, 0.0, self.config.threshold, True, str(exc)
            )
