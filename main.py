"""行间 Interline —— 摸鱼电子书阅读器。

阶段 1：无边框透明置顶窗口 + TXT 阅读闭环。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import (QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt,
                          QTimer, pyqtSignal)
from PyQt6.QtGui import (QAction, QColor, QFont, QFontInfo, QFontMetrics, QIcon,
                         QKeySequence, QPainter, QPainterPath, QPen, QPixmap,
                         QShortcut, QTextBlockFormat, QTextCharFormat,
                         QTextCursor)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout,
                             QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
                             QSlider, QStackedWidget, QSystemTrayIcon, QTextEdit,
                             QVBoxLayout, QWidget)

import book_parser
import hotkey
import library_view
import logo
import settings_dialog
import store
import theme
import tts
from theme import SKIN_NAMES, SKIN_ORDER, SKINS

APP_TITLE = "Interline"
# 每次迭代 +1，与服务器 version.json 对应；打包时写进 exe 的产品版本
BUILD_VERSION = 26
SERVER_KEY = "Interline.SingleInstance.v1"
HEADER_H = 36
RESIZE_MARGIN = 14
# 单行模式下能拖动窗口的顶部窄条高度（其余区域留给点字和翻句）
DRAG_ZONE = 8


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

        # 听书
        self.speech = tts.SpeechReader(self)
        self._tts_sentences: list[str] = []
        self._tts_offsets: list[int] = []   # 每句在章节正文里的起始字符
        self._tts_base = 0                  # 正文在文档里的起点（标题长度 + 换行）

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
        # 一行模式的窄条尺寸不该覆盖正常窗口大小
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

        self.title_label = QLabel("行间")
        self.title_label.setObjectName("titleLabel")
        hl.addWidget(self.title_label)
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
        self.btn_speak = QPushButton("听")
        self.btn_font = QPushButton("Aa")
        self.btn_oneline = QPushButton("单")
        for b, w, tip in ((self.btn_speak, 24, "朗读（右键调语速和音色）"),
                          (self.btn_font, 28, "字号与行距"),
                          (self.btn_oneline, 24, "一行模式（F9）")):
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
        root.addWidget(self.footer)

        # 信号
        self.btn_open.clicked.connect(self.pick_file)
        self.btn_toc.clicked.connect(self.toggle_toc)
        self.btn_font.clicked.connect(self.show_font_panel)
        self.btn_oneline.clicked.connect(self.toggle_one_line)
        self.btn_lib.clicked.connect(lambda: self.set_mode("books"))
        self.btn_add.clicked.connect(self.add_books)
        self.btn_back.clicked.connect(lambda: self.set_mode("read"))
        self.btn_close.clicked.connect(self._hide_window)
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
        self.speech.finished.connect(self._on_speech_finished)
        self.speech.state_changed.connect(self._on_speech_state)

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
            # 一行模式里只有一句，滚动没意义，滚轮改成翻句
            if self._one_line:
                d = ev.angleDelta().y()
                if not d:
                    d = ev.pixelDelta().y() * 8
                if d:
                    self._wheel_acc += d / 120.0
                    step = int(self._wheel_acc)
                    if step:
                        self._wheel_acc -= step
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
        """单行模式的翻句键。返回 True 表示已经处理掉。

        方向键在 QTextEdit 里只是移动光标（这里只有一句，动了也看不出来），
        所以必须拦下来自己处理。
        """
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Right,
                   Qt.Key.Key_Space, Qt.Key.Key_PageDown):
            self._step_one_line(1)
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Left, Qt.Key.Key_PageUp):
            self._step_one_line(-1)
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
        # Esc 退一层：书库/目录回阅读，阅读态才藏窗口
        if self.mode != "read":
            self.set_mode("read")
        else:
            self._hide_window()

    # -------------------------------------------------- 视图切换

    def set_mode(self, mode: str):
        if mode == self.mode:
            return
        # 切走前先落盘，否则 700ms 防抖窗口内的滚动位置会丢
        self._flush_progress()
        if self.font_panel is not None:
            self.font_panel.hide()
        self.mode = mode
        is_read = mode == "read"
        self.stack.setCurrentIndex(0 if is_read else 1)
        for b in self._read_btns:
            b.setVisible(is_read)
        self.btn_back.setVisible(not is_read)
        self.btn_add.setVisible(mode == "books")
        self.title_label.setVisible(is_read)
        self.btn_speak.setVisible(is_read)
        self.btn_font.setVisible(is_read)
        self.btn_oneline.setVisible(is_read)
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
        self._resizing = False
        self._drag_origin = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.header.setCursor(Qt.CursorShape.OpenHandCursor)
        if moved:
            self._save_geometry()
        ev.accept()

    def _persist(self):
        """存进度 + 存几何。

        关闭按钮走 hide()、托盘退出走 app.quit，两者都不触发 closeEvent，
        所以三处都得显式调这个，否则防抖窗口内的进度会丢。
        """
        self._flush_progress()
        self._save_geometry()

    def _hide_window(self):
        if self.font_panel is not None:
            self.font_panel.hide()
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
            self._hide_window()
        else:
            self._do_show()
            self.raise_()
            self.activateWindow()
            if self._boss_paused:
                self.speech.resume()
                self._boss_paused = False

    def closeEvent(self, ev):
        self._persist()
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

    def open_path(self, path: str):
        try:
            book = book_parser.load_book(path)
        except Exception as e:
            QMessageBox.warning(self, "打不开", f"{Path(path).name}\n\n{e}")
            return
        self.set_book(book)

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

    def load_chapter(self, idx: int, ratio: float = 0.0, at_offset: int | None = None):
        if not self.book or not self.book.chapters:
            return
        # 换章必须停朗读，否则会继续念上一章的内容
        if self.speech.running:
            self._stop_speech()
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

    def _font_family(self) -> str:
        req = self.st.get("font.family", store.UI_FONT)
        if self._font_cache and self._font_cache[0] == req:
            return self._font_cache[1]
        real = resolve_font(req)
        self._font_cache = (req, real)
        return real

    def _block_formats(self, size: int, line_h: float, fm: QFontMetrics):
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

        title, body = self._chapter_parts(ch)
        self.view.setPlainText(title + "\n" + body)

        fmt_body, fmt_head, char_head, char_body = self._block_formats(
            size, line_h, QFontMetrics(font))

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

        fmt_body, fmt_head, char_head, _cbody = self._block_formats(
            size, line_h, QFontMetrics(font))

        # 一次合并所有块的块级格式（不碰字符格式，正文颜色保留）
        cur = QTextCursor(doc)
        cur.select(QTextCursor.SelectionType.Document)
        cur.mergeBlockFormat(fmt_body)

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

    def _toggle_speech(self):
        if not self.book or not self._tts_sentences:
            return
        if self.speech.running:
            if self.speech.paused:
                vis = self._visible_sentence_index()
                # 暂停期间可能滚到别处了：位置变了就从新位置重起，否则就地继续
                if vis != self.speech.current_index:
                    self._start_speech(vis)
                else:
                    self.speech.resume()
                    self.btn_speak.setText("停")
            else:
                self.speech.pause()
        else:
            self._start_speech()

    def _start_speech(self, from_idx: int | None = None):
        if not self.book or not self._tts_sentences:
            return
        if from_idx is None:
            from_idx = self._visible_sentence_index()
        self._clear_selection()
        voice = self.st.get("tts.voice", "")
        if voice:
            self.speech.set_voice(voice)
        self.speech.set_rate(float(self.st.get("tts.rate", 0.0)))
        self.speech.start(self._tts_sentences, from_idx)
        self.btn_speak.setText("停")
        # 不等 sayingWord：引擎报第一个词要几百毫秒，
        # 期间旧高亮还挂在上一句上。先把目标句点亮，点击即有反馈。
        # 一行模式下没有排版坐标，跳过——highlights 会用到 _tts_offsets。
        if 0 <= from_idx < len(self._tts_sentences) and not self._one_line:
            self._highlight_sentence(from_idx)
            self._scroll_to_char(self._tts_base + self._tts_offsets[from_idx])

    def _stop_speech(self):
        self.speech.stop()
        self._clear_highlight()
        self.btn_speak.setText("听")

    def _on_sentence(self, idx: int):
        # 一行模式下没有排版坐标，整块重排：文本换了，宽度也要跟着变
        if self._one_line:
            if 0 <= idx < len(self._tts_sentences):
                self._one_line_idx = idx
                self._paint_one_line(self._tts_sentences[idx])
            return
        self._highlight_sentence(idx)
        if 0 <= idx < len(self._tts_offsets):
            self._scroll_to_char(self._tts_base + self._tts_offsets[idx])
        # 朗读进度单独记一份：滚动位置是像素、会因字号变化漂移，
        # 句子索引重开就能精确回到原处
        if self.book_id:
            self.st.set_tts_progress(self.book_id, self.chapter_idx, idx)

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

    def _on_speech_finished(self):
        self.btn_speak.setText("听")
        self._clear_highlight()

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
        cur_voice = self.st.get("tts.voice", "") or self.speech.current_voice()
        for name in self.speech.available_voices():
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(name == cur_voice)
            act.triggered.connect(lambda _c=False, n=name: self._set_voice(n))
        menu.exec(self.btn_speak.mapToGlobal(pos))

    def _set_rate(self, rate: float):
        self.st.set("tts.rate", rate)
        self.st.save_config()
        self.speech.set_rate(rate)

    def _set_voice(self, name: str):
        self.st.set("tts.voice", name)
        self.st.save_config()
        self.speech.set_voice(name)

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

    def _enter_one_line(self):
        if not self.book:
            return
        sb = self.view.verticalScrollBar()
        self._one_line_pos = (
            self.chapter_idx,
            0.0 if sb.maximum() == 0 else sb.value() / sb.maximum())
        self._normal_geo = self.geometry()
        self._one_line = True
        self._one_line_idx = self._current_sentence_index()
        self.header.hide()
        self.footer.hide()
        self.search_bar.hide()
        # 从朗读句起显示，而不是重新算——否则进来会跳一句
        if self._tts_sentences:
            self._paint_one_line(self._tts_sentences[self._one_line_idx])
        else:
            self._paint_one_line()
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
        self._one_line = False
        # setFixedSize 会同时锁住 min/max，不解除的话下面的 setGeometry 无效
        self.setMinimumSize(320, 120)
        self.setMaximumSize(16777215, 16777215)
        self.header.show()
        self.footer.show()
        self.view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        if self._normal_geo is not None:
            self.setGeometry(self._normal_geo)
        chapter, ratio = self._one_line_pos
        self.load_chapter(chapter, ratio=ratio)

    def _paint_one_line(self, text: str | None = None):
        """一行模式下重排尺寸和文字。字号变了也要调一次。

        宽度按句子实际长度自适应：短句窗口就窄，长句才铺开，这样多数情况
        真的只有一行。cols 只当上下限的参考——固定宽度会让长句折成两行，
        那就不像「一行」了。

        text 由 _on_sentence 传进来：那一刻文档已经只剩一句，
        _visible_sentence_index() 在这种状态下取不准。
        """
        if text is None:
            text = self._current_sentence_text()
        cols = int(self.st.get("one_line.cols", 20))
        font = self._make_font(int(self.st.get("font.size",
                                                store.DEFAULT_CONFIG["font"]["size"])))
        fm = QFontMetrics(font)
        unit = fm.horizontalAdvance("中")
        # 余量给大一点：horizontalAdvance 对标点、半角数字会略微低估，
        # 差几像素就会折成两行
        pad = 62
        need = fm.horizontalAdvance(text) + pad
        lo = unit * max(8, cols // 2) + pad      # 太窄看着像根棍子
        hi = unit * cols * 2 + pad               # 太长就别铺满屏幕
        screen = QApplication.primaryScreen().availableGeometry().width() - 120
        width = max(lo, min(need, hi, screen))
        self.setFixedWidth(int(width))

        self.view.setPlainText(text)
        self.view.document().setDefaultFont(font)
        self.view.setFont(font)
        self.view.setExtraSelections([])
        self._tts_base = 0
        self._hl_tts = None
        self._hl_search = []

        # 高度交给文档自己算：不同字号/行距下估算值总会差一点，
        # 差一点就会把最后一行裁掉
        doc_h = self.view.document().size().height()
        self.setFixedHeight(max(34, int(doc_h) + 14))
        # 一条字幕不需要滚动条，多出来的那几个像素会挤出一条不能拖的条
        self.view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

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
        preview = body[offset:offset + 60].strip()
        self.st.add_bookmark(self.book_id, self.chapter_idx, offset, preview)
        self._set_hint("已添加书签", 1600)

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

    # -------------------------------------------------- 显示

    def _do_show(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self._on_escape()
            return
        super().keyPressEvent(ev)


# ------------------------------------------------------------------ 入口

def selftest() -> int:
    """打包后自检：确认关键依赖都被 PyInstaller 收进来了。

    打包版是 console=False，没有任何窗口能显示 print，
    所以结果同时写一份到临时文件。
    """
    import tempfile

    lines: list[str] = []

    def log(msg: str):
        lines.append(str(msg))

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

        try:
            from PyQt6.QtTextToSpeech import QTextToSpeech
            log(f"tts engine= {QTextToSpeech.availableEngines()}")
            t = QTextToSpeech("winrt")
            log(f"tts voices= {[v.name() for v in t.availableVoices()]}")
        except Exception as e:
            log(f"tts FAIL  = {type(e).__name__}: {e}")

        # PDF 是延迟导入的，这里确认打包时真的把它收进去了
        try:
            import pdfminer
            log(f"pdfminer  = {pdfminer.__version__}")
        except Exception as e:
            log(f"pdfminer FAIL = {type(e).__name__}: {e}")
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


def main():
    if "--selftest" in sys.argv:
        return selftest()
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

    # 托盘
    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip("行间")
    tmenu = QMenu()
    tmenu.addAction("显示 / 隐藏").triggered.connect(
        lambda: win._hide_window() if win.isVisible() else win._do_show())
    tmenu.addAction("打开文件…").triggered.connect(win.pick_file)
    tmenu.addAction("书库…").triggered.connect(
        lambda: (win._do_show(), win.set_mode("books")))
    tmenu.addSeparator()
    tmenu.addAction("退出").triggered.connect(app.quit)
    tray.setContextMenu(tmenu)
    tray.activated.connect(
        lambda reason: win._do_show()
        if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

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

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
