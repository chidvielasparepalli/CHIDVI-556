from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class VoiceAuthConfig:
    sample_rate: int = 16000
    threshold: float = 0.62
    min_duration_seconds: float = 1.5
    max_duration_seconds: float = 12.0
    enroll_samples: int = 5
    voiceprint_dir: Path = Path("config") / "voice_auth"
    embedding_filename: str = "chidvielas.voiceprint.npy"

    @property
    def embedding_path(self) -> Path:
        return self.voiceprint_dir / self.embedding_filename

    @classmethod
    def from_env(cls) -> "VoiceAuthConfig":
        return cls(
            threshold=float(os.getenv("CHIDVI_VOICE_THRESHOLD", "0.62")),
            enroll_samples=int(os.getenv("CHIDVI_VOICE_ENROLL_SAMPLES", "5")),
        )
