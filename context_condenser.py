"""
context_condenser.py — fit oversized context into a small window by
CHUNKING + CONDENSING instead of dropping it.

The prompt pipeline (council_gui_engine._inject_file_contents_impl) assembles
context blocks under a token budget. When they overflow a small context
window it used to EVICT the lowest-priority blocks entirely — so on a 4K-ctx
machine a big file or a wide vault-match set simply vanished from the model's
view. The user's original request always survives (see task_memory.py's
[TASK MEMO]); this module rescues the *context* the same way: split a block
into sections and keep a condensed, task-relevant digest that fits.

Two modes:
  • deterministic (default, no latency): split into lines, score each by
    overlap with the task-memo terms, and keep head + highest-scoring +
    tail lines until the target budget is reached. A marker records how
    much was elided. Strictly better than dropping the whole block.
  • LLM map-reduce (opt-in, llm_call provided): split into chunks that fit
    the window, ask the model to extract only what's relevant to the task
    from each, then combine. Effectively extends usable context past n_ctx
    at the cost of N extra model calls.

Pure functions; estimate_tokens + llm_call are injected so the module is
dependency-free and unit-testable.
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional, Sequence

# Below this many tokens of headroom, condensing isn't worth it — drop.
DEFAULT_MIN_TOKENS = 64
# Words too generic to be useful relevance signals.
_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "for", "to", "and", "or", "is", "it",
    "what", "how", "with", "by", "at", "from", "this", "that", "these", "those",
    "do", "does", "are", "was", "were", "be", "been", "as", "if", "all", "any",
    "show", "tell", "give", "me", "my", "your", "please", "list", "find",
}


def _approx_tokens(text: str) -> int:
    """Char/4 fallback when no estimator is supplied."""
    return max(1, len(text or "") // 4)


def extract_terms(*texts: str) -> List[str]:
    """Salient lowercase terms from the task memo / query for relevance
    scoring. Keeps words >2 chars that aren't stopwords, plus any
    filename-like tokens (they're strong signals)."""
    terms: List[str] = []
    seen = set()
    for t in texts:
        for raw in re.findall(r"[A-Za-z0-9_.\-]+", (t or "").lower()):
            tok = raw.strip(".-_")
            if not tok or tok in _STOPWORDS or len(tok) <= 2:
                continue
            if tok not in seen:
                seen.add(tok)
                terms.append(tok)
    return terms


def _line_score(line: str, terms: Sequence[str]) -> int:
    """How many distinct memo terms appear in this line."""
    low = line.lower()
    return sum(1 for t in terms if t in low)


def chunk_by_tokens(text: str, max_tokens: int,
                    estimate_tokens: Optional[Callable[[str], int]] = None
                    ) -> List[str]:
    """Split text into chunks each <= max_tokens, breaking on line
    boundaries (never mid-line). A single over-long line becomes its own
    chunk rather than being split mid-word."""
    est = estimate_tokens or _approx_tokens
    max_tokens = max(1, int(max_tokens))
    chunks: List[str] = []
    cur: List[str] = []
    cur_tok = 0
    for line in (text or "").splitlines():
        lt = est(line) + 1
        if cur and cur_tok + lt > max_tokens:
            chunks.append("\n".join(cur))
            cur, cur_tok = [], 0
        cur.append(line)
        cur_tok += lt
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def condense_deterministic(text: str, target_tokens: int, *,
                           terms: Sequence[str] = (),
                           estimate_tokens: Optional[Callable[[str], int]] = None,
                           head_lines: int = 3,
                           tail_lines: int = 2) -> str:
    """Shrink `text` to ~target_tokens with NO model call.

    Always keeps the first ``head_lines`` and last ``tail_lines`` (structure
    / headers / totals), then fills the remaining budget with the lines that
    score highest on task-memo term overlap, restored to original order. A
    marker records how many lines were elided so the model knows the block
    is partial.
    """
    est = estimate_tokens or _approx_tokens
    if est(text) <= target_tokens:
        return text
    lines = (text or "").splitlines()
    n = len(lines)
    if n <= head_lines + tail_lines:
        return text  # too short to meaningfully cut on line boundaries

    keep_idx = set(range(min(head_lines, n)))
    keep_idx.update(range(max(0, n - tail_lines), n))

    # Rank the middle lines by relevance, then by length (informative).
    middle = [i for i in range(n) if i not in keep_idx]
    ranked = sorted(
        middle,
        key=lambda i: (_line_score(lines[i], terms), len(lines[i])),
        reverse=True,
    )

    # Reserve room for the summary note + elision markers so the FORMATTED
    # output (not just the kept lines) fits the target.
    note_overhead = 30
    budget = max(1, target_tokens - note_overhead)
    running = sum(est(lines[i]) + 1 for i in sorted(keep_idx))
    chosen_middle: List[int] = []
    for i in ranked:
        c = est(lines[i]) + 1
        if running + c > budget:
            continue
        chosen_middle.append(i)
        running += c

    def _render(mids: List[int]) -> str:
        kept = sorted(set(keep_idx) | set(mids))
        elided = n - len(kept)
        out: List[str] = []
        prev = -1
        for i in kept:
            if i != prev + 1:
                out.append(f"   … [{i - prev - 1} line(s) elided] …")
            out.append(lines[i])
            prev = i
        note = (f"[condensed to fit context — kept {len(kept)} of {n} "
                f"lines most relevant to your request; {elided} elided]")
        return note + "\n" + "\n".join(out)

    # Final guarantee: if the formatted result still exceeds target (many
    # elision markers can tip it over), drop the lowest-ranked kept middle
    # lines until it fits. Head/tail lines are never dropped.
    result = _render(chosen_middle)
    while est(result) > target_tokens and chosen_middle:
        chosen_middle.pop()           # ranked desc -> pop lowest relevance
        result = _render(chosen_middle)
    return result


_LLM_EXTRACT_PROMPT = """\
You are condensing one SECTION of context so it fits a small model's window.
Keep ONLY facts relevant to the task; drop everything else. Be terse, keep
exact numbers / names / file paths verbatim. No commentary, no preamble.

TASK:
{task}

SECTION {idx}/{total}:
{chunk}

RELEVANT FACTS (terse bullet list):"""


def condense_with_llm(text: str, target_tokens: int, *,
                      task: str = "",
                      chunk_tokens: int = 1024,
                      estimate_tokens: Optional[Callable[[str], int]] = None,
                      llm_call: Optional[Callable[[str], str]] = None,
                      terms: Sequence[str] = ()) -> str:
    """Map-reduce condense: split into window-sized chunks, ask the model to
    extract task-relevant facts from each, combine. Falls back to the
    deterministic condenser when no llm_call is given or a call fails, and
    always finishes with a deterministic trim so the result fits."""
    est = estimate_tokens or _approx_tokens
    if est(text) <= target_tokens:
        return text
    if llm_call is None:
        return condense_deterministic(text, target_tokens, terms=terms,
                                      estimate_tokens=est)
    chunks = chunk_by_tokens(text, chunk_tokens, est)
    extracts: List[str] = []
    for idx, ch in enumerate(chunks, 1):
        try:
            out = llm_call(_LLM_EXTRACT_PROMPT.format(
                task=task or "(no explicit task — keep salient facts)",
                idx=idx, total=len(chunks), chunk=ch))
            if out and out.strip():
                extracts.append(out.strip())
        except Exception:
            # Skip a failed chunk's model pass but keep a deterministic
            # digest of it so its content isn't lost entirely.
            extracts.append(condense_deterministic(
                ch, max(64, chunk_tokens // 4), terms=terms,
                estimate_tokens=est))
    combined = "\n".join(extracts)
    # The combined extracts may still exceed target — deterministic-trim.
    if est(combined) > target_tokens:
        combined = condense_deterministic(
            combined, target_tokens, terms=terms, estimate_tokens=est)
    return (f"[condensed from {len(chunks)} section(s) to fit context]\n"
            + combined)


def condense_to_fit(text: str, target_tokens: int, *,
                    task: str = "",
                    estimate_tokens: Optional[Callable[[str], int]] = None,
                    llm_call: Optional[Callable[[str], str]] = None,
                    min_tokens: int = DEFAULT_MIN_TOKENS,
                    chunk_tokens: int = 1024) -> Optional[str]:
    """Top-level: condense `text` to <= target_tokens, guided by `task`
    (the task-memo text). Returns the condensed string, or None when the
    target is too small to bother (caller should drop instead).

    Uses the LLM map-reduce when `llm_call` is given, else the deterministic
    relevance keep. The original text is returned unchanged if it already
    fits.
    """
    est = estimate_tokens or _approx_tokens
    if target_tokens < min_tokens:
        return None
    if est(text) <= target_tokens:
        return text
    terms = extract_terms(task)
    if llm_call is not None:
        return condense_with_llm(
            text, target_tokens, task=task, chunk_tokens=chunk_tokens,
            estimate_tokens=est, llm_call=llm_call, terms=terms)
    return condense_deterministic(
        text, target_tokens, terms=terms, estimate_tokens=est)
