import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import Qt, QByteArray, QBuffer, QIODevice, QRect
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QPushButton, QCheckBox, QScrollArea, QWidget, QDialog
from core.operator_ui import ClickOverlay, OperatorPanel, physical_to_local
from core.click_speech import ClickSpeech
from core.desktop_control import DesktopControl, ImageApproval
from ui import MainWindow


class OperatorUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_mixed_dpi_and_negative_origin_map_per_screen(self):
        self.assertEqual(physical_to_local(-1280, 720, (-2560, 0, 0, 1440), (1280, 720)), (640, 360))
        self.assertEqual(physical_to_local(2880, 540, (1920, 0, 3840, 1080), (1536, 864)), (768, 432))
        with self.assertRaises(ValueError):
            physical_to_local(3840, 0, (1920, 0, 3840, 1080), (1536, 864))

    def test_ring_does_not_accept_clicks_or_focus_and_expires(self):
        ring = ClickOverlay()
        self.assertTrue(ring.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.assertTrue(ring.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        self.assertTrue(ring.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        ring.show()
        ring._at = 0
        ring._tick()
        self.assertFalse(ring.isVisible())
        ring.close()

    def test_push_to_talk_and_handsfree_are_distinct(self):
        class Mic:
            _push_to_talk = MainWindow._push_to_talk
            _set_hands_free = MainWindow._set_hands_free
            _set_mic_active = MainWindow._set_mic_active
        mic = Mic()
        mic._hands_free = False
        mic._muted = True
        mic._hands_free_btn = QPushButton()
        mic._hands_free_btn.setCheckable(True)
        mic.hud = Mock()
        mic._log = Mock()
        mic._style_mute_btn = Mock()
        mic._apply_state = Mock()
        mic._push_to_talk(True)
        self.assertFalse(mic._muted)
        mic._push_to_talk(False)
        self.assertTrue(mic._muted)
        mic._set_hands_free(True)
        mic._push_to_talk(False)
        self.assertFalse(mic._muted)
        self.assertTrue(mic._hands_free_btn.isChecked())
        mic._set_hands_free(False)
        self.assertTrue(mic._muted)

    def test_exact_speech_errors_prevent_confirmation(self):
        speech = ClickSpeech(lambda: None, lambda: None, threading.Lock(), Mock(), Mock())
        with patch.object(speech, "_sapi") as speak:
            self.assertTrue(speech.speak("I am clicking here."))
            speak.assert_called_once_with("I am clicking here.")
        with self.assertRaises(ValueError):
            speech.speak("Clicking")
        with patch.object(speech, "_sapi", side_effect=RuntimeError("speaker missing")):
            with self.assertRaises(RuntimeError):
                speech.speak("I am clicking here.")
        speech.stopped.set()
        with self.assertRaises(RuntimeError):
            speech.speak("I am clicking here.")

    def image_bytes(self):
        pixels = QImage(80, 50, QImage.Format.Format_RGB32)
        pixels.fill(Qt.GlobalColor.blue)
        pixels.setPixelColor(0, 0, Qt.GlobalColor.white)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(pixels.save(buffer, "PNG"))
        return bytes(data)

    def panel(self, parent=None):
        with patch('memory.config_manager.get_cloud_vision_enabled', return_value=False):
            panel = OperatorPanel(parent)
        self.addCleanup(panel.close)
        foreground = patch.object(panel, '_capture_review_foreground', return_value=None)
        foreground.start()
        self.addCleanup(foreground.stop)
        panel.on_image_status = Mock()
        return panel

    def pump(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail('Qt image review did not reach the expected state')

    def start_review(self, panel):
        service = DesktopControl()
        service.cloud_vision_enabled = True
        cancelled = threading.Event()
        service.bind_feedback(None, None, cancelled.is_set)
        records, result = [], {}
        def queue(request):
            records.append(request)
            return panel.review_image(request)
        service.bind_image_review(queue)
        def run():
            try:
                result['receipt'] = service.review_image(self.image_bytes(), question='Describe this synthetic test image.', timeout=8)
            except Exception as exc:
                result['error'] = exc
        worker = threading.Thread(target=run)
        worker.start()
        def cleanup():
            cancelled.set()
            for request in records:
                if not request.done.is_set():
                    request.resolve('cancelled')
            self.app.processEvents()
            worker.join(2)
        self.addCleanup(cleanup)
        return service, cancelled, records, result, worker

    def test_worker_service_preview_requires_explicit_one_image_decision(self):
        for status, label in (('approved', 'Approve This Image Once'), ('rejected', 'Reject'), ('cancelled', 'Cancel')):
            with self.subTest(status=status):
                panel = self.panel()
                _, _, records, result, worker = self.start_review(panel)
                self.pump(lambda: records and records[0].visible.is_set())
                request, dialog = records[0], panel._image_dialog
                self.assertTrue(dialog.isVisible())
                self.assertTrue(worker.is_alive())
                self.assertFalse(request.done.is_set())
                self.assertTrue(panel.review_screenshot.isEnabled())
                self.assertIn(request.id, dialog.details.text())
                self.assertIn(request.question, dialog.details.text())
                dialog.full_size.setChecked(True)
                self.assertEqual(dialog.preview.pixmap().size(), QImage.fromData(self.image_bytes()).size())
                buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
                self.assertTrue(buttons['Cancel'].isDefault())
                buttons[label].click()
                self.pump(lambda: not worker.is_alive())
                self.assertEqual(request.status, status)
                self.assertEqual('receipt' in result, status == 'approved')
                self.assertIsNone(panel._image_request)
                self.assertIsNone(panel._image_dialog)
                self.assertFalse(panel.review_screenshot.isEnabled())
                self.assertTrue(dialog.preview.pixmap().isNull())
                panel.on_image_status.assert_called()

    def test_gui_thread_review_is_refused_without_pending_popup(self):
        panel = self.panel()
        with self.assertRaisesRegex(RuntimeError, 'worker'):
            panel.review_image(ImageApproval(self.image_bytes()))
        self.assertIsNone(panel._image_request)

    def test_preview_construction_error_reaches_service_and_visible_status(self):
        panel = self.panel()
        with patch('core.operator_ui.ImageReviewDialog', side_effect=RuntimeError('private untrusted detail')):
            _, _, records, result, worker = self.start_review(panel)
            self.pump(lambda: records and not worker.is_alive())
        self.assertEqual(records[0].status, 'preview_failed')
        self.assertIn('RuntimeError', panel.image_review_status.text())
        self.assertNotIn('private untrusted detail', panel.image_review_status.text())
        self.assertIn('error', result)
        self.assertFalse(panel.review_screenshot.isEnabled())

    def test_cancelled_request_cannot_approve_and_existing_changed_callback_survives(self):
        panel = self.panel()
        previous = Mock()
        request = ImageApproval(self.image_bytes(), changed=previous)
        worker = threading.Thread(target=lambda: panel.review_image(request))
        worker.start(); worker.join(1)
        self.pump(lambda: request.visible.is_set())
        dialog = panel._image_dialog
        request.resolve('cancelled')
        buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
        buttons['Approve This Image Once'].click()
        self.pump(lambda: panel._image_dialog is None)
        self.assertEqual(request.status, 'cancelled')
        previous.assert_called_with(request)

    def test_reopen_restores_minimized_owner_and_uses_active_repair_dialog(self):
        root = QWidget(); root.resize(700, 500); root.showMinimized()
        self.addCleanup(root.close)
        panel = self.panel(root)
        _, _, records, _, worker = self.start_review(panel)
        self.pump(lambda: records and records[0].visible.is_set())
        self.assertFalse(root.isMinimized())
        first = panel._image_dialog
        first.hide()
        panel.review_screenshot.click()
        self.app.processEvents()
        self.assertIs(panel._image_dialog, first)
        self.assertTrue(first.isVisible())
        records[0].resolve('cancelled')
        self.pump(lambda: not worker.is_alive())
        repair = QDialog(root); repair.show(); repair.activateWindow(); self.app.processEvents()
        self.addCleanup(repair.close)
        _, _, next_records, _, _ = self.start_review(panel)
        self.pump(lambda: next_records and next_records[0].visible.is_set())
        self.assertIs(panel._image_dialog.parent(), repair)

    def test_small_logical_display_keeps_all_decision_buttons_on_screen(self):
        panel = self.panel()
        area = QRect(-400, 0, 400, 350)
        with patch.object(panel, '_review_available_geometry', return_value=area):
            _, _, records, _, _ = self.start_review(panel)
            self.pump(lambda: records and records[0].visible.is_set())
            dialog = panel._image_dialog
            self.assertTrue(area.contains(dialog.frameGeometry()))
            for button in dialog.findChildren(QPushButton):
                rect = button.rect()
                rect.moveTopLeft(button.mapToGlobal(rect.topLeft()))
                self.assertTrue(area.contains(rect), (button.text(), rect))

    def test_focus_restoration_failure_does_not_revoke_explicit_approval(self):
        panel = self.panel()
        _, _, records, result, worker = self.start_review(panel)
        self.pump(lambda: records and records[0].visible.is_set())
        with patch.object(panel, '_restore_image_focus', side_effect=RuntimeError('private detail')):
            panel._image_dialog.approve.click()
        self.pump(lambda: not worker.is_alive())
        self.assertEqual(records[0].status, 'approved')
        self.assertIn('receipt', result)
        self.assertIn('focus was not restored', panel.image_review_status.text())
        self.assertNotIn('private detail', panel.image_review_status.text())

    def test_service_cancellation_closes_preview_and_drops_image_references(self):
        panel = self.panel()
        _, cancelled, records, result, worker = self.start_review(panel)
        self.pump(lambda: records and records[0].visible.is_set())
        cancelled.set()
        self.pump(lambda: not worker.is_alive() and panel._image_dialog is None)
        self.assertEqual(records[0].status, 'cancelled')
        self.assertIn('error', result)
        self.assertIsNone(panel._image_request)
        self.assertFalse(panel.review_screenshot.isEnabled())

    def test_replacement_and_expiry_keep_stale_decisions_inactive(self):
        panel = self.panel()
        first, second = ImageApproval(self.image_bytes()), ImageApproval(self.image_bytes())
        for request in (first, second):
            worker = threading.Thread(target=lambda value=request: panel.review_image(value))
            worker.start(); worker.join(1)
            self.pump(lambda: request.visible.is_set())
        self.assertEqual(first.status, 'cancelled')
        panel._decide_image(first, 'approved')
        self.assertEqual(first.status, 'cancelled')
        self.assertIs(panel._image_request, second)
        second.resolve('expired')
        self.pump(lambda: panel._image_request is None)
        self.assertIn('expired', panel.image_review_status.text())

    def test_terminal_request_arriving_after_prior_review_updates_visible_failure(self):
        panel = self.panel()
        for status in ('rejected', 'preview_failed'):
            request = ImageApproval(self.image_bytes())
            request.resolve(status, 'Preview did not become visible' if status == 'preview_failed' else '')
            worker = threading.Thread(target=lambda value=request: panel.review_image(value))
            worker.start(); worker.join(1)
            self.app.processEvents()
        self.assertIn('Preview did not become visible', panel.image_review_status.text())
        self.assertFalse(panel.review_screenshot.isEnabled())
        self.assertIsNone(panel._image_request)


if __name__ == "__main__":
    unittest.main()
