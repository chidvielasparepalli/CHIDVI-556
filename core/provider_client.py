"""Compatibility boundary for existing synchronous model-assisted tools.

Keeps their real tool handlers; requests are routed through the selected role.
Gemini-only services fail explicitly for other providers instead of failing over.
"""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace


def gemini_usage(usage):
    values = usage.model_dump(exclude_none=True) if hasattr(usage, "model_dump") else usage if isinstance(usage, dict) else {}
    names = {"prompt_token_count": "input_tokens", "candidates_token_count": "output_tokens", "response_token_count": "output_tokens",
             "total_token_count": "total_tokens", "cached_content_token_count": "cached_tokens", "thoughts_token_count": "reasoning_tokens"}
    return {names[name]: value for name, value in values.items() if name in names and type(value) is int and value >= 0}


def configured_client(role="chat", task_kind="planning", *, legacy_key=None, http_options=None, image_authorized=False):
    from core.provider_config import get_provider_store
    store = get_provider_store()
    profile = store.selected(role, task_kind=task_kind)
    if profile is None:
        if store.list_profiles():
            from core.providers import ProviderError
            raise ProviderError("missing_configuration", f"Choose a {role} provider in AI Providers settings.")
        # Compatibility while an existing plaintext configuration awaits migration.
        from memory.config_manager import get_gemini_key
        from google import genai
        key = legacy_key if legacy_key is not None else get_gemini_key()
        if not key:
            from core.providers import ProviderError
            raise ProviderError("missing_configuration", f"Configure the {role} provider in AI Providers.")
        return genai.Client(api_key=key, **({"http_options": http_options} if http_options else {}))
    return ConfiguredClient(store, profile, image_authorized=image_authorized)


def sharing_destinations():
    """Disclosure identity, always read live rather than from a task snapshot."""
    from core.provider_config import get_provider_store
    store = get_provider_store()
    data = store.snapshot()
    profiles = {p["id"]: p for p in store.list_profiles()}
    result = []
    for role in ("live_audio", "vision"):
        profile = profiles.get(data.get("roles", {}).get(role))
        if profile:
            result.append((role, profile["id"], profile["provider"], profile["model"], profile.get("base_url", "")))
    for kind in ("vision", "visual_control"):
        profile = profiles.get(data.get("overrides", {}).get(kind))
        if profile:
            result.append((kind, profile["id"], profile["provider"], profile["model"], profile.get("base_url", "")))
    return tuple(result)


def sharing_destination_label():
    values = sharing_destinations()
    return "; ".join(f"{role}: {provider} / {model} at {url}" for role, _, provider, model, url in values) or "Google Gemini (legacy configuration)"


