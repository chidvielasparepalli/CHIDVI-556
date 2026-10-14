"""Opt-in real desktop check using the agent's normal screen_process dispatcher."""
import asyncio
import json
import threading
import time
import uuid
from types import SimpleNamespace


def start(ui, jarvis, workspace):
    question = "Describe the visible application and its main controls using only this screenshot."
    task = jarvis._tasks.submit(question, uuid.uuid4().hex, "desktop")
    jarvis._tasks.claim(task["id"])
    report = {"started_at": time.time(), "task_id": task["id"], "events": [],
              "mode": "real application / real screen_process / explicit user approval"}
    path = workspace / "logs" / "vision-acceptance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.RLock()
    previous_status = ui.operator.on_image_status

    def save():
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(path)

    def status(snapshot):
        if previous_status:
            previous_status(snapshot)
        if snapshot["task_id"] == task["id"]:
            with lock:
                report["events"].append({**snapshot, "at": time.time()})
                save()
    ui.operator.on_image_status = status
    ui._win.showNormal()
    ui._win.raise_()
    ui._win.activateWindow()
    ui.write_log("SYS: Live screenshot check: capture → visible preview → your approval → one cloud analysis → this task's result.")

    async def run():
        jarvis._loop = asyncio.get_running_loop()
        try:
            with jarvis._tasks.context(task["id"]):
                response = await jarvis._execute_tool_for_task(SimpleNamespace(
                    id="vision-acceptance", name="screen_process",
                    args={"text": question, "action": "analyze", "angle": "screen"}))
            result = response.response["result"]
            with lock:
                report["result"] = result
                report["response_id"] = response.id
                report["finished_at"] = time.time()
                save()
            if isinstance(result, dict) and result.get("analyzed") is True:
                ui.write_log("JARVIS: " + result["description"])
                jarvis._tasks.update(task["id"], "succeeded", "Real screenshot description returned to the originating task")
            else:
                message = result.get("message", result) if isinstance(result, dict) else result
                ui.write_log("SYS: Live screenshot check did not complete analysis: " + str(message))
                jarvis._tasks.update(task["id"], "failed", "Live screenshot check did not complete analysis")
        except Exception as exc:
            with lock:
                report["error_type"] = type(exc).__name__
                save()
            ui.write_log("ERR: Live screenshot check failed: " + type(exc).__name__)
            jarvis._tasks.update(task["id"], "failed", type(exc).__name__)
        finally:
            jarvis._tasks.release(task["id"])
            jarvis._loop = None

    with lock:
        save()
    threading.Thread(target=lambda: asyncio.run(run()), name="vision-acceptance", daemon=True).start()
