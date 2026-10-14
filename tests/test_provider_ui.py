"""Provider settings run offscreen with an in-memory vault and fake adapters."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QMessageBox, QPushButton, QWidget
from core.provider_ui import ProviderDialog


class Store:
    def __init__(self):
        self.profiles, self.keys, self.roles, self.overrides = {}, {}, {}, {}
        self.credential = Mock(side_effect=lambda identity: self.keys.get(identity))
        self.status_writes = []

    def list_profiles(self):
        return [self.get_profile(identity) for identity in self.profiles]

    def get_profile(self, identity):
        item = self.profiles.get(identity)
        return {**copy.deepcopy(item), "has_credential": identity in self.keys} if item else None

    def save_profile(self, profile, key=None):
        profile = dict(profile)
        identity = profile.setdefault("id", "profile-" + str(len(self.profiles) + 1))
        self.profiles[identity] = profile
        if key is not None:
            if key:
                self.keys[identity] = key
            else:
                self.keys.pop(identity, None)
        return self.get_profile(identity)

    def remove_profile(self, identity):
        self.profiles.pop(identity)
        self.keys.pop(identity, None)
        self.roles = {key: value for key, value in self.roles.items() if value != identity}
        return True

    def selected(self, role="chat", task_kind=None):
        return self.get_profile(self.overrides.get(task_kind) or self.roles.get(role))

    def set_role(self, role, identity):
        self.roles[role] = identity

    def set_override(self, task, identity):
        self.overrides[task] = identity

    def has_configuration(self):
        item = self.selected()
        return bool(item and (item["has_credential"] or item.get("auth_type") == "none"))

    def snapshot(self):
        return {"profiles": self.list_profiles(), "roles": dict(self.roles), "overrides": dict(self.overrides)}

    def record_status(self, identity, connected, error="", usage=None, *, expected_profile=None, expected_credential=None):
        saved = self.profiles.get(identity)
        if saved is None:
            return None
        if expected_profile is not None and any(saved.get(field) != expected_profile.get(field)
                                                for field in ("id", "provider", "base_url", "model", "auth_type")):
            return None
        if self.keys.get(identity) != expected_credential:
            return None
        self.status_writes.append(identity)
        saved.update(status="connected" if connected else "error", connected=connected,
                     last_error=error, last_tested=1_800_000_000)
        if usage is not None:
            saved["last_usage"] = usage
        return self.get_profile(identity)


def registry():
    entries = {identity: {"label": label, "default_base_url": "https://" + identity + ".example/v1",
                         "key_required": identity != "compatible", "auth_type": "api_key"}
               for identity, label in (("gemini", "Google Gemini"), ("openai", "OpenAI"),
                                       ("anthropic", "Anthropic"), ("xai", "xAI"),
                                       ("deepseek", "DeepSeek"), ("nvidia", "NVIDIA"),
                                       ("opencode", "OpenCode"), ("compatible", "Compatible endpoint"))}
    def capabilities(provider, model, overrides=None):
        result = dict.fromkeys(("text", "tools", "vision", "streaming", "live_audio", "tts", "embeddings"), False)
        if model:
            result.update(text=True, streaming=True)
        if model == "vision-model":
            result["vision"] = True
        return {**result, **(overrides or {})}
    return SimpleNamespace(PROVIDERS=entries, model_capabilities=capabilities,
                           create_adapter=Mock(), provider_label=lambda value: entries[value]["label"])


class ProviderUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.store, self.registry = Store(), registry()
        self.dialogs = []
        self.addCleanup(self.close_dialogs)

    def close_dialogs(self):
        for dialog in self.dialogs:
            dialog.close()
        self.app.processEvents()

    def dialog(self, **kwargs):
        item = ProviderDialog(store=self.store, registry=self.registry, **kwargs)
        self.dialogs.append(item)
        return item

    def fill(self, dialog, provider="openai", model="chat-model", key="test-private-key"):
        dialog.provider.setCurrentIndex(dialog.provider.findData(provider))
        dialog.profile_name.setText("My provider")
        dialog.model.setCurrentText(model)
        dialog.api_key.setText(key)

    def pump(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Provider UI worker did not complete")

    def test_provider_selection_and_masked_key_save_never_return_credentials(self):
        dialog = self.dialog()
        changed = []
        dialog.changed.connect(changed.append)
        self.assertEqual(dialog.provider.count(), 8)
        self.assertEqual(dialog.api_key.echoMode(), QLineEdit.EchoMode.Password)
        self.assertTrue(dialog.model.isEditable())
        self.fill(dialog)
        dialog.default_chat.setChecked(True)
        dialog.save_profile.click()
        self.assertEqual(dialog.api_key.text(), "")
        self.assertTrue(self.store.has_configuration())
        self.assertEqual(self.store.selected()["provider"], "openai")
        self.assertEqual(changed, [self.store.selected()["id"]])
        self.assertNotIn("test-private-key", dialog.status.text())
        self.store.credential.assert_not_called()

    def test_blank_key_update_preserves_saved_credential_and_revoke_is_explicit(self):
        dialog = self.dialog()
        self.fill(dialog)
        dialog.save_profile.click()
        identity = dialog.profile_picker.currentData()
        dialog.profile_name.setText("Renamed")
        dialog.save_profile.click()
        self.assertEqual(self.store.keys[identity], "test-private-key")
        dialog.revoke_key.click()
        self.assertNotIn(identity, self.store.keys)
        self.assertEqual(dialog.api_key.text(), "")

    def test_changed_provider_needs_fresh_key_and_never_reuses_other_provider_credential(self):
        dialog = self.dialog()
        self.fill(dialog)
        dialog.save_profile.click()
        identity = dialog.profile_picker.currentData()
        dialog.provider.setCurrentIndex(dialog.provider.findData("anthropic"))
        dialog.model.setCurrentText("chat-model")
        dialog.save_profile.click()
        self.assertEqual(self.store.get_profile(identity)["provider"], "openai")
        self.assertIn("new key", dialog.status.text().lower())
        dialog.test_connection.click()
        self.registry.create_adapter.assert_not_called()
        self.store.credential.assert_not_called()

    def test_request_limits_and_explicit_vision_probe_are_separate_from_text(self):
        dialog = self.dialog()
        self.fill(dialog, model="vision-model")
        dialog.max_output_tokens.setValue(2048)
        dialog.session_token_warning.setValue(6000)
        dialog.save_profile.click()
        self.assertEqual(self.store.selected()["limits"], {"max_output_tokens": 2048, "session_token_warning": 6000})
        calls = []
        async def test(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "capability": kwargs["capability"], "usage": {"input_tokens": 10, "secret": "test-private-key"}}
        self.registry.create_adapter.return_value = SimpleNamespace(test=test)
        dialog.test_kind.setCurrentIndex(dialog.test_kind.findData("vision"))
        dialog.test_connection.click()
        self.pump(lambda: dialog.test_connection.isEnabled())
        self.assertEqual(calls[0]["capability"], "vision")
        self.assertIn("vision", dialog.status.text().lower())
        self.assertNotIn("test-private-key", dialog.usage.text())
        self.assertNotIn("secret", dialog.usage.text())

    def test_assignments_filter_capabilities_and_support_task_overrides(self):
        chat = self.store.save_profile({"name": "Text only", "provider": "openai", "model": "chat-model", "auth_type": "api_key", "capabilities": {}}, "fake")
        vision = self.store.save_profile({"name": "Vision", "provider": "gemini", "model": "vision-model", "auth_type": "api_key", "capabilities": {}}, "fake")
        dialog = self.dialog()
        self.assertGreaterEqual(dialog.role_boxes["chat"].findData(chat["id"]), 0)
        self.assertEqual(dialog.role_boxes["vision"].findData(chat["id"]), -1)
        self.assertGreaterEqual(dialog.role_boxes["vision"].findData(vision["id"]), 0)
        self.assertEqual(dialog.role_boxes["live_audio"].count(), 1)
        dialog.role_boxes["chat"].setCurrentIndex(dialog.role_boxes["chat"].findData(chat["id"]))
        dialog.override_boxes["planning"].setCurrentIndex(dialog.override_boxes["planning"].findData(vision["id"]))
        dialog.save_assignments.click()
        self.assertEqual(self.store.roles["chat"], chat["id"])
        self.assertEqual(self.store.overrides["planning"], vision["id"])

    def test_fetch_models_runs_off_gui_thread_and_preserves_manual_entry(self):
        dialog = self.dialog()
        self.fill(dialog, model="manual-model")
        workers = []
        gate = threading.Event()
        async def models():
            workers.append(threading.current_thread())
            await asyncio.to_thread(gate.wait, 2)
            return [{"id": "listed-model", "label": "Listed model", "capabilities": {"text": True}, "pricing": None, "limits": None}]
        self.registry.create_adapter.return_value = SimpleNamespace(models=models)
        started = time.monotonic()
        dialog.fetch_models.click()
        self.assertLess(time.monotonic() - started, .2)
        self.assertFalse(dialog.fetch_models.isEnabled())
        gate.set()
        self.pump(lambda: dialog.fetch_models.isEnabled())
        self.assertIsNot(workers[0], threading.main_thread())
        self.assertEqual(dialog.model.currentText(), "manual-model")
        self.assertGreaterEqual(dialog.model.findText("listed-model"), 0)
        self.assertIn("unknown", dialog.usage.text().lower())

    def test_worker_error_hides_keys_and_cannot_apply_to_changed_profile(self):
        dialog = self.dialog()
        self.fill(dialog)
        async def failed(*args, **kwargs):
            raise RuntimeError("test-private-key")
        self.registry.create_adapter.return_value = SimpleNamespace(test=failed)
        dialog.test_connection.click()
        self.pump(lambda: dialog.test_connection.isEnabled())
        self.assertNotIn("test-private-key", dialog.status.text())
        self.assertIn("failed", dialog.status.text().lower())
        gate = threading.Event()
        async def slow(*args, **kwargs):
            await asyncio.to_thread(gate.wait, 2)
            return {"ok": True, "model": "old-model", "capability": "text"}
        self.registry.create_adapter.return_value = SimpleNamespace(test=slow)
        dialog.test_connection.click()
        dialog.model.setCurrentText("changed-model")
        gate.set()
        self.pump(lambda: dialog.test_connection.isEnabled())
        self.assertNotIn("passed", dialog.status.text().lower())

    def test_saved_connection_status_usage_and_date_survive_reopening(self):
        dialog = self.dialog()
        self.fill(dialog)
        dialog.save_profile.click()
        async def passed(**kwargs):
            return {"ok": True, "capability": "text", "usage": {"input_tokens": 7, "output_tokens": 3}}
        self.registry.create_adapter.return_value = SimpleNamespace(test=passed)
        dialog.test_connection.click()
        self.pump(lambda: dialog.test_connection.isEnabled())
        self.assertEqual(len(self.store.status_writes), 1)
        reopened = self.dialog()
        self.assertIn("connected", reopened.status.text().lower())
        self.assertIn("last checked", reopened.status.text().lower())
        self.assertIn("input_tokens", reopened.usage.text())
        self.assertIn("7", reopened.usage.text())
        self.assertIn("pricing: unknown", reopened.usage.text().lower())
        identity = reopened.profile_picker.currentData()
        self.store.profiles[identity].update(status="error", last_error="password=private-value connection rejected")
        failed = self.dialog()
        self.assertIn("error", failed.status.text().lower())
        self.assertIn("connection rejected", failed.status.text())
        self.assertNotIn("private-value", failed.status.text())

    def test_unsaved_key_or_model_probe_cannot_mark_saved_profile_verified(self):
        dialog = self.dialog()
        self.fill(dialog)
        dialog.save_profile.click()
        async def passed(**kwargs):
            return {"ok": True, "capability": "text", "usage": {"input_tokens": 1}}
        self.registry.create_adapter.return_value = SimpleNamespace(test=passed)
        for field, value in ((dialog.api_key, "unsaved-other-key"), (dialog.model, "different-model")):
            with self.subTest(value=value):
                dialog._load_selected()
                field.setText(value) if isinstance(field, QLineEdit) else field.setCurrentText(value)
                dialog.test_connection.click()
                self.pump(lambda: dialog.test_connection.isEnabled())
                self.assertEqual(self.store.status_writes, [])
                self.assertIn("not saved", dialog.status.text().lower())

    def test_saved_credential_rotation_while_probe_runs_prevents_status_write(self):
        dialog = self.dialog()
        self.fill(dialog)
        dialog.save_profile.click()
        identity = dialog.profile_picker.currentData()
        gate, started = threading.Event(), threading.Event()
        payloads = []
        dialog.operation_finished.connect(payloads.append)
        async def passed(**kwargs):
            started.set()
            await asyncio.to_thread(gate.wait, 2)
            return {"ok": True, "capability": "text"}
        self.registry.create_adapter.return_value = SimpleNamespace(test=passed)
        dialog.test_connection.click()
        self.pump(started.is_set)
        self.store.keys[identity] = "rotated-private-key"
        gate.set()
        self.pump(lambda: dialog.test_connection.isEnabled())
        self.assertEqual(self.store.status_writes, [])
        self.assertNotIn("test-private-key", json.dumps(payloads))
        self.assertNotIn("rotated-private-key", json.dumps(payloads))

    def test_close_cancels_active_worker_without_waiting_for_network_timeout(self):
        dialog = self.dialog()
        self.fill(dialog)
        dialog.present()
        self.app.processEvents()
        started, cancelled = threading.Event(), threading.Event()
        async def slow(**kwargs):
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()
        self.registry.create_adapter.return_value = SimpleNamespace(test=slow)
        dialog.test_connection.click()
        self.pump(started.is_set)
        dialog.close()
        self.pump(cancelled.is_set)
        self.assertEqual(dialog.api_key.text(), "")
        self.assertNotIn("passed", dialog.status.text().lower())

    def test_import_is_explicit_and_failure_does_not_claim_success(self):
        self.store.migrate_legacy = Mock(return_value={"ok": False, "message": "private-server-message"})
        dialog = self.dialog()
        self.store.migrate_legacy.assert_not_called()
        dialog.import_legacy.click()
        self.store.migrate_legacy.assert_called_once_with()
        self.assertIn("failed", dialog.status.text().lower())
        self.assertNotIn("private-server-message", dialog.status.text())

    def test_real_registry_and_store_round_trip_only_write_temporary_config(self):
        from core import providers
        from core.credential_store import CredentialStore
        from core.provider_config import ProviderStore
        from tests.test_credential_store import FakeProtector
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            vault = CredentialStore(folder / "vault.dpapi", FakeProtector())
            store = ProviderStore(folder, vault=vault)
            dialog = ProviderDialog(store=store, registry=providers, first_run=True)
            self.dialogs.append(dialog)
            self.fill(dialog, provider="openai", model="gpt-4.1")
            self.assertFalse(dialog.capabilities["live_audio"].isEnabled())
            dialog.save_profile.click()
            self.assertTrue(store.has_configuration(), dialog.status.text())
            self.assertEqual(store.selected()["provider"], "openai")
            self.assertEqual(dialog.api_key.text(), "")
            self.assertNotIn("test-private-key", store.path.read_text())
            self.assertIsNone(store.selected("live_audio"))
            self.assertTrue(dialog.finish_setup.isEnabled())
            async def passed(**kwargs):
                return {"ok": True, "capability": "text", "usage": {"input_tokens": 5}}
            with patch.object(providers, "create_adapter", return_value=SimpleNamespace(test=passed)):
                dialog.test_connection.click()
                self.pump(lambda: dialog.test_connection.isEnabled())
            self.assertEqual(store.selected()["status"], "connected", dialog.status.text())
            reopened = ProviderDialog(store=store, registry=providers)
            self.dialogs.append(reopened)
            self.assertIn("connected", reopened.status.text())
            self.assertIn("last checked", reopened.status.text().lower())
            self.assertIn("input_tokens", reopened.usage.text())
            reopened.close()
            live = store.save_profile({"id": "live", "name": "Live audio", "provider": "gemini", "model": "gemini-3.1-flash-live-preview"}, key="synthetic-live-key")
            dialog._refresh_assignments()
            self.assertGreaterEqual(dialog.role_boxes["live_audio"].findData(live["id"]), 0)
            self.assertEqual(dialog.role_boxes["vision"].findData(live["id"]), -1)
            self.assertEqual(dialog.override_boxes["vision"].findData(live["id"]), -1)
            dialog.close()

    def test_remove_requires_confirmation_and_does_not_remove_when_declined(self):
        dialog = self.dialog()
        self.fill(dialog)
        dialog.save_profile.click()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            dialog.remove_profile.click()
        self.assertEqual(len(self.store.list_profiles()), 1)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            dialog.remove_profile.click()
        self.assertEqual(self.store.list_profiles(), [])

    def test_first_run_accepts_non_gemini_chat_and_preserves_system_fields(self):
        dialog = self.dialog(first_run=True, initial_system={"assistant_name": "CHIDVI", "user_name": "Tester", "os_system": "windows"})
        dialog.on_setup = Mock(return_value=True)
        self.assertFalse(dialog.finish_setup.isEnabled())
        self.fill(dialog, provider="anthropic")
        dialog.default_chat.setChecked(True)
        dialog.save_profile.click()
        self.assertTrue(dialog.finish_setup.isEnabled())
        dialog.finish_setup.click()
        dialog.on_setup.assert_called_once_with({"assistant_name": "CHIDVI", "user_name": "Tester", "os_system": "windows"})
        self.assertIsNone(self.store.selected("live_audio"))

    def test_small_screen_dialog_scrolls_and_saved_values_not_changed_by_opening(self):
        before = self.store.snapshot()
        dialog = self.dialog()
        with patch.object(dialog, "available_geometry", return_value=QRect(-400, 0, 400, 350)):
            dialog.present()
        self.app.processEvents()
        self.assertLessEqual(dialog.width(), 384)
        self.assertLessEqual(dialog.height(), 334)
        self.assertGreater(dialog.scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(self.store.snapshot(), before)

    def test_actual_customize_provider_button_opens_the_new_dialog(self):
        from ui import MainWindow
        central = QWidget()
        central.resize(980, 700)
        self.addCleanup(central.close)
        window = SimpleNamespace(_customize_overlay=None, centralWidget=lambda: central,
                                 _preview_ui_color=Mock(), _apply_name_update=Mock(),
                                 _open_vision_privacy=Mock(), _open_provider_settings=Mock())
        with patch("ui._read_full_config", return_value={}):
            MainWindow._open_customize(window)
        buttons = {button.text(): button for button in window._customize_overlay.findChildren(QPushButton)}
        button = next(value for text, value in buttons.items() if "AI Providers" in text)
        button.click()
        window._open_provider_settings.assert_called_once_with()

    def test_main_setup_checks_selected_chat_instead_of_gemini_key(self):
        from ui import MainWindow
        selected = self.store.save_profile({"provider": "anthropic", "model": "chat-model", "auth_type": "api_key"}, "fake")
        self.store.set_role("chat", selected["id"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_keys.json"
            path.write_text(json.dumps({"os_system": "windows"}), encoding="utf-8")
            with patch("ui.API_FILE", path), patch("core.provider_ui.get_provider_store", return_value=self.store):
                self.assertTrue(MainWindow._check_config(SimpleNamespace()))
                self.store.set_role("chat", None)
                self.assertFalse(MainWindow._check_config(SimpleNamespace()))

    def test_first_run_system_save_preserves_credentials_voice_and_personality(self):
        from ui import MainWindow
        import memory.config_manager as config
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_keys.json"
            preserved = {"gemini_api_key": "private-old-key", "personality": "custom", "voice_name": "Kore", "cloud_vision_enabled": False}
            path.write_text(json.dumps(preserved), encoding="utf-8")
            window = SimpleNamespace(_check_config=lambda: True, _refresh_provider_badge=Mock(),
                                     _title_lbl=QLabel(), _sub_lbl=QLabel(), hud=SimpleNamespace(),
                                     setWindowTitle=Mock(), _log=SimpleNamespace(append_log=Mock()),
                                     _apply_state=Mock())
            values = {"assistant_name": "CHIDVI", "user_name": "Tester", "os_system": "windows"}
            with patch.object(config, "CONFIG_DIR", Path(directory)):
                with patch.object(config, "CONFIG_FILE", path):
                    self.assertTrue(MainWindow._on_provider_setup_done(window, values))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved, {**preserved, **values})
            self.assertTrue(window._ready)
            self.assertNotIn("private-old-key", str(window._log.append_log.call_args_list))

    def test_setup_route_uses_provider_dialog_and_badge_update_is_queued(self):
        from ui import JarvisUI, MainWindow
        window = SimpleNamespace(_open_provider_settings=Mock())
        MainWindow._show_setup(window)
        window._open_provider_settings.assert_called_once_with(first_run=True)
        wrapper = SimpleNamespace(_win=SimpleNamespace(_provider_state_sig=SimpleNamespace(emit=Mock())))
        JarvisUI.set_provider_state(wrapper, "Chat: Anthropic / Claude | Live voice: Gemini")
        wrapper._win._provider_state_sig.emit.assert_called_once()
        self.assertIn("Anthropic", wrapper._win._provider_state_sig.emit.call_args.args[0])

    def test_legacy_setup_callback_uses_secure_key_writer_and_keeps_plaintext_free(self):
        from ui import MainWindow
        window = SimpleNamespace(_check_config=lambda: True, _overlay=None, _apply_state=Mock(),
                                 _refresh_provider_badge=Mock(), _on_provider_changed=Mock(),
                                 _log=SimpleNamespace(append_log=Mock()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_keys.json"
            original = {"personality": "custom", "voice_name": "Kore"}
            path.write_text(json.dumps(original), encoding="utf-8")
            import memory.config_manager as config
            with patch("ui.API_FILE", path), patch("ui.CONFIG_DIR", Path(directory)), \
                    patch.object(config, "CONFIG_DIR", Path(directory)), patch.object(config, "CONFIG_FILE", path), \
                    patch.object(config, "save_api_keys") as save:
                MainWindow._on_setup_done(window, "synthetic-private-key", "windows")
                save.assert_called_once_with("synthetic-private-key")
            self.assertEqual(json.loads(path.read_text()), {**original, "os_system": "windows"})
            self.assertNotIn("synthetic-private-key", str(window._log.append_log.call_args_list))


if __name__ == "__main__":
    unittest.main()