class ConfiguredClient:
    def __init__(self, store, profile, *, image_authorized=False):
        self.store, self.profile = store, profile
        self.models = self
        self.image_authorized = image_authorized
        self._credential = store.credential(profile["id"])

    def close(self):
        pass

    def generate_content(self, *, contents, model=None, config=None, **unused):
        from core.providers import ProviderError, provider_label
        from core.desktop_control import get_service
        from memory.config_manager import get_cloud_vision_enabled
        from PIL import Image
        reviews = []
        completion = "failed"
        try:
            if not self.image_authorized:
                parts = contents if isinstance(contents, list) else [contents]
                question = "\n".join(part for part in parts if isinstance(part, str))[:8000] or "Interpret this image"
                for part in parts:
                    data, mime = None, None
                    if isinstance(part, Image.Image):
                        buffer = io.BytesIO()
                        part.save(buffer, "PNG")
                        data, mime = buffer.getvalue(), "image/png"
                    elif getattr(part, "inline_data", None) is not None:
                        data, mime = part.inline_data.data, part.inline_data.mime_type
                    elif isinstance(part, dict):
                        data, mime = part.get("data"), part.get("mime_type")
                    if data and str(mime).startswith("image/"):
                        service = get_service()
                        if not get_cloud_vision_enabled() or not service.cloud_vision_enabled:
                            raise ProviderError("permission_denied", "Cloud Vision is disabled; no image was sent.")
                        approval = service.review_image(data, mime, question=question,
                            provider=f"{provider_label(self.profile['provider'])} ({self.profile['model']}) at {self.profile['base_url']}")
                        reviews.append((approval, data, mime, question))
                for approval, data, mime, question in reviews:
                    if not get_cloud_vision_enabled() or not get_service().cloud_vision_enabled:
                        raise ProviderError("permission_denied", "Cloud Vision permission changed; no image was sent.")
                    get_service().consume_image_approval(approval, data, mime, question)
            response = self._generate_content(contents=contents, model=model, config=config)
            completion = "completed"
            return response
        finally:
            for approval, *_ in reviews:
                get_service().finish_image_review(approval, completion)

    def _generate_content(self, *, contents, model=None, config=None):
        from core.providers import ProviderError, create_adapter
        from core.provider_runtime import is_live_profile
        from core.task_control import _origin
        import threading
        key = self.store.credential(self.profile["id"])
        current = self.store.get_profile(self.profile["id"])
        if (current is None or key != self._credential or
                any(current.get(name) != self.profile.get(name) for name in ("provider", "base_url", "auth_type"))):
            raise ProviderError("credentials_revoked", "The provider destination or credential changed; start a new task.")
        if not key and self.profile.get("auth_type") != "none":
            raise ProviderError("missing_credentials", "The selected tool provider needs a credential.")
        options = config.model_dump(exclude_none=True) if hasattr(config, "model_dump") else (config or {})
        if not isinstance(options, dict):
            raise ProviderError("invalid_request", "Invalid model request options.")
        origin = _origin.get()
        cancel = origin[0].event(origin[1]) if origin else threading.Event()
        if cancel.is_set():
            raise ProviderError("cancelled", "The originating task was cancelled.")
        # Existing Gemini Live installations use separate Gemini text models inside tools.
        if self.profile["provider"] == "gemini" or options.get("tools") or options.get("response_modalities"):
            if self.profile["provider"] != "gemini":
                raise ProviderError("unsupported_capability", "This tool uses a Gemini-specific grounding or output feature. Configure an explicit Gemini task override to use it.")
            from google import genai
            from google.genai import types
            with genai.Client(api_key=key, http_options=types.HttpOptions(timeout=45000,
                    retry_options=types.HttpRetryOptions(attempts=1))) as client:
                response = client.models.generate_content(model=model if is_live_profile(self.profile) else self.profile["model"],
                    contents=contents, config=config)
            if cancel.is_set() or self.store.credential(self.profile["id"]) != key:
                raise ProviderError("cancelled", "The task or credential ended; late output was discarded.")
            usage = getattr(response, "usage_metadata", None)
            self.store.record_status(self.profile["id"], connected=True,
                usage=gemini_usage(usage), expected_profile=self.profile, expected_credential=key)
            return response
        texts, images = [], []
        from PIL import Image
        for part in contents if isinstance(contents, list) else [contents]:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, Image.Image):
                output = io.BytesIO()
                part.save(output, "PNG")
                images.append({"data": output.getvalue(), "mime_type": "image/png"})
            elif isinstance(part, dict) and "data" in part:
                images.append(part)
            elif getattr(part, "inline_data", None) is not None:
                images.append({"data": part.inline_data.data, "mime_type": part.inline_data.mime_type})
            elif getattr(part, "text", None):
                texts.append(part.text)
            else:
                raise ProviderError("unsupported_input", "This input type is not supported by the selected provider adapter.")
        if any(not image.get("mime_type", "").startswith("image/") for image in images):
            raise ProviderError("unsupported_input", "Audio/file transcription needs a separately supported speech or file service.")
        system = str(options.get("system_instruction", ""))
        if options.get("response_mime_type") == "application/json":
            system += "\nReturn one valid JSON value only, without Markdown fences."
        adapter = create_adapter(self.profile, key or "")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ProviderError("invalid_thread", "Synchronous AI tools must run on the existing background worker.")
        response = asyncio.run(adapter.request([{"role": "user", "content": "\n".join(texts)}], system=system,
            images=images, cancel_event=cancel, max_tokens=options.get("max_output_tokens", 2048), timeout=45))
        if cancel.is_set() or self.store.credential(self.profile["id"]) != key:
            raise ProviderError("cancelled", "The task or its credential ended; late output was discarded.")
        self.store.record_status(self.profile["id"], connected=True, usage=response.usage,
                                 expected_profile=self.profile, expected_credential=key)
        return SimpleNamespace(text=response.text, usage_metadata=response.usage,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text=response.text)]))])
