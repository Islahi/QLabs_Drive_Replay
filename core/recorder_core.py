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

            self.document["finished_utc"] = utc_now_iso()
            telemetry = self.document["telemetry"]
            telemetry["sample_count"] = sample_count
            telemetry["dropped_samples"] = dropped
            telemetry["duration_s"] = round(elapsed, 6)
            if error:
                self.document["error"] = error
            write_json_atomic(self.session_dir / "session.json", self.document)



class SessionRecorderWorker:
    """One-button synchronized recorder for telemetry + OBS + QLabs CSI cameras.

    The existing :class:`RecorderWorker` remains available for simple/location-
    only recordings and unit tests. This worker is the experiment recorder used
    by the GUI.
    """

    def __init__(
        self,
        session_dir: Path,
        source: LocationSource,
        sample_rate_hz: float,
        obs_host: str = "localhost",
        obs_port: int = 4455,
        obs_password: str = "",
        camera_keys: tuple[str, ...] = ("left", "right", "rear"),
        camera_fps: float = 15.0,
    ) -> None:
        from integrations.recording_services import OBSRecordingController

        self.session_dir = Path(session_dir)
        self.source = source
        self.sample_rate_hz = float(sample_rate_hz)
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be greater than zero")
        self.sample_period_s = 1.0 / self.sample_rate_hz
        self.camera_keys = tuple(dict.fromkeys(camera_keys))
        self.camera_fps = float(camera_fps)

        self.obs = OBSRecordingController(
            host=obs_host,
            port=int(obs_port),
            password=obs_password,
            timeout_s=3.0,
        )
        self.obs_sync_logger = None
        self.camera_recorder = None
        self.camera_preflight: dict[str, dict] = {}
        self.warnings: list[str] = []

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
        self.obs_recording = False
        self.obs_output_path: str | None = None
        self.session_zero_wall: float | None = None

        self.document = new_session_document(
            session_id=self.session_dir.name,
            requested_sample_rate_hz=self.sample_rate_hz,
            source_metadata=self.source.metadata,
        )
        self.document["recorder"] = {
            "kind": "one_button_obs_qlabs",
            "qlabs_front_view": True,
            "additional_cameras": list(self.camera_keys),
            "additional_camera_fps": self.camera_fps,
            "camera_capture_mode": "single_connection_sequential",
        }
        self.document["obs"] = {
            "host": obs_host,
            "port": int(obs_port),
            "sync_file": "obs_sync.csv",
            "recording_path": None,
        }
        self.document["videos"]["qlabs_front"] = {
            "kind": "obs_qlabs_window",
            "label": "QLabs Front (OBS)",
            "path": None,
            "sync_file": "obs_sync.csv",
            "preferred": True,
        }
        write_json_atomic(self.session_dir / "session.json", self.document)

    @property
    def source_name(self) -> str:
        return self.source.name

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            raise RuntimeError("SessionRecorderWorker is already running.")
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="drive-session-recorder",
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def snapshot(self) -> dict:
        with self.lock:
            if self.camera_recorder is not None:
                camera_states = self.camera_recorder.snapshot()
            else:
                camera_states = {
                    key: {
                        "label": key.title(),
                        "frame_count": 0,
                        "duplicate_count": 0,
                        "failed_requests": 0,
                        "error": None,
                        "ready": False,
                        "preflight": self.camera_preflight.get(key),
                    }
                    for key in self.camera_keys
                }
            return {
                "sample_count": self.sample_count,
                "dropped_samples": self.dropped_samples,
                "elapsed_s": self.elapsed_s,
                "error": self.error,
                "ready": self.ready,
                "finished": self.finished,
                "obs_recording": self.obs_recording,
                "obs_output_path": self.obs_output_path,
                "cameras": camera_states,
                "camera_preflight": dict(self.camera_preflight),
                "warnings": list(self.warnings),
            }

    def _run(self) -> None:
        from integrations.recording_services import OBSStatusLogger, QLabsMultiCameraRecorder

        csv_path = self.session_dir / "location.csv"
        started_utc = None
        try:
            # 1) QLabs must be valid before OBS starts. This prevents an unusable
            # experiment from producing an apparently valid video.
            self.source.connect()

            # QLabs front CSI is the main experiment view captured by OBS.
            possess_front = getattr(self.source, "possess_front_camera", None)
            if callable(possess_front):
                possess_front()

            # 2) Preflight every selected Python camera BEFORE OBS starts.
            # This verifies QLabs image acquisition and MP4 encoding, and refuses
            # the experiment if get_image() blocks or returns unusable data.
            if self.camera_keys:
                self.camera_preflight = QLabsMultiCameraRecorder.preflight_cameras(
                    host=str(self.source.metadata.get("host", "localhost")),
                    actor_number=int(self.source.metadata.get("actor_number", 0)),
                    camera_keys=self.camera_keys,
                    session_dir=self.session_dir,
                    timeout_s=max(8.0, 4.0 * len(self.camera_keys)),
                )
                self.document["recorder"]["camera_preflight"] = self.camera_preflight
                write_json_atomic(self.session_dir / "session.json", self.document)

            # 3) Authenticate with OBS and refuse to start if OBS is already
            # recording, then align the session clock to OBS video t=0.
            self.obs.connect()
            session_zero_wall, obs_status = self.obs.start_and_estimate_video_zero()
            self.session_zero_wall = session_zero_wall
            self.obs_recording = True
            started_utc = utc_now_iso()

            self.document["recording_started_utc"] = started_utc
            self.document["timebase"]["obs_duration_at_first_status_s"] = round(
                obs_status.duration_s, 6
            )
            write_json_atomic(self.session_dir / "session.json", self.document)

            # 3) Continuously log OBS duration versus the shared session clock.
            self.obs_sync_logger = OBSStatusLogger(
                self.obs,
                self.session_dir / "obs_sync.csv",
                session_zero_wall=session_zero_wall,
                period_s=1.0,
            )
            self.obs_sync_logger.start()

            # 5) Optional extra camera videos. One worker owns one QLabs
            # connection and requests Left/Right/Rear sequentially.
            if self.camera_keys:
                self.camera_recorder = QLabsMultiCameraRecorder(
                    host=str(self.source.metadata.get("host", "localhost")),
                    actor_number=int(self.source.metadata.get("actor_number", 0)),
                    camera_keys=self.camera_keys,
                    session_dir=self.session_dir,
                    session_zero_wall=session_zero_wall,
                    fps=self.camera_fps,
                    preflight=self.camera_preflight,
                )
                self.camera_recorder.start()

            # 5) Unfiltered fixed-rate telemetry. time_s is actual capture time;
            # scheduled_time_s is useful for diagnosing loop jitter.
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    [
                        "time_s",
                        "scheduled_time_s",
                        "x",
                        "y",
                        "z",
                        "roll_rad",
                        "pitch_rad",
                        "yaw_rad",
                        "valid",
                    ]
                )
                handle.flush()

                next_sample_wall = time.perf_counter()
                last_flush_wall = next_sample_wall

                with self.lock:
                    self.ready = True
                self.ready_event.set()

                while not self.stop_event.is_set():
                    sample_wall = time.perf_counter()
                    t = sample_wall - session_zero_wall
                    scheduled_t = next_sample_wall - session_zero_wall
                    reading = self.source.read_position()

                    if reading is not None:
                        writer.writerow(
                            [
                                f"{t:.6f}",
                                f"{scheduled_t:.6f}",
                                f"{reading.x:.6f}",
                                f"{reading.y:.6f}",
                                f"{reading.z:.6f}",
                                "" if reading.roll_rad is None else f"{reading.roll_rad:.9f}",
                                "" if reading.pitch_rad is None else f"{reading.pitch_rad:.9f}",
                                "" if reading.yaw_rad is None else f"{reading.yaw_rad:.9f}",
                                1,
                            ]
                        )
                        with self.lock:
                            self.sample_count += 1
                    else:
                        with self.lock:
                            self.dropped_samples += 1

                    with self.lock:
                        self.elapsed_s = max(self.elapsed_s, t)

                    # Never allow another long experiment to continue silently
                    # with dead CSI recording. Preflight catches startup faults;
                    # this watchdog catches a worker that stalls after OBS starts.
                    if self.camera_recorder is not None and t >= 6.0:
                        camera_state = self.camera_recorder.snapshot()
                        for camera_key, info in camera_state.items():
                            camera_error = info.get("error")
                            if camera_error:
                                raise RuntimeError(
                                    f"{info.get('label', camera_key)} recording failed: {camera_error}"
                                )
                            frame_count = int(info.get("frame_count", 0))
                            last_session_time = info.get("last_session_time_s")
                            if frame_count <= 0:
                                raise RuntimeError(
                                    f"{info.get('label', camera_key)} produced no video frames "
                                    "within the first 6 seconds after recording started."
                                )
                            if (
                                last_session_time is not None
                                and t - float(last_session_time) > 6.0
                            ):
                                raise RuntimeError(
                                    f"{info.get('label', camera_key)} has not produced a frame "
                                    f"for {t - float(last_session_time):.1f} seconds."
                                )

                    now = time.perf_counter()
                    if now - last_flush_wall >= 1.0:
                        handle.flush()
                        last_flush_wall = now

                    next_sample_wall += self.sample_period_s
                    delay = next_sample_wall - time.perf_counter()
                    if delay > 0:
                        self.stop_event.wait(delay)
                    else:
                        # Avoid trying to catch up with a burst of stale samples.
                        next_sample_wall = time.perf_counter()

                handle.flush()

        except Exception as exc:
            with self.lock:
                self.error = str(exc)
            self.ready_event.set()
        finally:
            # Stop secondary writers before OBS so all sources share as much of
            # the same time range as possible.
            if self.camera_recorder is not None:
                try:
                    self.camera_recorder.stop()
                except Exception as exc:
                    self.warnings.append(f"Camera stop failed: {exc}")

            if self.obs_sync_logger is not None:
                try:
                    self.obs_sync_logger.stop()
                except Exception:
                    pass

            try:
                output_path = self.obs.stop()
                if output_path:
                    self.obs_output_path = output_path
            except Exception as exc:
                with self.lock:
                    if self.error is None:
                        self.error = f"OBS stop failed: {exc}"
            finally:
                self.obs_recording = False
                self.obs.close()

            try:
                self.source.close()
            except Exception:
                pass

            with self.lock:
                if self.session_zero_wall is not None:
                    self.elapsed_s = max(
                        self.elapsed_s,
                        time.perf_counter() - self.session_zero_wall,
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

            if self.obs_output_path:
                self.document["obs"]["recording_path"] = self.obs_output_path
                self.document["videos"]["qlabs_front"]["path"] = self.obs_output_path

            if self.obs_sync_logger is not None:
                self.document["obs"]["sync_samples"] = self.obs_sync_logger.sample_count
                if self.obs_sync_logger.error:
                    self.document["obs"]["sync_error"] = self.obs_sync_logger.error

            if self.camera_recorder is not None:
                camera_metadata = self.camera_recorder.video_metadata()
                for key, metadata in camera_metadata.items():
                    self.document["videos"][key] = metadata
                    if metadata.get("status") != "complete":
                        self.warnings.append(
                            f"{metadata.get('label', key)}: {metadata.get('error') or 'recording failed'}"
                        )

            # A camera failure is visible in session status even if telemetry/OBS
            # completed, avoiding the previous misleading status='complete'.
            if error:
                self.document["status"] = "error"
                self.document["error"] = error
            elif self.warnings:
                self.document["status"] = "complete_with_recording_warnings"
                self.document["warnings"] = list(dict.fromkeys(self.warnings))
            else:
                self.document["status"] = "complete"
            write_json_atomic(self.session_dir / "session.json", self.document)
