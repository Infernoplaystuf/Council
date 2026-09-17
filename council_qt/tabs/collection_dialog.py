"""
council_qt.tabs.collection_dialog — building a collection, with Discover.

A collection groups files that belong to one job even though nothing about
their names or folders says so. The dialog is small but it carries the one
feature that makes collections worth having: Discover, which proposes members
and says WHY it proposed each.

The reason matters more than the ranking. "invoices_q3.csv — value match,
relationship via Job ID" tells the user whether to trust the suggestion;
a bare list makes them open every file to find out. So the reasons are shown
inline and not hidden behind a tooltip.

Discover is deterministic — filename slugs, a value lookup in the data index,
and expansion along shared join columns. No model, despite what the Qt Vault
tab claimed for three commits.
"""
from __future__ import annotations

import threading
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                               QDialogButtonBox, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout)


class CollectionDialog(QDialog):
    """Name a collection, choose its files, save it."""

    def __init__(self, actions, existing: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.existing = existing
        self.result_message = ""
        self._reasons = {}

        self.setWindowTitle("Edit collection" if existing else "New collection")
        self.resize(640, 460)
        self._build()

        if existing:
            self.name_edit.setText(existing)
            for rel in actions.collection_files(existing):
                self.files.addItem(rel)

    # -- layout ---------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)

        grid = QGridLayout()
        grid.addWidget(QLabel("Name:"), 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Job Blue")
        grid.addWidget(self.name_edit, 0, 1)
        self.discover_btn = QPushButton("🔎 Discover")
        self.discover_btn.clicked.connect(self.on_discover)
        grid.addWidget(self.discover_btn, 0, 2)
        outer.addLayout(grid)

        outer.addWidget(QLabel("Files:"))
        self.files = QListWidget()
        self.files.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.files.currentItemChanged.connect(self.on_row_changed)
        outer.addWidget(self.files, 1)

        row = QHBoxLayout()
        self.add_box = QComboBox()
        self.add_box.setEditable(True)
        self.add_box.addItems(self.actions.candidate_files())
        self.add_box.setMinimumWidth(320)
        row.addWidget(self.add_box, 1)
        add = QPushButton("➕ Add")
        add.clicked.connect(self.on_add)
        row.addWidget(add)
        remove = QPushButton("✗ Remove")
        remove.clicked.connect(self.on_remove)
        row.addWidget(remove)
        outer.addLayout(row)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        outer.addWidget(self.detail)

        buttons = QDialogButtonBox(QDialogButtonBox.Save
                                   | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.on_save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # -- the list -------------------------------------------------------
    def current_files(self) -> List[str]:
        return [self.files.item(i).text() for i in range(self.files.count())]

    def on_add(self) -> None:
        value = self.add_box.currentText().strip()
        if value and value not in self.current_files():
            self.files.addItem(value)
        self.add_box.setCurrentText("")

    def on_remove(self) -> None:
        for item in self.files.selectedItems():
            self.files.takeItem(self.files.row(item))

    def on_row_changed(self, current: Optional[QListWidgetItem], _previous=None):
        """Show why a proposed file was proposed.

        Without this the score is invisible and Discover is a list of guesses
        the user has to verify by hand.
        """
        if current is None:
            return
        reason = self._reasons.get(current.text())
        self.detail.setText(f"{current.text()} — {reason}" if reason else "")

    # -- discover -------------------------------------------------------
    def on_discover(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self.detail.setText("Name the collection first.")
            return
        self.discover_btn.setEnabled(False)
        self.detail.setText("Looking…")

        def work() -> None:
            result = self.actions.propose_members(name)

            def show() -> None:
                self.discover_btn.setEnabled(True)
                if not result.ok:
                    self.detail.setText(result.message)
                    return
                have = set(self.current_files())
                added = 0
                for rel, _score, reasons in result.rows:
                    self._reasons[rel] = ", ".join(reasons)
                    if rel not in have:
                        self.files.addItem(rel)
                        have.add(rel)
                        added += 1
                self.detail.setText(
                    f"Proposed {len(result.rows)} file(s) ({added} new) — "
                    "select a row to see why; remove/add, then Save.")

            self._to_ui(show)

        threading.Thread(target=work, name="collection-discover",
                         daemon=True).start()

    def _to_ui(self, fn) -> None:
        """Hand a worker's result back to the GUI thread.

        Through the parent tab's bridge when there is one, exactly as the tab
        itself does — a worker touching a widget is undefined behaviour in Qt
        and Qt will not warn.
        """
        parent = self.parent()
        bridge = getattr(parent, "bridge", None)
        if bridge is not None:
            bridge.call_on_ui(fn)
        else:
            fn()

    # -- saving ---------------------------------------------------------
    def on_save(self) -> None:
        name = self.name_edit.text().strip()
        result = self.actions.save_collection(
            name, self.current_files(),
            renaming_from=self.existing if self.existing != name else None)
        if not result.ok:
            self.detail.setText(result.message)
            return
        self.result_message = result.message
        self.accept()
