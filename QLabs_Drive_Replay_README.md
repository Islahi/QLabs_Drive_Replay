# QLabs Drive Replay

Synchronized experiment recording and interactive replay for the Quanser **QLabs Open Road** workspace.

The project is designed around one experiment clock. A single **START SESSION** button starts the OBS recording, QCar telemetry logging, and optional QLabs side/rear camera recordings. The generated session can then be reviewed using two synchronized windows:

1. **Open Road map + replay timeline**
2. **Recorded video window**

A future QLabs replay window is intentionally **not exposed or implemented in the current user interface**.

---

## 1. What the program records

The current recording setup is:

| Source | How it is recorded | Default |
|---|---|---|
| QLabs/QCar front view | OBS records the QLabs application window | Required |
| QCar position and rotation | QLabs world transform | 20 Hz |
| Left CSI camera | Python/QLabs image capture | Enabled, 15 fps |
| Right CSI camera | Python/QLabs image capture | Enabled, 15 fps |
| Rear CSI camera | Python/QLabs image capture | Enabled, 15 fps |

The recorder does **not** drive the QCar. Drive it using your normal keyboard/controller/autonomous-driving program while the recorder is running.

When recording starts, the program automatically possesses the selected QCar's **front CSI camera**, so the main QLabs application window becomes the front-camera view that OBS records.

---

# Part A — First-time setup

You only need to perform most of this section once on a computer.

## 2. Requirements

You need:

- Windows with Quanser QLabs installed and working
- Python 3.10 or newer
- Quanser QLabs Python `qvl` package
- OBS Studio
- PySide6
- `obsws-python`
- OpenCV
- NumPy

The repository's pip dependencies are listed in `requirements.txt`:

```text
PySide6>=6.5
obsws-python>=1.7
opencv-python>=4.8
numpy>=1.24
```

> **Important:** use the same Python environment in which your existing Quanser/QLabs Python scripts already work. The `qvl` package is not installed by this repository's `requirements.txt`.

---

## 3. Install the Python dependencies

Open **Command Prompt** or **PowerShell**, change into the repository folder, and run:

```bat
cd C:\path\to\QLabs_Drive_Replay
python -m pip install -r requirements.txt
```

For example, if the repository is in Documents:

```bat
cd C:\Users\YourName\Documents\QLabs_Drive_Replay
python -m pip install -r requirements.txt
```

### Verify the Python environment

Run:

```bat
python -c "import PySide6, obsws_python, cv2, numpy; from qvl.qlabs import QuanserInteractiveLabs; print('Recorder dependencies OK')"
```

If it prints:

```text
Recorder dependencies OK
```

then the Python side is ready.

If `qvl` cannot be imported, you are probably running a different Python installation from the one used by your Quanser programs.

---

# Part B — Configure OBS

## 4. Create an OBS scene for the QLabs window

The main experiment video is the **QLabs application window showing the QCar front camera**.

Open OBS Studio and create a scene for QLabs:

1. Start **OBS Studio**.
2. In **Scenes**, create or select a scene such as `QLabs Recording`.
3. In **Sources**, click **+**.
4. Add a **Window Capture** source.
5. Select the QLabs application window.
6. Resize/crop it so the part of QLabs you want to preserve is visible in the OBS preview.
7. Make sure the source is visible and not hidden.

You do **not** need to manually switch QLabs to the front camera before each experiment. The recorder attempts to do this automatically when **START SESSION** is pressed.

### Recommended OBS recording format

For the easiest playback in the current replay application, use a common video format such as **MP4** with a codec supported by your Windows installation.

The actual OBS recording remains in the recording directory configured in OBS. When the session stops, the recorder asks OBS for the final output path and saves that path into `session.json`.

---

## 5. Enable OBS WebSocket

The one-button recorder controls OBS through OBS WebSocket.

In OBS:

1. Open **Tools**.
2. Open **WebSocket Server Settings**.
3. Enable the WebSocket server.
4. Leave the server port at **4455** unless you specifically need another port.
5. If authentication/password protection is enabled, note the password because you must enter the same password in the recorder.
6. Apply/close the settings.

The recorder defaults are:

```text
OBS host: localhost
OBS port: 4455
```

