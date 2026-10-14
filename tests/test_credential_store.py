import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.credential_store import CredentialStore, CredentialStoreError, WindowsDPAPI


class FakeProtector:
    """Reversible test fixture, deliberately unavailable from production code."""
    def protect(self, value):
        return b"test-only:" + base64.b64encode(value)

    def unprotect(self, value):
        if not value.startswith(b"test-only:"):
            raise ValueError("invalid fixture")
        return base64.b64decode(value[10:], validate=True)


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "credentials.dpapi"
        self.store = CredentialStore(self.path, protector=FakeProtector())

    def test_round_trip_rotation_and_removal_do_not_write_plaintext(self):
        self.store.set("profile-one", "synthetic-secret-first")
        self.assertEqual(self.store.get("profile-one"), "synthetic-secret-first")
        self.assertNotIn(b"synthetic-secret-first", self.path.read_bytes())
        self.store.set("profile-one", "synthetic-secret-rotated")
        self.assertEqual(CredentialStore(self.path, FakeProtector()).get("profile-one"),
                         "synthetic-secret-rotated")
        self.assertTrue(self.store.delete("profile-one"))
        self.assertIsNone(self.store.get("profile-one"))
        self.assertFalse(self.store.delete("profile-one"))

    def test_verification_failure_preserves_previous_ciphertext(self):
        self.store.set("one", "synthetic-secret")
        before = self.path.read_bytes()
        with mock.patch.object(self.store.protector, "protect", return_value=b"corrupt"):
            with self.assertRaises(CredentialStoreError):
                self.store.set("one", "replacement-secret")
        self.assertEqual(self.path.read_bytes(), before)

    def test_replace_failure_preserves_store_and_cleans_temporary_file(self):
        self.store.set("one", "synthetic-secret")
        before = self.path.read_bytes()
        with mock.patch("core.credential_store.os.replace", side_effect=OSError("private-detail")):
            with self.assertRaises(CredentialStoreError) as caught:
                self.store.set("one", "replacement-secret")
        self.assertNotIn("private-detail", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])

    def test_recovery_is_encrypted_and_readback_verified(self):
        raw = b'{"gemini_api_key":"synthetic-backup-secret","name":"kept"}'
        backup = self.store.backup(raw, label="legacy")
        self.assertNotIn(b"synthetic-backup-secret", backup.read_bytes())
        self.assertEqual(self.store.recover_backup(backup), raw)

    def test_corruption_fails_closed_and_exception_never_echoes_key(self):
        self.path.write_bytes(b"invalid-synthetic-secret")
        with self.assertRaises(CredentialStoreError) as caught:
            self.store.get("one")
        self.assertNotIn("synthetic-secret", str(caught.exception))
        with self.assertRaises(CredentialStoreError):
            self.store.set("two", "valid-secret")
        self.assertEqual(self.path.read_bytes(), b"invalid-synthetic-secret")

    def test_failed_post_write_verification_restores_previous_encrypted_store(self):
        from core.credential_store import atomic_write
        self.store.set("one", "original-secret")
        previous = self.path.read_bytes()
        calls = []
        def corrupt_once(path, value):
            calls.append(path)
            atomic_write(path, b"corrupted" if len(calls) == 1 else value)
        with mock.patch("core.credential_store.atomic_write", side_effect=corrupt_once):
            with self.assertRaises(CredentialStoreError):
                self.store.set("one", "new-secret")
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.store.get("one"), "original-secret")

    def test_hardlink_store_is_rejected_without_changing_original(self):
        original = self.path.parent / "original.dpapi"
        original.write_bytes(b"preserve-original")
        os.link(original, self.path)
        with self.assertRaises(CredentialStoreError):
            self.store.set("one", "synthetic-secret")
        self.assertEqual(original.read_bytes(), b"preserve-original")

    def test_rejects_unsafe_identifiers_and_control_characters(self):
        for identifier in ("../key", "ONE", "", "a" * 65):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.store.set(identifier, "valid-key")
        for key in ("", "key\nheader", "key\0hidden", 123):
            with self.subTest(key=repr(key)), self.assertRaises((ValueError, TypeError)):
                self.store.set("one", key)

    @unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI")
    def test_actual_windows_dpapi_round_trip_and_tamper_detection(self):
        protector = WindowsDPAPI()
        raw = b"synthetic-dpapi-test-only"
        encoded = protector.protect(raw)
        self.assertNotIn(raw, encoded)
        self.assertEqual(protector.unprotect(encoded), raw)
        with self.assertRaises(CredentialStoreError):
            protector.unprotect(encoded[:-1] + bytes([encoded[-1] ^ 1]))


if __name__ == "__main__":
    unittest.main()
