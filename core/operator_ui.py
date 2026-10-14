"""Qt-only operator indicators and a click-through ring; no inference here."""
from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, QRect, QRectF, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QDoubleSpinBox, QFormLayout,
                            QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
                            QScrollArea, QSpinBox, QVBoxLayout, QWidget)


def physical_to_local(x, y, physical_rect, logical_size):
    left, top, right, bottom = physical_rect
    width, height = logical_size
    if right <= left or bottom <= top or width <= 0 or height <= 0:
        raise ValueError("Invalid display mapping")
    if not left <= x < right or not top <= y < bottom:
        raise ValueError("Click is outside this display")
    return ((x - left) * width / (right - left), (y - top) * height / (bottom - top))


class ClickOverlay(QWidget):
    requested = pyqtSignal(object)

    def __init__(self):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowTransparentForInput
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.reduced_motion = False
        self.duration = max(0.15, min(3.0, float(os.getenv("CHIDVI_CLICK_RING_SECONDS", "0.7"))))
        self._at = 0.0
        self._point = (0, 0)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.requested.connect(self._display)

    def highlight(self, x, y):
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError("Click feedback must be requested from a worker")
        request = {"x": x, "y": y, "done": threading.Event(), "ok": False}
        self.requested.emit(request)
        if not request["done"].wait(2):
            raise RuntimeError("The click overlay did not respond; click refused")
        if not request["ok"]:
            raise RuntimeError(request.get("error", "No display mapping for click"))
        return True

    def _display(self, request):
        try:
            import win32api
            monitors = [win32api.GetMonitorInfo(m[0]) for m in win32api.EnumDisplayMonitors()]
            match = next((m for m in monitors if m["Monitor"][0] <= request["x"] < m["Monitor"][2]
                          and m["Monitor"][1] <= request["y"] < m["Monitor"][3]), None)
            if match is None:
                raise ValueError("Click outside connected displays")
            screen = next((s for s in QApplication.screens() if s.name() == match["Device"]), None)
            if screen is None:
                raise ValueError("Physical display could not be matched to Qt")
            geometry = screen.geometry()
            self._point = physical_to_local(request["x"], request["y"], match["Monitor"],
                                            (geometry.width(), geometry.height()))
            self.setGeometry(geometry)
            self._at = time.monotonic()
            self.show()
            self.repaint()
            self._timer.start(16)
            request["ok"] = True
            QTimer.singleShot(32, request["done"].set)
        except Exception as exc:
            request["error"] = str(exc)
            request["done"].set()

    def _tick(self):
        if time.monotonic() - self._at >= self.duration:
            self._timer.stop()
            self.hide()
        else:
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = 22 if self.reduced_motion else 22 + 5 * math.sin((time.monotonic() - self._at) * 16)
        x, y = self._point
        painter.setPen(QPen(QColor("#ffcf33"), 5))
        painter.drawEllipse(QRectF(x-radius, y-radius, radius*2, radius*2))
        painter.setPen(QPen(QColor("#121212"), 2))
        painter.drawEllipse(QRectF(x-radius-4, y-radius-4, radius*2+8, radius*2+8))


