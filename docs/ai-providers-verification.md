# Provider implementation verification — 14 October 2026

Workspace: `D:\CHIDVI-556`. Existing dirty/untracked work and the pre-existing Goku/Frieren source deletions were preserved. No commit, push, reset or deployment was performed.

## Automated checks

Final command: `.venv\Scripts\python.exe -m unittest discover -s tests -q`.

**677 tests ran; 675 passed.** The remaining failure/error are existing personality fixtures:

- `test_goku_is_discovered_can_be_previewed_canceled_and_selected`: the pre-existing deleted `Personality/goku.py` is not discovered.
- `test_valid_existing_portraits_are_preferred`: the pre-existing deleted `Personality/frieren.py` yields `KeyError: 'frieren'`.

The final run had no provider, credential, vision-consent, tool-routing or task-cancellation failures. Earlier intermittent QR-decoding and approval-expiry checks were investigated; QR production code was left untouched, and approval expiry tests now use their existing injectable clock. Both passed in the final run. Full output: `logs/provider-final-suite.txt`.

Provider tests cover native protocol payloads and streaming, model metadata/discovery, tools/results/continuation, malformed responses, blocked output, rate limits, cancellation, whole-batch validation, duplicate IDs, bounded approval resumes, secret redaction/rotation, recipient changes, DPAPI recovery/migration, UI workers, saved status, first-run setup, task/voice coexistence and selected vision routing. External providers use mocked HTTP transports for these tests. Native DPAPI round trips use isolated temporary vaults.

Compilation passed for `main.py`, `ui.py`, `core`, `actions`, `plugins`, `memory` and `tests`. `git diff --check` passed.

## Real account and desktop checks

| Check | Observed result |
| --- | --- |
| Existing Gemini key migration | Windows desktop-account DPAPI encryption, readback and encrypted recovery verified; plaintext legacy field removed; all other configuration values preserved |
| Gemini catalogue | 63 models returned; configured `gemini-flash-lite-latest` found |
| Synthetic text probe | Passed; 14 returned tokens; no files or desktop images sent |
| Shared provider runtime + Gemini REST | Passed: two requests, one validated `verify_sum(17,25)` call, returned result 42, final response `Verified sum: 42`; 373 reported tokens |
| Actual CHIDVI launch | Visible main window and AI Providers dialog, Gemini Live connected |
| Original desktop task queue | Synthetic `PROVIDER_READY` response received and original task marked succeeded |
| Provider UI | Actual dialog image inspected locally; masked/empty saved-key field, provider/model controls and scrolling layout usable |
| Privacy during check | Screen sharing OFF; microphone and phone dashboard disabled in diagnostic check mode |

Evidence files: `logs/provider-api-check.json`, `logs/provider-runtime-live.json`, `logs/provider-desktop-check.json`, `screenshots/provider-desktop-settings.png`. The synthetic runtime check preserved saved profiles and role assignments. Its real request exposed a Gemini function-schema mapping error; using the documented `parametersJsonSchema` field fixed it, and a regression test now covers that field.

DPAPI is Windows-user-bound. Migration was verified under the same desktop account that launches CHIDVI, not just the separate test-sandbox account. The successful encrypted recovery copy is under `config/provider_recovery/`; no key values were printed.

## Unverified or intentionally unsupported

OpenAI, Anthropic, xAI, DeepSeek, NVIDIA, OpenCode and custom endpoints require their own locally entered credentials for real account tests. Their protocol adapters have automated coverage, not live-account certification. No new speech synthesis or embedding transport was added; Gemini Live and existing voice-pack services remain independent. Prices are unknown unless trustworthy metadata exists; token warnings are not a spending cap.

No real screenshot analysis, microphone/audio-device exercise, phone-device action or administrator operation was performed during this provider verification. Earlier continuous-screen-sharing acceptance still requires starting a new session in the app; previous session approval does not authorize a new one.

The application left open is the diagnostic provider-check instance. It supports typed chat and settings; launch normally to use the microphone and phone dashboard.
