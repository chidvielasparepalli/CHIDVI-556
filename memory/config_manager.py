import json
import os
import sys
import tempfile
import threading
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"
_CONSENT_WRITE_LOCK = threading.RLock()

LIVE_SHARING_DEFAULTS = {"interval": 2.0, "max_per_minute": 12, "analysis_per_minute": 4,
                         "max_width": 1280, "max_height": 720, "monitor": 1,
                         "change_only": True, "session_minutes": 30}

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    """Compatibility setup API; credentials are stored with Windows DPAPI."""
    from core.provider_config import get_provider_store
    get_provider_store().save_gemini_key(gemini_api_key)

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}

def get_cloud_vision_enabled() -> bool:
    """Cloud uploads are opt-in: only a saved JSON boolean true enables them."""
    data = load_api_keys()
    return isinstance(data, dict) and data.get("cloud_vision_enabled") is True


def save_cloud_vision_enabled(enabled: bool) -> None:
    """Persist consent without replacing credentials on malformed/failed writes."""
    if type(enabled) is not bool:
        raise TypeError("Cloud Vision must be a boolean")
    _save_consent_fields({"cloud_vision_enabled": enabled})


def _save_consent_fields(values: dict) -> None:
    """Serialize every settings writer with credential migration atomically."""
    from core.credential_store import file_lock, safe_path
    with file_lock(CONFIG_FILE):
        safe_path(CONFIG_FILE)
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("Configuration must be a JSON object")
        data.update(values)
        ensure_config_dir()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CONFIG_DIR,
                                             prefix=".vision-consent-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, indent=4)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, CONFIG_FILE)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def validate_live_sharing_settings(value: dict) -> dict:
    """Return complete, bounded settings; reject unknown fields and coerced types."""
    if not isinstance(value, dict):
        raise TypeError("Live sharing settings must be an object")
    if set(value) - set(LIVE_SHARING_DEFAULTS):
        raise ValueError("Unknown live sharing setting")
    settings = {**LIVE_SHARING_DEFAULTS, **value}
    interval = settings["interval"]
    if type(interval) not in (int, float) or not 1 <= interval <= 30:
        raise ValueError("Live sharing interval must be between 1 and 30 seconds")
    settings["interval"] = float(interval)
    for key, low, high in (("max_per_minute", 1, 60), ("analysis_per_minute", 1, 20),
                           ("max_width", 320, 1920), ("max_height", 240, 1080),
                           ("monitor", 0, 64), ("session_minutes", 1, 120)):
        if type(settings[key]) is not int or not low <= settings[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    if type(settings["change_only"]) is not bool:
        raise TypeError("Change detection must be a boolean")
    return settings


def get_live_sharing_settings() -> dict:
    data = load_api_keys()
    try:
        return validate_live_sharing_settings(data.get("live_screen_sharing", {}))
    except (AttributeError, TypeError, ValueError):
        return dict(LIVE_SHARING_DEFAULTS)


def save_live_sharing_settings(settings: dict) -> None:
    _save_consent_fields({"live_screen_sharing": validate_live_sharing_settings(settings)})


def get_live_sharing_startup() -> bool:
    data = load_api_keys()
    return isinstance(data, dict) and data.get("live_screen_sharing_startup") is True


def save_live_sharing_startup(enabled: bool) -> None:
    if type(enabled) is not bool:
        raise TypeError("Live sharing startup permission must be a boolean")
    _save_consent_fields({"live_screen_sharing_startup": enabled})


def get_gemini_key() -> str | None:
    from core.provider_config import get_provider_store
    key = get_provider_store().gemini_credential()
    if key:
        return key
    # Existing installations continue to work before explicit safe migration.
    data = load_api_keys()
    legacy = data.get("gemini_api_key") if isinstance(data, dict) else None
    return legacy if isinstance(legacy, str) and legacy else None

def is_configured() -> bool:
    from core.provider_config import get_provider_store
    store = get_provider_store()
    if store.has_configuration():
        return True
    if store.list_profiles():
        return False
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    """Return the configured assistant name, or 'JARVIS' if not set."""
    return load_api_keys().get("assistant_name", "JARVIS") or "JARVIS"


def get_user_name() -> str:
    """Return the configured user name for addressing."""
    return load_api_keys().get("user_name", "")


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    """Persist assistant name and user name to config."""
    _save_consent_fields({"assistant_name": assistant_name.strip() or "JARVIS", "user_name": user_name.strip()})


# ── Assistant voice ──────────────────────────────────────────────────────────
# Gemini Live prebuilt voices. Names are proper nouns — identical in every
# language, so this list is safe to show verbatim in any locale.
AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
DEFAULT_VOICE    = "Charon"


def get_voice() -> str:
    """Return the configured Live voice, falling back to the default if unset
    or if the stored value is not a voice we recognise."""
    v = load_api_keys().get("voice_name", DEFAULT_VOICE) or DEFAULT_VOICE
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE


def save_voice(voice_name: str) -> None:
    """Persist the chosen Live voice. Unknown names collapse to the default so a
    bad value can never reach the API and break the session."""
    v = (voice_name or "").strip()
    _save_consent_fields({"voice_name": v if v in AVAILABLE_VOICES else DEFAULT_VOICE})


def get_wake_word_enabled() -> bool:
    """Whether local wake-word gating is on (assistant sleeps until ' Arise')."""
    return load_api_keys().get("wake_word_enabled", False)


def save_wake_word_enabled(enabled: bool) -> None:
    _save_consent_fields({"wake_word_enabled": bool(enabled)})


def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> None:
    _save_consent_fields({"morning_brief_enabled": enabled})


# ── Audio devices ────────────────────────────────────────────────────────────
# Stored as device NAMES, not sounddevice indices. Indices shift every time a
# USB device is plugged in or removed, so a saved index silently starts pointing
# at a different microphone. The empty string means "system default", which is
# both the factory setting and what an unresolvable saved device falls back to —
# so unplugging a headset degrades to the built-in speakers instead of crashing.

def _patch_config(**fields) -> None:
    """Atomically update settings while preserving malformed files for recovery."""
    _save_consent_fields(fields)


def get_input_device() -> str:
    """Microphone device name, or '' for the system default."""
    return (load_api_keys().get("input_device", "") or "").strip()


def save_input_device(name: str) -> None:
    _patch_config(input_device=(name or "").strip())


def get_output_device() -> str:
    """Speaker device name, or '' for the system default."""
    return (load_api_keys().get("output_device", "") or "").strip()


def save_output_device(name: str) -> None:
    _patch_config(output_device=(name or "").strip())


def get_plugin_enabled(plugin_name: str) -> bool:
    """Plugins are enabled by default the moment they're discovered (opt-out model)."""
    return load_api_keys().get("plugins_enabled", {}).get(plugin_name, True)


# ── Per-plugin settings ("tokens" / connection details) ───────────────────────
# Generic store so a plugin can declare its own config fields (PLUGIN_SETTINGS)
# and the settings UI renders + persists them WITHOUT any core edit — keeping the
# drop-in model intact. Values live under plugin_config[<namespace>][<key>].
# A namespace defaults to the plugin name, but a suite of plugins (e.g. the
# printer control/watchdog/autoeject trio) can share ONE namespace.
def get_plugin_config(namespace: str) -> dict:
    """All stored values for a namespace (empty dict if none set yet)."""
    cfg = load_api_keys().get("plugin_config")
    val = cfg.get(namespace) if isinstance(cfg, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def get_plugin_setting(namespace: str, key: str, default=None):
    """A single value from a namespace, or `default` if unset."""
    return get_plugin_config(namespace).get(key, default)


def save_plugin_config(namespace: str, values: dict) -> None:
    """Merge `values` into a namespace's stored config (read-modify-write, like
    every other helper here). Only the provided keys are touched."""
    from core.credential_store import file_lock, safe_path
    with file_lock(CONFIG_FILE):
        safe_path(CONFIG_FILE)
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("Configuration must be a JSON object")
        pc = data.get("plugin_config")
        pc = dict(pc) if isinstance(pc, dict) else {}
        current = pc.get(namespace)
        current = dict(current) if isinstance(current, dict) else {}
        current.update(values)
        pc[namespace] = current
        _save_consent_fields({"plugin_config": pc})


def save_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    from core.credential_store import file_lock, safe_path
    with file_lock(CONFIG_FILE):
        safe_path(CONFIG_FILE)
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("Configuration must be a JSON object")
        plugins_cfg = data.get("plugins_enabled")
        plugins_cfg = dict(plugins_cfg) if isinstance(plugins_cfg, dict) else {}
        plugins_cfg[plugin_name] = enabled
        _save_consent_fields({"plugins_enabled": plugins_cfg})
