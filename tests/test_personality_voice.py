"""Exercise real runtime methods with mocked speech, networking and persistence."""

import ast
import asyncio
from contextlib import suppress
from datetime import datetime
import os
from pathlib import Path
import threading
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLineEdit, QMainWindow, QWidget

from core.reference_voice import ReferenceVoiceUnavailable
from Personality.manager import PersonalityManager
from personality_joystick import PersonalityJoystick
from ui import MainWindow


ROOT = Path(__file__).resolve().parents[1]
METHODS = {
    "_apply_personality", "_on_personality_joystick_change", "interrupt",
    "_configure_reference_voice", "_queue_audio_pcm", "_discard_queued_audio",
    "_synthesize_reference_audio", "_receive_audio",
}
# Importing main patches subprocess.Popen and imports camera/network integrations.
# Compile its actual methods unchanged so these tests cannot start those services.
source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
runtime_class = next(node for node in source.body
                     if isinstance(node, ast.ClassDef) and node.name == "JarvisLive")
runtime_class.body = [node for node in runtime_class.body if getattr(node, "name", None) in METHODS]
assert {node.name for node in runtime_class.body} == METHODS
RUNTIME_GLOBALS = {
    "asyncio": asyncio, "time": time, "datetime": datetime, "traceback": traceback,
    "BASE_DIR": ROOT, "ReferenceVoiceUnavailable": ReferenceVoiceUnavailable,
    "load_reference_voice": Mock(return_value=None), "_clean_transcript": str.strip,
    "_STOP_WORDS": (), "print": Mock(),
}
exec(compile(ast.Module(body=[runtime_class], type_ignores=[]), str(ROOT / "main.py"), "exec"),
     RUNTIME_GLOBALS)


class Runtime(RUNTIME_GLOBALS["JarvisLive"]):
    def __init__(self, personality="tony"):
        self._personality = PersonalityManager(personality)
        self._reference_voice = None
        self._reference_audio_queue = asyncio.Queue()
        self.audio_in_queue = asyncio.Queue()
        self._voice_generation = 0
        self._reference_generating = False
        self._reference_turn_open = False
        self._interrupted = False
        self._turn_done_event = asyncio.Event()
        self._session_log = ["User: Keep this conversation"]
        self.memory = {"tony": {"project": "CHIDVI-556"}}
        self._resume_handle = "existing-conversation"
        self._asst_name = "CHIDVI"
        self._dashboard = None
        self._personality_reconnect_pending = False
        self._personality_reconnect_reason = ""
        self._game_stop = threading.Event()
        self.ui = SimpleNamespace(write_log=Mock(), set_personality_state=Mock())
        self._persist_personality = Mock()
        self.request_reconnect = Mock()
        self.set_speaking = Mock()


def response(*, pcm=b"", text=None, complete=False, interrupted=False):
    return SimpleNamespace(
        data=pcm, tool_call=None, session_resumption_update=None,
        server_content=SimpleNamespace(
            output_transcription=SimpleNamespace(text=text) if text else None,
            input_transcription=None, turn_complete=complete, interrupted=interrupted,
        ),
    )


class ScriptedSession:
    def __init__(self, responses):
        self.responses = responses
        self.processed = asyncio.Event()

    async def receive(self):
        for item in self.responses:
            yield item
        self.processed.set()
        await asyncio.Future()


class PersonalityVoiceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = Runtime()

    async def receive(self, *responses):
        self.runtime.session = ScriptedSession(responses)
        task = asyncio.create_task(self.runtime._receive_audio())
        try:
            await asyncio.wait_for(self.runtime.session.processed.wait(), 2)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def synthesize(self, *, while_generating=None, error=None):
        runtime = self.runtime
        runtime._reference_voice = Mock()
        runtime._reference_voice.synthesize = Mock(return_value=b"\x12\x34" * 2500, side_effect=error)
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def fake_to_thread(function, *args):
            started.set()
            await release.wait()
            try:
                return function(*args)
            finally:
                finished.set()

        with patch.object(asyncio, "to_thread", side_effect=fake_to_thread):
            task = asyncio.create_task(runtime._synthesize_reference_audio())
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.assertTrue(runtime._reference_generating)
                self.assertFalse(runtime._turn_done_event.is_set())
                if while_generating:
                    while_generating()
                release.set()
                await asyncio.wait_for(finished.wait(), 2)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def test_native_pcm_keeps_existing_chunking_and_transcript(self):
        pcm = b"\x01\x02" * 2500
        await self.receive(response(pcm=pcm, text="Native response.", complete=True))
        chunks = [self.runtime.audio_in_queue.get_nowait() for _ in range(3)]
        self.assertEqual([len(chunk) for chunk in chunks], [2400, 2400, 200])
        self.assertEqual(b"".join(chunks), pcm)
        self.assertTrue(self.runtime._reference_audio_queue.empty())
        self.assertTrue(self.runtime._turn_done_event.is_set())
        self.assertEqual(self.runtime._session_log[-1], "CHIDVI: Native response.")

    async def test_reference_voice_replaces_native_pcm_after_complete_transcript(self):
        self.runtime._reference_voice = Mock()
        await self.receive(response(pcm=b"native audio", text="Good evening."),
                           response(text="State your problem.", complete=True))
        self.assertTrue(self.runtime.audio_in_queue.empty())
        self.assertFalse(self.runtime._turn_done_event.is_set())
        await self.synthesize()
        self.runtime._reference_voice.synthesize.assert_called_once_with("Good evening. State your problem.")
        chunks = [self.runtime.audio_in_queue.get_nowait() for _ in range(3)]
        self.assertEqual(b"".join(chunks), b"\x12\x34" * 2500)
        self.assertTrue(self.runtime._turn_done_event.is_set())
        self.assertEqual(self.runtime._session_log[-1], "CHIDVI: Good evening. State your problem.")

    async def test_interrupt_discards_queued_and_in_flight_speech(self):
        runtime = self.runtime
        runtime._reference_audio_queue.put_nowait((0, "Old response"))
        runtime._reference_audio_queue.put_nowait((0, "Also old"))
        runtime.audio_in_queue.put_nowait(b"queued audio")
        await self.synthesize(while_generating=runtime.interrupt)
        self.assertTrue(runtime.audio_in_queue.empty())
        self.assertTrue(runtime._reference_audio_queue.empty())
        self.assertEqual(runtime._voice_generation, 1)
        self.assertFalse(runtime._turn_done_event.is_set())
        self.assertEqual(runtime._session_log, ["User: Keep this conversation"])
        self.assertEqual(runtime.memory, {"tony": {"project": "CHIDVI-556"}})

    async def test_interrupt_after_upstream_completion_preserves_next_response(self):
        runtime = self.runtime
        runtime._reference_voice = Mock()
        await self.receive(response(text="Completed old turn.", complete=True))
        await self.synthesize(while_generating=runtime.interrupt)
        self.assertTrue(runtime.audio_in_queue.empty())
        self.assertFalse(runtime._interrupted)

        await self.receive(response(text="Next response must be heard.", complete=True))
        await self.synthesize()
        runtime._reference_voice.synthesize.assert_called_once_with("Next response must be heard.")
        self.assertFalse(runtime.audio_in_queue.empty())
        self.assertTrue(runtime._turn_done_event.is_set())
        self.assertEqual(runtime._session_log[-1], "CHIDVI: Next response must be heard.")

    async def test_interrupt_pauses_active_repair_observation(self):
        observation = Mock()
        observation.snapshot.return_value = {'mode': 'running'}
        self.runtime._repair = SimpleNamespace(observation=observation)
        self.runtime.interrupt()
        observation.pause.assert_called_once_with()

    async def test_interrupt_during_upstream_turn_drains_only_that_turn(self):
        runtime = self.runtime
        runtime._reference_voice = Mock()
        await self.receive(response(pcm=b"old native audio", text="Old partial response"))
        self.assertTrue(runtime._reference_turn_open)
        runtime.interrupt()
        self.assertTrue(runtime._interrupted)
        await self.receive(response(text="Discarded old ending.", complete=True),
                           response(text="Fresh response.", complete=True))
        self.assertFalse(runtime._interrupted)
        await self.synthesize()
        runtime._reference_voice.synthesize.assert_called_once_with("Fresh response.")
        self.assertFalse(runtime.audio_in_queue.empty())
        self.assertEqual(runtime._session_log, ["User: Keep this conversation", "CHIDVI: Fresh response."])

    async def test_confirmed_switch_discards_old_voice_and_keeps_context(self):
        runtime = self.runtime
        runtime._reference_audio_queue.put_nowait((0, "Old personality speaking"))
        await self.synthesize(while_generating=lambda: runtime._apply_personality("wednesday"))
        self.assertTrue(runtime.audio_in_queue.empty())
        self.assertEqual(runtime._personality.active_id, "wednesday")
        runtime.request_reconnect.assert_called_once_with(keep_context=True, reason="personality: Wednesday Addams")
        self.assertEqual(runtime._resume_handle, "existing-conversation")
        self.assertEqual(runtime._session_log, ["User: Keep this conversation"])

    async def test_synthesis_error_leaves_transcript_and_unblocks_turn(self):
        self.runtime._reference_voice = Mock()
        await self.receive(response(text="Still readable.", complete=True))
        await self.synthesize(error=RuntimeError("mock model error"))
        self.assertTrue(self.runtime.audio_in_queue.empty())
        self.assertTrue(self.runtime._turn_done_event.is_set())
        self.assertEqual(self.runtime._session_log[-1], "CHIDVI: Still readable.")
        self.assertIn("remains available as text", self.runtime.ui.write_log.call_args.args[0])

    async def test_no_transcript_does_not_synthesize_or_leave_turn_blocked(self):
        voice = self.runtime._reference_voice = Mock()
        await self.receive(response(pcm=b"unusable native output", complete=True))
        voice.synthesize.assert_not_called()
        self.assertTrue(self.runtime.audio_in_queue.empty())
        self.assertTrue(self.runtime._reference_audio_queue.empty())
        self.assertTrue(self.runtime._turn_done_event.is_set())

    async def test_server_interruption_discards_partial_and_queued_transcripts(self):
        self.runtime._reference_voice = Mock()
        self.runtime._reference_audio_queue.put_nowait((0, "Queued old sentence"))
        self.runtime.audio_in_queue.put_nowait(b"queued old PCM")
        await self.receive(response(text="Interrupted fragment"), response(interrupted=True),
                           response(text="Fresh response.", complete=True))
        self.assertTrue(self.runtime.audio_in_queue.empty())
        self.assertEqual(self.runtime._reference_audio_queue.get_nowait(), (1, "Fresh response."))
        self.assertTrue(self.runtime._reference_audio_queue.empty())

    async def test_new_turn_stays_open_when_previous_synthesis_finishes(self):
        runtime = self.runtime
        runtime._reference_audio_queue.put_nowait((0, "Previous response"))
        await self.synthesize(while_generating=lambda: setattr(runtime, "_reference_turn_open", True))
        self.assertFalse(runtime._turn_done_event.is_set())
        self.assertFalse(runtime.audio_in_queue.empty())


