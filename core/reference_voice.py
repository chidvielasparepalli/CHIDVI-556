"""Optional local reference-conditioned TTS; never installs or downloads models."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


_TIMEOUT_SECONDS = 120
_MODEL_FILES = (
    "ve.safetensors", "t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors",
    "tokenizer_config.json", "vocab.json", "merges.txt", "added_tokens.json",
    "special_tokens_map.json",
)


class ReferenceVoiceUnavailable(RuntimeError):
    """An explicitly configured local voice cannot currently synthesize speech."""


def load_reference_voice(voice_config: dict, base_dir: Path) -> LocalReferenceVoice | None:
    """Validate local configuration without starting a model or contacting a service."""
    config_name = voice_config.get("reference_config")
    if not config_name:
        return None
    try:
        config_path = Path(base_dir) / config_name
        settings = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict) or settings.get("authorized") is not True:
            raise ValueError("reference recording authorization is required")
        reference = Path(settings.get("reference_audio", ""))
        if not reference.is_absolute() or not reference.is_file():
            raise ValueError("reference_audio must name an existing absolute file path")
        expected_hash = settings.get("reference_sha256", "")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError("reference_sha256 must contain the recording's SHA-256")
        with reference.open("rb") as recording:
            actual_hash = hashlib.file_digest(recording, "sha256").hexdigest()
        if actual_hash != expected_hash.lower():
            raise ValueError("reference recording SHA-256 does not match; source was changed")
        model_name = settings.get("model_dir")
        if not model_name:
            raise ValueError("local Chatterbox Turbo model_dir is not configured; no model downloaded")
        model_dir = (Path(base_dir) / model_name).resolve()
        missing = [name for name in _MODEL_FILES
                   if not (model_dir / name).is_file() or not (model_dir / name).stat().st_size]
        if missing:
            raise ValueError("local Chatterbox Turbo files are missing: " + ", ".join(missing))
        device = settings.get("device", "cuda")
        if device not in ("cuda", "cpu"):
            raise ValueError("device must be cuda or cpu")
        executable = Path(settings.get("python_executable") or sys.executable)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("python_executable must name an existing absolute Python path")
        return LocalReferenceVoice({
            "reference_audio": str(reference), "reference_sha256": actual_hash,
            "model_dir": str(model_dir), "device": device,
            "python_executable": str(executable),
        }, Path(base_dir))
    except (OSError, ValueError, TypeError) as exc:
        raise ReferenceVoiceUnavailable(f"Local reference voice unavailable: {exc}") from exc


class LocalReferenceVoice:
    """Serialize speech in an isolated offline worker, loaded on the first utterance."""

    def __init__(self, settings: dict, base_dir: Path):
        self._settings = settings
        self._base_dir = base_dir
        self._process = None
        self._responses = queue.Queue()
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def _read_responses(self, process) -> None:
        try:
            for line in process.stdout:
                self._responses.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._responses.put(None)

    def _start(self) -> None:
        environment = os.environ.copy()
        environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                           HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
        log_path = self._base_dir / "logs" / "reference_voice.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                [self._settings["python_executable"], "-I", "-B", "-u",
                 str(Path(__file__).with_name("reference_voice_worker.py"))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                text=True, encoding="utf-8", env=environment, cwd=str(self._base_dir),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        self._process = process
        threading.Thread(target=self._read_responses, args=(process,), daemon=True).start()
        if self._closed.is_set():
            self.close()
            raise ReferenceVoiceUnavailable("Local reference voice was closed")
        self._request(self._settings)

    def _request(self, payload: dict) -> dict:
        self._process.stdin.write(json.dumps(payload) + "\n")
        self._process.stdin.flush()
        try:
            line = self._responses.get(timeout=_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            raise ReferenceVoiceUnavailable("Local reference voice timed out") from exc
        if line is None:
            raise ReferenceVoiceUnavailable("Local reference voice worker stopped; see logs/reference_voice.log")
        response = json.loads(line)
        if not isinstance(response, dict) or response.get("ok") is not True:
            detail = response.get("error", "invalid worker response") if isinstance(response, dict) else "invalid worker response"
            raise ReferenceVoiceUnavailable(f"Local reference voice failed: {detail}")
        return response

    def synthesize(self, text: str) -> bytes:
        """Return mono signed little-endian PCM16 at 24 kHz without playing it."""
        with self._lock:
            if self._closed.is_set():
                raise ReferenceVoiceUnavailable("Local reference voice was closed")
            if not text.strip():
                return b""
            try:
                if self._process is None:
                    self._start()
                response = self._request({"text": text})
                if self._closed.is_set():
                    raise ReferenceVoiceUnavailable("Local reference voice was closed")
                if response.get("sample_rate") != 24000:
                    raise ValueError("worker returned an unsupported sample rate")
                pcm = base64.b64decode(response["pcm"], validate=True)
                if not pcm or len(pcm) % 2:
                    raise ValueError("worker returned invalid PCM16 audio")
                return pcm
            except (OSError, ValueError, KeyError, TypeError, ReferenceVoiceUnavailable) as exc:
                self.close()
                if isinstance(exc, ReferenceVoiceUnavailable):
                    raise
                raise ReferenceVoiceUnavailable(f"Local reference voice failed: {exc}") from exc

    def close(self) -> None:
        """Stop our child even when another thread is waiting for its synthesis."""
        self._closed.set()
        self._responses.put(None)
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
