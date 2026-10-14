"""Desktop input in physical screen pixels; feedback and cancellation fail closed."""
from __future__ import annotations

import ctypes
import hashlib
import math
import platform
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Callable

CLICK_PHRASE = "I am clicking here."
IMAGE_PREVIEW_TIMEOUT = 5.0


class DesktopError(RuntimeError):
    pass


class ImageReviewError(DesktopError):
    """Safe, structured failure: never include image bytes or callback exceptions."""
    def __init__(self, code, reason, request_id=""):
        super().__init__(reason)
        self.code, self.request_id = code, request_id


_IMAGE_ERRORS = {
    "pending": ("image_review_pending", "This image is already waiting for desktop approval."),
    "rejected": ("image_review_rejected", "This image was not approved; no image was sent."),
    "cancelled": ("image_review_cancelled", "Image review was cancelled; no further image action is allowed."),
    "expired": ("image_review_expired", "Image approval expired; request and review a new image."),
    "preview_failed": ("image_review_failed", "The exact-image preview failed; no image was sent."),
}


@dataclass(frozen=True)
class _ImageIdentity:
    id: str
    data: bytes
    mime: str
    sha256: str
    task_id: str
    task_text: str
    question: str
    provider: str
    deadline: float


class ImageApproval:
    """One immutable image/operation with an atomic, explicitly visible UI decision."""
    def __init__(self, data, mime="image/png", *, task_id="", task_text="", question="",
                 provider="configured cloud provider", deadline=None, valid=None,
                 changed=None, clock=None, origin=None):
        if not isinstance(data, bytes) or not data or mime not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("Invalid immutable image bytes or MIME type")
        self._clock = clock or time.monotonic
        request_id = uuid.uuid4().hex
        self._identity = _ImageIdentity(request_id, data, mime, hashlib.sha256(data).hexdigest(),
                                       task_id or request_id, task_text or question or "Image analysis",
                                       question, provider, deadline if deadline is not None else self._clock() + 180)
        self.done, self.visible = threading.Event(), threading.Event()
        self.changed = changed or (lambda request: None)
        self._lock = threading.RLock()
        self._valid, self._origin = valid, origin
        self._status, self._error, self._error_code = "pending", "", ""

    id = property(lambda self: self._identity.id)
    data = property(lambda self: self._identity.data)
    mime = property(lambda self: self._identity.mime)
    sha256 = property(lambda self: self._identity.sha256)
    task_id = property(lambda self: self._identity.task_id)
    task_text = property(lambda self: self._identity.task_text)
    question = property(lambda self: self._identity.question)
    provider = property(lambda self: self._identity.provider)
    deadline = property(lambda self: self._identity.deadline)
    status = property(lambda self: self._status)
    error = property(lambda self: self._error)

    def snapshot(self):
        with self._lock:
            return {key: getattr(self, key) for key in (
                "id", "mime", "sha256", "task_id", "task_text", "question", "provider", "deadline",
                "status", "error")} | {"request_id": self.id, "visible": self.visible.is_set()}

    def _notify(self):
        try:
            self.changed(self)
        except Exception:
            pass  # Display teardown must not bypass a decision or strand the waiter.

    def _invalid(self):
        if self._clock() >= self.deadline:
            return "expired", _IMAGE_ERRORS["expired"][1]
        try:
            if self._valid is not None and self._valid() is not True:
                return "cancelled", "The image's originating task or observation is no longer active."
        except ImageReviewError as error:
            return "cancelled", str(error)
        except Exception as error:
            return "cancelled", f"Image validity check failed ({type(error).__name__}); no image was sent."
        return None

    def _transition(self, status, error="", *, allowed=("pending",), code=""):
        with self._lock:
            if self._status not in allowed:
                return False
            self._status, self._error, self._error_code = status, error, code
            if status != "pending":
                self.done.set()
        self._notify()
        return True

    def resolve(self, status, error=""):
        if status not in {"approved", "rejected", "cancelled", "expired", "preview_failed"}:
            raise ValueError("Unknown image approval decision")
        requested_status = status
        with self._lock:
            if self._status != "pending":
                return False
            if status == "approved":
                invalid = self._invalid()
                if invalid:
                    status, error = invalid
                elif not self.visible.is_set():
                    status, error = "preview_failed", "Image approval refused: the exact-image preview was never shown."
            error = str(error)[:240] if error else _IMAGE_ERRORS.get(status, ("", ""))[1]
            self._status, self._error = status, error
            self.done.set()
        self._notify()
        return status == requested_status

    def mark_visible(self):
        with self._lock:
            if self._status != "pending":
                return False
            invalid = self._invalid()
            if not invalid:
                self.visible.set()
                return True
        self.resolve(*invalid)
        return False

    def _validate(self):
        with self._lock:
            invalid = self._invalid()
        if invalid:
            self._transition(*invalid, allowed=("pending", "approved"))
            self._raise()

    def _raise(self):
        code, reason = _IMAGE_ERRORS.get(self.status, ("image_review_failed", "This image approval cannot be used again."))
        raise ImageReviewError(self._error_code or code, self.error or reason, self.id)


