"""
council_qt.window — the Qt shell: one window, a tab host, a status bar.

This is the frame every ported tab drops into. It deliberately owns almost
nothing: a QTabWidget, a status line, the UiBridge, and the close path. Tabs are
registered, not hard-coded, so a build with three ported tabs is a whole app
with three tabs rather than a half-ported one — which is what makes the port
shippable in phases.

TABS ARE LAZY BY DEFAULT. The Tk shell builds all 14 (or 20) up front, which is
why it needs a splash to pump and a withdrawn root to hide the flicker. Building
a page the first time it is shown deletes that whole apparatus. A tab that must
exist from the start — because a background worker posts to it whether or not
the user has looked — registers with eager=True.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QLabel, QMainWindow, QStatusBar, QTabWidget,
                               QVBoxLayout, QWidget)

import branding

from .bridge import UiBridge


class CouncilWindow(QMainWindow):
    """The Qt shell. Tabs register themselves; nothing here knows about them."""

    def __init__(self, theme: str = "dark", parent=None):
        super().__init__(parent)
        self.setWindowTitle(branding.window_title())
        self.resize(1280, 860)
        self.theme_name = theme

        self.bridge = UiBridge(parent=self)
        self._closing = False
        self._factories: Dict[str, Callable[[], QWidget]] = {}
        self._built: Dict[str, QWidget] = {}
        self._order: List[str] = []
        self._swapping = False

        self.tabs = QTabWidget(self)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

        self.setStatusBar(QStatusBar(self))
        self._status = QLabel("")
        self.statusBar().addWidget(self._status, 1)

    # -- tabs ----------------------------------------------------------
    def add_tab(self, title: str, factory: Callable[[], QWidget], *,
                eager: bool = False) -> None:
        """Register a tab. ``factory`` builds it, on first show unless eager.

        The factory takes no arguments and returns a QWidget; it gets at the
        shell through the window it is constructed with, the same way the Tk
        builders reach `self`."""
        self._factories[title] = factory
        self._order.append(title)
        if eager:
            page = self._build(title)
        else:
            page = QWidget()                 # a placeholder, swapped on show
            page.setProperty("council_placeholder", True)
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
        self.tabs.addTab(page, title)

    def _build(self, title: str) -> QWidget:
        page = self._factories[title]()
        self._built[title] = page
        return page

    def _on_tab_changed(self, index: int) -> None:
        """Build a placeholder page the first time it is shown.

        Note that adding the FIRST tab makes it current straight away, so that
        one builds during add_tab — which is right: it is about to be visible.
        The swap happens inside addTab's own signal, so the reentrancy guard is
        not theoretical."""
        if index < 0 or self._swapping:
            return
        title = self.tabs.tabText(index)
        if title in self._built or title not in self._factories:
            return
        current = self.tabs.widget(index)
        if not current.property("council_placeholder"):
            return
        self._swapping = True
        try:
            page = self._build(title)
            self.tabs.removeTab(index)
            self.tabs.insertTab(index, page, title)
            self.tabs.setCurrentIndex(index)
        finally:
            self._swapping = False

    def show_tab(self, title: str) -> None:
        """Bring a tab to the front by name.

        The Tk shell does `self.nb.select(self.tab_models)` at 7 cross-tab
        sites — the error coach sending the user to Models, for instance. Those
        become this, and stop depending on a widget attribute existing."""
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == title:
                self.tabs.setCurrentIndex(i)
                return

    def tab(self, title: str) -> Optional[QWidget]:
        """The built page, or None if it has not been shown yet."""
        return self._built.get(title)

    # -- status --------------------------------------------------------
    def set_status(self, text: str) -> None:
        """The Tk shell's _set_status, unchanged in meaning."""
        self._status.setText("" if text is None else str(text))

    # -- closing -------------------------------------------------------
    def closeEvent(self, event) -> None:
        """Clean up, then ALWAYS accept.

        The obvious shape here — ignore() the event, run the cleanup, then
        close() again — deadlocks the application, and it took a bisect to find:
        during QApplication.quit() Qt sends close events to top-level windows,
        and a window that ignores one VETOES the shutdown. The app then sits in
        exec() for ever with its window gone. Measured: the process had to be
        killed.

        So cleanup and closing are separate. Nothing here ever vetoes.
        """
        self._shutdown()
        event.accept()

    def on_close(self) -> None:
        """Overridden by the app: flush logs, stop workers, release devices."""

    def _shutdown(self) -> None:
        """Run the app's cleanup exactly once. Never closes anything."""
        if self._closing:
            return
        self._closing = True
        try:
            self.on_close()
        except Exception as exc:                        # noqa: BLE001
            # A failing shutdown must not trap the user in a window they asked
            # to close; say what broke and go.
            print(f"on_close failed: {exc!r}")
        self.bridge.stop()

    def request_close(self) -> None:
        """Close the window from code — the Stop path, and the menu's Quit."""
        self._shutdown()
        self.close()
