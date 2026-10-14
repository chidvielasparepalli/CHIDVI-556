"""
JARVIS wireless phone control plugin — control an Android device over WiFi
via the JARVIS Companion app. No USB cable or USB debugging required.

The companion app runs an HTTP server on the phone (default port 8795).
This plugin sends JSON commands over HTTP using only stdlib (urllib.request).

Both devices must be on the same Tailscale network (or same LAN).
Configure the phone's Tailscale IP and auth token in plugin settings.
"""

import base64
import json
import socket
import urllib.request
import urllib.error
from core.phone_control import MAX_IMAGE_BYTES, integer, local_ip, save_png, validate_action
from core.task_control import check_task_cancelled
from memory.config_manager import get_plugin_setting

_PLUGIN_NAME = "wireless_control"
_DEFAULT_PORT = 8795

PLUGIN = {  
    "name": _PLUGIN_NAME,
    "description": (
        "Control an Android phone wirelessly over WiFi: tap at screen "
        "coordinates, swipe, type text, press hardware keys, launch apps, "
        "or capture the screen to a PNG. Requires the JARVIS Companion app "
        "installed and running on the phone (no USB cable needed). Use for "
        "phone actions; NOT for sending WhatsApp/Telegram messages (that is "
        "send_message)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "What to do: 'devices' (check phone connection), "
                    "'open' (launch app by package name in 'value'; "
                    "com.whatsapp=WhatsApp, com.android.chrome=Chrome, "
                    "com.instagram.android=Instagram), 'tap' (x y), "
                    "'swipe' (x1 y1 x2 y2 duration_ms), 'text' (string "
                    "in 'value'), 'keyevent' (event id, e.g. 3=home "
                    "4=back 26=power 66=enter 67=backspace, 24/25=volume), "
                    "'screencap' (save screen PNG to path in 'value'), "
                    "'back', 'home', or 'notifications'."
                ),
            },
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
    "namespace": _PLUGIN_NAME,
    "title": "Wireless Phone Control",
    "fields": [
        {"key": "phone_ip", "label": "Phone IP Address (LAN or Tailscale)", "type": "text",
         "placeholder": "100.x.x.x or 192.168.1.x"},
        {"key": "phone_token", "label": "Auth Token", "type": "password",
         "placeholder": "shown on phone companion app"},
        {"key": "port", "label": "Port", "type": "number", "default": _DEFAULT_PORT},
        {"key": "use_https", "label": "HTTPS (requires trusted phone certificate)", "type": "toggle", "default": False},
    ],
}


def _phone_url(path: str) -> str:
    host = local_ip(get_plugin_setting(_PLUGIN_NAME, "phone_ip", ""))
    port = integer(get_plugin_setting(_PLUGIN_NAME, "port", _DEFAULT_PORT), "port", 1)
    if not isinstance(path, str) or path.lstrip("/") not in {
        "ping", "launch", "tap", "swipe", "type", "keyevent", "screenshot", "back", "home", "notifications"
    }:
        raise ValueError("Unknown Companion endpoint.")
    scheme = "https" if get_plugin_setting(_PLUGIN_NAME, "use_https", False) is True else "http"
    return f"{scheme}://{host}:{port}/{path.lstrip('/')}"


def _phone_token() -> str:
    token = get_plugin_setting(_PLUGIN_NAME, "phone_token", "")
    if not isinstance(token, str) or not token.strip() or len(token) > 4096 or any(ord(c) < 32 for c in token):
        raise ValueError("Configure the Companion auth token in Plugin Settings; unauthenticated phone commands are disabled.")
    return token.strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward bearer credentials to a redirect destination.
        return None


