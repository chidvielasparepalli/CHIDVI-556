"""Repair uses the shared task owner and real verifier, while mode-off stays inert."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.repair_runtime import RepairRuntime
from core.task_control import TaskStore


class Panel:
    def __init__(self):
        self.events = []
        self.shortcut_input = SimpleNamespace(text=lambda: 'ctrl+alt+esc')

    def publish(self, event):
        self.events.append(event)

    def set_permissions(self, permissions):
        self.permissions = permissions

    def set_history(self, records):
        self.history = records

    def observation_options(self):
        return {'interval': 5, 'monitor': 1, 'retain': False}


class RepairRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/api_keys.json').write_text('{"gemini_api_key":"runtime-private-fixture-345678"}')
        self.panel, self.tasks = Panel(), TaskStore()
        self.runtime = RepairRuntime(self.root, self.tasks, self.panel, diagnostic=True)
        # Global shortcuts/editor launches are the only hardware boundaries replaced.
        self.shortcut = patch.object(self.runtime, 'set_shortcut', return_value=None)
        self.editor = patch.object(self.runtime, 'open_editor', return_value={'ok': False, 'error': 'Test editor unavailable'})
        self.shortcut.start()
        self.editor.start()

    def tearDown(self):
        self.runtime.close()
        self.editor.stop()
        self.shortcut.stop()
        self.temp.cleanup()

    def enable(self):
        self.runtime.set_mode(True)

    def claim(self, text='Repair fixture', request='fixture', executor='repair'):
        task = self.tasks.submit(text, request, executor=executor)
        self.tasks.claim(task['id'])
        return task['id']

    def owned(self, parameters):
        owner = self.tasks.active_id or self.claim()
        with self.tasks.context(owner):
            return self.runtime.handle(parameters)

    def test_shortcut_configuration_respects_shared_session_when_repair_is_off(self):
        self.panel.set_global_hotkey = Mock()
        self.panel.live_sharing_active = True
        RepairRuntime.set_shortcut(self.runtime, 'Ctrl+Shift+F10')
        self.panel.set_global_hotkey.assert_called_with('Ctrl+Shift+F10')
        self.panel.live_sharing_active = False
        RepairRuntime.set_shortcut(self.runtime, 'Ctrl+Shift+F10')
        self.panel.set_global_hotkey.assert_called_with(None)

    def test_stop_dispatches_shared_interrupt_even_when_repair_mode_is_already_off(self):
        self.runtime.interrupt = Mock()
        self.assertFalse(self.runtime.enabled)
        self.runtime.stop()
        self.runtime.interrupt.assert_called_once_with()

    def test_mode_off_rejects_execution_and_read_actions_without_side_effects(self):
        (self.root / 'probe.py').write_text("from pathlib import Path\nPath('executed').write_text('bad')\n")
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        for parameters in ({'action': 'command', 'request_json': '{"kind":"import","module":"probe"}'},
                           {'action': 'read', 'path': 'probe.py'}, {'action': 'observe'}, {'action': 'demo'},
                           {'action': 'export_history'}):
            self.assertFalse(self.runtime.handle(parameters)['ok'])
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')))
        self.assertTrue(self.runtime.handle({'action': 'status'})['ok'])
        self.assertFalse(any(event.get('type') == 'command_started' for event in self.panel.events))

    def test_enabled_service_requires_a_current_task_owner(self):
        self.enable()
        result = self.runtime.handle({'action': 'command', 'request_json': '{"kind":"python_version"}'})
        self.assertFalse(result['ok'])
        self.assertIn('task', result['error'].lower())
        self.assertFalse(any(event.get('type') == 'command_started' for event in self.panel.events))

    def test_invalid_and_undeclared_authorization_parameters_are_structured_rejections(self):
        self.enable()
        for value in (None, [], 'command', {'action': []}, {'action': 'status', 'approved': True},
                      {'action': 'command', 'administrator': True, 'request_json': '{"kind":"python_version"}'}):
            with self.subTest(value=value):
                result = self.runtime.handle(value)
                self.assertIsInstance(result, dict)
                self.assertFalse(result['ok'])

    def test_duplicate_failure_returns_its_original_record_after_another_failure(self):
        first = self.runtime.record_failure('failure A', source='fixture')
        second = self.runtime.record_failure('failure B', source='fixture')
        self.assertNotEqual(first, second)
        self.assertEqual(self.runtime.record_failure('failure A', source='fixture'), first)
        self.assertEqual(len(self.runtime.engine.history()), 2)

    def test_read_and_search_have_successful_structured_results(self):
        (self.root / 'fixture.py').write_text('answer = 42\n')
        self.enable()
        read = self.owned({'action': 'read', 'path': 'fixture.py'})
        self.assertTrue(read.get('ok'), read)
        self.assertIn('answer = 42', read['content'])
        search = self.owned({'action': 'search', 'text': 'answer'})
        self.assertIsInstance(search, dict)
        self.assertTrue(search['ok'])
        self.assertEqual(search['matches'][0]['path'], 'fixture.py')

    def test_stale_origin_cannot_borrow_new_active_task_after_history_window_advances(self):
        self.enable()
        old = self.claim(request='old', executor='agent')
        self.tasks.cancel(old)
        self.tasks.release(old)
        for index in range(101):
            task = self.tasks.submit('Completed task', f'complete-{index}')
            self.tasks.update(task['id'], 'succeeded')
        current = self.claim(request='current', executor='agent')
        with self.tasks.context(old):
            result = self.runtime.handle({'action': 'command', 'request_json': '{"kind":"python_version"}'})
        self.assertFalse(result['ok'])
        self.assertEqual(self.tasks.active_id, current)
        self.assertFalse(self.tasks.event(current).is_set())
        self.assertFalse(any(event.get('type') == 'command_started' for event in self.panel.events))

    def test_dispatch_respects_normal_task_ownership(self):
        self.enable()
        current = self.claim(executor='agent')
        self.runtime.dispatch({'action': 'command', 'request_json': '{"kind":"python_version"}'})
        self.runtime._thread.join(timeout=3)
        self.assertFalse(self.runtime._thread.is_alive())
        self.assertEqual(self.tasks.active_id, current)
        self.assertFalse(any(event.get('type') == 'command_started' for event in self.panel.events))

    def test_auto_failure_work_is_deduplicated_and_leaves_shared_task_finished(self):
        self.enable()
        identity = self.runtime.record_failure('No deterministic repair fixture', source='fixture')
        self.runtime._thread.join(timeout=5)
        self.assertFalse(self.runtime._thread.is_alive())
        self.assertIsNone(self.tasks.active_id)
        self.assertEqual(self.runtime.record_failure('No deterministic repair fixture', source='fixture'), identity)
        tasks = self.tasks.snapshot()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['executor'], 'repair')
        self.assertEqual(tasks[0]['state'], 'failed')

    def test_demo_runs_real_reproducer_patch_and_behavioral_tests_in_isolation(self):
        self.enable()
        result = self.owned({'action': 'demo'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'repaired')
        self.assertFalse(result['live_installation_modified'])
        directory = Path(result['isolated_workspace'])
        self.assertTrue(directory.is_relative_to(self.root / 'logs/repair/demos'))
        self.assertIn("TOOL['handler'] = demo_action", (directory / 'actions/demo_action.py').read_text())
        record = self.runtime._demo_engines[result['failure_id']].history()[0]
        commands = record['attempts'][0]['commands']
        self.assertFalse(next(item['result']['ok'] for item in commands if item['phase'] == 'reproducing'))
        self.assertTrue(next(item['result']['ok'] for item in commands if item['phase'] == 'verifying'))
        self.assertTrue(any(item['phase'] == 'testing' and item['result']['ok'] for item in commands))
        restored = RepairRuntime(self.root, TaskStore(), Panel(), diagnostic=True)
        self.assertIn(result['failure_id'], restored._demo_engines)
        self.assertEqual(restored.refresh_history()[0]['status'], 'repaired')

    def test_audit_directory_failure_blocks_commands_until_logging_recovers(self):
        self.enable()
        with patch.object(Path, 'mkdir', side_effect=PermissionError('fixture disk failure')):
            self.runtime.publish({'phase': 'Ready'})
        self.assertEqual(self.runtime._audit_error, 'PermissionError')
        blocked = self.owned({'action': 'command', 'request_json': '{"kind":"python_version"}'})
        self.assertFalse(blocked['ok'])
        self.assertIn('audit', blocked['error'])
        self.runtime.publish({'phase': 'Ready'})
        result = self.owned({'action': 'command', 'request_json': '{"kind":"python_version"}'})
        self.assertTrue(result['ok'], result)

    def test_engine_restart_uses_the_originating_task_cancellation(self):
        self.enable()
        identity = self.claim()
        with self.tasks.context(identity), patch.object(self.runtime.restart, 'restart') as restart:
            self.runtime.engine.restart()
        restart.assert_called_once_with(self.tasks.event(identity))

    def test_live_source_repairs_require_supervised_restart(self):
        self.enable()
        with patch.object(self.runtime.engine, 'repair', return_value={'ok': False}) as repair:
            self.owned({'action': 'repair', 'failure_id': 'fixture',
                        'request_json': '{"files": [], "restart": false}'})
        self.assertIs(repair.call_args.args[1]['restart'], True)

    def test_detected_failure_collects_task_logs_and_safe_configuration(self):
        task_id = self.claim()
        self.runtime.publish({'message': 'Recent command failure password=runtime-other-secret'})
        identity = self.runtime.record_failure({'message': 'failed', 'exit_code': 7}, task_id, source='fixture')
        record = next(item for item in self.runtime.engine.history() if item['id'] == identity)
        self.assertEqual(record['exit_code'], 7)
        self.assertEqual(record['evidence']['originating_task']['id'], task_id)
        self.assertTrue(record['evidence']['diagnostic_mode'])
        self.assertIn('Recent command failure', record['evidence']['recent_activity'])
        self.assertNotIn('runtime-other-secret', json.dumps(record))

    def test_failure_and_command_logs_never_include_known_credential_values(self):
        self.runtime.record_failure('Failure runtime-private-fixture-345678 password=runtime-other-secret', source='fixture')
        self.enable()
        (self.root / 'probe.py').write_text("print('runtime-private-fixture-345678')\n")
        result = self.owned({'action': 'command', 'request_json': '{"kind":"import","module":"probe"}'})
        self.assertTrue(result['ok'], result)
        payload = json.dumps(self.panel.events) + ''.join(path.read_text(encoding='utf-8') for path in (self.root / 'logs/repair').glob('*.json*'))
        self.assertNotIn('runtime-private-fixture-345678', payload)
        self.assertNotIn('runtime-other-secret', payload)
        self.assertIn('[REDACTED]', payload)

    def test_maximum_command_output_keeps_audit_json_lines_parseable(self):
        self.runtime.publish({'type': 'command_finished', 'stdout': 'x' * 65536, 'stderr': 'y' * 65536})
        lines = (self.root / 'logs/repair/activity.jsonl').read_text(encoding='utf-8').splitlines()
        for line in lines:
            self.assertIsInstance(json.loads(line), dict)

    def test_dependency_preflight_is_visible_before_installer_runs(self):
        self.runtime.publish({'type': 'dependency_preflight', 'dependency': {
            'license': 'MIT', 'download_size_bytes': 12345, 'required_disk_estimate_bytes': 100000,
            'source': 'https://files.pythonhosted.org/packages/fixture.whl'}})
        displayed = json.loads(self.panel.events[-1]['output'])['dependency']
        self.assertEqual(displayed['license'], 'MIT')
        self.assertEqual(displayed['download_size_bytes'], 12345)

    def test_action_binding_preserves_structured_permission_denial(self):
        from actions import self_repair
        previous = self_repair._runtime
        try:
            self_repair.bind(self.runtime)
            result = json.loads(self_repair.self_repair({'action': 'command', 'approved': True,
                                                       'request_json': '{"kind":"administrator"}'}))
            self.assertFalse(result['ok'])
            self.assertFalse(any(event.get('type') == 'command_started' for event in self.panel.events))
            self.assertTrue(json.loads(self_repair.self_repair({'action': 'status'}))['ok'])
        finally:
            self_repair.bind(previous)


if __name__ == '__main__':
    unittest.main()
