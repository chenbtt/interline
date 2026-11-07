"""奶蛙吸附边缘素材：构建期工具（只产美术，不含吸附逻辑）。

    python make_pet_edges.py --prep     # 切参考帧、出 1024 参考图（图生图用）
    python make_pet_edges.py --build    # 抠图 + 对齐 + 七向三相位裁切 + 量嘴位
    python make_pet_edges.py --mouth    # 只补 meta.json 的嘴位，不动成品图
    python make_pet_edges.py --sheet    # 八向 base 帧 3x3 拼图
    python make_pet_edges.py --context  # 模拟桌面贴边的场景图
    python make_pet_edges.py --anim     # 胶片条 + GIF，验证动画

产物：assets/naiwa/edges/edge-{n,ne,e,se,sw,w,nw}-{base,blink,hi}.png
      21 张 256x256 RGBA
      assets/naiwa/edges/edge-s-p{1..4}-{base,blink}.png
      8 张，底边四套躺姿，**不由本脚本生成**（见下）

底边 s 是多姿态方向：四套躺姿分别图生图、逐张裁切落格，meta.json 里
写成 poses 数组。所以 --build 见到已有 poses 就整个跳过该方向，预览类
命令取第一套；否则一次重跑会把四套图和 poses 一起冲成单套，
运行时表现是「每次都同一个姿势」，很难当场发现。

素材来源是两个目录，文件名带相位后缀（@blink / @hi）：
    assets/naiwa/edges/src/side/   斜姿态，出七向（四边里的三条 + 四角）
    assets/naiwa/edges/src/peek/   探身下望，只出 n；缺省则退回对半切

设计约定（接入时唯一需要记住的规则）：
    画布靠窗口的那条边 == 窗口边缘。
    w 的右边缘、e 的左边缘、n 的下边缘、s 的上边缘，四角则是两条边同时贴。
    所以切面永远落在画布边界上，不用记偏移量。
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

# 呼吸相位。关着：吸附态只眨眼，身体一动不动（用户定的）。
# 打开就多出 in / out 两帧——源图还在 src/*/@in.png、@out.png，
# 改这一行重跑 --build 即可，不必重新生成。
# 关掉的理由：顶边挂着时动的是头，幅度一大就抢眼；侧姿态那点躯干胀缩
# 在 256px 上几乎看不出来，收益不值这套复杂度。
BREATH = False

DIRS = ("n", "ne", "e", "se", "s", "sw", "w", "nw")

# 相位：base 常态、blink 眨眼、hi 抬手（鼠标指上来时用）。
# 缺哪个就退回 base，不会空窗。
PHASES = (("base", "in", "out", "blink") if BREATH else ("base", "blink")) + ("hi",)


def _posed() -> dict[str, dict]:
    """meta.json 里已经是多姿态的方向（目前只有底边 s 的四套躺姿）。

    这些方向的素材不是 --build 切的：四套是分别图生图、逐张裁切落格的。
    所以 --build / --sheet / --context / --anim 一律绕开它们，别用单套的
    逻辑去覆盖——覆盖了不只是图被换掉，meta 里的 poses 也会被写回单套，
    运行时随机选姿势就失效了，而失效的样子是「每次都同一个姿势」，很容易漏看。
    """
    p = OUT / "meta.json"
    if not p.exists():
        return {}
    try:
        dirs = (json.loads(p.read_text(encoding="utf-8")) or {}).get("dirs") or {}
    except Exception:
        return {}
    return {d: g for d, g in dirs.items() if g.get("poses")}


def base_png(d: str) -> Path:
    """该方向用来做预览的 base 帧。多姿态方向取第一套。"""
    return png_for(d, "base")


def png_for(d: str, phase: str) -> Path:
    """该方向该相位的图。多姿态方向取第一套——预览够用，真随机在运行时。"""
    poses = (_posed().get(d) or {}).get("poses") or []
    if poses:
        return OUT / f"edge-{d}-{poses[0]['id']}-{phase}.png"
    return OUT / f"edge-{d}-{phase}.png"


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


def cutout(src: Path, size: tuple[int, int] = (0, 0)) -> Image.Image:
    """把模型出的图裁到角色包围盒，返回透明底 RGBA。

    size 为 0 表示不缩放（相位帧要按统一系数缩，见 _scale）。
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

    # 缩放到固定尺寸：size 里哪一项为 0 就按另一项等比算，两项都 0 就不缩
    if not size[0] and not size[1]:
        return cut
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


