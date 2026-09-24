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
from typing import Any, Callable, Dict, List, Optional

from . import cameras

#: How long a stop() waits for the grab loop before reporting failure.
STOP_TIMEOUT = 3.0

#: The window the measured rate is averaged over, in seconds.
RATE_WINDOW = 1.0

#: How many consecutive failed reads end a capture. A backstop for any fault
#: that repeats every time rather than clearing.
MAX_CONSECUTIVE_ERRORS = 50


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

    def line(self, dropped_label: str = "dropped") -> str:
        """The one line a status bar shows.

        The drop count is always present, including at zero. A number that
        only appears once it is bad is a number nobody is watching when it
        goes bad. `dropped_label` lets a view say what it means in its own
        words ("not drawn (screen only)" in Typhon, where "dropped" was read
        as frames lost).
        """
        bits = [f"{self.rate:.1f} fps", f"{self.grabbed} grabbed",
                f"{self.dropped} {dropped_label}"]
        if self.recorded:
            bits.append(f"{self.recorded} saved")
        if self.waiting:
            bits.append(f"{self.waiting} waiting to save")
        if self.skipped:
            bits.append(f"{self.skipped} NOT saved (storage too slow)")
        if self.errors:
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


#: zlib level for saved PNGs. 1, not Pillow's default 6: measured on EVK4
#: windows (1280x720, busy scattered scene) level 6 managed 14 PNGs a second
#: on one thread and level 1 managed 61 — and PNG is lossless at every level,
#: so the pixels are identical; only the file is bigger (about 2x at worst).
PNG_COMPRESS_LEVEL = 1


