"""Bounded, in-memory task identities shared by desktop, voice and phone."""
from __future__ import annotations

import threading
import time
import uuid
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar


STATES = {"queued", "planning", "waiting_for_user_input", "executing", "verifying",
          "succeeded", "failed", "cancelled"}
TERMINAL = {"succeeded", "failed", "cancelled"}
_origin = ContextVar("chidvi_task_origin", default=None)


def check_task_cancelled():
    origin = _origin.get()
    if origin and origin[0].event(origin[1]).is_set():
        raise RuntimeError("Originating task was cancelled or failed; no further action allowed.")


def result_state(result):
    """Bridge existing readable tool results and the structured operator tools."""
    if isinstance(result, str):
        if "[CONFIRMATION_PENDING]" in result:
            return "waiting_for_user_input"
        try:
            structured = json.loads(result)
        except (ValueError, TypeError):
            structured = None
        if isinstance(structured, dict):
            result = structured
        elif re.search(r"failed:|browser error|timed out|could not|not available|refused|^NOT_FOUND$|element not found",
                       result[:180], re.IGNORECASE):
            return "failed"
    if isinstance(result, dict) and result.get("ok") is False:
        return "waiting_for_user_input" if result.get("code") == "image_review_pending" else "failed"
    return "verifying"


class TaskStore:
    def __init__(self, changed=None, limit=2000):
        self._tasks = {}
        self._requests = {}
        self._events = {}
        self._lock = threading.RLock()
        self.active_id = None
        self.changed = changed or (lambda task: None)
        self.limit = limit

    def submit(self, text, request_id, source="desktop", input_delivered=False, executor="agent"):
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ValueError("Commands must contain 1–8000 characters.")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("A request ID of 1–128 characters is required.")
        if not isinstance(source, str) or not 1 <= len(source) <= 128:
            raise ValueError("Invalid command source.")
        if executor not in ("agent", "repair"):
            raise ValueError("Invalid task executor.")
        with self._lock:
            key = (source, request_id)
            if key in self._requests:
                task = self._tasks[self._requests[key]]
                if task["text"] != text.strip() or task["executor"] != executor:
                    raise ValueError("Request ID already used for another command.")
                return dict(task)
            # Retain every request ID for the session: history eviction must not replay input.
            if len(self._tasks) >= self.limit:
                raise ValueError("Task history is full; restart the application to start a new session.")
            if sum(t["state"] not in TERMINAL for t in self._tasks.values()) >= 32:
                raise ValueError("Task queue is full; finish or cancel pending commands first.")
            task_id = uuid.uuid4().hex
            task = {"id": task_id, "request_id": request_id, "source": source,
                    "executor": executor,
                    "text": text.strip(), "state": "queued", "detail": "Queued",
                    "input_delivered": bool(input_delivered),
                    "created_at": time.time(), "updated_at": time.time()}
            self._tasks[task_id] = task
            self._requests[key] = task_id
            self._events[task_id] = threading.Event()
        self.changed(dict(task))
        return dict(task)

    def claim(self, task_id=None):
        with self._lock:
            if self.active_id is not None:
                return None
            task = (self._tasks.get(task_id) if task_id else
                    next((t for t in self._tasks.values() if t["state"] == "queued" and t["executor"] == "agent"), None))
            if task is None:
                return None
            if task["state"] != "queued":
                return None
            self.active_id = task["id"]
        return self.update(task["id"], "planning", "Understanding the command")

    def update(self, task_id, state, detail=""):
        if state not in STATES:
            raise ValueError("Unknown task state.")
        with self._lock:
            task = self._tasks[task_id]
            if task["state"] in TERMINAL:
                return dict(task)
            task.update(state=state, detail=str(detail)[:2000], updated_at=time.time())
            if state in {"failed", "cancelled"}:
                self._events[task_id].set()
            snapshot = dict(task)
        self.changed(snapshot)
        return snapshot

    def cancel(self, task_id):
        with self._lock:
            if task_id not in self._tasks:
                raise KeyError("Unknown task")
            self._events[task_id].set()
        return self.update(task_id, "cancelled", "Cancelled; an in-flight operation may finish")

    def cancelled(self):
        with self._lock:
            origin = _origin.get()
            if origin and origin[0] is self:
                return self._events[origin[1]].is_set()
            return bool(self.active_id and self._events[self.active_id].is_set())

    @contextmanager
    def context(self, task_id):
        token = _origin.set((self, task_id) if task_id else None)
        try:
            yield
        finally:
            _origin.reset(token)

    def owner(self):
        origin = _origin.get()
        return origin[1] if origin and origin[0] is self else self.active_id

    def can_confirm(self, owner):
        with self._lock:
            return bool(owner and owner == self.active_id and owner in self._tasks
                        and self._tasks[owner]["state"] not in TERMINAL
                        and not self._events[owner].is_set())

    def event(self, task_id):
        with self._lock:
            return self._events[task_id]

    def release(self, task_id):
        with self._lock:
            if self.active_id == task_id:
                self.active_id = None

    def get(self, task_id):
        with self._lock:
            return dict(self._tasks[task_id])

    def snapshot(self):
        with self._lock:
            terminal = [t for t in self._tasks.values() if t["state"] in TERMINAL]
            recent = {t["id"] for t in terminal[-100:]}
            return [dict(t) for t in self._tasks.values() if t["state"] not in TERMINAL or t["id"] in recent]
