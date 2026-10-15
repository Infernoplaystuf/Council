"""
council_core.triggered — capturing only when something happens, and saving
smaller pictures: a frame camera's CAPTURE SET-UP (Continuous, or Triggered
exact / snapshot; where the trigger comes from; how many frames each one
takes; Downsample), and what one triggered run fired, got and missed.

WHY
A run used to save every frame the camera sent, from Start to Stop. To
capture "at specific times" — the signal not chosen yet — a run can now save
only the frames a trigger asks for, from whichever source Typhon can offer:

  Software   the "Capture now" button, or F9 (Typhon fires the camera)
  Timer      every N seconds (Typhon fires the camera on a schedule)
  Line1 …    any hardware input the camera lists as a trigger source (an
             input line, a CoaXPress trigger CxpTrigger0 …) — the camera
             itself waits for the signal

TWO WAYS TO CAPTURE ON A TRIGGER
  Triggered: exact     the camera exposes ONLY when the trigger arrives
                       (its own FrameStart trigger, armed by Start and put
                       back by Stop): precise timing; between triggers the
                       picture shows the last capture.
  Triggered: snapshot  the live view keeps running; the next N frames that
                       reach Typhon AFTER the trigger are saved. Up to one
                       frame late: the first may have begun its exposure up
                       to one frame period before the trigger.

NOTHING IS EVER SILENT. A trigger fired while the camera was still busy
with the last frame is IGNORED by the camera with no error and no gap in
its frame numbers (measured on pylon's emulator: 20 triggers, 10 frames,
nothing counted anywhere). So Typhon asks the camera whether it is ready
before every trigger it fires (FrameTriggerWait), and one it could not fire
is counted "missed: camera busy". A trigger whose frame never arrives is
"no frame"; a snapshot trigger that came before the last one had its frames
is "cut short". Each trigger is a row of <run>_capture_triggers.csv, each
saved frame says its trigger in the frames CSV (capture.INDEX_COLUMNS), and
the run's end record and camera record carry the counts.

ONE SET-UP PER RUN. The set-up is chosen before Start and fixed while
capturing (Setting.per_run): a run's frames all mean the same thing.

THE SET-UP LIVES ON THE OPEN CAMERA (device.capture), as an event camera's
display settings do: described by the camera's settings provider as the
"Capture" group, so presets, the settings tabs and a run's camera record
carry it with no plumbing of their own. An event camera has none: its
stream is untouched by all of this.

NO TOOLKIT, NO SDK. The device does the camera's part (arming, firing,
asking whether it is ready); this module keeps the books.
"""
from __future__ import annotations

import csv
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

# ======================================================================
# The set-up
# ======================================================================
MODE_CONTINUOUS = "Continuous"
MODE_EXACT = "Triggered: exact"
MODE_SNAPSHOT = "Triggered: snapshot"
MODES: Tuple[str, ...] = (MODE_CONTINUOUS, MODE_EXACT, MODE_SNAPSHOT)

#: The sources Typhon fires itself. Every other source is the camera's own
#: (a line, a CoaXPress trigger), as the camera lists it.
SOFTWARE = "Software"
TIMER = "Timer"
TYPHON_SOURCES: Tuple[str, ...] = (SOFTWARE, TIMER)

#: "Downsample": the factor of each choice.
DOWNSAMPLES: Tuple[str, ...] = ("Off", "2x", "3x", "4x")

#: The settings group (and its tab) the set-up is described in.
GROUP = "Capture"

MODE_KEY = "capture.mode"
SOURCE_KEY = "capture.source"
PER_KEY = "capture.per_trigger"
EVERY_KEY = "capture.every_s"
EDGE_KEY = "capture.edge"
DELAY_KEY = "capture.delay_us"
DEBOUNCE_KEY = "capture.debounce_us"
DOWNSAMPLE_KEY = "save.downsample"

#: Every key of the set-up, in the order a set writes them.
KEYS: Tuple[str, ...] = (MODE_KEY, SOURCE_KEY, PER_KEY, EVERY_KEY, EDGE_KEY,
                         DELAY_KEY, DEBOUNCE_KEY, DOWNSAMPLE_KEY)

#: Frames one trigger may ask for (a short burst).
MAX_PER_TRIGGER = 1000

#: The timer's interval, seconds: 20 a second at most, one a day at least.
EVERY_RANGE: Tuple[float, float] = (0.05, 86400.0)

#: The camera's own trigger sources offered as hardware inputs: its input
#: lines and the CoaXPress triggers a frame grabber forwards. A camera's
#: internal signals (Counter*, Timer*, SoftwareSignal*, PeriodicSignal*,
#: Action*) are not inputs a signal is wired to, so they are not offered.
HARDWARE_PREFIXES: Tuple[str, ...] = ("Line", "CxpTrigger", "CC")


def is_capture_key(key: Any) -> bool:
    return str(key) in KEYS


def factor_of(text: Any) -> int:
    """"2x" -> 2, "Off" -> 1; a number as it is (1..4)."""
    word = str(text if text is not None else "").strip().lower()
    if word in ("", "off", "1", "1x", "none"):
        return 1
    if word.endswith("x"):
        word = word[:-1]
    try:
        factor = int(float(word))
    except ValueError:
        raise ValueError(f"Downsample is one of {', '.join(DOWNSAMPLES)} — "
                         f"not {text!r}") from None
    if not 1 <= factor <= 4:
        raise ValueError(f"Downsample is one of {', '.join(DOWNSAMPLES)} — "
                         f"not {text!r}")
    return factor


def downsample_text(factor: int) -> str:
    return "Off" if int(factor) <= 1 else f"{int(factor)}x"


def hardware_sources(listed: Any) -> Tuple[str, ...]:
    """The camera's TriggerSource entries that are inputs a signal can be
    wired to (HARDWARE_PREFIXES), in the camera's order."""
    out = []
    for entry in listed or ():
        name = str(entry)
        if name.startswith(HARDWARE_PREFIXES) and name not in out:
            out.append(name)
    return tuple(out)


