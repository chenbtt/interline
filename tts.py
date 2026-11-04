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
        self._paused = False
        self._start_idx = 0
        self._start_char = 0
        self._last_word_start = 0
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

    def stop_at(self) -> tuple[int, int]:
        """返回 (句索引, 句内 UTF-16 偏移)，用于之后从这里接着念。

        必须在 stop() 之前调用。winrt 的 pause/resume 不可用
        （resume 后状态是 Speaking 但不出声也不吐词），所以暂停一律做成
        「记住念到哪个词 → 停掉 → 之后从那个词重新开口」。
        """
        return max(0, self._idx), max(0, self._last_word_start)

    def rebuild(self) -> None:
        """换一个新的底层引擎。

        挂久了 winrt 的会话会僵死：resume() 不出声，连 say() 也哑掉，
        状态却看着正常。这种情况只能丢掉旧的 QTextToSpeech 重新建一个。
        调用前先把 running 之类清干净，重建后由调用方重新 start()。
        """
        self.stop()
        old = self._speaker
        try:
            old.stateChanged.disconnect()
            old.sayingWord.disconnect()
        except Exception:
            pass
        try:
            old.deleteLater()
        except Exception:
            pass
        self._speaker = QTextToSpeech(ENGINE)
        self._speaker.stateChanged.connect(self._on_state)
        self._speaker.sayingWord.connect(self._on_word)

    # -------------------------------------------------- 播放

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        # 用自己记的标志，不问引擎：挂久了 winrt 的状态会漂成 Ready/Error，
        # 按 state()==Paused 判断会失灵，调用方就走进「再暂停一次」的分支
        return self._paused

    @property
    def current_index(self) -> int:
        return self._idx

    def start(self, sentences: list[str], from_idx: int = 0,
              from_char: int = 0) -> None:
        """从第 from_idx 句的第 from_char 个字符开始念。

        from_char 是 UTF-16 码元偏移，用来实现「从停下的那个词接着说」。
        首句会按它截断，之后各句都完整。
        """
        self.stop()
        self._sentences = [s for s in sentences if s.strip()]
        if not self._sentences:
            return
        self._idx = -1
        self._paused = False
        self._next = max(0, min(from_idx, len(self._sentences) - 1))
        self._start_idx = self._next
        self._start_char = max(0, from_char)
        self._last_word_start = 0
        self._running = True
        self._say_next()

    def _say_next(self) -> None:
        """念下一句。引擎里始终只放一句，读完靠 stateChanged 回调续上。"""
        if not self._running or self._next >= len(self._sentences):
            return
        idx = self._next
        self._next += 1
        self._idx = idx
        self._last_word_start = 0
        # 立刻通知，不等 sayingWord：引擎从说话到报第一个词有几百毫秒延迟
        self.sentence_changed.emit(idx)
        text = self._sentences[idx]
        if idx == self._start_idx and self._start_char:
            # 首句从上次停下的那个词接上
            cut = utf16_to_index(text, self._start_char)
            text = text[cut:]
        self._speaker.say(text)

    def pause(self) -> None:
        if self._running:
            self._paused = True
            # 默认 hint 要等到句子边界才停，按下去半天没反应；要 Immediately
            self._speaker.pause(QTextToSpeech.BoundaryHint.Immediate)

    def resume(self) -> None:
        if self._running:
            self._paused = False
            self._speaker.resume()

    def stop(self) -> None:
        self._running = False
        self._paused = False
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
        self._paused = False
        self.state_changed.emit("ready")
        self.finished.emit()

    # -------------------------------------------------- 回调

    def _on_word(self, _word, _uid, start, length):
        # 不看 uid：winrt 后端报的 uid 恒定是 0，用它反查会永远命中第一句。
        # 引擎里始终只有一句在播，当前句索引就是 self._idx。
        if self._idx < 0 or self._idx >= len(self._sentences):
            return
        text = self._sentences[self._idx]
        # 首句可能被截断过，词偏移是相对截断后的文本，要补回原句里
        base = 0
        if self._idx == self._start_idx and self._start_char:
            base = utf16_to_index(text, self._start_char)
        i = base + utf16_to_index(text[base:], start)
        n = utf16_to_index(text[base:], start + length)
        n = max(1, n - (i - base))
        # 记下当前念到哪个词，暂停时据此从这里接着念
        self._last_word_start = i
        self.word_spoken.emit(self._idx, i, n)

    def _on_state(self, state):
        if state == QTextToSpeech.State.Speaking:
            self.state_changed.emit("speaking")
        elif state == QTextToSpeech.State.Paused:
            self.state_changed.emit("paused")
        elif state == QTextToSpeech.State.Ready:
            if not self._running:
                self.state_changed.emit("ready")
                return
            # 暂停中引擎偶尔也会报 Ready（挂久了状态会漂），
            # 这时候往下续读等于绕过用户的暂停，不能动
            if self._paused:
                return
            # 这一句念完了：还有就接着念，没有了就收尾
            if self._next >= len(self._sentences):
                self._finish()
            else:
                self._say_next()
        elif state == QTextToSpeech.State.Error:
            self._running = False
            self.state_changed.emit("error")
