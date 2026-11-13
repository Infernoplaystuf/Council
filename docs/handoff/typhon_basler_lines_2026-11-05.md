# A Basler's I/O cable as a trigger (5 November 2026, branch typhon-plugins)

Andy, 5 November 2026: "for all the Basler cameras they should have a
framework inside of them to accept triggers from their I/O cable; this
should be handled in the script to capture images." Answers: every Basler
model; the line is not known; the signal is a short pulse.

## What was there already

A Triggered: exact run from a line (Line1 …) arms the camera's own trigger
(`BaslerSettings.arm`: FrameStart / FrameBurstStart from the line, its edge,
delay and debounce) and counts the line's edges with Counter1. The camera
exposes only on a pulse, so it never misses one. That is unchanged.

## What is new

- `council_core/basler_lines.py`: a Basler's input lines (LineMode Input),
  their levels (LineStatus, or LineStatusAll's bit), and `LineWatcher`,
  which calls `fire(line)` once per edge from a thread of its own:
  - **counter** (preferred): one of the camera's counters counts edges on
    the line (Counter2 first, then 1, 3, 4; only one that is Off); read
    every 2 ms; no pulse is missed however short; its settings are put back
    at stop;
  - **line status** (fallback, e.g. an ace USB, whose counters count images
    only): the level read every 2 ms; a pulse shorter than a few ms can be
    missed, and the watcher's `note` says so (shown when a run starts).
  Only standard SFNC nodes, each used only where the camera has one. A
  LineSelector or CounterSelector is put back after every read, under the
  device's `trigger_lock`. A line's LineMode is never changed.
- `BaslerDevice.input_lines()`, `line_states()`, `watch_lines()`; the
  `hardware_trigger` facet's `lines()`, `states()`, `can_watch()`, `watch()`
  (API feature `facet.hardware_trigger.watch`).
- The triggering plugin: a **Triggered: snapshot** or **Triggered: record**
  run may take its trigger from a camera line. The camera runs live (nothing
  armed) and each pulse is a trigger on the run's books (`fire_from`), as a
  Capture now press is. Refused at Start when the line is not an input now,
  or the camera's lines cannot be watched (then only exact waits for it).
  Today's Typhon (classic) is unchanged: exact only from a line.
- The Triggering window, for a camera whose lines can be read: "Input lines
  now: Line1 low · Line3 HIGH", and **Find my trigger line** — watches every
  input for 10 s (`FIND_S`), says which line the pulses came on, and offers
  "Use LineN" (sets Trigger from). Not while capturing; "Stop looking" ends
  it early.
- The line is a set-up value (`capture.source`), so a preset keeps it.
- `tools/basler_line_test.py`: for Andy's PC. Lists the Baslers and their
  lines, finds the trigger line while you send pulses, and with `--runs`
  makes short exact, snapshot and record runs from it in a throw-away
  project. Everything goes under one new folder (report.txt and the runs).
  `--fake` checks the script with the test suite's fake camera.

## The cable into a CoaXPress frame grabber (6 November 2026)

Andy: the cable may go into the camera's I/O socket or into the frame
grabber, and users may switch. In the grabber, the card sends the pulse down
the CXP link and the camera sees it as `CxpTrigger0` — a trigger source, not
a line, with no level to read. `basler_lines.trigger_inputs` offers it
beside the lines where one of the camera's counters lists it among its
event sources (`countable`); the watcher gives such triggers a counter
first (a line left without one still has its level). So Find my trigger
line covers both places ("comes through the frame grabber, as
CxpTrigger0"), and a snapshot or record run can take its trigger from
`CxpTrigger0`. Where no counter counts it, only an exact run takes it and a
snapshot or record run is refused with the reason. The grabber must be set
in its own software to pass the trigger on; Typhon does not set the
grabber. Whether a real boost's counters list `CxpTrigger0` is not known
yet: `tools/basler_line_test.py` says so on the first line of its report.

## Tests

`tests/test_typhon_plugins_basler_lines.py` (fake Basler with counters, and
without: the level fallback; the finder; snapshot, record and exact runs
from Line1; refusals; the preset; the script with `--fake`) and the line
tests at the end of `tests/test_typhon_plugins_trigger_window.py`. Nothing
here has run on a real camera yet: `tools/basler_line_test.py` is that step.

## Open

- Real-camera numbers: which models fall back to line status, and the
  shortest pulse the fallback catches over USB and GigE.
- A Camera Link CC line is offered for exact runs as before; the watcher
  does not read it.

## pypylon not seeing a CXP camera that pylon Viewer sees (13 November 2026)

Andy's boA9344-30cm on a CXP grabber worked in pylon Viewer; the exported
Typhon listed no camera. pypylon's Windows wheel has no CXP transport layer
since 4.0.0: it reaches a CXP camera only through a GenTL producer (*.cti) on
GENICAM_GENTL64_PATH, which the pylon Software Suite installer sets for the
machine but an older console or environment never inherits.

- `council_core/pylon_env.py`: `prepare()` (called by
  `BaslerBackend.sdk()` before pypylon's first import) adds the machine's
  and user's GENICAM_GENTL64_PATH from the registry, else the folders under
  Program Files\Basler holding a .cti, to this process's path only
  (`COUNCIL_NO_GENTL_SEARCH=1` turns it off). `report()` / `words()`: pypylon
  version, transport layers, devices, the path. `full_lines()`: plus Python
  and environment, pylon installs, each path entry's .cti files, and each
  camera opened (sensor, input lines and levels, hardware trigger sources,
  CxpTrigger notes, what Typhon can watch). `python -m council_core.pylon_env`
  prints it.
- `cameras.discover`'s no-Basler note carries `words()`; `frame_camera`
  prints the camera list, notes, Connect and open failures to the Command
  Prompt as `[Typhon camera] …` (`COUNCIL_QUIET_CAMERA_CONSOLE=1` silences).
- The exported app's check.bat prints `full_lines()`; basler_line_test.py
  prints `words()` when no camera is found.
- Not verified on real hardware: whether a GenTL producer on the path shows
  in pypylon as its own transport layer, and what the Suite's CXP .cti is
  called. Tests: `tests/test_pylon_env.py` (fake pylon, temporary folders).

### check.bat sees the camera, Typhon does not (13 November 2026, later)

- The set-up wizard's "Scan Basler cameras" (`council_core/basler_scan.py`)
  imported pypylon without `pylon_env.prepare()`. Run before the app's camera
  list, it left pylon reading the GenTL path without the CXP producer for the
  rest of the process. It now calls `prepare()` first, and `prepare()` says in
  the Command Prompt if pypylon was already imported.
- Each camera scan now also prints, after its `[Typhon camera]` rows and
  notes: the Python that ran it (a double-clicked `run.bat` and `check.bat`
  typed in an activated environment can find different ones), the camera the
  app is set up for (a Prophesee set-up never searches for a Basler), and
  `GENICAM_GENTL64_PATH`.
- The cause on Andy's lab PC: the exported Typhon carried the Council PC's
  `camera_setup.json` (set up for Prophesee), so it searched only for an
  EVK4 and the simulated cameras. An export now leaves `camera_setup.json`
  behind (it is per machine), and an app set up for a camera kind it finds
  none of searches every kind, with a note to run Camera setup.
