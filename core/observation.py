"""Opt-in local window awareness. No screenshots, recording, AI calls or input."""

import ctypes
import math
import os
import re
import threading
import time
from urllib.parse import unquote, urlsplit, urlunsplit

import psutil

from core.desktop_control import foreground


_BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "chrome", "firefox"}
_SENSITIVE = re.compile(r"password|sign[ -]?in|log[ -]?in|auth|verification|one[ -]?time|passcode|credential|secret|token|checkout|payment|banking|\botp\b", re.I)
_SECRET = re.compile(r"\b\d{6,8}\b|\b[A-Za-z0-9_-]{32,}\b")
_DEFAULT_APPS = "chrome.exe,msedge.exe,firefox.exe,brave.exe,code.exe,notepad.exe,explorer.exe,blender.exe,winword.exe,excel.exe,powerpnt.exe"


def _navigation(url, title):
    if not isinstance(url, str) or not isinstance(title, str) or len(url) > 8192:
        return {"sensitive": True}
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return {"sensitive": True}
        path = unquote(parts.path)
        if parts.username or parts.password or _SENSITIVE.search(path + " " + title) or _SECRET.search(path + " " + title):
            return {"sensitive": True}
        host = parts.hostname
        authority = f"[{host}]" if ":" in host else host
        if parts.port:
            authority += f":{parts.port}"
        safe_title = re.sub(r"https?://[^\s]+", "[URL]", title)
        safe_title = " ".join(safe_title.split())
        return {"url": urlunsplit((parts.scheme, authority, parts.path[:512], "", "")),
                "website": host[:200], "page_title": safe_title[:160], "sensitive": False}
    except ValueError:
        return {"sensitive": True}


class _ForegroundEvents:
    """WinEvent callbacks only mark work pending; cleanup stays on the hook thread."""

    def __init__(self, changed):
        self.changed = changed
        self.stop_event = threading.Event()
        self.thread_id = None
        self.thread = threading.Thread(target=self._run, name="chidvi-foreground-events", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)

    def _run(self):
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                          wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
        callback = callback_type(lambda *_: self.changed())
        user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                          callback_type, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        message = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        hook = user32.SetWinEventHook(3, 3, None, callback, 0, 0, 0)  # EVENT_SYSTEM_FOREGROUND
        if not hook:
            return  # Bounded metadata polling remains available.
        try:
            while not self.stop_event.is_set() and user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            user32.UnhookWinEvent(hook)


