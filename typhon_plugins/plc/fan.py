"""
The fan-blade strobe lock during a run (docs/typhon_plc_trigger.md 9): its
settings, and the thread that follows the run's frames CSV as pictures are
saved, measures the blade in each (council_core.blade_phase, through
typhon_plugin_api.builtin), feeds strobe.Lock, and sets the camera's frame
rate when the lock asks.

Pictures are read from disk because that is where a run's pictures are
(the window does not hand plugins live frames yet). One 65 MP PNG takes
about a second to read, so when the run saves faster than that the thread
measures the NEWEST picture and logs the ones it skipped as "behind": the
lock works on frame numbers, so a skipped picture costs only time.

Stdlib only (a built-in plugin's rule).
"""
from __future__ import annotations

import csv
import io
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from typhon_plugin_api import builtin

from . import strobe

#: The fan's keys in the project's settings of the plugin.
PREFIX = "fan_"
#: The phase log beside the run's frames (the manifest's "files").
LOG_SUFFIX = "_blade_phase.csv"
LOG_COLUMNS = ("file", "index", "measured", "turn_deg", "error_deg",
               "score", "in_tolerance", "rate_fps", "note")
#: How often the thread looks for new rows.
LOOK_S = 0.1
#: How long a rate change may wait for the UI thread.
SET_WAIT_S = 3.0


@dataclass
class FanSettings:
    """The window's "Fan blade" section, kept in the project beside the
    PLC's settings (under "fan")."""
    enabled: bool = False
    rpm: float = 1200.0
    max_fps: float = 1.0
    blades: int = 1
    any_blade: bool = True
    view: str = strobe.VIEW_FACE
    hub_x: float = 0.0
    hub_y: float = 0.0
    ring_in: float = 0.0
    ring_out: float = 0.0
    box_x: int = 0
    box_y: int = 0
    box_w: int = 0
    box_h: int = 0
    axis: str = strobe.AXIS_X
    tolerance_deg: float = 1.0
    target_deg: float = 0.0
    gain: float = 0.6
    fit_frames: int = 5
    #: side-on pixels per degree; 0 = measure it at the start of each run
    px_per_deg: float = 0.0
    #: the reference picture's profile and what it was taken with; the
    #: profile is kept in the plugin's data folder (reference_file: a
    #: project's settings hold plain values only)
    reference: List[float] = field(default_factory=list)
    reference_name: str = ""
    reference_geometry: str = ""
    reference_file: str = ""

    @classmethod
    def from_values(cls, values: Optional[Mapping[str, Any]]
                    ) -> "FanSettings":
        out = cls()
        for f in fields(cls):
            if not values or f.name not in values:
                continue
            raw = values[f.name]
            try:
                if f.name == "reference":
                    value: Any = [float(v) for v in (raw or [])]
                elif f.type in ("bool", bool):
                    value = bool(raw)
                elif f.type in ("int", int):
                    value = int(raw)
                elif f.type in ("float", float):
                    value = float(raw)
                else:
                    value = str(raw)
            except (TypeError, ValueError):
                continue
            setattr(out, f.name, value)
        if out.view not in strobe.VIEWS:
            out.view = strobe.VIEW_FACE
        if out.axis not in strobe.AXES:
            out.axis = strobe.AXIS_X
        out.blades = min(max(out.blades, 1), 64)
        out.fit_frames = min(max(out.fit_frames, 2), 50)
        out.gain = min(max(out.gain, 0.05), 1.0)
        out.max_fps = max(out.max_fps, 0.01)
        return out

    def values(self) -> Dict[str, Any]:
        return asdict(self)

    def flat(self) -> Dict[str, Any]:
        """As the project keeps them: "fan_<name>", the profile left out."""
        return {f"{PREFIX}{k}": v for k, v in self.values().items()
                if k != "reference"}

    @classmethod
    def from_flat(cls, values: Optional[Mapping[str, Any]]
                  ) -> "FanSettings":
        return cls.from_values({k[len(PREFIX):]: v
                                for k, v in (values or {}).items()
                                if str(k).startswith(PREFIX)})

    @property
    def turns(self) -> int:
        return strobe.turns_per_picture(self.rpm, self.max_fps)

    @property
    def rate(self) -> float:
        return strobe.frame_rate(self.rpm, self.turns)

    @property
    def pitch_deg(self) -> float:
        return 360.0 / max(1, self.blades) if self.any_blade else 360.0

    def geometry(self) -> str:
        """What the reference depends on: taken again when it changes."""
        if self.view == strobe.VIEW_FACE:
            return (f"face {self.hub_x:.1f},{self.hub_y:.1f} "
                    f"{self.ring_in:.1f}-{self.ring_out:.1f} "
                    f"{self.blades if self.any_blade else 1}")
        return (f"side {self.box_x},{self.box_y},{self.box_w},{self.box_h} "
                f"{self.axis}")

    def fan_view(self) -> Any:
        return builtin.FanView(
            view=self.view, hub_x=self.hub_x, hub_y=self.hub_y,
            ring_in=self.ring_in, ring_out=self.ring_out,
            blades=self.blades, any_blade=self.any_blade,
            box=(self.box_x, self.box_y, self.box_w, self.box_h),
            axis=self.axis)

    def problem(self) -> str:
        """Why a run cannot lock, in words, or ""."""
        if self.rpm <= 0:
            return "enter the fan's speed (rpm)"
        why = self.fan_view().problem()
        if why:
            return why
        if not self.reference:
            return ("pick a reference picture (the blade where you want it)"
                    ": Use this frame as reference")
        if self.reference_geometry != self.geometry():
            return ("the hub, ring, box or blade count changed since the "
                    "reference was taken: take the reference again")
        return ""

    def make_lock(self, rate: Optional[float] = None) -> strobe.Lock:
        side = self.view == strobe.VIEW_SIDE
        return strobe.Lock(
            rate=rate or self.rate, turns=self.turns,
            pitch_deg=self.pitch_deg, target=self.target_deg,
            tolerance_deg=self.tolerance_deg, gain=self.gain,
            fit_frames=self.fit_frames, side=side,
            px_per_deg=(self.px_per_deg or None) if side else 1.0,
            max_jump=(max(self.box_w if self.axis == strobe.AXIS_X
                          else self.box_h, 1) / 4.0) if side else 0.0)


