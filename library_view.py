"""书库与目录列表视图。

两种模式共用一套列表控件：
- books：书目列表，双击/Enter 打开，右键移除（10 秒内可 Ctrl+Z 撤销）
- toc：章节列表，选中即跳转

做成独立文件是因为 Reader 已经够长了，而且这层逻辑不需要认识
QTextCursor、滚动条这些阅读态的东西，只依赖 store。
"""
from __future__ import annotations

import os

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QFrame, QListView, QListWidget,
                             QListWidgetItem, QMenu, QPushButton, QStyle,
                             QStyledItemDelegate, QVBoxLayout, QWidget)

import store as store_mod
import theme

ROW_H = 50
CARD_W, CARD_H = 130, 164   # 含卡片之间的间距，560 宽窗口一排能放 4 张
ALIGN_LEFT_MID = (Qt.AlignmentFlag.AlignLeft.value
                  | Qt.AlignmentFlag.AlignVCenter.value)
# 必须带 TextSingleLine：不带这个 flag 时 drawText 会把字符串里的 \n
# 当硬换行排成两行，撑出条目矩形压到下一行去
ALIGN_LEFT_MID_SINGLE = (ALIGN_LEFT_MID
                         | Qt.TextFlag.TextSingleLine.value)
ALIGN_CENTER = Qt.AlignmentFlag.AlignCenter.value


def book_meta(rec: dict) -> str:
    """第二行：格式 · 章号 · 全书进度。总章数缺失时降级成本章内百分比。"""
    prog = rec.get("progress") or {}
    ch = int(prog.get("chapter", 0) or 0) + 1
    ratio = float(prog.get("ratio", 0.0) or 0.0)
    total = int(rec.get("chapters", 0) or 0)
    fmt = (rec.get("format") or "").upper() or "TXT"
    if rec.get("missing"):
        return "文件缺失"
    if total > 0:
        overall = ((ch - 1) + max(0.0, min(1.0, ratio))) / total * 100
        return f"{fmt} · 第{ch}章 · {overall:.0f}%"
    return f"{fmt} · 第{ch}章 · {ratio * 100:.0f}%"


class RowDelegate(QStyledItemDelegate):
    """两行式行：标题 + 次要信息。只负责长相，键盘/滚动交给 QListWidget。"""

    def __init__(self, parent=None, st=None):
        super().__init__(parent)
        self.st = st      # 取当前皮肤的文字色，浅色皮肤下不能再用浅灰

    def sizeHint(self, opt, idx) -> QSize:
        return QSize(0, ROW_H)

    def _title_color(self):
        return theme.list_title_color(self.st) if self.st else QColor(208, 219, 231)

    def _sub_color(self):
        return theme.list_sub_color(self.st) if self.st else QColor(132, 144, 158)

    def paint(self, p: QPainter, opt, idx):
        rec = idx.data(Qt.ItemDataRole.UserRole) or {}
        r = QRectF(opt.rect).adjusted(6, 3, -6, -3)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        missing = bool(rec.get("missing"))

        if selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.overlay_pen(self.st, 18) if self.st
                       else QColor(255, 255, 255, 18))
            p.drawRoundedRect(r, 5, 5)

        # 缺失的书整体压暗，但仍可选中（用户要能把它删掉）
        fade = 0.36 if missing else (1.0 if selected else 0.86)

        f_title = QFont(opt.font)
        f_title.setPointSizeF(10.5)
        p.setFont(f_title)
        tc = QColor(self._title_color())
        tc.setAlpha(int(255 * fade))
        p.setPen(tc)
        title = rec.get("title") or "未命名"
        tr = QRectF(r.left() + 12, r.top() + 7, r.width() - 24, 20)
        fm = QFontMetrics(f_title)
        p.drawText(tr, ALIGN_LEFT_MID_SINGLE,
                   fm.elidedText(title, Qt.TextElideMode.ElideRight, int(tr.width())))

        f_sub = QFont(opt.font)
        f_sub.setPointSizeF(8.5)
        p.setFont(f_sub)
        # 次要信息压得更淡，避免两行抢同一个视觉层级
        sc = QColor(self._sub_color())
        sc.setAlpha(int(190 * fade))
        p.setPen(sc)
        sub = rec.get("sub") or ""
        fm2 = QFontMetrics(f_sub)
        # 高度按实际行高走：8.5pt 中文行高约 16px，写死 15 会裁掉一点
        sr = QRectF(r.left() + 12, r.top() + 27, r.width() - 24, fm2.height())
        p.drawText(sr, ALIGN_LEFT_MID_SINGLE,
                   fm2.elidedText(sub, Qt.TextElideMode.ElideRight, int(sr.width())))


