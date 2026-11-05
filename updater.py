"""从 GitHub Release 检查新版本、下载新版 exe、替换自身并重启。

版本号只有一处：main.BUILD_VERSION（整数）。Release 的 tag 形如 v41，
取其中第一段数字来比大小，所以 v41 / 41 / v41.0.0 都能认。

版本信息取自 releases.atom：它和网页一样是普通资源，**不占 API 额度**，
里面带标题、tag 链接和完整的更新说明 HTML。API 未认证只有 60 次/小时，
出口 IP 一共享就容易 403，所以默认根本不碰 API。

下载直链按 Release 的固定命名规则拼，也不需要 API：
  https://github.com/<owner>/<repo>/releases/download/<tag>/Interline.exe
（会 302 到 objects.githubusercontent.com）

下载走 QNetworkAccessManager，靠 downloadProgress 做真进度条；
先写 .part 再改名，避免半截文件被当成安装包。
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from PyQt6.QtCore import QFile, QIODevice, QObject, QUrl, pyqtSignal
from PyQt6.QtNetwork import (QNetworkAccessManager, QNetworkReply,
                             QNetworkRequest)

ATOM = "https://github.com/{repo}/releases.atom"
ASSET = "https://github.com/{repo}/releases/download/{tag}/Interline.exe"
NS = {"a": "http://www.w3.org/2005/Atom"}
UA = "Interline-Updater"
REDIRECT = QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy


def parse_version(text: str) -> int:
    """'v41' / '41' / 'v41.0.0' -> 41；认不出返回 0。"""
    m = re.search(r"(\d+)", str(text or ""))
    return int(m.group(1)) if m else 0


def _prepare(req: QNetworkRequest) -> QNetworkRequest:
    req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, UA)
    # GitHub 的 asset 直链会 302 到另一个域，不跟随就只会拿到一个跳转头
    req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute, REDIRECT)
    return req


def parse_atom(xml_text: str, repo: str) -> dict | None:
    """从 releases.atom 取最新一条。

    entry 里的 title 是「v42 · 说明」，content 是 HTML 版更新说明，
    link 指向 /releases/tag/v42。asset 不在 feed 里，按固定命名规则拼。
    """
    root = ET.fromstring(xml_text)
    entry = root.find("a:entry", NS)
    if entry is None:
        return None
    title = (entry.findtext("a:title", "", NS) or "").strip()
    html_url = ""
    for link in entry.findall("a:link", NS):
        if link.get("rel") == "alternate":
            html_url = link.get("href", "") or ""
            break
    tag = html_url.rsplit("/tag/", 1)[-1] if "/tag/" in html_url else ""
    if not tag:
        tag = title.split("·")[0].strip()
    body_html = entry.findtext("a:content", "", NS) or ""
    return {
        "tag": tag,
        "version": parse_version(tag),
        "name": title,
        "body": body_html,
        "body_is_html": True,
        "html_url": html_url or f"https://github.com/{repo}/releases",
        "asset": ASSET.format(repo=repo, tag=tag) if tag else "",
        "size": 0,
    }


class Updater(QObject):
    """只做检查。结果用 checked(ok, info, err) 回主线程。"""

    checked = pyqtSignal(bool, object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._repo = ""

    def check(self, repo: str, timeout_ms: int = 12000) -> None:
        if self._reply is not None:
            return                      # 上一次还没回来，别叠请求
        if not repo:
            self.checked.emit(False, None, "未配置仓库")
            return
        self._repo = repo
        req = _prepare(QNetworkRequest(QUrl(ATOM.format(repo=repo))))
        req.setRawHeader(b"Accept", b"application/atom+xml")
        req.setTransferTimeout(timeout_ms)
        self._reply = self._nam.get(req)
        self._reply.finished.connect(self._on_finished)

    def _on_finished(self) -> None:
        reply, self._reply = self._reply, None
        info, err = None, ""
        if reply is None:
            return
        code = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if reply.error() != QNetworkReply.NetworkError.NoError:
            err = reply.errorString()
            if code:
                err = f"HTTP {code} · {err}"
        else:
            try:
                raw = bytes(reply.readAll()).decode("utf-8", "replace")
                info = parse_atom(raw, self._repo)
                if info is None:
                    err = "发布信息为空"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        reply.deleteLater()
        self.checked.emit(info is not None, info, err)


class Downloader(QObject):
    """流式下载到 .part，完成后改名。"""

    progress = pyqtSignal(int, int)          # received, total
    done = pyqtSignal(bool, str, str)        # ok, path, err

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._file: QFile | None = None
        self._dest = Path()

    @property
    def partial(self) -> Path:
        return self._dest.with_suffix(self._dest.suffix + ".part")

    def start(self, url: str, dest: Path) -> None:
        self.cancel()
        self._dest = dest
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.done.emit(False, "", f"无法创建目录：{e}")
            return
        self._file = QFile(str(self.partial))
        if not self._file.open(QIODevice.OpenModeFlag.WriteOnly
                               | QIODevice.OpenModeFlag.Truncate):
            self.done.emit(False, "", f"无法写入 {self.partial}")
            return
        req = _prepare(QNetworkRequest(QUrl(url)))
        # 下载可能几十秒，别用短超时；用户点取消走 cancel()
        req.setTransferTimeout(120000)
        self._reply = self._nam.get(req)
        self._reply.readyRead.connect(self._on_read)
        self._reply.downloadProgress.connect(
            lambda r, t: self.progress.emit(int(r), int(t)))
        self._reply.finished.connect(self._on_finished)

    def cancel(self) -> None:
        if self._reply is not None:
            reply, self._reply = self._reply, None
            try:
                reply.abort()
                reply.deleteLater()
            except Exception:
                pass
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
            try:
                self.partial.unlink(missing_ok=True)
            except Exception:
                pass

    def _on_read(self) -> None:
        if self._reply is None or self._file is None:
            return
        chunk = bytes(self._reply.readAll())
        if chunk:
            self._file.write(chunk)

    def _on_finished(self) -> None:
        reply, self._reply = self._reply, None
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
        if reply is None:
            self.done.emit(False, "", "已取消")
            return
        if reply.error() != QNetworkReply.NetworkError.NoError:
            err = reply.errorString()
            reply.deleteLater()
            self._discard()
            self.done.emit(False, "", err)
            return
        reply.deleteLater()
        part = self.partial
        try:
            size = part.stat().st_size
        except Exception:
            size = 0
        if size < 1_000_000:
            self._discard()
            self.done.emit(False, "", f"文件太小（{size} 字节），可能不是安装包")
            return
        # 肉眼可见的完整性检查：Windows 可执行文件必须以 MZ 开头
        try:
            with open(part, "rb") as f:
                if f.read(2) != b"MZ":
                    raise ValueError("not PE")
        except Exception:
            self._discard()
            self.done.emit(False, "", "下载内容不是 Windows 可执行文件")
            return
        try:
            part.replace(self._dest)
        except Exception as e:
            self._discard()
            self.done.emit(False, "", f"改名失败：{e}")
            return
        self.done.emit(True, str(self._dest), "")

    def _discard(self) -> None:
        try:
            self.partial.unlink(missing_ok=True)
        except Exception:
            pass


def build_script(new_exe: Path, target: Path) -> str:
    """生成替换脚本。单独拆出来是为了能测，不必真的覆盖 exe。

    运行中的 exe 不能被覆盖，所以只能退到外部脚本里做。用 ping 代替
    timeout 等待：cmd 以无窗口方式启动时没有控制台，timeout 会直接报错。
    重试上限 40 次（约 80 秒），避免进程一直不退时脚本死循环。

    两个必须处理的坑：
    1. 用 move 而不是 copy：同卷是重命名，瞬间完成，不会留下写了一半的 exe；
    2. PyInstaller onefile 把文件解压到 %TEMP%\\_MEIxxxxxx 再加载 python3xx.dll，
       目录名按秒级种子生成。更新完立刻启动，新旧进程可能撞上同一个目录名，
       旧进程退出时正在删它 —— 新进程就会报「Failed to load Python DLL」。
       所以给新进程单独指一个 TEMP，和旧进程彻底隔开。
    """
    return f"""@echo off
setlocal
ping 127.0.0.1 -n 4 >nul
set /a n=0
:retry
if exist "{target}.old" del /q "{target}.old" >nul 2>&1
move /y "{target}" "{target}.old" >nul 2>&1
move /y "{new_exe}" "{target}" >nul 2>&1
if exist "{target}" goto done
set /a n+=1
if %n% geq 40 exit /b 1
ping 127.0.0.1 -n 2 >nul
goto retry
:done
set "TEMP=%LOCALAPPDATA%\\Interline\\run"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%" >nul 2>&1
start "" "{target}"
del "%~f0"
"""


def install_and_restart(new_exe: Path) -> bool:
    if not getattr(sys, "frozen", False):
        return False
    target = Path(sys.executable)
    if not new_exe.exists():
        return False
    script = Path(tempfile.gettempdir()) / "interline_update.cmd"
    try:
        script.write_text(build_script(new_exe, target), encoding="gbk")
        flags = 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        subprocess.Popen(["cmd", "/c", str(script)],
                         creationflags=flags, close_fds=True)
        return True
    except Exception:
        return False
