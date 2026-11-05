"""
A Basler camera's I/O lines, watched by Typhon (2 and 5 November 2026):
the line check and "Find my trigger line" of the Triggering window, and a
snapshot or record run fired by a pulse on the camera's cable.

WHY WATCH, NOT ARM. In an exact run the camera's own trigger is armed from
the line (BaslerSettings.arm: FrameStart, or FrameBurstStart, from Line1 …)
and the camera exposes only on a pulse. A snapshot or record run keeps the
live view: the camera free-runs, so its trigger cannot be the line's — and
Typhon has to be told when a pulse came. This module tells it.

HOW, ON EVERY BASLER. Only standard GenICam (SFNC) nodes, each looked up and
used only where the camera has it, so an ace, ace 2, boost, dart or a GigE
model each does what it can:

  * COUNTER (preferred): a counter of the camera's counting edges on the
    line (CounterSelector / CounterEventSource / CounterEventActivation /
    CounterValue). The camera counts every edge, however short the pulse,
    and Typhon reads the count every POLL_S: a pulse is never missed, and
    is noticed within a few ms. Counter2 first (an exact run's edge count
    uses Counter1), then Counter1. Each counter's settings are put back at
    stop. An ace USB's counters count images only: it has no line among
    their event sources, so its lines are watched by:
  * LINE STATUS (fallback): the line's level (LineSelector + LineStatus, or
    LineStatusAll's bit), read every POLL_S; an edge is a change between
    two reads. A pulse shorter than about two reads (a few ms over USB,
    more over GigE) can fall between them: the watcher says so (`method`,
    `note`).

The camera's other settings are never changed: a line that is an Output is
not watched (its LineMode is not touched); the LineSelector is put back
after every read, under the device's trigger_lock (a settings view walks
the same selectors).

No pylon import here: every call goes through the device's node map
(cameras.BaslerDevice._node), as the rest of the Basler code does.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: How often a watched line or counter is read, seconds.
POLL_S = 0.002
#: Counters a watch may borrow, in order (Counter1 last: an exact run from
#: a line counts its edges with it).
COUNTERS = ("Counter2", "Counter1", "Counter3", "Counter4")
#: "Find my trigger line": how long every input line is watched.
FIND_S = 10.0

COUNTER = "counter"
STATUS = "line status"


def _entries(node: Any) -> Tuple[str, ...]:
    if node is None:
        return ()
    for attr in ("GetSymbolics",):
        fn = getattr(node, attr, None)
        if callable(fn):
            try:
                return tuple(str(e) for e in fn())
            except Exception:                             # noqa: BLE001
                return ()
    return ()


def _get(node: Any) -> Any:
    return node.GetValue() if node is not None else None


def _set(node: Any, value: Any) -> bool:
    if node is None:
        return False
    try:
        if str(node.GetValue()) != str(value):
            node.SetValue(value)
        return True
    except Exception:                                     # noqa: BLE001
        return False


def _lock(device: Any) -> Any:
    lock = getattr(device, "trigger_lock", None)
    return lock if lock is not None else threading.RLock()


def input_lines(device: Any) -> Tuple[str, ...]:
    """The camera's lines that are inputs now (LineMode Input), in the
    camera's order — what a cable's pulse can arrive on. A line whose mode
    cannot be read is offered when the camera lists it among its trigger
    sources (an opto-coupled input has no LineMode on some models)."""
    node = getattr(device, "_node", None)
    if not callable(node):
        return ()
    selector, mode = node("LineSelector"), node("LineMode")
    lines = _entries(selector)
    if not lines:
        return ()
    listed = set()
    try:
        listed = set(getattr(device.capture_options(), "hardware", ()) or ())
    except Exception:                                     # noqa: BLE001
        listed = set()
    out = []
    with _lock(device):
        before = _get(selector)
        try:
            for line in lines:
                if not _set(selector, line):
                    continue
                try:
                    is_input = str(_get(mode)) == "Input" if mode is not None \
                        else line in listed
                except Exception:                         # noqa: BLE001
                    is_input = line in listed
                if is_input:
                    out.append(line)
        finally:
            if before is not None:
                _set(selector, before)
    return tuple(out)


def line_states(device: Any, lines: Sequence[str] = ()) -> Dict[str, Optional[bool]]:
    """Each line's level now: True (high), False, or None when the camera
    cannot say. `lines`: these (default: the inputs)."""
    lines = tuple(lines) or input_lines(device)
    node = device._node
    selector, status = node("LineSelector"), node("LineStatus")
    every = node("LineStatusAll")
    out: Dict[str, Optional[bool]] = {}
    with _lock(device):
        before = _get(selector)
        try:
            for line in lines:
                value: Optional[bool] = None
                if status is not None and _set(selector, line):
                    try:
                        value = bool(status.GetValue())
                    except Exception:                     # noqa: BLE001
                        value = None
                if value is None and every is not None:
                    value = _bit(every, line)
                out[line] = value
        finally:
            if before is not None:
                _set(selector, before)
    return out


def _bit(every: Any, line: str) -> Optional[bool]:
    """Line n's bit of LineStatusAll (bit 0 is Line1)."""
    digits = "".join(ch for ch in line if ch.isdigit())
    if not line.startswith("Line") or not digits:
        return None
    try:
        return bool(int(every.GetValue()) >> (int(digits) - 1) & 1)
    except Exception:                                     # noqa: BLE001
        return None


