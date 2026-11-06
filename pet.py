"""奶蛙：精灵表切帧、动画状态机、蛙与气泡的自绘。

只管「画」和「现在该播哪一帧」，不认识 Reader，也不碰朗读。
所有业务时长由调用方给（音效长度反推），这里不写死任何时间假设。
"""
from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QRect, QRectF, QSize, Qt
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPainterPath,
                         QPen, QPixmap)

import assets
import theme

# ---------------------------------------------------------------- 常量

IDLE = "idle"
READING = "reading"        # 朗读中：呼吸 + 开口说话（构建期把口型烘进精灵表）
RUNNING = "running"
THINKING = "thinking"      # 停下朗读后的姿势：抬手托腮，播一次就停住（不循环）
WAITING = "waiting"        # 抬手→放下→抱臂的原地小动作，只在朗读中偶尔播一次
WAVING = "waving"
JUMPING = "jumping"
REVIEW = "review"
FAILED = "failed"
RUN_LEFT = "running-left"
RUN_RIGHT = "running-right"

BASE_STATES = (IDLE, READING, RUNNING, THINKING)

# 朗读进行中随机播的小动作：抬手放下、抬头看一眼、原地跑两步。
# 都挑没有音效的——朗读时突然笑一声是另一种打扰
AMBIENT_ACTS = (WAITING, REVIEW, RUNNING)

# 瞬时动作的抢占权，大的能打断小的
PRIORITY = {IDLE: 0, READING: 0, RUNNING: 0, THINKING: 0,
            WAITING: 1, WAVING: 1, JUMPING: 1, REVIEW: 2, FAILED: 3,
            RUN_LEFT: 5, RUN_RIGHT: 5}

# base 之间切换的最小驻留，否则快速翻页会闪
MIN_DWELL_MS = 300

BUBBLE_PAD_X = 14
BUBBLE_PAD_Y = 11
BUBBLE_RADIUS = 12
BUBBLE_TAIL = 9          # 尾巴高度，画在气泡主体下方
BUBBLE_MAX_LINES = 3
BUBBLE_MIN_W = 120

# 不能出现在行首的标点：断在这里会把「。」孤零零甩到下一行
NO_LINE_START = set("，。、；：！？）】》」』”’,.;:!?)]}%")


# ---------------------------------------------------------------- 精灵表

