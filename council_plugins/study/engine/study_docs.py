"""
Study documents made from a literature review: flash cards, a quiz, a study
guide and a glossary. Each item cites the passage it came from.

HOW (``make_study_set``)
For each section of the review the model is shown the section's checked
sentences and the numbered passages the section was written from, and asked
for each kind wanted, as JSON constrained by a schema. Every item is then
checked without a model (engine.common.support) and DROPPED, with the
reason counted, when:
  * it cites no passage, or one this section was not shown;
  * its answer (a card's back, a quiz answer, a definition, a guide point)
    is not in the passage it cites;
  * a glossary term is not in its passage;
  * a multiple-choice question does not have four different choices with
    the answer among them;
  * it repeats an item already made.
So a card cannot teach something the reader's own documents do not say.

The set is plain data (``StudySet``, JSON through to_dict/from_dict). It
carries the passages it cites, so it stays readable when the review is
written again. Practice and export are engine.practice and engine.export.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import common as cm
from .review import Review, Source

KINDS = ("cards", "quiz", "guide", "glossary")
KIND_NAMES = {"cards": "Flash cards", "quiz": "Quiz", "guide": "Study guide",
              "glossary": "Glossary"}
PER_SECTION = {"cards": 6, "quiz": 4, "guide": 5, "glossary": 5}
TOKENS = 1200

_SRC = {"type": "integer", "minimum": 1, "maximum": 999}
SCHEMAS: Dict[str, Dict[str, Any]] = {
    "cards": {"type": "object", "required": ["cards"], "properties": {
        "cards": {"type": "array", "maxItems": PER_SECTION["cards"], "items": {
            "type": "object", "required": ["front", "back", "source"],
            "properties": {"front": {"type": "string", "maxLength": 200},
                           "back": {"type": "string", "maxLength": 300},
                           "source": _SRC}}}}},
    "quiz": {"type": "object", "required": ["questions"], "properties": {
        "questions": {"type": "array", "maxItems": PER_SECTION["quiz"], "items": {
            "type": "object", "required": ["kind", "question", "answer", "source"],
            "properties": {"kind": {"type": "string", "enum": ["choice", "short"]},
                           "question": {"type": "string", "maxLength": 250},
                           "choices": {"type": "array", "maxItems": 4,
                                       "items": {"type": "string", "maxLength": 120}},
                           "answer": {"type": "string", "maxLength": 200},
                           "explanation": {"type": "string", "maxLength": 300},
                           "source": _SRC}}}}},
    "guide": {"type": "object", "required": ["points"], "properties": {
        "points": {"type": "array", "maxItems": PER_SECTION["guide"], "items": {
            "type": "object", "required": ["text", "source"],
            "properties": {"text": {"type": "string", "maxLength": 300},
                           "source": _SRC}}}}},
    "glossary": {"type": "object", "required": ["terms"], "properties": {
        "terms": {"type": "array", "maxItems": PER_SECTION["glossary"], "items": {
            "type": "object", "required": ["term", "definition", "source"],
            "properties": {"term": {"type": "string", "maxLength": 60},
                           "definition": {"type": "string", "maxLength": 300},
                           "source": _SRC}}}}},
}

ASK = {
    "cards": ("Make up to {n} flash cards. The front is a question or a term; "
              "the back is the answer, in words taken from the source. "
              'Reply with JSON: {{"cards": [{{"front": ..., "back": ..., '
              '"source": <number>}}]}}'),
    "quiz": ("Make up to {n} quiz questions, mostly multiple choice. A choice "
             'question has "kind": "choice", exactly four different "choices" '
             'and the right one as "answer" (copied exactly); a short-answer '
             'question has "kind": "short". Add a one-sentence "explanation" '
             'from the source. Reply with JSON: {{"questions": [{{"kind": ..., '
             '"question": ..., "choices": [...], "answer": ..., '
             '"explanation": ..., "source": <number>}}]}}'),
    "guide": ("Write up to {n} key points a student must remember, each one "
              'sentence from the source. Reply with JSON: {{"points": '
              '[{{"text": ..., "source": <number>}}]}}'),
    "glossary": ("List up to {n} technical terms the sources use, each with "
                 "its definition in words from the source. Reply with JSON: "
                 '{{"terms": [{{"term": ..., "definition": ..., "source": <number>}}]}}'),
}


@dataclass
class StudySet:
    title: str
    model: str = ""
    created: float = 0.0
    cards: List[Dict[str, Any]] = field(default_factory=list)
    quiz: List[Dict[str, Any]] = field(default_factory=list)
    guide: List[Dict[str, Any]] = field(default_factory=list)   # {heading, points}
    glossary: List[Dict[str, Any]] = field(default_factory=list)
    dropped: Dict[str, int] = field(default_factory=dict)       # reason -> count
    sources: Dict[int, Source] = field(default_factory=dict)

    def counts(self) -> Dict[str, int]:
        return {"cards": len(self.cards), "quiz": len(self.quiz),
                "guide": sum(len(g["points"]) for g in self.guide),
                "glossary": len(self.glossary),
                "dropped": sum(self.dropped.values())}

    def to_dict(self) -> Dict[str, Any]:
        return {"version": 1, "title": self.title, "model": self.model,
                "created": self.created, "cards": self.cards, "quiz": self.quiz,
                "guide": self.guide, "glossary": self.glossary,
                "dropped": dict(self.dropped),
                "sources": [s.to_dict() for _n, s in sorted(self.sources.items())]}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StudySet":
        s = cls(title=str(d.get("title", "")), model=str(d.get("model", "")),
                created=float(d.get("created") or 0),
                cards=list(d.get("cards") or []), quiz=list(d.get("quiz") or []),
                guide=list(d.get("guide") or []), glossary=list(d.get("glossary") or []),
                dropped={str(k): int(v) for k, v in (d.get("dropped") or {}).items()})
        for x in d.get("sources") or []:
            src = Source.from_dict(x)
            s.sources[src.n] = src
        return s

    # -- rendering --
    def cite(self, n: int) -> str:
        return f"<a href='cite:{n}'>[{n}]</a>"

    def to_html(self, kind: str, esc: Callable[[str], str], *,
                answers: bool = True) -> str:
        out = [f"<h2>{esc(KIND_NAMES[kind])}: {esc(self.title)}</h2>"]
        if kind == "cards":
            if not self.cards:
                out.append("<p>No flash cards.</p>")
            for i, c in enumerate(self.cards, 1):
                out.append(f"<p><b>{i}. {esc(c['front'])}</b><br>{esc(c['back'])} "
                           f"{self.cite(c['source'])}</p>")
        elif kind == "quiz":
            if not self.quiz:
                out.append("<p>No quiz questions.</p>")
            for i, q in enumerate(self.quiz, 1):
                out.append(f"<p><b>{i}. {esc(q['question'])}</b></p>")
                if q.get("choices"):
                    out.append("<ol type='A'>" + "".join(
                        f"<li>{esc(c)}</li>" for c in q["choices"]) + "</ol>")
            if answers and self.quiz:
                out.append("<h3>Answer key</h3><ol>")
                for q in self.quiz:
                    letter = ""
                    if q.get("choices"):
                        letter = "ABCD"[q["choices"].index(q["answer"])] + ". "
                    why = f" {esc(q['explanation'])}" if q.get("explanation") else ""
                    out.append(f"<li>{letter}{esc(q['answer'])}.{why} "
                               f"{self.cite(q['source'])}</li>")
                out.append("</ol>")
        elif kind == "guide":
            if not self.guide:
                out.append("<p>No study guide.</p>")
            for g in self.guide:
                out.append(f"<h3>{esc(g['heading'])}</h3><ul>")
                out += [f"<li>{esc(p['text'])} {self.cite(p['source'])}</li>"
                        for p in g["points"]]
                out.append("</ul>")
        elif kind == "glossary":
            if not self.glossary:
                out.append("<p>No glossary.</p>")
            out.append("<dl>")
            for t in sorted(self.glossary, key=lambda t: t["term"].lower()):
                out.append(f"<dt><b>{esc(t['term'])}</b></dt><dd>{esc(t['definition'])} "
                           f"{self.cite(t['source'])}</dd>")
            out.append("</dl>")
        cited = sorted({n for n in self.cited(kind)})
        if cited:
            out.append("<h3>Sources</h3><ol>")
            out += [f"<li value='{n}'><a href='cite:{n}'>{esc(self.sources[n].cite())}"
                    f"</a></li>" for n in cited if n in self.sources]
            out.append("</ol>")
        return "".join(out)

    def cited(self, kind: str) -> List[int]:
        if kind == "guide":
            return [p["source"] for g in self.guide for p in g["points"]]
        return [x["source"] for x in getattr(self, kind)]


# ======================================================================
# Checking what the model made
# ======================================================================
def _drop(st: StudySet, why: str) -> None:
    st.dropped[why] = st.dropped.get(why, 0) + 1


def _source(item: Dict[str, Any], shown: Sequence[int],
            sources: Dict[int, Source]) -> Optional[Source]:
    n = cm.as_int(item.get("source"))
    return sources.get(n) if n in shown else None


def check_card(item: Dict[str, Any], src: Source) -> Optional[Dict[str, Any]]:
    front, back = cm.as_text(item.get("front"), 200), cm.as_text(item.get("back"), 300)
    if not front or not back:
        return None
    if not cm.supported(back, [src.text]):
        return None
    return {"front": front, "back": back, "source": src.n}


def check_question(item: Dict[str, Any], src: Source) -> Optional[Dict[str, Any]]:
    """A quiz question, or None (with ``why`` in item['_why'])."""
    question = cm.as_text(item.get("question"), 250)
    answer = cm.as_text(item.get("answer"), 200)
    if not question or not answer:
        item["_why"] = "empty question or answer"
        return None
    choices = [cm.as_text(c, 120) for c in item.get("choices") or [] if cm.as_text(c)]
    kind = "choice" if choices or item.get("kind") == "choice" else "short"
    out = {"kind": kind, "question": question, "answer": answer,
           "explanation": cm.as_text(item.get("explanation"), 300), "source": src.n}
    if kind == "choice":
        folded = [c.lower() for c in choices]
        if len(choices) != 4 or len(set(folded)) != 4:
            item["_why"] = "not four different choices"
            return None
        if answer.lower() not in folded:
            item["_why"] = "the answer is not among the choices"
            return None
        out["answer"] = choices[folded.index(answer.lower())]
        out["choices"] = choices
    if not cm.supported(out["answer"], [src.text]):
        item["_why"] = "not in the passage it cites"
        return None
    return out


def check_term(item: Dict[str, Any], src: Source) -> Optional[Dict[str, Any]]:
    term = cm.as_text(item.get("term"), 60)
    definition = cm.as_text(item.get("definition"), 300)
    if not term or not definition:
        return None
    if not cm.supported(term, [src.text], share=1.0):
        return None
    if not cm.supported(definition, [src.text]):
        return None
    return {"term": term, "definition": definition, "source": src.n}


def check_point(item: Dict[str, Any], src: Source) -> Optional[Dict[str, Any]]:
    text = cm.as_text(item.get("text"), 300)
    if not text or not cm.supported(text, [src.text]):
        return None
    return {"text": text, "source": src.n}


_LIST_KEY = {"cards": "cards", "quiz": "questions", "guide": "points",
             "glossary": "terms"}
_CHECK = {"cards": check_card, "quiz": check_question, "guide": check_point,
          "glossary": check_term}
_UNIQUE = {"cards": "front", "quiz": "question", "guide": "text", "glossary": "term"}


def _prompt(kind: str, review: Review, sec, n: int) -> List[Dict[str, str]]:
    block = cm.sources_block({k: review.sources[k] for k in sec.shown
                              if k in review.sources})
    summary = " ".join(s.text + "".join(f"[{c}]" for c in s.cites)
                       for s in sec.sentences if not s.gap)
    return [
        {"role": "system", "content":
            "You make study material from a reader's own documents, using ONLY "
            "the numbered sources given. Every item names the number of the "
            "source it comes from. Reply with JSON only."},
        {"role": "user", "content":
            f"Topic: {review.title} — {sec.heading}\n\nWhat the review says:\n"
            f"{summary or '(nothing yet)'}\n\nSOURCES:\n{block}\n\n"
            + ASK[kind].format(n=n)},
    ]


def make_study_set(chat, review: Review, kinds: Sequence[str] = KINDS, *,
                   model: str = "",
                   on_progress: Optional[Callable[[str], None]] = None,
                   should_stop: Optional[Callable[[], bool]] = None) -> StudySet:
    """Study documents of ``kinds`` from ``review`` (module docstring)."""
    say = on_progress or (lambda _t: None)
    kinds = [k for k in KINDS if k in kinds]
    if not kinds:
        raise ValueError("choose at least one kind of study document")
    if not review.sections or not review.cited():
        raise ValueError("the review cites nothing yet: write the review first")
    st = StudySet(title=review.title, model=model, created=time.time())
    seen: Dict[str, set] = {k: set() for k in kinds}
    for i, sec in enumerate(review.sections, 1):
        if not sec.shown:
            continue
        guide_points: List[Dict[str, Any]] = []
        for kind in kinds:
            cm.check_stop(should_stop)
            say(f"{KIND_NAMES[kind].lower()} for section {i} of "
                f"{len(review.sections)}: {sec.heading}")
            reply = chat(_prompt(kind, review, sec, PER_SECTION[kind]),
                         max_tokens=TOKENS, schema=SCHEMAS[kind])
            data = cm.parse_json(reply)
            items = data.get(_LIST_KEY[kind]) if isinstance(data, dict) else None
            if not isinstance(items, list):
                _drop(st, "the model's reply was not the JSON asked for")
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                src = _source(item, sec.shown, review.sources)
                if src is None:
                    _drop(st, "cites no passage this section was shown")
                    continue
                got = _CHECK[kind](item, src)
                if got is None:
                    _drop(st, item.get("_why") or "not in the passage it cites")
                    continue
                key = " ".join(got[_UNIQUE[kind]].lower().split())
                if key in seen[kind]:
                    _drop(st, "repeats an item already made")
                    continue
                seen[kind].add(key)
                st.sources.setdefault(src.n, src)
                if kind == "guide":
                    guide_points.append(got)
                else:
                    getattr(st, kind).append(got)
        if guide_points:
            st.guide.append({"heading": sec.heading, "points": guide_points})
    say("")
    return st
