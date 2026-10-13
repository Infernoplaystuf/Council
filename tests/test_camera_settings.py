"""
council_core.camera_settings — every setting a camera has, described by the
camera, written in a safe order, and reported as the camera took it.

The Basler and EVK4 providers run here against fakes with the SAME SURFACE as
the real bindings: pylon parameter objects (GetValue/SetValue/GetMin/GetMax/
GetInc/HasInc/GetSymbolics/IsWritable), and the Metavision HAL facilities with
bool-returning setters and pybind-style enums. The method names the EVK4
provider calls are checked against the real OpenEB bindings at the end of this
file, when they are importable (C:/ceb/build on the machine this was written
on); the Basler provider is checked against pylon's emulator in
test_basler_real.py.
"""
from __future__ import annotations

import enum
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import camera_settings as cs
from council_core import cameras
from council_core.cameras import CameraInfo, Roi
from tests import fake_evk4


# ======================================================================
# coerce: what was typed or saved, as the setting takes it
# ======================================================================
def setting(kind, value=0, **kw):
    return cs.Setting("k", "Thing", "G", kind, value, **kw)


def test_a_number_is_clamped_to_the_range_the_camera_reported():
    s = setting(cs.FLOAT, minimum=1.0, maximum=10.0)
    assert cs.coerce(s, 99) == (10.0, "clamped to the maximum, 10")
    assert cs.coerce(s, "-5")[0] == 1.0


def test_an_integer_is_snapped_to_its_increment_from_the_minimum():
    """pylon refuses an off-grid integer rather than snapping it."""
    s = setting(cs.INT, minimum=16, maximum=99, step=4)
    value, note = cs.coerce(s, 23)
    assert value == 24 and "multiple of 4" in note
    assert cs.coerce(s, 99)[0] == 96, "100 is on the grid but past the max"


def test_a_choice_is_matched_without_regard_to_case():
    s = setting(cs.CHOICE, "Mono8", choices=("Mono8", "Mono12"))
    assert cs.coerce(s, "mono12") == ("Mono12", "")
    with pytest.raises(cs.SettingError, match="Mono8, Mono12"):
        cs.coerce(s, "RGB8")


def test_on_and_off_are_read_as_words_too():
    s = setting(cs.BOOL, False)
    assert cs.coerce(s, "on")[0] is True and cs.coerce(s, "0")[0] is False
    with pytest.raises(cs.SettingError):
        cs.coerce(s, "maybe")


def test_text_that_is_not_a_number_is_a_mistake_not_a_zero():
    for bad in ("bright", "nan", "inf"):
        with pytest.raises(cs.SettingError):
            cs.coerce(setting(cs.FLOAT), bad)


def test_a_whole_number_too_big_for_any_camera_is_a_mistake_not_a_crash():
    """float(10**400) raises OverflowError, which coerce let through: a
    preset holding such a number stopped half way, the setting at fault
    unnamed. It is refused like any other value the setting cannot take."""
    with pytest.raises(cs.SettingError, match="Thing"):
        cs.coerce(setting(cs.FLOAT), 10 ** 400)
    with pytest.raises(cs.SettingError, match="Thing"):
        cs.coerce(setting(cs.INT, minimum=0, maximum=10), -10 ** 400)
    dev = simulated("frame")
    done = cs.apply(dev, {"ExposureTime": 1234.0, "Gain": 10 ** 400,
                          "BlackLevel": 7})
    refused = {c.key: c.note for c in done.refused}
    assert list(refused) == ["Gain"] and "Gain" in refused["Gain"]
    assert dev.state["ExposureTime"] == 1234.0 and dev.state["BlackLevel"] == 7


def test_a_change_is_said_by_its_label_with_its_unit():
    """The status line under every tab and pop-out said "window_ms: 99.901"
    and "PixelFormat: Mono12" — keys, not what the rows are called."""
    took = cs.Change("window_ms", 99.901, 99.901)
    assert took.line() == "window_ms: 99.901"
    assert took.line("Picture window", "ms") == "Picture window: 99.901 ms"
    assert cs.Change("Gain", 30, 24.0, note="clamped to the maximum, 24"
                     ).line("Gain", "dB") == \
        "Gain: 24 dB, asked 30 (clamped to the maximum, 24)"
    assert cs.Change("PixelFormat", "X", "Mono8", ok=False, note="no"
                     ).line("Pixel format") == "Pixel format: NOT changed — no"
    assert cs.Change("ReverseX", True, True).line("Mirror left-right") == \
        "Mirror left-right: on"
    applied = cs.Applied([cs.Change("PixelFormat", "X", "Mono8", ok=False,
                                    note="not an entry")])
    assert "(Pixel format: not an entry)" in applied.summary(
        {"PixelFormat": "Pixel format"})


def test_typed_values_are_not_adjustments_when_they_took_as_typed():
    assert not cs.Change("k", "5000", 5000.0).adjusted
    assert not cs.Change("k", "on", True).adjusted
    assert not cs.Change("k", "mono8", "Mono8").adjusted
    assert cs.Change("k", "5003.7", 5004.0).adjusted


# ======================================================================
# A fake pylon node map
# ======================================================================
class Node:
    """A pylon parameter: typed value, range, increment, writability."""

    def __init__(self, value, low=None, high=None, inc=None, choices=None,
                 unit="", locked=False, cam=None, snap=None):
        self.value = value
        self.low, self.high, self.inc = low, high, inc
        self.choices = choices
        self.unit = unit
        self.locked = locked          # TLParamsLocked while grabbing
        self.cam = cam
        self.snap = snap
        self.writes = []
        if choices is not None:
            self.GetSymbolics = lambda: tuple(self.choices)

    def IsValid(self):
        return True

    def IsReadable(self):
        return True

    def IsWritable(self):
        return not (self.locked and self.cam is not None and self.cam.grabbing)

    def GetValue(self):
        return self.value

    def GetMin(self):
        return self.low

    def GetMax(self):
        return self.high

    def HasInc(self):
        return self.inc is not None

    def GetInc(self):
        if self.inc is None:
            raise RuntimeError("node does not have an increment.")
        return self.inc

    def GetUnit(self):
        return self.unit

    def SetValue(self, value):
        if not self.IsWritable():
            raise RuntimeError("AccessException: node is not writable")
        if self.low is not None and value < self.low:
            raise RuntimeError(f"OutOfRangeException: {value} < {self.low}")
        if self.high is not None and value > self.high:
            raise RuntimeError(f"OutOfRangeException: {value} > {self.high}")
        if self.choices is not None and value not in self.choices:
            raise RuntimeError(f"{value} is not an entry")
        if self.snap:
            value = self.snap(value)
        self.writes.append(value)
        if self.cam is not None:
            self.cam.log.append((self.name, value))
        self.value = value


class NodeMap:
    """Just enough of an InstantCamera for BaslerDevice and its settings."""

    def __init__(self, **extra):
        self.grabbing = False
        self.log = []
        nodes = dict(
            ExposureAuto=Node("Continuous", choices=("Off", "Once",
                                                     "Continuous")),
            ExposureTime=Node(10000.0, 1.0, 1e7, 0.1, unit="us",
                              snap=lambda v: round(v)),
            GainAuto=Node("Off", choices=("Off", "Once", "Continuous")),
            Gain=Node(0.0, 0.0, 48.0, unit="dB"),
            BlackLevel=Node(0.0, 0.0, 1023.0, unit="DN"),
            PixelFormat=Node("Mono8", choices=("Mono8", "Mono12"),
                             locked=True),
            ReverseX=Node(False, locked=True),
            BinningHorizontal=Node(1, 1, 4, 1, locked=True),
            AcquisitionFrameRateEnable=Node(True),
            AcquisitionFrameRate=Node(100.0, 0.01, 5000.0, unit="Hz"),
            WidthMax=Node(1024), HeightMax=Node(1040),
            Width=Node(1024, 1, 1024, 4, locked=True),
            Height=Node(1040, 1, 1040, 2, locked=True),
            OffsetX=Node(0, 0, 1023, 4), OffsetY=Node(0, 0, 1039, 2),
        )
        nodes.update(extra)
        for name, node in nodes.items():
            node.name, node.cam = name, self
            setattr(self, name, node)

    def StartGrabbing(self, *a):
        self.grabbing = True

    def StopGrabbing(self):
        self.grabbing = False


class Pylon:
    TimeoutHandling_Return = "return"
    GrabStrategy_LatestImageOnly = "latest"
    GrabStrategy_OneByOne = "onebyone"


def basler(**extra):
    cam = NodeMap(**extra)
    dev = cameras.BaslerDevice(CameraInfo("basler", "40012345", "acA1920",
                                          "40012345"), cam, Pylon())
    return dev, cam


def by_key(dev):
    return {s.key: s for s in dev.settings()}


