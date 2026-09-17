"""
council_core.council_turn — the assembly both front ends were doing by hand.

The Tk shell builds the agent dict, routes the question, filters the panel,
runs the orchestrator and digs the answer back out of the event list at THREE
separate call sites. This module does it once. These tests need no toolkit, no
display and no model — the orchestrator is driven with stand-ins, which is
possible at all only because it was already free of both.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import council_turn as ct  # noqa: E402
from council_core.deliberation import AgentEvent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class FakeModel:
    """Anything with .respond is a personality as far as ModelAgent cares."""

    def __init__(self, reply="an answer"):
        self.reply = reply
        self.asked = []

    def respond(self, prompt, **kwargs):
        self.asked.append(prompt)
        return self.reply


class FakeJudge:
    """The judge contract, in full.

    Written out rather than mocked loosely because the orchestrator calls all
    three and a judge missing one takes the whole turn down with an
    AttributeError — the first version of this fake had only two, and
    `run_turn` reported "The turn failed: AttributeError('rank_candidates')"
    where a user would expect an answer.
    """

    def __init__(self, route="chat", critique="Verdict: PASS", confidence=8):
        self._route, self._critique, self._confidence = (
            route, critique, confidence)

    def route(self, text):
        """Which panel should answer this."""
        return self._route

    def critique(self, user_text, response, *, extra_context="",
                 query_mode=""):
        """The verdict on the synthesised answer.

        The keyword arguments are part of the contract: the orchestrator passes
        the ranking JSON and the query mode into it, and a judge that does not
        accept them fails the turn with a TypeError.
        """
        return self._critique

    def rank_candidates(self, user_text, candidates):
        """Scores the panel's drafts. Returns the JSON the judge emits."""
        import json
        return json.dumps({"confidence": self._confidence, "best": "writer"})


class Models:
    """A stand-in for the object carrying the model slots."""

    def __init__(self, **slots):
        for name in ct.AGENT_NAMES:
            setattr(self, name, None)
        for name, value in slots.items():
            setattr(self, name, value)


# ============================================================
# No toolkit anywhere near this
# ============================================================

@pytest.mark.parametrize("module", ["council_turn.py", "deliberation.py",
                                    "managers.py"])
def test_the_extracted_council_imports_no_toolkit(module):
    """The deliberation was ALREADY outside CouncilConsole and toolkit-free —
    that is why this port could move it instead of rewriting it. Keep it so."""
    source = (ROOT / "council_core" / module).read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "PyQt6", "import tk"):
        assert toolkit not in source, f"{module} imports {toolkit}"


# ============================================================
# Building the panel
# ============================================================

def test_only_populated_model_slots_become_agents():
    """An agent backed by None raises on its first call, deep inside a worker,
    with a message about the model rather than about the missing role."""
    agents = ct.build_agents(Models(writer=FakeModel(), coder=FakeModel()))
    assert set(agents) == {"writer", "coder"}


def test_a_build_with_no_models_produces_no_agents():
    assert ct.build_agents(Models()) == {}


def test_the_display_name_is_what_the_transcript_will_show():
    """The name is the product — it is what colours the speaker and what the
    user reads. `writer` in the code is `Writer` on screen."""
    agent = ct.build_agents(Models(writer=FakeModel()))["writer"]
    shown = getattr(agent, "display_name", None) or getattr(agent, "name", None)
    assert shown == "Writer"


def test_the_sage_is_found_through_its_wrapper():
    """Some builds reach the Sage through an agent object rather than a slot.
    Missing it means the sage route deliberates without a sage."""
    class Wrapper:
        model = FakeModel()

    models = Models(writer=FakeModel())
    models.sage_agent_obj = Wrapper()
    agents = ct.build_agents(models)
    assert "sage" in agents


def test_a_route_maps_to_its_panel():
    panel, synth = ct.panel_for_route("ide")
    assert panel == ["coder", "intern", "skeptic"]
    assert synth == "writer"


def test_an_unknown_route_falls_back_rather_than_failing():
    assert ct.panel_for_route("nonsense") == ct.panel_for_route("_default")


def test_the_panel_is_returned_as_a_copy():
    """Callers filter the panel in place. Handing out the shared list would let
    one turn's filtering permanently narrow every later turn on that route."""
    first, _ = ct.panel_for_route("ide")
    first.remove("skeptic")
    second, _ = ct.panel_for_route("ide")
    assert "skeptic" in second


def test_a_panel_is_filtered_to_the_roles_this_build_has():
    agents = ct.build_agents(Models(writer=FakeModel(), intern=FakeModel()))
    assert ct.usable_panel(["coder", "intern", "skeptic"], agents) == ["intern"]


def test_a_panel_is_never_left_empty():
    """The `ide` route asks for a skeptic. A build without one would otherwise
    deliberate with an empty panel, and the user sees a turn that ends with no
    answer and no error."""
    agents = ct.build_agents(Models(writer=FakeModel()))
    panel = ct.usable_panel(["coder", "skeptic"], agents)
    assert panel, "the panel was left empty"
    assert all(role in agents for role in panel)


# ============================================================
# Reading the result out of the events
# ============================================================

def test_the_answer_is_the_last_final_from_the_synthesiser():
    events = [
        AgentEvent("Writer", "final", "first draft"),
        AgentEvent("Peasant", "final", "not the answer"),
        AgentEvent("Writer", "final", "the real answer"),
    ]
    assert ct.final_answer(events) == "the real answer"