def cover_color(key: str) -> QColor:
    """由书名/id 生成固定的封面底色。

    同一个 hash 永远得到同一个色相，换视图、重启都不变；
    饱和度和亮度压得比较低，才不会在深色主题上刺眼。
    """
    h = 0
    for ch in key or "?":
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return QColor.fromHsl(h % 360, 68, 34)


def _first_char(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return "?"
    return t[0].upper()


class CardDelegate(QStyledItemDelegate):
    """卡片：封面块（书名首字）+ 书名 + 进度。"""

    def __init__(self, parent=None, st=None):
        super().__init__(parent)
        self.st = st

    def sizeHint(self, opt, idx) -> QSize:
        return QSize(CARD_W, CARD_H)

    def paint(self, p: QPainter, opt, idx):
        rec = idx.data(Qt.ItemDataRole.UserRole) or {}
        r = QRectF(opt.rect).adjusted(7, 7, -7, -7)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        missing = bool(rec.get("missing"))
        fade = 0.42 if missing else 1.0

        cover = QRectF(r.left(), r.top(), r.width(), r.height() - 48)

        if missing:
            p.setBrush(QColor(38, 44, 51))
        else:
            c = cover_color(rec.get("id") or rec.get("title") or "")
            p.setBrush(c)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(cover, 6, 6)

        # 首字当封面：半透明，不抢书名
        f_big = QFont(opt.font)
        f_big.setPixelSize(max(18, int(min(cover.width(), cover.height()) * 0.44)))
        f_big.setWeight(QFont.Weight.Bold)
        p.setFont(f_big)
        p.setPen(QColor(255, 255, 255, int(96 * fade)))
        p.drawText(cover, ALIGN_CENTER, _first_char(rec.get("title")))

        if selected:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(112, 163, 194, 200), 1.5))
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)

        f_name = QFont(opt.font)
        f_name.setPointSizeF(9.5)
        p.setFont(f_name)
        nc = QColor(theme.list_title_color(self.st) if self.st
                    else QColor(208, 219, 231))
        nc.setAlpha(int(255 * fade))
        p.setPen(nc)
        nr = QRectF(r.left() + 2, cover.bottom() + 8, r.width() - 4, 17)
        fm = QFontMetrics(f_name)
        p.drawText(nr, ALIGN_LEFT_MID,
                   fm.elidedText(rec.get("title") or "未命名",
                                 Qt.TextElideMode.ElideRight, int(nr.width())))

        f_sub = QFont(opt.font)
        f_sub.setPointSizeF(8)
        p.setFont(f_sub)
        scc = QColor(theme.list_sub_color(self.st) if self.st
                     else QColor(132, 144, 158))
        scc.setAlpha(int(200 * fade))
        p.setPen(scc)
        sr = QRectF(r.left() + 2, cover.bottom() + 26, r.width() - 4, 14)
        fm2 = QFontMetrics(f_sub)
        p.drawText(sr, ALIGN_LEFT_MID,
                   fm2.elidedText(rec.get("sub") or "",
                                  Qt.TextElideMode.ElideRight, int(sr.width())))


