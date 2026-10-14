"""Bounded, journaled source repair with reproducer checks and conflict-safe rollback."""
from __future__ import annotations

import ast
import copy
import difflib
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from core.repair_permissions import redact, _redact_event


MAX_ATTEMPTS = 3
MAX_FILES = 8
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"^[0-9a-f]{32}$")


def _hash(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _safe(value):
    if isinstance(value, str):
        text = redact(value)
        return text if len(text) <= 65536 else text[:65536] + "\n[truncated]"
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in _redact_event(value).items()}
    if isinstance(value, (tuple, list)):
        return [_safe(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[binary/object omitted]"


class _Stopped(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class RepairEngine:
    def __init__(self, workspace, broker, commands, emit=None, observe=None, restart=None):
        self.workspace = Path(workspace).resolve()
        self.broker, self.commands = broker, commands
        self.emit = emit or (lambda event: None)
        self.observe, self.restart = observe, restart
        self.directory = self._internal("logs/repair")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._repair_lock = threading.Lock()
        self._journal_lock = threading.RLock()

    def _internal(self, relative):
        target = self.broker.path(relative, write=True)
        if not target.is_relative_to(self.workspace):
            raise PermissionError("Repair journal must remain inside the workspace")
        return target

    def _atomic(self, path, data, *, expected=None, compare=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".repair-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if compare and self._current_hash(path) != expected:
                raise _Stopped("file_conflict", "A file changed before the atomic write")
            if path.exists():
                os.chmod(temporary, path.stat().st_mode)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _journal_path(self, failure_id):
        if not isinstance(failure_id, str) or not _ID.fullmatch(failure_id):
            raise ValueError("Invalid failure ID")
        return self._internal(f"logs/repair/{failure_id}.json")

    def _save(self, record):
        record["updated_at"] = time.time()
        with self._journal_lock:
            self._atomic(self._journal_path(record["id"]), json.dumps(_safe(record), indent=2).encode("utf-8"))

    def _load(self, failure_id):
        with self._journal_lock:
            return json.loads(self._journal_path(failure_id).read_text(encoding="utf-8"))

    def _notify(self, event):
        try:
            self.emit(_safe(event))
        except Exception:
            # UI delivery is supplemental; the atomic journal remains authoritative.
            pass

    def record_failure(self, error, task_id="", source="tool", traceback_text=""):
        message = (error.get("message") or error.get("error") or str(error)) if isinstance(error, dict) else str(error)
        record = {"id": uuid.uuid4().hex, "created_at": time.time(), "task_id": str(task_id)[:128],
                  "source": str(source)[:128], "error": redact(message)[:8000],
                  "error_type": type(error).__name__, "traceback": redact(str(traceback_text))[:24000],
                  "status": "recorded", "diagnosis": None, "attempts": []}
        if isinstance(error, dict) and (error.get("path") or error.get("file")):
            record["path"] = str(error.get("path") or error.get("file"))[:1024]
        if isinstance(error, dict) and "exit_code" in error:
            record["exit_code"] = _safe(error["exit_code"])
        if isinstance(error, dict) and "evidence" in error:
            evidence = _safe(error["evidence"])
            encoded = json.dumps(evidence, ensure_ascii=True)
            record["evidence"] = evidence if len(encoded) <= 65536 else {"excerpt": encoded[:32000], "truncated": True}
        self._save(record)
        self._notify({"phase": "recorded", "failure_id": record["id"], "task_id": task_id, "message": record["error"]})
        return copy.deepcopy(record)

    def history(self, limit=100):
        limit = max(1, min(int(limit), 200))
        records = []
        for path in self.directory.glob("*.json"):
            if not _ID.fullmatch(path.stem):
                continue
            try:
                records.append(self._load(path.stem))
            except (OSError, ValueError):
                self._notify({"phase": "journal_error", "message": "A repair journal could not be read", "failure_id": path.stem})
        return sorted(records, key=lambda item: item.get("created_at", 0), reverse=True)[:limit]

    def _bytes(self, path):
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Repair source files must be regular files of at most 512 KiB")
        return path.read_bytes()

    def _current_hash(self, path):
        return _hash(self._bytes(path))

    def read(self, path):
        target = self.broker.path(path)
        relative = target.relative_to(self.workspace).as_posix()
        self.broker.require("source_read", scope=relative, reason="Inspect source for a repair")
        data = self._bytes(target)
        if data is None:
            raise FileNotFoundError(relative)
        original = data.decode("utf-8-sig")
        content = redact(original)
        return {"path": relative, "content": content, "sha256": _hash(data), "size": len(data),
                "redacted": content != original}

    def search(self, query, paths=None, limit=50):
        if not isinstance(query, str) or not query or len(query) > 500:
            raise ValueError("Search requires 1–500 literal characters")
        limit = max(1, min(int(limit), 200))
        candidates = paths
        if candidates is None:
            candidates = []
            for directory, names, files in os.walk(self.workspace):
                names[:] = [name for name in names if name not in {".git", ".venv", "node_modules", "logs", "config", "__pycache__"}]
                for filename in sorted(files):
                    if Path(filename).suffix.lower() in {".py", ".md", ".txt", ".json", ".toml", ".html", ".js", ".css"}:
                        candidates.append(str((Path(directory) / filename).relative_to(self.workspace)))
                if len(candidates) >= 2000:
                    break
        matches = []
        for path in list(candidates)[:2000]:
            try:
                source = self.read(path)
            except (OSError, ValueError, UnicodeError):
                continue
            for number, line in enumerate(source["content"].splitlines(), 1):
                if query.casefold() in line.casefold():
                    matches.append({"path": source["path"], "line": number, "text": line[:500]})
                    if len(matches) >= limit:
                        return matches
        return matches

    def _handler_recipe(self, relative):
        source = self.read(relative)
        if source["redacted"]:
            return None
        tree = ast.parse(source["content"])
        dictionaries = [node.value for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "TOOL" for target in node.targets)
                        and isinstance(node.value, ast.Dict)]
        if len(dictionaries) != 1:
            return None
        fields = {key.value: value for key, value in zip(dictionaries[0].keys, dictionaries[0].values)
                  if isinstance(key, ast.Constant) and isinstance(key.value, str)}
        name = fields.get("name")
        if "handler" in fields or not isinstance(name, ast.Constant) or not isinstance(name.value, str):
            return None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name) and target.value.id == "TOOL"
                    and isinstance(target.slice, ast.Constant) and target.slice.value == "handler" for target in node.targets):
                return None
        handlers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name.value
                    and not node.decorator_list and (node.args.kwarg or any(arg.arg == "parameters"
                        for arg in node.args.args + node.args.kwonlyargs))]
        if len(handlers) != 1:
            return None
        return {"kind": "missing_tool_handler", "path": source["path"], "handler": name.value}

    def diagnose(self, failure_id):
        record = self._load(failure_id)
        diagnosis = {"hypothesis": "The recorded failure needs a targeted reproducer and source investigation.",
                     "confidence": .2, "confirmed": False, "evidence": [record["error"]]}
        if "TOOL" in record["error"] and "handler" in record["error"]:
            match = re.search(r"([\w./\\:-]+\.py)", record["error"])
            relative = record.get("path") or (match.group(1).replace("\\", "/") if match else None)
            if relative and "/" not in relative:
                relative = "actions/" + relative
            if relative:
                try:
                    recipe = self._handler_recipe(relative)
                    if recipe:
                        diagnosis = {"hypothesis": "The TOOL dictionary is missing the existing matching callable handler.",
                                     "confidence": .99, "confirmed": True, "recipe": recipe,
                                     "evidence": [record["error"], "AST confirms one matching synchronous handler and no TOOL handler binding."]}
                except (OSError, ValueError, SyntaxError):
                    pass
        record["diagnosis"] = diagnosis
        self._save(record)
        self._notify({"phase": "diagnosing", "failure_id": failure_id, "task_id": record["task_id"],
                      "message": diagnosis["hypothesis"], "diagnosis": diagnosis})
        return copy.deepcopy(diagnosis)

    def auto_repair(self, failure_id, task_id, cancel_event):
        diagnosis = self.diagnose(failure_id)
        recipe = diagnosis.get("recipe")
        if not recipe:
            return {"ok": False, "status": "blocked", "code": "diagnosis_required", "failure_id": failure_id,
                    "message": "No deterministic repair was established; provide an evidence-based plan and reproducer.", "diagnosis": diagnosis}
        source = self.read(recipe["path"])
        if source["redacted"]:
            return {"ok": False, "status": "blocked", "code": "protected_content", "failure_id": failure_id}
        tests = []
        candidate = "tests/test_" + Path(recipe["path"]).stem + ".py"
        if self.broker.path(candidate).is_file():
            tests.append({"kind": "tests", "start_dir": "tests", "pattern": Path(candidate).name})
        # Retain the exact original prefix, including a UTF-8 BOM and CRLFs.
        original = self._bytes(self.broker.path(recipe["path"])).decode("utf-8")
        newline = "\r\n" if "\r\n" in original else "\n"
        separator = newline if original.endswith(("\r", "\n")) else newline * 2
        plan = {"diagnosis": diagnosis["hypothesis"], "confidence": diagnosis["confidence"],
                "files": [{"path": recipe["path"], "expected_sha256": source["sha256"],
                           "content": original + separator + f"TOOL['handler'] = {recipe['handler']}" + newline}],
                "tests": tests, "verify": {"kind": "tool_contract", "path": recipe["path"]},
                "restart": self.restart is not None, "reason": "Bind the existing handler; verify the action contract in a fresh process"}
        return self.repair(failure_id, plan, task_id, cancel_event)

    def _check_cancelled(self, cancel_event):
        if cancel_event.is_set():
            raise _Stopped("cancelled", "Repair cancelled; applied changes will be rolled back")

    def _phase(self, record, attempt, phase, message, **details):
        record["status"] = attempt["status"] = phase
        attempt["phases"].append({"phase": phase, "at": time.time(), "message": message})
        self._save(record)
        self._notify({"phase": phase, "failure_id": record["id"], "attempt_id": attempt["id"],
                      "task_id": attempt["task_id"], "message": message, **details})

    def _command(self, record, attempt, phase, spec, cancel_event):
        self._check_cancelled(cancel_event)
        self._phase(record, attempt, phase, f"Running {spec.get('kind', spec.get('preset', 'diagnostic'))}")
        result = self.commands.run(copy.deepcopy(spec), cancel_event=cancel_event)
        attempt["commands"].append({"phase": phase, "spec": _safe(spec), "result": _safe(result)})
        self._save(record)
        self._notify({"phase": phase, "failure_id": record["id"], "attempt_id": attempt["id"],
                      "task_id": attempt["task_id"], "tests": attempt["commands"][-1]})
        self._check_cancelled(cancel_event)
        if result.get("cancelled"):
            raise _Stopped("cancelled", "Diagnostic command was cancelled")
        return result

    def _prepare(self, plan):
        if not isinstance(plan, dict) or not isinstance(plan.get("diagnosis"), str) or not plan["diagnosis"].strip():
            raise _Stopped("diagnosis_required", "A concrete diagnosis is required before editing")
        confidence = plan.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not .7 <= confidence <= 1:
            raise _Stopped("diagnosis_required", "Diagnosis confidence must be between 0.7 and 1")
        files, tests, verify = plan.get("files"), plan.get("tests"), plan.get("verify")
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise _Stopped("invalid_plan", "Repair must change 1–8 files")
        if not isinstance(tests, list) or len(tests) > 8 or not all(isinstance(spec, dict) for spec in tests):
            raise _Stopped("invalid_plan", "Provide at most eight structured test commands")
        if any(spec.get("kind", spec.get("preset")) not in {"compile", "tests", "import", "tool_contract"} for spec in tests):
            raise _Stopped("invalid_plan", "Repair checks support compilation, tests, imports and tool contracts only")
        if not isinstance(verify, dict) or verify.get("kind", verify.get("preset")) not in {"tool_contract", "tests", "import"}:
            raise _Stopped("reproducer_required", "A behavioral tests, import or tool_contract reproducer is required")
        if type(plan.get("restart", False)) is not bool:
            raise _Stopped("invalid_plan", "Restart must be a boolean")
        if plan.get("restart"):
            self.broker.require("restart", reason=plan.get("reason", "Verify repaired application startup"))
            if self.restart is None:
                raise _Stopped("restart_unavailable", "No supervised restart callback is available")
        verifier_root = self.broker.path(verify.get("start_dir", "tests")) if verify.get("kind", verify.get("preset")) == "tests" else None
        verifier_pattern = verify.get("pattern", "test_*.py")
        if verifier_root is not None and not isinstance(verifier_pattern, str):
            raise _Stopped("invalid_plan", "The original test pattern must be a string")
        prepared, seen, total = [], set(), 0
        for file in files:
            if not isinstance(file, dict) or "expected_sha256" not in file or not isinstance(file.get("content"), str):
                raise _Stopped("invalid_plan", "Every file needs path, expected_sha256 and UTF-8 content")
            path = self.broker.path(file.get("path"), write=True)
            relative = path.relative_to(self.workspace).as_posix()
            if relative in seen or relative.startswith("logs/repair/"):
                raise _Stopped("invalid_plan", "Duplicate paths and repair journal edits are not allowed")
            if (verifier_root is not None and path.is_relative_to(verifier_root)
                    and fnmatch.fnmatch(path.name, verifier_pattern)):
                raise _Stopped("reproducer_changed", "The repair cannot edit the original behavioral test used to prove its result")
            seen.add(relative)
            self.broker.require("source_edit", scope=relative, reason=plan.get("reason", plan["diagnosis"]))
            before, after = self._bytes(path), file["content"].encode("utf-8")
            if _hash(before) != file["expected_sha256"]:
                raise _Stopped("file_conflict", "Source hash changed; inspect the current file before proposing another repair")
            if len(after) > MAX_FILE_BYTES or (before is not None and b"\x00" in before):
                raise _Stopped("invalid_plan", "Only bounded text source changes are supported")
            if "[REDACTED]" in file["content"] and (before is None or b"[REDACTED]" not in before):
                raise _Stopped("protected_content", "Redacted placeholders cannot be written into source")
            total += len(after) + len(before or b"")
            if total > MAX_TOTAL_BYTES:
                raise _Stopped("invalid_plan", "Repair exceeds the total file size limit")
            prepared.append({"path": relative, "absolute": path, "before": before, "after": after,
                             "before_sha256": _hash(before), "after_sha256": _hash(after)})
        return prepared

    def _recovery_phase(self, record, attempt, phase, message):
        """Journal failure must never prevent restoring an owned source change."""
        try:
            self._phase(record, attempt, phase, message)
        except Exception as exc:
            record["journal_error"] = type(exc).__name__
            self._notify({"phase": phase, "failure_id": record["id"], "attempt_id": attempt["id"],
                          "task_id": attempt["task_id"], "message": message,
                          "journal_error": record["journal_error"]})

    def _rollback(self, record, attempt):
        self._recovery_phase(record, attempt, "rolling_back", "Restoring original files where the repair still owns their contents")
        conflict = False
        for file in reversed(attempt["files"]):
            if not file.get("applied"):
                continue
            try:
                path = self.broker.path(file["path"], write=True)
                self.broker.require("source_edit", scope=file["path"], reason="Restore the original repair backup")
                current = self._current_hash(path)
                if current == file["before_sha256"]:
                    file["rollback"] = "already_original"
                elif current != file["after_sha256"]:
                    file["rollback"] = "conflict"
                    conflict = True
                elif file["backup"] is None:
                    path.unlink()
                    file["rollback"] = "removed_new_file"
                else:
                    data = self._internal(file["backup"]).read_bytes()
                    if _hash(data) != file["before_sha256"]:
                        raise ValueError("Backup integrity check failed")
                    self._atomic(path, data, expected=file["after_sha256"], compare=True)
                    file["rollback"] = "restored"
            except Exception as exc:
                file["rollback"] = "conflict"
                file["rollback_error"] = type(exc).__name__
                conflict = True
        try:
            self._save(record)
        except Exception as exc:
            record["journal_error"] = type(exc).__name__
        return conflict

    def rollback(self, failure_id):
        """Restore the most recent attempt without overwriting subsequent edits."""
        if not self._repair_lock.acquire(blocking=False):
            return {"ok": False, "status": "blocked", "code": "repair_busy", "message": "Another repair is already running"}
        try:
            record = self._load(failure_id)
            attempts = record.get("attempts", [])
            if not attempts or not any(file.get("applied") for file in attempts[-1].get("files", [])):
                return {"ok": False, "status": "blocked", "code": "not_applied", "failure_id": failure_id,
                        "message": "The latest attempt has no applied source changes to restore"}
            attempt = attempts[-1]
            conflict = self._rollback(record, attempt)
            status = "blocked_conflict" if conflict else "rolled_back"
            message = "Newer source changes were preserved; rollback needs review" if conflict else "Original source files restored"
            self._recovery_phase(record, attempt, status, message)
            return {"ok": not conflict and not record.get("journal_error"), "status": status,
                    "code": "file_conflict" if conflict else "journal_error" if record.get("journal_error") else "rolled_back",
                    "failure_id": failure_id, "attempt_id": attempt["id"], "message": message,
                    **({"journal_error": record["journal_error"]} if record.get("journal_error") else {})}
        except Exception as exc:
            return {"ok": False, "status": "blocked", "code": "rollback_failed", "failure_id": failure_id,
                    "message": "Rollback could not complete: " + type(exc).__name__}
        finally:
            self._repair_lock.release()

    def repair(self, failure_id, plan, task_id, cancel_event):
        if not self._repair_lock.acquire(blocking=False):
            return {"ok": False, "status": "blocked", "code": "repair_busy", "message": "Another repair is already running"}
        record = attempt = None
        try:
            record = self._load(failure_id)
            if not isinstance(task_id, str) or not task_id or cancel_event is None or not callable(getattr(cancel_event, "is_set", None)):
                raise _Stopped("invalid_task", "An existing task identity and cancellation event are required")
            self._check_cancelled(cancel_event)
            prepared = self._prepare(plan)
            fingerprint = _hash(json.dumps({"files": [{key: file[key] for key in ("path", "before_sha256", "after_sha256")} for file in prepared],
                                            "tests": plan["tests"], "verify": plan["verify"], "restart": plan.get("restart", False)},
                                           sort_keys=True).encode())
            if any(item.get("fingerprint") == fingerprint for item in record["attempts"]):
                raise _Stopped("duplicate_attempt", "This identical repair plan was already attempted; new evidence and a different patch are required")
            if len(record["attempts"]) >= MAX_ATTEMPTS:
                raise _Stopped("attempt_limit", "Repair reached its three-attempt limit; further changes require investigation")
            attempt = {"id": uuid.uuid4().hex, "task_id": task_id, "fingerprint": fingerprint,
                       "diagnosis": plan["diagnosis"], "confidence": plan["confidence"], "reason": plan.get("reason", ""),
                       "created_at": time.time(), "phases": [], "commands": [], "files": [], "health": None}
            record["attempts"].append(attempt)
            if not record.get("diagnosis") or record["diagnosis"].get("hypothesis") != plan["diagnosis"]:
                record["diagnosis"] = {"hypothesis": plan["diagnosis"], "confidence": plan["confidence"],
                                       "confirmed": False, "evidence": [record["error"], "Supplied plan; original reproducer pending."]}
            self._phase(record, attempt, "diagnosing", "Testing the stated diagnosis against the original failure",
                        diagnosis=record["diagnosis"], files=[file["path"] for file in prepared])
            if self.observe is not None:
                try:
                    attempt["observation_before"] = _safe(self.observe())
                except Exception as exc:
                    attempt["observation_error"] = type(exc).__name__
            before = self._command(record, attempt, "reproducing", plan["verify"], cancel_event)
            if (before.get("ok") is not False or before.get("timed_out") or before.get("cancelled")
                    or not isinstance(before.get("exit_code"), int) or before["exit_code"] == 0):
                raise _Stopped("not_reproduced", "The original behavior did not produce a confirmed failing exit; no edit was applied")
            self._phase(record, attempt, "backing_up", "Saving exact originals and verifying all source hashes again")
            for file in prepared:
                if self._current_hash(file["absolute"]) != file["before_sha256"]:
                    raise _Stopped("file_conflict", "Source changed while the failure was being reproduced")
            folder = f"logs/repair/{failure_id}/{attempt['id']}"
            diffs = []
            for index, file in enumerate(prepared):
                backup = f"{folder}/backups/{index:02d}.bin" if file["before"] is not None else None
                if backup:
                    self._atomic(self._internal(backup), file["before"])
                attempt["files"].append({key: file[key] for key in ("path", "before_sha256", "after_sha256")}
                                        | {"backup": backup, "applied": False})
                diffs.extend(difflib.unified_diff((file["before"] or b"").decode("utf-8-sig").splitlines(True),
                             file["after"].decode("utf-8").splitlines(True), fromfile="a/" + file["path"], tofile="b/" + file["path"]))
            diff = redact("".join(diffs))
            attempt["diff_path"] = f"{folder}/changes.diff"
            self._atomic(self._internal(attempt["diff_path"]), diff.encode("utf-8"))
            self._phase(record, attempt, "applying", "Applying the proposed source changes", diff=diff)
            for file, saved in zip(prepared, attempt["files"]):
                self._check_cancelled(cancel_event)
                self.broker.require("source_edit", scope=file["path"], reason=plan.get("reason", plan["diagnosis"]))
                self._atomic(file["absolute"], file["after"], expected=file["before_sha256"], compare=True)
                saved["applied"] = True
                self._save(record)
            python_paths = [file["path"] for file in prepared if file["path"].endswith(".py")]
            checks = ([{"kind": "compile", "paths": python_paths}] if python_paths else []) + plan["tests"]
            for spec in checks:
                if self._command(record, attempt, "testing", spec, cancel_event).get("ok") is not True:
                    raise _Stopped("tests_failed", "Required compilation or regression checks failed")
            if self._command(record, attempt, "verifying", plan["verify"], cancel_event).get("ok") is not True:
                raise _Stopped("verification_failed", "The original failure still reproduces after the patch")
            if plan.get("restart"):
                self._check_cancelled(cancel_event)
                self.broker.require("restart", reason=plan.get("reason", "Verify repaired application startup"))
                self._phase(record, attempt, "restarting", "Checking the supervised replacement application")
                attempt["health"] = _safe(self.restart())
                self._save(record)
                self._check_cancelled(cancel_event)
                health = attempt["health"]
                if health is not True and (not isinstance(health, dict) or health.get("ok") is not True):
                    raise _Stopped("restart_failed", "The replacement application did not pass its health check")
                if self._command(record, attempt, "verifying_restart", plan["verify"], cancel_event).get("ok") is not True:
                    raise _Stopped("verification_failed", "The original behavior failed after restart")
            self._check_cancelled(cancel_event)
            attempt["verified"] = True
            record["last_outcome"] = {"status": "repaired", "task_id": task_id, "at": time.time()}
            self._phase(record, attempt, "repaired", "Original failure reproduced before the patch and passed all required checks afterward")
            return {"ok": True, "status": "repaired", "failure_id": failure_id, "attempt_id": attempt["id"],
                    "message": "Repair verified against the original failure", "health": attempt["health"]}
        except Exception as exc:
            code = exc.code if isinstance(exc, _Stopped) else "permission_denied" if isinstance(exc, PermissionError) else "repair_failed"
            message = redact(str(exc)) if isinstance(exc, (_Stopped, PermissionError, ValueError)) else f"Repair stopped: {type(exc).__name__}"
            status = "cancelled" if code == "cancelled" else "blocked"
            if attempt is not None:
                attempt["error"] = {"code": code, "message": message, "type": type(exc).__name__}
                applied = any(file.get("applied") for file in attempt["files"])
                conflict = self._rollback(record, attempt) if applied else False
                status = "blocked_conflict" if conflict else "cancelled" if code == "cancelled" else "rolled_back" if applied else "blocked"
                record["last_outcome"] = {"status": status, "code": code, "message": message, "task_id": task_id, "at": time.time()}
                self._recovery_phase(record, attempt, status, message)
            elif record is not None:
                record["status"] = status
                record["last_outcome"] = {"status": status, "code": code, "message": message, "task_id": str(task_id)[:128], "at": time.time()}
                try:
                    self._save(record)
                except Exception as journal_exc:
                    record["journal_error"] = type(journal_exc).__name__
                self._notify({"phase": status, "failure_id": failure_id, "message": message, "code": code})
            return {"ok": False, "status": status, "code": code, "message": message, "failure_id": failure_id,
                    **({"attempt_id": attempt["id"]} if attempt else {}),
                    **({"journal_error": record["journal_error"]} if record and record.get("journal_error") else {})}
        finally:
            self._repair_lock.release()
