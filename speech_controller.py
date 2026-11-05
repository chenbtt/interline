"""双引擎朗读控制器。

对外保持 tts.SpeechReader 的接口和信号，主窗口只认 self.speech，
不关心底下是系统语音还是 Edge 在线音色。

音色 id 形如 ``sys:Microsoft Huihui Desktop`` / ``edge:zh-CN-XiaoxiaoNeural``，
存在 config.json 的 tts.voice_id。默认空 → 系统语音（用户明确要求默认离线）。
老配置只有 tts.voice（纯系统语音名），启动时自动按 sys: 前缀兼容。

默认系统；选了在线音色才联网。在线合成失败时**这一次**自动切回系统语音，
但不改配置——下次点「听」仍会再试在线，用户要彻底关掉就在设置里选系统音色。
"""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtMultimedia import QMediaDevices

import tts

SYS = "sys:"
EDGE = "edge:"


class SpeechController(QObject):
    sentence_changed = pyqtSignal(int)
    word_spoken = pyqtSignal(int, int, int)
    finished = pyqtSignal()
    state_changed = pyqtSignal(str)
    notice = pyqtSignal(str)

    def __init__(self, parent, st):
        super().__init__(parent)
        self.st = st
        self._sys = tts.SpeechReader(self)
        self._edge = None
        self._edge_mod = None
        self._edge_err = ""
        try:
            import edge_speech
            self._edge_mod = edge_speech
            self._edge = edge_speech.EdgeEngine(self)
        except Exception as e:      # 没打进包/缺依赖时不影响离线朗读
            self._edge_err = f"{type(e).__name__}: {e}"

        self._kind = "sys"
        self._active = self._sys
        self._sentences: list[str] = []
        self._voice_id = ""
        self._rate = 0.0
        self._running = False
        self._paused = False
        self._resume: tuple[int, int] | None = None
        self._last_start = 0

        for eng in (self._sys, self._edge):
            if eng is None:
                continue
            eng.sentence_changed.connect(self.sentence_changed)
            eng.word_spoken.connect(self.word_spoken)
            eng.finished.connect(self._on_finished)
            eng.state_changed.connect(self._on_state)
        if self._edge is not None:
            self._edge.failed.connect(self._on_edge_failed)

        self.apply_config()

    # -------------------------------------------------- 配置

    def apply_config(self) -> None:
        """每次点「听」前重读配置：音色、语速、音量都即时生效。"""
        vid = self.st.get("tts.voice_id", "") or ""
        if not vid:
            old = self.st.get("tts.voice", "") or ""
            vid = (SYS + old) if old else ""
        self._rate = float(self.st.get("tts.rate", 0.0) or 0.0)
        vol = float(self.st.get("tts.volume", 1.0) or 1.0)
        self._sys.set_rate(self._rate)
        self._sys.set_volume(vol)
        if self._edge is not None:
            self._edge.set_rate(self._rate)
            self._edge.set_volume(vol)
            self._edge.set_device(self.st.get("tts.output_device", "") or "")
        self.set_voice(vid)

    def output_devices(self) -> list[tuple[str, str]]:
        """(存进配置的值, 显示名)。值用设备描述，本机唯一且可读。"""
        out = [("", "跟随系统（默认）")]
        try:
            for d in QMediaDevices.audioOutputs():
                out.append((d.description(), d.description()))
        except Exception:
            pass
        return out

    def set_output_device(self, desc: str) -> None:
        if self._edge is None:
            return
        self._edge.set_device(desc)
        if self._running and self._kind == "edge":
            # 换设备要重新 setSource 才生效；顺手从当前位置接上，
            # 用户不用重新点一遍「听」
            pos = self.stop_at()
            self._edge.stop()
            self._edge.start(self._sentences, *pos)

    def voice_choices(self) -> list[tuple[str, str, str]]:
        """(显示名, voice_id, 分组)。设置窗口和右键菜单共用这一份。"""
        out = [(n, SYS + n, "sys") for n in self._sys.available_voices()]
        if self._edge_mod is not None:
            out += [(label, EDGE + short, "edge")
                    for label, short in self._edge_mod.EDGE_VOICES]
        return out

    def available_voices(self) -> list[str]:
        return self._sys.available_voices()

    def current_voice(self) -> str:
        if self._voice_id:
            return self._voice_id
        name = self._sys.current_voice()
        return (SYS + name) if name else ""

    def set_voice(self, voice_id: str) -> bool:
        voice_id = voice_id or ""
        kind = "edge" if (voice_id.startswith(EDGE) and self._edge is not None) else "sys"
        name = voice_id.split(":", 1)[1] if ":" in voice_id else voice_id
        if kind == "sys":
            ok = self._sys.set_voice(name) if name else True
        else:
            ok = self._edge.set_voice(name)
            if not ok:      # 服务端音色清单变了，退回第一个可用音色
                self._edge.set_voice(self._edge_mod.EDGE_VOICES[0][1])

        if self._running and kind != self._kind:
            # 朗读中换引擎：记住位置，停掉旧的，用新引擎从同一处接上
            pos = self.stop_at()
            self._active.stop()
            self._kind, self._active = kind, (self._edge if kind == "edge" else self._sys)
            self._voice_id = voice_id
            self.start(self._sentences, *pos)
            return ok
        self._kind = kind
        self._active = self._edge if kind == "edge" else self._sys
        self._voice_id = voice_id
        return ok

    def set_rate(self, rate: float) -> None:
        self._rate = float(rate)
        self._sys.set_rate(self._rate)
        if self._edge is not None:
            self._edge.set_rate(self._rate)

    def set_volume(self, vol: float) -> None:
        self._sys.set_volume(vol)
        if self._edge is not None:
            self._edge.set_volume(vol)

    # -------------------------------------------------- 状态

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def current_index(self) -> int:
        return self._active.current_index

    @property
    def edge_available(self) -> bool:
        return self._edge is not None

    # -------------------------------------------------- 播放

    def start(self, sentences: list[str], from_idx: int = 0,
              from_char: int = 0) -> None:
        self._sentences = list(sentences)
        self._last_start = max(0, from_idx)
        self._running = True
        self._paused = False
        self._resume = None
        self._active.start(sentences, from_idx, from_char)

    def stop_at(self) -> tuple[int, int]:
        if self._paused and self._resume is not None:
            return self._resume
        return self._active.stop_at()

    def stop(self) -> None:
        self._running = False
        self._paused = False
        self._resume = None
        self._active.stop()

    def rebuild(self) -> None:
        self._active.rebuild()

    def skip(self, delta: int) -> None:
        self._active.skip(delta)

    def pause(self) -> None:
        """老板键隐藏窗口时用。

        两个引擎都统一成「记住位置 → 真正停掉 → 之后从那个位置重新开口」，
        不依赖 pause/resume 的长期状态：winrt 的 resume 实测根本不出声。
        """
        if not self._running or self._paused:
            return
        self._resume = self.stop_at()
        self._paused = True
        self._active.stop()

    def resume(self) -> None:
        if not self._paused:
            return
        self._paused = False
        pos = self._resume or (self._last_start, 0)
        self._resume = None
        self._active.start(self._sentences, *pos)

    # -------------------------------------------------- 回调

    def _on_finished(self) -> None:
        self._running = False
        self.finished.emit()

    def _on_state(self, state: str) -> None:
        self.state_changed.emit(state)

    def _on_edge_failed(self, err: str) -> None:
        if self._kind != "edge" or not self._running:
            return
        idx = self._edge.current_index
        if idx < 0:
            idx = self._last_start
        self._edge.stop()
        self._kind = "sys"
        self._active = self._sys
        self.notice.emit("在线音色连不上，已切回系统语音")
        self._sys.start(self._sentences, idx, 0)
