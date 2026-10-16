"""
Capabilities instead of "is it an event camera?", and the Provider.
extra_stops hook (plugin design 2.1, 2.4 item 4, 3.4; stage P2 row 1):

  * council_core.camera_capabilities: the names Typhon knows and the one
    rule that reads them off an open camera, for every camera family;
  * a camera slot answers what its camera can do — what the camera plugin
    that opened it confirmed, else what the camera says of itself;
  * frame_camera, the simulated camera's settings and the Council's Capture
    tab ask that question, and no longer compare a camera's kind with
    "event" (and what they say is what they said before);
  * the generic stop check names no camera: a provider says what else the
    stream is in the way of (extra_stops) — a Basler's other triggers.
"""
from __future__ import annotations

import ast
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
from council_core import camera_capabilities as cc  # noqa: E402
from council_core import camera_rig  # noqa: E402
from council_core import camera_settings as cs  # noqa: E402
from council_core import cameras  # noqa: E402
from tests import fake_evk4  # noqa: E402
from tests.sim_cameras import simulated_row  # noqa: E402


@pytest.fixture
def clean():
    from council_core import capture

    capture.warm_imports()
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None
    frame_camera._RIG.active.confirmed = None


def sim(kind):
    backend = cameras.SyntheticBackend()
    info = next(i for i in backend.discover() if i.kind == kind)
    return backend.open(info)


# ======================================================================
# The names and the rule
# ======================================================================
def test_the_names_are_the_designs_table():
    """Design 3.4's table: every capability with what it means and the
    facet it is used through (built at P3 and with velocity)."""
    assert set(cc.CAPABILITIES) == {
        "frames", "events", "restartable", "area_live", "grab_failures",
        "raw_recording", "software_trigger", "hardware_trigger", "binning",
        "camera_clock", "event_stream"}
    assert cc.CAPABILITIES["software_trigger"][1] == "SoftwareTrigger"
    assert cc.CAPABILITIES["binning"][1] == "Binning"
    assert set(cc.FROM_FLAGS) <= set(cc.CAPABILITIES)


def test_an_unknown_offered_name_is_ignored_and_a_needed_one_is_missing():
    """Design 3.4: what a camera OFFERS that Typhon does not know is
    ignored; what a feature NEEDS that Typhon does not know is missing."""
    assert cc.known(["frames", "x_ray", "events"]) == {"frames", "events"}
    assert cc.missing(["frames"], {"frames"}) == []
    assert cc.missing(["frames|events"], {"events"}) == []
    assert cc.missing(["software_trigger|hardware_trigger"], {"frames"}) == [
        "software_trigger|hardware_trigger"]
    assert cc.missing(["x_ray"], {"x_ray", "frames"}) == ["x_ray"]


def test_the_rule_reads_every_family_off_its_own_flags():
    """What today's Device says of itself — the Basler and EVK4 classes as
    they are, the simulated ones, nothing at all."""
    assert cc.of_device(None) == frozenset()
    assert cc.of_device(sim("frame")) == {"frames", "restartable"}
    assert cc.of_device(sim("event")) == {"events", "restartable",
                                          "area_live"}
    evk = cameras.EvkDevice(fake_evk4.info(), fake_evk4.FakeEvk4())
    assert cc.of_device(evk) == {"events", "area_live", "raw_recording"}, \
        "an EVK4's stream is never restarted on one connection"
    from tests.test_basler_trigger import FakeBasler, FakePylon
    pylon = FakePylon(FakeBasler())
    backend = cameras.BaslerBackend(sdk=pylon)
    basler = backend.open(backend.discover()[0])
    try:
        assert cc.of_device(basler) == {"frames", "restartable",
                                        "grab_failures"}
    finally:
        basler.close()


def test_a_device_with_no_kind_is_a_frame_camera_as_camera_info_is():
    class Bare:
        pass
    assert cc.of_device(Bare()) == {"frames", "restartable"}
    assert cc.kind_of(Bare()) == "frame"
    event = cameras.CameraInfo("x", "k", kind="event")
    assert cc.of_device(Bare(), event) == {"events", "restartable"}, \
        "the slot's info speaks for a device without one"


def test_the_rule_never_calls_the_camera():
    """Only flags: nothing on the device is called, nothing written."""
    calls = []

    class Watch:
        info = cameras.CameraInfo("x", "k", kind="frame")
        restartable = True

        def __getattr__(self, name):
            calls.append(name)
            raise AttributeError(name)
    cc.of_device(Watch())
    assert all(not callable(getattr(cameras.Device, n, None))
               for n in calls), calls


