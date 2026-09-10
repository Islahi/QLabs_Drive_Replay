"""External recording services used by the one-button session recorder.

The classes in this module deliberately use lazy imports so the repository can
still be inspected and unit-tested on machines that do not have QLabs, OpenCV,
or OBS WebSocket dependencies installed.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Callable


@dataclass(frozen=True)
class OBSRecordingStatus:
    active: bool
    paused: bool
    duration_s: float
    timecode: str
    bytes_written: int


class OBSRecordingController:
    """Small synchronous wrapper around obsws-python's request client."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 4455,
        password: str = "",
        timeout_s: float = 3.0,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.password = str(password)
        self.timeout_s = float(timeout_s)
        self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    def connect(self) -> None:
        self.close()
        import obsws_python as obs

        self._client = obs.ReqClient(
            host=self.host,
            port=self.port,
            password=self.password,
            timeout=self.timeout_s,
        )
        # Force a request now so authentication/connection failures happen
        # before an experiment is started.
        self.status()

    def status(self) -> OBSRecordingStatus:
        if self._client is None:
            raise RuntimeError("OBS WebSocket is not connected.")
        response = self._client.get_record_status()
        return OBSRecordingStatus(
            active=bool(getattr(response, "output_active", False)),
            paused=bool(getattr(response, "output_paused", False)),
            duration_s=max(0.0, float(getattr(response, "output_duration", 0.0)) / 1000.0),
            timecode=str(getattr(response, "output_timecode", "")),
            bytes_written=int(getattr(response, "output_bytes", 0) or 0),
        )

    def start_and_estimate_video_zero(self, timeout_s: float = 6.0) -> tuple[float, OBSRecordingStatus]:
        """Start OBS and estimate the local monotonic time of video t=0.

        The estimate is made from GetRecordStatus.output_duration rather than
        assuming that the StartRecord request instant is the first encoded
        frame. This gives telemetry and OBS a common session timebase.
        """
        if self._client is None:
            raise RuntimeError("OBS WebSocket is not connected.")

        current = self.status()
        if current.active:
            raise RuntimeError(
                "OBS is already recording. Stop the existing OBS recording before "
                "starting a synchronized session."
            )

        self._client.start_record()
        deadline = time.perf_counter() + max(0.5, float(timeout_s))
        last_status = current
        while time.perf_counter() < deadline:
            # Midpoint timing reduces error from request/response latency.
            before = time.perf_counter()
            last_status = self.status()
            after = time.perf_counter()
            if last_status.active:
                midpoint = (before + after) / 2.0
                video_zero_wall = midpoint - last_status.duration_s
                return video_zero_wall, last_status
            time.sleep(0.05)

        raise TimeoutError("OBS did not report an active recording within the timeout.")

    def stop(self) -> str | None:
        if self._client is None:
            return None
        status = self.status()
        if not status.active:
            return None
        response = self._client.stop_record()
        path = getattr(response, "output_path", None)
        return str(path) if path else None

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            disconnect = getattr(client, "disconnect", None)
            if callable(disconnect):
                disconnect()
        except Exception:
            pass


class OBSStatusLogger:
    """Write OBS/session clock correlation points while a session runs."""

    def __init__(
        self,
        controller: OBSRecordingController,
        csv_path: Path,
        session_zero_wall: float,
        period_s: float = 1.0,
    ) -> None:
        self.controller = controller
        self.csv_path = Path(csv_path)
        self.session_zero_wall = float(session_zero_wall)
        self.period_s = max(0.25, float(period_s))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.sample_count = 0

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="obs-sync-logger")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=3.0)

    def _run(self) -> None:
        try:
            with self.csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    [
                        "session_time_s",
                        "obs_duration_s",
                        "difference_s",
                        "output_timecode",
                        "output_bytes",
                    ]
                )
                handle.flush()
                while not self.stop_event.is_set():
                    before = time.perf_counter()
                    status = self.controller.status()
                    after = time.perf_counter()
                    midpoint = (before + after) / 2.0
                    session_time_s = midpoint - self.session_zero_wall
                    writer.writerow(
                        [
                            f"{session_time_s:.6f}",
                            f"{status.duration_s:.6f}",
                            f"{status.duration_s - session_time_s:.6f}",
                            status.timecode,
                            status.bytes_written,
                        ]
                    )
                    self.sample_count += 1
                    handle.flush()
                    if not status.active:
                        break
                    self.stop_event.wait(self.period_s)
        except Exception as exc:
            self.error = str(exc)


CAMERA_SPECS = {
    "left": ("CAMERA_CSI_LEFT", "Left CSI", "csi_left.mp4", "csi_left_frames.csv"),
    "right": ("CAMERA_CSI_RIGHT", "Right CSI", "csi_right.mp4", "csi_right_frames.csv"),
    "rear": ("CAMERA_CSI_BACK", "Rear CSI", "csi_rear.mp4", "csi_rear_frames.csv"),
}


