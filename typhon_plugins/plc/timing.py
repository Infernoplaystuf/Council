"""
The PLC plugin's sums: its settings, when a bottle reaches the camera, the
camera's trigger delay (wire mode) or Typhon's wait (network mode), the
correction auto-calibration makes, and the bottle count against the
frames saved. Pure Python, no I/O: each piece is tested on its own.

THE TIMING (both modes)
A bottle crosses the photo-eye; it reaches the middle of the camera's view
`travel = distance_mm / speed_mm_s` seconds later. Between the photo-eye
and the camera's exposure there are fixed delays Typhon cannot see: the
PLC's scan, its output switching, the camera's own trigger latency (and in
network mode the network). `fixed_latency_ms` is their sum, and
`offset_ms` the correction auto-calibration found (positive: fire later).

  wire     the PLC's output goes to the camera at once; the camera waits
           TriggerDelay = travel - fixed + offset (timed in the camera, to
           the microsecond)
  network  Typhon sees the count rise when it next reads it; it guesses
           the crossing was half a poll and half a round trip earlier,
           waits until crossing + travel - fixed + offset, then fires
           (software trigger: a few ms of spread on Windows)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Mapping, Optional

MODE_WIRE = "Wire (PLC output to camera)"
MODE_NETWORK = "Network (Typhon fires)"
MODES = (MODE_WIRE, MODE_NETWORK)

LINK_TCP = "Ethernet (Modbus TCP)"
LINK_SERIAL = "Serial (Modbus RTU)"
LINKS = (LINK_TCP, LINK_SERIAL)
PARITIES = ("None", "Even", "Odd")
BAUDS = (9600, 19200, 38400, 57600, 115200)

SPEED_FIXED = "Fixed"
SPEED_PLC = "From PLC register"
SPEEDS = (SPEED_FIXED, SPEED_PLC)

DIR_RIGHT = "Left to right"
DIR_LEFT = "Right to left"
DIR_DOWN = "Top to bottom"
DIR_UP = "Bottom to top"
DIRECTIONS = (DIR_RIGHT, DIR_LEFT, DIR_DOWN, DIR_UP)

#: Longest wait the plugin will set or keep (a bottle 60 s away is a
#: wrong distance or a stopped belt, not a plan).
MAX_WAIT_S = 60.0


@dataclass
class PlcSettings:
    link: str = LINK_TCP
    host: str = ""
    port: int = 502
    #: the PLC's Modbus station (unit) number: over Ethernet Do-More
    #: answers any; over serial it must match the port's station number
    unit: int = 1
    serial_port: str = ""
    baud: int = 115200
    parity: str = "None"
    stop_bits: int = 1
    mode: str = MODE_WIRE
    count_register: str = "MHR1"
    #: "Fire camera via PLC": Typhon writes 1 here; the ladder pulses the
    #: camera's output and writes 0 back (docs/typhon_plc_trigger.md 3.4b)
    fire_register: str = "MHR3"
    #: who triggers the camera: Typhon writes 0 (the PLC's timer) or 1 (the
    #: computer: the timer stops); docs/typhon_plc_trigger.md 9.4
    timer_register: str = "MHR5"
    speed_source: str = SPEED_FIXED
    speed_mm_s: float = 500.0
    speed_register: str = "MHR2"
    #: register units per mm/s (10: the PLC keeps 0.1 mm/s)
    speed_scale: float = 1.0
    distance_mm: float = 100.0
    fixed_latency_ms: float = 0.0
    offset_ms: float = 0.0
    poll_ms: float = 2.0
    #: wire mode: keep the camera's TriggerDelay matched between runs
    follow_speed: bool = True
    #: calibration: the camera's view along the belt, in mm
    view_mm: float = 0.0
    direction: str = DIR_RIGHT
    calibrate_frames: int = 10
    #: the built-in simulated PLC instead of a real one
    simulated: bool = False

    @classmethod
    def from_values(cls, values: Optional[Mapping[str, Any]]) -> "PlcSettings":
        out = cls()
        for f in fields(cls):
            if not values or f.name not in values:
                continue
            raw = values[f.name]
            try:
                if f.type in ("bool", bool):
                    value: Any = bool(raw)
                elif f.type in ("int", int):
                    value = int(raw)
                elif f.type in ("float", float):
                    value = float(raw)
                else:
                    value = str(raw)
            except (TypeError, ValueError):
                continue
            setattr(out, f.name, value)
        if out.link not in LINKS:
            out.link = LINK_TCP
        if out.parity not in PARITIES:
            out.parity = "None"
        out.stop_bits = 2 if out.stop_bits == 2 else 1
        out.unit = min(max(out.unit, 0), 247)
        if out.mode not in MODES:
            out.mode = MODE_WIRE
        if out.speed_source not in SPEEDS:
            out.speed_source = SPEED_FIXED
        if out.direction not in DIRECTIONS:
            out.direction = DIR_RIGHT
        out.poll_ms = min(max(out.poll_ms, 0.5), 100.0)
        out.calibrate_frames = min(max(out.calibrate_frames, 1), 200)
        if out.speed_scale <= 0:
            out.speed_scale = 1.0
        return out

    def values(self) -> Dict[str, Any]:
        return asdict(self)


def travel_s(distance_mm: float, speed_mm_s: Optional[float]
             ) -> Optional[float]:
    """Seconds from the photo-eye to the middle of the view, or None when
    the belt is stopped or the speed unknown."""
    if speed_mm_s is None or speed_mm_s <= 0:
        return None
    return max(0.0, float(distance_mm)) / float(speed_mm_s)


def camera_delay_us(settings: PlcSettings, speed_mm_s: Optional[float]
                    ) -> Optional[float]:
    """Wire mode: the camera's TriggerDelay in microseconds (never below 0;
    None while the speed is unknown or the wait absurd)."""
    t = travel_s(settings.distance_mm, speed_mm_s)
    if t is None:
        return None
    wait = t - settings.fixed_latency_ms / 1000.0 + settings.offset_ms / 1000.0
    if wait > MAX_WAIT_S:
        return None
    return max(0.0, wait) * 1e6


def fire_at(settings: PlcSettings, speed_mm_s: Optional[float],
            seen_at: float, poll_s: float, rtt_s: float) -> Optional[float]:
    """Network mode: when to fire (the clock of `seen_at`) for a bottle
    whose count rise was read back at `seen_at`, polling every `poll_s`
    with round trips of `rtt_s`. None while the speed is unknown."""
    t = travel_s(settings.distance_mm, speed_mm_s)
    if t is None or t > MAX_WAIT_S:
        return None
    crossed = seen_at - poll_s / 2.0 - rtt_s / 2.0
    return (crossed + t - settings.fixed_latency_ms / 1000.0
            + settings.offset_ms / 1000.0)


def speed_from_register(raw: int, scale: float) -> float:
    return float(raw) / float(scale or 1.0)


def count_step(before: Optional[int], now: int, limit: int = 1000) -> int:
    """New bottles between two reads of a 16-bit count: wraps at 65535; a
    count that went back (the PLC reset it) or jumped absurdly is 0."""
    if before is None:
        return 0
    step = (int(now) - int(before)) & 0xFFFF
    return step if step <= limit else 0


def correction_ms(offset_px: float, mm_per_px: float,
                  speed_mm_s: Optional[float]) -> Optional[float]:
    """Auto-calibration: a bottle seen `offset_px` past the middle of the
    view (positive: further along the belt's direction) was taken that
    much LATE; the milliseconds to add to offset_ms (negative: fire
    sooner). None when the speed or scale is unknown."""
    if not speed_mm_s or speed_mm_s <= 0 or mm_per_px <= 0:
        return None
    return -(offset_px * mm_per_px) / speed_mm_s * 1000.0


def bottle_check(bottles: Optional[int], triggers: Optional[int],
                 frames: Mapping[str, Optional[int]], per_trigger: int = 1,
                 presses: int = 0) -> Dict[str, Any]:
    """The run's bottles (from the PLC's count) against the triggers Typhon
    fired (network mode) and each camera's frames saved: the block written
    into the run's end record, with `missed` per camera (frames short of
    (bottles + presses) x per_trigger; 0 when none or unknown). `presses`:
    "Fire camera via PLC" presses the PLC took during the run, each one
    more picture on the same wire."""
    per = max(1, int(per_trigger or 1))
    presses = max(0, int(presses or 0))
    out: Dict[str, Any] = {"bottles": bottles, "triggers": triggers,
                           "presses": presses,
                           "frames_saved": dict(frames), "missed": {}}
    words = []
    for cam, saved in frames.items():
        if bottles is None or saved is None:
            out["missed"][cam] = None
            continue
        missed = max(0, (bottles + presses) * per - int(saved))
        out["missed"][cam] = missed
        if missed:
            extra = f" and {presses} press(es)" if presses else ""
            words.append(f"{cam}: {missed} frame(s) short of {bottles} "
                         f"bottle(s){extra}")
    if bottles is not None and triggers is not None and triggers < bottles:
        words.append(f"Typhon fired {triggers} of {bottles} bottles")
    out["ok"] = not words and bottles is not None
    out["words"] = "; ".join(words) if words else (
        f"{bottles} bottle(s), every one pictured" if bottles is not None
        else "the PLC's count could not be read")
    return out
