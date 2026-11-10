"""一行模式的悬浮工具带。

和奶读气泡那排按钮共用 `pet.draw_tool_button` / `TOOL_IDS`，所以两套界面
的按钮是同一家人：同样的圆角底片、同样的线描图标、同样跟着主题走。

和 v67 那版不同：这一版**不进布局、不铺满窗口**——它是 Reader 里一个绝对
定位的子控件，宽度自己定（紧凑、居中、不随正文拉伸），平时藏在窗口底部的
透明带里，只有鼠标指上窗口才淡入，移开淡出。这样：

- 「脱离面板」：它是一条浮在正文下方的独立小条，不再把整扇窗口撑成一条
  全宽的工具栏，正文窗口可以收得很窄；
- 「和奶蛙一致」：淡入淡出、移开留 180ms 宽限，都照搬 pet_window 的那套
  参数，鼠标交互手感跟奶读气泡的工具条一样。

宽度算法因此也变了：一行模式窗口的宽度不再需要为工具条留下限，正文多窄
窗口就多窄（按钮有自己最小宽，工具条窄到放不下进度标就只留按钮）。
"""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, pyqtSignal
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
# 工具条自己定的宽度：紧凑，不铺满正文窗口。正文窗口比它还窄就随窗宽。
TOOL_BAND_W = 232

PROGRESS_H = 15
PROGRESS_PX = 11
PROGRESS_PAD_X = 7
PROGRESS_GAP = 6
TIP_DELAY_MS = 420

# 淡入淡出照搬奶读：130ms 左右到位，移开留 180ms 宽限再淡出
FADE_STEP = 0.12          # 每 16ms 一帧，约 130ms 走完
GRACE_MS = 180

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
    """一行模式窗口底部那条悬浮工具带（绝对定位、hover 淡入）。

    `state_fn()` -> `(playing: bool, prog: (pct, 章号, 共几章) | None)`，
    在 paintEvent 里现取：朗读状态、换句、翻章都不用外部记得来刷一次。

    它不进布局：Reader 在 `_paint_one_line` 里调 `relayout()` 把它摆到
    窗口底部居中位置，宽度取 `TOOL_BAND_W` 与窗口宽度的较小值。
    """

    triggered = pyqtSignal(str)

    def __init__(self, st, state_fn, parent=None) -> None:
        super().__init__(parent)
        self.st = st
        self._state = state_fn
        self.setFixedHeight(TOOL_BAND)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 平时藏在透明带里：不画、且鼠标穿透（从工具条上也能拖窗）
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setEnabled(False)          # 非一行模式彻底不参与
        self._alpha = 0.0
        self._wanted = False
        self._hover = ""
        self._press = ""
        self._tip_at = None
        self._tip_timer = QTimer(self)
        self._tip_timer.setSingleShot(True)
        self._tip_timer.timeout.connect(self._show_tip)
        self._tick = QTimer(self)
        self._tick.setInterval(16)
        self._tick.timeout.connect(self._tick_t)
        self._grace = QTimer(self)
        self._grace.setSingleShot(True)
        self._grace.timeout.connect(self._on_grace)

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

    def relayout(self, parent_w: int, parent_h: int) -> None:
        """摆到窗口底部居中，宽度取自身紧凑宽与父窗口宽的较小值。"""
        if not self.isEnabled():
            return
        w = max(TOOL_MIN_W, min(TOOL_BAND_W, parent_w))
        self.setFixedSize(w, TOOL_BAND)
        self.move((parent_w - w) // 2, parent_h - TOOL_BAND)

    # -------------------------------------------------- 淡入淡出

    def fade_in(self) -> None:
        if not self.isEnabled():
            return
        self._wanted = True
        self._grace.stop()
        if self._alpha <= 0.01:
            self.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._start_tick()

    def fade_out(self) -> None:
        if not self.isEnabled():
            return
        self._wanted = False
        self._grace.stop()
        self._grace.start(GRACE_MS)     # 先宽限一下，免得鼠标一划过就闪

    def _on_grace(self) -> None:
        if not self._wanted:
            self._start_tick()

    def _start_tick(self) -> None:
        if not self._tick.isActive():
            self._tick.start()

    def _tick_t(self) -> None:
        target = 1.0 if self._wanted else 0.0
        if self._alpha < target:
            self._alpha = min(target, self._alpha + FADE_STEP)
        elif self._alpha > target:
            self._alpha = max(target, self._alpha - FADE_STEP)
        if abs(self._alpha - target) < 0.01:
            self._alpha = target
            self._tick.stop()
            if self._alpha <= 0.01:
                self.setAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.update()

    # 自检用：跳过动画直接全亮，方便抓图比对
    def _force_visible(self) -> None:
        self._wanted = True
        self._alpha = 1.0
        self._grace.stop()
        self._tick.stop()
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.update()

    # -------------------------------------------------- 开关

    def activate(self) -> None:
        """进一行模式时调用：允许显示、接收鼠标、参与淡入。"""
        self.setEnabled(True)

    def suspend(self) -> None:
        """退一行模式时调用：立刻收起、关掉动画、鼠标穿透。"""
        self.setEnabled(False)
        self._wanted = False
        self._alpha = 0.0
        self._tick.stop()
        self._grace.stop()
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.update()

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
        if self._alpha <= 0.01:
            return                      # 藏好了：不画、且鼠标已穿透
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setOpacity(self._alpha)
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
            # 空白处把事件还给主窗：从工具条上也能拖走这个窗口
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
