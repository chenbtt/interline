"""奶蛙悬浮窗 —— 思维上等同于音乐软件的「桌面歌词」。

主窗口（阅读器）才是主体：它照常显示正文、切书库、翻目录、改设置。
奶蛙是浮在桌面上的**另一个**独立小窗，只做三件事：

  1. 显示当前这一句（头顶气泡）
  2. 跟着朗读状态换动作（播 / 停 / 空闲）
  3. 接住鼠标：拖走、点一下、滚轮翻句、右键菜单

它不认识 Reader、不加载书、不推进句子 —— 所有业务动作都以信号抛回主窗，
正如歌词窗不会自己去切歌。这样阅读器和奶蛙互不干扰：点书库、换主题、
最小化主窗，蛙都不受影响（只是跟着一起藏）。

窗口属性按悬浮窗的规矩来：
  Qt.Tool           不进任务栏、不被主窗遮挡管理
  Frameless         无边框
  WA_Translucent     真透明（蛙是抠图 PNG，气泡自绘）
  WA_ShowWithoutActivating  弹出时不抢主窗焦点
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

import pet
import pet_audio
import pet_edge

DRAG_SLOP = 4          # 按下后移动超过这么远才算拖拽，否则当点击
BUBBLE_PAD = 2         # 气泡尾巴尖到蛙头的空隙（尾巴本身另算）
SCREEN_INSET = 40      # 首次出现时离屏幕右下角的距离
GREET_DELAY_MS = 200   # 出现后隔多久打招呼（让首帧和音频链路先就位）
SNAP_ANIM_MS = 150     # 吸附时滑过去的时间
TAIL_HEAD = 0.18       # 吸附态气泡尾巴的落点：可见盒从上往下 18% 处（嘴的位置）


class PetWindow(QWidget):
    # ---- 交给主窗处理的业务动作 ----
    toggle_speech = pyqtSignal()
    bubble_clicked = pyqtSignal()
    step = pyqtSignal(int)
    request_open = pyqtSignal()
    request_library = pyqtSignal()
    request_settings = pyqtSignal()
    exit_pet = pyqtSignal()
    quit_app = pyqtSignal()
    sound_started = pyqtSignal(int)     # 音效时长，让主窗压低朗读
    moved = pyqtSignal(int, int)        # 拖完存位置
    notice = pyqtSignal(str)

    def __init__(self, st, font_factory, speech_state):
        """st 是共享配置；font_factory() 给气泡字体；speech_state() 给菜单文案。"""
        super().__init__(None)
        self.st = st
        self._font_factory = font_factory
        self._speech_state = speech_state

        self.sprite = pet.PetSprite()
        self.state = pet.PetState(self.sprite)
        self.edge = pet_edge.EdgeAssets()
        self.edge_state = pet_edge.EdgeState()
        self._edge_dir = ""                 # 非空 = 正贴在屏幕边上
        self._snap_anim: dict | None = None
        self._audio: pet_audio.PetAudio | None = None
        self._text = ""
        self._drag = None
        self._last_ms = 0.0
        self._idle_ms = 0.0

        self.setWindowFlags(Qt.WindowType.Tool
                            | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("奶蛙")
        self.sync_on_top(bool(self.st.get("always_on_top", True)))

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        # 打招呼用挂在自己身上的单发定时器：静态 singleShot 在窗口先被销毁时
        # 还会回来敲一次门，对着已析构的 C++ 对象调方法就是 RuntimeError
        self._greet_timer = QTimer(self)
        self._greet_timer.setSingleShot(True)
        self._greet_timer.timeout.connect(self._greet)

    # -------------------------------------------------- 生命周期

    def start(self, text: str) -> bool:
        """从主窗点亮：贴到上次的位置，开始动。素材缺失返回 False。"""
        if not self.sprite.load():
            return False
        self.sprite.set_scale(float(self.st.get("pet.scale", 0.9)))
        self.edge.load()
        self.edge.set_scale(float(self.st.get("pet.scale", 0.9)))
        self.sync_on_top(bool(self.st.get("always_on_top", True)))
        self._text = text or ""
        self._place(first=True)
        # 上次是贴着边关掉的，就还贴在那儿——否则每次开奶读模式它都要
        # 从边上「掉」回桌面中间，看着像丢了一只
        self._restore_edge()
        self.show()
        self.state.reset()
        self._last_ms = time.monotonic() * 1000.0
        self._idle_ms = 0.0
        self._timer.start()
        # 第一声别急：窗口还在画首帧，音频链路也是冷的。
        # 先静音预热，200ms 后再打招呼——实测不这么做首条音效会被切掉开头
        audio = self._audio_obj()
        if audio is not None:
            audio.warm()
        self._greet_timer.start(GREET_DELAY_MS)
        return True

    def _greet(self):
        if self.isVisible():
            self.fx(pet.WAVING)

    def stop(self):
        self._timer.stop()
        self._greet_timer.stop()
        if self._audio is not None:
            self._audio.stop()
        self.state.reset()
        self._drag = None
        self._snap_anim = None
        self.save_pos()
        # 收起前把吸附态清掉：下次 start() 会按配置重新贴，
        # 留着的话窗口会以 256 的尺寸回到站立形态，画出来是空的
        self._edge_dir = ""
        self.edge_state.reset()
        self.hide()

    def suspend(self):
        """老板键（主窗隐藏）：别在看不见的时候烧 CPU 或出声。"""
        self._timer.stop()
        if self._audio is not None:
            self._audio.stop()
        # 藏起来时指针不一定还在蛙身上，留着 hi 会让它一露面就举着手
        self.edge_state.set_hover(False)
        self.hide()

    def resume(self):
        if not self.isVisible():
            self.show()
        self._last_ms = time.monotonic() * 1000.0
        if not self._timer.isActive():
            self._timer.start()

    def save_pos(self):
        if self._edge_dir:
            self._save_edge()
            return
        g = self.geometry()
        self.st.set("pet.x", g.x())
        self.st.set("pet.y", g.y())

    # -------------------------------------------------- 贴边吸附

    def _screen_obj(self, pt):
        """pt 所在的屏幕。坐标落在所有屏幕之外时退回主屏。"""
        scr = QApplication.screenAt(pt)
        if scr is None:
            scr = QApplication.screenAt(self.geometry().center())
        return scr or QApplication.primaryScreen()

    def _blocked(self, scr, pt) -> frozenset[str]:
        """多屏时相邻的那条边不吸：外面还是桌面，吸过去等于卡在两块屏中间。

        判定要拿**整块屏**的几何去探，不能用 availableGeometry——
        任务栏那块属于同一块屏，拿可用区探会把有任务栏的那条边误判成相邻屏。
        """
        full = scr.geometry()
        out = set()
        for side, p in (("L", QPoint(full.left() - 8, pt.y())),
                        ("R", QPoint(full.right() + 8, pt.y())),
                        ("T", QPoint(pt.x(), full.top() - 8)),
                        ("B", QPoint(pt.x(), full.bottom() + 8))):
            if full.contains(p):
                continue
            if QApplication.screenAt(p) is not None:
                out.add(side)
        return frozenset(out)

    def _frog_rect(self) -> QRect:
        """蛙「看得见的那部分」在全局坐标里的矩形。

        吸附态取素材的基准可见盒，站立态取气泡排版算出来的蛙矩形。
        切换形态时靠它当锚——尺寸和姿态都变了，但蛙不该跳。
        """
        if self._edge_dir:
            geo = self.edge.geo(self._edge_dir) or {}
            box = geo.get("base") or [0, 0, self.edge.canvas(), self.edge.canvas()]
            k = self.edge.scale
            # 画布坐标 + 画布在窗口里的偏移：吸附态窗口还带着气泡，
            # 画布不一定在窗口原点
            g = self._canvas_global()
            return QRect(g.x() + round(box[0] * k), g.y() + round(box[1] * k),
                         round((box[2] - box[0]) * k),
                         round((box[3] - box[1]) * k))
        _size, _bubble, frog = self._metrics()
        return frog.translated(self.geometry().topLeft())

    def _edge_layout(self):
        """吸附态排版：(窗口尺寸, 气泡矩形, 画布矩形)，顺带记下尾巴朝向。

        气泡放**屏幕内侧**：蛙贴在边上，气泡往屋里伸，尾巴从朝向蛙的那一侧
        伸出来指着头。窗口里画布永远顶着切口那一侧，所以「画布贴屏幕边」
        这条硬规则不用因为加气泡而改，`pet_edge.snap_pos` 也因此不用改口径。

        接的是**蛙可见的那条边**，不是画布边：256 的画布里蛙只占一条
        （w 的可见盒 `base` 只有 70px 宽），按画布边摆气泡会离蛙一大截。
        用全相位并集盒 `box` 而不是 `base`：抬手帧手会往外伸 35px，
        按 base 摆的话那只手会被气泡压住。

        `self._tail` 存 (朝向, 落点)，落点是尾巴那条边上的坐标（朝上下是 x，
        朝左右是 y）。paintEvent 直接拿去画。
        """
        c = self.edge.size().width()
        canvas = QRect(0, 0, c, c)
        self._tail = ("down", None)
        if not (bool(self.st.get("pet.show_bubble", True)) and self._text):
            return QSize(c, c), None, canvas

        body = pet.measure_bubble(self._font_factory(), self._text,
                                  int(self.st.get("pet.bubble_w", 360)))
        bw, bh = body.width(), body.height()
        h, v = pet_edge.PIN.get(self._edge_dir, ("", ""))
        t, pad = pet.BUBBLE_TAIL, BUBBLE_PAD
        geo = self.edge.geo(self._edge_dir) or {}
        base = geo.get("base") or [0, 0, c, c]
        box = geo.get("box") or base
        k = self.edge.scale
        cut = geo.get("cut") or {"L": "left", "R": "right",
                                 "T": "top", "B": "bottom"}.get(h or v, "")

        if cut in ("left", "right"):
            if cut == "left":
                # 蛙可见部分在画布左边：气泡接在它右边，窗口往屋里长
                bx = round(box[2] * k) + t + pad
                w = max(c, bx + bw)
            else:
                # 反过来：气泡接在可见部分的左边，画布得留在窗口右端
                w = max(c, c - round(box[0] * k) + t + pad + bw)
                bx = w - c + round(box[0] * k) - t - pad - bw
            hgt = max(c, bh)
            cy = hgt - c if v == "B" else 0 if v == "T" else (hgt - c) // 2
            canvas = QRect(0 if cut == "left" else w - c, cy, c, c)
            # 尾巴对着嘴：可见盒从上往下 18% 就是嘴的位置（量过 w/e/sw/se），
            # 对准盒子中点会指到肚子上，看着像在跟肚子说话
            head = canvas.top() + round(
                (base[1] + (base[3] - base[1]) * TAIL_HEAD) * k)
            by = max(0, min(head - bh // 2, hgt - bh))
            self._tail = ("left" if cut == "left" else "right", head)
            return QSize(w, hgt), QRect(bx, by, bw, bh), canvas

        # 上下贴边：画布水平居中（气泡比蛙宽时蛙才不会偏一边）
        if cut == "top":
            by = round(box[3] * k) + t + pad
            hgt = max(c, by + bh)
        else:
            hgt = max(c, c - round(box[1] * k) + t + pad + bh)
            by = hgt - c + round(box[1] * k) - t - pad - bh
        w = max(c, bw)
        canvas = QRect((w - c) // 2, 0 if cut == "top" else hgt - c, c, c)
        head = canvas.left() + round((base[0] + base[2]) / 2 * k)
        bx = max(0, min(head - bw // 2, w - bw))
        self._tail = ("up" if cut == "top" else "down", head)
        return QSize(w, hgt), QRect(bx, by, bw, bh), canvas

    def _canvas_global(self) -> QPoint:
        """画布（不是窗口）左上角的全局坐标。换气泡、改字号时以它为准不动。"""
        return self._metrics()[2].topLeft() + self.pos()

    def _reflow_edge(self, keep: QPoint | None = None) -> None:
        """吸附态重排：气泡内容变了，但画布不能动。

        画布的位置就是切口的位置，动一下切口就离了屏幕边。
        """
        keep = keep or self._canvas_global()
        size, _bubble, canvas = self._edge_layout()
        self.setFixedSize(size)
        self.move(keep - canvas.topLeft())
        self.update()

    def _snap_to(self, d: str, anchor: QPoint, animate: bool = True,
                 screen: QRect | None = None) -> bool:
        """吸到 d 那条边，蛙可见部分的中心落在 anchor 上。

        screen 只给自检用：那边要在一块假的屏幕上摆八只蛙出图，
        真屏上摆会被任务栏和副屏搅乱，断言也没法写。
        """
        geo = self.edge.geo(d)
        if geo is None:
            return False
        self._edge_dir = d
        self.edge_state.reset()
        size, _bubble, canvas = self._metrics()    # 含气泡的整窗，不是画布
        self.setFixedSize(size)

        scr = screen or self._screen_obj(anchor).availableGeometry()
        target = pet_edge.snap_pos(d, geo["base"], self.edge.canvas(),
                                   self.edge.scale, size, anchor, scr,
                                   canvas.topLeft())

        frm = self.pos()
        self._snap_anim = None
        if animate and frm != target and self._timer.isActive():
            # 滑动全靠 _tick 推进；定时器没跑（比如刚 suspend 过）就直接落位，
            # 否则会停在半路，切口离了屏幕边不说，还再也回不来
            self._snap_anim = {"from": frm, "to": target, "t": 0.0,
                               "dur": SNAP_ANIM_MS}
        else:
            self.move(target)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()
        return True

    def _unsnap(self, clamp: bool = False) -> bool:
        """从边上摘下来，变回站立的蛙。以可见中心为锚，摘的瞬间不跳。

        clamp 给「不是用户拖下来」的场合用（关掉吸附、改大小）：那时没人
        捏着鼠标，站立形态比吸附形态宽一倍，不夹一下会有一半留在屏幕外。
        拖动中不能夹——夹了蛙会从鼠标底下弹开几十像素。
        """
        if not self._edge_dir:
            return False
        c = self._frog_rect().center()
        self._edge_dir = ""
        self._snap_anim = None
        self.edge_state.reset()
        size, _bubble, frog = self._metrics()
        self.setFixedSize(size)
        pos = c - QPoint(frog.x() + frog.width() // 2,
                         frog.y() + frog.height() // 2)
        if clamp:
            scr = self._screen_obj(c).availableGeometry()
            pos = QPoint(max(scr.left(), min(pos.x(), scr.right() - size.width() + 1)),
                         max(scr.top(), min(pos.y(), scr.bottom() - size.height() + 1)))
        self.move(pos)
        self.state.reset()
        self.update()
        return True

    def _try_snap(self) -> None:
        """松手时判定：够近就吸过去。"""
        if self._edge_dir or not self.edge.ok:
            return
        if not bool(self.st.get("pet.snap", True)):
            return
        frog = self._frog_rect()
        pt = frog.center()
        scr = self._screen_obj(pt)
        d = pet_edge.pick_dir(frog, scr.availableGeometry(), pet_edge.SNAP,
                              self._blocked(scr, pt))
        if d:
            g = self.geometry()
            self.st.set("pet.x", g.x())
            self.st.set("pet.y", g.y())
            self._snap_to(d, pt)

    def _save_edge(self) -> None:
        """只存「贴哪条边 + 沿边走到哪」，不存像素坐标。

        存比例而不是绝对坐标：换分辨率、换屏幕排列之后还能落回原处，
        存绝对坐标的话副屏一拔就跑到屏幕外去了。
        """
        d = self._edge_dir
        scr = self._screen_obj(self._frog_rect().center()).availableGeometry()
        c = self._frog_rect().center()
        h, v = pet_edge.PIN.get(d, ("", ""))
        if h and not v:                 # 左右贴边：自由轴是 y
            t = (c.y() - scr.top()) / max(1, scr.height())
        elif v and not h:               # 上下贴边：自由轴是 x
            t = (c.x() - scr.left()) / max(1, scr.width())
        else:
            t = 0.5
        self.st.set("pet.snap_dir", d)
        self.st.set("pet.snap_t", round(min(1.0, max(0.0, t)), 4))

    def _restore_edge(self) -> None:
        self._edge_dir = ""
        if not self.edge.ok or not bool(self.st.get("pet.snap", True)):
            return
        d = str(self.st.get("pet.snap_dir", "") or "")
        if d not in pet_edge.PIN:
            return
        scr = self._screen_obj(self._frog_rect().center()).availableGeometry()
        c = self._frog_rect().center()
        t = float(self.st.get("pet.snap_t", 0.5) or 0.5)
        h, v = pet_edge.PIN[d]
        if h and not v:
            anchor = QPoint(c.x(), scr.top() + round(t * scr.height()))
        elif v and not h:
            anchor = QPoint(scr.left() + round(t * scr.width()), c.y())
        else:
            anchor = c
        self._snap_to(d, anchor, animate=False)

    def _hover_at(self, pos) -> None:
        """鼠标移动时按 alpha 判 hover。

        窗口是 256 见方，可见像素只占一成到三成，左右贴边时半边都是空的；
        按矩形判的话指针还离蛙十几像素它就开始挥手了。
        """
        hit = self._hit(pos)
        over = hit == "frog"
        if self._edge_dir:
            self.setCursor(Qt.CursorShape.PointingHandCursor if hit == "bubble"
                           else Qt.CursorShape.OpenHandCursor if over
                           else Qt.CursorShape.ArrowCursor)
            if self.edge_state.set_hover(over):
                self.update()
            return
        self.setCursor(Qt.CursorShape.PointingHandCursor if hit == "bubble"
                       else Qt.CursorShape.OpenHandCursor if over
                       else Qt.CursorShape.ArrowCursor)

    def leaveEvent(self, ev):
        if self._edge_dir and self.edge_state.set_hover(False):
            self.update()
        super().leaveEvent(ev)

    # -------------------------------------------------- 对外设置

    def set_text(self, text: str):
        text = text or ""
        if text == self._text:
            return
        self._text = text
        if self._edge_dir:
            # 吸附态也念正文：只重排气泡，画布（切口）不动
            self._reflow_edge()
            return
        self._place(first=False)
        self.update()

    def set_scale(self, scale: float):
        """改蛙的大小。气泡宽度不受影响，窗口按蛙脚锚点重新贴。"""
        # 吸附态先在旧缩放下标好锚点：换完缩放再算，蛙心会跟着画布偏移漂几像素
        anchor = self._frog_rect().center() if self._edge_dir else None
        self.sprite.set_scale(scale)
        self.edge.set_scale(scale)
        if self._edge_dir:
            # 缩放后画布也跟着变：重新贴同一条边，可见中心别动
            self._snap_to(self._edge_dir, anchor, animate=False)
            return
        self._place(first=False)
        self.update()

    def relayout(self):
        """气泡宽度之类的排版参数变了：重算尺寸，锚点仍是蛙脚。"""
        if self._edge_dir:
            self._reflow_edge()
            return
        self._place(first=False)
        self.update()

    def unsnap(self):
        """从屏幕边上下来（设置里关掉吸附时调用）。"""
        if self._unsnap(clamp=True):
            self.save_pos()

    def set_base(self, name: str):
        self.state.set_base(name)
        # 吸附态不推进站立动画，朗读开关得单独告诉相位状态机：
        # 它自己按 10fps 推口型，停下立刻闭嘴
        self.edge_state.set_reading(name == pet.READING)
        self.update()

    def fx(self, name: str):
        """瞬时动作 + 音效：时长由音频反推，没有音频就按帧数估。"""
        clip, cooldown = "", 0
        if name == pet.JUMPING:
            clip, cooldown = pet_audio.LAUGH, 2000
        elif name in (pet.WAVING, pet.IDLE):
            clip = pet_audio.PetAudio.random_voice()

        hold = 0.0
        if clip:
            audio = self._audio_obj()
            if audio is not None and audio.play(clip, cooldown_ms=cooldown):
                hold = float(audio.duration(clip))
                self.sound_started.emit(int(hold))
        if hold <= 0:
            n = len(self.sprite.frames(name)) or 4
            hold = 1000.0 / max(1, self.sprite.fps(name)) * n * 3

        self.state.play(name, hold)
        self.update()

    def play_direction(self, name: str, hold: int = 620):
        self.state.play(name, hold)
        self.update()

    def review(self):
        """读完了：抬头等翻页。"""
        self.state.set_base(pet.IDLE)
        self.state.play(pet.REVIEW, 3000)
        self.update()

    def preview_sound(self):
        audio = self._audio_obj()
        if audio is None:
            return
        # 试听往往就是这条链路的第一次发声，同样要先热一下再放
        if audio.warm():
            QTimer.singleShot(220, lambda: audio.play(pet_audio.LAUGH))
        else:
            audio.play(pet_audio.LAUGH)

    def apply_theme(self):
        self.update()

    def sync_on_top(self, on: bool):
        flags = self.windowFlags()
        if on:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        if flags == self.windowFlags():
            return
        was = self.isVisible()
        self.setWindowFlags(flags)
        # 改 flag 会把窗口藏起来，得自己叫回来
        if was:
            self.show()

    def _audio_obj(self) -> pet_audio.PetAudio | None:
        if self._audio is None:
            try:
                a = pet_audio.PetAudio(self.st, self)
                a.notice.connect(self.notice.emit)
                self._audio = a
            except Exception:
                self._audio = None
        return self._audio

    # -------------------------------------------------- 布局

    def _metrics(self):
        """(窗口尺寸, 气泡矩形, 蛙矩形)。气泡在上，蛙贴底居中。

        没书 / 关掉气泡时只剩蛙，返回 None 当气泡矩形 —— 跟手动隐藏一个样。
        吸附态也是一样的三件套，只是气泡摆在屏幕内侧、尾巴转向蛙头，
        排版交给 `_edge_layout`（画布永远顶着切口那一侧）。
        """
        if self._edge_dir:
            return self._edge_layout()

        frog = self.sprite.size()
        if not bool(self.st.get("pet.show_bubble", True)) or not self._text:
            return (QSize(frog.width(), frog.height()), None,
                    QRect(0, 0, frog.width(), frog.height()))

        max_w = int(self.st.get("pet.bubble_w", 360))
        body = pet.measure_bubble(self._font_factory(), self._text, max_w)
        w = max(frog.width(), body.width())
        h = body.height() + pet.BUBBLE_TAIL + BUBBLE_PAD + frog.height()
        bubble = QRect((w - body.width()) // 2, 0, body.width(), body.height())
        frog_r = QRect((w - frog.width()) // 2, h - frog.height(),
                       frog.width(), frog.height())
        return QSize(w, h), bubble, frog_r

    def _place(self, first: bool):
        if self._edge_dir:
            # 吸附态位置由贴边那一轴决定，排版变化只许重排气泡，
            # 画布（切口）必须钉在原地
            self._reflow_edge()
            return
        size, _bubble, _frog = self._metrics()
        if first:
            x = self.st.get("pet.x")
            y = self.st.get("pet.y")
            if (isinstance(x, int) and isinstance(y, int)
                    and any(s.availableGeometry().contains(x + 30, y + 30)
                            for s in QApplication.screens())):
                self.setFixedSize(size)
                self.move(x, y)
                return
            scr = QApplication.primaryScreen().availableGeometry()
            self.setFixedSize(size)
            self.move(scr.right() - size.width() - SCREEN_INSET,
                      scr.bottom() - size.height() - SCREEN_INSET - 8)
            return

        # 锚点用蛙脚：气泡多一行不能让它往上跳一下
        g = self.geometry()
        cx = g.x() + g.width() // 2
        by = g.y() + g.height()
        self.setFixedSize(size)
        self.move(cx - size.width() // 2, by - size.height())

    # -------------------------------------------------- 动画

    def _tick(self):
        now = time.monotonic() * 1000.0
        dt = now - self._last_ms if self._last_ms else 30.0
        self._last_ms = now
        dt = max(0.0, min(dt, 200.0))       # 休眠回来别一次跳几百帧

        if self._snap_anim is not None:
            a = self._snap_anim
            a["t"] += dt
            k = min(1.0, a["t"] / a["dur"])
            e = 1 - (1 - k) ** 3            # ease-out：起步快，贴上去时收住
            f, t = a["from"], a["to"]
            self.move(round(f.x() + (t.x() - f.x()) * e),
                      round(f.y() + (t.y() - f.y()) * e))
            if k >= 1.0:
                self._snap_anim = None

        if self._edge_dir:
            # 吸附态不推进站立动画（那只蛙没在画面上，白烧 CPU），
            # 也不在这儿嘀咕——挂在墙上的东西突然说话很奇怪
            if self.edge_state.tick(dt):
                self.update()
            return

        before = (self.state.current, self.state.frame)
        self.state.tick(dt)
        if before != (self.state.current, self.state.frame):
            self.update()

        # 闲置久了嘀咕一句（默认关，怕吵）
        if self.state.current == pet.IDLE and not self.state.busy:
            self._idle_ms += dt
            if self._idle_ms > 120000 and bool(self.st.get("pet.idle_voice", False)):
                self._idle_ms = 0.0
                self.fx(pet.IDLE)
        else:
            self._idle_ms = 0.0

    # -------------------------------------------------- 绘制

    def paintEvent(self, ev):
        try:
            p = QPainter(self)
            try:
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                # Source 才清得出真透明：SourceOver 铺全透明等于没铺
                p.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_Source)
                p.fillRect(self.rect(), Qt.GlobalColor.transparent)
                p.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceOver)

                _size, bubble, box = self._metrics()
                if self._edge_dir:
                    # 吸附态第三项是**画布**矩形：素材图贴着切口那一侧画
                    pm = self.edge.pixmap(self._edge_dir, self.edge_state.phase)
                    if pm is not None and not pm.isNull():
                        p.drawPixmap(box.topLeft(), pm)
                    # 口型画在画布坐标系里，跟素材图同一个 origin / scale。
                    # 嘴位由 meta.json 给；闭口时 mouth_shape 是 (0,0)，
                    # 什么都不画，直接用素材原本那条斜嘴线
                    mw, mh = self.edge_state.mouth_shape
                    geo = self.edge.geo(self._edge_dir) or {}
                    m = geo.get("mouth")
                    if mh > 0 and m and len(m) >= 3:
                        pet.draw_edge_mouth(p, box.topLeft(), self.edge.scale,
                                            m[0], m[1], m[2], mw, mh)
                    if bubble is not None and self._text:
                        tail, anchor = self._tail
                        pet.draw_bubble(p, bubble, self._font_factory(),
                                        self.st, self._text,
                                        tail=tail, anchor=anchor)
                else:
                    if bubble is not None and self._text:
                        pet.draw_bubble(p, bubble, self._font_factory(),
                                        self.st, self._text)
                    frames = self.sprite.frames(self.state.current)
                    if frames:
                        pet.draw_pet(p, box,
                                     frames[min(self.state.frame, len(frames) - 1)])
            finally:
                p.end()
        except Exception:
            pass

    # -------------------------------------------------- 鼠标

    def _hit(self, pos) -> str:
        _size, bubble, box = self._metrics()
        if self._edge_dir:
            if bubble is not None and self._text:
                body = QRect(bubble.x(), bubble.y(), bubble.width(),
                             bubble.height() + pet.BUBBLE_TAIL)
                if body.contains(pos):
                    return "bubble"
            # 缓存的素材图就是画布那么大：传画布内坐标，别再减窗口原点
            local = pos - box.topLeft()
            ok = self.edge.hit(self._edge_dir, self.edge_state.phase,
                               int(local.x()), int(local.y()))
            return "frog" if ok else ""
        if bubble is not None:
            body = QRect(bubble.x(), bubble.y(), bubble.width(),
                         bubble.height() + pet.BUBBLE_TAIL)
            if body.contains(pos):
                return "bubble"
        if box.contains(pos):
            return "frog"
        return ""

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            self._menu(ev.globalPosition().toPoint())
            ev.accept()
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if self._edge_dir and self._hit(ev.position().toPoint()) != "frog":
            # 吸附态画布一半是空的，按在空处不该把蛙拖走
            return
        gp = ev.globalPosition().toPoint()
        self._drag = {"start": gp, "win": self.pos(),
                      "last_x": gp.x(), "moved": False}
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        ev.accept()

    def mouseMoveEvent(self, ev):
        gp = ev.globalPosition().toPoint()
        d = self._drag
        if d is None:
            self._hover_at(ev.position().toPoint())
            return
        if not d["moved"] and (gp - d["start"]).manhattanLength() > DRAG_SLOP:
            d["moved"] = True
            if self._edge_dir:
                # 从边上抓下来：先变回站立的蛙（可见中心不动），
                # 再把拖拽基准重设成当前鼠标位置——否则窗口会先跳一下。
                # 刚才那几十像素不能丢，得补上：摘下这一下鼠标已经动了，
                # 不补的话蛙会比手指慢半拍，之后一直差着这个距离
                delta = gp - d["start"]
                self._unsnap()
                self.move(self.pos() + delta)
                d["win"] = self.pos()
                d["start"] = gp
                d["last_x"] = gp.x()
        if d["moved"]:
            self.move(d["win"] + (gp - d["start"]))
            dx = gp.x() - d["last_x"]
            if dx:
                # 拖拽独占：期间忽略一切状态触发，否则会和 running 互相踩
                self.state.lock(pet.RUN_LEFT if dx < 0 else pet.RUN_RIGHT)
                self.update()
        d["last_x"] = gp.x()
        ev.accept()

    def mouseReleaseEvent(self, ev):
        d = self._drag
        self._drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if d is not None and d["moved"]:
            self.state.unlock()
            self.save_pos()
            self._try_snap()
            g = self.geometry()
            self.moved.emit(g.x(), g.y())
            self.update()
            ev.accept()
            return

        hit = self._hit(ev.position().toPoint())
        if hit == "frog":
            if self._edge_dir:
                # 吸附态没有动作动画（身子被切了一半，跳起来会把切口撕开），
                # 点蛙身就等于点气泡：播 / 停
                self.bubble_clicked.emit()
            else:
                self.fx(pet.JUMPING)           # 摸它一下
        elif hit == "bubble":
            self.bubble_clicked.emit()
        ev.accept()

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if dy:
            self.step.emit(1 if dy < 0 else -1)
        ev.accept()

    def keyPressEvent(self, ev):
        key = ev.key()
        if key == Qt.Key.Key_Escape:
            self.exit_pet.emit()
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Right, Qt.Key.Key_Space):
            self.step.emit(1)
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Left):
            self.step.emit(-1)
            return
        super().keyPressEvent(ev)

    # -------------------------------------------------- 菜单

    def _menu(self, global_pos):
        menu = QMenu(self)
        if self._edge_dir:
            # 给一条下得来的路——不然只能靠拖，而拖是对着半个身子操作
            menu.addAction("从屏幕上取下来").triggered.connect(self.unsnap)
        show = bool(self.st.get("pet.show_bubble", True))
        menu.addAction("隐藏气泡" if show else "显示气泡").triggered.connect(
            self._toggle_bubble)

        label, enabled = self._speech_state()
        act = menu.addAction(label)
        act.setEnabled(enabled)
        act.triggered.connect(self.toggle_speech.emit)

        menu.addSeparator()
        menu.addAction("打开文件…").triggered.connect(self.request_open.emit)
        menu.addAction("书库…").triggered.connect(self.request_library.emit)
        menu.addAction("设置…").triggered.connect(self.request_settings.emit)
        menu.addSeparator()
        menu.addAction("收起奶蛙").triggered.connect(self.exit_pet.emit)
        menu.addAction("退出程序").triggered.connect(self.quit_app.emit)
        menu.exec(global_pos)

    def _toggle_bubble(self):
        self.st.set("pet.show_bubble", not bool(self.st.get("pet.show_bubble", True)))
        self.st.save_config()
        self._place(first=False)
        self.update()
