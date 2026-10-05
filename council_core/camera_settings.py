"""
council_core.camera_settings — every setting a connected camera has, read
from the camera itself, and written back one at a time or as a set.

WHY THIS EXISTS
Metavision Studio shows every knob an EVK4 has (its biases, the event rate
controller, anti-flicker, the trail filter, the ROI) and the pylon Viewer
shows a Basler's whole node map. Typhon showed exposure, gain and a frame
rate. A settings window and per-project presets need ONE description that
covers both: what each setting is called, what it takes, what it is now, and
whether it can change while the camera streams. That is `Setting`.

RANGES COME FROM THE CAMERA, NEVER FROM THIS FILE
A Basler node reports its own minimum, maximum and increment, and an
enumeration only the entries this model offers. An EVK4 bias reports its
allowed and recommended range through LL_Bias_Info; the ERC, anti-flicker and
trail filter report their supported bounds. The tables here say only WHICH
features to look for, what to call them, and in what order to write them.
(The one bound written here is the EVK4's picture window, which is this app's
own display setting, not the camera's.)

WHAT TOOK IS REPORTED, NOT ASSUMED
Every write reads the value back. A request outside the range is CLAMPED
before it is sent, because pylon raises OutOfRangeException rather than
clamping (measured on the emulator: ExposureTime 1e12 and Gain -50 both
raised). A value between increments is snapped BY THE CAMERA (ExposureTime
5003.7 came back 5004.0, measured), an integer node is snapped here because
pylon refuses an off-grid one, and an EVK4 bias or ERC rate may be quantised
by the sensor. `Change` carries what was asked, what the camera now says and
why they differ, so a settings window never shows a number the camera is not
using.

SOME SETTINGS CANNOT CHANGE WHILE THE CAMERA STREAMS
On the pylon emulator PixelFormat, ReverseX/Y, Binning and Width/Height are
not writable while grabbing (TLParamsLocked); exposure, gain, black level,
gamma, the frame rate and even OffsetX/Y are — all measured. A write refused
for that reason raises `NeedsStop`; this module never stops a stream itself,
because the grab thread belongs to capture.CaptureSession and stopping it is
the caller's decision (frame_camera stops, writes, starts again). An EVK4's
facilities are all set live, and its stream is never restarted anyway
(cameras.EvkDevice.restartable).

`apply` raises NeedsStop BEFORE writing anything — a set is never left half
applied because the stream was in the way halfway through. And a locked
setting whose value would not change is not a reason to stop: a preset that
saves PixelFormat Mono8, applied to a camera already in Mono8, changes only
what it changes, live (`stops_needed`).

A SAFE ORDER FOR A SET
`apply` writes a preset in an order that works: auto loops off before the
values they would overwrite; pixel format and binning before the area (binning
changes the sensor's own pixel grid); the area before the frame rate, whose
limit depends on it; an EVK4 filter's parameters before its enable flag.

NO TOOLKIT, NO SDK IMPORT
Providers are handed the SDK objects the devices already hold. Nothing here
imports pypylon or metavision_hal, so tests use fakes with the same surface —
checked against the real bindings in tests/test_camera_settings.py (OpenEB)
and tests/test_basler_real.py (the pylon emulator).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .cameras import (CameraError, MIN_ACCUMULATE_MS, NeedsStop, Roi, _ask,
                      _interface, fit_roi, off_sensor, on_sensor)

__all__ = ["Setting", "Change", "Applied", "SettingError", "NeedsStop",
           "area_unchanged",
           "coerce", "apply", "snapshot", "needs_stop", "stops_needed"]

#: What a setting holds.
FLOAT, INT, BOOL, CHOICE, TEXT = "float", "int", "bool", "choice", "text"


class SettingError(CameraError):
    """A setting that does not exist here, cannot be written, or was given
    something it cannot take."""


# ======================================================================
# What a setting is, and what became of a change
# ======================================================================
@dataclass(frozen=True)
class Setting:
    """One setting, as the camera describes it right now.

    THREE WAYS NOT TO BE WRITABLE, KEPT APART because each needs a different
    answer from a settings window:
      * `read_only` — never writable here (a temperature, a serial number).
      * `held` — writable, but another setting owns it at the moment
        ("ExposureAuto is Continuous"): grey it out and say why. A preset
        that turns the auto loop off in the same set still writes it.
      * `live` False — writable only with the stream stopped (a Basler's
        pixel format): the caller stops it, writes, and starts it again.
    """
    key: str
    label: str
    group: str
    kind: str
    value: Any
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    choices: Tuple[str, ...] = ()
    unit: str = ""
    read_only: bool = False
    live: bool = True
    help: str = ""
    #: An EVK4 bias's recommended range, inside the allowed one.
    recommended: Optional[Tuple[float, float]] = None
    #: Why another setting owns this one right now, or "".
    held: str = ""

    @property
    def savable(self) -> bool:
        """Belongs in a preset: something the user chose, not a reading."""
        return not self.read_only and self.kind != TEXT

    def as_dict(self) -> Dict[str, Any]:
        """Plain data, for a window or a JSON file."""
        return {"key": self.key, "label": self.label, "group": self.group,
                "type": self.kind, "value": self.value,
                "min": self.minimum, "max": self.maximum, "step": self.step,
                "choices": list(self.choices), "unit": self.unit,
                "read_only": self.read_only, "live": self.live,
                "held": self.held, "help": self.help,
                "recommended": (list(self.recommended)
                                if self.recommended else None)}


@dataclass(frozen=True)
class Change:
    """What one write did.

    `value` is what the camera reports AFTER the write — the truth, whatever
    was asked. `ok` False is a refusal (`note` says why) and the camera kept
    `value`. `skipped` is a value deliberately left alone, such as an
    exposure time while auto exposure is on.
    """
    key: str
    asked: Any
    value: Any = None
    ok: bool = True
    note: str = ""
    skipped: bool = False

    @property
    def adjusted(self) -> bool:
        """Took, but not exactly as asked (clamped or snapped)."""
        return self.ok and not self.skipped and not same(self.asked, self.value)

    def line(self) -> str:
        """One line for a status bar or a list."""
        if not self.ok:
            return f"{self.key}: NOT changed — {self.note}"
        if self.skipped:
            return f"{self.key}: left at {show(self.value)} — {self.note}"
        if self.adjusted:
            why = f" ({self.note})" if self.note else ""
            return (f"{self.key}: {show(self.value)}, asked "
                    f"{show(self.asked)}{why}")
        return f"{self.key}: {show(self.value)}"

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "asked": self.asked, "value": self.value,
                "ok": self.ok, "note": self.note, "skipped": self.skipped,
                "adjusted": self.adjusted}


@dataclass
class Applied:
    """What a whole set did: each change, and the area if one was asked."""
    changes: List[Change] = field(default_factory=list)
    roi_asked: Optional[Roi] = None
    #: The area the camera took, or None when none was asked or it refused.
    roi: Optional[Roi] = None
    roi_error: str = ""

    @property
    def refused(self) -> List[Change]:
        return [c for c in self.changes if not c.ok]

    @property
    def adjusted(self) -> List[Change]:
        return [c for c in self.changes if c.adjusted]

    @property
    def ok(self) -> bool:
        return not self.refused and not self.roi_error

    def summary(self) -> str:
        """One sentence: how many took, and the first thing that did not."""
        took = sum(1 for c in self.changes if c.ok and not c.skipped)
        bits = [f"{took} setting{'s' if took != 1 else ''} applied"]
        if self.roi_asked is not None:
            if self.roi is not None:
                area = ", ".join(str(v) for v in self.roi.as_tuple())
                bits.append(f"area {area}"
                            + (" (snapped)" if self.roi != self.roi_asked
                               else ""))
            else:
                bits.append(f"area NOT set: {self.roi_error}")
        if self.adjusted:
            bits.append(f"{len(self.adjusted)} adjusted by the camera")
        if self.refused:
            first = self.refused[0]
            bits.append(f"{len(self.refused)} refused ({first.key}: "
                        f"{first.note})")
        return "; ".join(bits) + "."

    def as_dict(self) -> Dict[str, Any]:
        return {"changes": [c.as_dict() for c in self.changes],
                "roi_asked": (list(self.roi_asked.as_tuple())
                              if self.roi_asked else None),
                "roi": list(self.roi.as_tuple()) if self.roi else None,
                "roi_error": self.roi_error, "ok": self.ok,
                "summary": self.summary()}


def same(a: Any, b: Any) -> bool:
    """Equal as settings: floats within float noise (a gain written as 3.0
    reads back 2.999994 on the emulator — measured — which is not a change
    anyone asked about)."""
    # What was TYPED or saved may be text ("5000", "on"): compare it as the
    # value it stands for, or every typed number reads as "adjusted".
    if isinstance(a, str) != isinstance(b, str):
        text, other = (a, b) if isinstance(a, str) else (b, a)
        lowered = text.strip().lower()
        if isinstance(other, bool):
            return other is True and lowered in _TRUE or (
                other is False and lowered in _FALSE)
        if isinstance(other, (int, float)):
            try:
                a, b = float(lowered), float(other)
            except ValueError:
                return False
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-5, abs_tol=1e-6)
    if isinstance(a, str) and isinstance(b, str):
        # A choice is matched without regard to case (coerce), so "mono8"
        # taking as "Mono8" is not an adjustment.
        return a.strip().lower() == b.strip().lower()
    return a == b


def show(value: Any) -> str:
    """A value for a person. Floats lose their read-back noise (a gain of 3
    reads back 2.999994 on the emulator — said as 3, not 2.99999)."""
    if isinstance(value, float):
        return f"{round(value, 4):g}"
    return str(value)


# ======================================================================
# Turning what was typed or saved into what the setting takes
# ======================================================================
_TRUE = {"1", "true", "yes", "on", "enabled"}
_FALSE = {"0", "false", "no", "off", "disabled"}


def coerce(setting: Setting, value: Any) -> Tuple[Any, str]:
    """`value` as this setting takes it, and a note if it had to move.

    Clamped to the range the camera reported; an integer snapped to its
    increment (pylon refuses an off-grid integer); a choice matched without
    regard to case. Anything that cannot be read as this kind of value
    raises SettingError — a typed "bright" is a mistake to show, not a
    zero to send.
    """
    kind = setting.kind
    if kind == BOOL:
        if isinstance(value, bool):
            return value, ""
        text = str(value).strip().lower()
        if text in _TRUE:
            return True, ""
        if text in _FALSE:
            return False, ""
        raise SettingError(f"{setting.label} is on or off, not {value!r}")
    if kind == CHOICE:
        text = str(value).strip()
        for choice in setting.choices:
            if choice == text:
                return choice, ""
        for choice in setting.choices:
            if choice.lower() == text.lower():
                return choice, ""
        raise SettingError(f"{setting.label} is one of "
                           f"{', '.join(setting.choices)} — not {value!r}")
    if kind in (INT, FLOAT):
        try:
            number = float(str(value).strip()) if not isinstance(
                value, (int, float)) else float(value)
        except (TypeError, ValueError):
            raise SettingError(f"{setting.label} must be a number, not "
                               f"{value!r}") from None
        if math.isnan(number) or math.isinf(number):
            raise SettingError(f"{setting.label} must be a finite number")
        note = ""
        low, high = setting.minimum, setting.maximum
        if low is not None and number < low:
            number, note = float(low), f"clamped to the minimum, {show(low)}"
        if high is not None and number > high:
            number, note = float(high), f"clamped to the maximum, {show(high)}"
        if kind == INT:
            step = int(setting.step or 1)
            base = int(low) if low is not None else 0
            snapped = base + int(round((number - base) / step)) * step
            if high is not None and snapped > high:
                snapped -= step
            if snapped != number and not note:
                note = f"snapped to a multiple of {step}"
            return int(snapped), note
        return float(number), note
    raise SettingError(f"{setting.label} cannot be set")


# ======================================================================
# The provider interface — one per kind of device
# ======================================================================
class Provider:
    """The settings of one open camera.

    Subclasses implement `describe`, `read` and `write`. `set` is the same
    for all of them: coerce, write, read back, say what happened.
    """

    #: Where a set's area is written, in `plan` order: after this group's
    #: settings, or None for last.
    roi_after_group: Optional[str] = None

    def describe(self) -> List[Setting]:
        raise NotImplementedError

    def read(self, key: str) -> Any:
        raise NotImplementedError

    def write(self, setting: Setting, value: Any,
              batch: Mapping[str, Any]) -> None:
        """Write a coerced value. Raise NeedsStop if the stream is in the
        way, SettingError (or anything) if the camera refuses."""
        raise NotImplementedError

    def held_by(self, setting: Setting,
                batch: Mapping[str, Any]) -> str:
        """Why this setting should be left alone (auto is on), or ""."""
        return ""

    # -- the camera's own factory settings ------------------------------
    def defaults_source(self) -> str:
        """What this camera calls its factory settings when it can load them
        itself (a Basler's UserSet "Default"), or "" when it cannot.

        An EVK4's HAL has nothing to load: its biases are the sensor's
        defaults when the device is opened, so "as connected" (which
        frame_camera keeps) is the nearest thing to a default it has. Saying
        "" rather than inventing defaults keeps a settings window from
        offering a button that cannot work."""
        return ""

    def load_defaults(self) -> None:
        """Load the factory settings. Raises NeedsStop, before anything is
        written, while the stream is in the way."""
        raise SettingError("this camera has no defaults of its own to load")

    # ------------------------------------------------------------------
    def find(self, key: str) -> Setting:
        for setting in self.describe():
            if setting.key == key:
                return setting
        raise SettingError(f"this camera has no setting called {key!r}")

    def set(self, key: str, value: Any,
            batch: Optional[Mapping[str, Any]] = None,
            setting: Optional[Setting] = None) -> Change:
        """Write one setting and report what the camera now says."""
        setting = setting or self.find(key)
        if setting.read_only:
            why = f" — {setting.help}" if setting.help else ""
            raise SettingError(f"{setting.label} cannot be changed{why}")
        wanted, note = coerce(setting, value)
        if not setting.live and same(wanted, setting.value):
            # Locked by the stream and already as asked (measured on the
            # emulator: writing PixelFormat Mono8 over Mono8 while grabbing
            # is still refused). Nothing to write, so nothing to stop for.
            return Change(key, value, setting.value)
        batch = batch if batch is not None else {}
        held = self.held_by(setting, batch)
        if held:
            return Change(key, value, self._safe_read(key), ok=True,
                          note=held, skipped=True)
        try:
            self.write(setting, wanted, batch)
        except NeedsStop:
            raise
        except Exception as exc:                            # noqa: BLE001
            return Change(key, value, self._safe_read(key), ok=False,
                          note=_why(exc))
        got = self._safe_read(key)
        if not note and not same(got, wanted):
            note = f"{show(wanted)} became {show(got)} on the camera"
        elif note and not same(got, wanted):
            note += f"; the camera made it {show(got)}"
        return Change(key, value, got, ok=True, note=note)

    def _safe_read(self, key: str) -> Any:
        try:
            return self.read(key)
        except Exception:                                   # noqa: BLE001
            return None

    def plan(self, keys: Iterable[str], described: Sequence[Setting],
             with_roi: bool) -> List[Optional[str]]:
        """The order to write `keys` in; None marks where the area goes.

        Describe order, which each provider lays out to be safe, except that
        a group's ".enabled" flag goes after that group's parameters.
        Unknown keys go last, so they are reported rather than dropped.
        """
        wanted = list(dict.fromkeys(keys))
        rank: Dict[str, Tuple[int, int, int]] = {}
        groups: Dict[str, int] = {}
        for index, setting in enumerate(described):
            group = groups.setdefault(setting.group, len(groups))
            flag = 1 if setting.key.endswith(".enabled") else 0
            rank[setting.key] = (group, flag, index)
        known = [k for k in wanted if k in rank]
        known.sort(key=lambda k: rank[k])
        out: List[Optional[str]] = list(known)
        if with_roi:
            after = self.roi_after_group
            spot = len(out)
            if after is not None and after in groups:
                limit = groups[after]
                spot = next((i for i, k in enumerate(out)
                             if k is not None and rank[k][0] > limit), len(out))
            out.insert(spot, None)
        out.extend(k for k in wanted if k not in rank)
        return out


class NoSettings(Provider):
    """A device that describes nothing: no settings is the honest answer."""

    def describe(self) -> List[Setting]:
        return []

    def read(self, key: str) -> Any:
        raise SettingError(f"this camera has no setting called {key!r}")

    def write(self, setting: Setting, value: Any,
              batch: Mapping[str, Any]) -> None:
        raise SettingError(f"this camera has no setting called "
                           f"{setting.key!r}")


def _why(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    # GenICam exceptions carry a file/line tail nobody needs to read.
    return text.split(" : ")[0][:200]


# ======================================================================
# Applying a set, and taking one
# ======================================================================
def _unchanged(setting: Setting, asked: Any) -> bool:
    """Would writing `asked` leave this setting as it is?"""
    try:
        wanted, _ = coerce(setting, asked)
    except SettingError:
        return False
    return same(wanted, setting.value)


def area_unchanged(device: Any, roi: Roi) -> bool:
    try:
        return device.roi() == fit_roi(roi, device.limits())
    except Exception:                                       # noqa: BLE001
        return False


def stops_needed(device: Any, values: Mapping[str, Any],
                 roi: Optional[Roi] = None,
                 described: Optional[Sequence[Setting]] = None) -> List[str]:
    """What in this set the stream is in the way of, by key ("area" for the
    area) — empty when it can all be written live, or nothing streams.

    Only CHANGES count: a locked setting asked for the value it already has
    is left alone by `apply`, so it is not a reason to stop the stream.
    """
    if not getattr(device, "streaming", False):
        return []
    if described is None:
        described = device.settings_provider().describe()
    by_key = {s.key: s for s in described}
    blocked = [key for key in values
               if key in by_key and not by_key[key].live
               and not by_key[key].read_only
               and not _unchanged(by_key[key], values[key])]
    if (roi is not None and not getattr(device, "area_live", False)
            and _fits(device, roi) and not area_unchanged(device, roi)):
        # An area off the sensor is refused, not written (apply): no reason
        # to stop the stream for it.
        blocked.append("area")
    return blocked


def _fits(device: Any, roi: Roi) -> bool:
    """The area overlaps this camera's sensor (cameras.on_sensor)."""
    try:
        return on_sensor(roi, device.limits())
    except Exception:                                       # noqa: BLE001
        return True                     # cannot tell: let the camera say


def needs_stop(device: Any, values: Mapping[str, Any],
               roi: Optional[Roi] = None) -> bool:
    """Would applying this set need the stream stopped?"""
    return bool(stops_needed(device, values, roi))


def apply(device: Any, values: Mapping[str, Any],
          roi: Optional[Roi] = None) -> Applied:
    """Write `values` (and the area, if given) in a safe order.

    Every key is accounted for in the result: written, adjusted, skipped
    (auto is on) or refused (`ok` False) — including keys this camera does
    not have, which happens when a preset made on another model of camera is
    applied.

    NeedsStop is raised BEFORE ANYTHING IS WRITTEN when the stream is in the
    way of any change in the set (`stops_needed`), so a half-applied set is
    never mistaken for a whole one: the caller stops the stream and calls
    again (frame_camera does that for a restartable camera).
    """
    provider = device.settings_provider()
    described = provider.describe()
    blocked = stops_needed(device, values, roi, described)
    if blocked:
        raise NeedsStop(blocked[0], f"{', '.join(blocked)} can only change "
                                    f"while the camera is not streaming")
    by_key = {s.key: s for s in described}
    done = Applied(roi_asked=roi)
    batch = dict(values)
    for key in provider.plan(values.keys(), described, roi is not None):
        if key is None:
            if not _fits(device, roi):
                # Written AFTER binning (plan order), so the sensor's size
                # is in the units the area is in.
                done.roi_error = off_sensor(roi, device.limits())
                continue
            try:
                if (getattr(device, "streaming", False)
                        and area_unchanged(device, roi)):
                    done.roi = device.roi()     # already so: nothing to stop
                else:
                    done.roi = device.set_roi(roi)
            except Exception as exc:                        # noqa: BLE001
                done.roi_error = _why(exc)
            continue
        asked = values[key]
        setting = by_key.get(key)
        if setting is None:
            done.changes.append(Change(key, asked, None, ok=False,
                                       note="this camera has no such setting"))
            continue
        if setting.read_only:
            done.changes.append(Change(key, asked, setting.value, ok=False,
                                       note="read-only on this camera"
                                       + (f" ({setting.help})"
                                          if setting.help else "")))
            continue
        if not setting.live and _unchanged(setting, asked):
            # Locked by the stream and already as asked: nothing to write.
            done.changes.append(Change(key, asked, setting.value))
            continue
        try:
            done.changes.append(provider.set(key, asked, batch=batch,
                                             setting=setting))
        except NeedsStop:
            raise
        except SettingError as exc:
            done.changes.append(Change(key, asked, setting.value, ok=False,
                                       note=str(exc)))
    return done


def snapshot(device: Any) -> Dict[str, Any]:
    """The values worth saving in a preset: every writable setting, as the
    camera has it now. Readings (a temperature) and labels are left out."""
    return {s.key: s.value for s in device.settings_provider().describe()
            if s.savable and s.value is not None}


# ======================================================================
# Basler — the pylon node map
# ======================================================================
@dataclass(frozen=True)
class _Feature:
    key: str
    names: Tuple[str, ...]
    label: str
    group: str
    unit: str = ""
    #: (other key, value it must have) for this one to mean anything.
    depends: Optional[Tuple[str, Any]] = None
    read_only: bool = False
    help: str = ""


#: The features worth a control, IN THE ORDER A SET IS WRITTEN. Which exist
#: is up to the camera; their ranges and entries come from it too. `names`
#: lists the spellings Basler's families use for the same thing (SFNC 2 vs
#: the older ace GigE), first match wins — a preset keeps the first spelling
#: as its key, so it reads the same on both.
BASLER_FEATURES: Tuple[_Feature, ...] = (
    _Feature("ExposureAuto", ("ExposureAuto",), "Auto exposure", "Exposure",
             help="Off to set the exposure time yourself."),
    _Feature("ExposureTime", ("ExposureTime", "ExposureTimeAbs"),
             "Exposure time", "Exposure", unit="µs",
             depends=("ExposureAuto", "Off")),
    _Feature("GainAuto", ("GainAuto",), "Auto gain", "Gain",
             help="Off to set the gain yourself."),
    _Feature("Gain", ("Gain", "GainAbs", "GainRaw"), "Gain", "Gain",
             depends=("GainAuto", "Off")),
    _Feature("BlackLevel", ("BlackLevel", "BlackLevelAbs", "BlackLevelRaw"),
             "Black level", "Gain"),
    _Feature("GammaEnable", ("GammaEnable",), "Gamma on", "Image"),
    _Feature("Gamma", ("Gamma",), "Gamma", "Image",
             depends=("GammaEnable", True)),
    _Feature("DigitalShift", ("DigitalShift",), "Digital shift", "Image",
             help="Multiplies pixel values by 2^n."),
    _Feature("PixelFormat", ("PixelFormat",), "Pixel format", "Image",
             help="Mono8 is the fastest to show and save; Mono10/12 keep "
                  "more of the sensor's range."),
    _Feature("ReverseX", ("ReverseX",), "Mirror left-right", "Image"),
    _Feature("ReverseY", ("ReverseY",), "Mirror top-bottom", "Image"),
    _Feature("BinningHorizontalMode", ("BinningHorizontalMode",),
             "Binning mode (horizontal)", "Binning"),
    _Feature("BinningHorizontal", ("BinningHorizontal",),
             "Binning (horizontal)", "Binning",
             help="Combines neighbouring pixels: a smaller, brighter, "
                  "faster picture. The area is in binned pixels."),
    _Feature("BinningVerticalMode", ("BinningVerticalMode",),
             "Binning mode (vertical)", "Binning"),
    _Feature("BinningVertical", ("BinningVertical",), "Binning (vertical)",
             "Binning"),
    _Feature("DecimationHorizontal", ("DecimationHorizontal",),
             "Decimation (horizontal)", "Binning"),
    _Feature("DecimationVertical", ("DecimationVertical",),
             "Decimation (vertical)", "Binning"),
    _Feature("AcquisitionFrameRateEnable", ("AcquisitionFrameRateEnable",),
             "Limit the frame rate", "Frame rate"),
    _Feature("AcquisitionFrameRate",
             ("AcquisitionFrameRate", "AcquisitionFrameRateAbs"),
             "Frame rate limit", "Frame rate", unit="fps",
             depends=("AcquisitionFrameRateEnable", True)),
    _Feature("SensorReadoutMode", ("SensorReadoutMode",), "Sensor readout",
             "Frame rate"),
    _Feature("ResultingFrameRate",
             ("BslResultingAcquisitionFrameRate", "ResultingFrameRate",
              "ResultingFrameRateAbs"),
             "Frame rate the camera will run at", "Frame rate", unit="fps",
             read_only=True),
    _Feature("DeviceTemperature", ("DeviceTemperature", "TemperatureAbs"),
             "Temperature", "Status", unit="°C", read_only=True),
)

#: pylon's unit strings, as a person writes them.
_UNITS = {"us": "µs", "Hz": "fps"}


class BaslerSettings(Provider):
    """A BaslerDevice's node map, as Settings.

    THE TYPE COMES FROM THE NODE, not from this table: an enumeration has
    GetSymbolics, and the value's own Python type tells bool, int, float and
    string apart — the same on pypylon's parameter classes and on a fake.
    """

    roi_after_group = "Binning"

    def __init__(self, device: Any):
        self.device = device

    # -- nodes -----------------------------------------------------------
    def _resolve(self, feature: _Feature) -> Optional[Tuple[str, Any]]:
        for name in feature.names:
            node = self.device._node(name)
            if node is not None and _readable(node):
                return name, node
        return None

    def _feature(self, key: str) -> _Feature:
        for feature in BASLER_FEATURES:
            if feature.key == key:
                return feature
        raise SettingError(f"this camera has no setting called {key!r}")

    def _streaming(self) -> bool:
        return bool(getattr(self.device, "_started", False))

    # -- describe --------------------------------------------------------
    def describe(self) -> List[Setting]:
        streaming = self._streaming()
        out = []
        for feature in BASLER_FEATURES:
            setting = self._describe_one(feature, streaming)
            if setting is not None:
                out.append(setting)
        return out

    def find(self, key: str) -> Setting:
        """ONE feature's nodes, not the whole table: a settings window
        writes one setting per step of a slider, and each write looks its
        setting up first."""
        setting = self._describe_one(self._feature(key), self._streaming())
        if setting is None:
            raise SettingError(f"this camera has no setting called {key!r}")
        return setting

    def _describe_one(self, feature: _Feature,
                      streaming: bool) -> Optional[Setting]:
        found = self._resolve(feature)
        if found is None:
            return None
        name, node = found
        setting = _node_setting(feature, name, node)
        if setting is None:
            return None
        held = self.held_by(setting, {})
        if feature.read_only or setting.kind == TEXT:
            setting = replace(setting, read_only=True)
        elif held:
            # Owned by an auto loop: not read-only — a set that turns
            # the loop off first still writes it.
            setting = replace(setting, held=held)
        elif not _writable(node):
            if streaming:
                # Locked by the stream (TLParamsLocked on the emulator:
                # PixelFormat, ReverseX/Y, Binning — measured).
                setting = replace(setting, live=False)
            else:
                setting = replace(setting, read_only=True)
        return setting

    def read(self, key: str) -> Any:
        found = self._resolve(self._feature(key))
        if found is None:
            raise SettingError(f"this camera has no {key}")
        return _plain(found[1].GetValue())

    def held_by(self, setting: Setting, batch: Mapping[str, Any]) -> str:
        feature = self._feature(setting.key)
        if feature.depends is None:
            return ""
        other, needed = feature.depends
        if other in batch:
            state = batch[other]
        else:
            found = self._resolve(self._feature(other))
            if found is None:
                return ""              # no auto loop on this model: write it
            state = _plain(found[1].GetValue())
        if isinstance(needed, bool):
            met = str(state).strip().lower() in _TRUE if not isinstance(
                state, bool) else state
            return "" if met == needed else f"{other} is off"
        return "" if str(state) == str(needed) else f"{other} is {state}"

    def write(self, setting: Setting, value: Any,
              batch: Mapping[str, Any]) -> None:
        found = self._resolve(self._feature(setting.key))
        if found is None:
            raise SettingError(f"this camera has no {setting.key}")
        name, node = found
        if not _writable(node):
            if self._streaming():
                raise NeedsStop(setting.key)
            raise SettingError(f"the camera does not allow changing "
                               f"{name} now")
        if setting.kind == INT and name == "GainRaw":
            value = int(value)
        node.SetValue(value)

    # -- factory settings: the user set every Basler carries -------------
    #: The UserSetSelector entry holding the factory settings (SFNC). It is
    #: read-only on the camera, so loading it can never be undone by a save.
    FACTORY_SET = "Default"

    def _user_set(self) -> Optional[Tuple[Any, Any]]:
        """(UserSetSelector, UserSetLoad), when this model has both and the
        selector offers the factory set."""
        selector = self.device._node("UserSetSelector")
        load = self.device._node("UserSetLoad")
        if selector is None or load is None:
            return None
        try:
            entries = {str(s) for s in selector.GetSymbolics()}
        except Exception:                                   # noqa: BLE001
            return None
        return (selector, load) if self.FACTORY_SET in entries else None

    def defaults_source(self) -> str:
        return f'UserSet "{self.FACTORY_SET}"' if self._user_set() else ""

    def load_defaults(self) -> None:
        """Select the factory user set and load it.

        ALWAYS WITH THE STREAM STOPPED. The set rewrites the area and the
        pixel format, which a Basler locks while grabbing; the emulator
        happens to leave UserSetLoad writable then (measured), and also
        ignores the load (Gain 5 dB stayed 5 dB, measured) — so this is
        checked against the node map's names only, never against what a
        real camera does with them."""
        found = self._user_set()
        if found is None:
            raise SettingError("this camera has no factory user set to load")
        if self._streaming():
            raise NeedsStop("defaults", "the camera's defaults can only be "
                                        "loaded while it is not streaming")
        selector, load = found
        selector.SetValue(self.FACTORY_SET)
        load.Execute()


def _readable(node: Any) -> bool:
    check = getattr(node, "IsReadable", None)
    if check is None:
        return True
    try:
        return bool(check())
    except Exception:                                       # noqa: BLE001
        return False


def _writable(node: Any) -> bool:
    check = getattr(node, "IsWritable", None)
    if check is None:
        return True
    try:
        return bool(check())
    except Exception:                                       # noqa: BLE001
        return False


def _plain(value: Any) -> Any:
    """A node's value as plain Python (numpy scalars and the like)."""
    if isinstance(value, (bool, int, float, str)):
        return value
    for kind in (bool, int, float):
        try:
            return kind(value) if not isinstance(value, str) else value
        except (TypeError, ValueError):
            continue
    return str(value)


def _node_setting(feature: _Feature, name: str, node: Any) -> Optional[Setting]:
    """A Setting for one pylon node — its type, range and entries read
    from the node itself."""
    try:
        value = _plain(node.GetValue())
    except Exception:                                       # noqa: BLE001
        return None
    symbolics = getattr(node, "GetSymbolics", None)
    unit = feature.unit
    raw_unit = _ask(node, "GetUnit")
    if not unit and raw_unit:
        unit = _UNITS.get(str(raw_unit), str(raw_unit))
    common = dict(key=feature.key, label=feature.label, group=feature.group,
                  unit=unit, help=feature.help)
    if callable(symbolics):
        try:
            choices = tuple(str(s) for s in symbolics())
        except Exception:                                   # noqa: BLE001
            choices = ()
        if value not in choices:
            choices = choices + (str(value),)
        return Setting(kind=CHOICE, value=str(value), choices=choices,
                       **common)
    if isinstance(value, bool):
        return Setting(kind=BOOL, value=value, **common)
    if isinstance(value, int):
        low, high = _ask(node, "GetMin"), _ask(node, "GetMax")
        step = _ask(node, "GetInc") or 1
        return Setting(kind=INT, value=value,
                       minimum=None if low is None else int(low),
                       maximum=None if high is None else int(high),
                       step=int(step), **common)
    if isinstance(value, float):
        low, high = _ask(node, "GetMin"), _ask(node, "GetMax")
        step = _ask(node, "GetInc") if _ask(node, "HasInc") else None
        return Setting(kind=FLOAT, value=value,
                       minimum=None if low is None else float(low),
                       maximum=None if high is None else float(high),
                       step=None if step is None else float(step), **common)
    return Setting(kind=TEXT, value=str(value), read_only=True, **common)


# ======================================================================
# Prophesee EVK4 — the Metavision HAL facilities
# ======================================================================
#: Every facility getter and method this provider calls, as the real OpenEB
#: bindings name them (introspected on OpenEB 5.x at C:/ceb/build). The test
#: suite checks each against metavision_hal where it is importable, so a
#: misspelt method fails a test instead of quietly hiding a control.
EVK_FACILITIES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "get_i_ll_biases": ("I_LL_Biases",
                        ("get_all_biases", "get_bias_info", "get", "set")),
    "get_i_erc_module": ("I_ErcModule",
                         ("is_enabled", "enable", "get_cd_event_rate",
                          "set_cd_event_rate",
                          "get_min_supported_cd_event_rate",
                          "get_max_supported_cd_event_rate",
                          "get_count_period")),
    "get_i_antiflicker_module": ("I_AntiFlickerModule",
                                 ("is_enabled", "enable", "get_filtering_mode",
                                  "set_filtering_mode",
                                  "get_band_low_frequency",
                                  "get_band_high_frequency",
                                  "set_frequency_band",
                                  "get_min_supported_frequency",
                                  "get_max_supported_frequency",
                                  "get_duty_cycle", "set_duty_cycle",
                                  "get_min_supported_duty_cycle",
                                  "get_max_supported_duty_cycle",
                                  "get_start_threshold", "set_start_threshold",
                                  "get_stop_threshold", "set_stop_threshold",
                                  "get_min_supported_start_threshold",
                                  "get_max_supported_start_threshold",
                                  "get_min_supported_stop_threshold",
                                  "get_max_supported_stop_threshold")),
    "get_i_event_trail_filter_module": ("I_EventTrailFilterModule",
                                        ("is_enabled", "enable", "get_type",
                                         "set_type", "get_available_types",
                                         "get_threshold", "set_threshold",
                                         "get_min_supported_threshold",
                                         "get_max_supported_threshold")),
    "get_i_event_rate": ("I_EventRateActivityFilterModule",
                         ("is_enabled", "enable", "get_thresholds",
                          "set_thresholds", "is_thresholds_supported",
                          "get_min_supported_thresholds",
                          "get_max_supported_thresholds")),
    "get_i_monitoring": ("I_Monitoring",
                         ("get_temperature", "get_illumination",
                          "get_pixel_dead_time")),
    "get_i_hw_identification": ("I_HW_Identification",
                                ("get_serial", "get_sensor_info",
                                 "get_current_data_encoding_format",
                                 "get_connection_type")),
    "get_i_roi": ("I_ROI", ("set_mode", "set_window", "enable")),
}

