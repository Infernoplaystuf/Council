"""
council_core.cameras — Basler frame cameras and Prophesee event cameras,
behind one interface, with no toolkit imported.

Two very different sensors are reachable from here. A Basler boA5320-150cm is a
FRAME camera: it hands back a 2D array of pixels on a trigger or free-run. A
Prophesee EVK4 is an EVENT camera: it has no frames at all, only a stream of
(x, y, polarity, timestamp) records emitted per-pixel as brightness changes.
The app wants to show both, so an event stream is binned into an image here —
but the binning is named for what it is, and the event-native numbers (event
rate, the time span actually covered) travel WITH the image rather than being
thrown away. A viewer that shows an event camera as "30 fps" is lying; what it
has is 30 accumulation windows per second over a stream with its own rate.

NEITHER SDK IS INSTALLED ON THE MACHINE THIS WAS WRITTEN ON
So every SDK call goes through an injected module (`sdk=`), and the tests pass
fakes that enforce the constraints the real hardware enforces: pylon rejecting
an unaligned AOI, and a grab result's buffer being REUSED after Release. That
makes the Basler and Prophesee paths genuinely exercised rather than merely
written. It does not make them hardware-verified, and nothing here claims that
— `available()` tells the truth about a machine with no SDK on it.

THE AOI IS SET ON THE CAMERA, NOT CROPPED FROM THE IMAGE
Cropping a full frame in software moves the same pixels over the link and then
throws most of them away. On a boA5320-150cm the whole point of a small AOI is
the frame rate it buys, which you only get if the sensor reads out less. So
`set_roi` writes the camera's own AOI registers.

  ORDER MATTERS AND GETS THIS WRONG SILENTLY. Width and OffsetX are validated
  against each other on every write: with OffsetX at 2000, writing Width=4000
  is refused, because 2000+4000 overruns the sensor. Set a new AOI naively —
  offset then size, or size then offset — and roughly half of all moves throw.
  The order that always works is: OFFSETS TO ZERO, THEN SIZES, THEN OFFSETS.

  Sizes and offsets must also be multiples of the sensor's increment. We SNAP
  to the increment rather than raising, and report what was actually set, so a
  user dragging a box on screen gets the nearest legal AOI instead of an error
  dialog.

A TIMED-OUT GRAB IS NOT None, AND THAT IS THE EASY BUG HERE
`RetrieveResult(timeout, TimeoutHandling_Return)` NEVER returns None. On a
timeout it returns a perfectly ordinary GrabResult whose `IsValid()` is False —
measured against the emulator. Testing `if grab is None` therefore never fires,
and the next line calls `GrabSucceeded()` on an empty result, which throws. On
a live view the first dropped frame kills the grab loop.

`grab.Array` IS ALREADY A COPY, which this file previously got backwards. The
docstring here used to claim it was a view into a recycled buffer and defended
it with `numpy.array(..., copy=True)`. Measured: an array taken from a grab
result and held across Release() and five further grabs was unchanged. At
5328x3040 Mono12 that mistaken defence was a 32 MB memcpy per frame on a camera
rated ~150 fps. Release() still happens in a `finally`, because the RESULT
must go back to the pool even though the pixels are ours.

A MISSING FEATURE IS NOT None EITHER
`getattr(camera, "NoSuchFeature", None)` returns a PlaceholderParameter whose
IsValid() is False — never None. Every `if node is None` guard written against
the obvious assumption is dead code, and the exception then surfaces from
SetValue as "the camera refused that area", blaming the camera for a typo.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: What an event camera's accumulated image uses for "no event here".
EVENT_MID = 128

#: Default accumulation window for an event stream, in milliseconds.
DEFAULT_ACCUMULATE_MS = 20.0

#: The shortest window an event camera is asked for: 1 ms, 1000 pictures a
#: second. Shorter windows hold too few events to show anything.
MIN_ACCUMULATE_MS = 1.0


class CameraError(Exception):
    """A camera could not do what was asked."""


class CameraEnded(CameraError):
    """The source is FINISHED — not "this grab failed, try again".

    A grab loop's natural reflex is to carry on after an error, and for one
    bad frame that is right. For a camera that has been unplugged, or a
    recording that has run out, every subsequent read raises the same thing
    and "carry on" becomes a spin. Measured: 737,070 errors in six seconds
    when a finished recording was treated as recoverable.
    """


# ======================================================================
# Value types
# ======================================================================
@dataclass(frozen=True)
class Roi:
    """An area of interest, in sensor pixels."""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    def __post_init__(self) -> None:
        for name in ("x", "y", "w", "h"):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"roi.{name} cannot be negative")

    @property
    def empty(self) -> bool:
        return self.w <= 0 or self.h <= 0

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)


@dataclass(frozen=True)
class Limits:
    """What a particular sensor will accept.

    `inc_*` are the increments every AOI value must be a multiple of. They are
    1 on a sensor with no constraint, which keeps the snapping arithmetic
    identical instead of special-cased.
    """
    width: int = 0
    height: int = 0
    inc_x: int = 1
    inc_y: int = 1
    inc_w: int = 1
    inc_h: int = 1
    min_w: int = 1
    min_h: int = 1
    exposure_us: Tuple[float, float] = (0.0, 0.0)
    gain: Tuple[float, float] = (0.0, 0.0)


@dataclass
class Frame:
    """One image on its way to a display.

    `image` is always a numpy array this module owns outright — never a view
    into an SDK buffer.
    """
    image: Any
    index: int = 0
    timestamp_us: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> Tuple[int, int]:
        h, w = self.image.shape[0], self.image.shape[1]
        return (int(w), int(h))


@dataclass(frozen=True)
class CameraInfo:
    """One camera that could be opened."""
    backend: str
    key: str
    model: str = ""
    serial: str = ""
    vendor: str = ""
    #: "frame" or "event" — the two behave differently enough that a view
    #: needs to know which it has before it draws anything.
    kind: str = "frame"

    @property
    def label(self) -> str:
        bits = [b for b in (self.vendor, self.model) if b]
        head = " ".join(bits) or self.key
        return f"{head} ({self.serial})" if self.serial else head


# ======================================================================
# AOI arithmetic — the part that is wrong everywhere else
# ======================================================================
def snap(value: int, inc: int, low: int = 0) -> int:
    """`value` rounded DOWN to a multiple of `inc`, never below `low`."""
    inc = max(1, int(inc))
    snapped = (int(value) // inc) * inc
    return max(int(low), snapped)


def fit_roi(roi: Roi, limits: Limits) -> Roi:
    """The nearest AOI the sensor will actually accept.

    Snapped to the increments and clamped inside the sensor. An empty or
    out-of-range request becomes the full sensor rather than an error: the
    caller is usually a mouse drag, and a dialog is a worse answer than the
    obvious one.
    """
    full = Roi(0, 0, int(limits.width), int(limits.height))
    if roi.empty or not limits.width or not limits.height:
        return full

    w = snap(min(int(roi.w), limits.width), limits.inc_w, limits.min_w)
    h = snap(min(int(roi.h), limits.height), limits.inc_h, limits.min_h)
    w = max(w, snap(limits.min_w, limits.inc_w, limits.min_w))
    h = max(h, snap(limits.min_h, limits.inc_h, limits.min_h))

    # Clamp the origin so the window stays on the sensor, THEN snap it. Snap
    # first and a clamp afterwards can leave an unaligned value.
    x = snap(min(int(roi.x), max(0, limits.width - w)), limits.inc_x)
    y = snap(min(int(roi.y), max(0, limits.height - h)), limits.inc_y)
    return Roi(x, y, w, h)


def accumulate_events(xs: Sequence[int], ys: Sequence[int],
                      pols: Sequence[int], width: int, height: int,
                      np_mod: Any = None) -> Any:
    """Bin an event batch into a grayscale image.

    Mid-gray is "nothing happened here", bright is a positive contrast change,
    dark is negative. This is a VIEWING convenience, not a measurement: an
    event camera has no exposure and no frames, and two events at the same
    pixel inside one window are not distinguishable in the result.
    """
    np = np_mod or _numpy()
    img = np.full((int(height), int(width)), EVENT_MID, dtype=np.uint8)
    if len(xs) == 0:
        return img
    xi = np.asarray(xs, dtype=np.int64)
    yi = np.asarray(ys, dtype=np.int64)
    pi = np.asarray(pols, dtype=np.int64)
    # Events outside the frame are DROPPED, not wrapped. A negative or
    # oversized coordinate indexes from the far edge in numpy, which would
    # paint speckle at the opposite side of the image.
    keep = (xi >= 0) & (xi < int(width)) & (yi >= 0) & (yi < int(height))
    xi, yi, pi = xi[keep], yi[keep], pi[keep]
    img[yi[pi > 0], xi[pi > 0]] = 255
    img[yi[pi <= 0], xi[pi <= 0]] = 0
    return img


def _numpy() -> Any:
    try:
        import numpy
    except ImportError as exc:                              # pragma: no cover
        raise CameraError("numpy is required to handle camera frames") from exc
    return numpy


# ======================================================================
# The device interface
# ======================================================================
class Device:
    """One opened camera. Subclasses talk to an SDK; this defines the shape."""

    info: CameraInfo

    #: Whether this camera can record its own raw stream to a file. True for
    #: event cameras, whose .raw is every event the sensor sent; a frame
    #: camera has no such stream, and its saved frames ARE the data.
    records_raw = False

    #: Whether stop() then start() on the same open device is safe. A frame
    #: camera's grabs are independent, so yes. An EVK4's decoder carries its
    #: state across a restart and Python cannot reset it (see EvkDevice), so
    #: its stream runs from the first start until the device is closed.
    restartable = True

    def start_raw(self, path: Any) -> Path:
        """Begin recording the camera's raw stream into `path`."""
        raise CameraError("this camera has no raw stream to record")

    def stop_raw(self) -> Optional[Path]:
        """Finish the raw recording. Returns its path, or None if none ran."""
        return None

    @property
    def raw_path(self) -> Optional[Path]:
        """The raw recording in progress, or None."""
        return None

    def limits(self) -> Limits:
        raise NotImplementedError

    def roi(self) -> Roi:
        raise NotImplementedError

    def set_roi(self, roi: Roi) -> Roi:
        """Apply an AOI and return what the camera actually took."""
        raise NotImplementedError

    def set_exposure_us(self, value: float) -> float:
        return 0.0

    def set_gain(self, value: float) -> float:
        return 0.0

    def set_frame_rate(self, fps: float) -> float:
        """Ask for `fps` pictures a second; 0 means the camera's own default.
        Returns the rate actually in effect, 0.0 if this camera cannot say."""
        return 0.0

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def read(self, timeout_ms: int = 1000) -> Optional[Frame]:
        """The next frame, or None if none arrived inside the timeout."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class Backend:
    """A family of cameras reachable through one SDK."""

    name = "backend"
    kind = "frame"
    #: What to tell a user who has no SDK installed.
    install_hint = ""

    def available(self) -> bool:
        raise NotImplementedError

    def discover(self) -> List[CameraInfo]:
        raise NotImplementedError

    def open(self, info: CameraInfo) -> Device:
        raise NotImplementedError


# ======================================================================
# Basler — pypylon
# ======================================================================
class BaslerBackend(Backend):
    """Basler frame cameras through pypylon.

    A boA5320-150cm is a CoaXPress camera. pypylon reaches it through the CXP
    transport layer, which needs a frame grabber and its driver installed —
    the SDK importing successfully says nothing about whether a CXP camera
    will enumerate. `discover()` returning empty with pypylon present is the
    normal symptom of a missing or unbound grabber, and is reported as such
    rather than as "no cameras".
    """

    name = "basler"
    kind = "frame"
    install_hint = "pip install pypylon (plus the pylon runtime; a CoaXPress "
    install_hint += "camera also needs its frame-grabber driver)"

    def __init__(self, sdk: Any = None):
        self._sdk = sdk

    def sdk(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        try:
            from pypylon import pylon
        except ImportError as exc:
            raise CameraError(f"pypylon is not installed: {exc}") from exc
        self._sdk = pylon
        return self._sdk

    def available(self) -> bool:
        try:
            self.sdk()
        except CameraError:
            return False
        return True

    def discover(self) -> List[CameraInfo]:
        try:
            pylon = self.sdk()
        except CameraError:
            return []
        try:
            tlf = pylon.TlFactory.GetInstance()
            devices = tlf.EnumerateDevices()
        except Exception as exc:                            # noqa: BLE001
            raise CameraError(f"pylon could not enumerate: {exc}") from exc
        out: List[CameraInfo] = []
        for dev in devices or []:
            serial = _call(dev, "GetSerialNumber")
            out.append(CameraInfo(
                backend=self.name,
                key=serial or _call(dev, "GetFullName") or "?",
                model=_call(dev, "GetModelName"),
                serial=serial,
                vendor=_call(dev, "GetVendorName") or "Basler",
                kind="frame"))
        return out

    def open(self, info: CameraInfo) -> "BaslerDevice":
        pylon = self.sdk()
        tlf = pylon.TlFactory.GetInstance()
        target = None
        for dev in tlf.EnumerateDevices() or []:
            if (_call(dev, "GetSerialNumber") == info.key
                    or _call(dev, "GetFullName") == info.key):
                target = dev
                break
        if target is None:
            raise CameraError(f"camera {info.key!r} is no longer attached")
        try:
            camera = pylon.InstantCamera(tlf.CreateDevice(target))
            camera.Open()
        except Exception as exc:                            # noqa: BLE001
            # The characteristic CoaXPress failure is the card already being
            # held — by the pylon Viewer, or by a previous run of this app
            # that exited without DestroyDevice. It arrives as a raw GenICam
            # exception, which every `except CameraError` in the app misses.
            raise CameraError(
                f"could not open {info.label}: {exc}. On a CoaXPress camera "
                f"this usually means something else is holding the frame "
                f"grabber — close the pylon Viewer and try again") from exc
        return BaslerDevice(info, camera, pylon)


class BaslerDevice(Device):
    """An open pylon InstantCamera."""

    def __init__(self, info: CameraInfo, camera: Any, pylon: Any):
        self.info = info
        self._cam = camera
        self._pylon = pylon
        self._index = 0
        self._started = False

    # -- node map helpers ------------------------------------------------
    def _node(self, name: str) -> Any:
        """A camera feature, or None if this model has not got it.

        `getattr` alone is not enough: pypylon answers an unknown feature name
        with a PlaceholderParameter rather than raising or returning None, and
        IsValid() is the only thing that tells them apart. Measured.
        """
        node = getattr(self._cam, name, None)
        if node is None:
            return None
        valid = getattr(node, "IsValid", None)
        if valid is not None and not valid():
            return None
        return node

    def _value(self, name: str, default: Any = 0) -> Any:
        node = self._node(name)
        if node is None:
            return default
        try:
            return node.GetValue()
        except Exception:                                   # noqa: BLE001
            return default

    def _set(self, name: str, value: Any) -> None:
        """Write a feature this camera actually has. Absent ones are skipped.

        Skipping matters: a feature name this model lacks would otherwise
        throw out of SetValue and be reported as "the camera refused that
        area" — blaming the camera for a feature it was never asked about.
        """
        node = self._node(name)
        if node is None:
            return
        writable = getattr(node, "IsWritable", None)
        if writable is not None and not writable():
            return
        node.SetValue(value)

    def _try_set(self, name: str, value: Any) -> None:
        """Best-effort write for a prerequisite, never fatal."""
        try:
            self._set(name, value)
        except Exception:                                   # noqa: BLE001
            pass

    def limits(self) -> Limits:
        def bound(name: str, getter: str, default: Any) -> Any:
            node = self._node(name)
            if node is None:
                return default
            try:
                return getattr(node, getter)()
            except Exception:                               # noqa: BLE001
                return default

        width = int(self._value("WidthMax", 0) or bound("Width", "GetMax", 0))
        height = int(self._value("HeightMax", 0)
                     or bound("Height", "GetMax", 0))
        return Limits(
            width=width, height=height,
            inc_x=int(bound("OffsetX", "GetInc", 1) or 1),
            inc_y=int(bound("OffsetY", "GetInc", 1) or 1),
            inc_w=int(bound("Width", "GetInc", 1) or 1),
            inc_h=int(bound("Height", "GetInc", 1) or 1),
            min_w=int(bound("Width", "GetMin", 1) or 1),
            min_h=int(bound("Height", "GetMin", 1) or 1),
            exposure_us=(float(bound("ExposureTime", "GetMin", 0.0)),
                         float(bound("ExposureTime", "GetMax", 0.0))),
            gain=(float(bound("Gain", "GetMin", 0.0)),
                  float(bound("Gain", "GetMax", 0.0))))

    def roi(self) -> Roi:
        return Roi(int(self._value("OffsetX", 0)),
                   int(self._value("OffsetY", 0)),
                   int(self._value("Width", 0)),
                   int(self._value("Height", 0)))

    def set_roi(self, roi: Roi) -> Roi:
        """Write the AOI to the sensor, in the only order that always works.

        OFFSETS TO ZERO, THEN SIZES, THEN OFFSETS. Width is validated against
        the current OffsetX on every write, so growing the window before
        moving the origin back is refused by the camera whenever the two
        overlap — which is most of the time.
        """
        wanted = fit_roi(roi, self.limits())
        try:
            self._set("OffsetX", 0)
            self._set("OffsetY", 0)
            self._set("Width", wanted.w)
            self._set("Height", wanted.h)
            self._set("OffsetX", wanted.x)
            self._set("OffsetY", wanted.y)
        except Exception as exc:                            # noqa: BLE001
            raise CameraError(f"the camera refused that area: {exc}") from exc
        return self.roi()

    def set_exposure_us(self, value: float) -> float:
        """Exposure in microseconds, with the auto loop turned off first.

        With ExposureAuto left Continuous the camera either refuses the write
        or overwrites it on its next frame, and the control appears to do
        nothing at all. Both prerequisites exist on the emulator and on the
        boA5320; a model without them skips the write harmlessly.
        """
        self._try_set("ExposureAuto", "Off")
        self._try_set("ExposureMode", "Timed")
        try:
            self._set("ExposureTime", float(value))
        except Exception as exc:                            # noqa: BLE001
            raise CameraError(f"exposure refused: {exc}") from exc
        return float(self._value("ExposureTime", value))

    def set_gain(self, value: float) -> float:
        """Gain, with the auto loop turned off first — as for exposure."""
        self._try_set("GainAuto", "Off")
        try:
            self._set("Gain", float(value))
        except Exception as exc:                            # noqa: BLE001
            raise CameraError(f"gain refused: {exc}") from exc
        return float(self._value("Gain", value))

    def set_frame_rate(self, fps: float) -> float:
        """Cap the camera at `fps`; 0 lifts the cap (as fast as it can).

        A boA5320 free-runs at up to ~150 fps of 49 MB frames, far more than
        any disk saves as PNG. Capping the camera at a rate the disk can take
        is the difference between a complete capture and one full of "NOT
        saved". SFNC-2 cameras call the node AcquisitionFrameRate; older GigE
        models AcquisitionFrameRateAbs. The request is clamped to the range
        the camera reports, and the rate it actually runs at is returned.
        """
        fps = float(fps)
        if fps <= 0:
            self._try_set("AcquisitionFrameRateEnable", False)
            return self.frame_rate()
        name = next((n for n in ("AcquisitionFrameRate",
                                 "AcquisitionFrameRateAbs")
                     if self._node(n) is not None), None)
        if name is None:
            raise CameraError("this camera has no frame-rate control")
        node = self._node(name)
        try:
            low, high = float(node.GetMin()), float(node.GetMax())
            fps = max(low, min(high, fps))
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self._set("AcquisitionFrameRateEnable", True)
            self._set(name, fps)
        except Exception as exc:                            # noqa: BLE001
            raise CameraError(f"frame rate refused: {exc}") from exc
        return self.frame_rate()

    def frame_rate(self) -> float:
        """The rate the camera says it will run at, 0.0 if it cannot say."""
        for name in ("ResultingFrameRate", "ResultingFrameRateAbs",
                     "AcquisitionFrameRate", "AcquisitionFrameRateAbs"):
            value = self._value(name, None)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return 0.0

    def start(self) -> None:
        if self._started:
            return
        strategy = getattr(self._pylon, "GrabStrategy_LatestImageOnly", None)
        # LatestImageOnly, deliberately: a live view that queues frames shows
        # an ever-growing lag behind the sensor and calls it "live".
        if strategy is None:
            self._cam.StartGrabbing()
        else:
            self._cam.StartGrabbing(strategy)
        self._started = True

    def stop(self) -> None:
        self._started = False
        try:
            self._cam.StopGrabbing()
        except Exception:                                   # noqa: BLE001
            pass

    def read(self, timeout_ms: int = 1000) -> Optional[Frame]:
        """One frame, or None if none arrived inside the timeout."""
        if not self._started:
            # RetrieveResult on a camera that is not grabbing throws. read()
            # is public and a view can call it once more while stopping.
            return None
        handling = self._pylon.TimeoutHandling_Return
        grab = self._cam.RetrieveResult(int(timeout_ms), handling)
        if grab is None:
            return None
        try:
            # A TIMEOUT IS AN INVALID RESULT, NOT None. Measured: three
            # 1 ms retrievals against the emulator returned GrabResult
            # objects with IsValid() False. Calling GrabSucceeded() on one
            # throws, so without this the first slow frame ends the loop.
            valid = getattr(grab, "IsValid", None)
            if valid is not None and not valid():
                return None
            if not grab.GrabSucceeded():
                said = _call(grab, "GetErrorDescription") or "grab failed"
                raise CameraError(str(said))
            # NOT copied again: .Array already owns its pixels (measured — an
            # array held across Release() and five further grabs was
            # unchanged). The copy this line used to make was 32 MB per frame
            # on a full-frame Mono12 boA5320.
            image = grab.Array
            # A tick count, not microseconds — and the boost CXP models do
            # not support Timestamp at all, so this is 0 on a boA5320. Kept
            # because other Basler families do fill it in.
            stamp = int(_call(grab, "GetTimeStamp") or 0)
        finally:
            try:
                grab.Release()
            except Exception:                               # noqa: BLE001
                pass
        self._index += 1
        return Frame(image=image, index=self._index, timestamp_us=stamp,
                     meta={"kind": "frame"})

    def close(self) -> None:
        """Close the camera AND release the underlying pylon device.

        Close() alone leaves the device attached to the InstantCamera until
        Python happens to collect it. On CoaXPress the grabber channel is
        exclusive per process, so that can leave the camera unopenable by the
        next run — or by the pylon Viewer — until the interpreter exits.
        pypylon's own context manager calls both, for this reason.
        """
        self.stop()
        for step in ("Close", "DestroyDevice"):
            fn = getattr(self._cam, step, None)
            if fn is None:
                continue
            try:
                fn()
            except Exception:                               # noqa: BLE001
                pass


# ======================================================================
# Prophesee EVK4 — Metavision
# ======================================================================
class EvkBackend(Backend):
    """Prophesee event cameras through the Metavision SDK.

    An EVK4 is a USB3 device carrying an IMX636 sensor: 1280x720, and no
    frames anywhere in its output. What comes back is a CD (contrast
    detection) event stream, which this backend bins into images so the same
    viewer can show it -- see `accumulate_events`.

    THIS PATH HAS NEVER RUN AGAINST HARDWARE OR THE REAL SDK. The Metavision
    SDK is not on PyPI (installer-only), so unlike the Basler path -- which is
    verified end to end against real pypylon via its camera emulator -- every
    call here is written from the published API reference. The first version
    was WRONG in a way that mattered: it pulled events with a
    `decoder.get_cd_events()` that does not exist, through a helper that
    swallowed the AttributeError, so a healthy camera would have opened,
    reported 1280x720 and then reported silence for ever. That is why nothing
    critical here goes through a defaulting helper any more.
    """

    name = "prophesee"
    kind = "event"
    install_hint = ("install the Metavision SDK from Prophesee (it is not on "
                    "PyPI); its Python bindings are built for one specific "
                    "Python version")

    def __init__(self, sdk: Any = None):
        self._sdk = sdk

    def sdk(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        try:
            import metavision_hal
        except ImportError as exc:
            raise CameraError(f"the Metavision SDK is not installed: {exc}") from exc
        self._sdk = metavision_hal
        return self._sdk

    def available(self) -> bool:
        try:
            self.sdk()
        except CameraError:
            return False
        return True

    def discover(self) -> List[CameraInfo]:
        try:
            hal = self.sdk()
        except CameraError:
            return []
        try:
            serials = hal.DeviceDiscovery.list()
        except Exception as exc:                            # noqa: BLE001
            raise CameraError(f"Metavision could not enumerate: {exc}") from exc
        return [CameraInfo(backend=self.name, key=str(s), model="EVK4",
                           serial=str(s), vendor="Prophesee", kind="event")
                for s in (serials or [])]

    def open(self, info: CameraInfo) -> "EvkDevice":
        hal = self.sdk()
        try:
            device = hal.DeviceDiscovery.open(info.key)
        except Exception as exc:                            # noqa: BLE001
            raise CameraError(f"could not open {info.key!r}: {exc}") from exc
        if device is None:
            raise CameraError(f"could not open {info.key!r}")
        return EvkDevice(info, device)


def _required(device: Any, *names: str) -> Any:
    """An SDK interface that MUST be there, or a CameraError naming it.

    The opposite of `_call`. Optional accessors differ between SDK versions
    and a missing one is not a reason to fail a scan -- but an interface the
    whole backend is built on is different, and defaulting it to None is how
    the first version of this file turned a fatal mistake into a camera that
    looked healthy and produced nothing.
    """
    for name in names:
        got = _interface(device, name)
        if got is not None:
            return got
    raise CameraError(
        f"this Metavision device exposes none of {', '.join(names)} -- the "
        f"SDK version may not match what this build expects")


#: How long stopping a raw recording keeps pulling what the SDK has queued.
#: stream.stop() DISCARDS buffers nobody pulled, and the log is written only
#: as buffers are pulled — so without this the last moments of a run are
#: missing from the .raw. Pulling without decoding is fast (0.1 ms per 14 MB
#: measured), so this is a backstop, not an expected wait.
RAW_DRAIN_SECONDS = 2.0


class _EventSink:
    """Where the CD decoder delivers events. Deliberately NOT the device.

    Registering a bound method of the device as the decoder callback creates
    a reference cycle through C++ that Python's garbage collector cannot see:
    measured on OpenEB 5.2, the device was never freed, and a .raw it was
    logging stayed open and locked with its tail unwritten (118,576 of
    119,988 bytes) until the process exited. The sink holds only the events.
    """

    def __init__(self) -> None:
        self.pending: List[Any] = []
        self.lock = threading.Lock()

    def on_events(self, buffer: Any) -> None:
        """Called by the decoder, on its own thread, with a RECYCLED buffer.

        The copy is not optional: the decoder owns that memory and hands the
        same block to the next callback. This is the pylon Release() hazard
        again, on the other vendor's SDK.
        """
        np = _numpy()
        try:
            taken = np.array(buffer, copy=True)
        except Exception:                                   # noqa: BLE001
            return
        if taken.size:
            with self.lock:
                self.pending.append(taken)

    def drain(self) -> List[Any]:
        with self.lock:
            got, self.pending = self.pending, []
        return got


class EvkDevice(Device):
    """An open EVK4, presented as accumulated images.

    `accumulate_ms` is the window each returned image covers. It is NOT an
    exposure: the sensor is free-running and asynchronous, and a longer window
    collects more events rather than more light.

    DECODING IS PUSHED, NOT PULLED. `I_EventsStreamDecoder` has no method that
    hands back the events it decoded; `decode()` fans them out to callbacks
    registered on the CD decoder. So the callback is registered once, here,
    and `read()` drains what it has collected.
    """

    def __init__(self, info: CameraInfo, device: Any,
                 accumulate_ms: float = DEFAULT_ACCUMULATE_MS):
        self.info = info
        self._dev = device
        self._index = 0
        self._started = False
        self._roi = Roi()
        self.accumulate_ms = float(accumulate_ms)

        geo = _required(device, "get_i_geometry")
        self._width = int(_call(geo, "get_width") or 1280)
        self._height = int(_call(geo, "get_height") or 720)

        self._stream = _required(device, "get_i_events_stream")
        self._decoder = _required(device, "get_i_events_stream_decoder",
                                  "get_i_decoder")
        # The CD decoder is a SEPARATE interface from the stream decoder, and
        # it is the only place decoded events are delivered.
        self._cd = _required(device, "get_i_event_cd_decoder",
                             "get_i_cd_decoder")

        self._sink = _EventSink()
        #: Set when poll_buffer reports the source is finished.
        self._ended = False
        #: The .raw being written, and the sensor time of its first event.
        self._raw: Optional[Path] = None
        self._raw_origin: Optional[int] = None
        #: stream.start() succeeded and stream.stop() has not been called.
        #: Not `_started`, which also goes False when the stream ENDS: a
        #: stream that ended still needs its stop(), exactly once.
        self._streaming = False
        #: finish_raw asks the grab loop to close the .raw at a quiet moment.
        self._raw_lock = threading.Lock()
        self._finish_wanted = False
        self._raw_finished = threading.Event()
        try:
            self._cd.add_event_buffer_callback(self._sink.on_events)
        except Exception as exc:                            # noqa: BLE001
            raise CameraError(f"could not subscribe to CD events: {exc}") from exc

    # ------------------------------------------------------------------
    def _drain(self) -> List[Any]:
        return self._sink.drain()

    # ------------------------------------------------------------------
    # One stream per connection
    # ------------------------------------------------------------------
    #: NEVER stop and start this stream again on the same open device.
    #: Measured against OpenEB 5.2: I_EventsStream.start/stop leave the
    #: decoder's state alone — the last time-high, the EVT3 wrap counter, a
    #: half-decoded vector group, a leftover byte — and on a live EVK4 stop()
    #: really stops the sensor's time base. Fed a restarted stream, that
    #: stale state made time run backwards ("NonMonotonicTimeHigh"), jump
    #: 16.78 s forward, or produced fake events. Prophesee's own SDK resets
    #: the decoder on every start; the Python bindings expose no way to. So
    #: the stream starts once, and the .raw opens and closes INSIDE it, which
    #: is how Prophesee's own recording works (Camera::start_recording).
    restartable = False

    # ------------------------------------------------------------------
    # The raw recording
    # ------------------------------------------------------------------
    records_raw = True

    @property
    def raw_path(self) -> Optional[Path]:
        return self._raw

    def start_raw(self, path: Any) -> Path:
        """Record every byte the camera sends into `path`, alongside the view.

        BEFORE start() WHEN IT CAN BE. The SDK writes each buffer as it is
        pulled, so a log started mid-stream begins at a buffer boundary, and a
        replay drops the events before the file's first time marker: at most
        one EVT3 time-high period, 4.1 ms, measured. Started before the
        stream, the file has everything. Started while the stream runs (a
        capture begun from the live preview), it loses at most that — the
        same as Prophesee's own recorder, and far better than restarting the
        stream to avoid it (see `restartable`).

        IT NEVER OVERWRITES. The SDK's log_raw_data silently truncates an
        existing file (measured: 1,700,147 bytes to 208), so an existing path
        is refused here before the SDK is asked.

        THE NAME MUST END IN .raw. The HAL accepts anything, but Metavision's
        own readers treat any other name as a camera serial number and fail.
        """
        target = Path(str(path))
        if target.suffix.lower() != ".raw":
            raise CameraError(f"{target.name}: a raw recording must end in "
                              f".raw — Metavision's readers refuse any other name")
        if target.exists():
            raise CameraError(f"{target.name} already exists, and a raw "
                              f"recording never overwrites one")
        if self._raw is not None:
            self.stop_raw()
        stream = self._stream
        with self._raw_lock:
            # The log call and the grab loop's pulls share the SDK's own
            # log mutex; this lock only keeps OUR bookkeeping consistent.
            try:
                ok = stream.log_raw_data(str(target))
            except Exception as exc:                        # noqa: BLE001
                raise CameraError(
                    f"could not start the raw recording: {exc}") from exc
            if ok is False:
                # False, not an exception: a missing folder, or no permission.
                raise CameraError(f"could not create {target}")
            self._raw = target
            self._raw_origin = None
            self._finish_wanted = False
            self._raw_finished.clear()
        return target

    def finish_raw(self, timeout: float = None) -> Optional[Path]:
        """Close the .raw while the stream keeps running.

        The grab loop closes it itself, the next time the SDK's queue is
        empty: every buffer up to then has been pulled — so written — AND
        decoded. Pulling the tail from here instead would take buffers away
        from the decoder and leave it out of step with the stream. If the
        loop does not get there within `timeout` (a file source never runs
        dry), the file is closed from here; the SDK serialises that with the
        loop's writes.
        """
        target = self._raw
        if target is None:
            return None
        if not self._started:
            return self.stop_raw()
        with self._raw_lock:
            self._finish_wanted = True
        wait = RAW_DRAIN_SECONDS if timeout is None else float(timeout)
        if not self._raw_finished.wait(wait):
            self._close_raw_now()
        return target

    def _close_raw_now(self) -> None:
        with self._raw_lock:
            if self._raw is None:
                return
            try:
                self._stream.stop_log_raw_data()
            finally:
                self._raw = None
                self._finish_wanted = False
                self._raw_finished.set()

    def stop_raw(self) -> Optional[Path]:
        """Finish the .raw: pull what is still queued, then close the file.

        Call it while the stream is still running and nothing else is reading
        it. stream.stop() discards buffers that were never pulled, and only a
        pulled buffer reaches the file. The file is complete and unlocked the
        moment stop_log_raw_data returns.
        """
        target = self._raw
        if target is None:
            return None
        if self._started:
            self._drain_to_log()
        try:
            self._close_raw_now()
        except Exception as exc:                            # noqa: BLE001
            raise CameraError(f"could not finish {target.name}: {exc}") from exc
        return target

    def _drain_to_log(self) -> None:
        deadline = time.monotonic() + RAW_DRAIN_SECONDS
        stream = self._stream
        while time.monotonic() < deadline:
            try:
                ready = stream.poll_buffer()
            except Exception:                               # noqa: BLE001
                return
            if ready <= 0:
                return
            # The pull IS the write: get_latest_raw_data logs the buffer it
            # hands back. Not decoding it only means the last few ms are in
            # the .raw and not in a PNG.
            stream.get_latest_raw_data()

    # ------------------------------------------------------------------
    def limits(self) -> Limits:
        # An event sensor has no exposure and no gain, and its ROI is a plain
        # pixel window. Reporting zero ranges is how a view knows not to draw
        # an exposure box it cannot honour.
        return Limits(width=self._width, height=self._height,
                      min_w=1, min_h=1)

    def roi(self) -> Roi:
        if self._roi.empty:
            return Roi(0, 0, self._width, self._height)
        return self._roi

    def set_roi(self, roi: Roi) -> Roi:
        """Set the sensor's own ROI, so unwanted pixels stop emitting.

        This is not a crop. An event camera's bottleneck is event RATE, and a
        hardware ROI stops the masked pixels producing events at all -- which
        is the difference between a usable stream and a saturated link when
        only part of the scene matters.

        THE RETURN VALUES ARE CHECKED. `set_window` and `enable` report
        success as a bool rather than raising, so ignoring them makes a
        refused ROI indistinguishable from an applied one -- and the refused
        one would then have its width and height used to size every image.
        """
        wanted = fit_roi(roi, self.limits())
        i_roi = _interface(self._dev, "get_i_roi")
        if i_roi is None:
            raise CameraError("this device exposes no ROI control")
        try:
            mode = getattr(i_roi, "set_mode", None)
            if mode is not None and hasattr(i_roi, "Mode"):
                mode(i_roi.Mode.ROI)
            window = i_roi.Window(wanted.x, wanted.y, wanted.w, wanted.h)
            if i_roi.set_window(window) is False:
                raise CameraError("the camera refused that window")
            if i_roi.enable(True) is False:
                raise CameraError("the camera refused to enable the window")
        except CameraError:
            raise
        except Exception as exc:                            # noqa: BLE001
            raise CameraError(f"the camera refused that area: {exc}") from exc
        self._roi = wanted
        return wanted

    def set_frame_rate(self, fps: float) -> float:
        """Pictures a second = how long each window collects events.

        An event camera has no frames: `fps` sets the window each picture
        covers (50 fps = 20 ms windows; 0 = the default). Safe while
        streaming — the next window uses it. The .raw is unaffected: it has
        every event whatever the windows are.
        """
        fps = float(fps)
        if fps <= 0:
            self.accumulate_ms = DEFAULT_ACCUMULATE_MS
        else:
            self.accumulate_ms = max(MIN_ACCUMULATE_MS, 1000.0 / fps)
        return 1000.0 / self.accumulate_ms

    def start(self) -> None:
        if self._started:
            return
        try:
            self._stream.start()
        except Exception as exc:                            # noqa: BLE001
            raise CameraError(f"could not start the stream: {exc}") from exc
        self._started = True
        self._streaming = True

    def stop(self) -> None:
        # The raw file first, while the stream is still running: stopping the
        # stream throws away whatever has not been pulled into it yet.
        try:
            self.stop_raw()
        except CameraError:
            pass
        self._started = False
        self._ended = False
        if self._streaming:
            # Once. On a live EVK4 each stop() is a round of USB register
            # writes to the sensor, and close() calls stop() again.
            self._streaming = False
            try:
                self._stream.stop()
            except Exception:                               # noqa: BLE001
                pass
        self._drain()

    def read(self, timeout_ms: int = 1000) -> Optional[Frame]:
        """Events for one window, binned into an image.

        Returns None on a quiet window rather than a blank image, so a caller
        can tell "no events arrived" apart from "events arrived and cancelled
        out". The event COUNT and the window travel in `meta`, because an
        image of an event stream on its own is not a measurement.
        """
        np = _numpy()
        batch = self._poll(timeout_ms)
        if batch is None:
            return None
        xs, ys, pols, stamps = batch
        roi = self.roi()
        # EVENTS CARRY ABSOLUTE SENSOR COORDINATES. I_ROI masks pixels in
        # place; it does not renumber what the survivors report. Binning raw
        # x/y into an image the size of the window would put every event
        # outside it -- a blank picture from a working camera, the moment the
        # window's origin is not (0, 0). int64 first: the SDK's x/y are
        # unsigned and would wrap on the subtraction.
        xs = np.asarray(xs, dtype=np.int64) - int(roi.x)
        ys = np.asarray(ys, dtype=np.int64) - int(roi.y)
        image = accumulate_events(xs, ys, pols, roi.w, roi.h, np)
        self._index += 1
        span_us = (int(stamps.max()) - int(stamps.min())) if len(stamps) else 0
        rate = (len(xs) / (span_us / 1e6)) if span_us > 0 else 0.0
        meta = {"kind": "event", "events": int(len(xs)),
                "window_ms": self.accumulate_ms,
                "span_us": span_us, "event_rate_hz": rate}
        if self._raw is not None and len(stamps):
            # WHERE THIS PICTURE IS IN THE .raw, as time since its first
            # event. The live clock and the file's clock differ: a replay is
            # time-shifted to start near zero, and an EVT3 file started after
            # the 24-bit clock wrapped reads k*16.78 s behind the camera.
            # Relative to the first event, both agree.
            if self._raw_origin is None:
                self._raw_origin = int(stamps.min())
            meta["raw_t_us"] = int(stamps[-1]) - self._raw_origin
        return Frame(image=image, index=self._index,
                     timestamp_us=int(stamps[-1]) if len(stamps) else 0,
                     meta=meta)

    def _poll(self, timeout_ms: int):
        """Pump the stream for one accumulation window.

        VECTORISED THROUGHOUT. An EVK4 can emit events in the hundreds of
        millions per second; a Python loop that calls int() four times per
        event cannot come close, and the buffers the SDK hands over are
        structured numpy arrays already.
        """
        np = _numpy()
        deadline = time.monotonic() + (self.accumulate_ms / 1000.0)
        waited = 0.0
        while time.monotonic() < deadline:
            ready = self._stream.poll_buffer()
            if ready < 0:
                # NEGATIVE IS NOT "QUIET". Measured against a real stream:
                # poll_buffer goes 0 (nothing yet), 1 (data ready), then -1
                # for ever once the source is finished. Treating -1 as
                # no-data-yet spins at a kilohertz while reporting a healthy
                # camera that simply has nothing to say.
                #
                # BUT IT IS NOT AN IMMEDIATE RAISE EITHER. Raising here threw
                # away whatever this window had already decoded -- the last
                # events before a camera was unplugged, which are the ones
                # most worth having. Mark it, deliver the final frame, and
                # report the end on the next read once nothing is left.
                self._started = False
                self._ended = True
                break
            if ready == 0:
                if self._finish_wanted:
                    # Everything queued has been pulled, logged and decoded:
                    # the one moment the .raw can end without the decoder
                    # missing a buffer.
                    self._close_raw_now()
                waited += 0.001
                if waited * 1000.0 >= timeout_ms:
                    break
                time.sleep(0.001)
                continue
            raw = self._stream.get_latest_raw_data()
            if raw is None:
                continue
            # decode() does not return events; it fans them out to the CD
            # callback registered in __init__.
            self._decoder.decode(raw)

        buffers = self._drain()
        if not buffers:
            if self._ended:
                raise CameraEnded(
                    "the event stream ended — the recording finished, or the "
                    "camera was disconnected")
            return None
        events = buffers[0] if len(buffers) == 1 else np.concatenate(buffers)
        if not len(events):
            return None
        return (events["x"], events["y"], events["p"], events["t"])

    def close(self) -> None:
        self.stop()
        # Let go of the SDK objects. A HAL device that is still referenced
        # keeps the camera claimed and any file it touched open on Windows.
        self._dev = self._stream = self._decoder = self._cd = None


# ======================================================================
# Synthetic — so the app is usable and testable with no hardware at all
# ======================================================================
class SyntheticBackend(Backend):
    """Two pretend cameras, one of each kind.

    This is not a demo toy. Neither SDK is installed on most machines that
    will open this tab, and a capture app that cannot be opened at all without
    hardware cannot be developed, reviewed, or tested. It is labelled
    "simulated" everywhere it appears so it is never mistaken for a camera.
    """

    name = "simulated"
    kind = "frame"

    def available(self) -> bool:
        return True

    def discover(self) -> List[CameraInfo]:
        return [
            CameraInfo(self.name, "sim-frame", "Simulated frame camera",
                       "SIM-1", "simulated", "frame"),
            CameraInfo(self.name, "sim-event", "Simulated event camera",
                       "SIM-2", "simulated", "event"),
        ]

    def open(self, info: CameraInfo) -> "SyntheticDevice":
        return SyntheticDevice(info)


class SyntheticDevice(Device):
    """A moving bar, as frames or as events.

    PACED LIKE A CAMERA. Unpaced, it handed out frames as fast as they were
    asked for — 2,500 a second measured in Typhon — which filled a folder
    with 1,500 PNGs in three seconds and reported most of them "NOT saved".
    Nothing real behaves like that, and a simulation that does teaches the
    wrong numbers. The event camera delivers one window per `accumulate_ms`,
    as the EVK4 path does; the frame camera runs at FRAME_FPS.
    """

    WIDTH, HEIGHT = 640, 480
    FRAME_FPS = 30.0

    def __init__(self, info: CameraInfo):
        self.info = info
        self._roi = Roi(0, 0, self.WIDTH, self.HEIGHT)
        self._index = 0
        self._started = False
        self.accumulate_ms = DEFAULT_ACCUMULATE_MS
        self._fps = self.FRAME_FPS
        self._due = 0.0

    def _interval(self) -> float:
        if self.info.kind == "event":
            return max(0.001, self.accumulate_ms / 1000.0)
        return 1.0 / self._fps

    def set_frame_rate(self, fps: float) -> float:
        fps = float(fps)
        if self.info.kind == "event":
            self.accumulate_ms = (DEFAULT_ACCUMULATE_MS if fps <= 0 else
                                  max(MIN_ACCUMULATE_MS, 1000.0 / fps))
            return 1000.0 / self.accumulate_ms
        self._fps = self.FRAME_FPS if fps <= 0 else min(1000.0, fps)
        return self._fps

    def limits(self) -> Limits:
        """The limits of whichever sensor this one is standing in for.

        An event camera has NO exposure and NO gain, so the simulated one must
        report none either. A simulation that quietly has more controls than
        the real device is how a control that cannot work ships looking fine.
        """
        common = dict(width=self.WIDTH, height=self.HEIGHT, inc_x=4, inc_y=2,
                      inc_w=4, inc_h=2, min_w=16, min_h=16)
        if self.info.kind == "event":
            return Limits(**common)
        return Limits(exposure_us=(20.0, 100000.0), gain=(0.0, 24.0),
                      **common)

    def roi(self) -> Roi:
        return self._roi

    def set_roi(self, roi: Roi) -> Roi:
        self._roi = fit_roi(roi, self.limits())
        return self._roi

    def set_exposure_us(self, value: float) -> float:
        low, high = self.limits().exposure_us
        return max(low, min(high, float(value)))

    def set_gain(self, value: float) -> float:
        low, high = self.limits().gain
        return max(low, min(high, float(value)))

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def read(self, timeout_ms: int = 1000) -> Optional[Frame]:
        np = _numpy()
        now = time.monotonic()
        wait = self._due - now
        if wait > 0:
            if wait > timeout_ms / 1000.0:
                time.sleep(timeout_ms / 1000.0)
                return None
            time.sleep(wait)
        self._due = max(self._due, now) + self._interval()
        roi = self._roi
        self._index += 1
        column = (self._index * 7) % max(1, roi.w)
        if self.info.kind == "event":
            # Only the moving edge emits, which is what an event camera does.
            xs = [column] * roi.h + [(column + 1) % roi.w] * roi.h
            ys = list(range(roi.h)) * 2
            pols = [1] * roi.h + [0] * roi.h
            image = accumulate_events(xs, ys, pols, roi.w, roi.h, np)
            return Frame(image, self._index, self._index * 1000,
                         {"kind": "event", "events": len(xs),
                          "window_ms": self.accumulate_ms,
                          "simulated": True})
        image = np.zeros((roi.h, roi.w), dtype=np.uint8)
        image[:, column] = 255
        image[roi.h // 3: 2 * roi.h // 3, :] += 40
        return Frame(image, self._index, self._index * 1000,
                     {"kind": "frame", "simulated": True})


# ======================================================================
# Discovery across every backend
# ======================================================================
def backends(sdk_basler: Any = None, sdk_evk: Any = None) -> List[Backend]:
    return [BaslerBackend(sdk_basler), EvkBackend(sdk_evk),
            SyntheticBackend()]


@dataclass
class Discovery:
    """What a scan found, and what it could not look at.

    `notes` carries the reason each unavailable backend was skipped. A camera
    tab that shows an empty list and no explanation sends the user to check
    cables when the real answer is that an SDK was never installed.
    """
    cameras: List[CameraInfo] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def discover(known: Optional[Sequence[Backend]] = None) -> Discovery:
    """Every camera on the machine, plus why any backend was skipped."""
    found = Discovery()
    for backend in (known if known is not None else backends()):
        if not backend.available():
            hint = backend.install_hint
            found.notes.append(
                f"{backend.name}: not available"
                + (f" — {hint}" if hint else ""))
            continue
        try:
            cameras = backend.discover()
        except CameraError as exc:
            found.notes.append(f"{backend.name}: {exc}")
            continue
        if not cameras and backend.name == "basler":
            found.notes.append(
                "basler: pypylon is installed but no camera enumerated. For a "
                "CoaXPress camera (boost series, e.g. boA5320-150cm) the pip "
                "wheel is not enough: since pypylon 4.0.0 the CXP GenTL "
                "producer was dropped from the Windows wheel, so install the "
                "pylon Software Suite with 'CXP Camera Support' ticked, and "
                "make sure the interface card's applet matches the number of "
                "CXP cables in use")
        elif not cameras:
            found.notes.append(f"{backend.name}: no cameras found")
        found.cameras.extend(cameras)
    return found


def open_camera(info: CameraInfo,
                known: Optional[Sequence[Backend]] = None) -> Device:
    """Open one camera by the info a scan returned."""
    for backend in (known if known is not None else backends()):
        if backend.name == info.backend:
            return backend.open(info)
    raise CameraError(f"no backend named {info.backend!r}")


# ======================================================================
# Small helpers
# ======================================================================
def _call(obj: Any, name: str, default: Any = "") -> Any:
    """Call an optional SDK accessor, or return `default`.

    SDK objects differ between versions in which accessors exist, and a
    missing one is not a reason to fail a scan.
    """
    fn = getattr(obj, name, None)
    if fn is None:
        return default
    try:
        return fn()
    except Exception:                                       # noqa: BLE001
        return default


def _interface(device: Any, name: str) -> Any:
    fn = getattr(device, name, None)
    if fn is None:
        return None
    try:
        return fn()
    except Exception:                                       # noqa: BLE001
        return None
