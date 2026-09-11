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
SESSION_VERSION = 2
DEFAULT_SAMPLE_RATE_HZ = 20.0


OPEN_ROAD_LANE_REFERENCE_FILES = {
    "upper_right": "open_road_reference_upper_right_lane.json",
    "upper_middle": "open_road_reference_upper_middle_lane.json",
    "upper_left": "open_road_reference_upper_left_lane.json",
    "lower_right": "open_road_reference_lower_right_lane.json",
    "lower_middle": "open_road_reference_lower_middle_lane.json",
    "lower_left": "open_road_reference_lower_left_lane.json",
}

# Straight-road calibration from the QLabs coordinate-helper screenshots.
# Lane centers are the measured JSON trajectories at +/-10, +/-6 and +/-2 m.
# The painted dividers round cleanly to +/-8 and +/-4 m, outer edges to +/-12 m,
# and the two median-side pavement edges are approximately +/-0.6 m.
OPEN_ROAD_MEDIAN_HALF_WIDTH_M = 0.6
OPEN_ROAD_CARRIAGEWAY_WIDTH_M = 12.0 - OPEN_ROAD_MEDIAN_HALF_WIDTH_M


@dataclass(frozen=True)
class OpenRoadLaneGeometry:
    lane_centers: dict[str, list[list[float]]]
    lane_dividers: dict[str, list[list[float]]]
    outer_edges: dict[str, list[list[float]]]
    median_edges: dict[str, list[list[float]]]
    median_center: list[list[float]]

    def all_xy_paths(self) -> list[list[list[float]]]:
        return (
            list(self.lane_centers.values())
            + list(self.lane_dividers.values())
            + list(self.outer_edges.values())
            + list(self.median_edges.values())
            + [self.median_center]
        )


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
            "kind": "obs_aligned_monotonic_seconds",
            "column": "time_s",
            "description": (
                "Seconds from the estimated first OBS-recorded video frame. "
                "Telemetry may begin slightly after t=0 while startup completes."
            ),
        },
        "source": source_metadata,
        "telemetry": {
            "file": "location.csv",
            "columns": [
                "time_s", "scheduled_time_s", "x", "y", "z",
                "roll_rad", "pitch_rad", "yaw_rad", "valid"
            ],
            "requested_sample_rate_hz": float(requested_sample_rate_hz),
            "sample_count": 0,
            "dropped_samples": 0,
            "duration_s": 0.0,
        },
        "videos": {},
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
        recorded_yaws: list[float | None] | None = None,
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
        derived = self._derive_yaws()
        if recorded_yaws is None or len(recorded_yaws) != len(times):
            self.yaws = derived
        else:
            self.yaws = [
                derived[i] if value is None or not math.isfinite(value) else float(value)
                for i, value in enumerate(recorded_yaws)
            ]

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
        recorded_yaws: list[float | None] = []

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
                    yaw_raw = row.get("yaw_rad")
                    yaw = None if yaw_raw in (None, "") else float(yaw_raw)
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
                    recorded_yaws[-1] = yaw
                    continue

                times.append(t)
                xs.append(x)
                ys.append(y)
                zs.append(z)
                recorded_yaws.append(yaw)
                last_time = t

        if not times:
            raise ValueError("Telemetry CSV has no valid samples.")

        # Legacy v1 sessions defined t=0 as the first telemetry sample, so keep
        # that behavior for old recordings. v2 sessions are aligned to the OBS
        # video timebase and must preserve their original timestamps.
        if int(metadata.get("version", 1)) <= 1:
            first_time = times[0]
            if abs(first_time) > 1e-9:
                times = [t - first_time for t in times]

        return cls(
            session_json.parent, metadata, times, xs, ys, zs,
            recorded_yaws=recorded_yaws,
        )

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


def find_open_road_lane_reference_files(reference_path: Path) -> dict[str, Path]:
    """Return all six measured Open Road lane-reference files when available.

    The search is intentionally anchored to the directory containing the normal
    open_road_reference.json so a copied repository remains self-contained.
    An empty dictionary means the reviewer should use the legacy single-reference
    rendering instead of partially mixing reference models.
    """
    data_dir = Path(reference_path).resolve().parent
    found = {
        name: data_dir / filename
        for name, filename in OPEN_ROAD_LANE_REFERENCE_FILES.items()
    }
    if not all(path.is_file() for path in found.values()):
        return {}
    return {name: path.resolve() for name, path in found.items()}


