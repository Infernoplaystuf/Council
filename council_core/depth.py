"""
council_core.depth — how much council a question gets, and when the debate
can stop early.

Every question used to get the same machinery: drafts, the Peasant's
questions, rebuttals and two turns of cross-fire — about 30 model calls for
"thanks, what's a good name for this file" as for "refactor this module".

    QUICK     one member (the Writer when there is one) answers, the Judge
              checks it. ~2 calls. Greetings, thanks, short plain questions.
    STANDARD  drafts, the Peasant's questions, rebuttals, the Judge, the
              Writer. No cross-fire. ~15 calls.
    DEEP      everything, as before.

`decide` picks one from the question and the Judge's keyword route — no
model call. The Council tab's Depth box can force one ("Auto" lets it
decide), and a member that reports low confidence lifts a Standard question
to Deep: its cross-fire runs after all.

EARLY AGREEMENT. Cross-fire exists to settle disagreements. `agree` says
when there are none to settle: every draft at HIGH confidence or more, and
every pair of drafts sharing most of their content words. `has_disagreement`
reads a cross-fire turn's messages (AGREE: … | DISAGREE: … | ADD: …); a
turn with no real DISAGREE ends the cross-fire.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations
from typing import Dict, Iterable, Optional

QUICK, STANDARD, DEEP = "quick", "standard", "deep"
AUTO = "auto"
LEVELS = (QUICK, STANDARD, DEEP)

#: Routes that always get the whole council.
DEEP_ROUTES = frozenset({"ide", "coder", "strategist", "director"})
#: Routes a short question may answer quickly.
QUICK_ROUTES = frozenset({"chat", "peasant", "writer", "_default", ""})

QUICK_MAX_WORDS = 18
DEEP_MIN_CHARS = 600
#: Two drafts "agree" at this share of shared content words.
AGREE_OVERLAP = 0.45

_DEEP_WORDS = re.compile(
    r"\b(design|architect\w*|refactor\w*|debug\w*|plan|plans|planning|compare|"
    r"trade-?offs?|strategy|optimi[sz]\w*|analy[sz]e|investigate|evaluate|"
    r"why does|why is|root cause|pros and cons|step[- ]by[- ]step|"
    r"implement\w*|migrat\w*)\b", re.IGNORECASE)
_QUICK_START = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|cool|great|good (morning|"
    r"afternoon|evening)|what is|what's|who is|who's|when (is|was|did)|"
    r"where is|define|how do you spell|what does .{1,30} (mean|stand for)|"
    r"how many|is it|can you|could you)\b", re.IGNORECASE)
_STOP = frozenset("""a an the and or but if then of to in on at for with by
from as is are was were be been being it its this that these those i you he
she we they them my your our their me us do does did not no yes so than too
very can will would should could may might must have has had what which who
whom when where why how all any both each few more most other some such only
own same just also into over under again further once here there about
against between through during before after above below up down out off
confidence""".split())


@dataclass(frozen=True)
class Depth:
    level: str
    reason: str
    forced: bool = False

    def note(self) -> str:
        how = "set in the Council tab" if self.forced else self.reason
        return f"Depth: {self.level} — {how}."

    @property
    def cross_fire(self) -> bool:
        return self.level == DEEP

    @property
    def debate(self) -> bool:
        return self.level != QUICK


def decide(question: str, route: str = "", *, forced: str = AUTO) -> Depth:
    """The depth for `question` (no model call)."""
    forced = (forced or AUTO).strip().lower()
    if forced in LEVELS:
        return Depth(forced, "", forced=True)
    q = (question or "").strip()
    words = len(q.split())
    if "```" in q or len(q) >= DEEP_MIN_CHARS:
        return Depth(DEEP, "a long question or one with code")
    if route in DEEP_ROUTES:
        return Depth(DEEP, f"a {route} question")
    if _DEEP_WORDS.search(q):
        return Depth(DEEP, "it asks for design, analysis or planning")
    if route in QUICK_ROUTES and words <= QUICK_MAX_WORDS and (
            _QUICK_START.search(q) or words <= 6):
        return Depth(QUICK, "a short, plain question")
    return Depth(STANDARD, "an ordinary question")


def _content_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9_]{3,}", (text or "").lower())
            if w not in _STOP}


def overlap(a: str, b: str) -> float:
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def agree(candidates: Dict[str, dict], high: Optional[int] = None) -> str:
    """Why the drafts need no cross-fire, or "" when they do."""
    from .confidence import HIGH
    high = HIGH if high is None else high
    items = [(k, v) for k, v in candidates.items() if k != "peasant"]
    if len(items) < 2:
        return ""
    if any(int(v.get("self_confidence", 0)) < high for _k, v in items):
        return ""
    for (_a, va), (_b, vb) in combinations(items, 2):
        if overlap(va.get("answer", ""), vb.get("answer", "")) < AGREE_OVERLAP:
            return ""
    lowest = min(int(v.get("self_confidence", 0)) for _k, v in items)
    return (f"the {len(items)} drafts agree and every member is at least "
            f"{lowest}% confident")


_DISAGREE = re.compile(r"DISAGREE\s*[:\-]\s*(.*?)(?:\||\n\s*(?:AGREE|ADD)\s*[:\-]|$)",
                       re.IGNORECASE | re.DOTALL)
_NOTHING = re.compile(r"^\s*[\(\[]?\s*(none|nothing|no(ne)? disagreements?|n/?a|"
                      r"-+|—|no|nil|not applicable|i agree|agreed)\b[\s.\)\]]*$",
                      re.IGNORECASE)


def has_disagreement(messages: Iterable[str]) -> bool:
    """Does any cross-fire message carry a real DISAGREE?"""
    for msg in messages:
        for m in _DISAGREE.finditer(msg or ""):
            body = m.group(1).strip().strip("*").strip()
            if body and not _NOTHING.match(body):
                return True
    return False


__all__ = ["Depth", "decide", "agree", "overlap", "has_disagreement",
           "QUICK", "STANDARD", "DEEP", "AUTO", "LEVELS"]
