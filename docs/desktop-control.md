# Desktop and browser control

The existing CHIDVI Gemini Live session still plans and dispatches tools. `core/desktop_control.py` supplies physical-coordinate input, observation metadata, feedback and cooperative cancellation; `actions/computer_control.py` retains its existing tool API. `actions/browser_control.py` retains the existing Playwright worker thread and adds structured inspection and unique semantic targets. Neither component creates a new AI agent.

## Start and browser connection

From PowerShell in `D:\CHIDVI-556`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Playwright was declared in the project but was missing from the audited virtual environment. Installing requirements restores that declared dependency. Using the installed Chrome/Edge channel does not require downloading a Playwright Chromium build.

Without a CDP setting, ordinary navigation opens the normal browser. The first interactive browser action opens a **separate CHIDVI profile** under `%USERPROFILE%\.jarvis_profiles`. It cannot see tabs, cookies or sign-ins from the normal profile. Sign in manually in that automation window if needed. User profile files are not copied, modified or unlocked.

To attach to an explicitly launched debug-enabled Chrome, use a separate profile and a loopback debugging port. Substitute the actual installed executable location if needed:

```powershell
$chromePath = Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'
Test-Path -LiteralPath $chromePath
& $chromePath --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 "--user-data-dir=$env:LOCALAPPDATA\CHIDVI\Chrome-Control"
$env:CHIDVI_CHROME_CDP = 'http://127.0.0.1:9222'
.\.venv\Scripts\python.exe main.py
```

These are user-run setup commands; automated tests do not launch Chrome. No firewall change is needed for loopback CDP. Do not expose this debugging port to the LAN/internet: it grants browser access. `CHIDVI_CHROME_CDP` rejects non-loopback hosts and embedded credentials. Closing the CHIDVI connection does not close the user's attached CDP browser. Bring the intended tab forward when multiple tabs are open.

