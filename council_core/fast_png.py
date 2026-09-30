"""
council_core.fast_png — lossless PNG as fast as the disk takes it.

WHY THIS EXISTS
A boA5320 frame is 5328 x 3040. Measured on a 28-core PC, Pillow needs 615 ms
to write one Mono8 frame as PNG (compress_level=1) and ~1 s for Mono12 held in
16 bits; OpenCV is no faster. Eight writer threads then manage 12.5 and 7
frames a second — against a camera sending 30 — while using only 7-8 of the 28
cores, which is exactly "frames are dropped but the PC is idle".

Almost all of that time is spent compressing, and compression buys little on
camera data: sensor noise leaves Mono8 at 15.4 MB compressed vs 16.2 MB raw,
16-bit at 29.6 vs 32.4 MB. So this writer does not compress. It writes a
STANDARD PNG — every row with filter type None, the pixels in "stored"
(uncompressed) deflate blocks — which any PNG reader opens and which decodes to
exactly the same pixels (checked against Pillow and OpenCV). Measured: 60 ms
per Mono8 frame on one thread, and the rest is disk bandwidth.

HOW IT IS FAST
Nothing is compressed and nothing is written a piece at a time: the whole file
is laid out in one buffer with numpy (two memcpy-speed copies: pixels into
rows with their filter byte — swapping to big-endian for 16-bit on the way —
and rows into 65535-byte stored blocks with their 5-byte headers), checksummed
with zlib's adler32/crc32 (which release the GIL), and handed to the OS in one
write. The buffers are kept per thread and reused: allocating 32-64 MB per
frame was measured to serialise writer threads on Windows.

COMPRESSED ONLY WHEN IT IS CHEAP AND PAYS
Some frames DO compress well — an EVK4 event picture is mid-grey with sparse
black and white pixels — and for those deflate is both small and fast:
measured on 1280x720 windows, zlib level 1 made them 1-11% of raw in 2.6-7.8
ms, where Pillow took 11-37 ms for 2-20%. On a Basler frame the same call
costs 360-1120 ms. So `compress="auto"` compresses a ~64 KB sample of the rows
first (0.3-2.5 ms) and compresses the frame only if the sample shrank to
COMPRESS_IF_RATIO or less: every EVK4 case measured was <= 0.11, every Basler
case >= 0.44 (a dark scene's sensor noise still defeats deflate), and the
sample predicted the whole frame's ratio within 0.03.

WHAT IT TAKES
2-D uint8 / uint16 (grey), and H x W x 3 or x 4 uint8 / uint16 (RGB / RGBA),
in any memory layout (crops and views included). Anything else raises
TypeError, so a caller can fall back to a general encoder.
"""
from __future__ import annotations

import struct
import threading
import zlib
from pathlib import Path
from typing import Any, Tuple

import numpy as np

#: The largest stored deflate block (RFC 1951: LEN is 16 bits).
BLOCK = 65535
#: A full stored block's header: BFINAL=0, BTYPE=00, LEN=0xFFFF, NLEN=0x0000.
_FULL_HEADER = np.frombuffer(b"\x00\xff\xff\x00\x00", np.uint8)
_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: zlib header for a stream of stored blocks: CM=8, 32 KB window, FLEVEL=0.
_ZLIB_HEADER = b"\x78\x01"
#: PNG colour types by channel count.
_COLOUR = {1: 0, 3: 2, 4: 6}

#: Compress a frame whose row sample shrinks to this fraction or less.
COMPRESS_IF_RATIO = 0.25
#: How much of the frame the sample compresses, in bytes of rows.
SAMPLE_BYTES = 64 * 1024

_local = threading.local()


def _layout(image: Any) -> Tuple[int, int, int, int]:
    """(height, width, channels, bytes per sample), or TypeError."""
    if not isinstance(image, np.ndarray):
        raise TypeError("fast_png writes numpy arrays")
    if image.dtype.kind != "u" or image.dtype.itemsize not in (1, 2):
        raise TypeError(f"fast_png writes uint8 / uint16, not {image.dtype}")
    if image.ndim == 2:
        channels = 1
    elif image.ndim == 3 and image.shape[2] in (3, 4):
        channels = image.shape[2]
    else:
        raise TypeError(f"fast_png writes grey, RGB or RGBA, not shape "
                        f"{image.shape}")
    h, w = image.shape[:2]
    if h < 1 or w < 1:
        raise TypeError("an empty image is not a PNG")
    return h, w, channels, image.dtype.itemsize


def _buffer(name: str, size: int) -> np.ndarray:
    """This thread's reusable buffer of at least `size` bytes."""
    held = getattr(_local, name, None)
    if held is None or held.size < size:
        held = np.empty(size, np.uint8)
        setattr(_local, name, held)
    return held[:size]


def _chunk_head(tag: bytes, length: int) -> bytes:
    return struct.pack(">I", length) + tag


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(data, zlib.crc32(tag)) & 0xFFFFFFFF
    return _chunk_head(tag, len(data)) + data + struct.pack(">I", crc)


