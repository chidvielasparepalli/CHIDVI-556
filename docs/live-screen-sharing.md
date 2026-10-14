# Live Screen Sharing

The real workspace is `D:\CHIDVI-556`. Live sharing extends the existing screen capture, task queue, Gemini conversation, computer-control service, and operator toolbar. It does not replace voice or personality configuration.

The approval loop came from a missing session concept: Cloud Vision allowed the provider, but each `screen_process` invocation still captured a different image and opened a new exact-image review. The Gemini Live connection sent audio only. Both the core prompt and runtime prompt required the image tool before describing anything. Those rules made recurring screen questions return to the one-shot workflow and could make the model say it had no current sight. The new state machine, frame sender and prompt changes address those paths together.

## Controls

Use **Start Live Screen Sharing** beside Cloud Vision in the bottom operator toolbar. Review the selected display, provider and limits in the session disclosure, then press **Start Live Screen Sharing**. Cloud Vision and the Live connection must already be available. No frame is transmitted merely because Cloud Vision is checked.

The toolbar shows OFF, STARTING, **LIVE**, PAUSED, STOPPING or ERROR and the latest capture time. **Pause**, **Resume**, and **Stop Sharing** are adjacent. Resume is a desktop action, so a model cannot silently undo a user pause. The model can report status, pause, or stop through `live_screen_sharing`.

The emergency shortcut defaults to **Ctrl+Alt+Escape**. Change it in Self-Repair's shortcut field. A single Windows hotkey registrar protects both modes, including when Self-Repair Mode is off. Emergency stop cancels the active automation and ends sharing. A failed Windows registration is visible in the repair status; the Stop button remains available.

**Customize → Vision & Privacy** contains sharing limits and **Start Live Sharing with CHIDVI**. Startup sharing is off by default and requires its own explicit disclosure. An enabled preference starts one session after connection on a future normal launch. It does not restart a manually stopped or failed session during that process.

## Capture, analysis and authorization

`core/screen_sharing.py` owns one in-memory full-resolution physical-pixel frame, timestamps, display geometry and the session generation. Background capture never uploads by itself. It checks foreground identity and locked/sensitive-window policy. Monitor 1 is the default; 0 explicitly includes all displays. A task cannot enlarge the selected display scope. No screenshot history is recorded to disk.

`core/live_screen_stream.py` sends bounded JPEG samples with `send_realtime_input(video=types.Blob(...))` on the **existing Gemini Live connection**. It neither creates another conversation nor consumes responses. The existing audio sender and response reader remain responsible for voice. Sampling is static-frame context, not full-motion video understanding.

Ordinary submitted tasks refresh the local frame and wait up to six seconds for matching or newer video context within the stream budget. Voice also receives the continuously sampled context; if that context is stale, the prompt directs the model to request a fresh observation. Screen tools use the same cache with a task-bound, signed frame lease. Live `screen_process` and visual targeting reuse the configured Gemini image-analysis adapter for structured tool results. They skip per-image approval only when the lease remains valid. This adapter is a bounded HTTP request, not another competing Live conversation.

When sharing is OFF, existing one-shot captures still use their exact-image approval preview. PAUSED, STARTING, STOPPING and ERROR cannot silently fall back to uploading through that flow. Camera review remains separate. No compatible local vision model was installed during implementation; local capture alone is not image understanding.

Stopping or pausing revokes all existing frame leases immediately, clears current pixels and delivery metadata, and prevents new dispatches. STOPPING remains visible until already dispatched operations drain. A request already sent cannot be recalled from the provider; late results cannot authorize actions or become successful task results. A synchronous image request has a 45-second timeout and no automatic provider retry. Reconnection does not silently restart sharing. Conversation history at the provider can still contain earlier images; application state marks them historical, and tools refuse their use as a current observation.

Visual clicks capture again before localization and immediately before input, including after speech and the click indicator. Display scope, task identity, foreground, pixels and sharing permission are rechecked. Sensitive-action confirmation policy remains enforced independently. Animated or ambiguous controls may be refused; input dispatch alone is never reported as verification.

## Defaults and usage

| Setting | Default | Allowed |
|---|---:|---:|
| Background capture interval | 2 seconds | 1–30 seconds |
| Stream uploads | 12/minute | 1–60/minute |
| Requested analysis/control uploads | 4/minute | 1–20/minute |
| Video dimensions | 1280 × 720 | configurable up to 1920 × 1080 |
| Encoded video frame | at most 512 KiB | hard cap |
| Change detection | on | on/off |
| Unchanged-frame heartbeat | 30 seconds | fixed |
| Session length | 30 minutes | 1–120 minutes |
| Display | 1 | 0–64, must exist |
| Startup sharing | off | explicit saved opt-in |