@dataclass
class CaptureSetup:
    """How the next run captures. The defaults are what every run did
    before: every frame, full size."""
    mode: str = MODE_CONTINUOUS
    source: str = SOFTWARE
    per_trigger: int = 1
    every_s: float = 1.0
    #: The edge, delay and debounce Typhon arms a HARDWARE source with —
    #: "" / None: as the camera has them (what its rows show).
    edge: str = ""
    delay_us: Optional[float] = None
    debounce_us: Optional[float] = None
    #: Downsample: 1 (Off), 2, 3 or 4.
    downsample: int = 1

    @property
    def triggered(self) -> bool:
        return self.mode in (MODE_EXACT, MODE_SNAPSHOT)

    @property
    def exact(self) -> bool:
        return self.mode == MODE_EXACT

    @property
    def snapshot(self) -> bool:
        return self.mode == MODE_SNAPSHOT

    @property
    def typhon_fires(self) -> bool:
        """Typhon fires the trigger (Software, Timer); else the camera's
        own input does."""
        return self.source in TYPHON_SOURCES

    def short(self) -> str:
        """"exact, Line1", "snapshot, Timer every 2 s", "continuous"."""
        if not self.triggered:
            return "continuous"
        how = "exact" if self.exact else "snapshot"
        return f"{how}, {self.source_words()}"

    def source_words(self) -> str:
        """"Line1", "Timer every 2 s", "Software, 3 frames per trigger"."""
        source = self.source
        if source == TIMER:
            source = f"Timer every {show_seconds(self.every_s)}"
        per = (f", {self.per_trigger} frames per trigger"
               if self.per_trigger > 1 else "")
        return f"{source}{per}"

    def as_record(self) -> Dict[str, Any]:
        """The set-up as a run's records say it."""
        return {"mode": self.mode, "source": self.source,
                "per_trigger": int(self.per_trigger),
                "every_s": float(self.every_s),
                "edge": self.edge or None, "delay_us": self.delay_us,
                "debounce_us": self.debounce_us,
                "downsample": int(self.downsample)}


def show_seconds(seconds: float) -> str:
    return f"{float(seconds):g} s"


@dataclass(frozen=True)
class CaptureOptions:
    """What a camera offers the set-up: its hardware trigger sources, and
    the edge, delay and debounce it has nodes for (each None/() when it has
    none) with their values now — what a row shows for "as the camera has
    it" — and whether Downsample can change while the stream runs (camera
    binning needs it stopped) and how it would be made."""
    hardware: Tuple[str, ...] = ()
    edges: Tuple[str, ...] = ()
    edge_now: str = ""
    delay: Optional[Tuple[float, float]] = None
    delay_now: float = 0.0
    debounce_label: str = "Debounce"
    debounce: Optional[Tuple[float, float]] = None
    debounce_now: float = 0.0
    downsample_live: bool = True
    downsample_help: str = ""


MODE_HELP = (
    "Continuous saves every frame from Start to Stop. Triggered: exact arms "
    "the camera so it exposes ONLY when the trigger arrives (precise; the "
    "picture shows the last capture between triggers). Triggered: snapshot "
    "keeps the live view and saves the next frames that arrive after each "
    "trigger (up to one frame late). Fixed while capturing.")
SOURCE_HELP = (
    "Software: the Capture now button or F9. Timer: every so many seconds. "
    "Line1 …: a hardware input the camera lists (exact only) — its edge, "
    "delay and debounce are the rows below. Fixed while capturing.")
PER_HELP = ("Frames each trigger saves: 1, or more for a short burst. A "
            "hardware trigger with more than one needs a camera with burst "
            "triggers (AcquisitionBurstFrameCount).")
EVERY_HELP = "The timer's interval, in seconds (source Timer only)."
EDGE_HELP = ("Which edge of the hardware signal triggers (TriggerActivation "
             "of the trigger Start arms).")
DELAY_HELP = ("How long the camera waits after the hardware trigger before "
              "it exposes (TriggerDelay).")
DEBOUNCE_HELP = ("Ignore pulses on the input line shorter than this (the "
                 "camera's line debouncer or input filter).")


def capture_settings(setup: CaptureSetup,
                     options: CaptureOptions) -> List[Any]:
    """The set-up as camera_settings.Setting rows, group GROUP: every one
    is chosen before a run and fixed while capturing (per_run)."""
    from .camera_settings import CHOICE, FLOAT, INT, Setting

    sources = TYPHON_SOURCES + tuple(options.hardware)
    if setup.source not in sources:
        # A preset from another camera: shown as it is, and Start says why
        # it cannot be used here.
        sources = sources + (setup.source,)
    out = [
        Setting(MODE_KEY, "Capture", GROUP, CHOICE, setup.mode,
                choices=MODES, help=MODE_HELP, per_run=True),
        Setting(SOURCE_KEY, "Trigger from", GROUP, CHOICE, setup.source,
                choices=sources, help=SOURCE_HELP, per_run=True),
        Setting(PER_KEY, "Frames per trigger", GROUP, INT,
                int(setup.per_trigger), minimum=1, maximum=MAX_PER_TRIGGER,
                step=1, help=PER_HELP, per_run=True),
        Setting(EVERY_KEY, "Timer every", GROUP, FLOAT, float(setup.every_s),
                minimum=EVERY_RANGE[0], maximum=EVERY_RANGE[1], unit="s",
                help=EVERY_HELP, per_run=True),
        Setting(DOWNSAMPLE_KEY, "Downsample", GROUP, CHOICE,
                downsample_text(setup.downsample), choices=DOWNSAMPLES,
                live=options.downsample_live, per_run=True,
                help=options.downsample_help or DOWNSAMPLE_HELP),
    ]
    if options.edges:
        edges = tuple(options.edges)
        edge = setup.edge or options.edge_now or edges[0]
        if edge not in edges:
            edges = edges + (edge,)
        out.append(Setting(EDGE_KEY, "Trigger edge", GROUP, CHOICE, edge,
                           choices=edges, help=EDGE_HELP, per_run=True))
    if options.delay is not None:
        low, high = options.delay
        out.append(Setting(DELAY_KEY, "Trigger delay", GROUP, FLOAT,
                           float(options.delay_now if setup.delay_us is None
                                 else setup.delay_us),
                           minimum=low, maximum=high, unit="µs",
                           help=DELAY_HELP, per_run=True))
    if options.debounce is not None:
        low, high = options.debounce
        out.append(Setting(DEBOUNCE_KEY, options.debounce_label, GROUP,
                           FLOAT, float(options.debounce_now
                                        if setup.debounce_us is None
                                        else setup.debounce_us),
                           minimum=low, maximum=high, unit="µs",
                           help=DEBOUNCE_HELP, per_run=True))
    return out


