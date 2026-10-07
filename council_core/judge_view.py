"""
council_core.judge_view — what the Judge reads of each candidate.

The Judge's ranking used to cut every answer at 2,000 characters. A coding
answer longer than ~50 lines was ranked without its second half — often
where the bug is — and lost to a tidy short answer it could not be compared
with.

Now:
  * each answer gets a share of the Judge's real context window
    (answer_budget), not a fixed 2,000 characters;
  * an answer longer than its share is shortened by context_condenser's
    deterministic condenser — it keeps the start, the end and the lines that
    share words with the question, and marks how much it left out — instead
    of losing everything after a cut;
  * every Python code block is checked without being run (tool_kit.lint:
    syntax errors, undefined names, unused imports) and the result goes to
    the Judge as a CODE CHECK line, so the ranking rests on facts as well as
    impressions.

No model calls. Never raises.
"""
from __future__ import annotations

import re
from typing import List, Optional

CHARS_PER_TOKEN = 3.5
#: Tokens kept back for the Judge's instructions, the question, evidence and
#: its reply.
RESERVE_TOKENS = 2500
MIN_CHARS = 2000
MAX_CHARS = 24000
#: The rest of a candidate (Peasant questions, rebuttal, discussion) gets
#: this share of what the answer gets.
SIDE_SHARE = 0.3

_FENCE = re.compile(r"```([\w+-]*)[^\n]*\n(.*?)```", re.DOTALL)


def answer_budget(n_ctx: int, n_candidates: int) -> int:
    """Characters of each candidate's answer the Judge can read."""
    n = max(1, int(n_candidates))
    room = max(0, int(n_ctx) - RESERVE_TOKENS) * CHARS_PER_TOKEN
    per = room / n / (1 + SIDE_SHARE)
    return int(max(MIN_CHARS, min(MAX_CHARS, per)))


def judge_window(judge) -> int:
    """The Judge's context window in tokens (4096 when unknown)."""
    try:
        import council_engine
        return int(council_engine.effective_n_ctx(
            getattr(judge, "name", "") or "judge"))
    except Exception:                                     # noqa: BLE001
        return 4096


def fit(text: str, max_chars: int, question: str = "") -> str:
    """`text`, or a condensed copy that fits `max_chars`."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    try:
        import context_condenser as cc
        out = cc.condense_deterministic(
            text, max(200, int(max_chars / CHARS_PER_TOKEN)),
            terms=cc.extract_terms(question),
            estimate_tokens=lambda t: int(len(t) / CHARS_PER_TOKEN) + 1)
        if len(out) <= max_chars * 1.1:
            return out
    except Exception:                                     # noqa: BLE001
        pass
    half = max_chars // 2 - 40
    return (text[:half] + f"\n…[{len(text) - 2 * half} characters left out]…\n"
            + text[-half:])


def code_blocks(text: str) -> List[str]:
    """The Python code blocks in an answer (``` fences tagged python, py or
    untagged-but-Python-looking)."""
    out = []
    for lang, body in _FENCE.findall(text or ""):
        lang = lang.lower()
        if lang in ("python", "py", "python3") or (
                not lang and re.search(r"^\s*(def |import |from \w+ import |class )",
                                       body, re.M)):
            out.append(body)
    return out


def code_check(text: str) -> str:
    """One line per Python block: no problems, or what is wrong.
    "" when the answer has no Python."""
    blocks = code_blocks(text)
    if not blocks:
        return ""
    from .tool_kit import lint
    lines = []
    for i, body in enumerate(blocks, 1):
        try:
            problems = lint(body, f"block {i}")
        except Exception as exc:                          # noqa: BLE001
            problems = [f"could not check ({exc})"]
        if problems:
            shown = "; ".join(problems[:5])
            more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
            lines.append(f"block {i}: {shown}{more}")
        else:
            lines.append(f"block {i}: no syntax errors or undefined names")
    return "CODE CHECK (parsed, not run): " + " | ".join(lines)


def has_problems(check: str) -> bool:
    """Did code_check find anything?"""
    return "line " in check or "could not check" in check


__all__ = ["answer_budget", "judge_window", "fit", "code_blocks",
           "code_check", "has_problems", "MIN_CHARS", "MAX_CHARS"]
