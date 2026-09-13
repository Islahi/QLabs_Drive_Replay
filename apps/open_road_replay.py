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
    QCheckBox,
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
    OpenRoadLaneGeometry,
    PassCandidate,
    SessionData,
    extract_stable_completed_loop,
    find_open_road_reference,
    format_time_s,
    load_open_road_reference,
    load_open_road_six_lane_geometry,
    offset_polyline_xy,
    rdp_simplify,
)
from core.replay_clock import ReplayClock
from core.replay_controller import ReplayCoordinator
from integrations.replay_sinks import MapReplaySink, VideoReplaySink
from ui.replay_video_window import VideoWindow

# Legacy single-reference fallback. New installations use the six measured
# lane-center JSONs and the calibrated +/-12 m road boundaries.
LANE_WIDTH_M = 4.0
MEDIAN_WIDTH_M = 0.25
LOGGED_LANE_OFFSET_FROM_MEDIAN_M = MEDIAN_WIDTH_M / 2.0 + 1.5 * LANE_WIDTH_M
TOTAL_APPROX_ROAD_WIDTH_M = 6.0 * LANE_WIDTH_M + MEDIAN_WIDTH_M

# Distinct trajectory colors for multi-record comparison. Colors are assigned
# in load order and remain attached to each recording when the active replay changes.
SESSION_COLORS = [
    (52, 183, 245),
    (255, 156, 64),
    (225, 91, 154),
    (79, 210, 170),
    (173, 126, 255),
    (245, 205, 70),
    (239, 92, 92),
    (83, 203, 232),
]