If OBS and the recorder run on the same computer, `localhost` is normally correct.

### Important OBS rule

**Do not manually start OBS recording before pressing START SESSION.**

The recorder intentionally refuses to begin if OBS is already recording because it would not know exactly where the experiment timeline begins in that video.

---

# Part C — Prepare QLabs

## 6. Load Open Road and prepare the QCar

Before starting the recorder:

1. Start **QLabs**.
2. Load the **Open Road** workspace.
3. Make sure the QCar2 you want to record already exists.
4. Confirm its actor number.

The recorder defaults to:

```text
QLabs host: localhost
QCar actor: 0
```

If your car is `QCar2 actor 1`, change the **QCar actor** field in the recorder to `1`.

The recorder **does not spawn the QCar**. It connects to an existing actor and reads its world transform.

---

# Part D — Record an experiment

## 7. Start the recorder program

The easiest method on Windows is to double-click:

```text
run_recorder.bat
```

You can also run it from a terminal:

```bat
python run_recorder.py
```

A window titled:

```text
QLabs Open Road Session Recorder
```

will appear.

---

## 8. Recorder window settings

### QLabs / telemetry

**QLabs host**

```text
localhost
```

Use this when QLabs is running on the same computer.

**QCar actor**

```text
0
```

Change this only when you want to record another existing QCar actor.

**Telemetry rate**

```text
20 Hz
```

20 Hz is the recommended default. Unlike the old map logger, this recorder does **not** discard points based on distance. It records the experiment timeline continuously, including periods when the vehicle is stopped.

---

### OBS WebSocket

Use the same values configured in OBS:

```text
OBS host:     localhost
OBS port:     4455
OBS password: your password, or blank if no password is configured
```

---

### Additional QCar camera recordings

The current additional-camera options are:

```text
Left CSI
Right CSI
Rear CSI
```

All three are enabled by default.

The front CSI camera is intentionally **not recorded a second time by Python**, because the front view is already shown in the main QLabs window and captured by OBS.

The default additional-camera frame rate is:

```text
15 fps
```

For an initial test you can leave all three selected. If your computer or QLabs has difficulty maintaining all streams, try reducing the camera FPS or temporarily disabling one or more additional cameras.

---

### Session label

The label is optional but strongly recommended for experiment organization.

Examples:

```text
participant_001_run_01
participant_001_baseline
participant_002_drowsy_run
```

The final folder name automatically includes the date/time followed by the label, for example:

```text
2026-09-10_091530_participant_001_run_01
```

---

### Recording folder

By default, recordings are saved under:

```text
QLabs_Drive_Replay\recordings\
```

You can change this in the recorder using **Browse...**.

---

## 9. Start everything with one button

Before pressing the button, check:

- QLabs is open.
- Open Road is loaded.
- The requested QCar actor exists.
- OBS is open.
- The OBS QLabs Window Capture scene is selected and visible.
- OBS WebSocket is enabled.
- OBS is **not already recording**.

Now press:

```text
START SESSION
```

The recorder performs the startup automatically in this order:

```text
Connect to QLabs
      ↓
Verify the selected QCar actor
      ↓
Possess the QCar FRONT CSI camera
      ↓
Connect/authenticate to OBS WebSocket
      ↓
Start OBS recording
      ↓
Wait until OBS reports recording is active
      ↓
Estimate OBS video t = 0
      ↓
Start OBS synchronization log
      ↓
Start optional Left / Right / Rear camera recorders
      ↓
Start continuous QCar telemetry
```

The large button changes to:

```text
STOP SESSION
```

Once initialization has completed successfully, the status area will show that the session is recording.

### What to watch during recording

The recorder displays:

- elapsed session time;
- number of telemetry samples;
- dropped telemetry count;
- OBS recording state;
- frame count for each selected CSI camera;
- duplicated-frame count when a camera request falls behind.

A typical status might look like:

```text
Elapsed: 00:04:12.350
Telemetry: 5,047 samples   Dropped: 0
OBS: RECORDING
Cameras: left: 3,785 frames (0 dup) | right: ... | rear: ...
```

At this point, drive the QCar normally using your experiment/control software.

---

## 10. Stop and finalize the experiment

