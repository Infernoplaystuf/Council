"""
The Machines & roles window: import from the Apothecary without credentials,
bind a role, save a valid file — and refuse to save an invalid one.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="the Qt shell needs PySide6")

from PySide6.QtWidgets import QApplication, QTableWidgetItem  # noqa: E402

from council_core import node_routing as nr  # noqa: E402
from council_core.model_slots import COUNCIL_ROLES  # noqa: E402
from council_qt.widgets.machine_routing import (  # noqa: E402
    MachineRoutingDialog, apothecary_nodes)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    (v / "node_registry.json").write_text(json.dumps({"nodes": [
        {"name": "pi-kitchen", "host": "192.168.1.50", "ollama_port": 11434,
         "password": "hunter2-secret", "username": "pi"}]}))
    return v


def test_import_reads_only_names_and_addresses(vault):
    nodes = apothecary_nodes(vault)
    assert [(n.name, n.url, n.enabled) for n in nodes] == [
        ("pi-kitchen", "http://192.168.1.50:11434", False)]


def test_bind_a_role_and_save(qapp, vault):
    d = MachineRoutingDialog(vault_dir=vault)
    try:
        assert not d.enabled_box.isChecked()
        d.import_apothecary()
        assert d.machine_names() == ["pi-kitchen"]
        d.machines.cellWidget(0, 2).setChecked(True)
        d.enabled_box.setChecked(True)
        row = list(COUNCIL_ROLES).index("peasant")
        d.roles.cellWidget(row, 1).setCurrentText("pi-kitchen")
        d.roles.cellWidget(row, 2).setCurrentText("fail")
        assert d.save()
        saved = nr.load(vault)
        assert saved.routing_enabled
        assert saved.roles["peasant"].node == "pi-kitchen"
        assert saved.roles["peasant"].fallback == "fail"
        raw = nr.path_for(vault).read_text()
        assert "hunter2" not in raw and "password" not in raw
    finally:
        d.close()
        d.deleteLater()


def test_a_bad_address_is_not_saved(qapp, vault):
    d = MachineRoutingDialog(vault_dir=vault)
    try:
        d._add_machine(nr.Node("box", "not a url", True, 1))
        assert not d.save()
        assert "Not saved" in d.status.text()
        assert not nr.path_for(vault).exists()
        d.machines.setItem(0, 1, QTableWidgetItem("http://10.0.0.9:11434"))
        assert d.save()
    finally:
        d.close()
        d.deleteLater()


def test_reopening_shows_what_was_saved(qapp, vault):
    nr.save(vault, nr.Routing(True, {"box": nr.Node(
        "box", "http://10.0.0.9:11434", True, 3)},
        {"intern": nr.Binding("box", "here")}))
    d = MachineRoutingDialog(vault_dir=vault)
    try:
        assert d.enabled_box.isChecked()
        assert d.machines.cellWidget(0, 3).value() == 3
        row = list(COUNCIL_ROLES).index("intern")
        assert d.roles.cellWidget(row, 1).currentText() == "box"
    finally:
        d.close()
        d.deleteLater()
