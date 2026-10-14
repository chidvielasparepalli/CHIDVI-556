# Desktop self-repair

Open **Self-Repair** in the desktop operator bar. **Self-Repair Mode** starts off
each time the application starts. It enables the repair executor in the existing
task system; it does not start another conversational agent or enable screenshots.
Only one task may own automation at a time.

The actual desktop and repair controls can also run without the conversational
session, audio loop, or phone dashboard:

```powershell
.\.venv\Scripts\python.exe main.py --diagnostic-mode --show-repair
```

Diagnostic mode is not a network sandbox. Explicit image analysis and an approved
dependency installation still use their respective external services.

## Controls

| Control | Implemented behavior |
| --- | --- |
| Self-Repair Mode | Enables repair operations for this process. Turning it off cancels active work and stops observation. |
| Run diagnostics | Runs fixed Python version, dependency, and system diagnostic commands with bounded output and cancellation. |
| Refresh snapshot | Captures locally only while repair observation is enabled. Returns image metadata to the tool, not raw image bytes. |
| Analyze latest image | Explicitly requests analysis of the latest eligible snapshot. External analysis requires saved Cloud Vision consent and review of that exact image. |
| Run isolated repair demo | Creates a deliberately broken action and a real failing test below `logs/repair/demos/`, then repairs and verifies that fixture. It does not break a live application source file. |
| Inspect failure | Displays the selected or most recently recorded failure and its evidence-based diagnosis. Unknown causes remain hypotheses. |
| Open VS Code | Opens the workspace and requested permitted source files in installed VS Code. If VS Code is unavailable, the panel reports that limitation. |
| Restart app | Checks importing `main`, starts a supervised replacement, verifies its UI heartbeat, and commits the handoff before quitting the current process. |
| History / Roll back selected | Shows persisted repair records and restores the latest backed-up attempt for the selected failure, subject to conflict checks. |
| Export history | Writes a local redacted history export to `logs/repair/diagnostic-export.json`. It does not upload anything. |
| STOP REPAIR | Cancels the active task, stops capture, clears the in-memory image, and disables repair mode. |

The panel displays task, phase, diagnosis, command output, changed files, diff,
test results, and restart status. Selecting a history entry opens its bounded
details, including the latest attempt's tests, backup and diff locations, and
rollback results. Output widgets are read-only and bounded.
Command output appears on command completion; this is not an interactive terminal.

The emergency shortcut defaults to **Ctrl+Alt+Escape** and can be changed in the
panel. Windows global STOP uses native `RegisterHotKey` and Qt's native event
filter, with no additional keyboard package. Common single shortcuts using Ctrl,
Alt, Shift, or Win with letters, digits, function keys, Escape, or navigation keys
are supported. Windows can refuse a reserved shortcut or one already registered
by another application; the panel displays that failure and keeps the Qt
application shortcut and STOP REPAIR button available. The global registration
remains active when the repair dialog is hidden and is released on mode off,
shortcut replacement, widget destruction, or application exit. See Microsoft's
[RegisterHotKey contract](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey).
A normal task interrupt also cancels its work and pauses repair observation.

## Observation and image privacy

Repair observation is separately opt-in and visibly marked on the desktop.
Choose a sampling interval from 2–60 seconds and a monitor index: `1` is the first
display and `0` captures all displays. The latest image stays in memory by
default. Captures have task ID, timestamp, hash, dimensions, display geometry, and
minimal foreground application/window metadata. Titles, URLs, and field values
are excluded from retained metadata.

Capture defers when CPU or memory pressure is high. Locked or recognized sensitive
foreground windows are refused, and a foreground change during capture discards
the image. These checks are not automatic pixel redaction: other visible windows
or unrecognized sensitive content can still appear in a screenshot.

**Keep latest screenshot locally** is unchecked by default. If explicitly selected, the most
recent raw image for that task is saved as
`logs/repair/task-<task-id>/latest.png`. Retention uses validated workspace paths
and atomic replacement. Previously retained files remain after stopping; stop
clears the current in-memory image and prevents late captures from being published
or saved. Repair, demo, and rollback completion stop their observation session.

No background capture invokes a cloud model. No compatible local image
interpretation backend is currently connected; foreground metadata, source,
diagnostics, and logs remain usable without one. To request external image
analysis, enable **Customize → Vision & Privacy → Cloud Vision**, then select
**Analyze latest image** and approve its exact preview. Saved permission alone
does not approve an image. Consent, cancellation, image freshness, and foreground
are checked again around the upload. See [screen understanding](screen-understanding.md)
for the shared image-analysis flow.

## Permission boundaries

Class A covers authorized workspace source inspection and edits, diagnostics,
tests, backups, editor use, restart, and separately enabled screenshots. Source
paths must stay within the workspace; credential files, repository/agent metadata,
links and reparse points, and the repair permission boundary itself are protected.

Class B has four saved preferences, all false by default:

