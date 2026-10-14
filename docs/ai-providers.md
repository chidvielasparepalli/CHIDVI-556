# AI providers

Open **Customize → AI Providers**. New installations open this setup screen automatically. Choose a provider, enter its masked key, fetch or type a model ID, test the selected capability, and save the profile. Check **Use this profile as the default chat provider**, or use **Roles and optional task overrides → Save assignments**. Only one configured chat profile is required. Saved keys are never filled back into the input.

The main window identifies the selected chat and live voice models. Changes apply between tasks; key removal or rotation revokes current requests immediately. There is no automatic provider failover. Conversation history is kept separately per provider/model/endpoint in memory, while the existing personality, memory, voice packs and task queue remain shared.

## What is implemented

| Provider | Chat and tools | Image interpretation | Voice |
| --- | --- | --- | --- |
| Google Gemini | Native content requests and streaming function calls; existing Live chat retained | Regular vision model through the existing screenshot service | Existing Gemini Live session, microphone, interruption and reference-voice path |
| OpenAI | Native Responses API with tool results and streaming | Supported model families; explicit image probe available | No new OpenAI audio adapter |
| Anthropic | Native Messages API, content blocks and streaming tools | Supported Claude families | No new Anthropic audio adapter |
| xAI | Documented Chat Completions protocol | Model-dependent | No new audio adapter |
| DeepSeek | Documented Chat Completions protocol and tools | Current declared chat models are text-only | No new audio adapter |
| NVIDIA NIM | Hosted Chat Completions endpoint | Declare/test the particular model | No new audio adapter |
| OpenCode | Model-family-specific Responses, Messages, Gemini or Chat Completions routing | Model-dependent | No new audio adapter |
| Compatible endpoint | Configurable Chat Completions URL, API-key/no-auth selection, model and capability probes | Declare/test the endpoint and model | No assumed audio support |

These are transport capabilities, not a claim that every account or model was tested live. Protocol references and limits are in [provider-protocols.md](provider-protocols.md).

**Primary chat handles typed desktop/phone tasks. Gemini Live separately handles live voice conversation.** A non-Gemini chat choice does not silently replace the microphone service or synthesize unsupported audio. The live chat profile must match the Live audio profile when using a dedicated Gemini Live model as primary chat. Regular Gemini text models can instead use the shared request runtime. Gemini native-audio model IDs are recognized as Live models.

The **Vision** role uses a regular image-capable model. Gemini Live image context uses the existing live session. Image uploads still require Cloud Vision and exact-image review, or an active authorized sharing session. Sharing disclosure identifies the configured destinations. Changing provider settings ends sharing and clears startup sharing opt-in; start again to approve the new destinations. No local model was installed by this work.

TTS and embeddings are separate configuration roles, but this release adds no new speech synthesis or embedding transports. Unsupported profiles cannot be assigned to those roles. Existing voice configuration and voice packs remain in Customize. Gemini-specific grounded search, image generation and native file/audio operations report a missing capability with other providers; an explicit Gemini task override can enable those operations. Plain planning/coding tools use the configured chat provider.

All providers use the existing tool registry, task owner, cancellation and permission checks. Tool arguments are validated before dispatch; results return to the originating model. The runtime limits turns and tool batches, binds approval continuations to their original call, and rejects stale task/credential responses. Provider observations never authorize actions themselves.

## Credentials and recovery

`config/providers.json` contains non-secret profiles, role assignments and sanitized status. `config/provider_credentials.dpapi` encrypts keys with Windows DPAPI for the **current Windows user**. Windows credential protection needs no additional Python package. These files and `config/provider_recovery/` are Git-ignored. The phone dashboard has no credential-management endpoint.

On startup, the existing `config/api_keys.json` Gemini key is encrypted, read back, and compared. A complete encrypted recovery copy is verified before only the legacy key field is removed. Other settings are preserved. The original Gemini Live and vision models become separate profiles; the existing Live experience remains the default. **Import existing Gemini settings** retries migration explicitly. A failed migration preserves the legacy configuration and reports an error.

Use **Revoke saved key**, enter a replacement key and **Save profile**, or **Remove profile**. Changing provider or endpoint requires a freshly entered key; an existing credential is not reused at a new destination. DPAPI files cannot be moved to a different Windows account and expected to decrypt. Recovery copies require the Windows account that created them. The implementation has no plaintext fallback for new credentials.

## Usage and limits

The default per-request output limit is 1,024 tokens, configurable from 1 to 16,384. Optional session token warnings use returned usage counters. Warnings are not spending caps; provider billing is authoritative. Prices and context limits remain **unknown** when the provider catalogue supplies no trustworthy metadata. No prices are invented or inferred from model names.

Text, image and tool probes use synthetic content and may consume provider quota. A successful text probe does not establish image, audio or tool support. Model catalogues come from official endpoints when available; manual model IDs remain supported. Unknown model capabilities must be declared from provider documentation and tested. Rate limits, authentication failures, disconnections and timeouts produce safe errors without retrying or switching accounts.

The adapters use the already installed `httpx`, now declared directly in requirements. OpenAI and Anthropic SDKs are not required. Google’s existing SDK stays for Gemini Live and existing native Gemini features.

## Verification controls

`python main.py --diagnostic-mode --provider-check --provider-settings` opens the real application and AI Providers page, connects the selected service, and submits one synthetic `PROVIDER_READY` request through the normal task queue. Microphone input, phone dashboard and automatic sharing are disabled for this check. Results go to `logs/provider-desktop-check.json`. Normal startup retains existing audio and dashboard behavior.

Provider unit tests use isolated settings, encrypted test vaults and mocked HTTP services. Live verification results are reported separately from them. Non-Gemini credentials must be entered locally in the masked UI before those providers can be tested against a real account.

## Changed application files

New shared modules: `core/providers.py`, `provider_config.py`, `credential_store.py`, `provider_runtime.py`, `provider_client.py`, `provider_ui.py`, `provider_acceptance.py`. Integration: `main.py`, `ui.py`, `memory/config_manager.py`, `core/vision.py`, `core/operator_ui.py`, `core/long_horizon_agent.py`, `core/app_builder/engine.py`.

Existing model-assisted callers updated: `actions/code_helper.py`, `computer_control.py`, `computer_settings.py`, `desktop.py`, `dev_agent.py`, `file_processor.py`, `flight_finder.py`, `image_generator.py`, `web_search.py`, `youtube_video.py`; `plugins/form_agent.py`, `form_agent_v2.py`, `game_control.py`. Dependency/ignore changes: `requirements.txt`, `.gitignore`. Provider tests and migration-safe legacy test fixtures accompany these changes. Pre-existing README, personality source deletions, recovered assets, operator/phone/repair changes and voice-pack work were preserved.
