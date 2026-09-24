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
import time
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
        self.destroyed = False
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

    def DestroyDevice(self):
        self.destroyed = True

    def StartGrabbing(self, strategy=None):
        self.grabbing, self.strategy = True, strategy

    def StopGrabbing(self):
        self.grabbing = False

    def RetrieveResult(self, timeout, handling):
        self._buffer[:] = 7
        return FakeGrab(self._buffer, ok=True)


class FakeGrab:
    """A grab result shaped like the real one.

    `.Array` returns a COPY, which is what pypylon actually does — measured:
    an array held across Release() and five further grabs was unchanged. And
    a TIMEOUT is a valid Python object whose IsValid() is False, never None.
    This fake asserted both the other way round, which is how the backend
    came to test `if grab is None` (dead code) and pay for a second memcpy.
    """

    def __init__(self, buffer, ok=True, valid=True):
        self._buffer, self._ok, self._valid = buffer, ok, valid
        self.released = False

    def IsValid(self):
        return self._valid

    @property
    def Array(self):
        return self._buffer.copy()   # pypylon hands back pixels you own

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
def test_read_returns_the_pixels_that_were_grabbed():
    """And they survive Release, because .Array already owns them."""
    backend, pylon = basler()
    device = backend.open(CameraInfo("basler", "40012345"))
    device.start()
    frame = device.read()
    assert frame is not None
    assert int(frame.image[0][0]) == 7
    assert not np.any(frame.image == 99), "the frame tracked a reused buffer"


def test_a_timed_out_grab_is_none_rather_than_an_exception():
    """A TIMEOUT IS NOT None — it is a result whose IsValid() is False.

    Measured: three 1 ms retrievals against the pylon emulator each returned
    a GrabResult object, never None. Calling GrabSucceeded() on one throws,
    so a backend that only tests `grab is None` dies on its first slow frame
    — which on a live view is the first frame the display cannot keep up with.
    """
    backend, pylon = basler()
    device = backend.open(CameraInfo("basler", "40012345"))
    device.start()
    timed_out = FakeGrab(np.zeros((2, 2), np.uint8), valid=False)
    pylon.camera.RetrieveResult = lambda t, h: timed_out
    assert device.read() is None
    assert timed_out.released, "a timed-out result was never released"


def test_reading_a_stopped_camera_is_none_not_an_exception():
    """RetrieveResult on a camera that is not grabbing throws."""
    backend, _ = basler()
    device = backend.open(CameraInfo("basler", "40012345"))
    assert device.read() is None


def test_close_releases_the_device_not_just_the_camera():
    """On CoaXPress the grabber channel is exclusive per process.

    Close() alone leaves the device attached until Python collects it, which
    can leave the camera unopenable by the next run or the pylon Viewer.
    """
    backend, pylon = basler()
    device = backend.open(CameraInfo("basler", "40012345"))
    device.close()
    assert pylon.camera.closed and pylon.camera.destroyed


def test_read_releases_the_grab_even_when_it_failed():
    """A failed grab must not leak its buffer."""
    backend, pylon = basler()
    device = backend.open(CameraInfo("basler", "40012345"))
    held = {}

    def retrieve(timeout, handling):
        held["grab"] = FakeGrab(np.zeros((2, 2), np.uint8), ok=False)
        return held["grab"]

    pylon.camera.RetrieveResult = retrieve
    device.start()
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
    said = " ".join(found.notes)
    # Naming the ACTUAL missing piece. Since pypylon 4.0.0 the CXP GenTL
    # producer is not in the Windows wheel, so "install the driver" is not
    # the advice that unblocks anyone.
    assert "pylon Software Suite" in said and "CXP Camera Support" in said, said


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
# THESE FAKES MODEL THE DOCUMENTED API, NOT THE ONE THIS FILE FIRST INVENTED.
# The first version of the backend pulled events with decoder.get_cd_events(),
# which does not exist, and the fake implemented it -- so the tests passed
# while a real EVK4 would have opened, reported 1280x720 and then reported
# silence for ever. Decoding is PUSH: decode() fans buffers out to callbacks
# registered on the CD decoder, events carry ABSOLUTE sensor coordinates, and
# the ROI setters report success as a bool rather than raising.
EVENT_DTYPE = np.dtype([("x", "<u2"), ("y", "<u2"),
                        ("p", "<i2"), ("t", "<i8")])


def events(*triples, t0=1000):
    """Build a CD buffer: (x, y, polarity) triples."""
    buf = np.zeros(len(triples), dtype=EVENT_DTYPE)
    for i, (x, y, p) in enumerate(triples):
        buf[i] = (x, y, p, t0 + i)
    return buf


