"""奶蛙贴边吸附：素材、方向判定、摆位、相位状态机。

纯逻辑模块，不认识 QWidget：窗口只管问「现在该画哪张、我该挪到哪」。

唯一的硬规则来自素材规格（`assets/naiwa/edges/SPEC.md`）——
**画布贴着屏幕的那条边，就是切口所在的那条边**。所以：

  1. 吸附后的窗口位置完全由「贴哪条边」决定，不用读图里的像素；
  2. 贴边那一轴的位置**不能因为换帧而重算**，否则切口会离开屏幕边；
  3. 自由那一轴按「可见部分的中心」对齐，跟着鼠标走。

方向判定用蛙的**可见矩形**而不是窗口矩形：站立形态带着气泡，窗口能比蛙宽
一倍，按窗口算会在离屏幕还很远的时候就吸过去。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtGui import QImage, QPixmap, qAlpha

import assets

SNAP = 10                 # 吸附阈值：蛙到屏幕边的距离；只在几乎贴到边上才吸
BLINK_GAP_MS = (2000, 4000)
BLINK_MS = 160.0

# 对齐哪两条边。"" 表示那一轴自由，跟着鼠标走。
# 和素材规格里的方向映射是同一张表：dir 指的是**被遮挡**的那一侧，
# 所以吸附屏幕左边是 w（左边被切），不是 e。
PIN = {
    "w": ("L", ""),
    "e": ("R", ""),
    "n": ("", "T"),
    "s": ("", "B"),
    "nw": ("L", "T"),
    "ne": ("R", "T"),
    "sw": ("L", "B"),
    "se": ("R", "B"),
}


# ---------------------------------------------------------------- 方向判定

def pick_dir(frog: QRect, screen: QRect, snap: int = SNAP,
             blocked: frozenset[str] = frozenset()) -> str:
    """拖动结束时该吸到哪里：返回方向名，不该吸返回空串。

    四角优先于四边：到竖边和横边的距离**都**小于阈值就按角落处理，
    否则只吸最近的那一条。这条规则写在规格里，实现放这儿，免得
    下次改阈值时两边不一致。

    蛙已经拖到屏幕外时距离按 0 算——松手就吸回来，而不是让它停在
    看不见的地方（否则用户只能靠托盘菜单把它找回来）。

    blocked 是「这条边不能吸」：多屏时相邻那条边外面还是桌面，
    吸过去等于卡在两块屏中间。传 "L"/"R"/"T"/"B"，判定在窗口那边做
    （只有它知道屏幕布局）。
    """
    big = 10 ** 6
    dl = big if "L" in blocked else max(0, frog.left() - screen.left())
    dr = big if "R" in blocked else max(0, screen.right() - frog.right())
    dt = big if "T" in blocked else max(0, frog.top() - screen.top())
    db = big if "B" in blocked else max(0, screen.bottom() - frog.bottom())

    h = min(dl, dr)
    v = min(dt, db)
    if h > snap and v > snap:
        return ""
    if h <= snap and v <= snap:
        return ("n" if dt <= db else "s") + ("w" if dl <= dr else "e")
    if h <= snap:
        return "w" if dl <= dr else "e"
    return "n" if dt <= db else "s"


def snap_pos(d: str, base_box, canvas: int, scale: float, size: QSize,
             anchor: QPoint, screen: QRect, off: QPoint | None = None) -> QPoint:
    """吸附后窗口该放哪。

    base_box 是画布内「基准帧可见部分」的包围盒（来自 meta.json），
    anchor 是希望**蛙可见部分的中心**落在的全局坐标——拖到哪就吸到哪，
    垂直于贴边方向的位置跟着走。

    size 是**整窗**尺寸而不是画布：吸附态窗口还带着气泡，比 256 宽一截，
    贴右边时要减整窗宽度，否则气泡会被顶到屏幕外去。off 是画布在窗口里
    的偏移——只对**自由轴**有用：贴边那一轴上画布永远顶着切口那一侧，
    所以窗口边就等于画布边，不用管 off。

    贴边那一轴由屏幕边直接算出，不参与 clamp：窗口边缘离开屏幕边，
    切口就离了屏幕边，那是这套素材唯一的硬约束。
    """
    h, v = PIN[d]
    off = off or QPoint(0, 0)
    vcx = (base_box[0] + base_box[2]) / 2.0 * scale
    vcy = (base_box[1] + base_box[3]) / 2.0 * scale
    ww, wh = size.width(), size.height()

    if h == "L":
        x = screen.left()
    elif h == "R":
        x = screen.right() - ww + 1
    else:
        # 自由轴夹的是**整窗**：气泡也得留在屏幕里
        x = round(anchor.x() - vcx) - off.x()
        x = max(screen.left(), min(x, screen.right() - ww + 1))

    if v == "T":
        y = screen.top()
    elif v == "B":
        y = screen.bottom() - wh + 1
    else:
        y = round(anchor.y() - vcy) - off.y()
        y = max(screen.top(), min(y, screen.bottom() - wh + 1))

    return QPoint(x, y)


# ---------------------------------------------------------------- 素材

class EdgeAssets:
    """懒加载 `assets/naiwa/edges/` 下的 24 张图与 meta.json。

    按需加载：一次会话通常只用到一个方向，没必要开局把 24 张全解成位图。
    缺素材时 `load()` 返回 False，窗口就当没这功能——吸附失败不能连累
    整只奶蛙不能用。
    """

    def __init__(self) -> None:
        self._meta: dict | None = None
        self._ok: bool | None = None
        self._scale = 1.0
        self._imgs: dict[tuple[str, str, float], QImage] = {}
        self._pms: dict[tuple[str, str, float], QPixmap] = {}

    # -------------------------------------------------- 加载

    def load(self) -> bool:
        if self._ok is not None:
            return self._ok
        try:
            raw = Path(assets.naiwa("edges/meta.json")).read_text(encoding="utf-8")
            meta = json.loads(raw)
            dirs = meta.get("dirs") or {}
            self._meta = meta if dirs else None
            self._ok = self._meta is not None
        except Exception:
            self._meta = None
            self._ok = False
        return self._ok

    @property
    def ok(self) -> bool:
        return self.load()

    # -------------------------------------------------- 尺寸

    def set_scale(self, scale: float) -> None:
        scale = max(0.4, min(2.0, float(scale)))
        if abs(scale - self._scale) < 1e-3:
            return
        self._scale = scale
        self._imgs.clear()
        self._pms.clear()

    @property
    def scale(self) -> float:
        return self._scale

    def canvas(self) -> int:
        meta = self._meta or {}
        return int(meta.get("canvas", 256))

    def size(self) -> QSize:
        """吸附态窗口的边长：素材画布 × 缩放系数。"""
        n = max(1, round(self.canvas() * self._scale))
        return QSize(n, n)

    def geo(self, d: str) -> dict | None:
        return ((self._meta or {}).get("dirs") or {}).get(d)

    def phases(self) -> tuple[str, ...]:
        ph = (self._meta or {}).get("phases") or ["base"]
        return tuple(ph)

    # -------------------------------------------------- 取图

    def _image(self, d: str, phase: str) -> QImage | None:
        key = (d, phase, round(self._scale, 3))
        if key in self._imgs:
            return self._imgs[key]

        path = assets.naiwa(f"edges/edge-{d}-{phase}.png")
        if not Path(path).exists() and phase != "base":
            # 缺相位就退回常态，别留空窗
            return self._image(d, "base")
        img = QImage(str(path))
        if img.isNull():
            self._imgs[key] = QImage()
            return None
        if img.format() != QImage.Format.Format_ARGB32:
            img = img.convertToFormat(QImage.Format.Format_ARGB32)
        if round(self._scale, 3) != 1.0:
            n = max(1, round(img.width() * self._scale))
            m = max(1, round(img.height() * self._scale))
            img = img.scaled(n, m,
                             Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        self._imgs[key] = img
        return img

    def pixmap(self, d: str, phase: str) -> QPixmap | None:
        key = (d, phase, round(self._scale, 3))
        pm = self._pms.get(key)
        if pm is not None:
            return pm
        img = self._image(d, phase)
        if img is None or img.isNull():
            return None
        pm = QPixmap.fromImage(img)
        self._pms[key] = pm
        return pm

    # -------------------------------------------------- 命中

    def hit(self, d: str, phase: str, x: int, y: int) -> bool:
        """(x, y) 是否压在蛙身上（按 alpha 判，不按矩形）。

        坐标是**窗口坐标**——缓存的图已经按 scale 缩放过，和窗口一样大。

        窗口是 256 见方，可见像素只占 14%~34%，左右贴边时半边都是空的。
        按矩形判的话，指针还离蛙十几像素它就开始挥手了。
        """
        img = self._image(d, phase)
        if img is None or img.isNull():
            return False
        x, y = int(x), int(y)
        if not (0 <= x < img.width() and 0 <= y < img.height()):
            return False
        return qAlpha(img.pixel(x, y)) > 24


# ---------------------------------------------------------------- 相位

class EdgeState:
    """吸附态的相位：`base` / `blink` / `hi`，外加朗读口型。

    相位不进循环——眨眼是 2~4 秒随机闪一帧，抬手由鼠标决定，鼠标不走
    它就一直抬着。这正是「挂在墙上跟人对视」该有的样子。

    口型是另一条独立的时间轴：朗读时按 MOUTH_CYCLE 慢慢开合，停下立刻
    闭嘴。它不换素材图、不动窗口几何，只在画布上多画一小块，所以和
    眨眼 / 抬手可以叠加，互不打断。
    """

    # 贴边形态身体完全静止、只眨眼，口型比站立的 16fps 更克制：
    # 12 帧 @10fps = 1.2 秒一轮，两次短开合 + 长停顿。
    # 形状是 (宽, 高)，高 0 = 闭口，直接用素材原本那条斜嘴线。
    # 和 make_pet_sheet.MOUTH_CYCLE 分开定义：贴边嘴只有 14px 宽、还是
    # 斜的，照搬站立那套 24 帧只会显得碎。
    MOUTH_FPS = 10
    MOUTH_CYCLE = (
        (0, 0), (0, 0),
        (12, 2), (14, 4), (12, 2), (0, 0),
        (0, 0), (13, 3), (15, 5), (13, 3), (0, 0), (0, 0),
    )

    def __init__(self) -> None:
        self._phase = "base"
        self._hover = False
        self._gap = 0.0          # 距下次眨眼的剩余 ms
        self._blink = 0.0        # 本次眨眼剩余 ms
        self._reading = False
        self._mouth_ms = 0.0
        self._mouth = (0, 0)     # 当前口型 (宽, 高)

    # -------------------------------------------------- 查询

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def hovering(self) -> bool:
        return self._hover

    @property
    def mouth_shape(self) -> tuple[int, int]:
        """当前该画的口型；(0, 0) 表示闭口，什么都不画。"""
        return self._mouth

    # -------------------------------------------------- 指令

    def reset(self) -> None:
        self._phase = "base"
        self._hover = False
        self._blink = 0.0
        self._gap = random.uniform(*BLINK_GAP_MS)
        self._mouth = (0, 0)
        self._mouth_ms = 0.0
        # _reading 故意不清：吸上去 / 摘下来都会 reset，主窗那会儿不会
        # 再同步一次 base，清了嘴就再也不动了

    def set_reading(self, on: bool) -> None:
        """由主窗的 base 状态驱动；停下立即闭嘴。"""
        self._reading = bool(on)
        if not on:
            self._mouth = (0, 0)
            self._mouth_ms = 0.0

    def set_hover(self, on: bool) -> bool:
        """鼠标进/出蛙身，返回要不要重绘。"""
        on = bool(on)
        if on == self._hover:
            return False
        self._hover = on
        if on:
            # 正在眨眼就先闪完再抬手：抬手是个短促动作，
            # 中途眼皮一合看着像抽搐
            if self._blink > 0:
                return False
            return self._switch("hi")
        if self._phase == "hi":
            return self._switch("base")
        return False

    def tick(self, dt_ms: float) -> bool:
        """推进相位与口型，返回要不要重绘。

        口型先算：原来的 tick 在「正在眨眼」和「抬手中」两处提前返回，
        直接挂在末尾的话那两段里嘴会卡住不动。
        """
        dirty = self._tick_mouth(dt_ms)
        if self._blink > 0:
            self._blink -= dt_ms
            if self._blink > 0:
                return dirty
            self._blink = 0.0
            # 眨眼结束时鼠标还压在上面就直接抬手，别再闪一帧 base
            return self._switch("hi" if self._hover else "base") or dirty

        if self._phase == "hi":
            return dirty

        self._gap -= dt_ms
        if self._gap > 0:
            return dirty
        self._gap = random.uniform(*BLINK_GAP_MS)
        self._blink = BLINK_MS
        return self._switch("blink") or dirty

    def _tick_mouth(self, dt_ms: float) -> bool:
        if not self._reading:
            if self._mouth == (0, 0):
                return False
            self._mouth = (0, 0)
            self._mouth_ms = 0.0
            return True
        self._mouth_ms += dt_ms
        step = 1000.0 / self.MOUTH_FPS
        shape = self.MOUTH_CYCLE[int(self._mouth_ms / step) % len(self.MOUTH_CYCLE)]
        if shape == self._mouth:
            return False
        self._mouth = shape
        return True

    # -------------------------------------------------- 内部

    def _switch(self, name: str) -> bool:
        if name == self._phase:
            return False
        self._phase = name
        return True