def write_image(image: Any, path: Path) -> None:
    """A frame as an image file the rest of the Barbie toolchain can read.

    PNG, because `frame_roi`, `frame_timing` and `frame_classes` all discover
    frames by IMAGE_SUFFIXES and open them with Pillow. A capture written as
    .npy is invisible to every one of them.

    THE PIXELS ARE WRITTEN AS THE CAMERA PRODUCED THEM. A 12-bit Basler frame
    arrives as uint16 holding 0..4095, and it is saved as a 16-bit PNG holding
    0..4095 -- verified lossless round-trip. Rescaling it to fill the 16-bit
    range would make the picture look right and the MEASUREMENT wrong, and
    this is a capture app: the file is the data.

    (Known consequence, deliberately not papered over: frame_classes.thumbnail
    scales 16-bit input by 1/256 on the assumption it fills the full range, so
    a 12-bit frame reads dark to the classifier. That is a conversion bug in
    the reader, not a reason for the writer to alter the user's pixels.)
    """
    from PIL import Image

    import numpy as np
    data = np.asarray(image)
    if data.ndim == 2 and data.dtype == np.uint16:
        # NO mode= argument. Pillow infers "I;16" from the dtype, and passing
        # the mode explicitly is deprecated for removal in Pillow 13
        # (2026-10-15) -- which would break capture outright, not warn.
        Image.fromarray(data).save(str(path), compress_level=PNG_COMPRESS_LEVEL)
        return
    if data.ndim == 2 and data.dtype != np.uint8:
        data = np.clip(data, 0, 255).astype(np.uint8)
    Image.fromarray(data).save(str(path), compress_level=PNG_COMPRESS_LEVEL)


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
    """
    try:
        import numpy                                       # noqa: F401
    except Exception:                                      # noqa: BLE001
        pass
    try:
        from PIL import Image
        Image.init()
    except Exception:                                      # noqa: BLE001
        pass


#: The columns of a run's frame index (Recorder `index_name`). window_us is
#: how long each event-camera picture collected events — the raw view uses
#: the same window, whatever frame rate the run was captured at.
INDEX_COLUMNS = ("file", "index", "timestamp_us", "raw_t_us", "events",
                 "window_us")


class Recorder:
    """Frames to disk, one file each, numbered in grab order.

    NUMBERED BY GRAB INDEX, NOT BY A COUNTER OF ITS OWN. A recorder that
    numbers its own output 0,1,2 produces a tidy sequence that silently hides
    any frame the camera never delivered; naming files after the frame index
    means a gap in the filenames is a gap in the capture, visible in a
    directory listing.
    """

    def __init__(self, out_dir: Any, stem: str = "frame",
                 writer: Optional[Callable[[Any, Path], None]] = None,
                 suffix: Optional[str] = None,
                 index_name: Optional[str] = None):
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

    def open(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        warm_imports()
        if self.index_path is not None and self._index_file is None:
            fresh = not self.index_path.exists()
            # APPEND, never truncate: the name is per run, but a file that is
            # somehow already there is the user's data.
            self._index_file = open(self.index_path, "a", newline="",
                                    encoding="utf-8")
            self._index_rows = csv.writer(self._index_file)
            if fresh:
                self._index_rows.writerow(INDEX_COLUMNS)

    def path_for(self, frame: cameras.Frame) -> Path:
        return self.dir / f"{self.stem}_{frame.index:06d}{self._suffix}"

    def save(self, frame: cameras.Frame, path: Path) -> None:
        """Encode and write one frame. Safe to call from several threads at
        once: it touches nothing but its own file."""
        self._writer(frame.image, path)

    def log(self, frame: cameras.Frame, path: Path) -> None:
        """Count a saved frame and give it its index row. NOT thread-safe:
        FrameWriter calls it one frame at a time, in grab order."""
        self.written += 1
        if self._index_rows is not None:
            meta = frame.meta or {}
            window = meta.get("window_ms")
            self._index_rows.writerow((
                path.name, frame.index, frame.timestamp_us,
                meta.get("raw_t_us", ""), meta.get("events", ""),
                int(round(float(window) * 1000)) if window else ""))

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

    def close(self) -> None:
        """Finish the index. Safe to call twice."""
        handle, self._index_file, self._index_rows = self._index_file, None, None
        if handle is not None:
            try:
                handle.close()
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

#: How many frames are encoded at once. PNG encoding releases the GIL, so
#: threads scale: measured on EVK4 windows, 4 writers were 3.6x one writer.
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
    """

    def __init__(self, recorder: Recorder,
                 budget_bytes: int = WRITE_BUDGET_BYTES,
                 threads: int = DEFAULT_WRITERS):
        self.recorder = recorder
        self.budget = int(budget_bytes)
        self.written = 0
        self.skipped = 0
        self.failed = ""
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
            if self.failed or self._closing:
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
                error = f"{type(exc).__name__}: {exc}"
            with self._cond:
                self._inflight -= 1
                self._finished[seq] = (_without_image(frame), path)
                if error and not self.failed:
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
        # finished after them can still be published in order.
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
                    error = error or f"{type(exc).__name__}: {exc}"
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

    def stats(self) -> Stats:
        writers = list(self._writers)[self._counted_from:]
        failed = next((w.failed for w in writers if w.failed), "")
        with self._lock:
            base = self._stats
        return replace(
            base, dropped=self.mailbox.dropped,
            recorded=sum(w.written for w in writers),
            skipped=sum(w.skipped for w in writers),
            waiting=sum(w.pending for w in writers),
            recording_failed=base.recording_failed or failed)

    def record_to(self, recorder: Optional[Recorder],
                  drain_timeout: Optional[float] = DRAIN_TIMEOUT,
                  reset: bool = False) -> bool:
        """Start writing frames to disk, or stop and let what is queued land.

        Called while running: the grab loop picks it up on its next frame.
        Returns False only when stopping, if frames were still waiting to be
        written when `drain_timeout` ran out — they carry on in the
        background, and `stats().waiting` says how many.

        `reset` starts the counts afresh IN THE SAME STEP as the switch. With
        the camera already streaming (an EVK4 going from preview to capture)
        doing the two separately let a frame land between them, counted as
        grabbed and never recorded — measured: "11 grabbed, 10 saved".
        """
        finished = True
        writer = None
        if recorder is not None:
            recorder.open()
            writer = FrameWriter(recorder)
        with self._lock:
            # The grab loop records and counts each frame under this lock,
            # so it sees either the old writer and old counts or the new.
            if reset:
                self._reset_locked()
            old = self.writer
            if writer is not None:
                self._writers.append(writer)
                self.writer = writer
                self._ended_stats = None
            else:
                self.writer = None
                if old is not None:
                    # Frozen HERE: a camera that keeps streaming after the
                    # recording (an EVK4) would otherwise count frames
                    # grabbed after Stop as grabbed-and-not-saved.
                    self._ended_stats = self.stats()
            self.recorder = recorder
        if old is not None:
            finished = old.close(drain_timeout)
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
        with self._lock:
            # Recorded and counted as one step (see record_to's `reset`).
            # submit() only queues, so the lock is held for microseconds.
            self._record(frame)
            self._stats = replace(
                self._stats, grabbed=self._stats.grabbed + 1,
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
            return False
        if writer.failed:
            # Recording STOPS on the first failure. Carrying on would leave a
            # capture with holes in it that nothing reports, and a run with a
            # hole is worse than a run that ended.
            said = writer.failed
            self.writer = None
            self.recorder = None
            with self._lock:
                self._stats = replace(self._stats, recording_failed=said,
                                      last_error=f"recording stopped: {said}",
                                      errors=self._stats.errors + 1)
                # The recording ended here, not at Stop: freeze its counts,
                # or a camera that keeps streaming (an EVK4) would count
                # every later frame as grabbed-and-not-saved.
                self._ended_stats = self.stats()
            return False
        return writer.submit(frame)

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

    def _note_error(self, said: str) -> None:
        with self._lock:
            self._stats = replace(self._stats, errors=self._stats.errors + 1,
                                  last_error=said)
        if self.on_stats is not None:
            self.on_stats(self.stats())
