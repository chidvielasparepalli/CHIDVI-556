# CHIDVI operator upgrade — implementation and acceptance report

Validated in `D:\CHIDVI-556` on 13 October 2026. The user selected Android 11+
wireless debugging and then deferred connection: **continue mocked checks**.
No phone pairing, live phone control, public deployment, commit or push was performed.

This report records the initial upgrade. The subsequent [screen-understanding fix](screen-understanding.md)
adds persisted Cloud Vision settings, separate local capture, structured interpretation results,
quiet discovery diagnostics and the app-builder registration repair. Its verification supersedes
the initial test count below.

## Architecture and preservation

The existing `main.py` Gemini Live session remains the main agent. Its existing
action/plugin registries, bounded autonomous planner, personality manager,
memory, reference voice, PyQt6 interface, FastAPI dashboard and QR renderer are
reused. Work runs off the Qt thread; Qt signals deliver display updates.

```text
Desktop text / desktop microphone / authenticated phone
                        |
          TaskStore: identity, queue, status, cancellation
                        |
          Existing Gemini Live agent and action registries
             /                  |                  \
      DesktopControl       BrowserSession        Phone transports
      physical input       Playwright/CDP        ADB / Companion HTTP
      ring + click voice   semantic controls     explicit device selection
             \                  |                  /
                observation / results / confirmation
                        |
                 Qt UI and phone task updates
```

The initial branch was `main`, at `92da5fa` (`feat: add optional Wednesday
reference voice`). The pre-existing modified `README.md` and untracked
`karting_reel_Astra.mp4` were preserved. The README diff still has its original
872 additions and 349 deletions. No stashes, recovered files, personality
profiles, API configuration or voice assets were replaced. No `AGENTS.md`
was found in the repository or its ancestors. No destructive Git command ran.

The baseline had 41 passing tests and one import failure caused by the missing
declared `google-genai` dependency. The environment was also missing declared
Playwright and FastAPI installations. The upgrade adds no runtime requirement
to `requirements.txt`; these declared packages and their dependencies were
installed into `.venv`. Installed versions checked during the work were
google-genai 2.30.1, Playwright 1.63.0 and FastAPI 0.143.0. `pip check` passes.

## Phase results

### Phase 0 — audit

- Traced startup, registries, Gemini Live, voice/personality switching, UI,
  browser worker, screenshot capture, QR pairing and dashboard routes.
- Confirmed the HTTP Companion plugin and ADB plugin are separate mechanisms.
- Found the existing QR implementation in `ui.py`; retained it.
- Found the existing desktop confirmation gate and extended it rather than
  accepting model-supplied approval flags.
- The initial audit found `actions/app_builder.py` missing a callable `TOOL['handler']`.
  The subsequent screen-understanding fix binds its existing handler and verifies discovery/dispatch.

### Phases 1–2 — desktop and browser operator

Changed `core/desktop_control.py`, `actions/computer_control.py`,
`actions/browser_control.py`, `core/action_loader.py`,
`core/long_horizon_agent.py`, `actions/autonomous_task.py` and `main.py`.

Desktop input now shares physical-coordinate validation, foreground checks,
cooperative cancellation, observation and feedback. Existing semantic browser
operations now inspect visible controls, refuse ambiguous matches, recheck
targets after speech/highlighting and return post-action page state. Credential
field values are omitted. Authentication challenges require human intervention.
Failed tool results are no longer automatically treated as successful steps.

Interactive browsing uses the existing Playwright worker with either an explicit
loopback CDP connection or a separate CHIDVI browser profile. It does not attach
silently to the normal Chrome profile. Ordinary navigation still uses the
ordinary browser. Legacy uncontrolled form loops in `plugins/form_agent.py` and
`plugins/form_agent_v2.py` now direct the agent to the tracked browser tools.

Cloud vision defaults to disabled; an explicit saved opt-in now survives restart. Every requested image also requires review in a
local preview before the exact bytes can leave the laptop. Cancel, timeout,
task cancellation or disabling cloud access refuses upload. Visual targeting
checks that the original screen/window is unchanged after review. It refuses
low confidence, multiple plausible targets and stale coordinates.

**Evidence:** 27 desktop and 21 browser tests, plus a real headless Chrome
smoke check against a synthetic intercepted page. The latter found the intended
control, clicked a harmless test button, observed its new label and excluded a
password field value. No website account, ordinary browser profile or model
request was used in that smoke check.

