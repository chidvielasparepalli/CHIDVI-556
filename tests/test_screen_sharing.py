"""Screen sharing lifecycle tests use synthetic pixels and no network or desktop."""
import io
from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from core.task_control import TaskStore

try:
    from core import screen_sharing as sharing
except ImportError:
    sharing = None


def pixels(color="blue", size=(64, 48), monitor=1):
    image = Image.new("RGB", size, color)
    image.putpixel((0, 0), (255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return {"data": buffer.getvalue(), "mime_type": "image/png", "width": size[0],
            "height": size[1], "monitor": monitor,
            "source_rect": {"left": -100, "top": 20, "width": size[0], "height": size[1]}}


class ScreenSharingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(sharing, "A shared live screen service must exist")
        self.now, self.allowed = [100.0], [True]
        self.capture = Mock(return_value=pixels())
        self.observer = Mock(return_value={"window_id": 7, "application": "code.exe", "title": "Editor"})
        self.events = []
        self.service = sharing.LiveScreenSharing(capture=self.capture, observer=self.observer,
            cloud_allowed=lambda: self.allowed[0], emit=self.events.append, clock=lambda: self.now[0])
        worker = patch("core.screen_sharing.threading.Thread", Mock())
        worker.start()
        self.addCleanup(worker.stop)
        self.addCleanup(self.service.close)

    def live(self, settings=None):
        self.service.start(settings)
        return self.service.frame()

    def test_off_never_captures_or_observes(self):
        self.assertEqual(self.service.snapshot()["state"], "OFF")
        self.assertIsNone(self.service.stream_frame())
        with self.assertRaises(sharing.ScreenSharingError):
            self.service.frame()
        self.capture.assert_not_called()
        self.observer.assert_not_called()

    def test_start_is_nonblocking_then_first_frame_becomes_live(self):
        self.assertEqual(self.service.start()["state"], "STARTING")
        self.capture.assert_not_called()
        frame = self.service.frame()
        self.assertTrue(self.service.valid(frame))
        state = self.service.snapshot()
        self.assertEqual(state["state"], "LIVE")
        self.assertEqual(state["frames_captured"], 1)
        self.assertNotIn("data", repr(state))
        self.assertNotIn("Editor", repr(state))
        self.assertNotIn("data", repr(self.events))
        self.assertEqual(frame["_sharing"].window_id, 7)

    def test_pause_resume_and_stop_revoke_frozen_frames(self):
        old = self.live()
        session = self.service.snapshot()["session_id"]
        self.assertEqual(self.service.pause()["state"], "PAUSED")
        self.assertFalse(self.service.valid(old))
        self.assertIsNone(self.service.stream_frame())
        self.service.resume()
        fresh = self.service.frame()
        self.assertTrue(self.service.valid(fresh))
        self.assertFalse(self.service.valid(old))
        self.assertEqual(self.service.snapshot()["session_id"], session)
        self.assertEqual(self.service.stop()["state"], "OFF")
        self.assertFalse(self.service.valid(fresh))

    def test_stop_stays_stopping_until_network_context_drains(self):
        frame = self.live()
        with self.service.transmission(frame):
            self.assertEqual(self.service.snapshot()["active_requests"], 1)
            self.assertEqual(self.service.stop()["state"], "STOPPING")
            self.assertFalse(self.service.valid(frame))
            with self.assertRaises(sharing.ScreenSharingError):
                with self.service.transmission(frame):
                    self.fail("No upload may start after Stop")
            self.assertFalse(self.service.mark_analyzed(frame, "Late response"))
        self.assertEqual(self.service.snapshot()["state"], "OFF")
        self.assertEqual(self.service.snapshot()["active_requests"], 0)

    def test_provider_failure_always_drains_permit(self):
        frame = self.live()
        with self.assertRaisesRegex(OSError, "synthetic"):
            with self.service.transmission(frame):
                raise OSError("synthetic")
        self.assertEqual(self.service.snapshot()["active_requests"], 0)
        with self.assertRaises(sharing.ScreenSharingError):
            with self.service.transmission(frame):
                self.fail("A consumed lease must not retry")

    def test_task_origin_and_current_context_are_required(self):
        tasks = TaskStore()
        first = tasks.submit("Look", "first")
        tasks.claim(first["id"])
        self.service.start()
        with tasks.context(first["id"]):
            frame = self.service.frame()
            self.assertTrue(self.service.valid(frame))
        self.assertFalse(self.service.valid(frame))
        tasks.cancel(first["id"])
        tasks.release(first["id"])
        newer = tasks.submit("Another task", "newer")
        tasks.claim(newer["id"])
        with tasks.context(newer["id"]):
            self.assertFalse(self.service.valid(frame))
            self.assertTrue(self.service.valid(self.service.frame()))

    def test_tampered_bytes_geometry_identity_and_lease_are_refused(self):
        frame = self.live()
        mutations = [{"data": pixels("red")["data"]}, {"width": 999}, {"frame_id": "forged"},
                     {"source_rect": {"left": 0, "top": 0, "width": 64, "height": 48}},
                     {"timestamp": 0}, {"_sharing": object()}]
        for mutation in mutations:
            with self.subTest(keys=list(mutation)):
                self.assertFalse(self.service.valid({**frame, **mutation}))
        other = sharing.LiveScreenSharing(capture=self.capture, observer=self.observer,
                                         cloud_allowed=lambda: True, clock=lambda: self.now[0])
        self.addCleanup(other.close)
        other.start()
        other.frame()
        self.assertFalse(other.valid(frame))

    def test_stale_dispatch_refused_but_running_response_has_45_seconds(self):
        frame = self.live()
        self.now[0] += 11
        with self.assertRaises(sharing.ScreenSharingError):
            with self.service.transmission(frame):
                self.fail("Stale pixels must not be sent")
        fresh = self.service.frame()
        with self.service.transmission(fresh):
            self.now[0] += 20
            self.assertTrue(self.service.valid(fresh))
            self.assertTrue(self.service.mark_analyzed(fresh, "Editor"))
            self.assertFalse(self.service.mark_analyzed(fresh, "Duplicate"))
            self.now[0] += 26
            self.assertFalse(self.service.valid(fresh))
        self.assertEqual(self.service.snapshot()["analyses"], 1)

    def test_cached_frame_rechecks_foreground_and_preserves_old_lease(self):
        first = self.live()
        again = self.service.frame()
        self.assertEqual(first["data"], again["data"])
        self.capture.assert_called_once_with(monitor=1)
        self.observer.return_value = {"window_id": 8, "application": "notepad.exe"}
        self.capture.return_value = pixels("red")
        fresh = self.service.frame()
        self.assertNotEqual(first["data"], fresh["data"])
        self.assertEqual(fresh["_sharing"].window_id, 8)
        self.assertEqual(first["data"], pixels()["data"])

    def test_force_capture_and_freshness_do_not_reuse_old_sample(self):
        self.live()
        self.service.frame(force=True)
        self.assertEqual(self.capture.call_count, 2)
        self.now[0] += 3
        self.service.frame(max_age=2)
        self.assertEqual(self.capture.call_count, 3)

    def test_monitor_scope_cannot_be_expanded_by_tool(self):
        self.live()
        with self.assertRaises(sharing.ScreenSharingError):
            self.service.frame(monitor=0)
        self.capture.assert_called_once_with(monitor=1)
        self.service.stop()
        self.capture.return_value = pixels(monitor=0)
        self.service.start({"monitor": 0})
        frame = self.service.frame(monitor=0)
        self.assertEqual(frame["source_rect"]["left"], -100)

    def test_privacy_and_cloud_revocation_invalidate_cache_without_upload(self):
        for raw in ({"window_id": 7, "locked": True}, {"window_id": 7, "sensitive": True},
                    {"window_id": 7, "title": "Password manager"}):
            self.service.stop()
            self.observer.return_value = {"window_id": 7}
            frame = self.live()
            self.observer.return_value = raw
            with self.assertRaises(sharing.ScreenSharingError):
                self.service.frame()
            self.assertFalse(self.service.valid(frame))
            self.assertIn(self.service.snapshot()["state"], {"PAUSED", "ERROR"})
        self.service.stop()
        self.observer.return_value = {"window_id": 7}
        frame = self.live()
        self.allowed[0] = False
        self.assertFalse(self.service.valid(frame))
        self.assertEqual(self.service.snapshot()["state"], "ERROR")

    def test_capture_changed_window_blank_or_provider_error_never_becomes_live(self):
        self.service.start()
        self.observer.side_effect = [{"window_id": 7}, {"window_id": 8}]
        with self.assertRaises(sharing.ScreenSharingError):
            self.service.frame()
        self.assertNotEqual(self.service.snapshot()["state"], "LIVE")
        self.observer.side_effect = None
        self.service.stop()
        self.capture.side_effect = RuntimeError("private window title or secret")
        self.service.start()
        with self.assertRaises(sharing.ScreenSharingError):
            self.service.frame()
        self.assertEqual(self.service.snapshot()["state"], "ERROR")
        self.assertNotIn("private window", repr(self.service.snapshot()))
        self.assertNotIn("secret", repr(self.events))
        self.assertEqual(self.service.resume()["state"], "ERROR")

    def test_session_expiry_requires_explicit_restart(self):
        frame = self.live({"session_minutes": 1})
        self.now[0] += 61
        self.assertFalse(self.service.valid(frame))
        self.assertEqual(self.service.snapshot()["state"], "OFF")
        self.assertIsNone(self.service.stream_frame())

    def test_stream_changes_heartbeat_rate_and_scaled_geometry(self):
        self.capture.return_value = pixels(size=(2000, 1200))
        self.live({"max_width": 1280, "max_height": 720})
        frame = self.service.stream_frame()
        self.assertEqual((frame["width"], frame["height"]), (1200, 720))
        self.assertEqual(frame["source_rect"]["width"], 2000)
        with self.service.transmission(frame, kind="stream"):
            self.assertTrue(self.service.mark_sent(frame))
        self.assertIsNone(self.service.stream_frame())
        self.now[0] += 6
        self.assertIsNone(self.service.stream_frame())
        self.now[0] += 25
        self.service.frame(force=True)  # The sampler has refreshed unchanged pixels.
        heartbeat = self.service.stream_frame()
        self.assertIsNotNone(heartbeat)
        with self.service.transmission(heartbeat, kind="stream"):
            self.service.mark_sent(heartbeat)
        self.assertEqual(self.service.snapshot()["frames_sent"], 2)
        self.now[0] += 6
        self.capture.return_value = pixels("red", size=(2000, 1200))
        self.service.frame(force=True)
        self.assertIsNotNone(self.service.stream_frame())

    def test_stream_and_analysis_limits_are_independent(self):
        frame = self.live({"max_per_minute": 2, "analysis_per_minute": 1})
        with self.service.transmission(frame):
            self.service.mark_analyzed(frame, "Editor")
        self.now[0] += 31
        frame = self.service.frame()
        with self.assertRaises(sharing.ScreenSharingError):
            with self.service.transmission(frame, kind="analysis"):
                self.fail("Analysis budget must remain separate")
        with self.service.transmission(frame, kind="stream"):
            self.service.mark_sent(frame)
        frame = self.service.frame()
        with self.assertRaises(sharing.ScreenSharingError):
            with self.service.transmission(frame, kind="stream"):
                self.fail("Total upload rate exceeded")
        self.now[0] += 31
        with self.service.transmission(self.service.frame()):
            pass

    def test_invalid_settings_fail_without_start_or_capture(self):
        for settings in ({"interval": 0}, {"interval": True}, {"max_per_minute": 61},
                         {"analysis_per_minute": 0}, {"max_width": 100}, {"max_height": 2000},
                         {"monitor": -1}, {"change_only": "yes"}, {"session_minutes": 121},
                         {"unexpected": 1}):
            with self.subTest(settings=settings), self.assertRaises((TypeError, ValueError)):
                self.service.start(settings)
        self.capture.assert_not_called()
        self.assertEqual(self.service.snapshot()["state"], "OFF")

    def test_capture_stop_race_discards_late_pixels_and_drains(self):
        entered, release = threading.Event(), threading.Event()
        def capture(**_):
            entered.set()
            self.assertTrue(release.wait(2))
            return pixels()
        self.capture.side_effect = capture
        self.service.start()
        errors = []
        def capture_on_worker():
            try:
                self.service.frame(force=True)
            except sharing.ScreenSharingError as error:
                errors.append(error.code)
        # Use the original Thread class, despite the service's mocked worker factory.
        worker = ORIGINAL_THREAD(target=capture_on_worker)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.service.stop()["state"], "STOPPING")
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.service.snapshot()["state"], "OFF")
        self.assertEqual(self.service.snapshot()["frames_captured"], 0)
        self.assertEqual(len(errors), 1)

    def test_bind_publishes_safe_state_and_revoked_callback_stops_sharing(self):
        updates = []
        self.service.bind(emit=updates.append, cloud_allowed=lambda: self.allowed[0])
        frame = self.live()
        self.assertEqual(updates[-1]["state"], "LIVE")
        self.service.bind(cloud_allowed=lambda: False)
        self.assertFalse(self.service.valid(frame))
        self.assertEqual(self.service.snapshot()["state"], "ERROR")

    def test_replacing_immutable_lease_fields_does_not_forge_authority(self):
        frame = self.live()
        forged = replace(frame["_sharing"], captured_mono=self.now[0] + 99)
        self.assertFalse(self.service.valid({**frame, "_sharing": forged}))

    def test_stream_polling_does_not_override_configured_capture_interval(self):
        self.live({"interval": 30})
        self.now[0] += 3
        frame = self.service.stream_frame()
        self.assertIsNotNone(frame)
        self.capture.assert_called_once_with(monitor=1)
        self.now[0] += 8
        self.assertIsNone(self.service.stream_frame())
        self.capture.assert_called_once_with(monitor=1)

    def test_stream_sensitive_foreground_revokes_without_new_capture(self):
        old = self.live()
        self.observer.return_value = {"window_id": 7, "locked": True}
        self.assertIsNone(self.service.stream_frame())
        self.assertFalse(self.service.valid(old))
        self.assertEqual(self.service.snapshot()["state"], "PAUSED")
        self.capture.assert_called_once_with(monitor=1)

    def test_permit_does_not_hold_service_lock_during_network_work(self):
        frame = self.live()
        with self.service.transmission(frame):
            stopped = threading.Event()
            worker = ORIGINAL_THREAD(target=lambda: (self.service.stop(), stopped.set()))
            worker.start()
            self.assertTrue(stopped.wait(1), "Stop must not wait for a provider response")
            worker.join(1)
        self.assertEqual(self.service.snapshot()["state"], "OFF")

    def test_actual_worker_is_local_only_and_stops_sampling(self):
        captured = threading.Event()
        def capture(**_):
            captured.set()
            return pixels()
        self.capture.side_effect = capture
        with patch("core.screen_sharing.threading.Thread", ORIGINAL_THREAD):
            self.service.start({"interval": 1})
            self.assertTrue(captured.wait(1))
            self.service.stop()
            worker = self.service._worker
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.service.snapshot()["state"], "OFF")
        self.assertEqual(self.service.snapshot()["active_requests"], 0)
        self.assertEqual(self.service.snapshot()["frames_sent"], 0)

    def test_last_sent_identity_is_validated_and_cleared_on_invalidation(self):
        frame = self.live()
        initial = self.service.snapshot()
        self.assertIn("last_sent_at", initial, "Sharing status must identify the last delivered capture")
        self.assertIsNone(initial["last_sent_at"])
        self.assertIsNone(initial["last_sent_frame_id"])
        self.assertIn("last_stream_frame_at", initial)
        self.assertIsNone(initial["last_stream_frame_at"])
        with self.service.transmission(frame, kind="stream"):
            self.assertTrue(self.service.mark_sent(frame))
        sent = self.service.snapshot()
        self.assertEqual(sent["last_sent_at"], frame["captured_at"])
        self.assertEqual(sent["last_sent_frame_id"], frame["frame_id"])
        self.assertEqual(sent["last_stream_frame_at"], frame["captured_at"])
        self.assertEqual(sent["sent_clock"], self.now[0])
        self.assertFalse(self.service.mark_sent({**frame, "data": b"tampered"}))
        self.assertEqual(self.service.snapshot()["last_sent_frame_id"], frame["frame_id"])
        self.service.pause()
        paused = self.service.snapshot()
        self.assertGreater(paused["generation"], sent["generation"])
        self.assertIsNone(paused["last_sent_at"])
        self.assertIsNone(paused["last_sent_frame_id"])
        self.assertIsNone(paused["sent_clock"])
        self.assertIsNone(paused["last_stream_frame_at"])
        self.assertFalse(self.service.mark_sent(frame))

    def test_stop_and_error_clear_last_sent_context(self):
        for end in (self.service.stop, lambda: self.service.fail("synthetic")):
            frame = self.live()
            with self.service.transmission(frame, kind="stream"):
                self.service.mark_sent(frame)
            end()
            self.assertIn("last_sent_at", self.service.snapshot())
            self.assertIsNone(self.service.snapshot()["last_sent_at"])
            self.assertIsNone(self.service.snapshot()["last_sent_frame_id"])

    def test_stream_does_not_delay_http_analysis_and_http_quota_has_no_spacing(self):
        frame = self.live({"max_per_minute": 1, "analysis_per_minute": 3})
        with self.service.transmission(frame, kind="stream"):
            self.service.mark_sent(frame)
        for _ in range(3):
            try:
                with self.service.transmission(self.service.frame(), kind="analysis"):
                    pass
            except sharing.ScreenSharingError as error:
                self.fail(f"Stream cadence must not consume the HTTP analysis budget: {error.code}")
        with self.assertRaises(sharing.ScreenSharingError):
            with self.service.transmission(self.service.frame(), kind="analysis"):
                self.fail("Rolling HTTP quota must still be enforced")
        self.assertEqual(self.service.snapshot()["state"], "LIVE")

    def test_failure_reason_preserves_safe_status_and_redacts_credentials(self):
        self.live()
        self.service.fail("Provider rate limit (429); token=synthetic-private")
        reason = self.service.snapshot()["error"]
        self.assertIn("429", reason)
        self.assertNotIn("synthetic-private", reason)
        self.assertIn("REDACTED", reason)

    def test_has_streamed_recognizes_unchanged_pixels_without_reusing_old_session(self):
        frame = self.live()
        self.assertTrue(callable(getattr(self.service, "has_streamed", None)), "Runtime needs delivery identity for unchanged frames")
        self.assertFalse(self.service.has_streamed(frame))
        with self.service.transmission(frame, kind="stream"):
            self.service.mark_sent(frame)
        refreshed = self.service.frame(force=True)
        self.assertTrue(self.service.has_streamed(refreshed))
        self.capture.return_value = pixels("red")
        self.assertFalse(self.service.has_streamed(self.service.frame(force=True)))
        self.service.pause()
        self.assertFalse(self.service.has_streamed(refreshed))

    def test_http_analysis_does_not_claim_native_video_context(self):
        frame = self.live()
        with self.service.transmission(frame, kind="analysis"):
            self.service.mark_analyzed(frame, "Editor")
        state = self.service.snapshot()
        self.assertEqual(state["last_sent_at"], frame["captured_at"])
        self.assertIn("last_stream_frame_at", state)
        self.assertIsNone(state["last_stream_frame_at"])
        self.assertFalse(self.service.has_streamed(frame))


ORIGINAL_THREAD = threading.Thread


if __name__ == "__main__":
    unittest.main()
