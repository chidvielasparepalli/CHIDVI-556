"""Native hotkey tests use fake Win32 calls; never register a real shortcut."""
import ctypes
from ctypes import wintypes
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from core.repair_hotkey import NativeRepairHotkey, key_chord
from core.repair_ui import RepairPanel


class NativeHotkeyTests(unittest.TestCase):
    def setUp(self):
        self.win32 = Mock()
        self.win32.RegisterHotKey.return_value = 1
        self.win32.UnregisterHotKey.return_value = 1
        self.app = Mock()
        self.stop = Mock()
        self.hotkey = NativeRepairHotkey(self.stop, application=self.app, user32=self.win32)
        self.addCleanup(self.hotkey.close)

    def test_supported_single_chords_and_invalid_keys(self):
        self.assertEqual(key_chord("Ctrl+Alt+Escape"), (0x0003, 0x1B))
        self.assertEqual(key_chord("Ctrl+Shift+F10"), (0x0006, 0x79))
        self.assertEqual(key_chord("Alt+X"), (0x0001, 0x58))
        for value in ("", "Ctrl+X, Ctrl+C", "NotARealShortcut", "Ctrl++"):
            with self.assertRaises(ValueError):
                key_chord(value)

    def test_register_replace_and_unregister_match_thread_and_id(self):
        self.assertTrue(self.hotkey.set_sequence("Ctrl+Alt+Escape"))
        identity = self.hotkey.identifier
        self.win32.RegisterHotKey.assert_called_once_with(None, identity, 0x4003, 0x1B)
        self.app.installNativeEventFilter.assert_called_once_with(self.hotkey)
        self.assertTrue(self.hotkey.set_sequence("Ctrl+Shift+F10"))
        self.win32.UnregisterHotKey.assert_called_once_with(None, identity)
        self.win32.RegisterHotKey.assert_called_with(None, identity, 0x4006, 0x79)
        self.hotkey.close()
        self.assertEqual(self.win32.UnregisterHotKey.call_count, 2)
        self.assertFalse(self.hotkey.registered)
        self.hotkey.close()
        self.assertEqual(self.win32.UnregisterHotKey.call_count, 2)

    def test_only_matching_registered_message_routes_stop(self):
        self.hotkey.set_sequence("Ctrl+Alt+Escape")
        message = wintypes.MSG()
        message.message = 0x0312
        message.wParam = self.hotkey.identifier
        message.lParam = (0x1B << 16) | 0x0003
        pointer = ctypes.addressof(message)
        self.assertEqual(self.hotkey.nativeEventFilter(b"other_event", pointer), (False, 0))
        message.wParam += 1
        self.assertEqual(self.hotkey.nativeEventFilter(b"windows_dispatcher_MSG", pointer), (False, 0))
        message.wParam -= 1
        message.lParam = (0x79 << 16) | 0x0006
        self.assertEqual(self.hotkey.nativeEventFilter(b"windows_dispatcher_MSG", pointer), (False, 0))
        message.lParam = (0x1B << 16) | 0x0003
        self.assertEqual(self.hotkey.nativeEventFilter(b"windows_dispatcher_MSG", pointer), (True, 0))
        self.stop.assert_called_once_with()
        self.hotkey.close()
        self.assertEqual(self.hotkey.nativeEventFilter(b"windows_dispatcher_MSG", pointer), (False, 0))

    def test_registration_failure_removes_filter_and_does_not_claim_success(self):
        self.win32.RegisterHotKey.return_value = 0
        with self.assertRaises(OSError):
            self.hotkey.set_sequence("Ctrl+Alt+Escape")
        self.assertFalse(self.hotkey.registered)
        self.app.removeNativeEventFilter.assert_called_once_with(self.hotkey)

    def test_failed_unregister_preserves_old_registration_and_refuses_replacement(self):
        self.hotkey.set_sequence("Ctrl+Alt+Escape")
        self.win32.UnregisterHotKey.return_value = 0
        with self.assertRaises(OSError):
            self.hotkey.set_sequence("Ctrl+Shift+F10")
        self.assertTrue(self.hotkey.registered)
        self.assertEqual(self.win32.RegisterHotKey.call_count, 1)
        self.win32.UnregisterHotKey.return_value = 1


class PanelHotkeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.patch = patch("core.repair_ui.NativeRepairHotkey")
        self.factory = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.native = self.factory.return_value
        self.panel = RepairPanel()
        self.addCleanup(self.panel.close)
        self.addCleanup(lambda: self.panel.set_global_hotkey(None))
        self.panel.on_stop = Mock()

    def test_worker_request_is_applied_on_gui_thread(self):
        threads = []
        self.native.set_sequence.side_effect = lambda _: threads.append(QThread.currentThread()) or True
        worker = Thread(target=lambda: self.panel.set_global_hotkey("Ctrl+Alt+Escape"))
        worker.start()
        worker.join(1)
        self.native.set_sequence.assert_not_called()
        self.app.processEvents()
        self.assertEqual(threads, [self.app.thread()])
        self.assertIn("active", self.panel.hotkey_status.text().lower())
        self.factory.call_args.args[0]()
        self.panel.on_stop.assert_called_once_with()
        self.native.set_sequence.assert_called_with(None)

    def test_registration_error_is_visible_and_application_shortcut_stays(self):
        previous = self.panel.stop_shortcut.key().toString()
        self.native.set_sequence.side_effect = OSError("Shortcut already registered by another application")
        self.assertFalse(self.panel.set_global_hotkey("Ctrl+Alt+Escape"))
        self.assertIn("unavailable", self.panel.hotkey_status.text().lower())
        self.assertIn("already registered", self.panel.status.text())
        self.assertEqual(self.panel.stop_shortcut.key().toString(), previous)

    def test_hiding_panel_keeps_global_stop_but_destruction_releases_it(self):
        self.panel.set_global_hotkey("Ctrl+Alt+Escape")
        self.panel.close()
        self.native.set_sequence.assert_called_once_with("Ctrl+Alt+Escape")
        self.panel.destroyed.emit()
        self.native.dispose.assert_called_once_with()

    def test_live_sharing_registers_while_repair_off_and_stop_preserves_until_sharing_ends(self):
        self.assertFalse(self.panel.mode.isChecked())
        self.panel.set_live_sharing_active(True)
        self.assertTrue(self.panel.live_sharing_active)
        self.native.set_sequence.assert_called_with("Ctrl+Alt+Escape")
        self.factory.assert_called_once()
        self.panel.set_mode(False)
        self.native.set_sequence.assert_called_with("Ctrl+Alt+Escape")
        self.factory.call_args.args[0]()
        self.panel.on_stop.assert_called_once_with()
        self.native.set_sequence.assert_called_with("Ctrl+Alt+Escape")
        self.panel.set_live_sharing_active(False)
        self.native.set_sequence.assert_called_with(None)

    def test_shared_hotkey_updates_are_queued_and_repair_protection_survives_sharing_end(self):
        worker = Thread(target=lambda: self.panel.set_live_sharing_active(True))
        worker.start()
        worker.join(1)
        self.native.set_sequence.assert_not_called()
        self.app.processEvents()
        self.assertTrue(self.panel.live_sharing_active)
        self.panel.set_mode(True)
        self.panel.set_live_sharing_active(False)
        self.native.set_sequence.assert_called_with("Ctrl+Alt+Escape")
        self.panel.set_mode(False)
        self.native.set_sequence.assert_called_with(None)

    def test_shared_shortcut_change_and_app_quit_use_the_same_registrar(self):
        self.panel.set_live_sharing_active(True)
        self.assertTrue(self.panel.set_emergency_shortcut("Ctrl+Shift+F10"))
        self.native.set_sequence.assert_called_with("Ctrl+Shift+F10")
        self.factory.assert_called_once()
        self.panel._release_global_hotkey()
        self.native.set_sequence.assert_called_with(None)


if __name__ == "__main__":
    unittest.main()
