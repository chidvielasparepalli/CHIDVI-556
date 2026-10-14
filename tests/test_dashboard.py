"""Controller security and routing tests; no server, phone, firewall or LLM."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, parse_qs

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dashboard.server import DashboardServer, COOKIE

ORIGIN = "http://127.0.0.1:8000"


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"CHIDVI_CONTROLLER_HOST":"127.0.0.1", "CHIDVI_CONTROLLER_SESSION_TTL":"43200"})
        self.environment.start()
        self.tls = patch.object(DashboardServer, "_ssl_enabled", return_value=False)
        self.tls.start()
        self.server = DashboardServer()
        self.client = TestClient(self.server.app, base_url=ORIGIN, headers={"Origin":ORIGIN})
        self.client.__enter__()
        self.task_list = []
        self.submit = Mock(side_effect=self._submit)
        self.cancel = Mock(side_effect=self._cancel)
        self.server.set_task_callbacks(self.submit, self.cancel, lambda:self.task_list)
        self.server.set_status_callback(lambda:{"personality":"Wednesday", "state":"ready", "control_enabled":True})
        self.folder = tempfile.TemporaryDirectory()
        self.server._uploads_dir = Path(self.folder.name)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tls.stop()
        self.environment.stop()
        self.folder.cleanup()

    def _submit(self, text, request_id, device_id):
        task = {"id":f"task-{len(self.task_list)+1}", "text":text, "request_id":request_id,
                "source":device_id, "state":"queued"}
        self.task_list.append(task)
        return task

    def _cancel(self, task_id):
        task = next((task for task in self.task_list if task["id"] == task_id), None)
        if task is None: raise KeyError(task_id)
        task["state"] = "cancelled"
        return task

    def pair(self):
        result = self.client.post("/login", json={"token":self.server.new_key()})
        self.assertEqual(result.status_code, 200, result.text)
        session = self.client.get("/api/session").json()
        self.client.headers["X-CSRF-Token"] = session["csrf"]
        return session

    def test_pairing_high_entropy_fragment_one_time_and_cookie(self):
        key = self.server.new_key()
        self.assertEqual(len(key), 43)
        url = urlsplit(self.server.get_pairing_url(key))
        self.assertEqual(url.query, "")
        self.assertEqual(parse_qs(url.fragment)["pair"], [key])
        result = self.client.post("/login", json={"token":key})
        self.assertEqual(result.status_code, 200)
        cookie = result.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertNotIn("token", result.json())
        self.assertEqual(self.client.post("/login", json={"token":key}).status_code, 401)

    def test_existing_qr_widget_preserves_pairing_url(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import cv2
        from PyQt6.QtWidgets import QApplication
        from ui import RemoteKeyOverlay
        application = QApplication.instance() or QApplication([])
        key = self.server.new_key()
        url = self.server.get_pairing_url(key)
        widget = RemoteKeyOverlay(self.server.get_url(), key, auto_login_url=url)
        path = Path(self.folder.name) / "pairing.png"
        self.assertTrue(widget._qr_label.pixmap().save(str(path)))
        decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(cv2.imread(str(path)))
        self.assertEqual(decoded, url)
        widget._do_close()
        application.processEvents()

    def test_pair_expiry_refresh_and_cancellation(self):
        expired = self.server.new_key()
        self.server._pending_keys[expired] = 0
        self.assertEqual(self.client.post("/login", json={"token":expired}).status_code, 401)
        old = self.server.new_key()
        current = self.server.new_key()
        self.assertEqual(self.client.post("/login", json={"token":old}).status_code, 401)
        self.server.cancel_pairing()
        self.assertEqual(self.client.post("/login", json={"token":current}).status_code, 401)

    def test_authentication_and_csrf_required(self):
        self.assertEqual(self.client.get("/api/session").status_code, 401)
        self.assertEqual(self.client.post("/api/command", json={"text":"x", "request_id":"one"}).status_code, 401)
        self.pair()
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual(self.client.post("/api/command", json={"text":"x", "request_id":"one"}).status_code, 403)
        self.submit.assert_not_called()

    def test_origin_and_dns_rebinding_rejected(self):
        key = self.server.new_key()
        self.assertEqual(self.client.post("/login", json={"token":key}, headers={"Origin":"https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Host":"evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/session", headers={"Origin":"null"}).status_code, 403)
        self.assertIn(key, self.server._pending_keys)

    def test_json_validation_and_size_bound(self):
        self.pair()
        for body in ([], {"text":{},"request_id":"one"}, {"text":"x","request_id":[]}, {"text":"x"}, {"text":"x"*4001,"request_id":"one"}):
            self.assertEqual(self.client.post("/api/command", json=body).status_code, 400)
        self.assertEqual(self.client.post("/api/command", content=b"x"*20000, headers={"Content-Type":"application/json"}).status_code, 413)
        self.assertEqual(self.client.post("/api/command", content="{").status_code, 415)
        self.submit.assert_not_called()

    def test_pair_attempt_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.client.post("/login", json={"token":"x"*43}).status_code, 401)
        self.assertEqual(self.client.post("/login", json={"token":"x"*43}).status_code, 429)

    def test_session_expires(self):
        self.pair()
        token = self.client.cookies.get(COOKIE)
        self.server._sessions[token]["deadline"] = 0
        self.assertEqual(self.client.get("/api/session").status_code, 401)
        self.assertEqual(self.client.post("/api/command", json={"text":"x","request_id":"one"}).status_code, 401)

    def test_commands_share_agent_and_replays_do_not_execute_twice(self):
        session = self.pair()
        body = {"text":"Open Chrome", "request_id":"request-1"}
        first = self.client.post("/api/command", json=body)
        self.assertEqual(first.status_code, 200)
        second = self.client.post("/api/command", json=body)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(first.json()["task"]["id"], second.json()["task"]["id"])
        self.submit.assert_called_once_with("Open Chrome", "request-1", session["device_id"])
        self.assertEqual(self.client.post("/api/command", json={**body,"text":"Delete files"}).status_code, 409)
        self.assertEqual(len(self.server._history), 1)

    def test_missing_agent_fails_without_queuing(self):
        self.pair()
        self.server._submit = None
        self.assertEqual(self.client.post("/api/command", json={"text":"x", "request_id":"one"}).status_code, 503)
        self.assertTrue(self.server._command_queue.empty())

    def test_cancel_routes_same_task(self):
        self.pair()
        result = self.client.post("/api/command", json={"text":"x", "request_id":"one"}).json()
        task = self.client.post("/api/cancel", json={"task_id":result["task"]["id"]}).json()["task"]
        self.assertEqual(task["state"], "cancelled")
        self.assertEqual(self.client.post("/api/cancel", json={"task_id":"missing"}).status_code, 404)

    def test_websocket_auth_and_origin_enforced(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("ws://127.0.0.1:8000/ws", headers={"Origin":ORIGIN}): pass
        self.pair()
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("ws://127.0.0.1:8000/ws", headers={"Origin":"https://evil.example"}): pass
        with self.client.websocket_connect("ws://127.0.0.1:8000/ws", headers={"Origin":ORIGIN}) as ws:
            ws.send_json({"type":"hello","csrf":"incorrect"})
            self.assertEqual(ws.receive()["code"], 4003)

    def test_websocket_snapshot_updates_reconnect_and_revocation(self):
        session = self.pair()
        with self.client.websocket_connect("ws://127.0.0.1:8000/ws", headers={"Origin":ORIGIN}) as ws:
            ws.send_json({"type":"hello","csrf":session["csrf"]})
            snapshot = ws.receive_json()
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertEqual(snapshot["status"]["personality"], "Wednesday")
            self.client.post("/api/command", json={"text":"x","request_id":"one"})
            self.assertEqual(ws.receive_json()["type"], "log")
            self.assertEqual(ws.receive_json()["task"]["state"], "queued")
        with self.client.websocket_connect("ws://127.0.0.1:8000/ws", headers={"Origin":ORIGIN}) as ws:
            ws.send_json({"type":"hello","csrf":session["csrf"]})
            snapshot = ws.receive_json()
            self.assertEqual(len(snapshot["tasks"]), 1)
            self.assertEqual(len(snapshot["history"]), 1)
            self.submit.assert_called_once()
            self.assertEqual(self.client.post("/api/revoke-devices", json={}).status_code, 200)
            self.assertEqual(ws.receive()["code"], 4001)
        self.assertEqual(self.client.get("/api/session").status_code, 401)
        self.assertEqual(self.task_list[0]["state"], "cancelled")

    def test_audio_requires_authenticated_handshake_and_bounds(self):
        session = self.pair()
        with self.client.websocket_connect("ws://127.0.0.1:8000/ws/phone-audio", headers={"Origin":ORIGIN}) as ws:
            ws.send_json({"type":"hello","csrf":session["csrf"]})
            self.assertEqual(ws.receive_json()["type"], "ready")
            ws.send_bytes(b"x"*8193)
            self.assertEqual(ws.receive()["code"], 4003)
        self.assertTrue(self.server._phone_audio_queue.empty())

    def test_phone_audio_keeps_device_identity_and_revocation(self):
        session = self.pair()
        device_id = session["device_id"]
        self.assertTrue(self.server.device_authorized(device_id))
        self.assertFalse(self.server.device_authorized("unknown-device"))
        with self.client.websocket_connect("ws://127.0.0.1:8000/ws/phone-audio", headers={"Origin":ORIGIN}) as ws:
            ws.send_json({"type":"hello","csrf":session["csrf"]})
            ws.receive_json()
            ws.send_bytes(b"\x01\x02"*1024)
            ws.send_bytes(b"invalid-odd")
            self.assertEqual(ws.receive()["code"], 4003)
        frame = self.server._phone_audio_queue.get_nowait()
        self.assertEqual(frame["device_id"], device_id)
        self.assertEqual(frame["data"], b"\x01\x02"*1024)
        self.assertEqual(frame["mime_type"], "audio/pcm")
        self.client.post("/api/revoke-devices", json={"device_id":device_id})
        self.assertFalse(self.server.device_authorized(device_id))

    def test_expired_phone_device_is_not_authorized(self):
        session = self.pair()
        token = self.client.cookies.get(COOKIE)
        self.server._sessions[token]["deadline"] = 0
        self.assertFalse(self.server.device_authorized(session["device_id"]))

    def test_revoke_cancels_tasks_omitted_from_snapshot(self):
        self.pair()
        self.client.post("/api/command", json={"text":"x", "request_id":"one"})
        self.server._snapshot = lambda: []
        self.client.post("/api/revoke-devices", json={})
        self.assertEqual(self.task_list[0]["state"], "cancelled")

    def test_websocket_rejects_wrong_frame_type(self):
        session = self.pair()
        with self.client.websocket_connect("ws://127.0.0.1:8000/ws/phone-audio", headers={"Origin":ORIGIN}) as ws:
            ws.send_json({"type":"hello","csrf":session["csrf"]})
            ws.receive_json()
            ws.send_text("not pcm")
            self.assertEqual(ws.receive()["code"], 4003)
        with self.client.websocket_connect("ws://127.0.0.1:8000/ws", headers={"Origin":ORIGIN}) as ws:
            ws.send_json({"type":"hello","csrf":session["csrf"]})
            ws.receive_json()
            ws.send_bytes(b"not text")
            self.assertEqual(ws.receive()["code"], 4003)

    def test_revoke_all_clears_pairing_and_pending_audio(self):
        self.pair()
        self.server.new_key()
        self.server._phone_audio_queue.put_nowait({"data":b"abc"})
        self.assertEqual(asyncio.run(self.server.revoke_all()), 1)
        self.assertEqual(self.server._pending_keys, {})
        self.assertTrue(self.server._phone_audio_queue.empty())
        self.assertEqual(self.client.get("/api/session").status_code, 401)

    def test_bounded_upload_download_and_auth(self):
        self.assertEqual(self.client.post("/api/upload", files={"file":("test.txt",b"hello")}).status_code, 401)
        self.pair()
        result = self.client.post("/api/upload", files={"file":("../test.html",b"<script>bad</script>")})
        self.assertEqual(result.status_code, 200, result.text)
        name = result.json()["name"]
        self.assertNotIn("..", name)
        response = self.client.get("/uploads/"+name)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(self.client.post("/api/upload", content=b"x",headers={"Content-Length":"30000000"}).status_code, 413)
        self.assertEqual(self.client.get("/uploads/nonexistent").status_code, 404)

    def test_upload_actual_body_limit_rejects_false_content_length(self):
        self.pair()
        with patch("dashboard.server.MAX_UPLOAD_MB", 0):
            result = self.client.post("/api/upload", content=b"x"*70000,
                                      headers={"Content-Length":"100", "Content-Type":"multipart/form-data; boundary=x"})
        self.assertEqual(result.status_code, 413)
        self.assertEqual(list(self.server._uploads_dir.iterdir()), [])

    def test_security_headers_static_allowlist_and_no_api_schema(self):
        result = self.client.get("/")
        self.assertEqual(result.status_code, 200)
        self.assertIn("frame-ancestors 'none'", result.headers["content-security-policy"])
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/static/crypto-js.min.js").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)

    def test_default_binding_and_invalid_lan_configuration(self):
        self.assertEqual(self.server.get_url(), ORIGIN)
        for host in ("0.0.0.0", "8.8.8.8", "malicious.example"):
            with patch.dict("os.environ", {"CHIDVI_CONTROLLER_HOST":host}), self.assertRaises(ValueError):
                DashboardServer()
        with patch.dict("os.environ", {"CHIDVI_CONTROLLER_HOST":"192.168.1.10"}), patch("dashboard.server._local_addresses", return_value={"192.168.1.10"}), self.assertRaisesRegex(ValueError,"TLS"):
            DashboardServer()

    def test_certificate_helper_preserves_files_and_matches_ip(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from dashboard.certificates import create_certificate
        folder = Path(self.folder.name) / "certificates"
        fingerprint = create_certificate("192.168.1.10", folder)
        certificate = x509.load_pem_x509_certificate((folder / "jarvis.crt").read_bytes())
        authority = x509.load_pem_x509_certificate((folder / "controller-ca.crt").read_bytes())
        self.assertEqual(fingerprint, authority.fingerprint(hashes.SHA256()).hex(":"))
        addresses = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
        self.assertEqual(str(addresses[0]), "192.168.1.10")
        authority.public_key().verify(certificate.signature, certificate.tbs_certificate_bytes, padding.PKCS1v15(), certificate.signature_hash_algorithm)
        original = (folder / "jarvis.key").read_bytes()
        with self.assertRaises(FileExistsError): create_certificate("192.168.1.10", folder)
        self.assertEqual((folder / "jarvis.key").read_bytes(), original)
        with self.assertRaises(ValueError): create_certificate("8.8.8.8", folder / "public")

    def test_lan_tls_cookie_and_serving_uses_restricted_host(self):
        with patch.dict("os.environ", {"CHIDVI_CONTROLLER_HOST":"192.168.1.10"}), patch("dashboard.server._local_addresses", return_value={"192.168.1.10"}), patch.object(DashboardServer,"_ssl_enabled",return_value=True):
            server = DashboardServer()
            with TestClient(server.app, base_url="https://192.168.1.10:8000", headers={"Origin":"https://192.168.1.10:8000"}) as client:
                result = client.post("/login",json={"token":server.new_key()})
                self.assertEqual(result.status_code, 200)
                self.assertIn("Secure", result.headers["set-cookie"])


if __name__ == "__main__":
    unittest.main()
