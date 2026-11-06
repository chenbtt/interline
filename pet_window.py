"""奶蛙悬浮窗 —— 思维上等同于音乐软件的「桌面歌词」。

主窗口（阅读器）才是主体：它照常显示正文、切书库、翻目录、改设置。
奶蛙是浮在桌面上的**另一个**独立小窗，只做三件事：

  1. 显示当前这一句（头顶气泡）
  2. 跟着朗读状态换动作（播 / 停 / 空闲）
  3. 接住鼠标：拖走、点一下、滚轮翻句、右键菜单

它不认识 Reader、不加载书、不推进句子 —— 所有业务动作都以信号抛回主窗，
正如歌词窗不会自己去切歌。这样阅读器和奶蛙互不干扰：点书库、换主题、
最小化主窗，蛙都不受影响（只是跟着一起藏）。

窗口属性按悬浮窗的规矩来：
  Qt.Tool           不进任务栏、不被主窗遮挡管理
  Frameless         无边框
  WA_Translucent     真透明（蛙是抠图 PNG，气泡自绘）
  WA_ShowWithoutActivating  弹出时不抢主窗焦点
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

import pet
import pet_audio

DRAG_SLOP = 4          # 按下后移动超过这么远才算拖拽，否则当点击
BUBBLE_PAD = 2         # 气泡尾巴尖到蛙头的空隙（尾巴本身另算）
SCREEN_INSET = 40      # 首次出现时离屏幕右下角的距离


class PetWindow(QWidget):
    # ---- 交给主窗处理的业务动作 ----
    toggle_speech = pyqtSignal()
    bubble_clicked = pyqtSignal()
    step = pyqtSignal(int)
    request_open = pyqtSignal()
    request_library = pyqtSignal()
    request_settings = pyqtSignal()
    exit_pet = pyqtSignal()
    quit_app = pyqtSignal()
    sound_started = pyqtSignal(int)     # 音效时长，让主窗压低朗读
    moved = pyqtSignal(int, int)        # 拖完存位置
    notice = pyqtSignal(str)

    def __init__(self, st, font_factory, speech_state):
        """st 是共享配置；font_factory() 给气泡字体；speech_state() 给菜单文案。"""
        super().__init__(None)
        self.st = st
        self._font_factory = font_factory
        self._speech_state = speech_state

        self.sprite = pet.PetSprite()
        self.state = pet.PetState(self.sprite)
        self._audio: pet_audio.PetAudio | None = None
        self._text = ""
        self._drag = None
        self._last_ms = 0.0
        self._idle_ms = 0.0

        self.setWindowFlags(Qt.WindowType.Tool
                            | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("奶蛙")
        self.sync_on_top(bool(self.st.get("always_on_top", True)))

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    # -------------------------------------------------- 生命周期

    def start(self, text: str) -> bool:
        """从主窗点亮：贴到上次的位置，开始动。素材缺失返回 False。"""
        if not self.sprite.load():
            return False
        self.sprite.set_scale(float(self.st.get("pet.scale", 0.9)))
        self.sync_on_top(bool(self.st.get("always_on_top", True)))
        self._text = text or ""
        self._place(first=True)
        self.show()
        self.state.reset()
        self._last_ms = time.monotonic() * 1000.0
        self._idle_ms = 0.0
        self._timer.start()
        self.fx(pet.WAVING)
        return True

    def stop(self):
        self._timer.stop()
        if self._audio is not None:
            self._audio.stop()
        self.state.reset()
        self._drag = None
        self.save_pos()
        self.hide()

    def suspend(self):
        """老板键（主窗隐藏）：别在看不见的时候烧 CPU 或出声。"""
        self._timer.stop()
        if self._audio is not None:
            self._audio.stop()
        self.hide()

    def resume(self):
        if not self.isVisible():
            self.show()
        self._last_ms = time.monotonic() * 1000.0
        if not self._timer.isActive():
            self._timer.start()

    def save_pos(self):
        g = self.geometry()
        self.st.set("pet.x", g.x())
        self.st.set("pet.y", g.y())

    # -------------------------------------------------- 对外设置

    def set_text(self, text: str):
        text = text or ""
        if text == self._text:
            return
        self._text = text
        self._place(first=False)
        self.update()

    def set_base(self, name: str):
        self.state.set_base(name)
        self.update()

    def fx(self, name: str):
        """瞬时动作 + 音效：时长由音频反推，没有音频就按帧数估。"""
        clip, cooldown = "", 0
        if name == pet.JUMPING:
            clip, cooldown = pet_audio.LAUGH, 2000
        elif name in (pet.WAVING, pet.IDLE):
            clip = pet_audio.PetAudio.random_voice()

        hold = 0.0
        if clip:
            audio = self._audio_obj()
            if audio is not None and audio.play(clip, cooldown_ms=cooldown):
                hold = float(audio.duration(clip))
                self.sound_started.emit(int(hold))
        if hold <= 0:
            n = len(self.sprite.frames(name)) or 4
            hold = 1000.0 / max(1, self.sprite.fps(name)) * n * 3

        self.state.play(name, hold)
        self.update()

    def play_direction(self, name: str, hold: int = 620):
        self.state.play(name, hold)
        self.update()

    def review(self):
        """读完了：抬头等翻页。"""
        self.state.set_base(pet.IDLE)
        self.state.play(pet.REVIEW, 3000)
        self.update()

    def preview_sound(self):
        audio = self._audio_obj()
        if audio is not None:
            audio.play(pet_audio.LAUGH)

    def apply_theme(self):
        self.update()

    def sync_on_top(self, on: bool):
        flags = self.windowFlags()
        if on:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        if flags == self.windowFlags():
            return
        was = self.isVisible()
        self.setWindowFlags(flags)
        # 改 flag 会把窗口藏起来，得自己叫回来
        if was:
            self.show()

    def _audio_obj(self) -> pet_audio.PetAudio | None:
        if self._audio is None:
            try:
                a = pet_audio.PetAudio(self.st, self)
                a.notice.connect(self.notice.emit)
                self._audio = a
            except Exception:
                self._audio = None
        return self._audio

    # -------------------------------------------------- 布局

    def _metrics(self):
        """(窗口尺寸, 气泡矩形, 蛙矩形)。气泡在上，蛙贴底居中。

        没书 / 关掉气泡时只剩蛙，返回 None 当气泡矩形 —— 跟手动隐藏一个样。
        """
        frog = self.sprite.size()
        if not bool(self.st.get("pet.show_bubble", True)) or not self._text:
            return (QSize(frog.width(), frog.height()), None,
                    QRect(0, 0, frog.width(), frog.height()))

        max_w = int(self.st.get("pet.bubble_w", 360))
        body = pet.measure_bubble(self._font_factory(), self._text, max_w)
        w = max(frog.width(), body.width())
        h = body.height() + pet.BUBBLE_TAIL + BUBBLE_PAD + frog.height()
        bubble = QRect((w - body.width()) // 2, 0, body.width(), body.height())
        frog_r = QRect((w - frog.width()) // 2, h - frog.height(),
                       frog.width(), frog.height())
        return QSize(w, h), bubble, frog_r

    def _place(self, first: bool):
        size, _bubble, _frog = self._metrics()
        if first:
            x = self.st.get("pet.x")
            y = self.st.get("pet.y")
            if (isinstance(x, int) and isinstance(y, int)
                    and any(s.availableGeometry().contains(x + 30, y + 30)
                            for s in QApplication.screens())):
                self.setFixedSize(size)
                self.move(x, y)
                return
            scr = QApplication.primaryScreen().availableGeometry()
            self.setFixedSize(size)
            self.move(scr.right() - size.width() - SCREEN_INSET,
                      scr.bottom() - size.height() - SCREEN_INSET - 8)
            return

        # 锚点用蛙脚：气泡多一行不能让它往上跳一下
        g = self.geometry()
        cx = g.x() + g.width() // 2
        by = g.y() + g.height()
        self.setFixedSize(size)
        self.move(cx - size.width() // 2, by - size.height())

    # -------------------------------------------------- 动画

    def _tick(self):
        now = time.monotonic() * 1000.0
        dt = now - self._last_ms if self._last_ms else 30.0
        self._last_ms = now
        dt = max(0.0, min(dt, 200.0))       # 休眠回来别一次跳几百帧

        before = (self.state.current, self.state.frame)
        self.state.tick(dt)
        if before != (self.state.current, self.state.frame):
            self.update()

        # 闲置久了嘀咕一句（默认关，怕吵）
        if self.state.current == pet.IDLE and not self.state.busy:
            self._idle_ms += dt
            if self._idle_ms > 120000 and bool(self.st.get("pet.idle_voice", False)):
                self._idle_ms = 0.0
                self.fx(pet.IDLE)
        else:
            self._idle_ms = 0.0

    # -------------------------------------------------- 绘制

    def paintEvent(self, ev):
        try:
            p = QPainter(self)
            try:
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                # Source 才清得出真透明：SourceOver 铺全透明等于没铺
                p.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_Source)
                p.fillRect(self.rect(), Qt.GlobalColor.transparent)
                p.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceOver)

                _size, bubble, frog = self._metrics()
                if bubble is not None and self._text:
                    pet.draw_bubble(p, bubble, self._font_factory(),
                                    self.st, self._text)
                frames = self.sprite.frames(self.state.current)
                if frames:
                    pet.draw_pet(p, frog,
                                 frames[min(self.state.frame, len(frames) - 1)])
            finally:
                p.end()
        except Exception:
            pass

    # -------------------------------------------------- 鼠标

    def _hit(self, pos) -> str:
        _size, bubble, frog = self._metrics()
        if bubble is not None:
            body = QRect(bubble.x(), bubble.y(), bubble.width(),
                         bubble.height() + pet.BUBBLE_TAIL)
            if body.contains(pos):
                return "bubble"
        if frog.contains(pos):
            return "frog"
        return ""

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            self._menu(ev.globalPosition().toPoint())
            ev.accept()
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        gp = ev.globalPosition().toPoint()
        self._drag = {"start": gp, "win": self.pos(),
                      "last_x": gp.x(), "moved": False}
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        ev.accept()

    def mouseMoveEvent(self, ev):
        gp = ev.globalPosition().toPoint()
        d = self._drag
        if d is None:
            hit = self._hit(ev.position().toPoint())
            self.setCursor(Qt.CursorShape.PointingHandCursor if hit == "bubble"
                           else Qt.CursorShape.OpenHandCursor if hit == "frog"
                           else Qt.CursorShape.ArrowCursor)
            return
        if not d["moved"] and (gp - d["start"]).manhattanLength() > DRAG_SLOP:
            d["moved"] = True
        if d["moved"]:
            self.move(d["win"] + (gp - d["start"]))
            dx = gp.x() - d["last_x"]
            if dx:
                # 拖拽独占：期间忽略一切状态触发，否则会和 running 互相踩
                self.state.lock(pet.RUN_LEFT if dx < 0 else pet.RUN_RIGHT)
                self.update()
        d["last_x"] = gp.x()
        ev.accept()

    def mouseReleaseEvent(self, ev):
        d = self._drag
        self._drag = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if d is not None and d["moved"]:
            self.state.unlock()
            self.save_pos()
            g = self.geometry()
            self.moved.emit(g.x(), g.y())
            self.update()
            ev.accept()
            return

        hit = self._hit(ev.position().toPoint())
        if hit == "frog":
            self.fx(pet.JUMPING)               # 摸它一下
        elif hit == "bubble":
            self.bubble_clicked.emit()
        ev.accept()

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if dy:
            self.step.emit(1 if dy < 0 else -1)
        ev.accept()

    def keyPressEvent(self, ev):
        key = ev.key()
        if key == Qt.Key.Key_Escape:
            self.exit_pet.emit()
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Right, Qt.Key.Key_Space):
            self.step.emit(1)
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Left):
            self.step.emit(-1)
            return
        super().keyPressEvent(ev)

    # -------------------------------------------------- 菜单

    def _menu(self, global_pos):
        menu = QMenu(self)
        show = bool(self.st.get("pet.show_bubble", True))
        menu.addAction("隐藏气泡" if show else "显示气泡").triggered.connect(
            self._toggle_bubble)

        label, enabled = self._speech_state()
        act = menu.addAction(label)
        act.setEnabled(enabled)
        act.triggered.connect(self.toggle_speech.emit)

        menu.addSeparator()
        menu.addAction("打开文件…").triggered.connect(self.request_open.emit)
        menu.addAction("书库…").triggered.connect(self.request_library.emit)
        menu.addAction("设置…").triggered.connect(self.request_settings.emit)
        menu.addSeparator()
        menu.addAction("收起奶蛙").triggered.connect(self.exit_pet.emit)
        menu.addAction("退出程序").triggered.connect(self.quit_app.emit)
        menu.exec(global_pos)

    def _toggle_bubble(self):
        self.st.set("pet.show_bubble", not bool(self.st.get("pet.show_bubble", True)))
        self.st.save_config()
        self._place(first=False)
        self.update()
