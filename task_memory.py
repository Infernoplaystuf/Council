"""
task_memory.py — RAM-resident per-session "sticky note" that survives
context-window truncation.

The problem
-----------
Small models (4K / 8K context) silently lose the original question
once enough vault matches, file injections, and analyst results
stack up in the prompt. By the time the Writer synthesizes, the
user's original ask has been pushed past the window's tail and the
model freelances from whatever fragments remain — typically by
inventing numbers from training-data memory.

The fix
-------
A short ``[TASK MEMO]`` block re-injected at the TOP of every prompt
this session. The memo holds three structured fields:

    goal:        one short sentence — what success looks like
    constraints: bullet list — "exclude zeros", "only data_in folder"
    forbidden:   bullet list — "do not invent column names"

Total cost ~60-100 tokens. Slotted at priority 1 (right after
``[NO DATA AVAILABLE]``), so it is never dropped or trimmed by the
budget pipeline. Even at a 4K context with heavy file injection,
this stays visible to the model.

Lifecycle
---------
- Per-session ``TaskMemory`` instance lives on ``CouncilApp.task_memory``.
- The condenser runs on EVERY user turn. If the new message looks
  like a follow-up to the previous memo (high lexical overlap with
  the previous goal), constraints are merged; otherwise the memo is
  replaced. Avoids the "user pivots topic, model still answers the
  old goal" trap.
- The condenser uses ``council_engine.local_chat`` at temperature 0,
  ``num_predict=200``. Roughly 1-2 seconds added to the first turn,
  and 1-2 seconds per subsequent turn for re-condensing. Fast model
  output is robust enough — we parse a simple ``KEY: value`` format,
  not JSON.
- On any parse or model failure, falls back to a heuristic-only memo
  built from canned reminder tags + the raw query as the goal. Never
  raises; the rest of the pipeline keeps working.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple


# ============================================================
# Canned reminder tags — heuristic intent classifier
# ============================================================
# These are derived from KEYWORDS in the user's question without
# calling the model. They get folded into the memo's `constraints`
# and `forbidden` fields so even when the condenser model fails the
# memo still has useful guard-rails.

_INTENT_TAGS: list = [
    # Computational queries
    (re.compile(r"\b(average|mean|median|sum|count|total|max|min|"
                r"standard\s+deviation|std|variance|percent|percentage|"
                r"ratio|proportion)\b", re.I),
     {"constraints": ["Quote numbers only from [ANALYST RESULT] or "
                      "[FILE: ...] blocks; never paraphrase."],
      "forbidden":   ["Do not invent column names, row counts, or "
                      "specific values from memory."]}),

    # Zero/null exclusion
    (re.compile(r"\b(exclud\w+|ignor\w+|omit\w+|without|except)\s+"
                r"(?:rows?\s+(?:with|containing|that\s+(?:have|contain)))?"
                r"\s*(zero(?:s|es)?|nulls?|nans?|missing|blanks?|0s?\b)",
                re.I),
     {"constraints": ["Filter exclusion happens BEFORE aggregation."]}),

    # File-listing queries (caught by the safety net too, but the
    # memo carries the constraint forward to follow-ups)
    (re.compile(r"\b(?:list|enumerate|dump|show\s+(?:all\s+)?files?)\b", re.I),
     {"constraints": ["Copy folder contents verbatim — do not "
                      "paraphrase, add, or remove files."]}),

    # Specific-file references
    (re.compile(r"\b[\w\-]+\.(?:csv|tsv|xlsx?|xlsm|parquet|json|"
                r"sqlite3?|db|duckdb|bson)\b", re.I),
     {"constraints": ["Use ONLY the file the user named. Do not "
                      "pull values from other files."]}),

    # Comparison / multi-file
    (re.compile(r"\b(compare|versus|vs\.?|difference\s+between|"
                r"side[\s-]by[\s-]side)\b", re.I),
     {"constraints": ["Show results for each compared item separately; "
                      "do not blend numbers across them."]}),

    # Filter-shaped queries
    (re.compile(r"\b(rows?\s+(?:where|with|that|whose)|filter|matching|"
                r"containing)\b", re.I),
     {"constraints": ["Apply the filter exactly as stated; "
                      "don't broaden or narrow it."]}),

    # Search-shape queries ("find builds with Iron Fist", "which builds
    # contain promethium", "show entries that use Heat Vision", "search
    # for files containing uridium") — common phrasing for content
    # search across JSON/CSV records. The filter-shape pattern above
    # misses these because it requires the word "rows" or "filter".
    # This pattern catches verb-of-search + noun + connector shapes.
    (re.compile(
        r"\b(?:find|which|show|list|search\s+for)\s+"
        r"(?:[\w\-]+\s+)?"             # optional adjective like "all"
        r"(?:builds?|entries|records|items|files?|configs?|"
        r"loadouts?|recipes?|results?|matches?)\b"
        # Connector word that introduces the search target. Either a
        # preposition immediately after the noun (with / containing /
        # using / matching / mentioning) OR a verb that means "has /
        # contains / uses" — covers "which builds CONTAIN promethium"
        # and "show entries THAT USE Heat Vision" alike.
        r"\s+(?:"
        r"with|contains?|containing|using|uses|"
        r"includes?|including|references?|referencing|"
        r"matching|matches|mentioning|mentions|having|has|have|"
        r"that\s+(?:use[s]?|have|has|contain[s]?|include[s]?|"
        r"reference[s]?|mention[s]?)"
        r")\b",
        re.I),
     {"constraints": [
         "Return only items where the named attribute actually appears "
         "in the file — don't include items that merely match the "
         "query word in an unrelated field.",
         "Quote item identifiers (names, IDs) verbatim from the "
         "source — don't paraphrase or guess.",
     ]}),
]


def derive_reminder_tags(user_text: str) -> dict:
    """Run the heuristic intent classifier on `user_text`.

    Returns a dict with ``constraints`` and ``forbidden`` lists.
    Multiple tags can fire for one question. Order is preserved
    so the prompt reads naturally to the model.
    """
    constraints: list = []
    forbidden: list   = []
    for pat, payload in _INTENT_TAGS:
        if pat.search(user_text or ""):
            for c in payload.get("constraints", []):
                if c not in constraints:
                    constraints.append(c)
            for f in payload.get("forbidden", []):
                if f not in forbidden:
                    forbidden.append(f)
    return {"constraints": constraints, "forbidden": forbidden}


# ============================================================
# TaskMemo dataclass
# ============================================================

@dataclass
class TaskMemo:
    """One structured memo. Created from the user's first task and
    re-condensed on each follow-up."""
    goal:        str             = ""
    constraints: List[str]       = field(default_factory=list)
    forbidden:   List[str]       = field(default_factory=list)
    raw_query:   str             = ""          # the user text the memo was built from
    created_ts:  float           = field(default_factory=time.time)
    updated_ts:  float           = field(default_factory=time.time)
    is_extension: bool           = False        # True when condenser declared "extension"

    def is_empty(self) -> bool:
        return not (self.goal or self.constraints or self.forbidden)

    def render_for_injection(self) -> str:
        """Format as the ``[TASK MEMO]`` block the model sees.

        Kept terse — every line costs tokens. Bullets use ``-``
        because most local models tokenise it as a single token.
        """
        if self.is_empty():
            return ""
        lines: list = ["[TASK MEMO — the user's ORIGINAL request and constraints]"]
        if self.goal:
            lines.append(f"goal: {self.goal}")
        if self.constraints:
            lines.append("constraints:")
            for c in self.constraints:
                lines.append(f"  - {c}")
        if self.forbidden:
            lines.append("forbidden:")
            for f in self.forbidden:
                lines.append(f"  - {f}")
        lines.append("[Stay aligned with this memo even if later context "
                     "blocks push the user message past your window.]")
        lines.append("[END TASK MEMO]")
        return "\n".join(lines)

    def render_for_transcript(self) -> str:
        """Short single-paragraph form for the user-visible transcript
        line. Lets the user spot a misread intent before the model
        answers."""
        bits: list = []
        if self.goal:
            bits.append(f"goal: {self.goal}")
        if self.constraints:
            bits.append("constraints: " + "; ".join(self.constraints[:3]))
        if self.forbidden:
            bits.append("forbidden: " + "; ".join(self.forbidden[:2]))
        return " · ".join(bits) if bits else "(no memo)"


# ============================================================
# Condenser
# ============================================================

_CONDENSE_PROMPT = """\
You are a TASK-MEMORY CONDENSER. Your only job is to read the user's new
message and output a tiny structured memo (~80 tokens) that the writer
model will keep in front of it as a sticky note across the rest of the
session. The memo prevents the writer from forgetting the goal when the
context window fills up with file dumps and search results.

