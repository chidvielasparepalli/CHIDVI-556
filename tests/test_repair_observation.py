"""Repair observation never captures a real screen in these tests."""
import io
import os
from pathlib import Path
import tempfile
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from core.repair_observation import RepairObservation
from core.screen_sharing import LiveScreenSharing
from core.task_control import TaskStore


class RepairObservationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        from memory import config_manager
        settings = patch.multiple(config_manager, CONFIG_DIR=self.workspace / "synthetic-config",
                                  CONFIG_FILE=self.workspace / "synthetic-config" / "settings.json")
        settings.start()
        self.addCleanup(settings.stop)
        image = Image.new("RGB", (4, 3), "black")
        image.putpixel((0, 0), (255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self.frame = {"data": buffer.getvalue(), "mime_type": "image/png", "width": 4, "height": 3,
                      "monitor": 1, "source_rect": {"left": 0, "top": 0, "width": 4, "height": 3}}
        self.capture = Mock(return_value=self.frame)
        self.observer = Mock(return_value={"application": "code.exe", "title": "Editor", "window_id": 7})
        self.emit = Mock()
        self.observation = RepairObservation(self.workspace, self.emit, capture=self.capture, observer=self.observer)
        for target, value in (("threading.Thread", Mock()),
                              ("psutil.cpu_percent", Mock(return_value=0)),
                              ("psutil.virtual_memory", Mock(return_value=SimpleNamespace(percent=20)))):
            item = patch("core.repair_observation." + target, value)
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.observation.stop)

    def test_default_stopped_refresh_never_captures(self):
        self.assertEqual(self.observation.snapshot()["mode"], "stopped")
        self.assertFalse(self.observation.refresh()["ok"])
        self.capture.assert_not_called()
        self.observer.assert_not_called()

    def test_opt_in_capture_returns_metadata_only_and_never_cloud_or_disk(self):
        self.observation.start("task-one")
        with patch("core.vision.analyze_capture") as analyze:
            result = self.observation.refresh()
        self.assertTrue(result["ok"])
        self.assertEqual(result["latest"]["task_id"], "task-one")
        self.assertEqual(result["latest"]["width"], 4)
        self.assertEqual(len(result["latest"]["sha256"]), 64)
        self.assertNotIn("data", result["latest"])
        self.assertFalse(result["local_backend"]["available"])
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertNotIn(repr(self.frame["data"]), repr(self.emit.call_args_list))
        analyze.assert_not_called()

    def test_start_options_validate_and_retention_is_explicit(self):
        for kwargs in ({"interval": 1}, {"interval": 61}, {"interval": float("nan")},
                       {"interval": True}, {"monitor": -1}, {"monitor": True}, {"retain": "yes"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.observation.start("task-one", **kwargs)
        with self.assertRaises(ValueError):
            self.observation.start("../outside")
        self.observation.start("task-one", interval=2, monitor=0, retain=True)
        result = self.observation.refresh()
        self.capture.assert_called_once_with(monitor=0)
        retained = list((self.workspace / "logs" / "repair").rglob("*.png"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), self.frame["data"])
        self.assertTrue(result["retain"])

    def test_retention_does_not_follow_a_preexisting_temporary_hardlink(self):
        folder = self.workspace / "logs" / "repair" / "task-one"
        folder.mkdir(parents=True)
        original = self.workspace / "unrelated.txt"
        original.write_bytes(b"keep unrelated content")
        os.link(original, folder / "latest.png.tmp")
        self.observation.start("one", retain=True)
        self.assertTrue(self.observation.refresh()["ok"])
        self.assertEqual(original.read_bytes(), b"keep unrelated content")

    def test_sensitive_locked_and_changed_foreground_are_refused(self):
        self.observation.start("task-one")
        for raw in ({"locked": True}, {"sensitive": True}, {"title": "Sign in - 123456"},
                    {"application": "LogonUI.exe"}):
            with self.subTest(raw=raw):
                self.observer.return_value = raw
                self.assertFalse(self.observation.refresh()["ok"])
        self.capture.assert_not_called()
        self.observer.side_effect = [{"application": "code.exe", "window_id": 1},
                                     {"application": "code.exe", "window_id": 2}]
        self.assertEqual(self.observation.refresh()["code"], "foreground_changed")
        self.assertEqual(self.observation.snapshot()["latest"], {})

    def test_resource_guard_and_capture_errors_do_not_expose_raw_exception(self):
        self.observation.start("task-one")
        with patch("core.repair_observation.psutil.cpu_percent", return_value=95):
            self.assertEqual(self.observation.refresh()["code"], "resources_busy")
        self.capture.assert_not_called()
        self.capture.side_effect = OSError("password=PRIVATE_VALUE")
        result = self.observation.refresh()
        self.assertEqual(result["code"], "capture_failed")
        self.assertNotIn("PRIVATE_VALUE", repr(result) + repr(self.emit.call_args_list))

    def test_pause_and_stop_clear_image_and_prevent_refresh(self):
        self.observation.start("task-one")
        self.observation.refresh()
        self.observation.pause()
        self.assertEqual(self.observation.snapshot()["mode"], "paused")
        self.assertEqual(self.observation.snapshot()["latest"], {})
        self.capture.reset_mock()
        self.observation.refresh()
        self.capture.assert_not_called()
        self.observation.stop()
        self.assertEqual(self.observation.snapshot()["mode"], "stopped")

    def test_stop_during_metadata_read_prevents_starting_a_capture(self):
        def stopped_foreground():
            self.observation.stop()
            return {"application": "code.exe", "window_id": 7}
        self.observer.side_effect = stopped_foreground
        self.observation.start("task-one")
        self.assertEqual(self.observation.refresh()["code"], "cancelled")
        self.capture.assert_not_called()

    def test_snapshot_cannot_mutate_private_capture_geometry(self):
        self.observation.start("task-one")
        result = self.observation.refresh()
        result["latest"]["source_rect"]["width"] = 900
        self.assertEqual(self.observation.snapshot()["latest"]["source_rect"]["width"], 4)

    def test_stop_during_capture_discards_late_image_and_retention_write(self):
        entered, release = Event(), Event()
        def blocked_capture(**_):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.frame
        self.capture.side_effect = blocked_capture
        self.observation.start("task-one", retain=True)
        result = []
        thread = Thread(target=lambda: result.append(self.observation.refresh()))
        thread.start()
        self.assertTrue(entered.wait(1))
        self.observation.stop()
        emitted = self.emit.call_count
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["code"], "cancelled")
        self.assertEqual(self.emit.call_count, emitted)
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(self.observation.snapshot()["latest"], {})
        self.assertEqual(self.observer.call_count, 1)

    def test_analysis_is_explicit_and_late_analysis_after_stop_is_discarded(self):
        self.observation.start("task-one")
        self.observation.refresh()
        with patch("core.vision.analyze_capture", return_value={"ok": True, "description": "Editor visible"}) as analyze:
            result = self.observation.analyze_latest("What failed?")
        self.assertTrue(result["ok"])
        self.assertEqual(analyze.call_args.args[0]["data"], self.frame["data"])
        self.assertTrue(callable(analyze.call_args.kwargs["valid"]))
        def late_analysis(*_, **__):
            self.observation.stop()
            return {"ok": True, "description": "late output"}
        with patch("core.vision.analyze_capture", side_effect=late_analysis):
            self.assertEqual(self.observation.analyze_latest("What failed?")["code"], "cancelled")
        self.assertNotIn("late output", repr(self.emit.call_args_list))

    def test_pause_and_stop_invalidate_approval_before_upload(self):
        for end in (self.observation.pause, self.observation.stop):
            with self.subTest(operation=end.__name__):
                self.observation.start("task-one")
                self.observation.refresh()
                upload = Mock()
                def pending_approval(frame, question, *, valid=None):
                    self.assertTrue(callable(valid), "Analysis needs a live observation-session guard")
                    self.assertTrue(valid())
                    end()
                    self.assertFalse(valid(), "Stopping observation must revoke pending image approval")
                    if valid():
                        upload(frame)
                    return {"ok": False, "code": "cancelled"}
                with patch("core.vision.analyze_capture", side_effect=pending_approval):
                    result = self.observation.analyze_latest("What failed?")
                self.assertEqual(result["code"], "cancelled")
                upload.assert_not_called()

    def test_new_task_cannot_keep_previous_snapshot_approval_valid(self):
        self.observation.start("task-one")
        self.observation.refresh()
        def pending_approval(frame, question, *, valid=None):
            self.assertTrue(callable(valid))
            self.assertTrue(valid())
            self.observation.start("task-two")
            self.observation.refresh()
            self.assertFalse(valid())
            return {"ok": True, "description": "Old snapshot result"}
        with patch("core.vision.analyze_capture", side_effect=pending_approval):
            result = self.observation.analyze_latest("What failed?")
        self.assertEqual(result["code"], "cancelled")
        self.assertEqual(self.observation.snapshot()["task_id"], "task-two")

    def test_explicit_review_freezes_image_for_225_seconds_without_deadline_renewal(self):
        clock = [100.0]
        with patch("core.repair_observation.time.monotonic", side_effect=lambda: clock[0]):
            self.observation.start("task-one", interval=5)
            self.observation.refresh()
            def pending_approval(frame, question, *, valid=None):
                self.assertTrue(callable(valid))
                self.assertTrue(valid())
                clock[0] = 130.0
                self.assertTrue(valid(), "User review may take longer than background snapshot freshness")
                clock[0] = 324.9
                self.assertTrue(valid())
                clock[0] = 325.1
                self.observation.refresh()
                self.assertFalse(valid(), "Fresh background capture cannot renew the explicit analysis deadline")
                return {"ok": True, "description": "Stale snapshot result"}
            with patch("core.vision.analyze_capture", side_effect=pending_approval):
                result = self.observation.analyze_latest("What failed?")
        self.assertEqual(result["code"], "cancelled")

    def test_initial_snapshot_freshness_is_still_required_before_review(self):
        with patch("core.repair_observation.time.monotonic", return_value=100.0):
            self.observation.start("task-one", interval=5)
            self.observation.refresh()
        with patch("core.repair_observation.time.monotonic", return_value=111.0), patch("core.vision.analyze_capture") as analyze:
            result = self.observation.analyze_latest("What failed?")
        self.assertEqual(result["code"], "stale_snapshot")
        analyze.assert_not_called()

    def test_privacy_guard_clearing_snapshot_revokes_pending_approval(self):
        self.observation.start("task-one")
        self.observation.refresh()
        def pending_approval(frame, question, *, valid=None):
            self.assertTrue(valid())
            self.observer.return_value = {"locked": True}
            self.assertEqual(self.observation.refresh()["code"], "privacy_guard")
            self.assertFalse(valid())
            return {"ok": True, "description": "Late result"}
        with patch("core.vision.analyze_capture", side_effect=pending_approval):
            result = self.observation.analyze_latest("What failed?")
        self.assertEqual(result["code"], "cancelled")

    def test_preview_window_refresh_does_not_revoke_original_image_approval(self):
        self.observation.start("task-one")
        self.observation.refresh()
        def pending_approval(frame, question, *, valid=None):
            self.assertTrue(callable(valid))
            self.assertTrue(valid())
            self.observer.return_value = {"application": "chidvi.exe", "window_id": 99}
            self.observation.refresh()
            self.assertTrue(valid(), "Preview focus and another local sample do not replace the approved pixels")
            return {"ok": True, "description": "Original image result"}
        with patch("core.vision.analyze_capture", side_effect=pending_approval):
            result = self.observation.analyze_latest("What failed?")
        self.assertTrue(result["ok"])

    def test_real_worker_starts_only_when_enabled_and_exits_on_stop(self):
        captured = Event()
        self.emit.side_effect = lambda event: captured.set() if event.get("observation", {}).get("latest") else None
        with patch("core.repair_observation.threading.Thread", Thread), patch("core.vision.analyze_capture") as analyze:
            self.observation.start("task-worker", interval=2)
            worker = self.observation._worker
            self.assertTrue(captured.wait(1))
            self.observation.stop()
            self.assertFalse(worker.is_alive())
            analyze.assert_not_called()

    def shared_observation(self):
        source = Mock(return_value=self.frame)
        sharing = LiveScreenSharing(capture=source, observer=self.observer, cloud_allowed=lambda: True)
        self.addCleanup(sharing.close)
        observation = RepairObservation(self.workspace, self.emit, capture=self.capture,
                                        observer=self.observer, sharing=sharing)
        self.addCleanup(observation.stop)
        sharing.start()
        sharing.frame()
        observation.start("repair-session")
        return observation, sharing, source

    def test_live_snapshot_reuses_shared_capture_without_upload_and_keeps_bytes_out_of_metadata(self):
        observation, sharing, source = self.shared_observation()
        with patch("core.vision.analyze_capture") as analyze:
            result = observation.refresh()
        self.assertTrue(result["ok"], result)
        self.assertEqual(source.call_count, 1)
        self.capture.assert_not_called()
        analyze.assert_not_called()
        self.assertNotIn("data", result["latest"])
        self.assertNotIn("_sharing", result["latest"])
        self.assertEqual(result["latest"]["source"], "live_sharing")

    def test_default_capture_path_uses_singleton_sharing_but_explicit_capture_stays_isolated(self):
        _, sharing, source = self.shared_observation()
        with patch("actions.screen_processor.capture_screen") as capture, \
                patch("core.screen_sharing.get_screen_sharing", return_value=sharing):
            production = RepairObservation(self.workspace, self.emit, observer=self.observer)
            self.addCleanup(production.stop)
            production.start("production-task")
            self.assertTrue(production.refresh()["ok"])
            capture.assert_not_called()
            self.observation.start("injected-task")
            self.assertTrue(self.observation.refresh()["ok"])
        self.assertEqual(source.call_count, 1)
        self.capture.assert_called_once_with(monitor=1)

    def test_shared_pixels_cannot_be_downgraded_to_one_shot_after_stop(self):
        observation, sharing, _ = self.shared_observation()
        observation.refresh()
        sharing.stop()
        with patch("core.vision.analyze_capture") as analyze:
            self.assertEqual(observation.analyze_latest("What failed?")["code"], "snapshot_unavailable")
            analyze.assert_not_called()
        self.assertTrue(observation.refresh()["ok"])
        self.capture.assert_called_once_with(monitor=1)

    def test_live_analysis_gets_new_lease_in_callers_task_context_without_one_shot_approval(self):
        from core.desktop_control import DesktopControl
        from core import vision
        from google import genai
        observation, sharing, source = self.shared_observation()
        self.assertTrue(observation.refresh()["ok"])
        tasks = TaskStore()
        task = tasks.submit("Describe repair screen", "repair-analysis", executor="repair")
        tasks.claim(task["id"])
        desktop = DesktopControl()
        desktop.cloud_vision_enabled = True
        desktop.review_image = Mock(side_effect=AssertionError("Live session must not ask per-image approval"))
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="Synthetic repair editor")
        with tasks.context(task["id"]), patch("core.screen_sharing.get_screen_sharing", return_value=sharing), \
                patch.object(vision, "get_service", return_value=desktop), \
                patch.object(vision.config_manager, "get_cloud_vision_enabled", return_value=True), \
                patch.object(vision.config_manager, "load_api_keys", return_value={"gemini_api_key": "fake-test-key"}), \
                patch.object(genai, "Client", return_value=client):
            result = observation.analyze_latest("What failed?")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sharing"]["task_id"], task["id"])
        desktop.review_image.assert_not_called()
        client.models.generate_content.assert_called_once()
        self.assertEqual(source.call_count, 1)
        self.capture.assert_not_called()

    def test_paused_shared_session_never_falls_back_to_local_capture_or_approval(self):
        observation, sharing, source = self.shared_observation()
        observation.refresh()
        sharing.pause()
        with patch("core.vision.analyze_capture") as analyze:
            self.assertEqual(observation.refresh()["code"], "sharing_inactive")
            self.assertEqual(observation.analyze_latest("What failed?")["code"], "sharing_inactive")
        self.capture.assert_not_called()
        self.assertEqual(source.call_count, 1)
        analyze.assert_not_called()

    def test_stop_during_shared_analysis_discards_result(self):
        observation, sharing, _ = self.shared_observation()
        observation.refresh()
        def stopped(frame, question, *, valid):
            self.assertTrue(valid())
            sharing.stop()
            self.assertFalse(valid())
            return {"ok": True, "description": "Late answer"}
        with patch("core.vision.analyze_capture", side_effect=stopped):
            result = observation.analyze_latest("What failed?")
        self.assertEqual(result["code"], "cancelled")


if __name__ == "__main__":
    unittest.main()
