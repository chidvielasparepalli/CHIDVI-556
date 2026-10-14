import base64
import io
import json
import os
import shlex
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from core import phone_control as phone
from core.task_control import TaskStore
from plugins import adb_control as adb
from plugins import wireless_control as wireless


def png():
    output = io.BytesIO()
    Image.new("RGB", (2, 3), "black").save(output, "PNG")
    return output.getvalue()


class PhoneValidationTests(unittest.TestCase):
    def test_invalid_requests_never_touch_transport(self):
        invalid = [None, [], {}, {"action": 1}, {"action": "unknown"},
                   {"action": "tap", "x": -1, "y": 2}, {"action": "tap", "x": True, "y": 2},
                   {"action": "tap", "x": float("nan"), "y": 2}, {"action": "tap", "x": 1.5, "y": 2},
                   {"action": "swipe", "x": 0, "y": 0}, {"action": "keyevent", "keyevent": 401},
                   {"action": "open", "value": "com.app;reboot"}, {"action": "text", "value": ""}]
        for module in (adb, wireless):
            with patch.object(phone, "adb_run") as run, patch.object(wireless, "_request") as request:
                for command in invalid:
                    with self.subTest(module=module.__name__, command=command):
                        self.assertFalse(module.execute(command)["ok"])
                run.assert_not_called()
                request.assert_not_called()

    def test_endpoints_and_bounds(self):
        self.assertEqual(phone.endpoint("192.168.1.2:37000"), "192.168.1.2:37000")
        self.assertEqual(phone.endpoint("[fd00::2]:37000"), "[fd00::2]:37000")
        self.assertEqual(phone.local_ip("100.100.1.2"), "100.100.1.2")
        for invalid in ("https://192.168.1.2:123", "192.168.1.2:0", "192.168.1.2:70000", "8.8.8.8:123", "host:123", "0.0.0.0:123"):
            with self.subTest(endpoint=invalid), self.assertRaises(ValueError):
                phone.endpoint(invalid)

    def test_screenshot_does_not_overwrite_or_write_invalid_data(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "phone.png"
            with self.assertRaises(ValueError):
                phone.save_png(b"not a png", target)
            self.assertFalse(target.exists())
            self.assertEqual(phone.save_png(png(), target), str(target.resolve()))
            before = target.read_bytes()
            with self.assertRaises(FileExistsError):
                phone.save_png(png(), target)
            self.assertEqual(target.read_bytes(), before)


class ADBTransportTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.object(phone, "get_plugin_setting", return_value="")
        self.config.start()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.config.stop)
        self.addCleanup(self.environment.stop)

    def test_sdk_discovery_when_not_on_path(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "Android" / "Sdk" / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.dict(os.environ, {"LOCALAPPDATA": folder}), patch.object(phone.shutil, "which", return_value=None):
                self.assertEqual(phone.adb_executable(), str(executable.resolve()))

    def test_cancelled_origin_cannot_launch_adb_after_active_task_switch(self):
        store = TaskStore()
        first = store.submit("old phone action", "old")
        second = store.submit("new phone action", "new")
        store.claim()
        with store.context(first["id"]):
            store.cancel(first["id"])
            store.release(first["id"])
            store.claim()
            self.assertEqual(store.active_id, second["id"])
            with patch.object(phone, "adb_executable", return_value="adb"), patch.object(phone.subprocess, "run") as process:
                with self.assertRaisesRegex(RuntimeError, "Originating task"):
                    phone.adb_run("-s", "PHONE", "shell", "input tap 1 2")
                process.assert_not_called()
                process.return_value = subprocess.CompletedProcess([], 0, "ready", "")
                with store.context(second["id"]):
                    self.assertEqual(phone.adb_run("devices"), "ready")
                process.assert_called_once()

    def test_cancellation_during_adb_discovery_is_checked_before_launch(self):
        store = TaskStore()
        task = store.submit("phone action", "one")
        store.claim()
        def resolve():
            store.cancel(task["id"])
            return "adb"
        with store.context(task["id"]), patch.object(phone, "adb_executable", side_effect=resolve), patch.object(phone.subprocess, "run") as process:
            with self.assertRaisesRegex(RuntimeError, "Originating task"):
                phone.adb_run("devices")
            process.assert_not_called()

    def test_missing_and_configured_executables(self):
        with patch.object(phone.shutil, "which", return_value=None), self.assertRaisesRegex(RuntimeError, "ADB not found"):
            phone.adb_executable()
        with patch.object(phone, "get_plugin_setting", return_value="missing-adb-file"), self.assertRaisesRegex(RuntimeError, "Configured ADB"):
            phone.adb_executable()
        with tempfile.NamedTemporaryFile() as executable, patch.object(phone, "get_plugin_setting", return_value=executable.name):
            self.assertEqual(phone.adb_executable(), str(Path(executable.name).resolve()))

    def test_timeout_and_nonzero_errors_hide_command_text(self):
        with patch.object(phone, "adb_executable", return_value="adb"), patch.object(phone.subprocess, "run") as process:
            process.side_effect = subprocess.TimeoutExpired("secret-code", 30)
            with self.assertRaisesRegex(RuntimeError, "timed out") as error:
                phone.adb_run("devices")
            self.assertNotIn("secret-code", str(error.exception))
            process.side_effect = None
            process.return_value = subprocess.CompletedProcess([], 1, "", "echo TOP-SECRET failed")
            with self.assertRaises(RuntimeError) as error:
                phone.adb_run("shell", "TOP-SECRET")
            self.assertNotIn("TOP-SECRET", str(error.exception))
            self.assertEqual(process.call_args.kwargs["timeout"], 30)

    def test_devices_and_authorized_selection(self):
        listing = "List of devices attached\nPHONE1\tunauthorized\nPHONE2\toffline\nPHONE3\tdevice product:x\n"
        with patch.object(phone, "adb_run", return_value=listing):
            self.assertEqual(phone.adb_device(), "PHONE3")
            self.assertEqual(adb.execute({"action": "devices"})["data"]["ready_count"], 1)
            for serial, error in (("PHONE1", "unauthorized"), ("PHONE2", "offline"), ("MISSING", "not connected")):
                with self.subTest(serial=serial), self.assertRaisesRegex(RuntimeError, error):
                    phone.adb_device(serial)

    def test_empty_and_ambiguous_selection(self):
        for listing, expected in (("List of devices attached\n", "No authorized"),
                                  ("List of devices attached\nA\tdevice\nB\tdevice\n", "Multiple authorized")):
            with patch.object(phone, "adb_run", return_value=listing), self.assertRaisesRegex(RuntimeError, expected):
                phone.adb_device()
        with patch.object(phone, "adb_run", return_value="List of devices attached\nA\tdevice\nB\tdevice\n"):
            self.assertEqual(phone.adb_device("B"), "B")

    def test_connect_success_failure_and_disconnect(self):
        address = "192.168.1.2:37000"
        with patch.object(phone, "adb_run", side_effect=[f"connected to {address}", f"List of devices attached\n{address}\tdevice\n"]):
            self.assertEqual(phone.adb_connect(address), address)
        with patch.object(phone, "adb_run", return_value="failed to connect"), self.assertRaisesRegex(RuntimeError, "could not connect"):
            phone.adb_connect(address)
        with patch.object(phone, "adb_run", return_value=f"disconnected {address}") as run:
            self.assertEqual(phone.adb_disconnect(address), address)
            self.assertEqual(run.call_args.args, ("disconnect", address))

    def test_connect_response_without_authorized_device_fails(self):
        with patch.object(phone, "adb_run", side_effect=["connected to 192.168.1.2:1234", "List of devices attached\n"]):
            self.assertFalse(adb.execute({"action": "connect", "endpoint": "192.168.1.2:1234"})["ok"])


class ADBPluginTests(unittest.TestCase):
    def test_pairing_is_local_and_never_executes_or_accepts_code(self):
        with patch.object(phone, "adb_executable", return_value="C:/SDK/adb.exe"), patch.object(phone, "adb_run") as run:
            result = adb.execute({"action": "pair", "endpoint": "192.168.1.2:37000"})
            self.assertEqual(result["status"], "needs_user_input")
            self.assertFalse(result["data"]["paired"])
            result = adb.execute({"action": "pair", "endpoint": "192.168.1.2:37000", "pairing_code": "123456"})
            self.assertFalse(result["ok"])
            self.assertNotIn("123456", str(result))
            run.assert_not_called()

    def test_text_is_quoted_for_phone_shell_without_echoing(self):
        value = "hello; echo 'not a command' & $(reboot)"
        with patch.object(phone, "adb_device", return_value="PHONE"), patch.object(phone, "adb_run") as run:
            result = adb.execute({"action": "text", "value": value, "serial": "PHONE"})
            self.assertTrue(result["ok"])
            self.assertEqual(run.call_args.args[:3], ("-s", "PHONE", "shell"))
            self.assertEqual(shlex.split(run.call_args.args[3]), ["input", "text", value.replace(" ", "%s")])
            self.assertNotIn(value, str(result))

    def test_unrepresentable_text_rejected_before_device_access(self):
        with patch.object(phone, "adb_device") as device:
            for value in ("नमस्ते", "literal%s", "new\nline"):
                self.assertFalse(adb.execute({"action": "text", "value": value})["ok"])
            device.assert_not_called()

    def test_all_actions_use_selected_phone(self):
        cases = [{"action": "open", "value": "com.android.chrome"}, {"action": "tap", "x": 1, "y": 2},
                 {"action": "swipe", "x": 0, "y": 0, "x2": 5, "y2": 6},
                 {"action": "keyevent", "keyevent": 3}, {"action": "back"}, {"action": "home"}]
        with patch.object(phone, "adb_device", return_value="PHONE"), patch.object(phone, "adb_run") as run:
            for command in cases:
                with self.subTest(command=command):
                    self.assertTrue(adb.execute(dict(command, serial="PHONE"))["ok"])
                    self.assertEqual(run.call_args.args[:3], ("-s", "PHONE", "shell"))

    def test_screenshot_explicit_and_binary_without_phone_tempfile(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(phone, "adb_device", return_value="PHONE"), patch.object(phone, "adb_run", return_value=png()) as run:
            target = Path(folder) / "screen.png"
            result = adb.execute({"action": "screencap", "value": str(target)})
            self.assertTrue(result["ok"])
            run.assert_called_once_with("-s", "PHONE", "exec-out", "screencap", "-p", binary=True)
            self.assertFalse(adb.execute({"action": "screencap", "value": str(target)})["ok"])
            self.assertEqual(run.call_count, 1)


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"phone_ip": "192.168.1.2", "phone_token": "TEST-TOKEN", "port": 8795}
        self.config = patch.object(wireless, "get_plugin_setting", side_effect=lambda ns, key, default=None: self.settings.get(key, default))
        self.config.start()
        self.addCleanup(self.config.stop)

    def response(self, value):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(value).encode()
        opener = MagicMock()
        opener.open.return_value = response
        return opener

    def test_url_authentication_timeout_and_redirect_policy(self):
        opener = self.response({"ok": True})
        with patch.object(wireless.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(wireless._get("/ping"), {"ok": True})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://192.168.1.2:8795/ping")
            self.assertEqual(request.get_header("Authorization"), "Bearer TEST-TOKEN")
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
            self.assertIsInstance(build.call_args.args[1], wireless._NoRedirect)
            self.assertIsNone(build.call_args.args[1].redirect_request(None, None, 302, "", {}, "http://attacker"))
        self.settings["use_https"] = True
        self.assertTrue(wireless._phone_url("ping").startswith("https://"))

    def test_cancelled_origin_cannot_send_http_after_active_task_switch(self):
        store = TaskStore()
        first = store.submit("old phone action", "old")
        second = store.submit("new phone action", "new")
        store.claim()
        opener = self.response({"ok": True})
        with store.context(first["id"]), patch.object(wireless.urllib.request, "build_opener", return_value=opener):
            store.cancel(first["id"])
            store.release(first["id"])
            store.claim()
            self.assertEqual(store.active_id, second["id"])
            result = wireless.execute({"action": "tap", "x": 1, "y": 2})
            self.assertFalse(result["ok"])
            self.assertIn("Originating task", result["error"])
            opener.open.assert_not_called()
            with store.context(second["id"]):
                self.assertTrue(wireless.execute({"action": "tap", "x": 1, "y": 2})["ok"])
            opener.open.assert_called_once()

    def test_cancellation_during_http_preparation_is_checked_before_open(self):
        store = TaskStore()
        task = store.submit("phone action", "one")
        store.claim()
        opener = self.response({"ok": True})
        def prepare(*_handlers):
            store.cancel(task["id"])
            return opener
        with store.context(task["id"]), patch.object(wireless.urllib.request, "build_opener", side_effect=prepare):
            with self.assertRaisesRegex(RuntimeError, "Originating task"):
                wireless._get("/ping")
            opener.open.assert_not_called()

    def test_missing_config_token_and_invalid_url_fail_without_request(self):
        for key, value in (("phone_ip", ""), ("phone_ip", "http://host"), ("port", 0), ("phone_token", ""), ("phone_token", "a\nb")):
            with self.subTest(key=key, value=value), patch.object(wireless.urllib.request, "build_opener") as build:
                old = self.settings[key]
                self.settings[key] = value
                self.assertFalse(wireless.execute({"action": "devices"})["ok"])
                build.assert_not_called()
                self.settings[key] = old

    def test_network_auth_json_and_server_errors_are_not_success(self):
        for code in (401, 403, 302, 500):
            opener = self.response({"ok": True})
            opener.open.side_effect = urllib.error.HTTPError("http://phone", code, "TEST-TOKEN", {}, None)
            with patch.object(wireless.urllib.request, "build_opener", return_value=opener):
                result = wireless.execute({"action": "devices"})
                self.assertFalse(result["ok"])
                self.assertNotIn("TEST-TOKEN", str(result))
        for value in ({"ok": False, "error": "secret"}, {}, [], {"ok": 1}):
            with patch.object(wireless.urllib.request, "build_opener", return_value=self.response(value)):
                self.assertFalse(wireless.execute({"action": "tap", "x": 1, "y": 2})["ok"])
        opener = self.response({})
        opener.open.side_effect = TimeoutError()
        with patch.object(wireless.urllib.request, "build_opener", return_value=opener):
            self.assertIn("not reachable", wireless.execute({"action": "devices"})["error"])
        opener = self.response({})
        opener.open.return_value.read.return_value = b"<html>invalid</html>"
        with patch.object(wireless.urllib.request, "build_opener", return_value=opener):
            self.assertIn("invalid JSON", wireless.execute({"action": "devices"})["error"])

    def test_response_size_limit(self):
        opener = self.response({})
        opener.open.return_value.read.return_value = b"x" * (17 * 1024 * 1024 + 1)
        with patch.object(wireless.urllib.request, "build_opener", return_value=opener):
            self.assertIn("limit", wireless.execute({"action": "devices"})["error"])

    def test_all_dispatches_and_no_typed_secrets_returned(self):
        cases = [("open", {"value": "com.android.chrome"}, "/launch"), ("tap", {"x": 1, "y": 2}, "/tap"),
                 ("swipe", {"x": 0, "y": 0, "x2": 4, "y2": 5}, "/swipe"),
                 ("text", {"value": "private text"}, "/type"), ("keyevent", {"keyevent": 3}, "/keyevent"),
                 ("back", {}, "/back"), ("home", {}, "/home")]
        with patch.object(wireless, "_post", return_value={"ok": True}) as post:
            for action, values, endpoint in cases:
                result = wireless.execute(dict(values, action=action))
                self.assertTrue(result["ok"])
                self.assertEqual(post.call_args.args[0], endpoint)
                self.assertNotIn("private text", str(result))

    def test_notifications_hide_codes(self):
        with patch.object(wireless, "_post", return_value={"notifications": [{"text": "OTP 123456"}]}):
            result = wireless.execute({"action": "notifications"})
            self.assertEqual(result["data"]["count"], 1)
            self.assertNotIn("123456", str(result))

    def test_screenshot_success_invalid_encoding_and_existing_file(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(wireless, "_post") as post:
            target = Path(folder) / "phone.png"
            post.return_value = {"png": "not-base64!"}
            self.assertFalse(wireless.execute({"action": "screencap", "value": str(target)})["ok"])
            self.assertFalse(target.exists())
            post.return_value = {"png": base64.b64encode(png()).decode()}
            self.assertTrue(wireless.execute({"action": "screencap", "value": str(target)})["ok"])
            post.reset_mock()
            self.assertFalse(wireless.execute({"action": "screencap", "value": str(target)})["ok"])
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
