"""
A fake EVK4 with EVERY facility Typhon's settings use — biases, the event
rate controller, anti-flicker, the trail filter, the activity filter,
monitoring, identification and the ROI — and an event stream, so a built
Typhon can be connected to it and its live view runs.

Shaped like the OpenEB bindings (checked by tests/test_camera_settings.py
against metavision_hal where it is importable): setters return a bool and
refuse out of range, enums are IntEnums with the bindings' member names, the
activity thresholds are one object with four fields. No physical EVK4 has
been on the machine this was written on; this stands in for one.

THE STREAM HONOURS THE SETTINGS, ROUGHLY: a higher bias_diff_on fires
fewer pixels on the moving edge, the trail filter drops its trailing OFF
events, and the ERC caps the count — so a drag in a pop-out shows in the
live picture, as it would on the sensor.
"""
from __future__ import annotations

import enum
import time
from typing import Any, Dict, List, Optional

import numpy as np

from council_core import cameras

EVENT_DTYPE = np.dtype([("x", "<u2"), ("y", "<u2"), ("p", "<i2"),
                        ("t", "<i8")])
WIDTH, HEIGHT = 1280, 720
SERIAL = "00051234"


class BiasInfo:
    def __init__(self, allowed, recommended):
        self.allowed, self.recommended = allowed, recommended

    def get_bias_allowed_range(self):
        return self.allowed

    def get_bias_recommended_range(self):
        return self.recommended

    def is_modifiable(self):
        return True

    def get_description(self):
        return "from the HAL"


class Biases:
    """I_LL_Biases with an IMX636's five biases and their ranges."""
    RANGES = {"bias_diff_on": (-85, 140), "bias_diff_off": (-35, 190),
              "bias_fo": (-35, 55), "bias_hpf": (0, 120),
              "bias_refr": (-20, 235)}

    def __init__(self):
        self.values = {name: 0 for name in self.RANGES}
        self.writes: List[Any] = []

    def get_all_biases(self):
        return dict(self.values)

    def get_bias_info(self, name):
        low, high = self.RANGES[name]
        return BiasInfo((low, high), (max(low, -25), min(high, 60)))

    def get(self, name):
        return self.values[name]

    def set(self, name, value):
        low, high = self.RANGES[name]
        self.writes.append((name, value))
        if not low <= value <= high:
            return False
        self.values[name] = int(value)
        return True


class _Module:
    def __init__(self):
        self.enabled = False

    def is_enabled(self):
        return self.enabled

    def enable(self, on):
        self.enabled = bool(on)
        return True


class Erc(_Module):
    def __init__(self):
        super().__init__()
        self.rate = 20_000_000

    def get_cd_event_rate(self):
        return self.rate

    def set_cd_event_rate(self, rate):
        if not 0 <= rate <= 1_000_000_000:
            return False
        self.rate = int(rate)
        return True

    def get_min_supported_cd_event_rate(self):
        return 0

    def get_max_supported_cd_event_rate(self):
        return 1_000_000_000

    def get_count_period(self):
        return 200


class AntiFlicker(_Module):
    class AntiFlickerMode(enum.IntEnum):
        BandPass = 0
        BandStop = 1

    def __init__(self):
        super().__init__()
        self.mode = self.AntiFlickerMode.BandStop
        self.band = (100, 150)
        self.duty = 50.0
        self.start, self.stop = 6, 4

    def get_filtering_mode(self):
        return self.mode

    def set_filtering_mode(self, mode):
        self.mode = self.AntiFlickerMode(int(mode))
        return True

    def get_band_low_frequency(self):
        return self.band[0]

    def get_band_high_frequency(self):
        return self.band[1]

    def set_frequency_band(self, low, high):
        if not 50 <= low < high <= 520:
            return False
        self.band = (int(low), int(high))
        return True

    def get_min_supported_frequency(self):
        return 50

    def get_max_supported_frequency(self):
        return 520

    def get_duty_cycle(self):
        return self.duty

    def set_duty_cycle(self, value):
        self.duty = float(value)
        return True

    def get_min_supported_duty_cycle(self):
        return 0.0

    def get_max_supported_duty_cycle(self):
        return 100.0

    def get_start_threshold(self):
        return self.start

    def set_start_threshold(self, value):
        self.start = int(value)
        return True

    def get_stop_threshold(self):
        return self.stop

    def set_stop_threshold(self, value):
        self.stop = int(value)
        return True

    def get_min_supported_start_threshold(self):
        return 0

    def get_max_supported_start_threshold(self):
        return 7

    def get_min_supported_stop_threshold(self):
        return 0

    def get_max_supported_stop_threshold(self):
        return 7


