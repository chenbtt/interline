"""奶读模式里挂在奶蛙旁边的书库面板。

为什么是独立窗口，而不是把奶蛙窗口撑宽
--------------------------------------
奶蛙窗口的尺寸恒等于「气泡 + 蛙」，`_pet_metrics` / `_pet_hit` / `_paint_pet`
一整套几何都建立在这个前提上。把面板塞进去，气泡换行、缩放滑块、拖拽、
点击命中全都要重算，任何一处对不上就是「点气泡点到空地上」。
独立窗口则互不干扰：开关面板不会让蛙跳一格，蛙拖到哪面板跟到哪。

它还顺带解决了另一件事 —— 面板不需要跟气泡抢宽度，可以比气泡宽得多。
"""
from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

import library_view
import theme

# 面板宽度 / 与奶蛙窗口的间隙 / 最小高度
PANEL_W = 360
PANEL_GAP = 12
PANEL_MIN_H = 460
# 面板顶不贴屏幕边，留一点余量给圆角和阴影
SCREEN_MARGIN = 28


class PetLibraryPanel(QWidget):
    """书库列表的浮动外壳：圆角底板 + 标题栏 + LibraryView。"""

    open_book = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, st, parent=None):
        super().__init__(parent)
        self.st = st
        self._view: library_view.LibraryView | None = None
        self.setWindowFlags(Qt.WindowType.Tool
                            | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("书库")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 12)
        lay.setSpacing(6)

        # 先建列表，标题栏的「＋」要接到它的 add_requested 上
        self._view = library_view.LibraryView(self.st, self)
        self._view.open_book.connect(self.open_book)

        head = QHBoxLayout()
        head.setContentsMargins(1, 0, 0, 0)
        title = QLabel("书库")
        title.setObjectName("petLibTitle")
        head.addWidget(title)
        head.addStretch(1)

        add = QPushButton("＋")
        add.setObjectName("petLibClose")
        add.setFixedSize(22, 22)
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.setToolTip("添加书籍…")
        add.clicked.connect(lambda: self._view.add_requested.emit())
        head.addWidget(add)

        close = QPushButton("×")
        close.setObjectName("petLibClose")
        close.setFixedSize(22, 22)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip("关闭书库（Esc）")
        close.clicked.connect(self.close)
        head.addWidget(close)
        lay.addLayout(head)

        lay.addWidget(self._view, 1)

        self._apply_style()

    # -------------------------------------------------- 对外

    def view(self) -> library_view.LibraryView:
        return self._view

    def apply_theme(self) -> None:
        self._apply_style()
        self.update()

    def sync_flags(self, on_top: bool) -> None:
        """跟主窗保持同一套窗口标志。

        主窗默认带 WindowStaysOnTopHint（蛙要浮在别的窗口上面），面板如果
        不加，就会出现「蛙在最前、书库沉到别的窗口后面」的割裂感。
        """
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
        if on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        if self.windowFlags() == flags:
            return
        vis = self.isVisible()
        self.setWindowFlags(flags)      # 改标志会隐藏窗口，得自己再 show 回来
        if vis:
            self.show()

    def show_books(self) -> None:
        self._view.show_books()

    def place_beside(self, anchor: QRect) -> None:
        """贴着 anchor 放：默认在左边，左边放不下才翻到右边；底边对齐。

        默认左边是因为奶蛙的落地位置就是屏幕右下角，往右展开直接出屏。
        底边对齐而不是顶边对齐 —— 蛙是贴着屏幕底边站的，面板跟着底边走
        看起来像「从蛙旁边长出来」，顶边对齐则会悬在半空。
        """
        scr = QApplication.screenAt(anchor.center()) or QApplication.primaryScreen()
        avail = scr.availableGeometry()

        h = max(anchor.height(), PANEL_MIN_H)
        h = min(h, max(240, avail.height() - SCREEN_MARGIN * 2))
        self.resize(QSize(PANEL_W, h))

        x = anchor.left() - PANEL_GAP - PANEL_W
        if x < avail.left():
            x = anchor.right() + 1 + PANEL_GAP      # 左边放不下，翻到右边
        y = anchor.bottom() + 1 - h                 # 底边对齐
        y = max(avail.top() + SCREEN_MARGIN,
                min(y, avail.bottom() + 1 - h - SCREEN_MARGIN))
        x = max(avail.left() + SCREEN_MARGIN,
                min(x, avail.right() + 1 - PANEL_W - SCREEN_MARGIN))
        self.move(x, y)

    def follow(self, anchor: QRect) -> None:
        if self.isVisible():
            self.place_beside(anchor)

    # -------------------------------------------------- 绘制

    def _apply_style(self) -> None:
        fg = self.st.get("colors.fg", "#c3cbd6")
        ov = theme.overlay(self.st)
        dim = theme.dim_text(self.st, "mid")
        self.setWindowOpacity(float(self.st.get("window_opacity", 1.0)))
        self.setStyleSheet(f"""
            QWidget {{ color: {fg}; }}
            QLabel#petLibTitle {{
                color: {theme.dim_text(self.st, "dim")};
                font-size: 11px; letter-spacing: .08em;
            }}
            QPushButton#petLibClose {{
                background: transparent; border: none; color: {dim};
                font-size: 15px; padding: 0 0 2px 0; border-radius: 4px;
            }}
            QPushButton#petLibClose:hover {{
                background: rgba({ov},.10); color: {fg};
            }}
        """)

    def paintEvent(self, ev):
        # 书库列表自己是透明底，底板得这里画；否则面板下面是桌面的花背景
        try:
            p = QPainter(self)
            try:
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                r = QRect(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
                path = QPainterPath()
                path.addRoundedRect(r.toRectF(), 10.0, 10.0)
                bg = QColor(self.st.get("colors.bg", "#0d0f12"))
                # 面板上要看清列表，背景不能太透
                bg.setAlpha(max(212, int(self.st.get("bg_alpha", 236))))
                p.fillPath(path, bg)
                p.setPen(QPen(theme.overlay_pen(self.st), 1.0))
                p.drawPath(path)
            finally:
                p.end()
        except Exception:
            pass

    # -------------------------------------------------- 事件

    def keyPressEvent(self, ev):
        # Esc 先关面板，别直接冒到主窗口变成「退出奶读模式」
        if ev.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(ev)

    def closeEvent(self, ev):
        self.closed.emit()
        ev.accept()
