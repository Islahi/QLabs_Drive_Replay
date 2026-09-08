"""Pure-Python data model and geometry helpers for QLabs drive replay.

This module deliberately has no PySide6 or QLabs imports so session files can
be validated and processed without a GUI or Quanser installation.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import json
import math
from pathlib import Path
from typing import Iterable, Sequence

SESSION_FORMAT = "qlabs_drive_session"
SESSION_VERSION = 1
DEFAULT_SAMPLE_RATE_HZ = 20.0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_time_s(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    whole_ms = int(round(seconds * 1000.0))
    hours, rem = divmod(whole_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def make_unique_session_dir(root: Path, label: str | None = None) -> Path:
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe_label = ""
    if label:
        safe_label = "_" + "".join(
            char if char.isalnum() or char in "-_" else "_" for char in label.strip()
        ).strip("_")

    base = f"{timestamp}{safe_label}"
    candidate = root / base
    counter = 1
    while candidate.exists():
        candidate = root / f"{base}_{counter:02d}"
        counter += 1
    candidate.mkdir(parents=False)
    return candidate


def write_json_atomic(path: Path, document: dict) -> None:
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
    temporary.replace(path)


def new_session_document(
    session_id: str,
    requested_sample_rate_hz: float,
    source_metadata: dict,
) -> dict:
    """Create session metadata without depending on a concrete source class."""
    source_metadata = dict(source_metadata)
    document = {
        "format": SESSION_FORMAT,
        "version": SESSION_VERSION,
        "workspace": "Open Road",
        "session_id": session_id,
        "status": "recording",
        "created_utc": utc_now_iso(),
        "recording_started_utc": None,
        "finished_utc": None,
        "timebase": {
            "kind": "monotonic_seconds_from_recording_start",
            "column": "time_s",
        },
        "source": source_metadata,
        "telemetry": {
            "file": "location.csv",
            "columns": ["time_s", "x", "y", "z"],
            "requested_sample_rate_hz": float(requested_sample_rate_hz),
            "sample_count": 0,
            "dropped_samples": 0,
            "duration_s": 0.0,
        },
        "replay": {
            "heading_source": "derived_from_consecutive_xy_samples",
        },
    }

    # Preserve the convenient v1 QLabs metadata block when the active source
    # is QLabs. Replay loading does not depend on this block.
    if source_metadata.get("kind") == "qlabs_qcar2_world_transform":
        document["qlabs"] = {
            "host": source_metadata.get("host", "localhost"),
            "source_actor_number": int(source_metadata.get("actor_number", 0)),
        }
    return document


@dataclass(frozen=True)
class PoseSample:
    time_s: float
    x: float
    y: float
    z: float
    yaw_rad: float


@dataclass(frozen=True)
class PassCandidate:
    time_s: float
    x: float
    y: float
    z: float
    distance_m: float
    index: int


class SessionData:
    """Timestamped drive session loaded from session.json + location.csv."""

    def __init__(
        self,
        folder: Path,
        metadata: dict,
        times: list[float],
        xs: list[float],
        ys: list[float],
        zs: list[float],
    ) -> None:
        if not times:
            raise ValueError("The recording contains no valid location samples.")
        if not (len(times) == len(xs) == len(ys) == len(zs)):
            raise ValueError("Telemetry columns have inconsistent lengths.")

        self.folder = Path(folder).resolve()
        self.metadata = metadata
        self.times = times
        self.xs = xs
        self.ys = ys
        self.zs = zs
        self.yaws = self._derive_yaws()

    @classmethod
    def load(cls, path: Path | str) -> "SessionData":
        path = Path(path).expanduser().resolve()
        if path.is_dir():
            session_json = path / "session.json"
        elif path.name.lower() == "session.json":
            session_json = path
        else:
            raise ValueError("Select a session folder or its session.json file.")

        if not session_json.is_file():
            raise FileNotFoundError(f"session.json not found: {session_json}")

        with session_json.open("r", encoding="utf-8") as handle:
            metadata = json.load(handle)

        if metadata.get("format") != SESSION_FORMAT:
            raise ValueError(
                f"Unsupported session format: {metadata.get('format')!r}. "
                f"Expected {SESSION_FORMAT!r}."
            )
        if int(metadata.get("version", 0)) > SESSION_VERSION:
            raise ValueError(
                f"Session version {metadata.get('version')} is newer than this replay tool."
            )

        telemetry = metadata.get("telemetry", {})
        telemetry_file = telemetry.get("file", "location.csv")
        csv_path = session_json.parent / telemetry_file
        if not csv_path.is_file():
            raise FileNotFoundError(f"Telemetry file not found: {csv_path}")

        times: list[float] = []
        xs: list[float] = []
        ys: list[float] = []
        zs: list[float] = []

        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"time_s", "x", "y", "z"}
            if not required.issubset(set(reader.fieldnames or [])):
                raise ValueError(
                    f"Telemetry must contain columns {sorted(required)}; "
                    f"found {reader.fieldnames}."
                )

            last_time = -math.inf
            for row_number, row in enumerate(reader, start=2):
                try:
                    t = float(row["time_s"])
                    x = float(row["x"])
                    y = float(row["y"])
                    z = float(row["z"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid telemetry value on CSV row {row_number}.") from exc

                if not all(math.isfinite(v) for v in (t, x, y, z)):
                    continue
                if t < last_time:
                    raise ValueError(
                        f"Telemetry time goes backwards at row {row_number}: {t} < {last_time}."
                    )
                if t == last_time and times:
                    # Keep the latest sample for an exactly duplicated timestamp.
                    xs[-1], ys[-1], zs[-1] = x, y, z
                    continue

                times.append(t)
                xs.append(x)
                ys.append(y)
                zs.append(z)
                last_time = t

        if not times:
            raise ValueError("Telemetry CSV has no valid samples.")

        # Replay always starts at 0. If an external source wrote a small non-zero
        # first timestamp, normalize it without changing the sample spacing.
        first_time = times[0]
        if abs(first_time) > 1e-9:
            times = [t - first_time for t in times]

        return cls(session_json.parent, metadata, times, xs, ys, zs)

    @property
    def sample_count(self) -> int:
        return len(self.times)

    @property
    def duration_s(self) -> float:
        return float(self.times[-1]) if self.times else 0.0

    def _derive_yaws(self) -> list[float]:
        count = len(self.times)
        if count == 1:
            return [0.0]

        yaws: list[float | None] = [None] * count
        last_yaw: float | None = None

        # Use a short forward window rather than a single adjacent sample. This
        # reduces heading jitter from sub-centimetre position noise at low speed.
        window = 4
        for i in range(count):
            j = min(count - 1, i + window)
            if j > i:
                dx = self.xs[j] - self.xs[i]
                dy = self.ys[j] - self.ys[i]
            else:
                # At the final sample, use the backward segment in the forward
                # direction instead of accidentally reversing the heading.
                j = max(0, i - window)
                dx = self.xs[i] - self.xs[j]
                dy = self.ys[i] - self.ys[j]
            if math.hypot(dx, dy) >= 0.05:
                last_yaw = math.atan2(dy, dx)
            yaws[i] = last_yaw

        first_valid = next((value for value in yaws if value is not None), 0.0)
        resolved: list[float] = []
        carry = float(first_valid)
        for value in yaws:
            if value is not None:
                carry = float(value)
            resolved.append(carry)
        return resolved

    def pose_at(self, time_s: float) -> PoseSample:
        t = max(0.0, min(float(time_s), self.duration_s))
        if len(self.times) == 1 or t <= self.times[0]:
            return PoseSample(t, self.xs[0], self.ys[0], self.zs[0], self.yaws[0])
        if t >= self.times[-1]:
            i = len(self.times) - 1
            return PoseSample(t, self.xs[i], self.ys[i], self.zs[i], self.yaws[i])

        right = bisect_right(self.times, t)
        i0 = right - 1
        i1 = right
        t0 = self.times[i0]
        t1 = self.times[i1]
        alpha = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)

        x = self.xs[i0] + alpha * (self.xs[i1] - self.xs[i0])
        y = self.ys[i0] + alpha * (self.ys[i1] - self.ys[i0])
        z = self.zs[i0] + alpha * (self.zs[i1] - self.zs[i0])

        yaw0 = self.yaws[i0]
        yaw1 = self.yaws[i1]
        delta = math.atan2(math.sin(yaw1 - yaw0), math.cos(yaw1 - yaw0))
        yaw = yaw0 + alpha * delta
        return PoseSample(t, x, y, z, yaw)

    def find_passes(
        self,
        x: float,
        y: float,
        radius_m: float,
        merge_gap_s: float = 0.75,
    ) -> list[PassCandidate]:
        """Find separate recorded visits near an XY click.

        Consecutive points inside the click radius become one visit. Small gaps
        are merged to avoid creating multiple candidates from threshold jitter.
        The closest sample of each visit is returned.
        """
        radius_sq = float(radius_m) ** 2
        candidates: list[PassCandidate] = []
        active_best: tuple[float, int] | None = None
        last_hit_time: float | None = None

        def close_group() -> None:
            nonlocal active_best, last_hit_time
            if active_best is None:
                return
            distance_sq, index = active_best
            candidates.append(
                PassCandidate(
                    time_s=self.times[index],
                    x=self.xs[index],
                    y=self.ys[index],
                    z=self.zs[index],
                    distance_m=math.sqrt(distance_sq),
                    index=index,
                )
            )
            active_best = None
            last_hit_time = None

        for index, (sx, sy, st) in enumerate(zip(self.xs, self.ys, self.times)):
            distance_sq = (sx - x) ** 2 + (sy - y) ** 2
            if distance_sq <= radius_sq:
                if (
                    active_best is not None
                    and last_hit_time is not None
                    and st - last_hit_time > merge_gap_s
                ):
                    close_group()

                if active_best is None or distance_sq < active_best[0]:
                    active_best = (distance_sq, index)
                last_hit_time = st
            elif (
                active_best is not None
                and last_hit_time is not None
                and st - last_hit_time > merge_gap_s
            ):
                close_group()

        close_group()
        return candidates

    def nearest_sample(self, x: float, y: float) -> PassCandidate:
        best_distance_sq = math.inf
        best_index = 0
        for index, (sx, sy) in enumerate(zip(self.xs, self.ys)):
            distance_sq = (sx - x) ** 2 + (sy - y) ** 2
            if distance_sq < best_distance_sq:
                best_distance_sq = distance_sq
                best_index = index
        return PassCandidate(
            time_s=self.times[best_index],
            x=self.xs[best_index],
            y=self.ys[best_index],
            z=self.zs[best_index],
            distance_m=math.sqrt(best_distance_sq),
            index=best_index,
        )

    def xyz_points(self) -> list[list[float]]:
        return [[x, y, z] for x, y, z in zip(self.xs, self.ys, self.zs)]


def find_open_road_reference(explicit: Path | None, script_path: Path) -> Path:
    if explicit is not None:
        explicit = Path(explicit).expanduser().resolve()
        if explicit.is_file():
            return explicit
        raise FileNotFoundError(f"Open Road reference not found: {explicit}")

    script_dir = Path(script_path).resolve().parent
    candidates = [
        script_dir / "data" / "open_road_reference.json",
        script_dir.parent / "data" / "open_road_reference.json",
        Path.cwd() / "data" / "open_road_reference.json",
        Path.cwd() / "open_road_reference.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "Could not find data/open_road_reference.json. Use --reference PATH."
    )


def load_open_road_reference(path: Path) -> tuple[dict, list[list[float]]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    if document.get("workspace") != "Open Road":
        raise ValueError("The reference JSON is not for the Open Road workspace.")

    raw_points = document.get("road_reference", {}).get("points", [])
    points: list[list[float]] = []
    for point in raw_points:
        if len(point) < 2:
            continue
        try:
            x = float(point[0])
            y = float(point[1])
            z = float(point[2]) if len(point) >= 3 else 0.0
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in (x, y, z)):
            points.append([x, y, z])

    if len(points) < 2:
        raise ValueError("Open Road reference contains too few valid points.")
    return document, points


def cumulative_xy_distances(points: Sequence[Sequence[float]]) -> list[float]:
    distances = [0.0]
    for i in range(1, len(points)):
        distances.append(
            distances[-1]
            + math.hypot(
                float(points[i][0]) - float(points[i - 1][0]),
                float(points[i][1]) - float(points[i - 1][1]),
            )
        )
    return distances


def extract_stable_completed_loop(
    points: list[list[float]],
    seam_lead_in_m: float = 200.0,
    minimum_lap_m: float = 10_000.0,
    return_radius_m: float = 25.0,
) -> tuple[list[list[float]], dict]:
    """Extract the first complete Open Road lap with a stable straight-road seam."""
    if len(points) < 2:
        return points, {"closed": False, "distance_m": 0.0}

    cumulative = cumulative_xy_distances(points)
    seam_index = next(
        (i for i, distance in enumerate(cumulative) if distance >= seam_lead_in_m),
        0,
    )
    sx, sy = points[seam_index][:2]
    entered_return_zone = False
    best_return: tuple[float, int, float] | None = None

    for index in range(seam_index + 1, len(points)):
        travelled = cumulative[index] - cumulative[seam_index]
        if travelled < minimum_lap_m:
            continue
        x, y = points[index][:2]
        distance_to_start = math.hypot(x - sx, y - sy)
        if distance_to_start <= return_radius_m:
            entered_return_zone = True
            if best_return is None or distance_to_start < best_return[0]:
                best_return = (distance_to_start, index, travelled)
            continue
        if entered_return_zone:
            break

    if best_return is None:
        return [list(p) for p in points], {
            "closed": False,
            "distance_m": cumulative[-1],
            "return_distance_m": None,
        }

    return_distance, end_index, travelled = best_return
    loop = [list(p) for p in points[seam_index : end_index + 1]]
    if len(loop) >= 2:
        loop[-1][0] = loop[0][0]
        loop[-1][1] = loop[0][1]
    return loop, {
        "closed": True,
        "start_index": seam_index,
        "end_index": end_index,
        "distance_m": travelled,
        "return_distance_m": return_distance,
    }


def point_segment_distance_sq(
    px: float, py: float, a: Sequence[float], b: Sequence[float]
) -> tuple[float, float]:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx = bx - ax
    dy = by - ay
    denominator = dx * dx + dy * dy
    if denominator <= 1e-12:
        return (px - ax) ** 2 + (py - ay) ** 2, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / denominator
    t = max(0.0, min(1.0, t))
    qx = ax + t * dx
    qy = ay + t * dy
    return (px - qx) ** 2 + (py - qy) ** 2, t


def rdp_simplify(points: list[list[float]], tolerance_m: float = 2.0) -> list[list[float]]:
    """Iterative XY RDP simplification."""
    if len(points) <= 2:
        return list(points)

    # First cap the input size for very long 20 Hz recordings. This uniform
    # pre-decimation preserves temporal route order and keeps RDP responsive.
    max_input = 12_000
    if len(points) > max_input:
        original_last = points[-1]
        stride = max(1, math.ceil(len(points) / max_input))
        points = points[::stride]
        if points[-1] is not original_last:
            points = points + [original_last]

    tolerance_sq = float(tolerance_m) ** 2
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        a = points[start]
        b = points[end]
        best_distance_sq = -1.0
        best_index: int | None = None
        for index in range(start + 1, end):
            p = points[index]
            distance_sq, _ = point_segment_distance_sq(float(p[0]), float(p[1]), a, b)
            if distance_sq > best_distance_sq:
                best_distance_sq = distance_sq
                best_index = index
        if best_index is not None and best_distance_sq > tolerance_sq:
            keep[best_index] = True
            stack.append((start, best_index))
            stack.append((best_index, end))

    return [point for index, point in enumerate(points) if keep[index]]


def offset_polyline_xy(
    points: list[list[float]], offset_m: float, closed: bool = True
) -> list[tuple[float, float]]:
    clean = [(float(p[0]), float(p[1])) for p in points if len(p) >= 2]
    if (
        closed
        and len(clean) >= 3
        and math.hypot(clean[-1][0] - clean[0][0], clean[-1][1] - clean[0][1]) < 1e-6
    ):
        clean = clean[:-1]
    if len(clean) < 2:
        return clean

    result: list[tuple[float, float]] = []
    count = len(clean)
    for index, (x, y) in enumerate(clean):
        if closed:
            px, py = clean[(index - 1) % count]
            nx, ny = clean[(index + 1) % count]
        elif index == 0:
            px, py = clean[0]
            nx, ny = clean[1]
        elif index == count - 1:
            px, py = clean[-2]
            nx, ny = clean[-1]
        else:
            px, py = clean[index - 1]
            nx, ny = clean[index + 1]
        tx, ty = nx - px, ny - py
        length = math.hypot(tx, ty)
        if length <= 1e-9:
            result.append((x, y))
            continue
        normal_x, normal_y = -ty / length, tx / length
        result.append((x + normal_x * offset_m, y + normal_y * offset_m))
    return result
