"""
Exporting a notebook's review and study documents.

  to_html      one printable page: the review, study guide, glossary, flash
               cards, the quiz and (on a new page) its answer key, and the
               sources. The tab prints it to PDF with Qt (QPdfWriter), so no
               PDF package is needed.
  to_docx      the same as a Word document (python-docx, already one of
               Council's requirements).
  to_anki      the flash cards as a tab-separated text file Anki imports
               (File > Import), with the citation as a third field.

Citations stay as [n] with the numbered sources at the end; nothing here
reads a file or asks a model.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .review import Review, Source
from .study_docs import StudySet


def _esc(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _sources(review: Optional[Review], st: Optional[StudySet]) -> Dict[int, Source]:
    out: Dict[int, Source] = {}
    if st is not None:
        out.update(st.sources)
    if review is not None:
        for n in review.cited():
            out[n] = review.sources[n]
    return out


def _cited_numbers(review: Optional[Review], st: Optional[StudySet]) -> List[int]:
    ns = set(review.cited()) if review is not None else set()
    if st is not None:
        for kind in ("cards", "quiz", "guide", "glossary"):
            ns |= set(st.cited(kind))
    return sorted(ns)


def title_of(review: Optional[Review], st: Optional[StudySet]) -> str:
    return (review.title if review is not None else
            st.title if st is not None else "Study notes")


def to_html(review: Optional[Review], st: Optional[StudySet]) -> str:
    """The whole notebook's study pack as one HTML page (citations as [n])."""
    e = _esc
    out = [f"<html><head><meta charset='utf-8'><title>{e(title_of(review, st))}</title>"
           f"</head><body style='font-family:sans-serif'>"
           f"<h1>{e(title_of(review, st))}</h1>"]
    if review is not None:
        out.append("<h2>Literature review</h2>")
        for sec in review.sections:
            out.append(f"<h3>{e(sec.heading)}</h3><p>" + " ".join(
                e(s.text) + "".join(f"[{n}]" for n in s.cites)
                for s in sec.sentences) + "</p>")
    if st is not None:
        if st.guide:
            out.append("<h2>Study guide</h2>")
            for g in st.guide:
                out.append(f"<h3>{e(g['heading'])}</h3><ul>" + "".join(
                    f"<li>{e(p['text'])} [{p['source']}]</li>" for p in g["points"])
                    + "</ul>")
        if st.glossary:
            out.append("<h2>Glossary</h2><dl>" + "".join(
                f"<dt><b>{e(t['term'])}</b></dt><dd>{e(t['definition'])} "
                f"[{t['source']}]</dd>"
                for t in sorted(st.glossary, key=lambda t: t["term"].lower())) + "</dl>")
        if st.cards:
            out.append("<h2>Flash cards</h2><table border='1' cellpadding='6' "
                       "cellspacing='0' width='100%'><tr><th>Front</th><th>Back</th></tr>")
            out += [f"<tr><td>{e(c['front'])}</td><td>{e(c['back'])} [{c['source']}]"
                    f"</td></tr>" for c in st.cards]
            out.append("</table>")
        if st.quiz:
            out.append("<h2>Quiz</h2>")
            for i, q in enumerate(st.quiz, 1):
                out.append(f"<p><b>{i}. {e(q['question'])}</b></p>")
                if q.get("choices"):
                    out.append("<ol type='A'>" + "".join(
                        f"<li>{e(c)}</li>" for c in q["choices"]) + "</ol>")
                else:
                    out.append("<p>_______________________________</p>")
            out.append("<h2 style='page-break-before:always'>Answer key</h2><ol>")
            for q in st.quiz:
                letter = ("ABCD"[q["choices"].index(q["answer"])] + ". "
                          if q.get("choices") else "")
                why = f" {e(q['explanation'])}" if q.get("explanation") else ""
                out.append(f"<li>{letter}{e(q['answer'])}.{why} [{q['source']}]</li>")
            out.append("</ol>")
    srcs = _sources(review, st)
    nums = [n for n in _cited_numbers(review, st) if n in srcs]
    if nums:
        out.append("<h2>Sources</h2><ol>" + "".join(
            f"<li value='{n}'>{e(srcs[n].cite())}</li>" for n in nums) + "</ol>")
    out.append("</body></html>")
    return "".join(out)


def to_docx(path: Any, review: Optional[Review], st: Optional[StudySet]) -> Path:
    """The study pack as a Word document at ``path``."""
    import docx
    from docx.enum.text import WD_BREAK

    d = docx.Document()
    d.add_heading(title_of(review, st), 0)
    if review is not None:
        d.add_heading("Literature review", 1)
        for sec in review.sections:
            d.add_heading(sec.heading, 2)
            d.add_paragraph(" ".join(s.text + "".join(f"[{n}]" for n in s.cites)
                                     for s in sec.sentences))
    if st is not None:
        if st.guide:
            d.add_heading("Study guide", 1)
            for g in st.guide:
                d.add_heading(g["heading"], 2)
                for p in g["points"]:
                    d.add_paragraph(f"{p['text']} [{p['source']}]", style="List Bullet")
        if st.glossary:
            d.add_heading("Glossary", 1)
            for t in sorted(st.glossary, key=lambda t: t["term"].lower()):
                para = d.add_paragraph()
                para.add_run(t["term"]).bold = True
                para.add_run(f": {t['definition']} [{t['source']}]")
        if st.cards:
            d.add_heading("Flash cards", 1)
            table = d.add_table(rows=1, cols=2)
            table.style = "Table Grid"
            table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Front", "Back"
            for c in st.cards:
                row = table.add_row().cells
                row[0].text = c["front"]
                row[1].text = f"{c['back']} [{c['source']}]"
        if st.quiz:
            d.add_heading("Quiz", 1)
            for i, q in enumerate(st.quiz, 1):
                d.add_paragraph().add_run(f"{i}. {q['question']}").bold = True
                for letter, c in zip("ABCD", q.get("choices") or []):
                    d.add_paragraph(f"{letter}. {c}")
                if not q.get("choices"):
                    d.add_paragraph("_" * 40)
            d.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            d.add_heading("Answer key", 1)
            for i, q in enumerate(st.quiz, 1):
                letter = ("ABCD"[q["choices"].index(q["answer"])] + ". "
                          if q.get("choices") else "")
                why = f" {q['explanation']}" if q.get("explanation") else ""
                d.add_paragraph(f"{i}. {letter}{q['answer']}.{why} [{q['source']}]")
    srcs = _sources(review, st)
    nums = [n for n in _cited_numbers(review, st) if n in srcs]
    if nums:
        d.add_heading("Sources", 1)
        for n in nums:
            d.add_paragraph(f"[{n}] {srcs[n].cite()}")
    path = Path(path)
    d.save(str(path))
    return path


def anki_text(st: StudySet) -> str:
    """The flash cards as Anki's tab-separated import: front, back, source."""
    buf = io.StringIO()
    buf.write("#separator:tab\n#html:false\n#columns:Front\tBack\tSource\n")
    writer = csv.writer(buf, delimiter="\t", lineterminator="\n")
    for c in st.cards:
        src = st.sources.get(c["source"])
        writer.writerow([c["front"], c["back"], src.cite() if src else ""])
    return buf.getvalue()


def to_anki(path: Any, st: StudySet) -> Path:
    path = Path(path)
    path.write_text(anki_text(st), encoding="utf-8")
    return path