- `dependencies`: approved dependency installation.
- `venv`: changes to the project virtual environment.
- `app_config`: nonsecret application configuration changes.
- `model_download`: model downloads.

Only the desktop controls can save these preferences, in
`logs/repair/permissions.json`. They can be revoked individually or with **Revoke
all Class B**. Stopping a task or disabling repair mode retains saved preferences;
neither grants permission for other capabilities. Enabling a permission does not
itself perform an installation or create a virtual environment.

Class C includes elevation, outside-workspace operations, system settings,
unrelated processes, destructive operations, uploads, publishing Git changes, and
persistent services. Such requests return a concrete approval requirement. There
is no general Class C grant checkbox, model-supplied approval bypass, or generic
elevated executor. The existing exact-image review is a separate explicit flow.

Commands use fixed structured presets, not arbitrary shell strings. Dependency
installation is limited to an exactly pinned wheel from PyPI into an existing
project virtual environment after Class B approval. A bounded public PyPI metadata
request must identify a non-yanked wheel compatible with the target interpreter,
a recognized declared license, positive download size, SHA256, and trusted HTTPS
download URL. The target and temporary storage must each have at least five times
the wheel size plus 64 MiB free; this is an installed-disk estimate, not an exact
extracted size. Missing evidence, insufficient space, timeout, or cancellation
blocks installation. The metadata is recorded before installation.

Pip receives the exact `files.pythonhosted.org` wheel URL and hash with
`--require-hashes`, `--no-index`, `--no-deps`, and the project site-packages target.
It does not resolve dependencies automatically. Each normalized package pin has
one attempt per command-service lifetime, including failed preflight or installation;
there is no automatic retry. There is no general virtual environment builder or
model downloader. Tests and imports execute trusted project Python: these
boundaries are not an OS sandbox for hostile source code.

## Repair, verification, and recovery

Recorded action/plugin discovery errors, tool failures, uncaught exceptions, and
explicit malfunction reports can feed the same repair history. Failure evidence
includes the originating task, mode, permission and observation state, and a
bounded redacted recent activity tail. New failures can trigger repair while mode
is enabled. Recording a failure while mode is off does
not silently enable repair or observation.

The built-in automatic recipe handles a missing `TOOL['handler']` when source
inspection identifies one unambiguous existing synchronous handler. Other repairs
need an evidence-backed structured plan; unsupported diagnoses remain blocked.
A plan includes expected source hashes, replacement content, checks, and a
behavioral reproducer. The engine requires that reproducer to fail before editing,
makes exact backups and a diff, applies bounded changes atomically, compiles changed
Python, runs specified tests, and reruns the original behavior. Failures, duplicate
plans, and retry limits do not silently broaden scope. There are at most three
attempts per failure.

Failed validation triggers rollback. Rollback checks the applied and backup hashes
and preserves newer user edits by reporting a conflict rather than overwriting
them. Live installation source repairs also require a supervised restart so the
running action registry can reload the repaired source. The
isolated demo verifies its fixture in fresh subprocesses without replacing the
live application.

Restart keeps the current application available while a replacement starts. Two
fresh heartbeat records must match its nonce and owned process tree; a final
health and cancellation check precedes handoff. Failed candidates and their owned
children are stopped. The supervisor permits at most two replacement attempts in
one process. Repair mode and observation remain off in the replacement. A healthy
UI heartbeat proves a responsive desktop event loop, not authenticated provider
access, microphone/audio operation, or arbitrary application behavior; behavioral
tests are recorded separately.

There is no persistent external crash watchdog. If the parent application dies,
its in-process repair worker cannot recover it until the application is relaunched.

Evidence is stored below `logs/repair/`:

- `<failure-id>.json`: failure, attempts, phases, checks, and latest outcome.
- `<failure-id>/<attempt-id>/`: exact source backups and the recorded diff.
- `activity.jsonl`: redacted runtime events and command results.
- `demos/<id>/`: isolated demonstration workspace and its own repair journal.
- `runtime/`: replacement health records and startup output.

Main repair history and the latest 50 isolated demo workspaces are restored on
startup. Diagnostic text is scrubbed for known configured secrets and common
credential patterns. This is not a universal secret detector. Exact source backups,
retained raw screenshots, and replacement startup output require local care; do
not treat the entire evidence directory as an automatically sanitized export.

The live local acceptance run exercised the actual Qt controls, isolated source
repair, local screenshot capture, observation stop, and replacement handoff. Its
current evidence is `logs/repair/live-acceptance.json` with separate checks for
the current demo, its task-associated capture, and committed restart handoff.
The run did **not** invoke cloud image interpretation or verify provider/audio
connectivity. No dependency package was downloaded or installed during this
implementation's validation. `--repair-acceptance` is an explicit diagnostic-mode workflow that
enables local observation, runs the isolated demo, saves a panel screenshot, and
requests a restart; it is not part of normal startup.
