"""
council_qt.bridge — everything that crosses from a worker thread to the GUI.

THIS IS THE MOST DANGEROUS FILE IN THE PORT, so it is the first one written.

The Tk shell marshals work onto the UI thread two ways: `self.ui_q.put(...)`
drained by a repeating `after(50)`, and ~83 direct `self.after(0, fn)` calls,
most of which are made FROM worker threads. The naive Qt translation of the
second one is `QTimer.singleShot(0, fn)` — and that is a trap:

    A QTimer created or started on a thread with no event loop never fires.
    Qt prints "QObject::startTimer: Timers can only be used with threads
    started with QThread" at most, raises nothing, and the callback is simply
    lost.

So a naive port silently deletes ~83 code paths — HF downloads, NX runs, model
scans, the scraper, index builds, the Designer's `call_soon` — with no
exception, no traceback, and a test suite that stays green. That is why this
module exists before any widget does.

HOW IT WORKS
    * One `queue.Queue` survives from the Tk design. It is already thread-safe
      and toolkit-neutral, and its drain-everything-then-update-once shape is
      load-bearing: the transcript coalesces ~100 tokens/sec into one scroll,
      which per-message signal delivery would undo.
    * A Qt Signal with a queued connection is the wake-up. Emitting a signal is
      safe from any thread; the slot runs on the thread that owns the object —
      here, the GUI thread.
    * A 50 ms timer is a backstop, so a lost wake-up costs latency, not the
      message. The Tk build polls at exactly this interval.
    * `after()` keeps Tk's signature so the ~83 call sites convert by name
      rather than by rewrite, and — unlike Tk — it is safe to call from any
      thread, because it marshals before it ever touches a QTimer.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal

# The message kind the after() shim posts to itself. Namespaced so it can never
# collide with one of the dispatcher's 33 real kinds.
_AFTER = "__council_after__"
_CALL = "__council_call__"


class UiBridge(QObject):
    """The one road from a worker thread to the GUI thread."""

    #: Emitted (from any thread) when something is waiting in the queue.
    wake = Signal()

    def __init__(self, dispatch: Optional[Callable[[Any], None]] = None,
                 parent: Optional[QObject] = None, interval_ms: int = 50):
        super().__init__(parent)
        self.q: "queue.Queue" = queue.Queue()
        self._dispatch = dispatch
        self._timers: dict = {}
        self._token = 0
        self._lock = threading.Lock()
        # QueuedConnection is what moves the call onto the GUI thread. Without
        # it a signal emitted from a worker would run its slot on that worker.
        self.wake.connect(self._drain, Qt.ConnectionType.QueuedConnection)
        self._pump = QTimer(self)
        self._pump.timeout.connect(self._drain)
        self._pump.start(interval_ms)

    # -- posting (safe from any thread) --------------------------------
    def post(self, item: Any) -> None:
        """Queue a message for the dispatcher. Safe from any thread."""
        self.q.put(item)
        self.wake.emit()

    def call_on_ui(self, fn: Callable, *args, **kwargs) -> None:
        """Run ``fn`` on the GUI thread, soon. Safe from any thread.

        This is the honest replacement for `self.after(0, fn)` — it is what the
        Tk sites MEANT, and unlike QTimer.singleShot(0, ...) it works when the
        caller is a worker."""
        self.post((_CALL, fn, args, kwargs))

    def after(self, ms: int, fn: Optional[Callable] = None, *args) -> Any:
        """Tk's `after`, with Tk's signature and Qt's safety.

        Returns a token `after_cancel` understands. Called from a worker, the
        timer is created on the GUI thread instead of on the caller — which is
        the whole point, because a timer started on a thread with no event loop
        never fires."""
        if fn is None:
            return None                     # Tk's sleeping form; unused here
        if self.on_ui_thread():
            return self._start_timer(ms, fn, args)
        self.post((_AFTER, ms, fn, args))
        return None                         # a cross-thread after is fire-and-forget

    def after_cancel(self, token: Any) -> None:
        if not token:
            return
        timer = self._timers.pop(token, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def after_idle(self, fn: Callable, *args) -> Any:
        return self.after(0, fn, *args)

    # -- thread identity ------------------------------------------------
    def on_ui_thread(self) -> bool:
        """Whether the caller is on the thread that owns this bridge."""
        return QThread.currentThread() is self.thread()

    def assert_ui_thread(self, what: str = "this") -> None:
        """Raise if called off the GUI thread.

        Tk sometimes catches this for us — the engine carries a comment
        recording real "main thread is not in main loop" errors. Qt does not:
        it usually just corrupts the widget quietly. A tripwire is the only way
        that class of bug stays findable after the port."""
        if not self.on_ui_thread():
            raise RuntimeError(
                f"{what} touched the UI from {threading.current_thread().name!r}; "
                f"use bridge.call_on_ui(...) or bridge.post(...)")

    # -- draining (always on the GUI thread) ----------------------------
    def _start_timer(self, ms: int, fn: Callable, args: tuple) -> Any:
        with self._lock:
            self._token += 1
            token = f"after#{self._token}"
        timer = QTimer(self)
        timer.setSingleShot(True)

        def _fire():
            self._timers.pop(token, None)
            self._safely(fn, args, {})

        timer.timeout.connect(_fire)
        self._timers[token] = timer
        timer.start(max(0, int(ms)))
        return token

    def _safely(self, fn: Callable, args: tuple, kwargs: dict) -> None:
        """Run one callback. A raising callback must not kill the pump.

        The Tk dispatcher wraps its whole body in `except Exception: print(...)`
        for exactly this reason; keeping that shape means a bad handler loses
        one message rather than every message after it."""
        try:
            fn(*args, **kwargs)
        except Exception as exc:                        # noqa: BLE001
            print(f"[ui] callback failed: {exc!r}")

    def _drain(self) -> None:
        """Empty the queue, then let the caller update once.

        Drains everything available rather than one item per tick: at 100+
        tokens/sec a per-item update makes the transcript stutter, which the Tk
        build already learned and coalesced."""
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            try:
                if isinstance(item, tuple) and item and item[0] == _CALL:
                    _, fn, args, kwargs = item
                    self._safely(fn, args, kwargs)
                elif isinstance(item, tuple) and item and item[0] == _AFTER:
                    _, ms, fn, args = item
                    self._start_timer(ms, fn, args)
                elif self._dispatch is not None:
                    self._dispatch(item)
            except Exception as exc:                    # noqa: BLE001
                print(f"[ui] queue handler failed: {exc!r}")

    def set_dispatch(self, dispatch: Callable[[Any], None]) -> None:
        self._dispatch = dispatch

    def stop(self) -> None:
        self._pump.stop()
        for timer in list(self._timers.values()):
            timer.stop()
        self._timers.clear()
