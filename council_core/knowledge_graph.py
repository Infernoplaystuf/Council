"""The knowledge graph: people, parts and projects across the vault documents.

One embedded SQLite file, ``<vault>/.knowledge_graph/graph.sqlite``. The
dot-folder keeps it (and its exports) out of every vault content search:
vault_index skips dot-folders and conversation_logger.PROTECTED_SUBDIRS lists
it for the walks that do not. Nothing here talks to a network or a model; free
text extraction by a model (KG2) is a later stage that only ADDS "suggested"
links through the same store.

What is in the store (schema version ``SCHEMA_VERSION``, documented in
``SCHEMA_SQL``):

  documents  one row per file seen: a stable UUID, the vault-relative path,
             the content hash it was read at.
  entities   PERSON / PART / PROJECT / DOCUMENT. The id is a hash of type +
             normalised key — never a name or a path — so it is the same on
             every rebuild and on every machine.
  aliases    every surface form an entity was seen as ('Lee, Carol').
  mentions   where an entity appears: document + locator (sheet/row/column,
             page/line) + snippet + how it was found.
  relations  subject -predicate-> object with a status:
               seeded    read from labelled fields / Collections, no model;
               suggested needs the user (a guess: an initial, a whole-document
                         co-occurrence with several projects, later a model);
               accepted / rejected  the user's decision — never changed by a
                         rebuild.
  evidence   why a relation is believed: document + content hash + locator +
             quote + method (row / record / document / collection) + run id.
  review     open questions for the user: an ambiguous name ('D. Whitfield'
             — Dana or Dan?), an inferred alias to confirm.
  decisions  the user's accept / reject / merge decisions, append-only.
  field_rules which labels mean a person, part or project, and how a labelled
             value links to the others in the same record. Defaults are
             PROPOSED until the user confirms them; drifted labels ('POC')
             are suggested by field_search.field_name_candidates, never
             assumed.
  name_decisions (v2) the user's answers ('D. Whitfield' is Dana — here or
             everywhere — or neither), keyed by person_key of the name.
  extraction_done (v2) which passages a model has read, per document
             version and per model, so a stopped run resumes.
  extraction_docs (v3) each document's passages and the signature they
             were worked out from, so a resumed run skips a finished
             document without reading it.
  doc_cache  (v3) what reading each document gave (labelled records, text
             lines) at its content hash: a rebuild re-reads only changed
             documents, and its reading phase commits in batches so a
             stopped rebuild resumes. Never exported.

Seeding reads "records": a table row, a JSON object, or one document's labelled
header fields. Two values in one row are a strong link (seeded); values that
only share a document are seeded when the document names one project and
suggested when it names several. Everything is cited: no fact without a file
and a place in it.

Spreadsheets (council_core/kg_excel.py, with the Ledger's own readers): a
Ledger's parent CSV gives firm links by its schema's column roles (who
INSPECTED which part, and the project when a confirmed rule names its
column), cited at the CSV row and the source cell the Ledger's review kept;
a workbook is read sheet by sheet (headers on any row, several tables,
labelled cells, text boxes, charts, pictures through Tesseract, Excel-only
files through Excel), and what only the Ledger's reading says stays
suggested. An inspector's initials are resolved through the Ledger's people
list, the user's answers and confirmed names - never guessed; others are
asked once. Folders outside the vault may be added (add_root): only read,
their documents stored as '<root id>/path'.

No pickle. Writes are SQLite transactions (atomic). A damaged store is
reported (KnowledgeGraphDamaged), never silently replaced.
"""
from __future__ import annotations

import contextvars
import csv
import hashlib
import io
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 3
STORE_DIR = ".knowledge_graph"
STORE_NAME = "graph.sqlite"

ENTITY_TYPES = ("PERSON", "PART", "PROJECT", "DOCUMENT")
#: INSPECTED (person -> part) comes only from the Ledger's columns and
#: reader (council_core/kg_excel.py): who signed a part's readings. A model
#: is never asked for it (kg_extract.SIGNATURES does not list it).
PREDICATES = ("LEADS", "CONTACT_FOR", "WORKS_ON", "OWNS", "USES_PART",
              "SUPERSEDES", "DOCUMENTED_IN", "INSPECTED")
STATUSES = ("seeded", "suggested", "accepted", "rejected")
#: Inside one seed() transaction only: a derived link not yet re-supported by
#: this run's evidence. Never committed (seed resolves or deletes every one).
_PENDING = "pending"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,               -- UUID, stable for a path
    path TEXT NOT NULL UNIQUE,         -- relative to the scanned root, '/'
    content_hash TEXT NOT NULL,        -- sha256 of the bytes last read
    size INTEGER, mtime REAL, scanned_ts REAL,
    status TEXT NOT NULL DEFAULT 'ok'  -- ok | unreadable | missing
);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,               -- sha256(type|key)[:20]
    type TEXT NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL,
    merged_into TEXT,                  -- set by a user merge (KG3)
    created_ts REAL NOT NULL,
    UNIQUE (type, key)
);
CREATE TABLE IF NOT EXISTS aliases (
    entity_id TEXT NOT NULL, alias TEXT NOT NULL, alias_norm TEXT NOT NULL,
    source TEXT NOT NULL,              -- field | collection | inferred | user
    PRIMARY KEY (entity_id, alias)
);
CREATE INDEX IF NOT EXISTS aliases_norm ON aliases(alias_norm);
CREATE TABLE IF NOT EXISTS mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id TEXT NOT NULL, document_id TEXT NOT NULL,
    locator TEXT NOT NULL,             -- JSON: sheet,row,column | page,line
    surface TEXT NOT NULL, snippet TEXT NOT NULL,
    method TEXT NOT NULL,              -- field | gazetteer | collection
    run_id TEXT NOT NULL,
    UNIQUE (entity_id, document_id, locator, method)
);
CREATE INDEX IF NOT EXISTS mentions_entity ON mentions(entity_id);
-- v3: the free-text pass asks for one document's mentions at a time; without
-- this every ask was a full scan (MEASURED: 84 of 118 s for 3,000 documents).
CREATE INDEX IF NOT EXISTS mentions_document ON mentions(document_id);
CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,               -- sha256(subject|predicate|object)[:20]
    subject_id TEXT NOT NULL, predicate TEXT NOT NULL, object_id TEXT NOT NULL,
    status TEXT NOT NULL, created_ts REAL NOT NULL, updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS relations_subject ON relations(subject_id);
CREATE INDEX IF NOT EXISTS relations_object ON relations(object_id);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id TEXT NOT NULL, document_id TEXT NOT NULL,
    content_hash TEXT NOT NULL, locator TEXT NOT NULL, quote TEXT NOT NULL,
    method TEXT NOT NULL,              -- row | record | document | collection | model
    model TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL,
    UNIQUE (relation_id, document_id, locator, method)
);
CREATE INDEX IF NOT EXISTS evidence_relation ON evidence(relation_id);
CREATE TABLE IF NOT EXISTS review (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                -- ambiguous_name | inferred_alias
    surface TEXT NOT NULL, candidates TEXT NOT NULL,   -- JSON entity ids
    document_id TEXT, locator TEXT, snippet TEXT,
    status TEXT NOT NULL DEFAULT 'open',               -- open | resolved
    run_id TEXT NOT NULL,
    UNIQUE (kind, surface, document_id, locator)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS field_rules (
    label TEXT PRIMARY KEY,
    rule TEXT NOT NULL,                -- JSON FieldRule
    status TEXT NOT NULL               -- proposed | confirmed | rejected
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, started_ts REAL NOT NULL,
    finished_ts REAL, stats TEXT
);
-- v2: the user's answers about names ('D. Whitfield' is Dana - here, or
-- everywhere; or neither). entity_id '' = a different, separate person.
CREATE TABLE IF NOT EXISTS name_decisions (
    surface_norm TEXT NOT NULL, document_id TEXT NOT NULL,   -- '' = everywhere
    entity_id TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (surface_norm, document_id)
);
-- v2: free-text extraction progress, so a stopped run resumes and an edited
-- document is read again (content_hash differs).
CREATE TABLE IF NOT EXISTS extraction_done (
    document_id TEXT NOT NULL, content_hash TEXT NOT NULL, chunk INTEGER NOT NULL,
    model TEXT NOT NULL, ts REAL NOT NULL, links INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (document_id, content_hash, chunk, model)
);
-- v3: one row per text document the free-text pass worked out: the
-- signature of everything its passages depend on (its content, its
-- mentions, its Collection, the project names) and the passage numbers that
-- went to a model. Same signature + every passage done = nothing to read.
CREATE TABLE IF NOT EXISTS extraction_docs (
    document_id TEXT PRIMARY KEY, sig TEXT NOT NULL, chunks TEXT NOT NULL
);
-- v3: what reading a document gave (its labelled records and its text lines),
-- so a rebuild reads only documents whose content changed and a stopped
-- rebuild resumes. reader_sig covers the confirmed field rules and the
-- reading code: either changing reads every document again. A row goes when
-- its document is deleted or can no longer be read (it holds the text).
CREATE TABLE IF NOT EXISTS doc_cache (
    document_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
    reader_sig TEXT NOT NULL, records TEXT NOT NULL, lines TEXT NOT NULL,
    ts REAL NOT NULL
);
"""


class KnowledgeGraphDamaged(RuntimeError):
    """The store exists but cannot be read. Reported, never replaced."""


class _GraphUnchanged(Exception):
    """Inside seed(): nothing the graph is worked out from changed."""


class KnowledgeGraphBusy(RuntimeError):
    """Another connection holds the store (a rebuild's one transaction):
    try again when it finishes. Not damage: it used to be reported as
    "could not be read (database is locked)" (review, 2026-10-16)."""


def _busy(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        w in str(exc).lower() for w in ("locked", "busy"))


# ── field rules ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FieldRule:
    """What a labelled field means.

    ``links`` (PERSON only): ``[(target_type, predicate), ...]`` — the person
    links to the FIRST target type present in the same record. 'Owner' next
    to a part owns the part; next to only a project, leads the project.
    ``alias_of``: the value is another name for the record's entity of this
    type ('Project Name' beside 'Project ID').
    ``supersedes``: PART — the record's main part supersedes this value."""
    label: str
    type: str
    links: Tuple[Tuple[str, str], ...] = ()
    alias_of: bool = False
    supersedes: bool = False

    def to_json(self) -> str:
        return json.dumps({"label": self.label, "type": self.type,
                           "links": [list(x) for x in self.links],
                           "alias_of": self.alias_of,
                           "supersedes": self.supersedes})

    @staticmethod
    def from_json(s: str) -> "FieldRule":
        d = json.loads(s)
        return FieldRule(label=d["label"], type=d["type"],
                         links=tuple(tuple(x) for x in d.get("links", [])),
                         alias_of=bool(d.get("alias_of")),
                         supersedes=bool(d.get("supersedes")))

    def renamed(self, label: str) -> "FieldRule":
        return FieldRule(label, self.type, self.links, self.alias_of,
                         self.supersedes)


_TO_PROJECT_OR_PART = (("PART", "CONTACT_FOR"), ("PROJECT", "CONTACT_FOR"))
DEFAULT_FIELD_RULES: Tuple[FieldRule, ...] = (
    FieldRule("Project ID", "PROJECT"),
    FieldRule("Project", "PROJECT"),
    FieldRule("Program", "PROJECT"),
    FieldRule("Project Name", "PROJECT", alias_of=True),
    FieldRule("P/N", "PART"),
    FieldRule("Part Number", "PART"),
    FieldRule("Part", "PART"),
    FieldRule("Supersedes", "PART", supersedes=True),
    FieldRule("Program Lead", "PERSON", (("PROJECT", "LEADS"),)),
    FieldRule("Project Lead", "PERSON", (("PROJECT", "LEADS"),)),
    FieldRule("Point of Contact", "PERSON", _TO_PROJECT_OR_PART),
    FieldRule("Owner", "PERSON", (("PART", "OWNS"), ("PROJECT", "LEADS"))),
    FieldRule("Originator", "PERSON", (("PROJECT", "WORKS_ON"),)),
    FieldRule("Approved by", "PERSON", (("PROJECT", "WORKS_ON"),)),
    FieldRule("Attendees", "PERSON", (("PROJECT", "WORKS_ON"),)),
)

#: Built-in readings, not rules the user ticks: what a Ledger's schema, or
#: the Ledger's own reader, says a column holds (council_core/kg_excel.py).
#: Bracketed labels can never be a label in a document, so no document's
#: column is read by them by accident; they are never stored in field_rules.
LEDGER_PART = FieldRule("[Ledger sample ID]", "PART")
INSPECTOR = FieldRule("[inspector]", "PERSON",
                      (("PART", "INSPECTED"), ("PROJECT", "WORKS_ON")))
SHEET_PART = FieldRule("[part named by its sheet]", "PART")
BUILTIN_RULES: Dict[str, FieldRule] = {r.label: r for r in
                                       (LEDGER_PART, INSPECTOR, SHEET_PART)}


# ── normalisation and ids ────────────────────────────────────────────────
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "pe", "md", "esq"}
_CODE_RE = re.compile(r"^[A-Za-z]{1,8}[-_ ]?\d[\w\-/.]*$")


