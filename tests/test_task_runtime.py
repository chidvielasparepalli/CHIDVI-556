"""Run the actual orchestration methods without starting audio, models or a server."""
import ast
import asyncio
from contextlib import contextmanager, suppress
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core import confirm
from core.task_control import TaskStore, check_task_cancelled

tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "JarvisLive")
methods = {"_process_dashboard_commands", "_submit_task", "_finish_task", "_confirmation_result",
           "_resume_confirmed_task", "_begin_voice_task", "_voice_source_authorized",
           "_send_realtime", "_run_action_worker", "_set_cloud_vision"}
node.body = [n for n in node.body if getattr(n, "name", None) in methods]
assert {n.name for n in node.body} == methods, "Runtime harness is missing an orchestration method"
scope = {"asyncio": asyncio, "confirm_gate": confirm, "json": json,
         "types": SimpleNamespace(Blob=lambda *, data, mime_type: SimpleNamespace(data=data, mime_type=mime_type))}
exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), scope)


class Runtime(scope["JarvisLive"]):
    def __init__(self):
        self._tasks = TaskStore()
        self._task_done = asyncio.Event()
        self._task_wakeup = asyncio.Event()
        self._model_idle = asyncio.Event()
        self._model_idle.set()
        self._game_stop = threading.Event()
        self._click_speech = SimpleNamespace(stopped=threading.Event())
        self.session = SimpleNamespace(send_client_content=AsyncMock(), send_realtime_input=AsyncMock())
        self._wake_enabled = False
        self._awake = True
        self.ui = SimpleNamespace(write_log=Mock(), stop_camera_stream=Mock())
        self._loop = asyncio.get_running_loop()
        self._dashboard = SimpleNamespace(device_authorized=Mock(return_value=True))
        self._voice_turn_source = None
        self.out_queue = asyncio.Queue()
        self.interrupt = Mock()
        self.request_reconnect = Mock()
        self._allow_cloud_vision = False
        self._vision_permission_generation = 0


async def until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Condition was not reached")


@contextmanager
def confirmation_gate(runtime):
    with patch.multiple(confirm, _pending=None, _show_cb=Mock(), _hide_cb=Mock(), _log_cb=Mock(),
                        _result_cb=runtime._confirmation_result, _owner_cb=runtime._tasks.owner,
                        _valid_cb=runtime._tasks.can_confirm), patch.object(confirm.threading, "Timer"):
        yield


class TaskRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_queue_reconnect_does_not_send_duplicate(self):
        runtime = Runtime()
        task = runtime._submit_task("open browser", "request", "device")
        runtime._submit_task("open browser", "request", "device")
        worker = asyncio.create_task(runtime._process_dashboard_commands())
        try:
            await until(lambda: runtime.session.send_client_content.await_count == 1)
            runtime._finish_task()
            await until(lambda: runtime._tasks.active_id is None)
            runtime._submit_task("open browser", "request", "device")
            await asyncio.sleep(0.02)
            self.assertEqual(runtime.session.send_client_content.await_count, 1)
            self.assertEqual(runtime._tasks.get(task["id"])["state"], "succeeded")
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def test_cancelled_operation_cannot_be_replayed_or_marked_success(self):
        runtime = Runtime()
        task = runtime._submit_task("click", "one")
        worker = asyncio.create_task(runtime._process_dashboard_commands())
        try:
            await until(lambda: runtime.session.send_client_content.await_count == 1)
            runtime._tasks.cancel(task["id"])
            runtime._finish_task()
            await until(lambda: runtime._tasks.active_id is None)
            self.assertEqual(runtime._tasks.get(task["id"])["state"], "cancelled")
            self.assertEqual(runtime.session.send_client_content.await_count, 1)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def test_command_send_failure_reconnects_and_next_task_waits_for_new_session(self):
        runtime = Runtime()
        first = runtime._submit_task("first command", "first")
        second = runtime._submit_task("next command", "next")
        failed_session = runtime.session
        failed_session.send_client_content.side_effect = RuntimeError("connection closed")
        worker = asyncio.create_task(runtime._process_dashboard_commands())
        try:
            await until(lambda: runtime.request_reconnect.call_count == 1)
            runtime.request_reconnect.assert_called_once_with(keep_context=False, reason="command delivery failed")
            self.assertEqual(runtime._tasks.get(first["id"])["state"], "failed")
            self.assertEqual(runtime._tasks.get(second["id"])["state"], "queued")
            self.assertIsNone(runtime._tasks.active_id)
            self.assertFalse(runtime._model_idle.is_set())
            failed_session.send_client_content.assert_awaited_once()
            runtime.session = SimpleNamespace(send_client_content=AsyncMock())
            runtime._model_idle.set()
            runtime._task_wakeup.set()
            await until(lambda: runtime.session.send_client_content.await_count == 1)
            sent = runtime.session.send_client_content.await_args.kwargs["turns"]["parts"][0]["text"]
            self.assertEqual(sent, "next command")
            self.assertEqual(runtime._tasks.active_id, second["id"])
            failed_session.send_client_content.assert_awaited_once()
            runtime._finish_task()
            await until(lambda: runtime._tasks.active_id is None)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def test_late_confirmation_is_bound_to_original_task(self):
        runtime = Runtime()
        first = runtime._submit_task("one", "one")
        second = runtime._submit_task("two", "two")
        runtime._tasks.claim()
        runtime._tasks.cancel(first["id"])
        runtime._tasks.release(first["id"])
        runtime._tasks.claim()
        with patch.object(runtime, "_resume_confirmed_task", wraps=runtime._resume_confirmed_task) as resume:
            runtime._confirmation_result(True, "Done", first["id"])
            await until(lambda: resume.await_count == 1)
        self.assertEqual(runtime._tasks.get(first["id"])["state"], "cancelled")
        self.assertEqual(runtime._tasks.get(second["id"])["state"], "planning")
        self.assertFalse(runtime._task_done.is_set())
        runtime.session.send_client_content.assert_not_awaited()

    async def test_confirmed_action_waits_for_idle_then_requests_verification_once(self):
        runtime = Runtime()
        task = runtime._submit_task("remove the selected file", "confirm-one")
        runtime._tasks.claim(task["id"])
        runtime._tasks.update(task["id"], "waiting_for_user_input")
        runtime._model_idle.clear()
        action = Mock(return_value="Selected file removed")
        with confirmation_gate(runtime), patch.object(runtime._model_idle, "wait",
                wraps=runtime._model_idle.wait) as idle_wait:
            self.assertIn("CONFIRMATION_PENDING", confirm.request("remove", "Remove file", "Selected file", action))
            confirm.resolve(True)
            await until(lambda: idle_wait.await_count == 1)
            action.assert_called_once()
            runtime.session.send_client_content.assert_not_awaited()
            runtime._finish_task()
            self.assertEqual(runtime._tasks.get(task["id"])["state"], "waiting_for_user_input")
            self.assertFalse(runtime._task_done.is_set())
            runtime._model_idle.set()
            await until(lambda: runtime.session.send_client_content.await_count == 1)
        message = runtime.session.send_client_content.await_args.kwargs
        self.assertTrue(message["turn_complete"])
        text = message["turns"]["parts"][0]["text"]
        self.assertIn("[APPROVED_ACTION_RESULT]", text)
        self.assertIn("Do not repeat that action", text)
        self.assertIn("Selected file removed", text)
        self.assertEqual(runtime._tasks.get(task["id"])["state"], "verifying")
        self.assertFalse(runtime._model_idle.is_set())
        self.assertFalse(runtime._task_done.is_set())
        action.assert_called_once()
        runtime._finish_task(detail="Verified the file is absent")
        self.assertEqual(runtime._tasks.get(task["id"])["state"], "succeeded")

    async def test_confirmation_returned_failure_finishes_failed_without_verification(self):
        for result in ({"ok": False, "error": "offline"}, "Android control failed: offline"):
            with self.subTest(result=result):
                runtime = Runtime()
                task = runtime._submit_task("perform action", "failed-action")
                runtime._tasks.claim(task["id"])
                runtime._tasks.update(task["id"], "waiting_for_user_input")
                runtime._model_idle.clear()
                action = Mock(return_value=result)
                with confirmation_gate(runtime):
                    confirm.request("action", "Action", "Test action", action)
                    confirm.resolve(True)
                    await until(lambda: runtime._tasks.get(task["id"])["state"] == "failed")
                action.assert_called_once()
                self.assertTrue(runtime._task_done.is_set())
                self.assertTrue(runtime._tasks.event(task["id"]).is_set())
                runtime.session.send_client_content.assert_not_awaited()
                runtime._finish_task()
                self.assertEqual(runtime._tasks.get(task["id"])["state"], "failed")

    async def test_confirmation_waiting_for_idle_rechecks_original_owner(self):
        runtime = Runtime()
        first = runtime._submit_task("one", "one")
        second = runtime._submit_task("two", "two")
        runtime._tasks.claim(first["id"])
        runtime._tasks.update(first["id"], "waiting_for_user_input")
        runtime._model_idle.clear()
        with patch.object(runtime._model_idle, "wait", wraps=runtime._model_idle.wait) as idle_wait:
            resume = asyncio.create_task(runtime._resume_confirmed_task(True, "Done", first["id"]))
            try:
                await until(lambda: idle_wait.await_count == 1)
                runtime._tasks.cancel(first["id"])
                runtime._tasks.release(first["id"])
                runtime._tasks.claim(second["id"])
                runtime._model_idle.set()
                await asyncio.wait_for(resume, 1)
            finally:
                resume.cancel()
                with suppress(asyncio.CancelledError):
                    await resume
        self.assertEqual(runtime._tasks.get(first["id"])["state"], "cancelled")
        self.assertEqual(runtime._tasks.get(second["id"])["state"], "planning")
        self.assertFalse(runtime._task_done.is_set())
        runtime.session.send_client_content.assert_not_awaited()

    async def test_phone_voice_claims_exact_task_and_is_not_sent_twice(self):
        runtime = Runtime()
        older = runtime._submit_task("older typed command", "older")
        runtime._voice_turn_source = "paired-phone"
        runtime._game_stop.set()
        runtime._click_speech.stopped.set()
        runtime._begin_voice_task("recognized phone command")
        voice = runtime._tasks.get(runtime._tasks.active_id)
        self.assertNotEqual(voice["id"], older["id"])
        self.assertEqual(voice["source"], "paired-phone")
        self.assertEqual(voice["text"], "recognized phone command")
        self.assertTrue(voice["input_delivered"])
        self.assertEqual(runtime._tasks.get(older["id"])["state"], "queued")
        self.assertFalse(runtime._game_stop.is_set())
        self.assertFalse(runtime._click_speech.stopped.is_set())
        with patch.object(runtime._task_done, "wait", wraps=runtime._task_done.wait) as task_wait:
            worker = asyncio.create_task(runtime._process_dashboard_commands())
            try:
                await until(lambda: task_wait.await_count == 1)
                runtime.session.send_client_content.assert_not_awaited()
                self.assertEqual(runtime._tasks.active_id, voice["id"])
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker

    async def test_expired_or_revoked_voice_interrupts_without_creating_task(self):
        for source in ("expired-phone", "revoked-phone"):
            with self.subTest(source=source):
                runtime = Runtime()
                runtime._voice_turn_source = source
                runtime._dashboard.device_authorized.return_value = False
                runtime._begin_voice_task("stale recognized command")
                runtime._dashboard.device_authorized.assert_called_once_with(source)
                runtime.interrupt.assert_called_once_with()
                self.assertEqual(runtime._tasks.snapshot(), [])
                self.assertIsNone(runtime._tasks.active_id)

    async def test_realtime_strips_device_identity_and_drops_revoked_frames(self):
        runtime = Runtime()
        authorized = {"paired-phone"}
        runtime._dashboard.device_authorized.side_effect = lambda source: source in authorized
        revoked_frame = {"device_id": "revoked-phone", "data": b"revoked", "mime_type": "audio/pcm"}
        valid_frame = {"device_id": "paired-phone", "data": b"valid", "mime_type": "audio/pcm;rate=16000"}
        await runtime.out_queue.put(revoked_frame)
        await runtime.out_queue.put(valid_frame)
        with patch.object(scope["types"], "Blob", wraps=scope["types"].Blob) as blob:
            sender = asyncio.create_task(runtime._send_realtime())
            try:
                await until(lambda: runtime.session.send_realtime_input.await_count == 1)
                blob.assert_called_once_with(data=b"valid", mime_type="audio/pcm;rate=16000")
                audio = runtime.session.send_realtime_input.await_args.kwargs["audio"]
                self.assertEqual(vars(audio), {"data": b"valid", "mime_type": "audio/pcm;rate=16000"})
                self.assertEqual(runtime._voice_turn_source, "paired-phone")
                authorized.clear()
                await runtime.out_queue.put(valid_frame)
                await until(runtime.out_queue.empty)
                self.assertEqual(runtime.session.send_realtime_input.await_count, 1)
                self.assertEqual(blob.call_count, 1)
            finally:
                sender.cancel()
                with suppress(asyncio.CancelledError):
                    await sender

    async def test_realtime_only_accepts_current_owner_microphone(self):
        for owner, allowed, rejected in (("paired-phone", "paired-phone", ("other-phone", "voice")),
                                         ("desktop", "voice", ("paired-phone",))):
            with self.subTest(owner=owner):
                runtime = Runtime()
                task = runtime._submit_task("active command", "active", owner)
                runtime._tasks.claim(task["id"])
                for source in rejected:
                    await runtime.out_queue.put({"device_id": source, "data": b"wrong owner"})
                await runtime.out_queue.put({"device_id": allowed, "data": b"owner"})
                sender = asyncio.create_task(runtime._send_realtime())
                try:
                    await until(lambda: runtime.session.send_realtime_input.await_count == 1)
                    self.assertTrue(runtime.out_queue.empty())
                    audio = runtime.session.send_realtime_input.await_args.kwargs["audio"]
                    self.assertEqual(audio.data, b"owner")
                    self.assertEqual(runtime._voice_turn_source, allowed)
                    runtime.request_reconnect.assert_not_called()
                finally:
                    sender.cancel()
                    with suppress(asyncio.CancelledError):
                        await sender

    async def test_realtime_discards_audio_during_assistant_or_click_speech(self):
        for flag in ("_is_speaking", "_feedback_speaking"):
            with self.subTest(flag=flag):
                runtime = Runtime()
                setattr(runtime, flag, True)
                await runtime.out_queue.put({"data": b"assistant speech"})
                sender = asyncio.create_task(runtime._send_realtime())
                try:
                    await until(runtime.out_queue.empty)
                    runtime.session.send_realtime_input.assert_not_awaited()
                    self.assertIsNone(runtime._voice_turn_source)
                    setattr(runtime, flag, False)
                    await runtime.out_queue.put({"data": b"user speech"})
                    await until(lambda: runtime.session.send_realtime_input.await_count == 1)
                    audio = runtime.session.send_realtime_input.await_args.kwargs["audio"]
                    self.assertEqual(audio.data, b"user speech")
                finally:
                    sender.cancel()
                    with suppress(asyncio.CancelledError):
                        await sender

    async def test_cancelled_action_worker_drains_and_cancels_origin_after_task_switch(self):
        runtime = Runtime()
        first = runtime._submit_task("original action", "original")
        second = runtime._submit_task("next action", "next")
        runtime._tasks.claim(first["id"])
        started, release, finished, saw_cancellation = (threading.Event() for _ in range(4))
        observed_owners = []

        def action():
            observed_owners.append(runtime._tasks.owner())
            started.set()
            try:
                if not release.wait(3):
                    raise AssertionError("Test did not release the action worker")
                try:
                    check_task_cancelled()
                except RuntimeError:
                    saw_cancellation.set()
                    raise
            finally:
                finished.set()

        with runtime._tasks.context(first["id"]):
            worker = asyncio.create_task(runtime._run_action_worker(action))
        try:
            await until(started.is_set)
            runtime._tasks.release(first["id"])
            runtime._tasks.claim(second["id"])
            worker.cancel()
            await until(lambda: runtime._tasks.event(first["id"]).is_set())
            self.assertFalse(worker.done(), "Coroutine must remain alive until the action thread drains")
            self.assertFalse(finished.is_set())
            self.assertEqual(runtime._tasks.get(first["id"])["state"], "cancelled")
            self.assertEqual(runtime._tasks.get(second["id"])["state"], "planning")
            self.assertFalse(runtime._tasks.event(second["id"]).is_set())
            self.assertTrue(runtime._game_stop.is_set())
            self.assertTrue(runtime._click_speech.stopped.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 1)
            self.assertTrue(finished.is_set())
            self.assertTrue(saw_cancellation.is_set())
            self.assertEqual(observed_owners, [first["id"]])
        finally:
            release.set()
            with suppress(asyncio.CancelledError):
                await worker

    async def test_cloud_vision_disable_cancels_active_analysis_and_resets_notice(self):
        runtime = Runtime()
        task = runtime._submit_task("look at screen", "vision")
        runtime._tasks.claim(task["id"])
        runtime._cancel_task = Mock()
        runtime._vision_notices = set()
        service = SimpleNamespace(cloud_vision_enabled=False)
        with patch("core.desktop_control.get_service", return_value=service):
            runtime._set_cloud_vision(True)
            self.assertTrue(runtime._allow_cloud_vision)
            self.assertTrue(service.cloud_vision_enabled)
            capture_generation = runtime._vision_permission_generation
            runtime._vision_notices.add(("backend_unavailable", capture_generation))
            runtime._vision_busy = True
            runtime._set_cloud_vision(False)
            await asyncio.sleep(0)
            self.assertFalse(runtime._allow_cloud_vision)
            self.assertFalse(service.cloud_vision_enabled)
            self.assertFalse(runtime._vision_busy)
            self.assertFalse(runtime._vision_notices)
            runtime._cancel_task.assert_called_once_with(task["id"])
            self.assertGreater(runtime._vision_permission_generation, capture_generation)
            runtime.ui.stop_camera_stream.assert_called_once_with()
            runtime._set_cloud_vision(True)
            self.assertGreater(runtime._vision_permission_generation, capture_generation)
            runtime._cancel_task.assert_called_once_with(task["id"])

    async def test_repeated_cancellation_cannot_exit_before_action_thread_drains(self):
        runtime = Runtime()
        task = runtime._submit_task("blocking action", "repeated-cancel")
        runtime._tasks.claim(task["id"])
        started, release, finished = (threading.Event() for _ in range(3))

        def action():
            started.set()
            release.wait(3)
            finished.set()

        with runtime._tasks.context(task["id"]):
            worker = asyncio.create_task(runtime._run_action_worker(action))
        try:
            await until(started.is_set)
            worker.cancel()
            await until(lambda: runtime._tasks.event(task["id"]).is_set())
            worker.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(worker.done(), "Repeated cancellation must not abandon the running action thread")
            self.assertFalse(finished.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 1)
            self.assertTrue(finished.is_set())
        finally:
            release.set()
            with suppress(asyncio.CancelledError):
                await worker
            await until(finished.is_set)


if __name__ == "__main__":
    unittest.main()
