"""Private JSON-line worker for installed Chatterbox Turbo and local checkpoints."""
from contextlib import redirect_stdout
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback


def _load_model(settings):
    # Set before imports: Hugging Face caches these values at import time.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    reference = Path(settings["reference_audio"])
    with reference.open("rb") as recording:
        if hashlib.file_digest(recording, "sha256").hexdigest() != settings["reference_sha256"]:
            raise ValueError("reference recording changed before conditioning")
    from chatterbox.tts_turbo import ChatterboxTurboTTS
    import torch

    model = ChatterboxTurboTTS.from_local(settings["model_dir"], settings["device"])
    if model.sr != 24000:
        raise ValueError("Chatterbox Turbo must produce 24 kHz audio")
    with torch.inference_mode():
        model.prepare_conditionals(str(reference))
    return model


def _synthesize(model, text):
    import numpy as np
    import torch

    with torch.inference_mode():
        samples = model.generate(text).detach().cpu().float().numpy().squeeze()
    if samples.ndim != 1 or not samples.size or not np.isfinite(samples).all():
        raise ValueError("Chatterbox returned invalid mono samples")
    pcm = (np.clip(samples, -1, 1) * 32767).round().astype("<i2").tobytes()
    return {"ok": True, "sample_rate": model.sr, "pcm": base64.b64encode(pcm).decode("ascii")}


def main():
    protocol = sys.stdout

    def send(payload):
        protocol.write(json.dumps(payload) + "\n")
        protocol.flush()

    try:
        settings = json.loads(sys.stdin.readline())
        with redirect_stdout(sys.stderr):
            model = _load_model(settings)
        send({"ok": True})
        for line in sys.stdin:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                response = _synthesize(model, request["text"])
            send(response)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
