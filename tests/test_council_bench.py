"""The council benchmark (council_core/council_bench.py and its dialog)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import council_bench as cb  # noqa: E402
from council_core import council_turn as ct  # noqa: E402


@pytest.mark.parametrize("answer,checks,ok", [
    ("17 × 23 = 391.", [{"number": 391}], True),
    ("It is 1,391.", [{"number": 391}], False),
    ("The rate is 2.80%.", [{"number": 2.8, "tol": 0.05}], True),
    ("Canberra.", [{"contains": ["canberra"]}], True),
    ("```python\ndef f(:\n```", [{"python_parses": True}], False),
    ("```python\ndef f():\n    return 1\n```", [{"python_parses": True}],
     True),
    ("A map. ```x```", [{"not_contains": ["```"]}], False),
    ("Back up first, then verify.", [{"any": ["backup", "back up"]},
                                     {"any": ["verify"]}], True),
])
def test_checks(answer, checks, ok):
    assert (cb.check(answer, checks) == []) is ok


def test_every_default_question_has_checks():
    assert len(cb.DEFAULT_SET) >= 20
    assert all(q.checks for q in cb.DEFAULT_SET)
    assert len({q.id for q in cb.DEFAULT_SET}) == len(cb.DEFAULT_SET)


def test_vault_questions_are_read(tmp_path):
    d = tmp_path / ".council_bench"
    d.mkdir()
    (d / "questions.json").write_text(json.dumps(
        [{"id": "seal", "text": "Which seal?",
          "checks": [{"contains": ["EPDM"]}]}]), encoding="utf-8")
    qs = cb.load_questions(tmp_path, "vault")
    assert [q.id for q in qs] == ["seal"]
    assert len(cb.load_questions(tmp_path, "all")) == len(cb.DEFAULT_SET) + 1


def fake_turn(answers):
    from council_core.deliberation import AgentEvent

    def turn(question, models, *, judge=None, on_event=None, depth="auto"):
        for label in ("▶ Writer — drafting answer", "▶ Judge — critiquing"):
            on_event(AgentEvent("Orchestrator", "phase", label))
            time.sleep(0.01)
        return NS(ok=True, answer=answers.get(question, "?"), depth="quick",
                  verdict="PASS", confidence=80,
                  meter={"calls": 2, "seconds": 1.5, "loads": 1,
                         "load_s": 0.8}, message="")
    return turn


def test_a_run_is_recorded_and_compared(tmp_path):
    qs = [cb.Question("a", "17*23?", checks=[{"number": 391}]),
          cb.Question("b", "capital?", checks=[{"contains": ["Canberra"]}])]
    lines = []
    s1 = cb.run(qs, None, tmp_path, label="before",
                turn=fake_turn({"17*23?": "391"}), progress=lines.append)
    assert s1["correct"] == 1 and s1["questions"] == 2
    assert s1["mean_calls"] == 2 and s1["loads"] == 2
    assert set(s1["steps_s"]) == {"draft", "critique"}
    assert len(lines) == 2 and "✗ b" in lines[1]
    rows = (tmp_path / ".council_bench" / s1["file"]).read_text().splitlines()
    assert len(rows) == 2 and json.loads(rows[0])["correct"] is True
    s2 = cb.run(qs, None, tmp_path, label="after",
                turn=fake_turn({"17*23?": "391", "capital?": "Canberra"}),
                now=time.time() + 2)
    assert cb.find_run(tmp_path, "before")["correct"] == 1
    text = cb.compare(cb.find_run(tmp_path, "before"),
                      cb.find_run(tmp_path, "after"))
    assert "accuracy" in text and "+1 better" in text


def test_the_real_turn_runs_the_bench(tmp_path):
    from tests.test_council_turn import FakeJudge, FakeModel
    models = NS(**{n: None for n in ct.AGENT_NAMES})
    models.writer, models.judge = FakeModel("It is 391."), FakeJudge()
    o = cb.run_one(cb.Question("a", "What is 17 multiplied by 23?",
                               checks=[{"number": 391}]), models)
    assert o.ok and o.correct and o.depth == "quick"
    assert o.steps


def test_the_dialog_runs_and_compares(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from council_qt.widgets.bench_dialog import BenchDialog
    d = BenchDialog(vault_dir=tmp_path, loader=lambda v: (object(), ""),
                    turn=fake_turn({}))
    for label in ("one", "two"):
        d.label_edit.setText(label)
        d.start()
        deadline = time.monotonic() + 10
        while d._busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert not d._busy
    assert "Saved as" in d.output.toPlainText()
    assert d.run_a.count() == 2
    d.compare()
    assert "accuracy" in d.output.toPlainText()