DOWNSAMPLE_HELP = (
    "Saves smaller pictures: each 2 x 2, 3 x 3 or 4 x 4 block of pixels "
    "becomes one, AVERAGED — by the camera where it can (binning, Average), "
    "else by Typhon before saving. Off (the default) never changes the "
    "data. Kept in presets; fixed while capturing.")


def read_capture(setup: CaptureSetup, options: CaptureOptions,
                 key: str) -> Any:
    """One key's value as its row shows it."""
    for setting in capture_settings(setup, options):
        if setting.key == key:
            return setting.value
    raise KeyError(key)


def write_capture(setup: CaptureSetup, key: str, value: Any) -> None:
    """One key, already coerced to what its row takes (Provider.set).
    Downsample's camera side (binning) is the device's to do."""
    if key == MODE_KEY:
        setup.mode = str(value)
    elif key == SOURCE_KEY:
        setup.source = str(value)
    elif key == PER_KEY:
        setup.per_trigger = max(1, min(MAX_PER_TRIGGER, int(value)))
    elif key == EVERY_KEY:
        setup.every_s = max(EVERY_RANGE[0], min(EVERY_RANGE[1],
                                                float(value)))
    elif key == EDGE_KEY:
        setup.edge = str(value)
    elif key == DELAY_KEY:
        setup.delay_us = float(value)
    elif key == DEBOUNCE_KEY:
        setup.debounce_us = float(value)
    elif key == DOWNSAMPLE_KEY:
        setup.downsample = factor_of(value)
    else:
        raise KeyError(key)


# ======================================================================
# One triggered run
# ======================================================================
#: "<run>_capture_triggers.csv": one row per trigger, whatever became of it.
#: Not "<run>_triggers.csv", which the multi-camera design keeps for an
#: event camera's trigger-input edges (docs/typhon_multicamera_design.md).
TRIGGERS_SUFFIX = "_capture_triggers.csv"
TRIGGER_COLUMNS: Tuple[str, ...] = (
    "trigger", "source", "fired_at", "first_frame_at", "frames_asked",
    "frames_arrived", "failed_grabs", "outcome")

#: What became of a trigger (the triggers CSV's `outcome`).
OK = "ok"
MISSED_BUSY = "missed: camera busy"
NO_FRAME = "no frame"
FAILED_GRAB = "failed grab"
CUT_SHORT = "cut short: the next trigger came first"
NOT_FIRED = "not fired"
#: Its frames came but were NOT saved — the save queue was full (storage
#: too slow) or the recording had stopped on a failed write. A miss: the
#: run does not hold what the trigger asked for.
NOT_SAVED = "not saved"
#: A burst Stop ended: the frames fired before Stop, no more after it.
STOPPED = "cut short by Stop"

#: How long the first trigger of a run waits for the camera to be ready
#: (measured on the emulator: ready 14-15 ms after StartGrabbing; a trigger
#: fired before that is lost with no error, 0 frames of 10).
FIRST_READY_S = 2.0
#: How long Capture now waits for a busy camera: a person cannot tell 1 s,
#: and a press is not a schedule. A timer tick does not wait: late is not
#: on time, so a busy camera at a tick is a missed tick.
PRESS_READY_S = 1.0
#: How long after a fired trigger its frame may take before it is "no
#: frame": the exposure, plus this.
DEADLINE_S = 1.0
#: The worker's longest sleep: deadlines are checked and rows written this
#: often.
WAKE_S = 0.05
#: How long Stop waits for the frames still being written to say whether
#: they were saved (confirm_saves) before it closes the books.
SAVE_WAIT_S = 1.0


@dataclass
class _Trigger:
    number: int
    source: str
    asked: int
    fired: int = 0
    got: int = 0
    failed: int = 0
    firing: bool = False
    fired_mono: Optional[float] = None
    fired_wall: Optional[float] = None
    first_wall: Optional[float] = None
    deadline: Optional[float] = None
    outcome: str = ""
    note: str = ""
    #: Frames tagged for it whose save is not yet known (confirm_saves),
    #: and those the session could NOT save, with why.
    waiting: int = 0
    unsaved: int = 0
    unsaved_why: str = ""
    #: Stop came in the middle of its burst: no more were fired.
    stopped: bool = False
    #: Past its deadline with no frame, held a while longer for a LATE
    #: frame (TriggerRun._expire) — until this moment (monotonic).
    held_until: Optional[float] = None

    @property
    def done(self) -> bool:
        return bool(self.outcome)

    @property
    def arrived(self) -> int:
        return self.got + self.failed


def _stamp(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.6f}"


