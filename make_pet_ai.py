"""构建期工具：用图生图模型给奶蛙精灵表补中间帧。

和 make_pet_sheet.py 同一定位：只在开发机跑，不参与运行时。

为什么要它：
    原素材每个动作只有 4-8 帧，8fps 时单帧停 125ms，看着一顿一顿。
    试过的传统插值（质心补偿交叉淡化 / 方向模糊 / 块匹配 / 金字塔 LK /
    OpenCV DIS 光流）在所有「整体不动、肢体大摆」的动作上都留双影 ——
    质心只移 2-5px，腿却转过 40°，线性混合必然叠出两条腿。
    图生图不是「求中间值」而是「重画一帧」，反而把腿的姿态画对了
    （实测下半身质心/面积/落地脚位置都落在两侧源帧之间）。

代价与坑（都是实测出来的，别踩第二遍）：
    1. 模型会把不动的地方也重画一遍：头部整段宽 +5%、躯干 +12%。
       用逐行宽度归一化（rib warp）压回两帧均值，误差 4.5px -> 0.9px。
    2. rib warp 必须先沿 y 平滑缩放比，否则手臂凸出那几行会让缩放比跳变，
       把肚子上的浅色块撕成台阶。
    3. 腿部区域不能做 rib warp：「两脚分开」和「两脚并拢」的跨度均值
       没有物理意义，会把脚抹平。所以权重在 55%H 之后线性退出。
    4. 对齐必须先裁到角色包围盒再缩放，否则整张 1024 画布会被压进
       192x208 的格子（这个 bug 让第一轮指标全错，误判成「模型不行」）。

用法：
    python make_pet_ai.py --prep thinking running     # 生成准备图到 build/naiwa_ai/in/
    # 然后用图生图工具逐对生成到 build/naiwa_ai/raw/<anim>/<i>_<j>.png
    python make_pet_ai.py --build                     # 抠底/对齐/宽度归一化 -> out/
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "build" / "naiwa_src" / "spritesheet.webp"
AI = HERE / "build" / "naiwa_ai"
IN, RAW = AI / "in", AI / "raw"          # 中间产物，可重建，跟着 build/ 一起忽略
OUT = HERE / "assets" / "naiwa" / "mids"  # 成品中间帧，进仓库（见 make_pet_sheet.AI_DIR）

FRAME_W, FRAME_H = 192, 208
UPSCALE = 512 / FRAME_W          # 喂给模型的输入宽度

# 做 AI 插帧的动作 -> (行号, 源帧数, 是否循环)
# 不在这里的动作：
#   idle    相邻帧位移 <1px，传统插值本来就干净，没必要多花几十次生成
#   waving  模型把「抬手挥动」的中间态画成「手放在嘴边」，还带出喙状变形，
#           步长只降 18%（tts-samples/_wave_board.png），退回不插帧
#   jumping 源素材带白色地面投影，喂给模型后投影在白底上消失，
#           生成的中间帧没有投影，跟前后帧对不上
#   running-left 见 MIRROR：它是 running-right 的镜像，直接翻不用重新生成
AI_ANIMS = {
    "thinking":      (8, 2, False),
    "waiting":       (8, 6, True),
    "running":       (6, 6, True),
    "running-right": (1, 8, True),
    "review":        (7, 6, False),
    "failed":        (5, 8, False),
}

# 镜像派生：源行素材本身就是水平翻转的（实测 right 与 mirror(left) 差 4.5-16，
# 而直接对比差 30-53），所以中间帧翻一下就能用，居中度也基本一致（67% vs 68%）
MIRROR = {"running-left": "running-right"}

PROMPT = (
    "The two input images are two neighbouring frames of one continuous animation "
    "of the same character. Draw the SINGLE in-between frame that belongs exactly "
    "halfway between them. Character: a chubby yellow frog-like creature, soft matte "
    "3D render, rounded pear-shaped body, large cream-white belly patch, two large "
    "mint-green eyes with round black pupils, short stubby grey-green arms and feet. "
    "Hard requirements: exactly the same body proportions, the same head size, the "
    "same eye size and spacing, and the same belly patch size as the inputs; the "
    "character must be the same size and in the same position in the frame; only the "
    "limb pose and any small body bounce should be halfway between the two inputs. "
    "Plain pure white background, absolutely no shadow, no ground plane, no text, no "
    "extra objects, no duplicated limbs. Full body visible, feet fully inside the "
    "frame, generous white margin around the character."
)


# ---------------------------------------------------------------- 取源帧
# 只做镜像派生的动作也要能切源帧，所以单独列一份 (行号, 源帧数, 是否循环)
EXTRA_ANIMS = {"running-left": (2, 8, True)}
ALL_ANIMS = {**AI_ANIMS, **EXTRA_ANIMS}


def load_source_frames():
    """从缓存的原始 webp 切出所有动作的源帧（不是 assets/sheet.png，
    那张已经插过帧了，再插一次会把插出来的帧当源帧）"""
    from PIL import Image

    if not SRC.exists():
        sys.exit(f"找不到原始精灵表 {SRC}\n先跑一次 make_pet_sheet.py 让它下载")
    sheet = Image.open(SRC).convert("RGBA")
    out = {}
    for name, (row, n, loop) in ALL_ANIMS.items():
        out[name] = [sheet.crop((c * FRAME_W, row * FRAME_H,
                                 (c + 1) * FRAME_W, (row + 1) * FRAME_H))
                     for c in range(n)]
    return out


def pairs(n: int, loop: bool) -> list[tuple[int, int]]:
    return ([(i, (i + 1) % n) for i in range(n)] if loop
            else [(i, i + 1) for i in range(n - 1)])


# ---------------------------------------------------------------- 准备输入
def prep(names: list[str]) -> None:
    from PIL import Image

    fr = load_source_frames()
    w, h = int(round(FRAME_W * UPSCALE)), int(round(FRAME_H * UPSCALE))
    for name in names:
        if name not in AI_ANIMS:
            sys.exit(f"未知动作 {name}，可选：{', '.join(AI_ANIMS)}")
        row, n, loop = AI_ANIMS[name]
        d = IN / name
        d.mkdir(parents=True, exist_ok=True)
        # 清掉上一次的文件，避免帧数变过之后残留
        for f in d.glob("*.png"):
            f.unlink()
        frames = fr[name]
        for c, f in enumerate(frames):
            canvas = Image.new("RGB", (w, h), (255, 255, 255))
            g = f.resize((w, h), Image.LANCZOS)
            canvas.paste(g, (0, 0), g)
            canvas.save(d / f"src{c}.png")
        ps = pairs(n, loop)
        print(f"{name:14s} 行{row}  {n} 帧  需生成 {len(ps)} 张中间帧 -> {d}")
        for i, j in ps:
            print(f"    src{i}.png + src{j}.png  ->  raw/{name}/{i}_{j}.png")


def job_list(names: list[str]) -> None:
    """给批量调用用的清单（JSON），一次打印出来省得来回翻"""
    fr = load_source_frames()
    jobs = []
    for name in names:
        row, n, loop = AI_ANIMS[name]
        for i, j in pairs(n, loop):
            jobs.append({
                "anim": name,
                "prompt": PROMPT,
                "image_path": [str(IN / name / f"src{i}.png"),
                               str(IN / name / f"src{j}.png")],
                "size": "1024x1024",
                "filename": f"{i}_{j}",
                "output_dir": str(RAW / name),
            })
    print(json.dumps(jobs, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- 抠底 + 对齐
def _premul(a):
    import numpy as np
    al = a[..., 3:4] / 255.0
    return np.dstack([a[..., :3] * al, a[..., 3:4]])


def _unpremul(p):
    import numpy as np
    al = np.clip(p[..., 3:4], 0, 255) / 255.0
    rgb = np.divide(p[..., :3], al, out=np.zeros_like(p[..., :3]), where=al > 1e-4)
    return np.dstack([np.clip(rgb, 0, 255), p[..., 3:4]])


def _spans(mask):
    import numpy as np
    out = []
    for y in range(mask.shape[0]):
        xs = np.where(mask[y] > 0)[0]
        out.append((float(xs.min()), float(xs.max())) if len(xs) else None)
    return out


def _smooth(v, k=11):
    import numpy as np
    if k < 3:
        return v
    pad = k // 2
    return np.convolve(np.pad(v, pad, mode="edge"), np.ones(k) / k, mode="valid")


def cutout_align(path: Path, ref):
    """白底 -> 透明；按角色包围盒等比缩放到参考帧的高度，底边中心对齐"""
    import cv2
    import numpy as np
    from PIL import Image

    im = Image.open(path).convert("RGB")
    a = np.array(im).astype(np.int16)
    fg = ~((a[:, :, 0] > 244) & (a[:, :, 1] > 244) & (a[:, :, 2] > 244))
    n, lab, st, _ = cv2.connectedComponentsWithStats(fg.astype(np.uint8), 8)
    if n > 1:
        fg = lab == (1 + int(np.argmax(st[1:, 4])))
    ys, xs = np.where(fg)
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)

    # 软边：按到前景边界的距离给 alpha，硬阈值会有锯齿
    dist = cv2.distanceTransform(fg.astype(np.uint8), cv2.DIST_L2, 3)
    alpha = np.clip(dist / 1.6 * 255, 0, 255).astype(np.uint8)
    # 必须裁到包围盒再缩，否则整张 1024 会被压进 192x208
    rgba = np.dstack([np.array(im).astype(np.float64), alpha])[box[1]:box[3], box[0]:box[2]]

    rys, rxs = np.where(ref[:, :, 3] > 40)
    rbox = (int(rxs.min()), int(rys.min()), int(rxs.max()) + 1, int(rys.max()) + 1)
    rw, rh = rbox[2] - rbox[0], rbox[3] - rbox[1]
    bw, bh = box[2] - box[0], box[3] - box[1]
    scale = rh / max(bh, 1)
    nw, nh = max(1, round(bw * scale)), max(1, round(bh * scale))

    small = Image.fromarray(rgba.astype(np.uint8), "RGBA").resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (FRAME_W, FRAME_H), (0, 0, 0, 0))
    canvas.paste(small, (rbox[0] + rw // 2 - nw // 2, rbox[1] + rh - nh), small)
    return np.array(canvas)


def upper_body_weight(ref):
    """55%H 以内全强度校正，70%H 之后完全退出（腿部两帧位置差太远，均值无意义）"""
    import numpy as np
    ys = np.where((ref[:, :, 3] > 40).any(axis=1))[0]
    y0, y1 = int(ys.min()), int(ys.max())
    w = np.zeros(FRAME_H)
    for y in range(FRAME_H):
        f = (y - y0) / max(y1 - y0, 1)
        w[y] = 1.0 if f <= 0.55 else (0.0 if f >= 0.70 else (0.70 - f) / 0.15)
    return w, y0, y1


def rib_warp(src, target_spans, weight, min_scale=0.6, max_scale=1.7):
    """逐行把水平跨度搬到 target_spans。中心线与半宽都先沿 y 平滑，
    否则缩放比逐行跳变会把内部色块撕成台阶。"""
    import numpy as np

    h, w = src.shape[:2]
    p = _premul(src.astype(np.float64))
    sp = _spans(src[:, :, 3] > 40)
    dst = np.array(p, copy=True)

    cs, hs, ct, ht = (np.full(h, np.nan) for _ in range(4))
    for y in range(h):
        if sp[y] is None or target_spans[y] is None:
            continue
        cs[y] = (sp[y][0] + sp[y][1]) / 2
        hs[y] = max((sp[y][1] - sp[y][0]) / 2, 1e-3)
        ct[y] = (target_spans[y][0] + target_spans[y][1]) / 2
        ht[y] = max((target_spans[y][1] - target_spans[y][0]) / 2, 1e-3)

    def fill(a):
        idx, ok = np.arange(h), ~np.isnan(a)
        return np.interp(idx, idx[ok], a[ok]) if ok.sum() >= 2 else a

    cs, hs, ct, ht = (_smooth(fill(x)) for x in (cs, hs, ct, ht))
    for y in range(h):
        if sp[y] is None or target_spans[y] is None or weight[y] <= 0.01:
            continue
        wt = float(weight[y])
        c_y = cs[y] + (ct[y] - cs[y]) * wt
        h_y = hs[y] + (ht[y] - hs[y]) * wt
        sc = h_y / max(hs[y], 1e-3)
        if sc < min_scale or sc > max_scale:
            continue
        ox = np.arange(w)
        sx = cs[y] + (ox - c_y) / sc
        x0 = np.floor(sx).astype(int)
        fr = (sx - x0)[:, None]
        valid = ((sx >= 0) & (sx <= w - 1))[:, None]
        row = p[y, np.clip(x0, 0, w - 1)] * (1 - fr) + p[y, np.clip(x0 + 1, 0, w - 1)] * fr
        dst[y] = np.where(valid, row * wt + p[y] * (1 - wt), p[y])
    return _unpremul(dst)


# ---------------------------------------------------------------- 合成
def build(names: list[str]) -> None:
    import numpy as np
    from PIL import Image

    src_frames = load_source_frames()
    total = 0
    for name in names:
        if name not in AI_ANIMS:
            hint = "，它用 --mirror 派生" if name in MIRROR else ""
            sys.exit(f"{name} 不在 AI_ANIMS 里{hint}")
        row, n, loop = AI_ANIMS[name]
        frames = [np.array(f) for f in src_frames[name]]
        weight, y0, y1 = upper_body_weight(frames[0])
        d = OUT / name
        d.mkdir(parents=True, exist_ok=True)
        print(f"\n{name}  行{row}  {n} 帧 {'loop' if loop else 'once'}")
        print(f"  {'pair':9s} {'上半宽偏差':>12s}   {'居中度':>7s}")
        for i, j in pairs(n, loop):
            p = RAW / name / f"{i}_{j}.png"
            if not p.exists():
                print(f"  {i}->{j:<6d} 缺 {p}（跳过）")
                continue
            raw = cutout_align(p, frames[i])
            sa, sb = _spans(frames[i][:, :, 3] > 40), _spans(frames[j][:, :, 3] > 40)
            tgt = []
            for y in range(FRAME_H):
                if sa[y] is None or sb[y] is None:
                    tgt.append(sa[y] or sb[y])
                else:
                    tgt.append(((sa[y][0] + sb[y][0]) / 2, (sa[y][1] + sb[y][1]) / 2))
            out = rib_warp(raw, tgt, weight)
            Image.fromarray(out.astype(np.uint8), "RGBA").save(d / f"{i}_{j}.png")
            total += 1

            def err(im):
                m = im[:, :, 3] > 40
                e = []
                for f in (0.03, 0.08, 0.13, 0.18, 0.28, 0.40, 0.50, 0.62):
                    y = int(round(y0 + f * (y1 - y0)))
                    xs = np.where(m[y])[0]
                    w_ = xs.max() - xs.min() + 1 if len(xs) else 0
                    e.append(abs(w_ - (tgt[y][1] - tgt[y][0] + 1)))
                return float(np.mean(e))

            # 居中度：中间帧在两源帧之间的位置（50% 为理想）
            pa, pb, pm = (_premul(x.astype(float)) for x in (frames[i], frames[j], out))

            def dd(p, q):
                u = (p[:, :, 3] + q[:, :, 3]) > 38
                return float(np.abs(p - q).sum(axis=2)[u].mean())

            dab, dam = dd(pa, pb), dd(pa, pm)
            print(f"  {i}->{j:<6d} {err(raw):5.1f} -> {err(out):4.1f} px   "
                  f"{100 * dam / max(dab, 1e-6):5.0f}%")
    print(f"\n写出 {total} 张中间帧到 {OUT}")


def mirror() -> None:
    """把某个动作的中间帧水平翻转，得到它的镜像动作（running-right -> running-left）"""
    from PIL import Image, ImageOps

    for dst, src in MIRROR.items():
        row_d, n_d, loop_d = ALL_ANIMS[dst]
        row_s, n_s, loop_s = AI_ANIMS[src]
        if (n_d, loop_d) != (n_s, loop_s):
            sys.exit(f"{dst} 与 {src} 帧数/循环方式不一致，不能镜像")
        d = OUT / dst
        d.mkdir(parents=True, exist_ok=True)
        m = 0
        for i, j in pairs(n_s, loop_s):
            p = OUT / src / f"{i}_{j}.png"
            if not p.exists():
                sys.exit(f"缺 {p}，先跑 --build {src}")
            ImageOps.mirror(Image.open(p).convert("RGBA")).save(d / f"{i}_{j}.png")
            m += 1
        print(f"{dst}  <- mirror({src})  {m} 张 -> {d}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prep", nargs="*", metavar="ANIM", help="生成准备图")
    ap.add_argument("--jobs", nargs="*", metavar="ANIM", help="打印批量生成清单(JSON)")
    ap.add_argument("--build", nargs="*", metavar="ANIM", help="合成中间帧")
    ap.add_argument("--mirror", action="store_true", help="镜像派生 running-left")
    a = ap.parse_args()
    if a.prep:
        prep(a.prep)
    elif a.jobs:
        job_list(a.jobs)
    elif a.build:
        build(a.build)
    elif a.mirror:
        mirror()
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
