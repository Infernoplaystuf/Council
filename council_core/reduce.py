"""
council_core.reduce — a camera picture made smaller by AVERAGING blocks of
pixels, for a run saved at "Downsample 2x / 3x / 4x" when the camera cannot
bin (average) its own pixels.

WHY AVERAGE, AND WHY HERE
The point is a smaller file: a 5328 x 3040 Mono12 frame is 32 MB as it is
saved (uncompressed, capture.write_image), 8.1 MB at 2x2, 3.6 MB at 3x3 and
2.0 MB at 4x4 (measured). Where the camera can bin in Average mode it does
it itself (council_core.cameras, BaslerDevice.apply_downsample) and the link
carries less too. Where it cannot — the simulated camera, pylon's emulator
(it accepts binning and does nothing, measured), a colour model without
binning — Typhon averages each f x f block before the frame is saved and
before it is shown, so the saved PNG, the live picture, the slider and a box
drawn on any of them are ONE geometry.

NEVER ON THE GRAB THREAD. A full boA5320 frame takes 22-34 ms here
(measured, strided adds into uint32), against a grab budget of 6.7 ms at
150 fps. It runs on the save queue's writer threads (capture.Recorder.save)
and on the display's thread pool (live_display.DisplayPrep), never in
cameras' read().

16-BIT SAFE, BRIGHTNESS KEPT
Blocks are summed in uint32 (16 x 65535 fits with room to spare: a Mono16
2x2 sum in uint16 overflowed, measured 39262 for a true 170334) and divided
ROUNDING HALF UP, (sum + n // 2) // n — at most half a count from the exact
mean, where floor division darkened every picture by 3/8 of a count. The
result keeps the input's dtype: a Mono12 frame stays uint16 holding
0..4095, so its PNG means what an unreduced one does. The right and bottom
edges that do not fill a whole block are LEFT OUT (said in the run's record,
`dropped`), never averaged with padding that was not in the picture.

A RAW BAYER PICTURE IS REFUSED. Its neighbouring pixels are different
colours; averaging them would mix red, green and blue into a grey that is
neither. Choose a Mono or RGB pixel format to downsample (`refusal`).

`divided` is the other half of "brightness kept": a camera that can only SUM
its binned pixels (some ace models) gives f x f times the brightness, and
Typhon divides each pixel by f x f, rounding half up, so the picture is as
bright as unbinned. What the camera clipped at its maximum stays clipped —
at max / (f x f) after the division — which the run's record says.

NO TOOLKIT, NO SDK. numpy only.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

#: The factors offered (1 is "Off").
FACTORS: Tuple[int, ...] = (1, 2, 3, 4)

#: How a reduced picture was made, as the frames CSV and the records say it.
TYPHON_MEAN = "typhon_block_mean"
CAMERA_AVERAGE = "camera_binning_average"
CAMERA_SUM_DIVIDED = "camera_binning_sum_divided"

#: Below this many pixels a frame is reduced on one thread.
PARALLEL_PIXELS = 1_000_000


def _np() -> Any:
    import numpy
    return numpy


def refusal(fmt: Any) -> str:
    """Why a picture in pixel format `fmt` cannot be averaged by Typhon, or
    "" when it can. Only a raw Bayer picture is refused."""
    name = str(fmt or "")
    if name.lower().startswith("bayer"):
        return (f"the pixel format is {name}, a raw Bayer picture — its "
                f"neighbouring pixels are different colours, and averaging "
                f"them mixes the colours. Choose a Mono or RGB pixel format "
                f"to downsample")
    return ""


def reduced_size(width: int, height: int, factor: int) -> Tuple[int, int]:
    """The picture's (width, height) after `factor`: whole blocks only."""
    factor = max(1, int(factor))
    return int(width) // factor, int(height) // factor


