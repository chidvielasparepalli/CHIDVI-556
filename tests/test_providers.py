"""Provider contracts use synthetic data and httpx.MockTransport only."""
import asyncio
import importlib
import io
import json
import threading
import unittest
from datetime import datetime, timezone

import httpx
from PIL import Image
from unittest.mock import patch


def sse(*events):
    return "".join("data: " + (event if isinstance(event, str) else json.dumps(event)) + "\n\n" for event in events).encode()


def chat_stream(text="Ready", calls=None):
    return sse({"model": "synthetic", "choices": [{"index": 0, "delta": {"content": text,
        **({"tool_calls": calls} if calls else {})}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if calls else "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}, "[DONE]")


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self):
        self.p = importlib.import_module("core.providers")

    def test_registered_protocols_and_safe_endpoints(self):
        self.assertEqual(set(self.p.PROVIDERS), {"gemini", "openai", "anthropic", "xai", "deepseek", "nvidia", "opencode", "compatible"})
        self.assertEqual(self.p.default_base_url("opencode"), "https://opencode.ai/inference/v1")
        for provider in self.p.PROVIDERS:
            if provider != "compatible":
                with self.subTest(provider=provider), self.assertRaises(ValueError):
                    self.p.validate_base_url(provider, "https://unrelated.example/v1")
        for url in ("http://public.example/v1", "https://key@api.example/v1", "https://api.example/v1?key=secret", "https://api.example/v1#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.p.validate_base_url("compatible", url)
        self.assertEqual(self.p.validate_base_url("compatible", "http://127.0.0.1:11434/v1/"), "http://127.0.0.1:11434/v1")

    def test_unknown_models_do_not_inherit_tools_vision_or_audio(self):
        caps = self.p.model_capabilities("compatible", "unknown")
        self.assertIsNone(caps["vision"])
        self.assertIsNone(caps["tools"])
        self.assertFalse(caps["live_audio"])
        self.assertTrue(self.p.model_capabilities("gemini", "gemini-3.1-flash-live-preview")["live_audio"])
        self.assertFalse(self.p.model_capabilities("deepseek", "deepseek-chat")["vision"])
        with self.assertRaises(ValueError):
            self.p.validate_profile({"provider": "openai", "model": "../not-a-model"})


class ProviderProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.p = importlib.import_module("core.providers")
        self.requests = []

    def adapter(self, provider, model, response, **profile):
        def handle(request):
            self.requests.append(request)
            return response(request) if callable(response) else httpx.Response(200,
                headers={"content-type": "text/event-stream"}, content=response)
        return self.p.create_adapter({"provider": provider, "model": model, **profile}, "synthetic-private-key",
                                     transport=httpx.MockTransport(handle))

    async def test_chat_stream_assembles_fragmented_tool_arguments(self):
        body = sse(
            {"choices": [{"delta": {"content": "Checking", "tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "inspect", "arguments": '{"path":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"README.md"}'}}]}, "finish_reason": "tool_calls"}]}, "[DONE]")
        adapter = self.adapter("deepseek", "deepseek-chat", body)
        seen = []
        result = await adapter.request([{"role": "user", "content": "Inspect"}], "System", tools=[{
            "name": "inspect", "description": "Inspect a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}], on_text=seen.append)
        self.assertEqual(result.text, "Checking")
        self.assertEqual(result.tool_calls[0].arguments, {"path": "README.md"})
        self.assertEqual(seen, ["Checking"])
        self.assertEqual(self.requests[0].url.path, "/v1/chat/completions")
        self.assertEqual(self.requests[0].headers["authorization"], "Bearer synthetic-private-key")

    async def test_anthropic_native_blocks_and_tool_result_history(self):
        body = sse({"type": "message_start", "message": {"model": "claude-sonnet-4-5", "usage": {"input_tokens": 12}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool_1", "name": "inspect", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"x":1}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 8}}, {"type": "message_stop"})
        adapter = self.adapter("anthropic", "claude-sonnet-4-5", body)
        result = await adapter.request([{"role": "user", "content": "Use inspect"}], "System")
        self.assertEqual(result.tool_calls[0].arguments, {"x": 1})
        await adapter.request([{"role": "user", "content": "Use inspect"}, result.as_message(),
                               {"role": "tool", "tool_call_id": "tool_1", "content": '{"ok":true}'}], "System")
        payload = json.loads(self.requests[-1].content)
        self.assertEqual(payload["system"], "System")
        self.assertEqual(payload["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertEqual(payload["messages"][-1]["content"][0]["tool_use_id"], "tool_1")
        self.assertEqual(self.requests[0].headers["x-api-key"], "synthetic-private-key")
        self.assertNotIn("authorization", self.requests[0].headers)

    async def test_gemini_native_signature_round_trip_and_usage(self):
        body = sse({"candidates": [{"content": {"parts": [{"functionCall": {"id": "g1", "name": "inspect", "args": {"x": 1}},
                                                                 "thoughtSignature": "opaque-signature"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2, "totalTokenCount": 6}})
        adapter = self.adapter("gemini", "gemini-2.5-flash", body)
        result = await adapter.request([{"role": "user", "content": "Inspect"}])
        self.assertEqual(result.usage["total_tokens"], 6)
        await adapter.request([{"role": "user", "content": "Inspect"}, result.as_message(),
                               {"role": "tool", "tool_call_id": "g1", "content": '{"ok":true}'}])
        payload = json.loads(self.requests[-1].content)
        self.assertEqual(payload["contents"][1]["parts"][0]["thoughtSignature"], "opaque-signature")
        self.assertEqual(payload["contents"][2]["parts"][0]["functionResponse"]["name"], "inspect")
        self.assertEqual(self.requests[0].url.path, "/v1beta/models/gemini-2.5-flash:streamGenerateContent")
        self.assertNotIn("synthetic-private-key", str(self.requests[0].url))

    async def test_gemini_uses_json_schema_field_for_shared_tool_constraints(self):
        body = sse({"candidates": [{"content": {"parts": [{"text": "Ready"}]}, "finishReason": "STOP"}]})
        adapter = self.adapter("gemini", "gemini-2.5-flash", body)
        schema = {"type": "OBJECT", "properties": {"number": {"type": "INTEGER", "minimum": 1}},
                  "required": ["number"], "additionalProperties": False}
        await adapter.request([{"role": "user", "content": "Inspect"}], tools=[{
            "name": "inspect", "description": "Inspect a number", "parameters": schema}])
        declaration = json.loads(self.requests[-1].content)["tools"][0]["functionDeclarations"][0]
        self.assertNotIn("parameters", declaration)
        self.assertEqual(declaration["parametersJsonSchema"], {
            "type": "object", "properties": {"number": {"type": "integer", "minimum": 1}},
            "required": ["number"], "additionalProperties": False})

    async def test_openai_responses_preserves_opaque_continuation(self):
        output = [{"type": "reasoning", "id": "r1", "encrypted_content": "opaque", "summary": []},
                  {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "inspect", "arguments": '{"x":1}'}]
        body = sse({"type": "response.output_item.added", "output_index": 1, "item": {**output[1], "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc1", "delta": '{"x":'},
            {"type": "response.function_call_arguments.delta", "item_id": "fc1", "delta": '1}'},
            {"type": "response.completed", "response": {"model": "gpt-4.1", "output": output, "usage": {"input_tokens": 3, "output_tokens": 5}}})
        adapter = self.adapter("openai", "gpt-4.1", body)
        result = await adapter.request([{"role": "user", "content": "Inspect"}])
        self.assertEqual(result.tool_calls[0].id, "call1")
        await adapter.request([{"role": "user", "content": "Inspect"}, result.as_message(),
                               {"role": "tool", "tool_call_id": "call1", "content": '{"ok":true}'}])
        payload = json.loads(self.requests[-1].content)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["input"][1]["encrypted_content"], "opaque")
        self.assertEqual(payload["input"][-1]["type"], "function_call_output")
        self.assertEqual(self.requests[0].url.path, "/v1/responses")

    async def test_unknown_tools_and_vision_refused_before_network(self):
        adapter = self.adapter("compatible", "manual", chat_stream(), base_url="http://localhost:1234/v1")
        with self.assertRaises(self.p.ProviderError) as failure:
            await adapter.request([{"role": "user", "content": "Inspect"}], tools=[{"name": "inspect", "parameters": {"type": "object"}}])
        self.assertEqual(failure.exception.code, "unsupported_capability")
        with self.assertRaises(self.p.ProviderError):
            await adapter.request([{"role": "user", "content": "Look"}], images=[{"data": b"synthetic", "mime_type": "image/png"}])
        self.assertEqual(self.requests, [])

    async def test_auth_quota_errors_are_safe_and_not_retried(self):
        for status, code in ((401, "authentication_failed"), (429, "rate_limited"), (503, "provider_unavailable")):
            with self.subTest(status=status):
                adapter = self.adapter("deepseek", "deepseek-chat", lambda _: httpx.Response(status,
                    headers={"retry-after": "7"}, json={"error": "synthetic-private-key private prompt"}))
                with self.assertRaises(self.p.ProviderError) as failure:
                    await adapter.request([{"role": "user", "content": "Hi"}])
                self.assertEqual(failure.exception.code, code)
                self.assertNotIn("private", str(failure.exception))
                if status == 429:
                    self.assertEqual(failure.exception.retry_after, 7)
        self.assertEqual(len(self.requests), 3)

    async def test_truncated_or_malformed_tool_stream_is_not_executable(self):
        for body in (sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "inspect", "arguments": '{"x":'}}]}}]}),
                     chat_stream(calls=[{"index": 0, "id": "c", "function": {"name": "inspect", "arguments": "[]"}}])):
            adapter = self.adapter("deepseek", "deepseek-chat", body)
            with self.assertRaises(self.p.ProviderError):
                await adapter.request([{"role": "user", "content": "Hi"}])

    async def test_pending_stream_cancellation_and_timeout_close_owned_response(self):
        for mode in ("event", "task", "timeout"):
            with self.subTest(mode=mode):
                entered, closed = asyncio.Event(), asyncio.Event()
                cancel = threading.Event()
                class Stream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        entered.set()
                        await asyncio.Event().wait()
                        yield b"never"
                    async def aclose(self):
                        closed.set()
                adapter = self.adapter("deepseek", "deepseek-chat", lambda _: httpx.Response(200,
                    headers={"content-type": "text/event-stream"}, stream=Stream()))
                task = asyncio.create_task(adapter.request([{"role": "user", "content": "Hi"}],
                    cancel_event=cancel, timeout=.1 if mode == "timeout" else 2))
                await asyncio.wait_for(entered.wait(), 1)
                if mode == "task":
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    if mode == "event":
                        cancel.set()
                    with self.assertRaises(self.p.ProviderError) as failure:
                        await asyncio.wait_for(task, 1)
                    self.assertEqual(failure.exception.code, "cancelled" if mode == "event" else "timeout")
                self.assertTrue(closed.is_set())

    async def test_sse_multibyte_chunks_comments_and_multiline_data(self):
        raw = (': heartbeat\r\ndata: {"choices":[\r\ndata: {"delta":{"content":"Héllo"},"finish_reason":"stop"}]}\r\n\r\ndata: [DONE]\r\n\r\n').encode()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for byte in raw:
                    yield bytes((byte,))
        adapter = self.adapter("deepseek", "deepseek-chat", lambda _: httpx.Response(200,
            headers={"content-type": "text/event-stream"}, stream=Stream()))
        result = await adapter.request([{"role": "user", "content": "Hi"}])
        self.assertEqual(result.text, "Héllo")

    async def test_response_bounds_and_redirect_do_not_leak_keys_or_retry(self):
        adapter = self.adapter("deepseek", "deepseek-chat", chat_stream("x" * 2048))
        with patch.object(self.p, "MAX_RESPONSE_BYTES", 512), self.assertRaises(self.p.ProviderError) as failure:
            await adapter.request([{"role": "user", "content": "Hi"}])
        self.assertEqual(failure.exception.code, "response_too_large")
        adapter = self.adapter("deepseek", "deepseek-chat", lambda _: httpx.Response(307,
            headers={"location": "https://unrelated.example/steal"}))
        with self.assertRaises(self.p.ProviderError) as failure:
            await adapter.request([{"role": "user", "content": "Hi"}])
        self.assertEqual(failure.exception.code, "redirect_refused")
        self.assertEqual(len(self.requests), 2)

    async def test_models_can_be_fetched_before_selection_without_claiming_capabilities(self):
        adapter = self.adapter("openai", "", lambda _: httpx.Response(200,
            json={"data": [{"id": "gpt-4.1"}, {"id": "future-unknown"}]}))
        models = await adapter.models()
        self.assertEqual([item["id"] for item in models], ["gpt-4.1", "future-unknown"])
        self.assertIsNone(models[1]["capabilities"]["vision"])
        self.assertIsNone(models[0]["pricing"])
        self.assertEqual(models[0]["provider"], "openai")
        self.assertEqual(models[0]["input_modalities"], ["text", "image"])
        self.assertEqual(models[0]["output_modalities"], ["text"])
        self.assertIsNone(models[1]["input_modalities"])
        self.assertIsNone(models[1]["output_modalities"])
        self.assertEqual(models[0]["catalogue_checked_at"], datetime.now(timezone.utc).date().isoformat())
        self.assertIsNone(models[0]["pricing_checked_at"])
        self.assertEqual(self.requests[0].method, "GET")

    async def test_catalogue_distinguishes_native_audio_tts_and_embeddings(self):
        names = ["gemini-2.5-flash-native-audio-preview", "gemini-2.5-flash-preview-tts", "gemini-embedding-001"]
        adapter = self.adapter("gemini", "", lambda _: httpx.Response(200,
            json={"models": [{"name": "models/" + name} for name in names]}))
        rows = await adapter.models()
        self.assertEqual([(row["input_modalities"], row["output_modalities"]) for row in rows],
                         [(["text", "image", "audio"], ["audio"]), (["text"], ["audio"]), (["text"], ["embedding"])])
        self.assertTrue(all(row["provider"] == "gemini" and row["pricing_checked_at"] is None for row in rows))

    async def test_opencode_model_protocol_is_not_assumed_chat_compatible(self):
        cases = (
            ("gpt-4.1", "/openai/v1/responses", sse({"type": "response.completed", "response": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Hi"}]}]}})),
            ("claude-sonnet-4-5", "/anthropic/v1/messages", sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "Hi"}}, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"})),
            ("gemini-3-flash", "/google/v1beta/models/gemini-3-flash:streamGenerateContent", sse({"candidates": [{"content": {"parts": [{"text": "Hi"}]}, "finishReason": "STOP"}]})),
            ("glm-5", "/openai/v1/chat/completions", chat_stream("Hi")))
        for model, endpoint, response in cases:
            with self.subTest(model=model):
                adapter = self.adapter("opencode", model, response)
                result = await adapter.request([{"role": "user", "content": "Hi"}])
                self.assertEqual(result.text, "Hi")
                self.assertEqual(self.requests[-1].url.path, "/inference" + endpoint)
                self.assertEqual(self.requests[-1].headers["authorization"], "Bearer synthetic-private-key")

    async def test_native_image_content_is_exact_and_does_not_fetch_urls(self):
        image = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(image, "PNG")
        cases = (("gemini", "gemini-2.5-flash", sse({"candidates": [{"content": {"parts": [{"text": "red"}]}, "finishReason": "STOP"}]})),
                 ("anthropic", "claude-sonnet-4-5", sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "red"}}, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"})),
                 ("openai", "gpt-4.1", sse({"type": "response.completed", "response": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "red"}]}]}})))
        import base64
        for provider, model, response in cases:
            with self.subTest(provider=provider):
                adapter = self.adapter(provider, model, response)
                await adapter.request([{"role": "user", "content": "Color?"}], images=[{"data": image.getvalue(), "mime_type": "image/png"}])
                self.assertIn(base64.b64encode(image.getvalue()), self.requests[-1].content)
                count = len(self.requests)
                with self.assertRaises(self.p.ProviderError):
                    await adapter.request([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://private.example/image"}}]}])
                self.assertEqual(len(self.requests), count)

    async def test_explicit_capability_probes_never_execute_tools_or_infer_other_support(self):
        adapter = self.adapter("compatible", "manual", chat_stream("READY"), base_url="https://example.test/v1")
        text = await adapter.test()
        self.assertEqual(text["checked_capabilities"], {"text": True})
        self.assertIsNone(adapter.capabilities()["tools"])
        tool_adapter = self.adapter("compatible", "manual", chat_stream(calls=[{"index": 0, "id": "c", "function": {
            "name": "capability_probe", "arguments": '{"value":"READY"}'}}]), base_url="https://example.test/v1")
        tools = await tool_adapter.test(capability="tools")
        self.assertEqual(tools["checked_capabilities"], {"tools": True})
        payload = json.loads(self.requests[-1].content)
        self.assertEqual(payload["tools"][0]["function"]["name"], "capability_probe")
        vision_adapter = self.adapter("compatible", "manual", chat_stream("red"), base_url="https://example.test/v1")
        vision = await vision_adapter.test(capability="vision")
        self.assertTrue(vision["ok"])
        self.assertEqual(vision["checked_capabilities"], {"vision": True})
        self.assertIn("data:image/png;base64,", self.requests[-1].content.decode())

    async def test_declared_non_streaming_endpoint_uses_complete_json_response(self):
        adapter = self.adapter("compatible", "manual", lambda _: httpx.Response(200, json={
            "choices": [{"message": {"content": "READY"}, "finish_reason": "stop"}]}),
            base_url="https://example.test/v1", capabilities={"text": True, "streaming": False})
        seen = []
        result = await adapter.request([{"role": "user", "content": "Hi"}], on_text=seen.append)
        self.assertFalse(json.loads(self.requests[-1].content)["stream"])
        self.assertEqual(result.text, "READY")
        self.assertEqual(seen, ["READY"])

    async def test_success_status_with_error_payload_does_not_become_empty_success(self):
        adapter = self.adapter("openai", "gpt-4.1", lambda _: httpx.Response(200,
            json={"error": {"message": "synthetic-private-key private request"}}))
        with self.assertRaises(self.p.ProviderError) as failure:
            await adapter.request([{"role": "user", "content": "Hi"}])
        self.assertNotIn("private", str(failure.exception))

    async def test_cancellation_after_partial_text_cannot_return_late_tool_call(self):
        cancel = threading.Event()
        body = chat_stream("Checking", [{"index": 0, "id": "c", "function": {"name": "inspect", "arguments": "{}"}}])
        adapter = self.adapter("deepseek", "deepseek-chat", body)
        with self.assertRaises(self.p.ProviderError) as failure:
            await adapter.request([{"role": "user", "content": "Hi"}], cancel_event=cancel,
                                  on_text=lambda _: cancel.set())
        self.assertEqual(failure.exception.code, "cancelled")

    async def test_model_discovery_follows_only_bounded_provider_cursors(self):
        def handle(request):
            if "pageToken" not in request.url.params:
                return httpx.Response(200, json={"models": [{"name": "models/gemini-2.5-flash", "inputTokenLimit": 100}], "nextPageToken": "page2"})
            self.assertEqual(request.url.params["pageToken"], "page2")
            return httpx.Response(200, json={"models": [{"name": "models/gemini-2.5-pro"}]})
        adapter = self.adapter("gemini", "", handle)
        rows = await adapter.models()
        self.assertEqual([row["id"] for row in rows], ["gemini-2.5-flash", "gemini-2.5-pro"])
        self.assertEqual(rows[0]["limits"], {"input_tokens": 100})
        self.assertTrue(all(request.url.host == "generativelanguage.googleapis.com" for request in self.requests))

    async def test_non_streaming_chat_keeps_parallel_tool_calls_separate(self):
        calls = [{"id": identity, "type": "function", "function": {"name": "inspect", "arguments": '{"x":1}'}}
                 for identity in ("first", "second")]
        adapter = self.adapter("deepseek", "deepseek-chat", lambda _: httpx.Response(200, json={
            "choices": [{"message": {"content": "", "tool_calls": calls}, "finish_reason": "tool_calls"}]}))
        result = await adapter.request([{"role": "user", "content": "Inspect"}])
        self.assertEqual([call.id for call in result.tool_calls], ["first", "second"])

    async def test_responses_accepts_plain_text_assistant_history(self):
        adapter = self.adapter("openai", "gpt-4.1", sse({"type": "response.completed", "response": {"output": []}}))
        await adapter.request([{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"},
                               {"role": "user", "content": "Continue"}])
        self.assertEqual(json.loads(self.requests[-1].content)["input"][1], {"role": "assistant", "content": "Hello"})

    async def test_non_finite_tool_arguments_are_rejected(self):
        adapter = self.adapter("deepseek", "deepseek-chat", chat_stream(calls=[{"index": 0, "id": "c", "function": {
            "name": "inspect", "arguments": '{"x":NaN}'}}]))
        with self.assertRaises(self.p.ProviderError):
            await adapter.request([{"role": "user", "content": "Inspect"}])

    async def test_malformed_nested_remote_shapes_use_safe_errors(self):
        for value in ({"choices": [1]}, {"choices": [{"delta": ["private-detail"]}]}):
            with self.subTest(value=value):
                adapter = self.adapter("deepseek", "deepseek-chat", sse(value, "[DONE]"))
                with self.assertRaises(self.p.ProviderError) as failure:
                    await adapter.request([{"role": "user", "content": "Inspect"}])
                self.assertEqual(failure.exception.code, "malformed_response")
                self.assertNotIn("private-detail", str(failure.exception))

    async def test_blocked_finish_reasons_never_return_executable_tools(self):
        call = {"index": 0, "id": "call", "function": {"name": "inspect", "arguments": "{}"}}
        cases = [("deepseek", "deepseek-chat", sse({"choices": [{"delta": {"tool_calls": [call]},
                  "finish_reason": "content_filter"}]}, "[DONE]"))]
        for reason in ("refusal", "pause_turn"):
            cases.append(("anthropic", "claude-sonnet-4-5", sse(
                {"type": "content_block_start", "index": 0, "content_block": {
                    "type": "tool_use", "id": "call", "name": "inspect", "input": {}}},
                {"type": "message_delta", "delta": {"stop_reason": reason}}, {"type": "message_stop"})))
        for provider, model, body in cases:
            with self.subTest(provider=provider, body=body):
                with self.assertRaises(self.p.ProviderError) as failure:
                    await self.adapter(provider, model, body).request([{"role": "user", "content": "Inspect"}])
                self.assertEqual(failure.exception.code, "response_blocked")

    async def test_usage_maps_only_bounded_supported_counters(self):
        body = sse({"candidates": [{"content": {"parts": [{"text": "Ready"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 3, "totalTokenCount": 15,
                              "thoughtsTokenCount": 4, "cachedContentTokenCount": 2,
                              "toolUsePromptTokenCount": 1, "unrecognizedPrivateCounter": 7}})
        result = await self.adapter("gemini", "gemini-2.5-flash", body).request([{"role": "user", "content": "Hi"}])
        self.assertEqual(result.usage, {"input_tokens": 8, "output_tokens": 3, "total_tokens": 15,
                                        "reasoning_tokens": 4, "cached_tokens": 2})
        body = sse({"type": "response.completed", "response": {"output": [], "usage": {
            "input_tokens": 8, "output_tokens": 6, "total_tokens": 14,
            "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 2},
            "service_tier": 8, "unknown": 10**20}}})
        result = await self.adapter("openai", "gpt-4.1", body).request([{"role": "user", "content": "Hi"}])
        self.assertEqual(result.usage, {"input_tokens": 8, "output_tokens": 6, "total_tokens": 14,
                                        "cached_tokens": 3, "reasoning_tokens": 2})


if __name__ == "__main__":
    unittest.main()
