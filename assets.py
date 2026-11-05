"""运行时资源定位。

PyInstaller onefile 会把 datas 解到 sys._MEIPASS，源码运行时就在本文件旁边。
之前工程里没有任何图片资源（图标全靠 logo.py 画），所以这是第一条资源管线。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

NAIWA_DIR = "assets/naiwa"


def resource_path(rel: str) -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = os.path.dirname(os.path.abspath(__file__))
    return Path(base) / rel


def naiwa(rel: str) -> Path:
    """奶蛙资源：naiwa("sheet.png") / naiwa("sfx/laugh.mp3")"""
    return resource_path(f"{NAIWA_DIR}/{rel}")
