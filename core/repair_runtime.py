"""Bind repair work to the existing task store and visible desktop controls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

from core.diagnostic_commands import CommandService
from core.repair_permissions import PermissionBroker, redact, _redact_event
from core.repair_observation import RepairObservation
from core.repair_restart import RestartSupervisor
from core.self_repair import RepairEngine


class RepairRuntime:
    def __init__(self, workspace, tasks, panel, activity=lambda _: None, diagnostic=False,
                 quit_current=lambda: None, interrupt=lambda: None, indicator=lambda _: None):
        self.workspace = Path(workspace).resolve()
        self.tasks, self.panel, self.activity = tasks, panel, activity
        self.quit_current, self.interrupt, self.indicator = quit_current, interrupt, indicator
        self.enabled = False
        self.session_id = None
        self._worker_lock = threading.Lock()
        self._cancel = threading.Event()
        self._closed = threading.Event()
        self._thread = None
        self._failure_keys = {}
        self._failure_lock = threading.Lock()
        self._last_failure = None
        self._demo_engines = {}
        self._audit_lock = threading.Lock()
        self._audit_error = None
        self._observation_options = {'interval': 5, 'monitor': 1, 'retain': False}
        self.broker = PermissionBroker(self.workspace, audit=self.publish)
        self.commands = CommandService(self.workspace, self.broker, emit=self.publish)
        self.observation = RepairObservation(self.workspace, self.publish)
        self.restart = RestartSupervisor(self.workspace, diagnostic=diagnostic, emit=self.publish)
        self.engine = RepairEngine(self.workspace, self.broker, self.commands, emit=self.publish,
            observe=self.observation.refresh, restart=lambda: self.restart.restart(self.tasks.event(self.tasks.owner())))
        panel.on_mode = self.set_mode
        panel.on_stop = self.stop
        panel.on_command = self.dispatch
        panel.on_observation = self.set_observation
        panel.on_permissions = self.set_permission
        panel.on_rollback = lambda identity: self.dispatch({'action': 'rollback', 'failure_id': identity})
        panel.on_restart = lambda: self.dispatch({'action': 'restart'})
        panel.on_shortcut_changed = self.set_shortcut
        panel.set_permissions(self.broker.snapshot()['preapproved'])
        self._restore_demo_history()
        self.refresh_history()

    def publish(self, event):
        safe = _redact_event(event)
        kind = safe.get('type', '')
        if kind == 'command_started':
            safe.update(phase='Running diagnostics', output='$ ' + safe.get('command', ''))
        elif kind in {'command_finished', 'dependency_preflight'}:
            safe['output'] = json.dumps(safe, ensure_ascii=False)
        if 'status' in safe and 'phase' not in safe:
            safe['phase'] = str(safe['status']).replace('_', ' ').title()
        self.panel.publish(safe)
        if safe.get('phase'):
            self.activity('REPAIR: ' + redact(safe.get('message') or str(safe['phase'])))
        if 'monitoring' in safe:
            self.indicator('Repair monitoring ON · local screenshots' if safe['monitoring'] else 'Repair monitoring OFF')
        if hasattr(self, 'broker'):
            try:
                path = self.broker.path('logs/repair/activity.jsonl')
                path.parent.mkdir(parents=True, exist_ok=True)
                record = json.dumps({'at': time.time(), **safe}, ensure_ascii=False)
                if len(record) > 100000:
                    record = json.dumps({'at': time.time(), 'type': 'bounded_event',
                        'phase': safe.get('phase'), 'preview': record[:32000], 'truncated': True}, ensure_ascii=False)
                with self._audit_lock, path.open('a', encoding='utf-8') as stream:
                    stream.write(record + '\n')
                self._audit_error = None
            except OSError as exc:
                self._audit_error = type(exc).__name__
                self.panel.publish({'phase': 'Blocked', 'message': 'The repair audit log is unavailable; further operations are blocked until logging recovers.'})

    def set_mode(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Mode must be boolean')
        if not enabled:
            self.stop()
            return
        self.enabled = True
        self._cancel = threading.Event()
        self.session_id = uuid.uuid4().hex
        self.publish({'mode': True, 'phase': 'Ready', 'message': 'Self-Repair Mode enabled for this session. Screenshot monitoring is separately opt-in.'})
        self.set_shortcut(self.panel.shortcut_input.text())

    def set_permission(self, capability, enabled):
        if enabled and not self.enabled:
            raise PermissionError('Enable Self-Repair Mode before preapproving an operation')
        self.broker.set_preapproved(capability, enabled)

    def set_shortcut(self, sequence):
        try:
            active = self.enabled or getattr(self.panel, 'live_sharing_active', False) is True
            # None keeps the last configured chord while the GUI still owns a
            # live session. Only that session's OFF event releases its protection.
            self.panel.set_global_hotkey((sequence or None) if active else None)
        except Exception as exc:
            self.publish({'message': f'Global stop shortcut unavailable ({type(exc).__name__}); the repair window shortcut and Emergency Stop remain available.'})

    def set_observation(self, enabled):
        if enabled:
            if not self.enabled:
                raise PermissionError('Enable Self-Repair Mode before monitoring')
            self.broker.require('screenshot', reason='User enabled repair-session desktop monitoring')
            self._observation_options = self.panel.observation_options()
            self.observation.start(self.tasks.active_id or self.session_id, **self._observation_options)
        else:
            self.observation.pause()

    def stop(self):
        self.enabled = False
        self._cancel.set()
        if self.tasks.active_id:
            self.tasks.cancel(self.tasks.active_id)
        self.observation.stop()
        self.set_shortcut('')
        self.interrupt()
        self.publish({'mode': False, 'monitoring': False, 'phase': 'Stopped', 'message': 'Automation cancelled; no further repair commands or screenshots will start.'})

    def close(self):
        self._closed.set()
        self.stop()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def record_failure(self, error, task_id='', source='tool', traceback_text=''):
        key = (str(source)[:128], redact(str(error))[:1000])
        with self._failure_lock:
            if key in self._failure_keys:
                self._last_failure = self._failure_keys[key]
                return self._last_failure
            evidence = {'mode_enabled': self.enabled, 'diagnostic_mode': self.restart.diagnostic,
                        'permissions': self.broker.snapshot()['preapproved'], 'observation': self.observation.snapshot()}
            if task_id:
                try:
                    evidence['originating_task'] = self.tasks.get(task_id)
                except KeyError:
                    evidence['originating_task'] = 'Task is no longer in the active history window.'
            try:
                with self.broker.path('logs/repair/activity.jsonl').open('rb') as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell()-8000))
                    evidence['recent_activity'] = stream.read(8000).decode('utf-8', errors='replace')
            except OSError:
                evidence['recent_activity'] = 'No readable repair activity log is available.'
            failure_input = dict(error) if isinstance(error, dict) else {'message': str(error)}
            if 'evidence' in failure_input:
                evidence['reported_evidence'] = failure_input['evidence']
            failure_input['evidence'] = _redact_event(evidence)
            failure = self.engine.record_failure(failure_input, task_id=task_id, source=source, traceback_text=traceback_text)
            if len(self._failure_keys) >= 200:
                self._failure_keys.pop(next(iter(self._failure_keys)))
            self._failure_keys[key] = failure['id']
            self._last_failure = failure['id']
        self.refresh_history()
        if self.enabled:
            self.dispatch({'action': 'auto_repair', 'failure_id': failure['id']}, wait=True)
        return failure['id']

    def dispatch(self, parameters, wait=False):
        if self._closed.is_set():
            return
        if self._thread and self._thread.is_alive():
            self.publish({'phase': 'Blocked', 'message': 'A repair command is already running; stop or wait for its result.'})
            return
        def run():
            deadline = time.monotonic() + (30 if wait else 0)
            while self.tasks.active_id and time.monotonic() < deadline and not self._cancel.wait(.1):
                pass
            if self.tasks.active_id or self._closed.is_set():
                self.publish({'phase': 'Blocked', 'message': 'Another task owns automation. Finish or stop it before repair.'})
                return
            task = self.tasks.submit('Repair: ' + str(parameters.get('action', 'status')), uuid.uuid4().hex, executor='repair')
            if not self.tasks.claim(task['id']):
                self.tasks.cancel(task['id'])
                return
            try:
                with self.tasks.context(task['id']):
                    self.tasks.update(task['id'], 'executing', 'Running visible repair workflow')
                    self.publish({'task_id': task['id']})
                    if parameters.get('action') in {'demo', 'repair', 'auto_repair', 'diagnostics'} and self.observation.snapshot()['mode'] == 'running':
                        self.observation.start(task['id'], **self._observation_options)
                    result = self.handle(parameters)
                    self.tasks.update(task['id'], 'succeeded' if result.get('ok') else 'failed', redact(str(result.get('status') or result.get('error') or result.get('message') or 'Diagnostics complete')))
                    self.publish({'output': json.dumps(_redact_event(result), ensure_ascii=False)})
            except Exception as exc:
                self.tasks.update(task['id'], 'failed', type(exc).__name__)
                self.publish({'phase': 'Blocked', 'message': redact(str(exc))})
            finally:
                self.tasks.release(task['id'])
                self.refresh_history()
        self._thread = threading.Thread(target=run, name='chidvi-repair-task', daemon=True)
        self._thread.start()

    def handle(self, parameters):
        if (not isinstance(parameters, dict)
                or set(parameters) - {'action', 'failure_id', 'path', 'text', 'request_json'}):
            return {'ok': False, 'error': 'Use the declared repair parameters; authorization flags and arbitrary options are prohibited.'}
        action = parameters.get('action', 'status')
        allowed = {'status', 'history', 'export_history', 'diagnose', 'inspect_failure', 'read', 'search',
                   'observe', 'refresh_snapshot', 'analyze', 'open_vscode', 'diagnostics', 'command',
                   'demo', 'restart', 'repair', 'auto_repair', 'rollback', 'report'}
        if not isinstance(action, str) or action not in allowed:
            return {'ok': False, 'error': 'Unknown repair action.'}
        readonly = action in ('status', 'history')
        if not readonly and (not self.enabled or self._closed.is_set()):
            return {'ok': False, 'error': 'Enable Self-Repair Mode in the desktop repair panel first.'}
        if not readonly and self._audit_error:
            return {'ok': False, 'error': 'Repair audit logging is unavailable; operations remain blocked.'}
        task_id = self.tasks.owner()
        if not readonly:
            if not task_id or task_id != self.tasks.active_id:
                return {'ok': False, 'error': 'A current originating task must own repair automation.'}
            try:
                cancel = self.tasks.event(task_id)
                task = self.tasks.get(task_id)
            except KeyError:
                return {'ok': False, 'error': 'The originating repair task no longer exists.'}
            if cancel.is_set() or self._cancel.is_set() or task['state'] in {'succeeded', 'failed', 'cancelled'}:
                return {'ok': False, 'cancelled': True, 'error': 'The originating task has stopped.'}
        else:
            cancel = self._cancel
        if not self._worker_lock.acquire(blocking=False):
            return {'ok': False, 'error': 'Repair service is busy.'}
        try:
            if action in ('status', 'history'):
                return {'ok': True, 'enabled': self.enabled, 'observation': self.observation.snapshot(), 'history': self.refresh_history()}
            if action == 'export_history':
                path = self.broker.path('logs/repair/diagnostic-export.json')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(_redact_event(self.refresh_history()), indent=2), encoding='utf-8')
                return {'ok': True, 'path': str(path), 'message': 'Local redacted repair history exported. No files uploaded.'}
            identity = parameters.get('failure_id') or self._last_failure
            if action == 'report':
                text = parameters.get('text')
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000:
                    raise ValueError('A reported malfunction must contain 1–8000 characters.')
                identity = self.record_failure(text, task_id, source='user_report')
                return {'ok': True, 'failure_id': identity, 'diagnosis': self.engine.diagnose(identity)}
            if action in ('diagnose', 'inspect_failure'):
                if not identity:
                    return {'ok': False, 'error': 'No recorded failure is selected.'}
                result = self._demo_engines.get(identity, self.engine).diagnose(identity)
                self.publish({'phase': 'Diagnosing', 'diagnosis': result})
                return {'ok': True, 'diagnosis': result}
            if cancel.is_set() or self._cancel.is_set():
                return {'ok': False, 'cancelled': True}
            if action == 'read':
                return {'ok': True, **self.engine.read(parameters.get('path', ''))}
            if action == 'search':
                return {'ok': True, 'matches': self.engine.search(parameters.get('text', ''))}
            if action in ('observe', 'refresh_snapshot'):
                return self.observation.refresh()
            if action == 'analyze':
                return self.observation.analyze_latest(parameters.get('text', 'Describe the visible repair application.'))
            if action == 'open_vscode':
                return self.open_editor([parameters['path']] if parameters.get('path') else [])
            if action == 'diagnostics':
                results = [self.commands.run(spec, cancel) for spec in (
                    {'preset': 'python_version'}, {'preset': 'dependencies'},
                    {'preset': 'powershell_diagnostic', 'diagnostic': 'system'})]
                return {'ok': all(r['ok'] for r in results), 'diagnostics': results}
            raw = parameters.get('request_json', '{}')
            if not isinstance(raw, str) or len(raw) > 1_000_000:
                raise ValueError('request_json must be at most 1 MB')
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError('A JSON object is required')
            if action == 'command':
                return self.commands.run(request, cancel)
            if action == 'demo':
                return self.demo(task_id, cancel)
            if action == 'restart':
                self.broker.require('restart', reason='User requested controlled application replacement')
                check = self.commands.run({'preset': 'import', 'module': 'main'}, cancel)
                if not check['ok']:
                    return check
                result = self.restart.restart(cancel)
                self.last_restart = result
                self.publish({'type': 'restart_result', 'restart_status': result})
            elif action == 'repair':
                self.open_editor([item['path'] for item in request.get('files', [])])
                request['restart'] = True
                result = self.engine.repair(identity, request, task_id, cancel)
            elif action == 'auto_repair':
                diagnosis = self.engine.diagnose(identity)
                if diagnosis.get('recipe', {}).get('path'):
                    self.open_editor([diagnosis['recipe']['path']])
                result = self.engine.auto_repair(identity, task_id, cancel)
            elif action == 'rollback':
                result = self._demo_engines.get(identity, self.engine).rollback(identity)
            else:
                return {'ok': False, 'error': 'Unknown repair action'}
            if self.restart.handoff_pending:
                if result.get('ok'):
                    self.publish({'phase': 'Verifying', 'restart_status': 'Replacement UI health verified; handing over after journal completion.'})
                    handoff = self.restart.commit_handoff()
                    if not handoff.get('ok'):
                        self.restart.stop_candidate()
                        rollback = self.engine.rollback(identity) if action in {'repair', 'auto_repair'} else None
                        self.last_restart = {'ok': False, 'error': 'Replacement failed before handoff.', 'handoff': handoff, 'rollback': rollback}
                        return self.last_restart
                    self.last_restart = {**result, 'handoff': handoff}
                    self.quit_current()
                else:
                    self.restart.stop_candidate()
            return result
        except (ValueError, TypeError, OSError, PermissionError, KeyError, RuntimeError) as exc:
            return {'ok': False, 'error': redact(str(exc)), 'error_type': type(exc).__name__}
        finally:
            if action in {'repair', 'auto_repair', 'demo', 'rollback'}:
                self.observation.stop()
            self._worker_lock.release()

    def _restore_demo_history(self):
        folder = self.broker.path('logs/repair/demos')
        if not folder.is_dir():
            return
        candidates = [path for path in folder.iterdir() if re.fullmatch('[0-9a-f]{32}', path.name)]
        for path in sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[:50]:
            try:
                path = self.broker.path(path)
                if not (path/'logs/repair').is_dir():
                    continue
                broker = PermissionBroker(path, audit=self.publish)
                commands = CommandService(path, broker, emit=self.publish)
                engine = RepairEngine(path, broker, commands, emit=self.publish)
                for record in engine.history():
                    self._demo_engines[record['id']] = engine
            except (OSError, ValueError, KeyError):
                self.publish({'phase': 'Blocked', 'message': 'An isolated repair history could not be restored; its files were preserved.'})

    def refresh_history(self):
        records = self.engine.history() if hasattr(self, 'engine') else []
        for engine in set(self._demo_engines.values()):
            records.extend(engine.history())
        records.sort(key=lambda record: record.get('created_at', 0), reverse=True)
        self.panel.set_history(records)
        return records

    def open_editor(self, paths):
        self.broker.require('editor', reason='Show authoritative source and changes in VS Code')
        files = [self.broker.path(path) for path in paths]
        command = shutil.which('code')
        executable = Path(command).parent.parent/'Code.exe' if command else None
        if executable is None or not executable.is_file():
            return {'ok': False, 'error': 'Installed VS Code executable not found; source and diff remain visible in the repair panel.'}
        environment = dict(os.environ)
        environment.pop('ELECTRON_RUN_AS_NODE', None)
        subprocess.Popen([str(executable), '--reuse-window', str(self.workspace), *(str(path) for path in files)],
                         cwd=self.workspace, env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.publish({'files': [str(path) for path in files], 'message': 'VS Code opened; diagnostic commands and output appear in the repair panel.'})
        return {'ok': True, 'files': [str(path) for path in files]}

    def demo(self, task_id, cancel):
        folder = self.broker.path('logs/repair/demos/' + uuid.uuid4().hex)
        (folder/'actions').mkdir(parents=True)
        (folder/'tests').mkdir()
        source = folder/'actions'/'demo_action.py'
        source.write_text("def demo_action(parameters):\n    return 'recovered'\nTOOL = {'name': 'demo_action', 'description': 'Isolated repair demo'}\n", encoding='utf-8')
        (folder/'tests'/'test_demo_action.py').write_text("import unittest\nfrom actions.demo_action import TOOL\nclass Behavior(unittest.TestCase):\n def test_dispatch(self):\n  self.assertEqual(TOOL['handler']({}), 'recovered')\n", encoding='utf-8')
        broker = PermissionBroker(folder, audit=self.publish)
        commands = CommandService(folder, broker, emit=self.publish)
        engine = RepairEngine(folder, broker, commands, emit=self.publish, observe=self.observation.refresh)
        failure = engine.record_failure("Action rejected: actions/demo_action.py — TOOL['handler'] missing or not callable.", task_id=task_id, source='isolated_demo')
        self._demo_engines[failure['id']] = engine
        self.open_editor([str(source)])
        result = engine.auto_repair(failure['id'], task_id, cancel)
        self.refresh_history()
        return {**result, 'isolated_workspace': str(folder), 'live_installation_modified': False}
