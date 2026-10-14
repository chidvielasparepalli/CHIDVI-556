"""Acceptance bookkeeping uses current-run evidence and a real temporary report."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from core import repair_acceptance
from core.repair_permissions import PermissionBroker


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self):
        for callback in self.callbacks:
            callback()


class ManualTimer:
    def __init__(self, parent):
        self.timeout = Signal()
        self.running = False

    def start(self, interval):
        self.running = True

    def stop(self):
        self.running = False

    @staticmethod
    def singleShot(delay, callback):
        callback()


class Worker:
    alive = False

    def is_alive(self):
        return self.alive


class ObservationState:
    def __init__(self):
        self.mode = 'running'
        self.latest = None

    def snapshot(self):
        return {'mode': self.mode, 'latest': copy.deepcopy(self.latest)}

    def stop(self):
        self.mode = 'stopped'


class RepairAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'logs/repair').mkdir(parents=True)
        self.records = [{'id': 'previous-failure', 'source': 'isolated_demo',
                         'task_id': 'previous-task', 'status': 'repaired'}]
        self.worker = Worker()
        self.repair = SimpleNamespace(
            broker=PermissionBroker(self.root), _thread=self.worker, observation=ObservationState(),
            refresh_history=lambda: copy.deepcopy(self.records), last_restart=None,
            publish=lambda event: None, stop=lambda: self.repair.observation.stop())
        self.panel = SimpleNamespace(mode=Mock(), observation=Mock(), grab=Mock(),
            commands={'demo': Mock()}, restart_button=Mock())
        self.panel.commands['demo'].click.side_effect = self.start_worker
        self.panel.restart_button.click.side_effect = self.start_worker
        self.about_to_quit = Signal()
        self.ui = SimpleNamespace(repair_panel=self.panel, show_repair_panel=Mock(), _win=object(),
                                  _app=SimpleNamespace(aboutToQuit=self.about_to_quit))
        timer = patch.object(repair_acceptance, 'QTimer', ManualTimer)
        timer.start()
        self.addCleanup(timer.stop)

    def start_worker(self):
        self.worker.alive = True

    def start(self):
        repair_acceptance.start(self.ui, self.repair)
        self.timer = self.ui._repair_acceptance_timer
        self.timer.timeout.emit()  # Observe the demo while its worker owns the task.

    def complete_demo(self, status='repaired', capture_task='current-task'):
        self.records.append({'id': 'current-failure', 'source': 'isolated_demo',
                             'task_id': 'current-task', 'status': status})
        self.repair.observation.latest = {'task_id': capture_task, 'width': 1920, 'height': 1080}
        self.worker.alive = False
        self.timer.timeout.emit()

    def complete_restart(self, result):
        self.repair.last_restart = result
        self.worker.alive = False
        self.timer.timeout.emit()

    def report(self):
        return json.loads((self.root/'logs/repair/live-acceptance.json').read_text(encoding='utf-8'))

    def test_previous_repaired_history_cannot_certify_failed_current_demo(self):
        self.start()
        self.complete_demo(status='rolled_back')
        report = self.report()
        self.assertFalse(report['repair_verified'])
        self.assertFalse(report['ok'])
        self.panel.restart_button.click.assert_not_called()
        self.assertFalse(self.timer.running)

    def test_capture_must_belong_to_the_current_new_demo_task(self):
        self.start()
        self.complete_demo(capture_task='previous-task')
        report = self.report()
        self.assertTrue(report['repair_verified'])
        self.assertFalse(report['capture_verified'])
        self.assertFalse(report['ok'])
        self.panel.restart_button.click.assert_not_called()

    def test_success_requires_committed_handoff_after_current_demo(self):
        self.start()
        self.complete_demo()
        self.panel.restart_button.click.assert_called_once_with()
        self.assertFalse(self.report()['ok'], 'The pending restart is not a completed acceptance result')
        self.complete_restart({'ok': True, 'verified': 'Two current heartbeats'})
        self.assertFalse(self.report()['ok'], 'Health checks alone do not prove committed handoff')
        self.repair.last_restart = {'ok': True, 'handoff': {'ok': True, 'status': 'handed_off'}}
        self.about_to_quit.emit()
        self.assertTrue(self.report()['ok'])

    def test_failed_restart_overwrites_an_earlier_successful_report(self):
        self.start()
        self.complete_demo()
        self.complete_restart({'ok': True, 'handoff': {'ok': True}})
        self.assertTrue(self.report()['ok'])
        self.repair.last_restart = {'ok': False, 'error': 'Replacement crashed'}
        self.about_to_quit.emit()
        report = self.report()
        self.assertFalse(report['ok'])
        self.assertEqual(report['restart']['error'], 'Replacement crashed')

    def test_previous_handoff_cannot_certify_a_current_restart_without_a_result(self):
        self.repair.last_restart = {'ok': True, 'handoff': {'ok': True, 'pid': 123}}
        self.start()
        self.complete_demo()
        self.assertFalse(self.report()['ok'], 'A previous handoff cannot certify a newly requested restart')
        self.worker.alive = False  # Current restart failed before it produced a health result.
        self.timer.timeout.emit()
        self.assertFalse(self.report()['ok'])
        self.assertFalse(self.timer.running)

    def test_failed_restart_with_a_stale_handoff_field_is_not_success(self):
        self.start()
        self.complete_demo()
        self.complete_restart({'ok': False, 'error': 'Final handoff failed', 'handoff': {'ok': True}})
        self.assertFalse(self.report()['ok'])


if __name__ == '__main__':
    unittest.main()
