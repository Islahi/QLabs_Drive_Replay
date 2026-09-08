"""Master monotonic replay clock."""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, QTimer, Signal


class ReplayClock(QObject):
    """Encapsulates all replay-time state and wall-clock anchoring."""

    timeChanged = Signal(float)
    playingChanged = Signal(bool)
    rateChanged = Signal(float)
    durationChanged = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.current_time_s = 0.0
        self.duration_s = 0.0
        self.playing = False
        self.rate = 1.0
        self._anchor_wall = time.perf_counter()
        self._anchor_time = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_duration(self, duration_s: float) -> None:
        self.duration_s = max(0.0, float(duration_s))
        self.current_time_s = min(self.current_time_s, self.duration_s)
        self._reanchor()
        self.durationChanged.emit(self.duration_s)
        self.timeChanged.emit(self.current_time_s)

    def seek(self, time_s: float) -> None:
        self.current_time_s = max(0.0, min(float(time_s), self.duration_s))
        self._reanchor()
        self.timeChanged.emit(self.current_time_s)

    def play(self) -> None:
        if self.duration_s <= 0:
            return
        if self.current_time_s >= self.duration_s:
            self.current_time_s = 0.0
            self.timeChanged.emit(self.current_time_s)
        if not self.playing:
            self.playing = True
            self._reanchor()
            self.playingChanged.emit(True)

    def pause(self) -> None:
        if self.playing:
            self._update_from_wall()
            # Publish the exact pause time before publishing the state change.
            self.timeChanged.emit(self.current_time_s)
            self.playing = False
            self.playingChanged.emit(False)

    def toggle(self) -> None:
        self.pause() if self.playing else self.play()

    def set_rate(self, rate: float) -> None:
        rate = max(0.05, min(float(rate), 8.0))
        if self.playing:
            self._update_from_wall()
            self.timeChanged.emit(self.current_time_s)
        self.rate = rate
        self._reanchor()
        self.rateChanged.emit(rate)

    def _reanchor(self) -> None:
        self._anchor_wall = time.perf_counter()
        self._anchor_time = self.current_time_s

    def _update_from_wall(self) -> None:
        if self.playing:
            self.current_time_s = self._anchor_time + (
                time.perf_counter() - self._anchor_wall
            ) * self.rate
            self.current_time_s = max(0.0, min(self.current_time_s, self.duration_s))

    def _tick(self) -> None:
        if not self.playing:
            return
        self._update_from_wall()
        if self.current_time_s >= self.duration_s:
            self.current_time_s = self.duration_s
            self.timeChanged.emit(self.current_time_s)
            self.playing = False
            self.playingChanged.emit(False)
            return
        self.timeChanged.emit(self.current_time_s)
