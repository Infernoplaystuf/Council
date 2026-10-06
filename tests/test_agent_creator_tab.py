"""The Agent Creator tab (council_qt/tabs/agent_creator.py), offscreen, with a
scripted council and a scripted agent model. Every vault is a tmp_path."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="the Agent Creator tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_core import agent_profiles as ap  # noqa: E402
from council_qt.tabs import REGISTRY  # noqa: E402
from council_qt.tabs.agent_creator import (AgentCreatorActions,  # noqa: E402
                                           AgentCreatorTab, build_agent_creator)

from tests.test_agent_profiles import GOOD_TOOL, Council  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def drive(qapp, tab, seconds=15.0):
    deadline = time.time() + seconds
    while tab._busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert not tab._busy


class Runner:
    def __init__(self):
        self.n = 0

    def chat(self, messages, max_tokens=None):
        self.n += 1
        if self.n == 1:
            return json.dumps({"action": "tool", "tool": "count_rows", "args": {}})
        return json.dumps({"action": "final", "answer": "ok.csv has 3 rows"})


@pytest.fixture
def make_tab(qapp, tmp_path):
    made = []

    def make(verdicts=None, confirm=True):
        vault = tmp_path / "vault"
        (vault / "data_in").mkdir(parents=True, exist_ok=True)
        (vault / "data_in" / "ok.csv").write_text("a\n1\n2\n3\n", encoding="utf-8")
        council = Council([GOOD_TOOL], verdicts or {"judge": [True], "skeptic": [True]})
        actions = AgentCreatorActions(vault, chat=council, runner_factory=Runner,
                                      confirm=lambda *_: confirm)
        tab = AgentCreatorTab(actions=actions)
        made.append(tab)
        return tab, council

    yield make
    deadline = time.time() + 5
    while any(t.name.startswith("agent-creator") and t.is_alive()
              for t in threading.enumerate()) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    for t in made:
        t.close()
        t.deleteLater()
    qapp.processEvents()


def test_registered_as_a_default_tab():
    assert any(f is build_agent_creator for _t, f, _e in REGISTRY)


def test_factory_takes_a_window(qapp):
    view = build_agent_creator(None)
    view.close()
    view.deleteLater()
    qapp.processEvents()


def test_create_and_save_an_agent(qapp, make_tab):
    tab, _ = make_tab()
    tab.on_new()
    tab.name.setText("Parts analyst")
    tab.model_role.setCurrentIndex(tab.model_role.findData("judge"))
    tab.role_checks["skeptic"].setChecked(False)
    tab.tool_checks["graph_neighbors"].setChecked(True)
    tab.steps.setValue(4)
    tab.on_save()
    prof = tab.actions.store.profiles()[0]
    assert prof.name == "Parts analyst" and prof.model_role == "judge"
    assert "skeptic" not in prof.connected_roles
    assert "graph_neighbors" in prof.builtin_tools and prof.max_steps == 4
    # never offered
    assert "write_tool" not in tab.tool_checks and "run_app_tool" not in tab.tool_checks


def test_setting_toggle_is_saved(qapp, make_tab):
    tab, _ = make_tab()
    tab.attach_box.setCurrentIndex(1)
    assert tab.actions.store.tool_attach_mode() == "automatic"
    assert "automatically" in tab.status.text()
    tab.attach_box.setCurrentIndex(0)
    assert tab.actions.store.tool_attach_mode() == "approve"


def test_build_then_approve_a_tool(qapp, make_tab):
    tab, council = make_tab()
    tab.on_new()
    tab.tool_request.setPlainText("count the rows of ok.csv")
    tab.on_build_tool()
    drive(qapp, tab)
    assert "waiting for you" in tab.progress.text()
    tab.requests.setCurrentRow(0)
    shown = tab.request_view.toPlainText()
    assert "judge approves" in shown and "def count_rows" in shown
    assert tab.approve_btn.isEnabled()
    tab.approve_btn.click()
    assert tab.attached.count() == 1
    assert not tab.approve_btn.isEnabled()
    # and the agent can now use it
    tab.goal.setText("how many rows?")
    tab.on_run()
    drive(qapp, tab)
    out = tab.output.toPlainText()
    assert "step 1: count_rows" in out and "ok.csv has 3 rows" in out


def test_automatic_mode_attaches_without_asking(qapp, make_tab):
    tab, _ = make_tab()
    tab.attach_box.setCurrentIndex(1)
    tab.on_new()
    tab.tool_request.setPlainText("count the rows")
    tab.on_build_tool()
    drive(qapp, tab)
    assert tab.attached.count() == 1
    assert "automatic" in tab.attached.item(0).text()


def test_automatic_mode_still_waits_on_an_objection(qapp, make_tab):
    tab, _ = make_tab(verdicts={"judge": [True], "skeptic": [False]})
    tab.attach_box.setCurrentIndex(1)
    tab.on_new()
    tab.tool_request.setPlainText("count the rows")
    tab.on_build_tool()
    drive(qapp, tab)
    assert tab.attached.count() == 0
    tab.requests.setCurrentRow(0)
    assert "skeptic objects" in tab.request_view.toPlainText()
    tab.reject_btn.click()
    assert tab.actions.store.requests()[0].status == "rejected"


def test_delete_asks_first(qapp, make_tab):
    tab, _ = make_tab(confirm=False)
    tab.on_new()
    tab.on_delete()
    assert len(tab.actions.store.profiles()) == 1


def test_damaged_store_disables_the_tab(qapp, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    (vault / ap.STORE_NAME).write_text("{oops", encoding="utf-8")
    tab = AgentCreatorTab(actions=AgentCreatorActions(vault))
    assert "could not be read" in tab.status.text() and not tab.isEnabled()
    assert (vault / ap.STORE_NAME).read_text(encoding="utf-8") == "{oops"
    tab.close()
    tab.deleteLater()
    qapp.processEvents()
