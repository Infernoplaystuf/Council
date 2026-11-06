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