def _anchor_x(im: Image.Image) -> float:
    """取底部 1/4 可见像素的水平重心，当作「身体」的锚点。

    只对**基准帧**算一次，所有相位共用。每帧各算各的话，抬手帧少了一只手
    撑着、重心就偏，身体在 hover 时会横跳（实测顶边偏 18px）。

    取底部这一段是因为它最不动：侧姿态是脚，顶边那张头朝下，那就是头。
    """
    a = np.asarray(im)[:, :, 3] > 8
    h = a.shape[0]
    band = a[int(h * 0.75):]
    ys, xs = np.where(band)
    return float(xs.mean()) if len(xs) else a.shape[1] / 2


def _best_shift(ref: np.ndarray, ph: np.ndarray, rng: int = 96) -> tuple[int, int]:
    """相位帧要挪多少才能跟基准帧的身体重合：返回 (dy, dx)。

    判据是「剪影重叠面积最大」。抬手帧多出来的那只手伸在空白处，重叠面积
    为 0，挪到哪儿都不加分，所以最优解只由身体决定——换成包围盒居中或者
    重心对齐都会被那只手带偏（实测侧姿态 34px、顶边 18px）。

    用 FFT 做互相关再取峰，比按偏移量暴力搜索快两个数量级。
    """
    h = ref.shape[0] + ph.shape[0] + 2 * rng
    w = ref.shape[1] + ph.shape[1] + 2 * rng
    fa = np.fft.rfft2((ref > 8).astype(np.float32), (h, w))
    fb = np.fft.rfft2((ph > 8).astype(np.float32), (h, w))
    cc = np.fft.fftshift(np.fft.irfft2(fa * np.conj(fb), (h, w)))
    cy, cx = h // 2, w // 2
    win = cc[cy - rng:cy + rng + 1, cx - rng:cx + rng + 1]
    ky, kx = np.unravel_index(int(np.argmax(win)), win.shape)
    return int(ky - rng), int(kx - rng)


def _align(base: Image.Image,
           frames: dict[str, Image.Image]) -> dict[str, tuple[Image.Image, int, int]]:
    """把相位帧挪进基准帧的坐标系：返回 (图, dx, dy)。

    抬手帧是重新生成的，模型把整个蛙在画布里画偏了。各帧按自己的包围盒
    摆位的话，hover 时身体会平移，顶边那张举起来的手还会顶出画布被切掉。

    只算偏移、不动画布尺寸——偏移可以是负数（手伸到基准帧左边界之外），
    贴图时溢出画布的部分自然消失。
    """
    ref = np.asarray(base)[:, :, 3]
    out: dict[str, tuple[Image.Image, int, int]] = {"base": (base, 0, 0)}
    for phase, im in frames.items():
        if phase == "base":
            continue
        dy, dx = _best_shift(ref, np.asarray(im)[:, :, 3])
        out[phase] = (im, dx, dy)
    return out


