"""Visible, guarded repair controls. Runtime work is delegated through callbacks."""
from __future__ import annotations

import json
import re

from PyQt6.QtCore import QCoreApplication, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QGridLayout, QGroupBox,
                            QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                            QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from core.repair_hotkey import NativeRepairHotkey


def _safe_text(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=lambda _: "[unavailable]")
    try:
        from core.repair_permissions import redact
        return redact(value)[:32000]
    except Exception:
        return "[Output hidden: privacy filter unavailable]"


def _history_entry(record, identity):
    """Put recovery evidence ahead of verbose context in the bounded preview."""
    status = record.get("status") or record.get("state") or "recorded"
    error = record.get("error") or record.get("summary") or "Repair record"
    label = f"{_safe_text(status)[:40]} · {' '.join(_safe_text(error).split())[:140]} · {identity}"
    attempts = record.get("attempts")
    latest = attempts[-1] if isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict) else {}
    details = {"id": identity, "status": status, "error": error,
               "last_outcome": record.get("last_outcome"),
               "latest_attempt": {key: latest[key] for key in
                   ("id", "status", "verified", "files", "diff_path", "error", "health") if key in latest}}
    if isinstance(latest.get("commands"), list):
        details["latest_attempt"]["commands"] = [
            {key: _safe_text(value)[:1500] if key in {"stdout", "stderr", "error"} else value
             for key, value in command.items()}
            for command in latest["commands"][-24:] if isinstance(command, dict)]
    for key in ("diagnosis", "source", "task_id", "created_at", "context", "traceback"):
        if key in record:
            details[key] = record[key]
    return label, _safe_text(json.dumps(details, ensure_ascii=False, indent=2, default=lambda _: "[unavailable]"))


