"""Workspace repair policy. Only the desktop UI may persist Class B consent."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading


CLASSES = {
    'A': ('source_read', 'source_edit', 'diagnostics', 'tests', 'backup', 'editor', 'restart', 'screenshot'),
    'B': ('dependencies', 'venv', 'app_config', 'model_download'),
    'C': ('administrator', 'outside_workspace', 'system_settings', 'unrelated_process', 'destructive',
          'upload', 'git_publish', 'persistent_service'),
}
_SECRET_NAME = re.compile(r'api[_-]?key|token|password|passwd|secret|authorization|credential|webhook', re.I)
_SECRET_VALUES = set()
_SECRET_LOCK = threading.RLock()
_NAMED_SECRET = re.compile(
    r'''(?ix)(?<![\w.-])((?:["']?[\w.-]*(?:api[_-]?key|token|password|passwd|secret|authorization|credential|webhook)[\w.-]*["']?)\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)''')
_PROTECTED_WRITES = {'core/repair_permissions.py', 'core/diagnostic_commands.py', 'logs/repair/permissions.json'}


def redact(text) -> str:
    """Sanitize diagnostic text before it reaches journals, models, or widgets."""
    value = str(text)
    with _SECRET_LOCK:
        for secret in sorted(_SECRET_VALUES, key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
    value = _NAMED_SECRET.sub(lambda match: match.group(1) + '[REDACTED]', value)
    value = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', value)
    value = re.sub(r'\bAIza[\w-]{30,}|\bsk-[\w-]{20,}', '[REDACTED]', value)
    return value


def _redact_event(value):
    if isinstance(value, dict):
        return {str(key): ('[REDACTED]' if _SECRET_NAME.search(str(key)) else _redact_event(item))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_event(item) for item in value]
    return redact(value) if isinstance(value, (str, Path)) else value


class PermissionBroker:
    def __init__(self, workspace: Path, audit=None):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError('Repair workspace must be a directory.')
        self.audit = audit
        self._lock = threading.RLock()
        self._preapproved = {name: False for name in CLASSES['B']}
        self._load_secrets()
        self._policy = self._resolve('logs/repair/permissions.json')
        if self._policy.exists():
            try:
                if self._policy.stat().st_size > 16384:
                    raise ValueError('Permission file exceeds limit')
                data = json.loads(self._policy.read_text(encoding='utf-8'))
                approved = data.get('preapproved', {})
                if not isinstance(approved, dict):
                    raise ValueError('Invalid permission file')
                self._preapproved.update({name: approved.get(name) is True for name in CLASSES['B']})
            except (OSError, ValueError, AttributeError) as exc:
                self._emit({'type': 'permission_load_failed', 'error': type(exc).__name__})

    def _emit(self, event):
        if self.audit:
            self.audit(_redact_event(event))

    def _load_secrets(self):
        values = {value for key, value in os.environ.items() if _SECRET_NAME.search(key) and len(value) >= 6}
        try:
            path = self._resolve('config/api_keys.json')
            if path.is_file() and path.stat().st_size <= 1024 * 1024:
                def collect(value, sensitive=False):
                    if isinstance(value, dict):
                        for key, item in value.items():
                            collect(item, sensitive or bool(_SECRET_NAME.search(str(key))))
                    elif isinstance(value, list):
                        for item in value:
                            collect(item, sensitive)
                    elif sensitive and isinstance(value, str) and len(value) >= 6:
                        values.add(value)
                collect(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError, PermissionError):
            pass
        with _SECRET_LOCK:
            _SECRET_VALUES.update(values)

    def _resolve(self, relative) -> Path:
        if not isinstance(relative, (str, os.PathLike)) or not str(relative) or '\x00' in str(relative):
            raise PermissionError('A workspace-relative path is required.')
        raw = Path(relative)
        candidate = raw if raw.is_absolute() else self.workspace / raw
        try:
            lexical = candidate.absolute().relative_to(self.workspace)
            if any(part in {'.git', '.agents', '.codex'} for part in (p.lower() for p in lexical.parts)):
                raise PermissionError('Repository metadata and agent configuration are protected.')
            if any(':' in part or part.rstrip(' .') != part for part in lexical.parts):
                raise PermissionError('Alternate streams and ambiguous Windows paths are prohibited.')
            cursor = self.workspace
            for part in lexical.parts:
                cursor = cursor / part
                try:
                    info = cursor.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise PermissionError('Symlinks and reparse points are not repair targets.')
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    raise PermissionError('Hardlinked files are not repair targets.')
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.workspace)
        except (ValueError, OSError) as exc:
            raise PermissionError('Path must remain inside the repair workspace.') from exc
        return resolved

    def path(self, relative, write=False) -> Path:
        path = self._resolve(relative)
        rel = path.relative_to(self.workspace).as_posix().lower()
        names = [part.lower() for part in path.relative_to(self.workspace).parts]
        if (any(name == '.env' or name.startswith('.env.') or _SECRET_NAME.search(name) for name in names)
                or path.suffix.lower() in {'.key', '.pem', '.pfx', '.p12'}):
            raise PermissionError('Credential and secret files are protected.')
        if write and rel in _PROTECTED_WRITES:
            raise PermissionError('The repair permission boundary cannot edit itself or its policy.')
        return path

    def check(self, capability, scope='', reason='') -> dict:
        category = next((name for name, capabilities in CLASSES.items() if capability in capabilities), 'unknown')
        decision = {'capability': capability, 'scope': str(scope), 'class': category,
                    'allowed': False, 'approval_required': False, 'reason': str(reason),
                    'requested_reason': str(reason)}
        explanation = ''
        try:
            if category == 'unknown':
                explanation = 'Unknown capability; permission denied.'
            elif category == 'C':
                decision['approval_required'] = True
                explanation = ('Administrator actions require a specific desktop approval and legitimate Windows UAC elevation.'
                               if capability == 'administrator' else 'This action requires a specific desktop approval; persistent or tool-supplied approval cannot grant it.')
            elif category == 'B' and not self._preapproved[capability]:
                decision['approval_required'] = True
                explanation = 'Enable this Class B capability in desktop Repair Permissions first.'
            else:
                if scope and capability in {'source_read', 'source_edit', 'backup', 'editor', 'app_config', 'venv'}:
                    path = self.path(scope, write=capability in {'source_edit', 'app_config', 'venv'})
                    first = path.relative_to(self.workspace).parts[0].lower() if path != self.workspace else ''
                    extra = ('app_config' if first == 'config' else 'venv' if first in {'.venv', 'venv'} else None)
                    if capability == 'source_edit' and extra and not self._preapproved[extra]:
                        decision['approval_required'] = True
                        explanation = f'This path requires Class B {extra} permission.'
                decision['allowed'] = not explanation
        except (PermissionError, ValueError, TypeError) as exc:
            explanation = str(exc)
        decision['reason'] = explanation or str(reason) or 'Allowed within the configured repair workspace.'
        decision = _redact_event(decision)
        self._emit({'type': 'permission_checked', **decision})
        return decision

    def require(self, capability, scope='', reason='') -> dict:
        decision = self.check(capability, scope, reason)
        if not decision['allowed']:
            error = PermissionError(decision['reason'])
            error.decision = decision
            raise error
        return decision

    def set_preapproved(self, capability, enabled: bool):
        """Desktop UI entry point; never expose this as an agent tool argument."""
        if capability not in CLASSES['B'] or type(enabled) is not bool:
            raise ValueError('Only Class B capabilities accept a boolean desktop preference.')
        with self._lock:
            path = self._resolve('logs/repair/permissions.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            updated = {**self._preapproved, capability: enabled}
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, suffix='.tmp', delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump({'version': 1, 'preapproved': updated}, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._resolve(path)
                os.replace(temporary, path)
                self._preapproved = updated
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
            self._emit({'type': 'permission_changed', 'capability': capability, 'enabled': enabled})
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            return {'workspace': str(self.workspace), 'preapproved': dict(self._preapproved),
                    'classes': {key: list(value) for key, value in CLASSES.items()}}
