"""
The Ledger tab's "Link by clicking…" window (user, 2026-10-21: "Maybe
instead of highlighting we can add some easier linking system?").

The template on the left, a sample workbook or deck on the right. Click the
template cell where a value goes (or pick its field from the list), then the
sample cell it comes from: a link appears below as a plain sentence. "From
the file name" and "Look it up here" say the field is part of the file's
name, or looked up in a column of another table. For a deck, the Text boxes
tab says which table each text box goes with. Nothing is kept until Save;
Save teaches the next Extract (council_core.ledger_linking.save). Colour
highlights in the workbooks keep working beside it.

Reads run on worker threads; their results come back through Qt signals.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QSplitter,
                               QTableView, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from council_core import ledger_linking as ll
from sample_ledger import LedgerError

from ..view import hand_to_qt

#: the tints: a template cell that is a field; the field picked now; a cell
#: linked in this window; a sample cell in a table Council found
FIELD_TINT, PICKED_TINT, LINKED_TINT, TABLE_TINT = "#DDEBF7", "#9BC2E6", "#FFE699", "#F2F2F2"
SOURCES = ("Workbooks, decks and documents (*.xlsx *.xlsm *.xls *.xlsb *.pptx *.pptm *.csv "
           "*.docx *.txt *.md *.pdf)")


def _letters(c: int) -> str:
    from openpyxl.utils import get_column_letter
    return get_column_letter(max(1, c))


class SheetModel(QAbstractTableModel):
    """One sheet's cells, read only, with tints."""

    def __init__(self, sheet: ll.ViewSheet, parent=None):
        super().__init__(parent)
        self.sheet = sheet
        self.tints: Dict[Tuple[int, int], str] = {}

    def rowCount(self, parent=QModelIndex()) -> int:                # noqa: N802
        return 0 if parent.isValid() else self.sheet.rows

    def columnCount(self, parent=QModelIndex()) -> int:             # noqa: N802
        return 0 if parent.isValid() else self.sheet.cols

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        rc = (index.row() + 1, index.column() + 1)
        if role == Qt.DisplayRole:
            v = self.sheet.cells.get(rc)
            return None if v is None else str(v)
        if role == Qt.BackgroundRole:
            k = self.tints.get(rc)
            return QBrush(QColor(k)) if k else None
        if role == Qt.ForegroundRole and rc in self.tints:
            return QBrush(QColor("#000000"))                # readable on the tint, any theme
        if role == Qt.ToolTipRole:
            f = self.sheet.fields.get(rc)
            return f if f and not f.startswith("T") else None
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole:
            return None
        return _letters(section + 1) if orientation == Qt.Horizontal else str(section + 1)

    def set_tints(self, tints: Dict[Tuple[int, int], str]) -> None:
        self.beginResetModel()
        self.tints = tints
        self.endResetModel()


class DrawTable(QTableView):
    """A sheet's table that hands over the cells drawn over (one click is a
    drawing of one cell) when the mouse is let go."""
    drawn = Signal(object)                  # [(row, col), ...], 1-based, reading order

    def mouseReleaseEvent(self, event) -> None:            # noqa: N802
        super().mouseReleaseEvent(event)
        cells = sorted((i.row() + 1, i.column() + 1) for i in self.selectedIndexes())
        if cells:
            self.drawn.emit(cells)


