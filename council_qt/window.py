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

import collections
from typing import Any, Callable, Dict, List, Optional

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
        #: Reports from other tabs that arrived before any page could show
        #: them (see append_transcript). Bounded: a tab reporting in a loop
        #: with no transcript to land in must not grow without limit.
        self._held_notices: "collections.deque" = collections.deque(
            maxlen=100)
        #: This launch's conversation (council_core.conversations.Conversation)
        #: — created and owned by the Council tab, which runs the turns; the
        #: Sessions tab loads an older session into it. None until then.
        self.conversation = None
        #: Called first on close, each guarded, before on_close: a tab that
        #: holds something unsaved (the Council tab's turn) saves it here.
        self.close_hooks: List[Callable[[], None]] = []

        self.tabs = QTabWidget(self)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

        self.setStatusBar(QStatusBar(self))
        self._status = QLabel("")
        self.statusBar().addWidget(self._status, 1)

        # Which group a given tab title/tab_id belongs to, once a grouped
        # layout is installed. Each entry says both the group page it lives
        # in and the inner QTabWidget the lazy-swap targets.
        self._tab_group: Dict[str, Any] = {}
        self._tab_group_for_title: Dict[str, Any] = {}
        self._tab_id_for_title: Dict[str, str] = {}
        self.grouped: Any = None     # a group_tabs.GroupedTabsController

    # -- tabs ----------------------------------------------------------
    def install_grouped_layout(self, controller: Any) -> None:
        """Wire up the GroupedTabsController, created once per launch.

        Called from the entry point AFTER the window exists and BEFORE any
        tab is registered. Nothing fatal if it fails: the shell falls back
        to the flat layout, which is what the tests exercise.
        """
        self.grouped = controller
        # Wire inner lazy-build for each group page up front — a group may
        # be the current top-level tab before its inner tabs are added, in
        # which case the top-level currentChanged has already fired and will
        # not fire again when a tab registers into the group.
        for page in controller.groups.values():
            page.inner.currentChanged.connect(
                lambda i, gp=page: self._on_inner_tab_changed(gp, i))

    def add_tab(self, title: str, factory: Callable[[], QWidget], *,
                eager: bool = False, tab_id: Optional[str] = None) -> None:
        """Register a tab. ``factory`` builds it, on first show unless eager.

        The factory takes no arguments and returns a QWidget; it gets at the
        shell through the window it is constructed with, the same way the Tk
        builders reach `self`.

        ``tab_id`` is the stable identifier the group layout keys off. When
        the window has a grouped layout installed and the id belongs to one
        of the groups, the tab lives inside that group's inner QTabWidget;
        otherwise the tab is added flat (the behaviour the Tk-era tests
        depend on).
        """
        self._factories[title] = factory
        self._order.append(title)
        if tab_id is not None:
            self._tab_id_for_title[title] = tab_id
        if eager:
            page = self._build(title)
        else:
            page = QWidget()                 # a placeholder, swapped on show
            page.setProperty("council_placeholder", True)
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)

        if (self.grouped is not None and tab_id is not None
                and tab_id in self.grouped._id_to_group):      # noqa: SLF001
            group_page = self.grouped.register_tab(tab_id, title, page)
            if group_page is not None:
                self._tab_group[tab_id] = group_page
                self._tab_group_for_title[title] = group_page
                return
        self.tabs.addTab(page, title)

    def _build(self, title: str) -> QWidget:
        page = self._factories[title]()
        self._built[title] = page
        if self._held_notices and hasattr(page, "receive_notice"):
            held, self._held_notices = (list(self._held_notices),
                                        collections.deque(maxlen=100))
            for who, text, kind, source in held:
                self._deliver_notice(page, who, text, kind, source)
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
            # Could be a top-level group page: when a group becomes the
            # current tab, make sure its first-visible inner tab's lazy
            # content is built too.
            self._build_current_inner()
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
        self._build_current_inner()

    def _build_current_inner(self) -> None:
        """Build whichever inner tab the current group page shows now."""
        if self.grouped is None:
            return
        idx = self.tabs.currentIndex()
        if idx < 0:
            return
        page = self.tabs.widget(idx)
        if page not in self.grouped.groups.values():
            return
        inner = page.inner
        i = inner.currentIndex()
        if i < 0:
            return
        self._maybe_build_inner(page, i, inner.tabText(i))

    def _on_inner_tab_changed(self, group_page, index: int) -> None:
        if index < 0:
            return
        title = group_page.inner.tabText(index)
        self._maybe_build_inner(group_page, index, title)

    def _maybe_build_inner(self, group_page, index: int, title: str) -> None:
        if title in self._built or title not in self._factories:
            return
        current = group_page.inner.widget(index)
        if not current.property("council_placeholder"):
            return
        page = self._build(title)
        group_page.inner.removeTab(index)
        group_page.inner.insertTab(index, page, title)
        group_page.inner.setCurrentIndex(index)
        # No mapping update needed: the group's tab-id order is stable
        # since nothing is reordered — removeTab/insertTab at the same
        # index keeps every id at the same position.

    def show_tab(self, title: str) -> None:
        """Bring a tab to the front by name.

        The Tk shell does `self.nb.select(self.tab_models)` at 7 cross-tab
        sites — the error coach sending the user to Models, for instance. Those
        become this, and stop depending on a widget attribute existing."""
        group_page = self._tab_group_for_title.get(title)
        if group_page is not None:
            idx = self.tabs.indexOf(group_page)
            if idx >= 0:
                self.tabs.setCurrentIndex(idx)
            inner = group_page.inner
            for i in range(inner.count()):
                if inner.tabText(i) == title:
                    inner.setCurrentIndex(i)
                    return
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == title:
                self.tabs.setCurrentIndex(i)
                return

    def has_tab(self, title: str) -> bool:
        """Whether this build has that tab at all.

        Separate from tab(), which returns None both for "never registered"
        and for "registered but not built yet". The engine needs to tell those
        apart to say "that tab is not in this build" rather than failing
        silently on a tab the user simply has not opened.
        """
        if title in self._tab_group_for_title:
            return True
        return any(self.tabs.tabText(i) == title
                   for i in range(self.tabs.count()))

    def tab(self, title: str) -> Optional[QWidget]:
        """The built page, or None if it has not been shown yet."""
        return self._built.get(title)

    # -- status --------------------------------------------------------
    def set_status(self, text: str) -> None:
        """The Tk shell's _set_status, unchanged in meaning."""
        self._status.setText("" if text is None else str(text))

    # -- reports from other tabs ----------------------------------------
    def append_transcript(self, who: str, text: str, kind: str = "final",
                          *, source: str = "") -> None:
        """A report from another tab, written into the Council transcript.

        THIS WAS MISSING, AND NOTHING SAID SO. The IDE, Librarian and Nodes
        tabs end an operation with `getattr(window, "append_transcript",
        None)` and return quietly when it is absent — so the snapshot path,
        the commit receipt with its SHA and the node-rebuild result were all
        dropped. The Tk shell writes each of them into the transcript.

        Safe from any thread: off the GUI thread it hops through the bridge,
        the one road from a worker to a widget. The page that shows it is
        whichever built page has `receive_notice` (the Council tab), found
        rather than named, so a build without one is not an error. Until one
        is built the notice is HELD and handed over when it is — and shown in
        the status bar meanwhile, so it is never silent.

        ``source`` names the tab it came from, for the label.
        """
        if not self.bridge.on_ui_thread():
            self.bridge.call_on_ui(self.append_transcript, who, text, kind,
                                   source=source)
            return
        page = self._notice_page()
        if page is None:
            self._held_notices.append((who, text, kind, source))
            self.set_status(f"{source or who}: {text}")
            return
        self._deliver_notice(page, who, text, kind, source)

    def _notice_page(self) -> Optional[QWidget]:
        for title in self._order:
            page = self._built.get(title)
            if page is not None and hasattr(page, "receive_notice"):
                return page
        return None

    @staticmethod
    def _deliver_notice(page, who, text, kind, source) -> None:
        try:
            page.receive_notice(who, text, kind, source=source)
        except Exception as exc:                        # noqa: BLE001
            # A report about another tab must never take this one down.
            print(f"[window] a notice could not be shown: {exc!r}")

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
        for hook in list(self.close_hooks):
            try:
                hook()
            except Exception as exc:                    # noqa: BLE001
                print(f"a close hook failed: {exc!r}")
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
