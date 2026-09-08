# QLabs Drive Replay v2 — Grouped OOP Architecture

This version keeps the v2 OOP design but groups the source files by responsibility so the project structure mirrors the architecture.

## Project layout

```text
QLabs_Drive_Replay_v2_OOP_grouped/
├── apps/
│   ├── __init__.py
│   ├── location_recorder.py
│   └── open_road_replay.py
│
├── core/
│   ├── __init__.py
│   ├── oop_interfaces.py
│   ├── recorder_core.py
│   ├── replay_clock.py
│   ├── replay_controller.py
│   └── replay_core.py
│
├── integrations/
│   ├── __init__.py
│   ├── location_sources.py
│   ├── qlabs_replay.py
│   └── replay_sinks.py
│
├── ui/
│   ├── __init__.py
│   └── replay_video_window.py
│
├── data/
│   └── open_road_reference.json
│
├── recordings/
│   └── .gitkeep
│
├── docs/
│   └── OOP_ARCHITECTURE.md
│
├── scripts/
│   ├── run_recorder.bat
│   └── run_replay.bat
│
├── tests/
│   └── test_oop_core.py
│
├── requirements.txt
├── run_recorder.py
├── run_replay.py
├── run_recorder.bat
└── run_replay.bat
```

## Folder responsibilities

### `apps/`
Application entry-point GUI code.

- `location_recorder.py` — recorder window
- `open_road_replay.py` — map/timeline replay window

### `core/`
Application-independent logic and OOP contracts.

- `oop_interfaces.py` — `LocationSource` and `ReplaySink` abstract classes
- `recorder_core.py` — timestamped recording engine
- `replay_core.py` — session model, interpolation, map geometry
- `replay_clock.py` — master replay clock
- `replay_controller.py` — polymorphic replay coordinator

### `integrations/`
Adapters to external systems or concrete replay/recording endpoints.

- `location_sources.py` — QLabs QCar recording source
- `qlabs_replay.py` — QLabs replay sink
- `replay_sinks.py` — map/video replay adapters

This is also the natural folder for future LSL implementations.

### `ui/`
Reusable presentation components.

- `replay_video_window.py` — second-screen video window

### `data/`
Static reference data.

- `open_road_reference.json` — measured Open Road XYZ reference

### `recordings/`
Generated session folders.

### `docs/`
Architecture/documentation.

### `scripts/`
Convenience OS launch scripts.

### `tests/`
Automated tests.

## Quick start

From the project root:

### Recorder

```bash
python run_recorder.py
```

or double-click:

```text
run_recorder.bat
```

### Replay

```bash
python run_replay.py
```

or double-click:

```text
run_replay.bat
```

The launchers keep the grouped package structure hidden from the normal user workflow.

## OOP structure

The architecture remains:

```text
LocationSource <<abstract>>
        ↑
QLabsQCarLocationSource
        ↓
RecorderWorker
```

and:

```text
ReplaySink <<abstract>>
      ↑        ↑        ↑
      │        │        │
 MapReplay   QLabs    VideoReplay
   Sink      Replay      Sink
             Sink
      \        |        /
       \       |       /
        ReplayCoordinator
               ↓
          ReplayClock
```

The folder grouping now reinforces the same responsibilities:

```text
core/          abstractions + domain logic
integrations/  concrete external implementations
apps/          top-level application coordination/UI
ui/            reusable windows/widgets
```

## Requirements

- Python 3.10+
- PySide6
- Quanser QLabs Python `qvl` package for recording/direct replay

Install dependencies as needed:

```bash
pip install -r requirements.txt
```

## Tests

From the project root:

```bash
python -m unittest discover -s tests -v
```

## Future LSL integration

Future LSL classes can be grouped under `integrations/`, for example:

```text
integrations/
├── lsl_location_source.py
└── lsl_event_replay_sink.py
```

They can inherit the existing abstractions without modifying the recorder or replay coordinator.
