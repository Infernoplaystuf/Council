"""
council_qt.widgets.plugin_choice — the Designer's Export app… tick boxes:
one per Typhon plugin, ticked as the project runs it, for the exported
copy's plugin list (Andy, 24 November 2026: "All plugins should be a tick
box to turn them on or off inside both the export from the designer and
inside of Typhon"). Every plugin's code goes with the export either way;
the ticks only say which run when the exported Typhon starts, and its own
Plugins… window changes them later.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QLabel,
                               QScrollArea, QVBoxLayout, QWidget)

from .. import dialogs


class PluginChoiceDialog(QDialog):
    def __init__(self, choices: Sequence[Tuple[str, str, bool]],
                 parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export app — plugins")
        layout = QVBoxLayout(self)
        intro = QLabel("Tick the plugins the exported Typhon runs. Every "
                       "plugin goes with it, so one left off can be ticked "
                       "later in its Plugins… window.", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        inner = QWidget(area)
        rows = QVBoxLayout(inner)
        self.boxes: Dict[str, QCheckBox] = {}
        for pid, name, on in choices:
            box = QCheckBox(f"{name}  ({pid})", inner)
            box.setObjectName(f"export_{pid.replace('.', '_')}")
            box.setChecked(bool(on))
            rows.addWidget(box)
            self.boxes[pid] = box
        rows.addStretch(1)
        area.setWidget(inner)
        layout.addWidget(area, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(460, 420)

    def chosen(self) -> Dict[str, bool]:
        return {pid: box.isChecked() for pid, box in self.boxes.items()}


def ask_plugins(choices: List[Tuple[str, str, bool]],
                parent: Any = None) -> Optional[Dict[str, bool]]:
    """{id: on}, or None when cancelled. Unattended (COUNCIL_NO_DIALOGS):
    the plugins as the project runs them, no dialog."""
    if dialogs.disabled():
        return {pid: bool(on) for pid, _n, on in choices}
    dlg = PluginChoiceDialog(choices, parent)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return dlg.chosen()
