from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from integrations.location_sources import QLabsQCarLocationSource
from core.oop_interfaces import LocationReading, LocationSource, ReplaySink
from integrations.qlabs_replay import QLabsReplaySink
from core.recorder_core import RecorderWorker
from core.replay_core import SessionData, build_open_road_lane_geometry
from integrations.replay_sinks import MapReplaySink, VideoReplaySink


class FakeLocationSource(LocationSource):
    def __init__(self) -> None:
        self.index = 0
        self.was_closed = False

    @property
    def name(self) -> str:
        return "Fake source"

    @property
    def metadata(self) -> dict:
        return {"kind": "fake"}

    def connect(self) -> None:
        pass

    def read_position(self) -> LocationReading:
        self.index += 1
        return LocationReading(float(self.index), 2.0, 3.0)

    def close(self) -> None:
        self.was_closed = True


class OOPArchitectureTests(unittest.TestCase):
    def test_abstract_classes_cannot_be_instantiated(self) -> None:
        with self.assertRaises(TypeError):
            LocationSource()
        with self.assertRaises(TypeError):
            ReplaySink()

    def test_concrete_classes_inherit_interfaces(self) -> None:
        self.assertTrue(issubclass(QLabsQCarLocationSource, LocationSource))
        self.assertTrue(issubclass(QLabsReplaySink, ReplaySink))
        self.assertTrue(issubclass(MapReplaySink, ReplaySink))
        self.assertTrue(issubclass(VideoReplaySink, ReplaySink))

    def test_recorder_uses_polymorphic_location_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / "session"
            session_dir.mkdir()
            source = FakeLocationSource()
            worker = RecorderWorker(session_dir, source, sample_rate_hz=50.0)
            worker.start()
            time.sleep(0.13)
            worker.stop()
            worker.join(timeout=2.0)

            state = worker.snapshot()
            self.assertIsNone(state["error"])
            self.assertGreaterEqual(state["sample_count"], 3)
            self.assertTrue(source.was_closed)

            session = SessionData.load(session_dir)
            self.assertEqual(session.sample_count, state["sample_count"])
            self.assertEqual(session.metadata["source"]["kind"], "fake")


    def test_v2_preserves_obs_aligned_time_and_recorded_yaw(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir)
            (session_dir / "session.json").write_text(
                '{"format":"qlabs_drive_session","version":2,'
                '"telemetry":{"file":"location.csv"}}',
                encoding="utf-8",
            )
            (session_dir / "location.csv").write_text(
                "time_s,x,y,z,yaw_rad\n0.250,0,0,1,1.25\n0.300,1,0,1,1.30\n",
                encoding="utf-8",
            )
            session = SessionData.load(session_dir)
            self.assertAlmostEqual(session.times[0], 0.250, places=6)
            self.assertAlmostEqual(session.yaws[0], 1.25, places=6)

    def test_final_heading_does_not_reverse(self) -> None:
        # Regression test for the final-sample yaw derivation.
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir)
            (session_dir / "session.json").write_text(
                '{"format":"qlabs_drive_session","version":1,'
                '"telemetry":{"file":"location.csv"}}',
                encoding="utf-8",
            )
            (session_dir / "location.csv").write_text(
                "time_s,x,y,z\n0,0,0,0\n1,1,0,0\n2,2,0,0\n3,3,0,0\n4,4,0,0\n",
                encoding="utf-8",
            )
            session = SessionData.load(session_dir)
            self.assertAlmostEqual(session.yaws[-1], 0.0, places=6)

    def test_six_lane_geometry_uses_rounded_straight_markings(self) -> None:
        # Synthetic parallel closed paths verify the calibrated straight values.
        def lane(y: float) -> list[list[float]]:
            return [[0.0, y, 1.0], [100.0, y, 1.0], [100.0, y + 20.0, 1.0], [0.0, y + 20.0, 1.0], [0.0, y, 1.0]]

        geometry = build_open_road_lane_geometry(
            {
                "upper_right": lane(10.0),
                "upper_middle": lane(6.0),
                "upper_left": lane(2.0),
                "lower_right": lane(-2.0),
                "lower_middle": lane(-6.0),
                "lower_left": lane(-10.0),
            },
            sample_count=32,
        )
        self.assertAlmostEqual(geometry.outer_edges["upper"][0][1], 12.0, places=6)
        self.assertAlmostEqual(geometry.outer_edges["lower"][0][1], -12.0, places=6)
        self.assertAlmostEqual(geometry.lane_dividers["upper_right_middle"][0][1], 8.0, places=6)
        self.assertAlmostEqual(geometry.lane_dividers["upper_middle_left"][0][1], 4.0, places=6)
        self.assertAlmostEqual(geometry.lane_dividers["lower_right_middle"][0][1], -4.0, places=6)
        self.assertAlmostEqual(geometry.lane_dividers["lower_middle_left"][0][1], -8.0, places=6)
        self.assertAlmostEqual(geometry.median_edges["upper"][0][1], 0.6, places=6)
        self.assertAlmostEqual(geometry.median_edges["lower"][0][1], -0.6, places=6)



if __name__ == "__main__":
    unittest.main()



def test_detect_sustained_motion_start():
    from core.replay_core import detect_sustained_motion_start

    times = [i * 0.1 for i in range(80)]
    xs = []
    ys = []
    for t in times:
        if t < 2.0:
            xs.append(0.002 * (int(t * 10) % 2))  # tiny stationary jitter
        else:
            xs.append((t - 2.0) * 1.0)
        ys.append(0.0)
    detected = detect_sustained_motion_start(
        times, xs, ys, threshold_mps=0.3, required_motion_s=1.0, speed_window_s=0.5
    )
    assert detected is not None
    assert 1.9 <= detected <= 2.1
