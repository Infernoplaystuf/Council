"""
council_core.capture — the grab loop, the frame budget, and recording.

A boA5320-150cm runs to 150 fps and an EVK4's event rate has no frame ceiling
at all. No UI redraws that fast, and none should try. So this module separates
two things that look alike and are not:

  DISPLAY frames may be dropped. The viewer wants the NEWEST frame, not every
  frame, and a queue that grows is a live view that drifts further behind the
  sensor every second while still calling itself live. Dropping is correct —
  but it is COUNTED and shown, because a viewer silently showing 12 of every
  150 frames while the user reads "150 fps" off the camera is lying.

  RECORDED frames are written on their OWN thread (FrameWriter), and if
  storage cannot keep up the overflow is COUNTED and shown as "NOT saved".
  This used to be the opposite — recording inline in the grab loop, on the
  theory that a slowing frame rate was the honest failure. A real EVK4 test
  writing to a network share showed why that was wrong: the loop crawled,
  and the camera kept producing while it waited, so events piled up in the
  SDK and smeared into the next window. Stalling the grab loop does not save
  those frames either; it only hides where they went.

STOP IS BOUNDED AND JOINED
`stop()` asks the loop to finish, waits, and returns whether it actually
ended. A capture app whose Stop button returns while a thread is still pushing
frames into a closing window is the standard way to get a crash on exit.

NOTHING HERE IMPORTS A TOOLKIT
The session hands frames to a callback. Who marshals them onto a UI thread is
the view's problem, not this module's.
"""
from __future__ import annotations

import collections
import csv
import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import cameras, reduce, triggered

#: How long a stop() waits for the grab loop before reporting failure.
STOP_TIMEOUT = 3.0

#: The window the measured rate is averaged over, in seconds.
RATE_WINDOW = 1.0

#: A camera that has sent nothing for this many of its own frame intervals
#: (the last one measured; and at least RATE_WINDOW) is not running at its
#: last measured rate.
QUIET_INTERVALS = 3.0

#: How many consecutive failed reads end a capture. A backstop for any fault
#: that repeats every time rather than clearing.
MAX_CONSECUTIVE_ERRORS = 50

#: What the display-drop count (Stats.dropped) is called in a status line.
#: "dropped" was read as frames LOST; they are frames the screen did not
#: redraw — every one of them is still saved. ONE wording for Typhon's
#: lines (frame_camera) and the Council's Capture tab, which said "N
#: dropped" for the number Typhon calls this.
SCREEN_DROPS = "not drawn (screen only)"


@dataclass(frozen=True)
class Stats:
    """What the grab loop has actually done."""
    grabbed: int = 0
    #: Frames the display never saw because a newer one replaced them.
    dropped: int = 0
    recorded: int = 0
    errors: int = 0
    #: Frames per second, measured — not the camera's configured rate.
    rate: float = 0.0
    last_error: str = ""
    #: Set when recording stopped because writing failed.
    recording_failed: str = ""
    #: Frames NOT saved because storage fell behind and the write queue was
    #: full. Not the same as `dropped`, which is frames the screen skipped.
    skipped: int = 0
    #: Frames grabbed and queued, not yet on disk.
    waiting: int = 0
    #: pylon's skipped-image count (Frame.meta["skipped_by_camera"]): frames
    #: pylon threw away before the app saw them. It counts ONLY in pylon's
    #: "latest image" grab strategies, which a live view uses. A recording
    #: grabs "one by one", where it stays 0 (vendor docs; measured on the
    #: emulator: 0, against 161 under LatestImageOnly with a slow reader —
    #: docs/typhon_multicamera_design.md 3.7). So it does NOT count frames
    #: lost while recording; a failed grab is counted in `grab_failures`.
    camera_skipped: int = 0
    #: Grabs that came back FAILED (cameras.GrabFailed): exposures the camera
    #: made and could not deliver, such as an incomplete frame on the link.
    #: Each one used up a frame number, so it is a gap in the saved files'
    #: numbers, and the frames CSV's `grab_failures` column says where.
    grab_failures: int = 0
    #: Frames of a recording that STOPPED ON A FAILED WRITE (recording_
    #: failed) and so were grabbed and NOT saved: the frame whose write
    #: failed, those queued behind it, and every frame grabbed after it
    #: until the app ended the recording (record_to). Not frozen at the
    #: failure: a run that went on for a minute after it lost a minute.
    unsaved: int = 0
    #: Frames of a "Triggered: snapshot" run that came BETWEEN triggers:
    #: grabbed and shown, never asked for, so not saved — and not a loss
    #: (council_core.triggered). Always 0 for any other run.
    passed: int = 0

    @property
    def grab_attempts(self) -> int:
        """Grabs that came back, good or failed. A timed-out read is not a
        grab: nothing came back."""
        return self.grabbed + self.grab_failures

    def line(self, dropped_label: str = SCREEN_DROPS) -> str:
        """The one line a status bar shows.

        The drop count is always present, including at zero. A number that
        only appears once it is bad is a number nobody is watching when it
        goes bad. It is called SCREEN_DROPS ("not drawn (screen only)") in
        every view — "dropped" was read as frames lost; `dropped_label`
        says it otherwise.
        """
        bits = [f"{self.rate:.1f} fps", f"{self.grabbed} grabbed",
                f"{self.dropped} {dropped_label}"]
        if self.recorded:
            bits.append(f"{self.recorded} saved")
        if self.passed:
            bits.append(f"{self.passed} between triggers (not asked for)")
        if self.waiting:
            bits.append(f"{self.waiting} waiting to save")
        if self.skipped:
            bits.append(f"{self.skipped} NOT saved (storage too slow)")
        if self.unsaved:
            bits.append(f"{self.unsaved} NOT saved (recording stopped)")
        if self.camera_skipped:
            bits.append(f"{self.camera_skipped} lost by the camera/driver")
        if self.grab_failures:
            bits.append(f"{self.grab_failures} failed grab"
                        f"{'' if self.grab_failures == 1 else 's'}")
        if self.errors:
            # Failed grabs are NOT among these (CaptureSession._loop): one
            # failed grab once read "1 failed grab · 1 error(s)", as if two.
            bits.append(f"{self.errors} error(s)")
        return " · ".join(bits)


