"""
council_core.event_playback — an event camera's .raw recording as a sequence
of pictures a slider can jump around in.

A .raw is every event the sensor sent, in Prophesee's own format. Showing it
means binning events into fixed windows — the same 20 ms pictures the live
view and the PNGs are — and the slider needs window k at any k, in any order.

WHY NOT METAVISION'S OWN READERS (measured on OpenEB 5.2)
  * They cannot seek backward: RawReader raises "cannot seek backward in RAW
    file", and a forward seek decodes everything in between (316 ms to reach
    9.5 s into a 20M-event file). A slider dragged backwards would reopen the
    file every time.
  * The index that would make seeking fast is C++ only. The Python bindings do
    not expose seek(), but a default open still BUILDS the index — and writes
    it as "<name>.raw.tmp_index" into the user's capture folder.
  * A RawReader dropped without a `with` block is never freed: it keeps the
    .raw locked (so "copy the run to the NAS later" fails) and leaks ~190 MB
    per reopen. Thirty reopens cost 5.8 GB.
  * metavision_core cannot be imported without h5py.

SO THIS MAKES ONE PASS, WITH THE HAL ONLY
The file is opened with build_index=False (nothing is written beside it) and
read once, front to back, on a worker thread — the same poll/decode loop the
live camera uses. Each window is rendered exactly as the live view renders it
(cameras.accumulate_events), and only the pixels that CHANGED are kept: 4
bytes each, appended to a temporary file outside the capture folder. After
that, window k is one read and one fill — well under a millisecond — at any k,
in any order. The pass runs at tens of millions of events a second, and the
windows it has finished are usable while it carries on, so a long recording
opens at once and fills in.

TIME
Windows are counted from the CAPTURE'S ORIGIN — the first event the live view
saw after the .raw began — which each PNG's row in the frames CSV is measured
from (raw_t_us). That is what lets a viewer swap from a PNG to the raw at the
same moment. Given the origin (`origin_us`, the camera's clock), the file is
read on the camera's clock too: unshifted, with the EVT3 24-bit wrap put back
(an EVT3 file decodes k x 16.78 s behind the live view, measured). This
matters for a .raw started while the stream was already running: a replay
drops the events before the file's first time marker (up to 4.1 ms), so
anchoring at the file's first event would put every window up to 4 ms off.
Without an origin (a run with no CSV) the file is read time-shifted and
windows start at its first event.

A FILE STILL BEING WRITTEN is readable only up to what has been flushed, and
the last window may be partial. Callers should open a .raw once it is closed.
"""
from __future__ import annotations

import csv
import os
import shutil
import tempfile
import threading
import time
from bisect import bisect_left
from pathlib import Path
from typing import Any, List, Optional, Tuple

from . import cameras

#: The default window: the same as the live view's, so the raw view and the
#: PNGs look alike.
DEFAULT_WINDOW_US = int(cameras.DEFAULT_ACCUMULATE_MS * 1000)

#: How long close() waits for the reading thread before giving up on it.
CLOSE_TIMEOUT = 5.0

#: EVT3 timestamps are 24-bit: an EVT3 file decodes a whole number of these
#: behind the live camera's clock.
TIME_WRAP_US = 1 << 24


class RawUnavailable(RuntimeError):
    """The .raw cannot be read here — no SDK, or not a recording."""


def _hal() -> Any:
    try:
        import metavision_hal
    except ImportError as exc:
        raise RawUnavailable(
            "the raw view needs the Metavision SDK (metavision_hal), which "
            "is not installed for this Python") from exc
    return metavision_hal


class _Collector:
    """The decoder's callback target. It holds events only — never the
    device — so nothing forms a cycle that would keep the file open."""

    def __init__(self, np_mod: Any) -> None:
        self._np = np_mod
        self.batches: List[Any] = []

    def take(self, buffer: Any) -> None:
        taken = self._np.array(buffer, copy=True)
        if taken.size:
            self.batches.append(taken)

    def drain(self) -> List[Any]:
        got, self.batches = self.batches, []
        return got