def _check(image: Any) -> Any:
    np = _np()
    data = np.asarray(image)
    if data.ndim not in (2, 3):
        raise ValueError(f"a picture has 2 or 3 dimensions, not {data.ndim}")
    if data.dtype not in (np.uint8, np.uint16):
        raise ValueError(f"a {data.dtype} picture cannot be averaged here "
                         f"(uint8 or uint16 only)")
    return data


def block_mean(image: Any, factor: int, divide: int = 1,
               pool: Any = None) -> Any:
    """`image` with every `factor` x `factor` block averaged into one pixel
    (rounding half up), the input's dtype kept, and the right and bottom
    remainders left out. A 3-channel picture is averaged per channel.

    `divide` (> 1) also divides each result by it — a camera's Sum binning
    compensated (see the module notes); with `factor` 1 that is all it does.
    `pool` (a concurrent.futures executor) splits a large picture into row
    bands worked on at once — the display's pool, never the grab thread.
    `factor` 1 and `divide` 1 hand the picture back as it is."""
    np = _np()
    factor = int(factor)
    divide = max(1, int(divide or 1))
    if factor < 1:
        raise ValueError(f"the downsample factor must be 1 or more, "
                         f"not {factor}")
    data = _check(image)
    if factor == 1 and divide == 1:
        return data
    rows, cols = data.shape[0] // factor, data.shape[1] // factor
    if rows < 1 or cols < 1:
        raise ValueError(f"a {data.shape[1]} x {data.shape[0]} picture is "
                         f"smaller than one {factor} x {factor} block")
    out = np.empty((rows, cols) + data.shape[2:], dtype=data.dtype)
    n = factor * factor * divide

    def band(start: int, stop: int) -> None:
        part = data[start * factor:stop * factor, :cols * factor]
        acc = np.zeros((stop - start, cols) + data.shape[2:], np.uint32)
        for dy in range(factor):
            for dx in range(factor):
                acc += part[dy::factor, dx::factor]
        acc += n // 2
        acc //= n
        # The mean of values that fit the dtype fits it too (rounding half
        # up cannot carry past the largest: (n * top + n // 2) // n is top).
        out[start:stop] = acc

    if pool is not None and data.size >= PARALLEL_PIXELS and rows >= 8:
        parts = 4
        step = (rows + parts - 1) // parts
        spans = [(s, min(rows, s + step)) for s in range(0, rows, step)]
        list(pool.map(lambda span: band(*span), spans))
    else:
        band(0, rows)
    return out


def divided(image: Any, divide: int) -> Any:
    """Each pixel divided by `divide`, rounding half up, dtype kept."""
    return block_mean(image, 1, divide=divide)


def prepared(image: Any, meta: Optional[Mapping[str, Any]],
             pool: Any = None) -> Any:
    """A frame's picture as it is saved and shown: reduced by
    meta["reduce"] (Typhon's block mean) and divided by meta["divide"]
    (a camera's Sum binning compensated), when the camera that took it
    said so — else the picture as it came. A raw Bayer frame with a
    reduction to make raises ValueError (refusal): a run fails loudly
    rather than saving mixed colours."""
    meta = meta or {}
    factor = int(meta.get("reduce", 1) or 1)
    divide = int(meta.get("divide", 1) or 1)
    if factor <= 1 and divide <= 1:
        return image
    why = refusal(meta.get("format", ""))
    if why:
        raise ValueError(why)
    return block_mean(image, factor, divide=divide, pool=pool)


def sensor_position(x_picture: float, factor: int, area_x: float = 0.0,
                    binning: int = 1) -> float:
    """Where the CENTRE of a picture pixel lies on the sensor, in unbinned
    sensor pixels (centres at integers): a reduced picture's pixel x covers
    `factor` camera pixels from x * factor, the camera's area starts at
    `area_x` (camera pixels — binned ones on a binned camera), and each
    camera pixel covers `binning` sensor pixels. For a script that measures
    on a saved frame and wants the sensor's coordinates."""
    camera = float(x_picture) * factor + (factor - 1) / 2.0 + float(area_x)
    return camera * binning + (binning - 1) / 2.0
