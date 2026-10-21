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
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

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
    when ``host`` names one), JSON-schema constrained, temperature 0.
    ``schema``: the open extraction's (OPEN_SCHEMA) or None for free text
    (the graph's answers)."""
    def chat(messages, *, num_predict: int = NUM_PREDICT, schema: Any = SCHEMA) -> str:
        import council_engine as ce
        return ce.local_chat(list(messages), temperature=0.0, num_predict=num_predict,
                             model=f"ollama:{model}", host=host, json_schema=schema,
                             seed=7, timeout=300)
    return chat


def gguf_chat(path: str) -> ChatFn:
    """chat(messages) on a GGUF file the user picked: Council's main model
    when it is that file, else the file loaded in process
    (council_core.ledger_llm) - every LLM call in Council takes a GGUF
    (user, 2026-10-19); Suggest links offered Ollama models only."""
    def chat(messages, *, num_predict: int = NUM_PREDICT, schema: Any = SCHEMA) -> str:
        from council_core import ledger_llm
        if ledger_llm._is_main_gguf(path):
            import council_engine as ce
            return ce.local_chat(list(messages), temperature=0.0, num_predict=num_predict,
                                 json_schema=schema, seed=7, timeout=300)
        llm = ledger_llm._gguf_llm(path)
        kw = dict(messages=list(messages), temperature=0.0, max_tokens=num_predict, seed=7)
        with ledger_llm._GGUF_LOCK:
            try:
                out = (llm.create_chat_completion(
                    response_format={"type": "json_object", "schema": schema}, **kw)
                    if schema is not None else llm.create_chat_completion(**kw))
            except TypeError:                              # an older llama-cpp-python
                out = llm.create_chat_completion(**kw)
        ledger_llm._gpu_ok()          # it answered: the GPU-crash marker goes
        return out["choices"][0]["message"]["content"] or ""
    return chat


def is_gguf(model: str) -> bool:
    return str(model or "").lower().endswith(".gguf")


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


# ── open extraction: any entity, any relationship (user, 2026-10-21) ─────
# "I would like the connections tab to function very similar to Morphik":
# the model reads EVERY passage and names the entities in it, of any type,
# and the relationships between them - as Morphik's graph did - instead of
# only linking people, parts and projects a labelled field already named.
# What Morphik got wrong is not copied (docs/morphik_assessment.md §2): no
# text is cut off (a passage is sent whole), a merge never ignores the type
# of two core entities (a PERSON and a PART of one name stay two), names are
# merged by rules, not by one model call over every label, and every fact
# keeps its page/line and quote. The Council still checks what it is told:
# an entity must be named in the passage, a link's quote must be in it, and
# both ends must be named near the quote.

#: The types the rest of the Council knows by name (labelled fields, the
#: Ledger). A model's type is mapped onto these when it plainly means one.
CORE_TYPES = ("PERSON", "PART", "PROJECT")
#: The types the prompt suggests (Morphik's own were PERSON, ORGANIZATION,
#: LOCATION, CONCEPT); any other the model names is kept as it says.
SUGGESTED_TYPES = ("PERSON", "ORGANIZATION", "PROJECT", "PART", "LOCATION",
                   "EQUIPMENT", "MATERIAL", "PROCESS", "DOCUMENT", "EVENT", "CONCEPT")
_TYPE_WORDS = {
    "PERSON": ("person", "people", "individual", "employee", "engineer", "inspector",
               "technician", "manager", "author", "operator", "contact", "name",
               "human", "staff", "worker"),
    "ORGANIZATION": ("organization", "organisation", "org", "company", "supplier",
                     "vendor", "customer", "team", "department", "group", "firm",
                     "corporation", "agency", "institution", "manufacturer", "business"),
    "PROJECT": ("project", "program", "programme", "initiative"),
    "PART": ("part", "component", "assembly", "part number", "subassembly",
             "sub assembly", "spare", "item", "sku"),
    "LOCATION": ("location", "place", "site", "city", "country", "facility",
                 "plant", "building", "region", "address", "lab", "laboratory"),
    "EQUIPMENT": ("equipment", "machine", "tool", "instrument", "device", "rig",
                  "system", "hardware"),
    "MATERIAL": ("material", "alloy", "chemical", "substance"),
    "PROCESS": ("process", "procedure", "method", "technique", "test", "inspection"),
    # 'DOCUMENT' is the graph's own type for the files it read: a report a
    # passage names is a REPORT.
    "REPORT": ("document", "report", "drawing", "specification", "spec", "standard",
               "ecn", "ncr", "memo", "manual"),
    "EVENT": ("event", "meeting", "review", "milestone", "incident"),
}
_TYPE_INDEX = {w: t for t, ws in _TYPE_WORDS.items() for w in ws}


def canonical_type(raw: Any) -> str:
    """'Engineer' -> PERSON, 'Supplier' -> ORGANIZATION, 'Program' -> PROJECT;
    any other word is kept, in upper case with underscores ('Heat
    Treatment' -> HEAT_TREATMENT). Empty -> CONCEPT."""
    f = kgm.fold(raw)
    if not f:
        return "CONCEPT"
    if f.upper().replace(" ", "_") in _TYPE_WORDS or f.upper() in CORE_TYPES:
        t = f.upper().replace(" ", "_")
        return "REPORT" if t == "DOCUMENT" else t
    if f in _TYPE_INDEX:
        return _TYPE_INDEX[f]
    for sing in ((f[:-3] + "y") if f.endswith("ies") else "", f[:-1] if f.endswith("s")
                 else ""):
        if sing in _TYPE_INDEX:
            return _TYPE_INDEX[sing]          # 'companies', 'suppliers'
    last = f.split()[-1]
    if last in _TYPE_INDEX and len(f.split()) <= 3:
        return _TYPE_INDEX[last]            # 'design engineer', 'test site'
    t = re.sub(r"[^A-Z0-9]+", "_", f.upper()).strip("_")[:30]
    return "REPORT" if t == "DOCUMENT" else (t or "CONCEPT")


#: Plain spellings of the relationships the rest of the graph already uses.
_RELATION_WORDS = {
    "LEADS": ("leads", "lead", "lead of", "leads project", "manages", "heads",
              "is lead of", "project lead", "program lead", "in charge of"),
    "WORKS_ON": ("works on", "works for project", "assigned to", "member of project",
                 "contributes to", "participates in"),
    "OWNS": ("owns", "owner of", "responsible for"),
    "USES_PART": ("uses part", "uses", "uses component", "includes part",
                  "contains part", "fitted with", "requires part"),
    "SUPERSEDES": ("supersedes", "replaces", "superseded", "replaced"),
    "CONTACT_FOR": ("contact for", "point of contact for", "poc for",
                    "point of contact"),
}
_RELATION_INDEX = {w: p for p, ws in _RELATION_WORDS.items() for w in ws}
#: The same relationships said the other way round ('X is used on Y' is
#: 'Y uses part X'): the ends are swapped.
_INVERSE_WORDS = {
    "LEADS": ("led by", "is led by", "managed by", "headed by"),
    "OWNS": ("owned by", "is owned by"),
    "USES_PART": ("used on", "used in", "is used on", "is used in", "part of",
                  "fitted to", "fitted on", "installed on", "installed in",
                  "component of"),
    "SUPERSEDES": ("replaced by", "superseded by", "is replaced by",
                   "is superseded by"),
    "WORKS_ON": ("worked on by", "staffed by"),
}
_INVERSE_INDEX = {w: p for p, ws in _INVERSE_WORDS.items() for w in ws}


def relation_direction(raw: Any) -> Tuple[str, bool]:
    """(canonical relation, whether its ends are to be swapped):
    'used on' -> ('USES_PART', True)."""
    f = kgm.fold(raw)
    if f in _INVERSE_INDEX:
        return _INVERSE_INDEX[f], True
    return canonical_relation(raw), False


def canonical_relation(raw: Any) -> str:
    """'replaces' -> SUPERSEDES, 'works on' -> WORKS_ON; any other phrase is
    kept as UPPER_SNAKE ('supplies' -> SUPPLIES). Empty -> RELATED_TO."""
    f = kgm.fold(raw)
    if f in _RELATION_INDEX:
        return _RELATION_INDEX[f]
    t = re.sub(r"[^A-Z0-9]+", "_", f.upper()).strip("_")[:40]
    return t or "RELATED_TO"


OPEN_MAX_ENTITIES = 20
OPEN_MAX_LINKS = 15
#: 20 entities x ~45 tokens + 15 links x ~110 tokens, with room: an answer
#: cut off mid-JSON is lost whole (the closed mode measured an 872-token
#: reply that the engine's default 600 cut).
OPEN_NUM_PREDICT = 3000

OPEN_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "maxItems": OPEN_MAX_ENTITIES, "items": {
            "type": "object",
            "properties": {"name": {"type": "string", "maxLength": 80},
                           "type": {"type": "string", "maxLength": 30},
                           "description": {"type": "string", "maxLength": 160}},
            "required": ["name", "type"]}},
        "relationships": {"type": "array", "maxItems": OPEN_MAX_LINKS, "items": {
            "type": "object",
            "properties": {"source": {"type": "string", "maxLength": 80},
                           "relation": {"type": "string", "maxLength": 40},
                           "target": {"type": "string", "maxLength": 80},
                           "quote": {"type": "string", "maxLength": 300}},
            "required": ["source", "relation", "target", "quote"]}}},
    "required": ["entities", "relationships"],
}


