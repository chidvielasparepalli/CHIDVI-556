"""Bounded native REST adapters. No SDK, credential persistence or tool execution.

Protocol sources and validation limits are recorded in docs/provider-protocols.md.
Opaque continuation metadata belongs in memory only; never display or log it.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import io
import ipaddress
import json
import math
import re
import uuid
from urllib.parse import quote, urlsplit, urlunsplit

import httpx


PROVIDERS = {
    "gemini": {"label": "Google Gemini", "default_base_url": "https://generativelanguage.googleapis.com/v1beta", "protocol": "gemini"},
    "openai": {"label": "OpenAI", "default_base_url": "https://api.openai.com/v1", "protocol": "responses"},
    "anthropic": {"label": "Anthropic Claude", "default_base_url": "https://api.anthropic.com/v1", "protocol": "anthropic"},
    "xai": {"label": "xAI Grok", "default_base_url": "https://api.x.ai/v1", "protocol": "chat"},
    "deepseek": {"label": "DeepSeek", "default_base_url": "https://api.deepseek.com/v1", "protocol": "chat"},
    "nvidia": {"label": "NVIDIA NIM", "default_base_url": "https://integrate.api.nvidia.com/v1", "protocol": "chat"},
    "opencode": {"label": "OpenCode (Zen / Console)", "default_base_url": "https://opencode.ai/inference/v1", "protocol": "model-specific"},
    "compatible": {"label": "OpenAI-compatible endpoint", "default_base_url": "", "protocol": "chat"},
}
for _id, _record in PROVIDERS.items():
    _record.update(auth_type="api_key", key_required=_id != "compatible")

CAPABILITIES = ("text", "vision", "tools", "streaming", "live_audio", "tts", "embeddings")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 512 * 1024
MAX_TOOL_CHARS = 128 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_TOOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_OPENCODE_CHAT = ("deepseek-", "minimax-", "glm-", "kimi-", "mistral-", "big-pickle", "qwen3.8-max",
                  "space-bunny", "cat-jam", "longcat-", "step-", "exo-", "mimo-", "ling-", "nemotron-")


def provider_label(provider):
    return PROVIDERS[provider]["label"]


def default_base_url(provider):
    return PROVIDERS[provider]["default_base_url"]


def validate_base_url(provider, url=None):
    if provider not in PROVIDERS:
        raise ValueError("Unknown provider")
    value = default_base_url(provider) if url is None or url == "" and provider != "compatible" else url
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError("A valid provider base URL is required")
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ValueError("Invalid provider URL") from None
    if (not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment
            or "\\" in value or any(segment in {".", ".."} for segment in parts.path.split("/"))):
        raise ValueError("Provider URL cannot contain credentials, query, fragment or relative paths")
    local = host.lower() == "localhost"
    with suppress(ValueError):
        local = ipaddress.ip_address(host).is_loopback
    if parts.scheme != "https" and not (provider == "compatible" and parts.scheme == "http" and local):
        raise ValueError("Provider URLs require HTTPS; HTTP is allowed only for an explicit loopback endpoint")
    normalized = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
    if provider != "compatible":
        allowed = {default_base_url(provider)}
        if provider == "opencode":
            allowed.add("https://opencode.ai/zen/v1")
        if normalized not in allowed:
            raise ValueError("This provider requires its official endpoint; use a separate compatible profile for another endpoint")
    return normalized


def model_capabilities(provider, model, overrides=None):
    if provider not in PROVIDERS:
        raise ValueError("Unknown provider")
    model = str(model).removeprefix("models/").lower()
    caps = {name: None if name in {"text", "vision", "tools", "streaming"} else False for name in CAPABILITIES}
    family = provider
    if provider == "opencode":
        family = ("gemini" if model.startswith("gemini-") else "anthropic" if model.startswith("claude-")
                  else "openai" if model.startswith("gpt-") else "xai" if model.startswith("grok-") else provider)
    if family == "gemini" and model.startswith("gemini-"):
        if "embedding" in model:
            caps.update(text=False, vision=False, tools=False, streaming=False, embeddings=True)
        elif "tts" in model:
            caps.update(text=False, vision=False, tools=False, streaming=False, tts=provider == "gemini")
        elif "live" in model or "native-audio" in model:
            caps.update(text=True, vision=True, tools=True, streaming=True, live_audio=provider == "gemini")
        elif re.match(r"gemini-(?:[234](?:\.\d+)?-|flash(?:-lite)?-latest)", model) and "image" not in model:
            caps.update(text=True, vision=True, tools=True, streaming=True)
    elif family == "anthropic" and re.match(r"claude-(?:[345]-|(?:sonnet|opus|haiku|fable)-[345])", model):
        caps.update(text=True, vision=True, tools=True, streaming=True)
    elif family == "openai" and re.match(r"(?:gpt-(?:4o|4\.1|[56](?:\.|-|$))|o[34](?:-|$))", model):
        if not any(value in model for value in ("audio", "realtime", "image", "transcribe", "tts")):
            caps.update(text=True, vision=True, tools=True, streaming=True)
    elif family == "xai" and re.match(r"grok-(?:[234](?:\.|-|$)|code-)", model):
        caps.update(text=True, vision=model.startswith("grok-4") or "vision" in model, tools=True, streaming=True)
    elif family == "deepseek" and (model in {"deepseek-chat", "deepseek-reasoner", "deepseek-flash", "deepseek-pro"} or model.startswith("deepseek-v4")):
        caps.update(text=True, vision=False, tools=True, streaming=True)
    elif provider == "nvidia" and model in {"meta/llama-3.1-8b-instruct", "meta/llama-3.1-70b-instruct", "meta/llama-3.3-70b-instruct"}:
        caps.update(text=True, vision=False, streaming=True)
    elif provider == "opencode" and model.startswith(_OPENCODE_CHAT):
        caps.update(text=True, streaming=True)
    if overrides is not None:
        if not isinstance(overrides, dict) or any(key not in CAPABILITIES or value is not None and type(value) is not bool for key, value in overrides.items()):
            raise ValueError("Capabilities must contain supported boolean or unknown values")
        for key, value in overrides.items():
            if value is True and caps[key] is False:
                raise ValueError(f"This adapter does not support {key} for that model")
            if value is not None:
                caps[key] = value
    return caps


def _modalities(capabilities):
    """Catalogue hints only; unknown model families retain unknown modalities."""
    if capabilities["live_audio"] is True:
        return ["text", "image", "audio"], ["audio"]
    if capabilities["tts"] is True:
        return ["text"], ["audio"]
    if capabilities["embeddings"] is True:
        return ["text"], ["embedding"]
    if capabilities["text"] is True:
        inputs = (["text", "image"] if capabilities["vision"] is True else
                  ["text"] if capabilities["vision"] is False else None)
        return inputs, ["text"]
    return None, None


def validate_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError("A provider profile object is required")
    result = dict(profile)
    provider, model = result.get("provider"), result.get("model", "")
    if provider not in PROVIDERS:
        raise ValueError("Unknown provider")
    if not isinstance(model, str) or not _MODEL.fullmatch(model) or any(x in {".", ".."} for x in model.split("/")):
        raise ValueError("A valid model identifier is required")
    auth = result.get("auth_type", "api_key")
    if auth not in {"api_key", "none"} or auth == "none" and provider != "compatible":
        raise ValueError("This provider requires API-key authentication")
    result.update(model=model.removeprefix("models/") if provider == "gemini" else model,
                  base_url=validate_base_url(provider, result.get("base_url")), auth_type=auth)
    overrides = result.get("capabilities", {})
    model_capabilities(provider, model, overrides)
    result["capabilities"] = dict(overrides)
    return result


class ProviderError(RuntimeError):
    def __init__(self, code, message, retry_after=None):
        super().__init__(message)
        self.code, self.message, self.retry_after = code, message, retry_after


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class AIResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: dict
    model: str
    finish_reason: str = "stop"
    provider_data: dict = field(default_factory=dict, repr=False)

    def as_message(self):
        result = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            result["tool_calls"] = [{"id": call.id, "type": "function", "function": {
                "name": call.name, "arguments": json.dumps(call.arguments, separators=(",", ":"))}} for call in self.tool_calls]
        if self.provider_data:
            result["_provider"] = deepcopy(self.provider_data)
        return result


def _error(code, message):
    raise ProviderError(code, message)


def _json(value):
    def non_finite(_):
        raise ValueError("Non-finite JSON number")
    try:
        return json.loads(value, parse_constant=non_finite)
    except (ValueError, TypeError, RecursionError):
        _error("malformed_response", "The provider returned malformed JSON; no tool call was accepted.")


def _call(identity, name, arguments):
    if not isinstance(identity, str) or not identity or len(identity) > 256 or not isinstance(name, str) or not _TOOL.fullmatch(name):
        _error("malformed_response", "The provider returned an invalid tool identity.")
    if isinstance(arguments, str):
        if len(arguments) > MAX_TOOL_CHARS:
            _error("response_too_large", "Provider tool arguments exceeded the configured bound.")
        arguments = _json(arguments)
    if not isinstance(arguments, dict):
        _error("malformed_response", "Provider tool arguments must be a JSON object.")
    try:
        if len(json.dumps(arguments, allow_nan=False)) > MAX_TOOL_CHARS:
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        _error("malformed_response", "Provider tool arguments are not a bounded JSON object.")
    return ToolCall(identity, name, arguments)


def _schema(value):
    if isinstance(value, dict):
        return {key: item.lower() if key == "type" and isinstance(item, str) else _schema(item)
                for key, item in value.items() if key != "handler"}
    return [_schema(item) for item in value] if isinstance(value, list) else value


def _tools(tools):
    if tools is None:
        return []
    if not isinstance(tools, list) or len(tools) > 256:
        _error("invalid_request", "Tools must be a bounded list of function schemas.")
    result = []
    for tool in tools:
        value = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = value.get("name")
        if not isinstance(name, str) or not _TOOL.fullmatch(name):
            _error("invalid_request", "Tool names must be valid identifiers.")
        parameters = _schema(value.get("parameters", {"type": "object", "properties": {}}))
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            _error("invalid_request", "Tool parameters must be an object schema.")
        result.append({"name": name, "description": str(value.get("description", ""))[:4000], "parameters": parameters})
    return result


def _protocol(provider, model):
    if provider != "opencode":
        return PROVIDERS[provider]["protocol"]
    if model.startswith(("gpt-", "grok-", "muse-spark")):
        return "responses"
    if model.startswith(("claude-", "qwen3.8-flash", "qwen3.7-", "qwen3.6-", "qwen3.5-")):
        return "anthropic"
    if model.startswith("gemini-"):
        return "gemini"
    if model.startswith(_OPENCODE_CHAT):
        return "chat"
    _error("unsupported_model", "The protocol for this OpenCode model is unknown; choose a documented model or an explicit compatible profile.")


def create_adapter(profile, api_key, transport=None):
    return ProviderAdapter(profile, api_key, transport=transport)


class _Response:
    """Assemble complete tool calls; fragments never escape this object."""
    def __init__(self, protocol, model):
        self.protocol, self.model = protocol, model
        self.text, self.reasoning, self.finish = "", "", ""
        self.usage, self.blocks, self.calls, self.items, self.parts = {}, {}, {}, {}, []
        self.complete = False

    def feed(self, event):
        if not isinstance(event, dict):
            _error("malformed_response", "Provider stream event must be an object.")
        if "error" in event or event.get("type") in {"error", "response.failed"}:
            _error("provider_unavailable", "The provider reported a stream failure; no partial tool call was accepted.")
        emitted = ""
        if self.protocol == "chat":
            if event.get("__done__"):
                self.complete = bool(self.finish)
            choices = event.get("choices") or []
            if choices:
                choice = choices[0]
                delta = choice.get("delta") or {}
                emitted = delta.get("content") or ""
                self.reasoning += delta.get("reasoning_content") or ""
                for call in delta.get("tool_calls") or []:
                    index = call.get("index", 0)
                    if type(index) is not int or not 0 <= index < 64:
                        _error("malformed_response", "Provider tool-call index is invalid.")
                    current = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if call.get("id"):
                        if current["id"] and current["id"] != call["id"]:
                            _error("malformed_response", "Provider changed a tool identity during streaming.")
                        current["id"] = call["id"]
                    function = call.get("function") or {}
                    current["name"] += function.get("name") or ""
                    current["arguments"] += function.get("arguments") or ""
                self.finish = choice.get("finish_reason") or self.finish
            self.usage.update(event.get("usage") or {})
            self.model = event.get("model") or self.model
        elif self.protocol == "anthropic":
            kind = event.get("type")
            index = event.get("index", 0)
            if kind == "message_start":
                message = event.get("message") or {}
                self.model = message.get("model") or self.model
                self.usage.update(message.get("usage") or {})
            elif kind == "content_block_start":
                if type(index) is not int or not 0 <= index < 128 or index in self.blocks:
                    _error("malformed_response", "Provider content block is invalid.")
                self.blocks[index] = deepcopy(event["content_block"])
                if self.blocks[index].get("type") == "text":
                    emitted = self.blocks[index].get("text", "")
            elif kind == "content_block_delta":
                if index not in self.blocks:
                    _error("malformed_response", "Provider delta has no matching content block.")
                block, delta = self.blocks[index], event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    emitted = delta.get("text", "")
                    block["text"] = block.get("text", "") + emitted
                elif delta.get("type") == "input_json_delta":
                    block["_json"] = block.get("_json", "") + delta.get("partial_json", "")
                elif delta.get("type") == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                elif delta.get("type") == "signature_delta":
                    block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif kind == "message_delta":
                self.finish = (event.get("delta") or {}).get("stop_reason") or self.finish
                self.usage.update(event.get("usage") or {})
            elif kind == "message_stop":
                self.complete = bool(self.finish)
        elif self.protocol == "gemini":
            candidates = event.get("candidates") or []
            if candidates:
                candidate = candidates[0]
                for original in (candidate.get("content") or {}).get("parts") or []:
                    part = deepcopy(original)
                    if "functionCall" in part:
                        call = part["functionCall"]
                        call.setdefault("id", "call_" + uuid.uuid4().hex)
                    self.parts.append(part)
                    if "text" in part and not part.get("thought"):
                        emitted += part["text"]
                reason = candidate.get("finishReason")
                if reason:
                    self.finish = "stop" if reason == "STOP" else "length" if reason == "MAX_TOKENS" else reason.lower()
                    self.complete = True
                    if reason not in {"STOP", "MAX_TOKENS"}:
                        _error("response_blocked", "The provider stopped this response; no tool call was accepted.")
            if (event.get("promptFeedback") or {}).get("blockReason"):
                _error("response_blocked", "The provider declined this request.")
            self.usage.update(event.get("usageMetadata") or {})
            self.model = event.get("modelVersion") or self.model
        else:
            kind = event.get("type")
            if kind == "response.output_text.delta":
                emitted = event.get("delta", "")
            elif kind in {"response.output_item.added", "response.output_item.done"}:
                item = deepcopy(event.get("item") or {})
                identity = item.get("id") or str(event.get("output_index", ""))
                self.items[identity] = item
            elif kind == "response.function_call_arguments.delta":
                identity = event.get("item_id")
                if identity not in self.items:
                    _error("malformed_response", "Provider tool delta has no matching output item.")
                self.items[identity]["arguments"] = self.items[identity].get("arguments", "") + event.get("delta", "")
            elif kind == "response.function_call_arguments.done":
                identity = event.get("item_id")
                if identity not in self.items:
                    _error("malformed_response", "Provider tool completion has no matching output item.")
                self.items[identity]["arguments"] = event.get("arguments", "")
            elif kind in {"response.completed", "response.incomplete"}:
                response = event.get("response") or {}
                self.model = response.get("model") or self.model
                self.usage.update(response.get("usage") or {})
                if isinstance(response.get("output"), list):
                    self.items = {str(index): deepcopy(item) for index, item in enumerate(response["output"])}
                    final_text = "".join(part.get("text", "") for item in self.items.values() if item.get("type") == "message"
                                         for part in item.get("content", []) if part.get("type") == "output_text")
                    if final_text and not self.text:
                        emitted = final_text
                self.finish = "length" if kind == "response.incomplete" else "stop"
                self.complete = True
        if not isinstance(emitted, str):
            _error("malformed_response", "Provider text must be a string.")
        self.text += emitted
        if len(self.text) > MAX_TEXT_CHARS or len(self.reasoning) > MAX_TEXT_CHARS or len(self.calls) > 64 or len(self.parts) > 4096:
            _error("response_too_large", "The provider response exceeded the configured bound.")
        return emitted

    def final(self, identity):
        if not self.complete:
            _error("incomplete_response", "The provider stream ended before completion; no partial tool call was accepted.")
        successful = {"chat": {"stop", "tool_calls", "length"},
                      "anthropic": {"end_turn", "tool_use", "stop_sequence", "max_tokens"},
                      "gemini": {"stop", "length"}, "responses": {"stop", "length"}}
        if self.finish not in successful.get(self.protocol, set()):
            _error("response_blocked", "The provider did not complete this response successfully; no tool call was accepted.")
        calls, opaque = [], {}
        if self.protocol == "chat":
            calls = [_call(call["id"], call["name"], call["arguments"]) for _, call in sorted(self.calls.items())]
            if self.reasoning:
                opaque["reasoning_content"] = self.reasoning
        elif self.protocol == "anthropic":
            blocks = []
            for _, value in sorted(self.blocks.items()):
                block = dict(value)
                if block.get("type") == "tool_use":
                    arguments = block.pop("_json", block.get("input", {}))
                    call = _call(block.get("id"), block.get("name"), arguments)
                    block["input"] = call.arguments
                    calls.append(call)
                blocks.append(block)
            opaque["content"] = blocks
        elif self.protocol == "gemini":
            for part in self.parts:
                if "functionCall" in part:
                    call = part["functionCall"]
                    calls.append(_call(call.get("id"), call.get("name"), call.get("args", {})))
            opaque["parts"] = self.parts
        else:
            items = [item for item in self.items.values() if item.get("type") in {"message", "function_call", "reasoning"}]
            calls = [_call(item.get("call_id"), item.get("name"), item.get("arguments", ""))
                     for item in items if item.get("type") == "function_call"]
            opaque["items"] = items
        if len(calls) > 64 or len({call.id for call in calls}) != len(calls):
            _error("malformed_response", "Provider tool identities are duplicated or excessive.")
        if calls and self.finish in {"length", "max_tokens"}:
            _error("incomplete_response", "The provider exhausted its output limit; incomplete tool calls cannot run.")
        names = {"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens", "promptTokenCount": "input_tokens",
                 "candidatesTokenCount": "output_tokens", "totalTokenCount": "total_tokens",
                 "thoughtsTokenCount": "reasoning_tokens", "cachedContentTokenCount": "cached_tokens",
                 "cache_read_input_tokens": "cached_tokens", "prompt_cache_hit_tokens": "cached_tokens"}
        allowed = {"input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"}
        usage = {names.get(key, key): value for key, value in self.usage.items()
                 if names.get(key, key) in allowed and type(value) is int and 0 <= value <= 10**12}
        for field, key in (("input_tokens_details", "cached_tokens"), ("prompt_tokens_details", "cached_tokens"),
                           ("output_tokens_details", "reasoning_tokens"), ("completion_tokens_details", "reasoning_tokens")):
            details = self.usage.get(field)
            value = details.get(key) if isinstance(details, dict) else None
            if type(value) is int and 0 <= value <= 10**12:
                usage[key] = value
        return AIResponse(self.text, calls, usage, self.model, self.finish, {**identity, **opaque})


async def _events(response):
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer, lines, total = "", [], 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            _error("response_too_large", "The provider response exceeded the configured byte limit.")
        try:
            buffer += decoder.decode(chunk)
        except UnicodeError:
            _error("malformed_response", "The provider stream is not valid UTF-8.")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line.startswith("data:"):
                lines.append(line[5:].removeprefix(" "))
            elif not line and lines:
                value, lines = "\n".join(lines), []
                yield {"__done__": True} if value == "[DONE]" else _json(value)
            if len(buffer) > MAX_RESPONSE_BYTES or sum(map(len, lines)) > MAX_EVENT_BYTES:
                _error("response_too_large", "Provider event exceeded the configured limit.")
        if len(buffer) > MAX_EVENT_BYTES:
            _error("response_too_large", "Provider stream line exceeded the configured limit.")
    try:
        buffer += decoder.decode(b"", final=True)
    except UnicodeError:
        _error("malformed_response", "The provider stream ended within a UTF-8 character.")
    if buffer.startswith("data:"):
        lines.append(buffer[5:].strip())
    if lines:
        value = "\n".join(lines)
        yield {"__done__": True} if value == "[DONE]" else _json(value)


def _image(value):
    from PIL import Image
    if not isinstance(value, dict):
        _error("invalid_request", "Images must contain bytes and a MIME type.")
    data, mime = value.get("data"), value.get("mime_type")
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES or mime not in {"image/png", "image/jpeg", "image/webp"}:
        _error("invalid_request", "Image format or size is unsupported; maximum is 5 MiB per image.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if Image.MIME.get(image.format) != mime or image.width * image.height > 40_000_000:
                raise ValueError()
            image.verify()
    except Exception:
        _error("invalid_request", "Image bytes are corrupt or do not match the declared MIME type.")
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mime_type": mime}


def _parts(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list) or len(content) > 32:
        _error("invalid_request", "Message content must be text or a bounded list of parts.")
    parts = []
    for part in content:
        if not isinstance(part, dict):
            _error("invalid_request", "Invalid message content part.")
        if part.get("type") in {"text", "input_text", "output_text"} and isinstance(part.get("text"), str):
            parts.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image":
            parts.append(_image(part))
        elif part.get("type") == "image_url":
            url = part.get("image_url", {}).get("url", "")
            match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)", url)
            if not match or len(url) > 8 * 1024 * 1024:
                _error("invalid_request", "Only bounded inline images are supported; remote image URLs are not fetched.")
            try:
                data = base64.b64decode(match[2], validate=True)
            except ValueError:
                _error("invalid_request", "Image base64 is invalid.")
            parts.append(_image({"data": data, "mime_type": match[1]}))
        else:
            _error("unsupported_capability", "This message content type is not supported by the provider adapter.")
    return parts


class ProviderAdapter:
    def __init__(self, profile, api_key, transport=None):
        discovery = isinstance(profile, dict) and profile.get("model", "") == ""
        try:
            self.profile = validate_profile({**profile, "model": "model-discovery"} if discovery else profile)
            if discovery:
                self.profile["model"] = ""
        except ValueError as error:
            raise ProviderError("invalid_profile", str(error)) from None
        self.provider, self.model = self.profile["provider"], self.profile["model"]
        self.base_url, self.transport = self.profile["base_url"], transport
        if not isinstance(api_key, str) or len(api_key) > 8192 or any(c in api_key for c in "\r\n"):
            _error("invalid_credential", "The API credential format is invalid.")
        if self.profile["auth_type"] == "api_key" and PROVIDERS[self.provider]["key_required"] and not api_key.strip():
            _error("missing_credential", "This provider requires a saved API key.")
        self._api_key = api_key.strip()

    def capabilities(self, model=None):
        return model_capabilities(self.provider, model or self.model, self.profile.get("capabilities"))

    def _headers(self, protocol):
        headers = {"accept": "text/event-stream, application/json", "content-type": "application/json"}
        if self.provider == "anthropic":
            headers.update({"x-api-key": self._api_key, "anthropic-version": "2023-06-01"})
        elif self.provider == "gemini":
            headers["x-goog-api-key"] = self._api_key
        elif self._api_key and self.profile["auth_type"] != "none":
            headers["authorization"] = "Bearer " + self._api_key
        if protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return headers

    def _identity(self, protocol):
        return {"provider": self.provider, "model": self.model, "base_url": self.base_url, "protocol": protocol}

    def _history(self, messages, system, images):
        if not isinstance(messages, list) or not 1 <= len(messages) <= 256 or not isinstance(system, str) or len(system) > 128_000:
            _error("invalid_request", "A bounded message history and system instruction are required.")
        normalized, systems, names, total, count_images = [], [system] if system else [], {}, 0, 0
        for value in messages:
            if not isinstance(value, dict) or value.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
                _error("invalid_request", "Message role is unsupported.")
            role = value["role"]
            if role in {"system", "developer"}:
                if not isinstance(value.get("content"), str):
                    _error("invalid_request", "System instructions must be text.")
                systems.append(value["content"])
                continue
            parts = _parts(value.get("content"))
            count_images += sum(part["type"] == "image" for part in parts)
            total += sum(len(part.get("text", part.get("data", ""))) for part in parts)
            item = {"role": role, "parts": parts}
            if role == "assistant":
                calls = value.get("tool_calls") or []
                if not isinstance(calls, list) or len(calls) > 64:
                    _error("invalid_request", "Assistant tool-call history is invalid.")
                item["calls"] = []
                for call in calls:
                    function = call.get("function", {})
                    checked = _call(call.get("id"), function.get("name"), function.get("arguments", {}))
                    item["calls"].append(checked)
                    names[checked.id] = checked.name
                if isinstance(value.get("_provider"), dict):
                    item["private"] = value["_provider"]
            elif role == "tool":
                identity = value.get("tool_call_id")
                if identity not in names:
                    _error("invalid_request", "Tool result has no preceding assistant tool call.")
                item.update(call_id=identity, name=names[identity])
            normalized.append(item)
        if images:
            if not isinstance(images, list) or len(images) > 4:
                _error("invalid_request", "At most four explicit images may be sent per request.")
            users = [item for item in normalized if item["role"] == "user"]
            if not users:
                _error("invalid_request", "Images require a user message.")
            for value in images:
                part = _image(value)
                users[-1]["parts"].append(part)
                count_images += 1
                total += len(part["data"])
        system = "\n\n".join(systems)
        if len(system) > 128_000 or total > 24 * 1024 * 1024 or count_images > 4 or not normalized:
            _error("invalid_request", "Provider input exceeded its configured size limit.")
        return normalized, system, bool(count_images)

    def _body(self, messages, system, tools, images, max_tokens, *, probe=False):
        if not self.model:
            _error("invalid_profile", "Select a model before sending or testing a request.")
        caps = self.capabilities()
        stream = caps["streaming"] is True or probe
        protocol = _protocol(self.provider, self.model)
        if caps["live_audio"] or caps["tts"] or caps["embeddings"]:
            _error("unsupported_capability", "This model uses a dedicated audio or embedding API; the text adapter cannot impersonate it.")
        functions = _tools(tools)
        messages, system, has_images = self._history(messages, system, images)
        for capability, requested in (("text", True), ("tools", bool(functions)), ("vision", has_images)):
            if requested and caps[capability] is not True and not probe:
                _error("unsupported_capability", f"{capability.capitalize()} is not confirmed for this model; use an explicit capability test first.")
        identity, rows = self._identity(protocol), []
        for item in messages:
            role, parts = item["role"], item["parts"]
            private = item.get("private") or {}
            private = private if all(private.get(key) == value for key, value in identity.items()) else {}
            text = "".join(part["text"] for part in parts if part["type"] == "text")
            if protocol == "chat":
                content = [{"type": "text", "text": part["text"]} if part["type"] == "text" else {
                    "type": "image_url", "image_url": {"url": f"data:{part['mime_type']};base64,{part['data']}"}} for part in parts]
                row = {"role": role, "content": text if all(p["type"] == "text" for p in parts) else content}
                if role == "assistant" and item["calls"]:
                    row["tool_calls"] = [{"id": call.id, "type": "function", "function": {
                        "name": call.name, "arguments": json.dumps(call.arguments)}} for call in item["calls"]]
                    if private.get("reasoning_content"):
                        row["reasoning_content"] = private["reasoning_content"]
                if role == "tool":
                    row["tool_call_id"] = item["call_id"]
                rows.append(row)
            elif protocol == "gemini":
                if role == "tool":
                    try:
                        response = json.loads(text)
                    except ValueError:
                        response = {"result": text}
                    if not isinstance(response, dict):
                        response = {"result": response}
                    content = [{"functionResponse": {"id": item["call_id"], "name": item["name"], "response": response}}]
                elif role == "assistant" and private.get("parts"):
                    content = deepcopy(private["parts"])
                else:
                    content = [{"text": part["text"]} if part["type"] == "text" else {
                        "inlineData": {"mimeType": part["mime_type"], "data": part["data"]}} for part in parts]
                    content += [{"functionCall": {"id": call.id, "name": call.name, "args": call.arguments}} for call in item.get("calls", [])]
                rows.append({"role": "model" if role == "assistant" else "user", "parts": content})
            elif protocol == "anthropic":
                if role == "tool":
                    content = [{"type": "tool_result", "tool_use_id": item["call_id"], "content": text}]
                elif role == "assistant" and private.get("content"):
                    content = deepcopy(private["content"])
                else:
                    content = [{"type": "text", "text": part["text"]} if part["type"] == "text" else {
                        "type": "image", "source": {"type": "base64", "media_type": part["mime_type"], "data": part["data"]}} for part in parts]
                    content += [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in item.get("calls", [])]
                actual_role = "assistant" if role == "assistant" else "user"
                if rows and rows[-1]["role"] == actual_role:
                    rows[-1]["content"].extend(content)
                else:
                    rows.append({"role": actual_role, "content": content})
            else:
                if role == "tool":
                    rows.append({"type": "function_call_output", "call_id": item["call_id"], "output": text})
                elif role == "assistant" and private.get("items"):
                    rows.extend(deepcopy(private["items"]))
                else:
                    content = [{"type": "output_text" if role == "assistant" else "input_text", "text": part["text"]} if part["type"] == "text" else {
                        "type": "input_image", "image_url": f"data:{part['mime_type']};base64,{part['data']}"} for part in parts]
                    if content:
                        rows.append({"role": role, "content": text if role == "assistant" else content})
                    rows += [{"type": "function_call", "call_id": call.id, "name": call.name,
                              "arguments": json.dumps(call.arguments)} for call in item.get("calls", [])]
        if protocol == "gemini":
            body = {"contents": rows, "generationConfig": {"maxOutputTokens": max_tokens}}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            if functions:
                # Shared schemas are JSON Schema, not Google's smaller Schema message.
                body["tools"] = [{"functionDeclarations": [{"name": function["name"],
                    "description": function["description"], "parametersJsonSchema": function["parameters"]}
                    for function in functions]}]
            path = "/models/" + quote(self.model, safe="") + (":streamGenerateContent" if stream else ":generateContent")
            params = {"alt": "sse"} if stream else None
        elif protocol == "anthropic":
            body = {"model": self.model, "messages": rows, "max_tokens": max_tokens, "stream": stream}
            if system:
                body["system"] = system
            if functions:
                body["tools"] = [{"name": f["name"], "description": f["description"], "input_schema": f["parameters"]} for f in functions]
            path, params = "/messages", None
        elif protocol == "responses":
            body = {"model": self.model, "input": rows, "max_output_tokens": max_tokens, "stream": stream, "store": False}
            if self.provider == "openai":
                body["include"] = ["reasoning.encrypted_content"]
            if system:
                body["instructions"] = system
            if functions:
                body["tools"] = [{"type": "function", **function, "strict": False} for function in functions]
            path, params = "/responses", None
        else:
            if system:
                rows.insert(0, {"role": "system", "content": system})
            body = {"model": self.model, "messages": rows, "max_tokens": max_tokens, "stream": stream}
            if functions:
                body["tools"] = [{"type": "function", "function": function} for function in functions]
            path, params = "/chat/completions", None
        try:
            if len(json.dumps(body, allow_nan=False)) > 32 * 1024 * 1024:
                raise ValueError()
        except (TypeError, ValueError, RecursionError):
            _error("invalid_request", "Provider request is not a bounded JSON object.")
        return protocol, path, params, body

    @staticmethod
    def _http_error(response):
        if 200 <= response.status_code < 300:
            return
        status = response.status_code
        code, message = ("authentication_failed", "The provider rejected this API key.") if status == 401 else (
            ("permission_denied", "This account or key cannot use the requested provider resource.") if status == 403 else
            ("model_unavailable", "The requested model or endpoint was not found.") if status == 404 else
            ("rate_limited", "The provider rate or quota limit was reached; automatic retries are disabled.") if status in {402, 429} else
            ("provider_unavailable", "The provider is temporarily unavailable; no fallback request was made.") if status >= 500 else
            ("redirect_refused", "The endpoint attempted a redirect; credentials were not forwarded.") if 300 <= status < 400 else
            ("request_rejected", "The provider rejected the request format or selected capabilities."))
        retry = response.headers.get("retry-after", "")
        retry = min(86400, int(retry)) if retry.isdecimal() else None
        raise ProviderError(code, message, retry)

    async def _perform(self, protocol, path, params, body, timeout, on_text):
        state = _Response(protocol, self.model)
        base = self.base_url
        if self.provider == "opencode" and base == default_base_url("opencode"):
            suffix = {"gemini": "/google/v1beta", "anthropic": "/anthropic/v1", "responses": "/openai/v1", "chat": "/openai/v1"}[protocol]
            base = "https://opencode.ai/inference" + suffix
        async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(timeout, connect=min(timeout, 10)),
                                    follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", base + path, headers=self._headers(protocol), params=params, json=body) as response:
                self._http_error(response)
                if "text/event-stream" in response.headers.get("content-type", ""):
                    async for event in _events(response):
                        emitted = state.feed(event)
                        if emitted and on_text is not None:
                            await self._emit(on_text, emitted)
                else:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            _error("response_too_large", "The provider response exceeded the configured byte limit.")
                    value = _json(bytes(raw))
                    if not isinstance(value, dict) or "error" in value:
                        _error("malformed_response", "The provider did not return a successful response object.")
                    if protocol == "chat":
                        choices = value.get("choices") or []
                        deltas = []
                        for choice in choices:
                            message = dict(choice.get("message") or {})
                            if message.get("tool_calls"):
                                message["tool_calls"] = [{**call, "index": index} for index, call in enumerate(message["tool_calls"])]
                            deltas.append({"delta": message, "finish_reason": choice.get("finish_reason")})
                        state.feed({**value, "choices": deltas})
                        state.feed({"__done__": True})
                    elif protocol == "gemini":
                        state.feed(value)
                    elif protocol == "anthropic":
                        state.feed({"type": "message_start", "message": value})
                        for index, block in enumerate(value.get("content", [])):
                            state.feed({"type": "content_block_start", "index": index, "content_block": block})
                        state.feed({"type": "message_delta", "delta": {"stop_reason": value.get("stop_reason")}})
                        state.feed({"type": "message_stop"})
                    else:
                        state.feed({"type": "response.completed" if value.get("status") == "completed" else "response.incomplete", "response": value})
                    if state.text and on_text is not None:
                        await self._emit(on_text, state.text)
        return state.final(self._identity(protocol))

    @staticmethod
    async def _emit(callback, text):
        try:
            result = callback(text)
            if inspect.isawaitable(result):
                await result
        except Exception:
            raise ProviderError("callback_failed", "The response consumer failed; provider streaming was stopped.") from None

    async def request(self, messages, system="", tools=None, cancel_event=None, on_text=None, images=None,
                      max_tokens=1024, timeout=45, *, _probe=False):
        if type(max_tokens) is not int or not 1 <= max_tokens <= 32768 or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not .01 <= timeout <= 120:
            _error("invalid_request", "Provider token or timeout limits are invalid.")
        if cancel_event is not None and cancel_event.is_set():
            _error("cancelled", "Provider request was cancelled before transmission.")
        protocol, path, params, body = self._body(messages, system, tools, images, max_tokens, probe=_probe)
        task = asyncio.create_task(self._perform(protocol, path, params, body, timeout, on_text))
        try:
            async with asyncio.timeout(timeout):
                while not task.done():
                    if cancel_event is not None and cancel_event.is_set():
                        _error("cancelled", "Provider request was cancelled; its late response was discarded.")
                    await asyncio.wait((task,), timeout=.05)
                if cancel_event is not None and cancel_event.is_set():
                    _error("cancelled", "Provider request was cancelled; its late response was discarded.")
                return await task
        except ProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError("timeout", "The provider request timed out; no automatic retry or fallback was made.") from None
        except httpx.HTTPError:
            raise ProviderError("connection_failed", "The provider connection failed; check the endpoint and network.") from None
        except (AttributeError, KeyError, IndexError, TypeError, ValueError, RecursionError):
            raise ProviderError("malformed_response", "The provider returned an unsupported or malformed response.") from None
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def models(self):
        try:
            async with asyncio.timeout(15):
                return await self._models()
        except TimeoutError:
            raise ProviderError("timeout", "Provider model discovery timed out.") from None

    async def _models(self):
        protocol = PROVIDERS[self.provider]["protocol"]
        try:
            rows, cursors, total = [], set(), 0
            params = {"pageSize": 1000} if self.provider == "gemini" else {"limit": 1000} if self.provider == "anthropic" else None
            async with httpx.AsyncClient(transport=self.transport, timeout=15, follow_redirects=False, trust_env=False) as client:
                for _ in range(10):
                    async with client.stream("GET", self.base_url + "/models", headers=self._headers(protocol), params=params) as response:
                        self._http_error(response)
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            total += len(chunk)
                            if total > MAX_RESPONSE_BYTES:
                                _error("response_too_large", "Provider model list exceeded the configured bound.")
                    value = _json(bytes(data))
                    page = value.get("models") if self.provider == "gemini" else value.get("data", value.get("models"))
                    if not isinstance(page, list) or len(rows) + len(page) > 4096:
                        _error("malformed_response", "Provider model list has an unsupported format.")
                    rows.extend(page)
                    cursor = value.get("nextPageToken") if self.provider == "gemini" else value.get("last_id") if self.provider == "anthropic" and value.get("has_more") else None
                    if not cursor:
                        break
                    if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in cursors:
                        _error("malformed_response", "Provider model pagination is invalid.")
                    cursors.add(cursor)
                    params["pageToken" if self.provider == "gemini" else "after_id"] = cursor
                else:
                    _error("response_too_large", "Provider model discovery exceeded its page limit; enter a model manually.")
            result = []
            checked_at = datetime.now(timezone.utc).date().isoformat()
            for row in rows:
                name = row.get("id") or row.get("name") if isinstance(row, dict) else None
                if not isinstance(name, str) or not _MODEL.fullmatch(name):
                    continue
                name = name.removeprefix("models/")
                limits = {key: row[source] for key, source in (("input_tokens", "inputTokenLimit"), ("output_tokens", "outputTokenLimit")) if type(row.get(source)) is int}
                capabilities = model_capabilities(self.provider, name)
                inputs, outputs = _modalities(capabilities)
                result.append({"id": name, "label": str(row.get("displayName") or row.get("display_name") or name)[:200],
                    "provider": self.provider, "capabilities": capabilities, "input_modalities": inputs,
                    "output_modalities": outputs, "limits": limits or None, "pricing": None,
                    "catalogue_checked_at": checked_at, "pricing_checked_at": None})
            return result
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError("timeout", "Provider model discovery timed out.") from None
        except httpx.HTTPError:
            raise ProviderError("connection_failed", "Provider model discovery failed to connect.") from None
        except (AttributeError, TypeError, KeyError, ValueError):
            raise ProviderError("malformed_response", "Provider model discovery returned an unsupported response.") from None

    async def test(self, model=None, capability="text"):
        if capability not in {"text", "tools", "vision"}:
            _error("unsupported_capability", "Only explicit text, tool, and synthetic-image probes are supported.")
        adapter = self if model is None or model == self.model else create_adapter({**self.profile, "model": model}, self._api_key, self.transport)
        tools, images, question = None, None, "Reply with exactly READY. This is a synthetic connection test."
        if capability == "tools":
            question = "Call the capability_probe function exactly once with value READY. Do not answer in text."
            tools = [{"name": "capability_probe", "description": "Synthetic no-op capability probe; no action is executed.",
                "parameters": {"type": "object", "properties": {"value": {"type": "string", "enum": ["READY"]}}, "required": ["value"]}}]
        elif capability == "vision":
            from PIL import Image
            output = io.BytesIO()
            Image.new("RGB", (32, 32), "red").save(output, "PNG")
            images = [{"data": output.getvalue(), "mime_type": "image/png"}]
            question = "Name only the dominant color of this synthetic test image."
        result = await adapter.request([{"role": "user", "content": question}], tools=tools, images=images,
                                       max_tokens=128, timeout=30, _probe=True)
        passed = (bool(result.text.strip()) if capability == "text" else
                  len(result.tool_calls) == 1 and result.tool_calls[0].name == "capability_probe" and result.tool_calls[0].arguments == {"value": "READY"} if capability == "tools" else
                  result.text.strip().strip('.!"\'').lower() == "red")
        return {"ok": passed, "model": adapter.model, "capability": capability,
                "checked_capabilities": {capability: passed}, "usage": result.usage,
                "message": "Synthetic capability test passed." if passed else "The provider responded but did not pass this capability probe."}
