"""
Screen & webcam capture for JARVIS vision.

`capture_screen()` returns a validated local PNG and its physical display
geometry. `_capture_screen()` retains the compressed tuple used by older
callers. Capture does not require an analysis backend or upload/save images.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import numpy as np
from core.desktop_control import physical_coordinates
from core.task_control import check_task_cancelled

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE        = _base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        cfg = _load_config()
        cfg[key] = value
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Could not save config key '{key}': {e}")


def _get_os() -> str:
    return _load_config().get("os_system", "windows").lower()


_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q    = 82


def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"

    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}"


def capture_screen(monitor: int = 1) -> dict:
    """Capture one display (1..N), or all displays (0), in physical pixels."""
    if isinstance(monitor, bool) or not isinstance(monitor, int) or monitor < 0:
        raise ValueError("Monitor must be an integer: 0 for all displays or 1..N for one display.")
    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")
    if not _PIL:
        raise RuntimeError("Pillow is required to validate screenshots. Run: pip install pillow")

    check_task_cancelled()
    with physical_coordinates(), mss.mss() as sct:
        monitors = sct.monitors
        if len(monitors) < 2:
            raise RuntimeError("No connected physical display is available for capture.")
        if monitor >= len(monitors):
            raise ValueError(f"Monitor {monitor} is unavailable; choose 0..{len(monitors) - 1}.")
        target = {key: monitors[monitor][key] for key in ("left", "top", "width", "height")}
        if (any(isinstance(value, bool) or not isinstance(value, int) for value in target.values())
                or target["width"] <= 0 or target["height"] <= 0):
            raise RuntimeError("Display dimensions are invalid; screenshot was not captured.")
        size = (target["width"], target["height"])
        check_task_cancelled()
        shot = sct.grab(target)
        if tuple(shot.size) != size:
            raise RuntimeError("Screenshot dimensions do not match the selected physical display.")
        rgb = shot.rgb
        if not rgb or len(rgb) != size[0] * size[1] * 3:
            raise RuntimeError("Screenshot pixels are empty or incomplete.")
        png = mss.tools.to_png(rgb, size)

    if not isinstance(png, bytes) or not png:
        raise RuntimeError("Screenshot encoding returned an empty or invalid image.")
    try:
        with PIL.Image.open(io.BytesIO(png)) as image:
            if image.format != "PNG":
                raise RuntimeError("Screenshot encoding did not return a valid PNG image.")
            image.load()
            if image.size != size:
                raise RuntimeError("Decoded screenshot dimensions do not match the selected display.")
            if all(low == high for low, high in image.convert("RGB").getextrema()):
                raise RuntimeError("Screenshot is blank or uniform; show visible content on the selected display and retry.")
    except (OSError, ValueError, SyntaxError) as exc:
        raise RuntimeError("Screenshot image could not be decoded or validated.") from exc
    check_task_cancelled()
    return {"data": png, "mime_type": "image/png", "width": size[0], "height": size[1],
            "monitor": monitor, "source_rect": target}


def _capture_screen() -> tuple[bytes, str]:
    frame = capture_screen()
    return _compress(frame["data"], "PNG")


def _cv2_backend() -> int:
    """Return the best OpenCV camera backend for the current OS."""
    if not _CV2:
        return 0
    os_name = _get_os()
    if os_name == "windows":
        return cv2.CAP_DSHOW
    if os_name == "mac":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:

    if not _CV2:
        return False
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)


def _detect_camera_index() -> int:

    backend = _cv2_backend()
    print("[Vision] 🔍 Auto-detecting camera...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Camera found at index {idx}")
            _save_config_key("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Camera index {idx}: no usable frame")

    print("[Vision] ⚠️  No camera found — defaulting to index 0")
    _save_config_key("camera_index", 0)
    return 0


def _get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])
    return _detect_camera_index()


def _capture_camera() -> tuple[bytes, str]:
    if not _CV2:
        raise RuntimeError("OpenCV (cv2) is not installed. Run: pip install opencv-python")

    index   = _get_camera_index()
    backend = _cv2_backend()
    cap     = cv2.VideoCapture(index, backend)

    if not cap.isOpened():
        raise RuntimeError(f"Camera index {index} could not be opened.")

    for _ in range(10):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        raise RuntimeError("Camera returned no frame.")

    if _PIL:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        return buf.getvalue(), "image/jpeg"

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q])
    return buf.tobytes(), "image/jpeg"
