"""
goal_anchor.py — distill the user's intent into a one-line "goal" string
that gets re-injected at every agent step so small local models (8B-ish)
don't lose the thread after large file-injection blocks land in context.

Why this exists
---------------
The injection pipeline routinely puts ~3K tokens of CSV / vault / analyst
output ahead of the user's original question. On a Granite-8B context the
question ends up at the tail, drowned out by the data. Every agent step
(council deliberation, coder retries, analyst followups) then re-reads
that augmented blob and has to guess intent.

The goal anchor is a tiny (<= ~180 char) restatement of what the user is
actually asking, computed once per turn, threaded through AgentContext,
and re-injected at the TOP and BOTTOM of every prompt so the model
anchors on it regardless of how much data sits in the middle.

Distillation strategy (hybrid)
------------------------------
1. Strip obvious paste-bloat from the user message (fenced code blocks,
   file paths, runs of tabular-looking lines, runs of very long lines).
2. If what remains is short enough, use it verbatim — no LLM call.
3. Otherwise call the provided model with a tight summarize prompt
   (max ~60 tokens out). If the model errors, fall back to a hard
   truncation of the stripped text. Never raise — callers always get
   a non-empty string.
"""

from __future__ import annotations

import re
from typing import Any, Optional


# ============================================================
# Anaphora detection — does the user reference a prior turn?
# ============================================================

# Phrases at the START of the message that strongly suggest the user
# is continuing from a prior question rather than starting fresh.
_LEADING_FOLLOWUP_RE = re.compile(
    r"^\s*(?:"
    r"now\s+(?:do|try|run|show|give)\s+(?:that|the\s+same|it)|"
    r"again\b|"
    r"same\s+thing\b|"
    r"do\s+(?:it|that)\s+(?:again|for)|"
    r"and\s+(?:now|then)\b|"
    r"what\s+about\b|"
    r"how\s+about\b|"
    r"but\s+(?:for|with|without)\b"
    # NOTE: bare "ok" / "okay" was previously listed here but caused
    # false positives on conversational filler like "ok so I have a
    # fresh question…". Real anaphora after "ok" almost always uses
    # one of the other cues above ("ok now do that", "ok again", "ok
    # do it for X") so dropping the bare-ok branch loses nothing.
    r")",
    re.IGNORECASE,
)

# Words that, anywhere in the message, suggest a back-reference.
_REFERENCE_WORDS_RE = re.compile(
    r"\b(?:"
    r"that\s+(?:file|result|answer|number|chart|row|table)|"
    r"the\s+(?:previous|last|prior|earlier|above)\s+\w+|"
    r"like\s+before|"
    r"as\s+before|"
    r"same\s+as\s+(?:above|before|last)"
    r")\b",
    re.IGNORECASE,
)


def looks_like_followup(user_text: str) -> bool:
    """Heuristic: does this message lean on prior conversation context?"""
    if not user_text:
        return False
    t = user_text.strip()
    if len(t) < 200 and _LEADING_FOLLOWUP_RE.search(t):
        return True
    if _REFERENCE_WORDS_RE.search(t):
        return True
    return False


# ============================================================
# Paste-bloat stripping
# ============================================================

# Fenced code blocks — ``` ... ```
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)

# Windows-style paths (C:\...) or POSIX paths (/foo/bar/...) anywhere
# in the message. The injection layer has its own path extractor; we
# just need to remove paths from the goal text so the distillation
# doesn't waste tokens on them.
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s\"'<>|]+|"
    r"/[A-Za-z0-9_\-./~][^\s\"'<>|]*)",
)


def _strip_bloat(text: str) -> str:
    """Remove fenced code, paths, and runs of pasted/tabular lines."""
    if not text:
        return ""
    t = _FENCED_CODE_RE.sub(" [code] ", text)
    t = _PATH_RE.sub(" [path] ", t)

    # Drop lines that look pasted: very long, or comma/tab-heavy
    # (likely CSV/TSV rows), or runs of repeating "key: value" lines.
    kept = []
    for line in t.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if len(stripped) > 200:
            continue
        # crude CSV row detector: 3+ commas and no question/sentence cues
        if stripped.count(",") >= 3 and "?" not in stripped and not stripped.endswith("."):
            continue
        # tab-separated
        if stripped.count("\t") >= 2:
            continue
        kept.append(line)
    out = "\n".join(kept)
    # collapse run-on whitespace
    out = re.sub(r"\s+", " ", out).strip()
    return out


# ============================================================
# Distillation
# ============================================================

_DISTILL_PROMPT = (
    "Summarize the user's request below in ONE short sentence "
    "(at most 20 words). Output ONLY the sentence — no preamble, "
    "no quotes, no explanation. Strip out file paths, pasted code, "
    "and pasted data; keep only what the user actually wants done.\n\n"
    "USER MESSAGE:\n{text}\n\n"
    "GOAL:"
)


def distill_goal(
    user_text: str,
    *,
    model: Any = None,
    max_chars: int = 180,
) -> str:
    """Return a <= ``max_chars`` one-line statement of what the user wants.

    ``model`` is optional. When provided it must expose ``.respond(prompt,
    max_tokens=...)`` (i.e. a ``council_engine.PersonalityModel``-like).
    When omitted or when the heuristic path suffices, no LLM call is made.
    Never raises — always returns a non-empty string.
    """
    if not user_text or not user_text.strip():
        return "(no user request)"

    stripped = _strip_bloat(user_text)

    # Heuristic path: the de-bloated text already fits the budget.
    if stripped and len(stripped) <= max_chars:
        return stripped

    # LLM path: ask the model to compress, with a hard cap on output.
    if model is not None:
        try:
            raw = model.respond(
                _DISTILL_PROMPT.format(text=user_text[:4000]),
                max_tokens=60,
            )
            line = (raw or "").strip().splitlines()[0].strip()
            # Strip leading bullets / numbering the model might add
            line = re.sub(r"^[\-\*•\d\.\)\s]+", "", line).strip()
            if line:
                return line[:max_chars]
        except Exception:
            pass

    # Fallback: hard truncate the de-bloated text (or the raw text if
    # stripping nuked everything).
    base = stripped or user_text.strip()
    return base[: max_chars - 1].rstrip() + "…"


# ============================================================
# Prompt-injection formatters
# ============================================================

def format_goal_header(goal: str) -> str:
    """Block to place near the TOP of an agent prompt (primacy slot)."""
    if not goal:
        return ""
    return (
        "[USER GOAL — your single objective for this turn. Anything "
        "injected below is context to help you achieve it, not the "
        "task itself.]\n"
        f"  {goal}"
    )


def format_goal_reminder(goal: str) -> str:
    """Short reminder for the BOTTOM of an agent prompt (recency slot)."""
    if not goal:
        return ""
    return f"⚑ REMEMBER — the user's goal is: {goal}"


def format_followup_goal(goal: str, recent_goals: list) -> str:
    """When the current turn is a follow-up, fold prior goals into the anchor.

    ``recent_goals`` is a list of strings (oldest → newest, excluding the
    current turn). Empty list short-circuits back to plain ``goal``.
    """
    if not recent_goals:
        return goal
    lines = ["(this turn references prior conversation)"]
    for i, prev in enumerate(recent_goals[-3:], 1):
        lines.append(f"  prior goal {i}: {prev}")
    lines.append(f"  current ask: {goal}")
    return "\n".join(lines)
