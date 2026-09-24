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

  RECORDED frames may not be dropped. That is data loss, and a capture that
  quietly loses a third of its frames is worse than one that refuses to start.
  So recording happens inline in the grab loop: if the disk cannot keep up,
  the measured rate falls and the user SEES it fall, which is the honest
  failure. A background writer with a queue would hide the same problem until
  the queue blew up.

STOP IS BOUNDED AND JOINED
`stop()` asks the loop to finish, waits, and returns whether it actually
ended. A capture app whose Stop button returns while a thread is still pushing
frames into a closing window is the standard way to get a crash on exit.

NOTHING HERE IMPORTS A TOOLKIT
The session hands frames to a callback. Who marshals them onto a UI thread is
the view's problem, not this module's.
"""
from __future__ import annotations

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

    def line(self) -> str:
        """The one line a status bar shows.

        The drop count is always present, including at zero. A number that
        only appears once it is bad is a number nobody is watching when it
        goes bad.
        """
        bits = [f"{self.rate:.1f} fps", f"{self.grabbed} grabbed",
                f"{self.dropped} dropped"]
        if self.recorded:
            bits.append(f"{self.recorded} saved")
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
        Image.fromarray(data).save(str(path))
        return
    if data.ndim == 2 and data.dtype != np.uint8:
        data = np.clip(data, 0, 255).astype(np.uint8)
    Image.fromarray(data).save(str(path))


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
                 suffix: Optional[str] = None):
        self.dir = Path(out_dir)
        self.stem = str(stem) or "frame"
        self.written = 0
        self._writer = writer or write_image
        self._suffix = suffix if suffix is not None else (
            ".png" if writer is None else "")

    def open(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        warm_imports()

    def write(self, frame: cameras.Frame) -> Path:
        path = self.dir / f"{self.stem}_{frame.index:06d}{self._suffix}"
        self._writer(frame.image, path)
        self.written += 1
        return path


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
        self._clock = clock
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._stats = Stats()
        self._marks: List[float] = []

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def stats(self) -> Stats:
        with self._lock:
            return replace(self._stats, dropped=self.mailbox.dropped)

    def record_to(self, recorder: Optional[Recorder]) -> None:
        """Start or stop writing frames to disk.

        Called while running: the grab loop picks it up on its next frame.
        """
        if recorder is not None:
            recorder.open()
        self.recorder = recorder

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
        recorded = self._record(frame)
        with self._lock:
            self._stats = replace(
                self._stats, grabbed=self._stats.grabbed + 1,
                recorded=self._stats.recorded + (1 if recorded else 0),
                rate=self._rate())
        self.mailbox.put(frame)
        if self.on_frame is not None:
            self.on_frame(frame)
        if self.on_stats is not None:
            self.on_stats(self.stats())

    def _record(self, frame: cameras.Frame) -> bool:
        recorder = self.recorder
        if recorder is None:
            return False
        try:
            recorder.write(frame)
            return True
        except Exception as exc:                            # noqa: BLE001
            # Recording STOPS on the first failure. Carrying on would leave a
            # capture with holes in it that nothing reports, and a run with a
            # hole is worse than a run that ended.
            said = f"{type(exc).__name__}: {exc}"
            self.recorder = None
            with self._lock:
                self._stats = replace(self._stats, recording_failed=said,
                                      last_error=f"recording stopped: {said}",
                                      errors=self._stats.errors + 1)
            return False

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