# ======================================================================
# Basler
# ======================================================================
def test_basler_ranges_types_and_entries_come_from_the_nodes():
    found = by_key(basler()[0])
    assert found["ExposureTime"].kind == cs.FLOAT
    assert (found["ExposureTime"].minimum, found["ExposureTime"].maximum,
            found["ExposureTime"].step) == (1.0, 1e7, 0.1)
    assert found["ExposureTime"].unit == "µs"
    assert found["Gain"].step is None, "no increment: none invented"
    assert found["PixelFormat"].choices == ("Mono8", "Mono12")
    assert found["BinningHorizontal"].kind == cs.INT
    assert found["ReverseX"].kind == cs.BOOL
    assert found["AcquisitionFrameRate"].unit == "fps"
    assert "DeviceTemperature" not in found, "a node this model lacks"


def test_a_value_the_auto_loop_owns_is_held_not_read_only():
    exposure = by_key(basler()[0])["ExposureTime"]
    # Said by the label on the row that owns it, not by its node name
    # ("ExposureAuto is Continuous" under a row labelled "Auto exposure").
    assert exposure.held == '"Auto exposure" is Continuous'
    assert not exposure.read_only
    dev, cam = basler()
    cam.AcquisitionFrameRateEnable.value = False
    assert by_key(dev)["AcquisitionFrameRate"].held == \
        '"Limit the frame rate" is off'
    assert by_key(simulated("frame"))["AcquisitionFrameRate"].held == \
        '"Limit the frame rate" is off'


def test_a_basler_category_is_read_from_its_own_nodes_only():
    """A pop-out reads its category again after every drag (describe_
    groups). On a Basler that is the category's own nodes: with the group
    filter removed, a pop-out read the whole node map after each drag and
    every test stayed green (a mutation the review ran)."""
    dev, cam = basler(
        TriggerSelector=Node("FrameStart", choices=("FrameStart",
                                                    "FrameBurstStart")),
        TriggerMode=Node("Off", choices=("Off", "On")),
        GammaEnable=Node(False), Gamma=Node(1.0, 0.0, 4.0))
    asked = []
    original = dev._node
    dev._node = lambda name: asked.append(name) or original(name)
    got = dev.settings_provider().describe_groups(["Gain"])
    assert [s.key for s in got] == ["GainAuto", "Gain", "BlackLevel"]
    own = {n for f in cs.BASLER_FEATURES if f.group == "Gain"
           for n in f.names}
    assert asked and set(asked) <= own, sorted(set(asked) - own)


class Selected(Node):
    """A node of the SELECTED trigger (SFNC): TriggerMode and TriggerSource
    hold one value per TriggerSelector entry, as a Basler's do."""

    def __init__(self, values, choices=None, **node):
        super().__init__(next(iter(values.values())), choices=choices, **node)
        self.values = dict(values)

    def GetValue(self):
        return self.values[self.cam.TriggerSelector.value]

    def SetValue(self, value):
        super().SetValue(value)
        self.values[self.cam.TriggerSelector.value] = value


def trigger_basler(full=False):
    """Three triggers; `full`: each with its own edge and delay too, as
    the emulator's nine have (measured: TriggerSource, TriggerActivation
    and TriggerDelay change per TriggerSelector entry)."""
    entries = ("FrameBurstStart", "FrameStart", "ExposureStart")
    extra = {}
    if full:
        extra = dict(
            TriggerActivation=Selected({e: "RisingEdge" for e in entries},
                                       ("RisingEdge", "FallingEdge")),
            TriggerDelay=Selected({e: 0.0 for e in entries}, low=0.0,
                                  high=10000.0, unit="us"))
    return basler(
        TriggerSelector=Node("FrameStart", choices=entries),
        TriggerMode=Selected({e: "Off" for e in entries}, ("Off", "On")),
        TriggerSource=Selected({e: "Software" for e in entries},
                               ("Software", "Line1")), **extra)


def test_every_trigger_is_seen_not_only_the_selected_one():
    """TriggerMode is the SELECTED trigger's. Measured on the emulator: a
    FrameBurstStart trigger left On while the selector showed FrameStart
    was invisible to the rows and to Reset; and the selected one On (its
    source Software by default) froze the live view with no word said."""
    dev, cam = trigger_basler()
    provider = dev.settings_provider()
    assert provider.triggers_on() == []
    connected = provider.trigger_settings(selected=True)
    cam.TriggerSelector.value = "FrameBurstStart"
    cam.TriggerMode.SetValue("On")
    cam.TriggerSelector.value = "FrameStart"
    on = provider.triggers_on()
    assert on == [cs.Trigger("FrameBurstStart", "Software")]
    assert on[0].software
    assert on[0].said() == "its FrameBurstStart trigger is On (source Software)"
    assert cam.TriggerSelector.value == "FrameStart", "not put back"
    shown = by_key(dev)["TriggerMode"]
    assert shown.value == "Off" and shown.warning == "", "the selected one"
    # The selected trigger On: its row says what that does to the picture.
    cam.TriggerMode.SetValue("On")
    warning = by_key(dev)["TriggerMode"].warning
    assert "software trigger" in warning and "nothing here sends" in warning
    assert by_key(dev)["TriggerMode"].as_dict()["warning"] == warning
    cam.TriggerSource.SetValue("Line1")
    assert by_key(dev)["TriggerMode"].warning == \
        "No picture until a trigger arrives on Line1"
    assert not provider.triggers_on()[-1].software
    # Put back as connected: every trigger, the selector as it was.
    said = provider.restore_triggers(connected)
    assert said == ["the FrameBurstStart trigger Off again",
                    "the FrameStart trigger Off again (its source Software "
                    "too)"]
    assert provider.trigger_settings(selected=True) == connected
    assert cam.TriggerSelector.value == "FrameStart"
    assert provider.restore_triggers(connected) == [], "nothing to do"


def test_putting_every_trigger_back_puts_its_source_edge_and_delay_too():
    """"Reset as connected" put back each trigger's MODE only. A Basler
    holds a source, an edge and a delay per TriggerSelector entry too
    (measured on the emulator), so a FrameBurstStart trigger moved to
    Line1 on the falling edge, 250 µs late, stayed so after a reset. Every
    one goes back now — each trigger's mode after its source, edge and
    delay — the selector left where it was, and what changed is said."""
    dev, cam = trigger_basler(full=True)
    provider = dev.settings_provider()
    connected = provider.trigger_settings(selected=True)
    assert connected["TriggerSource[FrameBurstStart]"] == "Software"
    assert connected["TriggerActivation[FrameBurstStart]"] == "RisingEdge"
    assert connected["TriggerDelay[ExposureStart]"] == 0.0
    sel = cam.TriggerSelector
    sel.value = "FrameBurstStart"
    cam.TriggerSource.SetValue("Line1")
    cam.TriggerActivation.SetValue("FallingEdge")
    cam.TriggerDelay.SetValue(250.0)
    sel.value = "ExposureStart"
    cam.TriggerDelay.SetValue(40.0)
    cam.TriggerMode.SetValue("On")
    sel.value = "FrameStart"
    cam.log.clear()
    said = provider.restore_triggers(connected)
    assert provider.trigger_settings(selected=True) == connected
    assert sel.value == "FrameStart", "the selector left where it was"
    assert said == [
        "the FrameBurstStart trigger's source Software, edge RisingEdge and "
        "delay 0 µs again",
        "the ExposureStart trigger Off again (its delay 0 µs too)"], said
    written = [w for w in cam.log if w[0] != "TriggerSelector"]
    assert written == [("TriggerSource", "Software"),
                       ("TriggerActivation", "RisingEdge"),
                       ("TriggerDelay", 0.0),
                       ("TriggerDelay", 0.0), ("TriggerMode", "Off")], written
    # A trigger this camera does not have is said, not dropped.
    said = provider.restore_triggers({"TriggerMode[AcquisitionStart]": "Off"})
    assert said == ["the AcquisitionStart trigger's mode NOT put back to "
                    "Off — this camera has no AcquisitionStart trigger"], said


def test_a_model_with_one_trigger_and_no_selector_is_read_too():
    dev, cam = basler(TriggerMode=Node("On", choices=("Off", "On")),
                      TriggerSource=Node("Line1", choices=("Software",
                                                           "Line1")))
    provider = dev.settings_provider()
    assert provider.triggers_on() == [cs.Trigger("", "Line1")]
    # Its one trigger's settings are the rows' own (the snapshot holds
    # them): none listed apart, nothing to put back apart.
    assert provider.trigger_settings(selected=True) == {}
    assert provider.restore_triggers({}) == []
    # Cameras with no trigger at all say none.
    assert simulated("frame").settings_provider().triggers_on() == []
    assert simulated("frame").settings_provider().restore_triggers(
        {"TriggerMode[FrameStart]": "Off"}) == []


