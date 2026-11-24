"""
typhon.plc — PLC trigger (Do-More): an AutomationDirect Do-More PLC
(programmed in Do-More Designer) tells Typhon when a part — a bottle on a
conveyor — passes a photo-eye, and Typhon makes sure the camera takes its
picture when the bottle is in the middle of the view, whatever the
latency (Andy, 24 November 2026; both modes picked the same day).

OPTIONAL
Built in, off in a new project (council_core/plugin_host.OPTIONAL_BUILTINS):
tick "PLC trigger (Do-More)" in Plugins… and its button opens its window.

TWO MODES (timing.py has the sums)
  Wire     the PLC's output is wired to the camera's trigger input (a
           Basler's Line, or CxpTrigger0 through the frame grabber). The
           run is Triggered: exact from that line, as today. This plugin
           reads the belt speed and sets the camera's TriggerDelay
           (capture.delay_us) so the exposure waits until the bottle has
           travelled from the photo-eye to the middle of the view; with
           "Keep the delay matched" it sets it again between runs when the
           speed changes. Microsecond timing, done by the camera.
  Network  no wire: the plugin offers a trigger source, "PLC (network)",
           under Trigger from. Its thread reads the PLC's bottle count
           every poll_ms; each rise is a bottle, fired (Capture now on the
           run's cameras) when it should be in the middle of the view.
           Timing spread: a poll and the PC's scheduling (a few ms).

THE PLC SIDE (docs/typhon_plc_trigger.md, the Do-More Designer sheet)
Modbus TCP, which Do-More's internal Ethernet port serves when "Enable
Modbus/TCP Server" is ticked — or Modbus RTU on its serial port (Andy, 24
November 2026), through pyserial (council_core/serial_port.py, reached by
typhon_plugin_api.builtin; optional). A COM port opens once, so the
window, the source and the run hooks share one serial client.
The ladder adds 1 to a holding register
(MHR1) per bottle and may keep the belt speed in another (MHR2, mm/s).
modbus.py is the client (standard library only); simulator.py a pretend
PLC and conveyor on 127.0.0.1 for trying it without hardware.

ALSO
  * Fire camera via PLC (Andy, 24 November 2026): the GUI writes 1 to the
    fire register (MHR3); one ladder rung pulses the camera's output and
    writes 0 back. A Basler listens to one trigger source, so during a
    run from the PLC's wire this is how Typhon takes an extra picture.
    Presses are counted in the missed-bottle check.
  * Auto-calibrate (council_core/bottle_position.py, through
    typhon_plugin_api.builtin): from the newest pictures, how far the
    bottle sits from the middle of the view; the delay is corrected by
    that distance over the speed.
  * Missed-bottle check (run hooks): the PLC's count at Start and at the
    end of the run against the frames each camera saved, written into the
    run's end record ("plugins" → "typhon.plc") and said when short.
"""
from __future__ import annotations

import heapq
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import typhon_plugin_api as api
from typhon_plugin_api import builtin

from . import timing
from .fan import FanRun, FanSettings, LOG_SUFFIX, wait_in_ui
from .modbus import ModbusClient, ModbusError, ModbusRtuClient, parse_address

PLUGIN_ID = "typhon.plc"
SOURCE_NAME = "PLC (network)"
DELAY_KEY = "capture.delay_us"
#: How often the window's readout (and the wire mode's follow) asks the PLC.
MONITOR_S = 0.5
#: The delay is set again when it is this far off (µs, or a fraction).
FOLLOW_US = 50.0
FOLLOW_FRACTION = 0.01
#: The camera's frame rate (the fan lock's free-running clock).
RATE_KEY = "AcquisitionFrameRate"
RATE_ON_KEY = "AcquisitionFrameRateEnable"
TRIGGER_MODE_KEY = "TriggerMode"
#: The reference profile, in the plugin's data folder (numbered when one
#: is there: never written over).
REFERENCE_FILE = "fan_reference.json"
#: Pictures Find hub reads.
HUB_PICTURES = 8