def _geom(base: Image.Image, cut: str, amount: int, valign: str | None,
          anchor: float | None) -> tuple[int, int]:
    """算**基准帧**该摆在画布上的哪个位置；所有相位共用这一组坐标。

    amount 是像素、按基准帧算：切的是屏幕边缘，它不会因为蛙抬手而移动。
    左右贴边时横向位置由切口决定；上下两条边横向自由，靠 anchor（身体
    锚点该落在的 x）定；顶边那张的 anchor 就是头。

    anchor 为 None 且不是上下切口时按包围盒居中。
    """
    c = CANVAS
    w, h = base.size

    if cut == "left":
        x = -amount
    elif cut == "right":
        x = c - w + amount
    elif anchor is not None:
        x = round(anchor - _anchor_x(base))
    else:
        x = (c - w) // 2

    # 先裁掉水平溢出，再拿**剩下的可见像素**算垂直贴角。
    # 蛙头顶是不对称的（斜姿态 + 举手），镜像之后最高点可能整个落在
    # 被切掉的那一侧；用整张图的包围盒贴角会贴出一条十几像素的缝。
    vis = base.crop((max(0, -x), 0, min(w, c - max(0, x)), h))
    _, vy0, _, vy1 = _alpha_box(vis)

    if cut == "top":
        y = -amount
    elif cut == "bottom":
        y = c - h + amount
    elif valign == "top":
        y = -vy0                    # 贴角：可见像素顶到画布边
    elif valign == "bottom":
        y = c - vy1
    else:
        y = (c - h) // 2
    return x, y


def place(frog: Image.Image, x: int, y: int) -> Image.Image:
    """把一张相位帧贴到画布 (x, y)。负坐标表示溢出画布，溢出部分丢掉。

    被切的那一侧因此正好压在画布边缘上，这就是接入时对齐的那条线。
    """
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    _paste(canvas, frog, x, y)
    return canvas


def _trim_top(im: Image.Image, rows: int) -> Image.Image:
    """从顶部裁掉 rows 行。

    裁掉的那段是「要被屏幕边缘挡住的背」，裁多裁少都不影响切口位置——
    切口永远在画布上边。但**每帧必须裁一样多**（按基准帧算），否则相位
    之间会上下错位。想造上下浮动就逐帧改这个量。
    """
    return im.crop((0, rows, im.width, im.height)) if rows > 0 else im


def _frames(dir_path: Path) -> dict[str, Path]:
    """读一个姿态目录里的相位帧。

    文件名约定：`pose_x.png` 是基准帧，`pose_x@in.png` / `@out.png` /
    `@blink.png` 是它的变体。@ 后面就是相位名。
    """
    out: dict[str, Path] = {}
    for p in sorted(dir_path.glob("*.png")):
        phase = p.stem.split("@", 1)[1].lower() if "@" in p.stem else "base"
        out[phase] = p
    return out


def _scale(frames: dict[str, Path], *,
           h: int = 0, w: int = 0) -> dict[str, tuple[Image.Image, int, int]]:
    """缩放到**同一个系数**并挪进基准帧的坐标系。

    缩放不能偷懒写成「每帧各自缩到固定高度」：吸气帧本来就比基准帧高 2%，
    再被拉回同一高度，呼吸就被吃干净了，播起来一动不动。所以先用基准帧
    定系数 k，全体乘同一个 k。

    返回的 (图, dx, dy) 里 dx/dy 是相对基准帧左上角的偏移，见 _align。
    """
    base = cutout(frames["base"])
    k = (h / base.height) if h else (w / base.width)
    scaled: dict[str, Image.Image] = {}
    for phase, path in frames.items():
        im = base if phase == "base" else cutout(path)
        # 相位帧必须是同一构图下的重画。尺寸差太多只有两种可能：
        # 源图分辨率不一致（我把变体降到 512、基准还留 1024，就出过一次），
        # 或者模型干脆换了姿势。两种都不能用，直接喊出来。
        r = im.height / base.height
        if phase != "base" and not 0.85 < r < 1.15:
            print(f"  ！{phase} 帧高度是基准的 {r:.0%}，八成源分辨率不一致或换了姿势")
        scaled[phase] = im.resize((max(1, round(im.width * k)),
                                   max(1, round(im.height * k))), Image.LANCZOS)
    return _align(scaled["base"], scaled)


def _offs(frames: dict[str, tuple[Image.Image, int, int]]) -> str:
    """把各相位的对齐偏移打成一行，方便肉眼核对。"""
    return " ".join(f"{p}=({dx:+d},{dy:+d})" for p, (_, dx, dy) in sorted(frames.items()))


