"""Repair acceptance tests operate on disposable projects and real diagnostic children."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock


SOURCE = ("TOOL = {'name': 'broken', 'description': 'Repair fixture', 'parameters': {'type': 'OBJECT'}}\n"
          "def broken(parameters):\n    return 'done'\n")
PATCHED = SOURCE + "\nTOOL['handler'] = broken\n"


class RepairEngineTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("core.self_repair"), "A verified transactional repair engine is required")
        self.module = importlib.import_module("core.self_repair")
        from core.repair_permissions import PermissionBroker
        from core.diagnostic_commands import CommandService
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "actions/broken.py"
        self.path.parent.mkdir()
        self.path.write_bytes(SOURCE.encode())
        self.broker = PermissionBroker(self.root)
        self.commands = CommandService(self.root, self.broker)
        self.events = []
        self.engine = self.module.RepairEngine(self.root, self.broker, self.commands, emit=self.events.append)
        self.cancel = threading.Event()
        self.failure = self.engine.record_failure(
            "Action rejected: actions/broken.py — TOOL['handler'] missing or not callable.", task_id="existing-task")

    def plan(self, **changes):
        plan = {"diagnosis": "TOOL lacks its callable handler binding", "confidence": .99,
                "files": [{"path": "actions/broken.py", "expected_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
                           "content": PATCHED}], "tests": [],
                "verify": {"kind": "tool_contract", "path": "actions/broken.py"},
                "restart": False, "reason": "Restore the action registration contract"}
        plan.update(changes)
        return plan

    def repair(self, plan=None):
        return self.engine.repair(self.failure["id"], self.plan() if plan is None else plan,
                                  task_id="existing-task", cancel_event=self.cancel)

    def failed_tests(self):
        tests = self.root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_failure.py").write_text("import unittest\nclass Case(unittest.TestCase):\n def test_failure(self): self.fail('regression')\n")
        return [{"kind": "tests", "start_dir": "tests", "pattern": "test_failure.py"}]

    def record(self):
        return next(item for item in self.engine.history() if item["id"] == self.failure["id"])

    def test_missing_handler_auto_repair_proves_before_and_after_in_fresh_processes(self):
        diagnosis = self.engine.diagnose(self.failure["id"])
        self.assertTrue(diagnosis["confirmed"])
        self.assertEqual(diagnosis["recipe"]["path"], "actions/broken.py")
        result = self.engine.auto_repair(self.failure["id"], task_id="existing-task", cancel_event=self.cancel)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "repaired")
        self.assertIn("TOOL['handler'] = broken", self.path.read_text())
        record = self.record()
        attempt = record["attempts"][0]
        self.assertEqual(record["task_id"], "existing-task")
        self.assertEqual(attempt["status"], "repaired")
        commands = attempt["commands"]
        self.assertNotEqual(commands[0]["result"]["exit_code"], 0)
        self.assertEqual(commands[-1]["result"]["exit_code"], 0)
        self.assertTrue(any(item["spec"]["kind"] == "compile" for item in commands))
        self.assertTrue(any(event.get("diff") for event in self.events))
        validations = [event["tests"] for event in self.events if event.get("tests")]
        self.assertTrue(any(item["phase"] == "reproducing" and not item["result"]["ok"] for item in validations))
        self.assertTrue(any(item["phase"] == "verifying" and item["result"]["ok"] for item in validations))
        self.assertTrue(any(event.get("files") == ["actions/broken.py"] and event.get("diagnosis") for event in self.events))
        backup = self.root / attempt["files"][0]["backup"]
        self.assertEqual(backup.read_bytes(), SOURCE.encode())

    def test_failed_tests_restore_exact_original_and_journal_evidence(self):
        result = self.repair(self.plan(tests=self.failed_tests()))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())
        attempt = self.record()["attempts"][0]
        self.assertTrue(any(item["phase"] == "testing" and not item["result"]["ok"] for item in attempt["commands"]))
        self.assertEqual(attempt["files"][0]["rollback"], "restored")

    def test_rollback_preserves_newer_user_edit_and_reports_conflict(self):
        original_run = self.commands.run
        def run(spec, cancel_event=None):
            if spec["kind"] == "tests":
                self.path.write_text("# later user edit\n", encoding="utf-8")
            return original_run(spec, cancel_event=cancel_event)
        self.commands.run = run
        result = self.repair(self.plan(tests=self.failed_tests()))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_conflict")
        self.assertEqual(self.path.read_text(), "# later user edit\n")
        self.assertEqual(self.record()["attempts"][0]["files"][0]["rollback"], "conflict")

    def test_all_files_and_hashes_are_validated_before_any_write(self):
        second = self.root / "actions/second.py"
        second.write_text("original\n")
        plan = self.plan()
        plan["files"].append({"path": "actions/second.py", "expected_sha256": "0" * 64, "content": "replacement\n"})
        result = self.repair(plan)
        self.assertFalse(result["ok"])
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())
        self.assertEqual(second.read_text(), "original\n")

    def test_new_file_is_removed_on_rollback_but_unrelated_files_remain(self):
        unrelated = self.root / "keep.txt"
        unrelated.write_text("user asset")
        plan = self.plan(tests=self.failed_tests())
        plan["files"].append({"path": "actions/new_helper.py", "expected_sha256": None, "content": "VALUE = 1\n"})
        result = self.repair(plan)
        self.assertEqual(result["status"], "rolled_back")
        self.assertFalse((self.root / "actions/new_helper.py").exists())
        self.assertEqual(unrelated.read_text(), "user asset")

    def test_pre_cancelled_repair_executes_no_command_and_changes_no_file(self):
        self.cancel.set()
        self.commands.run = Mock(wraps=self.commands.run)
        result = self.repair()
        self.assertEqual(result["status"], "cancelled")
        self.commands.run.assert_not_called()
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())

    def test_cancellation_after_edit_rolls_back_before_returning(self):
        original_run = self.commands.run
        def run(spec, cancel_event=None):
            result = original_run(spec, cancel_event=cancel_event)
            if spec["kind"] == "compile":
                self.cancel.set()
            return result
        self.commands.run = run
        result = self.repair()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())

    def test_duplicate_failed_plan_is_not_retried(self):
        plan = self.plan(tests=self.failed_tests())
        self.assertEqual(self.repair(plan)["status"], "rolled_back")
        self.commands.run = Mock(wraps=self.commands.run)
        result = self.repair(plan)
        self.assertEqual(result["code"], "duplicate_attempt")
        self.commands.run.assert_not_called()
        self.assertEqual(len(self.record()["attempts"]), 1)

    def test_attempt_limit_bounds_distinct_failed_patches(self):
        tests = self.failed_tests()
        for index in range(3):
            plan = self.plan(tests=tests)
            plan["files"][0]["content"] += f"# attempt {index}\n"
            self.assertEqual(self.repair(plan)["status"], "rolled_back")
        plan["files"][0]["content"] += "# fourth\n"
        result = self.repair(plan)
        self.assertEqual(result["code"], "attempt_limit")
        self.assertEqual(len(self.record()["attempts"]), 3)

    def test_already_passing_reproducer_does_not_apply_changes(self):
        self.path.write_bytes(PATCHED.encode())
        plan = self.plan()
        plan["files"][0].update(expected_sha256=hashlib.sha256(PATCHED.encode()).hexdigest(), content=PATCHED + "# unnecessary\n")
        result = self.repair(plan)
        self.assertEqual(result["code"], "not_reproduced")
        self.assertEqual(self.path.read_text(), PATCHED)

    def test_restart_failure_rolls_back_and_is_never_reported_repaired(self):
        self.engine.restart = Mock(return_value={"ok": False, "error": "health failed"})
        result = self.repair(self.plan(restart=True))
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())
        self.engine.restart.assert_called_once_with()

    def test_successful_restart_requires_another_original_behavior_check(self):
        self.engine.restart = Mock(return_value={"ok": True, "status": "healthy"})
        result = self.repair(self.plan(restart=True))
        self.assertTrue(result["ok"], result)
        attempt = self.record()["attempts"][0]
        self.assertEqual(attempt["commands"][-1]["phase"], "verifying_restart")
        self.assertEqual(attempt["commands"][-1]["result"]["exit_code"], 0)
        self.assertTrue(attempt["health"]["ok"])

    def test_automatic_recipe_activates_through_configured_restart_and_reverifies(self):
        self.engine.restart = Mock(return_value={"ok": True, "status": "healthy"})
        result = self.engine.auto_repair(self.failure["id"], "existing-task", self.cancel)
        self.assertTrue(result["ok"], result)
        self.engine.restart.assert_called_once_with()
        attempt = self.record()["attempts"][0]
        self.assertEqual(attempt["commands"][-1]["phase"], "verifying_restart")
        self.assertEqual(attempt["commands"][-1]["spec"], attempt["commands"][0]["spec"])
        self.assertTrue(attempt["commands"][-1]["result"]["ok"])
        phases = [item["phase"] for item in attempt["phases"]]
        self.assertLess(phases.index("restarting"), phases.index("verifying_restart"))
        self.assertLess(phases.index("verifying_restart"), phases.index("repaired"))

    def test_automatic_recipe_rolls_back_when_configured_restart_fails(self):
        self.engine.restart = Mock(return_value={"ok": False, "error": "health failed"})
        result = self.engine.auto_repair(self.failure["id"], "existing-task", self.cancel)
        self.engine.restart.assert_called_once_with()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["code"], "restart_failed")
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())

    def test_unknown_failure_never_invents_a_patch(self):
        failure = self.engine.record_failure("Network request sometimes stalls")
        diagnosis = self.engine.diagnose(failure["id"])
        self.assertFalse(diagnosis["confirmed"])
        result = self.engine.auto_repair(failure["id"], task_id="existing-task", cancel_event=self.cancel)
        self.assertEqual(result["code"], "diagnosis_required")
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())

    def test_protected_paths_and_missing_reproducer_are_refused(self):
        for filename in ("../outside.py", "config/api_keys.json", "core/repair_permissions.py"):
            plan = self.plan()
            plan["files"][0]["path"] = filename
            with self.subTest(filename=filename):
                self.assertFalse(self.repair(plan)["ok"])
        plan = self.plan()
        del plan["verify"]
        self.assertFalse(self.repair(plan)["ok"])
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())

    def test_read_search_and_persistent_history_keep_source_and_redact_secrets(self):
        contents = self.engine.read("actions/broken.py")
        self.assertEqual(contents["sha256"], hashlib.sha256(SOURCE.encode()).hexdigest())
        self.assertEqual(contents["content"], SOURCE)
        matches = self.engine.search("def broken")
        self.assertEqual(matches[0]["path"], "actions/broken.py")
        with self.assertRaises(PermissionError):
            self.engine.read("config/api_keys.json")
        secret = self.engine.record_failure("token=secret-for-journal", traceback_text="password=another-secret")
        reopened = self.module.RepairEngine(self.root, self.broker, self.commands)
        history = reopened.history()
        self.assertIn(secret["id"], [item["id"] for item in history])
        self.assertNotIn("secret-for-journal", json.dumps(history))
        self.assertNotIn("another-secret", json.dumps(history))
        for journal in (self.root / "logs/repair").glob("*.json"):
            json.loads(journal.read_text(encoding="utf-8"))

    def test_manual_history_rollback_restores_verified_repair(self):
        self.assertTrue(self.repair()["ok"])
        rollback = getattr(self.engine, "rollback", None)
        self.assertTrue(callable(rollback), "Verified history needs an explicit rollback operation")
        result = rollback(self.failure["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())
        self.assertEqual(self.record()["status"], "rolled_back")

    def test_manual_rollback_refuses_to_overwrite_later_user_changes(self):
        self.assertTrue(self.repair()["ok"])
        self.path.write_text("# user's newer source\n")
        rollback = getattr(self.engine, "rollback", None)
        self.assertTrue(callable(rollback), "History rollback must compare current file hashes")
        result = rollback(self.failure["id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_conflict")
        self.assertEqual(self.path.read_text(), "# user's newer source\n")

    def test_journal_write_failure_after_edit_does_not_prevent_rollback(self):
        save = self.engine._save
        def failed_save(record):
            if any(file.get("applied") for attempt in record["attempts"] for file in attempt["files"]):
                raise OSError("simulated full journal volume")
            return save(record)
        self.engine._save = failed_save
        result = self.repair()
        self.assertFalse(result["ok"])
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())
        self.assertIn("journal_error", result)

    def test_auto_binding_preserves_existing_bom_and_windows_newlines(self):
        original = b"\xef\xbb\xbf" + SOURCE.replace("\n", "\r\n").encode() + b"\r\n\r\n"
        self.path.write_bytes(original)
        result = self.engine.auto_repair(self.failure["id"], "existing-task", self.cancel)
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.path.read_bytes().startswith(original), "Only the missing binding should be appended")

    def test_repair_cannot_weaken_its_original_behavioral_test(self):
        tests = self.root / "tests"
        tests.mkdir()
        original = "import unittest\nclass Check(unittest.TestCase):\n def test_behavior(self): self.fail('original failure')\n"
        target = tests / "test_behavior.py"
        target.write_bytes(original.encode())
        plan = self.plan(verify={"kind": "tests", "start_dir": "tests", "pattern": "test_behavior.py"})
        plan["files"].append({"path": "tests/test_behavior.py", "expected_sha256": hashlib.sha256(original.encode()).hexdigest(),
                              "content": original.replace("self.fail('original failure')", "self.assertTrue(True)")})
        result = self.repair(plan)
        self.assertFalse(result["ok"], "The same original reproducer must survive the repair")
        self.assertEqual(target.read_bytes(), original.encode())
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())

    def test_non_test_commands_cannot_run_inside_a_source_transaction(self):
        self.commands.run = Mock(wraps=self.commands.run)
        result = self.repair(self.plan(tests=[{"kind": "python_version"}]))
        self.assertFalse(result["ok"])
        self.commands.run.assert_not_called()
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())

    def test_post_restart_behavior_failure_rolls_back_even_when_health_passed(self):
        self.engine.restart = Mock(return_value={"ok": True})
        original_run = self.commands.run
        contract_calls = 0
        def run(spec, cancel_event=None):
            nonlocal contract_calls
            if spec["kind"] == "tool_contract":
                contract_calls += 1
                if contract_calls == 3:
                    return {"ok": False, "exit_code": 1, "stderr": "behavior no longer passes"}
            return original_run(spec, cancel_event=cancel_event)
        self.commands.run = run
        result = self.repair(self.plan(restart=True))
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["code"], "verification_failed")
        self.assertEqual(self.path.read_bytes(), SOURCE.encode())

    def test_pre_edit_rejections_are_journaled_without_counting_as_a_patch_attempt(self):
        result = self.repair(self.plan(confidence=.1))
        self.assertFalse(result["ok"])
        record = self.record()
        self.assertEqual(record["last_outcome"]["code"], "diagnosis_required")
        self.assertEqual(record["last_outcome"]["task_id"], "existing-task")
        self.assertEqual(record["attempts"], [])

    def test_nested_observation_and_health_credentials_are_redacted(self):
        self.engine.observe = lambda: {"metadata": {"password": "never-journal-this"}}
        self.engine.restart = lambda: {"ok": True, "api_key": "never-journal-that"}
        result = self.repair(self.plan(restart=True))
        self.assertTrue(result["ok"], result)
        combined = json.dumps([result, self.record(), self.events])
        self.assertNotIn("never-journal-this", combined)
        self.assertNotIn("never-journal-that", combined)

    def test_failure_record_preserves_bounded_structured_evidence_and_exit_code(self):
        failure = self.engine.record_failure({"message": "Action failed", "path": "actions/broken.py", "exit_code": 17,
            "evidence": {"task": {"id": "existing-task", "state": "failed"}, "log_tail": "password=fixture-private-value"}})
        reopened = next(item for item in self.engine.history() if item["id"] == failure["id"])
        self.assertEqual(reopened["error"], "Action failed")
        self.assertEqual(reopened["path"], "actions/broken.py")
        self.assertEqual(reopened["exit_code"], 17)
        self.assertEqual(reopened["evidence"]["task"], {"id": "existing-task", "state": "failed"})
        self.assertNotIn("fixture-private-value", json.dumps([failure, reopened]))

    def test_failure_evidence_has_a_total_serialized_size_bound(self):
        evidence = {f"part_{index}": "a" * 40000 for index in range(4)}
        failure = self.engine.record_failure({"message": "Action failed", "evidence": evidence})
        self.assertIn("evidence", failure)
        self.assertLess(len(json.dumps(failure["evidence"])), 66000)
        self.assertTrue(failure["evidence"]["truncated"])


if __name__ == "__main__":
    unittest.main()
