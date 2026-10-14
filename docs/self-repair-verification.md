# Self-repair implementation and verification — 2026-10-13

## Workspace and preservation

The existing repository is `D:\CHIDVI-556`, on `main`, initially at `92da5fa`.
`D:\CHDIVI-556` was absent. No repository was created or copied. Existing staged,
unstaged and untracked work was inspected and preserved. The pre-existing README
diff remains 872 added / 349 removed lines; the 102,549,314-byte untracked
`karting_reel_Astra.mp4` remains present. No commit, push, deployment, reset,
checkout, clean or operating-system configuration change was performed.

## Original failures

- Routine action/plugin discovery success was being routed through the same UI
  activity callback as meaningful activity. The existing corrected loaders route
  routine discovery to DEBUG diagnostics and preserve errors in the activity UI.
  Regression tests verify the routing policy rather than filtering UI strings.
- Screenshot capture had been coupled to cloud interpretation. With Cloud Vision
  off and no compatible local vision backend, `screen_process` failed as if local
  capture itself were unavailable. The shared capture and interpretation paths
  now report these separately, use one saved privacy setting, and require an
  exact-image review before upload. No compatible local vision backend was found.

## Implemented workflow

The existing TaskStore and action registry own repair work. The `self_repair`
action supports evidence collection, source reading/reference search, diagnosis,
structured plans, hash-checked atomic edits, original backups, unified diffs,
compilation/tests, reproduction before and verification after repair, supervised
restart, recovery journaling and conflict-safe rollback. Attempts, commands,
output, installation attempts and restart attempts are bounded. Repeated plans
are rejected; unknown causes remain hypotheses.

The built-in automatic recipe repairs a missing TOOL handler only when AST
evidence identifies the existing matching function. Other repairs require a
focused plan with a failing behavioral reproducer. Live source repairs require
a supervised restart so the running registry reloads the change. The isolated
demo modifies only its generated fixture below `logs/repair/demos/`.

The desktop includes repair mode, explicit local observation, a persistent status,
diagnosis, terminal output, files/diff/tests, readable recovery history and backup
details, rollback, permission preferences, diagnostic export, and STOP. A native
Windows global shortcut replaces the unavailable optional keyboard package.
Commands start suspended on Windows, enter an owned job, then resume; this fixes
a timing bug found when a fast command exited before job attachment. Failed
replacements and owned descendants are cleaned up before reporting failure.

Observation is local and session-scoped, with bounded sampling, display geometry,
foreground metadata, task IDs and timestamps. Screenshot retention defaults off.
Cloud analysis never runs automatically during observation or repair.

## Verification results

| Check | Result |
| --- | --- |
| Complete automated suite | **382 passed**, 39.487 seconds, exit code 0 |
| Compilation | **49 source and test modules passed** |
| Whitespace validation | `git diff --check` passed; Git reports an existing LF/CRLF normalization warning |
| Unit and simulated external-service tests | Passed permission refusals, privacy gates, missing backend, logging, UI, journal, cancellation, history and acceptance bookkeeping |
| Real subprocess integration | Passed stdout/stderr/exit capture, timeout, owned-child cancellation, syntax/import/behavior checks, failed-validation rollback and user-change conflict protection |
| Real Windows diagnostics | Python version, system information, processes, ports and `pip check` passed; ports required execution outside the Codex tool sandbox, with no UAC elevation |
| Actual Qt application | Launched through `main.py --diagnostic-mode --show-repair`; visible controls exercised through application callbacks and inspected in captured panel images |
| End-to-end isolated repair | Reproduced the missing-handler failure, preserved backup/hash, patched it, passed compilation and a real behavioral unittest, then passed the original reproducer |
| Real desktop observation | Captured an actual 1920 × 1080 screenshot associated with that repair task; observation stopped and the frame was cleared afterward |
| Native global stop | Real Windows registration succeeded; event routing/cancellation and cleanup are covered by tests; a physical keystroke was not injected |
| Actual application restart | Replacement passed two fresh nonce/PID-bound UI heartbeats and committed the handoff; replacement remains open |
| Screenshot interpretation | **Not verified**. Cloud Vision was enabled by the user, but no completed exact-image approval/analysis result was recorded at report time |

Evidence is local: `logs/repair/full-suite.log`, `live-acceptance.json`,
`live-diagnostics.json`, `live-repair-panel.png`, the individual recovery journals,
and replacement health/startup files. The live acceptance report distinguishes
the current task's repair/capture from older records and requires a successful
current restart and committed handoff.

## Permission state and remaining steps

Class A is workspace-scoped and automatically permitted while repair mode is
enabled. The user's saved Class B choices currently enable dependencies, virtual
environment changes, app configuration changes and model downloads. They are
revocable in the repair panel. Enabling a capability does not perform an action.
Pinned dependency installation has metadata/license/size/hash/compatibility/disk
preflight; external metadata and installer behavior were tested with isolated
fixtures. No real dependencies or models were downloaded or installed.

Class C remains blocked pending specific authorization and an implemented adapter.
There is no arbitrary administrator executor or UAC bypass. The dedicated image
review flow remains the explicit upload authorization mechanism. No administrator
operation was needed or performed.

The updated diagnostic application is open. To finish the optional vision check,
enable **Self-Repair Mode**, select **Observe this task**, wait for a snapshot,
choose **Analyze latest image**, and approve the exact preview you intend to send.
Cloud Vision is already saved as enabled. The replacement intentionally starts
repair mode and observation off.

Remaining limitations: no general model downloader or virtual-environment builder;
no persistent watchdog for a completely dead parent; no compatible local vision
adapter installed; no verification of normal cloud conversation/audio/phone
connectivity or additional physical monitors. The computer-use bridge was
unavailable, so direct external mouse/keyboard interaction was not verified.
Imports and tests execute trusted project Python; the broker is not an OS sandbox
for hostile source. See [operational controls](self-repair.md) for retention,
backup handling, command limits, and recovery behavior.

## Files in this implementation

- Integration: `main.py`, `ui.py`, `core/task_control.py`, `core/operator_ui.py`,
  `actions/self_repair.py`.
- Repair components: `core/repair_permissions.py`, `core/diagnostic_commands.py`,
  `core/self_repair.py`, `core/repair_runtime.py`, `core/repair_restart.py`,
  `core/repair_observation.py`, `core/repair_ui.py`, `core/repair_hotkey.py`,
  `core/repair_acceptance.py`.
- New tests: `tests/test_repair_permissions.py`, `tests/test_diagnostic_commands.py`,
  `tests/test_self_repair.py`, `tests/test_repair_runtime.py`,
  `tests/test_repair_restart.py`, `tests/test_repair_observation.py`,
  `tests/test_repair_ui.py`, `tests/test_repair_hotkey.py`,
  `tests/test_repair_acceptance.py`.
- Extended tests: `tests/test_task_control.py`, `tests/test_personality_voice.py`,
  `tests/test_screen_understanding.py`.
- Documentation: `docs/self-repair-plan.md`, `docs/self-repair.md`, and this report.

Earlier operator/vision/phone changes and all other pre-existing dirty files were
retained; the manifest above identifies this self-repair implementation's scope.
