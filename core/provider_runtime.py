"""Provider turns using the existing task owner and tool dispatcher."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
from types import SimpleNamespace


def validate_arguments(value, schema, depth=0):
    if depth > 20 or not isinstance(schema, dict):
        raise ValueError("Invalid tool schema")
    kind = str(schema.get("type", "")).lower()
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError("Tool arguments must be an object")
        if set(schema.get("required", [])) - value.keys():
            raise ValueError("Missing required tool arguments")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties", False) is False and set(value) - properties.keys():
            raise ValueError("Unknown tool argument")
        for key, item in value.items():
            if key in properties:
                validate_arguments(item, properties[key], depth + 1)
    elif kind == "array":
        if not isinstance(value, list) or len(value) > 1000:
            raise ValueError("Invalid tool array")
        for item in value:
            validate_arguments(item, schema.get("items", {}), depth + 1)
    elif kind == "string" and (not isinstance(value, str) or len(value) > 100_000):
        raise ValueError("Invalid tool text")
    elif kind == "boolean" and type(value) is not bool:
        raise ValueError("Invalid tool boolean")
    elif kind in {"number", "integer"}:
        if type(value) not in (int, float) or not math.isfinite(value) or kind == "integer" and type(value) is not int:
            raise ValueError("Invalid tool number")
        if "minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]:
            raise ValueError("Tool number is outside its allowed range")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Invalid tool choice")


def is_live_profile(profile):
    from core.providers import model_capabilities
    return bool(profile and profile.get("provider") == "gemini" and
                model_capabilities("gemini", profile.get("model", "")).get("live_audio") is True)


class ProviderRuntime:
    def __init__(self, store=None, adapter_factory=None):
        from core.provider_config import get_provider_store
        from core.providers import create_adapter
        self.store = store or get_provider_store()
        self.adapter_factory = adapter_factory or create_adapter
        self.histories, self.pending, self.usage = {}, {}, {}

    async def run(self, task_id, text, *, system, tools, dispatch, cancelled,
                  valid=lambda: True, on_text=None, profile=None, resume=None):
        from core.providers import ProviderError
        from core.task_control import result_state
        rounds_left = 8
        if resume is not None:
            turn = self.pending.get(task_id)
            if turn is None:
                raise ProviderError("expired", "The pending provider turn has expired.")
            messages, profile, key, seen, expected, rounds_left, text = turn
            if (not isinstance(resume, dict) or set(resume) != {"role", "tool_call_id", "name", "content"}
                    or any(resume.get(field) != expected[field] for field in ("role", "tool_call_id", "name"))
                    or not isinstance(resume.get("content"), str)):
                raise ProviderError("invalid_resume", "This approval does not belong to the pending tool call.")
            self.pending.pop(task_id)
            messages.append({**resume, "content": resume["content"][:32_000]})
        else:
            if task_id in self.pending:
                raise ProviderError("pending", "This task is still waiting for its original desktop approval.")
            profile = copy.deepcopy(profile or self.store.selected("chat"))
            if profile is None:
                raise ProviderError("missing_configuration", "Choose a chat provider in AI Providers settings.")
            key = self.store.credential(profile["id"])
            if not key and profile.get("auth_type", "api_key") != "none":
                raise ProviderError("missing_credentials", "The selected provider needs a saved credential.")
            history_id = self._history_id(profile)
            messages = copy.deepcopy(self.histories.get(history_id, [])) + [{"role": "user", "content": text}]
            seen = set()
        history_id = self._history_id(profile)

        def guard():
            if cancelled.is_set() or not valid():
                raise ProviderError("cancelled", "The originating task ended; its late results were discarded.")
            current = self.store.get_profile(profile["id"])
            if (current is None or self.store.credential(profile["id"]) != key
                    or any(current.get(field) != profile.get(field) for field in ("provider", "base_url", "auth_type"))):
                raise ProviderError("credentials_revoked", "This provider credential or recipient changed; start a new task.")

        async def emit_text(chunk):
            # A provider may already be delivering a chunk when Stop/key rotation arrives.
            try:
                guard()
            except ProviderError:
                return
            result = on_text(chunk)
            if inspect.isawaitable(result):
                await result

        guard()
        adapter = self.adapter_factory(profile, key or "")
        catalogue = {tool["name"]: tool for tool in tools}
        capabilities = adapter.capabilities()
        allowed_tools = tools if capabilities.get("tools") is True else []
        if not allowed_tools:
            system += "\nThis model has no verified tool capability. Answer text questions only; do not claim to perform desktop actions."
        for round_index in range(rounds_left):
            guard()
            response = await adapter.request(messages, system=system, tools=allowed_tools,
                cancel_event=cancelled, on_text=emit_text if on_text is not None else None,
                max_tokens=profile.get("limits", {}).get("max_output_tokens", 2048), timeout=60)
            guard()
            self.store.record_status(profile["id"], connected=True, usage=response.usage,
                                     expected_profile=profile, expected_credential=key)
            totals = self.usage.setdefault(profile["id"], {})
            for name, count in response.usage.items():
                if type(count) in (int, float) and math.isfinite(count) and count >= 0:
                    totals[name] = totals.get(name, 0) + count
            messages.append(response.as_message())
            if not response.tool_calls:
                if not response.text.strip():
                    raise ProviderError("empty_response", "The provider returned no answer.")
                # Keep whole completed turns so truncation never orphans tool replies.
                if len(messages) > 60 or len(json.dumps(messages, default=str)) > 200_000:
                    messages = [{"role": "user", "content": text}, {"role": "assistant", "content": response.text}]
                self.histories[history_id] = messages
                return {"text": response.text, "provider": profile["provider"], "model": profile["model"],
                        "usage": response.usage, "session_usage": dict(totals), "waiting": False}
            if not allowed_tools or len(response.tool_calls) > 16:
                raise ProviderError("unsupported_tools", "This response cannot dispatch tools.")
            # Reject an invalid batch before its first side effect, including duplicate IDs.
            batch_ids = set()
            for call in response.tool_calls:
                if call.id in seen or call.id in batch_ids or call.name not in catalogue:
                    raise ProviderError("invalid_tool", "The provider returned an unknown or repeated tool call.")
                try:
                    validate_arguments(call.arguments, catalogue[call.name].get("parameters", {"type": "OBJECT"}))
                except ValueError as exc:
                    raise ProviderError("invalid_arguments", str(exc)) from None
                batch_ids.add(call.id)
            seen.update(batch_ids)
            for index, call in enumerate(response.tool_calls):
                guard()
                result = await dispatch(SimpleNamespace(id=call.id, name=call.name, args=call.arguments))
                guard()
                result = result.response.get("result") if hasattr(result, "response") else result
                content = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
                reply = {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content[:32_000]}
                outcome = result_state(result)
                if outcome == "waiting_for_user_input":
                    for later in response.tool_calls[index + 1:]:
                        messages.append({"role": "tool", "tool_call_id": later.id, "name": later.name,
                                         "content": "Not executed: another action requires user approval."})
                    self.pending[task_id] = (messages, profile, key, seen, dict(reply),
                                             rounds_left - round_index - 1, text)
                    return {"text": "Waiting for desktop approval.", "waiting": True, "resume_tool": reply}
                messages.append(reply)
                if outcome == "failed":
                    return {"text": content, "waiting": False, "failed": True, "usage": response.usage}
        raise ProviderError("step_limit", "Stopped after eight model turns; no actions were retried automatically.")

    @staticmethod
    def _history_id(profile):
        return tuple(profile.get(field, "") for field in ("id", "provider", "model", "base_url", "auth_type"))
