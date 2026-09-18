"""
council_qt.tabs.nodes — which Ollama hosts are up, and what they are running.

ADVANCED MODE ONLY, as in Tk. Rebuilding the dispatcher re-points the whole
council at a different set of machines.

THE AUTO-REFRESH SURVIVES A BAD HOST
The Tk timer is re-armed only inside the branch that handles a successful
result, and the probe worker has no error handling. One host that answers
`/api/ps` with a JSON array instead of an object raises in a daemon thread, the
result never arrives, and the tab reads "Probing…" until the app is restarted.
Here the re-arm is in a `finally`, so the timer outlives any probe.

A QTimer PARENTED TO THE TAB, NOT A BRIDGE CALL
The re-arm is UI-thread-only and a QTimer dies with the widget that owns it.
The Tk `after` id has to be cancelled by hand and is stored on the application
object, which is why closing the tab never stops the polling.

A STALE PROBE CANNOT REPAINT THE TABLE
Two probes can be in flight — the 15-second one and the one Apply starts — and
the slower can land last. Every result carries the hosts it ran against, and
one that does not match the dispatcher is dropped.
"""
from __future__ import annotations

import threading
from typing import Any, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from council_core import nodes, paths

from .. import theme
from ..view import ViewHelpers, amp

#: How often the table refreshes itself, in milliseconds.
REFRESH_MS = 15_000

COLUMNS = ("Host", "Status", "Latency", "Running", "Installed")
WIDTHS = (220, 80, 80, 140, 400)


class NodesActions:
    """What the Nodes tab can ask the application to do."""

    def __init__(self, dispatcher: Any = None, vault_dir=None):
        self.vault_dir = vault_dir or paths.vault_dir()
        self._dispatcher = dispatcher

    @property
    def dispatcher(self) -> Any:
        """Built on first use, so a tab nobody opens costs nothing."""
        if self._dispatcher is None:
            import council_engine
            self._dispatcher = council_engine.build_dispatcher()
        return self._dispatcher

    def hosts(self) -> str:
        """The host list as the box shows it."""
        return ", ".join(str(h) for h in
                         getattr(self.dispatcher, "hosts", ()) or ())

    def probe(self, *, force: bool = False) -> nodes.ProbeResult:
        return nodes.probe(self.dispatcher, force=force)

    def is_stale(self, result: nodes.ProbeResult) -> bool:
        return nodes.is_stale(result, self._dispatcher)

    def rebuild(self, raw_hosts: str) -> nodes.RebuildResult:
        result = nodes.rebuild(raw_hosts, self.vault_dir)
        if result.ok:
            # Swapped only on success. The Tk code assigns the dispatcher
            # before building the council, so a failure leaves a new dispatcher
            # wired to the old personalities.
            self._dispatcher = result.dispatcher
        return result


class NodesTab(ViewHelpers, QWidget):
    """A table of hosts, a host box, and two buttons."""

    def __init__(self, window=None, actions: Optional[NodesActions] = None,
                 auto_refresh: bool = True):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or NodesActions()
        self._tokens = theme.tokens("dark")
        self._busy = False

        self._build()
        # A REPEATING timer, started once on the GUI thread, rather than one
        # re-armed after each probe. There is then no re-arm path to get
        # wrong — which is the whole defect this tab is fixing — and no timer
        # call from a worker, where QTimer.start() silently does nothing.
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(lambda: self.refresh(force=False))
        if auto_refresh:
            self._timer.start()
            self.refresh(force=False)

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        blurb = QLabel(
            "Every Ollama host the council can dispatch to. Refreshed every "
            "15 seconds.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(blurb)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        for index, width in enumerate(WIDTHS):
            self.table.setColumnWidth(index, width)
        self.table.horizontalHeader().setSectionResizeMode(
            len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        outer.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Extra hosts:"))
        self.hosts_box = QLineEdit(self.actions.hosts())
        self.hosts_box.setPlaceholderText(
            "http://pi1:11434, http://pi2:11434")
        row.addWidget(self.hosts_box, 1)
        self.apply_btn = self._button(row, amp("Apply && Rebuild"),
                                      self.on_apply)
        self._button(row, amp("Refresh Now"), lambda: self.refresh(force=True))
        outer.addLayout(row)

        self.note = QLabel(nodes.hosts_are_temporary())
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.note)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

    # ------------------------------------------------------------------
    def refresh(self, *, force: bool = False) -> None:
        """Probe every host on a worker and repaint when it answers."""
        if self._busy:
            return
        self._busy = True
        self.status.setText("Probing…")

        def work() -> None:
            try:
                result = self.actions.probe(force=force)
                self._to_ui(lambda: self._show(result))
            finally:
                # In a `finally`: a probe that raised must still release the
                # busy flag, or the repeating timer skips every tick from here
                # on and the tab reads "Probing…" for the rest of the session.
                # That is the Tk defect, reached by a different route.
                self._to_ui(self._done)

        threading.Thread(target=work, name="nodes-probe", daemon=True).start()

    def _done(self) -> None:
        self._busy = False

    def _show(self, result: nodes.ProbeResult) -> None:
        if self.actions.is_stale(result):
            # Belongs to a host list the user has already replaced.
            return
        if not result.ok:
            self.status.setText(result.problem)
            return
        self.table.setRowCount(len(result.rows))
        for index, row in enumerate(result.rows):
            self._paint_row(index, row)
        self.status.setText(f"{len(result.rows)} host(s); "
                            f"{sum(1 for r in result.rows if r.up)} up")

    def _paint_row(self, index: int, row: nodes.NodeRow) -> None:
        colour = QColor(self._tokens["success"] if row.up
                        else self._tokens["error"])
        for column, text in enumerate((row.host, row.status, row.latency,
                                       row.active, row.installed)):
            item = QTableWidgetItem(text)
            if column == 1:
                item.setForeground(colour)
            self.table.setItem(index, column, item)

    # ------------------------------------------------------------------
    def on_apply(self) -> None:
        """Rebuild the dispatcher and the council for a new host list.

        On a worker: it maps model files and can load GGUF weights, which on
        the UI thread stops the window repainting for tens of seconds.
        """
        if self._busy:
            self.status.setText("Already working — wait for it to finish.")
            return
        raw = self.hosts_box.text()
        self._busy = True
        self.apply_btn.setEnabled(False)
        self.status.setText("Rebuilding…")

        def work() -> None:
            result = self.actions.rebuild(raw)

            def show() -> None:
                self._busy = False
                self.apply_btn.setEnabled(True)
                self.status.setText(result.message)
                self._report(result.message)
                if result.ok:
                    self.refresh(force=True)

            self._to_ui(show)

        threading.Thread(target=work, name="nodes-rebuild",
                         daemon=True).start()

    def _report(self, message: str) -> None:
        append = getattr(self.window, "append_transcript", None)
        if append is None:
            return
        try:
            append("Librarian", message, "final")
        except Exception:                                 # noqa: BLE001
            pass


def build_nodes(window) -> QWidget:
    """Factory for the tab registry."""
    return NodesTab(window)
