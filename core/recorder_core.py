"""GUI-independent recording engine.

RecorderWorker depends only on the LocationSource abstraction. This module can
therefore be unit-tested without PySide6 or a Quanser installation.
"""

from __future__ import annotations

import csv
from pathlib import Path
import threading
import time

from core.oop_interfaces import LocationSource
from core.replay_core import new_session_document, utc_now_iso, write_json_atomic


class RecorderWorker:
    """Record one LocationSource on a background thread at a fixed rate."""

    def __init__(
        self,
        session_dir: Path,
        source: LocationSource,
        sample_rate_hz: float,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.source = source
        self.sample_rate_hz = float(sample_rate_hz)
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be greater than zero")
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
            requested_sample_rate_hz=self.sample_rate_hz,
            source_metadata=self.source.metadata,
        )
        write_json_atomic(self.session_dir / "session.json", self.document)

    @property
    def source_name(self) -> str:
        return self.source.name

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            raise RuntimeError("RecorderWorker is already running.")
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="drive-location-recorder",
        )
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
        started_wall = None
        csv_path = self.session_dir / "location.csv"

        try:
            # Polymorphic call: this can be QLabs, LSL, a mock source, etc.
            self.source.connect()

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
                    reading = self.source.read_position()

                    if reading is not None:
                        writer.writerow(
                            [
                                f"{t:.6f}",
                                f"{reading.x:.6f}",
                                f"{reading.y:.6f}",
                                f"{reading.z:.6f}",
                            ]
                        )
                        with self.lock:
                            self.sample_count += 1
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
                        next_sample_wall = time.perf_counter()

                handle.flush()

        except Exception as exc:
            with self.lock:
                self.error = str(exc)
            self.ready_event.set()
        finally:
            try:
                self.source.close()
            except Exception:
                pass

            with self.lock:
                if started_wall is not None:
                    self.elapsed_s = max(
                        self.elapsed_s,
                        time.perf_counter() - started_wall,
                    )
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
