"""
The literature review: a notebook's documents written up section by
section, every sentence citing the passages it came from.

HOW ONE IS MADE (``write_review``)
  1. Outline. The model is shown the opening of every document (or, with a
     topic, the passages that best match the topic) and replies with a
     title and 3-6 sections, each a heading and the question it answers.
  2. Each section. The passages that best match its question (BM25 over the
     notebook, council_core.plugin_services) are numbered — the number of a
     passage is the same everywhere in the review — and the model writes the
     section from them only, ending every sentence with its citations, [3]
     or [3][5].
  3. The check (engine.common.support, no model). A sentence goes in when
     it cites at least one passage the section was shown AND most of its
     content words are in what it cites. Otherwise it is LEFT OUT and kept
     beside the section with why ("cites nothing", "cites [9], which this
     section was not shown", "not in what it cites"), so the reader can see
     what the model tried to say. A sentence saying the sources do not cover
     something is kept as a gap, uncited.

The review is plain data (``Review``, JSON through to_dict/from_dict), and
renders to HTML with each citation a link ``cite:<n>`` and to Markdown with
a sources list. The study documents (engine.study_docs) are made from it.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import common as cm

SECTIONS = 5
PER_SECTION = 6
OUTLINE_TOKENS = 700
SECTION_TOKENS = 1100
#: Opening characters of each document shown for the outline, and how many
#: documents at most.
OPENING_CHARS = 500
OUTLINE_DOCS = 40

OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 120},
        "sections": {"type": "array", "minItems": 2, "maxItems": 8, "items": {
            "type": "object",
            "properties": {"heading": {"type": "string", "maxLength": 80},
                           "question": {"type": "string", "maxLength": 200}},
            "required": ["heading", "question"]}},
    },
    "required": ["title", "sections"],
}

_CITE = re.compile(r"\[(\d{1,3})\]")
_GAP = re.compile(r"\b(?:sources?|passages?|documents?)\s+(?:do|does|did)\s+not\s+"
                  r"(?:cover|say|mention|address|discuss|describe|state|give)", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])(?:\s*(?:\[\d{1,3}\])+)?\s+(?=[A-Z0-9\"“(])")


# ======================================================================
# Sources (a passage as the review keeps it)
# ======================================================================
@dataclass
class Source:
    """A cited passage: enough to show it, cite it and read it again."""
    n: int
    document_id: str
    path: str
    display: str
    page: Any
    line_nos: List[int]
    text: str

    @classmethod
    def of(cls, n: int, p: Any) -> "Source":
        return cls(n, p.document_id, p.path, p.display, p.page,
                   list(p.line_nos), p.text)

    @property
    def key(self) -> str:
        return f"{self.document_id}:{self.line_nos[:1]}:{self.page}"

    @property
    def first_line(self) -> int:
        return self.line_nos[0] if self.line_nos else 0

    @property
    def last_line(self) -> int:
        return self.line_nos[-1] if self.line_nos else 0

    def where(self) -> str:
        at = f"p{self.page} " if isinstance(self.page, int) else \
            (f"{self.page} " if self.page else "")
        if self.first_line == self.last_line:
            return f"{at}L{self.first_line}"
        return f"{at}L{self.first_line}-L{self.last_line}"

    def cite(self) -> str:
        return f"{self.display}, {self.where()}"

    def locator(self, line: Optional[int] = None) -> Dict[str, Any]:
        loc: Dict[str, Any] = {"line": int(line or self.first_line)}
        if isinstance(self.page, int):
            loc["page"] = self.page
        elif self.page:
            loc["sheet"] = self.page
        return loc

    def to_dict(self) -> Dict[str, Any]:
        return {"n": self.n, "document_id": self.document_id, "path": self.path,
                "display": self.display, "page": self.page,
                "line_nos": list(self.line_nos), "text": self.text}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Source":
        return cls(int(d["n"]), str(d.get("document_id", "")), str(d.get("path", "")),
                   str(d.get("display") or d.get("path", "")), d.get("page"),
                   [int(x) for x in d.get("line_nos") or []], str(d.get("text", "")))


@dataclass
class Sentence:
    text: str
    cites: List[int] = field(default_factory=list)
    gap: bool = False


@dataclass
class LeftOut:
    text: str
    why: str


@dataclass
class Section:
    heading: str
    question: str
    sentences: List[Sentence] = field(default_factory=list)
    left_out: List[LeftOut] = field(default_factory=list)
    shown: List[int] = field(default_factory=list)      # sources it was given

    def text(self) -> str:
        return " ".join(s.text for s in self.sentences)


@dataclass
class Review:
    title: str
    topic: str = ""
    model: str = ""
    created: float = 0.0
    sections: List[Section] = field(default_factory=list)
    sources: Dict[int, Source] = field(default_factory=dict)

    # -- numbers --
    def cited(self) -> List[int]:
        seen: List[int] = []
        for sec in self.sections:
            for s in sec.sentences:
                for n in s.cites:
                    if n not in seen:
                        seen.append(n)
        return seen

    def counts(self) -> Dict[str, int]:
        return {"sections": len(self.sections),
                "sentences": sum(len(s.sentences) for s in self.sections),
                "left_out": sum(len(s.left_out) for s in self.sections),
                "sources": len(self.cited())}

    # -- JSON --
    def to_dict(self) -> Dict[str, Any]:
        return {"version": 1, "title": self.title, "topic": self.topic,
                "model": self.model, "created": self.created,
                "sections": [{"heading": s.heading, "question": s.question,
                              "shown": list(s.shown),
                              "sentences": [{"text": x.text, "cites": list(x.cites),
                                             "gap": x.gap} for x in s.sentences],
                              "left_out": [{"text": x.text, "why": x.why}
                                           for x in s.left_out]}
                             for s in self.sections],
                "sources": [src.to_dict() for _n, src in sorted(self.sources.items())]}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Review":
        r = cls(title=str(d.get("title", "")), topic=str(d.get("topic", "")),
                model=str(d.get("model", "")), created=float(d.get("created") or 0))
        for s in d.get("sources") or []:
            src = Source.from_dict(s)
            r.sources[src.n] = src
        for s in d.get("sections") or []:
            r.sections.append(Section(
                heading=str(s.get("heading", "")), question=str(s.get("question", "")),
                shown=[int(x) for x in s.get("shown") or []],
                sentences=[Sentence(str(x.get("text", "")),
                                    [int(n) for n in x.get("cites") or []],
                                    bool(x.get("gap"))) for x in s.get("sentences") or []],
                left_out=[LeftOut(str(x.get("text", "")), str(x.get("why", "")))
                          for x in s.get("left_out") or []]))
        return r

    # -- rendering --
    def to_html(self, esc: Callable[[str], str]) -> str:
        out = [f"<h2>{esc(self.title)}</h2>"]
        c = self.counts()
        meta = [f"{c['sentences']} cited sentences from {c['sources']} passages"]
        if self.model:
            meta.append(f"written by {esc(self.model)}")
        if c["left_out"]:
            meta.append(f"{c['left_out']} sentence(s) left out as unsupported")
        out.append(f"<p style='color:#888'>{' · '.join(meta)}</p>")
        for sec in self.sections:
            out.append(f"<h3>{esc(sec.heading)}</h3><p>")
            for s in sec.sentences:
                links = "".join(f"<a href='cite:{n}'>[{n}]</a>" for n in s.cites)
                style = " style='color:#888'" if s.gap else ""
                out.append(f"<span{style}>{esc(s.text)}</span>{links} ")
            out.append("</p>")
            if sec.left_out:
                items = "".join(f"<li>{esc(x.text)} <i>({esc(x.why)})</i></li>"
                                for x in sec.left_out)
                out.append(f"<details><p style='color:#a66'>Left out as unsupported:</p>"
                           f"<ul style='color:#a66'>{items}</ul></details>")
        cited = self.cited()
        if cited:
            out.append("<h3>Sources</h3><ol>")
            for n in sorted(cited):
                out.append(f"<li value='{n}'><a href='cite:{n}'>"
                           f"{esc(self.sources[n].cite())}</a></li>")
            out.append("</ol>")
        return "".join(out)

    def to_markdown(self) -> str:
        out = [f"# {self.title}", ""]
        for sec in self.sections:
            out += [f"## {sec.heading}", ""]
            out.append(" ".join(s.text + "".join(f"[{n}]" for n in s.cites)
                                for s in sec.sentences))
            out.append("")
        cited = self.cited()
        if cited:
            out += ["## Sources", ""]
            out += [f"{n}. {self.sources[n].cite()}" for n in sorted(cited)]
        return "\n".join(out).rstrip() + "\n"


# ======================================================================
# Writing one
# ======================================================================
def split_sentences(text: str) -> List[str]:
    """Sentences of a section, each keeping the citations that follow it."""
    text = " ".join((text or "").split())
    if not text:
        return []
    parts, start = [], 0
    for m in _SENTENCE.finditer(text):
        parts.append(text[start:m.end()].strip())
        start = m.end()
    parts.append(text[start:].strip())
    return [p for p in parts if p]


def check_section(sec: Section, body: str, sources: Dict[int, Source]) -> None:
    """Split the model's ``body`` into sentences and keep the supported ones
    (module docstring, step 3)."""
    shown = set(sec.shown)
    for raw in split_sentences(body):
        nums = [int(n) for n in _CITE.findall(raw)]
        text = " ".join(_CITE.sub("", raw).split())
        text = re.sub(r"\s+([.,;:!?])", r"\1", text)
        if not text or re.fullmatch(r"[\W\d]*", text):
            continue
        if _GAP.search(text):
            sec.sentences.append(Sentence(text, [], gap=True))
            continue
        good = [n for n in dict.fromkeys(nums) if n in shown and n in sources]
        if not nums:
            sec.left_out.append(LeftOut(text, "cites nothing"))
            continue
        if not good:
            bad = ", ".join(f"[{n}]" for n in nums)
            sec.left_out.append(LeftOut(text, f"cites {bad}, which this section "
                                              f"was not shown"))
            continue
        if not cm.supported(text, [sources[n].text for n in good]):
            sec.left_out.append(LeftOut(text, "not in what it cites"))
            continue
        sec.sentences.append(Sentence(text, good))


def _outline_prompt(topic: str, material: str, n: int) -> List[Dict[str, str]]:
    want = (f"a literature review on: {topic}" if topic else
            "a literature review of these documents as a whole")
    return [
        {"role": "system", "content":
            "You plan literature reviews from a reader's own documents. Reply "
            "with JSON only."},
        {"role": "user", "content":
            f"Plan {want}.\n\nReply with JSON: {{\"title\": ..., \"sections\": "
            f"[{{\"heading\": ..., \"question\": ...}}]}} with {n} sections. "
            f"The first section is an overview of the main themes. Each "
            f"question is what that section answers from the documents. Use "
            f"only subjects the material below is about.\n\nMATERIAL:\n{material}"},
    ]


def _section_prompt(title: str, sec: Section, block: str) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content":
            "You write sections of a literature review using ONLY the numbered "
            "sources you are given. You never add facts from memory."},
        {"role": "user", "content":
            f"Review: {title}\nSection: {sec.heading}\nQuestion: {sec.question}\n\n"
            f"Write this section in one to three paragraphs of plain prose (no "
            f"headings, no lists). End EVERY sentence with the numbers of the "
            f"sources it comes from, like [2] or [2][5]. Compare and contrast "
            f"the sources where they agree or differ. If the sources do not "
            f"cover part of the question, write a sentence saying the sources "
            f"do not cover it.\n\nSOURCES:\n{block}"},
    ]


def _material(sources: Any, nid: str, topic: str) -> str:
    """What the outline is planned from: each document's opening, or the
    passages that best match the topic."""
    if topic:
        hits = sources.search(nid, topic, 12)
        return "\n\n".join(f"{p.cite()}\n{cm.clip(p.text, 700)}" for p in hits)
    firsts: Dict[str, Any] = {}
    for p in sources.passages(nid):
        firsts.setdefault(p.document_id, p)
        if len(firsts) >= OUTLINE_DOCS:
            break
    return "\n\n".join(f"{p.display}\n{cm.clip(p.text, OPENING_CHARS)}"
                       for p in firsts.values())


def outline(chat, sources: Any, nid: str, topic: str = "",
            sections: int = SECTIONS) -> Review:
    """The review's title and sections, nothing written yet."""
    material = _material(sources, nid, topic)
    if not material.strip():
        raise ValueError("this notebook has no text to review yet: build it first")
    reply = chat(_outline_prompt(topic, material, sections),
                 max_tokens=OUTLINE_TOKENS, schema=OUTLINE_SCHEMA)
    data = cm.parse_json(reply) or {}
    review = Review(title=cm.as_text(data.get("title"), 120) or (topic or "Literature review"),
                    topic=topic)
    for s in (data.get("sections") or [])[:8]:
        if not isinstance(s, dict):
            continue
        heading = cm.as_text(s.get("heading"), 80)
        question = cm.as_text(s.get("question"), 200) or heading
        if heading:
            review.sections.append(Section(heading, question))
    if not review.sections:
        # a model that could not plan still gets a review of the topic
        review.sections = [Section("Overview", topic or "What do these documents say?")]
    return review


