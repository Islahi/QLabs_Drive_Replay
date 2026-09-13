"""Concrete ReplaySink adapters for map and video outputs."""

from __future__ import annotations

import csv
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


def _resolve_video_path(folder: Path, metadata: dict) -> Path | None:
    raw = metadata.get("path") or metadata.get("file")
    if not raw:
        return None
    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        candidate = folder / candidate
    if candidate.is_file():
        return candidate.resolve()

    # If an OBS video was copied into the session folder after recording, find
    # it by basename without requiring session.json to be edited.
    basename = Path(str(raw)).name
    fallback = folder / basename
    if fallback.is_file():
        return fallback.resolve()
    return None


def _read_sync_points(path: Path, x_column: str, y_column: str) -> list[tuple[float, float]]:
    if not path.is_file():
        return []
    points: list[tuple[float, float]] = []
    last_kept_x = -1e99
    pending_last: tuple[float, float] | None = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if x_column not in (reader.fieldnames or []) or y_column not in (reader.fieldnames or []):
            return []
        for row in reader:
            try:
                x = float(row[x_column])
                y = float(row[y_column])
            except (TypeError, ValueError):
                continue
            pending_last = (x, y)
            # One point per ~0.5 s is ample for clock-drift interpolation and
            # avoids loading hundreds of thousands of camera frame rows.
            if not points or x - last_kept_x >= 0.5:
                points.append((x, y))
                last_kept_x = x
    if pending_last is not None and (not points or pending_last != points[-1]):
        points.append(pending_last)
    return points


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
        if session is None:
            self._window.set_sources({})
            return

        sources: dict[str, dict] = {}
        preferred_key: str | None = None
        videos = session.metadata.get("videos", {})
        if isinstance(videos, dict):
            for key, metadata in videos.items():
                if not isinstance(metadata, dict):
                    continue
                path = _resolve_video_path(session.folder, metadata)
                if path is None:
                    continue

                sync_points: list[tuple[float, float]] = []
                sync_file = metadata.get("sync_file")
                frame_file = metadata.get("frame_timestamps_file")
                if sync_file:
                    sync_points = _read_sync_points(
                        session.folder / str(sync_file),
                        "session_time_s",
                        "obs_duration_s",
                    )
                elif frame_file:
                    sync_points = _read_sync_points(
                        session.folder / str(frame_file),
                        "session_time_s",
                        "video_time_s",
                    )

                analysis_origin_s = float(getattr(session, "analysis_start_s", 0.0))
                if sync_points and analysis_origin_s > 0.0:
                    # Sync CSV uses raw session time; replay clock uses trimmed
                    # analysis time. Shift only the sync X-axis, never video time.
                    sync_points = [
                        (session_t - analysis_origin_s, video_t)
                        for session_t, video_t in sync_points
                    ]
                base_offset = float(metadata.get("offset_s", 0.0))
                if not sync_points:
                    # Without a measured sync curve, raw video t equals raw
                    # session t, so the non-destructive trim is a simple offset.
                    base_offset += analysis_origin_s

                sources[str(key)] = {
                    "label": str(metadata.get("label", key)),
                    "path": path,
                    "offset_s": base_offset,
                    "sync_points": sync_points,
                }
                if metadata.get("preferred"):
                    preferred_key = str(key)

        # Backward compatibility with the project's previous single-video
        # session metadata.
        legacy = session.metadata.get("video")
        if not sources and isinstance(legacy, dict) and legacy.get("file"):
            path = _resolve_video_path(session.folder, legacy)
            if path is not None:
                sources["legacy"] = {
                    "label": "Session video",
                    "path": path,
                    "offset_s": float(legacy.get("offset_s", 0.0)) + float(getattr(session, "analysis_start_s", 0.0)),
                    "sync_points": [],
                }
                preferred_key = "legacy"

        if preferred_key is None and "qlabs_front" in sources:
            preferred_key = "qlabs_front"
        self._window.set_sources(sources, preferred_key=preferred_key)

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
