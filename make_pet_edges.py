"""奶蛙吸附边缘素材：构建期工具（只产美术，不含吸附逻辑）。

    python make_pet_edges.py --prep     # 切参考帧、出 1024 参考图
    python make_pet_edges.py --build    # 抠图 + 摆放 + 八个方向裁切
    python make_pet_edges.py --sheet    # 出一张预览拼图
    python make_pet_edges.py --context  # 出一张模拟桌面贴边的场景图

产物：assets/naiwa/edges/edge-{n,ne,e,se,s,sw,w,nw}.png（256x256 RGBA）

素材来源是两个目录，各取第一张：
    build/naiwa_edges/raw/     斜姿态。出七张（四边里的三条 + 四角）
    build/naiwa_edges/raw_n/   探身下望。只出 edge-n；缺省则退回对半切

设计约定（接入时唯一需要记住的规则）：
    画布靠窗口的那条边 == 窗口边缘。
    edge-w 的右边缘、edge-e 的左边缘、edge-n 的下边缘、edge-s 的上边缘，
    四角则是两条边同时贴。所以切面永远落在画布边界上，不用记偏移量。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
SRC = HERE / "assets" / "naiwa" / "sheet.png"
OUT = HERE / "assets" / "naiwa" / "edges"
BUILD = HERE / "build" / "naiwa_edges"
REF = BUILD / "ref"

# 源姿态图跟成品放在一起，不进 build/：build/ 是 gitignore 的，
# 源图放那儿等于「重新克隆后 --build 会静默退化」，而退化只有肉眼看得出
SRC_SIDE = OUT / "src" / "side"     # 斜姿态，出七张
SRC_PEEK = OUT / "src" / "peek"     # 探身下望，只出顶边

CANVAS = 256          # 画布边长（正方形，四角留够斜探空间）
FROG_H = 224          # 摆位后蛙的高度：与站立帧 208 接近，吸附时不跳大小墙

# 顶边那张是单独作画的：蛙趴在边缘上往下探，我们从上后方看它的**背**，
# 肚皮压在边缘上根本看不到。这张的比例不能按身高归一——它画的是
# 「头 + 一大截背」，背有多长完全看模型心情，按整体归一头部就会缩没。
# 所以按头宽定尺度（蛙宽 187 时头宽约 84，与站立帧的 84~90 对得上），
# 再把超过 PEEK_MAX_H 的背裁掉：那段在屏幕外，裁了没人看得见。
PEEK_W = 187
PEEK_MAX_H = 150

DIRS = ("n", "ne", "e", "se", "s", "sw", "w", "nw")


# ---------------------------------------------------------------- 切参考帧

def idle_frame() -> Image.Image:
    meta = json.loads((HERE / "assets" / "naiwa" / "manifest.json")
                      .read_text(encoding="utf-8"))
    fw, fh = int(meta["frame_w"]), int(meta["frame_h"])
    sheet = Image.open(SRC).convert("RGBA")
    return sheet.crop((0, 0, fw, fh))


def prep() -> None:
    REF.mkdir(parents=True, exist_ok=True)
    SRC_SIDE.mkdir(parents=True, exist_ok=True)
    SRC_PEEK.mkdir(parents=True, exist_ok=True)
    fr = idle_frame()
    fr.save(REF / "idle.png")

    # 图生图参考：放大 + 深灰底（模型对透明底容易糊成白边）
    big = fr.resize((512, 555), Image.LANCZOS)
    canvas = Image.new("RGBA", (1024, 1024), (32, 36, 42, 255))
    canvas.alpha_composite(big, ((1024 - 512) // 2, (1024 - 555) // 2))
    canvas.convert("RGB").save(REF / "idle_ref.png")
    print(f"参考帧 = {REF / 'idle.png'}  ({fr.width}x{fr.height})")
    print(f"参考大图 = {REF / 'idle_ref.png'}  (1024x1024)")
    print(f"源姿态图放这里：{SRC_SIDE}（斜姿态）/ {SRC_PEEK}（探身下望）")


# ---------------------------------------------------------------- 抠图对齐

def _smooth(v, k: int = 11):
    pad = k // 2
    return np.convolve(np.pad(v, pad, mode="edge"), np.ones(k) / k, mode="valid")


def cutout(src: Path, size: tuple[int, int]) -> Image.Image:
    """把模型出的图裁到角色包围盒，缩到目标高度，返回透明底 RGBA。

    模型给的是深灰底，用「与四角平均色的距离」当 alpha：奶蛙通体亮黄，
    和深底差得远，阈值好定；硬阈值再用距离变换铺一层软边去锯齿。
    """
    import cv2

    im = Image.open(src).convert("RGB")
    a = np.asarray(im).astype(np.float32)

    corner = np.concatenate([a[:20, :20].reshape(-1, 3), a[:20, -20:].reshape(-1, 3),
                             a[-20:, :20].reshape(-1, 3), a[-20:, -20:].reshape(-1, 3)])
    bg = np.median(corner, axis=0)
    dist = np.linalg.norm(a - bg, axis=2)
    fg = (dist > 38).astype(np.uint8)

    # 填掉内部空洞（肚子上的浅色块被误判成背景）
    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    mask = np.zeros_like(fg)
    cv2.drawContours(mask, contours, -1, 1, -1)
    fg = mask

    soft = cv2.distanceTransform(fg, cv2.DIST_L2, 3)
    alpha = np.clip(soft / 1.6 * 255, 0, 255).astype(np.uint8)
    alpha[fg == 0] = 0

    rgba = np.dstack([np.asarray(im).astype(np.uint8), alpha])
    ys, xs = np.where(alpha > 8)
    box = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
    cut = Image.fromarray(rgba[box[1]:box[3], box[0]:box[2]], "RGBA")

    # 缩放到固定尺寸：size 里哪一项为 0 就按另一项等比算
    if size[1]:
        w = max(1, round(cut.width * size[1] / cut.height))
        return cut.resize((w, size[1]), Image.LANCZOS)
    h = max(1, round(cut.height * size[0] / cut.width))
    return cut.resize((size[0], h), Image.LANCZOS)


# ---------------------------------------------------------------- 摆位与遮罩

# 命名：edge-{方位} = 该方位被遮挡，蛙从对侧露出。
# 吸附到屏幕左边 → 左边被切 → edge-w（露出右半身）。
#
# 关键约束：切面必须落在**画布边缘**，不留在画布中间。
# 这样接入时只有一条规则——把画布靠屏幕的那条边对齐屏幕边缘，
# 不需要为每个方向记裁剪偏移。多余部分全是透明，不影响下面窗口的内容。
#
# 四边对半切；四角只切 45%，且**绝不切上边**：
# 头在角色最上方，任何水平切口都会把头切掉，四角宁可浅切也要保住脸，
# 靠垂直方向贴角（valign）来体现"在角落"。
SPEC = {
    "w":  dict(flip=True,  cut="left",   ratio=0.50, valign="center"),
    "e":  dict(flip=False, cut="right",  ratio=0.50, valign="center"),
    "n":  dict(flip=False, cut="top",    ratio=0.50, valign=None),
    "s":  dict(flip=False, cut="bottom", ratio=0.50, valign=None),
    "nw": dict(flip=True,  cut="left",   ratio=0.45, valign="top"),
    "sw": dict(flip=True,  cut="left",   ratio=0.45, valign="bottom"),
    "ne": dict(flip=False, cut="right",  ratio=0.45, valign="top"),
    "se": dict(flip=False, cut="right",  ratio=0.45, valign="bottom"),
}


def _alpha_box(im: Image.Image) -> tuple[int, int, int, int]:
    """可见像素的包围盒。

    不能用 Image.getbbox()：透明区的 RGB 还留着原图颜色，它会把整张
    图都算成不透明。只能按 alpha 阈值自己算。
    """
    a = np.asarray(im)[:, :, 3]
    ys, xs = np.where(a > 8)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _paste(canvas: Image.Image, part: Image.Image, x: int, y: int) -> None:
    """把 part 贴到 (x, y)，负坐标表示溢出、溢出部分丢掉。

    手写裁剪，不用 alpha_composite 的负 dest：实测 x 为负时会连带吃掉
    顶部的行，而这个函数的全部意义就是让切面精确压在画布边上，不能
    建立在库的实现细节上。
    """
    sx0, sy0 = max(0, -x), max(0, -y)
    dx0, dy0 = max(0, x), max(0, y)
    w = min(part.width - sx0, canvas.width - dx0)
    h = min(part.height - sy0, canvas.height - dy0)
    if w <= 0 or h <= 0:
        return
    canvas.alpha_composite(part.crop((sx0, sy0, sx0 + w, sy0 + h)), (dx0, dy0))


def place(frog: Image.Image, cut: str, ratio: float, valign: str | None) -> Image.Image:
    """把蛙摆到画布上，让被切的部分露在画布外。

    被切的那一侧直接摆到负坐标，超出画布的部分自然消失——切面因此
    正好压在画布边缘上，这就是接入时对齐的那条线。
    """
    c = CANVAS
    w, h = frog.size

    if cut == "left":
        x = -int(ratio * w)
    elif cut == "right":
        x = c - w + int(ratio * w)
    else:
        x = (c - w) // 2

    # 先裁掉水平溢出，再拿**剩下的可见像素**算垂直贴角。
    # 蛙头顶是不对称的（斜姿态 + 举手），镜像之后最高点可能整个落在
    # 被切掉的那一侧；用整张图的包围盒贴角会贴出一条十几像素的缝。
    dx = max(0, x)
    vis = frog.crop((max(0, -x), 0, min(w, c - dx), h))
    _, vy0, _, vy1 = _alpha_box(vis)

    if cut == "top":
        y = -int(ratio * h)
    elif cut == "bottom":
        y = c - h + int(ratio * h)
    elif valign == "top":
        y = -vy0                    # 贴角：可见像素顶到画布边
    elif valign == "bottom":
        y = c - vy1
    else:
        y = (c - h) // 2

    canvas = Image.new("RGBA", (c, c), (0, 0, 0, 0))
    _paste(canvas, vis, dx, y)
    return canvas


def place_peek(frog: Image.Image) -> Image.Image:
    """顶边专用：蛙趴在边缘上往下探，身子在屏幕外。

    这张图本身画的就是「头在下、身体被画面上边切平」的构图，所以不套
    上面那套裁切比例——可见像素顶到画布上边，切口自然就落在那里。
    """
    c = CANVAS
    canvas = Image.new("RGBA", (c, c), (0, 0, 0, 0))
    _paste(canvas, frog, (c - frog.width) // 2, 0)
    return canvas


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    srcs = sorted(SRC_SIDE.glob("*.png"))
    if not srcs:
        print(f"没有素材：把图生图结果放进 {SRC_SIDE}")
        return

    base = cutout(srcs[0], (0, FROG_H))
    mirror = base.transpose(Image.FLIP_LEFT_RIGHT)
    print(f"基准蛙 = {base.width}x{base.height}")

    # 顶边单独一张姿态（探身下望）。没有就用对半切的老办法兜底，
    # 那种切法只剩腹部和脚，能用但看不出是蛙
    peek = None
    n_src = sorted(SRC_PEEK.glob("*.png")) if SRC_PEEK.is_dir() else []
    if n_src:
        peek = cutout(n_src[0], (PEEK_W, 0))
        if peek.height > PEEK_MAX_H:
            # 只留头上面一小截背，再往上都是要藏到屏幕外的
            peek = peek.crop((0, peek.height - PEEK_MAX_H, peek.width, peek.height))
        print(f"顶边姿态 = {peek.width}x{peek.height}  ({n_src[0].name})")
    else:
        print(f"顶边姿态 = 缺省，edge-n 退回对半切（放一张进 {SRC_PEEK} 可换掉）")

    for d in DIRS:
        cfg = SPEC[d]
        if d == "n" and peek is not None:
            img = place_peek(peek)
        else:
            frog = mirror if cfg["flip"] else base
            img = place(frog, cfg["cut"], cfg["ratio"], cfg["valign"])
        img.save(OUT / f"edge-{d}.png", "PNG")
        alpha = np.asarray(img)[:, :, 3]
        print(f"  edge-{d}.png  {img.width}x{img.height}  "
              f"占画布 {(alpha > 8).mean()*100:.0f}%")


def sheet() -> None:
    """预览：八个方向按 3x3 摆开，中间写方向名，看整体调性。"""
    from PIL import ImageDraw

    cell = CANVAS
    gap = 12
    pad = 24
    w = pad * 2 + cell * 3 + gap * 2
    h = pad * 2 + cell * 3 + gap * 2
    bg = Image.new("RGBA", (w, h), (24, 27, 32, 255))
    d = ImageDraw.Draw(bg)

    grid = [("nw", 0, 0), ("n", 1, 0), ("ne", 2, 0),
            ("w", 0, 1), (None, 1, 1), ("e", 2, 1),
            ("sw", 0, 2), ("s", 1, 2), ("se", 2, 2)]
    for name, cx, cy in grid:
        x = pad + cx * (cell + gap)
        y = pad + cy * (cell + gap)
        # 窗口示意：画布靠窗口的那两条边描成实线，方便肉眼检查切面是否平
        d.rectangle([x, y, x + cell - 1, y + cell - 1], outline=(60, 66, 76, 255))
        if name is None:
            d.text((x + cell // 2 - 18, y + cell // 2 - 6), "corner", fill=(90, 98, 110))
            continue
        img = Image.open(OUT / f"edge-{name}.png")
        bg.alpha_composite(img, (x, y))
        d.text((x + 6, y + 6), name, fill=(150, 160, 175))

    out = HERE / "screenshots" / "pet-edges.png"
    bg.convert("RGB").save(out)
    print(f"预览 = {out}  ({bg.width}x{bg.height})")


def context() -> None:
    """场景预览：把八张按「画布边 = 屏幕边」摆到模拟桌面上。

    这张图是给接入方和评审看的——能一眼看出切面是否真的贴合屏幕边缘，
    以及四角/四边在小尺寸下的观感。
    """
    from PIL import ImageDraw

    W, H, RIM = 1280, 780, 26
    img = Image.new("RGBA", (W, H), (8, 10, 13, 255))
    d = ImageDraw.Draw(img)

    sx0, sy0, sx1, sy1 = RIM, RIM, W - RIM, H - RIM
    d.rectangle([sx0, sy0, sx1, sy1], fill=(30, 33, 39, 255))

    # 一张模拟应用窗口，用来判断"蛙压在内容上"是否协调
    wx0, wy0, wx1, wy1 = sx0 + 210, sy0 + 120, sx1 - 210, sy1 - 150
    d.rounded_rectangle([wx0, wy0, wx1, wy1], radius=10, fill=(22, 25, 30, 255),
                        outline=(52, 57, 66, 255))
    d.rounded_rectangle([wx0, wy0, wx1, wy0 + 30], radius=10, fill=(38, 42, 50, 255))
    d.text((wx0 + 14, wy0 + 9), "mock window", fill=(120, 128, 140))

    c = CANVAS
    spots = {
        "nw": (sx0, sy0),                 "n": (W // 2 - c // 2, sy0),
        "ne": (sx1 - c, sy0),             "w": (sx0, H // 2 - c // 2),
        "e":  (sx1 - c, H // 2 - c // 2), "sw": (sx0, sy1 - c),
        "s":  (W // 2 - c // 2, sy1 - c), "se": (sx1 - c, sy1 - c),
    }
    for name, (x, y) in spots.items():
        fig = Image.open(OUT / f"edge-{name}.png")
        img.alpha_composite(fig, (x, y))
        # 标签放在蛙内侧，避免压到画面主体
        lx = x + c + 8 if x < W // 2 else x - 60
        ly = y + c // 2 if 0 < y < sy1 - c else y + c + 6
        d.text((lx, ly), name, fill=(96, 104, 116))

    out = HERE / "screenshots" / "pet-edges-context.png"
    img.convert("RGB").save(out)
    print(f"场景预览 = {out}  ({img.width}x{img.height})")


if __name__ == "__main__":
    if "--prep" in sys.argv:
        prep()
    elif "--build" in sys.argv:
        build()
    elif "--sheet" in sys.argv:
        sheet()
    elif "--context" in sys.argv:
        context()
    else:
        print(__doc__)
