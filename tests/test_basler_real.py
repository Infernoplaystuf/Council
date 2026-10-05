"""
BaslerDevice against REAL pypylon and its camera emulator: what the app does
with each pixel format, and what pylon keeps when the app falls behind.

Skipped where pypylon is not installed (it is on this machine in the `pylon`
env). Nothing here is hardware: the emulator stands in for a camera.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("PYLON_CAMEMU", "1")

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import cameras, capture

pylon = pytest.importorskip("pypylon.pylon")


@pytest.fixture
def device():
    backend = cameras.BaslerBackend()
    found = [c for c in backend.discover() if "Emulation" in (c.model or "")]
    if not found:
        pytest.skip("pylon's camera emulator is not enabled (PYLON_CAMEMU)")
    dev = backend.open(found[0])
    yield dev
    dev.close()


def grab_one(dev, fmt):
    dev._cam.PixelFormat.SetValue(fmt)
    dev.start()
    try:
        for _ in range(20):
            frame = dev.read(1000)
            if frame is not None:
                return frame
    finally:
        dev.stop()
    raise AssertionError(f"no frame in {fmt}")


@pytest.mark.parametrize("fmt, shape_len, dtype", [
    ("Mono8", 2, "uint8"), ("Mono12", 2, "uint16"), ("RGB8Packed", 3, "uint8"),
    ("BayerRG8", 2, "uint8"), ("BayerRG12", 2, "uint16"),
])
def test_formats_saved_as_they_come(device, fmt, shape_len, dtype, tmp_path):
    frame = grab_one(device, fmt)
    assert frame.image.ndim == shape_len and frame.image.dtype.name == dtype
    capture.write_image(frame.image, tmp_path / "f.png")       # saves


@pytest.fixture
def red_camera(device, tmp_path):
    """The emulator serving a known RED picture from a file. Its own test
    image is grey (all three channels equal), so on it a red/blue swap is
    invisible — which is how one went unnoticed."""
    from PIL import Image

    picture = np.zeros((64, 64, 3), np.uint8)
    picture[..., 0], picture[..., 1], picture[..., 2] = 200, 60, 10
    Image.fromarray(picture).save(tmp_path / "red.png")
    cam = device._cam
    cam.TestImageSelector.SetValue("Off")
    cam.ImageFileMode.SetValue("On")
    cam.ImageFilename.SetValue(str(tmp_path / "red.png"))
    return device


@pytest.mark.parametrize("fmt", ["RGB8Packed", "BGR8Packed", "BGRA8Packed"])
def test_colour_comes_out_red_first_whatever_the_format(red_camera, fmt):
    """grab.Array hands BGR8 back blue-first (measured); saved as it came,
    red and blue were swapped in every PNG. RGB8 must NOT be swapped."""
    image = grab_one(red_camera, fmt).image
    middle = image[image.shape[0] // 2, image.shape[1] // 2]
    assert list(middle) == [200, 60, 10], (fmt, list(middle))


@pytest.mark.parametrize("fmt", ["BGRA8Packed", "RGB16Packed"])
def test_formats_pypylon_cannot_hand_over_are_converted_not_fatal(device, fmt,
                                                                  tmp_path):
    """grab.Array raised on these and ended the capture loop."""
    frame = grab_one(device, fmt)
    assert frame.image.ndim == 3 and frame.image.shape[2] == 3
    assert frame.image.dtype.name == "uint8"
    capture.write_image(frame.image, tmp_path / "f.png")


def _slow_reads(dev, seconds, recording):
    dev._cam.AcquisitionFrameRateEnable.SetValue(True)
    dev._cam.AcquisitionFrameRate.SetValue(100.0)
    dev.prepare(recording)
    dev.start()
    got, lost = 0, 0
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            frame = dev.read(1000)
            if frame is not None:
                got += 1
                lost += frame.meta.get("skipped_by_camera", 0)
            time.sleep(0.02)                    # a consumer at half speed
    finally:
        dev.stop()
    return got, lost


def test_live_view_keeps_only_the_newest_and_counts_the_rest(device):
    got, lost = _slow_reads(device, 1.0, recording=False)
    assert lost > 0, "LatestImageOnly skipped nothing?"


def test_recording_keeps_frames_rather_than_skipping_them(device):
    """While recording, pylon queues frames (OneByOne) instead of throwing
    them away before the app sees them."""
    got, lost = _slow_reads(device, 1.0, recording=True)
    assert lost == 0, f"{lost} frames skipped while recording"
    assert got >= 40


def test_recording_sizes_pylons_buffers_from_the_frame_size(device):
    device.prepare(True)
    device.start()
    try:
        payload = device._cam.PayloadSize.GetValue()
        want = max(10, min(200, cameras.BaslerDevice.RECORD_BUFFER_BYTES // payload))
        assert device._cam.MaxNumBuffer.GetValue() == want
    finally:
        device.stop()


@pytest.fixture
def changing_camera(device, tmp_path):
    """The emulator cycling through distinct pictures (a folder of them), so
    a frame overwritten by a later one is visible."""
    from PIL import Image

    for k in range(6):
        Image.fromarray(np.full((64, 64), 20 + 30 * k, np.uint8)).save(
            tmp_path / f"p{k}.png")
    cam = device._cam
    cam.TestImageSelector.SetValue("Off")
    cam.ImageFileMode.SetValue("On")
    cam.ImageFilename.SetValue(str(tmp_path))
    cam.PixelFormat.SetValue("Mono8")
    return device


def test_frames_kept_by_the_app_are_never_overwritten(changing_camera):
    """Frame arrays are reused (FramePool) — but only once nothing holds
    them. Keep some frames, read many more, and the kept ones must still
    hold exactly what they held when they arrived."""
    dev = changing_camera
    dev.prepare(True)
    dev.start()
    try:
        kept = []
        for _ in range(40):
            frame = dev.read(2000)
            if frame is None:
                continue
            if len(kept) < 5:
                kept.append((frame, frame.image.copy()))
        values = {int(f.image[0, 0]) for f, _ in kept}
        assert len(values) > 1, "the emulator served one picture only"
        for frame, snapshot in kept:
            assert np.array_equal(frame.image, snapshot)
        assert dev._pool.reused > 0, "no array was ever reused"
    finally:
        dev.stop()


# ======================================================================
# Settings (council_core.camera_settings) on the emulator's node map
# ======================================================================
def _settings(dev):
    return {s.key: s for s in dev.settings()}


def test_the_emulators_settings_come_from_its_nodes(device):
    from council_core import camera_settings as cs

    found = _settings(device)
    exposure = found["ExposureTime"]
    assert exposure.kind == cs.FLOAT and exposure.unit == "µs"
    node = device._cam.ExposureTime
    assert (exposure.minimum, exposure.maximum) == (node.GetMin(),
                                                    node.GetMax())
    assert found["Gain"].unit == "dB" and found["Gain"].step is None
    assert "Mono12" in found["PixelFormat"].choices
    assert found["BinningHorizontal"].kind == cs.INT
    assert found["ReverseX"].kind == cs.BOOL


def test_an_out_of_range_gain_is_clamped_not_an_exception(device):
    """pylon raises OutOfRangeException for Gain 1e9 (measured)."""
    change = device.set_setting("Gain", 1e9)
    assert change.ok and change.adjusted
    assert change.value == pytest.approx(device._cam.Gain.GetMax())


def test_the_camera_snapped_exposure_is_what_is_reported(device):
    device.set_setting("ExposureAuto", "Off")
    change = device.set_setting("ExposureTime", 5003.7)
    assert change.value == pytest.approx(5004.0) and change.adjusted


def test_locked_while_grabbing_is_refused_before_anything_is_written(device):
    from council_core import camera_settings as cs

    device._cam.PixelFormat.SetValue("Mono8")
    gain = device._cam.Gain.GetValue()
    device.start()
    try:
        assert _settings(device)["PixelFormat"].live is False
        with pytest.raises(cs.NeedsStop):
            device.apply_settings({"Gain": gain + 3, "PixelFormat": "Mono12"})
        assert device._cam.Gain.GetValue() == pytest.approx(gain)
        with pytest.raises(cs.NeedsStop):
            device.set_roi(cameras.Roi(0, 0, 256, 256))
        done = device.apply_settings({"Gain": gain + 3,
                                      "PixelFormat": "Mono8"})
        assert done.ok, "an unchanged locked format needs no stop"
    finally:
        device.stop()


def test_a_snapshot_applies_back_with_its_area(device):
    from council_core import camera_settings as cs

    device._cam.PixelFormat.SetValue("Mono8")
    device.set_roi(cameras.Roi(16, 8, 320, 240))
    saved = cs.snapshot(device)
    area = device.roi()
    device.apply_settings({"PixelFormat": "Mono12", "ReverseX": True},
                          roi=cameras.Roi(0, 0, 640, 480))
    done = device.apply_settings(saved, roi=area)
    assert done.ok, done.summary()
    assert done.roi == area
    assert device._cam.PixelFormat.GetValue() == "Mono8"
    assert device._cam.ReverseX.GetValue() is False
    frame = grab_one(device, "Mono8")
    assert frame.size == (320, 240) and frame.meta["aoi"] == (16, 8, 320, 240)
