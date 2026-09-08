"""Concrete ReplaySink adapters for map and video outputs."""

from __future__ import annotations

from pathlib import Path

from core.oop_interfaces import ReplaySink
from core.replay_core import PoseSample, SessionData


class MapReplaySink(ReplaySink):
    """Adapt ReplayMap to the ReplaySink interface."""

    def __init__(self, map_widget) -> None:
        self._map_widget = map_widget

    @property
    def name(self) -> str:
        return "Map"

    def set_session(self, session: SessionData | None) -> None:
        self._map_widget.set_session(session)

    def update_time(self, time_s: float, pose: PoseSample) -> None:
        self._map_widget.set_current_pose(pose)

    def set_playing(self, playing: bool) -> None:
        pass

    def set_rate(self, rate: float) -> None:
        pass

    def close(self) -> None:
        pass


class VideoReplaySink(ReplaySink):
    """Adapt the separate VideoWindow to the common replay interface."""

    def __init__(self, video_window) -> None:
        self._window = video_window

    @property
    def name(self) -> str:
        return "Video"

    @property
    def window(self):
        return self._window

    def load_video(self, path: Path | str) -> None:
        self._window.load_video(path)

    def set_offset_s(self, offset_s: float) -> None:
        self._window.set_offset_s(offset_s)

    def set_session(self, session: SessionData | None) -> None:
        # A session may or may not have a video. Loading is intentionally kept
        # explicit in the UI, but time synchronization is generic.
        pass

    def update_time(self, time_s: float, pose: PoseSample) -> None:
        self._window.set_session_time(time_s)

    def set_playing(self, playing: bool) -> None:
        self._window.set_playing(playing)

    def set_rate(self, rate: float) -> None:
        self._window.set_rate(rate)

    def force_sync(self) -> None:
        self._window.force_sync()

    def close(self) -> None:
        self._window.close()
