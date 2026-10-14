import unittest
from core.task_control import TaskStore, check_task_cancelled, result_state


class TaskTests(unittest.TestCase):
    def test_tool_failure_and_confirmation_do_not_become_success(self):
        for result in ('{"ok":false,"error":"lost device"}', "Android control failed: offline",
                       "Browser error (click): ambiguous", "Browser action timed out (60s)."):
            self.assertEqual(result_state(result), "failed")
        self.assertEqual(result_state("[CONFIRMATION_PENDING] Approve"), "waiting_for_user_input")
        self.assertEqual(result_state({"ok": False, "code": "image_review_pending"}), "waiting_for_user_input")
        self.assertEqual(result_state('{"ok":true,"verified":null}'), "verifying")

    def test_reconnect_is_idempotent_and_conflicting_replay_rejected(self):
        store = TaskStore()
        first = store.submit("open browser", "one", "phone")
        self.assertEqual(first, store.submit("open browser", "one", "phone"))
        with self.assertRaises(ValueError):
            store.submit("delete files", "one", "phone")
        self.assertEqual(len(store.snapshot()), 1)

    def test_cancel_does_not_release_inflight_operation_or_allow_late_success(self):
        store = TaskStore()
        first = store.submit("one", "one")
        store.submit("two", "two")
        self.assertEqual(store.claim()["id"], first["id"])
        store.cancel(first["id"])
        self.assertTrue(store.cancelled())
        self.assertIsNone(store.claim())
        self.assertEqual(store.update(first["id"], "succeeded")["state"], "cancelled")
        store.release(first["id"])
        self.assertEqual(store.claim()["text"], "two")

    def test_history_bounds_preserve_replay_protection(self):
        store = TaskStore(limit=1)
        store.submit("one", "one")
        with self.assertRaises(ValueError):
            store.submit("two", "two")
        self.assertEqual(store.submit("one", "one")["text"], "one")

    def test_voice_claim_selects_its_exact_task_despite_older_queue(self):
        store = TaskStore()
        older = store.submit("open browser", "typed-1", "desktop")
        voice = store.submit("look at this page", "voice-1", "paired-phone", input_delivered=True)
        claimed = store.claim(voice["id"])
        self.assertEqual(claimed["id"], voice["id"])
        self.assertTrue(claimed["input_delivered"])
        self.assertEqual(store.get(older["id"])["state"], "queued")
        store.update(voice["id"], "succeeded")
        store.release(voice["id"])
        self.assertIsNone(store.claim("missing-id"))
        self.assertEqual(store.claim()["id"], older["id"])

    def test_input_delivery_is_preserved_on_replay_and_in_status(self):
        updates = []
        store = TaskStore(changed=updates.append)
        typed = store.submit("typed", "typed-1")
        voice = store.submit("spoken", "voice-1", "phone", input_delivered=True)
        self.assertFalse(typed["input_delivered"])
        self.assertTrue(store.get(voice["id"])["input_delivered"])
        self.assertTrue(updates[-1]["input_delivered"])
        self.assertTrue(store.submit("spoken", "voice-1", "phone")["input_delivered"])
        self.assertEqual(len(store.snapshot()), 2)

    def test_pending_cap_includes_active_tasks_and_keeps_replay_valid(self):
        store = TaskStore()
        queued = [store.submit(f"command {i}", f"request-{i}") for i in range(32)]
        store.claim(queued[0]["id"])
        store.update(queued[0]["id"], "waiting_for_user_input")
        with self.assertRaises(ValueError):
            store.submit("over capacity", "request-overflow")
        self.assertEqual(store.submit("command 5", "request-5")["id"], queued[5]["id"])
        store.cancel(queued[0]["id"])
        replacement = store.submit("replacement", "request-replacement")
        self.assertEqual(replacement["state"], "queued")
        self.assertEqual(sum(task["state"] not in {"succeeded", "failed", "cancelled"}
                             for task in store.snapshot()), 32)
        self.assertIsNone(store.claim(), "Cancelled in-flight task still owns execution until released")

    def test_snapshot_keeps_old_pending_and_recent_terminal_tasks_in_order(self):
        store = TaskStore()
        waiting = store.submit("old queued command", "keep-queued", "phone")
        completed = []
        for i in range(105):
            task = store.submit(f"finished {i}", f"done-{i}")
            store.claim(task["id"])
            store.update(task["id"], "succeeded")
            store.release(task["id"])
            completed.append(task["id"])
        newest = store.submit("latest queued command", "last-queued")
        snapshot = store.snapshot()
        self.assertEqual([task["id"] for task in snapshot],
                         [waiting["id"], *completed[-100:], newest["id"]])
        snapshot[0]["state"] = "failed"
        self.assertEqual(store.get(waiting["id"])["state"], "queued")

    def test_original_context_remains_cancelled_after_active_task_switches(self):
        store = TaskStore()
        original = store.submit("original", "original")
        replacement = store.submit("replacement", "replacement")
        store.claim(original["id"])
        with store.context(original["id"]):
            self.assertEqual(store.owner(), original["id"])
            store.cancel(original["id"])
            store.release(original["id"])
            store.claim(replacement["id"])
            self.assertEqual(store.owner(), original["id"])
            self.assertTrue(store.cancelled())
            with self.assertRaises(RuntimeError):
                check_task_cancelled()
            with store.context(replacement["id"]):
                self.assertEqual(store.owner(), replacement["id"])
                self.assertFalse(store.cancelled())
                check_task_cancelled()
            self.assertTrue(store.cancelled())
        self.assertEqual(store.owner(), replacement["id"])
        self.assertFalse(store.cancelled())

    def test_failed_task_sets_event_and_cannot_be_revived(self):
        store = TaskStore()
        task = store.submit("operation", "failed-operation")
        store.claim(task["id"])
        store.update(task["id"], "failed", "Device disconnected")
        self.assertTrue(store.event(task["id"]).is_set())
        self.assertTrue(store.cancelled())
        self.assertFalse(store.can_confirm(task["id"]))
        with store.context(task["id"]), self.assertRaises(RuntimeError):
            check_task_cancelled()
        self.assertEqual(store.update(task["id"], "succeeded")["state"], "failed")

    def test_confirmation_is_owned_by_current_live_uncancelled_task(self):
        store = TaskStore()
        first = store.submit("first", "first")
        second = store.submit("second", "second")
        self.assertFalse(store.can_confirm(None))
        self.assertFalse(store.can_confirm("missing"))
        self.assertFalse(store.can_confirm(first["id"]))
        store.claim(first["id"])
        store.update(first["id"], "waiting_for_user_input")
        self.assertTrue(store.can_confirm(first["id"]))
        self.assertFalse(store.can_confirm(second["id"]))
        store.update(first["id"], "succeeded")
        self.assertFalse(store.can_confirm(first["id"]))
        store.release(first["id"])
        store.claim(second["id"])
        self.assertFalse(store.can_confirm(first["id"]))
        self.assertTrue(store.can_confirm(second["id"]))
        store.event(second["id"]).set()
        self.assertFalse(store.can_confirm(second["id"]))

    def test_status_callback_gets_copy_and_validation(self):
        updates = []
        store = TaskStore(changed=updates.append)
        task = store.submit("one", "one")
        updates[0]["state"] = "failed"
        self.assertEqual(store.get(task["id"])["state"], "queued")
        for text in ("", "x" * 8001, None):
            with self.assertRaises(ValueError):
                store.submit(text, "bad")


class RepairExecutorTests(unittest.TestCase):
    def test_model_queue_does_not_claim_local_repair_work(self):
        store = TaskStore()
        repair = store.submit('diagnose', 'repair-request', executor='repair')
        voice = store.submit('hello', 'voice-request')
        self.assertEqual(store.claim()['id'], voice['id'])
        store.release(voice['id'])
        self.assertIsNone(store.claim())
        self.assertEqual(store.claim(repair['id'])['executor'], 'repair')


if __name__ == "__main__":
    unittest.main()