class RepairPanel(QDialog):
    event_received = pyqtSignal(object)
    global_hotkey_requested = pyqtSignal(object)
    live_sharing_requested = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_mode = self.on_command = self.on_stop = self.on_observation = None
        self.on_permissions = self.on_rollback = self.on_restart = self.on_shortcut_changed = None
        self._native_hotkey = None
        self._live_sharing_active = False
        self._hotkey_sequence = "Ctrl+Alt+Escape"
        self.setWindowTitle("Self-Repair")
        self.resize(900, 780)
        self.setMinimumSize(660, 600)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.mode = QCheckBox("Self-Repair Mode")
        header.addWidget(self.mode)
        header.addStretch()
        self.stop_button = QPushButton("STOP REPAIR")
        self.stop_button.setStyleSheet("QPushButton { background: #a12424; color: white; padding: 7px 16px; font-weight: bold; }")
        header.addWidget(self.stop_button)
        layout.addLayout(header)
        self.monitoring = self._label("Monitoring OFF — no repair screenshots")
        layout.addWidget(self.monitoring)
        options = QHBoxLayout()
        self.observation = QCheckBox("Observe this task")
        self.interval = QSpinBox()
        self.interval.setRange(2, 60)
        self.interval.setValue(5)
        self.interval.setSuffix(" s")
        self.monitor = QSpinBox()
        self.monitor.setRange(0, 64)
        self.monitor.setValue(1)
        self.monitor.setToolTip("Physical display index; 0 captures all displays. Unavailable displays are refused.")
        self.retain = QCheckBox("Keep latest screenshot locally")
        self.retain.setToolTip("Off by default. When enabled, raw pixels may contain private information. Saves only latest.png under logs/repair/task-ID.")
        for widget in (self.observation, self._label("Every"), self.interval, self._label("Display"), self.monitor, self.retain):
            options.addWidget(widget)
        layout.addLayout(options)
        layout.addWidget(self._label("Screenshots stay in memory unless local retention is selected. Retained raw images can contain private information. "
                                    "Background repair observation never uploads images. Explicit analysis uses an authorized Live Screen Sharing session, "
                                    "or a separate one-shot image approval when sharing is off."))
        state = QHBoxLayout()
        self.task = self._label("No repair task")
        self.phase = self._label("Idle")
        state.addWidget(self.task, 1)
        state.addWidget(self.phase, 1)
        layout.addLayout(state)
        self.status = self._label("Enable Self-Repair Mode to run authorized repair actions.")
        layout.addWidget(self.status)

        commands = QGridLayout()
        self.commands = {}
        for index, (action, label) in enumerate((("diagnostics", "Run diagnostics"), ("refresh_snapshot", "Refresh snapshot"),
                ("analyze", "Analyze latest image"), ("demo", "Run isolated repair demo"),
                ("inspect_failure", "Inspect failure"), ("open_vscode", "Open VS Code"))):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, value=action: self._command(value))
            commands.addWidget(button, index // 3, index % 3)
            self.commands[action] = button
        self.restart_button = QPushButton("Restart app")
        self.restart_button.clicked.connect(lambda: self._invoke(self.on_restart) if self.mode.isChecked() else None)
        commands.addWidget(self.restart_button, 2, 0, 1, 3)
        layout.addLayout(commands)

        self.tabs = tabs = QTabWidget()
        self.diagnosis = self._output(400)
        self.terminal = self._output(1000)
        self.files = self._output(200)
        self.diff = self._output(1000)
        self.tests = self._output(500)
        self.history_details = self._output(1000)
        changes = QWidget()
        changes_layout = QVBoxLayout(changes)
        self.files.setMaximumHeight(90)
        changes_layout.addWidget(self.files)
        changes_layout.addWidget(self.diff, 1)
        for widget, title in ((self.diagnosis, "Diagnosis"), (self.terminal, "Terminal"),
                              (changes, "Files & diff"), (self.tests, "Tests"),
                              (self.history_details, "History details")):
            tabs.addTab(widget, title)
        layout.addWidget(tabs, 1)
        self.restart_status = self._label("Restart: not requested")
        layout.addWidget(self.restart_status)

        permission_box = QGroupBox("Class B permissions — saved preferences; revoke at any time")
        permissions_layout = QGridLayout(permission_box)
        self.permissions = {}
        for index, (capability, label) in enumerate((("dependencies", "Install dependencies"), ("venv", "Change virtual environment"),
                                                    ("app_config", "Change app configuration"), ("model_download", "Download models"))):
            checkbox = QCheckBox(label)
            checkbox.toggled.connect(lambda enabled, cap=capability: self._permission(cap, enabled))
            self.permissions[capability] = checkbox
            permissions_layout.addWidget(checkbox, index // 2, index % 2)
        self.revoke_permissions = QPushButton("Revoke all Class B")
        self.revoke_permissions.clicked.connect(self._revoke)
        permissions_layout.addWidget(self.revoke_permissions, 0, 2, 2, 1)
        layout.addWidget(permission_box)
        self.approval = self._label("Class C operations are blocked pending specific per-operation desktop approval and an implemented adapter.")
        layout.addWidget(self.approval)

        history_row = QHBoxLayout()
        history_row.addWidget(self._label("Repair history"))
        self.history = QComboBox()
        self.history.setMinimumContentsLength(20)
        self.history.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.history.currentIndexChanged.connect(self._show_history)
        self.history.activated.connect(lambda _: self.tabs.setCurrentWidget(self.history_details))
        history_row.addWidget(self.history, 1)
        self.rollback_button = QPushButton("Roll back selected")
        self.rollback_button.clicked.connect(self._rollback)
        history_row.addWidget(self.rollback_button)
        self.export_button = QPushButton("Export history")
        self.export_button.clicked.connect(lambda: self._command("export_history", guarded=False))
        history_row.addWidget(self.export_button)
        layout.addLayout(history_row)
        shortcut_row = QHBoxLayout()
        shortcut_row.addWidget(self._label("Emergency stop shortcut"))
        self.shortcut_input = QLineEdit("Ctrl+Alt+Escape")
        shortcut_row.addWidget(self.shortcut_input)
        apply_shortcut = QPushButton("Apply")
        apply_shortcut.clicked.connect(lambda: self.set_emergency_shortcut(self.shortcut_input.text()))
        shortcut_row.addWidget(apply_shortcut)
        self.hotkey_status = self._label("Global STOP off; app shortcut available.")
        shortcut_row.addWidget(self.hotkey_status)
        layout.addLayout(shortcut_row)
        self.stop_shortcut = QShortcut(QKeySequence("Ctrl+Alt+Escape"), self)
        self.stop_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.stop_shortcut.activated.connect(self._stop)
        self.stop_button.clicked.connect(self._stop)
        self.mode.toggled.connect(self._mode_changed)
        self.observation.toggled.connect(self._observation_changed)
        self.event_received.connect(self._event, Qt.ConnectionType.QueuedConnection)
        self.global_hotkey_requested.connect(self._apply_global_hotkey, Qt.ConnectionType.QueuedConnection)
        self.live_sharing_requested.connect(self._apply_live_sharing_active, Qt.ConnectionType.QueuedConnection)
        QCoreApplication.instance().aboutToQuit.connect(self._release_global_hotkey)
        self._controls(False)

    @staticmethod
    def _label(text):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _output(blocks):
        widget = QPlainTextEdit()
        widget.setReadOnly(True)
        widget.setMaximumBlockCount(blocks)
        widget.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        return widget

    def _invoke(self, callback, *args):
        if callback is None:
            self.status.setText("Repair runtime is unavailable; no action was performed.")
            return False
        try:
            return callback(*args) is not False
        except Exception:
            self.status.setText("Repair action could not start. Inspect the diagnostic log.")
            return False

    def _controls(self, enabled):
        for widget in (*self.commands.values(), self.restart_button, self.rollback_button,
                       self.observation, self.interval, self.monitor, self.retain, *self.permissions.values()):
            widget.setEnabled(enabled)
        if not enabled:
            self.set_observation(False)

    def _mode_changed(self, enabled):
        if not self._invoke(self.on_mode, enabled):
            self.set_mode(False)
            return
        self._controls(enabled)
        self._sync_global_hotkey()

    def set_mode(self, enabled):
        self.mode.blockSignals(True)
        self.mode.setChecked(enabled is True)
        self.mode.blockSignals(False)
        self._controls(enabled is True)
        self._sync_global_hotkey()

    @property
    def live_sharing_active(self):
        """Plain state for runtime callers; Qt widgets remain GUI-thread-only."""
        return self._live_sharing_active

    def set_live_sharing_active(self, enabled):
        if type(enabled) is not bool:
            raise TypeError("Live sharing activity must be a boolean")
        if QThread.currentThread() != self.thread():
            self.live_sharing_requested.emit(enabled)
            return True
        return self._apply_live_sharing_active(enabled)

    def _apply_live_sharing_active(self, enabled):
        self._live_sharing_active = enabled
        return self._sync_global_hotkey()

    def _sync_global_hotkey(self):
        return self._apply_global_hotkey(self._hotkey_sequence if
            self.mode.isChecked() or self._live_sharing_active else None)

    def set_observation(self, enabled):
        self.observation.blockSignals(True)
        self.observation.setChecked(enabled is True)
        self.observation.blockSignals(False)

    def _observation_changed(self, enabled):
        if not self._invoke(self.on_observation, enabled):
            self.set_observation(not enabled)

    def observation_options(self):
        return {"interval": self.interval.value(), "monitor": self.monitor.value(), "retain": self.retain.isChecked()}

    def _permission(self, capability, enabled):
        if enabled and not self.mode.isChecked():
            self.set_permissions({capability: False})
            return
        if not self._invoke(self.on_permissions, capability, enabled):
            self.set_permissions({capability: not enabled})

    def set_permissions(self, permissions):
        for capability, value in permissions.items():
            if capability in self.permissions:
                checkbox = self.permissions[capability]
                checkbox.blockSignals(True)
                checkbox.setChecked(value is True)
                checkbox.blockSignals(False)

    def _revoke(self):
        for checkbox in self.permissions.values():
            checkbox.setChecked(False)

    def _command(self, action, guarded=True):
        if not guarded or self.mode.isChecked():
            parameters = {"action": action}
            if action == "inspect_failure" and self.history.currentData():
                parameters["failure_id"] = self.history.currentData()
            self._invoke(self.on_command, parameters)

    def _rollback(self):
        if self.mode.isChecked() and self.history.currentData():
            self._invoke(self.on_rollback, self.history.currentData())

    def _show_history(self, index):
        self.history_details.setPlainText(self.history.itemData(index, Qt.ItemDataRole.UserRole + 1) or "")

    def _stop(self):
        self.set_mode(False)
        self._invoke(self.on_stop)

    def set_global_hotkey(self, sequence_or_none):
        """Return the result on the GUI thread; worker calls queue the request."""
        if QThread.currentThread() != self.thread():
            self.global_hotkey_requested.emit(sequence_or_none)
            return True
        return self._apply_global_hotkey(sequence_or_none)

    def _apply_global_hotkey(self, sequence_or_none, *, force_release=False):
        try:
            if sequence_or_none:
                self._hotkey_sequence = sequence_or_none
            elif not force_release and (self._live_sharing_active or self.mode.isChecked()):
                sequence_or_none = self._hotkey_sequence
            if self._native_hotkey is None and sequence_or_none:
                self._native_hotkey = NativeRepairHotkey(self._stop)
                dispose = self._native_hotkey.dispose
                self.destroyed.connect(lambda _=None: dispose())
            if self._native_hotkey is not None:
                self._native_hotkey.set_sequence(sequence_or_none)
            self.hotkey_status.setText(f"Global STOP active: {sequence_or_none}" if sequence_or_none else
                                      "Global STOP off; app shortcut available.")
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            message = _safe_text(f"Global STOP unavailable: {exc} The application shortcut, Stop Sharing, and STOP REPAIR remain available.")
            self.hotkey_status.setText("Global STOP unavailable; app shortcut available.")
            self.status.setText(message)
            self.terminal.appendPlainText(message)
            return False

    def _release_global_hotkey(self):
        self._apply_global_hotkey(None, force_release=True)

    def set_emergency_shortcut(self, sequence):
        key = QKeySequence.fromString(sequence.strip()) if isinstance(sequence, str) else QKeySequence()
        if key.isEmpty() or key.count() != 1 or key[0].key() == Qt.Key.Key_unknown:
            self.status.setText("Choose one valid emergency shortcut, such as Ctrl+Alt+Escape.")
            return False
        self.stop_shortcut.setKey(key)
        self.shortcut_input.setText(key.toString())
        self._hotkey_sequence = key.toString()
        if self.on_shortcut_changed:
            self._invoke(self.on_shortcut_changed, key.toString())
        self._sync_global_hotkey()
        return True

    def publish(self, event):
        if not isinstance(event, dict):
            return
        safe = {}
        for key in ("task_id", "phase", "message", "diagnosis", "output", "files", "diff", "tests", "restart_status",
                    "operation", "capability", "class", "scope", "reason", "effects"):
            if key in event:
                safe[key] = _safe_text(event[key])
        for key in ("mode", "monitoring", "approval_required"):
            if type(event.get(key)) is bool:
                safe[key] = event[key]
        if isinstance(event.get("observation"), dict):
            safe["observation"] = _safe_text(event["observation"].get("status", ""))
        if isinstance(event.get("history"), list):
            safe["history"] = []
            for record in event["history"][:200]:
                if not isinstance(record, dict):
                    continue
                identity = record.get("id") or record.get("attempt_id") or record.get("failure_id")
                if isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
                    label, details = _history_entry(record, identity)
                    safe["history"].append((identity, label, details))
        self.event_received.emit(safe)

    def set_history(self, records):
        self.publish({"history": records})

    def _event(self, event):
        for key, widget in (("task_id", self.task), ("phase", self.phase), ("message", self.status),
                            ("restart_status", self.restart_status)):
            if key in event:
                widget.setText(event[key])
        for key, widget in (("diagnosis", self.diagnosis), ("files", self.files), ("diff", self.diff), ("tests", self.tests)):
            if key in event:
                widget.setPlainText(event[key])
        if "output" in event:
            self.terminal.appendPlainText(event["output"])
        if "mode" in event:
            self.set_mode(event["mode"])
        if "monitoring" in event:
            self.set_observation(event["monitoring"])
            self.monitoring.setText(event.get("observation") or ("Monitoring ON — local screenshots only" if event["monitoring"] else "Monitoring OFF — no repair screenshots"))
        if event.get("approval_required"):
            operation = event.get("operation") or event.get("capability")
            details = ([f"Operation: {operation}"] if operation else [])
            details.extend(f"{key.title()}: {event[key]}" for key in ("scope", "reason", "effects") if key in event)
            header = ("Class C blocked: specific per-operation desktop approval and an implemented adapter are required."
                      if event.get("class") == "C" else "Operation blocked: per-operation desktop permission is required.")
            self.approval.setText(header + "\n" + "\n".join(details))
        if "history" in event:
            selected = self.history.currentData()
            self.history.blockSignals(True)
            self.history.clear()
            for identity, label, details in event["history"]:
                self.history.addItem(label, identity)
                self.history.setItemData(self.history.count() - 1, details, Qt.ItemDataRole.UserRole + 1)
            index = self.history.findData(selected)
            if index >= 0:
                self.history.setCurrentIndex(index)
            self.history.blockSignals(False)
            self._show_history(self.history.currentIndex())
