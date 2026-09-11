# QLabs Drive Replay — synchronized recorder + two-window reviewer

This repository keeps the existing grouped OOP design and adds synchronized
experiment recording **inside the existing recorder/replay codebase**.

## Current workflow

### Recorder: one button

Run:

```bash
python run_recorder.py
```

Prepare QLabs Open Road with the target QCar already present, and prepare an OBS
scene that captures the QLabs application window. OBS WebSocket must be enabled.

Press **START SESSION** once. The recorder then:

1. connects to the selected QCar in QLabs;
2. forces the QLabs application view to the QCar **front CSI camera**;
3. connects to OBS WebSocket and starts OBS recording;
4. estimates video `t=0` from OBS's reported recording duration;
5. records unfiltered QCar telemetry at the requested rate (default 20 Hz);
6. optionally records left/right/rear QLabs CSI cameras to separate MP4 files;
7. writes OBS/video clock correlation data for replay synchronization.

Press the same button again (**STOP SESSION**) to stop and finalize everything.

The recorder refuses to start a synchronized session if OBS is already
recording, because it would not know where the experiment video begins.

### Replay: exactly two active windows

Run:

```bash
python run_replay.py
```

The current reviewer intentionally opens only:

1. **Map + timeline window**
2. **Video window**

There is **no QLabs replay UI** in this version. The old integration module is
left dormant so a small optional third QLabs replay window can be added later
without changing the session format.

When a synchronized session is loaded, the video window automatically discovers
available sources such as:

- QLabs Front (OBS)
- Left CSI
- Right CSI
- Rear CSI

Switching camera keeps the same experiment time. The map can now load **multiple
recorded sessions simultaneously**. Each recording receives a distinct trajectory
color, while one selected **Active replay** controls the video and master timeline.
At any replay time, all loaded cars with data at that elapsed time are shown on
the map together. Clicking a recorded location searches every loaded trajectory;
choosing a pass can automatically switch the active replay and seek its video.

Use **Add Session...** for one recording, **Add Recordings Folder...** to load all
sessions below a parent directory, the **Active replay** drop-down to change the
video-driving session, **Remove Active** to remove one comparison, and **Clear All**
to clear the map.

Use **Open All Session Videos** when several sessions are loaded. The normal video
window remains attached to the active replay, while every non-active session with
an available recording opens in its own synchronized video window. All windows
follow the same elapsed replay time and playback rate, and each window can choose
its own Front/Left/Right/Rear source. Toggle **Close Comparison Videos** to return
to the single active video window.

The map now uses **six measured Open Road lane-center references** when all six
JSON files are present. The straight-road visual calibration uses rounded
marking coordinates: outer edges at approximately `+/-12 m`, lane dividers at
`+/-8 m` and `+/-4 m`, and median-side pavement edges at approximately
`+/-0.6 m`. Curved markings are smoothly approximated from the measured lane
center trajectories. The pavement is filled directly between the derived road
boundaries, so it keeps the correct world width when zooming instead of shrinking
relative to the lanes. Road edges and dashed lane dividers are also drawn thicker
for easier visual analysis. The recorded participant trajectory is drawn more
strongly than the reference layers so it remains easy to analyze.

## Recording output

A typical session is:

```text
recordings/
└── 2026-09-10_090000_participant_001/
    ├── session.json
    ├── location.csv
    ├── obs_sync.csv
    ├── csi_left.mp4
    ├── csi_left_frames.csv
    ├── csi_right.mp4
    ├── csi_right_frames.csv
    ├── csi_rear.mp4
    └── csi_rear_frames.csv
```

The OBS recording normally remains in the OBS recording directory. Its absolute
path is stored in `session.json`. If you later copy that video into the session
folder, the reviewer also searches there by filename.

## Synchronization model

Session v2 timestamps are referenced to the estimated first OBS-recorded frame.
They are **not normalized to the first telemetry sample** during replay.

`obs_sync.csv` records:

```text
session_time_s,obs_duration_s,...
```

Side-camera timestamp files record:

```text
frame_index,video_time_s,session_time_s,...
```

The video window interpolates these mappings, so OBS and QLabs CSI recordings
can stay aligned to the same replay timeline even if capture starts slightly
later or the clocks drift slightly over a long experiment.

## Project layout

```text
QLabs_Drive_Replay/
├── apps/
│   ├── location_recorder.py
│   └── open_road_replay.py
├── core/
│   ├── oop_interfaces.py
│   ├── recorder_core.py
│   ├── replay_clock.py
│   ├── replay_controller.py
│   └── replay_core.py
├── integrations/
│   ├── location_sources.py
│   ├── recording_services.py
│   ├── replay_sinks.py
│   └── qlabs_replay.py          # dormant/future, not shown in current UI
├── ui/
│   └── replay_video_window.py
├── data/
│   ├── open_road_reference.json                 # legacy/fallback reference
│   ├── open_road_reference_upper_right_lane.json
│   ├── open_road_reference_upper_middle_lane.json
│   ├── open_road_reference_upper_left_lane.json
│   ├── open_road_reference_lower_right_lane.json
│   ├── open_road_reference_lower_middle_lane.json
│   └── open_road_reference_lower_left_lane.json
└── recordings/
```

## Requirements

- Python 3.10+
- PySide6
- Quanser QLabs Python `qvl` package
- OBS Studio with OBS WebSocket enabled
- `obsws-python`
- OpenCV + NumPy for optional left/right/rear video capture

Install the pip dependencies with:

```bash
pip install -r requirements.txt
```

## Recommended first validation

Do a 1–2 minute test before a full experiment. Drive past a recognizable point,
stop the session, open it in the reviewer, click that point, then switch between
Front/Left/Right/Rear in the video window and check that the same moment is
shown across the available recordings.