def stamped(*quads):
    """Build a CD buffer with explicit times: (x, y, polarity, t) tuples."""
    buf = np.zeros(len(quads), dtype=EVENT_DTYPE)
    for i, quad in enumerate(quads):
        buf[i] = quad
    return buf


class FakeCdDecoder:
    """I_EventCDDecoder: callbacks in, nothing out."""

    def __init__(self):
        self.callbacks = []

    def add_event_buffer_callback(self, fn):
        self.callbacks.append(fn)


class FakeStreamDecoder:
    """I_EventsStreamDecoder.decode() dispatches; it returns nothing."""

    def __init__(self, cd, script=()):
        self._cd = cd
        self.script = list(script)
        self.decoded = 0

    def decode(self, raw):
        self.decoded += 1
        if not self.script:
            return                      # decoded nothing; delivers nothing
        buf = self.script.pop(0)
        for fn in self._cd.callbacks:
            fn(buf)


class FakeStream:
    """I_EventsStream: poll_buffer() < 0 means the stream ENDED.

    `calls` records the order of everything that matters to a raw recording.
    Like the real SDK, log_raw_data returns a bool and creates the file, and
    only a PULLED buffer (get_latest_raw_data) reaches it.
    """

    def __init__(self, polls=None, log_ok=True):
        self.polls = list(polls) if polls is not None else [1]
        self.started = False
        self.stopped = False
        self.calls = []
        self.logging = None
        self.log_ok = log_ok

    def start(self):
        self.started = True
        self.calls.append("start")

    def stop(self):
        self.stopped = True
        self.calls.append("stop")

    def poll_buffer(self):
        # QUIET once the script runs out, not "data ready" for ever: the real
        # loop runs for the whole accumulation window, so a fake that always
        # has data decodes thousands of times and no count is predictable.
        return self.polls.pop(0) if self.polls else 0

    def get_latest_raw_data(self):
        self.calls.append("pull")
        if self.logging is not None:
            with open(self.logging, "ab") as handle:
                handle.write(b"raw")
        return b"raw"

    def log_raw_data(self, path):
        self.calls.append("log")
        if not self.log_ok:
            return False
        open(path, "wb").close()
        self.logging = path
        return True

    def stop_log_raw_data(self):
        self.calls.append("stop_log")
        self.logging = None


class FakeRoi:
    class Mode:
        ROI = "roi"

    def __init__(self, accept=True):
        self.window = None
        self.enabled = False
        self.mode = None
        self._accept = accept

    def set_mode(self, mode):
        self.mode = mode

    def Window(self, x, y, w, h):
        return (x, y, w, h)

    def set_window(self, window):
        if not self._accept:
            return False
        self.window = window
        return True

    def enable(self, on):
        if not self._accept:
            return False
        self.enabled = bool(on)
        return True


class FakeEvkDevice:
    def __init__(self, w=1280, h=720, polls=None, script=(), accept=True,
                 omit=(), log_ok=True):
        self._roi = FakeRoi(accept)
        self._geo = type("G", (), {"get_width": lambda s: w,
                                   "get_height": lambda s: h})()
        self._cd = FakeCdDecoder()
        self._stream = FakeStream(polls, log_ok=log_ok)
        self._decoder = FakeStreamDecoder(self._cd, script)
        for name in omit:                 # simulate an SDK that lacks one
            setattr(self, name, None)

    def get_i_roi(self):
        return self._roi

    def get_i_geometry(self):
        return self._geo

    def get_i_events_stream(self):
        return self._stream

    def get_i_events_stream_decoder(self):
        return self._decoder

    def get_i_event_cd_decoder(self):
        return self._cd


def evk(**kw):
    raw = FakeEvkDevice(**kw)
    return cameras.EvkDevice(CameraInfo("prophesee", "s", kind="event"),
                             raw), raw


def test_evk_geometry_is_the_imx636_sensor():
    device, _ = evk()
    limits = device.limits()
    assert (limits.width, limits.height) == (1280, 720)


def test_evk_reports_no_exposure_range():
    """An event sensor has no exposure; a view must be able to tell."""
    device, _ = evk()
    assert device.limits().exposure_us == (0.0, 0.0)


def test_evk_subscribes_to_cd_events_when_it_opens():
    """The ONLY way decoded events are delivered.

    Without this the pipeline runs end to end and produces nothing, which is
    exactly what the first version of this backend did.
    """
    _, raw = evk()
    assert raw._cd.callbacks, "no CD callback was ever registered"


def test_a_missing_interface_is_refused_loudly():
    """Not defaulted to None.

    Silently defaulting is how a fatal mistake became a camera that looked
    healthy and stayed quiet.
    """
    with pytest.raises(CameraError, match="get_i_event_cd_decoder"):
        evk(omit=("get_i_event_cd_decoder",))


