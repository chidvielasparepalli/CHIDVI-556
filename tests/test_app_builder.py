import unittest
from unittest.mock import Mock, patch

from actions import app_builder as app_builder_action
from core.action_loader import ActionRegistry, _validate

from core.app_builder.engine import _safe_rel, _slug, _strip_fences


class AppBuilderUtilityTests(unittest.TestCase):
    def test_action_registers_callable_handler_and_dispatches(self):
        record = _validate(app_builder_action, "app_builder.py")
        self.assertTrue(record.valid, record.error)
        self.assertIs(record.handler, app_builder_action.app_builder)
        activity, player, speak = Mock(), Mock(), Mock()
        registry = ActionRegistry({record.name: record}, activity)
        result = Mock(project_dir=None)
        result.text.return_value = '{"ok":true,"status":"READY"}'
        with patch.object(app_builder_action, "AppBuilder") as builder:
            builder.return_value.status.return_value = result
            output = registry.run("app_builder", {"action":"status", "project_dir":"fixture-project"},
                                  {"player":player, "speak":speak})
        self.assertEqual(output, '{"ok":true,"status":"READY"}')
        builder.return_value.status.assert_called_once()
        activity.assert_not_called()

    def test_slug(self):
        self.assertEqual(_slug("Luxury AI / Builder!"), "luxury-ai-builder")

    def test_safe_relative_path(self):
        self.assertEqual(_safe_rel("app/page.tsx"), "app/page.tsx")
        with self.assertRaises(ValueError):
            _safe_rel("../escape.txt")

    def test_strip_fences(self):
        fenced = chr(96) * 3 + "json\n{\"ok\": true}\n" + chr(96) * 3
        self.assertEqual(_strip_fences(fenced), '{\"ok\": true}')


if __name__ == "__main__":
    unittest.main()
