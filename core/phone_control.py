"""Validation shared by the existing Android control transports."""

import ipaddress
import io
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

from core.task_control import check_task_cancelled
from memory.config_manager import get_plugin_setting


MAX_IMAGE_BYTES = 12 * 1024 * 1024


def adb_executable():
    configured = get_plugin_setting("adb_control", "adb_path", "") or os.environ.get("ADB_PATH", "")
    if configured:
        path = Path(os.path.expandvars(str(configured))).expanduser()
        if not path.is_file():
            raise RuntimeError("Configured ADB executable does not exist. Check adb_path or ADB_PATH.")
        return str(path.resolve())
    installed = shutil.which("adb")
    if installed:
        return installed
    roots = [os.environ.get("ANDROID_SDK_ROOT"), os.environ.get("ANDROID_HOME")]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(str(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk"))
    for root in filter(None, roots):
        path = Path(root).expanduser() / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
        if path.is_file():
            return str(path.resolve())
    raise RuntimeError("ADB not found. Install Android SDK Platform Tools or configure adb_path in Plugin Settings.")


def adb_run(*args, binary=False, timeout=30):
    """Never invoke a host shell or include commands/text/secrets in failures."""
    try:
        executable = adb_executable()
        check_task_cancelled()
        process = subprocess.run([executable, *args], capture_output=True,
                                 text=not binary, timeout=timeout, check=False,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError("ADB command timed out; check the phone connection and retry.") from None
    except OSError:
        raise RuntimeError("Could not start ADB. Check the configured executable and permissions.") from None
    if process.returncode:
        error = process.stderr.decode(errors="replace") if isinstance(process.stderr, bytes) else process.stderr
        if "unauthorized" in error.lower():
            raise RuntimeError("Phone is unauthorized. Unlock it and accept the USB debugging prompt, or pair wireless debugging.")
        if "offline" in error.lower():
            raise RuntimeError("Phone is offline. Reconnect it, then check devices.")
        raise RuntimeError("ADB command failed. Check devices, authorization and phone connectivity.")
    return process.stdout


def adb_devices():
    devices = []
    for line in adb_run("devices", "-l", timeout=10).splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) >= 2 and parts[1] in {"device", "offline", "unauthorized", "recovery", "sideload", "bootloader", "no"}:
            devices.append({"serial": parts[0], "state": parts[1]})
    return devices


def adb_device(serial=""):
    devices = adb_devices()
    if serial:
        if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9_.:\[\]-]{1,200}", serial):
            raise ValueError("Invalid device serial. Select one returned by devices.")
        device = next((item for item in devices if item["serial"] == serial), None)
        if device is None:
            raise RuntimeError("Selected phone is not connected. Run devices and select a listed serial.")
        if device["state"] != "device":
            raise RuntimeError(f"Selected phone is {device['state']}. Unlock/authorize or reconnect it before controlling it.")
        return serial
    ready = [item for item in devices if item["state"] == "device"]
    if len(ready) > 1:
        raise RuntimeError("Multiple authorized Android devices are connected. Supply 'serial' or select one in Plugin Settings.")
    if not ready:
        states = ", ".join(sorted({item["state"] for item in devices})) or "no devices"
        raise RuntimeError(f"No authorized Android device ({states}). Unlock/authorize USB debugging or pair Wireless debugging.")
    return ready[0]["serial"]


def adb_connect(address):
    address = endpoint(address)
    result = adb_run("connect", address, timeout=15).strip().lower()
    if not (result.startswith("connected to ") or result.startswith("already connected to ")):
        raise RuntimeError("ADB could not connect. Use the connection port (not pairing port), enable Wireless debugging, and check the shared network.")
    if not any(item["serial"] == address and item["state"] == "device" for item in adb_devices()):
        raise RuntimeError("ADB connection responded, but the phone is not authorized/ready. Check devices and pair if needed.")
    return address


def adb_disconnect(address):
    address = endpoint(address)
    result = adb_run("disconnect", address, timeout=10).strip().lower()
    if not result.startswith("disconnected "):
        raise RuntimeError("ADB did not disconnect that endpoint. Check devices for its current address.")
    return address


def integer(value, name, minimum=0, maximum=65535):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    if isinstance(value, str) and not re.fullmatch(r"\d+", value):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    return number


def local_ip(value):
    """Accept literal LAN, loopback or Tailscale addresses, never arbitrary URLs."""
    if not isinstance(value, str):
        raise ValueError("Configure a literal phone IP address on your LAN or Tailscale.")
    try:
        address = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        raise ValueError("Configure a literal phone IP address on your LAN or Tailscale.") from None
    tailscale = address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10")
    if address.is_multicast or address.is_unspecified or not (address.is_private or tailscale):
        raise ValueError("Use a phone IP on your LAN or Tailscale; public addresses are disabled.")
    return f"[{address}]" if address.version == 6 else str(address)


def endpoint(value):
    if not isinstance(value, str) or ":" not in value:
        raise ValueError("endpoint must be PHONE_IP:PORT from Android Wireless debugging.")
    host, port = value.rsplit(":", 1)
    return f"{local_ip(host)}:{integer(port, 'port', 1)}"


def validate_action(parameters, known):
    if not isinstance(parameters, dict):
        raise ValueError("Phone command must be an object.")
    action = parameters.get("action")
    if not isinstance(action, str) or not action.strip():
        raise ValueError("I need an 'action'.")
    action = action.lower().strip()
    if action not in known:
        raise ValueError("Unknown action. Use " + ", ".join(sorted(known)) + ".")
    if action in {"open", "text", "screencap"}:
        value = parameters.get("value")
        if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
            raise ValueError(f"'{action}' needs a nonempty 'value' (up to 4096 characters).")
        if action == "open" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", value):
            raise ValueError("'open' needs a valid Android app package in 'value'.")
        if action == "screencap":
            path = Path(value).expanduser()
            if path.suffix.lower() != ".png" or not path.parent.is_dir():
                raise ValueError("Choose a .png path in an existing directory.")
            if path.exists():
                raise ValueError("Screenshot destination already exists; choose a new .png path.")
    keys = ("x", "y", "x2", "y2") if action == "swipe" else ("x", "y") if action == "tap" else ()
    validated = dict(parameters, action=action)
    for key in keys:
        validated[key] = integer(parameters.get(key), key)
    if action == "swipe":
        validated["duration"] = integer(parameters.get("duration", 300), "duration", 1, 10000)
    if action == "keyevent":
        validated["keyevent"] = integer(parameters.get("keyevent"), "keyevent", 0, 400)
    return validated


def save_png(data, destination):
    """Verify before writing, and never replace an existing user file."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Phone screenshot is empty or exceeds 12 MiB.")
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as picture:
            if picture.format != "PNG" or picture.width * picture.height > 33_177_600:
                raise ValueError("Phone screenshot must be a PNG of at most 33 million pixels.")
            picture.verify()
    except (OSError, SyntaxError):
        raise ValueError("Phone returned an invalid PNG screenshot.") from None
    path = Path(destination).expanduser()
    with path.open("xb") as output:
        try:
            output.write(data)
        except Exception:
            output.close()
            path.unlink(missing_ok=True)
            raise
    return str(path.resolve())
