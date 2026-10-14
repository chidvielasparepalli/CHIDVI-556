"""Discovery diagnostics stay off the activity feed; actual failures stay visible."""
from pathlib import Path
import io
import logging
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.action_loader import discover_actions
from core.diagnostics import configure_discovery_logging
from core.plugin_loader import discover_plugins


class DiscoveryLoggingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        self.imported = []

    def tearDown(self):
        for module in self.imported:
            sys.modules.pop(module, None)
        self.folder.cleanup()

    def write(self, kind, name, source):
        path = self.path / (name + '.py')
        path.write_text(source, encoding='utf-8')
        self.imported.append(kind + '.' + name)
        return path

    def discover(self, kind, activity):
        if kind == 'actions':
            return discover_actions(self.path, logger=activity)
        return discover_plugins(self.path, core_tool_names=set(), logger=activity)

    def valid_source(self, kind, name='test_diagnostic_good'):
        if kind == 'actions':
            return "def run(parameters): return 'done'\nTOOL = {'name': '"+name+"', 'description': 'A diagnostic fixture', 'handler': run}\n"
        return "PLUGIN = {'name': '"+name+"', 'description': 'A diagnostic fixture'}\ndef run(parameters): return 'done'\n"

    def test_discovery_logging_configuration_is_scoped_and_does_not_duplicate(self):
        loggers = [logging.getLogger(name) for name in ('core.action_loader', 'core.plugin_loader')]
        previous = [(logger, logger.handlers[:], logger.level, logger.propagate) for logger in loggers]
        provider = logging.getLogger('google.genai')
        provider_level = provider.level
        output = io.StringIO()
        try:
            for logger in loggers:
                logger.handlers = []
            with patch.dict('os.environ', {'CHIDVI_LOG_LEVEL':'DEBUG'}), patch('sys.stderr', output):
                configure_discovery_logging()
                configure_discovery_logging()
                loggers[0].debug('action details')
                loggers[1].warning('plugin problem')
            self.assertEqual(output.getvalue().count('action details'), 1)
            self.assertEqual(output.getvalue().count('plugin problem'), 1)
            self.assertEqual(provider.level, provider_level)
            self.assertTrue(all(logger.level == logging.DEBUG for logger in loggers))
            output.seek(0)
            output.truncate()
            with patch.dict('os.environ', {'CHIDVI_LOG_LEVEL':''}):
                configure_discovery_logging()
                loggers[0].debug('hidden action details')
                loggers[1].error('visible failure')
            self.assertNotIn('hidden action details', output.getvalue())
            self.assertEqual(output.getvalue().count('visible failure'), 1)
            self.assertTrue(all(len(logger.handlers) == 1 for logger in loggers))
        finally:
            for logger, handlers, level, propagate in previous:
                for handler in logger.handlers:
                    handler.close()
                logger.handlers, logger.level, logger.propagate = handlers, level, propagate

    def test_routine_action_discovery_is_debug_only(self):
        self.write('actions', 'test_diagnostic_action', self.valid_source('actions'))
        activity = Mock()
        with self.assertLogs('core.action_loader', level='DEBUG') as diagnostics:
            registry = self.discover('actions', activity)
        self.assertTrue(registry.has('test_diagnostic_good'))
        activity.assert_not_called()
        self.assertEqual([r.levelname for r in diagnostics.records], ['DEBUG', 'DEBUG'])
        self.assertIn('Action loaded:', diagnostics.output[0])
        self.assertIn('discovery complete:', diagnostics.output[1])

    def test_routine_plugin_discovery_is_debug_only(self):
        self.write('plugins', 'test_diagnostic_plugin', self.valid_source('plugins'))
        activity = Mock()
        with self.assertLogs('core.plugin_loader', level='DEBUG') as diagnostics:
            registry = self.discover('plugins', activity)
        self.assertTrue(registry.has('test_diagnostic_good'))
        activity.assert_not_called()
        self.assertEqual([r.levelname for r in diagnostics.records], ['DEBUG', 'DEBUG'])
        self.assertIn('Plugin loaded:', diagnostics.output[0])

    def test_initialization_failure_logged_once_with_traceback_and_visible_once(self):
        for kind, module in [('actions','core.action_loader'), ('plugins','core.plugin_loader')]:
            with self.subTest(kind=kind):
                path = self.write(kind, 'test_diagnostic_bad', "raise RuntimeError('fixture initialization failed')\n")
                activity = Mock()
                with self.assertLogs(module, level='WARNING') as diagnostics:
                    registry = self.discover(kind, activity)
                self.assertFalse(registry.has('test_diagnostic_bad'))
                activity.assert_called_once()
                self.assertIn('fixture initialization failed', activity.call_args.args[0])
                self.assertEqual(len(diagnostics.records), 1)
                self.assertEqual(diagnostics.records[0].levelname, 'ERROR')
                self.assertIsNotNone(diagnostics.records[0].exc_info)
                path.unlink()

    def test_invalid_handler_is_a_visible_warning_without_crashing_scan(self):
        self.write('actions', 'test_diagnostic_bad', "TOOL={'name':'bad_fixture','description':'Invalid handler','handler':None}\n")
        self.write('actions', 'test_diagnostic_good', self.valid_source('actions'))
        activity = Mock()
        with self.assertLogs('core.action_loader', level='WARNING') as diagnostics:
            registry = self.discover('actions', activity)
        self.assertTrue(registry.has('test_diagnostic_good'))
        self.assertFalse(registry.has('bad_fixture'))
        activity.assert_called_once()
        self.assertIn("TOOL['handler']", activity.call_args.args[0])
        self.assertEqual([r.levelname for r in diagnostics.records], ['WARNING'])

    def test_runtime_failure_remains_visible_and_logs_traceback(self):
        for kind, module in [('actions','core.action_loader'), ('plugins','core.plugin_loader')]:
            with self.subTest(kind=kind):
                source = self.valid_source(kind).replace("return 'done'", "raise RuntimeError('fixture run failed')")
                path = self.write(kind, 'test_diagnostic_runtime', source)
                activity = Mock()
                registry = self.discover(kind, activity)
                activity.reset_mock()
                with patch('core.plugin_loader.get_plugin_enabled', return_value=True), self.assertLogs(module, level='ERROR') as diagnostics:
                    result = registry.run('test_diagnostic_good', {})
                self.assertIn('failed', result)
                activity.assert_called_once()
                self.assertEqual(len(diagnostics.records), 1)
                self.assertIsNotNone(diagnostics.records[0].exc_info)
                path.unlink()


if __name__ == '__main__':
    unittest.main()
