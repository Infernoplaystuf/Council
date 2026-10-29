"""
The Ledger's Groups window (council_core.ledger_groups): sub-groups of its
parts - by part name, date or source folder, and ticked in or out by hand -
each with its own fields (tracked, not tracked, or Yes / No) and a workbook
of its own beside the full one. Saved with the Ledger (its saved-Ledger
file); "Write workbooks" writes them now, and Apply writes them each time.
Not modal.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from council_core import ledger_groups as lg
from sample_ledger import LedgerError, ParentCSV, sample_sort_key


class GroupsDialog(QDialog):
    def __init__(self, csv: str, parent=None, review_parts: Sequence[str] = (),
                 log: Optional[Callable[[str], None]] = None,
                 on_write: Optional[Callable[[], None]] = None, template: str = ""):
        super().__init__(parent)
        self.csv, self.template = csv, template
        self.log = log or (lambda _s: None)
        self.on_write = on_write
        ledger = ParentCSV(csv)
        self.fields: List[str] = [f.name for f in ledger.schema.fields] if ledger.schema else []
        self.facts: Dict[str, lg.PartFacts] = lg.facts_from_ledger(ledger)
        for p in review_parts:                   # parts only the review has yet
            if p and p.lower() not in {k.lower() for k in self.facts}:
                self.facts[p] = lg.PartFacts()
        self.groups: List[lg.Group] = lg.load_groups(csv)
        self.current: Optional[lg.Group] = None
        self.dirty = False
        self._browse_folder = self._pick_folder
        self.setWindowTitle(f"Groups — {Path(csv).stem}")
        self.setModal(False)
        self.resize(1100, 640)
        self._build()
        self._fill_groups()
        if self.groups:
            self.group_list.setCurrentRow(0)
        self._refresh()

    # ── layout ──────────────────────────────────────────────────────────
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        help_ = QLabel(
            "A group is a slice of this Ledger's parts with a workbook of its own, beside "
            "the full one: each part's filled template on a sheet, in numerical order. Take "
            "its parts by part name, date or source folder, tick parts in or out, and say "
            "which fields it tracks. Values of a field a part's groups do not track are not "
            "asked about in review.")
        help_.setWordWrap(True)
        lay.addWidget(help_)
        body = QHBoxLayout()
        left = QVBoxLayout()
        self.group_list = QListWidget()
        self.group_list.currentRowChanged.connect(self._picked)
        left.addWidget(self.group_list)
        row = QHBoxLayout()
        new = QPushButton("New group")
        new.clicked.connect(lambda: self.new_group())
        row.addWidget(new)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(self.delete_group)
        row.addWidget(self.delete_btn)
        left.addLayout(row)
        body.addLayout(left, 1)

        self.form = QWidget()
        grid = QGridLayout(self.form)
        grid.addWidget(QLabel("Name:"), 0, 0)
        self.name = QLineEdit()
        self.name.editingFinished.connect(self._name_edited)
        grid.addWidget(self.name, 0, 1)
        grid.addWidget(QLabel("Parts by:"), 1, 0)
        self.by = QComboBox()
        for key, words in lg.BY.items():
            self.by.addItem(words, key)
        self.by.currentIndexChanged.connect(self._rule_edited)
        grid.addWidget(self.by, 1, 1)
        self.rule = QStackedWidget()
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText("ABC*, *-R1   or a part-name pattern such as @@@#@@###")
        self.pattern.editingFinished.connect(self._rule_edited)
        self.rule.addWidget(self.pattern)
        dates = QWidget()
        drow = QHBoxLayout(dates)
        drow.setContentsMargins(0, 0, 0, 0)
        self.date_field = QComboBox()
        self.date_field.addItems(self.fields)
        guess = next((f for f in self.fields if "date" in f.lower()), "")
        if guess:
            self.date_field.setCurrentText(guess)
        self.date_field.currentIndexChanged.connect(self._rule_edited)
        self.date_from, self.date_to = QLineEdit(), QLineEdit()
        self.date_from.setPlaceholderText("from (e.g. 2023-01-01)")
        self.date_to.setPlaceholderText("to (e.g. 2023-12-31)")
        for w in (self.date_from, self.date_to):
            w.editingFinished.connect(self._rule_edited)
        drow.addWidget(self.date_field)
        drow.addWidget(self.date_from)
        drow.addWidget(self.date_to)
        self.rule.addWidget(dates)
        folders = QWidget()
        frow = QHBoxLayout(folders)
        frow.setContentsMargins(0, 0, 0, 0)
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("a folder this Ledger read workbooks from")
        self.folder.editingFinished.connect(self._rule_edited)
        frow.addWidget(self.folder)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_clicked)
        frow.addWidget(browse)
        self.rule.addWidget(folders)
        self.rule.addWidget(QLabel("Tick the parts below."))
        grid.addWidget(self.rule, 2, 1)
        self.count = QLabel()
        grid.addWidget(self.count, 3, 0, 1, 2)
        lists = QHBoxLayout()
        self.parts = QListWidget()
        self.parts.itemChanged.connect(self._part_ticked)
        lists.addWidget(self.parts, 1)
        self.field_table = QTableWidget(len(self.fields), 3)
        self.field_table.setHorizontalHeaderLabels(["Field", "In this group", "Counts as Yes"])
        self.field_table.horizontalHeaderItem(2).setToolTip(
            "For a Yes / No field: the values that are Yes, comma-separated (pass, ok). "
            "Blank: any value is Yes. An empty cell is always No.")
        self.field_table.horizontalHeader().setStretchLastSection(True)
        self._mode_boxes: List[QComboBox] = []
        for r, f in enumerate(self.fields):
            it = QTableWidgetItem(f)
            it.setToolTip(f)
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.field_table.setItem(r, 0, it)
            box = QComboBox()
            for key, words in lg.FIELD_MODES.items():
                box.addItem(words, key)
            box.currentIndexChanged.connect(lambda _i, r=r: self._field_edited(r))
            self.field_table.setCellWidget(r, 1, box)
            self._mode_boxes.append(box)
            self.field_table.setItem(r, 2, QTableWidgetItem(""))
        self.field_table.resizeColumnToContents(0)
        self.field_table.resizeColumnToContents(1)
        # a long field name is cut, so 'Counts as Yes' stays in sight
        self.field_table.setColumnWidth(0, min(self.field_table.columnWidth(0), 300))
        self.field_table.itemChanged.connect(lambda it: self._field_edited(it.row())
                                             if it.column() == 2 else None)
        lists.addWidget(self.field_table, 2)
        grid.addLayout(lists, 4, 0, 1, 2)
        grid.setRowStretch(4, 1)
        body.addWidget(self.form, 4)
        lay.addLayout(body, 1)

        row = QHBoxLayout()
        self.status = QLabel()
        row.addWidget(self.status, 1)
        save = QPushButton("Save groups")
        save.clicked.connect(self.on_save)
        row.addWidget(save)
        self.write_btn = QPushButton("Write workbooks")
        self.write_btn.setToolTip("Save, then write the full workbook and one per group "
                                  "from the parent CSV (Apply writes them too)")
        self.write_btn.clicked.connect(self.on_write_clicked)
        self.write_btn.setEnabled(self.on_write is not None)
        row.addWidget(self.write_btn)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        lay.addLayout(row)

    # ── groups ──────────────────────────────────────────────────────────
    def _fill_groups(self) -> None:
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for g in self.groups:
            self.group_list.addItem(g.name)
        self.group_list.blockSignals(False)

    def new_group(self, name: str = "") -> lg.Group:
        taken = {g.name.lower() for g in self.groups}
        if not name:
            n = len(self.groups) + 1
            while f"group {n}" in taken:
                n += 1
            name = f"Group {n}"
        g = lg.Group(name=name)
        self.groups.append(g)
        self.dirty = True
        self._fill_groups()
        self.group_list.setCurrentRow(len(self.groups) - 1)
        return g

    def delete_group(self) -> None:
        if self.current is None:
            return
        self.groups.remove(self.current)
        self.current = None
        self.dirty = True
        self._fill_groups()
        if self.groups:
            self.group_list.setCurrentRow(0)
        self._refresh()

    def _picked(self, row: int) -> None:
        self.current = self.groups[row] if 0 <= row < len(self.groups) else None
        self._show()

    def _show(self) -> None:
        g = self.current
        self.form.setEnabled(g is not None)
        self.delete_btn.setEnabled(g is not None)
        if g is None:
            self._refresh()
            return
        for w in (self.name, self.by, self.pattern, self.date_field, self.date_from,
                  self.date_to, self.folder):
            w.blockSignals(True)
        self.name.setText(g.name)
        self.by.setCurrentIndex(max(0, self.by.findData(g.by)))
        self.rule.setCurrentIndex(list(lg.BY).index(g.by) if g.by in lg.BY else 0)
        self.pattern.setText(g.pattern)
        if g.date_field:
            self.date_field.setCurrentText(g.date_field)
        self.date_from.setText(g.date_from)
        self.date_to.setText(g.date_to)
        self.folder.setText(g.folder)
        for w in (self.name, self.by, self.pattern, self.date_field, self.date_from,
                  self.date_to, self.folder):
            w.blockSignals(False)
        self.field_table.blockSignals(True)
        for r, f in enumerate(self.fields):
            box = self._mode_boxes[r]
            box.blockSignals(True)
            box.setCurrentIndex(box.findData(g.mode(f)))
            box.blockSignals(False)
            self.field_table.item(r, 2).setText(g.yes_no.get(f, ""))
        self.field_table.blockSignals(False)
        self._refresh()

    def _name_edited(self) -> None:
        if self.current is not None and self.name.text().strip():
            self.current.name = self.name.text().strip()
            self.dirty = True
            self.group_list.item(self.groups.index(self.current)).setText(self.current.name)

    def _rule_edited(self, *_a) -> None:
        g = self.current
        if g is None:
            return
        g.by = self.by.currentData() or "part"
        self.rule.setCurrentIndex(list(lg.BY).index(g.by))
        g.pattern = self.pattern.text().strip()
        g.date_field = self.date_field.currentText()
        g.date_from, g.date_to = self.date_from.text().strip(), self.date_to.text().strip()
        g.folder = self.folder.text().strip()
        self.dirty = True
        self._refresh()

    def set_rule(self, by: str, *, pattern: str = "", date_field: str = "",
                 date_from: str = "", date_to: str = "", folder: str = "") -> None:
        """As choosing and typing in the form does."""
        self.pattern.setText(pattern)
        if date_field:
            self.date_field.setCurrentText(date_field)
        self.date_from.setText(date_from)
        self.date_to.setText(date_to)
        self.folder.setText(folder)
        self.by.setCurrentIndex(self.by.findData(by))
        self._rule_edited()

    def _pick_folder(self) -> str:
        return QFileDialog.getExistingDirectory(self, "Source folder of the group",
                                                self.folder.text().strip())

    def _browse_clicked(self) -> None:
        folder = self._browse_folder()
        if folder:
            self.folder.setText(folder)
            self._rule_edited()

    # ── fields ──────────────────────────────────────────────────────────
    def _field_edited(self, r: int) -> None:
        g = self.current
        if g is None:
            return
        f = self.fields[r]
        mode = self._mode_boxes[r].currentData()
        g.off = [x for x in g.off if x != f]
        g.yes_no.pop(f, None)
        if mode == lg.OFF:
            g.off.append(f)
        elif mode == lg.YES_NO:
            g.yes_no[f] = self.field_table.item(r, 2).text().strip()
        self.dirty = True

    def set_field(self, fname: str, mode: str, yes: str = "") -> None:
        """As the field's row does."""
        r = self.fields.index(fname)
        self.field_table.item(r, 2).setText(yes)
        box = self._mode_boxes[r]
        box.setCurrentIndex(box.findData(mode))
        self._field_edited(r)

    # ── parts ───────────────────────────────────────────────────────────
    def members(self) -> List[str]:
        return lg.members(self.current, self.facts, self.csv) if self.current else []

    def _refresh(self) -> None:
        inside = {p.lower() for p in self.members()}
        self.parts.blockSignals(True)
        self.parts.clear()
        for p in sorted(self.facts, key=sample_sort_key):
            it = QListWidgetItem(p)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if p.lower() in inside else Qt.Unchecked)
            self.parts.addItem(it)
        self.parts.setEnabled(self.current is not None)
        self.parts.blockSignals(False)
        self.count.setText(f"{len(inside)} of {len(self.facts)} part(s) in this group"
                           if self.current else "Make a group with New group.")

    def tick(self, part: str, on: bool) -> None:
        """As ticking a part does."""
        for i in range(self.parts.count()):
            it = self.parts.item(i)
            if it.text() == part:
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)
                return

    def _part_ticked(self, it: QListWidgetItem) -> None:
        g = self.current
        if g is None:
            return
        part, on = it.text(), it.checkState() == Qt.Checked
        key = part.lower()
        g.include = [p for p in g.include if p.lower() != key]
        g.exclude = [p for p in g.exclude if p.lower() != key]
        ruled = lg.by_rule(g, part, self.facts.get(part),
                           lg.folder_names(self.csv, g.folder) if g.by == "folder" else None)
        if on and not ruled:
            g.include.append(part)
        elif not on and ruled:
            g.exclude.append(part)
        self.dirty = True
        self._refresh()

    # ── saving ──────────────────────────────────────────────────────────
    def on_save(self) -> bool:
        self._name_edited()
        try:
            lg.save_groups(self.csv, self.groups, self.template)
        except (LedgerError, OSError) as e:
            self.status.setText(f"Not saved: {e}")
            return False
        self.dirty = False
        self.status.setText(f"{len(self.groups)} group(s) saved with the Ledger.")
        self.log(f"Groups: {len(self.groups)} saved ("
                 + ", ".join(f"{g.name}: {len(lg.members(g, self.facts, self.csv))} parts"
                             for g in self.groups) + ")")
        return True

    def on_write_clicked(self) -> bool:
        if self.on_write is None or not self.on_save():
            return False
        self.on_write()
        return True

    def reject(self) -> None:                                # noqa: D401
        if self.dirty:
            self.on_save()                     # nothing lost by closing
        super().reject()
