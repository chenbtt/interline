"""Edge 在线神经音色的逐句朗读引擎。

与 tts.SpeechReader 对外同构（信号、start/stop/stop_at/current_index …），
由 speech_controller 决定什么时候用哪个引擎。

为什么不用 Qt 的 pause/resume：QMediaPlayer 的暂停本身是可靠的（实测 8 秒
长暂停后 position 照常推进），但「停几小时」不该依赖长期存活的播放器状态。
所以这里统一做成：停 → 记下 (句子, 音频毫秒位置, 词偏移) 并释放播放器；
续 → 重新 setSource，等 LoadedMedia 后 setPosition 再 play。
**seek 必须等 LoadedMedia**：setSource 之后立刻 setPosition 会被丢弃，
实测会从头播。

一句一个 MP3，音频和词边界在同一次 stream() 里拿回来；播当前句时后台
预取下一句，把约 1 秒的网络合成延迟藏起来。缓存按 文本+音色+语速 哈希，
放在数据目录的 tts-cache/ 下。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

import store

# edge_tts.list_voices() 实测的 zh-CN 音色，静态列出来避免设置窗口联网等待
EDGE_VOICES = [
    ("晓晓 · 女声 · 通用", "zh-CN-XiaoxiaoNeural"),
    ("晓伊 · 女声 · 温柔", "zh-CN-XiaoyiNeural"),
    ("云希 · 男声 · 通用", "zh-CN-YunxiNeural"),
    ("云健 · 男声 · 浑厚", "zh-CN-YunjianNeural"),
    ("云夏 · 男声 · 少年", "zh-CN-YunxiaNeural"),
    ("云扬 · 男声 · 播报", "zh-CN-YunyangNeural"),
    ("晓北 · 东北话", "zh-CN-liaoning-XiaobeiNeural"),
    ("晓妮 · 陕西话", "zh-CN-shaanxi-XiaoniNeural"),
]

CACHE_DIR_NAME = "tts-cache"
SYNTH_TIMEOUT_MS = 8000


def rate_arg(rate: float) -> str:
    """配置里的 -1..1 折算成 edge-tts 的百分比参数。"""
    return f"{int(round(max(-1.0, min(1.0, rate)) * 100)):+d}%"


def cache_key(text: str, voice: str, rate: float) -> str:
    raw = f"{text}|{voice}|{rate_arg(rate)}".encode("utf-8", "ignore")
    return hashlib.sha1(raw).hexdigest()[:24]


def map_words(text: str, words: list) -> list[tuple[float, int, int]]:
    """把 WordBoundary 的时间轴映射回句内字符下标。

    offset 单位是 100ns（实测 87,000,000 = 8.7 秒）。中文按词切分且不含
    标点，所以用顺序 find 跳过标点来对齐。
    """
    out: list[tuple[float, int, int]] = []
    pos = 0
    for w in words:
        try:
            seg = str(w[2])
            ms = int(w[0]) / 10000.0
        except (IndexError, TypeError, ValueError):
            continue
        i = text.find(seg, pos) if seg else -1
        if i < 0:
            i = pos
        out.append((ms, i, len(seg)))
        pos = i + len(seg)
    return out


def _synth(text: str, voice: str, rate: float, mp3: Path, meta: Path) -> None:
    """在后台线程里跑：一次 stream() 同时收音频和词边界。"""
    import edge_tts

    async def go():
        c = edge_tts.Communicate(
            text, voice, rate=rate_arg(rate), boundary="WordBoundary",
            connect_timeout=4, receive_timeout=20)
        buf = bytearray()
        words = []
        async for chunk in c.stream():
            kind = chunk.get("type")
            if kind == "audio":
                buf.extend(chunk["data"])
            elif kind == "WordBoundary":
                words.append([chunk.get("offset", 0),
                              chunk.get("duration", 0),
                              chunk.get("text", "")])
        return bytes(buf), words

    data, words = asyncio.run(go())
    if len(data) < 1000:
        raise RuntimeError("NoAudioReceived")
    tmp = mp3.parent / (mp3.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(mp3)
    meta.write_text(json.dumps({"text": text, "words": words},
                               ensure_ascii=False), encoding="utf-8")


class _SynthWorker(QThread):
    done = pyqtSignal(int, str, str, str)   # idx, mp3, meta, err

    def __init__(self, idx, text, voice, rate, mp3, meta, parent=None):
        super().__init__(parent)
        self.idx, self.text, self.voice, self.rate = idx, text, voice, rate
        self.mp3, self.meta = mp3, meta

    def run(self):
        try:
            _synth(self.text, self.voice, self.rate, self.mp3, self.meta)
            self.done.emit(self.idx, str(self.mp3), str(self.meta), "")
        except Exception as e:
            self.done.emit(self.idx, "", "", f"{type(e).__name__}: {e}")


class EdgeEngine(QObject):
    """逐句播 MP3。信号与 tts.SpeechReader 一致，另加 failed 供控制器降级。"""

    sentence_changed = pyqtSignal(int)
    word_spoken = pyqtSignal(int, int, int)
    finished = pyqtSignal()
    state_changed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._voice = EDGE_VOICES[0][1]
        self._rate = 0.0
        self._volume = 1.0
        self._device_desc = ""      # 空 = 跟随系统默认输出设备
        self._sentences: list[str] = []
        self._idx = -1
        self._next = 0
        self._running = False
        self._waiting = -1            # 正在等哪一句合成完
        self._start_idx = 0
        self._start_char = 0
        self._trunc_idx = -1
        self._trunc_char = 0
        self._last_word_start = 0
        self._last_word_key = -1
        self._bounds: list[tuple[float, int, int]] = []
        self._mp3_now = ""
        self._play_pending = False
        self._seek_ms = 0
        self._resume = None           # 上次停下的快照，供「续」零延迟续播
        self._cache: dict[int, tuple[str, str]] = {}
        self._inflight: set[int] = set()
        self._workers: list[_SynthWorker] = []
        self._key = ""

        try:
            self._cache_dir = store.app_dir() / CACHE_DIR_NAME
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self._cache_dir = None

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setVolume(self._volume)
        self._player.setAudioOutput(self._audio)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.positionChanged.connect(self._on_position)
        self._player.errorOccurred.connect(self._on_player_error)

    # -------------------------------------------------- 配置

    def available_voices(self) -> list[str]:
        return [v for _, v in EDGE_VOICES]

    def set_voice(self, short: str) -> bool:
        if short and short in self.available_voices():
            self._voice = short
            return True
        return False

    def current_voice(self) -> str:
        return self._voice

    def set_rate(self, rate: float) -> None:
        self._rate = max(-1.0, min(1.0, float(rate)))

    def set_volume(self, vol: float) -> None:
        self._volume = max(0.0, min(1.0, float(vol)))
        self._audio.setVolume(self._volume)

    def set_device(self, desc: str) -> bool:
        """指定音频输出设备，空串 = 跟随系统默认。

        QMediaPlayer 只认系统默认输出设备；如果默认是没连上的蓝牙耳机，
        音频照样「播完」但人听不到。设置里允许手动指到正在用的扬声器。
        """
        desc = desc or ""
        if desc == self._device_desc:
            return True
        if desc:
            dev = next((d for d in QMediaDevices.audioOutputs()
                        if d.description() == desc), None)
            if dev is None:
                return False       # 设备被拔了，保留原设置，别把声音弄丢
        else:
            dev = QMediaDevices.defaultAudioOutput()
        try:
            self._audio.setDevice(dev)
            self._device_desc = desc
            return True
        except Exception:
            return False

    def rebuild(self) -> None:
        # MP3 播放不依赖长期存活的语音会话，没有 winrt 那种僵死问题
        pass

    # -------------------------------------------------- 状态

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return False        # 暂停语义由控制器统一实现

    @property
    def current_index(self) -> int:
        return self._idx

    def stop_at(self) -> tuple[int, int]:
        return max(0, self._idx), max(0, self._last_word_start)

    # -------------------------------------------------- 播放

    def start(self, sentences: list[str], from_idx: int = 0,
              from_char: int = 0) -> None:
        # 先把「停」时记下的快照捞出来：下面 stop() 会清状态，
        # 直接让 stop(keep_resume=True) 在这里覆盖快照会让「续」永远从头播。
        prev_resume = self._resume
        self.stop(keep_resume=False)
        self._sentences = [s for s in sentences if s.strip()]
        if not self._sentences:
            return
        self._start_idx = max(0, min(from_idx, len(self._sentences) - 1))
        self._start_char = max(0, from_char)
        self._idx = self._start_idx
        self._next = self._start_idx
        self._waiting = self._start_idx
        self._running = True
        self._last_word_start = 0
        self._key = hashlib.sha1(
            ("\n".join(self._sentences) + "|" + self._voice + "|" +
             rate_arg(self._rate)).encode("utf-8", "ignore")).hexdigest()

        # 快路径：同一章、同一句、同一音色，音频还在 → 直接从停下的毫秒继续，
        # 不重新合成、不走网络，也就没有「停几小时后不能续」这回事。
        r = prev_resume
        if (r and r.get("key") == self._key and r.get("idx") == self._start_idx
                and Path(r.get("mp3", "")).exists()
                and Path(r.get("meta", "")).exists()):
            self._trunc_idx = -1
            self._trunc_char = 0
            self._cache[self._start_idx] = (r["mp3"], r["meta"])
            self._play(self._start_idx, r["mp3"], r["meta"],
                       seek_ms=int(r.get("ms", 0)), truncated=False)
            return

        self._trunc_idx = self._start_idx if self._start_char else -1
        self._trunc_char = self._start_char
        text = self._sentences[self._start_idx]
        if self._start_char:
            text = text[self._start_char:]
        self._request(self._start_idx, text)

    def _request(self, idx: int, text: str) -> None:
        if not (0 <= idx < len(self._sentences)) or not text.strip():
            return
        if self._cache_dir is None:
            self._on_synth(idx, "", "", "no cache dir")
            return
        key = cache_key(text, self._voice, self._rate)
        mp3 = self._cache_dir / f"{key}.mp3"
        meta = self._cache_dir / f"{key}.json"
        if mp3.exists() and mp3.stat().st_size > 1000 and meta.exists():
            # 缓存命中：绕一圈回主线程，和后台完成的路径保持一致
            QTimer.singleShot(0, lambda: self._on_synth(idx, str(mp3), str(meta), ""))
            return
        if idx in self._inflight:
            return
        self._inflight.add(idx)
        w = _SynthWorker(idx, text, self._voice, self._rate, mp3, meta, self)
        w.done.connect(self._on_synth)
        w.finished.connect(lambda obj=w: self._drop_worker(obj))
        self._workers.append(w)
        w.start()
        QTimer.singleShot(SYNTH_TIMEOUT_MS, lambda: self._synth_timeout(idx))

    def _drop_worker(self, w):
        try:
            self._workers.remove(w)
        except ValueError:
            pass
        w.deleteLater()

    def _synth_timeout(self, idx: int):
        if idx in self._inflight:
            self._inflight.discard(idx)
            self._on_synth(idx, "", "", "timeout")

    def _on_synth(self, idx: int, mp3: str, meta: str, err: str) -> None:
        self._inflight.discard(idx)
        if err:
            if idx == self._waiting and self._running:
                self._running = False
                self.failed.emit(err)
            return
        self._cache[idx] = (mp3, meta)
        if idx == self._waiting and self._running:
            self._play(idx, mp3, meta,
                       truncated=(idx == self._trunc_idx))

    def _play(self, idx: int, mp3: str, meta: str,
              seek_ms: int = 0, truncated: bool = False) -> None:
        if not self._running or not (0 <= idx < len(self._sentences)):
            return
        self._idx = idx
        self._next = idx + 1
        self._waiting = -1
        self._mp3_now = mp3
        self._last_word_start = 0
        self._last_word_key = -1
        self._bounds = []
        try:
            data = json.loads(Path(meta).read_text(encoding="utf-8"))
            self._bounds = map_words(data.get("text", ""),
                                     data.get("words", []))
        except Exception:
            pass
        if truncated and self._trunc_char:
            base = self._trunc_char
            self._bounds = [(ms, i + base, n) for ms, i, n in self._bounds]

        # 播当前句时把下一句先合成好，句间不留 1 秒空白
        if self._next < len(self._sentences):
            self._request(self._next, self._sentences[self._next])

        self.sentence_changed.emit(idx)
        self._seek_ms = max(0, int(seek_ms))
        self._play_pending = True
        self._player.setSource(QUrl.fromLocalFile(mp3))

    def _on_media_status(self, status) -> None:
        v = getattr(status, "name", str(status))
        if v in ("LoadedMedia", "BufferedMedia") and self._play_pending:
            self._play_pending = False
            if self._seek_ms:
                self._player.setPosition(self._seek_ms)
            self._player.play()
            self.state_changed.emit("speaking")
        elif v == "EndOfMedia":
            self._on_end_of_media()

    def _on_end_of_media(self) -> None:
        if not self._running:
            return
        self._resume = None
        if self._next >= len(self._sentences):
            self._finish()
            return
        idx = self._next
        self._waiting = idx
        hit = self._cache.get(idx)
        if hit and Path(hit[0]).exists():
            self._play(idx, hit[0], hit[1])
        else:
            # 预取还没回来（或失败了重试一次）
            self._request(idx, self._sentences[idx])

    def _on_position(self, ms: int) -> None:
        if not self._running or self._idx < 0 or not self._bounds:
            return
        hit = -1
        for k, (start_ms, _i, _n) in enumerate(self._bounds):
            if start_ms <= ms:
                hit = k
            else:
                break
        if hit < 0 or hit == self._last_word_key:
            return
        self._last_word_key = hit
        _ms, i, n = self._bounds[hit]
        self._last_word_start = i
        self.word_spoken.emit(self._idx, i, max(1, n))

    def _on_player_error(self, *args) -> None:
        if self._running:
            self._running = False
            self.failed.emit("player: " + str(args)[:120])

    def _finish(self) -> None:
        self._running = False
        self._idx = -1
        self.state_changed.emit("ready")
        self.finished.emit()

    def stop(self, keep_resume: bool = True) -> None:
        # 先取快照：stop() 会同步 emit positionChanged(0)
        if keep_resume and self._running and self._idx >= 0 and self._mp3_now:
            self._resume = {
                "key": self._key, "idx": self._idx,
                "mp3": self._mp3_now,
                "meta": self._cache.get(self._idx, ("", ""))[1],
                "ms": self._player.position(),
            }
        else:
            self._resume = None
        self._play_pending = False
        self._seek_ms = 0
        self._running = False
        self._waiting = -1
        self._idx = -1
        try:
            self._player.stop()
            self._player.setSource(QUrl())
        except Exception:
            pass

    def skip(self, delta: int) -> None:
        if not self._running:
            return
        target = max(0, min(self._idx + delta, len(self._sentences) - 1))
        self._play_pending = False
        self._player.stop()
        self._waiting = target
        hit = self._cache.get(target)
        if hit and Path(hit[0]).exists():
            self._play(target, hit[0], hit[1])
        else:
            self._request(target, self._sentences[target])