class LinkDialog(QDialog):
    _template_read = Signal(object, str)    # (TemplateView or None, error)
    _sample_read = Signal(object, str)      # (SampleView or None, error)

    def __init__(self, csv_path: str, sources: Sequence[str] = (), parent=None,
                 log: Optional[Callable[[str], None]] = None,
                 on_saved: Optional[Callable[[List[str]], None]] = None,
                 browse: Optional[Callable[[QWidget], str]] = None):
        super().__init__(parent)
        self.setWindowTitle("Link by clicking")
        self.resize(1300, 800)
        self.csv_path = csv_path
        self.log = log or (lambda _s: None)
        self.on_saved = on_saved
        self._browse = browse
        self.template: Optional[ll.TemplateView] = None
        self.sample: Optional[ll.SampleView] = None
        self.picked: str = ""                    # the field the next sample click links
        #: the fields of the template cells drawn over, in reading order: the
        #: next sample drawing of as many cells links them one to one
        self.picked_many: List[str] = []
        self.links: List[ll.Link] = []
        self.busy = False
        self._template_models: List[SheetModel] = []
        self._sample_models: List[SheetModel] = []
        self._lookup_mode = False
        self._build(sources)
        hand_to_qt(self)          # PySide6 6.12: see view.hand_to_qt
        self._template_read.connect(self._got_template)
        self._sample_read.connect(self._got_sample)
        self._read_template()
        if self.sample_choice.count():
            self._read_sample(self.sample_choice.currentText())

    # ── layout ──────────────────────────────────────────────────────────
    def _build(self, sources: Sequence[str]) -> None:
        outer = QVBoxLayout(self)
        self.help = QLabel(
            "1. Click the template cell where a value goes (or pick its field in the list). "
            "2. Click the sample cell it comes from: a column's header or any of its values, "
            "or a labelled value. Or drag over several template cells, then over as many "
            "sample cells: they link one to one, in order. Each link is listed below; Save "
            "teaches the next Extract.")
        self.help.setWordWrap(True)
        outer.addWidget(self.help)
        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("<b>Template</b> — where a value goes"))
        self.find_field = QLineEdit()
        self.find_field.setPlaceholderText("Find a field…")
        self.find_field.textChanged.connect(self._filter_fields)
        lv.addWidget(self.find_field)
        self.field_list = QListWidget()
        self.field_list.setMaximumHeight(130)
        self.field_list.currentTextChanged.connect(self._field_from_list)
        lv.addWidget(self.field_list)
        self.template_tabs = QTabWidget()
        lv.addWidget(self.template_tabs, 1)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("<b>Sample</b> — where it comes from"))
        row = QHBoxLayout()
        self.sample_choice = QComboBox()
        self.sample_choice.setEditable(False)
        for s in sources:
            self.sample_choice.addItem(str(s))
        self.sample_choice.activated.connect(
            lambda _i: self._read_sample(self.sample_choice.currentText()))
        row.addWidget(self.sample_choice, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse_sample)
        row.addWidget(b)
        rv.addLayout(row)
        row = QHBoxLayout()
        self.file_name_btn = QPushButton("From the file name…")
        self.file_name_btn.setToolTip("The picked field is part of this sample's file name")
        self.file_name_btn.clicked.connect(self._link_file_name)
        row.addWidget(self.file_name_btn)
        self.lookup_btn = QPushButton("Look it up here")
        self.lookup_btn.setCheckable(True)
        self.lookup_btn.setToolTip("Then click the column of a list to look the picked field "
                                   "up in (each record's name finds its row)")
        self.lookup_btn.toggled.connect(self._lookup_toggled)
        row.addWidget(self.lookup_btn)
        row.addStretch(1)
        rv.addLayout(row)
        self.sample_tabs = QTabWidget()
        rv.addWidget(self.sample_tabs, 1)
        split.addWidget(right)
        split.setSizes([600, 700])
        outer.addWidget(split, 1)

        self.status = QLabel("Reading the template…")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        outer.addWidget(QLabel("<b>Links</b> (not kept until Save)"))
        self.link_list = QListWidget()
        self.link_list.setMaximumHeight(140)
        self.link_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        outer.addWidget(self.link_list)
        row = QHBoxLayout()
        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self._remove_links)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        self.save_btn = QPushButton("Save links")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.on_save)
        row.addWidget(self.save_btn)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        outer.addLayout(row)
        self._refresh_buttons()

    def _table_view(self, model: SheetModel, on_draw) -> QTableView:
        view = DrawTable()
        view.setModel(model)
        view.setSelectionMode(QAbstractItemView.ContiguousSelection)
        view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        view.horizontalHeader().setDefaultSectionSize(90)
        view.verticalHeader().setDefaultSectionSize(22)
        view.drawn.connect(lambda cells: on_draw(model, cells))
        return view

    # ── reading (worker threads) ────────────────────────────────────────
    def _read_template(self) -> None:
        def work() -> None:
            try:
                self._template_read.emit(ll.template_view(self.csv_path), "")
            except LedgerError as e:
                self._template_read.emit(None, str(e))
            except Exception:                              # noqa: BLE001
                self._template_read.emit(None, traceback.format_exc(limit=3))
        threading.Thread(target=work, name="ledger-link-template", daemon=True).start()

    def _read_sample(self, path: str) -> None:
        if not path:
            return
        self.busy = True
        self.status.setText(f"Reading {Path(path).name}…")
        self._refresh_buttons()

        def work() -> None:
            try:
                self._sample_read.emit(ll.sample_view(path, self.csv_path), "")
            except LedgerError as e:
                self._sample_read.emit(None, str(e))
            except Exception:                              # noqa: BLE001
                self._sample_read.emit(None, traceback.format_exc(limit=3))
        threading.Thread(target=work, name="ledger-link-sample", daemon=True).start()

    def _got_template(self, view, err: str) -> None:
        if view is None:
            self.status.setText(f"Could not read the template: {err}")
            return
        self.template = view
        self.field_list.clear()
        for f in view.fields:
            self.field_list.addItem(f)
        self.template_tabs.clear()
        self._template_models = []
        for sheet in view.sheets:
            m = SheetModel(sheet, self)
            self._template_models.append(m)
            self.template_tabs.addTab(self._table_view(m, self.on_template_drawn), sheet.name)
        self._retint()
        if not self.busy:
            self.status.setText("Click the template cell where a value goes.")

    def _got_sample(self, view, err: str) -> None:
        self.busy = False
        self._refresh_buttons()
        if view is None:
            self.status.setText(f"Could not read that sample: {err}")
            return
        self.sample = view
        self.sample_tabs.clear()
        self._sample_models = []
        for sheet in view.sheets:
            m = SheetModel(sheet, self)
            self._sample_models.append(m)
            self.sample_tabs.addTab(self._table_view(m, self.on_sample_drawn), sheet.name)
        if view.pairings:
            self.sample_tabs.addTab(self._pairings_table(view), "Text boxes")
        self._retint()
        self.status.setText(f"{Path(view.path).name}: {len(view.sheets)} sheet(s). "
                            + ("Pick a field, then click where it comes from."
                               if not self.picked else
                               f"Click where {self.picked} comes from."))

    def _pairings_table(self, view: ll.SampleView) -> QWidget:
        """A deck's text boxes, each with the table it goes with."""
        box = QWidget()
        v = QVBoxLayout(box)
        v.addWidget(QLabel("Which table each text box goes with. A guess (by where it sits) "
                           "is marked; pick the right table to link it."))
        t = QTableWidget(len(view.pairings), 4)
        t.setHorizontalHeaderLabels(["Slide", "Text box", "Goes with", "Told by"])
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pairing_boxes: List[QComboBox] = []
        for i, p in enumerate(view.pairings):
            t.setItem(i, 0, QTableWidgetItem(str(p.slide)))
            t.setItem(i, 1, QTableWidgetItem(p.text))
            combo = QComboBox()
            combo.addItem("Every table on the slide", [])
            for n, name in enumerate(p.choices, start=1):
                combo.addItem(name, [n])
            combo.setCurrentIndex(p.numbers[0] if len(p.numbers) == 1 else 0)
            combo.currentIndexChanged.connect(
                lambda _i, p=p, combo=combo: self._pair(p, combo.currentData()))
            self.pairing_boxes.append(combo)
            t.setCellWidget(i, 2, combo)
            told = p.describe().rsplit("(", 1)[-1].rstrip(")")
            t.setItem(i, 3, QTableWidgetItem(told))
        v.addWidget(t)
        self.pairing_table = t
        return box

    # ── clicks ──────────────────────────────────────────────────────────
    def _filter_fields(self, text: str) -> None:
        low = text.strip().lower()
        for i in range(self.field_list.count()):
            item = self.field_list.item(i)
            item.setHidden(bool(low) and low not in item.text().lower())

    def _field_from_list(self, name: str) -> None:
        if name:
            self.pick(name)

    def pick(self, name: str) -> None:
        self.picked = name
        self.picked_many = [name]
        self._retint()
        self._refresh_buttons()
        self.status.setText(f"{name}: now click where it comes from in the sample"
                            + (" (the column to look it up in)" if self._lookup_mode else ""))

    def on_template_drawn(self, model: SheetModel, cells) -> None:
        """One cell: its field. Several: their fields in reading order, for
        the next sample drawing of as many cells."""
        if len(cells) == 1:
            self.on_template_click(model, *cells[0])
            return
        fields = ll.template_fields(model.sheet, cells)
        if not fields:
            self.status.setText("None of those cells is one the Ledger fills: draw over "
                                "the tinted cells where values go.")
            return
        self.picked, self.picked_many = fields[0], fields
        self._retint()
        self._refresh_buttons()
        distinct = list(dict.fromkeys(fields))
        self.status.setText(
            f"{len(fields)} template cell(s): {', '.join(distinct[:6])}"
            + (" …" if len(distinct) > 6 else "")
            + f". Now draw over {len(distinct) if len(distinct) > 1 else len(fields)} "
            "sample cell(s), in the same order.")

    def on_sample_drawn(self, model: SheetModel, cells) -> None:
        if len(cells) == 1 and len(set(self.picked_many)) <= 1:
            self.on_sample_click(model, *cells[0])
            return
        if self.sample is None or self.template is None:
            return
        if not self.picked_many:
            self.status.setText("First draw over the template cells (or pick the field) "
                                "these values go to.")
            return
        if self._lookup_mode:
            self.status.setText("A lookup takes one column: click one cell of it.")
            return
        if len(set(self.picked_many)) == 1 and len(self.picked_many) == 1:
            self.status.setText(f"One template field ({self.picked}) and {len(cells)} sample "
                                "cells: draw over as many template cells, or click one "
                                "sample cell.")
            return
        try:
            links = ll.link_cells(self.template.kind, self.picked_many, self.sample,
                                  model.sheet.name, cells)
        except LedgerError as e:
            self.status.setText(f"Not linked: {e}")
            return
        for link in links:
            self.add_link(link)
        self.status.setText(f"Linked {len(links)}: " + "; ".join(k.sentence for k in links)[:300])

    def on_template_click(self, model: SheetModel, r: int, c: int) -> None:
        name = model.sheet.fields.get((r, c))
        if not name:
            self.status.setText("That cell is not one the Ledger fills: click a cell where a "
                                "value goes (they are tinted).")
            return
        items = self.field_list.findItems(name, Qt.MatchExactly)
        if items:
            self.field_list.blockSignals(True)
            self.field_list.setCurrentItem(items[0])
            self.field_list.blockSignals(False)
        self.pick(name)

    def on_sample_click(self, model: SheetModel, r: int, c: int) -> None:
        if self.sample is None or self.template is None:
            return
        if not self.picked:
            self.status.setText("First click the template cell (or pick the field) this "
                                "value goes to.")
            return
        sheet = model.sheet.name
        try:
            if self._lookup_mode:
                if self.template.kind != "records":
                    raise LedgerError("a lookup is for a table Ledger")
                link = ll.link_lookup(self.picked, self.sample, sheet, r, c)
                self.lookup_btn.setChecked(False)
            else:
                link = ll.link_cell(self.template.kind, self.picked, self.sample, sheet, r, c)
        except LedgerError as e:
            self.status.setText(f"Not linked: {e}")
            return
        link.detail["_at"] = (self.sample.path, sheet, r, c)
        self.add_link(link)

    def _link_file_name(self) -> None:
        if not self.picked or self.sample is None:
            return
        stem = Path(self.sample.path).stem
        value, ok = QInputDialog.getText(
            self, "From the file name",
            f"The part of '{stem}' that is {self.picked} (delete the rest):", text=stem)
        if not ok or not value.strip():
            return
        try:
            self.add_link(ll.link_file_name(self.picked, Path(self.sample.path).name,
                                            value.strip()))
        except LedgerError as e:
            self.status.setText(f"Not linked: {e}")

    def _lookup_toggled(self, on: bool) -> None:
        self._lookup_mode = on
        if on:
            self.status.setText("Click the column of the list to look "
                                f"{self.picked or 'the field'} up in.")

    def _pair(self, pairing, numbers) -> None:
        if self.sample is None:
            return
        self.add_link(ll.link_pairing(self.sample, pairing, numbers or []))

    def add_link(self, link: ll.Link) -> None:
        self.links = [x for x in self.links if x.key != link.key] + [link]
        self._show_links()
        self.status.setText(f"Linked: {link.sentence}. Pick the next field, or Save.")
        self._retint()

    def _show_links(self) -> None:
        self.link_list.clear()
        for k in self.links:
            QListWidgetItem(k.sentence, self.link_list)
        self._refresh_buttons()

    def _remove_links(self) -> None:
        rows = sorted({self.link_list.row(i) for i in self.link_list.selectedItems()},
                      reverse=True)
        for r in rows:
            del self.links[r]
        self._show_links()
        self._retint()

    # ── tints ───────────────────────────────────────────────────────────
    def _retint(self) -> None:
        linked_fields = {k.field for k in self.links}
        for m in self._template_models:
            tints = {}
            for rc, f in m.sheet.fields.items():
                tints[rc] = PICKED_TINT if f in (self.picked_many or [self.picked]) else (
                    LINKED_TINT if f in linked_fields else FIELD_TINT)
            m.set_tints(tints)
        for m in self._sample_models:
            tints = {rc: TABLE_TINT for rc in m.sheet.fields}
            for k in self.links:
                at = k.detail.get("_at")
                if at and self.sample and at[0] == self.sample.path and at[1] == m.sheet.name:
                    tints[(at[2], at[3])] = LINKED_TINT
            m.set_tints(tints)

    def _refresh_buttons(self) -> None:
        records = self.template is not None and self.template.kind == "records"
        self.file_name_btn.setEnabled(records and bool(self.picked) and self.sample is not None)
        self.lookup_btn.setEnabled(records and bool(self.picked) and self.sample is not None)
        self.remove_btn.setEnabled(bool(self.links))
        self.save_btn.setEnabled(bool(self.links) and not self.busy)

    # ── save ────────────────────────────────────────────────────────────
    def _browse_sample(self) -> None:
        path = self._browse(self) if self._browse else QFileDialog.getOpenFileName(
            self, "Pick a sample workbook or deck", "", SOURCES)[0]
        if not path:
            return
        if self.sample_choice.findText(path) < 0:
            self.sample_choice.addItem(path)
        self.sample_choice.setCurrentText(path)
        self._read_sample(path)

    def on_save(self) -> None:
        if not self.links:
            return
        try:
            said = ll.save(self.csv_path, self.links)
        except LedgerError as e:
            self.status.setText(f"Not saved: {e}")
            return
        except Exception as e:                             # noqa: BLE001
            self.status.setText(f"Not saved: {e}")
            self.log(traceback.format_exc())
            return
        self.log("Links saved — the next Extract reads them:\n"
                 + "\n".join(f"  {s}" for s in said))
        self.links = []
        self._show_links()
        self._retint()
        self.status.setText(f"Saved {len(said)} link(s). The next Extract uses them.")
        if self.on_saved is not None:
            self.on_saved(said)