def test_the_selected_triggers_mode_is_written_after_its_source_edge_and_delay():
    """The other triggers' modes were written last (apply_triggers), but the
    SELECTED trigger's plain keys went in describe order — selector, MODE,
    source, edge, delay — so applying a preset turned that trigger On while
    its source and edge were still the old ones (measured on this fake: a
    write log with TriggerMode before TriggerSource)."""
    dev, cam = trigger_basler(full=True)
    done = dev.apply_settings({
        "TriggerSelector": "FrameBurstStart", "TriggerMode": "On",
        "TriggerSource": "Line1", "TriggerActivation": "FallingEdge",
        "TriggerDelay": 5.0, "Gain": 2.0})
    assert done.ok, done.summary()
    assert cam.log == [
        ("Gain", 2.0), ("TriggerSelector", "FrameBurstStart"),
        ("TriggerSource", "Line1"), ("TriggerActivation", "FallingEdge"),
        ("TriggerDelay", 5.0), ("TriggerMode", "On")], cam.log
    # The rows keep their order: the mode under the selector.
    keys = [s.key for s in dev.settings() if s.group == "Trigger"]
    assert keys == ["TriggerSelector", "TriggerMode", "TriggerSource",
                    "TriggerActivation", "TriggerDelay"]
    # Without a source in the set the mode stays where it was.
    cam.log.clear()
    dev.apply_settings({"TriggerMode": "Off", "TriggerSelector": "FrameStart"})
    assert cam.log == [("TriggerSelector", "FrameStart"),
                       ("TriggerMode", "Off")], cam.log


def _preset_of(dev):
    """What camera_presets.capture saves: the rows and every other trigger."""
    provider = dev.settings_provider()
    settings = cs.snapshot(dev, provider.describe())
    settings.update(provider.trigger_settings())
    return settings


def test_an_unchanged_trigger_is_no_reason_to_stop_when_the_mode_is_locked():
    """stops_needed counted EVERY other trigger's key as blocking whenever
    the selected trigger's node was locked by the stream — even for a preset
    just saved, so nothing would change (measured on this fake: [
    'TriggerMode[FrameBurstStart]', 'TriggerMode[ExposureStart]']). That
    restarted the live view for nothing and named raw keys. Only CHANGES
    count: the other triggers are read (the selector moved, then put back)
    and compared."""
    dev, cam = trigger_basler(full=True)
    cam.TriggerMode.locked = True             # locked while grabbing
    sel = cam.TriggerSelector
    sel.value = "FrameBurstStart"
    cam.TriggerSource.SetValue("Line1")
    cam.TriggerMode.SetValue("On")
    sel.value = "FrameStart"
    preset = _preset_of(dev)
    assert preset["TriggerMode[FrameBurstStart]"] == "On"
    dev.start()
    assert by_key(dev)["TriggerMode"].live is False
    assert cs.stops_needed(dev, preset) == []
    assert sel.value == "FrameStart", "reading them moved the selector"
    cam.log.clear()
    done = dev.apply_settings(preset)
    assert done.ok, done.summary()
    assert ("TriggerMode", "On") not in cam.log and \
        ("TriggerMode", "Off") not in cam.log, cam.log
    # A real change still needs the stream stopped, and only that one.
    changed = dict(preset, **{"TriggerMode[FrameBurstStart]": "Off"})
    assert cs.stops_needed(dev, changed) == ["TriggerMode[FrameBurstStart]"]
    with pytest.raises(cs.NeedsStop):
        dev.apply_settings(changed)
    # A trigger this camera does not have is refused, not stopped for.
    other = dict(preset, **{"TriggerMode[AcquisitionStart]": "On"})
    assert cs.stops_needed(dev, other) == []


def test_a_trigger_key_is_named_for_a_person():
    """The restart message ("The live view restarted for …") and a refusal
    named a raw "TriggerMode[FrameBurstStart]"."""
    dev, _ = trigger_basler(full=True)
    labels = cs.labels_of(dev.settings(), ["TriggerMode[FrameBurstStart]",
                                           "TriggerDelay[ExposureStart]",
                                           "Gain"])
    assert labels["TriggerMode[FrameBurstStart]"] == \
        "Trigger mode (FrameBurstStart trigger)"
    assert labels["TriggerDelay[ExposureStart]"] == \
        "Trigger delay (ExposureStart trigger)"
    assert labels["Gain"] == "Gain"
    # A camera without the trigger rows still names them by the table.
    assert cs.labels_of([], ["TriggerSource[FrameStart]"]) == {
        "TriggerSource[FrameStart]": "Trigger source (FrameStart trigger)"}


def test_a_selector_locked_by_the_stream_is_said_not_taken_for_a_missing_trigger():
    """With TriggerSelector locked while grabbing, _each_trigger can visit
    the selected trigger only. A preset then saved no other trigger and said
    nothing; applying one refused each with "this camera has no
    FrameBurstStart trigger" — the camera has it, only the selector could
    not move; and stops_needed did not count the selector's lock."""
    dev, cam = trigger_basler(full=True)
    cam.TriggerSelector.locked = True
    provider = dev.settings_provider()
    assert provider.unreadable_triggers() == [], "not streaming: it moves"
    dev.start()
    assert provider.unreadable_triggers() == ["FrameBurstStart",
                                              "ExposureStart"]
    assert provider.trigger_settings() == {}, "nothing it could not read"
    wanted = {"TriggerMode[FrameBurstStart]": "Off",
              "TriggerMode[AcquisitionStart]": "On"}
    # The selector must move for FrameBurstStart: the stream is in the way.
    # AcquisitionStart is not this camera's: refused, not stopped for.
    assert cs.stops_needed(dev, wanted) == ["TriggerMode[FrameBurstStart]"]
    refused = {c.key: c.note for c in provider.apply_triggers(wanted)}
    assert refused["TriggerMode[AcquisitionStart]"] == \
        "this camera has no AcquisitionStart trigger"
    note = refused["TriggerMode[FrameBurstStart]"]
    assert "has no" not in note, note
    assert note == ("the FrameBurstStart trigger cannot be selected now — "
                    "the camera locks the trigger selector while it "
                    "streams"), note
    # Stopped, the selector moves and every trigger is read and written.
    dev.stop()
    assert provider.unreadable_triggers() == []
    changes = provider.apply_triggers({"TriggerMode[FrameBurstStart]": "On"})
    assert [c.ok for c in changes] == [True]
    assert cam.TriggerMode.values["FrameBurstStart"] == "On"


def test_a_set_turns_the_auto_loop_off_before_the_value_it_owns():
    dev, cam = basler()
    done = dev.apply_settings({"ExposureTime": 2000, "ExposureAuto": "Off"})
    names = [name for name, _ in cam.log]
    assert names == ["ExposureAuto", "ExposureTime"]
    assert done.ok and cam.ExposureTime.value == 2000


def test_a_held_value_is_left_alone_and_said_so():
    dev, cam = basler()
    done = dev.apply_settings({"ExposureTime": 2000})
    change = done.changes[0]
    assert change.skipped and "Continuous" in change.note
    assert cam.ExposureTime.writes == []


def test_out_of_range_is_clamped_before_pylon_can_refuse_it():
    """pylon raises OutOfRangeException rather than clamping (measured)."""
    dev, cam = basler()
    change = dev.set_setting("Gain", 1e9)
    assert change.ok and change.value == 48.0 and change.adjusted
    assert "maximum" in change.note


def test_what_the_camera_snapped_is_what_is_reported():
    """ExposureTime 5003.7 came back 5004.0 on the emulator."""
    dev, cam = basler()
    dev.set_setting("ExposureAuto", "Off")
    change = dev.set_setting("ExposureTime", 5003.7)
    assert change.value == 5004 and change.adjusted
    assert "became" in change.note


def test_a_locked_setting_says_it_needs_the_stream_stopped():
    dev, cam = basler()
    dev.start()
    assert by_key(dev)["PixelFormat"].live is False
    with pytest.raises(cs.NeedsStop):
        dev.set_setting("PixelFormat", "Mono12")
    assert by_key(dev)["Gain"].live is True
    assert dev.set_setting("Gain", 3).ok, "gain is set live"


def test_a_set_the_stream_is_in_the_way_of_writes_nothing():
    """NeedsStop before the first write — never half a preset."""
    dev, cam = basler()
    dev.start()
    with pytest.raises(cs.NeedsStop) as caught:
        dev.apply_settings({"Gain": 6, "PixelFormat": "Mono12"})
    assert caught.value.key == "PixelFormat"
    assert cam.log == [], "something was written before the refusal"


def test_an_unchanged_locked_setting_is_no_reason_to_stop():
    dev, cam = basler()
    dev.start()
    values = {"PixelFormat": "mono8", "Gain": 6}
    assert cs.stops_needed(dev, values) == []
    done = dev.apply_settings(values)
    assert done.ok and cam.Gain.value == 6
    assert cam.PixelFormat.writes == []


def test_the_area_of_a_streaming_basler_needs_a_stop():
    """Before: set_roi moved OffsetX (writable while grabbing) and silently
    skipped the locked Width — a box moved and never resized."""
    dev, cam = basler()
    dev.start()
    with pytest.raises(cs.NeedsStop):
        dev.set_roi(Roi(0, 0, 64, 64))
    assert cs.stops_needed(dev, {}, Roi(0, 0, 64, 64)) == ["area"]
    assert cs.stops_needed(dev, {}, Roi(0, 0, 1024, 1040)) == [], \
        "the area it already has is no reason to stop"


