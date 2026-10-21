"""Asking the knowledge graph: what it holds about the things a question names.

Morphik answered a question with its graph by finding the entities the
question names, expanding their neighbourhood a few hops and handing what it
found - with the paths between them - to the model beside the retrieved text
(user, 2026-10-21: the graph should be used for "Answers in the Council" and
"Ask the graph"). This does that, the Council's way:

  * the entities are found by their names and aliases in the question (every
    run of up to six words, accents and case ignored, a person also by
    'Last, First') - no model call, so nothing is guessed and it costs
    nothing per question (Morphik asked a model, and re-ran its merge over
    every entity on every query - docs/morphik_assessment.md §2);
  * their links are followed ``hops`` deep (1 = their own links), the
    user's accepted links first, a user's rejected link never; a hub's links
    are capped so one busy person cannot fill the block;
  * when the question names two or more, the shortest chain of links
    between them is given too ("how is X connected to Y");
  * every fact is numbered and cited: the file, the sheet/row or page/line,
    the quoted text, and whether a model read it there.

``context`` gives that for one graph; ``block`` for every graph the user
marked for answers (council_core.kg_graphs), which council_core.turn_context
puts in front of the Council's question; ``ask`` answers a question from it
with a local model, or lists the facts when there is no model. Toolkit-free;
reads the stores only.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from council_core import knowledge_graph as kgm

#: How a link reads from each end (the Connections tab shows the same words).
PHRASES = {
    "LEADS": ("leads", "led by"),
    "CONTACT_FOR": ("point of contact for", "point of contact"),
    "WORKS_ON": ("works on", "worked on by"),
    "OWNS": ("owns", "owned by"),
    "USES_PART": ("uses part", "used in"),
    "SUPERSEDES": ("supersedes", "superseded by"),
    "DOCUMENTED_IN": ("documents (Collection)", "in Collection of"),
    "INSPECTED": ("inspected", "inspected by"),
}


def phrase(predicate: str, direction: str) -> str:
    """'LEADS' -> 'leads' / 'led by'; a model's own relationship reads as
    its words, and from the other end with an arrow ('← supplies')."""
    if predicate in PHRASES:
        out, inn = PHRASES[predicate]
        return out if direction == "out" else inn
    words = predicate.replace("_", " ").lower()
    return words if direction == "out" else f"← {words}"


#: Words that never name an entity on their own, however an alias reads.
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are",
         "was", "who", "what", "which", "where", "when", "how", "why", "does", "do",
         "did", "it", "this", "that", "part", "parts", "project", "projects", "person",
         "people", "team", "about", "tell", "me", "us", "show", "list", "all", "any",
         "work", "works", "lead", "leads", "owner", "owns", "use", "uses", "used"}
MAX_WORDS = 6


@dataclass
class Fact:
    n: int
    relation_id: str
    subject: Dict[str, Any]
    predicate: str
    object: Dict[str, Any]
    status: str
    hop: int
    evidence: List[Dict[str, Any]] = field(default_factory=list)


def _ngrams(text: str) -> List[Tuple[int, int, str]]:
    toks = kgm.fold(text).split()
    out = []
    for i in range(len(toks)):
        for j in range(i + 1, min(len(toks), i + MAX_WORDS) + 1):
            out.append((i, j, " ".join(toks[i:j])))
    return out


def entities_in_text(kg: kgm.KnowledgeGraph, text: str, limit: int = 8
                     ) -> List[Dict[str, Any]]:
    """The graph's entities a question names, longest name first, each
    once: 'Falcon Wing Retrofit' is the project, not also 'Falcon'. A single
    word counts only when it is not an everyday word and is 3+ letters (a
    code such as 'KA-101' folds to two words)."""
    grams = _ngrams(text)
    if not grams:
        return []
    by_norm: Dict[str, List[Tuple[int, int]]] = {}
    for i, j, g in grams:
        if j - i == 1 and (g in _STOP or len(g) < 3):
            continue
        by_norm.setdefault(g, []).append((i, j))
    hits: List[Tuple[int, int, str]] = []
    norms = list(by_norm)
    for k in range(0, len(norms), 400):
        part = norms[k:k + 400]
        marks = ",".join("?" * len(part))
        rows = kg.db.execute(
            "SELECT a.alias_norm, a.entity_id FROM aliases a JOIN entities e ON"
            f" e.id=a.entity_id WHERE a.alias_norm IN ({marks}) AND e.type != 'DOCUMENT'",
            part).fetchall()
        rows += kg.db.execute(
            "SELECT key, id FROM entities WHERE type='PERSON' AND"
            f" key IN ({marks})", part).fetchall()
        for norm, eid in rows:
            for i, j in by_norm.get(norm, []):
                hits.append((i, j, kg.resolve(eid)))
    hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
    used: set = set()
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for i, j, eid in hits:
        span = set(range(i, j))
        if span & used or eid in seen:
            continue
        ent = kg.entity(eid)
        if ent is None or ent.get("merged_into"):
            continue
        used |= span
        seen.add(eid)
        out.append(ent)
    order = {e["id"]: min(i for i, j, x in hits if x == e["id"]) for e in out}
    out.sort(key=lambda e: order[e["id"]])
    return out[:limit]


_RANK = {"accepted": 0, "seeded": 1, "suggested": 2}


def neighbourhood(kg: kgm.KnowledgeGraph, seeds: Sequence[str], *, hops: int = 2,
                  max_facts: int = 40, per_entity: int = 15, evidence: int = 2
                  ) -> List[Fact]:
    """The links around ``seeds``, nearest first: a breadth-first walk
    ``hops`` deep over every link but the rejected and the Collection ones
    (a project's documents are not facts about it), at most ``per_entity``
    links from any one entity."""
    facts: List[Fact] = []
    seen_rel: set = set()
    visited = {kg.resolve(s) for s in seeds}
    frontier = list(visited)
    for hop in range(1, max(1, hops) + 1):
        nxt: List[str] = []
        for eid in frontier:
            rows = kg.db.execute(
                "SELECT * FROM relations WHERE (subject_id=? OR object_id=?) AND"
                " status != 'rejected' AND predicate != 'DOCUMENTED_IN'", (eid, eid)
            ).fetchall()
            rows.sort(key=lambda r: (_RANK.get(r["status"], 3), r["predicate"]))
            for r in rows[:per_entity]:
                if r["id"] in seen_rel:
                    continue
                seen_rel.add(r["id"])
                s, o = kg.entity(r["subject_id"]), kg.entity(r["object_id"])
                if s is None or o is None:
                    continue
                facts.append(Fact(0, r["id"], s, r["predicate"], o, r["status"], hop,
                                  kg.evidence(r["id"], limit=evidence)))
                other = r["object_id"] if r["subject_id"] == eid else r["subject_id"]
                if other not in visited:
                    visited.add(other)
                    nxt.append(other)
                if len(facts) >= max_facts:
                    break
            if len(facts) >= max_facts:
                break
        frontier = nxt
        if len(facts) >= max_facts or not frontier:
            break
    facts.sort(key=lambda f: (f.hop, _RANK.get(f.status, 3)))
    for i, f in enumerate(facts, 1):
        f.n = i
    return facts


def path_between(kg: kgm.KnowledgeGraph, a: str, b: str, max_hops: int = 4
                 ) -> List[Tuple[str, str, str, str]]:
    """The shortest chain of links from ``a`` to ``b`` (either direction),
    as (relation id, from id, predicate, to id) steps; [] when none within
    ``max_hops``."""
    a, b = kg.resolve(a), kg.resolve(b)
    if a == b:
        return []
    prev: Dict[str, Tuple[str, str, str, str]] = {}
    q = deque([(a, 0)])
    seen = {a}
    while q:
        eid, d = q.popleft()
        if d >= max_hops:
            continue
        for r in kg.db.execute(
                "SELECT id, subject_id, predicate, object_id FROM relations WHERE"
                " (subject_id=? OR object_id=?) AND status != 'rejected' AND"
                " predicate != 'DOCUMENTED_IN'", (eid, eid)):
            other = r["object_id"] if r["subject_id"] == eid else r["subject_id"]
            if other in seen:
                continue
            seen.add(other)
            prev[other] = (r["id"], r["subject_id"], r["predicate"], r["object_id"])
            if other == b:
                steps = []
                cur = b
                while cur != a:
                    step = prev[cur]
                    steps.append(step)
                    cur = step[1] if step[3] == cur else step[3]
                return list(reversed(steps))
            q.append((other, d + 1))
    return []


def _name(ent: Dict[str, Any]) -> str:
    name = ent.get("name", "")
    plain = [x for x in ent.get("aliases", []) if kgm.fold(x) != kgm.fold(name)
             and not kgm.looks_like_code(x)]
    if kgm.looks_like_code(name) and plain:
        name = f"{name} \"{max(plain, key=len)}\""
    return f"{name} ({ent.get('type', '').lower()})"


def _cite(kg: kgm.KnowledgeGraph, ev: Dict[str, Any]) -> str:
    where = f"{ev.get('file') or ev.get('path')}, {ev.get('where')}".rstrip(", ")
    quote = " ".join(str(ev.get("quote") or "").split())
    if len(quote) > 160:
        quote = quote[:159] + "…"
    who = f"; read by a model ({str(ev.get('model') or '').split(' @ ')[0]})" \
        if ev.get("method") == "model" else ""
    return f"{where}: \"{quote}\"{who}" if quote else f"{where}{who}"


def context(kg: kgm.KnowledgeGraph, question: str, *, hops: int = 2, max_facts: int = 30,
            graph_name: str = "") -> Optional[Dict[str, Any]]:
    """What one graph holds about what ``question`` names, as numbered,
    cited facts: {entities, facts, paths, text, sources}. None when the
    question names nothing the graph knows."""
    ents = entities_in_text(kg, question)
    if not ents:
        return None
    facts = neighbourhood(kg, [e["id"] for e in ents], hops=hops, max_facts=max_facts)
    by_rel = {f.relation_id: f for f in facts}
    paths: List[List[Fact]] = []
    if len(ents) >= 2:
        for x in range(len(ents)):
            for y in range(x + 1, len(ents)):
                steps = path_between(kg, ents[x]["id"], ents[y]["id"])
                if len(steps) < 2:
                    continue                    # none, or a direct link listed already
                chain = []
                for rid, s, pred, o in steps:
                    f = by_rel.get(rid)
                    if f is None:
                        st = kg.db.execute("SELECT status FROM relations WHERE id=?",
                                           (rid,)).fetchone()
                        f = Fact(len(facts) + 1, rid, kg.entity(s), pred, kg.entity(o),
                                 st[0] if st else "", 0, kg.evidence(rid, limit=2))
                        facts.append(f)
                        by_rel[rid] = f
                    chain.append(f)
                paths.append(chain)
    title = f"Knowledge graph \"{graph_name}\"" if graph_name else "Knowledge graph"
    lines = [f"{title} — what it holds about: "
             + ", ".join(_name(e) for e in ents) + "."]
    for e in ents:
        if e.get("description"):
            lines.append(f"About {e['name']}: {e['description']}")
    if not facts:
        lines.append("It knows these, but holds no links for them.")
    sources: List[str] = []
    for f in facts:
        mark = " [suggested, not yet confirmed]" if f.status == "suggested" else ""
        cites = "; ".join(_cite(kg, ev) for ev in f.evidence) or "no source kept"
        lines.append(f"[{f.n}] {_name(f.subject)} — {phrase(f.predicate, 'out')} → "
                     f"{_name(f.object)}{mark}. Source: {cites}")
        for ev in f.evidence:
            p = str(kg.abs_path(ev["path"]))
            if p not in sources:
                sources.append(p)
    for chain in paths:
        a, b = chain[0], chain[-1]
        lines.append("How they connect: " + " → ".join(f"[{f.n}]" for f in chain)
                     + f" ({len(chain)} links)")
    return {"entities": ents, "facts": facts, "paths": paths,
            "text": "\n".join(lines), "sources": sources}


def _graphs(vault: Any, graphs: Optional[Sequence[str]]) -> List[Tuple[str, str]]:
    """(id, name) of the graphs to read: those named, else the ones marked
    for answers. A graph never built has no store and is skipped (asking
    must not create one)."""
    from council_core import kg_graphs as kgg
    gl = kgg.GraphList(vault)
    try:
        infos = gl.all() if graphs is not None else gl.for_answers()
    except kgg.GraphListDamaged:
        infos = [kgg.GraphInfo(kgg.MAIN, kgg.MAIN_NAME)]
    out = []
    for g in infos:
        if graphs is not None and g.id not in graphs:
            continue
        store = kgm.store_path(vault) if g.is_main else gl.store_dir(g.id) / kgm.STORE_NAME
        if store.exists():
            out.append((g.id, g.name))
    return out


def gather(vault: Any, question: str, graphs: Optional[Sequence[str]] = None, *,
           hops: int = 2, max_facts: int = 30) -> List[Dict[str, Any]]:
    """context() of each graph (named, or marked for answers) that knows
    something the question names. A busy or damaged store is skipped."""
    from council_core import kg_graphs as kgg
    gl = kgg.GraphList(vault)
    out = []
    for gid, name in _graphs(vault, graphs):
        try:
            with gl.open(gid) as kg:
                ctx = context(kg, question, hops=hops, max_facts=max_facts,
                              graph_name=name if gid != kgg.MAIN else "")
        except (kgm.KnowledgeGraphBusy, kgm.KnowledgeGraphDamaged, KeyError, ValueError):
            continue
        if ctx is not None:
            ctx["graph"] = gid
            out.append(ctx)
    return out


def block(vault: Any, question: str, *, max_chars: int = 6000
          ) -> Optional[Tuple[str, List[str]]]:
    """The text the Council's models are shown for ``question`` and the
    files it cites - or None when no graph knows anything it names."""
    ctxs = gather(vault, question)
    if not ctxs:
        return None
    text = "\n\n".join(c["text"] for c in ctxs)
    if len(text) > max_chars:
        cut = text.rfind("\n[", 0, max_chars)
        text = text[:cut if cut > 0 else max_chars] + "\n(… more links in the graph)"
    sources: List[str] = []
    for c in ctxs:
        sources += [s for s in c["sources"] if s not in sources]
    return text, sources


ASK_SYSTEM = ("You answer questions from a knowledge graph built from the user's own "
              "documents. Use ONLY the numbered facts given. Cite the facts you use like "
              "[2]. If the facts do not answer the question, say so plainly and say what "
              "they do show. A fact marked 'suggested' is not confirmed yet - say so when "
              "you rely on one.")


def ask(vault: Any, question: str, chat: Optional[Callable[..., str]] = None,
        graphs: Optional[Sequence[str]] = None, *, hops: int = 2) -> Dict[str, Any]:
    """Answer ``question`` from the graph(s): {answer, text, contexts,
    error}. ``answer`` is the model's (None without a model, or when it
    failed - ``error`` says why); ``text`` is always the cited facts, so a
    PC without a model still gets what the graph knows."""
    ctxs = gather(vault, question, graphs, hops=hops)
    text = "\n\n".join(c["text"] for c in ctxs)
    out: Dict[str, Any] = {"answer": None, "text": text, "contexts": ctxs, "error": ""}
    if not ctxs:
        out["error"] = ("the question names no person, part, project or other entity the "
                        "graph knows - try a name as the documents write it")
        return out
    if chat is None:
        return out
    msgs = [{"role": "system", "content": ASK_SYSTEM},
            {"role": "user", "content": f"Facts:\n{text}\n\nQuestion: {question}"}]
    try:
        try:
            out["answer"] = (chat(msgs, num_predict=900, schema=None) or "").strip()
        except TypeError:
            out["answer"] = (chat(msgs) or "").strip()
    except Exception as exc:                               # noqa: BLE001
        out["error"] = f"the model could not answer: {exc.__class__.__name__}: {exc}"[:300]
    return out
