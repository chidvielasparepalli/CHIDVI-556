"""Exact-image approvals use synthetic bytes and no real screen or provider."""
import contextvars
import threading
import time
import unittest
from unittest.mock import Mock, patch

from core import desktop_control as desktop
from core.task_control import TaskStore


class ImageApprovalTests(unittest.TestCase):
    def setUp(self):
        self.service = desktop.DesktopControl()
        self.service.cloud_vision_enabled = True
        self.tasks = TaskStore()
        self.task = self.tasks.submit("Describe the selected screen", "screen")
        self.tasks.claim(self.task["id"])
        self.context = self.tasks.context(self.task["id"])
        self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.service.bind_feedback(None, None, self.tasks.cancelled)
        self.data = b"immutable synthetic image bytes"
        self.requests = []
        self.ready = threading.Event()
        self.service.bind_image_review(self.approve)

    def approve(self, request):
        self.requests.append(request)
        request.mark_visible()
        request.resolve("approved")

    def queued(self, request):
        self.requests.append(request)
        request.mark_visible()
        self.ready.set()

    def start_review(self, **kwargs):
        outcomes = []
        context = contextvars.copy_context()
        def run():
            try:
                outcomes.append(self.service.review_image(self.data, **kwargs))
            except desktop.DesktopError as error:
                outcomes.append(error)
        thread = threading.Thread(target=lambda: context.run(run))
        thread.start()
        self.addCleanup(thread.join, 1)
        return thread, outcomes

    def assert_code(self, code, operation):
        with self.assertRaises(desktop.ImageReviewError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_receipt_preserves_exact_immutable_bytes_and_safe_snapshot(self):
        receipt = self.service.review_image(self.data, question="What is visible?", provider="Test provider")
        self.assertIs(receipt.data, self.data)
        self.assertEqual(receipt.status, "approved")
        self.assertEqual(receipt.task_id, self.task["id"])
        self.assertEqual(receipt.task_text, self.task["text"])
        self.assertEqual(receipt.snapshot()["provider"], "Test provider")
        self.assertNotIn("data", receipt.snapshot())
        self.assertNotIn(repr(self.data), repr(receipt.snapshot()))
        with self.assertRaises(AttributeError):
            receipt.data = b"replacement"
        self.assertFalse(receipt.resolve("approved"))
        self.assertEqual(self.tasks.get(self.task["id"])["state"], "executing")

    def test_boolean_callback_return_does_not_approve(self):
        self.service.bind_image_review(lambda request: request.mark_visible() or True)
        self.assert_code("image_review_expired", lambda: self.service.review_image(self.data, timeout=.03))

    def test_missing_preview_ack_cannot_approve(self):
        self.service.bind_image_review(lambda request: request.resolve("approved"))
        self.assert_code("image_review_failed", lambda: self.service.review_image(self.data))

    def test_unavailable_broken_and_unshown_preview_are_distinct_safe_failures(self):
        self.service.bind_image_review(None)
        self.assert_code("image_review_unavailable", lambda: self.service.review_image(self.data))
        def broken(_):
            raise RuntimeError("secret-api-key-value")
        self.service.bind_image_review(broken)
        error = self.assert_code("image_review_failed", lambda: self.service.review_image(self.data))
        self.assertNotIn("secret-api-key-value", str(error))
        self.assertIn("RuntimeError", str(error))
        self.service.bind_image_review(lambda request: None)
        with patch.object(desktop, "IMAGE_PREVIEW_TIMEOUT", .02):
            self.assert_code("image_review_unavailable", lambda: self.service.review_image(self.data, timeout=.1))

    def test_rejection_restores_task_and_notifies_once(self):
        changes = []
        def reject(request):
            request.changed = lambda value: changes.append(value.status)
            request.mark_visible()
            request.resolve("rejected")
            self.assertFalse(request.resolve("approved"))
        self.service.bind_image_review(reject)
        self.assert_code("image_review_rejected", lambda: self.service.review_image(self.data))
        self.assertEqual(changes.count("rejected"), 1)
        self.assertEqual(self.tasks.get(self.task["id"])["state"], "executing")

    def test_duplicate_pending_request_keeps_identity_and_one_preview(self):
        self.service.bind_image_review(self.queued)
        thread, outcomes = self.start_review(question="Describe")
        self.assertTrue(self.ready.wait(1))
        original = self.requests[0]
        error = self.assert_code("image_review_pending", lambda: self.service.review_image(self.data, question="Describe"))
        self.assertEqual(error.request_id, original.id)
        self.assertEqual(len(self.requests), 1)
        original.resolve("rejected")
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes[0].code, "image_review_rejected")

    def test_different_request_expires_old_and_late_approval_is_refused(self):
        self.service.bind_image_review(self.queued)
        thread, outcomes = self.start_review(question="First")
        self.assertTrue(self.ready.wait(1))
        original = self.requests[0]
        self.service.bind_image_review(self.approve)
        replacement = self.service.review_image(b"new pixels", question="Second")
        thread.join(1)
        self.assertEqual(outcomes[0].code, "image_review_expired")
        self.assertNotEqual(original.id, replacement.id)
        self.assertFalse(original.resolve("approved"))

    def test_task_cancellation_during_visible_preview_wakes_waiter(self):
        self.service.bind_image_review(self.queued)
        thread, outcomes = self.start_review()
        self.assertTrue(self.ready.wait(1))
        self.tasks.cancel(self.task["id"])
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes[0].code, "image_review_cancelled")
        self.assertEqual(self.requests[0].status, "cancelled")

    def test_observation_guard_cancels_before_approval(self):
        valid = [True]
        def stop(request):
            request.mark_visible()
            valid[0] = False
            request.resolve("approved")
        self.service.bind_image_review(stop)
        self.assert_code("image_review_cancelled", lambda: self.service.review_image(self.data, valid=lambda: valid[0]))

    def test_completed_or_released_origin_cannot_approve(self):
        for state in ("succeeded", "released"):
            with self.subTest(state=state):
                tasks = TaskStore()
                task = tasks.submit("Preview", state)
                tasks.claim(task["id"])
                self.service.bind_feedback(None, None, tasks.cancelled)
                def ended(request):
                    request.mark_visible()
                    if state == "succeeded":
                        tasks.update(task["id"], "succeeded")
                    else:
                        tasks.release(task["id"])
                    self.assertFalse(request.resolve("approved"))
                self.service.bind_image_review(ended)
                with tasks.context(task["id"]):
                    self.assert_code("image_review_cancelled", lambda: self.service.review_image(self.data))

    def test_wrong_pixels_mime_or_operation_cannot_consume(self):
        for values in ((b"other bytes", "image/png", "Question"),
                       (self.data, "image/jpeg", "Question"),
                       (self.data, "image/png", "Different question")):
            with self.subTest(values=values[1:]):
                receipt = self.service.review_image(self.data, question="Question")
                self.assert_code("image_review_failed", lambda: self.service.consume_image_approval(receipt, *values[:2], question=values[2]))

    def test_wrong_task_context_cannot_consume_even_if_original_is_active(self):
        receipt = self.service.review_image(self.data)
        other = self.tasks.submit("Other", "other")
        with self.tasks.context(other["id"]):
            self.assert_code("image_review_cancelled", lambda: self.service.consume_image_approval(receipt, self.data, "image/png"))

    def test_receipt_is_single_use_and_finish_clears_pending(self):
        receipt = self.service.review_image(self.data, question="Describe")
        changes = []
        receipt.changed = lambda value: changes.append(value.status)
        self.assertTrue(self.service.consume_image_approval(receipt, self.data, "image/png", question="Describe"))
        self.assertEqual(receipt.status, "consumed")
        self.assertIsNone(self.service._pending_image)
        self.assert_code("image_review_failed", lambda: self.service.consume_image_approval(receipt, self.data, "image/png", question="Describe"))
        self.service.finish_image_review(receipt)
        self.assertEqual(receipt.status, "completed")
        self.assertEqual(changes, ["consumed", "completed"])
        next_receipt = self.service.review_image(self.data, question="Describe")
        self.assertNotEqual(next_receipt.id, receipt.id)

    def test_expired_or_revoked_approval_cannot_consume(self):
        receipt = self.service.review_image(self.data, timeout=.02)
        with patch.object(receipt, "_clock", return_value=receipt.deadline + .01):
            self.assert_code("image_review_expired", lambda: self.service.consume_image_approval(receipt, self.data, "image/png"))
        receipt = self.service.review_image(self.data)
        self.service.cloud_vision_enabled = False
        self.service.cloud_vision_enabled = True
        self.assert_code("image_review_cancelled", lambda: self.service.consume_image_approval(receipt, self.data, "image/png"))

    def test_stale_approved_receipt_does_not_block_fresh_explicit_review(self):
        receipt = self.service.review_image(self.data, timeout=.02)
        with patch.object(receipt, "_clock", return_value=receipt.deadline + .01):
            replacement = self.service.review_image(self.data)
        self.assertNotEqual(replacement.id, receipt.id)
        self.assertEqual(receipt.status, "expired")
        self.assertEqual(replacement.status, "approved")

    def test_callback_failure_after_decision_still_refuses_upload(self):
        def broken_after_approval(request):
            self.approve(request)
            raise RuntimeError("untrusted exception payload")
        self.service.bind_image_review(broken_after_approval)
        error = self.assert_code("image_review_failed", lambda: self.service.review_image(self.data))
        self.assertNotIn("untrusted exception payload", str(error))

    def test_receipt_from_different_service_is_not_accepted(self):
        receipt = self.service.review_image(self.data)
        other = desktop.DesktopControl()
        other.cloud_vision_enabled = True
        self.assert_code("image_review_failed", lambda: other.consume_image_approval(receipt, self.data, "image/png"))

    def test_standalone_preview_has_explicit_identity_without_task(self):
        with self.tasks.context(None):
            receipt = self.service.review_image(self.data, question="Standalone")
            self.assertEqual(receipt.task_id, receipt.id)
            self.assertEqual(receipt.task_text, "Standalone")
            self.assertTrue(self.service.consume_image_approval(receipt, self.data, "image/png", question="Standalone"))


if __name__ == "__main__":
    unittest.main()
