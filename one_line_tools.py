"""一行模式的悬浮工具带。

和奶读气泡那排按钮共用 `pet.draw_tool_button` / `TOOL_IDS`，所以两套界面
的按钮是同一家人：同样的圆角底片、同样的线描图标、同样跟着主题走。

做成 Reader 布局里的一行子控件，不是独立顶层窗口——多一个 HWND 就多一处
置顶、焦点、跟随移动要同步（奶读模式就是因为独立浮窗才写了那么多同步代码）。
常驻而不玩 hover 淡入：一行模式把 header/footer 全藏了，按钮得随时能点。
"""
from __future__ import annotations

from PyQt6.QtCore import QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import QToolTip, QWidget

import pet

TOOL_BTN = 22
TOOL_GAP = 6
TOOL_INSET = 4
TOOL_Y_GAP = 2
TOOL_PAD = 4
TOOL_BAND = TOOL_Y_GAP + TOOL_BTN + TOOL_PAD        # 28
# 五颗按钮本身要 3*22+2*6 + 2*22+1*6 + 2*4 = 136，再留一点余量
TOOL_MIN_W = 142

PROGRESS_H = 15
PROGRESS_PX = 11
PROGRESS_PAD_X = 7
PROGRESS_GAP = 6
TIP_DELAY_MS = 420

LEFT = ("prev", "play", "next")
RIGHT = ("hide", "main")
TIPS = {
    "prev": "上一句",
    "play": "开始 / 停止朗读",
    "next": "下一句",
    "hide": "隐藏窗口（托盘唤回）",
    "main": "退出一行模式，回到主面板",
}


class OneLineTools(QWidget):
    """一行模式窗口底部那条工具带。

    `state_fn()` -> `(playing: bool, prog: (pct, 章号, 共几章) | None)`，
    在 paintEvent 里现取：朗读状态、换句、翻章都不用外部记得来刷一次。
    """

    triggered = pyqtSignal(str)

    def __init__(self, st, state_fn, parent=None) -> None:
        super().__init__(parent)
        self.st = st
        self._state = state_fn
        self.setFixedHeight(TOOL_BAND)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._hover = ""
        self._press = ""
        self._tip_at = None
        self._tip_timer = QTimer(self)
        self._tip_timer.setSingleShot(True)
        self._tip_timer.timeout.connect(self._show_tip)

    # -------------------------------------------------- 布局

    def layout_buttons(self) -> dict[str, QRect]:
        """左三贴左沿、右二贴右沿，中间那段留给进度标。"""
        step = TOOL_BTN + TOOL_GAP
        rw = len(RIGHT) * step - TOOL_GAP
        right0 = self.width() - TOOL_INSET - rw
        rects = {}
        for i, k in enumerate(LEFT):
            rects[k] = QRect(TOOL_INSET + i * step, TOOL_Y_GAP,
                             TOOL_BTN, TOOL_BTN)
        for i, k in enumerate(RIGHT):
            rects[k] = QRect(right0 + i * step, TOOL_Y_GAP,
                             TOOL_BTN, TOOL_BTN)
        return rects

    def tool_at(self, pos) -> str:
        for k, r in self.layout_buttons().items():
            if r.contains(pos):
                return k
        return ""

    # -------------------------------------------------- 进度标

    def progress_label(self, rects: dict[str, QRect]) -> tuple[QRect, str]:
        """中间空带够宽才画，文案按宽度三级退化；画不下就干脆不画。"""
        try:
            _playing, prog = self._state()
        except Exception:
            return QRect(), ""
        if not prog:
            return QRect(), ""
        pct, no, total = prog
        lo = rects["next"].right() + PROGRESS_GAP
        hi = rects["hide"].left() - PROGRESS_GAP
        avail = hi - lo
        if avail < 30:
            return QRect(), ""
        f = QFont(self.font())
        f.setPixelSize(PROGRESS_PX)
        fm = QFontMetrics(f)
        for txt in (f"第 {no}/{total} 章 · {pct}%",
                    f"{no}/{total} · {pct}%", f"{pct}%"):
            w = fm.horizontalAdvance(txt) + 2 * PROGRESS_PAD_X
            if w <= avail:
                return (QRect(round(lo + (avail - w) / 2),
                              TOOL_Y_GAP + (TOOL_BTN - PROGRESS_H) // 2,
                              w, PROGRESS_H), txt)
        return QRect(), ""

    # -------------------------------------------------- 绘制

    def paintEvent(self, ev) -> None:
        # 这里不铺整条带的底色：窗口的圆角背景由 Reader 自己画，
        # 工具带只往上面扣按钮，空处保持透明，底下那层才看得见
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            rects = self.layout_buttons()
            try:
                playing, _prog = self._state()
            except Exception:
                playing = False
            for k, r in rects.items():
                pet.draw_tool_button(p, r, k, self.st,
                                     hover=(k == self._hover), playing=playing)
            pr, txt = self.progress_label(rects)
            if not pr.isEmpty():
                bg = QColor(self.st.get("colors.bg", "#0d0f12"))
                bg.setAlpha(180)
                fg = QColor(self.st.get("colors.fg", "#c3cbd6"))
                fg.setAlpha(185)
                f = QFont(self.font())
                f.setPixelSize(PROGRESS_PX)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(bg)
                p.drawRoundedRect(
                    QRectF(pr).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
                p.setFont(f)
                p.setPen(fg)
                p.drawText(pr, Qt.AlignmentFlag.AlignCenter, txt)
        except Exception:
            pass                 # 自绘里出异常不能把整个窗口带崩
        finally:
            p.end()

    # -------------------------------------------------- 鼠标

    def mouseMoveEvent(self, ev) -> None:
        pos = ev.position().toPoint()
        hit = self.tool_at(pos)
        if hit != self._hover:
            self._hover = hit
            self.update()
        self.setCursor(Qt.CursorShape.PointingHandCursor if hit
                       else Qt.CursorShape.ArrowCursor)
        if hit:
            self._tip_at = self.mapToGlobal(pos)
            self._tip_timer.start(TIP_DELAY_MS)
        else:
            self._tip_timer.stop()
            QToolTip.hideText()
        ev.accept()

    def _show_tip(self) -> None:
        if self._hover and self._tip_at is not None:
            QToolTip.showText(self._tip_at, TIPS.get(self._hover, ""), self)

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton:
            ev.ignore()
            return
        self._press = self.tool_at(ev.position().toPoint())
        if self._press:
            ev.accept()
        else:
            # 空白处把事件还给主窗：从工具带上也能拖走这个窗口
            ev.ignore()

    def mouseReleaseEvent(self, ev) -> None:
        kind, self._press = self._press, ""
        if kind and self.tool_at(ev.position().toPoint()) == kind:
            self.triggered.emit(kind)
            ev.accept()
        else:
            ev.ignore()

    def leaveEvent(self, ev) -> None:
        self._hover = ""
        self._tip_timer.stop()
        QToolTip.hideText()
        self.update()
        super().leaveEvent(ev)
