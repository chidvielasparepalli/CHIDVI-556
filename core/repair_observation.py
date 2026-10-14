"""Explicit, session-scoped repair screenshots; background work is local only."""
from __future__ import annotations

from datetime import datetime, timezone
import copy
import hashlib
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time

import psutil

from core.desktop_control import foreground
from core.observation import _SENSITIVE, _SECRET


class RepairObservation:
    def __init__(self, workspace, emit, capture=None, observer=foreground, sharing=None):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError("Repair workspace must be an existing directory")
        self._use_sharing = capture is None or sharing is not None
        self._sharing = sharing
        if capture is None:
            from actions.screen_processor import capture_screen
            capture = capture_screen
        self._capture, self._observer, self._emit = capture, observer, emit
        self._lock, self._capture_lock = threading.RLock(), threading.Lock()
        self._worker, self._stop_event = None, threading.Event()
        self._generation = 0
        self._mode, self._status = "stopped", "Repair observation OFF"
        self._task_id = None
        self._interval, self._monitor, self._retain = 5, 1, False
        self._latest, self._latest_frame, self._captured_at = {}, None, 0.0
        self._path_broker = None

    def _sharing_state(self):
        # Explicitly injected capture providers retain their independent test/
        # integration behavior unless a sharing service is also supplied.
        if not self._use_sharing:
            return None, "OFF"
        service = self._sharing
        if service is None:
            from core.screen_sharing import get_screen_sharing
            service = get_screen_sharing()
        return service, service.snapshot()["state"]

    def snapshot(self):
        with self._lock:
            return {"mode": self._mode, "status": self._status, "task_id": self._task_id,
                    "interval": self._interval, "monitor": self._monitor, "retain": self._retain,
                    "latest": copy.deepcopy(self._latest),
                    "local_backend": {"available": False, "reason": "Local image interpretation is unavailable; window metadata and logs remain available."},
                    "automatic_cloud_analysis": False}

    def _publish(self):
        # Invoked under the state lock: stop returning guarantees no late events/writes.
        event = {"type": "repair_observation", "monitoring": self._mode == "running",
                 "observation": self.snapshot(), "message": self._status}
        try:
            self._emit(event)
        except Exception:
            pass  # UI shutdown must not strand the capture worker.

    def start(self, task_id, interval=5, monitor=1, retain=False):
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", task_id):
            raise ValueError("A safe repair task ID is required")
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or not 2 <= interval <= 60):
            raise ValueError("Observation interval must be 2–60 seconds")
        if isinstance(monitor, bool) or not isinstance(monitor, int) or not 0 <= monitor <= 64:
            raise ValueError("Monitor must be 0 (all) or a display index from 1–64")
        if type(retain) is not bool:
            raise ValueError("Screenshot retention requires explicit boolean consent")
        with self._lock:
            self._stop_event.set()
            self._generation += 1
            generation = self._generation
            self._task_id, self._interval, self._monitor, self._retain = task_id, interval, monitor, retain
            self._mode, self._status = "running", "Repair observation ON — local screenshots; no automatic cloud analysis"
            self._latest, self._latest_frame = {}, None
            self._stop_event = threading.Event()
            self._worker = threading.Thread(target=self._run, args=(generation, self._stop_event),
                                            name="chidvi-repair-observation", daemon=True)
            self._publish()
            self._worker.start()
            return self.snapshot()

    def _run(self, generation, stopped):
        while not stopped.is_set():
            with self._lock:
                if generation != self._generation or self._mode != "running":
                    return
                interval = self._interval
            self.refresh()
            if stopped.wait(interval):
                return

    def _end(self, mode):
        with self._lock:
            self._generation += 1
            self._stop_event.set()
            self._mode, self._status = mode, f"Repair observation {mode.upper()} — images cleared from memory"
            self._latest, self._latest_frame = {}, None
            worker, self._worker = self._worker, None
            self._publish()
        if worker is not None and worker is not threading.current_thread() and worker.is_alive():
            worker.join(timeout=0.25)
        return self.snapshot()

    def pause(self):
        return self._end("paused")

    def stop(self):
        return self._end("stopped")

    @staticmethod
    def _window(raw):
        if not isinstance(raw, dict):
            raise ValueError("Window metadata is unavailable")
        title = str(raw.get("title", ""))
        app = str(raw.get("application", "")).replace("\\", "/").rsplit("/", 1)[-1][:80]
        if (raw.get("locked") or raw.get("sensitive") or _SENSITIVE.search(title) or _SECRET.search(title)
                or app.lower() in {"lockapp.exe", "logonui.exe", "credentialuibroker.exe"}):
            raise PermissionError("Sensitive or locked foreground")
        window_id = raw.get("window_id")
        if isinstance(window_id, bool) or not isinstance(window_id, int) or window_id <= 0:
            raise ValueError("Foreground window is unknown")
        # Titles/URLs and control values are deliberately absent from retained metadata.
        return {"application": app, "window_id": window_id}

    def _failure(self, generation, code, message):
        with self._lock:
            if generation != self._generation or self._mode != "running":
                return {"ok": False, "code": "cancelled", "message": "Observation stopped; late capture discarded."}
            self._latest, self._latest_frame = {}, None
            self._status = message
            self._publish()
            return {"ok": False, "code": code, "message": message, **self.snapshot()}

    def refresh(self):
        with self._lock:
            if self._mode != "running":
                return {"ok": False, "code": "observation_inactive", "message": "Enable repair observation for this task first.", **self.snapshot()}
            generation, monitor, task_id = self._generation, self._monitor, self._task_id
        if not self._capture_lock.acquire(blocking=False):
            return {"ok": False, "code": "capture_busy", "message": "A repair snapshot is already being captured."}
        try:
            if psutil.cpu_percent(interval=None) > 85 or psutil.virtual_memory().percent > 90:
                return self._failure(generation, "resources_busy", "Observation deferred: system resources are busy.")
            try:
                before = self._window(self._observer())
            except PermissionError:
                return self._failure(generation, "privacy_guard", "Observation paused for a locked or sensitive foreground.")
            with self._lock:
                if generation != self._generation or self._mode != "running":
                    return {"ok": False, "code": "cancelled", "message": "Observation stopped before capture."}
            sharing, sharing_state = self._sharing_state()
            shared = sharing_state in {"STARTING", "LIVE"}
            if sharing_state not in {"OFF", "STARTING", "LIVE"}:
                return self._failure(generation, "sharing_inactive",
                    "Live sharing is paused or unavailable; resume or stop it before refreshing repair snapshots.")
            frame = sharing.frame(max_age=2, monitor=monitor) if shared else self._capture(monitor=monitor)
            with self._lock:
                if generation != self._generation or self._mode != "running":
                    return {"ok": False, "code": "cancelled", "message": "Observation stopped; late capture discarded."}
            from core.vision import _image_metadata
            metadata = _image_metadata(frame)
            try:
                after = self._window(self._observer())
            except PermissionError:
                return self._failure(generation, "privacy_guard", "Foreground became sensitive; screenshot discarded.")
            if before != after:
                return self._failure(generation, "foreground_changed", "Foreground changed during capture; screenshot discarded.")
            if shared and not sharing.valid(frame):
                return self._failure(generation, "sharing_inactive", "Live sharing ended; its repair snapshot was discarded.")
            data = frame["data"]
            with self._lock:
                if generation != self._generation or self._mode != "running":
                    return {"ok": False, "code": "cancelled", "message": "Observation stopped; late capture discarded."}
                retained_path = None
                if self._retain:
                    if self._path_broker is None:
                        from core.repair_permissions import PermissionBroker
                        self._path_broker = PermissionBroker(self.workspace)
                    folder = self._path_broker.path(Path("logs") / "repair" / ("task-" + task_id))
                    folder.mkdir(parents=True, exist_ok=True)
                    retained_path = self._path_broker.path(folder / "latest.png", write=True)
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=folder, prefix=".capture-", suffix=".tmp", delete=False) as stream:
                            temporary = Path(stream.name)
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        self._path_broker.path(retained_path, write=True)
                        os.replace(temporary, retained_path)
                    finally:
                        if temporary is not None:
                            temporary.unlink(missing_ok=True)
                self._latest_frame = {**metadata, "data": data}
                self._captured_at = time.monotonic()
                self._latest = {**metadata, "task_id": task_id, "timestamp": datetime.now(timezone.utc).isoformat(),
                                "sha256": hashlib.sha256(data).hexdigest(), "window": before,
                                "source": "live_sharing" if shared else "local_capture"}
                if retained_path is not None:
                    self._latest["retained_path"] = str(retained_path.relative_to(self.workspace))
                self._status = ("Monitoring repair task — shared live frame reused; no extra upload" if shared else
                                "Monitoring repair task — latest local screenshot available; no automatic cloud analysis")
                self._publish()
                return {"ok": True, "code": "captured", **self.snapshot()}
        except Exception as exc:
            from core.screen_sharing import ScreenSharingError
            if isinstance(exc, ScreenSharingError):
                return self._failure(generation, exc.code, exc.message)
            return self._failure(generation, "capture_failed", "Repair screenshot unavailable; local window/log diagnostics can continue.")
        finally:
            self._capture_lock.release()

    def analyze_latest(self, question):
        """Explicit analysis: fresh task-scoped LIVE lease, otherwise one-shot review."""
        with self._lock:
            if self._mode != "running":
                return {"ok": False, "code": "snapshot_unavailable", "message": "Enable observation and refresh a repair snapshot first."}
        try:
            sharing, sharing_state = self._sharing_state()
        except Exception:
            return {"ok": False, "code": "sharing_unavailable", "message": "Live sharing status is unavailable; no image was analyzed."}
        if sharing_state in {"STARTING", "LIVE"}:
            return self._analyze_shared(sharing, question)
        if sharing_state != "OFF":
            return {"ok": False, "code": "sharing_inactive", "message": "Resume or stop live sharing before requesting repair image analysis."}
        with self._lock:
            if self._mode != "running" or self._latest_frame is None:
                return {"ok": False, "code": "snapshot_unavailable", "message": "Enable observation and refresh a repair snapshot first."}
            if self._latest.get("source") == "live_sharing":
                return {"ok": False, "code": "snapshot_unavailable", "message": "Sharing ended; refresh a local repair snapshot before requesting one-shot analysis."}
            analysis_started = time.monotonic()
            if analysis_started - self._captured_at > max(10, self._interval * 2):
                return {"ok": False, "code": "stale_snapshot", "message": "Refresh the repair snapshot before analyzing it."}
            generation, task_id = self._generation, self._task_id
            frame, window = dict(self._latest_frame), dict(self._latest["window"])
            # Freshness is required at request start. Freeze the exact pixels for
            # 180 seconds of explicit approval plus the provider's 45-second limit.
            # A background refresh must never renew this independent deadline.
            deadline = analysis_started + 225

        def valid():
            # The approval dialog may itself be foreground; other local samples
            # must not revoke these exact pixels solely because focus changed.
            with self._lock:
                return (self._mode == "running" and generation == self._generation
                        and task_id == self._task_id and self._latest_frame is not None
                        and self._latest.get("task_id") == task_id
                        and analysis_started <= time.monotonic() < deadline)
        try:
            if self._window(self._observer()) != window:
                return self._failure(generation, "foreground_changed", "Foreground changed; refresh the repair snapshot.")
        except Exception:
            return self._failure(generation, "privacy_guard", "Image analysis paused for unavailable or sensitive foreground.")
        if not valid():
            return {"ok": False, "code": "cancelled", "message": "Observation changed or the snapshot expired before analysis."}
        from core.vision import analyze_capture
        result = analyze_capture(frame, question, valid=valid)
        if not valid():
            return {"ok": False, "code": "cancelled", "message": "Observation changed or the snapshot expired; late analysis discarded."}
        return result

    def _analyze_shared(self, sharing, question):
        with self._lock:
            generation, task_id, monitor = self._generation, self._task_id, self._monitor
        frame = None

        def valid():
            with self._lock:
                current = (self._mode == "running" and generation == self._generation and task_id == self._task_id)
            # Do not hold observation locks while calling the shared service:
            # service state callbacks may pause observation during shutdown.
            return current and (frame is None or sharing.valid(frame))

        if not valid():
            return {"ok": False, "code": "cancelled", "message": "Observation ended before analysis."}
        try:
            # A fresh lease must be created in this caller's ContextVar task.
            # The shared service may reuse recent pixels, avoiding another MSS capture.
            frame = sharing.frame(max_age=2, monitor=monitor)
        except Exception as exc:
            from core.screen_sharing import ScreenSharingError
            if isinstance(exc, ScreenSharingError):
                return {"ok": False, "code": exc.code, "message": exc.message}
            return {"ok": False, "code": "sharing_unavailable", "message": "A shared repair frame is unavailable; no image was analyzed."}
        if not valid():
            return {"ok": False, "code": "cancelled", "message": "Observation or live sharing ended before analysis."}
        from core.vision import analyze_capture
        result = analyze_capture(frame, question, valid=valid)
        if not valid():
            return {"ok": False, "code": "cancelled", "message": "Observation or live sharing ended; late analysis discarded."}
        return result