#: Group names, in display (and write) order.
G_BIASES = "Biases"
G_ERC = "Event rate controller"
G_AFK = "Anti-flicker"
G_TRAIL = "Event trail filter"
G_ACTIVITY = "Event rate activity filter"
G_VIEW = "Picture"
G_STATUS = "Status"
G_CAMERA = "Camera"

#: The activity filter's four thresholds, in the binding's own field names.
ACTIVITY_FIELDS = ("lower_bound_start", "lower_bound_stop",
                   "upper_bound_start", "upper_bound_stop")

#: The longest picture window offered: one picture a second. The window is
#: this app's display setting, so this is the one bound not read from the
#: camera.
MAX_WINDOW_MS = 1000.0

_BIAS_HELP = {
    "bias_diff_on": "Contrast threshold for ON (brighter) events: higher "
                    "means fewer, stronger events.",
    "bias_diff_off": "Contrast threshold for OFF (darker) events: higher "
                     "means fewer, stronger events.",
    "bias_fo": "Low-pass filter: lower filters out fast flicker and noise.",
    "bias_hpf": "High-pass filter: higher removes slow changes.",
    "bias_refr": "Refractory period: how long a pixel waits before it can "
                 "fire again.",
    "bias_diff": "Reference level the ON and OFF thresholds are set "
                 "against.",
}


