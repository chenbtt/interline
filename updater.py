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

import os
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


def build_script(new_exe: Path, target: Path, old_pid: int = 0,
                 marker: Path | None = None, log: Path | None = None) -> str:
    """生成替换脚本。单独拆出来是为了能测，不必真的覆盖 exe。

    运行中的 exe 不能被覆盖，所以只能退到外部脚本里做。move 重试上限 600 次
    （每次忙等约 0.2s，约两分钟），避免进程一直不退时脚本死循环。

    四个必须处理的坑：
    1. **不能用 ping 当延时器**。原来的写法是 `ping 127.0.0.1 -n 4`，但有些
       机器上回环 ping 会 100% 超时（实测某台 Win11），每个包要等满超时，
       脚本会卡十几秒才往下走。现在改成纯 CPU 忙等（for /l），不碰网络，
       也不会弹出控制台窗口。
    2. **用 move /y 直接覆盖，失败就重试**。同卷是重命名，瞬间完成，不会留
       下写了一半的 exe；旧进程还占着文件时 move 必失败，一直重试到成功。
       3. **move 成功 ≠ 旧进程收工**。文件句柄一放，move 就过了，但 PyInstaller
       的父引导进程还要删掉自己的 %TEMP%\\_MEIxxxxxx。`_MEI` 名字里带 pid，
       新旧进程一旦撞上同一个 pid，新进程刚解压出来的 python3xx.dll 就被
       旧进程的清理删掉 —— 报「Failed to load Python DLL」。所以拿到旧 PID
       之后要**显式等它从 tasklist 里消失**，别赌时序。
       注意 old_pid 该传**父引导的 pid**（onefile 的父比子后死、_MEI 也是它删的），
       传跑 Python 的那个子进程 pid 基本等于没等。
    4. **每次给新进程一个全新的 TEMP 子目录**，从物理上避开撞名；并且启动
       后用 marker 文件**握手确认新进程真的活着**，没活着就再启动一次（最多
       3 次）。前面几条都是「尽量不出错」，这条是「错了也能自己爬起来」。

    marker 由 app 在 main() 最开头写（早于任何 Qt 初始化），所以它存在就
    等价于「解压 + 加载 python 都成功了」。

    log 默认写 %LOCALAPPDATA%\\Interline\\update_log.txt；压测时传别的路径，
    免得把用户那份日志刷成一堆测试记录。
    """
    if log is None:
        log = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
               / "Interline" / "update_log.txt")
    mp = str(marker) if marker else str(log.parent / "restart.marker")

    waitpid = ""
    if old_pid:
        # 注意：这里刻意不用 find / findstr 判 PID —— 如果用户的 PATH 里有
        # Git/MSYS 的 Unix find，cmd 会优先命中它（实测在 Git Bash 里启动的
        # 进程，PATH 带着 usr\bin），判断直接失效。改用 tasklist 绝对路径 +
        # for /f 取第 2 个逗号字段，纯内置、不受 PATH 影响。
        waitpid = f"""rem 等旧进程彻底消失（含 PyInstaller 清理 _MEI 目录），最多约 60s
set "SYS=%SystemRoot%\\System32"
if not exist "%SYS%\\tasklist.exe" set "SYS=C:\\Windows\\System32"
set /a w=0
:waitloop
set "ALIVE="
for /f "tokens=2 delims=," %%B in ('"%SYS%\\tasklist.exe" /fi "PID eq {old_pid}" /nh /fo csv 2^>nul') do if "%%B"=="{old_pid}" set "ALIVE=1"
if not defined ALIVE goto launch0
call :nap
set /a w+=1
if %w% lss 300 goto waitloop
"""
    else:
        waitpid = "rem 没拿到旧 PID，跳过等待\n"

    return f"""@echo off
setlocal
set "NEW={new_exe}"
set "TARGET={target}"
set "MARKER={mp}"
set "LOG={log}"
if not exist "{log.parent}" mkdir "{log.parent}" >nul 2>&1
set /a n=0

:retry
move /y "%NEW%" "%TARGET%" >nul 2>&1
rem 新包被移走 = 替换成功；仍存在说明旧进程还占着 exe，继续等
if not exist "%NEW%" goto moved
set /a n+=1
if %n% geq 600 goto bail
call :nap
goto retry

:bail
echo [%DATE% %TIME%] target locked, give up >> "%LOG%"
del "%~f0"
exit /b 1

:moved
echo [%DATE% %TIME%] moved ok, wait_pid={old_pid} >> "%LOG%"
{waitpid}
:launch0
set /a tries=0

:launch
rem 每次全新 TEMP 子目录，彻底避开 PyInstaller 的 _MEI 目录撞名
set "TEMP=%LOCALAPPDATA%\\Interline\\run\\%RANDOM%%RANDOM%"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%" >nul 2>&1
del "%MARKER%" >nul 2>&1
echo [%DATE% %TIME%] launch try=%tries% TEMP=%TEMP% >> "%LOG%"
set "INTERLINE_RESTART_MARKER=%MARKER%"
start "" "%TARGET%"

rem 等新进程写回 marker：写出来了 = 解压 + 加载 python 都成功
rem 每轮约 20s（200 × 0.1s）：慢盘解压 64MB 单文件也要命，给够余量
set /a s=0
:waitmark
if exist "%MARKER%" goto ok
call :nap
set /a s+=1
if %s% lss 200 goto waitmark
set /a tries+=1
if %tries% lss 3 goto launch
echo [%DATE% %TIME%] marker timeout, give up >> "%LOG%"

:ok
echo [%DATE% %TIME%] done >> "%LOG%"
del "%~f0"
exit /b 0

:nap
rem 约 0.2s 忙等；不依赖 ping / timeout，无控制台也能跑
for /l %%i in (1,1,400000) do rem
exit /b 0
"""


def install_and_restart(new_exe: Path) -> bool:
    if not getattr(sys, "frozen", False):
        return False
    target = Path(sys.executable)
    if not new_exe.exists():
        return False
    # onefile 在 Windows 上是「父引导 + 子进程」两段：子进程才是跑 Python 的
    # 那一个（os.getpid()），而解压目录 _MEI 是父引导收尾时删的，父比子后死。
    # 所以等父 PID 才有意义 —— 等自己等于什么都没等。
    wait_pid = os.getpid()
    if getattr(sys, "_MEIPASS", ""):
        try:
            ppid = os.getppid()
        except Exception:
            ppid = 0
        if ppid and ppid != os.getpid():
            wait_pid = ppid
    script = Path(tempfile.gettempdir()) / "interline_update.cmd"
    try:
        script.write_text(build_script(new_exe, target, wait_pid),
                          encoding="gbk")
        # 只传 DETACHED_PROCESS：无控制台，且父进程（旧 exe）退出后脚本仍能继续跑。
        # 之前和 CREATE_NO_WINDOW 一起传，MSDN 明确说两者不能合用（语义冲突），
        # 实测偶尔会弹出可见的 cmd 窗口。
        # 显式构造 env：摘掉「重启握手」用的两个变量，否则新进程会被当成探针
        # 直接退出（只有 --restartcheck 才该带它们）。
        env = dict(os.environ)
        env.pop("INTERLINE_RESTART_MARKER", None)
        env.pop("INTERLINE_RESTART_PROBE", None)
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        subprocess.Popen(["cmd", "/c", str(script)],
                         creationflags=flags, env=env, close_fds=True)
        return True
    except Exception:
        return False
