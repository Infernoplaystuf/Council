"""
council_qt.tabs.connections_dialogs — the Connections tab's dialogs.

Kept apart from the tab module because the all-tabs harness resolves every
`self.x` a module connects to against the TAB, and a dialog's own
`self.accept` is not the tab's.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                               QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from ..view import amp


class FoldersDialog(QDialog):
    """The folders the graph reads: the vault's data_in (always), and
    folders outside the vault the user added. Added folders are only READ -
    nothing in them is written, moved or copied into the vault - and
    removing one takes its documents out of the graph at the next rebuild.

    ``add(path)`` / ``remove(id)`` do the change and return an error text or
    ''; ``pick()`` asks for a folder (None = cancelled)."""

    def __init__(self, data_in: str, folders: List[dict], add: Callable[[str], str],
                 remove: Callable[[str], str], pick: Callable[[], Optional[str]],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Folders the graph reads")
        self.resize(560, 360)
        self._add, self._remove, self._pick = add, remove, pick
        outer = QVBoxLayout(self)
        intro = QLabel("The graph reads the vault's documents and any folder added here. "
                       "Added folders are only read: nothing in them is changed, moved or "
                       "copied. Rebuild after a change.")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        self.list = QListWidget()
        outer.addWidget(self.list, 1)
        first = QListWidgetItem(f"{data_in}   (the vault - always read)")
        first.setData(Qt.ItemDataRole.UserRole, None)
        self.list.addItem(first)
        for f in folders:
            self._row(f)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        row = QHBoxLayout()
        self.add_btn = QPushButton("Add a folder…")
        self.add_btn.clicked.connect(self.on_add)
        row.addWidget(self.add_btn)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.on_remove)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        outer.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)
        self.changed = False

    def _row(self, f: dict) -> None:
        text = f["path"] + ("" if f.get("exists", True) else "   (not there now)")
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, f["id"])
        self.list.addItem(item)

    def on_add(self) -> None:
        path = self._pick()
        if not path:
            return
        err = self._add(path)
        if err:
            self.message.setText(err)
            return
        self.changed = True
        self.message.setText(f"Added {path}. Rebuild to read it.")
        self._row({"id": "?", "path": str(path)})

    def on_remove(self) -> None:
        item = self.list.currentItem()
        rid = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not rid or rid == "?":
            self.message.setText("Select an added folder to remove (the vault always stays).")
            return
        err = self._remove(rid)
        if err:
            self.message.setText(err)
            return
        self.changed = True
        self.list.takeItem(self.list.row(item))
        self.message.setText("Removed. Its documents leave the graph at the next rebuild; "
                             "the folder itself is not touched.")


class FieldsDialog(QDialog):
    """Which labels the graph reads, and which drifted labels mean the same.

    Rules start 'proposed' — guesses about this vault's vocabulary — and are
    read only once ticked. Suggested synonyms ('POC' for 'Point of Contact')
    come ticked only at confidence >= 0.85; nothing is assumed."""

    def __init__(self, rules, suggestions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fields the graph reads")
        self.resize(560, 520)
        outer = QVBoxLayout(self)
        intro = QLabel("Tick the labels that mean a person, part or project in "
                       "your documents. Only ticked labels are read.")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        body = QWidget()
        lay = QVBoxLayout(body)
        self.rule_boxes: Dict[str, QCheckBox] = {}
        for etype, title in (("PERSON", "People"), ("PART", "Parts"),
                             ("PROJECT", "Projects")):
            box = QGroupBox(title)
            bl = QVBoxLayout(box)
            for rule, status in rules:
                if rule.type != etype:
                    continue
                extra = " (another name for the project)" if rule.alias_of else \
                    " (the part it replaces)" if rule.supersedes else ""
                cb = QCheckBox(amp(rule.label + extra))
                cb.setChecked(status == "confirmed")
                self.rule_boxes[rule.label] = cb
                bl.addWidget(cb)
            lay.addWidget(box)
        self.syn_boxes: List[tuple] = []
        if suggestions:
            box = QGroupBox("Labels in your documents that may mean the same")
            bl = QVBoxLayout(box)
            for s in suggestions:
                cb = QCheckBox(amp(f"'{s['label']}' means '{s['means']}'  "
                                   f"({s['confidence']:.2f}: {s['why']})"))
                cb.setChecked(bool(s.get("precheck")))
                self.syn_boxes.append((s, cb))
                bl.addWidget(cb)
            lay.addWidget(box)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def choices(self):
        """({label: confirmed?}, [(synonym label, means)])"""
        rules = {lbl: cb.isChecked() for lbl, cb in self.rule_boxes.items()}
        syns = [(s["label"], s["means"]) for s, cb in self.syn_boxes if cb.isChecked()]
        return rules, syns
