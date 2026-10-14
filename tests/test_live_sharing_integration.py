"""Runtime sharing hooks without a UI, device capture, or provider connection."""
import ast
import asyncio
from contextlib import suppress
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from core.screen_sharing import LiveScreenSharing
from core.task_control import TaskStore


def runtime_class():
    path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "JarvisLive")
    methods = {"_sharing_context", "_control_screen_sharing", "_start_saved_sharing",
               "_process_dashboard_commands", "_cancel_task", "interrupt", "_set_cloud_vision"}
    node.body = [item for item in node.body if getattr(item, "name", None) in methods]
    scope = {"asyncio": asyncio, "json": json, "confirm_gate": SimpleNamespace(resolve=Mock())}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), scope)
    return scope["JarvisLive"]


class LiveSharingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.runtime = runtime_class()()
        self.runtime._tasks = TaskStore()
        pixels = Image.new("RGB", (40, 30), "blue")
        pixels.putpixel((0, 0), (255, 255, 255))
        output = io.BytesIO()
        pixels.save(output, "PNG")
        self.frame = {"data": output.getvalue(), "mime_type": "image/png", "width": 40,
                      "height": 30, "monitor": 1,
                      "source_rect": {"left": 0, "top": 0, "width": 40, "height": 30}}
        self.capture = Mock(return_value=self.frame)
        self.sharing = LiveScreenSharing(capture=self.capture,
            observer=lambda: {"window_id": 7, "title": "Editor"}, cloud_allowed=lambda: True)
        self.addCleanup(self.sharing.close)
        mock_worker = patch.object(self.sharing, "_spawn_locked", Mock())
        mock_worker.start()
        self.addCleanup(mock_worker.stop)
        runtime = self.runtime
        runtime._screen_sharing = self.sharing
        runtime.session = SimpleNamespace(send_client_content=AsyncMock())
        runtime.ui = SimpleNamespace(write_log=Mock(), stop_camera_stream=Mock())
        runtime._task_done, runtime._task_wakeup, runtime._model_idle = asyncio.Event(), asyncio.Event(), asyncio.Event()
        runtime._model_idle.set()
        runtime._game_stop = threading.Event()
        runtime._click_speech = SimpleNamespace(stopped=threading.Event())
        runtime._wake_enabled, runtime._awake = False, True
        runtime._reference_voice, runtime._reference_turn_open = None, False
        runtime._discard_queued_audio = Mock()
        runtime._repair = None
        runtime._vision_busy = False
        runtime._vision_permission_generation = 0
        runtime._vision_notices = set()
        runtime._loop = None
        runtime.request_reconnect = Mock()

    def task(self, text="Look at the screen", request_id="first", claim=True):
        task = self.runtime._tasks.submit(text, request_id)
        if claim:
            self.runtime._tasks.claim(task["id"])
        return task

    def start(self):
        self.sharing.start()
        return self.sharing.frame()

    async def test_context_recognizes_already_streamed_pixels_within_task_origin(self):
        unscoped = self.start()
        with self.sharing.transmission(unscoped, kind="stream"):
            self.sharing.mark_sent(unscoped)
        task = self.task()
        # Allow executor startup under the app/full-suite CPU load, but stay
        # below the runtime's six-second wait for genuinely missing video.
        context = await asyncio.wait_for(self.runtime._sharing_context(task["id"]), timeout=3)
        payload = json.loads(context.removeprefix("[SCREEN SHARING STATUS] "))
        self.assertTrue(payload["fresh_video_available"])
        self.assertEqual(payload["session_id"], self.sharing.snapshot()["session_id"])
        self.assertEqual(self.capture.call_count, 2, "Submission must refresh local pixels once")

    async def test_off_or_paused_context_has_no_current_image_and_never_captures(self):
        task = self.task()
        for state in ("OFF", "PAUSED"):
            if state == "PAUSED":
                self.start()
                self.sharing.pause()
                self.capture.reset_mock()
            context = await self.runtime._sharing_context(task["id"])
            payload = json.loads(context.removeprefix("[SCREEN SHARING STATUS] "))
            self.assertFalse(payload["fresh_video_available"])
            self.assertEqual(payload["state"], state)
            self.capture.assert_not_called()

    async def test_cancel_during_freshness_wait_does_not_stall_next_queued_command(self):
        first = self.task(claim=False)
        second = self.task("Next request", "second", claim=False)
        sent = asyncio.Event()
        async def context(task_id):
            if task_id == first["id"]:
                self.runtime._tasks.cancel(task_id)
            return "[SCREEN SHARING STATUS] synthetic"
        async def deliver(**_):
            self.runtime._tasks.update(second["id"], "succeeded")
            self.runtime._task_done.set()
            self.runtime._model_idle.set()
            sent.set()
        self.runtime._sharing_context = context
        self.runtime.session.send_client_content.side_effect = deliver
        processor = asyncio.create_task(self.runtime._process_dashboard_commands())
        try:
            try:
                await asyncio.wait_for(sent.wait(), timeout=1)
            except asyncio.TimeoutError:
                self.fail("Cancellation before model delivery must not leave model_idle cleared and stall the next task")
            self.runtime.session.send_client_content.assert_awaited_once()
            parts = self.runtime.session.send_client_content.call_args.kwargs["turns"]["parts"]
            self.assertEqual(parts[0]["text"], "Next request")
        finally:
            processor.cancel()
            with suppress(asyncio.CancelledError):
                await processor

    async def test_stop_interrupt_cancels_origin_and_invalidates_sharing(self):
        frame = self.start()
        task = self.task()
        self.runtime._cancel_task(task["id"])
        self.assertEqual(self.runtime._tasks.get(task["id"])["state"], "cancelled")
        self.assertEqual(self.sharing.snapshot()["state"], "OFF")
        self.assertFalse(self.sharing.valid(frame))
        self.runtime._discard_queued_audio.assert_called_once()

    async def test_cancelling_queued_task_does_not_stop_another_active_task_sharing(self):
        frame = self.start()
        active = self.task()
        queued = self.task("Queued request", "queued", claim=False)
        self.runtime._cancel_task(queued["id"])
        self.assertTrue(self.sharing.valid(frame))
        self.assertEqual(self.runtime._tasks.active_id, active["id"])
        self.runtime._discard_queued_audio.assert_not_called()

    async def test_model_cannot_start_and_disconnected_desktop_cannot_start(self):
        self.assertFalse(self.runtime._control_screen_sharing("start")["ok"])
        self.runtime.session = None
        self.assertFalse(self.runtime._control_screen_sharing("start", explicit=True)["ok"])
        self.capture.assert_not_called()
        self.assertEqual(self.sharing.snapshot()["state"], "OFF")

    async def test_model_cannot_resume_paused_sharing_without_desktop_authorization(self):
        self.start()
        self.sharing.pause()
        result = self.runtime._control_screen_sharing("resume")
        self.assertFalse(result["ok"])
        self.assertEqual(self.sharing.snapshot()["state"], "PAUSED")
        result = self.runtime._control_screen_sharing("resume", explicit=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "STARTING")

    async def test_saved_optin_is_attempted_once_and_never_replayed_after_stop(self):
        with patch("memory.config_manager.get_live_sharing_startup", return_value=True), \
                patch.object(self.runtime, "_control_screen_sharing", return_value={"ok": True}) as start:
            self.runtime._start_saved_sharing()
            self.sharing.stop()
            self.runtime._start_saved_sharing()
        start.assert_called_once_with("start", explicit=True)

    async def test_cloud_disable_stops_sharing_even_without_active_image_tool(self):
        frame = self.start()
        with patch("core.desktop_control.get_service", return_value=SimpleNamespace(cloud_vision_enabled=True)):
            self.runtime._set_cloud_vision(False)
        self.assertEqual(self.sharing.snapshot()["state"], "OFF")
        self.assertFalse(self.sharing.valid(frame))


if __name__ == "__main__":
    unittest.main()
