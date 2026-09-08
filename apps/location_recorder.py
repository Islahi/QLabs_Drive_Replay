"""Timestamped Open Road QCar location recorder using OOP abstractions.

RecorderWorker depends on LocationSource rather than on QLabs directly. The
current GUI creates QLabsQCarLocationSource, while a future LSL or test source
can be substituted without rewriting the recording loop.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
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
from core.recorder_core import RecorderWorker
from core.replay_core import (
    DEFAULT_SAMPLE_RATE_HZ,
    format_time_s,
    make_unique_session_dir,
)


class RecorderWindow(QMainWindow):
    """Qt presentation layer for the recorder."""

    def __init__(self, output_root: Path) -> None:
        super().__init__()
        self.setWindowTitle("QLabs Drive Location Recorder — OOP")
        self.resize(620, 360)
        self.worker: RecorderWorker | None = None
        self.output_root = Path(output_root).expanduser().resolve()

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        form = QFormLayout()
        layout.addLayout(form)

        self.host_edit = QLineEdit("localhost")
        form.addRow("QLabs host", self.host_edit)

        self.actor_spin = QSpinBox()
        self.actor_spin.setRange(0, 1_000_000)
        self.actor_spin.setValue(0)
        form.addRow("Source QCar actor", self.actor_spin)

        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(1.0, 100.0)
        self.rate_spin.setDecimals(1)
        self.rate_spin.setValue(DEFAULT_SAMPLE_RATE_HZ)
        self.rate_spin.setSuffix(" Hz")
        form.addRow("Sample rate", self.rate_spin)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("optional, e.g. participant_001")
        form.addRow("Session label", self.label_edit)

        output_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.output_root))
        output_row.addWidget(self.output_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_output)
        output_row.addWidget(browse)
        form.addRow("Recording folder", output_row)

        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        self.start_button = QPushButton("Start Recording")
        self.start_button.clicked.connect(self.start_recording)
        buttons.addWidget(self.start_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_recording)
        buttons.addWidget(self.stop_button)

        self.elapsed_label = QLabel("Elapsed: 00:00:00.000")
        layout.addWidget(self.elapsed_label)
        self.samples_label = QLabel("Samples: 0   Dropped: 0")
        layout.addWidget(self.samples_label)
        self.status_label = QLabel(
            "Ready. QLabs should already have Open Road and the source QCar loaded."
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

    def start_recording(self) -> None:
        if self.worker is not None and not self.worker.snapshot()["finished"]:
            return

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
            self.worker = RecorderWorker(
                session_dir=session_dir,
                source=source,
                sample_rate_hz=self.rate_spin.value(),
            )
            self.worker.start()
        except Exception as exc:
            QMessageBox.critical(self, "Could not start recorder", str(exc))
            return

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status_label.setText(f"Connecting… Session: {session_dir}")
        self.poll_timer.start()

    def stop_recording(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.status_label.setText("Stopping and finalizing session…")
            self.stop_button.setEnabled(False)

    def poll_worker(self) -> None:
        if self.worker is None:
            return
        state = self.worker.snapshot()
        self.elapsed_label.setText(f"Elapsed: {format_time_s(state['elapsed_s'])}")
        self.samples_label.setText(
            f"Samples: {state['sample_count']:,}   Dropped: {state['dropped_samples']:,}"
        )

        if state["ready"] and not state["finished"]:
            self.status_label.setText(
                f"Recording from {self.worker.source_name} at "
                f"{self.worker.sample_rate_hz:g} Hz\n{self.worker.session_dir}"
            )

        if state["finished"]:
            self.poll_timer.stop()
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            if state["error"]:
                self.status_label.setText(
                    f"Recording stopped with an error: {state['error']}\n"
                    f"Session folder: {self.worker.session_dir}"
                )
            else:
                self.status_label.setText(
                    f"Recording complete: {state['sample_count']:,} samples\n"
                    f"Session folder: {self.worker.session_dir}"
                )

    def closeEvent(self, event) -> None:
        if self.worker is not None and not self.worker.snapshot()["finished"]:
            self.worker.stop()
            self.worker.join(timeout=3.0)
        super().closeEvent(event)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record timestamped QLabs QCar XYZ at a fixed rate."
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