At the end of the run, press:

```text
STOP SESSION
```

Do not immediately close the application. Wait until it reports that the session has completed.

The program stops/finalizes:

1. additional CSI camera recordings;
2. OBS synchronization logging;
3. OBS recording;
4. QLabs telemetry connection;
5. `session.json` metadata.

After successful completion, the status area shows the session folder and, when reported by OBS, the path to the main OBS video.

If you try to close the recorder while a recording is still active, the program asks whether it should stop and finalize the recording first.

---

# Part E — Understand the recorded files

## 11. Session folder structure

A typical recording looks like:

```text
recordings/
└── 2026-09-10_091530_participant_001_run_01/
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

The OBS front video normally remains in the OBS recording folder rather than being copied into the session directory.

Its absolute file path is stored in:

```text
session.json
```

### `location.csv`

This is the main synchronized QCar telemetry file.

Columns are:

```text
time_s
scheduled_time_s
x
y
z
roll_rad
pitch_rad
yaw_rad
valid
```

`time_s` is aligned to the experiment/OBS timebase.

`scheduled_time_s` is also saved so timing jitter can be diagnosed later.

---

### `obs_sync.csv`

This records correspondence between the Python session clock and OBS's reported recording duration:

```text
session_time_s
obs_duration_s
difference_s
output_timecode
output_bytes
```

The replay program uses this mapping rather than assuming that pressing the OBS start command and the first encoded video frame happened at exactly the same instant.

---

### `csi_*_frames.csv`

Each additional QLabs camera has a timestamp sidecar associated with its MP4 file.

The replay program uses those timestamps to keep Left/Right/Rear video synchronized with the same experiment timeline as the OBS front video.

---

# Part F — Replay a recorded experiment

## 12. Start the replay program

Double-click:

```text
run_replay.bat
```

or run:

```bat
python run_replay.py
```

The current reviewer opens **two windows**:

1. **Map + timeline**
2. **Video**

There is intentionally **no QLabs replay window or QLabs replay button in the current version**.

---

## 13. Load one or multiple sessions

The reviewer now supports **multiple recorded drives on the map at the same time**.
One recording is the **active replay**: its video and duration control the master
timeline. Every other loaded recording stays visible as a comparison trajectory,
and its QCar marker is placed at the same elapsed session time.

### Add one session

1. Click **Add Session...**.
2. Select the recording's **session folder**.

Example:

```text
recordings\2026-09-10_091530_participant_001_run_01
```

Select the folder itself — not `location.csv`. The program reads `session.json`
and `location.csv` automatically.

Repeat **Add Session...** for additional participants/runs. Each trajectory gets
a different persistent color. The most recently added session becomes the active
replay by default.

### Add an entire recordings folder

If many session folders are stored under the same parent directory, click
**Add Recordings Folder...** and select that parent directory. The reviewer
searches below it for `session.json` files and loads every valid recording that
is not already open.

### Choose the active replay

Use the **Active replay** drop-down at the top of the map window. Changing the
active recording:

- keeps every loaded trajectory visible;
- switches the Video window to that recording's available video sources;
- changes the timeline duration to that recording;
- preserves the current elapsed time when possible.

Use **Remove Active** to remove only the selected recording, or **Clear All** to
remove every comparison recording.

The comparison clock is **session-relative**. For example, at replay time
`00:05:00`, every loaded car with telemetry at five minutes is shown at its own
five-minute position. A shorter recording simply stops showing a current-position
marker after its duration; its complete trajectory remains visible.

If the active session's OBS and camera video paths are available, the separate
Video window automatically loads its synchronized video sources.

### Open several session videos at once

When two or more recordings are loaded, click **Open All Session Videos**. The
normal Video window remains attached to the **Active replay**. Every non-active
session that has an available video opens in its own additional Video window.
All of these windows follow the same elapsed replay time, Play/Pause state, and
playback rate. Each window can independently select its own Front/Left/Right/Rear
source. Click **Close Comparison Videos** to close the additional windows while
keeping the active video window available.

---

## 14. Map window controls

The map uses the six measured Open Road lane-center files when they are available:

```text
data\open_road_reference_upper_right_lane.json
data\open_road_reference_upper_middle_lane.json
data\open_road_reference_upper_left_lane.json
data\open_road_reference_lower_right_lane.json
data\open_road_reference_lower_middle_lane.json
data\open_road_reference_lower_left_lane.json
```

`data\open_road_reference.json` is retained as the legacy/fallback reference.

On the straight section, the reviewer uses the rounded road calibration:

```text
+12 m   upper outer road edge
+10 m   upper-right lane center
 +8 m   upper lane divider
 +6 m   upper-middle lane center
 +4 m   upper lane divider
 +2 m   upper-left lane center
