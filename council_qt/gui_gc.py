"""
council_qt.gui_gc — Python's garbage is collected on the GUI thread only.

PYTHON COLLECTS GARBAGE ON WHICHEVER THREAD HAPPENS TO ALLOCATE when a
threshold is crossed - a worker as often as not. A Qt widget in a reference
cycle is then destroyed on that worker, which Qt forbids, and one with a
window does worse than crash: ~QWidget closes its QWindow, which waits for
the GUI thread to handle the close - while the GUI thread is joining that
very worker. Found as a hang of tests/test_launch.py (2026-10-22): the
vault-setup step's worker collected a window an earlier build had left in a
cycle, and launch's _Step.wait waited for it forever. Under PySide6 6.10 and
6.12 alike.

So automatic collection is turned off and a timer on the GUI thread does it
instead, by the same thresholds Python would use - the usual cure for Qt
apps. Reference counting, which frees almost everything, is untouched; only
cycles wait for the next tick.
"""
from __future__ import annotations

import gc
from typing import Optional

from PySide6.QtCore import QObject, QTimer

#: how often the GUI thread looks at the collector's counts
INTERVAL_MS = 500

_collector: Optional["GuiThreadGC"] = None


class GuiThreadGC(QObject):
    """Automatic collection off; ``collect`` on a GUI-thread timer instead."""

    def __init__(self, parent: QObject, interval_ms: int = INTERVAL_MS):
        super().__init__(parent)
        self.threshold = gc.get_threshold()
        gc.disable()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.collect)
        self.timer.start(interval_ms)

    def collect(self) -> int:
        """What an automatic collection would do now: the oldest generation
        whose count is past its threshold, or nothing."""
        counts = gc.get_count()
        generation = -1
        for i, (count, limit) in enumerate(zip(counts, self.threshold)):
            if limit and count > limit:
                generation = i
            else:
                break
        return gc.collect(generation) if generation >= 0 else 0


def install(app: QObject) -> GuiThreadGC:
    """Collect on ``app``'s thread from now on (once per application)."""
    global _collector
    try:
        alive = _collector is not None and _collector.parent() is app
    except RuntimeError:                       # its app was deleted
        alive = False
    if not alive:
        _collector = GuiThreadGC(app)
    return _collector