Stream and requested-analysis quotas are separate. At defaults, a continuously changing desktop can send at most 360 video frames during a 30-minute session, plus up to 120 user-requested analysis/control images. Requested image analysis uses full-resolution pixels. Unchanged frames are suppressed except for the heartbeat; there is no upload backlog. Sampling and requests are bounded, but these are not a currency-denominated billing cap.

The installed `google-genai` SDK is 2.30.1 and the configured Live model is `models/gemini-3.1-flash-live-preview`. Google's [Live documentation](https://ai.google.dev/gemini-api/docs/live-api/get-started-sdk) specifies the video input used here; [capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities) limit video to one frame per second. The implementation stays below that limit.

Google's [pricing page](https://ai.google.dev/gemini-api/docs/pricing) lists image/video input for this Live model at $1 per million tokens at implementation time. Audio, output, and separate image-analysis requests add charges. Frame count alone does not determine tokens or cost; use provider usage metadata and account billing, and lower limits for restricted plans. No model was downloaded and no provider/model selection was silently changed.

## Verification

Run the focused tests with the project interpreter:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_screen_sharing tests.test_live_screen_stream tests.test_live_vision tests.test_live_sharing_ui tests.test_live_sharing_runtime tests.test_live_sharing_integration tests.test_desktop_control tests.test_image_approval tests.test_repair_observation tests.test_repair_hotkey
```

For a real check through the actual desktop, existing Live receiver, and normal task dispatcher:

```powershell
.\.venv\Scripts\python.exe main.py --diagnostic-mode --live-sharing-acceptance
```

This connects the configured Gemini Live conversation but disables microphone, dashboard and proactive background actions for the check. It waits for the user to press Start and accept the session disclosure. It never approves or starts sharing automatically. Two harmless foreground windows display different randomly generated words/numbers and colors. Ordinary screen questions check the actual answers; the report records whether `screen_process` was used, distinguishing native Live video from HTTP interpretation. A reversible test button verifies real control. Pause, Resume and Stop then check capture/transmission counters and cleanup. STOP cancels only the check's owned task.

The report is `logs/live-sharing-acceptance.json`; it contains metadata and returned text, not screenshots or credentials. Passing unit tests or reaching LIVE does not establish successful image interpretation. Read this report for the actual runtime result. A waiting or incomplete report is not live-vision success.

### Verification recorded on 2026-10-14

- Full suite: **539 tests**, **537 passed**, with the two pre-existing missing-personality failures (`frieren` and `goku`). See `logs/live-sharing-final-suite.txt`. These deleted files were not restored or replaced.
- After the final unchanged-pixel target-refresh fix and configurable video-size check, **120 focused tests passed**, covering desktop control, sender, runtime integration, existing voice/personality behavior, task orchestration and screen dispatch.
- `compileall` for `main.py`, `core`, `actions`, `memory`, `tests`, and `git diff --check` passed.
- The actual updated PyQt application was launched, connected to the configured Gemini Live model, and reported a visible, ready UI with a healthy provider connection. During idle testing the provider sometimes closed an unused connection with code 1008; the existing reconnect path recovered. Sharing does not automatically restart on reconnection.
- At the recorded checkpoint the acceptance report was **waiting_for_start**, with zero image-review events and no test frame submissions. The user must activate sharing through its visible disclosure. Native Live frame interpretation, refreshed-window answers, real mouse input, and the real pause/resume/stop cycle are **not yet verified**. Offscreen UI tests, synthetic images and mocked APIs do not substitute for that result.
- Physical multi-monitor/mixed-DPI behavior and the Windows emergency key chord have automated coverage; the complete hardware checks remain outstanding. No installed local vision backend was claimed or downloaded.

## Files in this change

Production: `main.py`, `core/prompt.txt`, `core/screen_sharing.py`, `core/live_screen_stream.py`, `core/vision.py`, `core/operator_ui.py`, `memory/config_manager.py`, `actions/computer_control.py`, `core/desktop_control.py`, `core/repair_ui.py`, `core/repair_runtime.py`, `core/repair_observation.py`, `core/live_sharing_acceptance.py`.

Tests: new `test_screen_sharing.py`, `test_live_screen_stream.py`, `test_live_vision.py`, `test_live_sharing_ui.py`, `test_live_sharing_runtime.py`, `test_live_sharing_integration.py`; extended `test_desktop_control.py`, `test_vision_settings.py`, `test_repair_ui.py`, `test_repair_runtime.py`, `test_repair_observation.py`.

Existing unrelated modifications, recovered assets, README work and personality deletions remain preserved. No commit, push, reset or deployment is part of this change.