class EvkSettings(Provider):
    """An EVK4's HAL facilities, as Settings.

    WRITTEN FROM THE BINDINGS, VERIFIED AGAINST THEM, NEVER RUN ON A CAMERA.
    There is no EVK4 on the machine this was written on. Every getter and
    method name used here is in EVK_FACILITIES and checked against the real
    OpenEB bindings by the tests; a file-backed device (what OpenEB opens a
    .raw as) has none of these facilities — measured: get_i_ll_biases,
    get_i_erc_module, get_i_roi and the rest all return None — so each
    facility is optional here and a missing one is simply not listed.

    THE HAL REPORTS SUCCESS AS A BOOL. set(), enable() and the set_* calls
    return False rather than raising when the sensor refuses, so a False is
    turned into a refusal here; ignoring it would report a bias the camera
    never took.
    """

    roi_after_group = None          # the area last: nothing depends on it

    def __init__(self, device: Any):
        self.device = device
        dev = getattr(device, "_dev", None)
        self.biases = _interface(dev, "get_i_ll_biases")
        self.erc = _interface(dev, "get_i_erc_module")
        self.afk = _interface(dev, "get_i_antiflicker_module")
        self.trail = _interface(dev, "get_i_event_trail_filter_module")
        self.activity = _interface(dev, "get_i_event_rate")
        self.monitor = _interface(dev, "get_i_monitoring")
        self.hw = _interface(dev, "get_i_hw_identification")

    # -- describe --------------------------------------------------------
    def describe(self) -> List[Setting]:
        out: List[Setting] = []
        out.extend(self._biases())
        out.extend(self._erc())
        out.extend(self._afk())
        out.extend(self._trail())
        out.extend(self._activity())
        out.append(self._window())
        out.extend(self._status())
        out.extend(self._camera())
        return out

    def find(self, key: str) -> Setting:
        """ONE facility's settings, not every facility's: on a live EVK4
        each read is a USB round trip, and a settings window looks a setting
        up before every write — one per step of a slider. A bias is one
        bias (its value and its info), not all of them."""
        group, _, rest = key.partition(".")
        if group == "bias" and rest and self.biases is not None:
            try:
                value = self.biases.get(rest)
            except Exception:                               # noqa: BLE001
                value = None
            if value is not None:
                return self._bias(rest, value)
        if key == "window_ms":
            # The app's own setting: no facility to read at all. It fell
            # through to describe() — every bias and filter, dozens of USB
            # reads on a live EVK4 — once per step of its slider.
            return self._window()
        part = {"erc": self._erc, "afk": self._afk, "trail": self._trail,
                "activity": self._activity, "status": self._status,
                "camera": self._camera}.get(group)
        for setting in (part() if part is not None else self.describe()):
            if setting.key == key:
                return setting
        raise SettingError(f"this camera has no setting called {key!r}")

    def _window(self) -> Setting:
        return Setting("window_ms", "Picture window", G_VIEW, FLOAT,
                       float(getattr(self.device, "accumulate_ms", 20.0)),
                       minimum=MIN_ACCUMULATE_MS, maximum=MAX_WINDOW_MS,
                       unit="ms",
                       help="How long each picture collects events "
                            "(1000 / pictures a second). The live view "
                            "and the PNGs only — the .raw has every "
                            "event.")

    def _biases(self) -> List[Setting]:
        if self.biases is None:
            return []
        try:
            values = dict(self.biases.get_all_biases())
        except Exception:                                   # noqa: BLE001
            return []
        return [self._bias(name, value) for name, value in values.items()]

    def _bias(self, name: Any, value: Any) -> Setting:
        info = _ask(self.biases, "get_bias_info", name)
        allowed = _pair(_ask(info, "get_bias_allowed_range")) or _pair(
            _ask(info, "get_bias_range"))
        recommended = _pair(_ask(info, "get_bias_recommended_range"))
        modifiable = _ask(info, "is_modifiable")
        described = _ask(info, "get_description") or ""
        help_text = _BIAS_HELP.get(str(name), "") or str(described)
        return Setting(
            f"bias.{name}", str(name), G_BIASES, INT, int(value),
            minimum=allowed[0] if allowed else None,
            maximum=allowed[1] if allowed else None, step=1,
            read_only=modifiable is False, help=help_text,
            recommended=recommended)

    def _erc(self) -> List[Setting]:
        erc = self.erc
        if erc is None:
            return []
        out = [Setting("erc.enabled", "Event rate controller on", G_ERC,
                       BOOL, bool(_ask(erc, "is_enabled")),
                       help="Drops events evenly once the rate passes the "
                            "limit, so a busy scene cannot swamp the link.")]
        rate = _ask(erc, "get_cd_event_rate")
        if rate is not None:
            out.append(Setting(
                "erc.rate", "Event rate limit", G_ERC, INT, int(rate),
                minimum=_num(_ask(erc, "get_min_supported_cd_event_rate")),
                maximum=_num(_ask(erc, "get_max_supported_cd_event_rate")),
                step=1, unit="ev/s"))
        period = _ask(erc, "get_count_period")
        if period is not None:
            out.append(Setting("erc.period", "Counting period", G_ERC, INT,
                               int(period), unit="µs", read_only=True))
        return out

    def _afk(self) -> List[Setting]:
        afk = self.afk
        if afk is None:
            return []
        out = [Setting("afk.enabled", "Anti-flicker on", G_AFK, BOOL,
                       bool(_ask(afk, "is_enabled")),
                       help="Removes events from lights flickering inside "
                            "the frequency band (mains lighting: 100/120 Hz).")]
        mode = _ask(afk, "get_filtering_mode")
        if mode is not None:
            names = _enum_names(getattr(type(afk), "AntiFlickerMode", None)
                                or type(mode))
            out.append(Setting("afk.mode", "Filter", G_AFK, CHOICE,
                               _enum_name(mode), choices=names,
                               help="BandStop removes the band; BandPass "
                                    "keeps only the band."))
        low_f = _num(_ask(afk, "get_min_supported_frequency"))
        high_f = _num(_ask(afk, "get_max_supported_frequency"))
        for key, label, getter in (("afk.low_hz", "Band from", "get_band_low_frequency"),
                                   ("afk.high_hz", "Band to", "get_band_high_frequency")):
            got = _ask(afk, getter)
            if got is not None:
                out.append(Setting(key, label, G_AFK, INT, int(got),
                                   minimum=low_f, maximum=high_f, step=1,
                                   unit="Hz"))
        duty = _ask(afk, "get_duty_cycle")
        if duty is not None:
            out.append(Setting(
                "afk.duty_cycle", "Duty cycle", G_AFK, FLOAT, float(duty),
                minimum=_num(_ask(afk, "get_min_supported_duty_cycle")),
                maximum=_num(_ask(afk, "get_max_supported_duty_cycle")),
                unit="%"))
        for key, label, stem in (("afk.start_threshold", "Start threshold", "start"),
                                 ("afk.stop_threshold", "Stop threshold", "stop")):
            got = _ask(afk, f"get_{stem}_threshold")
            if got is not None:
                out.append(Setting(
                    key, label, G_AFK, INT, int(got),
                    minimum=_num(_ask(afk, f"get_min_supported_{stem}_threshold")),
                    maximum=_num(_ask(afk, f"get_max_supported_{stem}_threshold")),
                    step=1))
        return out

    def _trail(self) -> List[Setting]:
        trail = self.trail
        if trail is None:
            return []
        out = [Setting("trail.enabled", "Trail filter on", G_TRAIL, BOOL,
                       bool(_ask(trail, "is_enabled")),
                       help="Removes the trail of events a moving edge "
                            "leaves behind (TRAIL), or keeps only "
                            "confirmed events (STC).")]
        kind = _ask(trail, "get_type")
        if kind is not None:
            available = _ask(trail, "get_available_types")
            names = tuple(sorted((_enum_name(t) for t in (available or ())),
                                 key=lambda n: _enum_rank(trail, n)))
            if not names:
                names = _enum_names(getattr(type(trail), "Type", None)
                                    or type(kind))
            out.append(Setting("trail.type", "Filter type", G_TRAIL, CHOICE,
                               _enum_name(kind), choices=names))
        threshold = _ask(trail, "get_threshold")
        if threshold is not None:
            out.append(Setting(
                "trail.threshold", "Threshold", G_TRAIL, INT, int(threshold),
                minimum=_num(_ask(trail, "get_min_supported_threshold")),
                maximum=_num(_ask(trail, "get_max_supported_threshold")),
                step=1, unit="µs"))
        return out

    def _activity(self) -> List[Setting]:
        module = self.activity
        if module is None:
            return []
        out = [Setting("activity.enabled", "Activity filter on", G_ACTIVITY,
                       BOOL, bool(_ask(module, "is_enabled")))]
        now = _ask(module, "get_thresholds")
        supported = _ask(module, "is_thresholds_supported")
        lows = _ask(module, "get_min_supported_thresholds")
        highs = _ask(module, "get_max_supported_thresholds")
        if now is None:
            return out
        for name in ACTIVITY_FIELDS:
            if supported is not None and not getattr(supported, name, 1):
                continue
            value = getattr(now, name, None)
            if value is None:
                continue
            out.append(Setting(
                f"activity.{name}", name.replace("_", " ").capitalize(),
                G_ACTIVITY, INT, int(value),
                minimum=_num(getattr(lows, name, None)),
                maximum=_num(getattr(highs, name, None)), step=1,
                unit="ev/s"))
        return out

    def _status(self) -> List[Setting]:
        out = []
        for key, label, getter, unit in (
                ("status.temperature", "Temperature", "get_temperature", "°C"),
                ("status.illumination", "Illumination", "get_illumination", "lux"),
                ("status.pixel_dead_time", "Pixel dead time",
                 "get_pixel_dead_time", "µs")):
            got = _ask(self.monitor, getter) if self.monitor is not None else None
            if got is not None:
                out.append(Setting(key, label, G_STATUS, INT, got, unit=unit,
                                   read_only=True))
        return out

    def _camera(self) -> List[Setting]:
        hw = self.hw
        if hw is None:
            return []
        out = []
        serial = _ask(hw, "get_serial")
        if serial:
            out.append(Setting("camera.serial", "Serial", G_CAMERA, TEXT,
                               str(serial), read_only=True))
        info = _ask(hw, "get_sensor_info")
        name = getattr(info, "name", "") if info is not None else ""
        if name:
            version = ""
            major = getattr(info, "major_version", None)
            if major is not None:
                version = f" {major}.{getattr(info, 'minor_version', 0)}"
            out.append(Setting("camera.sensor", "Sensor", G_CAMERA, TEXT,
                               f"{name}{version}", read_only=True))
        fmt = _ask(hw, "get_current_data_encoding_format")
        if fmt:
            out.append(Setting("camera.format", "Event format", G_CAMERA, TEXT,
                               str(fmt), read_only=True))
        return out

    # -- read / write ----------------------------------------------------
    def read(self, key: str) -> Any:
        """One value, read straight from its facility.

        Not by describing everything: on a live EVK4 every facility read is
        a USB register round trip, and the read-back after each write would
        otherwise re-read every bias and filter on the camera.
        """
        if key.startswith("bias."):
            return int(self.biases.get(key[5:]))
        if key == "window_ms":
            return float(self.device.accumulate_ms)
        module, _, part = key.partition(".")
        target = {"erc": self.erc, "afk": self.afk, "trail": self.trail,
                  "activity": self.activity}.get(module)
        if target is not None:
            if part == "enabled":
                return bool(target.is_enabled())
            if module == "activity" and part in ACTIVITY_FIELDS:
                return int(getattr(target.get_thresholds(), part))
            direct = _EVK_READERS.get(key)
            if direct is not None:
                got = getattr(target, direct)()
                return _enum_name(got) if key in _EVK_ENUMS else _plain(got)
        # A reading (temperature) or a camera fact: its own facility only.
        return self.find(key).value

    def write(self, setting: Setting, value: Any,
              batch: Mapping[str, Any]) -> None:
        key = setting.key
        if key.startswith("bias."):
            _accepted(self.biases.set(key[5:], int(value)), setting)
            return
        if key == "window_ms":
            self.device.accumulate_ms = max(MIN_ACCUMULATE_MS, float(value))
            return
        module, _, part = key.partition(".")
        target = {"erc": self.erc, "afk": self.afk, "trail": self.trail,
                  "activity": self.activity}.get(module)
        if target is None:
            raise SettingError(f"{setting.label} cannot be set")
        if part == "enabled":
            _accepted(target.enable(bool(value)), setting)
        elif key == "erc.rate":
            _accepted(target.set_cd_event_rate(int(value)), setting)
        elif key == "afk.mode":
            _accepted(target.set_filtering_mode(
                _enum_value(getattr(type(target), "AntiFlickerMode", None),
                            target.get_filtering_mode(), value)), setting)
        elif key in ("afk.low_hz", "afk.high_hz"):
            # ONE CALL SETS BOTH ENDS (set_frequency_band(min, max)), so the
            # other end comes from the same set when it has one — otherwise
            # moving a band upwards would be refused for crossing the old
            # upper end on its way.
            low = int(value) if key == "afk.low_hz" else _whole(batch.get(
                "afk.low_hz", target.get_band_low_frequency()))
            high = int(value) if key == "afk.high_hz" else _whole(batch.get(
                "afk.high_hz", target.get_band_high_frequency()))
            if low > high:
                raise SettingError(f"the band must run from low to high, "
                                   f"not {low} to {high} Hz")
            _accepted(target.set_frequency_band(low, high), setting)
        elif key == "afk.duty_cycle":
            _accepted(target.set_duty_cycle(float(value)), setting)
        elif key == "afk.start_threshold":
            _accepted(target.set_start_threshold(int(value)), setting)
        elif key == "afk.stop_threshold":
            _accepted(target.set_stop_threshold(int(value)), setting)
        elif key == "trail.type":
            _accepted(target.set_type(
                _enum_value(getattr(type(target), "Type", None),
                            target.get_type(), value)), setting)
        elif key == "trail.threshold":
            _accepted(target.set_threshold(int(value)), setting)
        elif module == "activity" and part in ACTIVITY_FIELDS:
            now = target.get_thresholds()
            setattr(now, part, int(value))
            _accepted(target.set_thresholds(now), setting)
        else:
            raise SettingError(f"{setting.label} cannot be set")


