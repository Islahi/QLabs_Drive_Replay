"""Separate synchronized video window for recorded drive sessions."""

from __future__ import annotations

from bisect import bisect_right
import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.replay_core import format_time_s


class VideoWindow(QMainWindow):
    """Video-only second screen synchronized to session time.

    Multiple recorded video sources can be registered for a session. Switching
    between QLabs Front (OBS), Left, Right, and Rear preserves the current
    experiment time.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Drive Replay Video")
        self.resize(960, 640)

        self.video_path: Path | None = None
        self.offset_s = 0.0
        self._session_time_s = 0.0
        self._playing = False
        self._rate = 1.0
        self._last_resync_wall = 0.0
        self._fullscreen = False
        self._force_next_sync = True
        self._sources: dict[str, dict] = {}
        self._current_source_key: str | None = None
        self._sync_points: list[tuple[float, float]] = []
        self._sync_session_times: list[float] = []

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video_widget = QVideoWidget(self)
        self.player.setVideoOutput(self.video_widget)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)

        header = QHBoxLayout()
        layout.addLayout(header)
        header.addWidget(QLabel("Camera:"))
        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(220)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        header.addWidget(self.source_combo)
        self.source_status = QLabel("No session videos")
        self.source_status.setWordWrap(True)
        header.addWidget(self.source_status, 1)

        layout.addWidget(self.video_widget, 1)

        footer = QHBoxLayout()
        layout.addLayout(footer)
        self.time_label = QLabel("No video loaded")
        footer.addWidget(self.time_label, 1)

        self.next_screen_button = QPushButton("Move to Next Screen")
        self.next_screen_button.clicked.connect(self.move_to_next_screen)
        footer.addWidget(self.next_screen_button)

        self.fullscreen_button = QPushButton("Full Screen")
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        footer.addWidget(self.fullscreen_button)

        self.player.errorOccurred.connect(self._on_player_error)

    def set_sources(self, sources: dict[str, dict], preferred_key: str | None = None) -> None:
        """Replace the available session video sources.

        Each source dict accepts: label, path, offset_s, sync_points.
        sync_points are (session_time_s, video_time_s) pairs.
        """
        was_playing = self._playing
        self.player.pause()
        self._sources = dict(sources)
        self.source_combo.blockSignals(True)
        self.source_combo.clear()

        keys = list(self._sources)
        for key in keys:
            self.source_combo.addItem(str(self._sources[key].get("label", key)), key)

        if not keys:
            self.source_combo.blockSignals(False)
            self._current_source_key = None
            self.video_path = None
            self._sync_points = []
            self._sync_session_times = []
            self.player.setSource(QUrl())
            self.source_status.setText("No video files found for this session.")
            self.time_label.setText("No video loaded")
            return

        if preferred_key not in self._sources:
            preferred_key = keys[0]
        index = self.source_combo.findData(preferred_key)
        self.source_combo.setCurrentIndex(max(0, index))
        selected_key = str(self.source_combo.currentData())
        self.source_combo.blockSignals(False)
        self._load_source(selected_key)
        if was_playing:
            self.set_playing(True)

    def add_manual_video(self, path: Path | str, label: str = "Manual video", offset_s: float = 0.0) -> None:
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Video not found: {path}")
        sources = dict(self._sources)
        sources["manual"] = {
            "label": label,
            "path": path,
            "offset_s": float(offset_s),
            "sync_points": [],
        }
        self.set_sources(sources, preferred_key="manual")

    def available_source_count(self) -> int:
        return len(self._sources)

    def current_source_key(self) -> str | None:
        return self._current_source_key

    def load_video(self, path: Path | str) -> None:
        """Backward-compatible manual-video method."""
        self.add_manual_video(path, offset_s=self.offset_s)

    def _on_source_changed(self, _index: int) -> None:
        key = self.source_combo.currentData()
        if key:
            self._load_source(str(key))

    def _load_source(self, key: str) -> None:
        source = self._sources.get(key)
        if source is None:
            return
        path = Path(source["path"]).expanduser().resolve()
        if not path.is_file():
            self.source_status.setText(f"Missing video: {path}")
            return

        was_playing = self._playing
        self.player.pause()
        self._current_source_key = key
        self.video_path = path
        self.offset_s = float(source.get("offset_s", 0.0))
        self._sync_points = [
            (float(a), float(b)) for a, b in source.get("sync_points", [])
        ]
        self._sync_points.sort(key=lambda item: item[0])
        self._sync_session_times = [point[0] for point in self._sync_points]
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.setPlaybackRate(self._rate)
        self.force_sync()
        self.set_session_time(self._session_time_s)
        self.source_status.setText(str(source.get("label", key)))
        if was_playing:
            self.player.play()

    def set_offset_s(self, offset_s: float) -> None:
        # Manual fine adjustment. For synchronized sessions this is added after
        # the recorded sync mapping rather than replacing it.
        self.offset_s = float(offset_s)
        if self._current_source_key and self._current_source_key in self._sources:
            self._sources[self._current_source_key]["offset_s"] = self.offset_s
        self.force_sync()
        if self.video_path is not None:
            self.set_session_time(self._session_time_s)

    def set_session_time(self, session_time_s: float) -> None:
        self._session_time_s = max(0.0, float(session_time_s))
        if self.video_path is None:
            return

        desired = self._desired_position_ms(self._session_time_s)
        actual = self.player.position()
        now = time.monotonic()

        if self._force_next_sync or not self._playing:
            if self._force_next_sync or abs(actual - desired) > 25:
                self.player.setPosition(desired)
            self._force_next_sync = False
        elif (
            abs(actual - desired) > 1000
            or (abs(actual - desired) > 180 and now - self._last_resync_wall >= 0.5)
        ):
            self.player.setPosition(desired)
            self._last_resync_wall = now

        video_time_s = desired / 1000.0
        self.time_label.setText(
            f"{self.video_path.name} — session {format_time_s(self._session_time_s)} "
            f"| video {format_time_s(video_time_s)}"
        )

    def set_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        if self.video_path is None:
            return
        self.force_sync()
        self.set_session_time(self._session_time_s)
        if self._playing:
            self.player.play()
        else:
            self.player.pause()

    def set_rate(self, rate: float) -> None:
        self._rate = float(rate)
        self.player.setPlaybackRate(self._rate)

    def force_sync(self) -> None:
        self._force_next_sync = True

    def _mapped_video_time_s(self, session_time_s: float) -> float:
        t = float(session_time_s)
        points = self._sync_points
        if not points:
            return t + self.offset_s
        if len(points) == 1:
            return points[0][1] + (t - points[0][0]) + self.offset_s

        if t <= points[0][0]:
            a, b = points[0], points[1]
        elif t >= points[-1][0]:
            a, b = points[-2], points[-1]
        else:
            right = bisect_right(self._sync_session_times, t)
            a, b = points[right - 1], points[right]

        ds = b[0] - a[0]
        if abs(ds) < 1e-12:
            mapped = a[1]
        else:
            alpha = (t - a[0]) / ds
            mapped = a[1] + alpha * (b[1] - a[1])
        return mapped + self.offset_s

    def _desired_position_ms(self, session_time_s: float) -> int:
        desired = max(0, int(round(self._mapped_video_time_s(session_time_s) * 1000.0)))
        duration = self.player.duration()
        if duration > 0:
            desired = min(desired, duration)
        return desired

    def _on_player_error(self, _error, error_string: str) -> None:
        self.time_label.setText(f"Video error: {error_string}")

    def move_to_next_screen(self) -> None:
        screens = QGuiApplication.screens()
        if len(screens) <= 1:
            self.time_label.setText("Only one display is currently detected.")
            return
        current = self.screen()
        try:
            index = screens.index(current)
        except ValueError:
            index = 0
        target = screens[(index + 1) % len(screens)]
        self.showNormal()
        geometry = target.availableGeometry()
        self.setGeometry(geometry)
        self.showMaximized()

    def toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        if self._fullscreen:
            self.showFullScreen()
            self.fullscreen_button.setText("Exit Full Screen")
        else:
            self.showNormal()
            self.fullscreen_button.setText("Full Screen")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self._fullscreen:
            self.toggle_fullscreen()
            return
        super().keyPressEvent(event)
