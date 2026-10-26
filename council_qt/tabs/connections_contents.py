"""The Connections tab's Contents view: everything fed into the graph.

User, 2026-10-26: "ALL of the input data should be in the knowledge graph
for example an excel file has the head node with the file name then lets say
stat headers/labels and then that data is connected to the labels". This is
that tree, read from the graph's Contents layer (council_core/kg_contents.py):

    file › sheet › header or label › values › every place a value is › its row

Children are read when a branch is opened (a 100k-row workbook is one
click away, not loaded up front). A numeric column is one line with its
range and lists its cells on request; a text column lists its
``kg_contents.DRAWN_VALUES`` commonest values; both say how many more there
are (search finds them). A text value is one value in every file, so opening
it lists every file, sheet and cell that has it.

With text in the tab's search box the tree shows what matches instead:
values, headers and labels, files, and the cells holding a number typed.
Selecting a cell asks the tab to preview it (``preview_requested``).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from council_core import kg_contents
from council_core import knowledge_graph as kgm

ROLE_KIND = Qt.ItemDataRole.UserRole
ROLE_DATA = Qt.ItemDataRole.UserRole + 1
#: True once a branch's children were read
ROLE_LOADED = Qt.ItemDataRole.UserRole + 2

KIND_WORDS = {"column": "column", "label": "label", "loose": "cells under no label"}


def _plural(n: int, one: str, many: str = "") -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


class ContentsView(QWidget):
    """The Contents tree. ``graph()`` gives the tab's open KnowledgeGraph
    (or None)."""

    #: (vault-relative path, locator, where in words, the cell's text)
    preview_requested = Signal(str, object, str, str)

    def __init__(self, graph: Callable[[], Optional[kgm.KnowledgeGraph]], parent=None):
        super().__init__(parent)
        self._graph = graph
        self._query = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["File / sheet / label / value", "What it holds"])
        self.tree.setColumnWidth(0, 340)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.currentItemChanged.connect(self._on_current)
        lay.addWidget(self.tree, 1)
        #: what search() found last, for the tab title
        self.matches = 0

    # ── filling ──
    def refresh(self) -> None:
        """Read again from the graph (after a rebuild or a graph change)."""
        self.search(self._query)

    def search(self, text: str) -> int:
        """Show what matches ``text`` (or, empty, every file). Returns how
        many matches there are (0 with no text)."""
        self._query = (text or "").strip()
        self.tree.clear()
        self.matches = 0
        kg = self._graph()
        if kg is None:
            self.summary.setText("")
            return 0
        n = kg.contents_counts()
        if not n["files"]:
            self.summary.setText("Nothing is in the contents yet: ⟳ Build graph reads every "
                                 "CSV and workbook into it, every cell under its header or "
                                 "label.")
            return 0
        self.summary.setText(
            f"{_plural(n['files'], 'file')}, {_plural(n['fields'], 'header or label', 'headers and labels')}, "
            f"{_plural(n['cells'], 'value')} ({n['values']:,} different texts). "
            "Open a branch to see what is under it; search finds any value.")
        if not self._query:
            for f in kg.contents_files():
                self.tree.addTopLevelItem(self._file_item(f))
            return 0
        got = kg.search_contents(self._query)
        groups = [("values", "Values", self._value_item),
                  ("fields", "Headers and labels", self._field_item),
                  ("files", "Files", self._file_item),
                  ("numbers", "Numbers", self._number_item)]
        for key, title, make in groups:
            hits = got.get(key) or []
            if not hits:
                continue
            self.matches += len(hits)
            g = QTreeWidgetItem([f"{title} ({len(hits)})", ""])
            g.setData(0, ROLE_LOADED, True)
            self.tree.addTopLevelItem(g)
            for h in hits:
                g.addChild(make(h))
            g.setExpanded(True)
        if not self.matches:
            self.tree.addTopLevelItem(QTreeWidgetItem([f"Nothing in the contents matches "
                                                       f"'{self._query}'", ""]))
        return self.matches

    def _lazy(self, item: QTreeWidgetItem, kind: str, data: Dict[str, Any]) -> QTreeWidgetItem:
        item.setData(0, ROLE_KIND, kind)
        item.setData(0, ROLE_DATA, data)
        item.setData(0, ROLE_LOADED, False)
        item.addChild(QTreeWidgetItem(["…", ""]))
        return item

    def _leaf(self, item: QTreeWidgetItem, kind: str, data: Dict[str, Any]) -> QTreeWidgetItem:
        item.setData(0, ROLE_KIND, kind)
        item.setData(0, ROLE_DATA, data)
        item.setData(0, ROLE_LOADED, True)
        return item

    def _file_item(self, f: Dict[str, Any]) -> QTreeWidgetItem:
        it = QTreeWidgetItem([f"📄 {f.get('file') or f['path']}",
                              _plural(int(f.get("cells") or 0), "value")])
        it.setToolTip(0, f.get("file") or f["path"])
        return self._lazy(it, "file", {"document_id": f["document_id"], "path": f["path"]})

    def _field_item(self, f: Dict[str, Any], with_file: bool = True) -> QTreeWidgetItem:
        where = ""
        if with_file and f.get("file"):
            where = f"  —  {f['file']}" + (f" › {f['sheet']}" if f.get("sheet") else "")
        icon = "🔢" if f.get("numeric") else "🏷"
        it = QTreeWidgetItem([f"{icon} {f['label']}{where}",
                              f.get("summary") or kg_contents.summary(f)])
        it.setToolTip(0, f"{KIND_WORDS.get(f['kind'], f['kind'])}: {f['label']}")
        return self._lazy(it, "field", {"field_id": f["id"]})

    def _value_item(self, v: Dict[str, Any]) -> QTreeWidgetItem:
        n = int(v.get("count") or 0)
        it = QTreeWidgetItem([f"▪ {v['text']}", _plural(n, "place")])
        return self._lazy(it, "value", {"value_id": v["value_id"], "text": v["text"]})

    def _number_item(self, v: Dict[str, Any]) -> QTreeWidgetItem:
        it = QTreeWidgetItem([f"# {v['text']}", _plural(int(v["count"]), "cell")])
        return self._lazy(it, "number", {"number": v["number"], "text": v["text"]})

    def _place_item(self, pl: Dict[str, Any], show_value: bool = False) -> QTreeWidgetItem:
        where = kgm.locator_text(pl["locator"])
        head = f"{pl['file']}" + (f" › {pl['sheet']}" if pl.get("sheet") else "")
        text = f"{pl['text']}  —  " if show_value else ""
        it = QTreeWidgetItem([f"{text}{head} › {pl['label']}", where])
        it.setToolTip(0, f"{head} · {where}")
        return self._lazy(it, "place", pl)

    # ── opening a branch ──
    def _expand(self, item: QTreeWidgetItem) -> None:
        if item.data(0, ROLE_LOADED):
            return
        item.setData(0, ROLE_LOADED, True)
        item.takeChildren()
        kg = self._graph()
        if kg is None:
            return
        kind, data = item.data(0, ROLE_KIND), item.data(0, ROLE_DATA) or {}
        if kind == "file":
            sheets = kg.contents_sheets(data["document_id"])
            if len(sheets) == 1 and not sheets[0]["sheet"]:      # a CSV: no sheet level
                self._add_fields(item, kg, data["document_id"], "")
                return
            for sh in sheets:
                it = QTreeWidgetItem([f"▦ {sh['sheet'] or '(sheet)'}",
                                      f"{_plural(sh['fields'], 'header or label', 'headers and labels')}, "
                                      f"{_plural(int(sh['cells'] or 0), 'value')}"])
                item.addChild(self._lazy(it, "sheet", {"document_id": data["document_id"],
                                                       "sheet": sh["sheet"]}))
        elif kind == "sheet":
            self._add_fields(item, kg, data["document_id"], data["sheet"])
        elif kind == "field":
            self._add_values(item, kg, data["field_id"])
        elif kind == "value":
            self._add_places(item, kg.value_places(value_id=data["value_id"]),
                             int((kg.content_value(data["value_id"]) or {}).get("count") or 0))
        elif kind == "number":
            self._add_places(item, kg.value_places(number=data["number"]), 0)
        elif kind == "place":
            for c in kg.row_cells(data["field_id"], data["row"]):
                mark = "▶ " if c["field_id"] == data["field_id"] else ""
                item.addChild(QTreeWidgetItem([f"{mark}{c['label']}: {c['text']}",
                                               f"column {kg_contents.col_letter(c['col'])}"]))

    def _add_fields(self, item, kg, did: str, sheet: str) -> None:
        for f in kg.contents_fields(did, sheet):
            item.addChild(self._field_item(f, with_file=False))

    def _add_values(self, item, kg, fid: int) -> None:
        f = kg.contents_field(fid)
        if f is None:
            return
        same = kg.same_fields(fid)
        if same:
            g = QTreeWidgetItem([f"↔ Same label in {_plural(len(same), 'other place')}", ""])
            g.setData(0, ROLE_LOADED, True)
            for s in same:
                g.addChild(self._field_item(s))
            item.addChild(g)
        got = kg.field_values(fid)
        for v in got["values"]:
            if got["numeric"]:
                loc = kg_contents.locator(f, v["row"], v["col"])
                place = self._place_from(kg, f, v, loc)
                it = QTreeWidgetItem([f"{v['text']}", kgm.locator_text(loc)])
                item.addChild(self._lazy(it, "place", place))
            elif v.get("value_id") is not None:
                n = int(v.get("count") or 0)
                total = int((kg.content_value(v["value_id"]) or {}).get("count") or n)
                det = _plural(n, "time") + (f" here, {total:,} in all" if total > n else "")
                it = QTreeWidgetItem([f"▪ {v['text']}", det])
                item.addChild(self._lazy(it, "value", {"value_id": v["value_id"],
                                                       "text": v["text"]}))
            else:
                loc = kg_contents.locator(f, v["row"], v["col"])
                item.addChild(self._lazy(QTreeWidgetItem([v["text"], kgm.locator_text(loc)]),
                                         "place", self._place_from(kg, f, v, loc)))
        if got["more"]:
            item.addChild(QTreeWidgetItem([f"… and {got['more']:,} more (search to find "
                                           "one)", ""]))

    def _place_from(self, kg, f, v, loc) -> Dict[str, Any]:
        row = kg.db.execute("SELECT path FROM documents WHERE id=?",
                            (f["document_id"],)).fetchone()
        path = row[0] if row else ""
        return {"field_id": f["id"], "row": v["row"], "col": v["col"], "text": v["text"],
                "path": path, "file": kg.display_path(path) if path else "",
                "document_id": f["document_id"], "sheet": f["sheet"], "label": f["label"],
                "kind": f["kind"], "locator": loc}

    def _add_places(self, item, places: List[Dict[str, Any]], total: int) -> None:
        files = {p["document_id"] for p in places}
        if len(files) > 1:
            item.setText(1, f"{item.text(1)} in {len(files)} files")
        for pl in places:
            item.addChild(self._place_item(pl, show_value=item.data(0, ROLE_KIND) == "number"))
        if total > len(places):
            item.addChild(QTreeWidgetItem([f"… and {total - len(places):,} more", ""]))

    # ── selecting ──
    def selected(self) -> Optional[Dict[str, Any]]:
        it = self.tree.currentItem()
        if it is None or it.data(0, ROLE_KIND) != "place":
            return None
        return it.data(0, ROLE_DATA)

    def _on_current(self, item, _prev=None) -> None:
        if item is None or item.data(0, ROLE_KIND) != "place":
            return
        pl = item.data(0, ROLE_DATA) or {}
        if pl.get("path"):
            self.preview_requested.emit(pl["path"], pl["locator"],
                                        kgm.locator_text(pl["locator"]), pl.get("text", ""))
