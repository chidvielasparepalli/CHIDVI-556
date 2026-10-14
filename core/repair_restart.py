"""Replacement-process health handshake; never terminate the current or unrelated app."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import psutil

from core.diagnostic_commands import _ProcessTree, _CREATE_SUSPENDED


def _health_path(workspace, path):
    directory = (Path(workspace).resolve() / 'logs' / 'repair' / 'runtime').resolve()
    if not directory.is_relative_to(Path(workspace).resolve()):
        raise ValueError('Health directory escapes the workspace')
    target = Path(path).resolve()
    if not target.is_relative_to(directory) or target.suffix != '.json':
        raise ValueError('Health records must stay in logs/repair/runtime')
    directory.mkdir(parents=True, exist_ok=True)
    return target


def write_health(workspace, path, token, mode, ready, **details):
    target = _health_path(workspace, path)
    record = {**details, 'ready': ready is True, 'pid': os.getpid(), 'token': token,
              'heartbeat': time.time(), 'mode': mode}
    # Readers reject partial JSON; heartbeat files are disposable, not recovery records.
    target.write_text(json.dumps(record), encoding='utf-8')


class RestartSupervisor:
    def __init__(self, workspace, diagnostic=False, emit=lambda _: None):
        self.workspace = Path(workspace).resolve()
        self.diagnostic, self.emit = diagnostic, emit
        self.child = None
        self.attempts = 0
        self.handoff_pending = False
        self._tree = None
        self._handoff = None
        self._handed_off = False
        self._released_child = None

    def restart(self, cancel_event, timeout=30):
        if cancel_event.is_set():
            return {'ok': False, 'cancelled': True}
        if self.handoff_pending or self._handed_off or self.attempts >= 2:
            return {'ok': False, 'error': 'Restart limit reached; a new diagnosis is required.'}
        self.attempts += 1
        token = secrets.token_hex(24)
        health = _health_path(self.workspace, self.workspace/'logs'/'repair'/'runtime'/f'{token}.json')
        python = self.workspace/'.venv'/'Scripts'/'python.exe'
        if not python.is_file():
            python = Path(sys.executable)
        argv = [str(python), str(self.workspace/'main.py'), '--health-file', str(health), '--health-token', token]
        if self.diagnostic:
            argv.append('--diagnostic-mode')
        self.emit({'phase': 'Restarting', 'message': 'Starting a replacement; current process stays available until healthy.'})
        log = health.with_suffix('.log')
        try:
            self._tree = _ProcessTree()
            with log.open('wb') as output:
                self.child = subprocess.Popen(argv, cwd=self.workspace, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT,
                    creationflags=(subprocess.CREATE_NO_WINDOW | _CREATE_SUSPENDED) if os.name == 'nt' else 0,
                    start_new_session=os.name != 'nt')
            self._tree.attach(self.child, resume=os.name == 'nt')
            deadline = time.monotonic() + min(60, max(1, timeout))
            first_heartbeat = None
            while time.monotonic() < deadline and not cancel_event.wait(.1):
                code = self.child.poll()
                if code is not None:
                    self.stop_candidate()
                    return {'ok': False, 'exit_code': code, 'error': 'Replacement exited before UI health was verified.', 'log': str(log)}
                try:
                    record = json.loads(health.read_text(encoding='utf-8'))
                    owned = {self.child.pid, *(process.pid for process in psutil.Process(self.child.pid).children(recursive=True))}
                    valid = (record.get('token') == token and record.get('pid') in owned
                             and record.get('ready') is True and 0 <= time.time()-record['heartbeat'] < 3)
                    if valid:
                        if first_heartbeat is not None and record['heartbeat'] > first_heartbeat:
                            self.handoff_pending = True
                            self._handoff = {'path': health, 'token': token, 'pid': record['pid'], 'cancel_event': cancel_event}
                            return {'ok': True, 'pid': record['pid'], 'mode': record.get('mode'),
                                    'health_file': str(health), 'verified': 'Two fresh UI event-loop heartbeats',
                                    'provider_connected': record.get('provider_connected', False)}
                        first_heartbeat = record['heartbeat']
                except (OSError, ValueError, KeyError, TypeError, psutil.Error):
                    pass
            self.stop_candidate()
            return {'ok': False, 'cancelled': cancel_event.is_set(),
                    'error': 'Replacement health did not become ready; current application remains running.', 'log': str(log)}
        except OSError as exc:
            self.stop_candidate()
            return {'ok': False, 'error': 'Replacement could not start.', 'error_type': type(exc).__name__}

    def commit_handoff(self):
        """Release a verified replacement only after the final repair journal is saved."""
        if not self.handoff_pending or self._handoff is None or self._tree is None:
            return {'ok': False, 'error': 'No health-verified replacement is pending handoff.'}
        handoff = self._handoff
        if handoff['cancel_event'].is_set():
            return {'ok': False, 'cancelled': True, 'error': 'Replacement handoff was cancelled.'}
        if self.child is None or self.child.poll() is not None:
            return {'ok': False, 'error': 'Replacement exited before handoff could complete.'}
        # Heartbeats are deliberately disposable writes; retry a partial read briefly.
        valid = False
        for _ in range(3):
            try:
                record = json.loads(handoff['path'].read_text(encoding='utf-8'))
                owned = {self.child.pid, *(process.pid for process in psutil.Process(self.child.pid).children(recursive=True))}
                valid = (record.get('token') == handoff['token'] and record.get('pid') == handoff['pid']
                         and record['pid'] in owned and record.get('ready') is True
                         and 0 <= time.time() - record['heartbeat'] < 3)
                if valid:
                    break
            except (OSError, ValueError, KeyError, TypeError, psutil.Error):
                pass
            if handoff['cancel_event'].wait(.02):
                break
        if not valid or handoff['cancel_event'].is_set():
            return {'ok': False, 'error': 'Replacement health is no longer current; handoff was not committed.'}
        try:
            self._tree.release()
        except OSError as exc:
            return {'ok': False, 'error': 'Replacement ownership could not be released.', 'error_type': type(exc).__name__}
        self._tree = None
        self._released_child, self.child = self.child, None
        self._handoff = None
        self.handoff_pending = False
        self._handed_off = True
        return {'ok': True, 'status': 'handed_off', 'pid': record['pid']}

    def stop_candidate(self):
        # The job/group owns descendants even if their launcher already exited.
        tree, self._tree = self._tree, None
        if tree is not None:
            tree.close(self.child)
        if self.child is not None and self.child.poll() is None:
            try:
                self.child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=3)
        self._handoff = None
        self.handoff_pending = False
