"""奶蛙音效：一个独立的 QMediaPlayer。

不复用朗读的播放器 —— 那边有预取队列和词边界映射，混进来会互相打架。
这里只负责「放一条短的、随时能掐断」。
"""
from __future__ import annotations

import random
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

import assets

LAUGH = "laugh.mp3"
VOICES = ("voice_01.mp3", "voice_02.mp3", "voice_03.mp3", "voice_04.mp3")

# 读不到时长时的兜底，别让动画 hold 变成 0
FALLBACK_MS = 1500

# 预热放多久就掐：够把端点唤醒即可，长了只是白占播放器
WARM_MS = 160


class PetAudio(QObject):
    """奶蛙音效播放器。

    时长从 manifest 里读（构建期算好的），不依赖 QMediaPlayer 的异步
    durationChanged —— 那边要等媒体加载完，拿不到就只能瞎猜动画时长。
    """

    notice = pyqtSignal(str)

    def __init__(self, st, parent=None) -> None:
        super().__init__(parent)
        self.st = st
        self._player = QMediaPlayer(self)
        self._out = QAudioOutput(self)
        self._player.setAudioOutput(self._out)

        self._durations: dict[str, int] = {}
        self._last_play: dict[str, float] = {}
        self._device_desc: str | None = None
        self._bad: set[str] = set()          # 解不出来的，别再试第二遍
        self._warned = False
        self._warming = False
        self._warmed = False
        self._load_manifest()

        self._player.errorOccurred.connect(self._on_error)
        # 设备必须在构造时就定好。留在 play() 里首次调用等于现建一次音频 sink，
        # 环回录音实测：首播 132ms 才出声，后续只要 93ms —— 差的就是被吞掉的开头
        self.set_output_device(self.st.get("tts.output_device", ""))

    # -------------------------------------------------- 元数据

    def _load_manifest(self) -> None:
        try:
            import json
            meta = json.loads(Path(assets.naiwa("manifest.json")).read_text(encoding="utf-8"))
            self._durations = {str(k): int(v) for k, v in (meta.get("sfx") or {}).items()}
        except Exception:
            self._durations = {}

    def duration(self, clip: str) -> int:
        return self._durations.get(clip, 0) or FALLBACK_MS

    @staticmethod
    def random_voice() -> str:
        return random.choice(VOICES)

    # -------------------------------------------------- 设备与音量

    def set_output_device(self, desc: str) -> bool:
        """跟随朗读的输出设备。

        QMediaPlayer 默认只走系统默认设备，蓝牙耳机没连时「播完但听不到」——
        这和朗读踩过的是同一个坑，所以设置里指了哪个设备，音效就跟哪个。
        """
        desc = desc or ""
        if desc == self._device_desc:
            return True
        if desc:
            dev = next((d for d in QMediaDevices.audioOutputs()
                        if d.description() == desc), None)
            if dev is None:
                return False        # 设备被拔了，保留原设置
        else:
            dev = QMediaDevices.defaultAudioOutput()
        try:
            self._out.setDevice(dev)
            self._device_desc = desc
            return True
        except Exception:
            return False

    def volume(self) -> float:
        return max(0.0, min(1.0, float(self.st.get("pet.sound_volume", 0.7))))

    # -------------------------------------------------- 播放

    def warm(self) -> bool:
        """把音频链路提前叫醒，返回是否真的预热了一次。

        首次 play 要现走一遍端点初始化（蓝牙/眼镜这类设备还得等链路唤醒），
        这段延迟里写进去的样本是听不到的。用静音放一小小段把这步做掉，
        真正要出声的时候链路已经热了。

        静音用 muted + volume 0 双保险：单靠 0 音量在某些设备上只是衰减，
        真漏出来就是「没点它自己叫了一声」。
        """
        if self._warmed or not self.st.get("pet.sound", True):
            return False
        path = assets.naiwa("sfx/voice_01.mp3")
        if not Path(path).exists():
            return False

        self._warmed = True
        self._warming = True
        try:
            self.set_output_device(self.st.get("tts.output_device", ""))
            self._out.setVolume(0.0)
            self._out.setMuted(True)
            self._player.stop()
            self._player.setSource(QUrl.fromLocalFile(str(path)))
            self._player.play()
            QTimer.singleShot(WARM_MS, self._warm_done)
            return True
        except Exception:
            self._warm_done()
            return False

    def _warm_done(self) -> None:
        """预热收尾。期间有真声来了就别掐了，那会是「笑到一半被腰斩」。"""
        if not self._warming:
            return
        self._warming = False
        try:
            self._player.stop()
            self._out.setMuted(False)
            self._out.setVolume(self.volume())
        except Exception:
            pass

    def play(self, clip: str, cooldown_ms: int = 0) -> bool:
        """放一条音效。返回是否真的出声。"""
        if not self.st.get("pet.sound", True):
            return False
        if clip in self._bad:
            return False

        path = assets.naiwa(f"sfx/{clip}")
        if not Path(path).exists():
            self._bad.add(clip)
            return False

        now = time.monotonic() * 1000.0
        if cooldown_ms and now - self._last_play.get(clip, -1e9) < cooldown_ms:
            return False        # 冷却中：动画照播，但别再响一遍

        # 真声先到：预热作废，别让 _warm_done 把这一条掐断
        if self._warming:
            self._warming = False
            self._out.setMuted(False)

        self.set_output_device(self.st.get("tts.output_device", ""))
        self._out.setVolume(self.volume())
        self._last_play[clip] = now
        self._warmed = True

        # 单实例：先掐掉上一条，天然不会叠加
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.setPosition(0)
        self._player.play()
        return True

    def stop(self) -> None:
        try:
            self._player.stop()
        except Exception:
            pass

    # -------------------------------------------------- 错误

    def _on_error(self, err, msg: str) -> None:
        """解码失败只降级不弹窗：奶读模式的动画不该因为一条音效而中断。"""
        if self._player.source().isEmpty():
            return
        name = Path(self._player.source().toLocalFile()).name
        if name:
            self._bad.add(name)
        if not self._warned:
            self._warned = True
            self.notice.emit("奶蛙音效放不出来，已静音")
