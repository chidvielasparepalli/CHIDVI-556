"""Repair controls run offscreen and invoke mocks, never live repair actions."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
from core.repair_ui import RepairPanel


class RepairUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        hotkey = patch("core.repair_ui.NativeRepairHotkey")
        hotkey.start()
        self.addCleanup(hotkey.stop)
        self.panel = RepairPanel()
        self.addCleanup(self.panel.close)
        self.panel.on_command = Mock()
        self.panel.on_mode = Mock()
        self.panel.on_stop = Mock()
        self.panel.on_permissions = Mock()
        self.panel.on_observation = Mock()
        self.panel.on_rollback = Mock()
        self.panel.on_restart = Mock()

    def test_mode_and_monitoring_start_off_and_actions_are_guarded(self):
        self.assertFalse(self.panel.mode.isChecked())
        self.assertFalse(self.panel.observation.isChecked())
        self.assertFalse(self.panel.retain.isChecked())
        self.panel.commands["demo"].click()
        self.panel.on_command.assert_not_called()
        self.panel.mode.setChecked(True)
        self.panel.on_mode.assert_called_once_with(True)
        self.panel.commands["demo"].click()
        self.panel.on_command.assert_called_once_with({"action": "demo"})
        self.panel.observation.setChecked(True)
        self.panel.on_observation.assert_called_once_with(True)
        self.assertEqual(self.panel.observation_options(), {"interval": 5, "monitor": 1, "retain": False})
        self.assertEqual((self.panel.interval.minimum(), self.panel.interval.maximum()), (2, 60))

    def test_class_b_permissions_default_off_and_can_be_revoked(self):
        self.assertEqual(set(self.panel.permissions), {"dependencies", "venv", "app_config", "model_download"})
        self.assertFalse(any(widget.isChecked() for widget in self.panel.permissions.values()))
        self.panel.mode.setChecked(True)
        self.panel.permissions["dependencies"].setChecked(True)
        self.panel.on_permissions.assert_called_with("dependencies", True)
        self.panel.revoke_permissions.click()
        self.panel.on_permissions.assert_called_with("dependencies", False)
        self.assertFalse(self.panel.permissions["dependencies"].isChecked())

    def test_image_analysis_requires_explicit_mode_and_click(self):
        self.panel.commands["analyze"].click()
        self.panel.on_command.assert_not_called()
        self.panel.mode.setChecked(True)
        self.panel.on_command.assert_not_called()
        self.panel.commands["analyze"].click()
        self.panel.on_command.assert_called_once_with({"action": "analyze"})
        self.assertFalse(self.panel.observation.isChecked())

    def test_stop_shortcut_is_application_scoped_and_configurable(self):
        self.assertEqual(self.panel.stop_shortcut.context(), Qt.ShortcutContext.ApplicationShortcut)
        self.panel.set_emergency_shortcut("Ctrl+Shift+Escape")
        self.assertIn("Esc", self.panel.stop_shortcut.key().toString())
        self.panel.stop_shortcut.activated.emit()
        self.panel.on_stop.assert_called_once_with()
        self.assertFalse(self.panel.mode.isChecked())

    def test_failed_monitoring_start_and_revoke_restore_actual_state(self):
        self.panel.mode.setChecked(True)
        self.panel.on_observation.return_value = False
        self.panel.observation.setChecked(True)
        self.assertFalse(self.panel.observation.isChecked())
        self.panel.permissions["dependencies"].setChecked(True)
        self.panel.on_permissions.return_value = False
        self.panel.permissions["dependencies"].setChecked(False)
        self.assertTrue(self.panel.permissions["dependencies"].isChecked())

    def test_invalid_shortcut_preserves_working_emergency_binding(self):
        previous = self.panel.stop_shortcut.key().toString()
        for text in ("", "NotARealShortcut"):
            self.assertFalse(self.panel.set_emergency_shortcut(text))
            self.assertEqual(self.panel.stop_shortcut.key().toString(), previous)

    def test_worker_events_are_queued_redacted_and_output_is_bounded(self):
        with patch("core.repair_permissions.redact", side_effect=lambda value: value.replace("TEST_SECRET", "[REDACTED]")):
            thread = Thread(target=lambda: self.panel.publish({"task_id": "task-1", "phase": "testing",
                "diagnosis": "TEST_SECRET", "output": "TEST_SECRET\n" + "line\n" * 1400,
                "diff": "TEST_SECRET", "tests": "2 passed", "restart_status": "Not requested"}))
            thread.start()
            thread.join(1)
            self.app.processEvents()
        self.assertEqual(self.panel.phase.text(), "testing")
        self.assertNotIn("TEST_SECRET", self.panel.terminal.toPlainText() + self.panel.diff.toPlainText() + self.panel.diagnosis.toPlainText())
        self.assertLessEqual(self.panel.terminal.document().blockCount(), 1000)
        self.assertTrue(self.panel.terminal.isReadOnly())

    def test_history_rollback_uses_selected_id_and_approval_is_not_a_blanket_grant(self):
        self.panel.set_history([{"id": "attempt-1", "state": "failed", "summary": "Demo regression"}])
        self.app.processEvents()
        self.panel.mode.setChecked(True)
        self.panel.rollback_button.click()
        self.panel.on_rollback.assert_called_once_with("attempt-1")
        self.panel.publish({"approval_required": True, "operation": "delete_file", "scope": "one file",
                            "reason": "cleanup", "effects": "removes data"})
        self.app.processEvents()
        self.assertIn("per-operation", self.panel.approval.text().lower())
        self.assertNotIn("class_c", self.panel.permissions)

    def test_selected_history_shows_redacted_backups_tests_and_rollback(self):
        records = [{"id": "recent", "status": "repaired", "error": "A recent issue"},
                   {"id": "previous", "status": "rolled_back", "error": "Prior action failed",
                    "traceback": "oversized context " * 4000,
                    "attempts": [{"id": "prior-attempt", "status": "rolled_back", "verified": False,
                        "files": [{"path": "actions/example.py", "backup": "logs/repair/previous/prior-attempt/backups/00.bin",
                                   "rollback": "restored"}],
                        "diff_path": "logs/repair/previous/prior-attempt/changes.diff",
                        "commands": [{"phase": "testing", "exit_code": 1, "stderr": "password=do-not-display"}]}]}]
        self.panel.set_history(records)
        self.app.processEvents()
        self.panel.history.setCurrentIndex(1)
        self.assertEqual(self.panel.history.currentData(), "previous")
        self.assertTrue(self.panel.history.currentText().startswith("rolled_back"))
        self.assertIn("Prior action failed", self.panel.history.currentText())
        detail = self.panel.history_details.toPlainText()
        self.assertIn("backups/00.bin", detail)
        self.assertIn("changes.diff", detail)
        self.assertIn("restored", detail)
        self.assertIn('"exit_code": 1', detail)
        self.assertNotIn("do-not-display", detail)
        self.assertLessEqual(len(detail), 32000)
        self.assertTrue(self.panel.history_details.isReadOnly())
        self.panel.set_history([{"id": "new", "status": "recorded"}, *records])
        self.app.processEvents()
        self.assertEqual(self.panel.history.currentData(), "previous")
        self.assertIn("backups/00.bin", self.panel.history_details.toPlainText())

    def test_class_c_approval_displays_broker_capability_and_blocked_adapter(self):
        self.panel.publish({"approval_required": True, "class": "C", "capability": "administrator",
                            "reason": "Windows UAC approval required; token=hide-this", "scope": "one command"})
        self.app.processEvents()
        message = self.panel.approval.text()
        self.assertIn("administrator", message)
        self.assertIn("blocked", message.lower())
        self.assertIn("adapter", message.lower())
        self.assertIn("Windows UAC", message)
        self.assertNotIn("hide-this", message)

    def test_inspect_failure_uses_selected_history_after_restart(self):
        self.panel.set_history([{"id": "restored-demo", "status": "repaired", "source": "isolated_demo"}])
        self.app.processEvents()
        self.panel.mode.setChecked(True)
        self.panel.commands["inspect_failure"].click()
        self.panel.on_command.assert_called_once_with({"action": "inspect_failure", "failure_id": "restored-demo"})

    def test_mode_off_revokes_monitoring_and_disables_mutating_controls(self):
        self.panel.mode.setChecked(True)
        self.panel.observation.setChecked(True)
        self.panel.mode.setChecked(False)
        self.assertFalse(self.panel.observation.isChecked())
        self.panel.on_mode.assert_called_with(False)
        self.assertFalse(self.panel.restart_button.isEnabled())
        self.assertFalse(self.panel.rollback_button.isEnabled())

    def test_runtime_stop_event_does_not_emit_pause_callback_back_to_runtime(self):
        self.panel.mode.setChecked(True)
        self.panel.observation.setChecked(True)
        self.panel.on_observation.reset_mock()
        self.panel.publish({"mode": False, "monitoring": False, "phase": "Stopped"})
        self.app.processEvents()
        self.panel.on_observation.assert_not_called()
        self.assertFalse(self.panel.observation.isChecked())


if __name__ == "__main__":
    unittest.main()