class FanRun:
    """One camera's lock for one run.

    folder, run   where the run saves, and its name
    settings      FanSettings (copied: the window may change them)
    set_rate      sets the camera's frame rate, answers what it took
                  (or raises); called from this thread
    log           an open text file for the phase log, or None (rows are
                  kept and handed out by `rows()`)
    measure       (path, view, reference) -> Measure; the builtin's by
                  default
    """

    def __init__(self, folder: Path, run: str, settings: FanSettings,
                 set_rate: Callable[[float], float], log: Any = None,
                 measure: Optional[Callable[..., Any]] = None,
                 rate: Optional[float] = None) -> None:
        self.folder = Path(folder)
        self.run = run
        self.settings = FanSettings.from_values(settings.values())
        self.view = self.settings.fan_view()
        self.lock = self.settings.make_lock(rate)
        self.set_rate = set_rate
        self.log = log
        self.measure = measure or builtin.blade_measure
        self._rows: List[Dict[str, Any]] = []
        self._writer = csv.writer(log) if log is not None else None
        if self._writer is not None:
            self._writer.writerow(LOG_COLUMNS)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending = ""
        self._seen_rows = 0
        self.behind = 0
        self.missing = 0
        self.failed_sets = 0
        self.words: List[str] = []

    # -- life ---------------------------------------------------------------
    def start(self) -> None:
        t = threading.Thread(target=self._loop, daemon=True,
                             name="plc-fan-lock")
        self._thread = t
        t.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        # the pictures saved while stopping: measured, not acted on
        try:
            self.step(act=False)
        except Exception:                                 # noqa: BLE001
            pass
        if self.log is not None:
            try:
                self.log.flush()
            except (OSError, ValueError):
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:                      # noqa: BLE001
                self._say(f"the fan lock stopped: {exc}")
                return
            self._stop.wait(LOOK_S)

    def _say(self, text: str) -> None:
        if text and text not in self.words:
            self.words.append(text)

    # -- one look at the frames CSV --------------------------------------------
    def new_rows(self) -> List[Dict[str, str]]:
        """Rows of the run's frames CSV not yet seen (whole lines only)."""
        index = self.folder / f"{self.run}{INDEX_SUFFIX}"
        try:
            with open(index, "rb") as handle:
                data = handle.read()
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        text = data[:end + 1].decode("utf-8", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        out = rows[self._seen_rows:]
        self._seen_rows = len(rows)
        return out

    def step(self, act: bool = True) -> int:
        """Measure what is new; answers how many pictures were measured."""
        rows = [r for r in self.new_rows() if r.get("file")]
        if not rows:
            return 0
        # Behind: log the older ones as skipped, measure the newest.
        for r in rows[:-1]:
            self.behind += 1
            self._log(r, None, "behind")
        row = rows[-1]
        path = self.folder / row["file"]
        if not path.is_file():
            self.missing += 1
            self._say("pictures are not PNG files yet (raw recording): the "
                      "lock needs Typhon to save PNGs")
            self._log(row, None, "no picture")
            return 0
        try:
            index = int(float(row.get("index") or 0))
        except ValueError:
            index = self.lock.last_index + 1
        m = self.measure(path, self.view, self.settings.reference)
        new = self.lock.add(index, float(m.value), float(m.score))
        self._log(row, m, "")
        if new is not None and act:
            self._set(new)
        if self.lock.said:
            self._say(self.lock.said)
            self.lock.said = ""
        return 1

    def _set(self, rate: float) -> None:
        try:
            took = self.set_rate(rate)
        except Exception as exc:                          # noqa: BLE001
            self.failed_sets += 1
            self._say(f"the camera's frame rate was not changed: {exc}")
            return
        self.lock.took(float(took) if took else rate)

    def _log(self, row: Mapping[str, str], m: Any, note: str) -> None:
        err = None if m is None else self.lock.error_deg(float(m.value))
        rec = {"file": row.get("file", ""), "index": row.get("index", ""),
               "measured": "" if m is None else 1,
               "turn_deg": "" if m is None else round(float(m.value), 4),
               "error_deg": "" if err is None else round(err, 4),
               "score": "" if m is None else round(float(m.score), 4),
               "in_tolerance": ("" if err is None else
                                int(abs(err) <= self.settings.tolerance_deg)),
               "rate_fps": round(self.lock.rate, 6), "note": note}
        self._rows.append(rec)
        if self._writer is not None:
            try:
                self._writer.writerow([rec[c] for c in LOG_COLUMNS])
            except (OSError, ValueError):
                self._writer = None

    def rows(self) -> List[Dict[str, Any]]:
        return list(self._rows)

    def csv_bytes(self) -> bytes:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(LOG_COLUMNS)
        for rec in self._rows:
            w.writerow([rec[c] for c in LOG_COLUMNS])
        return out.getvalue().encode("utf-8")

    def summary(self) -> Dict[str, Any]:
        out = self.lock.summary()
        out.update({"behind": self.behind, "no_picture": self.missing,
                    "rate_not_set": self.failed_sets,
                    "rpm_entered": self.settings.rpm,
                    "view": self.settings.view,
                    "blades": self.settings.blades,
                    "any_blade": self.settings.any_blade,
                    "tolerance_deg": self.settings.tolerance_deg,
                    "log": f"{self.run}{LOG_SUFFIX}"})
        words = [w for w in [out.get("words", "")] + self.words if w]
        out["words"] = "; ".join(dict.fromkeys(words))
        return out


#: The run's frames CSV (council_core.capture.INDEX_SUFFIX).
INDEX_SUFFIX = "_frames.csv"


def wait_in_ui(call_in_ui: Callable[[Callable[[], None]], None],
               fn: Callable[[], Any], timeout: float = SET_WAIT_S) -> Any:
    """Run fn on the UI thread and wait for its answer (or its error)."""
    done = threading.Event()
    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:                      # noqa: BLE001
            box["error"] = exc
        finally:
            done.set()

    call_in_ui(run)
    start = time.perf_counter()
    if not done.wait(timeout):
        raise TimeoutError(f"the window did not answer in "
                           f"{time.perf_counter() - start:.1f} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")
