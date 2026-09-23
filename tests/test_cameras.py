"""
Tests for council_core.cameras.

THE FAKE SDKS ENFORCE WHAT THE REAL ONES ENFORCE
A fake that accepts everything proves nothing: the Basler AOI code would pass
against it in any order, including the orders that throw on a real camera. So
`FakeNode` rejects an unaligned value and rejects a size that overruns the
sensor given the CURRENT offset — which is exactly the pylon behaviour that
makes AOI ordering matter — and `FakeGrab` hands back a view into a buffer it
reuses after Release, which is what makes the copy necessary.

That makes these real tests of the real code paths. It does NOT make them
hardware verification, and no test here claims a camera was attached.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import cameras
from council_core.cameras import (CameraError, CameraInfo, Limits, Roi,
                                  accumulate_events, fit_roi)

# The real sensor this was written for.
SENSOR_W, SENSOR_H = 5320, 4600


# ======================================================================
# Fake pylon
# ======================================================================
class FakeNode:
    """A pylon node that validates like the real one."""

    def __init__(self, value, inc=1, low=0, high=0, camera=None, axis=None):
        self._value = value
        self._inc, self._min, self._max = inc, low, high
        self._camera, self._axis = camera, axis

    def GetValue(self):
        return self._value

    def GetInc(self):
        return self._inc

    def GetMin(self):
        return self._min

    def GetMax(self):
        return self._max

    def SetValue(self, value):
        value = int(value) if isinstance(self._value, int) else float(value)
        if isinstance(value, int) and self._inc > 1 and value % self._inc:
            raise RuntimeError(f"value {value} is not a multiple of "
                               f"{self._inc}")
        if self._max and value > self._max:
            raise RuntimeError(f"value {value} exceeds max {self._max}")
        if value < self._min:
            raise RuntimeError(f"value {value} below min {self._min}")
        # The cross-validation that makes ordering matter: offset + size must
        # fit on the sensor, checked against whatever the OTHER node holds now.
        if self._camera is not None and self._axis:
            self._camera.check(self._axis, self, value)
        self._value = value


class FakeCamera:
    def __init__(self, w=SENSOR_W, h=SENSOR_H):
        self.sensor = (w, h)
        self.WidthMax = FakeNode(w)
        self.HeightMax = FakeNode(h)
        self.Width = FakeNode(w, inc=4, low=16, high=w, camera=self, axis="x")
        self.Height = FakeNode(h, inc=2, low=16, high=h, camera=self, axis="y")
        self.OffsetX = FakeNode(0, inc=4, low=0, high=w, camera=self, axis="x")
        self.OffsetY = FakeNode(0, inc=2, low=0, high=h, camera=self, axis="y")
        self.ExposureTime = FakeNode(5000.0, low=20.0, high=100000.0)
        self.Gain = FakeNode(0.0, low=0.0, high=24.0)
        self.grabbing = False
        self.closed = False
        self.strategy = None
        self._buffer = np.zeros((4, 4), np.uint8)

    def check(self, axis, node, new_value):
        size, offset, limit = ((self.Width, self.OffsetX, self.sensor[0])
                               if axis == "x" else
                               (self.Height, self.OffsetY, self.sensor[1]))
        total = (new_value + offset.GetValue() if node is size
                 else size.GetValue() + new_value)
        if total > limit:
            raise RuntimeError(
                f"offset+size {total} overruns the sensor ({limit})")

    def Open(self):
        pass

    def Close(self):
        self.closed = True

    def StartGrabbing(self, strategy=None):
        self.grabbing, self.strategy = True, strategy

    def StopGrabbing(self):
        self.grabbing = False

    def RetrieveResult(self, timeout, handling):
        self._buffer[:] = 7
        return FakeGrab(self._buffer, ok=True)


class FakeGrab:
    """A grab result whose buffer is REUSED once released."""

    def __init__(self, buffer, ok=True):
        self._buffer, self._ok = buffer, ok
        self.released = False

    @property
    def Array(self):
        return self._buffer          # a view, not a copy — as in pypylon

    def GrabSucceeded(self):
        return self._ok

    def GetErrorDescription(self):
        return "" if self._ok else "the camera reported an error"

    def GetTimeStamp(self):
        return 123456

    def Release(self):
        self.released = True
        self._buffer[:] = 99         # the pool hands the buffer to the next grab


class FakeDeviceInfo:
    def __init__(self, serial, model="boA5320-150cm"):
        self._serial, self._model = serial, model

    def GetSerialNumber(self):
        return self._serial

    def GetModelName(self):
        return self._model

    def GetVendorName(self):
        return "Basler"

    def GetFullName(self):
        return f"Basler {self._model} {self._serial}"


class FakePylon:
    GrabStrategy_LatestImageOnly = "latest"
    TimeoutHandling_Return = "return"

    def __init__(self, devices=None, camera=None):
        self._devices = devices if devices is not None else [
            FakeDeviceInfo("40012345")]
        self.camera = camera or FakeCamera()
        outer = self

        class _Factory:
            @staticmethod
            def GetInstance():
                return outer

        self.TlFactory = _Factory

    def EnumerateDevices(self):
        return self._devices

    def CreateDevice(self, info):
        return info

    def InstantCamera(self, device):
        return self.camera


def basler(**kw):
    pylon = FakePylon(**kw)
    return cameras.BaslerBackend(sdk=pylon), pylon


# ======================================================================
# AOI arithmetic
# ======================================================================
def test_fit_roi_snaps_down_to_the_increment():
    limits = Limits(width=640, height=480, inc_x=4, inc_y=2, inc_w=4,
                    inc_h=2, min_w=16, min_h=16)
    got = fit_roi(Roi(7, 5, 101, 51), limits)
    assert got.as_tuple() == (4, 4, 100, 50)


def test_fit_roi_keeps_the_window_on_the_sensor():
    limits = Limits(width=640, height=480, inc_x=4, inc_y=2, inc_w=4,
                    inc_h=2, min_w=16, min_h=16)
    got = fit_roi(Roi(600, 400, 400, 400), limits)
    assert got.x + got.w <= 640 and got.y + got.h <= 480


def test_fit_roi_origin_stays_aligned_after_clamping():
    """Clamping must not undo the snapping.

    Snap-then-clamp can leave an origin that is inside the sensor but not a
    multiple of the increment, which the camera then refuses.
    """
    # The size increment is 1 and the origin increment is 8, so the furthest
    # legal origin (1000-37 = 963) is NOT itself a multiple of 8. Snapping the
    # requested origin and clamping afterwards lands exactly on 963 -- inside
    # the sensor, correctly sized, and refused by the camera for being
    # unaligned. With equal increments both orders agree and prove nothing.
    limits = Limits(width=1000, height=1000, inc_x=8, inc_y=8, inc_w=1,
                    inc_h=1, min_w=1, min_h=1)
    got = fit_roi(Roi(995, 995, 37, 37), limits)
    assert got.x % 8 == 0 and got.y % 8 == 0, f"unaligned origin {got}"
    assert got.x + got.w <= 1000 and got.y + got.h <= 1000


def test_empty_roi_means_the_whole_sensor():
    limits = Limits(width=640, height=480)
    assert fit_roi(Roi(), limits).as_tuple() == (0, 0, 640, 480)


def test_negative_roi_is_refused_at_construction():
    with pytest.raises(ValueError):
        Roi(-1, 0, 10, 10)


# ======================================================================
# The ordering trap
# ======================================================================
def test_the_fake_really_does_reject_the_naive_order():
    """Proof the fake has teeth.

    Growing the window before moving the origin back is what a naive
    implementation does, and a real pylon refuses it. If this test ever stops
    raising, the fake stopped enforcing the constraint and every AOI test
    below became worthless.
    """
    camera = FakeCamera()
    # Even reaching the starting state needs the size shrunk before the origin
    # moves -- which is the same trap, one level down.
    camera.Width.SetValue(1000)
    camera.OffsetX.SetValue(2000)
    with pytest.raises(RuntimeError, match="overruns the sensor"):
        camera.Width.SetValue(SENSOR_W)      # size before offset -- refused


def test_set_roi_moves_from_a_small_window_back_to_full_frame():
    """The move the naive order cannot make."""
    backend, pylon = basler()
    device = backend.open(CameraInfo("basler", "40012345"))
    device.set_roi(Roi(2000, 1000, 1000, 1000))
    assert device.roi().as_tuple() == (2000, 1000, 1000, 1000)

    got = device.set_roi(Roi(0, 0, SENSOR_W, SENSOR_H))
    assert got.as_tuple() == (0, 0, SENSOR_W, SENSOR_H)


def test_set_roi_snaps_an_unaligned_request_instead_of_raising():
    """A mouse drag lands on odd pixels; the camera still gets a legal AOI."""
    backend, _ = basler()
    device = backend.open(CameraInfo("basler", "40012345"))
    got = device.set_roi(Roi(101, 101, 333, 333))
    assert got.x % 4 == 0 and got.w % 4 == 0
    assert got.y % 2 == 0 and got.h % 2 == 0


def test_set_roi_reports_what_the_camera_took_not_what_was_asked():
    backend, _ = basler()
    device = backend.open(CameraInfo("basler", "40012345"))
    asked = Roi(101, 101, 333, 333)
    got = device.set_roi(asked)
    assert got.as_tuple() != asked.as_tuple()
    assert got.as_tuple() == device.roi().as_tuple()


# ======================================================================
# The buffer-reuse trap
# ======================================================================
def test_read_copies_the_frame_out_of_the_pylon_buffer():
    """The frame must survive Release handing its buffer to the next grab."""
    backend, pylon = basler()
    device = backend.open(CameraInfo("basler", "40012345"))
    device.start()
    frame = device.read()
    assert frame is not None
    assert int(frame.image[0][0]) == 7          # what the grab held
    # Release already overwrote the buffer with 99. A view would show 99.
    assert not np.any(frame.image == 99), "frame is a view into a reused buffer"


def test_read_releases_the_grab_even_when_it_failed():
    """A failed grab must not leak its buffer."""
    backend, pylon = basler()
    device = backend.open(CameraInfo("basler", "40012345"))
    held = {}

    def retrieve(timeout, handling):
        held["grab"] = FakeGrab(np.zeros((2, 2), np.uint8), ok=False)
        return held["grab"]

    pylon.camera.RetrieveResult = retrieve
    with pytest.raises(CameraError):
        device.read()
    assert held["grab"].released, "a failed grab was never released"


def test_start_asks_for_the_newest_frame_not_a_queue():
    backend, pylon = basler()
    device = backend.open(CameraInfo("basler", "40012345"))
    device.start()
    assert pylon.camera.strategy == FakePylon.GrabStrategy_LatestImageOnly


# ======================================================================
# Discovery
# ======================================================================
def test_discovery_says_why_a_backend_was_skipped():
    """An empty list with no explanation sends the user to check cables."""
    found = cameras.discover([cameras.BaslerBackend(sdk=None),
                              cameras.EvkBackend(sdk=None)])
    assert found.cameras == []
    assert any("basler" in n for n in found.notes)
    assert any("prophesee" in n for n in found.notes)
    assert any("pypylon" in n for n in found.notes)


def test_pypylon_present_but_nothing_enumerated_names_the_grabber():
    """The CoaXPress symptom, explained where the user will read it."""
    backend = cameras.BaslerBackend(sdk=FakePylon(devices=[]))
    found = cameras.discover([backend])
    assert found.cameras == []
    assert any("grabber" in n for n in found.notes), found.notes


def test_discovery_finds_a_basler_and_labels_it():
    backend, _ = basler()
    found = cameras.discover([backend])
    assert len(found.cameras) == 1
    assert found.cameras[0].kind == "frame"
    assert "boA5320-150cm" in found.cameras[0].label


def test_open_camera_refuses_an_unknown_backend():
    with pytest.raises(CameraError):
        cameras.open_camera(CameraInfo("nonesuch", "x"), [])


# ======================================================================
# Events
# ======================================================================
def test_accumulate_marks_both_polarities_against_a_neutral_field():
    img = accumulate_events([1, 2], [0, 0], [1, 0], 4, 2)
    assert img[0][1] == 255 and img[0][2] == 0
    assert img[1][0] == cameras.EVENT_MID


def test_accumulate_drops_out_of_range_events_rather_than_wrapping():
    """A negative x would index from the far edge and paint the wrong side."""
    img = accumulate_events([-1, 99], [0, 0], [1, 1], 4, 2)
    assert not np.any(img == 255), "an out-of-range event was drawn"


def test_accumulate_with_no_events_is_a_neutral_field():
    img = accumulate_events([], [], [], 3, 3)
    assert img.shape == (3, 3) and np.all(img == cameras.EVENT_MID)


# ======================================================================
# Prophesee
# ======================================================================
class FakeRoi:
    def __init__(self):
        self.window = None
        self.enabled = False

    def Window(self, x, y, w, h):
        return (x, y, w, h)

    def set_window(self, window):
        self.window = window

    def enable(self, on):
        self.enabled = bool(on)


class FakeEvkDevice:
    def __init__(self, w=1280, h=720):
        self._roi = FakeRoi()
        self._geo = type("G", (), {"get_width": lambda s: w,
                                   "get_height": lambda s: h})()

    def get_i_roi(self):
        return self._roi

    def get_i_geometry(self):
        return self._geo

    def get_i_events_stream(self):
        return None

    def get_i_events_stream_decoder(self):
        return None


def test_evk_geometry_is_the_imx636_sensor():
    device = cameras.EvkDevice(CameraInfo("prophesee", "s", kind="event"),
                               FakeEvkDevice())
    limits = device.limits()
    assert (limits.width, limits.height) == (1280, 720)


def test_evk_roi_is_written_to_the_sensor_and_enabled():
    """A hardware ROI stops masked pixels emitting; a crop would not."""
    raw = FakeEvkDevice()
    device = cameras.EvkDevice(CameraInfo("prophesee", "s", kind="event"), raw)
    got = device.set_roi(Roi(100, 50, 200, 100))
    assert raw._roi.window == (100, 50, 200, 100)
    assert raw._roi.enabled is True
    assert got.as_tuple() == (100, 50, 200, 100)


def test_evk_reports_no_exposure_range():
    """An event sensor has no exposure; a view must be able to tell."""
    device = cameras.EvkDevice(CameraInfo("prophesee", "s", kind="event"),
                               FakeEvkDevice())
    assert device.limits().exposure_us == (0.0, 0.0)


# ======================================================================
# Synthetic
# ======================================================================
def test_the_simulated_backend_is_always_available():
    assert cameras.SyntheticBackend().available() is True


def test_the_simulated_cameras_are_labelled_simulated():
    """It must never be mistakable for a real camera."""
    for info in cameras.SyntheticBackend().discover():
        assert "simulated" in info.label.lower()


def test_simulated_event_frames_carry_an_event_count():
    backend = cameras.SyntheticBackend()
    info = [c for c in backend.discover() if c.kind == "event"][0]
    device = backend.open(info)
    device.start()
    frame = device.read()
    assert frame.meta["kind"] == "event"
    assert frame.meta["events"] > 0


def test_simulated_frames_follow_the_roi():
    backend = cameras.SyntheticBackend()
    info = [c for c in backend.discover() if c.kind == "frame"][0]
    device = backend.open(info)
    device.set_roi(Roi(0, 0, 64, 32))
    device.start()
    assert device.read().size == (64, 32)