def _request(path, body=None):
    url = _phone_url(path)
    token = _phone_token()
    data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
    if data is not None and len(data) > 65536:
        raise ValueError("Phone command exceeds 64 KiB.")
    request = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}"
    }, method="GET" if body is None else "POST")
    # Keep LAN credentials out of configured HTTP proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    check_task_cancelled()
    try:
        with opener.open(request, timeout=10 if body is None else 15) as response:
            raw = response.read(17 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise RuntimeError("Companion rejected authentication. Check the phone's current token.") from None
        if 300 <= error.code < 400:
            raise RuntimeError("Companion redirect refused. Configure the actual phone server address.") from None
        raise RuntimeError(f"Companion HTTP error {error.code}. Check the phone app and its permissions.") from None
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        raise RuntimeError("Phone not reachable. Check IP/port, same LAN or Tailscale, and that the Companion server is running.") from None
    if len(raw) > 17 * 1024 * 1024:
        raise ValueError("Companion response exceeds the 17 MiB limit.")
    try:
        result = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Companion returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise ValueError("Companion response must be a JSON object.")
    if result.get("ok") is False or result.get("error"):
        raise RuntimeError("Companion rejected the command. Check its accessibility, screen-capture and notification permissions.")
    if path.lstrip("/") not in {"screenshot", "notifications"} and result.get("ok") is not True:
        raise RuntimeError("Companion did not acknowledge the command; no success assumed.")
    return result


def _post(path: str, body: dict | None = None) -> dict:
    """Preserved transport entry point used by game_control."""
    if body is not None and not isinstance(body, dict):
        raise ValueError("Phone command body must be an object.")
    return _request(path, body or {})


def _get(path: str) -> dict:
    """Preserved transport entry point used by game_control."""
    return _request(path)


def execute(parameters: dict) -> dict:
    """Structured result; never return typed text or notification secrets."""
    try:
        params = validate_action(parameters, {"devices", "open", "tap", "swipe", "text", "keyevent",
                                              "screencap", "back", "home", "notifications"})
        action = params["action"]
        if action == "devices":
            info = _get("/ping")
            if info.get("ok") is not True:
                raise RuntimeError("Companion did not confirm an authenticated connection.")
            return {"ok": True, "message": "Phone Companion connected.",
                    "data": {"screen_capture": bool(info.get("screen"))}}
        if action == "screencap":
            response = _post("/screenshot", {"quality": 80})
            encoded = response.get("png")
            if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                raise ValueError("Phone returned an empty or oversized screenshot.")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                raise ValueError("Phone returned invalid screenshot encoding.") from None
            path = save_png(data, params["value"])
            return {"ok": True, "message": f"Screen saved to {path}.", "data": {"path": path}}
        if action == "notifications":
            response = _post("/notifications")
            notifications = response.get("notifications")
            if not isinstance(notifications, list):
                raise ValueError("Companion returned invalid notification data.")
            # Notifications can contain one-time codes; never send their contents to the agent.
            return {"ok": True, "message": f"{len(notifications)} notification(s). Contents are hidden for privacy.",
                    "data": {"count": len(notifications)}}
        calls = {
            "open": ("/launch", {"package": params.get("value")}),
            "tap": ("/tap", {"x": params.get("x"), "y": params.get("y")}),
            "swipe": ("/swipe", {"x1": params.get("x"), "y1": params.get("y"),
                                   "x2": params.get("x2"), "y2": params.get("y2"), "duration": params.get("duration")}),
            "text": ("/type", {"text": params.get("value")}),
            "keyevent": ("/keyevent", {"keycode": params.get("keyevent")}),
            "back": ("/back", {}), "home": ("/home", {}),
        }
        path, body = calls[action]
        response = _post(path, body)
        if response.get("ok") is not True:
            raise RuntimeError("Companion did not acknowledge the command; no success assumed.")
        return {"ok": True, "message": f"{action.capitalize()} command accepted by phone. UI effect has not been verified."}
    except (ValueError, RuntimeError, OSError) as error:
        return {"ok": False, "error": str(error)}
    except Exception:
        return {"ok": False, "error": "Unexpected Companion failure. Check configuration and phone permissions."}


def run(parameters: dict, player=None, session_memory=None) -> str:
    result = execute(parameters)
    return result["message"] if result["ok"] else f"Wireless phone control failed: {result['error']}"


def _test_connection(_values=None):
    result = execute({"action": "devices"})
    return result["ok"], result.get("message", result.get("error", "Connection failed."))


PLUGIN_SETTINGS["action"] = {"label": "TEST CONNECTION", "run": _test_connection}


if __name__ == "__main__":
    import unittest
    suite = unittest.defaultTestLoader.discover("tests", pattern="test_phone_control.py")
    raise SystemExit(not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful())