**Limits:** browser inspection covers the main document, not every iframe or
shadow tree. Semantic clicks use a page-local ring and do not move the OS cursor.
Explicit visual targeting currently requires one display; OS coordinates support
multiple displays. Model confidence is a refusal threshold, not measured accuracy.
Post-click state is evidence for the agent to inspect, not automatic proof of the
whole objective. Sensitive-action classification is heuristic; custom controls
still require careful review. User image review does not provide automatic
secret redaction. See [desktop setup and limits](desktop-control.md).

### Phase 3 — visible and audible clicks

Added `core/operator_ui.py` and `core/click_speech.py`; integrated them through
`main.py`, `ui.py` and the central desktop service. Updated
`actions/game_updater.py` to use validated central clicks.

Every central desktop click requires a transparent, input-pass-through ring and
completed playback of **“I am clicking here.”** before input injection. Missing
feedback refuses the click. Focus/lock/cancellation are rechecked after blocking
feedback. Drags release held buttons on cancellation; typing and clipboard
sequences stop if focus changes. Browser clicks use the same exact speech and
a pointer-transparent page ring.

**Real Windows check:** a temporary test button received a physical-coordinate
click after the overlay acknowledgement and local Windows SAPI playback. The
test window was closed afterward. This was a single 1920×1080 monitor; mixed-DPI
and negative-origin mappings were tested with synthetic geometries. Later
focus/cancellation fixes were verified with regression tests.

Conversation keeps the selected personality/reference voice. Click confirmation
uses the configured local reference voice when available, otherwise Windows
SAPI; this fallback uses the Windows default voice/output. It adds no paid TTS
dependency. Synthetic input may be distinguishable from physical hardware input.

### Phases 4–5 — proactive help and observation

Added `core/observation.py`; bound browser navigation and foreground events to
the existing agent. Monitoring starts stopped, AC-only enabled, help offers off.
The persistent panel provides Start, Pause, Stop, Clear, AC-only, Offer help,
reduced motion and cloud-image controls.

Windows foreground events and bounded adaptive metadata polling maintain local
application/site context. Lock/sensitive contexts, battery policy, resource
pressure and errors suspend or back off work. Help offers deduplicate app/site
events, respect cooldowns and do not interrupt active tasks or speech. They ask
for intent and cannot authorize tools. Pause/Stop also suppress idle proactive
check-ins.

**Evidence:** 16 observation tests and a real native Windows event-hook
start/stop check. Continuous observation reads metadata only: it does not run
OCR, record video, save screenshots or continuously send pixels to a provider.
Visual analysis is a separate explicit, reviewed operation. This is the main
limit on the requested broad continuous visual awareness. See
[observation controls and configuration](observation.md).

### Phase 6 — microphone and voice

Updated existing `ui.py` microphone controls and `main.py` audio orchestration.
Microphone transmission starts muted. Hold the microphone button for push-to-talk;
release it to stop. Hands-free listening requires the separate visible toggle.
Existing wake-word support and the Audio device chooser remain available.

Own-speech gating suppresses desktop and phone microphone frames during output.
Phone frames are attributed to an authenticated device, checked again before
provider delivery, and stripped of routing metadata. Different microphones do
not mix into one recognizer turn. Switching sources before a turn finishes may
require a clean session reconnect; use one microphone at a time.

Use **Audio** to choose an input/output device. The input level indicator and
error log diagnose missing or disconnected microphones; text remains usable if
input fails. Device changes rebuild the audio session. Escape/Stop interrupts
speech and requests cancellation. Personality changes defer session rebuilding
during an active task.

**Evidence:** existing personality/reference-voice tests, new UI/runtime tests,
and real SAPI click speech. Live Gemini speech recognition, a real phone
microphone, echo performance and every reference-voice/device combination were
not exercised. No continuous disk audio recording was added.

### Phase 7 — phone controller and QR

Repaired `dashboard/server.py`, reused `dashboard/static/app.html` and
`login.html`, and added `controller.js`, `controller.css`, `login.js` and the
owner-run `dashboard/certificates.py` helper. Existing FastAPI, Uvicorn,
cryptography, QR and PCM conversion components are reused.

Old pairing/session/origin controls and broad automatic firewall setup were
replaced with expiring one-use pairing, HttpOnly sessions, strict origins,
CSRF/WebSocket authentication, replay-safe request IDs, bounds/rate limits,
revocation and validated inert uploads. Restarting requires pairing again.
The phone shows chat/history, personality, connection and shared task state,
explicit retry, cancel/stop and optional microphone controls. Reconnection does
not resubmit a command or restart the microphone automatically.

The long random pairing code is carried in the QR URL fragment, expires after
ten minutes by default and is invalidated on use, replacement or dismissal.
The existing QR overlay now wraps the long code and rejects loopback as a phone
address. LAN access requires an assigned private interface and trusted TLS.
The server stays loopback-only by default; no public relay or firewall mutation
is started automatically.

