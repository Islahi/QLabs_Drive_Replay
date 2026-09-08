"""Separate-screen video window controlled by a ReplaySink adapter."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.replay_core import format_time_s


class VideoWindow(QMainWindow):
    """Video-only second-screen window.

    This class encapsulates media-player details. It no longer depends directly
    on ReplayClock; VideoReplaySink supplies session time, play state, and rate.
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

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video_widget = QVideoWidget(self)
        self.player.setVideoOutput(self.video_widget)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
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

    def load_video(self, path: Path | str) -> None:
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Video not found: {path}")
        self.video_path = path
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.setPlaybackRate(self._rate)
        self.force_sync()
        self.set_session_time(self._session_time_s)
        self.time_label.setText(f"{path.name} — {format_time_s(self._session_time_s)}")

    def set_offset_s(self, offset_s: float) -> None:
        self.offset_s = float(offset_s)
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

        self.time_label.setText(
            f"{self.video_path.name} — session {format_time_s(self._session_time_s)}"
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

    def _desired_position_ms(self, session_time_s: float) -> int:
        desired = max(0, int(round((float(session_time_s) + self.offset_s) * 1000.0)))
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