class _Counted:
    """One line counted by one of the camera's counters."""

    def __init__(self, device: Any, line: str, counter: str,
                 edge: str) -> None:
        self.device, self.line, self.counter = device, line, counter
        self.edge = edge
        self.saved: Dict[str, Any] = {}
        self.seen = 0

    def start(self) -> bool:
        node = self.device._node
        sel = node("CounterSelector")
        source, value = node("CounterEventSource"), node("CounterValue")
        if sel is None or source is None or value is None:
            return False
        if self.counter not in _entries(sel):
            return False
        with _lock(self.device):
            before = _get(sel)
            try:
                if not _set(sel, self.counter):
                    return False
                if self.line not in _entries(source):
                    return False
                if str(_get(source)) not in ("Off", ""):
                    return False                     # in use: not ours
                activation = node("CounterEventActivation")
                reset_source = node("CounterResetSource")
                for name, n in (("CounterEventSource", source),
                                ("CounterEventActivation", activation),
                                ("CounterResetSource", reset_source)):
                    if n is not None:
                        self.saved[name] = _get(n)
                if not _set(source, self.line):
                    return False
                if activation is not None and self.edge in _entries(
                        activation):
                    _set(activation, self.edge)
                if reset_source is not None and "Software" in _entries(
                        reset_source):
                    _set(reset_source, "Software")
                reset = node("CounterReset")
                execute = getattr(reset, "Execute", None)
                if callable(execute):
                    execute()
                self.seen = int(value.GetValue())
                return True
            finally:
                if before is not None:
                    _set(sel, before)

    def read(self) -> int:
        """New edges since the last read."""
        node = self.device._node
        sel, value = node("CounterSelector"), node("CounterValue")
        with _lock(self.device):
            before = _get(sel)
            try:
                _set(sel, self.counter)
                now = int(value.GetValue())
            finally:
                if before is not None:
                    _set(sel, before)
        new, self.seen = max(0, now - self.seen), now
        return new

    def stop(self) -> None:
        node = self.device._node
        sel = node("CounterSelector")
        with _lock(self.device):
            before = _get(sel)
            try:
                if _set(sel, self.counter):
                    for name in ("CounterResetSource",
                                 "CounterEventActivation",
                                 "CounterEventSource"):
                        if name in self.saved:
                            _set(node(name), self.saved[name])
            finally:
                if before is not None:
                    _set(sel, before)


