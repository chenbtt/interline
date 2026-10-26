"""全局热键（仅 Windows）。

QShortcut 只在窗口有焦点时有效，老板键要求焦点在别的窗口时也能按，
所以走 Win32 的 RegisterHotKey + WM_HOTKEY，用一个原生事件过滤器接住。

两个坑：
- RegisterHotKey 失败时 GetLastError 可能是上次遗留的值，注册前先清一下
- 错误码 1409 = ERROR_HOTKEY_ALREADY_REGISTERED，说明被别的程序占了，
  这时要换一个键，不能当作"不支持"
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, pyqtSignal

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

_MODS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "shift": MOD_SHIFT,
    "win": MOD_WIN, "meta": MOD_WIN,
}

_NAMED = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "delete": 0x2E,
}


def parse_sequence(seq: str):
    """'Ctrl+Alt+D' -> (modifiers, vk)。解析不了返回 None。"""
    if not seq:
        return None
    parts = [p.strip().lower() for p in seq.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        if p not in _MODS:
            return None
        mods |= _MODS[p]
    key = parts[-1]
    if key in _NAMED:
        return mods, _NAMED[key]
    if len(key) == 1:
        return mods, ord(key.upper())
    if key.startswith("f") and key[1:].isdigit():
        n = int(key[1:])
        if 1 <= n <= 24:
            return mods, 0x70 + n - 1
    return None


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, owner: "GlobalHotkey"):
        super().__init__()
        self.owner = owner

    def nativeEventFilter(self, eventType, message):
        try:
            et = bytes(eventType)
        except Exception:
            return False, 0
        if et not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(wt.MSG)).contents
        except Exception:
            return False, 0
        if msg.message == WM_HOTKEY and msg.wParam == self.owner.hotkey_id:
            self.owner.activated.emit()
            return True, 0
        return False, 0


class GlobalHotkey(QObject):
    activated = pyqtSignal()

    def __init__(self, app, parent=None, hotkey_id: int = 0x1E4E):
        super().__init__(parent)
        self.hotkey_id = hotkey_id
        self._user32 = ctypes.windll.user32
        self._current = ""
        self._filter = _Filter(self)
        app.installNativeEventFilter(self._filter)

    def register(self, seq: str) -> bool:
        """注册一个组合键。已注册的会先注销。"""
        self.unregister()
        parsed = parse_sequence(seq)
        if not parsed:
            return False
        mods, vk = parsed
        try:
            ctypes.windll.kernel32.SetLastError(0)
        except Exception:
            pass
        ok = bool(self._user32.RegisterHotKey(None, self.hotkey_id,
                                              mods | MOD_NOREPEAT, vk))
        if ok:
            self._current = seq
        return ok

    def register_first(self, seqs) -> str:
        """按顺序尝试，返回成功的那个；全失败返回空串。"""
        for seq in seqs:
            if seq and self.register(seq):
                return seq
        return ""

    def unregister(self) -> None:
        if self._current:
            try:
                self._user32.UnregisterHotKey(None, self.hotkey_id)
            except Exception:
                pass
            self._current = ""

    @property
    def current(self) -> str:
        return self._current