def resample_polyline_by_distance_fraction(
    points: list[list[float]],
    sample_count: int = 1600,
) -> list[list[float]]:
    """Resample a closed/open XYZ polyline uniformly by normalized XY arc length."""
    if len(points) < 2:
        return [list(p) for p in points]
    sample_count = max(16, int(sample_count))

    clean = [list(map(float, p[:3])) for p in points if len(p) >= 3]
    if len(clean) < 2:
        return clean

    # Avoid a duplicated closure point while computing the cumulative distance.
    closed = math.hypot(
        clean[-1][0] - clean[0][0], clean[-1][1] - clean[0][1]
    ) < 1e-6
    if closed and len(clean) > 2:
        clean = clean[:-1]

    distances = cumulative_xy_distances(clean)
    total = distances[-1]
    if total <= 1e-9:
        result = [list(clean[0]) for _ in range(sample_count)]
        if closed:
            result.append(list(result[0]))
        return result

    result: list[list[float]] = []
    targets = [total * i / sample_count for i in range(sample_count)]
    segment = 0
    for target in targets:
        while segment + 1 < len(distances) and distances[segment + 1] < target:
            segment += 1
        next_segment = min(segment + 1, len(clean) - 1)
        d0 = distances[segment]
        d1 = distances[next_segment]
        alpha = 0.0 if d1 <= d0 else (target - d0) / (d1 - d0)
        a = clean[segment]
        b = clean[next_segment]
        result.append([
            a[0] + alpha * (b[0] - a[0]),
            a[1] + alpha * (b[1] - a[1]),
            a[2] + alpha * (b[2] - a[2]),
        ])

    if closed and result:
        result.append(list(result[0]))
    return result


def blend_paths(
    path_a: list[list[float]],
    path_b: list[list[float]],
    weight_b: float,
) -> list[list[float]]:
    """Blend already aligned XYZ paths; weight_b=0.5 gives their midpoint."""
    if len(path_a) != len(path_b):
        raise ValueError("Aligned Open Road paths must have equal sample counts.")
    w = float(weight_b)
    return [
        [
            float(a[0]) + w * (float(b[0]) - float(a[0])),
            float(a[1]) + w * (float(b[1]) - float(a[1])),
            float(a[2]) + w * (float(b[2]) - float(a[2])),
        ]
        for a, b in zip(path_a, path_b)
    ]


def extrapolate_from_neighbor(
    outer: list[list[float]],
    inner: list[list[float]],
    factor: float,
) -> list[list[float]]:
    """Move from an outer lane center away from its adjacent inner center."""
    if len(outer) != len(inner):
        raise ValueError("Aligned Open Road paths must have equal sample counts.")
    f = float(factor)
    return [
        [
            float(a[0]) + f * (float(a[0]) - float(b[0])),
            float(a[1]) + f * (float(a[1]) - float(b[1])),
            float(a[2]) + f * (float(a[2]) - float(b[2])),
        ]
        for a, b in zip(outer, inner)
    ]


def align_target_to_reference(
    reference: list[list[float]],
    target: list[list[float]],
    search_window: int = 18,
) -> list[list[float]]:
    """Project each reference sample to the nearby portion of a target lane.

    Lane loops have slightly different arc lengths, so pairing them by the same
    normalized distance fraction can shift curved sections by several metres.
    We use normalized progress only as a search hint, then choose the nearest
    local target segment geometrically. This preserves route order while keeping
    adjacent lane geometry spatially aligned.
    """
    if len(reference) < 2 or len(target) < 2:
        return [list(p) for p in target]

    ref_closed = math.hypot(
        reference[-1][0] - reference[0][0], reference[-1][1] - reference[0][1]
    ) < 1e-6
    tgt_closed = math.hypot(
        target[-1][0] - target[0][0], target[-1][1] - target[0][1]
    ) < 1e-6

    ref_core = reference[:-1] if ref_closed else reference
    tgt_core = target[:-1] if tgt_closed else target
    n_ref = len(ref_core)
    n_tgt = len(tgt_core)
    if n_ref < 2 or n_tgt < 2:
        return [list(p) for p in target]

    window = max(3, int(search_window))
    result: list[list[float]] = []

    for i, p in enumerate(ref_core):
        # Preserve exact common start calibration, e.g. lane centers at
        # +/-10, +/-6 and +/-2 m on the straight section.
        if i == 0:
            result.append(list(tgt_core[0]))
            continue

        fraction = i / n_ref if ref_closed else i / max(1, n_ref - 1)
        expected = int(round(fraction * n_tgt)) % n_tgt
        best: tuple[float, list[float]] | None = None

        for delta in range(-window, window + 1):
            j0 = (expected + delta) % n_tgt if tgt_closed else expected + delta
            if not tgt_closed and not (0 <= j0 < n_tgt - 1):
                continue
            j1 = (j0 + 1) % n_tgt
            a = tgt_core[j0]
            b = tgt_core[j1]
            distance_sq, alpha = point_segment_distance_sq(
                float(p[0]), float(p[1]), a, b
            )
            if best is None or distance_sq < best[0]:
                best = (
                    distance_sq,
                    [
                        float(a[0]) + alpha * (float(b[0]) - float(a[0])),
                        float(a[1]) + alpha * (float(b[1]) - float(a[1])),
                        float(a[2]) + alpha * (float(b[2]) - float(a[2])),
                    ],
                )

        if best is None:
            result.append(list(tgt_core[expected]))
        else:
            result.append(best[1])

    if ref_closed and result:
        result.append(list(result[0]))
    return result