def test_binning_goes_before_the_area_and_the_area_before_the_rate():
    """Binning changes the pixel grid the area is measured in, and the
    frame-rate limit depends on the area."""
    dev, cam = basler()
    done = dev.apply_settings({"AcquisitionFrameRate": 50,
                               "BinningHorizontal": 2, "Gain": 1},
                              roi=Roi(8, 2, 256, 128))
    names = [name for name, _ in cam.log]
    assert names.index("BinningHorizontal") < names.index("Width")
    assert names.index("Width") < names.index("AcquisitionFrameRate")
    assert done.roi == Roi(8, 2, 256, 128)


def test_a_setting_this_camera_lacks_is_reported_not_dropped():
    """A preset made on another model."""
    dev, _ = basler()
    done = dev.apply_settings({"DeviceLinkThroughputLimit": 5})
    assert not done.ok
    assert done.refused[0].note == "this camera has no such setting"
    assert "refused" in done.summary()


def test_frames_carry_the_area_they_were_taken_with():
    dev, cam = basler()
    dev.set_roi(Roi(8, 4, 64, 32))
    dev.start()
    assert dev._aoi == (8, 4, 64, 32)


def test_a_snapshot_holds_what_a_user_chose_not_readings():
    dev, _ = basler(DeviceTemperature=Node(41.5, unit="C"))
    snap = cs.snapshot(dev)
    assert "DeviceTemperature" not in snap
    assert snap["PixelFormat"] == "Mono8" and snap["ExposureTime"] == 10000.0


# ======================================================================
# A fake Metavision HAL, shaped like the real facilities
# ======================================================================
class Biases(fake_evk4.Biases):
    """The fake EVK4's IMX636 biases — OpenEB's own tables, checked by the
    HAL's rule (fake_evk4.Biases) — with each write logged, and a sensor
    that quantises: an odd bias_hpf lands on the even below."""

    def __init__(self):
        super().__init__()
        self.log = self.writes

    def set(self, name, value):
        took = super().set(name, value)
        if name == "bias_hpf":
            self.values[name] = value - value % 2
        return took


class Module:
    def __init__(self, log):
        self.enabled = False
        self.log = log

    def is_enabled(self):
        return self.enabled

    def enable(self, on):
        self.log.append((type(self).__name__, "enable", on))
        self.enabled = bool(on)
        return True


class Erc(Module):
    def __init__(self, log):
        super().__init__(log)
        self.rate = 20_000_000

    def get_cd_event_rate(self):
        return self.rate

    def set_cd_event_rate(self, rate):
        self.log.append(("Erc", "rate", rate))
        self.rate = rate
        return True

    def get_min_supported_cd_event_rate(self):
        return 0

    def get_max_supported_cd_event_rate(self):
        return 1_000_000_000

    def get_count_period(self):
        return 200


class AntiFlicker(Module):
    class AntiFlickerMode(enum.IntEnum):     # pybind11 enums: name + int
        BandPass = 0
        BandStop = 1

    def __init__(self, log):
        super().__init__(log)
        self.mode = self.AntiFlickerMode.BandStop
        self.band = (100, 150)

    def get_filtering_mode(self):
        return self.mode

    def set_filtering_mode(self, mode):
        assert isinstance(mode, self.AntiFlickerMode), "a name, not the enum"
        self.mode = mode
        return True

    def get_band_low_frequency(self):
        return self.band[0]

    def get_band_high_frequency(self):
        return self.band[1]

    def set_frequency_band(self, low, high):
        self.log.append(("AntiFlicker", "band", (low, high)))
        if low >= high:
            return False
        self.band = (low, high)
        return True

    def get_min_supported_frequency(self):
        return 50

    def get_max_supported_frequency(self):
        return 520


class Trail(Module):
    class Type(enum.IntEnum):
        TRAIL = 0
        STC_CUT_TRAIL = 1
        STC_KEEP_TRAIL = 2

    def __init__(self, log):
        super().__init__(log)
        self.kind = self.Type.TRAIL
        self.threshold = 10_000

    def get_type(self):
        return self.kind

    def get_available_types(self):
        return {self.Type.STC_KEEP_TRAIL, self.Type.TRAIL,
                self.Type.STC_CUT_TRAIL}

    def set_type(self, kind):
        self.kind = kind
        return True

    def get_threshold(self):
        return self.threshold

    def set_threshold(self, value):
        self.log.append(("Trail", "threshold", value))
        self.threshold = value
        return True

    def get_min_supported_threshold(self):
        return 1

    def get_max_supported_threshold(self):
        return 100_000


class Monitoring:
    def get_temperature(self):
        return 34

    def get_illumination(self):
        raise RuntimeError("not supported on this sensor")

    def get_pixel_dead_time(self):
        return 5


class HwId:
    def get_serial(self):
        return "00051234"

    def get_sensor_info(self):
        return type("S", (), {"name": "IMX636", "major_version": 4,
                              "minor_version": 2})()

    def get_current_data_encoding_format(self):
        return "EVT3"


class Hal:
    """A HAL device: the stream interfaces EvkDevice needs, plus whichever
    facilities `with_` names (a file-backed device has none of them)."""

    def __init__(self, with_=("biases", "erc", "afk", "trail", "monitor",
                              "hw")):
        log = self.log = []
        stream = type("Stream", (), {"start": lambda s: None,
                                     "stop": lambda s: None,
                                     "poll_buffer": lambda s: 0})()
        self.get_i_geometry = lambda: type("G", (), {
            "get_width": lambda s: 1280, "get_height": lambda s: 720})()
        self.get_i_events_stream = lambda: stream
        self.get_i_events_stream_decoder = lambda: object()
        self.get_i_event_cd_decoder = lambda: type("Cd", (), {
            "add_event_buffer_callback": lambda s, fn: None})()
        self.biases = Biases() if "biases" in with_ else None
        self.erc = Erc(log) if "erc" in with_ else None
        self.afk = AntiFlicker(log) if "afk" in with_ else None
        self.trail = Trail(log) if "trail" in with_ else None
        self.get_i_ll_biases = lambda: self.biases
        self.get_i_erc_module = lambda: self.erc
        self.get_i_antiflicker_module = lambda: self.afk
        self.get_i_event_trail_filter_module = lambda: self.trail
        self.get_i_event_rate = lambda: None
        self.get_i_monitoring = lambda: (Monitoring() if "monitor" in with_
                                         else None)
        self.get_i_hw_identification = lambda: (HwId() if "hw" in with_
                                                else None)


def evk(**kw):
    hal = Hal(**kw)
    dev = cameras.EvkDevice(CameraInfo("prophesee", "00051234", "EVK4",
                                       "00051234", kind="event"), hal)
    return dev, hal


# ======================================================================
# EVK4
# ======================================================================
def test_every_bias_the_hal_lists_is_offered_with_its_ranges():
    found = by_key(evk()[0])
    on = found["bias.bias_diff_on"]
    # The range the HAL accepts — the recommended one, without the bypass.
    assert (on.minimum, on.maximum) == (-85, 140)
    assert on.recommended == (-85, 140)
    assert on.group == cs.G_BIASES and on.kind == cs.INT
    assert {k for k in found if k.startswith("bias.")} == {
        f"bias.{n}" for n in Biases.RANGES}


def test_the_fake_evk4s_biases_are_openebs_imx636_tables():
    """tests/fake_evk4 reported each bias's RECOMMENDED range as "allowed"
    and an invented -25 … 60 slice as "recommended", and it had five
    biases, not six (bias_diff was missing). Its table is now OpenEB's,
    read here from the source where it is on the machine."""
    import re

    recommended = fake_evk4.openeb_source(
        "hal_psee_plugins/include/devices/imx636/imx636_bias_settings.h")
    allowed = fake_evk4.openeb_source(
        "hal_psee_plugins/include/devices/imx636/"
        "imx636_bias_settings_iterator.h")
    ll_biases = fake_evk4.openeb_source(
        "hal_psee_plugins/psee_hw_layer_headers/include/metavision/"
        "psee_hw_layer/devices/imx636/imx636_ll_biases.h")
    if None in (recommended, allowed, ll_biases):
        pytest.skip("the OpenEB source is not on this machine")
    # The table's fields are in this order (struct imx636_bias_setting).
    assert re.search(r"std::string name;\s*int min_allowed_offset;\s*"
                     r"int max_allowed_offset;\s*int min_recommended_offset;"
                     r"\s*int max_recommended_offset;", ll_biases)
    offsets = {k: int(v) for k, v in re.findall(
        r"static constexpr int (\w+)\s*=\s*(-?\d+);", recommended)}
    rows = re.findall(r'\{"(\w+)",\s*(-?\d+),\s*(-?\d+),\s*(\w+),\s*(\w+),'
                      r'\s*(true|false)\}', allowed)
    table = {name: ((int(lo), int(hi)), (offsets[rlo], offsets[rhi]))
             for name, lo, hi, rlo, rhi, _mod in rows}
    # ONE table in the product, read by the simulated event camera and the
    # fake EVK4 alike — pinned here to the OpenEB headers.
    assert table == cameras.IMX636_BIASES
    assert fake_evk4.Biases.RANGES is cameras.IMX636_BIASES
    assert cameras.SyntheticDevice.BIAS_TABLE is cameras.IMX636_BIASES
    assert table["bias_diff_on"] == ((-150, 200), (-85, 140))
    assert sorted(cameras.IMX636_BIASES) == list(cameras.IMX636_BIASES), \
        "get_all_biases is a std::map: by name"


