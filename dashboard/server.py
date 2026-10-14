"""Authenticated phone access to the existing CHIDVI agent; loopback by default."""
from __future__ import annotations

import asyncio
from collections import deque
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import uvicorn

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).parent / "static"
PORT = 8000
MAX_UPLOAD_MB = 20
MAX_JSON_BYTES = 16_384
COOKIE = "chidvi_controller"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_LOG = logging.getLogger(__name__)


def _local_addresses() -> set[str]:
    import psutil
    return {address.address for addresses in psutil.net_if_addrs().values()
            for address in addresses}


class _UploadSizeLimit:
    """Bound the actual body before FastAPI's multipart parser touches it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/api/upload":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_UPLOAD_MB * 1024 * 1024 + 65536:
                response = JSONResponse({"detail": "Upload body is too large."}, status_code=413)
                return await response(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


class DashboardServer:
    """One server and one agent. Pairing, sessions and history stay in RAM."""

    def __init__(self):
        self._ip = os.environ.get("CHIDVI_CONTROLLER_HOST", "127.0.0.1").strip()
        try:
            address = ipaddress.IPv4Address(self._ip)
        except ipaddress.AddressValueError as exc:
            raise ValueError("CHIDVI_CONTROLLER_HOST must be a local IPv4 address.") from exc
        self._lan = not address.is_loopback
        if self._lan:
            private = any(address in ipaddress.ip_network(network) for network in
                          ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
            if not private or self._ip not in _local_addresses():
                raise ValueError("Controller host must be an assigned private LAN IPv4 address.")
            if not self._ssl_enabled():
                raise ValueError("LAN control requires config/certs/jarvis.crt and jarvis.key (TLS).")
        self._session_ttl = int(os.environ.get("CHIDVI_CONTROLLER_SESSION_TTL", "43200"))
        if not 60 <= self._session_ttl <= 604800:
            raise ValueError("CHIDVI_CONTROLLER_SESSION_TTL must be 60..604800 seconds.")
        self._pending_keys: dict[str, float] = {}
        self._pair_lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._clients: dict[WebSocket, str] = {}
        self._audio_clients: dict[WebSocket, str] = {}
        self._history: list[dict] = []
        self._sequence = 0
        self._rates: dict[str, deque] = {}
        self._command_queue = asyncio.Queue(maxsize=32)
        self._phone_audio_queue = asyncio.Queue(maxsize=64)
        self._connect_callback = self._wake_callback = None
        self._submit = self._cancel = self._snapshot = self._status = None
        self._uploads_active: set[str] = set()
        self._uploads_dir = Path.home() / "Downloads" / "JARVIS Uploads"
        self.app = self._build_app()

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").is_file() and (certs / "jarvis.crt").is_file()

    def get_url(self) -> str:
        return f"{'https' if self._ssl_enabled() else 'http'}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        return self.get_url()

    def new_key(self, expiry_secs: int = 600) -> str:
        if not 1 <= expiry_secs <= 3600:
            raise ValueError("Pairing expiry must be between 1 and 3600 seconds.")
        key = secrets.token_urlsafe(32)
        with self._pair_lock:
            self._pending_keys = {key: time.monotonic() + expiry_secs}
        return key

    def cancel_pairing(self, key: str | None = None) -> None:
        with self._pair_lock:
            if key is None:
                self._pending_keys.clear()
            else:
                self._pending_keys.pop(key, None)

    def get_pairing_url(self, key: str) -> str:
        return f"{self.get_url()}/login#pair={quote(key, safe='')}"

    def set_task_callbacks(self, submit, cancel, snapshot) -> None:
        self._submit, self._cancel, self._snapshot = submit, cancel, snapshot

    def set_status_callback(self, callback) -> None:
        self._status = callback

    def set_connect_callback(self, callback) -> None:
        self._connect_callback = callback

    def set_wake_callback(self, callback) -> None:
        self._wake_callback = callback

    def _session(self, token: str | None) -> dict | None:
        session = self._sessions.get(token or "")
        if session and session["deadline"] > time.monotonic():
            return session
        self._sessions.pop(token or "", None)
        return None

    def device_authorized(self, device_id: str) -> bool:
        return any(session["device_id"] == device_id and session["deadline"] > time.monotonic()
                   for session in self._sessions.values())

    def _authenticated(self, request: Request, *, csrf: bool = False) -> dict:
        session = self._session(request.cookies.get(COOKIE))
        if session is None:
            raise HTTPException(401, "Pair this device again on the laptop.")
        supplied = request.headers.get("x-csrf-token", "")
        if csrf and not secrets.compare_digest(supplied, session["csrf"]):
            raise HTTPException(403, "Invalid request verification token.")
        return session

    def _origin_ok(self, headers, *, required: bool = False) -> bool:
        host = headers.get("host", "")
        hosts = {f"{self._ip}:{PORT}"}
        if not self._lan:
            hosts.add(f"localhost:{PORT}")
        if host not in hosts:
            return False
        origin = headers.get("origin")
        scheme = "https" if self._ssl_enabled() else "http"
        return origin == f"{scheme}://{host}" if origin else not required

    def _rate_limit(self, key: str, limit: int, window: int = 60) -> None:
        now = time.monotonic()
        if len(self._rates) > 1000:
            self._rates = {k: v for k, v in self._rates.items() if v and v[-1] > now - 60}
        entries = self._rates.setdefault(key, deque())
        while entries and entries[0] <= now - window:
            entries.popleft()
        if len(entries) >= limit:
            raise HTTPException(429, "Too many requests. Wait a minute before trying again.")
        entries.append(now)

    async def _json(self, request: Request) -> dict:
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "Send application/json.")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_JSON_BYTES:
                raise HTTPException(413, "Request too large.")
            body.extend(chunk)
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "Invalid JSON.") from exc
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected a JSON object.")
        return value

    def _status_info(self) -> dict:
        status = dict(self._status() or {}) if self._status else {}
        return {**status, "tls": self._ssl_enabled(), "lan_enabled": self._lan,
                "connected_devices": len({s["device_id"] for token in self._clients.values()
                                          if (s := self._session(token))}),
                "screenshot_preview": False}

    def _tasks(self) -> list:
        return list(self._snapshot() or []) if self._snapshot else []

    async def broadcast(self, message: dict) -> None:
        self._sequence += 1
        message = {**message, "sequence": self._sequence}
        if message.get("type") in {"log", "sys", "file_received"}:
            self._history = (self._history + [message])[-300:]
        for websocket, token in list(self._clients.items()):
            try:
                if self._session(token) is None:
                    await websocket.close(code=4001)
                    self._clients.pop(websocket, None)
                else:
                    await asyncio.wait_for(websocket.send_json(message), timeout=2)
            except Exception:
                self._clients.pop(websocket, None)

    async def _revoke(self, device_id: str | None) -> int:
        tokens = {token for token, session in self._sessions.items()
                  if device_id is None or session["device_id"] == device_id}
        devices = {self._sessions[token]["device_id"] for token in tokens}
        tasks = {task["id"]: task for task in self._tasks()}
        for token in tokens:
            for _, task in self._sessions[token]["requests"].values():
                tasks.setdefault(task["id"], task)
        for token in tokens:
            self._sessions.pop(token, None)
        if self._cancel:
            for task in tasks.values():
                if task.get("source") in devices and task.get("state") not in {"succeeded", "failed", "cancelled"}:
                    try:
                        await self.broadcast({"type": "task", "task": self._cancel(task["id"])})
                    except Exception:
                        _LOG.exception("Could not cancel a revoked device's task")
        for clients in (self._clients, self._audio_clients):
            for websocket, token in list(clients.items()):
                if token in tokens:
                    try:
                        await websocket.close(code=4001)
                    except Exception:
                        pass
                    clients.pop(websocket, None)
        while not self._phone_audio_queue.empty():
            self._phone_audio_queue.get_nowait()
        _LOG.info("Controller devices revoked: %d", len(tokens))
        return len(tokens)

    async def revoke_all(self) -> int:
        self.cancel_pairing()
        return await self._revoke(None)

    async def _websocket_session(self, websocket: WebSocket) -> tuple[str, dict] | None:
        token = websocket.cookies.get(COOKIE, "")
        session = self._session(token)
        if session is None or not self._origin_ok(websocket.headers, required=True):
            await websocket.close(code=4001)
            return None
        if sum(t == token for t in (*self._clients.values(), *self._audio_clients.values())) >= 3:
            await websocket.close(code=4008)
            return None
        try:
            self._rate_limit(f"socket:{session['device_id']}", 30)
        except HTTPException:
            await websocket.close(code=4008)
            return None
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=5)
            if len(raw) > 1024:
                raise ValueError("Handshake too large")
            hello = json.loads(raw)
            if (not isinstance(hello, dict) or hello.get("type") != "hello"
                    or not isinstance(hello.get("csrf"), str)
                    or not secrets.compare_digest(hello["csrf"], session["csrf"])):
                raise ValueError("Invalid handshake")
        except (ValueError, KeyError, TypeError, asyncio.TimeoutError, WebSocketDisconnect):
            await websocket.close(code=4003)
            return None
        return token, session

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.add_middleware(_UploadSizeLimit)

        @app.middleware("http")
        async def guard(request: Request, call_next):
            mutation = request.method not in {"GET", "HEAD"}
            if not self._origin_ok(request.headers, required=mutation):
                return JSONResponse({"detail": "Untrusted host or origin."}, status_code=403)
            if self._lan and request.url.scheme != "https":
                return JSONResponse({"detail": "LAN control requires TLS."}, status_code=403)
            upload_device = None
            if request.url.path == "/api/upload":
                try:
                    upload_session = self._authenticated(request, csrf=True)
                    upload_device = upload_session["device_id"]
                    self._rate_limit(f"upload:{upload_device}", 5)
                    if upload_device in self._uploads_active or len(self._uploads_active) >= 2:
                        raise HTTPException(429, "Another upload is in progress. Try again shortly.")
                    size = int(request.headers.get("content-length", "-1"))
                    if not 0 <= size <= (MAX_UPLOAD_MB * 1024 * 1024 + 65536):
                        raise HTTPException(413, f"Upload needs a length and a {MAX_UPLOAD_MB} MB limit.")
                except (ValueError, HTTPException) as exc:
                    return JSONResponse({"detail": getattr(exc, "detail", "Invalid upload length.")},
                                        status_code=getattr(exc, "status_code", 400))
            if upload_device:
                self._uploads_active.add(upload_device)
            try:
                response = await call_next(request)
            finally:
                if upload_device:
                    self._uploads_active.discard(upload_device)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self' blob:; style-src 'self'; "
                "connect-src 'self'; img-src 'self' blob:; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'")
            response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
            return response

        @app.get("/", response_class=HTMLResponse)
        async def index():
            return HTMLResponse((STATIC_DIR / "app.html").read_text(encoding="utf-8"))

        @app.get("/login", response_class=HTMLResponse)
        @app.get("/auto-login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse((STATIC_DIR / "login.html").read_text(encoding="utf-8"))

        @app.get("/static/{filename}")
        async def static_file(filename: str):
            if filename not in {"controller.css", "controller.js", "login.js"}:
                raise HTTPException(404, "Not found.")
            return FileResponse(STATIC_DIR / filename)

        @app.post("/login")
        async def login(request: Request):
            host = request.client.host if request.client else "unknown"
            self._rate_limit(f"pair:{host}", 5)
            self._rate_limit("pair:all", 60)
            body = await self._json(request)
            key = body.get("pin", body.get("token", ""))
            if not isinstance(key, str) or len(key) != 43:
                raise HTTPException(401, "Invalid or expired pairing code.")
            self._sessions = {t: s for t, s in self._sessions.items()
                              if s["deadline"] > time.monotonic()}
            if len(self._sessions) >= 16:
                raise HTTPException(429, "Revoke an existing device before pairing another.")
            with self._pair_lock:
                expiry = self._pending_keys.pop(key, 0)
            if expiry <= time.monotonic():
                raise HTTPException(401, "Invalid or expired pairing code.")
            token = secrets.token_urlsafe(32)
            session = {"device_id": secrets.token_hex(12), "csrf": secrets.token_urlsafe(32),
                       "deadline": time.monotonic() + self._session_ttl,
                       "expires_at": time.time() + self._session_ttl, "requests": {}}
            self._sessions[token] = session
            response = JSONResponse({"ok": True, "device_id": session["device_id"],
                                     "expires_at": session["expires_at"]})
            response.set_cookie(COOKIE, token, max_age=self._session_ttl, httponly=True,
                                secure=self._ssl_enabled(), samesite="strict", path="/")
            if self._connect_callback:
                self._connect_callback()
            _LOG.info("Controller paired device %s", session["device_id"])
            return response

        @app.get("/api/session")
        async def session_info(request: Request):
            session = self._authenticated(request)
            return {"csrf": session["csrf"], "device_id": session["device_id"],
                    "expires_at": session["expires_at"], "status": self._status_info(),
                    "tasks": self._tasks()}

        @app.get("/api/devices")
        async def devices(request: Request):
            self._authenticated(request)
            return {"devices": [{"device_id": s["device_id"], "expires_at": s["expires_at"]}
                                for s in list(self._sessions.values())
                                if s["deadline"] > time.monotonic()]}

        @app.post("/api/revoke-devices")
        async def revoke(request: Request):
            self._authenticated(request, csrf=True)
            body = await self._json(request)
            device_id = body.get("device_id")
            if device_id is not None and (not isinstance(device_id, str)
                                          or not _IDENTIFIER.fullmatch(device_id)):
                raise HTTPException(400, "Invalid device ID.")
            return {"ok": True, "revoked": await self._revoke(device_id)}

        @app.post("/api/command")
        async def command(request: Request):
            session = self._authenticated(request, csrf=True)
            self._rate_limit(f"command:{session['device_id']}", 30)
            body = await self._json(request)
            text, request_id = body.get("text"), body.get("request_id")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000 or "\x00" in text:
                raise HTTPException(400, "Command must contain 1..4000 characters.")
            if not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
                raise HTTPException(400, "A valid request_id is required.")
            text = text.strip()
            previous = session["requests"].get(request_id)
            if previous:
                if previous[0] != text:
                    raise HTTPException(409, "This request ID belongs to another command.")
                task = next((t for t in self._tasks() if t.get("id") == previous[1].get("id")), previous[1])
                return {"ok": True, "task": task, "duplicate": True}
            if len(session["requests"]) >= 500:
                raise HTTPException(429, "Session command limit reached. Pair again to continue.")
            if not self._submit:
                raise HTTPException(503, "The laptop agent is not ready. No task was started.")
            try:
                task = self._submit(text, request_id, session["device_id"])
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(409, str(exc)) from exc
            session["requests"][request_id] = (text, dict(task))
            await self.broadcast({"type": "log", "speaker": "user", "text": text,
                                  "task_id": task.get("id")})
            await self.broadcast({"type": "task", "task": task})
            _LOG.info("Controller submitted device=%s task=%s", session["device_id"], task.get("id"))
            return {"ok": True, "task": task}

        @app.post("/api/cancel")
        async def cancel(request: Request):
            session = self._authenticated(request, csrf=True)
            self._rate_limit(f"cancel:{session['device_id']}", 60)
            body = await self._json(request)
            task_id = body.get("task_id")
            if not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id):
                raise HTTPException(400, "A valid task_id is required.")
            if not self._cancel:
                raise HTTPException(503, "The laptop agent is not ready.")
            try:
                task = self._cancel(task_id)
            except (ValueError, KeyError) as exc:
                raise HTTPException(404, "Task not found.") from exc
            await self.broadcast({"type": "task", "task": task})
            return {"ok": True, "task": task}

        @app.post("/api/wake")
        async def wake(request: Request):
            session = self._authenticated(request, csrf=True)
            self._rate_limit(f"wake:{session['device_id']}", 10)
            if self._wake_callback:
                self._wake_callback()
            return {"ok": True}

        @app.post("/api/upload")
        async def upload(request: Request, file: UploadFile = File(...)):
            session = self._authenticated(request, csrf=True)
            raw = (file.filename or "upload").replace("\\", "/").split("/")[-1]
            safe = re.sub(r'[^\w. -]', "_", raw, flags=re.UNICODE).strip(". ")[:120] or "upload"
            self._uploads_dir.mkdir(parents=True, exist_ok=True)
            destination = self._uploads_dir / f"{secrets.token_hex(6)}_{safe}"
            size, created = 0, False
            try:
                with destination.open("xb") as output:
                    created = True
                    while chunk := await file.read(65536):
                        size += len(chunk)
                        if size > MAX_UPLOAD_MB * 1024 * 1024:
                            raise HTTPException(413, f"Files must be smaller than {MAX_UPLOAD_MB} MB.")
                        await asyncio.to_thread(output.write, chunk)
            except BaseException:
                if created:
                    destination.unlink(missing_ok=True)
                raise
            finally:
                await file.close()
            await self.broadcast({"type": "file_received", "name": destination.name, "size": size})
            return {"ok": True, "name": destination.name, "size": size}

        @app.get("/api/files")
        async def files(request: Request):
            self._authenticated(request)
            if not self._uploads_dir.exists():
                return {"files": []}
            return {"files": [{"name": p.name, "size": p.stat().st_size}
                              for p in self._uploads_dir.iterdir() if p.is_file() and not p.is_symlink()][:100]}

        @app.get("/uploads/{filename}")
        async def download(request: Request, filename: str):
            self._authenticated(request)
            path = self._uploads_dir / filename
            if (Path(filename).name != filename or path.is_symlink()
                    or path.resolve().parent != self._uploads_dir.resolve() or not path.is_file()):
                raise HTTPException(404, "File not found.")
            return FileResponse(path, filename=filename, media_type="application/octet-stream")

        @app.websocket("/ws")
        async def websocket_updates(websocket: WebSocket):
            auth = await self._websocket_session(websocket)
            if not auth:
                return
            token, _ = auth
            self._clients[websocket] = token
            try:
                await websocket.send_json({"type": "snapshot", "history": self._history,
                                           "tasks": self._tasks(), "status": self._status_info()})
                while self._session(token):
                    try:
                        message = await asyncio.wait_for(websocket.receive_text(), timeout=5)
                        if message != "ping":
                            await websocket.close(code=4003)
                            return
                    except asyncio.TimeoutError:
                        pass
                    if self._session(token) is None:
                        break
                    await websocket.send_json({"type": "connection", "status": self._status_info()})
                await websocket.close(code=4001)
            except KeyError:
                await websocket.close(code=4003)
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                self._clients.pop(websocket, None)

        @app.websocket("/ws/phone-audio")
        async def phone_audio(websocket: WebSocket):
            auth = await self._websocket_session(websocket)
            if not auth:
                return
            token, session = auth
            self._audio_clients[websocket] = token
            try:
                await websocket.send_json({"type": "ready"})
                while self._session(token):
                    try:
                        data = await asyncio.wait_for(websocket.receive_bytes(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    if self._session(token) is None:
                        break
                    if not data or len(data) > 8192 or len(data) % 2:
                        await websocket.close(code=4003)
                        return
                    try:
                        self._rate_limit(f"audio:{session['device_id']}", 60, window=1)
                    except HTTPException:
                        await websocket.close(code=4008)
                        return
                    try:
                        self._phone_audio_queue.put_nowait({"data": data, "mime_type": "audio/pcm",
                                                            "device_id": session["device_id"]})
                    except asyncio.QueueFull:
                        pass
                await websocket.close(code=4001)
            except KeyError:
                await websocket.close(code=4003)
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                self._audio_clients.pop(websocket, None)

        return app

    async def serve(self) -> None:
        certs = BASE_DIR / "config" / "certs"
        tls = ({"ssl_keyfile": str(certs / "jarvis.key"), "ssl_certfile": str(certs / "jarvis.crt")}
               if self._ssl_enabled() else {})
        config = uvicorn.Config(self.app, host=self._ip, port=PORT, log_level="warning",
                                proxy_headers=False, ws_max_size=16384, **tls)
        _LOG.info("Controller listening at %s; firewall unchanged", self.get_url())
        await uvicorn.Server(config).serve()
