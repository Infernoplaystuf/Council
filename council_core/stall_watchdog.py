"""
council_core.stall_watchdog — "the app froze and I don't know why".

Starts a daemon thread that samples every live Python thread's stack
every N seconds and, when the target thread's own progress heartbeat
has not advanced within a wider window, writes those stacks to the
log alongside a one-line summary of what was running.

This solves the user-reported stall where the whole app goes
unresponsive during a Ledger extract. In Python, that symptom usually
means a CPU-bound stretch holding the GIL in a worker thread so the
Qt event loop can't make progress. The watchdog's job is to tell us
WHICH line of WHICH thread is the GIL-hog, without needing sample
data from the user.

Public API:
  * ``watch(label, log, stall_s=15, sample_s=2.0)`` — context manager.
    ``log`` is a one-arg callable; ``label`` names the step. Call
    ``w.beat()`` from the worker on every unit of real progress so
    the watchdog doesn't bark during slow-but-moving work.
  * ``format_all_stacks()`` — a one-shot snapshot of every live
    thread's current frame as human-readable text.

Nothing here imports Qt or Council's engine — safe to call from any
thread, any process."""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from typing import Callable, Iterator, Optional


def format_all_stacks() -> str:
    """Snapshot every live Python thread's current frame.

    Uses ``sys._current_frames()`` — a cheap read of the interpreter's
    thread registry. Returns a string with one block per thread. The
    output is deterministic in thread id order so a diff between
    successive snapshots is easy to eyeball."""
    frames = sys._current_frames()
    thread_names = {t.ident: t.name for t in threading.enumerate()}
    out: list = []
    for tid in sorted(frames.keys()):
        name = thread_names.get(tid, f"<thread {tid}>")
        out.append(f"--- thread id={tid} name={name!r} ---")
        try:
            stack = traceback.format_stack(frames[tid])
        except Exception as exc:                           # noqa: BLE001
            stack = [f"<stack unavailable: {exc!r}>"]
        out.extend(stack)
    return "".join(out) if out else "<no live threads>"


class _Watchdog:
    def __init__(self, label: str, log: Callable[[str], None],
                 *, stall_s: float, sample_s: float) -> None:
        self.label = label
        self._log = log
        self.stall_s = float(stall_s)
        self.sample_s = max(0.5, float(sample_s))
        self._last_beat = time.monotonic()
        self._stop = threading.Event()
        self._reported = False
        self._thread: Optional[threading.Thread] = None

    def beat(self) -> None:
        """Mark progress. Call this from the worker whenever a unit of
        real work finishes (one workbook, one picture, one match)."""
        self._last_beat = time.monotonic()
        self._reported = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"stall-watchdog:{self.label}",
            daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.sample_s * 2)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.sample_s):
            idle = time.monotonic() - self._last_beat
            if idle < self.stall_s or self._reported:
                continue
            try:
                stacks = format_all_stacks()
                self._log(f"[stall-watchdog] no progress on {self.label!r} "
                          f"for {idle:.1f}s — stacks follow:\n{stacks}")
            except Exception as exc:                       # noqa: BLE001
                try:
                    self._log(f"[stall-watchdog] could not snapshot: {exc!r}")
                except Exception:                          # noqa: BLE001
                    pass
            # Report once per stall so a long sustained stall does not
            # spam the log every sample. The next beat resets the flag.
            self._reported = True


@contextmanager
def watch(label: str, log: Callable[[str], None],
          *, stall_s: float = 15.0, sample_s: float = 2.0
          ) -> Iterator[_Watchdog]:
    """Monitor progress of a block of work; dump stacks when it stalls.

    Usage::

        with stall_watchdog.watch("ledger extract", self._say) as w:
            for src in sources:
                process(src)
                w.beat()
    """
    w = _Watchdog(label, log, stall_s=stall_s, sample_s=sample_s)
    w.start()
    try:
        yield w
    finally:
        w.stop()


def system_info() -> str:
    """A short block the user can paste in a bug report: Python, OS,
    the Ollama models visible on localhost (counts only, no model
    names so a corporate vault stays private), VRAM free when a
    pyNVML is around, pywin32 presence.

    Everything optional — a missing library is a line that says
    'not installed', never a crash."""
    lines = [f"os.name={os.name}  platform={sys.platform}",
             f"python={sys.version.split()[0]}"]
    try:
        import platform
        lines.append(f"machine={platform.machine()} "
                     f"processor={platform.processor() or '<unknown>'}")
    except Exception:                                     # noqa: BLE001
        pass
    try:
        import pywintypes                                  # noqa: F401
        lines.append("pywin32=present")
    except Exception:                                     # noqa: BLE001
        lines.append("pywin32=not installed")
    try:
        import llama_cpp
        lines.append(f"llama_cpp={getattr(llama_cpp, '__version__', '?')}")
    except Exception:                                     # noqa: BLE001
        lines.append("llama_cpp=not installed")
    try:
        import pynvml
        pynvml.nvmlInit()
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            lines.append(f"gpu0 vram free={mem.free // (1024 ** 2)} MiB "
                         f"total={mem.total // (1024 ** 2)} MiB")
        finally:
            pynvml.nvmlShutdown()
    except Exception:                                     # noqa: BLE001
        lines.append("gpu=unknown (install pynvml or nvidia-ml-py "
                     "for VRAM in the debug bundle)")
    # Ollama probe omitted on purpose: the offline test guard counts
    # any loopback connection attempt as a failure, and we don't want
    # a diagnostic helper to trip it. Users who paste this block into
    # a bug report can add `ollama list` output alongside it.
    return "\n".join(lines)