def test_a_bias_runs_over_the_range_the_hal_accepts_not_the_allowed_one():
    """I_LL_Biases::set refuses (raises) a value outside get_bias_range(),
    which is the RECOMMENDED range unless the device was opened with the
    range-check bypass — and Typhon opens an EVK4 without it
    (DeviceDiscovery.open(serial)). The slider and a preset were bounded by
    the ALLOWED range: bias_diff_on 180 was offered and always refused.
    The fake had hidden it by calling the recommended range "allowed"."""
    hal = fake_evk4.FakeEvk4()
    dev = cameras.EvkDevice(fake_evk4.info(), hal)
    on = by_key(dev)["bias.bias_diff_on"]
    assert (on.minimum, on.maximum) == (-85, 140)
    assert on.recommended == (-85, 140)
    change = dev.set_setting("bias.bias_diff_on", 180)
    assert change.ok, change.note
    assert change.value == 140 and change.note == "clamped to the maximum, 140"
    done = dev.apply_settings({"bias.bias_refr": 250, "bias.bias_diff": -40})
    assert done.ok, done.summary()
    assert hal.biases.values["bias_refr"] == 235
    assert hal.biases.values["bias_diff"] == -25
    # Opened with the bypass, set() checks no range at all, and
    # get_bias_range() REPORTS the allowed one — which the slider then uses.
    hal.biases.bypass = True
    on = by_key(dev)["bias.bias_diff_on"]
    assert (on.minimum, on.maximum) == (-150, 200)
    assert on.recommended == (-85, 140)
    assert dev.set_setting("bias.bias_diff_on", 180).value == 180


def test_with_the_bypass_the_hal_checks_no_range_and_neither_does_the_fake():
    """With biases_range_check_bypass, I_LL_Biases::set skips the range
    check ENTIRELY (OpenEB i_ll_biases.cpp); get_bias_info only makes
    get_bias_range() report the allowed range. The fake raised outside the
    allowed range instead, so it modelled a HAL that does not exist."""
    biases = fake_evk4.Biases(bypass=True)
    assert biases.get_bias_info("bias_diff_on").get_bias_range() == (-150, 200)
    assert biases.set("bias_diff_on", 230) is True
    assert biases.get("bias_diff_on") == 230
    assert biases.set("bias_hpf", -10) is True
    # Without the bypass the recommended range is checked, as before.
    with pytest.raises(RuntimeError, match=r"within range \[-85, 140\]"):
        fake_evk4.Biases().set("bias_diff_on", 141)
    source = fake_evk4.openeb_source("hal/cpp/src/facilities/i_ll_biases.cpp")
    if source is not None:
        check = source.index(
            "if (!device_config_.biases_range_check_bypass())")
        assert source.index("get_bias_range()", check) < source.index(
            "return set_impl(bias_name, bias_value);", check), \
            "the range is checked only without the bypass"
        assert "bias_info.disable_recommended_range();" in source


def test_the_filters_and_the_readings_are_described():
    found = by_key(evk()[0])
    assert found["erc.rate"].maximum == 1_000_000_000
    assert found["afk.mode"].choices == ("BandPass", "BandStop")
    assert found["afk.mode"].value == "BandStop"
    assert (found["afk.low_hz"].minimum, found["afk.high_hz"].maximum) == (
        50, 520)
    assert found["trail.type"].choices == ("TRAIL", "STC_CUT_TRAIL",
                                           "STC_KEEP_TRAIL")
    assert found["status.temperature"].read_only
    assert found["status.temperature"].value == 34
    assert "status.illumination" not in found, "a reading that raised"
    assert found["camera.sensor"].value == "IMX636 4.2"
    assert not found["camera.serial"].savable


def test_the_help_says_which_way_refr_goes_and_what_erc_and_stc_delete():
    """Three texts the velocity design (docs/typhon_velocity_design.md 5.1)
    found wrong or missing: bias_refr gave no direction; the ERC said it
    "drops events evenly", when on the IMX636 it drops by time slot and
    quality degrades once it acts; the trail filter said STC "keeps only
    confirmed events", hiding that it deletes every lone event — which is
    what a small, fast particle makes."""
    found = by_key(evk()[0])
    refr = found["bias.bias_refr"].help
    assert "Higher shortens the rest, so more events" in refr
    assert "lower gives fewer events each time an edge crosses" in refr
    erc = found["erc.enabled"].help
    assert "Drops events evenly" not in erc and "not evenly" in erc
    assert "drops events by time slot" in erc
    assert "200 µs" in erc and "IMX636" in erc
    assert "signal quality degrades once it acts" in erc
    assert "Prefer a smaller area (ROI) and the filters" in erc
    trail = found["trail.enabled"].help
    assert "confirmed events" not in trail
    assert "TRAIL keeps the first event of each burst" in trail
    assert ("Both STC modes drop the first event of each burst and every "
            "lone event") in trail
    assert "small, fast particle" in trail
    # Read by an engineer who may not know the vendor's words: the burst,
    # the polarity and STC are said before (or where) they are used.
    assert trail.index("A burst is") < trail.index("of each burst")
    assert "same polarity (all ON, brighter, or all OFF, darker)" in trail
    assert "STC is Prophesee's spatio-temporal contrast filter" in trail
    assert trail.index("STC is") < trail.index("Both STC modes")
    threshold = found["trail.threshold"].help
    assert "1-100 ms" in threshold
    assert "rounded to the nearest whole millisecond" in threshold
    # The same words for the simulated event camera's rows.
    sim = by_key(simulated("event"))
    assert sim["bias.bias_refr"].help == refr


def test_the_numbers_the_help_quotes_are_openebs():
    """Checked against the OpenEB source where it is on this machine
    (C:/ceb/openeb, or OPENEB_SOURCE): the IMX636 trail filter takes
    1000-100000 µs and keeps whole milliseconds; the IMX636's ERC (the
    Gen41 one) counts over 200 µs and drops by time only."""
    from tests import fake_evk4

    trail = fake_evk4.openeb_source(
        "hal_psee_plugins/src/devices/imx636/"
        "imx636_event_trail_filter_module.cpp")
    erc = fake_evk4.openeb_source(
        "hal_psee_plugins/src/devices/gen41/gen41_erc.cpp")
    device = fake_evk4.openeb_source(
        "hal_psee_plugins/src/devices/imx636/imx636_tz_device.cpp")
    if None in (trail, erc, device):
        pytest.skip("the OpenEB source is not on this machine")
    assert "get_min_supported_threshold() const {\n    return 1000;" in trail
    assert "get_max_supported_threshold() const {\n    return 100000;" in trail
    assert "threshold_ms_ = std::roundf(threshold / 1000.0);" in trail
    assert "std::make_unique<Gen41Erc>" in device
    assert '{"erc_reference_period", 200}' in erc
    assert '{"h_dropping_en", 0}' in erc and '{"v_dropping_en", 0}' in erc
    assert '{"t_dropping_en", en}' in erc


def test_a_bias_the_sensor_refuses_is_a_refusal_not_a_success():
    """Outside its range I_LL_Biases::set RAISES (HalException
    ValueOutOfRange, OpenEB source) — it does not return False, as this
    test used to say; the fake it ran on returned False. A False from the
    sensor is a refusal too. Either way the camera keeps what it had."""
    dev, hal = evk()
    unclamped = cs.Setting("bias.bias_fo", "bias_fo", cs.G_BIASES, cs.INT, 0)
    provider = dev.settings_provider()
    assert provider.set("bias.bias_fo", 50, setting=unclamped).ok
    refused = provider.set("bias.bias_fo", 60, setting=unclamped)
    assert not refused.ok
    assert "Value should be within range [-35, 55]" in refused.note
    assert refused.value == 50, "the camera kept what it had"
    hal.biases.set = lambda name, value: False
    refused = provider.set("bias.bias_fo", 40, setting=unclamped)
    assert not refused.ok and "refused" in refused.note
    assert refused.value == 50


def test_a_quantised_bias_reports_what_the_sensor_holds():
    dev, _ = evk()
    change = dev.set_setting("bias.bias_hpf", 31)
    assert change.value == 30 and change.adjusted


