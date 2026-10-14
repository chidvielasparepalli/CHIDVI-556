"""AI tool interface for the authorized Android ADB transport in core.phone_control."""

import os
import shlex

from core import phone_control as transport
from memory.config_manager import get_plugin_setting

_PLUGIN_NAME = "adb_control"

PLUGIN = {
    "name": _PLUGIN_NAME,
    "description": (
        "Control a connected Android phone over ADB (Android Debug Bridge): "
        "tap at screen coordinates, swipe, type text, press hardware keys, or "
        "capture the screen to a PNG. Requires authorized USB or paired Wireless debugging on the "
        "device and the adb binary on this computer. Use for phone actions; "
        "NOT for sending WhatsApp/Telegram messages (that is send_message)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("What to do: 'devices' (list states), 'pair' (local interactive setup), 'connect'/'disconnect' (endpoint IP:port), "
                                "'open' (launch an app by its package name in "
                                "'value'; com.whatsapp=WhatsApp, com.android.chrome=Chrome, "
                                "com.instagram.android=Instagram), 'tap' (x y), "
                                "'swipe' (x1 y1 x2 y2 duration_ms), 'text' (string "
                                "in 'value'), 'keyevent' (event id, e.g. 3=home "
                                "4=back 26=power 66=enter 67=backspace, 24/25=volume), "
                                "or 'screencap' (save screen PNG to the path in 'value')."),
            },
            "serial": {"type": "STRING", "description": "Target serial from devices; required when several phones are connected."},
            "endpoint": {"type": "STRING", "description": "PHONE_IP:PORT for pair/connect/disconnect; never a pairing code."},
            "x": {"type": "NUMBER", "description": "First/only x coordinate (tap = tap point)."},
            "y": {"type": "NUMBER", "description": "First/only y coordinate."},
            "x2": {"type": "NUMBER", "description": "Swipe end x."},
            "y2": {"type": "NUMBER", "description": "Swipe end y."},
            "duration": {"type": "NUMBER", "description": "Swipe duration in ms (default 300)."},
            "keyevent": {"type": "NUMBER", "description": "Hardware key event id (action 'keyevent')."},
            "value": {"type": "STRING", "description": "Text to type (action 'text') or output PNG path (action 'screencap')."},
        },
        "required": ["action"],
    },
}


PLUGIN_SETTINGS = {
    "namespace": _PLUGIN_NAME, "title": "Android ADB Control",
    "fields": [
        {"key": "adb_path", "label": "ADB executable (blank = discover SDK)", "type": "text"},
        {"key": "serial", "label": "Selected device serial (from devices)", "type": "text"},
    ],
}


# Preserve diagnostic helpers while keeping connection management outside the AI interface.
_adb = transport.adb_run
_first_device = transport.adb_device


def execute(parameters: dict) -> dict:
    try:
        params = transport.validate_action(parameters, {"devices", "pair", "connect", "disconnect", "open",
                                                        "tap", "swipe", "text", "keyevent", "screencap", "back", "home"})
        if any(key in params for key in ("code", "pairing_code", "password", "token")):
            raise ValueError("Enter pairing codes only in the local ADB terminal prompt, never in an agent command.")
        action = params["action"]
        if action == "devices":
            devices = transport.adb_devices()
            ready = sum(device["state"] == "device" for device in devices)
            status = "; ".join(f"{device['serial']}: {device['state']}" for device in devices)
            return {"ok": True, "message": f"{ready} authorized device(s). " + (status or "No Android devices detected."),
                    "data": {"devices": devices, "ready_count": ready}}
        if action == "pair":
            address = transport.endpoint(params.get("endpoint"))
            executable = transport.adb_executable().replace("'", "''")
            return {"ok": True, "status": "needs_user_input", "data": {"paired": False},
                    "message": "On Android 11+, open Developer options > Wireless debugging > Pair using pairing code. "
                               f"In local PowerShell run: & '{executable}' pair '{address}' . "
                               "Enter the code only at that terminal prompt. Then connect using the separate connection port."}
        if action in {"connect", "disconnect"}:
            address = (transport.adb_connect if action == "connect" else transport.adb_disconnect)(params.get("endpoint"))
            return {"ok": True, "message": f"Phone {action}ed: {address}.", "data": {"endpoint": address}}
        if action == "text":
            value = params["value"]
            # shortcut: Android input text is ASCII-only; use a phone IME for Unicode or literal percent-s.
            if not value.isascii() or "%s" in value or any(ord(char) < 32 for char in value):
                raise ValueError("ADB text supports printable ASCII without literal %s. Use the phone keyboard for other text.")
        serial = params.get("serial") or get_plugin_setting(_PLUGIN_NAME, "serial", "") or os.environ.get("ANDROID_SERIAL", "")
        device = transport.adb_device(serial)
        if action == "screencap":
            data = transport.adb_run("-s", device, "exec-out", "screencap", "-p", binary=True)
            path = transport.save_png(data, params["value"])
            return {"ok": True, "message": f"Screen saved to {path}.", "data": {"path": path}}
        if action == "open":
            command = ["monkey", "-p", params["value"], "-c", "android.intent.category.LAUNCHER", "1"]
        elif action == "text":
            command = ["input", "text", params["value"].replace(" ", "%s")]
        elif action == "tap":
            command = ["input", "tap", str(params["x"]), str(params["y"])]
        elif action == "swipe":
            command = ["input", "swipe", *(str(params[key]) for key in ("x", "y", "x2", "y2", "duration"))]
        else:
            code = {"back": 4, "home": 3}.get(action, params.get("keyevent"))
            command = ["input", "keyevent", str(code)]
        # adb shell joins arguments on the phone: quote for its POSIX shell, not Windows.
        transport.adb_run("-s", device, "shell", shlex.join(command))
        return {"ok": True, "message": f"{action.capitalize()} command completed on phone. UI effect has not been verified."}
    except (ValueError, RuntimeError, OSError) as error:
        return {"ok": False, "error": str(error)}
    except Exception:
        return {"ok": False, "error": "Unexpected ADB failure. Check devices and connection settings."}


def run(parameters: dict, player=None, session_memory=None) -> str:
    result = execute(parameters)
    return result["message"] if result["ok"] else f"Android control failed: {result['error']}"


def _test_connection(_values=None):
    result = execute({"action": "devices"})
    ready = result.get("data", {}).get("ready_count", 0)
    return result["ok"] and ready > 0, result.get("message", result.get("error", "Connection failed."))


PLUGIN_SETTINGS["action"] = {"label": "CHECK DEVICES", "run": _test_connection}


if __name__ == "__main__":
    import unittest
    suite = unittest.defaultTestLoader.discover("tests", pattern="test_phone_control.py")
    raise SystemExit(not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful())
