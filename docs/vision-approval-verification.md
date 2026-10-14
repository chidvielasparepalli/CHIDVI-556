# Screenshot approval repair — 14 October 2026

Workspace: `D:\CHIDVI-556`. Existing changes were retained. No commit, push,
deployment, dependency installation or change to saved Cloud Vision consent.

## Cause established by the trace

The text “Cloud analysis requires approval of this exact image in the desktop
preview” was emitted after unsuccessful review, not when an approval became
pending. `OperatorPanel.review_image` had already executed its `finally` cleanup:
mark done and dismiss the dialog. `vision.analyze_capture` then replaced every
review failure with that same instruction. No event reopened the dismissed image.
Consequently the message referred to a preview that was no longer available.

There were additional reproduced defects: fixed 720×540 geometry put approval
controls outside a high-DPI logical screen; dialog construction exceptions left
the worker waiting; the preview lacked explicit activation or a reopen control;
and failed foreground restoration could turn an explicit approval into rejection.
The retained incident journal discarded the underlying exception, so the trigger
for the original unsuccessful review cannot be identified retrospectively.

The fix uses the existing desktop service, Qt operator panel, vision service and
agent task dispatcher. It adds exact immutable image/task/operation identity,
queued rendering with visible acknowledgement, foreground/resizable preview,
single-use consumption, explicit errors and status, stale/task/observation
invalidation, and bounded provider calls. The privacy guard remains enforced.

## Real application result

Launched `main.py --diagnostic-mode --vision-acceptance`. The user confirmed that
they clicked **Approve This Image Once** in the actual application's preview.

- Captured image: real 1920×1080 PNG, display 1; no image saved by this workflow.
- Image ID: `edd6c184ee5345a28b1ec4a30abc648c`.
- Originating task: `c352891e0dc741448a9b291098cd9d07`.
- SHA-256: `ec64e1574c287b677216f59e60677d3be5d3438553e797cef190edace577e072`.
- Recorded sequence: pending → visible → approved → consumed → completed.
- One approval consumption and one provider invocation, configured for one
  attempt with a 45-second timeout.
- Google Gemini returned `ok=true`, `captured=true`, `analyzed=true`, describing
  the CHIDVI-556 dashboard, navigation, Activity Console, and visible controls.
- The approval ID, hash and task ID match the result from the normal
  `JarvisLive._execute_tool_for_task` / `screen_process` dispatcher.
- The description was displayed in CHIDVI's Activity Console.
- Application PID 33708 reported `ready=true`, `repair_ready=true`; mode diagnostic.

Evidence: `logs/vision-acceptance.json`, `logs/vision-approval-runtime.log`, and
`logs/repair/runtime/process-33708.json`. The health record's provider connection
flag refers to the inactive conversational Live session, not the completed image
API request. Do not publish the health token or raw private diagnostic records.

## Automated verification

- **179 focused tests passed**, covering the approval contract, real Qt worker
  signals/dialogs, exact-byte upload, reject/cancel, stale image/task responses,
  preview failures, duplicate handling, observation stop, consent revocation,
  result dispatch, screen capture and activity discovery routing.
- **423 full-suite tests ran: 421 passed, one failure, one error.** The two
  failures are unrelated existing tests requiring the already-deleted
  `Personality/goku.py` and `Personality/frieren.py`. Those deletions were preserved.
- All **15 changed Python files compile**.
- `git diff --check` passes. Git's existing CRLF warning on
  `tests/test_personality_voice.py` is unchanged.
- Synthetic Qt previews were inspected at normal and 400×350 logical sizes.
  These are test artifacts, not real desktop screenshots.

Commands and output: `logs/vision-approval-focused-tests.log` and
`logs/vision-approval-tests.log`.

## Files changed by this fix

Implementation:

- `main.py`
- `actions/computer_control.py`
- `core/desktop_control.py`
- `core/operator_ui.py`
- `core/task_control.py`
- `core/vision.py`
- `core/vision_acceptance.py` (new opt-in live check)
- `core/repair_observation.py`

Tests:

- `tests/test_desktop_control.py`
- `tests/test_image_approval.py` (new)
- `tests/test_operator_ui.py`
- `tests/test_repair_observation.py`
- `tests/test_screen_understanding.py`
- `tests/test_task_control.py`
- `tests/test_vision.py`

Documentation: `docs/screen-understanding.md` and this file.

## Operational notes and limits

The foreground preview appears above the active CHIDVI or Self-Repair window.
The bottom toolbar's **Review screenshot** button can bring a pending preview
forward again. Reject/Cancel/close send nothing. Expired requests require a new
capture; old approvals cannot authorize it. Cloud Vision remains enabled as the
user configured it; each image still requires its own approval.

The successful live check used the real application's diagnostic mode and normal
tool dispatcher. Conversational Gemini Live speech/chat, phone control, additional
physical monitors and live visual clicking were not retested. External computer-use
automation was unavailable; consent was provided by the user in the app. The
already-running normal app instance must be restarted to load the changed Python
modules. The verified diagnostic instance remains open.

Other image tools that do not call this shared vision/review path were outside
this focused fix. No compatible local vision backend was added.