+0.6 m  upper median-side pavement edge
-0.6 m  lower median-side pavement edge
 -2 m   lower-right lane center
 -4 m   lower lane divider
 -6 m   lower-middle lane center
 -8 m   lower lane divider
-10 m   lower-left lane center
-12 m   lower outer road edge
```

The six JSON recordings are treated as the authoritative lane centers. On curved
sections the painted lane dividers and boundaries are approximated smoothly from
those measured trajectories. The grey pavement is filled directly between the
derived outer and median-side boundaries, so its world width stays correct at
every zoom level. The solid road edges and dashed lane markers are intentionally
thicker than before so they remain easy to recognize under several trajectories.

Map colors are:

```text
White/gray solid  = outer and median-side road boundaries
White/gray dashed = lane dividers
Faint dotted      = six measured lane-center references
Colored lines     = loaded recorded QCar trajectories
Thicker color     = active replay trajectory
Colored markers   = each QCar position at the shared elapsed replay time
```

Each loaded session keeps a distinct color. The active trajectory is thicker,
and current car markers are labeled so overlapping participants/runs can still
be distinguished.

Controls:

| Action | Result |
|---|---|
| Left-click any recorded route | Choose the recording/pass, make it active if needed, and jump to that time |
| Mouse wheel | Zoom |
| Middle/right-button drag | Pan |
| Timeline drag | Seek replay time |
| Play/Pause | Start/stop replay |
| Space | Play/Pause |
| Left arrow | Move back 1 second |
| Right arrow | Move forward 1 second |

Playback rates available in the map/timeline window are:

```text
0.25×
0.5×
1×
2×
4×
```

### Locations visited more than once

If one or more loaded QCars passed a clicked location multiple times, the
reviewer does not guess. A menu groups candidates by recording and lists each
pass time, for example:

```text
participant_001
    Pass 1: 00:31:14.550
    Pass 2: 01:20:47.300

participant_002
    Pass 1: 00:30:58.100
