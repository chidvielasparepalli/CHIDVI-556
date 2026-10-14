"""Opt-in real application/Live check; Start consent is always left to the user."""
import asyncio
import json
import time
import uuid

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


def start(ui, jarvis, workspace):
    report = {"started_at": time.time(), "status": "waiting_for_start",
              "mode": "real desktop / existing Gemini Live session / normal task dispatcher",
              "checks": [], "tools": [], "image_reviews": 0}
    path = workspace / "logs" / "live-sharing-acceptance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"phase": "waiting", "task": None, "future": None, "at": time.time(),
             "window": None, "expected": "", "clicked": False, "finished": False}
    previous_review = ui.operator.on_image_status
    original_execute = jarvis._execute_tool

    def save():
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(path)

    def review(snapshot):
        if previous_review:
            previous_review(snapshot)
        if snapshot.get("status") == "pending":
            report["image_reviews"] += 1
            save()

    async def execute(fc):
        report["tools"].append({"name": fc.name, "task_id": jarvis._tasks.active_id, "at": time.time()})
        return await original_execute(fc)

    ui.operator.on_image_status = review
    jarvis._execute_tool = execute
    timer = QTimer(ui._win)

    def finish(error=None):
        if state["finished"]:
            return
        state["finished"] = True
        timer.stop()
        jarvis._screen_sharing.stop("Acceptance check finished")
        future = state.get("future")
        if state["task"] is None and future and future.done() and not future.cancelled():
            try:
                state["task"] = future.result()["id"]
            except Exception:
                pass
        if future and not future.done():
            future.cancel()
        task_id = state["task"]
        if task_id and jarvis._tasks.get(task_id)["state"] not in {"succeeded", "failed", "cancelled"}:
            jarvis._tasks.cancel(task_id)
            if jarvis._tasks.active_id == task_id and jarvis._loop:
                jarvis._loop.call_soon_threadsafe(jarvis.interrupt)
        jarvis._execute_tool = original_execute
        ui.operator.on_image_status = previous_review
        report["finished_at"] = time.time()
        report["sharing"] = jarvis._screen_sharing.snapshot()
        report["latest_provider_usage"] = getattr(jarvis, "_live_usage", {})
        if error:
            report["error"] = str(error)
        report["status"] = "passed" if not error and report["checks"] and all(
            check["passed"] for check in report["checks"]) and not report["image_reviews"] else "incomplete"
        if state["window"]:
            state["window"].hide()
        ui._win.showNormal()
        ui._win.raise_()
        ui._win.activateWindow()
        ui.write_log("SYS: Live sharing check " + report["status"] + ". See logs/live-sharing-acceptance.json.")
        save()

    def check(name, passed, **details):
        report["checks"].append({"name": name, "passed": bool(passed), **details})
        save()

    def scene(number):
        if state["window"]:
            state["window"].hide()
        window = QWidget(ui._win, __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.WindowType.Window)
        window.setWindowTitle(f"CHIDVI Live Sharing Check {number}")
        background = "#123c9b" if number == 1 else "#206238"
        window.setStyleSheet(f"QWidget {{background:{background}; color:white;}} QLabel {{font-size:32px;}} QPushButton {{background:white; color:black; padding:22px; font-size:24px;}}")
        layout = QVBoxLayout(window)
        banner = QLabel("LIVE SCREEN SHARING TEST — sampled frames sent to Gemini")
        banner.setWordWrap(True)
        layout.addWidget(banner)
        label = QLabel("BLUE PANEL" if number == 1 else "GREEN PANEL")
        layout.addWidget(label)
        nonce = ("ORBIT " if number == 1 else "CEDAR ") + str(int(uuid.uuid4().hex[:6], 16) % 9000 + 1000)
        layout.addWidget(QLabel("Visible verification word: " + nonce))
        marker = QLabel("Marker: OFF")
        layout.addWidget(marker)
        toggle = QPushButton("Toggle marker")
        def clicked():
            state["clicked"] = not state["clicked"]
            marker.setText("Marker: ON" if state["clicked"] else "Marker: OFF")
        toggle.clicked.connect(clicked)
        layout.addWidget(toggle)
        stop = QPushButton("Stop check and sharing")
        stop.clicked.connect(lambda: finish("Stopped by user"))
        layout.addWidget(stop)
        window.showMaximized()
        window.raise_()
        window.activateWindow()
        state.update(window=window, expected=nonce, at=time.time(), phase=f"scene{number}")

    def question(text, phase):
        async def submit():
            if state["finished"]:
                return None
            task = jarvis._submit_task(text, uuid.uuid4().hex, "desktop")
            state["task"] = task["id"]
            if state["finished"]:
                jarvis._tasks.cancel(task["id"])
            return task
        state.update(task=None, phase=phase, at=time.time())
        state["future"] = asyncio.run_coroutine_threadsafe(submit(), jarvis._loop)
        ui.write_log("You: " + text)

    def completed():
        if state["task"] is None:
            if not state["future"].done():
                return None
            state["task"] = state["future"].result()["id"]
        task = jarvis._tasks.get(state["task"])
        if task["state"] not in {"succeeded", "failed", "cancelled"}:
            return None
        return task

    def tick():
        try:
            sharing = jarvis._screen_sharing.snapshot()
            phase = state["phase"]
            if phase == "waiting":
                if sharing["state"] == "LIVE" and jarvis.session is not None:
                    report["status"] = "running"
                    report["model"] = __import__("__main__").LIVE_MODEL
                    report["session_id"] = sharing["session_id"]
                    check("visible_live_indicator", ui.operator.live_status.isVisible() and "LIVE" in ui.operator.live_status.text())
                    scene(1)
                return
            if time.time() - state["at"] > 100:
                finish("Timed out during " + phase)
                return
            if sharing["state"] == "ERROR":
                finish(sharing.get("error", "Sharing error"))
                return
            if phase in {"scene1", "scene2"}:
                if (sharing.get("last_stream_frame_at") or 0) >= state["at"]:
                    question("Describe the current screen briefly. Read the visible verification word and number and identify the panel color. Use the live visual context; do not guess.", "answer" + phase[-1])
            elif phase in {"answer1", "answer2"}:
                task = completed()
                if task:
                    answer = task["detail"]
                    tools = [item["name"] for item in report["tools"] if item["task_id"] == task["id"]]
                    correct = state["expected"].replace(" ", "").lower() in answer.replace(" ", "").lower()
                    check("fresh_screen_question_" + phase[-1], task["state"] == "succeeded" and correct,
                          task_id=task["id"], expected=state["expected"], answer=answer, tools=tools,
                          native_live_video="screen_process" not in tools)
                    if phase == "answer1":
                        scene(2)
                    else:
                        question("On the CHIDVI Live Sharing Check window, click the Toggle marker button once. This is an isolated reversible test button. Do not click Stop. Verify whether Marker changed to ON.", "action")
            elif phase == "action":
                task = completed()
                if task:
                    check("safe_desktop_action", state["clicked"] and task["state"] == "succeeded",
                          task_id=task["id"], answer=task["detail"])
                    ui.operator._dispatch_live("pause")
                    state.update(phase="pause_drain", at=time.time())
            elif phase == "pause_drain" and not sharing["active_requests"]:
                state.update(phase="paused", at=time.time(), counts=(sharing["frames_captured"], sharing["frames_sent"]))
            elif phase == "paused" and time.time() - state["at"] >= 3:
                check("pause_no_capture_or_transmission", sharing["state"] == "PAUSED" and state["counts"] == (sharing["frames_captured"], sharing["frames_sent"]))
                ui.operator._dispatch_live("resume")
                state.update(phase="resuming", at=time.time(), sent=sharing["frames_sent"])
            elif phase == "resuming" and sharing["state"] == "LIVE" and sharing["frames_sent"] > state["sent"]:
                check("resume_fresh_transmission", True)
                ui.operator._dispatch_live("stop")
                state.update(phase="stop_drain", at=time.time())
            elif phase == "stop_drain" and sharing["state"] == "OFF" and not sharing["active_requests"]:
                state.update(phase="stopped", at=time.time(), counts=(sharing["frames_captured"], sharing["frames_sent"]))
            elif phase == "stopped" and time.time() - state["at"] >= 3:
                check("stop_no_capture_or_transmission", sharing["state"] == "OFF" and state["counts"] == (sharing["frames_captured"], sharing["frames_sent"]) and sharing["last_frame_at"] is None)
                check("no_per_image_reviews", report["image_reviews"] == 0)
                finish()
        except Exception as exc:
            finish("Check failed: " + type(exc).__name__)

    timer.timeout.connect(tick)
    timer.start(250)
    ui._app.aboutToQuit.connect(lambda: finish("Application closed before check finished") if not state["finished"] else None)
    ui._win._live_acceptance_timer = timer
    save()
    ui.write_log("SYS: Live check ready. Start Live Screen Sharing in the toolbar and accept its session disclosure. The check then displays two harmless test windows and asks ordinary screen questions plus one reversible button click. Pause, Resume and Stop are verified automatically.")