@dataclass
class Found:
    """An entity the model named in a passage, checked."""
    name: str                 # as the passage writes it
    type: str                 # canonical_type
    description: str = ""
    known: Optional[Known] = None   # the graph's entity it resolved to, if any
    line: Optional[int] = None      # 1-based line of the passage naming it
    #: other names the passage gives it: 'Falcon Wing Retrofit (KA-101)'
    aliases: List[str] = field(default_factory=list)


@dataclass
class OpenLink:
    subject: Found
    predicate: str
    object: Found
    quote: str
    line: Optional[int] = None
    fixed: str = ""


@dataclass
class OpenResult:
    entities: List[Found] = field(default_factory=list)
    links: List[OpenLink] = field(default_factory=list)
    rejected: List[Rejected] = field(default_factory=list)
    raw: str = ""
    error: str = ""


def build_open_prompt(text: str, known: Sequence[Known], focus: str = ""
                      ) -> List[Dict[str, str]]:
    """Morphik's ask (entities with a type, relationships between them),
    with the names the graph already knows offered so the same thing is
    written the same way, the graph's own "what to look for", and a quote
    for every relationship (the Council finds the line from it)."""
    ents = "\n".join(f"- {k.type}: {k.name}" for k in known[:60])
    rels = ", ".join(_RELATION_WORDS)
    system = ("You build a knowledge graph from documents. Extract the entities a "
              "passage names and the relationships it states between them. Only "
              "state what the text says: no guesses, no outside knowledge, and skip "
              "anything the text negates or only considers.")
    user = ("Entity types: use one of " + ", ".join(SUGGESTED_TYPES)
            + " when it fits, otherwise a short type of your own.\n"
            + (f"\nWhat to look for in these documents: {focus.strip()}\n" if focus.strip()
               else "")
            + (f"\nEntities already in the graph (write them exactly like this when the "
               f"text names them):\n{ents}\n" if ents else "")
            + f"\nRelationships: a short verb phrase in UPPER_SNAKE_CASE, subject first; "
              f"use {rels} when one fits.\n"
            + f"\nText:\n\"\"\"\n{text}\n\"\"\"\n\n"
              "List the named entities in the text (people, organisations, projects, "
              "parts, places, equipment and other specific things; not pronouns or "
              "generic words), each with its name as the text writes it, its type and "
              "a few words saying what it is here. Then every relationship the text "
              "states between two of those entities, with the exact sentence (or "
              "clause) from the text that states it as \"quote\". Answer only with "
              'JSON: {"entities": [{"name": ..., "type": ..., "description": ...}], '
              '"relationships": [{"source": ..., "relation": ..., "target": ..., '
              '"quote": ...}]}.')
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


