"""
The Agents tab: council_core.agents_status, council_core.rag_jobs, the Sage
scoring fix, and the Qt tab with its two embedded panels.

Written against docs/qt_migration/remaining_tabs_requirements.md §agents. The
defect tests are the point: each would pass against a faithful translation of
the Tk tab only if the defect had been carried across.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from council_core import agents_status, rag_jobs


# ============================================================
# agents_status
# ============================================================

def test_the_board_has_the_tk_rows_in_the_tk_order():
    labels = [label for label, _m in agents_status.SUBSYSTEMS]
    assert labels[0].startswith("Coder Agent")
    assert labels[5] == "Vault RAG (ChromaDB)"
    assert len(labels) == 8


def test_a_missing_module_is_unavailable_and_says_why():
    ok, problem = agents_status.importable("no_such_module_anywhere_xyz")
    assert not ok
    assert "ModuleNotFoundError" in problem


def test_a_present_module_is_available():
    assert agents_status.importable("json") == (True, "")


@pytest.mark.parametrize("msg, tag", [
    ("tests PASS", "result"),
    ("✓ done", "result"),
    ("step FAIL", "fail"),
    ("RAG index Error: boom", "fail"),
    ("Vault indexed: 3 files", "phase"),
    ("PASS after an error", "result"),         # result wins, as in Tk
])
def test_classify_is_the_tk_rule(msg, tag):
    assert agents_status.classify("x", msg) == tag


def test_toggles_default_on_exactly_when_available():
    rows = [agents_status.Row("c", "coder_agent", True),
            agents_status.Row("i", "intern_agent", False),
            agents_status.Row("r", "vault_rag", True)]
    t = agents_status.AgentToggles.defaults(rows)
    assert (t.use_coder_agent, t.use_intern_agent, t.use_rag) == \
        (True, False, True)


# ============================================================
# rag_jobs
# ============================================================

class _Stats:
    def __init__(self, files=3, chunks=9):
        self.total_files, self.total_chunks = files, chunks
        self.backend = "fake"


class _FakeRag:
    built = 0

    def __init__(self, vault_dir, chroma_dir):
        type(self).built += 1
        self.forces = []
        self.gate = None

    def index(self, force=False):
        self.forces.append(force)
        if self.gate is not None:
            self.gate.wait(3.0)
        return _Stats()

    def collection_count(self):
        return 42


@pytest.fixture
def fake_rag_cls():
    cls = type("FakeRag", (_FakeRag,), {"built": 0})
    return cls


def test_a_user_reindex_forces(tmp_path, fake_rag_cls):
    """Defect 1: `index()` with no argument skips for 300 s after startup and
    reports zero files as a success."""
    index = rag_jobs.RagIndex(tmp_path, factory=fake_rag_cls)
    out = index.reindex()
    assert out.ok and out.files == 3 and out.chunks == 9
    assert index.rag.forces == [True]
    assert "3 files, 9 chunks (fake)" in out.message


def test_count_is_none_until_something_is_built(tmp_path, fake_rag_cls):
    index = rag_jobs.RagIndex(tmp_path, factory=fake_rag_cls)
    assert index.count() is None
    index.reindex()
    assert index.count() == 42


def test_a_second_reindex_while_one_runs_is_refused(tmp_path, fake_rag_cls):
    index = rag_jobs.RagIndex(tmp_path, factory=fake_rag_cls)
    index.ensure().gate = threading.Event()
    first = threading.Thread(target=index.reindex)
    first.start()
    deadline = time.time() + 3
    while not index.busy and time.time() < deadline:
        time.sleep(0.005)
    second = index.reindex()
    index.rag.gate.set()
    first.join(3)
    assert second.skipped and not second.ok
    assert index.rag.forces == [True]


def test_concurrent_first_use_builds_one_rag(tmp_path, fake_rag_cls):
    """The Tk check-then-set built two VaultRAGs on two quick clicks."""
    index = rag_jobs.RagIndex(tmp_path, factory=fake_rag_cls)
    threads = [threading.Thread(target=index.ensure) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3)
    assert fake_rag_cls.built == 1


def test_a_failing_build_is_a_message_not_an_exception(tmp_path):
    def boom(**_kw):
        raise RuntimeError("chromadb exploded")
    out = rag_jobs.RagIndex(tmp_path, factory=boom).reindex()
    assert not out.ok and "chromadb exploded" in out.message


def test_for_vault_shares_one_index(tmp_path):
    assert rag_jobs.for_vault(tmp_path) is rag_jobs.for_vault(tmp_path / ".")


# ============================================================
# the Sage scoring fix (defect 5)
# ============================================================

def test_the_confidence_boost_applies_once_per_record(tmp_path):
    """Two facts matching the same four query words. The medium one is denser
    (fewer other terms), so its TF is ~1.22x higher. A single 1.2x boost must
    not lift the high-confidence fact past it — the old per-term compounding
    (~1.34x overall here) did."""
    import sage_agent
    kb = sage_agent.SageKnowledge(tmp_path)
    pad = " ".join(f"pad{i:02d}" for i in range(5))
    kb.add_fact("hightopic", f"alpha beta gamma delta {pad}", "src", "high")
    kb.add_fact("medtopic", "alpha beta gamma delta pad90 pad91 pad92",
                "src", "medium")
    ranked = [r["topic"] for r in kb.search_relevant("alpha beta gamma delta")]
    assert ranked == ["medtopic", "hightopic"]


# ============================================================
# The Qt side
# ============================================================

pytest.importorskip("PySide6", reason="the Agents tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.agents import (AgentsActions, AgentsTab,  # noqa: E402
                                    build_agents)
from council_qt.widgets.sage_tuning import SageTuningPanel  # noqa: E402
from council_qt.widgets.vault_agent_panel import (  # noqa: E402
    VaultAgentPanel, summary_lines)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def pump(qapp, until, seconds=5.0):
    deadline = time.time() + seconds
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert until(), "timed out"


def _rows(**avail):
    return [agents_status.Row(label, module, avail.get(module, True))
            for label, module in agents_status.SUBSYSTEMS]


class FakeActions(AgentsActions):
    def __init__(self, vault_dir, rows, models=None):
        super().__init__(vault_dir, models=models,
                         rag=rag_jobs.RagIndex(vault_dir,
                                               factory=type("R", (_FakeRag,),
                                                            {"built": 0})))
        self._rows = rows
        self.availability_threads = []

    def availability(self):
        self.availability_threads.append(threading.current_thread().name)
        return self._rows


class Models:
    writer = "writer-model"
    coder = "coder-model"
    judge = intern = peasant = None


@pytest.fixture
def make_tab(qapp, tmp_path):
    made = []

    def make(rows=None, models=Models()):
        actions = FakeActions(tmp_path, rows or _rows(), models=models)
        view = AgentsTab(actions=actions)
        pump(qapp, lambda: not view._busy and view._panels_built)
        made.append(view)
        return view

    yield make
    for view in made:
        view.deleteLater()
    qapp.processEvents()


def test_the_factory_takes_a_window(qapp):
    view = build_agents(None)
    assert isinstance(view, AgentsTab)
    pump(qapp, lambda: not view._busy, seconds=60)
    view.deleteLater()


def test_the_tab_is_advanced_only():
    from council_qt.tabs import ADVANCED_REGISTRY, REGISTRY
    assert any(f is build_agents for _t, f, _e in ADVANCED_REGISTRY)
    assert not any(f is build_agents for _t, f, _e in REGISTRY)


def test_the_board_is_read_off_the_gui_thread(make_tab):
    tab = make_tab()
    assert tab.actions.availability_threads == ["agents-status"]


def test_the_board_shows_every_row(make_tab):
    tab = make_tab(_rows(intern_agent=False))
    texts = [tab.status_layout.itemAt(i).widget().text()
             for i in range(tab.status_layout.count())]
    assert len(texts) == 8
    assert texts[1].startswith("✗ Intern")
    assert texts[0].startswith("✓ Coder")


def test_an_unavailable_toggle_is_disabled_and_off(make_tab):
    tab = make_tab(_rows(intern_agent=False))
    assert not tab.checks["use_intern_agent"].isEnabled()
    t = tab.toggles()
    assert t.use_coder_agent and not t.use_intern_agent and t.use_rag


def test_the_toggles_snapshot_follows_the_checkboxes(make_tab):
    tab = make_tab()
    tab.checks["use_rag"].setChecked(False)
    assert tab.toggles().use_rag is False


def test_reindex_forces_and_shows_the_count(qapp, make_tab):
    tab = make_tab()
    tab.on_reindex()
    pump(qapp, lambda: "Chunks indexed" in tab.rag_count.text())
    assert tab.actions.rag.rag.forces == [True]
    assert tab.rag_count.text() == "Chunks indexed: 42"
    assert "3 files, 9 chunks" in tab.log.toPlainText()
    assert tab.reindex_btn.isEnabled()


def test_reindex_is_disabled_without_rag(make_tab):
    tab = make_tab(_rows(vault_rag=False))
    assert not tab.reindex_btn.isEnabled()


def test_log_event_is_safe_from_a_worker(qapp, make_tab):
    tab = make_tab()
    t = threading.Thread(target=tab.log_event, args=("probe", "✓ from a worker"))
    t.start()
    t.join(3)
    pump(qapp, lambda: "from a worker" in tab.log.toPlainText())
    assert "[probe] ✓ from a worker" in tab.log.toPlainText()


def test_the_sage_panel_needs_no_sage_model(make_tab):
    """Defect 3: with no "sage" personality pinned, Tk showed a separator and
    nothing else. The panel only needs the knowledge store."""
    tab = make_tab(models=Models())          # Models has no sage slot at all
    assert isinstance(tab.sage_panel, SageTuningPanel)


def test_missing_modules_say_so_instead_of_panels(make_tab):
    tab = make_tab(_rows(sage_agent=False, vault_agent=False))
    assert tab.sage_panel is None and tab.vault_panel is None
    assert tab.lower.count() == 2


def test_one_splitter_holds_the_tab(make_tab):
    """Defect 4: two expanding siblings split the height evenly."""
    tab = make_tab()
    assert tab.split.count() == 2 and tab.lower.count() == 2


# -- Sage tuning ---------------------------------------------------------

@pytest.fixture
def sage(qapp, tmp_path):
    import sage_agent
    confirms = []
    panel = SageTuningPanel(sage_agent.SageKnowledge(tmp_path / "sage"),
                            confirm=lambda *a, **k: confirms.pop(0))
    panel.confirms = confirms
    yield panel
    panel.deleteLater()
    qapp.processEvents()


def test_teach_adds_a_fact_and_updates_stats(sage):
    sage.teach_topic.setText("widgets")
    sage.teach_fact.setPlainText("Widgets are blue.")
    sage.on_teach()
    assert sage.knowledge.get_facts()[0]["fact"] == "Widgets are blue."
    assert "facts:1" in sage.stats_label.text()
    assert "Widgets are blue." in sage.kb_box.toPlainText()
    assert sage.teach_topic.text() == ""


def test_teach_without_a_fact_says_so_and_writes_nothing(sage):
    sage.teach_topic.setText("widgets")
    sage.on_teach()
    assert sage.knowledge.get_facts() == []
    assert "required" in sage.status.text()


def test_a_correction_is_saved(sage):
    sage.corr_query.setPlainText("colour of widgets?")
    sage.corr_right.setPlainText("blue")
    sage.on_correct()
    assert sage.knowledge.get_corrections()[0]["correction"] == "blue"


def test_a_domain_is_declared_and_listed(sage):
    sage.dom_name.setText("optics")
    sage.dom_desc.setPlainText("lenses")
    sage.on_domain()
    assert "optics: lenses" in sage.dom_list.toPlainText()


def test_clear_gaps_asks_first(sage):
    sage.knowledge.log_gap("what is x?", "unknown")
    sage.refresh_gaps()
    sage.confirms.append(False)
    sage.on_clear_gaps()
    assert len(sage.knowledge.get_gaps()) == 1
    sage.confirms.append(True)
    sage.on_clear_gaps()
    assert sage.knowledge.get_gaps() == []
    assert "no gaps" in sage.gaps_box.toPlainText()


# -- Vault Agent ---------------------------------------------------------

class _Step:
    def __init__(self, kind, content):
        self.kind, self.content = kind, content


def test_summary_names_the_answer():
    lines = summary_lines([_Step("tool_call", "x"), _Step("done", "All good")])
    assert ("done", "All good") in lines
    assert lines[-1][1].strip() == "[2 steps, 1 tool calls]"


def test_summary_names_the_error_when_there_is_no_answer():
    lines = summary_lines([_Step("error", "model died")])
    assert any("Agent stopped: model died" in text for _t, text in lines)


@pytest.fixture
def vault_panel(qapp, tmp_path):
    calls = []

    def runner(model, vault_dir, task, on_event):
        calls.append((model, task, threading.current_thread().name))
        on_event("thought", "thinking hard")
        return [_Step("done", "Finished.")]

    resolved = []

    def resolve(role):
        resolved.append((role, threading.current_thread().name))
        return {"writer": "W", "coder": "C"}.get(role)

    panel = VaultAgentPanel(resolve, tmp_path, runner=runner)
    panel.calls, panel.resolved = calls, resolved
    yield panel
    pump(qapp, lambda: not panel._busy)
    panel.deleteLater()
    qapp.processEvents()


def test_the_role_is_read_on_the_gui_thread(qapp, vault_panel):
    """Defect 2: Tk's worker read the model StringVar itself. The role must be
    captured before the thread starts; only resolving it runs on the worker."""
    vault_panel.model.setCurrentText("coder")
    vault_panel.task.setText("list files")
    vault_panel.on_run()
    # Changing the picker mid-run must not change the run.
    vault_panel.model.setCurrentText("writer")
    pump(qapp, lambda: not vault_panel._busy)
    assert vault_panel.resolved == [("coder", "vault-agent")]
    assert vault_panel.calls == [("C", "list files", "vault-agent")]
    text = vault_panel.log.toPlainText()
    assert "💭 thinking hard" in text and "Finished." in text


def test_a_run_disables_run_until_done(qapp, vault_panel):
    vault_panel.task.setText("x")
    vault_panel.on_run()
    assert not vault_panel.run_btn.isEnabled()
    pump(qapp, lambda: not vault_panel._busy)
    assert vault_panel.run_btn.isEnabled()


def test_a_second_run_while_busy_is_refused_out_loud(qapp, vault_panel):
    vault_panel._busy = True
    vault_panel.task.setText("x")
    vault_panel.on_run()
    assert "already running" in vault_panel.log.toPlainText()
    assert vault_panel.calls == []
    vault_panel._busy = False


def test_an_unloaded_model_is_named(qapp, vault_panel):
    vault_panel.model.setCurrentText("judge")
    vault_panel.task.setText("x")
    vault_panel.on_run()
    pump(qapp, lambda: not vault_panel._busy)
    assert "No 'judge' model is loaded" in vault_panel.log.toPlainText()
    assert vault_panel.calls == []


def test_a_preset_fills_the_task_and_runs(qapp, vault_panel):
    vault_panel.run_preset("List all files")
    pump(qapp, lambda: not vault_panel._busy)
    assert vault_panel.task.text() == "List all files"
    assert vault_panel.calls[0][1] == "List all files"


def test_empty_task_does_nothing(vault_panel):
    vault_panel.on_run()
    assert not vault_panel._busy and vault_panel.calls == []
