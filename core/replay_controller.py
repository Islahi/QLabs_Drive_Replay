"""Application controller coordinating the master replay clock and sinks."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.oop_interfaces import ReplaySink
from core.replay_core import PoseSample, SessionData
from core.replay_clock import ReplayClock


class ReplayCoordinator(QObject):
    """Synchronize any number of ReplaySink implementations polymorphically.

    ReplayWindow no longer needs to know how map, video, and QLabs update
    themselves. It manipulates the master clock and the coordinator broadcasts
    state through the ReplaySink abstraction.
    """

    poseChanged = Signal(float, object)  # time_s, PoseSample
    sessionChanged = Signal(object)      # SessionData | None
    sinkError = Signal(str, str)         # sink name, error message

    def __init__(self, clock: ReplayClock, parent=None) -> None:
        super().__init__(parent)
        self.clock = clock
        self.session: SessionData | None = None
        self._sinks: list[ReplaySink] = []

        self.clock.timeChanged.connect(self._on_time_changed)
        self.clock.playingChanged.connect(self._on_playing_changed)
        self.clock.rateChanged.connect(self._on_rate_changed)

    @property
    def sinks(self) -> tuple[ReplaySink, ...]:
        return tuple(self._sinks)

    def add_sink(self, sink: ReplaySink) -> None:
        if sink in self._sinks:
            return
        self._sinks.append(sink)
        try:
            sink.set_session(self.session)
            sink.set_playing(self.clock.playing)
            sink.set_rate(self.clock.rate)
        except Exception as exc:
            self.sinkError.emit(sink.name, str(exc))

    def remove_sink(self, sink: ReplaySink, close: bool = False) -> None:
        if sink not in self._sinks:
            return
        self._sinks.remove(sink)
        if close:
            try:
                sink.close()
            except Exception as exc:
                self.sinkError.emit(sink.name, str(exc))

    def set_session(self, session: SessionData | None) -> None:
        self.clock.pause()
        self.session = session

        for sink in self._sinks:
            try:
                sink.set_session(session)
            except Exception as exc:
                self.sinkError.emit(sink.name, str(exc))

        duration = 0.0 if session is None else session.duration_s
        self.clock.set_duration(duration)
        self.clock.seek(0.0)
        self.sessionChanged.emit(session)

    def force_sync(self) -> None:
        for sink in self._sinks:
            try:
                sink.force_sync()
            except Exception as exc:
                self.sinkError.emit(sink.name, str(exc))

    def sync_now(self) -> None:
        """Synchronize every sink immediately at the current master time."""
        self.force_sync()
        self._on_time_changed(self.clock.current_time_s)

    def current_pose(self) -> PoseSample | None:
        if self.session is None:
            return None
        return self.session.pose_at(self.clock.current_time_s)

    def _on_time_changed(self, time_s: float) -> None:
        if self.session is None:
            return
        pose = self.session.pose_at(time_s)
        for sink in tuple(self._sinks):
            try:
                sink.update_time(time_s, pose)
            except Exception as exc:
                self.sinkError.emit(sink.name, str(exc))
        self.poseChanged.emit(time_s, pose)

    def _on_playing_changed(self, playing: bool) -> None:
        for sink in tuple(self._sinks):
            try:
                sink.set_playing(playing)
            except Exception as exc:
                self.sinkError.emit(sink.name, str(exc))

    def _on_rate_changed(self, rate: float) -> None:
        for sink in tuple(self._sinks):
            try:
                sink.set_rate(rate)
            except Exception as exc:
                self.sinkError.emit(sink.name, str(exc))

    def close(self) -> None:
        self.clock.pause()
        for sink in reversed(self._sinks):
            try:
                sink.close()
            except Exception as exc:
                self.sinkError.emit(sink.name, str(exc))
