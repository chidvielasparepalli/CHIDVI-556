# Provider transport contract

`core.providers` sends native REST requests with `httpx`. New provider SDKs are not required. The application owns credentials, task ownership, screenshot consent, tool execution, conversation rounds and provider selection; the transport executes no tools and performs no automatic retry or provider fallback.

Protocol references were checked on 14 October 2026. Adapter tests use synthetic images, mocked HTTP transports and protocol-shaped events; these tests do **not** establish live account access or prove every listed model is currently available.

An explicitly authorized live Gemini check also passed on 14 October 2026 with the selected `gemini-flash-lite-latest` vision model, copied into an encrypted temporary chat profile. The shared runtime made two native REST requests: it dispatched only `verify_sum(a=17,b=25)`, returned `42` to the model, and received `Verified sum: 42`. Reported usage was 373 tokens across both requests. Saved profiles and roles remained unchanged. Evidence is in `logs/provider-runtime-live.json`; the repeatable synthetic check is `logs/provider-runtime-live-check.py`. Other providers have mocked protocol coverage, not live account verification.

| Provider | Request protocol | Official reference |
| --- | --- | --- |
| Gemini | `generateContent` / SSE `streamGenerateContent`; native function declarations, calls and responses | [Gemini content API](https://ai.google.dev/api/generate-content), [function calling](https://ai.google.dev/gemini-api/docs/function-calling) |
| OpenAI | Responses, `store:false`, native function calls and outputs | [Function calling](https://developers.openai.com/api/docs/guides/function-calling), [stream events](https://developers.openai.com/api/reference/resources/responses/streaming-events) |
| Anthropic | Messages, native content blocks, `tool_use` and `tool_result` | [API overview](https://platform.claude.com/docs/en/api/overview), [streaming](https://platform.claude.com/docs/en/build-with-claude/streaming) |
| xAI | Documented Chat Completions compatibility | [Chat Completions reference](https://docs.x.ai/developers/rest-api-reference/inference/chat-completions) |
| DeepSeek | Chat Completions and tool-call deltas | [Tool calls](https://api-docs.deepseek.com/guides/tool_calls/) |
| NVIDIA NIM | Documented `/v1/chat/completions`; capabilities remain model-specific | [NIM LLM APIs](https://docs.api.nvidia.com/nim/reference/llm-apis) |
| OpenCode | Model-specific Responses, Messages, Gemini or Chat Completions | [Inference API](https://opencode.ai/v2/docs/console/api/inference/), [model endpoints](https://opencode.ai/v2/docs/console/models/) |
| Compatible | Explicit Chat Completions endpoint; capabilities must be declared or tested | Endpoint operator's documentation is required |

OpenCode's current discovery URL is `https://opencode.ai/inference/v1/models`. Its dedicated inference documentation specifies `/inference/openai/v1`, `/inference/anthropic/v1` and `/inference/google/v1beta`; the adapter selects that documented route for the model family. Explicit legacy `/zen/v1` profiles remain accepted, without a live compatibility claim. Models with unknown OpenCode protocols fail clearly.

## Integration

`create_adapter(profile, api_key, transport=None)` accepts `provider`, `model`, optional `base_url`, `auth_type` (`api_key`, or `none` for compatible endpoints), and explicit capability overrides. An empty model is allowed only for discovery; saved profiles and inference require a model.

`await adapter.request(messages, system='', tools=None, cancel_event=None, on_text=None, images=None, max_tokens=1024, timeout=45)` returns `AIResponse(text, tool_calls, usage, model, finish_reason)`. Images are explicit `{data: bytes, mime_type: str}` values. The caller must obtain the applicable image consent before calling this method. Remote image URLs are refused.

Append `response.as_message()` unchanged to the in-memory conversation. Its private `_provider` field carries opaque native continuation data. Do not display, log or persist that field. Keep tool outputs as `{role:'tool', tool_call_id:..., content:...}`. Native signatures and reasoning continuation items must accompany later tool outputs; plain text cannot recreate them. Only completed, valid JSON-object tool arguments become `ToolCall` records.

Shared tool declarations use JSON Schema. Gemini receives them through `parametersJsonSchema`, which supports constraints such as `additionalProperties`; the smaller native `parameters` Schema message rejected that same live test before this mapping was corrected.

`await adapter.models()` returns `{id,label,provider,capabilities,input_modalities,output_modalities,limits,pricing,catalogue_checked_at,pricing_checked_at}` records. Unknown modalities, limits and prices are `None`. Modality hints come from the conservative capability registry; the catalogue date is the UTC discovery date, while unknown pricing has no verification date. Gemini and Anthropic discovery follow bounded cursors; unsupported discovery endpoints report a safe error so users can enter a model manually.

`await adapter.test(capability='text'|'tools'|'vision')` is an explicit network action. Text tests use a synthetic greeting. The tool probe requests a no-op function without executing it. Vision tests use a generated red square, never desktop pixels. Results identify only the tested capability; a text pass does not establish tool, image or audio support. Registry capabilities are conservative model-family hints, not proof of account availability. Dedicated Live audio, TTS and embedding APIs are not simulated by this text transport.

## Bounds and failures

Requests have a total timeout of at most 120 seconds (default 45); discovery has a 15-second total timeout, ten-page maximum and 4 MiB response limit. Inference responses are bounded to 4 MiB, individual SSE events to 1 MiB, visible text to 512 KiB characters, and tool arguments to 128 KiB characters. Up to four explicit images are allowed, each at most 5 MiB. Streaming-disabled profiles use complete JSON responses.

Cancellation closes the owned request and discards late results. `ProviderError` exposes safe `code`, `message` and optional numeric `retry_after`; HTTP error bodies, API keys, prompts and private URLs are excluded. Redirects and environment proxy inheritance are disabled. Known providers are pinned to official HTTPS base paths. Custom endpoints require HTTPS, except explicitly configured HTTP loopback endpoints. OAuth, cloud IAM, Anthropic multi-workspace headers and arbitrary extra authentication headers are not implemented.