def blend_aligned_lane_pair(
    reference: list[list[float]],
    target: list[list[float]],
    weight_target: float,
) -> list[list[float]]:
    aligned_target = align_target_to_reference(reference, target)
    return blend_paths(reference, aligned_target, weight_target)


def build_open_road_lane_geometry(
    lane_loops: dict[str, list[list[float]]],
    sample_count: int = 1600,
) -> OpenRoadLaneGeometry:
    """Build a smooth six-lane replay map from measured lane-center loops.

    The six JSON trajectories are authoritative lane centers. Painted dividers
    are approximated geometrically between neighboring centers. Outer edges are
    extrapolated half a lane spacing, yielding +/-12 m on the calibrated
    straight. Median-side edges are interpolated to +/-0.6 m on that straight.

    Each lane is independently resampled by distance, then neighboring lanes are
    locally aligned before interpolation. This avoids phase error in curves when
    the inside and outside lanes have different total lap lengths.
    """
    required = tuple(OPEN_ROAD_LANE_REFERENCE_FILES)
    missing = [name for name in required if name not in lane_loops]
    if missing:
        raise ValueError(f"Missing Open Road lane reference(s): {', '.join(missing)}")

    centers = {
        name: resample_polyline_by_distance_fraction(lane_loops[name], sample_count)
        for name in required
    }

    ur = centers["upper_right"]
    um = centers["upper_middle"]
    ul = centers["upper_left"]
    lr = centers["lower_right"]
    lm = centers["lower_middle"]
    ll = centers["lower_left"]

    # Pair each neighboring lane spatially, not just by normalized arc length.
    um_on_ur = align_target_to_reference(ur, um)
    ul_on_um = align_target_to_reference(um, ul)
    lm_on_lr = align_target_to_reference(lr, lm)
    ll_on_lm = align_target_to_reference(lm, ll)
    lr_on_ul = align_target_to_reference(ul, lr)

    dividers = {
        "upper_right_middle": blend_paths(ur, um_on_ur, 0.5),
        "upper_middle_left": blend_paths(um, ul_on_um, 0.5),
        "lower_right_middle": blend_paths(lr, lm_on_lr, 0.5),
        "lower_middle_left": blend_paths(lm, ll_on_lm, 0.5),
    }

    # The calibrated straight has inner lane centers at +2 and -2 m. The
    # median-side pavement edges are +/-0.6 m, i.e. 35% and 65% across that gap.
    median_upper_weight = (2.0 - OPEN_ROAD_MEDIAN_HALF_WIDTH_M) / 4.0  # 0.35
    median_lower_weight = (2.0 + OPEN_ROAD_MEDIAN_HALF_WIDTH_M) / 4.0  # 0.65
    median_edges = {
        "upper": blend_paths(ul, lr_on_ul, median_upper_weight),
        "lower": blend_paths(ul, lr_on_ul, median_lower_weight),
    }
    median_center = blend_paths(ul, lr_on_ul, 0.5)

    # Extend the outermost lane center by half the measured adjacent-lane vector.
    outer_edges = {
        "upper": extrapolate_from_neighbor(ur, um_on_ur, 0.5),
        "lower": extrapolate_from_neighbor(ll_on_lm, lm, 0.5),
    }

    return OpenRoadLaneGeometry(
        lane_centers=centers,
        lane_dividers=dividers,
        outer_edges=outer_edges,
        median_edges=median_edges,
        median_center=median_center,
    )

def load_open_road_six_lane_geometry(
    reference_path: Path,
    sample_count: int = 1600,
) -> OpenRoadLaneGeometry | None:
    """Load all six lane JSONs beside the base reference, or return None."""
    files = find_open_road_lane_reference_files(reference_path)
    if not files:
        return None

    loops: dict[str, list[list[float]]] = {}
    for name, path in files.items():
        _document, raw = load_open_road_reference(path)
        loop, info = extract_stable_completed_loop(raw, seam_lead_in_m=0.0)
        if not info.get("closed"):
            raise ValueError(f"Lane reference {path.name} does not contain a complete loop.")
        loops[name] = loop
    return build_open_road_lane_geometry(loops, sample_count=sample_count)
