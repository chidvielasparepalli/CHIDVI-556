"""Windows emergency hotkey on the Qt event thread; no input-hook dependency."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import itertools
import os

from PyQt6.QtCore import QAbstractNativeEventFilter, QCoreApplication, Qt
from PyQt6.QtGui import QKeySequence


_IDENTIFIERS = itertools.count(0x5560)
_WM_HOTKEY = 0x0312
_MOD_NOREPEAT = 0x4000
_SPECIAL_KEYS = {
    Qt.Key.Key_Escape: 0x1B, Qt.Key.Key_Tab: 0x09, Qt.Key.Key_Backspace: 0x08,
    Qt.Key.Key_Return: 0x0D, Qt.Key.Key_Enter: 0x0D, Qt.Key.Key_Space: 0x20,
    Qt.Key.Key_Insert: 0x2D, Qt.Key.Key_Delete: 0x2E, Qt.Key.Key_Pause: 0x13,
    Qt.Key.Key_Home: 0x24, Qt.Key.Key_End: 0x23, Qt.Key.Key_Left: 0x25,
    Qt.Key.Key_Up: 0x26, Qt.Key.Key_Right: 0x27, Qt.Key.Key_Down: 0x28,
    Qt.Key.Key_PageUp: 0x21, Qt.Key.Key_PageDown: 0x22,
}


def key_chord(sequence):
    key = QKeySequence.fromString(sequence.strip()) if isinstance(sequence, str) else QKeySequence()
    if key.isEmpty() or key.count() != 1 or key[0].key() == Qt.Key.Key_unknown:
        raise ValueError("Choose a single shortcut, such as Ctrl+Alt+Escape.")
    chord = key[0]
    modifiers = 0
    remaining = chord.keyboardModifiers().value
    for qt_flag, win_flag in ((Qt.KeyboardModifier.AltModifier, 0x0001),
                              (Qt.KeyboardModifier.ControlModifier, 0x0002),
                              (Qt.KeyboardModifier.ShiftModifier, 0x0004),
                              (Qt.KeyboardModifier.MetaModifier, 0x0008)):
        if remaining & qt_flag.value:
            modifiers |= win_flag
            remaining &= ~qt_flag.value
    if remaining:
        raise ValueError("The global shortcut supports Ctrl, Alt, Shift and Win modifiers.")
    code = int(chord.key())
    if ord('A') <= code <= ord('Z') or ord('0') <= code <= ord('9'):
        virtual_key = code
    elif int(Qt.Key.Key_F1) <= code <= int(Qt.Key.Key_F24):
        virtual_key = 0x70 + code - int(Qt.Key.Key_F1)
    else:
        virtual_key = _SPECIAL_KEYS.get(chord.key())
    if virtual_key is None:
        raise ValueError("Use a letter, digit, function key, Escape, or navigation key for global STOP.")
    return modifiers, virtual_key


class NativeRepairHotkey(QAbstractNativeEventFilter):
    """Register and handle a thread-associated WM_HOTKEY on the Qt UI thread."""

    def __init__(self, callback, application=None, user32=None):
        super().__init__()
        self._callback = callback
        self._app = application if application is not None else QCoreApplication.instance()
        self._api = user32
        self.identifier = next(_IDENTIFIERS)
        if self.identifier > 0xBFFF:
            raise RuntimeError("No application hotkey identifiers remain.")
        self._chord = None
        self._installed = False

    @property
    def registered(self):
        return self._chord is not None

    def _win32(self):
        if self._api is None:
            if os.name != 'nt':
                raise OSError("Native global STOP is available on Windows only.")
            self._api = ctypes.WinDLL('user32', use_last_error=True)
            self._api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            self._api.RegisterHotKey.restype = wintypes.BOOL
            self._api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self._api.UnregisterHotKey.restype = wintypes.BOOL
        return self._api

    def _detach(self):
        if self._installed:
            self._app.removeNativeEventFilter(self)
            self._installed = False

    def set_sequence(self, sequence):
        chord = None if sequence is None or sequence == '' else key_chord(sequence)
        if chord == self._chord:
            return True
        if self._chord is not None:
            if not self._win32().UnregisterHotKey(None, self.identifier):
                raise OSError("Windows could not unregister the previous global STOP shortcut.")
            self._chord = None
        self._detach()
        if chord is None:
            return True
        if self._app is None:
            raise RuntimeError("The Qt application is unavailable for global STOP.")
        api = self._win32()
        self._app.installNativeEventFilter(self)
        self._installed = True
        modifiers, virtual_key = chord
        if not api.RegisterHotKey(None, self.identifier, modifiers | _MOD_NOREPEAT, virtual_key):
            self._detach()
            raise OSError("Windows refused this global STOP shortcut; it may be reserved or already registered.")
        self._chord = chord
        return True

    def close(self):
        return self.set_sequence(None)

    def dispose(self):
        """Best-effort final cleanup when the owning Qt widget is destroyed."""
        try:
            self.close()
        except (OSError, RuntimeError):
            pass  # The process/thread exit also releases any remaining Windows registration.
        finally:
            self._callback = lambda: None

    def nativeEventFilter(self, event_type, message):
        if self._chord is None or bytes(event_type) not in {b'windows_dispatcher_MSG', b'windows_generic_MSG'} or not message:
            return False, 0
        native = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
        if native.message != _WM_HOTKEY or native.wParam != self.identifier or native.hWnd:
            return False, 0
        chord = (native.lParam & 0xFFFF, (native.lParam >> 16) & 0xFFFF)
        if chord != self._chord:
            return False, 0  # Ignore a queued event for a replaced shortcut.
        self._callback()
        return True, 0
