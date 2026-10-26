"""
A saved Ledger's runs (council_core.ledger_saved): each run's name, when
and how it ran, how many files it read (and of them new or changed), its
values and how many it left to review. A name is changed by typing over it;
"Filled template…" opens the run's filled template window, "Open review"
its review workbook. Not modal.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from council_core import ledger_saved

COLUMNS = ["Name", "When", "How", "Files read", "New or changed", "Values",
           "Pre-accepted", "To review"]


class LedgerRunsDialog(QDialog):
    def __init__(self, csv: str, parent=None,
                 open_filled: Optional[Callable[[str], None]] = None,
                 opener: Optional[Callable[[str], None]] = None,
                 on_renamed: Optional[Callable[[], None]] = None):
        super().__init__(parent)
        self.csv = csv
        self.open_filled = open_filled
        self.opener = opener
        self.on_renamed = on_renamed
        self.runs: List[ledger_saved.Run] = []
        self.setModal(False)
        self.resize(980, 420)
        lay = QVBoxLayout(self)
        self.heading = QLabel()
        lay.addWidget(self.heading)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._renamed)
        self.table.itemSelectionChanged.connect(self._refresh_buttons)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        self.filled_btn = QPushButton("Filled template…")
        self.filled_btn.setToolTip("The run's template filled in per part, to click through")
        self.filled_btn.clicked.connect(self.on_filled)
        row.addWidget(self.filled_btn)
        self.review_btn = QPushButton("Open review")
        self.review_btn.clicked.connect(self.on_open_review)
        row.addWidget(self.review_btn)
        row.addStretch(1)
        self.status = QLabel("Type over a name to rename the run.")
        row.addWidget(self.status)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        lay.addLayout(row)
        self.reload()

    def reload(self) -> None:
        led = ledger_saved.load(self.csv)
        self.runs = list(reversed(led.runs)) if led else []      # newest first
        title = led.title if led else Path(self.csv).stem
        self.setWindowTitle(f"Runs — {title}")
        self.heading.setText(
            f"<b>{title}</b>" + (f" · {led.project}" if led and led.project else "")
            + f" — {len(self.runs)} run(s)")
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.runs))
        for r, run in enumerate(self.runs):
            cells = [run.name, run.at.replace("T", " "), run.how, str(run.files),
                     str(run.new_files) if run.kind == "update" else "",
                     str(run.values), str(run.pre_accepted), str(run.to_review)]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if c:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(r, c, it)
        self.table.blockSignals(False)
        if self.runs:
            self.table.selectRow(0)
        self._refresh_buttons()

    def current(self) -> Optional[ledger_saved.Run]:
        rows = self.table.selectionModel().selectedRows()
        return self.runs[rows[0].row()] if rows else None

    def _refresh_buttons(self) -> None:
        run = self.current()
        there = run is not None and Path(run.review).is_file()
        self.filled_btn.setEnabled(there and self.open_filled is not None)
        self.review_btn.setEnabled(there and self.opener is not None)

    def _renamed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        run = self.runs[item.row()]
        name = item.text().strip()
        if not name or name == run.name:
            return
        if ledger_saved.rename_run(self.csv, run.id, name):
            run.name = name
            self.status.setText(f"Renamed to \"{name}\".")
            if self.on_renamed is not None:
                self.on_renamed()

    def rename(self, row: int, name: str) -> None:
        """As typing over the run's name does."""
        self.table.item(row, 0).setText(name)

    def on_filled(self) -> None:
        run = self.current()
        if run is not None and self.open_filled is not None:
            self.open_filled(run.review)

    def on_open_review(self) -> None:
        run = self.current()
        if run is None or self.opener is None:
            return
        try:
            self.opener(run.review)
        except Exception as e:                              # noqa: BLE001
            self.status.setText(f"could not open {Path(run.review).name}: {e}")
