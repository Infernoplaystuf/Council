"""
council_qt.tabs.connections_dialogs — the Connections tab's dialogs.

Kept apart from the tab module because the all-tabs harness resolves every
`self.x` a module connects to against the TAB, and a dialog's own
`self.accept` is not the tab's.
"""
from __future__ import annotations

from typing import Dict, List

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                               QGroupBox, QLabel, QScrollArea, QVBoxLayout,
                               QWidget)

from ..view import amp


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
