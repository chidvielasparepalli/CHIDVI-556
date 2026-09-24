from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os


def _default_voiceprint_dir() -> Path:
    # Anchor biometric storage to the repository/application directory instead
    # of the process working directory. This prevents re-enrollment or loading
    # the wrong voiceprint when CHIDVI is launched from another directory.
    return Path(__file__).resolve().parent.parent / "config" / "voice_auth"


@dataclass(frozen=True)
class VoiceAuthConfig:
    sample_rate: int = 16000
    threshold: float = 0.62
    min_duration_seconds: float = 1.5
    max_duration_seconds: float = 12.0
    enroll_samples: int = 5
    voiceprint_dir: Path = field(default_factory=_default_voiceprint_dir)
    embedding_filename: str = "chidvielas.voiceprint.npy"

    @property
    def embedding_path(self) -> Path:
        return self.voiceprint_dir / self.embedding_filename

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "VoiceAuthConfig":
        root = Path(base_dir).resolve() if base_dir is not None else Path(__file__).resolve().parent.parent
        return cls(
            sample_rate=int(os.getenv("CHIDVI_VOICE_SAMPLE_RATE", "16000")),
            threshold=float(os.getenv("CHIDVI_VOICE_THRESHOLD", "0.62")),
            min_duration_seconds=float(os.getenv("CHIDVI_VOICE_MIN_SECONDS", "1.5")),
            max_duration_seconds=float(os.getenv("CHIDVI_VOICE_MAX_SECONDS", "12.0")),
            enroll_samples=max(3, int(os.getenv("CHIDVI_VOICE_ENROLL_SAMPLES", "5"))),
            voiceprint_dir=root / "config" / "voice_auth",
        )