#: Facility getters for EvkSettings.read, by key (the enable flags and the
#: activity thresholds are handled there).
_EVK_READERS: Dict[str, str] = {
    "erc.rate": "get_cd_event_rate",
    "afk.mode": "get_filtering_mode",
    "afk.low_hz": "get_band_low_frequency",
    "afk.high_hz": "get_band_high_frequency",
    "afk.duty_cycle": "get_duty_cycle",
    "afk.start_threshold": "get_start_threshold",
    "afk.stop_threshold": "get_stop_threshold",
    "trail.type": "get_type",
    "trail.threshold": "get_threshold",
}
_EVK_ENUMS = ("afk.mode", "trail.type")


def _whole(value: Any) -> int:
    """An int from whatever a set holds ("150", 150.0, 150)."""
    try:
        return int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        raise SettingError(f"{value!r} is not a number") from None


def _accepted(result: Any, setting: Setting) -> None:
    """The HAL's bool answer as a refusal. None (a void binding) is taken."""
    if result is False:
        raise SettingError(f"the camera refused that {setting.label.lower()}")


def _pair(value: Any) -> Optional[Tuple[float, float]]:
    try:
        low, high = value
        return (float(low) if not float(low).is_integer() else int(low),
                float(high) if not float(high).is_integer() else int(high))
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if name:
        return str(name)
    text = str(value)
    return text.rsplit(".", 1)[-1]