def test_a_filters_parameters_go_before_its_enable_flag():
    dev, hal = evk()
    dev.apply_settings({"erc.enabled": True, "erc.rate": 5_000_000,
                        "trail.enabled": True, "trail.threshold": 2000})
    assert hal.log.index(("Erc", "rate", 5_000_000)) < hal.log.index(
        ("Erc", "enable", True))
    assert hal.log.index(("Trail", "threshold", 2000)) < hal.log.index(
        ("Trail", "enable", True))


def test_an_enum_setting_is_written_as_the_bindings_own_member():
    dev, hal = evk()
    assert dev.set_setting("afk.mode", "bandpass").value == "BandPass"
    assert hal.afk.mode is AntiFlicker.AntiFlickerMode.BandPass
    assert dev.set_setting("trail.type", "STC_CUT_TRAIL").ok


def test_a_band_moved_upwards_is_written_as_one_pair():
    """Writing the low end first against the OLD high end (150) would be
    refused: 200 > 150."""
    dev, hal = evk()
    done = dev.apply_settings({"afk.low_hz": 200, "afk.high_hz": 300})
    assert done.ok, done.summary()
    assert hal.afk.band == (200, 300)


def test_the_picture_window_is_a_setting_of_the_app_not_the_camera():
    dev, _ = evk()
    change = dev.set_setting("window_ms", 5)
    assert dev.accumulate_ms == 5.0 and change.ok


def test_an_evk4_area_is_set_live():
    dev, _ = evk()
    dev._started = True
    assert dev.area_live
    assert cs.stops_needed(dev, {"bias.bias_fo": 3}, Roi(0, 0, 64, 64)) == []


def test_a_recording_opened_as_a_device_lists_only_what_it_has():
    """A file-backed HAL device has none of the facilities (measured on
    OpenEB: get_i_ll_biases, get_i_erc_module, ... all return None)."""
    dev, _ = evk(with_=("hw",))
    keys = [s.key for s in dev.settings()]
    # The display settings are the app's own (how the picture is drawn),
    # so even a recording has them.
    assert keys == ["window_ms", "display.events", "display.palette",
                    "camera.serial", "camera.sensor", "camera.format"]


def test_evk_frames_carry_the_area_they_were_binned_against():
    dev, hal = evk()
    dev._roi = Roi(100, 50, 64, 32)
    dev._poll = lambda timeout: (np.array([110]), np.array([60]),
                                 np.array([1]), np.array([5, 9]))
    frame = dev.read()
    assert frame.meta["aoi"] == (100, 50, 64, 32)


# ======================================================================
# The simulated cameras
# ======================================================================
def simulated(kind):
    backend = cameras.SyntheticBackend()
    return backend.open(next(c for c in backend.discover() if c.kind == kind))


def test_the_simulated_frame_camera_has_basler_keys_and_its_own_ranges():
    found = by_key(simulated("frame"))
    assert (found["ExposureTime"].minimum, found["ExposureTime"].maximum) == \
        cameras.SyntheticDevice.RANGES["ExposureTime"]
    assert found["PixelFormat"].choices == ("Mono8", "Mono12")
    assert found["AcquisitionFrameRate"].held     # the limit is off


def test_the_simulated_frame_camera_refuses_like_a_basler_while_grabbing():
    dev = simulated("frame")
    dev.start()
    assert by_key(dev)["PixelFormat"].live is False
    with pytest.raises(cs.NeedsStop):
        dev.set_setting("PixelFormat", "Mono12")
    with pytest.raises(cs.NeedsStop):
        dev.set_roi(Roi(0, 0, 64, 64))
    assert dev.set_setting("Gain", 6).ok


def test_the_simulated_picture_honours_its_settings():
    dev = simulated("frame")
    before = dev.read().image
    dev.apply_settings({"PixelFormat": "Mono12", "ReverseX": True})
    after = dev.read().image
    assert after.dtype == np.uint16 and int(after.max()) <= 4095
    assert before.dtype == np.uint8


def test_the_simulated_event_camera_has_the_imx636s_biases_and_ranges():
    """It showed an invented recommended -50 … 50 for every bias and had
    five biases, not six (no bias_diff), so its tabs looked like no EVK4's.
    Now every bias is the IMX636's, described as an EVK4 opened as Typhon
    opens one describes it: the range it accepts is the recommended one."""
    dev = simulated("event")
    found = by_key(dev)
    sim = {k: s for k, s in found.items() if k.startswith("bias.")}
    real = {k: s for k, s in by_key(fake_evk4_device()).items()
            if k.startswith("bias.")}
    assert list(sim) == list(real) == [
        f"bias.{n}" for n in ("bias_diff", "bias_diff_off", "bias_diff_on",
                              "bias_fo", "bias_hpf", "bias_refr")]
    for key, setting in sim.items():
        twin = real[key]
        assert (setting.minimum, setting.maximum, setting.recommended) == (
            twin.minimum, twin.maximum, twin.recommended), key
        assert setting.help == twin.help, key
    assert found["bias.bias_diff"].recommended == (-25, 23)
    assert found["bias.bias_refr"].recommended == (-20, 235)
    assert all(s.recommended != (-50, 50) for s in sim.values())


def test_the_simulated_event_camera_refuses_a_bias_as_the_hal_does():
    """I_LL_Biases::set (without the range-check bypass) RAISES outside
    get_bias_range() in its own words, and the camera keeps what it had —
    the simulated camera stored any number it was handed."""
    dev = simulated("event")
    unclamped = cs.Setting("bias.bias_fo", "bias_fo", cs.G_BIASES, cs.INT, 0)
    provider = dev.settings_provider()
    assert provider.set("bias.bias_fo", 50, setting=unclamped).ok
    refused = provider.set("bias.bias_fo", 60, setting=unclamped)
    assert not refused.ok
    assert refused.note == ('Invalid value 60 for bias "bias_fo". Value '
                            'should be within range [-35, 55].')
    assert refused.value == 50 and dev.state["bias.bias_fo"] == 50
    # The fake EVK4 says it in the same words (one statement of them), in
    # the block a real HalException's text is (hal_exception_text).
    with pytest.raises(RuntimeError) as raised:
        fake_evk4.Biases().set("bias_fo", 60)
    assert str(raised.value) == cameras.hal_exception_text(
        cameras.HAL_VALUE_OUT_OF_RANGE, refused.note)
    assert cs._why(raised.value) == refused.note
    # Through the row's own range a slider or a preset is clamped first.
    change = dev.set_setting("bias.bias_diff_on", 180)
    assert change.ok and change.value == 140
    assert change.note == "clamped to the maximum, 140"


def fake_evk4_device():
    return cameras.EvkDevice(fake_evk4.info(), fake_evk4.FakeEvk4())


# ======================================================================
# An OpenEB exception, as Python gets it
# ======================================================================
RULE = "-" * 48


def test_an_openeb_exception_is_said_by_its_message_not_its_frame():
    """A real HalException reaches Python as a RuntimeError whose text is
    a BLOCK — a rule of dashes, the category, "Error <hex>: " and the
    message, a rule (BaseException::what, OpenEB sdk/base/utils/
    error_utils.h; measured in the pylon env, open_raw_file on a missing
    path). _why passed the block through whole, so a refusal that reached
    a row's one-line "NOT changed —" note was a run of dashes. The fakes
    raised the bare message, so the tests never saw it."""
    said = ('Invalid value 141 for bias "bias_diff_on". Value should be '
            'within range [-85, 140].')
    block = cameras.hal_exception_text(cameras.HAL_VALUE_OUT_OF_RANGE, said)
    assert block == (f"\n{RULE}\nMetavision HAL exception\n\nError 102001: "
                     f"{said}\n\n{RULE}\n")
    assert cs._why(RuntimeError(block)) == said
    assert cs._why(RuntimeError(block.strip())) == said
    # No message of its own (HalException(UnsupportedValue)): the code.
    bare = cameras.hal_exception_text(0x102004, "")
    assert cs._why(RuntimeError(bare)) == \
        "Metavision HAL exception, error 102004"
    # Anything else as it was: a GenICam text loses its file/line tail.
    assert cs._why(RuntimeError("Node not writable : RuntimeException "
                                "thrown (file 'x.cpp', line 9)")) == \
        "Node not writable"
    # Through a provider: the fake EVK4's refusals come as blocks now, and
    # a row's note is the message alone.
    dev = fake_evk4_device()
    unclamped = cs.Setting("bias.bias_fo", "bias_fo", cs.G_BIASES, cs.INT, 0)
    refused = dev.settings_provider().set("bias.bias_fo", 60,
                                          setting=unclamped)
    assert refused.note == ('Invalid value 60 for bias "bias_fo". Value '
                            'should be within range [-35, 55].')
    assert "\n" not in refused.line() and RULE not in refused.line()


