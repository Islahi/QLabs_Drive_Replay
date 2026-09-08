"""Separate-screen video window synchronized to a replay clock."""

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

from replay_core import format_time_s


class VideoWindow(QMainWindow):
    """A video-only window intended for a second display.

    The supplied clock is the master. Video follows the clock rather than
    becoming a separate timeline, which keeps the design ready for future LSL
    synchronization.
    """

    def __init__(self, replay_clock, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Drive Replay Video")
        self.resize(960, 640)

        self.clock = replay_clock
        self.video_path: Path | None = None
        self.offset_s = 0.0
        self._last_resync_wall = 0.0
        self._fullscreen = False

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

        self.clock.timeChanged.connect(self._on_clock_time)
        self.clock.playingChanged.connect(self._on_playing_changed)
        self.clock.rateChanged.connect(self._on_rate_changed)

        self.player.errorOccurred.connect(self._on_player_error)

    def load_video(self, path: Path | str) -> None:
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Video not found: {path}")
        self.video_path = path
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.setPlaybackRate(self.clock.rate)
        self._seek_exact(self.clock.current_time_s)
        self.time_label.setText(f"{path.name} — {format_time_s(self.clock.current_time_s)}")

    def set_offset_s(self, offset_s: float) -> None:
        # video position = replay session time + offset
        self.offset_s = float(offset_s)
        if self.video_path is not None:
            self._seek_exact(self.clock.current_time_s)

    def _desired_position_ms(self, session_time_s: float) -> int:
        return max(0, int(round((float(session_time_s) + self.offset_s) * 1000.0)))

    def _seek_exact(self, session_time_s: float) -> None:
        if self.video_path is None:
            return
        desired = self._desired_position_ms(session_time_s)
        duration = self.player.duration()
        if duration > 0:
            desired = min(desired, duration)
        self.player.setPosition(desired)

    def _on_clock_time(self, session_time_s: float) -> None:
        if self.video_path is None:
            return

        desired = self._desired_position_ms(session_time_s)
        actual = self.player.position()
        now = time.monotonic()

        # Paused/scrubbing should be exact. During normal playback, allow a
        # small tolerance and resync at most twice per second to avoid stutter.
        if not self.clock.playing:
            if abs(actual - desired) > 25:
                self.player.setPosition(desired)
        elif (
            abs(actual - desired) > 1000
            or (abs(actual - desired) > 180 and now - self._last_resync_wall >= 0.5)
        ):
            self.player.setPosition(desired)
            self._last_resync_wall = now

        self.time_label.setText(
            f"{self.video_path.name} — session {format_time_s(session_time_s)}"
        )

    def _on_playing_changed(self, playing: bool) -> None:
        if self.video_path is None:
            return
        self._seek_exact(self.clock.current_time_s)
        if playing:
            self.player.play()
        else:
            self.player.pause()

    def _on_rate_changed(self, rate: float) -> None:
        self.player.setPlaybackRate(float(rate))

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
