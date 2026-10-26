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
    # QKeySequence 在部分平台/版本会输出标点名称而不是符号
    "period": 0xBE, "comma": 0xBC, "minus": 0xBD, "plus": 0xBB,
    "semicolon": 0xBA, "slash": 0xBF, "backslash": 0xDC,
    "bracketleft": 0xDB, "bracketright": 0xDD,
    "quoteleft": 0xC0, "apostrophe": 0xDE,
}

# 小键盘上的同一个符号是独立按键，虚拟键码和主键盘不同。
# 只注册 0xBE 的话，用小键盘按点号完全没反应，所以两个都要注册。
_NUMPAD = {
    ".": 0x6E, "/": 0x6F, "*": 0x6A, "-": 0x6D, "+": 0x6B,
    "0": 0x60, "1": 0x61, "2": 0x62, "3": 0x63, "4": 0x64,
    "5": 0x65, "6": 0x66, "7": 0x67, "8": 0x68, "9": 0x69,
}

# 标点不能拿字符编码当虚拟键码：ord('.') 是 46，正好撞上 VK_DELETE，
# 结果「Ctrl+.」会被注册成「Ctrl+Delete」，按了当然没反应。
_OEM = {
    ".": 0xBE, ",": 0xBC, "-": 0xBD, "=": 0xBB,
    ";": 0xBA, "'": 0xDE, "/": 0xBF, "\\": 0xDC,
    "[": 0xDB, "]": 0xDD, "`": 0xC0,
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
    if key in _OEM:
        return mods, _OEM[key]
    # 只有 ASCII 字母数字才能用字符编码当虚拟键码
    if len(key) == 1 and key.isascii() and key.isalnum():
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
        if (msg.message == WM_HOTKEY
                and msg.wParam in self.owner.registered_ids):
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
        self.registered_ids: list[int] = []
        self._filter = _Filter(self)
        app.installNativeEventFilter(self._filter)

    def register(self, seq: str) -> bool:
        """注册一个组合键。已注册的会先注销。

        主键盘符号和小键盘符号是两个不同的 VK，都要注册；
        任意一个成功就算可用（另一个可能被别的程序占着）。
        """
        self.unregister()
        parsed = parse_sequence(seq)
        if not parsed:
            return False
        mods, vk = parsed

        vks = [vk]
        last = seq.split("+")[-1].strip().lower()
        npad = _NUMPAD.get(last)
        if npad is not None and npad != vk:
            vks.append(npad)

        ids: list[int] = []
        for i, key_vk in enumerate(vks):
            hid = self.hotkey_id + i
            try:
                ctypes.windll.kernel32.SetLastError(0)
            except Exception:
                pass
            if self._user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT,
                                           key_vk):
                ids.append(hid)
        if ids:
            self.registered_ids = ids
            self._current = seq
            return True
        return False

    def register_first(self, seqs) -> str:
        """按顺序尝试，返回成功的那个；全失败返回空串。"""
        for seq in seqs:
            if seq and self.register(seq):
                return seq
        return ""

    def unregister(self) -> None:
        for hid in self.registered_ids:
            try:
                self._user32.UnregisterHotKey(None, hid)
            except Exception:
                pass
        self.registered_ids = []
        self._current = ""

    @property
    def current(self) -> str:
        return self._current
