"""皮肤定义。

main 和 settings_dialog 都要引用，单独放一个模块，避免循环导入。
只定义颜色，不碰任何 Qt 对象。
"""

SKINS = {
    "read": {
        "bg": "#0d0f12", "fg": "#c3cbd6", "heading": "#a8bccd",
        "tts_hl": "#1d3947", "search_hl": "#4c2f1c", "mark_hl": "#4c411c",
    },
    "ink": {
        "bg": "#070809", "fg": "#a9b0b7", "heading": "#8d97a1",
        "tts_hl": "#14303a", "search_hl": "#3f2818", "mark_hl": "#3d3517",
    },
    "warm": {
        "bg": "#12100d", "fg": "#cdc3b6", "heading": "#bda994",
        "tts_hl": "#33301e", "search_hl": "#4a2e19", "mark_hl": "#43391a",
    },
    "paper": {
        "bg": "#f3f0e8", "fg": "#3b3833", "heading": "#6d6459",
        "tts_hl": "#cbdcea", "search_hl": "#f2d9a6", "mark_hl": "#e6dfa2",
    },
}

SKIN_ORDER = ("read", "ink", "warm", "paper")
SKIN_NAMES = {"read": "墨蓝", "ink": "纯黑", "warm": "暖褐", "paper": "纸白"}

# 浅色皮肤要把所有「白叠加」反过来变成黑叠加，否则按钮、hover、
# 滚动条在浅底上完全看不见
LIGHT_SKINS = ("paper",)


def is_light(st) -> bool:
    return st.get("skin", "read") in LIGHT_SKINS


def overlay(st) -> str:
    """叠加色 RGB 串，QSS 里拼成 rgba({overlay},.07)。"""
    return "17,17,17" if is_light(st) else "255,255,255"


def overlay_pen(st, alpha: int = 18):
    """给 QPainter 用的叠加色（描边、选中底色）。"""
    from PyQt6.QtGui import QColor
    return QColor(17, 17, 17, alpha) if is_light(st) else QColor(255, 255, 255, alpha)


def dim_text(st, level: str = "mid") -> str:
    """次级文字色。浅底下用黑色半透明，深底下用浅灰。"""
    if is_light(st):
        return {"mid": "rgba(60,55,48,.74)", "dim": "rgba(60,55,48,.58)",
                "faint": "rgba(60,55,48,.46)"}[level]
    return {"mid": "rgba(160,175,190,.62)", "dim": "rgba(150,162,176,.66)",
            "faint": "rgba(160,175,190,.55)"}[level]


def list_title_color(st):
    """书库/目录列表的标题色。"""
    from PyQt6.QtGui import QColor
    return QColor(60, 55, 48) if is_light(st) else QColor(208, 219, 231)


def list_sub_color(st):
    from PyQt6.QtGui import QColor
    return QColor(108, 100, 90) if is_light(st) else QColor(132, 144, 158)


def menu_colors(st):
    """右键菜单的底色/边框/文字/选中。"""
    if is_light(st):
        return "#fbf9f4", "#d9d2c4", "#3b3833", "#e6e0d2"
    return "#171d24", "#29333d", "#c8d2dd", "#24303b"


def status_text_color(st):
    from PyQt6.QtGui import QColor
    return QColor(90, 85, 78, 200) if is_light(st) else QColor(150, 162, 176, 168)

# 快捷键的名称。只列真正接上了动作的，改了才有用；
# 列一堆不生效的会让人以为坏了。
KEY_LABELS = {
    "open_search": "搜索",
    "add_bookmark": "添加书签",
    "toggle_skin": "切换皮肤",
    "toggle_play": "朗读开关",
    "toggle_oneline": "一行模式",
    "open_toc": "目录",
    "open_library": "书库",
    "boss_key": "老板键（全局）",
}

KEY_DEFAULTS = {
    "open_search": "Ctrl+F",
    "add_bookmark": "Ctrl+D",
    "toggle_skin": "F10",
    "toggle_play": "F5",
    "toggle_oneline": "F9",
    "open_toc": "Ctrl+T",
    "open_library": "Ctrl+L",
    "boss_key": "Ctrl+Alt+D",
}