def oriented(frames: dict[str, tuple[Image.Image, int, int]],
             flip: bool) -> dict[str, tuple[Image.Image, int, int]]:
    """把一组相位帧整体镜像（西侧三个方向用），偏移跟着换算。

    偏移的定义是「自己的左上角落在基准帧坐标系的哪儿」。镜像轴是**基准帧
    自己的中轴**，不是 0——原图跨 [ox, ox+w]，翻过来跨 [W-ox-w, W-ox]，
    所以新偏移是 W - ox - w（基准帧自己的 ox=0 时结果仍是 0，不会整体跑掉）。
    """
    if not flip:
        return frames
    mid = frames["base"][0].width
    return {p: (im.transpose(Image.FLIP_LEFT_RIGHT), mid - ox - im.width, oy)
            for p, (im, ox, oy) in frames.items()}


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    posed = _posed()
    if posed:
        print(f"  多姿态方向 {', '.join(sorted(posed))} 保持原样，不参与本次裁切")
    side_src = _frames(SRC_SIDE) if SRC_SIDE.is_dir() else {}
    if "base" not in side_src:
        print(f"没有素材：把图生图结果放进 {SRC_SIDE}")
        return

    side = _scale(side_src, h=FROG_H)
    side = {p: v for p, v in side.items() if p in PHASES}
    b = side["base"][0]
    # 上下两条边不约束横向位置，得给个身体锚点
    anchor_side = (CANVAS - b.width) // 2 + _anchor_x(b)
    print(f"侧姿态 = {b.width}x{b.height}  相位 {'/'.join(sorted(side))}")
    print(f"  对齐 {_offs(side)}")

    # 顶边单独一张姿态（探身下望）。没有就用对半切的老办法兜底，
    # 那种切法只剩腹部和脚，能用但看不出是蛙
    peek: dict[str, tuple[Image.Image, int, int]] = {}
    peek_src = _frames(SRC_PEEK) if SRC_PEEK.is_dir() else {}
    if "base" in peek_src:
        peek = _scale(peek_src, w=PEEK_W)
        peek = {p: v for p, v in peek.items() if p in PHASES}
        # 背画得太长，从顶上裁掉：那段在屏幕外，裁了没人看得见。
        # 按基准帧定行数、所有相位裁一样多，偏移量因此不受影响
        rows = max(0, peek["base"][0].height - PEEK_MAX_H)
        peek = {p: (_trim_top(im, rows), ox, oy) for p, (im, ox, oy) in peek.items()}
        pb = peek["base"][0]
        anchor_peek = (CANVAS - pb.width) // 2 + _anchor_x(pb)
        print(f"顶边姿态 = {pb.width}x{pb.height}  相位 {'/'.join(sorted(peek))}")
        print(f"  对齐 {_offs(peek)}")
    else:
        print(f"顶边姿态 = 缺省，edge-n 退回对半切（放一张进 {SRC_PEEK} 可换掉）")

    for d in DIRS:
        if d in posed:
            continue
        cfg = SPEC[d]
        use_peek = d == "n" and bool(peek)
        # 先翻到目标朝向再算几何：镜像会换掉「哪半边是被切掉的那侧」，
        # 拿未翻转的基准帧去算 x，再贴翻转后的图，会整套推到画布外
        pool = oriented(peek if use_peek else side, cfg["flip"])
        ref = pool["base"][0]
        if use_peek:
            # 顶边那张的构图已经是「头朝下压在边缘上」，不套裁切比例：
            # 可见像素顶到画布上边，切口自然就落在那里
            gx = round(anchor_peek - _anchor_x(ref))
            gy = 0
        else:
            # 裁切量按基准帧算一次，所有相位共用
            cut_px = (round(cfg["ratio"] * ref.width) if cfg["cut"] in ("left", "right")
                      else round(cfg["ratio"] * ref.height))
            # 横向锚点只有上下两条边用得上；四个斜向都是左右切口，用不到
            gx, gy = _geom(ref, cfg["cut"], cut_px, cfg["valign"], anchor_side)

        for phase in PHASES:
            im, ox, oy = pool.get(phase) or pool["base"]
            place(im, gx + ox, gy + oy).save(OUT / f"edge-{d}-{phase}.png", "PNG")

    # 校验：切面必须逐帧落在同一条画布边上，否则播起来切口会抖；
    # 同时也不能有帧被画布的左右边界切到——那是屏幕中间，切了就是莫名其妙的直边
    print()
    bad = 0
    geo: dict[str, dict] = {}
    for d in DIRS:
        if d in posed:
            # 多姿态方向：只核对四套图都在、切口都贴边，几何照抄 meta
            g0 = posed[d]
            cut = g0.get("cut", "bottom")
            miss = [p["id"] for p in g0.get("poses") or []
                    if not all((OUT / f'edge-{d}-{p["id"]}-{ph}.png').exists()
                               for ph in ("base", "blink"))]
            line = [f'{len(g0.get("poses") or [])} 套', "缺=" + ",".join(miss) if miss else "齐"]
            for p in g0.get("poses") or []:
                a = np.asarray(Image.open(OUT / f'edge-{d}-{p["id"]}-base.png'))[:, :, 3]
                x0, y0, x1, y1 = _alpha_box(Image.fromarray(a))
                ok = {"left": x0 == 0, "right": x1 == CANVAS,
                      "top": y0 == 0, "bottom": y1 == CANVAS}[cut]
                if not ok:
                    bad += 1
                    line.append(f'!{p["id"]}切口没贴边')
            print(f"  edge-{d}: " + "  ".join(line))
            geo[d] = g0
            continue
        cfg = SPEC[d]
        cut = "top" if (d == "n" and peek) else cfg["cut"]
        line = []
        boxes: dict[str, list[int]] = {}
        for phase in PHASES:
            p = OUT / f"edge-{d}-{phase}.png"
            if not p.exists():
                line.append(f"{phase}=缺")
                bad += 1
                continue
            a = np.asarray(Image.open(p))[:, :, 3]
            if a.max() <= 8:
                line.append(f"{phase}=空")
                bad += 1
                continue
            box = list(_alpha_box(Image.open(p)))
            boxes[phase] = box
            x0, y0, x1, y1 = box
            line.append(f"{phase}=({x0},{y0},{x1},{y1})")
            hit = {"left": x0 == 0, "right": x1 == CANVAS,
                   "top": y0 == 0, "bottom": y1 == CANVAS}[cut]
            if not hit:
                bad += 1
                line.append(f"!{phase}切口没贴边")
            if cut in ("top", "bottom") and (x0 <= 0 or x1 >= CANVAS):
                bad += 1
                line.append(f"!{phase}被画布侧边切了")
        print(f"  edge-{d}: " + "  ".join(line))
        if "base" in boxes:
            # 可见盒全相位取并集：抬手帧多出来的手也要能接住鼠标
            uni = [min(b[i] for b in boxes.values()) for i in (0, 1)]
            uni += [max(b[i] for b in boxes.values()) for i in (2, 3)]
            geo[d] = {"cut": cut, "base": boxes["base"], "box": uni}
    print(f"\n  切口/侧边不合格 {bad} 处")

    # 运行时靠这份元数据算吸附位置、命中区和嘴位：素材重出后自动对齐，
    # 不用把包围盒或嘴坐标抄进代码（抄错了只有肉眼看得出，而且只在
    # 边角上错几像素）。写盘交给 write_meta，--mouth 也走同一份格式。
    if len(geo) == len(DIRS):
        for d in DIRS:
            if d in posed:
                continue      # 多姿态方向不画口型，量了也用不上
            m = find_mouth(OUT / f"edge-{d}-base.png")
            if m:
                geo[d]["mouth"] = m
            else:
                print(f"  ！{d} 找不到嘴线，该向不会张嘴")
        write_meta(geo)


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
        img = Image.open(base_png(name))
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
        fig = Image.open(base_png(name))
        img.alpha_composite(fig, (x, y))
        # 标签放在蛙内侧，避免压到画面主体
        lx = x + c + 8 if x < W // 2 else x - 60
        ly = y + c // 2 if 0 < y < sy1 - c else y + c + 6
        d.text((lx, ly), name, fill=(96, 104, 116))

    out = HERE / "screenshots" / "pet-edges-context.png"
    img.convert("RGB").save(out)
    print(f"场景预览 = {out}  ({img.width}x{img.height})")