class TriggerRun:
    """The books of one triggered run, and the thread that fires Typhon's
    own triggers.

    `device` fires (fire_trigger()) and says whether it is ready
    (trigger_ready(timeout) -> True, False, or None for "cannot tell") —
    used in exact mode with a Software or Timer source only. `tag` is
    called on the GRAB THREAD for every frame the camera sends while
    recording (capture.CaptureSession): it answers the frame's trigger
    columns, or None for a snapshot frame no trigger asked for. Rows of the
    triggers CSV are written by the worker, never on the grab thread."""

    def __init__(self, setup: CaptureSetup, device: Any = None,
                 path: Optional[Path] = None, exposure_s: float = 0.0,
                 frame_period_s: float = 0.0,
                 clock: Callable[[], float] = time.perf_counter,
                 wall: Optional[Callable[[], float]] = None):
        self.setup = replace(setup)
        self.device = device
        self.path = Path(path) if path is not None else None
        self.exposure_s = max(0.0, float(exposure_s or 0.0))
        #: The camera's frame period when it free-runs (snapshot): a
        #: trigger's frames are waited for this long each, and more.
        self.frame_period_s = max(0.0, float(frame_period_s or 0.0))
        # ONE FINE CLOCK. time.time() and time.monotonic() step every
        # 15.6 ms on Windows (Python 3.11): a trigger and its frame read the
        # same moment (measured). The times written are the wall clock at
        # the start plus the performance counter since.
        self._clock = clock
        if wall is None:
            wall0, perf0 = time.time(), clock()
            wall = lambda: wall0 + (clock() - perf0)       # noqa: E731
        self._wall = wall
        self.exact = self.setup.exact
        self.typhon_fires = self.setup.typhon_fires
        self.per = max(1, int(self.setup.per_trigger))
        self.timer = self.setup.source == TIMER
        self.period = float(self.setup.every_s)
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._requests = 0
        self._stopping = False
        self._thread: Optional[threading.Thread] = None
        self._number = 0
        self._pending: Deque[_Trigger] = deque()
        self._current: Optional[_Trigger] = None      # snapshot
        self._hw_seen = 0                              # exact, hardware
        self._hw_current: Optional[_Trigger] = None
        #: Exact, Typhon's: triggers past their deadline with no frame,
        #: oldest first, held for a late frame (_expire).
        self._held: Deque[_Trigger] = deque()
        #: The camera's frame number (Frame.meta "frame_id") of the last
        #: frame or failed grab, where it gives one: a frame whose number
        #: skips one says a frame was lost on the way, not late.
        self._last_fid: Optional[int] = None
        #: The session says whether each tagged frame was saved (saved()):
        #: a trigger is settled only then (confirm_saves).
        self._confirm = False
        self._awaiting: Dict[int, _Trigger] = {}
        self._ever_ready = False
        self._t0: Optional[float] = None
        self._tick = 0
        self._rows: List[Tuple[Any, ...]] = []
        self._file: Any = None
        self._writer: Any = None
        self.file_error = ""
        self.counts: Dict[str, int] = {
            "triggers": 0, "ok": 0, "missed_busy": 0, "no_frame": 0,
            "failed_grab": 0, "cut_short": 0, "not_fired": 0,
            "not_saved": 0, "stopped": 0, "frames": 0, "untriggered": 0}
        #: When the last trigger was fired or seen (monotonic), or None.
        self.last_at: Optional[float] = None
        #: Hardware edges the camera counted (Stop), or None.
        self.edges: Optional[int] = None
        self.edges_note = ""
        #: Between which moments the camera counted them, in words.
        self.edges_window = ""

    # -- the triggers CSV ------------------------------------------------
    def open(self) -> Optional[Path]:
        """Create the triggers CSV — EXCLUSIVELY (never over a file) — with
        its header. Raises OSError when it cannot be made."""
        if self.path is None:
            return None
        handle = open(self.path, "x", newline="", encoding="utf-8")
        self._file = handle
        self._writer = csv.writer(handle)
        self._writer.writerow(TRIGGER_COLUMNS)
        handle.flush()
        return self.path

    def _write_rows(self) -> None:
        with self._lock:
            rows, self._rows = self._rows, []
        if not rows or self._writer is None:
            return
        try:
            self._writer.writerows(rows)
            self._file.flush()
        except (OSError, ValueError) as exc:
            # Said in the run's records, never on the grab thread.
            self.file_error = self.file_error or f"{type(exc).__name__}: {exc}"

    # -- running ---------------------------------------------------------
    def start(self) -> None:
        """Begin: the timer's first tick is now (exact: once the camera
        says it is ready)."""
        self._t0 = self._clock()
        self._tick = 0
        self._thread = threading.Thread(target=self._work,
                                        name="capture-trigger", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def capture_now(self) -> str:
        """A Capture now press (or F9): queued for the worker, which fires
        it at once. Returns what to say."""
        if not self.typhon_fires:
            return (f"the camera waits for {self.setup.source} — Capture now "
                    f"fires a Software or Timer run only")
        with self._cond:
            if self._stopping:
                return "the run is stopping"
            self._requests += 1
            self._cond.notify()
        return "trigger sent" if self.exact else "saving the next frame" + (
            "s" if self.per > 1 else "")

    def halt(self, timeout: float = 5.0) -> None:
        """Fire nothing more (the worker ends); triggers already fired keep
        waiting for their frames — Stop calls this BEFORE it ends the
        stream, so a trigger fired a moment before Stop still gets its
        frame (wait_pending).

        A BURST IN PROGRESS ENDS HERE: the worker looks for Stop before
        every frame of a burst, and waits for a busy camera in short steps
        (_wait_ready), so this returns within about one of them. It looked
        only between triggers: Stop in a 300-frame burst blocked the window
        for 6.5 s while ~176 frames were exposed and saved after it (review,
        measured on the emulator)."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _stop_asked(self) -> bool:
        with self._cond:
            return self._stopping

    def wait_pending(self, timeout: float) -> bool:
        """Wait up to `timeout` for every fired trigger's frames; True when
        none is waiting. Frames that came and are still being SAVED are not
        waited for here — stop() gives them their moment (SAVE_WAIT_S)."""
        deadline = self._clock() + max(0.0, float(timeout))
        while True:
            with self._lock:
                current = self._current
                waiting = any(t.firing or t.arrived < t.fired
                              for t in self._pending) or (
                    current is not None and not current.done
                    and current.arrived < current.asked)
            if not waiting:
                return True
            if self._clock() >= deadline:
                return False
            time.sleep(0.005)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop firing, then settle every trigger still waiting for frames:
        the run has ended, so what has not arrived will not."""
        self.halt(timeout)
        # The frames still being written are given a moment to land, so
        # their triggers say what became of them.
        deadline = self._clock() + SAVE_WAIT_S
        while self._confirm and self._clock() < deadline:
            with self._lock:
                if not self._awaiting:
                    break
            time.sleep(0.005)
        with self._lock:
            # A save not told by now (a slow disk still writing at Stop; or
            # no session telling, the books alone): handed on to be saved,
            # and said so.
            for trig in list(self._awaiting.values()):
                if self._confirm and not trig.note:
                    trig.note = "its frames were still being saved at Stop"
                trig.waiting = 0
                self._maybe_settle(trig)
            self._awaiting.clear()
            for trig in list(self._held):
                self._settle(trig, NO_FRAME, note="the run stopped")
            self._held.clear()
            for trig in list(self._pending):
                self._settle(trig, NO_FRAME if trig.arrived == 0
                             else self._partial(trig), note="the run stopped")
            if self._current is not None and not self._current.done:
                self._settle(self._current, NO_FRAME
                             if self._current.arrived == 0
                             else self._partial(self._current),
                             note="the run stopped")
            if self._hw_current is not None and not self._hw_current.done:
                self._settle(self._hw_current, self._partial(
                    self._hw_current), note="the run stopped mid-burst")
        self._write_rows()
        self.close()

    def close(self) -> None:
        handle, self._file, self._writer = self._file, None, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _work(self) -> None:
        while True:
            with self._cond:
                if self._stopping and not self._requests:
                    break
                wait = WAKE_S
                if self.timer:
                    wait = min(wait, max(0.0, self._due() - self._clock()))
                if not self._requests and not self._stopping and wait > 0:
                    self._cond.wait(wait)
                # A press made before Stop is FIRED, not dropped: it was
                # answered "trigger sent" (and Stop waits for its frame).
                presses, self._requests = self._requests, 0
                stopping = self._stopping
            self._expire()
            for _ in range(presses):
                self._trigger(SOFTWARE)
            if stopping:
                break
            if self.timer and self._clock() >= self._due():
                self._timer_tick()
            self._write_rows()
        self._write_rows()

    def _due(self) -> float:
        return (self._t0 or 0.0) + self._tick * self.period

    def _timer_tick(self) -> None:
        """Fire the tick that is due; ticks that passed while the worker
        was busy (a camera slow to be ready) are each counted missed."""
        now = self._clock()
        late = int((now - self._due()) // self.period) if self.period else 0
        for _ in range(max(0, late)):
            number = self._next_number()
            trig = _Trigger(number, TIMER, self.per)
            with self._lock:
                self._settle(trig, MISSED_BUSY,
                             note="the timer's tick came while the last was "
                                  "still being fired")
            self._tick += 1
        self._tick += 1
        self._trigger(TIMER)

    def _next_number(self) -> int:
        with self._lock:
            self._number += 1
            self.counts["triggers"] += 1
            return self._number

    # -- firing ---------------------------------------------------------
    def _ready(self, timeout: float) -> Optional[bool]:
        ask = getattr(self.device, "trigger_ready", None)
        if not callable(ask):
            return None
        try:
            return ask(timeout)
        except Exception:                                 # noqa: BLE001
            return None

    def _own_frames_due(self, trig: _Trigger) -> bool:
        """A frame Typhon fired for has not arrived yet (the lock held)."""
        return trig.arrived < trig.fired or any(
            t.arrived < t.fired for t in self._pending if t is not trig)

    def _wait_ready(self, timeout: float, trig: _Trigger,
                    stoppable: bool) -> Optional[bool]:
        """Is the camera ready for a trigger? Asked in short steps (WAKE_S)
        so a Stop is seen while it waits (`stoppable`): None then, as "do
        not fire". A camera that CANNOT SAY (trigger_ready None) is taken
        as ready once every frame Typhon fired so far has come — a trigger
        fired into a camera still busy is ignored with no error, and the
        next frame was then paired with it (review)."""
        deadline = self._clock() + max(0.0, float(timeout))
        while True:
            left = max(0.0, deadline - self._clock())
            ready = self._ready(min(left, WAKE_S) if stoppable else left)
            if ready is None:
                with self._lock:
                    ready = not self._own_frames_due(trig)
                if not ready and self._clock() < deadline:
                    if stoppable and self._stop_asked():
                        return None
                    time.sleep(0.002)
                    continue
                if not ready:
                    trig.note = trig.note or ("the camera cannot say it is "
                                              "ready, and the last frame "
                                              "fired had not come")
            if ready or self._clock() >= deadline:
                return bool(ready)
            if stoppable and self._stop_asked():
                return None

    def _trigger(self, source: str) -> None:
        number = self._next_number()
        trig = _Trigger(number, source, self.per)
        if not self.exact:
            now = self._clock()
            with self._lock:
                current = self._current
                if current is not None and not current.done and \
                        current.arrived < current.asked:
                    # Cut short only when its frames had not all come: one
                    # whose frames all came, their saves still being told
                    # (confirm_saves), settles when they are.
                    self._settle(current, CUT_SHORT)
                trig.fired_mono, trig.fired_wall = now, self._wall()
                trig.fired = self.per
                trig.deadline = now + DEADLINE_S + (self.per + 1) * max(
                    self.exposure_s, self.frame_period_s)
                self._current = trig
                self.last_at = now
            return
        trig.firing = True
        for i in range(self.per):
            if i == 0:
                timeout = (FIRST_READY_S if not self._ever_ready else
                           0.0 if source == TIMER else PRESS_READY_S)
            else:
                if self._stop_asked():
                    # Stop came mid-burst: nothing more is exposed. The
                    # frames fired so far are waited for (wait_pending).
                    with self._lock:
                        trig.stopped = True
                    break
                timeout = self.exposure_s + DEADLINE_S
            # The first frame of a press made before Stop is fired (it was
            # answered "trigger sent"); the rest of a burst is not.
            ready = self._wait_ready(timeout, trig, stoppable=i > 0)
            if ready is None:
                with self._lock:
                    trig.stopped = True
                break
            if ready is False:
                with self._lock:
                    if i == 0:
                        trig.firing = False
                        self._settle(trig, MISSED_BUSY)
                        return
                    trig.note = (f"{i} of {self.per} fired: the camera was "
                                 f"busy")
                break
            self._ever_ready = True
            with self._lock:
                now = self._clock()
                if i == 0:
                    trig.fired_mono, trig.fired_wall = now, self._wall()
                    self._pending.append(trig)
                    self.last_at = now
                trig.fired += 1
                trig.deadline = now + self.exposure_s + DEADLINE_S
            try:
                self.device.fire_trigger()
            except Exception as exc:                      # noqa: BLE001
                with self._lock:
                    trig.fired -= 1
                    trig.note = f"not fired: {type(exc).__name__}: {exc}"
                    if i == 0:
                        trig.firing = False
                        self._settle(trig, NOT_FIRED, note=trig.note)
                        return
                break
        with self._lock:
            trig.firing = False
            self._maybe_settle(trig)

    def _expire(self) -> None:
        """Triggers past their deadline (exposure + DEADLINE_S) with frames
        still due are "no frame" — but an exact trigger of Typhon's that got
        NONE is first HELD as long again: frames come in the order their
        triggers were fired, so a frame arriving while one is held is ITS
        late frame. Settled at once, its late frame was paired with the
        next trigger and that trigger's own frame left "untriggered" — both
        frames given the wrong trigger in the frames CSV (review). A late
        frame comes from ExposureAuto lengthening the exposure after Start
        (read once, then), a slow link, or frames queued in pylon."""
        now = self._clock()
        with self._lock:
            for trig in list(self._held):
                if trig.held_until is not None and now > trig.held_until:
                    self._held.remove(trig)
                    self._settle(trig, NO_FRAME)
            waiting = list(self._pending)
            if self._current is not None and not self._current.done:
                waiting.append(self._current)
            for trig in waiting:
                if trig.firing or trig.deadline is None or now <= trig.deadline:
                    continue
                if trig.arrived >= trig.fired:
                    continue
                if (self.exact and self.typhon_fires and trig.arrived == 0
                        and trig.held_until is None):
                    trig.held_until = now + self.exposure_s + DEADLINE_S
                    self._pending.remove(trig)
                    self._held.append(trig)
                    continue
                self._settle(trig, NO_FRAME if trig.arrived == 0
                             else self._partial(trig))

    # -- the books (the lock held) ---------------------------------------
    def _partial(self, trig: _Trigger) -> str:
        return f"{NO_FRAME}: {trig.arrived} of {trig.asked} frames"

    def _maybe_settle(self, trig: _Trigger) -> None:
        if (trig.done or trig.firing or trig.arrived < trig.fired
                or trig.waiting > 0):
            return
        if trig.failed:
            self._settle(trig, FAILED_GRAB)
        elif trig.unsaved:
            # Its frames came, and the run does not hold them: a miss,
            # however the camera did (it was "ok" before — review).
            self._settle(trig, f"{NOT_SAVED}: {trig.unsaved_why}, "
                               f"{trig.unsaved} of {trig.asked} frame"
                               f"{'' if trig.asked == 1 else 's'}")
        elif trig.got >= trig.asked:
            self._settle(trig, OK)
        elif trig.stopped:
            # Stop ended the burst: not the camera's doing, not a miss.
            self._settle(trig, f"{STOPPED}: {trig.fired} of {trig.asked} "
                               f"fired")
        else:
            # Fired fewer than asked (busy mid-burst), all of those came.
            self._settle(trig, MISSED_BUSY if trig.fired == 0 else
                         f"{MISSED_BUSY}: {trig.fired} of {trig.asked} "
                         f"fired")

    def _settle(self, trig: _Trigger, outcome: str, note: str = "") -> None:
        if trig.done:
            return
        trig.outcome = outcome
        if note:
            trig.note = note
        try:
            self._pending.remove(trig)
        except ValueError:
            pass
        try:
            self._held.remove(trig)
        except ValueError:
            pass
        if self._current is trig:
            self._current = None
        if outcome == OK:
            self.counts["ok"] += 1
        elif outcome.startswith(MISSED_BUSY):
            self.counts["missed_busy"] += 1
        elif outcome.startswith(NO_FRAME):
            self.counts["no_frame"] += 1
        elif outcome == FAILED_GRAB:
            self.counts["failed_grab"] += 1
        elif outcome == CUT_SHORT:
            self.counts["cut_short"] += 1
        elif outcome == NOT_FIRED:
            self.counts["not_fired"] += 1
        elif outcome.startswith(NOT_SAVED):
            self.counts["not_saved"] += 1
        elif outcome.startswith(STOPPED):
            self.counts["stopped"] += 1
        said = outcome + (f" ({trig.note})" if trig.note else "")
        self._rows.append((trig.number, trig.source, _stamp(trig.fired_wall),
                           _stamp(trig.first_wall), trig.asked, trig.got,
                           trig.failed, said))

    # -- the grab thread -------------------------------------------------
    def confirm_saves(self) -> None:
        """The capture session tells this run whether each frame it tagged
        was saved (saved()): a trigger is settled only then — "ok" only
        when the run holds its frames. Without it (the books alone) a
        trigger is settled as its frames arrive."""
        self._confirm = True

    def saved(self, tags: Optional[Dict[str, Any]], ok: bool,
              why: str = "") -> None:
        """Whether a frame tag() answered `tags` for was saved — or not,
        and `why` ("storage too slow", "recording stopped"). Grab thread."""
        if not tags:
            return
        try:
            number = int(tags.get("trigger") or 0)
        except (TypeError, ValueError):
            return
        with self._lock:
            trig = self._awaiting.get(number)
            if trig is None:
                return
            trig.waiting = max(0, trig.waiting - 1)
            if trig.waiting == 0:
                del self._awaiting[number]
            if not ok:
                trig.unsaved += 1
                trig.unsaved_why = trig.unsaved_why or why or "not saved"
            self._maybe_settle(trig)

    def _frame_number(self, frame: Any) -> Optional[int]:
        meta = getattr(frame, "meta", None) or {}
        value = meta.get("frame_id")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _exact_trigger_for(self, frame: Any) -> Optional[_Trigger]:
        """The trigger of Typhon's an exact run's frame belongs to (the
        lock held): the oldest HELD trigger (past its deadline, its frame
        late) — unless the camera's frame number skips one per held
        trigger, which says its frame was lost on the way, not late — then
        the oldest pending one still owed a frame."""
        fid = self._frame_number(frame)
        if fid is not None:
            skipped = (fid - self._last_fid - 1
                       if self._last_fid is not None else 0)
            self._last_fid = fid
            while skipped > 0 and self._held:
                lost = self._held[0]
                owed = max(1, lost.fired - lost.arrived)
                if owed > skipped:
                    break
                self._settle(lost, NO_FRAME,
                             note="its frame was lost on the way: the "
                                  "camera's frame numbers skip it")
                skipped -= owed
        if self._held:
            trig = self._held[0]
            late = (self._clock() - trig.fired_mono
                    if trig.fired_mono is not None else 0.0)
            trig.note = f"late frame: it came {late:.1f} s after the trigger"
            if trig.arrived + 1 >= trig.fired:
                self._held.popleft()
            return trig
        return next((t for t in self._pending if t.arrived < t.fired), None)

    def tag(self, frame: Any = None) -> Optional[Dict[str, Any]]:
        """The trigger columns of a frame that just arrived — or None: a
        snapshot frame no trigger asked for (grabbed, shown, not saved)."""
        arrived_mono, arrived_wall = self._clock(), self._wall()
        with self._lock:
            if self.exact and self.typhon_fires:
                trig = self._exact_trigger_for(frame)
                if trig is None:
                    # The camera sent a frame no trigger of Typhon's asked
                    # for: saved (it is the camera's data), and counted.
                    self.counts["untriggered"] += 1
                    self.counts["frames"] += 1
                    return self._tags(None, 0, arrived_wall)
                trig.got += 1
                return self._took(trig, arrived_wall)
            if self.exact:
                trig = self._hardware_frame(arrived_mono, arrived_wall)
                trig.got += 1
                return self._took(trig, arrived_wall)
            trig = self._current
            if (trig is None or trig.done or trig.arrived >= trig.asked
                    or trig.fired_mono is None
                    or arrived_mono <= trig.fired_mono):
                return None
            trig.got += 1
            return self._took(trig, arrived_wall)

    def _hardware_frame(self, mono: float, wall: float) -> _Trigger:
        """Exact with the camera's own input: every `per` frames that
        arrive are one trigger (a burst of `per`, or one frame each)."""
        if self._hw_current is None or self._hw_current.done or \
                self._hw_current.arrived >= self._hw_current.asked:
            self._number += 1
            self.counts["triggers"] += 1
            trig = _Trigger(self._number, self.setup.source, self.per,
                            fired=self.per)
            self._hw_current = trig
            self.last_at = mono
        return self._hw_current

    def _took(self, trig: _Trigger, wall: float) -> Dict[str, Any]:
        if trig.first_wall is None:
            trig.first_wall = wall
        self.counts["frames"] += 1
        tags = self._tags(trig, trig.arrived, wall)
        if self._confirm:
            # Settled once the session says the frame was saved (saved()).
            trig.waiting += 1
            self._awaiting[trig.number] = trig
        self._maybe_settle(trig)
        return tags

    def _tags(self, trig: Optional[_Trigger], sub: int,
              wall: float) -> Dict[str, Any]:
        if trig is None:
            return {"trigger": "", "trigger_frame": "", "trigger_source": "",
                    "trigger_time": "", "arrived": _stamp(wall)}
        return {"trigger": trig.number, "trigger_frame": sub,
                "trigger_source": trig.source,
                "trigger_time": _stamp(trig.fired_wall),
                "arrived": _stamp(wall)}

    def failed_grab(self, index: Any = None) -> None:
        """A grab came back failed (cameras.GrabFailed): an exposure the
        camera made and could not deliver — it uses up a frame of the
        trigger it belongs to."""
        with self._lock:
            if self.exact and self.typhon_fires:
                trig = self._held[0] if self._held else next(
                    (t for t in self._pending if t.arrived < t.fired), None)
                if trig is not None and trig in self._held and \
                        trig.arrived + 1 >= trig.fired:
                    self._held.popleft()
                if self._last_fid is not None:
                    # It used up the camera's next frame number.
                    self._last_fid += 1
            elif self.exact:
                trig = self._hardware_frame(self._clock(), self._wall())
            else:
                trig = self._current
                if trig is not None and (trig.done or
                                         trig.arrived >= trig.asked):
                    trig = None
            if trig is None:
                return
            trig.failed += 1
            if trig.first_wall is None:
                trig.first_wall = self._wall()
            self._maybe_settle(trig)

    # -- what is said ------------------------------------------------------
    def edges_counted(self, edges: Optional[int], note: str = "",
                      window: str = "") -> None:
        """The camera's own count of edges on the hardware input (read at
        Stop), or None with why not; `window`: between which moments."""
        self.edges = edges
        self.edges_note = note
        self.edges_window = window

    def missed_hardware(self) -> Optional[int]:
        """Edges the camera counted that triggered nothing (it was busy),
        when it counted them — else None."""
        if self.edges is None or self.typhon_fires or not self.exact:
            return None
        return max(0, int(self.edges) - int(self.counts["triggers"]))

    @property
    def missed(self) -> int:
        """Triggers that saved nothing they asked for: busy, no frame,
        failed, not fired, not saved — and hardware edges the camera
        counted and ignored."""
        c = self.counts
        return (c["missed_busy"] + c["no_frame"] + c["failed_grab"]
                + c["not_fired"] + c["not_saved"]
                + (self.missed_hardware() or 0))

    def summary(self) -> Dict[str, Any]:
        """The run's capture block: its set-up and what came of it."""
        with self._lock:
            counts = dict(self.counts)
        out: Dict[str, Any] = dict(self.setup.as_record())
        out.update({
            "triggers": counts["triggers"], "ok": counts["ok"],
            "missed": {"camera_busy": counts["missed_busy"],
                       "no_frame": counts["no_frame"],
                       "failed_grab": counts["failed_grab"],
                       "not_fired": counts["not_fired"],
                       "not_saved": counts["not_saved"],
                       "hardware_edges_ignored": self.missed_hardware()},
            "cut_short": counts["cut_short"],
            "cut_by_stop": counts["stopped"],
            "frames_arrived": counts["frames"],
            "untriggered_frames": counts["untriggered"],
            "edges_counted": self.edges,
            "rule": self.rule(),
            "triggers_file": self.path.name if self.path else "",
        })
        if self.edges_window:
            out["edges_counted_between"] = self.edges_window
        notes = []
        if self.edges_note:
            notes.append(self.edges_note)
        if self.file_error:
            notes.append(f"the triggers CSV stopped: {self.file_error}")
        if notes:
            out["notes"] = notes
        return out

    def rule(self) -> str:
        """Which frames a trigger saved, in words."""
        n = self.per
        frames = "frame" if n == 1 else f"{n} frames"
        if not self.exact:
            return (f"the first {frames} that reached Typhon after the "
                    f"trigger; the first may have begun its exposure up to "
                    f"one frame period before it")
        if self.typhon_fires:
            return (f"the camera exposed {'the frame' if n == 1 else frames} "
                    f"on Typhon's software trigger, fired once the camera "
                    f"said it was ready")
        return (f"the camera exposed {'one frame' if n == 1 else frames} on "
                f"each {self.setup.source} trigger it accepted")

    def line(self) -> str:
        """The live status line's head while the run is armed."""
        with self._lock:
            c = dict(self.counts)
            last = self.last_at
        setup = self.setup
        per = (f"{self.per} frames" if self.per > 1 else "1 frame")
        if self.exact:
            if setup.source == SOFTWARE:
                head = "Armed — press Capture now (F9)"
            elif setup.source == TIMER:
                left = max(0.0, self._due() - self._clock())
                head = (f"Armed — every {show_seconds(self.period)}, next "
                        f"in {left:.1f} s")
            else:
                edge = f" {setup.edge}" if setup.edge else ""
                head = f"Armed — waiting for {setup.source}{edge}"
        else:
            if setup.source == TIMER:
                head = (f"Snapshot — live, saving {per} every "
                        f"{show_seconds(self.period)}")
            else:
                head = f"Snapshot — live, saving {per} per Capture now (F9)"
        bits = [head, f"{c['triggers']} trigger"
                      f"{'' if c['triggers'] == 1 else 's'}", f"{c['ok']} ok"]
        missed = (c["missed_busy"] + c["no_frame"] + c["failed_grab"]
                  + c["not_fired"] + c["not_saved"])
        if self.exact and not self.typhon_fires:
            bits.append("missed: counted at Stop" if missed == 0 else
                        f"{missed} MISSED")
        else:
            bits.append(f"{missed} MISSED" if missed else "0 missed")
        if c["missed_busy"]:
            bits[-1] += " (camera busy)" if missed == c["missed_busy"] else ""
        elif c["not_saved"]:
            bits[-1] += " (NOT saved)" if missed == c["not_saved"] else ""
        if not self.exact:
            bits.append(f"{c['cut_short']} cut short")
        if c["untriggered"]:
            bits.append(f"{c['untriggered']} frames with no trigger")
        if last is not None:
            bits.append(f"last {self._clock() - last:.1f} s ago")
        return " · ".join(bits)

    def stopped_line(self) -> str:
        """Said after Stop: the triggers, what came of them, and the
        camera's own count of edges where it gave one."""
        c = self.counts
        bits = [f"{c['triggers']} trigger{'' if c['triggers'] == 1 else 's'}",
                f"{c['ok']} ok"]
        missed = self.missed
        if missed or self.typhon_fires or not self.exact:
            bits.append(f"{missed} missed")
            if c["not_saved"]:
                bits[-1] += f" ({c['not_saved']} NOT saved)"
        if not self.exact:
            bits.append(f"{c['cut_short']} cut short")
        if c["stopped"]:
            bits.append(f"{c['stopped']} cut short by Stop")
        said = ", ".join(bits)
        if self.exact and not self.typhon_fires:
            if self.edges is not None:
                said += (f" · the camera counted {self.edges} edge"
                         f"{'' if self.edges == 1 else 's'} on "
                         f"{self.setup.source}")
            else:
                said += (f" · missed hardware triggers NOT counted: "
                         f"{self.edges_note or 'the camera has no edge counter'}")
        return said


def frame_columns() -> Tuple[str, ...]:
    """The frames CSV columns a trigger fills (capture.INDEX_COLUMNS)."""
    return ("trigger", "trigger_frame", "trigger_source", "trigger_time",
            "arrived")