class LineWatcher:
    """Watches `lines` of a camera and calls `fire(line)` once per edge
    (`edge`: RisingEdge, FallingEdge or AnyEdge) from a thread of its own,
    until stop(). `counts`: edges seen per line; `method`: how each line is
    watched (COUNTER or STATUS); `note`: what the user should know."""

    def __init__(self, device: Any, lines: Sequence[str],
                 fire: Optional[Callable[[str], Any]] = None,
                 edge: str = "RisingEdge", poll_s: float = POLL_S) -> None:
        self.device = device
        self.lines = tuple(dict.fromkeys(str(l) for l in lines))
        self.fire = fire
        self.edge = str(edge or "RisingEdge")
        self.poll_s = max(0.0005, float(poll_s))
        self.counts: Dict[str, int] = {line: 0 for line in self.lines}
        self.method: Dict[str, str] = {}
        self._counted: Dict[str, _Counted] = {}
        self._levels: Dict[str, Optional[bool]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error = ""

    @property
    def note(self) -> str:
        polled = [l for l, m in self.method.items() if m == STATUS]
        if not polled:
            return ""
        return (f"{', '.join(polled)} watched by reading the line's level "
                f"every {self.poll_s * 1000:g} ms (this camera has no "
                f"counter that counts that line): a pulse shorter than a "
                f"few ms can be missed — an exact run from the line never "
                f"misses one")

    def start(self) -> "LineWatcher":
        if self._thread is not None:
            return self
        free = list(COUNTERS)
        for line in self.lines:
            counted = None
            while free and counted is None:
                trial = _Counted(self.device, line, free.pop(0), self.edge)
                if trial.start():
                    counted = trial
            if counted is not None:
                self._counted[line] = counted
                self.method[line] = COUNTER
            else:
                self.method[line] = STATUS
        polled = [l for l, m in self.method.items() if m == STATUS]
        if polled:
            self._levels = line_states(self.device, polled)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="basler-line-watch")
        self._thread.start()
        return self

    def _run(self) -> None:
        polled = [l for l, m in self.method.items() if m == STATUS]
        while not self._stop.wait(self.poll_s):
            try:
                for line, counted in self._counted.items():
                    for _ in range(counted.read()):
                        self._edge(line)
                if polled:
                    now = line_states(self.device, polled)
                    for line in polled:
                        was, level = self._levels.get(line), now.get(line)
                        if was is not None and level is not None and \
                                was != level and self._wanted(was, level):
                            self._edge(line)
                        self._levels[line] = level
            except Exception as exc:                      # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.05)

    def _wanted(self, was: bool, now: bool) -> bool:
        if self.edge == "AnyEdge":
            return True
        rising = (not was) and now
        return rising if self.edge != "FallingEdge" else (was and not now)

    def _edge(self, line: str) -> None:
        self.counts[line] = self.counts.get(line, 0) + 1
        fire = self.fire
        if fire is not None:
            try:
                fire(line)
            except Exception as exc:                      # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"

    @property
    def running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive()
                    and not self._stop.is_set())

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)
        for counted in self._counted.values():
            try:
                counted.stop()
            except Exception:                             # noqa: BLE001
                pass
        self._counted = {}


def find_words(watcher: LineWatcher) -> str:
    """What "Find my trigger line" found, in words."""
    seen = {l: n for l, n in watcher.counts.items() if n}
    if not watcher.lines:
        return "This camera has no input line Typhon can watch."
    if not seen:
        return (f"No pulse on {', '.join(watcher.lines)} — check the cable "
                f"is plugged into the camera's I/O socket and the signal is "
                f"on." + (f" ({watcher.note})" if watcher.note else ""))
    best = max(seen, key=seen.get)
    parts = ", ".join(f"{l}: {n} pulse{'' if n == 1 else 's'}"
                      for l, n in seen.items())
    return (f"Your trigger cable is on {best} ({parts}). Choose Trigger "
            f"from: {best}.")