#: Words that name nothing in particular: a model lists them as entities.
_GENERIC = {"he", "she", "they", "it", "we", "i", "you", "them", "him", "her", "us",
            "team", "the team", "company", "the company", "project", "the project",
            "part", "the part", "parts", "document", "report", "this report",
            "supplier", "the supplier", "customer", "the customer", "everyone",
            "someone", "management", "staff"}


def _named_line(text: str, name: str, etype: str) -> Optional[int]:
    """The first line (1-based) of ``text`` that names ``name`` - by its
    words in order, accents and case ignored; a person also surname first
    ('Okafor, Ngozi'). None when the text never names it."""
    k = Known("", etype, name, [])
    if etype == "PERSON" and kgm.is_initial_form(kgm.person_key(name)):
        # named_in never counts an initial as naming someone; here the
        # passage writing 'D. Whitfield' is what is asked
        want = f" {kgm.fold(name)} "
        for i, ln in enumerate(text.splitlines(), 1):
            if want in f" {kgm.fold(ln)} ":
                return i
        return None
    for i, ln in enumerate(text.splitlines(), 1):
        if named_in(ln, k):
            return i
    # a name hard-wrapped over two lines (PDF text)
    if named_in(text, k):
        return 1
    return None


def check_open(raw: Dict[str, Any], text: str, known: Sequence[Known]
               ) -> Tuple[List[Found], List[OpenLink], List[Rejected]]:
    """Keep what the passage supports. An entity must be named in it (a
    model that writes a full name the text only gives as initials has
    supplied the name itself - refused, as in the closed mode); a known
    entity of the graph is used when the name fits exactly one (a PERSON and
    a PART of one name are never one); a relationship needs both ends among
    the entities, a quote that is in the text, and both ends named near it."""
    idx = _index(known)
    rejected: List[Rejected] = []
    found: List[Found] = []
    by_form: Dict[str, List[Found]] = {}

    def forms(name: str, etype: str) -> set:
        out = {kgm.fold(name)}
        if etype == "PERSON":
            out.add(kgm.person_key(name))
        if kgm.looks_like_code(name):
            out.add(kgm.fold(kgm.code_key(name)))
        return {f for f in out if f}

    for e in raw.get("entities") or []:
        if not isinstance(e, dict):
            continue
        name = " ".join(str(e.get("name") or "").split()).strip(" .,;:\"'")
        etype = canonical_type(e.get("type"))
        f = kgm.fold(name)
        if len(f) < 2 or f in _GENERIC or (f.replace(" ", "").isdigit() and len(f) < 5):
            rejected.append(Rejected(e, "not a particular thing"))
            continue
        if len(name) > 80:
            rejected.append(Rejected(e, "too long for a name"))
            continue
        line = _named_line(text, name, etype)
        if line is None:
            rejected.append(Rejected(e, f"the text never names {name}"))
            continue
        k = None
        if etype == "PERSON" and kgm.is_initial_form(kgm.person_key(name)):
            # 'D. Whitfield': the graph's person only when exactly one fits;
            # several = only the user can say (the closed mode's rule)
            k = resolve(name, idx)
            if k is None and any(
                    (lambda t: len(t) >= 2 and t[-1] == kgm.person_key(name).split()[1])(
                        kgm.person_key(x.name).split()) for x in idx.get("\0people", [])):
                rejected.append(Rejected(e, f"{name} could be more than one known person"))
                continue
        else:
            k = resolve(name, idx)
        if k is not None and k.type in CORE_TYPES and etype in CORE_TYPES \
                and k.type != etype:
            k = None                       # a PERSON is never a PART of one name
        if k is not None:
            etype = k.type
        fd = Found(name, etype, " ".join(str(e.get("description") or "").split())[:160],
                   k, line)
        dup = next((x for x in by_form.get(next(iter(forms(name, etype))), [])
                    if x.type == etype or (x.known and k and x.known.key == k.key)), None)
        if dup is not None:
            if not dup.description and fd.description:
                dup.description = fd.description
            continue
        found.append(fd)
        for fm in forms(name, etype):
            by_form.setdefault(fm, []).append(fd)
    _fold_bracketed(found, text, by_form, forms)

    def end(name: str) -> Optional[Found]:
        name = " ".join(str(name or "").split()).strip(" .,;:\"'")
        hits: List[Found] = []
        for fm in forms(name, "PERSON"):
            for x in by_form.get(fm, []):
                if x not in hits:
                    hits.append(x)
        if len(hits) == 1:
            return hits[0]
        if not hits:
            k = resolve(name, idx)
            if k is not None:
                # a known entity the model named only in the relationship
                hits = [x for x in found if x.known and x.known.key == k.key]
                if hits:
                    return hits[0]
                line = _named_line(text, name, k.type) or _named_line(text, k.name, k.type)
                if line is not None:
                    fd = Found(name, k.type, "", k, line)
                    found.append(fd)
                    for fm in forms(name, k.type):
                        by_form.setdefault(fm, []).append(fd)
                    return fd
        return None

    links: List[OpenLink] = []
    seen = set()
    lines = text.splitlines()
    for r in raw.get("relationships") or []:
        if not isinstance(r, dict):
            continue
        pred, swap = relation_direction(r.get("relation"))
        s, o = end(r.get("source")), end(r.get("target"))
        if swap:
            s, o = o, s
        if s is None or o is None:
            rejected.append(Rejected(r, "names an entity that is not among the passage's"))
            continue
        if s is o or (s.known and o.known and s.known.key == o.known.key):
            rejected.append(Rejected(r, "links an entity to itself"))
            continue
        fixed = ""
        if pred in SIGNATURES and s.type in CORE_TYPES and o.type in CORE_TYPES:
            st, ot = SIGNATURES[pred]
            if not (s.type in st and o.type in ot):
                if o.type in st and s.type in ot:
                    s, o, fixed = o, s, "turned round (the types were reversed)"
                else:
                    rejected.append(Rejected(r, f"{pred} cannot link a {s.type} to a "
                                                f"{o.type}"))
                    continue
        if pred == "SUPERSEDES":
            rs, ro = _revision(s.name), _revision(o.name)
            if rs and ro and rs[0] == ro[0] and rs[1] != ro[1] and rs[1] < ro[1]:
                s, o, fixed = o, s, "turned round (the later revision supersedes)"
        quote = str(r.get("quote") or "").strip()
        span = find_quote_span(text, quote)
        if span is None:
            rejected.append(Rejected(r, "its quote is not in the text"))
            continue
        first, last = span
        near = "\n".join(lines[max(0, first - 2):last + 2])
        # people and parts named near the quote; anything else (a project,
        # an organisation a note is about) anywhere in the passage
        unnamed = [x.name for x in (s, o)
                   if x.type in ("PERSON", "PART")
                   and _named_line(near, x.name, x.type) is None
                   and not (x.known and named_in(near, x.known))]
        if unnamed:
            rejected.append(Rejected(r, f"the text never names {unnamed[0]} near that quote"))
            continue
        key = (id(s), pred, id(o))
        if key in seen:
            continue
        seen.add(key)
        links.append(OpenLink(s, pred, o, quote, first, fixed))
    return found, links, rejected