def test_the_critique_is_the_judges_observation():
    events = [AgentEvent("Judge", "observation", "Verdict: PASS")]
    assert ct.judge_critique(events) == "Verdict: PASS"


def test_a_malformed_confidence_costs_the_reading_not_the_answer():
    """It arrives as JSON inside a text event. A turn the user is waiting for
    must not be lost to a stray brace."""
    events = [AgentEvent("Judge", "observation", "confidence Ranking:\n{oops")]
    assert ct.judge_confidence(events) == 0


def test_a_well_formed_confidence_is_read():
    events = [AgentEvent("Judge", "observation",
                         'confidence Ranking:\n{"confidence": 7}')]
    assert ct.judge_confidence(events) == 7


# ============================================================
# Running a turn
# ============================================================

def test_a_blank_question_is_refused_before_anything_loads():
    result = ct.run_turn("   ", Models())
    assert not result.ok and "Ask something" in result.message


def test_a_build_with_no_judge_says_so():
    result = ct.run_turn("why?", Models(writer=FakeModel()))
    assert not result.ok and "judge" in result.message.lower()


def test_a_build_with_no_personalities_names_the_tab_to_visit():
    """"The turn failed" sends the user looking for a bug. Naming the Models
    tab sends them somewhere useful."""
    models = Models()
    models.judge = FakeJudge()
    result = ct.run_turn("why?", models)
    assert not result.ok
    assert "Models tab" in result.message


def test_a_turn_never_raises(monkeypatch):
    """A front end mid-turn needs something to show the user more than it
    needs a traceback — and the exception is kept for whoever wants one."""
    class Exploding:
        def route(self, text):
            return "chat"

        def critique(self, *a):
            raise RuntimeError("judge died")

    models = Models(writer=FakeModel(), peasant=FakeModel())
    models.judge = Exploding()
    result = ct.run_turn("why?", models, judge=Exploding())
    assert isinstance(result, ct.TurnResult)
    if not result.ok:
        assert result.message
        assert result.error is not None


def test_an_unroutable_question_is_still_asked():
    """A judge that cannot route must not cost the user their turn — the
    default panel answers it."""
    class NoRoute(FakeJudge):
        def route(self, text):
            raise RuntimeError("router offline")

    models = Models(writer=FakeModel(), intern=FakeModel(),
                    peasant=FakeModel())
    result = ct.run_turn("why?", models, judge=NoRoute(), max_rounds=1,
                         debate_turns=1)
    assert result.ok, result.message
    assert result.panel


def test_events_reach_the_callback_as_they_happen():
    seen = []
    models = Models(writer=FakeModel(), intern=FakeModel(),
                    peasant=FakeModel())
    result = ct.run_turn("why?", models, judge=FakeJudge(), max_rounds=1,
                         debate_turns=1, on_event=seen.append)
    assert result.ok, result.message
    assert seen, "nothing was reported while the turn ran"
    assert all(isinstance(e, AgentEvent) for e in seen)


# ============================================================
# A3 — a turn without a verdict must be able to say so
# ============================================================

def test_a_deliberated_turn_carries_a_verdict_id():
    models = Models(writer=FakeModel(), intern=FakeModel(),
                    peasant=FakeModel())
    result = ct.run_turn("why?", models, judge=FakeJudge(), max_rounds=1,
                         debate_turns=1)
    assert result.ok, result.message
    assert result.verdict_id, "a judged turn produced no verdict id"


def test_a_turn_with_no_critique_carries_no_verdict_id():
    """THE WHOLE POINT. The Tk build shows the verdict bar after every turn and
    then records the user's agreement against the last line of
    verdict_history.jsonl — which, after a turn that produced no verdict,
    belongs to an unrelated earlier deliberation."""
    models = Models(writer=FakeModel(), intern=FakeModel(),
                    peasant=FakeModel())
    result = ct.run_turn("why?", models, judge=FakeJudge(critique=""),
                         max_rounds=1, debate_turns=1)
    assert result.ok, result.message
    assert result.verdict_id == ""


def test_a_failed_turn_carries_no_verdict_id():
    assert ct.run_turn("", Models()).verdict_id == ""


def test_the_qt_tab_shows_the_bar_from_the_turns_own_id():
    source = (ROOT / "council_qt" / "tabs" / "council.py").read_text(
        encoding="utf-8")
    assert "show_verdict_bar(result.verdict_id)" in source, (
        "the Qt tab decides for itself whether to show the verdict bar")


# ============================================================
# What a judge has to be
# ============================================================

def test_the_judge_contract_is_three_methods():
    """Read off the orchestrator rather than asserted from memory, so a judge
    written for a different build can be checked against it.

    It matters because the orchestrator calls all three unguarded: a judge
    missing one fails the turn with an AttributeError, and the user sees "The
    turn failed" where they expected an answer.
    """
    import ast
    source = (ROOT / "council_core" / "deliberation.py").read_text(
        encoding="utf-8")
    called = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)):
            continue
        target = node.func.value
        name = getattr(target, "attr", None) or getattr(target, "id", None)
        if name in ("judge", "judge_model"):
            called.add(node.func.attr)

    # `route` is called by run_turn, not by the orchestrator.
    assert called == {"critique", "rank_candidates"}
    for method in called | {"route"}:
        assert hasattr(FakeJudge(), method), (
            f"the contract grew a method the test double does not have: "
            f"{method}")