# ======================================================================
# A slot answers
# ======================================================================
class Template:
    def __init__(self):
        self.device = None
        self.info = None

    def clear(self):
        self.device = None
        self.info = None


def test_a_slot_answers_what_its_plugin_confirmed_else_the_camera():
    rig = camera_rig.Rig(Template)
    slot = rig.active
    assert slot.capabilities == frozenset(), "no camera, nothing"
    device = sim("frame")
    slot.device = device
    assert slot.capabilities == {"frames", "restartable"}
    slot.confirm(device, {"frames", "restartable", "software_trigger"})
    assert slot.capabilities == {"frames", "restartable", "software_trigger"}
    other = sim("event")
    slot.device = other
    assert slot.capabilities == {"events", "restartable", "area_live"}, \
        "a confirmation is for the device it was made for only"
    slot.clear()
    assert slot.capabilities == frozenset()


def test_a_template_may_not_declare_what_a_slot_answers_with():
    """frame_camera._Live may not declare a per-connection field by the
    names the slot answers with: it would hide them."""
    for name in ("capabilities", "confirmed", "confirm"):
        assert name in camera_rig.SLOT_OWN

        def init(self, name=name):
            self.device = None
            setattr(self, name, None)

        def clear(self, name=name):
            self.device = None
            setattr(self, name, None)
        bad = type("Bad", (), {"__init__": init, "clear": clear})
        with pytest.raises(ValueError, match=name):
            camera_rig.Rig(bad)


# ======================================================================
# The questions asked of a slot, not of a kind
# ======================================================================
KIND_CHECK = re.compile(r"""(?:\.kind|get\(["']kind["']\))\s*[!=]=\s*["']event["']""")


def _source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_frame_camera_asks_capabilities_not_the_kind():
    """The four places frame_camera asked "is it an event camera?" (design
    2.4 item 4) ask the active slot for `events`."""
    text = _source("frame_camera.py")
    assert not KIND_CHECK.search(text), KIND_CHECK.search(text).group(0)
    assert text.count('_can("events")') == 4


def test_the_settings_code_and_the_capture_tab_ask_capabilities_too():
    settings = _source("council_core/camera_settings.py")
    tree = ast.parse(settings)
    synthetic = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                     and n.name == "SyntheticSettings")
    body = ast.get_source_segment(settings, synthetic)
    assert not KIND_CHECK.search(body)
    assert body.count("has(") >= 2
    tab = _source("council_qt/tabs/capture.py")
    assert not KIND_CHECK.search(tab)
    assert tab.count('camera_capabilities.has(') == 2


def test_an_event_camera_still_says_events_and_windows(clean, tmp_path):
    """What the lines said before, they say now."""
    listed = frame_camera.list_cameras()
    frame_camera.connect(simulated_row(listed["rows"], "event"))
    out = frame_camera.set_frame_rate(50)
    assert "(20.0 ms windows)" in out["summary"]
    out = frame_camera.apply_frame_rate(100)
    assert "(10.0 ms windows)" in out["summary"]
    frame_camera.start(str(tmp_path))
    try:
        time.sleep(0.2)
        said = []
        for _ in range(20):
            frame_camera.pump(lambda a: None, said.append)
            time.sleep(0.01)
    finally:
        frame_camera.stop()
    assert any("events/window" in s for s in said), said[:3]


def test_a_frame_camera_says_no_windows(clean):
    listed = frame_camera.list_cameras()
    frame_camera.connect(simulated_row(listed["rows"], "frame"))
    out = frame_camera.set_frame_rate(20)
    assert "windows" not in out["summary"]
    out = frame_camera.apply_frame_rate(25)
    assert "windows" not in out["summary"]


def test_the_answer_is_the_slots_not_the_cameras_kind(clean):
    """The question goes to the slot: a frame camera whose plugin confirmed
    `events` is spoken of as one, and an event camera confirmed without it
    is not. (No plugin confirms that today; it shows who answers.)"""
    listed = frame_camera.list_cameras()
    frame_camera.connect(simulated_row(listed["rows"], "event"))
    slot = frame_camera._RIG.active
    slot.confirm(frame_camera._LIVE.device, {"frames", "restartable"})
    out = frame_camera.set_frame_rate(50)
    assert "windows" not in out["summary"]
    assert frame_camera._preview_line(
        frame_camera._LIVE.session, frame_camera._LIVE.session.stats(),
        cameras.Frame(None, 1, 0, {"kind": "event", "events": 7})
    ).count("events/window") == 0
    slot.confirm(frame_camera._LIVE.device, {"events", "area_live"})
    out = frame_camera.set_frame_rate(50)
    assert "(20.0 ms windows)" in out["summary"]
    assert "7 events/window" in frame_camera._preview_line(
        frame_camera._LIVE.session, frame_camera._LIVE.session.stats(),
        cameras.Frame(None, 1, 0, {"events": 7}))


