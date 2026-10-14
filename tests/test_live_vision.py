"""Real lease/vision integration with synthetic frames and a mocked provider."""
import ast
import asyncio
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from core import vision
from core.desktop_control import DesktopControl
from core.screen_sharing import LiveScreenSharing
from core.task_control import TaskStore


def image(color="blue"):
    pixels = Image.new("RGB", (40, 30), color)
    pixels.putpixel((0, 0), (255, 255, 255))
    out = io.BytesIO()
    pixels.save(out, "PNG")
    return {"data": out.getvalue(), "mime_type": "image/png", "width": 40, "height": 30,
            "monitor": 1, "source_rect": {"left": -40, "top": 0, "width": 40, "height": 30}}


class Fixture:
    def setUp(self):
        super().setUp()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config_file = Path(directory.name) / "settings.json"
        config_file.write_text(json.dumps({"gemini_api_key": "synthetic-key", "cloud_vision_enabled": True}), encoding="utf-8")
        from memory import config_manager
        settings = patch.multiple(config_manager, CONFIG_FILE=config_file, CONFIG_DIR=config_file.parent)
        settings.start()
        self.addCleanup(settings.stop)
        self.now = [100.0]
        self.tasks = TaskStore()
        self.task = self.tasks.submit("Describe the editor", "live-test")
        self.tasks.claim(self.task["id"])
        self.capture = Mock(return_value=image())
        self.sharing = LiveScreenSharing(capture=self.capture,
            observer=lambda: {"window_id": 7, "title": "Editor", "application": "code.exe"},
            cloud_allowed=lambda: True, clock=lambda: self.now[0])
        self.addCleanup(self.sharing.close)
        self.desktop = DesktopControl()
        self.desktop.cloud_vision_enabled = True
        self.review = Mock(side_effect=lambda request: (request.mark_visible(), request.resolve("approved")))
        self.desktop.bind_image_review(self.review)
        self.desktop.observe = Mock(return_value={"locked": False, "sensitive": False})
        self.client = Mock()
        self.client.models.generate_content.return_value = SimpleNamespace(text="A blue editor is visible.")
        for target, options in (
                ("core.screen_sharing.threading.Thread", {"new": Mock()}),
                ("core.screen_sharing.get_screen_sharing", {"return_value": self.sharing}),
                ("core.vision.get_service", {"return_value": self.desktop}),
                ("core.desktop_control.get_service", {"return_value": self.desktop}),
                ("memory.config_manager.get_cloud_vision_enabled", {"return_value": True}),
                ("google.genai.Client", {"return_value": self.client})):
            mocked = patch(target, **options)
            handle = mocked.start()
            self.addCleanup(mocked.stop)
            if target == "google.genai.Client":
                self.provider = handle

    def live_frame(self):
        self.sharing.start()
        with self.tasks.context(self.task["id"]):
            return self.sharing.frame()

    def analyze(self, frame):
        with self.tasks.context(self.task["id"]):
            return vision.analyze_capture(frame, "Describe the editor")


