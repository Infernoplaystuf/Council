"""
docs_open.py — text documents read as the Ledger reads a workbook (user,
2026-10-21: "the ledger can read text documents because some labels may be
properly explained in the text documents (word, .txt etc) or they could have
data in them").

A document gives the Ledger two things:

DATA, laid out as sheets both Ledgers read:
  - "Text": its headings and short lines in order ('Part Number: 101',
    'Inspected by JB', 'Visual Inspection'), one per row;
  - "Table N": each table (a Word table, a Markdown '| a | b |' table, a
    block of tab-separated lines), with its headings and the short lines
    since the table before it above it, then a blank row, then the table -
    the same layout as a slide's table under its text box (pptx_open).

EXPLANATIONS of labels, never read as data: a line that says what a word
means ('HP — health points at full growth', 'OX: the oxygen reading taken
after the dive', 'EC means eddy current'), and the rows of a two-column
table titled like Term | Meaning. A 'Label: value' line is data when the
value is short (a few words, no sentence); a longer one is an explanation.
explanations_for() turns them into label -> field suggestions for a
template, and they go into Council's model's questions as notes.

.docx/.docm (Word's XML, standard library only), .txt/.md/.log/.text, .pdf
(pypdf, text only; it is in Council's requirements). An old .doc / .rtf
is refused with "save it as .docx". Pictures in a Word document are handed
back for the Ledger's image reader.
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

SUFFIXES = (".docx", ".docm", ".txt", ".text", ".md", ".log", ".pdf", ".doc", ".rtf")
TEXT_SUFFIXES = (".txt", ".text", ".md", ".log")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"

#: a line at most this long is data or a heading; longer is prose
SHORT_LINE = 100
#: a 'Label: value' value of at most this many words is a value; more is an
#: explanation of the label
VALUE_WORDS = 5
#: short lines above a table, at most (the text since the table before it)
LINES_ABOVE = 6
#: a document read at most this far
MAX_CHARS = 5_000_000
MAX_TABLE_CELLS = 100_000


class DocError(Exception):
    pass


@dataclass
class Explanation:
    term: str                     # 'HP'
    text: str                     # 'health points at full growth'
    source: str                   # 'notes.docx'
    line: str = ""                # the whole line, as written

    def describe(self) -> str:
        return f"{self.source}: {self.line or (self.term + ' — ' + self.text)}"


@dataclass
class DocSheet:
    name: str
    cells: Dict[Tuple[int, int], str]


@dataclass
class DocPicture:
    sheet: str
    anchor: str
    data: bytes
    fmt: str


@dataclass
class Document:
    sheets: List[DocSheet] = field(default_factory=list)
    explanations: List[Explanation] = field(default_factory=list)
    pictures: List[DocPicture] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# ── a line: data, a heading, an explanation, or prose ───────────────────
#: 'TERM means/stands for/is short for/refers to TEXT' - always an explanation
_MEANS = re.compile(r"^\s*[\"'“‘]?(?P<term>[^\"'“”‘’:=]{1,40}?)[\"'”’]?\s+"
                    r"(?:means|stands for|is short for|refers to|is the abbreviation for|"
                    r"is an abbreviation for|denotes)\s+(?P<text>.{3,})$", re.I)
#: 'TERM: TEXT', 'TERM = TEXT', 'TERM — TEXT', 'TERM - TEXT'
_SPLIT = re.compile(r"^\s*[\"'“‘]?(?P<term>[^:=—–\"'“”‘’]{1,40}?)[\"'”’]?\s*"
                    r"(?P<sep>:|=|—|–|\s-\s)\s*(?P<text>.+)$")
_BULLET = re.compile(r"^\s*(?:[-*•·▪◦]|\d{1,2}[.)])\s+")


def _clean(line: str) -> str:
    return " ".join(_BULLET.sub("", line or "").split())


def classify(line: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    """('data' | 'explanation' | 'prose' | 'empty', (term, text) for an
    explanation)."""
    s = _clean(line)
    if not s:
        return "empty", None
    m = _MEANS.match(s)
    if m and len(m.group("term").split()) <= 4:
        return "explanation", (m.group("term").strip(), m.group("text").strip())
    m = _SPLIT.match(s)
    if m and re.search(r"[A-Za-z]", m.group("term")) and len(m.group("term").split()) <= 5:
        text = m.group("text").strip()
        words = text.split()
        if re.fullmatch(r"\d{1,2}:\d{2}.*", s):            # a time, not a label
            return ("data" if len(s) <= SHORT_LINE else "prose"), None
        if m.group("sep") == ":":
            # 'Part Number: 101' is a value; 'OX: the oxygen reading taken
            # after the dive' explains OX
            sentence = len(words) > VALUE_WORDS or (text.endswith(".") and len(words) > 2)
        else:
            # a dash or '=' between a word and three or more is a definition
            # ('HP — health points'); 'Tank — North tank' is a value
            sentence = len(words) >= 3
        if sentence:
            return "explanation", (m.group("term").strip(), text.rstrip("."))
        return "data", None
    return ("data" if len(s) <= SHORT_LINE else "prose"), None


# ── reading ─────────────────────────────────────────────────────────────
def read_document(path, want_pictures: bool = True) -> Document:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".doc", ".rtf"):
        raise DocError(f"{path.name}: an old Word / rich-text file is not read - save it as "
                       ".docx in Word and add that copy")
    if suffix in (".docx", ".docm"):
        blocks, pictures = _docx_blocks(path, want_pictures)
    elif suffix == ".pdf":
        blocks, pictures = _pdf_blocks(path), []
    else:
        blocks, pictures = _text_blocks(path), []
    doc = _lay_out(path.name, blocks)
    doc.pictures = pictures
    return doc


#: a block of a document: ('heading', text) | ('line', text) | ('table', rows)
Block = Tuple[str, object]


def _text_blocks(path: Path) -> List[Block]:
    raw = path.read_bytes()[:MAX_CHARS]
    text = None
    encodings = ("utf-16",) if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else \
        ("utf-8-sig", "cp1252", "latin-1")
    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = (text or "").splitlines()
    out: List[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # a Markdown table: | a | b | rows (the |---| rule left out)
        if line.strip().startswith("|") and line.count("|") >= 3:
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if len(rows) >= 2:
                out.append(("table", rows))
            continue
        # tab-separated lines (pasted from Excel)
        if "\t" in line and line.count("\t") >= 1:
            rows = []
            while i < len(lines) and "\t" in lines[i]:
                rows.append([c.strip() for c in lines[i].split("\t")])
                i += 1
            if len(rows) >= 2:
                out.append(("table", rows))
            else:
                out.append(("line", " ".join(rows[0])))
            continue
        s = line.strip()
        if s.startswith("#"):
            out.append(("heading", s.lstrip("#").strip()))
        elif s:
            out.append(("line", s))
        i += 1
    return out


def _pdf_blocks(path: Path) -> List[Block]:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise DocError(f"{path.name}: reading a PDF needs the pypdf package "
                       "(pip install pypdf)") from e
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:                              # noqa: BLE001
                raise DocError(f"{path.name} has a password to open")
        out: List[Block] = []
        n = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            n += len(text)
            out += [("line", s.strip()) for s in text.splitlines() if s.strip()]
            if n > MAX_CHARS:
                break
        return out
    except DocError:
        raise
    except Exception as e:                                 # noqa: BLE001
        raise DocError(f"could not read {path.name}: {e}") from e


def _para_text(p: ET.Element) -> str:
    parts = []
    for node in p.iter():
        tag = node.tag
        if tag == W + "t" and node.text:
            parts.append(node.text)
        elif tag in (W + "tab",):
            parts.append("\t")
        elif tag in (W + "br", W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _heading(p: ET.Element) -> bool:
    style = p.find(f"{W}pPr/{W}pStyle")
    val = (style.get(W + "val") if style is not None else "") or ""
    return bool(re.match(r"(?i)(heading|title|subtitle|überschrift|titre)", val))


def _docx_blocks(path: Path, want_pictures: bool) -> Tuple[List[Block], List[DocPicture]]:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise DocError(f"{path.name} is not a .docx (an old .doc, or damaged): save it as "
                       ".docx in Word and add that copy") from e
    with z:
        try:
            root = ET.fromstring(z.read("word/document.xml"))
        except KeyError as e:
            raise DocError(f"{path.name} is not a Word document") from e
        body = root.find(W + "body")
        out: List[Block] = []
        for node in list(body) if body is not None else []:
            if node.tag == W + "p":
                text = _para_text(node)
                for piece in text.split("\n"):
                    piece = piece.strip()
                    if piece:
                        out.append(("heading" if _heading(node) else "line", piece))
            elif node.tag == W + "tbl":
                out.append(("table", _docx_table(node)))
        pictures: List[DocPicture] = []
        if want_pictures:
            rels = _rels(z, "word/document.xml")
            k = 0
            for blip in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip"):
                part = rels.get(blip.get(R_EMBED, ""))
                if not part:
                    continue
                try:
                    data = z.read(part)
                except KeyError:
                    continue
                k += 1
                pictures.append(DocPicture("Text", f"picture {k}", data,
                                           Path(part).suffix.lstrip(".").lower() or "png"))
    return out, pictures


def _docx_table(tbl: ET.Element) -> List[List[str]]:
    rows: List[List[str]] = []
    n = 0
    for tr in tbl.findall(W + "tr"):
        row: List[str] = []
        for tc in tr.findall(W + "tc"):
            n += 1
            if n > MAX_TABLE_CELLS:
                break
            pr = tc.find(W + "tcPr")
            vmerge = pr.find(W + "vMerge") if pr is not None else None
            text = "" if vmerge is not None and vmerge.get(W + "val") != "restart" else \
                " ".join(" ".join(_para_text(p).split()) for p in tc.findall(W + "p")).strip()
            span = pr.find(W + "gridSpan") if pr is not None else None
            row.append(text)
            if span is not None:
                row += [""] * (int(span.get(W + "val", "1")) - 1)
        rows.append(row)
    return rows


def _rels(z: zipfile.ZipFile, part: str) -> Dict[str, str]:
    folder, name = posixpath.split(part)
    try:
        root = ET.fromstring(z.read(posixpath.join(folder, "_rels", name + ".rels")))
    except KeyError:
        return {}
    out = {}
    for rel in root:
        target = rel.get("Target", "")
        if rel.get("TargetMode") == "External":
            continue
        out[rel.get("Id", "")] = posixpath.normpath(
            target[1:] if target.startswith("/") else posixpath.join(folder, target))
    return out


# ── laying a document out as sheets ─────────────────────────────────────
_GLOSSARY_HEADS = re.compile(r"(?i)^(term|terms|label|labels|abbreviation|abbrev\.?|acronym|"
                             r"code|codes|column|columns|field|symbol|header|heading|name)$")
_MEANING_HEADS = re.compile(r"(?i)^(definition|definitions|meaning|meanings|description|"
                            r"descriptions|explanation|stands for|means|what it is|notes?)$")


def _glossary_table(rows: List[List[str]]) -> bool:
    if not rows or len(rows[0]) < 2:
        return False
    head = [c.strip() for c in rows[0]]
    return bool(_GLOSSARY_HEADS.match(head[0]) and _MEANING_HEADS.match(head[1]))


def _lay_out(name: str, blocks: List[Block]) -> Document:
    doc = Document()
    text_cells: Dict[Tuple[int, int], str] = {}
    r = 1
    headings: List[str] = []
    since: List[str] = []                       # short lines since the last table
    tables = 0
    for kind, value in blocks:
        if kind == "heading":
            text = str(value)
            headings = (headings + [text])[-2:]
            text_cells[(r, 1)] = text
            r += 1
            since = []
            continue
        if kind == "line":
            text = _clean(str(value))
            what, expl = classify(text)
            if what == "explanation" and expl:
                doc.explanations.append(Explanation(expl[0], expl[1], name, text))
            elif what == "data":
                text_cells[(r, 1)] = text
                r += 1
                since = (since + [text])[-LINES_ABOVE:]
            continue
        rows = [[str(c) for c in row] for row in value]               # a table
        if _glossary_table(rows):
            for row in rows[1:]:
                if len(row) >= 2 and row[0].strip() and row[1].strip():
                    doc.explanations.append(Explanation(
                        row[0].strip(), row[1].strip(), name,
                        f"{row[0].strip()} — {row[1].strip()}"))
            continue
        tables += 1
        cells: Dict[Tuple[int, int], str] = {}
        rr = 1
        for line in headings + [s for s in since if s not in headings]:
            cells[(rr, 1)] = line
            rr += 1
        if rr > 1:
            rr += 1                                  # a blank row: headings, not headers
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                if v.strip():
                    cells[(rr + i, 1 + j)] = v.strip()
        doc.sheets.append(DocSheet(f"Table {tables}", cells))
        since = []
    if text_cells:
        doc.sheets.insert(0, DocSheet("Text", text_cells))
    return doc


# ── explanations -> fields ──────────────────────────────────────────────
def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def explanations_for(explanations: Sequence[Explanation],
                     fields: Sequence[Tuple[str, Sequence[str]]]
                     ) -> Dict[str, Tuple[str, Explanation]]:
    """Which field each explained label is: {label words: (field, the
    explanation)}. ``fields``: (field name, the ways it is written). A
    label is a field's when its explanation names that field (the longest
    name found wins; two fields named equally, none) - or, the other way
    round, when a field's own name is explained by a short quoted word
    ('Health: written "HP" in the old logs'). A label that already is a
    field's name says nothing new and is left out."""
    keys = []
    for name, ways in fields:
        for w in set(ways) | {name}:
            k = _norm(w)
            if k:
                keys.append((k, name))
    own = {k for k, _ in keys}
    out: Dict[str, Tuple[str, Explanation]] = {}
    for ex in explanations:
        term = _norm(ex.term)
        text = f" {_norm(ex.text)} "
        if not term:
            continue
        if term in own:
            # 'Health: written "HP" in the old logs'
            quoted = re.findall(r"[\"'“‘]([^\"'“”‘’]{1,30})[\"'”’]", ex.text)
            name = next(n for k, n in keys if k == term)
            for q in quoted:
                qk = _norm(q)
                if qk and qk not in own and qk not in out:
                    out[qk] = (name, ex)
            continue
        hits = [(len(k), n) for k, n in keys if f" {k} " in text]
        if not hits:
            continue
        best = max(hits)[0]
        names = {n for ln, n in hits if ln == best}
        if len(names) == 1 and term not in out:
            out[term] = (names.pop(), ex)
    return out


def notes_text(explanations: Sequence[Explanation], about: Sequence[str],
               most: int = 120, chars: int = 12000) -> str:
    """The explanations a question about these labels should see: those of
    the labels asked first, then those mentioning them, then the rest - as
    lines for a model's prompt ('' when there are none)."""
    if not explanations:
        return ""
    asked = {_norm(a) for a in about if _norm(a)}

    def rank(ex: Explanation) -> int:
        t = _norm(ex.term)
        if t in asked:
            return 0
        body = f" {_norm(ex.text)} "
        return 1 if any(f" {a} " in body for a in asked) else 2
    lines, size = [], 0
    for ex in sorted(explanations, key=rank)[:most]:
        line = f"- {ex.describe()}"
        if size + len(line) > chars:
            break
        lines.append(line)
        size += len(line)
    return ("What the user's documents say these words mean (use it to decide):\n"
            + "\n".join(lines) + "\n\n") if lines else ""