**Evidence:** 25 dashboard tests cover authentication, pairing, origins, replay,
reconnect/cancellation, uploads, certificate generation and audio revocation.
The existing Qt QR rendered and decoded a complete long pairing URL. Mocked
Chrome flows were checked at 320, 390 and 760 pixels, with no browser errors.
No Android browser connection, trusted LAN TLS session or real phone microphone
was tested. Phone screenshot preview is disabled.

**Network audit:** Wi-Fi and Tailscale were Private. No listener on port 8000
was found in the final read-only check. Two pre-existing inbound rules,
`JARVIS Dashboard Port 8000` and `JARVIS Dashboard Python`, allow traffic on
**Any** profile. They were left unchanged. Review those broad legacy rules before
LAN use; the application no longer creates or broadens them.

Follow [phone controller setup](phone-controller.md) when ready. That guide
includes the exact certificate/trust, binding, QR and firewall diagnostics.
No production certificates were generated or installed during this work.

### Phase 8 — wireless Android control

Repaired `plugins/adb_control.py` and `plugins/wireless_control.py`; added
`core/phone_control.py` and kept the Companion interfaces used by game control.

Root causes: the wireless plugin actually uses a Companion HTTP service, not
Android developer options; no Companion APK/source is bundled. Empty settings,
doubled endpoint slashes, ignored remote failures and its broken self-check
masked the problem. The separate ADB plugin assumed PATH discovery and lacked
adequate connection/argument validation.

ADB is installed at
`C:\Users\chidv\AppData\Local\Android\Sdk\platform-tools\adb.exe`,
version 37.0.1. Real `adb devices -l` returned no devices. Discovery now supports
the configured path and standard SDK locations. Operations validate the selected
authorized device, LAN endpoints, timeouts, input arguments and screenshot
destinations. Cancellation is checked at transport boundaries. Pairing codes
are entered only in the user's local ADB prompt, never through the agent.

**Evidence:** 26 mocked phone transport tests and both plugin self-checks;
real executable/version/device-list diagnostics only. ADB transport success
does not prove the resulting phone UI state. Already-started commands may finish
after cancellation. Use [Android setup](android-control.md) later; pairing and
connection ports are different and can change.

### Phase 9 — shared tasks and confirmations

Added `core/task_control.py`, integrated `main.py`, and extended `core/confirm.py`.
Desktop, voice and phone commands share task IDs, a bounded serial queue and
queued/planning/executing/waiting/verifying/terminal state. At most 32 pending
tasks are retained; request identities remain for the process session to prevent
replay. Snapshots include all nonterminal tasks plus the last 100 terminal tasks.

Cancellation belongs to the originating task even if another task becomes
active. Worker threads drain before their cancelled coroutine exits, including
repeated cancellation. Lost delivery requests a fresh connection without
replaying the command. Phone revocation cancels owned tasks and blocks queued
audio. An expired old confirmation cannot cancel a newer one; failed actions
cannot report successful approval. Successful approval returns its result to
the same agent for verification and continuation, without repeating the action.

**Evidence:** 12 task-store tests, 15 orchestration tests, 9 confirmation tests
and 4 autonomous-planner tests. These use the actual runtime methods with fake
sessions/transports. Cancellation is cooperative and cannot undo a delivered
OS event, completed website operation or already-started legacy plugin action.

### Phases 10–11 — checks and handoff

Final complete suite: **204 tests passed**. Compilation, `pip check` and
`git diff --check` passed. Main-module import and action/plugin discovery were
smoke-tested without connecting Gemini or starting the dashboard. Mobile screens
and the local cloud-image review were rendered and inspected. The image-review
test used a synthetic picture, not a screenshot of personal content.

Reproduce the checks or start the existing app in PowerShell:

```powershell
Set-Location D:\CHIDVI-556
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
git diff --check
.\.venv\Scripts\python.exe main.py
```

Use the existing API configuration and personality/voice settings. Text commands
work without enabling observation or cloud vision. Enable local observation only
when wanted. For an image request, enable Cloud vision, inspect its preview
(including Full size when needed), then choose Send this image or Cancel. Never
approve an image containing secrets. Stop the application through its normal
close control; no background helper or publicly exposed service was installed.

The focused guides contain environment variables, browser connection options,
troubleshooting, microphone behavior, Android pairing and TLS trust steps.
Real phone pairing remains deliberately deferred. Multi-monitor hardware,
external vision accuracy, a live CDP account and full Gemini/reference-voice
conversation remain manual acceptance checks; the tests do not certify them.
