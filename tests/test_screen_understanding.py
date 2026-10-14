"""Exercise actual dispatch without starting Gemini, audio or the desktop app."""
import ast
import asyncio
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from core.task_control import TaskStore
from core.desktop_control import DesktopControl
from memory import config_manager


tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "JarvisLive")
names = {"_execute_tool_for_task", "_process_vision"}
node.body = [n for n in node.body if getattr(n, "name", None) in names]
scope = {"asyncio": asyncio, "types": SimpleNamespace(FunctionResponse=lambda **kw: SimpleNamespace(**kw)),
         "traceback": SimpleNamespace(print_exc=Mock(), format_exc=lambda: "test traceback"), "print": Mock()}
exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), scope)


class Runtime(scope["JarvisLive"]):
    def __init__(self):
        self._tasks = TaskStore()
        self._tasks.submit("look at screen", "test")
        self._tasks.claim()
        self._allow_cloud_vision = False
        self._vision_permission_generation = 0
        self._vision_busy = False
        self._vision_notices = set()
        self.ui = SimpleNamespace(muted=True, set_state=Mock(), write_log=Mock(), stop_camera_stream=Mock())
        self.speak_error = Mock()
        self._run_action_worker = AsyncMock(side_effect=lambda function: function())


class ScreenDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config_file = Path(temporary.name) / "api_keys.json"
        self.config_file.write_text("{}", encoding="utf-8")
        for name, value in (("CONFIG_FILE", self.config_file), ("CONFIG_DIR", self.config_file.parent)):
            mocked = patch.object(config_manager, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def frame(self):
        png = io.BytesIO()
        image = Image.new("RGB", (32, 24), "blue")
        image.putpixel((0, 0), (255, 0, 0))
        image.save(png, "PNG")
        return {"data": png.getvalue(), "mime_type": "image/png", "width": 32, "height": 24,
                "monitor": 1, "source_rect": {"left": 0, "top": 0, "width": 32, "height": 24}}

    def observer(self):
        service = DesktopControl()
        mock = patch("core.desktop_control.get_service", return_value=service)
        mock.start()
        self.addCleanup(mock.stop)
        service.observe = Mock(return_value={"locked": False, "sensitive": False})
        service.cloud_vision_enabled = True
        def approve(request):
            request.mark_visible()
            request.resolve("approved")
        service.bind_image_review(approve)
        vision_mock = patch("core.vision.get_service", return_value=service)
        vision_mock.start()
        self.addCleanup(vision_mock.stop)
        return service

    async def call(self, runtime, **args):
        return (await runtime._execute_tool_for_task(SimpleNamespace(
            name="screen_process", id="tool", args=args))).response["result"]

    async def test_explicit_local_capture_succeeds_with_cloud_disabled(self):
        runtime = Runtime()
        self.observer()
        with patch("actions.screen_processor.capture_screen", return_value=self.frame()) as capture:
            result = await self.call(runtime, action="capture", text="capture")
        self.assertIsInstance(result, dict)
        self.assertTrue(result["ok"])
        self.assertTrue(result["captured"])
        self.assertFalse(result["analyzed"])
        self.assertEqual(result["image"]["width"], 32)
        capture.assert_called_once_with(monitor=1)
        runtime.speak_error.assert_not_called()

    async def test_disabled_backend_returns_actual_next_action_without_content_or_upload(self):
        runtime = Runtime()
        self.observer()
        with patch("actions.screen_processor.capture_screen", return_value=self.frame()), patch("google.genai.Client") as client:
            result = await self.call(runtime, text="look at the screen")
        self.assertFalse(result["ok"])
        self.assertTrue(result["captured"])
        self.assertFalse(result["analyzed"])
        self.assertFalse(result["local_backend"]["available"])
        self.assertEqual(result["code"], "backend_unavailable")
        self.assertEqual(result["next_action"], "Customize -> Vision & Privacy -> Cloud Vision")
        self.assertNotIn("description", result)
        self.assertEqual(runtime._tasks.get(runtime._tasks.active_id)["state"], "failed")
        client.assert_not_called()
        runtime.speak_error.assert_not_called()

    async def test_retries_do_not_repeat_the_same_disabled_notice(self):
        runtime = Runtime()
        self.observer()
        with patch("actions.screen_processor.capture_screen", return_value=self.frame()):
            first = await runtime._process_vision({"text": "look"})
            second = await runtime._process_vision({"text": "look again"})
        self.assertEqual(first["code"], second["code"])
        self.assertEqual(runtime.ui.write_log.call_count, 1)

    async def test_capture_failure_is_structured_with_actual_reason(self):
        runtime = Runtime()
        self.observer()
        with patch("actions.screen_processor.capture_screen", side_effect=RuntimeError("display unavailable")):
            result = await self.call(runtime, text="look")
        self.assertFalse(result["captured"])
        self.assertEqual(result["code"], "capture_failed")
        self.assertIn("display unavailable", result["message"])
        self.assertFalse(runtime._vision_busy)

    async def test_real_capture_errors_remain_visible_on_subsequent_requests(self):
        runtime = Runtime()
        self.observer()
        with patch("actions.screen_processor.capture_screen", side_effect=[
                RuntimeError("display unavailable"), RuntimeError("incomplete pixels")]):
            await runtime._process_vision({"action": "capture"})
            await runtime._process_vision({"action": "capture"})
        messages = [call.args[0] for call in runtime.ui.write_log.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertIn("display unavailable", messages[0])
        self.assertIn("incomplete pixels", messages[1])

    async def test_sensitive_context_prevents_capture_and_interpretation(self):
        runtime = Runtime()
        self.observer().observe.return_value = {"locked": False, "sensitive": True}
        with patch("actions.screen_processor.capture_screen") as capture:
            result = await self.call(runtime, text="look")
        self.assertEqual(result["code"], "sensitive_context")
        self.assertFalse(result["captured"])
        capture.assert_not_called()

    async def test_successful_analysis_returns_grounded_result_in_same_tool_response(self):
        runtime = Runtime()
        self.config_file.write_text(json.dumps({"cloud_vision_enabled": True, "gemini_api_key": "test-key"}), encoding="utf-8")
        self.observer()
        with patch("actions.screen_processor.capture_screen", return_value=self.frame()), patch("google.genai.Client") as client:
            client.return_value.models.generate_content.return_value = SimpleNamespace(text="A blue image with a red pixel in its top left.")
            result = await self.call(runtime, text="describe this image")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["analyzed"])
        self.assertEqual(result["description"], "A blue image with a red pixel in its top left.")
        self.assertNotIn("data", result["image"])
        self.assertFalse(runtime._vision_busy)
        self.assertEqual(client.return_value.models.generate_content.call_count, 1)

    async def test_duplicate_request_waits_without_cancelling_the_original_task(self):
        runtime = Runtime()
        runtime._vision_busy = True
        with patch("actions.screen_processor.capture_screen") as capture:
            first = await self.call(runtime, text="look")
            second = await self.call(runtime, text="look")
        self.assertEqual(first["code"], "image_review_pending")
        self.assertEqual(second["code"], first["code"])
        self.assertEqual(runtime._tasks.get(runtime._tasks.active_id)["state"], "waiting_for_user_input")
        self.assertFalse(runtime._tasks.cancelled())
        capture.assert_not_called()
        runtime.ui.write_log.assert_not_called()

    async def test_old_preview_result_does_not_change_the_new_task(self):
        runtime = Runtime()
        original = runtime._tasks.active_id
        newer = runtime._tasks.submit("Another request", "another")
        async def switched(_):
            runtime._tasks.update(original, "succeeded")
            runtime._tasks.release(original)
            runtime._tasks.claim(newer["id"])
            return {"ok": False, "code": "image_review_cancelled"}
        runtime._process_vision = switched
        with runtime._tasks.context(original):
            result = await self.call(runtime, text="look")
        self.assertEqual(result["code"], "image_review_cancelled")
        self.assertEqual(runtime._tasks.get(newer["id"])["state"], "planning")

    async def test_repeated_preview_failure_has_one_activity_notice(self):
        runtime = Runtime()
        self.config_file.write_text(json.dumps({"cloud_vision_enabled": True, "gemini_api_key": "test-key"}), encoding="utf-8")
        self.observer().bind_image_review(None)
        with patch("actions.screen_processor.capture_screen", return_value=self.frame()), patch("google.genai.Client") as client:
            first = await runtime._process_vision({"text": "look"})
            second = await runtime._process_vision({"text": "look"})
        self.assertEqual(first["code"], "image_review_unavailable")
        self.assertEqual(second["code"], first["code"])
        self.assertEqual(runtime.ui.write_log.call_count, 1)
        client.assert_not_called()

    async def test_malformed_action_is_a_structured_failure(self):
        result = await self.call(Runtime(), action=[], text="look")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["code"], "invalid_vision_request")
