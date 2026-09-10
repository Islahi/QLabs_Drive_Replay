# OOP Architecture — QLabs Drive Replay v2

This version makes the four main object-oriented programming principles explicit in the application architecture, not only through Qt inheritance.

## Class structure

```text
                         +----------------------+
                         |    LocationSource    |  <<abstract>>
                         +----------------------+
                         | + connect()          |
                         | + read_position()    |
                         | + close()            |
                         +----------+-----------+
                                    ^
                                    |
                         +----------+-----------+
                         | QLabsQCarLocation... |
                         +----------------------+
                                    |
                                    v
                         +----------------------+
                         |    RecorderWorker    |
                         +----------------------+
                         | depends on interface |
                         +----------------------+


                         +----------------------+
                         |      ReplaySink      |  <<abstract>>
                         +----------------------+
                         | + set_session()      |
                         | + update_time()      |
                         | + set_playing()      |
                         | + set_rate()         |
                         | + close()            |
                         +----------+-----------+
                                    ^
                +-------------------+-------------------+
                |                   |                   |
        +-------+-------+   +-------+-------+   +-------+-------+
        | MapReplaySink |   | QLabsReplaySink|  |VideoReplaySink|
        +---------------+   +---------------+   +---------------+
                \                   |                   /
                 \                  |                  /
                  +-----------------+-----------------+
                                    |
                                    v
                         +----------------------+
                         |  ReplayCoordinator   |
                         +----------------------+
                         | list[ReplaySink]     |
                         | one ReplayClock      |
                         +----------------------+
```

## 1. Encapsulation

State and implementation details are owned by the class responsible for them.

Examples:

- `ReplayClock` owns current time, duration, rate, play state, and wall-clock anchoring.
- `SessionData` owns telemetry arrays, interpolation, yaw derivation, pass lookup, and nearest-point lookup.
- `QLabsQCarLocationSource` owns the QLabs connection and QCar actor handle.
- `QLabsReplaySink` owns replay-actor connection state and QLabs update throttling.
- `VideoWindow` owns `QMediaPlayer`, video offset, player position, and full-screen behavior.

The UI does not manipulate those internal details directly.

## 2. Abstraction

`core/core/oop_interfaces.py` defines two abstract base classes:

### `LocationSource`

The recorder only requires:

```python
source.connect()
reading = source.read_position()
source.close()
```

It does not need to know that the source is QLabs.

### `ReplaySink`

The replay controller only requires:

```python
sink.set_session(session)
sink.update_time(time_s, pose)
sink.set_playing(playing)
sink.set_rate(rate)
```

It does not need to know whether the sink is the map, QLabs, video, or a future LSL component.

## 3. Inheritance

Application-level inheritance is explicit:

```text
QLabsQCarLocationSource -> LocationSource
MapReplaySink           -> ReplaySink
QLabsReplaySink         -> ReplaySink
VideoReplaySink         -> ReplaySink
```

Qt inheritance is still used as well:

```text
RecorderWindow -> QMainWindow
ReplayWindow   -> QMainWindow
ReplayMap      -> QWidget
VideoWindow    -> QMainWindow
ReplayClock    -> QObject
```

## 4. Polymorphism

### Recorder

`RecorderWorker` receives a `LocationSource`:

```python
source: LocationSource
```

The same recording loop works with any concrete implementation of that interface.

Today:

```text
QLabsQCarLocationSource
```

Later, without changing `RecorderWorker`:

```text
LSLLocationSource
RecordedFileLocationSource
TestLocationSource
```

### Replay

`ReplayCoordinator` stores:

```python
list[ReplaySink]
```

At each master replay time it calls the same method on every sink:

```python
for sink in self._sinks:
    sink.update_time(time_s, pose)
```

The result is different according to the object's concrete type:

```text
MapReplaySink    -> moves map marker
QLabsReplaySink  -> moves QLabs QCar
VideoReplaySink  -> seeks/synchronizes video
```

That is application-level runtime polymorphism.

## Why this helps the later LSL integration

A future LSL component can be introduced as another implementation rather than by rewriting the replay window.

Possible examples:

```text
LSLEventReplaySink -> ReplaySink
LSLLocationSource  -> LocationSource
```

The current controller and recorder can continue operating through the existing interfaces.

## Separation of responsibilities

```text
core/replay_core.py           session model + geometry
core/recorder_core.py         recording engine
integrations/location_sources.py      concrete recording inputs
core/oop_interfaces.py        abstract contracts
core/replay_clock.py          master replay clock
core/replay_controller.py     synchronization coordinator
integrations/replay_sinks.py          map/video adapters
integrations/qlabs_replay.py          QLabs replay sink (dormant/future)
apps/open_road_replay.py      map UI / user interaction
ui/replay_video_window.py   video UI
apps/location_recorder.py     recorder UI
```

This keeps domain logic, external systems, synchronization, and presentation separated.


## Grouped project structure

Version 2 grouped layout separates architectural roles physically:

```text
apps/          application entry points and main windows
core/          abstract contracts, session model, clocks, controllers
integrations/  QLabs and replay/recording adapters
ui/            reusable presentation windows
data/          static Open Road reference data
recordings/    generated drive sessions
docs/          architecture documentation
scripts/       convenience launch scripts
tests/         automated tests
```

This structure makes the dependency direction clearer:

```text
apps/
  ↓
core/ ← integrations/
  ↑
 ui/
```

`core/` does not depend on QLabs. Concrete integrations depend on core abstractions.