def test_the_exception_block_is_openebs_own():
    """hal_exception_text against OpenEB's source, where it is on the
    machine: the layout (error_utils.h), the HAL category's name and its
    empty message (hal_exception.cpp), the codes (hal_error_code.h)."""
    utils = fake_evk4.openeb_source(
        "sdk/modules/base/cpp/include/metavision/sdk/base/utils/"
        "error_utils.h")
    category = fake_evk4.openeb_source("hal/cpp/src/utils/hal_exception.cpp")
    codes = fake_evk4.openeb_source(
        "hal/cpp/include/metavision/hal/utils/hal_error_code.h")
    if None in (utils, category, codes):
        pytest.skip("OpenEB's source is not on this machine")
    assert f'msg_ = "\\n{RULE}\\n";' in utils
    assert 'msg_ += std::string(ecat.name()) + "\\n\\n";' in utils
    assert 'msg_ += "Error " + error_as_hex.str() + ": ";' in utils
    assert 'msg_ += what_arg + "\\n";' in utils
    assert f'msg_ += "\\n{RULE}\\n";' in utils
    assert 'return "Metavision HAL exception";' in category
    assert "message(int err) const override {\n        return \"\";" in \
        category
    assert "CameraError = 0x100000" in codes
    assert "InvalidArgument = CameraError | 0x02000" in codes
    assert "ValueOutOfRange = InvalidArgument | 0x1" in codes
    assert cameras.HAL_INVALID_ARGUMENT == 0x100000 | 0x02000
    assert cameras.HAL_VALUE_OUT_OF_RANGE == cameras.HAL_INVALID_ARGUMENT | 1


# ======================================================================
# The trail filter's threshold: whole milliseconds, 1-100 ms
# ======================================================================
def test_the_trail_threshold_is_kept_in_whole_ms_as_openeb_keeps_it():
    """threshold_ms_ = std::roundf(threshold / 1000.0): the nearest whole
    ms, a half AWAY from zero (Python's round would make 2500 µs 2 ms);
    outside 1000-100000 µs a HalException in the HAL's words."""
    kept = cameras.trail_threshold_kept
    assert [kept(v) for v in (1000, 1499, 1500, 2500, 10400, 10500, 99_999,
                              100_000)] == [
        1000, 1000, 2000, 3000, 10000, 11000, 100_000, 100_000]
    for bad in (0, 1, 999, 100_001):
        with pytest.raises(ValueError, match=rf"Bad STC threshold value: "
                                             rf"{bad}\. Value should be in "
                                             rf"range \[1000, 100000\]\."):
            kept(bad)
    source = fake_evk4.openeb_source(
        "hal_psee_plugins/src/devices/imx636/"
        "imx636_event_trail_filter_module.cpp")
    if source is not None:
        assert ('"Bad STC threshold value: " << threshold << ". Value should '
                'be in range [1000, 100000]."') in source
        assert "throw HalException(HalErrorCode::InvalidArgument" in source


def test_the_fake_evk4s_trail_filter_rounds_and_raises_like_the_hal():
    """It took 1-100000 µs and kept any of them — 8706 µs, measured after a
    wheel turn, is a value no IMX636 can hold — and said False outside,
    where the HAL raises."""
    trail = fake_evk4.Trail()
    assert (trail.get_min_supported_threshold(),
            trail.get_max_supported_threshold()) == (1000, 100_000)
    assert trail.set_threshold(8706) is True
    assert trail.get_threshold() == 9000
    assert trail.set_threshold(2500) and trail.get_threshold() == 3000
    for bad in (1, 999, 100_001):
        with pytest.raises(RuntimeError, match="Bad STC threshold value"):
            trail.set_threshold(bad)
    assert trail.get_threshold() == 3000, "a refusal changes nothing"


def test_the_trail_threshold_steps_and_is_shown_in_whole_ms():
    """The row stepped by 1 µs over 1-100000: 99,001 slider positions of
    which 99 exist on the sensor. It steps by 1000 µs from 1000 and is
    SHOWN in ms; the value and a preset stay in µs, the HAL's unit."""
    for dev in (fake_evk4_device(), simulated("event")):
        row = by_key(dev)["trail.threshold"]
        assert (row.minimum, row.maximum, row.step) == (1000, 100_000, 1000)
        assert row.unit == "µs" and row.shown() == ("ms", 1000.0)
        assert row.as_dict()["shown_unit"] == "ms"
        assert row.value == 10_000
        change = dev.set_setting("trail.threshold", 12_400)
        assert change.ok and change.value == 12_000, change
        assert change.note == "snapped to whole ms"
        assert change.line(row.label, *row.shown()) == \
            "Threshold: 12 ms, asked 12.4 (snapped to whole ms)"
        assert cs.snapshot(dev)["trail.threshold"] == 12_000
        assert dev.set_setting("trail.threshold", 500).value == 1000
    # A threshold range that is not whole ms (no OpenEB sensor reports one)
    # keeps a 1 µs step, shown in µs.
    odd = cs.trail_threshold_setting(10_000, 1, 100_000)
    assert odd.step == 1 and odd.shown() == ("µs", 1.0)


def test_the_simulated_trail_filter_keeps_whole_ms_and_refuses_like_the_hal():
    dev = simulated("event")
    dev.write_setting("trail.threshold", 2500)
    assert dev.state["trail.threshold"] == 3000
    with pytest.raises(cameras.CameraError, match="Bad STC threshold"):
        dev.write_setting("trail.threshold", 999)
    assert dev.state["trail.threshold"] == 3000


def test_a_half_millisecond_threshold_is_rounded_as_the_hal_rounds_it():
    """coerce snapped the row BEFORE the camera saw it, with Python's
    round(), which takes a half to the EVEN whole: 3500 µs became 3 ms and
    99500 µs 99 ms, where an IMX636 asked for them keeps 4 and 100
    (std::roundf, a half away from zero: cameras.trail_threshold_kept).
    Only a preset or a script asks for one — the box and the slider give
    whole ms — so a preset holding 3500 loaded as 3 ms. A Basler's integer
    node keeps its own snap (from its minimum, by its increment)."""
    row = cs.trail_threshold_setting(10_000, 1000, 100_000)
    for asked in (1499, 1500, 2500, 3500, 11_500, 12_400, 50_500, 99_500):
        assert cs.coerce(row, asked)[0] == \
            cameras.trail_threshold_kept(asked), asked
    assert cs.coerce(row, 3500) == (4000, "snapped to whole ms")
    assert cs.coerce(row, 99_500) == (100_000, "snapped to whole ms")
    for dev in (fake_evk4_device(), simulated("event")):
        change = dev.set_setting("trail.threshold", 3500)
        assert change.ok and change.value == 4000, change
        assert dev.set_setting("trail.threshold", 99_500).value == 100_000
        applied = cs.apply(dev, {"trail.threshold": 11_500})
        assert applied.changes[0].value == 12_000, applied.changes
    basler = setting(cs.INT, minimum=16, maximum=99, step=4)
    assert cs.coerce(basler, 18)[0] == 16, "a Basler node's snap is its own"


def test_a_clamped_threshold_says_its_bound_in_the_unit_it_is_shown_in():
    """'Threshold: 1 ms, asked 0.5 (clamped to the minimum, 1000)': the
    note gave the bound in µs inside a line in ms."""
    row = cs.trail_threshold_setting(10_000, 1000, 100_000)
    assert cs.coerce(row, 500) == (1000, "clamped to the minimum, 1 ms")
    assert cs.coerce(row, 100_400) == (100_000,
                                       "clamped to the maximum, 100 ms")
    dev = simulated("event")
    change = dev.set_setting("trail.threshold", 500)
    assert change.line(row.label, *row.shown()) == \
        "Threshold: 1 ms, asked 0.5 (clamped to the minimum, 1 ms)"
    # A setting shown in its own unit says the bound as before.
    assert cs.coerce(setting(cs.FLOAT, minimum=1.0, maximum=10.0, unit="dB"),
                     99) == (10.0, "clamped to the maximum, 10")


def test_the_simulated_event_camera_fires_less_at_a_higher_threshold():
    dev = simulated("event")
    dev.start()
    many = dev.read().meta["events"]
    dev.set_setting("bias.bias_diff_on", 120)
    dev.set_setting("trail.enabled", True)
    assert dev.read().meta["events"] < many / 2


def test_the_frame_rate_setting_and_the_fps_box_are_one_rate():
    dev = simulated("frame")
    dev.set_frame_rate(12)
    assert by_key(dev)["AcquisitionFrameRate"].value == 12.0
    dev.apply_settings({"AcquisitionFrameRateEnable": False})
    assert dev._fps == cameras.SyntheticDevice.FRAME_FPS


# ======================================================================
# One setting is looked up on its own — a settings window writes one per
# step of a slider, and each write looks its setting up first
# ======================================================================
def counting(obj, name, calls):
    real = getattr(obj, name)

    def counted(*a, **kw):
        calls.append(name)
        return real(*a, **kw)
    setattr(obj, name, counted)