class ObservationController:
    def __init__(self, observer=foreground, clock=time.monotonic, on_status=lambda _status: None):
        self._observer, self._clock, self._on_status = observer, clock, on_status
        self._lock = threading.RLock()
        self._dirty = threading.Event()
        self._hook = None
        self._mode, self._status = "stopped", "Observation stopped"
        self._context, self._navigation, self._greeted = {}, {}, {}
        self._generation = 0
        self._next_due = self._retry_at = 0.0
        self._last_sample = self._last_greeting = -math.inf
        self._interval, self._failures = 1.0, 0
        self._ac_only, self._proactive = True, False
        try:
            self._cooldown = max(15.0, min(3600.0, float(os.environ.get("CHIDVI_PROACTIVE_COOLDOWN", "120"))))
            if not math.isfinite(self._cooldown):
                self._cooldown = 120.0
        except ValueError:
            self._cooldown = 120.0
        self._supported = {app.strip().lower() for app in os.environ.get("CHIDVI_PROACTIVE_APPS", _DEFAULT_APPS).split(",") if app.strip()}

    def _set_status(self, status):
        if status != self._status:
            self._status = status
            try:
                self._on_status(status)
            except Exception:
                pass  # A UI callback must not break observation cleanup.

    def set_mode(self, mode):
        if mode not in {"running", "paused", "stopped"}:
            raise ValueError("Observation mode must be running, paused or stopped.")
        with self._lock:
            if self._mode == mode:
                return
            self._mode = mode
            self._generation += 1
            self._context, self._navigation = {}, {}
            self._next_due = self._retry_at = 0.0
            if mode == "running":
                self._dirty.set()
                self._set_status("Observation enabled: local metadata only")
                if os.name == "nt" and self._observer is foreground:
                    self._hook = _ForegroundEvents(self._dirty.set)
                    try:
                        self._hook.start()
                    except RuntimeError:
                        self._hook = None  # Bounded polling still works if no thread can start.
            else:
                self._dirty.clear()
                if self._hook:
                    self._hook.stop()
                    self._hook = None
                self._set_status(f"Observation {mode}")
                if mode == "stopped":
                    self._greeted.clear()

    def configure(self, ac_only=True, proactive=False):
        if not isinstance(ac_only, bool) or not isinstance(proactive, bool):
            raise ValueError("Observation settings must be booleans.")
        with self._lock:
            self._ac_only, self._proactive = ac_only, proactive
            self._generation += 1
            self._next_due = self._retry_at = 0.0
            self._dirty.set()

    def clear(self):
        with self._lock:
            self._context, self._navigation, self._greeted = {}, {}, {}
            self._generation += 1
            self._dirty.set()
            self._set_status("Observations cleared; no images or history were saved")

    def notify_navigation(self, url, title=""):
        with self._lock:
            if self._mode != "running":
                return
            navigation = _navigation(url, title)
            if navigation != self._navigation:
                self._navigation = navigation
                self._dirty.set()

    def _suspend(self, reason, now, delay=5):
        self._context = {}
        self._retry_at = now + delay
        self._set_status("Observation paused: " + reason)

    def poll(self, busy=False, speaking=False):
        """Call from a worker; returns changed context and at most one help offer."""
        with self._lock:
            if self._mode != "running":
                return None
            now = self._clock()
            if now < self._retry_at or now - self._last_sample < 0.5:
                return None
            if now < self._next_due and not self._dirty.is_set():
                return None
            generation, ac_only = self._generation, self._ac_only
            self._dirty.clear()
            self._last_sample = now
        try:
            battery = psutil.sensors_battery() if ac_only else None
            reason = "connect AC power" if battery is not None and not battery.power_plugged else ""
            if not reason and (psutil.cpu_percent(interval=None) > 85 or psutil.virtual_memory().percent > 90):
                reason = "system resources are busy"
            raw = None if reason else self._observer()
            if not reason and not isinstance(raw, dict):
                raise ValueError("Invalid window observation")
        except Exception:
            with self._lock:
                if generation == self._generation and self._mode == "running":
                    self._failures += 1
                    self._suspend("window/power diagnostics unavailable; retrying", now, min(60, 2 ** min(self._failures, 6)))
            return None
        with self._lock:
            if generation != self._generation or self._mode != "running":
                return None
            if reason:
                self._suspend(reason, now)
                return None
            title, app = str(raw.get("title", "")), str(raw.get("application", ""))
            app = app.replace("\\", "/").rsplit("/", 1)[-1][:80]
            navigation = self._navigation if app.lower() in _BROWSERS else {}
            if raw.get("locked") or app.lower() in {"lockapp.exe", "logonui.exe", "credentialuibroker.exe"}:
                self._navigation = {}
                self._suspend("desktop is locked", now, 2)
                return None
            if raw.get("sensitive") or navigation.get("sensitive") or _SENSITIVE.search(title) or _SECRET.search(title):
                self._suspend("sensitive window or page", now, 2)
                return None
            # Titles can contain URLs; retain only their origin, never query/fragment values.
            title = re.sub(r"https?://[^\s]+", lambda match: _navigation(match.group(), "").get("website", "[private URL]"), title)
            title = " ".join(title.split())
            context = {"application": app, "title": title[:160], "locked": False, "sensitive": False}
            if isinstance(raw.get("window_id"), int) and not isinstance(raw["window_id"], bool):
                context["window_id"] = raw["window_id"]
            context.update({key: value for key, value in navigation.items() if key != "sensitive"})
            changed = context != self._context
            self._context = context
            self._failures = 0
            self._interval = 1.0 if changed else min(15.0, self._interval * 1.5)
            self._next_due = now + self._interval
            self._retry_at = 0.0
            self._set_status("Monitoring: local window metadata only")
            event = {"context": dict(context), "status": self._status}
            identity = context.get("website") or app.lower()
            eligible = app.lower() in self._supported or "*" in self._supported
            if (self._proactive and eligible and identity and identity not in self._greeted
                    and not busy and not speaking and now - self._last_greeting >= self._cooldown):
                label = context.get("website") or app.removesuffix(".exe")
                event["greeting"] = f"I see you've opened {label}. What would you like to do? Leave it to me, I'll take care of it."
                self._greeted[identity] = now
                if len(self._greeted) > 128:
                    self._greeted.pop(next(iter(self._greeted)))
                self._last_greeting = now
            if busy and changed:
                event["task_status"] = "A task is already running; context updated."
            return event if changed or "greeting" in event else None

    def snapshot(self):
        with self._lock:
            return {"mode": self._mode, "status": self._status, "ac_only": self._ac_only,
                    "proactive": self._proactive, "context": dict(self._context),
                    "stores_images": False, "external_analysis": False}
