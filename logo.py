"""程序图标。

托盘、窗口、exe 资源共用这一套画法——之前托盘和 exe 是两份独立实现，
一个只有「行」字、一个多了两条线，看起来像两个软件。

不用文字：16px 下汉字笔画会糊成一团，线条图形反而认得出来。
"""
from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen

BG = "#1a2129"      # 底色，比正文背景略亮一档
EDGE = "#43606f"    # 描边
SPINE = "#5f8fa8"   # 书脊
LINE = "#9dbccd"    # 前两行
LINE_DIM = "#6f8fa2"  # 最后一行，压暗表示段落未满


def draw_logo(p: QPainter, size: float) -> None:
    """在 (0, 0, size, size) 内画图标。调用方负责 antialiasing 与 p.end()。"""
    inset = max(0.5, size * 0.05)
    radius = size * 0.22
    p.setBrush(QColor(BG))
    p.setPen(QPen(QColor(EDGE), max(1.0, size * 0.022)))
    p.drawRoundedRect(
        QRectF(inset, inset, size - inset * 2, size - inset * 2),
        radius, radius)

    p.setPen(Qt.PenStyle.NoPen)

    # 书脊：左侧细竖条
    spine_w = max(1.5, size * 0.058)
    p.setBrush(QColor(SPINE))
    p.drawRoundedRect(
        QRectF(size * 0.255, size * 0.245, spine_w, size * 0.51),
        spine_w * 0.5, spine_w * 0.5)

    # 小尺寸下三条线会粘成一块，减到两条并加粗
    if size < 24:
        rows = ((0.335, 0.36), (0.555, 0.28))
        line_h = max(1.4, size * 0.10)
    else:
        rows = ((0.275, 0.375), (0.430, 0.315), (0.585, 0.215))
        line_h = max(1.2, size * 0.06)

    lx = size * 0.375
    for i, (y, w) in enumerate(rows):
        p.setBrush(QColor(LINE if i < len(rows) - 1 else LINE_DIM))
        p.drawRoundedRect(QRectF(lx, size * y, size * w, line_h),
                          line_h * 0.5, line_h * 0.5)
