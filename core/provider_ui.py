"""Qt provider profiles and first-run setup; network operations stay on workers."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
import platform
import threading

from PyQt6.QtCore import Qt, QRect, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFormLayout,
                            QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                            QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)


CAPABILITIES = ("text", "tools", "vision", "streaming", "live_audio", "tts", "embeddings")
ROLES = {"chat": "text", "vision": "vision", "live_audio": "live_audio", "tts": "tts", "embeddings": "embeddings"}
TASKS = {"chat": "text", "vision": "vision", "planning": "text"}
USAGE_FIELDS = {"input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
                "prompt_tokens", "completion_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                "input_audio_tokens", "output_audio_tokens", "requests"}


def get_provider_store():
    from core.provider_config import get_provider_store as get_store
    return get_store()


def _notice(value, secret=""):
    text = str(value)
    if secret:
        text = text.replace(secret, "[redacted]")
    try:
        from core.repair_permissions import redact
        text = redact(text)
    except Exception:
        return "Provider operation completed; details unavailable."
    return text[:800]


def _numeric_metadata(value, allowed):
    if not isinstance(value, dict):
        return None
    return {key: number for key, number in value.items() if key in allowed
            and type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 10**12} or None


class ProviderDialog(QDialog):
    changed = pyqtSignal(str)  # profile ID only, never a credential
    operation_finished = pyqtSignal(object)

    def __init__(self, parent=None, *, store=None, registry=None, first_run=False, initial_system=None):
        super().__init__(parent)
        if registry is None:
            from core import providers as registry
        self.registry = registry
        self.store = store if store is not None else get_provider_store()
        self.first_run, self.on_setup = first_run, None
        self._identity, self._generation, self._busy, self._loading = None, 0, False, False
        self._closed = False
        self._request_control = None
        self._models = {}
        self.setWindowTitle("Set up AI providers" if first_run else "AI Providers")
        self.setMinimumSize(240, 180)
        self.resize(660, 730)
        outer = QVBoxLayout(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumSize(0, 0)
        content = QWidget()
        body = QVBoxLayout(content)
        intro = self._label("Primary chat handles typed desktop and phone tasks. Gemini Live separately handles live voice conversation. "
                            "Other providers do not automatically support live audio. Vision, live audio, speech and embeddings are separate roles; "
                            "only compatible profiles can be assigned. Live image context belongs to Live audio; screenshot analysis needs a regular vision model. "
                            "Saving a provider does not start screen sharing.")
        body.addWidget(intro)
        profile_row = QHBoxLayout()
        self.profile_picker = QComboBox()
        self.profile_picker.setMinimumWidth(0)
        self.profile_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.profile_picker.setMinimumContentsLength(14)
        profile_row.addWidget(self.profile_picker, 1)
        self.add_profile = QPushButton("Add profile")
        self.add_profile.clicked.connect(self._new_profile)
        profile_row.addWidget(self.add_profile)
        self.remove_profile = QPushButton("Remove")
        self.remove_profile.clicked.connect(self._remove_profile)
        profile_row.addWidget(self.remove_profile)
        body.addLayout(profile_row)
        self.import_legacy = QPushButton("Import existing Gemini settings")
        self.import_legacy.setToolTip("Move an existing local Gemini key into encrypted provider storage; keep assistant and privacy settings.")
        self.import_legacy.clicked.connect(self._import_legacy)
        body.addWidget(self.import_legacy)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.profile_name = QLineEdit()
        self.profile_name.setMaxLength(100)
        form.addRow("Profile name", self.profile_name)
        self.provider = QComboBox()
        for identity, entry in registry.PROVIDERS.items():
            self.provider.addItem(entry["label"], identity)
        form.addRow("Provider", self.provider)
        self.endpoint = QLineEdit()
        self.endpoint.setMaxLength(2048)
        self.endpoint.setPlaceholderText("Provider default endpoint")
        form.addRow("API endpoint", self.endpoint)
        self.auth = QComboBox()
        form.addRow("Authentication", self.auth)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("API key")
        self.api_key.setMaxLength(8192)
        form.addRow("API key", self.api_key)
        self.revoke_key = QPushButton("Revoke saved key")
        self.revoke_key.clicked.connect(self._revoke_key)
        form.addRow("", self.revoke_key)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.setMinimumContentsLength(10)
        self.model.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model.lineEdit().setPlaceholderText("Enter a model ID or fetch the provider's models")
        self.model.lineEdit().setMaxLength(200)
        self.model.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.model.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        form.addRow("Model (search or type)", self.model)
        self.max_output_tokens = QSpinBox()
        self.max_output_tokens.setRange(1, 16384)
        self.max_output_tokens.setValue(1024)
        form.addRow("Maximum output tokens per request", self.max_output_tokens)
        self.session_token_warning = QSpinBox()
        self.session_token_warning.setRange(0, 10_000_000)
        self.session_token_warning.setSpecialValueText("No session token warning")
        form.addRow("Warn at session token count", self.session_token_warning)
        body.addLayout(form)
        capabilities = QGroupBox("Declared model capabilities")
        capability_layout = QGridLayout(capabilities)
        self.capabilities = {}
        for index, capability in enumerate(CAPABILITIES):
            checkbox = QCheckBox(capability.replace("_", " ").title())
            self.capabilities[capability] = checkbox
            capability_layout.addWidget(checkbox, index // 3, index % 3)
            checkbox.toggled.connect(self._edited)
        body.addWidget(capabilities)
        body.addWidget(self._label("Known model capabilities come from the registry. For a manual model, enable only capabilities "
                                   "documented by its provider. These declarations are not proof that a connection test verified them."))
        self.test_kind = QComboBox()
        for label, value in (("Text: synthetic greeting", "text"), ("Vision: generated red square only", "vision"),
                             ("Tools: synthetic function, no execution", "tools")):
            self.test_kind.addItem(label, value)
        body.addWidget(self.test_kind)
        actions = QHBoxLayout()
        self.test_connection = QPushButton("Test selected capability")
        self.test_connection.clicked.connect(lambda: self._operation("test"))
        self.fetch_models = QPushButton("Fetch models")
        self.fetch_models.clicked.connect(lambda: self._operation("models"))
        self.save_profile = QPushButton("Save profile")
        self.save_profile.clicked.connect(self._save_profile)
        for button in (self.test_connection, self.fetch_models, self.save_profile):
            actions.addWidget(button)
        body.addLayout(actions)
        body.addWidget(self._label("Test and Fetch contact the selected endpoint. Select the capability to test, then press Test. "
                                   "Probes use synthetic content only, never a desktop image or an executed action, and may incur provider usage. "
                                   "Keys are never shown again; leave the field blank to keep a saved key."))
        self.default_chat = QCheckBox("Use this profile as the default chat provider")
        body.addWidget(self.default_chat)
        self.status = self._label("No provider request has been sent.")
        body.addWidget(self.status)
        self.usage = self._label("Pricing, context limits and usage: unknown until the provider supplies them.")
        body.addWidget(self.usage)
        assignments = QGroupBox("Roles and optional task overrides")
        assignment_layout = QFormLayout(assignments)
        assignment_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.role_boxes, self.override_boxes = {}, {}
        for group, mapping, prefix in ((self.role_boxes, ROLES, ""), (self.override_boxes, TASKS, "Task: ")):
            for role in mapping:
                combo = QComboBox()
                combo.setMinimumContentsLength(12)
                combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                group[role] = combo
                assignment_layout.addRow(prefix + role.replace("_", " ").title(), combo)
        self.save_assignments = QPushButton("Save assignments")
        self.save_assignments.clicked.connect(self._save_assignments)
        assignment_layout.addRow(self.save_assignments)
        body.addWidget(assignments)
        self.finish_setup = None
        if first_run:
            system = initial_system or {}
            group = QGroupBox("Your assistant")
            setup = QFormLayout(group)
            setup.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            self.assistant_name = QLineEdit(system.get("assistant_name") or "JARVIS")
            self.assistant_name.setMaxLength(80)
            self.user_name = QLineEdit(system.get("user_name") or "")
            self.user_name.setMaxLength(80)
            self.os_system = QComboBox()
            for identity, label in (("windows", "Windows"), ("mac", "macOS"), ("linux", "Linux")):
                self.os_system.addItem(label, identity)
            detected = {"Windows": "windows", "Darwin": "mac"}.get(platform.system(), "linux")
            index = self.os_system.findData(system.get("os_system") or detected)
            self.os_system.setCurrentIndex(max(0, index))
            setup.addRow("Assistant name", self.assistant_name)
            setup.addRow("Your name", self.user_name)
            setup.addRow("Operating system", self.os_system)
            body.addWidget(group)
        body.addStretch()
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll, 1)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        if first_run:
            self.finish_setup = QPushButton("Continue with selected chat provider")
            self.finish_setup.clicked.connect(self._finish_setup)
            outer.addWidget(self.finish_setup)
        outer.addWidget(self.close_button)
        self.operation_finished.connect(self._operation_done, Qt.ConnectionType.QueuedConnection)
        self.profile_picker.currentIndexChanged.connect(self._load_selected)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.model.currentTextChanged.connect(self._model_changed)
        for edit in (self.profile_name, self.endpoint, self.api_key):
            edit.textChanged.connect(self._edited)
        self.auth.currentIndexChanged.connect(self._edited)
        self.test_kind.currentIndexChanged.connect(self._edited)
        self.max_output_tokens.valueChanged.connect(self._edited)
        self.session_token_warning.valueChanged.connect(self._edited)
        self._refresh_profiles()

    @staticmethod
    def _label(text):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    def available_geometry(self):
        screen = self.screen() or QApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 800, 600)

    def present(self):
        self._closed = False
        area = self.available_geometry().adjusted(8, 8, -8, -8)
        self.setMaximumSize(max(240, area.width()), max(180, area.height()))
        self.resize(min(660, max(240, area.width())), min(730, max(180, area.height())))
        self.showNormal()
        frame = self.frameGeometry()
        self.move(area.x() + (area.width() - frame.width()) // 2,
                  area.y() + (area.height() - frame.height()) // 2)
        self.raise_()
        self.activateWindow()

    def _edited(self, *_):
        if not self._loading:
            self._generation += 1
            self._cancel_worker()

    def _cancel_worker(self):
        control = self._request_control
        if control is None:
            return
        control["cancel"].set()
        loop, task = control.get("loop"), control.get("task")
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The bounded worker already closed its event loop.

    def _provider_changed(self, *_):
        if self._loading:
            return
        self._loading = True
        entry = self.registry.PROVIDERS[self.provider.currentData()]
        self.endpoint.setText(entry.get("default_base_url") or "")
        self.auth.clear()
        self.auth.addItem("API key", "api_key")
        if not entry.get("key_required", True):
            self.auth.addItem("No authentication (trusted local endpoint)", "none")
        self.model.clear()
        self._models.clear()
        self.api_key.clear()
        self._loading = False
        self._model_changed()

    def _model_changed(self, *_):
        if self._loading:
            return
        self._generation += 1
        self._cancel_worker()
        caps = self.registry.model_capabilities(self.provider.currentData(), self.model.currentText().strip())
        for key, checkbox in self.capabilities.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(caps.get(key) is True)
            checkbox.setEnabled(caps.get(key) is not False)
            checkbox.blockSignals(False)
        self._show_model_metadata()

    def _show_model_metadata(self, usage=None):
        entry = self._models.get(self.model.currentText(), {})
        parts = []
        for name, value in (("Pricing", entry.get("pricing")), ("Context limits", entry.get("limits")), ("Usage", usage)):
            parts.append(name + ": " + (json.dumps(value, sort_keys=True) if value else "unknown"))
        self.usage.setText(" | ".join(parts))

    def _refresh_profiles(self, selected=None):
        items = self.store.list_profiles()
        self.profile_picker.blockSignals(True)
        self.profile_picker.clear()
        for profile in items:
            self.profile_picker.addItem(f"{profile.get('name') or profile['provider']} · {profile.get('model', '')}", profile["id"])
        index = self.profile_picker.findData(selected) if selected else 0
        self.profile_picker.setCurrentIndex(max(0, index) if items else -1)
        self.profile_picker.blockSignals(False)
        self._load_selected()
        self._refresh_assignments()

    def _new_profile(self):
        self.profile_picker.blockSignals(True)
        self.profile_picker.setCurrentIndex(-1)
        self.profile_picker.blockSignals(False)
        self._load_selected()

    def _load_selected(self, *_):
        self._generation += 1
        self._cancel_worker()
        identity = self.profile_picker.currentData()
        profile = self.store.get_profile(identity) if identity else None
        self._identity = identity
        self._loading = True
        self.profile_name.setText(profile.get("name", "") if profile else "")
        provider = profile.get("provider") if profile else next(iter(self.registry.PROVIDERS))
        self.provider.setCurrentIndex(max(0, self.provider.findData(provider)))
        entry = self.registry.PROVIDERS[self.provider.currentData()]
        self.auth.clear()
        self.auth.addItem("API key", "api_key")
        if not entry.get("key_required", True):
            self.auth.addItem("No authentication (trusted local endpoint)", "none")
        self.auth.setCurrentIndex(max(0, self.auth.findData(profile.get("auth_type", "api_key") if profile else "api_key")))
        self.endpoint.setText((profile.get("base_url") if profile else "") or entry.get("default_base_url") or "")
        self.model.clear()
        self.model.setCurrentText(profile.get("model", "") if profile else "")
        self._models.clear()
        limits = profile.get("limits", {}) if profile else {}
        self.max_output_tokens.setValue(limits.get("max_output_tokens", 1024))
        self.session_token_warning.setValue(limits.get("session_token_warning", 0))
        self.api_key.clear()
        present = bool(profile and profile.get("has_credential"))
        self.api_key.setPlaceholderText("Saved key; leave blank to keep it" if present else "API key")
        self.revoke_key.setEnabled(present)
        self.remove_profile.setEnabled(profile is not None)
        caps = self.registry.model_capabilities(provider, self.model.currentText(), profile.get("capabilities", {}) if profile else {})
        base = self.registry.model_capabilities(provider, self.model.currentText())
        for key, checkbox in self.capabilities.items():
            checkbox.setChecked(caps.get(key) is True)
            checkbox.setEnabled(base.get(key) is not False)
        selected = self.store.selected("chat")
        self.default_chat.setChecked(not selected or bool(profile and selected["id"] == profile["id"]))
        self._loading = False
        if profile:
            state = profile.get("status", "untested")
            state = state if state in {"untested", "connected", "disconnected", "error"} else "unknown"
            message = "Saved status: " + state + "."
            timestamp = profile.get("last_tested")
            if type(timestamp) in (int, float) and math.isfinite(timestamp):
                try:
                    message += " Last checked: " + datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + "."
                except (ValueError, OSError, OverflowError):
                    pass
            if profile.get("last_error"):
                message += " " + _notice(profile["last_error"])
            message += " Saved connection status does not verify every declared capability."
            self.status.setText(message)
        else:
            self.status.setText("Add a provider profile to begin.")
        self._show_model_metadata(_numeric_metadata(profile.get("last_usage"), USAGE_FIELDS) if profile else None)

    def _profile(self, require_model=True):
        model = self.model.currentText().strip()
        if require_model and not model:
            raise ValueError("Enter or select a model ID first.")
        profile = {"name": self.profile_name.text().strip() or self.provider.currentText(),
                   "provider": self.provider.currentData(), "model": model,
                   "base_url": self.endpoint.text().strip(), "auth_type": self.auth.currentData(),
                   "capabilities": {key: checkbox.isChecked() for key, checkbox in self.capabilities.items()},
                   "limits": {"max_output_tokens": self.max_output_tokens.value(),
                              "session_token_warning": self.session_token_warning.value()}}
        if self._identity:
            profile["id"] = self._identity
        return profile

    def _key_for_form(self, profile):
        if profile.get("auth_type") == "none":
            return ""  # Choosing no authentication explicitly revokes a saved key.
        key = self.api_key.text().strip()
        saved = self.store.get_profile(profile["id"]) if profile.get("id") else None
        if not key and saved and any(str(profile.get(field, "")).rstrip("/") != str(saved.get(field, "")).rstrip("/")
                                     for field in ("provider", "base_url", "auth_type")):
            raise ValueError("Enter a new key after changing the provider, endpoint or authentication.")
        return key or None

    def _save_profile(self):
        try:
            profile = self._profile()
            key = self._key_for_form(profile)
            default = self.default_chat.isChecked()
            saved = self.store.save_profile(profile, key=key)
            self.api_key.clear()
            assignment_failed = False
            if default:
                try:
                    self.store.set_role("chat", saved["id"])
                except Exception:
                    assignment_failed = True
            self._refresh_profiles(saved["id"])
            self.status.setText("Profile saved, but the default chat assignment could not be saved. Check the model's Text capability." if assignment_failed else
                                "Profile saved. " + ("Default chat provider selected." if default else "Assign it to a role when ready."))
            self.changed.emit(saved["id"])
        except Exception as exc:
            self.status.setText("Profile could not be saved. " + _notice(str(exc), self.api_key.text().strip()))

    def _revoke_key(self):
        if not self._identity:
            return
        try:
            profile = self.store.get_profile(self._identity)
            self.store.save_profile(profile, key="")
            self.api_key.clear()
            identity = self._identity
            self._refresh_profiles(identity)
            self.status.setText("Saved credential revoked. This profile needs a new key before authenticated requests.")
            self.changed.emit(identity)
        except Exception:
            self.status.setText("Credential could not be revoked. The existing credential was kept.")

    def _import_legacy(self):
        self._generation += 1
        self._cancel_worker()
        self.api_key.clear()
        try:
            report = self.store.migrate_legacy()
            if report.get("ok") is not True:
                self.status.setText("Legacy import failed. Existing settings were preserved; check encrypted storage and retry.")
                return
            self._refresh_profiles()
            if report.get("migrated"):
                self.status.setText("Existing Gemini settings imported into encrypted profiles. Assistant and privacy settings were retained.")
                selected = self.store.selected("chat")
                self.changed.emit(selected["id"] if selected else "")
            else:
                self.status.setText("No legacy Gemini credential was found to import. Existing profiles were kept.")
        except Exception:
            self.status.setText("Legacy import could not complete. Existing files were preserved; check encrypted storage and retry.")

    def _remove_profile(self):
        if not self._identity:
            return
        decision = QMessageBox.question(self, "Remove provider profile", "Remove this profile and its saved credential? Role assignments to it will be cleared.",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if decision != QMessageBox.StandardButton.Yes:
            return
        try:
            identity = self._identity
            self.store.remove_profile(identity)
            self.api_key.clear()
            self._refresh_profiles()
            self.status.setText("Provider profile removed.")
            self.changed.emit(identity)
        except Exception:
            self.status.setText("Provider profile could not be removed.")

    def _refresh_assignments(self):
        from core.provider_runtime import is_live_profile
        profiles = self.store.list_profiles()
        snapshot = self.store.snapshot()
        for boxes, mapping, selected in ((self.role_boxes, ROLES, snapshot.get("roles", {})),
                                         (self.override_boxes, TASKS, snapshot.get("overrides", {}))):
            for role, combo in boxes.items():
                combo.clear()
                combo.addItem("Use default role" if boxes is self.override_boxes else "Not assigned", None)
                for profile in profiles:
                    caps = self.registry.model_capabilities(profile["provider"], profile["model"], profile.get("capabilities", {}))
                    if mapping[role] == "vision" and is_live_profile(profile):
                        continue
                    if caps.get(mapping[role]) is True:
                        combo.addItem(f"{profile.get('name') or profile['provider']} · {profile['model']}", profile["id"])
                combo.setCurrentIndex(max(0, combo.findData(selected.get(role))))
        if self.finish_setup is not None:
            self.finish_setup.setEnabled(self.store.has_configuration())

    def _save_assignments(self):
        try:
            for role, combo in self.role_boxes.items():
                self.store.set_role(role, combo.currentData())
            for task, combo in self.override_boxes.items():
                self.store.set_override(task, combo.currentData())
            self._refresh_assignments()
            selected = self.store.selected("chat")
            self.changed.emit(selected["id"] if selected else "")
            self.status.setText("Provider assignments saved.")
        except Exception:
            try:
                self._refresh_assignments()
                selected = self.store.selected("chat")
                self.changed.emit(selected["id"] if selected else "")
            except Exception:
                pass
            self.status.setText("Some assignments could not be saved. Current stored values are shown.")

    def _operation(self, action):
        if self._busy:
            return
        try:
            profile = self._profile(require_model=action != "models")
            entered_key = self._key_for_form(profile)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        except Exception:
            self.status.setText("Provider configuration could not be read. No request was sent.")
            return
        generation = self._generation
        capability = self.test_kind.currentData()
        self._busy = True
        self.test_connection.setEnabled(False)
        self.fetch_models.setEnabled(False)
        self.status.setText("Fetching model list…" if action == "models" else f"Testing a synthetic {capability} request…")
        control = {"cancel": threading.Event()}
        self._request_control = control
        def work():
            key = entered_key
            try:
                if key is None and profile.get("id") and profile.get("auth_type") != "none":
                    key = self.store.credential(profile["id"]) or ""
                adapter = self.registry.create_adapter(profile, key or None)
                async def request():
                    control.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
                    if control["cancel"].is_set():
                        raise asyncio.CancelledError
                    if action == "models":
                        return await adapter.models()
                    return await adapter.test(model=profile["model"], capability=capability)
                result = asyncio.run(asyncio.wait_for(request(), timeout=35))
                if action == "models":
                    models = [{"id": _notice(item["id"], key),
                               "pricing": _numeric_metadata(item.get("pricing"), {"input_per_million", "output_per_million", "cached_input_per_million"}),
                               "limits": _numeric_metadata(item.get("limits"), {"input_tokens", "output_tokens", "context_tokens"})}
                              for item in result[:1000] if isinstance(item, dict) and isinstance(item.get("id"), str)]
                    payload = {"models": models, "ok": True}
                else:
                    payload = {"ok": result.get("ok") is True,
                               "capability": _notice(result.get("capability", "text"), key),
                               "usage": _numeric_metadata(result.get("usage"), USAGE_FIELDS)}
            except asyncio.CancelledError:
                payload = {"ok": False, "error": "Provider request cancelled after settings changed or the dialog closed."}
            except Exception as exc:
                code = getattr(exc, "code", type(exc).__name__)
                payload = {"ok": False, "error": _notice(f"Provider request failed ({code}). Check credentials, endpoint and model.", key)}
            if (action == "test" and profile.get("id") and not control["cancel"].is_set()
                    and not self._closed and generation == self._generation):
                try:
                    # Match and write atomically in storage. The request credential
                    # stays on this worker and is never part of the Qt payload.
                    saved = self.store.record_status(profile["id"], connected=payload["ok"],
                        error="" if payload["ok"] else payload.get("error", f"Synthetic {capability} test did not pass."),
                        usage=payload.get("usage"), expected_profile=profile, expected_credential=key or None)
                    payload["status_saved"] = saved is not None
                except Exception:
                    payload["status_save_failed"] = True
            try:
                self.operation_finished.emit({"generation": generation, "action": action, **payload})
            except RuntimeError:
                pass  # The owning window was destroyed while its bounded request drained.
        threading.Thread(target=work, name="chidvi-provider-check", daemon=True).start()

    def _operation_done(self, result):
        self._busy = False
        self._request_control = None
        self.test_connection.setEnabled(True)
        self.fetch_models.setEnabled(True)
        if self._closed or result["generation"] != self._generation:
            self.status.setText("Provider selection changed; the previous request result was discarded.")
            return
        if not result["ok"]:
            self.status.setText(result.get("error") or "Provider test failed; no role or capability was changed.")
            if result["action"] == "test":
                self._show_test_persistence(result)
                self._show_model_metadata(result.get("usage"))
            return
        if result["action"] == "models":
            current = self.model.currentText()
            self._loading = True
            self.model.clear()
            for item in result["models"]:
                self.model.addItem(item["id"])
                self._models[item["id"]] = item
            self.model.setCurrentText(current)
            self._loading = False
            self.status.setText(f"Fetched {len(result['models'])} models. Manual model IDs remain available.")
        else:
            capability = result.get("capability", "text")
            self.status.setText(f"Synthetic {capability} test passed. Other capabilities were not verified; no action was executed.")
            self._show_test_persistence(result)
        self._show_model_metadata(result.get("usage"))

    def _show_test_persistence(self, result):
        if result.get("status_saved"):
            suffix = " Saved connection status updated."
        elif result.get("status_save_failed"):
            suffix = " Test result could not be saved locally."
        else:
            suffix = " Test result not saved: this request did not match a current saved profile."
        self.status.setText(self.status.text() + suffix)

    def _finish_setup(self):
        if not self.store.has_configuration():
            self.status.setText("Save a configured default chat profile before continuing.")
            return
        values = {"assistant_name": self.assistant_name.text().strip() or "JARVIS",
                  "user_name": self.user_name.text().strip(), "os_system": self.os_system.currentData()}
        try:
            if self.on_setup is not None and self.on_setup(values) is False:
                self.status.setText("Assistant settings could not be saved. Setup remains open.")
                return
            self.accept()
        except Exception:
            self.status.setText("Assistant settings could not be saved. Setup remains open.")

    def done(self, result):
        self._closed = True
        self._generation += 1
        self._cancel_worker()
        self.api_key.clear()
        super().done(result)