def _fold_bracketed(found: List[Found], text: str, by_form, forms) -> None:
    """'Falcon Wing Retrofit (KA-101)': two names the passage gives one thing
    (same type, the second in brackets right after the first) are one entity
    - the bracketed one becomes its alias. A known entity is never folded
    into another known one (only the user merges those)."""
    flat = " ".join(text.split()).lower()
    for a in list(found):
        for b in list(found):
            if a is b or a.type != b.type or b not in found or a not in found:
                continue
            if a.known and b.known and a.known.key != b.known.key:
                continue
            if f"{a.name.lower()} ({b.name.lower()})" not in flat:
                continue
            # the code-shaped one is the entity's name, the plain one its alias
            keep, gone = (b, a) if kgm.looks_like_code(b.name) and not \
                kgm.looks_like_code(a.name) else (a, b)
            keep.aliases += [gone.name] + gone.aliases
            keep.known = keep.known or gone.known
            keep.description = keep.description or gone.description
            found.remove(gone)
            for fm in forms(gone.name, gone.type):
                by_form[fm] = [keep if x is gone else x for x in by_form.get(fm, [])]


def parse_open(reply: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", reply or "", re.S)
    if not m:
        raise ValueError("the model's answer has no JSON")
    data = json.loads(m.group(0))
    if not isinstance(data, dict) or not isinstance(data.get("entities", []), list) \
            or not isinstance(data.get("relationships", []), list):
        raise ValueError("the model's answer has no 'entities' / 'relationships' lists")
    return data


def extract_open(text: str, known: Sequence[Known], chat: ChatFn, focus: str = ""
                 ) -> OpenResult:
    res = OpenResult()
    try:
        try:
            res.raw = chat(build_open_prompt(text, known, focus),
                           num_predict=OPEN_NUM_PREDICT, schema=OPEN_SCHEMA)
        except TypeError:
            # a chat function of the closed mode's shape (a test's, a Pi's)
            res.raw = chat(build_open_prompt(text, known, focus))
        data = parse_open(res.raw)
    except Exception as exc:                               # noqa: BLE001
        res.error = f"{exc.__class__.__name__}: {exc}"[:300]
        return res
    res.entities, res.links, res.rejected = check_open(data, text, known)
    return res
