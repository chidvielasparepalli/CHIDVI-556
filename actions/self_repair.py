"""Tools for the existing agent's desktop-authorized repair session."""
import json

_runtime = None


def bind(runtime):
    global _runtime
    _runtime = runtime


def self_repair(parameters, **_):
    if _runtime is None:
        return json.dumps({'ok': False, 'error': 'The desktop repair service is not ready.'})
    return json.dumps(_runtime.handle(parameters), ensure_ascii=False)


TOOL = {
    'name': 'self_repair',
    'description': (
        'Investigate and repair this project within a user-enabled Self-Repair Mode. '
        'Uses the same task, recorded failure, evidence, backed-up hash-checked patches, tests and behavioral verification. '
        'Never claim success unless status is repaired and verified. Unknown causes are hypotheses. '
        'No arbitrary shell commands, approval flags, remote uploads or automatic privilege escalation. '
        'Actions: status/history/report/diagnose/read/search/command/repair/auto_repair/rollback/restart/observe/analyze. '
        'request_json is a JSON object: command uses a fixed preset (python_version, dependencies, '
        'compile with paths, tests with start_dir/pattern, import with module, tool_contract with path, '
        'powershell_diagnostic with diagnostic=system/processes/ports). repair uses a plan containing '
        'diagnosis, confidence, files [{path,expected_sha256,content}], tests [presets], verify preset. '
        'Live source repairs require a supervised restart; the isolated demo does not modify this installation. '
        'Read files and references first. The verifier must reproduce the original failure BEFORE editing '
        'and pass afterward. Capture is separate from image understanding; cloud still needs exact-image approval.'),
    'parameters': {'type': 'OBJECT', 'properties': {
        'action': {'type': 'STRING'}, 'failure_id': {'type': 'STRING'},
        'path': {'type': 'STRING'}, 'text': {'type': 'STRING'},
        'request_json': {'type': 'STRING'}}, 'required': ['action']},
    'handler': self_repair,
}
