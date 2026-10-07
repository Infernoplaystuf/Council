"""Confidence as a percentage (council_core/confidence.py) and the member's
CONFIDENCE line inside its draft."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import confidence as cf  # noqa: E402
from council_core import council_turn as ct  # noqa: E402


@pytest.mark.parametrize("text,pct,reason", [
    ("Answer.\nCONFIDENCE: 85% — the units in 2019", 85, "the units in 2019"),
    ("Answer.\n**Confidence:** 72%", 72, ""),
    ("Answer.\nConfidence: 64 percent - unsure of the date", 64, "unsure of the date"),
    ("Answer.\nCONFIDENCE: 7/10", 70, ""),
    ("Answer.\nCONFIDENCE: 150%", 100, ""),
])
def test_split_draft(text, pct, reason):
    cleaned, got, why = cf.split_draft(text)
    assert cleaned == "Answer." and got == pct and why == reason


def test_a_draft_without_the_line_is_untouched():
    assert cf.split_draft("Just an answer.") == ("Just an answer.", None, "")


def test_the_last_line_wins_and_every_copy_of_it_is_not_left_behind():
    text = "CONFIDENCE: 10%\nBody.\nCONFIDENCE: 90% — fine"
    cleaned, pct, _ = cf.split_draft(text)
    assert pct == 90 and "CONFIDENCE: 90" not in cleaned and "Body." in cleaned


@pytest.mark.parametrize("raw,pct", [("85", 85), ("85%", 85), ("0.85", 85),
                                     ("8/10", 80), ("about 67 percent", 67),
                                     ("none", None)])
def test_parse_reply(raw, pct):
    assert cf.parse_reply(raw) == pct


def test_rankings_on_the_old_scale_are_converted():
    old = cf.normalise_ranking({"winner": "coder", "confidence": 8,
                                "scores": {"coder": 9, "writer": 6}})
    assert old["confidence"] == 80 and old["scores"] == {"coder": 90,
                                                         "writer": 60}
    new = cf.normalise_ranking({"winner": "coder", "confidence": 73,
                                "scores": {"coder": 88, "writer": 41}})
    assert new["confidence"] == 73 and new["scores"]["writer"] == 41
    assert "scale_converted" not in new


def test_legacy_session_records_show_as_percent():
    from council_core import sessions
    assert "70%" in sessions.session_label("s", {"confidence": 7,
                                                 "passed": True})
    assert "73%" in sessions.session_label("s", {"confidence_pct": 73,
                                                 "confidence": 7,
                                                 "passed": True})


class Member:
    """Answers with a CONFIDENCE line when the prompt asks for one."""

    def __init__(self, pct=None, reason="the date"):
        self.pct, self.reason, self.asked = pct, reason, []

    def respond(self, prompt, **kw):
        self.asked.append(prompt)
        if "How confident are you" in prompt:
            return "55"
        if "CONFIDENCE: <0-100>%" in prompt and self.pct is not None:
            return f"The answer.\nCONFIDENCE: {self.pct}% — {self.reason}"
        return "The answer."


class Judge:
    def __init__(self):
        self.seen = None

    def route(self, text):
        return "chat"

    def rank_candidates(self, user_text, candidates, extra_context=""):
        self.seen = candidates
        return json.dumps({"winner": "writer", "confidence": 76,
                           "scores": {"writer": 81}})

    def critique(self, user_text, response, *, extra_context="",
                 query_mode=""):
        return "Verdict: PASS"


def _models(**slots):
    m = type("Models", (), {r: None for r in ct.AGENT_NAMES})()
    for k, v in slots.items():
        setattr(m, k, v)
    return m


def test_the_draft_carries_its_confidence_and_no_extra_call_is_made():
    writer = Member(pct=37, reason="the 2019 units")
    judge = Judge()
    events = []
    res = ct.run_turn("q", _models(writer=writer, peasant=Member(pct=90)),
                      judge=judge, max_rounds=1, debate_turns=1,
                      on_event=events.append)
    assert res.ok
    assert judge.seen["writer"]["self_confidence"] == 37
    assert judge.seen["writer"]["confidence_reason"] == "the 2019 units"
    assert "CONFIDENCE:" not in judge.seen["writer"]["answer"]
    assert not any("How confident are you" in p for p in writer.asked)
    texts = [e.text for e in events]
    assert any("Self-confidence: 37%" in t and "the 2019 units" in t
               for t in texts)
    assert res.confidence == 76
    # Low confidence reaches the wishlist with the member's own reason.
    assert any("37%" in g["reason"] and "2019 units" in g["reason"]
               for g in res.low_conf_gaps)


def test_a_draft_without_the_line_falls_back_to_one_question():
    writer = Member(pct=None)
    judge = Judge()
    ct.run_turn("q", _models(writer=writer, peasant=Member(pct=90)),
                judge=judge, max_rounds=1, debate_turns=1)
    assert judge.seen["writer"]["self_confidence"] == 55
    fallback = [p for p in writer.asked if "How confident are you" in p]
    assert len(fallback) == 1 and "The answer." in fallback[0]
