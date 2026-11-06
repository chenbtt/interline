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
from PyQt6.QtGui import QColor, QCursor, QPainter
from PyQt6.QtWidgets import QApplication, QMenu, QToolTip, QWidget

import pet
import pet_audio
import pet_edge

DRAG_SLOP = 4          # 按下后移动超过这么远才算拖拽，否则当点击
BUBBLE_PAD = 2         # 气泡尾巴尖到蛙头的空隙（尾巴本身另算）
SCREEN_INSET = 40      # 首次出现时离屏幕右下角的距离
GREET_DELAY_MS = 200   # 出现后隔多久打招呼（让首帧和音频链路先就位）
SNAP_ANIM_MS = 150     # 吸附时滑过去的时间
TAIL_HEAD = 0.18       # 吸附态气泡尾巴的落点：可见盒从上往下 18% 处（嘴的位置）

# 气泡悬停工具条：左三（上一句 / 播放切换 / 下一句）、右二（收起气泡 / 主面板切换）
TOOL_BTN = 22          # 单颗按钮边长
TOOL_GAP = 6           # 组内间距
TOOL_INSET = 4         # 距工具带左右边
TOOL_Y_GAP = 2         # 距气泡边：贴着放，别留一段让指针掉出去的死区
TOOL_PAD = 4           # 按钮下沿留白
TOOL_BAND = TOOL_Y_GAP + TOOL_BTN + TOOL_PAD   # 工具带整体高度 28
TOOL_MARGIN = 6        # 悬停带四周外扩：指针压到按钮边缘也不熄灭
TOOL_FADE_MS = 130     # 淡入淡出时长
TOOL_LEAVE_MS = 180    # 脱离窗口的宽限：系统偶发一次 leave 不该让工具条消失
TOOL_LEFT = ("prev", "play", "next")
TOOL_RIGHT = ("hide", "main")
TOOL_TIPS = {
    "prev": "上一句",
    "play": "开始 / 停止朗读",
    "next": "下一句",
    "hide": "收起气泡（再点展开）",
    "main": "显示 / 收起主面板",
}
TOOL_TIP_DELAY_MS = 420


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
    collapse_bubble = pyqtSignal()      # 收起 / 展开气泡；蛙照旧留在桌面上
    show_main = pyqtSignal()            # 主面板显示 / 收起切换
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
        # 悬停工具条：站立 / 吸附两种形态都有；_band 是按钮占的那条横带，
        # _bridge 是铺了 alpha=1 的命中桥（透明像素在 Windows 不接鼠标，
        # 不铺的话指针一进气泡与按钮之间的缝就会掉到桌面、触发 leave）
        self._tools_on = False
        self._tools_alpha = 0.0
        self._tool_hover = ""
        self._tool_press = ""
        self._tool_tip_at = QPoint()
        self._band = QRect()
        self._bridge = QRect()

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
        self._tip_timer = QTimer(self)
        self._tip_timer.setSingleShot(True)
        self._tip_timer.timeout.connect(self._show_tool_tip)
        self._leave_timer = QTimer(self)
        self._leave_timer.setSingleShot(True)
        self._leave_timer.timeout.connect(self._finish_leave)

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
        self._tools_on = False
        self._tools_alpha = 0.0
        self._tool_press = ""
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
        self._set_tools(False)
        self._tools_alpha = 0.0
        self._tip_timer.stop()
        self._leave_timer.stop()
        QToolTip.hideText()
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
        self._set_tools(False)
        self._tools_alpha = 0.0
        self._tip_timer.stop()
        self._leave_timer.stop()
        QToolTip.hideText()
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
        """吸附态排版：(窗口尺寸, 气泡矩形, 画布矩形)，顺带记下尾巴与工具带。

        气泡放**屏幕内侧**：蛙贴在边上，气泡往屋里伸，尾巴从朝向蛙的那一侧
        伸出来指着头。窗口里画布永远顶着切口那一侧，所以「画布贴屏幕边」
        这条硬规则不用因为加气泡而改，`pet_edge.snap_pos` 也不用改口径。

        v59 的关键改动：**窗口几何不跟着句子行数变**。旧版气泡高度由正文
        行数决定，`hgt = max(c, by + bh)` 在 3 行→2 行时被夹回画布高度，
        气泡底下凭空多出十几像素透明带，就是「底部裂了一条缝」。现在气泡
        固定占满 BUBBLE_MAX_LINES 行的槽位（正文最多也就这么多行），
        工具带接在气泡背离蛙的那一侧，窗口尺寸恒定。换句只换气泡里的字，
        画布（切口）、蛙、五颗按钮全都不动——这才是「奶蛙位置保持不动，
        由气泡调整自身位置适配」。

        工具带方向：上下贴边时，气泡在蛙的下/上方，工具带就接在气泡再往外
        的一侧（顶部贴边在气泡下方，底部贴边在气泡上方）；左右贴边时气泡
        在蛙旁边，工具带接气泡下沿、并贴着气泡的左右沿排按钮，不压蛙身。
        工具带一直延伸到窗口边，窗口底/顶不会出现空带。
        """
        c = self.edge.size().width()
        canvas = QRect(0, 0, c, c)
        self._tail = ("down", None)
        self._band = QRect()

        font = self._font_factory()
        max_w = int(self.st.get("pet.bubble_w", 360))
        show = bool(self.st.get("pet.show_bubble", True)) and bool(self._text)
        # 吸附态气泡宽高都固定：换句只换气泡里的字，窗口一寸都不动。
        # 宽度取设置里的上限（正文会自动在这条宽度里重排），高度取正文最多
        # 能占的行数——两者都跟当前这句无关，才谈得上「位置保持不动」。
        bw = max(max_w, 5 * TOOL_BTN + 4 * TOOL_GAP + 2 * TOOL_INSET)
        bh = (pet.BUBBLE_MAX_LINES * pet.line_height(font)
              + 2 * pet.BUBBLE_PAD_Y) if show else 0
        block = bh + TOOL_BAND

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
            # 画布的 y 由 hgt 决定，hgt 又由气泡块决定：迭代几轮就收敛，
            # 起始取 c。收敛后窗口高度恒定，换句不会重排。
            mouth_y = round((base[1] + (base[3] - base[1]) * TAIL_HEAD) * k)
            hgt = c
            for _ in range(6):
                cy = (hgt - c if v == "B" else 0 if v == "T"
                      else (hgt - c) // 2)
                head = cy + mouth_y
                # 气泡（不是整块）对着嘴，工具带接在它下面；至少留 2px，
                # 别让气泡顶部的圆角被窗口上沿切掉
                by = max(2, min(head - bh // 2, hgt - block))
                new_hgt = max(c, by + block)
                if new_hgt == hgt:
                    break
                hgt = new_hgt
            cy = (hgt - c if v == "B" else 0 if v == "T"
                  else (hgt - c) // 2)
            canvas = QRect(0 if cut == "left" else w - c, cy, c, c)
            head = cy + mouth_y
            self._tail = ("left" if cut == "left" else "right", head)
            bubble = QRect(bx, by, bw, bh) if show else None
            # 工具带从气泡下沿一直铺到窗口下沿，按钮贴气泡左右沿
            self._band = QRect(bx, by + bh, bw, max(TOOL_BAND, hgt - by - bh))
            return QSize(w, hgt), bubble, canvas

        # 上下贴边：画布水平居中（气泡比蛙宽时蛙才不会偏一边）
        if cut == "top":
            by = round(box[3] * k) + t + pad
            hgt = max(c, by + block)
            cy = 0
            band_y = by + bh
            band_h = hgt - band_y
        else:
            a = round(box[1] * k)
            extra = max(0, block + t + pad - a)
            hgt = c + extra
            cy = extra
            be = cy + a - t - pad            # 气泡下沿（贴着蛙可见上沿）
            by = be - bh
            if show:
                band_y, band_h = 0, by        # 工具带在气泡上方，铺到窗口顶
            else:
                # 气泡收起后按钮得挨着蛙，别孤零零挂在窗口顶
                band_y, band_h = max(0, be - TOOL_BAND), TOOL_BAND
        w = max(c, bw)
        canvas = QRect((w - c) // 2, cy, c, c)
        head = canvas.left() + round((base[0] + base[2]) / 2 * k)
        bx = max(0, min(head - bw // 2, w - bw))
        self._tail = ("up" if cut == "top" else "down", head)
        bubble = QRect(bx, by, bw, bh) if show else None
        self._band = QRect(bx, band_y, bw, max(TOOL_BAND, band_h))
        return QSize(w, hgt), bubble, canvas

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
        # 形态切换时按钮会换位置，先收干净，等指针再指过来重新点亮
        self._set_tools(False)
        self._tools_alpha = 0.0
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

    # -------------------------------------------------- 气泡悬停工具条

    def _toolbar(self):
        """(按钮矩形表, 连续悬停带)。站立 / 吸附 / 气泡收起三种形态都给按钮。

        按钮排在一条「工具带」上：左三贴左沿、右二贴右沿，中间留给蛙头。
        吸附态工具带和气泡同宽（左右贴边时气泡只占窗口一侧），按钮因此
        不会压到蛙身；顶部贴边时它正好落在气泡下方那条横带上。

        v59 修悬停连续性的关键是**没有缝**：工具带从气泡边界直接
        开始，气泡本身是不透明底片，两者之间没有一格透明像素。悬停带 zone
        是气泡（含尾巴）与工具带的并集再外扩一圈，指针从气泡走到按钮的
        每一步都在里面。另外 `self._bridge` 会在这条带上铺一层 alpha=1 的
        底——Windows 分层窗口对 alpha=0 不做命中测试，不铺的话指针一进
        缝就掉到桌面、leaveEvent 立刻把按钮熄灭。
        """
        if not self._text:
            self._band = QRect()
            self._bridge = QRect()
            return {}, QRect()
        size, bubble, _frog = self._metrics()
        if self._edge_dir:
            band = self._band                  # _edge_layout() 刚算好
        elif bubble is not None:
            band = QRect(0, bubble.bottom(), size.width(), TOOL_BAND)
        else:
            band = QRect(0, size.height() - TOOL_BAND,
                         size.width(), TOOL_BAND)
        self._band = band
        if band.isEmpty():
            self._bridge = QRect()
            return {}, QRect()

        y = band.y() + TOOL_Y_GAP
        step = TOOL_BTN + TOOL_GAP
        lw = len(TOOL_LEFT) * step - TOOL_GAP
        rw = len(TOOL_RIGHT) * step - TOOL_GAP
        x0 = band.x() + TOOL_INSET if self._edge_dir else TOOL_INSET
        right0 = band.x() + band.width() - TOOL_INSET - rw
        rects = {}
        for i, k in enumerate(TOOL_LEFT):
            rects[k] = QRect(x0 + i * step, y, TOOL_BTN, TOOL_BTN)
        for i, k in enumerate(TOOL_RIGHT):
            rects[k] = QRect(right0 + i * step, y, TOOL_BTN, TOOL_BTN)

        zone = band.adjusted(-TOOL_MARGIN, -TOOL_MARGIN,
                             TOOL_MARGIN, TOOL_MARGIN)
        if bubble is not None:
            zone = zone.united(bubble.adjusted(
                -TOOL_MARGIN, -TOOL_MARGIN, TOOL_MARGIN,
                TOOL_MARGIN + pet.BUBBLE_TAIL))
        bridge = zone
        if bubble is None:
            # 收起气泡后从蛙身走到按钮的这一段也要接住指针
            bridge = bridge.united(QRect(0, max(0, band.top() - 30),
                                         size.width(), 30))
        self._bridge = bridge.intersected(
            QRect(0, 0, size.width(), size.height()))
        return rects, zone

    def _set_tools(self, on: bool) -> None:
        on = bool(on) and self._drag is None
        if self._tools_on == on:
            return
        self._tools_on = on
        if not on:
            self._tool_hover = ""
            self._tool_press = ""
            self._tip_timer.stop()
            QToolTip.hideText()
        self.update()

    def _tool_at(self, pos) -> str:
        if not self._tools_on:
            return ""
        rects, _ = self._toolbar()
        for k, r in rects.items():
            if r.contains(pos):
                return k
        return ""

    def _show_tool_tip(self) -> None:
        if self._tools_on and self._tool_hover:
            tip = TOOL_TIPS.get(self._tool_hover, "")
            if (self._tool_hover == "hide"
                    and not bool(self.st.get("pet.show_bubble", True))):
                tip = "展开气泡"
            QToolTip.showText(self._tool_tip_at, tip, self)

    def _emit_tool(self, kind: str) -> None:
        if kind == "prev":
            self.step.emit(-1)
        elif kind == "next":
            self.step.emit(1)
        elif kind == "play":
            # 没书时走「挑一本」，有书时播 / 停，跟点气泡一致
            self.bubble_clicked.emit()
        elif kind == "hide":
            self.collapse_bubble.emit()
        elif kind == "main":
            self.show_main.emit()

    def _hover_at(self, pos) -> None:
        """鼠标移动时按 alpha 判 hover，顺带管气泡工具条的显隐。

        v59 起吸附态一样亮工具条；气泡收起时改由蛙身点亮（否则那颗
        「展开气泡」按钮自己就没法再被点到）。
        """
        rects, zone = self._toolbar()
        hit = self._hit(pos)
        tool = hit[5:] if hit.startswith("tool:") else ""
        _size, bubble, _frog = self._metrics()
        want = (hit == "bubble" or bool(tool)
                or (bubble is None and hit == "frog")
                or (rects and zone.contains(pos)))
        self._set_tools(want)
        if want:
            self._leave_timer.stop()

        self._tool_hover = tool if self._tools_on else ""
        if self._tool_hover:
            self._tool_tip_at = self.mapToGlobal(pos)
            self._tip_timer.start(TOOL_TIP_DELAY_MS)
        else:
            self._tip_timer.stop()
            QToolTip.hideText()

        over = hit == "frog"
        pointing = hit == "bubble" or bool(tool)
        if self._edge_dir:
            self.setCursor(Qt.CursorShape.PointingHandCursor if pointing
                           else Qt.CursorShape.OpenHandCursor if over
                           else Qt.CursorShape.ArrowCursor)
            if self.edge_state.set_hover(over):
                self.update()
            return
        self.setCursor(Qt.CursorShape.PointingHandCursor if pointing
                       else Qt.CursorShape.OpenHandCursor if over
                       else Qt.CursorShape.ArrowCursor)

    def leaveEvent(self, ev):
        # 分层窗口偶尔会在指针压着气泡/按钮边缘时误报一次 leave，
        # 给 180ms 宽限；期间指针回来（_hover_at）就把定时器掐掉
        if self._tools_on:
            self._leave_timer.start(TOOL_LEAVE_MS)
        else:
            self._set_tools(False)
        self._tip_timer.stop()
        QToolTip.hideText()
        if self._edge_dir and self.edge_state.set_hover(False):
            self.update()
        super().leaveEvent(ev)

    def _finish_leave(self):
        """宽限到了还在窗外才真熄灭。"""
        if self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            return
        self._set_tools(False)

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
            # 气泡收起时留出一条工具带：不然「收起气泡」那颗按钮自己也
            # 跟着消失，用户只能靠右键菜单把它找回来
            h = frog.height() + (TOOL_BAND if self._text else 0)
            return (QSize(frog.width(), h), None,
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

        # 工具条淡入淡出。放在贴边分支之前：吸附态也要能把已亮起的条子收干净
        target = 1.0 if self._tools_on else 0.0
        if self._tools_alpha != target:
            step = dt / TOOL_FADE_MS
            self._tools_alpha = (min(1.0, self._tools_alpha + step)
                                 if self._tools_on
                                 else max(0.0, self._tools_alpha - step))
            self.update()

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

                # 命中桥先铺：alpha=1 肉眼看不见，但 Windows 分层窗口只对
                # 非全透明像素做命中测试。气泡与按钮之间只要有一格 alpha=0，
                # 指针就会漏到桌面、leaveEvent 一响按钮全灭。
                rects, _zone = self._toolbar()
                if not self._bridge.isEmpty():
                    p.fillRect(self._bridge, QColor(0, 0, 0, 1))

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

                # 工具条最后画：它压在气泡外侧那条带上
                if self._tools_alpha > 0.01 and rects:
                    label, _enabled = self._speech_state()
                    playing = label == "停止朗读"
                    collapsed = not bool(self.st.get("pet.show_bubble", True))
                    p.save()
                    p.setOpacity(self._tools_alpha)
                    for k, r in rects.items():
                        pet.draw_tool_button(
                            p, r, k, self.st,
                            hover=(k == self._tool_hover), playing=playing,
                            collapsed=collapsed)
                    p.restore()
            finally:
                p.end()
        except Exception:
            pass

    # -------------------------------------------------- 鼠标

    def _hit(self, pos) -> str:
        # 工具按钮压在气泡命中区的下沿上，必须先判，否则点按钮会当成点气泡
        tool = self._tool_at(pos)
        if tool:
            return "tool:" + tool
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
        tool = self._tool_at(ev.position().toPoint())
        if tool:
            # 按在按钮上不是拖拽：记下目标，松开时还在同一颗上才算点
            self._tool_press = tool
            ev.accept()
            return
        if self._edge_dir and self._hit(ev.position().toPoint()) != "frog":
            # 吸附态画布一半是空的，按在空处不该把蛙拖走
            return
        gp = ev.globalPosition().toPoint()
        self._drag = {"start": gp, "win": self.pos(),
                      "last_x": gp.x(), "moved": False}
        self._set_tools(False)
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
        if self._tool_press:
            kind, self._tool_press = self._tool_press, ""
            if self._tool_at(ev.position().toPoint()) == kind:
                self._emit_tool(kind)
            ev.accept()
            return
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

    def set_bubble_visible(self, on: bool) -> None:
        """收起 / 展开气泡。工具条不会跟着消失，收起态的那颗按钮负责展开。"""
        on = bool(on)
        if on == bool(self.st.get("pet.show_bubble", True)):
            return
        # 先在**旧配置**下量画布全局：改了配置再量，_canvas_global 会拿
        # 新布局的画布偏移配旧窗口位置，吸在底边时画布会悄悄挪几像素
        keep = self._canvas_global() if self._edge_dir else None
        self.st.set("pet.show_bubble", on)
        self.st.save_config()
        if self._edge_dir:
            self._reflow_edge(keep)
        else:
            self._place(first=False)
        # 收起/展开会改窗口尺寸，指针可能瞬间落到窗外：别让按钮闪一下就没
        self._leave_timer.stop()
        self.update()

    def _toggle_bubble(self):
        self.set_bubble_visible(not bool(self.st.get("pet.show_bubble", True)))
