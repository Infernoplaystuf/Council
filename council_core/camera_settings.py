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
enumeration only the entries this model offers. An EVK4 bias reports
get_bias_range() — as Typhon opens one, the only range its HAL accepts —
and the recommended one through LL_Bias_Info (EvkSettings says why that is
not the "allowed" range); the ERC,
anti-flicker and trail filter report their supported bounds. The tables here say only WHICH
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
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple)

from .cameras import (CameraError, DEFAULT_PALETTE, EVENT_PALETTES,
                      EVENT_SHOWS, MIN_ACCUMULATE_MS, SHOW_BOTH, NeedsStop,
                      Roi, _ask, _interface, fit_roi, nearest_whole,
                      off_sensor, on_sensor, said_plainly)

__all__ = ["Setting", "Change", "Applied", "SettingError", "NeedsStop",
           "Trigger", "trigger_warning", "area_unchanged",
           "coerce", "apply", "snapshot", "needs_stop", "stops_needed"]

#: What a setting holds.
FLOAT, INT, BOOL, CHOICE, TEXT = "float", "int", "bool", "choice", "text"

#: Groups whose settings change the camera's AREA itself, not only the
#: picture: a binned (or decimated) Basler's AOI — offsets, width, height
#: and the sensor's own size — is in binned pixels, so changing the binning
#: changes every one of those numbers. Whoever shows the area, or holds a
#: box drawn on the old picture, has to hear about it (frame_camera).
AREA_GROUPS = frozenset({"Binning"})


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
        ('"Auto exposure" is Continuous', by the owner's label): grey it
        out and say why. A preset that turns the auto loop off in the same
        set still writes it.
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
    #: What this value does to the picture that a person must be told, or
    #: "": a Basler's TriggerMode On means no frame until a trigger arrives,
    #: which froze the live view with no word said (trigger_warning).
    warning: str = ""
    #: The unit a person is SHOWN it in, when not `unit`, and how many of
    #: `unit` make one of it. A trail filter's threshold is µs to the HAL
    #: and in a preset, but the sensor keeps whole milliseconds: it steps
    #: by 1000 µs and is shown in ms ("ms", 1000). The value, its range and
    #: its step stay in `unit`; only what is shown is divided.
    shown_unit: str = ""
    shown_per: float = 1.0
    #: Chosen before a run and FIXED WHILE CAPTURING, though writing it
    #: never needs the stream stopped: the capture set-up (Continuous or a
    #: trigger, its source, Downsample — council_core.triggered). One run
    #: keeps one set-up, so its frames all mean the same thing.
    per_run: bool = False
    #: The key of the setting that SETS this one now, or "": a Basler's
    #: binning while Downsample bins the camera (save.downsample). A preset
    #: keeps the owner, not this — applied, the owner sets it again (and
    #: puts it back as it was when it goes Off).
    owned_by: str = ""

    @property
    def savable(self) -> bool:
        """Belongs in a preset: something the user chose, not a reading."""
        return not self.read_only and self.kind != TEXT and not self.owned_by

    def shown(self) -> Tuple[str, float]:
        """(the unit a person sees this in, how many of `unit` to one)."""
        if self.shown_unit:
            return self.shown_unit, float(self.shown_per or 1.0)
        return self.unit, 1.0

    def as_dict(self) -> Dict[str, Any]:
        """Plain data, for a window or a JSON file."""
        return {"key": self.key, "label": self.label, "group": self.group,
                "type": self.kind, "value": self.value,
                "min": self.minimum, "max": self.maximum, "step": self.step,
                "choices": list(self.choices), "unit": self.unit,
                "shown_unit": self.shown_unit, "shown_per": self.shown_per,
                "read_only": self.read_only, "live": self.live,
                "held": self.held, "warning": self.warning,
                "help": self.help, "per_run": self.per_run,
                "owned_by": self.owned_by,
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

    def line(self, label: str = "", unit: str = "", per: float = 1.0) -> str:
        """One line for a status bar or a list — said by `label`, the name
        on the setting's row, with its `unit`, when given. The key alone
        ("window_ms: 99.901", "PixelFormat: Mono12") was what every tab's
        and pop-out's status line said under a row called "Picture window"
        or "Pixel format". `per`: how many of the value's own unit make one
        of `unit` (Setting.shown) — a trail threshold of 10000 µs is said
        "10 ms", as its row shows it."""
        name = label or self.key
        per = float(per or 1.0)

        def number(value: Any) -> Any:
            if per != 1.0 and isinstance(value, (int, float)) \
                    and not isinstance(value, bool):
                return value / per
            return value

        def said(value: Any) -> str:
            text = show(number(value))
            numeric = isinstance(value, (int, float)) and not isinstance(
                value, bool)
            return f"{text} {unit}" if unit and numeric else text

        if not self.ok:
            return f"{name}: NOT changed — {self.note}"
        if self.skipped:
            return f"{name}: left at {said(self.value)} — {self.note}"
        if self.adjusted:
            why = f" ({self.note})" if self.note else ""
            return (f"{name}: {said(self.value)}, asked "
                    f"{show(number(self.asked))}{why}")
        return f"{name}: {said(self.value)}"

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

    def summary(self, labels: Optional[Mapping[str, str]] = None) -> str:
        """One sentence: how many took, and the first thing that did not —
        named by its label when `labels` (key: label) has it."""
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
            name = (labels or {}).get(first.key) or first.key
            bits.append(f"{len(self.refused)} refused ({name}: "
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
    reads back 2.999994 on the emulator — said as 3, not 2.99999); on and
    off are words, as the settings window says them, not True and False."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{round(value, 4):g}"
    return str(value)


# ======================================================================
# A camera that waits for a trigger
# ======================================================================
@dataclass(frozen=True)
class Trigger:
    """One of the camera's triggers that is ON: the camera waits for a
    signal from `source` before what `selector` names (a frame, a burst, an
    exposure) — so, until one arrives, it sends nothing.

    MEASURED on pylon's emulator: TriggerMode On (its source is Software by
    default) stopped the frames at once; the live line kept its last
    "62.5 fps", and a 4 s capture saved nothing, with no word on why.
    Typhon sends a software trigger only in a "Triggered: exact" run,
    which arms the trigger itself (council_core.triggered) — never for a
    trigger left On in the rows."""
    selector: str
    source: str

    @property
    def software(self) -> bool:
        """Only a program can fire it — and only a triggered run does."""
        return self.source.strip().lower().startswith("software")

    def said(self) -> str:
        which = f"{self.selector} trigger" if self.selector else "trigger"
        return f"its {which} is On (source {self.source or 'unknown'})"


#: A trigger's own settings: a Basler holds one value of each PER
#: TriggerSelector entry (SFNC; on the emulator each entry's mode, source,
#: edge and delay changed alone — measured), and its rows show the selected
#: entry's. In the order a set writes them — every other trigger's in
#: apply_triggers, the selected one's in BaslerSettings.plan: the mode LAST,
#: so a trigger is never On while its source and edge are still the old
#: ones.
TRIGGER_FEATURES: Tuple[str, ...] = ("TriggerSource", "TriggerActivation",
                                     "TriggerDelay", "TriggerMode")


def trigger_key(entry: str, key: str) -> str:
    """A preset's key for one trigger's setting, other than the selected
    trigger's: "TriggerMode[FrameBurstStart]". Still a plain name: value
    pair, so a presets file keeps its format, and an older Typhon applying
    one reports these keys as settings it does not know, not as damage."""
    return f"{key}[{entry}]"


def split_trigger_key(key: Any) -> Optional[Tuple[str, str]]:
    """(setting key, selector entry) for a trigger_key, else None."""
    text = str(key)
    if not text.endswith("]"):
        return None
    base, mark, entry = text[:-1].partition("[")
    if not mark or not entry or base not in TRIGGER_FEATURES:
        return None
    return base, entry


def trigger_warning(source: str) -> str:
    """Under a TriggerMode row that is On: what that does to the picture."""
    if Trigger("", source).software:
        return ("No picture: it waits for a software trigger, and nothing "
                "here sends one in the live view — set it Off (to capture "
                "on a trigger, choose Capture: Triggered: exact)")
    return (f"No picture until a trigger arrives on {source or 'its source'}")


# ======================================================================
# Downsample: the core's row in every frame camera's "Capture" group
# ======================================================================
def downsample_setting(factor: Any, options: Any = None) -> Setting:
    """THE CORE'S DOWNSAMPLE ROW (docs/typhon_plugins_design.md 2.1, 2.5):
    Downsample, "save.downsample" (Off, 2x, 3x, 4x), in the "Capture" group
    of every frame camera with a capture set-up, chosen before a run and
    fixed while capturing. Downsample is the core's, not the triggering
    plugin's: this row is Typhon's whatever the camera, and the camera's
    `options` (CaptureOptions) say only whether it can change while the
    stream runs (camera binning needs it stopped) and how it is made there
    (downsample_live, downsample_help).

    The key, its choices and its words stay in council_core.triggered,
    where today's set-up has them, until the files move at stage P7
    (design 7.2); today's providers build their Capture group with this
    row (triggered.capture_settings), so both Typhons describe it alike."""
    from . import triggered

    live = getattr(options, "downsample_live", True)
    words = str(getattr(options, "downsample_help", "") or "")
    return Setting(triggered.DOWNSAMPLE_KEY, "Downsample", triggered.GROUP,
                   CHOICE, triggered.downsample_text(factor),
                   choices=triggered.DOWNSAMPLES, live=bool(live),
                   per_run=True, help=words or triggered.DOWNSAMPLE_HELP)


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
        except OverflowError:
            # A whole number no float holds (10**400 read from a file):
            # refused as this setting's, so a set reports it by name and
            # writes the rest — it stopped a preset half way, unnamed.
            raise SettingError(f"{setting.label} must be a number a camera "
                               f"can hold, not one of {len(str(value))} "
                               f"digits") from None
        if math.isnan(number) or math.isinf(number):
            raise SettingError(f"{setting.label} must be a finite number")
        note = ""
        low, high = setting.minimum, setting.maximum
        shown_unit, per = setting.shown()

        def bound(value: Any) -> str:
            # In the unit the row SHOWS: "Threshold: 1 ms, asked 0.5
            # (clamped to the minimum, 1000)" gave it in µs, in an ms line.
            if per != 1.0:
                return f"{show(float(value) / per)} {shown_unit}".rstrip()
            return show(value)

        if low is not None and number < low:
            number, note = float(low), f"clamped to the minimum, {bound(low)}"
        if high is not None and number > high:
            number, note = (float(high),
                            f"clamped to the maximum, {bound(high)}")
        if kind == INT:
            step = int(setting.step or 1)
            # Stepped by one whole unit of what it is shown in (a trail
            # threshold: whole ms, in µs): snapped AS THE SENSOR ROUNDS IT,
            # from zero and a half away from zero (cameras.nearest_whole,
            # OpenEB's std::roundf). round() from the minimum took 3500 µs
            # to 3 ms where an IMX636 keeps 4. A Basler's integer node keeps
            # this snap: from its minimum, by its increment.
            whole = per != 1.0 and step == per
            if whole:
                snapped = nearest_whole(number / step) * step
            else:
                base = int(low) if low is not None else 0
                snapped = base + int(round((number - base) / step)) * step
            if high is not None and snapped > high:
                snapped -= step
            if whole and low is not None and snapped < low:
                snapped += step
            if snapped != number and not note:
                note = (f"snapped to whole {shown_unit}" if whole
                        else f"snapped to a multiple of {step}")
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

    #: The keys whose value may carry a `warning` (Setting.warning): after a
    #: write of one, the caller reads it again to say it.
    warns: frozenset = frozenset()

    def describe(self) -> List[Setting]:
        raise NotImplementedError

    def describe_groups(self, groups: Iterable[str]) -> List[Setting]:
        """Only the settings in `groups`, in describe order.

        A pop-out window shows one category and reads it again after every
        drag; describing the WHOLE camera for that is a USB round trip per
        bias, filter and reading on an EVK4. Providers that can read one
        group alone override this; the default filters describe()."""
        wanted = set(groups)
        return [s for s in self.describe() if s.group in wanted]

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

    # -- triggers: what makes a camera send nothing ------------------------
    def triggers_on(self) -> List[Trigger]:
        """Every trigger the camera waits for now — all of them, not only
        the one its selector shows. [] for a camera with no trigger (an
        EVK4 here, the simulated cameras): it runs free."""
        return []

    def restore_triggers(self, values: Mapping[str, Any]) -> List[str]:
        """Put EVERY trigger back as `values` has it — trigger_settings(
        selected=True), read when the camera was connected: each one's
        source, edge and delay, then its mode, the selector left where it
        was. What changed or could not be put back, one line per trigger.
        [] for a camera with no triggers."""
        return []

    def trigger_settings(self, selected: bool = False) -> Dict[str, Any]:
        """Every OTHER trigger's settings, for a preset: {"TriggerMode
        [FrameBurstStart]": "On", ...} (trigger_key). The selected trigger's
        are the plain keys a snapshot already holds — with `selected` they
        are listed this way too (to compare a preset with the camera). {}
        for a camera with one trigger or none."""
        return {}

    def apply_triggers(self, values: Mapping[str, Any]) -> List[Change]:
        """Write trigger_settings() values back, each trigger selected in
        turn, the selector left where it was; one Change per key. A camera
        without them refuses each, by name."""
        return [Change(key, asked, None, ok=False,
                       note="this camera has no such setting")
                for key, asked in values.items()]

    def unreadable_triggers(self) -> List[str]:
        """The triggers trigger_settings cannot read now, by selector entry
        (a selector locked by the stream), so a preset saved now would not
        hold them; [] when it reads them all, or there are none."""
        return []

    # -- what else the stream is in the way of (plugin design 2.1) ---------
    def extra_stops(self, values: Mapping[str, Any],
                    described: Sequence[Setting]) -> List[str]:
        """The keys of `values` — a set's keys that no row of `described`
        (the camera's description) has — that the stream is in the way of
        on THIS camera; [] by default. `stops_needed` checks the described
        rows itself and asks this for the rest, so the generic check names
        no camera: a Basler's other triggers ("TriggerMode[FrameBurstStart]",
        trigger_key) are its own (BaslerSettings)."""
        return []

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
    # An OpenEB exception's text is a block — rules of dashes, the
    # category, "Error <code>: " (cameras.said_plainly): its message alone,
    # or a row's one-line "NOT changed —" note was a run of dashes.
    text = said_plainly(str(exc)).strip() or type(exc).__name__
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


def _would_change(setting: Setting, asked: Any) -> bool:
    """Would `asked` be WRITTEN and change this setting? Not when it is
    already so, and not when it cannot be taken at all (a pixel format
    this camera does not offer, a word where a number goes): that is
    refused before any write, so it is no reason to stop the stream —
    measured, a preset holding a format the camera no longer offers
    stopped and restarted the live view only to be refused."""
    try:
        wanted, _ = coerce(setting, asked)
    except SettingError:
        return False
    return not same(wanted, setting.value)


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
    provider = None
    if described is None:
        provider = device.settings_provider()
        described = provider.describe()
    by_key = {s.key: s for s in described}
    blocked = [key for key in values
               if key in by_key and not by_key[key].live
               and not by_key[key].read_only
               and _would_change(by_key[key], values[key])]
    # The keys no row describes, asked of the camera's own provider (a
    # Basler's other triggers: BaslerSettings.extra_stops).
    blocked += _extra_stops(device, provider, {
        key: value for key, value in values.items() if key not in by_key},
        described)
    if (roi is not None and not getattr(device, "area_live", False)
            and _fits(device, roi) and not area_unchanged(device, roi)):
        # An area off the sensor is refused, not written (apply): no reason
        # to stop the stream for it.
        blocked.append("area")
    return blocked


def _extra_stops(device: Any, provider: Any, values: Mapping[str, Any],
                 described: Sequence[Setting]) -> List[str]:
    """What the camera's provider says the stream is in the way of among
    `values`, the keys no described row has (Provider.extra_stops) — a
    provider only made for it when there are such keys. A provider without
    the hook (a stand-in) adds nothing."""
    if not values:
        return []
    if provider is None:
        try:
            provider = device.settings_provider()
        except Exception:                                   # noqa: BLE001
            return []
    ask = getattr(provider, "extra_stops", None)
    if not callable(ask):
        return []
    return list(ask(values, described))


def _locked(setting: Optional[Setting]) -> bool:
    """Locked by the stream (and writable once it stops)."""
    return setting is not None and not setting.live and not setting.read_only


def _triggers_in_the_way(device: Any, values: Mapping[str, Any],
                         by_key: Mapping[str, Setting]) -> List[str]:
    """The other triggers' settings (trigger_key) the stream is in the way
    of. Writing one selects its trigger, then writes the node the rows show
    for the selected one (SFNC), so it is in the way when that node is
    locked by the stream — or the SELECTOR is, and the trigger is not the
    selected one.

    ONLY CHANGES COUNT, as for the rows: every one was counted whenever
    the selected trigger's node was locked, so a preset just saved stopped
    and restarted the live view for nothing (measured on the test fake).
    So the triggers are read (trigger_settings: only the selector moves,
    and is put back) and compared. One that cannot be read — the selector
    locked — cannot be known unchanged, and counts. A trigger the camera
    does not have is refused by apply_triggers, nothing written: no reason
    to stop. (On the emulator every trigger node, the selector too, stays
    writable while grabbing — measured — so none of this costs it a read.)
    """
    asked = {key: split for key in values
             for split in [split_trigger_key(key)] if split}
    if not asked:
        return []
    selector = by_key.get("TriggerSelector")
    if not _locked(selector) and not any(_locked(by_key.get(base))
                                         for base, _ in asked.values()):
        return []
    entries = set(selector.choices) if selector is not None else set()
    current = str(selector.value) if selector is not None else ""
    try:
        now: Optional[Mapping[str, Any]] = dict(
            device.settings_provider().trigger_settings(selected=True))
    except Exception:                                       # noqa: BLE001
        now = None
    out: List[str] = []
    for key, (base, entry) in asked.items():
        shown = by_key.get(base)
        if shown is None or shown.read_only or entry not in entries:
            continue                    # refused, not written
        moves = _locked(selector) and entry != current
        if not (moves or _locked(shown)):
            continue
        if now is None or key not in now:
            out.append(key)             # cannot tell: it may change
            continue
        try:
            wanted, _ = coerce(shown, values[key])
        except SettingError:
            continue                    # refused before any write
        if not same(wanted, now[key]):
            out.append(key)
    return out


def labels_of(described: Sequence[Setting],
              keys: Iterable[str] = ()) -> Dict[str, str]:
    """key: label, for a person — every described setting's, and each
    trigger_key among `keys` as its row says it, with the trigger named:
    "Trigger mode (FrameBurstStart trigger)". The restart message said
    "The live view restarted for TriggerMode[FrameBurstStart]"."""
    out = {s.key: s.label for s in described}
    table = {f.key: f.label for f in BASLER_FEATURES}
    for key in keys:
        split = split_trigger_key(key)
        if split is not None:
            base, entry = split
            out[key] = (f"{out.get(base) or table.get(base, base)} "
                        f"({entry} trigger)")
    return out


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

    The OTHER TRIGGERS' settings (trigger_key: "TriggerMode[FrameBurstStart]")
    go last, through the provider's apply_triggers, which selects each
    trigger in turn and leaves the selector where this set put it — where
    the user had it when the preset was saved.
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
    triggers = {k: v for k, v in values.items() if split_trigger_key(k)}
    plain = [k for k in values if k not in triggers]
    for key in provider.plan(plain, described, roi is not None):
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
    if triggers:
        done.changes.extend(provider.apply_triggers(triggers))
    return done


def snapshot(device: Any,
             described: Optional[Sequence[Setting]] = None) -> Dict[str, Any]:
    """The values worth saving in a preset: every writable setting, as the
    camera has it now. Readings (a temperature) and labels are left out.
    `described`: the camera's description, when it was just read."""
    if described is None:
        described = device.settings_provider().describe()
    return {s.key: s.value for s in described
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
    # THE TRIGGER, selector first: TriggerMode, Source, Activation and
    # Delay are the selected trigger's (SFNC), so the selector is written
    # before them. With a trigger On the camera waits for one before each
    # frame — the live view shows nothing until one arrives.
    _Feature("TriggerSelector", ("TriggerSelector",), "Trigger",
             "Trigger", help="Which trigger the settings below are for — "
                             "FrameStart starts each frame. Each trigger "
                             "has its own mode, source, edge and delay: "
                             "these rows show the selected one only (a "
                             "preset keeps every trigger's)."),
    _Feature("TriggerMode", ("TriggerMode",), "Trigger mode", "Trigger",
             help="On: the camera waits for the trigger selected above "
                  "before each frame, and the live view shows nothing until "
                  "one arrives. Off: it runs free. For the selected trigger "
                  "only — Reset puts every trigger back as connected."),
    _Feature("TriggerSource", ("TriggerSource",), "Trigger source",
             "Trigger", help="Where the trigger comes from: an input line, "
                             "software, a timer …"),
    _Feature("TriggerActivation", ("TriggerActivation",),
             "Trigger on", "Trigger",
             help="Which edge or level of the signal triggers."),
    _Feature("TriggerDelay", ("TriggerDelay", "TriggerDelayAbs"),
             "Trigger delay", "Trigger", unit="µs"),
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
    warns = frozenset({"TriggerMode"})

    def __init__(self, device: Any):
        self.device = device

    def extra_stops(self, values: Mapping[str, Any],
                    described: Sequence[Setting]) -> List[str]:
        """The other triggers' settings (trigger_key) the stream is in the
        way of (Provider.extra_stops; _triggers_in_the_way)."""
        return _triggers_in_the_way(self.device, values,
                                    {s.key: s for s in described})

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
        # The capture set-up last (council_core.triggered): Typhon's own,
        # read from the device, not the node map — but its Downsample is
        # written BEFORE the area by plan().
        out.extend(self._capture_rows())
        return out

    def describe_groups(self, groups: Iterable[str]) -> List[Setting]:
        """The nodes of `groups` only — not the whole table."""
        from . import triggered

        wanted = set(groups)
        streaming = self._streaming()
        out = []
        for feature in BASLER_FEATURES:
            if feature.group not in wanted:
                continue
            setting = self._describe_one(feature, streaming)
            if setting is not None:
                out.append(setting)
        if triggered.GROUP in wanted:
            out.extend(self._capture_rows())
        return out

    def find(self, key: str) -> Setting:
        """ONE feature's nodes, not the whole table: a settings window
        writes one setting per step of a slider, and each write looks its
        setting up first."""
        from . import triggered

        if triggered.is_capture_key(key):
            for setting in self._capture_rows():
                if setting.key == key:
                    return setting
            raise SettingError(f"this camera has no setting called {key!r}")
        setting = self._describe_one(self._feature(key), self._streaming())
        if setting is None:
            raise SettingError(f"this camera has no setting called {key!r}")
        return setting

    def _capture_rows(self) -> List[Setting]:
        from . import triggered

        setup = getattr(self.device, "capture", None)
        if setup is None:
            return []
        return triggered.capture_settings(setup, self.capture_options())

    def _downsample_owns_binning(self, batch: Mapping[str, Any]) -> str:
        """'"Downsample" is 2x' while Typhon's Downsample bins the camera
        (and the set being written does not turn it Off), else ""."""
        from . import triggered

        dev = self.device
        if not str(getattr(dev, "downsample_method", "")).startswith(
                "camera"):
            return ""
        factor = getattr(getattr(dev, "capture", None), "downsample", 1)
        if triggered.DOWNSAMPLE_KEY in batch:
            try:
                factor = triggered.factor_of(batch[triggered.DOWNSAMPLE_KEY])
            except ValueError:
                pass
        return f'"Downsample" is {factor}x' if int(factor) > 1 else ""

    def _describe_one(self, feature: _Feature,
                      streaming: bool) -> Optional[Setting]:
        from . import triggered

        found = self._resolve(feature)
        if found is None:
            return None
        name, node = found
        setting = _node_setting(feature, name, node)
        if setting is None:
            return None
        if feature.group == "Binning":
            owner = self._downsample_owns_binning({})
            if owner:
                # Typhon's Downsample set it: greyed with the reason, and
                # left out of a preset (which keeps Downsample instead).
                return replace(setting, held=owner,
                               owned_by=triggered.DOWNSAMPLE_KEY,
                               live=not streaming or _writable(node))
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
        if feature.key == "TriggerMode" and str(setting.value) == "On":
            # The row says what On does to the picture, where it was
            # clicked: the tooltip alone said it, and the live view froze.
            source = self._resolve(self._feature("TriggerSource"))
            setting = replace(setting, warning=trigger_warning(
                str(_plain(source[1].GetValue())) if source else ""))
        return setting

    def read(self, key: str) -> Any:
        from . import triggered

        if triggered.is_capture_key(key):
            return triggered.read_capture(self.device.capture,
                                          self.capture_options(), key)
        found = self._resolve(self._feature(key))
        if found is None:
            raise SettingError(f"this camera has no {key}")
        return _plain(found[1].GetValue())

    def plan(self, keys: Iterable[str], described: Sequence[Setting],
             with_roi: bool) -> List[Optional[str]]:
        """Describe order, except THE SELECTED TRIGGER'S MODE GOES AFTER ITS
        SOURCE, EDGE AND DELAY (TRIGGER_FEATURES' order), as every other
        trigger's does in apply_triggers. The rows keep the mode under the
        selector; written in that order, applying a preset turned the
        selected trigger On while its source and edge were still the old
        ones (measured on the test fake's write log).

        DOWNSAMPLE BEFORE THE BINNING ROWS, AND SO BEFORE THE AREA. It may
        bin the camera, which changes the area's pixels: a preset's area is
        in the pixels its Downsample made, so it is written after Downsample,
        not before. And Downsample Off puts back the binning Typhon set
        (BaslerDevice._restore_binning) — written AFTER a set's own Binning
        rows, that undid them: a preset holding the user's own 2 x 2 binning,
        applied while Downsample binned the camera, ended 1 x 1 with its
        area on a quarter of the sensor, and said "applied" (review, on the
        test fake). Before them, the set's own binning is written last; and
        Reset as connected keeps the area, carried over by the restore."""
        from . import triggered

        out = super().plan(keys, described, with_roi)
        if "TriggerMode" in out:
            at = out.index("TriggerMode")
            before = [i for i, k in enumerate(out)
                      if k in TRIGGER_FEATURES and k != "TriggerMode"]
            if before and before[-1] > at:
                out.insert(before[-1], out.pop(at))
        if triggered.DOWNSAMPLE_KEY in out:
            at = out.index(triggered.DOWNSAMPLE_KEY)
            key = out.pop(at)
            binning = {s.key for s in described if s.group == "Binning"}
            spot = next((i for i, k in enumerate(out)
                         if k is None or k in binning), None)
            out.insert(at if spot is None or spot > at else spot, key)
        return out

    # -- triggers ------------------------------------------------------------
    def _selector(self) -> Tuple[Any, str, List[str]]:
        """(TriggerSelector node, the entry selected, every entry) — (None,
        "", []) on a model with no readable selector."""
        selector = self.device._node("TriggerSelector")
        if selector is None or not _readable(selector):
            return None, "", []
        was = ""
        try:
            was = str(_plain(selector.GetValue()))
            return selector, was, [str(s) for s in selector.GetSymbolics()]
        except Exception:                                   # noqa: BLE001
            return selector, was, []

    def unreadable_triggers(self) -> List[str]:
        """The triggers trigger_settings cannot reach now, by selector entry:
        every one but the selected, while the selector cannot be written
        (locked by the stream on a model that locks it — the emulator does
        not, measured). [] when it reaches them all."""
        mode = self.device._node("TriggerMode")
        if mode is None or not _readable(mode):
            return []
        selector, was, entries = self._selector()
        if len(entries) < 2 or _writable(selector):
            return []
        return [entry for entry in entries if entry != was]

    def _each_trigger(self, visit: Callable[[str, Any, Any], None]) -> None:
        """Call visit(selector entry, TriggerMode node, TriggerSource node)
        with each of the camera's triggers selected in turn, then select
        the one that was selected before.

        EVERY TRIGGER, NOT THE SELECTED ONE. TriggerMode and its source are
        the SELECTED trigger's (SFNC), and the rows show that one: measured
        on the emulator, FrameBurstStart left On while the selector showed
        FrameStart was invisible to the rows, to a preset and to "Reset
        Trigger" — on a real camera a burst trigger left On stops the
        frames. Only the selector is written here, and put back."""
        mode = self.device._node("TriggerMode")
        if mode is None or not _readable(mode):
            return
        # UNDER THE DEVICE'S TRIGGER LOCK: a run armed to fire software
        # triggers fires with the same lock held, so the selector is never
        # moved away from the armed trigger at the moment one is fired — a
        # trigger fired with the selector on another entry is lost with no
        # error (measured on the emulator: 0 frames).
        with _trigger_lock(self.device):
            source = self.device._node("TriggerSource")
            selector, was, entries = self._selector()
            if len(entries) < 2 or not _writable(selector):
                visit(was, mode, source)
                return
            try:
                for entry in entries:
                    try:
                        if entry != str(_plain(selector.GetValue())):
                            selector.SetValue(entry)
                    except Exception:                       # noqa: BLE001
                        continue        # an entry this model cannot select
                    visit(entry, mode, source)
            finally:
                try:
                    if str(_plain(selector.GetValue())) != was:
                        selector.SetValue(was)
                except Exception:                           # noqa: BLE001
                    pass

    def triggers_on(self) -> List[Trigger]:
        found: List[Trigger] = []

        def visit(entry: str, mode: Any, source: Any) -> None:
            try:
                if str(_plain(mode.GetValue())) != "On":
                    return
                said = (str(_plain(source.GetValue()))
                        if source is not None and _readable(source) else "")
            except Exception:                               # noqa: BLE001
                return
            found.append(Trigger(entry, said))
        self._each_trigger(visit)
        return found

    def restore_triggers(self, values: Mapping[str, Any]) -> List[str]:
        """EVERY TRIGGER BACK AS CONNECTED, NOT ONLY ITS MODE. A Basler
        holds a source, an edge and a delay per TriggerSelector entry as
        well as a mode (measured on the emulator: each changed alone), and
        "Reset as connected" put back the modes only — a FrameBurstStart
        trigger moved to Line1 on the falling edge stayed there.

        Written by apply_triggers: each trigger selected in turn, its
        source, edge and delay before its mode, a value it already has not
        written again, and the selector put back where it was. Read first
        (trigger_settings), so what CHANGED is said: "the FrameBurstStart
        trigger Off again (its source Software and edge RisingEdge too)"."""
        if not values:
            return []
        try:
            before = self.trigger_settings(selected=True)
        except Exception:                                   # noqa: BLE001
            before = {}
        words = {"TriggerMode": "mode", "TriggerSource": "source",
                 "TriggerActivation": "edge", "TriggerDelay": "delay"}
        changed: Dict[str, Dict[str, Any]] = {}
        refused: List[str] = []
        for change in self.apply_triggers(values):
            split = split_trigger_key(change.key)
            if split is None:
                continue
            base, entry = split
            if not change.ok:
                refused.append(f"the {entry} trigger's {words[base]} NOT put "
                               f"back to {show(change.asked)} — "
                               f"{change.note}")
            elif change.key not in before or not same(before[change.key],
                                                      change.value):
                changed.setdefault(entry, {})[base] = change.value
        done: List[str] = []
        for entry, now in changed.items():
            parts = []
            for base in ("TriggerSource", "TriggerActivation",
                         "TriggerDelay"):
                word = words[base]
                if base in now:
                    unit = " µs" if base == "TriggerDelay" else ""
                    parts.append(f"{word} {show(now[base])}{unit}")
            said = (", ".join(parts[:-1]) + " and " + parts[-1]
                    if len(parts) > 1 else "".join(parts))
            if "TriggerMode" in now:
                line = f"the {entry} trigger {show(now['TriggerMode'])} again"
                if said:
                    line += f" (its {said} too)"
            else:
                line = f"the {entry} trigger's {said} again"
            done.append(line)
        return done + refused

    def _selected_trigger(self) -> str:
        selector = self.device._node("TriggerSelector")
        if selector is None or not _readable(selector):
            return ""
        try:
            return str(_plain(selector.GetValue()))
        except Exception:                                   # noqa: BLE001
            return ""

    def trigger_settings(self, selected: bool = False) -> Dict[str, Any]:
        """EVERY TRIGGER IN A PRESET, NOT THE SELECTED ONE ONLY. A snapshot
        holds the rows — the selected trigger's mode, source, edge and
        delay — so a preset kept one trigger of nine (the emulator's): a
        FrameBurstStart trigger On from Line1, saved while the selector
        showed FrameStart, was lost. These are the others', by trigger_key;
        only the selector is written to read them, and put back."""
        current = self._selected_trigger()
        streaming = self._streaming()
        out: Dict[str, Any] = {}

        def visit(entry: str, _mode: Any, _source: Any) -> None:
            if not entry or (entry == current and not selected):
                return
            for base in TRIGGER_FEATURES:
                setting = self._describe_one(self._feature(base), streaming)
                if setting is not None and setting.savable \
                        and setting.value is not None:
                    out[trigger_key(entry, base)] = setting.value
        self._each_trigger(visit)
        return out

    def apply_triggers(self, values: Mapping[str, Any]) -> List[Change]:
        """Each trigger selected in turn (in the camera's order), its
        settings written — source, edge and delay before the mode — and
        the selector put back where it was (_each_trigger). A value the
        trigger already has is not written again. A trigger this camera
        does not have is refused by name, and one it has but cannot select
        now (the selector locked by the stream) says that instead."""
        wanted: Dict[str, Dict[str, Tuple[str, Any]]] = {}
        done: List[Change] = []
        for key, asked in values.items():
            split = split_trigger_key(key)
            if split is None:
                done.append(Change(key, asked, None, ok=False,
                                   note="this camera has no such setting"))
                continue
            base, entry = split
            wanted.setdefault(entry, {})[base] = (key, asked)
        streaming = self._streaming()

        def visit(entry: str, _mode: Any, _source: Any) -> None:
            asks = wanted.pop(entry, None) if entry else None
            for base in TRIGGER_FEATURES:
                if asks and base in asks:
                    key, asked = asks[base]
                    done.append(self._set_trigger(key, base, asked,
                                                  streaming))
        self._each_trigger(visit)
        # Not visited: a trigger this camera does not have — or one it has
        # that could not be selected now. Said apart: "has no FrameBurstStart
        # trigger" was the answer for both, with the selector locked by the
        # stream (on the test fake) and the trigger right there.
        mode = self.device._node("TriggerMode")
        selector, _was, entries = (self._selector() if mode is not None
                                   and _readable(mode) else (None, "", []))
        for entry, asks in wanted.items():
            if entry not in entries:
                why = f"this camera has no {entry} trigger"
            elif not _writable(selector):
                why = (f"the {entry} trigger cannot be selected now — the "
                       + ("camera locks the trigger selector while it "
                          "streams" if streaming else
                          "camera does not allow changing the trigger "
                          "selector now"))
            else:
                why = (f"the {entry} trigger cannot be selected now — the "
                       f"camera refused to select it")
            for key, asked in asks.values():
                done.append(Change(key, asked, None, ok=False, note=why))
        return done

    def _set_trigger(self, key: str, base: str, asked: Any,
                     streaming: bool) -> Change:
        """One setting of the trigger now selected, reported under its
        trigger_key."""
        setting = self._describe_one(self._feature(base), streaming)
        if setting is None:
            return Change(key, asked, None, ok=False,
                          note="this camera has no such setting")
        if setting.read_only:
            return Change(key, asked, setting.value, ok=False,
                          note="read-only on this camera")
        if _unchanged(setting, asked):
            return Change(key, asked, setting.value)
        try:
            change = self.set(base, asked, batch={}, setting=setting)
        except NeedsStop:
            raise
        except SettingError as exc:
            return Change(key, asked, setting.value, ok=False, note=str(exc))
        return replace(change, key=key)

    def held_by(self, setting: Setting, batch: Mapping[str, Any]) -> str:
        from . import triggered

        if triggered.is_capture_key(setting.key):
            return ""
        feature = self._feature(setting.key)
        if feature.group == "Binning":
            owner = self._downsample_owns_binning(batch)
            if owner:
                return owner
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
        # Said by the label on the row that owns it: the node name
        # ("ExposureAuto is Continuous") under a row called "Auto exposure"
        # sent the user looking for a setting the window does not show.
        owner = f'"{self._feature(other).label}"'
        if isinstance(needed, bool):
            met = str(state).strip().lower() in _TRUE if not isinstance(
                state, bool) else state
            return "" if met == needed else f"{owner} is off"
        return "" if str(state) == str(needed) else f"{owner} is {state}"

    def write(self, setting: Setting, value: Any,
              batch: Mapping[str, Any]) -> None:
        from . import triggered

        if triggered.is_capture_key(setting.key):
            if setting.key == triggered.DOWNSAMPLE_KEY:
                # The camera's binning (or Typhon's averaging) — raises
                # NeedsStop before writing while the stream locks binning.
                self.device.apply_downsample(triggered.factor_of(value))
            else:
                triggered.write_capture(self.device.capture, setting.key,
                                        value)
            return
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

    # -- the capture set-up: arming the camera's own trigger -------------
    #: The nodes a line's debounce or input filter has, by family: ace
    #: Classic/U/L LineDebouncerTime (ace GigE ...Abs); ace 2, boost and
    #: dart R the input filter instead (vendor docs). First one found wins.
    DEBOUNCE_NODES: Tuple[Tuple[str, str], ...] = (
        ("LineDebouncerTime", "Debounce"),
        ("LineDebouncerTimeAbs", "Debounce"),
        ("BslInputFilterTime", "Input filter"))

    def _entries(self, name: str) -> List[str]:
        node = self.device._node(name)
        if node is None:
            return []
        try:
            return [str(s) for s in node.GetSymbolics()]
        except Exception:                                   # noqa: BLE001
            return []

    def _on_entry(self, entry: str, read: Callable[[], Any]) -> Any:
        """read() with the trigger selector on `entry` (when the camera has
        it), the selector put back — under the trigger lock."""
        with _trigger_lock(self.device):
            selector, was, entries = self._selector()
            moved = (selector is not None and entry in entries
                     and entry != was and _writable(selector))
            try:
                if moved:
                    selector.SetValue(entry)
                return read()
            finally:
                if moved:
                    try:
                        selector.SetValue(was)
                    except Exception:                       # noqa: BLE001
                        pass

    def capture_options(self) -> Any:
        """What this camera offers the capture set-up: its hardware trigger
        sources and the edge and delay of its FrameStart trigger, read ONCE
        per connection (they are the camera's, and every view describes the
        camera often; reading them moves the selector), and the debounce of
        its lines; whether Downsample can change while it streams."""
        dev = self.device
        cached = getattr(dev, "_capture_options", None)
        if cached is None:
            cached = self._read_capture_options()
            try:
                dev._capture_options = cached
            except Exception:                               # noqa: BLE001
                pass
        live = not (self._streaming() and self._binning_locked())
        method = ("by the camera (binning, " + dev.binning_mode() + ")"
                  if callable(getattr(dev, "can_bin", None))
                  and dev.can_bin(2) else "by Typhon before saving")
        return replace(cached, downsample_live=live, downsample_help=(
            f"Saves smaller pictures: each 2 x 2, 3 x 3 or 4 x 4 block of "
            f"pixels becomes one, AVERAGED — on this camera {method} where "
            f"it can, else by Typhon before saving (a camera that accepts "
            f"binning without its picture getting smaller is checked for). "
            f"Off (the default) never changes the data. Kept in presets; "
            f"fixed while capturing."
            + (" Changing it restarts the live view." if not live else "")))

    def _binning_locked(self) -> bool:
        node = self.device._node("BinningHorizontal")
        dev = self.device
        if node is None or _writable(node):
            return False
        can = getattr(dev, "can_bin", None)
        return bool((callable(can) and can(2))
                    or getattr(dev, "_binning_before", None) is not None)

    def _read_capture_options(self) -> Any:
        from . import triggered

        dev = self.device
        hardware: Tuple[str, ...] = ()
        edges: Tuple[str, ...] = ()
        edge_now, delay, delay_now = "", None, 0.0
        if dev._node("TriggerMode") is not None and \
                dev._node("TriggerSource") is not None:
            def read() -> Tuple[Any, ...]:
                sources = self._entries("TriggerSource")
                activations = tuple(self._entries("TriggerActivation"))
                now = ""
                if activations:
                    now = str(dev._value("TriggerActivation", "") or "")
                span, at = None, 0.0
                name = dev._first_node("TriggerDelay", "TriggerDelayAbs")
                if name is not None:
                    node = dev._node(name)
                    low, high = _ask(node, "GetMin"), _ask(node, "GetMax")
                    if low is not None and high is not None:
                        span = (float(low), float(high))
                        at = float(dev._value(name, 0.0) or 0.0)
                return sources, activations, now, span, at
            sources, edges, edge_now, delay, delay_now = self._on_entry(
                "FrameStart", read)
            hardware = triggered.hardware_sources(sources)
        label, debounce, debounce_now = "Debounce", None, 0.0
        if hardware:
            for name, said in self.DEBOUNCE_NODES:
                node = dev._node(name)
                if node is None:
                    continue
                low, high = _ask(node, "GetMin"), _ask(node, "GetMax")
                if low is None or high is None:
                    continue
                label, debounce = said, (float(low), float(high))
                debounce_now = float(dev._value(name, 0.0) or 0.0)
                break
        return triggered.CaptureOptions(
            hardware=hardware, edges=edges, edge_now=edge_now, delay=delay,
            delay_now=delay_now, debounce_label=label, debounce=debounce,
            debounce_now=debounce_now)

    def _burst_count_node(self) -> Any:
        return self.device._node("AcquisitionBurstFrameCount")

    def trigger_problem(self, setup: Any) -> str:
        """Why the camera cannot be armed for `setup`, or "": asked at
        Start before anything stops, so a refusal leaves the live view as
        it was. A HARDWARE source is used only where Typhon can confirm the
        camera has it: listed as a trigger source of the trigger it arms,
        and — for an input line — among the camera's lines, not an output
        that cannot be made an input."""
        from . import triggered

        if not setup.triggered:
            return ""
        if setup.snapshot:
            if not setup.typhon_fires:
                return (f"Snapshot takes its trigger from Software (Capture "
                        f"now) or the Timer; {setup.source} is the camera's "
                        f"own input, which only an exact run waits for — "
                        f"choose Triggered: exact, or Software / Timer")
            return ""
        dev = self.device
        if dev._node("TriggerMode") is None or \
                dev._node("TriggerSource") is None:
            return ("this camera has no trigger Typhon can arm (no "
                    "TriggerMode / TriggerSource node)")
        burst = not setup.typhon_fires and setup.per_trigger > 1
        entry = "FrameBurstStart" if burst else "FrameStart"
        selectors = self._entries("TriggerSelector")
        if burst and (entry not in selectors
                      or self._burst_count_node() is None):
            return (f"this camera cannot take {setup.per_trigger} frames per "
                    f"hardware trigger (it has no burst trigger, "
                    f"AcquisitionBurstFrameCount) — set Frames per trigger "
                    f"to 1, or capture with Software or the Timer")
        if burst:
            high = _ask(self._burst_count_node(), "GetMax")
            if high is not None and setup.per_trigger > int(high):
                return (f"this camera takes at most {int(high)} frames per "
                        f"burst trigger, not {setup.per_trigger}")
        if selectors and entry not in selectors:
            return f"this camera has no {entry} trigger to arm"
        sources = self._on_entry(entry, lambda: self._entries(
            "TriggerSource"))
        want = triggered.SOFTWARE if setup.typhon_fires else setup.source
        if want not in sources:
            if setup.typhon_fires:
                return "this camera has no Software trigger source"
            listed = triggered.hardware_sources(sources)
            return (f"this camera does not list {want} as a trigger source "
                    f"— it lists {', '.join(listed) or 'no hardware input'}; "
                    f"choose one of those, or Software / Timer")
        if setup.typhon_fires:
            node = dev._node("TriggerSoftware")
            if node is None and not callable(getattr(
                    dev._cam, "ExecuteSoftwareTrigger", None)):
                return ("this camera has no software trigger command "
                        "(TriggerSoftware)")
            return ""
        if want.startswith("Line"):
            return self._line_problem(want)
        return ""

    def _line_problem(self, line: str) -> str:
        dev = self.device
        lines = self._entries("LineSelector")
        if dev._node("LineSelector") is None:
            return ""                     # one line, no selector: as listed
        if line not in lines:
            return (f"Typhon cannot confirm {line} exists on this camera — "
                    f"its lines are {', '.join(lines) or 'not listed'}")
        node = dev._node("LineSelector")
        was = str(_plain(node.GetValue()))
        try:
            if was != line:
                node.SetValue(line)
            mode = dev._node("LineMode")
            if mode is None:
                return ""
            now = str(_plain(mode.GetValue()))
            if now != "Input" and "Input" not in self._entries("LineMode"):
                return (f"{line} is an output on this camera (LineMode "
                        f"{now}) and cannot be made an input")
            if now != "Input" and not _writable(mode):
                return (f"{line} is set as {now} and the camera does not let "
                        f"Typhon make it an input now")
            return ""
        except Exception as exc:                            # noqa: BLE001
            return f"Typhon cannot read {line} on this camera: {_why(exc)}"
        finally:
            try:
                if str(_plain(node.GetValue())) != was:
                    node.SetValue(was)
            except Exception:                               # noqa: BLE001
                pass

    def arm(self, setup: Any) -> Dict[str, Any]:
        """ARM the camera for an exact run: the trigger it waits for On
        from the run's source, every other trigger Off, the selector left
        on the armed one. Returns everything changed, for disarm — which
        Stop and a failed Start call. A camera left armed by a crash is put
        right by the next Connect: pylon's AcquireContinuousConfiguration
        turns every trigger Off (measured on the emulator).

        Software / Timer: FrameStart On from Software (Typhon fires one per
        frame). A hardware source: FrameStart On from it — or, for more than
        one frame per trigger, FrameBurstStart On from it with
        AcquisitionBurstFrameCount the frames, FrameStart Off. Its edge and
        delay as the set-up has them (where the camera has the nodes); for
        an input line, LineMode Input and its debounce, put back after."""
        from . import triggered

        dev = self.device
        hardware = not setup.typhon_fires
        burst = hardware and setup.per_trigger > 1
        entry = "FrameBurstStart" if burst else "FrameStart"
        value = setup.source if hardware else triggered.SOFTWARE
        saved: Dict[str, Any] = {"entry": entry, "source": value,
                                 "said": []}
        with _trigger_lock(dev):
            saved["selector"] = self._selected_trigger()
            saved["triggers"] = self.trigger_settings(selected=True)
            if not saved["triggers"]:
                # One trigger, no selector: its plain rows.
                saved["plain"] = {
                    key: dev._value(name, None) for key, name in (
                        ("TriggerMode", "TriggerMode"),
                        ("TriggerSource", "TriggerSource"),
                        ("TriggerActivation", "TriggerActivation"),
                        ("TriggerDelay", dev._first_node(
                            "TriggerDelay", "TriggerDelayAbs") or
                         "TriggerDelay"))
                    if dev._node(name) is not None}
            try:
                self._arm_writes(setup, saved, entry, value, hardware, burst)
            except Exception as exc:                        # noqa: BLE001
                self.disarm(saved)
                raise SettingError(f"the camera refused to be armed: "
                                   f"{_why(exc)}") from exc
            dev._armed_entry = entry if self._selector()[0] is not None \
                else ""
        return saved

    def _arm_writes(self, setup: Any, saved: Dict[str, Any], entry: str,
                    value: str, hardware: bool, burst: bool) -> None:
        dev = self.device
        selector, _was, entries = self._selector()
        if selector is not None and len(entries) >= 2:
            for other in entries:
                if other == entry:
                    continue
                try:
                    selector.SetValue(other)
                except Exception:                           # noqa: BLE001
                    continue
                mode = dev._node("TriggerMode")
                if mode is not None and str(_plain(mode.GetValue())) == "On":
                    mode.SetValue("Off")
                    saved["said"].append(f"the {other} trigger Off for the "
                                         f"run")
            selector.SetValue(entry)
        mode = dev._node("TriggerMode")
        if mode is not None and str(_plain(mode.GetValue())) == "On":
            # Off while its source changes: never On from the old source.
            mode.SetValue("Off")
        dev._node("TriggerSource").SetValue(value)
        if hardware:
            if setup.edge and setup.edge in self._entries("TriggerActivation"):
                dev._node("TriggerActivation").SetValue(setup.edge)
            name = dev._first_node("TriggerDelay", "TriggerDelayAbs")
            if setup.delay_us is not None and name is not None:
                node = dev._node(name)
                low, high = _ask(node, "GetMin"), _ask(node, "GetMax")
                delay = float(setup.delay_us)
                if low is not None:
                    delay = max(float(low), delay)
                if high is not None:
                    delay = min(float(high), delay)
                node.SetValue(delay)
        if burst:
            count = self._burst_count_node()
            saved["burst_count"] = int(_plain(count.GetValue()))
            count.SetValue(int(setup.per_trigger))
        if hardware and value.startswith("Line"):
            saved["line"] = self._arm_line(setup, value, saved["said"])
        if hardware:
            saved["counter"] = self._start_counter(setup)
        dev._node("TriggerMode").SetValue("On")

    def _arm_line(self, setup: Any, line: str,
                  said: List[str]) -> Optional[Dict[str, Any]]:
        """The input line ready for the run: LineMode Input, its debounce
        (or input filter) as the set-up has it. What was there, for
        disarm."""
        dev = self.device
        node = dev._node("LineSelector")
        if node is None:
            return None
        out: Dict[str, Any] = {"selector": str(_plain(node.GetValue())),
                               "line": line}
        node.SetValue(line)
        try:
            mode = dev._node("LineMode")
            if mode is not None:
                now = str(_plain(mode.GetValue()))
                if now != "Input":
                    out["mode"] = now
                    mode.SetValue("Input")
                    said.append(f"{line} made an input for the run (it was "
                                f"{now})")
            if setup.debounce_us is not None:
                for name, _label in self.DEBOUNCE_NODES:
                    found = dev._node(name)
                    if found is None:
                        continue
                    out["debounce"] = (name, float(_plain(found.GetValue())))
                    low, high = _ask(found, "GetMin"), _ask(found, "GetMax")
                    value = float(setup.debounce_us)
                    if low is not None:
                        value = max(float(low), value)
                    if high is not None:
                        value = min(float(high), value)
                    found.SetValue(value)
                    break
        finally:
            node.SetValue(out["selector"])
        return out

    def _start_counter(self, setup: Any) -> Optional[Dict[str, Any]]:
        """Count the hardware input's edges in the camera (Counter1), so
        Stop can say how many it IGNORED while busy — the camera says
        nothing of those. Only where the camera's counter can count that
        input (ace 2 and boost list lines as CounterEventSource; an ace
        USB's Counter1 counts images only). UNVERIFIED ON HARDWARE: none
        was on the machine this was written on; a count below the
        triggers seen is said as untrusted, never as missed triggers."""
        dev = self.device
        names = ("CounterSelector", "CounterEventSource", "CounterValue",
                 "CounterReset")
        if any(dev._node(n) is None for n in names):
            return None
        selector = dev._node("CounterSelector")
        if "Counter1" not in self._entries("CounterSelector"):
            return None
        out: Dict[str, Any] = {"selector": str(_plain(selector.GetValue()))}
        try:
            selector.SetValue("Counter1")
            if setup.source not in self._entries("CounterEventSource"):
                return None
            source = dev._node("CounterEventSource")
            out["event_source"] = str(_plain(source.GetValue()))
            source.SetValue(setup.source)
            activation = dev._node("CounterEventActivation")
            if activation is not None and setup.edge and setup.edge in \
                    self._entries("CounterEventActivation"):
                out["activation"] = str(_plain(activation.GetValue()))
                activation.SetValue(setup.edge)
            reset_source = dev._node("CounterResetSource")
            if reset_source is not None and "Software" in self._entries(
                    "CounterResetSource"):
                out["reset_source"] = str(_plain(reset_source.GetValue()))
                reset_source.SetValue("Software")
            dev._node("CounterReset").Execute()
            out["base"] = int(_plain(dev._node("CounterValue").GetValue()))
            return out
        except Exception:                                   # noqa: BLE001
            return None
        finally:
            try:
                selector.SetValue(out["selector"])
            except Exception:                               # noqa: BLE001
                pass

    def _read_counter(self, counter: Dict[str, Any]) -> int:
        """Counter1's value now, its selector put back. Raises."""
        dev = self.device
        selector = dev._node("CounterSelector")
        try:
            selector.SetValue("Counter1")
            return int(_plain(dev._node("CounterValue").GetValue()))
        finally:
            try:
                selector.SetValue(counter["selector"])
            except Exception:                               # noqa: BLE001
                pass

    def count_from_now(self, armed: Any) -> None:
        """THE COUNT STARTS HERE: called once the stream runs and the
        camera says it is ready for a trigger. Edges before that (between
        arming and the stream, while the camera's record was read, before
        it was ready) triggered nothing and were counted — every run with
        a periodic signal said "missed" for them (review: 51 edges for 50
        triggers the camera kept up with; 9 with a 150 ms record read).

        The count at arming is kept too ("armed"): the camera began taking
        triggers somewhere between the two reads, so an edge between them
        may have triggered a frame — count_edges never calls it missed."""
        counter = (armed or {}).get("counter") if isinstance(armed, dict) \
            else None
        if not counter:
            return
        try:
            value = self._read_counter(counter)
        except Exception:                                   # noqa: BLE001
            return
        counter["armed"] = int(counter.get("base", 0))
        counter["base"] = value

    def count_to_now(self, armed: Any) -> None:
        """THE COUNT ENDS HERE, before the stream stops: the count is read,
        then the armed trigger takes Software as its source, so the camera
        accepts no more edges while the stream winds down — edges counted
        after StopGrabbing triggered nothing, and were "missed" too (the
        same review). Disarm puts every trigger back after. Whether the
        camera took Software is kept ("closed"): one that refuses it while
        grabbing goes on taking edges until the stream stops."""
        counter = (armed or {}).get("counter") if isinstance(armed, dict) \
            else None
        if not counter:
            return
        dev = self.device
        try:
            counter["final"] = self._read_counter(counter)
        except Exception:                                   # noqa: BLE001
            return
        entry = str(armed.get("entry") or "")
        closed = False
        with _trigger_lock(dev):
            selector, was, entries = self._selector()
            try:
                if selector is not None and entry in entries and was != entry:
                    selector.SetValue(entry)
                if "Software" in self._entries("TriggerSource"):
                    dev._node("TriggerSource").SetValue("Software")
                    closed = True
            except Exception:                               # noqa: BLE001
                pass
            finally:
                if selector is not None and was and was != entry:
                    try:
                        selector.SetValue(was)
                    except Exception:                       # noqa: BLE001
                        pass
        counter["closed"] = closed

    def count_edges(self, armed: Any, seen: Optional[int] = None
                    ) -> Tuple[Optional[int], str]:
        """The edges the camera counted while it was taking triggers, read
        after the stream stopped; `seen` the triggers Typhon saw.

        BRACKETED, NEVER GUESSED. The count is sure between the read after
        the camera was ready (count_from_now) and the read before the
        stream stopped (count_to_now): an edge outside them may or may not
        have triggered a frame, and is never called missed. Typhon's
        triggers are checked against the most the counter can have counted
        while the camera took edges — from arming to now — so a count that
        cannot cover them is untrusted, never "missed"."""
        counter = (armed or {}).get("counter") if isinstance(armed, dict) \
            else None
        if not counter:
            return None, ("the camera has no edge counter Typhon can set to "
                          "this input")
        try:
            now = self._read_counter(counter)
        except Exception as exc:                            # noqa: BLE001
            if "final" not in counter:
                return None, f"the camera's edge counter could not be " \
                             f"read: {_why(exc)}"
            now = int(counter["final"])
        final = int(counter.get("final", now))
        edges = max(0, final - int(counter.get("base", 0)))
        most = max(0, now - int(counter.get("armed", counter.get("base", 0))))
        if seen is not None and int(seen) > most:
            return None, (f"the camera's edge counter read {most}, fewer than "
                          f"the {int(seen)} triggers seen — its count is not "
                          f"trusted on this camera")
        return edges, ""

    def disarm(self, saved: Any) -> List[str]:
        """Put back everything arm changed: every trigger's mode, source,
        edge and delay (restore_triggers), the burst count, the line and the
        counter, then the selector where it was. What changed, in words."""
        if not isinstance(saved, dict):
            return []
        dev = self.device
        said: List[str] = []
        with _trigger_lock(dev):
            dev._armed_entry = ""
            counter = saved.get("counter")
            if counter:
                self._put_back(("CounterSelector", "Counter1"), (
                    ("CounterEventSource", counter.get("event_source")),
                    ("CounterEventActivation", counter.get("activation")),
                    ("CounterResetSource", counter.get("reset_source"))),
                    counter.get("selector"))
            line = saved.get("line")
            if line:
                writes = [("LineMode", line.get("mode"))]
                if line.get("debounce"):
                    writes.append(tuple(line["debounce"]))
                self._put_back(("LineSelector", line["line"]), writes,
                               line.get("selector"))
            if "burst_count" in saved:
                count = self._burst_count_node()
                try:
                    count.SetValue(int(saved["burst_count"]))
                except Exception:                           # noqa: BLE001
                    said.append("the burst frame count NOT put back")
            if saved.get("triggers"):
                try:
                    said.extend(self.restore_triggers(saved["triggers"]))
                except Exception as exc:                    # noqa: BLE001
                    said.append(f"the triggers NOT put back — {_why(exc)}")
            elif saved.get("plain"):
                plain = saved["plain"]
                for key in ("TriggerSource", "TriggerActivation",
                            "TriggerDelay", "TriggerMode"):
                    if plain.get(key) is None:
                        continue
                    name = (dev._first_node("TriggerDelay", "TriggerDelayAbs")
                            if key == "TriggerDelay" else key)
                    try:
                        dev._node(name).SetValue(plain[key])
                    except Exception:                       # noqa: BLE001
                        said.append(f"{key} NOT put back")
            selector = dev._node("TriggerSelector")
            was = saved.get("selector")
            if selector is not None and was:
                try:
                    if str(_plain(selector.GetValue())) != was:
                        selector.SetValue(was)
                except Exception:                           # noqa: BLE001
                    pass
        return said

    def _put_back(self, select: Tuple[str, str],
                  writes: Sequence[Tuple[str, Any]],
                  back_to: Any) -> None:
        dev = self.device
        name, entry = select
        node = dev._node(name)
        if node is None:
            return
        try:
            node.SetValue(entry)
            for key, value in writes:
                if value is None or dev._node(key) is None:
                    continue
                try:
                    dev._node(key).SetValue(value)
                except Exception:                           # noqa: BLE001
                    pass
        except Exception:                                   # noqa: BLE001
            pass
        finally:
            if back_to:
                try:
                    node.SetValue(back_to)
                except Exception:                           # noqa: BLE001
                    pass


class _NoLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> None:
        return None


def _trigger_lock(device: Any) -> Any:
    """The device's trigger lock (cameras.BaslerDevice.trigger_lock), or a
    lock that holds nothing for a device without one (a test fake)."""
    lock = getattr(device, "trigger_lock", None)
    return lock if lock is not None else _NoLock()


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
#: The event picture as this app draws it (live view and PNGs, never the
#: .raw): its window, which events it shows, and its colours.
G_VIEW = "Display"
G_STATUS = "Status"
G_CAMERA = "Camera"

#: The activity filter's four thresholds, in the binding's own field names.
ACTIVITY_FIELDS = ("lower_bound_start", "lower_bound_stop",
                   "upper_bound_start", "upper_bound_stop")

#: The longest picture window offered: one picture a second. The window is
#: this app's display setting, so this is the one bound not read from the
#: camera.
MAX_WINDOW_MS = 1000.0

#: An event picture's display settings, by the device attribute each one
#: is. The app's own settings, not the camera's: no facility, no USB read.
DISPLAY_ATTRIBUTES: Dict[str, str] = {
    "window_ms": "accumulate_ms",
    "display.events": "display_events",
    "display.palette": "display_palette",
}


def display_settings(device: Any) -> List[Setting]:
    """An event camera's display settings, as `device` holds them now.

    WHAT THE USER SEES, NOT WHAT IS RECORDED. The window, the events shown
    and the colours shape the live picture and the PNGs saved from it; the
    .raw has every event whatever they are, and the raw view draws it in
    the standard grey. They are kept in presets and in a run's camera
    record like any other setting, so a run says how its PNGs were drawn.
    """
    shown = str(getattr(device, "display_events", SHOW_BOTH) or SHOW_BOTH)
    palette = str(getattr(device, "display_palette", DEFAULT_PALETTE)
                  or DEFAULT_PALETTE)
    return [
        Setting("window_ms", "Picture window", G_VIEW, FLOAT,
                float(getattr(device, "accumulate_ms", 20.0)),
                minimum=MIN_ACCUMULATE_MS, maximum=MAX_WINDOW_MS, unit="ms",
                help="How long each picture collects events (1000 / "
                     "pictures a second). The live view and the PNGs only "
                     "— the .raw has every event."),
        Setting("display.events", "Events shown", G_VIEW, CHOICE, shown,
                choices=EVENT_SHOWS,
                help="Leave out one polarity: ON is brighter, OFF darker. "
                     "The live view and the PNGs only — the .raw keeps "
                     "both."),
        Setting("display.palette", "Colours", G_VIEW, CHOICE, palette,
                choices=tuple(EVENT_PALETTES),
                help="Grey: mid-grey ground, white ON, black OFF (the "
                     "standard picture). Dark: the events on black. "
                     "Colour: light ON and blue OFF on dark blue (PNGs are "
                     "then saved in colour). The .raw is unaffected."),
    ]


def read_display(device: Any, key: str) -> Any:
    value = getattr(device, DISPLAY_ATTRIBUTES[key])
    return float(value) if key == "window_ms" else str(value)


def write_display(device: Any, key: str, value: Any) -> None:
    """One display setting, already coerced (a choice is one of its
    entries); the grab thread's next window is drawn with it."""
    if key == "window_ms":
        device.accumulate_ms = max(MIN_ACCUMULATE_MS, float(value))
    else:
        setattr(device, DISPLAY_ATTRIBUTES[key], str(value))


_BIAS_HELP = {
    "bias_diff_on": "Contrast threshold for ON (brighter) events: higher "
                    "means fewer, stronger events.",
    "bias_diff_off": "Contrast threshold for OFF (darker) events: higher "
                     "means fewer, stronger events.",
    "bias_fo": "Low-pass filter: lower filters out fast flicker and noise.",
    "bias_hpf": "High-pass filter: higher removes slow changes.",
    "bias_refr": "Refractory period: how long a pixel rests after an event "
                 "before it can fire again. Higher shortens the rest, so "
                 "more events; lower gives fewer events each time an edge "
                 "crosses a pixel.",
    "bias_diff": "Reference level the ON and OFF thresholds are set "
                 "against.",
}

# WHAT THE FILTERS DELETE, said where they are switched on. Checked against
# the OpenEB 5.2 source (tests/test_camera_settings.py reads it where it is
# on the machine): the IMX636 driver (Gen41Erc) counts the rate over a
# 200 µs reference period with time-based dropping only (row and column
# dropping off); its trail filter takes 1000-100000 µs and keeps
# roundf(threshold / 1000) whole milliseconds. What STC and TRAIL keep, and
# what a burst is, is I_EventTrailFilterModule::Type's own description; the
# name STC (spatio-temporal contrast) is Prophesee's documentation's — the
# OpenEB source never spells it out.
ERC_HELP = (
    "Caps the event rate so a busy scene cannot swamp the link — but not "
    "evenly: on the EVK4's IMX636 it drops events by time slot (OpenEB's "
    "driver counts the rate in 200 µs slots and drops by time only, not by "
    "row or column), so a track gets gaps in time, and signal quality "
    "degrades once it acts. Prefer a smaller area (ROI) and the filters; "
    "keep this as a safety cap.")
TRAIL_HELP = (
    "A burst is a run of events of the same polarity (all ON, brighter, or "
    "all OFF, darker) at one pixel, closer together than the threshold "
    "below. STC is Prophesee's spatio-temporal contrast filter. "
    "TRAIL keeps the first event of each burst and removes the trail a "
    "moving edge leaves behind it. Both STC modes drop the first event of "
    "each burst and every lone event (STC_CUT_TRAIL keeps only the second "
    "event of a burst, STC_KEEP_TRAIL the second and later), so STC can "
    "delete a small, fast particle that fires a pixel once. TRAIL for fast "
    "particles; test STC on a real recording first.")
TRAIL_THRESHOLD_HELP = (
    "A burst is events of one polarity closer together than this. On the "
    "EVK4's IMX636 it is 1-100 ms (1,000-100,000 µs), rounded to the "
    "nearest whole millisecond.")


def trail_threshold_setting(value: Any, low: Any, high: Any) -> Setting:
    """The trail filter's threshold row.

    IN WHOLE MILLISECONDS, AS THE SENSOR KEEPS IT. Every OpenEB trail
    filter takes 1000-100000 µs, keeps the nearest whole ms and raises
    outside that (cameras.trail_threshold_kept); the slider stepped by
    1 µs, so it offered 99,001 positions of which 99 exist, and a drag said
    values the camera never held. The value stays in µs — what the HAL and
    a preset hold — stepped by 1000 and SHOWN in ms (Setting.shown_unit).
    A range that is not in whole ms (no OpenEB sensor reports one) keeps a
    1 µs step, shown in µs."""
    whole_ms = all(v is not None and float(v) % 1000 == 0
                   for v in (low, high))
    return Setting(
        "trail.threshold", "Threshold", G_TRAIL, INT, int(value),
        minimum=low, maximum=high, step=1000 if whole_ms else 1, unit="µs",
        help=TRAIL_THRESHOLD_HELP, shown_unit="ms" if whole_ms else "",
        shown_per=1000.0 if whole_ms else 1.0)


class EvkSettings(Provider):
    """An EVK4's HAL facilities, as Settings.

    WRITTEN FROM THE BINDINGS, VERIFIED AGAINST THEM, NEVER RUN ON A CAMERA.
    There is no EVK4 on the machine this was written on. Every getter and
    method name used here is in EVK_FACILITIES and checked against the real
    OpenEB bindings by the tests; a file-backed device (what OpenEB opens a
    .raw as) has none of these facilities — measured: get_i_ll_biases,
    get_i_erc_module, get_i_roi and the rest all return None — so each
    facility is optional here and a missing one is simply not listed.

    THE HAL REPORTS SUCCESS AS A BOOL — OR RAISES. set(), enable() and the
    set_* calls return False for some refusals, so a False is turned into a
    refusal here; ignoring it would report a bias the camera never took.
    Others raise (OpenEB source: a bias outside its range, an IMX636 trail
    threshold outside 1-100 ms), which Provider.set reports as a refusal too.

    A BIAS RUNS OVER LL_Bias_Info.get_bias_range(). Without the range-check
    bypass (DeviceConfig ll_biases_range_check_bypass) that is the
    RECOMMENDED range, and exactly what I_LL_Biases::set checks — and
    DeviceDiscovery.open(serial), as cameras.EvkBackend opens one, does not
    set the bypass. An IMX636's allowed bias_diff_on is -150..200, but only
    -85..140 is accepted (OpenEB's imx636_bias_settings*.h); bounded by the
    allowed range, a slider or a preset asked for values the HAL always
    refused. WITH the bypass, set() checks no range at all and
    get_bias_range() only reports the allowed one, which then bounds the
    slider (OpenEB i_ll_biases.cpp). The recommended range is reported
    either way.
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
        out.extend(self._display())
        out.extend(self._status())
        out.extend(self._camera())
        return out

    def describe_groups(self, groups: Iterable[str]) -> List[Setting]:
        """Each group's own facility, read alone (see Provider.describe_
        groups) — in describe order."""
        wanted = set(groups)
        out: List[Setting] = []
        for group, part in ((G_BIASES, self._biases), (G_ERC, self._erc),
                            (G_AFK, self._afk), (G_TRAIL, self._trail),
                            (G_ACTIVITY, self._activity),
                            (G_VIEW, self._display),
                            (G_STATUS, self._status),
                            (G_CAMERA, self._camera)):
            if group in wanted:
                out.extend(part())
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
        if key in DISPLAY_ATTRIBUTES:
            # The app's own settings: no facility to read at all. It fell
            # through to describe() — every bias and filter, dozens of USB
            # reads on a live EVK4 — once per step of its slider.
            return next(s for s in self._display() if s.key == key)
        part = {"erc": self._erc, "afk": self._afk, "trail": self._trail,
                "activity": self._activity, "status": self._status,
                "camera": self._camera}.get(group)
        for setting in (part() if part is not None else self.describe()):
            if setting.key == key:
                return setting
        raise SettingError(f"this camera has no setting called {key!r}")

    def _display(self) -> List[Setting]:
        return display_settings(self.device)

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
        # get_bias_range(): what set() checks without the bypass, the
        # allowed range only with it (see the class notes).
        allowed = _pair(_ask(info, "get_bias_range")) or _pair(
            _ask(info, "get_bias_allowed_range"))
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
                       help=ERC_HELP)]
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
                       bool(_ask(trail, "is_enabled")), help=TRAIL_HELP)]
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
            out.append(trail_threshold_setting(
                threshold, _num(_ask(trail, "get_min_supported_threshold")),
                _num(_ask(trail, "get_max_supported_threshold"))))
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
        if key in DISPLAY_ATTRIBUTES:
            return read_display(self.device, key)
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
        if key in DISPLAY_ATTRIBUTES:
            write_display(self.device, key, value)
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
        from .camera_capabilities import EVENTS, has

        dev = self.device
        state = dev.state
        ranges = dev.RANGES
        out: List[Setting] = []
        if has(dev, EVENTS):
            for name in dev.BIASES:
                # Like an EVK4 opened as Typhon opens one: the range it
                # accepts (RANGES — the IMX636's recommended range, what
                # get_bias_range() reports without the bypass) and the
                # recommended range itself, both the sensor's own.
                low, high = ranges[f"bias.{name}"]
                _allowed, recommended = dev.BIAS_TABLE[name]
                out.append(Setting(f"bias.{name}", name, G_BIASES, INT,
                                   state[f"bias.{name}"], low, high, 1,
                                   help=_BIAS_HELP.get(name, ""),
                                   recommended=tuple(recommended)))
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
            out.append(trail_threshold_setting(state["trail.threshold"],
                                               low, high))
            out.extend(display_settings(dev))
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
        out.extend(self._capture_rows())
        return out

    def _capture_rows(self) -> List[Setting]:
        """The frame camera's capture set-up (council_core.triggered)."""
        from . import triggered

        setup = getattr(self.device, "capture", None)
        if setup is None:
            return []
        return triggered.capture_settings(setup,
                                          self.device.capture_options())

    def read(self, key: str) -> Any:
        from . import triggered

        if key in DISPLAY_ATTRIBUTES:
            return read_display(self.device, key)
        if triggered.is_capture_key(key) and getattr(
                self.device, "capture", None) is not None:
            return triggered.read_capture(self.device.capture,
                                          self.device.capture_options(), key)
        if key in self.device.state:
            return self.device.state[key]
        raise SettingError(f"this camera has no setting called {key!r}")

    def held_by(self, setting: Setting, batch: Mapping[str, Any]) -> str:
        if setting.key != "AcquisitionFrameRate":
            return ""
        on = batch.get("AcquisitionFrameRateEnable",
                       self.device.state["AcquisitionFrameRateEnable"])
        on = on if isinstance(on, bool) else str(on).lower() in _TRUE
        return "" if on else '"Limit the frame rate" is off'

    def write(self, setting: Setting, value: Any,
              batch: Mapping[str, Any]) -> None:
        from . import triggered

        if setting.key in DISPLAY_ATTRIBUTES:
            write_display(self.device, setting.key, value)
            return
        if triggered.is_capture_key(setting.key) and getattr(
                self.device, "capture", None) is not None:
            if setting.key == triggered.DOWNSAMPLE_KEY:
                self.device.apply_downsample(triggered.factor_of(value))
            else:
                triggered.write_capture(self.device.capture, setting.key,
                                        value)
            return
        self.device.write_setting(setting.key, value)

    def defaults_source(self) -> str:
        """The simulated frame camera has factory settings to load, as a
        Basler does; the simulated event camera has none, as an EVK4 has
        none."""
        from .camera_capabilities import EVENTS, has

        if has(self.device, EVENTS):
            return ""
        return "the simulated camera's defaults"

    def load_defaults(self) -> None:
        if not self.defaults_source():
            raise SettingError("this camera has no defaults of its own to "
                               "load")
        self.device.load_defaults()
