import base64
from contextlib import nullcontext
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import struct
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from core import reference_voice as voice
from core import reference_voice_worker as worker


class FakeProcess:
    def __init__(self, responses):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(item) + "\n" for item in responses))
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    kill = terminate

    def wait(self, timeout):
        return 0


class ReferenceVoiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.recording = self.root / "reference.wav"
        self.recording.write_bytes(b"unchanged reference fixture")
        self.model_dir = self.root / "model"
        self.model_dir.mkdir()
        for name in voice._MODEL_FILES:
            (self.model_dir / name).write_bytes(b"mock local checkpoint")
        self.settings = {
            "authorized": True,
            "reference_audio": str(self.recording),
            "reference_sha256": hashlib.sha256(self.recording.read_bytes()).hexdigest(),
            "model_dir": str(self.model_dir),
            "device": "cuda",
        }

    def load(self):
        (self.root / "voice.json").write_text(json.dumps(self.settings), encoding="utf-8")
        result = voice.load_reference_voice({"reference_config": "voice.json"}, self.root)
        self.addCleanup(result.close)
        return result

    def test_profiles_without_reference_configuration_do_not_start_a_worker(self):
        from Personality.loader import discover_personalities
        with patch.object(voice.subprocess, "Popen") as popen:
            for profile in discover_personalities().values():
                config = {key: value for key, value in profile["voice"].items()
                          if key != "reference_config"}
                self.assertIsNone(voice.load_reference_voice(config, self.root))
            popen.assert_not_called()

    def test_missing_settings_model_directory_and_checkpoint_never_start_a_worker(self):
        with patch.object(voice.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(voice.ReferenceVoiceUnavailable, "unavailable"):
                voice.load_reference_voice({"reference_config": "missing.json"}, self.root)
            self.settings["model_dir"] = ""
            with self.assertRaisesRegex(voice.ReferenceVoiceUnavailable, "not configured"):
                self.load()
            self.settings["model_dir"] = str(self.model_dir)
            (self.model_dir / "ve.safetensors").unlink()
            with self.assertRaisesRegex(voice.ReferenceVoiceUnavailable, "ve.safetensors"):
                self.load()
            popen.assert_not_called()

    def test_authorization_hash_and_absolute_reference_are_required(self):
        for key, value, error in (
            ("authorized", False, "authorization"),
            ("authorized", "true", "authorization"),
            ("reference_sha256", "0" * 64, "SHA-256 does not match"),
            ("reference_audio", "reference.wav", "absolute"),
        ):
            with self.subTest(key=key, value=value):
                original = self.settings[key]
                self.settings[key] = value
                with patch.object(voice.subprocess, "Popen") as popen:
                    with self.assertRaisesRegex(voice.ReferenceVoiceUnavailable, error):
                        self.load()
                    popen.assert_not_called()
                self.settings[key] = original

    def test_lazy_persistent_worker_pcm_protocol_and_offline_environment(self):
        pcm = b"\x00\x00\xff\x7f"
        rendered = {"ok": True, "sample_rate": 24000,
                    "pcm": base64.b64encode(pcm).decode("ascii")}
        process = FakeProcess([{"ok": True}, rendered, rendered])
        environment = dict(os.environ)
        with patch.object(voice.subprocess, "Popen", return_value=process) as popen:
            local = self.load()
            popen.assert_not_called()
            self.assertEqual(local.synthesize("   "), b"")
            popen.assert_not_called()
            self.assertEqual(local.synthesize("First sentence."), pcm)
            self.assertEqual(local.synthesize("Second sentence."), pcm)
            popen.assert_called_once()
            args, kwargs = popen.call_args
            self.assertEqual(args[0][1:4], ["-I", "-B", "-u"])
            self.assertEqual(kwargs["env"]["HF_HUB_OFFLINE"], "1")
            self.assertEqual(kwargs["env"]["TRANSFORMERS_OFFLINE"], "1")
            self.assertEqual(kwargs["env"]["HF_HUB_DISABLE_TELEMETRY"], "1")
            self.assertNotEqual(kwargs["stderr"], voice.subprocess.PIPE)
            self.assertEqual(dict(os.environ), environment)
            messages = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
            self.assertEqual(messages[0]["reference_audio"], str(self.recording))
            self.assertEqual(messages[1:], [{"text": "First sentence."}, {"text": "Second sentence."}])
            local.close()
            self.assertTrue(process.terminated)
            with self.assertRaisesRegex(voice.ReferenceVoiceUnavailable, "closed"):
                local.synthesize("Must not speak.")
        self.assertEqual(self.recording.read_bytes(), b"unchanged reference fixture")

    def test_timeout_stops_owned_worker(self):
        process = FakeProcess([])
        with patch.object(voice.subprocess, "Popen", return_value=process), \
                patch.object(voice.LocalReferenceVoice, "_read_responses"), \
                patch.object(voice, "_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(voice.ReferenceVoiceUnavailable, "timed out"):
                self.load().synthesize("Timeout.")
        self.assertTrue(process.terminated)

    def test_worker_error_and_invalid_pcm_are_reported_and_stop_worker(self):
        for response, error in (
            ({"ok": False, "error": "CUDA memory unavailable"}, "CUDA memory"),
            ({"ok": True, "sample_rate": 22050, "pcm": "AAAA"}, "sample rate"),
            ({"ok": True, "sample_rate": 24000, "pcm": "AA=="}, "PCM16"),
        ):
            with self.subTest(response=response):
                process = FakeProcess([{"ok": True}, response])
                with patch.object(voice.subprocess, "Popen", return_value=process):
                    with self.assertRaisesRegex(voice.ReferenceVoiceUnavailable, error):
                        self.load().synthesize("Failure.")
                self.assertTrue(process.terminated)

    def test_close_does_not_wait_for_synthesis_lock(self):
        local = self.load()
        held = threading.Event()
        release = threading.Event()

        def hold_synthesis_lock():
            with local._lock:
                held.set()
                release.wait(timeout=2)

        thread = threading.Thread(target=hold_synthesis_lock)
        thread.start()
        try:
            self.assertTrue(held.wait(timeout=1))
            local.close()
            self.assertFalse(release.is_set())
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(timeout=1)

    def test_worker_uses_local_model_and_prepares_reference_once(self):
        model = Mock(sr=24000)
        model_class = Mock()
        model_class.from_local.return_value = model
        fake_turbo = types.ModuleType("chatterbox.tts_turbo")
        fake_turbo.ChatterboxTurboTTS = model_class
        fake_torch = types.ModuleType("torch")
        fake_torch.inference_mode = nullcontext
        modules = {"chatterbox": types.ModuleType("chatterbox"),
                   "chatterbox.tts_turbo": fake_turbo, "torch": fake_torch}
        with patch.dict(sys.modules, modules), patch.dict(os.environ):
            self.assertIs(worker._load_model(self.settings), model)
            model_class.from_local.assert_called_once_with(str(self.model_dir), "cuda")
            model_class.from_pretrained.assert_not_called()
            model.prepare_conditionals.assert_called_once_with(str(self.recording))
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
        self.recording.write_bytes(b"modified after parent validation")
        with patch.dict(sys.modules, modules), patch.dict(os.environ):
            with self.assertRaisesRegex(ValueError, "changed before conditioning"):
                worker._load_model(self.settings)
        model_class.from_local.assert_called_once()

    def test_worker_calls_tts_and_encodes_clipped_little_endian_pcm(self):
        class Samples:
            ndim = 1

            def __init__(self, values):
                self.values = values
                self.size = len(values)

            def __mul__(self, scale):
                return Samples([value * scale for value in self.values])

            def round(self):
                return Samples([round(value) for value in self.values])

            def astype(self, dtype):
                self_test.assertEqual(dtype, "<i2")
                return self

            def tobytes(self):
                return struct.pack("<" + "h" * self.size, *self.values)

        self_test = self
        fake_numpy = types.ModuleType("numpy")
        fake_numpy.clip = lambda samples, lo, hi: Samples([max(lo, min(hi, n)) for n in samples.values])
        fake_numpy.isfinite = lambda samples: types.SimpleNamespace(all=lambda: all(map(math.isfinite, samples.values)))
        fake_torch = types.ModuleType("torch")
        fake_torch.inference_mode = nullcontext
        model = Mock(sr=24000)
        model.generate.return_value.detach.return_value.cpu.return_value.float.return_value.numpy.return_value.squeeze.return_value = Samples([-2, 0, .5, 2])
        with patch.dict(sys.modules, {"numpy": fake_numpy, "torch": fake_torch}):
            rendered = worker._synthesize(model, "Authorized voice text.")
            self.assertEqual(base64.b64decode(rendered["pcm"]), struct.pack("<hhhh", -32767, 0, 16384, 32767))
            self.assertEqual(rendered["sample_rate"], 24000)
            model.generate.assert_called_once_with("Authorized voice text.")
            model.generate.return_value.detach.return_value.cpu.return_value.float.return_value.numpy.return_value.squeeze.return_value = Samples([float("nan")])
            with self.assertRaisesRegex(ValueError, "invalid mono samples"):
                worker._synthesize(model, "Reject invalid output.")

    def test_worker_keeps_backend_output_out_of_json_protocol(self):
        model = object()

        def load(settings):
            print("Model log on stdout")
            return model

        def synthesize(actual_model, text):
            self.assertIs(actual_model, model)
            print("Synthesis log on stdout")
            return {"ok": True, "sample_rate": 24000, "pcm": "AAAAAA=="}

        incoming = io.StringIO(json.dumps(self.settings) + '\n{"text":"Hello"}\n')
        protocol, diagnostics = io.StringIO(), io.StringIO()
        with patch.object(worker, "_load_model", side_effect=load) as load_model, \
                patch.object(worker, "_synthesize", side_effect=synthesize), \
                patch.object(sys, "stdin", incoming), patch.object(sys, "stdout", protocol), \
                patch.object(sys, "stderr", diagnostics):
            self.assertEqual(worker.main(), 0)
        load_model.assert_called_once()
        responses = [json.loads(line) for line in protocol.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0], {"ok": True})
        self.assertIn("Model log on stdout", diagnostics.getvalue())
        self.assertIn("Synthesis log on stdout", diagnostics.getvalue())


if __name__ == "__main__":
    unittest.main()
