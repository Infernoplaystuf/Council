"""
council_core.reduce — Downsample by Typhon: each block of pixels AVERAGED
into one, 16-bit safe, brightness kept, a Bayer picture refused, and a
saved pixel mapped back to the sensor exactly.

Pure numpy: no camera, no Qt.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import capture, cameras, reduce


def exact_mean(image, f):
    h, w = (image.shape[0] // f) * f, (image.shape[1] // f) * f
    part = image[:h, :w].astype(np.float64)
    if image.ndim == 2:
        return part.reshape(h // f, f, w // f, f).mean(axis=(1, 3))
    return part.reshape(h // f, f, w // f, f, image.shape[2]).mean(
        axis=(1, 3))


@pytest.mark.parametrize("factor", [2, 3, 4])
def test_a_block_is_its_mean_within_half_a_count(factor):
    rng = np.random.default_rng(factor)
    mono12 = rng.integers(0, 4096, (120, 161), dtype=np.uint16)
    got = reduce.block_mean(mono12, factor)
    assert got.dtype == np.uint16 and got.shape == (120 // factor,
                                                    161 // factor)
    assert np.abs(got - exact_mean(mono12, factor)).max() <= 0.5
    # Rounding half up, not down: floor division darkened every picture.
    assert abs(float(got.mean()) - float(exact_mean(mono12, factor).mean())) \
        < 0.3
    assert int(got.max()) <= 4095, "a Mono12 frame stays 0..4095"


def test_a_full_mono16_block_does_not_overflow():
    """Summed in uint16 a 2 x 2 Mono16 block overflowed (measured 39262 for
    a true 170334): summed in uint32 it stays at the top."""
    full = np.full((8, 8), 65535, np.uint16)
    assert np.all(reduce.block_mean(full, 2) == 65535)
    assert np.all(reduce.block_mean(full, 4) == 65535)
    eight = np.full((6, 6), 255, np.uint8)
    assert np.all(reduce.block_mean(eight, 3) == 255)


def test_odd_edges_are_left_out_not_padded():
    image = np.arange(7 * 9, dtype=np.uint16).reshape(7, 9)
    got = reduce.block_mean(image, 2)
    assert got.shape == (3, 4)
    assert got[0, 0] == int((image[0, 0] + image[0, 1] + image[1, 0]
                             + image[1, 1] + 2) // 4)
    assert reduce.reduced_size(9, 7, 2) == (4, 3)
    with pytest.raises(ValueError, match="smaller than one"):
        reduce.block_mean(np.zeros((3, 3), np.uint8), 4)


def test_colour_is_averaged_per_channel_and_bayer_is_refused():
    rgb = np.zeros((4, 4, 3), np.uint8)
    rgb[..., 0], rgb[..., 1], rgb[..., 2] = 200, 60, 10
    rgb[0, 0] = (0, 0, 0)
    got = reduce.block_mean(rgb, 2)
    assert got.shape == (2, 2, 3)
    assert tuple(got[0, 0]) == (150, 45, 8)          # half up: 7.5 -> 8
    assert tuple(got[1, 1]) == (200, 60, 10)
    assert reduce.refusal("Mono12") == "" and reduce.refusal("RGB8") == ""
    assert "Bayer" in reduce.refusal("BayerRG8")
    with pytest.raises(ValueError, match="raw Bayer"):
        reduce.prepared(np.zeros((4, 4), np.uint8),
                        {"reduce": 2, "format": "BayerRG8"})


def test_a_sum_binned_picture_is_divided_back_to_its_brightness():
    """A camera that can only SUM its binned pixels gives f x f times the
    brightness; divided, rounding half up, it is as bright as unbinned."""
    summed = np.array([[4 * 1000, 4 * 1000 + 2, 7]], np.uint16)
    assert reduce.divided(summed, 4).tolist() == [[1000, 1001, 2]]
    meta = {"divide": 4}
    assert reduce.prepared(summed, meta).tolist() == [[1000, 1001, 2]]


def test_the_threaded_bands_equal_one_pass():
    rng = np.random.default_rng(7)
    big = rng.integers(0, 65536, (1203, 1001), dtype=np.uint16)
    with ThreadPoolExecutor(4) as pool:
        for factor in (2, 3, 4):
            assert np.array_equal(reduce.block_mean(big, factor, pool=pool),
                                  reduce.block_mean(big, factor))


def test_off_hands_the_picture_back_untouched():
    image = np.arange(12, dtype=np.uint8).reshape(3, 4)
    assert reduce.prepared(image, {}) is image
    assert reduce.prepared(image, None) is image
    assert reduce.prepared(image, {"reduce": 1, "divide": 1}) is image
    assert reduce.block_mean(image, 1) is image


def test_a_spot_on_a_saved_frame_maps_back_to_the_sensor():
    """A sub-pixel spot on the SENSOR, binned 2 x 2 by the camera, cut to
    the camera's area, averaged 2 x 2 by Typhon: its centroid on the saved
    picture, through sensor_position, lands within 0.01 px of where it
    was."""
    yy, xx = np.mgrid[0:400, 0:600].astype(np.float64)
    sx, sy = 317.3, 211.85                # pixel centres at integers
    sensor = 60000.0 * np.exp(-((xx - sx) ** 2 + (yy - sy) ** 2) / 50.0)
    binned = sensor.reshape(200, 2, 300, 2).mean(axis=(1, 3))
    ax, ay = 60, 40                       # the camera's area, binned px
    frame = np.round(binned[ay:ay + 128, ax:ax + 160]).astype(np.uint16)
    saved = reduce.block_mean(frame, 2).astype(np.float64)
    py, px = np.mgrid[0:saved.shape[0], 0:saved.shape[1]]
    total = saved.sum()
    cx, cy = (px * saved).sum() / total, (py * saved).sum() / total
    assert reduce.sensor_position(cx, 2, ax, 2) == pytest.approx(sx, abs=0.01)
    assert reduce.sensor_position(cy, 2, ay, 2) == pytest.approx(sy, abs=0.01)


def test_the_recorder_saves_the_frame_the_camera_says_to_reduce(tmp_path):
    """Reduced on the writer threads (Recorder.save), never in read(); the
    frames CSV says the factor and how; Off writes every pixel as it came
    and leaves the columns blank."""
    from PIL import Image

    capture.warm_imports()
    image = (np.arange(48 * 64, dtype=np.uint16).reshape(48, 64) % 4096)
    rec = capture.Recorder(tmp_path, stem="r_frame", index_name="r.csv")
    rec.open()
    reduced = cameras.Frame(image, 1, 0, dict(
        cameras.downsample_meta(_setup(2), reduce.TYPHON_MEAN),
        format="Mono12"))
    plain = cameras.Frame(image, 2, 0, {"format": "Mono12"})
    rec.write(reduced)
    rec.write(plain)
    rec.close()
    one = np.asarray(Image.open(tmp_path / "r_frame_000001.png"))
    two = np.asarray(Image.open(tmp_path / "r_frame_000002.png"))
    assert np.array_equal(one, reduce.block_mean(image, 2))
    assert np.array_equal(two, image), "Off never changes the data"
    import csv
    with open(tmp_path / "r.csv", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert (rows[0]["downsample"], rows[0]["downsample_method"]) == (
        "2", "typhon_block_mean")
    assert (rows[1]["downsample"], rows[1]["downsample_method"]) == ("", "")


def test_what_each_frame_carries_for_each_method():
    meta = cameras.downsample_meta
    assert meta(_setup(1), "") == {}
    assert meta(_setup(2), reduce.TYPHON_MEAN) == {
        "downsample": 2, "downsample_method": "typhon_block_mean",
        "reduce": 2}
    assert meta(_setup(3), reduce.CAMERA_AVERAGE) == {
        "downsample": 3, "downsample_method": "camera_binning_average"}
    assert meta(_setup(2), reduce.CAMERA_SUM_DIVIDED) == {
        "downsample": 2, "downsample_method": "camera_binning_sum_divided",
        "divide": 4}
    assert meta(None, "") == {}, "an event camera has no set-up"


def _setup(factor):
    from council_core import triggered

    return triggered.CaptureSetup(downsample=factor)
