"""
council_core.side_by_side — independent steps run at the same time.

The Council tab gathers what a question needs before the council answers —
the vault passages, the Sage's knowledge, past decisions, the docs — and
those lookups do not depend on each other, so they run side by side: the
wait is the slowest step, not the sum. `start` starts them and returns at
once, so the caller can do its own work meanwhile (the task memo and the
analyst) and, when that work answers the question by itself (an answer from
the data), never wait for them at all.

No toolkit: a plain thread per step, a daemon, so a lookup nobody waits for
— or one that hangs — cannot keep the app from closing.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Mapping, Optional


class Started:
    """Steps started by `start`; `wait()` gives {name: result}, and a step
    that raised is left out, its error kept under "_errors" — one failed
    lookup costs that lookup, not the question."""

    def __init__(self, steps: Mapping[str, Optional[Callable[[], Any]]]):
        self._results: Dict[str, Any] = {}
        self._errors: Dict[str, BaseException] = {}
        self._threads = []
        for name, fn in steps.items():
            if fn is None:
                continue
            thread = threading.Thread(target=self._work, args=(name, fn),
                                      name=f"council-prep-{name}",
                                      daemon=True)
            self._threads.append(thread)
            thread.start()

    def _work(self, name: str, fn: Callable[[], Any]) -> None:
        try:
            self._results[name] = fn()
        except Exception as exc:                          # noqa: BLE001
            self._errors[name] = exc

    def wait(self) -> Dict[str, Any]:
        for thread in self._threads:
            thread.join()
        out: Dict[str, Any] = dict(self._results)
        if self._errors:
            out["_errors"] = dict(self._errors)
        return out


def start(steps: Mapping[str, Optional[Callable[[], Any]]]) -> Started:
    """Start each non-None step on its own thread now; `.wait()` for
    {name: result}."""
    return Started(steps)


def run(steps: Mapping[str, Optional[Callable[[], Any]]]) -> Dict[str, Any]:
    """Run each non-None step at the same time and wait for all of them."""
    return start(steps).wait()


__all__ = ["Started", "start", "run"]