class PetSprite:
    """懒加载 sheet.png，按 manifest 切帧，按 scale 缓存缩放后的 QPixmap。

    缩放只算一次：12fps 下每帧都做平滑缩放纯属浪费。
    """

    def __init__(self) -> None:
        self._sheet: QPixmap | None = None
        self._meta: dict | None = None
        self._ok: bool | None = None
        self._scale = 1.0
        self._cache: dict[tuple[str, float], list[QPixmap]] = {}

    # -------------------------------------------------- 加载

    def load(self) -> bool:
        """失败不抛异常：奶读模式退化成空窗口，不能连带程序一起挂。"""
        if self._ok is not None:
            return self._ok
        try:
            meta_path = assets.naiwa("manifest.json")
            self._meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            pm = QPixmap(str(assets.naiwa("sheet.png")))
            self._sheet = None if pm.isNull() else pm
            self._ok = self._sheet is not None
        except Exception:
            self._meta = None
            self._sheet = None
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
        self._cache.clear()

    @property
    def scale(self) -> float:
        return self._scale

    def _frame_size(self) -> tuple[int, int]:
        meta = self._meta or {}
        return int(meta.get("frame_w", 192)), int(meta.get("frame_h", 208))

    def size(self) -> QSize:
        """缩放后的单帧尺寸，所有动画同尺寸。"""
        fw, fh = self._frame_size()
        return QSize(max(1, round(fw * self._scale)), max(1, round(fh * self._scale)))

    # -------------------------------------------------- 取帧

    def _info(self, anim: str) -> dict:
        anims = (self._meta or {}).get("anims", {})
        return anims.get(anim) or anims.get(IDLE) or {
            "row": 0, "frames": 1, "fps": 8, "loop": True}

    def fps(self, anim: str) -> int:
        return max(1, int(self._info(anim).get("fps", 10)))

    def loops(self, anim: str) -> bool:
        return bool(self._info(anim).get("loop", True))

    def frames(self, anim: str) -> list[QPixmap]:
        if not self.load():
            return []
        key = (anim, round(self._scale, 3))
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        info = self._info(anim)
        fw, fh = self._frame_size()
        cols = int((self._meta or {}).get("cols", 8))
        row = int(info.get("row", 0))
        n = int(info.get("frames", 1))
        size = self.size()

        out: list[QPixmap] = []
        for c in range(max(1, min(n, cols))):
            cell = self._sheet.copy(c * fw, row * fh, fw, fh)
            if (size.width(), size.height()) != (fw, fh):
                cell = cell.scaled(size.width(), size.height(),
                                   Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            out.append(cell)
        self._cache[key] = out
        return out


# ---------------------------------------------------------------- 状态机

class PetState:
    """三层结构：base 持续态 / transient 一次性 / direction 独占。

    对应计划里的冲突消解规则：
      1. base 变化不打断正在播的 transient，只改「回落后去哪」
      2. 高优先级抢低优先级，同级不重入
      3. 拖拽方向独占，期间忽略一切触发
      4. 同名动画重播也从第 0 帧起（点一下必须有反应）
      5. base 之间切换最小驻留 MIN_DWELL_MS
      6. 一次性动画在 hold 期间循环播放，铺满整段音频
    """

    def __init__(self, sprite: PetSprite) -> None:
        self.sprite = sprite
        self._base = IDLE
        self._cur = IDLE
        self._frame = 0
        self._acc = 0.0            # 帧累加器 ms
        self._now = 0.0            # 全局时钟 ms
        self._started = 0.0        # 当前动画开始时刻
        self._hold_until = 0.0     # 瞬时动作到期时刻
        self._pending = None       # 受驻留限制、还没落地的 base
        self._locked = ""          # 拖拽锁定的方向动画

    # -------------------------------------------------- 查询

    @property
    def current(self) -> str:
        return self._cur

    @property
    def base(self) -> str:
        return self._base

    @property
    def frame(self) -> int:
        return self._frame

    @property
    def busy(self) -> bool:
        """有一次性动作在播。"""
        return bool(self._hold_until)

    # -------------------------------------------------- 指令

    def set_base(self, name: str) -> None:
        if name not in BASE_STATES:
            name = IDLE
        if name == self._base:
            return
        self._base = name
        self._pending = name
        self._apply_pending()

    def play(self, name: str, hold_ms: float = 0.0) -> bool:
        """播一次性动作，返回是否真的切了。"""
        if self._locked:
            return False
        if self._hold_until and PRIORITY.get(name, 0) < PRIORITY.get(self._cur, 0):
            return False
        if hold_ms <= 0:
            hold_ms = 1000.0 / max(1, self.sprite.fps(name)) * 3
        self._hold_until = self._now + hold_ms
        self._pending = None
        self._switch(name)
        return True

    def lock(self, name: str) -> None:
        if self._locked == name:
            return
        self._locked = name
        self._hold_until = 0.0
        self._pending = None
        self._switch(name)

    def unlock(self) -> None:
        if not self._locked:
            return
        self._locked = ""
        self._switch(self._base)

    def reset(self) -> None:
        self._hold_until = 0.0
        self._locked = ""
        self._pending = None
        if self._base not in BASE_STATES:
            self._base = IDLE
        self._switch(self._base)

    # -------------------------------------------------- 推进

    def _switch(self, name: str) -> None:
        self._cur = name
        self._frame = 0
        self._acc = 0.0
        self._started = self._now

    def _apply_pending(self, force: bool = False) -> None:
        if self._pending is None or self._locked or self._hold_until:
            return
        if not force and self._now - self._started < MIN_DWELL_MS:
            return          # 刚切过，留到下个 tick
        name, self._pending = self._pending, None
        if name != self._cur:
            self._switch(name)

    def tick(self, dt_ms: float) -> bool:
        """推进时钟，返回是否需要重绘（动画名或帧号变了）。"""
        self._now += dt_ms

        reloaded = False
        if self._hold_until and self._now >= self._hold_until:
            self._hold_until = 0.0
            self._pending = self._base
            self._apply_pending(force=True)
            reloaded = True
        else:
            self._apply_pending()

        fps = self.sprite.fps(self._cur)
        step = 1000.0 / fps
        self._acc += dt_ms
        if self._acc < step:
            return reloaded

        # 低帧率下 dt 可能跨过好几帧，别一次只进一帧
        advance = int(self._acc // step)
        self._acc -= advance * step
        total = len(self.sprite.frames(self._cur)) or 1
        nxt = self._frame + advance
        if nxt >= total:
            if self.sprite.loops(self._cur) or self._hold_until:
                nxt %= total        # 一次性动画在 hold 期间继续循环，笑满整段
            else:
                nxt = total - 1     # 不循环就停在末帧
        if nxt == self._frame:
            return reloaded
        self._frame = nxt
        return True


# ---------------------------------------------------------------- 绘制

def draw_pet(p: QPainter, rect: QRect, pm: QPixmap) -> None:
    """把当前帧居中贴底画进 rect（蛙脚永远贴住矩形底边）。"""
    if pm is None or pm.isNull():
        return
    x = rect.x() + (rect.width() - pm.width()) // 2
    y = rect.y() + rect.height() - pm.height()
    p.drawPixmap(x, y, pm)


def draw_edge_mouth(p: QPainter, origin, scale: float,
                    cx: float, cy: float, angle: float,
                    w: float, h: float) -> None:
    """贴边吸附态的口型：一条沿斜嘴线旋转的小深色胶囊。

    cx / cy / angle 是 256 画布（未缩放）里的嘴线位置与倾角，来自
    `edges/meta.json`，由 `make_pet_edges.py` 逐方向量好；origin 是画布
    左上角在窗口里的位置。坐标全程按画布算，换气泡、改缩放都不会跑偏。

    口腔中心沿嘴线法线（指向下巴）偏 h*0.45，让原嘴线正好落在上缘，
    看着是「嘴张开了」而不是「嘴线变粗了」。闭口帧 h=0 不画，直接用
    素材原本那条线。
    """
    import math

    if h <= 0:
        return
    rad = math.radians(angle)
    k = scale
    x = origin.x() + (cx - math.sin(rad) * h * 0.45) * k
    y = origin.y() + (cy + math.cos(rad) * h * 0.45) * k
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(x, y)
    # QPainter 顺时针为正，正好对应图像坐标（y 向下）量出来的主轴角
    p.rotate(angle)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(62, 18, 22, 240))
    p.drawRoundedRect(QRectF(-w * k / 2, -h * k / 2, w * k, h * k),
                      h * k / 2, h * k / 2)
    p.restore()


def _wrap(fm: QFontMetrics, text: str, width: int, max_lines: int) -> list[str]:
    """按像素宽度折行，超出行数末行加省略号。

    中文没空格，所以逐字贪心，但有两条排版规矩要守：
    - ASCII 单词尽量整词换行，避免「Interli / ne」这种断法
    - 避头点：断行点后面紧跟标点时把标点悬挂在本行末尾，
      否则「下雨。」的句号会被孤零零甩到下一行开头
    """
    text = " ".join((text or "").split())
    if not text:
        return []

    lines: list[str] = []
    cur = ""
    i, n = 0, len(text)
    overflow = False

    while i < n:
        ch = text[i]
        if fm.horizontalAdvance(cur + ch) <= width:
            cur += ch
            i += 1
            continue

        if not cur:                 # 单字就超宽，硬塞进去，别死循环
            cur = ch
            i += 1

        cut = len(cur)
        if (ch.isascii() and ch.isalnum()
                and cur[-1].isascii() and cur[-1].isalnum()):
            sp = cur.rfind(" ")
            if sp > 0:
                cut = sp

        if cut == len(cur):         # 没走整词回退，才可以悬挂标点
            while i < n and text[i] in NO_LINE_START:
                cur += text[i]
                i += 1
            cut = len(cur)

        lines.append(cur[:cut])
        cur = cur[cut:].lstrip()
        if len(lines) >= max_lines:
            overflow = i < n or bool(cur)
            break

    if cur:
        if len(lines) < max_lines:
            lines.append(cur)
        else:
            overflow = True

    if overflow and lines:
        last = lines[-1]
        while last and fm.horizontalAdvance(last + "…") > width:
            last = last[:-1]
        lines[-1] = last + "…"

    return lines[:max_lines]


def line_height(font: QFont) -> int:
    return QFontMetrics(font).height() + 3


def measure_bubble(font: QFont, text: str, max_w: int) -> QSize:
    """气泡主体尺寸，不含尾巴。"""
    fm = QFontMetrics(font)
    inner = max(40, max_w - 2 * BUBBLE_PAD_X)
    lines = _wrap(fm, text, inner, BUBBLE_MAX_LINES) or [""]
    widest = max((fm.horizontalAdvance(s) for s in lines), default=0)
    w = max(BUBBLE_MIN_W, min(max_w, widest + 2 * BUBBLE_PAD_X + 2))
    h = len(lines) * line_height(font) + 2 * BUBBLE_PAD_Y
    return QSize(int(w), int(h))


def draw_bubble(p: QPainter, body: QRect, font: QFont, st, text: str,
                tail: str = "down", anchor: float | None = None) -> None:
    """气泡主体 + 指向蛙头的小尾巴。body 不含尾巴。

    tail 是尾巴伸出的那一侧（`down` / `up` / `left` / `right`）。站着的时候
    蛙在气泡下面，尾巴朝下；贴到屏幕左边时气泡在蛙的右侧，尾巴得朝左。
    anchor 是尾巴落点在「尾巴那条边」上的坐标（朝上下时是 x，朝左右时是 y），
    默认取中点；给了就让尾巴尽量对着蛙头，读起来才像在说话。
    """
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    bg = QColor(st.get("colors.bg", "#0d0f12"))
    # alpha 夹到 1：0 时 Windows 不把这块算进命中测试，点击会穿到下面窗口去
    bg.setAlpha(max(1, min(255, int(st.get("bg_alpha", 236)))))
    fg = QColor(st.get("colors.fg", "#c3cbd6"))

    bodyf = QRectF(body).adjusted(0.5, 0.5, -0.5, -0.5)
    path = QPainterPath()
    path.addRoundedRect(bodyf, BUBBLE_RADIUS, BUBBLE_RADIUS)

    t = QPainterPath()
    if tail in ("up", "down"):
        cx = (bodyf.center().x() if anchor is None
              else min(max(float(anchor), bodyf.left() + BUBBLE_RADIUS),
                       bodyf.right() - BUBBLE_RADIUS))
        if tail == "down":
            y0, tip = bodyf.bottom() - 1.0, BUBBLE_TAIL
        else:
            y0, tip = bodyf.top() + 1.0, -BUBBLE_TAIL
        t.moveTo(cx - BUBBLE_TAIL, y0)
        t.lineTo(cx, y0 + tip)
        t.lineTo(cx + BUBBLE_TAIL, y0)
    else:
        cy = (bodyf.center().y() if anchor is None
              else min(max(float(anchor), bodyf.top() + BUBBLE_RADIUS),
                       bodyf.bottom() - BUBBLE_RADIUS))
        if tail == "right":
            x0, tip = bodyf.right() - 1.0, BUBBLE_TAIL
        else:
            x0, tip = bodyf.left() + 1.0, -BUBBLE_TAIL
        t.moveTo(x0, cy - BUBBLE_TAIL)
        t.lineTo(x0 + tip, cy)
        t.lineTo(x0, cy + BUBBLE_TAIL)
    t.closeSubpath()
    path = path.united(t)

    p.fillPath(path, bg)
    p.setPen(QPen(theme.overlay_pen(st, 44), 1.0))
    p.drawPath(path)

    fm = QFontMetrics(font)
    inner = body.adjusted(BUBBLE_PAD_X, BUBBLE_PAD_Y, -BUBBLE_PAD_X, -BUBBLE_PAD_Y)
    p.setFont(font)
    p.setPen(fg)
    y = inner.y()
    for s in _wrap(fm, text, inner.width(), BUBBLE_MAX_LINES):
        # 用 (x, y) 重载：QRect + flags 那套在不同 Qt 版本上行为有差异
        p.drawText(inner.x(), y + fm.ascent(), s)
        y += line_height(font)
