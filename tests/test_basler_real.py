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
