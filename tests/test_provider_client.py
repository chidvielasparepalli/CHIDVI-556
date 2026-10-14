"""Existing tool clients use the configured role, real privacy gate and shared cancellation."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from core.credential_store import CredentialStore
from core.provider_config import ProviderStore
from core.provider_client import ConfiguredClient, configured_client, sharing_destinations
from core.providers import AIResponse, ProviderError
from core.desktop_control import DesktopControl
from core.task_control import TaskStore
from tests.test_credential_store import FakeProtector


class ProviderClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.store = ProviderStore(folder, vault=CredentialStore(folder / "vault", FakeProtector()))
        self.profile = self.store.save_profile({"id": "chat", "provider": "openai", "model": "gpt-4.1"}, key="synthetic-private-key")
        self.store.set_role("chat", "chat")
        self.store.set_role("vision", "chat")
        mocked = patch("core.provider_config.get_provider_store", return_value=self.store)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.adapter = Mock()
        self.adapter.request = AsyncMock(return_value=AIResponse("A blue window.", [], {"input_tokens": 12}, "gpt-4.1", "stop", {}))
        factory = patch("core.providers.create_adapter", return_value=self.adapter)
        factory.start()
        self.addCleanup(factory.stop)
        self.frame = Image.new("RGB", (20, 15), "blue")
        self.frame.putpixel((0, 0), (255, 255, 255))
        output = io.BytesIO()
        self.frame.save(output, "PNG")
        self.data = output.getvalue()

    def test_existing_text_tool_uses_selected_model_and_json_instruction(self):
        result = configured_client(task_kind="coding").models.generate_content(model="ignored-gemini-name", contents="Plan", config={"response_mime_type": "application/json"})
        self.assertEqual(result.text, "A blue window.")
        self.assertIn("valid JSON", self.adapter.request.call_args.kwargs["system"])
        self.assertEqual(self.store.get_profile("chat")["last_usage"], {"input_tokens": 12})

    def test_missing_role_never_falls_back_to_another_saved_provider(self):
        self.store.set_role("vision", None)
        with self.assertRaisesRegex(ProviderError, "vision"):
            configured_client("vision")
        self.adapter.request.assert_not_called()

    def test_key_rotation_invalidates_prepared_tool_client(self):
        client = configured_client()
        self.store.save_profile({"id": "chat"}, key="synthetic-rotated-key")
        with self.assertRaisesRegex(ProviderError, "changed"):
            client.generate_content(contents="Plan")
        self.adapter.request.assert_not_called()

    def test_cancelled_origin_never_requests_model(self):
        tasks = TaskStore()
        task = tasks.submit("Plan", "client-check")
        tasks.claim(task["id"])
        tasks.cancel(task["id"])
        with tasks.context(task["id"]), self.assertRaisesRegex(ProviderError, "cancelled"):
            configured_client().generate_content(contents="Plan")
        self.adapter.request.assert_not_called()

    def test_grounding_does_not_silently_fall_back_to_gemini(self):
        with self.assertRaisesRegex(ProviderError, "Gemini-specific"):
            configured_client().generate_content(contents="Search", config={"tools": [{"google_search": {}}]})
        self.adapter.request.assert_not_called()

    def test_private_images_require_enabled_cloud_and_exact_review(self):
        with patch("memory.config_manager.get_cloud_vision_enabled", return_value=False):
            with self.assertRaisesRegex(ProviderError, "disabled"):
                configured_client("vision").generate_content(contents=[self.frame, "What is visible?"])
        self.adapter.request.assert_not_called()
        service = DesktopControl()
        service.cloud_vision_enabled = True
        def approve(request):
            self.assertEqual(request.data, self.data)
            self.assertIn("OpenAI", request.provider)
            request.mark_visible()
            request.resolve("approved")
        review = Mock(side_effect=approve)
        service.bind_image_review(review)
        with patch("core.desktop_control.get_service", return_value=service), patch("memory.config_manager.get_cloud_vision_enabled", return_value=True):
            configured_client("vision").generate_content(contents=[self.frame, "What is visible?"])
        review.assert_called_once()
        self.assertEqual(self.adapter.request.call_args.kwargs["images"][0]["data"], self.data)

    def test_vision_service_uses_selected_provider_with_one_review(self):
        from core import vision
        service = DesktopControl()
        service.cloud_vision_enabled = True
        def approve(request):
            self.assertIn("OpenAI", request.provider)
            request.mark_visible()
            request.resolve("approved")
        review = Mock(side_effect=approve)
        service.bind_image_review(review)
        with patch("core.vision.get_service", return_value=service), patch("memory.config_manager.get_cloud_vision_enabled", return_value=True):
            result = vision.analyze_capture({"data": self.data, "mime_type": "image/png", "width": 20, "height": 15}, "Describe")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["backend"], "openai")
        self.assertEqual(result["description"], "A blue window.")
        review.assert_called_once()
        self.adapter.request.assert_awaited_once()

    def test_sharing_disclosure_detects_destination_change_despite_task_snapshot(self):
        before = sharing_destinations()
        with self.store.selection():
            self.store.save_profile({"id": "another", "provider": "anthropic", "model": "claude-sonnet-4-5"}, key="synthetic-other-key")
            self.store.set_role("vision", "another")
            self.assertNotEqual(sharing_destinations(), before)
            self.assertEqual(self.store.selected("vision")["id"], "chat")

    def test_native_gemini_usage_details_are_normalized_without_exposing_keys(self):
        profile = self.store.save_profile({"id": "gemini", "provider": "gemini", "model": "gemini-flash-lite-latest"}, key="synthetic-gemini-key")
        native = Mock()
        native.__enter__ = Mock(return_value=native)
        native.__exit__ = Mock(return_value=False)
        native.models.generate_content.return_value = SimpleNamespace(text="Ready", usage_metadata=SimpleNamespace(model_dump=lambda **_: {"prompt_token_count": 4, "total_token_count": 7, "prompt_tokens_details": [{"token_count": 4}]}))
        with patch("google.genai.Client", return_value=native):
            result = ConfiguredClient(self.store, profile).generate_content(contents="Check")
        self.assertEqual(result.text, "Ready")
        self.assertEqual(self.store.get_profile("gemini")["last_usage"], {"input_tokens": 4, "total_tokens": 7})
        self.assertNotIn("synthetic-gemini-key", json.dumps(self.store.snapshot()))
