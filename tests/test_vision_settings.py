"""Cloud vision settings use temporary configuration and offscreen Qt only."""
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton, QWidget
from PyQt6.QtGui import QColor, QPalette
from core.operator_ui import OperatorPanel
from memory import config_manager as config
from ui import MainWindow


class TemporaryConfig(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_file = Path(self.directory.name) / "api_keys.json"
        for name, value in (("CONFIG_DIR", self.config_file.parent),
                            ("CONFIG_FILE", self.config_file)):
            item = patch.object(config, name, value)
            item.start()
            self.addCleanup(item.stop)

    def write_config(self, value):
        self.config_file.write_text(json.dumps(value), encoding="utf-8")


class CloudVisionConfigTests(TemporaryConfig):
    def test_default_is_off_without_creating_config(self):
        self.assertIs(config.get_cloud_vision_enabled(), False)
        self.assertFalse(self.config_file.exists())

    def test_only_explicit_json_true_enables_cloud(self):
        for value in (True, False, "true", "false", 1, 0, None, [], {}):
            with self.subTest(value=value):
                self.write_config({"cloud_vision_enabled": value})
                self.assertIs(config.get_cloud_vision_enabled(), value is True)
        for value in ([], None, True):
            self.write_config(value)
            self.assertIs(config.get_cloud_vision_enabled(), False)

    def test_save_preserves_credentials_and_unrelated_nested_settings(self):
        original = {"gemini_api_key": "test-key", "voice_name": "Kore",
                    "plugin_config": {"printer": {"token": "test-secret"}}}
        self.write_config(original)
        for enabled in (True, False):
            config.save_cloud_vision_enabled(enabled)
            self.assertEqual(json.loads(self.config_file.read_text(encoding="utf-8")),
                             {**original, "cloud_vision_enabled": enabled})
            self.assertIs(config.get_cloud_vision_enabled(), enabled)

    def test_save_rejects_non_boolean_without_touching_config(self):
        self.write_config({"gemini_api_key": "test-key"})
        original = self.config_file.read_bytes()
        for value in (1, 0, "true", None):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                config.save_cloud_vision_enabled(value)
            self.assertEqual(self.config_file.read_bytes(), original)

    def test_malformed_config_is_not_overwritten(self):
        for original in (b'{"gemini_api_key":', b'[]', b'null'):
            self.config_file.write_bytes(original)
            with self.assertRaises(ValueError):
                config.save_cloud_vision_enabled(True)
            self.assertEqual(self.config_file.read_bytes(), original)

    def test_failed_atomic_replace_preserves_original_and_cleans_temporary_file(self):
        self.write_config({"gemini_api_key": "test-key", "cloud_vision_enabled": False})
        original = self.config_file.read_bytes()
        with patch("os.replace", side_effect=PermissionError("read only")):
            with self.assertRaises(PermissionError):
                config.save_cloud_vision_enabled(True)
        self.assertEqual(self.config_file.read_bytes(), original)
        self.assertEqual(list(self.config_file.parent.iterdir()), [self.config_file])


class LiveSharingConfigTests(TemporaryConfig):
    defaults = {"interval": 2.0, "max_per_minute": 12, "analysis_per_minute": 4,
                "max_width": 1280, "max_height": 720, "monitor": 1,
                "change_only": True, "session_minutes": 30}

    def test_defaults_and_startup_are_read_only_and_independent_of_cloud_permission(self):
        self.assertEqual(config.get_live_sharing_settings(), self.defaults)
        self.assertIs(config.get_live_sharing_startup(), False)
        self.assertFalse(self.config_file.exists())
        self.write_config({"cloud_vision_enabled": True})
        self.assertIs(config.get_live_sharing_startup(), False)
        result = config.get_live_sharing_settings()
        result["interval"] = 30
        self.assertEqual(config.get_live_sharing_settings(), self.defaults)

    def test_invalid_settings_are_rejected_without_touching_existing_credentials(self):
        self.write_config({"gemini_api_key": "test-key"})
        original = self.config_file.read_bytes()
        invalid = [{"interval": value} for value in (0, .5, 31, 10 ** 500, True, "2", float("nan"), float("inf"))]
        invalid += [{"max_per_minute": 61}, {"analysis_per_minute": 21}, {"monitor": -1},
                    {"max_width": 1921}, {"max_height": 239}, {"session_minutes": 121},
                    {"change_only": 1}, {"unknown": True}, None, []]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                config.save_live_sharing_settings(value)
            self.assertEqual(self.config_file.read_bytes(), original)

    def test_settings_and_startup_preserve_credentials_and_each_other(self):
        original = {"gemini_api_key": "test-key", "plugin_config": {"phone": {"token": "test-secret"}}}
        self.write_config(original)
        expected = {**self.defaults, "interval": 3.5, "monitor": 0, "change_only": False}
        config.save_live_sharing_settings(expected)
        config.save_live_sharing_startup(True)
        self.assertEqual(config.get_live_sharing_settings(), expected)
        self.assertIs(config.get_live_sharing_startup(), True)
        self.assertEqual(json.loads(self.config_file.read_text()),
                         {**original, "live_screen_sharing": expected, "live_screen_sharing_startup": True})
        self.assertIs(config.get_cloud_vision_enabled(), False)

    def test_startup_requires_explicit_boolean_and_invalid_saved_settings_use_defaults(self):
        for value in ("true", 1, None, []):
            self.write_config({"live_screen_sharing_startup": value,
                               "live_screen_sharing": {"interval": 0}})
            self.assertIs(config.get_live_sharing_startup(), False)
            self.assertEqual(config.get_live_sharing_settings(), self.defaults)
            original = self.config_file.read_bytes()
            with self.assertRaises((TypeError, ValueError)):
                config.save_live_sharing_startup(value)
            self.assertEqual(self.config_file.read_bytes(), original)

    def test_failed_live_save_is_atomic_and_malformed_config_is_preserved(self):
        self.write_config({"gemini_api_key": "test-key"})
        original = self.config_file.read_bytes()
        with patch("os.replace", side_effect=PermissionError("private path")):
            with self.assertRaises(PermissionError):
                config.save_live_sharing_settings(self.defaults)
        self.assertEqual(self.config_file.read_bytes(), original)
        self.assertEqual(list(self.config_file.parent.iterdir()), [self.config_file])
        self.config_file.write_bytes(b'{"gemini_api_key":')
        with self.assertRaises(ValueError):
            config.save_live_sharing_startup(True)
        self.assertEqual(self.config_file.read_bytes(), b'{"gemini_api_key":')

    def test_validation_accepts_contract_boundaries_and_concurrent_consent_saves_preserve_all_fields(self):
        low = {"interval": 1, "max_per_minute": 1, "analysis_per_minute": 1,
               "max_width": 320, "max_height": 240, "monitor": 0,
               "change_only": False, "session_minutes": 1}
        high = {"interval": 30, "max_per_minute": 60, "analysis_per_minute": 20,
                "max_width": 1920, "max_height": 1080, "monitor": 64,
                "change_only": True, "session_minutes": 120}
        for settings in (low, high):
            self.assertEqual(config.validate_live_sharing_settings(settings), settings)
        self.write_config({"gemini_api_key": "test-key"})
        threads = [threading.Thread(target=callback, args=(value,)) for callback, value in (
            (config.save_cloud_vision_enabled, True), (config.save_live_sharing_startup, True),
            (config.save_live_sharing_settings, high))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        saved = json.loads(self.config_file.read_text())
        self.assertEqual(saved, {"gemini_api_key": "test-key", "cloud_vision_enabled": True,
                                "live_screen_sharing_startup": True, "live_screen_sharing": high})


class CloudVisionUITests(TemporaryConfig):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def panel(self):
        panel = OperatorPanel()
        self.addCleanup(panel.close)
        self.addCleanup(panel.overlay.close)
        return panel

    def test_toolbar_initializes_from_saved_setting_without_writing(self):
        for value in (True, False, "true", 1):
            self.write_config({"cloud_vision_enabled": value})
            with patch.object(config, "save_cloud_vision_enabled") as save:
                panel = self.panel()
                self.assertIs(panel.cloud_vision.isChecked(), value is True)
                save.assert_not_called()

    def test_dialog_and_toolbar_share_persistence_and_runtime_callback(self):
        panel = self.panel()
        observed = []
        panel.on_cloud_vision = lambda value: observed.append((value, config.get_cloud_vision_enabled()))
        dialog = panel.show_vision_privacy()
        self.addCleanup(dialog.close)
        self.assertIn("external AI provider", dialog.explanation.text())
        self.assertIn("review", dialog.explanation.text().lower())
        dialog.cloud_vision.setChecked(True)
        self.assertTrue(panel.cloud_vision.isChecked())
        self.assertEqual(observed, [(True, True)])
        panel.cloud_vision.setChecked(False)
        self.assertFalse(dialog.cloud_vision.isChecked())
        self.assertEqual(observed, [(True, True), (False, False)])

    def test_save_error_restores_both_controls_and_does_not_enable_runtime(self):
        panel = self.panel()
        panel.on_cloud_vision = Mock()
        dialog = panel.show_vision_privacy()
        self.addCleanup(dialog.close)
        with patch.object(config, "save_cloud_vision_enabled", side_effect=PermissionError("private path")):
            dialog.cloud_vision.setChecked(True)
        self.assertFalse(panel.cloud_vision.isChecked())
        self.assertFalse(dialog.cloud_vision.isChecked())
        self.assertIn("could not be saved", dialog.error.text().lower())
        self.assertIn("could not be saved", panel.cloud_vision_error.text().lower())
        self.assertNotIn("private path", dialog.error.text())
        panel.on_cloud_vision.assert_not_called()
        self.assertFalse(self.config_file.exists())

    def test_runtime_sync_does_not_persist_or_call_runtime_again(self):
        panel = self.panel()
        panel.on_cloud_vision = Mock()
        with patch.object(config, "save_cloud_vision_enabled") as save:
            panel.set_cloud_vision_state(True)
        self.assertTrue(panel.cloud_vision.isChecked())
        save.assert_not_called()
        panel.on_cloud_vision.assert_not_called()

    def test_save_error_has_readable_contrast_on_light_and_dark_dialogs(self):
        def luminance(color):
            channels = [color.redF(), color.greenF(), color.blueF()]
            linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                      for value in channels]
            return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        original_palette = self.app.palette()
        self.addCleanup(self.app.setPalette, original_palette)
        for background in ("#efefef", "#101010"):
            with self.subTest(background=background):
                palette = self.app.palette()
                palette.setColor(QPalette.ColorRole.Window, QColor(background))
                self.app.setPalette(palette)
                panel = self.panel()
                dialog = panel.show_vision_privacy()
                self.addCleanup(dialog.close)
                panel._cloud_error("Cloud Vision could not be saved. Previous setting kept.")
                dialog.error.ensurePolished()
                foreground = dialog.error.palette().color(QPalette.ColorRole.WindowText)
                values = sorted((luminance(foreground), luminance(dialog.palette().window().color())))
                self.assertGreaterEqual((values[1] + 0.05) / (values[0] + 0.05), 4.5)

    def test_real_customize_button_opens_vision_privacy_settings(self):
        self.write_config({"cloud_vision_enabled": True})
        panel = self.panel()
        central = QWidget()
        central.resize(980, 700)
        self.addCleanup(central.close)
        window = SimpleNamespace(_customize_overlay=None, centralWidget=lambda: central,
                                 _preview_ui_color=Mock(), _apply_name_update=Mock(),
                                 _operator_panel=panel)
        window._open_vision_privacy = lambda: MainWindow._open_vision_privacy(window)
        with patch("ui._read_full_config", return_value={}):
            MainWindow._open_customize(window)
        buttons = {button.text().replace("&&", "&"): button for button in window._customize_overlay.findChildren(QPushButton)}
        self.assertIn("Vision & Privacy…", buttons)
        buttons["Vision & Privacy…"].click()
        dialog = panel._vision_privacy_dialog
        self.addCleanup(dialog.close)
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.windowTitle(), "Vision & Privacy")
        self.assertTrue(dialog.cloud_vision.isChecked())


if __name__ == "__main__":
    unittest.main()
