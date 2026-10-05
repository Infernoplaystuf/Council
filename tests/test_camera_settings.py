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
    assert exposure.held == "ExposureAuto is Continuous"
    assert not exposure.read_only


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
class BiasInfo:
    def __init__(self, allowed, recommended, modifiable=True):
        self.allowed, self.recommended = allowed, recommended
        self.modifiable = modifiable

    def get_bias_allowed_range(self):
        return self.allowed

    def get_bias_recommended_range(self):
        return self.recommended

    def is_modifiable(self):
        return self.modifiable

    def get_description(self):
        return "from the HAL"


class Biases:
    RANGES = {"bias_diff_on": (-85, 140), "bias_diff_off": (-35, 190),
              "bias_fo": (-35, 55), "bias_hpf": (0, 120),
              "bias_refr": (-20, 235)}

    def __init__(self):
        self.values = {name: 0 for name in self.RANGES}
        self.log = []

    def get_all_biases(self):
        return dict(self.values)

    def get_bias_info(self, name):
        low, high = self.RANGES[name]
        return BiasInfo((low, high), (max(low, -25), min(high, 60)))

    def get(self, name):
        return self.values[name]

    def set(self, name, value):
        low, high = self.RANGES[name]
        self.log.append((name, value))
        if not low <= value <= high:
            return False                 # the HAL's answer: a bool
        # The sensor quantises: an odd bias_hpf lands on the even below.
        self.values[name] = value - (value % 2 if name == "bias_hpf" else 0)
        return True


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
    assert (on.minimum, on.maximum) == (-85, 140)
    assert on.recommended == (-25, 60)
    assert on.group == cs.G_BIASES and on.kind == cs.INT
    assert {k for k in found if k.startswith("bias.")} == {
        f"bias.{n}" for n in Biases.RANGES}


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


def test_a_bias_the_sensor_refuses_is_a_refusal_not_a_success():
    """set() returns False rather than raising."""
    dev, hal = evk()
    change = dev.settings_provider().set(
        "bias.bias_fo", 50, setting=cs.Setting(
            "bias.bias_fo", "bias_fo", cs.G_BIASES, cs.INT, 0))  # no clamp
    assert change.ok
    hal.biases.RANGES = dict(hal.biases.RANGES, bias_fo=(-35, 10))
    refused = dev.settings_provider().set(
        "bias.bias_fo", 40, setting=cs.Setting(
            "bias.bias_fo", "bias_fo", cs.G_BIASES, cs.INT, 0))
    assert not refused.ok and "refused" in refused.note
    assert refused.value == 50, "the camera kept what it had"


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
    assert keys == ["window_ms", "camera.serial", "camera.sensor",
                    "camera.format"]


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
# The real OpenEB bindings, when they are importable here
# ======================================================================
def _hal():
    try:
        import metavision_hal
    except Exception:                                       # noqa: BLE001
        pytest.skip("the Metavision HAL is not importable here")
    return metavision_hal


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
    for name in ("get_bias_allowed_range", "get_bias_recommended_range",
                 "is_modifiable", "get_description"):
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
