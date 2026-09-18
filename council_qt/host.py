"""
council_qt.host — the Qt twin of council_modules.StandaloneHost.

WHY THIS EXISTS, AND WHY IT IS NOT AN AFTERTHOUGHT
`council_modules.StandaloneHost` is not leftover code. It is the contract a family of
tab modules is written against — `tab_grapher.py` here, and `tab_ideas.py` /
`tab_video.py` on `main` and `odysseus-council`. Each of those is a tab that can also
run as its own little application, and the host is what gives it a window, a UI queue,
a theme and the personality-model slots when nothing else is around.

A tab module written against the Tk host does four things to it:

    host.root                 the window
    host.after(ms, fn)        schedule on the UI thread
    host.ui_q                 the queue a worker posts to
    host.<role>               model slots (writer, director, coder, ...), or None

so the Qt host offers the same four, and a ported tab module changes which host it
imports rather than how it talks to one. `after` is the interesting one: the Tk host
delegates straight to `root.after`, which is only safe on the UI thread, while this one
routes through UiBridge and is safe from any thread — so a tab module that was quietly
wrong under Tk becomes right by moving.

The personality-model slots are deliberately copied verbatim from the Tk host rather
than tidied. They are part of the contract: a tab module does `if self.writer is None:`
and behaves differently, so renaming or dropping one changes behaviour in a module this
package cannot see.
"""
from __future__ import annotations

import queue
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from council_core import paths

from . import theme
from .window import CouncilWindow

#: The roles council_modules.StandaloneHost defines, in its order. A tab module may
#: read any of them, so the set has to match exactly.
MODEL_ROLES = ("writer", "director", "content", "sage", "strategist", "coder",
               "artist", "musician", "peasant", "intern", "eye", "cutter",
               "algorithm", "coach")


class StandaloneHost:
    """Run one tab module on its own, in Qt.

    Mirrors council_modules.StandaloneHost's surface. Construct it, build your tab
    into ``host.container``, then call ``host.run()``.
    """

    def __init__(self, vault_dir: Optional[Path] = None,
                 title: str = "Council Module",
                 geometry: str = "1100x760",
                 theme_name: str = "dark"):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.vault_dir.mkdir(parents=True, exist_ok=True)

        self._owns_app = QApplication.instance() is None
        self.app = QApplication.instance() or QApplication([])
        self.theme_name = theme_name
        # Applied whether or not we made the QApplication. Guarding this on
        # _owns_app meant StandaloneHost(theme_name="light") inside an existing
        # application silently stayed dark, and theme_name was stored nowhere
        # and read by nothing.
        theme.apply(self.app, theme_name)

        self.window = CouncilWindow(theme=theme_name)
        self.window.setWindowTitle(title)
        width, _, height = geometry.partition("x")
        try:
            self.window.resize(int(width), int(height))
        except ValueError:
            pass                                  # a malformed geometry is not fatal

        # `root` is what a Tk tab module reaches for. Pointing it at the window keeps
        # the attribute meaningful (it is the top-level widget) without pretending to
        # be a Tk root — anything calling root.tk would fail loudly rather than oddly.
        self.root = self.window
        self.bridge = self.window.bridge
        self.ui_q: "queue.Queue" = self.bridge.q

        self.container = QWidget()
        self._layout = QVBoxLayout(self.container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        # Hosted AS A TAB, not as the central widget. setCentralWidget evicted
        # CouncilWindow's QTabWidget — the window still held it, still parented
        # it, and never showed it again, so anything the hosted module added
        # through the window went nowhere. The tab bar is hidden when there is
        # only one, so a standalone module still looks standalone.
        self.window.add_tab(title, lambda: self.container, eager=True)
        self._hide_lone_tab_bar()

        for role in MODEL_ROLES:
            setattr(self, role, None)
        self._content_style = None

        # Council-specific attributes a tab module checks for and finds absent when it
        # is running standalone. Same three the Tk host stubs out.
        self.nb = None
        self.tab_council = None
        self.input = None

    def _hide_lone_tab_bar(self) -> None:
        """One tab is not a tab bar; it is a title the user cannot click."""
        try:
            self.window.tabs.tabBar().setVisible(self.window.tabs.count() > 1)
        except Exception:                                 # noqa: BLE001
            pass

    # -- the delegation a tab module uses ------------------------------
    def after(self, ms: int, fn=None, *args):
        """Schedule on the UI thread. Unlike the Tk host's, safe from any thread."""
        return self.bridge.after(ms, fn, *args)

    def after_cancel(self, token) -> None:
        self.bridge.after_cancel(token)

    def call_on_ui(self, fn, *args, **kwargs) -> None:
        self.bridge.call_on_ui(fn, *args, **kwargs)

    def set_status(self, text: str) -> None:
        self.window.set_status(text)

    def add(self, widget: QWidget) -> None:
        """Put the tab's own widget into the host window."""
        self._layout.addWidget(widget, 1)

    # -- running -------------------------------------------------------
    def run(self) -> int:
        """Show the window and run the loop. Returns the exit code."""
        self.app.aboutToQuit.connect(self.window.request_close)
        self.window.show()
        code = self.app.exec()
        self.window.request_close()
        return code

    def _init_models(self) -> None:
        """Populate the model slots from council_engine when it is importable.

        Subclasses override this, exactly as they do on the Tk host. The base keeps
        every slot None so a standalone run works with no engine at all."""
        return
