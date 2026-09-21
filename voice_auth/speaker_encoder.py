from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np


class ECAPATDNNEncoder:
    """Speaker encoder backed by SpeechBrain's pretrained ECAPA-TDNN model."""

    def __init__(self, device: str | None = None):
        from speechbrain.inference.speaker import EncoderClassifier
        import torch

        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=str(
                Path.home() / ".cache" / "speechbrain" / "spkrec-ecapa-voxceleb"
            ),
            run_opts={"device": self.device},
        )

    def embed(self, audio: np.ndarray) -> np.ndarray:
        import torch

        signal = np.asarray(audio, dtype=np.float32).reshape(-1)
        tensor = torch.from_numpy(signal).float().unsqueeze(0).to(self.device)
        with torch.no_grad():
            embedding = self.model.encode_batch(tensor)
        vector = embedding.squeeze().detach().cpu().numpy().astype(np.float32)
        norm = np.linalg.norm(vector)
        if norm == 0:
            raise ValueError("Speaker embedding has zero norm")
        return vector / norm

    def centroid(self, embeddings: Iterable[np.ndarray]) -> np.ndarray:
        vectors = [np.asarray(v, dtype=np.float32) for v in embeddings]
        if not vectors:
            raise ValueError("At least one embedding is required")
        centroid = np.mean(vectors, axis=0)
        norm = np.linalg.norm(centroid)
        if norm == 0:
            raise ValueError("Voiceprint centroid has zero norm")
        return (centroid / norm).astype(np.float32)