class QLabsCameraRecorder:
    """Record one QLabs QCar CSI camera to MP4 with a timestamp sidecar.

    Each camera owns its own QLabs connection. This avoids sharing one socket
    between concurrent camera threads and keeps a slow camera request from
    blocking telemetry acquisition.
    """

    def __init__(
        self,
        host: str,
        actor_number: int,
        camera_key: str,
        session_dir: Path,
        session_zero_wall: float,
        fps: float = 15.0,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        if camera_key not in CAMERA_SPECS:
            raise ValueError(f"Unsupported QLabs camera: {camera_key!r}")
        self.host = str(host)
        self.actor_number = int(actor_number)
        self.camera_key = camera_key
        self.session_dir = Path(session_dir)
        self.session_zero_wall = float(session_zero_wall)
        self.fps = float(fps)
        if self.fps <= 0:
            raise ValueError("Camera FPS must be greater than zero.")
        self.on_status = on_status

        _constant, label, video_name, frame_name = CAMERA_SPECS[camera_key]
        self.label = label
        self.video_path = self.session_dir / video_name
        self.frames_path = self.session_dir / frame_name

        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.frame_count = 0
        self.duplicate_count = 0
        self.failed_requests = 0
        self.first_session_time_s: float | None = None
        self.last_session_time_s: float | None = None

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"qlabs-camera-{self.camera_key}",
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5.0)

    def snapshot(self) -> dict:
        return {
            "label": self.label,
            "frame_count": self.frame_count,
            "duplicate_count": self.duplicate_count,
            "failed_requests": self.failed_requests,
            "error": self.error,
            "ready": self.ready_event.is_set() and self.error is None,
            "first_session_time_s": self.first_session_time_s,
            "last_session_time_s": self.last_session_time_s,
        }

    def video_metadata(self) -> dict:
        return {
            "kind": "qlabs_csi_camera",
            "label": self.label,
            "file": self.video_path.name,
            "frame_timestamps_file": self.frames_path.name,
            "camera": self.camera_key,
            "requested_fps": self.fps,
            "frame_count": self.frame_count,
            "duplicate_frames": self.duplicate_count,
            "failed_requests": self.failed_requests,
            "first_session_time_s": self.first_session_time_s,
            "last_session_time_s": self.last_session_time_s,
        }

    def _run(self) -> None:
        qlabs = None
        writer = None
        try:
            import cv2
            import numpy as np
            from qvl.qlabs import QuanserInteractiveLabs
            from qvl.qcar2 import QLabsQCar2

            qlabs = QuanserInteractiveLabs()
            if qlabs.open(self.host) is False:
                raise ConnectionError(f"QLabs rejected camera connection to {self.host!r}.")

            qcar = QLabsQCar2(qlabs)
            qcar.actorNumber = self.actor_number
            if not qcar.ping():
                raise RuntimeError(f"QCar2 actor {self.actor_number} does not exist in QLabs.")

            constant_name = CAMERA_SPECS[self.camera_key][0]
            camera_constant = getattr(qcar, constant_name)
            frame_period = 1.0 / self.fps
            next_capture_wall = time.perf_counter()
            last_frame = None

            with self.frames_path.open("w", encoding="utf-8", newline="") as frame_handle:
                csv_writer = csv.writer(frame_handle)
                csv_writer.writerow(
                    [
                        "frame_index",
                        "video_time_s",
                        "session_time_s",
                        "capture_completed_time_s",
                        "source_ok",
                        "duplicate",
                    ]
                )
                frame_handle.flush()
                self.ready_event.set()

                while not self.stop_event.is_set():
                    capture_started = time.perf_counter()
                    status, jpeg_data = qcar.get_image(camera=camera_constant)
                    capture_completed = time.perf_counter()
                    session_time_s = capture_started - self.session_zero_wall
                    completed_time_s = capture_completed - self.session_zero_wall

                    decoded = None
                    if status and jpeg_data:
                        buffer = np.frombuffer(bytes(jpeg_data), dtype=np.uint8)
                        decoded = cv2.imdecode(buffer, cv2.IMREAD_COLOR)

                    source_ok = decoded is not None
                    duplicate = False
                    if source_ok:
                        last_frame = decoded
                    else:
                        self.failed_requests += 1
                        if last_frame is None:
                            # Before the first valid frame there is nothing safe
                            # to write; wait for a real image and let the sidecar
                            # establish the camera's initial time offset.
                            next_capture_wall += frame_period
                            delay = next_capture_wall - time.perf_counter()
                            if delay > 0:
                                self.stop_event.wait(delay)
                            else:
                                next_capture_wall = time.perf_counter()
                            continue
                        decoded = last_frame
                        duplicate = True
                        self.duplicate_count += 1

                    if writer is None:
                        height, width = decoded.shape[:2]
                        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                        writer = cv2.VideoWriter(
                            str(self.video_path),
                            fourcc,
                            self.fps,
                            (int(width), int(height)),
                        )
                        if not writer.isOpened():
                            raise RuntimeError(
                                f"OpenCV could not create {self.video_path.name}. "
                                "Check that MP4 video encoding is available."
                            )
                        self.first_session_time_s = session_time_s

                    writer.write(decoded)
                    video_time_s = self.frame_count / self.fps
                    csv_writer.writerow(
                        [
                            self.frame_count,
                            f"{video_time_s:.6f}",
                            f"{session_time_s:.6f}",
                            f"{completed_time_s:.6f}",
                            int(source_ok),
                            int(duplicate),
                        ]
                    )
                    self.frame_count += 1
                    self.last_session_time_s = session_time_s
                    if self.frame_count % max(1, int(round(self.fps))) == 0:
                        frame_handle.flush()

                    next_capture_wall += frame_period
                    delay = next_capture_wall - time.perf_counter()
                    if delay > 0:
                        self.stop_event.wait(delay)
                    else:
                        # Do not create an ever-growing backlog if get_image is
                        # slower than the requested FPS.
                        next_capture_wall = time.perf_counter()

                frame_handle.flush()

        except Exception as exc:
            self.error = str(exc)
            self.ready_event.set()
        finally:
            if writer is not None:
                try:
                    writer.release()
                except Exception:
                    pass
            if qlabs is not None:
                try:
                    qlabs.close()
                except Exception:
                    pass
