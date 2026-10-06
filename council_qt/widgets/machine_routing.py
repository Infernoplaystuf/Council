"""
council_qt.widgets.machine_routing — "Machines & roles": which role answers
on which machine (council_core.node_routing).

Three things, all off until the user sets them:
  * the master switch, "Send roles to other machines";
  * the machines: a name, the Ollama address (http://host:11434), whether it
    is enabled, and how many calls it may run at once;
  * the bindings: a role, its machine, and what happens when that machine
    does not answer ("here": answer on this PC with the role's own model;
    "fail": show the error).

"Import from Apothecary" copies each registered node's NAME and ADDRESS
(host + Ollama port) — never its credentials, which the same file holds —
and adds them disabled, so importing sends nothing anywhere.

Saving validates the whole file (node_routing.parse) before writing; a bad
address or an unknown machine is reported and nothing is written.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from council_core import model_slots, node_routing as nr, paths

from .. import theme
from ..view import ViewHelpers

HERE = "(this PC)"


def apothecary_nodes(vault_dir: Path) -> List[nr.Node]:
    """Names and Ollama addresses from node_registry.json — only those two
    fields; the file also holds SSH passwords."""
    try:
        data = json.loads((Path(vault_dir) / "node_registry.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for d in (data.get("nodes") or []) if isinstance(data, dict) else []:
        if not isinstance(d, dict) or not d.get("name") or not d.get("host"):
            continue
        port = d.get("ollama_port") or 11434
        out.append(nr.Node(str(d["name"]).strip(),
                           f"http://{str(d['host']).strip()}:{int(port)}",
                           enabled=False, parallel=1))
    return out


class MachineRoutingDialog(ViewHelpers, QDialog):
    def __init__(self, parent=None, vault_dir: Optional[Path] = None):
        super().__init__(parent)
        self.window = parent
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self._tokens = theme.tokens("dark")
        self.setWindowTitle("Machines & roles")
        self.resize(820, 620)
        self.routing = nr.load(self.vault_dir)
        self._build()
        self._fill()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        v = QVBoxLayout(self)
        intro = QLabel(
            "Send a role's calls to another machine's Ollama. Nothing leaves "
            "this PC unless routing is on, the machine is enabled, and a "
            "role is bound to it. A bound role uses its own model (set in "
            "the Models tab), which that machine must have installed.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        v.addWidget(intro)
        self.enabled_box = QCheckBox("Send roles to other machines")
        v.addWidget(self.enabled_box)

        v.addWidget(QLabel("Machines"))
        self.machines = QTableWidget(0, 4)
        self.machines.setHorizontalHeaderLabels(
            ["Name", "Ollama address", "Enabled", "Calls at once"])
        self.machines.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.machines.verticalHeader().setVisible(False)
        v.addWidget(self.machines, 1)
        row = QHBoxLayout()
        self._button(row, "Add machine", lambda: self._add_machine(
            nr.Node("", "http://", False, 1)))
        self._button(row, "Remove selected", self._remove_machine)
        self._button(row, "Import from Apothecary", self.import_apothecary)
        row.addStretch(1)
        v.addLayout(row)

        v.addWidget(QLabel("Roles"))
        self.roles = QTableWidget(len(model_slots.COUNCIL_ROLES), 3)
        self.roles.setHorizontalHeaderLabels(
            ["Role", "Answers on", "If it does not answer"])
        self.roles.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.roles.verticalHeader().setVisible(False)
        v.addWidget(self.roles, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)
        self._button(bottom, "Save", self.save)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        bottom.addWidget(close)
        v.addLayout(bottom)

    def _fill(self) -> None:
        self.enabled_box.setChecked(self.routing.routing_enabled)
        self.machines.setRowCount(0)
        for node in self.routing.nodes.values():
            self._add_machine(node)
        self._fill_roles()

    def _add_machine(self, node: nr.Node) -> None:
        r = self.machines.rowCount()
        self.machines.insertRow(r)
        self.machines.setItem(r, 0, QTableWidgetItem(node.name))
        self.machines.setItem(r, 1, QTableWidgetItem(node.url))
        box = QCheckBox()
        box.setChecked(node.enabled)
        self.machines.setCellWidget(r, 2, box)
        spin = QSpinBox()
        spin.setRange(1, 16)
        spin.setValue(node.parallel)
        self.machines.setCellWidget(r, 3, spin)
        self._fill_roles()

    def _remove_machine(self) -> None:
        rows = sorted({i.row() for i in self.machines.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.machines.removeRow(r)
        self._fill_roles()

    def machine_names(self) -> List[str]:
        out = []
        for r in range(self.machines.rowCount()):
            item = self.machines.item(r, 0)
            if item and item.text().strip():
                out.append(item.text().strip())
        return out

    def _fill_roles(self) -> None:
        """Rebuild the role rows, keeping each role's current choice."""
        if not hasattr(self, "roles"):
            return
        chosen = {}
        for r in range(self.roles.rowCount()):
            role_item = self.roles.item(r, 0)
            combo = self.roles.cellWidget(r, 1)
            fb = self.roles.cellWidget(r, 2)
            if role_item and combo:
                chosen[role_item.text()] = (combo.currentText(),
                                            fb.currentText() if fb else "here")
        names = self.machine_names()
        for r, role in enumerate(model_slots.COUNCIL_ROLES):
            self.roles.setItem(r, 0, QTableWidgetItem(role))
            binding = self.routing.roles.get(role)
            node, fallback = chosen.get(role, (
                binding.node if binding else HERE,
                binding.fallback if binding else "here"))
            combo = QComboBox()
            combo.addItems([HERE] + names)
            combo.setCurrentText(node if node in names else HERE)
            self.roles.setCellWidget(r, 1, combo)
            fb = QComboBox()
            fb.addItems(list(nr.FALLBACKS))
            fb.setCurrentText(fallback)
            self.roles.setCellWidget(r, 2, fb)

    # ------------------------------------------------------------------
    def import_apothecary(self) -> None:
        have = set(self.machine_names())
        added = 0
        for node in apothecary_nodes(self.vault_dir):
            if node.name not in have:
                self._add_machine(node)
                added += 1
        self.status.setText(
            f"Imported {added} machine(s), disabled. Enable the ones to use."
            if added else "Nothing new in the Apothecary.")

    def collect(self) -> nr.Routing:
        nodes = {}
        for r in range(self.machines.rowCount()):
            name = (self.machines.item(r, 0) or QTableWidgetItem()).text()
            url = (self.machines.item(r, 1) or QTableWidgetItem()).text()
            if not name.strip():
                continue
            nodes[name.strip()] = nr.Node(
                name.strip(), url.strip().rstrip("/"),
                self.machines.cellWidget(r, 2).isChecked(),
                self.machines.cellWidget(r, 3).value())
        roles = {}
        for r, role in enumerate(model_slots.COUNCIL_ROLES):
            node = self.roles.cellWidget(r, 1).currentText()
            if node != HERE and node in nodes:
                roles[role] = nr.Binding(
                    node, self.roles.cellWidget(r, 2).currentText())
        return nr.Routing(self.enabled_box.isChecked(), nodes, roles)

    def save(self) -> bool:
        try:
            routing = self.collect()
            nr.save(self.vault_dir, routing)
        except nr.RoutingError as exc:
            self.status.setText(f"Not saved: {exc}")
            return False
        self.routing = routing
        bound = len(routing.roles)
        self.status.setText(
            "Saved. " + (f"{bound} role(s) answer on other machines."
                         if routing.routing_enabled and bound else
                         "Every role answers on this PC."))
        return True


__all__ = ["MachineRoutingDialog", "apothecary_nodes"]
