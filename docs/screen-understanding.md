# Screen understanding and activity-console repair

Implemented in `D:\CHIDVI-556` on 13 October 2026. The existing working tree,
README, voice/personality changes and untracked video were preserved. No commit or push.

## Root causes

- Action and plugin discovery sent successful loads and summaries through the same
  callback used for activity messages. Main also printed the callback output.
- `screen_process` checked an in-memory cloud flag before taking a screenshot.
  The flag started false on every launch and had no persisted settings control.
  Capture and analysis were coupled. The old tool acknowledged a queued image,
  then uploaded it on a later Live turn, so it could not return the analysis or
  its failure in the original tool response.
- App-builder had the expected callable and compatible parameters but omitted
  `TOOL["handler"]`. Discovery correctly rejected the incomplete declaration.

## Settings and use

The actual desktop path is **Customize → Vision & Privacy → Cloud Vision**.
The toolbar **Cloud Vision** checkbox controls the same setting. The dialog
explains external image analysis and the separate image-review requirement.

Consent defaults to false. Only JSON boolean `true` enables it. Both controls
save `cloud_vision_enabled` in the existing `config/api_keys.json`, and startup,
`screen_process` and visual targeting read that source. Writes preserve the
other settings, use atomic replacement, and leave malformed/unwritable files
untouched with a visible error. Disabling takes effect immediately; in-flight
results are discarded and pending uploads refused. An upload already sent to
the provider cannot be recalled.

Ask JARVIS to look at the screen after enabling Cloud Vision. It captures the
requested display, opens a foreground preview, and waits for **Approve This Image Once**.
Inspect the whole image, using **Full size** if needed. Cancel and hide any
passwords, codes, tokens or other private content before making a new request.
The checkbox alone does not authorize a screenshot upload. The exact approved
bytes go to the existing configured Gemini provider; no image is saved by this
path. Foreground privacy checks remain heuristic, not automatic redaction.

The `screen_process` tool accepts:

| Field | Behavior |
|---|---|
| `action: "capture"` | Explicit capture and validation in memory; no interpretation or upload |
| `action: "analyze"` (default) | Capture, check backend/consent, review, then return interpretation |
| `monitor: 1` (default), `1..N` | Select a physical display |
| `monitor: 0` | Capture the virtual desktop including all displays |
| `angle: "screen"` (default) / `"camera"` | Select the capture source |
| `text` | Question about the image |

Capture validates image bytes, decoded dimensions, physical monitor bounds and
nonblank content. It runs under the existing per-thread physical DPI context.
Locked/sensitive foreground contexts are refused. Explicit local capture needs
no cloud opt-in. Saving screenshots through `computer_control` retains its
separate `desktop_capture_enabled` permission. Cloud visual targeting uses the
new shared saved consent instead of the old hidden `desktop_external_vision` flag.

No observation mode or continuous screenshot capture is enabled by this change.
Existing task Stop/cancellation and observation pause controls remain in place.

## Local and cloud backends

The read-only environment audit found MSS, Pillow, OpenCV, torch and ONNX Runtime,
but no compatible installed/configured local image-understanding model. There
were no Ollama/LM Studio/llama executables, relevant running local endpoints,
local vision weights or configured local vision adapter. The cached
`faster-whisper-base` model handles speech, not images. Generic tensor/image
libraries and a configured text-model name do not establish vision capability.

Consequently this build reports `local_backend.available: false`. It does not
auto-install or download models. Local interpretation would require an actual
multimodal model and runtime, verified image-input support and a tested local
adapter. Setting an arbitrary local model name alone does not enable it.

Cloud interpretation reuses the configured `gemini_api_key` and optional
`vision_model`, with the existing `gemini-flash-lite-latest` fallback. No new
provider or dependency was added. The provider request has a 45-second timeout
and one attempt. Failures and empty responses return structured errors without
raw provider payloads or credentials. The existing Gemini Live agent receives
the description in the same tool result and speaks it using the existing
personality/voice pipeline.

## Exact-image approval repair (14 October 2026)

The reported "requires approval" message was not a pending-approval event: it
was emitted only after review had returned unsuccessfully. By then the Qt
callback's `finally` block had already marked the request done and queued the
dialog for dismissal. Nothing reopened that dismissed preview. This explains
why the Activity Console could instruct the user to approve an image while no
approval interface remained available.

The old service returned only a boolean. Its Qt callback created a fixed-size,
modeless preview with `show()` but no foreground activation or accessible reopen
control. At high DPI its buttons could fall outside the available screen. A Qt
construction exception did not wake the waiting worker. Timeout, rejection,
missing reviewer and callback errors were all rewritten as the same misleading
"requires approval" message. The retained incident log contains that generic
message only, so it cannot identify the original runtime trigger precisely.

The existing operator toolbar now has **Review screenshot** and a persistent
review status. Requests travel through an explicitly queued Qt signal and must
receive a visible-preview acknowledgement within five seconds. The resizable
preview opens over the active CHIDVI window (including Self-Repair), restores a
minimized owner, and keeps its action buttons within the available logical screen
geometry. The screenshot, originating request, task ID, operation and configured
cloud destination are shown together. **Reject**, **Cancel**, Escape and close
authorize no upload. Focus restoration is best effort and cannot revoke consent.