class LatestFrame:
    """A one-slot mailbox that keeps the NEWEST frame and counts the rest.

    `put` overwriting an unread frame is a display drop. It is counted here
    rather than shrugged off, because this is the only place in the program
    that can tell the difference between "the camera sent 150" and "the
    screen showed 12".
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[cameras.Frame] = None
        self.dropped = 0

    def put(self, frame: cameras.Frame) -> bool:
        """Store `frame`. Returns True if it displaced an unread one."""
        with self._lock:
            displaced = self._frame is not None
            if displaced:
                self.dropped += 1
            self._frame = frame
        return displaced

    def take(self) -> Optional[cameras.Frame]:
        with self._lock:
            frame, self._frame = self._frame, None
        return frame


#: zlib level for PNGs Pillow writes — now only the layouts fast_png does not
#: take. 1, not Pillow's default 6: measured on EVK4 windows level 6 managed
#: 14 PNGs a second on one thread and level 1 managed 61.
PNG_COMPRESS_LEVEL = 1


def write_image(image: Any, path: Path) -> None:
    """A frame as an image file the rest of the Barbie toolchain can read.

    PNG, because `frame_roi`, `frame_timing` and `frame_classes` all discover
    frames by IMAGE_SUFFIXES and open them with Pillow. A capture written as
    .npy is invisible to every one of them.

    WRITTEN BY council_core.fast_png, NOT PILLOW. Pillow took 615 ms to write
    one full-frame boA5320 Mono8 PNG and ~1 s for 16-bit, so eight writers
    saved 5-12 of the 30 frames a second while the PC sat mostly idle
    (measured end to end: an 8 s capture took 49 s / 117 s to finish saving).
    fast_png writes the same standard PNG in 20 / 50 ms — stored when deflate
    would not pay (camera frames), compressed when it is cheap and does (EVK4
    pictures: smaller AND faster than Pillow was). Pillow is kept for any
    layout fast_png refuses, which it does before touching the file.

    THE PIXELS ARE WRITTEN AS THE CAMERA PRODUCED THEM. A 12-bit Basler frame
    arrives as uint16 holding 0..4095, and it is saved as a 16-bit PNG holding
    0..4095 -- verified lossless round-trip. Rescaling it to fill the 16-bit
    range would make the picture look right and the MEASUREMENT wrong, and
    this is a capture app: the file is the data.

    (Known consequence, deliberately not papered over: frame_classes.thumbnail
    scales 16-bit input by 1/256 on the assumption it fills the full range, so
    a 12-bit frame reads dark to the classifier. That is a conversion bug in
    the reader, not a reason for the writer to alter the user's pixels.)

    NEVER OVER ANYTHING. Both writers create the file with exclusive
    creation ("xb"): a file already at `path` raises FileExistsError and is
    left exactly as it was. A frame's name is its run's and its number, so a
    clash means something else went wrong — two cameras given one run name
    lost half their frames this way, silently, when frames were written
    "wb" (docs/typhon_multicamera_design.md 2.6) — and a clash from any
    cause must fail loudly, as an unsaved frame, not replace one on disk.
    And neither leaves HALF a frame: a write that fails part-way (a full
    disk) removes the file that call created, so no PNG that cannot be
    opened is left in the run for the slider to list.
    """
    import numpy as np

    from . import fast_png

    data = np.asarray(image)
    if data.ndim == 2 and data.dtype not in (np.uint8, np.uint16):
        data = np.clip(data, 0, 255).astype(np.uint8)
    try:
        fast_png.write_png(data, path)
        return
    except TypeError:
        pass                    # a layout it does not take: Pillow below
    from PIL import Image

    # NO mode= argument. Pillow infers "I;16" from a uint16 dtype, and passing
    # the mode explicitly is deprecated for removal in Pillow 13 (2026-10-15)
    # -- which would break capture outright, not warn.
    picture = Image.fromarray(data)
    # Pillow is handed a file opened "xb" here (see NEVER OVER ANYTHING); it
    # still takes the format from the name's extension, through the handle.
    made = False
    try:
        with open(path, "xb") as handle:
            made = True
            picture.save(handle, compress_level=PNG_COMPRESS_LEVEL)
    except BaseException:
        if made:
            # Given a PATH, Pillow removes a file it made and failed to
            # finish; given our handle it cannot. This file is the one this
            # call created a moment ago — nobody else's — so it goes, rather
            # than lie in the run as a frame that cannot be opened.
            fast_png.discard_unfinished(path)
        raise


def warm_imports() -> None:
    """Pay for numpy and Pillow BEFORE acquisition begins.

    WHAT THIS DOES NOT DO is make Start faster — the import has to happen
    somewhere, and measured end to end the time from Start to the first frame
    is the same either way. The first guess here was wrong about that and the
    measurement corrected it.

    WHAT IT DOES FIX is a stale first frame. `import numpy` happens inside
    `device.read()`, and a free-running camera does not wait: with acquisition
    already started, the first read took 547 ms cold and 0 ms warmed. On a
    real camera that half-second is the sensor producing frames nobody is
    reading — discarded under LatestImageOnly, or backing up an event stream —
    so the first frame actually delivered is half a second old. Warming before
    `device.start()` means acquisition begins only once the loop can keep up.

    (The second read costs 0 ms, which is why this is a first-frame problem
    and not a throughput one. Pillow adds ~125 ms on the first write: 31 to
    import and 94 for `Image.init()`, which builds the plugin registry and is
    not paid by the import alone.)

    Never raises: a missing package is reported by the real call, which fails
    loudly. This is an optimisation, not a check.

    ONCE, AND BEST OFF THE UI THREAD. Called from the UI thread (a live
    view's first start) it was the window's longest stall after Connect:
    MEASURED in a built Typhon, the first tick after Connect took 110-307 ms
    (frame camera) and 113-483 ms (event camera) over five fresh starts each,
    nearly all of it importing PIL.Image's ~70 plugins — main.py has
    imported numpy and PIL's package by then, never PIL.Image. So
    `warm_in_background` does it on a thread of its own as soon as an app
    attaches a camera, `imports_warm` says when it is done, and a call made
    after that returns at once.
    """
    if _WARMED.is_set():
        return
    try:
        import numpy                                       # noqa: F401
    except Exception:                                      # noqa: BLE001
        pass
    try:
        from PIL import Image
        Image.init()
    except Exception:                                      # noqa: BLE001
        pass
    _WARMED.set()


_WARMED = threading.Event()
_WARMER: List[threading.Thread] = []
_WARMER_LOCK = threading.Lock()


def warm_in_background() -> None:
    """Start warm_imports on a thread of its own, once per process; a call
    after the first (or once warm) does nothing."""
    if _WARMED.is_set():
        return
    with _WARMER_LOCK:
        if _WARMER:
            return
        thread = threading.Thread(target=warm_imports, name="warm-imports",
                                  daemon=True)
        _WARMER.append(thread)
    thread.start()


def imports_warm() -> bool:
    """numpy and Pillow are imported and Pillow's plugins registered."""
    return _WARMED.is_set()


#: The columns of a run's frame index (Recorder `index_name`). window_us is
#: how long each event-camera picture collected events — the raw view uses
#: the same window, whatever frame rate the run was captured at.
#:
#: timestamp_us IS THE CAMERA'S OWN CLOCK, AS IT GAVE IT. For an event camera
#: it is microseconds. For a Basler it is the grab result's tick count
#: (GetTimeStamp), NOT microseconds: the tick's length depends on the model,
#: and a boost CXP camera (the boA5320) has no timestamp at all, so it is 0
#: there. The name is kept so that older runs read the same.
#:
#: grab_failures is a running count of the grabs that came back failed so
#: far in this recording (cameras.GrabFailed), on each saved frame's row —
#: blank for a camera that does not report failed grabs (an event camera has
#: no grabs to fail). A failed grab uses up a frame number, so it is also a
#: gap in `index`.
#:
#: A TRIGGERED RUN'S COLUMNS (council_core.triggered), blank in any other:
#:   trigger          the trigger the frame was saved for, 1, 2, 3 … in the
#:                    order they came (<run>_capture_triggers.csv has a row
#:                    for every one, missed ones too)
#:   trigger_frame    which of that trigger's frames it is: 1 .. per trigger
#:   trigger_source   Software, Timer, or the camera's input (Line1 …)
#:   trigger_time     when Typhon fired it, seconds since 1970 — blank for
#:                    the camera's own input, whose moment is the camera's
#:                    timestamp_us
#:   arrived          when the frame reached Typhon, seconds since 1970
#: frame_id is the camera's own frame (block) number where it gives one —
#: blank where it does not (pylon's emulator: none).
#: downsample, downsample_method: the frame's Downsample factor and how it
#: was made (council_core.reduce: camera_binning_average, camera_binning_
#: sum_divided, typhon_block_mean) — blank when Downsample is Off.
#:
#: NEW COLUMNS GO AT THE END ONLY: every reader takes the CSV by column name
#: (event_playback.raw_origin, run_window_us, frame_times), and a reader of
#: an older run finds the columns it knows where they always were.
INDEX_COLUMNS = ("file", "index", "timestamp_us", "raw_t_us", "events",
                 "window_us", "grab_failures", "trigger", "trigger_frame",
                 "trigger_source", "trigger_time", "arrived", "frame_id",
                 "downsample", "downsample_method")

#: A run's frame index is "<run>_frames.csv" — and creating it is what
#: reserves the run's name (reserve_run).
INDEX_SUFFIX = "_frames.csv"

#: A triggered run's "<run>_capture_triggers.csv" (council_core.triggered).
TRIGGERS_SUFFIX = triggered.TRIGGERS_SUFFIX

#: A run's end record, "<run>_run_end.json": what only the end of a
#: recording knows (Recorder.finish) — above all the failed grabs, which
#: leave no row of their own, so those after the last saved frame were
#: nowhere on disk.
RUN_END_SUFFIX = "_run_end.json"
RUN_END_FORMAT = "typhon-run-end"
RUN_END_VERSION = 1

#: The endings that put a run's name on a file in a capture folder. A run
#: with any file of these there is taken, and so is a FOLDER of the run's
#: name (a run of several cameras is a folder; the multi-camera design 2.6).
RUN_MARKERS = ("_frame_", "_events.raw", INDEX_SUFFIX, "_camera.json",
               RUN_END_SUFFIX, TRIGGERS_SUFFIX)

#: How many names reserve_run tries (base, base_2, ...) before giving up.
RESERVE_TRIES = 1000


def taken_runs(folder: Any) -> set:
    """The run names already used in `folder`: every name a file of a run
    starts with (RUN_MARKERS), and every sub-folder's name. Empty for a
    folder that does not exist or cannot be listed."""
    names: set = set()
    try:
        with os.scandir(str(folder)) as found:
            for entry in found:
                try:
                    if entry.is_dir():
                        names.add(entry.name)
                except OSError:
                    pass
                for marker in RUN_MARKERS:
                    head, hit, _ = entry.name.partition(marker)
                    if hit:
                        names.add(head)
    except OSError:
        pass
    return names


def claim_run(folder: Any, base: str) -> Tuple[str, Any]:
    """RESERVE a run's name in `folder`: `base`, or `base_2`, `base_3`, ...
    when that one is taken. Returns (the name, its frames CSV still OPEN for
    appending, header written) — hand the handle to the run's Recorder
    (run_recorder), which writes through it and closes it.

    Reserved, not just looked for. Looking at the folder was not enough:
    two Starts in one second — two windows saving into one folder — both
    looked before either had written a thing, got the same name, and
    silently overwrote each other's frames (measured: 184-185 frames
    reported saved, 92-93 PNGs on disk, duplicated CSV rows, the header not
    on line 1; docs/typhon_multicamera_design.md 2.6). So the run's frames
    CSV is created here with EXCLUSIVE CREATION ("x": the file system
    refuses a name that exists, whoever made it and however close in time)
    and its header is written at once. A name whose file someone else
    created first is taken, and the next one is tried.

    KEPT OPEN, NOT CLOSED AND OPENED AGAIN. Closed here and reopened by the
    recorder, the CSV was free in between for any program that opens a new
    file without sharing for a moment (some antivirus, sync and indexing
    programs do) — measured: Start then failed with "Permission denied"
    after the live view had stopped. Held from creation to the run's end,
    such a program cannot take it.

    Raises OSError when the folder cannot be made or written, or no name is
    free after RESERVE_TRIES."""
    where = Path(str(folder))
    where.mkdir(parents=True, exist_ok=True)
    taken = taken_runs(where)
    run = str(base)
    for n in range(2, RESERVE_TRIES + 2):
        if run not in taken:
            try:
                handle = open(where / f"{run}{INDEX_SUFFIX}", "x",
                              newline="", encoding="utf-8")
            except FileExistsError:
                handle = None              # another window was first
            if handle is not None:
                try:
                    csv.writer(handle).writerow(INDEX_COLUMNS)
                    handle.flush()         # line 1, before anything else
                except BaseException:
                    handle.close()
                    raise
                return run, handle
        run = f"{base}_{n}"
    raise OSError(f"no free run name in {where} after {RESERVE_TRIES} "
                  f"tries from {base}")


def reserve_run(folder: Any, base: str) -> str:
    """claim_run, with the CSV closed: the reserved name alone. A Recorder
    for it appends to the CSV and writes no second header. Prefer claim_run
    for a run about to be recorded (see KEPT OPEN there)."""
    run, handle = claim_run(folder, base)
    handle.close()
    return run


def run_recorder(folder: Any, run: str, claim: Any = None) -> "Recorder":
    """The Recorder for the run `run` in `folder`: <run>_frame_NNNNNN.png,
    one row each in <run>_frames.csv — through `claim`, the handle
    claim_run kept open, when given — and <run>_run_end.json when the
    recording ends (Recorder.finish)."""
    return Recorder(folder, stem=f"{run}_frame",
                    index_name=f"{run}{INDEX_SUFFIX}", index_handle=claim,
                    end_name=f"{run}{RUN_END_SUFFIX}")


class Recorder:
    """Frames to disk, one file each, numbered in grab order.

    NUMBERED BY GRAB INDEX, NOT BY A COUNTER OF ITS OWN. A recorder that
    numbers its own output 0,1,2 produces a tidy sequence that silently hides
    any frame the camera never delivered; naming files after the frame index
    means a gap in the filenames is a gap in the capture, visible in a
    directory listing. A grab that came back failed uses up its number too
    (cameras.GrabFailed), so it is a gap like any other.
    """

    def __init__(self, out_dir: Any, stem: str = "frame",
                 writer: Optional[Callable[[Any, Path], None]] = None,
                 suffix: Optional[str] = None,
                 index_name: Optional[str] = None,
                 index_handle: Any = None,
                 end_name: Optional[str] = None):
        self.dir = Path(out_dir)
        self.stem = str(stem) or "frame"
        self.written = 0
        self._writer = writer or write_image
        self._suffix = suffix if suffix is not None else (
            ".png" if writer is None else "")
        #: A CSV with one row per saved frame (INDEX_COLUMNS), or None.
        #: A picture of an event stream is not a measurement on its own; the
        #: row says when it was taken, how many events it holds, and where it
        #: falls in the run's .raw — which is how a viewer swaps between the
        #: two at the same moment.
        self.index_path: Optional[Path] = (
            self.dir / index_name if index_name else None)
        self._index_file: Any = None
        self._index_rows: Any = None
        #: The index as claim_run left it, open with its header written:
        #: written through, never closed and opened again (see claim_run).
        self._claimed: Any = index_handle
        #: The run's end record (finish), or None for none.
        self.end_path: Optional[Path] = (
            self.dir / end_name if end_name else None)
        #: Why the end record was not written, or "".
        self.end_note = ""
        self._finished = False
        #: A triggered run's capture block for the end record — a function
        #: (council_core.triggered.TriggerRun.summary), asked when the
        #: recording ends — or None.
        self.capture_info: Optional[Callable[[], Dict[str, Any]]] = None
        #: The run's Downsample block for the end record, or None for Off.
        self.downsample_info: Optional[Dict[str, Any]] = None

    def open(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        warm_imports()
        if self._claimed is not None and self._index_file is None:
            # RESERVED AND STILL OPEN (claim_run): header and all, written
            # through the handle that created it.
            self._index_file, self._claimed = self._claimed, None
            self._index_rows = csv.writer(self._index_file)
        if self.index_path is not None and self._index_file is None:
            # A RESERVED run's index is already there, header and all
            # (reserve_run): it is appended to, with no second header. One
            # that is not there yet is created EXCLUSIVELY, so two recorders
            # can never both decide it is new and both write a header — a
            # look first ("does it exist?") let them. APPEND, never
            # truncate: a file already there is the user's data.
            try:
                handle = open(self.index_path, "x", newline="",
                              encoding="utf-8")
                fresh = True
            except FileExistsError:
                handle = open(self.index_path, "a", newline="",
                              encoding="utf-8")
                fresh = False
            self._index_file = handle
            self._index_rows = csv.writer(handle)
            if fresh:
                self._index_rows.writerow(INDEX_COLUMNS)
                handle.flush()             # line 1, before anything else

    def path_for(self, frame: cameras.Frame) -> Path:
        return self.dir / f"{self.stem}_{frame.index:06d}{self._suffix}"

    def save(self, frame: cameras.Frame, path: Path) -> None:
        """Encode and write one frame. Safe to call from several threads at
        once: it touches nothing but its own file.

        DOWNSAMPLED HERE, on the writer threads, when the camera that took
        it says Typhon reduces it (Frame.meta "reduce", "divide";
        council_core.reduce.prepared) — never on the grab thread, whose
        budget at 150 fps is 6.7 ms against 22-34 ms for a full frame."""
        self._writer(reduce.prepared(frame.image, frame.meta), path)

    def log(self, frame: cameras.Frame, path: Path) -> None:
        """Count a saved frame and give it its index row. NOT thread-safe:
        FrameWriter calls it one frame at a time, in grab order."""
        self.written += 1
        if self._index_rows is not None:
            meta = frame.meta or {}
            window = meta.get("window_ms")
            factor = meta.get("downsample")
            self._index_rows.writerow((
                path.name, frame.index, frame.timestamp_us,
                meta.get("raw_t_us", ""), meta.get("events", ""),
                int(round(float(window) * 1000)) if window else "",
                meta.get("grab_failures", ""),
                meta.get("trigger", ""), meta.get("trigger_frame", ""),
                meta.get("trigger_source", ""), meta.get("trigger_time", ""),
                meta.get("arrived", ""), meta.get("frame_id", ""),
                factor if factor and int(factor) > 1 else "",
                meta.get("downsample_method", "")
                if factor and int(factor) > 1 else ""))

    def write(self, frame: cameras.Frame) -> Path:
        path = self.path_for(frame)
        self.save(frame, path)
        self.log(frame, path)
        return path

    def flush_index(self) -> None:
        """Put published rows on disk now, not at the next 8 KB boundary:
        the raw view reads the run's origin from this file while the tail of
        the run may still be saving."""
        handle = self._index_file
        if handle is not None:
            try:
                handle.flush()
            except (OSError, ValueError):
                pass

    def finish(self, totals: Mapping[str, Any]) -> Optional[Path]:
        """Write the run's END RECORD (`end_name`, format "typhon-run-end"),
        once: FrameWriter calls it when the recording's last frame is on
        disk — or its first failed write ended it — with its own counts
        (FrameWriter.totals). Returns its path, or None for none.

        WHY: a failed grab has no row of its own. The frames CSV's running
        grab_failures shows one only on a LATER saved frame's row, so
        failed grabs after the last saved frame — and the totals of grab
        attempts and failures (docs/typhon_multicamera_design.md 3.7) — were
        nowhere on disk once the window closed (measured: [ok, ok, ok, FAIL,
        FAIL, FAIL] left a CSV reading 0, 0, 0).

        WHAT IT SAYS (format "typhon-run-end", version 1)
            run, index        the run's name and its frames CSV
            written, at       when (local time, and seconds since 1970)
            frames_saved      PNGs written, each with its row in `index`
            frames_not_saved  storage_too_slow: skipped by the save queue;
                              write_failed: the frame whose write failed
                              and those queued behind it
            grab_failures     failed grabs while recording (null for a
                              camera that does not report them: an event
                              camera, the simulated one)
            grab_attempts     frames the recording was offered plus
                              grab_failures (null likewise)
            first_index, last_index   the first and last frame number the
                              recording was offered or failed to grab
            recording_failed  "" — or the write that ended the recording,
                              naming its frame. The record is written when
                              the recording ENDS, which for a failed write
                              is that moment: frames the camera sent after
                              it were not offered to the recording and are
                              not counted here (the app's Stop line counts
                              them "NOT saved (recording stopped)").
            capture           A TRIGGERED RUN ONLY (added in version 1, so a
                              reader of version 1 may meet it): its mode,
                              source, frames per trigger, and what came of
                              its triggers — triggers, ok, missed (camera
                              busy, no frame, failed grab, not fired, and
                              hardware edges the camera counted and
                              ignored), cut short, frames arrived, the rule
                              for which frames a trigger saved, and the
                              triggers CSV (council_core.triggered).
                              Frames passed over between snapshot triggers
                              are `frames_between_triggers`, not a loss.
            downsample        DOWNSAMPLE ON ONLY: its factor and method
                              (council_core.reduce), the picture's size, and
                              the right and bottom pixels a block could not
                              hold.

        NEVER OVER ANYTHING: written whole to a temporary name and renamed
        into a name that must not exist (camera_record.write_once). A record
        that cannot be written is kept in `end_note`; the frames come first.
        """
        if self.end_path is None or self._finished:
            return None
        self._finished = True
        name = self.end_path.name
        run = (name[:-len(RUN_END_SUFFIX)] if name.endswith(RUN_END_SUFFIX)
               else self.stem)
        counted = totals.get("grab_failures")
        doc = {
            "format": RUN_END_FORMAT, "format_version": RUN_END_VERSION,
            "run": run,
            "written": time.strftime("%Y-%m-%d %H:%M:%S"), "at": time.time(),
            "index": self.index_path.name if self.index_path else "",
            "frames_saved": int(totals.get("frames_saved", 0) or 0),
            "frames_not_saved": {
                "storage_too_slow": int(totals.get("storage_too_slow", 0)
                                        or 0),
                "write_failed": int(totals.get("write_failed", 0) or 0)},
            "grab_failures": None if counted is None else int(counted),
            "grab_attempts": (None if counted is None else
                              int(totals.get("offered", 0) or 0)
                              + int(counted)),
            "first_index": totals.get("first_index"),
            "last_index": totals.get("last_index"),
            "recording_failed": str(totals.get("recording_failed") or ""),
        }
        info = self.capture_info
        if callable(info):
            try:
                doc["capture"] = dict(info())
            except Exception as exc:                        # noqa: BLE001
                doc["capture"] = {"error": f"{type(exc).__name__}: {exc}"}
            doc["capture"]["frames_between_triggers"] = int(
                totals.get("passed", 0) or 0)
        if self.downsample_info:
            doc["downsample"] = dict(self.downsample_info)
        from . import camera_record
        try:
            return camera_record.write_once(self.end_path, doc)
        except Exception as exc:                            # noqa: BLE001
            self.end_note = f"{type(exc).__name__}: {exc}"
            return None

    def close(self) -> None:
        """Finish the index. Safe to call twice — and for a run that never
        recorded (a refused Start), which still holds claim_run's handle."""
        handle, self._index_file, self._index_rows = self._index_file, None, None
        claimed, self._claimed = self._claimed, None
        for each in (handle, claimed):
            if each is not None:
                try:
                    each.close()
                except OSError:
                    pass


def _physical_memory() -> int:
    """Bytes of RAM in this machine, or 0 if it cannot be read."""
    try:
        if os.name == "nt":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
            return 0
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except Exception:                                      # noqa: BLE001
        return 0


def _write_budget() -> int:
    """A quarter of this machine's RAM, between 512 MB and 8 GB.

    A fixed 512 MB held about ten full-frame boA5320 frames (49 MB each at
    12 bits) — a burst the disk could not absorb became "NOT saved" while
    most of the machine's memory sat unused. Big enough to ride out bursts,
    small enough to leave the rest of the machine alone.
    """
    quarter = _physical_memory() // 4
    return int(min(8 << 30, max(512 << 20, quarter)))


#: How many bytes of frames may wait to be written before new frames are
#: skipped. A budget in BYTES, not frames: an EVK4 window is under 1 MB and a
#: full-frame boA5320 frame is about 49 MB, so a frame count that suited one
#: would be either useless or gigabytes for the other.
WRITE_BUDGET_BYTES = _write_budget()

#: How long stopping a recording waits for queued frames to reach the disk.
DRAIN_TIMEOUT = 30.0

#: How many frames are encoded at once. The heavy work (numpy copies, zlib,
#: the file write) releases the GIL, so threads scale: fast_png encoded 266
#: Mono8 / 141 16-bit boA5320 frames a second on 8 threads (measured).
#: Up to 8 — a machine with cores to spare should spend them here rather
#: than report frames "NOT saved" — leaving one core for the camera and one
#: for the window.
DEFAULT_WRITERS = max(1, min(8, (os.cpu_count() or 2) - 2))


class FrameWriter:
    """Saves frames on its OWN threads, so saving can never stall the camera.

    WHY THIS EXISTS — MEASURED ON A REAL EVK4
    Saving used to happen inline in the grab loop. Pointed at a network share
    it made capture crawl, and it did worse than slow things down: while a PNG
    was being written the camera kept producing events, they piled up in the
    SDK, and the NEXT window swept them all in — so pictures stopped meaning
    "20 ms of camera time". The grab loop now hands frames here and goes
    straight back to the camera.

    SEVERAL WRITERS, ONE ORDER
    One writer thread could not keep up with a busy EVK4 scene (14 PNGs a
    second measured, against 50 windows a second), so `threads` writers take
    frames off one queue and encode them at the same time. They FINISH out of
    order, but nothing downstream sees that: a finished frame waits until
    every frame grabbed before it is done, and only then is it counted, given
    its CSV row and added to `paths`. The slider, the CSV and the counts are
    in grab order exactly as with one writer.

    WHEN STORAGE CANNOT KEEP UP, FRAMES ARE SKIPPED — AND COUNTED
    The queue has a byte budget. Past it, new frames are not saved and
    `skipped` says how many. That is a deliberate change from "never drop a
    recorded frame": stalling the grab loop instead does not save them
    either — with LatestImageOnly the camera just discards them, silently.
    A counted skip is the honest version of the same loss.

    THE FIRST WRITE FAILURE STOPS IT, as before: a capture with holes that
    nothing reports is worse than one that ended. Frames already being written
    by the other writers still land and are counted; nothing new starts.
    `failed` names the frame ("frame 20 not saved — FileExistsError: ..."),
    and `lost` counts what the failure cost: that frame and those queued
    behind it.

    THE RECORDING'S OWN TOTALS. The session tells it of every failed grab
    while it is the recording (note_failed_grab), and it counts every frame
    it is offered; when the last frame is on disk it hands them to the
    recorder's end record (Recorder.finish).
    """

    def __init__(self, recorder: Recorder,
                 budget_bytes: int = WRITE_BUDGET_BYTES,
                 threads: int = DEFAULT_WRITERS):
        self.recorder = recorder
        self.budget = int(budget_bytes)
        self.written = 0
        self.skipped = 0
        self.failed = ""
        #: Frames accepted and then NOT saved because a write failed: the
        #: one whose write failed, those queued behind it, and any offered
        #: after it. Stats.unsaved.
        self.lost = 0
        #: Failed grabs while this was the recording (note_failed_grab) —
        #: the frames CSV's running grab_failures. Counted only for a
        #: camera that reports them (set by CaptureSession.record_to).
        self.grab_failures = 0
        self.counts_grab_failures = False
        #: Snapshot frames that came between triggers (note_passed): the
        #: camera's, never asked for, so not offered — for the end record.
        self.passed = 0
        #: Frames offered (submit), and the first and last frame number
        #: offered or failed, for the end record.
        self.offered = 0
        self.first_index: Optional[int] = None
        self.last_index: Optional[int] = None
        #: Every file written, in GRAB order — append-only, so a viewer can
        #: pick up new frames without rescanning a folder of thousands.
        self.paths: List[Path] = []
        self._queue: collections.deque = collections.deque()
        self._queued_bytes = 0
        self._inflight = 0          # taken off the queue, not yet published
        self._closing = False
        self._cond = threading.Condition()
        self._submitted = 0         # sequence number of the next frame
        self._published = 0         # sequence number of the next to publish
        #: Finished frames waiting for an earlier one: seq -> (stub, path),
        #: path None for a frame that was not saved. The stub is the frame
        #: WITHOUT its image: measured, one stalled write let the other
        #: writers park 796 MB of finished images here against a 16 MB
        #: budget, because nothing was bounding them.
        self._finished: Dict[int, Any] = {}
        #: One writer at a time writes index rows, outside the lock, in order.
        self._publishing = False
        self._in_publish = 0
        self._running = max(1, int(threads))
        self._threads = [threading.Thread(target=self._run, daemon=True,
                                          name=f"capture-writer-{i + 1}")
                         for i in range(self._running)]
        for thread in self._threads:
            thread.start()

    @property
    def pending(self) -> int:
        """Frames not yet counted as saved: queued, being written, or written
        and waiting for an earlier one to be published."""
        with self._cond:
            return (len(self._queue) + self._inflight + len(self._finished)
                    + self._in_publish)

    @property
    def alive(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def submit(self, frame: cameras.Frame) -> bool:
        """Queue `frame` for saving. False if it will not be saved."""
        size = int(getattr(frame.image, "nbytes", 0) or 0)
        with self._cond:
            if self._closing:
                return False
            self.offered += 1
            self._saw_index(frame.index)
            if self.failed:
                # Offered between the failure and the session noticing it.
                self.lost += 1
                return False
            # One frame bigger than the whole budget is still accepted when
            # nothing is waiting — otherwise a large enough camera could never
            # save anything at all.
            if self._queue and self._queued_bytes + size > self.budget:
                self.skipped += 1
                return False
            self._queue.append((self._submitted, frame))
            self._submitted += 1
            self._queued_bytes += size
            self._cond.notify()
        return True

    def note_failed_grab(self, index: Any = None) -> None:
        """A grab came back failed while this was the recording
        (cameras.GrabFailed; `index` the frame number it used up)."""
        with self._cond:
            self.grab_failures += 1
            self._saw_index(index)

    def note_passed(self) -> None:
        """A snapshot frame no trigger asked for went by (not offered)."""
        with self._cond:
            self.passed += 1

    def _saw_index(self, index: Any) -> None:
        """Called with the lock held."""
        try:
            number = int(index)
        except (TypeError, ValueError):
            return
        if self.first_index is None:
            self.first_index = number
        self.last_index = number

    def totals(self) -> Dict[str, Any]:
        """This recording's counts, for its end record (Recorder.finish)."""
        with self._cond:
            return {
                "frames_saved": self.written,
                "storage_too_slow": self.skipped,
                "write_failed": self.lost,
                "offered": self.offered,
                "grab_failures": (self.grab_failures
                                  if self.counts_grab_failures else None),
                "first_index": self.first_index,
                "last_index": self.last_index,
                "recording_failed": self.failed,
                "passed": self.passed,
            }

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closing and not self.failed:
                    self._cond.wait()
                if not self._queue:
                    # Nothing left for this writer. The LAST one out closes
                    # the index: by then every frame has been published.
                    self._running -= 1
                    last = self._running == 0
                    self._cond.notify_all()
                    break
                seq, frame = self._queue.popleft()
                self._queued_bytes -= int(getattr(frame.image, "nbytes", 0) or 0)
                self._inflight += 1
            path: Optional[Path] = None
            error = ""
            try:
                path = self.recorder.path_for(frame)
                self.recorder.save(frame, path)
            except Exception as exc:                        # noqa: BLE001
                path = None
                error = f"frame {frame.index} not saved — {_failure(exc)}"
            with self._cond:
                self._inflight -= 1
                self._finished[seq] = (_without_image(frame), path)
                if error:
                    self.lost += 1
                    if not self.failed:
                        self._fail(error)
                self._cond.notify_all()
            del frame                   # the image is saved; let it go
            self._publish()
        if last:
            self._close_recorder()

    def _fail(self, error: str) -> None:
        """Stop taking frames. Called with the lock held."""
        self.failed = error
        # Frames still queued will never be written; mark them so the ones
        # finished after them can still be published in order — and count
        # them, as grabbed and NOT saved.
        self.lost += len(self._queue)
        for seq, frame in self._queue:
            self._finished[seq] = (None, None)
        self._queue.clear()
        self._queued_bytes = 0

    def _publish(self) -> None:
        """Count, index and list finished frames in grab order.

        The index rows are written OUTSIDE the lock. Written under it, one
        slow flush of the CSV (a stalled share, an antivirus scan) blocked
        the grab loop and the UI thread for the whole flush — measured 1 s —
        because both take this lock to hand a frame over or read a count.
        One writer at a time publishes (`_publishing`), which keeps the rows
        in order without holding the lock across the file write.
        """
        while True:
            with self._cond:
                if self._publishing or self._published not in self._finished:
                    return
                batch = []
                while self._published in self._finished:
                    batch.append(self._finished.pop(self._published))
                    self._published += 1
                self._publishing = True
                self._in_publish = len(batch)
            error = ""
            for stub, path in batch:
                if path is None:
                    continue
                try:
                    self.recorder.log(stub, path)
                except Exception as exc:                    # noqa: BLE001
                    # The PNG is on disk but its row could not be written:
                    # the index would have a hole nobody sees. Same rule as
                    # a failed write.
                    error = error or (f"frame {stub.index}'s row in the "
                                      f"frames CSV not written — "
                                      f"{_failure(exc)}")
            flush = getattr(self.recorder, "flush_index", None)
            if callable(flush):
                flush()
            with self._cond:
                for stub, path in batch:
                    if path is not None:
                        self.written += 1
                        self.paths.append(path)
                self._in_publish = 0
                self._publishing = False
                if error and not self.failed:
                    self._fail(error)
                self._cond.notify_all()
            # Loop: frames that finished while this batch was being written
            # were left for us, since only one writer publishes at a time.

    def _close_recorder(self) -> None:
        """The last writer out: every frame is published (or the first
        failed write ended the recording). The end record, then the index."""
        finish = getattr(self.recorder, "finish", None)
        if callable(finish):
            try:
                finish(self.totals())
            except Exception:                               # noqa: BLE001
                pass
        closer = getattr(self.recorder, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:                               # noqa: BLE001
                pass

    def wait_empty(self, timeout: Optional[float] = None) -> bool:
        """Block until everything queued is on disk. False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while ((self._queue or self._inflight or self._finished
                    or self._publishing) and self._running):
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._cond.wait(left if left is not None else 0.1)
        return True

    def close(self, timeout: Optional[float] = DRAIN_TIMEOUT) -> bool:
        """Accept nothing more, finish what is queued, stop.

        Returns whether it finished inside `timeout`. If not, the writers
        carry on in the background and `pending` says how much is left — the
        caller should say so rather than claim the run is saved.
        """
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in self._threads:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(left)
        return not self.alive


def _failure(exc: BaseException) -> str:
    """A failed write, short enough for a status line that is one row:
    "FileExistsError: File exists (20261012_120000_frame_000020.png)" — the
    file's NAME, not its whole path (the run's folder is known)."""
    name = type(exc).__name__
    if isinstance(exc, OSError) and exc.strerror:
        where = (f" ({Path(str(exc.filename)).name})"
                 if exc.filename else "")
        return f"{name}: {exc.strerror}{where}"
    return f"{name}: {exc}"


def _without_image(frame: cameras.Frame) -> cameras.Frame:
    """What the index row needs, and nothing that holds the pixels."""
    return cameras.Frame(image=None, index=frame.index,
                         timestamp_us=frame.timestamp_us,
                         meta=dict(frame.meta or {}))


class CaptureSession:
    """A camera, a thread reading it, and the numbers that describe both."""

    def __init__(self, device: cameras.Device,
                 on_frame: Optional[Callable[[cameras.Frame], None]] = None,
                 on_stats: Optional[Callable[[Stats], None]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 timeout_ms: int = 1000):
        self.device = device
        self.on_frame = on_frame
        self.on_stats = on_stats
        self.timeout_ms = int(timeout_ms)
        self.mailbox = LatestFrame()
        self.recorder: Optional[Recorder] = None
        #: The writer for the recording in progress, or None.
        self.writer: Optional[FrameWriter] = None
        #: Every writer this session has had, so the counts survive Stop.
        self._writers: List[FrameWriter] = []
        #: Writers before this one belong to an earlier run (reset_stats).
        self._counted_from = 0
        #: The camera's numbers when the last recording ended (run_stats).
        self._ended_stats: Optional[Stats] = None
        self._clock = clock
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._lock = threading.RLock()   # re-entrant: _took holds it across _record
        self._stats = Stats()
        self._marks: List[float] = []
        #: When the last frame arrived (the clock's time) — or the stream
        #: started, before the first; None before any start.
        self._last_frame_at: Optional[float] = None
        #: Seconds between the last two frames of this stream, or None.
        #: The measured RATE cannot say how often a slow camera sends: under
        #: one frame a RATE_WINDOW it reads 0.
        self._interval: Optional[float] = None
        #: The last frame time is the stream's start, not a frame's.
        self._from_start = False
        #: A recording that STOPPED ON A FAILED WRITE and that the app has
        #: not ended yet (record_to): every frame grabbed meanwhile is a
        #: frame of that run NOT saved (Stats.unsaved), not a preview frame.
        self._broken: Optional[FrameWriter] = None
        #: What a failed write does to the RECORDING. False (Typhon): the
        #: run goes on — the camera, and frame_camera's capture — and every
        #: frame grabbed until the app ends it (Stop) is counted NOT saved.
        #: True (the Council's Capture tab, where recording is a toggle
        #: beside a live view that goes on): the recording ENDS at the
        #: failure, and the count stops there — the frame that failed and
        #: those queued behind it. That tab counted "NOT saved (recording
        #: stopped)" for every live frame after a failed write, until the
        #: next Record or Disconnect.
        self.end_recording_on_failure = False
        #: A TRIGGERED recording's books (council_core.triggered.
        #: TriggerRun), set with the recorder (record_to): asked on the grab
        #: thread which trigger each frame belongs to — and, in snapshot
        #: mode, whether any trigger asked for it at all. None otherwise:
        #: every frame is offered, as always.
        self.gate: Any = None

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def reset_stats(self) -> None:
        """Start counting afresh — for a new run, or a preview.

        Earlier writers are still waited for on close (their frames are the
        user's data); they just stop counting towards this run.
        """
        with self._lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        self._stats = Stats()
        self._marks = []
        self.mailbox.dropped = 0
        self._counted_from = len(self._writers)
        self._ended_stats = None

    def quiet_for(self) -> float:
        """Seconds since the camera last sent a frame (since the stream
        started, before the first one); 0 when it is not running.

        A camera that stops sending raises nothing — a Basler waiting for a
        trigger just times out its reads — so this is how the live line
        knows (measured on the emulator: TriggerMode On, no frame for 6 s,
        and the line still said "62.5 fps")."""
        last = self._last_frame_at
        if last is None or not self.running:
            return 0.0
        return max(0.0, self._clock() - last)

    def quiet(self, least: float = RATE_WINDOW) -> float:
        """quiet_for() once it is longer than `least` AND than
        QUIET_INTERVALS of the camera's own last measured frame intervals
        (a camera running at 0.2 fps is not quiet 2 s after a frame);
        else 0."""
        gone = self.quiet_for()
        step = self._interval
        enough = max(least, QUIET_INTERVALS * step) if step else least
        return gone if gone > enough else 0.0

    def stats(self) -> Stats:
        writers = list(self._writers)[self._counted_from:]
        failed = next((w.failed for w in writers if w.failed), "")
        quiet = self.quiet()
        with self._lock:
            base = self._stats
        if base.rate > 0 and quiet:
            # THE RATE IS MEASURED FROM FRAMES THAT ARRIVE: with none
            # arriving the sliding window is never updated, and the last
            # rate stood for a camera that had stopped. Several of its own
            # intervals with nothing is not that rate any more.
            base = replace(base, rate=0.0)
        return replace(
            base, dropped=self.mailbox.dropped,
            recorded=sum(w.written for w in writers),
            skipped=sum(w.skipped for w in writers),
            waiting=sum(w.pending for w in writers),
            unsaved=base.unsaved + sum(w.lost for w in writers),
            recording_failed=base.recording_failed or failed)

    def record_to(self, recorder: Optional[Recorder],
                  drain_timeout: Optional[float] = DRAIN_TIMEOUT,
                  reset: bool = False, gate: Any = None) -> bool:
        """Start writing frames to disk, or stop and let what is queued land.

        Called while running: the grab loop picks it up on its next frame.
        Returns False only when stopping, if frames were still waiting to be
        written when `drain_timeout` ran out — they carry on in the
        background, and `stats().waiting` says how many.

        `reset` starts the counts afresh IN THE SAME STEP as the switch. With
        the camera already streaming (an EVK4 going from preview to capture)
        doing the two separately let a frame land between them, counted as
        grabbed and never recorded — measured: "11 grabbed, 10 saved".

        `gate`, with a recorder: a triggered run's books (see `self.gate`),
        switched in the same step. Stopping drops it.
        """
        finished = True
        writer = None
        if recorder is not None:
            recorder.open()
            writer = FrameWriter(recorder)
            # A 0 from a camera that cannot report failed grabs would claim
            # a count nobody made: its CSV column and end record say nothing.
            writer.counts_grab_failures = bool(
                getattr(self.device, "counts_grab_failures", False))
        with self._lock:
            # The grab loop records and counts each frame under this lock,
            # so it sees either the old writer and old counts or the new.
            if reset:
                self._reset_locked()
            old = self.writer
            broken, self._broken = self._broken, None
            self.gate = gate if writer is not None else None
            if writer is not None:
                self._writers.append(writer)
                self.writer = writer
                self._ended_stats = None
            else:
                self.writer = None
                if old is not None or broken is not None:
                    # Frozen HERE: a camera that keeps streaming after the
                    # recording (an EVK4) would otherwise count frames
                    # grabbed after Stop as grabbed-and-not-saved. A
                    # recording that stopped on a failed write is frozen
                    # here too, NOT at the failure: the frames grabbed in
                    # between are the run's, and were not saved.
                    self._ended_stats = self.stats()
            self.recorder = recorder
        if old is not None:
            finished = old.close(drain_timeout)
        if broken is not None and broken is not old:
            # Its failed write already stopped its threads; wait for the
            # last one to write the end record and close the CSV, so the
            # run is whole on disk when the app's Stop returns.
            finished = broken.close(drain_timeout) and finished
        return finished

    def run_stats(self) -> Stats:
        """The latest recording's numbers: the camera's counts as they were
        when it ended, the saving counts as they are now (frames may still
        be landing). While recording, or with none ended, `stats()`."""
        live = self.stats()
        frozen = self._ended_stats
        if frozen is None or self.writer is not None:
            return live
        return replace(frozen, recorded=live.recorded, skipped=live.skipped,
                       waiting=live.waiting,
                       unsaved=max(frozen.unsaved, live.unsaved),
                       recording_failed=live.recording_failed)

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Wait until every queued frame is on disk. False on timeout."""
        writer = self.writer
        return writer.wait_empty(timeout) if writer is not None else True

    def written(self, start: int = 0) -> List[Path]:
        """Files the latest recording has saved, from position `start` on.

        The latest, not only the running one: after Stop its last frames may
        still be landing, and a viewer following the run wants those too.
        """
        writers = self._writers
        return writers[-1].paths[start:] if writers else []

    @property
    def saving(self) -> bool:
        """Frames are still on their way to disk."""
        return any(w.pending for w in list(self._writers))

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        # Before the thread, not inside it: see warm_imports.
        warm_imports()
        self.device.start()
        self._last_frame_at = self._clock()
        self._interval = None
        self._from_start = True
        self._thread = threading.Thread(target=self._loop, name="capture",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT) -> bool:
        """Ask the loop to finish and WAIT for it.

        Returns whether the thread actually ended. The caller needs that
        answer: tearing down the window while a grab thread is still running
        is how a capture app crashes on exit.
        """
        self._stopping.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                return False
        self._thread = None
        try:
            self.device.stop()
        except Exception as exc:                            # noqa: BLE001
            self._note_error(f"stopping: {exc}")
        return True

    def close(self) -> None:
        self.stop()
        # The frames already grabbed are the user's data; let them land —
        # including any a quick Stop left saving in the background.
        self.record_to(None)
        for writer in list(self._writers):
            writer.close(DRAIN_TIMEOUT)
        try:
            self.device.close()
        except Exception:                                   # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        misses = 0
        while not self._stopping.is_set():
            try:
                frame = self.device.read(self.timeout_ms)
            except cameras.CameraEnded as exc:
                # NOT recoverable. The camera is gone or the recording is
                # over, so every further read raises the same thing.
                self._note_error(str(exc))
                break
            except cameras.CameraError as exc:
                # One bad grab is not a reason to end a capture — but an
                # endless run of them is. Without this cap a fault that
                # repeats every read spins the loop as fast as the CPU
                # allows, which is how a finished stream logged 737,070
                # errors in six seconds.
                misses += 1
                if isinstance(exc, cameras.GrabFailed):
                    # An exposure that never arrived: counted on its own,
                    # with its place in the run (Stats.grab_failures, the
                    # frames CSV, the end record) — and ONLY there, not
                    # among errors too: "3 failed grabs · 3 error(s)" read
                    # as six faults (measured). Still the last error said.
                    self._count_failed_grab(exc)
                    self._note_error(str(exc), count=False)
                else:
                    self._note_error(str(exc))
                if misses >= MAX_CONSECUTIVE_ERRORS:
                    self._note_error(
                        f"giving up after {misses} failed reads in a row: "
                        f"{exc}")
                    break
                continue
            except Exception as exc:                        # noqa: BLE001
                # An SDK raising something unexpected ends the loop rather
                # than spinning on it forever, but it says so first.
                self._note_error(f"{type(exc).__name__}: {exc}")
                break
            if frame is None:
                continue
            misses = 0
            self._took(frame)

    def _took(self, frame: cameras.Frame) -> None:
        now, last = self._clock(), self._last_frame_at
        if last is not None and not self._from_start:
            self._interval = max(0.0, now - last)
        self._last_frame_at, self._from_start = now, False
        with self._lock:
            # Recorded and counted as one step (see record_to's `reset`).
            # submit() only queues, so the lock is held for microseconds.
            self._record(frame)
            lost = int((frame.meta or {}).get("skipped_by_camera", 0) or 0)
            self._stats = replace(
                self._stats, grabbed=self._stats.grabbed + 1,
                camera_skipped=self._stats.camera_skipped + lost,
                rate=self._rate())
        self.mailbox.put(frame)
        if self.on_frame is not None:
            self.on_frame(frame)
        if self.on_stats is not None:
            self.on_stats(self.stats())

    def _record(self, frame: cameras.Frame) -> bool:
        """Hand `frame` to the writer. Never touches the disk itself."""
        writer = self.writer
        if writer is None:
            if self._broken is not None:
                # A frame of a run whose recording stopped on a failed write
                # and that the app has not ended: grabbed, NOT saved.
                with self._lock:
                    self._stats = replace(self._stats,
                                          unsaved=self._stats.unsaved + 1)
            return False
        if writer.failed:
            # Recording STOPS on the first failure. Carrying on would leave a
            # capture with holes in it that nothing reports, and a run with a
            # hole is worse than a run that ended.
            said = writer.failed
            self.writer = None
            self.recorder = None
            if self.end_recording_on_failure:
                # ENDED HERE (end_recording_on_failure): this frame came
                # after the recording, and is not counted against it. The
                # writer finishes on its own threads after a failure — its
                # end record and index close there — so nothing waits here.
                with self._lock:
                    self._stats = replace(
                        self._stats, recording_failed=said,
                        last_error=f"recording stopped: {said}",
                        errors=self._stats.errors + 1)
                    self._ended_stats = self.stats()
                return False
            # NOT frozen here (record_to freezes it when the app ends the
            # recording): until then every frame grabbed is the run's and
            # is counted NOT saved — this one first. Frozen here, a Stop a
            # minute later said "20 grabbed" for a run that grabbed 1,800
            # (measured: 20 for 62), and nothing said the rest were lost.
            self._broken = writer
            with self._lock:
                self._stats = replace(self._stats, recording_failed=said,
                                      last_error=f"recording stopped: {said}",
                                      errors=self._stats.errors + 1,
                                      unsaved=self._stats.unsaved + 1)
            return False
        gate = self.gate
        if gate is not None:
            # A TRIGGERED RUN: which trigger this frame is for — or, in
            # snapshot mode, none (a frame between triggers: grabbed and
            # shown, never asked for, not saved, and not a loss).
            try:
                tags = gate.tag(frame)
            except Exception as exc:                        # noqa: BLE001
                tags = {"trigger": "", "trigger_source":
                        f"error: {type(exc).__name__}"}
            if tags is None:
                with self._lock:
                    self._stats = replace(self._stats,
                                          passed=self._stats.passed + 1)
                writer.note_passed()
                return False
            meta = frame.meta if frame.meta is not None else {}
            meta.update(tags)
            frame.meta = meta
        if writer.counts_grab_failures:
            # The running count goes on the frame's CSV row: this
            # recording's own failed grabs so far (a recording can begin in
            # a stream already running). Only for a camera that reports
            # failed grabs: a 0 from one that cannot would claim a count
            # nobody made.
            meta = frame.meta if frame.meta is not None else {}
            meta["grab_failures"] = writer.grab_failures
            frame.meta = meta
        return writer.submit(frame)

    def _count_failed_grab(self, exc: Any = None) -> None:
        with self._lock:
            self._stats = replace(
                self._stats, grab_failures=self._stats.grab_failures + 1)
            writer = self.writer
            if writer is not None and not writer.failed:
                writer.note_failed_grab(getattr(exc, "index", None))
                gate = self.gate
                if gate is not None:
                    try:
                        gate.failed_grab(getattr(exc, "index", None))
                    except Exception:                       # noqa: BLE001
                        pass

    def _rate(self) -> float:
        """Frames per second over the last `RATE_WINDOW` seconds.

        MEASURED, over a sliding window. An average since start climbs slowly
        and then never falls, so a camera that stopped delivering still reads
        as healthy for minutes.
        """
        now = self._clock()
        self._marks.append(now)
        cutoff = now - RATE_WINDOW
        while self._marks and self._marks[0] < cutoff:
            self._marks.pop(0)
        if len(self._marks) < 2:
            return 0.0
        span = self._marks[-1] - self._marks[0]
        return (len(self._marks) - 1) / span if span > 0 else 0.0

    def _note_error(self, said: str, count: bool = True) -> None:
        """`said` is the last error; `count` adds it to `errors` (not for a
        failed grab, which is counted in grab_failures alone)."""
        with self._lock:
            self._stats = replace(
                self._stats, errors=self._stats.errors + (1 if count else 0),
                last_error=said)
        if self.on_stats is not None:
            self.on_stats(self.stats())
