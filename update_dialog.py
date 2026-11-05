"""更新提示窗口：最新版本号 + Release 内容 + 下载进度。

样式跟设置窗口同一套取色，浅色皮肤下也能看清。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from PyQt6.QtCore import QTimer, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QPalette
from PyQt6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                             QProgressBar, QPushButton, QTextBrowser,
                             QVBoxLayout)

import theme
import updater


class UpdateDialog(QDialog):
    def __init__(self, reader, info: dict):
        super().__init__(reader)
        self.reader = reader
        self.st = reader.st
        self.info = info or {}
        self._dl: updater.Downloader | None = None
        self.setWindowTitle("发现新版本")
        self.resize(640, 560)
        self.setMinimumSize(520, 420)
        self._build()
        self._apply_style()

    # -------------------------------------------------- 骨架

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(10)
        tag = self.info.get("tag") or self.info.get("name") or ""
        self.title = QLabel(f"新版本 {tag}" if tag else "发现新版本")
        self.title.setObjectName("dlgTitle")
        top.addWidget(self.title)
        top.addStretch(1)
        self.btn_page = QPushButton("前往发布页")
        self.btn_page.setObjectName("linkBtn")
        self.btn_page.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_page.clicked.connect(self._open_page)
        top.addWidget(self.btn_page)
        lay.addLayout(top)

        self.body = QTextBrowser(self)
        self.body.setObjectName("releaseBody")
        self.body.setOpenExternalLinks(True)
        text = (self.info.get("body") or "").strip()
        if self.info.get("body_is_html"):
            # releases.atom 给的就是 HTML，直接塞进去，省一层 markdown 转换
            self.body.setHtml(text or "<p>这个版本没有写更新说明。</p>")
        else:
            self.body.setMarkdown(text or "这个版本没有写更新说明。")
        lay.addWidget(self.body, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.status = QLabel("")
        self.status.setObjectName("dlgHint")
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)

        self.bar = QProgressBar(self)
        self.bar.setFixedWidth(170)
        self.bar.setFixedHeight(8)
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100)
        self.bar.setVisible(False)
        bottom.addWidget(self.bar)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.clicked.connect(self._on_cancel)
        bottom.addWidget(self.btn_cancel)

        self.btn_update = QPushButton("更新")
        self.btn_update.setObjectName("primaryBtn")
        self.btn_update.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_update.setDefault(True)
        self.btn_update.clicked.connect(self._start_download)
        bottom.addWidget(self.btn_update)
        lay.addLayout(bottom)

    # -------------------------------------------------- 行为

    def _open_page(self):
        url = self.info.get("html_url") or ""
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _start_download(self):
        asset = self.info.get("asset") or ""
        if not asset:
            # 这个 Release 没挂 exe，只能让用户去发布页自己拿
            self.status.setText("这个版本没有可下载的安装包，已打开发布页。")
            self._open_page()
            return
        dest = (Path(tempfile.gettempdir()) / "Interline_update"
                / "Interline.exe")
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        self.btn_update.setEnabled(False)
        self.btn_update.setText("下载中…")
        self.btn_cancel.setText("取消")
        self.bar.setVisible(True)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.status.setText("正在从 GitHub 下载…")

        self._dl = updater.Downloader(self)
        self._dl.progress.connect(self._on_progress)
        self._dl.done.connect(self._on_downloaded)
        self._dl.start(asset, dest)

    def _on_progress(self, received: int, total: int):
        if total > 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(received * 100 / total))
            self.status.setText(
                f"正在下载 {received / 1048576:.1f} / {total / 1048576:.1f} MB")
        else:
            self.bar.setRange(0, 0)     # 大小未知，转圈
            self.status.setText(f"正在下载 {received / 1048576:.1f} MB")

    def _on_downloaded(self, ok: bool, path: str, err: str):
        self.bar.setVisible(False)
        self._dl = None
        if not ok:
            self.status.setText(f"下载失败：{err[:70]}")
            self.btn_update.setEnabled(True)
            self.btn_update.setText("重试")
            self.btn_cancel.setText("关闭")
            return

        self.btn_update.setVisible(False)
        self.btn_cancel.setText("关闭")
        if updater.install_and_restart(Path(path)):
            self.status.setText("下载完成，正在安装…程序会自动重启")
            QTimer.singleShot(1500, QApplication.quit)
        else:
            # 源码运行时没有可替换的 exe，退化成「打开下载目录」
            self.status.setText("已下载完成。自动安装不可用（源码运行），"
                                "请手动用它替换 exe：")
            self.btn_update.setVisible(True)
            self.btn_update.setEnabled(True)
            self.btn_update.setText("打开文件夹")
            try:
                self.btn_update.clicked.disconnect()
            except Exception:
                pass
            self.btn_update.clicked.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(Path(path).parent))))

    def _on_cancel(self):
        if self._dl is not None:
            try:
                self._dl.done.disconnect()
            except Exception:
                pass
            self._dl.cancel()
            self._dl = None
        self.reject()

    def closeEvent(self, ev):
        if self._dl is not None:
            try:
                self._dl.done.disconnect()
            except Exception:
                pass
            self._dl.cancel()
            self._dl = None
        super().closeEvent(ev)

    # -------------------------------------------------- 样式

    def _apply_style(self):
        bg = self.st.get("colors.bg", "#0d0f12")
        fg = self.st.get("colors.fg", "#c3cbd6")
        heading = self.st.get("colors.heading", "#a8bccd")
        ov = theme.overlay(self.st)
        faint = theme.dim_text(self.st, "faint")

        # QTextBrowser 里 setMarkdown 的正文不吃 QSS 的 color，得改调色板
        pal = self.body.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor(fg))
        pal.setColor(QPalette.ColorRole.Base, QColor(0, 0, 0, 0))
        self.body.setPalette(pal)

        self.setStyleSheet(f"""
            QDialog {{ background: {bg}; }}
            QLabel {{ color: {fg}; font-size: 12.5px; }}
            QLabel#dlgTitle {{ font-size: 16px; color: {heading}; }}
            QLabel#dlgHint {{ color: {faint}; font-size: 11.5px; }}
            QTextBrowser#releaseBody {{
                background: rgba({ov},.05);
                border: 1px solid rgba({ov},.10);
                border-radius: 6px; padding: 8px 10px; font-size: 12.5px;
            }}
            QPushButton {{
                background: rgba({ov},.10); border: none; border-radius: 5px;
                color: {fg}; font-size: 12.5px; padding: 6px 14px;
            }}
            QPushButton:hover {{ background: rgba({ov},.20); }}
            QPushButton#primaryBtn, QPushButton#linkBtn {{
                background: #2f6f9e; color: #ffffff;
            }}
            QPushButton#primaryBtn:hover, QPushButton#linkBtn:hover {{
                background: #3a82b8;
            }}
            QPushButton#primaryBtn:disabled {{
                background: rgba({ov},.12); color: {faint};
            }}
            QProgressBar {{
                background: rgba({ov},.10); border: none; border-radius: 4px;
            }}
            QProgressBar::chunk {{ background: #5f8fa8; border-radius: 4px; }}
            QScrollBar:vertical {{
                background: transparent; width: 9px; margin: 2px 0;
            }}
            QScrollBar::handle:vertical {{
                background: rgba({ov},.18); border-radius: 4px; min-height: 24px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)