The shared desktop service binds each approval to a unique request ID, immutable
bytes, SHA-256, MIME, originating task, question, and 180-second deadline. The
provider boundary consumes it once. Replacement requests expire the old preview;
duplicate pending requests reuse its identity without opening another dialog.
Task changes, cancellation, consent revocation and observation pause/stop revoke
pending authority. An observation image is frozen for explicit review; the preview
taking focus does not invalidate those pixels. No screenshot is saved by this path.

Failures now distinguish unavailable/failed preview, rejected/cancelled review,
and expiry. Activity shows waiting, upload, completion or the actual failure;
identical approval notices are suppressed within the task. The original tool
returns the description only after successful analysis. Other image-processing
tools that do not use this shared review service are outside this focused fix.

For a manual integration check, run:

```powershell
.venv\Scripts\python.exe main.py --diagnostic-mode --vision-acceptance
```

This launches the actual application, captures one real desktop image through
`screen_process`, and waits for the user to approve its preview. It never approves
automatically. The response appears in the Activity Console and a metadata/result
record in `logs/vision-acceptance.json`; no image or credential is written there.
Normal audio and dashboard connections are inactive during this check.

## Results and logging

Tool results distinguish `captured` from `analyzed`; only successful analysis
contains a `description`. When no backend is available the result contains
`ok: false`, `captured: true`, `analyzed: false`, `code: "backend_unavailable"`,
and `next_action: "Customize -> Vision & Privacy -> Cloud Vision"`.
The task records failure, not success. Tool/system instructions tell the agent
to report the real failure and supplied next action, avoid inventing screen
contents or settings, and not infer browser capabilities from a vision error.
These instructions constrain responses; tests cannot prove a live model will
never hallucinate. Identical disabled-backend activity notices are suppressed
until consent changes; every tool caller still receives its structured result.

Successful discovery and summary diagnostics now use `DEBUG` on
`core.action_loader` and `core.plugin_loader`. Actual rejections/import failures
remain visible in activity, with diagnostic details/tracebacks in the existing
process stderr log. No file log sink existed and none was invented. The duplicate
`print` callback was removed. To see detailed discovery while launching:

```powershell
$env:CHIDVI_LOG_LEVEL = 'DEBUG'
.\.venv\Scripts\python.exe main.py
```

App-builder now assigns its existing callable to `TOOL["handler"]`. Invalid
actions still produce diagnostic warnings and do not stop discovery.

## Changed files

| Area | Files |
|---|---|
| Dispatch and grounded response | `main.py`, `core/vision.py`, `core/prompt.txt` |
| Capture and visual targeting | `actions/screen_processor.py`, `actions/computer_control.py` |
| Saved consent and real UI | `memory/config_manager.py`, `core/operator_ui.py`, `ui.py` |
| Discovery diagnostics | `core/action_loader.py`, `core/plugin_loader.py`, `core/diagnostics.py` |
| Builder contract | `actions/app_builder.py` |
| Regressions | `tests/test_discovery_logging.py`, `tests/test_app_builder.py`, `tests/test_screen_capture.py`, `tests/test_screen_understanding.py`, `tests/test_vision.py`, `tests/test_vision_settings.py`, `tests/test_desktop_control.py`, `tests/test_task_runtime.py`, `tests/test_personality_voice.py` |
| Guides | This file, `docs/desktop-control.md`, `docs/operator-upgrade.md` |

## Verification

Real local capture succeeded at **1920 × 1080** with Cloud Vision disabled.
The PNG decoded and matched the display geometry; pixels stayed in memory and
were neither saved nor uploaded. Windows BitBlt failed inside the restricted
sandbox; the same memory-only check succeeded outside it.

A real `JarvisLive` constructor/discovery smoke check registered **19 actions
and 7 plugins**, including app-builder, with **zero activity messages**. It did
not connect to Gemini, start the dashboard or execute actions. The settings
route is exercised through the actual Customize button with temporary config.
Automated model tests use synthetic images and a mocked Gemini API. They check
exact approved bytes, cancellation/revocation at the upload boundary, failed
and empty responses, grounded-result plumbing and absence of invented content
on failure. Temporary config tests cover persistence without touching credentials.

Final complete suite: **258 tests passed**. Compilation of `main.py`, `ui.py`,
`core`, `actions`, `memory`, `plugins` and `tests` passed, as did `git diff --check`.
The suite emitted an existing Starlette/httpx deprecation warning; there were no
failed or skipped tests. The README diff still matches its original 872 additions
and 349 deletions.

The actual Customize overlay and Vision & Privacy dialog were rendered offscreen
with Segoe UI and inspected. Both fit without clipping. A faint save-error label
was corrected and checked for readable contrast on light and dark palettes.
These renders used temporary configuration, with no real consent changes.
Gitignored review artifacts are `screenshots/privacy-customize-offscreen.png`,
`screenshots/vision-privacy-offscreen.png` and
`screenshots/vision-privacy-save-error-offscreen.png`.

Reproduce the automated checks in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q main.py ui.py core actions memory plugins tests
git diff --check
```

Real external-model interpretation, a full spoken Live conversation, camera
capture and mixed-DPI/multiple physical monitors still need live acceptance
checks. No real screenshot was sent to a provider during this work.

Phone pairing is separate: ADB was installed but no phone was connected during
the earlier audit. The user selected Android 11+ wireless debugging and explicitly
deferred connection. Mocked transport coverage remains; no pairing attempt was
made for this repair. Follow [Android setup](android-control.md) when ready.
