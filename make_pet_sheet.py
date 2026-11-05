"""构建期工具：把奶蛙精灵表切成可用的 PNG + 清单，并把音效归档。

和 make_icon.py 同一定位：只在开发机跑，不参与运行时。

用法：
    python make_pet_sheet.py --dump          # 只导出 11 行条带，用来肉眼校对行序
    python make_pet_sheet.py                 # 产出 assets/naiwa/sheet.png + manifest.json + sfx/

精灵表来源：https://github.com/timerring/codex-pet-naiwa （MIT）
1536x2288，每帧 192x208，8 列 x 11 行；9 行动画 + 2 行 16 方向注视。
仓库没有公开行序文档，所以 ROW_MAP 是先猜后校对的产物 —— 改错了只改这张表。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "build" / "naiwa_src"
OUT = HERE / "assets" / "naiwa"

SHEET_URL = ("https://raw.githubusercontent.com/timerring/codex-pet-naiwa/"
             "main/naifrog/spritesheet.webp")

FRAME_W, FRAME_H = 192, 208
COLS, ROWS = 8, 11

# 插帧后的列数：相邻两帧之间插一帧，帧数最多翻倍（8 -> 16）
HI_COLS = COLS * 2
# 质心位移小于这个值就只做淡化，不做平移补偿 —— 原地呼吸、轻微晃头
# 的动画硬平移反而会抖
MIN_SHIFT = 2.5

# 动画 -> (行号, 源帧数, 播放 fps, 是否循环, 是否插帧)
# 行号 0 基，已用像素差逐行校对过（见 --dump 与 alpha 覆盖率统计）：
#   row0 idle（第 7 帧是首帧副本，去掉，用 6 帧）
#   row1/row2 互为镜像，按眼睛朝向定为 right / left
#   row3 waving(4) row4 jumping(5) row5 failed(8)
#   row6 running(6) row7 review(6) row8 waiting(6)
#   row9/10 是 16 方向注视，不属于动画循环，不收
#
# 关于 fps：素材只有 4-8 帧，8fps 时单帧停留 125ms，看着一顿一顿。
# 试过在两帧之间合成中间帧，只有 idle 干净（相邻帧位移 <1px）；
# 跑动/摇头这类「整体不动、肢体在动」的动作，全局位移补偿和块匹配
# 都会留下双影（实测见 tts-samples/_interp_all.png），所以其余动作
# 不插帧，改为把播放帧率提上来缩短单帧停留，动作也跟着利落一点。
ROW_MAP = {
    "idle":          (0, 6, 8,  True,  True),
    "running-right": (1, 8, 16, True,  False),
    "running-left":  (2, 8, 16, True,  False),
    "waving":        (3, 4, 12, False, False),
    "jumping":       (4, 5, 14, False, False),
    "failed":        (5, 8, 9,  False, False),
    "running":       (6, 6, 15, True,  False),
    "review":        (7, 6, 10, False, False),
    "waiting":       (8, 6, 10, True,  False),
}

# 音效：源文件名 -> 入库名。必须 ASCII 无空格（PyInstaller datas 与命令行都怕）
SFX_MAP = {
    "voice/laugh.mp3": "laugh.mp3",
    "voice/ganbadie.mp3": "voice_01.mp3",
    "voice/gagadilaxia.mp3": "voice_02.mp3",
    "voice/gajiaosei.mp3": "voice_03.mp3",
    "voice/andi (1).mp3": "voice_04.mp3",
}


def fetch_sheet(src: str | None) -> Path:
    """拿到原始精灵表。优先用本地文件，否则下载并缓存。"""
    if src:
        p = Path(src).expanduser()
        if not p.exists():
            sys.exit(f"找不到精灵表：{p}")
        return p

    CACHE.mkdir(parents=True, exist_ok=True)
    dst = CACHE / "spritesheet.webp"
    if dst.exists() and dst.stat().st_size > 1024:
        return dst

    print(f"下载 {SHEET_URL}")
    req = urllib.request.Request(SHEET_URL, headers={"User-Agent": "interline-build"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    dst.write_bytes(data)
    print(f"  -> {dst}  {len(data) / 1024:.0f} KB")
    return dst


def load_sheet(path: Path):
    # QImage 读 webp 需要 gui app 起来才能加载 imageformats 插件
    from PyQt6.QtGui import QGuiApplication, QImage

    app = QGuiApplication.instance() or QGuiApplication([])
    img = QImage(str(path))
    if img.isNull():
        sys.exit("精灵表解码失败：Qt 可能缺 webp 插件。\n"
                 "降级方案：改成下载 naifrog/previews/<anim>.png 九条独立条带。")
    if (img.width(), img.height()) != (FRAME_W * COLS, FRAME_H * ROWS):
        print(f"警告：尺寸 {img.width()}x{img.height()} 与推定 "
              f"{FRAME_W * COLS}x{FRAME_H * ROWS} 不符，按实际尺寸切")
    return app, img


def dump_rows(img) -> None:
    """导出每行整条，供肉眼校对。缩放 0.5 只为方便看。"""
    from PyQt6.QtCore import Qt

    d = CACHE / "rows"
    d.mkdir(parents=True, exist_ok=True)
    for r in range(ROWS):
        strip = img.copy(0, r * FRAME_H, FRAME_W * COLS, FRAME_H)
        strip = strip.scaled(FRAME_W * COLS // 2, FRAME_H // 2,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        p = d / f"row_{r:02d}.png"
        strip.save(str(p), "PNG")
        print(f"  row {r:2d} -> {p.name}")
    print(f"\n条带目录：{d}\n看完把 ROW_MAP 改对，再不带 --dump 跑一遍。")


def mp3_duration_ms(path: Path) -> int:
    """读 MP3 帧头估算时长（这套素材都是 CBR 128kbps）。

    构建期算好写进 manifest，运行时就不用等 QMediaPlayer 异步报 duration，
    动画时长可以直接由音频反推。
    """
    d = path.read_bytes()
    i = 0
    if d[:3] == b"ID3":
        sz = (((d[6] & 0x7F) << 21) | ((d[7] & 0x7F) << 14)
              | ((d[8] & 0x7F) << 7) | (d[9] & 0x7F))
        i = 10 + sz
    while i < len(d) - 4 and not (d[i] == 0xFF and (d[i + 1] & 0xE0) == 0xE0):
        i += 1
    if i >= len(d) - 4:
        return 0
    br_tab = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
    br = br_tab[(d[i + 2] >> 4) & 0xF]
    if not br:
        return 0
    return int((len(d) - i) * 8 / (br * 1000) * 1000)


def copy_sfx(src_dir: str | None) -> dict[str, int]:
    if not src_dir:
        print("未给 --sfx，跳过音效")
        return {}
    base = Path(src_dir).expanduser()
    if not base.exists():
        sys.exit(f"找不到音效目录：{base}")

    dst = OUT / "sfx"
    dst.mkdir(parents=True, exist_ok=True)
    total = 0
    durations: dict[str, int] = {}
    for name, new in SFX_MAP.items():
        s = base / name
        if not s.exists():
            print(f"  缺少 {name}，跳过")
            continue
        shutil.copyfile(s, dst / new)
        kb = (dst / new).stat().st_size / 1024
        total += kb
        ms = mp3_duration_ms(dst / new)
        durations[new] = ms
        print(f"  {name:26s} -> sfx/{new}  {kb:6.1f} KB  {ms} ms")
    print(f"音效合计 {total:.0f} KB")
    return durations


def neutralise_shadow(img, row: int, y_from: int = 185, dark_alpha: float = 0.42) -> int:
    """把某一行的白色地面阴影改成低透明度黑色软阴影。

    jumping 那一行带一圈白色投影（原素材按浅色底做的），放在深色桌面上
    就是一块发白的光晕。只处理 y>=185 的中性色像素 —— 眼睛高光和棕色脚掌
    都不在这个范围里，碰不到。返回改了多个像素。
    """
    from PyQt6.QtGui import QColor

    n = 0
    for c in range(COLS):
        for y in range(y_from, FRAME_H):
            for x in range(FRAME_W):
                col = img.pixelColor(c * FRAME_W + x, row * FRAME_H + y)
                if col.alpha() < 8:
                    continue
                r, g, b = col.red(), col.green(), col.blue()
                if min(r, g, b) > 165 and max(r, g, b) - min(r, g, b) < 32:
                    col = QColor(18, 16, 14, int(col.alpha() * dark_alpha))
                    img.setPixelColor(c * FRAME_W + x, row * FRAME_H + y, col)
                    n += 1
    return n


def write_assets(img, sfx: dict[str, int], interp: bool = True) -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # 没给 --sfx 时沿用上一版 manifest 的时长：mp3 没变，时长就不该丢，
    # 丢了 hold 会退化成兜底的 1500ms
    if not sfx:
        old = OUT / "manifest.json"
        if old.exists():
            try:
                sfx = json.loads(old.read_text(encoding="utf-8")).get("sfx") or {}
                print(f"沿用已有 manifest 的 {len(sfx)} 条音效时长")
            except Exception:
                sfx = {}

    fixed = neutralise_shadow(img, ROW_MAP["jumping"][0])
    print(f"中和 jumping 白色地面阴影 {fixed} 像素")

    if interp:
        img, anims, cols = interpolate_sheet(img)
    else:
        anims = {k: {"row": v[0], "frames": v[1], "fps": v[2], "loop": v[3],
                     "src_frames": v[1]} for k, v in ROW_MAP.items()}
        cols = COLS
    # 整表转 PNG：运行时只读 PNG，不依赖 Qt 的 webp 插件
    sheet = OUT / "sheet.png"
    if not img.save(str(sheet), "PNG"):
        sys.exit("写出 sheet.png 失败")
    size_kb = sheet.stat().st_size / 1024
    print(f"sheet.png          {size_kb:7.0f} KB  {img.width()}x{img.height()}")

    for k, v in anims.items():
        print(f"  {k:<14} {v['src_frames']:>2} -> {v['frames']:>2} 帧  "
              f"{v['fps']:>2} fps  {'loop' if v['loop'] else 'once'}")

    manifest = {
        "frame_w": FRAME_W,
        "frame_h": FRAME_H,
        "cols": cols,
        # 哪些动作真的插了帧，看每个动作的 src_frames 与 frames 是否相等
        "interp": bool(interp),
        "anims": anims,
        # 音效时长：动画 hold 由它反推，避免两处各写一个数字
        "sfx": sfx,
    }
    mp = OUT / "manifest.json"
    mp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"manifest.json      {mp.stat().st_size:7.0f} B   {len(anims)} 个动画 / {len(sfx)} 条音效")


# ---------------------------------------------------------------- 插帧
#
# 原素材每个动作只有 4-8 帧（idle 8fps），动起来是一顿一顿的。
# 上线拿不到更高帧率的源（仓库只有这一张表），所以构建期在两帧之间
# 合成一张中间帧：帧数翻倍、fps 同步翻倍，总时长不变。
#
# 单纯交叉淡化会让快速动作出现"两个身子"，所以先按质心位移把两帧
# 对齐到同一时刻再混合；位移小的动画不做补偿，避免硬平移产生的抖动。

def _to_np(img):
    import numpy as np
    w, h = img.width(), img.height()
    buf = img.constBits()
    buf.setsize(img.sizeInBytes())
    a = np.frombuffer(buf, np.uint8).reshape(h, img.bytesPerLine() // 4, 4)
    return a[:, :w].astype(np.float32)


def _to_qimg(a):
    import numpy as np
    from PyQt6.QtGui import QImage
    a8 = np.clip(a, 0, 255).astype(np.uint8)
    h, w, _ = a8.shape
    return QImage(a8.tobytes(), w, h, w * 4,
                  QImage.Format.Format_RGBA8888).copy()


def _premul(a):
    import numpy as np
    al = a[..., 3:4] / 255.0
    return np.concatenate([a[..., :3] * al, a[..., 3:4]], axis=-1)


def _unpremul(p):
    import numpy as np
    al = np.clip(p[..., 3:4], 0, 255) / 255.0
    rgb = np.where(al > 1e-4, p[..., :3] / np.maximum(al, 1e-4), 0.0)
    return np.concatenate([np.clip(rgb, 0, 255), p[..., 3:4]], axis=-1)


def _centroid(a):
    import numpy as np
    al = a[..., 3]
    m = al.sum()
    if m < 1:
        return None
    h, w = al.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    return float((xs * al).sum() / m), float((ys * al).sum() / m)


def _shift(p, dx: float, dy: float):
    """双线性平移，出界补透明。作用在 premultiplied 数据上，边缘不渗色。"""
    import numpy as np
    h, w = p.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    sx, sy = xs - dx, ys - dy
    x0 = np.floor(sx).astype(np.int32)
    y0 = np.floor(sy).astype(np.int32)
    fx = (sx - x0)[..., None]
    fy = (sy - y0)[..., None]

    def samp(yy, xx):
        ok = ((yy >= 0) & (yy < h) & (xx >= 0) & (xx < w))[..., None]
        return p[np.clip(yy, 0, h - 1), np.clip(xx, 0, w - 1)] * ok

    return (samp(y0, x0) * (1 - fx) * (1 - fy)
            + samp(y0, x0 + 1) * fx * (1 - fy)
            + samp(y0 + 1, x0) * (1 - fx) * fy
            + samp(y0 + 1, x0 + 1) * fx * fy)


def blend_mid(a, b, t: float = 0.5, min_shift: float = MIN_SHIFT):
    """合成 a、b 之间的一帧。位移够大就先对齐再混合。"""
    ca, cb = _centroid(a), _centroid(b)
    if ca and cb:
        dx, dy = cb[0] - ca[0], cb[1] - ca[1]
        if (dx * dx + dy * dy) ** 0.5 >= min_shift:
            pa = _shift(_premul(a), dx * t, dy * t)
            pb = _shift(_premul(b), -dx * (1 - t), -dy * (1 - t))
            return _unpremul(pa * (1 - t) + pb * t)
    return _unpremul(_premul(a) * (1 - t) + _premul(b) * t)


def interp_anim(frames: list, loop: bool) -> list:
    """每两帧中间插一帧。循环动画还要补「末帧 -> 首帧」的接缝，否则循环会顿一下。"""
    n = len(frames)
    if n < 2:
        return list(frames)
    out: list = []
    pairs = ([(i, (i + 1) % n) for i in range(n)] if loop
             else [(i, i + 1) for i in range(n - 1)])
    for i, j in pairs:
        out.append(frames[i])
        out.append(blend_mid(frames[i], frames[j]))
    if not loop:
        out.append(frames[-1])
    return out


def interpolate_sheet(img):
    """按 ROW_MAP 重排精灵表，需要插帧的动作帧数翻倍。

    列宽取所有动作里的最大帧数：只有 idle 插帧时是 12 列，全插时才是 16。
    """
    import numpy as np
    from PyQt6.QtGui import QImage

    src = img.convertToFormat(QImage.Format.Format_RGBA8888)
    rows = max(v[0] for v in ROW_MAP.values()) + 1

    frames_by_anim: dict[str, list] = {}
    anims: dict[str, dict] = {}
    for name, (row, n, fps, loop, do_interp) in ROW_MAP.items():
        frames = []
        for c in range(n):
            cell = src.copy(c * FRAME_W, row * FRAME_H, FRAME_W, FRAME_H)
            frames.append(_to_np(cell))
        hi = interp_anim(frames, loop) if do_interp else frames
        frames_by_anim[name] = hi

        dur = n / fps                       # 播放总时长，秒
        anims[name] = {
            "row": row,
            "frames": len(hi),
            "fps": max(1, round(len(hi) / dur)),
            "loop": loop,
            "src_frames": n,
        }

    cols = max(len(v) for v in frames_by_anim.values())
    if cols > HI_COLS:
        sys.exit(f"最多 {cols} 帧，超过 HI_COLS={HI_COLS}，调大常量")

    canvas = np.zeros((rows * FRAME_H, cols * FRAME_W, 4), np.uint8)
    for name, frames in frames_by_anim.items():
        row = anims[name]["row"]
        for c, fr in enumerate(frames):
            y, x = row * FRAME_H, c * FRAME_W
            canvas[y:y + FRAME_H, x:x + FRAME_W] = np.clip(fr, 0, 255).astype(np.uint8)

    h, w, _ = canvas.shape
    out = QImage(canvas.tobytes(), w, h, w * 4,
                 QImage.Format.Format_RGBA8888).copy()
    return out, anims, cols


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="只导出条带供校对")
    ap.add_argument("--src", help="本地 spritesheet.webp 路径（默认下载）")
    ap.add_argument("--sfx", help="音效源目录，例如 C:/Users/User/Desktop/naiwa-pet/naiwa")
    ap.add_argument("--no-interp", action="store_true",
                    help="不插帧，输出原始 8 列（出问题时用来回退）")
    args = ap.parse_args()

    path = fetch_sheet(args.src)
    app, img = load_sheet(path)
    print(f"精灵表 {img.width()}x{img.height()}  每帧 {FRAME_W}x{FRAME_H}\n")

    if args.dump:
        dump_rows(img)
        return 0

    write_assets(img, copy_sfx(args.sfx), interp=not args.no_interp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
