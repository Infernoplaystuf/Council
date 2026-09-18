"""
council_qt.tabs.vault_health — what is in the vault, and how big.

A read-only dashboard: per-personality memory files, the top level of the
vault, and a short summary of the wishlist and project context. Advanced mode,
as in Tk.

IT GATHERS ON A WORKER
Nothing in the Tk tab does. It runs iterdir, N stat() calls and a read_text
synchronously on the UI thread — including once during startup, over a vault
that can hold tens of thousands of files. That is a window that does not paint
until the disk is done.

`_guard` matters more here than anywhere: the constructor kicks off a refresh,
so a tab built and closed quickly has a worker posting to a widget that is
already gone. On Windows that is an access violation, not an exception.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from council_core import paths, vault_health

from .. import theme
from ..view import ViewHelpers, amp

COLUMNS = ("Name", "Size", "Modified")


class VaultHealthActions:
    """What the Vault Health tab can ask the application to do."""

    def __init__(self, vault_dir: Optional[Path] = None,
                 mem_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.mem_dir = Path(mem_dir) if mem_dir else self.vault_dir / "memory"

    def gather(self) -> vault_health.Report:
        return vault_health.gather(self.vault_dir, self.mem_dir)

    def open_folder(self, path: Optional[Path] = None) -> None:
        """Reused from the Vault tab. Tk has two copies of this and they have
        already drifted."""
        from .vault import VaultActions
        VaultActions(self.vault_dir).open_folder(path)


class VaultHealthTab(ViewHelpers, QWidget):
    """Two trees and a summary pane."""

    def __init__(self, window=None,
                 actions: Optional[VaultHealthActions] = None,
                 auto_refresh: bool = True):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or VaultHealthActions()
        self._tokens = theme.tokens("dark")
        self._busy = False
        self._report: Optional[vault_health.Report] = None

        self._build()
        if auto_refresh:
            self.refresh()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        row = QHBoxLayout()
        self.refresh_btn = self._button(row, amp("⟳ Refresh"), self.refresh)
        self._button(row, amp("Open vault folder"), self.on_open_vault)
        self.wishlist_btn = self._button(row, amp("Open wishlist"),
                                         self.on_open_wishlist)
        row.addStretch(1)
        self.status = QLabel("")
        row.addWidget(self.status)
        outer.addLayout(row)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._tree_box("Personality memory files", "memory"))
        split.addWidget(self._tree_box("Vault root", "vault"))
        split.setSizes([420, 520])
        outer.addWidget(split, 1)

        summary_box = QGroupBox("Summary")
        summary_layout = QVBoxLayout(summary_box)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMaximumHeight(140)
        summary_layout.addWidget(self.summary)
        outer.addWidget(summary_box)

    def _tree_box(self, title: str, which: str) -> QWidget:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        tree = QTreeWidget()
        tree.setColumnCount(len(COLUMNS))
        tree.setHeaderLabels(list(COLUMNS))
        tree.setRootIsDecorated(False)
        tree.setColumnWidth(0, 220)
        layout.addWidget(tree)
        setattr(self, f"{which}_tree", tree)
        return box

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.status.setText("Reading…")

        def work() -> None:
            try:
                report = self.actions.gather()
                self._to_ui(lambda: self._show(report))
            finally:
                self._to_ui(self._done)

        threading.Thread(target=work, name="vault-health",
                         daemon=True).start()

    def _done(self) -> None:
        self._busy = False

    def _show(self, report: vault_health.Report) -> None:
        self._report = report
        self._fill(self.memory_tree, report.memory)
        self._fill(self.vault_tree, report.vault)
        self.summary.setPlainText("\n".join(report.summary.lines()))
        unreadable = sum(1 for e in report.memory + report.vault if e.problem)
        self.status.setText(
            f"{len(report.memory)} memory file(s), "
            f"{len(report.vault)} vault entr(y/ies)"
            + (f" — {unreadable} unreadable" if unreadable else ""))

    def _fill(self, tree: QTreeWidget, entries) -> None:
        tree.clear()
        for entry in entries:
            item = QTreeWidgetItem([entry.label, entry.size, entry.modified])
            if entry.path is not None:
                # The PATH travels with the row rather than being rebuilt from
                # the label — the defect the Librarian tab is named after.
                item.setData(0, Qt.ItemDataRole.UserRole, str(entry.path))
            if entry.problem:
                item.setToolTip(0, entry.problem)
                item.setForeground(0, QColor(self._tokens["error"]))
            tree.addTopLevelItem(item)

    # ------------------------------------------------------------------
    def on_open_vault(self) -> None:
        self._open(None)

    def on_open_wishlist(self) -> None:
        """The wishlist file, or a line saying there is not one yet.

        Tk shows a modal for "the file does not exist", which is a dialog to
        dismiss in exchange for information a label could have carried.
        """
        path = getattr(self._report.summary, "wishlist_path", None) \
            if self._report else None
        if path is None or not Path(path).exists():
            self.status.setText("No wishlist file yet.")
            return
        self._open(Path(path))

    def _open(self, path: Optional[Path]) -> None:
        try:
            self.actions.open_folder(path)
        except Exception as exc:                          # noqa: BLE001
            self.status.setText(f"Could not open that: {exc}")


def build_vault_health(window) -> QWidget:
    """Factory for the tab registry."""
    return VaultHealthTab(window)
