"""配置、书库、标注的持久化。

全部落在 %LOCALAPPDATA%\\Interline\\，不碰桌面、不写启动项、不生成日志。
带 --portable 参数时改为 exe 同目录的 InterlineData\\。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path

APP_NAME = "Interline"

# 正文字号用像素而非磅：滑杆上写多少，屏幕上就是多少像素高，
# 用户不用再心算 12pt=16px。10 是中文的可读下限，40 约等于原来的 32pt。
PX_MIN, PX_MAX = 10, 40

# 界面字体。优先雅黑，缺了就沿这条链往下找，避免缩进量和图标字形走样
UI_FONT = "Microsoft YaHei"
FONT_FALLBACK = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                 "Noto Sans CJK SC", "Source Han Sans SC", "SimHei", "Segoe UI")

DEFAULT_KEYS = {
    "next_page": "Space",
    "prev_page": "Shift+Space",
    "next_chapter": "Right",
    "prev_chapter": "Left",
    "toggle_play": "F5",
    "toggle_oneline": "F9",
    "toggle_skin": "F10",
    "add_bookmark": "Ctrl+D",
    "add_highlight": "Ctrl+H",
    "open_toc": "Ctrl+T",
    "open_search": "Ctrl+F",
    "open_library": "Ctrl+L",
    "open_settings": "Ctrl+,",
    "boss_key": "Ctrl+Alt+D",
    "rate_down": "[",
    "rate_up": "]",
}

DEFAULT_CONFIG = {
    "window": {"x": None, "y": None, "w": 560, "h": 660},
    "always_on_top": True,
    "click_through": False,
    "skin": "read",  # read | code
    "font": {"family": "Microsoft YaHei", "size": 23, "unit": "px",
             "line_height": 1.75, "weight": 400},
    "colors": {
        "bg": "#0d0f12",
        "fg": "#c3cbd6",
        "heading": "#a8bccd",
        "tts_hl": "#1d3947",
        "mark_hl": "#4c411c",
        "search_hl": "#4c2f1c",
    },
    "bg_alpha": 236,        # 面板背景 alpha, 0-255
    "window_opacity": 1.0,  # 整体透明度, 0.15-1.0
    "one_line": {"cols": 20},
    "library": {"view": "card"},   # card | list，书库列表的展示方式
    "tts": {"engine": "winrt", "voice": "", "rate": 0.0, "volume": 1.0},
    "update": {"base_url": "", "check_minutes": 10, "auto": True},
    "keys": DEFAULT_KEYS,
}


def _deep_merge(base: dict, override: dict) -> dict:
    """用 override 覆盖 base，缺失的键保留默认值。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json(path: Path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def _write_json(path: Path, data) -> None:
    """先写临时文件再替换，避免断电/崩溃留下半个文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def app_dir() -> Path:
    # 调试/测试用：不走真实用户目录
    env = os.environ.get("INTERLINE_DATA_DIR")
    if env:
        return Path(env)
    if "--portable" in sys.argv:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent / "InterlineData"
        return Path(__file__).resolve().parent / "InterlineData"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ("." + APP_NAME.lower())


def make_book_id(path: str) -> str:
    """路径决定 id，同一本书换个盘符会被当成新书，这是有意的。

    ⚠ 已知局限：文件搬家/改名后 id 就变了，书签和高亮会孤悬在旧 id 下。
    将来要支持「重新定位」时，必须在这里连同 marks[old_id] 一起迁移到新 id。
    """
    return hashlib.sha1(str(Path(path)).lower().encode("utf-8", "ignore")).hexdigest()[:16]


class Store:
    def __init__(self):
        self.dir = app_dir()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._config_path = self.dir / "config.json"
        self._library_path = self.dir / "library.json"
        self._marks_path = self.dir / "marks.json"

        raw_config = _read_json(self._config_path, {})
        self.config = _deep_merge(DEFAULT_CONFIG, raw_config)
        self._migrate_font_unit(raw_config)
        self.library = _read_json(self._library_path, {"books": []})
        if not isinstance(self.library.get("books"), list):
            self.library = {"books": []}
        # 旧文件没有这个键，补空：启动时据此决定要不要接着上次读
        self.library.setdefault("last_book", "")
        self.marks = _read_json(self._marks_path, {})
        if not isinstance(self.marks, dict):
            self.marks = {}

    def _migrate_font_unit(self, raw) -> None:
        """旧版字号存的是 pt，换成 px 要换算一次。

        必须拿 _deep_merge **之前**的原始配置来判断：合并会把默认值里的
        unit="px" 补进去，之后再也分不出这是不是旧配置，迁移就永远不触发。
        unit 字段同时充当幂等标记，写进文件后不会重复换算。
        """
        font = raw.get("font") if isinstance(raw, dict) else None
        if not isinstance(font, dict) or font.get("unit") == "px":
            return
        try:
            pt = float(font.get("size", 17))
        except (TypeError, ValueError):
            pt = 17.0
        self.config["font"]["size"] = max(PX_MIN, min(PX_MAX, round(pt * 4 / 3)))
        self.config["font"]["unit"] = "px"
        self.save_config()

    # ---------- 通用 ----------
    def get(self, dotted: str, default=None):
        node = self.config
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value) -> None:
        parts = dotted.split(".")
        node = self.config
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save_config(self) -> None:
        _write_json(self._config_path, self.config)

    def save_library(self) -> None:
        _write_json(self._library_path, self.library)

    def save_marks(self) -> None:
        _write_json(self._marks_path, self.marks)

    # ---------- 书库 ----------
    def books(self) -> list:
        """返回副本：外部排序/过滤不该动到内部状态。"""
        return list(self.library["books"])

    def find_book(self, book_id: str):
        for b in self.library["books"]:
            if b.get("id") == book_id:
                return b
        return None

    def add_book(self, path: str, title: str, fmt: str) -> dict:
        bid = make_book_id(path)
        existing = self.find_book(bid)
        if existing:
            # 重新添加时刷新路径与标题，保留进度
            existing["path"] = str(path)
            existing["title"] = title
            existing["missing"] = False
            self.save_library()
            return existing
        book = {
            "id": bid,
            "path": str(path),
            "title": title,
            "format": fmt,
            "added": int(time.time()),
            "opened": 0,
            "chapters": 0,          # 打开后由 set_chapters 补齐，书库靠它算全书进度
            "progress": {"chapter": 0, "offset": 0, "ratio": 0.0},
            "tts": None,
            "missing": False,
        }
        self.library["books"].append(book)
        self.save_library()
        return book

    def remove_book(self, book_id: str) -> None:
        self.library["books"] = [b for b in self.library["books"] if b.get("id") != book_id]
        self.save_library()
        self.marks.pop(book_id, None)
        self.save_marks()

    def touch_book(self, book_id: str, path: str) -> None:
        b = self.find_book(book_id)
        if not b:
            return
        b["opened"] = int(time.time())
        b["missing"] = not Path(path).exists()
        self.save_library()

    def set_last_book(self, book_id: str) -> None:
        """记住最后读的是哪本，下次启动接着打开。

        记在打开时而不是关闭时：点 × 只是隐藏窗口，进程还在，
        走不到任何关闭流程，那样记就漏了。
        """
        if self.library.get("last_book") != book_id:
            self.library["last_book"] = book_id
            self.save_library()

    def last_book(self) -> str:
        v = self.library.get("last_book")
        return v if isinstance(v, str) else ""

    def set_progress(self, book_id: str, chapter: int, offset: int, ratio: float) -> None:
        b = self.find_book(book_id)
        if not b:
            return
        b["progress"] = {"chapter": int(chapter), "offset": int(offset), "ratio": float(ratio)}
        self.save_library()

    def set_chapters(self, book_id: str, count: int) -> None:
        """记录总章数。书库列表要用它算「全书进度」，不写这行以后就得做数据迁移。"""
        b = self.find_book(book_id)
        if not b:
            return
        if b.get("chapters") != int(count):
            b["chapters"] = int(count)
            self.save_library()

    def set_tts_progress(self, book_id: str, chapter: int, sentence: int) -> None:
        b = self.find_book(book_id)
        if not b:
            return
        b["tts"] = {"chapter": int(chapter), "sentence": int(sentence)}
        self.save_library()

    # ---------- 删除与撤销 ----------

    def snapshot_book(self, book_id: str) -> dict | None:
        """删除前留快照。remove_book 会连标注一起删，撤销只能靠这份拷贝。"""
        b = self.find_book(book_id)
        if not b:
            return None
        return {
            "book": copy.deepcopy(b),
            "marks": copy.deepcopy(self.marks.get(book_id)
                                   or {"bookmarks": [], "highlights": []}),
        }

    def restore_book(self, snapshot: dict | None) -> None:
        if not snapshot or not snapshot.get("book"):
            return
        bid = snapshot["book"].get("id")
        if not bid:
            return
        self.library["books"] = [b for b in self.library["books"] if b.get("id") != bid]
        self.library["books"].append(snapshot["book"])
        if snapshot.get("marks"):
            self.marks[bid] = snapshot["marks"]
        self.save_library()
        self.save_marks()

    # ---------- 标注 ----------
    def _marks_of(self, book_id: str) -> dict:
        node = self.marks.setdefault(book_id, {})
        node.setdefault("bookmarks", [])
        node.setdefault("highlights", [])
        return node

    def bookmarks(self, book_id: str) -> list:
        return self._marks_of(book_id)["bookmarks"]

    def highlights(self, book_id: str, chapter: int = None) -> list:
        items = self._marks_of(book_id)["highlights"]
        if chapter is None:
            return items
        return [h for h in items if h.get("chapter") == chapter]

    def add_bookmark(self, book_id: str, chapter: int, offset: int, preview: str) -> dict:
        node = self._marks_of(book_id)
        for b in node["bookmarks"]:
            # 配置被手工改过时缺字段也别崩
            if b.get("chapter") == chapter and abs(int(b.get("offset", 0)) - offset) < 40:
                return b  # 同一位置不重复加
        item = {
            "chapter": int(chapter),
            "offset": int(offset),
            "preview": preview[:80],
            "created": int(time.time()),
        }
        node["bookmarks"].append(item)
        self.save_marks()
        return item

    def remove_bookmark(self, book_id: str, chapter: int, offset: int) -> None:
        node = self._marks_of(book_id)
        node["bookmarks"] = [
            b for b in node["bookmarks"]
            if not (b.get("chapter") == chapter and b.get("offset") == offset)
        ]
        self.save_marks()

    def add_highlight(self, book_id: str, chapter: int, start: int, end: int, text: str) -> dict:
        node = self._marks_of(book_id)
        item = {
            "chapter": int(chapter),
            "start": int(start),
            "end": int(end),
            "text": text[:200],
            "created": int(time.time()),
        }
        node["highlights"].append(item)
        self.save_marks()
        return item

    def remove_highlight(self, book_id: str, chapter: int, start: int, end: int) -> None:
        node = self._marks_of(book_id)
        node["highlights"] = [
            h for h in node["highlights"]
            if not (h.get("chapter") == chapter and h.get("start") == start
                    and h.get("end") == end)
        ]
        self.save_marks()

    def clear_marks(self, book_id: str) -> None:
        self.marks[book_id] = {"bookmarks": [], "highlights": []}
        self.save_marks()
