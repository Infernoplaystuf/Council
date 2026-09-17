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

        Falls back to calling directly when there is no bridge, which is how
        the views behave under test and when hosted outside CouncilWindow. That
        fallback is safe only because those callers are already on the GUI
        thread; it is not a licence to run a view without a bridge in the app.
        """
        bridge = getattr(self, "bridge", None) or self._find_bridge()
        if bridge is not None:
            bridge.call_on_ui(fn, *args, **kwargs)
        else:
            fn(*args, **kwargs)

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
