"""一行模式的悬浮工具带（独立浮窗）。

和奶读气泡那排按钮共用 `pet.draw_tool_button` / `TOOL_IDS`，所以两套界面
的按钮是同一家人：同样的圆角底片、同样的线描图标、同样跟着主题走。

v70 起它**不再是主窗里的子控件**，而是跟奶读窗一个路子的**独立浮窗**
（`Qt.Tool` + 无边框 + 真透明），落在一行模式面板**正下方**（留一道小缝），
真正脱离面板：面板只负责显示那一行字，工具带是它下面一颗独立浮着的小条，
鼠标从面板划下去就能碰到它。

位置由主窗在 `_paint_one_line` / `moveEvent` / `resizeEvent` 里调 `place_below`
跟着面板走；淡入淡出、移开留宽限，照搬 pet_window 那套参数，和奶读气泡的
工具条手感一致。
"""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QToolTip, QWidget

import pet
import theme

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

# 浮窗落在面板下方的缝隙（面板贴底时翻到面板上方）
TOOL_GAP_Y = 4

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
    """一行模式面板正下方那颗独立悬浮工具带（绝对不是主窗子控件）。

    `state_fn()` -> `(playing: bool, prog: (pct, 章号, 共几章) | None)`，
    在 paintEvent 里现取：朗读状态、换句、翻章都不用外部记得来刷一次。

    它是独立浮窗：主窗在需要的时候调 `place_below(rect)` 把它摆到面板正下方
    居中处，宽度取自身紧凑宽与面板宽的较小值。平时藏起来（透明 + 隐藏），
    指上窗口才淡入，移开淡出。
    """

    triggered = pyqtSignal(str)

    def __init__(self, st, state_fn, parent=None) -> None:
        # 挂到主窗下（Qt.Tool）只为「主窗藏/最小化时它跟着一起收」和「永远
        # 浮在主窗上面」；它仍是独立顶层窗口，位置由 place_below 手动摆，
        # 不会被主窗裁剪。
        super().__init__(parent)
        self.st = st
        self._state = state_fn
        # 独立浮窗的规矩：不进任务栏、无边框、不抢焦点、真透明、弹出不夺焦
        self.setWindowFlags(Qt.WindowType.Tool
                            | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedHeight(TOOL_BAND)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setEnabled(False)          # 非一行模式彻底不参与、也不显示
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

    # -------------------------------------------------- 布局 / 摆位

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

    def place_below(self, panel_rect: QRect) -> None:
        """摆到面板正下方居中；面板贴屏幕底就翻到面板正上方。"""
        if not self.isEnabled():
            return
        w = max(TOOL_MIN_W, min(TOOL_BAND_W, panel_rect.width()))
        self.setFixedSize(w, TOOL_BAND)
        x = panel_rect.x() + (panel_rect.width() - w) // 2
        screen = QApplication.primaryScreen().availableGeometry()
        y = panel_rect.y() + panel_rect.height() + TOOL_GAP_Y
        if y + TOOL_BAND > screen.bottom():
            y = panel_rect.y() - TOOL_GAP_Y - TOOL_BAND
        self.move(x, y)

    # -------------------------------------------------- 淡入淡出

    def fade_in(self) -> None:
        if not self.isEnabled():
            return
        self._wanted = True
        self._grace.stop()
        if not self.isVisible():
            self.show()
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
                self.hide()             # 彻底透明就收起来，别挡鼠标
        self.update()

    # 自检用：跳过动画直接全亮，方便抓图比对
    def _force_visible(self) -> None:
        self._wanted = True
        self._alpha = 1.0
        self._grace.stop()
        self._tick.stop()
        if not self.isVisible():
            self.show()
        self.update()

    # -------------------------------------------------- 开关

    def activate(self) -> None:
        """进一行模式时调用：允许显示、接收鼠标、参与淡入。"""
        self.setEnabled(True)

    def suspend(self) -> None:
        """退一行模式时调用：立刻收起、关掉动画。"""
        self.setEnabled(False)
        self._wanted = False
        self._alpha = 0.0
        self._tick.stop()
        self._grace.stop()
        self.hide()
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
            # 一条贯通全宽的圆角胶囊底：把按钮们包成一颗浮在面板下方的
            # 独立小条，和正文之间留着 TOOL_GAP_Y 的缝隙，视觉上彻底脱离面板
            cap = QRectF(1, 1, self.width() - 2, self.height() - 2)
            bg = QColor(self.st.get("colors.bg", "#0d0f12"))
            bg.setAlpha(max(1, min(255, int(self.st.get("bg_alpha", 236)))))
            p.setPen(QPen(theme.overlay_pen(self.st, 44), 1.0))
            p.setBrush(bg)
            p.drawRoundedRect(cap, cap.height() / 2, cap.height() / 2)
            # 按钮直接画在胶囊上（只画图标，hover 才提亮），不再各带一块底片
            rects = self.layout_buttons()
            try:
                playing, _prog = self._state()
            except Exception:
                playing = False
            for k, r in rects.items():
                pet.draw_tool_button(p, r, k, self.st,
                                     hover=(k == self._hover), playing=playing,
                                     surface=False)
            # 进度标直接画在胶囊上（不另起底片），画不下就干脆不画
            pr, txt = self.progress_label(rects)
            if not pr.isEmpty():
                fg = QColor(self.st.get("colors.fg", "#c3cbd6"))
                fg.setAlpha(200)
                f = QFont(self.font())
                f.setPixelSize(PROGRESS_PX)
                p.setFont(f)
                p.setPen(fg)
                p.drawText(pr, Qt.AlignmentFlag.AlignCenter, txt)
        except Exception:
            pass                 # 自绘里出异常不能把整个窗口带崩
        finally:
            p.end()

    # -------------------------------------------------- 鼠标

    def enterEvent(self, ev) -> None:
        if self.isEnabled():
            self.fade_in()       # 指上胶囊本身就让它保持亮着
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:
        self._hover = ""
        self._tip_timer.stop()
        QToolTip.hideText()
        if self.isEnabled():
            self.fade_out()      # 离开胶囊：宽限后淡出（指回面板会被面板拦下）
        super().leaveEvent(ev)

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
            # 空白处不接鼠标（独立浮窗，点到空白就是点到桌面/面板之间的缝）
            ev.ignore()

    def mouseReleaseEvent(self, ev) -> None:
        kind, self._press = self._press, ""
        if kind and self.tool_at(ev.position().toPoint()) == kind:
            self.triggered.emit(kind)
            ev.accept()
        else:
            ev.ignore()
