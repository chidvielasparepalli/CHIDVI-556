"""Session sharing controls: offscreen Qt, synthetic status, temporary config only."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication
from core.operator_ui import OperatorPanel
from memory import config_manager as config


class LiveSharingUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "api_keys.json"
        self.path.write_text(json.dumps({"cloud_vision_enabled": True}), encoding="utf-8")
        for name, value in (("CONFIG_FILE", self.path), ("CONFIG_DIR", self.path.parent)):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.panel = OperatorPanel()
        self.panel.on_live_sharing = Mock()
        self.addCleanup(self.panel.close)
        self.addCleanup(self.panel.overlay.close)
        self.addCleanup(self.close_dialogs)

    def close_dialogs(self):
        for name in ("_live_disclosure", "_vision_privacy_dialog"):
            dialog = getattr(self.panel, name, None)
            if dialog is not None:
                dialog.close()
        self.app.processEvents()

    def publish(self, state, **values):
        snapshot = {"state": state, "session_id": "session-test", "last_frame_at": None,
                    "frames_captured": 0, "frames_sent": 0, "analyses": 0,
                    "active_requests": 0, "error": "", "settings": config.get_live_sharing_settings()}
        self.panel.publish_live({**snapshot, **values})
        self.app.processEvents()

    def test_start_requires_visible_disclosure_and_cancel_dispatches_nothing(self):
        self.panel.live_start.click()
        dialog = self.panel._live_disclosure
        self.assertTrue(dialog.isVisible())
        self.assertIn("Google", dialog.explanation.text())
        self.assertIn("12", dialog.explanation.text())
        self.assertIn("review", dialog.explanation.text().lower())
        self.panel.on_live_sharing.assert_not_called()
        dialog.cancel.click()
        self.panel.on_live_sharing.assert_not_called()
        self.assertNotIn("LIVE", self.panel.live_status.text())

    def test_one_disclosure_per_session_with_authoritative_pause_resume_stop(self):
        self.panel.live_start.click()
        self.panel._live_disclosure.approve.click()
        self.panel.on_live_sharing.assert_called_once_with("start", config.get_live_sharing_settings())
        self.assertNotIn("LIVE", self.panel.live_status.text())
        self.assertFalse(self.panel.live_start.isEnabled())
        self.publish("LIVE", last_frame_at=1700000000, frames_sent=1)
        self.assertIn("LIVE", self.panel.live_status.text())
        self.assertNotIn("none", self.panel.live_status.text().lower())
        self.panel.live_pause.click()
        self.assertEqual(self.panel.on_live_sharing.call_args.args[0], "pause")
        self.publish("PAUSED")
        self.assertEqual(self.panel.live_pause.text(), "Resume")
        self.panel.live_pause.click()
        self.assertEqual(self.panel.on_live_sharing.call_args.args[0], "resume")
        self.assertIsNone(self.panel._live_disclosure)
        self.publish("LIVE")
        self.panel.live_stop.click()
        self.assertEqual(self.panel.on_live_sharing.call_args.args[0], "stop")
        self.publish("OFF")
        count = self.panel.on_live_sharing.call_count
        self.panel.live_start.click()
        self.assertTrue(self.panel._live_disclosure.isVisible())
        self.assertEqual(self.panel.on_live_sharing.call_count, count)

    def test_worker_status_is_queued_and_error_is_plain_text(self):
        before = self.panel.live_status.text()
        worker = threading.Thread(target=lambda: self.panel.publish_live({"state": "ERROR", "error": "Provider disconnected"}))
        worker.start()
        worker.join()
        self.assertEqual(self.panel.live_status.text(), before)
        self.app.processEvents()
        self.assertIn("ERROR", self.panel.live_status.text())
        self.assertIn("Provider disconnected", self.panel.live_status.toolTip())
        self.assertTrue(self.panel.live_start.isEnabled())

    def test_cloud_disabled_or_dispatch_failure_does_not_claim_live(self):
        self.panel.set_cloud_vision_state(False)
        self.panel._request_live_start()
        self.assertIsNone(self.panel._live_disclosure)
        self.panel.on_live_sharing.assert_not_called()
        self.assertIn("Cloud Vision", self.panel.live_status.toolTip())
        self.panel.set_cloud_vision_state(True)
        self.panel.on_live_sharing.side_effect = RuntimeError("private provider value")
        self.panel.live_start.click()
        self.panel._live_disclosure.approve.click()
        self.assertNotIn("LIVE", self.panel.live_status.text())
        self.assertNotIn("private provider value", self.panel.live_status.toolTip())

    def test_saved_startup_never_starts_during_load_and_enabling_requires_disclosure(self):
        self.path.write_text(json.dumps({"cloud_vision_enabled": True, "live_screen_sharing_startup": True}))
        before = self.path.read_bytes()
        dialog = self.panel.show_vision_privacy()
        self.assertTrue(dialog.live_startup.isChecked())
        self.panel.on_live_sharing.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        dialog.live_startup.setChecked(False)
        self.assertIs(config.get_live_sharing_startup(), False)
        dialog.live_startup.setChecked(True)
        self.assertIs(config.get_live_sharing_startup(), False)
        self.panel._live_disclosure.approve.click()
        self.assertIs(config.get_live_sharing_startup(), True)
        self.panel.on_live_sharing.assert_not_called()

    def test_settings_save_failure_preserves_values_and_surfaces_safe_error(self):
        dialog = self.panel.show_vision_privacy()
        previous = config.get_live_sharing_settings()
        dialog.live_interval.setValue(3.5)
        with patch.object(config, "save_live_sharing_settings", side_effect=PermissionError("private path")):
            dialog.live_save.click()
        self.assertEqual(config.get_live_sharing_settings(), previous)
        self.assertIn("could not be saved", dialog.live_error.text())
        self.assertNotIn("private path", dialog.live_error.text())
        dialog.live_save.click()
        self.assertEqual(config.get_live_sharing_settings()["interval"], 3.5)

    def test_revoking_cloud_while_disclosure_open_cannot_authorize_start(self):
        self.panel.live_start.click()
        dialog = self.panel._live_disclosure
        self.panel.set_cloud_vision_state(False)
        self.assertIsNone(self.panel._live_disclosure)
        self.assertFalse(dialog.isVisible())
        self.panel.on_live_sharing.assert_not_called()
        self.assertFalse(self.panel.live_start.isEnabled())

    def test_next_session_uses_saved_limits_and_startup_save_failure_remains_off(self):
        dialog = self.panel.show_vision_privacy()
        dialog.live_interval.setValue(3.5)
        dialog.live_save.click()
        self.panel.live_start.click()
        self.assertIn("3.5 seconds", self.panel._live_disclosure.explanation.text())
        self.panel._live_disclosure.approve.click()
        self.assertEqual(self.panel.on_live_sharing.call_args.args[1]["interval"], 3.5)
        self.publish("OFF")
        dialog.live_startup.setChecked(True)
        with patch.object(config, "save_live_sharing_startup", side_effect=PermissionError("private secret")):
            self.panel._live_disclosure.approve.click()
        self.assertFalse(dialog.live_startup.isChecked())
        self.assertIs(config.get_live_sharing_startup(), False)
        self.assertIn("could not be saved", dialog.live_error.text())
        self.assertNotIn("private secret", dialog.live_error.text())

    def test_privacy_settings_remain_scrollable_on_small_logical_screen(self):
        with patch.object(self.panel, "_review_available_geometry", return_value=QRect(0, 0, 400, 350)):
            dialog = self.panel.show_vision_privacy()
        self.app.processEvents()
        self.assertLessEqual(dialog.width(), 384)
        self.assertLessEqual(dialog.height(), 334)
        self.assertGreater(dialog.scroll.verticalScrollBar().maximum(), 0)
        self.assertLessEqual(self.panel.minimumSizeHint().width(), 1400)

    def test_small_logical_screen_keeps_disclosure_decisions_visible_and_toolbar_bounded(self):
        with patch.object(self.panel, "_review_available_geometry", return_value=QRect(-400, 0, 400, 350)):
            self.panel.live_start.click()
        dialog = self.panel._live_disclosure
        self.app.processEvents()
        self.assertLessEqual(dialog.width(), 384)
        self.assertLessEqual(dialog.height(), 334)
        for button in (dialog.approve, dialog.cancel):
            self.assertTrue(button.isVisible())
            self.assertTrue(dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight())))
        self.assertLessEqual(self.panel.live_controls.maximumWidth(), 240)


if __name__ == "__main__":
    unittest.main()
