import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.credential_store import CredentialStore, CredentialStoreError
from core.provider_config import ProviderStore, ProviderConfigError
from tests.test_credential_store import FakeProtector


class ProviderConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.vault = CredentialStore(self.folder / "provider_credentials.dpapi", FakeProtector())
        self.store = ProviderStore(self.folder, vault=self.vault)

    def profile(self, identifier="work", **values):
        return {"id": identifier, "name": "Work", "provider": "openai", "model": "gpt-4.1", **values}

    def legacy(self, **values):
        data = {"gemini_api_key": "synthetic-legacy-credential", "personality": "keep-me",
                "cloud_vision_enabled": False, **values}
        self.store.legacy_path.write_text(json.dumps(data), encoding="utf-8")
        return self.store.legacy_path.read_bytes()

    def test_multiple_profiles_round_trip_never_include_credentials_in_metadata(self):
        first = self.store.save_profile(self.profile(), key="synthetic-first-secret")
        self.store.save_profile(self.profile("personal", name="Personal"), key="synthetic-second-secret")
        self.assertTrue(first["has_credential"])
        self.assertEqual(len(self.store.list_profiles()), 2)
        self.assertEqual(self.store.credential("work"), "synthetic-first-secret")
        public = json.dumps(self.store.snapshot()) + self.store.path.read_text()
        self.assertNotIn("synthetic-first-secret", public)
        self.assertNotIn("synthetic-second-secret", public)
        self.assertEqual(ProviderStore(self.folder, vault=self.vault).get_profile("personal")["name"], "Personal")

    def test_key_none_preserves_empty_removes_and_rotation_updates_only_selected_profile(self):
        self.store.save_profile(self.profile(), key="synthetic-first-secret")
        self.store.save_profile({"id": "work", "name": "Renamed"})
        self.assertEqual(self.store.credential("work"), "synthetic-first-secret")
        self.store.save_profile({"id": "work"}, key="synthetic-rotated-secret")
        self.assertEqual(self.store.credential("work"), "synthetic-rotated-secret")
        result = self.store.save_profile({"id": "work"}, key="")
        self.assertFalse(result["has_credential"])

    def test_roles_overrides_and_removal_are_independent(self):
        self.store.save_profile(self.profile(), key="synthetic-work-secret")
        self.store.save_profile(self.profile("personal"), key="synthetic-personal-secret")
        self.store.set_role("chat", "work")
        self.store.set_role("vision", "personal")
        self.store.set_override("coding", "personal")
        self.assertEqual(self.store.selected()["id"], "work")
        self.assertEqual(self.store.selected(task_kind="coding")["id"], "personal")
        self.assertEqual(self.store.selected("vision")["id"], "personal")
        self.assertTrue(self.store.has_configuration())
        self.assertTrue(self.store.remove_profile("personal"))
        self.assertIsNone(self.store.selected("vision"))
        self.assertEqual(self.store.selected(task_kind="coding")["id"], "work")
        self.assertIsNone(self.vault.get("personal"))

    def test_configuration_requires_selected_chat_and_credential_unless_auth_none(self):
        self.store.save_profile(self.profile())
        self.store.set_role("chat", "work")
        self.assertFalse(self.store.has_configuration())
        self.store.save_profile(self.profile("local", provider="compatible", model="local-model",
                                             base_url="http://127.0.0.1:1234/v1", auth_type="none"))
        self.store.set_role("chat", "local")
        self.assertTrue(self.store.has_configuration())

    def test_unsupported_role_rejected_unknown_role_warns(self):
        self.store.save_profile(self.profile("text", provider="deepseek", model="deepseek-chat"))
        with self.assertRaises(ValueError):
            self.store.set_role("vision", "text")
        self.store.save_profile(self.profile("unknown", model="future-manual-model"))
        self.store.set_role("chat", "unknown")
        self.assertTrue(self.store.selected()["warnings"])

    def test_rejects_secret_fields_invalid_ids_endpoints_and_coerced_limits(self):
        bad = [{"api_key": "private"}, {"id": "../bad"}, {"provider": "invalid"},
               {"model": "bad\nmodel"}, {"base_url": "https://api.openai.com.evil/v1"},
               {"limits": {"max_output_tokens": True}}, {"limits": {"session_token_warning": -1}}]
        for values in bad:
            with self.subTest(fields=list(values)), self.assertRaises((TypeError, ValueError)):
                self.store.save_profile(self.profile(**values))
        self.assertFalse(self.store.path.exists())

    def test_limits_status_and_usage_are_bounded_and_secret_redacted(self):
        self.store.save_profile(self.profile(limits={"max_output_tokens": 2000, "session_token_warning": 10000}),
                                key="synthetic-opaque-secret")
        status = self.store.record_status("work", connected=False,
                                          error="Rejected synthetic-opaque-secret; api_key=also-private",
                                          usage={"input_tokens": 123, "output_tokens": 5})
        self.assertEqual(status["last_usage"]["input_tokens"], 123)
        self.assertEqual(status["status"], "error")
        self.assertNotIn("synthetic-opaque-secret", self.store.path.read_text())
        self.assertNotIn("also-private", self.store.path.read_text())
        with self.assertRaises(ValueError):
            self.store.record_status("work", True, usage={"raw_prompt": "private"})
        with self.assertRaises(ValueError):
            self.store.record_status("work", True, usage={"input_tokens": -1})

    def test_long_credential_is_redacted_before_error_truncation(self):
        secret = "synthetic-long-credential-" + "q" * 5000
        self.store.save_profile(self.profile(), key=secret)
        self.store.record_status("work", False, error="rejected " + secret)
        saved = self.store.path.read_text()
        self.assertFalse(secret[:100] in saved, "Truncation must not leave a credential prefix")

    def test_connection_test_status_is_recorded_only_for_same_profile_and_credential(self):
        original = self.store.save_profile(self.profile(), key="original-test-key")
        result = self.store.record_status("work", True, usage={"input_tokens": 1},
                                          expected_profile=original, expected_credential="original-test-key")
        self.assertEqual(result["status"], "connected")
        self.store.save_profile({"id": "work"}, key="rotated-test-key")
        before = self.store.path.read_bytes()
        result = self.store.record_status("work", False, error="old test failed", usage={"input_tokens": 2},
                                          expected_profile=original, expected_credential="original-test-key")
        self.assertIsNone(result)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.save_profile({"id": "work", "model": "gpt-5"})
        before = self.store.path.read_bytes()
        self.assertIsNone(self.store.record_status("work", True, expected_profile=original,
                                                   expected_credential="rotated-test-key"))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.remove_profile("work")
        self.assertIsNone(self.store.record_status("work", True, expected_profile=original,
                                                   expected_credential="original-test-key"))

    def test_connection_status_resets_when_destination_model_or_credential_changes(self):
        changes = [({"model": "new-model"}, None),
                   ({"base_url": "http://127.0.0.1:4321/v1"}, "replacement-test-key"),
                   ({"auth_type": "none"}, ""),
                   ({"provider": "openai", "model": "gpt-4.1"}, "replacement-test-key"),
                   ({}, "replacement-test-key"), ({}, "")]
        for index, (fields, key) in enumerate(changes):
            with self.subTest(fields=tuple(fields), key_changed=key is not None):
                identifier = "test-" + str(index)
                self.store.save_profile(self.profile(identifier, provider="compatible", model="local-model",
                                                      base_url="http://127.0.0.1:1234/v1"), key="original-test-key")
                tested = self.store.record_status(identifier, True, error="old detail",
                                                  usage={"input_tokens": 7})
                # UI saves may include previously loaded status fields as well.
                incoming = {**tested, **fields}
                if "provider" in fields:
                    incoming.pop("base_url")
                saved = self.store.save_profile(incoming, key=key)
                self.assertEqual(saved["status"], "untested")
                self.assertFalse(saved["connected"])
                for name in ("last_error", "last_tested", "last_usage"):
                    self.assertNotIn(name, saved)

    def test_name_change_and_identical_credential_preserve_connection_result(self):
        self.store.save_profile(self.profile(), key="same-test-key")
        tested = self.store.record_status("work", True, usage={"input_tokens": 7})
        saved = self.store.save_profile({"id": "work", "name": "Renamed"}, key="same-test-key")
        for name in ("status", "connected", "last_tested", "last_usage"):
            self.assertEqual(saved[name], tested[name])

    def test_metadata_failure_restores_rotated_credential(self):
        self.store.save_profile(self.profile(), key="synthetic-original-secret")
        with mock.patch("core.provider_config.atomic_write", side_effect=OSError("unsafe-detail")):
            with self.assertRaises(ProviderConfigError) as caught:
                self.store.save_profile({"id": "work", "name": "Changed"}, key="synthetic-new-secret")
        self.assertNotIn("unsafe-detail", str(caught.exception))
        self.assertEqual(self.store.credential("work"), "synthetic-original-secret")
        self.assertEqual(self.store.get_profile("work")["name"], "Work")

    def test_new_credential_cannot_leak_through_status_or_profile_metadata(self):
        result = self.store.save_profile(self.profile(last_error="Rejected synthetic-new-secret"),
                                         key="synthetic-new-secret")
        self.assertNotIn("synthetic-new-secret", result["last_error"])
        with self.assertRaises(ValueError):
            self.store.save_profile(self.profile("other", name="synthetic-new-secret"))
        self.assertNotIn("synthetic-new-secret", self.store.path.read_text())

    def test_selection_freezes_models_roles_and_overrides_but_credentials_remain_live(self):
        self.store.save_profile(self.profile(), key="synthetic-original-key")
        self.store.save_profile(self.profile("other", model="gpt-5"), key="synthetic-other-key")
        self.store.set_role("chat", "work")
        self.store.set_role("vision", "work")
        self.store.set_override("coding", "work")
        with self.store.selection():
            self.store.set_role("chat", "other")
            self.store.set_role("vision", "other")
            self.store.set_override("coding", "other")
            self.store.save_profile({"id": "work", "model": "gpt-5"}, key="synthetic-rotated-key")
            with self.store.selection():
                self.assertEqual(self.store.selected()["id"], "work")
                self.assertEqual(self.store.selected()["model"], "gpt-4.1")
                self.assertEqual(self.store.selected("vision")["id"], "work")
                self.assertEqual(self.store.selected(task_kind="coding")["id"], "work")
                self.assertEqual(self.store.credential("work"), "synthetic-rotated-key")
            self.store.save_profile({"id": "work"}, key="")
            self.assertFalse(self.store.selected()["has_credential"])
        self.assertEqual(self.store.selected()["id"], "other")

    def test_selection_does_not_override_other_store_or_background_gui_context(self):
        import contextvars
        self.store.save_profile(self.profile(), key="synthetic-work-key")
        self.store.save_profile(self.profile("other"), key="synthetic-other-key")
        self.store.set_role("chat", "work")
        with self.store.selection():
            self.store.set_role("chat", "other")
            fresh = contextvars.Context().run(self.store.selected)
            self.assertEqual(fresh["id"], "other")
            with tempfile.TemporaryDirectory() as folder:
                alternate = ProviderStore(folder, vault=self.vault)
                self.assertIsNone(alternate.selected())

    def test_recipient_change_requires_explicit_key_and_revokes_old_task_credential(self):
        self.store.save_profile(self.profile(), key="original-openai-key")
        self.store.set_role("chat", "work")
        with self.assertRaises(ValueError):
            self.store.save_profile({"id": "work", "provider": "anthropic", "model": "claude-sonnet-4-5"})
        self.assertEqual(self.store.get_profile("work")["provider"], "openai")
        with self.store.selection():
            self.store.save_profile({"id": "work", "provider": "anthropic", "model": "claude-sonnet-4-5"},
                                    key="new-anthropic-key")
            self.assertEqual(self.store.selected()["provider"], "openai")
            self.assertIsNone(self.store.credential("work"))
        self.assertEqual(self.store.credential("work"), "new-anthropic-key")

    def test_corrupt_metadata_never_overwrites_profiles_or_plaintext_legacy(self):
        before = self.legacy()
        self.store.path.write_text("not JSON", encoding="utf-8")
        with self.assertRaises(ProviderConfigError):
            self.store.save_profile(self.profile(), key="new-secret")
        report = self.store.migrate_legacy()
        self.assertFalse(report["ok"])
        self.assertEqual(self.store.legacy_path.read_bytes(), before)

    def test_migration_preserves_live_vision_models_preferences_and_verified_recovery(self):
        original = self.legacy(vision_model="gemini-2.5-pro")
        report = self.store.migrate_legacy()
        self.assertTrue(report["ok"])
        self.assertTrue(report["migrated"])
        self.assertEqual(self.store.selected()["model"], "gemini-3.1-flash-live-preview")
        self.assertEqual(self.store.selected("live_audio")["id"], self.store.selected()["id"])
        self.assertEqual(self.store.selected("vision")["model"], "gemini-2.5-pro")
        self.assertEqual(self.store.credential(self.store.selected()["id"]), "synthetic-legacy-credential")
        updated = json.loads(self.store.legacy_path.read_text())
        self.assertNotIn("gemini_api_key", updated)
        self.assertEqual(updated["personality"], "keep-me")
        self.assertFalse(updated["cloud_vision_enabled"])
        self.assertEqual(self.vault.recover_backup(Path(report["backup_path"])), original)
        self.assertNotIn("synthetic-legacy-credential", json.dumps(report))
        self.assertFalse(self.store.migrate_legacy()["migrated"])

    def test_migration_preserves_existing_role_selection(self):
        self.store.save_profile(self.profile(), key="synthetic-work-secret")
        self.store.set_role("chat", "work")
        self.legacy()
        self.assertTrue(self.store.migrate_legacy()["ok"])
        self.assertEqual(self.store.selected()["id"], "work")
        self.assertEqual(self.store.selected("live_audio")["provider"], "gemini")

    def test_migration_verification_failure_keeps_original(self):
        before = self.legacy()
        with mock.patch.object(self.vault, "get", return_value=None):
            report = self.store.migrate_legacy()
        self.assertFalse(report["ok"])
        self.assertEqual(self.store.legacy_path.read_bytes(), before)

    def test_migration_backup_failure_keeps_original(self):
        before = self.legacy()
        with mock.patch.object(self.vault, "backup", side_effect=CredentialStoreError("failed")):
            report = self.store.migrate_legacy()
        self.assertFalse(report["ok"])
        self.assertEqual(self.store.legacy_path.read_bytes(), before)

    def test_migration_detects_concurrent_legacy_change_without_overwriting_it(self):
        self.legacy()
        backup = self.vault.backup
        changed = b'{"gemini_api_key":"newer-secret","personality":"newer"}'
        def concurrent_change(*args, **kwargs):
            result = backup(*args, **kwargs)
            self.store.legacy_path.write_bytes(changed)
            return result
        with mock.patch.object(self.vault, "backup", side_effect=concurrent_change):
            report = self.store.migrate_legacy()
        self.assertFalse(report["ok"])
        self.assertEqual(report["code"], "legacy_changed")
        self.assertEqual(self.store.legacy_path.read_bytes(), changed)

    def test_compatibility_key_functions_use_secure_store_and_legacy_fallback(self):
        from memory import config_manager as cfg
        with mock.patch("core.provider_config.get_provider_store", return_value=self.store), \
             mock.patch.object(cfg, "CONFIG_DIR", self.folder), \
             mock.patch.object(cfg, "CONFIG_FILE", self.store.legacy_path):
            self.legacy()
            self.assertEqual(cfg.get_gemini_key(), "synthetic-legacy-credential")
            cfg.save_api_keys("synthetic-secure-replacement")
            self.assertEqual(cfg.get_gemini_key(), "synthetic-secure-replacement")
            self.assertTrue(cfg.is_configured())
            self.assertNotIn("gemini_api_key", cfg.load_api_keys())

    def test_default_store_respects_patched_configuration_paths(self):
        from core.provider_config import get_provider_store
        from memory import config_manager as cfg
        alternate = self.folder / "alternate-settings.json"
        with mock.patch.object(cfg, "CONFIG_DIR", self.folder), mock.patch.object(cfg, "CONFIG_FILE", alternate):
            default = get_provider_store()
            self.assertEqual(default.path, self.folder / "providers.json")
            self.assertEqual(default.legacy_path, alternate)
            self.assertIs(default, get_provider_store())
            self.assertEqual(default.list_profiles(), [])
        self.assertFalse(alternate.exists())

    def test_migration_refuses_to_replace_preexisting_rotated_gemini_profile(self):
        self.store.save_profile(self.profile("legacy-gemini-live", provider="gemini",
                                             model="gemini-3.1-flash-live-preview"), key="newer-secure-key")
        before = self.legacy()
        report = self.store.migrate_legacy()
        self.assertFalse(report["ok"])
        self.assertEqual(report["code"], "migration_conflict")
        self.assertEqual(self.store.legacy_path.read_bytes(), before)
        self.assertEqual(self.store.credential("legacy-gemini-live"), "newer-secure-key")

    def test_settings_writer_waits_for_migration_then_preserves_key_removal(self):
        import threading
        from memory import config_manager as cfg
        self.legacy()
        in_backup, finish_backup, writer_done = threading.Event(), threading.Event(), threading.Event()
        backup = self.vault.backup
        report, errors = {}, []
        def blocked_backup(*args, **kwargs):
            result = backup(*args, **kwargs)
            in_backup.set()
            if not finish_backup.wait(3):
                raise RuntimeError("test backup timeout")
            return result
        def migrate():
            report.update(self.store.migrate_legacy())
        def write_setting():
            try:
                cfg.save_voice("Kore")
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                writer_done.set()
        with mock.patch.object(cfg, "CONFIG_DIR", self.folder), \
             mock.patch.object(cfg, "CONFIG_FILE", self.store.legacy_path), \
             mock.patch.object(self.vault, "backup", side_effect=blocked_backup):
            migration = threading.Thread(target=migrate)
            writer = threading.Thread(target=write_setting)
            migration.start()
            try:
                self.assertTrue(in_backup.wait(3))
                writer.start()
                self.assertFalse(writer_done.wait(.15), "Settings must use the same migration lock")
            finally:
                finish_backup.set()
                migration.join(3)
                if writer.ident is not None:
                    writer.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(report["ok"])
        saved = json.loads(self.store.legacy_path.read_text())
        self.assertNotIn("gemini_api_key", saved)
        self.assertEqual(saved["voice_name"], "Kore")

    def test_all_legacy_setting_writers_preserve_malformed_configuration(self):
        from memory import config_manager as cfg
        operations = (lambda: cfg.save_assistant_config("Assistant", "User"),
                      lambda: cfg.save_voice("Kore"), lambda: cfg.save_wake_word_enabled(True),
                      lambda: cfg.save_brief_enabled(False), lambda: cfg.save_input_device("Mic"),
                      lambda: cfg.save_output_device("Speaker"),
                      lambda: cfg.save_plugin_config("example", {"setting": "value"}),
                      lambda: cfg.save_plugin_enabled("example", False))
        with mock.patch.object(cfg, "CONFIG_DIR", self.folder), mock.patch.object(cfg, "CONFIG_FILE", self.store.legacy_path):
            for index, operation in enumerate(operations):
                self.store.legacy_path.write_bytes(b"{invalid-configuration")
                with self.subTest(index=index), self.assertRaises(ValueError):
                    operation()
                self.assertEqual(self.store.legacy_path.read_bytes(), b"{invalid-configuration")


if __name__ == "__main__":
    unittest.main()
