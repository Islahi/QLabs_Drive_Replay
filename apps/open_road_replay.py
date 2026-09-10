"""Interactive Open Road drive replay with explicit OOP architecture.

Main window: map + master replay timeline.
Second window: synchronized recorded video.

QLabs transform replay is intentionally not exposed in this version. The
ReplayCoordinator synchronizes only the map and video windows for now; a small
third QLabs replay window can be attached later without changing session files.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QMouseEvent, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from core.replay_core import (
    PassCandidate,
    SessionData,
    extract_stable_completed_loop,
    find_open_road_reference,
    format_time_s,
    load_open_road_reference,
    offset_polyline_xy,
    rdp_simplify,
)
from core.replay_clock import ReplayClock
from core.replay_controller import ReplayCoordinator
from integrations.replay_sinks import MapReplaySink, VideoReplaySink
from ui.replay_video_window import VideoWindow

LANE_WIDTH_M = 4.0
MEDIAN_WIDTH_M = 0.25
LOGGED_LANE_OFFSET_FROM_MEDIAN_M = MEDIAN_WIDTH_M / 2.0 + 1.5 * LANE_WIDTH_M
TOTAL_APPROX_ROAD_WIDTH_M = 6.0 * LANE_WIDTH_M + MEDIAN_WIDTH_M


class ReplayMap(QWidget):
    mapClicked = Signal(float, float)

    def __init__(self, reference_loop: list[list[float]], parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(900, 500)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.reference_loop = reference_loop
        self.reference_display = rdp_simplify(reference_loop, tolerance_m=3.0)
        self.median_points = offset_polyline_xy(
            self.reference_display,
            LOGGED_LANE_OFFSET_FROM_MEDIAN_M,
            closed=True,
        )

        xs = [p[0] for p in reference_loop]
        ys = [p[1] for p in reference_loop]
        self.data_min_x = min(xs)
        self.data_max_x = max(xs)
        self.data_min_y = min(ys)
        self.data_max_y = max(ys)
        self.data_center_x = (self.data_min_x + self.data_max_x) / 2.0
        self.data_center_y = (self.data_min_y + self.data_max_y) / 2.0

        self.center_x = self.data_center_x
        self.center_y = self.data_center_y
        self.zoom = 1.0

        self.session: SessionData | None = None
        self.session_display: list[list[float]] = []
        self.current_pose = None
        self.hover_world: tuple[float, float] | None = None
        self.last_clicked_world: tuple[float, float] | None = None

        self._pan_active = False
        self._pan_last = QPointF()

    def set_session(self, session: SessionData | None) -> None:
        self.session = session
        if session is None:
            self.session_display = []
        else:
            self.session_display = rdp_simplify(session.xyz_points(), tolerance_m=2.0)
        self.update()

    def set_current_pose(self, pose) -> None:
        self.current_pose = pose
        self.update()

    def reset_view(self) -> None:
        self.center_x = self.data_center_x
        self.center_y = self.data_center_y
        self.zoom = 1.0
        self.update()

    def fit_scale(self) -> float:
        margin = 34.0
        width = max(1.0, self.width() - 2 * margin)
        height = max(1.0, self.height() - 2 * margin)
        span_x = max(1.0, self.data_max_x - self.data_min_x)
        span_y = max(1.0, self.data_max_y - self.data_min_y)
        return min(width / span_x, height / span_y)

    def pixels_per_metre(self) -> float:
        return self.fit_scale() * self.zoom

    def world_to_screen(self, x: float, y: float) -> QPointF:
        scale = self.pixels_per_metre()
        return QPointF(
            self.width() / 2.0 + (x - self.center_x) * scale,
            self.height() / 2.0 - (y - self.center_y) * scale,
        )

    def screen_to_world(self, pos: QPointF) -> tuple[float, float]:
        scale = max(self.pixels_per_metre(), 1e-12)
        return (
            self.center_x + (pos.x() - self.width() / 2.0) / scale,
            self.center_y - (pos.y() - self.height() / 2.0) / scale,
        )

    def click_radius_m(self) -> float:
        # Approx. 12 screen pixels, constrained so whole-map clicks do not select
        # a completely different road branch hundreds of metres away.
        scale = max(self.pixels_per_metre(), 1e-12)
        return max(8.0, min(50.0, 12.0 / scale))

    def _make_path(self, points) -> QPainterPath:
        path = QPainterPath()
        if not points:
            return path
        p = self.world_to_screen(float(points[0][0]), float(points[0][1]))
        path.moveTo(p)
        for point in points[1:]:
            p = self.world_to_screen(float(point[0]), float(point[1]))
            path.lineTo(p)
        return path

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(31, 35, 41))

        scale = self.pixels_per_metre()
        road_width_px = max(3.0, min(90.0, TOTAL_APPROX_ROAD_WIDTH_M * scale))
        road_pen = QPen(QColor(82, 88, 96), road_width_px)
        road_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        road_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(road_pen)
        painter.drawPath(self._make_path(self.median_points))

        median_pen = QPen(QColor(176, 163, 112), max(1.0, min(8.0, MEDIAN_WIDTH_M * scale)))
        median_pen.setCosmetic(True)
        painter.setPen(median_pen)
        painter.drawPath(self._make_path(self.median_points))

        reference_pen = QPen(QColor(226, 191, 66), 1.5)
        reference_pen.setCosmetic(True)
        reference_pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(reference_pen)
        painter.drawPath(self._make_path(self.reference_display))

        if self.session_display:
            session_pen = QPen(QColor(74, 176, 230), 2.2)
            session_pen.setCosmetic(True)
            painter.setPen(session_pen)
            painter.drawPath(self._make_path(self.session_display))

        origin = self.world_to_screen(0.0, 0.0)
        axis_pen = QPen(QColor(90, 140, 180, 90), 1.0)
        axis_pen.setCosmetic(True)
        painter.setPen(axis_pen)
        painter.drawLine(QPointF(origin.x(), 0), QPointF(origin.x(), self.height()))
        painter.drawLine(QPointF(0, origin.y()), QPointF(self.width(), origin.y()))

        if self.current_pose is not None:
            p = self.world_to_screen(self.current_pose.x, self.current_pose.y)
            marker_pen = QPen(QColor(92, 236, 153), 2.0)
            marker_pen.setCosmetic(True)
            painter.setPen(marker_pen)
            painter.setBrush(QColor(92, 236, 153, 100))
            painter.drawEllipse(p, 7.0, 7.0)

            # Heading tick.
            length_px = 20.0
            end = QPointF(
                p.x() + math.cos(self.current_pose.yaw_rad) * length_px,
                p.y() - math.sin(self.current_pose.yaw_rad) * length_px,
            )
            painter.drawLine(p, end)

        if self.last_clicked_world is not None:
            p = self.world_to_screen(*self.last_clicked_world)
            click_pen = QPen(QColor(244, 126, 96), 1.5)
            click_pen.setCosmetic(True)
            painter.setPen(click_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(p, 6.0, 6.0)

        painter.setPen(QColor(225, 230, 235))
        if self.hover_world is not None:
            painter.drawText(
                12,
                22,
                f"Cursor X {self.hover_world[0]:.1f} m   Y {self.hover_world[1]:.1f} m",
            )
        painter.setPen(QColor(184, 192, 201))
        painter.drawText(
            12,
            self.height() - 12,
            "Blue: recorded session   Yellow dotted: Open Road reference   |   "
            "Left click route: seek   Wheel: zoom   Middle/right drag: pan",
        )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        self.hover_world = self.screen_to_world(pos)
        if self._pan_active:
            delta = pos - self._pan_last
            scale = max(self.pixels_per_metre(), 1e-12)
            self.center_x -= delta.x() / scale
            self.center_y += delta.y() / scale
            self._pan_last = pos
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._pan_active = True
            self._pan_last = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            x, y = self.screen_to_world(event.position())
            self.last_clicked_world = (x, y)
            self.mapClicked.emit(x, y)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._pan_active = False
            self.unsetCursor()

    def wheelEvent(self, event: QWheelEvent) -> None:
        mouse_pos = event.position()
        before_x, before_y = self.screen_to_world(mouse_pos)
        steps = event.angleDelta().y() / 120.0
        self.zoom = max(0.35, min(300.0, self.zoom * (1.25 ** steps)))
        after_x, after_y = self.screen_to_world(mouse_pos)
        self.center_x += before_x - after_x
        self.center_y += before_y - after_y
        self.update()


class ReplayWindow(QMainWindow):
    """Presentation layer for the replay system.

    ReplayWindow handles user interaction only. ReplayCoordinator owns
    synchronization and broadcasts state to ReplaySink implementations.
    """

    def __init__(self, reference_path: Path, reference_loop: list[list[float]]) -> None:
        super().__init__()
        self.setWindowTitle("QLabs Open Road Drive Replay — OOP")
        self.resize(1320, 820)

        self.reference_path = reference_path
        self.session: SessionData | None = None
        self.clock = ReplayClock(self)
        self.coordinator = ReplayCoordinator(self.clock, self)
        self._slider_dragging = False
        self._resume_after_drag = False

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        top = QHBoxLayout()
        outer.addLayout(top)

        self.load_button = QPushButton("Load Session…")
        self.load_button.clicked.connect(self.choose_session)
        top.addWidget(self.load_button)

        self.session_label = QLabel("No recording loaded")
        self.session_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top.addWidget(self.session_label, 1)

        self.reset_map_button = QPushButton("Reset Map")
        top.addWidget(self.reset_map_button)

        self.map_widget = ReplayMap(reference_loop)
        self.map_widget.mapClicked.connect(self.on_map_clicked)
        self.reset_map_button.clicked.connect(self.map_widget.reset_view)
        outer.addWidget(self.map_widget, 1)

        # The active replay has exactly two outputs: map + recorded video.
        # QLabs transform replay is intentionally dormant for now.
        self.map_sink = MapReplaySink(self.map_widget)
        self.video_window = VideoWindow()
        self.video_sink = VideoReplaySink(self.video_window)
        self.coordinator.add_sink(self.map_sink)
        self.coordinator.add_sink(self.video_sink)

        transport = QHBoxLayout()
        outer.addLayout(transport)

        self.back_button = QPushButton("−1 s")
        self.back_button.clicked.connect(
            lambda: self.clock.seek(self.clock.current_time_s - 1.0)
        )
        transport.addWidget(self.back_button)

        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.clock.toggle)
        transport.addWidget(self.play_button)

        self.forward_button = QPushButton("+1 s")
        self.forward_button.clicked.connect(
            lambda: self.clock.seek(self.clock.current_time_s + 1.0)
        )
        transport.addWidget(self.forward_button)

        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.sliderPressed.connect(self.on_slider_pressed)
        self.timeline.sliderMoved.connect(self.on_slider_moved)
        self.timeline.sliderReleased.connect(self.on_slider_released)
        transport.addWidget(self.timeline, 1)

        self.time_label = QLabel("00:00:00.000 / 00:00:00.000")
        transport.addWidget(self.time_label)

        self.rate_combo = QComboBox()
        for label, value in [
            ("0.25×", 0.25),
            ("0.5×", 0.5),
            ("1×", 1.0),
            ("2×", 2.0),
            ("4×", 4.0),
        ]:
            self.rate_combo.addItem(label, value)
        self.rate_combo.setCurrentIndex(2)
        self.rate_combo.currentIndexChanged.connect(
            lambda _index: self.clock.set_rate(float(self.rate_combo.currentData()))
        )
        transport.addWidget(self.rate_combo)

        lower = QHBoxLayout()
        outer.addLayout(lower)

        self.position_label = QLabel("X —   Y —   Z —")
        lower.addWidget(self.position_label, 1)
        lower.addWidget(QLabel("Replay outputs: Map + Video"))

        video_row = QHBoxLayout()
        outer.addLayout(video_row)
        video_row.addWidget(QLabel("Video window:"))
        self.video_path_edit = QLineEdit()
        self.video_path_edit.setReadOnly(True)
        video_row.addWidget(self.video_path_edit, 1)
        self.load_video_button = QPushButton("Load Manual Video…")
        self.load_video_button.clicked.connect(self.choose_video)
        video_row.addWidget(self.load_video_button)
        self.show_video_button = QPushButton("Show Video Window")
        self.show_video_button.clicked.connect(self.show_video_window)
        video_row.addWidget(self.show_video_button)
        video_row.addWidget(QLabel("Offset"))
        self.video_offset_spin = QDoubleSpinBox()
        self.video_offset_spin.setRange(-3600.0, 3600.0)
        self.video_offset_spin.setDecimals(3)
        self.video_offset_spin.setSingleStep(0.1)
        self.video_offset_spin.setSuffix(" s")
        self.video_offset_spin.setToolTip(
            "video_position = replay_time + offset. Positive means the session starts later in the video."
        )
        self.video_offset_spin.valueChanged.connect(self.video_sink.set_offset_s)
        video_row.addWidget(self.video_offset_spin)

        self.status_label = QLabel(
            f"Open Road reference: {reference_path}. Load a recorded session to begin."
        )
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        self.clock.playingChanged.connect(self.on_playing_changed)
        self.clock.durationChanged.connect(self.on_duration_changed)
        self.coordinator.poseChanged.connect(self.on_pose_changed)
        self.coordinator.sinkError.connect(self.on_sink_error)

    def choose_session(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose recorded session folder")
        if path:
            self.load_session(Path(path))

    def load_session(self, path: Path) -> None:
        try:
            session = SessionData.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Could not load session", str(exc))
            return

        self.session = session
        self.coordinator.set_session(session)
        self.session_label.setText(
            f"{session.folder.name} — {session.sample_count:,} samples — "
            f"{format_time_s(session.duration_s)}"
        )
        self.status_label.setText(f"Loaded session: {session.folder}")

        # VideoReplaySink auto-discovers all synchronized session videos.
        if self.video_window.available_source_count() > 0:
            self.video_window.show()
            self.video_window.raise_()
            if self.video_window.video_path is not None:
                self.video_path_edit.setText(str(self.video_window.video_path))
            self.status_label.setText(
                f"Loaded session: {session.folder} — "
                f"{self.video_window.available_source_count()} video source(s) available."
            )
        else:
            self.video_path_edit.clear()
            self.status_label.setText(
                f"Loaded session: {session.folder} — no recorded video file was found."
            )

    def on_duration_changed(self, duration_s: float) -> None:
        self.timeline.setRange(0, max(0, int(round(duration_s * 1000.0))))

    def on_playing_changed(self, playing: bool) -> None:
        self.play_button.setText("Pause" if playing else "Play")

    def on_pose_changed(self, time_s: float, pose) -> None:
        if self.session is None:
            return
        self.position_label.setText(
            f"X {pose.x:.3f}   Y {pose.y:.3f}   Z {pose.z:.3f}"
        )
        self.time_label.setText(
            f"{format_time_s(time_s)} / {format_time_s(self.session.duration_s)}"
        )
        if not self._slider_dragging:
            self.timeline.blockSignals(True)
            self.timeline.setValue(int(round(time_s * 1000.0)))
            self.timeline.blockSignals(False)

    def on_sink_error(self, sink_name: str, message: str) -> None:
        self.status_label.setText(f"{sink_name} replay update failed: {message}")

    def on_slider_pressed(self) -> None:
        self._slider_dragging = True
        self._resume_after_drag = self.clock.playing
        self.clock.pause()

    def on_slider_moved(self, value: int) -> None:
        self.clock.seek(value / 1000.0)

    def on_slider_released(self) -> None:
        self.coordinator.force_sync()
        self.clock.seek(self.timeline.value() / 1000.0)
        self._slider_dragging = False
        if self._resume_after_drag:
            self.clock.play()
        self._resume_after_drag = False

    def on_map_clicked(self, x: float, y: float) -> None:
        if self.session is None:
            self.status_label.setText("Load a recording before seeking from the map.")
            return

        radius = self.map_widget.click_radius_m()
        candidates = self.session.find_passes(x, y, radius_m=radius)
        if not candidates:
            nearest = self.session.nearest_sample(x, y)
            self.status_label.setText(
                f"No recorded pass within {radius:.1f} m. Nearest route point is "
                f"{nearest.distance_m:.1f} m away at {format_time_s(nearest.time_s)}."
            )
            return

        if len(candidates) == 1:
            self.seek_candidate(candidates[0])
            return

        menu = QMenu(self)
        title = menu.addAction(f"{len(candidates)} passes found — choose one")
        title.setEnabled(False)
        menu.addSeparator()
        for number, candidate in enumerate(candidates, start=1):
            action = menu.addAction(
                f"Pass {number}: {format_time_s(candidate.time_s)}   "
                f"({candidate.distance_m:.1f} m from click)"
            )
            action.triggered.connect(
                lambda _checked=False, c=candidate: self.seek_candidate(c)
            )
        menu.exec(QCursor.pos())

    def seek_candidate(self, candidate: PassCandidate) -> None:
        self.coordinator.force_sync()
        self.clock.seek(candidate.time_s)
        self.status_label.setText(
            f"Jumped to {format_time_s(candidate.time_s)} — recorded point "
            f"X={candidate.x:.2f}, Y={candidate.y:.2f}, Z={candidate.z:.2f}."
        )

    def choose_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose replay video",
            str(self.session.folder if self.session else Path.cwd()),
            "Video files (*.mp4 *.mov *.avi *.mkv *.m4v);;All files (*)",
        )
        if not path:
            return
        try:
            self.video_sink.load_video(Path(path))
            self.video_path_edit.setText(path)
            self.video_sink.set_offset_s(self.video_offset_spin.value())
            self.coordinator.sync_now()
            self.video_window.show()
        except Exception as exc:
            QMessageBox.critical(self, "Could not load video", str(exc))

    def show_video_window(self) -> None:
        self.video_window.show()
        self.video_window.raise_()
        self.video_window.activateWindow()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            self.clock.toggle()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Left:
            self.coordinator.force_sync()
            self.clock.seek(self.clock.current_time_s - 1.0)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self.coordinator.force_sync()
            self.clock.seek(self.clock.current_time_s + 1.0)
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self.coordinator.close()
        super().closeEvent(event)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive QLabs Open Road drive replay."
    )
    parser.add_argument(
        "--session",
        type=Path,
        default=None,
        help="Recorded session folder",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        default=None,
        help="open_road_reference.json path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        reference_path = find_open_road_reference(args.reference, Path(__file__))
        _document, raw_reference = load_open_road_reference(reference_path)
        reference_loop, _info = extract_stable_completed_loop(raw_reference)
    except Exception as exc:
        print(f"Could not load Open Road reference: {exc}", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    window = ReplayWindow(reference_path, reference_loop)
    if args.session is not None:
        window.load_session(args.session)
    window.show()
    # The reviewer intentionally uses two windows: map/timeline and video.
    window.video_window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
