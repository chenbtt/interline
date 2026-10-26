"""把一段文字按句念出来。

**别用 enqueue 排队**（踩过的坑）：
winrt 后端其实没有队列。连塞两句进去，它只读最后一句，而且 sayingWord
回调里的 uid 永远停在 0——返回的 id 和回调里的 uid 对不上。原来的「预取下一句」
策略因此会把正在读的句子顶掉，句索引卡在第一句不动，高亮也就不走了。

现在的做法：一次只 `say()` 一句，等 stateChanged 变成 Ready 再念下一句。
sayingWord 只用来驱动词级高亮，不看 uid，因为引擎里始终只有一句。

另外：`say()` 在 PyQt6 里不接受 id，只有 `enqueue` 返回 id，这也是上面那条
映射思路失效的原因之一。

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
        self._id_map: dict[int, int] = {}   # 留作兼容，队列策略已弃用
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
        self._say_next()

    def _say_next(self) -> None:
        """念下一句。引擎里始终只放一句，读完靠 stateChanged 回调续上。"""
        if not self._running or self._next >= len(self._sentences):
            return
        idx = self._next
        self._next += 1
        self._idx = idx
        # 立刻通知，不等 sayingWord：引擎从说话到报第一个词有几百毫秒延迟
        self.sentence_changed.emit(idx)
        self._speaker.say(self._sentences[idx])

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
        """跳句。从目标句重新往下读。"""
        if not self._running:
            return
        target = max(0, min(self._idx + delta, len(self._sentences) - 1))
        try:
            self._speaker.stop(QTextToSpeech.BoundaryHint.Immediate)
        except Exception:
            pass
        self._next = target
        self._say_next()

    def _finish(self) -> None:
        self._running = False
        self.state_changed.emit("ready")
        self.finished.emit()

    # -------------------------------------------------- 回调

    def _on_word(self, _word, _uid, start, length):
        # 不看 uid：winrt 后端报的 uid 恒定是 0，用它反查会永远命中第一句。
        # 引擎里始终只有一句在播，当前句索引就是 self._idx。
        if self._idx < 0 or self._idx >= len(self._sentences):
            return
        text = self._sentences[self._idx]
        i = utf16_to_index(text, start)
        n = utf16_to_index(text, start + length) - i
        self.word_spoken.emit(self._idx, i, max(1, n))

    def _on_state(self, state):
        if state == QTextToSpeech.State.Speaking:
            self.state_changed.emit("speaking")
        elif state == QTextToSpeech.State.Paused:
            self.state_changed.emit("paused")
        elif state == QTextToSpeech.State.Ready:
            if not self._running:
                self.state_changed.emit("ready")
                return
            # 这一句念完了：还有就接着念，没有了就收尾
            if self._next >= len(self._sentences):
                self._finish()
            else:
                self._say_next()
        elif state == QTextToSpeech.State.Error:
            self._running = False
            self.state_changed.emit("error")
