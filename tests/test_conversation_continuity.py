"""
Follow-up questions in the Qt council: the conversation is recorded, and a
task memo carries the goal and constraints forward.

Pinned: each app run has its own session; after an answer the exchange is
in the conversation store before the next question (the real engine store,
the format the personalities and the Sessions tab read); the [TASK MEMO]
reaches every member for the question and is gone after; a follow-up keeps
the earlier constraints; a failed answer is not recorded.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import after_turn  # noqa: E402
from council_core import council_options  # noqa: E402
from council_core import vault_context as vc  # noqa: E402
from tests.test_council_turn import FakeJudge  # noqa: E402


class Member:
    """Records the standing context each call saw; answers plainly."""

    def __init__(self, store=None, session=None):
        self.extra_context = ""
        self.seen = []
        self.conversation_store, self.session_id = store, session

    def respond(self, prompt, **kw):
        self.seen.append(self.extra_context)
        return "7" if "Rate your confidence" in prompt else "Use the P-200."


def _actions(tmp_path):
    import council_engine as ce
    from council_core import council_turn as ct
    from council_qt.tabs.council import CouncilActions
    actions = CouncilActions(vault_dir=tmp_path)
    store = ce.ConversationStore(tmp_path / "conversations")
    models = NS(**{n: None for n in ct.AGENT_NAMES})
    models.writer = Member(store, actions.session_id)
    models.peasant = Member()
    models.judge = FakeJudge()
    actions._models = models
    actions.vault_brief = lambda q: vc.Brief()
    actions.memo_llm = None                      # keyword rules only
    return actions, models, store


def test_each_run_has_its_own_session(tmp_path):
    from council_qt.tabs.council import CouncilActions
    a = CouncilActions(vault_dir=tmp_path)
    assert a.session_id.startswith("qt-") and a.session_id != "qt"


def test_the_exchange_is_history_before_the_next_question(tmp_path):
    actions, models, store = _actions(tmp_path)
    opts = council_options.CouncilOptions.defaults()
    result = actions.send("Which pump for 40 l/min?", opts)
    assert result.ok, result.message
    turns = store.load_last(actions.session_id)
    assert [t["who"] for t in turns] == ["User", "Council"]
    assert turns[0]["text"] == "Which pump for 40 l/min?"
    assert turns[1]["text"] == result.answer and turns[1]["ts"]
    actions.send("And for 80?", opts)
    assert len(store.load_last(actions.session_id)) == 4


def test_a_failed_answer_is_not_recorded(tmp_path):
    actions, models, store = _actions(tmp_path)
    models.writer.respond = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("model gone"))
    result = actions.send("anything", council_options.CouncilOptions
                          .defaults())
    assert not result.ok
    assert store.load_last(actions.session_id) == []


def test_record_exchange_needs_a_store_and_both_sides(tmp_path):
    assert not after_turn.record_exchange(NS(writer=Member()), "q", "a")
    import council_engine as ce
    store = ce.ConversationStore(tmp_path)
    models = NS(writer=Member(store, "s1"))
    assert not after_turn.record_exchange(models, "q", "")
    assert after_turn.record_exchange(models, "q", "a")


def test_the_memo_reaches_members_for_the_question_only(tmp_path):
    actions, models, _store = _actions(tmp_path)
    events = []
    actions.send("Only use data_in and do not invent column names: total "
                 "pump hours", council_options.CouncilOptions.defaults(),
                 on_event=events.append)
    assert any("[TASK MEMO" in s for s in models.writer.seen)
    assert models.writer.extra_context == ""
    assert any(e.who == "Task memo" for e in events)


def test_a_follow_up_keeps_the_earlier_constraints(tmp_path):
    actions, models, _store = _actions(tmp_path)
    opts = council_options.CouncilOptions.defaults()
    actions.send("Sum the pump hours, excluding zeros, from data_in only",
                 opts)
    first = actions.task_memory.current()
    assert first.constraints
    actions.send("also, what about the valve hours?", opts)
    memo = actions.task_memory.current()
    assert memo.is_extension
    for c in first.constraints:
        assert c in memo.constraints


# ---- past decisions, across sessions -----------------------------------------

from council_core import past_decisions as pd  # noqa: E402


def test_recall_finds_similar_questions_and_ignores_others(tmp_path):
    pd.record(tmp_path, "Which pump for 40 litres per minute?",
              "The P-200.", "PASS", ["writer"])
    pd.record(tmp_path, "What is the capital of France?", "Paris.", "PASS")
    pd.record(tmp_path, "Seal material for acid service?", "PTFE.",
              "NEEDS_WORK")
    hits = pd.recall(tmp_path, "pump for 80 litres per minute")
    assert [h.answer for h in hits] == ["The P-200."]
    assert pd.recall(tmp_path, "weather tomorrow") == []
    text = pd.block(hits)
    assert text.startswith("PAST DECISIONS") and "Verdict: PASS" in text
    assert "1 similar question" in pd.note(hits)


def test_only_answered_verdicts_are_recorded(tmp_path):
    assert not pd.record(tmp_path, "q", "", "PASS")
    assert not pd.record(tmp_path, "q", "a", "")
    assert not pd.path_for(tmp_path).exists()


def test_it_can_be_turned_off(tmp_path, monkeypatch):
    pd.record(tmp_path, "pump sizing for 40 lpm", "P-200", "PASS")
    monkeypatch.setenv("COUNCIL_PAST_DECISIONS", "0")
    assert pd.recall(tmp_path, "pump sizing for 40 lpm") == []
    assert not pd.record(tmp_path, "x y z", "a", "PASS")


def test_the_store_is_kept_out_of_vault_searches(tmp_path):
    import conversation_logger
    import vault_rag
    pd.record(tmp_path, "pump sizing for 40 lpm", "P-200", "PASS")
    assert pd.DIR_NAME in conversation_logger.PROTECTED_SUBDIRS
    assert vault_rag._collect_files(tmp_path) == []


class EvidenceJudge(FakeJudge):
    def __init__(self):
        super().__init__()
        self.ranked_with = []

    def rank_candidates(self, user_text, candidates, *, extra_context=""):
        self.ranked_with.append(extra_context)
        return super().rank_candidates(user_text, candidates)


def test_a_similar_question_later_recalls_the_decision(tmp_path):
    actions, models, _store = _actions(tmp_path)
    opts = council_options.CouncilOptions.defaults()
    first = actions.send("Which pump for 40 litres per minute?", opts)
    assert first.ok and first.critique
    assert pd.recall(tmp_path, "pump 40 litres per minute")
    # A later run (a new session) asks something similar.
    actions2, models2, _ = _actions(tmp_path)
    models2.judge = EvidenceJudge()
    events = []
    actions2.send("Pump for 40 litres per minute — which one?", opts,
                  on_event=events.append)
    assert any(e.text.startswith("Past decisions: 1") for e in events)
    assert any("PAST DECISIONS" in s for s in models2.writer.seen)
    assert not any("PAST DECISIONS" in s for s in models2.peasant.seen)
    assert "PAST DECISIONS" in models2.judge.ranked_with[0]
    assert models2.writer.extra_context == ""
