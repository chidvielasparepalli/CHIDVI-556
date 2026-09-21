from __future__ import annotations

from pathlib import Path
import os
import tempfile

import numpy as np


class VoiceprintStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def save(self, embedding: np.ndarray) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        embedding = np.asarray(embedding, dtype=np.float32)

        fd, tmp_name = tempfile.mkstemp(
            prefix="voiceprint_", suffix=".npy", dir=self.path.parent
        )
        os.close(fd)
        try:
            np.save(tmp_name, embedding)
            os.replace(tmp_name, self.path)
        finally:
            Path(tmp_name).unlink(missing_ok=True)

        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def load(self) -> np.ndarray:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        vector = np.load(self.path, allow_pickle=False).astype(np.float32)
        norm = np.linalg.norm(vector)
        if norm == 0:
            raise ValueError("Stored voiceprint has zero norm")
        return vector / norm