def write_review(chat, sources: Any, nid: str, *, topic: str = "", model: str = "",
                 sections: int = SECTIONS, per_section: int = PER_SECTION,
                 on_progress: Optional[Callable[[str], None]] = None,
                 should_stop: Optional[Callable[[], bool]] = None) -> Review:
    """A whole review (module docstring). ``chat(messages, max_tokens=,
    schema=)`` is the models service's; ``sources`` the sources service."""
    say = on_progress or (lambda _t: None)
    say("planning the review")
    review = outline(chat, sources, nid, topic, sections)
    review.model, review.created = model, time.time()
    numbers: Dict[str, int] = {}
    for i, sec in enumerate(review.sections, 1):
        cm.check_stop(should_stop)
        say(f"writing section {i} of {len(review.sections)}: {sec.heading}")
        query = f"{sec.heading} {sec.question} {topic}".strip()
        hits = sources.search(nid, query, per_section)
        for p in hits:
            k = f"{p.document_id}:{p.page}:{p.line_nos[:1]}"
            if k not in numbers:
                numbers[k] = len(numbers) + 1
                review.sources[numbers[k]] = Source.of(numbers[k], p)
            if numbers[k] not in sec.shown:
                sec.shown.append(numbers[k])
        if not hits:
            sec.sentences.append(Sentence("The sources do not cover this.", [], gap=True))
            continue
        block = cm.sources_block({n: review.sources[n] for n in sec.shown})
        body = chat(_section_prompt(review.title, sec, block), max_tokens=SECTION_TOKENS)
        check_section(sec, body, review.sources)
    say("")
    return review
