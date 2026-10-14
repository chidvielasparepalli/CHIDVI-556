"""Explicit session screen sharing: one local frame, revocable upload leases.

Capture is local and never uploads by itself. A transmission context is the
serialized dispatch boundary: Stop revokes future dispatches immediately while
already dispatched requests drain. Network calls never hold the state lock.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import hmac
import io
import math
import secrets
import threading
import time
import uuid

from PIL import Image

from core.task_control import _origin


STATES = {"OFF", "STARTING", "LIVE", "PAUSED", "STOPPING", "ERROR"}


class ScreenSharingError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class _Sample:
    data: bytes
    mime_type: str
    width: int
    height: int
    monitor: int
    rect: tuple
    window_id: int
    frame_id: str
    captured_at: float
    captured_mono: float
    sha256: str
    content_hash: str


@dataclass(frozen=True)
class _Lease:
    session_id: str
    generation: int
    nonce: str
    frame_id: str
    captured_at: float
    captured_mono: float
    mime_type: str
    width: int
    height: int
    monitor: int
    rect: tuple
    window_id: int
    sha256: str
    content_hash: str
    origin: object
    signature: str = ""

    @property
    def task_id(self):
        return self.origin[1] if self.origin else ""


class LiveScreenSharing:
    def __init__(self, capture=None, observer=None, cloud_allowed=None, emit=None, clock=time.monotonic):
        from memory.config_manager import validate_live_sharing_settings
        if capture is None:
            from actions.screen_processor import capture_screen
            capture = capture_screen
        if observer is None:
            from core.desktop_control import foreground
            observer = foreground
        if cloud_allowed is None:
            def cloud_allowed():
                from memory.config_manager import get_cloud_vision_enabled
                from core.desktop_control import get_service
                return get_cloud_vision_enabled() and get_service().cloud_vision_enabled
        self._capture, self._observer, self._cloud_allowed = capture, observer, cloud_allowed
        self._emit, self._clock = emit or (lambda _: None), clock
        self._lock, self._capture_lock = threading.RLock(), threading.Lock()
        self._settings = validate_live_sharing_settings({})
        self._state, self._error, self._session_id = "OFF", "", ""
        self._generation, self._captures, self._active = 0, 0, 0
        self._latest, self._worker = None, None
        self._stopped = threading.Event()
        self._secret = secrets.token_bytes(32)
        self._started, self._deadline = 0.0, 0.0
        self._captured, self._sent, self._analyses = 0, 0, 0
        self._stream_times, self._analysis_times = deque(), deque()
        # Bounded by the configured rate and at most 120 minutes per session.
        # Only dispatch metadata is kept; no image history or descriptions.
        self._dispatches = {}
        self._last_stream_hash, self._last_stream_at = None, None
        self._last_sent_at, self._last_sent_frame_id, self._sent_clock = None, None, None
        self._last_stream_frame_at = None

    def _snapshot_locked(self):
        return {"state": self._state, "session_id": self._session_id,
                "generation": self._generation,
                "last_frame_at": self._latest.captured_at if self._latest else None,
                "last_sent_at": self._last_sent_at, "last_sent_frame_id": self._last_sent_frame_id,
                "last_stream_frame_at": self._last_stream_frame_at,
                "sent_clock": self._sent_clock,
                "frames_captured": self._captured, "frames_sent": self._sent,
                "analyses": self._analyses, "active_requests": self._active,
                "error": self._error, "settings": dict(self._settings)}

    def snapshot(self):
        with self._lock:
            self._environment_locked()
            return self._snapshot_locked()

    def bind(self, emit=None, cloud_allowed=None):
        """Bind runtime/UI callbacks without changing the active session."""
        if emit is not None and not callable(emit) or cloud_allowed is not None and not callable(cloud_allowed):
            raise TypeError("Screen sharing callbacks must be callable")
        with self._lock:
            if emit is not None:
                self._emit = emit
            if cloud_allowed is not None:
                self._cloud_allowed = cloud_allowed
            self._environment_locked()
            self._publish_locked()
            return self._snapshot_locked()

    def _publish_locked(self):
        try:
            self._emit(self._snapshot_locked())
        except Exception:
            pass  # UI shutdown must not strand capture or upload cleanup.

    def _drained_locked(self):
        if self._state == "STOPPING" and not self._captures and not self._active:
            self._state = "OFF"
            self._publish_locked()

    def _invalidate_locked(self, state, message=""):
        self._generation += 1
        self._stopped.set()
        self._latest = None
        self._last_stream_hash = self._last_stream_at = None
        self._last_sent_at = self._last_sent_frame_id = self._sent_clock = None
        self._last_stream_frame_at = None
        self._state, self._error = state, message
        self._publish_locked()
        self._drained_locked()

    def _environment_locked(self):
        if self._state not in {"STARTING", "LIVE", "PAUSED"}:
            return
        try:
            allowed = self._cloud_allowed() is True
        except Exception:
            allowed = False
        if not allowed:
            self._invalidate_locked("ERROR", "Cloud Vision is disabled; start sharing again after enabling it.")
        elif self._clock() < self._started:
            self._invalidate_locked("ERROR", "Screen sharing clock changed; start a new session.")
        elif self._clock() >= self._deadline:
            self._invalidate_locked("STOPPING", "Screen sharing session expired; start a new session.")

    def _require_locked(self, starting=False):
        self._environment_locked()
        if self._state not in ({"LIVE", "STARTING"} if starting else {"LIVE"}):
            raise ScreenSharingError("sharing_inactive", "Live screen sharing is not active.")

    def _spawn_locked(self):
        self._stopped = threading.Event()
        self._worker = threading.Thread(target=self._run, args=(self._generation, self._stopped),
                                        name="chidvi-screen-sharing", daemon=True)
        self._worker.start()

    def start(self, settings=None):
        from memory.config_manager import validate_live_sharing_settings
        options = validate_live_sharing_settings({} if settings is None else settings)
        with self._lock:
            self._environment_locked()
            if self._state in {"LIVE", "STARTING"} and (settings is None or options == self._settings):
                return self._snapshot_locked()
            if self._captures or self._active:
                raise ScreenSharingError("sharing_busy", "Wait for the previous screen operation to finish before starting again.")
            self._invalidate_locked("OFF")
            self._settings, self._session_id = options, uuid.uuid4().hex
            self._secret = secrets.token_bytes(32)
            self._started = self._clock()
            self._deadline = self._started + options["session_minutes"] * 60
            self._captured = self._sent = self._analyses = 0
            self._dispatches.clear()
            self._stream_times.clear()
            self._analysis_times.clear()
            self._last_stream_hash = self._last_stream_at = None
            self._state, self._error = "STARTING", ""
            self._environment_locked()
            self._publish_locked()
            if self._state == "STARTING":
                self._spawn_locked()
            return self._snapshot_locked()

    def pause(self, reason=""):
        with self._lock:
            if self._state in {"LIVE", "STARTING", "PAUSED"}:
                self._invalidate_locked("PAUSED", "Screen sharing paused.")
            return self._snapshot_locked()

    def resume(self):
        with self._lock:
            self._environment_locked()
            if self._state != "PAUSED":
                return self._snapshot_locked()
            if self._captures or self._active:
                raise ScreenSharingError("sharing_busy", "Wait for the paused screen operation to finish before resuming.")
            self._generation += 1
            self._state, self._error = "STARTING", ""
            self._publish_locked()
            self._spawn_locked()
            return self._snapshot_locked()

    def stop(self, reason=""):
        with self._lock:
            self._invalidate_locked("STOPPING")
            return self._snapshot_locked()

    def close(self):
        return self.stop()

    def fail(self, reason):
        from core.repair_permissions import redact
        # Preserve caller-provided status/type details, with the same redaction
        # used by diagnostics. Never stringify arbitrary payload objects.
        message = (redact(reason)[:300] if isinstance(reason, str) and len(reason) <= 2000 else "")
        message = " ".join(message.split()) or "Live screen sharing failed; start a new session when ready."
        with self._lock:
            self._invalidate_locked("ERROR", message)
            return self._snapshot_locked()

    @staticmethod
    def _window(raw):
        from core.observation import _SENSITIVE, _SECRET
        if not isinstance(raw, dict):
            raise ScreenSharingError("sharing_privacy", "Foreground identity is unavailable.")
        title = str(raw.get("title", ""))
        app = str(raw.get("application", "")).replace("\\", "/").rsplit("/", 1)[-1].lower()
        if (raw.get("locked") or raw.get("sensitive") or _SENSITIVE.search(title) or _SECRET.search(title)
                or app in {"lockapp.exe", "logonui.exe", "credentialuibroker.exe"}):
            raise ScreenSharingError("sharing_privacy", "Screen sharing paused for a locked or sensitive foreground.")
        window = raw.get("window_id")
        if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
            raise ScreenSharingError("sharing_privacy", "Foreground identity is unavailable.")
        return window

    @staticmethod
    def _origin_valid(origin):
        return origin is None or (origin == _origin.get() and origin[0].can_confirm(origin[1]))

    def _sign(self, lease):
        identity = tuple(getattr(lease, key) for key in (
            "session_id", "generation", "nonce", "frame_id", "captured_at", "captured_mono", "mime_type",
            "width", "height", "monitor", "rect", "window_id", "sha256", "content_hash"))
        identity += (id(lease.origin[0]) if lease.origin else None, lease.task_id)
        return hmac.new(self._secret, repr(identity).encode("utf-8"), hashlib.sha256).hexdigest()

    def _frame_locked(self, sample, origin):
        lease = _Lease(self._session_id, self._generation, uuid.uuid4().hex,
                       sample.frame_id, sample.captured_at, sample.captured_mono, sample.mime_type,
                       sample.width, sample.height, sample.monitor, sample.rect, sample.window_id,
                       sample.sha256, sample.content_hash, origin)
        lease = replace(lease, signature=self._sign(lease))
        return {"data": sample.data, "mime_type": sample.mime_type, "width": sample.width,
                "height": sample.height, "monitor": sample.monitor,
                "source_rect": dict(zip(("left", "top", "width", "height"), sample.rect)),
                "timestamp": sample.captured_at, "captured_at": sample.captured_at,
                "frame_id": sample.frame_id, "_sharing": lease}

    def frame(self, force=False, max_age=2, monitor=None):
        return self._frame(force=force, max_age=max_age, monitor=monitor)

    def _frame(self, force=False, max_age=2, monitor=None, expected_generation=None):
        if type(force) is not bool or isinstance(max_age, bool) or not isinstance(max_age, (int, float)) or not math.isfinite(max_age) or not 0 <= max_age <= 10:
            raise ValueError("Frame freshness must be 0–10 seconds and force must be a boolean.")
        origin = _origin.get()
        if not self._origin_valid(origin):
            raise ScreenSharingError("sharing_cancelled", "The requesting task is no longer active.")
        with self._lock:
            self._require_locked(starting=True)
            if expected_generation is not None and expected_generation != self._generation:
                raise ScreenSharingError("sharing_cancelled", "The capture worker belongs to an earlier sharing session.")
            if monitor is not None and (type(monitor) is not int or monitor != self._settings["monitor"]):
                raise ScreenSharingError("sharing_monitor_mismatch", "The requested display is outside the current sharing selection.")
            generation = self._generation
        # A separate capture lock serializes samples without blocking Stop.
        if not self._capture_lock.acquire(timeout=1.0):
            raise ScreenSharingError("sharing_busy", "A screen capture is already in progress.")
        counted = False
        try:
            with self._lock:
                self._require_locked(starting=True)
                if generation != self._generation:
                    raise ScreenSharingError("sharing_cancelled", "Screen sharing changed before capture.")
                self._captures += 1
                counted = True
                selected = self._settings["monitor"]
            before = self._window(self._observer())
            with self._lock:
                self._require_locked(starting=True)
                if generation != self._generation or not self._origin_valid(origin):
                    raise ScreenSharingError("sharing_cancelled", "Screen sharing changed before capture.")
                latest = self._latest
                if (not force and latest and before == latest.window_id
                        and 0 <= self._clock() - latest.captured_mono <= max_age):
                    return self._frame_locked(latest, origin)
            raw = self._capture(monitor=selected)
            from core.vision import _image_metadata
            metadata = _image_metadata(raw)
            if metadata.get("monitor") != selected or "source_rect" not in metadata:
                raise ValueError("Capture does not match the selected display")
            rect = tuple(metadata["source_rect"][key] for key in ("left", "top", "width", "height"))
            if (metadata["width"], metadata["height"]) != rect[2:]:
                raise ValueError("Local capture must preserve physical pixels")
            after = self._window(self._observer())
            if before != after:
                raise ScreenSharingError("sharing_privacy", "Foreground changed during capture; screen sharing paused.")
            data = raw["data"]
            fingerprint = hashlib.sha256(data).hexdigest()
            with self._lock:
                self._require_locked(starting=True)
                if generation != self._generation or not self._origin_valid(origin):
                    raise ScreenSharingError("sharing_cancelled", "Screen sharing changed; late capture discarded.")
                self._latest = _Sample(data, metadata["mime_type"], metadata["width"], metadata["height"],
                    selected, rect, before, uuid.uuid4().hex, time.time(), self._clock(), fingerprint, fingerprint)
                self._captured += 1
                self._state, self._error = "LIVE", ""
                self._publish_locked()
                return self._frame_locked(self._latest, origin)
        except ScreenSharingError as error:
            if error.code == "sharing_privacy":
                with self._lock:
                    if generation == self._generation:
                        self._invalidate_locked("PAUSED", error.message)
            raise
        except Exception as error:
            with self._lock:
                if generation == self._generation:
                    self._invalidate_locked("ERROR", f"Screen capture failed ({type(error).__name__}); start a new session.")
            raise ScreenSharingError("sharing_capture_failed", "Screen capture could not be validated; no image was sent.") from None
        finally:
            with self._lock:
                if counted:
                    self._captures -= 1
                self._drained_locked()
            self._capture_lock.release()

    def _valid_locked(self, frame):
        self._environment_locked()
        if self._state != "LIVE" or not isinstance(frame, dict):
            return False
        lease = frame.get("_sharing")
        if not isinstance(lease, _Lease):
            return False
        try:
            if (lease.session_id != self._session_id or lease.generation != self._generation
                    or not self._origin_valid(lease.origin) or not hmac.compare_digest(lease.signature, self._sign(lease))):
                return False
            if (not isinstance(frame.get("data"), bytes) or hashlib.sha256(frame["data"]).hexdigest() != lease.sha256
                    or any(frame.get(key) != getattr(lease, key) for key in (
                        "frame_id", "captured_at", "mime_type", "width", "height", "monitor"))
                    or frame.get("timestamp") != lease.captured_at
                    or tuple(frame["source_rect"][key] for key in ("left", "top", "width", "height")) != lease.rect):
                return False
            dispatch = self._dispatches.get(lease.nonce)
            start = dispatch["started"] if dispatch else lease.captured_mono
            return 0 <= self._clock() - start <= (45 if dispatch else 10)
        except (KeyError, TypeError, AttributeError, ValueError):
            return False

    def valid(self, frame):
        with self._lock:
            return self._valid_locked(frame)

    def _rate_locked(self, kind):
        now = self._clock()
        for times in (self._stream_times, self._analysis_times):
            while times and now - times[0] >= 60:
                times.popleft()
        if kind == "stream":
            return (len(self._stream_times) < self._settings["max_per_minute"]
                    and (not self._stream_times or now - self._stream_times[-1] >= 60 / self._settings["max_per_minute"]))
        # User questions/control have a separate rolling quota; unlike video,
        # consecutive explicit requests do not need artificial spacing.
        return len(self._analysis_times) < self._settings["analysis_per_minute"]

    @contextmanager
    def transmission(self, frame, kind="analysis"):
        if kind not in {"analysis", "stream", "control"}:
            raise ValueError("Unsupported screen transmission kind")
        with self._lock:
            self._require_locked()
            if not self._valid_locked(frame):
                raise ScreenSharingError("sharing_invalid_frame", "This screen frame is stale, changed or no longer authorized.")
            lease = frame["_sharing"]
            if lease.nonce in self._dispatches:
                raise ScreenSharingError("sharing_consumed", "This screen lease was already dispatched; do not retry it.")
            if not 0 <= self._clock() - lease.captured_mono <= 10:
                raise ScreenSharingError("sharing_stale", "The screen frame is too old to upload.")
            if self._active:
                raise ScreenSharingError("sharing_busy", "Another screen request is still in progress.")
            if not self._rate_locked(kind):
                raise ScreenSharingError("sharing_rate_limited", "The screen sharing request limit has been reached; wait before requesting another frame.")
            started = self._clock()
            permit = {"started": started, "kind": kind, "sent": False, "analyzed": False}
            self._dispatches[lease.nonce] = permit
            if kind == "stream":
                self._stream_times.append(started)
            else:
                self._analysis_times.append(started)
            self._active += 1
            self._publish_locked()
        try:
            yield lease
        finally:
            with self._lock:
                self._active -= 1
                self._drained_locked()
                self._publish_locked()

    def mark_sent(self, frame):
        with self._lock:
            if not self._valid_locked(frame):
                return False
            lease = frame["_sharing"]
            permit = self._dispatches.get(lease.nonce)
            if permit is None or permit["sent"]:
                return False
            permit["sent"] = True
            self._sent += 1
            self._last_sent_at, self._last_sent_frame_id = lease.captured_at, lease.frame_id
            self._sent_clock = self._clock()
            if permit["kind"] == "stream":
                self._last_stream_hash, self._last_stream_at = lease.content_hash, self._clock()
                self._last_stream_frame_at = lease.captured_at
            self._publish_locked()
            return True

    def has_streamed(self, frame):
        """Whether this exact pixel content reached Live in the current session."""
        with self._lock:
            return (self._valid_locked(frame)
                    and self._last_stream_hash == frame["_sharing"].content_hash)

    def mark_analyzed(self, frame, description):
        if not isinstance(description, str) or not description.strip():
            return False
        with self._lock:
            if not self._valid_locked(frame):
                return False
            permit = self._dispatches.get(frame["_sharing"].nonce)
            if permit is None or permit["kind"] == "stream" or permit["analyzed"]:
                return False
            permit["analyzed"] = True
            self._analyses += 1
            self.mark_sent(frame)
            self._publish_locked()
            return True

    def stream_frame(self):
        with self._lock:
            self._environment_locked()
            if self._state != "LIVE" or self._active or not self._rate_locked("stream"):
                return None
            generation = self._generation
        try:
            # Polling consumers must not increase the configured sampling rate.
            # A stale cache waits for the sampler instead of capturing here.
            window = self._window(self._observer())
            with self._lock:
                self._environment_locked()
                latest = self._latest
                if (self._state != "LIVE" or generation != self._generation or latest is None
                        or latest.window_id != window or not 0 <= self._clock() - latest.captured_mono <= 10):
                    return None
                frame = self._frame_locked(latest, _origin.get())
                if not self._valid_locked(frame):
                    return None
                lease = frame["_sharing"]
                if (self._settings["change_only"] and lease.content_hash == self._last_stream_hash
                        and self._last_stream_at is not None and self._clock() - self._last_stream_at < 30):
                    return None
                limits = (self._settings["max_width"], self._settings["max_height"])
            if frame["width"] <= limits[0] and frame["height"] <= limits[1]:
                return frame
            with Image.open(io.BytesIO(frame["data"])) as image:
                image = image.convert("RGB")
                image.thumbnail(limits, Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, "PNG")
                data, width, height = buffer.getvalue(), image.width, image.height
            with self._lock:
                if not self._valid_locked(frame):
                    return None
                sample = _Sample(data, "image/png", width, height, lease.monitor, lease.rect, lease.window_id,
                    lease.frame_id, lease.captured_at, lease.captured_mono, hashlib.sha256(data).hexdigest(), lease.content_hash)
                return self._frame_locked(sample, lease.origin)
        except ScreenSharingError as error:
            if error.code == "sharing_privacy":
                with self._lock:
                    if generation == self._generation:
                        self._invalidate_locked("PAUSED", error.message)
            return None
        except Exception:
            self.fail("Frame preparation failed")
            return None

    def _run(self, generation, stopped):
        token = _origin.set(None)  # The sharing session outlives individual tasks.
        try:
            while not stopped.is_set():
                with self._lock:
                    self._environment_locked()
                    if generation != self._generation or self._state not in {"STARTING", "LIVE"}:
                        return
                    interval = self._settings["interval"]
                try:
                    self._frame(force=True, expected_generation=generation)
                except ScreenSharingError:
                    with self._lock:
                        if generation != self._generation or self._state not in {"STARTING", "LIVE"}:
                            return
                if stopped.wait(interval):
                    return
        finally:
            _origin.reset(token)


_singleton = None
_singleton_lock = threading.Lock()


def get_screen_sharing():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = LiveScreenSharing()
        return _singleton