def number(value, name: str, minimum=None, maximum=None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DesktopError(f"{name} must be a finite number.")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise DesktopError(f"{name} is outside the supported range.")
    return value


@dataclass(frozen=True)
class ScreenRect:
    left: int
    top: int
    width: int
    height: int

    def contains(self, x, y):
        return self.left <= x < self.left + self.width and self.top <= y < self.top + self.height

    def as_dict(self):
        return dict(left=self.left, top=self.top, width=self.width, height=self.height)


def map_image_point(x, y, image_width, image_height, screen: ScreenRect):
    """Map a measured image region to its original physical desktop rectangle."""
    number(image_width, "image_width", 1)
    number(image_height, "image_height", 1)
    number(screen.width, "screen width", 1)
    number(screen.height, "screen height", 1)
    number(x, "x", 0, image_width - 1)
    number(y, "y", 0, image_height - 1)
    return (screen.left + int(x * screen.width / image_width),
            screen.top + int(y * screen.height / image_height))


def physical_to_logical(x, y, physical: ScreenRect, logical: ScreenRect):
    """Qt mapping uses each monitor's measured physical and logical origins."""
    if not physical.contains(x, y):
        raise DesktopError("Point is outside the selected monitor.")
    return (logical.left + (x - physical.left) * logical.width / physical.width,
            logical.top + (y - physical.top) * logical.height / physical.height)


@contextmanager
def physical_coordinates():
    previous = None
    if platform.system() == "Windows":
        try:
            fn = ctypes.windll.user32.SetThreadDpiAwarenessContext
            fn.restype = ctypes.c_void_p
            fn.argtypes = [ctypes.c_void_p]
            previous = fn(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass
    try:
        yield
    finally:
        if previous:
            fn(previous)


def monitor_rects():
    import mss
    with physical_coordinates(), mss.mss() as capture:
        return [ScreenRect(m["left"], m["top"], m["width"], m["height"])
                for m in capture.monitors[1:]]


def foreground():
    """Metadata only; values of controls and screenshots are never collected here."""
    if platform.system() != "Windows":
        return {"window_id": None, "title": "", "application": "", "locked": False}
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    desktop = user32.OpenInputDesktop(0, False, 0x0100)
    if not desktop:
        return {"window_id": None, "title": "", "application": "", "locked": True}
    user32.CloseDesktop(desktop)
    hwnd = user32.GetForegroundWindow()
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title, len(title))
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app = ""
    try:
        import psutil
        app = psutil.Process(pid.value).name()
    except Exception:
        pass
    sensitive = any(term in title.value.lower() for term in
                    ("password", "sign in", "sign-in", "log in", "login", "verification", "authentication", "banking", "checkout"))
    return {"window_id": int(hwnd or 0), "title": "[sensitive window]" if sensitive else title.value,
            "application": app, "locked": False, "sensitive": sensitive}


@dataclass(frozen=True)
class VisualTarget:
    x: int
    y: int
    confidence: float
    observed_at: float
    fingerprint: str
    window_id: int | None
    sharing_frame: dict | None = field(default=None, repr=False, compare=False)


class DesktopControl:
    def __init__(self, backend=None, monitors=monitor_rects, observer=foreground, clock=time.monotonic):
        self._backend = backend
        self._monitors, self._observer, self._clock = monitors, observer, clock
        self._highlight = self._speak = None
        self._image_review = None
        self._image_lock = threading.RLock()
        self._pending_image = None
        self._cloud_vision_enabled = False
        self._image_permission_generation = 0
        self._cancelled = lambda: False
        self._lock = threading.RLock()
        self._pressed = set()
        self.cloud_vision_enabled = False

    @property
    def cloud_vision_enabled(self):
        return self._cloud_vision_enabled

    @cloud_vision_enabled.setter
    def cloud_vision_enabled(self, enabled):
        enabled = bool(enabled)
        with self._image_lock:
            if enabled != self._cloud_vision_enabled:
                self._image_permission_generation += 1
            self._cloud_vision_enabled = enabled
            pending = self._pending_image
        if not enabled and pending is not None:
            pending._transition("cancelled", "Cloud Vision was disabled; this image approval was revoked.",
                                allowed=("pending", "approved"))

    def bind_feedback(self, highlight, speak, cancelled=None):
        self._highlight, self._speak = highlight, speak
        self._cancelled = cancelled or (lambda: False)

    def bind_image_review(self, callback):
        """Bind a trusted callback(request) that queues the preview and returns."""
        self._image_review = callback

    def review_image(self, data, mime="image/png", *, question="", provider="configured cloud provider",
                     valid=None, timeout=180):
        from core.task_control import _origin
        number(timeout, "image approval timeout", .01, 300)
        if not self.cloud_vision_enabled:
            raise ImageReviewError("image_review_cancelled", "Cloud Vision was disabled before upload; image discarded.")
        if self._image_review is None:
            raise ImageReviewError("image_review_unavailable", "Desktop image review is unavailable; no image was sent.")
        if (not isinstance(data, bytes) or not data or len(data) > 64 * 1024 * 1024
                or mime not in {"image/png", "image/jpeg", "image/webp"}
                or not isinstance(question, str) or len(question) > 8000
                or not isinstance(provider, str) or len(provider) > 200):
            raise ImageReviewError("image_review_failed", "Invalid image bytes, format or review operation.")
        origin = _origin.get()
        generation = self._image_permission_generation
        def guard():
            try:
                self.check_cancelled()
            except DesktopError:
                raise ImageReviewError("image_review_cancelled", "Desktop operation cancelled during image review.") from None
            if not self.cloud_vision_enabled or generation != self._image_permission_generation:
                raise ImageReviewError("image_review_cancelled", "Cloud Vision was disabled during review; approval was revoked.")
            if origin is not None and not origin[0].can_confirm(origin[1]):
                return False
            return valid() is True if valid is not None else True
        task = origin[0].get(origin[1]) if origin else {}
        request = ImageApproval(data, mime, task_id=task.get("id", ""), task_text=task.get("text", ""),
                                question=question, provider=provider, deadline=time.monotonic() + timeout,
                                valid=guard, origin=origin)
        request._service = self
        request._validate()
        with self._image_lock:
            previous = self._pending_image
            if previous and previous.status in {"pending", "approved"}:
                try:
                    previous._validate()
                except ImageReviewError:
                    pass  # A stale receipt must not block a fresh explicit review.
            if previous and previous.status in {"pending", "approved", "consumed"}:
                same = (previous.sha256, previous.mime, previous._origin, previous.question, previous.provider) == (
                    request.sha256, request.mime, request._origin, question, provider)
                if same or previous.status == "consumed":
                    raise ImageReviewError("image_review_pending", _IMAGE_ERRORS["pending"][1], previous.id)
            self._pending_image = request
        if previous:
            previous._transition("expired", "A newer image request replaced this preview.", allowed=("pending", "approved"))
        if origin and origin[0].can_confirm(origin[1]):
            origin[0].update(origin[1], "waiting_for_user_input", "Waiting for screenshot approval")
        try:
            visible_deadline = min(request.deadline, time.monotonic() + IMAGE_PREVIEW_TIMEOUT)
            try:
                self._image_review(request)
            except Exception as error:
                request._transition("preview_failed", f"Desktop image preview failed ({type(error).__name__}).",
                                    allowed=("pending", "approved"))
            while request.status == "pending":
                request._validate()
                if not request.visible.is_set() and time.monotonic() >= visible_deadline:
                    request._transition("preview_failed", "The desktop image preview did not become visible; no image was sent.",
                                        code="image_review_unavailable")
                    break
                request.done.wait(.05)
            if request.status != "approved":
                request._raise()
            request._validate()
            return request
        finally:
            with self._image_lock:
                if self._pending_image is request and request.status != "approved":
                    self._pending_image = None
                other_waiting = (self._pending_image is not None and self._pending_image is not request
                                 and self._pending_image._origin == origin and self._pending_image.status == "pending")
                if origin and not other_waiting and origin[0].can_confirm(origin[1]):
                    origin[0].update(origin[1], "executing", "Image approval decision received")

    def consume_image_approval(self, request, data, mime, question=""):
        """Consume one exact, current approval immediately before provider upload."""
        from core.task_control import _origin
        with self._image_lock:
            if not isinstance(request, ImageApproval) or self._pending_image is not request:
                raise ImageReviewError("image_review_failed", "This image approval is not the current pending request.",
                                       request.id if isinstance(request, ImageApproval) else "")
            if request.status != "approved":
                request._raise()
            if _origin.get() != request._origin:
                request._transition("cancelled", "Image approval belongs to another originating task.", allowed=("approved",))
                request._raise()
            if (not isinstance(data, bytes) or mime != request.mime or question != request.question
                    or hashlib.sha256(data).hexdigest() != request.sha256):
                request._transition("preview_failed", "Image bytes, MIME type or operation differ from the approved preview.",
                                    allowed=("approved",))
                request._raise()
            request._validate()
            if not request._transition("consumed", allowed=("approved",)):
                request._raise()
            self._pending_image = None
            return True

    def finish_image_review(self, request, status="completed"):
        """Publish provider completion and release the pending request, including failures."""
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("Unknown image operation outcome")
        with self._image_lock:
            if not isinstance(request, ImageApproval) or getattr(request, "_service", None) is not self:
                return False
            if self._pending_image is request:
                self._pending_image = None
            return request._transition(status, allowed=("approved", "consumed"))

    def check_cancelled(self):
        if self._cancelled():
            raise DesktopError("Desktop operation cancelled.")

    def backend(self):
        if self._backend is None:
            import pyautogui
            pyautogui.FAILSAFE = True
            self._backend = pyautogui
        return self._backend

    def observe(self):
        self.check_cancelled()
        return {**self._observer(), "monitors": [m.as_dict() for m in self._monitors()],
                "observed_at": self._clock()}

    def point(self, x, y):
        number(x, "x")
        number(y, "y")
        if int(x) != x or int(y) != y:
            raise DesktopError("Desktop coordinates must be integer physical pixels.")
        if not any(m.contains(x, y) for m in self._monitors()):
            raise DesktopError("Target is outside connected displays (or in a monitor gap).")
        return int(x), int(y)

    def speak_click(self):
        self.check_cancelled()
        if self._speak is None or self._speak(CLICK_PHRASE) is not True:
            raise DesktopError("Click refused: exact audible confirmation is unavailable.")
        self.check_cancelled()

    def feedback(self, x, y):
        if self._highlight is None:
            raise DesktopError("Click refused: visible highlight is unavailable.")
        self.speak_click()
        if self._highlight(x, y) is not True:
            raise DesktopError("Click refused: visible highlight did not complete.")
        self.check_cancelled()

    def _live_input_frame(self, points=()):
        """Refresh local evidence within the explicit sharing session, if active."""
        from core.screen_sharing import get_screen_sharing
        sharing = get_screen_sharing()
        state = sharing.snapshot()["state"]
        if state == "OFF":
            return None
        if state != "LIVE":
            raise DesktopError(f"Screen sharing is {state.lower()}; resume or stop sharing before desktop input.")
        frame = sharing.frame(force=True)
        self._validate_live_input(frame, points)
        return frame

    def _validate_live_input(self, frame, points=(), refresh=False):
        if frame is None:
            return
        from core.screen_sharing import get_screen_sharing
        self.check_cancelled()
        sharing = get_screen_sharing()
        if not sharing.valid(frame):
            raise DesktopError("Screen sharing permission changed; desktop input was cancelled.")
        rect = ScreenRect(**frame["source_rect"])
        if any(not rect.contains(x, y) for x, y in points):
            raise DesktopError("The target is outside the shared display; select that display before acting.")
        current = self._observer()
        if current.get("locked") or current.get("sensitive"):
            raise DesktopError("Desktop input refused in a locked or sensitive context.")
        if current.get("window_id") != frame["_sharing"].window_id:
            raise DesktopError("Foreground window changed since the shared frame; observe again.")
        if refresh:
            fresh = sharing.frame(force=True)
            if (fresh["_sharing"].sha256 != frame["_sharing"].sha256
                    or fresh["source_rect"] != frame["source_rect"]
                    or fresh["_sharing"].window_id != frame["_sharing"].window_id):
                raise DesktopError("Screen changed; observe again before desktop input.")
            if not sharing.valid(frame):
                raise DesktopError("Screen sharing permission changed; desktop input was cancelled.")

    def _refresh_after_input(self, frame):
        if frame is None:
            return False
        from core.screen_sharing import get_screen_sharing
        sharing = get_screen_sharing()
        if not sharing.valid(frame):
            return False
        try:
            return sharing.valid(sharing.frame(force=True))
        except Exception:
            return False  # Input already happened; a failed refresh is not verification.

    def move(self, x, y, duration=0.2, *, _frame=None):
        x, y = self.point(x, y)
        number(duration, "duration", 0, 5)
        self.check_cancelled()
        with self._lock, physical_coordinates():
            frame = self._live_input_frame(((x, y),)) if _frame is None else _frame
            self._validate_live_input(frame, ((x, y),))
            self.check_cancelled()
            self.backend().moveTo(x, y, duration=duration)
            if tuple(self.backend().position()) != (x, y):
                raise DesktopError("The cursor did not reach the requested physical position.")
        return {"ok": True, "x": x, "y": y}

    def click(self, x=None, y=None, button="left", clicks=1, verifier=None, preflight=None):
        if button not in ("left", "right", "middle") or type(clicks) is not int or clicks not in (1, 2):
            raise DesktopError("Unsupported mouse button or click count.")
        if (x is None) != (y is None):
            raise DesktopError("Supply both x and y.")
        with self._lock, physical_coordinates():
            if x is None:
                x, y = self.backend().position()
            x, y = self.point(x, y)
            before = self._observer()
            if before.get("locked") or before.get("sensitive"):
                raise DesktopError("Desktop is locked or sensitive; input refused.")
            frame = self._live_input_frame(((x, y),))
            self.move(x, y, _frame=frame)
            if self._highlight is None:
                raise DesktopError("Click refused: visible highlight is unavailable.")
            self.speak_click()
            # A speech queue can outlive the original target. Never click a different window.
            if self._observer().get("window_id") != before.get("window_id"):
                raise DesktopError("Foreground window changed during confirmation; observe again.")
            self.point(x, y)
            if preflight:
                preflight()
            self.check_cancelled()
            if self._highlight(x, y) is not True:
                raise DesktopError("Click refused: visible highlight did not complete.")
            self.check_cancelled()
            current = self._observer()
            if current.get("locked") or current.get("sensitive") or current.get("window_id") != before.get("window_id"):
                raise DesktopError("Foreground window changed, became sensitive or locked during highlighting; observe again.")
            if preflight:
                preflight()
            self._validate_live_input(frame, ((x, y),), refresh=True)
            self.point(x, y)
            self.check_cancelled()
            self.backend().click(x, y, button=button, clicks=clicks, interval=0.12)
            after = self._observer()
            verified = bool(verifier(before, after)) if verifier else None
            refreshed = self._refresh_after_input(frame)
            return {"ok": True, "x": x, "y": y, "clicks": clicks,
                    **({"observation_refreshed": refreshed} if frame is not None else {}),
                    "verified": verified, "verification": "verified" if verified else "Input dispatched; intended result requires observation."}

    def button(self, button="left", down=True, *, _frame=None):
        if button not in ("left", "right", "middle"):
            raise DesktopError("Unsupported mouse button.")
        with self._lock, physical_coordinates():
            if down:
                self.check_cancelled()
                before = self._observer()
                if before.get("locked") or before.get("sensitive"):
                    raise DesktopError("Desktop is locked or sensitive; input refused.")
                x, y = self.point(*self.backend().position())
                frame = self._live_input_frame(((x, y),)) if _frame is None else _frame
                self.feedback(x, y)
                if self._observer().get("window_id") != before.get("window_id"):
                    raise DesktopError("Foreground window changed during confirmation; observe again.")
                self._validate_live_input(frame, ((x, y),), refresh=True)
                self.backend().mouseDown(button=button)
                self._pressed.add(button)
            else:
                self.backend().mouseUp(button=button)
                self._pressed.discard(button)

    def release_buttons(self):
        with self._lock, physical_coordinates():
            for button in tuple(self._pressed):
                self.backend().mouseUp(button=button)
                self._pressed.discard(button)

    def drag(self, x1, y1, x2, y2, duration=0.5):
        self.point(x1, y1)
        self.point(x2, y2)
        number(duration, "duration", 0.1, 5)
        with self._lock, physical_coordinates():
            frame = self._live_input_frame(((x1, y1), (x2, y2)))
            self.move(x1, y1, _frame=frame)
            self.button(down=True, _frame=frame)
            try:
                steps = max(2, int(duration / 0.05))
                for step in range(1, steps + 1):
                    self.check_cancelled()
                    self.move(round(x1 + (x2-x1)*step/steps), round(y1 + (y2-y1)*step/steps), 0, _frame=frame)
                    time.sleep(duration / steps)
            finally:
                self.button(down=False)
            self._refresh_after_input(frame)
        return {"ok": True, "verified": None}

    def scroll(self, direction="down", amount=3):
        if direction not in ("up", "down", "left", "right"):
            raise DesktopError("Invalid scroll direction.")
        number(amount, "amount", 1, 100)
        if int(amount) != amount:
            raise DesktopError("Scroll amount must be an integer.")
        self.check_cancelled()
        with self._lock:
            point = self.point(*self.backend().position())
            frame = self._live_input_frame((point,))
            self._validate_live_input(frame, (point,))
            self.check_cancelled()
            method = self.backend().scroll if direction in ("up", "down") else self.backend().hscroll
            method(int(amount) * (1 if direction in ("up", "right") else -1))
            self._refresh_after_input(frame)

    def type_text(self, text, interval=0.03):
        if not isinstance(text, str) or len(text) > 20_000:
            raise DesktopError("Text must be a string of at most 20,000 characters.")
        number(interval, "typing interval", 0, 0.1)
        with self._lock:
            self.check_cancelled()
            original = self._observer()
            for start in range(0, len(text), 8):
                self.check_cancelled()
                current = self._observer()
                if current.get("locked") or current.get("sensitive") or current.get("window_id") != original.get("window_id"):
                    raise DesktopError("Window changed or became sensitive while typing; input stopped.")
                self.backend().typewrite(text[start:start+8], interval=interval)
        return {"ok": True, "characters": len(text)}

    def capture_region(self, rect: ScreenRect):
        """Explicit, in-memory capture. Callers must enforce their capture consent."""
        self.check_cancelled()
        state = self._observer()
        if state.get("locked") or state.get("sensitive"):
            raise DesktopError("Capture paused in a locked or sensitive context.")
        self.point(rect.left, rect.top)
        self.point(rect.left + rect.width - 1, rect.top + rect.height - 1)
        import mss
        import mss.tools
        with physical_coordinates(), mss.mss() as capture:
            shot = capture.grab(rect.as_dict())
            data = mss.tools.to_png(shot.rgb, shot.size)
        return data, hashlib.sha256(data).hexdigest(), state.get("window_id")

    def refresh_target(self, target: VisualTarget, fresh_frame):
        """Renew LIVE evidence only after proving unchanged pixels and scope."""
        original = target.sharing_frame
        if original is None:
            raise DesktopError("Fresh shared evidence is required to renew a visual target.")
        self._validate_live_input(original, ((target.x, target.y),))
        self._validate_live_input(fresh_frame, ((target.x, target.y),))
        old, fresh = original["_sharing"], fresh_frame["_sharing"]
        if (any(getattr(old, key) != getattr(fresh, key) for key in
                ("session_id", "generation", "origin", "monitor", "mime_type", "width", "height"))
                or original["source_rect"] != fresh_frame["source_rect"]
                or target.fingerprint != old.sha256 or target.fingerprint != fresh.sha256
                or target.window_id != old.window_id or target.window_id != fresh.window_id
                or fresh.captured_mono < target.observed_at):
            raise DesktopError("Screen changed; observe again before clicking.")
        renewed = replace(target, observed_at=fresh.captured_mono, sharing_frame=fresh_frame)
        self.validate_target(renewed, fresh.sha256, fresh.window_id)
        return renewed

    def validate_target(self, target: VisualTarget, fingerprint, window_id, max_age=5.0):
        self.check_cancelled()
        self._validate_live_input(target.sharing_frame, ((target.x, target.y),))
        number(target.confidence, "confidence", 0, 1)
        if target.confidence < 0.9:
            raise DesktopError("Target confidence is insufficient; ask the user to clarify.")
        if self._clock() - target.observed_at > max_age or self._clock() < target.observed_at:
            raise DesktopError("Visual target is stale; observe again.")
        if target.fingerprint != fingerprint or target.window_id != window_id:
            raise DesktopError("Screen changed; observe again before clicking.")
        return self.point(target.x, target.y)


_service = DesktopControl()


def get_service():
    return _service


def bind_feedback(highlight: Callable, speak: Callable, cancelled: Callable | None = None):
    _service.bind_feedback(highlight, speak, cancelled)
