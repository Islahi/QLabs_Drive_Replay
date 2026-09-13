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


class QLabsMultiCameraRecorder:
    """Record several QLabs QCar CSI cameras through ONE QLabs connection.

    QLabs camera requests are performed sequentially on one worker thread.  The
    previous implementation created one QLabs connection/thread per camera;
    field recordings showed those concurrent get_image calls could block before
    the first frame.  This design deliberately trades a few milliseconds of
    inter-camera skew for much more predictable acquisition.
    """

    def __init__(
        self,
        host: str,
        actor_number: int,
        camera_keys: tuple[str, ...],
        session_dir: Path,
        session_zero_wall: float,
        fps: float = 15.0,
        preflight: dict | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        keys = tuple(dict.fromkeys(str(k) for k in camera_keys))
        unknown = [k for k in keys if k not in CAMERA_SPECS]
        if unknown:
            raise ValueError(f"Unsupported QLabs cameras: {unknown!r}")
        self.host = str(host)
        self.actor_number = int(actor_number)
        self.camera_keys = keys
        self.session_dir = Path(session_dir)
        self.session_zero_wall = float(session_zero_wall)
        self.fps = float(fps)
        if self.fps <= 0:
            raise ValueError("Camera FPS must be greater than zero.")
        self.preflight = dict(preflight or {})
        self.on_status = on_status

        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self._stats: dict[str, dict] = {}
        for key in self.camera_keys:
            _constant, label, video_name, frame_name = CAMERA_SPECS[key]
            self._stats[key] = {
                "label": label,
                "video_path": self.session_dir / video_name,
                "frames_path": self.session_dir / frame_name,
                "frame_count": 0,
                "duplicate_count": 0,
                "failed_requests": 0,
                "first_session_time_s": None,
                "last_session_time_s": None,
                "error": None,
                "last_frame_wall": None,
            }

    @staticmethod
    def preflight_cameras(
        host: str,
        actor_number: int,
        camera_keys: tuple[str, ...],
        session_dir: Path,
        timeout_s: float = 12.0,
    ) -> dict[str, dict]:
        """Verify selected QLabs images AND MP4 encoding before OBS starts.

        The QLabs API does not expose a request timeout for get_image(), so the
        whole preflight runs in a daemon thread and the caller enforces a wall
        timeout.  If QLabs blocks, the experiment is refused instead of silently
        producing header-only CSV files for an hour-long session.
        """
        keys = tuple(dict.fromkeys(str(k) for k in camera_keys))
        if not keys:
            return {}
        result: dict[str, dict] = {}
        failure: list[BaseException] = []

        def work() -> None:
            qlabs = None
            try:
                import cv2
                import numpy as np
                from qvl.qlabs import QuanserInteractiveLabs
                from qvl.qcar2 import QLabsQCar2

                qlabs = QuanserInteractiveLabs()
                if qlabs.open(str(host)) is False:
                    raise ConnectionError(f"QLabs rejected camera preflight connection to {host!r}.")
                qcar = QLabsQCar2(qlabs)
                qcar.actorNumber = int(actor_number)
                if not qcar.ping():
                    raise RuntimeError(f"QCar2 actor {actor_number} does not exist in QLabs.")

                Path(session_dir).mkdir(parents=True, exist_ok=True)
                for key in keys:
                    if key not in CAMERA_SPECS:
                        raise ValueError(f"Unsupported QLabs camera: {key!r}")
                    constant_name, label, _video_name, _frame_name = CAMERA_SPECS[key]
                    camera_constant = getattr(qcar, constant_name)
                    started = time.perf_counter()
                    status, jpeg_data = qcar.get_image(camera=camera_constant)
                    elapsed = time.perf_counter() - started
                    if not status or not jpeg_data:
                        raise RuntimeError(f"{label} returned no image during preflight.")
                    buffer = np.frombuffer(bytes(jpeg_data), dtype=np.uint8)
                    decoded = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
                    if decoded is None:
                        raise RuntimeError(f"{label} image could not be decoded by OpenCV.")
                    height, width = decoded.shape[:2]

                    # Test the exact encoder/file type used by the real recorder.
                    probe = Path(session_dir) / f".__camera_preflight_{key}.mp4"
                    writer = cv2.VideoWriter(
                        str(probe),
                        cv2.VideoWriter_fourcc(*"mp4v"),
                        5.0,
                        (int(width), int(height)),
                    )
                    if not writer.isOpened():
                        raise RuntimeError(
                            f"OpenCV could not create MP4 for {label}. "
                            "Install an OpenCV/FFmpeg build with MP4 encoding support."
                        )
                    writer.write(decoded)
                    writer.release()
                    try:
                        size = probe.stat().st_size if probe.exists() else 0
                    finally:
                        try:
                            probe.unlink(missing_ok=True)
                        except Exception:
                            pass
                    if size <= 0:
                        raise RuntimeError(f"MP4 encoder test for {label} created an empty file.")
                    result[key] = {
                        "label": label,
                        "width": int(width),
                        "height": int(height),
                        "request_time_s": round(elapsed, 6),
                        "encoder": "mp4v",
                        "ok": True,
                    }
            except BaseException as exc:
                failure.append(exc)
            finally:
                if qlabs is not None:
                    try:
                        qlabs.close()
                    except Exception:
                        pass

        thread = threading.Thread(target=work, daemon=True, name="qlabs-camera-preflight")
        thread.start()
        thread.join(timeout=max(1.0, float(timeout_s)))
        if thread.is_alive():
            raise TimeoutError(
                f"QLabs camera preflight timed out after {float(timeout_s):.1f} s. "
                "A QCar get_image() request appears to be blocked; the session was not started."
            )
        if failure:
            raise RuntimeError(str(failure[0])) from failure[0]
        return result

    def start(self) -> None:
        if not self.camera_keys:
            self.ready_event.set()
            return
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="qlabs-multi-camera-recorder",
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=8.0)
            if self.thread.is_alive() and self.error is None:
                self.error = (
                    "Camera worker did not stop within 8 s; a QLabs get_image() call may be blocked."
                )

    def snapshot(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for key, stat in self._stats.items():
            out[key] = {
                "label": stat["label"],
                "frame_count": int(stat["frame_count"]),
                "duplicate_count": int(stat["duplicate_count"]),
                "failed_requests": int(stat["failed_requests"]),
                "error": stat["error"] or self.error,
                "ready": self.ready_event.is_set() and not (stat["error"] or self.error),
                "first_session_time_s": stat["first_session_time_s"],
                "last_session_time_s": stat["last_session_time_s"],
                "preflight": self.preflight.get(key),
            }
        return out

    def video_metadata(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for key, stat in self._stats.items():
            error = stat["error"] or self.error
            count = int(stat["frame_count"])
            status = "complete" if count > 0 and not error else "error"
            if count <= 0 and not error:
                error = "No frames were encoded."
            result[f"csi_{key}"] = {
                "kind": "qlabs_csi_camera",
                "label": stat["label"],
                "file": Path(stat["video_path"]).name,
                "frame_timestamps_file": Path(stat["frames_path"]).name,
                "camera": key,
                "requested_fps": self.fps,
                "frame_count": count,
                "duplicate_frames": int(stat["duplicate_count"]),
                "failed_requests": int(stat["failed_requests"]),
                "first_session_time_s": stat["first_session_time_s"],
                "last_session_time_s": stat["last_session_time_s"],
                "status": status,
                "error": error,
                "preflight": self.preflight.get(key),
            }
        return result

    def _run(self) -> None:
        qlabs = None
        writers: dict[str, object] = {}
        frame_handles: dict[str, object] = {}
        csv_writers: dict[str, object] = {}
        last_frames: dict[str, object] = {}
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

            constants = {
                key: getattr(qcar, CAMERA_SPECS[key][0]) for key in self.camera_keys
            }
            for key, stat in self._stats.items():
                handle = Path(stat["frames_path"]).open("w", encoding="utf-8", newline="")
                frame_handles[key] = handle
                csv_writer = csv.writer(handle)
                csv_writer.writerow([
                    "frame_index",
                    "video_time_s",
                    "session_time_s",
                    "capture_completed_time_s",
                    "source_ok",
                    "duplicate",
                ])
                handle.flush()
                csv_writers[key] = csv_writer

            frame_period = 1.0 / self.fps
            next_cycle_wall = time.perf_counter()
            self.ready_event.set()

            while not self.stop_event.is_set():
                for key in self.camera_keys:
                    if self.stop_event.is_set():
                        break
                    stat = self._stats[key]
                    capture_started = time.perf_counter()
                    try:
                        status, jpeg_data = qcar.get_image(camera=constants[key])
                        capture_completed = time.perf_counter()
                        decoded = None
                        if status and jpeg_data:
                            buffer = np.frombuffer(bytes(jpeg_data), dtype=np.uint8)
                            decoded = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
                        source_ok = decoded is not None
                    except Exception as exc:
                        capture_completed = time.perf_counter()
                        decoded = None
                        source_ok = False
                        stat["error"] = str(exc)

                    session_time_s = capture_started - self.session_zero_wall
                    completed_time_s = capture_completed - self.session_zero_wall
                    duplicate = False
                    if source_ok:
                        last_frames[key] = decoded
                    else:
                        stat["failed_requests"] += 1
                        if key not in last_frames:
                            continue
                        decoded = last_frames[key]
                        duplicate = True
                        stat["duplicate_count"] += 1

                    if key not in writers:
                        height, width = decoded.shape[:2]
                        writer = cv2.VideoWriter(
                            str(stat["video_path"]),
                            cv2.VideoWriter_fourcc(*"mp4v"),
                            self.fps,
                            (int(width), int(height)),
                        )
                        if not writer.isOpened():
                            stat["error"] = (
                                f"OpenCV could not create {Path(stat['video_path']).name}."
                            )
                            continue
                        writers[key] = writer
                        stat["first_session_time_s"] = session_time_s

                    writers[key].write(decoded)
                    frame_index = int(stat["frame_count"])
                    video_time_s = frame_index / self.fps
                    csv_writers[key].writerow([
                        frame_index,
                        f"{video_time_s:.6f}",
                        f"{session_time_s:.6f}",
                        f"{completed_time_s:.6f}",
                        int(source_ok),
                        int(duplicate),
                    ])
                    stat["frame_count"] = frame_index + 1
                    stat["last_session_time_s"] = session_time_s
                    stat["last_frame_wall"] = time.perf_counter()
                    if stat["frame_count"] % max(1, int(round(self.fps))) == 0:
                        frame_handles[key].flush()

                next_cycle_wall += frame_period
                delay = next_cycle_wall - time.perf_counter()
                if delay > 0:
                    self.stop_event.wait(delay)
                else:
                    # get_image() for all selected cameras took longer than the
                    # requested cycle.  Preserve synchronization; do not backlog.
                    next_cycle_wall = time.perf_counter()

        except Exception as exc:
            self.error = str(exc)
            self.ready_event.set()
        finally:
            for writer in writers.values():
                try:
                    writer.release()
                except Exception:
                    pass
            for handle in frame_handles.values():
                try:
                    handle.flush()
                    handle.close()
                except Exception:
                    pass
            if qlabs is not None:
                try:
                    qlabs.close()
                except Exception:
                    pass

