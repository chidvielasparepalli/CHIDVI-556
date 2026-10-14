"""Validated provider metadata and roles, separate from encrypted credentials."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import math
import time
import uuid
from pathlib import Path

from core.credential_store import (CredentialStore, CredentialStoreError, atomic_write,
                                   file_lock, safe_path, validate_identifier, validate_secret)
from core.providers import model_capabilities, validate_profile
from core.repair_permissions import redact


ROLES = ("chat", "vision", "live_audio", "tts", "embeddings")
_ROLE_CAPABILITY = {"chat": "text", **{key: key for key in ROLES[1:]}}
_FIELDS = {"id", "name", "provider", "model", "base_url", "auth_type", "capabilities", "limits",
           "status", "connected", "last_error", "last_tested", "last_usage", "created_at", "updated_at"}
_COMPUTED = {"has_credential", "warnings", "effective_capabilities"}
_RECIPIENT = ("provider", "base_url", "auth_type")
_USAGE = {"input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
          "prompt_tokens", "completion_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
          "input_audio_tokens", "output_audio_tokens", "requests"}
_SELECTION = ContextVar("provider_selection", default=None)
_DEFAULTS = {}
_UNSPECIFIED = object()
LEGACY_LIVE_MODEL = "gemini-3.1-flash-live-preview"
LEGACY_VISION_MODEL = "gemini-flash-lite-latest"


class ProviderConfigError(RuntimeError):
    """Safe configuration error without credentials or raw file contents."""


def _text(value, *, limit=120, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("Profile text is empty or exceeds its allowed length")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Profile text contains control characters")
    return value.strip()


def _usage(value):
    if not isinstance(value, dict) or set(value) - _USAGE:
        raise ValueError("Usage must contain supported numeric counters")
    if any(type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 10**12
           for number in value.values()):
        raise ValueError("Usage counters must be finite nonnegative numbers")
    return dict(value)


def _limits(value):
    if not isinstance(value, dict) or set(value) - {"max_output_tokens", "session_token_warning"}:
        raise ValueError("Unknown profile limit")
    result = {"max_output_tokens": 1024, "session_token_warning": 0, **value}
    for key, lower, upper in (("max_output_tokens", 1, 16384), ("session_token_warning", 0, 10_000_000)):
        if type(result[key]) is not int or not lower <= result[key] <= upper:
            raise ValueError("Profile limit is outside its allowed range")
    return result


class ProviderStore:
    def __init__(self, config_dir=None, vault=None, *, legacy_path=None):
        if config_dir is None:
            from memory import config_manager
            config_dir = config_manager.CONFIG_DIR
            if legacy_path is None:
                legacy_path = config_manager.CONFIG_FILE
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / "providers.json"
        self.legacy_path = Path(legacy_path) if legacy_path is not None else self.config_dir / "api_keys.json"
        self.vault = vault if vault is not None else CredentialStore(self.config_dir / "provider_credentials.dpapi")
        self._lock = file_lock(self.path)
        self._identity = str(self.path.absolute()).casefold()

    def _profile(self, value):
        if not isinstance(value, dict) or set(value) - _FIELDS:
            raise ValueError("Unknown or sensitive provider profile field")
        profile = dict(value)
        profile["id"] = validate_identifier(profile.get("id"))
        profile["name"] = _text(profile.get("name", profile["id"]))
        profile = validate_profile(profile)
        profile["limits"] = _limits(profile.get("limits", {}))
        profile["status"] = profile.get("status", "untested")
        if profile["status"] not in {"untested", "connected", "disconnected", "error"}:
            raise ValueError("Unknown provider status")
        if "connected" in profile and type(profile["connected"]) is not bool:
            raise ValueError("Connected status must be a boolean")
        if "last_error" in profile:
            profile["last_error"] = _text(profile["last_error"], limit=500, empty=True)
        if "last_usage" in profile:
            profile["last_usage"] = _usage(profile["last_usage"])
        for field in ("created_at", "updated_at", "last_tested"):
            if field in profile and (type(profile[field]) not in (int, float)
                                     or not math.isfinite(profile[field]) or profile[field] < 0):
                raise ValueError("Profile timestamp is invalid")
        return profile

    def _load(self):
        try:
            path = safe_path(self.path)
            if not path.exists():
                return {"version": 1, "profiles": {}, "roles": {}, "overrides": {}}
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("too large")
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or set(data) != {"version", "profiles", "roles", "overrides"}
                    or data["version"] != 1 or not isinstance(data["profiles"], dict)
                    or len(data["profiles"]) > 128):
                raise ValueError("invalid format")
            profiles = {identifier: self._profile(profile) for identifier, profile in data["profiles"].items()}
            if any(identifier != profile["id"] for identifier, profile in profiles.items()):
                raise ValueError("profile mismatch")
            for mapping, known in ((data["roles"], ROLES), (data["overrides"], None)):
                if not isinstance(mapping, dict) or len(mapping) > 64:
                    raise ValueError("invalid mapping")
                for key, identifier in mapping.items():
                    validate_identifier(key)
                    if known is not None and key not in known or identifier not in profiles:
                        raise ValueError("invalid selection")
            data["profiles"] = profiles
            return data
        except Exception:
            raise ProviderConfigError("Provider configuration is unreadable; existing files were preserved") from None

    def _write(self, data):
        try:
            atomic_write(self.path, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))
        except Exception:
            raise ProviderConfigError("Provider configuration could not be saved") from None

    def _public(self, profile):
        result = deepcopy(profile)
        result["has_credential"] = bool(self.vault.get(profile["id"]))
        caps = model_capabilities(profile["provider"], profile["model"], profile.get("capabilities"))
        result["effective_capabilities"] = caps
        unknown = [name for name in ("text", "vision", "tools", "streaming") if caps[name] is None]
        result["warnings"] = (["Model capabilities are unverified: " + ", ".join(unknown)
                               + ". Test the model and confirm supported capabilities."] if unknown else [])
        return result

    def list_profiles(self):
        with self._lock:
            return [self._public(profile) for profile in self._load()["profiles"].values()]

    def get_profile(self, identifier):
        validate_identifier(identifier)
        with self._lock:
            profile = self._load()["profiles"].get(identifier)
            return self._public(profile) if profile is not None else None

    def credential(self, identifier):
        validate_identifier(identifier)
        with self._lock:
            current = self._load()["profiles"].get(identifier)
            if current is None:
                return None
            frozen = _SELECTION.get()
            previous = frozen[1]["profiles"].get(identifier) if frozen and frozen[0] == self._identity else None
            if previous and any(current.get(name) != previous.get(name) for name in _RECIPIENT):
                # A new endpoint's key must never reach a still-running task that
                # pinned the old endpoint. Ordinary same-recipient rotation works.
                return None
            return self.vault.get(identifier)

    def snapshot(self):
        with self._lock:
            data = self._load()
            return {"version": 1, "profiles": [self._public(p) for p in data["profiles"].values()],
                    "roles": dict(data["roles"]), "overrides": dict(data["overrides"])}

    @contextmanager
    def selection(self):
        """Freeze role/model metadata for a task; credential revocation stays live.

        Nested tool dispatch must preserve its outer task's selection. ContextVar
        also propagates to asyncio.to_thread workers without changing GUI reads.
        """
        current = _SELECTION.get()
        if current is not None and current[0] == self._identity:
            yield self
            return
        with self._lock:
            data = deepcopy(self._load())
        token = _SELECTION.set((self._identity, data))
        try:
            yield self
        finally:
            _SELECTION.reset(token)

    def save_profile(self, profile, key=None):
        if not isinstance(profile, dict):
            raise TypeError("Provider profile must be an object")
        incoming = {name: value for name, value in profile.items() if name not in _COMPUTED}
        if set(incoming) - _FIELDS:
            raise ValueError("Unknown or sensitive provider profile field")
        identifier = validate_identifier(incoming.get("id") or uuid.uuid4().hex)
        if key is not None and key != "":
            key = validate_secret(key)
        with self._lock:
            data = self._load()
            existing = data["profiles"].get(identifier, {})
            if not existing and len(data["profiles"]) >= 128:
                raise ValueError("Provider profile limit reached")
            merged = {**existing, **incoming, "id": identifier}
            if any(field in incoming and incoming[field] != existing.get(field) for field in ("model", "provider")):
                if "capabilities" not in incoming:
                    merged["capabilities"] = {}
                if "provider" in incoming and incoming["provider"] != existing.get("provider") and "base_url" not in incoming:
                    merged.pop("base_url", None)
            merged.setdefault("created_at", time.time())
            merged["updated_at"] = time.time()
            validated = self._profile(merged)
            if "last_error" in validated:
                if key:
                    validated["last_error"] = validated["last_error"].replace(key, "[REDACTED]")
                validated["last_error"] = self._safe_error(validated["last_error"], data)
            # Credentials are accepted only by the separate key argument. Refuse
            # accidental paste into a name/model/URL, including another profile's key.
            public_text = json.dumps(validated, ensure_ascii=False)
            known_keys = [self.vault.get(item) for item in data["profiles"]]
            if key:
                known_keys.append(key)
            if any(secret and secret in public_text for secret in known_keys):
                raise ValueError("Credentials cannot be included in provider metadata")
            old_key = self.vault.get(identifier)
            if existing and old_key and key is None and any(existing.get(name) != validated.get(name) for name in _RECIPIENT):
                raise ValueError("Changing a provider, endpoint or authentication requires an explicit new credential or removal")
            effective_key = old_key if key is None else key or None
            if existing and (effective_key != old_key or any(
                    existing.get(name) != validated.get(name) for name in (*_RECIPIENT, "model"))):
                # Connection results belong to the exact recipient/model/key tested.
                # A UI save can include stale metadata from its original form load.
                validated.update(status="untested", connected=False)
                for name in ("last_error", "last_tested", "last_usage"):
                    validated.pop(name, None)
            try:
                if key == "":
                    self.vault.delete(identifier)
                elif key is not None:
                    self.vault.set(identifier, key)
                    if self.vault.get(identifier) != key:
                        raise CredentialStoreError("Credential readback failed")
                data["profiles"][identifier] = validated
                self._write(data)
            except Exception:
                if key is not None:
                    try:
                        self.vault.set(identifier, old_key) if old_key is not None else self.vault.delete(identifier)
                    except Exception:
                        raise ProviderConfigError("Profile save failed; encrypted credential recovery is required") from None
                raise ProviderConfigError("Provider profile could not be saved; existing metadata was preserved") from None
            return self._public(validated)

    def remove_profile(self, identifier):
        validate_identifier(identifier)
        with self._lock:
            original = self._load()
            if identifier not in original["profiles"]:
                return False
            data = deepcopy(original)
            del data["profiles"][identifier]
            for name in ("roles", "overrides"):
                data[name] = {key: value for key, value in data[name].items() if value != identifier}
            self._write(data)
            try:
                self.vault.delete(identifier)
            except Exception:
                self._write(original)
                raise ProviderConfigError("Encrypted credential removal failed; profile was preserved") from None
            return True

    def _compatible(self, profile, role):
        caps = model_capabilities(profile["provider"], profile["model"], profile.get("capabilities"))
        return caps[_ROLE_CAPABILITY[role]] is not False

    def set_role(self, role, identifier):
        if role not in ROLES:
            raise ValueError("Unknown provider role")
        with self._lock:
            data = self._load()
            if identifier is None:
                data["roles"].pop(role, None)
            else:
                validate_identifier(identifier)
                profile = data["profiles"].get(identifier)
                if profile is None or not self._compatible(profile, role):
                    raise ValueError("Profile does not support the selected role")
                data["roles"][role] = identifier
            self._write(data)

    def set_override(self, task_kind, identifier):
        validate_identifier(task_kind)
        with self._lock:
            data = self._load()
            if identifier is None:
                data["overrides"].pop(task_kind, None)
            else:
                validate_identifier(identifier)
                if identifier not in data["profiles"]:
                    raise ValueError("Unknown provider profile")
                if len(data["overrides"]) >= 64 and task_kind not in data["overrides"]:
                    raise ValueError("Task override limit reached")
                data["overrides"][task_kind] = identifier
            self._write(data)

    def selected(self, role="chat", task_kind=None):
        if role not in ROLES:
            raise ValueError("Unknown provider role")
        if task_kind is not None:
            validate_identifier(task_kind)
        with self._lock:
            frozen = _SELECTION.get()
            data = frozen[1] if frozen is not None and frozen[0] == self._identity else self._load()
            identifier = data["overrides"].get(task_kind) if task_kind is not None else None
            identifier = identifier or data["roles"].get(role)
            profile = data["profiles"].get(identifier)
            if profile is None or not self._compatible(profile, role):
                return None
            return self._public(profile)

    def has_configuration(self):
        profile = self.selected("chat")
        return bool(profile and (profile["auth_type"] == "none" or profile["has_credential"]))

    def _safe_error(self, value, data):
        if not isinstance(value, str):
            raise TypeError("Provider error must be text")
        # Redact before truncating: cutting a long API token first would leave
        # an unrecognizable, unmasked credential prefix in public metadata.
        if len(value) > 32000:
            return "Provider error details exceeded the safe display limit."
        for identifier in data["profiles"]:
            secret = self.vault.get(identifier)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = redact(value)
        return " ".join(value.split())[:500]

    def record_status(self, identifier, connected, error="", usage=None, *,
                      expected_profile=None, expected_credential=_UNSPECIFIED):
        validate_identifier(identifier)
        if type(connected) is not bool:
            raise TypeError("Connected status must be a boolean")
        numeric_usage = _usage(usage) if usage is not None else None
        if expected_profile is not None and not isinstance(expected_profile, dict):
            raise TypeError("Expected provider profile must be an object")
        with self._lock:
            data = self._load()
            profile = data["profiles"].get(identifier)
            if expected_profile is not None or expected_credential is not _UNSPECIFIED:
                if profile is None:
                    return None
                if expected_profile is not None and any(profile.get(name) != expected_profile.get(name)
                        for name in ("id", "provider", "base_url", "model", "auth_type")):
                    return None
                if expected_credential is not _UNSPECIFIED and self.vault.get(identifier) != expected_credential:
                    return None
            if profile is None:
                raise ValueError("Unknown provider profile")
            safe_error = self._safe_error(error, data)
            profile.update(connected=connected, status="connected" if connected else "error" if safe_error else "disconnected",
                           last_error=safe_error, last_tested=time.time())
            if numeric_usage is not None:
                profile["last_usage"] = numeric_usage
            self._write(data)
            return self._public(profile)

    def migrate_legacy(self):
        """Explicit migration only: verify ciphertext and recovery before removal."""
        with self._lock, file_lock(self.legacy_path):
            try:
                legacy_path = safe_path(self.legacy_path)
                if not legacy_path.exists():
                    return {"ok": True, "migrated": False, "code": "no_legacy_key"}
                if legacy_path.stat().st_size > 1024 * 1024:
                    raise ValueError("legacy too large")
                original = legacy_path.read_bytes()
                legacy = json.loads(original)
                if not isinstance(legacy, dict):
                    raise ValueError("legacy must be object")
                key = legacy.get("gemini_api_key")
                if key is None or key == "":
                    return {"ok": True, "migrated": False, "code": "no_legacy_key"}
                key = validate_secret(key)
                data = self._load()
                live_model = legacy.get("live_model") or legacy.get("gemini_live_model") or LEGACY_LIVE_MODEL
                vision_model = legacy.get("vision_model") or LEGACY_VISION_MODEL
                recipes = (("legacy-gemini-live", "Gemini Live", live_model),
                           ("legacy-gemini-vision", "Gemini Vision", vision_model))
                created = []
                for identifier, name, model in recipes:
                    proposed = self._profile({"id": identifier, "name": name, "provider": "gemini", "model": model,
                                              "created_at": time.time(), "updated_at": time.time()})
                    previous = data["profiles"].get(identifier)
                    if previous and (previous["provider"] != "gemini" or previous["model"] != proposed["model"]
                                     or self.vault.get(identifier) not in (None, key)):
                        return {"ok": False, "migrated": False, "code": "migration_conflict",
                                "message": "An existing Gemini profile differs; legacy configuration was preserved."}
                    self.vault.set(identifier, key)
                    if self.vault.get(identifier) != key:
                        raise CredentialStoreError("Migration credential verification failed")
                    if not previous:
                        data["profiles"][identifier] = proposed
                        created.append(identifier)
                backup = self.vault.backup(original, label="legacy-api-keys")
                data["roles"].setdefault("chat", recipes[0][0])
                data["roles"].setdefault("live_audio", recipes[0][0])
                data["roles"].setdefault("vision", recipes[1][0])
                if legacy_path.read_bytes() != original:
                    return {"ok": False, "migrated": False, "code": "legacy_changed",
                            "message": "Legacy configuration changed during migration; its newer contents were preserved."}
                self._write(data)
                if legacy_path.read_bytes() != original:
                    return {"ok": False, "migrated": False, "code": "legacy_changed",
                            "message": "Legacy configuration changed during migration; its newer contents were preserved."}
                cleaned = dict(legacy)
                del cleaned["gemini_api_key"]
                atomic_write(legacy_path, json.dumps(cleaned, indent=4, ensure_ascii=False).encode("utf-8"))
                return {"ok": True, "migrated": True, "code": "migrated", "profiles_created": created,
                        "backup_path": str(backup), "removed_fields": ["gemini_api_key"]}
            except Exception:
                return {"ok": False, "migrated": False, "code": "migration_failed",
                        "message": "Credential migration failed; legacy credentials were preserved. Check local encrypted storage."}

    def gemini_credential(self):
        for role in ("live_audio", "chat", "vision"):
            profile = self.selected(role)
            if profile and profile["provider"] == "gemini":
                key = self.credential(profile["id"])
                if key:
                    return key
        for profile in self.list_profiles():
            if profile["provider"] == "gemini":
                key = self.credential(profile["id"])
                if key:
                    return key
        return None

    def save_gemini_key(self, key):
        """Compatibility setup/rotation; it never puts a key in api_keys.json."""
        key = validate_secret(key)
        report = self.migrate_legacy()
        if not report["ok"]:
            raise ProviderConfigError(report["message"])
        live = self.selected("live_audio")
        if not live or live["provider"] != "gemini":
            live = {"id": "legacy-gemini-live", "name": "Gemini Live", "provider": "gemini", "model": LEGACY_LIVE_MODEL}
        live = self.save_profile(live, key=key)
        vision = self.selected("vision")
        if not vision or vision["provider"] != "gemini":
            vision = {"id": "legacy-gemini-vision", "name": "Gemini Vision", "provider": "gemini", "model": LEGACY_VISION_MODEL}
        vision = self.save_profile(vision, key=key)
        for role, profile in (("chat", live), ("live_audio", live), ("vision", vision)):
            if self.selected(role) is None:
                self.set_role(role, profile["id"])


def get_provider_store():
    from memory import config_manager
    key = (str(Path(config_manager.CONFIG_DIR).absolute()), str(Path(config_manager.CONFIG_FILE).absolute()))
    with file_lock(Path(config_manager.CONFIG_DIR) / "providers.json"):
        if key not in _DEFAULTS:
            _DEFAULTS[key] = ProviderStore(config_manager.CONFIG_DIR, legacy_path=config_manager.CONFIG_FILE)
        return _DEFAULTS[key]