def test_evk_roi_is_written_to_the_sensor_and_enabled():
    """A hardware ROI stops masked pixels emitting; a crop would not."""
    device, raw = evk()
    got = device.set_roi(Roi(100, 50, 200, 100))
    assert raw._roi.window == (100, 50, 200, 100)
    assert raw._roi.enabled is True
    assert raw._roi.mode == FakeRoi.Mode.ROI
    assert got.as_tuple() == (100, 50, 200, 100)


def test_a_refused_roi_is_not_reported_as_applied():
    """set_window/enable report failure with a BOOL, not an exception.

    Ignoring it makes a refused window indistinguishable from an applied one
    — and the refused one's size would then be used to shape every image.
    """
    device, _ = evk(accept=False)
    with pytest.raises(CameraError, match="refused"):
        device.set_roi(Roi(100, 50, 200, 100))
    assert device.roi().as_tuple() == (0, 0, 1280, 720)


def test_events_are_binned_relative_to_the_roi_origin():
    """IMX636 masks pixels in place; survivors keep ABSOLUTE coordinates.

    Binning raw x/y into a window-sized image puts every event outside it —
    a blank picture from a working camera, the moment the origin is not 0,0.
    """
    device, _ = evk(script=[events((105, 55, 1), (106, 56, 0))])
    device.set_roi(Roi(100, 50, 40, 20))
    device.start()
    frame = device.read(50)
    assert frame is not None, "a working camera produced no frame"
    assert frame.image.shape == (20, 40)
    assert frame.image[5][5] == 255, "the positive event landed nowhere"
    assert frame.image[6][6] == 0


def test_a_lost_camera_is_reported_not_treated_as_quiet():
    """poll_buffer() < 0 means the stream ended.

    Treating it as no-data-yet spins at a kilohertz for ever while reporting
    a healthy camera that simply has nothing to say.
    """
    device, _ = evk(polls=[-1])
    device.start()
    with pytest.raises(CameraError, match="disconnected"):
        device.read(50)


def test_the_last_events_before_the_end_are_not_thrown_away():
    """poll_buffer going negative must not discard what was already decoded.

    Measured against a real stream: it goes 0, 1, then -1 for ever. Raising
    the moment -1 appears threw away that window's events — the last ones
    before a camera was unplugged, which are the ones most worth having.
    """
    device, _ = evk(polls=[1, -1], script=[events((5, 5, 1), (6, 6, 0))])
    device.start()
    frame = device.read(50)
    assert frame is not None, "the final window was discarded"
    assert frame.meta["events"] == 2
    # Only once nothing is left does it report the end.
    with pytest.raises(CameraError, match="ended"):
        device.read(50)


def test_a_quiet_window_is_none_rather_than_a_blank_image():
    device, _ = evk(polls=[0, 0, 0, 0])
    device.start()
    assert device.read(5) is None


def test_the_event_buffer_is_copied_out_of_the_decoder():
    """The decoder owns that memory and hands the same block to the next
    callback — the pylon Release() hazard, on the other vendor's SDK."""
    device, raw = evk()
    buf = events((1, 1, 1))
    for fn in raw._cd.callbacks:
        fn(buf)
    buf["x"] = 999                      # the decoder reuses it
    held = device._drain()
    assert held and int(held[0]["x"][0]) == 1, "the backend kept a view"


def test_event_frames_carry_the_event_count_and_rate():
    device, _ = evk(script=[events((10, 10, 1), (11, 11, 1), (12, 12, 0))])
    device.start()
    frame = device.read(50)
    assert frame.meta["kind"] == "event"
    assert frame.meta["events"] == 3
    assert frame.meta["window_ms"] == cameras.DEFAULT_ACCUMULATE_MS


def test_stopping_drops_events_that_arrived_too_late():
    device, raw = evk()
    device.start()
    for fn in raw._cd.callbacks:
        fn(events((1, 1, 1)))
    device.stop()
    assert device._drain() == []


# ======================================================================
# EVK: recording the .raw
# ======================================================================
def test_a_frame_camera_has_no_raw_stream():
    assert cameras.Device.records_raw is False
    with pytest.raises(CameraError, match="no raw stream"):
        cameras.Device().start_raw("x.raw")
    assert cameras.Device().stop_raw() is None


def test_an_event_camera_records_its_raw(tmp_path):
    device, raw = evk()
    assert device.records_raw is True
    path = device.start_raw(tmp_path / "run_events.raw")
    assert path.exists() and device.raw_path == path


def test_the_raw_log_starts_before_the_stream(tmp_path):
    """Started mid-stream, the file begins at a buffer boundary and a replay
    drops everything before its first time marker. Started first, it has the
    whole run."""
    device, raw = evk()
    device.start_raw(tmp_path / "r.raw")
    device.start()
    assert raw._stream.calls[:2] == ["log", "start"]


