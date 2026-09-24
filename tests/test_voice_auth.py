from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import numpy as np

from voice_auth.config import VoiceAuthConfig
from voice_auth.gate import VoiceAuthGate
from voice_auth.service import VoiceAuthResult, VoiceAuthService


class FakeEncoder:
    def embed(self, audio: np.ndarray) -> np.ndarray:
        return np.array(
            [float(np.mean(audio)), float(np.std(audio)), float(np.mean(np.abs(audio)))],
            dtype=np.float32,
        )

    def centroid(self, embeddings):
        vectors = [np.asarray(v, dtype=np.float32) for v in embeddings]
        centroid = np.mean(vectors, axis=0)
        return centroid / np.linalg.norm(centroid)


class FakeGateService:
    def __init__(self, authenticated: bool):
        self.authenticated = authenticated

    def verify(self, sample, sample_rate):
        return VoiceAuthResult(
            authenticated=self.authenticated,
            score=0.9 if self.authenticated else 0.2,
            threshold=0.62,
            enrolled=True,
            reason="match" if self.authenticated else "no_match",
        )


class VoiceAuthTests(unittest.TestCase):
    def test_config_storage_is_anchored_to_base_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = VoiceAuthConfig.from_env(Path(tmp))
            self.assertEqual(cfg.embedding_path, Path(tmp) / "config" / "voice_auth" / "chidvielas.voiceprint.npy")

    def test_service_enroll_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = VoiceAuthConfig(
                enroll_samples=3,
                voiceprint_dir=Path(tmp),
                threshold=0.5,
            )
            service = VoiceAuthService(cfg, encoder=FakeEncoder())
            samples = [
                np.sin(np.linspace(0, 10, 16000 * 2)).astype(np.float32),
                np.sin(np.linspace(0, 11, 16000 * 2)).astype(np.float32),
                np.sin(np.linspace(0, 12, 16000 * 2)).astype(np.float32),
            ]
            service.enroll(samples)
            result = service.verify(samples[0])
            self.assertTrue(result.authenticated)
            self.assertTrue(result.enrolled)

    def test_gate_does_not_release_rejected_audio(self):
        async def run():
            loop = asyncio.get_running_loop()
            sent = []
            rejected = []
            gate = VoiceAuthGate(FakeGateService(False), rejected.append, loop)
            gate._send_audio = lambda data: sent.append(data)
            audio = (np.ones(16000 * 2, dtype=np.int16)).tobytes()
            await gate._finish_utterance(audio)
            self.assertEqual(sent, [])
            self.assertEqual(len(rejected), 1)

        asyncio.run(run())

    def test_gate_releases_authenticated_audio(self):
        async def run():
            loop = asyncio.get_running_loop()
            sent = []
            gate = VoiceAuthGate(FakeGateService(True), lambda _: None, loop)
            gate._send_audio = lambda data: sent.append(data)
            audio = (np.ones(16000 * 2, dtype=np.int16)).tobytes()
            await gate._finish_utterance(audio)
            self.assertEqual(len(sent), 1)
            self.assertTrue(gate.state.owner_verified)

        asyncio.run(run())

    def test_public_access_is_explicit(self):
        async def run():
            loop = asyncio.get_running_loop()
            sent = []
            gate = VoiceAuthGate(FakeGateService(False), lambda _: None, loop)
            gate._send_audio = lambda data: sent.append(data)
            self.assertFalse(gate.bypassed)
            gate.grant_public_access()
            audio = (np.ones(16000 * 2, dtype=np.int16)).tobytes()
            await gate._finish_utterance(audio)
            self.assertTrue(gate.bypassed)
            self.assertEqual(len(sent), 1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
