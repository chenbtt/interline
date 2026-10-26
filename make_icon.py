"""生成 icon.ico。不依赖 Pillow：用 Qt 画出 PNG，再手工包一层 ICO 头。

ICO 从 Vista 起支持直接内嵌 PNG，所以多尺寸只要拼几段就行。
绘制逻辑来自 logo.draw_logo，和运行时托盘图标完全同一套，不会再出现
「托盘一个样、桌面另一个样」。
"""
import struct
import sys

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

import logo

SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> bytes:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    logo.draw_logo(p, size)
    p.end()

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pm.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def main():
    app = QApplication(sys.argv)  # QPixmap 需要 QGuiApplication
    blobs = [(s, render(s)) for s in SIZES]

    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries = b""
    body = b""
    for size, data in blobs:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        body += data

    with open("icon.ico", "wb") as f:
        f.write(header + entries + body)
    print("icon.ico written,", len(header + entries + body), "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
