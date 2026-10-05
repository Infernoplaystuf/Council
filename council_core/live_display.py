"""
council_core.live_display — a camera frame as the 8-bit array a picture can
wrap, at the least cost to the UI thread.

WHY THIS EXISTS
Typhon's live view runs on the UI thread, thirty times a second. Measured on
a boA5320-size frame (5328 x 3040), offscreen, before this module:

    Mono8   10.7 ms a frame: 9.1 ms of it the canvas COPYING the frame
    Mono12  19.8 ms a frame, and the picture was WRONG: a uint16 array was
            handed to a Grayscale8 QImage, which showed its raw bytes

THE COPY IS NOT NEEDED. The canvas copies "because a driver usually hands
back a buffer it will overwrite" — but no frame here is such a buffer: the
Basler path copies out of pylon's buffer into a FramePool array that is
handed out again only once NOTHING else refers to it (a QImage keeping the
array as its buffer is such a reference), and the event and simulated
cameras make a new array per frame. So an 8-bit frame goes to the canvas as
it is (`set_array(..., copy=False)`), and the 9 ms copy is gone.

A 16-BIT FRAME IS SHIFTED TO 8 BITS, the way capture_review.DisplayDecoder
shows a saved Mono12 PNG: by the bits it uses, a shift that only ever grows
(a dark frame must not flash brighter than its neighbours). Done in row
blocks on a few threads — numpy releases the GIL — into arrays from a
FramePool, so the picture on screen is never overwritten by the next one:
20 ms single-threaded, ~6 ms on four threads (measured).

FULL RESOLUTION, NEVER DECIMATED. Painting a 16-megapixel frame scaled to
the window measured 1.6 ms; decimating would save little and would put the
picture in different pixels from the frame — and a box drawn on it must be
in the frame's own pixels (frame_camera.set_area adds the camera area's
origin and nothing else).
"""
from __future__ import annotations

import os
import threading
from typing import Any

from .cameras import FramePool, _numpy

#: Below this many pixels one thread is as quick as several.
PARALLEL_PIXELS = 1_000_000

#: Display arrays kept for reuse: a few frames' worth at the largest size.
POOL_BYTES = 256 * 1024 * 1024

_EXECUTOR: Any = None
_EXECUTOR_LOCK = threading.Lock()


def _executor() -> Any:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            from concurrent.futures import ThreadPoolExecutor

            workers = max(2, min(4, (os.cpu_count() or 2)))
            _EXECUTOR = ThreadPoolExecutor(max_workers=workers,
                                           thread_name_prefix="display")
        return _EXECUTOR


class DisplayPrep:
    """Turns frames into what a canvas can wrap without copying.

    One per viewer: the 16-bit shift it learns belongs to what that viewer
    shows. `reset()` when the camera changes (a new connection).

    THE SHIFT BELONGS TO ONE PIXEL FORMAT. It only grows, so that a dark
    frame does not flash brighter than its neighbours — but a camera whose
    pixel format changes under a running live view (the settings window, a
    preset) changes how many bits the same uint16 holds. Measured on pylon's
    emulator before: Mono16 then Mono12 showed the Mono12 picture at 15 of
    255 (shift 8 kept), Mono10 at 3 — a black live view until Disconnect.
    So the shift starts again whenever the frame's format (Frame.meta
    ["format"], else its dtype and shape) is not the last one's.
    """

    def __init__(self) -> None:
        self.shift = 0
        self._pool = FramePool(POOL_BYTES)
        self._format: Any = None

    def reset(self) -> None:
        self.shift = 0
        self._format = None
        self._pool.clear()

    def __call__(self, image: Any, fmt: Any = None) -> Any:
        np = _numpy()
        data = image
        if data is None:
            return None
        kind = (fmt, data.dtype.str, data.ndim)
        if kind != self._format:
            self._format = kind
            self.shift = 0
        if data.dtype == np.uint8 and (data.ndim == 2 or (
                data.ndim == 3 and data.shape[2] in (3, 4))):
            # Wrapped as it is. A view whose rows are not contiguous (a
            # mirrored or cropped slice) cannot be: QImage reads each row
            # as consecutive bytes.
            if data.flags["C_CONTIGUOUS"]:
                return data
            return np.ascontiguousarray(data)
        if data.dtype == np.uint16 and data.ndim == 2:
            return self._shifted(data)
        if data.dtype == np.uint16 and data.ndim == 3:
            top = int(data.max()) if data.size else 0
            self.shift = max(self.shift, max(0, top.bit_length() - 8))
            return np.ascontiguousarray((data >> self.shift).astype(np.uint8))
        # Anything else (float, int32): scaled into 0..255 for viewing.
        lo, hi = float(data.min()), float(data.max())
        scale = 255.0 / (hi - lo) if hi > lo else 0.0
        return ((data - lo) * scale).astype(np.uint8)

    def _shifted(self, data: Any) -> Any:
        np = _numpy()
        # The bits in use, from EVERY pixel (1.4 ms at 16 megapixels): a
        # sampled maximum misses a lone hot pixel, and a value above 255
        # after the shift would wrap round to dark, not saturate.
        top = int(data.max()) if data.size else 0
        self.shift = max(self.shift, max(0, top.bit_length() - 8))
        shift = self.shift
        out = self._pool.take(data.shape, np.uint8)
        rows = data.shape[0]
        if data.size < PARALLEL_PIXELS or rows < 8:
            np.right_shift(data, shift, out=out, casting="unsafe")
            return out
        parts = 4
        step = (rows + parts - 1) // parts

        def block(start: int) -> None:
            stop = min(rows, start + step)
            np.right_shift(data[start:stop], shift, out=out[start:stop],
                           casting="unsafe")

        list(_executor().map(block, range(0, rows, step)))
        return out
