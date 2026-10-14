"""Confirmation tests never show UI or execute real actions."""
import contextvars
import json
import threading
import unittest
from unittest.mock import Mock, patch

from core import confirm


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.names = ("_pending", "_show_cb", "_hide_cb", "_log_cb", "_result_cb", "_owner_cb", "_valid_cb")
        self.saved = {name: getattr(confirm, name) for name in self.names}
        with confirm._lock:
            confirm._pending = None
        self.now = 100.0
        self.owner = "task-original"
        self.owner_valid = True
        self.show, self.hide, self.log = Mock(), Mock(), Mock()
        self.results = []
        self.completed = threading.Event()
        self.timers = []

        def result(accepted, value, owner):
            self.results.append((accepted, value, owner))
            self.completed.set()

        def timer_factory(interval, function, args=None, kwargs=None):
            timer = Mock()
            timer.callback = lambda: function(*(args or ()), **(kwargs or {}))
            self.timers.append(timer)
            return timer

        self.clock_patch = patch.object(confirm.time, "monotonic", side_effect=lambda: self.now)
        self.timer_patch = patch.object(confirm.threading, "Timer", side_effect=timer_factory)
        self.clock_patch.start()
        self.timer_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.timer_patch.stop)
        self.addCleanup(self.restore)
        confirm.bind(self.show, self.hide, self.log, result=result,
                     owner=lambda: self.owner,
                     valid=lambda owner: self.owner_valid and owner == "task-original")

    def restore(self):
        with confirm._lock:
            for name, value in self.saved.items():
                setattr(confirm, name, value)

    def request(self, run, title="Original action"):
        return confirm.request("test-action", title, "Test details", run)

    def await_result(self):
        self.assertTrue(self.completed.wait(3), "Confirmation worker did not report completion")
        self.assertEqual(len(self.results), 1)
        return self.results[0]

    def test_success_runs_in_original_context_and_reports_original_owner(self):
        context = contextvars.ContextVar("confirmation-test-context", default="outside")
        token = context.set("original task context")
        run = Mock(side_effect=lambda: context.get())
        try:
            self.assertIn("CONFIRMATION_PENDING", self.request(run))
        finally:
            context.reset(token)
        self.owner = "different task at approval time"
        confirm.resolve(True)
        self.assertEqual(self.await_result(), (True, "original task context", "task-original"))
        run.assert_called_once()
        self.assertEqual(context.get(), "outside")
        self.assertEqual(confirm.pending_title(), "")

    def test_invalid_owner_never_runs_action(self):
        run = Mock(return_value="Done")
        self.request(run)
        self.owner_valid = False
        confirm.resolve(True)
        accepted, _, owner = self.await_result()
        self.assertFalse(accepted)
        self.assertEqual(owner, "task-original")
        run.assert_not_called()

    def test_owner_cancelled_between_resolve_and_worker_never_runs_action(self):
        run = Mock(return_value="Done")
        self.request(run)
        workers = []

        def queued_worker(*, target, **kwargs):
            workers.append(target)
            return Mock()

        with patch.object(confirm.threading, "Thread", side_effect=queued_worker):
            confirm.resolve(True)
        self.assertEqual(len(workers), 1)
        self.owner_valid = False
        workers[0]()
        self.assertFalse(self.await_result()[0])
        run.assert_not_called()

    def test_returned_tool_failures_are_not_successful_confirmations(self):
        for failure in ("Tool 'browser' failed: unavailable", {"ok": False, "error": "disconnected"},
                        json.dumps({"ok": False, "error": "disconnected"})):
            with self.subTest(failure=failure):
                self.results.clear()
                self.completed.clear()
                run = Mock(return_value=failure)
                self.request(run)
                confirm.resolve(True)
                accepted, value, owner = self.await_result()
                self.assertFalse(accepted)
                self.assertEqual(value, failure)
                self.assertEqual(owner, "task-original")
                run.assert_called_once()

    def test_pending_confirmation_cannot_be_replaced(self):
        original, replacement = Mock(return_value="Done"), Mock()
        self.request(original)
        first = confirm._pending
        answer = self.request(replacement, "Replacement action")
        self.assertIn("CONFIRMATION_PENDING", answer)
        self.assertIs(confirm._pending, first)
        self.assertEqual(confirm.pending_title(), "Original action")
        self.show.assert_called_once()
        self.assertEqual(len(self.timers), 1)
        confirm.resolve(True)
        self.assertTrue(self.await_result()[0])
        original.assert_called_once()
        replacement.assert_not_called()

    def test_expired_confirmation_never_runs_action(self):
        run = Mock(return_value="Done")
        self.request(run)
        self.now += confirm.TIMEOUT_SECONDS + 1
        confirm.resolve(True)
        accepted, result, owner = self.await_result()
        self.assertFalse(accepted)
        self.assertIn("expired", result.lower())
        self.assertEqual(owner, "task-original")
        run.assert_not_called()

    def test_old_expiry_callback_cannot_cancel_replacement(self):
        original, replacement = Mock(), Mock()
        self.request(original)
        old_timer = self.timers[0]
        self.now += confirm.TIMEOUT_SECONDS + 1
        self.request(replacement, "Replacement action")
        newer = confirm._pending
        old_timer.callback()
        self.assertIs(confirm._pending, newer)
        self.assertEqual(confirm.pending_title(), "Replacement action")
        self.hide.assert_not_called()
        self.assertEqual(self.results, [])
        original.assert_not_called()
        replacement.assert_not_called()

    def test_expiry_identity_rechecked_atomically_after_interleaved_request(self):
        original, replacement = Mock(), Mock()
        self.request(original)
        old_timer = self.timers[0]
        self.now += confirm.TIMEOUT_SECONDS + 1
        resolve = confirm.resolve
        replaced = []

        def interleave(accepted, *args, **kwargs):
            self.request(replacement, "Replacement action")
            replaced.append(confirm._pending)
            return resolve(accepted, *args, **kwargs)

        with patch.object(confirm, "resolve", side_effect=interleave):
            old_timer.callback()
        self.assertEqual(len(replaced), 1)
        self.assertIs(confirm._pending, replaced[0])
        self.assertEqual(confirm.pending_title(), "Replacement action")
        self.hide.assert_not_called()
        original.assert_not_called()
        replacement.assert_not_called()

    def test_decline_and_duplicate_resolution_do_not_execute(self):
        run = Mock()
        self.request(run)
        confirm.resolve(False)
        self.assertFalse(self.await_result()[0])
        confirm.resolve(True)
        run.assert_not_called()
        self.assertEqual(len(self.results), 1)


if __name__ == "__main__":
    unittest.main()
