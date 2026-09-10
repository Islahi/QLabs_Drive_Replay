"""One-button synchronized Open Road experiment recorder.

The existing project structure is retained, but the recorder now starts the
main QLabs/OBS video, timestamped QCar telemetry, and optional QLabs CSI side
cameras from one button so the generated session can be opened directly by the
replay application.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from integrations.location_sources import QLabsQCarLocationSource
from core.oop_interfaces import LocationSource
from core.recorder_core import SessionRecorderWorker
from core.replay_core import DEFAULT_SAMPLE_RATE_HZ, format_time_s, make_unique_session_dir


class RecorderWindow(QMainWindow):
    """Experiment recorder presentation layer."""

    def __init__(self, output_root: Path) -> None:
        super().__init__()
        self.setWindowTitle("QLabs Open Road Session Recorder")
        self.resize(760, 610)
        self.worker: SessionRecorderWorker | None = None
        self.output_root = Path(output_root).expanduser().resolve()

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        qlabs_group = QGroupBox("QLabs / telemetry")
        qlabs_form = QFormLayout(qlabs_group)
        layout.addWidget(qlabs_group)

        self.host_edit = QLineEdit("localhost")
        qlabs_form.addRow("QLabs host", self.host_edit)

        self.actor_spin = QSpinBox()
        self.actor_spin.setRange(0, 1_000_000)
        self.actor_spin.setValue(0)
        qlabs_form.addRow("QCar actor", self.actor_spin)

        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(1.0, 100.0)
        self.rate_spin.setDecimals(1)
        self.rate_spin.setValue(DEFAULT_SAMPLE_RATE_HZ)
        self.rate_spin.setSuffix(" Hz")
        qlabs_form.addRow("Telemetry rate", self.rate_spin)

        front_note = QLabel(
            "When recording starts, the QLabs application is automatically switched "
            "to this QCar's FRONT CSI camera. OBS should capture the QLabs window."
        )
        front_note.setWordWrap(True)
        qlabs_form.addRow("Main video", front_note)

        obs_group = QGroupBox("OBS WebSocket")
        obs_form = QFormLayout(obs_group)
        layout.addWidget(obs_group)

        self.obs_host_edit = QLineEdit("localhost")
        obs_form.addRow("OBS host", self.obs_host_edit)

        self.obs_port_spin = QSpinBox()
        self.obs_port_spin.setRange(1, 65535)
        self.obs_port_spin.setValue(4455)
        obs_form.addRow("OBS port", self.obs_port_spin)

        self.obs_password_edit = QLineEdit()
        self.obs_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.obs_password_edit.setPlaceholderText("leave blank if OBS has no password")
        obs_form.addRow("OBS password", self.obs_password_edit)

        camera_group = QGroupBox("Additional QCar camera recordings (Python / QLabs)")
        camera_layout = QGridLayout(camera_group)
        layout.addWidget(camera_group)

        self.left_check = QCheckBox("Left CSI")
        self.left_check.setChecked(True)
        camera_layout.addWidget(self.left_check, 0, 0)

        self.right_check = QCheckBox("Right CSI")
        self.right_check.setChecked(True)
        camera_layout.addWidget(self.right_check, 0, 1)

        self.rear_check = QCheckBox("Rear CSI")
        self.rear_check.setChecked(True)
        camera_layout.addWidget(self.rear_check, 0, 2)

        camera_layout.addWidget(QLabel("Requested FPS"), 1, 0)
        self.camera_fps_spin = QDoubleSpinBox()
        self.camera_fps_spin.setRange(1.0, 30.0)
        self.camera_fps_spin.setDecimals(1)
        self.camera_fps_spin.setValue(15.0)
        self.camera_fps_spin.setSuffix(" fps")
        camera_layout.addWidget(self.camera_fps_spin, 1, 1)

        camera_note = QLabel(
            "Front CSI is not duplicated here because OBS records the QLabs front-camera "
            "view. Each selected side/rear stream gets an MP4 plus frame timestamps."
        )
        camera_note.setWordWrap(True)
        camera_layout.addWidget(camera_note, 2, 0, 1, 3)

        session_group = QGroupBox("Session")
        session_form = QFormLayout(session_group)
        layout.addWidget(session_group)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("optional, e.g. participant_001_run_01")
        session_form.addRow("Session label", self.label_edit)

        output_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.output_root))
        output_row.addWidget(self.output_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_output)
        output_row.addWidget(browse)
        session_form.addRow("Recording folder", output_row)

        self.record_button = QPushButton("START SESSION")
        self.record_button.setMinimumHeight(48)
        self.record_button.clicked.connect(self.toggle_recording)
        layout.addWidget(self.record_button)

        status_grid = QGridLayout()
        layout.addLayout(status_grid)

        self.elapsed_label = QLabel("Elapsed: 00:00:00.000")
        status_grid.addWidget(self.elapsed_label, 0, 0)
        self.samples_label = QLabel("Telemetry: 0 samples   Dropped: 0")
        status_grid.addWidget(self.samples_label, 0, 1)

        self.obs_status_label = QLabel("OBS: idle")
        status_grid.addWidget(self.obs_status_label, 1, 0)
        self.camera_status_label = QLabel("Cameras: idle")
        self.camera_status_label.setWordWrap(True)
        status_grid.addWidget(self.camera_status_label, 1, 1)

        self.status_label = QLabel(
            "Ready. Load Open Road and QCar actor 0 in QLabs, prepare an OBS scene that "
            "captures the QLabs window, then press START SESSION."
        )
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(100)
        self.poll_timer.timeout.connect(self.poll_worker)

    def browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose recording root",
            self.output_edit.text(),
        )
        if folder:
            self.output_edit.setText(folder)

    def selected_cameras(self) -> tuple[str, ...]:
        selected: list[str] = []
        if self.left_check.isChecked():
            selected.append("left")
        if self.right_check.isChecked():
            selected.append("right")
        if self.rear_check.isChecked():
            selected.append("rear")
        return tuple(selected)

    def toggle_recording(self) -> None:
        if self.worker is not None and not self.worker.snapshot()["finished"]:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        try:
            output_root = Path(self.output_edit.text()).expanduser().resolve()
            session_dir = make_unique_session_dir(
                output_root,
                self.label_edit.text() or None,
            )
            source: LocationSource = QLabsQCarLocationSource(
                host=self.host_edit.text().strip() or "localhost",
                actor_number=self.actor_spin.value(),
            )
            self.worker = SessionRecorderWorker(
                session_dir=session_dir,
                source=source,
                sample_rate_hz=self.rate_spin.value(),
                obs_host=self.obs_host_edit.text().strip() or "localhost",
                obs_port=self.obs_port_spin.value(),
                obs_password=self.obs_password_edit.text(),
                camera_keys=self.selected_cameras(),
                camera_fps=self.camera_fps_spin.value(),
            )
            self.worker.start()
        except Exception as exc:
            QMessageBox.critical(self, "Could not start session", str(exc))
            return

        self.record_button.setText("STOP SESSION")
        self.status_label.setText(
            "Starting synchronized session: checking QLabs, forcing front camera, "
            "starting OBS, then telemetry/cameras…"
        )
        self.poll_timer.start()

    def stop_recording(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.record_button.setEnabled(False)
            self.status_label.setText("Stopping cameras, OBS, and finalizing session.json…")

    def poll_worker(self) -> None:
        if self.worker is None:
            return
        state = self.worker.snapshot()
        self.elapsed_label.setText(f"Elapsed: {format_time_s(state['elapsed_s'])}")
        self.samples_label.setText(
            f"Telemetry: {state['sample_count']:,} samples   "
            f"Dropped: {state['dropped_samples']:,}"
        )
        self.obs_status_label.setText(
            "OBS: RECORDING" if state["obs_recording"] else "OBS: idle"
        )

        cameras = state.get("cameras", {})
        if cameras:
            pieces = []
            for key in ("left", "right", "rear"):
                info = cameras.get(key)
                if not info:
                    continue
                if info.get("error"):
                    pieces.append(f"{key}: ERROR")
                else:
                    pieces.append(
                        f"{key}: {int(info.get('frame_count', 0)):,} frames"
                        f" ({int(info.get('duplicate_count', 0))} dup)"
                    )
            self.camera_status_label.setText("Cameras: " + " | ".join(pieces))
        else:
            self.camera_status_label.setText("Cameras: none selected")

        if state["ready"] and not state["finished"]:
            self.status_label.setText(
                f"SESSION RECORDING — {self.worker.source_name} @ "
                f"{self.worker.sample_rate_hz:g} Hz\n{self.worker.session_dir}"
            )

        if state["finished"]:
            self.poll_timer.stop()
            self.record_button.setEnabled(True)
            self.record_button.setText("START SESSION")
            if state["error"]:
                self.status_label.setText(
                    f"Session stopped with an error: {state['error']}\n"
                    f"Session folder: {self.worker.session_dir}"
                )
            else:
                self.status_label.setText(
                    f"Session complete: {state['sample_count']:,} telemetry samples\n"
                    f"Session folder: {self.worker.session_dir}\n"
                    f"OBS file: {state.get('obs_output_path') or 'path not reported by OBS'}"
                )

    def closeEvent(self, event) -> None:
        if self.worker is not None and not self.worker.snapshot()["finished"]:
            reply = QMessageBox.question(
                self,
                "Recording is active",
                "Stop and finalize the current recording before closing?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.stop()
            self.worker.join(timeout=8.0)
        super().closeEvent(event)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record synchronized OBS video + QLabs QCar telemetry/cameras."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "recordings",
        help="Root folder for recording sessions",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QApplication([])
    window = RecorderWindow(args.output)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
