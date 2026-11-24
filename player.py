# -*- coding: utf-8 -*-
"""播放器封装：QtMultimedia 的 QMediaPlayer + QAudioOutput。

只负责「放歌」这件事，不碰界面。界面通过信号拿到状态。
"""

from __future__ import annotations

import os
import random

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


class Player(QObject):
    """一条播放队列 + 单曲播放控制。"""

    trackChanged = Signal(int)          # 当前曲目下标，-1 表示没有
    stateChanged = Signal(str)          # "playing" / "paused" / "stopped"
    positionChanged = Signal(int)       # 毫秒
    durationChanged = Signal(int)       # 毫秒
    volumeChanged = Signal(int)         # 0-100
    failed = Signal(str)                # 播放失败的人话原因
    finished = Signal()                 # 一首放完了

    def __init__(self, parent=None):
        super().__init__(parent)
        self._paths = []
        self._index = -1
        self._volume = 80
        self._shuffle = False
        self._history = []

        self.audio = QAudioOutput(self)
        self.audio.setVolume(self._volume / 100.0)

        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)

        # qlonglong 不能直接接到 Signal(int)，用 lambda 转一道
        self.player.positionChanged.connect(lambda ms: self.positionChanged.emit(int(ms)))
        self.player.durationChanged.connect(lambda ms: self.durationChanged.emit(int(ms)))
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)

    # ---------- 队列 ----------

    def set_playlist(self, paths):
        """整批替换播放列表（字符串路径）。"""
        self._paths = [os.fspath(p) for p in paths]
        if self._index >= len(self._paths):
            self._index = -1

    def playlist(self):
        return list(self._paths)

    def current_index(self):
        return self._index

    def current_path(self):
        if 0 <= self._index < len(self._paths):
            return self._paths[self._index]
        return ""

    def play_index(self, index):
        """从第 index 首开始播。下标越界时什么都不做。"""
        if not (0 <= index < len(self._paths)):
            return False
        self._index = index
        path = self._paths[index]
        if not os.path.exists(path):
            self.failed.emit("找不到文件：" + os.path.basename(path))
            return False
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self.trackChanged.emit(index)
        return True

    def play_file(self, path):
        """把当前曲目换成另一个文件继续播（同一首歌切换播放格式）。"""
        if not os.path.exists(path):
            self.failed.emit("找不到文件：" + os.path.basename(path))
            return False
        if 0 <= self._index < len(self._paths):
            self._paths[self._index] = os.fspath(path)
        self.player.setSource(QUrl.fromLocalFile(os.fspath(path)))
        self.player.play()
        return True

    def play(self):
        if self.player.source().isEmpty():
            if self._paths:
                return self.play_index(0 if self._index < 0 else self._index)
            return False
        self.player.play()
        return True

    def pause(self):
        self.player.pause()

    def stop(self):
        self.player.stop()

    def next(self, wrap=True):
        if not self._paths:
            return False
        if self._shuffle:
            return self._play_random()
        nxt = self._index + 1
        if nxt >= len(self._paths):
            if not wrap:
                return False
            nxt = 0
        return self.play_index(nxt)

    def prev(self):
        if not self._paths:
            return False
        if self._shuffle:
            if self._history:
                return self.play_index(self._history.pop())
            return self._play_random()
        prv = self._index - 1
        if prv < 0:
            prv = len(self._paths) - 1
        return self.play_index(prv)

    def _play_random(self):
        """随机挑一首和当前不同的；上一首留给 prev 回退。"""
        count = len(self._paths)
        if count <= 1:
            return self.play_index(0)
        if 0 <= self._index < count:
            self._history.append(self._index)
            del self._history[:-100]
        pick = self._index
        for _ in range(24):
            choice = random.randrange(count)
            if choice != self._index:
                pick = choice
                break
        return self.play_index(pick)

    # ---------- 播放顺序 ----------

    def set_shuffle(self, on):
        """True = 随机播放，False = 顺序播放。"""
        self._shuffle = bool(on)
        if not self._shuffle:
            self._history = []
        return self._shuffle

    def shuffle(self):
        return self._shuffle

    def toggle_shuffle(self):
        return self.set_shuffle(not self._shuffle)

    # ---------- 参数 ----------

    def seek(self, ms):
        self.player.setPosition(max(0, int(ms)))

    def position(self):
        return self.player.position()

    def duration(self):
        return self.player.duration()

    def is_playing(self):
        return self.player.playbackState() == QMediaPlayer.PlayingState

    def set_volume(self, percent):
        self._volume = max(0, min(100, int(percent)))
        self.audio.setVolume(self._volume / 100.0)
        self.volumeChanged.emit(self._volume)

    def volume(self):
        return self._volume

    # ---------- 内部 ----------

    def _on_state(self, state):
        if state == QMediaPlayer.PlayingState:
            self.stateChanged.emit("playing")
        elif state == QMediaPlayer.PausedState:
            self.stateChanged.emit("paused")
        else:
            self.stateChanged.emit("stopped")

    def _on_status(self, status):
        if status == QMediaPlayer.EndOfMedia:
            self.finished.emit()

    def _on_error(self, _error, message):
        msg = (message or "").strip() or "这个格式放不了"
        self.failed.emit(msg)