class PlcLink:
    """One connection to the PLC (real or simulated) and what it reads."""

    def __init__(self, settings: timing.PlcSettings, client: ModbusClient,
                 owns: bool = True) -> None:
        self.settings = settings
        self.client = client
        #: False for the serial port, which one link holds for everyone (a
        #: COM port opens once): closing this link leaves it open.
        self.owns = owns
        self.count_at = parse_address(settings.count_register)
        self.speed_at = (parse_address(settings.speed_register)
                         if settings.speed_source == timing.SPEED_PLC
                         else None)
        self.rtt_s = 0.0

    def close(self) -> None:
        if self.owns:
            self.client.close()

    def read_count(self) -> Tuple[int, float]:
        """(count, when it was read: time.perf_counter() at the reply)."""
        sent = time.perf_counter()
        value = self.client.read_one(self.count_at)
        got = time.perf_counter()
        rtt = got - sent
        self.rtt_s = rtt if self.rtt_s == 0 else 0.9 * self.rtt_s + 0.1 * rtt
        return value, got

    def read_speed(self) -> Optional[float]:
        """mm/s: the fixed speed, or the PLC's register over its scale."""
        if self.speed_at is None:
            return self.settings.speed_mm_s
        raw = self.client.read_one(self.speed_at)
        return timing.speed_from_register(raw, self.settings.speed_scale)


def _pause(stop: threading.Event, seconds: float) -> None:
    """Wait up to `seconds` (or until `stop`), to the millisecond on
    Windows too: there an Event's wait and time.monotonic tick every
    ~15.6 ms (measured on the CI runner: bottles fired 13-29 ms late),
    while time.sleep and time.perf_counter are fine-grained (Python 3.11+).
    Long waits use the Event, so stop is still seen at once; the last
    20 ms are slept in 1 ms steps."""
    if seconds > 0.02:
        stop.wait(min(seconds - 0.02, 0.05))
    elif not stop.is_set():
        time.sleep(min(seconds, 0.001))


class PlcSource:
    """The network mode's trigger source for one run: reads the bottle
    count every poll and fires each bottle at its time (a heap of times;
    one thread does both). Repeated start/stop are harmless; once
    stopped it stays stopped (the TriggerSource contract)."""

    camera_line = False

    def __init__(self, name: str, make_link: Callable[[], PlcLink],
                 settings: timing.PlcSettings,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.name = name
        self._make_link = make_link
        self.settings = settings
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self.fired = 0
        self.late = 0
        self.bottles = 0
        self.merged = 0
        self.error = ""
        self.fire_times: List[float] = []

    def needs(self, mode: str = api.MODE_SNAPSHOT) -> frozenset:
        if str(mode) == api.MODE_EXACT:
            return frozenset({"frames", "software_trigger"})
        return frozenset({"frames"})

    def start(self, fire: Callable[[api.TriggerEvent], None]) -> None:
        if self._started or self._stop.is_set():
            return
        self._started = True
        link = self._make_link()          # raises: the plugin says why
        self._thread = threading.Thread(target=self._run, args=(link, fire),
                                        daemon=True, name="plc-source")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)

    def _run(self, link: PlcLink, fire: Callable[[api.TriggerEvent], None]
             ) -> None:
        poll = self.settings.poll_ms / 1000.0
        pending: List[float] = []
        last: Optional[int] = None
        speed: Optional[float] = None
        speed_at = 0.0
        next_poll = self._clock()
        try:
            while not self._stop.is_set():
                now = self._clock()
                while pending and pending[0] <= now:
                    heapq.heappop(pending)
                    self.fired += 1
                    self.fire_times.append(now)
                    fire(api.TriggerEvent(source=self.name,
                                          number=self.fired,
                                          at=time.time()))
                if now >= next_poll:
                    try:
                        if speed is None or now - speed_at > 0.25:
                            speed = link.read_speed()
                            speed_at = now
                        count, seen = link.read_count()
                        self.error = ""
                    except ModbusError as exc:
                        self.error = str(exc)
                        next_poll = now + 0.2
                        continue
                    step = timing.count_step(last, count)
                    last = count
                    if step:
                        self.bottles += step
                        if step > 1:
                            self.merged += step - 1
                        when = timing.fire_at(self.settings, speed, seen,
                                              poll, link.rtt_s)
                        if when is None:
                            when = seen
                        if when < self._clock():
                            self.late += step
                        for _ in range(step):
                            heapq.heappush(pending, when)
                    next_poll = max(next_poll + poll, now)
                wake = next_poll if not pending else min(next_poll,
                                                         pending[0])
                wait = wake - self._clock()
                if wait > 0:
                    _pause(self._stop, wait)
        finally:
            link.close()

    def summary(self) -> Dict[str, Any]:
        return {"bottles_seen": self.bottles, "triggers": self.fired,
                "late": self.late, "merged": self.merged,
                "error": self.error}


