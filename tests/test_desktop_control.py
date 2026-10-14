"""Input tests use an in-memory backend; no screenshots or real cursor movement."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from core.desktop_control import (CLICK_PHRASE, DesktopControl, DesktopError,
                                  ScreenRect, VisualTarget, map_image_point,
                                  physical_to_logical)


def isolate_provider_configuration(test):
    from memory import config_manager
    directory = TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    folder = Path(directory.name)
    setting = patch.multiple(config_manager, CONFIG_DIR=folder, CONFIG_FILE=folder / "settings.json")
    setting.start()
    test.addCleanup(setting.stop)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        isolate_provider_configuration(self)
        self.position = (100, 100)
        self.backend = Mock()
        self.backend.position.side_effect = lambda: self.position
        self.backend.moveTo.side_effect = lambda x, y, duration=0: setattr(self, "position", (x, y))
        self.observer = Mock(return_value={"window_id": 9, "locked": False})
        self.service = DesktopControl(self.backend, lambda: [ScreenRect(-1920, 0, 1920, 1080), ScreenRect(0, 0, 2560, 1440)], self.observer, lambda: 100)
        self.events = []
        saved_cloud = patch("memory.config_manager.get_cloud_vision_enabled", return_value=False)
        self.saved_cloud = saved_cloud.start()
        self.addCleanup(saved_cloud.stop)
        sharing = patch("core.screen_sharing.get_screen_sharing", return_value=Mock(
            snapshot=Mock(return_value={"state": "OFF"})))
        sharing.start()
        self.addCleanup(sharing.stop)
        self.service.bind_feedback(lambda x, y: self.events.append(("ring", x, y)) or True,
                                   lambda phrase: self.events.append(("speech", phrase)) or True)

    def test_mapping_uses_region_origin_and_scale(self):
        self.assertEqual(map_image_point(640, 360, 1280, 720, ScreenRect(-1920, 100, 1920, 1080)), (-960, 640))
        self.assertEqual(physical_to_logical(-960, 540, ScreenRect(-1920, 0, 1920, 1080), ScreenRect(-1280, 0, 1280, 720)), (-640, 360))

    def test_invalid_and_monitor_gap_coordinates_refused(self):
        for x, y in [(float("nan"), 1), (float("inf"), 1), (True, 1), (0.2, 1), (3000, 0), (-1, 1100)]:
            with self.subTest(x=x, y=y), self.assertRaises(DesktopError):
                self.service.click(x, y)
        self.backend.click.assert_not_called()

    def test_visible_cursor_phrase_ring_then_click(self):
        self.backend.click.side_effect = lambda *a, **k: self.events.append(("click",))
        result = self.service.click(-200, 20)
        self.assertEqual(self.position, (-200, 20))
        self.assertEqual(self.events, [("speech", CLICK_PHRASE), ("ring", -200, 20), ("click",)])
        self.assertTrue(result["ok"])
        self.assertIsNone(result["verified"])

    def test_refuses_missing_or_failed_feedback(self):
        for highlight, speak in [(None, lambda _: True), (lambda *_: True, None), (lambda *_: False, lambda _: True)]:
            self.service.bind_feedback(highlight, speak)
            with self.assertRaises(DesktopError):
                self.service.click(5, 5)
        self.backend.click.assert_not_called()

    def test_window_change_during_speech_refused(self):
        self.observer.side_effect = [{"window_id": 9}, {"window_id": 10}]
        with self.assertRaisesRegex(DesktopError, "window changed"):
            self.service.click(5, 5)
        self.backend.click.assert_not_called()

    def test_cancelled_after_speech_never_clicks(self):
        cancelled = [False]
        def speak(_):
            cancelled[0] = True
            return True
        self.service.bind_feedback(lambda *_: True, speak, lambda: cancelled[0])
        with self.assertRaisesRegex(DesktopError, "cancelled"):
            self.service.click(5, 5)
        self.backend.click.assert_not_called()

    def test_focus_change_during_overlay_never_clicks(self):
        def highlight(*_):
            self.observer.return_value = {"window_id": 10, "locked": False}
            return True
        self.service.bind_feedback(highlight, lambda _: True)
        with self.assertRaisesRegex(DesktopError, "highlighting"):
            self.service.click(5, 5)
        self.backend.click.assert_not_called()

    def test_lock_during_overlay_never_clicks(self):
        def highlight(*_):
            self.observer.return_value = {"window_id": 9, "locked": True}
            return True
        self.service.bind_feedback(highlight, lambda _: True)
        with self.assertRaisesRegex(DesktopError, "locked"):
            self.service.click(5, 5)
        self.backend.click.assert_not_called()

    def test_fresh_target_confidence_and_screen_identity(self):
        good = VisualTarget(5, 5, .95, 99, "same", 9)
        self.assertEqual(self.service.validate_target(good, "same", 9), (5, 5))
        for target, fingerprint, window in [(VisualTarget(5, 5, .5, 99, "same", 9), "same", 9),
                                             (VisualTarget(5, 5, .95, 90, "same", 9), "same", 9),
                                             (good, "changed", 9), (good, "same", 10)]:
            with self.assertRaises(DesktopError):
                self.service.validate_target(target, fingerprint, window)

    def test_preflight_reobserves_after_speech_before_ring(self):
        self.service.click(5, 5, preflight=lambda: self.events.append(("observe",)))
        self.assertEqual(self.events, [("speech", CLICK_PHRASE), ("observe",), ("ring", 5, 5), ("observe",)])

    def test_drag_releases_button_on_failure(self):
        with patch.object(self.service, "move", side_effect=[{}, DesktopError("cancelled")]):
            with self.assertRaises(DesktopError):
                self.service.drag(1, 1, 50, 50)
        self.backend.mouseUp.assert_called_once_with(button="left")

    def test_stop_prevents_remaining_desktop_typing(self):
        cancelled = [False]
        self.backend.typewrite.side_effect = lambda *_, **__: cancelled.__setitem__(0, True)
        self.service.bind_feedback(lambda *_: True, lambda _: True, lambda: cancelled[0])
        with self.assertRaisesRegex(DesktopError, "cancelled"):
            self.service.type_text("abcdefghijklmnop", interval=0)
        self.backend.typewrite.assert_called_once_with("abcdefgh", interval=0)

    def test_focus_change_stops_remaining_desktop_typing(self):
        self.backend.typewrite.side_effect = lambda *_, **__: setattr(self.observer, "return_value", {"window_id": 10})
        with self.assertRaisesRegex(DesktopError, "Window changed"):
            self.service.type_text("abcdefghijklmnop", interval=0)
        self.backend.typewrite.assert_called_once_with("abcdefgh", interval=0)

    def test_clear_field_does_not_delete_in_newly_focused_window(self):
        from actions import computer_control
        def changed(_):
            self.observer.return_value = {"window_id": 10}
        with patch.object(computer_control, "get_service", return_value=self.service), patch.object(computer_control, "pyautogui") as input_api, patch.object(computer_control, "_PYAUTOGUI", True), patch.object(computer_control, "_get_os", return_value="windows"), patch.object(computer_control.time, "sleep", side_effect=changed):
            with self.assertRaisesRegex(DesktopError, "window changed"):
                computer_control._clear_field()
            input_api.hotkey.assert_called_once_with("ctrl", "a")
            input_api.press.assert_not_called()

    def test_locked_no_input(self):
        self.observer.return_value = {"locked": True, "window_id": 9}
        with self.assertRaises(DesktopError):
            self.service.click(5, 5)
        self.backend.click.assert_not_called()

    def test_backend_error_is_not_success(self):
        self.backend.click.side_effect = OSError("device error")
        with self.assertRaises(OSError):
            self.service.click(5, 5)

    def test_cloud_capture_disabled_by_default(self):
        from actions import computer_control
        with patch.object(computer_control, "get_service", return_value=self.service), patch.object(computer_control, "_get_api_key") as key:
            with self.assertRaisesRegex(DesktopError, "disabled"):
                computer_control._screen_find("Search")
            key.assert_not_called()

    def test_missing_identity_does_not_generate_fake_data(self):
        from actions import computer_control
        with patch.object(computer_control, "get_service", return_value=self.service), patch.object(computer_control, "_user_profile", return_value={}), patch.object(computer_control, "_random_data") as random_data:
            result = computer_control.computer_control({"action": "user_data", "field": "email"})
            self.assertIn("Ask the user", result)
            random_data.assert_not_called()

    def test_cloud_permission_revocation_discards_capture_before_upload(self):
        import io
        from PIL import Image
        from google import genai
        from actions import computer_control
        data = io.BytesIO()
        Image.new("RGB", (10, 10)).save(data, format="PNG")
        self.service.cloud_vision_enabled = True
        self.saved_cloud.return_value = True
        def capture(_):
            self.service.cloud_vision_enabled = False
            return data.getvalue(), "fingerprint", 9
        config = {}
        with patch.object(computer_control, "get_service", return_value=self.service), patch.object(computer_control, "_load_config", return_value=config), patch.object(computer_control, "_get_api_key", return_value="test-key"), patch.object(self.service, "observe", return_value={"monitors": [{"left": 0, "top": 0, "width": 10, "height": 10}]}), patch.object(self.service, "capture_region", side_effect=capture), patch.object(genai, "Client") as client:
            with self.assertRaisesRegex(DesktopError, "disabled before upload"):
                computer_control._visual_target("Search")
            client.assert_not_called()

    def test_saved_consent_off_refuses_even_if_runtime_cache_is_true(self):
        from actions import computer_control
        self.service.cloud_vision_enabled = True
        legacy = {"desktop_capture_enabled": True, "desktop_external_vision": True}
        with patch.object(computer_control, "get_service", return_value=self.service), patch.object(computer_control, "_load_config", return_value=legacy), patch.object(computer_control, "_get_api_key", return_value="") as key:
            with self.assertRaisesRegex(DesktopError, "[Cc]loud"):
                computer_control._visual_target("Search")
            key.assert_not_called()

    def test_saved_consent_revoked_during_review_or_preupload_refuses_upload(self):
        import io
        from PIL import Image
        from google import genai
        from actions import computer_control
        buffer = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buffer, format="PNG")
        data = buffer.getvalue()
        self.service.cloud_vision_enabled = True
        legacy = {"desktop_capture_enabled": True, "desktop_external_vision": True}
        for stage in ("review", "recapture", "client"):
            with self.subTest(stage=stage):
                self.saved_cloud.return_value = True
                calls = []
                def review(*_):
                    if stage == "review":
                        self.saved_cloud.return_value = False
                    return True
                def capture(_):
                    calls.append("capture")
                    if stage == "recapture" and len(calls) == 2:
                        self.saved_cloud.return_value = False
                    return data, "unchanged", 9
                model = Mock()
                model.models.generate_content.return_value.text = '{"found": false, "sensitive": false}'
                def make_client(**_):
                    if stage == "client":
                        self.saved_cloud.return_value = False
                    return model
                self.bind_image_review(review)
                with patch.object(computer_control, "get_service", return_value=self.service), patch.object(computer_control, "_load_config", return_value=legacy), patch.object(computer_control, "_get_api_key", return_value="test-key"), patch.object(self.service, "observe", return_value={"monitors": [{"left": 0, "top": 0, "width": 10, "height": 10}]}), patch.object(self.service, "capture_region", side_effect=capture), patch.object(genai, "Client", side_effect=make_client):
                    with self.assertRaisesRegex(DesktopError, "disabled before upload"):
                        computer_control._visual_target("Search")
                model.models.generate_content.assert_not_called()

    def bind_image_review(self, callback):
        binder = getattr(self.service, "bind_image_review", None)
        self.assertTrue(callable(binder), "Desktop image approval gate must be implemented")
        def queued(request):
            request.mark_visible()
            answer = callback(request.data, request.mime)
            request.resolve("approved" if answer is True else "rejected")
        binder(queued if callback is not None else None)

    def test_image_review_refuses_without_bound_reviewer(self):
        self.bind_image_review(None)
        self.service.cloud_vision_enabled = True
        with self.assertRaisesRegex(DesktopError, "review"):
            self.service.review_image(b"exact screenshot bytes")

    def test_image_review_passes_exact_bytes_and_requires_explicit_approval(self):
        data = b"exact captured image, not a recapture"
        review = Mock(return_value=True)
        self.bind_image_review(review)
        self.service.cloud_vision_enabled = True
        receipt = self.service.review_image(data, "image/jpeg")
        self.assertEqual(receipt.status, "approved")
        self.assertTrue(self.service.consume_image_approval(receipt, data, "image/jpeg"))
        review.assert_called_once_with(data, "image/jpeg")
        self.assertIs(review.call_args.args[0], data)

    def test_image_review_refuses_when_cloud_is_disabled(self):
        review = Mock(return_value=True)
        self.bind_image_review(review)
        with self.assertRaises(DesktopError):
            self.service.review_image(b"capture")
        review.assert_not_called()

    def test_image_review_refuses_decline_and_implicit_truthy_approval(self):
        self.service.cloud_vision_enabled = True
        for answer in (False, None, 1, "yes"):
            with self.subTest(answer=answer):
                self.bind_image_review(Mock(return_value=answer))
                with self.assertRaisesRegex(DesktopError, "approved"):
                    self.service.review_image(b"capture")

    def test_image_review_refuses_cancellation_before_and_after_review(self):
        cancelled = [True]
        review = Mock(return_value=True)
        self.bind_image_review(review)
        self.service.cloud_vision_enabled = True
        self.service.bind_feedback(None, None, lambda: cancelled[0])
        with self.assertRaisesRegex(DesktopError, "cancelled"):
            self.service.review_image(b"capture")
        review.assert_not_called()
        cancelled[0] = False
        def cancel_during_review(*_):
            cancelled[0] = True
            return True
        self.bind_image_review(cancel_during_review)
        with self.assertRaisesRegex(DesktopError, "cancelled"):
            self.service.review_image(b"capture")

    def test_image_review_refuses_permission_revoked_during_review(self):
        self.service.cloud_vision_enabled = True
        def revoke_during_review(*_):
            self.service.cloud_vision_enabled = False
            return True
        self.bind_image_review(revoke_during_review)
        with self.assertRaisesRegex(DesktopError, "disabled"):
            self.service.review_image(b"capture")

    def test_visual_target_rechecks_screen_after_review_before_any_upload(self):
        import io
        from PIL import Image
        from google import genai
        from actions import computer_control
        buffer = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buffer, format="PNG")
        data = buffer.getvalue()
        self.service.cloud_vision_enabled = True
        self.saved_cloud.return_value = True
        reviewer = Mock(return_value=True)
        self.bind_image_review(reviewer)
        config = {}
        with patch.object(computer_control, "get_service", return_value=self.service), patch.object(computer_control, "_load_config", return_value=config), patch.object(computer_control, "_get_api_key", return_value="test-key"), patch.object(self.service, "observe", return_value={"monitors": [{"left": 0, "top": 0, "width": 10, "height": 10}]}), patch.object(self.service, "capture_region", side_effect=[(data, "original", 9), (data, "changed", 9)]), patch.object(genai, "Client") as client:
            client.return_value.models.generate_content.return_value.text = '{"found": false, "sensitive": false}'
            with self.assertRaisesRegex(DesktopError, "changed during image review"):
                computer_control._visual_target("Search")
            reviewer.assert_called_once_with(data, "image/png")
            client.assert_not_called()

    def test_visual_target_uploads_exact_reviewed_bytes_after_slow_approval(self):
        import io
        from PIL import Image
        from google import genai
        from actions import computer_control
        buffer = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buffer, format="PNG")
        data, now = buffer.getvalue(), [100.0]
        self.service.cloud_vision_enabled = True
        self.saved_cloud.return_value = True
        self.service._clock = lambda: now[0]
        def approve(*_):
            now[0] += 20
            return True
        reviewer = Mock(side_effect=approve)
        self.bind_image_review(reviewer)
        config = {}
        with patch.object(computer_control, "get_service", return_value=self.service), patch.object(computer_control, "_load_config", return_value=config), patch.object(computer_control, "_get_api_key", return_value="test-key"), patch.object(computer_control.time, "monotonic", side_effect=lambda: now[0]), patch.object(self.service, "observe", return_value={"monitors": [{"left": 0, "top": 0, "width": 10, "height": 10}]}), patch.object(self.service, "capture_region", return_value=(data, "unchanged", 9)), patch.object(genai, "Client") as client:
            client.return_value.models.generate_content.return_value.text = '{"found": true, "sensitive": false, "candidates": 1, "confidence": 0.95, "x": 5, "y": 5}'
            target, _ = computer_control._visual_target("Search")
            self.assertEqual((target.x, target.y, target.observed_at), (5, 5, 120))
            reviewer.assert_called_once_with(data, "image/png")
            uploaded = client.return_value.models.generate_content.call_args.kwargs["contents"][0].inline_data.data
            self.assertEqual(uploaded, data)
            options = client.call_args.kwargs["http_options"]
            self.assertEqual(options.timeout, 45_000)
            self.assertEqual(options.retry_options.attempts, 1)
            client.return_value.close.assert_called_once_with()

    def test_visual_target_discards_provider_response_after_consent_or_task_revocation(self):
        import io
        import time
        from contextlib import ExitStack
        from PIL import Image
        from google import genai
        from actions import computer_control
        from core.task_control import TaskStore
        buffer = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buffer, format="PNG")
        data = buffer.getvalue()
        self.service._clock = time.monotonic
        for revoked in ("runtime_consent", "saved_consent", "completed_task", "released_task"):
            with self.subTest(revoked=revoked), ExitStack() as stack:
                self.service.cloud_vision_enabled = True
                self.saved_cloud.return_value = True
                self.bind_image_review(lambda *_: True)
                tasks = TaskStore()
                task = tasks.submit("Find Search", "original")
                other = tasks.submit("Other task", "other")
                tasks.claim(task["id"])
                self.service.bind_feedback(None, None, tasks.cancelled)
                stack.enter_context(tasks.context(task["id"]))
                stack.enter_context(patch.object(computer_control, "get_service", return_value=self.service))
                stack.enter_context(patch.object(computer_control, "_load_config", return_value={}))
                stack.enter_context(patch.object(computer_control, "_get_api_key", return_value="test-key"))
                stack.enter_context(patch.object(self.service, "observe", return_value={
                    "monitors": [{"left": 0, "top": 0, "width": 10, "height": 10}]}))
                capture = stack.enter_context(patch.object(self.service, "capture_region", return_value=(data, "unchanged", 9)))
                finish = stack.enter_context(patch.object(self.service, "finish_image_review", wraps=self.service.finish_image_review))
                client = stack.enter_context(patch.object(genai, "Client"))
                def late_response(**_):
                    if revoked == "runtime_consent":
                        self.service.cloud_vision_enabled = False
                    elif revoked == "saved_consent":
                        self.saved_cloud.return_value = False
                    else:
                        if revoked == "completed_task":
                            tasks.update(task["id"], "succeeded")
                        tasks.release(task["id"])
                        tasks.claim(other["id"])
                    return Mock(text='{"found": true, "sensitive": false, "candidates": 1, "confidence": 0.95, "x": 5, "y": 5}')
                client.return_value.models.generate_content.side_effect = late_response
                with self.assertRaisesRegex(DesktopError, "disabled|task"):
                    computer_control._visual_target("Search")
                client.return_value.models.generate_content.assert_called_once()
                client.return_value.close.assert_called_once_with()
                self.assertEqual(capture.call_count, 2)
                self.assertEqual(finish.call_args.args[1], "failed")


class LiveDesktopTests(unittest.TestCase):
    def setUp(self):
        isolate_provider_configuration(self)
        import hashlib
        import io
        from PIL import Image
        from google import genai
        from actions import computer_control
        from core.screen_sharing import LiveScreenSharing
        self.control = computer_control
        image = Image.new("RGB", (64, 48), "navy")
        image.putpixel((1, 1), (255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        self.data = buffer.getvalue()
        self.fingerprint = hashlib.sha256(self.data).hexdigest()
        self.rect = {"left": -64, "top": 0, "width": 64, "height": 48}
        self.window = {"window_id": 9, "locked": False, "sensitive": False,
                       "application": "editor.exe", "title": "Synthetic editor"}
        self.capture = Mock(return_value={"data": self.data, "mime_type": "image/png", "width": 64,
            "height": 48, "monitor": 1, "source_rect": self.rect})
        self.now = [100.0]
        self.sharing = LiveScreenSharing(capture=self.capture, observer=lambda: dict(self.window),
                                        cloud_allowed=lambda: True, clock=lambda: self.now[0])
        self.addCleanup(self.sharing.close)
        self.position = (-20, 20)
        self.backend = Mock()
        self.backend.position.side_effect = lambda: self.position
        self.backend.moveTo.side_effect = lambda x, y, duration=0: setattr(self, "position", (x, y))
        self.desktop = DesktopControl(self.backend,
            lambda: [ScreenRect(**self.rect), ScreenRect(0, 0, 64, 48)], lambda: dict(self.window),
            clock=lambda: self.now[0])
        self.desktop.cloud_vision_enabled = True
        self.desktop.bind_feedback(lambda *_: True, lambda _: True)
        self.review = Mock(side_effect=AssertionError("LIVE must not request one-shot approval"))
        self.desktop.bind_image_review(self.review)
        patches = [patch.object(self.sharing, "_spawn_locked"),
            patch("core.screen_sharing.get_screen_sharing", return_value=self.sharing),
            patch("memory.config_manager.get_cloud_vision_enabled", return_value=True),
            patch.object(computer_control, "get_service", return_value=self.desktop),
            patch.object(computer_control, "_get_api_key", return_value="synthetic-key"),
            patch.object(computer_control, "_load_config", return_value={}),
            patch.object(self.desktop, "capture_region", return_value=(self.data, self.fingerprint, 9)),
            patch.object(genai, "Client")]
        handles = []
        for item in patches:
            handles.append(item.start())
            self.addCleanup(item.stop)
        self.client = handles[-1]
        self.client.return_value.models.generate_content.return_value.text = (
            '{"found":true,"sensitive":false,"candidates":1,"confidence":0.95,"x":16,"y":12}')
        self.sharing.start()
        self.sharing.frame()

    def test_live_target_uses_selected_monitor_and_no_one_shot_review(self):
        target, rect = self.control._visual_target("Search")
        self.assertEqual((target.x, target.y), (-48, 12))
        self.assertEqual(rect, ScreenRect(**self.rect))
        self.review.assert_not_called()
        self.assertGreaterEqual(self.capture.call_count, 3)
        self.assertEqual(self.sharing.snapshot()["active_requests"], 0)
        self.assertEqual(self.sharing.snapshot()["frames_sent"], 1)
        self.assertEqual(self.sharing.snapshot()["analyses"], 1)
        uploaded = self.client.return_value.models.generate_content.call_args.kwargs["contents"][0].inline_data.data
        self.assertEqual(uploaded, self.data)

    def test_slow_provider_renews_target_only_from_identical_fresh_capture(self):
        response = self.client.return_value.models.generate_content.return_value
        def slow(**_):
            self.now[0] += 8
            return response
        self.client.return_value.models.generate_content.side_effect = slow
        target, _ = self.control._visual_target("Search")
        self.assertEqual(target.observed_at, 108.0)
        self.assertEqual(target.sharing_frame["_sharing"].captured_mono, 108.0)
        self.desktop.validate_target(target, self.fingerprint, 9)
        self.now[0] += 6
        with self.assertRaisesRegex(DesktopError, "stale"):
            self.desktop.validate_target(target, self.fingerprint, 9)

    def test_slow_speech_and_ring_renew_identical_target_before_click(self):
        import json
        target, rect = self.control._visual_target("Search")
        def speak(_):
            self.now[0] += 6
            return True
        def ring(*_):
            self.now[0] += 2
            return True
        self.desktop.bind_feedback(ring, speak)
        with patch.object(self.control, "_visual_target", return_value=(target, rect)):
            result = self.control.computer_control({"action": "screen_click", "description": "Search"})
        result = json.loads(result)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["verified"])
        self.backend.click.assert_called_once()

    def test_slow_provider_never_renews_a_changed_window_or_geometry(self):
        response = self.client.return_value.models.generate_content.return_value
        def changed(**_):
            self.now[0] += 8
            self.capture.return_value = {**self.capture.return_value,
                "source_rect": {**self.rect, "top": 1}}
            return response
        self.client.return_value.models.generate_content.side_effect = changed
        with self.assertRaisesRegex(DesktopError, "Screen changed"):
            self.control._visual_target("Search")
        self.backend.click.assert_not_called()

    def test_non_live_sharing_never_falls_back_to_one_shot_upload(self):
        for state in ("PAUSED", "STARTING", "STOPPING", "ERROR"):
            with self.subTest(state=state), patch.object(self.sharing, "snapshot", return_value={"state": state}):
                with self.assertRaisesRegex(DesktopError, "sharing"):
                    self.control._visual_target("Search")
        self.review.assert_not_called()
        self.client.assert_not_called()

    def test_late_stop_discards_target_and_drains_control_transmission(self):
        def stop(**_):
            self.assertEqual(self.sharing.snapshot()["active_requests"], 1)
            self.sharing.stop()
            return Mock(text='{"found":true,"sensitive":false,"candidates":1,"confidence":0.95,"x":16,"y":12}')
        self.client.return_value.models.generate_content.side_effect = stop
        with self.assertRaisesRegex(DesktopError, "sharing"):
            self.control._visual_target("Search")
        self.assertEqual(self.sharing.snapshot()["state"], "OFF")
        self.assertEqual(self.sharing.snapshot()["frames_sent"], 0)
        self.client.return_value.close.assert_called_once_with()
        self.backend.click.assert_not_called()

    def test_target_permission_does_not_survive_stop_or_restart(self):
        target, _ = self.control._visual_target("Search")
        self.sharing.stop()
        with self.assertRaisesRegex(DesktopError, "sharing"):
            self.desktop.validate_target(target, self.fingerprint, 9)
        self.sharing.start()
        self.sharing.frame()
        with self.assertRaisesRegex(DesktopError, "sharing"):
            self.desktop.validate_target(target, self.fingerprint, 9)

    def test_pause_during_ring_blocks_raw_coordinate_click(self):
        def pause(*_):
            self.sharing.pause()
            return True
        self.desktop.bind_feedback(pause, lambda _: True)
        with self.assertRaisesRegex(DesktopError, "sharing"):
            self.desktop.click(-20, 20)
        self.backend.click.assert_not_called()

    def test_changed_pixels_during_ring_blocks_screen_click(self):
        import io
        from PIL import Image
        target, rect = self.control._visual_target("Search")
        buffer = io.BytesIO()
        image = Image.open(io.BytesIO(self.data))
        image.putpixel((2, 2), (255, 0, 0))
        image.save(buffer, "PNG")
        def changed(*_):
            self.capture.return_value = {**self.capture.return_value, "data": buffer.getvalue()}
            return True
        self.desktop.bind_feedback(changed, lambda _: True)
        with patch.object(self.control, "_visual_target", return_value=(target, rect)):
            result = self.control.computer_control({"action": "screen_click", "description": "Search"})
        self.assertIn("Screen changed", result)
        self.backend.click.assert_not_called()

    def test_live_target_remains_bound_to_its_requesting_task(self):
        from core.task_control import TaskStore
        tasks = TaskStore()
        task = tasks.submit("Find Search", "origin")
        tasks.claim(task["id"])
        with tasks.context(task["id"]):
            target, _ = self.control._visual_target("Search")
            self.desktop.validate_target(target, self.fingerprint, 9)
        with self.assertRaisesRegex(DesktopError, "sharing"):
            self.desktop.validate_target(target, self.fingerprint, 9)
        with tasks.context(task["id"]):
            tasks.cancel(task["id"])
            with self.assertRaisesRegex(DesktopError, "sharing"):
                self.desktop.validate_target(target, self.fingerprint, 9)

    def test_paused_sharing_blocks_all_coordinate_input_before_dispatch(self):
        self.sharing.pause()
        actions = (lambda: self.desktop.move(-20, 20), lambda: self.desktop.click(-20, 20),
                   lambda: self.desktop.drag(-40, 20, -10, 20), lambda: self.desktop.button(down=True),
                   lambda: self.desktop.scroll())
        self.capture.reset_mock()
        for action in actions:
            with self.subTest(action=action), self.assertRaisesRegex(DesktopError, "sharing"):
                action()
        self.capture.assert_not_called()
        for name in ("moveTo", "click", "mouseDown", "scroll"):
            getattr(self.backend, name).assert_not_called()

    def test_pause_during_drag_releases_owned_button_and_stops_movement(self):
        self.backend.mouseDown.side_effect = lambda **_: self.sharing.pause()
        with self.assertRaisesRegex(DesktopError, "sharing"):
            self.desktop.drag(-40, 20, -10, 20, duration=.1)
        self.backend.mouseDown.assert_called_once_with(button="left")
        self.backend.mouseUp.assert_called_once_with(button="left")
        self.assertEqual(self.backend.moveTo.call_count, 1)

    def test_live_sharing_does_not_authorize_sensitive_visual_controls(self):
        self.client.return_value.models.generate_content.return_value.text = (
            '{"found":true,"sensitive":true,"candidates":1,"confidence":0.95,"x":16,"y":12}')
        with self.assertRaisesRegex(DesktopError, "Sensitive"):
            self.control._visual_target("Search")
        self.backend.click.assert_not_called()

    def test_raw_live_click_refreshes_before_and_after_without_claiming_verification(self):
        self.capture.reset_mock()
        result = self.desktop.click(-20, 20)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["verified"])
        self.assertGreaterEqual(self.capture.call_count, 2)
        self.backend.click.assert_called_once()
        self.client.assert_not_called()

    def test_raw_live_input_refuses_other_monitor_and_sensitive_context(self):
        with self.assertRaisesRegex(DesktopError, "shared display"):
            self.desktop.click(20, 20)
        self.window["sensitive"] = True
        with self.assertRaises(Exception):
            self.desktop.click(-20, 20)
        self.backend.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
