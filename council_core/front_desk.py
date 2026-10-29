"""
council_core.front_desk — one model answers first; the council only when needed.

With Deliberation off (the default) a question no longer goes to the whole
council just because it was asked: the Writer answers it on its own, in one
streamed call, and hands it to the council only when

  * the question plainly needs one (`needs_council`, no model call): code, a
    long question, a coding or strategy route, or a request for a design,
    plan, comparison or analysis — but a request to EXPLAIN something
    ("explain the boid algorithm", "what is recursion") stays with the
    first answer unless it also asks for code to be written or changed;
  * the Writer says so: its reply is ``ESCALATE: <why>`` instead of an answer;
  * the Writer is not sure: its CONFIDENCE line is at or below
    confidence.LOW.

Before this, the keyword depth rule (council_core.depth) sent "Why is the sky
blue?" to the deep council, about 30 model calls, and a quick answer could
never be handed on. Deliberation on still means "always the council"; the
single-model chat (COUNCIL_CHAT_MODE=single) never hands anything on.
"""
from __future__ import annotations

import re
from typing import Callable, Optional, Tuple

from . import confidence
from .depth import DEEP_MIN_CHARS, DEEP_ROUTES

#: What the Writer is told, after its usual context, when it answers first.
INSTRUCTION = (
    "You are answering on your own first. If you can answer this well by "
    "yourself, answer it directly and plainly.\n"
    "If it needs more than one careful viewpoint (a design, a plan, a "
    "trade-off, code to write or review, an analysis across many of the "
    "user's files), or you would mostly be guessing, reply with ONLY one "
    "line instead of an answer:\n"
    "ESCALATE: <a few words on why the council should take it>\n"
    "Otherwise " + confidence.DRAFT_INSTRUCTION[0].lower()
    + confidence.DRAFT_INSTRUCTION[1:])

#: Requests that need the council's several drafts and the Judge, decided
#: without a model. Narrower than depth's deep words: "why is" and "why
#: does" are ordinary questions here.
_COUNCIL_WORDS = re.compile(
    r"\b(design|architect\w*|refactor\w*|debug\w*|plan|plans|planning|compare|"
    r"trade-?offs?|strategy|optimi[sz]\w*|analy[sz]e|investigate|evaluate|"
    r"root cause|pros and cons|implement\w*|migrat\w*)\b", re.IGNORECASE)

#: A request to explain something: the first answer is enough, whatever the
#: topic. "Explain the boid algorithm" or "what is the difference between a
#: list and a tuple in Python" went to the council only because the Judge's
#: keyword route calls anything naming a language a coding question.
_EXPLAIN = re.compile(
    r"^\W*(explain|describe|define|tell me about|what (is|are|was|were|does|"
    r"do)\b|what's|whats|how (does|do|is|are)\b(?!\s+(i|we|you)\b)|"
    r"why (does|do|is|are)\b|when (should|would|do|does)\b)|"
    r"\bdifference between\b", re.IGNORECASE)
#: ...unless it also asks for something to be made or changed.
_MAKE = re.compile(
    r"\b(write|build|create|make|generate|fix|refactor\w*|"
    r"debug\w*|rewrite|port|convert|code (me|up)|my code|this code)\b",
    re.IGNORECASE)

_ESCALATE = re.compile(r"^\W*escalate\b\s*[:\-—]?\s*(.*)$",
                       re.IGNORECASE | re.DOTALL)


def needs_council(typed: str, route: str = "") -> str:
    """Why ``typed`` goes straight to the council, or "" when the Writer may
    answer it first."""
    q = (typed or "").strip()
    if "```" in q:
        return "the question has code in it"
    if len(q) >= DEEP_MIN_CHARS:
        return "a long question"
    if _EXPLAIN.search(q) and not _MAKE.search(q):
        return ""
    if route in DEEP_ROUTES:
        return ("a coding question" if route in ("ide", "coder")
                else f"a {route} question")
    if _COUNCIL_WORDS.search(q):
        return "it asks for design, analysis or planning"
    return ""


def read_reply(raw: str) -> Tuple[str, str, Optional[int]]:
    """(answer, why to escalate, confidence) from the Writer's reply.

    ``why`` is non-empty when the council should take the question: the
    Writer asked for it, said nothing, or rated itself at or below
    confidence.LOW. ``answer`` is the reply without its CONFIDENCE line."""
    text = (raw or "").strip()
    m = _ESCALATE.match(text)
    if m:
        why = m.group(1).strip().splitlines()[0].strip() if m.group(1).strip() \
            else ""
        return "", why or "the Writer asked for the council", None
    answer, pct, _reason = confidence.split_draft(text)
    answer = answer.strip()
    if not answer:
        return "", "the Writer gave no answer", pct
    if pct is not None and pct <= confidence.LOW:
        return answer, f"the Writer was only {pct}% sure", pct
    return answer, "", pct


class StreamGate:
    """Holds the first few tokens back until the reply is plainly not an
    ESCALATE line, so a hand-off never shows in the stream box as the start
    of an answer. ``on_token(token)`` receives what is let through."""

    def __init__(self, on_token: Callable[[str], None]):
        self._on_token = on_token
        self._held = ""
        self._open = False
        self.escalating = False

    def __call__(self, token: str) -> None:
        if self._open:
            self._on_token(token)
            return
        if self.escalating:
            return
        self._held += token
        head = self._held.lstrip().lstrip("*_#>` ").upper()
        if head.startswith("ESCALATE") or (head and "ESCALATE".startswith(head)):
            if head.startswith("ESCALATE"):
                self.escalating = True
            return
        self._open = True
        self._on_token(self._held)
        self._held = ""
