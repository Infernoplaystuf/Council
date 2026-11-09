"""
The webcam plugin's own tests: run with a Council checkout on PYTHONPATH,
by `python -m typhon_plugin_api.check <this plugin's folder>` (which runs
the camera contract checks first) or by pytest directly.

No webcam is needed: FakeCv2 stands in for OpenCV, with one or two
pretend cameras that send a moving test pattern.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from typhon_plugin_api.fake_host import FakeHost

PLUGIN = Path(__file__).resolve().parents[1]


class FakeCapture:
    """cv2.VideoCapture for a pretend camera (or none at that number)."""

    def __init__(self, cameras: int, number: int) -> None:
        self.number = number
        self.opened = number < cameras
        self.count = 0

    def isOpened(self) -> bool:                           # noqa: N802
        return self.opened

    def read(self):
        if not self.opened:
            return False, None
        self.count += 1
        picture = np.zeros((48, 64, 3), dtype=np.uint8)
        picture[:, self.count % 64] = (10, 200, 30)       # a moving line
        return True, picture

    def release(self) -> None:
        self.opened = False


class FakeCv2:
    """The few parts of OpenCV the plugin uses."""
    COLOR_BGR2GRAY = 6
    COLOR_BGR2RGB = 4

    def __init__(self, cameras: int = 1) -> None:
        self.cameras = cameras

    def VideoCapture(self, number: int) -> FakeCapture:   # noqa: N802
        return FakeCapture(self.cameras, number)

    def cvtColor(self, picture, code):                    # noqa: N802
        if code == self.COLOR_BGR2GRAY:
            return picture.mean(axis=2).astype(np.uint8)
        return picture[:, :, ::-1]


@pytest.fixture
def fake(tmp_path):
    with FakeHost(tmp_path) as host:
        host.load(PLUGIN)
        host.instance("example.webcam").cv2 = FakeCv2(cameras=2)
        yield host


def test_it_lists_each_webcam(fake):
    plugin = fake.instance("example.webcam")
    infos = plugin.discover()
    assert [i.key for i in infos] == ["webcam-0", "webcam-1"]
    assert all(i.backend == "example.webcam" for i in infos)


def test_without_opencv_it_says_what_to_install(tmp_path):
    with FakeHost(tmp_path) as host:
        host.load(PLUGIN)
        plugin = host.instance("example.webcam")

        def missing():
            raise ImportError("No module named 'cv2'")
        plugin.cv2 = None
        plugin._sdk = missing
        ok, hint = plugin.available()
        assert not ok and "opencv-python" in hint


def test_a_frame_owns_its_array_and_is_cropped(fake):
    plugin = fake.instance("example.webcam")
    device = plugin.open(plugin.discover()[0])
    try:
        device.set_roi(type(device.roi())(8, 4, 32, 16))
        device.start()
        frame = device.read(1000)
        assert frame.image.shape == (16, 32)               # Mono8, cropped
        assert frame.image.flags.owndata
        assert frame.meta["aoi"] == (8, 4, 32, 16)
    finally:
        device.stop()
        device.close()


def test_typhon_captures_from_it(fake):
    """The whole path: Connect, Start, 3 frames saved, Stop — the frames
    CSV, the PNGs and the end record agree."""
    fake.connect("frame", label="Webcam 0")
    run = fake.capture(frames=3)
    assert run.problems() == []
    assert len(run.pngs()) >= 3
    assert run.record["plugins"]["example.webcam"]["fields"][
        "camera_number"] == 0
