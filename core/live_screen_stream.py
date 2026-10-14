"""Bounded video frames on the existing Gemini Live voice connection.

Protocol: https://ai.google.dev/gemini-api/docs/live-api/get-started-sdk
Limits: https://ai.google.dev/gemini-api/docs/live-api/capabilities
Pricing: https://ai.google.dev/gemini-api/docs/pricing

The provider accepts at most one frame per second. Sending a frame does not prove
that it was interpreted. The sharing service owns consent, capture, freshness,
rate budgets and the transmission lease; this module never opens a connection,
reads responses, captures a screen, or retains a frame backlog.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, suppress
import io
import math

from google.genai import types
from PIL import Image


MAX_FRAME_BYTES = 512 * 1024
MAX_WIDTH, MAX_HEIGHT = 1920, 1080
MIN_FRAME_INTERVAL = 1.0
_TRANSMISSION_REFUSALS = {
    "sharing_inactive", "sharing_cancelled", "sharing_stale", "sharing_rate_limited",
    "sharing_busy", "sharing_consumed", "sharing_monitor_mismatch", "sharing_invalid_frame",
    "sharing_privacy", "sharing_capture_failed", "sharing_cloud_disabled", "sharing_expired",
}


def encode_video_frame(frame, settings):
    """Create a bounded JPEG; retain the full-resolution source for other tools."""
    data = frame.get("data")
    if (not isinstance(data, bytes) or not data or len(data) > 64 * 1024 * 1024
            or frame.get("mime_type") != "image/png"):
        raise ValueError("A valid bounded PNG screen frame is required")
    width, height = settings.get("max_width", 1280), settings.get("max_height", 720)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (width, height)):
        raise ValueError("Invalid live video dimensions")
    with Image.open(io.BytesIO(data)) as source:
        if (source.format != "PNG" or source.width * source.height > 80_000_000
                or source.size != (frame.get("width"), frame.get("height"))):
            raise ValueError("Screen frame dimensions or encoding are invalid")
        image = source.convert("RGB")
    try:
        image.thumbnail((min(width, MAX_WIDTH), min(height, MAX_HEIGHT)), Image.Resampling.LANCZOS)
        for quality in (75, 60, 45, 30):
            output = io.BytesIO()
            image.save(output, "JPEG", quality=quality, optimize=False)
            encoded = output.getvalue()
            if len(encoded) <= MAX_FRAME_BYTES:
                return encoded
    finally:
        image.close()
    raise ValueError("Screen frame could not be encoded within the live video byte limit")


async def _send_frame(session, sharing, frame, encoded, poll_interval, send_timeout):
    """Own and drain the send task before releasing the service transmission."""
    if not sharing.valid(frame):
        return False
    task = asyncio.create_task(session.send_realtime_input(
        video=types.Blob(data=encoded, mime_type="image/jpeg")))
    deadline = asyncio.get_running_loop().time() + send_timeout
    try:
        while True:
            if not sharing.valid(frame):
                return False
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Live screen frame send timed out")
            completed, _ = await asyncio.wait((task,), timeout=min(poll_interval, remaining))
            if completed:
                await task
                return sharing.valid(frame)
    finally:
        if not task.done():
            task.cancel()
        # The SDK's send is cancellation-aware. Do not release the transmission
        # while its owned coroutine can still write to the socket.
        with suppress(asyncio.CancelledError, Exception):
            await task


def _failure_message(stage, error):
    kind = type(error).__name__
    if getattr(error, "code", None) == 429:
        return f"Live screen sharing reached the provider rate limit ({kind}); press Start to retry."
    return f"Live screen {stage} failed ({kind}); sharing stopped. Press Start to retry."


async def stream_screen(session, sharing, *, poll_interval=.1, send_timeout=10):
    """Session-scoped sender; failures stop sharing without cancelling voice tasks.

    Inactive sharing remains idle until an explicit user start/resume. Cancellation
    of this coroutine drains its owned send and propagates to the session owner.
    """
    for value, label, minimum, maximum in ((poll_interval, "poll interval", .001, 1),
                                            (send_timeout, "send timeout", .01, 10)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"Invalid live screen {label}")
    loop = asyncio.get_running_loop()
    next_send = 0.0
    while True:
        await asyncio.sleep(poll_interval)
        stage = "sharing state"
        snapshot, frame = None, None
        try:
            snapshot = sharing.snapshot()
            if snapshot.get("state") != "LIVE" or loop.time() < next_send:
                continue
            stage = "frame preparation"
            frame = await asyncio.to_thread(sharing.stream_frame)
            if frame is None or not sharing.valid(frame):
                continue
            stage = "frame encoding"
            encoded = await asyncio.to_thread(encode_video_frame, frame, snapshot.get("settings", {}))
            if not sharing.valid(frame):
                continue
            with ExitStack() as permits:
                stage = "transmission permission"
                try:
                    permits.enter_context(sharing.transmission(frame, kind="stream"))
                except Exception as error:
                    if getattr(error, "code", None) in _TRANSMISSION_REFUSALS:
                        continue
                    raise
                if not sharing.valid(frame):
                    continue
                stage = "send"
                next_send = loop.time() + MIN_FRAME_INTERVAL
                if await _send_frame(session, sharing, frame, encoded, poll_interval, send_timeout):
                    sharing.mark_sent(frame)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Never expose SDK exception text, pixels, credentials or URLs.
            try:
                current = sharing.snapshot()
                if (snapshot is None or current.get("state") != "LIVE"
                        or current.get("session_id") != snapshot.get("session_id")
                        or current.get("generation") != snapshot.get("generation")
                        or frame is not None and not sharing.valid(frame)):
                    continue  # Late failures do not replace Stop, Pause or a new session.
                sharing.fail(_failure_message(stage, error))
            except Exception:
                return  # A broken status callback must not terminate voice.
