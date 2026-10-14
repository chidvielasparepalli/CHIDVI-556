"""Live video transport tests use synthetic pixels and an in-memory session."""
import asyncio
from contextlib import contextmanager, suppress
import importlib
import io
import random
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from google.genai import types
from PIL import Image


def frame(width=1920, height=1080, noisy=False, identity=1):
    image = (Image.frombytes("RGB", (width, height), random.Random(4).randbytes(width * height * 3))
             if noisy else Image.new("RGB", (width, height), "navy"))
    image.putpixel((1, 1), (255, 255, 255))
    output = io.BytesIO()
    image.save(output, "PNG")
    return {"data": output.getvalue(), "mime_type": "image/png", "width": width, "height": height,
            "frame_id": identity, "_sharing": 1, "source_rect": {"left": -width, "top": 0, "width": width, "height": height}}


class Sharing:
    def __init__(self, state="LIVE"):
        self.state, self.generation = state, 1
        self.settings = {"max_width": 1280, "max_height": 720}
        self.latest = frame()
        self.sent, self.failures = [], []
        self.active = 0
        self.enter_hook = None
        self.continuous = False

    def snapshot(self):
        return {"state": self.state, "settings": dict(self.settings)}

    def stream_frame(self):
        return self.latest if self.state == "LIVE" and self.latest["frame_id"] not in self.sent else None

    def valid(self, value):
        return self.state == "LIVE" and value["_sharing"] == self.generation

    @contextmanager
    def transmission(self, value, kind="stream"):
        assert kind == "stream"
        if not self.valid(value):
            raise PermissionError("stale sharing lease")
        self.active += 1
        try:
            if self.enter_hook:
                self.enter_hook()
            yield True
        finally:
            self.active -= 1
            if self.state == "STOPPING" and not self.active:
                self.state = "OFF"

    def mark_sent(self, value):
        assert self.valid(value)
        self.sent.append(value["frame_id"])
        if self.continuous:
            self.latest = {**self.latest, "frame_id": value["frame_id"] + 1}

    def fail(self, reason):
        self.failures.append(reason)
        self.state = "ERROR"
        self.generation += 1


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


class LiveScreenStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stream = importlib.import_module("core.live_screen_stream")
        self.sharing = Sharing()
        self.session = type("Session", (), {})()
        self.session.send_realtime_input = AsyncMock()
        self.session.send_client_content = AsyncMock()
        self.session.receive = AsyncMock()
        self.workers = []

    async def asyncTearDown(self):
        for task in self.workers:
            task.cancel()
        for task in self.workers:
            with suppress(asyncio.CancelledError):
                await task

    def start(self, **kwargs):
        task = asyncio.create_task(self.stream.stream_screen(self.session, self.sharing, poll_interval=.005, **kwargs))
        self.workers.append(task)
        return task

    async def test_sends_real_sdk_video_blob_only_with_bounded_jpeg(self):
        original = self.sharing.latest["data"]
        self.start()
        await until(lambda: self.sharing.sent)
        args = self.session.send_realtime_input.await_args.kwargs
        self.assertEqual(set(args), {"video"})
        self.assertIsInstance(args["video"], types.Blob)
        self.assertEqual(args["video"].mime_type, "image/jpeg")
        self.assertLessEqual(len(args["video"].data), 512 * 1024)
        with Image.open(io.BytesIO(args["video"].data)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (1280, 720))
        self.assertIs(self.sharing.latest["data"], original)
        self.session.send_client_content.assert_not_called()
        self.session.receive.assert_not_called()

    async def test_inactive_states_do_not_encode_or_send(self):
        with patch.object(self.stream, "encode_video_frame", side_effect=AssertionError("No capture needed")):
            for state in ("OFF", "STARTING", "PAUSED", "STOPPING", "ERROR"):
                self.sharing.state = state
                task = self.start()
                await asyncio.sleep(.02)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self.session.send_realtime_input.assert_not_called()
        self.assertEqual(self.sharing.failures, [])

    async def test_sender_caps_provider_rate_even_if_service_produces_more(self):
        self.sharing.continuous = True
        times = []
        async def sent(**_):
            times.append(asyncio.get_running_loop().time())
        self.session.send_realtime_input.side_effect = sent
        self.start()
        await until(lambda: len(times) >= 2)
        self.assertGreaterEqual(times[1] - times[0], .99)

    async def test_frame_preparation_keeps_event_loop_responsive(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = self.sharing.stream_frame
        def prepare():
            entered.set()
            release.wait(1)
            finished.set()
            return original()
        with patch.object(self.sharing, "stream_frame", side_effect=prepare):
            self.start()
            try:
                await until(entered.is_set)
                self.assertFalse(finished.is_set(), "Frame preparation blocked the voice event loop")
                release.set()
                await until(lambda: self.sharing.sent)
            finally:
                release.set()

    async def test_service_rate_refusal_is_not_a_provider_failure(self):
        from core.screen_sharing import ScreenSharingError
        @contextmanager
        def denied(*_, **__):
            self.sharing.state = "PAUSED"
            raise ScreenSharingError("sharing_rate_limited", "Wait for the current budget")
            yield  # The real service also refuses while entering a context manager.
        with patch.object(self.sharing, "transmission", side_effect=denied):
            self.start()
            await until(lambda: self.sharing.state == "PAUSED")
        self.assertEqual(self.sharing.failures, [])
        self.session.send_realtime_input.assert_not_called()

    async def test_actual_service_receives_sent_event_for_its_signed_frame(self):
        from core.screen_sharing import LiveScreenSharing
        sample = {**frame(64, 48), "monitor": 1}
        service = LiveScreenSharing(capture=Mock(return_value=sample), cloud_allowed=lambda: True,
            observer=lambda: {"window_id": 7, "application": "code.exe", "title": "Synthetic editor"})
        self.addCleanup(service.close)
        with patch.object(service, "_spawn_locked"):
            service.start()
            service.frame()
        self.sharing = service
        self.start()
        await until(lambda: service.snapshot()["frames_sent"] == 1)
        self.assertEqual(service.snapshot()["active_requests"], 0)
        self.assertEqual(service.snapshot()["analyses"], 0)
        self.assertEqual(service.snapshot()["state"], "LIVE")
        self.session.send_realtime_input.assert_awaited_once()
        self.session.send_client_content.assert_not_called()
        self.session.receive.assert_not_called()

    async def test_pause_after_encoding_discards_frame_before_permit(self):
        entered, release = threading.Event(), threading.Event()
        encode = self.stream.encode_video_frame
        def blocked(value, settings):
            entered.set()
            release.wait(2)
            return encode(value, settings)
        with patch.object(self.stream, "encode_video_frame", side_effect=blocked):
            self.start()
            try:
                await until(entered.is_set)
                self.sharing.state = "PAUSED"
                release.set()
                await asyncio.sleep(.08)
            finally:
                release.set()
        self.session.send_realtime_input.assert_not_called()
        self.assertEqual(self.sharing.active, 0)

    async def test_rechecks_lease_inside_transmission_before_send(self):
        self.sharing.enter_hook = lambda: setattr(self.sharing, "state", "PAUSED")
        self.start()
        await until(lambda: self.sharing.state == "PAUSED")
        self.session.send_realtime_input.assert_not_called()
        self.assertEqual(self.sharing.active, 0)

    async def test_pause_and_stop_cancel_pending_send_and_drain_permit(self):
        for state in ("PAUSED", "STOPPING"):
            with self.subTest(state=state):
                self.sharing = Sharing()
                entered, cancelled = asyncio.Event(), asyncio.Event()
                async def waiting(**_):
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                self.session.send_realtime_input.reset_mock()
                self.session.send_realtime_input.side_effect = waiting
                task = self.start()
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual(self.sharing.active, 1)
                self.sharing.state = state
                await asyncio.wait_for(cancelled.wait(), .5)
                await until(lambda: self.sharing.active == 0)
                self.assertEqual(self.sharing.sent, [])
                self.assertEqual(self.sharing.failures, [])
                self.assertEqual(self.sharing.state, "OFF" if state == "STOPPING" else state)
                self.assertFalse(task.done())
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def test_session_cancellation_cancels_owned_send_before_exiting(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def waiting(**_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.session.send_realtime_input.side_effect = waiting
        task = self.start()
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.sharing.active, 0)
        self.assertEqual(self.sharing.sent, [])

    async def test_send_timeout_fails_sharing_without_raising_into_voice(self):
        async def waiting(**_):
            await asyncio.Event().wait()
        self.session.send_realtime_input.side_effect = waiting
        task = self.start(send_timeout=.03)
        await until(lambda: self.sharing.state == "ERROR")
        self.assertIn("TimeoutError", self.sharing.failures[0])
        self.assertEqual(self.sharing.active, 0)
        self.assertEqual(self.sharing.sent, [])
        await asyncio.sleep(.06)
        self.session.send_realtime_input.assert_awaited_once()
        self.assertFalse(task.done())

    async def test_network_and_rate_failures_are_safe_and_not_retried(self):
        class RateLimitError(RuntimeError):
            code = 429
        for error in (ConnectionError("secret token and frame payload"), RateLimitError("private quota details")):
            with self.subTest(kind=type(error).__name__):
                self.sharing = Sharing()
                self.session.send_realtime_input.reset_mock()
                self.session.send_realtime_input.side_effect = error
                task = self.start()
                await until(lambda: self.sharing.state == "ERROR")
                self.assertNotIn(str(error), self.sharing.failures[0])
                self.assertIn(type(error).__name__, self.sharing.failures[0])
                await asyncio.sleep(.03)
                self.session.send_realtime_input.assert_awaited_once()
                self.assertEqual(self.sharing.active, 0)
                self.assertFalse(task.done())
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def test_late_provider_error_cannot_replace_revocation_or_new_session(self):
        for state in ("OFF", "PAUSED", "STOPPING", "LIVE"):
            with self.subTest(state=state):
                self.sharing = Sharing()
                def revoked(**_):
                    self.sharing.state = state
                    self.sharing.generation += 1
                    raise ConnectionError("late private provider error")
                self.session.send_realtime_input.reset_mock()
                self.session.send_realtime_input.side_effect = revoked
                task = self.start()
                await until(lambda: self.session.send_realtime_input.await_count)
                await asyncio.sleep(.03)
                self.assertEqual(self.sharing.failures, [])
                self.assertEqual(self.sharing.state, "OFF" if state == "STOPPING" else state)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def test_corrupt_image_failure_does_not_touch_session(self):
        self.sharing.latest["data"] = b"not an image"
        self.start()
        await until(lambda: self.sharing.state == "ERROR")
        self.session.send_realtime_input.assert_not_called()

    def test_encoder_reduces_quality_to_byte_cap_and_honors_smaller_dimensions(self):
        noisy = frame(1280, 720, noisy=True)
        encoded = self.stream.encode_video_frame(noisy, {"max_width": 3840, "max_height": 2160})
        self.assertLessEqual(len(encoded), 512 * 1024)
        with Image.open(io.BytesIO(encoded)) as image:
            self.assertEqual(image.size, (1280, 720))
        encoded = self.stream.encode_video_frame(frame(), {"max_width": 640, "max_height": 360})
        with Image.open(io.BytesIO(encoded)) as image:
            self.assertEqual(image.size, (640, 360))
        with patch.object(self.stream, "MAX_FRAME_BYTES", 32), self.assertRaises(ValueError):
            self.stream.encode_video_frame(noisy, {"max_width": 1280, "max_height": 720})

    def test_explicit_full_hd_setting_is_honored_within_hard_pixel_cap(self):
        encoded = self.stream.encode_video_frame(frame(1920, 1080), {"max_width": 1920, "max_height": 1080})
        with Image.open(io.BytesIO(encoded)) as image:
            self.assertEqual(image.size, (1920, 1080))
        encoded = self.stream.encode_video_frame(frame(2560, 1440), {"max_width": 3840, "max_height": 2160})
        with Image.open(io.BytesIO(encoded)) as image:
            self.assertEqual(image.size, (1920, 1080))


if __name__ == "__main__":
    unittest.main()