class Trail(_Module):
    class Type(enum.IntEnum):
        TRAIL = 0
        STC_CUT_TRAIL = 1
        STC_KEEP_TRAIL = 2

    def __init__(self):
        super().__init__()
        self.kind = self.Type.TRAIL
        self.threshold = 10_000

    def get_type(self):
        return self.kind

    def get_available_types(self):
        return set(self.Type)

    def set_type(self, kind):
        self.kind = self.Type(int(kind))
        return True

    def get_threshold(self):
        return self.threshold

    def set_threshold(self, value):
        if not 1 <= value <= 100_000:
            return False
        self.threshold = int(value)
        return True

    def get_min_supported_threshold(self):
        return 1

    def get_max_supported_threshold(self):
        return 100_000


class Thresholds:
    """I_EventRateActivityFilterModule.thresholds: four fields."""

    def __init__(self, a=0, b=0, c=0, d=0):
        self.lower_bound_start, self.lower_bound_stop = a, b
        self.upper_bound_start, self.upper_bound_stop = c, d


class Activity(_Module):
    def __init__(self):
        super().__init__()
        self.now = Thresholds(10_000, 8_000, 900_000, 800_000)

    def get_thresholds(self):
        t = self.now
        return Thresholds(t.lower_bound_start, t.lower_bound_stop,
                          t.upper_bound_start, t.upper_bound_stop)

    def set_thresholds(self, value):
        self.now = Thresholds(value.lower_bound_start, value.lower_bound_stop,
                              value.upper_bound_start, value.upper_bound_stop)
        return True

    def is_thresholds_supported(self):
        return Thresholds(1, 1, 1, 1)

    def get_min_supported_thresholds(self):
        return Thresholds(0, 0, 0, 0)

    def get_max_supported_thresholds(self):
        return Thresholds(10_000_000, 10_000_000, 10_000_000, 10_000_000)


class Monitoring:
    def get_temperature(self):
        return 34

    def get_illumination(self):
        return 120

    def get_pixel_dead_time(self):
        return 5


class HwId:
    def get_serial(self):
        return SERIAL

    def get_sensor_info(self):
        return type("S", (), {"name": "IMX636", "major_version": 4,
                              "minor_version": 2})()

    def get_current_data_encoding_format(self):
        return "EVT3"

    def get_integrator(self):
        return "Prophesee"

    def get_connection_type(self):
        return "USB"


class Roi:
    class Mode:
        ROI = "roi"

    def __init__(self):
        self.window = None
        self.enabled = False

    def set_mode(self, mode):
        self.mode = mode

    def Window(self, x, y, w, h):                       # noqa: N802
        return (x, y, w, h)

    def set_window(self, window):
        self.window = window
        return True

    def enable(self, on):
        self.enabled = bool(on)
        return True


class CdDecoder:
    def __init__(self):
        self.callbacks: List[Any] = []

    def add_event_buffer_callback(self, fn):
        self.callbacks.append(fn)


class Stream:
    """I_EventsStream: one buffer ready per WINDOW_S, a moving edge."""

    WINDOW_S = 0.01

    def __init__(self, hal: "FakeEvk4"):
        self.hal = hal
        self.started = False
        self._next = 0.0
        self.logging: Optional[str] = None

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def poll_buffer(self):
        now = time.monotonic()
        if not self.started or now < self._next:
            return 0
        self._next = now + self.WINDOW_S
        return 1

    def get_latest_raw_data(self):
        if self.logging is not None:
            with open(self.logging, "ab") as handle:
                handle.write(b"raw")
        return b"raw"

    def log_raw_data(self, path):
        open(path, "wb").close()
        self.logging = path
        return True

    def stop_log_raw_data(self):
        self.logging = None