class PlcPlugin:
    """PLC trigger (Do-More): source plugin (rig scope) with a window and
    run hooks."""

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None
        self.settings = timing.PlcSettings()
        self.sim: Any = None
        self._lock = threading.Lock()
        self._monitor: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._link: Optional[PlcLink] = None
        self._serial: Optional[ModbusRtuClient] = None
        self.reading: Dict[str, Any] = {"connected": False, "error": "",
                                        "count": None, "speed": None,
                                        "delay_us": None}
        self.sources_made: List[PlcSource] = []
        self._run_start: Dict[str, Any] = {}
        self.last_said = ""
        #: "Fire camera via PLC" presses the PLC took (this session)
        self.presses = 0
        #: the fan-blade strobe lock (fan.py, strobe.py)
        self.fan = FanSettings()
        self.fan_run: Optional[FanRun] = None
        self._fan_handle: Any = None
        self._fan_folder: Any = None

    # -- life ---------------------------------------------------------------
    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx
        try:
            self.settings = timing.PlcSettings.from_values(ctx.settings())
        except Exception:                                 # noqa: BLE001
            self.settings = timing.PlcSettings()
        try:
            self.fan = FanSettings.from_flat(ctx.settings())
            self.fan.reference = self._load_reference(self.fan.reference_file)
        except Exception:                                 # noqa: BLE001
            self.fan = FanSettings()
        if self._set_up():
            self.start_monitor()

    def deactivate(self) -> None:
        self._stop_fan()
        self.stop_monitor()
        self._stop_sim()
        self.ctx = None

    # -- settings -----------------------------------------------------------
    def update_settings(self, values: Dict[str, Any]) -> str:
        """New settings from the window: kept in the project, the link made
        again. What went wrong saving, or ""."""
        merged = self.settings.values()
        merged.update(values)
        new = timing.PlcSettings.from_values(merged)
        reconnect = any(getattr(new, k) != getattr(self.settings, k)
                        for k in ("link", "host", "port", "unit",
                                  "serial_port", "baud", "parity",
                                  "stop_bits", "count_register",
                                  "speed_register", "speed_source",
                                  "simulated"))
        self.settings = new
        why = self._save()
        if new.simulated and self.sim is not None:
            self.sim.set_speed(new.speed_mm_s)
        if reconnect:
            self.stop_monitor()
            if not new.simulated:
                self._stop_sim()
            if self._set_up():
                self.start_monitor()
        return why

    def _save(self) -> str:
        """Keep the PLC's and the fan's settings in the project. What went
        wrong, or ""."""
        if self.ctx is None:
            return ""
        try:
            self.ctx.save_settings({**self.settings.values(),
                                    **self.fan.flat()})
        except Exception as exc:                          # noqa: BLE001
            return str(exc)
        return ""

    def update_fan(self, values: Dict[str, Any]) -> str:
        """New fan settings from the window (kept in the project)."""
        merged = self.fan.values()
        merged.update(values)
        self.fan = FanSettings.from_values(merged)
        return self._save()

    def _set_up(self) -> bool:
        s = self.settings
        serial = s.link == timing.LINK_SERIAL
        return bool(s.simulated or (s.serial_port if serial else s.host))

    def _ensure_sim(self) -> Any:
        with self._lock:
            if self.sim is None:
                from .simulator import SimulatedPlc

                count = parse_address(self.settings.count_register)
                try:
                    speed = parse_address(self.settings.speed_register)
                except ValueError:
                    speed = parse_address("MHR2")
                sim = SimulatedPlc(speed_mm_s=self.settings.speed_mm_s,
                                   count_offset=count.offset,
                                   speed_offset=speed.offset)
                try:
                    sim.fire_offset = parse_address(
                        self.settings.fire_register).offset
                except ValueError:
                    pass
                self.sim = sim.start()
            return self.sim

    def _stop_sim(self) -> None:
        with self._lock:
            sim, self.sim = self.sim, None
        if sim is not None:
            sim.stop()

    def make_link(self) -> PlcLink:
        """A link to the PLC: over Ethernet a connection of its own (the
        network source's reads never wait behind the window's); over a
        serial port the one port everyone shares."""
        s = self.settings
        if s.link == timing.LINK_SERIAL:
            return PlcLink(s, self._serial_client(), owns=False)
        if s.simulated:
            port = self._ensure_sim().port
            return PlcLink(s, ModbusClient("127.0.0.1", port, s.unit))
        return PlcLink(s, ModbusClient(s.host, s.port, s.unit))

    def _serial_client(self) -> ModbusRtuClient:
        sim = self._ensure_sim() if self.settings.simulated else None
        with self._lock:
            if self._serial is None:
                s = self.settings
                if sim is not None:
                    opener = sim.rtu_port
                    name = "the simulated serial port"
                else:
                    def opener() -> Any:
                        return builtin.open_serial_port(
                            s.serial_port, s.baud, s.parity, s.stop_bits)
                    name = s.serial_port or "the serial port"
                self._serial = ModbusRtuClient(opener, s.unit, name)
            return self._serial

    def serial_ports(self) -> List[str]:
        """This PC's serial ports now (none listed without pyserial)."""
        try:
            return list(builtin.serial_ports())
        except Exception:                                 # noqa: BLE001
            return []

    def _close_serial(self) -> None:
        with self._lock:
            client, self._serial = self._serial, None
        if client is not None:
            client.close()

    # -- the trigger source (network mode) ------------------------------------
    def sources(self) -> List[str]:
        if self.settings.mode == timing.MODE_NETWORK and self._set_up():
            return [SOURCE_NAME]
        return []

    def make_source(self, name: str) -> PlcSource:
        made = PlcSource(name, self.make_link, self.settings)
        self.sources_made.append(made)
        return made

    # -- the monitor: the window's readout and the wire mode's follow --------
    def start_monitor(self) -> None:
        if self._monitor is not None:
            return
        if self.settings.simulated:
            self._ensure_sim()
        self._monitor_stop = threading.Event()
        t = threading.Thread(target=self._watch, args=(self._monitor_stop,),
                             daemon=True, name="plc-monitor")
        self._monitor = t
        t.start()

    def stop_monitor(self) -> None:
        self._monitor_stop.set()
        t, self._monitor = self._monitor, None
        if t is not None and t is not threading.current_thread():
            t.join(2.0)
        link, self._link = self._link, None
        if link is not None:
            link.close()
        self._close_serial()

    def read_now(self) -> Dict[str, Any]:
        """Read the PLC once (count, speed, the delay they make)."""
        out: Dict[str, Any] = {"connected": False, "error": "",
                               "count": None, "speed": None,
                               "delay_us": None}
        try:
            if self._link is None:
                self._link = self.make_link()
            link = self._link
            out["count"] = link.read_count()[0]
            out["speed"] = link.read_speed()
            out["connected"] = True
            out["timer"] = self._read_timer(link)
            out["rtt_ms"] = round(link.rtt_s * 1000.0, 2)
        except (ModbusError, ValueError, OSError) as exc:
            out["error"] = str(exc)
            if self.settings.speed_source == timing.SPEED_FIXED:
                out["speed"] = self.settings.speed_mm_s
        out["delay_us"] = timing.camera_delay_us(self.settings, out["speed"])
        self.reading = out
        return out

    def _read_timer(self, link: PlcLink) -> Optional[bool]:
        """True when the PLC's timer triggers the camera, False when the
        computer does, None when unknown."""
        try:
            at = parse_address(self.settings.timer_register)
            return int(link.client.read_one(at)) == 0
        except (ModbusError, ValueError, OSError):
            return None

    def _watch(self, stop: threading.Event) -> None:
        while not stop.is_set():
            out = self.read_now()
            if (self.settings.mode == timing.MODE_WIRE
                    and self.settings.follow_speed and out["connected"]
                    and self._should_follow(out["delay_us"])):
                ctx = self.ctx
                if ctx is not None:
                    ctx.call_in_ui(lambda: self.apply_delay(quiet=True))
            stop.wait(MONITOR_S)

    def applied_us(self) -> Optional[float]:
        """The active camera's capture.delay_us now, or None."""
        ctx = self.ctx
        if ctx is None:
            return None
        try:
            setup = ctx.capture_setup()
        except Exception:                                 # noqa: BLE001
            return None
        value = getattr(setup, "delay_us", None)
        return None if value is None else float(value)

    def _should_follow(self, delay_us: Optional[float]) -> bool:
        if delay_us is None:
            return False
        now = self.applied_us()
        if now is None:
            return self.ctx is not None and bool(self.ctx.active_camera())
        return abs(delay_us - now) > max(FOLLOW_US, FOLLOW_FRACTION *
                                         max(delay_us, 1.0))

    # -- wire mode: the camera's TriggerDelay -------------------------------
    def apply_delay(self, quiet: bool = False) -> str:
        """Set the active camera's trigger delay from the speed now. What
        happened, in words (on the UI thread: frame_camera's rule)."""
        delay = self.reading.get("delay_us")
        if delay is None:
            delay = self.read_now().get("delay_us")
        if delay is None:
            return self._said("No delay to set: the belt speed is unknown or "
                              "zero.", quiet)
        if self.ctx is None or not self.ctx.active_camera():
            return self._said("Connect a camera first.", quiet)
        try:
            out = builtin.set_camera_setting(DELAY_KEY, round(delay, 1))
        except Exception as exc:                          # noqa: BLE001
            return self._said(f"The camera's trigger delay was not set: "
                              f"{exc}", quiet)
        took = out.get("value", delay) if isinstance(out, dict) else delay
        return self._said(f"Trigger delay set to {float(took) / 1000.0:.3f} "
                          f"ms on {self.ctx.active_camera()}.", quiet)

    # -- the GUI fires the camera through the PLC (Andy, 24 November 2026) --
    def fire_via_plc(self) -> str:
        """Ask the PLC to pulse the camera's output once: write 1 to the
        fire register; the ladder pulses the output and writes 0 back. The
        camera keeps listening to its wire (CxpTrigger0, Line1), so this
        works in the middle of a PLC run, with the same trigger delay as a
        bottle. What happened, in words. Waits on the PLC (a Modbus write,
        a few ms): call it from a button, not a timer."""
        if not self._set_up():
            return self._said("Set the PLC's address or serial port first.",
                              False)
        try:
            at = parse_address(self.settings.fire_register)
        except ValueError as exc:
            return self._said(f"Fire register: {exc}", False)
        taken = {self.settings.count_register.upper(),
                 self.settings.speed_register.upper()}
        if at.text in {t.replace(" ", "") for t in taken}:
            return self._said(f"Fire register {at.text} is already the "
                              f"bottle count or speed register: pick its "
                              f"own (MHR3).", False)
        if not at.writable:
            return self._said(f"Fire register {at.text} is an input "
                              f"register: pick a holding register (MHR3).",
                              False)
        try:
            if self._link is None:
                self._link = self.make_link()
            client = self._link.client
            pending = client.read_one(at)
            client.write(at, [1])
        except (ModbusError, OSError) as exc:
            return self._said(f"The PLC did not take the trigger: {exc}",
                              False)
        self.presses += 1
        note = (" (the last press was still waiting: is the fire rung in "
                "the ladder?)" if pending else "")
        return self._said(f"Trigger sent through the PLC ({at.text}).{note}",
                          False)

    def _said(self, text: str, quiet: bool) -> str:
        self.last_said = text
        if not quiet and self.ctx is not None:
            self.ctx.say(text)
        return text

    # -- auto-calibration ----------------------------------------------------
    def calibrate(self, camera: Optional[str] = None) -> str:
        """Correct offset_ms from the newest pictures of `camera` (the
        active one). Slow (reads pictures): call it off the UI thread."""
        ctx = self.ctx
        if ctx is None:
            return "The plugin is switched off."
        s = self.settings
        folder = ctx.capture_folder(camera)
        paths = builtin.latest_pictures(folder, s.calibrate_frames)
        if not paths:
            return ("No pictures to calibrate from: run a triggered capture "
                    "of a few bottles first.")
        result = builtin.bottle_calibrate(paths, s.direction)
        if result.median_px is None:
            return (f"No bottle stood out in {len(paths)} picture(s) "
                    f"({'; '.join(result.skipped[:2])}). Use a plain belt "
                    f"or set the offset by hand.")
        if s.view_mm <= 0:
            return ("Set 'View along belt (mm)' first: how many mm of belt "
                    "the picture spans.")
        speed = self.reading.get("speed") or self.read_now().get("speed")
        fix = timing.correction_ms(result.median_px,
                                   s.view_mm / max(result.width_px, 1),
                                   speed)
        if fix is None:
            return "No correction: the belt speed is unknown or zero."
        self.update_settings({"offset_ms": round(s.offset_ms + fix, 3)})
        spread = result.spread_px
        return (f"Bottles sat {result.median_px:+.0f} px from the middle in "
                f"{len(result.offsets_px)} picture(s)"
                + (f" (spread {spread:.0f} px)" if spread is not None else "")
                + f"; offset now {self.settings.offset_ms:+.3f} ms. "
                  f"Apply the delay, then run again to check.")

    # -- who triggers the camera: the PLC's timer or the computer -----------
    def set_timer(self, timer_on: bool) -> str:
        """Write the timer register: 0 hands the camera back to the PLC's
        timer, 1 stops the timer so the computer triggers (Fire camera via
        PLC, or the fan lock's free-running camera). Only ever by the
        window's switch: Typhon never turns the timer back on by itself
        (Andy, 24 November 2026: the timer is the backup)."""
        if not self._set_up():
            return self._said("Set the PLC's address or serial port first.",
                              False)
        try:
            at = parse_address(self.settings.timer_register)
        except ValueError as exc:
            return self._said(f"Timer register: {exc}", False)
        used = {parse_address(r).text for r in
                (self.settings.count_register, self.settings.speed_register,
                 self.settings.fire_register) if _parses(r)}
        if at.text in used or not at.writable:
            return self._said(f"Timer register {at.text} must be a holding "
                              f"register of its own (MHR5).", False)
        try:
            if self._link is None:
                self._link = self.make_link()
            self._link.client.write(at, [0 if timer_on else 1])
            now = self._read_timer(self._link)
        except (ModbusError, OSError) as exc:
            return self._said(f"The PLC did not take it: {exc}", False)
        self.reading["timer"] = now
        if now is None or now != timer_on:
            return self._said(f"Wrote {at.text}, but the PLC reads back "
                              f"otherwise: is the timer rung in the ladder?",
                              False)
        return self._said("The PLC's timer triggers the camera again."
                          if timer_on else
                          "The PLC's timer is off: the computer triggers "
                          "the camera.", False)

    # -- the fan-blade strobe lock (Andy, 24 November 2026) -----------------
    def _load_reference(self, name: str) -> List[float]:
        """The reference profile kept in the plugin's data folder."""
        if not name or self.ctx is None:
            return []
        try:
            data = json.loads((self.ctx.data_dir() / name).read_text(
                encoding="utf-8"))
            return [float(v) for v in data.get("profile", [])]
        except (OSError, ValueError, TypeError, AttributeError):
            return []

    def fan_camera(self) -> str:
        """Set the active camera to free-run at the fan's frame rate:
        TriggerMode Off, the frame rate limit on, at rpm/60/K. Before a
        run (the trigger cannot change mid-run). On the UI thread."""
        f = self.fan
        if f.rpm <= 0:
            return self._said("Enter the fan's speed (rpm) first.", False)
        if self.ctx is None or not self.ctx.active_camera():
            return self._said("Connect a camera first.", False)
        done, problems = [], []
        for key, value in ((TRIGGER_MODE_KEY, "Off"), (RATE_ON_KEY, True),
                           (RATE_KEY, round(f.rate, 6))):
            try:
                out = builtin.set_camera_setting(key, value)
            except Exception as exc:                      # noqa: BLE001
                problems.append(f"{key}: {exc}")
                continue
            took = out.get("value", value) if isinstance(out, dict) else value
            done.append(f"{key} {took}")
        words = (f"Fan {f.rpm:g} rpm, {f.turns} turn(s) per picture: "
                 + ", ".join(done))
        if problems:
            words += ". Not set: " + "; ".join(problems)
        return self._said(words + ".", False)

    def fan_hub(self, camera: Optional[str] = None) -> str:
        """Find the hub and the ring from the newest pictures of the
        spinning fan. Slow (reads pictures): off the UI thread."""
        ctx = self.ctx
        if ctx is None:
            return "The plugin is switched off."
        paths = builtin.latest_pictures(ctx.capture_folder(camera),
                                        HUB_PICTURES)
        try:
            x, y, r_in, r_out = builtin.find_hub(paths)
        except Exception as exc:                          # noqa: BLE001
            return (f"No hub found: {exc}. Record a few seconds of the "
                    f"spinning fan first (any frame rate).")
        self.update_fan({"hub_x": round(x, 1), "hub_y": round(y, 1),
                         "ring_in": round(r_in, 1),
                         "ring_out": round(r_out, 1)})
        return (f"Hub at ({x:.0f}, {y:.0f}), ring {r_in:.0f}–{r_out:.0f} px "
                f"from {len(paths)} pictures. Now pick the reference.")

    def fan_reference(self, path: Any = None) -> str:
        """Take the reference from `path` (the window passes the frame the
        slider shows; None: the newest picture): where the blade should
        be. Reads a picture: off the UI thread."""
        ctx = self.ctx
        if path is None and ctx is not None:
            found = builtin.latest_pictures(ctx.capture_folder(), 1)
            path = found[-1] if found else None
        if path is None:
            return "No picture to take the reference from: record one."
        try:
            profile = builtin.blade_reference(path, self.fan.fan_view())
        except Exception as exc:                          # noqa: BLE001
            return f"No reference: {exc}."
        name = getattr(path, "name", str(path))
        kept = ""
        if ctx is not None:
            data = json.dumps({"picture": str(path), "profile": profile,
                               "geometry": self.fan.geometry()})
            try:
                kept = ctx.write_new(ctx.data_dir(), REFERENCE_FILE,
                                     data.encode("utf-8")).name
            except Exception as exc:                      # noqa: BLE001
                return f"The reference was not kept: {exc}."
        self.update_fan({"reference": profile, "reference_name": name,
                         "reference_geometry": self.fan.geometry(),
                         "reference_file": kept})
        return f"Reference: {name}. The lock holds the blade where it is here."

    def _set_rate(self, camera: str, rate: float) -> float:
        ctx = self.ctx
        if ctx is None:
            raise RuntimeError("the plugin is switched off")

        def ui() -> Any:
            if ctx.active_camera() != camera:
                raise RuntimeError(f"the active camera is no longer "
                                   f"{camera}")
            return builtin.set_camera_setting(RATE_KEY, round(rate, 6))

        out = wait_in_ui(ctx.call_in_ui, ui)
        took = out.get("value", rate) if isinstance(out, dict) else rate
        return float(took)

    def _start_fan(self, run: api.RunInfo) -> None:
        self._stop_fan()
        f = self.fan
        if not f.enabled or self.ctx is None:
            return
        why = f.problem()
        if why:
            self.ctx.say(f"Fan lock off this run: {why}.")
            return
        camera = self.ctx.active_camera() or ""
        folder = run.folders.get(camera)
        if folder is None and run.folders:
            camera, folder = next(iter(run.folders.items()))
        if folder is None:
            return
        handle = None
        try:
            handle = self.ctx.create_file(folder, f"{run.run}{LOG_SUFFIX}")
        except Exception:                                 # noqa: BLE001
            handle = None            # kept in memory, written at the end
        self._fan_handle, self._fan_folder = handle, folder
        self.fan_run = FanRun(folder, run.run, f,
                              lambda r: self._set_rate(camera, r), handle)
        self.fan_run.start()

    def _stop_fan(self) -> Optional[Dict[str, Any]]:
        fr, self.fan_run = self.fan_run, None
        if fr is None:
            return None
        fr.stop()
        handle, self._fan_handle = self._fan_handle, None
        if handle is not None:
            try:
                handle.close()
            except (OSError, ValueError):
                pass
        elif self.ctx is not None and self._fan_folder is not None:
            try:
                self.ctx.write_new(self._fan_folder,
                                   f"{fr.run}{LOG_SUFFIX}", fr.csv_bytes())
            except Exception:                             # noqa: BLE001
                pass
        return fr.summary()

    # -- run hooks: the missed-bottle check ----------------------------------
    def _count_now(self) -> Optional[int]:
        if not self._set_up():
            return None
        try:
            if self._link is None:
                self._link = self.make_link()
            return self._link.read_count()[0]
        except (ModbusError, ValueError, OSError):
            return None

    def on_run_started(self, run: api.RunInfo) -> None:
        self._start_fan(run)
        if not self._set_up():
            self._run_start = {}
            return
        self._run_start = {"run": run.run, "count": self._count_now(),
                           "speed": self.reading.get("speed"),
                           "made": len(self.sources_made),
                           "presses": self.presses}

    def on_run_finished(self, run: api.RunInfo) -> Optional[Dict[str, Any]]:
        fan = self._stop_fan()
        out = self._bottle_block(run)
        if fan is not None:
            if fan.get("words") and self.ctx is not None:
                self.ctx.say(f"Fan lock: {fan['words']}")
            out = dict(out or {})
            out["fan"] = fan
        return out

    def _bottle_block(self, run: api.RunInfo) -> Optional[Dict[str, Any]]:
        start = self._run_start
        if not start or start.get("run") != run.run:
            return None
        self._run_start = {}
        if start.get("count") is None:
            return {"words": "the PLC's count could not be read at Start"}
        end = self._count_now()
        bottles = None if end is None else timing.count_step(
            start["count"], end, limit=65535)
        source = next((s for s in self.sources_made[start["made"]:]
                       if s.bottles or s.fired), None)
        triggers = source.fired if source is not None else None
        frames: Dict[str, Optional[int]] = {}
        per = 1
        for cam in run.cameras:
            counts = run.counts.get(cam) or {}
            saved = counts.get("frames_saved")
            frames[cam] = None if saved is None else int(saved)
            try:
                setup = self.ctx.capture_setup(cam) if self.ctx else None
                per = max(per, int(getattr(setup, "per_trigger", 1) or 1))
            except Exception:                             # noqa: BLE001
                pass
        presses = self.presses - int(start.get("presses") or 0)
        out = timing.bottle_check(bottles, triggers, frames, per, presses)
        out["mode"] = self.settings.mode
        out["speed_mm_s"] = {"start": start.get("speed"),
                             "end": self.reading.get("speed")}
        out["delay_us"] = timing.camera_delay_us(self.settings,
                                                 start.get("speed"))
        if source is not None:
            out["network"] = source.summary()
        if not out["ok"] and self.ctx is not None:
            self.ctx.say(out["words"])
        return out

    # -- the window -----------------------------------------------------------
    def panel(self, parent: Any = None) -> Any:
        from .panel import PlcPanel
        return PlcPanel(self, parent)


def _parses(text: str) -> bool:
    try:
        parse_address(text)
    except ValueError:
        return False
    return True
