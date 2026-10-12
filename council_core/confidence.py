"""
council_core.confidence — confidence as a percentage, 0–100.

Members used to rate themselves 1–10 in a separate call, and the Judge's
ranking carried a 0–10 confidence. Ten steps round hard: 7 and 8 are the
difference between "answer may be weak" and "accept despite NEEDS_WORK".
Everything is now a percentage.

A member gives its confidence INSIDE its draft, as a last line

    CONFIDENCE: 85% — unsure whether the 2019 log uses the same units

which this module finds, removes from the answer, and returns with the
reason. The old separate call (a 5-token reply to a stateless model shown the
first 400 characters of "its" answer) is now only the fallback when the line
is missing.

Old records carry 1–10 numbers; `from_legacy` turns them into percentages.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

#: A member at or below this is flagged ("answer may be weak") and its
#: topic goes to the Librarian's wishlist.
LOW = 40
#: The Judge at or above this accepts an answer on the last round even with
#: NEEDS_WORK; members all at or above it may skip the cross-fire.
HIGH = 80
#: The Judge at or below this on round one adds a round.
VERY_LOW = 20
#: When nothing could be read.
DEFAULT = 50

DRAFT_INSTRUCTION = (
    "End your answer with ONE final line, exactly in this form:\n"
    "CONFIDENCE: <0-100>% — <what you are least sure of, in a few words>\n"
    "100% = certain and checked; 50% = an informed guess; below 30% = "
    "mostly guessing. Use any whole number, not just multiples of ten.")

_LINE = re.compile(
    r"^[\s>*_#`-]*confidence\s*[:=]\s*\**\s*(\d{1,3}(?:\.\d+)?)\s*"
    r"(%|/\s*100|/\s*10\b|percent)?\**\s*(?:[-—–:,;(]\s*(.*?))?\)?\s*$",
    re.IGNORECASE | re.MULTILINE)


def clamp(value: Any) -> int:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT
    return int(round(max(0.0, min(100.0, v))))


def from_legacy(value: Any) -> int:
    """A 0–10 (or 1–10) rating as a percentage."""
    try:
        return clamp(float(value) * 10)
    except (TypeError, ValueError):
        return DEFAULT


def parse_reply(raw: str) -> Optional[int]:
    """A percentage from a short reply: "85", "85%", "0.85", "8/10"."""
    text = (raw or "").strip()
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(%|/\s*100|/\s*10\b)?", text)
    if not m:
        return None
    v = float(m.group(1))
    unit = (m.group(2) or "").replace(" ", "")
    if unit == "/10":
        return from_legacy(v)
    if not unit and v <= 1.0 and "." in m.group(1):
        return clamp(v * 100)
    return clamp(v)


def split_draft(answer: str) -> Tuple[str, Optional[int], str]:
    """(answer without its CONFIDENCE line, percentage or None, reason).

    The LAST such line counts; it is removed wherever it is, so a model that
    puts it first still gets a clean answer."""
    matches = list(_LINE.finditer(answer or ""))
    if not matches:
        return answer, None, ""
    m = matches[-1]
    v = float(m.group(1))
    unit = (m.group(2) or "").replace(" ", "").lower()
    pct = from_legacy(v) if unit == "/10" else clamp(v)
    reason = (m.group(3) or "").strip().strip("*").strip()
    cleaned = (answer[:m.start()] + answer[m.end():]).rstrip()
    return cleaned, pct, reason


def label(pct: Optional[int]) -> str:
    return "?" if pct is None else f"{int(pct)}%"


def normalise_ranking(obj: dict) -> dict:
    """A Judge ranking with `confidence` and `scores` as percentages.

    The prompt asks for 0–100. A model that answered on the old 0–10 scale
    gives itself away: every number in the ranking is 10 or less, and at
    least one score is above 1 (so it is not 0–1 fractions either)."""
    if not isinstance(obj, dict):
        return obj
    scores = obj.get("scores") if isinstance(obj.get("scores"), dict) else {}
    nums = []
    for v in list(scores.values()) + [obj.get("confidence")]:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            pass
    ten_scale = bool(nums) and max(nums) <= 10 and any(n > 1 for n in nums)
    unit_scale = bool(nums) and max(nums) <= 1 and any(0 < n < 1 for n in nums)
    conv = from_legacy if ten_scale else (
        (lambda v: clamp(float(v) * 100)) if unit_scale else clamp)
    out = dict(obj)
    if "confidence" in obj:
        out["confidence"] = conv(obj.get("confidence"))
    if scores:
        out["scores"] = {k: conv(v) for k, v in scores.items()}
    if ten_scale or unit_scale:
        out["scale_converted"] = "0-10" if ten_scale else "0-1"
    return out


__all__ = ["LOW", "HIGH", "VERY_LOW", "DEFAULT", "DRAFT_INSTRUCTION",
           "clamp", "from_legacy", "parse_reply", "split_draft", "label",
           "normalise_ranking"]