def _toggle_icon(kind: str, size: int = 14) -> QIcon:
    """切换按钮的图标：网格 / 横线。不用 unicode 符号，字体不一定有。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(196, 210, 224), 1.5))
    if kind == "grid":
        for gx in (1.0, 8.0):
            for gy in (1.0, 8.0):
                p.drawRect(QRectF(gx, gy, 5, 5))
    else:
        for y in (2, 6, 10):
            p.drawLine(1, y, 13, y)
    p.end()
    return QIcon(pm)


class LibraryView(QWidget):
    open_book = pyqtSignal(str)        # 书的路径
    chapter_chosen = pyqtSignal(int)   # 目录模式：章索引
    mark_chosen = pyqtSignal(int, int)   # 书签模式：(章, 正文偏移)
    mark_removed = pyqtSignal(int, int)
    add_requested = pyqtSignal()
    status_hint = pyqtSignal(str)
    view_changed = pyqtSignal()        # 卡片/列表切换后通知外壳更新按钮

    def __init__(self, st: store_mod.Store, parent=None):
        super().__init__(parent)
        self.st = st
        self.mode = "books"
        self.view_mode = "card"      # card | list，真正的初值在 set_view_mode 里定
        self._snapshot = None
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.setInterval(3000)
        self._hint_timer.timeout.connect(lambda: self.status_hint.emit(""))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 4)
        lay.setSpacing(0)

        self.row_delegate = RowDelegate(self, self.st)
        self.card_delegate = CardDelegate(self, self.st)

        self.list = QListWidget(self)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setItemDelegate(self.row_delegate)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setUniformItemSizes(True)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.viewport().setAutoFillBackground(False)
        # 底部留一点，小窗口时最后一行不会贴着状态条
        self.list.setViewportMargins(0, 0, 0, 6)
        self.list.setStyleSheet("""
            QListWidget { background: transparent; border: none; outline: none; }
            QScrollBar:vertical { background: transparent; width: 9px; margin: 2px 0; }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,.10); border-radius: 4px; min-height: 28px;
            }
            QScrollBar::handle:vertical:hover { background: rgba(255,255,255,.20); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
        """)
        lay.addWidget(self.list)

        self.list.itemDoubleClicked.connect(self._on_activate)
        self.list.customContextMenuRequested.connect(self._on_menu)
        # 焦点在列表上时按键不会冒泡到 LibraryView，得单独拦
        self.list.installEventFilter(self)

        # 切换按钮由外层 Reader 放在底部状态条上，和状态文字同一行才好对齐
        self.set_view_mode(self._pref_view())

    # -------------------------------------------------- 视图切换

    def _pref_view(self) -> str:
        return "card" if self.st.get("library.view", "card") == "card" else "list"

    def set_view_mode(self, mode: str):
        self.view_mode = "card" if mode == "card" else "list"
        is_card = self.view_mode == "card"
        self.list.setItemDelegate(
            self.card_delegate if is_card else self.row_delegate)
        if is_card:
            self.list.setViewMode(QListView.ViewMode.IconMode)
            self.list.setGridSize(QSize(CARD_W, CARD_H))
            self.list.setResizeMode(QListView.ResizeMode.Adjust)
            self.list.setMovement(QListView.Movement.Static)
        else:
            self.list.setViewMode(QListView.ViewMode.ListMode)
            self.list.setGridSize(QSize())
            self.list.setResizeMode(QListView.ResizeMode.Fixed)
        self.list.doItemsLayout()
        self.list.viewport().update()
        self.view_changed.emit()

    def toggle_view(self):
        new = "list" if self.view_mode == "card" else "card"
        self.st.set("library.view", new)
        self.st.save_config()
        if self.mode == "books":
            self.set_view_mode(new)

    def toggle_icon(self) -> QIcon:
        """按钮该显示的图标：卡片态显示「列表」，列表态显示「网格」。"""
        return _toggle_icon("list" if self.view_mode == "card" else "grid")

    # -------------------------------------------------- 数据

    def show_books(self):
        self.mode = "books"
        self.set_view_mode(self._pref_view())
        self.refresh()

    def show_toc(self, chapters: list, current: int):
        self.mode = "toc"
        # 目录用卡片没有意义，固定列表；偏好不动，回到书库自动恢复
        self.set_view_mode("list")
        self.list.clear()
        for i, ch in enumerate(chapters):
            title = getattr(ch, "title", str(ch)) or f"第 {i + 1} 节"
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, {
                "mode": "toc",
                "index": i,
                "title": title,  # 章节名大多自带「第X章」，不再重复加序号
                "sub": "正在读" if i == current else f"{len(getattr(ch, 'text', '') or '')} 字",
            })
            self.list.addItem(item)
            if i == current:
                self.list.setCurrentItem(item)
        if self.list.currentItem():
            self.list.scrollToItem(self.list.currentItem(),
                                   QAbstractItemView.ScrollHint.PositionAtCenter)

    def show_marks(self, marks: list, chapters: list):
        """书签列表。复用行式列表，每条记着章号和正文偏移。"""
        self.mode = "marks"
        self.set_view_mode("list")
        self.list.clear()
        for m in marks:
            ci = int(m.get("chapter", 0) or 0)
            if 0 <= ci < len(chapters):
                title = getattr(chapters[ci], "title", "") or f"第 {ci + 1} 节"
            else:
                title = f"第 {ci + 1} 节"
            # 旧数据里 preview 可能带着段落换行，这里统一压成单行
            preview = " ".join((m.get("preview") or "").split())
            self.list.addItem(self._mark_item({
                "mode": "marks",
                "title": title,
                "sub": preview or "（无预览）",
                "chapter": ci,
                "offset": int(m.get("offset", 0) or 0),
            }))
        if self.list.count():
            self.list.setCurrentRow(0)
            self.list.scrollToTop()

    def _mark_item(self, rec: dict) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, ROW_H))
        item.setData(Qt.ItemDataRole.UserRole, rec)
        return item

    def refresh(self):
        """重读书库。missing 只在这轮统一 stat 一次，末尾整体落盘。"""
        if self.mode != "books":
            return
        books = self.st.books()
        dirty = False
        for b in books:
            path = b.get("path") or ""
            miss = not (path and os.path.exists(path))
            if bool(b.get("missing")) != miss:
                b["missing"] = miss
                dirty = True
        if dirty:
            self.st.save_library()

        books.sort(key=lambda b: (bool(b.get("missing")),
                                  -(b.get("opened") or 0),
                                  -(b.get("added") or 0)))

        current_id = None
        cur_item = self.list.currentItem()
        if cur_item:
            current_id = (cur_item.data(Qt.ItemDataRole.UserRole) or {}).get("id")

        self.list.clear()
        for rec in books:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, {
                "mode": "books",
                "id": rec.get("id"),
                "path": rec.get("path"),
                "title": rec.get("title"),
                "sub": book_meta(rec),
                "missing": bool(rec.get("missing")),
            })
            self.list.addItem(item)
            if rec.get("id") == current_id:
                self.list.setCurrentItem(item)

        self.list.viewport().update()
        self.update()

    # -------------------------------------------------- 交互

    def eventFilter(self, obj, ev):
        if obj is self.list and ev.type() == QEvent.Type.KeyPress:
            if self._handle_key(ev):
                return True
        return super().eventFilter(obj, ev)

    def keyPressEvent(self, ev):
        if not self._handle_key(ev):
            super().keyPressEvent(ev)

    def _handle_key(self, ev) -> bool:
        key = ev.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_activate(self.list.currentItem())
            return True
        if key == Qt.Key.Key_Delete:
            if self.mode == "books":
                self._delete_current()
                return True
            if self.mode == "marks":
                self._remove_current_mark()
                return True
        if (key == Qt.Key.Key_Z
                and ev.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.undo()
            return True
        return False

    def _on_activate(self, item):
        if item is None:
            return
        rec = item.data(Qt.ItemDataRole.UserRole) or {}
        if rec.get("mode") == "toc":
            self.chapter_chosen.emit(int(rec.get("index", 0)))
            return
        if rec.get("mode") == "marks":
            self.mark_chosen.emit(int(rec.get("chapter", 0)),
                                  int(rec.get("offset", 0)))
            return
        if rec.get("missing"):
            self._hint(f"文件已不在原路径：{rec.get('path')}", 5000)
            return
        self.open_book.emit(rec.get("path") or "")

    def _on_menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            if self.mode == "books":
                menu = QMenu(self)
                menu.addAction("添加书籍…").triggered.connect(self.add_requested.emit)
                menu.exec(self.list.mapToGlobal(pos))
            return
        rec = item.data(Qt.ItemDataRole.UserRole) or {}
        menu = QMenu(self)
        if rec.get("mode") == "toc":
            menu.addAction("跳到本章").triggered.connect(lambda: self._on_activate(item))
        elif rec.get("mode") == "marks":
            menu.addAction("跳到这里").triggered.connect(lambda: self._on_activate(item))
            menu.addSeparator()
            menu.addAction("删除书签").triggered.connect(self._remove_current_mark)
        else:
            menu.addAction("打开").triggered.connect(lambda: self._on_activate(item))
            menu.addSeparator()
            menu.addAction("从书库移除").triggered.connect(lambda: self._delete(rec))
        menu.exec(self.list.mapToGlobal(pos))

    def _remove_current_mark(self):
        item = self.list.currentItem()
        if item is None:
            return
        rec = item.data(Qt.ItemDataRole.UserRole) or {}
        self.mark_removed.emit(int(rec.get("chapter", 0)),
                               int(rec.get("offset", 0)))

    def _delete_current(self):
        item = self.list.currentItem()
        if item is None:
            return
        self._delete(item.data(Qt.ItemDataRole.UserRole) or {})

    def _delete(self, rec: dict):
        bid = rec.get("id")
        if not bid:
            return
        # 先留快照：remove_book 会连标注一起删
        self._snapshot = self.st.snapshot_book(bid)
        self.st.remove_book(bid)
        self.refresh()
        self._hint(f"已移除《{rec.get('title') or ''}》 · Ctrl+Z 撤销", 10000)

    def undo(self):
        if not self._snapshot:
            self._hint("没有可撤销的操作")
            return
        self.st.restore_book(self._snapshot)
        self._snapshot = None
        self.refresh()
        self._hint("已恢复")

    def _hint(self, text: str, ms: int = 3000):
        self.status_hint.emit(text)
        self._hint_timer.start(ms)

    # -------------------------------------------------- 空态

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self.mode != "books" or self.list.count() > 0:
            return
        try:
            p = QPainter(self)
            try:
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                cx = self.rect().center().x()
                cy = self.rect().center().y()
                p.setPen(QColor(122, 134, 148, 190))
                f1 = QFont(store_mod.UI_FONT)
                f1.setPointSizeF(11.5)
                p.setFont(f1)
                p.drawText(QRectF(0, cy - 26, self.width(), 24),
                           ALIGN_CENTER, "书库是空的")
                p.setPen(QColor(110, 121, 134, 150))
                f2 = QFont(store_mod.UI_FONT)
                f2.setPointSizeF(9)
                p.setFont(f2)
                p.drawText(QRectF(0, cy + 6, self.width(), 40),
                           ALIGN_CENTER | Qt.AlignmentFlag.TextWordWrap.value,
                           "点右上角「＋」添加 txt 或 epub\n文件只记路径，不会动原文件")
            finally:
                p.end()
        except Exception:
            pass
