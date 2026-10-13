"""
The Analyst before a numbers question (council_core.analyst_step).

Pinned: only a question that reads as a computation runs it; it needs data
files; the model's code runs in a child process under the hardened sandbox
with a time limit (an infinite loop is stopped, a read outside the data
folder refused); a failure goes back to the model once; when it still fails
the council is told NOT to invent a number; the result reaches every member
and the Judge for that question; COUNCIL_ANALYST=0 turns it off.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import analyst_step as an  # noqa: E402

QUESTION = "what is the average hours across the pump logs?"
GOOD = ("```python\nimport pandas as pd\n"
        "result_df = pd.read_csv('pumps.csv')[['hours']].mean()"
        ".to_frame('mean_hours')\n```")
BAD = "```python\nimport pandas as pd\nresult_df = pd.read_csv('nope.csv')\n```"


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "data_in").mkdir(parents=True)
    (v / "data_in" / "pumps.csv").write_text(
        "pump,hours\nP1,10\nP2,30\nP3,20\n")
    return v


def script(*replies):
    calls = []

    def chat(messages, **kw):
        calls.append(messages[-1]["content"])
        return replies[min(len(calls), len(replies)) - 1]
    chat.calls = calls
    return chat


def test_a_numbers_question_gets_computed_figures(vault):
    chat = script(GOOD)
    r = an.run(QUESTION, vault, chat=chat)
    assert r.block.startswith("[ANALYST RESULT")
    assert "20" in r.block and "mean_hours" in r.block
    assert "1×1 table" in r.note and "mean_hours" in r.table
    assert "pumps.csv" in chat.calls[0]                 # the inventory


def test_other_questions_do_not_run_it(vault):
    chat = script(GOOD)
    assert an.run("copy a folder tree", vault, chat=chat).block == ""
    assert chat.calls == []


def test_no_data_files_says_so_without_a_model(tmp_path):
    chat = script(GOOD)
    r = an.run(QUESTION, tmp_path, chat=chat)
    assert r.block == "" and "holds no data files" in r.note
    assert chat.calls == []


def test_a_failure_goes_back_to_the_model_once(vault):
    chat = script(BAD, GOOD)
    r = an.run(QUESTION, vault, chat=chat)
    assert r.block.startswith("[ANALYST RESULT")
    assert len(chat.calls) == 2 and "YOUR CODE FAILED" in chat.calls[1]


def test_when_it_still_fails_the_council_must_not_invent(vault):
    r = an.run(QUESTION, vault, chat=script(BAD, BAD))
    assert r.block.startswith("[ANALYST FAILED")
    assert "Do NOT invent a number" in r.block
    assert "could not compute" in r.note


def test_an_endless_loop_is_stopped(vault):
    t0 = time.monotonic()
    df, log = an.run_code("while True:\n    pass\n",
                          vault / "data_in", timeout=3)
    assert df is None and "stopped" in log
    assert time.monotonic() - t0 < 20


def test_the_sandbox_keeps_reads_inside_the_data_folder(vault, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n")
    df, log = an.run_code(
        f"import pandas as pd\nresult_df = pd.read_csv(r'{secret}')\n",
        vault / "data_in")
    assert df is None and "outside the data folders" in log


def test_it_can_be_turned_off(vault, monkeypatch):
    monkeypatch.setenv("COUNCIL_ANALYST", "0")
    assert an.run(QUESTION, vault, chat=script(GOOD)).block == ""


def test_the_result_reaches_every_member_and_the_judge(tmp_path,
                                                       monkeypatch):
    """Through the Qt Council's send, whose analyst is the turn context's
    (council_core.turn_context.prepare_turn — it also answers a count
    straight from the data, with no model asked): the figures are in front
    of the question every member and the Judge read. This module's analyst
    ran beside it in council-map; together they computed the same figures
    twice, from two model calls, into two blocks."""
    from council_core import council_options, council_turn as ct
    from council_core import turn_context
    from council_core import vault_context as vc
    from council_qt.tabs.council import CouncilActions
    from tests.test_council_turn import FakeJudge

    class Member:
        def __init__(self):
            self.extra_context, self.prompts = "", []

        def respond(self, prompt, **kw):
            self.prompts.append(prompt)
            return "7" if "Rate your confidence" in prompt else "It is 20."

    class Judge(FakeJudge):
        def __init__(self):
            super().__init__()
            self.ranked_on = []

        def rank_candidates(self, user_text, cands, *, extra_context=""):
            self.ranked_on.append(user_text)
            return super().rank_candidates(user_text, cands)

    block = "[ANALYST RESULT — computed]\nmean_hours 20"
    ran = []
    monkeypatch.setattr(turn_context, "_run_analyst_step",
                        lambda q: ran.append(q) or (
                            block, None, ["__ANALYST_TABLE__:20"]))
    monkeypatch.setattr(an, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a second analyst ran")))
    actions = CouncilActions(vault_dir=tmp_path)
    models = NS(**{n: None for n in ct.AGENT_NAMES})
    models.writer, models.peasant = Member(), Member()
    models.judge = Judge()
    actions._models = models
    actions.vault_brief = lambda q: vc.Brief()
    actions.memo_llm = None
    events = []
    opts = council_options.CouncilOptions.defaults()
    opts.depth = "deep"              # the Judge ranks only at depth > quick
    result = actions.send(QUESTION, opts, on_event=events.append)
    assert result.ok, result.message
    assert ran == [QUESTION]                          # once, on the typed text
    assert any("ANALYST RESULT" in p for p in models.writer.prompts)
    assert any("ANALYST RESULT" in p for p in models.peasant.prompts)
    assert "ANALYST RESULT" in models.judge.ranked_on[0]
    assert models.writer.extra_context == ""
    assert ("Council", "Analyst result table:\n20") in [
        (e.who, e.text) for e in events]
