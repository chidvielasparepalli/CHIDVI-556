"""Current-user Windows DPAPI storage. No plaintext fallback is provided.

The protector argument exists for isolated tests and platform integrations. The
application always uses WindowsDPAPI, whose ciphertext is bound to the logged-in
Windows user. Recovery files are encrypted with the same protection.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import stat
import tempfile
import threading
import uuid
from pathlib import Path


_MAGIC = b"CHIDVI-DPAPI-v1\0"
_MAX_FILE = 2 * 1024 * 1024
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class CredentialStoreError(RuntimeError):
    """A safe error; messages never include plaintext or provider responses."""


def file_lock(path):
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def validate_identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Identifier must contain 1 to 64 lowercase letters, digits, underscores or hyphens")
    return value


def validate_secret(value):
    if not isinstance(value, str):
        raise TypeError("Credential must be text")
    value = value.strip()
    if not value or len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Credential is empty, too long or contains control characters")
    return value


def safe_path(path):
    """Refuse linked credential/configuration locations before reading/writing."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise CredentialStoreError("Linked credential/configuration paths are not supported")
        if part == path and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise CredentialStoreError("Linked credential/configuration files are not supported")
    return path


def atomic_write(path, payload):
    """Write bytes to a sibling temporary file and atomically replace the target."""
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=".provider-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(path)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class WindowsDPAPI:
    """CryptProtectData with UI forbidden and without machine-wide protection."""
    def _transform(self, value, *, decrypt):
        if os.name != "nt":
            raise CredentialStoreError("Windows credential protection is unavailable on this platform")
        if not isinstance(value, bytes) or not value or len(value) > _MAX_FILE:
            raise CredentialStoreError("Credential protection input is invalid")
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        source_buffer = ctypes.create_string_buffer(value)
        source = Blob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        entropy_bytes = b"CHIDVI-556/provider-credentials/v1"
        entropy_buffer = ctypes.create_string_buffer(entropy_bytes)
        entropy = Blob(len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        method.restype = wintypes.BOOL
        try:
            if not method(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1,
                          ctypes.byref(output)):
                raise CredentialStoreError("Windows credential protection failed")
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if output.pbData:
                ctypes.memset(output.pbData, 0, output.cbData)
                kernel.LocalFree(output.pbData)
            ctypes.memset(source_buffer, 0, len(value))

    def protect(self, value):
        return self._transform(value, decrypt=False)

    def unprotect(self, value):
        return self._transform(value, decrypt=True)


class CredentialStore:
    def __init__(self, path=None, protector=None):
        if path is None:
            from memory.config_manager import CONFIG_DIR
            path = CONFIG_DIR / "provider_credentials.dpapi"
        self.path = Path(path)
        self.protector = protector if protector is not None else WindowsDPAPI()
        self._lock = file_lock(self.path)

    def _decode(self, encrypted):
        try:
            if not encrypted.startswith(_MAGIC) or len(encrypted) > _MAX_FILE:
                raise ValueError("invalid envelope")
            return self.protector.unprotect(encrypted[len(_MAGIC):])
        except Exception:
            raise CredentialStoreError("Encrypted credential data could not be read") from None

    def _encode(self, plain):
        try:
            encrypted = _MAGIC + self.protector.protect(plain)
            if self._decode(encrypted) != plain:
                raise ValueError("verification failed")
            return encrypted
        except Exception:
            raise CredentialStoreError("Encrypted credential verification failed") from None

    def _read(self):
        try:
            path = safe_path(self.path)
            if not path.exists():
                return {}
            if path.stat().st_size > _MAX_FILE:
                raise ValueError("too large")
            data = json.loads(self._decode(path.read_bytes()))
            if not isinstance(data, dict) or len(data) > 256:
                raise ValueError("invalid store")
            for identifier, value in data.items():
                validate_identifier(identifier)
                validate_secret(value)
            return data
        except Exception:
            raise CredentialStoreError("Encrypted credential store is unreadable") from None

    def _write(self, data):
        previous = None
        written = False
        observed = None
        try:
            path = safe_path(self.path)
            previous = path.read_bytes() if path.exists() else None
            plain = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
            encrypted = self._encode(plain)
            atomic_write(self.path, encrypted)
            written = True
            observed = safe_path(self.path).read_bytes()
            if observed != encrypted or self._decode(observed) != plain:
                raise CredentialStoreError("Encrypted credential readback failed")
        except Exception:
            if written:
                try:
                    # Preserve a newer external write instead of rolling over it.
                    current = safe_path(self.path).read_bytes()
                    if current == observed or current == encrypted:
                        if previous is not None:
                            atomic_write(self.path, previous)
                        else:
                            safe_path(self.path).unlink(missing_ok=True)
                except Exception:
                    raise CredentialStoreError("Encrypted credential update failed; recovery is required") from None
            raise CredentialStoreError("Encrypted credential update failed") from None

    def get(self, identifier):
        validate_identifier(identifier)
        with self._lock:
            return self._read().get(identifier)

    def set(self, identifier, key):
        validate_identifier(identifier)
        key = validate_secret(key)
        with self._lock:
            data = self._read()
            data[identifier] = key
            self._write(data)

    def delete(self, identifier):
        validate_identifier(identifier)
        with self._lock:
            data = self._read()
            if identifier not in data:
                return False
            del data[identifier]
            self._write(data)
            return True

    def backup(self, payload=None, *, label="recovery"):
        """Create and verify an encrypted recovery copy, never a plaintext file."""
        validate_identifier(label)
        with self._lock:
            try:
                if payload is None:
                    payload = json.dumps(self._read(), sort_keys=True).encode("utf-8")
                if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_FILE:
                    raise ValueError("invalid recovery data")
                target = self.path.parent / "provider_recovery" / (label + "-" + uuid.uuid4().hex + ".dpapi")
                atomic_write(target, self._encode(payload))
                if self.recover_backup(target) != payload:
                    raise ValueError("recovery verification failed")
                return target
            except Exception:
                raise CredentialStoreError("Encrypted recovery backup failed") from None

    def recover_backup(self, path):
        """Return bytes only for an explicitly selected local recovery file."""
        try:
            path = safe_path(path)
            path.relative_to(safe_path(self.path.parent / "provider_recovery"))
            if path.stat().st_size > _MAX_FILE:
                raise ValueError("too large")
            return self._decode(path.read_bytes())
        except Exception:
            raise CredentialStoreError("Encrypted recovery backup is unreadable") from None
