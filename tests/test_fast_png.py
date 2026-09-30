"""
council_core.fast_png — a standard, lossless PNG written at disk speed.

Every image is read back by the readers people actually use (Pillow, and
OpenCV where installed) and must come back pixel for pixel. The edge cases are
the ones a stored-block writer gets wrong: a stream that fills its 65535-byte
blocks exactly, a single block, a 1-pixel image, crops and views, and 16-bit
byte order.
"""
from __future__ import annotations

import sys
import threading
import zlib
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import fast_png

Image = pytest.importorskip("PIL.Image")


def decode(data):
    """A reference decode that trusts no library: walk the chunks, check
    every CRC, inflate the IDAT (zlib checks the adler32), undo filter None."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    at, idat, ihdr = 8, b"", None
    while at < len(data):
        length = int.from_bytes(data[at:at + 4], "big")
        tag, body = data[at + 4:at + 8], data[at + 8:at + 8 + length]
        crc = int.from_bytes(data[at + 8 + length:at + 12 + length], "big")
        assert zlib.crc32(tag + body) & 0xFFFFFFFF == crc, tag
        if tag == b"IHDR":
            ihdr = body
        elif tag == b"IDAT":
            idat += body
        at += 12 + length
    w, h = int.from_bytes(ihdr[:4], "big"), int.from_bytes(ihdr[4:8], "big")
    depth, colour = ihdr[8], ihdr[9]
    channels = {0: 1, 2: 3, 6: 4}[colour]
    rows = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(h, -1)
    assert (rows[:, 0] == 0).all()                   # filter None everywhere
    pixels = np.ascontiguousarray(rows[:, 1:])
    if depth == 16:
        pixels = pixels.view(">u2").astype(np.uint16)
    shape = (h, w) if channels == 1 else (h, w, channels)
    return pixels.reshape(shape)


def readers(path, image):
    got = [("reference", decode(path.read_bytes()))]
    # Pillow has no 16-bit RGB/RGBA mode (it reduces them to 8-bit), so it
    # can only vouch for the layouts it can hold.
    if not (image.ndim == 3 and image.dtype.itemsize == 2):
        got.append(("Pillow", np.asarray(Image.open(path))))
    try:
        import cv2
    except Exception:                                   # noqa: BLE001
        return got
    back = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if back is not None and back.ndim == 3:
        order = [2, 1, 0] if back.shape[2] == 3 else [2, 1, 0, 3]
        back = back[..., order]                        # cv2 is BGR(A)
    got.append(("OpenCV", back))
    return got


def roundtrip(image, tmp_path, name="f.png"):
    path = tmp_path / name
    fast_png.write_png(image, path)
    for who, back in readers(path, image):
        assert back is not None, who
        assert (back.dtype.kind, back.dtype.itemsize) == \
            (image.dtype.kind, image.dtype.itemsize), (who, back.dtype)
        assert np.array_equal(back, image), who
    return path


rng = np.random.default_rng(7)


@pytest.mark.parametrize("shape, dtype", [
    ((480, 640), np.uint8), ((480, 640), np.uint16),
    ((97, 131, 3), np.uint8), ((97, 131, 3), np.uint16),
    ((50, 70, 4), np.uint8), ((1, 1), np.uint8), ((1, 1), np.uint16),
    ((1, 5000), np.uint8), ((5000, 1), np.uint16),
])
def test_every_layout_reads_back_exactly(shape, dtype, tmp_path):
    top = np.iinfo(dtype).max
    roundtrip(rng.integers(0, top, shape, endpoint=True).astype(dtype), tmp_path)


def test_rows_that_fill_the_stored_blocks_exactly(tmp_path):
    """No short final block: the last full block must carry BFINAL."""
    # h * (w + 1) == 65535 * 3  ->  h = 3, w = 65534
    image = rng.integers(0, 255, (3, 65534), endpoint=True).astype(np.uint8)
    assert image.shape[0] * (image.shape[1] + 1) % fast_png.BLOCK == 0
    roundtrip(image, tmp_path)


def test_one_block_and_one_byte_over(tmp_path):
    for w in (65534 - 1, 65534 + 1):                 # just under / over a block
        image = rng.integers(0, 255, (1, w), endpoint=True).astype(np.uint8)
        roundtrip(image, tmp_path, f"w{w}.png")


def test_crops_views_and_big_endian_input(tmp_path):
    frame = rng.integers(0, 4095, (300, 400), endpoint=True).astype(np.uint16)
    roundtrip(frame[37:201, 55:333], tmp_path, "crop.png")        # a view
    roundtrip(frame[::2, ::3], tmp_path, "strided.png")
    roundtrip(frame.astype(">u2"), tmp_path, "be.png")
    rgb = rng.integers(0, 255, (60, 80, 3), endpoint=True).astype(np.uint8)
    roundtrip(rgb[..., ::-1], tmp_path, "bgr_view.png")          # negative stride


def test_the_stream_is_valid_deflate_and_the_checksums_hold():
    image = rng.integers(0, 255, (123, 777), endpoint=True).astype(np.uint8)
    assert np.array_equal(decode(bytes(fast_png.encode_into(image))), image)


def test_threads_write_different_images_at_once(tmp_path):
    """The reusable buffers are per thread: parallel writers never mix."""
    images = [np.full((200, 300), i, np.uint8) + np.arange(300, dtype=np.uint8)
              for i in range(8)]
    errors = []

    def write(i):
        try:
            for k in range(5):
                fast_png.write_png(images[i], tmp_path / f"t{i}_{k}.png")
        except Exception as exc:                   # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for i in range(8):
        for k in range(5):
            assert np.array_equal(np.asarray(Image.open(tmp_path / f"t{i}_{k}.png")),
                                  images[i])


@pytest.mark.parametrize("bad", [
    np.zeros((4, 4), np.float32), np.zeros((4, 4), np.int16),
    np.zeros((4, 4, 2), np.uint8), np.zeros((0, 4), np.uint8),
    np.zeros((2, 3, 4, 5), np.uint8), [[1, 2], [3, 4]],
])
def test_what_it_does_not_take_is_a_type_error_so_callers_fall_back(bad, tmp_path):
    with pytest.raises(TypeError):
        fast_png.write_png(bad, tmp_path / "x.png")
    assert not (tmp_path / "x.png").exists()


def test_a_full_boa5320_frame_in_both_depths(tmp_path):
    """The size this exists for: 5328 x 3040, Mono8 and Mono12-in-16-bit."""
    mono8 = rng.integers(0, 255, (3040, 5328), endpoint=True).astype(np.uint8)
    roundtrip(mono8, tmp_path, "m8.png")
    mono12 = rng.integers(0, 4095, (3040, 5328), endpoint=True).astype(np.uint16)
    roundtrip(mono12, tmp_path, "m12.png")


# ======================================================================
# Compressed only when it is cheap and pays
# ======================================================================
def event_picture(w=1280, h=720, fraction=0.02):
    """An EVK4 window as capture draws it: mid-grey, sparse black/white."""
    img = np.full((h, w), 128, np.uint8)
    n = int(w * h * fraction)
    ys, xs = rng.integers(0, h, n), rng.integers(0, w, n)
    img[ys, xs] = np.where(rng.random(n) < 0.5, 255, 0).astype(np.uint8)
    return img


def test_an_event_picture_is_compressed_and_reads_back_exactly(tmp_path):
    img = event_picture()
    path = roundtrip(img, tmp_path, "evk.png")
    # Stored would be ~0.92 MB; measured ~11% of raw at 5% events.
    assert path.stat().st_size < 0.25 * img.nbytes


def test_a_noisy_camera_frame_is_stored_not_compressed(tmp_path):
    """Deflate on sensor noise costs ~40x the time for a few percent."""
    frame = np.clip(rng.normal(40, 8, (600, 800)), 0, 4095).astype(np.uint16)
    path = roundtrip(frame, tmp_path, "noisy.png")
    assert path.stat().st_size > frame.nbytes            # stored: raw + framing


@pytest.mark.parametrize("compress", [True, False])
def test_forced_either_way_is_still_exact(compress, tmp_path):
    for img in (event_picture(), rng.integers(0, 4095, (97, 131), endpoint=True).astype(np.uint16)):
        path = tmp_path / f"f{compress}.png"
        fast_png.write_png(img, path, compress=compress)
        assert np.array_equal(decode(path.read_bytes()), img)
        assert np.array_equal(np.asarray(Image.open(path)), img)


def test_the_sample_decides_like_the_whole_frame():
    assert fast_png.worth_compressing(fast_png._rows(event_picture())[0])
    noisy = np.clip(rng.normal(3, 2, (400, 600)), 0, 255).astype(np.uint8)
    assert not fast_png.worth_compressing(fast_png._rows(noisy)[0])