class RawPlayback:
    """A .raw as `count` windows of `window_us`, each renderable by index.

    Starts reading at once, on its own thread. `count` grows until `done`;
    `error` says why it stopped early, if it did. Nothing calls back from that
    thread: a viewer polls `count` and `done` on its own timer, so no widget
    is ever touched off the UI thread. Always `close()` it: that stops the
    reader, releases the file and removes the temporary data.
    """

    def __init__(self, path: Any, window_us: int = DEFAULT_WINDOW_US,
                 hal: Any = None, np_mod: Any = None,
                 origin_us: Optional[int] = None):
        self.path = Path(str(path))
        self.window_us = max(1, int(window_us))
        #: The capture's origin on the camera's clock, or None.
        self.origin_us = None if origin_us is None else int(origin_us)
        self._hal_mod = hal
        self._np = np_mod or cameras._numpy()
        self.width = 0
        self.height = 0
        self.done = False
        self.error = ""
        self.events = 0
        #: (byte offset, value count) per finished window. Append-only.
        self._index: List[Tuple[int, int]] = []
        self._stop = threading.Event()
        self._opened = threading.Event()
        self._lock = threading.Lock()
        self._tmp = tempfile.mkdtemp(prefix="event_playback_")
        self._store = os.path.join(self._tmp, "windows.bin")
        self._out = open(self._store, "wb")
        self._in: Any = None
        self._written = 0
        self._thread = threading.Thread(target=self._read, daemon=True,
                                        name="raw-playback")
        self._thread.start()

    # ------------------------------------------------------------------
    @property
    def count(self) -> int:
        """Windows ready to show."""
        return len(self._index)

    @property
    def duration_us(self) -> int:
        return self.count * self.window_us

    def wait_open(self, timeout: Optional[float] = None) -> bool:
        """Block until the file is open (or failed). Mostly for tests."""
        return self._opened.wait(timeout)

    def wait_done(self, timeout: Optional[float] = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def index_at(self, t_us: float) -> int:
        """The window holding time `t_us`, counted from the first event."""
        return max(0, int(t_us) // self.window_us)

    def time_of(self, k: int) -> int:
        """Where window k starts, in µs from the first event."""
        return int(k) * self.window_us

    def frame(self, k: int) -> Any:
        """Window k as an image: mid-gray nothing, white positive, black
        negative — identical to the live view's picture of the same events."""
        np = self._np
        offset, n = self._index[int(k)]
        image = np.full((self.height, self.width), cameras.EVENT_MID,
                        dtype=np.uint8)
        if n:
            with self._lock:
                if self._in is None:
                    self._in = open(self._store, "rb")
                self._in.seek(offset)
                data = self._in.read(n * 4)
            values = np.frombuffer(data, dtype=np.int32)
            flat = image.reshape(-1)
            flat[values[values > 0] - 1] = 255
            flat[-values[values < 0] - 1] = 0
        return image

    def close(self) -> None:
        """Stop reading, let go of the file, remove the temporary data."""
        self._stop.set()
        self._thread.join(CLOSE_TIMEOUT)
        with self._lock:
            for handle in (self._in, self._out):
                try:
                    if handle is not None:
                        handle.close()
                except OSError:
                    pass
            self._in = None
        if not self._thread.is_alive():
            shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self) -> "RawPlayback":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # The one pass
    # ------------------------------------------------------------------
    def _read(self) -> None:
        np = self._np
        device = stream = decoder = cd = None
        try:
            hal = self._hal_mod or _hal()
            if not self.path.is_file():
                raise RawUnavailable(f"{self.path.name} does not exist")
            config = hal.RawFileConfig()
            # build_index=False: a default open writes "<name>.raw.tmp_index"
            # into the capture folder, and Python cannot use it anyway.
            config.build_index = False
            # On the camera's own clock when the capture's origin is known;
            # otherwise shifted to start near zero.
            config.do_time_shifting = self.origin_us is None
            try:
                device = hal.DeviceDiscovery.open_raw_file(str(self.path), config)
            except Exception as exc:                        # noqa: BLE001
                raise RawUnavailable(f"cannot open {self.path.name}: {exc}") from exc
            if device is None:
                raise RawUnavailable(f"cannot open {self.path.name}")
            geometry = device.get_i_geometry()
            self.width = int(geometry.get_width())
            self.height = int(geometry.get_height())
            stream = device.get_i_events_stream()
            decoder = device.get_i_events_stream_decoder()
            cd = device.get_i_event_cd_decoder()
            collector = _Collector(np)
            cd.add_event_buffer_callback(collector.take)
            self._opened.set()

            stream.start()
            origin: Optional[int] = None
            offset = 0
            carry = None
            next_k = 0
            while not self._stop.is_set():
                ready = stream.poll_buffer()
                if ready < 0:
                    break                       # the end of the file
                if ready == 0:
                    time.sleep(0.0005)
                    continue
                decoder.decode(stream.get_latest_raw_data())
                batches = collector.drain()
                if not batches:
                    continue
                events = batches[0] if len(batches) == 1 else np.concatenate(batches)
                if origin is None:
                    first = int(events["t"][0])
                    if self.origin_us is None:
                        origin = first
                    else:
                        origin = self.origin_us
                        # The whole number of 24-bit wraps between the file's
                        # clock and the camera's (0 for an EVT2 file).
                        offset = int(round((origin - first) / TIME_WRAP_US)) * TIME_WRAP_US
                if offset:
                    events = events.copy()
                    events["t"] = events["t"].astype(np.int64) + offset
                before = events["t"] < origin
                if before.any():
                    # Only possible if the file began before the origin the
                    # CSV records; those events belong to no window.
                    events = events[~before]
                    if not len(events):
                        continue
                if carry is not None and len(carry):
                    events = np.concatenate([carry, events])
                ks = (events["t"].astype(np.int64) - origin) // self.window_us
                last = int(ks[-1])
                # Every window before the newest is complete: the stream is
                # in time order.
                next_k = self._emit(events, ks, next_k, last)
                carry = events[ks >= last]
            if carry is not None and len(carry) and not self._stop.is_set():
                ks = (carry["t"].astype(np.int64) - origin) // self.window_us
                self._emit(carry, ks, next_k, int(ks[-1]) + 1)
        except Exception as exc:                            # noqa: BLE001
            self.error = str(exc) or type(exc).__name__
        finally:
            self._opened.set()
            if stream is not None:
                try:
                    stream.stop()
                except Exception:                           # noqa: BLE001
                    pass
            # Drop every SDK object on this thread, so the file is released
            # as soon as the pass is over rather than whenever GC gets to it.
            del device, stream, decoder, cd
            try:
                self._out.flush()
            except (OSError, ValueError):
                pass
            self.done = True

    def _emit(self, events: Any, ks: Any, first: int, stop: int) -> int:
        """Store windows first..stop-1 (empty ones included). Returns stop."""
        np = self._np
        if stop <= first:
            return first
        edges = np.searchsorted(ks, np.arange(first, stop + 1), side="left")
        for i, k in enumerate(range(first, stop)):
            if self._stop.is_set():
                return k
            chunk = events[edges[i]:edges[i + 1]]
            self._store_window(chunk)
        return stop

    def _store_window(self, chunk: Any) -> None:
        np = self._np
        self.events += len(chunk)
        if not len(chunk):
            self._index.append((self._written, 0))
            return
        image = cameras.accumulate_events(chunk["x"], chunk["y"], chunk["p"],
                                          self.width, self.height, np)
        flat = image.reshape(-1)
        changed = np.flatnonzero(flat != cameras.EVENT_MID)
        # +index+1 for white, -(index+1) for black: one int32 per pixel.
        values = (changed + 1).astype(np.int32)
        values[flat[changed] == 0] *= -1
        data = values.tobytes()
        with self._lock:
            self._out.write(data)
            self._out.flush()
        self._index.append((self._written, len(values)))
        self._written += len(data)


def raw_origin(index_csv: Any) -> Optional[int]:
    """The capture's origin on the camera's clock, from its frames CSV:
    timestamp_us - raw_t_us of any row that has both. None if there is none
    (a frame camera's run, or an older capture)."""
    try:
        with open(str(index_csv), newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                stamp = str(row.get("timestamp_us") or "").strip()
                raw_t = str(row.get("raw_t_us") or "").strip()
                if stamp.lstrip("-").isdigit() and raw_t.lstrip("-").isdigit():
                    return int(stamp) - int(raw_t)
    except (OSError, csv.Error, UnicodeDecodeError):
        return None
    return None


def run_window_us(index_csv: Any) -> Optional[int]:
    """How long each of the run's pictures collected events, from its CSV;
    None if it does not say (a frame camera, or an older capture)."""
    try:
        with open(str(index_csv), newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                value = str(row.get("window_us") or "").strip()
                if value.isdigit() and int(value) > 0:
                    return int(value)
    except (OSError, csv.Error, UnicodeDecodeError):
        return None
    return None


def open_raw(path: Any, window_us: int = DEFAULT_WINDOW_US, **kw: Any) -> RawPlayback:
    """Start reading `path`. Raises RawUnavailable at once if the SDK is
    missing, rather than handing back a playback that will only ever fail."""
    if kw.get("hal") is None:
        _hal()
    return RawPlayback(path, window_us=window_us, **kw)


# ======================================================================
# Where each saved PNG falls in the .raw
# ======================================================================
def frame_times(index_csv: Any) -> List[Tuple[int, str]]:
    """(raw_t_us, file name) for every row of a run's frames CSV that has a
    raw time, sorted by time. [] if the file is missing or unreadable."""
    rows: List[Tuple[int, str]] = []
    try:
        with open(str(index_csv), newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                raw_t = str(row.get("raw_t_us") or "").strip()
                name = str(row.get("file") or "").strip()
                if raw_t.lstrip("-").isdigit() and name:
                    rows.append((int(raw_t), name))
    except (OSError, csv.Error, UnicodeDecodeError):
        return []
    rows.sort()
    return rows


def window_for(raw_t_us: int, window_us: int) -> int:
    """The raw window matching a PNG whose last event was at `raw_t_us`.

    A PNG covers the window that ENDED at its last event, so its middle is
    half a window earlier; that is the raw window to show.
    """
    return max(0, (int(raw_t_us) - int(window_us) // 2)) // max(1, int(window_us))


def nearest_frame(times: List[Tuple[int, str]], t_us: int) -> str:
    """The file whose raw time is closest to `t_us`, or "" if none."""
    if not times:
        return ""
    keys = [t for t, _ in times]
    i = bisect_left(keys, int(t_us))
    best = min((j for j in (i - 1, i) if 0 <= j < len(times)),
               key=lambda j: abs(keys[j] - int(t_us)))
    return times[best][1]
