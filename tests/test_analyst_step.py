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
    opts = council_options.CouncilOptions(deliberate=True)
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


# ============================================================
# The Qt turn's analyst keeps council-map's contract
# ============================================================
#
# The merged Qt Council's analyst is the turn context's
# (turn_context.prepare_turn). Found in review: it ignored COUNCIL_ANALYST=0,
# asked the main model (role None) once with no retry, ran the model's code
# in-process with no time limit or memory cap, and handed it save_app_tool /
# run_app_tool — one numbers question wrote App_Built_tools/ and
# analyst_results/ into the vault. Each test sends a real question through
# CouncilActions.send over a temp vault, with council_engine.local_chat
# stubbed; the code runs in the real child process.

class _Writer:
    def __init__(self):
        self.extra_context, self.prompts = "", []

    def respond(self, prompt, **kw):
        self.prompts.append(prompt)
        return "It is 20."


def _qt_turn(vault, monkeypatch, replies):
    """(actions, writer, calls): the Qt Council's actions over `vault` with
    one Writer, local_chat answering the analyst's code prompts with
    `replies` in turn (the last one repeated); `calls` are those prompts.
    (The vault search's own word-matching call gets "".)"""
    import council_engine
    from council_core import council_turn as ct
    from council_core import vault_context as vc
    from council_core.docs_brief import DocsBrief
    from council_qt.tabs.council import CouncilActions
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    calls = []

    def local_chat(messages=None, *a, **kw):
        prompt = messages[-1]["content"]
        if "pandas snippet" not in prompt:
            return ""
        calls.append(dict(kw, prompt=prompt))
        return replies[min(len(calls), len(replies)) - 1]
    monkeypatch.setattr(council_engine, "local_chat", local_chat)
    actions = CouncilActions(vault_dir=vault)
    models = NS(**{n: None for n in ct.AGENT_NAMES})
    models.writer = _Writer()
    actions._models = models
    actions.memo_llm = None
    actions.vault_brief = lambda q: vc.Brief()
    actions.docs_brief = lambda q, m: DocsBrief()
    return actions, models.writer, calls


def _direct():
    from council_core import council_options
    opts = council_options.CouncilOptions.defaults()
    opts.deliberate = False
    return opts


#: The analyst's block in a prompt (the task memo's rules NAME it, "Quote
#: numbers only from [ANALYST RESULT] …", without the dash).
BLOCK = "[ANALYST RESULT —"


def _files(vault):
    """The vault's files, without the app's own bookkeeping (the vault
    index's files at the root, dot-folders): what the user would find."""
    import conversation_logger
    return sorted(str(p.relative_to(vault)) for p in vault.rglob("*")
                  if p.is_file()
                  and not conversation_logger.is_protected_path(p, vault)
                  and not any(part.startswith(".")
                              for part in p.relative_to(vault).parts))


def test_the_qt_turn_s_analyst_can_be_turned_off(vault, monkeypatch):
    monkeypatch.setenv("COUNCIL_ANALYST", "0")
    actions, writer, calls = _qt_turn(vault, monkeypatch, [GOOD])
    assert actions.send(QUESTION, _direct()).ok
    assert calls == [], "the analyst asked a model while switched off"
    assert writer.prompts and not any(BLOCK in p for p in writer.prompts)


def test_the_qt_turn_asks_the_coder_and_retries_once(vault, monkeypatch):
    monkeypatch.delenv("COUNCIL_ANALYST", raising=False)
    actions, writer, calls = _qt_turn(vault, monkeypatch, [BAD, GOOD])
    assert actions.send(QUESTION, _direct()).ok
    assert len(calls) == 2, calls
    assert [c.get("role") for c in calls] == ["coder", "coder"]
    assert calls[0]["num_predict"] == 700 and calls[0]["timeout"] == 120
    assert "YOUR CODE FAILED" in calls[1]["prompt"]
    assert "nope.csv" in calls[1]["prompt"]
    shown = writer.prompts[-1]
    assert BLOCK in shown and "20" in shown
    assert "COMPUTATION FAILED" not in shown


def test_the_qt_turn_s_analyst_writes_nothing_to_the_vault(vault,
                                                           monkeypatch):
    """The model's code is not handed save_app_tool (it is not in the
    sandbox, so the call fails and goes back to the model), and the result
    table is not saved into the vault."""
    tool = "def mean_hours(x=1):\n    return x\n"
    saves = ("```python\nimport pandas as pd\n"
             f"msg = save_app_tool('mean_hours', 'a tool', {tool!r})\n"
             "result_df = pd.DataFrame({'msg': [str(msg)]})\n```")
    monkeypatch.delenv("COUNCIL_ANALYST", raising=False)
    actions, writer, calls = _qt_turn(vault, monkeypatch, [saves, GOOD])
    before = _files(vault)
    assert actions.send(QUESTION, _direct()).ok
    assert "save_app_tool" not in calls[0]["prompt"], (
        "the prompt still offers a tool the sandbox does not have")
    assert len(calls) == 2 and "save_app_tool" in calls[1]["prompt"]
    assert _files(vault) == before
    assert BLOCK in writer.prompts[-1]


def test_the_qt_turn_runs_the_code_in_a_child_with_a_time_limit(
        vault, monkeypatch):
    """An endless loop is stopped at the analyst's time limit (shortened
    here) and the council is told the computation failed — in-process it
    had no limit and hung the question for good."""
    monkeypatch.delenv("COUNCIL_ANALYST", raising=False)
    monkeypatch.setattr(an, "TIME_LIMIT_S", 3)
    loop = "```python\nwhile True:\n    pass\n```"
    actions, writer, calls = _qt_turn(vault, monkeypatch, [loop])
    t0 = time.monotonic()
    assert actions.send(QUESTION, _direct()).ok
    assert time.monotonic() - t0 < 30
    assert len(calls) == an.MAX_TRIES
    assert "COMPUTATION FAILED" in writer.prompts[-1]
    assert "stopped" in writer.prompts[-1]