def test_an_existing_file_is_never_overwritten(tmp_path):
    """log_raw_data truncates silently: measured 1,700,147 bytes to 208."""
    target = tmp_path / "r.raw"
    target.write_bytes(b"the user data")
    device, raw = evk()
    with pytest.raises(CameraError, match="already exists"):
        device.start_raw(target)
    assert target.read_bytes() == b"the user data"
    assert "log" not in raw._stream.calls


def test_a_raw_recording_must_end_in_dot_raw(tmp_path):
    """Metavision's readers treat any other name as a camera serial."""
    device, _ = evk()
    with pytest.raises(CameraError, match="must end in .raw"):
        device.start_raw(tmp_path / "r.bin")


def test_a_refused_log_is_an_error_not_a_silent_nothing(tmp_path):
    device, _ = evk(log_ok=False)
    with pytest.raises(CameraError, match="could not create"):
        device.start_raw(tmp_path / "r.raw")
    assert device.raw_path is None


def test_stopping_pulls_what_is_queued_into_the_raw_first(tmp_path):
    """stream.stop() DISCARDS buffers nobody pulled, and only a pulled buffer
    reaches the file. So the tail of the run is pulled, then the log is
    closed, and only then is the stream stopped."""
    device, raw = evk(polls=[])
    target = device.start_raw(tmp_path / "r.raw")
    device.start()
    raw._stream.polls = [1, 1, 0]            # two buffers still queued
    device.stop()
    assert raw._stream.calls[-4:] == ["pull", "pull", "stop_log", "stop"]
    assert target.read_bytes() == b"rawraw"
    assert device.raw_path is None


def test_stop_raw_on_its_own_leaves_the_stream_running(tmp_path):
    device, raw = evk(polls=[])
    device.start_raw(tmp_path / "r.raw")
    device.start()
    assert device.stop_raw() == tmp_path / "r.raw"
    assert "stop" not in raw._stream.calls
    assert device.stop_raw() is None                 # nothing left to finish


def test_windows_say_where_they_fall_in_the_raw(tmp_path):
    """Time since the .raw's first event: the one clock the live view and a
    time-shifted replay of the file agree on."""
    device, raw = evk(polls=[1],
                      script=[stamped((1, 1, 1, 5000)),
                              stamped((2, 2, 1, 5100), (3, 3, 0, 25000))])
    device.start_raw(tmp_path / "r.raw")
    device.start()
    first = device.read(5)
    raw._stream.polls = [1]          # the second buffer, in the next window
    second = device.read(5)
    assert first.meta["raw_t_us"] == 0
    assert second.meta["raw_t_us"] == 20000


def test_without_a_raw_recording_windows_carry_no_raw_time():
    device, _ = evk(script=[events((1, 1, 1))])
    device.start()
    assert "raw_t_us" not in device.read(50).meta


def test_the_decoder_callback_does_not_hold_the_device():
    """A bound method of the device, registered with the C++ decoder, is a
    cycle Python cannot collect: measured, the device was never freed and its
    .raw stayed locked with the tail unwritten until the process exited."""
    device, raw = evk()
    assert raw._cd.callbacks
    for fn in raw._cd.callbacks:
        assert getattr(fn, "__self__", None) is not device


def test_closing_lets_go_of_the_sdk_objects():
    device, _ = evk()
    device.close()
    assert device._dev is None and device._stream is None


# ======================================================================
# Synthetic
# ======================================================================
def test_the_simulated_backend_is_always_available():
    assert cameras.SyntheticBackend().available() is True


def test_the_simulated_cameras_are_labelled_simulated():
    """It must never be mistakable for a real camera."""
    for info in cameras.SyntheticBackend().discover():
        assert "simulated" in info.label.lower()


def test_the_simulated_event_camera_is_paced_like_one():
    """Unpaced it produced 2,500 windows a second and filled a folder with
    1,500 PNGs in three seconds. An EVK4 gives one window per 20 ms."""
    backend = cameras.SyntheticBackend()
    info = [c for c in backend.discover() if c.kind == "event"][0]
    device = backend.open(info)
    device.start()
    began = time.monotonic()
    for _ in range(6):
        assert device.read() is not None
    elapsed = time.monotonic() - began
    assert 0.08 <= elapsed <= 0.5, elapsed


def test_a_simulated_read_honours_its_timeout():
    backend = cameras.SyntheticBackend()
    info = [c for c in backend.discover() if c.kind == "frame"][0]
    device = backend.open(info)
    device.start()
    device.read()
    assert device.read(timeout_ms=1) is None        # next frame is 33 ms away


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
