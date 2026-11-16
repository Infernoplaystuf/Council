# Typhon: several cameras in one window (16 November 2026)

Andy asked for this on 16 November 2026: "One window should handle multiple
cameras say two baslers or one and an EVK4 or other cameras we haven't
discussed yet. Keep in mind that the cameras aren't always recording they may
be set to trigger. For instance the Basler may only take images when
triggered while the EVK4 is constantly recording."

He also asked for a dropdown: "camera 1, camera 2, camera #, Mulitcam". After
seeing it, he asked to move the dropdown "above the image slider" so that it
no longer covers the stream. He approved that look.

This builds the core of stage M1 of `docs/typhon_multicamera_design.md` (MODE
1). It works in both Typhons: today's Typhon and "Typhon (plugins)".

## What the user sees

- **Connect** with a camera already open does not refuse any more. The camera
  picked in the list opens into a slot of its own (cam2, cam3, cam4; at most
  `camera_rig.MAX_SLOTS` = 4) and becomes the shown camera. The others keep
  streaming.
  - Picking a camera that is already open is refused ("already open as cam1").
  - Connect is also refused while a run is recording.
- **The dropdown** under the picture, right-aligned in the gap above the
  slider, lists "Camera 1 · <label>", "Camera 2 · <label>", ..., "Multicam".
  It appears only with two or more cameras.
  - **Camera N** shows that camera, which becomes the active one. The
    settings tabs, the boxes beside Start, the capture set-up, the preset
    box, Camera settings… and the slider all act on it.
  - **Multicam** shows every stream: side by side for two cameras, a 2 x 2
    grid for three or four. The active camera is outlined, and clicking
    another picture makes it the active one.
- **The status line** starts with the shown camera's name ("cam1: ...").
- **Each camera keeps its own capture set-up.** Continuous, Triggered: exact,
  Triggered: snapshot and Triggered: record belong to each camera, so a
  Basler can wait on its line while an EVK4 records all the time.
- **One Start records every connected camera** into
  `<folder>/<run>/<camera>/`. Each camera folder is laid out exactly as a
  one-camera run, so the slider, the raw view and the classifiers work on it
  unchanged.
  - `<run>_rig.json` (written once at Start) lists each camera, its folder,
    its capture set-up, its share of the save budget and the start order.
  - `<run>_rig_end.json` (written once at Stop) holds each camera's counts.
  - The order follows design 2.3:
    1. Start: event cameras (not `restartable`) first, then frame cameras
       `RIG_GAP_SECONDS` (0.1 s) later.
    2. Stop: frame cameras first, then event cameras.
  - If one camera refuses, the cameras already started are stopped again and
    the message names the camera that refused and why.
  - **The boxes beside Start** apply to the shown camera only. The other
    cameras keep their own settings.
  - **The slider** is pointed at the shown camera's folder of the run, and
    follows the dropdown.
  - **A later Start** given that folder (the same box) saves beside the run,
    not inside it.
- **One camera saves exactly as before.** No run folder and no rig records.
- **Capture now (F9)** fires every recording camera whose trigger Typhon
  fires (Software or Timer, or a plugin project's triggering plugin). A
  camera on its own input line, or one recording continuously, is not
  asked.
- **Disconnect** closes the shown camera. The next camera is shown, and its
  slot goes. `shutdown` closes every camera.
- **The save budget** (`capture.WRITE_BUDGET_BYTES`) is shared between the
  cameras by frame size, each getting at least 256 MB (`Recorder.budget_bytes`).
- **The save-speed check** times the folder once, against what every camera
  sends together.

## Where it is

- `frame_camera.py`, section "Several cameras in one window":
  - `_another_slot`, `_back_to`, `_open_in_slot`: Connect.
  - `_show_another`, `_shown_now`, `_select_camera`, `_tile_picked`: which
    camera is shown.
  - `_on_slot`: runs the one-camera code on another slot, with its
    announcements kept from the views.
  - `_start_rig`, `_stop_rig`, `_rig_firing`: one Start, one Stop and
    Capture now.
  - `_claim_rig_run`, `_shared_budgets`, `_rig_speed_check`,
    `_write_rig_record`, `_write_rig_end`.
  - `_tend_others`, `_feed_tiles`: each timer tick.
  - `start`, `stop` and `capture_now` keep their docstrings and hand over to
    `_start_one`, `_stop_one` and `_capture_now_one` (the one-camera code,
    unchanged). No new public function: a public one would show in the
    Designer's links and in typhon_recreate's shortlist.
  - Two new fields, both about the window: `_Live.rig_run` and
    `_Live.tiles`.
- `council_qt/widgets/camera_tiles.py`: the dropdown and the Multicam grid
  (`CameraTiles`, `grid_shape`, `small_image`). It sits in the canvas's
  parent so that it never covers the picture.
- `council_core/capture.py`: `Recorder.budget_bytes`, read by `record_to`.
- Tests: `tests/test_typhon_multicamera.py`, run on the simulated cameras and
  added to the branch gate.
- `docs/typhon_camera_plugins.md`, section 7: what this means for a camera
  plugin. The plugin API is unchanged.

## Not done yet (from design M1-M2)

- A per-camera Start and Stop.
- Rig presets.
- Pop-out windows per camera.
- The trigger cable pairing (M2).
- Several busy EVK4s in separate programs.
- A per-camera Record box: every connected camera records.

The plugin host is told of each camera's run apart, the same run name for
each. A rig-wide run-hook plugin hears it once per camera.

Nothing has run on real hardware yet. Not yet tried with two Baslers, or with
a Basler and an EVK4.