```

Choosing a candidate automatically makes that recording active, switches the
Video window to it, and seeks both windows to that pass.

---

# Part G — Video window

## 15. Change camera while keeping the same time

The Video window discovers all available session sources, typically:

```text
QLabs Front (OBS)
Left CSI
Right CSI
Rear CSI
```

Use the **Camera** drop-down at the top of the Video window.

Changing from one camera to another preserves the same experiment time. For example, if the replay is at:

```text
00:42:17.250
```

switching from Front to Rear attempts to show the rear-camera frame corresponding to the same `00:42:17.250` session time.

The Video window also provides:

- **Move to Next Screen** — useful with two monitors;
- **Full Screen**;
- `Esc` to exit full-screen mode.

---

## 16. Manual video loading and fine synchronization

Normally the front OBS video is discovered from `session.json` automatically.

If the OBS video was moved, renamed, or is otherwise not found, the map window contains:

```text
Load Manual Video...
```

which lets you choose a replacement video file.

There is also a manual **Offset** control in seconds for fine adjustment.

Use this only when visual validation shows a small remaining synchronization error.

The recorded synchronization files remain the normal source of alignment.

### If you move the OBS video after recording

The simplest options are:

1. leave the video at the path stored in `session.json`; or
2. copy the video into the session folder using the **same filename**; or
3. use **Load Manual Video...** during replay.

The reviewer checks the stored path first and also checks the session folder for the same basename.

---

# Part H — Recommended validation before a long experiment

## 17. Perform a short synchronization test first

Before recording another one- or two-hour experiment, perform a **1–2 minute validation run**.

Suggested test:

1. Open QLabs Open Road.
2. Put QCar actor 0 on a recognizable section of road.
3. Open OBS with the QLabs capture scene ready.
4. Start `run_recorder.bat`.
5. Leave Left/Right/Rear enabled.
6. Press **START SESSION**.
7. Confirm QLabs switches to the QCar front view.
8. Confirm the GUI shows `OBS: RECORDING`.
9. Drive the QCar past a recognizable location.
10. Stop there briefly if useful for synchronization checking.
11. Continue driving for another short distance.
12. Press **STOP SESSION**.
13. Wait for `Session complete`.
14. Start `run_replay.bat`.
15. Load the new session folder.
16. Click the recognizable point on the blue route.
17. Verify the front video shows the correct location/time.
18. Switch Front → Left → Right → Rear.
19. Check that all available camera views correspond to the same moment.

Only after this passes should you rely on the setup for a long experimental session.

---

# Part I — Troubleshooting

## 18. `ModuleNotFoundError: qvl`

You are probably using the wrong Python environment.

Check:

```bat
python -c "from qvl.qlabs import QuanserInteractiveLabs; print('qvl OK')"
```

Use the Python installation/environment in which your existing Quanser scripts already run.

---

## 19. OBS connection/authentication fails

Check all of the following:

- OBS is running.
- OBS WebSocket server is enabled.
- The recorder port matches the OBS port.
- Default port is `4455`.
- The password in the recorder matches OBS.
- If OBS is on the same computer, use `localhost`.

---

## 20. Recorder says OBS is already recording

Stop the existing OBS recording manually, then press **START SESSION** again.

This restriction is deliberate so the program can establish the experiment video start time correctly.

---

## 21. `QCar2 actor ... does not exist in QLabs`

The actor number in the recorder does not match the QCar in the loaded workspace.

Check the actual QCar actor number and enter it in **QCar actor** before starting.

---

## 22. QLabs does not switch to the front camera

Check that:

- the correct QCar actor number is selected;
- the QCar exists;
- QLabs is responsive;
- another application is not continuously taking over the possessed QLabs camera.

A failure to possess the front camera causes the synchronized recording startup to fail rather than silently recording the wrong main view.

---

## 23. Left/Right/Rear camera shows `ERROR`

Try:

1. stopping the current test session;
2. reducing **Requested FPS** from `15` to `10`;
3. disabling one or more optional cameras;
4. verifying QLabs remains responsive.

The side/rear camera recordings are optional; telemetry + the OBS front recording are the primary experiment data.

---

## 24. Telemetry has dropped samples

Occasional dropped samples are counted rather than hidden.

If the count becomes large:

- reduce optional camera load;
- close unnecessary applications;
- verify QLabs performance;
- keep telemetry at 20 Hz unless you have a specific reason to increase it.

---

## 25. Replay says the OBS video is missing

The original OBS file may have been moved after the session was recorded.

Try one of these:

- restore the video to its original OBS output path;
- copy it into the session folder with its original filename;
- use **Load Manual Video...** in the replay window.

---

## 26. Video does not play in the Qt video window

The replay application uses Qt Multimedia, so playback depends on codecs available on the operating system.

If a video file cannot be played, use a common OBS recording codec/container that Windows/Qt supports well. MP4 is generally the simplest choice for this project.

---

## 27. Open Road map reference cannot be loaded

Make sure the `data` folder contains the six lane-reference files listed in
Section 14. If any of those files are missing, the reviewer falls back to:

```text
data\open_road_reference.json
```

The fallback keeps old copies of the repository usable, but it only shows the
legacy single-reference road visualization rather than the full six-lane map.

---

# Part J — Command-line options

## 28. Recorder output folder from the command line

You can choose the default recording root before the GUI opens:

```bat
python run_recorder.py --output D:\QLabsExperiments\recordings
```

You can still change it in the GUI afterward.

---

## 29. Open a replay session directly

Instead of selecting a session after startup:

```bat
python run_replay.py --session "C:\path\to\recordings\2026-09-10_091530_participant_001_run_01"
```

You can also provide a specific Open Road reference file:

```bat
python run_replay.py --session "C:\path\to\session" --reference "C:\path\to\open_road_reference.json"
```

---

# Part K — Synchronization design

## 30. Why the recorder starts OBS first

The project does not assume that sending an OBS `StartRecord` command means that the first encoded video frame exists at that exact instant.

The recorder instead:

1. requests OBS recording start;
2. waits until OBS reports that recording is active;
3. reads OBS's current output duration;
4. estimates the monotonic-clock instant corresponding to video `t = 0`;
5. uses that as the common session timeline;
6. periodically records OBS/session clock correlation into `obs_sync.csv`.

Telemetry and additional camera timestamps are saved on that same session timeline.

This is why manually starting OBS before **START SESSION** is not supported.

---

# Part L — Current project structure

```text
QLabs_Drive_Replay/
├── apps/
│   ├── location_recorder.py       # one-button recorder GUI
│   └── open_road_replay.py        # map/timeline replay GUI
├── core/
│   ├── oop_interfaces.py
│   ├── recorder_core.py           # synchronized session recorder
│   ├── replay_clock.py
│   ├── replay_controller.py
│   └── replay_core.py
├── integrations/
│   ├── location_sources.py        # QLabs QCar world-transform source
│   ├── recording_services.py      # OBS + additional CSI recording
│   ├── replay_sinks.py
│   └── qlabs_replay.py            # dormant/future; not exposed in UI
├── ui/
│   └── replay_video_window.py      # separate synchronized video window
├── data/
│   ├── open_road_reference.json
│   ├── open_road_reference_upper_right_lane.json
│   ├── open_road_reference_upper_middle_lane.json
│   ├── open_road_reference_upper_left_lane.json
│   ├── open_road_reference_lower_right_lane.json
│   ├── open_road_reference_lower_middle_lane.json
│   └── open_road_reference_lower_left_lane.json
├── recordings/
├── run_recorder.py
├── run_replay.py
├── run_recorder.bat
├── run_replay.bat
└── requirements.txt
```

---

# Quick start checklist

For normal experiment days, after the first-time setup is complete, the workflow is simply:

```text
1. Start QLabs
2. Load Open Road and the correct QCar actor
3. Start OBS and select the QLabs capture scene
4. Make sure OBS is NOT already recording
5. Run run_recorder.bat
6. Enter/check actor number and session label
7. Press START SESSION once
8. Wait for SESSION RECORDING / OBS: RECORDING
9. Perform the drive
10. Press STOP SESSION once
11. Wait for Session complete
12. Run run_replay.bat
13. Load the session folder
14. Review map + synchronized video
```

The goal is that **you never have to manually press OBS Record during an experiment**.


## Camera preflight and Replay-side start alignment

Before OBS begins, the recorder preflights every selected Python CSI camera. It must successfully receive and decode one QLabs image from each selected camera and create a small `mp4v` test file. If `get_image()` blocks, no image is returned, or the MP4 encoder is unavailable, **START SESSION is refused** instead of silently producing empty camera CSV files.

Left/Right/Rear are recorded by one sequential multi-camera worker using one dedicated QLabs connection. Each camera still receives its own `csi_<camera>.mp4` and `csi_<camera>_frames.csv`. The few milliseconds between sequential camera requests are preserved in the frame timestamps.

**Start alignment is no longer performed by the recorder.** The recorder always preserves the complete raw setup/waiting period. In the replay map window, click **Configure Starts…** to open the non-destructive alignment editor. For each loaded recording you can:

- run the movement detector and see the proposed raw start time plus its X/Y/Z position;
- preview the proposed start in the synchronized map/video replay;
- accept the detected start, type a manual raw start time, or choose the original recording start;
- optionally save that Replay choice in `session.json` under `replay.start_alignment`.

The default detector uses a **0.30 m/s rolling speed threshold**, **1.5 s sustained movement**, a **0.50 s speed averaging window**, and **0.30 m minimum displacement from the initial position**. These values are editable in the configuration window. Detection is only a suggestion; it is never applied until you choose **Apply to selected recording**.

The **Use configured starts** checkbox applies the selected offset for each driver so their chosen launch becomes replay `00:00.000`. Uncheck it at any time to inspect every raw recording from its original start. OBS/CSI MP4 files and telemetry CSVs are never edited or cut.
