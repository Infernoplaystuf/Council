"""
"Find these fields in my files…": where each field of a Ledger template is
in the files the knowledge graph read (its Contents layer,
council_core/kg_contents.field_lookup). Step 4 of the Contents layer (user,
2026-10-26): a template is matched to its sources in one look - which files
have a 'Hardness' column, under which labels ('Hardness (HRC)' in 120, 'HRC'
in 23), how many values and what they look like - before an Extract reads
every workbook again. Read only; not modal.
"""
from __future__ import annotations

from typing import Any, Dict, List

from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

from council_core import kg_contents


class TemplateFieldsDialog(QDialog):
    def __init__(self, template_name: str, found: List[Dict[str, Any]], parent=None,
                 graph_note: str = ""):
        super().__init__(parent)
        self.setWindowTitle(f"Template fields in my files — {template_name}")
        self.setModal(False)
        self.resize(900, 520)
        lay = QVBoxLayout(self)
        n_found = sum(1 for f in found if f["labels"])
        self.heading = QLabel(
            f"{n_found} of {len(found)} field(s) of the template are in the files the "
            "knowledge graph read. Open a field to see the labels it goes by and the files "
            "that hold it." + (f" {graph_note}" if graph_note else ""))
        self.heading.setWordWrap(True)
        lay.addWidget(self.heading)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Template field / label in the files / file",
                                   "What it holds"])
        self.tree.setColumnWidth(0, 420)
        lay.addWidget(self.tree, 1)
        for item in found:
            top = QTreeWidgetItem([item["name"], "" if item["labels"] else
                                   "not found in the files the graph read"])
            self.tree.addTopLevelItem(top)
            for lab in item["labels"]:
                how = "" if lab["exact"] else "  (a near match - check it)"
                li = QTreeWidgetItem([f"as '{lab['label']}'{how}",
                                      kg_contents.lookup_summary(lab)])
                top.addChild(li)
                for pl in lab["places"]:
                    where = pl["file"] + (f" › {pl['sheet']}" if pl.get("sheet") else "")
                    li.addChild(QTreeWidgetItem([where, f"{pl['n_cells']:,} value(s)"]))
                if lab["files"] > len(lab["places"]):
                    li.addChild(QTreeWidgetItem(
                        [f"… and {lab['files'] - len(lab['places'])} more file(s)", ""]))
            if item["labels"]:
                top.setText(1, kg_contents.lookup_summary(item["labels"][0]))
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(close)
        lay.addLayout(row)
