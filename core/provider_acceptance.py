"""Opt-in desktop check using the application's real queue and selected provider."""
import json
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import QTimer


def start(ui, agent, workspace):
    timer = QTimer(ui._win)
    started = time.monotonic()
    state = {"task_id": None}
    destination = Path(workspace) / "logs" / "provider-desktop-check.json"

    def finish(ok, message):
        timer.stop()
        profile = agent._providers.store.selected("chat", task_kind="chat")
        task = agent._tasks.get(state["task_id"]) if state["task_id"] else None
        report = {"ok": ok, "message": message, "ui_visible": ui._win.isVisible(),
                  "provider": profile["provider"] if profile else None,
                  "model": profile["model"] if profile else None,
                  "task_id": state["task_id"], "task_state": task["state"] if task else None,
                  "live_connected": agent.session is not None,
                  "screen_sharing": agent._screen_sharing.snapshot()["state"],
                  "microphone_and_dashboard": "disabled for this check"}
        dialog = getattr(ui._win, "_provider_dialog", None)
        report["provider_settings_visible"] = bool(dialog and dialog.isVisible())
        if report["provider_settings_visible"]:
            target = Path(workspace) / "screenshots" / "provider-desktop-settings.png"
            target.parent.mkdir(exist_ok=True)
            report["settings_image_saved"] = dialog.grab().save(str(target))
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
        ui.write_log(("SYS: " if ok else "ERR: ") + message)

    def tick():
        if time.monotonic() - started > 50:
            if state["task_id"]:
                agent._cancel_task(state["task_id"])
            finish(False, "Provider desktop check timed out; no request was retried.")
            return
        if state["task_id"] is None:
            if agent._loop is None or not agent._chat_available() or agent._tasks.active_id:
                return
            request_id = "provider-check-" + uuid.uuid4().hex
            state["task_id"] = agent._submit_task("For the connection check, reply exactly PROVIDER_READY. Do not use any tools.", request_id)["id"]
            ui.write_log("SYS: Checking the selected provider through the normal desktop task queue.")
        else:
            task = agent._tasks.get(state["task_id"])
            if task["state"] in {"succeeded", "failed", "cancelled"}:
                grounded = any("PROVIDER_READY" in row and not row.startswith("User:") for row in agent._session_log[-6:])
                ok = task["state"] == "succeeded" and grounded
                finish(ok, "Provider desktop check passed: the selected provider returned PROVIDER_READY to the original task."
                       if ok else "Provider desktop check did not receive the requested verification reply.")

    timer.timeout.connect(tick)
    timer.start(250)
    ui._provider_check_timer = timer