PREVIOUS MEMO (may be empty — empty means this is a fresh session):
{previous}

NEW USER MESSAGE:
{new_query}

DECIDE FIRST:
- If the new message refines or builds on the previous memo's goal,
  the kind is "extension". Carry over the previous constraints and add
  or change only what the new message says.
- If the new message is on a clearly different topic, the kind is "new"
  and you start fresh.

OUTPUT — EXACTLY this format, no markdown fences, no commentary:

KIND: extension
GOAL: <one short sentence — what success looks like, present tense>
CONSTRAINTS:
- <bullet 1>
- <bullet 2>
FORBIDDEN:
- <bullet 1>

Rules:
- Use "extension" or "new" for KIND. Nothing else.
- GOAL is at most 20 words. State the action and the data.
- CONSTRAINTS are concrete filters / scopes the user named
  (e.g. "exclude rows where rating = 0", "only sales.csv", "data_in
  folder only"). Keep each bullet under 15 words.
- FORBIDDEN is what the writer must REFUSE to do (e.g. "do not invent
  column names", "do not pull values from training data"). Up to 2.
- If a section has nothing to say, write the header and a single "- (none)"
  bullet.
"""


def _normalise(text: str) -> str:
    """Lowercase + strip + collapse whitespace. Used for similarity."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _lexical_overlap(a: str, b: str) -> float:
    """Cheap symmetric Jaccard on whitespace tokens. 0.0 = unrelated,
    1.0 = identical. Used to detect topic shift when the condenser
    model isn't available or its KIND output is unparseable."""
    sa = set(_normalise(a).split())
    sb = set(_normalise(b).split())
    if not sa or not sb:
        return 0.0
    # Drop tiny stop-word tokens that inflate the overlap
    stop = {"a", "an", "the", "of", "in", "on", "for", "to", "and",
            "or", "is", "it", "what", "how", "with", "by", "at"}
    sa = {t for t in sa if t not in stop and len(t) > 1}
    sb = {t for t in sb if t not in stop and len(t) > 1}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# Follow-up markers — when the user's message OPENS with one of these,
# treat the query as an extension of the previous task even if the
# lexical overlap is below threshold. Examples that should be caught:
#   "also exclude null customers"
#   "and now for sales2.csv"
#   "what about excluding the last row"
#   "actually, sum instead of average"
# Without this, the lexical-overlap fallback (Jaccard ≥ 0.30) misses
# rewordings like "csv" vs "sales.csv" or "exclude" vs "excluding"
# and the previous memo's constraints get silently dropped.
_FOLLOWUP_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"also|and|now|next|then|"
    r"actually|additionally|furthermore|moreover|"
    r"what\s+about|how\s+about|"
    r"instead|otherwise|alternatively|"
    r"more\s+specifically|on\s+top\s+of\s+that|"
    r"plus|with\s+the\s+same"
    r")\b",
    re.IGNORECASE,
)


def _is_likely_followup(user_text: str) -> bool:
    """True when the message opens with a follow-up marker."""
    return bool(_FOLLOWUP_MARKER_RE.match(user_text or ""))


_KIND_RE        = re.compile(r"(?im)^\s*KIND\s*:\s*(\w+)")
_GOAL_RE        = re.compile(r"(?im)^\s*GOAL\s*:\s*(.+?)\s*$")
_SECTION_RE     = re.compile(r"(?im)^\s*(CONSTRAINTS|FORBIDDEN)\s*:\s*$")
_BULLET_RE      = re.compile(r"^\s*[-•*]\s*(.+?)\s*$")


def _parse_condenser_output(text: str) -> Optional[dict]:
    """Parse the structured KIND/GOAL/CONSTRAINTS/FORBIDDEN block the
    condenser emits. Returns None on parse failure (caller falls back
    to the heuristic-only memo).
    """
    if not text or not text.strip():
        return None
    kind_m = _KIND_RE.search(text)
    goal_m = _GOAL_RE.search(text)
    if not kind_m or not goal_m:
        return None
    kind = kind_m.group(1).strip().lower()
    goal = goal_m.group(1).strip()
    if kind not in ("extension", "new"):
        kind = "new"
    if not goal or goal.lower().startswith("(none"):
        return None

    # Section-by-section bullet capture
    constraints: list = []
    forbidden:   list = []
    current_section: Optional[str] = None
    for raw_line in text.splitlines():
        sec_m = _SECTION_RE.match(raw_line)
        if sec_m:
            current_section = sec_m.group(1).upper()
            continue
        b = _BULLET_RE.match(raw_line)
        if b and current_section:
            item = b.group(1).strip()
            if not item or item.lower().startswith("(none"):
                continue
            if current_section == "CONSTRAINTS":
                constraints.append(item)
            elif current_section == "FORBIDDEN":
                forbidden.append(item)

    return {
        "kind":        kind,
        "goal":        goal,
        "constraints": constraints,
        "forbidden":   forbidden,
    }


def condense_query(
    user_text: str,
    previous_memo: Optional[TaskMemo],
    llm_call: Optional[Callable[[str], str]],
) -> TaskMemo:
    """Build a fresh TaskMemo from `user_text`.

    1. Heuristic intent classifier ALWAYS runs — its output is a
       fallback memo so we have something even if the LLM fails.
    2. If `llm_call` is provided, run the condenser prompt and merge
       its output with the heuristic. The condenser is the smarter
       layer (it understands continuation) but the heuristic is the
       safety net (it always produces something useful).
    3. Topic-shift handling — when condenser declares "new" OR when
       lexical overlap with the previous memo's goal is below 0.15,
       constraints from the previous memo are discarded. When it's
       an extension, previous constraints are inherited and de-duped.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return TaskMemo()

    # Heuristic-only baseline — always produced.
    heuristic = derive_reminder_tags(user_text)
    memo = TaskMemo(
        goal        = user_text[:200],          # raw query as fallback goal
        constraints = list(heuristic["constraints"]),
        forbidden   = list(heuristic["forbidden"]),
        raw_query   = user_text,
    )

    # Try the LLM condenser if available
    parsed: Optional[dict] = None
    if llm_call is not None:
        try:
            prev_text = "(empty)"
            if previous_memo is not None and not previous_memo.is_empty():
                prev_text = previous_memo.render_for_transcript()
            prompt = _CONDENSE_PROMPT.format(
                previous=prev_text,
                new_query=user_text[:1200],   # cap the condenser input
            )
            raw = llm_call(prompt)
            parsed = _parse_condenser_output(raw)
        except Exception:
            parsed = None

    # Topic-shift signal: prefer the strongest source first.
    #   1. Follow-up markers in the user's text ("also", "and now",
    #      "what about", …) — these are unambiguous "this builds on
    #      the previous question" signals and override everything.
    #   2. KIND field from the LLM condenser, if it ran.
    #   3. Jaccard similarity with the previous query, when neither
    #      of the above is available.
    is_extension = False
    if previous_memo is not None and not previous_memo.is_empty():
        if _is_likely_followup(user_text):
            is_extension = True
        elif parsed is not None and parsed["kind"] == "extension":
            is_extension = True
        elif parsed is None:
            # Fall back to lexical similarity when the LLM didn't speak
            overlap = _lexical_overlap(user_text, previous_memo.raw_query)
            is_extension = overlap >= 0.30

    if parsed is not None:
        memo.goal        = parsed["goal"][:200] or memo.goal
        # Union the parsed bullets with the heuristic bullets
        for c in parsed["constraints"]:
            if c not in memo.constraints:
                memo.constraints.append(c)
        for f in parsed["forbidden"]:
            if f not in memo.forbidden:
                memo.forbidden.append(f)

    # Inherit constraints from the previous memo if this is an extension
    if is_extension and previous_memo is not None:
        for c in previous_memo.constraints:
            if c not in memo.constraints:
                memo.constraints.append(c)
        for f in previous_memo.forbidden:
            if f not in memo.forbidden:
                memo.forbidden.append(f)
        memo.is_extension = True

    # Cap each list so a runaway condenser can't blow the token budget
    memo.constraints = memo.constraints[:8]
    memo.forbidden   = memo.forbidden[:4]

    return memo


# ============================================================
# TaskMemory — per-session container with the public API
# ============================================================

class TaskMemory:
    """Per-session memo store. One instance per CouncilApp.

    Public API used by council_gui_engine:

      update(user_text, llm_call)   -> TaskMemo   re-condense from user text
      current()                     -> TaskMemo   current memo (may be empty)
      reset()                       -> None       drop the memo entirely
      render_injection_block()      -> str        for the prompt pipeline
      render_transcript_line()      -> str        for the visible observation
    """

    def __init__(self):
        self._memo: TaskMemo = TaskMemo()

    def current(self) -> TaskMemo:
        return self._memo

    def reset(self) -> None:
        self._memo = TaskMemo()

    def update(self,
               user_text: str,
               llm_call: Optional[Callable[[str], str]] = None,
               ) -> TaskMemo:
        """Re-condense from `user_text`. Caller passes a thin wrapper
        around council_engine.local_chat so we don't import it at the
        module level (keeps task_memory dependency-free for testing)."""
        new_memo = condense_query(user_text, self._memo, llm_call)
        # If condenser failed badly enough to produce an empty memo,
        # leave the previous one in place so we don't regress.
        if not new_memo.is_empty():
            self._memo = new_memo
        return self._memo

    def render_injection_block(self) -> str:
        return self._memo.render_for_injection()

    def render_transcript_line(self) -> str:
        if self._memo.is_empty():
            return "(no task memo)"
        prefix = "Tracking" + (" (extending previous)" if self._memo.is_extension else "")
        return f"{prefix} → {self._memo.render_for_transcript()}"
