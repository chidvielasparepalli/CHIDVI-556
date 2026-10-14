"""Actual main orchestration methods, without starting audio, cloud or the GUI."""
import ast
import asyncio
from contextlib import nullcontext, suppress
from datetime import datetime
import json
from pathlib import Path
import threading
import tempfile
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core import confirm
from core.task_control import TaskStore


tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "JarvisLive")
methods = {"_process_dashboard_commands", "_submit_task", "_finish_task", "_run_provider_task",
           "_on_provider_change", "_sync_provider_state", "_apply_provider_switch",
           "_begin_voice_task", "_voice_source_authorized", "_send_realtime", "_receive_audio",
           "_pin_task_providers", "_execute_tool", "_live_voice_authorized", "_chat_available"}
node.body = [item for item in node.body if getattr(item, "name", None) in methods]
assert {item.name for item in node.body} == methods
scope = {"asyncio": asyncio, "confirm_gate": confirm, "json": json, "time": time,
         "datetime": datetime, "traceback": traceback, "TOOL_DECLARATIONS": [],
         "_STOP_WORDS": ["stop"], "_clean_transcript": lambda text: text, "print": Mock(),
         "types": SimpleNamespace(Blob=lambda **fields: SimpleNamespace(**fields),
                                  FunctionResponse=lambda **fields: SimpleNamespace(**fields))}
exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), scope)


class Runtime(scope["JarvisLive"]):
    def __init__(self, *, live=False, connected=True):
        self._tasks = TaskStore()
        self._task_done, self._task_wakeup, self._model_idle = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self._model_idle.set()
        self._game_stop = threading.Event()
        self._click_speech = SimpleNamespace(stopped=threading.Event())
        self._loop = asyncio.get_running_loop()
        self._profile = {"id": "primary", "provider": "gemini" if live else "openai",
                         "model": "gemini-3.1-flash-live-preview" if live else "gpt-4.1", "limits": {}}
        store = SimpleNamespace(selected=Mock(side_effect=lambda role, **kw: self._profile if role == "chat" or role == "live_audio" and live else None),
                                selection=nullcontext, record_status=Mock(),
                                credential=Mock(return_value="synthetic-key"), get_profile=Mock(side_effect=lambda identity: dict(self._profile)))
        self._providers = SimpleNamespace(store=store, pending={},
            run=AsyncMock(return_value={"text": "Synthetic answer", "waiting": False}))
        self._provider_task_id, self._provider_voice_suppressed = None, False
        self._provider_resume, self._provider_reconnect_pending = {}, False
        self._provider_contexts = {}
        self._screen_sharing = SimpleNamespace(stop=Mock())
        self._sharing_context = AsyncMock(return_value="")
        self.session = self.new_session() if connected else None
        self._wake_enabled, self._awake = False, True
        self._voice_turn_source = None
        self._reference_voice, self._reference_audio_queue, self._turn_done_event = None, None, None
        self._reference_turn_open, self._vision_busy, self._interrupted = False, False, False
        self._personality_reconnect_pending = False
        self._personality_reconnect_reason = ""
        self._session_log, self._asst_name, self._resume_handle = [], "CHIDVI", None
        self._last_user_speech = 0
        self._voice_generation = 1
        self.ui = SimpleNamespace(write_log=Mock(), set_state=Mock(), set_provider_state=Mock(),
                                  show_content=Mock(), muted=True)
        self._dashboard = SimpleNamespace(device_authorized=Mock(return_value=True), broadcast=AsyncMock())
        self._action_registry = self._plugin_registry = SimpleNamespace(get_tool_declarations=lambda: [])
        self._build_config = Mock(return_value=SimpleNamespace(system_instruction="Personality is preserved."))
        self._execute_tool = AsyncMock()
        self._queue_audio_pcm = Mock()
        self.interrupt, self.request_reconnect = Mock(), Mock()
        self.out_queue = asyncio.Queue()

    def new_session(self):
        self.incoming = asyncio.Queue()
        async def receive():
            while True:
                yield await self.incoming.get()
        return SimpleNamespace(receive=receive, send_client_content=AsyncMock(),
                               send_realtime_input=AsyncMock(), send_tool_response=AsyncMock())


async def until(predicate):
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(.005)
    raise AssertionError("Runtime condition was not reached")


async def stop(worker):
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker


def response(*, tool=False, complete=False, transcript="", output=""):
    content = SimpleNamespace(input_transcription=SimpleNamespace(text=transcript),
                              output_transcription=SimpleNamespace(text=output),
                              turn_complete=complete, interrupted=False)
    call = SimpleNamespace(function_calls=[SimpleNamespace(id="late", name="computer_control", args={})]) if tool else None
    return SimpleNamespace(data=None, server_content=content, tool_call=call)


class ProviderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_typed_task_without_live_voice_keeps_original_owner_and_result(self):
        runtime = Runtime(connected=False)
        seen = []
        async def run(task_id, text, **kwargs):
            seen.append((task_id, runtime._tasks.owner(), text, kwargs["profile"]["model"]))
            kwargs["on_text"]("Synthetic ")
            return {"text": "Synthetic answer", "waiting": False}
        runtime._providers.run.side_effect = run
        task = runtime._submit_task("hello", "desktop-one")
        worker = asyncio.create_task(runtime._process_dashboard_commands())
        try:
            await until(lambda: runtime._tasks.get(task["id"])["state"] == "succeeded")
            self.assertEqual(seen, [(task["id"], task["id"], "hello", "gpt-4.1")])
            self.assertIn("Synthetic answer", runtime._tasks.get(task["id"])["detail"])
            runtime.ui.show_content.assert_any_call("AI RESPONSE", "Synthetic answer")
            self.assertIn("Personality is preserved.", runtime._providers.run.await_args.kwargs["system"])
            runtime.request_reconnect.assert_not_called()
        finally:
            await stop(worker)

    async def test_external_phone_and_desktop_requests_are_serial_and_not_sent_to_live(self):
        runtime = Runtime()
        gate = asyncio.Event()
        async def run(task_id, text, **kwargs):
            if text == "first":
                await gate.wait()
            return {"text": "reply to " + text, "waiting": False}
        runtime._providers.run.side_effect = run
        first = runtime._submit_task("first", "one", "paired-phone")
        second = runtime._submit_task("second", "two", "desktop")
        worker = asyncio.create_task(runtime._process_dashboard_commands())
        try:
            await until(lambda: runtime._providers.run.await_count == 1)
            self.assertEqual(runtime._tasks.get(second["id"])["state"], "queued")
            gate.set()
            await until(lambda: runtime._tasks.get(second["id"])["state"] == "succeeded")
            self.assertEqual([call.args[0] for call in runtime._providers.run.await_args_list], [first["id"], second["id"]])
            runtime.session.send_client_content.assert_not_awaited()
        finally:
            gate.set()
            await stop(worker)

    async def test_existing_live_chat_queue_still_delivers_once_and_waits_for_turn(self):
        runtime = Runtime(live=True)
        task = runtime._submit_task("original live command", "one")
        worker = asyncio.create_task(runtime._process_dashboard_commands())
        try:
            await until(lambda: runtime.session.send_client_content.await_count == 1)
            runtime._providers.run.assert_not_awaited()
            self.assertEqual(runtime._tasks.active_id, task["id"])
            runtime._finish_task(detail="Live answer")
            await until(lambda: runtime._tasks.active_id is None)
            self.assertEqual(runtime.session.send_client_content.await_count, 1)
            self.assertEqual(runtime._tasks.get(task["id"])["state"], "succeeded")
        finally:
            await stop(worker)

    async def test_existing_phone_voice_task_is_not_replayed_to_external_chat(self):
        runtime = Runtime()
        queued = runtime._submit_task("typed command", "one")
        runtime._voice_turn_source = "paired-phone"
        runtime._begin_voice_task("voice command already delivered")
        voice = runtime._tasks.get(runtime._tasks.active_id)
        worker = asyncio.create_task(runtime._process_dashboard_commands())
        try:
            await asyncio.sleep(.03)
            self.assertTrue(voice["input_delivered"])
            self.assertEqual(voice["source"], "paired-phone")
            runtime._providers.run.assert_not_awaited()
            runtime.session.send_client_content.assert_not_awaited()
            self.assertEqual(runtime._tasks.get(queued["id"])["state"], "queued")
            runtime._finish_task(detail="Voice answer")
            await until(lambda: runtime._tasks.get(queued["id"])["state"] == "succeeded")
            self.assertEqual(runtime._providers.run.await_args.args[0], queued["id"])
        finally:
            await stop(worker)

    async def test_provider_switch_waits_for_active_task_and_stops_sharing(self):
        runtime = Runtime()
        task = runtime._submit_task("in progress", "active")
        runtime._tasks.claim(task["id"])
        with patch("memory.config_manager.save_live_sharing_startup") as save:
            runtime._on_provider_change("new-profile")
            await asyncio.sleep(.02)
            runtime.request_reconnect.assert_not_called()
            self.assertTrue(runtime._provider_reconnect_pending)
            runtime._screen_sharing.stop.assert_called_once()
            save.assert_called_once_with(False)
            runtime._tasks.update(task["id"], "succeeded")
            runtime._tasks.release(task["id"])
            runtime._apply_provider_switch()
            runtime.request_reconnect.assert_called_once_with(keep_context=False, reason="AI provider configuration changed")

    async def test_gemini_tools_and_stop_are_gated_while_external_chat_owns_task(self):
        runtime = Runtime()
        task = runtime._submit_task("external operation", "external")
        runtime._tasks.claim(task["id"])
        runtime._provider_task_id = task["id"]
        receiver = asyncio.create_task(runtime._receive_audio())
        try:
            await runtime.incoming.put(response(tool=True, transcript="stop"))
            await until(lambda: runtime.session.send_tool_response.await_count == 1)
            runtime._execute_tool.assert_not_awaited()
            runtime.interrupt.assert_called_once()
            self.assertEqual(runtime._tasks.get(task["id"])["state"], "planning")
        finally:
            await stop(receiver)

    async def test_late_gemini_tool_from_prior_turn_cannot_execute_after_external_task(self):
        runtime = Runtime()
        runtime._model_idle.clear()  # An older Gemini Live turn is still in flight.
        task = runtime._submit_task("external operation", "external")
        worker = asyncio.create_task(runtime._process_dashboard_commands())
        receiver = asyncio.create_task(runtime._receive_audio())
        try:
            await until(lambda: runtime._tasks.get(task["id"])["state"] == "succeeded")
            await runtime.incoming.put(response(tool=True, complete=True))
            await until(lambda: runtime.session.send_tool_response.await_count == 1)
            runtime._execute_tool.assert_not_awaited()
        finally:
            await stop(worker)
            await stop(receiver)

    async def test_revoked_live_credential_is_checked_before_sending_more_microphone_audio(self):
        runtime = Runtime(live=True)
        runtime._connected_voice_profile = dict(runtime._profile)
        runtime._connected_voice_credential = "synthetic-key"
        self.assertTrue(runtime._live_voice_authorized())
        runtime._providers.store.credential.return_value = None
        self.assertFalse(runtime._live_voice_authorized())
        await runtime.out_queue.put({"data": b"synthetic-pcm"})
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            await runtime._send_realtime()
        runtime.session.send_realtime_input.assert_not_awaited()

    async def test_revoked_live_credential_rejects_queued_incoming_tools_and_transcripts(self):
        runtime = Runtime(live=True)
        runtime._connected_voice_profile = dict(runtime._profile)
        runtime._connected_voice_credential = "synthetic-key"
        task = runtime._submit_task("original voice operation", "voice-owner", "voice")
        runtime._tasks.claim(task["id"])
        runtime._providers.store.credential.return_value = None
        await runtime.incoming.put(response(tool=True, complete=True, transcript="stale input", output="stale answer"))
        with patch.object(traceback, "print_exc"), self.assertRaisesRegex(RuntimeError, "revoked"):
            await asyncio.wait_for(runtime._receive_audio(), timeout=1)
        runtime._execute_tool.assert_not_awaited()
        runtime.session.send_tool_response.assert_not_awaited()
        runtime.ui.show_content.assert_not_called()
        self.assertEqual(runtime._session_log, [])
        self.assertEqual(runtime._tasks.get(task["id"])["state"], "planning")

    async def test_chat_available_without_live_requires_usable_primary_credentials(self):
        runtime = Runtime(connected=False)
        self.assertTrue(runtime._chat_available())
        runtime._providers.store.credential.return_value = None
        self.assertFalse(runtime._chat_available())
        runtime._profile["auth_type"] = "none"
        self.assertTrue(runtime._chat_available())
        runtime._providers.store.selected.return_value = None
        runtime._providers.store.selected.side_effect = None
        self.assertFalse(runtime._chat_available())

    async def test_live_tool_roles_stay_pinned_until_that_task_finishes(self):
        from core.credential_store import CredentialStore
        from core.provider_config import ProviderStore
        from tests.test_credential_store import FakeProtector
        runtime = Runtime(live=True)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            store = ProviderStore(folder, vault=CredentialStore(folder / "vault", FakeProtector()))
            for identity in ("vision-one", "vision-two"):
                store.save_profile({"id": identity, "name": identity, "provider": "openai", "model": "gpt-4.1"}, key="synthetic-test-key")
            store.set_role("vision", "vision-one")
            runtime._providers.store = store
            first = runtime._submit_task("first live task", "one")
            runtime._tasks.claim(first["id"])
            runtime._pin_task_providers(first["id"])
            store.set_role("vision", "vision-two")
            observations = []
            async def execute(call):
                observations.append((runtime._tasks.owner(), store.selected("vision")["id"]))
                return "synthetic result"
            runtime._execute_tool_for_task = execute
            runtime._pin_task_providers(first["id"])
            await scope["JarvisLive"]._execute_tool(runtime, SimpleNamespace())
            runtime._tasks.update(first["id"], "succeeded")
            runtime._tasks.release(first["id"])
            second = runtime._submit_task("second live task", "two")
            runtime._tasks.claim(second["id"])
            runtime._pin_task_providers(second["id"])
            await scope["JarvisLive"]._execute_tool(runtime, SimpleNamespace())
            self.assertEqual(observations, [(first["id"], "vision-one"), (second["id"], "vision-two")])


if __name__ == "__main__":
    unittest.main()
