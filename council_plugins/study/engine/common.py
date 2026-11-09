"""
What the review and the study documents share: reading a model's JSON
reply, numbering the sources a prompt shows, and the check that a piece of
text is supported by the passage it cites.

THE SUPPORT CHECK IS NOT A MODEL
A sentence (or a card's answer) counts as supported when most of its
content words are in the passages it cites: words of three letters or more,
stop words dropped, a plural 's' ignored, numbers kept whole. It is cheap,
it is the same on every PC, and it catches what a small model does wrong
most often: an answer it remembered rather than read, or a citation number
pointing at the wrong passage. It cannot tell a sentence that reverses the
source's meaning from one that repeats it, so the review keeps every
citation clickable for the reader to check.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

#: Share of a sentence's content words that must be in its cited passages.
SUPPORT_SHARE = 0.5
#: Characters of one passage shown to a model.
PASSAGE_CHARS = 1500

_WORD = re.compile(r"[^\W_]+(?:[.'’][^\W_]+)*", re.UNICODE)
_STOP = frozenset("""about above after again against all also although among and
another any are around because been before being below between both but can
cannot could did does doing done down during each either else even ever every
few for from further had has have having her here hers him his how however into
its itself just least less like made make makes many may might more most much
must near neither nor not now off often once one only onto other others otherwise
our ours out over own per rather same several she should since some such than
that the their theirs them themselves then there therefore these they this those
though through thus too toward under until upon very via was were what whatever
when where whether which while who whom whose why will with within without would
yet you your yours also being called known used uses using based include includes
including""".split())


def content_words(text: str) -> Set[str]:
    """The words of ``text`` a check compares: lower case, three letters or
    more (any number), stop words dropped, a final plural 's' dropped."""
    out = set()
    for m in _WORD.finditer(text or ""):
        w = m.group(0).lower().replace("’", "'")
        if w.isdigit() or any(ch.isdigit() for ch in w):
            out.add(w)
            continue
        if len(w) < 3 or w in _STOP:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def support(text: str, passages: Iterable[str]) -> float:
    """The share of ``text``'s content words found in ``passages`` (1.0 for
    a text with none)."""
    words = content_words(text)
    if not words:
        return 1.0
    have: Set[str] = set()
    for p in passages:
        have |= content_words(p)
    return len(words & have) / len(words)


def supported(text: str, passages: Iterable[str],
              share: float = SUPPORT_SHARE) -> bool:
    return support(text, passages) >= share


def parse_json(reply: str) -> Any:
    """The JSON a model replied with: the whole reply, or the first {...} or
    [...] in it (a model may wrap it in prose or a code fence). None when
    there is none."""
    text = (reply or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    for open_, close in (("{", "}"), ("[", "]")):
        start = text.find(open_)
        end = text.rfind(close)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    return None


def clip(text: str, limit: int = PASSAGE_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def sources_block(numbered: Dict[int, Any]) -> str:
    """'[3] notes/x.md, L1-L30\\n<text>' for each numbered passage."""
    return "\n\n".join(f"[{n}] {p.cite()}\n{clip(p.text)}"
                       for n, p in sorted(numbered.items()))


def as_text(value: Any, limit: int = 2000) -> str:
    return " ".join(str(value or "").split())[:limit]


def as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip().strip("[]"))
    except (TypeError, ValueError):
        return None


class Stopped(Exception):
    """The user pressed Stop."""


def check_stop(should_stop) -> None:
    if should_stop is not None and should_stop():
        raise Stopped()


def unique_by(items: Sequence[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for it in items:
        k = " ".join(str(it.get(key, "")).lower().split())
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out