def test_one_bias_is_looked_up_without_reading_every_facility():
    """Before: find() described the whole camera — every bias's info, the
    ERC, anti-flicker, trail filter and the temperature — and
    set_camera_setting described it a second time to ask whether the
    stream was in the way. On an EVK4 each of those reads is a USB round
    trip."""
    dev, hal = evk()
    calls = []
    counting(hal.biases, "get_all_biases", calls)
    counting(hal.biases, "get_bias_info", calls)
    counting(hal.erc, "get_cd_event_rate", calls)
    counting(hal.afk, "get_band_low_frequency", calls)
    found = dev.settings_provider().find("bias.bias_fo")
    assert (found.key, found.minimum, found.maximum) == ("bias.bias_fo",
                                                         -35, 55)
    assert calls == ["get_bias_info"], calls
    calls.clear()
    assert dev.settings_provider().find("erc.rate").maximum == 1_000_000_000
    assert "get_all_biases" not in calls and "get_band_low_frequency" \
        not in calls
    with pytest.raises(cs.SettingError):
        dev.settings_provider().find("bias.no_such_bias")


def test_one_basler_feature_is_looked_up_without_the_whole_node_map():
    dev, cam = basler()
    calls = []
    counting(cam.BlackLevel, "GetValue", calls)
    counting(cam.PixelFormat, "GetValue", calls)
    found = dev.settings_provider().find("Gain")
    assert found.key == "Gain" and found.maximum == 48.0
    assert calls == [], "other features were read to find one"
    dev.start()
    assert dev.settings_provider().find("PixelFormat").live is False
    with pytest.raises(cs.SettingError):
        dev.settings_provider().find("NoSuchFeature")


def test_a_temperature_is_read_from_its_own_facility():
    dev, hal = evk()
    calls = []
    counting(hal.biases, "get_all_biases", calls)
    assert dev.settings_provider().read("status.temperature") == 34
    assert calls == []


# ======================================================================
# The camera's own factory settings
# ======================================================================
class Command:
    def __init__(self, cam):
        self.cam = cam
        self.runs = 0

    def IsValid(self):
        return True

    def IsReadable(self):
        return True

    def IsWritable(self):
        return True

    def Execute(self):
        self.runs += 1
        self.cam.log.append(("UserSetLoad", self.cam.UserSetSelector.value))


def test_a_basler_loads_its_factory_user_set_with_the_stream_stopped():
    dev, cam = basler(UserSetSelector=Node("UserSet1", choices=(
        "Default", "UserSet1")))
    cam.UserSetLoad = Command(cam)
    provider = dev.settings_provider()
    assert provider.defaults_source() == 'UserSet "Default"'
    dev.start()
    with pytest.raises(cs.NeedsStop):
        provider.load_defaults()
    assert cam.UserSetLoad.runs == 0, "loaded while grabbing"
    dev.stop()
    provider.load_defaults()
    assert cam.log[-2:] == [("UserSetSelector", "Default"),
                            ("UserSetLoad", "Default")]


def test_a_camera_without_a_factory_set_offers_none():
    dev, _ = basler()
    assert dev.settings_provider().defaults_source() == ""
    with pytest.raises(cs.SettingError):
        dev.settings_provider().load_defaults()
    assert evk()[0].settings_provider().defaults_source() == ""


def test_the_simulated_frame_camera_loads_its_defaults_like_a_basler():
    dev = cameras.SyntheticDevice(CameraInfo("synthetic", "sim-frame",
                                             "sim", kind="frame"))
    dev.set_setting("Gain", 12)
    dev.set_roi(Roi(64, 32, 128, 96))
    provider = dev.settings_provider()
    assert provider.defaults_source()
    dev.start()
    with pytest.raises(cs.NeedsStop):
        provider.load_defaults()
    dev.stop()
    provider.load_defaults()
    assert dev.state["Gain"] == 0.0
    assert dev.roi().as_tuple() == (0, 0, dev.WIDTH, dev.HEIGHT)
    event = cameras.SyntheticDevice(CameraInfo("synthetic", "sim-event",
                                               "sim", kind="event"))
    assert event.settings_provider().defaults_source() == "", \
        "an EVK4 has no factory set to load; neither does its stand-in"


# ======================================================================
# The real OpenEB bindings, when they are importable here
# ======================================================================
def _hal():
    try:
        import metavision_hal
    except Exception:                                       # noqa: BLE001
        pytest.skip("the Metavision HAL is not importable here")
    return metavision_hal


def test_a_real_hal_exception_is_the_block_and_is_said_plainly(tmp_path):
    """The real bindings' text for a HalException (a missing RAW file is
    one no camera is needed for): exactly hal_exception_text's block, and
    _why says the message alone."""
    hal = _hal()
    missing = tmp_path / "nothing_here.raw"
    with pytest.raises(RuntimeError) as raised:
        hal.DeviceDiscovery.open_raw_file(str(missing))
    said = f"Unable to open RAW file '{missing}'"
    assert str(raised.value) == cameras.hal_exception_text(0x101000, said)
    assert cs._why(raised.value) == said


def test_every_facility_method_used_exists_in_the_real_bindings():
    hal = _hal()
    for getter, (cls_name, methods) in cs.EVK_FACILITIES.items():
        assert hasattr(hal.Device, getter), getter
        cls = getattr(hal, cls_name)
        missing = [m for m in methods if not hasattr(cls, m)]
        assert not missing, f"{cls_name} has no {missing}"
    assert set(hal.I_AntiFlickerModule.AntiFlickerMode.__members__) == {
        "BandPass", "BandStop"}
    assert {"TRAIL", "STC_CUT_TRAIL", "STC_KEEP_TRAIL"} <= set(
        hal.I_EventTrailFilterModule.Type.__members__)
    for name in ("get_bias_range", "get_bias_allowed_range",
                 "get_bias_recommended_range", "is_modifiable",
                 "get_description"):
        assert hasattr(hal.LL_Bias_Info, name), name
    assert hasattr(hal.I_ROI, "Window") and hasattr(hal.I_ROI, "Mode")


def test_a_real_recording_device_describes_without_inventing(tmp_path):
    hal = _hal()
    try:
        import metavision_sdk_stream as stream_mod
    except Exception:                                       # noqa: BLE001
        pytest.skip("metavision_sdk_stream is not importable here")
    dtype = np.dtype([("x", "<u2"), ("y", "<u2"), ("p", "<i2"), ("t", "<i8")])
    events = np.zeros(100, dtype)
    events["t"] = np.arange(100) * 100
    path = tmp_path / "rec_events.raw"
    writer = stream_mod.RAWEvt2EventFileWriter(1280, 720, str(path))
    writer.add_cd_events(events)
    writer.flush()
    writer.close()
    del writer
    device = cameras.EvkDevice(CameraInfo("prophesee", str(path),
                                          kind="event"),
                               hal.DeviceDiscovery.open_raw_file(str(path)))
    try:
        found = by_key(device)
        assert "window_ms" in found
        assert not [k for k in found if k.split(".")[0] in
                    ("bias", "erc", "afk", "trail", "activity")]
        assert found["camera.format"].value == "EVT2"
    finally:
        device.close()


# ======================================================================
# Review: an area off the sensor
# ======================================================================
def test_an_area_off_the_sensor_is_refused_and_needs_no_stop():
    """fit_roi pulls ANY area onto the sensor — right for a drag, wrong for
    a saved one: 5000, 4000, 64, 64 became 576, 416, 64, 64 (measured)."""
    dev, cam = basler()
    far = Roi(5000, 4000, 64, 64)
    dev.start()
    assert cs.stops_needed(dev, {}, far) == [], "stopped only to refuse it"
    dev.stop()
    before = dev.roi()
    done = cs.apply(dev, {}, far)
    assert done.roi is None and "outside" in done.roi_error
    assert not done.ok and dev.roi() == before
    # Overlapping the edge is still fitted, as before.
    edge = cs.apply(dev, {}, Roi(1000, 1000, 64, 64))
    assert edge.roi is not None and not edge.roi_error


def test_the_picture_window_is_looked_up_without_reading_the_camera():
    """window_ms is the app's own setting, but find() fell through to
    describe(): every bias, the ERC, anti-flicker and trail filter read for
    each step of the picture-window slider (USB round trips on an EVK4)."""
    dev, hal = evk()
    calls = []
    counting(hal.biases, "get_all_biases", calls)
    counting(hal.biases, "get_bias_info", calls)
    counting(hal.erc, "get_cd_event_rate", calls)
    found = dev.settings_provider().find("window_ms")
    assert found.key == "window_ms" and found.unit == "ms"
    assert calls == [], calls
    change = dev.set_setting("window_ms", 50)
    assert change.value == 50.0 and calls == [], calls


def test_a_value_the_camera_cannot_take_is_no_reason_to_stop():
    """A preset holding a pixel format this camera does not offer (or a
    word where a number goes) is refused before any write — it stopped and
    restarted a streaming Basler only to be refused."""
    dev, cam = basler()
    dev.start()
    assert cs.stops_needed(dev, {"PixelFormat": "Mono99"}) == []
    done = cs.apply(dev, {"PixelFormat": "Mono99", "Gain": 3.0})
    assert [c.key for c in done.refused] == ["PixelFormat"]
    assert cs.stops_needed(dev, {"PixelFormat": "Mono12"}) == ["PixelFormat"]