class ImageReviewDialog(QDialog):
    """Exact-image preview with scrolling content and always-visible decisions."""

    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review screenshot before upload")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(190, 150)
        self._pixels = QPixmap()
        if not self._pixels.loadFromData(request.data):
            raise ValueError("The preview image could not be decoded")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumSize(0, 0)
        content = QWidget()
        body = QVBoxLayout(content)
        note = QLabel("Review these exact pixels before upload. Nothing is redacted automatically. "
                      "Do not approve images containing passwords, codes, tokens, or sensitive values.")
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        body.addWidget(note)
        self.details = QLabel(f"Request: {request.id}\nTask: {request.task_id}\n{request.task_text}\n"
                              f"Question: {request.question}\nUpload destination: {request.provider}")
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.details)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(self.preview)
        self.full_size = QCheckBox("Full size (scroll to inspect)")
        self.full_size.toggled.connect(self._resize_preview)
        body.addWidget(self.full_size)
        body.addStretch()
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        buttons = QGridLayout()
        self.approve = QPushButton("Approve This Image Once")
        self.approve.setAutoDefault(False)
        self.approve.setEnabled(False)
        self.reject_image = QPushButton("Reject")
        self.reject_image.setAutoDefault(False)
        self.cancel = QPushButton("Cancel")
        self.cancel.setDefault(True)
        buttons.addWidget(self.approve, 0, 0, 1, 2)
        buttons.addWidget(self.reject_image, 1, 0)
        buttons.addWidget(self.cancel, 1, 1)
        layout.addLayout(buttons)

    def _resize_preview(self, *_):
        if self._pixels.isNull():
            return
        pixels = self._pixels if self.full_size.isChecked() else self._pixels.scaled(
            max(1, self.scroll.viewport().width() - 30), max(80, min(370, self.height() // 2)),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(pixels)
        self.preview.setFixedSize(pixels.size())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_preview()

    def clear_image(self):
        self._pixels = QPixmap()
        self.preview.setPixmap(QPixmap())
        self.approve.setEnabled(False)


class LiveSharingDisclosure(QDialog):
    """A session or startup grant; no image data is handled by this dialog."""

    def __init__(self, settings, parent=None, *, startup=False):
        super().__init__(parent)
        self.setWindowTitle("Live Screen Sharing permission")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(190, 150)
        layout = QVBoxLayout(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumSize(0, 0)
        content = QWidget()
        body = QVBoxLayout(content)
        scope = ("Allow CHIDVI to begin a new sharing session each time it starts, while Cloud Vision is enabled. "
                 "You will not be asked again at each startup. " if startup else
                 "Allow this sharing session until you stop it or its time limit expires. ")
        from core.provider_client import sharing_destinations, sharing_destination_label
        self.destinations = sharing_destinations()
        self.explanation = QLabel(
            scope + "Screenshots of the selected display will be sampled and sent to the configured providers: "
            + sharing_destination_label() + ". During this session, individual frames will not require review or approval. "
            "Anything visible may be shared, including personal information. Nothing is automatically redacted.\n\n"
            f"Display: {settings['monitor']} (0 means all displays). Sampling interval: {settings['interval']:g} seconds. "
            f"Separate limits: {settings['max_per_minute']} streamed frames per minute, plus up to "
            f"{settings['analysis_per_minute']} requested image uploads per minute for analysis or visual control; "
            f"streamed frames up to {settings['max_width']} × {settings['max_height']} pixels; "
            f"{settings['session_minutes']} minutes per session. Requested analysis/control may upload full-resolution screenshots. "
            f"Change detection: {'on' if settings['change_only'] else 'off'}. Provider usage and charges may apply.\n\n"
            "Pause stops sampling and uploads; Resume continues this session. Stop Sharing ends its permission. "
            "Turning off Cloud Vision or using the emergency stop also ends sharing. "
            "One-shot image requests outside sharing still require their separate preview and approval.")
        self.explanation.setWordWrap(True)
        self.explanation.setTextFormat(Qt.TextFormat.PlainText)
        body.addWidget(self.explanation)
        body.addStretch()
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        self.approve = QPushButton("Allow sharing at startup" if startup else "Start Live Screen Sharing")
        self.approve.setAutoDefault(False)
        self.cancel = QPushButton("Cancel")
        self.cancel.setDefault(True)
        self.approve.clicked.connect(self.accept)
        self.cancel.clicked.connect(self.reject)
        layout.addWidget(self.approve)
        layout.addWidget(self.cancel)


class OperatorPanel(QWidget):
    repair_status_changed = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    task_changed = pyqtSignal(str)
    image_requested = pyqtSignal(object)
    image_updated = pyqtSignal(object)
    cloud_vision_changed = pyqtSignal(bool)
    cloud_vision_error_changed = pyqtSignal(str)
    live_updated = pyqtSignal(object)
    live_controls_changed = pyqtSignal()
    live_startup_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_repair = None
        self.on_observation = None
        self.on_clear = None
        self.on_settings = None
        self.on_revoke = None
        self.on_cloud_vision = None
        self.on_image_status = None
        self.on_live_sharing = None
        self._live_snapshot = {"state": "OFF", "session_id": "", "last_frame_at": None}
        self._live_pending = None
        self._live_disclosure = None
        self._live_notice = ""
        self._image_request = None
        self._image_dialog = None
        self._image_return_windows = {}
        self._image_status_key = None
        self._image_status_id = None
        self._image_focus_notice = None
        from memory.config_manager import get_cloud_vision_enabled
        self._cloud_vision_enabled = get_cloud_vision_enabled()
        self._vision_privacy_dialog = None
        self.overlay = ClickOverlay()
        self.image_requested.connect(self._show_image_review, Qt.ConnectionType.QueuedConnection)
        self.image_updated.connect(self._update_image_review, Qt.ConnectionType.QueuedConnection)
        self.live_updated.connect(self._apply_live_snapshot, Qt.ConnectionType.QueuedConnection)
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)
        observation_controls = QWidget()
        observation_controls.setMaximumWidth(220)
        observation_layout = QVBoxLayout(observation_controls)
        observation_layout.setContentsMargins(0, 0, 0, 0)
        self.indicator = QLabel("Observation OFF")
        self.indicator.setMaximumWidth(220)
        self.indicator.setWordWrap(True)
        self.indicator.setToolTip("Opt-in local context only. No screenshots are saved or uploaded in this mode.")
        observation_layout.addWidget(self.indicator)
        observation_buttons = QHBoxLayout()
        observation_buttons.setSpacing(2)
        for label, mode in (("Start", "running"), ("Pause", "paused"), ("Stop", "stopped")):
            button = QPushButton(label)
            button.setMinimumWidth(0)
            button.clicked.connect(lambda checked=False, value=mode: self.on_observation and self.on_observation(value))
            observation_buttons.addWidget(button)
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self.on_clear and self.on_clear())
        clear.setMinimumWidth(0)
        observation_buttons.addWidget(clear)
        observation_layout.addLayout(observation_buttons)
        row.addWidget(observation_controls)
        self.ac_only = QCheckBox("AC only")
        self.ac_only.setChecked(True)
        self.proactive = QCheckBox("Offer help")
        self.reduced_motion = QCheckBox("Less motion")
        local_options = QVBoxLayout()
        for checkbox in (self.ac_only, self.proactive, self.reduced_motion):
            local_options.addWidget(checkbox)
            checkbox.toggled.connect(self._settings)
        row.addLayout(local_options)
        self.cloud_vision = QCheckBox("Cloud Vision")
        self.cloud_vision.setToolTip("External AI image permission. One-shot images require review; Live Screen Sharing needs separate session consent. Customize → Vision & Privacy.")
        self.cloud_vision.setChecked(self._cloud_vision_enabled)
        self.cloud_vision.toggled.connect(self.set_cloud_vision)
        self.cloud_vision_error = QLabel()
        self.cloud_vision_error.setWordWrap(True)
        self.cloud_vision_error.setMaximumWidth(190)
        self.cloud_vision_error.setStyleSheet("color: #ffb1a8;")
        self.cloud_vision_error.hide()
        self.review_screenshot = QPushButton("Review screenshot")
        self.review_screenshot.setEnabled(False)
        self.review_screenshot.clicked.connect(self._reopen_image_review)
        self.image_review_status = QLabel("No image review pending")
        self.image_review_status.setTextFormat(Qt.TextFormat.PlainText)
        self.image_review_status.setWordWrap(True)
        review_controls = QWidget()
        review_controls.setMaximumWidth(180)
        review_layout = QVBoxLayout(review_controls)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(2)
        review_layout.addWidget(self.cloud_vision)
        review_layout.addWidget(self.cloud_vision_error)
        review_layout.addWidget(self.review_screenshot)
        review_layout.addWidget(self.image_review_status)
        row.addWidget(review_controls)
        self.live_controls = QWidget()
        self.live_controls.setMaximumWidth(240)
        live_layout = QVBoxLayout(self.live_controls)
        live_layout.setContentsMargins(0, 0, 0, 0)
        live_layout.setSpacing(2)
        self.live_status = QLabel("Sharing OFF · Last frame: none")
        self.live_status.setTextFormat(Qt.TextFormat.PlainText)
        self.live_status.setWordWrap(True)
        live_layout.addWidget(self.live_status)
        self.live_start = QPushButton("Start Live Screen Sharing")
        self.live_start.clicked.connect(self._request_live_start)
        live_layout.addWidget(self.live_start)
        live_buttons = QHBoxLayout()
        live_buttons.setSpacing(2)
        self.live_pause = QPushButton("Pause")
        self.live_pause.clicked.connect(lambda: self._dispatch_live(
            "resume" if self._live_snapshot["state"] == "PAUSED" else "pause"))
        self.live_stop = QPushButton("Stop Sharing")
        self.live_stop.clicked.connect(lambda: self._dispatch_live("stop"))
        live_buttons.addWidget(self.live_pause)
        live_buttons.addWidget(self.live_stop)
        live_layout.addLayout(live_buttons)
        row.addWidget(self.live_controls)
        self.task = QLabel("No task")
        self.task.setMaximumWidth(180)
        self.task.setWordWrap(True)
        row.addWidget(self.task)
        revoke = QPushButton("Revoke phones")
        revoke.clicked.connect(lambda: self.on_revoke and self.on_revoke())
        row.addWidget(revoke)
        self.repair_button = QPushButton("Self-Repair")
        self.repair_button.clicked.connect(lambda: self.on_repair and self.on_repair())
        row.addWidget(self.repair_button)
        self.repair_indicator = QLabel("Repair OFF")
        self.repair_indicator.setMaximumWidth(150)
        self.repair_indicator.setWordWrap(True)
        row.addWidget(self.repair_indicator)
        self.repair_status_changed.connect(self.repair_indicator.setText)
        self.status_changed.connect(self.indicator.setText)
        self.task_changed.connect(self.task.setText)
        self._refresh_live_controls()

    def publish_live(self, snapshot):
        """Worker-safe: queue service metadata, never screenshot bytes or UI work."""
        keys = ("state", "session_id", "last_frame_at", "frames_captured", "frames_sent",
                "analyses", "active_requests", "error")
        safe = {key: snapshot.get(key) for key in keys if key in snapshot}
        if isinstance(snapshot.get("settings"), dict):
            safe["settings"] = dict(snapshot["settings"])
        self.live_updated.emit(safe)

    def _apply_live_snapshot(self, snapshot):
        if snapshot.get("state") not in {"OFF", "STARTING", "LIVE", "PAUSED", "STOPPING", "ERROR"}:
            return
        self._live_snapshot = dict(snapshot)
        self._live_pending = None
        self._live_notice = ""
        self._refresh_live_controls()

    def _refresh_live_controls(self, notice=None):
        if notice is not None:
            self._live_notice = notice
        state = self._live_snapshot["state"]
        at = self._live_snapshot.get("last_frame_at")
        try:
            if type(at) in (int, float):
                at = datetime.fromtimestamp(at).strftime("%H:%M:%S")
            elif isinstance(at, str):
                at = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
            else:
                at = "none"
        except (ValueError, OverflowError, OSError):
            at = "unknown"
        pending = f" · {self._live_pending} requested" if self._live_pending else ""
        error = str(self._live_snapshot.get("error") or "")[:500]
        detail = self._live_notice or error
        self.live_status.setText(f"Sharing {state}{pending}\nLast frame: {at}"
                                + ("\n" + detail[:110] if detail else ""))
        self.live_status.setToolTip(detail or (
            f"{self._live_snapshot.get('frames_sent', 0)} frames sent; "
            f"{self._live_snapshot.get('analyses', 0)} analyses. Settings: Customize → Vision & Privacy."))
        active = state in {"STARTING", "LIVE", "PAUSED", "STOPPING"}
        self.live_start.setEnabled(not active and not self._live_pending and self._cloud_vision_enabled)
        self.live_pause.setText("Resume" if state == "PAUSED" else "Pause")
        self.live_pause.setEnabled(state in {"LIVE", "PAUSED"} and not self._live_pending)
        self.live_stop.setEnabled((active or self._live_pending == "start") and state != "STOPPING")
        self.live_controls_changed.emit()

    def _fit_live_dialog(self, dialog, width=550, height=510):
        area = self._review_available_geometry(dialog.parentWidget() or self.window()).adjusted(8, 8, -8, -8)
        dialog.setMaximumSize(max(190, area.width()), max(150, area.height()))
        dialog.resize(min(width, max(190, area.width())), min(height, max(150, area.height())))
        dialog.showNormal()
        frame = dialog.frameGeometry()
        dialog.move(area.x() + (area.width() - frame.width()) // 2,
                    area.y() + (area.height() - frame.height()) // 2)
        dialog.raise_()
        dialog.activateWindow()
        if dialog.windowHandle():
            dialog.windowHandle().requestActivate()

    def _request_live_start(self):
        if not self._cloud_vision_enabled:
            self._refresh_live_controls("Enable Cloud Vision in Vision & Privacy before starting sharing.")
            return
        if self._live_snapshot["state"] not in {"OFF", "ERROR"} or self._live_pending:
            return
        from memory.config_manager import get_live_sharing_settings
        self._show_live_disclosure(get_live_sharing_settings(), startup=False)

    def _show_live_disclosure(self, settings, *, startup=False):
        if self._live_disclosure is not None:
            self._live_disclosure.raise_()
            self._live_disclosure.activateWindow()
            return
        dialog = LiveSharingDisclosure(settings, self._review_owner(), startup=startup)
        self._live_disclosure = dialog
        def finished(result):
            self._live_disclosure = None
            dialog.deleteLater()
            if result != QDialog.DialogCode.Accepted or not self._cloud_vision_enabled:
                return
            from core.provider_client import sharing_destinations
            if dialog.destinations != sharing_destinations():
                self._refresh_live_controls("Provider settings changed. Start again to review the new destinations.")
                return
            if startup:
                self._save_live_startup(True)
            elif self._live_snapshot["state"] in {"OFF", "ERROR"} and not self._live_pending:
                self._dispatch_live("start", settings)
        dialog.finished.connect(finished)
        self._fit_live_dialog(dialog)
        dialog.cancel.setFocus()

    def _dispatch_live(self, action, settings=None):
        from memory.config_manager import get_live_sharing_settings, validate_live_sharing_settings
        if action in {"start", "resume"} and not self._cloud_vision_enabled:
            self._refresh_live_controls("Cloud Vision is disabled; sharing was not started.")
            return
        try:
            values = validate_live_sharing_settings(settings if settings is not None else
                self._live_snapshot.get("settings", get_live_sharing_settings()))
            if not self.on_live_sharing:
                raise RuntimeError("Sharing controller is unavailable")
            self._live_pending = action
            self._refresh_live_controls("")
            if self.on_live_sharing(action, values) is False:
                raise RuntimeError("Sharing request was refused")
        except Exception:
            self._live_pending = None
            self._refresh_live_controls("Sharing request could not be dispatched. Check the activity log and connection.")

    def _save_live_startup(self, enabled):
        from memory.config_manager import get_live_sharing_startup, save_live_sharing_startup
        try:
            save_live_sharing_startup(enabled)
        except Exception:
            self.live_startup_changed.emit(get_live_sharing_startup())
            if self._vision_privacy_dialog:
                self._vision_privacy_dialog._live_error("Startup permission could not be saved. Previous setting kept.")
            return False
        self.live_startup_changed.emit(enabled)
        if self._vision_privacy_dialog:
            self._vision_privacy_dialog._live_error("")
        return True

    def set_cloud_vision_state(self, enabled):
        """Synchronize controls from runtime/startup; never persist or dispatch."""
        if type(enabled) is not bool:
            raise TypeError("Cloud Vision must be a boolean")
        self._cloud_vision_enabled = enabled
        was_blocked = self.cloud_vision.blockSignals(True)
        self.cloud_vision.setChecked(enabled)
        self.cloud_vision.blockSignals(was_blocked)
        self.cloud_vision_changed.emit(enabled)
        self._refresh_live_controls()
        if not enabled and self._live_disclosure is not None:
            self._live_disclosure.reject()

    def _cloud_error(self, message):
        self.cloud_vision_error.setText(message)
        self.cloud_vision_error.setVisible(bool(message))
        self.cloud_vision_error_changed.emit(message)

    def set_cloud_vision(self, enabled):
        """One UI save path for the toolbar and Customize privacy settings."""
        from memory.config_manager import save_cloud_vision_enabled
        previous = self._cloud_vision_enabled
        try:
            save_cloud_vision_enabled(enabled)
        except Exception:
            self.set_cloud_vision_state(previous)
            self._cloud_error("Cloud Vision could not be saved. Previous setting kept. Check configuration permissions and format.")
            return False
        self.set_cloud_vision_state(enabled)
        self._cloud_error("")
        if self.on_cloud_vision:
            try:
                self.on_cloud_vision(enabled)
            except Exception:
                self._cloud_error("Cloud Vision was saved, but the running app could not update. Restart the app to apply it.")
                return False
        return True

    def show_vision_privacy(self):
        if self._vision_privacy_dialog is None:
            self._vision_privacy_dialog = VisionPrivacyDialog(self, self.window())
        dialog = self._vision_privacy_dialog
        self._fit_live_dialog(dialog, 560, 690)
        return dialog

    def _settings(self):
        self.overlay.reduced_motion = self.reduced_motion.isChecked()
        if self.on_settings:
            self.on_settings(self.ac_only.isChecked(), self.proactive.isChecked())

    def review_image(self, request):
        """Queue one request; the desktop service owns waiting and authorization."""
        if QThread.currentThread() == self.thread():
            raise RuntimeError("Image review must be requested from a worker")
        previous = request.changed
        def changed(current):
            try:
                if previous:
                    previous(current)
            finally:
                self.image_updated.emit(current)
        request.changed = changed
        self._image_return_windows[request.id] = self._capture_review_foreground()
        self.image_requested.emit(request)
        return True

    @staticmethod
    def _capture_review_foreground():
        try:
            from core.desktop_control import foreground
            return foreground().get("window_id")
        except Exception:
            return None

    def _review_owner(self):
        owner = self.window()
        active = QApplication.activeWindow()
        if owner.isMinimized() or not owner.isVisible():
            owner.showNormal()
        if active and active.isVisible() and not active.isMinimized():
            ancestor = active
            while ancestor is not None:
                if ancestor is owner:
                    return active
                ancestor = ancestor.parentWidget()
        return owner

    @staticmethod
    def _review_available_geometry(owner):
        screen = owner.screen() or QApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 800, 600)

    def _present_image_review(self, request, dialog):
        if request.done.is_set():
            self._update_image_review(request)
            return
        owner = dialog.parentWidget()
        if owner and (owner.isMinimized() or not owner.isVisible()):
            owner.showNormal()
        area = self._review_available_geometry(owner or self.window()).adjusted(8, 8, -8, -8)
        width, height = max(190, area.width() - 8), max(150, area.height() - 8)
        dialog.setMaximumSize(width, height)
        dialog.resize(min(740, width), min(600, height))
        dialog.showNormal()
        frame = dialog.frameGeometry()
        dialog.move(area.x() + (area.width() - frame.width()) // 2,
                    area.y() + (area.height() - frame.height()) // 2)
        dialog.raise_()
        dialog.activateWindow()
        if dialog.windowHandle():
            dialog.windowHandle().requestActivate()
        dialog.cancel.setFocus()
        QTimer.singleShot(0, lambda: self._mark_image_visible(request, dialog))

    def _mark_image_visible(self, request, dialog):
        if request is not self._image_request or request.done.is_set():
            return
        try:
            if not dialog.isVisible() or dialog.isMinimized() or dialog.preview.pixmap().isNull():
                raise RuntimeError("Preview did not become visible")
            request.mark_visible()
            dialog.approve.setEnabled(request.visible.is_set() and not request.done.is_set())
            self._update_image_review(request)
        except Exception as exc:
            self._preview_failed(request, exc)

    def _preview_failed(self, request, exc):
        request.resolve("preview_failed", f"Screenshot preview failed ({type(exc).__name__}); no image was approved.")
        self._update_image_review(request)

    def _show_image_review(self, request):
        if request.done.is_set():
            if self._image_request is None:
                self._image_status_id = request.id
            self._update_image_review(request)
            return
        try:
            if self._image_request is not None and self._image_request is not request:
                old = self._image_request
                old.resolve("cancelled", "A newer screenshot review replaced this request.")
                self._update_image_review(old)
            self._image_request = request
            self._image_status_id = request.id
            self._image_focus_notice = None
            self.review_screenshot.setEnabled(True)
            self._update_image_review(request)
            dialog = ImageReviewDialog(request, self._review_owner())
            self._image_dialog = dialog
            dialog.approve.clicked.connect(lambda: self._decide_image(request, "approved"))
            dialog.reject_image.clicked.connect(lambda: self._decide_image(request, "rejected"))
            dialog.cancel.clicked.connect(lambda: self._decide_image(request, "cancelled"))
            dialog.finished.connect(lambda _: self._decide_image(request, "cancelled"))
            self._present_image_review(request, dialog)
        except Exception as exc:
            self._preview_failed(request, exc)

    def _reopen_image_review(self):
        request, dialog = self._image_request, self._image_dialog
        if request is None or request.done.is_set():
            return
        try:
            if dialog is None:
                self._show_image_review(request)
            else:
                self._present_image_review(request, dialog)
        except Exception as exc:
            self._preview_failed(request, exc)

    def _restore_image_focus(self, request, dialog):
        """Best effort only; foreground restoration never grants/revokes consent."""
        target = self._image_return_windows.get(request.id)
        if not target or dialog is None:
            return
        import win32gui
        import win32process
        owned = win32gui.GetForegroundWindow() == int(dialog.winId())
        dialog.hide()
        current = win32gui.GetForegroundWindow()
        if owned and win32process.GetWindowThreadProcessId(current)[1] == os.getpid():
            win32gui.SetForegroundWindow(target)

    def _decide_image(self, request, status):
        if request is not self._image_request or request.done.is_set():
            self._update_image_review(request)
            return
        if status == "approved" and not request.visible.is_set():
            return
        try:
            self._restore_image_focus(request, self._image_dialog)
        except Exception as exc:
            self._image_focus_notice = (request.id, f"Original application focus was not restored ({type(exc).__name__}); the image decision is unchanged.")
        request.resolve(status)
        self._update_image_review(request)

    def _update_image_review(self, request):
        snapshot = request.snapshot()
        if self._image_focus_notice and self._image_focus_notice[0] == request.id:
            snapshot["focus_notice"] = self._image_focus_notice[1]
        key = (request.id, snapshot.get("status"), snapshot.get("visible"), snapshot.get("error"))
        if key != self._image_status_key:
            self._image_status_key = key
            if self.on_image_status:
                try:
                    self.on_image_status(snapshot)
                except Exception as exc:
                    self.image_review_status.setText(f"Image review activity update failed ({type(exc).__name__}).")
        if self._image_status_id is None:
            self._image_status_id = request.id
        if self._image_status_id != request.id:
            self._image_return_windows.pop(request.id, None)
            return
        status = snapshot.get("status", "pending")
        descriptions = {"approved": "Image approved once; awaiting upload", "rejected": "Image rejected; no upload",
                        "cancelled": "Image review cancelled", "expired": "Image review expired; request a new capture",
                        "preview_failed": "Screenshot preview failed", "consumed": "Approved image uploading",
                        "completed": "Image analysis completed", "failed": "Image analysis failed"}
        message = ("Review screenshot: waiting for your decision" if request.visible.is_set() else "Preparing screenshot review") if status == "pending" else descriptions.get(status, status)
        if snapshot.get("error"):
            message += ": " + str(snapshot["error"])
        if snapshot.get("focus_notice"):
            message += ". " + snapshot["focus_notice"]
        self.image_review_status.setText(message)
        if request.done.is_set():
            self._image_return_windows.pop(request.id, None)
            self.review_screenshot.setEnabled(False)
            if self._image_request is request:
                self._image_request = None
                dialog, self._image_dialog = self._image_dialog, None
                if dialog is not None:
                    dialog.clear_image()
                    dialog.blockSignals(True)
                    dialog.close()
                    dialog.deleteLater()


class VisionPrivacyDialog(QDialog):
    """One-shot and session consent are distinct, with explicit saved limits."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.setWindowTitle("Vision & Privacy")
        self.setMinimumSize(190, 150)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumSize(0, 0)
        content = QWidget()
        body = QVBoxLayout(content)
        self.cloud_vision = QCheckBox("Cloud Vision")
        self.cloud_vision.setObjectName("cloud_vision_enabled")
        self.cloud_vision.setChecked(panel.cloud_vision.isChecked())
        self.cloud_vision.toggled.connect(panel.set_cloud_vision)
        body.addWidget(self.cloud_vision)
        self.explanation = QLabel("Allow requested screenshots and camera images to be sent to your configured external AI provider. "
                                  "This is off by default and saved immediately. One-shot requests require an exact-image review before upload. "
                                  "Live Screen Sharing instead requires explicit session permission, then shares sampled screenshots without per-image prompts. "
                                  "Nothing is redacted automatically. Local screenshot capture does not require Cloud Vision.")
        self.explanation.setWordWrap(True)
        body.addWidget(self.explanation)
        self.error = QLabel(panel.cloud_vision_error.text())
        self.error.setWordWrap(True)
        error_color = "#b3261e" if self.palette().window().color().lightness() > 128 else "#ffb1a8"
        self.error.setStyleSheet(f"color: {error_color};")
        self.error.setVisible(bool(self.error.text()))
        body.addWidget(self.error)
        sharing = QGroupBox("Live Screen Sharing")
        sharing_body = QVBoxLayout(sharing)
        self.live_state = QLabel()
        self.live_state.setTextFormat(Qt.TextFormat.PlainText)
        self.live_state.setWordWrap(True)
        sharing_body.addWidget(self.live_state)
        self.live_start = QPushButton("Start Live Screen Sharing")
        self.live_start.clicked.connect(panel._request_live_start)
        sharing_body.addWidget(self.live_start)
        sharing_buttons = QHBoxLayout()
        self.live_pause = QPushButton("Pause")
        self.live_pause.clicked.connect(panel.live_pause.click)
        self.live_stop = QPushButton("Stop Sharing")
        self.live_stop.clicked.connect(panel.live_stop.click)
        sharing_buttons.addWidget(self.live_pause)
        sharing_buttons.addWidget(self.live_stop)
        sharing_body.addLayout(sharing_buttons)
        from memory.config_manager import get_live_sharing_settings, get_live_sharing_startup
        settings = get_live_sharing_settings()
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.live_interval = QDoubleSpinBox()
        self.live_interval.setRange(1, 30)
        self.live_interval.setSingleStep(.5)
        self.live_interval.setDecimals(1)
        self.live_interval.setSuffix(" s")
        self.live_interval.setValue(settings["interval"])
        form.addRow("Sample interval", self.live_interval)
        self._live_fields = {"interval": self.live_interval}
        for key, label, low, high in (("max_per_minute", "Stream frames per minute", 1, 60),
                                     ("analysis_per_minute", "Analysis/control uploads per minute", 1, 20),
                                     ("max_width", "Maximum streamed width (px)", 320, 1920),
                                     ("max_height", "Maximum streamed height (px)", 240, 1080),
                                     ("monitor", "Display (0 = all)", 0, 64),
                                     ("session_minutes", "Session limit (min)", 1, 120)):
            spin = QSpinBox()
            spin.setRange(low, high)
            spin.setValue(settings[key])
            self._live_fields[key] = spin
            form.addRow(label, spin)
        sharing_body.addLayout(form)
        self.live_change_only = QCheckBox("Share only when the screen changes")
        self.live_change_only.setChecked(settings["change_only"])
        sharing_body.addWidget(self.live_change_only)
        settings_note = QLabel("Saved limits apply to the next session. Provider and transport limits may be lower. "
                              "Video streaming and requested analysis/control uploads use separate rate limits; their usage adds together. "
                              "Network usage and provider charges may apply.")
        settings_note.setWordWrap(True)
        sharing_body.addWidget(settings_note)
        self.live_save = QPushButton("Save sharing limits")
        self.live_save.clicked.connect(self._save_live_settings)
        sharing_body.addWidget(self.live_save)
        self.live_startup = QCheckBox("Start Live Sharing with CHIDVI")
        self.live_startup.setChecked(get_live_sharing_startup())
        self.live_startup.toggled.connect(self._change_live_startup)
        sharing_body.addWidget(self.live_startup)
        startup_note = QLabel("Optional and off by default. Requires Cloud Vision. Enabling this grants future startup sessions "
                              "without another dialog, subject to your saved time and rate limits. Loading settings never starts sharing.")
        startup_note.setWordWrap(True)
        sharing_body.addWidget(startup_note)
        self.live_error = QLabel()
        self.live_error.setTextFormat(Qt.TextFormat.PlainText)
        self.live_error.setWordWrap(True)
        self.live_error.setStyleSheet(f"color: {error_color};")
        self.live_error.hide()
        sharing_body.addWidget(self.live_error)
        body.addWidget(sharing)
        body.addStretch()
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        layout.addWidget(close)
        panel.cloud_vision_changed.connect(self._sync)
        panel.cloud_vision_error_changed.connect(self._error)
        panel.live_controls_changed.connect(self._sync_live)
        panel.live_startup_changed.connect(self._sync_startup)
        self._sync_live()

    def _sync_live(self):
        panel = self.panel
        self.live_state.setText(panel.live_status.text())
        self.live_state.setToolTip(panel.live_status.toolTip())
        self.live_start.setEnabled(panel.live_start.isEnabled())
        self.live_pause.setText(panel.live_pause.text())
        self.live_pause.setEnabled(panel.live_pause.isEnabled())
        self.live_stop.setEnabled(panel.live_stop.isEnabled())

    def _save_live_settings(self):
        from memory.config_manager import save_live_sharing_settings
        settings = {key: widget.value() for key, widget in self._live_fields.items()}
        settings["change_only"] = self.live_change_only.isChecked()
        try:
            save_live_sharing_settings(settings)
        except Exception:
            self._live_error("Sharing limits could not be saved. Previous settings kept.")
            return
        self._live_error("")
        self.live_save.setToolTip("Saved. These limits apply to the next session.")

    def _sync_startup(self, enabled):
        was_blocked = self.live_startup.blockSignals(True)
        self.live_startup.setChecked(enabled)
        self.live_startup.blockSignals(was_blocked)

    def _change_live_startup(self, enabled):
        from memory.config_manager import get_live_sharing_startup, get_live_sharing_settings
        self._sync_startup(get_live_sharing_startup())
        if enabled:
            if not self.panel._cloud_vision_enabled:
                self._live_error("Enable Cloud Vision before granting sharing at startup.")
                return
            self.panel._show_live_disclosure(get_live_sharing_settings(), startup=True)
        else:
            self.panel._save_live_startup(False)

    def _live_error(self, message):
        self.live_error.setText(message)
        self.live_error.setVisible(bool(message))

    def _sync(self, enabled):
        was_blocked = self.cloud_vision.blockSignals(True)
        self.cloud_vision.setChecked(enabled)
        self.cloud_vision.blockSignals(was_blocked)

    def _error(self, message):
        self.error.setText(message)
        self.error.setVisible(bool(message))