def _enum_names(enum_type: Any) -> Tuple[str, ...]:
    members = getattr(enum_type, "__members__", None)
    if not members:
        return ()
    return tuple(sorted(members, key=lambda n: int(_enum_int(members[n]))))


def _enum_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _enum_rank(module: Any, name: str) -> int:
    members = getattr(getattr(type(module), "Type", None), "__members__", None)
    if members and name in members:
        return _enum_int(members[name])
    return 0


def _enum_value(enum_type: Any, current: Any, name: Any) -> Any:
    """The binding's enum member called `name`. From the facility's own
    class attribute (I_AntiFlickerModule.AntiFlickerMode,
    I_EventTrailFilterModule.Type), else from the current value's type."""
    for source in (enum_type, type(current)):
        members = getattr(source, "__members__", None)
        if members and str(name) in members:
            return members[str(name)]
    raise SettingError(f"{name!r} is not one this camera offers")


# ======================================================================
# The simulated cameras — a small, realistic set, honoured by the picture
# ======================================================================
class SyntheticSettings(Provider):
    """SyntheticDevice's settings: the same KEYS as the real cameras, so
    presets and the settings window are exercised without hardware.

    A frame camera has Basler's names (ExposureTime, Gain, BlackLevel,
    PixelFormat, ReverseX, AcquisitionFrameRate) and, like a real Basler,
    refuses PixelFormat and ReverseX while it streams (NeedsStop). An event
    camera has an IMX636's biases, an ERC and a trail filter, and the
    picture window. The ranges are the device's own (`SyntheticDevice.
    RANGES`), read here — not copied.
    """

    roi_after_group = "Image"

    def __init__(self, device: Any):
        self.device = device

    def describe(self) -> List[Setting]:
        dev = self.device
        state = dev.state
        ranges = dev.RANGES
        out: List[Setting] = []
        if dev.info.kind == "event":
            for name in dev.BIASES:
                low, high = ranges[f"bias.{name}"]
                out.append(Setting(f"bias.{name}", name, G_BIASES, INT,
                                   state[f"bias.{name}"], low, high, 1,
                                   help=_BIAS_HELP.get(name, ""),
                                   recommended=(max(low, -50), min(high, 50))))
            out.append(Setting("erc.enabled", "Event rate controller on",
                               G_ERC, BOOL, state["erc.enabled"]))
            low, high = ranges["erc.rate"]
            out.append(Setting("erc.rate", "Event rate limit", G_ERC, INT,
                               state["erc.rate"], low, high, 1, unit="ev/s"))
            out.append(Setting("trail.enabled", "Trail filter on", G_TRAIL,
                               BOOL, state["trail.enabled"]))
            out.append(Setting("trail.type", "Filter type", G_TRAIL, CHOICE,
                               state["trail.type"],
                               choices=dev.TRAIL_TYPES))
            low, high = ranges["trail.threshold"]
            out.append(Setting("trail.threshold", "Threshold", G_TRAIL, INT,
                               state["trail.threshold"], low, high, 1,
                               unit="µs"))
            out.append(Setting("window_ms", "Picture window", G_VIEW, FLOAT,
                               float(dev.accumulate_ms),
                               minimum=MIN_ACCUMULATE_MS,
                               maximum=MAX_WINDOW_MS, unit="ms"))
            out.append(Setting("status.temperature", "Temperature", G_STATUS,
                               INT, 31, unit="°C", read_only=True))
            return out
        streaming = bool(getattr(dev, "_started", False))
        low, high = ranges["ExposureTime"]
        out.append(Setting("ExposureTime", "Exposure time", "Exposure", FLOAT,
                           state["ExposureTime"], low, high, unit="µs"))
        low, high = ranges["Gain"]
        out.append(Setting("Gain", "Gain", "Gain", FLOAT, state["Gain"],
                           low, high, unit="dB"))
        low, high = ranges["BlackLevel"]
        out.append(Setting("BlackLevel", "Black level", "Gain", INT,
                           state["BlackLevel"], low, high, 1))
        out.append(Setting("PixelFormat", "Pixel format", "Image", CHOICE,
                           state["PixelFormat"], choices=dev.PIXEL_FORMATS,
                           live=not streaming))
        out.append(Setting("ReverseX", "Mirror left-right", "Image", BOOL,
                           state["ReverseX"], live=not streaming))
        out.append(Setting("AcquisitionFrameRateEnable",
                           "Limit the frame rate", "Frame rate", BOOL,
                           state["AcquisitionFrameRateEnable"]))
        low, high = ranges["AcquisitionFrameRate"]
        rate = Setting("AcquisitionFrameRate", "Frame rate limit",
                       "Frame rate", FLOAT, state["AcquisitionFrameRate"],
                       low, high, unit="fps")
        out.append(replace(rate, held=self.held_by(rate, {})))
        out.append(Setting("DeviceTemperature", "Temperature", G_STATUS,
                           FLOAT, 38.5, unit="°C", read_only=True))
        return out

    def read(self, key: str) -> Any:
        if key == "window_ms":
            return float(self.device.accumulate_ms)
        if key in self.device.state:
            return self.device.state[key]
        raise SettingError(f"this camera has no setting called {key!r}")

    def held_by(self, setting: Setting, batch: Mapping[str, Any]) -> str:
        if setting.key != "AcquisitionFrameRate":
            return ""
        on = batch.get("AcquisitionFrameRateEnable",
                       self.device.state["AcquisitionFrameRateEnable"])
        on = on if isinstance(on, bool) else str(on).lower() in _TRUE
        return "" if on else "AcquisitionFrameRateEnable is off"

    def write(self, setting: Setting, value: Any,
              batch: Mapping[str, Any]) -> None:
        if setting.key == "window_ms":
            self.device.accumulate_ms = max(MIN_ACCUMULATE_MS, float(value))
            return
        self.device.write_setting(setting.key, value)

    def defaults_source(self) -> str:
        """The simulated frame camera has factory settings to load, as a
        Basler does; the simulated event camera has none, as an EVK4 has
        none."""
        if self.device.info.kind == "event":
            return ""
        return "the simulated camera's defaults"

    def load_defaults(self) -> None:
        if not self.defaults_source():
            raise SettingError("this camera has no defaults of its own to "
                               "load")
        self.device.load_defaults()
