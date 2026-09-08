"""Timestamped Open Road QCar location recorder.

Records every requested sample (default 20 Hz) as time_s,x,y,z. There is no
minimum-distance filter: stationary time is intentionally retained for replay.
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
import threading
import time

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

from replay_core import (
    DEFAULT_SAMPLE_RATE_HZ,
    format_time_s,
    make_unique_session_dir,
    new_session_document,
    utc_now_iso,
    write_json_atomic,
)


class RecorderWorker:
    def __init__(
        self,
        session_dir: Path,
        host: str,
        actor_number: int,
        sample_rate_hz: float,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.host = host
        self.actor_number = int(actor_number)
        self.sample_rate_hz = float(sample_rate_hz)
        self.sample_period_s = 1.0 / self.sample_rate_hz

        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

        self.sample_count = 0
        self.dropped_samples = 0
        self.elapsed_s = 0.0
        self.error: str | None = None
        self.ready = False
        self.finished = False

        self.document = new_session_document(
            session_id=self.session_dir.name,
            host=self.host,
            actor_number=self.actor_number,
            requested_sample_rate_hz=self.sample_rate_hz,
        )
        write_json_atomic(self.session_dir / "session.json", self.document)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="qlabs-location-recorder")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "sample_count": self.sample_count,
                "dropped_samples": self.dropped_samples,
                "elapsed_s": self.elapsed_s,
                "error": self.error,
                "ready": self.ready,
                "finished": self.finished,
            }

    def _run(self) -> None:
        from qvl.qlabs import QuanserInteractiveLabs
        from qvl.qcar2 import QLabsQCar2

        qlabs = QuanserInteractiveLabs()
        started_wall = None
        csv_path = self.session_dir / "location.csv"

        try:
            result = qlabs.open(self.host)
            if result is False:
                raise ConnectionError(f"QLabs rejected connection to {self.host!r}.")

            qcar = QLabsQCar2(qlabs)
            qcar.actorNumber = self.actor_number
            if not qcar.ping():
                raise RuntimeError(f"QCar2 actor {self.actor_number} does not exist in QLabs.")

            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["time_s", "x", "y", "z"])
                handle.flush()

                started_wall = time.perf_counter()
                self.document["recording_started_utc"] = utc_now_iso()
                write_json_atomic(self.session_dir / "session.json", self.document)
                next_sample_wall = started_wall
                last_flush_wall = started_wall

                with self.lock:
                    self.ready = True
                self.ready_event.set()

                while not self.stop_event.is_set():
                    sample_wall = time.perf_counter()
                    t = sample_wall - started_wall
                    status, location, _rotation, _scale = qcar.get_world_transform()

                    if status and len(location) >= 3:
                        x, y, z = map(float, location[:3])
                        if all(math.isfinite(value) for value in (x, y, z)):
                            writer.writerow([f"{t:.6f}", f"{x:.6f}", f"{y:.6f}", f"{z:.6f}"])
                            with self.lock:
                                self.sample_count += 1
                        else:
                            with self.lock:
                                self.dropped_samples += 1
                    else:
                        with self.lock:
                            self.dropped_samples += 1

                    with self.lock:
                        self.elapsed_s = t

                    now = time.perf_counter()
                    if now - last_flush_wall >= 1.0:
                        handle.flush()
                        last_flush_wall = now

                    next_sample_wall += self.sample_period_s
                    delay = next_sample_wall - time.perf_counter()
                    if delay > 0:
                        self.stop_event.wait(delay)
                    else:
                        # Avoid an ever-growing backlog if QLabs briefly stalls.
                        next_sample_wall = time.perf_counter()

                handle.flush()

        except Exception as exc:
            with self.lock:
                self.error = str(exc)
            self.ready_event.set()
        finally:
            try:
                qlabs.close()
            except Exception:
                pass

            with self.lock:
                if started_wall is not None:
                    self.elapsed_s = max(self.elapsed_s, time.perf_counter() - started_wall)
                sample_count = self.sample_count
                dropped = self.dropped_samples
                elapsed = self.elapsed_s
                error = self.error
                self.finished = True

            self.document["status"] = "error" if error else "complete"
            self.document["finished_utc"] = utc_now_iso()
            telemetry = self.document["telemetry"]
            telemetry["sample_count"] = sample_count
            telemetry["dropped_samples"] = dropped
            telemetry["duration_s"] = round(elapsed, 6)
            if error:
                self.document["error"] = error
            write_json_atomic(self.session_dir / "session.json", self.document)


class RecorderWindow(QMainWindow):
    def __init__(self, output_root: Path) -> None:
        super().__init__()
        self.setWindowTitle("QLabs Drive Location Recorder")
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
        self.status_label = QLabel("Ready. QLabs should already have Open Road and the source QCar loaded.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(100)
        self.poll_timer.timeout.connect(self.poll_worker)

    def browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose recording root", self.output_edit.text())
        if folder:
            self.output_edit.setText(folder)

    def start_recording(self) -> None:
        if self.worker is not None and not self.worker.snapshot()["finished"]:
            return

        try:
            output_root = Path(self.output_edit.text()).expanduser().resolve()
            session_dir = make_unique_session_dir(output_root, self.label_edit.text() or None)
            self.worker = RecorderWorker(
                session_dir=session_dir,
                host=self.host_edit.text().strip() or "localhost",
                actor_number=self.actor_spin.value(),
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
                f"Recording actor {self.worker.actor_number} at {self.worker.sample_rate_hz:g} Hz\n"
                f"{self.worker.session_dir}"
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
    parser = argparse.ArgumentParser(description="Record timestamped QLabs QCar XYZ at a fixed rate.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "recordings",
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
