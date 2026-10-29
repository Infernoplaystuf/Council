"""One model answers first; the council only when the question needs one
(council_core.front_desk, CouncilActions._front_desk).

Asked 2026-10-29: "if someone just asks about basic concepts like how plants
use sunlight the entire council doesn't need to fire". The keyword depth rule
sent "Why is the sky blue?" to the deep council (about 30 calls), and a quick
answer could never be handed on.
"""
from __future__ import annotations

import pytest

from council_core import council_options, front_desk


# ============================================================
# The rules, without a model
# ============================================================

@pytest.mark.parametrize("q", [
    "How do plants use sunlight?",
    "Explain how plants use sunlight to make food",
    "Why is the sky blue?",
    "what is photosynthesis",
    "Which run had a power dip and who should I confirm it with?",
])
def test_plain_questions_are_answered_first(q):
    assert front_desk.needs_council(q, "chat") == ""


@pytest.mark.parametrize("q,route", [
    # The Judge's keyword route calls these coding questions ("ide"): asked
    # 2026-10-29, an explanation stays with the first answer.
    ("explain the boid algorithm", "chat"),
    ("explain the boid algorithm in python", "ide"),
    ("what is recursion", "chat"),
    ("explain big-O notation", "chat"),
    ("what is the difference between a list and a tuple in python", "ide"),
    ("explain how to implement quicksort", "ide"),
    ("what is a design pattern", "chat"),
    ("how does a hash map work?", "ide"),
])
def test_an_explanation_is_answered_first_whatever_the_topic(q, route):
    assert front_desk.needs_council(q, route) == ""


@pytest.mark.parametrize("q,route", [
    ("write a boids simulation in python", "ide"),
    ("implement quicksort", "ide"),
    ("explain and then write quicksort in C", "ide"),
    ("how do I write a parser in python", "ide"),
    ("explain this code:\n```py\nx = 1\n```", "chat"),
    ("how do I design a REST API", "chat"),
    ("Design a schema for the run logs", "chat"),
    ("compare the pros and cons of SQLite and Postgres here", "chat"),
    ("fix this:\n```py\nprint(x\n```", "chat"),
    ("write me a parser", "coder"),
    ("x " * 400, "chat"),
])
def test_some_questions_go_straight_to_the_council(q, route):
    assert front_desk.needs_council(q, route)


def test_an_answer_stands_and_loses_its_confidence_line():
    answer, why, pct = front_desk.read_reply(
        "Plants turn light into sugar.\nCONFIDENCE: 90% — textbook")
    assert (answer, why, pct) == ("Plants turn light into sugar.", "", 90)


def test_the_writer_can_ask_for_the_council():
    answer, why, _ = front_desk.read_reply(
        "ESCALATE: needs a careful comparison of the two logs")
    assert answer == "" and why == "needs a careful comparison of the two logs"
    assert front_desk.read_reply("**Escalate**")[1]


def test_an_unsure_answer_is_handed_on():
    answer, why, pct = front_desk.read_reply("Maybe 42.\nCONFIDENCE: 30% — a guess")
    assert answer == "Maybe 42." and pct == 30 and "30%" in why


def test_a_reply_without_a_confidence_line_stands():
    assert front_desk.read_reply("Chlorophyll.") == ("Chlorophyll.", "", None)


def test_the_gate_streams_an_answer_and_swallows_a_hand_off():
    seen = []
    gate = front_desk.StreamGate(seen.append)
    for tok in ("Pla", "nts ", "use ", "light"):
        gate(tok)
    assert "".join(seen) == "Plants use light"

    seen.clear()
    gate = front_desk.StreamGate(seen.append)
    for tok in ("ESC", "ALATE", ": needs ", "the council"):
        gate(tok)
    assert seen == [] and gate.escalating

    seen.clear()
    gate = front_desk.StreamGate(seen.append)
    for tok in ("E", "lectrons ", "move"):
        gate(tok)
    assert "".join(seen) == "Electrons move"


# ============================================================
# Through CouncilActions.send
# ============================================================

class Writer:
    """Replies with `reply` to the front-desk call (it carries the
    INSTRUCTION) and with a draft to anything the council asks."""

    def __init__(self, reply):
        self.reply = reply
        self.first = []
        self.council = []

    def respond(self, prompt, *, extra_context="", token_callback=None, **_k):
        if front_desk.INSTRUCTION in (extra_context or ""):
            self.first.append(prompt)
            for word in self.reply.split(" "):
                if token_callback is not None:
                    token_callback(word + " ")
            return self.reply
        self.council.append(prompt)
        return "The council's answer."


def _actions(tmp_path, writer, *, judge=True, demo_mode=False):
    pytest.importorskip("PySide6")
    from council_qt.tabs.council import CouncilActions
    from tests.test_council_turn import FakeJudge, Models
    actions = CouncilActions(vault_dir=tmp_path, demo_mode=demo_mode)
    actions._models = Models(writer=writer,
                             judge=FakeJudge() if judge else None)
    actions.begin = lambda *a, **k: None
    actions.prepare = lambda *a, **k: None
    return actions


def _send(actions, q, **opts):
    events, tokens = [], []
    options = council_options.CouncilOptions.defaults()
    for k, v in opts.items():
        setattr(options, k, v)
    result = actions.send(q, options, on_event=events.append,
                          on_token=lambda who, t: tokens.append((who, t)))
    return result, events, "".join(t for _w, t in tokens)