class ReplayMap(QWidget):
    mapClicked = Signal(float, float)

    def __init__(
        self,
        reference_loop: list[list[float]],
        lane_geometry: OpenRoadLaneGeometry | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumSize(900, 500)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.reference_loop = reference_loop
        self.reference_display = rdp_simplify(reference_loop, tolerance_m=3.0)
        self.lane_geometry = lane_geometry

        # Legacy fallback for repositories that only have one Open Road trace.
        self.median_points = offset_polyline_xy(
            self.reference_display,
            LOGGED_LANE_OFFSET_FROM_MEDIAN_M,
            closed=True,
        )

        if lane_geometry is not None:
            bound_paths = lane_geometry.all_xy_paths()
            xs = [float(p[0]) for path in bound_paths for p in path]
            ys = [float(p[1]) for path in bound_paths for p in path]
        else:
            xs = [float(p[0]) for p in reference_loop]
            ys = [float(p[1]) for p in reference_loop]

        self.data_min_x = min(xs)
        self.data_max_x = max(xs)
        self.data_min_y = min(ys)
        self.data_max_y = max(ys)
        self.data_center_x = (self.data_min_x + self.data_max_x) / 2.0
        self.data_center_y = (self.data_min_y + self.data_max_y) / 2.0

        self.center_x = self.data_center_x
        self.center_y = self.data_center_y
        self.zoom = 1.0

        # One recording is active for video/timeline replay, while any number of
        # additional recordings can remain visible for trajectory comparison.
        self.session: SessionData | None = None
        self.session_display: list[list[float]] = []
        self.current_pose = None
        self.session_overlays: dict[str, dict] = {}
        self.active_session_key: str | None = None
        self.comparison_poses: dict[str, object] = {}
        self._display_cache: dict[str, list[list[float]]] = {}
        self.hover_world: tuple[float, float] | None = None
        self.last_clicked_world: tuple[float, float] | None = None

        self._pan_active = False
        self._pan_last = QPointF()

    def set_session(self, session: SessionData | None) -> None:
        """Set the active replay without clearing comparison trajectories."""
        self.session = session
        if session is None:
            self.session_display = []
        elif self.session_overlays:
            # Multi-record mode already owns cached simplified trajectories.
            self.session_display = []
        else:
            self.session_display = rdp_simplify(session.xyz_points(), tolerance_m=1.0)
        self.update()

    def set_session_overlays(self, entries: list[dict], active_key: str | None) -> None:
        keep_keys = {str(entry["key"]) for entry in entries}
        self._display_cache = {
            key: value for key, value in self._display_cache.items() if key in keep_keys
        }
        overlays: dict[str, dict] = {}
        for entry in entries:
            key = str(entry["key"])
            session = entry["session"]
            if key not in self._display_cache:
                self._display_cache[key] = rdp_simplify(
                    session.xyz_points(), tolerance_m=1.0
                )
            overlays[key] = {
                "key": key,
                "label": str(entry["label"]),
                "session": session,
                "color": tuple(entry["color"]),
                "display": self._display_cache[key],
            }
        self.session_overlays = overlays
        self.active_session_key = active_key
        self.comparison_poses = {}
        self.update()

    def set_comparison_time(self, time_s: float) -> None:
        """Place all comparison cars at the same elapsed session time."""
        poses: dict[str, object] = {}
        for key, entry in self.session_overlays.items():
            if key == self.active_session_key:
                continue
            session = entry["session"]
            if not session.times:
                continue
            if float(time_s) < float(session.times[0]) or float(time_s) > session.duration_s:
                continue
            poses[key] = session.pose_at(time_s)
        self.comparison_poses = poses
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

    def _make_band_path(self, edge_a, edge_b) -> QPainterPath:
        """Build a filled road band between two already aligned world paths.

        Filling the actual boundary polygon (instead of drawing a very thick
        centerline pen) preserves the road width exactly at every zoom level.
        """
        path = QPainterPath()
        if not edge_a or not edge_b:
            return path
        first = self.world_to_screen(float(edge_a[0][0]), float(edge_a[0][1]))
        path.moveTo(first)
        for point in edge_a[1:]:
            path.lineTo(self.world_to_screen(float(point[0]), float(point[1])))
        for point in reversed(edge_b):
            path.lineTo(self.world_to_screen(float(point[0]), float(point[1])))
        path.closeSubpath()
        return path

    def _draw_six_lane_reference(self, painter: QPainter) -> None:
        geometry = self.lane_geometry
        if geometry is None:
            return

        # Fill the carriageways from their measured/derived world boundaries.
        # This is intentionally polygon-based instead of a capped thick pen: the
        # grey pavement therefore continues to cover all six lanes at any zoom.
        road_fill = QColor(75, 81, 90)
        painter.fillPath(
            self._make_band_path(
                geometry.outer_edges["upper"], geometry.median_edges["upper"]
            ),
            road_fill,
        )
        painter.fillPath(
            self._make_band_path(
                geometry.median_edges["lower"], geometry.outer_edges["lower"]
            ),
            road_fill,
        )

        # Fill the approximate median/barrier between the calibrated +/-0.6 m
        # median-side edges. It scales naturally because it is also a polygon.
        painter.fillPath(
            self._make_band_path(
                geometry.median_edges["upper"], geometry.median_edges["lower"]
            ),
            QColor(164, 155, 128),
        )

        # Solid road boundaries: +/-12 m outer edges and both median-side edges.
        # Cosmetic width is deliberate here: these are markings, so they should
        # remain easy to identify even at the whole-map view.
        edge_pen = QPen(QColor(246, 248, 250), 2.8)
        edge_pen.setCosmetic(True)
        painter.setPen(edge_pen)
        for path in geometry.outer_edges.values():
            painter.drawPath(self._make_path(path))
        for path in geometry.median_edges.values():
            painter.drawPath(self._make_path(path))

        # Painted dashed lane dividers. These are intentionally thicker than the
        # previous version to stay recognizable under multiple trajectory lines.
        divider_pen = QPen(QColor(248, 249, 251), 2.6)
        divider_pen.setCosmetic(True)
        divider_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(divider_pen)
        for path in geometry.lane_dividers.values():
            painter.drawPath(self._make_path(path))

        # Faint measured lane-center guides remain secondary analysis aids.
        center_pen = QPen(QColor(132, 158, 178, 155), 1.0)
        center_pen.setCosmetic(True)
        center_pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(center_pen)
        for path in geometry.lane_centers.values():
            painter.drawPath(self._make_path(path))

    def _draw_legacy_reference(self, painter: QPainter) -> None:
        scale = self.pixels_per_metre()
        road_width_px = max(3.0, TOTAL_APPROX_ROAD_WIDTH_M * scale)
        road_pen = QPen(QColor(82, 88, 96), road_width_px)
        road_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        road_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(road_pen)
        painter.drawPath(self._make_path(self.median_points))

        median_pen = QPen(QColor(176, 163, 112), 1.2)
        median_pen.setCosmetic(True)
        painter.setPen(median_pen)
        painter.drawPath(self._make_path(self.median_points))

        reference_pen = QPen(QColor(226, 191, 66), 1.5)
        reference_pen.setCosmetic(True)
        reference_pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(reference_pen)
        painter.drawPath(self._make_path(self.reference_display))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(31, 35, 41))

        if self.lane_geometry is not None:
            self._draw_six_lane_reference(painter)
        else:
            self._draw_legacy_reference(painter)

        # Every loaded recording gets a persistent color. The active recording
        # is drawn last and thicker because it controls the video/timeline.
        overlay_items = list(self.session_overlays.items())
        overlay_items.sort(key=lambda item: item[0] == self.active_session_key)
        for key, entry in overlay_items:
            color = QColor(*entry["color"])
            session_pen = QPen(color, 3.4 if key == self.active_session_key else 2.1)
            session_pen.setCosmetic(True)
            session_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            session_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(session_pen)
            painter.drawPath(self._make_path(entry["display"]))

        if not self.session_overlays and self.session_display:
            session_pen = QPen(QColor(52, 183, 245), 3.0)
            session_pen.setCosmetic(True)
            painter.setPen(session_pen)
            painter.drawPath(self._make_path(self.session_display))

        origin = self.world_to_screen(0.0, 0.0)
        axis_pen = QPen(QColor(90, 140, 180, 75), 1.0)
        axis_pen.setCosmetic(True)
        painter.setPen(axis_pen)
        painter.drawLine(QPointF(origin.x(), 0), QPointF(origin.x(), self.height()))
        painter.drawLine(QPointF(0, origin.y()), QPointF(self.width(), origin.y()))

        marker_poses = dict(self.comparison_poses)
        if self.current_pose is not None and self.active_session_key is not None:
            marker_poses[self.active_session_key] = self.current_pose

        for marker_index, (key, pose) in enumerate(marker_poses.items()):
            entry = self.session_overlays.get(key)
            if entry is None:
                continue
            color = QColor(*entry["color"])
            p = self.world_to_screen(pose.x, pose.y)
            active = key == self.active_session_key
            radius = 8.0 if active else 6.0

            marker_pen = QPen(QColor(245, 248, 250), 2.0 if active else 1.2)
            marker_pen.setCosmetic(True)
            painter.setPen(marker_pen)
            fill = QColor(color)
            fill.setAlpha(210)
            painter.setBrush(fill)
            painter.drawEllipse(p, radius, radius)

            heading_pen = QPen(color, 2.0 if active else 1.4)
            heading_pen.setCosmetic(True)
            painter.setPen(heading_pen)
            length_px = 22.0 if active else 16.0
            end = QPointF(
                p.x() + math.cos(pose.yaw_rad) * length_px,
                p.y() - math.sin(pose.yaw_rad) * length_px,
            )
            painter.drawLine(p, end)

            painter.setPen(QColor(235, 239, 243))
            label = entry["label"]
            if len(label) > 24:
                label = label[:21] + "…"
            prefix = "ACTIVE: " if active else ""
            painter.drawText(
                p + QPointF(9.0, -9.0 - (marker_index % 2) * 10.0),
                prefix + label,
            )

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

        # Compact recording/color legend. This stays visible even when current
        # car markers overlap on the same lane.
        if self.session_overlays:
            legend_x = max(12, self.width() - 300)
            legend_y = 22
            max_rows = 10
            for row, (key, entry) in enumerate(list(self.session_overlays.items())[:max_rows]):
                color = QColor(*entry["color"])
                line_pen = QPen(color, 3.0 if key == self.active_session_key else 2.0)
                line_pen.setCosmetic(True)
                painter.setPen(line_pen)
                painter.drawLine(legend_x, legend_y + row * 18 - 5, legend_x + 24, legend_y + row * 18 - 5)
                painter.setPen(QColor(230, 234, 238))
                label = entry["label"]
                if len(label) > 26:
                    label = label[:23] + "…"
                prefix = "ACTIVE — " if key == self.active_session_key else ""
                painter.drawText(legend_x + 31, legend_y + row * 18, prefix + label)
            if len(self.session_overlays) > max_rows:
                painter.setPen(QColor(184, 192, 201))
                painter.drawText(
                    legend_x + 31,
                    legend_y + max_rows * 18,
                    f"+{len(self.session_overlays) - max_rows} more",
                )

        painter.setPen(QColor(184, 192, 201))
        if self.lane_geometry is not None:
            legend = (
                "White: road/lane markings   Faint dotted: six measured lane centers   "
                "Colored: loaded recordings (thicker = active)   |   "
                "Left click any route: seek/switch   Wheel: zoom   Middle/right drag: pan"
            )
        else:
            legend = (
                "Blue: recorded session   Yellow dotted: legacy Open Road reference   |   "
                "Left click route: seek   Wheel: zoom   Middle/right drag: pan"
            )
        painter.drawText(12, self.height() - 12, legend)

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

    def __init__(
        self,
        reference_path: Path,
        reference_loop: list[list[float]],
        lane_geometry: OpenRoadLaneGeometry | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("QLabs Open Road Drive Replay — OOP")
        self.resize(1320, 820)

        self.reference_path = reference_path
        self.session: SessionData | None = None
        self.loaded_sessions: dict[str, SessionData] = {}
        self.session_labels: dict[str, str] = {}
        self.session_colors: dict[str, tuple[int, int, int]] = {}
        self.session_order: list[str] = []
        self.active_session_key: str | None = None
        self.clock = ReplayClock(self)
        self.coordinator = ReplayCoordinator(self.clock, self)
        self._slider_dragging = False
        self._resume_after_drag = False

        # Optional synchronized comparison video windows. The normal video
        # window always belongs to the active replay; when multi-video mode is
        # enabled, each non-active loaded session receives its own VideoWindow.
        self.multi_video_mode = False
        self.comparison_video_windows: dict[str, VideoWindow] = {}
        self.comparison_video_sinks: dict[str, VideoReplaySink] = {}

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        top = QHBoxLayout()
        outer.addLayout(top)

        self.load_button = QPushButton("Add Session…")
        self.load_button.setToolTip("Add one recorded session to the comparison map.")
        self.load_button.clicked.connect(self.choose_session)
        top.addWidget(self.load_button)

        self.load_folder_button = QPushButton("Add Recordings Folder…")
        self.load_folder_button.setToolTip(
            "Load every session.json found below a recordings folder."
        )
        self.load_folder_button.clicked.connect(self.choose_recordings_folder)
        top.addWidget(self.load_folder_button)

        top.addWidget(QLabel("Active replay:"))
        self.active_session_combo = QComboBox()
        self.active_session_combo.setMinimumWidth(220)
        self.active_session_combo.setToolTip(
            "The active recording controls the timeline and video; all loaded trajectories stay visible."
        )
        self.active_session_combo.currentIndexChanged.connect(self.on_active_session_changed)
        top.addWidget(self.active_session_combo)

        self.remove_session_button = QPushButton("Remove Active")
        self.remove_session_button.clicked.connect(self.remove_active_session)
        top.addWidget(self.remove_session_button)

        self.clear_sessions_button = QPushButton("Clear All")
        self.clear_sessions_button.clicked.connect(self.clear_sessions)
        top.addWidget(self.clear_sessions_button)

<<<<<<< HEAD
        self.apply_trim_check = QCheckBox("Align driving starts")
        self.apply_trim_check.setChecked(True)
        self.apply_trim_check.setToolTip(
            "Apply each session's saved non-destructive driving-start trim. "
            "Turn this off to inspect the raw setup/waiting period."
        )
        self.apply_trim_check.toggled.connect(self.on_alignment_mode_changed)
        top.addWidget(self.apply_trim_check)

=======
>>>>>>> 2bfea3aae85bdb3c31675e62667ce3948d6a0dad
        self.session_label = QLabel("0 recordings loaded")
        self.session_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top.addWidget(self.session_label, 1)

        self.reset_map_button = QPushButton("Reset Map")
        top.addWidget(self.reset_map_button)

        self.map_widget = ReplayMap(reference_loop, lane_geometry=lane_geometry)
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
        self.show_video_button = QPushButton("Show Active Video")
        self.show_video_button.clicked.connect(self.show_video_window)
        video_row.addWidget(self.show_video_button)

        self.multi_video_button = QPushButton("Open All Session Videos")
        self.multi_video_button.setCheckable(True)
        self.multi_video_button.setToolTip(
            "Open one synchronized video window for every loaded recording. "
            "The active recording keeps the normal video window; comparison "
            "recordings open in additional windows."
        )
        self.multi_video_button.toggled.connect(self.set_multi_video_mode)
        video_row.addWidget(self.multi_video_button)

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

        reference_mode = "six measured lane references" if lane_geometry is not None else "legacy single reference"
        self.status_label = QLabel(
            f"Open Road reference: {reference_path} ({reference_mode}). "
            "Load a recorded session to begin."
        )
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        self.clock.playingChanged.connect(self.on_playing_changed)
        self.clock.playingChanged.connect(self._sync_comparison_video_playing)
        self.clock.rateChanged.connect(self._sync_comparison_video_rate)
        self.clock.timeChanged.connect(self._sync_comparison_video_time)
        self.clock.durationChanged.connect(self.on_duration_changed)
        self.coordinator.poseChanged.connect(self.on_pose_changed)
        self.coordinator.sinkError.connect(self.on_sink_error)

    def choose_session(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose recorded session folder")
        if path:
            self.add_session(Path(path), make_active=True)

    def choose_recordings_folder(self) -> None:
        root = QFileDialog.getExistingDirectory(
            self, "Choose folder containing recorded sessions"
        )
        if not root:
            return
        root_path = Path(root)
        candidates = sorted({path.parent for path in root_path.rglob("session.json")})
        if not candidates:
            QMessageBox.information(
                self, "No sessions found",
                f"No session.json files were found below:\n{root_path}",
            )
<<<<<<< HEAD
=======
            return

        loaded = 0
        first_new_key: str | None = None
        for folder in candidates:
            key = str(folder.resolve())
            if key in self.loaded_sessions:
                continue
            if self.add_session(folder, make_active=False, show_errors=False):
                loaded += 1
                if first_new_key is None:
                    first_new_key = key

        if self.active_session_key is None and first_new_key is not None:
            self.set_active_session(first_new_key, preserve_time=False)
        else:
            self._refresh_session_ui()
        self.status_label.setText(
            f"Loaded {loaded} new recording(s) from {root_path}. "
            f"{len(self.loaded_sessions)} total trajectories visible."
        )

    def _unique_session_label(self, session: SessionData) -> str:
        base = session.folder.name or "recording"
        used = set(self.session_labels.values())
        if base not in used:
            return base
        counter = 2
        while f"{base} ({counter})" in used:
            counter += 1
        return f"{base} ({counter})"

    def _session_overlay_entries(self) -> list[dict]:
        entries: list[dict] = []
        for key in self.session_order:
            session = self.loaded_sessions.get(key)
            if session is None:
                continue
            entries.append({
                "key": key,
                "label": self.session_labels[key],
                "session": session,
                "color": self.session_colors[key],
            })
        return entries

    def _refresh_session_ui(self) -> None:
        self.active_session_combo.blockSignals(True)
        self.active_session_combo.clear()
        active_index = -1
        for key in self.session_order:
            if key not in self.loaded_sessions:
                continue
            label = self.session_labels[key]
            color = self.session_colors[key]
            self.active_session_combo.addItem(label, key)
            item_index = self.active_session_combo.count() - 1
            self.active_session_combo.setItemData(
                item_index, QColor(*color), Qt.ItemDataRole.ForegroundRole
            )
            if key == self.active_session_key:
                active_index = item_index
        if active_index >= 0:
            self.active_session_combo.setCurrentIndex(active_index)
        self.active_session_combo.blockSignals(False)

        self.map_widget.set_session_overlays(
            self._session_overlay_entries(), self.active_session_key
        )
        self.map_widget.set_comparison_time(self.clock.current_time_s)

        count = len(self.loaded_sessions)
        if self.active_session_key in self.loaded_sessions:
            active = self.loaded_sessions[self.active_session_key]
            label = self.session_labels[self.active_session_key]
            self.session_label.setText(
                f"{count} recording(s) loaded — active: {label} — "
                f"{active.sample_count:,} samples — {format_time_s(active.duration_s)}"
            )
        else:
            self.session_label.setText(f"{count} recording(s) loaded")
        self.remove_session_button.setEnabled(count > 0)
        self.clear_sessions_button.setEnabled(count > 0)
        self.active_session_combo.setEnabled(count > 0)

    def add_session(
        self, path: Path, make_active: bool = True, show_errors: bool = True
    ) -> bool:
        try:
            session = SessionData.load(path)
        except Exception as exc:
            if show_errors:
                QMessageBox.critical(self, "Could not load session", str(exc))
            return False

        key = str(session.folder.resolve())
        if key not in self.loaded_sessions:
            self.loaded_sessions[key] = session
            self.session_order.append(key)
            self.session_labels[key] = self._unique_session_label(session)
            used_colors = set(self.session_colors.values())
            available = [color for color in SESSION_COLORS if color not in used_colors]
            if available:
                self.session_colors[key] = available[0]
            else:
                palette_index = (len(self.session_order) - 1) % len(SESSION_COLORS)
                self.session_colors[key] = SESSION_COLORS[palette_index]

        if make_active or self.active_session_key is None:
            self.set_active_session(
                key, preserve_time=(self.active_session_key is not None)
            )
        else:
            self._refresh_session_ui()
            self._refresh_comparison_video_windows()
        return True

    def load_session(self, path: Path) -> None:
        """Backward-compatible API: loading now adds rather than replaces."""
        self.add_session(path, make_active=True)

    def set_active_session(self, key: str, preserve_time: bool = True) -> None:
        if key not in self.loaded_sessions:
>>>>>>> 2bfea3aae85bdb3c31675e62667ce3948d6a0dad
            return
        old_time = self.clock.current_time_s if preserve_time else 0.0
        self.active_session_key = key
        self.session = self.loaded_sessions[key]
        # Populate/cached map overlays before the coordinator changes the active
        # sink session, avoiding a second simplification of long telemetry.
        self._refresh_session_ui()
        self.coordinator.set_session(self.session)
        self.clock.seek(min(old_time, self.session.duration_s) if preserve_time else 0.0)

<<<<<<< HEAD
        loaded = 0
        first_new_key: str | None = None
        for folder in candidates:
            key = str(folder.resolve())
            if key in self.loaded_sessions:
                continue
            if self.add_session(folder, make_active=False, show_errors=False):
                loaded += 1
                if first_new_key is None:
                    first_new_key = key

        if self.active_session_key is None and first_new_key is not None:
            self.set_active_session(first_new_key, preserve_time=False)
        else:
            self._refresh_session_ui()
        self.status_label.setText(
            f"Loaded {loaded} new recording(s) from {root_path}. "
            f"{len(self.loaded_sessions)} total trajectories visible."
        )

    def _unique_session_label(self, session: SessionData) -> str:
        base = session.folder.name or "recording"
        used = set(self.session_labels.values())
        if base not in used:
            return base
        counter = 2
        while f"{base} ({counter})" in used:
            counter += 1
        return f"{base} ({counter})"

    def _session_overlay_entries(self) -> list[dict]:
        entries: list[dict] = []
        for key in self.session_order:
            session = self.loaded_sessions.get(key)
            if session is None:
                continue
            entries.append({
                "key": key,
                "label": self.session_labels[key],
                "session": session,
                "color": self.session_colors[key],
            })
        return entries

    def _refresh_session_ui(self) -> None:
        self.active_session_combo.blockSignals(True)
        self.active_session_combo.clear()
        active_index = -1
        for key in self.session_order:
            if key not in self.loaded_sessions:
                continue
            label = self.session_labels[key]
            color = self.session_colors[key]
            self.active_session_combo.addItem(label, key)
            item_index = self.active_session_combo.count() - 1
            self.active_session_combo.setItemData(
                item_index, QColor(*color), Qt.ItemDataRole.ForegroundRole
            )
            if key == self.active_session_key:
                active_index = item_index
        if active_index >= 0:
            self.active_session_combo.setCurrentIndex(active_index)
        self.active_session_combo.blockSignals(False)

        self.map_widget.set_session_overlays(
            self._session_overlay_entries(), self.active_session_key
        )
        self.map_widget.set_comparison_time(self.clock.current_time_s)

        count = len(self.loaded_sessions)
        if self.active_session_key in self.loaded_sessions:
            active = self.loaded_sessions[self.active_session_key]
            label = self.session_labels[self.active_session_key]
            alignment = (
                f"aligned +{active.analysis_start_s:.2f}s"
                if getattr(active, "analysis_start_s", 0.0) > 0.0
                else "raw start"
            )
            self.session_label.setText(
                f"{count} recording(s) loaded — active: {label} — "
                f"{active.sample_count:,} samples — {format_time_s(active.duration_s)} — {alignment}"
            )
        else:
            self.session_label.setText(f"{count} recording(s) loaded")
        self.remove_session_button.setEnabled(count > 0)
        self.clear_sessions_button.setEnabled(count > 0)
        self.active_session_combo.setEnabled(count > 0)

    def on_alignment_mode_changed(self, enabled: bool) -> None:
        """Reload in-memory sessions using raw or saved analysis time origins."""
        if not self.loaded_sessions:
            return
        self.clock.pause()
        active_key = self.active_session_key
        failures: list[str] = []
        for key in list(self.session_order):
            if key not in self.loaded_sessions:
                continue
            try:
                self.loaded_sessions[key] = SessionData.load(
                    Path(key), apply_analysis_trim=bool(enabled)
                )
            except Exception as exc:
                failures.append(f"{Path(key).name}: {exc}")
        if active_key in self.loaded_sessions:
            self.set_active_session(active_key, preserve_time=False)
        else:
            self._refresh_session_ui()
        mode = "driving-start aligned" if enabled else "raw recording start"
        self.status_label.setText(
            f"Replay timing changed to {mode} for {len(self.loaded_sessions)} loaded session(s)."
        )
        if failures:
            QMessageBox.warning(
                self,
                "Some sessions could not be reloaded",
                "\n".join(failures),
            )

    def add_session(
        self, path: Path, make_active: bool = True, show_errors: bool = True
    ) -> bool:
        try:
            session = SessionData.load(path, apply_analysis_trim=self.apply_trim_check.isChecked())
        except Exception as exc:
            if show_errors:
                QMessageBox.critical(self, "Could not load session", str(exc))
            return False

        key = str(session.folder.resolve())
        if key not in self.loaded_sessions:
            self.loaded_sessions[key] = session
            self.session_order.append(key)
            self.session_labels[key] = self._unique_session_label(session)
            used_colors = set(self.session_colors.values())
            available = [color for color in SESSION_COLORS if color not in used_colors]
            if available:
                self.session_colors[key] = available[0]
            else:
                palette_index = (len(self.session_order) - 1) % len(SESSION_COLORS)
                self.session_colors[key] = SESSION_COLORS[palette_index]

        if make_active or self.active_session_key is None:
            self.set_active_session(
                key, preserve_time=(self.active_session_key is not None)
            )
        else:
            self._refresh_session_ui()
            self._refresh_comparison_video_windows()
        return True

    def load_session(self, path: Path) -> None:
        """Backward-compatible API: loading now adds rather than replaces."""
        self.add_session(path, make_active=True)

    def set_active_session(self, key: str, preserve_time: bool = True) -> None:
        if key not in self.loaded_sessions:
            return
        old_time = self.clock.current_time_s if preserve_time else 0.0
        self.active_session_key = key
        self.session = self.loaded_sessions[key]
        # Populate/cached map overlays before the coordinator changes the active
        # sink session, avoiding a second simplification of long telemetry.
        self._refresh_session_ui()
        self.coordinator.set_session(self.session)
        self.clock.seek(min(old_time, self.session.duration_s) if preserve_time else 0.0)

=======
>>>>>>> 2bfea3aae85bdb3c31675e62667ce3948d6a0dad
        label = self.session_labels[key]
        self.video_window.setWindowTitle(f"Drive Replay Video — ACTIVE — {label}")
        self._refresh_comparison_video_windows()
        if self.video_window.available_source_count() > 0:
            self.video_window.show()
            self.video_window.raise_()
            if self.video_window.video_path is not None:
                self.video_path_edit.setText(str(self.video_window.video_path))
            self.status_label.setText(
                f"Active replay: {label}. {len(self.loaded_sessions)} trajectories visible; "
                f"{self.video_window.available_source_count()} video source(s) available."
            )
        else:
            self.video_path_edit.clear()
            self.status_label.setText(
                f"Active replay: {label}. {len(self.loaded_sessions)} trajectories visible; "
                "no recorded video file was found for the active session."
            )

    def on_active_session_changed(self, index: int) -> None:
        if index < 0:
            return
        key = self.active_session_combo.itemData(index)
        if key and str(key) != self.active_session_key:
            self.set_active_session(str(key), preserve_time=True)

    def remove_active_session(self) -> None:
        key = self.active_session_key
        if key is None or key not in self.loaded_sessions:
            return
        self.clock.pause()
        self._close_comparison_video(key)
        try:
            index = self.session_order.index(key)
        except ValueError:
            index = 0
        self.loaded_sessions.pop(key, None)
        self.session_labels.pop(key, None)
        self.session_colors.pop(key, None)
        self.session_order = [item for item in self.session_order if item != key]

        if self.session_order:
            next_index = min(index, len(self.session_order) - 1)
            self.set_active_session(self.session_order[next_index], preserve_time=False)
        else:
            self.active_session_key = None
            self.session = None
            self.coordinator.set_session(None)
            self.video_path_edit.clear()
            self._refresh_session_ui()
            self.status_label.setText("All recordings removed. Add a session to begin.")

    def clear_sessions(self) -> None:
        self.clock.pause()
        self._close_all_comparison_videos()
        self.loaded_sessions.clear()
        self.session_labels.clear()
        self.session_colors.clear()
        self.session_order.clear()
        self.active_session_key = None
        self.session = None
        self.coordinator.set_session(None)
        self.video_path_edit.clear()
        self._refresh_session_ui()
        self.status_label.setText("All recordings cleared.")

    def set_multi_video_mode(self, enabled: bool) -> None:
        self.multi_video_mode = bool(enabled)
        self.multi_video_button.setText(
            "Close Comparison Videos" if self.multi_video_mode else "Open All Session Videos"
        )
        if self.multi_video_mode:
            self.show_video_window()
            self._refresh_comparison_video_windows()
            count = len(self.comparison_video_windows)
            self.status_label.setText(
                f"Multi-video mode enabled: active video + {count} comparison video window(s)."
            )
        else:
            self._close_all_comparison_videos()
            self.status_label.setText(
                "Multi-video mode disabled. The active replay video remains available."
            )

    def _close_comparison_video(self, key: str) -> None:
        sink = self.comparison_video_sinks.pop(key, None)
        window = self.comparison_video_windows.pop(key, None)
        if sink is not None:
            try:
                sink.set_playing(False)
                sink.close()
            except Exception:
                pass
        elif window is not None:
            try:
                window.close()
            except Exception:
                pass

    def _close_all_comparison_videos(self) -> None:
        for key in list(self.comparison_video_windows):
            self._close_comparison_video(key)

    def _refresh_comparison_video_windows(self) -> None:
        if not self.multi_video_mode:
            return

        desired = {
            key for key in self.session_order
            if key in self.loaded_sessions and key != self.active_session_key
        }
        for key in list(self.comparison_video_windows):
            if key not in desired:
                self._close_comparison_video(key)

        for key in self.session_order:
            if key not in desired or key in self.comparison_video_windows:
                continue
            session = self.loaded_sessions[key]
            label = self.session_labels[key]
            window = VideoWindow()
            window.setWindowTitle(f"Drive Replay Video — {label}")
            sink = VideoReplaySink(window)
            sink.set_session(session)
            if window.available_source_count() <= 0:
                # Do not leave empty windows around when a recording has no
                # discoverable video file.
                sink.close()
                continue
            self.comparison_video_windows[key] = window
            self.comparison_video_sinks[key] = sink
            sink.set_rate(self.clock.rate)
            sink.set_playing(self.clock.playing)
            t = min(self.clock.current_time_s, session.duration_s)
            sink.force_sync()
            sink.update_time(t, session.pose_at(t))
            window.show()
            window.raise_()

    def _sync_comparison_video_time(self, time_s: float) -> None:
        for key, sink in tuple(self.comparison_video_sinks.items()):
            session = self.loaded_sessions.get(key)
            if session is None or not session.times:
                continue
            t = min(max(float(time_s), float(session.times[0])), session.duration_s)
            try:
                sink.update_time(t, session.pose_at(t))
            except Exception as exc:
                self.status_label.setText(
                    f"Comparison video sync failed for {self.session_labels.get(key, key)}: {exc}"
                )

    def _sync_comparison_video_playing(self, playing: bool) -> None:
        for key, sink in tuple(self.comparison_video_sinks.items()):
            session = self.loaded_sessions.get(key)
            effective = bool(playing)
            if session is not None and self.clock.current_time_s >= session.duration_s:
                effective = False
            try:
                sink.set_playing(effective)
            except Exception as exc:
                self.status_label.setText(
                    f"Comparison video playback failed for {self.session_labels.get(key, key)}: {exc}"
                )

    def _sync_comparison_video_rate(self, rate: float) -> None:
        for key, sink in tuple(self.comparison_video_sinks.items()):
            try:
                sink.set_rate(rate)
            except Exception as exc:
                self.status_label.setText(
                    f"Comparison video rate failed for {self.session_labels.get(key, key)}: {exc}"
                )

    def _force_comparison_video_sync(self) -> None:
        for sink in tuple(self.comparison_video_sinks.values()):
            try:
                sink.force_sync()
            except Exception:
                pass

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
        self.map_widget.set_comparison_time(time_s)
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
        self._force_comparison_video_sync()
        self.clock.seek(self.timeline.value() / 1000.0)
        self._slider_dragging = False
        if self._resume_after_drag:
            self.clock.play()
        self._resume_after_drag = False

    def on_map_clicked(self, x: float, y: float) -> None:
        if not self.loaded_sessions:
            self.status_label.setText(
                "Load one or more recordings before seeking from the map."
            )
            return

        radius = self.map_widget.click_radius_m()
        matches: list[tuple[str, PassCandidate]] = []
        nearest: tuple[str, PassCandidate] | None = None
        for key in self.session_order:
            session = self.loaded_sessions[key]
            for candidate in session.find_passes(x, y, radius_m=radius):
                matches.append((key, candidate))
            candidate = session.nearest_sample(x, y)
            if nearest is None or candidate.distance_m < nearest[1].distance_m:
                nearest = (key, candidate)

        if not matches:
            assert nearest is not None
            key, candidate = nearest
            self.status_label.setText(
                f"No loaded recording passes within {radius:.1f} m. Nearest is "
                f"{self.session_labels[key]}: {candidate.distance_m:.1f} m away at "
                f"{format_time_s(candidate.time_s)}."
            )
            return

        if len(matches) == 1:
            self.seek_candidate(matches[0][0], matches[0][1])
            return

        menu = QMenu(self)
        title = menu.addAction(
            f"{len(matches)} pass(es) found across loaded recordings"
        )
        title.setEnabled(False)
        menu.addSeparator()
        for key in self.session_order:
            session_matches = [c for k, c in matches if k == key]
            if not session_matches:
                continue
            header = menu.addAction(self.session_labels[key])
            header.setEnabled(False)
            for number, candidate in enumerate(session_matches, start=1):
                action = menu.addAction(
                    f"    Pass {number}: {format_time_s(candidate.time_s)}   "
                    f"({candidate.distance_m:.1f} m from click)"
                )
                action.triggered.connect(
                    lambda _checked=False, k=key, c=candidate: self.seek_candidate(k, c)
                )
            menu.addSeparator()
        menu.exec(QCursor.pos())

    def seek_candidate(self, key: str, candidate: PassCandidate) -> None:
        if key != self.active_session_key:
            self.set_active_session(key, preserve_time=False)
        self.coordinator.force_sync()
        self._force_comparison_video_sync()
        self.clock.seek(candidate.time_s)
        self.status_label.setText(
            f"Active: {self.session_labels.get(key, key)} — jumped to "
            f"{format_time_s(candidate.time_s)} — recorded point "
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
            self._force_comparison_video_sync()
            self.clock.seek(self.clock.current_time_s - 1.0)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self.coordinator.force_sync()
            self._force_comparison_video_sync()
            self.clock.seek(self.clock.current_time_s + 1.0)
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._close_all_comparison_videos()
        self.coordinator.close()
        super().closeEvent(event)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive QLabs Open Road drive replay."
    )
    parser.add_argument(
        "--session",
        type=Path,
        action="append",
        default=[],
        help="Recorded session folder. Repeat --session to overlay multiple recordings.",
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
        lane_geometry = load_open_road_six_lane_geometry(reference_path)
    except Exception as exc:
        print(f"Could not load Open Road reference: {exc}", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    window = ReplayWindow(reference_path, reference_loop, lane_geometry=lane_geometry)
    for index, session_path in enumerate(args.session):
        window.add_session(session_path, make_active=(index == 0))
    window.show()
    # The reviewer intentionally uses two windows: map/timeline and video.
    window.video_window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
