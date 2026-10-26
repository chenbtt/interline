"""把一段文字按句念出来。

两个 API 细节（和 Qt C++ 文档不一致，踩过坑）：
- PyQt6 里 `say(text)` **不接受 id**，`enqueue(text)` 才返回 id
- 所以句索引是靠 enqueue 的返回值建立映射，sayingWord 回调拿这个 id 反查

队列策略：一次只挂一句缓冲。第一句 enqueue 后引擎空闲会立刻读，
等 sayingWord 报告进入这句时再把下一句 enqueue 进去，既不吞也不断。

引擎必须显式指定 winrt：默认构造在某些机器上会落到 mock（无声）。
winrt 提供 Huihui / Yaoyao / Kangkang 三个中文音色。
"""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtTextToSpeech import QTextToSpeech

ENGINE = "winrt"


def utf16_to_index(text: str, u16_pos: int) -> int:
    """QTextToSpeech 的偏移是按 UTF-16 码元算的，Python 是字符下标。

    含 emoji 或生僻字（代理对）时两者会错位，需要换算。
    """
    if u16_pos <= 0:
        return 0
    # 快速路径：没有代理对时两者相等
    if len(text.encode("utf-16-le")) // 2 == len(text):
        return min(u16_pos, len(text))
    units = 0
    for i, ch in enumerate(text):
        if units >= u16_pos:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


class SpeechReader(QObject):
    """逐句朗读。句子由外部切好后传进来。"""

    sentence_changed = pyqtSignal(int)        # 开始读第几句
    word_spoken = pyqtSignal(int, int, int)   # (句索引, 句内字符起点, 长度)
    finished = pyqtSignal()
    state_changed = pyqtSignal(str)           # speaking | paused | ready | error

    def __init__(self, parent=None, engine: str | None = None):
        super().__init__(parent)
        # engine 只给测试用：沙箱/无音频会话下 winrt 的 say() 会挂起，
        # 那时改用 "mock" 验证队列逻辑
        self._speaker = QTextToSpeech(engine or ENGINE)
        self._sentences: list[str] = []
        self._idx = -1        # 正在读的句子
        self._next = 0        # 下一句待入队
        self._running = False
        self._id_map: dict[int, int] = {}   # enqueue 返回的 id -> 句子索引
        self._speaker.stateChanged.connect(self._on_state)
        self._speaker.sayingWord.connect(self._on_word)

    # -------------------------------------------------- 设备

    def available_voices(self) -> list[str]:
        return [v.name() for v in self._speaker.availableVoices()]

    def set_voice(self, name: str) -> bool:
        for v in self._speaker.availableVoices():
            if v.name() == name:
                self._speaker.setVoice(v)
                return True
        return False

    def current_voice(self) -> str:
        v = self._speaker.voice()
        return v.name() if v else ""

    def set_rate(self, rate: float) -> None:
        self._speaker.setRate(max(-1.0, min(1.0, rate)))

    def set_volume(self, vol: float) -> None:
        self._speaker.setVolume(max(0.0, min(1.0, vol)))

    # -------------------------------------------------- 播放

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._speaker.state() == QTextToSpeech.State.Paused

    @property
    def current_index(self) -> int:
        return self._idx

    def start(self, sentences: list[str], from_idx: int = 0) -> None:
        self.stop()
        self._sentences = [s for s in sentences if s.strip()]
        if not self._sentences:
            return
        self._idx = -1
        self._next = max(0, min(from_idx, len(self._sentences) - 1))
        self._running = True
        self._enqueue_next()

    def _enqueue_next(self) -> None:
        """把下一句挂进队列。引擎空闲时会立刻开始读，所以这里不用区分首次。"""
        if not self._running or self._next >= len(self._sentences):
            return
        idx = self._next
        self._next += 1
        uid = self._speaker.enqueue(self._sentences[idx])
        self._id_map[uid] = idx

    def pause(self) -> None:
        if self._running:
            # 默认 hint 要等到句子边界才停，按下去半天没反应；要 Immediately
            self._speaker.pause(QTextToSpeech.BoundaryHint.Immediate)

    def resume(self) -> None:
        if self._running:
            self._speaker.resume()

    def stop(self) -> None:
        self._running = False
        self._idx = -1
        self._next = 0
        self._id_map.clear()
        try:
            self._speaker.stop(QTextToSpeech.BoundaryHint.Immediate)
        except Exception:
            pass

    def skip(self, delta: int) -> None:
        """跳句。清空队列，从目标句重新往下读。"""
        if not self._running:
            return
        target = max(0, min(self._idx + delta, len(self._sentences) - 1))
        try:
            self._speaker.stop(QTextToSpeech.BoundaryHint.Immediate)
        except Exception:
            pass
        self._id_map.clear()
        self._idx = -1
        self._next = target
        self._enqueue_next()

    def _finish(self) -> None:
        self._running = False
        self.state_changed.emit("ready")
        self.finished.emit()

    # -------------------------------------------------- 回调

    def _on_word(self, _word, uid, start, length):
        idx = self._id_map.get(uid)
        if idx is None:
            return
        # 进入新句子：通知 UI，并立刻预取下一句
        if idx != self._idx:
            self._idx = idx
            self.sentence_changed.emit(idx)
            self._enqueue_next()
        text = self._sentences[idx]
        i = utf16_to_index(text, start)
        n = utf16_to_index(text, start + length) - i
        self.word_spoken.emit(idx, i, max(1, n))

    def _on_state(self, state):
        if state == QTextToSpeech.State.Speaking:
            self.state_changed.emit("speaking")
        elif state == QTextToSpeech.State.Paused:
            self.state_changed.emit("paused")
        elif state == QTextToSpeech.State.Ready:
            if not self._running:
                self.state_changed.emit("ready")
                return
            # 队列空了但还有句子没读：说明预取没成功，兜底补一句
            if self._next >= len(self._sentences):
                self._finish()
            else:
                self._enqueue_next()
        elif state == QTextToSpeech.State.Error:
            self._running = False
            self.state_changed.emit("error")