def fold(s: Any) -> str:
    """Lower-case, accents removed, non-alphanumerics -> single spaces.
    'Tomás Echeverría' and 'Tomas Echeverria' fold to the same key."""
    t = unicodedata.normalize("NFKD", str(s or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def person_display(raw: str) -> str:
    """'Lee, Carol' -> 'Carol Lee'; 'Bob Smith, Jr.' stays; whitespace tidied."""
    s = " ".join(str(raw or "").split())
    if "," in s:
        left, right = [x.strip() for x in s.split(",", 1)]
        if fold(right) in _SUFFIXES or not right:
            return s
        if len(left.split()) == 1 and 1 <= len(right.split()) <= 2:
            return f"{right} {left}"
    return s


def person_key(raw: str) -> str:
    toks = [t for t in fold(person_display(raw)).split() if t not in _SUFFIXES]
    return " ".join(toks)


def _name_key(surface: str) -> str:
    """How a name answer is filed: 'D. Whitfield' and 'Whitfield, D.' are one
    question (person_key reads both as 'd whitfield')."""
    return person_key(surface) or fold(surface)


def is_initial_form(key: str) -> bool:
    """'d whitfield' — a first initial and a surname."""
    toks = key.split()
    return len(toks) == 2 and len(toks[0]) == 1


def code_key(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw or "")).upper()


def looks_like_code(raw: str) -> bool:
    return bool(_CODE_RE.match(str(raw or "").strip()))


def entity_key(etype: str, raw: str) -> str:
    if etype == "PERSON":
        return person_key(raw)
    if etype == "PART":
        return code_key(raw)
    if etype == "PROJECT":
        return code_key(raw) if looks_like_code(raw) else fold(raw)
    return str(raw)


def entity_id(etype: str, key: str) -> str:
    return hashlib.sha256(f"{etype}|{key}".encode("utf-8")).hexdigest()[:20]


def relation_id(subject_id: str, predicate: str, object_id: str) -> str:
    return hashlib.sha256(f"{subject_id}|{predicate}|{object_id}"
                          .encode("utf-8")).hexdigest()[:20]


def _file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


#: What a locator may say. A workbook adds the cell ('B7'), the cells a row's
#: link rests on, the table's header row and a picture's anchor; a Ledger row
#: adds its flaw ('item') and where the Ledger says the value came from
#: ('from': 'nde.xlsx › Part 101!C4', or 'sources': the row's workbooks).
_LOC_KEYS = ("sheet", "row", "column", "columns", "page", "line", "cell", "cells",
             "header", "image", "item", "from", "sources")


def _loc_json(loc: Dict[str, Any]) -> str:
    keep = {k: loc[k] for k in _LOC_KEYS if loc.get(k) is not None}
    return json.dumps(keep, sort_keys=True, ensure_ascii=False)


def locator_text(loc: Any) -> str:
    """A locator as a person reads it: 'sheet Projects, row 2 (Program Lead)',
    'page 1, line 5', 'line 4', 'sheet Build, cell B4 (Freq (kHz))',
    'row 5, Flaw # 1A (EC Pre [JB]) — from nde.xlsx › Part 101!C4'."""
    if isinstance(loc, str):
        try:
            loc = json.loads(loc)
        except Exception:
            return loc
    loc = loc or {}
    bits = []
    # A workbook's free-text line: its "page" is the sheet, its "line" the row.
    sheet_page = isinstance(loc.get("page"), str)
    if loc.get("sheet"):
        bits.append(f"sheet {loc['sheet']}")
    if loc.get("page"):
        bits.append(f"sheet {loc['page']}" if sheet_page else f"page {loc['page']}")
    if loc.get("image"):
        bits.append(f"picture at {loc['image']}")
    if loc.get("row"):
        bits.append(f"row {loc['row']}")
    elif loc.get("cell"):
        bits.append(f"cell {loc['cell']}")
    if loc.get("item"):
        bits.append(str(loc["item"]))
    if loc.get("line"):
        bits.append(f"row {loc['line']}" if sheet_page else f"line {loc['line']}")
    s = ", ".join(bits) or "whole document"
    cols = loc.get("columns") or ([loc["column"]] if loc.get("column") else [])
    if cols:
        s += f" ({', '.join(cols)})"
    if loc.get("from"):
        s += f" — from {loc['from']}"
    elif loc.get("sources"):
        s += f" — source workbook(s): {loc['sources']}"
    return s


# ── reading records out of documents ─────────────────────────────────────
@dataclass(slots=True)        # a 100k-row workbook holds ~300,000 of these
class Value:
    rule: FieldRule
    raw: str
    locator: Dict[str, Any]
    snippet: str
    #: Only the Ledger's reading says it (an 'Inspector' column, a part
    #: named by its sheet, a picture read by OCR): its links stay suggested.
    tentative: bool = False
    #: A person value from a Ledger or a workbook: bare initials ('JB') in it
    #: go through the Ledger's people list and the name questions, whatever
    #: the column ('Owner' as much as 'Inspector'). A CSV's or a text
    #: document's people are read as they always were.
    signed: bool = False


@dataclass(slots=True)
class Record:
    path: str                    # relative to the scanned root
    kind: str                    # row | record | document | skipped |
    #                              ledger (a parent-CSV row) | sheet (a sheet's
    #                              labelled cells) | picture (a table read by
    #                              OCR) | reading (see below)
    values: List[Value] = field(default_factory=list)
    #: kind 'skipped': a table row that could not be read (more cells than
    #: the header), reported in the run's coverage instead of vanishing.
    skipped_row: int = 0
    #: kind 'reading', last in what read_records gives for a workbook or a
    #: Ledger: the kg_excel.Reading (its text lines, labels, summary, notes),
    #: so one read gives both. seed() takes it off; it is never a record.
    reading: Any = None


#: Tables read as rows: a CSV by its header; a workbook by the Ledger's
#: reader (council_core/kg_excel.py), which also gives its text lines.
_CSV = {".csv", ".tsv"}
_WORKBOOK = {".xlsx", ".xlsm", ".xls", ".xlsb"}
_TABULAR = _CSV | _WORKBOOK
_TEXT = {".txt", ".md", ".markdown", ".rst", ".log", ".pdf", ".docx",
         ".html", ".htm", ".xml", ".yaml", ".yml", ".ini", ".cfg"}
_JSON = {".json"}
SUPPORTED_SUFFIXES = _TABULAR | _TEXT | _JSON
#: Kinds of record whose links rest on one row's cells.
_ROW_KINDS = ("row", "picture", "ledger")


def _rule_index(rules: Sequence[FieldRule]):
    import field_search as fs
    return {fs._norm_key(r.label): r for r in rules}


def _table_records(p: Path, rel: str, rules: Sequence[FieldRule]) -> List[Record]:
    """One Record per non-empty row, per sheet. A column belongs to a rule
    only when its header IS the label (after case/format normalisation) — a
    loose match would make 'Project Name' a second project column."""
    import field_search as fs
    by_norm = _rule_index(rules)
    out: List[Record] = []
    # strict: a table that cannot be read raises, so seed() lists it as
    # unreadable instead of counting it as a document with no facts.
    for sheet, frame in fs._table_frames(p, strict=True):
        for rowno in frame.attrs.get("skipped_rows", []):
            out.append(Record(rel, "skipped", [], skipped_row=rowno))
        cols = [(c, by_norm.get(fs._norm_key(c))) for c in frame.columns]
        cols = [(c, r) for c, r in cols if r is not None]
        if not cols:
            continue
        # The index is the row a person sees (blank and bad lines counted).
        for rowno, row in zip(frame.index, frame.itertuples(index=False, name=None)):
            rowd = dict(zip(frame.columns, row))
            rec = Record(rel, "row")
            for c, r in cols:
                raw = rowd.get(c)
                if raw is None or (isinstance(raw, float) and raw != raw):
                    continue
                raw = str(raw).strip()
                if not raw or raw.lower() == "nan":
                    continue
                loc = {"row": int(rowno), "column": str(c)}
                if sheet is not None:
                    loc["sheet"] = sheet
                for one in (fs._split_values(raw, r.label, r.type) or [raw]):
                    rec.values.append(Value(r, one, loc, raw))
            if rec.values:
                out.append(rec)
    return out


def _json_records(p: Path, rel: str, rules: Sequence[FieldRule]) -> List[Record]:
    """One Record per JSON object that has at least one labelled key."""
    import field_search as fs
    by_norm = _rule_index(rules)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        data = json.loads(text)
    except Exception:
        return []
    lines = text.splitlines()
    out: List[Record] = []
    cursor = [0]

    def walk(o):
        if isinstance(o, dict):
            rec = Record(rel, "record")
            for k, v in o.items():
                r = by_norm.get(fs._norm_key(k))
                if r is None or isinstance(v, (dict, list)) or v is None:
                    continue
                raw = str(v).strip()
                if not raw:
                    continue
                # The key and the value together: the bare value 'PN-1234'
                # was found inside the earlier '"Part": "PN-1234/A"' line.
                i, end = fs.json_pair_line(text, k, v, cursor[0])
                if i >= 0:
                    cursor[0] = end
                else:
                    i = fs._line_of(lines, json.dumps(raw, ensure_ascii=False)[1:-1])
                loc = {"line": i + 1} if i >= 0 else {}
                snip = fs._snippet(lines[i]) if i >= 0 else raw
                for one in (fs._split_values(raw, r.label, r.type) or [raw]):
                    rec.values.append(Value(r, one, loc, snip))
            if rec.values:
                out.append(rec)
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return out


def _text_records(p: Path, rel: str, rules: Sequence[FieldRule]) -> List[Record]:
    """The labelled fields of one text-like document (md/txt/pdf/docx) as ONE
    Record. A line's key must BE the rule's label, as a table header must
    (exact=True): 'Project Manager: Ann Stone' is not a project, 'Part Qty: 4'
    not a part. When two rules match the same value on the same line, the
    longer label wins."""
    import field_search as fs
    best: Dict[tuple, Value] = {}
    # Read ONCE for every rule: field_value_locations read and parsed the
    # file again per rule — MEASURED 16 reads of each PDF per rebuild.
    pages = fs.document_pages(p)
    for r in rules:
        for loc in fs.field_value_locations(p, r.label, kind=r.type, exact=True,
                                            pages=pages):
            key = (loc.get("page"), loc.get("line"), loc["value"])
            prev = best.get(key)
            if prev is None or len(r.label) > len(prev.rule.label):
                best[key] = Value(r, loc["value"], loc, loc.get("snippet", ""))
    if not best:
        return []
    vals = sorted(best.values(), key=lambda v: (v.locator.get("page") or 0,
                                                v.locator.get("line") or 0))
    return [Record(rel, "document", vals)]


def missing_reader(p: Path) -> str:
    """Why this file cannot be read on this install, or ''. A file whose
    reader library is missing would otherwise read as 'no facts' — the
    coverage line must say it was not read at all (this desktop's council
    env has no openpyxl or pypdf)."""
    import importlib.util as iu
    suf = p.suffix.lower()
    if suf in _WORKBOOK:
        from council_core import kg_excel
        return kg_excel.missing_reader(p)
    need = {".pdf": "pypdf"}.get(suf)
    if need and iu.find_spec(need) is None:
        return f"needs the {need} package, which is not installed"
    return ""


#: The image reader for the workbook being read, set by seed() for its own
#: thread (a context variable: the Connections tab's rebuild worker and a
#: label harvest on another thread never see each other's), so read_records
#: keeps its three-argument call.
_IMAGE_READER: "contextvars.ContextVar[Any]" = contextvars.ContextVar(
    "kg_image_reader", default=None)


def _image_reader_now():
    return _IMAGE_READER.get()


#: The terminal progress of the rebuild in progress on this thread
#: (progress_report.Progress), for the readers to say what they are on.
_PROGRESS: "contextvars.ContextVar[Any]" = contextvars.ContextVar(
    "kg_progress", default=None)


def progress_now():
    """The running rebuild's Progress, or None."""
    return _PROGRESS.get()


#: Set by seed()'s reading phase: workbook and Ledger readers then give
#: their records as cache lines, not objects (kg_excel.Reading).
_AS_LINES: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "kg_records_as_lines", default=False)


def records_as_lines_now() -> bool:
    return _AS_LINES.get()


def read_records(p: Path, rel: str, rules: Sequence[FieldRule]) -> List[Record]:
    """What a document says, as records. A workbook or a Ledger (a parent
    CSV with its schema) is read by council_core/kg_excel.py, and its
    Record(kind='reading') comes last: the text lines, labels and summary
    of the same read."""
    suf = p.suffix.lower()
    if suf in _WORKBOOK or (suf == ".csv" and _is_ledger(p)):
        from council_core import kg_excel
        reading = (kg_excel.read_workbook(p, rel, rules, _image_reader_now())
                   if suf in _WORKBOOK else kg_excel.read_ledger(p, rel, rules))
        return list(reading.records) + [Record(rel, "reading", reading=reading)]
    if suf in _TABULAR:
        return _table_records(p, rel, rules)
    if suf in _JSON:
        return _json_records(p, rel, rules)
    return _text_records(p, rel, rules)


def _is_ledger(p: Path) -> bool:
    from council_core import kg_excel
    return kg_excel.is_ledger_csv(p)


def _signed_initials(v: Value) -> bool:
    """Bare initials ('JB'), not a written name, in an inspector's column -
    or in any person column of a Ledger or a workbook (capitals there: 'Owner
    | MK'). Those are resolved through the Ledger's people list and the name
    questions, never taken as a person called 'MK' (review, 2026-10-16)."""
    inspector = v.rule.label == INSPECTOR.label
    if not (inspector or v.signed):
        return False
    from council_core import kg_excel
    return kg_excel.is_initials(v.raw, loose=inspector)


def _ledger_people(csv_path: Path) -> Dict[str, str]:
    """The Ledger's people list beside a parent CSV: {'JB': 'Jordan Blake'}."""
    try:
        import source_extract
        return source_extract.load_people(csv_path)
    except Exception:
        return {}


def _plain_path(path: Any) -> str:
    r"""'\\?\C:\x' -> 'C:\x', '\\?\UNC\srv\s' -> '\\srv\s'."""
    s = str(path)
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


def _real(path: Any) -> str:
    import os
    return os.path.normcase(os.path.realpath(_plain_path(os.path.expanduser(str(path)))))


def _overlaps(a: Any, b: Any, inside_only: bool = False) -> bool:
    """One path inside the other (or the same), as Windows compares them;
    ``inside_only``: ``a`` inside (or at) ``b``."""
    import os
    x, y = _real(a), _real(b)
    try:
        common = os.path.commonpath([x, y])
    except ValueError:                 # different drives
        return False
    return common == y or (not inside_only and common == x)


def _is_link(path: str) -> bool:
    import os
    import stat
    try:
        st = os.lstat(path)
    except OSError:
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return stat.S_ISLNK(st.st_mode) or bool(attrs & 0x400)       # REPARSE_POINT


def _skip_dir(path: str, name: str) -> bool:
    import os
    import stat
    if name.startswith(".") or name.lower() in ("$recycle.bin", "system volume information",
                                                "recycler", "$windows.~bt", "$windows.~ws"):
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return stat.S_ISLNK(st.st_mode) or bool(attrs & (0x400 | 0x4))   # reparse, system


def _excel_available() -> bool:
    try:
        import excel_open
        return bool(excel_open.excel_available())
    except Exception:
        return False


#: Part of every doc_cache signature. The reading code's own bytes are in the
#: signature too (see _reader_sig), so this needs bumping only for a change
#: elsewhere that alters what a document reads as.
READER_VERSION = 2

#: The modules whose code decides what a document reads as.
_READER_MODULES = ("field_search", "vault_rag", "source_extract", "excel_open",
                   "sample_ledger", "council_core.kg_excel")


def _reader_sig(rules: Sequence[FieldRule], *, image_reader: str = "",
                excel: bool = False) -> str:
    """What a cached reading depends on besides the document's bytes: the
    confirmed rules, the reading code (this module, field_search, vault_rag
    and the Ledger's readers), the reader libraries' versions, and what this
    PC can read pictures and Excel-only workbooks with. Any change = read
    again: a cache that outlived a parser fix would keep the old answers for
    good, and a picture first seen without Tesseract would never be read."""
    import importlib.util as iu
    h = hashlib.sha256(f"reader {READER_VERSION}".encode("utf-8"))
    for r in sorted(rules, key=lambda r: r.label):
        h.update(r.to_json().encode("utf-8"))
    for mod in _READER_MODULES:
        try:
            spec = iu.find_spec(mod)
            h.update(Path(spec.origin).read_bytes() if spec and spec.origin else b"-")
        except Exception:
            h.update(b"?")
    h.update(Path(__file__).read_bytes())
    h.update(f"images={image_reader}|excel={bool(excel)}".encode("utf-8"))
    try:
        from importlib import metadata as md
        for lib in ("pandas", "openpyxl", "pypdf", "python-docx", "xlrd", "pywin32"):
            try:
                h.update(f"{lib}={md.version(lib)}".encode("utf-8"))
            except md.PackageNotFoundError:
                h.update(f"{lib}=-".encode("utf-8"))
    except Exception:
        h.update(b"no-metadata")
    return h.hexdigest()


def _values_json(r: Record) -> List[list]:
    """[label, raw, locator, snippet] + flags when any: 1 tentative, 2 signed."""
    out = []
    for v in r.values:
        flags = (1 if v.tentative else 0) | (2 if v.signed else 0)
        out.append([v.rule.label, v.raw, v.locator, v.snippet] + ([flags] if flags else []))
    return out


#: The first line of a workbook's or a Ledger's cached records: its
#: reading's summary, then one record per line - so the Spreadsheets list
#: reads one line, and a rebuild one record at a time. MEASURED (review,
#: 2026-10-16): sources() parsed a 100k-row workbook's whole cache on the
#: GUI thread (1.8 s), and a rebuild held every record of it (805 MB).
_JSONL_HEAD = '{"v": 3'


def _record_json(r: Record) -> str:
    return json.dumps({"kind": r.kind, "skipped_row": r.skipped_row,
                       "values": _values_json(r)}, ensure_ascii=False)


def _records_json(records: Sequence[Record], reading: Any = None,
                  error: str = "") -> str:
    """A document's records for doc_cache: a JSON list - or, for a workbook
    or Ledger, its reading's summary, labels, notes (and why it can never be
    read, ``error``) on a first line and one record per line after it."""
    recs = [r for r in records if r.kind != "reading"]
    if reading is None and not error:
        return "[" + ",".join(_record_json(r) for r in recs) + "]"
    body = getattr(reading, "record_lines", None)
    head = json.dumps({"v": 3, "error": error,
                       "labels": list(getattr(reading, "labels", []) or []),
                       "summary": dict(getattr(reading, "summary", {}) or {}),
                       "notes": list(getattr(reading, "notes", []) or [])},
                      ensure_ascii=False)
    if body is not None:              # written as they were found
        return "\n".join([head] + list(body) + [_record_json(r) for r in recs])
    return "\n".join([head] + [_record_json(r) for r in recs])


def _record_load(d: Dict[str, Any], rel: str, rules_by_label: Dict[str, FieldRule]) -> Record:
    return Record(rel, d["kind"], [Value(rules_by_label[v[0]], v[1], v[2], v[3],
                                         bool(int(v[4]) & 1) if len(v) > 4 else False,
                                         bool(int(v[4]) & 2) if len(v) > 4 else False)
                                   for v in d["values"]],
                  skipped_row=int(d.get("skipped_row") or 0))


def _records_iter(text: str, rel: str, rules_by_label: Dict[str, FieldRule]
                  ) -> Iterable[Record]:
    """Records from doc_cache, one at a time for the line-per-record form.
    A label no confirmed rule has raises KeyError (the caller reads the
    document instead)."""
    if text.startswith(_JSONL_HEAD):
        first = True
        for line in io.StringIO(text):
            if first:
                first = False
                continue
            if line.strip():
                yield _record_load(json.loads(line), rel, rules_by_label)
        return
    data = json.loads(text)
    if isinstance(data, dict):                     # an earlier dict form
        data = data.get("records", [])
    for d in data:
        yield _record_load(d, rel, rules_by_label)


def _records_load(text: str, rel: str, rules_by_label: Dict[str, FieldRule]
                  ) -> List[Record]:
    return list(_records_iter(text, rel, rules_by_label))


def _reading_load(text: Optional[str]) -> Dict[str, Any]:
    """The reading part of a cached workbook / Ledger ({} for others): the
    first line of the line-per-record form is enough."""
    text = text or "[]"
    if text.startswith(_JSONL_HEAD):
        text = text.split("\n", 1)[0]
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


#: SQL for a cached reading's first line only (a workbook's summary).
_HEAD_SQL = ("CASE WHEN instr(c.records, char(10)) > 0 AND substr(c.records, 1, 7) = '{\"v\": 3'"
             " THEN substr(c.records, 1, instr(c.records, char(10)) - 1)"
             " WHEN substr(c.records, 1, 1) = '{' THEN c.records ELSE NULL END")


def document_lines(p: Path) -> List[Tuple[Optional[int], int, str]]:
    """Every text line of a document as ``(page or None, line, text)`` —
    for the gazetteer pass. A workbook's free text (notes cells, text
    boxes, pictures) is ``(sheet name, row, text)``; a CSV has none."""
    import field_search as fs
    suf = p.suffix.lower()
    out: List[Tuple[Optional[int], int, str]] = []
    if suf == ".pdf":
        for pno, text in enumerate(fs._pdf_pages(p), start=1):
            for i, ln in enumerate(text.splitlines(), start=1):
                out.append((pno, i, ln))
        return out
    if suf in _WORKBOOK:
        from council_core import kg_excel
        try:
            return list(kg_excel.read_workbook(p, "", [], _image_reader_now()).lines)
        except Exception:
            return []
    if suf in _TABULAR:
        return []          # tables are read by column, not by free text
    text = fs._read_text(p, max_chars=5_000_000)
    for i, ln in enumerate(text.splitlines(), start=1):
        out.append((None, i, ln))
    return out


# ── the store ────────────────────────────────────────────────────────────
def store_path(vault: Any) -> Path:
    return Path(vault) / STORE_DIR / STORE_NAME


