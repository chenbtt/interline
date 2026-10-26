"""设置窗口：左边分类，右边内容。

所有改动即时生效，没有「确定/取消」——摸鱼时不想为一个滑块点两次。
不 import main，靠传进来的 reader 对象调用，避免循环导入。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
                             QKeySequenceEdit, QListWidget, QListWidgetItem,
                             QMessageBox, QPushButton, QSlider, QStackedWidget,
                             QVBoxLayout, QWidget)

import theme
from theme import KEY_DEFAULTS, KEY_LABELS, SKIN_NAMES, SKIN_ORDER, SKINS


class SettingsDialog(QDialog):
    def __init__(self, reader):
        super().__init__(reader)
        self.reader = reader
        self.st = reader.st
        self.setWindowTitle("设置")
        self.setMinimumSize(600, 470)
        self.resize(660, 520)
        self._build()
        self._apply_style()

    # -------------------------------------------------- 骨架

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.nav = QListWidget(self)
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(148)
        self.nav.setFrameShape(QFrame.Shape.NoFrame)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.nav)

        self.pages = QStackedWidget(self)
        root.addWidget(self.pages, 1)

        for title, page in (
            ("外观", self._page_look()),
            ("快捷键", self._page_keys()),
            ("朗读", self._page_tts()),
            ("关于", self._page_about()),
        ):
            self.nav.addItem(QListWidgetItem(title))
            self.pages.addWidget(page)

        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

    def _page(self, title: str):
        w = QWidget(self)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(12)
        if title:
            head = QLabel(title, w)
            head.setObjectName("pageTitle")
            lay.addWidget(head)
        return w, lay

    def _section(self, text: str) -> QLabel:
        lb = QLabel(text)
        lb.setObjectName("section")
        return lb

    def _slider(self, lo: int, hi: int, value: int) -> QSlider:
        s = QSlider(Qt.Orientation.Horizontal, self)
        s.setRange(lo, hi)
        s.setValue(max(lo, min(hi, value)))
        s.setCursor(Qt.CursorShape.PointingHandCursor)
        return s

    def _slider_row(self, slider: QSlider, label: QLabel) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(slider, 1)
        label.setFixedWidth(46)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        label.setObjectName("value")
        row.addWidget(label)
        return row

    # -------------------------------------------------- 外观

    def _page_look(self):
        w, lay = self._page("外观")

        lay.addWidget(self._section("皮肤"))
        row = QHBoxLayout()
        row.setSpacing(10)
        self._skin_btns = {}
        cur_skin = self.st.get("skin", "read")
        for key in SKIN_ORDER:
            btn = QPushButton(SKIN_NAMES[key], w)
            btn.setCheckable(True)
            btn.setFixedHeight(40)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setChecked(key == cur_skin)
            # 直接把皮肤底色画出来，比写三个色块直观
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {SKINS[key]['bg']}; color: {SKINS[key]['fg']};
                    border: 1px solid rgba(255,255,255,.14);
                    border-radius: 6px; font-size: 12px;
                }}
                QPushButton:hover {{ border-color: rgba(255,255,255,.32); }}
                QPushButton:checked {{ border: 1.5px solid #5f8fa8; }}
            """)
            btn.clicked.connect(lambda _c=False, k=key: self._pick_skin(k))
            row.addWidget(btn, 1)
            self._skin_btns[key] = btn
        lay.addLayout(row)
        lay.addSpacing(10)

        lay.addWidget(self._section("面板透明度"))
        hint1 = QLabel("整个窗口连同文字一起变透明，适合叠在别的窗口上偷看。", w)
        hint1.setObjectName("hint")
        hint1.setWordWrap(True)
        lay.addWidget(hint1)
        op = int(round(float(self.st.get("window_opacity", 1.0)) * 100))
        self.op_slider = self._slider(15, 100, op)
        self.op_label = QLabel(f"{op}%", w)
        self.op_slider.valueChanged.connect(self._on_opacity)
        lay.addLayout(self._slider_row(self.op_slider, self.op_label))
        lay.addSpacing(10)

        lay.addWidget(self._section("背景不透明度"))
        hint2 = QLabel("只让底板变透，文字保持清晰。", w)
        hint2.setObjectName("hint")
        hint2.setWordWrap(True)
        lay.addWidget(hint2)
        # 配置里存的是 0-255 的 alpha，界面上按百分比显示更好懂
        alpha = int(self.st.get("bg_alpha", 236))
        pct = max(0, min(100, round(alpha / 255 * 100)))
        self.bg_slider = self._slider(0, 100, pct)
        self.bg_label = QLabel(f"{pct}%", w)
        self.bg_slider.valueChanged.connect(self._on_bg_pct)
        lay.addLayout(self._slider_row(self.bg_slider, self.bg_label))
        lay.addSpacing(10)

        lay.addWidget(self._section("一行模式宽度"))
        hint3 = QLabel("单行模式下窗口按这个字数定宽，窗内只显示当前这句。", w)
        hint3.setObjectName("hint")
        hint3.setWordWrap(True)
        lay.addWidget(hint3)
        cols = int(self.st.get("one_line.cols", 20))
        self.cols_slider = self._slider(10, 48, cols)
        self.cols_label = QLabel(f"{cols} 字", w)
        self.cols_slider.valueChanged.connect(self._on_cols)
        lay.addLayout(self._slider_row(self.cols_slider, self.cols_label))

        lay.addStretch(1)
        return w

    def _pick_skin(self, key: str):
        for k, btn in self._skin_btns.items():
            btn.setChecked(k == key)
        self.reader.apply_skin(key)
        self._apply_style()      # 设置窗口自己也要跟着换

    def _on_opacity(self, value: int):
        self.op_label.setText(f"{value}%")
        opacity = value / 100.0
        self.st.set("window_opacity", opacity)
        self.reader.setWindowOpacity(opacity)
        if self.reader.font_panel is not None:
            self.reader.font_panel.setWindowOpacity(opacity)
        self.st.save_config()

    def _on_bg_pct(self, value: int):
        self.bg_label.setText(f"{value}%")
        # 不能真的给 0：全透明像素在 Windows 上不参与命中测试，
        # 鼠标会直接穿透到下面的窗口，右键菜单和拖动全都点不到。
        # 1/255 视觉上和全透明没区别，但窗口还能接住点击。
        self.st.set("bg_alpha", max(1, round(value * 255 / 100)))
        self.reader.update()
        if self.reader.font_panel is not None:
            self.reader.font_panel.update()
        self.st.save_config()

    def _on_cols(self, value: int):
        self.cols_label.setText(f"{value} 字")
        self.st.set("one_line.cols", value)
        self.st.save_config()
        # 正处在一行模式时立刻按新宽度重排
        if getattr(self.reader, "_one_line", False):
            self.reader._paint_one_line()

    # -------------------------------------------------- 快捷键

    def _page_keys(self):
        w, lay = self._page("快捷键")
        hint = QLabel("点右侧输入框，按下新的组合键即可，改完立即生效。"
                      "留空会恢复默认。", w)
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addSpacing(4)

        self._key_edits = {}
        for name, label in KEY_LABELS.items():
            row = QHBoxLayout()
            row.setSpacing(12)
            lb = QLabel(label, w)
            lb.setFixedWidth(120)
            row.addWidget(lb)
            edit = QKeySequenceEdit(w)
            edit.setClearButtonEnabled(True)
            cur = self.st.get(f"keys.{name}", KEY_DEFAULTS[name])
            edit.setKeySequence(QKeySequence(cur))
            edit.editingFinished.connect(
                lambda n=name, e=edit: self._on_key_changed(n, e))
            row.addWidget(edit, 1)
            lay.addLayout(row)
            self._key_edits[name] = edit

        lay.addStretch(1)
        if getattr(self.reader, "_boss_fallback", False):
            tip = QLabel(f"⚠ 设定的键位被其他软件占用，当前实际使用 "
                         f"{self.reader.boss_key}", w)
            tip.setObjectName("warn")
            tip.setWordWrap(True)
            lay.addWidget(tip)
        note = QLabel("老板键是全局热键，焦点在别的窗口也能用；"
                      "如果被其他软件占用，会自动换成备选键位。", w)
        note.setObjectName("hint")
        note.setWordWrap(True)
        lay.addWidget(note)
        return w

    def _on_key_changed(self, name: str, edit: QKeySequenceEdit):
        seq = edit.keySequence().toString()
        if not seq:
            seq = KEY_DEFAULTS[name]
            edit.setKeySequence(QKeySequence(seq))
            return
        keys = self.st.get("keys") or {}
        for other, val in keys.items():
            if other != name and other in KEY_LABELS and val == seq:
                QMessageBox.warning(
                    self, "快捷键冲突",
                    f"「{KEY_LABELS[other]}」已经占用了 {seq}，换一个组合键。")
                back = keys.get(name) or KEY_DEFAULTS[name]
                edit.setKeySequence(QKeySequence(back))
                return
        if seq == keys.get(name):
            return
        self.st.set(f"keys.{name}", seq)
        self.st.save_config()
        self.reader.reload_shortcuts()

    # -------------------------------------------------- 朗读

    def _page_tts(self):
        w, lay = self._page("朗读")

        lay.addWidget(self._section("音色"))
        self.voice_box = QComboBox(w)
        for v in self.reader.speech.available_voices():
            self.voice_box.addItem(v)
        cur_voice = self.st.get("tts.voice", "") or self.reader.speech.current_voice()
        idx = self.voice_box.findText(cur_voice)
        if idx >= 0:
            self.voice_box.setCurrentIndex(idx)
        self.voice_box.currentTextChanged.connect(self._on_voice)
        lay.addWidget(self.voice_box)
        lay.addSpacing(10)

        lay.addWidget(self._section("语速"))
        rate = float(self.st.get("tts.rate", 0.0))
        # 存的是 -1..1，界面按百分比显示更好懂
        self.rate_slider = self._slider(-100, 100, int(round(rate * 100)))
        self.rate_label = QLabel(_rate_text(rate), w)
        self.rate_slider.valueChanged.connect(self._on_rate)
        lay.addLayout(self._slider_row(self.rate_slider, self.rate_label))
        lay.addStretch(1)
        return w

    def _on_voice(self, name: str):
        self.st.set("tts.voice", name)
        self.st.save_config()
        self.reader.speech.set_voice(name)

    def _on_rate(self, value: int):
        rate = value / 100.0
        self.rate_label.setText(_rate_text(rate))
        self.st.set("tts.rate", rate)
        self.st.save_config()
        self.reader.speech.set_rate(rate)

    # -------------------------------------------------- 关于

    def _page_about(self):
        w, lay = self._page("关于")
        import main as app_main      # 只为拿版本号，函数内导入避免循环

        lay.addWidget(QLabel(f"行间 Interline", w))
        ver = QLabel(f"版本 {app_main.BUILD_VERSION}", w)
        ver.setObjectName("hint")
        lay.addWidget(ver)
        lay.addSpacing(8)

        boss = self.reader.boss_key or "（未注册）"
        for text in (f"老板键：{boss}",
                     f"数据目录：{self.st.dir}",
                     "支持格式：txt / md / epub / html / pdf"):
            lb = QLabel(text, w)
            lb.setObjectName("hint")
            lb.setWordWrap(True)
            lay.addWidget(lb)

        lay.addStretch(1)
        return w

    # -------------------------------------------------- 样式

    def _apply_style(self):
        bg = self.st.get("colors.bg", "#0d0f12")
        fg = self.st.get("colors.fg", "#c3cbd6")
        heading = self.st.get("colors.heading", "#a8bccd")
        ov = theme.overlay(self.st)
        dim = theme.dim_text(self.st, "mid")
        faint = theme.dim_text(self.st, "faint")
        menu_bg, menu_border, _menu_fg, menu_sel = theme.menu_colors(self.st)
        self.setStyleSheet(f"""
            QDialog {{ background: {bg}; }}
            QListWidget#nav {{
                background: rgba({ov},.04); border: none;
                outline: none; padding: 12px 7px; font-size: 12.5px;
                color: {dim};
            }}
            QListWidget#nav::item {{
                padding: 9px 12px; border-radius: 6px; margin: 2px 0;
            }}
            QListWidget#nav::item:selected {{
                background: rgba(95,143,168,.22); color: {fg};
            }}
            QListWidget#nav::item:hover {{ background: rgba({ov},.06); }}
            QLabel {{ color: {fg}; font-size: 12.5px; }}
            QLabel#pageTitle {{ font-size: 15.5px; color: {heading}; }}
            QLabel#section {{ font-size: 12.5px; color: {heading}; }}
            QLabel#hint {{ color: {faint}; font-size: 11.5px; }}
            QLabel#warn {{ color: #c08a2e; font-size: 11.5px; }}
            QLabel#value {{ color: {heading}; font-size: 12px; }}
            QKeySequenceEdit {{
                background: rgba({ov},.06);
                border: 1px solid rgba({ov},.12);
                border-radius: 5px; padding: 4px 8px; color: {fg};
                selection-background-color: rgba(95,143,168,.45);
            }}
            QKeySequenceEdit:focus {{ border-color: rgba(95,143,168,.6); }}
            QComboBox {{
                background: rgba({ov},.06);
                border: 1px solid rgba({ov},.12);
                border-radius: 5px; padding: 5px 10px; color: {fg};
            }}
            QComboBox:hover {{ border-color: rgba({ov},.26); }}
            QComboBox QAbstractItemView {{
                background: {menu_bg}; color: {fg};
                border: 1px solid {menu_border};
                selection-background-color: {menu_sel};
                outline: none;
            }}
            QSlider::groove:horizontal {{
                height: 4px; background: rgba({ov},.14); border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: rgba(95,143,168,.75); border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 12px; height: 12px; margin: -5px 0;
                background: #8fa8bb; border-radius: 6px;
            }}
            QSlider::handle:horizontal:hover {{ background: #b6cad8; }}
        """)
        self.setWindowOpacity(1.0)


def _rate_text(rate: float) -> str:
    """-1..1 的引擎速率折算成倍数显示。去掉多余的零：1.00 -> 1、1.50 -> 1.5。"""
    s = f"{1.0 + rate:.2f}".rstrip("0").rstrip(".")
    return f"{s}x"