def test_no_frame_no_event_count(clean):
    """As before: the event count is a frame's; with no frame, none."""
    listed = frame_camera.list_cameras()
    frame_camera.connect(simulated_row(listed["rows"], "event"))
    stats = frame_camera._LIVE.session.stats()
    assert "events/window" not in frame_camera._status_line(stats, None)
    assert "events/window" in frame_camera._status_line(
        stats, cameras.Frame(None, 1, 0, {"kind": "event", "events": 3}))


def test_the_simulated_settings_ask_too():
    """The simulated event camera's settings are an EVK4's because it has
    `events`, and it has no factory set to load; the frame camera's are a
    Basler's."""
    event = sim("event").settings_provider()
    assert any(s.key.startswith("bias.") for s in event.describe())
    assert event.defaults_source() == ""
    frame = sim("frame").settings_provider()
    assert any(s.key == "ExposureTime" for s in frame.describe())
    assert frame.defaults_source() == "the simulated camera's defaults"


# ======================================================================
# Provider.extra_stops (design 2.1)
# ======================================================================
def test_the_base_provider_adds_no_stops():
    assert cs.Provider().extra_stops({"TriggerMode[FrameStart]": "On"}, []) \
        == []
    assert cs.NoSettings().extra_stops({"x": 1}, []) == []


def test_a_baslers_other_triggers_are_its_own_stops():
    """Today's _triggers_in_the_way, reached through BaslerSettings.
    extra_stops: what tests/test_camera_settings.py pins of the generic
    check holds through the hook."""
    from tests.test_camera_settings import trigger_basler

    dev, cam = trigger_basler()
    provider = dev.settings_provider()
    dev.start()
    try:
        cam.TriggerSelector.locked = True
        cam.TriggerMode.locked = True
        described = provider.describe()
        asked = {"TriggerMode[FrameBurstStart]": "On"}
        assert provider.extra_stops(asked, described) == \
            ["TriggerMode[FrameBurstStart]"] == \
            cs._triggers_in_the_way(dev, asked,
                                    {s.key: s for s in described})
        assert cs.stops_needed(dev, asked) == ["TriggerMode[FrameBurstStart]"]
    finally:
        dev.stop()


class _Live(cs.Provider):
    """A provider with one setting and one key of its own that the stream
    is in the way of — a camera from someone else."""

    def __init__(self, device):
        self.device = device
        self.asked = []

    def describe(self):
        return [cs.Setting("gain", "Gain", "Gain", cs.FLOAT,
                           self.device.values["gain"], 0.0, 10.0)]

    def read(self, key):
        return self.device.values[key]

    def write(self, setting, value, batch):
        self.device.values[setting.key] = value

    def extra_stops(self, values, described):
        self.asked.append((dict(values), [s.key for s in described]))
        return [k for k in values if k.startswith("acme.thermal/")]


class _Device:
    streaming = True
    area_live = True

    def __init__(self):
        self.values = {"gain": 1.0}
        self.made = 0
        self.provider = None

    def settings_provider(self):
        self.made += 1
        self.provider = _Live(self)
        return self.provider


def test_the_generic_check_asks_the_provider_for_the_keys_it_does_not_describe():
    dev = _Device()
    assert cs.stops_needed(dev, {"gain": 2.0, "acme.thermal/palette": "Iron"}
                           ) == ["acme.thermal/palette"]
    assert dev.provider.asked == [({"acme.thermal/palette": "Iron"},
                                   ["gain"])], \
        "only the keys no row describes, with the description"
    with pytest.raises(cs.NeedsStop):
        cs.apply(dev, {"gain": 3.0, "acme.thermal/palette": "Iron"})
    assert dev.values["gain"] == 1.0, "refused before anything was written"


def test_no_provider_is_made_when_every_key_is_described():
    """A settings window writes one described key per slider step: the
    hook costs it nothing (no provider made for it)."""
    dev = _Device()
    described = dev.settings_provider().describe()
    dev.made = 0
    assert cs.stops_needed(dev, {"gain": 2.0}, described=described) == []
    assert dev.made == 0
    assert cs.stops_needed(dev, {"other": 1}, described=described) == []
    assert dev.made == 1


def test_a_stand_in_provider_without_the_hook_adds_nothing():
    class Plain:
        def describe(self):
            return []

    class Dev:
        streaming = True

        def settings_provider(self):
            return Plain()
    assert cs.stops_needed(Dev(), {"TriggerMode[FrameStart]": "On"}) == []
