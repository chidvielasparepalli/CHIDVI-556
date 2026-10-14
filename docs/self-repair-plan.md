# Self-repair implementation plan

The user's phase 0–10 specification is the design authority. Work remains in
`D:\CHIDVI-556`, on the existing dirty working tree; no commit, push, reset or copy.

## Architecture and scope

Reuse `TaskStore`, the existing Gemini Live agent, action registry, Qt operator
controls, screenshot service and exact-image review. A repair is an executor of
an existing task, not another conversational agent. Workspace source edits use
expected hashes, complete backups, atomic writes and a journal. Reproduction
before and after the patch is required for a verified repair.

1. `core/repair_permissions.py` and `core/diagnostic_commands.py`: capability
   classes, validated workspace paths, redacted audit events, fixed PowerShell
   diagnostic recipes, bounded subprocess output, timeout and cancellation.
   Test real temporary subprocesses and refusal paths before integrating.
2. `core/self_repair.py`: failure evidence, diagnoses, source search, minimal
   patch plans, backup/diff/validate/verify/restart/rollback lifecycle. Test an
   isolated missing-handler repair, failed checks and concurrent user edits.
3. `core/repair_ui.py` and `core/repair_observation.py`: visible repair phase,
   terminal output, history, permissions, emergency stop and session-scoped
   on-demand/bounded local capture. No automatic cloud upload or retention.
4. `core/repair_runtime.py`, `actions/self_repair.py`, `main.py`, `ui.py`,
   `core/operator_ui.py`, `core/task_control.py`: wire the same task store,
   failure detection, mode/permission controls and model-facing repair tools.
5. `core/repair_restart.py`: launch a replacement with a nonce-bound local
   heartbeat. Keep the old process until replacement UI health is verified;
   never terminate unrelated processes. Add an actual diagnostic application
   mode so repairs remain usable without a connected cloud or audio session.
6. Run focused tests and the full existing suite, compilation and diff checks.
   Launch the real Qt application, inspect the live repair UI, run a real
   isolated repair, capture locally and test replacement-process health.

Class A is workspace-scoped. Class B defaults off and is reviewed/revoked in the
desktop permissions tab. Class C produces concrete approval requirements; no
model-supplied `approved` parameter grants privileges. Arbitrary shell strings
are not a command API. Subprocess tests/imports execute trusted project code;
the broker is a capability boundary, not an OS sandbox for hostile Python.

No compatible local vision backend was found in the previous verified audit.
Cloud consent remains off unless the user explicitly changes it and reviews
the exact image. Screenshot capture and visual interpretation are reported
separately. Unknown failures remain hypotheses until a reproducer establishes
the cause; bounded attempts do not silently broaden their scope.
