"""An earlier passed answer offered for the same question
(council_core/answer_reuse.py and the Council tab)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import answer_reuse as ar  # noqa: E402

Q = "What seal does the P-200 pump need for 40 litres per minute?"


def test_same_question_is_found(tmp_path):
    assert ar.record(tmp_path, Q, "Use the EPDM seal.", "PASS")
    hit = ar.find(tmp_path, "what seal does the P-200 pump need for 40 "
                            "litres per minute")
    assert hit and hit.answer == "Use the EPDM seal." and hit.score >= 0.9


@pytest.mark.parametrize("other", [
    "What seal does the P-200 pump need for 50 litres per minute?",
    "Which gasket fits the P-300 housing at high temperature?",
    "and what about for 40 litres per minute?",
])
def test_a_different_or_follow_up_question_is_not(tmp_path, other):
    ar.record(tmp_path, Q, "Use the EPDM seal.", "PASS")
    assert ar.find(tmp_path, other) is None


def test_only_passed_answers_are_kept(tmp_path):
    assert not ar.record(tmp_path, Q, "maybe", "NEEDS_WORK")
    assert ar.find(tmp_path, Q) is None


def test_an_old_answer_is_not_offered(tmp_path):
    ar.record(tmp_path, Q, "old", "PASS", now=time.time() - 40 * 86400)
    assert ar.find(tmp_path, Q) is None


def test_a_changed_source_file_means_ask_again(tmp_path):
    doc = tmp_path / "manual.md"
    doc.write_text("EPDM for water.", encoding="utf-8")
    ar.record(tmp_path, Q, "Use EPDM.", "PASS", [doc])
    assert ar.find(tmp_path, Q) is not None
    doc.write_text("EPDM for water. FKM for acid, updated.", encoding="utf-8")
    os.utime(doc, (time.time() + 5, time.time() + 5))
    assert ar.find(tmp_path, Q) is None
    doc.unlink()
    assert ar.find(tmp_path, Q) is None


def test_source_names_resolve_to_files(tmp_path):
    (tmp_path / "docs").mkdir()
    f = tmp_path / "docs" / "manual.md"
    f.write_text("x", encoding="utf-8")
    assert ar.source_paths(tmp_path, ["manual.md", "?"]) == [f]


def test_the_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("COUNCIL_ANSWER_REUSE", "0")
    assert not ar.record(tmp_path, Q, "a", "PASS")


def test_the_tab_offers_it_and_can_ask_again(tmp_path, monkeypatch):
    import threading
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from council_qt.tabs.council import CouncilTab

    from council_qt.tabs.council import CouncilActions

    class Actions(CouncilActions):
        sent = []

        def reusable(self, q):
            return ar.Reusable(time.time(), Q, "Use the EPDM seal.", 1.0)

        def send(self, typed, options, **kw):
            Actions.sent.append(typed)

    tab = CouncilTab(actions=Actions(vault_dir=tmp_path))
    tab.input.setPlainText(Q)
    tab.on_send()
    text = tab.transcript.toPlainText()
    assert "Use the EPDM seal." in text and "Judge passed" in text
    assert not tab.reuse_frame.isHidden() and Actions.sent == []
    tab.on_reuse_again()
    assert tab.reuse_frame.isHidden()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and Actions.sent != [Q]:
        app.processEvents()
        time.sleep(0.01)
    assert Actions.sent == [Q]                    # asked the council again
    while any(t.name == "council-turn" and t.is_alive()
              for t in threading.enumerate()):
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def test_a_passed_council_answer_is_kept_with_its_vault_files(tmp_path):
    from types import SimpleNamespace as NS
    from council_core import council_options
    from council_core import council_turn as ct
    from council_core import vault_context as vc
    from council_qt.tabs.council import CouncilActions
    from tests.test_council_turn import FakeJudge, FakeModel

    doc = tmp_path / "manual.md"
    doc.write_text("EPDM seal for the P-200.", encoding="utf-8")
    actions = CouncilActions(vault_dir=tmp_path)
    models = NS(**{n: None for n in ct.AGENT_NAMES})
    models.writer, models.judge = FakeModel("Use EPDM."), FakeJudge()
    actions._models = models
    actions.memo_llm = None
    actions.vault_brief = lambda q: vc.Brief("VAULT CONTEXT:\n[1] manual.md",
                                             ["manual.md"], "keyword")
    assert actions.send(Q, council_options.CouncilOptions.defaults()).ok
    hit = actions.reusable(Q)
    assert hit is not None and hit.answer.startswith("Use EPDM.")
    assert list(hit.sources) == ["manual.md"]
    doc.write_text("changed", encoding="utf-8")
    os.utime(doc, (time.time() + 5, time.time() + 5))
    assert actions.reusable(Q) is None
