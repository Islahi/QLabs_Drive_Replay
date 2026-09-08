# QLabs Drive Replay v1

A clean recording/replay toolset for Open Road. This package is intentionally
separate from the Track Maker: the existing Open Road map is used only as a
spatial reference.

## What is implemented

### 1. Timestamped location recorder

`location_recorder.py`

- Connects to an **existing** QCar2 actor in QLabs.
- Records `time_s,x,y,z` continuously at 20 Hz by default.
- **No distance filter**: stationary time is retained.
- Creates one folder per drive:

```text
recordings/
└── 2026-09-08_132500_participant_001/
    ├── session.json
    └── location.csv
```

### 2. Interactive map replay

`open_road_replay.py`

- Same Open Road visual concept as the click-to-camera utility.
- Blue line = this recorded drive.
- Yellow dotted line = Open Road measured reference.
- Play / pause / seek timeline.
- Current QCar marker follows synchronized session time.
- Click the recorded route to jump to that time.
- If the same location was visited on several laps, a menu shows each pass and
  timestamp instead of guessing.
- Space = play/pause, Left/Right = ±1 second.

### 3. Direct QLabs replay

The replay window can optionally connect to QLabs and spawn/reuse a dedicated
QCar actor (default actor 900).

The actor is updated from the recorded XYZ using transform-based playback with
dynamics disabled. Heading is derived from consecutive XY samples because v1
records only location + time.

Buttons allow the replay QCar's trailing, overhead, or front camera to be
possessed in QLabs.

**Important:** this reconstructs the recorded QCar motion in the static Open
Road workspace. Other experiment actors/events are not reconstructed until a
future event/LSL stream is added.

### 4. Separate synchronized video window

`replay_video_window.py` is used by the main replay application.

- Video is in a separate top-level window for a two-monitor setup.
- Main replay clock remains the master clock.
- Load a video from the replay window.
- `Move to Next Screen` moves/maximizes it on the next detected monitor.
- Adjustable synchronization offset:

```text
video_position = session_time + offset
```

This keeps the architecture ready for a future LSL master timeline.

## Requirements

- Python 3.10+
- PySide6
- Quanser QLabs Python `qvl` package for recording and direct QLabs replay
- QLabs Open Road workspace loaded when using QLabs features

Install the GUI dependency if needed:

```bash
pip install PySide6
```

The Quanser `qvl` package normally comes from the Quanser/QLabs installation;
it is not bundled here.

For video replay, MP4/H.264 is a good default. Qt Multimedia uses the codecs
available through its platform multimedia backend.

## Quick start

### Record

Load Open Road and your QCar in QLabs, then:

```bash
python location_recorder.py
```

Choose actor 0 (or another existing QCar), click **Start Recording**, then
**Stop** when finished.

### Replay map only

```bash
python open_road_replay.py
```

Click **Load Session…** and select the recording folder.

Or:

```bash
python open_road_replay.py --session recordings/2026-09-08_132500_participant_001
```

### Replay in QLabs

1. Open the **Open Road** workspace in QLabs.
2. Load a session in the replay tool.
3. Click **Connect Replay**.
4. Play or scrub the timeline.
5. Use **Trailing**, **Overhead**, or **Front** to inspect the replay from QLabs.

The default replay actor is 900 so it does not collide with normal experiment
actor numbers. Change it in the UI if necessary.

### Two-screen video

1. Load the session.
2. Click **Load Video…**.
3. The separate video window opens.
4. Click **Move to Next Screen** on the video window.
5. Adjust the video offset if its recording did not begin at the exact same
   instant as location logging.

## Session format v1

`session.json` contains metadata and points to `location.csv`.

Example:

```json
{
  "format": "qlabs_drive_session",
  "version": 1,
  "workspace": "Open Road",
  "session_id": "2026-09-08_132500_participant_001",
  "status": "complete",
  "timebase": {
    "kind": "monotonic_seconds_from_recording_start",
    "column": "time_s"
  },
  "qlabs": {
    "host": "localhost",
    "source_actor_number": 0
  },
  "telemetry": {
    "file": "location.csv",
    "columns": ["time_s", "x", "y", "z"],
    "requested_sample_rate_hz": 20.0
  }
}
```

`location.csv`:

```csv
time_s,x,y,z
0.000000,0.788000,6.420000,1.123000
0.050041,0.612000,6.421000,1.123000
0.100034,0.431000,6.420000,1.124000
```

The timestamp is the **actual monotonic sample time**, not a synthetic
`sample_index / 20`. This is important when QLabs or Windows briefly delays a
sample.

## Future LSL integration

The replay application already has one master `ReplayClock`. The intended LSL
integration is to make LSL/event streams another data source indexed by the
same session time rather than redesigning the map/video UI.

Likely future session additions:

```text
lsl.xdf / lsl_events.csv
video metadata
experiment event stream
additional actors
```

The existing `location.csv` and v1 sessions remain readable.
