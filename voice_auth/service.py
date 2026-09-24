from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

import numpy as np

from .config import VoiceAuthConfig
from .speaker_encoder import ECAPATDNNEncoder
from .storage import VoiceprintStore


class SpeakerEncoder(Protocol):
    def embed(self, audio: np.ndarray) -> np.ndarray: ...
    def centroid(self, embeddings: Iterable[np.ndarray]) -> np.ndarray: ...


@dataclass(frozen=True)
class VoiceAuthResult:
    authenticated: bool
    score: float
    threshold: float
    enrolled: bool
    reason: str


class VoiceAuthService:
    """Local speaker verification for CHIDVI-556.

    The expensive ECAPA model is created exactly once per service instance. A
    caller may inject an encoder/store in tests, keeping the biometric path
    deterministic without downloading the pretrained model.
    """

    def __init__(
        self,
        config: VoiceAuthConfig | None = None,
        *,
        encoder: SpeakerEncoder | None = None,
        store: VoiceprintStore | None = None,
    ):
        self.config = config or VoiceAuthConfig.from_env()
        self.encoder = encoder or ECAPATDNNEncoder()
        self.store = store or VoiceprintStore(self.config.embedding_path)

        if not 0.0 < self.config.threshold < 1.0:
            raise ValueError("Voice authentication threshold must be between 0 and 1.")
        if self.config.min_duration_seconds <= 0:
            raise ValueError("Minimum voice duration must be positive.")
        if self.config.max_duration_seconds < self.config.min_duration_seconds:
            raise ValueError("Maximum voice duration must be >= minimum duration.")

    @staticmethod
    def _trim_silence(data: np.ndarray, sample_rate: int) -> np.ndarray:
        """Remove long leading/trailing silence before speaker embedding."""
        if data.size < sample_rate // 5:
            return data

        frame = max(160, int(sample_rate * 0.02))
        count = len(data) // frame
        if count == 0:
            return data

        frames = data[: count * frame].reshape(count, frame)
        rms = np.sqrt(np.mean(frames * frames, axis=1))
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        threshold = max(0.008, peak * 0.10)
        active = np.flatnonzero(rms >= threshold)

        if active.size == 0:
            return data

        pad = int(sample_rate * 0.08)
        start = max(0, int(active[0]) * frame - pad)
        end = min(len(data), (int(active[-1]) + 1) * frame + pad)
        return data[start:end]

    def _validate_audio(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if sample_rate != self.config.sample_rate:
            raise ValueError(
                f"Voice authentication expects {self.config.sample_rate} Hz mono audio"
            )

        data = np.asarray(audio, dtype=np.float32).reshape(-1)
        if data.size == 0 or not np.all(np.isfinite(data)):
            raise ValueError("Voice sample contains no valid audio.")

        raw_duration = len(data) / sample_rate
        if raw_duration < self.config.min_duration_seconds:
            raise ValueError("Voice sample is too short")

        if raw_duration > self.config.max_duration_seconds:
            data = data[: int(self.config.max_duration_seconds * sample_rate)]

        data = np.clip(data, -1.0, 1.0)
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        rms = float(np.sqrt(np.mean(data * data))) if data.size else 0.0
        if peak < 0.01 or rms < 0.003:
            raise ValueError("Voice sample is too quiet")

        data = self._trim_silence(data, sample_rate)
        if len(data) / sample_rate < self.config.min_duration_seconds:
            raise ValueError("Not enough speech was detected in the voice sample.")
        return data

    def enroll(self, samples: Iterable[np.ndarray], sample_rate: int = 16000) -> None:
        embeddings: list[np.ndarray] = []
        for index, sample in enumerate(samples, start=1):
            audio = self._validate_audio(sample, sample_rate)
            embedding = np.asarray(self.encoder.embed(audio), dtype=np.float32).reshape(-1)
            if embedding.size == 0 or not np.all(np.isfinite(embedding)):
                raise ValueError(f"Enrollment sample {index} produced an invalid embedding.")
            embeddings.append(embedding)

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
            probe = np.asarray(self.encoder.embed(audio), dtype=np.float32).reshape(-1)
            enrolled = self.store.load()

            if probe.shape != enrolled.shape:
                raise ValueError("Voiceprint and probe embeddings have incompatible shapes.")
            if not np.all(np.isfinite(probe)):
                raise ValueError("Probe embedding contains invalid values.")

            norm = float(np.linalg.norm(probe))
            if norm <= 1e-8:
                raise ValueError("Probe embedding has zero norm")
            probe = probe / norm

            score = float(np.dot(probe, enrolled))
            if not np.isfinite(score):
                raise ValueError("Speaker verification returned an invalid score.")

            authenticated = score >= self.config.threshold
            return VoiceAuthResult(
                authenticated=authenticated,
                score=score,
                threshold=self.config.threshold,
                enrolled=True,
                reason="match" if authenticated else "no_match",
            )
        except Exception as exc:
            return VoiceAuthResult(
                False, 0.0, self.config.threshold, True, str(exc)
            )