class StreamDecoder:
    def __init__(self, hal: "FakeEvk4"):
        self.hal = hal
        self._t = 0
        self._column = 0

    def decode(self, raw):
        events = self.hal.events(self)
        for fn in self.hal.cd.callbacks:
            fn(events)


class FakeEvk4:
    """A HAL device with every facility. `events()` makes one buffer."""

    def __init__(self):
        self.biases = Biases()
        self.erc = Erc()
        self.afk = AntiFlicker()
        self.trail = Trail()
        self.activity = Activity()
        self.monitor = Monitoring()
        self.hw = HwId()
        self.roi = Roi()
        self.cd = CdDecoder()
        self.stream = Stream(self)
        self.decoder = StreamDecoder(self)
        self.geometry = type("G", (), {"get_width": lambda s: WIDTH,
                                       "get_height": lambda s: HEIGHT})()

    def get_i_geometry(self):
        return self.geometry

    def get_i_events_stream(self):
        return self.stream

    def get_i_events_stream_decoder(self):
        return self.decoder

    def get_i_event_cd_decoder(self):
        return self.cd

    def get_i_ll_biases(self):
        return self.biases

    def get_i_erc_module(self):
        return self.erc

    def get_i_antiflicker_module(self):
        return self.afk

    def get_i_event_trail_filter_module(self):
        return self.trail

    def get_i_event_rate(self):
        return self.activity

    def get_i_monitoring(self):
        return self.monitor

    def get_i_hw_identification(self):
        return self.hw

    def get_i_roi(self):
        return self.roi

    def events(self, decoder: StreamDecoder) -> Any:
        """A moving vertical edge: ON events on it, OFF just behind it (left
        out by the trail filter); sparser at a higher bias_diff_on; capped
        by the ERC."""
        decoder._column = (decoder._column + 9) % WIDTH
        every = 1 + max(0, int(self.biases.values["bias_diff_on"])) // 20
        ys = np.arange(0, HEIGHT, every, dtype=np.int64)
        parts = [(np.full(len(ys), decoder._column), ys, 1)]
        if not self.trail.enabled:
            parts.append((np.full(len(ys), (decoder._column + WIDTH - 2)
                                  % WIDTH), ys, 0))
        count = sum(len(p[1]) for p in parts)
        out = np.zeros(count, EVENT_DTYPE)
        at = 0
        for xs, yv, pol in parts:
            n = len(yv)
            out["x"][at:at + n] = xs
            out["y"][at:at + n] = yv
            out["p"][at:at + n] = pol
            at += n
        decoder._t += 10_000
        out["t"] = decoder._t + np.arange(count)
        if self.erc.enabled:
            out = out[:max(1, int(self.erc.rate * Stream.WINDOW_S))]
        return out


def info() -> cameras.CameraInfo:
    """What discovery says about it (no model: discovery never guesses)."""
    return cameras.CameraInfo("prophesee", SERIAL, "", SERIAL, "Prophesee",
                              "event")


def install(monkeypatch: Any, frame_camera: Any) -> FakeEvk4:
    """Make frame_camera's next scan list this fake EVK4 (and the simulated
    cameras) and its connect open it as a real cameras.EvkDevice."""
    hal = FakeEvk4()
    found = info()

    def open_camera(chosen, known=None):
        if chosen.backend == "prophesee":
            return cameras.EvkDevice(cameras.identify(chosen, hal), hal)
        return cameras.SyntheticBackend().open(chosen)

    def discover(known=None):
        listed = [found] + cameras.SyntheticBackend().discover()
        return cameras.Discovery(listed, [])

    monkeypatch.setattr(cameras, "open_camera", open_camera)
    monkeypatch.setattr(cameras, "discover", discover)
    return hal
