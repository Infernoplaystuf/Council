"""Round 2 is a revision when the Judge names what to fix, and the whole
panel again only when it rejects the approach (council_core/deliberation)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import council_turn as ct  # noqa: E402
from council_core.deliberation import needs_full_round  # noqa: E402

NEEDS_WORK = ("=== Judge Critique ===\nVerdict: NEEDS_WORK\nFindings:\n- misses "
              "units\nREQUIRED_CHANGES:\n- State the units (kg)\n- Cite the "
              "manual\n========================")
START_OVER = ("Verdict: NEEDS_WORK\nFindings:\n- This uses the wrong approach "
              "entirely.\nREQUIRED_CHANGES:\n- Use the logs\n")


class Member:
    def __init__(self, name):
        self.name, self.prompts = name, []

    def respond(self, prompt, **kw):
        self.prompts.append(prompt)
        n = len(self.prompts)
        return f"{self.name} answer {n}\nCONFIDENCE: 70% — x"

    def drafts(self):
        return [p for p in self.prompts if "CONFIDENCE: <0-100>%" in p]


class Judge:
    def __init__(self, critiques, confidence=60):
        self.critiques, self.confidence = list(critiques), confidence
        self.calls = []

    def route(self, text):
        return "chat"

    def rank_candidates(self, user_text, candidates, extra_context=""):
        self.calls.append("rank")
        return json.dumps({"winner": "writer", "confidence": self.confidence,
                           "scores": {"writer": 70}})

    def critique(self, user_text, response, *, extra_context="",
                 query_mode=""):
        self.calls.append("critique")
        return self.critiques.pop(0) if self.critiques else "Verdict: PASS"


def models(**slots):
    m = type("Models", (), {r: None for r in ct.AGENT_NAMES})()
    for k, v in slots.items():
        setattr(m, k, v)
    return m


def test_a_named_fix_gets_a_revision_not_a_new_debate():
    writer, peasant = Member("writer"), Member("peasant")
    judge = Judge([NEEDS_WORK, "Verdict: PASS"])
    events = []
    res = ct.run_turn("q", models(writer=writer, peasant=peasant),
                      judge=judge, max_rounds=2, debate_turns=1,
                      on_event=events.append)
    assert res.ok and res.verdict == "PASS"
    # One ranking (round 1) and two critiques: round 2 did not re-rank.
    assert judge.calls == ["rank", "critique", "critique"]
    # Nobody drafted twice; the Writer's last prompt revises its answer.
    assert len(writer.drafts()) == 1 and len(peasant.drafts()) == 1
    last = writer.prompts[-1]
    assert "YOUR PREVIOUS ANSWER" in last and "State the units (kg)" in last
    phases = [e.text for e in events if e.kind == "phase"]
    assert any("Round 2/2 — Writer revises" in p for p in phases)


def test_a_rejected_approach_gets_the_whole_panel_again():
    writer, peasant = Member("writer"), Member("peasant")
    judge = Judge([START_OVER, "Verdict: PASS"])
    ct.run_turn("q", models(writer=writer, peasant=peasant), judge=judge,
                max_rounds=2, debate_turns=1)
    assert judge.calls.count("rank") == 2
    assert len(writer.drafts()) == 2


def test_the_extra_round_for_very_low_confidence_really_runs():
    """A7: `for r in range(self.max_rounds)` fixed the count before the
    escalation to 3 rounds, while the transcript announced it."""
    writer = Member("writer")
    judge = Judge([NEEDS_WORK] * 5, confidence=10)
    events = []
    ct.run_turn("q", models(writer=writer), judge=judge, max_rounds=2,
                debate_turns=1, on_event=events.append)
    assert judge.calls.count("critique") == 3
    assert any("Round 3/3" in e.text for e in events if e.kind == "phase")


def test_required_changes_are_read_even_at_very_low_confidence():
    """A8: the `else` that parsed them belonged to the low-confidence
    branch, so they were skipped exactly when most needed."""
    writer = Member("writer")
    judge = Judge([NEEDS_WORK] * 5, confidence=10)
    events = []
    ct.run_turn("q", models(writer=writer), judge=judge, max_rounds=2,
                debate_turns=1, on_event=events.append)
    assert any("Required changes for next round" in e.text
               and "State the units" in e.text for e in events)


def test_needs_full_round():
    assert needs_full_round(NEEDS_WORK, ["State the units"], 60) == ""
    assert "approach" in needs_full_round(START_OVER, ["x"], 60)
    assert "no specific" in needs_full_round("Verdict: NEEDS_WORK", [], 60)
    assert "very low" in needs_full_round(NEEDS_WORK, ["x"], 15)
