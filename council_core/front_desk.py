"""
council_core.front_desk — one model answers first; the council only when needed.

With Deliberation off (the default) a question no longer goes to the whole
council just because it was asked: the Writer answers it on its own, in one
streamed call, and hands it to the council only when

  * the question plainly needs one (`needs_council`, no model call): code, a
    long question, a coding or strategy route, or a request for a design,
    plan, comparison or analysis — but a request to EXPLAIN something
    ("explain the boid algorithm", "what is recursion") stays with the
    first answer unless it also asks for code to be written or changed.
    One that names a language ("explain boids in Python") is answered as a
    short tutorial ending in a runnable program (GUIDE), by the Coder when
    there is one;
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

#: Languages an explanation can ask for, by how the user writes them. C, R
#: and Go are common words or letters, so they count only after "in",
#: "using" or "with" ("explain quicksort in C").
_LANGUAGES = {
    "python": "Python", "javascript": "JavaScript", "typescript": "TypeScript",
    "java": "Java", "c++": "C++", "cpp": "C++", "c#": "C#", "csharp": "C#",
    "rust": "Rust", "golang": "Go", "ruby": "Ruby", "php": "PHP",
    "swift": "Swift", "kotlin": "Kotlin", "matlab": "MATLAB",
    "julia": "Julia", "scala": "Scala", "haskell": "Haskell", "lua": "Lua",
    "perl": "Perl", "bash": "Bash", "powershell": "PowerShell", "sql": "SQL",
}
_LANGUAGE = re.compile(
    r"(?<![\w+#])(" + "|".join(re.escape(k) for k in sorted(
        _LANGUAGES, key=len, reverse=True)) + r")(?![\w+#])", re.IGNORECASE)
_SHORT_LANGUAGE = re.compile(r"\b(?:in|using|with)\s+(c|r|go)\b(?![+#])",
                             re.IGNORECASE)

#: Added to INSTRUCTION when an explanation names a language (asked
#: 2026-10-29: "explain conceptually but then also write a Python script
#: that shows the algorithm. Something similar to a GeeksForGeeks guide").
GUIDE = (
    "The user asked for this explained in {lang}. Answer as a short tutorial "
    "in this order:\n"
    "1. The idea in plain words, before any code: what it does and why it "
    "works.\n"
    "2. How it works, step by step (pseudocode if it helps).\n"
    "3. ONE complete, runnable {lang} program that shows it, in a single "
    "code block: standard library only unless the topic needs more, small "
    "sample input built in, comments on the key lines, and a main entry "
    "point that prints a result.\n"
    "4. What it prints when run.\n"
    "5. Time and space complexity, and a common pitfall or variation.\n"
    "Keep it to what a reader needs; no filler.")

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
    if is_explanation(q):
        return ""
    if route in DEEP_ROUTES:
        return ("a coding question" if route in ("ide", "coder")
                else f"a {route} question")
    if _COUNCIL_WORDS.search(q):
        return "it asks for design, analysis or planning"
    return ""


def is_explanation(typed: str) -> bool:
    """A request to explain something, not to make or change code."""
    q = (typed or "").strip()
    return bool(_EXPLAIN.search(q)) and not _MAKE.search(q)


def guide_language(typed: str) -> str:
    """The language an explanation asks for ("explain boids in Python" ->
    "Python"), or "" when it is not an explanation or names none."""
    q = (typed or "").strip()
    if "```" in q or not is_explanation(q):
        return ""
    m = _LANGUAGE.search(q)
    if m:
        return _LANGUAGES[m.group(1).lower()]
    m = _SHORT_LANGUAGE.search(q)
    if m:
        return {"c": "C", "r": "R", "go": "Go"}[m.group(1).lower()]
    return ""


def instruction(typed: str) -> str:
    """What the first answer is told: INSTRUCTION, and GUIDE when the
    question asks for an explanation in a language."""
    lang = guide_language(typed)
    return INSTRUCTION + ("\n\n" + GUIDE.format(lang=lang) if lang else "")


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
