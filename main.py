"""奶蛙阅读器 Interline —— 摸鱼电子书阅读器。

阶段 1：无边框透明置顶窗口 + TXT 阅读闭环。
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time
import traceback
from pathlib import Path

from PyQt6.QtCore import (QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt,
                          QTimer, pyqtSignal)
from PyQt6.QtGui import (QAction, QColor, QCursor, QFont, QFontInfo, QFontMetrics,
                         QIcon, QImage, QKeySequence, QPainter, QPainterPath,
                         QPen,
                         QPixmap, QShortcut, QTextBlockFormat, QTextCharFormat,
                         QTextCursor, qAlpha)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout,
                             QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
                             QSlider, QStackedWidget, QSystemTrayIcon, QTextEdit,
                             QVBoxLayout, QWidget)

import assets
import book_parser
import hotkey
import library_view
import logo
import one_line_tools
import pet
import pet_window
import settings_dialog
import store
import theme
import tts
import speech_controller
import update_dialog
import updater
from theme import SKIN_NAMES, SKIN_ORDER, SKINS

APP_TITLE = "Interline"
# 每次迭代 +1，与服务器 version.json 对应；打包时写进 exe 的产品版本
BUILD_VERSION = 71
SERVER_KEY = "Interline.SingleInstance.v1"
# 朗读中奶蛙隔多久随机来一个小动作（ms）。一句通常 3-6 秒，
# 8-15 秒大约是每两三句一个，不至于一直在动
AMBIENT_GAP_MS = (8000, 15000)
HEADER_H = 36
RESIZE_MARGIN = 14
# 单行模式下能拖动窗口的顶部窄条高度（其余区域留给点字和翻句）
DRAG_ZONE = 8
# 一行模式：窗口固定宽度（不再每句变宽），宽度可在 ONELINE_MIN_W..MAX_W 间
# 用鼠标拖右下角调整；句子比窗口长就出横向细滚动条，方向键左右滚、上下切句
ONELINE_MIN_W = 280
ONELINE_MAX_W = 760
# 正文区 QSS：常规阅读态与一行模式态两轮。一行模式给横向滚动条做细
# （7px 高、圆角手柄、隐藏两端的箭头），常规态没有横向条、竖条按需出现
VIEW_BASE_SS = "QTextEdit{background:transparent;border:none;}"
VIEW_ONELINE_SS = (
    "QTextEdit{background:transparent;border:none;}"
    "QScrollBar:horizontal{height:7px;background:transparent;margin:0 2px;}"
    "QScrollBar::handle:horizontal{"
    "background:rgba(140,150,165,150);border-radius:3px;min-width:28px;}"
    "QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal"
    "{width:0;height:0;}"
    "QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal"
    "{background:transparent;}"
)
# 奶读模式：气泡尾巴尖到蛙头顶的间隙
# 奶读模式下判「这是拖拽不是点击」的位移阈值


# ------------------------------------------------------------------ 单实例

def resolve_font(requested: str) -> str:
    """返回系统里真实存在的字体名。Qt 遇到缺失字体是静默替换的，
    等到缩进量或字形不对才发现就晚了，所以这里显式查一遍。"""
    if QFontInfo(QFont(requested)).family() == requested:
        return requested
    for alt in store.FONT_FALLBACK:
        if QFontInfo(QFont(alt)).family() == alt:
            return alt
    return requested


def acquire_single_instance(key: str):
    """已有实例则把消息发过去并返回 (False, None)，否则返回 (True, server)。"""
    sock = QLocalSocket()
    sock.connectToServer(key)
    if sock.waitForConnected(250):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(250)
        sock.disconnectFromServer()
        return False, None

    QLocalServer.removeServer(key)
    server = QLocalServer()
    if not server.listen(key):
        # 监听失败（残留 socket 等）时宁可不做限制，也别阻止启动
        return True, None
    return True, server


# ------------------------------------------------------------------ 托盘图标

def make_icon() -> QIcon:
    # 按 256 画再让 QIcon 缩小，比直接画 16px 清楚；和 exe 资源同一套画法
    pm = QPixmap(256, 256)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    logo.draw_logo(p, 256)
    p.end()
    return QIcon(pm)


class FrogButton(QPushButton):
    """底栏「奶读模式」：把「蛙」字换成单色蛙头图标。

    图标自绘而不是 setIcon：底栏文字色是 QSS 按主题给的，QIcon 不会跟着变色，
    深浅皮肤总有一边看不清。直接拿配置里的前景色画，hover 时提亮一档。
    """
    def __init__(self, st, parent=None):
        super().__init__("", parent)
        self.st = st
        self.setObjectName("footBtn")

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        try:
            col = QColor(self.st.get("colors.fg", "#c3cbd6"))
            if not self.underMouse():
                col.setAlpha(175)
            pet.draw_frog_head(p, self.rect().adjusted(3, 1, -3, -1), col)
        finally:
            p.end()

    def enterEvent(self, ev):
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self.update()
        super().leaveEvent(ev)


class FootIconButton(QPushButton):
    """底栏小按钮：把文字换成自绘单色图标（跟随主题前景色、hover 提亮）。

    和 FrogButton 一个路子——图标自绘而不是 setIcon，底栏文字色是 QSS 按主题
    给的，QIcon 不跟皮肤变色。图标线条复用 pet._tool_glyph，和奶蛙工具条同款。

    kind：
      "play"    朗读，随 听/停/续 在「播放三角 / 暂停双竖条」间切；
      "font"    字号（一个 A）；
      "oneline" 一行模式（单行文本框 + 一条线）。
    """
    def __init__(self, st, kind, parent=None):
        super().__init__("", parent)
        self.st = st
        self.kind = kind
        self.playing = False        # 朗读按钮：False=播/续，True=暂停
        self.setObjectName("footBtn")

    def set_playing(self, playing: bool) -> None:
        if playing != self.playing:
            self.playing = playing
            self.update()

    # 兼容旧代码里的 setText("听"/"停"/"续")：不显示文字，只换图标
    def setText(self, text: str) -> None:
        self.playing = (text == "停")
        self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        try:
            col = QColor(self.st.get("colors.fg", "#c3cbd6"))
            if not self.underMouse():
                col.setAlpha(175)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r = self.rect()
            side = min(r.width(), r.height())
            box = QRect(r.center().x() - side // 2,
                        r.center().y() - side // 2, side, side)
            pet._tool_glyph(p, box, self.kind, col, self.playing)
        finally:
            p.end()

    def enterEvent(self, ev):
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self.update()
        super().leaveEvent(ev)


# ------------------------------------------------------------------ 主窗口

class FontPanel(QWidget):
    """字号/行距面板。

    用 Qt.Popup 而非 QMenu + QWidgetAction：菜单那套是方角纯色底，
    和主窗自绘的 10px 圆角 + 半透明底放一起会明显不搭。
    Popup 还能点外面自动关，不用自己管关闭逻辑。
    """

    def _apply_style(self):
        """面板样式跟随皮肤：浅色皮肤下白叠加要反过来。"""
        st = self.reader.st
        ov = theme.overlay(st)
        dim = theme.dim_text(st, "mid")
        faint = theme.dim_text(st, "faint")
        fg = st.get("colors.fg", "#cfdae5")
        self.setStyleSheet(f"""
            QLabel {{ color: {dim}; font-size: 12px; }}
            QLabel#val {{ color: {fg}; }}
            QSlider::groove:horizontal {{
                height: 3px; background: rgba({ov},.14); border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{ background: #5f8fa8; border-radius: 2px; }}
            QSlider::handle:horizontal {{
                width: 12px; height: 12px; margin: -5px 0;
                border-radius: 6px; background: #8fa8bb;
            }}
            QSlider::handle:horizontal:hover {{ background: #b6cad8; }}
            QPushButton#preset {{
                background: rgba({ov},.08); border: none; border-radius: 4px;
                color: {dim}; font-size: 12px; padding: 4px 12px;
            }}
            QPushButton#preset:hover {{ background: rgba({ov},.18); color: {fg}; }}
            QPushButton#reset {{
                background: none; border: none; color: {faint};
                font-size: 11.5px; padding: 4px 8px;
            }}
            QPushButton#reset:hover {{ color: {dim}; }}
            QFrame#sep {{ background: rgba({ov},.12); border: none; max-height: 1px; }}
        """)

    def __init__(self, reader):
        super().__init__(reader, Qt.WindowType.Popup)
        self.reader = reader
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(250)
        self._apply_style()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 13, 16, 12)
        lay.setSpacing(7)
        self.size_val = QLabel("", objectName="val")
        self.size_val.setAlignment(Qt.AlignmentFlag.AlignRight
                                   | Qt.AlignmentFlag.AlignVCenter)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(store.PX_MIN, store.PX_MAX)
        self.size_slider.setSingleStep(1)
        self.size_slider.setPageStep(4)
        lay.addLayout(self._head_row("字号", self.size_val))
        lay.addWidget(self.size_slider)

        self.lh_val = QLabel("", objectName="val")
        self.lh_val.setAlignment(Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignVCenter)
        self.lh_slider = QSlider(Qt.Orientation.Horizontal)
        # 用整数承载 line_height×100，避免 0.05 的浮点步进误差。
        # 下限放到 1.00：ProportionalHeight 100% 等于字体自身行高（不是 1em），
        # 雅黑自然行距约 1.3em，所以 1.0 虽然密但不会叠字。
        self.lh_slider.setRange(100, 280)
        self.lh_slider.setSingleStep(5)
        self.lh_slider.setPageStep(10)
        lay.addLayout(self._head_row("行距", self.lh_val))
        lay.addWidget(self.lh_slider)

        sep = QFrame(objectName="sep")
        sep.setFixedHeight(1)
        lay.addSpacing(2)
        lay.addWidget(sep)

        row = QHBoxLayout()
        row.setSpacing(6)
        for text, s, l in (("小", 16, 1.70), ("中", 23, 1.75),
                           ("大", 29, 1.95), ("特大", 36, 2.10)):
            b = QPushButton(text, objectName="preset")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(
                lambda _c=False, s=s, l=l: self.reader.apply_preset(s, l))
            row.addWidget(b)
        row.addStretch(1)
        rst = QPushButton("重置", objectName="reset")
        rst.setCursor(Qt.CursorShape.PointingHandCursor)
        rst.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rst.clicked.connect(self.reader.reset_typo)
        row.addWidget(rst)
        lay.addLayout(row)

        for s in (self.size_slider, self.lh_slider):
            s.sliderPressed.connect(self.reader.begin_typo_drag)
            s.sliderReleased.connect(self.reader.commit_typo)
            s.valueChanged.connect(self._on_changed)

    @staticmethod
    def _head_row(title: str, value: QLabel) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel(title))
        row.addStretch(1)
        value.setFixedWidth(46)
        row.addWidget(value)
        return row

    def _on_changed(self, _v):
        s = self.size_slider.value()
        self.size_val.setText(f"{s} px")
        lh = self.lh_slider.value() / 100.0
        self.lh_val.setText(f"{lh:.2f}")
        self.reader.preview_typo(size=s, line_h=lh)

    def sync(self):
        """从配置回填，避免打开面板时数值和正文不一致。"""
        defaults = store.DEFAULT_CONFIG["font"]
        s = int(self.reader.st.get("font.size", defaults["size"]))
        s = max(store.PX_MIN, min(store.PX_MAX, s))   # 手改过配置也要先夹紧
        lh = float(self.reader.st.get("font.line_height", defaults["line_height"]))
        for w, v in ((self.size_slider, s), (self.lh_slider, round(lh * 100))):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        # 显示滑杆的实际值：setValue 会静默夹紧，直接显示 s 会跟滑杆对不上
        self.size_val.setText(f"{self.size_slider.value()} px")
        self.lh_val.setText(f"{self.lh_slider.value() / 100:.2f}")

    def paintEvent(self, ev):
        try:
            p = QPainter(self)
            try:
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
                path = QPainterPath()
                path.addRoundedRect(r, 10.0, 10.0)
                bg = QColor(self.reader.st.get("colors.bg", "#0d0f12"))
                # 面板上要看清滑杆，背景不准太透
                bg.setAlpha(max(212, int(self.reader.st.get("bg_alpha", 236))))
                p.fillPath(path, bg)
                p.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
                p.drawPath(path)
            finally:
                p.end()
        except Exception:
            pass

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(ev)

    def hideEvent(self, ev):
        self.reader.commit_typo()
        super().hideEvent(ev)
        # Popup 关掉后焦点会飘走，不还给正文的话翻页快捷键就废了
        QTimer.singleShot(0, self._refocus)

    def _refocus(self):
        self.reader.activateWindow()
        self.reader.view.setFocus()


class _ChapterLoader(QObject):
    """PDF 章节在后台解析。

    一本几百页的 PDF，单章解析要 0.5–8 秒，放主线程界面会白掉。
    这里丢到 daemon 线程，结果用信号回主线程（Qt 会自动排队）。
    """

    done = pyqtSignal(int, str)     # (章索引, 错误信息；空串表示成功)

    def load(self, idx: int, chapter) -> None:
        def work():
            try:
                chapter.text
                err = ""
            except Exception as exc:      # noqa: BLE001
                err = str(exc) or exc.__class__.__name__
            self.done.emit(idx, err)

        threading.Thread(target=work, daemon=True).start()


class Reader(QWidget):
    request_show = pyqtSignal()

    def __init__(self, st: store.Store):
        super().__init__()
        self.st = st
        self.book: book_parser.Book | None = None
        self.chapter_idx = 0
        self.mode = "read"  # read | books | toc
        self._drag_origin: QPoint | None = None
        self._resizing = False
        self._font_cache: tuple[str, str] | None = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(700)
        self._save_timer.timeout.connect(self._flush_progress)

        # 字号/行距调节状态
        self.font_panel: FontPanel | None = None
        self._pending_size: int | None = None
        self._pending_line_h: float | None = None
        self._drag_anchor = None
        self._wheel_acc = 0.0
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(90)
        self._preview_timer.timeout.connect(self._do_preview)
        self._wheel_save_timer = QTimer(self)
        self._wheel_save_timer.setSingleShot(True)
        self._wheel_save_timer.setInterval(300)
        self._wheel_save_timer.timeout.connect(self.st.save_config)

        # 听书：双引擎控制器，对外接口和原来的 SpeechReader 一致
        self.speech = speech_controller.SpeechController(self, st)

        # 在线更新：比对 GitHub 最新 Release，比当前版本新就挂 NEW 标
        self._update_info = None
        self._manual_check = False
        self._updater = updater.Updater(self)
        self._updater.checked.connect(self._on_update_checked)
        self._update_timer = QTimer(self)
        try:
            minutes = int(st.get("update.check_minutes", 60) or 60)
        except (TypeError, ValueError):
            minutes = 60
        self._update_timer.setInterval(max(10, minutes) * 60000)
        self._update_timer.timeout.connect(self.check_updates)
        self._tts_sentences: list[str] = []
        self._tts_offsets: list[int] = []   # 每句在章节正文里的起始字符
        self._tts_base = 0                  # 正文在文档里的起点（标题长度 + 换行）
        self._speech_pos = -1               # 上次停下时念到第几句
        self._speech_char = 0               # 那一句里念到第几个字
        self._speech_paused = False         # 是被「停」下来的，不是自然读完

        # 高亮分两层维护：朗读和搜索各记各的，合并后一次性交给 QTextEdit，
        # 否则两边都调 setExtraSelections 会互相清掉
        self._hl_tts = None
        self._hl_search: list = []

        # 搜索
        self._hits: list[dict] = []
        self._hit_idx = -1
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(220)
        self._search_timer.timeout.connect(self._do_search)

        # PDF 章节异步解析
        self._ch_loader = _ChapterLoader(self)
        self._ch_loader.done.connect(self._on_chapter_loaded)
        self._pending_chapter = None

        # 老板键：全局热键，焦点在别的窗口时也能按。
        # 键位注册放在 reload_shortcuts 里，设置改完要能重来一次。
        self._boss_paused = False
        self.boss_key = ""
        self._boss_fallback = False
        self._shortcuts: list = []

        # 一行模式：整个窗口压成一条，只显示当前这句
        self._one_line = False
        self._normal_geo = None
        self._one_line_pos = (0, 0.0)
        self._one_line_idx = 0
        # 进入时定一次的「固定宽度」：之后每句都沿用，点下一句不再变宽；
        # 用户拖拽改宽后这里同步成新宽度，下次翻句保持不变
        self._one_line_w = None

        # 奶读模式：奶蛙是**独立悬浮窗**（像音乐软件的桌面歌词），
        # 主窗照常看书。这里只保留「谁在念、念到第几句」这类业务状态。
        self._pet = False
        self._pet_window: pet_window.PetWindow | None = None
        self._pet_text = ""
        self._pet_idx = 0
        self._pet_ducked = False
        # 笑声盖住朗读时把朗读压低，播完恢复；退出模式时也必须恢复
        self._pet_duck_timer = QTimer(self)
        self._pet_duck_timer.setSingleShot(True)
        self._pet_duck_timer.timeout.connect(self._pet_duck_end)
        # 朗读中奶蛙常驻呼吸，隔一阵子随机来一个小动作（抬手/抬头/跑两步）。
        # 常驻不放大动作：那套抬手放下循环 20fps 一直在动，看着很赶
        self._pet_ambient_timer = QTimer(self)
        self._pet_ambient_timer.setSingleShot(True)
        self._pet_ambient_timer.timeout.connect(self._pet_ambient)
        self.hotkey = None
        try:
            self.hotkey = hotkey.GlobalHotkey(QApplication.instance(), self)
            self.hotkey.activated.connect(self._toggle_boss)
        except Exception:
            self.hotkey = None

        self._init_window()
        self._build_ui()
        self._apply_theme()
        self._restore_window()
        # 启动时还没书，先把用不上的按钮收起来
        self._refresh_actions()

    # -------------------------------------------------- 窗口

    def _init_window(self):
        # 置顶要读配置，否则设置里关掉、重启又生效（之前就是这个 bug）
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self.st.get("always_on_top", True):
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(make_icon())
        self.setMinimumSize(320, 120)
        self.setMouseTracking(True)

    def _restore_window(self):
        w = int(self.st.get("window.w", 560))
        h = int(self.st.get("window.h", 660))
        self.resize(w, h)
        x = self.st.get("window.x")
        y = self.st.get("window.y")
        if isinstance(x, int) and isinstance(y, int):
            # 位置可能来自已拔掉的外接屏，落在屏幕外就回中
            screens = QApplication.screens()
            visible = any(s.availableGeometry().contains(x + 60, y + 30) for s in screens)
            if visible:
                self.move(x, y)
                return
        self._center()

    def _center(self):
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.center().x() - self.width() // 2,
                  scr.center().y() - self.height() // 2)

    def _save_geometry(self):
        # 奶读模式和一行模式的尺寸都不是「正常窗口」，别拿它覆盖 window.*
        if self._pet:
            g = self.geometry()
            self.st.set("pet.x", g.x())
            self.st.set("pet.y", g.y())
            self.st.save_config()
            return
        if self._one_line:
            return
        g = self.geometry()
        self.st.set("window.x", g.x())
        self.st.set("window.y", g.y())
        self.st.set("window.w", g.width())
        self.st.set("window.h", g.height())
        self.st.save_config()

    # -------------------------------------------------- UI

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        # 顶部条：书名 + 章节 + 按钮
        self.header = QWidget(self)
        self.header.setFixedHeight(HEADER_H)
        # 顶栏是拖动窗口的地方，给只手提示；按钮自己会覆盖成手型
        self.header.setCursor(Qt.CursorShape.OpenHandCursor)
        self.header.setMouseTracking(True)
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(14, 0, 8, 0)
        hl.setSpacing(6)

        self.title_label = QLabel("奶蛙阅读器")
        self.title_label.setObjectName("titleLabel")
        hl.addWidget(self.title_label)

        # 有新版本时在书名右边挂个 NEW，点开看更新说明
        self.new_badge = QPushButton("NEW")
        self.new_badge.setObjectName("newBadge")
        self.new_badge.setFixedHeight(16)
        self.new_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_badge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.new_badge.setToolTip("有新版本，点击查看")
        self.new_badge.setVisible(False)
        self.new_badge.clicked.connect(self._open_update)
        hl.addWidget(self.new_badge)
        hl.addStretch(1)

        # 顶栏只管书：打开 / 目录 / 书签 / 书库 / 关闭（书库态换成 ＋ 返回）
        self.btn_open = QPushButton("打开")
        self.btn_toc = QPushButton("目录")
        self.btn_marks = QPushButton("书签")
        self.btn_lib = QPushButton("书库")
        self.btn_add = QPushButton("＋")
        self.btn_back = QPushButton("返回")
        self.btn_close = QPushButton("×")
        for b, tip in ((self.btn_open, "打开文件"),
                       (self.btn_toc, "目录"),
                       (self.btn_marks, "书签列表（Ctrl+D 添加当前位置）"),
                       (self.btn_lib, "书库"),
                       (self.btn_add, "添加书籍"),
                       (self.btn_back, "返回阅读"),
                       (self.btn_close, "关闭")):
            b.setObjectName("hdrBtn")
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            hl.addWidget(b)
        self.btn_close.setObjectName("closeBtn")
        self.btn_add.setObjectName("addBtn")

        # 阅读态显示前四个，书库/目录态只留 ＋ 和 返回
        self._read_btns = (self.btn_open, self.btn_toc, self.btn_marks, self.btn_lib)
        self._lib_btns = (self.btn_add, self.btn_back)
        for b in self._lib_btns:
            b.setVisible(False)

        root.addWidget(self.header)

        # 搜索条：平时折叠，Ctrl+F 展开
        self.search_bar = self._build_search_bar()
        root.addWidget(self.search_bar)
        self.search_bar.setVisible(False)

        self.font_panel = FontPanel(self)

        # 正文
        self.view = QTextEdit(self)
        self.view.setReadOnly(True)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setAcceptRichText(False)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.viewport().setAutoFillBackground(False)
        self.view.setStyleSheet("QTextEdit{background:transparent;border:none;}")
        # 没有撤销 UI，关掉撤销栈：两千次 setBlockFormat 不再堆记录，顺带快一截
        self.view.document().setUndoRedoEnabled(False)

        # 书库/目录和正文共用一块区域，用 stack 切换：
        # 不另开窗口是因为 Qt.Tool 窗口靠隐藏保命，多一个 HWND 就多一处状态要同步
        self.library = library_view.LibraryView(self.st, self)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.library)
        root.addWidget(self.stack, 1)

        # 底部状态条：文字自绘（透明窗口里 QLabel 出过怪东西），
        # 右侧挂阅读调节按钮和视图切换——都在同一行才好对齐
        self._status_text = ""
        self.footer = QWidget(self)
        self.footer.setFixedHeight(24)
        fl = QHBoxLayout(self.footer)
        # 底部留 4px：让按钮中心贴近状态文字的视觉中心，不是机械居中
        fl.setContentsMargins(0, 0, 12, 4)
        fl.setSpacing(4)
        fl.addStretch(1)

        # 朗读与字号放右下角：顶栏只留管书的功能
        self.btn_speak = FootIconButton(self.st, "play")
        self.btn_font = FootIconButton(self.st, "font")
        self.btn_oneline = FootIconButton(self.st, "oneline")
        self.btn_pet = FrogButton(self.st)
        for b, w, tip in ((self.btn_speak, 24, "朗读（右键调语速和音色）"),
                          (self.btn_font, 28, "字号与行距"),
                          (self.btn_oneline, 24, "一行模式（F9）"),
                          (self.btn_pet, 24, "奶读模式（F8）")):
            b.setObjectName("footBtn")
            b.setParent(self.footer)
            b.setFixedSize(w, 20)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            fl.addWidget(b)

        self.view_btn = QPushButton(self.footer)
        self.view_btn.setObjectName("viewToggle")
        self.view_btn.setFixedSize(20, 20)
        self.view_btn.setIconSize(QSize(12, 12))
        self.view_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.view_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.view_btn.setVisible(False)
        self.view_btn.clicked.connect(self._toggle_library_view)
        fl.addWidget(self.view_btn)
        # 一行模式的悬浮工具带：v70 起是**独立浮窗**（不是主窗子控件），
        # 落在一行模式面板正下方。只有进一行模式才激活；指上窗口才淡入，
        # 跟奶读气泡那排按钮的交互手感一致。
        self._one_line_tools = one_line_tools.OneLineTools(
            self.st, self._one_line_state, self)
        self._one_line_tools.triggered.connect(self._on_one_line_tool)
        root.addWidget(self.footer)

        # 信号
        self.btn_open.clicked.connect(self.pick_file)
        self.btn_toc.clicked.connect(self.toggle_toc)
        self.btn_font.clicked.connect(self.show_font_panel)
        self.btn_oneline.clicked.connect(self.toggle_one_line)
        self.btn_pet.clicked.connect(self.toggle_pet)
        self.btn_lib.clicked.connect(lambda: self.set_mode("books"))
        self.btn_add.clicked.connect(self.add_books)
        self.btn_back.clicked.connect(lambda: self.set_mode("read"))
        self.btn_close.clicked.connect(lambda: self._hide_window())
        self.view.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.view.customContextMenuRequested.connect(self._show_view_menu)
        self.request_show.connect(self._do_show)
        # 焦点在正文时 Esc 会被 QTextEdit 自己吃掉，装个过滤器才能统一处理
        self.view.installEventFilter(self)
        self.view.viewport().installEventFilter(self)

        self.library.open_book.connect(self._open_from_library)
        self.library.chapter_chosen.connect(self._goto_chapter)
        self.library.add_requested.connect(self.add_books)
        self.library.status_hint.connect(self._set_hint)
        self.library.view_changed.connect(self._sync_view_btn)
        self.library.mark_chosen.connect(self._goto_mark)
        self.library.mark_removed.connect(self._remove_mark)
        self.btn_marks.clicked.connect(self._open_marks)

        # 快捷键全部读配置，改配置或设置窗口改完都会走这里重建
        self.reload_shortcuts()
        sc_esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.search_input)
        sc_esc.setContext(Qt.ShortcutContext.WidgetShortcut)
        sc_esc.activated.connect(self._close_search)

        # 听书
        self.btn_speak.clicked.connect(self._toggle_speech)
        self.btn_speak.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_speak.customContextMenuRequested.connect(self._speech_menu)
        self.speech.sentence_changed.connect(self._on_sentence)
        self.speech.word_spoken.connect(self._on_word_spoken)
        self.speech.finished.connect(self._on_speech_finished)
        self.speech.state_changed.connect(self._on_speech_state)
        self.speech.notice.connect(lambda s: self._set_hint(s, 4000))

        # 搜索
        self.search_input.textChanged.connect(
            lambda _t: self._search_timer.start())
        self.search_input.returnPressed.connect(lambda: self._step_hit(1))
        self.btn_s_prev.clicked.connect(lambda: self._step_hit(-1))
        self.btn_s_next.clicked.connect(lambda: self._step_hit(1))
        self.btn_s_close.clicked.connect(self._close_search)

    def _build_search_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setFixedHeight(34)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 4, 10, 4)
        lay.setSpacing(6)

        self.search_input = QLineEdit(bar)
        self.search_input.setPlaceholderText("搜索全书…")
        lay.addWidget(self.search_input, 1)

        self.search_count = QLabel("", bar)
        self.search_count.setFixedWidth(56)
        self.search_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.search_count)

        self.btn_s_prev = QPushButton("↑", bar)
        self.btn_s_next = QPushButton("↓", bar)
        self.btn_s_close = QPushButton("×", bar)
        for b in (self.btn_s_prev, self.btn_s_next, self.btn_s_close):
            b.setObjectName("footBtn")
            b.setFixedSize(22, 22)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            lay.addWidget(b)
        self._style_search_bar()
        return bar

    def _style_search_bar(self):
        """搜索条跟随皮肤。放在方法里，切肤时能再刷一次。"""
        ov = theme.overlay(self.st)
        fg = self.st.get("colors.fg", "#d6e0ea")
        dim = theme.dim_text(self.st, "dim")
        self.search_input.setStyleSheet(
            f"QLineEdit{{background:rgba({ov},.09);"
            f"border:1px solid rgba({ov},.13);border-radius:5px;"
            f"color:{fg};padding:2px 8px;font-size:12px;}}"
            f"QLineEdit:focus{{border-color:rgba(95,143,168,.55);}}")
        self.search_count.setStyleSheet(f"color:{dim};font-size:11.5px;")

    def eventFilter(self, obj, ev):
        # 空界面时正文上点一下就是「打开书」，否则这里点了毫无反应
        if (self.book is None and obj is self.view.viewport()
                and ev.type() == QEvent.Type.MouseButtonPress
                and ev.button() == Qt.MouseButton.LeftButton):
            self.pick_file()
            return True
        # 单行模式里 header 藏了，整个窗口只剩 QTextEdit。它会把左键吃掉
        # （用于选文本），事件传不到 Reader，窗口就拖不动了。这里接过来。
        # 但只在顶部窄条上生效——文字区域点了就拖，误触太频繁。
        if self._one_line and obj in (self.view, self.view.viewport()):
            t = ev.type()
            # 只有鼠标事件有 position()；键盘/焦点事件不能走这条路，
            # 否则后面的滚轮和翻句处理会被整个跳过
            if t in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                     QEvent.Type.MouseButtonRelease):
                pos = ev.position()
                if t == QEvent.Type.MouseButtonPress:
                    if (ev.button() == Qt.MouseButton.LeftButton
                            and pos.y() <= DRAG_ZONE):
                        self._drag_origin = (ev.globalPosition().toPoint()
                                             - self.pos())
                        self.setCursor(Qt.CursorShape.ClosedHandCursor)
                        return True
                elif t == QEvent.Type.MouseMove:
                    if self._drag_origin is not None:
                        self.move(ev.globalPosition().toPoint()
                                  - self._drag_origin)
                        return True
                    # 能拖的地方给只手，其余保持文字光标
                    self.setCursor(Qt.CursorShape.OpenHandCursor
                                   if pos.y() <= DRAG_ZONE
                                   else Qt.CursorShape.IBeamCursor)
                elif t == QEvent.Type.MouseButtonRelease:
                    if self._drag_origin is not None:
                        self._drag_origin = None
                        self.setCursor(Qt.CursorShape.OpenHandCursor)
                        # 挪过之后退出单行，应该还在这个位置，别跳回原处
                        if self._normal_geo is not None:
                            g = self.geometry()
                            self._normal_geo = QRect(
                                g.x(), g.y(), self._normal_geo.width(),
                                self._normal_geo.height())
                        return True
        # 一行模式：鼠标指上窗口淡入工具条，移开淡出（和奶读一致）。
        # 工具条 v70 起是独立浮窗，它自己管自己的 enter/leave；这里只负责
        # 面板（正文）这边：指上窗口淡入，离开窗口且没落到工具条上才淡出。
        if self._one_line and obj in (self.view, self.view.viewport()):
            t = ev.type()
            if t == QEvent.Type.Enter:
                self._one_line_tools.fade_in()
            elif t == QEvent.Type.Leave:
                if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
                    self._one_line_tools.fade_out()
        if ev.type() == QEvent.Type.Wheel and obj is self.view.viewport():
            # Ctrl+滚轮调字号。必须拦下来：QTextEdit 自带 zoomInF 会绕过
            # _render 直接改文档字体，导致标题(绝对字号)不跟随、缩进错位。
            if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
                d = ev.angleDelta().y()
                if not d:
                    d = ev.pixelDelta().y() * 8  # 触控板只给像素增量
                if d:
                    self._wheel_acc += d / 120.0
                    step = int(self._wheel_acc)
                    if step:
                        self._wheel_acc -= step
                        self._nudge_size(step)
                return True
            # 一行模式：这一行超宽时，滚轮**先**横向滚它，滚到端点了**再**
            # 翻句——下滚=前进=滚到最右才下一句，上滚=后退=滚到最左才上一句，
            # 和键盘 ←/→ 横滚、↑/↓ 翻句的方向完全一致；内容没超宽（没有横滚条）
            # 时滚轮直接翻句。
            if self._one_line:
                # 正在朗读时滚轮：先暂停朗读（别让自动朗读和用户抢滚动）。
                # 这一下只负责暂停，不再滚/翻句——下一次滚轮才正常导航。
                if self.speech.running:
                    self._pause_speech()
                    return True
                d = ev.angleDelta().y()
                if not d:
                    d = ev.pixelDelta().y() * 8   # 触控板只给像素增量
                if d:
                    self._wheel_acc += d / 120.0
                    step = int(self._wheel_acc)
                    if step:
                        self._wheel_acc -= step
                        fwd = step < 0              # 下滚 = 前进
                        sb = self.view.horizontalScrollBar()
                        # 超宽才有横向滚动条：先滚它，到端点才翻句
                        if sb.maximum() > 0:
                            cur = sb.value()
                            inc = max(40,
                                      int(self.view.viewport().width() * 0.2))
                            if fwd:
                                if cur < sb.maximum():
                                    sb.setValue(min(sb.maximum(), cur + inc))
                                    return True
                            else:
                                if cur > sb.minimum():
                                    sb.setValue(max(sb.minimum(), cur - inc))
                                    return True
                        # 没超宽、或已经滚到端点：滚轮直接翻句
                        self._step_one_line(-step)   # 上滚 = 上一句
                return True
            # 非 Ctrl 必须放行，否则正文滚不动
        if ev.type() == QEvent.Type.KeyPress:
            if self._one_line and self._handle_one_line_key(ev.key()):
                return True
            if ev.key() == Qt.Key.Key_Escape:
                if self._one_line:
                    self._exit_one_line()
                else:
                    self._on_escape()
                return True
        return super().eventFilter(obj, ev)

    def _handle_one_line_key(self, key) -> bool:
        """单行模式的键盘操作。返回 True 表示已经处理掉。

        - ↑ / ↓：切上一句 / 下一句（顺着章节边界翻章）
        - ← / →（以及 PageUp / PageDown）：横向滚动这一行超宽的正文
        - Space：下一句
        - Home / End：跳到本章首句 / 尾句

        方向键在 QTextEdit 里只是移动光标（这里只有一句，动了也看不出来），
        所以必须拦下来自己处理。
        """
        sb = self.view.horizontalScrollBar()
        if key == Qt.Key.Key_Up:
            self._step_one_line(-1)
            return True
        if key == Qt.Key.Key_Down:
            self._step_one_line(1)
            return True
        if key in (Qt.Key.Key_Left, Qt.Key.Key_PageUp,
                   Qt.Key.Key_Right, Qt.Key.Key_PageDown):
            # 内容没超宽就没得滚，直接吞掉按键（别让光标在单行里乱跳）
            if sb.maximum() > 0:
                step = max(40, int(self.view.viewport().width() * 0.2))
                if key in (Qt.Key.Key_Left, Qt.Key.Key_PageUp):
                    sb.setValue(sb.value() - step)
                else:
                    sb.setValue(sb.value() + step)
            return True
        if key == Qt.Key.Key_Space:
            self._step_one_line(1)
            return True
        if key == Qt.Key.Key_Home and self._tts_sentences:
            self._one_line_idx = 0
            self._paint_one_line(self._tts_sentences[0])
            return True
        if key == Qt.Key.Key_End and self._tts_sentences:
            self._one_line_idx = len(self._tts_sentences) - 1
            self._paint_one_line(self._tts_sentences[-1])
            return True
        return False

    def _on_escape(self):
        # Esc 退一层：正常阅读 → 书库/目录 → 藏窗口。
        # 奶读不在这一链上：蛙是独立窗口，Esc 落在它身上就收它自己。
        if self.mode != "read":
            self.set_mode("read")
        else:
            self._hide_window()

    # -------------------------------------------------- 视图切换

    def _refresh_actions(self):
        """按「有没有书」刷新按钮。

        没打开书时，目录/书签/听/单行按了也没反应，直接不显示，
        免得看着像坏了。打开/书库和字号任何时候都在。
        """
        is_read = self.mode == "read"
        has_book = self.book is not None
        self.btn_open.setVisible(is_read)
        self.btn_lib.setVisible(is_read)
        self.btn_font.setVisible(is_read)
        self.btn_toc.setVisible(is_read and has_book)
        self.btn_marks.setVisible(is_read and has_book)
        self.btn_speak.setVisible(is_read and has_book)
        self.btn_oneline.setVisible(is_read and has_book)
        self.btn_pet.setVisible(is_read and has_book)

    def set_mode(self, mode: str):
        if mode == self.mode:
            return
        # 一行模式的窗口只有一句话高，书库/目录塞不进去；Ctrl+L 之类的
        # 快捷键在一行模式下照样能触发，先退回正常窗口再切视图。
        # _exit_one_line 开头就把 _one_line 置 False，不会递归回来
        if self._one_line and mode != "read":
            self._exit_one_line()
        # 切走前先落盘，否则 700ms 防抖窗口内的滚动位置会丢
        self._flush_progress()
        if self.font_panel is not None:
            self.font_panel.hide()
        self.mode = mode
        is_read = mode == "read"
        self.stack.setCurrentIndex(0 if is_read else 1)
        self._refresh_actions()
        self.btn_back.setVisible(not is_read)
        self.btn_add.setVisible(mode == "books")
        self.title_label.setVisible(is_read)
        self.view_btn.setVisible(mode == "books")
        self._sync_view_btn()

        if mode == "books":
            self.library.show_books()
            self._set_hint("")
        elif mode == "toc":
            if self.book:
                self.library.show_toc(self.book.chapters, self.chapter_idx)
                self._set_hint("目录 · Enter 跳转 · Esc 返回")
        elif mode == "marks":
            if self.book and self.book_id:
                self.library.show_marks(self.st.bookmarks(self.book_id),
                                        self.book.chapters)
            self._set_hint("")
        else:
            self.view.setFocus()
            self._update_status()
        self.library.setFocus()

    def _set_hint(self, text: str, timeout: int = 0):
        """底部状态条文字。timeout > 0 时到点恢复默认内容。

        阅读模式下也要能临时借用这条：按 Ctrl+D 加书签得有个反馈，
        到点后由 _update_status 复原成章节进度。
        """
        if self.mode == "read":
            if not text:
                self._update_status()
                return
            self._status_text = text
            self.update(0, max(0, self.height() - 28), self.width(), 28)
            if timeout > 0:
                QTimer.singleShot(timeout, self._update_status)
            return

        if self.mode == "marks":
            n = len(self.st.bookmarks(self.book_id)) if self.book_id else 0
            base = f"{n} 个书签 · Enter 跳转 · Del 删除"
        elif self.mode == "toc":
            base = "目录 · Enter 跳转 · Esc 返回"
        else:
            base = f"共 {len(self.st.books())} 本 · 双击打开 · Del 移除"
        self._status_text = text or base
        self.update(0, max(0, self.height() - 28), self.width(), 28)
        if text and timeout > 0:
            QTimer.singleShot(timeout, lambda: self._set_hint(""))

    def _toggle_library_view(self):
        self.library.toggle_view()

    def _sync_view_btn(self):
        if getattr(self, "view_btn", None) is None:
            return
        self.view_btn.setIcon(self.library.toggle_icon())
        self.view_btn.setToolTip(
            "切换为列表" if self.library.view_mode == "card" else "切换为卡片")

    def _open_from_library(self, path: str):
        self.set_mode("read")
        self.open_path(path)

    def _goto_chapter(self, idx: int):
        self.set_mode("read")
        self.load_chapter(idx, ratio=0.0)

    def add_books(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "添加书籍", str(Path.home()),
            "电子书 (*.txt *.epub *.md *.html *.htm *.pdf);;所有文件 (*.*)")
        if not paths:
            return
        for p in paths:
            # 这里只记路径和文件名，不解析——批量加几十本时逐本解析会卡死界面
            fmt = Path(p).suffix.lower().lstrip(".") or "txt"
            self.st.add_book(p, Path(p).stem, fmt)
        self.library.refresh()
        self._set_hint(f"已添加 {len(paths)} 本")

    def apply_skin(self, name: str):
        """换肤。只改 colors.* 六项，其余沿用白叠加样式。"""
        skin = SKINS.get(name)
        if not skin:
            return
        self.st.set("skin", name)
        for key, val in skin.items():
            self.st.set(f"colors.{key}", val)
        self.st.save_config()
        self._apply_theme()
        self.library.update()
        self.library.list.viewport().update()
        # 正文颜色是渲染时写进字符格式的，不重排不生效
        if self.book and self._pending_chapter is None:
            sb = self.view.verticalScrollBar()
            ratio = 0.0 if sb.maximum() == 0 else sb.value() / sb.maximum()
            self.load_chapter(self.chapter_idx, ratio=ratio)
        self._set_hint(f"皮肤 · {SKIN_NAMES.get(name, name)}", 1400)

    def cycle_skin(self):
        cur = self.st.get("skin", "read")
        i = SKIN_ORDER.index(cur) if cur in SKIN_ORDER else 0
        self.apply_skin(SKIN_ORDER[(i + 1) % len(SKIN_ORDER)])

    def reload_shortcuts(self):
        """按配置重建快捷键。设置窗口改完键位后调用。"""
        for sc in self._shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._shortcuts = []
        for name, fallback, func in (
            ("open_search", "Ctrl+F", self._open_search),
            ("add_bookmark", "Ctrl+D", self._add_bookmark),
            ("toggle_skin", "F10", self.cycle_skin),
            ("toggle_play", "F5", self._toggle_speech),
            ("toggle_oneline", "F9", self.toggle_one_line),
            ("toggle_pet", "F8", self.toggle_pet),
            ("open_toc", "Ctrl+T", self.toggle_toc),
            ("open_library", "Ctrl+L", lambda: self.set_mode("books")),
        ):
            seq = QKeySequence(self.st.get(f"keys.{name}", fallback))
            if not seq.isEmpty():
                sc = QShortcut(seq, self)
                sc.activated.connect(func)
                self._shortcuts.append(sc)

        # 老板键单独注册：想要的键位常被别的软件占着，按备选往下找。
        # 只记运行时实际生效的，不写回配置——否则重注册几次配置就被改乱了
        if self.hotkey is not None:
            want = self.st.get("keys.boss_key", "Ctrl+Alt+D")
            self.boss_key = self.hotkey.register_first(
                [want, "Ctrl+Alt+H", "Ctrl+Shift+Q", "Ctrl+Alt+F12"])
            self._boss_fallback = bool(self.boss_key) and self.boss_key != want
        else:
            self._boss_fallback = False

    def _open_settings(self):
        dlg = settings_dialog.SettingsDialog(self)
        dlg.exec()
        self._set_hint(f"老板键 {self.boss_key}" if self.boss_key else "", 1800)

    def _apply_theme(self):
        bg = self.st.get("colors.bg", "#0d0f12")
        fg = self.st.get("colors.fg", "#c3cbd6")
        heading = self.st.get("colors.heading", "#93a2b2")
        opacity = float(self.st.get("window_opacity", 1.0))
        self.setWindowOpacity(opacity)
        # 弹出面板是独立窗口，不跟随主窗的 window_opacity，得镜像一次
        if self.font_panel is not None:
            self.font_panel.setWindowOpacity(opacity)
            self.font_panel._apply_style()
        # 奶读书库面板同理
        if self._pet_window is not None:
            self._pet_window.apply_theme()
        if getattr(self, "search_input", None) is not None:
            self._style_search_bar()
        # 浅色皮肤要把所有「白叠加」翻成黑叠加，否则这些控件在浅底上会消失
        ov = theme.overlay(self.st)
        dim = theme.dim_text(self.st, "mid")
        faint = theme.dim_text(self.st, "faint")
        menu_bg, menu_border, menu_fg, menu_sel = theme.menu_colors(self.st)
        self.setStyleSheet(f"""
            QWidget {{ color: {fg}; }}
            QLabel#titleLabel {{
                color: {heading}; font-size: 12px; letter-spacing: .06em;
            }}
            QPushButton#newBadge {{
                background: #b04a4a; color: #ffffff; border: none;
                border-radius: 3px; font-size: 9px; font-weight: 600;
                padding: 0 5px 1px 5px; letter-spacing: .08em;
            }}
            QPushButton#newBadge:hover {{ background: #c95a5a; }}
            QPushButton#hdrBtn {{
                background: transparent; border: none; color: {dim};
                font-size: 12px; padding: 3px 7px; border-radius: 4px;
            }}
            QPushButton#hdrBtn:hover {{
                background: rgba({ov},.09); color: {fg};
            }}
            QPushButton#addBtn {{
                background: rgba(95,143,168,.22); border: none; border-radius: 4px;
                color: {fg}; font-size: 14px; padding: 1px 7px 2px 7px;
            }}
            QPushButton#addBtn:hover {{ background: rgba(95,143,168,.40); }}
            QPushButton#viewToggle {{
                background: rgba({ov},.09); border: none; border-radius: 5px;
            }}
            QPushButton#viewToggle:hover {{ background: rgba({ov},.20); }}
            QPushButton#footBtn {{
                background: rgba({ov},.09); border: none; border-radius: 5px;
                color: {dim}; font-size: 11.5px;
            }}
            QPushButton#footBtn:hover {{ background: rgba({ov},.20); color: {fg}; }}
            QPushButton#closeBtn {{
                background: transparent; border: none; color: {faint};
                font-size: 17px; padding: 0 8px; border-radius: 4px;
            }}
            QPushButton#closeBtn:hover {{ background: #7a3336; color: #ffffff; }}
            QScrollBar:vertical {{
                background: transparent; width: 9px; margin: 2px 0;
            }}
            QScrollBar::handle:vertical {{
                background: rgba({ov},.16); border-radius: 4px; min-height: 28px;
            }}
            QScrollBar::handle:vertical:hover {{ background: rgba({ov},.30); }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
            QMenu {{
                background: {menu_bg}; border: 1px solid {menu_border};
                padding: 4px; color: {menu_fg};
            }}
            QMenu::item {{ padding: 5px 22px 5px 16px; border-radius: 3px; }}
            QMenu::item:selected {{ background: {menu_sel}; }}
        """)
        self.update()

    # -------------------------------------------------- 背景自绘

    def paintEvent(self, ev):
        # 绘制里任何异常都不该让整个程序退出（PyQt6 默认会把 slot 异常变成 abort）
        try:
            self._paint()
        except Exception:
            pass

    def _paint(self):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path = QPainterPath()
            path.addRoundedRect(rect, 10.0, 10.0)

            bg = QColor(self.st.get("colors.bg", "#0d0f12"))
            # 夹到 1：alpha=0 时窗口是纯透明的，Windows 不把它算进命中测试，
            # 鼠标会穿到下面的窗口上去（旧配置里可能存了 0）
            bg.setAlpha(max(1, int(self.st.get("bg_alpha", 236))))
            p.fillPath(path, bg)

            p.setPen(QPen(theme.overlay_pen(self.st), 1.0))
            p.drawPath(path)

            # 状态文字是自绘的，footer.hide() 管不着它，单行模式下要显式跳过
            if self._status_text and not self._one_line:
                # 跟随正文字号，但夹在 9–14px：正文调到 10px 时不能比正文还大
                size = int(self.st.get("font.size",
                                       store.DEFAULT_CONFIG["font"]["size"]))
                f = QFont(self._font_family())
                f.setPixelSize(max(9, min(14, round(size * 0.55))))
                p.setFont(f)
                p.setPen(theme.status_text_color(self.st))
                # 用 (x, y, text) 重载，避开 QRect + flags 那套重载在不同版本上的差异
                p.drawText(16, self.height() - 13, self._status_text)
        finally:
            p.end()

    # -------------------------------------------------- 拖动与缩放

    def _edge_hit(self, pos: QPoint) -> bool:
        return (pos.x() >= self.width() - RESIZE_MARGIN
                and pos.y() >= self.height() - RESIZE_MARGIN)

    def moveEvent(self, ev):
        if self._one_line:
            self._one_line_tools.place_below(self.geometry())
        super().moveEvent(ev)

    def resizeEvent(self, ev):
        if self._one_line:
            self._one_line_tools.place_below(self.geometry())
        super().resizeEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        pos = ev.position().toPoint()
        if self._edge_hit(pos):
            self._resizing = True
        else:
            self._drag_origin = ev.globalPosition().toPoint() - self.pos()
            # header 自己被设了 OpenHand，得单独改才能看到"抓住"的效果
            self.header.setCursor(Qt.CursorShape.ClosedHandCursor)
        ev.accept()

    def mouseMoveEvent(self, ev):
        pos = ev.position().toPoint()
        if self._edge_hit(pos):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif not self._resizing:
            self.setCursor(Qt.CursorShape.ArrowCursor)

        if self._resizing:
            gp = ev.globalPosition().toPoint()
            self.resize(max(self.minimumWidth(), gp.x() - self.x()),
                        max(self.minimumHeight(), gp.y() - self.y()))
        elif self._drag_origin is not None:
            self.move(ev.globalPosition().toPoint() - self._drag_origin)
        ev.accept()

    def mouseReleaseEvent(self, ev):
        moved = self._resizing or self._drag_origin is not None
        was_resizing = self._resizing
        self._resizing = False
        self._drag_origin = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.header.setCursor(Qt.CursorShape.OpenHandCursor)
        if moved:
            self._save_geometry()
        # 一行模式拖右下角改了宽度：把新宽度记下来，下次翻句保持固定
        if was_resizing and self._one_line:
            self._one_line_w = self.width()
        # 拖完/缩放完，独立浮窗工具带跟着面板重新落位
        if self._one_line:
            self._one_line_tools.place_below(self.geometry())
        ev.accept()

    def wheelEvent(self, ev):
        super().wheelEvent(ev)

    def _persist(self):
        """存进度 + 存几何。

        关闭按钮走 hide()、托盘退出走 app.quit，两者都不触发 closeEvent，
        所以三处都得显式调这个，否则防抖窗口内的进度会丢。
        """
        self._flush_progress()
        self._save_geometry()

    def _hide_window(self, hide_pet: bool = False):
        """把主窗收起来。

        hide_pet 只给老板键用：那是「一键全消失」，蛙也得跟着躲。
        点主窗的 × 只是收阅读界面，蛙该继续留在桌面上念——朗读本来就没停，
        把蛙一起藏掉等于声音还在、人不见了。
        """
        if self.font_panel is not None:
            self.font_panel.hide()
        if self._pet and self._pet_window is not None and hide_pet:
            self._pet_window.suspend()
            self._pet_duck_end()
        # 工具带是独立浮窗，主窗藏起来时它得跟着收（否则主窗没了还飘着一条）
        band = getattr(self, "_one_line_tools", None)
        if band is not None:
            band.hide()
        self._persist()
        self.hide()

    def _toggle_boss(self):
        """老板键：全局生效，一按就消失，再按回来。

        隐藏时顺手把朗读暂停——否则窗口没了声音还在，耳机没戴就露馅。
        """
        if self.isVisible():
            if self.speech.running and not self.speech.paused:
                self.speech.pause()
                self._boss_paused = True
                self._pet_sync_base()       # 蛙也在藏起来的路上，姿势先换成托腮
            self._hide_window(hide_pet=True)
        else:
            self._do_show()
            self.raise_()
            self.activateWindow()
            if self._boss_paused:
                self.speech.resume()
                self._boss_paused = False
                self._pet_sync_base()

    def closeEvent(self, ev):
        self._persist()
        # 悬浮窗没有父窗口，不显式关掉会留下一个无主的空窗
        if self._pet_window is not None:
            self._pet_window.stop()
            self._pet_window.close()
        if self.hotkey is not None:
            self.hotkey.unregister()
        ev.accept()

    # -------------------------------------------------- 打开与渲染

    def pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开书籍", str(Path.home()),
            "电子书 (*.txt *.epub *.md *.html *.htm *.pdf);;所有文件 (*.*)")
        if path:
            self.open_path(path)

    def open_path(self, path: str, quiet: bool = False):
        """打开一本书。quiet=True 时解析失败不弹窗（启动自动恢复用）。"""
        try:
            book = book_parser.load_book(path)
        except Exception as e:
            if not quiet:
                QMessageBox.warning(self, "打不开", f"{Path(path).name}\n\n{e}")
            return
        self.set_book(book)

    def _restore_last_book(self):
        """启动时接着上次读的地方。

        文件被删或者换了盘符就安静停在空状态：这时候用户什么都没做，
        弹一个错误框只会让人莫名其妙。
        """
        if self.book is not None:      # 命令行已经指定了文件，优先
            return
        bid = self.st.last_book()
        if not bid:
            return
        rec = self.st.find_book(bid)
        if not rec:
            return
        path = rec.get("path") or ""
        if not path or not os.path.exists(path):
            return
        self.open_path(path, quiet=True)

    def set_book(self, book: book_parser.Book):
        if not book or not getattr(book, "chapters", None):
            QMessageBox.warning(self, "打不开", "这本书没有可读的正文。")
            return
        self.book = book
        rec = self.st.add_book(book.path, book.title, book.format)
        self.st.touch_book(rec["id"], book.path)
        # 记总章数：书库列表要用它算全书进度
        self.st.set_chapters(rec["id"], len(book.chapters))
        self.book_id = rec["id"]

        prog = rec.get("progress") or {}
        idx = int(prog.get("chapter", 0) or 0)
        idx = max(0, min(idx, len(book.chapters) - 1))
        self.load_chapter(idx, ratio=float(prog.get("ratio", 0.0) or 0.0))
        self._refresh_actions()
        self.st.set_last_book(self.book_id)

    def load_chapter(self, idx: int, ratio: float = 0.0, at_offset: int | None = None):
        if not self.book or not self.book.chapters:
            return
        # 换章必须停朗读，否则会继续念上一章的内容
        if self.speech.running:
            self._stop_speech()
        # 暂停状态切章也要清：否则点「续」会拿上一章的句子号在这章里念
        self._speech_paused = False
        self._speech_pos = -1
        self._speech_char = 0
        idx = max(0, min(idx, len(self.book.chapters) - 1))
        ch = self.book.chapters[idx]
        self.chapter_idx = idx

        # PDF 章节按需解析：丢到后台，先给个占位，别让界面卡住
        if getattr(ch, "loaded", True) is False:
            self._pending_chapter = (idx, ratio, at_offset)
            pages = len(ch._pages) if hasattr(ch, "_pages") else 0
            self.view.setPlainText(f"正在解析：{ch.title}…")
            self.view.setExtraSelections([])
            self._hl_tts = None
            self._status_text = f"解析中 · {ch.title}" + (
                f" · 约 {max(1, round(pages * 0.3))} 秒" if pages else "")
            self.update(0, max(0, self.height() - 28), self.width(), 28)
            self._ch_loader.load(idx, ch)
            return

        self._render(ch)
        self._build_tts_index(ch)
        # 手动换章后搜索高亮会失效（setPlainText 清空），补画一次
        if self._hits and self.search_bar.isVisible():
            self._paint_search_hits()

        if at_offset is not None:
            self._restore_scroll(lambda: self._scroll_to_char(at_offset))
        else:
            self._restore_scroll(lambda: self._scroll_to_ratio(ratio))

        self._update_status()
        self._schedule_save()

        # 奶读模式下滚到章边界、或启动时先恢复书再恢复奶蛙，都会走到这里：
        # 不刷新的话气泡会一直挂着上一章最后一句
        if self._pet:
            self._pet_idx = max(0, min(self._pet_idx, len(self._tts_sentences) - 1))
            self._pet_refresh()

    def _font_family(self) -> str:
        req = self.st.get("font.family", store.UI_FONT)
        if self._font_cache and self._font_cache[0] == req:
            return self._font_cache[1]
        real = resolve_font(req)
        self._font_cache = (req, real)
        return real

    def _block_formats(self, size: int, line_h: float, fm: QFontMetrics,
                       body_font: QFont | None = None):
        """构造一套块/字符格式。

        全量重排和「只改排版」的快路径共用这里，否则两套参数迟早漂移
        —— 默认行距 1.75 / 1.9 那处不一致就是这么来的。
        """
        heading_color = self.st.get("colors.heading", "#a8bccd")
        fg_color = self.st.get("colors.fg", "#c3cbd6")

        lh = int(round(line_h * 100))
        lh_type = QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
        # 段距按「实际行高」推算，而不是写死像素：
        # 行距一调大，固定段距就会被行间空白淹掉，段落又糊成一片。
        line_px = fm.lineSpacing() * line_h
        gap = max(10, round(line_px * 0.42))

        fmt_body = QTextBlockFormat()
        fmt_body.setLineHeight(lh, lh_type)
        fmt_body.setBottomMargin(gap)
        fmt_body.setLeftMargin(0)
        fmt_body.setRightMargin(0)
        # 中文排版：首行缩进两个字，按实际字宽量出来
        fmt_body.setTextIndent(max(0, fm.horizontalAdvance("汉字")))

        fmt_head = QTextBlockFormat()
        fmt_head.setLineHeight(lh, lh_type)
        fmt_head.setTopMargin(6)
        fmt_head.setBottomMargin(gap + 6)
        fmt_head.setTextIndent(0)

        char_head = QTextCharFormat()
        char_head.setForeground(QColor(heading_color))
        # 标题按常数偏移而不是百分比：px 下 23*1.02 取整回 23 就和正文一样大了。
        # 偏移让它在小字号时相对明显（+30%）、大字号时收敛（+10%）。
        char_head.setFont(self._make_font(size + max(3, round(size * 0.10)),
                                          QFont.Weight.Bold.value, clamp=False))

        char_body = QTextCharFormat()
        char_body.setForeground(QColor(fg_color))
        # 正文字号显式写进字符格式。以前只靠 document 默认字体，
        # 一旦字符格式被别的路径污染（一行模式把唯一那块当标题格式化过），
        # 字号滑杆只 merge 块格式，就再也改不回来。
        if body_font is not None:
            char_body.setFont(body_font)

        return fmt_body, fmt_head, char_head, char_body

    def _make_font(self, px: int, weight: int = 400, clamp: bool = True) -> QFont:
        """按像素字号造字体。

        不能写 QFont(family, px, weight)：那个第二参是磅值，
        和 setPixelSize 混用会互相清掉（后设者胜）。
        标题要按正文加偏移，可能超出正文的上下限，所以留了 clamp 开关。
        """
        f = QFont(self._font_family())
        f.setWeight(QFont.Weight(weight))
        if clamp:
            px = max(store.PX_MIN, min(store.PX_MAX, int(px)))
        f.setPixelSize(max(1, int(px)))
        return f

    def _chapter_parts(self, ch) -> tuple[str, str]:
        """返回 (标题, 正文)。

        空正文给个占位，否则 doc.begin() 拿到无效块、格式全套不上会白屏。
        朗读索引也走这里——两边必须用同一份文本，字符偏移才对得上。
        """
        title = ch.title or " "
        body = ch.text if ch.text.strip() else "（本章没有正文）"
        return title, body

    def _render(self, ch: book_parser.Chapter):
        font_cfg = self.st.get("font", {})
        defaults = store.DEFAULT_CONFIG["font"]
        size = int(font_cfg.get("size", defaults["size"]))
        line_h = float(font_cfg.get("line_height", defaults["line_height"]))
        weight = int(font_cfg.get("weight", 400))

        font = self._make_font(size, weight)
        doc = self.view.document()
        doc.setDefaultFont(font)
        # 一行模式会把当前字符格式换成单句用的字体。不清掉的话，
        # 下面 setPlainText 会拿它插入整章，正文全部跟着变大。
        self.view.setFont(font)
        self.view.setCurrentCharFormat(QTextCharFormat())

        title, body = self._chapter_parts(ch)
        self.view.setPlainText(title + "\n" + body)

        fmt_body, fmt_head, char_head, char_body = self._block_formats(
            size, line_h, QFontMetrics(font), font)

        block = doc.begin()
        first = True
        while block.isValid():
            cur = QTextCursor(block)
            if first:
                cur.setBlockFormat(fmt_head)
                cur.select(QTextCursor.SelectionType.BlockUnderCursor)
                cur.mergeCharFormat(char_head)
                first = False
            else:
                cur.setBlockFormat(fmt_body)
                cur.select(QTextCursor.SelectionType.BlockUnderCursor)
                cur.mergeCharFormat(char_body)
            block = block.next()

        # 正文左右留白，靠 viewport margin 更稳（改宽度时不用重排 block）
        # 底部留 10px，否则大字时最后一行会贴着状态栏
        self.view.setViewportMargins(18, 6, 14, 10)

    # -------------------------------------------------- 滚动定位

    def _restore_scroll(self, fn):
        """延迟应用滚动位置。

        setPlainText 之后 Qt 还没算完布局，此刻 verticalScrollBar().maximum()
        仍是 0，直接 setValue 会永远停在顶部 —— 之前「重开回到原位」失效就是这个原因。
        """
        def attempt(retries: int = 0):
            if self.view.verticalScrollBar().maximum() == 0 and retries < 3:
                QTimer.singleShot(40, lambda: attempt(retries + 1))
                return
            fn()

        QTimer.singleShot(0, attempt)

    def _scroll_to_ratio(self, ratio: float):
        sb = self.view.verticalScrollBar()
        sb.setValue(int(sb.maximum() * max(0.0, min(1.0, ratio))))

    def _scroll_to_char(self, pos: int):
        """按字符偏移定位。搜索 / 书签 / 高亮都复用它。"""
        doc = self.view.document()
        pos = max(0, min(int(pos), doc.characterCount() - 1))
        block = doc.findBlock(pos)
        if not block.isValid():
            return
        # 块在文档中的纵向位置 + 行在块内的偏移
        y = doc.documentLayout().blockBoundingRect(block).top()
        lay = block.layout()
        if lay is not None and lay.lineCount():
            line = lay.lineForTextPosition(pos - block.position())
            if line.isValid():
                y += line.y()
        sb = self.view.verticalScrollBar()
        sb.setValue(int(max(0.0, y - self.view.viewport().height() * 0.32)))

    def _update_status(self):
        if not self.book:
            # 没打开书时也要把状态条擦干净，否则从书库返回会残留
            #「共 N 本 · 双击打开」那行提示
            self._status_text = ""
            self.title_label.setText(APP_TITLE)
            self.update(0, max(0, self.height() - 28), self.width(), 28)
            return
        ch = self.book.chapters[self.chapter_idx]
        total = len(self.book.chapters)
        sb = self.view.verticalScrollBar()
        pct = 0 if sb.maximum() == 0 else int(sb.value() * 100 / sb.maximum())
        self._status_text = (
            f"{self.chapter_idx + 1}/{total}   {ch.title[:26]}   {pct}%")
        self.title_label.setText(self.book.title[:24])
        # 只重绘底部一条，滚动时不带着整窗背景一起刷
        self.update(0, max(0, self.height() - 28), self.width(), 28)

    # -------------------------------------------------- 进度

    def _on_scroll(self, _v):
        self._update_status()
        self._schedule_save()

    def _schedule_save(self):
        if self.book:
            self._save_timer.start()

    def _flush_progress(self):
        if not self.book or not getattr(self, "book_id", None):
            return
        sb = self.view.verticalScrollBar()
        ratio = 0.0 if sb.maximum() == 0 else sb.value() / sb.maximum()
        self.st.set_progress(self.book_id, self.chapter_idx, sb.value(), ratio)
        # 正在朗读时同时记句子索引：像素位置会因字号变化漂移，句子不会
        if self.speech.running and self.speech.current_index >= 0:
            self.st.set_tts_progress(self.book_id, self.chapter_idx,
                                     self.speech.current_index)

    # -------------------------------------------------- 章节与翻页

    def page_down(self):
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.value() + int(sb.pageStep() * 0.92))

    def page_up(self):
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.value() - int(sb.pageStep() * 0.92))

    def next_chapter(self):
        if self.book and self.chapter_idx + 1 < len(self.book.chapters):
            self.load_chapter(self.chapter_idx + 1, ratio=0.0)

    def prev_chapter(self):
        if self.book and self.chapter_idx > 0:
            self.load_chapter(self.chapter_idx - 1, ratio=1.0)

    # -------------------------------------------------- 目录

    def toggle_toc(self):
        # 目录改用列表控件：上千章时 QMenu 会直接溢出屏幕，而且 QSS 的
        # max-height 对 QMenu 根本不生效
        if not self.book:
            return
        self.set_mode("read" if self.mode == "toc" else "toc")

    # -------------------------------------------------- 字号行距

    def show_font_panel(self):
        if self.font_panel.isVisible():
            self.font_panel.hide()
            return
        self.font_panel.sync()
        self.font_panel.adjustSize()
        # 按钮在底部，面板要往上弹，否则会超出屏幕
        pos = self.btn_font.mapToGlobal(
            QPoint(self.btn_font.width() - self.font_panel.width() + 4,
                   -self.font_panel.height() - 6))
        self.font_panel.move(pos)
        self.font_panel.show()

    def begin_typo_drag(self):
        """拖动开始时记一次锚点。

        拖动过程中不能再取，否则每次预览都基于上一帧的位置，会累积漂移。
        """
        self._drag_anchor = self._anchor_top()

    def preview_typo(self, size=None, line_h=None):
        """滑杆移动：先只记参数，交给去抖定时器统一应用。"""
        if size is not None:
            self._pending_size = int(size)
        if line_h is not None:
            self._pending_line_h = float(line_h)
        self._preview_timer.start()

    def _do_preview(self):
        if self._drag_anchor is None:
            self._drag_anchor = self._anchor_top()
        self._apply_typo(size=self._pending_size,
                         line_h=self._pending_line_h,
                         anchor=self._drag_anchor, persist=False)

    def commit_typo(self):
        """松手 / 关面板：立即应用并落盘。"""
        self._preview_timer.stop()
        if self._pending_size is not None or self._pending_line_h is not None:
            self._apply_typo(size=self._pending_size,
                             line_h=self._pending_line_h,
                             anchor=self._drag_anchor, persist=True)
            self._pending_size = None
            self._pending_line_h = None
        self._drag_anchor = None

    def apply_preset(self, size: int, line_h: float):
        self._apply_typo(size=size, line_h=line_h,
                         anchor=self._anchor_top(), persist=True)
        if self.font_panel is not None:
            self.font_panel.sync()

    def reset_typo(self):
        d = store.DEFAULT_CONFIG["font"]
        self.apply_preset(d["size"], d["line_height"])

    def _nudge_size(self, step: int):
        """Ctrl+滚轮改字号，和滑杆走同一个落点。"""
        if not step:
            return
        cur = int(self.st.get("font.size", store.DEFAULT_CONFIG["font"]["size"]))
        size = max(store.PX_MIN, min(store.PX_MAX, cur + step))
        if size == cur:
            return
        self._apply_typo(size=size, anchor=self._anchor_top(), persist=False)
        # 滚轮没有 release 信号，靠尾部去抖落盘
        self._wheel_save_timer.start()
        if self.font_panel is not None and self.font_panel.isVisible():
            self.font_panel.sync()

    def _apply_typo(self, size=None, line_h=None, persist=False, anchor=None):
        """只改排版参数，不重排文本。

        拖滑杆时文本内容没变，没必要 setPlainText 再遍历上千个 block，
        一次 mergeBlockFormat 就够，否则每帧 150ms 以上根本没法实时预览。
        """
        size_arg = size
        if size is not None:
            self.st.set("font.size", int(size))
        if line_h is not None:
            self.st.set("font.line_height", round(float(line_h), 2))

        if not self.book:
            if persist:
                self.st.save_config()
            return

        defaults = store.DEFAULT_CONFIG["font"]
        size = int(self.st.get("font.size", defaults["size"]))
        line_h = float(self.st.get("font.line_height", defaults["line_height"]))
        weight = int(self.st.get("font.weight", 400))

        font = self._make_font(size, weight)
        doc = self.view.document()
        doc.setDefaultFont(font)
        self.view.setFont(font)

        fmt_body, fmt_head, char_head, char_body = self._block_formats(
            size, line_h, QFontMetrics(font), font)

        # 一行模式下文档里只有当前这一句，不存在「首块是章节标题」。
        # 走下面那个标题分支会把这一句放大加粗，还把当前字符格式带脏，
        # 退出后整章都继承成大字、且字号滑杆再也改不动。
        if self._one_line:
            self._paint_one_line()
            if persist:
                self.st.save_config()
            return

        # 一次合并所有块的块级格式（不碰字符格式，正文颜色保留）
        cur = QTextCursor(doc)
        cur.select(QTextCursor.SelectionType.Document)
        cur.mergeBlockFormat(fmt_body)
        # 字号真的变了才刷新字符格式：行距调整不需要，也能少一次全文遍历
        if size_arg is not None:
            cur.mergeCharFormat(char_body)

        # 首块是章节标题：整体覆盖回标题格式，并把绝对字号补回去
        first = QTextCursor(doc.firstBlock())
        first.setBlockFormat(fmt_head)
        first.select(QTextCursor.SelectionType.BlockUnderCursor)
        first.mergeCharFormat(char_head)

        if anchor is not None:
            self._restore_anchor(anchor)
        if persist:
            self.st.save_config()

    # -------------------------------------------------- 听书

    def _build_tts_index(self, ch):
        """切句并记下每句在正文里的起始位置。

        文档里是「标题 + 换行 + 正文」，所以句子在文档中的位置要加上 _tts_base。
        """
        title, body = self._chapter_parts(ch)
        self._tts_base = len(title) + 1
        self._tts_sentences = []
        self._tts_offsets = []
        pos = 0
        for s in book_parser.split_sentences(body):
            i = body.find(s, pos)
            if i < 0:
                i = pos
            self._tts_sentences.append(s)
            self._tts_offsets.append(i)
            pos = i + len(s)

    def _sentence_at(self, char_pos: int) -> int:
        """把文档里的字符位置映射到句子索引。"""
        if not self._tts_offsets:
            return 0
        rel = char_pos - self._tts_base
        idx = 0
        for i, off in enumerate(self._tts_offsets):
            if off <= rel:
                idx = i
            else:
                break
        return idx

    def _visible_sentence_index(self) -> int:
        """视口顶部那句的索引。

        朗读起点跟着视口走：用户滚到哪就从哪开始念，
        不必先滚回去找。取视口顶部中央的字符再映射到句子。
        """
        vp = self.view.viewport()
        pos = self.view.cursorForPosition(QPoint(vp.width() // 2, 6)).position()
        return self._sentence_at(pos)

    def _speak_from_selection(self) -> None:
        cur = self.view.textCursor()
        if not cur.hasSelection():
            return
        pos = min(cur.selectionStart(), cur.selectionEnd())
        idx = self._sentence_at(pos)
        # 先清选区再读：系统的选中色画在 ExtraSelection 之上，
        # 不清掉的话朗读高亮会被灰色选中块盖住，看着像没生效
        cur.clearSelection()
        cur.setPosition(pos)
        self.view.setTextCursor(cur)
        self._start_speech(idx)


    def _clear_selection(self) -> None:
        cur = self.view.textCursor()
        if cur.hasSelection():
            cur.clearSelection()
            self.view.setTextCursor(cur)

    def _pause_speech(self) -> bool:
        """「停」：记住念到哪个词，然后把引擎真正停掉。

        正在念才动，返回是否真的停过。不用 pause()/resume()：实测 winrt 上
        resume() 完全不出声（状态会变回 Speaking，但没有声音、也没有逐词回调），
        连 1 秒的短暂停都恢复不了。只能停掉、之后从那个词重新开口。
        """
        if not self.speech.running:
            return False
        self._speech_pos, self._speech_char = self.speech.stop_at()
        self._speech_paused = True
        self.speech.stop()
        self._clear_highlight()
        self.btn_speak.setText("续")
        # 引擎停掉时不一定会报状态回来，这里自己推一次：
        # 奶蛙要切到「托腮等你」的姿势
        self._pet_sync_base()
        return True

    def _toggle_speech(self):
        if not self.book or not self._tts_sentences:
            return
        if self.speech.running:
            self._pause_speech()
        else:
            if self._speech_paused and self._speech_pos >= 0:
                # 「续」：从停下的那个词接上。先换个新引擎——
                # 挂久了旧实例连 say 都是哑的，重建只要几十毫秒
                self.speech.rebuild()
                self._start_speech(self._speech_pos, self._speech_char)
            else:
                self._start_speech()
            self._speech_paused = False


    def _start_speech(self, from_idx: int | None = None, from_char: int = 0):
        if not self.book or not self._tts_sentences:
            return
        if from_idx is None:
            # 一行模式下按视口算起点同样是 0，会从章首念起；
            # 应该从当前显示的那句开始
            from_idx = (self._one_line_idx if self._one_line
                        else self._visible_sentence_index())
        self._clear_selection()
        # 每次点听都重读音色/语速/音量：设置里改完立即生效，
        # 在线音色连不上时控制器会在这次朗读里自动切回系统语音
        self.speech.apply_config()
        self.speech.start(self._tts_sentences, from_idx, from_char)
        self._speech_pos = from_idx
        self._speech_char = from_char
        self.btn_speak.setText("停")
        # 引擎报 speaking 要几百毫秒，这期间奶蛙还挂着上一个姿势。
        # 自己推一次，顺手把「朗读中随机小动作」的定时器启动起来
        self._pet_sync_base()
        # 不等 sayingWord：引擎报第一个词要几百毫秒，
        # 期间旧高亮还挂在上一句上。先把目标句点亮，点击即有反馈。
        # 一行模式下没有排版坐标，跳过——highlights 会用到 _tts_offsets。
        if 0 <= from_idx < len(self._tts_sentences) and not self._one_line:
            self._highlight_sentence(from_idx)
            self._scroll_to_char(self._tts_base + self._tts_offsets[from_idx])

    def _stop_speech(self):
        self.speech.stop()
        self._clear_highlight()
        self._speech_pos = -1
        self._speech_char = 0
        self._speech_paused = False
        self.btn_speak.setText("听")
        self._pet_sync_base()

    def _on_sentence(self, idx: int):
        # 一行模式下没有排版坐标，整块重排：文本换了，宽度也要跟着变
        if self._one_line:
            if 0 <= idx < len(self._tts_sentences):
                self._one_line_idx = idx
                self._paint_one_line(self._tts_sentences[idx])
            return
        # 奶读模式只换气泡文字，**不 return**：下面照常记进度、滚动隐藏的正文，
        # 这样退出时位置还是对的
        if self._pet:
            self._pet_on_sentence(idx)
        self._highlight_sentence(idx)
        if 0 <= idx < len(self._tts_offsets):
            self._scroll_to_char(self._tts_base + self._tts_offsets[idx])
        # 朗读进度单独记一份：滚动位置是像素、会因字号变化漂移，
        # 句子索引重开就能精确回到原处
        if self.book_id:
            self.st.set_tts_progress(self.book_id, self.chapter_idx, idx)

    def _on_word_spoken(self, idx: int, start: int, length: int):
        """引擎念到某个词：一行模式下把横向滚动条跟到那个字上。

        只在一行模式、且念的正是屏幕上这一句时滚——别的场景（正文阅读）
        下面没有横向滚动条，滚了也没意义。
        """
        if self._one_line and idx == self._one_line_idx:
            self._scroll_one_line_to_pos(start)

    def _scroll_one_line_to_pos(self, pos: int):
        """把一行模式那条横向细滚动条滚到「当前朗读的字」附近。

        做法：算出这个字当前在视口里的横向位置，只有它要跑出舒适区
        （左 12% ~ 右 72%）时才滚，且把它滚到离左边约 35% 处。这样
        一句读下来是「跟着走、分段挪」，而不是每念一个字都抖一下。
        """
        if not self._one_line:
            return
        sb = self.view.horizontalScrollBar()
        if sb.maximum() <= 0:              # 没超宽、滚不动
            return
        vp = self.view.viewport()
        vw = vp.width()
        if vw <= 0:
            return
        doc = self.view.document()
        n = doc.characterCount()
        if n <= 0:
            return
        cur = QTextCursor(doc)
        cur.setPosition(max(0, min(pos, n - 1)))
        r = self.view.cursorRect(cur)      # 视口坐标（已含当前滚动量）
        lo = int(vw * 0.12)
        hi = int(vw * 0.72)
        if lo <= r.left() <= hi:           # 还在舒适区，别动，免得抖
            return
        doc_x = sb.value() + r.left()      # 该字在整行里的像素位置
        target = doc_x - int(vw * 0.35)    # 滚到离左边约 35% 的位置
        sb.setValue(max(sb.minimum(), min(sb.maximum(), target)))

    def _apply_highlights(self):
        """把各层高亮合并后一次性提交给 QTextEdit。"""
        sels = []
        if self._hl_tts is not None:
            sels.append(self._hl_tts)
        sels.extend(self._hl_search)
        self.view.setExtraSelections(sels)

    def _highlight_sentence(self, idx: int):
        if not (0 <= idx < len(self._tts_sentences)):
            return
        doc = self.view.document()
        last = doc.characterCount() - 1
        start = min(self._tts_base + self._tts_offsets[idx], last)
        end = min(start + len(self._tts_sentences[idx]), last)
        if end <= start:
            return
        cur = QTextCursor(doc)
        cur.setPosition(start)
        cur.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        sel = QTextEdit.ExtraSelection()
        sel.cursor = cur
        sel.format.setBackground(
            QColor(self.st.get("colors.tts_hl", "#1d3947")))
        self._hl_tts = sel
        self._apply_highlights()

    def _clear_highlight(self):
        self._hl_tts = None
        self._apply_highlights()

    def _on_speech_state(self, state: str):
        if state == "speaking":
            self.btn_speak.setText("停")
        elif state == "paused":
            self.btn_speak.setText("续")
        else:
            self.btn_speak.setText("听")
        self._pet_sync_base()

    def _on_speech_finished(self):
        self.btn_speak.setText("听")
        self._clear_highlight()
        # 读完一章：奶蛙抬头等你翻页
        if self._pet and self._pet_window is not None:
            self._pet_window.review()

    def _speech_menu(self, pos):
        if not self.book:
            return
        menu = QMenu(self)
        cur_rate = float(self.st.get("tts.rate", 0.0))
        for label, rate in (("0.8 倍", -0.30), ("1.0 倍", 0.0),
                            ("1.25 倍", 0.25), ("1.5 倍", 0.5), ("2.0 倍", 1.0)):
            act = menu.addAction(f"语速 {label}")
            act.setCheckable(True)
            act.setChecked(abs(cur_rate - rate) < 0.01)
            act.triggered.connect(lambda _c=False, r=rate: self._set_rate(r))
        menu.addSeparator()
        # 音色放子菜单，系统语音和在线音色同列，和设置窗口共用一份清单
        voice_menu = menu.addMenu("音色")
        cur_voice = self.st.get("tts.voice_id", "") or self.speech.current_voice()
        last_group = None
        for label, vid, group in self.speech.voice_choices():
            if group != last_group:
                if last_group is not None:
                    voice_menu.addSeparator()
                title = voice_menu.addAction(
                    "系统语音" if group == "sys" else "在线音色")
                title.setEnabled(False)
                last_group = group
            text = label if group == "sys" else "在线 · " + label
            act = voice_menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(vid == cur_voice)
            act.triggered.connect(lambda _c=False, v=vid: self._set_voice(v))
        menu.exec(self.btn_speak.mapToGlobal(pos))

    def _set_rate(self, rate: float):
        self.st.set("tts.rate", rate)
        self.st.save_config()
        self.speech.set_rate(rate)

    def _set_voice(self, voice_id: str):
        self.st.set("tts.voice_id", voice_id)
        self.st.save_config()
        self.speech.set_voice(voice_id)

    # -------------------------------------------------- 在线更新

    def check_updates(self, manual: bool = False):
        """比对 GitHub Release。manual=True 时无视配置、并给状态反馈。"""
        if not manual and not self.st.get("update.auto", True):
            return
        repo = str(self.st.get("update.repo", "chenbtt/interline") or "")
        if not repo:
            return
        self._manual_check = manual
        if manual:
            self._set_hint("正在检查更新…", 0)
        self._updater.check(repo)

    def _on_update_checked(self, ok: bool, info, err: str):
        manual, self._manual_check = self._manual_check, False
        newer = bool(ok and info
                     and int(info.get("version") or 0) > BUILD_VERSION)
        self._update_info = info if newer else None
        badge = getattr(self, "new_badge", None)
        if badge is not None:
            badge.setVisible(newer)
        if not manual:
            return
        if newer:
            self._set_hint(f"发现新版本 {info.get('tag', '')}", 4000)
        else:
            self._set_hint("已是最新版本" if ok else f"检查失败：{err[:40]}", 5000)

    def _open_update(self):
        if not self._update_info:
            return
        update_dialog.UpdateDialog(self, self._update_info).exec()

    def _on_chapter_loaded(self, idx: int, err: str) -> None:
        # 解析期间用户可能已经翻走了，旧结果直接作废
        if idx != self.chapter_idx or not self._pending_chapter:
            return
        pend = self._pending_chapter
        self._pending_chapter = None
        if err:
            self.view.setPlainText(f"这一章解析失败：{err}")
            self._status_text = "解析失败"
            self.update(0, max(0, self.height() - 28), self.width(), 28)
            return
        self.load_chapter(pend[0], ratio=pend[1], at_offset=pend[2])

    # -------------------------------------------------- 一行模式

    def _current_sentence_index(self) -> int:
        """当前该用哪一句：朗读中跟着读到的走，否则取视口顶部。"""
        if not self._tts_sentences:
            return 0
        if self.speech.running and self.speech.current_index >= 0:
            i = self.speech.current_index
        else:
            i = self._visible_sentence_index()
        return max(0, min(i, len(self._tts_sentences) - 1))

    def _current_sentence_text(self) -> str:
        if not self._tts_sentences:
            return "（本章没有正文）"
        return self._tts_sentences[self._current_sentence_index()]

    def toggle_one_line(self):
        if self._one_line:
            self._exit_one_line()
        else:
            self._enter_one_line()

    # -------------------------------------------------- 一行模式的悬浮工具带

    def _one_line_state(self):
        """工具带画的时候现取：播放图标 + 章节进度。

        进度按「当前第几句 / 本章共几句」算，不用滚动条比例——一行模式
        把滚动条关了，那个比例恒为 0，奶读气泡上那个 0% 就是这么来的。
        """
        playing = bool(self.speech.running and not self.speech.paused)
        if not self.book:
            return playing, None
        n = len(self._tts_sentences)
        pct = int(self._one_line_idx * 100 / max(1, n)) if n else 0
        return playing, (pct, self.chapter_idx + 1,
                         len(self.book.chapters))

    def _one_line_sync(self):
        """朗读状态/换句变了，让工具带重画一次（图标与进度标）。"""
        w = getattr(self, "_one_line_tools", None)
        if self._one_line and w is not None:
            w.update()

    def _on_one_line_tool(self, kind: str):
        if not self._one_line:
            return
        if kind == "prev":
            self._step_one_line(-1)
        elif kind == "next":
            self._step_one_line(1)
        elif kind == "play":
            self._toggle_speech()
        elif kind == "hide":
            self._hide_window()
        elif kind == "main":
            self._exit_one_line()

    def _enter_one_line(self):
        if not self.book:
            return
        sb = self.view.verticalScrollBar()
        self._one_line_pos = (
            self.chapter_idx,
            0.0 if sb.maximum() == 0 else sb.value() / sb.maximum())
        self._normal_geo = self.geometry()
        self._one_line = True
        self._one_line_w = None        # 进入时复位，让默认宽按当前句重新算一次
        self._one_line_idx = self._current_sentence_index()
        self.header.hide()
        self.footer.hide()
        self.search_bar.hide()
        # 工具带是独立浮窗：先激活（允许显示、接收鼠标、参与淡入）。它不再
        # 占本窗口的底部，所以正文视口只需上下留一点窄边距给那一行字透气
        self._one_line_tools.activate()
        self.view.setViewportMargins(0, 4, 0, 4)
        # 横向滚动条做细的样式只在一行模式铺上，退出时还原成常规态
        self.view.setStyleSheet(VIEW_ONELINE_SS)
        # 从朗读句起显示，而不是重新算——否则进来会跳一句
        if self._tts_sentences:
            self._paint_one_line(self._tts_sentences[self._one_line_idx])
        else:
            self._paint_one_line()
        # 进来时鼠标一般就悬在窗口上，直接淡入
        self._one_line_tools.fade_in()
        self._set_hint("", 0)

    def _step_one_line(self, delta: int):
        """一行模式下翻句。走到章首/章尾就顺着翻章。"""
        if not self._tts_sentences:
            return
        nxt = self._one_line_idx + delta
        if nxt < 0:
            if self.chapter_idx > 0:
                self._one_line_jump_chapter(self.chapter_idx - 1, last=True)
            return
        if nxt >= len(self._tts_sentences):
            if self.chapter_idx < len(self.book.chapters) - 1:
                self._one_line_jump_chapter(self.chapter_idx + 1, last=False)
            return
        self._one_line_idx = nxt
        self._paint_one_line(self._tts_sentences[nxt])
        if self.speech.running:
            self._start_speech(nxt)     # 正在读就从新的一句接着读

    def _one_line_jump_chapter(self, idx: int, last: bool):
        self.load_chapter(idx)
        if not self._one_line or not self._tts_sentences:
            return
        self._one_line_idx = len(self._tts_sentences) - 1 if last else 0
        self._paint_one_line(self._tts_sentences[self._one_line_idx])
        if self.speech.running:
            self._start_speech(self._one_line_idx)

    def _exit_one_line(self):
        if not self._one_line:
            return
        # 一行模式把文档换成了单句、_tts_base 也被清成 0。退出时如果还用
        # 进入前记的滚动比例，就会回到「切过去那一刻」，而不是现在读到哪。
        # 所以以当前章节 + 当前句索引为准，换算成正文里的字符偏移再定位。
        chapter = self.chapter_idx
        idx = self._one_line_idx
        was_running = self.speech.running
        paused = (self._speech_pos, self._speech_char, self._speech_paused)
        body_off = (self._tts_offsets[idx]
                    if 0 <= idx < len(self._tts_offsets) else None)

        self._one_line = False
        self._one_line_tools.suspend()
        self._one_line_w = None
        # 还原成正文阅读态：常规 QSS、按窗口宽度折行、关掉横向滚动条
        self.view.setStyleSheet(VIEW_BASE_SS)
        self.view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setViewportMargins(0, 0, 0, 0)
        # setFixedSize 会同时锁住 min/max，不解除的话下面的 setGeometry 无效
        self.setMinimumSize(320, 120)
        self.setMaximumSize(16777215, 16777215)
        self.header.show()
        self.footer.show()
        self.view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        if self._normal_geo is not None:
            self.setGeometry(self._normal_geo)

        if body_off is None or not self.book:
            # 拿不到句子索引就退回进入时的位置，至少不会跳到章首
            old_chapter, ratio = self._one_line_pos
            self.load_chapter(old_chapter, ratio=ratio)
            return

        ch = self.book.chapters[chapter]
        title, _body = self._chapter_parts(ch)
        # at_offset 是文档内字符位置：标题 + 换行 + 正文偏移，
        # 和 _build_tts_index 里的 _tts_base 算法一致
        self.load_chapter(chapter, at_offset=len(title) + 1 + body_off)

        loaded = getattr(ch, "loaded", True) is not False
        if was_running and loaded:
            # load_chapter 会停朗读；一行模式本来在读，切回来接着从这句念，
            # 否则用户看到的是「进度对的、声音没了」
            self._start_speech(idx)
        elif paused[2] and paused[0] >= 0:
            # 切过去之前是按了「停」的，保留续读点，别被 load_chapter 清掉
            self._speech_pos, self._speech_char = paused[0], paused[1]
            self._speech_paused = True
            self.btn_speak.setText("续")

    def _paint_one_line(self, text: str | None = None):
        """一行模式下重排尺寸和文字。字号变了也要调一次。

        宽度是**固定**的：进入时按当前这句算一次默认宽（夹在 ONELINE_MIN_W..
        MAX_W），之后每句都沿用，点下一句不再变宽；用户用鼠标拖右下角改的宽
        也会记下来、下次翻句保持。句子比窗口长时**不折行**，而是出一条横向
        细滚动条（方向键左右滚），所以始终是「一行」。

        text 由 _on_sentence 传进来：那一刻文档已经只剩一句，
        _visible_sentence_index() 在这种状态下取不准。
        """
        if text is None:
            text = self._current_sentence_text()
        font = self._make_font(int(self.st.get("font.size",
                                                store.DEFAULT_CONFIG["font"]["size"])))

        # 先定字体再换文本，确保这一句没有被上一次的字符格式带脏
        self.view.document().setDefaultFont(font)
        self.view.setFont(font)
        self.view.setCurrentCharFormat(QTextCharFormat())
        # 不折行：一句始终排在一行上，太长就靠横向滚动条看，而不是断成两行
        self.view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.view.setPlainText(text)
        self.view.setExtraSelections([])
        self._tts_base = 0
        self._hl_tts = None
        self._hl_search = []

        # 高度交给文档自己算：单行模式只用一行。v70 起工具带是独立浮窗、
        # 不再占本窗口底部，所以这里只留那一行字 + 一圈窄内边距 + 底部
        # 横向滚动条的 7px——面板就是一条矮矮的文字条，比之前矮一大截。
        # （不留滚动条那 7px，超宽时细滚动条会把那一行字压掉一截）
        doc_h = self.view.document().size().height()
        h = max(32, int(doc_h) + 20)

        # 宽度：默认取「普通面板」的宽度（进入一行模式前的窗口宽），只在本会话
        # 第一句定一次，之后每句沿用（拖拽改过的也记着，点下一句不再变宽）
        if self._one_line_w is None:
            screen = (QApplication.primaryScreen()
                      .availableGeometry().width() - 80)
            normal_geo = getattr(self, "_normal_geo", None)
            normal_w = int(normal_geo.width()) if normal_geo else self.width()
            self._one_line_w = int(max(ONELINE_MIN_W, min(normal_w, screen)))
            # 允许鼠标拖右下角改宽：只锁高度，宽度放开到屏幕量级
            self.setMinimumWidth(ONELINE_MIN_W)
            self.setMaximumWidth(screen + 80)
        else:
            # 沿用当前实际宽度（用户拖拽改过的会被这里收进来）
            self._one_line_w = self.width()
        self.setFixedHeight(h)                      # 锁高度成「一条」的矮窗
        self.resize(self._one_line_w, h)            # 定宽（不锁宽，留给拖拽）

        # 工具带是独立浮窗：摆到面板正下方居中，跟着窗口走
        self._one_line_tools.place_below(self.geometry())
        # 横向：内容超宽才出细滚动条（样式在 _enter_one_line 里铺好）；
        # 纵向：一条字幕不需要竖条，多出来的那几个像素会挤出一条不能拖的条
        self.view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._one_line_sync()

    # -------------------------------------------------- 奶读模式

    def toggle_pet(self):
        if self._pet:
            w = self._pet_window
            # 气泡按钮刚把蛙藏起来时，F8 / 底栏蛙头是「唤回来」而不是关掉
            if w is not None and not w.isVisible():
                w.resume()
                self._pet_sync_base()
                self._set_hint("奶蛙回来了", 1600)
                return
            self._exit_pet()
        else:
            self._enter_pet()

    def _pet_font(self) -> QFont:
        """气泡字号跟随正文字号，但夹在 13–17px：正文调到 30px 时气泡会炸掉。"""
        size = int(self.st.get("font.size", store.DEFAULT_CONFIG["font"]["size"]))
        f = QFont(self._font_family())
        f.setPixelSize(max(13, min(17, round(size * 0.62))))
        return f

    def _pet_current_text(self) -> str:
        if not self.book:
            return "打开一本书吧"
        if self._tts_sentences:
            i = max(0, min(self._pet_idx, len(self._tts_sentences) - 1))
            return self._tts_sentences[i]
        return "（本章没有正文）"

    def _pet_refresh(self):
        if not self._pet:
            return
        self._pet_text = self._pet_current_text()
        win = self._pet_window
        if win is not None:
            win.set_text(self._pet_text)

    # 设置页改蛙的大小 / 气泡宽度时走这两个入口。
    # 没进奶读模式时窗口还不存在，直接空转——scale 会在 start() 时从配置读
    def pet_apply_scale(self, scale: float):
        if self._pet_window is not None:
            self._pet_window.set_scale(scale)

    def pet_relayout(self):
        if self._pet_window is not None:
            self._pet_window.relayout()

    def pet_unsnap(self):
        """设置里关掉贴边吸附：让蛙从边上下来，回到桌面上的位置。"""
        if self._pet_window is not None:
            self._pet_window.unsnap()

    # -------------------------------------------------- 进出

    def _pet_win(self) -> pet_window.PetWindow:
        """惰性建窗：不用奶读模式的人不该为它付启动开销。"""
        if self._pet_window is None:
            w = pet_window.PetWindow(self.st, self._pet_font,
                                     self._pet_speech_label,
                                     self._pet_progress)
            w.toggle_speech.connect(self._toggle_speech)
            w.bubble_clicked.connect(self._pet_bubble_clicked)
            w.step.connect(self._pet_step)
            w.request_open.connect(self.pick_file)
            w.request_library.connect(self._pet_open_library)
            w.request_settings.connect(self._open_settings)
            w.exit_pet.connect(self._exit_pet)
            w.collapse_bubble.connect(self._pet_collapse_bubble)
            w.show_main.connect(self._pet_toggle_main)
            w.quit_app.connect(QApplication.instance().quit)
            w.sound_started.connect(self._pet_duck_start)
            w.notice.connect(lambda s: self._set_hint(s, 4000))
            self._pet_window = w
        return self._pet_window

    def _pet_progress(self):
        """奶蛙悬停工具条上的本章进度：(百分比, 第几章, 共几章)，没开书给 None。

        和状态栏 `_update_status` 同一个口径，别另算一套。奶读模式下正文
        仍在滚动（_on_sentence 照常 _scroll_to_char），滚动条比例就是
        这一章读到哪儿了；蛙只在 hover 绘制时现取，不用往它那边推状态。
        """
        if not self.book or not getattr(self.book, "chapters", None):
            return None
        sb = self.view.verticalScrollBar()
        pct = 0 if sb.maximum() == 0 else int(sb.value() * 100 / sb.maximum())
        return (pct, self.chapter_idx + 1, len(self.book.chapters))

    def _pet_speech_label(self) -> tuple[str, bool]:
        """右键菜单里那一条朗读项：文案 + 能不能点。"""
        if self.speech.running and not self.speech.paused:
            return "停止朗读", True
        if self.speech.paused or self._speech_paused:
            return "继续朗读", True
        return "开始朗读", bool(self.book and self._tts_sentences)

    def _pet_bubble_clicked(self):
        """点气泡：没书就去挑书，有书就播/停。"""
        if not self.book:
            self.pick_file()
        else:
            self._toggle_speech()

    def _pet_open_library(self):
        """悬浮窗菜单点「书库」：把主窗叫到前面并切到书库。

        蛙不退出——它只是浮在旁边，和主窗里的书库互不干扰。
        """
        self._do_show()
        self.set_mode("books")

    def _pet_collapse_bubble(self):
        """气泡按钮「收起气泡」：只收气泡，蛙和朗读都照旧留在桌面上。

        和「显示/收起主面板」是两件事——一个管奶蛙自己，一个管阅读器。
        收起态工具条还在，同一颗按钮变成向上折角，再点一次就展开。
        """
        w = self._pet_window
        if w is None:
            return
        on = not bool(self.st.get("pet.show_bubble", True))
        w.set_bubble_visible(on)
        self._set_hint("气泡已收起，再点同一颗按钮展开" if not on
                       else "气泡回来了", 2600)

    def _pet_toggle_main(self):
        """气泡按钮「主面板」：显示时收起、隐藏时打开，别只认「叫到前面」。"""
        if self.isVisible():
            self._hide_window()          # 只收阅读界面；蛙不跟着藏
        else:
            self._do_show()

    def _enter_pet(self):
        if self._pet:
            return
        if self._one_line:
            self._exit_one_line()

        self._pet = True
        self._pet_idx = self._current_sentence_index() if self.book else 0
        self._pet_text = self._pet_current_text()
        if not self._pet_win().start(self._pet_text):
            self._pet = False
            self._set_hint("奶蛙素材缺失，奶读模式用不了", 4000)
            return
        self._pet_sync_base()

        self.st.set("pet.on", True)
        self.st.save_config()

    def _exit_pet(self):
        if not self._pet:
            return
        self._pet = False
        self._pet_ambient_timer.stop()
        if self._pet_window is not None:
            self._pet_window.stop()
        self._pet_duck_end()
        self.st.set("pet.on", False)
        self.st.save_config()

    # -------------------------------------------------- 状态与音效

    def _pet_sync_base(self):
        """朗读状态 → 悬浮窗的持续动作。"""
        # 一行模式的工具带也要跟着换播放图标；放在早退之前，
        # 没开奶读时同样要刷新
        self._one_line_sync()
        if not self._pet:
            return
        if self.speech.running and not self.speech.paused:
            # 朗读中常驻「呼吸 + 口型」（reading），嘴跟着念、身子还是那套
            # 安静的 idle 呼吸。原来常驻的是 row8 那套「抬手→放下→抱臂」
            # 循环，20fps 一直在比划，读着书旁边有个东西不停动，看着很赶。
            # 小动作改成隔一阵子随机来一个（见 _pet_ambient）
            name = pet.READING

        elif self.speech.paused or self._speech_paused:
            # 停了（手动按「停」或被老板键按下）：抬手托腮，播一次就停在
            # 那个姿势上等用户回来
            name = pet.THINKING
        else:
            name = pet.IDLE
        if self._pet_window is not None:
            self._pet_window.set_base(name)
        self._pet_ambient_sync()

    # -------------------------------------------------- 朗读中的随机小动作

    def _pet_ambient_sync(self):
        """按朗读状态启停「随机小动作」的定时器。"""
        reading = bool(self._pet and self.speech.running
                       and not self.speech.paused)
        if reading:
            if not self._pet_ambient_timer.isActive():
                self._pet_ambient_schedule()
        else:
            self._pet_ambient_timer.stop()

    def _pet_ambient_schedule(self):
        self._pet_ambient_timer.start(random.randint(AMBIENT_GAP_MS[0],
                                                     AMBIENT_GAP_MS[1]))

    def _pet_ambient(self):
        """朗读中隔 8-15 秒随机来一个小动作，播完自己回到呼吸。

        只挑没有音效的那几个：朗读时突然笑一声或者说话，是另一种打扰。
        """
        if not (self._pet and self.speech.running and not self.speech.paused):
            return
        self._pet_fx(random.choice(pet.AMBIENT_ACTS))
        self._pet_ambient_schedule()

    def _pet_fx(self, name: str):
        if not self._pet or self._pet_window is None:
            return
        self._pet_window.fx(name)

    def _pet_play_direction(self, name: str, hold: int = 620):
        """翻句时原地跑两步，表示「往那个方向去了」。"""
        if not self._pet or self._pet_window is None:
            return
        self._pet_window.play_direction(name, hold)

    def _pet_duck_start(self, ms: int):
        if not self.st.get("pet.duck", True) or self._pet_ducked:
            return
        if not (self.speech.running and not self.speech.paused):
            return
        self._pet_ducked = True
        self.speech.set_volume(float(self.st.get("tts.volume", 1.0)) * 0.4)
        self._pet_duck_timer.start(max(300, int(ms) + 200))

    def _pet_duck_end(self):
        """必须无条件恢复：卡在 0.4 音量比不 duck 还难查。"""
        if not self._pet_ducked:
            return
        self._pet_ducked = False
        self._pet_duck_timer.stop()
        self.speech.set_volume(float(self.st.get("tts.volume", 1.0)))

    # -------------------------------------------------- 翻句

    def _pet_step(self, delta: int):
        if not self._pet or not self._tts_sentences:
            return
        self._pet_play_direction(pet.RUN_LEFT if delta < 0 else pet.RUN_RIGHT)

        nxt = self._pet_idx + delta
        if nxt < 0:
            if not (self.book and self.chapter_idx > 0):
                return
            self.load_chapter(self.chapter_idx - 1)
            if not self._tts_sentences:
                self._pet_refresh()
                return
            self._pet_idx = len(self._tts_sentences) - 1
        elif nxt >= len(self._tts_sentences):
            if not (self.book and self.chapter_idx < len(self.book.chapters) - 1):
                return
            self.load_chapter(self.chapter_idx + 1)
            self._pet_idx = 0
        else:
            self._pet_idx = nxt

        if self.speech.running:
            self._start_speech(self._pet_idx)
        elif 0 <= self._pet_idx < len(self._tts_offsets):
            # 主窗正文跟着滚到同一句，蛙念到哪读者就看到哪
            self._scroll_to_char(self._tts_base + self._tts_offsets[self._pet_idx])
        self._pet_refresh()

    def _pet_on_sentence(self, idx: int):
        if 0 <= idx < len(self._tts_sentences):
            self._pet_idx = idx
            self._pet_refresh()

    # -------------------------------------------------- 书签

    def _open_marks(self):
        if not self.book:
            return
        self.set_mode("read" if self.mode == "marks" else "marks")

    def _add_bookmark(self):
        """在视口顶部那句话的位置加书签。不打断阅读，只给个轻提示。"""
        if not self.book or not self.book_id:
            return
        anchor = self._anchor_top()
        pos = anchor[0] if anchor else 0
        offset = max(0, pos - self._tts_base)
        _title, body = self._chapter_parts(self.book.chapters[self.chapter_idx])
        # 压成单行再存：书签常落在段落交界，正文里的 \n 会带进预览
        preview = " ".join(body[offset:offset + 60].split())
        self.st.add_bookmark(self.book_id, self.chapter_idx, offset, preview)
        self._set_hint("已添加书签", 1600)
        self._pet_fx(pet.JUMPING)

    def _goto_mark(self, chapter: int, offset: int):
        if not self.book:
            return
        self.set_mode("read")
        idx = max(0, min(chapter, len(self.book.chapters) - 1))
        self.load_chapter(idx, at_offset=max(0, offset))

    def _remove_mark(self, chapter: int, offset: int):
        if not self.book_id or not self.book:
            return
        self.st.remove_bookmark(self.book_id, chapter, offset)
        self.library.show_marks(self.st.bookmarks(self.book_id),
                                self.book.chapters)
        self._set_hint("")

    # -------------------------------------------------- 搜索

    def _open_search(self):
        if self.mode != "read":
            self.set_mode("read")
        self.search_bar.setVisible(True)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _close_search(self):
        self.search_bar.setVisible(False)
        self._hits = []
        self._hit_idx = -1
        self._hl_search = []
        self._apply_highlights()
        self.search_count.setText("")
        self.view.setFocus()

    def _do_search(self):
        kw = self.search_input.text().strip()
        self._hits = []
        self._hit_idx = -1
        self._hl_search = []
        if not kw or not self.book:
            self.search_count.setText("")
            self._apply_highlights()
            return
        self._hits = book_parser.search(self.book, kw)
        if not self._hits:
            self.search_count.setText("无结果")
            self._apply_highlights()
            return
        self._goto_hit(0)

    def _step_hit(self, delta: int):
        if not self._hits:
            return
        self._goto_hit((self._hit_idx + delta) % len(self._hits))

    def _goto_hit(self, idx: int):
        if not (0 <= idx < len(self._hits)):
            return
        self._hit_idx = idx
        h = self._hits[idx]
        if h["chapter"] != self.chapter_idx:
            # 跨章：换章后再画（setPlainText 会清掉所有 ExtraSelection）
            self.load_chapter(h["chapter"], at_offset=h["offset"])
        else:
            self._scroll_to_char(self._tts_base + h["offset"])
        self._paint_search_hits()
        self.search_count.setText(f"{idx + 1}/{len(self._hits)}")

    def _paint_search_hits(self):
        """只画当前章内的命中；换章后由调用方再画一次。"""
        sels = []
        doc = self.view.document()
        last = doc.characterCount() - 1
        kw_len = max(1, len(self.search_input.text().strip()))
        base = QColor(self.st.get("colors.search_hl", "#4c2f1c"))
        # 当前命中提亮，其余压暗，否则满屏同色看不出焦点在哪
        active = base.lighter(155)
        for i, h in enumerate(self._hits):
            if h["chapter"] != self.chapter_idx:
                continue
            start = self._tts_base + h["offset"]
            if start < 0 or start >= last:
                continue
            cur = QTextCursor(doc)
            cur.setPosition(start)
            cur.setPosition(min(start + kw_len, last),
                            QTextCursor.MoveMode.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cur
            sel.format.setBackground(active if i == self._hit_idx else base)
            sels.append(sel)
        self._hl_search = sels
        self._apply_highlights()

    # -------------------------------------------------- 排版锚点

    def _anchor_top(self):
        """记「视口顶部那个字符 + 相对像素偏移」。

        不能用滚动比例：行距一变，同样的 ratio 对应的内容就挪位了。
        """
        doc = self.view.document()
        vp = self.view.viewport()
        pos = self.view.cursorForPosition(QPoint(vp.width() // 2, 2)).position()
        block = doc.findBlock(pos)
        if not block.isValid():
            return None
        top = doc.documentLayout().blockBoundingRect(block).top()
        return pos, self.view.verticalScrollBar().value() - top

    def _restore_anchor(self, anchor):
        if not anchor:
            return
        pos, dy = anchor
        doc = self.view.document()
        # 布局是惰性的，不先算一遍会拿到旧几何
        doc.documentLayout().documentSize()
        block = doc.findBlock(pos)
        if not block.isValid():
            return
        top = doc.documentLayout().blockBoundingRect(block).top()
        self.view.verticalScrollBar().setValue(int(top + dy))

    # -------------------------------------------------- 右键菜单

    def _show_view_menu(self, pos):
        # 单行模式下窗口只有一条，菜单也只给最小集合
        if self._one_line:
            menu = QMenu(self)
            menu.addAction("退出单行").triggered.connect(self._exit_one_line)
            menu.addAction("设置…").triggered.connect(self._open_settings)
            menu.addSeparator()
            menu.addAction("隐藏").triggered.connect(self._hide_window)
            menu.addAction("退出").triggered.connect(
                QApplication.instance().quit)
            menu.exec(self.view.viewport().mapToGlobal(pos))
            return

        # 选中一段文字时，右键只给「从这里开始读」，别的都收起来
        cur = self.view.textCursor()
        if cur.hasSelection() and self._tts_sentences:
            menu = QMenu(self)
            act = menu.addAction("从这里开始读")
            act.triggered.connect(self._speak_from_selection)
            menu.exec(self.view.viewport().mapToGlobal(pos))
            return

        menu = QMenu(self)
        act_open = menu.addAction("打开文件…")
        act_lib = menu.addAction("书库…")
        act_toc = menu.addAction("目录…")
        act_mark = menu.addAction("在此处添加书签")
        skin_menu = menu.addMenu("皮肤")
        cur_skin = self.st.get("skin", "read")
        for key in SKIN_ORDER:
            a = skin_menu.addAction(SKIN_NAMES[key])
            a.setCheckable(True)
            a.setChecked(key == cur_skin)
            a.triggered.connect(lambda _c=False, k=key: self.apply_skin(k))
        act_settings = menu.addAction("设置…")
        act_settings.triggered.connect(self._open_settings)
        act_oneline = menu.addAction("一行模式")
        act_oneline.triggered.connect(self.toggle_one_line)
        act_update = menu.addAction("检查更新")
        menu.addSeparator()
        act_top = menu.addAction("置顶")
        act_top.setCheckable(True)
        act_top.setChecked(bool(self.st.get("always_on_top", True)))
        menu.addSeparator()
        if self.boss_key:
            boss_hint = menu.addAction(f"老板键  {self.boss_key}")
            boss_hint.setEnabled(False)
        act_hide = menu.addAction("隐藏")
        act_quit = menu.addAction("退出")

        act_open.triggered.connect(self.pick_file)
        act_lib.triggered.connect(lambda: self.set_mode("books"))
        act_toc.triggered.connect(self.toggle_toc)
        act_mark.triggered.connect(self._add_bookmark)
        act_top.triggered.connect(self.toggle_on_top)
        act_update.triggered.connect(lambda: self.check_updates(True))
        act_hide.triggered.connect(self._hide_window)
        act_quit.triggered.connect(QApplication.instance().quit)
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def toggle_on_top(self):
        on = not bool(self.st.get("always_on_top", True))
        self.st.set("always_on_top", on)
        self.st.save_config()
        flags = self.windowFlags()
        if on:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()
        # 悬浮窗是独立窗口，置顶开关不会自动跟过去
        if self._pet_window is not None:
            self._pet_window.sync_on_top(on)

    # -------------------------------------------------- 显示

    def _do_show(self):
        self.show()
        self.raise_()
        self.activateWindow()
        # 一行模式下主窗被藏过，工具带也一起藏了，唤回来时重新亮起来
        if self._one_line:
            self._one_line_tools.place_below(self.geometry())
            self._one_line_tools.fade_in()
        if self._pet and self._pet_window is not None:
            self._pet_window.resume()
            # 刚从托盘/热键/二次启动唤起来，让奶蛙打个招呼
            self._pet_window.fx(pet.WAVING)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self._on_escape()
            return
        super().keyPressEvent(ev)


# ------------------------------------------------------------------ 入口

def _check_data_dir(tag: str) -> Path:
    """给自检单独开一个「每次运行都清空」的数据目录，保证回归图可复现。

    --toolcheck 会改 pet.scale / colors.fg / pet.show_bubble，
    set_bubble_visible() 又把整份配置写回 config.json。几个自检共用一个
    固定的 %TEMP%/interline_check 时，下一次 --edgecheck 读到的就是上一次
    的残值：画布从 230px / scale 0.90 漂到 154px / scale 0.60，连工具条
    前景色都变了。回归图 md5 每次不同，就没法用它判断改动有没有伤到绘制。
    目录按自检名分开、每次先清空，Store() 只会拿到 DEFAULT_CONFIG。
    用赋值而不是 setdefault：自检绝不该写到用户真实配置里。
    """
    import shutil
    import tempfile
    d = Path(tempfile.gettempdir()) / f"interline_check_{tag}"
    shutil.rmtree(d, ignore_errors=True)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    os.environ["INTERLINE_DATA_DIR"] = str(d)
    return d


def selftest() -> int:
    """打包后自检：确认关键依赖都被 PyInstaller 收进来了。

    打包版是 console=False，没有任何窗口能显示 print，
    所以结果同时写一份到临时文件。
    """
    import tempfile

    lines: list[str] = []
    # 每行立刻落盘：selftest 曾在 Qt 层原生崩溃（0xC0000409）过，
    # 攒到最后一次性写，崩了连「死在哪一步」都留不下证据
    out = Path(tempfile.gettempdir()) / "interline_selftest.txt"
    try:
        out.write_text("", encoding="utf-8")
    except Exception:
        pass

    def log(msg: str):
        lines.append(str(msg))
        try:
            out.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    rc = 0
    try:
        # QTextToSpeech 需要有 QApplication 实例，selftest 早于正常启动流程
        QApplication.instance() or QApplication(sys.argv[:1])

        import PyQt6.QtCore as qc
        st = store.Store()
        log(f"version   = {BUILD_VERSION}")
        log(f"qt        = {qc.QT_VERSION_STR}")
        log(f"data dir  = {st.dir}")
        log(f"writable  = {os.access(st.dir, os.W_OK)}")

        tmp = st.dir / "_probe.txt"
        with open(tmp, "w", encoding="gbk") as f:
            f.write("第一章 自检\n\n正文一段。\n")
        book = book_parser.load_book(str(tmp))
        log(f"parse     = {book.title} / {len(book.chapters)}章 / enc={book.encoding}")
        log(f"sentences = {book_parser.split_sentences('今天天气很好，我们去看书吧。')}")
        tmp.unlink(missing_ok=True)

        # 奶蛙素材：onefile 解包后路径没登记就会读不到，这条最值得测。
        # 必须放在 QTextToSpeech 之前 —— 建完 winrt 引擎后再做 Qt 的图形
        # 操作会原生崩溃（0xC0000409），而且崩溃时连日志都写不出来。
        # 这里也只用 QImage 校验，不碰 QPixmap 切帧：切帧由 UI 冒烟测试覆盖。
        try:
            from PyQt6.QtGui import QImage
            import json as _json
            sheet = assets.naiwa("sheet.png")
            img = QImage(str(sheet))
            meta = _json.loads(assets.naiwa("manifest.json").read_text(encoding="utf-8"))
            # 尺寸从清单推，别硬编码：改了列数（插帧）就得同步改断言
            cols = int(meta.get("cols") or 0)
            rows = max((a.get("row", 0) for a in (meta.get("anims") or {}).values()),
                       default=0) + 1
            want = (cols * int(meta.get("frame_w", 0)), rows * int(meta.get("frame_h", 0)))
            ok_img = (not img.isNull()) and (img.width(), img.height()) == want
            log(f"pet sheet = {sheet.name} {img.width()}x{img.height()} "
                f"（清单要 {want[0]}x{want[1]}）{'OK' if ok_img else 'FAIL'}")
            if not ok_img:
                rc = 2

            anims = meta.get("anims") or {}
            interped = sum(1 for a in anims.values()
                           if a.get("frames") != a.get("src_frames"))
            log(f"pet anims = {len(anims)} 种动画 "
                f"/ 帧 {meta.get('frame_w')}x{meta.get('frame_h')} "
                f"/ {cols} 列 / {interped} 个动作插过帧")
            if not (anims and meta.get("frame_w")):
                rc = 2

            fw = int(meta.get("frame_w", 0))
            fh = int(meta.get("frame_h", 0))
            rd, idl = anims.get("reading") or {}, anims.get("idle") or {}
            import pet as pet_mod

            # 精灵表该有的动画都得在。漏一个的话 PetSprite._info 会静默
            # 回落到 idle —— 不报错，只是切到它时蛙站着发呆
            lack_anim = [n for n in pet_mod.ALL_ANIMS if n not in anims]
            log(f"pet names = {len(pet_mod.ALL_ANIMS)} 个必备动画 "
                f"{'OK' if not lack_anim else 'MISSING ' + str(lack_anim)}")
            if lack_anim:
                rc = 2

            # 吐舌那条是图生图逐帧出的单独素材，不进源表；它是「点蛙反应」
            # 之一，不是朗读 base。行号必须跟 reading 不同，且不许进 BASE_STATES
            # （进了就可能被当成常驻姿势，点完不回落）
            rd2 = anims.get(pet_mod.TONGUE) or {}
            ok_r2 = (int(rd2.get("frames") or 0) >= 6
                     and bool(rd2.get("loop"))
                     and not rd2.get("mouth")
                     and rd2.get("row") is not None
                     and rd2.get("row") != rd.get("row")
                     and pet_mod.TONGUE not in pet_mod.BASE_STATES)
            log(f"pet tongue= {rd2.get('frames')} 帧 {rd2.get('fps')}fps "
                f"row {rd2.get('row')} {'OK' if ok_r2 else 'FAIL'}")
            if not ok_r2:
                rc = 2

            if idl and rd2 and fw and fh:
                def alpha_box(ax: int, ay: int) -> tuple:
                    """一格的可见包围盒（右下开区间）。40k 个像素也就几十毫秒。"""
                    x0, y0, x1, y1 = 10 ** 6, 10 ** 6, -1, -1
                    for y in range(fh):
                        py = ay + y
                        for x in range(fw):
                            if (img.pixel(ax + x, py) >> 24) & 0xFF > 8:
                                if x < x0:
                                    x0 = x
                                if x > x1:
                                    x1 = x
                                if y < y0:
                                    y0 = y
                                if y > y1:
                                    y1 = y
                    return (x0, y0, x1 + 1, y1 + 1) if x1 >= 0 else (0, 0, 0, 0)

                irow, trow = int(idl["row"]), int(rd2["row"])
                ib, tb = alpha_box(0, irow * fh), alpha_box(0, trow * fh)
                dh = abs(tb[3] - ib[3])
                ratio = (tb[3] - tb[1]) / max(1, ib[3] - ib[1])
                dc = abs((tb[0] + tb[2]) / 2 - (ib[0] + ib[2]) / 2)
                # 隔 4 像素抽样比一遍：两套是不同画，不抽样比全图太慢
                diff = sum(1 for y in range(0, fh, 4) for x in range(0, fw, 4)
                           if img.pixel(x, trow * fh + y) != img.pixel(x, irow * fh + y))
                ok_size = (dh <= 3 and 0.96 <= ratio <= 1.04 and dc <= 4 and diff > 200)
                log(f"pet 体量  = 吐舌 vs idle 底边差 {dh}px / 高比 {ratio:.3f} / "
                    f"中心差 {dc:.1f}px / 抽样差 {diff} 点 "
                    f"{'OK' if ok_size else 'FAIL'}")
                if not ok_size:
                    rc = 2

            # 点蛙反应池：两套都得能抽到（抽不到说明常量写错了），
            # 而且吐舌不能被写进 AMBIENT_ACTS —— 朗读中自己蹦出来很出戏
            seen = {pet_mod.poke_act() for _ in range(200)}
            ok_poke = (seen == set(pet_mod.POKE_ACTS)
                       and pet_mod.TONGUE not in pet_mod.AMBIENT_ACTS)
            log(f"pet poke  = 点蛙反应 {sorted(seen)} / 朗读小动作里没有吐舌 "
                f"{pet_mod.TONGUE not in pet_mod.AMBIENT_ACTS} "
                f"{'OK' if ok_poke else 'FAIL'}")
            if not ok_poke:
                rc = 2

            # reading 的口型是构建期画进精灵表的，不是运行时叠的图层。
            # 直接比像素：张口帧必须和 idle 同列不同，闭口帧必须完全相同。
            # 这样万一以后重跑 make_pet_sheet.py 漏了口型，自检会当场发现。
            if rd.get("mouth") and idl:
                ri, rr = int(idl.get("row", 0)), int(rd.get("row", -1))

                def mouth_diff(col: int, src: int) -> int:
                    n = 0
                    for dy in range(30, 40):
                        for dx in range(74, 94):
                            if (img.pixel(col * fw + dx, rr * fh + dy)
                                    != img.pixel(src * fw + dx, ri * fh + dy)):
                                n += 1
                    return n

                opened, closed = mouth_diff(2, 2), mouth_diff(0, 0)
                ok_mouth = opened >= 20 and closed == 0
                log(f"pet mouth = reading 张口帧差 {opened} 像素 / "
                    f"闭口帧差 {closed} 像素 {'OK' if ok_mouth else 'FAIL'}")
                if not ok_mouth:
                    rc = 2

            sfx = [assets.naiwa(f"sfx/{n}") for n in
                   ("laugh.mp3", "voice_01.mp3", "voice_02.mp3",
                    "voice_03.mp3", "voice_04.mp3")]
            missing = [p.name for p in sfx if not p.exists()]
            log(f"pet sfx   = {'OK 5/5' if not missing else 'MISSING ' + str(missing)}")
            if missing:
                rc = 2

            # 贴边素材：meta.json 和 24 张图少一个，吸附就整块失效
            # （PetWindow 会静默降级成「拖到边上也不吸」，不报错）
            edge_meta = assets.naiwa("edges/meta.json")
            edge_ok = False
            try:
                em = _json.loads(edge_meta.read_text(encoding="utf-8"))
                dirs = em.get("dirs") or {}
                phases = em.get("phases") or ["base"]
                # 单姿态方向：dir × phase；多姿态方向（s）：每个 pose 的 base/blink
                want = []
                for d, g in dirs.items():
                    poses = g.get("poses")
                    if poses:
                        for p in poses:
                            want += [f"edge-{d}-{p['id']}-base",
                                     f"edge-{d}-{p['id']}-blink"]
                    else:
                        want += [f"edge-{d}-{ph}" for ph in phases]
                lack = [s for s in want
                        if not assets.naiwa(f"edges/{s}.png").exists()]
                with_mouth = [d for d, g in dirs.items()
                              if not g.get("poses")
                              and len(g.get("mouth") or []) == 3]
                with_pose = [d for d, g in dirs.items() if g.get("poses")]
                edge_ok = bool(want) and not lack
                log(f"pet edge  = {'OK ' + str(len(want)) + ' 张' if edge_ok else 'MISSING ' + str(lack[:4])}")
                # 其余 7 向必须有嘴位；s 换成 4 套躺姿后暂时不画口型（每张
                # 嘴位置都不同，自动测不准，宁可不画也不画到脸上别处）
                ok_mouth = (len(dirs) == 8 and len(with_mouth) == 7
                            and len(with_pose) == 1)
                log(f"pet mouth = 贴边 {len(with_mouth)}/{len(dirs)} 向有嘴位"
                    f"（s 多姿态暂不画口型） {'OK' if ok_mouth else 'FAIL'}")
                if not ok_mouth:
                    rc = 2
            except Exception as e:
                log(f"pet edge  = FAIL {type(e).__name__}: {e}")
            if not edge_ok:
                rc = 2

            # 悬浮窗是奶读模式的主力，模块没打进来要在这里就暴露。
            # 只查类和方法是否存在，不构造 QWidget（见下面 TTS 与 QPixmap 的坑）
            import pet_window
            need = ("start", "stop", "set_text", "fx", "suspend", "resume",
                    "unsnap", "set_scale", "relayout", "set_bubble_visible",
                    "_toolbar", "_emit_tool", "_progress_label",
                    "collapse_bubble", "show_main")
            lacking = [n for n in need
                       if not hasattr(pet_window.PetWindow, n)]
            log(f"pet window= {'OK' if not lacking else 'MISSING ' + str(lacking)}")
            if lacking:
                rc = 2
            need_reader = ("_pet_progress",)
            lacking_r = [n for n in need_reader if not hasattr(Reader, n)]
            log(f"pet prog  = {'OK' if not lacking_r else 'MISSING ' + str(lacking_r)}")
            if lacking_r:
                rc = 2
        except Exception as e:
            log(f"pet FAIL  = {type(e).__name__}: {e}")
            rc = 2

        try:
            from PyQt6.QtTextToSpeech import QTextToSpeech
            log(f"tts engine= {QTextToSpeech.availableEngines()}")
            t = QTextToSpeech("winrt")
            log(f"tts voices= {[v.name() for v in t.availableVoices()]}")
        except Exception as e:
            log(f"tts FAIL  = {type(e).__name__}: {e}")

        # 别在这里构造 Reader 验证主窗口：打包版 selftest 没有消息循环，
        # 而且上面已经建过一个 QTextToSpeech，构造主窗口会触发 Qt 层
        # 原生崩溃（0xC0000409），连日志都写不出来。主窗口由 UI 冒烟测试覆盖。

        # PDF 是延迟导入的，这里确认打包时真的把它收进去了
        try:
            import pdfminer
            log(f"pdfminer  = {pdfminer.__version__}")
        except Exception as e:
            log(f"pdfminer FAIL = {type(e).__name__}: {e}")
            rc = 2


        # 在线音色：确认 edge_tts 和它的 aiohttp/certifi 依赖链都打进包了
        try:
            import edge_tts
            import aiohttp
            from PyQt6.QtMultimedia import QMediaPlayer
            log(f"edge-tts  = {getattr(edge_tts, '__version__', '?')} "
                f"/ aiohttp {aiohttp.__version__} / QMediaPlayer ok")
        except Exception as e:
            log(f"edge-tts FAIL = {type(e).__name__}: {e}")

        # 在线更新：确认 updater / update_dialog 都进了包
        try:
            import updater
            import update_dialog
            log(f"updater   = repo {st.get('update.repo', '')} "
                f"/ auto {st.get('update.auto', True)}")
        except Exception as e:
            log(f"updater FAIL = {type(e).__name__}: {e}")
            rc = 2

        log("RESULT    = OK")
    except Exception as e:
        import traceback
        log(f"RESULT    = FAIL {type(e).__name__}: {e}")
        log(traceback.format_exc())
        rc = 1

    out = Path(tempfile.gettempdir()) / "interline_selftest.txt"
    try:
        out.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass
    return rc


def voicecheck() -> int:
    """打包后验证在线音色整条链路：联网合成 → MP3 解码 → 逐句播放。

    spec 里剔掉了 ffmpeg 后端，只留 WMF；这个自检用来确认正式 exe
    真的能播 MP3，而不只是「import 得到模块」。
    """
    import tempfile

    lines: list[str] = []
    rc = 0
    app = QApplication.instance() or QApplication(sys.argv[:1])

    def log(msg: str):
        lines.append(str(msg))

    try:
        import aiohttp
        import edge_tts
        import edge_speech
        from PyQt6.QtMultimedia import QMediaDevices, QMediaPlayer
        log(f"edge-tts  = {getattr(edge_tts, '__version__', '?')} "
            f"/ aiohttp {aiohttp.__version__}")
        log(f"audio out = {[d.description() for d in QMediaDevices.audioOutputs()]}")

        hits = {"sentences": [], "words": 0, "finished": False, "err": ""}
        eng = edge_speech.EdgeEngine()
        eng.set_volume(0.01)       # 几乎听不见；别设 0，Qt 静音时时钟可能不推进
        eng.sentence_changed.connect(lambda i: hits["sentences"].append(i))
        eng.word_spoken.connect(lambda *a: hits.update(words=hits["words"] + 1))
        eng.finished.connect(lambda: (hits.update(finished=True), app.quit()))
        eng.failed.connect(lambda e: (hits.update(err=e), app.quit()))
        eng.start(["打包后的在线音色自检，第一句。",
                   "这是第二句，用来确认逐句接着念。"], 0, 0)
        QTimer.singleShot(25000, app.quit)
        app.exec()
        eng.stop()
        log(f"sentences = {hits['sentences']}")
        log(f"words     = {hits['words']}")
        log(f"finished  = {hits['finished']}")
        log(f"error     = {hits['err']}")
        if not hits["finished"] or hits["err"]:
            rc = 2
        log("RESULT    = OK" if rc == 0 else "RESULT    = FAIL")
    except Exception:
        import traceback
        log("RESULT    = FAIL")
        log(traceback.format_exc())
        rc = 1

    out = Path(tempfile.gettempdir()) / "interline_voicecheck.txt"
    try:
        out.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass
    return rc


def updatecheck() -> int:
    """打包后验证更新链路：能连上 GitHub API 并解析出 latest Release。"""
    import tempfile

    app = QApplication.instance() or QApplication(sys.argv[:1])
    lines: list[str] = []
    rc = 0

    def log(msg: str):
        lines.append(str(msg))

    try:
        import updater
        st = store.Store()
        repo = str(st.get("update.repo", "") or "")
        hit: dict = {}
        u = updater.Updater()

        def on_checked(ok, info, err):
            hit.update(ok=ok, info=info, err=err)
            app.quit()

        u.checked.connect(on_checked)
        log(f"repo      = {repo}")
        u.check(repo)
        QTimer.singleShot(20000, app.quit)
        app.exec()
        if not hit:
            log("RESULT    = FAIL timeout")
            rc = 2
        elif not hit.get("ok"):
            err = str(hit.get("err") or "")
            if "限流" in err or "403" in err or "429" in err:
                # 403 说明 HTTPS 和 API 都通了，只是额度用光——
                # 对「打包是否完整」这个自检目的来说算通过
                log(f"note      = {err}（网络链路正常）")
                log("RESULT    = OK (rate limited)")
            else:
                log(f"error     = {err}")
                log("RESULT    = FAIL")
                rc = 2
        else:
            info = hit.get("info") or {}
            log(f"latest    = {info.get('tag')} "
                f"(version {info.get('version')})")
            log(f"asset     = {info.get('asset')}")
            log(f"size      = {info.get('size') or '（下载时取 Content-Length）'}")
            log(f"body      = {len(info.get('body') or '')} chars")
            log("RESULT    = OK")
    except Exception:
        import traceback
        log("RESULT    = FAIL")
        log(traceback.format_exc())
        rc = 1

    out = Path(tempfile.gettempdir()) / "interline_updatecheck.txt"
    try:
        out.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass
    return rc


def restartcheck() -> int:
    """端到端验证「更新后自动重启」这条链路，用 exe 的副本跑，不碰正在用的那份。

    走的和真实更新完全同一条路：生成脚本 → move 覆盖 → 等旧 PID 消失 →
    独立 TEMP 启动 → 新进程写 marker 回执。唯一区别是被启动的那份带着
    INTERLINE_RESTART_PROBE，写完 marker 立刻退出，不会把界面拉起来。

    为什么值得单独有这么个检查：这条链路的失败是「更新成功了、但程序起不来」
    ——用户看到的是一句 python313.dll 加载失败，实际情况是旧进程清理 _MEI
    目录时把新进程解压出来的 DLL 一起删了。这种时序问题在源码模式下根本
    复现不了，只有打包版 + 真替换才暴露。
    """
    import shutil
    import subprocess
    import tempfile
    import time

    lines: list[str] = []
    out = Path(tempfile.gettempdir()) / "interline_restartcheck.txt"

    def log(msg: str):
        lines.append(str(msg))
        try:
            out.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    def finish(code: int, msg: str) -> int:
        log(f"RESULT    = {msg}")
        try:
            out.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass
        return code

    if not getattr(sys, "frozen", False):
        log("skip      = 源码运行，没有 exe 可测")
        return finish(0, "SKIP (not frozen)")

    import updater

    exe = Path(sys.executable)
    work = Path(tempfile.gettempdir()) / "interline_restartcheck"
    try:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log(f"error     = 无法准备目录：{e}")
        return finish(2, "FAIL")

    target = work / "target.exe"
    new = work / "new.exe"
    marker = work / "marker.txt"
    try:
        shutil.copy2(exe, target)
        shutil.copy2(exe, new)
    except Exception as e:
        log(f"error     = 复制 exe 失败：{e}")
        return finish(2, "FAIL")

    log(f"version   = {BUILD_VERSION}")
    log(f"exe       = {exe} ({exe.stat().st_size} B)")
    log(f"work      = {work}")

    script = updater.build_script(new, target, old_pid=0, marker=marker)
    cmd_file = work / "u.cmd"
    try:
        cmd_file.write_text(script, encoding="gbk")
    except Exception as e:
        log(f"error     = 写脚本失败：{e}")
        return finish(2, "FAIL")

    # 脚本自己会设 marker；这里只需要告诉新进程「你是探针，写完就退」
    env = dict(os.environ)
    env.pop("INTERLINE_RESTART_MARKER", None)
    env["INTERLINE_RESTART_PROBE"] = "1"
    try:
        subprocess.Popen(["cmd", "/c", str(cmd_file)],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS",
                                               0x00000008),
                         env=env, close_fds=True)
    except Exception as e:
        log(f"error     = 拉起脚本失败：{e}")
        return finish(2, "FAIL")

    t0 = time.time()
    while time.time() - t0 < 45:
        if marker.exists():
            break
        time.sleep(0.3)
    elapsed = time.time() - t0
    log(f"elapsed   = {elapsed:.1f}s")

    if not marker.exists():
        log("marker    = 缺失（新进程没能起来）")
        log_file = Path(os.environ.get("LOCALAPPDATA", ".")) / "Interline" / "update_log.txt"
        try:
            log("--- update_log.txt ---")
            for ln in log_file.read_text(encoding="gbk",
                                         errors="replace").splitlines()[-12:]:
                log(f"  {ln}")
        except Exception as e:
            log(f"  （读不到更新日志：{e}）")
        return finish(2, "FAIL 新进程没有回执")

    content = marker.read_text(encoding="utf-8", errors="replace")
    info = {}
    for ln in content.splitlines():
        if "=" in ln:
            k, _, v = ln.partition("=")
            info[k.strip()] = v.strip()
    log(f"marker    = {len(content)} B")
    for k in ("pid", "TEMP", "MEIPASS"):
        log(f"  {k:<8}= {info.get(k, '')}")

    problems = []
    # 1) 替换真的发生了：target 和 new 已经是同一份新包
    if new.exists():
        problems.append("new.exe 没被移走，替换没发生")
    try:
        if target.stat().st_size != exe.stat().st_size:
            problems.append("target 大小和新包不一致")
    except Exception as e:
        problems.append(f"target 读不到：{e}")
    # 2) TEMP 隔离生效：这是躲开 _MEI 撞名的关键
    meipass = info.get("MEIPASS", "")
    if "\\Interline\\run\\" not in meipass:
        problems.append(f"TEMP 没隔离，MEIPASS 落在 {meipass or '（空）'}")
    # 3) 起来的是另一个进程，不是把当前这个当成回执
    if info.get("pid") and info.get("pid") == str(os.getpid()):
        problems.append("marker 是当前进程写的，不是新进程")

    if problems:
        for p in problems:
            log(f"problem   = {p}")
        return finish(2, "FAIL")
    return finish(0, "OK")


def petcheck() -> int:
    """打包后验证奶蛙的像素路径：切帧 + 画一张「气泡 + 蛙」出来。

    为什么不放进 --selftest：那边会先建 QTextToSpeech("winrt")，之后再做
    QPixmap 相关的图形操作会原生崩溃（0xC0000409），连日志都写不出来。
    所以切帧单独在这里跑，全程不碰 TTS。
    """
    import tempfile

    lines: list[str] = []
    out = Path(tempfile.gettempdir()) / "interline_petcheck.txt"
    png = Path(tempfile.gettempdir()) / "interline_petcheck.png"

    def log(msg: str):
        lines.append(str(msg))
        try:
            out.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    _check_data_dir("pet")
    rc = 0
    app = QApplication.instance() or QApplication(sys.argv[:1])
    try:
        import pet as pet_mod
        import store as store_mod

        st = store_mod.Store()
        log(f"version   = {BUILD_VERSION}")
        log(f"sheet     = {assets.naiwa('sheet.png')}")

        sp = pet_mod.PetSprite()
        if not sp.load():
            log("RESULT    = FAIL 精灵表读不出来")
            return 2
        log(f"frame     = {sp.size().width()}x{sp.size().height()}")
        for name in pet_mod.ALL_ANIMS:
            fr = sp.frames(name)
            log(f"  {name:<14} {len(fr):>2} 帧 {sp.fps(name):>2}fps "
                f"{'loop' if sp.loops(name) else 'once'}")
            if not fr:
                rc = 2

        # 状态机接线：朗读常驻 reading、随机小动作播完能落回 reading、
        # 停下朗读换成托腮（一次性、不循环）。常量改漏了这里会直接报错。
        pst = pet_mod.PetState(sp)
        pst.set_base(pet_mod.READING)
        pst.tick(400)                       # 越过 MIN_DWELL_MS，让 pending 落地
        at_read = pst.current == pet_mod.READING
        pst.play(pet_mod.WAITING, 500)
        pst.tick(600)                       # hold 到期，回到 base
        back = pst.current == pet_mod.READING
        # 吐舌是「点蛙反应」的一次性动作：点一下播一轮，播完必须回落。
        # 要是它被误写进 BASE_STATES，这里就会变成常驻姿势、点完不回来
        pst.play(pet_mod.TONGUE, 1200)
        at_tongue = pst.current == pet_mod.TONGUE
        pst.tick(1400)
        back2 = pst.current == pet_mod.READING
        pst.set_base(pet_mod.THINKING)
        pst.tick(400)
        at_pause = (pst.current == pet_mod.THINKING
                    and not sp.loops(pet_mod.THINKING))
        log(f"state     = 朗读 {at_read} / 小动作后回落 {back} / "
            f"吐舌 {at_tongue} / 吐舌后回落 {back2} / 停下托腮 {at_pause}")
        if not (at_read and back and at_tongue and back2 and at_pause):
            rc = 2

        # 两套朗读动画的体量：吐舌是图生图出来的另一条素材，构建期按高度
        # 归一过，这里量一遍，确认没跑偏（底边/高度/水平中心）
        def qbox(im) -> tuple:
            x0, y0, x1, y1 = 10 ** 6, 10 ** 6, -1, -1
            for y in range(im.height()):
                for x in range(im.width()):
                    if (im.pixel(x, y) >> 24) & 0xFF > 8:
                        if x < x0:
                            x0 = x
                        if x > x1:
                            x1 = x
                        if y < y0:
                            y0 = y
                        if y > y1:
                            y1 = y
            return (x0, y0, x1 + 1, y1 + 1) if x1 >= 0 else (0, 0, 0, 0)

        ib = qbox(sp.frames(pet_mod.IDLE)[0].toImage())
        tb = qbox(sp.frames(pet_mod.TONGUE)[0].toImage())
        dh = abs(tb[3] - ib[3])
        ratio = (tb[3] - tb[1]) / max(1, ib[3] - ib[1])
        dc = abs((tb[0] + tb[2]) / 2 - (ib[0] + ib[2]) / 2)
        ok_size = dh <= 3 and 0.96 <= ratio <= 1.04 and dc <= 4
        log(f"size      = 吐舌 vs idle 底边差 {dh}px / 高比 {ratio:.3f} / "
            f"中心差 {dc:.1f}px {'OK' if ok_size else 'FAIL'}")
        if not ok_size:
            rc = 2

        # 吐舌 vs 朗读并排一张：肉眼确认是一路人（配色、轮廓粗细、受光方向），
        # 毕竟点蛙反应和念书姿势会先后出现在同一个位置上
        png2 = Path(tempfile.gettempdir()) / "interline_petcheck-2sets.png"
        fsz = sp.size()
        sets = QPixmap(fsz.width() * 2 + 48, fsz.height() + 24)
        sets.fill(QColor("#161a1f"))
        p = QPainter(sets)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for i, nm in enumerate((pet_mod.READING, pet_mod.TONGUE)):
            fr = sp.frames(nm)
            if fr:
                p.drawPixmap(16 + i * (fsz.width() + 16), 12,
                             fr[min(2, len(fr) - 1)])
        p.end()
        if not sets.save(str(png2), "PNG"):
            log("sets      = 保存失败")
            rc = 2
        else:
            log(f"sets      = 朗读 vs 吐舌并排 -> {png2}")

        # 真画一遍：气泡在上、蛙在下，导出 PNG 供肉眼回归。
        # 排版规则照抄 pet_window._metrics：窗口宽取两者较宽者，各自水平居中，
        # 否则气泡尾巴会指不到蛙头，导出的图跟真实样子对不上。
        size = int(st.get("font.size", store_mod.DEFAULT_CONFIG["font"]["size"]))
        font = QFont(st.get("font.family", store_mod.UI_FONT))
        font.setPixelSize(max(13, min(17, round(size * 0.62))))
        text = "他把书合上，抬头看了看窗外那片一直没有停过的雨。"
        body = pet_mod.measure_bubble(font, text, 360)
        w = max(body.width(), fsz.width())
        h = (body.height() + pet_mod.BUBBLE_TAIL + pet_window.BUBBLE_PAD
             + fsz.height())
        margin = 24
        pm = QPixmap(w + margin * 2, h + margin * 2)
        pm.fill(QColor("#161a1f"))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.translate(margin, margin)
        bubble = QRect((w - body.width()) // 2, 0, body.width(), body.height())
        pet_mod.draw_bubble(p, bubble, font, st, text)
        frog = QRect((w - fsz.width()) // 2, h - fsz.height(),
                     fsz.width(), fsz.height())
        # 用 reading 的张口帧，导出的回归图里能直接看见嘴有没有被打进包
        pet_mod.draw_pet(p, frog, sp.frames(pet_mod.READING)[2])
        p.end()
        if not pm.save(str(png), "PNG"):
            log("render    = 保存失败")
            rc = 2
        else:
            log(f"render    = {pm.width()}x{pm.height()} "
                f"(气泡 {body.width()}x{body.height()}) -> {png}")
        log("RESULT    = OK")
    except Exception as e:
        import traceback
        log(f"RESULT    = FAIL {type(e).__name__}: {e}")
        log(traceback.format_exc())
        rc = 1
    return rc


def toolcheck() -> int:
    """气泡悬停工具条 + 底栏蛙头图标的真窗口自检。

    单独跑，不碰 TTS：这轮只画 QPainter / 抓 QWidget，建 QTextToSpeech
    之后再动 QPixmap 会原生崩溃（见 --petcheck 的说明）。
    除了算坐标，还要真点一遍五颗按钮、抓两张图，确认图标和底片都画出来了。
    """
    import tempfile

    lines: list[str] = []
    out = Path(tempfile.gettempdir()) / "interline_toolcheck.txt"
    here = Path(__file__).resolve().parent

    def log(msg: str):
        lines.append(str(msg))
        try:
            out.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    check_dir = _check_data_dir("tools")
    rc = 0
    app = QApplication.instance() or QApplication(sys.argv[:1])
    win = None
    try:
        import pet_window as pw
        import store as store_mod

        st = store_mod.Store()
        st.set("pet.snap_dir", "")
        # 音效关掉。奶蛙一亮相就会静音预热音频链路，收尾那个
        # `stop()/setMuted()` 在 ffmpeg 后端上等设备端点时是会**阻塞**的，
        # 而这里第一次长时间 processEvents() 正好把那个定时器派发出去——
        # 结果就是自检卡死在「预热收尾」，后面的断言一条都跑不到。
        # 自检本来也不验音效（那条在 --selftest 里），关掉顺手让结果
        # 不依赖当前机器有没有可用输出设备。
        st.set("pet.sound", False)
        text = ("我们试图用我们仅有的天分去表达我们深层的感受，去表达我们"
                "对前人所有贡献的感激，去为历史长河加上一点儿什么")
        win = pw.PetWindow(st, lambda: QFont(store_mod.UI_FONT),
                           lambda: ("停止朗读", True))
        if not win.start(text):
            log("RESULT = FAIL 奶蛙素材读不出来")
            return 2
        win.hide()                       # 只借它的绘制，不弹到桌面
        log(f"version = {BUILD_VERSION}")
        log(f"data dir = {check_dir}（每次清空，不读用户配置）")

        size, bubble, frog = win._metrics()
        log(f"  窗口 {size.width()}x{size.height()} / 气泡 "
            f"{bubble.width()}x{bubble.height()} / 蛙 {frog.width()}x{frog.height()}")
        rects, zone = win._toolbar()
        ids = tuple(rects)
        want_ids = pw.TOOL_LEFT + pw.TOOL_RIGHT
        ok = ids == want_ids
        rc |= 0 if ok else 2
        log(f"  五颗按钮 {ids} {'ok' if ok else '?? 期望 ' + str(want_ids)}")

        # 尺寸 / 落位：都在窗口内、气泡下方、互不重叠、不压正文
        inside = all(0 <= r.left() and r.right() < size.width()
                     and 0 <= r.top() and r.bottom() < size.height()
                     for r in rects.values())
        below = all(r.top() > bubble.bottom() for r in rects.values())
        clear = all(not r.intersects(bubble) for r in rects.values())
        ordered = (rects["prev"].left() < rects["play"].left()
                   < rects["next"].left() < rects["hide"].left()
                   < rects["main"].left())
        # 站立帧头部可见宽度只占框的 0.36~0.64；两边各留 0.32 就碰不到头
        head_clear = (rects["next"].right() < frog.left() + frog.width() * 0.32
                      and rects["hide"].left()
                      > frog.right() - frog.width() * 0.32)
        ok = inside and below and clear and ordered and head_clear
        rc |= 0 if ok else 2
        log(f"  落位 窗口内 {inside} / 气泡下 {below} / 不压正文 {clear} / "
            f"左三右二 {ordered} / 不压蛙头 {head_clear} "
            f"{'ok' if ok else '??'}")

        # 悬停带要把气泡和按钮连起来，中间不能断
        mid = QPoint(bubble.center().x(), bubble.bottom() + 2)
        zone_ok = zone.contains(mid) and all(
            zone.contains(r.center()) for r in rects.values())
        rc |= 0 if zone_ok else 2
        log(f"  悬停带连续 {zone_ok} {'ok' if zone_ok else '??'}")

        win._set_tools(True)
        win._tools_alpha = 1.0
        hits = {k: win._hit(r.center()) for k, r in rects.items()}
        ok = all(v == "tool:" + k for k, v in hits.items())
        rc |= 0 if ok else 2
        log(f"  命中 {hits} {'ok' if ok else '??'}")
        ok = win._hit(bubble.center()) == "bubble"
        rc |= 0 if ok else 2
        log(f"  气泡仍可点 {ok} {'ok' if ok else '??'}")

        # 五颗按钮真的把对应信号抛出去
        got: list[tuple] = []
        win.step.connect(lambda d: got.append(("step", d)))
        win.bubble_clicked.connect(lambda: got.append(("play",)))
        win.collapse_bubble.connect(lambda: got.append(("hide",)))
        win.show_main.connect(lambda: got.append(("main",)))
        expect = {"prev": ("step", -1), "next": ("step", 1),
                  "play": ("play",), "hide": ("hide",), "main": ("main",)}
        fired = {}
        for k in want_ids:
            got.clear()
            win._emit_tool(k)
            fired[k] = tuple(got) == (expect[k],)
        ok = all(fired.values())
        rc |= 0 if ok else 2
        log(f"  动作接线 {fired} {'ok' if ok else '??'}")

        # 真鼠标事件链：悬停气泡 → 移到按钮 → 按下 → 松开 → 移开熄灭。
        # 上面只验了各段方法，这里验它们串起来不打架（尤其别把按钮当拖拽）
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QMouseEvent

        def send(t, pos, btn=Qt.MouseButton.NoButton,
                 buttons=Qt.MouseButton.NoButton):
            ev = QMouseEvent(t, QPointF(pos), QPointF(pos), btn, buttons,
                             Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(win, ev)

        center = rects["play"].center()
        send(QEvent.Type.MouseMove, bubble.center())
        on_bubble = win._tools_on
        send(QEvent.Type.MouseMove, center)
        on_button = win._tools_on and win._tool_hover == "play"
        send(QEvent.Type.MouseButtonPress, center,
             Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
        no_drag = win._drag is None and win._tool_press == "play"
        got.clear()
        send(QEvent.Type.MouseButtonRelease, center,
             Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
        clicked = tuple(got) == (("play",),)
        send(QEvent.Type.MouseMove, QPoint(size.width() // 2, size.height() - 4))
        off_ok = not win._tools_on
        ok = on_bubble and on_button and no_drag and clicked and off_ok
        rc |= 0 if ok else 2
        log(f"  真鼠标链 亮起 {on_bubble} / 移到按钮 {on_button} / 不当拖拽 "
            f"{no_drag} / 点击生效 {clicked} / 移开熄灭 {off_ok} "
            f"{'ok' if ok else '??'}")

        # ---- v59：吸附态工具条 / 行数变化 / 收起气泡 / 主面板切换
        import pet_edge

        ok = pet_edge.SNAP == 10
        rc |= 0 if ok else 2
        log(f"  吸附阈值 = {pet_edge.SNAP}px {'ok' if ok else '?? 期望 10'}")

        t3 = ("他的问题是他同时还是一个极为罕见的报时人，我和科林斯都上当了，"
              "不然几年前我就该买苹果的；")
        t2 = "你怎么看我的理解？你能简单说说“利润之外”追求对公司的好处在哪里么？"
        mock = QRect(0, 0, 1280, 780)
        anchors = {
            "nw": QPoint(60, 60), "n": QPoint(640, 40), "ne": QPoint(1220, 60),
            "w": QPoint(40, 400), "e": QPoint(1240, 300),
            "sw": QPoint(60, 720), "s": QPoint(560, 740), "se": QPoint(1220, 720),
        }
        edge_ok = True
        for d, a in anchors.items():
            win._edge_dir = ""
            win._snap_to(d, a, animate=False, screen=mock)
            sz, bub, _cv = win._metrics()
            rs, zn = win._toolbar()
            band = QRect(win._band)
            inside = (len(rs) == 5 and all(
                0 <= r.left() and r.right() < sz.width()
                and 0 <= r.top() and r.bottom() < sz.height()
                for r in rs.values()))
            flush = (band.bottom() == sz.height() - 1 or band.top() == 0)
            gapless = (bub is None or band.top() == bub.bottom() + 1
                       or band.bottom() + 1 == bub.top())
            path = (bub is None or zn.contains(bub.center()))
            good = inside and flush and gapless and path
            edge_ok &= good
            if not good:
                log(f"    {d:<3} 按钮{len(rs)} 窗内{inside} 齐边{flush} "
                    f"无缝{gapless} 气泡在带内{path}")
        rc |= 0 if edge_ok else 2
        log(f"  八向吸附工具条 齐边无缝 {edge_ok} "
            f"{'ok' if edge_ok else '??'}")

        # 顶部贴边 3 行 → 2 行：窗口 / 画布 / 蛙 / 工具带一寸都不动
        win._edge_dir = ""
        win.set_text(t3)
        win._snap_to("n", QPoint(640, 40), animate=False, screen=mock)
        g3 = win.geometry()
        _s3, b3, cv3 = win._metrics()
        fr3 = win._frog_rect()
        band3 = QRect(win._band)
        rs3, _zn3 = win._toolbar()
        win.set_text(t2)
        g2 = win.geometry()
        _s2, b2, cv2 = win._metrics()
        fr2 = win._frog_rect()
        band2 = QRect(win._band)
        rs2, _zn2 = win._toolbar()
        same = (g3 == g2 and cv3 == cv2 and fr3 == fr2 and b3 == b2
                and band3 == band2 and rs3 == rs2)
        rc |= 0 if same else 2
        log(f"  顶部贴边 3→2 行 窗口/画布/蛙/按钮全不动 {same} "
            f"{'ok' if same else '??'}")
        flush2 = (b2 is not None and band2.top() == b2.bottom() + 1
                  and band2.bottom() == _s2.height() - 1)
        rc |= 0 if flush2 else 2
        log(f"  气泡下沿接工具带、工具带铺到窗口底 {flush2} "
            f"{'ok' if flush2 else '??'}")

        # 吸附态悬停连续性：气泡 → 缝 → 按钮三步都亮着，中途误报 leave 也不灭
        win._set_tools(False)
        steps = [b2.center(), QPoint(b2.center().x(), b2.bottom() + 1),
                 rs2["play"].center()]
        chain = True
        for pt in steps:
            send(QEvent.Type.MouseMove, pt)
            chain &= win._tools_on
        QApplication.sendEvent(win, QEvent(QEvent.Type.Leave))
        grace = win._tools_on and win._leave_timer.isActive()
        send(QEvent.Type.MouseMove, rs2["play"].center())
        back = win._tools_on and not win._leave_timer.isActive()
        ok = chain and grace and back
        rc |= 0 if ok else 2
        log(f"  吸附态悬停链 三步常亮 {chain} / 误报宽限 {grace} / "
            f"回来续上 {back} {'ok' if ok else '??'}")

        # 收起气泡：按钮还在，同一颗能把自己展开回来
        win.set_bubble_visible(False)
        rs_c, _zn_c = win._toolbar()
        win._set_tools(True)
        win._tools_alpha = 1.0
        hit_c = bool(rs_c) and win._hit(rs_c["hide"].center()) == "tool:hide"
        ok_c = (len(rs_c) == 5 and hit_c
                and not bool(st.get("pet.show_bubble", True)))
        rc |= 0 if ok_c else 2
        log(f"  收起气泡 按钮仍可点 {ok_c} {'ok' if ok_c else '??'}")
        win.set_bubble_visible(True)

        # 吸附态收起/展开：画布（切口）一寸都不能挪
        win._edge_dir = ""
        win.set_text(t3)
        win._snap_to("s", QPoint(560, 740), animate=False, screen=mock)
        cg0 = win._metrics()[2].translated(win.geometry().topLeft())
        win.set_bubble_visible(False)
        cg1 = win._metrics()[2].translated(win.geometry().topLeft())
        win.set_bubble_visible(True)
        cg2 = win._metrics()[2].translated(win.geometry().topLeft())
        ok = cg0 == cg1 == cg2
        rc |= 0 if ok else 2
        log(f"  吸附态收起→展开 画布不挪 {ok} "
            + ("ok" if ok else f"?? {cg0} / {cg1} / {cg2}"))

        # 两个按钮的职责：一个收气泡、一个切主面板，都能来回拨
        class _FakeReader:
            def __init__(self, store, w):
                self.st, self._pet_window = store, w
                self.v, self.calls, self.hints = False, [], []

            def isVisible(self):
                return self.v

            def _hide_window(self):
                self.calls.append("hide")
                self.v = False

            def _do_show(self):
                self.calls.append("show")
                self.v = True

            def _set_hint(self, s, ms):
                self.hints.append(s)

        fake = _FakeReader(st, win)
        Reader._pet_toggle_main(fake)
        Reader._pet_toggle_main(fake)
        toggle_ok = fake.calls == ["show", "hide"]
        rc |= 0 if toggle_ok else 2
        log(f"  主面板按钮 显示→收起切换 {toggle_ok} "
            f"{'ok' if toggle_ok else '??'}")
        Reader._pet_collapse_bubble(fake)
        off = not bool(st.get("pet.show_bubble", True))
        Reader._pet_collapse_bubble(fake)
        on = bool(st.get("pet.show_bubble", True))
        ok = off and on
        rc |= 0 if ok else 2
        log(f"  收起按钮 收起→展开往返 {ok} {'ok' if ok else '??'}")

        # 顶部贴边工具条出图，供 README 引用
        win.set_text(t3)
        win._snap_to("n", QPoint(640, 40), animate=False, screen=mock)
        win._set_tools(True)
        win._tools_alpha = 1.0
        epm = win.grab()
        eb = QImage(epm.width() + 40, epm.height() + 40,
                    QImage.Format.Format_RGB32)
        eb.fill(QColor("#12161b"))
        ep = QPainter(eb)
        ep.drawPixmap(20, 20, epm)
        ep.end()
        eshot = here / "screenshots" / "pet-edge-tools.png"
        eshot = eshot if eshot.parent.is_dir() else (
            Path(tempfile.gettempdir()) / "interline_edge_tools.png")
        ok = bool(eb.save(str(eshot)))
        rc |= 0 if ok else 2
        log(f"  吸附工具条预览 = {eshot}" if ok else "  吸附工具条预览 = 保存失败")

        # 回站立形态，后面的播放/暂停图标与预览图仍按开头那套坐标验
        win._edge_dir = ""
        win.set_text(text)
        win._set_tools(True)
        win._tools_alpha = 1.0

        # 播放键要跟着朗读状态换图标：暂停双竖条 vs 播放三角
        pr = rects["play"]
        orig = win._speech_state
        win._speech_state = lambda: ("开始朗读", True)
        play_img = win.grab().toImage()
        win._speech_state = lambda: ("停止朗读", True)
        pause_img = win.grab().toImage()
        win._speech_state = orig
        diff = sum(1 for y in range(pr.top(), pr.bottom() + 1)
                   for x in range(pr.left(), pr.right() + 1)
                   if play_img.pixel(x, y) != pause_img.pixel(x, y))
        ok = diff >= 12
        rc |= 0 if ok else 2
        log(f"  播放/暂停图标切换 {diff} 像素 {'ok' if ok else '??'}")

        # 出图：真窗口抓下来铺到深色桌面上
        pm = win.grab()
        board = QImage(pm.width() + 40, pm.height() + 40,
                       QImage.Format.Format_RGB32)
        board.fill(QColor("#12161b"))
        bp = QPainter(board)
        bp.drawPixmap(20, 20, pm)
        bp.end()
        shot = here / "screenshots" / "pet-tools.png"
        shot = shot if shot.parent.is_dir() else (
            Path(tempfile.gettempdir()) / "interline_tools.png")
        ok = bool(board.save(str(shot)))
        rc |= 0 if ok else 2
        log(f"  工具条预览 = {shot}" if ok else "  工具条预览 = 保存失败")

        # 底栏蛙头按钮：深浅两套前景色各抓一张，放大 8 倍供肉眼回归
        ib = QImage(24 * 8 * 2 + 60, 20 * 8 + 40,
                    QImage.Format.Format_RGB32)
        ib.fill(QColor("#12161b"))
        ip = QPainter(ib)
        orig_fg = st.get("colors.fg", "#c3cbd6")
        for i, (fg, bgc) in enumerate((("#c3cbd6", "#252a31"),
                                       ("#2b3038", "#e8e4da"))):
            st.set("colors.fg", fg)
            b = FrogButton(st)
            b.setFixedSize(24, 20)
            b.setStyleSheet(
                f"QPushButton#footBtn {{ background:{bgc}; border:none; "
                f"border-radius:5px; }}")
            ip.drawPixmap(20 + i * (24 * 8 + 20), 20,
                          b.grab().scaled(24 * 8, 20 * 8))
            b.deleteLater()
        ip.end()
        st.set("colors.fg", orig_fg)     # 预览只是换色试画，别把残值留给后面的自检
        shot2 = here / "screenshots" / "pet-footbtn.png"
        shot2 = shot2 if shot2.parent.is_dir() else (
            Path(tempfile.gettempdir()) / "interline_footbtn.png")
        ok = bool(ib.save(str(shot2)))
        rc |= 0 if ok else 2
        log(f"  蛙头按钮预览 = {shot2}" if ok else "  蛙头按钮预览 = 保存失败")

        # 熄灭后按钮不该再吃点击
        win._set_tools(False)
        ok = win._hit(rects["play"].center()) != "tool:play"
        rc |= 0 if ok else 2
        log(f"  熄灭后不吃点击 {ok} {'ok' if ok else '??'}")

        # ---- v60：小缩放 / 气泡收起时五颗按钮不重叠、点哪颗就是哪颗
        # 真实翻车场景：pet.scale=0.6 时站立帧只有 115px 宽，左三的 next
        # (60..81) 与右二的 hide (61..82) 叠在一起，后画的展开按钮盖住
        # 「下一句」（看着少一颗），命中却先撞上 next（点展开变下一句）。
        def check_toolbar(tag):
            sz, _bub, _fr = win._metrics()
            rs, _zn = win._toolbar()
            ks = list(rs)
            ov = [(a, b) for i, a in enumerate(ks) for b in ks[i + 1:]
                  if rs[a].intersects(rs[b])]
            inside = all(0 <= r.left() and r.right() < sz.width()
                         and 0 <= r.top() and r.bottom() < sz.height()
                         for r in rs.values())
            hits = {k: win._hit(r.center()) == "tool:" + k
                    for k, r in rs.items()}
            good = (len(rs) == 5 and not ov and inside and all(hits.values()))
            if not good:
                log(f"    {tag} 窗口{sz.width()}x{sz.height()} 按钮{len(rs)} "
                    f"重叠{ov} 窗内{inside} 命中{hits}")
            return good

        layout_ok = True
        for sc, shown in ((0.5, False), (0.6, False), (0.6, True),
                          (0.9, False), (0.9, True), (1.4, False),
                          (2.0, True)):
            win._edge_dir = ""
            st.set("pet.scale", sc)
            win.set_scale(sc)
            win.set_bubble_visible(shown)
            win._set_tools(True)
            win._tools_alpha = 1.0
            layout_ok &= check_toolbar(f"scale={sc} 气泡={shown}")
        rc |= 0 if layout_ok else 2
        log(f"  7 档缩放×气泡显隐 五颗不重叠且命中一致 {layout_ok} "
            f"{'ok' if layout_ok else '??'}")

        # 吸附态八向、小缩放、气泡显隐两态也要排得下五颗
        st.set("pet.scale", 0.6)
        win.set_scale(0.6)
        snap_ok = True
        for shown in (False, True):
            win.set_bubble_visible(shown)
            for d, a in anchors.items():
                win._edge_dir = ""
                win._snap_to(d, a, animate=False, screen=mock)
                win._set_tools(True)
                win._tools_alpha = 1.0
                snap_ok &= check_toolbar(f"{d} 气泡={shown}")
        rc |= 0 if snap_ok else 2
        log(f"  八向×收起/展开 五颗不重叠且命中一致 {snap_ok} "
            f"{'ok' if snap_ok else '??'}")

        # 真鼠标点「展开」按钮：命中 hide、发出 collapse_bubble、气泡回来
        win._edge_dir = ""
        st.set("pet.scale", 0.6)
        win.set_scale(0.6)
        win.set_bubble_visible(False)
        win._set_tools(True)
        win._tools_alpha = 1.0
        rs_c, _zn_c = win._toolbar()
        center = rs_c["hide"].center()
        send(QEvent.Type.MouseMove, center)
        hover_ok = win._tool_hover == "hide"
        got.clear()
        send(QEvent.Type.MouseButtonPress, center,
             Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
        send(QEvent.Type.MouseButtonRelease, center,
             Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
        fired = tuple(got) == (("hide",),)
        fake = _FakeReader(st, win)
        Reader._pet_collapse_bubble(fake)
        expanded = bool(st.get("pet.show_bubble", True))
        _sz2, bub2, _fr2 = win._metrics()
        five = len(win._toolbar()[0]) == 5
        ok = hover_ok and fired and expanded and bub2 is not None and five
        rc |= 0 if ok else 2
        log(f"  小缩放收起态点展开 悬停{hover_ok} / 发信号{fired} / "
            f"气泡回来{expanded and bub2 is not None} / 五颗{five} "
            f"{'ok' if ok else '??'}")
        st.set("pet.scale", 0.9)
        win.set_scale(0.9)
        win.set_bubble_visible(True)

        # ---- v61：悬停气泡时，工具带上显示本章进度
        # 只验绘制与落位；进度数值本身由主窗给，蛙不认识书。
        # 中段不是空位：尾巴占上半、蛙头占下半，所以必须验三者都不压。
        def head_box():
            h = win._frog_rect()
            g = win.pos()                # _frog_rect 给全局坐标
            x = round(h.left() - g.x() + h.width() * 0.30)
            return QRect(x, h.top() - g.y(),
                         round(h.width() * 0.40), h.height())

        def tail_box():
            _sz, bub, _fr = win._metrics()
            tail = win._tail[0] if win._edge_dir else "down"
            if (bub is None or win._band.isEmpty() or tail != "down"
                    or win._band.top() < bub.bottom() - 1):
                return QRect()
            return QRect(round(bub.center().x() - pet.BUBBLE_TAIL),
                         win._band.top(), 2 * pet.BUBBLE_TAIL,
                         pet.BUBBLE_TAIL)

        def label_clear(lab, rr):
            if lab.isEmpty():
                return True              # 太窄就干脆不画，也算合规
            return (not any(lab.intersects(r) for r in rr.values())
                    and not lab.intersects(head_box())
                    and not lab.intersects(tail_box())
                    and QRect(win._band).contains(lab))

        win._progress_state = lambda: (42, 3, 12)
        win._set_tools(True)
        win._tools_alpha = 1.0
        rs_p, _zn_p = win._toolbar()
        lab, ptext = win._progress_label(rs_p)
        clear = label_clear(lab, rs_p)
        ok = not lab.isEmpty() and "42%" in ptext and clear
        rc |= 0 if ok else 2
        log(f"  进度标 文案{ptext!r} 避开尾巴/蛙头/按钮 {clear} "
            f"{'ok' if ok else '??'}")

        # 真画出来：同一窗口开/关进度标，底片那块得有像素变化
        with_img = win.grab().toImage()
        win._progress_state = lambda: None
        without_img = win.grab().toImage()
        pdiff = sum(1 for y in range(lab.top(), lab.bottom() + 1)
                    for x in range(lab.left(), lab.right() + 1)
                    if with_img.pixel(x, y) != without_img.pixel(x, y))
        ok = pdiff >= 30
        rc |= 0 if ok else 2
        log(f"  进度标真的画出来 {pdiff} 像素 {'ok' if ok else '??'}")

        # 八向吸附态：工具带一样是横带，进度标也得放得下且不压任何东西
        win._progress_state = lambda: (42, 3, 12)
        pedge = True
        for d, a in anchors.items():
            win._edge_dir = ""
            win._snap_to(d, a, animate=False, screen=mock)
            rr, _z = win._toolbar()
            pl, pt = win._progress_label(rr)
            good = (not pl.isEmpty() and "42%" in pt
                    and label_clear(pl, rr))
            if not good:
                hb, tb = head_box(), tail_box()
                log(f"    {d} 文案{pt!r} 标{pl.getRect()} 带{win._band.getRect()}"
                    f" 头{hb.getRect()} 尾{tb.getRect()}"
                    f" 撞按钮{[k for k, r in rr.items() if pl.intersects(r)]}"
                    f" 撞头{pl.intersects(hb)} 撞尾{pl.intersects(tb)}")
            pedge &= good
        rc |= 0 if pedge else 2
        log(f"  八向吸附 进度标放得下且不压东西 {pedge} "
            f"{'ok' if pedge else '??'}")

        # 用户实际配置：260px + 0.6 倍，应该退化成只剩百分比
        win._edge_dir = ""
        st.set("pet.bubble_w", 260)
        st.set("pet.scale", 0.6)
        win.set_scale(0.6)
        win.set_text(text)
        rr, _z = win._toolbar()
        pl, pt = win._progress_label(rr)
        narrow = (not pl.isEmpty() and "%" in pt and label_clear(pl, rr))
        rc |= 0 if narrow else 2
        log(f"  260px×0.6 倍 退化成 {pt!r} 且不压东西 {narrow} "
            f"{'ok' if narrow else '??'}")

        # 最窄 200px：宁可不画，也不能压蛙头 / 尾巴 / 按钮
        st.set("pet.bubble_w", 200)
        win.set_scale(0.6)
        win.set_text(text)
        rr, _z = win._toolbar()
        pl, pt = win._progress_label(rr)
        tiny = label_clear(pl, rr)
        rc |= 0 if tiny else 2
        shown = repr(pt) if pt else "不画"
        log(f"  200px 极窄 {shown} 仍不压东西 {tiny} "
            f"{'ok' if tiny else '??'}")

        # 出图：站立态点亮工具条 + 进度标，供 README 引用
        st.set("pet.bubble_w", 360)
        st.set("pet.scale", 0.9)
        win.set_scale(0.9)
        win.set_text(text)
        win._set_tools(True)
        win._tools_alpha = 1.0
        pm_p = win.grab()
        pb = QImage(pm_p.width() + 40, pm_p.height() + 40,
                    QImage.Format.Format_RGB32)
        pb.fill(QColor("#12161b"))
        pp = QPainter(pb)
        pp.drawPixmap(20, 20, pm_p)
        pp.end()
        pshot = here / "screenshots" / "pet-progress.png"
        pshot = pshot if pshot.parent.is_dir() else (
            Path(tempfile.gettempdir()) / "interline_progress.png")
        ok = bool(pb.save(str(pshot)))
        rc |= 0 if ok else 2
        log(f"  进度标预览 = {pshot}" if ok else "  进度标预览 = 保存失败")

        # 设置页「奶蛙」回归图：v61 把「奶读模式」说明 + 两颗按钮整段删了。
        # 用假 Reader 抓真对话框（不 show，grab 照样能渲染），顺带断言
        # 分组里再没有那个过期的标题。
        try:
            import settings_dialog as sd

            class _SpeechStub:
                edge_available = False

                def voice_choices(self):
                    return [("系统 默认", "", "sys")]

                def current_voice(self):
                    return ""

                def output_devices(self):
                    return []

            class _FakeSettingsReader(QWidget):
                def __init__(self, cfg):
                    super().__init__()
                    self.st = cfg
                    self.speech = _SpeechStub()
                    self.boss_key = "Ctrl+Alt+D"
                    self.font_panel = None

                def apply_skin(self, key): pass
                def reload_shortcuts(self): pass
                def pet_apply_scale(self, v): pass
                def pet_relayout(self): pass
                def pet_unsnap(self): pass

            fake_r = _FakeSettingsReader(st)
            sdlg = sd.SettingsDialog(fake_r)
            sdlg.nav.setCurrentRow(3)      # 「奶蛙」页
            sdlg.resize(660, 607)
            sdlg.hide()
            QApplication.processEvents()
            labels = [w.text() for w in
                      sdlg.pages.widget(3).findChildren(QLabel)
                      if w.objectName() in ("section", "pageTitle")]
            gone = "奶读模式" not in labels
            sshot = here / "screenshots" / "settings-pet.png"
            sshot = sshot if sshot.parent.is_dir() else (
                Path(tempfile.gettempdir()) / "interline_settings_pet.png")
            saved = bool(sdlg.grab().save(str(sshot), "PNG"))
            ok = gone and saved
            rc |= 0 if ok else 2
            log(f"  设置页 分组{labels} 无奶读模式{gone} 出图{saved} "
                f"{'ok' if ok else '??'}")
            sdlg.close()
            fake_r.deleteLater()
        except Exception as e:
            log(f"  设置页 FAIL {type(e).__name__}: {e}")
            rc |= 2

        # 没开书时不该冒出一个 0% 的标签
        win._progress_state = lambda: None
        ok = win._progress_label(win._toolbar()[0])[0].isEmpty()
        rc |= 0 if ok else 2
        log(f"  无书时不出进度标 {ok} {'ok' if ok else '??'}")

        # ---- v67：一行模式的悬浮工具带
        # 只画这个控件，不构造真 Reader —— Reader.__init__ 会拉起朗读链路，
        # 跟上面这一堆 QPixmap / grab 混在一个进程里跑不稳。
        # 主窗接线单独用鸭子对象调未绑定方法，同样不碰 Qt 对象构造。
        try:
            import one_line_tools as olt

            from PyQt6.QtGui import QMouseEvent
            state = {"playing": True, "prog": (42, 3, 12)}
            band = olt.OneLineTools(st, lambda: (state["playing"],
                                                 state["prog"]))
            band.show()
            band.resize(360, olt.TOOL_BAND)
            band._force_visible()       # 自检跳过淡入，直接全亮方便抓图比对
            band.activate()             # 真鼠标链要收事件，得先激活
            rs = band.layout_buttons()
            ids = tuple(rs)
            want = olt.LEFT + olt.RIGHT
            ok = ids == want
            rc |= 0 if ok else 2
            log(f"  一行工具带 五颗按钮 {ids} {'ok' if ok else '?? 期望 ' + str(want)}")

            # 左三右二按序排开、互不重叠、全部落在带内
            ordered = all(rs[a].right() < rs[b].left()
                          for a, b in zip(ids, ids[1:]))
            frame = QRect(0, 0, band.width(), band.height())
            inside = all(frame.contains(r) for r in rs.values())
            minw_ok = (olt.TOOL_MIN_W >=
                       len(want) * olt.TOOL_BTN
                       + (len(olt.LEFT) - 1 + len(olt.RIGHT) - 1)
                       * olt.TOOL_GAP + 2 * olt.TOOL_INSET)
            ok = ordered and inside and minw_ok
            rc |= 0 if ok else 2
            log(f"  一行工具带 顺序{ordered} 带内{inside} "
                f"最小宽够用{minw_ok} {'ok' if ok else '??'}")

            # 命中：五个中心各归各位，中间空白谁也不吃
            hits = {k: band.tool_at(r.center()) for k, r in rs.items()}
            ok = (all(v == k for k, v in hits.items())
                  and band.tool_at(QPoint(band.width() // 2,
                                          olt.TOOL_Y_GAP + 2)) == "")
            rc |= 0 if ok else 2
            log(f"  一行工具带 命中 {hits} 空白不误命中 "
                f"{'ok' if ok else '??'}")

            # 进度标：文案带百分比，不压任何按钮
            lab, ptext = band.progress_label(rs)
            ok = (not lab.isEmpty() and "42%" in ptext and frame.contains(lab)
                  and not any(lab.intersects(r) for r in rs.values()))
            rc |= 0 if ok else 2
            log(f"  一行工具带 进度标 {ptext!r} 不压按钮 "
                f"{'ok' if ok else '??'}")

            # 真画出来：开关进度标，那块底片得有像素变化
            with_img = band.grab().toImage()
            state["prog"] = None
            QApplication.processEvents()
            without_img = band.grab().toImage()
            pdiff = sum(1 for y in range(lab.top(), lab.bottom() + 1)
                        for x in range(lab.left(), lab.right() + 1)
                        if with_img.pixel(x, y) != without_img.pixel(x, y))
            state["prog"] = (42, 3, 12)
            ok = pdiff >= 30
            rc |= 0 if ok else 2
            log(f"  一行工具带 进度标真的画出来 {pdiff} 像素 "
                f"{'ok' if ok else '??'}")

            # 播放图标跟着朗读状态换：只比较 play 那颗按钮的范围
            prect = rs["play"]
            play_img = band.grab().toImage()
            state["playing"] = False
            QApplication.processEvents()
            pause_img = band.grab().toImage()
            idiff = sum(1 for y in range(prect.top(), prect.bottom() + 1)
                        for x in range(prect.left(), prect.right() + 1)
                        if play_img.pixel(x, y) != pause_img.pixel(x, y))
            state["playing"] = True
            ok = idiff >= 8
            rc |= 0 if ok else 2
            log(f"  一行工具带 播放/暂停图标有区别 {idiff} 像素 "
                f"{'ok' if ok else '??'}")

            # 真鼠标链：按下→松开触发一次；按在空白上要 ignore 掉让主窗拖窗
            from PyQt6.QtCore import QPointF

            def osend(t, pos, btn=Qt.MouseButton.NoButton,
                      buttons=Qt.MouseButton.NoButton):
                ev = QMouseEvent(t, QPointF(pos), QPointF(pos), btn, buttons,
                                 Qt.KeyboardModifier.NoModifier)
                QApplication.sendEvent(band, ev)
                return ev.isAccepted()

            got = []
            band.triggered.connect(got.append)
            c = rs["next"].center()
            pressed = osend(QEvent.Type.MouseButtonPress, c,
                            Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton)
            osend(QEvent.Type.MouseButtonRelease, c,
                  Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
            blank = osend(QEvent.Type.MouseButtonPress,
                          QPoint(band.width() // 2, olt.TOOL_Y_GAP + 2),
                          Qt.MouseButton.LeftButton,
                          Qt.MouseButton.LeftButton)
            osend(QEvent.Type.MouseButtonRelease,
                  QPoint(band.width() // 2, olt.TOOL_Y_GAP + 2),
                  Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
            ok = pressed and tuple(got) == ("next",) and not blank
            rc |= 0 if ok else 2
            log(f"  一行工具带 真鼠标链 按下{pressed} 发出{got} "
                f"空白让出{not blank} {'ok' if ok else '??'}")

            # 收到最窄宽度：按钮照样齐全，进度标退化或不画，谁都不许叠
            band.resize(olt.TOOL_MIN_W, olt.TOOL_BAND)
            rs2 = band.layout_buttons()
            frame2 = QRect(0, 0, band.width(), band.height())
            lab2, pt2 = band.progress_label(rs2)
            ok2 = (all(frame2.contains(r) for r in rs2.values())
                   and all(rs2[a].right() < rs2[b].left()
                           for a, b in zip(tuple(rs2), tuple(rs2)[1:]))
                   and not any(lab2.intersects(r) for r in rs2.values()))
            rc |= 0 if ok2 else 2
            log(f"  一行工具带 {olt.TOOL_MIN_W}px 极窄 进度标"
                f"{repr(pt2) if pt2 else '不画'} 按钮不叠 {ok2} "
                f"{'ok' if ok2 else '??'}")

            # 主窗接线：不构造 Reader，鸭子对象直接调未绑定方法
            calls = []

            class _FakeReader:
                _one_line = True

                def _step_one_line(self, d): calls.append(("step", d))
                def _toggle_speech(self): calls.append(("play",))
                def _hide_window(self): calls.append(("hide",))
                def _exit_one_line(self): calls.append(("main",))

            fake = _FakeReader()
            for k in want:
                Reader._on_one_line_tool(fake, k)
            ok = calls == [("step", -1), ("play",), ("step", 1),
                           ("hide",), ("main",)]
            fake._one_line = False
            Reader._on_one_line_tool(fake, "next")
            ok = ok and len(calls) == 5
            rc |= 0 if ok else 2
            log(f"  一行工具带 主窗接线 {calls} 非一行模式不响应 "
                f"{'ok' if ok else '??'}")

            # 出图：给 README 一行模式那节引用
            band.resize(360, olt.TOOL_BAND)
            QApplication.processEvents()
            pm = band.grab()
            canvas = QImage(pm.width() + 40, pm.height() + 40,
                            QImage.Format.Format_RGB32)
            canvas.fill(QColor("#12161b"))
            pc = QPainter(canvas)
            pc.drawPixmap(20, 20, pm)
            pc.end()
            shot = here / "screenshots" / "one-line-tools.png"
            shot = shot if shot.parent.is_dir() else (
                Path(tempfile.gettempdir()) / "interline_one_line_tools.png")
            saved = bool(canvas.save(str(shot)))
            rc |= 0 if saved else 2
            log(f"  一行工具带 预览 {shot} {'ok' if saved else '?? 保存失败'}")
            band.deleteLater()
        except Exception as e:
            import traceback
            log(f"  一行工具带 FAIL {type(e).__name__}: {e}")
            log(traceback.format_exc())
            rc |= 2

        log("RESULT = OK")
    except Exception as e:
        import traceback
        log(f"RESULT = FAIL {type(e).__name__}: {e}")
        log(traceback.format_exc())
        rc = 1
    finally:
        if win is not None:
            win.stop()
    return rc


def edgecheck() -> int:
    """打包后验证贴边吸附：方向判定、摆位、真窗口贴边截图。

    和 --petcheck 一样单独跑，全程不碰 TTS：那边一建 QTextToSpeech("winrt")，
    之后再碰 QPixmap 就是原生崩溃（0xC0000409），连日志都来不及写。

    为什么要真窗口而不只算坐标：吸附的全部风险都在「切口有没有真的压在
    屏幕边上」。坐标算对、画的时候差一像素，看坐标是看不出来的。
    """
    import tempfile

    lines: list[str] = []
    out = Path(tempfile.gettempdir()) / "interline_edgecheck.txt"
    here = Path(__file__).resolve().parent
    # 源码跑时落在 screenshots/ 供 README 引用；打包版 __file__ 在临时解包目录里，
    # 那儿没有 screenshots/，退回临时目录（和 --petcheck 一样）
    png = here / "screenshots" / "pet-snap.png"

    def log(msg: str):
        lines.append(str(msg))
        try:
            out.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    # 别污染真实配置，也别读到别的自检写下的残值：Store 在构造时就按这个变量定目录
    check_dir = _check_data_dir("edges")

    rc = 0
    app = QApplication.instance() or QApplication(sys.argv[:1])
    try:
        import pet_edge
        import pet_window as pw
        import store as store_mod

        st = store_mod.Store()
        st.set("pet.snap_dir", "")      # 从「没贴过」开始，别让上次的边干扰
        log(f"version = {BUILD_VERSION}")
        log(f"data dir = {check_dir}（每次清空，不读用户配置）")

        # ---- 1. 纯逻辑：方向判定
        scr = QRect(0, 0, 1280, 780)
        fw, fh = 173, 187
        cases = [
            ("左上角", 8, 8, "nw"), ("上边中", 600, 8, "n"),
            ("偏上 11px", 600, 11, ""), ("偏上 10px", 600, 10, "n"),
            ("右上角", 1280 - fw - 6, 5, "ne"), ("左边中", 4, 400, "w"),
            ("右边中", 1280 - fw - 3, 400, "e"), ("左下角", 8, 780 - fh - 4, "sw"),
            ("下边中", 500, 780 - fh - 10, "s"), ("右下角", 1280 - fw - 2, 780 - fh - 2, "se"),
            ("偏左 11px", 11, 400, ""), ("正好 10px", 10, 400, "w"),
            ("偏左 9px", 9, 400, "w"),
            ("正中", 500, 300, ""), ("拖出屏幕左", -50, 400, "w"),
        ]
        for name, x, y, want in cases:
            got = pet_edge.pick_dir(QRect(x, y, fw, fh), scr)
            ok = got == want
            rc |= 0 if ok else 2
            log(f"  {name:<10} ({x:>4},{y:>3}) -> {got or '不吸':<3} "
                f"{'ok' if ok else '?? 期望 ' + (want or '不吸')}")
        # 多屏：右邻有屏时右边不吸
        got = pet_edge.pick_dir(QRect(1280 - fw - 3, 400, fw, fh), scr,
                                blocked=frozenset({"R"}))
        ok = got != "e"
        rc |= 0 if ok else 2
        log(f"  {'右边是相邻屏':<10} -> {got or '不吸':<3} {'ok' if ok else '?? 不该吸'}")

        # ---- 2. 真窗口：八个方向各贴一次，抓图
        win = pw.PetWindow(st, lambda: QFont(store_mod.UI_FONT),
                           lambda: ("开始朗读", False))
        if not win.start("贴边自检"):
            log("RESULT = FAIL 奶蛙素材读不出来")
            return 2
        win.hide()                       # 只借它的绘制，不真弹到桌面上
        if not win.edge.ok:
            log("RESULT = FAIL 贴边素材缺 meta.json")
            return 2

        mock = QRect(0, 0, 1280, 780)
        size = win.edge.size()
        anchors = {
            "nw": QPoint(60, 60), "n": QPoint(640, 40), "ne": QPoint(1220, 60),
            "w": QPoint(40, 400), "e": QPoint(1240, 300),
            "sw": QPoint(60, 720), "s": QPoint(560, 740), "se": QPoint(1220, 720),
        }
        shots = {}
        log(f"  画布 {size.width()}x{size.height()}，缩放 "
            f"{win.edge.scale:.2f}")
        for d, a in anchors.items():
            if not win._snap_to(d, a, animate=False, screen=mock):
                log(f"  {d:<3} 贴不上")
                rc = 2
                continue
            g = win.geometry()
            _sz, bubble, cv = win._metrics()
            # 吸附态窗口带着气泡，贴着屏幕边的是**画布**，不是窗口
            cg = cv.translated(g.topLeft())
            cut = win.edge.geo(d)["cut"]
            edge = {"left": cg.left() == mock.left(),
                    "right": cg.right() == mock.right(),
                    "top": cg.top() == mock.top(),
                    "bottom": cg.bottom() == mock.bottom()}[cut]
            # 抓图后按 alpha 再验一次：像素真的压在切口那条边上，不是只算了坐标
            img = win.grab().toImage()
            if cut in ("left", "right"):
                col = cv.left() if cut == "left" else cv.right()
                ink = any(qAlpha(img.pixel(col, y)) > 8
                          for y in range(cv.top(), cv.bottom() + 1))
            else:
                row = cv.top() if cut == "top" else cv.bottom()
                ink = any(qAlpha(img.pixel(x, row)) > 8
                          for x in range(cv.left(), cv.right() + 1))
            # 气泡得在屏幕内侧，而且不能压住蛙（气泡是贴在蛙可见那一侧画的，
            # 画布本身有一半是透明的，所以判据看蛙矩形、不看画布）
            fr = win._frog_rect()
            bub = True
            if bubble is None:
                bub = False
                log("     ?? 吸附态没有气泡")
            else:
                body = bubble.translated(g.topLeft())
                bub = mock.contains(body) and not body.intersects(fr)
                if not bub:
                    log(f"     ?? 气泡 {body} 越界或压住了蛙 {fr}")
            ok = edge and ink and bub
            rc |= 0 if ok else 2
            log(f"  {d:<3} 窗口 ({g.x():>4},{g.y():>3}) 画布 ({cg.x():>4},{cg.y():>3}) "
                f"切口={cut:<6} {'齐平' if edge else '离边'}/"
                f"{'有像素' if ink else '空的!'}/{'气泡' if bub else '无气泡'} "
                f"{'ok' if ok else '??'}")
            shots[d] = (win.grab(), QPoint(g.x(), g.y()))

        # ---- 3. 换相位不许动窗口：抬手帧比基准宽 35px，位置一变就前功尽弃
        base_geo = None
        for d in ("w", "n"):
            win._snap_to(d, anchors[d], animate=False, screen=mock)
            g0 = win.geometry()
            for on in (True, False):
                win.edge_state.set_hover(on)
                win.edge_state.tick(9999)      # 顺手把眨眼也过一遍
            g1 = win.geometry()
            ok = g0 == g1
            rc |= 0 if ok else 2
            log(f"  {d:<3} 换相位窗口 "
                f"{'没动' if ok else f'动了 {g0} -> {g1}'} {'ok' if ok else '??'}")
            base_geo = base_geo or g0

        # ---- 3.5 朗读口型：嘴位齐、时钟会开合、真画上去不改几何
        import pet as pet_mod

        mouths = {}
        for d in anchors:
            m = (win.edge.geo(d) or {}).get("mouth")
            mouths[d] = m
            if not m and win.edge.pose_count(d):
                # s 换成多姿态后没有统一嘴位：不画口型，不算失败
                log(f"  嘴位 {d:<3} 多姿态，无口型（跳过）")
                continue
            ok = bool(m) and len(m) == 3 and 0 <= m[0] <= win.edge.canvas()
            rc |= 0 if ok else 2
            log(f"  嘴位 {d:<3} {m} {'ok' if ok else '??'}")

        es = pet_edge.EdgeState()
        es.set_reading(True)
        seen = set()
        for _ in range(24):
            es.tick(100)
            seen.add(es.mouth_shape)
        es.reset()                       # 吸附换边会 reset，朗读状态不能丢
        kept = es._reading
        es.set_reading(False)
        closed = es.mouth_shape == (0, 0)
        ok = len(seen) >= 4 and kept and closed
        rc |= 0 if ok else 2
        log(f"  口型时钟 {len(seen)} 种形状 / reset 保留朗读 {kept} / "
            f"停下闭嘴 {closed} {'ok' if ok else '??'}")

        # 张嘴时再验一次：窗口几何一像素都不该动，且口型必须真的画在
        # 「画布坐标 + 画布偏移」× scale 算出来的那个位置。v53 在这套
        # 坐标系上栽过，光看窗口没动不够
        win._snap_to("e", anchors["e"], animate=False, screen=mock)
        g0 = win.geometry()
        _sz, _bub, cv = win._metrics()
        win.edge_state.set_reading(True)
        win.edge_state._mouth = (0, 0)
        shut = win.grab().toImage()
        win.edge_state._mouth = (14, 5)
        spoken = win.grab().toImage()
        ok = g0 == win.geometry()
        rc |= 0 if ok else 2
        log(f"  张嘴后窗口 {'没动' if ok else '动了'} {'ok' if ok else '??'}")

        m = mouths["e"]
        k = win.edge.scale
        px = round(cv.left() + m[0] * k)
        py = round(cv.top() + m[1] * k)
        diff = 0
        for dy in range(-4, 7):
            for dx in range(-10, 11):
                x, y = px + dx, py + dy
                if (0 <= x < shut.width() and 0 <= y < shut.height()
                        and qAlpha(shut.pixel(x, y)) > 8
                        and shut.pixel(x, y) != spoken.pixel(x, y)):
                    diff += 1
        ok = diff >= 8
        rc |= 0 if ok else 2
        log(f"  真窗口张嘴 嘴部变化 {diff} 像素 {'ok' if ok else '??'}")

        # 口型预览：每方向闭口 / 张口并排，走的就是运行时那个绘制函数。
        # 格子和缩放必须跟运行时一致——素材图是 256*k 的，嘴位也要乘 k；
        # 早先图按 256 的格子摆、嘴按 1.0 画，缩放 0.9 时口型会整体
        # 偏到嘴线下方十几像素，看着像掉出脸外（实际运行窗口没这问题）。
        k = win.edge.scale
        c = win.edge.size().width()          # = round(canvas * k)
        board = QImage(c * 4, c * 4, QImage.Format.Format_ARGB32)
        board.fill(QColor("#12161b"))
        bp = QPainter(board)
        for i, d in enumerate(("n", "ne", "e", "se", "s", "sw", "w", "nw")):
            row, col = divmod(i, 2)
            ox, oy = col * c * 2, row * c
            pm = win.edge.pixmap(d, "base")
            if pm is None or pm.isNull():
                continue
            bp.drawPixmap(ox, oy, pm)
            bp.drawPixmap(ox + c, oy, pm)
            m = mouths.get(d)
            if m:
                pet_mod.draw_edge_mouth(bp, QPoint(ox + c, oy), k,
                                        m[0], m[1], m[2], 14, 4)
        bp.end()

        # 回归护栏：光「嘴部有像素变化」不够，得验变化确实落在嘴线上。
        # 预览早先把素材按 0.9 画、嘴按 1.0 画，窗口几何和像素差断言
        # 全过，肉眼一看深色块掉到脸外。这里直接比对闭口/张口两格的
        # 差异质心和预期嘴位，缩放再改也能当场抓出来。
        import math as _math

        n_mouth = sum(1 for _d in ("n", "ne", "e", "se", "s", "sw", "w", "nw")
                      if mouths.get(_d))
        placed = 0
        for i, d in enumerate(("n", "ne", "e", "se", "s", "sw", "w", "nw")):
            row, col = divmod(i, 2)
            ox, oy = col * c * 2, row * c
            m = mouths.get(d)
            if not m:
                continue
            rad = _math.radians(m[2])
            ex = (m[0] - _math.sin(rad) * 4 * 0.45) * k
            ey = (m[1] + _math.cos(rad) * 4 * 0.45) * k
            n, sx, sy = 0, 0, 0
            for yy in range(max(0, round(ey) - 18), min(c, round(ey) + 19)):
                for xx in range(max(0, round(ex) - 20), min(c, round(ex) + 21)):
                    if (board.pixel(ox + xx, oy + yy)
                            != board.pixel(ox + c + xx, oy + yy)):
                        n += 1
                        sx += xx
                        sy += yy
            dx = sx / n - ex if n else 999
            dy = sy / n - ey if n else 999
            hit = n >= 20 and abs(dx) <= 6 and abs(dy) <= 6
            placed += bool(hit)
            log(f"  口型落位 {d:<3} 差 {n:3d} 像素 "
                f"偏移 ({dx:+.1f},{dy:+.1f}) {'ok' if hit else '??'}")
        ok = n_mouth > 0 and placed == n_mouth
        rc |= 0 if ok else 2
        log(f"  口型落位 {placed}/{n_mouth} 向贴在嘴线上 {'ok' if ok else '??'}")

        m_png = here / "screenshots" / "pet-edge-reading.png"
        dm = (m_png if m_png.parent.is_dir()
              else Path(tempfile.gettempdir()) / "interline_edge_mouth.png")
        ok = bool(board.save(str(dm)))
        log(f"  口型预览 = {dm}" if ok else "  口型预览 = 保存失败")
        if not ok:
            rc = 2

        # ---- 4. 出图：把八只拼到一块假桌面上，供肉眼回归
        bg = QImage(mock.size(), QImage.Format.Format_RGB32)
        bg.fill(QColor("#12161b"))
        p = QPainter(bg)
        p.fillRect(QRect(200, 96, 880, 560), QColor("#0d0f12"))
        p.setPen(QColor("#2a3038"))
        p.drawRect(QRect(200, 96, 880, 560))
        p.setPen(QColor("#59626f"))
        p.drawText(QRect(200, 66, 880, 24), Qt.AlignmentFlag.AlignCenter,
                   "贴边吸附：画布贴屏幕边的那条边就是切口")
        for d, (pm, pos) in shots.items():
            p.drawPixmap(pos, pm)
            p.setPen(QColor("#7c8794"))
            p.drawText(pos.x() + 8, pos.y() + 16, d)
        p.end()
        dst = png if png.parent.is_dir() else Path(tempfile.gettempdir()) / "interline_snap.png"
        ok = bool(bg.save(str(dst)))
        if not ok:
            dst = Path(tempfile.gettempdir()) / "interline_snap.png"
            ok = bool(bg.save(str(dst)))
        log(f"render = {dst}" if ok else "render = 保存失败")
        if not ok:
            rc = 2

        win.stop()
        log("RESULT = " + ("OK" if rc == 0 else f"FAIL({rc})"))
    except Exception as e:
        import traceback
        log(f"RESULT = FAIL {type(e).__name__}: {e}")
        log(traceback.format_exc())
        rc = 1
    return rc


def pdfcheck(path: str) -> int:
    """打包后验证 PDF 解析：cmap 数据表如果没被收进 exe，中文会解析失败。

    打包版是 console=False，print 看不到，结果写临时文件。
    """
    import tempfile

    out: list[str] = []
    rc = 1
    try:
        b = book_parser.load_book(path)
        out.append(f"chapters = {len(b.chapters)}")
        out.append(f"pages    = {getattr(b, 'total_pages', 0)}")
        idx = 1 if len(b.chapters) > 1 else 0
        c = b.chapters[idx]
        t = c.text
        out.append(f"chapter  = {c.title}")
        out.append(f"chars    = {len(t)}")
        out.append(f"head     = {t[:100]}")
        # 中文占比低说明 cmap 缺失、解出来是乱码
        han = sum(1 for ch in t[:500] if "\u4e00" <= ch <= "\u9fff")
        out.append(f"han/500  = {han}")
        rc = 0 if han > 50 else 1
    except Exception as e:      # noqa: BLE001
        out.append(f"FAIL = {type(e).__name__}: {e}")

    try:
        dst = os.path.join(tempfile.gettempdir(), "interline_pdfcheck.txt")
        with open(dst, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
    except Exception:
        pass
    return rc


def install_excepthook(get_win):
    """槽里抛未捕获异常时别让进程 abort。

    PyQt6 默认会把这类异常交给 qFatal()，表现就是「点一下滑块整个程序没了」，
    连报错都看不到（打包版 console=False，stderr 是空的）。
    装上自己的钩子后进程继续跑，堆栈落到 <数据目录>/error.log。
    """
    def hook(t, v, tb):
        text = "".join(traceback.format_exception(t, v, tb))
        try:
            path = store.app_dir() / "error.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + text + "\n")
        except Exception:
            pass
        # 打包版没有 stderr，往里写会再抛一次
        try:
            if sys.stderr is not None:
                sys.stderr.write(text)
                sys.stderr.flush()
        except Exception:
            pass
        try:
            w = get_win()
            if w is not None:
                w._set_hint("刚才那一步出错了，详情记在 error.log", 5000)
        except Exception:
            pass

    sys.excepthook = hook


def main():
    # 更新脚本的「握手」：它启动新 exe 后等这个文件出现，出现才算重启成功。
    # 必须放在 main() 最前面——能跑到这里，就说明 PyInstaller 解压成功、
    # python313.dll 也加载上了，正是外面想知道的那件事。
    _marker = os.environ.get("INTERLINE_RESTART_MARKER", "")
    if _marker:
        try:
            mp = Path(_marker)
            mp.parent.mkdir(parents=True, exist_ok=True)
            mp.write_text(
                f"pid={os.getpid()}\n"
                f"TEMP={os.environ.get('TEMP', '')}\n"
                f"MEIPASS={getattr(sys, '_MEIPASS', '')}\n"
                f"EXE={sys.executable}\n", encoding="utf-8")
        except Exception:
            pass
        # --restartcheck 用：写完就退，别真的把界面拉起来
        if os.environ.get("INTERLINE_RESTART_PROBE"):
            return 0
    if "--envdump" in sys.argv:
        # 排查「更新后重启报 python DLL」专用：把启动时看到的 TEMP / 解包目录写出来
        i = sys.argv.index("--envdump")
        out = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        if out:
            try:
                with open(out, "w", encoding="utf-8") as f:
                    f.write(f"TEMP={os.environ.get('TEMP', '')}\n")
                    f.write(f"TMP={os.environ.get('TMP', '')}\n")
                    f.write(f"MEIPASS={getattr(sys, '_MEIPASS', '')}\n")
                    f.write(f"EXE={sys.executable}\n")
                    f.write(f"FROZEN={getattr(sys, 'frozen', False)}\n")
            except Exception:
                pass
        return 0
    if "--selftest" in sys.argv:
        return selftest()
    if "--petcheck" in sys.argv:
        return petcheck()
    if "--edgecheck" in sys.argv:
        return edgecheck()
    if "--toolcheck" in sys.argv:
        return toolcheck()
    if "--restartcheck" in sys.argv:
        return restartcheck()
    if "--voicecheck" in sys.argv:
        return voicecheck()
    if "--updatecheck" in sys.argv:
        return updatecheck()
    if "--pdfcheck" in sys.argv:
        i = sys.argv.index("--pdfcheck")
        target = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        return pdfcheck(target)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(False)

    ok, server = acquire_single_instance(SERVER_KEY)
    if not ok:
        return 0

    st = store.Store()
    win = Reader(st)
    # 槽里出错只记日志，不再整个程序消失
    install_excepthook(lambda: win)

    # 托盘
    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip("奶蛙阅读器")
    tmenu = QMenu()
    tmenu.addAction("显示 / 隐藏").triggered.connect(
        lambda: win._hide_window() if win.isVisible() else win._do_show())
    tmenu.addAction("打开文件…").triggered.connect(win.pick_file)
    tmenu.addAction("书库…").triggered.connect(
        lambda: (win._do_show(), win.set_mode("books")))

    # 奶读模式在托盘里给个可勾选项：进了奶读模式顶栏就没了，这里是稳定入口
    act_pet = tmenu.addAction("奶读模式")
    act_pet.setCheckable(True)

    def toggle_pet_from_tray(_checked=False):
        win._do_show()
        win.toggle_pet()
        act_pet.setChecked(win._pet)

    act_pet.triggered.connect(toggle_pet_from_tray)
    tmenu.aboutToShow.connect(lambda: act_pet.setChecked(win._pet))

    tmenu.addSeparator()
    tmenu.addAction("退出").triggered.connect(app.quit)
    tray.setContextMenu(tmenu)
    tray.activated.connect(
        lambda reason: win._do_show()
        if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    # 上次是奶读模式就接着当奶蛙（素材缺失时 _enter_pet 自己会退回去）
    if st.get("pet.on", False):
        QTimer.singleShot(0, win._enter_pet)

    # app.quit 不会走 closeEvent，退出前必须自己落盘
    app.aboutToQuit.connect(win._persist)

    if server is not None:
        def on_new_conn():
            conn = server.nextPendingConnection()
            if conn:
                conn.readyRead.connect(lambda: (conn.readAll(), win.request_show.emit()))
                conn.disconnected.connect(conn.deleteLater)
        server.newConnection.connect(on_new_conn)

    win.show()

    # 启动参数里带文件路径时直接打开
    for arg in sys.argv[1:]:
        if arg.startswith("-"):
            continue
        if os.path.exists(arg):
            win.open_path(arg)
            break

    # 没指定文件就接着上次读。用 singleShot(0) 让窗口先画出来，
    # EPUB/PDF 解析要几百毫秒，直接调会让启动看着卡一下
    QTimer.singleShot(0, win._restore_last_book)

    # 更新检查晚 3 秒：别跟「恢复上次那本书」抢启动那一秒
    QTimer.singleShot(3000, win.check_updates)
    win._update_timer.start()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