class LiveVisionTests(Fixture, unittest.TestCase):
    def test_live_skips_preview_and_uploads_exact_task_lease_only_once(self):
        frame = self.live_frame()
        self.capture.return_value = image("red")
        self.sharing.frame(force=True)  # A newer local sample must not alter the frozen request.
        result = self.analyze(frame)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sharing"]["task_id"], self.task["id"])
        self.assertEqual(result["sharing"]["frame_id"], frame["frame_id"])
        self.assertEqual(result["sharing"]["captured_at"], frame["captured_at"])
        self.assertEqual(self.client.models.generate_content.call_args.kwargs["contents"][0].inline_data.data, frame["data"])
        self.review.assert_not_called()
        self.client.close.assert_called_once()
        self.assertEqual(self.sharing.snapshot()["analyses"], 1)
        self.assertEqual(self.sharing.snapshot()["active_requests"], 0)
        repeated = self.analyze(frame)
        self.assertFalse(repeated["ok"])
        self.assertEqual(repeated["code"], "sharing_consumed")
        self.assertEqual(self.client.models.generate_content.call_count, 1)

    def test_off_one_shot_still_requires_exact_image_approval(self):
        frame = image()
        result = self.analyze(frame)
        self.assertTrue(result["ok"], result)
        self.assertIn("approval", result)
        self.assertNotIn("sharing", result)
        self.review.assert_called_once()
        self.assertEqual(self.review.call_args.args[0].data, frame["data"])
        self.assertEqual(self.sharing.snapshot()["frames_sent"], 0)

    def test_pause_stop_and_lease_tampering_never_fall_back_to_preview(self):
        for transition in (self.sharing.pause, self.sharing.stop):
            with self.subTest(transition=transition.__name__):
                frame = self.live_frame()
                transition()
                result = self.analyze(frame)
                self.assertEqual(result["code"], "sharing_inactive")
        frame = self.live_frame()
        result = self.analyze({**frame, "data": image("red")["data"]})
        self.assertEqual(result["code"], "sharing_inactive")
        self.review.assert_not_called()
        self.provider.assert_not_called()

    def test_old_task_cannot_use_lease_after_active_task_changes(self):
        frame = self.live_frame()
        self.tasks.update(self.task["id"], "succeeded")
        self.tasks.release(self.task["id"])
        newer = self.tasks.submit("New request", "newer")
        self.tasks.claim(newer["id"])
        result = self.analyze(frame)
        self.assertEqual(result["code"], "cancelled")
        with self.tasks.context(newer["id"]):
            result = vision.analyze_capture(frame, "Describe")
        self.assertEqual(result["code"], "sharing_inactive")
        self.assertEqual(self.tasks.get(newer["id"])["state"], "planning")
        self.provider.assert_not_called()

    def test_rate_limit_does_not_upload_or_change_live_state(self):
        self.sharing.start({"analysis_per_minute": 1})
        first = self.live_frame()
        self.assertTrue(self.analyze(first)["ok"])
        with self.tasks.context(self.task["id"]):
            next_frame = self.sharing.frame()
        result = self.analyze(next_frame)
        self.assertEqual(result["code"], "sharing_rate_limited")
        self.assertEqual(self.sharing.snapshot()["state"], "LIVE")
        self.assertEqual(self.client.models.generate_content.call_count, 1)
        self.assertEqual(self.sharing.snapshot()["active_requests"], 0)

    def test_stop_during_client_setup_prevents_dispatch_and_closes_client(self):
        frame = self.live_frame()
        self.provider.side_effect = lambda **_: self.sharing.stop() and self.client
        result = self.analyze(frame)
        self.assertFalse(result["ok"])
        self.client.models.generate_content.assert_not_called()
        self.client.close.assert_called_once()
        self.assertEqual(self.sharing.snapshot()["state"], "OFF")

    def test_stop_during_provider_discards_late_caption(self):
        frame = self.live_frame()
        def response(**_):
            self.assertEqual(self.sharing.stop()["state"], "STOPPING")
            return SimpleNamespace(text="This response is too late.")
        self.client.models.generate_content.side_effect = response
        result = self.analyze(frame)
        self.assertEqual(result["code"], "sharing_inactive")
        self.assertNotIn("description", result)
        self.assertEqual(self.sharing.snapshot()["state"], "OFF")
        self.assertEqual(self.sharing.snapshot()["analyses"], 0)
        self.client.close.assert_called_once()

    def test_late_provider_exception_after_stop_does_not_resurrect_error(self):
        frame = self.live_frame()
        def response(**_):
            self.sharing.stop()
            raise OSError("synthetic provider failure")
        self.client.models.generate_content.side_effect = response
        result = self.analyze(frame)
        self.assertFalse(result["ok"])
        self.assertNotIn("description", result)
        self.assertEqual(self.sharing.snapshot()["state"], "OFF")
        self.assertEqual(self.sharing.snapshot()["active_requests"], 0)
        self.client.close.assert_called_once()

    def test_stop_at_result_commit_does_not_report_success(self):
        frame = self.live_frame()
        with patch.object(self.sharing, "mark_analyzed", side_effect=lambda *_: self.sharing.stop() and False):
            result = self.analyze(frame)
        self.assertFalse(result["ok"], "A result rejected by the sharing session must not be reported as successful")
        self.assertNotIn("description", result)
        self.assertEqual(self.sharing.snapshot()["state"], "OFF")


def dispatcher():
    path = Path(__file__).resolve().parents[1] / "main.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    node = next(item for item in module.body if isinstance(item, ast.ClassDef) and item.name == "JarvisLive")
    node.body = [item for item in node.body if getattr(item, "name", None) == "_process_vision"]
    scope = {"asyncio": asyncio}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), scope)
    return scope["JarvisLive"]()


class LiveDispatchTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def runtime(self):
        runtime = dispatcher()
        runtime._tasks = self.tasks
        runtime._vision_busy = False
        runtime._vision_notices = set()
        runtime._vision_permission_generation = 0
        runtime.ui = SimpleNamespace(write_log=Mock())
        runtime._run_action_worker = AsyncMock(side_effect=lambda function: function())
        return runtime

    async def test_live_dispatch_uses_cached_shared_frame_with_request_task(self):
        self.live_frame()
        runtime = self.runtime()
        with self.tasks.context(self.task["id"]), patch("actions.screen_processor.capture_screen") as fallback:
            result = await runtime._process_vision({"text": "Describe"})
        self.assertTrue(result["ok"], result)
        self.capture.assert_called_once_with(monitor=1)
        fallback.assert_not_called()
        self.review.assert_not_called()
        self.assertEqual(result["sharing"]["task_id"], self.task["id"])
        self.assertFalse(runtime._vision_busy)

    async def test_non_off_pending_states_never_fall_back_to_one_shot(self):
        for state in ("STARTING", "PAUSED", "STOPPING", "ERROR"):
            runtime = self.runtime()
            with self.subTest(state=state), patch.object(self.sharing, "snapshot", return_value={"state": state}), \
                    patch("actions.screen_processor.capture_screen") as fallback:
                result = await runtime._process_vision({"text": "Describe"})
            self.assertEqual(result["code"], "sharing_inactive")
            fallback.assert_not_called()
            self.assertFalse(runtime._vision_busy)
        self.provider.assert_not_called()
        self.review.assert_not_called()

    async def test_off_dispatch_keeps_existing_explicit_preview_path(self):
        runtime = self.runtime()
        with self.tasks.context(self.task["id"]), patch("actions.screen_processor.capture_screen", return_value=image()) as fallback:
            result = await runtime._process_vision({"text": "Describe"})
        self.assertTrue(result["ok"], result)
        fallback.assert_called_once_with(monitor=1)
        self.review.assert_called_once()
        self.assertIn("approval", result)


if __name__ == "__main__":
    unittest.main()
