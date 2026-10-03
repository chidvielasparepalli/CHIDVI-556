"""Discover standalone persona modules in this folder.

Each persona file exports a PROFILE dictionary. Files are loaded dynamically,
so adding a new persona does not require editing a central registry.
"""
from copy import deepcopy
import importlib.util
from pathlib import Path
import re

_PERSONA_DIR = Path(__file__).resolve().parent
_RESERVED = {"__init__", "manager", "profiles", "loader"}


def _valid_id(value):
    return bool(re.fullmatch(r"[a-z][a-z0-9_]*", value))


def discover_personalities():
    # Keep legacy built-ins available while standalone modules are migrated.
    from .profiles import PERSONALITIES as legacy
    found = deepcopy(legacy)

    for path in sorted(_PERSONA_DIR.glob("*.py")):
        if path.stem.startswith("_") or path.stem in _RESERVED:
            continue
        if not _valid_id(path.stem):
            continue
        spec = importlib.util.spec_from_file_location(
            f"_chidvi_persona_{path.stem}", path
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        profile = getattr(module, "PROFILE", None)
        if not isinstance(profile, dict):
            continue
        required = {"name", "description", "interaction", "voice", "theme"}
        if not required.issubset(profile):
            raise ValueError(f"Invalid persona profile in {path.name}")
        found[path.stem] = deepcopy(profile)
    return found
