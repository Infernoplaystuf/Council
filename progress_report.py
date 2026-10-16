"""
progress_report.py — progress lines in the terminal for long local work (the
Ledger's extract / apply, the knowledge graph's rebuild), so it can be seen
whether it is moving or has stalled.

    with Progress("ledger extract") as p:
        p.stage("reading workbooks", total=len(files), unit="files")
        for f in files:
            p.update(f"opening {f.name}")       # what it is doing now
            ...
            p.advance(what=f"{f.name} read")    # one more of the total done

prints, to the terminal the Council was started from:

    [ledger extract] reading workbooks: 0/12 files (0%)
    [ledger extract] reading workbooks: 4/12 files (33%) · 2041_EC.xlsx read · 0:12
    [ledger extract] still working, nothing new for 30 s: opening locked.xlsx through Excel · 1:05
    [ledger extract] done in 1:20 · 12 files: 1240 values found, ...

THE STALL LINE is the point: a heartbeat thread prints "still working,
nothing new for N s" while no step has finished, so a quiet terminal never
leaves you guessing between "busy" and "stuck". A step that finishes resets it.

Lines are throttled (one per `every` seconds, plus every 10 % and the last
one) so a 5 000-row stage does not flood the console. `listener(line,
fraction)` gets every line too - the Ledger tab shows them in its status line
and progress bar. Nothing leaves the machine; COUNCIL_PROGRESS=0 silences the
terminal (the listener still gets the lines).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Optional, TextIO

Listener = Callable[[str, Optional[float]], None]


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class Progress:
    def __init__(self, task: str, total: int = 0, unit: str = "",
                 stream: Optional[TextIO] = None, listener: Optional[Listener] = None,
                 heartbeat: float = 20.0, every: float = 2.0):
        self.task = task
        self.stage_name = ""
        self.total, self.done, self.unit = int(total or 0), 0, unit
        self.what = ""
        self.listener = listener
        self.heartbeat, self.every = heartbeat, every
        quiet = os.environ.get("COUNCIL_PROGRESS", "").strip() in ("0", "off", "no")
        self.stream = None if quiet else (stream if stream is not None else sys.stderr)
        self.started = self.last_activity = self.last_beat = time.monotonic()
        self._last_print = 0.0
        self._last_tenth = -1
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._beater: Optional[threading.Thread] = None
        if heartbeat and heartbeat > 0:
            self._beater = threading.Thread(target=self._beat, name="progress-heartbeat",
                                            daemon=True)
            self._beater.start()

    # ── what callers use ──────────────────────────────────────────────
    def stage(self, name: str, total: int = 0, unit: str = "") -> None:
        """A new phase: its own count, from 0."""
        with self._lock:
            self.stage_name, self.total, self.done, self.unit = name, int(total or 0), 0, unit
            self.what = ""
            self._last_tenth = 0 if self.total else -1      # its 0% is this line
            self._touch()
            self._emit(force=True)

    def update(self, what: str, force: bool = False) -> None:
        """What it is doing now (no step finished). `force` shows it at once
        (opening a file through Excel - where a wait can start)."""
        with self._lock:
            self.what = what
            self._touch()
            self._emit(force=force)

    def advance(self, n: int = 1, what: str = "") -> None:
        """`n` more of the stage's total finished."""
        with self._lock:
            self.done += n
            if what:
                self.what = what
            self._touch()
            tenth = int(10 * self.done / self.total) if self.total else -1
            self._emit(force=tenth != self._last_tenth or self.done == self.total)
            self._last_tenth = tenth

    def note(self, message: str) -> None:
        """A line of its own (a problem, a decision) - always shown."""
        with self._lock:
            self._touch()
            self._write(f"[{self.task}] {message}", self.fraction)

    def finish(self, summary: str = "") -> None:
        self._end(f"done in {_clock(time.monotonic() - self.started)}"
                  + (f" · {summary}" if summary else ""), 1.0)

    def fail(self, reason: str) -> None:
        self._end(f"stopped after {_clock(time.monotonic() - self.started)}: {reason}", None)

    @property
    def fraction(self) -> Optional[float]:
        return min(1.0, self.done / self.total) if self.total else None

    # context manager: finish on success, fail on an exception
    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, kind, exc, tb) -> bool:
        if not self._stop.is_set():
            if exc is None:
                self.finish()
            else:
                self.fail(f"{type(exc).__name__}: {str(exc)[:200]}")
        return False

    # ── inside ────────────────────────────────────────────────────────
    def _touch(self) -> None:
        self.last_activity = time.monotonic()

    def _line(self) -> str:
        parts = [f"[{self.task}]"]
        head = f"{self.stage_name}:" if self.stage_name else ""
        if self.total:
            count = f"{self.done}/{self.total}{' ' + self.unit if self.unit else ''}"
            parts.append(f"{head} {count} ({100 * self.done // self.total}%)".strip())
        elif self.done:
            parts.append(f"{head} {self.done}{' ' + self.unit if self.unit else ''}".strip())
        elif head:
            parts.append(head.rstrip(":"))
        if self.what:
            parts.append(f"· {self.what}" if len(parts) > 1 else self.what)
        parts.append(f"· {_clock(time.monotonic() - self.started)}")
        return " ".join(parts)

    def _emit(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_print >= self.every:
            self._last_print = now
            self._write(self._line(), self.fraction)

    def _write(self, line: str, fraction: Optional[float]) -> None:
        if self.stream is not None:
            try:
                print(line, file=self.stream, flush=True)
            except Exception:                          # no console (pythonw)
                self.stream = None
        if self.listener is not None:
            try:
                self.listener(line.split("] ", 1)[-1], fraction)
            except Exception:
                pass

    def _beat(self) -> None:
        while not self._stop.wait(min(1.0, max(0.02, self.heartbeat / 4))):
            with self._lock:
                now = time.monotonic()
                quiet = now - self.last_activity
                if quiet >= self.heartbeat and now - self.last_beat >= self.heartbeat:
                    self.last_beat = now
                    doing = self.what or self.stage_name or "working"
                    secs = f"{quiet:.1f}" if quiet < 10 else f"{int(quiet)}"
                    self._write(f"[{self.task}] still working, nothing new for {secs} s: "
                                f"{doing} · {_clock(now - self.started)}", self.fraction)

    def _end(self, message: str, fraction: Optional[float]) -> None:
        self._stop.set()
        with self._lock:
            self._write(f"[{self.task}] {message}", fraction)


class NullProgress(Progress):
    """For callers that pass none: same methods, no output, no thread."""

    def __init__(self, *a, **k):
        super().__init__("", stream=None, heartbeat=0)
        self.stream = None

    def _write(self, line, fraction) -> None:
        pass