def test_deliberation_is_off_by_default():
    assert council_options.CouncilOptions.defaults().deliberate is False
    assert council_options.SWITCHES_BY_KEY["deliberate"].default is False


def test_a_basic_question_is_one_streamed_call(tmp_path):
    writer = Writer("Plants turn light into sugar.\nCONFIDENCE: 92% — basic")
    result, events, streamed = _send(_actions(tmp_path, writer),
                                     "How do plants use sunlight?")
    assert result.ok and result.route == "direct"
    assert result.answer == "Plants turn light into sugar."
    assert len(writer.first) == 1 and writer.council == []
    assert streamed.startswith("Plants turn light")
    assert not any("Handing this to the council" in e.text for e in events)


@pytest.mark.parametrize("reply", [
    "ESCALATE: needs a careful comparison",
    "Perhaps.\nCONFIDENCE: 20% — guessing",
])
def test_the_writer_hands_a_question_on(tmp_path, reply):
    writer = Writer(reply)
    result, events, streamed = _send(_actions(tmp_path, writer),
                                     "How do these two logs differ?")
    assert result.ok and result.route not in ("direct", "data")
    assert result.answer == "The council's answer."
    assert writer.council, "the council never ran"
    assert any("Handing this to the council" in e.text for e in events)
    assert "ESCALATE" not in streamed


def test_a_design_question_skips_the_first_answer(tmp_path):
    writer = Writer("should not be asked")
    result, events, _ = _send(_actions(tmp_path, writer),
                              "Design a schema for the run logs")
    assert result.ok and writer.first == [] and writer.council
    assert any("design, analysis or planning" in e.text for e in events)


def test_deliberation_on_is_always_the_council(tmp_path):
    writer = Writer("should not be asked")
    result, _e, _s = _send(_actions(tmp_path, writer),
                           "How do plants use sunlight?", deliberate=True)
    assert result.ok and writer.first == [] and writer.council


def test_without_a_judge_the_first_answer_stands(tmp_path):
    writer = Writer("ESCALATE: hard")
    result, _e, _s = _send(_actions(tmp_path, writer, judge=False),
                           "Design a schema")
    assert not result.ok and "no answer" in result.message.lower()
    writer = Writer("Unsure.\nCONFIDENCE: 10% — guess")
    result, _e, _s = _send(_actions(tmp_path, writer, judge=False), "q?")
    assert result.ok and result.answer == "Unsure."


def test_the_single_model_chat_never_hands_on(tmp_path):
    writer = Writer("ESCALATE: hard")
    actions = _actions(tmp_path, writer, demo_mode=True)
    writer.respond = lambda prompt, **_k: "Just the Writer."
    result, _e, _s = _send(actions, "Design a schema")
    assert result.ok and result.answer == "Just the Writer."


# ============================================================
# "Explain it in Python": a tutorial ending in a runnable program
# ============================================================

@pytest.mark.parametrize("q,lang", [
    ("explain the boid algorithm in python", "Python"),
    ("Explain quicksort in C", "C"),
    ("explain quicksort in C++", "C++"),
    ("how does a hash map work in Go?", "Go"),
    ("explain closures using JavaScript", "JavaScript"),
    ("what is recursion", ""),
    ("explain go routines", ""),          # "go" is a word here
    ("explain the C block results", ""),  # and "C" a label
    ("write boids in python", ""),        # a build ask: the council's
])
def test_an_explanation_names_its_language(q, lang):
    assert front_desk.guide_language(q) == lang


def test_the_guide_is_asked_for_only_with_a_language():
    assert front_desk.instruction("what is recursion") == front_desk.INSTRUCTION
    told = front_desk.instruction("explain the boid algorithm in python")
    assert told.startswith(front_desk.INSTRUCTION)
    assert "runnable Python program" in told


class Recording(Writer):
    def respond(self, prompt, *, extra_context="", token_callback=None, **k):
        self.kwargs = dict(k, extra_context=extra_context)
        return super().respond(prompt, extra_context=extra_context,
                               token_callback=token_callback)


def test_the_coder_writes_the_tutorial_when_there_is_one(tmp_path):
    writer, coder = Recording("unused"), Recording(
        "Boids flock by three rules.\n```python\nprint(1)\n```\n"
        "CONFIDENCE: 85% — fine")
    actions = _actions(tmp_path, writer)
    actions._models.coder = coder
    result, events, streamed = _send(actions,
                                     "explain the boid algorithm in python")
    assert result.ok and result.route == "direct"
    assert result.answer.startswith("Boids flock") and "```python" in result.answer
    assert writer.first == [] and len(coder.first) == 1
    assert "runnable Python program" in coder.kwargs["extra_context"]
    assert int(coder.kwargs["max_tokens"]) >= 2400
    assert events[-1].who == "Coder"


def test_without_a_coder_the_writer_writes_it(tmp_path):
    writer = Recording("The idea, then code.\nCONFIDENCE: 80% — ok")
    result, _e, _s = _send(_actions(tmp_path, writer),
                           "explain quicksort in C")
    assert result.ok and len(writer.first) == 1
    assert "runnable C program" in writer.kwargs["extra_context"]


def test_a_plain_explanation_gets_no_tutorial(tmp_path):
    writer = Recording("Recursion is a function calling itself.")
    actions = _actions(tmp_path, writer)
    actions._models.coder = Recording("unused")
    result, _e, _s = _send(actions, "what is recursion")
    assert result.ok and len(writer.first) == 1
    assert writer.kwargs["extra_context"] == front_desk.INSTRUCTION
    assert "max_tokens" not in writer.kwargs
