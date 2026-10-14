"""Vision tests use synthetic images, temporary settings and a mocked provider."""
import importlib
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from google import genai
from PIL import Image

from core.desktop_control import DesktopControl
from core.task_control import TaskStore
from memory import config_manager


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("core.vision"),
                             "Screen interpretation needs an independent structured vision service")
        self.vision = importlib.import_module("core.vision")
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "settings.json"
        patcher = patch.multiple(config_manager, CONFIG_FILE=self.config, CONFIG_DIR=self.config.parent)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = {"gemini_api_key": "private-test-key", "cloud_vision_enabled": False}
        self.save_settings()
        self.frame = self.make_frame()
        self.review = Mock(side_effect=self.approve)
        self.service = DesktopControl()
        self.service.cloud_vision_enabled = True
        self.service.bind_image_review(self.review)
        service_patch = patch.object(self.vision, "get_service", return_value=self.service)
        service_patch.start()
        self.addCleanup(service_patch.stop)
        self.client = Mock()
        self.client.models.generate_content.return_value = SimpleNamespace(text="A browser displays a project README.")
        provider_patch = patch.object(genai, "Client", return_value=self.client)
        self.provider = provider_patch.start()
        self.addCleanup(provider_patch.stop)

    @staticmethod
    def make_frame(format="PNG"):
        image = Image.new("RGB", (16, 10), "black")
        image.putpixel((2, 3), (255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, format=format)
        return {"data": buffer.getvalue(), "mime_type": "image/png" if format == "PNG" else "image/jpeg",
                "width": 16, "height": 10, "monitor": 2,
                "source_rect": {"left": -16, "top": 0, "width": 16, "height": 10}}

    def save_settings(self, **changes):
        self.settings.update(changes)
        self.config.write_text(json.dumps(self.settings), encoding="utf-8")

    def analyze(self, frame=None, question="What is visible?", **kwargs):
        return self.vision.analyze_capture(self.frame if frame is None else frame, question, **kwargs)

    def assert_no_upload(self):
        self.client.models.generate_content.assert_not_called()

    @staticmethod
    def approve(request):
        request.mark_visible()
        request.resolve("approved")

    def test_capture_only_returns_metadata_without_settings_review_or_provider(self):
        self.frame["private_extra"] = "do not return this"
        with patch.object(config_manager, "get_cloud_vision_enabled", side_effect=AssertionError("No analysis requested")):
            result = self.analyze(analyze=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["captured"])
        self.assertFalse(result["analyzed"])
        self.assertEqual(result["image"], {"mime_type": "image/png", "width": 16, "height": 10,
                                         "monitor": 2, "source_rect": self.frame["source_rect"]})
        encoded = json.dumps(result)
        self.assertNotIn("private_extra", encoded)
        self.assertNotIn("description", result)
        self.review.assert_not_called()
        self.provider.assert_not_called()

    def test_disabled_cloud_reports_capture_and_exact_real_settings_path(self):
        self.save_settings(local_vision_model="an-uninstalled-vision-model", llm_provider="ollama")
        result = self.analyze()
        self.assertFalse(result["ok"])
        self.assertTrue(result["captured"])
        self.assertFalse(result["analyzed"])
        self.assertEqual(result["code"], "backend_unavailable")
        self.assertEqual(result["next_action"], "Customize -> Vision & Privacy -> Cloud Vision")
        self.assertEqual(result["next_action"], self.vision.SETTINGS_PATH)
        self.assertFalse(result["local_backend"]["available"])
        self.assertNotIn("description", result)
        self.review.assert_not_called()
        self.provider.assert_not_called()

    def test_cloud_uses_exact_reviewed_pixels_and_actual_caption(self):
        self.save_settings(cloud_vision_enabled=True)
        result = self.analyze(question="Describe the open application")
        self.assertTrue(result["ok"])
        self.assertTrue(result["captured"])
        self.assertTrue(result["analyzed"])
        self.assertEqual(result["description"], "A browser displays a project README.")
        self.review.assert_called_once()
        reviewed = self.review.call_args.args[0]
        self.assertEqual(reviewed.data, self.frame["data"])
        self.assertEqual(reviewed.question, "Describe the open application")
        self.assertEqual(result["approval"]["sha256"], reviewed.sha256)
        request = self.client.models.generate_content.call_args.kwargs
        self.assertEqual(request["model"], "gemini-flash-lite-latest")
        self.assertEqual(request["contents"][0].inline_data.data, self.frame["data"])
        self.assertEqual(request["contents"][0].inline_data.mime_type, "image/png")
        self.assertIn("Describe the open application", request["contents"])
        instructions = request["config"].system_instruction.lower()
        for boundary in ("visible", "untrusted", "password", "invent"):
            self.assertIn(boundary, instructions)
        options = self.provider.call_args.kwargs["http_options"]
        self.assertEqual(options.timeout, 45_000)
        self.assertEqual(options.retry_options.attempts, 1)
        self.client.close.assert_called_once_with()
        self.assertNotIn("private-test-key", json.dumps(result))

    def test_configured_model_is_used_and_camera_jpeg_is_supported(self):
        self.save_settings(cloud_vision_enabled=True, vision_model="configured-vision-model")
        camera = self.make_frame("JPEG")
        del camera["source_rect"], camera["monitor"]
        result = self.analyze(camera)
        self.assertTrue(result["analyzed"])
        self.assertEqual(result["image"], {"mime_type": "image/jpeg", "width": 16, "height": 10})
        request = self.client.models.generate_content.call_args.kwargs
        self.assertEqual(request["model"], "configured-vision-model")
        self.assertEqual(request["contents"][0].inline_data.data, camera["data"])

    def test_invalid_capture_returns_structured_failure_before_review_or_upload(self):
        self.save_settings(cloud_vision_enabled=True)
        bad_frames = [None, {}, {**self.frame, "data": b""}, {**self.frame, "data": b"not an image"},
                      {**self.frame, "width": 0}, {**self.frame, "height": True},
                      {**self.frame, "width": 8}, {**self.frame, "mime_type": "image/jpeg"},
                      {**self.frame, "mime_type": "text/plain"}]
        for frame in bad_frames:
            with self.subTest(frame_type=type(frame).__name__):
                result = self.vision.analyze_capture(frame, "Describe")
                self.assertFalse(result["ok"])
                self.assertFalse(result["captured"])
                self.assertFalse(result["analyzed"])
                self.assertEqual(result["code"], "invalid_capture")
        self.review.assert_not_called()
        self.provider.assert_not_called()

    def test_invalid_question_never_reaches_image_review_or_provider(self):
        self.save_settings(cloud_vision_enabled=True)
        for question in ("", "  ", None, "x" * 8001):
            with self.subTest(question_type=type(question).__name__):
                result = self.analyze(question=question)
                self.assertEqual(result["code"], "invalid_request")
                self.assertFalse(result["analyzed"])
        self.review.assert_not_called()
        self.provider.assert_not_called()

    def test_missing_provider_key_reports_configuration_failure(self):
        self.save_settings(cloud_vision_enabled=True, gemini_api_key="")
        result = self.analyze()
        self.assertEqual(result["code"], "provider_unconfigured")
        self.assertTrue(result["captured"])
        self.assertFalse(result["analyzed"])
        self.review.assert_not_called()
        self.provider.assert_not_called()

    def test_review_decline_or_missing_reviewer_prevents_provider_creation(self):
        self.save_settings(cloud_vision_enabled=True)
        for callback, code in ((None, "image_review_unavailable"),
                               (lambda request: request.resolve("rejected"), "image_review_rejected")):
            self.service.bind_image_review(callback)
            result = self.analyze()
            self.assertEqual(result["code"], code)
            self.assertFalse(result["analyzed"])
        self.provider.assert_not_called()

    def test_saved_consent_revoked_during_review_prevents_upload(self):
        self.save_settings(cloud_vision_enabled=True)
        self.review.side_effect = lambda request: self.approve(request) or self.save_settings(cloud_vision_enabled=False)
        result = self.analyze()
        self.assertEqual(result["code"], "permission_revoked")
        self.assertFalse(result["analyzed"])
        self.provider.assert_not_called()

    def test_consent_revoked_during_client_creation_is_checked_at_upload_boundary(self):
        self.save_settings(cloud_vision_enabled=True)
        self.provider.side_effect = lambda **_: self.save_settings(cloud_vision_enabled=False) or self.client
        result = self.analyze()
        self.assertEqual(result["code"], "permission_revoked")
        self.assert_no_upload()
        self.client.close.assert_called_once_with()

    def test_service_permission_revoked_before_upload_is_also_honored(self):
        self.save_settings(cloud_vision_enabled=True)
        def client(**_):
            self.service.cloud_vision_enabled = False
            return self.client
        self.provider.side_effect = client
        result = self.analyze()
        self.assertEqual(result["code"], "permission_revoked")
        self.assert_no_upload()
        self.client.close.assert_called_once_with()

    def test_cancelled_origin_after_active_task_switch_cannot_upload(self):
        self.save_settings(cloud_vision_enabled=True)
        tasks = TaskStore()
        original = tasks.submit("screen", "original")
        newer = tasks.submit("next", "newer")
        tasks.claim(original["id"])
        def reviewed(request):
            request.mark_visible()
            tasks.cancel(original["id"])
            tasks.release(original["id"])
            tasks.claim(newer["id"])
            request.resolve("approved")
        self.review.side_effect = reviewed
        with tasks.context(original["id"]):
            result = self.analyze()
        self.assertEqual(result["code"], "cancelled")
        self.assertEqual(tasks.get(newer["id"])["state"], "planning")
        self.provider.assert_not_called()

    def test_completed_origin_cannot_approve_for_a_new_task(self):
        self.save_settings(cloud_vision_enabled=True)
        tasks = TaskStore()
        original = tasks.submit("screen", "original")
        newer = tasks.submit("next", "newer")
        tasks.claim(original["id"])
        def reviewed(request):
            request.mark_visible()
            tasks.update(original["id"], "succeeded")
            tasks.release(original["id"])
            tasks.claim(newer["id"])
            request.resolve("approved")
        self.review.side_effect = reviewed
        with tasks.context(original["id"]):
            result = self.analyze()
        self.assertEqual(result["code"], "cancelled")
        self.provider.assert_not_called()

    def test_cancel_reject_and_expire_never_create_provider(self):
        self.save_settings(cloud_vision_enabled=True)
        for state in ("cancelled", "rejected", "expired"):
            with self.subTest(state=state):
                self.review.side_effect = lambda request: request.resolve(state)
                result = self.analyze()
                self.assertEqual(result["code"], "image_review_" + state)
        self.provider.assert_not_called()

    def test_preview_exception_reports_failure_instead_of_imaginary_pending_preview(self):
        self.save_settings(cloud_vision_enabled=True)
        self.review.side_effect = RuntimeError("private-test-key")
        result = self.analyze()
        self.assertEqual(result["code"], "image_review_failed")
        self.assertNotIn("private-test-key", json.dumps(result))
        self.provider.assert_not_called()

    def test_caller_validity_rechecked_at_upload_boundary(self):
        self.save_settings(cloud_vision_enabled=True)
        active = [True]
        self.provider.side_effect = lambda **_: active.__setitem__(0, False) or self.client
        result = self.analyze(valid=lambda: active[0])
        self.assertEqual(result["code"], "cancelled")
        self.assert_no_upload()

    def test_empty_provider_response_is_failure_and_client_closes(self):
        self.save_settings(cloud_vision_enabled=True)
        self.client.models.generate_content.return_value = SimpleNamespace(text="  ")
        result = self.analyze()
        self.assertEqual(result["code"], "empty_response")
        self.assertFalse(result["ok"])
        self.assertFalse(result["analyzed"])
        self.assertNotIn("description", result)
        self.client.close.assert_called_once_with()

    def test_provider_exception_is_structured_without_raw_secrets_and_client_closes(self):
        self.save_settings(cloud_vision_enabled=True)
        self.client.models.generate_content.side_effect = RuntimeError("private-test-key raw-image-payload")
        result = self.analyze()
        self.assertEqual(result["code"], "provider_failed")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertTrue(result["captured"])
        self.assertFalse(result["analyzed"])
        self.assertNotIn("private-test-key", json.dumps(result))
        self.assertNotIn("raw-image-payload", json.dumps(result))
        self.client.close.assert_called_once_with()

    def test_cancelled_during_provider_call_discards_late_description(self):
        self.save_settings(cloud_vision_enabled=True)
        tasks = TaskStore()
        task = tasks.submit("screen", "cancel-during-analysis")
        tasks.claim(task["id"])
        def response(**_):
            tasks.cancel(task["id"])
            return SimpleNamespace(text="Late description")
        self.client.models.generate_content.side_effect = response
        with tasks.context(task["id"]):
            result = self.analyze()
        self.assertEqual(result["code"], "cancelled")
        self.assertNotIn("description", result)
        self.client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