The connection uses [Playwright's supported CDP API](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp). Semantic targets use [Playwright locators](https://playwright.dev/python/docs/locators). Inspection returns visible main-document controls with names, roles, associated descriptions, placeholders, states and CSS bounding boxes. Values of form fields, page body text and full page titles are omitted. URLs are reduced to origins because paths, queries and fragments can carry credentials. Iframes/shadow roots and unexposed native controls require manual assistance; the current implementation does not claim complete accessibility-tree coverage.

## Input and verification

Desktop `x/y` are integer **physical pixels**, including negative coordinates for monitors to the left/above the primary display. Points in monitor gaps or outside displays are refused. Each thread temporarily uses Windows physical DPI coordinates; image-to-screen mapping keeps source size and region origin. Do not supply resized screenshot coordinates directly.

Every central desktop click waits for successful synthesis/playback of exactly **“I am clicking here.”**, then waits for the transparent overlay acknowledgement before injecting input. Missing speech or overlay support refuses the click. Double-click is one requested operation with one confirmation phrase and a two-event native double-click sequence. Drag presses are also announced and highlighted; cancellation releases held buttons. Synthetic input can be distinguished by some applications.

Browser semantic clicks speak the same phrase and draw a pointer-transparent ring inside the page at the freshly measured control bounds. They do not move the system cursor: CSS viewport coordinates alone cannot establish a calibrated OS position. Use explicit physical-coordinate input when visible cursor movement is required. No browser-chrome/window-offset guess is used.

Browser actions refuse multiple matches, credential/payment fields and human-verification challenges. Sensitive named actions and submit controls use the existing desktop confirmation gate. A pending approval captures the original target and checks it again before execution. Page/target changes during speech require fresh inspection. Timeouts cancel pending browser coroutines; no click retry is automatic. A dispatched click is reported separately from verified task success. The browser captures resulting semantic state; the current agent must assess whether the intended result occurred. Raw desktop clicks cannot infer task success from cursor movement alone.

`computer_control` adds `observe`, `mouse_down`, `mouse_up` and declared drag parameters. `observe` reads only foreground/monitor metadata. Text entry results report character counts; tool logs do not echo typed values. Missing remembered identity values ask the user instead of inventing data. `random_data` remains available for explicitly requested test data.

## Capture and visual fallback

Screenshots are not continuously captured by this service. `screen_process` with `action: capture` captures and validates an image locally in memory with Cloud Vision off. It supports display indices `1..N` and `0` for the entire virtual desktop, including negative monitor origins. Explicit screenshot storage still requires `desktop_capture_enabled: true` in the existing `config/api_keys.json`; do not replace that file or publish its contents.

Image interpretation and visual targeting share the saved **Customize → Vision & Privacy → Cloud Vision** setting (`cloud_vision_enabled`, default false). The toolbar checkbox changes the same setting. The old `desktop_external_vision` flag is no longer needed for explicit image analysis. `vision_model` optionally selects the existing Gemini vision model. Using that configured provider can incur its normal API charges. See [screen understanding](screen-understanding.md) for configuration, structured failures and verification evidence.

External analysis sends the requested screenshot to the configured Gemini provider **only after a local preview of that exact image receives explicit approval**. The cloud checkbox alone never approves an upload. Review the preview for passwords, codes, tokens and other private content; decline and hide that content before requesting a new capture. Missing review UI, a declined/closed preview, cancellation or cloud-permission revocation refuses the upload. The same immutable bytes that were reviewed are sent; the application does not recapture a changed screen after approval.

Locked/sensitive foreground contexts are refused, and vision responses identifying sensitive controls are refused. Those checks are heuristic and are **not a complete local redaction system**. Per-image user review is the concrete privacy gate; it does not claim automatic secret detection or redaction. Cloud analysis stays disabled by default. Screenshots used by the visual finder remain in memory and are not saved.

Visual target responses must identify exactly one candidate with reported confidence at least 0.9. Confidence is model-reported, not measured pixel accuracy. After image review restores the original window, a local capture must match the approved image hash and foreground identity; those new bytes are used only for comparison and never replace the approved payload. Screen hashes, foreground identity and maximum target age are checked again after inference and immediately before clicking. Time spent reviewing does not age an unchanged, revalidated screen. Changed/animated screens or slow inference can cause a safe refusal. The current visual finder supports one monitor; use semantic inspection or explicit physical coordinates on multiple monitors. There is no newly installed OCR model.

Legacy `form_agent`/`form_agent_v2` background loops are disabled because their untracked threads, guessed coordinates and automatic form submission bypassed shared task cancellation/confirmation. Their tool response directs the existing CHIDVI agent to semantic browser tools. Steam profile colour guesses and installer coordinate guesses also now request manual interaction; accessible Steam targets route through central feedback.

## Events, stop and tests

`bind_navigation(callback)` emits origin/website metadata from Playwright navigation, DOM readiness and tab-visibility events. The opt-in observation controller decides whether to retain the event or offer assistance, including cooldowns and active-task suppression. No screenshot is taken for these notifications.

The main application binds `get_service().bind_feedback(highlight, speak, cancelled)` and `get_service().bind_image_review(review_callback)`. The image callback receives `(data: bytes, mime: str)` and must return exactly `True` only after the user approves that image. `review_image(data, mime)` checks cancellation and cloud consent before and after the preview. Both click-feedback callbacks must return `True` only after completion. Escape/Stop cancellation is checked before input and after blocking speech; drag/typing loops also check between steps. `release_buttons()` can clean up held input. Cancellation cannot undo an input event already delivered to the OS or a network request already sent by a website.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_desktop_control.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_browser_control.py' -v
git diff --check
```

Tests use fake input devices, pages and observations. They do not capture a real screen, click the desktop, contact Gemini, or require Chrome. Real DPI/multi-monitor overlay calibration, audible playback, external vision accuracy and a live CDP connection still require manual acceptance checks with the user's hardware.