class KnowledgeGraph:
    """The graph for one vault. ``root`` is the folder whose documents are
    read (default ``<vault>/data_in``, the folder Collections are relative to);
    paths in the store are relative to it."""

    #: How long opening the store waits for a rebuild elsewhere to let go.
    BUSY_TIMEOUT_S = 5.0

    #: ``image_reader``: how pictures in workbooks are read. The default is
    #: the Ledger's (source_extract.default_image_reader: Tesseract when it is
    #: installed, else nothing - never a vision model). None reads none.
    DEFAULT_IMAGE_READER = "default"

    def __init__(self, vault: Any, root: Any = None,
                 image_reader: Any = DEFAULT_IMAGE_READER) -> None:
        self.vault = Path(vault)
        self.root = Path(root) if root is not None else self.vault / "data_in"
        self.path = store_path(self.vault)
        self._image_reader_arg = image_reader
        #: During seed(): the entities this run supports (see seed).
        self._live: Optional[set] = None
        #: During seed(): each Ledger's people list (initials -> name), by
        #: document id, and the initials asked about this run.
        self._people: Dict[str, Dict[str, str]] = {}
        self._people_all: Dict[str, str] = {}
        self._asked: Dict[str, Dict[str, Any]] = {}
        #: During seed()'s graph phase only: what this rebuild already looked
        #: up (merges followed, entity names, aliases written, link
        #: statuses) and the evidence / mentions it gathered to write at
        #: once. Nothing merges during a rebuild, so these stay true for its
        #: length. MEASURED (review, 2026-10-16): 560,000 SQLite calls, 18 of
        #: 40 s, for a 20,000-row workbook rebuilt unchanged.
        self._memo: Optional[Dict[str, Any]] = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._check_before_writing()
        self.db = None
        try:
            self.db = sqlite3.connect(str(self.path), timeout=self.BUSY_TIMEOUT_S)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys = ON")
            # Deleted rows are overwritten with zeros, not just unlinked. The
            # store caches every document's text (doc_cache, decision g), and
            # a deleted document's text stayed in the file's free pages after
            # the rebuild that dropped its row - MEASURED: 3 copies of a
            # marker line (review, 2026-10-07). 'FAST' would leave the freed
            # overflow pages a long text lives in: it has to be ON.
            self.db.execute("PRAGMA secure_delete = ON")
            with self.db:
                self.db.executescript(SCHEMA_SQL)
                self.db.execute("INSERT OR IGNORE INTO meta VALUES "
                                "('schema_version', ?)", (str(SCHEMA_VERSION),))
                # v1 -> v2 -> v3 only ADD tables and an index (created
                # above); record it.
                self.db.execute("UPDATE meta SET value=? WHERE key='schema_version'"
                                " AND CAST(value AS INTEGER) < ?",
                                (str(SCHEMA_VERSION), SCHEMA_VERSION))
                for r in DEFAULT_FIELD_RULES:
                    self.db.execute("INSERT OR IGNORE INTO field_rules VALUES "
                                    "(?, ?, 'proposed')", (r.label, r.to_json()))
        except sqlite3.DatabaseError as exc:
            if self.db is not None:
                self.db.close()
            if _busy(exc):
                raise KnowledgeGraphBusy(
                    "the knowledge graph is being rebuilt (or written) elsewhere; try "
                    "again when that finishes") from exc
            raise KnowledgeGraphDamaged(
                f"the knowledge graph store {self.path} could not be read "
                f"({exc}); it was left untouched") from exc

    def _check_before_writing(self) -> None:
        """An existing store is checked over a READ-ONLY connection before
        anything is written: a damaged one, or one a newer Council wrote, is
        refused with its bytes untouched. The schema script and the default
        rules used to run first — MEASURED: a 'newer' store got its dropped
        tables recreated and a deleted rule re-inserted, then was refused."""
        # The whole path percent-encoded after 'file:' (no authority part):
        # Path.as_uri() gives 'file://%3F/C:/…' for a \\?\ path — the
        # spelling of a vault deeper than 260 characters — which SQLite
        # refuses ("invalid uri authority"), and the store was reported
        # damaged. Encoded like this, both spellings open.
        uri = "file:" + "".join(
            ch if ch.isascii() and ch.isalnum() else
            "".join(f"%{b:02X}" for b in ch.encode("utf-8"))
            for ch in str(self.path.resolve())) + "?mode=ro"
        try:
            ro = sqlite3.connect(uri, uri=True, timeout=self.BUSY_TIMEOUT_S)
        except sqlite3.Error as exc:
            raise KnowledgeGraphDamaged(
                f"the knowledge graph store {self.path} could not be read "
                f"({exc}); it was left untouched") from exc
        try:
            ok = ro.execute("PRAGMA quick_check").fetchone()[0]
            if ok != "ok":
                raise sqlite3.DatabaseError(ok)
            has_meta = ro.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                                  " AND name='meta'").fetchone()
            row = (ro.execute("SELECT value FROM meta WHERE key='schema_version'")
                   .fetchone() if has_meta else None)
            ver = int(row[0]) if row else None
        except (sqlite3.DatabaseError, ValueError) as exc:
            if _busy(exc):
                raise KnowledgeGraphBusy(
                    "the knowledge graph is being rebuilt (or written) elsewhere; try "
                    "again when that finishes") from exc
            raise KnowledgeGraphDamaged(
                f"the knowledge graph store {self.path} could not be read "
                f"({exc}); it was left untouched") from exc
        finally:
            ro.close()
        if ver is not None and ver > SCHEMA_VERSION:
            raise KnowledgeGraphDamaged(
                f"{self.path} was written by a newer Council (schema {ver}); "
                f"this one reads up to {SCHEMA_VERSION}; it was left untouched")

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def meta(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # ── decisions (append-only) ──
    def _decide(self, action: str, payload: Dict[str, Any]) -> None:
        self.db.execute("INSERT INTO decisions (ts, action, payload) VALUES (?,?,?)",
                        (time.time(), action, json.dumps(payload, sort_keys=True,
                                                         ensure_ascii=False)))

    def decisions(self) -> List[Dict[str, Any]]:
        return [{"id": r["id"], "ts": r["ts"], "action": r["action"],
                 "payload": json.loads(r["payload"])}
                for r in self.db.execute("SELECT * FROM decisions ORDER BY id")]

    # ── field rules ──
    def field_rules(self, status: Optional[str] = None) -> List[Tuple[FieldRule, str]]:
        q = "SELECT rule, status FROM field_rules"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        return [(FieldRule.from_json(r["rule"]), r["status"])
                for r in self.db.execute(q + " ORDER BY label", args)]

    def set_rule_status(self, label: str, status: str) -> None:
        assert status in ("proposed", "confirmed", "rejected")
        with self.db:
            n = self.db.execute("UPDATE field_rules SET status=? WHERE label=?",
                                (status, label)).rowcount
            if not n:
                raise KeyError(label)
            self._decide("field_rule", {"label": label, "status": status})

    def confirm_all_rules(self) -> None:
        for r, st in self.field_rules():
            if st == "proposed":
                self.set_rule_status(r.label, "confirmed")

    def add_label_synonym(self, label: str, means: str) -> FieldRule:
        """The user confirmed that ``label`` (e.g. 'POC') means the existing
        rule ``means`` ('Point of Contact'). Stored confirmed."""
        base = {r.label: r for r, _s in self.field_rules()}.get(means)
        if base is None:
            raise KeyError(means)
        rule = base.renamed(label)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO field_rules VALUES (?,?,'confirmed')",
                            (label, rule.to_json()))
            self._decide("label_synonym", {"label": label, "means": means})
        return rule

    def suggest_label_synonyms(self, files: Optional[Iterable[Path]] = None,
                               min_confidence: float = 0.6) -> List[Dict[str, Any]]:
        """Labels in the documents that probably mean one of the confirmed
        rules but are not rules yet ('POC' ~ 'Point of Contact', 0.80). For
        the user to confirm — never applied silently. ``precheck`` is True at
        confidence >= 0.85, the level the UI may tick by default."""
        import field_search as fs
        labels = self.harvest_labels(files)
        known = {fs._norm_key(r.label) for r, _s in self.field_rules()}
        out = []
        for rule, _st in self.field_rules("confirmed"):
            for c in fs.field_name_candidates(rule.label, labels,
                                              min_confidence=min_confidence):
                if fs._norm_key(c["label"]) in known:
                    continue
                out.append({"label": c["label"], "means": rule.label,
                            "confidence": c["confidence"], "why": c["why"],
                            "precheck": c["confidence"] >= 0.85})
        out.sort(key=lambda d: -d["confidence"])
        return out

    def harvest_labels(self, files: Optional[Iterable[Path]] = None) -> List[str]:
        """Every column header, JSON key and 'Label:' in the documents."""
        import field_search as fs
        seen: Dict[str, str] = {}
        for p in (files if files is not None else self.document_files()):
            suf = p.suffix.lower()
            try:
                if suf in _WORKBOOK:
                    # The labels the Ledger's reader found (headers on any
                    # row, labelled cells, text boxes): from the last
                    # rebuild's reading when it is current - re-reading
                    # would start Excel for every .xls - else read now.
                    for lab in self._workbook_labels(p):
                        seen.setdefault(fs._norm_key(lab), lab)
                elif suf in _TABULAR:
                    for _s, fr in fs._table_frames(p):
                        for c in fr.columns:
                            # A sentence in row 1 (a 'Read Me' sheet) is
                            # not a label.
                            if len(str(c).split()) <= 5:
                                seen.setdefault(fs._norm_key(c), str(c))
                elif suf in _JSON:
                    def keys(o):
                        if isinstance(o, dict):
                            for k, v in o.items():
                                seen.setdefault(fs._norm_key(k), str(k))
                                keys(v)
                        elif isinstance(o, list):
                            for v in o:
                                keys(v)
                    keys(json.loads(p.read_text(encoding="utf-8", errors="replace")))
                else:
                    for _pg, _i, ln in document_lines(p):
                        for k, _v in fs._kv_pairs(ln):
                            if k and len(k.split()) <= 5:
                                seen.setdefault(fs._norm_key(k), k)
            except Exception:
                continue
        return [v for k, v in seen.items() if k]

    # ── folders outside the vault (read only) ──
    #: meta key: the extra folders the user added, as JSON
    #: [{"id", "path", "added_ts"}]. Kept in the store, so per vault.
    EXTRA_ROOTS_KEY = "extra_roots"
    _EXTRA_RE = re.compile(r"^<([0-9a-f]{10})>/(.*)$")

    @staticmethod
    def root_id(folder: Any) -> str:
        """A short stable id for a folder (its resolved, case-folded path):
        the prefix of its documents' paths in the store, '<id>/rel/path'.
        '<' and '>' cannot be in a Windows file name, so no path under
        data_in can look like one."""
        return hashlib.sha256(_real(folder).encode("utf-8")).hexdigest()[:10]

    def extra_roots(self) -> List[Dict[str, Any]]:
        """The folders outside the vault the graph also reads, as the user
        added them: id, path, name, and whether the folder is there now."""
        raw = self.meta(self.EXTRA_ROOTS_KEY)
        try:
            items = json.loads(raw) if raw else []
        except Exception:
            items = []
        out = []
        for d in items if isinstance(items, list) else []:
            if isinstance(d, dict) and d.get("id") and d.get("path"):
                p = Path(str(d["path"]))
                out.append({"id": str(d["id"]), "path": str(p), "name": p.name or str(p),
                            "exists": p.is_dir(), "added_ts": d.get("added_ts")})
        return out

    def _extra_bases(self) -> List[Tuple[str, Path]]:
        if getattr(self, "_bases", None) is None:
            self._bases = [(r["id"], Path(r["path"])) for r in self.extra_roots()]
        return self._bases

    def add_root(self, folder: Any) -> Dict[str, Any]:
        """Also read the documents of ``folder`` - a folder outside the vault,
        only ever READ: nothing in it is written, moved or copied into the
        vault. Refused when it is not a folder, or overlaps the vault (its
        data_in, its store) or a folder already added - the same file would
        be two documents. Rebuild to read it."""
        import os
        p = Path(_plain_path(folder)).expanduser()
        if not p.is_dir():
            raise ValueError(f"{folder} is not a folder")
        p = Path(os.path.realpath(str(p)))      # kept as written; compared case-folded
        # compared as Windows does: case-folded, links and the '\\?\' long
        # spelling resolved - '\\?\C:\vault\data_in\sub' got past it, and
        # its documents were read twice (review, 2026-10-16)
        if _overlaps(p, self.vault) or _overlaps(p, self.root):
            raise ValueError(f"{p} overlaps the vault, which the graph reads already")
        current = self.extra_roots()
        for r in current:
            if _overlaps(p, r["path"]):
                raise ValueError(f"{p} overlaps a folder already added ({r['path']})")
        entry = {"id": self.root_id(p), "path": str(p), "added_ts": time.time()}
        items = [{"id": r["id"], "path": r["path"], "added_ts": r.get("added_ts")}
                 for r in current] + [entry]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                            (self.EXTRA_ROOTS_KEY, json.dumps(items, ensure_ascii=False)))
            self._decide("add_folder", {"id": entry["id"], "path": entry["path"]})
        self._bases = None
        return dict(entry, name=p.name or str(p), exists=True)

    def remove_root(self, rid: str) -> None:
        """Stop reading an added folder. Its documents leave the graph at the
        next rebuild (they count as gone: their links and cached text go);
        the folder itself is not touched."""
        current = self.extra_roots()
        if not any(r["id"] == rid for r in current):
            raise KeyError(rid)
        items = [{"id": r["id"], "path": r["path"], "added_ts": r.get("added_ts")}
                 for r in current if r["id"] != rid]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                            (self.EXTRA_ROOTS_KEY, json.dumps(items, ensure_ascii=False)))
            self._decide("remove_folder", {"id": rid})
        self._bases = None

    def abs_path(self, rel: str) -> Path:
        """Where a document of the store is on disk: under data_in, or under
        the added folder its '<id>/' names (a removed folder's documents
        resolve to a path that does not exist)."""
        m = self._EXTRA_RE.match(str(rel or ""))
        if m:
            for rid, base in self._extra_bases():
                if rid == m.group(1):
                    return base / m.group(2)
            return self.vault / STORE_DIR / "removed-folder" / m.group(2)
        return self.root / rel

    def display_path(self, rel: str) -> str:
        """A document's path as a person reads it: 'trackers/x.xlsx' for the
        vault's, 'Q3 Ledgers/lmm_parent.csv' for an added folder's."""
        m = self._EXTRA_RE.match(str(rel or ""))
        if not m:
            return rel
        name = next((base.name or str(base) for rid, base in self._extra_bases()
                     if rid == m.group(1)), "(a removed folder)")
        return f"{name}/{m.group(2)}"

    def _workbook_labels(self, p: Path) -> List[str]:
        try:
            rel = self._rel(p)
        except ValueError:
            rel = ""
        doc = self.db.execute("SELECT status FROM documents WHERE path=?",
                              (rel,)).fetchone() if rel else None
        if doc is not None and doc["status"] == "unreadable":
            # the last rebuild could not read it: Excel is not started again
            # for its labels (review, 2026-10-16)
            return []
        row = self.db.execute(
            "SELECT c.records, c.content_hash FROM doc_cache c JOIN documents d ON"
            " d.id=c.document_id WHERE d.path=?", (rel,)).fetchone() if rel else None
        if row is not None and row["content_hash"] == self._content_hash(p):
            got = _reading_load(row["records"])
            if "labels" in got:
                return list(got["labels"])
        from council_core import kg_excel
        return list(kg_excel.read_workbook(p, rel, []).labels)

    # ── documents ──
    def document_files(self) -> List[Path]:
        """Supported files under ``root`` and under every folder the user
        added (add_root), skipping dot-folders and the protected app
        folders. An added folder that is not there now (a drive unplugged)
        gives nothing: its documents count as gone until it is back."""
        try:
            import conversation_logger as cl
        except Exception:
            cl = None
        from council_core import kg_excel
        out = []
        prog = progress_now()
        if self.root.is_dir():
            if prog is not None:
                prog.update(f"listing {self.root}", force=True)
            out += self._files_under(self.root, cl, kg_excel, sorted(self.root.rglob("*")))
        others = [base for _rid, base in self._extra_bases()]
        for base in others:
            if not base.is_dir():
                continue
            if prog is not None:
                prog.update(f"listing {base}", force=True)
            out += self._files_under(base, cl, kg_excel, self._walk_added(base, others))
        return out

    def _walk_added(self, base: Path, others: Sequence[Path]) -> List[Path]:
        """An added folder's files. Links and junctions are not followed and
        a file that resolves into the vault or another folder is left out: a
        junction to the vault read data_in twice and data_out too (review,
        2026-10-16). System folders (a drive's Recycle Bin, System Volume
        Information) are skipped: deleted files' text came into the graph."""
        import os
        guard = [self.vault] + [o for o in others if Path(o) != base]
        out: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames
                                 if not _skip_dir(os.path.join(dirpath, d), d))
            for f in sorted(filenames):
                full = os.path.join(dirpath, f)
                if _is_link(full) or any(_overlaps(full, g, inside_only=True) for g in guard):
                    continue
                out.append(Path(full))
        return out

    def _files_under(self, base: Path, cl, kg_excel, candidates) -> List[Path]:
        out = []
        for p in candidates:
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            rel = p.relative_to(base)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if cl is not None and cl.is_protected_path(p, self.vault):
                continue
            # Excel's '~$' lock files, the Ledger's own settings beside a
            # parent CSV (its schema and aliases: the Ledger reads them, they
            # say nothing about people or parts) and its review workbooks
            # (values still WAITING for a person's decision - only the parent
            # CSV holds accepted ones) are not documents.
            if p.name.startswith("~$") or kg_excel.is_ledger_sidecar(p) \
                    or kg_excel.is_review_workbook(p):
                continue
            out.append(p)
        return out

    def _rel(self, p: Path) -> str:
        """A document's path in the store: relative to data_in, or
        '<folder id>/relative' for an added folder's."""
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            pass
        for rid, base in self._extra_bases():
            try:
                return f"<{rid}>/" + p.relative_to(base).as_posix()
            except ValueError:
                continue
        raise ValueError(f"{p} is in none of the folders the graph reads")

    @staticmethod
    def _content_hash(p: Path) -> str:
        """The file's sha256 - for a Ledger, of its CSV, its schema and its
        review workbooks together: the same CSV reads differently under a
        changed schema, and a newly saved review says where more values came
        from."""
        if p.suffix.lower() == ".csv":
            from council_core import kg_excel
            if kg_excel.is_ledger_csv(p):
                h = hashlib.sha256(_file_hash(p).encode("ascii"))
                h.update(kg_excel.ledger_fingerprint(p))
                return h.hexdigest()
        return _file_hash(p)

    def _document(self, p: Path, run_id: str) -> Tuple[str, str]:
        rel = self._rel(p)
        h = self._content_hash(p)
        st = p.stat()
        row = self.db.execute("SELECT id FROM documents WHERE path=?", (rel,)).fetchone()
        did = row["id"] if row else str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO documents (id, path, content_hash, size, mtime, scanned_ts, status)"
            " VALUES (?,?,?,?,?,?,'ok') ON CONFLICT(path) DO UPDATE SET"
            " content_hash=excluded.content_hash, size=excluded.size,"
            " mtime=excluded.mtime, scanned_ts=excluded.scanned_ts, status='ok'",
            (did, rel, h, st.st_size, st.st_mtime, time.time()))
        # A document's entity id IS its document UUID, so a relation can
        # point at the document directly (project DOCUMENTED_IN document).
        self.db.execute("INSERT OR IGNORE INTO entities (id, type, key, name, created_ts)"
                        " VALUES (?, 'DOCUMENT', ?, ?, ?)", (did, did, rel, time.time()))
        self.db.execute("UPDATE entities SET name=? WHERE id=?", (rel, did))
        return did, h

    # ── entities ──
    def _entity(self, etype: str, raw: str, display: Optional[str] = None,
                *, key: Optional[str] = None, alias_source: str = "field") -> str:
        key = key if key is not None else entity_key(etype, raw)
        eid = entity_id(etype, key)
        if self._live is not None:
            self._live.add(eid)
        name = display or (person_display(raw) if etype == "PERSON" else str(raw).strip())
        memo = self._memo
        known = memo["names"].get(eid) if memo is not None else None
        if known is None:
            row = self.db.execute("SELECT name FROM entities WHERE id=?", (eid,)).fetchone()
            known = row["name"] if row is not None else None
        if known is None:
            self.db.execute("INSERT INTO entities (id, type, key, name, created_ts)"
                            " VALUES (?,?,?,?,?)", (eid, etype, key, name, time.time()))
            known = name
        elif etype == "PERSON" and len(name) > len(known):
            # 'Bob Smith' then 'Bob Smith, Jr.': keep the fullest form.
            self.db.execute("UPDATE entities SET name=? WHERE id=?", (name, eid))
            known = name
        if memo is not None:
            memo["names"][eid] = known
        if etype != "DOCUMENT":
            self._alias(eid, raw, alias_source)
        return eid

    def _alias(self, eid: str, alias: str, source: str) -> None:
        eid = self.resolve(eid)          # a merged entity's names live on its survivor
        alias = " ".join(str(alias).split())
        if alias:
            memo = self._memo
            if memo is not None:
                if (eid, alias) in memo["aliases"]:
                    return
                memo["aliases"].add((eid, alias))
            self.db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?,?,?)",
                            (eid, alias, fold(alias), source))

    def resolve(self, eid: str) -> str:
        """Follow user merges to the surviving entity."""
        memo = self._memo
        if memo is not None:
            got = memo["resolve"].get(eid)
            if got is not None:
                return got
        start = eid
        seen = set()
        while eid and eid not in seen:
            seen.add(eid)
            row = self.db.execute("SELECT merged_into FROM entities WHERE id=?",
                                  (eid,)).fetchone()
            if not row or not row["merged_into"]:
                break
            eid = row["merged_into"]
        if memo is not None and start:
            memo["resolve"][start] = eid
        return eid

    # ── relations ──
    def _relate(self, s: str, pred: str, o: str, status: str, *, doc_id: str,
                content_hash: str, locator: Dict[str, Any], quote: str,
                method: str, run_id: str, model: str = "") -> str:
        s, o = self.resolve(s), self.resolve(o)
        if s == o:
            # Two merged entries (PN-1234/B with PN-1234/A merged into it):
            # their row's 'B supersedes A' would make B supersede ITSELF.
            return ""
        rid = relation_id(s, pred, o)
        now = time.time()
        memo = self._memo
        cur = memo["rels"].get(rid) if memo is not None else None
        if cur is None:
            row = self.db.execute("SELECT status FROM relations WHERE id=?",
                                  (rid,)).fetchone()
            cur = row["status"] if row is not None else None
        if cur is None:
            self.db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?,?)",
                            (rid, s, pred, o, status, now, now))
            cur = status
        elif cur == _PENDING or (cur == "suggested" and status == "seeded"):
            # First evidence this run (seed() marked every derived status
            # pending), or firmer evidence; the user's accept/reject is never
            # touched.
            self.db.execute("UPDATE relations SET status=?, updated_ts=? "
                            "WHERE id=?", (status, now, rid))
            cur = status
        if memo is not None:
            memo["rels"][rid] = cur
        row = (rid, doc_id, content_hash, _loc_json(locator), quote, method, model, run_id)
        if memo is not None and memo.get("buffer") is not None:
            memo["buffer"]["evidence"].append(row)
        else:
            self.db.execute(_EVIDENCE_INSERT, row)
        return rid

    def set_relation_status(self, rid: str, status: str, *, note: str = "") -> None:
        if status not in ("accepted", "rejected"):
            raise ValueError(status)
        with self.db:
            n = self.db.execute("UPDATE relations SET status=?, updated_ts=? WHERE id=?",
                                (status, time.time(), rid)).rowcount
            if not n:
                raise KeyError(rid)
            self._decide(f"relation_{status}", {"relation_id": rid, "note": note})

    # ── seeding ──
    #: The reading phase commits after this many documents (or SEED_BATCH_S
    #: seconds), so a rebuild stopped part-way — the app closed, the PC
    #: slept — keeps what it read: the next one starts where it stopped.
    SEED_BATCH = 50
    SEED_BATCH_S = 5.0

    def _cached(self, did: str, h: str, sig: str) -> Optional[sqlite3.Row]:
        row = self.db.execute("SELECT * FROM doc_cache WHERE document_id=?",
                              (did,)).fetchone()
        return row if row is not None and row["content_hash"] == h and \
            row["reader_sig"] == sig else None

    def _cached_lines(self, did: str, h: str, p: Path
                      ) -> List[Tuple[Optional[int], int, str]]:
        """A document's text lines: from doc_cache when it holds this
        version, else read from the file."""
        row = self.db.execute("SELECT content_hash, lines FROM doc_cache WHERE"
                              " document_id=?", (did,)).fetchone()
        if row is not None and row["content_hash"] == h:
            return [tuple(x) for x in json.loads(row["lines"])]
        return document_lines(p)

    def seed(self, *, use_collections: bool = True, gazetteer: bool = True,
             on_progress=None, listener=None) -> Dict[str, Any]:
        """Rebuild, with progress lines in the terminal (progress_report:
        each stage's count and %, the file being read, a "still working"
        line while nothing moves) - also handed to ``listener(line,
        fraction)``, the Connections tab's status line. See _seed."""
        from progress_report import Progress
        with Progress("kg rebuild", listener=listener) as prog:
            token = _PROGRESS.set(prog)
            try:
                stats = self._seed(use_collections=use_collections, gazetteer=gazetteer,
                                   on_progress=on_progress, prog=prog)
            finally:
                _PROGRESS.reset(token)
            rels = stats.get("relations") or {}
            prog.finish(f"{stats['documents']} documents ({stats['documents_read']} read, "
                        f"{stats['documents_unchanged']} unchanged, "
                        f"{len(stats['unreadable'])} not read); "
                        f"{rels.get('seeded', 0) + rels.get('accepted', 0)} links, "
                        f"{rels.get('suggested', 0)} suggested, "
                        f"{stats.get('open_reviews', 0)} question(s)")
            return stats

    def _seed(self, *, use_collections: bool, gazetteer: bool, on_progress,
              prog) -> Dict[str, Any]:
        """Rebuild everything that comes from documents, with no model:
        labelled fields (confirmed rules only), Collections, and a gazetteer
        pass. The user's decisions and accepted/rejected statuses survive.
        Returns the run's stats (also stored in ``runs``).

        Two phases. READING: each document's records and text lines are kept
        in doc_cache by content hash, so only documents whose content changed
        (or every one, when the confirmed rules or the reading code changed)
        are read and parsed; this phase commits in batches, so a stopped
        rebuild resumes. MEASURED before (review, 2026-10-06): ~15 minutes
        for 3,100 files in ONE transaction - stopped, it kept nothing. THE
        GRAPH: worked out from the cached readings in one transaction, as
        before - never half-built, and the same as a fresh build."""
        from council_core import kg_excel
        run_id = str(uuid.uuid4())
        t0 = time.time()
        rules = [r for r, _s in self.field_rules("confirmed")]
        by_label = {r.label: r for r in rules}
        by_label.update(BUILTIN_RULES)
        reader = self._resolve_image_reader()
        # What reads pictures and whether Excel is here matter to workbooks
        # only: a new Tesseract re-read every text file and PDF too (review,
        # 2026-10-16).
        base_sig = _reader_sig(rules)
        wb_sig = _reader_sig(rules, image_reader=getattr(reader, "name", "image reader")
                             if reader is not None else "", excel=_excel_available())

        def sig_for(p: Path) -> str:
            return wb_sig if p.suffix.lower() in _WORKBOOK else base_sig
        prog.stage("listing documents")
        files = self.document_files()
        gone_roots = [r for r in self.extra_roots() if not r["exists"]]
        stats: Dict[str, Any] = {"documents": len(files), "records": 0,
                                 "unreadable": [], "skipped_rows": [],
                                 "rules": len(rules), "documents_read": 0,
                                 "documents_unchanged": 0, "notes": [],
                                 "ledgers": 0, "workbooks": 0, "pictures": 0,
                                 "pictures_read": 0}
        for r in gone_roots:
            stats["notes"].append(f"{r['path']}: this added folder is not there now; its "
                                  "documents count as gone until it is back")
        with self.db:
            self.db.execute("INSERT INTO runs (id, kind, started_ts) VALUES (?,?,?)",
                            (run_id, "seed", t0))
        # ── 1. reading (batched commits) ──
        seen_paths = set()
        readable: List[Tuple[Path, str, str]] = []
        #: Workbooks that can never be read as they are (a password, not a
        #: workbook at all): their cached reason is kept, so Excel is not
        #: started for them again on every rebuild.
        failed_keep: List[str] = []
        last, n_batch = time.time(), 0
        token = _IMAGE_READER.set(reader)
        as_lines = _AS_LINES.set(True)
        prog.stage("reading documents", total=len(files), unit="files")
        try:
            for n, p in enumerate(files):
                if on_progress:
                    on_progress(n, len(files))
                rel = self._rel(p)
                said = "unchanged"
                try:
                    did, h = self._document(p, run_id)
                    seen_paths.add(rel)
                    why = missing_reader(p)
                    sig = sig_for(p)
                    cached = None if why else self._cached(did, h, sig)
                    if why:
                        stats["unreadable"].append(f"{rel}: {why}")
                        self.db.execute("UPDATE documents SET status='unreadable'"
                                        " WHERE id=?", (did,))
                    elif cached is not None:
                        err = _reading_load(cached["records"]).get("error")
                        if err:
                            stats["unreadable"].append(f"{rel}: {err}")
                            self.db.execute("UPDATE documents SET status='unreadable'"
                                            " WHERE id=?", (did,))
                            failed_keep.append(did)
                        else:
                            stats["documents_unchanged"] += 1
                            readable.append((p, did, h))
                    else:
                        said = "read"
                        if p.suffix.lower() in (".xls", ".xlsb"):
                            # where a wait can start: shown at once
                            prog.update(f"opening {rel} through Excel (up to 2 min)",
                                        force=True)
                        else:
                            prog.update(f"reading {rel}")
                        try:
                            recs = read_records(p, rel, rules)
                        except kg_excel.Unreadable as exc:
                            if exc.permanent:
                                self.db.execute(
                                    "INSERT OR REPLACE INTO doc_cache VALUES (?,?,?,?,?,?)",
                                    (did, h, sig, _records_json([], error=str(exc)), "[]",
                                     time.time()))
                                failed_keep.append(did)
                            raise
                        reading = next((r.reading for r in recs if r.kind == "reading"),
                                       None)
                        recs = [r for r in recs if r.kind != "reading"]
                        if reading is not None:
                            lines = list(reading.lines)
                        else:
                            lines = [] if p.suffix.lower() in _TABULAR else document_lines(p)
                        self.db.execute(
                            "INSERT OR REPLACE INTO doc_cache VALUES (?,?,?,?,?,?)",
                            (did, h, sig, _records_json(recs, reading),
                             json.dumps(lines, ensure_ascii=False), time.time()))
                        stats["documents_read"] += 1
                        readable.append((p, did, h))
                except Exception as exc:
                    # Listed but not readable now (held open with an exclusive
                    # lock by another program, a parse error): 'unreadable',
                    # with what it had - not 'missing'. It never reached
                    # seen_paths, so it was marked missing and its model links
                    # were deleted (review, 2026-10-07: locked once, its links
                    # were gone for good). A workbook says why in words.
                    stats["unreadable"].append(
                        f"{rel}: {exc}" if isinstance(exc, kg_excel.Unreadable)
                        else f"{rel}: {exc.__class__.__name__}")
                    said = "not read"
                    seen_paths.add(rel)
                    self.db.execute("UPDATE documents SET status='unreadable' WHERE path=?",
                                    (rel,))
                prog.advance(what=f"{rel} {said}")
                n_batch += 1
                if n_batch >= self.SEED_BATCH or time.time() - last >= self.SEED_BATCH_S:
                    self.db.commit()
                    last, n_batch = time.time(), 0
            self.db.commit()
        except BaseException:
            self.db.rollback()          # the batch in progress; earlier ones stay
            raise
        finally:
            _IMAGE_READER.reset(token)
            _AS_LINES.reset(as_lines)
        # ── 2. the graph (one transaction) ──
        try:
            with self.db:
                prog.stage("building the graph", total=len(readable), unit="documents")
                self._people, self._people_all, self._asked = {}, {}, {}
                clash: set = set()
                usable: List[Tuple[Path, str, str]] = []
                fallback: Dict[str, List[Record]] = {}
                for p, did, h in readable:
                    head = self.db.execute(
                        f"SELECT {_HEAD_SQL} AS head, c.content_hash, c.reader_sig FROM"
                        " doc_cache c WHERE c.document_id=?", (did,)).fetchone()
                    if head is None or head["content_hash"] != h or \
                            head["reader_sig"] != sig_for(p):
                        # not expected (the reading phase cached it): read it now
                        try:
                            token = _IMAGE_READER.set(reader)
                            try:
                                fallback[did] = [r for r in read_records(p, self._rel(p), rules)
                                                 if r.kind != "reading"]
                            finally:
                                _IMAGE_READER.reset(token)
                        except Exception as exc:
                            stats["unreadable"].append(
                                f"{self._rel(p)}: {exc.__class__.__name__}")
                            continue
                    usable.append((p, did, h))
                    reading = _reading_load(head["head"] if head is not None else None)
                    summary = reading.get("summary") or {}
                    for note in reading.get("notes") or []:
                        if len(stats["notes"]) < 200:
                            stats["notes"].append(f"{self._rel(p)}: {note}")
                    if summary.get("kind") == "ledger":
                        stats["ledgers"] += 1
                        # Its people list: 'JB' is Jordan Blake in THIS ledger.
                        self._people[did] = _ledger_people(p)
                        for k, name in self._people[did].items():
                            if self._people_all.setdefault(k, name) != name:
                                clash.add(k)
                    elif summary.get("kind") == "workbook":
                        stats["workbooks"] += 1
                        stats["pictures"] += int(summary.get("pictures") or 0)
                        stats["pictures_read"] += int(summary.get("pictures_read") or 0)
                    prog.advance()
                # Initials two Ledgers give to different people are nobody's
                # outside those Ledgers: asked, not picked.
                for k in clash:
                    self._people_all.pop(k, None)

                def records():
                    """Every record, one document at a time, one record at a
                    time: _seed_records reads them in three passes, and they
                    used to be held all at once."""
                    for p, did, h in usable:
                        if did in fallback:
                            recs = fallback[did]
                        else:
                            row = self.db.execute("SELECT records FROM doc_cache WHERE"
                                                  " document_id=?", (did,)).fetchone()
                            try:
                                recs = _records_iter(row["records"], self._rel(p), by_label)
                            except Exception:
                                recs = []
                        for rec in recs:
                            yield rec, did, h


                def kept():
                    for rec, did, h in records():
                        if rec.kind != "skipped":
                            yield rec, did, h
                # Gone since the last run: kept (decisions may cite them), marked.
                for r in self.db.execute("SELECT id, path FROM documents").fetchall():
                    if r["path"] not in seen_paths:
                        self.db.execute("UPDATE documents SET status='missing' WHERE id=?",
                                        (r["id"],))
                # The cache holds a document's text: none is kept for a file
                # that was deleted or can no longer be read.
                keep = [did for _p, did, _h in readable] + failed_keep
                self.db.execute("CREATE TEMP TABLE IF NOT EXISTS _kg_keep (id TEXT PRIMARY KEY)")
                self.db.execute("DELETE FROM _kg_keep")
                self.db.executemany("INSERT OR IGNORE INTO _kg_keep VALUES (?)",
                                    [(d,) for d in keep])
                self.db.execute("DELETE FROM doc_cache WHERE document_id NOT IN"
                                " (SELECT id FROM _kg_keep)")
                # Nothing the graph is worked out from changed (each document's
                # content and reading, the user's answers, merges, decisions,
                # names, Collections, the Ledgers' people lists): the graph
                # that is there IS the answer. MEASURED (review, 2026-10-16):
                # an unchanged 100k-row workbook's rebuild took 274 s, all of
                # it working the same graph out again.
                gsig = self._graph_sig(usable, failed_keep, sig_for, use_collections,
                                       gazetteer)
                if gsig == self.meta("graph_sig") and not fallback:
                    raise _GraphUnchanged()
                # From here on the graph is worked out again (the check above
                # comes first: clearing 500,000 derived rows to roll them back
                # was most of an unchanged rebuild).
                n_records = 0
                for rec, _did, _h in records():
                    if rec.kind == "skipped":
                        stats["skipped_rows"].append(
                            f"{rec.path}: row {rec.skipped_row} has more cells than the header")
                    else:
                        n_records += 1
                stats["records"] = n_records
                # Derived rows are rebuilt; decisions and user statuses are kept.
                self.db.execute("DELETE FROM evidence WHERE method != 'model'")
                self.db.execute("DELETE FROM mentions")
                self.db.execute("DELETE FROM review WHERE status='open'")
                self.db.execute("DELETE FROM aliases WHERE source != 'user'")
                # Every derived status is worked out again from THIS run's
                # evidence: a link once 'seeded' stayed seeded after its document
                # came to name several projects, so the user was never asked.
                self.db.execute(f"UPDATE relations SET status='{_PENDING}'"
                                " WHERE status IN ('seeded','suggested')")
                # Only entities this run supports (plus the user's: merged,
                # aliased by hand, in an accepted link) may explain an initial or
                # give the gazetteer a name. A stale 'Dana Whitfield' otherwise
                # gave herself a mention for 'D. Whitfield' and never went away.
                self._memo = {"resolve": {}, "names": {}, "aliases": set(), "rels": {},
                              "buffer": None}
                self._live = {r[0] for r in self.db.execute(
                    "SELECT entity_id FROM aliases WHERE source='user'"
                    " UNION SELECT merged_into FROM entities WHERE merged_into IS NOT NULL"
                    " UNION SELECT subject_id FROM relations WHERE status='accepted'"
                    " UNION SELECT object_id FROM relations WHERE status='accepted'")}
                # A model's evidence stays across rebuilds (it cost GPU time) -
                # unless its document changed since, or is gone: then it is
                # stale and goes. A deleted file kept its last content hash,
                # so its model links lived on with nothing behind them.
                self.db.execute(
                    "DELETE FROM evidence WHERE method='model' AND (content_hash !="
                    " (SELECT content_hash FROM documents d WHERE d.id=evidence.document_id)"
                    " OR document_id IN (SELECT id FROM documents WHERE status='missing'))")
                # ...and what a model READ goes with what it found: those
                # passages are read again if that content comes back (a file
                # moved out and back, an edit undone). Kept, the passages still
                # counted as done and the dropped links never came back
                # (review, 2026-10-07: chunks=0, documents_skipped=1).
                self.db.execute(
                    "DELETE FROM extraction_done WHERE document_id NOT IN"
                    " (SELECT id FROM documents WHERE status != 'missing') OR content_hash !="
                    " (SELECT content_hash FROM documents d WHERE"
                    "  d.id=extraction_done.document_id)")
                self.db.execute(
                    "DELETE FROM extraction_docs WHERE document_id NOT IN"
                    " (SELECT id FROM documents WHERE status != 'missing')")
                prog.update(f"linking {n_records} records", force=True)
                # Evidence and mentions are gathered, then written in batches;
                # nothing reads them until the gazetteer (which checks the
                # field mentions) - flushed before it.
                self._memo["buffer"] = {"evidence": [], "mentions": []}
                self._seed_records(kept, run_id)
                self._finish_initials()
                if use_collections:
                    prog.update("Collections")
                    stats["collections"] = self._seed_collections(run_id)
                self._flush()
                self._memo["buffer"] = None
                if gazetteer:
                    prog.stage("finding known names in the text", total=len(files),
                               unit="files")
                    stats["gazetteer_mentions"] = self._gazetteer(files, run_id, prog)
                # Still pending: no document gave evidence this run. One a model
                # proposed (KG2 evidence is kept across runs) is a suggestion; the
                # rest - the document changed or the rule was withdrawn - go. The
                # user's accepted/rejected ones stay.
                self.db.execute(
                    f"UPDATE relations SET status='suggested' WHERE status='{_PENDING}'"
                    " AND id IN (SELECT relation_id FROM evidence)")
                self.db.execute(
                    "DELETE FROM relations WHERE status IN ('seeded','suggested',"
                    f" '{_PENDING}') AND id NOT IN (SELECT relation_id FROM evidence)")
                # An entity nothing supports any more (its only document changed)
                # goes too, unless the user touched it: merged, merged into, or
                # given an alias by hand.
                self.db.execute(
                    "DELETE FROM entities WHERE type != 'DOCUMENT' AND merged_into IS NULL"
                    " AND id NOT IN (SELECT entity_id FROM mentions)"
                    " AND id NOT IN (SELECT subject_id FROM relations)"
                    " AND id NOT IN (SELECT object_id FROM relations)"
                    " AND id NOT IN (SELECT merged_into FROM entities"
                    "                WHERE merged_into IS NOT NULL)"
                    " AND id NOT IN (SELECT entity_id FROM aliases WHERE source='user')")
                self.db.execute("DELETE FROM aliases WHERE entity_id NOT IN"
                                " (SELECT id FROM entities)")
                stats.update(self.counts())
                stats["graph_unchanged"] = False
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('graph_sig', ?)",
                                (self._graph_sig(usable, failed_keep, sig_for, use_collections,
                                                 gazetteer),))
                self.db.execute("UPDATE runs SET finished_ts=?, stats=? WHERE id=?",
                                (time.time(), json.dumps(stats), run_id))
        except _GraphUnchanged:
            # The transaction above rolled back (it had only marked statuses
            # pending and cleared derived rows): the graph stands as it was.
            last = self.db.execute("SELECT stats FROM runs WHERE kind='seed' AND finished_ts"
                                   " IS NOT NULL ORDER BY finished_ts DESC LIMIT 1").fetchone()
            before = json.loads(last["stats"]) if last and last["stats"] else {}
            for k in ("collections", "gazetteer_mentions", "records", "skipped_rows"):
                if k in before:
                    stats[k] = before[k]
            stats.update(self.counts())
            stats["graph_unchanged"] = True
            with self.db:
                self.db.execute("UPDATE runs SET finished_ts=?, stats=? WHERE id=?",
                                (time.time(), json.dumps(stats), run_id))
        finally:
            self._live = None
            self._memo = None
            self._people, self._people_all, self._asked = {}, {}, {}
        stats["seconds"] = round(time.time() - t0, 3)
        return stats

    def _resolve_image_reader(self):
        arg = self._image_reader_arg
        if arg != self.DEFAULT_IMAGE_READER:
            return arg
        try:
            import source_extract
            return source_extract.default_image_reader()
        except Exception:
            return None

    def _graph_sig(self, usable, failed_keep, sig_for, use_collections: bool,
                   gazetteer: bool) -> str:
        """Everything the graph phase works the graph out from."""
        h = hashlib.sha256(f"graph {SCHEMA_VERSION} {READER_VERSION} {use_collections} "
                           f"{gazetteer}".encode("utf-8"))
        for p, did, dh in sorted(usable, key=lambda x: x[1]):
            h.update(f"doc|{did}|{dh}|{sig_for(p)}\n".encode("utf-8"))
            if p.suffix.lower() == ".csv" and _is_ledger(p):
                h.update(json.dumps(_ledger_people(p), sort_keys=True).encode("utf-8"))
        for did in sorted(failed_keep):
            h.update(f"failed|{did}\n".encode("utf-8"))
        for name, q in (
                ("status", "SELECT id, path, status FROM documents ORDER BY id"),
                ("answer", "SELECT surface_norm, document_id, entity_id FROM name_decisions"
                           " ORDER BY surface_norm, document_id"),
                ("alias", "SELECT entity_id, alias FROM aliases WHERE source='user'"
                          " ORDER BY entity_id, alias"),
                ("merge", "SELECT id, merged_into FROM entities WHERE merged_into IS NOT NULL"
                          " ORDER BY id"),
                ("decided", "SELECT id, status FROM relations WHERE status IN"
                            " ('accepted','rejected') ORDER BY id")):
            for r in self.db.execute(q):
                h.update(f"{name}|{tuple(r)}\n".encode("utf-8"))
        if use_collections:
            try:
                import vault_collections as vc
                h.update(Path(vc.CollectionStore(self.vault).path).read_bytes())
            except Exception:
                h.update(b"no collections")
        return h.hexdigest()

    #: Evidence and mention rows written per batch in a rebuild's linking pass.
    FLUSH_EVERY = 20_000

    def _seed_records(self, records, run_id: str) -> None:
        """``records()`` gives (record, document id, content hash) afresh
        each time it is called: read three times, held never."""
        # 1. Aliases first: 'Project Name' beside 'Project ID' ties the name
        #    to the code, so 'Helios Turbine Upgrade' elsewhere is PRJ-0915.
        name_to_code: Dict[Tuple[str, str], str] = {}
        for rec, _d, _h in records():
            for v in rec.values:
                if v.rule.alias_of:
                    codes = [w for w in rec.values if w.rule.type == v.rule.type
                             and not w.rule.alias_of and not w.rule.supersedes]
                    if len(codes) == 1:
                        name_to_code[(v.rule.type, fold(v.raw))] = codes[0].raw

        def project_or_part(etype: str, raw: str, source: str = "field") -> str:
            code = name_to_code.get((etype, fold(raw)))
            if code is not None:
                eid = self._entity(etype, code)
                self._alias(eid, raw, source)
                return eid
            return self._entity(etype, raw, alias_source=source)

        # 2. Full person names before initials, so 'M. Oyelaran' can find
        #    Marcus Oyelaran no matter which file was read first. An
        #    inspector's bare initials ('JB') are not a name: they are
        #    resolved (or asked about) in _person.
        persons_full: Dict[str, str] = {}
        for rec, _d, _h in records():
            for v in rec.values:
                if v.rule.type == "PERSON" and not is_initial_form(person_key(v.raw)) \
                        and not _signed_initials(v):
                    persons_full[person_key(v.raw)] = self._entity("PERSON", v.raw)

        memo = self._memo
        for rec, did, h in records():
            buf = memo.get("buffer") if memo is not None else None
            if buf and len(buf["evidence"]) + len(buf["mentions"]) >= self.FLUSH_EVERY:
                self._flush()
            ents: List[Tuple[Value, Optional[str], bool]] = []
            for v in rec.values:
                inferred = False
                if v.rule.type == "PERSON":
                    eid, inferred = self._person(v, did, run_id)
                else:
                    eid = project_or_part(v.rule.type, v.raw)
                ents.append((v, eid, inferred))
                if eid:
                    self._mention(eid, did, v.locator, v.raw, v.snippet, "field", run_id)
            self._record_relations(rec, ents, did, h, run_id)

    def _person(self, v: Value, did: str, run_id: str) -> Tuple[Optional[str], bool]:
        """(entity id or None, inferred). An initial form that fits exactly
        one known person is linked to them but flagged as inferred (its links
        stay suggested and a review asks the user); one that fits several is
        left unlinked with a review. An inspector's bare initials ('JB') go
        to _initials_person: never guessed."""
        if _signed_initials(v):
            return self._initials_person(v, did, run_id), False
        key = person_key(v.raw)
        if not is_initial_form(key):
            return self._entity("PERSON", v.raw), False
        dec = self.decided_name(v.raw, did)
        if dec is not None:
            return (self.resolve(dec) if dec else self._entity("PERSON", v.raw)), False
        confirmed = self._confirmed_alias(v.raw)
        if confirmed:
            return confirmed, False
        cands = self.initial_candidates(key)
        if len(cands) == 1:
            eid = cands[0]
            self._alias(eid, v.raw, "inferred")
            self._review("inferred_alias", v.raw, cands, did, v.locator, v.snippet, run_id)
            return eid, True
        if len(cands) > 1:
            self._review("ambiguous_name", v.raw, cands, did, v.locator, v.snippet, run_id)
            return None, False
        return self._entity("PERSON", v.raw), False

    def _initials_person(self, v: Value, did: str, run_id: str) -> Optional[str]:
        """Who signed with these initials, or None (then asked once).

        In order: the user's answer (for this document, else everywhere -
        "a different person" keeps 'JB' as a person of its own); the Ledger's
        people list (this Ledger's own for its rows; for a workbook, the list
        every Ledger in the vault agrees on); a name the user confirmed. Not
        found = no person and no link, and ONE question for these initials
        however many rows they signed: never a guess, not even when exactly
        one known person has those initials (they are offered as answers)."""
        from council_core import kg_excel
        surface = kg_excel.initials_key(v.raw)
        # Most particular first: the user's answer for THIS document; this
        # Ledger's own people list; an answer for everywhere; the list every
        # Ledger agrees on. An answer given "everywhere" for one Ledger's
        # question used to replace another Ledger's own list (review,
        # 2026-10-16: Jordan Blake's firm link became Jill Brown's).
        here = self._decided_here(surface, did)
        if here is not None:
            return self.resolve(here) if here else self._entity("PERSON", surface)
        own = self._people.get(did)
        name = own.get(surface) if own is not None else None
        dec = None if name else self.decided_name(surface, did)
        if dec is not None:
            return self.resolve(dec) if dec else self._entity("PERSON", surface)
        if not name and own is None:
            name = self._people_all.get(surface)
        if name:
            eid = self._entity("PERSON", name)
            self._alias(eid, surface, "ledger")
            return eid
        confirmed = self._confirmed_alias(surface)
        if confirmed:
            return confirmed
        self._ask_initials(surface, did, v, run_id)
        return None

    def _ask_initials(self, surface: str, did: str, v: Value, run_id: str) -> None:
        q = self._asked.get(surface)
        if q is None:
            self._review("initials", surface, [], did, v.locator, v.snippet, run_id)
            row = self.db.execute("SELECT id FROM review WHERE kind='initials' AND surface=?"
                                  " AND document_id IS ? AND locator=?",
                                  (surface, did, _loc_json(v.locator))).fetchone()
            if row is not None:
                # Answered here once, but that answer no longer stands (its
                # person is gone): the question is open again, not hidden.
                self.db.execute("UPDATE review SET status='open', run_id=? WHERE id=?",
                                (run_id, row["id"]))
            q = self._asked[surface] = {"id": row["id"] if row else None, "n": 0,
                                        "docs": set(), "first": locator_text(v.locator)}
        q["n"] += 1
        q["docs"].add(did)

    def _finish_initials(self) -> None:
        """Each initials question, once every record is read: the people
        whose initials these are (to choose from, never chosen), and how
        often the initials were seen."""
        if not self._asked:
            return
        people = []
        for r in self.db.execute("SELECT id, key FROM entities WHERE type='PERSON'"
                                 " AND merged_into IS NULL"):
            if self._live is not None and r["id"] not in self._live:
                continue
            toks = r["key"].split()
            if len(toks) >= 2:
                people.append((r["id"], "".join(t[0] for t in toks), toks[0][0] + toks[-1][0]))
        for surface, q in self._asked.items():
            if q["id"] is None:
                continue
            want = surface.lower()
            cands = sorted({self.resolve(eid) for eid, every, ends in people
                            if want in (every, ends)})
            n_docs = len(q["docs"])
            snippet = (f"'{surface}' signed {q['n']} value(s) in {n_docs} "
                       f"document{'s' if n_docs != 1 else ''}; first at {q['first']}")
            self.db.execute("UPDATE review SET candidates=?, snippet=? WHERE id=?"
                            " AND status='open'", (json.dumps(cands), snippet, q["id"]))

    def add_person(self, name: str) -> str:
        """A person the user named ('JB is Jordan Blake', someone no document
        names in full). Kept across rebuilds (a name of the user's own), so a
        name answer can point at them."""
        name = " ".join(str(name or "").split())
        if not person_key(name):
            raise ValueError("a person needs a name")
        with self.db:
            eid = self._entity("PERSON", name, alias_source="user")
            self._decide("add_person", {"entity_id": eid, "name": name})
        return eid

    def _confirmed_alias(self, surface: str) -> Optional[str]:
        """The entity the user said ``surface`` means, if they did."""
        row = self.db.execute("SELECT entity_id FROM aliases WHERE alias_norm=?"
                              " AND source='user'", (fold(surface),)).fetchone()
        return self.resolve(row[0]) if row else None

    def initial_candidates(self, key: str) -> List[str]:
        """People whose surname matches and first name starts with the
        initial: 'd whitfield' -> Dana Whitfield, Dan Whitfield.

        A spelling the user merged counts for its survivor, once: 'Dana J
        Whitfield' merged into 'Dana Whitfield' is ONE candidate. (Here the
        merged spelling was dropped, while the gazetteer counted it as a
        second person, so one 'D. Whitfield' line got both an inferred
        alias and a "which one?" question naming the survivor and her own
        merged spelling, after every rebuild - review, 2026-10-07.)"""
        ini, surname = key.split()
        out = set()
        for r in self.db.execute("SELECT id, key FROM entities WHERE type='PERSON'"):
            if self._live is not None and r["id"] not in self._live:
                continue        # left over from an earlier run (see seed)
            toks = r["key"].split()
            if (len(toks) >= 2 and toks[-1] == surname and toks[0].startswith(ini)
                    and len(toks[0]) > 1):
                out.add(self.resolve(r["id"]))
        return sorted(out)

    def _review(self, kind, surface, cands, did, loc, snippet, run_id) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO review (kind, surface, candidates, document_id,"
            " locator, snippet, run_id) VALUES (?,?,?,?,?,?,?)",
            (kind, surface, json.dumps(cands), did, _loc_json(loc), snippet, run_id))

    def _has_mention(self, eid, did, loc) -> bool:
        """Already cited at this spot (a labelled field found it first)."""
        return self.db.execute(
            "SELECT 1 FROM mentions WHERE entity_id=? AND document_id=? AND locator=?",
            (eid, did, _loc_json(loc))).fetchone() is not None

    def _mention(self, eid, did, loc, surface, snippet, method, run_id) -> None:
        eid = self.resolve(eid)
        row = (eid, did, _loc_json(loc), surface, snippet, method, run_id)
        memo = self._memo
        if memo is not None and memo.get("buffer") is not None:
            memo["buffer"]["mentions"].append(row)
        else:
            self.db.execute(_MENTION_INSERT, row)

    def _flush(self) -> None:
        """Write the evidence and mentions a rebuild's linking pass gathered
        (in the order they were found, so INSERT OR IGNORE keeps the same
        first one), with executemany: one statement per row was a third of
        a 100k-row workbook's rebuild."""
        memo = self._memo
        buf = memo.get("buffer") if memo is not None else None
        if not buf:
            return
        self.db.executemany(_EVIDENCE_INSERT, buf["evidence"])
        self.db.executemany(_MENTION_INSERT, buf["mentions"])
        buf["evidence"].clear()
        buf["mentions"].clear()

    def _record_relations(self, rec: Record, ents, did, h, run_id) -> None:
        main = {}
        inferred_ids = {eid for _v, eid, inf in ents if inf}
        ents = [(v, eid) for v, eid, _inf in ents]
        for v, eid in ents:
            if eid and v.rule.type in ("PROJECT", "PART") and not v.rule.alias_of \
                    and not v.rule.supersedes:
                main.setdefault(v.rule.type, []).append((v, eid))
        method = rec.kind
        # Whole-document co-occurrence is only firm when the document is
        # about one project; otherwise the user decides. A Ledger row is a
        # table row the user accepted.
        n_proj = len({e for _v, e in main.get("PROJECT", [])})
        firm = rec.kind in ("row", "record", "ledger") or n_proj <= 1
        status = "seeded" if firm else "suggested"

        def rel(s, pred, o, v_s: Value, v_o: Value, at: Value):
            # ``at`` is the field that DEFINES the link (the person's field,
            # the 'Supersedes' field, the part's field), so the citation
            # points at the line that says it.
            loc = at.locator
            if rec.kind in _ROW_KINDS:
                # The row, and the two cells the link rests on. The row from
                # whichever value has one: 'Inspector: JB' above a table is a
                # cell, and its links were cited with no row at all.
                loc = {k: v_s.locator.get(k) or v_o.locator.get(k) for k in ("sheet", "row")}
                loc["columns"] = list(dict.fromkeys(
                    c for v in (v_s, v_o)
                    for c in (v.locator.get("columns") or [v.locator.get("column")]) if c))
                # A workbook's cells and header row, a picture, a Ledger
                # row's flaw and where its values came from.
                cells = [v.locator["cell"] for v in (v_s, v_o) if v.locator.get("cell")]
                if cells:
                    loc["cells"] = list(dict.fromkeys(cells))
                for k in ("header", "image", "item"):
                    loc[k] = v_s.locator.get(k) or v_o.locator.get(k)
                for k in ("from", "sources"):
                    got = [v.locator[k] for v in (v_s, v_o) if v.locator.get(k)]
                    if got:
                        loc[k] = "; ".join(dict.fromkeys(got))
            quote = v_s.snippet if v_s.snippet == v_o.snippet else \
                f"{v_s.snippet} | {v_o.snippet}"
            st = "suggested" if (s in inferred_ids or o in inferred_ids
                                 or v_s.tentative or v_o.tentative) else status
            self._relate(s, pred, o, st, doc_id=did, content_hash=h,
                         locator=loc, quote=quote, method=method, run_id=run_id)

        for v, eid in ents:
            if not eid:
                continue
            if v.rule.type == "PERSON":
                for ttype, pred in v.rule.links:
                    if ttype in main:
                        for tv, tid in main[ttype]:
                            rel(eid, pred, tid, v, tv, v)
                        break
            elif v.rule.supersedes:
                for pv, pid in main.get("PART", []):
                    if pid != eid:
                        rel(pid, "SUPERSEDES", eid, pv, v, v)
        for pv, pid in main.get("PROJECT", []):
            for tv, tid in main.get("PART", []):
                rel(pid, "USES_PART", tid, pv, tv, tv)

    def _seed_collections(self, run_id: str) -> int:
        """Collections are the user's own project -> document groupings. A
        collection whose name is a known project alias (or the first words of
        exactly one project's name) links to that project; otherwise it
        becomes a project of its own."""
        try:
            import vault_collections as vc
            colls = vc.CollectionStore(self.vault).all()
        except Exception:
            return 0
        n = 0
        for c in colls:
            pid = self.find_project(c.name)
            if pid is None:
                pid = self._entity("PROJECT", c.name, alias_source="collection")
            else:
                self._alias(pid, c.name, "collection")
            for f in c.files:
                row = self.db.execute("SELECT id, content_hash FROM documents WHERE path=?"
                                      " AND status='ok'", (f,)).fetchone()
                if row is None:
                    continue
                self._relate(pid, "DOCUMENTED_IN", row["id"], "seeded",
                              doc_id=row["id"], content_hash=row["content_hash"],
                              locator={}, quote=f"Collection '{c.name}'",
                              method="collection", run_id=run_id)
                self._mention(pid, row["id"], {}, c.name, f"Collection '{c.name}'",
                              "collection", run_id)
                n += 1
        return n

    def find_project(self, name: str) -> Optional[str]:
        fn = fold(name)
        rows = self.db.execute(
            "SELECT DISTINCT a.entity_id FROM aliases a JOIN entities e ON e.id=a.entity_id"
            " WHERE e.type='PROJECT' AND a.alias_norm=?", (fn,)).fetchall()
        if len(rows) == 1:
            return rows[0][0]
        pre = set()
        for r in self.db.execute(
                "SELECT a.entity_id, a.alias_norm FROM aliases a JOIN entities e"
                " ON e.id=a.entity_id WHERE e.type='PROJECT'"):
            if fn and (r["alias_norm"] + " ").startswith(fn + " "):
                pre.add(r["entity_id"])
        return pre.pop() if len(pre) == 1 else None

    def _gazetteer(self, files: Sequence[Path], run_id: str, prog=None) -> int:
        """Plain search of every text document for every known name and code,
        so a person or part is found where no label points at it. Mentions
        only — a link needs a label, a Collection, the user or (KG2) a model.
        Initial forms ('D. Whitfield') that fit two people open a review."""
        terms: List[Tuple[re.Pattern, str, str]] = []
        for r in self.db.execute(
                "SELECT a.entity_id, a.alias FROM aliases a JOIN entities e"
                " ON e.id=a.entity_id WHERE e.type IN ('PERSON','PART','PROJECT')"
                " AND e.merged_into IS NULL"):
            alias = r["alias"]
            if len(alias) < 4 or len(alias.split()) == 1 and not looks_like_code(alias):
                continue        # single bare words are too noisy to search
            terms.append((None, r["entity_id"], alias))
        # Index the terms by their first token. A term can match a line only
        # where that token is a whole token of the line (the term's own
        # boundaries guarantee it), so each line is tested against the few
        # terms whose first token it holds instead of every term — MEASURED:
        # one regex per alias per line was 12.1 M searches and 67 of a 104 s
        # rebuild on 310 files. Same matches, in the same order.
        by_token: Dict[str, List[int]] = {}
        always: List[int] = []
        for i, (_rx, _eid, alias) in enumerate(terms):
            m = _TOKEN_RE.match(alias)
            if m:
                by_token.setdefault(m.group(0).lower(), []).append(i)
            else:
                always.append(i)
        # Initial forms of every full-named person: 'D. Whitfield'. A merged
        # spelling counts for its survivor, once (see initial_candidates).
        initials: Dict[str, set] = {}
        for r in self.db.execute("SELECT id, key FROM entities WHERE type='PERSON'"):
            if self._live is not None and r["id"] not in self._live:
                continue        # left over from an earlier run (see seed)
            toks = r["key"].split()
            if len(toks) >= 2 and len(toks[0]) > 1:
                initials.setdefault(f"{toks[0][0]} {toks[-1]}", set()).add(
                    self.resolve(r["id"]))
        n = 0
        for p in files:
            if prog is not None:
                prog.advance()
            # A CSV is read by column. A workbook's free text (notes cells,
            # text boxes, pictures) is searched like any document's lines.
            if p.suffix.lower() in _CSV:
                continue
            # Not a file this install cannot read (no pypdf): it has no
            # lines to search, and reading it again only to find that out
            # was the one read left in an unchanged vault's rebuild.
            row = self.db.execute("SELECT id, content_hash FROM documents WHERE path=?"
                                  " AND status='ok'", (self._rel(p),)).fetchone()
            if row is None:
                continue
            did = row["id"]
            # The lines the reading phase cached: an unchanged document is
            # not read again for the gazetteer either.
            for page, line, text in self._cached_lines(did, row["content_hash"], p):
                loc = {"line": line}
                if page is not None:
                    loc["page"] = page
                cand = set(always)
                for tok in _TOKEN_RE.findall(text):
                    cand.update(by_token.get(tok.lower(), ()))
                for i in sorted(cand):
                    rx, eid, alias = terms[i]
                    if rx is None:
                        # compiled the first time a line holds its first
                        # word: 20,000 part numbers were 20,000 compiles
                        # (6.4 s) per rebuild, nearly all never used
                        rx = _term_re(alias)
                        terms[i] = (rx, eid, alias)
                    if rx.search(text) and not self._has_mention(eid, did, loc):
                        self._mention(eid, did, loc, alias, _snip(text), "gazetteer", run_id)
                        n += 1
                for m in _INITIAL_NAME_RE.finditer(text):
                    key = f"{m.group(1).lower()} {fold(m.group(2))}"
                    dec = self.decided_name(m.group(0), did)
                    if dec is not None:
                        if dec and not self._has_mention(dec, did, loc):
                            self._mention(dec, did, loc, m.group(0), _snip(text),
                                          "gazetteer", run_id)
                            n += 1
                        continue
                    cands = sorted(initials.get(key, []))
                    if len(cands) == 1:
                        if not self._has_mention(cands[0], did, loc):
                            self._mention(cands[0], did, loc, m.group(0), _snip(text),
                                          "gazetteer", run_id)
                            n += 1
                    elif len(cands) > 1:
                        self._review("ambiguous_name", m.group(0), cands, did, loc,
                                     _snip(text), run_id)
        return n

    # ── the user's answers (KG3) ──
    def decided_name(self, surface: str, document_id: str) -> Optional[str]:
        """What the user said ``surface`` means in this document: an entity
        id, '' for "neither, a different person", or None (not asked yet).
        An answer for this document beats one for everywhere.

        Answers are keyed by person_key, so 'Whitfield, D.' is the same
        question as 'D. Whitfield' (they were keyed by fold() and the
        'everywhere' answer missed the surname-first spelling); rows an
        earlier Council wrote under fold() are still found. An answer naming
        a person a rebuild has since deleted (no document names her any
        more) counts as NOT answered, so the question comes back: it used to
        hand out the deleted id, giving links and mentions with no entity."""
        for doc in (document_id or "", ""):
            for key in dict.fromkeys((_name_key(surface), fold(surface))):
                row = self.db.execute("SELECT entity_id FROM name_decisions WHERE"
                                      " surface_norm=? AND document_id=?",
                                      (key, doc)).fetchone()
                if row is None:
                    continue
                if row[0] and self.entity(self.resolve(row[0])) is None:
                    return None
                return row[0]
        return None

    def _decided_here(self, surface: str, document_id: str) -> Optional[str]:
        """decided_name for this document only (not an 'everywhere' answer)."""
        if not document_id:
            return None
        for key in dict.fromkeys((_name_key(surface), fold(surface))):
            row = self.db.execute("SELECT entity_id FROM name_decisions WHERE"
                                  " surface_norm=? AND document_id=?",
                                  (key, document_id)).fetchone()
            if row is None:
                continue
            if row[0] and self.entity(self.resolve(row[0])) is None:
                return None
            return row[0]
        return None

    def answers(self) -> List[Dict[str, Any]]:
        """The user's name answers, newest first, each with what it says
        ('D. Whitfield' is Dana Whitfield, in which document) — so a wrong
        one can be found and forgotten."""
        said: Dict[Tuple[str, str], str] = {}
        for d in self.decisions():
            if d["action"] == "name":
                p = d["payload"]
                surface, doc = p.get("surface", ""), p.get("document_id") or ""
                # Under both keys: an older Council stored the answer under
                # fold(), and its row was listed as 'whitfield d', not as
                # the name the user answered about.
                for key in dict.fromkeys((_name_key(surface), fold(surface))):
                    said[(key, doc)] = surface
        out = []
        for r in self.db.execute(
                "SELECT nd.*, d.path FROM name_decisions nd LEFT JOIN documents d ON"
                " d.id=nd.document_id ORDER BY nd.ts DESC"):
            ent = self.entity(self.resolve(r["entity_id"])) if r["entity_id"] else None
            out.append({"key": r["surface_norm"], "document_id": r["document_id"],
                        "surface": said.get((r["surface_norm"], r["document_id"]),
                                            r["surface_norm"]),
                        "entity_id": r["entity_id"],
                        "name": (ent["name"] if ent else
                                 "a different person" if not r["entity_id"] else "(gone)"),
                        "path": r["path"] or "", "everywhere": not r["document_id"]})
        return out

    def forget_answer(self, key: str, document_id: str = "") -> None:
        """Undo one name answer: the question is asked again at the next
        rebuild. Logged as 'name_undo' (the log itself is never edited)."""
        with self.db:
            n = self.db.execute("DELETE FROM name_decisions WHERE surface_norm=? AND"
                                " document_id=?", (key, document_id or "")).rowcount
            if not n:
                raise KeyError(key)
            # The question's own row is 'resolved', and a rebuild re-asks
            # with INSERT OR IGNORE on the same (kind, surface, document,
            # spot): it would stay answered. It goes, so it is asked again.
            # ``key`` may be an older Council's fold() key ('whitfield d' for
            # 'Whitfield, D.'): matched only by person_key, its question
            # stayed answered and the name stayed unlinked for good.
            for r in self.db.execute("SELECT id, surface, document_id FROM review"
                                     " WHERE status='resolved'").fetchall():
                if key in (_name_key(r["surface"]), fold(r["surface"])) and (
                        not document_id or r["document_id"] == document_id):
                    self.db.execute("DELETE FROM review WHERE id=?", (r["id"],))
            self._decide("name_undo", {"key": key, "document_id": document_id or None})

    def answer_review(self, review_id: int, entity_id: Optional[str], *,
                      everywhere: bool = False) -> None:
        """Settle an open question. ``entity_id`` None = neither (the name is
        a different person). Kept in the decision log and across rebuilds;
        call seed() to apply it."""
        r = self.db.execute("SELECT * FROM review WHERE id=?", (review_id,)).fetchone()
        if r is None:
            raise KeyError(review_id)
        if entity_id is not None and self.entity(entity_id) is None:
            raise KeyError(entity_id)
        doc = "" if everywhere else (r["document_id"] or "")
        with self.db:
            # An older row under the fold() key would shadow nothing (the
            # person_key row is looked up first) but would come back after
            # this one is forgotten.
            self.db.execute("DELETE FROM name_decisions WHERE surface_norm=? AND"
                            " document_id=?", (fold(r["surface"]), doc))
            self.db.execute("INSERT OR REPLACE INTO name_decisions VALUES (?,?,?,?)",
                            (_name_key(r["surface"]), doc, entity_id or "", time.time()))
            q = "UPDATE review SET status='resolved' WHERE status='open' AND surface=?"
            args: tuple = (r["surface"],)
            if not everywhere:
                q += " AND document_id IS ?"
                args += (r["document_id"],)
            self.db.execute(q, args)
            self.db.execute("UPDATE review SET status='resolved' WHERE id=?", (review_id,))
            self._decide("name", {"surface": r["surface"], "entity_id": entity_id,
                                  "document_id": doc or None, "review_id": review_id})

    #: Statuses only the user sets. A merge never copies one over another
    #: status, and an unmerge gives each back to the entry it belonged to.
    _USER_STATUSES = ("accepted", "rejected")

    def merge(self, keep_id: str, absorb_id: str) -> List[Dict[str, Any]]:
        """``absorb_id`` is the same thing as ``keep_id`` (two spellings of one
        person, a part listed twice). Its aliases, mentions, links and the
        user's decisions on them move to ``keep_id``; undo with `unmerge`.

        Returns the CLASHES: links both entries had where the absorbed one
        carries a user decision (accepted / rejected) that differs from the
        survivor's status. The survivor's status is KEPT — the user decided
        about the other entry's link, not this one — and the caller asks.
        MEASURED (review, 2026-10-07): a rejected model link on a duplicate
        'Caroline Lee' made Carol Lee's own labelled link 'rejected', it stayed
        rejected after unmerge and rebuild, and neighbors() hid it for good.

        Everything the merge changes is recorded in its decision (moved and
        created links with their old statuses, links that became loops — an
        accepted 'B supersedes A' once A is B — and their evidence, aliases
        copied) so `unmerge` can put it back."""
        keep_id, absorb_id = self.resolve(keep_id), self.resolve(absorb_id)
        k, a = self.entity(keep_id), self.entity(absorb_id)
        if k is None or a is None:
            raise KeyError(keep_id if k is None else absorb_id)
        if keep_id == absorb_id:
            raise ValueError("that is the same entity")
        if k["type"] != a["type"] or k["type"] == "DOCUMENT":
            raise ValueError(f"cannot merge a {a['type']} into a {k['type']}")
        changes: Dict[str, List[Any]] = {"relations": [], "loops": [], "aliases": []}
        clashes: List[Dict[str, Any]] = []
        with self.db:
            # Entries merged into ``absorb`` earlier stay merged into IT
            # (resolve() follows the chain): splitting ``absorb`` off later
            # keeps them with it. They used to be re-pointed at ``keep``.
            self.db.execute("UPDATE entities SET merged_into=? WHERE id=?", (keep_id, absorb_id))
            for al in self.db.execute("SELECT * FROM aliases WHERE entity_id=?",
                                      (absorb_id,)).fetchall():
                if self.db.execute("SELECT 1 FROM aliases WHERE entity_id=? AND alias=?",
                                   (keep_id, al["alias"])).fetchone() is None:
                    self.db.execute("INSERT INTO aliases VALUES (?,?,?,?)",
                                    (keep_id, al["alias"], al["alias_norm"], al["source"]))
                    changes["aliases"].append([al["alias"], al["source"]])
            # Mentions are rebuilt by every seed; no record needed.
            self.db.execute("UPDATE OR IGNORE mentions SET entity_id=? WHERE entity_id=?",
                            (keep_id, absorb_id))
            self.db.execute("DELETE FROM mentions WHERE entity_id=?", (absorb_id,))
            for r in self.db.execute("SELECT * FROM relations WHERE subject_id=? OR object_id=?",
                                     (absorb_id, absorb_id)).fetchall():
                old = dict(r)
                ev = [dict(e) for e in self.db.execute(
                    "SELECT * FROM evidence WHERE relation_id=?", (r["id"],))]
                s_ = keep_id if r["subject_id"] == absorb_id else r["subject_id"]
                o_ = keep_id if r["object_id"] == absorb_id else r["object_id"]
                if s_ == o_:
                    changes["loops"].append({"old": old, "evidence": ev})
                else:
                    new_id = relation_id(s_, r["predicate"], o_)
                    have = self.db.execute("SELECT status FROM relations WHERE id=?",
                                           (new_id,)).fetchone()
                    entry = {"old": old, "new_id": new_id, "created": have is None,
                             "status": r["status"], "evidence": ev, "moved": []}
                    if have is None:
                        self.db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?,?)",
                                        (new_id, s_, r["predicate"], o_, r["status"],
                                         r["created_ts"], time.time()))
                    elif (r["status"] != have["status"]
                          and r["status"] in self._USER_STATUSES):
                        clashes.append({"relation_id": new_id, "predicate": r["predicate"],
                                        "subject_id": s_, "object_id": o_,
                                        "kept": have["status"], "theirs": r["status"]})
                    for e in ev:
                        if self.db.execute("UPDATE OR IGNORE evidence SET relation_id=?"
                                           " WHERE id=?", (new_id, e["id"])).rowcount:
                            entry["moved"].append(e["id"])
                    changes["relations"].append(entry)
                self.db.execute("DELETE FROM evidence WHERE relation_id=?", (r["id"],))
                self.db.execute("DELETE FROM relations WHERE id=?", (r["id"],))
            self._decide("merge", {"keep": keep_id, "absorb": absorb_id,
                                   "keep_name": k["name"], "absorb_name": a["name"],
                                   "changes": changes})
        return clashes

    def _merge_record(self, absorb_id: str) -> Optional[Dict[str, Any]]:
        """The latest merge decision that absorbed ``absorb_id``."""
        for r in self.db.execute("SELECT payload FROM decisions WHERE action='merge'"
                                 " ORDER BY id DESC"):
            p = json.loads(r[0])
            if p.get("absorb") == absorb_id:
                return p
        return None

    def unmerge(self, absorb_id: str) -> None:
        """Undo a merge: the entry is its own again, with the links the merge
        moved, the links it turned into loops, and the user's decisions on
        them, as they were; the names the merge copied go back. Links from
        labels and Collections are worked out again at the next rebuild;
        links a model suggested WHILE merged stay with the survivor. (A merge
        an older Council recorded without its changes only comes apart.)"""
        row = self.db.execute("SELECT merged_into FROM entities WHERE id=?",
                              (absorb_id,)).fetchone()
        if row is None or not row["merged_into"]:
            raise ValueError("that entity is not merged into another")
        keep_id = row["merged_into"]
        rec = self._merge_record(absorb_id) or {}
        with self.db:
            self.db.execute("UPDATE entities SET merged_into=NULL WHERE id=?", (absorb_id,))
            ch = rec.get("changes") if rec.get("keep") == keep_id else None
            if ch:
                self._undo_merge(keep_id, ch)
            self._decide("unmerge", {"entity": absorb_id, "was_in": keep_id})

    def _undo_merge(self, keep_id: str, ch: Dict[str, Any]) -> None:
        cols = ("id", "subject_id", "predicate", "object_id", "status", "created_ts",
                "updated_ts")
        ecols = ("id", "relation_id", "document_id", "content_hash", "locator", "quote",
                 "method", "model", "run_id")

        def put_back(rel: Dict[str, Any], evidence: List[Dict[str, Any]]) -> None:
            self.db.execute("INSERT OR IGNORE INTO relations VALUES (?,?,?,?,?,?,?)",
                            tuple(rel[c] for c in cols))
            for e in evidence:
                # Still there (moved onto the survivor's link): move it back.
                # Gone (a duplicate the merge dropped, or a rebuild re-made
                # it): the recorded row; the next rebuild drops it again if
                # it is stale or derived.
                if not self.db.execute("UPDATE OR IGNORE evidence SET relation_id=? WHERE"
                                       " id=?", (rel["id"], e["id"])).rowcount:
                    self.db.execute(
                        f"INSERT OR IGNORE INTO evidence ({', '.join(ecols)}) VALUES"
                        f" ({', '.join('?' * len(ecols))})",
                        tuple({**e, "relation_id": rel["id"]}[c] for c in ecols))

        for alias, source in ch.get("aliases", []):
            self.db.execute("DELETE FROM aliases WHERE entity_id=? AND alias=? AND source=?",
                            (keep_id, alias, source))
        for loop in ch.get("loops", []):
            put_back(loop["old"], loop["evidence"])
        for entry in ch.get("relations", []):
            put_back(entry["old"], entry["evidence"])
            if not entry["created"]:
                continue
            cur = self.db.execute("SELECT status FROM relations WHERE id=?",
                                  (entry["new_id"],)).fetchone()
            if cur is None:
                continue
            left = self.db.execute("SELECT COUNT(*) FROM evidence WHERE relation_id=?",
                                   (entry["new_id"],)).fetchone()[0]
            carried = cur["status"] == entry["status"]
            if cur["status"] in self._USER_STATUSES and not carried:
                continue            # the user decided on the merged link itself
            if not left:
                self.db.execute("DELETE FROM relations WHERE id=?", (entry["new_id"],))
            elif cur["status"] in self._USER_STATUSES:
                # The decision belonged to the split-off entry's link; what
                # is left here is the survivor's own evidence: a suggestion
                # the next rebuild works out again.
                self.db.execute("UPDATE relations SET status='suggested', updated_ts=?"
                                " WHERE id=?", (time.time(), entry["new_id"]))

    def merged_into_me(self, eid: str) -> List[Dict[str, Any]]:
        return [self.entity(r[0]) for r in self.db.execute(
            "SELECT id FROM entities WHERE merged_into=?", (eid,))]

    # ── free-text extraction (KG2) ──
    def _text_documents(self) -> List[sqlite3.Row]:
        """The readable documents whose free text may hold links (CSV tables
        and JSON give theirs through labelled fields; a workbook's notes,
        text boxes and pictures are free text)."""
        return [d for d in self.db.execute(
                    "SELECT id, path, content_hash FROM documents WHERE status='ok'"
                    " ORDER BY path").fetchall()
                if Path(d["path"]).suffix.lower() not in _CSV | _JSON]

    def _doc_lines(self, d) -> Optional[List[Tuple[Optional[int], int, str]]]:
        try:
            return self._cached_lines(d["id"], d["content_hash"], self.abs_path(d["path"]))
        except Exception:
            return None

    def _doc_context(self, d) -> Tuple[List[Tuple[str, str]], set]:
        """The document's mentions as (entity id, locator JSON), and every
        entity tied to it: mentioned anywhere in it, or the project of its
        Collection."""
        ments = [(m[0], m[1]) for m in self.db.execute(
            "SELECT entity_id, locator FROM mentions WHERE document_id=?", (d["id"],))]
        doc_ents = {e for e, _l in ments}
        doc_ents |= {r[0] for r in self.db.execute(
            "SELECT subject_id FROM relations WHERE predicate='DOCUMENTED_IN' AND object_id=?",
            (d["id"],))}
        return ments, doc_ents

    @staticmethod
    def _doc_sig(d, ments, doc_ents, terms_sig: str, max_lines: int, overlap: int) -> str:
        """Everything a document's passages are worked out from. Unchanged =
        the same passages, so a finished document need not be read again."""
        h = hashlib.sha256()
        for part in ([d["content_hash"], terms_sig, str(max_lines), str(overlap)]
                     + sorted(f"{e}@{loc}" for e, loc in ments) + sorted(doc_ents)):
            h.update(part.encode("utf-8"))
            h.update(b"\0")
        return h.hexdigest()

    def _doc_chunks(self, d, lines, ments, doc_ents, index, max_lines: int,
                    overlap: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        by_spot: Dict[Tuple[Optional[int], int], set] = {}
        for eid, locj in ments:
            loc = json.loads(locj)
            if loc.get("line"):
                by_spot.setdefault((loc.get("page"), int(loc["line"])), set()).add(eid)
        groups: Dict[Optional[int], List[Tuple[int, str]]] = {}
        for page, line, text in lines:
            groups.setdefault(page, []).append((line, text))
        n = 0
        for page, plines in groups.items():
            step = max(1, max_lines - overlap)
            for start in range(0, max(1, len(plines)), step):
                window = plines[start:start + max_lines]
                if not window:
                    break
                ents = set()
                for line, _t in window:
                    ents |= by_spot.get((page, line), set())
                wtext = "\n".join(t for _l, t in window)
                named_projects = self._projects_named(wtext, index)
                if len(ents) >= 2 or (ents and named_projects - ents):
                    ents |= named_projects
                    out.append({"document_id": d["id"], "path": d["path"],
                                "content_hash": d["content_hash"], "chunk": n,
                                "page": page, "first_line": window[0][0],
                                # A workbook's text lines are rows that need
                                # not be next to each other (row 3, then 9):
                                # the passage's Nth line is row line_nos[N-1].
                                "line_nos": [ln for ln, _t in window],
                                "text": wtext,
                                # the chunk's own entities first
                                "entities": (sorted(ents) + sorted(doc_ents - ents))[:60]})
                n += 1
                if start + max_lines >= len(plines):
                    break
        return out

    def text_chunks(self, max_lines: int = 40, overlap: int = 5
                    ) -> List[Dict[str, Any]]:
        """Every text chunk worth a model call: a PDF page, or ~40 lines with
        a small overlap, that mentions at least TWO known entities (the
        gazetteer and labelled-field mentions say which). Tables are skipped
        — their links come from labelled columns."""
        index = self._project_index()
        out: List[Dict[str, Any]] = []
        for d in self._text_documents():
            lines = self._doc_lines(d)
            if lines is None:
                continue
            ments, doc_ents = self._doc_context(d)
            out += self._doc_chunks(d, lines, ments, doc_ents, index, max_lines, overlap)
        return out

    _COMMON_FIRST_WORDS = {"project", "program", "programme", "the", "new", "phase",
                           "test", "line", "pump", "turbine", "upgrade", "retrofit"}

    def _project_terms(self) -> List[Tuple["re.Pattern", str]]:
        """Projects by every alias, ONE-WORD ones included, plus the
        distinctive first word of a multi-word name ('Atlas' of 'Atlas Test
        Rig'): prose names projects that way. These only OFFER a project to
        the model — the checks decide what survives; a one-word MENTION
        would be too noisy, so the gazetteer does not do this."""
        return [(_term_re(w), pid) for w, pid in self._project_words()]

    def _project_words(self) -> List[Tuple[str, str]]:
        words_out, seen = [], set()
        for r in self.db.execute(
                "SELECT a.entity_id, a.alias FROM aliases a JOIN entities e ON"
                " e.id=a.entity_id WHERE e.type='PROJECT' AND e.merged_into IS NULL"
                " ORDER BY a.entity_id, a.alias"):
            words = [r["alias"]]
            first = r["alias"].split()[0] if " " in r["alias"] else ""
            if (len(first) >= 5 and first[:1].isupper()
                    and first.lower() not in self._COMMON_FIRST_WORDS):
                words.append(first)
            for w in words:
                if len(w) >= 4 and (w.lower(), r["entity_id"]) not in seen:
                    seen.add((w.lower(), r["entity_id"]))
                    words_out.append((w, r["entity_id"]))
        return words_out

    def _project_index(self) -> Dict[str, Any]:
        """The project terms indexed by their first token, as the gazetteer
        indexes names: a passage is tested only against the terms whose
        first word it holds. MEASURED (review, 3,000 documents): one search
        per term per passage was 348k searches and most of what was left of
        a 120 s pass. Not one alternation regex: Python takes the FIRST
        alternative that matches at a spot, so a project called 'Atlas Test'
        inside another's 'Atlas Test Rig' would no longer be offered."""
        by_token: Dict[str, List[Tuple["re.Pattern", str]]] = {}
        always: List[Tuple["re.Pattern", str]] = []
        words = self._project_words()
        for w, pid in words:
            m = _TOKEN_RE.match(w)
            (by_token.setdefault(m.group(0).lower(), []) if m else always).append(
                (_term_re(w), pid))
        sig = hashlib.sha256("\0".join(f"{w.lower()}|{pid}" for w, pid in sorted(words))
                             .encode("utf-8")).hexdigest()
        return {"by_token": by_token, "always": always, "sig": sig}

    @staticmethod
    def _projects_named(text: str, index: Dict[str, Any]) -> set:
        cands = list(index["always"])
        for tok in set(t.lower() for t in _TOKEN_RE.findall(text)):
            cands += index["by_token"].get(tok, [])
        return {pid for rx, pid in cands if rx.search(text)}

    def known_entities(self, ids: Iterable[str]):
        """kg_extract.Known for these entity ids (merged ones resolved)."""
        from council_core import kg_extract as kx
        out = []
        for eid in dict.fromkeys(self.resolve(i) for i in ids):
            e = self.entity(eid)
            if e and e["type"] in ("PERSON", "PART", "PROJECT"):
                out.append(kx.Known(eid, e["type"], e["name"], list(e["aliases"])))
        return out

    def suggest_from_text(self, workers: Dict[str, Any], *, model: str,
                          on_progress=None, should_stop=lambda: False,
                          only_new: bool = True, max_lines: int = 40,
                          overlap: int = 5) -> Dict[str, Any]:
        """_suggest_from_text, with progress lines in the terminal (finding
        passages, then each passage a model has read, with %, and a "still
        working" line while a model is thinking)."""
        from progress_report import Progress
        with Progress(f"kg suggest links ({model})") as prog:
            token = _PROGRESS.set(prog)
            try:
                stats = self._suggest_from_text(
                    workers, model=model, on_progress=on_progress, should_stop=should_stop,
                    only_new=only_new, max_lines=max_lines, overlap=overlap)
            finally:
                _PROGRESS.reset(token)
            prog.finish(f"{stats['done']} of {stats['chunks']} passages read, "
                        f"{stats['links']} link(s) suggested"
                        + (" - stopped, run again to continue" if stats["stopped"] else ""))
            return stats

    def _suggest_from_text(self, workers: Dict[str, Any], *, model: str,
                           on_progress=None, should_stop=lambda: False,
                           only_new: bool = True, max_lines: int = 40,
                           overlap: int = 5) -> Dict[str, Any]:
        """Ask a model for links in the free text; every surviving link lands
        as SUGGESTED (method 'model', the model's name, the quote, the line
        the Council found). ``workers`` maps a worker name ('this PC',
        'NodePrimus'…) to a kg_extract chat function — which reads with
        ``model`` — or to ``(chat, its own model)``: a Pi reads with the
        smaller model it has. Chunks are shared out through one queue and
        the results written here, by one thread. ``model`` names the run.

        Resumable: a passage counts as done when a model of THIS run's
        workers read this version of it without an error. Each passage is
        recorded under the model that actually read it — a Pi's llama3.2:3b
        passages were recorded as the PC's gemma3:12b's, so the stronger
        model never read them — and one that failed (Ollama not running) is
        read again next time. A document whose passages are all done and
        whose signature (content, mentions, Collection, project names) is
        unchanged is not read at all. ``should_stop()`` pauses after the
        chunks in flight."""
        import queue as _queue
        import threading as _threading
        from council_core import kg_extract as kx

        wk: Dict[str, Tuple[Any, str]] = {
            name: (w, model) if callable(w) else (w[0], str(w[1]))
            for name, w in workers.items()}
        models = sorted({m for _c, m in wk.values()})
        marks = ",".join("?" * len(models))

        def is_done(doc_id: str, h: str, chunk: int) -> bool:
            return self.db.execute(
                "SELECT 1 FROM extraction_done WHERE document_id=? AND content_hash=? AND"
                f" chunk=? AND error='' AND model IN ({marks})",
                (doc_id, h, chunk, *models)).fetchone() is not None

        run_id = str(uuid.uuid4())
        index = self._project_index()
        todo = []
        read = skipped = 0
        prog = progress_now()
        docs = self._text_documents()
        if prog is not None:
            prog.stage("finding passages", total=len(docs), unit="documents")
        with self.db:
            for d in docs:
                if prog is not None:
                    prog.advance(what=self.display_path(d["path"]))
                ments, doc_ents = self._doc_context(d)
                sig = self._doc_sig(d, ments, doc_ents, index["sig"], max_lines, overlap)
                prev = self.db.execute("SELECT sig, chunks FROM extraction_docs WHERE"
                                       " document_id=?", (d["id"],)).fetchone()
                if (only_new and prev is not None and prev["sig"] == sig
                        and all(is_done(d["id"], d["content_hash"], n)
                                for n in json.loads(prev["chunks"]))):
                    skipped += 1
                    continue
                lines = self._doc_lines(d)
                if lines is None:
                    continue
                read += 1
                chunks = self._doc_chunks(d, lines, ments, doc_ents, index, max_lines,
                                          overlap)
                self.db.execute("INSERT OR REPLACE INTO extraction_docs VALUES (?,?,?)",
                                (d["id"], sig, json.dumps([c["chunk"] for c in chunks])))
                for c in chunks:
                    if only_new and is_done(c["document_id"], c["content_hash"], c["chunk"]):
                        continue
                    c["known"] = self.known_entities(c["entities"])
                    todo.append(c)
        stats = {"chunks": len(todo), "done": 0, "links": 0, "rejected": 0, "errors": 0,
                 "stopped": False, "by_worker": {}, "documents_read": read,
                 "documents_skipped": skipped}
        with self.db:
            self.db.execute("INSERT INTO runs (id, kind, started_ts) VALUES (?,?,?)",
                            (run_id, f"extract:{model}", time.time()))
        if prog is not None:
            prog.stage("asking the model", total=len(todo), unit="passages")
        jobs: "_queue.Queue" = _queue.Queue()
        for c in todo:
            jobs.put(c)
        results: "_queue.Queue" = _queue.Queue()

        def work(name, chat, wmodel):
            while not should_stop():
                try:
                    c = jobs.get_nowait()
                except _queue.Empty:
                    return
                res = kx.extract(c["text"], c["known"], chat)
                results.put((name, wmodel, c, res))

        threads = [_threading.Thread(target=work, args=(n, ch, m), name=f"kg-extract-{n}",
                                     daemon=True) for n, (ch, m) in wk.items()]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads) or not results.empty():
            try:
                name, wmodel, c, res = results.get(timeout=0.2)
            except _queue.Empty:
                continue
            with self.db:
                for link in res.links:
                    nos = c.get("line_nos") or []
                    k = (link.line or 1) - 1
                    line = nos[k] if 0 <= k < len(nos) else (c["first_line"] + k)
                    loc = {"line": line}
                    if c["page"] is not None:
                        loc["page"] = c["page"]
                    quote = link.quote + (f"  [{link.fixed}]" if link.fixed else "")
                    self._relate(link.subject, link.predicate, link.object, "suggested",
                                 doc_id=c["document_id"], content_hash=c["content_hash"],
                                 locator=loc, quote=quote, method="model", run_id=run_id,
                                 model=f"{wmodel} @ {name}")
                self.db.execute(
                    "INSERT OR REPLACE INTO extraction_done VALUES (?,?,?,?,?,?,?)",
                    (c["document_id"], c["content_hash"], c["chunk"], wmodel, time.time(),
                     len(res.links), res.error))
            stats["done"] += 1
            stats["links"] += len(res.links)
            stats["rejected"] += len(res.rejected)
            stats["errors"] += bool(res.error)
            stats["by_worker"][name] = stats["by_worker"].get(name, 0) + 1
            if prog is not None:
                prog.advance(what=f"{self.display_path(c['path'])} read by {name}")
            if on_progress:
                on_progress(stats["done"], stats["chunks"], name)
        stats["stopped"] = stats["done"] < stats["chunks"]
        with self.db:
            self.db.execute("UPDATE runs SET finished_ts=?, stats=? WHERE id=?",
                            (time.time(), json.dumps(stats), run_id))
        return stats

    # ── reading the graph ──
    def counts(self) -> Dict[str, Any]:
        q = self.db.execute
        by_type = {r[0]: r[1] for r in q("SELECT type, COUNT(*) FROM entities"
                                         " WHERE merged_into IS NULL GROUP BY type")}
        by_status = {r[0]: r[1] for r in q("SELECT status, COUNT(*) FROM relations"
                                           " GROUP BY status")}
        return {"entities": by_type, "relations": by_status,
                "mentions": q("SELECT COUNT(*) FROM mentions").fetchone()[0],
                "open_reviews": q("SELECT COUNT(*) FROM review WHERE status='open'")
                .fetchone()[0]}

    def entity(self, eid: str) -> Optional[Dict[str, Any]]:
        r = self.db.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        if d["type"] == "DOCUMENT":
            doc = self.db.execute("SELECT path FROM documents WHERE id=?", (eid,)).fetchone()
            d["name"] = doc["path"] if doc else d["name"]
        d["aliases"] = [a["alias"] for a in self.db.execute(
            "SELECT alias FROM aliases WHERE entity_id=? ORDER BY alias", (eid,))]
        return d

    def search(self, text: str, etype: Optional[str] = None, limit: int = 50
               ) -> List[Dict[str, Any]]:
        """Entities whose name or any alias contains ``text`` (accent- and
        case-insensitive), best first: exact, then prefix, then substring."""
        # 'Lee, Carol' also searches as 'Carol Lee', whichever was seen.
        forms = {f for f in (fold(text), fold(person_display(text))) if f}
        if not forms:
            return []
        best: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        for ft in forms:
            q = ("SELECT DISTINCT e.id, e.type, e.name, a.alias_norm FROM aliases a"
                 " JOIN entities e ON e.id=a.entity_id WHERE e.merged_into IS NULL"
                 " AND a.alias_norm LIKE ?")
            # fold() leaves only [a-z0-9 ], so no LIKE wildcard can be in ft.
            args: List[Any] = [f"%{ft}%"]
            if etype:
                q += " AND e.type=?"
                args.append(etype)
            for r in self.db.execute(q, args):
                score = (0 if r["alias_norm"] == ft
                         else 1 if r["alias_norm"].startswith(ft) else 2)
                cur = best.get(r["id"])
                if cur is None or score < cur[0]:
                    best[r["id"]] = (score, {"id": r["id"], "type": r["type"],
                                             "name": r["name"]})
        ranked = sorted(best.values(), key=lambda x: (x[0], x[1]["name"].lower()))
        return [d for _s, d in ranked[:limit]]

    def neighbors(self, eid: str, *, include_rejected: bool = False,
                  evidence_limit: Optional[int] = None,
                  relation_limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Every relation touching ``eid``, each with its evidence: what a
        person needs to see WHY two things are linked. ``evidence_limit``
        keeps the first N pieces of each (``evidence_total`` is the count):
        an inspector who signed 30 flaws on 3,000 parts has 90,000, and the
        Connections tab drew every one on the GUI thread. ``relation_limit``
        keeps the first N links of each kind (predicate and direction);
        neighbor_counts() counts them all. MEASURED (review, 2026-10-16): a
        person with 4,000 links took 3.4 s here while the tab drew 300."""
        eid = self.resolve(eid)
        q = ("SELECT * FROM relations WHERE (subject_id=? OR object_id=?)")
        if not include_rejected:
            q += " AND status != 'rejected'"
        out = []
        per_kind: Dict[Tuple[str, str], int] = {}
        for r in self.db.execute(q + " ORDER BY predicate", (eid, eid)).fetchall():
            kind = (r["predicate"], "out" if r["subject_id"] == eid else "in")
            per_kind[kind] = per_kind.get(kind, 0) + 1
            if relation_limit is not None and per_kind[kind] > relation_limit:
                continue
            other = r["object_id"] if r["subject_id"] == eid else r["subject_id"]
            ev = self.evidence(r["id"], limit=evidence_limit)
            total = len(ev) if evidence_limit is None or len(ev) < evidence_limit else \
                self.db.execute("SELECT COUNT(*) FROM evidence WHERE relation_id=?",
                                (r["id"],)).fetchone()[0]
            out.append({"relation_id": r["id"], "predicate": r["predicate"],
                        "direction": "out" if r["subject_id"] == eid else "in",
                        "status": r["status"], "other": self.entity(other),
                        "evidence": ev, "evidence_total": total})
        return out

    def neighbor_counts(self, eid: str, *, include_rejected: bool = False
                        ) -> Dict[Tuple[str, str], int]:
        """How many links ``eid`` has of each kind: {(predicate, 'out'|'in'): n}."""
        eid = self.resolve(eid)
        q = ("SELECT predicate, subject_id=? AS outward, COUNT(*) FROM relations"
             " WHERE (subject_id=? OR object_id=?)")
        if not include_rejected:
            q += " AND status != 'rejected'"
        return {(r[0], "out" if r[1] else "in"): r[2]
                for r in self.db.execute(q + " GROUP BY predicate, outward", (eid, eid, eid))}

    def evidence(self, rid: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        q = ("SELECT ev.*, d.path, d.content_hash AS doc_hash FROM evidence ev"
             " JOIN documents d ON d.id=ev.document_id WHERE ev.relation_id=?"
             " ORDER BY d.path, ev.locator")
        args: tuple = (rid,)
        if limit is not None:
            q += " LIMIT ?"
            args += (int(limit),)
        return [{"path": e["path"], "file": self.display_path(e["path"]),
                 "document_id": e["document_id"],
                 "locator": json.loads(e["locator"]),
                 "where": locator_text(e["locator"]), "quote": e["quote"],
                 "method": e["method"], "model": e["model"],
                 "stale": e["content_hash"] != e["doc_hash"]}
                for e in self.db.execute(q, args)]

    def mentions(self, eid: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        q = ("SELECT m.*, d.path FROM mentions m JOIN documents d ON"
             " d.id=m.document_id WHERE m.entity_id=? ORDER BY d.path, m.locator")
        args: tuple = (self.resolve(eid),)
        if limit is not None:
            q += " LIMIT ?"
            args += (int(limit),)
        return [{"path": m["path"], "file": self.display_path(m["path"]),
                 "where": locator_text(m["locator"]),
                 "locator": json.loads(m["locator"]), "surface": m["surface"],
                 "snippet": m["snippet"], "method": m["method"]}
                for m in self.db.execute(q, args)]

    def mention_count(self, eid: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM mentions WHERE entity_id=?",
                               (self.resolve(eid),)).fetchone()[0]

    def sources(self) -> List[Dict[str, Any]]:
        """The spreadsheets the graph read, in plain words: each Ledger
        (parent CSV) and workbook with what reading it gave, and the ones it
        could not read with why - for the Connections tab's Sources list."""
        from council_core import kg_excel
        last = self.db.execute("SELECT stats FROM runs WHERE kind='seed' AND finished_ts"
                               " IS NOT NULL ORDER BY finished_ts DESC LIMIT 1").fetchone()
        st = json.loads(last["stats"]) if last and last["stats"] else {}
        why = {}
        for u in st.get("unreadable", []):
            path, _sep, reason = u.partition(": ")
            why[path] = reason
        notes: Dict[str, List[str]] = {}
        for n in st.get("notes", []):
            path, _sep, note = n.partition(": ")
            notes.setdefault(path, []).append(note)
        out = []
        for d in self.db.execute(
                f"SELECT d.id, d.path, d.status, {_HEAD_SQL} AS records FROM documents d"
                " LEFT JOIN doc_cache c ON c.document_id=d.id WHERE d.status != 'missing'"
                " ORDER BY d.path").fetchall():
            suf = Path(d["path"]).suffix.lower()
            reading = _reading_load(d["records"])
            summary = reading.get("summary") or {}
            if suf not in _WORKBOOK and summary.get("kind") != "ledger" and not (
                    suf == ".csv" and kg_excel.is_ledger_csv(self.abs_path(d["path"]))):
                continue
            kind = summary.get("kind") or ("workbook" if suf in _WORKBOOK else "ledger")
            if d["status"] != "ok":
                text = f"Not read: {why.get(d['path']) or reading.get('error') or 'unreadable'}"
            else:
                text = kg_excel.describe(summary)
            out.append({"path": d["path"], "file": self.display_path(d["path"]),
                        "kind": kind, "status": d["status"],
                        "summary": summary, "text": text,
                        "notes": notes.get(d["path"], reading.get("notes") or [])})
        return out

    def reviews(self, status: str = "open") -> List[Dict[str, Any]]:
        out = []
        for r in self.db.execute(
                "SELECT rv.*, d.path FROM review rv LEFT JOIN documents d ON"
                " d.id=rv.document_id WHERE rv.status=? ORDER BY rv.id", (status,)):
            out.append({"id": r["id"], "kind": r["kind"], "surface": r["surface"],
                        "candidates": [self.entity(c) for c in json.loads(r["candidates"])],
                        "path": r["path"], "file": self.display_path(r["path"] or ""),
                        "where": locator_text(r["locator"]),
                        "locator": json.loads(r["locator"] or "{}"),
                        "snippet": r["snippet"]})
        return out

    def coverage(self) -> Dict[str, Any]:
        """The coverage line's numbers: documents read, documents that gave
        at least one fact, unreadable ones."""
        q = self.db.execute
        total = q("SELECT COUNT(*) FROM documents WHERE status='ok'").fetchone()[0]
        with_fact = q("SELECT COUNT(DISTINCT document_id) FROM evidence").fetchone()[0]
        with_mention = q("SELECT COUNT(DISTINCT document_id) FROM mentions").fetchone()[0]
        last = q("SELECT stats, finished_ts FROM runs WHERE kind='seed' AND finished_ts"
                 " IS NOT NULL ORDER BY finished_ts DESC LIMIT 1").fetchone()
        st = json.loads(last["stats"]) if last and last["stats"] else {}
        return {"documents": total, "with_facts": with_fact,
                "with_mentions": with_mention,
                "unreadable": st.get("unreadable", []),
                "skipped_rows": st.get("skipped_rows", []),
                "notes": st.get("notes", []),
                "ledgers": st.get("ledgers", 0), "workbooks": st.get("workbooks", 0),
                "pictures": st.get("pictures", 0),
                "pictures_read": st.get("pictures_read", 0),
                "last_run": last["finished_ts"] if last else None}

    def all_relations(self, include_rejected: bool = False) -> List[Dict[str, Any]]:
        q = "SELECT * FROM relations"
        if not include_rejected:
            q += " WHERE status != 'rejected'"
        return [{"id": r["id"], "subject": self.entity(r["subject_id"]),
                 "predicate": r["predicate"], "object": self.entity(r["object_id"]),
                 "status": r["status"]} for r in self.db.execute(q)]

    # ── export ──
    def export_json(self) -> Dict[str, Any]:
        """The whole graph as plain JSON (schema_version included), for
        backup or another tool. Read-only."""
        q = self.db.execute
        return {
            "schema_version": SCHEMA_VERSION,
            "exported_ts": time.time(),
            "documents": [dict(r) for r in q("SELECT * FROM documents ORDER BY path")],
            "entities": [dict(r) for r in q("SELECT * FROM entities ORDER BY type, name")],
            "aliases": [dict(r) for r in q("SELECT * FROM aliases ORDER BY entity_id, alias")],
            "relations": [dict(r) for r in q("SELECT * FROM relations ORDER BY id")],
            "evidence": [dict(r) for r in q("SELECT * FROM evidence ORDER BY id")],
            "mentions": [dict(r) for r in q("SELECT * FROM mentions ORDER BY id")],
            "review": [dict(r) for r in q("SELECT * FROM review ORDER BY id")],
            "decisions": [dict(r) for r in q("SELECT * FROM decisions ORDER BY id")],
            "field_rules": [dict(r) for r in q("SELECT * FROM field_rules ORDER BY label")],
        }

    def export_relations_csv(self) -> str:
        """One row per (relation, evidence): subject, predicate, object,
        status, file, where, quote, method."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["subject_type", "subject", "predicate", "object_type", "object",
                    "status", "file", "where", "quote", "method"])
        # A store an older Council wrote can hold a link whose end is gone
        # (a name answer once pointed at a deleted person): exported as
        # '(gone)' instead of failing the whole export.
        gone = {"type": "", "name": "(gone)"}
        for rel in self.all_relations(include_rejected=True):
            subj, obj = rel["subject"] or gone, rel["object"] or gone
            for ev in self.evidence(rel["id"]) or [{}]:
                w.writerow([subj["type"], subj["name"],
                            rel["predicate"], obj["type"], obj["name"],
                            rel["status"], ev.get("path", ""), ev.get("where", ""),
                            ev.get("quote", ""), ev.get("method", "")])
        return buf.getvalue()

    def write_exports(self) -> Tuple[Path, Path]:
        """Atomically write graph.json and relations.csv into the store's
        dot-folder (never anywhere in the user's documents)."""
        d = self.path.parent
        pj, pc = d / "graph.json", d / "relations.csv"
        for target, text in ((pj, json.dumps(self.export_json(), indent=1,
                                               ensure_ascii=False)),
                             (pc, self.export_relations_csv())):
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8", newline="")
            tmp.replace(target)
        return pj, pc


def source_context(root: Any, rel_path: str, locator: Any, *, radius: int = 3,
                   quote: str = "") -> List[Tuple[str, str, bool]]:
    """The part of a document a citation points at, for an in-app preview:
    ``[(gutter label, text, is_the_cited_line)]``.

    Text: ``radius`` lines either side of the cited line ('p2 L5'). Table:
    the header and the cited row, one 'column: value' line per cell. No
    locator (a Collection): the first lines. Read-only; ``[]`` when the file
    cannot be read. A person can see the evidence without leaving the app,
    which matters for a PDF or workbook whose viewer cannot jump to a line."""
    import field_search as fs
    if isinstance(locator, str):
        try:
            locator = json.loads(locator)
        except Exception:
            locator = {}
    locator = locator or {}
    p = Path(root) / rel_path
    if p.suffix.lower() in _WORKBOOK:
        # The Ledger's reader's places (sheet, cell, header row, a text box,
        # a picture), read back with openpyxl - never through Excel, which
        # the preview does not start (it runs when evidence is clicked).
        from council_core import kg_excel
        try:
            return kg_excel.preview(p, locator, quote)
        except Exception:
            return []
    try:
        if p.suffix.lower() in _TABULAR and locator.get("row"):
            for sheet, frame in fs._table_frames(p):
                if locator.get("sheet") not in (None, sheet):
                    continue
                # The frame's index is the row number a person sees.
                rowno = int(locator["row"])
                if rowno not in frame.index:
                    return []
                row = frame.loc[rowno]
                marked = set(locator.get("columns") or [locator.get("column")])
                out = [("", f"{'sheet ' + str(sheet) + ', ' if sheet else ''}"
                            f"row {locator['row']}", False)]
                for col in frame.columns:
                    val = row[col]
                    if val is None or (isinstance(val, float) and val != val):
                        val = ""
                    out.append((str(col), str(val), str(col) in marked))
                # A Ledger row: where the Ledger says its values came from.
                if locator.get("from"):
                    out.append(("from", str(locator["from"]), False))
                elif locator.get("sources"):
                    out.append(("sources", str(locator["sources"]), False))
                return out
            return []
        if p.suffix.lower() == ".pdf":
            pages = fs._pdf_pages(p)
            pg = int(locator.get("page") or 1)
            lines = pages[pg - 1].splitlines() if 0 < pg <= len(pages) else []
            prefix = f"p{pg} "
        else:
            lines = fs._read_text(p, max_chars=5_000_000).splitlines()
            prefix = ""
    except Exception:
        return []
    target = int(locator.get("line") or 0)
    if target:
        lo, hi = max(1, target - radius), min(len(lines), target + radius)
    else:
        lo, hi = 1, min(len(lines), 2 * radius + 1)
    return [(f"{prefix}L{i}", lines[i - 1], i == target) for i in range(lo, hi + 1)]


_EVIDENCE_INSERT = ("INSERT OR IGNORE INTO evidence (relation_id, document_id, content_hash,"
                    " locator, quote, method, model, run_id) VALUES (?,?,?,?,?,?,?,?)")
_MENTION_INSERT = ("INSERT OR IGNORE INTO mentions (entity_id, document_id, locator, surface,"
                   " snippet, method, run_id) VALUES (?,?,?,?,?,?,?)")

_INITIAL_NAME_RE = re.compile(r"\b([A-Z])\.\s+([A-Z][A-Za-zÀ-ÿ'’\-]+)")
#: A run of the characters _term_re treats as part of a token.
_TOKEN_RE = re.compile(r"[\w/\-]+")


def _term_re(alias: str) -> re.Pattern:
    """Whole-token, case-insensitive match for one alias: 'PN-1234/A' does not
    match inside 'PN-1234/AB', 'Carol Lee' does not match 'Carol Leeds'."""
    return re.compile(r"(?<![\w/\-])" + re.escape(alias) + r"(?![\w/\-])", re.I)


def _snip(text: str, limit: int = 200) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1] + "…"
