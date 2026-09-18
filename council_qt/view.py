"""
council_qt.view — the few things every Qt view does the same way.

WHY THIS EXISTS NOW AND NOT LATER
Three ported views in, `amp()` was defined twice, `_button` twice and `_to_ui`
THREE times — copy-pasted between vault.py, council.py and collection_dialog.py.
Phases 7, 8 and 10 add roughly a dozen more views. A helper copied into twelve
files is twelve places to fix when it is wrong, and the one that was already at
three copies is `_to_ui`: the hop that gets a worker's result back onto the GUI
thread, which is the seam this whole front end is built around.

Copying that one is not a style problem. A view whose `_to_ui` quietly differs
is a view where a worker touches a widget, which Qt does not warn about and
which fails on someone else's machine weeks later.

WHAT IS AND IS NOT HERE
Only what is genuinely identical across views: ampersand escaping, a button in
a layout, a keyboard shortcut, and the thread hop. Anything a tab does its own
way — its activity log, its status line, its layout — stays in the tab. A base
class that grows a method per tab is worse than the duplication it replaced.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import (QCoreApplication, QObject, Qt, QThread,
                            Signal)
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QPushButton, QWidget

from . import theme


def amp(text: str) -> str:
    """Escape & so Qt shows it instead of eating it as a mnemonic.

    Measured the first time the Vault tab rendered: a group box titled
    "Index & Vectorize" came out as "Index _Vectorize", because Qt reads & in
    any button, label, tab or group-box title as "underline the next letter".
    Tk has no such rule, so EVERY caption carried across from the Tk shell is a
    candidate — and the failure is silent and cosmetic, which is how it
    survives review.
    """
    # `str(text)` because the two copies this replaces had ALREADY
    # diverged: the Vault tab's coerced, the Council tab's did not, so a
    # caption that was not a string crashed in one tab and not the other.
    # Found while deduplicating them, which is the argument for doing it.
    return str(text).replace("&", "&&")


def _on_gui_thread() -> bool:
    """Whether the caller is the thread that owns the widgets.

    `QCoreApplication.instance()` is None before an app exists — at import
    time, or in a test that has not built one — and there is no GUI thread to
    be off in that case, so the answer is yes.
    """
    app = QCoreApplication.instance()
    return app is None or QThread.currentThread() is app.thread()


class _Marshal(QObject):
    """One queued signal, so a worker's callback runs on the GUI thread.

    Deliberately tiny: no queue, no pump timer, no dispatcher. A view without a
    bridge has no message STREAM to coalesce — it has individual callbacks, and
    a queued signal is exactly the right size for that. UiBridge stays the road
    for a view that is part of the app.
    """

    fire = Signal(object)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        # QueuedConnection is the whole mechanism: the slot runs on the thread
        # that owns this object, whichever thread does the emitting.
        self.fire.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, fn) -> None:
        fn()


class ViewHelpers:
    """Mixin for a Qt view. Deliberately not a QWidget subclass.

    A mixin rather than a base class because `CollectionDialog` is a QDialog
    and the tabs are QWidgets; a shared base would have to pick one and the
    other would go on copying `_to_ui`, which is what this is here to stop.
    """

    #: Set by the view's __init__, as the existing tabs already do.
    bridge = None
    window = None

    def _tokens_for(self, name: str = "dark") -> dict:
        return theme.tokens(name)

    # -- construction --------------------------------------------------
    def _button(self, layout, text: str, slot: Callable) -> QPushButton:
        """A button, escaped, wired, and added to ``layout``."""
        button = QPushButton(amp(text))
        button.clicked.connect(slot)
        layout.addWidget(button)
        return button

    def _shortcut(self, keys: str, slot: Callable,
                  parent: Optional[QWidget] = None) -> QShortcut:
        shortcut = QShortcut(QKeySequence(keys), parent or self)
        shortcut.activated.connect(slot)
        return shortcut

    # -- the thread seam -----------------------------------------------
    def _to_ui(self, fn: Callable, *args, **kwargs) -> None:
        """Run ``fn`` on the GUI thread. Safe to call from a worker.

        THE ONE HELPER THAT IS NOT CONVENIENCE. A worker touching a widget is
        undefined behaviour in Qt exactly as it is in Tk, and unlike Tk, Qt
        will not raise — it corrupts quietly. Every worker in every view hands
        its result back through here.

        WITH NO BRIDGE THIS USED TO CALL DIRECTLY, ON THE CALLER'S THREAD.
        The rationale said that was safe "because those callers are already on
        the GUI thread" — and that is exactly false for the case it exists to
        serve. A view constructed without a bridge (every test, and any module
        hosted outside CouncilWindow) had EVERY worker touching widgets from
        the worker thread. It survives most of the time and then does not: a
        Windows access violation inside the harness that builds all thirteen
        tabs, reproduced roughly one run in three once three tabs with
        constructor-time workers were added.

        So the fallback now checks. On the GUI thread, calling directly is
        genuinely right and costs nothing. Off it, the call goes through a
        queued signal — the same mechanism UiBridge uses, without its queue or
        its 50 ms pump, because a bridgeless view has no message stream to
        coalesce.
        """
        guarded = self._guard(fn)
        bridge = getattr(self, "bridge", None) or self._find_bridge()
        if bridge is not None:
            bridge.call_on_ui(guarded, *args, **kwargs)
            return
        if _on_gui_thread():
            guarded(*args, **kwargs)
            return
        self._marshal().fire.emit(lambda: guarded(*args, **kwargs))

    def _marshal(self) -> "_Marshal":
        """This view's queued-signal hop, built once.

        THE FIRST VERSION OF THIS BUILT A WHOLE UiBridge, LAZILY, AND LOST
        EVERY MESSAGE. A UiBridge starts a QTimer and connects a queued signal
        to itself — and built from inside a worker it belongs to that worker,
        whose thread has no event loop. That is precisely the trap bridge.py's
        own docstring opens with, walked into from the other direction.

        So this is moved onto the GUI thread when a worker is the one that
        needs it first. A queued connection delivers to the receiver's thread
        as it is at EMIT time, so moving after connecting is what makes the hop
        land in the right place.
        """
        marshal = getattr(self, "_private_marshal", None)
        if marshal is None:
            marshal = _Marshal()
            app = QCoreApplication.instance()
            if app is not None and QThread.currentThread() is not app.thread():
                marshal.moveToThread(app.thread())
            self._private_marshal = marshal
        return marshal

    def _guard(self, fn: Callable) -> Callable:
        """Wrap a callback so it does nothing once this view is gone.

        A WORKER CAN OUTLIVE ITS WIDGET, and two tabs start one in their
        CONSTRUCTOR — so a tab that is built, shown and closed inside a second
        leaves a thread holding a closure over `self`. When that closure lands
        and touches a QWidget whose C++ object has been destroyed, Qt does not
        raise: it is an access violation that takes the process with it.

        Observed exactly that way — an intermittent Windows access violation in
        the per-tab harness check that builds and shows every registered tab.

        `shiboken6.isValid` answers whether the C++ side is still there. When
        it is not, the callback is dropped: a result nobody can see is not
        worth a crash.
        """
        def deliver(*args, **kwargs):
            try:
                import shiboken6
                if not shiboken6.isValid(self):
                    return
            except Exception:                             # noqa: BLE001
                pass              # no shiboken: fall through and try anyway
            try:
                return fn(*args, **kwargs)
            except RuntimeError as exc:
                # "Internal C++ object already deleted" — the same race,
                # caught on the Python side when Qt is kind enough to raise.
                if "already deleted" not in str(exc):
                    raise
        return deliver

    def _find_bridge(self):
        """Walk up to a parent that has one.

        A dialog opened from a tab has no bridge of its own, and giving it one
        would mean remembering to pass it at every call site.
        """
        node = getattr(self, "parent", None)
        node = node() if callable(node) else None
        seen = 0
        while node is not None and seen < 8:          # a window is never deep
            bridge = getattr(node, "bridge", None)
            if bridge is not None:
                return bridge
            parent = getattr(node, "parent", None)
            node = parent() if callable(parent) else None
            seen += 1
        return None