class VoiceSelectorWindow(QMainWindow):
    _on_escape = MainWindow._on_escape
    _do_interrupt = MainWindow._do_interrupt
    _on_personality_selected = MainWindow._on_personality_selected

    def __init__(self, runtime):
        super().__init__()
        self.setCentralWidget(QWidget(self))
        self.resize(1100, 800)
        self.editor = QLineEdit(self.centralWidget())
        self._personality_joystick = PersonalityJoystick(self.centralWidget(), "tony")
        self._personality_joystick.sync_geometry(self.centralWidget().size())
        self._personality_joystick.selected.connect(self._on_personality_selected)
        self.on_personality_change = Mock(side_effect=runtime._on_personality_joystick_change)
        self.on_interrupt = Mock(side_effect=runtime.interrupt)
        self.shortcut = QShortcut(QKeySequence("Escape"), self)
        self.shortcut.setAutoRepeat(False)
        self.shortcut.activated.connect(self._on_escape)


class PersonalityVoiceSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_wednesday_preview_escape_and_confirmed_voice_activation(self):
        runtime = Runtime()
        previous_voice = runtime._reference_voice = Mock()
        wednesday_voice = Mock()
        factory = Mock(return_value=wednesday_voice)
        window = VoiceSelectorWindow(runtime)
        joystick = window._personality_joystick
        try:
            window.show()
            window.activateWindow()
            window.editor.setFocus()
            self.app.processEvents()
            with patch.dict(RUNTIME_GLOBALS, load_reference_voice=factory):
                joystick._open_selector()
                selector = joystick._selector
                target = selector._node_position(selector._ids.index("wednesday"))
                QTest.mouseMove(selector, target.toPoint())
                self.assertEqual(selector._highlighted, "wednesday")
                self.assertEqual(runtime._personality.active_id, "tony")
                QTest.keyClick(selector, Qt.Key.Key_Escape)
                self.assertFalse(selector.isVisible())
                self.assertIs(runtime._reference_voice, previous_voice)
                self.assertEqual(runtime._personality.active_id, "tony")
                factory.assert_not_called()
                previous_voice.synthesize.assert_not_called()
                window.on_personality_change.assert_not_called()
                window.on_interrupt.assert_not_called()
                runtime._persist_personality.assert_not_called()
                self.assertEqual(runtime._session_log, ["User: Keep this conversation"])
                self.assertEqual(runtime.memory, {"tony": {"project": "CHIDVI-556"}})

                joystick._open_selector()
                QTest.mouseRelease(selector, Qt.MouseButton.LeftButton, pos=target.toPoint())
                self.assertEqual(runtime._personality.active_id, "wednesday")
                runtime.request_reconnect.assert_called_once_with(keep_context=True, reason="personality: Wednesday Addams")
                runtime._configure_reference_voice()  # The reconnect loop applies the committed profile.
                factory.assert_called_once_with(runtime._personality.voice_config(), ROOT)
                self.assertEqual(factory.call_args.args[0]["reference_config"], "config/voices/wednesday.json")
                self.assertIs(runtime._reference_voice, wednesday_voice)
                self.assertEqual(runtime._resume_handle, "existing-conversation")
                self.assertEqual(runtime.memory, {"tony": {"project": "CHIDVI-556"}})
        finally:
            joystick.cancel_selection()
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_unavailable_reference_voice_reports_native_fallback_without_synthesis(self):
        runtime = Runtime("wednesday")
        factory = Mock(side_effect=ReferenceVoiceUnavailable("model files missing"))
        with patch.dict(RUNTIME_GLOBALS, load_reference_voice=factory):
            runtime._configure_reference_voice()
        self.assertIsNone(runtime._reference_voice)
        self.assertIn("Using the configured Gemini voice", runtime.ui.write_log.call_args.args[0])
        self.assertEqual(runtime._personality.active_id, "wednesday")
        self.assertEqual(runtime._resume_handle, "existing-conversation")


if __name__ == "__main__":
    unittest.main()
