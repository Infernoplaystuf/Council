"""Model-proposed links from free text — what labels and Collections cannot give.

The laptop's measurements (docs/morphik_assessment.md §5) set the design: local
models FIND entities well but get relationships wrong — right endpoints with
the wrong label or direction, invented links, and no model found a part→project
link. So the model is asked for as little as possible and checked hard:

  * it is GIVEN the known people, parts and projects (from the graph) and asked
    only for links among them, quoting the sentence that says so;
  * every proposed link is checked by the Council, not trusted:
      - both ends must resolve to a known entity (by name or alias, accents
        and 'Last, First' handled by knowledge_graph.fold / person_key);
      - the predicate must fit the types (a PART cannot LEAD); a link whose
        types are simply reversed (PART USES_PART PROJECT) is turned round;
      - SUPERSEDES between two revisions of one code is put the right way by
        the revision letters (PN-1234/B supersedes PN-1234/A), not the model;
      - the quote must really be in the text (whitespace/case-insensitive) —
        the Council finds the line from it; the model's line number is
        ignored;
  * whatever survives is only ever SUGGESTED until the user accepts it.

``num_predict`` defaults to 1,400: the laptop measured an 872-token reply that
the engine's default of 600 would have cut off. The schema caps facts per
chunk. Toolkit-free; model calls go through an injected ``chat`` (the engine's
local_chat by default — local models only).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from council_core import knowledge_graph as kgm

#: predicate -> (allowed subject types, allowed object types)
SIGNATURES: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "LEADS": (("PERSON",), ("PROJECT",)),
    "CONTACT_FOR": (("PERSON",), ("PROJECT", "PART")),
    "WORKS_ON": (("PERSON",), ("PROJECT",)),
    "OWNS": (("PERSON",), ("PART",)),
    "USES_PART": (("PROJECT",), ("PART",)),
    "SUPERSEDES": (("PART",), ("PART",)),
}
MEANINGS = {
    "LEADS": "PERSON leads / is in charge of / owns PROJECT",
    "CONTACT_FOR": "PERSON is the point of contact for PROJECT or PART",
    "WORKS_ON": "PERSON works on / attends reviews of / approves for PROJECT",
    "OWNS": "PERSON is responsible for / owns / sources PART",
    "USES_PART": "PROJECT uses / will be fitted with / is tested with PART",
    "SUPERSEDES": "newer PART supersedes / replaces older PART",
}
#: The model the Connections tab offers first, from the KG0 benchmark
#: (council_core/kg_bench.py, 2026-10-06, RTX 5080; table in docs/handoff):
#: phi4:14b, gpt-oss:20b and gemma3:12b tie at F1 ~0.89, but phi4 moved
#: between identical runs (part->project recall 0.70 then 0.50) while gemma3:12b
#: repeated exactly and finds part->project links best (0.80) — the links the
#: user asked for — at 1.0 s per passage and 8.1 GB.
DEFAULT_EXTRACTOR = "gemma3:12b"
MAX_FACTS = 12
NUM_PREDICT = 1400

SCHEMA = {
    "type": "object",
    "properties": {"links": {"type": "array", "maxItems": MAX_FACTS, "items": {
        "type": "object",
        # Bounded, so the engine can prove the longest valid reply fits
        # num_predict (12 x ~110 tokens) instead of risking a cut-off.
        "properties": {"subject": {"type": "string", "maxLength": 80},
                       "predicate": {"type": "string", "enum": list(SIGNATURES)},
                       "object": {"type": "string", "maxLength": 80},
                       "quote": {"type": "string", "maxLength": 300}},
        "required": ["subject", "predicate", "object", "quote"]}}},
    "required": ["links"],
}


@dataclass
class Known:
    """A known entity as the extractor sees it."""
    key: str                  # stable id (graph entity id, or a test key)
    type: str
    name: str
    aliases: List[str] = field(default_factory=list)


@dataclass
class Link:
    subject: str              # Known.key
    predicate: str
    object: str
    quote: str
    line: Optional[int] = None
    fixed: str = ""           # what the Council corrected, if anything


@dataclass
class Rejected:
    raw: Dict[str, str]
    why: str


@dataclass
class Result:
    links: List[Link] = field(default_factory=list)
    rejected: List[Rejected] = field(default_factory=list)
    raw: str = ""
    error: str = ""


# ── prompt ────────────────────────────────────────────────────────────────
def build_prompt(text: str, known: Sequence[Known]) -> List[Dict[str, str]]:
    ents = "\n".join(f"- {k.type}: {k.name}"
                     + (f" (also: {', '.join(a for a in k.aliases if a != k.name)})"
                        if [a for a in k.aliases if a != k.name] else "")
                     for k in known)
    preds = "\n".join(f"- {p}: {m}" for p, m in MEANINGS.items())
    system = ("You extract relationships between KNOWN people, parts and projects "
              "from a document. Only state what the text says. Do not guess, do not "
              "use outside knowledge, and skip anything the text negates or only "
              "considers.")
    user = (f"Known entities:\n{ents}\n\nRelationship types (subject first):\n{preds}\n\n"
            f"Text:\n\"\"\"\n{text}\n\"\"\"\n\n"
            "List every relationship the text states between the known entities. "
            "Use the entity names exactly as listed. For each, copy the exact sentence "
            "(or clause) from the text that states it as \"quote\". Answer only with "
            'JSON: {"links": [{"subject": ..., "predicate": ..., "object": ..., '
            '"quote": ...}]}. If there are none, answer {"links": []}.')
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ── checking ──────────────────────────────────────────────────────────────
def _index(known: Sequence[Known]) -> Dict[str, List[Known]]:
    idx: Dict[str, List[Known]] = {}
    for k in known:
        names = [k.name] + list(k.aliases)
        if k.type == "PERSON":
            # An initial ('D. Whitfield') is never an alias here: it may fit
            # two people, and only the user can settle it (the review queue).
            names = [n for n in names if not kgm.is_initial_form(kgm.person_key(n))]
        forms = {kgm.fold(n) for n in names}
        if k.type == "PERSON":
            forms |= {kgm.person_key(n) for n in names}
        for f in forms:
            if f:
                idx.setdefault(f, [])
                if k not in idx[f]:
                    idx[f].append(k)
    idx["\0people"] = [k for k in known if k.type == "PERSON"]
    return idx


def resolve(name: str, idx: Dict[str, List[Known]]) -> Optional[Known]:
    """A known entity for ``name``, only when exactly one fits. An initial
    form resolves only if exactly one known person has that initial and
    surname ('D. Whitfield' with both Dana and Dan known -> None)."""
    pk = kgm.person_key(name)
    if kgm.is_initial_form(pk):
        ini, surname = pk.split()
        fits = [k for k in idx.get("\0people", [])
                if (lambda t: len(t) >= 2 and t[-1] == surname and t[0].startswith(ini)
                    and len(t[0]) > 1)(kgm.person_key(k.name).split())]
        return fits[0] if len(fits) == 1 else None
    for form in (kgm.fold(name), pk):
        hits = idx.get(form) or []
        if len(hits) == 1:
            return hits[0]
    return None


_REV_RE = re.compile(r"^(.*?)[/\s-]?(?:REV\.?\s*)?([A-Z])$", re.I)


def _revision(code: str) -> Optional[Tuple[str, str]]:
    m = _REV_RE.match(code.strip())
    return (m.group(1).upper(), m.group(2).upper()) if m else None


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def find_quote_span(text: str, quote: str) -> Optional[Tuple[int, int]]:
    """(first line, last line), 1-based, of ``quote`` in ``text``
    (whitespace/case-insensitive, may span lines), or None when the text
    does not contain it. The LAST line matters as much as the first: a
    quote hard-wrapped over four or more lines (PDF page text has short
    lines) names its people at its end."""
    q = _norm_ws(quote).strip(" .\"'")
    if len(q) < 8:
        return None
    flat, starts, pos = [], [], 0
    for ln in text.splitlines():
        starts.append(pos)
        piece = _norm_ws(ln)
        flat.append(piece)
        pos += len(piece) + 1
    joined = " ".join(flat)
    at = joined.find(q)
    if at < 0:
        return None
    end = at + len(q) - 1
    first = last = 0
    for i, s in enumerate(starts):
        if s <= at:
            first = i
        if s <= end:
            last = i
    return first + 1, last + 1


def find_quote(text: str, quote: str) -> Optional[int]:
    """1-based line where ``quote`` starts, or None (see find_quote_span)."""
    span = find_quote_span(text, quote)
    return span[0] if span else None


_FIRST_WORD_SKIP = {"project", "program", "programme", "the", "new", "phase", "test",
                    "line", "pump", "turbine", "upgrade", "retrofit"}


def named_in(text: str, k: Known) -> bool:
    """Does ``text`` itself name ``k`` — by its name, an alias, or (for a
    project) the distinctive first word of its name? Initials do not count.
    A model that writes "Dana Whitfield" for a passage that only says
    "D. Whitfield" has supplied the name itself (measured: phi4:14b and
    gemma3:12b both did, KG0 2026-10-06), and that link is refused."""
    flat = " " + kgm.fold(text) + " "
    forms = [k.name] + list(k.aliases)
    if k.type == "PERSON":
        forms = [f for f in forms if not kgm.is_initial_form(kgm.person_key(f))]
        # 'Raman, Priya' names Priya Raman: try the surname-first order too.
        # person_key also drops suffixes: "Bob Smith" names "Bob Smith, Jr.".
        keys = [kgm.person_key(f).split() for f in forms]
        forms += [" ".join(t) for t in keys if t]
        forms += [" ".join([t[-1]] + t[:-1]) for t in keys if len(t) >= 2]
    if k.type == "PROJECT":
        for f in list(forms):
            first = f.split()[0] if " " in f else ""
            if len(first) >= 5 and first[:1].isupper() and first.lower() not in _FIRST_WORD_SKIP:
                forms.append(first)
    return any(f" {kgm.fold(f)} " in flat for f in forms if kgm.fold(f))


def check(raw_links: Iterable[Dict[str, str]], text: str, known: Sequence[Known]
          ) -> Tuple[List[Link], List[Rejected]]:
    idx = _index(known)
    out: List[Link] = []
    rejected: List[Rejected] = []
    seen = set()
    for r in raw_links:
        if not isinstance(r, dict):
            continue
        pred = str(r.get("predicate", "")).upper().strip()
        s, o = resolve(str(r.get("subject", "")), idx), resolve(str(r.get("object", "")), idx)
        if pred not in SIGNATURES:
            rejected.append(Rejected(r, f"unknown relationship {pred!r}"))
            continue
        if s is None or o is None:
            rejected.append(Rejected(r, "names an entity that is not known (or is ambiguous)"))
            continue
        if s.key == o.key:
            rejected.append(Rejected(r, "links an entity to itself"))
            continue
        fixed = ""
        st, ot = SIGNATURES[pred]
        if not (s.type in st and o.type in ot):
            if o.type in st and s.type in ot:
                s, o, fixed = o, s, "turned round (the types were reversed)"
            else:
                rejected.append(Rejected(r, f"{pred} cannot link a {s.type} to a {o.type}"))
                continue
        if pred == "SUPERSEDES":
            rs, ro = _revision(s.name), _revision(o.name)
            if rs and ro and rs[0] == ro[0] and rs[1] != ro[1]:
                if rs[1] < ro[1]:
                    s, o, fixed = o, s, "turned round (the later revision supersedes)"
        span = find_quote_span(text, str(r.get("quote", "")))
        if span is None:
            rejected.append(Rejected(r, "its quote is not in the text"))
            continue
        line, last = span
        # Named NEAR the quote — the line before it to two after its LAST
        # line (a pronoun in the next sentence is fine) — not merely
        # somewhere in the passage: measured on the Ironbridge vault,
        # phi4:14b credited "D. Whitfield will review…" (line 13) to Dana
        # because "Whitfield, Dana" is on line 4 of the same chunk. The
        # window used to end two lines after the quote's FIRST line, so a
        # 195-character quote wrapped over lines 3-6 lost the name at its
        # own end (review probe, 2026-10-07).
        # A PROJECT may be named anywhere in the passage: notes are often
        # about the project in their title ("Atlas sync") and never repeat it.
        lines = text.splitlines()
        near = "\n".join(lines[max(0, line - 2):last + 2])
        unnamed = [e.name for e in (s, o)
                   if not named_in(text if e.type == "PROJECT" else near, e)]
        if unnamed:
            rejected.append(Rejected(r, f"the text never names {unnamed[0]} near that quote"))
            continue
        k = (s.key, pred, o.key)
        if k in seen:
            continue
        seen.add(k)
        out.append(Link(s.key, pred, o.key, str(r.get("quote", "")).strip(), line, fixed))
    return out, rejected


def parse(reply: str) -> List[Dict[str, str]]:
    m = re.search(r"\{.*\}", reply or "", re.S)
    if not m:
        raise ValueError("the model's answer has no JSON")
    data = json.loads(m.group(0))
    links = data.get("links") if isinstance(data, dict) else None
    if not isinstance(links, list):
        raise ValueError("the model's answer has no 'links' list")
    return links


ChatFn = Callable[..., str]


def ollama_chat(model: str, host: Optional[str] = None) -> ChatFn:
    """chat(messages) through the engine (localhost Ollama, or a Council node
    when ``host`` names one), JSON-schema constrained, temperature 0."""
    def chat(messages, *, num_predict: int = NUM_PREDICT) -> str:
        import council_engine as ce
        return ce.local_chat(list(messages), temperature=0.0, num_predict=num_predict,
                             model=f"ollama:{model}", host=host, json_schema=SCHEMA,
                             seed=7, timeout=300)
    return chat


def extract(text: str, known: Sequence[Known], chat: ChatFn) -> Result:
    res = Result()
    try:
        res.raw = chat(build_prompt(text, known))
        raw_links = parse(res.raw)
    except Exception as exc:                               # noqa: BLE001
        res.error = f"{exc.__class__.__name__}: {exc}"[:300]
        return res
    res.links, res.rejected = check(raw_links, text, known)
    return res
