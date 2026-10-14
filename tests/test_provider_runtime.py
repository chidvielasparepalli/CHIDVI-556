"""Shared turns use real temporary profile storage and synthetic adapters only."""
import asyncio
import copy
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.credential_store import CredentialStore
from core.provider_config import ProviderStore
from core.provider_runtime import ProviderRuntime, is_live_profile
from core.providers import AIResponse, ProviderError, ToolCall
from core.task_control import TaskStore
from tests.test_credential_store import FakeProtector


TOOL = {"name": "inspect", "description": "Read a fixture", "parameters": {
    "type": "OBJECT", "properties": {"path": {"type": "STRING"},
        "count": {"type": "INTEGER", "minimum": 1, "maximum": 5}}, "required": ["path"]}}


def answer(text="Ready", calls=(), usage=None, opaque=None):
    return AIResponse(text, list(calls), usage or {}, "synthetic", provider_data=opaque or {})


def call(identity="call-1", name="inspect", **arguments):
    return ToolCall(identity, name, {"path": "fixture.py", **arguments})


class FakeAdapter:
    def __init__(self, owner, profile, key):
        self.owner, self.profile, self.key = owner, copy.deepcopy(profile), key

    def capabilities(self):
        return {"text": True, "tools": self.owner.tools_enabled}

    async def request(self, messages, **options):
        self.owner.requests.append((self.profile, copy.deepcopy(messages), options))
        if not self.owner.responses:
            raise AssertionError("Unexpected provider request")
        value = self.owner.responses.pop(0)
        if callable(value):
            value = value(messages, options)
        return await value if inspect.isawaitable(value) else value


class ProviderRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.store = ProviderStore(root, vault=CredentialStore(root / "credentials.dpapi", FakeProtector()))
        self.store.save_profile({"id": "first", "name": "First", "provider": "openai", "model": "gpt-4.1"},
                                key="synthetic-first-key")
        self.store.set_role("chat", "first")
        self.requests, self.responses, self.factories, self.dispatched = [], [], [], []
        self.tools_enabled = True
        def factory(profile, key):
            self.factories.append((copy.deepcopy(profile), key))
            return FakeAdapter(self, profile, key)
        self.runtime = ProviderRuntime(self.store, factory)
        self.tasks = TaskStore()
        self.task = self.tasks.submit("Inspect fixture", "request")
        self.tasks.claim(self.task["id"])
        self.cancel = self.tasks.event(self.task["id"])

    async def dispatch(self, action):
        self.dispatched.append(action)
        return SimpleNamespace(response={"result": {"ok": True, "found": action.args["path"]}})

    async def run_turn(self, **values):
        task_id = values.pop("task_id", self.task["id"])
        text = values.pop("text", "Inspect fixture")
        options = {"system": "System", "tools": [TOOL], "dispatch": self.dispatch,
                   "cancelled": self.cancel, "valid": lambda: self.tasks.can_confirm(self.task["id"])}
        options.update(values)
        return await self.runtime.run(task_id, text, **options)

    async def test_dispatches_validated_tool_and_continues_with_normalized_result(self):
        opaque = {"provider": "openai", "model": "gpt-4.1", "items": [{"encrypted_content": "opaque"}]}
        self.responses = [answer("Checking", [call()], {"input_tokens": 3}, opaque),
                          answer("Found fixture", usage={"input_tokens": 5, "output_tokens": 2})]
        result = await self.run_turn()
        self.assertEqual(result["text"], "Found fixture")
        self.assertEqual(result["session_usage"], {"input_tokens": 8, "output_tokens": 2})
        self.assertEqual([(item.id, item.name, item.args) for item in self.dispatched],
                         [("call-1", "inspect", {"path": "fixture.py"})])
        continuation = self.requests[1][1]
        self.assertEqual(continuation[-2]["_provider"], opaque)
        self.assertEqual(continuation[-1]["tool_call_id"], "call-1")
        self.assertEqual(json.loads(continuation[-1]["content"]), {"ok": True, "found": "fixture.py"})
        self.assertEqual(self.requests[0][2]["tools"], [TOOL])
        self.assertNotIn("synthetic-first-key", self.store.path.read_text())

    def test_native_audio_gemini_profile_is_recognized_as_live(self):
        self.assertTrue(is_live_profile({"provider": "gemini", "model": "gemini-2.5-flash-native-audio-preview"}))
        self.assertFalse(is_live_profile({"provider": "openai", "model": "gemini-2.5-flash-native-audio-preview"}))
        self.assertFalse(is_live_profile({"provider": "gemini", "model": "gemini-2.5-flash"}))
        self.assertFalse(is_live_profile(None))

    async def test_rejects_unknown_or_repeated_batch_before_any_dispatch(self):
        for invalid in (call("call-2", name="unknown"), call(), call("call-2", count=True)):
            with self.subTest(invalid=invalid):
                self.responses = [answer(calls=[call(), invalid])]
                self.dispatched.clear()
                with self.assertRaises(ProviderError) as failure:
                    await self.run_turn()
                self.assertIn(failure.exception.code, {"invalid_tool", "invalid_arguments"})
                self.assertEqual(self.dispatched, [])

    async def test_schema_rejections_do_not_dispatch(self):
        arguments = ({}, {"path": "a", "unknown": 1}, {"path": 12}, {"path": "a", "count": False},
                     {"path": "a", "count": 6}, {"path": "a", "count": float("nan")})
        for value in arguments:
            with self.subTest(value=value):
                self.responses = [answer(calls=[ToolCall("c", "inspect", value)])]
                with self.assertRaises(ProviderError) as failure:
                    await self.run_turn()
                self.assertEqual(failure.exception.code, "invalid_arguments")
        self.assertEqual(self.dispatched, [])

    async def test_call_identity_cannot_repeat_in_later_round(self):
        self.responses = [answer(calls=[call()]), answer(calls=[call()])]
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn()
        self.assertEqual(failure.exception.code, "invalid_tool")
        self.assertEqual(len(self.dispatched), 1)

    async def test_unverified_tool_model_gets_no_tools_and_cannot_dispatch(self):
        self.tools_enabled = None
        self.responses = [answer(calls=[call()])]
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn()
        self.assertEqual(failure.exception.code, "unsupported_tools")
        self.assertEqual(self.requests[0][2]["tools"], [])
        self.assertIn("no verified tool capability", self.requests[0][2]["system"])
        self.assertEqual(self.dispatched, [])

    async def test_cancelled_before_request_has_no_network_or_tool_effect(self):
        self.cancel.set()
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn()
        self.assertEqual(failure.exception.code, "cancelled")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.dispatched, [])

    async def test_late_text_and_tools_are_discarded_after_task_cancellation(self):
        for response in (answer("Late text"), answer(calls=[call()])):
            with self.subTest(response=response):
                self.cancel.clear()
                def late(messages, options):
                    self.cancel.set()
                    return response
                self.responses = [late]
                with self.assertRaises(ProviderError) as failure:
                    await self.run_turn()
                self.assertEqual(failure.exception.code, "cancelled")
        self.assertEqual(self.runtime.histories, {})
        self.assertEqual(self.dispatched, [])

    async def test_late_response_after_task_owner_switch_is_discarded(self):
        def late(messages, options):
            self.tasks.release(self.task["id"])
            other = self.tasks.submit("Other task", "other")
            self.tasks.claim(other["id"])
            return answer("Old owner answer")
        self.responses = [late]
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn()
        self.assertEqual(failure.exception.code, "cancelled")
        self.assertEqual(self.runtime.histories, {})

    async def test_rotation_or_removal_during_response_never_accepts_text(self):
        for mutate in (lambda: self.store.save_profile({"id": "first"}, key="synthetic-rotated-key"),
                       lambda: self.store.remove_profile("first")):
            with self.subTest(mutate=mutate):
                self.store.save_profile({"id": "first", "name": "First", "provider": "openai", "model": "gpt-4.1"},
                                        key="synthetic-first-key")
                self.store.set_role("chat", "first")
                def late(messages, options):
                    mutate()
                    return answer("Revoked answer")
                self.responses = [late]
                with self.assertRaises(ProviderError) as failure:
                    await self.run_turn()
                self.assertEqual(failure.exception.code, "credentials_revoked")
        self.assertEqual(self.runtime.histories, {})

    async def test_endpoint_recipient_change_revokes_inflight_turn_even_with_same_key(self):
        def late(messages, options):
            self.store.save_profile({"id": "first", "provider": "compatible", "model": "gpt-4.1",
                "base_url": "https://another.example/v1", "capabilities": {"text": True, "tools": True}},
                key="synthetic-first-key")
            return answer("Old recipient")
        self.responses = [late]
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn()
        self.assertEqual(failure.exception.code, "credentials_revoked")

    async def test_streamed_chunks_after_revocation_are_not_displayed(self):
        seen = []
        async def late(messages, options):
            value = options["on_text"]("Before")
            if inspect.isawaitable(value):
                await value
            self.store.save_profile({"id": "first"}, key="synthetic-rotated-key")
            value = options["on_text"]("After")
            if inspect.isawaitable(value):
                await value
            return answer("BeforeAfter")
        self.responses = [late]
        with self.assertRaises(ProviderError):
            await self.run_turn(on_text=seen.append)
        self.assertEqual(seen, ["Before"])

    async def test_cancellation_during_tool_discards_pending_or_failed_late_result(self):
        for late in ("[CONFIRMATION_PENDING] Allow action", {"ok": False, "error": "failed"}):
            with self.subTest(late=late):
                self.cancel.clear()
                async def dispatch(action):
                    self.cancel.set()
                    return late
                self.responses = [answer(calls=[call()])]
                with self.assertRaises(ProviderError) as failure:
                    await self.run_turn(dispatch=dispatch)
                self.assertEqual(failure.exception.code, "cancelled")
        self.assertEqual(self.runtime.pending, {})
        self.assertEqual(self.runtime.histories, {})

    async def test_rotation_during_tool_cannot_start_next_tool(self):
        async def dispatch(action):
            self.dispatched.append(action)
            self.store.save_profile({"id": "first"}, key="synthetic-rotated-key")
            return {"ok": True}
        self.responses = [answer(calls=[call(), call("second")])]
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn(dispatch=dispatch)
        self.assertEqual(failure.exception.code, "credentials_revoked")
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.runtime.histories, {})

    async def test_external_cancellation_cleans_owned_provider_await(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def blocked(messages, options):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        self.responses = [blocked]
        task = asyncio.create_task(self.run_turn())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.runtime.histories, {})
        self.assertEqual(self.runtime.pending, {})

    async def test_pending_approval_resumes_original_turn_once_without_reexecuting(self):
        async def pending(action):
            self.dispatched.append(action)
            return "[CONFIRMATION_PENDING] Allow action"
        self.responses = [answer(calls=[call(), call("skipped")]), answer("Approved action completed")]
        waiting = await self.run_turn(dispatch=pending)
        self.assertTrue(waiting["waiting"])
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.runtime.pending[self.task["id"]][1]["id"], "first")
        reply = {**waiting["resume_tool"], "content": '{"ok":true}'}
        result = await self.run_turn(resume=reply)
        self.assertFalse(result["waiting"])
        self.assertEqual(len(self.dispatched), 1)
        self.assertNotIn(self.task["id"], self.runtime.pending)
        history = self.requests[-1][1]
        self.assertEqual(history[-1], reply)
        self.assertIn("Not executed", history[-2]["content"])
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn(resume=reply)
        self.assertEqual(failure.exception.code, "expired")

    async def test_wrong_approval_identity_cannot_resume_or_replace_pending_turn(self):
        async def pending(action):
            return "[CONFIRMATION_PENDING] Allow action"
        self.responses = [answer(calls=[call()])]
        waiting = await self.run_turn(dispatch=pending)
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn(resume={**waiting["resume_tool"], "tool_call_id": "another-call"})
        self.assertEqual(failure.exception.code, "invalid_resume")
        self.assertIn(self.task["id"], self.runtime.pending)
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn()
        self.assertEqual(failure.exception.code, "pending")
        self.assertEqual(len(self.requests), 1)

    async def test_key_rotation_while_pending_refuses_resume_before_network(self):
        async def pending(action):
            return "[CONFIRMATION_PENDING] Allow action"
        self.responses = [answer(calls=[call()])]
        waiting = await self.run_turn(dispatch=pending)
        self.store.save_profile({"id": "first"}, key="synthetic-rotated-key")
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn(resume=waiting["resume_tool"])
        self.assertEqual(failure.exception.code, "credentials_revoked")
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(self.task["id"], self.runtime.pending)

    async def test_cancelled_pending_task_cannot_resume_approved_result(self):
        async def pending(action):
            return "[CONFIRMATION_PENDING] Allow action"
        self.responses = [answer(calls=[call()])]
        waiting = await self.run_turn(dispatch=pending)
        self.tasks.cancel(self.task["id"])
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn(resume=waiting["resume_tool"])
        self.assertEqual(failure.exception.code, "cancelled")
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(self.task["id"], self.runtime.pending)

    async def test_provider_and_model_switch_between_tasks_keeps_histories_separate(self):
        self.responses = [answer("First answer"), answer("Second provider"), answer("New model"), answer("Back to first")]
        await self.run_turn(text="First private question")
        self.store.save_profile({"id": "second", "name": "Second", "provider": "anthropic", "model": "claude-sonnet-4-5"},
                                key="synthetic-second-key")
        self.store.set_role("chat", "second")
        await self.run_turn(task_id="second-task", text="Second private question")
        self.assertEqual(self.requests[1][1], [{"role": "user", "content": "Second private question"}])
        self.store.set_role("chat", "first")
        self.store.save_profile({"id": "first", "model": "gpt-5"})
        await self.run_turn(task_id="third-task", text="New model question")
        self.assertEqual(self.requests[2][1], [{"role": "user", "content": "New model question"}])
        self.store.save_profile({"id": "first", "model": "gpt-4.1"})
        await self.run_turn(task_id="fourth-task", text="Continue first")
        self.assertEqual(self.requests[3][1][0]["content"], "First private question")
        self.assertNotIn("Second private", json.dumps(self.requests[3][1]))
        self.assertEqual([profile["provider"] for profile, _ in self.factories], ["openai", "anthropic", "openai", "openai"])

    async def test_failed_tool_stops_without_retry_or_successful_history(self):
        self.responses = [answer(calls=[call(), call("later")])]
        async def failed(action):
            self.dispatched.append(action)
            return {"ok": False, "error": "Fixture failure"}
        result = await self.run_turn(dispatch=failed)
        self.assertTrue(result["failed"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.runtime.histories, {})

    async def test_model_change_during_task_applies_only_to_next_turn(self):
        def change_model(messages, options):
            self.store.save_profile({"id": "first", "model": "gpt-5"})
            return answer("Original model answer")
        self.responses = [change_model, answer("New model answer")]
        current = await self.run_turn()
        following = await self.run_turn(task_id="next-task")
        self.assertEqual(current["model"], "gpt-4.1")
        self.assertEqual(following["model"], "gpt-5")
        self.assertEqual(len(self.requests[1][1]), 1)

    async def test_history_bound_keeps_complete_turn_without_orphan_tool_results(self):
        self.responses = [answer("x" * 200_001)]
        await self.run_turn()
        # A large preceding answer is discarded as one whole turn after the next completion.
        self.responses = [answer("Checking", [call()]), answer("Finished")]
        await self.run_turn(task_id="next-task", text="Small question")
        history = next(iter(self.runtime.histories.values()))
        self.assertEqual(history, [{"role": "user", "content": "Small question"},
                                   {"role": "assistant", "content": "Finished"}])

    async def test_eight_round_limit_applies_across_approval_resumes(self):
        async def pending(action):
            return "[CONFIRMATION_PENDING] Allow action"
        self.responses = [answer(calls=[call(f"call-{index}")]) for index in range(9)]
        resume = None
        for _ in range(8):
            waiting = await self.run_turn(dispatch=pending, resume=resume)
            resume = waiting["resume_tool"]
        with self.assertRaises(ProviderError) as failure:
            await self.run_turn(dispatch=pending, resume=resume)
        self.assertEqual(failure.exception.code, "step_limit")
        self.assertEqual(len(self.requests), 8)


if __name__ == "__main__":
    unittest.main()
