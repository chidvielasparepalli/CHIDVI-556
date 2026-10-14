"""Actual orchestration/hooks with synthetic pixels and no desktop/network actions."""
import ast
import asyncio
from concurrent.futures import Future
from contextlib import suppress
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QWidget
from core import confirm
from core.screen_sharing import LiveScreenSharing
from core.task_control import TaskStore
from tests.test_task_runtime import Runtime as TaskRuntime, until


tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "JarvisLive")
methods = {"_control_screen_sharing", "_start_saved_sharing", "_sharing_context", "interrupt"}
node.body = [item for item in node.body if getattr(item, "name", None) in methods]
assert {item.name for item in node.body} == methods
scope = {"asyncio": asyncio, "json": json, "confirm_gate": confirm}
exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), scope)
SharingMethods = scope["JarvisLive"]


class Runtime(TaskRuntime, SharingMethods):
    pass


class LiveSharingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        output = io.BytesIO()
        image = Image.new("RGB", (32, 24), "blue")
        image.putpixel((0, 0), (255, 255, 255))
        image.save(output, "PNG")
        self.frame = {"data": output.getvalue(), "mime_type": "image/png", "width": 32,
                      "height": 24, "monitor": 1,
                      "source_rect": {"left": 0, "top": 0, "width": 32, "height": 24}}
        self.capture = Mock(return_value=self.frame)
        self.sharing = LiveScreenSharing(capture=self.capture,
            observer=lambda: {"window_id": 7, "application": "test.exe"}, cloud_allowed=lambda: True)
        worker = patch.object(self.sharing, "_spawn_locked")
        worker.start()
        self.addCleanup(worker.stop)
        self.addCleanup(self.sharing.close)

    def runtime(self):
        runtime = Runtime()
        runtime._screen_sharing = self.sharing
        return runtime

    def claim(self, runtime):
        task = runtime._submit_task("What is on the current screen?", "sharing-question")
        runtime._tasks.claim(task["id"])
        return task["id"]

    async def test_tools_cannot_start_and_disconnected_ui_cannot_start_or_resume(self):
        runtime = self.runtime()
        self.assertFalse(runtime._control_screen_sharing("start")["ok"])
        runtime.session = None
        self.assertFalse(runtime._control_screen_sharing("start", explicit=True)["ok"])
        self.assertFalse(runtime._control_screen_sharing("resume")["ok"])
        self.assertEqual(self.sharing.snapshot()["state"], "OFF")
        self.capture.assert_not_called()

    async def test_saved_startup_is_explicit_false_by_default_and_attempted_only_once(self):
        from memory import config_manager
        for enabled in (False, True):
            runtime = self.runtime()
            runtime._control_screen_sharing = Mock(return_value={"ok": True})
            with patch.object(config_manager, "get_live_sharing_startup", return_value=enabled):
                runtime._start_saved_sharing()
                runtime._start_saved_sharing()
            self.assertEqual(runtime._control_screen_sharing.call_count, int(enabled))
            if enabled:
                runtime._control_screen_sharing.assert_called_once_with("start", explicit=True)

    async def test_context_reports_transmitted_pixels_under_the_correct_task_identity(self):
        runtime = self.runtime()
        owner = self.claim(runtime)
        self.sharing.start()
        sent = self.sharing.frame()
        with self.sharing.transmission(sent, kind="stream"):
            self.assertTrue(self.sharing.mark_sent(sent))
        text = await runtime._sharing_context(owner)
        context = json.loads(text.split("] ", 1)[1])
        self.assertTrue(context["fresh_video_available"], context)
        self.assertEqual(context["session_id"], self.sharing.snapshot()["session_id"])
        self.assertNotIn("data", context)
        self.assertEqual(runtime._tasks.active_id, owner)

    async def test_http_analysis_upload_alone_is_not_native_live_video(self):
        runtime = self.runtime()
        owner = self.claim(runtime)
        self.sharing.start()
        self.sharing.frame()
        original = self.sharing.frame
        def capture_then_analyze(**kwargs):
            frame = original(**kwargs)
            with self.sharing.transmission(frame, kind="analysis"):
                self.assertTrue(self.sharing.mark_analyzed(frame, "Synthetic description"))
            return frame
        clock = Mock(side_effect=[0, 10])
        proxy = SimpleNamespace(to_thread=asyncio.to_thread, sleep=AsyncMock(),
                                get_running_loop=lambda: SimpleNamespace(time=clock))
        with patch.object(self.sharing, "frame", side_effect=capture_then_analyze), patch.dict(scope, asyncio=proxy):
            text = await runtime._sharing_context(owner)
        context = json.loads(text.split("] ", 1)[1])
        self.assertFalse(context["fresh_video_available"], "HTTP analysis must not claim that video reached the Live session")

    async def test_paused_context_does_not_capture_and_task_completion_keeps_session_paused(self):
        runtime = self.runtime()
        self.sharing.start()
        self.sharing.frame()
        self.sharing.pause()
        self.capture.reset_mock()
        task = runtime._submit_task("Continue talking normally", "paused-turn")
        worker = asyncio.create_task(runtime._process_dashboard_commands())
        try:
            await until(lambda: runtime.session.send_client_content.await_count == 1)
            parts = runtime.session.send_client_content.await_args.kwargs["turns"]["parts"]
            self.assertEqual(parts[0]["text"], "Continue talking normally")
            self.assertIn('"state": "PAUSED"', parts[1]["text"])
            runtime._finish_task(detail="Normal conversational answer")
            await until(lambda: runtime._tasks.active_id is None)
            self.assertEqual(runtime._tasks.get(task["id"])["state"], "succeeded")
            self.assertEqual(self.sharing.snapshot()["state"], "PAUSED")
            self.capture.assert_not_called()
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def test_interrupt_stops_sharing_without_an_active_conversation_task(self):
        runtime = self.runtime()
        self.sharing.start()
        runtime._reference_voice = None
        runtime._reference_turn_open = False
        runtime._discard_queued_audio = Mock()
        SharingMethods.interrupt(runtime)
        self.assertEqual(self.sharing.snapshot()["state"], "OFF")
        runtime._discard_queued_audio.assert_called_once()

    async def test_cloud_revoke_stops_shared_session_and_clears_frames(self):
        runtime = self.runtime()
        runtime._vision_busy = False
        self.sharing.start()
        self.sharing.frame()
        with patch("core.desktop_control.get_service", return_value=SimpleNamespace(cloud_vision_enabled=True)):
            runtime._set_cloud_vision(False)
        self.assertEqual(self.sharing.snapshot()["state"], "OFF")
        self.assertIsNone(self.sharing.snapshot()["last_frame_at"])


class AcceptanceLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import live_sharing_acceptance
        self.module = live_sharing_acceptance
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workspace = Path(folder.name)
        self.window = QWidget()
        self.addCleanup(self.window.close)
        self.status = QLabel("Sharing LIVE", self.window)
        self.window.show()
        self.app.processEvents()
        self.ui = SimpleNamespace(_win=self.window, _app=SimpleNamespace(aboutToQuit=Mock()),
            operator=SimpleNamespace(on_image_status=None, live_status=self.status, _dispatch_live=Mock()), write_log=Mock())
        self.snapshot = {"state": "OFF", "session_id": "fixture-session", "last_sent_at": 10 ** 12,
            "last_stream_frame_at": 10 ** 12,
            "last_frame_at": None, "frames_sent": 0, "frames_captured": 0, "active_requests": 0, "error": ""}
        sharing = Mock(snapshot=lambda: dict(self.snapshot))
        self.jarvis = SimpleNamespace(_screen_sharing=sharing, _tasks=TaskStore(), _loop=Mock(),
                                     _execute_tool=AsyncMock(), session=object(), interrupt=Mock())
        def submit(text, request_id, source):
            task = self.jarvis._tasks.submit(text, request_id, source)
            self.jarvis._tasks.claim(task["id"])
            return task
        self.jarvis._submit_task = submit
        self.timer = Mock()
        timer_patch = patch.object(self.module, "QTimer", return_value=self.timer)
        timer_patch.start()
        self.addCleanup(timer_patch.stop)
        import __main__
        model = patch.object(__main__, "LIVE_MODEL", "synthetic-model", create=True)
        model.start()
        self.addCleanup(model.stop)

    def start(self):
        self.module.start(self.ui, self.jarvis, self.workspace)
        self.tick = self.timer.timeout.connect.call_args.args[0]
        self.quit = self.ui._app.aboutToQuit.connect.call_args.args[0]
        self.addCleanup(self.quit)

    def test_acceptance_waits_for_ui_start_without_changing_user_consent(self):
        self.start()
        self.tick()
        self.jarvis._screen_sharing.start.assert_not_called()
        self.assertEqual(self.jarvis._tasks.snapshot(), [])
        self.assertEqual(json.loads((self.workspace / "logs/live-sharing-acceptance.json").read_text())["status"],
                         "waiting_for_start")

    def test_stop_check_cancels_owned_task_before_hiding_fixture(self):
        self.start()
        self.snapshot["state"] = "LIVE"
        self.tick()  # Show the first synthetic Qt fixture.
        def submitted(coroutine, loop):
            future = Future()
            future.set_result(asyncio.run(coroutine))
            return future
        with patch.object(self.module.asyncio, "run_coroutine_threadsafe", side_effect=submitted):
            self.tick()  # Submit the question, but do not complete it.
        owner = self.jarvis._tasks.active_id
        self.assertTrue(owner)
        buttons = [button for button in self.window.findChildren(QPushButton)
                   if button.text() == "Stop check and sharing"]
        self.assertEqual(len(buttons), 1)
        buttons[0].click()
        self.assertEqual(self.jarvis._tasks.get(owner)["state"], "cancelled")
        self.jarvis._loop.call_soon_threadsafe.assert_called_once_with(self.jarvis.interrupt)
        self.assertFalse(buttons[0].window().isVisible())
        self.timer.stop.assert_called_once()

    def test_stop_cancels_created_task_even_before_submission_future_is_delivered(self):
        self.start()
        self.snapshot["state"] = "LIVE"
        self.tick()
        pending = Future()
        def created_but_not_delivered(coroutine, loop):
            asyncio.run(coroutine)
            return pending
        with patch.object(self.module.asyncio, "run_coroutine_threadsafe", side_effect=created_but_not_delivered):
            self.tick()
        owner = self.jarvis._tasks.active_id
        self.assertTrue(owner)
        self.quit()
        self.assertTrue(pending.cancelled())
        self.assertFalse(self.jarvis._tasks.can_confirm(owner), "An undispatched Future result must not leave an authorized orphan task")

    def test_check_cleanup_does_not_cancel_an_unrelated_later_task(self):
        self.start()
        self.snapshot["state"] = "LIVE"
        self.tick()
        def submitted(coroutine, loop):
            future = Future()
            future.set_result(asyncio.run(coroutine))
            return future
        with patch.object(self.module.asyncio, "run_coroutine_threadsafe", side_effect=submitted):
            self.tick()
        previous = self.jarvis._tasks.active_id
        self.jarvis._tasks.update(previous, "succeeded")
        self.jarvis._tasks.release(previous)
        later = self.jarvis._tasks.submit("Unrelated command", "other-request")
        self.jarvis._tasks.claim(later["id"])
        self.quit()
        self.assertTrue(self.jarvis._tasks.can_confirm(later["id"]))
        self.jarvis._loop.call_soon_threadsafe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
