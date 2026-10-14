"""The repair broker cannot turn workspace access into credential or system access."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from core.repair_permissions import PermissionBroker, redact


class RepairPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.events = []
        self.broker = PermissionBroker(self.root, audit=self.events.append)

    def tearDown(self):
        self.temp.cleanup()

    def test_workspace_work_allowed_and_elevated_capabilities_cannot_be_preapproved(self):
        self.assertTrue(self.broker.check('source_edit', 'actions/new.py')['allowed'])
        for capability in ('administrator', 'outside_workspace', 'system_settings', 'destructive', 'upload', 'git_publish'):
            decision = self.broker.check(capability, reason='repair failed')
            self.assertFalse(decision['allowed'])
            self.assertTrue(decision['approval_required'])
            with self.assertRaises((PermissionError, ValueError)):
                self.broker.set_preapproved(capability, True)
        with self.assertRaises(PermissionError):
            self.broker.require('unrecognized_capability')

    def test_class_b_is_opt_in_persistent_and_revocable(self):
        self.assertFalse(self.broker.check('dependencies')['allowed'])
        self.broker.set_preapproved('dependencies', True)
        self.assertTrue(PermissionBroker(self.root).check('dependencies')['allowed'])
        self.broker.set_preapproved('dependencies', False)
        self.assertFalse(PermissionBroker(self.root).check('dependencies')['allowed'])
        self.assertTrue(any(event.get('type') == 'permission_changed' for event in self.events))

    def test_policy_file_cannot_grant_class_c_or_truthy_nonbooleans(self):
        policy = self.root / 'logs/repair/permissions.json'
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(json.dumps({'preapproved': {'administrator': True, 'dependencies': 'true'}}))
        broker = PermissionBroker(self.root)
        self.assertFalse(broker.check('administrator')['allowed'])
        self.assertFalse(broker.check('dependencies')['allowed'])

    def test_paths_reject_escape_credentials_and_permission_code_edits(self):
        for path in ('../outside.py', '.git/config', '.agents/rules.md', '.codex/config.toml', 'config/api_keys.json', '.env', 'oauth/token.json'):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                self.broker.path(path)
        for path in ('core/repair_permissions.py', 'core/diagnostic_commands.py', 'logs/repair/permissions.json'):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                self.broker.path(path, write=True)
        self.assertEqual(self.broker.path('actions/new.py', write=True), self.root / 'actions/new.py')
        self.assertFalse(self.broker.check('source_edit', 'config/preferences.json')['allowed'])
        self.broker.set_preapproved('app_config', True)
        self.assertTrue(self.broker.check('source_edit', 'config/preferences.json')['allowed'])

    def test_existing_symlink_cannot_escape_even_for_new_descendant(self):
        outside = self.root.parent / (self.root.name + '-outside')
        outside.mkdir()
        link = self.root / 'linked'
        try:
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                if os.name != 'nt':
                    raise
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
            with self.assertRaises(PermissionError):
                self.broker.path('linked/new.py', write=True)
        finally:
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                link.rmdir()
            outside.rmdir()

    def test_hardlink_alias_cannot_bypass_protected_source(self):
        protected = self.root / 'core/repair_permissions.py'
        protected.parent.mkdir()
        protected.write_text('protected = True')
        os.link(protected, self.root / 'alias.py')
        with self.assertRaises(PermissionError):
            self.broker.path('alias.py', write=True)

    def test_redaction_covers_known_values_and_named_secrets_in_audit(self):
        (self.root / 'config').mkdir()
        (self.root / 'config/api_keys.json').write_text(json.dumps({'gemini_api_key': 'fixture-secret-123456', 'plugin_config': {'password': 'nested-secret-123456'}}))
        broker = PermissionBroker(self.root, audit=self.events.append)
        broker.check('administrator', reason='fixture-secret-123456 token=another-secret-value')
        serialized = json.dumps(self.events)
        self.assertNotIn('fixture-secret-123456', serialized)
        self.assertNotIn('another-secret-value', serialized)
        self.assertNotIn('nested-secret-123456', redact('raw nested-secret-123456'))
        self.assertIn('[REDACTED]', serialized)


if __name__ == '__main__':
    unittest.main()
