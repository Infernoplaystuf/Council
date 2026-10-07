"""How much council a question gets, and the early stops
(council_core/depth.py and the orchestrator)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import council_turn as ct  # noqa: E402
from council_core import depth as dp  # noqa: E402


@pytest.mark.parametrize("q,route,level", [
    ("thanks!", "chat", "quick"),
    ("what is a PID loop?", "chat", "quick"),
    ("hi there", "", "quick"),
    ("write a function that parses the header", "ide", "deep"),
    ("Can you compare the two pump designs and their trade-offs?", "chat",
     "deep"),
    ("Tell me about the history of the plant and how the second line came "
     "to be built in the nineties after the flood", "chat", "standard"),
    ("```python\nx=1\n```", "chat", "deep"),
])
def test_decide(q, route, level):
    assert dp.decide(q, route).level == level


def test_a_forced_depth_wins():
    d = dp.decide("thanks!", "chat", forced="deep")
    assert d.level == "deep" and d.forced


def test_agreement():
    same = "The pump runs 40 litres per minute at 3 bar with the P-200 seal"
    cands = {"writer": {"answer": same, "self_confidence": 90},
             "intern": {"answer": same + " fitted.", "self_confidence": 85}}
    assert dp.agree(cands)
    cands["intern"]["self_confidence"] = 60
    assert not dp.agree(cands)
    cands["intern"] = {"answer": "Use a diaphragm pump instead; the seals "
                                 "fail in acid", "self_confidence": 95}
    assert not dp.agree(cands)


@pytest.mark.parametrize("msg,real", [
    ("AGREE: units | DISAGREE: none | ADD: nothing", False),
    ("AGREE: all\nDISAGREE: N/A\nADD: -", False),
    ("AGREE: x | DISAGREE: the seal rating is wrong | ADD: y", True),
    ("I agree with everything.", False),
])
def test_has_disagreement(msg, real):
    assert dp.has_disagreement([msg]) is real


class Member:
    def __init__(self, name, answer=None, pct=90, cross="AGREE: x | "
                 "DISAGREE: none | ADD: none"):
        self.name, self.answer, self.pct, self.cross = name, answer, pct, cross
        self.prompts = []

    def respond(self, prompt, **kw):
        self.prompts.append((prompt, kw.get("extra_context", "")))
        if "CONFIDENCE: <0-100>%" in prompt:          # a draft
            ans = self.answer or "The pump runs 40 litres per minute at 3 bar"
            return f"{ans}\nCONFIDENCE: {self.pct}% — none"
        if "cross-fire message" in prompt:
            return self.cross
        if "For EACH of these members" in prompt:
            return "TO WRITER:\nQ1: a?\nQ2: b?\nTO INTERN:\nQ1: c?\nQ2: d?"
        if "rebuttal" in prompt:
            return "rebut"
        if "Q1" in prompt or "NEW questions" in prompt:
            return "Q1: one?\nQ2: two?"
        ans = self.answer or "The pump runs 40 litres per minute at 3 bar"
        return f"{ans}\nCONFIDENCE: {self.pct}% — none"

    def count(self, text):
        return sum(1 for p, _ in self.prompts if text in p)


class Judge:
    """Routes to "intern": the panel is intern, writer, peasant."""

    def __init__(self, route="intern"):
        self.calls, self._route = [], route

    def route(self, text):
        return self._route

    def rank_candidates(self, user_text, candidates, extra_context=""):
        self.calls.append("rank")
        return json.dumps({"winner": "writer", "confidence": 85,
                           "scores": {"writer": 85}})

    def critique(self, user_text, response, *, extra_context="",
                 query_mode=""):
        self.calls.append("critique")
        return "Verdict: PASS"


def models(**slots):
    m = type("Models", (), {r: None for r in ct.AGENT_NAMES})()
    for k, v in slots.items():
        setattr(m, k, v)
    return m


def run(q, depth="auto", route="intern", **slots):
    judge, events = Judge(route), []
    res = ct.run_turn(q, models(**slots), judge=judge, max_rounds=1,
                      debate_turns=2, on_event=events.append, depth=depth)
    phases = [e.text for e in events if e.kind == "phase"]
    return res, judge, phases, events


def test_a_quick_question_is_one_answer_and_one_check():
    w, i, p = Member("writer"), Member("intern"), Member("peasant")
    res, judge, phases, _ = run("thanks!", route="chat", writer=w, intern=i,
                                peasant=p)
    assert res.ok and res.answer.startswith("The pump runs")
    assert "CONFIDENCE" not in res.answer
    assert judge.calls == ["critique"]
    assert len(w.prompts) == 1 and not i.prompts and not p.prompts
    assert any("Depth: quick" in x for x in phases)


def test_a_standard_question_has_no_cross_fire():
    w, i, p = Member("writer", pct=90), Member("intern", pct=90), Member("peasant")
    res, judge, phases, _ = run("tell me about the plant's second line and "
                                "how it came to be", depth="standard",
                                writer=w, intern=i, peasant=p)
    assert judge.calls == ["rank", "critique"]
    assert w.count("cross-fire message") == 0
    assert any("Cross-fire skipped — standard depth" in x for x in phases)


def test_an_unsure_member_lifts_a_standard_question_into_cross_fire():
    w = Member("writer", pct=30, cross="AGREE: a | DISAGREE: units are off")
    i = Member("intern", pct=90, cross="AGREE: a | DISAGREE: units are off")
    res, judge, phases, _ = run("tell me about the plant's second line",
                                writer=w, intern=i, peasant=Member("peasant"))
    assert any("Cross-fire added" in x for x in phases)
    assert w.count("cross-fire message") == 2


def test_drafts_that_agree_skip_the_cross_fire():
    w, i = Member("writer", pct=90), Member("intern", pct=88)
    res, judge, phases, _ = run("q", depth="deep", writer=w, intern=i,
                                peasant=Member("peasant"))
    assert any("Cross-fire skipped — the 2 drafts agree" in x for x in phases)
    assert w.count("cross-fire message") == 0


def test_a_turn_without_disagreement_ends_the_cross_fire():
    w = Member("writer", answer="Use the P-200 pump", pct=90)
    i = Member("intern", answer="A diaphragm unit is safer in acid", pct=90)
    res, judge, phases, _ = run("q", depth="deep", writer=w, intern=i,
                                peasant=Member("peasant"))
    assert w.count("cross-fire message") == 1        # of 2 turns
    assert any("no disagreements left" in x for x in phases)


def test_the_peasant_asks_about_a_whole_turn_in_one_call():
    w = Member("writer", answer="Use the P-200 pump", pct=90,
               cross="DISAGREE: intern is wrong")
    i = Member("intern", answer="A diaphragm unit is safer in acid", pct=90,
               cross="DISAGREE: writer is wrong")
    p = Member("peasant")
    res, judge, phases, events = run("q", depth="deep", writer=w, intern=i,
                                     peasant=p)
    assert p.count("For EACH of these members") == 2  # one per turn
    texts = [e.text for e in events]
    assert any("Cross-fire questions for intern T1:\nQ1: c?" in t
               for t in texts)


def test_a_cross_fire_turn_runs_side_by_side_with_parallel_members():
    import time
    import threading

    class Slow(Member):
        live = 0
        peak = 0
        lock = threading.Lock()

        def respond(self, prompt, **kw):
            if "cross-fire message" in prompt:
                with Slow.lock:
                    Slow.live += 1
                    Slow.peak = max(Slow.peak, Slow.live)
                time.sleep(0.2)
                with Slow.lock:
                    Slow.live -= 1
            return super().respond(prompt, **kw)

    w = Slow("writer", answer="Use the P-200 pump", pct=90,
             cross="DISAGREE: no")
    i = Slow("intern", answer="A diaphragm unit is safer in acid", pct=90,
             cross="DISAGREE: no")
    judge, events = Judge(), []
    ct.run_turn("q", models(writer=w, intern=i, peasant=Member("peasant")),
                judge=judge, max_rounds=1, debate_turns=1, depth="deep",
                parallel_members=True, on_event=events.append)
    assert Slow.peak == 2
    assert any("Cross-fire T1 — 2 members at once" in e.text for e in events)
