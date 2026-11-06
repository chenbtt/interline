"""设置窗口：左边分类，右边内容。

所有改动即时生效，没有「确定/取消」——摸鱼时不想为一个滑块点两次。
不 import main，靠传进来的 reader 对象调用，避免循环导入。
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QKeySequence, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtMultimedia import QMediaDevices
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout,
                             QLabel, QKeySequenceEdit, QListWidget,
                             QListWidgetItem, QMessageBox, QPushButton, QSlider,
                             QStackedWidget, QVBoxLayout, QWidget)

import theme
from theme import KEY_DEFAULTS, KEY_LABELS, SKIN_NAMES, SKIN_ORDER, SKINS


def _check_png(st) -> str:
    """画一张 13px 的勾号图给 QSS 用，缓存在数据目录。

    QSS 的 ::indicator 只能改背景和边框，画不出勾——纯色块的观感像
    「禁用」而不是「已选」，所以补一张小图。画不出来就返回空，退回纯色块。
    """
    path = Path(st.dir) / "ui_check13.png"
    if path.exists():
        return str(path).replace("\\", "/")
    try:
        pm = QPixmap(13, 13)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(244, 250, 254))
        pen.setWidthF(1.9)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QPolygonF([QPointF(2.4, 6.9),
                                  QPointF(5.3, 9.8),
                                  QPointF(10.8, 3.2)]))
        p.end()
        path.parent.mkdir(parents=True, exist_ok=True)
        pm.save(str(path), "PNG")
    except Exception:
        return ""
    return str(path).replace("\\", "/")


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
            ("奶蛙", self._page_pet()),
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
        hint = QLabel("系统语音离线可用；在线音色音质更自然，需要联网，"
                      "连不上时这一次会自动切回系统语音。", w)
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.voice_box = QComboBox(w)
        last_group = None
        for label, vid, group in self.reader.speech.voice_choices():
            if group != last_group:
                if last_group is not None:
                    self.voice_box.insertSeparator(self.voice_box.count())
                last_group = group
            text = label if group == "sys" else "在线 · " + label
            self.voice_box.addItem(text, vid)   # itemData 存 voice_id
        cur = self.reader.speech.current_voice()
        idx = self.voice_box.findData(cur)
        if idx < 0 and cur:
            idx = self.voice_box.findData("sys:" + cur)  # 老配置不带前缀
        self.voice_box.setCurrentIndex(max(0, idx))
        # 先设好再连信号：打开设置不该因为初始化就重启正在进行的朗读
        self.voice_box.currentIndexChanged.connect(self._on_voice)
        lay.addWidget(self.voice_box)

        if self.reader.speech.edge_available:
            note = QLabel("在线音色由 edge-tts（LGPL-3.0）提供，"
                          "走微软 Edge 的在线服务。", w)
            note.setObjectName("hint")
        else:
            note = QLabel("在线音色组件未加载，当前只有系统语音。", w)
            note.setObjectName("warn")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addSpacing(10)

        lay.addWidget(self._section("输出设备（在线音色）"))
        try:
            default_dev = QMediaDevices.defaultAudioOutput().description()
        except Exception:
            default_dev = "?"
        dev_hint = QLabel(
            f"在线音色走 Qt 的音频输出，当前系统默认是「{default_dev}」。"
            "默认设备是没连上的蓝牙耳机时，音频照常播放但你听不到；"
            "手动选一个正在用的扬声器即可。系统语音不受这一项影响。", w)
        dev_hint.setObjectName("hint")
        dev_hint.setWordWrap(True)
        lay.addWidget(dev_hint)
        self.dev_box = QComboBox(w)
        for value, label in self.reader.speech.output_devices():
            self.dev_box.addItem(label, value)
        cur_dev = self.st.get("tts.output_device", "") or ""
        idx = self.dev_box.findData(cur_dev)
        self.dev_box.setCurrentIndex(max(0, idx))
        self.dev_box.currentIndexChanged.connect(self._on_output_device)
        lay.addWidget(self.dev_box)
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

    def _on_voice(self, index: int):
        vid = self.voice_box.itemData(index)
        if not vid:      # 分隔条
            return
        self.st.set("tts.voice_id", vid)
        self.st.save_config()
        self.reader.speech.set_voice(vid)

    def _on_output_device(self, index: int):
        value = self.dev_box.itemData(index) or ""
        self.st.set("tts.output_device", value)
        self.st.save_config()
        self.reader.speech.set_output_device(value)

    def _on_rate(self, value: int):
        rate = value / 100.0
        self.rate_label.setText(_rate_text(rate))
        self.st.set("tts.rate", rate)
        self.st.save_config()
        self.reader.speech.set_rate(rate)

    # -------------------------------------------------- 奶蛙

    def _page_pet(self):
        w, lay = self._page("奶蛙")

        # 这里原来有一整段「奶读模式」说明 + 进入/试听两颗按钮（v59 之前）。
        # v49 起奶蛙是独立悬浮窗，「开启后整个阅读器变成奶蛙」已经不对，
        # 进入也早有 F8 / 底栏蛙头 / 托盘三个入口；整段删掉，别留过期说明。
        lay.addWidget(self._section("奶蛙大小"))
        scale = int(round(float(self.st.get("pet.scale", 0.9)) * 100))
        self.pet_scale_slider = self._slider(60, 160, scale)
        self.pet_scale_label = QLabel(f"{scale}%", w)
        self.pet_scale_slider.valueChanged.connect(self._on_pet_scale)
        lay.addLayout(self._slider_row(self.pet_scale_slider, self.pet_scale_label))
        lay.addSpacing(10)

        lay.addWidget(self._section("气泡宽度"))
        bw = int(self.st.get("pet.bubble_w", 360))
        self.pet_bw_slider = self._slider(200, 560, bw)
        self.pet_bw_label = QLabel(f"{bw}px", w)
        self.pet_bw_slider.valueChanged.connect(self._on_pet_bubble_w)
        lay.addLayout(self._slider_row(self.pet_bw_slider, self.pet_bw_label))
        lay.addSpacing(10)

        lay.addWidget(self._section("贴边"))
        self.pet_snap_cb = QCheckBox("拖到屏幕边缘松手就吸上去", w)
        self.pet_snap_cb.setChecked(bool(self.st.get("pet.snap", True)))
        self.pet_snap_cb.toggled.connect(self._on_pet_snap)
        lay.addWidget(self.pet_snap_cb)
        lay.addSpacing(10)

        lay.addWidget(self._section("音效"))
        self.pet_sound_cb = QCheckBox("摸奶蛙、打招呼时出声", w)
        self.pet_sound_cb.setChecked(bool(self.st.get("pet.sound", True)))
        self.pet_sound_cb.toggled.connect(self._on_pet_sound)
        lay.addWidget(self.pet_sound_cb)
        self.pet_duck_cb = QCheckBox("出声时把朗读音量压低，两个声音不打架", w)
        self.pet_duck_cb.setChecked(bool(self.st.get("pet.duck", True)))
        self.pet_duck_cb.toggled.connect(self._on_pet_duck)
        lay.addWidget(self.pet_duck_cb)

        self.pet_idle_cb = QCheckBox("闲置两分钟自己嘀咕一句（默认关）", w)
        self.pet_idle_cb.setChecked(bool(self.st.get("pet.idle_voice", False)))
        self.pet_idle_cb.toggled.connect(self._on_pet_idle)
        lay.addWidget(self.pet_idle_cb)

        vol = int(round(float(self.st.get("pet.sound_volume", 0.7)) * 100))
        self.pet_vol_slider = self._slider(0, 100, vol)
        self.pet_vol_label = QLabel(f"{vol}%", w)
        self.pet_vol_slider.valueChanged.connect(self._on_pet_volume)
        lay.addSpacing(8)
        lay.addWidget(self._section("音效音量"))
        lay.addLayout(self._slider_row(self.pet_vol_slider, self.pet_vol_label))

        lay.addStretch(1)
        return w

    def _on_pet_scale(self, value: int):
        self.pet_scale_label.setText(f"{value}%")
        self.st.set("pet.scale", value / 100.0)
        self.st.save_config()
        # 蛙在独立窗口里，改大小只通知它；没进奶读时窗口还没建，空转
        self.reader.pet_apply_scale(value / 100.0)

    def _on_pet_bubble_w(self, value: int):
        self.pet_bw_label.setText(f"{value}px")
        self.st.set("pet.bubble_w", value)
        self.st.save_config()
        self.reader.pet_relayout()

    def _on_pet_sound(self, on: bool):
        self.st.set("pet.sound", bool(on))
        self.st.save_config()

    def _on_pet_snap(self, on: bool):
        self.st.set("pet.snap", bool(on))
        if not on:
            # 关掉吸附的同时把「上次贴在哪」清掉：否则再打开时蛙会
            # 直接跳回一条它现在并不在的边上
            self.st.set("pet.snap_dir", "")
        self.st.save_config()
        self.reader.pet_unsnap()

    def _on_pet_duck(self, on: bool):
        self.st.set("pet.duck", bool(on))
        self.st.save_config()

    def _on_pet_idle(self, on: bool):
        self.st.set("pet.idle_voice", bool(on))
        self.st.save_config()

    def _on_pet_volume(self, value: int):
        self.pet_vol_label.setText(f"{value}%")
        self.st.set("pet.sound_volume", value / 100.0)
        self.st.save_config()

    # -------------------------------------------------- 关于

    def _page_about(self):
        w, lay = self._page("关于")
        import main as app_main      # 只为拿版本号，函数内导入避免循环

        lay.addWidget(QLabel(f"奶蛙阅读器 Interline", w))
        ver = QLabel(f"版本 {app_main.BUILD_VERSION}", w)
        ver.setObjectName("hint")
        lay.addWidget(ver)
        lay.addSpacing(8)

        boss = self.reader.boss_key or "（未注册）"
        for text in (f"老板键：{boss}",
                     f"数据目录：{self.st.dir}",
                     "支持格式：txt / md / epub / html / pdf",
                     "奶蛙精灵图：timerring/codex-pet-naiwa（MIT）",
                     "奶蛙音效：naiwa-pet（仅供学习交流，请勿商用）"):
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
        # QSS 的 indicator 只能填色、画不出勾，所以给一张 13px 的小图
        _png = _check_png(self.st)
        check_rule = f'image: url("{_png}");' if _png else ""
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
            QCheckBox {{ color: {fg}; font-size: 12.5px; spacing: 8px; }}
            QCheckBox::indicator {{
                width: 13px; height: 13px; border-radius: 3px;
                border: 1px solid rgba({ov},.30); background: transparent;
            }}
            QCheckBox::indicator:hover {{ border-color: rgba(95,143,168,.7); }}
            QCheckBox::indicator:checked {{
                background: #6f9fbc; border-color: #a9c8db;
                {check_rule}
            }}
        """)
        self.setWindowOpacity(1.0)


def _rate_text(rate: float) -> str:
    """-1..1 的引擎速率折算成倍数显示。去掉多余的零：1.00 -> 1、1.50 -> 1.5。"""
    s = f"{1.0 + rate:.2f}".rstrip("0").rstrip(".")
    return f"{s}x"
