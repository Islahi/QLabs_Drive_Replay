"""Interactive Open Road drive replay with explicit OOP architecture.

Main window: map + master replay timeline.
Second window: synchronized recorded video.

QLabs transform replay is intentionally not exposed in this version. The
ReplayCoordinator synchronizes only the map and video windows for now; a small
third QLabs replay window can be attached later without changing session files.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
import math
from pathlib import Path
import sys

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from core.replay_core import (
    OpenRoadLaneGeometry,
    RoadCoordinateProjector,
    StraightRoadSample,
    PassCandidate,
    SessionData,
    detect_sustained_motion_start,
    extract_stable_completed_loop,
    find_open_road_reference,
    format_time_s,
    load_open_road_reference,
    load_open_road_six_lane_geometry,
    offset_polyline_xy,
    rdp_simplify,
    write_json_atomic,
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
                "x_shift": float(entry.get("x_shift", 0.0)),
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

    def _make_path(self, points, x_shift: float = 0.0) -> QPainterPath:
        path = QPainterPath()
        if not points:
            return path
        p = self.world_to_screen(float(points[0][0]) + x_shift, float(points[0][1]))
        path.moveTo(p)
        for point in points[1:]:
            p = self.world_to_screen(float(point[0]) + x_shift, float(point[1]))
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
            painter.drawPath(self._make_path(entry["display"], float(entry.get("x_shift", 0.0))))

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
            x_shift = float(entry.get("x_shift", 0.0))
            p = self.world_to_screen(pose.x + x_shift, pose.y)
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


class StartAlignmentDialog(QDialog):
    """Replay-side, non-destructive start alignment editor.

    Detection is only a suggestion. The user can accept the detected launch,
    type an arbitrary raw-session start time, or keep the complete raw start.
    """

    DEFAULT_SETTINGS = {
        "threshold_mps": 0.30,
        "required_motion_s": 1.5,
        "speed_window_s": 0.50,
        "min_displacement_m": 0.30,
    }

    def __init__(self, owner) -> None:
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("Configure Replay Start Alignment")
        self.resize(620, 500)

        layout = QVBoxLayout(self)

        session_row = QHBoxLayout()
        session_row.addWidget(QLabel("Recording:"))
        self.session_combo = QComboBox()
        for key in owner.session_order:
            if key in owner.raw_sessions:
                self.session_combo.addItem(owner.session_labels.get(key, Path(key).name), key)
        self.session_combo.currentIndexChanged.connect(self._load_selected)
        session_row.addWidget(self.session_combo, 1)
        layout.addLayout(session_row)

        detector_group = QGroupBox("Movement detector (suggestion only)")
        detector_form = QFormLayout(detector_group)
        layout.addWidget(detector_group)

        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0.02, 10.0)
        self.threshold_spin.setDecimals(2)
        self.threshold_spin.setSingleStep(0.05)
        self.threshold_spin.setSuffix(" m/s")
        detector_form.addRow("Rolling speed threshold", self.threshold_spin)

        self.required_spin = QDoubleSpinBox()
        self.required_spin.setRange(0.1, 10.0)
        self.required_spin.setDecimals(1)
        self.required_spin.setSingleStep(0.1)
        self.required_spin.setSuffix(" s")
        detector_form.addRow("Sustained movement", self.required_spin)

        self.window_spin = QDoubleSpinBox()
        self.window_spin.setRange(0.05, 3.0)
        self.window_spin.setDecimals(2)
        self.window_spin.setSingleStep(0.05)
        self.window_spin.setSuffix(" s")
        detector_form.addRow("Speed averaging window", self.window_spin)

        self.displacement_spin = QDoubleSpinBox()
        self.displacement_spin.setRange(0.0, 10.0)
        self.displacement_spin.setDecimals(2)
        self.displacement_spin.setSingleStep(0.05)
        self.displacement_spin.setSuffix(" m")
        detector_form.addRow("Minimum distance from start", self.displacement_spin)

        detect_row = QHBoxLayout()
        self.detect_button = QPushButton("Detect selected")
        self.detect_button.clicked.connect(self.detect_selected)
        detect_row.addWidget(self.detect_button)
        self.detect_all_button = QPushButton("Detect all loaded recordings")
        self.detect_all_button.clicked.connect(self.detect_all)
        detect_row.addWidget(self.detect_all_button)
        detect_row.addStretch(1)
        detector_form.addRow(detect_row)

        self.detected_label = QLabel("Detected movement: not calculated")
        self.detected_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detector_form.addRow(self.detected_label)
        self.detected_position_label = QLabel("Detected position: —")
        self.detected_position_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detector_form.addRow(self.detected_position_label)

        choice_group = QGroupBox("Replay 00:00 choice")
        choice_form = QFormLayout(choice_group)
        layout.addWidget(choice_group)

        self.detected_radio = QRadioButton("Use detected movement start")
        self.detected_radio.toggled.connect(self._update_manual_enabled)
        choice_form.addRow(self.detected_radio)

        manual_row = QHBoxLayout()
        self.manual_radio = QRadioButton("Use manual raw time")
        self.manual_radio.toggled.connect(self._update_manual_enabled)
        manual_row.addWidget(self.manual_radio)
        self.manual_spin = QDoubleSpinBox()
        self.manual_spin.setRange(0.0, 100000.0)
        self.manual_spin.setDecimals(3)
        self.manual_spin.setSingleStep(0.1)
        self.manual_spin.setSuffix(" s")
        manual_row.addWidget(self.manual_spin, 1)
        choice_form.addRow(manual_row)

        self.raw_radio = QRadioButton("Use original recording start (no alignment)")
        self.raw_radio.toggled.connect(self._update_manual_enabled)
        choice_form.addRow(self.raw_radio)

        self.chosen_label = QLabel("Replay 00:00 → raw 00:00:00.000")
        self.chosen_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        choice_form.addRow("Current choice", self.chosen_label)

        self.save_check = QCheckBox("Save this replay choice in session.json")
        self.save_check.setChecked(True)
        self.save_check.setToolTip(
            "Only replay metadata is updated. MP4 files and telemetry CSVs are never edited or cut."
        )
        layout.addWidget(self.save_check)

        note = QLabel(
            "Detection is not automatic recording trim. It is a Replay-side suggestion. "
            "Use Preview to inspect the proposed launch, then Apply only if it is correct."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QHBoxLayout()
        self.preview_button = QPushButton("Preview chosen start")
        self.preview_button.clicked.connect(self.preview_choice)
        buttons.addWidget(self.preview_button)
        self.apply_button = QPushButton("Apply to selected recording")
        self.apply_button.clicked.connect(self.apply_choice)
        buttons.addWidget(self.apply_button)
        buttons.addStretch(1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        # Keep the displayed choice label synchronized with numeric/radio edits.
        self.manual_spin.valueChanged.connect(self._refresh_choice_label)
        self.detected_radio.toggled.connect(self._refresh_choice_label)
        self.manual_radio.toggled.connect(self._refresh_choice_label)
        self.raw_radio.toggled.connect(self._refresh_choice_label)

        active = owner.active_session_key
        if active is not None:
            index = self.session_combo.findData(active)
            if index >= 0:
                self.session_combo.setCurrentIndex(index)
        self._load_selected()

    def _current_key(self) -> str | None:
        value = self.session_combo.currentData()
        return str(value) if value else None

    def _settings(self) -> dict:
        return {
            "threshold_mps": float(self.threshold_spin.value()),
            "required_motion_s": float(self.required_spin.value()),
            "speed_window_s": float(self.window_spin.value()),
            "min_displacement_m": float(self.displacement_spin.value()),
        }

    def _load_selected(self, *_args) -> None:
        key = self._current_key()
        if key is None or key not in self.owner.raw_sessions:
            return
        raw = self.owner.raw_sessions[key]
        settings = dict(self.DEFAULT_SETTINGS)
        settings.update(self.owner.alignment_detection_settings.get(key, {}))
        self.threshold_spin.setValue(float(settings["threshold_mps"]))
        self.required_spin.setValue(float(settings["required_motion_s"]))
        self.window_spin.setValue(float(settings["speed_window_s"]))
        self.displacement_spin.setValue(float(settings["min_displacement_m"]))
        self.manual_spin.setMaximum(max(0.0, raw.raw_duration_s))

        detected = self.owner.detected_motion_starts.get(key)
        configured = float(self.owner.session_start_offsets.get(key, 0.0))
        mode = self.owner.session_start_modes.get(key, "raw")
        self.manual_spin.setValue(configured)
        if mode == "detected" and detected is not None:
            self.detected_radio.setChecked(True)
        elif mode == "manual" and configured > 0.0:
            self.manual_radio.setChecked(True)
        else:
            self.raw_radio.setChecked(True)
        self._show_detected(key)
        self._update_manual_enabled()
        self._refresh_choice_label()

    def _show_detected(self, key: str) -> None:
        detected = self.owner.detected_motion_starts.get(key)
        if detected is None:
            self.detected_label.setText("Detected movement: not calculated / not found")
            self.detected_position_label.setText("Detected position: —")
            return
        raw = self.owner.raw_sessions[key]
        pose = raw.pose_at(float(detected))
        self.detected_label.setText(
            f"Detected movement: raw {format_time_s(float(detected))} ({float(detected):.3f} s)"
        )
        self.detected_position_label.setText(
            f"Detected position: X {pose.x:.3f} m   Y {pose.y:.3f} m   Z {pose.z:.3f} m"
        )

    def _detect_key(self, key: str) -> float | None:
        raw = self.owner.raw_sessions[key]
        settings = self._settings()
        detected = detect_sustained_motion_start(
            raw.times,
            raw.xs,
            raw.ys,
            threshold_mps=settings["threshold_mps"],
            required_motion_s=settings["required_motion_s"],
            speed_window_s=settings["speed_window_s"],
            min_displacement_m=settings["min_displacement_m"],
        )
        self.owner.detected_motion_starts[key] = detected
        self.owner.alignment_detection_settings[key] = dict(settings)
        return detected

    def detect_selected(self) -> None:
        key = self._current_key()
        if key is None:
            return
        detected = self._detect_key(key)
        self._show_detected(key)
        if detected is not None:
            self.manual_spin.setValue(float(detected))
            self.detected_radio.setChecked(True)
        else:
            QMessageBox.information(
                self,
                "No sustained movement found",
                "The current detector settings did not find a sustained vehicle start. "
                "Adjust the settings or choose a manual start time.",
            )
        self._refresh_choice_label()

    def detect_all(self) -> None:
        found = 0
        missed: list[str] = []
        for key in self.owner.session_order:
            if key not in self.owner.raw_sessions:
                continue
            detected = self._detect_key(key)
            if detected is None:
                missed.append(self.owner.session_labels.get(key, Path(key).name))
            else:
                found += 1
        current = self._current_key()
        if current:
            self._show_detected(current)
        message = f"Detected a movement start for {found} recording(s)."
        if missed:
            message += "\n\nNo start found for:\n" + "\n".join(missed)
        QMessageBox.information(self, "Detection complete", message)

    def _chosen(self) -> tuple[float, str] | None:
        key = self._current_key()
        if key is None:
            return None
        if self.raw_radio.isChecked():
            return 0.0, "raw"
        if self.manual_radio.isChecked():
            return float(self.manual_spin.value()), "manual"
        detected = self.owner.detected_motion_starts.get(key)
        if detected is None:
            return None
        return float(detected), "detected"

    def _update_manual_enabled(self, *_args) -> None:
        self.manual_spin.setEnabled(self.manual_radio.isChecked())

    def _refresh_choice_label(self, *_args) -> None:
        chosen = self._chosen()
        if chosen is None:
            self.chosen_label.setText("Replay 00:00 → no detected start selected")
            return
        start_s, mode = chosen
        self.chosen_label.setText(
            f"Replay 00:00 → raw {format_time_s(start_s)} ({start_s:.3f} s, {mode})"
        )

    def preview_choice(self) -> None:
        key = self._current_key()
        chosen = self._chosen()
        if key is None or chosen is None:
            QMessageBox.warning(self, "Nothing to preview", "Detect a start or enter a manual time first.")
            return
        self.owner.preview_raw_start(key, chosen[0])

    def apply_choice(self) -> None:
        key = self._current_key()
        chosen = self._chosen()
        if key is None or chosen is None:
            QMessageBox.warning(self, "No valid start", "Detect a start or enter a manual start time first.")
            return
        start_s, mode = chosen
        self.owner.apply_replay_start_choice(
            key,
            start_s,
            mode,
            detected_start=self.owner.detected_motion_starts.get(key),
            settings=self._settings(),
            persist=self.save_check.isChecked(),
        )
        self._refresh_choice_label()



class StraightRoadLiveView(QWidget):
    """Interactive live 50 km straight-road replay view.

    Unlike the export plot, this behaves like the normal map: loaded trajectories
    stay visible, every driver's current position moves with the replay clock,
    and the user can zoom/pan/seek directly on the straightened road.
    """

    roadClicked = Signal(float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(1050, 560)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.entries: dict[str, dict] = {}
        self.current_time_s = 0.0
        self.x_min_km = 0.0
        self.x_max_km = 50.0
        self.view_center_km = 25.0
        self.view_span_km = 50.0
        self.lat_min = -13.0
        self.lat_max = 13.0
        self.follow_active = False
        self._pan_active = False
        self._pan_last = QPointF()
        self.hover_road: tuple[float, float] | None = None
        self.last_clicked_road: tuple[float, float] | None = None

    def set_entries(self, entries: list[dict]) -> None:
        self.entries = {str(entry["key"]): entry for entry in entries}
        self.update()

    def set_follow_active(self, enabled: bool) -> None:
        self.follow_active = bool(enabled)
        if self.follow_active:
            self._follow_current_active()
        self.update()

    def set_time(self, time_s: float) -> None:
        self.current_time_s = float(time_s)
        if self.follow_active:
            self._follow_current_active()
        self.update()

    def fit_all(self) -> None:
        self.view_span_km = 50.0
        self.view_center_km = 25.0
        self.update()

    def set_view_span(self, span_km: float) -> None:
        self.view_span_km = max(0.25, min(50.0, float(span_km)))
        self._clamp_view()
        if self.follow_active:
            self._follow_current_active()
        self.update()

    def _active_entry(self) -> dict | None:
        for entry in self.entries.values():
            if entry.get("active"):
                return entry
        return None

    @staticmethod
    def _sample_at_time(entry: dict, time_s: float) -> StraightRoadSample | None:
        samples: list[StraightRoadSample] = entry.get("samples", [])
        times: list[float] = entry.get("times", [])
        if not samples or not times or time_s < times[0] or time_s > times[-1]:
            return None
        right = bisect_right(times, float(time_s))
        index = max(0, min(len(samples) - 1, right - 1))
        return samples[index]

    def _follow_current_active(self) -> None:
        if self.view_span_km >= 49.999:
            return
        entry = self._active_entry()
        if entry is None:
            return
        sample = self._sample_at_time(entry, self.current_time_s)
        if sample is None:
            return
        margin = self.view_span_km * 0.22
        left, right = self._visible_range()
        x = float(sample.display_distance_km)
        if x < left + margin or x > right - margin:
            self.view_center_km = x
            self._clamp_view()

    def _visible_range(self) -> tuple[float, float]:
        half = self.view_span_km / 2.0
        return self.view_center_km - half, self.view_center_km + half

    def _clamp_view(self) -> None:
        half = self.view_span_km / 2.0
        if self.view_span_km >= 50.0:
            self.view_center_km = 25.0
            return
        self.view_center_km = max(half, min(50.0 - half, self.view_center_km))

    def _view_rect(self):
        left = 16.0
        right = 16.0
        top = 34.0
        bottom = 30.0
        return left, top, max(1.0, self.width() - left - right), max(1.0, self.height() - top - bottom)

    def road_to_screen(self, distance_km: float, lateral_m: float) -> QPointF:
        left, top, width, height = self._view_rect()
        view_left, view_right = self._visible_range()
        x = left + (float(distance_km) - view_left) / max(1e-9, view_right - view_left) * width
        y = top + (self.lat_max - float(lateral_m)) / (self.lat_max - self.lat_min) * height
        return QPointF(x, y)

    def screen_to_road(self, pos: QPointF) -> tuple[float, float]:
        left, top, width, height = self._view_rect()
        view_left, view_right = self._visible_range()
        distance_km = view_left + (pos.x() - left) / max(1e-9, width) * (view_right - view_left)
        lateral_m = self.lat_max - (pos.y() - top) / max(1e-9, height) * (self.lat_max - self.lat_min)
        return distance_km, lateral_m

    def _draw_road(self, painter: QPainter) -> None:
        left, top, width, height = self._view_rect()
        view_left, view_right = self._visible_range()

        # Constant-width straightened road. The vertical scale is intentionally
        # exaggerated relative to the 50 km longitudinal axis so lane drift is
        # easy to see during live playback.
        upper_top = self.road_to_screen(view_left, 12.0).y()
        upper_bottom = self.road_to_screen(view_left, 0.6).y()
        lower_top = self.road_to_screen(view_left, -0.6).y()
        lower_bottom = self.road_to_screen(view_left, -12.0).y()
        painter.fillRect(int(left), int(upper_top), int(width), int(upper_bottom - upper_top), QColor(75, 81, 90))
        painter.fillRect(int(left), int(lower_top), int(width), int(lower_bottom - lower_top), QColor(75, 81, 90))
        median_top = self.road_to_screen(view_left, 0.6).y()
        median_bottom = self.road_to_screen(view_left, -0.6).y()
        painter.fillRect(int(left), int(median_top), int(width), int(median_bottom - median_top), QColor(164, 155, 128))

        edge_pen = QPen(QColor(248, 249, 251), 3.0)
        edge_pen.setCosmetic(True)
        painter.setPen(edge_pen)
        for lateral in (12.0, 0.6, -0.6, -12.0):
            painter.drawLine(self.road_to_screen(view_left, lateral), self.road_to_screen(view_right, lateral))

        divider_pen = QPen(QColor(252, 252, 252), 3.0)
        divider_pen.setCosmetic(True)
        divider_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(divider_pen)
        for lateral in (8.0, 4.0, -4.0, -8.0):
            painter.drawLine(self.road_to_screen(view_left, lateral), self.road_to_screen(view_right, lateral))

        center_pen = QPen(QColor(132, 158, 178, 125), 1.0)
        center_pen.setCosmetic(True)
        center_pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(center_pen)
        for lateral in (10.0, 6.0, 2.0, -2.0, -6.0, -10.0):
            painter.drawLine(self.road_to_screen(view_left, lateral), self.road_to_screen(view_right, lateral))

        # Kilometer posts rather than chart axes. Spacing adapts to zoom.
        span = self.view_span_km
        if span > 30.0:
            step = 5.0
        elif span > 12.0:
            step = 2.0
        elif span > 5.0:
            step = 1.0
        elif span > 2.0:
            step = 0.5
        else:
            step = 0.1
        first = math.ceil(max(0.0, view_left) / step) * step
        km = first
        while km <= min(50.0, view_right) + 1e-9:
            x = self.road_to_screen(km, 0.0).x()
            grid_pen = QPen(QColor(125, 132, 142, 70), 1.0)
            grid_pen.setCosmetic(True)
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(x, top), QPointF(x, top + height))
            painter.setPen(QColor(225, 229, 234))
            label = f"{km:.1f} km" if step < 1.0 else f"{km:g} km"
            painter.drawText(QPointF(x + 4.0, top + 16.0), label)
            km += step

        lane_labels = [
            (10.0, "Upper Right"), (6.0, "Upper Middle"), (2.0, "Upper Left"),
            (-2.0, "Lower Right"), (-6.0, "Lower Middle"), (-10.0, "Lower Left"),
        ]
        painter.setPen(QColor(222, 227, 232, 210))
        for lateral, label in lane_labels:
            y = self.road_to_screen(view_left, lateral).y()
            painter.drawText(QPointF(left + 8.0, y - 5.0), label)

    def _draw_trajectories(self, painter: QPainter) -> None:
        view_left, view_right = self._visible_range()
        items = list(self.entries.values())
        items.sort(key=lambda entry: bool(entry.get("active")))
        for entry in items:
            samples: list[StraightRoadSample] = entry.get("samples", [])
            if not samples:
                continue
            path = QPainterPath()
            started = False
            for sample in samples:
                d = float(sample.display_distance_km)
                if d < view_left - 0.05 or d > view_right + 0.05:
                    if started and d > view_right:
                        break
                    continue
                p = self.road_to_screen(d, sample.lateral_m)
                if not started:
                    path.moveTo(p)
                    started = True
                else:
                    path.lineTo(p)
            if not started:
                continue
            color = QColor(*entry["color"])
            pen = QPen(color, 3.4 if entry.get("active") else 2.2)
            pen.setCosmetic(True)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        marker_row = 0
        for entry in items:
            current = self._sample_at_time(entry, self.current_time_s)
            if current is None:
                continue
            if not (view_left <= current.display_distance_km <= view_right):
                continue
            p = self.road_to_screen(current.display_distance_km, current.lateral_m)
            color = QColor(*entry["color"])
            active = bool(entry.get("active"))
            painter.setPen(QPen(QColor(246, 249, 251), 2.0 if active else 1.2))
            fill = QColor(color)
            fill.setAlpha(225)
            painter.setBrush(fill)
            radius = 8.0 if active else 6.0
            painter.drawEllipse(p, radius, radius)

            painter.setPen(QColor(238, 242, 246))
            label = str(entry["label"])
            if len(label) > 24:
                label = label[:21] + "…"
            prefix = "ACTIVE: " if active else ""
            painter.drawText(p + QPointF(10.0, -10.0 - (marker_row % 2) * 11.0), prefix + label)
            marker_row += 1

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(31, 35, 41))
        self._draw_road(painter)
        self._draw_trajectories(painter)

        painter.setPen(QColor(228, 233, 238))
        view_left, view_right = self._visible_range()
        painter.drawText(16, 22, f"Live straightened Open Road   view {max(0.0, view_left):.2f}–{min(50.0, view_right):.2f} km")
        if self.hover_road is not None:
            painter.drawText(
                max(16, self.width() - 310), 22,
                f"Cursor {self.hover_road[0]:.3f} km   lateral {self.hover_road[1]:+.2f} m",
            )
        painter.setPen(QColor(184, 192, 201))
        painter.drawText(
            16,
            self.height() - 9,
            "Colored lines: full recorded paths   Dots: live replay positions   |   Left click: seek   Wheel: zoom   Middle/right drag: pan",
        )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        self.hover_road = self.screen_to_road(pos)
        if self._pan_active:
            delta = pos - self._pan_last
            _left, _top, width, _height = self._view_rect()
            self.view_center_km -= delta.x() / max(1.0, width) * self.view_span_km
            self._clamp_view()
            self._pan_last = pos
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._pan_active = True
            self._pan_last = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            distance_km, lateral_m = self.screen_to_road(event.position())
            distance_km = max(0.0, min(50.0, distance_km))
            self.last_clicked_road = (distance_km, lateral_m)
            self.roadClicked.emit(distance_km, lateral_m)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._pan_active = False
            self.unsetCursor()

    def wheelEvent(self, event: QWheelEvent) -> None:
        before_km, _ = self.screen_to_road(event.position())
        steps = event.angleDelta().y() / 120.0
        new_span = max(0.25, min(50.0, self.view_span_km / (1.25 ** steps)))
        if abs(new_span - self.view_span_km) < 1e-12:
            return
        left, _top, width, _height = self._view_rect()
        fraction = (event.position().x() - left) / max(1.0, width)
        fraction = max(0.0, min(1.0, fraction))
        self.view_span_km = new_span
        self.view_center_km = before_km - (fraction - 0.5) * new_span
        self._clamp_view()
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.fit_all()


class StraightRoadExportPlot(QWidget):
    """Publication/export plot; created only when the user exports a PNG."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.entries: dict[str, dict] = {}
        self.current_time_s = 0.0
        self.x_max_km = 50.0
        self.lat_min = -13.0
        self.lat_max = 13.0

    def set_entries(self, entries: list[dict]) -> None:
        self.entries = {str(entry["key"]): entry for entry in entries}
        self.update()

    def set_time(self, time_s: float) -> None:
        self.current_time_s = float(time_s)
        self.update()

    def _plot_rect(self):
        left = 92.0
        right = 24.0
        top = 38.0
        bottom = 58.0
        return left, top, max(1.0, self.width() - left - right), max(1.0, self.height() - top - bottom)

    def _screen(self, distance_km: float, lateral_m: float) -> QPointF:
        left, top, width, height = self._plot_rect()
        x = left + max(0.0, min(self.x_max_km, float(distance_km))) / self.x_max_km * width
        y = top + (self.lat_max - float(lateral_m)) / (self.lat_max - self.lat_min) * height
        return QPointF(x, y)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(28, 31, 36))
        left, top, width, height = self._plot_rect()

        upper_top = self._screen(0.0, 12.0).y()
        upper_bottom = self._screen(0.0, 0.6).y()
        lower_top = self._screen(0.0, -0.6).y()
        lower_bottom = self._screen(0.0, -12.0).y()
        painter.fillRect(int(left), int(upper_top), int(width), int(upper_bottom - upper_top), QColor(75, 81, 90))
        painter.fillRect(int(left), int(lower_top), int(width), int(lower_bottom - lower_top), QColor(75, 81, 90))
        median_top = self._screen(0.0, 0.6).y()
        median_bottom = self._screen(0.0, -0.6).y()
        painter.fillRect(int(left), int(median_top), int(width), int(median_bottom - median_top), QColor(164, 155, 128))

        for km in range(0, 51, 5):
            p0 = self._screen(float(km), self.lat_min)
            p1 = self._screen(float(km), self.lat_max)
            grid_pen = QPen(QColor(120, 126, 134, 75 if km % 10 else 115), 1.0)
            grid_pen.setCosmetic(True)
            painter.setPen(grid_pen)
            painter.drawLine(p0, p1)
            painter.setPen(QColor(218, 223, 228))
            painter.drawText(QPointF(p0.x() - 12.0, top + height + 24.0), f"{km}")

        edge_pen = QPen(QColor(248, 249, 251), 2.8)
        edge_pen.setCosmetic(True)
        painter.setPen(edge_pen)
        for lateral in (12.0, 0.6, -0.6, -12.0):
            painter.drawLine(self._screen(0.0, lateral), self._screen(50.0, lateral))

        divider_pen = QPen(QColor(250, 250, 250), 2.6)
        divider_pen.setCosmetic(True)
        divider_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(divider_pen)
        for lateral in (8.0, 4.0, -4.0, -8.0):
            painter.drawLine(self._screen(0.0, lateral), self._screen(50.0, lateral))

        center_pen = QPen(QColor(132, 158, 178, 145), 1.0)
        center_pen.setCosmetic(True)
        center_pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(center_pen)
        lane_labels = [
            (10.0, "Upper Right"), (6.0, "Upper Middle"), (2.0, "Upper Left"),
            (-2.0, "Lower Right"), (-6.0, "Lower Middle"), (-10.0, "Lower Left"),
        ]
        for lateral, label in lane_labels:
            painter.drawLine(self._screen(0.0, lateral), self._screen(50.0, lateral))
            painter.setPen(QColor(220, 225, 230))
            y = self._screen(0.0, lateral).y() + 4.0
            painter.drawText(QPointF(8.0, y), label)
            painter.setPen(center_pen)

        for entry in self.entries.values():
            samples = entry.get("samples", [])
            if not samples:
                continue
            path = QPainterPath()
            first = self._screen(samples[0].display_distance_km, samples[0].lateral_m)
            path.moveTo(first)
            for sample in samples[1:]:
                if sample.display_distance_km < -0.1 or sample.display_distance_km > 50.1:
                    continue
                path.lineTo(self._screen(sample.display_distance_km, sample.lateral_m))
            color = QColor(*entry["color"])
            pen = QPen(color, 2.4 if entry.get("active") else 1.8)
            pen.setCosmetic(True)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        painter.setPen(QColor(235, 239, 243))
        painter.drawText(QPointF(left + width / 2.0 - 90.0, self.height() - 12.0), "Normalized route distance (km)")
        painter.drawText(QPointF(left, 22.0), "Straightened Open Road — lateral position (m); lane centers ±10, ±6, ±2")

        legend_x = left + width - 260.0
        legend_y = top + 20.0
        for row, entry in enumerate(list(self.entries.values())[:10]):
            color = QColor(*entry["color"])
            pen = QPen(color, 3.0 if entry.get("active") else 2.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(QPointF(legend_x, legend_y + row * 18), QPointF(legend_x + 24.0, legend_y + row * 18))
            painter.setPen(QColor(235, 239, 243))
            label = str(entry["label"])
            prefix = "ACTIVE — " if entry.get("active") else ""
            painter.drawText(QPointF(legend_x + 31.0, legend_y + row * 18 + 5.0), prefix + label[:28])


class StraightRoadAnalysisWindow(QMainWindow):
    """Live multi-session straight-road replay with CSV/plot export."""

    def __init__(self, owner, projector: RoadCoordinateProjector) -> None:
        super().__init__(owner)
        self.owner = owner
        self.projector = projector
        self.setWindowTitle("50 km Straightened Open Road — Live Analysis")
        self.resize(1320, 720)
        self._plot_cache: dict[tuple[str, int, bool], list[StraightRoadSample]] = {}
        self._entries: list[dict] = []

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        controls = QHBoxLayout()
        layout.addLayout(controls)
        self.normalize_check = QCheckBox("Start each recording at 0 km")
        self.normalize_check.setChecked(True)
        self.normalize_check.setToolTip(
            "Subtract each recording's route station at Replay 00:00. This is the recommended spatial alignment for driver comparison."
        )
        self.normalize_check.toggled.connect(lambda _checked: self.refresh_from_owner())
        controls.addWidget(self.normalize_check)

        self.follow_check = QCheckBox("Follow active car")
        self.follow_check.setChecked(False)
        self.follow_check.setToolTip("When zoomed in, automatically pan the straight-road view as the active car moves.")
        self.follow_check.toggled.connect(self._set_follow)
        controls.addWidget(self.follow_check)

        fit_button = QPushButton("Fit 50 km")
        fit_button.clicked.connect(self._fit_all)
        controls.addWidget(fit_button)

        zoom5_button = QPushButton("5 km view")
        zoom5_button.clicked.connect(lambda: self.live_view.set_view_span(5.0))
        controls.addWidget(zoom5_button)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_from_owner)
        controls.addWidget(refresh)

        export_csv = QPushButton("Export Analysis CSV…")
        export_csv.clicked.connect(self.export_csv)
        controls.addWidget(export_csv)

        export_png = QPushButton("Export Plot PNG…")
        export_png.setToolTip("Generate the full 0–50 km analysis plot only when exporting. The on-screen view remains the live road replay.")
        export_png.clicked.connect(self.export_png)
        controls.addWidget(export_png)
        controls.addStretch(1)

        self.live_view = StraightRoadLiveView()
        self.live_view.roadClicked.connect(self._seek_from_live_view)
        layout.addWidget(self.live_view, 1)
        self.status = QLabel(
            "Live 50 km road replay. Car markers move with the master timeline; wheel zoom and middle/right drag work like the normal map."
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def _set_follow(self, enabled: bool) -> None:
        self.live_view.set_follow_active(enabled)

    def _fit_all(self) -> None:
        self.follow_check.setChecked(False)
        self.live_view.fit_all()

    def refresh_from_owner(self) -> None:
        entries: list[dict] = []
        normalize = self.normalize_check.isChecked()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for key in self.owner.session_order:
                session = self.owner.loaded_sessions.get(key)
                if session is None:
                    continue
                cache_key = (key, id(session), bool(normalize))
                samples = self._plot_cache.get(cache_key)
                if samples is None:
                    samples = self.projector.straightened_samples(
                        session,
                        normalize_start=normalize,
                        display_length_m=50_000.0,
                        max_points=7000,
                    )
                    self._plot_cache[cache_key] = samples
                entries.append({
                    "key": key,
                    "label": self.owner.session_labels.get(key, Path(key).name),
                    "color": self.owner.session_colors.get(key, (52, 183, 245)),
                    "active": key == self.owner.active_session_key,
                    "samples": samples,
                    "times": [sample.time_s for sample in samples],
                })
        finally:
            QApplication.restoreOverrideCursor()
        self._entries = entries
        self.live_view.set_entries(entries)
        self.live_view.set_time(self.owner.clock.current_time_s)
        self.live_view.set_follow_active(self.follow_check.isChecked())
        self.status.setText(
            f"{len(entries)} recording(s) in the live straight-road view. One reference lap ({self.projector.length_m/1000.0:.2f} km) is normalized to 50.00 km. "
            "Use the timeline/video normally; the car markers here update at the same replay time."
        )

    def set_time(self, time_s: float) -> None:
        self.live_view.set_time(time_s)

    def _seek_from_live_view(self, distance_km: float, lateral_m: float) -> None:
        if not self._entries:
            return
        # Select the recording whose straightened trajectory passes nearest the
        # click, then seek that session to the corresponding replay timestamp.
        best: tuple[float, dict, StraightRoadSample] | None = None
        for entry in self._entries:
            samples: list[StraightRoadSample] = entry.get("samples", [])
            if not samples:
                continue
            # Horizontal distance is converted back to metres so both dimensions
            # participate in one intuitive nearest-point score.
            sample = min(
                samples,
                key=lambda item: (
                    (float(item.display_distance_km) - float(distance_km)) * 1000.0
                ) ** 2 + (float(item.lateral_m) - float(lateral_m)) ** 2,
            )
            score = ((sample.display_distance_km - distance_km) * 1000.0) ** 2 + (sample.lateral_m - lateral_m) ** 2
            if best is None or score < best[0]:
                best = (score, entry, sample)
        if best is None:
            return
        _score, entry, sample = best
        key = str(entry["key"])
        if key != self.owner.active_session_key:
            self.owner.set_active_session(key, preserve_time=False)
        self.owner.coordinator.force_sync()
        self.owner._force_comparison_video_sync()
        self.owner.clock.seek(float(sample.time_s))
        self.status.setText(
            f"Seek: {entry['label']} at {sample.display_distance_km:.3f} km, lateral {sample.lateral_m:+.2f} m, replay {format_time_s(sample.time_s)}."
        )

    def export_png(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export straightened-road plot", "straightened_open_road.png", "PNG image (*.png)"
        )
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        if not self._entries:
            QMessageBox.information(self, "Nothing to export", "Load at least one recording first.")
            return

        # The chart-style plot exists only for export. It is rendered off-screen
        # and is never used as the live analysis UI.
        plot = StraightRoadExportPlot()
        plot.resize(1800, 900)
        plot.set_entries(self._entries)
        plot.set_time(self.owner.clock.current_time_s)
        image = QImage(1800, 900, QImage.Format.Format_ARGB32)
        image.fill(QColor(28, 31, 36))
        painter = QPainter(image)
        plot.render(painter)
        painter.end()
        plot.deleteLater()
        if not image.save(path, "PNG"):
            QMessageBox.warning(self, "Export failed", f"Could not save PNG:\n{path}")
            return
        self.status.setText(f"Full 0–50 km analysis plot exported to {path}")

    def export_csv(self) -> None:
        if not self.owner.loaded_sessions:
            QMessageBox.information(self, "Nothing to export", "Load at least one recording first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export straightened-road data", "straightened_open_road.csv", "CSV file (*.csv)"
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        normalize = self.normalize_check.isChecked()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    "recording", "replay_time_s", "world_x_m", "world_y_m", "world_z_m",
                    "route_station_m", "route_progress_m", "display_distance_km",
                    "lateral_m", "nearest_lane", "lane_center_m", "lane_error_m",
                ])
                for key in self.owner.session_order:
                    session = self.owner.loaded_sessions.get(key)
                    if session is None:
                        continue
                    label = self.owner.session_labels.get(key, Path(key).name)
                    samples = self.projector.straightened_samples(
                        session,
                        normalize_start=normalize,
                        display_length_m=50_000.0,
                        max_points=None,
                    )
                    for sample in samples:
                        writer.writerow([
                            label,
                            f"{sample.time_s:.6f}", f"{sample.x:.6f}", f"{sample.y:.6f}", f"{sample.z:.6f}",
                            f"{sample.route_station_m:.6f}", f"{sample.route_progress_m:.6f}", f"{sample.display_distance_km:.9f}",
                            f"{sample.lateral_m:.6f}", sample.lane_name, f"{sample.lane_center_m:.6f}", f"{sample.lane_error_m:.6f}",
                        ])
        except Exception as exc:
            QMessageBox.critical(self, "CSV export failed", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.status.setText(f"Analysis CSV exported to {path}")


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
        self.lane_geometry = lane_geometry
        self.road_projector = (
            RoadCoordinateProjector(
                lane_geometry.median_center,
                positive_side_point=lane_geometry.lane_centers["upper_middle"][0],
            )
            if lane_geometry is not None else None
        )
        self.analysis_window: StraightRoadAnalysisWindow | None = None
        self.session: SessionData | None = None
        # Raw recordings are always kept in memory unchanged. loaded_sessions
        # contains the current Replay view (raw or non-destructively shifted).
        self.raw_sessions: dict[str, SessionData] = {}
        self.loaded_sessions: dict[str, SessionData] = {}
        self.session_start_offsets: dict[str, float] = {}
        self.session_start_modes: dict[str, str] = {}
        self.detected_motion_starts: dict[str, float | None] = {}
        self.alignment_detection_settings: dict[str, dict] = {}
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

        self.apply_trim_check = QCheckBox("Use configured starts")
        self.apply_trim_check.setChecked(True)
        self.apply_trim_check.setToolTip(
            "Use the Replay-side start choices configured for each recording. "
            "Turn this off at any time to inspect every raw recording from its original start."
        )
        self.apply_trim_check.toggled.connect(self.on_alignment_mode_changed)
        top.addWidget(self.apply_trim_check)

        self.configure_starts_button = QPushButton("Configure Starts…")
        self.configure_starts_button.setToolTip(
            "Detect, preview, accept, or manually edit each recording's non-destructive replay start."
        )
        self.configure_starts_button.clicked.connect(self.configure_starts)
        self.configure_starts_button.setEnabled(False)
        top.addWidget(self.configure_starts_button)

        self.session_label = QLabel("0 recordings loaded")
        self.session_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top.addWidget(self.session_label, 1)

        self.reset_map_button = QPushButton("Reset Map")
        top.addWidget(self.reset_map_button)

        position_row = QHBoxLayout()
        outer.addLayout(position_row)
        self.align_map_x_check = QCheckBox("Align map start X")
        self.align_map_x_check.setToolTip(
            "Display-only normalization: shift each trajectory in X so its Replay 00:00 point shares the target X. Y is preserved, so different lanes remain separate."
        )
        self.align_map_x_check.toggled.connect(self.on_position_alignment_changed)
        position_row.addWidget(self.align_map_x_check)
        position_row.addWidget(QLabel("Target X"))
        self.align_map_x_spin = QDoubleSpinBox()
        self.align_map_x_spin.setRange(-10000.0, 10000.0)
        self.align_map_x_spin.setDecimals(3)
        self.align_map_x_spin.setSingleStep(0.1)
        self.align_map_x_spin.setValue(-0.084)
        self.align_map_x_spin.setSuffix(" m")
        self.align_map_x_spin.valueChanged.connect(self.on_position_alignment_changed)
        position_row.addWidget(self.align_map_x_spin)

        self.straight_analysis_button = QPushButton("50 km Live Analysis…")
        self.straight_analysis_button.setToolTip(
            "Open a live synchronized straightened 0–50 km six-lane road view; export CSV or a full plot only when needed."
        )
        self.straight_analysis_button.clicked.connect(self.show_straight_analysis)
        self.straight_analysis_button.setEnabled(lane_geometry is not None)
        position_row.addWidget(self.straight_analysis_button)
        position_row.addStretch(1)

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
        self.clock.timeChanged.connect(self._sync_straight_analysis_time)
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
                "x_shift": self._map_x_shift(key),
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
                f"configured +{active.analysis_start_s:.2f}s"
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
        self.configure_starts_button.setEnabled(count > 0)
        self.straight_analysis_button.setEnabled(count > 0 and self.road_projector is not None)
        if self.analysis_window is not None and self.analysis_window.isVisible():
            self.analysis_window.refresh_from_owner()

    def _map_x_shift(self, key: str) -> float:
        session = self.loaded_sessions.get(key)
        if session is None or not session.xs or not self.align_map_x_check.isChecked():
            return 0.0
        return float(self.align_map_x_spin.value()) - float(session.xs[0])

    def on_position_alignment_changed(self, *_args) -> None:
        self.map_widget.set_session_overlays(self._session_overlay_entries(), self.active_session_key)
        self.map_widget.set_comparison_time(self.clock.current_time_s)
        if self.align_map_x_check.isChecked():
            self.status_label.setText(
                f"Map X normalization enabled: each trajectory starts at X={self.align_map_x_spin.value():.3f} m; lane-dependent Y values are unchanged."
            )
        else:
            self.status_label.setText("Map X normalization disabled; trajectories use their recorded world coordinates.")

    def show_straight_analysis(self) -> None:
        if self.road_projector is None:
            QMessageBox.warning(self, "Six-lane reference required", "The straightened analysis requires the six Open Road lane reference files.")
            return
        if not self.loaded_sessions:
            QMessageBox.information(self, "No recordings loaded", "Load one or more recordings before opening the 50 km analysis.")
            return
        if self.analysis_window is None:
            self.analysis_window = StraightRoadAnalysisWindow(self, self.road_projector)
        self.analysis_window.refresh_from_owner()
        self.analysis_window.show()
        self.analysis_window.raise_()
        self.analysis_window.activateWindow()

    def _sync_straight_analysis_time(self, time_s: float) -> None:
        if self.analysis_window is not None and self.analysis_window.isVisible():
            self.analysis_window.set_time(time_s)

    def configure_starts(self) -> None:
        if not self.raw_sessions:
            QMessageBox.information(
                self,
                "No recordings loaded",
                "Load at least one recorded session before configuring replay starts.",
            )
            return
        dialog = StartAlignmentDialog(self)
        dialog.exec()

    def _session_view(self, key: str) -> SessionData:
        raw = self.raw_sessions[key]
        if not self.apply_trim_check.isChecked():
            return raw
        start_s = max(0.0, float(self.session_start_offsets.get(key, 0.0)))
        if start_s <= 1e-9:
            return raw
        return raw.with_analysis_start(start_s)

    def _rebuild_session_views(self, preserve_active: bool = False) -> None:
        if not self.raw_sessions:
            self.loaded_sessions.clear()
            self._refresh_session_ui()
            return
        active_key = self.active_session_key
        old_time = self.clock.current_time_s if preserve_active else 0.0
        was_multi_video = self.multi_video_mode
        self._close_all_comparison_videos()
        self.loaded_sessions = {
            key: self._session_view(key)
            for key in self.session_order
            if key in self.raw_sessions
        }
        if active_key in self.loaded_sessions:
            self.set_active_session(active_key, preserve_time=False)
            if preserve_active:
                self.clock.seek(min(old_time, self.session.duration_s if self.session else 0.0))
        else:
            self._refresh_session_ui()
        if was_multi_video:
            self._refresh_comparison_video_windows()

    def on_alignment_mode_changed(self, enabled: bool) -> None:
        """Toggle between raw recording time and Replay-configured starts."""
        if not self.raw_sessions:
            return
        self.clock.pause()
        self._rebuild_session_views(preserve_active=False)
        mode = "configured replay starts" if enabled else "original raw recording starts"
        self.status_label.setText(
            f"Replay timing changed to {mode} for {len(self.loaded_sessions)} loaded recording(s)."
        )

    def _read_saved_replay_alignment(self, session: SessionData) -> tuple[float, str, float | None, dict]:
        replay = session.metadata.get("replay", {})
        alignment = replay.get("start_alignment", {}) if isinstance(replay, dict) else {}
        if not isinstance(alignment, dict):
            return 0.0, "raw", None, {}
        try:
            start_s = float(alignment.get("start_s", 0.0))
        except (TypeError, ValueError):
            start_s = 0.0
        start_s = max(0.0, min(start_s, session.raw_duration_s))
        mode = str(alignment.get("mode", "raw"))
        if mode not in {"raw", "manual", "detected"}:
            mode = "manual" if start_s > 0.0 else "raw"
        detected = alignment.get("detected_motion_start_s")
        try:
            detected_f = float(detected) if detected is not None else None
        except (TypeError, ValueError):
            detected_f = None
        settings = alignment.get("detector_settings", {})
        if not isinstance(settings, dict):
            settings = {}
        return start_s, mode, detected_f, dict(settings)

    def preview_raw_start(self, key: str, raw_time_s: float) -> None:
        """Show the requested raw time without changing the saved alignment choice."""
        if key not in self.raw_sessions:
            return
        if key != self.active_session_key:
            self.set_active_session(key, preserve_time=False)
        applied_offset = (
            float(self.session_start_offsets.get(key, 0.0))
            if self.apply_trim_check.isChecked()
            else 0.0
        )
        replay_time = max(0.0, float(raw_time_s) - applied_offset)
        if self.session is not None:
            replay_time = min(replay_time, self.session.duration_s)
        self.clock.seek(replay_time)
        self.show_video_window()
        self.status_label.setText(
            f"Previewing raw {format_time_s(float(raw_time_s))} for "
            f"{self.session_labels.get(key, Path(key).name)}. No files or start settings were changed."
        )

    def apply_replay_start_choice(
        self,
        key: str,
        start_s: float,
        mode: str,
        detected_start: float | None = None,
        settings: dict | None = None,
        persist: bool = True,
    ) -> None:
        """Apply one non-destructive Replay start choice to a loaded recording."""
        raw = self.raw_sessions.get(key)
        if raw is None:
            return
        start = max(0.0, min(float(start_s), raw.raw_duration_s))
        if mode not in {"raw", "manual", "detected"}:
            mode = "manual" if start > 0.0 else "raw"
        if mode == "raw":
            start = 0.0

        self.session_start_offsets[key] = start
        self.session_start_modes[key] = mode
        self.detected_motion_starts[key] = detected_start
        if settings is not None:
            self.alignment_detection_settings[key] = dict(settings)

        if persist:
            replay = raw.metadata.setdefault("replay", {})
            replay["start_alignment"] = {
                "non_destructive": True,
                "mode": mode,
                "start_s": round(start, 6),
                "detected_motion_start_s": (
                    None if detected_start is None else round(float(detected_start), 6)
                ),
                "detector_settings": dict(settings or {}),
            }
            try:
                write_json_atomic(raw.folder / "session.json", raw.metadata)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Could not save replay start",
                    f"The start was applied in memory, but session.json could not be updated:\n{exc}",
                )

        self._rebuild_session_views(preserve_active=False)
        if key in self.loaded_sessions:
            self.set_active_session(key, preserve_time=False)
        origin = "raw start" if start <= 1e-9 else f"raw +{start:.3f} s"
        saved = " and saved" if persist else " for this Replay session"
        self.status_label.setText(
            f"Replay start for {self.session_labels.get(key, Path(key).name)} set to {origin}{saved}."
        )

    def add_session(
        self, path: Path, make_active: bool = True, show_errors: bool = True
    ) -> bool:
        try:
            # Replay owns start alignment. Always load the recorder output raw.
            raw_session = SessionData.load(path, apply_analysis_trim=False)
        except Exception as exc:
            if show_errors:
                QMessageBox.critical(self, "Could not load session", str(exc))
            return False

        key = str(raw_session.folder.resolve())
        if key not in self.raw_sessions:
            self.raw_sessions[key] = raw_session
            self.session_order.append(key)
            self.session_labels[key] = self._unique_session_label(raw_session)
            used_colors = set(self.session_colors.values())
            available = [color for color in SESSION_COLORS if color not in used_colors]
            if available:
                self.session_colors[key] = available[0]
            else:
                palette_index = (len(self.session_order) - 1) % len(SESSION_COLORS)
                self.session_colors[key] = SESSION_COLORS[palette_index]

            start_s, mode, detected, settings = self._read_saved_replay_alignment(raw_session)
            self.session_start_offsets[key] = start_s
            self.session_start_modes[key] = mode
            self.detected_motion_starts[key] = detected
            self.alignment_detection_settings[key] = settings
            self.loaded_sessions[key] = self._session_view(key)

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
        self.raw_sessions.pop(key, None)
        self.session_start_offsets.pop(key, None)
        self.session_start_modes.pop(key, None)
        self.detected_motion_starts.pop(key, None)
        self.alignment_detection_settings.pop(key, None)
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
        self.raw_sessions.clear()
        self.session_start_offsets.clear()
        self.session_start_modes.clear()
        self.detected_motion_starts.clear()
        self.alignment_detection_settings.clear()
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
            # If map X normalization is enabled, transform the display click back
            # into that recording's original world coordinate before searching.
            raw_x = float(x) - self._map_x_shift(key)
            for candidate in session.find_passes(raw_x, y, radius_m=radius):
                matches.append((key, candidate))
            candidate = session.nearest_sample(raw_x, y)
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
        if self.analysis_window is not None:
            self.analysis_window.close()
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