def anim() -> None:
    """动画预览：一张胶片条 + 一个 GIF。

    胶片条看的是**切面有没有逐帧位移**（每格右下角压着窗口示意边），
    GIF 看的是动起来是不是那个意思。两张缺一不可，光看静帧判断不了。
    """
    from PIL import ImageDraw

    cell, gap, pad, gut, top = 118, 6, 18, 34, 20
    cols, rows = len(PHASES), len(DIRS)
    w = pad * 2 + gut + cols * (cell + gap) - gap
    h = pad * 2 + top + rows * (cell + gap) - gap
    bg = Image.new("RGBA", (w, h), (24, 27, 32, 255))
    d = ImageDraw.Draw(bg)

    for ci, phase in enumerate(PHASES):
        d.text((pad + gut + ci * (cell + gap) + 2, pad + 4), phase, fill=(140, 150, 165))
    for ri, name in enumerate(DIRS):
        y = pad + top + ri * (cell + gap)
        d.text((pad, y + cell // 2 - 4), name, fill=(140, 150, 165))
        for ci, phase in enumerate(PHASES):
            p = png_for(name, phase)
            if not p.exists():
                continue
            x = pad + gut + ci * (cell + gap)
            bg.alpha_composite(Image.open(p).resize((cell, cell), Image.LANCZOS), (x, y))
            d.rectangle([x, y, x + cell - 1, y + cell - 1], outline=(50, 56, 66, 255))

    out = HERE / "screenshots" / "pet-edges-frames.png"
    bg.convert("RGB").save(out)
    print(f"胶片条 = {out}  ({bg.width}x{bg.height})")

    # GIF 只放三个方向：左边贴墙、底边探头、顶边探身，覆盖三种典型姿态
    picks = ("w", "s", "n")
    frames = []
    for phase in PHASES:
        fr = Image.new("RGBA", (len(picks) * CANVAS, CANVAS), (16, 18, 22, 255))
        fd = ImageDraw.Draw(fr)
        for i, name in enumerate(picks):
            x0 = i * CANVAS
            fd.rectangle([x0 + 4, 4, x0 + CANVAS - 5, CANVAS - 5],
                         fill=(24, 27, 32, 255), outline=(52, 58, 68, 255))
            fd.text((x0 + 12, 12), name, fill=(130, 140, 155))
            p = png_for(name, phase)
            if p.exists():
                fr.alpha_composite(Image.open(p), (x0, 0))
        fd.text((12, CANVAS - 22), phase, fill=(130, 140, 155))
        frames.append(fr.convert("RGB"))
    gif = HERE / "screenshots" / "pet-edges-anim.gif"
    # 不做呼吸时 base 停久一点，看着就是「静止的蛙偶尔眨一下眼」；
    # hi 停久一点，是「鼠标压在上面时才抬手」
    dwell = {"base": 420 if BREATH else 1800, "in": 420, "out": 420,
             "blink": 160, "hi": 900}
    frames[0].save(gif, save_all=True, append_images=frames[1:],
                   duration=[dwell.get(p, 300) for p in PHASES],
                   loop=0, optimize=False)
    print(f"GIF = {gif}  ({gif.stat().st_size // 1024} KB，{len(frames)} 帧)")


def find_mouth(path: Path) -> list[float] | None:
    """从一张贴边成品图里量嘴线：返回 [cx, cy, 倾角]。

    嘴线是暖棕色细弧（r>g>b 且色差够大）；眼睛是墨绿带白高光、肚皮是
    浅色、身体侧面的阴影是竖长条，都进不了「细长小簇」这道过滤。
    angle 是图像坐标（x 右、y 下）下的主轴角，QPainter.rotate 顺时针
    为正，可以直接拿去旋转口型，不用换算。
    """
    import cv2

    a = np.asarray(Image.open(path).convert("RGBA")).astype(np.int16)
    r, g, b, al = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    m = ((al > 180) & (r < 205) & (r > g) & (g > b) & ((r - b) > 20))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(
        m.astype(np.uint8), connectivity=8)
    best = None
    for i in range(1, n):
        _, _, w, h, area = (int(stats[i, k]) for k in range(5))
        if not (10 <= w <= 26 and 2 <= h <= 10 and w / h >= 1.5 and area >= 12):
            continue
        if best is not None and area <= best[0]:
            continue
        ys, xs = np.where(lab == i)
        pts = np.column_stack([xs, ys]).astype(np.float64)
        pts -= pts.mean(axis=0)
        cov = pts.T @ pts / max(1, len(pts))
        vals, vecs = np.linalg.eigh(cov)
        v = vecs[:, int(np.argmax(vals))]
        ang = float(np.degrees(np.arctan2(v[1], v[0])))
        while ang >= 90:
            ang -= 180
        while ang < -90:
            ang += 180
        best = (area, round(float(cent[i][0]), 1),
                round(float(cent[i][1]), 1), round(ang, 1))
    return [best[1], best[2], best[3]] if best else None


def write_meta(geo: dict) -> None:
    """写 meta.json。一个方向一行，别用 json.dumps(indent=) 把数组展开。

    多姿态方向（底边 s）写成一整行 `poses` 数组：每套带自己的 base / box，
    cut 提到方向级——四套都是 bottom，逐套重复没有意义。
    """
    out = ["{", f' "canvas": {CANVAS},',
           f' "phases": {json.dumps(list(PHASES))},', ' "dirs": {']
    for i, d in enumerate(DIRS):
        g = geo[d]
        tail = "," if i < len(DIRS) - 1 else ""
        if g.get("poses"):
            # 多姿态展开成多行：四套盒子横着排一行人眼读不了，
            # 对不上号时改错一行就该看得见
            ps = ",\n".join(
                '   {"id": "%s", "base": %s, "box": %s}'
                % (p["id"], p["base"], p["box"]) for p in g["poses"])
            out.append(f'  "{d}": {{"cut": "{g["cut"]}", "poses": [\n{ps}\n  ]}}{tail}')
            continue
        mouth = f', "mouth": {g["mouth"]}' if g.get("mouth") else ""
        out.append(f'  "{d}": {{"cut": "{g["cut"]}", '
                   f'"base": {g["base"]}, "box": {g["box"]}{mouth}}}{tail}')
    out += [" }", "}", ""]
    (OUT / "meta.json").write_text("\n".join(out), encoding="utf-8")
    print(f"  元数据 = {OUT / 'meta.json'}（{len(geo)} 向）")


def mouth_only() -> None:
    """只补单姿态方向的嘴位，不重出成品图。

    --build 要重跑抠图和相位对齐，源姿态是图生图产物，换个 cv2/PIL
    版本就可能有像素级差异。只想给已有素材补嘴位时用这个，图一个字节
    都不动。多姿态方向（底边四套躺姿）没有嘴位，跳过。
    """
    meta = json.loads((OUT / "meta.json").read_text(encoding="utf-8"))
    geo = meta.get("dirs") or {}
    posed = _posed()
    for d in DIRS:
        if d in posed:
            print(f"  {d:<3} 多姿态，无口型（跳过）")
            continue
        m = find_mouth(OUT / f"edge-{d}-base.png")
        if m is None:
            print(f"  ！{d} 找不到嘴线")
            continue
        geo[d]["mouth"] = m
        print(f"  {d:<3} {m}")
    write_meta(geo)


if __name__ == "__main__":
    if "--prep" in sys.argv:
        prep()
    elif "--build" in sys.argv:
        build()
    elif "--mouth" in sys.argv:
        mouth_only()
    elif "--sheet" in sys.argv:
        sheet()
    elif "--context" in sys.argv:
        context()
    elif "--anim" in sys.argv:
        anim()
    else:
        print(__doc__)
