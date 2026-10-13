"""Selector behavior without starting audio, networking, or the full command center."""

import copy
import math
import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QLineEdit, QMainWindow, QWidget

from Personality.manager import PersonalityManager
from personality_joystick import PersonalityJoystick
from ui import MainWindow


class SelectorWindow(QMainWindow):
    _on_escape = MainWindow._on_escape
    _do_interrupt = MainWindow._do_interrupt
    _on_personality_selected = MainWindow._on_personality_selected

    def __init__(self):
        super().__init__()
        self.setCentralWidget(QWidget(self))
        self.resize(1100, 800)
        self.editor = QLineEdit(self.centralWidget())
        self.editor.setGeometry(10, 10, 300, 30)
        self.manager = PersonalityManager("tony")
        self.history = [{"role": "user", "content": "Keep this conversation"}]
        self.memory = {"tony": {"project": "CHIDVI-556"}}
        self.on_interrupt = Mock()
        self.on_personality_change = Mock(side_effect=self.switch_personality)
        self._personality_joystick = PersonalityJoystick(self.centralWidget(), "tony")
        self._personality_joystick.selected.connect(self._on_personality_selected)
        self._escape_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._escape_shortcut.setAutoRepeat(False)
        self._escape_shortcut.activated.connect(self._on_escape)

    def switch_personality(self, personality_id):
        self.manager.set_personality(personality_id)
        return self.manager.active_id


class PersonalityJoystickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = SelectorWindow()
        self.window.show()
        self.window.activateWindow()
        self.joystick = self.window._personality_joystick
        self.joystick.sync_geometry(self.window.centralWidget().size())
        self.selector = self.joystick._selector
        self.selected = QSignalSpy(self.joystick.selected)
        self.closed = QSignalSpy(self.selector.closed)
        self.window.editor.setFocus()
        self.app.processEvents()
        self.history = copy.deepcopy(self.window.history)
        self.memory = copy.deepcopy(self.window.memory)

    def tearDown(self):
        self.joystick.cancel_selection()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def open_selector(self):
        self.joystick._open_selector()
        self.app.processEvents()
        self.assertTrue(self.selector.isVisible())

    def preview_other(self):
        target = next(key for key in self.selector._ids if key != "tony")
        position = self.selector._node_position(self.selector._ids.index(target))
        self.selector._pick_from_position(position)
        self.assertEqual(self.selector._highlighted, target)
        return target, position

    def assert_not_switched(self):
        self.assertEqual(self.window.manager.active_id, "tony")
        self.assertEqual(self.joystick._current_id, "tony")
        self.assertEqual(self.selector._current_id, "tony")
        self.assertEqual(len(self.selected), 0)
        self.window.on_personality_change.assert_not_called()
        self.assertEqual(self.window.history, self.history)
        self.assertEqual(self.window.memory, self.memory)

    def test_open_and_preview_preserve_active_personality(self):
        self.open_selector()
        self.assertTrue(self.selector.hasFocus())
        self.assert_not_switched()
        self.preview_other()
        self.assert_not_switched()

    def test_escape_cancels_from_selector_handle_or_editor_focus(self):
        for focus in (self.selector, self.joystick._handle, self.window.editor):
            with self.subTest(focus=type(focus).__name__):
                self.window.editor.setFocus()
                self.open_selector()
                self.preview_other()
                focus.setFocus()
                self.app.processEvents()
                QTest.keyClick(focus, Qt.Key.Key_Escape)
                self.app.processEvents()
                self.assertFalse(self.selector.isVisible())
                self.assertFalse(self.selector._dragging)
                self.assertEqual(self.selector._highlighted, "tony")
                self.assertEqual(self.selector._pointer, self.selector._center)
                self.assertIsNot(QWidget.mouseGrabber(), self.selector)
                self.assertFalse(self.joystick._handle.isDown())
                self.assertTrue(self.window.editor.hasFocus())
                self.assert_not_switched()
                self.window.on_interrupt.assert_not_called()
        self.assertEqual(len(self.closed), 3)

    def test_cancel_is_idempotent_and_late_release_does_not_select(self):
        self.open_selector()
        _, position = self.preview_other()
        self.assertTrue(self.joystick.cancel_selection())
        self.assertFalse(self.joystick.cancel_selection())
        QTest.mouseRelease(self.selector, Qt.MouseButton.LeftButton, pos=position.toPoint())
        self.assert_not_switched()
        self.assertEqual(len(self.closed), 1)
        self.open_selector()
        self.assertEqual(self.selector._highlighted, "tony")
        self.assert_not_switched()

    def test_closed_escape_retains_existing_interrupt_action(self):
        self.assertFalse(self.joystick.cancel_selection())
        QTest.keyClick(self.window.editor, Qt.Key.Key_Escape)
        self.window.on_interrupt.assert_called_once_with()
        self.assertFalse(self.selector.isVisible())
        self.assertEqual(len(self.closed), 0)
        self.assert_not_switched()

    def test_escape_during_mouse_hold_clears_handle_and_ignores_release(self):
        QTest.mousePress(self.joystick._handle, Qt.MouseButton.LeftButton)
        self.assertTrue(self.selector.isVisible())
        self.assertTrue(self.selector._dragging)
        target = next(key for key in self.selector._ids if key != "tony")
        position = self.selector._node_position(self.selector._ids.index(target))
        QTest.mouseMove(self.selector, position.toPoint())
        self.app.processEvents()
        self.assertEqual(self.selector._highlighted, target)
        self.assert_not_switched()
        QTest.keyClick(self.selector, Qt.Key.Key_Escape)
        self.assertFalse(self.selector.isVisible())
        self.assertFalse(self.joystick._handle.isDown())
        QTest.mouseRelease(self.joystick._handle, Qt.MouseButton.LeftButton)
        self.assert_not_switched()
        self.window.on_interrupt.assert_not_called()
        self.assertEqual(len(self.closed), 1)

    def test_empty_roster_closes_pending_selection_without_committing(self):
        self.open_selector()
        _, position = self.preview_other()
        self.selector.set_profiles({})
        self.assertFalse(self.selector.isVisible())
        self.assertFalse(self.selector._dragging)
        QTest.keyClick(self.selector, Qt.Key.Key_Right)
        QTest.mouseRelease(self.selector, Qt.MouseButton.LeftButton, pos=position.toPoint())
        self.assertEqual(len(self.closed), 1)
        self.assertEqual(len(self.selected), 0)
        self.assertEqual(self.window.manager.active_id, "tony")
        self.assertEqual(self.joystick._current_id, "tony")
        self.window.on_personality_change.assert_not_called()
        self.assertEqual(self.window.history, self.history)
        self.assertEqual(self.window.memory, self.memory)

    def test_only_left_release_commits_and_synchronizes_selection(self):
        QTest.mousePress(self.joystick._handle, Qt.MouseButton.LeftButton)
        self.assertTrue(self.selector.isVisible())
        self.assertTrue(self.selector._dragging)
        target = next(key for key in self.selector._ids if key != "tony")
        position = self.selector._node_position(self.selector._ids.index(target))
        QTest.mouseMove(self.selector, position.toPoint())
        self.app.processEvents()
        self.assertEqual(self.selector._highlighted, target)
        QTest.mouseRelease(self.selector, Qt.MouseButton.RightButton, pos=position.toPoint())
        self.assertTrue(self.selector.isVisible())
        self.assert_not_switched()
        QTest.mouseRelease(self.selector, Qt.MouseButton.LeftButton, pos=position.toPoint())
        self.assertFalse(self.selector.isVisible())
        self.assertEqual(list(self.selected), [[target]])
        self.window.on_personality_change.assert_called_once_with(target)
        self.assertEqual(self.window.manager.active_id, target)
        self.assertEqual(self.joystick._current_id, target)
        self.assertEqual(self.selector._current_id, target)
        self.assertFalse(self.joystick._handle.isDown())
        self.assertEqual(len(self.closed), 1)

    def test_handle_keyboard_open_arrows_preview_and_enter_commits(self):
        for key in (Qt.Key.Key_Space, Qt.Key.Key_Return):
            with self.subTest(open_key=key):
                self.joystick._handle.setFocus()
                QTest.keyClick(self.joystick._handle, key)
                self.app.processEvents()
                self.assertTrue(self.selector.isVisible())
                self.assertTrue(self.selector.hasFocus())
                self.assert_not_switched()
                self.joystick.cancel_selection()
        self.open_selector()
        QTest.keyClick(self.selector, Qt.Key.Key_Right)
        target = self.selector._highlighted
        self.assertNotEqual(target, "tony")
        self.assert_not_switched()
        QTest.keyClick(self.selector, Qt.Key.Key_Return)
        self.assertEqual(list(self.selected), [[target]])
        self.assertEqual(self.window.manager.active_id, target)
        self.assertFalse(self.selector.isVisible())

    def test_goku_is_discovered_can_be_previewed_canceled_and_selected(self):
        self.assertIn("goku", self.joystick._profiles)
        self.assertIn("naruto", self.joystick._profiles)
        self.open_selector()
        position = self.selector._node_position(self.selector._ids.index("goku"))
        QTest.mouseMove(self.selector, position.toPoint())
        self.assertEqual(self.selector._highlighted, "goku")
        self.assert_not_switched()
        QTest.keyClick(self.selector, Qt.Key.Key_Escape)
        self.assert_not_switched()
        self.open_selector()
        QTest.mouseRelease(self.selector, Qt.MouseButton.LeftButton, pos=position.toPoint())
        self.assertEqual(list(self.selected), [["goku"]])
        self.assertEqual(self.window.manager.active_id, "goku")
        self.assertEqual(self.window.manager.current()["name"], "Goku")
        self.assertIn("Persona ID: goku", self.window.manager.system_prompt())
        self.assertFalse(self.joystick._handle.icon().isNull())

    def test_joystick_nodes_dead_zone_and_pointer_clamp(self):
        self.open_selector()
        for index, personality_id in enumerate(self.selector._ids):
            with self.subTest(personality_id=personality_id):
                position = self.selector._node_position(index)
                offset = position - self.selector._center
                self.assertAlmostEqual(math.hypot(offset.x(), offset.y()), 205.0)
                self.selector._pick_from_position(position)
                self.assertEqual(self.selector._highlighted, personality_id)
                pointer = self.selector._pointer - self.selector._center
                self.assertAlmostEqual(math.hypot(pointer.x(), pointer.y()), 139.4)
        top = self.selector._node_position(0)
        self.assertAlmostEqual(top.x(), self.selector._center.x())
        self.assertLess(top.y(), self.selector._center.y())
        self.assertGreater(self.selector._node_position(1).x(), top.x())
        highlighted = self.selector._highlighted
        self.selector._pick_from_position(self.selector._center + QPointF(0, 42))
        self.assertEqual(self.selector._highlighted, highlighted)
        self.selector._pick_from_position(self.selector._center + QPointF(0, -43))
        self.assertEqual(self.selector._highlighted, self.selector._ids[0])
        self.assert_not_switched()


if __name__ == "__main__":
    unittest.main()