def _rows(image: np.ndarray) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
    """The image as PNG rows, each led by its filter byte (0 = None), in
    this thread's reusable buffer; and its (h, w, channels, depth bytes)."""
    h, w, channels, depth_bytes = _layout(image)
    row = w * channels * depth_bytes
    raw = _buffer("raw", h * (row + 1)).reshape(h, row + 1)
    raw[:, 0] = 0
    if depth_bytes == 1:
        np.copyto(raw[:, 1:].reshape(image.shape), image, casting="no")
    else:
        # PNG is big-endian; the cast happens during the copy, not after it.
        np.copyto(raw[:, 1:].view(">u2").reshape(image.shape), image,
                  casting="equiv")
    return raw, (h, w, channels, depth_bytes)


def worth_compressing(raw: np.ndarray) -> bool:
    """Whether deflate will shrink these rows a lot — decided on a sample of
    evenly spaced rows, never the whole frame (see the module notes)."""
    h, width = raw.shape
    take = max(1, min(h, SAMPLE_BYTES // max(1, width)))
    sample = np.ascontiguousarray(raw[::max(1, h // take)][:take])
    return len(zlib.compress(sample, 1)) <= COMPRESS_IF_RATIO * sample.nbytes


def _ihdr(h: int, w: int, channels: int, depth_bytes: int) -> bytes:
    return _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8 * depth_bytes,
                                       _COLOUR[channels], 0, 0, 0))


def encode_into(image: np.ndarray, compress: Any = "auto") -> memoryview:
    """The whole PNG file for `image`.

    `compress`: "auto" (the module notes), True or False. A stored file is
    laid out in this thread's reusable buffer and is valid until this thread
    encodes again — write it out before that.
    """
    raw, (h, w, channels, depth_bytes) = _rows(image)
    if compress == "auto":
        compress = worth_compressing(raw)
    if compress:
        idat = zlib.compress(raw, 1)                 # releases the GIL
        return memoryview(_SIGNATURE + _ihdr(h, w, channels, depth_bytes)
                          + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))
    return _stored(raw, h, w, channels, depth_bytes)


def _stored(raw: np.ndarray, h: int, w: int, channels: int,
            depth_bytes: int) -> memoryview:
    """The file with the rows in stored (uncompressed) deflate blocks."""
    flat = raw.reshape(-1)
    raw_len = flat.size

    full, last = divmod(raw_len, BLOCK)
    blocks = full + (1 if last else 0)
    zlen = len(_ZLIB_HEADER) + raw_len + 5 * blocks + 4

    head = (_SIGNATURE + _ihdr(h, w, channels, depth_bytes)
            + _chunk_head(b"IDAT", zlen))
    tail_len = 4 + 12                        # IDAT CRC + IEND chunk
    total = len(head) + zlen + tail_len
    out = _buffer("out", total)

    at = 0
    out[at:at + len(head)] = np.frombuffer(head, np.uint8)
    at += len(head)
    idat_from = at - 4                       # the CRC covers the tag too
    out[at:at + 2] = np.frombuffer(_ZLIB_HEADER, np.uint8)
    at += 2
    if full:
        stored = out[at:at + full * (BLOCK + 5)].reshape(full, BLOCK + 5)
        stored[:, :5] = _FULL_HEADER
        stored[:, 5:] = flat[:full * BLOCK].reshape(full, BLOCK)
        at += full * (BLOCK + 5)
    if last:
        # The final, shorter block (BFINAL=1).
        out[at:at + 5] = np.frombuffer(
            struct.pack("<BHH", 1, last, last ^ 0xFFFF), np.uint8)
        out[at + 5:at + 5 + last] = flat[full * BLOCK:]
        at += 5 + last
    else:
        # The rows filled whole blocks exactly: the last full one is final.
        out[at - (BLOCK + 5)] = 1
    adler = zlib.adler32(flat) & 0xFFFFFFFF
    out[at:at + 4] = np.frombuffer(struct.pack(">I", adler), np.uint8)
    at += 4
    crc = zlib.crc32(out[idat_from:at]) & 0xFFFFFFFF
    out[at:at + 4] = np.frombuffer(struct.pack(">I", crc), np.uint8)
    at += 4
    out[at:at + 12] = np.frombuffer(_chunk(b"IEND", b""), np.uint8)
    at += 12
    assert at == total, (at, total)
    return memoryview(out)


def write_png(image: np.ndarray, path: Any, compress: Any = "auto") -> int:
    """Write `image` to `path` as a lossless PNG; the number of bytes written.

    TypeError for an image this writer does not take (see the module notes),
    raised BEFORE the file is opened, so the caller can fall back to a
    general encoder. Writes `path` directly: a caller that must never leave a
    partial file writes to a temporary name and renames.
    """
    data = encode_into(image, compress)
    with open(Path(path), "wb", buffering=0) as f:
        written = 0
        while written < len(data):
            written += f.write(data[written:])
    return written
