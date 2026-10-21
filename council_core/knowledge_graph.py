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
from typing import Any, Collection, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 4
STORE_DIR = ".knowledge_graph"
STORE_NAME = "graph.sqlite"

ENTITY_TYPES = ("PERSON", "PART", "PROJECT", "DOCUMENT")
#: The types labelled fields and the Ledger give. A model reading free text
#: may name any other (ORGANIZATION, LOCATION, EQUIPMENT ... - user,
#: 2026-10-21: "function very similar to Morphik"); DOCUMENT is only ever a
#: file the graph read.
CORE_TYPES = ("PERSON", "PART", "PROJECT")
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
-- v4: what a model said an entity is in a document ('description'), kept with
-- the document version it read (a changed document's go at the next rebuild).
CREATE TABLE IF NOT EXISTS entity_props (
    entity_id TEXT NOT NULL, document_id TEXT NOT NULL, content_hash TEXT NOT NULL,
    name TEXT NOT NULL, value TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL,
    PRIMARY KEY (entity_id, document_id, name, value)
);
CREATE INDEX IF NOT EXISTS entity_props_entity ON entity_props(entity_id);
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
    # what inspection workbooks call the part: no rule covered them and
    # Fields… could not offer them (review, 2026-10-19)
    FieldRule("Sample ID", "PART"),
    FieldRule("Sample No.", "PART"),
    FieldRule("Serial Number", "PART"),
    FieldRule("S/N", "PART"),
    FieldRule("Specimen", "PART"),
    FieldRule("Specimen ID", "PART"),
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
    if etype == "DOCUMENT":
        return str(raw)
    # any type a model named: a code as a code, a name folded
    return code_key(raw) if looks_like_code(raw) else fold(raw)


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
             "header", "image", "item", "from", "sources", "ocr", "checked")

#: How sure (0-100, Tesseract's own word confidence) the OCR must be of every
#: value a link rests on before a link read from a picture is firm - with
#: every one of those values known from a source that is not a picture. The
#: user, 2026-10-18: images read, "not ... 100% trusted unless we are positive
#: that the data pulled from the images is accurate".
PICTURE_SURE = 90


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
    if "checked" in loc:
        sure = (f"{loc['ocr']}% sure" if loc.get("ocr") is not None else "how sure unknown")
        s += (f" — read from a picture, {sure}, every name found elsewhere" if loc["checked"]
              else f" — read from a picture, {sure} — check it")
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
    #: Read from a picture by OCR. Its links are firm only when confirmed:
    #: the OCR was sure of it (locator 'ocr' >= PICTURE_SURE) and the vault
    #: knows it from a source that is not a picture (see _record_relations).
    from_image: bool = False


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
        # As a text document is read (fs.decode_text): an ANSI file's accents
        # were U+FFFD, and a BOM made json.loads refuse the whole file.
        text = fs.decode_text(p.read_bytes())
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
    page_lines: Dict[int, List[str]] = {}

    def source_line(loc) -> str:
        # The whole line, not the snippet: that is cut at 200 characters, and
        # a long attendee line's '[[Lee, Carol]]' past it made two people
        # (review, 2026-10-19).
        pno, ln = (loc.get("page") or 1) - 1, loc.get("line")
        if ln and 0 <= pno < len(pages):
            lines = page_lines.get(pno)
            if lines is None:
                lines = page_lines[pno] = pages[pno].splitlines()
            if ln <= len(lines):
                return lines[ln - 1]
        return loc.get("snippet", "")

    for r in rules:
        for loc in fs.field_value_locations(p, r.label, kind=r.type, exact=True,
                                            pages=pages):
            # 'Owner: [[Priya Raman]]' is Priya Raman (see field_link_value).
            value = loc["value"]
            if isinstance(value, str) and ("[[" in value or "]]" in value):
                value = field_link_value(value, source_line(loc))
            if loc["value"] and not value:
                continue        # only an embed, a [[#heading]], a 'see [[…]]'
            key = (loc.get("page"), loc.get("line"), value)
            prev = best.get(key)
            if prev is None or len(r.label) > len(prev.rule.label):
                best[key] = Value(r, value, loc, loc.get("snippet", ""))
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


def _categorize_unreadable_reason(text: str) -> str:
    """Compress an unreadable reason string into a short category label
    so the Connections tab can roll a long list up into one line.

    The categories are stable so the UI can display them in a fixed
    order; anything that doesn't match a specific case lands in
    ``other``."""
    t = (text or "").lower()
    if "needs the pypdf" in t or "pypdf" in t:
        return "needs pypdf"
    # the reader's own words, before the 'openpyxl' substring test: a
    # .xls/.xlsb on a PC without Excel was 'other', and a file that is not
    # a workbook 'needs openpyxl' (review, 2026-10-19)
    if "opens only through microsoft excel" in t:
        return "no Excel COM"
    # the Excel reader's newer words were all 'other', and a valid .xls in a
    # 256-259 character path had at least been 'corrupt' (review, 2026-10-20)
    if "path too long" in t:
        return "path too long"
    if "empty file" in t:
        return "empty file"
    if "refuses every call" in t or "excel could not be started" in t:
        return "no Excel COM"
    if "excel stopped while" in t:
        return "Excel stopped"
    if "is not a workbook" in t:
        return "corrupt / wrong format"
    if "needs the openpyxl" in t or "needs openpyxl" in t:
        return "needs openpyxl"
    if "password-protected" in t or "has a password" in t:
        return "password-protected"
    if "rights-protected" in t or "sensitivity label" in t:
        return "rights-protected"
    if "corrupt" in t or "excel refused to open" in t or "trust center" in t:
        return "corrupt / wrong format"
    if "access denied" in t:
        return "access denied"
    if "excel automation is not available" in t:
        return "no Excel COM"
    if "did not open" in t and "stopped" in t:
        return "Excel timeout"
    if "could not be found" in t or "not found" in t:
        return "not found"
    return "other"


def unreadable_breakdown(unreadable_list) -> Dict[str, int]:
    """Count the unreadable list's entries by their category label."""
    from collections import Counter
    counts: Counter = Counter()
    for text in unreadable_list:
        # Each entry is "<rel>: <reason>" or just "<reason>".
        reason = text.split(":", 1)[1].strip() if ":" in text else text
        counts[_categorize_unreadable_reason(reason)] += 1
    return dict(counts)


def rebuild_summary_line(stats) -> str:
    """A one-line human summary of a kg.seed() stats dict for the UI.

    Shape: ``"Rebuilt from N document(s) in X.Xs — W workbooks,
    L ledgers, O other docs; U unreadable (needs pypdf: 10, ...);
    R links + S suggested"``. Keeps zeros out so a healthy run reads
    cleanly."""
    seconds = float(stats.get("seconds", 0) or 0)
    unchanged = bool(stats.get("graph_unchanged"))
    bits = [f"Rebuilt from {stats.get('documents', 0)} document(s) in "
            f"{seconds:.1f}s"]
    kinds = []
    if stats.get("workbooks"):
        kinds.append(f"{stats['workbooks']} workbook"
                     + ("s" if stats['workbooks'] != 1 else ""))
    if stats.get("ledgers"):
        ledger_bit = (f"{stats['ledgers']} ledger"
                      + ("s" if stats['ledgers'] != 1 else ""))
        if stats.get("ledger_people"):
            ledger_bit += (f" ({stats['ledger_people']} "
                           f"{'person' if stats['ledger_people'] == 1 else 'people'})")
        kinds.append(ledger_bit)
    if stats.get("documents_other"):
        kinds.append(f"{stats['documents_other']} other doc"
                     + ("s" if stats['documents_other'] != 1 else ""))
    if kinds:
        bits.append(" — " + ", ".join(kinds))
    if unchanged:
        # The graph that was there is the answer: no entity is new this run.
        # It used to repeat the last run's '(23 matched existing, 18 new
        # entities)' with nothing saying so (review, 2026-10-19).
        bits.append("; nothing changed since the last rebuild")
    if stats.get("wikilink_mentions"):
        wm = stats["wikilink_mentions"]
        tail = ""
        matched = stats.get("wikilinks_matched") or 0
        created = stats.get("wikilinks_created") or 0
        if (matched or created) and not unchanged:
            parts = []
            if matched:
                parts.append(f"{matched} matched existing")
            if created:
                parts.append(f"{created} new "
                             + ("entity" if created == 1 else "entities"))
            tail = f" ({', '.join(parts)})"
        bits.append(f"; {wm} [[wiki-link]] mention"
                    + ("s" if wm != 1 else "")
                    + " found" + tail)
    unreadable = stats.get("unreadable") or []
    if unreadable:
        counts = stats.get("unreadable_reasons") or unreadable_breakdown(unreadable)
        parts = [f"{v} {k}" for k, v in sorted(counts.items(),
                                               key=lambda kv: -kv[1])]
        bits.append(f"; {len(unreadable)} unreadable ({', '.join(parts)})")
    rels = stats.get("relations") or {}
    seeded = int(rels.get("seeded") or 0) + int(rels.get("accepted") or 0)
    suggested = int(rels.get("suggested") or 0)
    if seeded or suggested:
        # The suggested ones are added ON: '20 links (5 suggested)' read as 5
        # of the 20 when there were 25. 'Links' stays the seeded + accepted
        # ones, as the coverage line and the progress finish line count them
        # ('20 links, 5 waiting for you'); a total with the suggested ones in
        # it put two link counts on screen at once (review, 2026-10-19).
        extra = f"; {seeded} link" + ("s" if seeded != 1 else "")
        if suggested:
            extra += f" + {suggested} suggested"
        bits.append(extra)
    else:
        bits.append("; 0 links found")
    # '0 links' and nothing more left a tester with no next step (user,
    # 2026-10-20): the most likely reason, from the rebuild's own
    # why_no_links (see KnowledgeGraph._seed_finish)
    why = stats.get("no_links") or {}
    if not seeded and why.get("short"):
        bits[-1] += f": {why['short']}"
    # "0 links" with initials nobody has named yet must point somewhere:
    # those rows link once the question is answered (review, 2026-10-19)
    n_q = int(stats.get("open_reviews") or 0)
    if n_q:
        who = list(stats.get("initials_open") or [])
        tail = ""
        if who:
            tail = " (who is " + ", ".join(who[:4]) + ("…" if len(who) > 4 else "") + "?)"
        bits.append(f"; {n_q} question{'s' if n_q != 1 else ''} waiting in "
                    f"Connections › Questions{tail}")
    return _sentence("".join(bits))


def _sentence(text: str) -> str:
    """``text`` with a full stop, unless it ends in one already ('Fields…')."""
    return text if text.endswith(("…", ".", "?", "!")) else text + "."


def _plural(k: int, one: str, many: str = "") -> str:
    return f"{k} {one if k == 1 else (many or one + 's')}"


def _no_links_reasons(d: Dict[str, Any]) -> List[Dict[str, str]]:
    """What keeps links away, most likely first, from KnowledgeGraph.
    why_no_links' facts: each ``{code, short, todo}`` - ``short`` is what the
    rebuild's summary line says after '0 links found:', ``todo`` what to do.
    The first is the one a '0 links' summary names."""
    out: List[Dict[str, str]] = []

    def why(code: str, short: str, todo: str) -> None:
        out.append({"code": code, "short": short, "todo": todo})
    led, docs, rules = d["ledgers"], d["documents"], d["rules"]

    def ledgers(k: int) -> str:
        """'the Ledger' when it is the only one, else '2 Ledgers'."""
        return "the Ledger" if k == 1 and led["known"] == 1 else _plural(k, "Ledger")
    if not d["built"]:
        why("not_built", "the graph has not been built yet",
            "Press ⟳ Build graph. It reads the vault's data_in folder, the folders added "
            "in Connections › Folders… and the Ledgers made in the Ledger tab.")
        return out
    n_docs = docs["read"] + docs["unreadable"]
    # Nothing to read - unless a Ledger the graph knows is the reason: one
    # that moved said 'put some in data_in' (review, 2026-10-20)
    if not n_docs and not led["waiting"] and not led["not_there"]:
        why("no_documents", "no documents to read — put some in data_in, add a folder "
            "(Connections › Folders…) or pick your Ledger in the Ledger tab",
            "Put documents in the vault's data_in folder, add a folder in Connections › "
            "Folders…, or pick, create or apply a Ledger in the Ledger tab; then ⟳ Rebuild "
            "graph.")
    if led["not_there"]:
        k = led["not_there"]
        # Not under Spreadsheets…: it lists the documents read or tried, and
        # a file that is not there is neither (review, 2026-10-20)
        why("ledger_gone", f"{ledgers(k)} {'is' if k == 1 else 'are'} not where "
            f"{'it was' if k == 1 else 'they were'} — put {'it' if k == 1 else 'them'} back, "
            f"or pick {'it' if k == 1 else 'them'} again in the Ledger tab",
            "The graph reads a Ledger where the Ledger tab made it. It was moved or renamed, "
            "or its drive is unplugged: put it back, or pick its parent CSV where it is now "
            "in the Ledger tab (that adds it there); then ⟳ Build graph. Connections › "
            "Folders… lists where the graph looks.")
    if led["waiting"]:
        k = led["waiting"]
        why("ledger_not_read", f"{ledgers(k)} {'has' if k == 1 else 'have'} not been read "
            "yet — ⟳ Build graph",
            f"The Ledger tab added {'it' if k == 1 else 'them'} after the last rebuild: press "
            "⟳ Build graph here (or Rebuild knowledge graph in the Ledger tab).")
    if led["not_ledger"]:
        # its schema.json went: read as a plain table, and 'not read yet -
        # Rebuild' was never true (review, 2026-10-20)
        k = led["not_ledger"]
        why("ledger_no_schema", ("the Ledger's schema file is" if k == 1 and led["known"] == 1
                                 else f"the schema files of {_plural(k, 'Ledger')} are")
            + " missing — read as a plain table",
            "A Ledger is its parent CSV and the '.schema.json' file the Ledger tab made "
            "beside it. Put that file back beside the CSV, or press Create / extend parent "
            "CSV in the Ledger tab with its template to make it again; then ⟳ Build graph.")
    if docs["unreadable"] and not docs["read"]:
        why("all_unreadable", "none of the documents could be read",
            "The rebuild's line and Connections › Spreadsheets… say why for each: a reader "
            "package that is not installed, a password, Excel not available on this PC.")
    elif led["unreadable"]:
        k = led["unreadable"]
        why("ledger_unreadable", f"{ledgers(k)} could not be read — Connections › "
            "Spreadsheets… says why",
            f"Connections › Spreadsheets… lists {'it' if k == 1 else 'them'} under Not read, "
            "with why.")
    if d["initials_waiting"]:
        k = d["initials_waiting"]
        why("initials_unnamed", ("an inspector's initials have" if k == 1 else
                                 f"{k} inspectors' initials have") + " no name yet",
            "Say who each one is in Connections › Questions, or type the full names on the "
            "People sheet of the Ledger's review and Apply it again; then ⟳ Build graph. "
            "Initials are never guessed.")
    if led["empty"]:
        k = led["empty"]
        why("ledger_empty", f"{ledgers(k)} {'has' if k == 1 else 'have'} no rows yet — Apply "
            "a review in the Ledger tab",
            "Extract, review and Apply in the Ledger tab: only the rows a person accepted are "
            "in the parent CSV, and only they give links.")
    if led["no_inspectors"]:
        k = led["no_inspectors"]
        why("no_inspectors", f"{ledgers(k)} {'has' if k == 1 else 'have'} no inspector columns",
            "A Ledger links each part to who inspected it, so its template needs an "
            "'Inspector Initials' cell beside the readings (or 'Inspector 1' rows, or an "
            "'Inspected by' / 'Operator' column). Add one to the template, press Create / "
            "extend parent CSV in the Ledger tab, then extract, review and Apply again.")
    if led["no_signers"]:
        k = led["no_signers"]
        why("no_signers", ("the Ledger's inspector columns are" if k == 1 and led["known"] == 1
                           else f"the inspector columns of {_plural(k, 'Ledger')} are")
            + " empty",
            "No accepted row names an inspector: accept (Y) the inspectors' initials in the "
            "review too - or type them in - and Apply again.")
    model = d.get("model") or {}
    if model.get("prose_documents") and not model.get("passages"):
        # A vault of reports and notes: what is in their sentences comes only
        # from a model reading them (Morphik's way) - the most likely reason
        # by far for a prose vault (verified 2026-10-21: one notes file naming
        # a lead, a part and a supplier gave 0 links and nothing said why but
        # 'no Ledger is known').
        why("text_not_read", "no model has read the text yet — choose a model and "
            "⟳ Build graph",
            "Labelled fields and tables give links without a model; the sentences of "
            "reports, notes and PDFs need one. Choose an installed model in Connections "
            "(gemma3:12b reads best; install one with 'ollama pull gemma3:12b') and press "
            "⟳ Build graph.")
    seen = rules["proposed_seen"]
    if seen:
        k = len(seen)
        short = ("no field rules ticked" if not rules["confirmed"] else
                 f"{_plural(k, 'field label')} your documents use "
                 f"{'is' if k == 1 else 'are'} not ticked")
        why("rules_unticked", short + " — Connections › Fields…",
            "Connections › Fields…: tick the labels that mean a person, part or project ("
            + ", ".join(seen[:8]) + (" …" if k > 8 else "") + ") - or press 'Tick the "
            "suggested field rules' - then ⟳ Build graph. Only ticked labels are read.")
    elif docs["workbooks"] + docs["text"] and not rules["confirmed_seen"] \
            and not model.get("passages"):
        why("rules_not_seen", "no field label the graph knows is in your documents — "
            "Connections › Fields…",
            "Connections › Fields… also lists labels your documents use that may mean the "
            "same ('POC' for 'Point of Contact'): tick those, then ⟳ Build graph. A model "
            "reads the free text when one is chosen.")
    if not led["known"] and n_docs and not d["links"]:
        # The tester's case (user, 2026-10-20): a Ledger made with an earlier
        # build is not added until it is picked, applied or rebuilt in the
        # Ledger tab. Last of the causes, and only at 0 links: it was the
        # first reason given for every vault without a Ledger, ahead of the
        # one that applied (review, 2026-10-21).
        why("no_ledger", "if you made a Ledger, the graph does not know it — pick its "
            "parent CSV in the Ledger tab",
            "A Ledger made with an earlier Council is not added to the graph by itself. In "
            "the Ledger tab, pick its parent CSV (Parent CSV › Browse…): that adds it, as "
            "Apply and Rebuild knowledge graph there do. Then ⟳ Build graph here. If you "
            "do not use the Ledger, this is not the reason.")
    if not d["links"] and d["suggested"]:
        k = d["suggested"]
        why("only_suggested", f"{_plural(k, 'suggested link')} {'waits' if k == 1 else 'wait'} "
            "for you — ✓ Accept",
            "Open a person, part or project and ✓ Accept (or ✗ Reject) each suggested link: "
            "a suggestion is not a link until you accept it.")
    if not d["links"] and not out:
        if sum(d["entities"].values()):
            why("no_pairs", "people, parts or projects were found, but never two in one row "
                "or document",
                "A link needs two of them together: a part and its owner in one row, a "
                "project and its lead in one document. Tick a part or project label beside "
                "the people in Connections › Fields…, or ⟳ Build graph with a model.")
        else:
            why("nothing_named", "nothing in the documents was read as a person, part or "
                "project",
                "Connections › Fields…: tick the labels your documents use, then ⟳ Build "
                "graph with a model chosen, so the sentences are read too.")
    return out


def why_no_links_text(d: Dict[str, Any]) -> str:
    """KnowledgeGraph.why_no_links as plain text, for the Connections tab's
    'Why no links?' and its Copy: the reason and what to do first, then the
    facts. Counts and column / label names only - never a value from a
    document, nor a file name."""
    led, docs, rules = d["ledgers"], d["documents"], d["rules"]
    out = ["Why no links? (Council › Connections)"]
    if d["built"]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["last_run"]))
        out.append(f"Links now: {d['links']}"
                   + (f", and {d['suggested']} suggested waiting for you" if d["suggested"]
                      else "") + f" (last rebuild {when}).")
    reasons = d["reasons"]
    if reasons and not d["links"]:
        out += ["", _sentence(f"Most likely: {reasons[0]['short']}"),
                f"What to do: {reasons[0]['todo']}"]
        rest = reasons[1:]
        if rest:
            out += ["", "Also:"]
    else:
        rest = reasons
        if rest:
            out += ["", "What keeps more links away:"]
    for r in rest:
        out += [_sentence(f"  • {r['short']}"), f"    {r['todo']}"]
    out += ["", "What the graph read:"]
    if d["built"]:
        n_docs = docs["read"] + docs["unreadable"]
        kinds = ", ".join(_plural(k, w) for k, w in ((docs["ledgers"], "Ledger"),
                                                     (docs["workbooks"], "workbook"),
                                                     (docs["text"], "text document"))
                          if k)
        line = f"  Documents: {n_docs} — {docs['read']} read" + (f" ({kinds})" if kinds else "")
        if docs["unreadable"]:
            why = ", ".join(f"{k}: {v}" for k, v in sorted(
                docs["unreadable_reasons"].items(), key=lambda kv: -kv[1]))
            line += f"; {docs['unreadable']} not read" + (f" ({why})" if why else "")
        out.append(line)
    if led["known"]:
        line = f"  Ledgers (parent CSVs): {led['known']} known to the graph, {led['read']} read"
        bad = [f"{led[k]} {w}" for k, w in (("waiting", "waiting for a rebuild"),
                                            ("unreadable", "not readable"),
                                            ("not_there", "not there now"),
                                            ("not_ledger", "read as a plain table: no "
                                                           "schema file")) if led[k]]
        out.append(line + (f" ({', '.join(bad)})" if bad else ""))
        if led["read"]:
            out.append(f"    {_plural(led['rows'], 'row')}, {_plural(led['parts'], 'part')}, "
                       f"{_plural(led['people'], 'person', 'people')} signing")
            cols = led["person_columns"]
            n_cols = len(cols) + led["reading_columns"]
            line = f"    inspector columns: {n_cols}"
            if cols:
                line += " — " + "; ".join(cols[:6]) + (" …" if len(cols) > 6 else "")
            if led["reading_columns"]:
                line += (f" (and {_plural(led['reading_columns'], 'reading column')} of "
                         "one inspector each)")
            out.append(line)
    else:
        out.append("  Ledgers (parent CSVs): none known to the graph. A Ledger is added when "
                   "it is picked, created or applied in the Ledger tab (or by its Rebuild "
                   "knowledge graph).")
    out.append(f"  Inspector initials waiting for a name: {d['initials_waiting']}")
    out.append(f"  Field rules: {rules['confirmed']} ticked, {rules['proposed']} proposed "
               "(a proposed rule is not read until it is ticked)")
    if d["built"]:
        for title, labels in (("proposed", rules["proposed_seen"]),
                              ("ticked", rules["confirmed_seen"])):
            out.append(f"    {title} labels your documents use: {len(labels)}"
                       + (f" — {', '.join(labels)}" if labels else ""))
        rels = d["relations"]
        out.append("  Links by status: " + ", ".join(
            f"{s} {rels.get(s, 0)}" for s in ("seeded", "accepted", "suggested", "rejected")))
        ents = d["entities"]
        others = sum(v for k, v in ents.items() if k not in CORE_TYPES)
        out.append(f"  Found: {_plural(ents.get('PERSON', 0), 'person', 'people')}, "
                   f"{_plural(ents.get('PART', 0), 'part')}, "
                   f"{_plural(ents.get('PROJECT', 0), 'project')}"
                   + (f", {_plural(others, 'other entity', 'other entities')}"
                      if others else ""))
        model = d.get("model") or {}
        out.append(f"  Text read by a model: {model.get('documents', 0)} of "
                   f"{model.get('text_documents', 0)} text document(s), "
                   f"{_plural(model.get('passages', 0), 'passage')}")
        out.append(f"  Open questions: {d['questions']} (Connections › Questions)")
    return "\n".join(out)


def _csv_header(p: Path) -> List[str]:
    """A CSV's (or TSV's) first non-empty row, from its first 64 KB: the
    labels a plain table uses, which the rebuild's cache does not keep."""
    import field_search as fs
    try:
        with open(p, "rb") as fh:
            text = fs.decode_text(fh.read(1 << 16))
        for row in csv.reader(io.StringIO(text, newline=""),
                              delimiter="\t" if p.suffix.lower() == ".tsv" else ","):
            if any(c.strip() for c in row):
                return [c.strip() for c in row]
    except (OSError, csv.Error):
        pass
    return []


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


_REBUILD_LOCKS: Dict[str, Any] = {}


def _rebuild_lock(store: Path):
    """One rebuild at a time per store, whichever tab or tool starts it."""
    import threading
    key = _real(store)
    return _REBUILD_LOCKS.setdefault(key, threading.Lock())


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
    """[label, raw, locator, snippet] + flags when any: 1 tentative, 2 signed,
    4 read from a picture."""
    out = []
    for v in r.values:
        flags = (1 if v.tentative else 0) | (2 if v.signed else 0) | \
            (4 if v.from_image else 0)
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
                                         bool(int(v[4]) & 2) if len(v) > 4 else False,
                                         bool(int(v[4]) & 4) if len(v) > 4 else False)
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
        nl = text.find("\n")          # not split: that copied the whole rest
        text = text[:nl] if nl >= 0 else text
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


#: SQL for a cached reading's first line only (a workbook's summary).
_HEAD_SQL = ("CASE WHEN instr(c.records, char(10)) > 0 AND substr(c.records, 1, 7) = '{\"v\": 3'"
             " THEN substr(c.records, 1, instr(c.records, char(10)) - 1)"
             " WHEN substr(c.records, 1, 1) = '{' THEN c.records ELSE NULL END")


#: Match an Obsidian-style ``[[Entity|display text]]`` reference. Nested
#: brackets and pipes split the match, which is deliberate — a user who
#: types ``[[foo [bar]]]`` should get a clean ``foo [bar`` entity, not a
#: swallowed outer ``]]``.
WIKILINK_RE = __import__("re").compile(r"\[\[([^\[\]\n]+?)\]\]")


@__import__("dataclasses").dataclass(frozen=True)
class WikiLinkRef:
    """One ``[[…]]`` reference found while reading a document.

    ``surface`` is what the user wrote inside the brackets, before the
    optional pipe (so ``[[Jordan Blake|JB]]`` has surface
    ``"Jordan Blake"``). ``display`` is the right-hand side of the pipe
    when present, else the surface. ``page`` is the PDF page or workbook
    sheet (whichever the line's first element was); ``line`` is the line
    index within it."""
    page: Optional[int]
    line: int
    surface: str
    display: str


def scan_wikilinks(lines: Sequence[Tuple[Optional[int], int, str]]
                   ) -> List[WikiLinkRef]:
    """Pure function over document_lines output; no DB access.

    Obsidian's own forms are read the way Obsidian reads them (review,
    2026-10-19): ``[[Jordan Blake#Contact]]`` and ``[[Jordan Blake#^a1b2]]``
    link to Jordan Blake (a heading or a block IN that note), so the anchor
    is cut off the target - it made a 'Jordan Blake#Contact' entity. A
    ``[[#Heading]]`` points into the note it is in and names nothing. An
    embed, ``![[diagram.png]]``, shows a file inline: not a reference to an
    entity, so it is skipped. In a markdown table the pipe is written
    ``\\|`` (``| Owner | [[Priya Raman\\|Priya]] |``): the backslash is not
    part of the name. A link written as a path, ``[[People/Priya Raman]]``
    or ``[[Priya Raman.md]]``, is to the note Priya Raman (see
    _link_note_name)."""
    out: List[WikiLinkRef] = []
    for page, line_no, text in lines:
        if "[[" not in text:
            continue
        for m in WIKILINK_RE.finditer(text):
            if m.start() > 0 and text[m.start() - 1] == "!":
                continue                    # an embed
            inside = m.group(1).strip()
            if not inside:
                continue
            target, _, display = inside.partition("|")
            if "|" in inside and target.endswith("\\"):
                target = target[:-1]        # a table's escaped '\|'
            surface = _link_note_name(_WIKILINK_ANCHOR_RE.split(target, 1)[0])
            display = (display.strip() or surface) if "|" in inside else surface
            if surface:
                out.append(WikiLinkRef(page=page, line=line_no,
                                       surface=surface, display=display))
    return out


#: Where a wiki-link's target ends and its anchor starts: ``#heading``,
#: ``#^block`` (and a bare ``^block``).
_WIKILINK_ANCHOR_RE = re.compile(r"[#^]")
#: A part code with its revision after a slash ('PN-1234/A', 'BRG-77/2',
#: 'PN-1234/Rev B'): one name, not a folder and a note.
_CODE_REVISION_RE = re.compile(r"^[^/]*\d\s*/\s*(?:rev\.?\s*)?[A-Za-z0-9]{1,3}$", re.I)


def _link_note_name(target: str) -> str:
    """The note a link's target names. Obsidian writes a link as a path
    under its 'Absolute path in vault' and 'Relative path' settings
    ('[[People/Priya Raman]]', '[[../People/Priya Raman]]') and a link can
    keep the file's '.md': read as written they made a PERSON 'People/Priya
    Raman' or 'Priya Raman.md' beside Priya Raman (review, 2026-10-19). A
    part code with a revision after its slash ('[[PN-1234/A]]') is kept
    whole: 'A' names nothing."""
    t = str(target or "").strip()
    md = t.lower().endswith(".md")
    if md:
        t = t[:-3].rstrip()
    if "/" in t and (md or not _CODE_REVISION_RE.match(t)):
        t = t.rstrip("/").rsplit("/", 1)[-1].strip()
    return t


#: A link that names nothing, inside a field's value: an embed (``![[…]]``)
#: or a link to a heading or block of the note it is in (``[[#Specs]]``).
_UNNAMED_LINK_RE = re.compile(r"!" + WIKILINK_RE.pattern + r"|\[\[\s*[#^][^\[\]\n]*\]\]")
#: What may stand before a link that IS a field's value: quotes, brackets,
#: emphasis, list punctuation, a list's 'and' (what an embed before it left:
#: '![[diagram.png]] and [[Priya Raman]]') or a title ('Dr. [[Priya
#: Raman]]').
_LINK_LEAD_RE = re.compile(
    r"(?:[\s\"'“”‘’(\[{*_,;&+–—-]"
    r"|\b(?:and|dr|mrs?|ms|mx|prof|sir|dame|capt)\b\.?)*", re.I)
#: What a value ends with when a link after it is a cross-reference: 'PN-1002
#: (see [[Pump Spec]])', 'PN-1002 per [[ECO-77]]', 'Priya Raman, cc
#: [[Jordan Blake]]', 'PN-1002 - [[Drawing]]'.
_LINK_REMARK_RE = re.compile(
    r"(?:[\s\"'“”‘’(\[{*_,;:&+/–—-]"
    r"|\b(?:see|also|per|cc|bcc|via|ref|re|backup|aka|and|or)\b\.?)*$", re.I)
#: Where the value before a link starts, in its line: after a field's ':', a
#: list's ',' ';' 'and', a table's '|', or the link before it.
_LINK_LIST_SEP_RE = re.compile(r"[:;,|=\t]|\]\]|\s(?:and|&)\s", re.I)


def _value_before_link(lead: str) -> Optional[str]:
    """``lead``, the text a field's value has before its first link: None
    when the link IS the value (only quotes, brackets, a title before it),
    else the value the text holds - '' when it is only a remark ('see',
    'cc'). An opening bracket the link is inside is cut with what follows
    it: 'Priya Raman (backup [[Jordan Blake]])' is Priya Raman."""
    if _LINK_LEAD_RE.fullmatch(lead):
        return None
    cut = max(lead.rfind("("), lead.rfind("["))
    if cut >= 0 and not any(c in lead[cut:] for c in ")]"):
        lead = lead[:cut]
    return _LINK_REMARK_RE.sub("", lead).strip().strip("\"'“”‘’").strip()


def field_link_value(value: str, line: str = "") -> str:
    """A labelled field's value with its wiki-link syntax taken off: the
    target of the link the value starts with - not the ``|alias``, not the
    ``#anchor``, not the quotes YAML front matter needs round a link
    (``owner: "[[Priya Raman]]"``). A value with no link is given back as
    it is.

    Read as written, 'Part Number: [[PN-1002]]' made a part '[[PN-1002]]'
    beside PN-1002, 'Owner: [[Priya Raman]]' renamed Priya Raman to the
    bracketed form (a person keeps the fullest name), and 'Owner: [[Jordan
    Blake]]. Tracked as [[PRJ-0915]].' made a person of the whole sentence
    (review, 2026-10-19). The links after the first are the wiki-link
    pass's (_seed_wikilinks), as every link in the text is.

    A value that starts with plain text is that text, and a link after it a
    cross-reference: taking the link made 'Priya Raman (backup [[Jordan
    Blake]])' Jordan Blake and 'PN-1002 per [[ECO-77]]' a part ECO-77, each
    with a firm (seeded) link that was not written (review, 2026-10-19).
    A link that names nothing - an embed, a ``[[#heading]]`` - is taken out
    and the rest kept: 'PN-1002 [[#Specs]]' is PN-1002; '' only when
    nothing is left.

    ``line``: the full line the value was read from. A link the value
    splitter cut in two ('[[Lee, Carol]]' read as '[[Lee' and 'Carol]]') is
    found whole there."""
    s = str(value or "")
    if "[[" not in s and "]]" not in s:
        return s
    s = s.strip()
    if _UNNAMED_LINK_RE.search(s):
        s = " ".join(_UNNAMED_LINK_RE.sub(" ", s).split())
    if "[[" not in s and "]]" not in s:
        return s.strip("\"'“”‘’").strip()
    opens, closes = s.find("[["), s.find("]]")
    m = WIKILINK_RE.search(s)
    if opens >= 0 and (closes < 0 or opens < closes):
        # The value starts with a link, or with text and a link after it.
        embed = opens > 0 and s[opens - 1] == "!"      # the start of a cut embed
        plain = _value_before_link(s[:opens - 1] if embed else s[:opens])
        if plain is not None:
            return plain
        if embed:
            return ""
        if m and m.start() == opens:
            refs = scan_wikilinks([(None, 0, m.group(0))])
            return refs[0].surface if refs else ""
    head = s[opens:] if opens >= 0 and (closes < 0 or opens < closes) else ""
    tail = s[:closes + 2] if closes >= 0 and not head else ""
    line = line or ""
    for m in WIKILINK_RE.finditer(line):
        whole = m.group(0)
        # Cut at a separator: '[[Lee' is not the start of '[[Leeds Upgrade]]'.
        if (head and whole.startswith(head) and not whole[len(head)].isalnum()) or \
                (tail and whole.endswith(tail) and not whole[-len(tail) - 1].isalnum()):
            if tail:
                lead, sep = line[:m.start()], None
                for sep in _LINK_LIST_SEP_RE.finditer(lead):
                    pass
                if _value_before_link(lead[sep.end():] if sep else lead) is not None:
                    return ""       # the end of a cross-reference link
            # From the character before it: an embed's '!' is kept.
            refs = scan_wikilinks([(None, 0, line[max(m.start() - 1, 0):m.end()])])
            return refs[0].surface if refs else ""
    return s.replace("[[", "").replace("]]", "").strip().strip("\"'()[]").strip()


#: Words that make a run of Capitalised words a title, not a person:
#: 'Blade Inspection Program', 'Engine Upgrade Plan', 'Night Line'. Folded
#: forms; only words that are (nearly) never a given name or a surname.
#: The shop's own teams and companies too ('Quality Team', 'First Shift',
#: 'Dana Corporation'): an Owner or Approved-by column often holds one, and
#: as a PERSON its first word vouched for topics (review, 2026-10-19:
#: 'Owner: Quality Team' made [[Quality Escape]] a person).
_NOT_NAME_WORDS = frozenset((
    "program programme project projects upgrade upgrades inspection inspections"
    " line lines plan plans report reports review reviews test tests testing"
    " system systems process procedure meeting meetings notes team group phase"
    " schedule spec specs specification manual design analysis study initiative"
    " board committee lab laboratory center centre department division office"
    " unit assembly module engine calibration maintenance overhaul repair build"
    " release roadmap proposal budget contract requirements guide training"
    " workshop audit survey log policy standard summary checklist template"
    " results data minutes agenda the of and for inc llc ltd corp company"
    " shift shifts crew staff quality engineering production purchasing"
    " supplier suppliers vendor vendors customer customers operations"
    " management logistics shipping receiving warehouse facility facilities"
    " corporation incorporated industries enterprises holdings manufacturing"
    " technologies solutions services international associates partners"
    " award awards").split())

#: Lower-case particles a name may hold between its first and last word:
#: 'Ludwig van Beethoven', 'Maria de la Cruz'.
_NAME_PARTICLES = frozenset(
    "de da del della der den di du la le van von bin ibn al dos das".split())

#: The particles that on their own say "a person": 'Pieter van Dijk',
#: 'Ursula von der Leyen'. 'de', 'del', 'la' and the rest name companies,
#: plants and places as often ('Aceros de Monterrey', 'Planta de Juarez',
#: 'Rio de Janeiro'), and 'bin' is a shop word ('Kanban bin'): with one of
#: those a name still needs its given name (review, 2026-10-19).
_PERSON_PARTICLES = frozenset("van von ibn".split())

#: Common given names across cultures (folded), so an unknown ``[[Jordan
#: Blake]]`` is a PERSON while ``[[Root Cause]]`` and ``[[Eddy Current]]``
#: are not. The US Social Security lists' top names for people born from the
#: 1940s on lead, as most users are US shops: a short list left out Frank,
#: Ashley, Martin, Raymond and Debra (review, 2026-10-19), and a person the
#: graph did not know became a PROJECT. Left out on purpose: names that are
#: also everyday or shop words, months, places, brands or units (Mark, Will,
#: Max, Jack, Grant, Chase, Rose, June, Jun, Austin, Phoenix, Aurora, Nova,
#: Eddy, Nick, Jan, Otto, Maya, Julian, Victor, Kim, Ray, Lee, Allen, Lewis,
#: Stanley, Parker, Taylor, Reed, Lance, Warren, Troy, Grace, Amber, Crystal,
#: Dawn, Chad, Virginia, Madison, Florence, Dev, Min, Al...) - such a person
#: still lands as a PERSON when the graph knows them (see
#: _infer_wikilink_type), and the user can relabel.
_GIVEN_NAMES = frozenset((
    # English-speaking (US Social Security top names, 1940s to now)
    "aaliyah aaron abby abigail adam adrian adrienne agnes aidan aiden alan"
    " albert alberta alec alex alexa alexander alexandra alexandria alexis"
    " alfred alice alicia alison alissa allison alma alvin alyssa amanda"
    " amelia amy andre andrea andrew andy angela angelica angelina angie anita"
    " ann anna anne annette annie anthony arlene arnold arthur ashlee ashley"
    " aubrey audrey ava barbara barry beatrice becky belinda ben benjamin"
    " bernadette bernard bernice beth bethany betsy betty beverly bill billie"
    " billy blake bob bobbie bobby bonnie brad bradley brady brandi brandon"
    " brenda brendan brent brett brian briana brianna bridget brittany"
    " brittney brooke bruce bryan bryce byron caitlin caleb calvin cameron"
    " camille candace cara carl carla carlton carmen carol carole caroline"
    " carolyn carrie casey cassandra cassidy catherine cathy cecil cecilia"
    " cedric celeste charlene charles charlie charlotte chelsea cheryl chester"
    " chloe chris christian christie christina christine christopher christy"
    " cindy claire clara clarence claude claudia clayton clifford clint clyde"
    " cody colby colin colleen connie connor constance corey cory courtney"
    " craig cristina curtis cynthia dale damon dan dana daniel danielle danny"
    " darin darla darlene darnell darrell darren darryl daryl dave david dean"
    " deanna deb debbie debora deborah debra delores denise dennis derek"
    " derrick desiree devin dewayne diana diane dianne dolores dominic don"
    " donald donna donnie dora doreen doris dorothy doug douglas duane dustin"
    " dwayne dwight dylan earl ed eddie edgar edith edna edward edwin eileen"
    " elaine eleanor elijah elisa elise eliza elizabeth ella ellen ellie elmer"
    " eloise elsie emily emma eric erica erik erika erin ernest esther ethan"
    " ethel eugene eva evan evelyn faye felicia felix fiona floyd frances"
    " francis frank franklin fred freddie frederick gabriel gabrielle gail"
    " gary gavin gayle gene geoffrey george gerald geraldine gilbert gina"
    " gladys glen glenda glenn gloria gordon greg gregg gregory gretchen gwen"
    " gwendolyn hailey haley hannah harold harriet harry harvey hayden heather"
    " heidi helen henry herbert herman hilda howard hubert ian ida irene irma"
    " isaac isabel isabella isaiah ivan jackie jacob jacqueline jacquelyn"
    " jaime jake jamal james jamie jana jane janet janice janie jared jasmine"
    " jason jay jayden jean jeanette jeanne jeff jeffery jeffrey jen jenna"
    " jennifer jenny jeremiah jeremy jerome jerry jesse jessica jessie jill"
    " jillian jim jimmie jimmy jo joan joann joanna joanne jocelyn jodi jody"
    " joe joel joey johanna john johnnie johnny jon jonathan jordan joseph"
    " josephine josh joshua joyce juanita judith judy julia julie justin kara"
    " karen kari karl kate katelyn katherine kathleen kathryn kathy katie"
    " katrina kay kayla kaylee keith kelley kelli kellie kelly kelsey ken"
    " kendra kenneth kenny kerry kevin kimberly kirk kristen kristi kristie"
    " kristin kristina kristine kristy krystal kurt kyle lacey lamar larry"
    " latasha latoya laura lauren laurie lawrence leah leigh lena leo leon"
    " leonard leroy leslie lester leticia liam lillian linda lindsay lindsey"
    " lisa liz lloyd logan lois lonnie lora loretta lori lorraine louis louise"
    " lucas lucille lucy luke lydia lyle lynda lynn lynne mabel mackenzie"
    " madeline mandy marc marcia marcus margaret margie maria marian marie"
    " marilyn marion marjorie marla marlene marsha martha martin marvin mary"
    " maryann matt matthew maureen maurice maxine megan melanie melinda"
    " melissa melvin meredith mia michael micheal michele michelle mickey mike"
    " mildred milton mindy miranda mitchell molly monica monique morgan muriel"
    " myra nadine nancy naomi natalie natasha nathan nathaniel neil nellie"
    " nicholas nicole nina noah nora norma norman olga oliver olivia oscar"
    " owen paige pam pamela patricia patrick patsy patty paul paula pauline"
    " peggy perry pete peter phil philip phillip phyllis preston priscilla"
    " rachael rachel ralph ramona randall randy raymond rebecca regina"
    " reginald renee rhonda richard rick ricky riley rita rob robert roberta"
    " robin robyn rochelle rodney roger roland ron ronald ronnie rosa rosalie"
    " roxanne roy rudy russell ruth ryan sabrina sally sam samantha samuel"
    " sandra sandy sara sarah scott sean seth shane shanna shannon shari"
    " sharon shawn shawna sheila shelia shelley shelly sheri sherri sherry"
    " shirley sidney simon sonia sonya sophia sophie spencer stacey stacy"
    " stella stephanie stephen steve steven stuart sue susan susie suzanne"
    " sylvia tabitha tamara tami tammy tanya tara tasha ted teresa teri"
    " terrance terrence terri terry thelma theodore theresa thomas tiffany tim"
    " timothy tina toby todd tom tommy toni tony tonya tracey traci tracy"
    " travis trevor tricia trisha tyler tyrone ursula valerie vanessa vera"
    " vernon veronica vicki vickie vicky victoria vincent virgil vivian"
    " wallace walter wanda wayne wendell wendy wesley whitney wilbur willard"
    " william willie wilma winston yolanda yvette yvonne zachary zoe"
    # Irish and Scottish
    " aoife ciara conor declan eamon fergus niamh seamus sinead siobhan"
    # Spanish and Portuguese
    " adolfo adriana agustin alberto alejandra alejandro alfredo ana andres"
    " antonio armando arturo beatriz benito blanca camila carlos catalina"
    " cesar consuelo cristian daniela diego eduardo efrain elena emilio"
    " enrique ernesto esperanza esteban estela eugenio fabiola federico felipe"
    " fernanda fernando fidel francisco gabriela gerardo gilberto gonzalo"
    " graciela guadalupe guillermo gustavo hector heriberto humberto ignacio"
    " ines isidro javier jesus jimena joao joaquin jorge jose josefina josue"
    " juan juana julio leobardo leonardo leonel liliana lorena lourdes lucia"
    " luis lupe luz manuel manuela marcela marco marcos margarita mariana"
    " maribel maricela mario marisol marta mateo mauricio miguel moises"
    " natalia nestor noemi octavio osvaldo pablo paola patricio pedro pilar"
    " rafael ramiro ramon raquel raul rebeca reynaldo ricardo rigoberto"
    " roberto rocio rodolfo rodrigo rogelio rolando rosario ruben salvador"
    " santiago santos saul sergio silvia socorro sofia teodoro thiago tomas"
    " ulises valentina valeria wilfredo ximena yadira yesenia"
    # French, German, Dutch, Nordic, Italian, Greek
    " alain alessandro amelie anders andreas anke antoine antonella astrid"
    " benoit bernd birgit bjorn bram camille celine chiara christoph"
    " christophe daan dieter dimitris dirk dominique eleni emile emmanuel"
    " fabien fabio federica florian francesca francois francoise friedrich"
    " georgios gilles giovanni giulia giuseppe guillaume gunnar gunter hans"
    " heike heinrich helene helmut henrik ingrid isabelle jacques jens johan"
    " johannes jonas joost jorg julien jurgen katrin kerstin klaus"
    " konstantinos kristian lars laurent leif lorenzo luca ludwig luigi lukas"
    " magnus manfred manon marcello markus martine massimo mathieu matteo"
    " matthias michel monika nathalie nicolas niels nikos nils olaf olivier"
    " paolo patrice petra philippe pierre pieter rainer ralf rasmus reinhard"
    " rene sabine salvatore sandrine sanne sebastien sigrid silke simone soren"
    " stefan stefano stephane sven sylvie thierry thorsten torsten ulrich uwe"
    " vasilis vittorio volker werner willem wolfgang yannis yves"
    # Slavic and Eastern European
    " agnieszka aleksandr alexei alexey anastasia andrei andrzej anya boris"
    " dmitri dmitry dragan ekaterina galina goran igor irina jakub jiri"
    " katarina katarzyna krzysztof lukasz lyudmila magdalena marcin marek"
    " michal mikhail milos natasha nikola nikolai oksana oleg pavel pawel petr"
    " piotr ruslan sergei svetlana tatiana tomasz vadim viktor vladimir"
    " wojciech yelena yuri zofia zoran"
    # South Asian
    " aarav abhishek aditi aditya ajay akash amit anand ananya anil anita"
    " anjali ankit arjun arnav arun ashish ashok deepak divya ganesh gaurav"
    " harish ishaan kavita kiran krishna kunal lakshmi lata mahesh manish"
    " manoj meera mohan mukesh naveen neha nikhil nikita nisha pankaj pooja"
    " pradeep prakash pranav praveen preeti priya radha rahul raj rajan rajesh"
    " rajiv rakesh ramesh ravi rekha ritu rohan rohit sachin sameer sandeep"
    " sanjana sanjay sanjeev santosh shalini shilpa shreya siddharth sneha"
    " srinivas sudhir sunil sunita suresh swati tarun uma usha varun venkat"
    " vijay vikram vinod vishal vivek"
    # East and Southeast Asian
    " aiko akira anh bao binh cuong daisuke duc eun hai hana hanh hao haruka"
    " haruto hien hieu hiro hiroko hiroshi hoa hoang hui huong huy hye hyun"
    " jae jian jie jin jing jisoo jiwoo jiyoung joon kaori kazuo keiko kenichi"
    " kenji kenta khanh kyung lei liang lin ling linh loan mai mariko masato"
    " megumi mei ming minh minjun naoko nga ngoc nhung phong phuc phuong qiang"
    " qing quang quoc rui ryo sakura satoshi seoyeon seung soo takashi takeshi"
    " taro thanh thao thuy tien tomoko trang trinh trung tuan vinh wei xiao"
    " xin xue yan ying yoko yong yoshi yuki yuko yumi yun zhen zhi zhong"
    # Arabic, Turkish, Persian, Hebrew
    " abdullah ahmad ahmed aisha ali amir amira avi ayesha eitan farah fatima"
    " hamza hasan hassan huda hussein ibrahim jamal karim khaled khalid layla"
    " leila mahmoud malik mariam maryam mehmet mohamed mohammad mohammed mona"
    " moshe muhammad mustafa nadia noa nour omar rami rana rania reza salma"
    " samir sara shira tamar tariq walid yael yasmin yosef yousef youssef"
    " yusuf zainab zeynep ziad"
    # African
    " abena adebayo akua ama amara ayodele chidi chinedu chinonso chioma emeka"
    " esi femi ifeoma kehinde kofi kwabena kwame kwesi lindiwe ngozi nnamdi"
    " obinna oluwaseun sipho taiwo tendai thabo tunde zanele"
    # Short and shared
    " eli mara").split())


def _name_word(tok: str) -> bool:
    """'Blake', "O'Brien", 'Jean-Luc', 'García', 'Ångström': a capital and
    then letters (any script's), apostrophes and hyphens; 2+ letters."""
    core = tok.replace("'", "").replace("’", "").replace("-", "")
    return len(core) >= 2 and core.isalpha() and tok[0].isupper()


def _name_initial(tok: str) -> bool:
    """'R.' in 'Jordan R. Blake', 'J.' in 'J. Blake'."""
    return len(tok) == 2 and tok[1] == "." and tok[0].isalpha() and tok[0].isupper()


def _given_name(tok: str, names: Collection[str]) -> bool:
    """Is this first word a given name in ``names`` (folded)? 'Jean-Luc'
    by 'jeanluc' or its first part 'jean'; 'José' as 'jose'."""
    f = fold(tok)
    return bool(f) and (f.replace(" ", "") in names or f.split()[0] in names)


def _name_shape(surface: str) -> Optional[Tuple[List[str], List[str]]]:
    """The SHAPE of a human name, or None: ``(words, named)`` - its words
    (a trailing 'Jr.' or 'PhD' dropped) and those of them that are not
    particles.

    Two to four Capitalised words (any script: 'José García'), the last a
    whole word, the others whole words or initials ('Jordan R. Blake',
    'J. Blake'), with lower-case particles between ('Ludwig van
    Beethoven'). 'Last, First' is read the way person_display reads it
    ('Blake, Jordan R.'). A word that names a thing, a team or a company,
    not a person ('Program', 'Inspection', 'Team', 'Shift' -
    _NOT_NAME_WORDS), makes it a title.

    Both _looks_like_person and KnowledgeGraph._known_people ask it: the
    graph's own people vouched for first names without it (review,
    2026-10-19: an Owner column's 'Quality Team' made 'quality' a given
    name, and [[Quality Escape]] a person)."""
    s = person_display(surface)
    if "," in s:
        left, right = (x.strip() for x in s.split(",", 1))
        if fold(right) not in _SUFFIXES:
            return None             # a comma that is not 'Last, First' or ', Jr.'
        s = left
    toks = s.split()
    while toks and fold(toks[-1]) in _SUFFIXES:
        toks.pop()                  # 'Jordan Blake Jr.'
    named = [t for t in toks if t not in _NAME_PARTICLES]
    if not 2 <= len(named) <= 4 or toks[0] in _NAME_PARTICLES \
            or toks[-1] in _NAME_PARTICLES:
        return None
    if any(fold(t) in _NOT_NAME_WORDS for t in named):
        return None
    if not (_name_word(named[-1]) and all(_name_word(t) or _name_initial(t)
                                          for t in named[:-1])):
        return None
    return toks, named


def _acronym_first(toks: List[str]) -> bool:
    """An all-caps first word in a mixed-case name ('SAM Registration',
    'QA Lead') is an acronym, not a given name; 'MARA KIM' is all caps."""
    return toks[0].isupper() and not " ".join(toks).isupper()


def _looks_like_person(surface: str, given_names: Collection[str] = ()) -> bool:
    """Does an unresolved ``[[surface]]`` name a human? Two things must hold:
    the SHAPE of a name (_name_shape), and POSITIVE evidence it is a person -
    an initial ('J. Blake', 'Jordan R. Blake'), a particle only a person's
    name holds ('van', 'von', 'ibn' - _PERSON_PARTICLES), or a first word
    that is a given name: a common one (_GIVEN_NAMES) or the first name of
    a person the graph already knows (``given_names``). An all-caps first
    word in a mixed-case surface is an acronym, not a name.

    The shape alone made a PERSON of every two Capitalised words (review,
    2026-10-19): [[Root Cause]], [[Eddy Current]], [[First Article]],
    [[Turbine Blade]] and [[Atlas Pump Retrofit]] landed in the People
    filter and the gazetteer searched the text for them as people. 'Last,
    First' and any particle were evidence on their own too (review,
    2026-10-19): 'Valve, Check' and 'Bearing, Ball' (a parts list's 'Noun,
    Modifier') and 'Aceros de Monterrey' became people. Before that it only
    had ASCII letters and no initials (review, 2026-10-19): 'Jordan R.
    Blake', 'J. Blake' and 'José García' became PROJECTs.
    (``looks_like_code`` runs first on anything with a digit.)"""
    shape = _name_shape(surface)
    if shape is None:
        return False
    toks, named = shape
    if any(_name_initial(t) for t in named) \
            or any(t in _PERSON_PARTICLES for t in toks):
        return True
    if _acronym_first(toks):
        return False                # 'SAM Registration'
    return _given_name(named[0], _GIVEN_NAMES) or _given_name(named[0], given_names)


def _infer_wikilink_type(surface: str, known_people: Collection[str] = (),
                         given_names: Collection[str] = ()) -> str:
    """Pick an entity type for an unresolved ``[[surface]]``.

    ``known_people``: the person keys (person_key) of the people the graph
    already knows; ``given_names``: their first names, folded (see
    KnowledgeGraph._known_people).

    Order:
      1. Looks like a code (``[A-Za-z]{1,8}[-_ ]?\\d[\\w\\-/.]*``) → PART.
      2. Is a person the graph knows ('Jordan Blake Jr.' for Jordan Blake)
         or looks like a human name (_looks_like_person) → PERSON.
      3. Fallback → PROJECT (the KG's catch-all for named things): an
         unknown Title Case phrase with no sign of a person in it."""
    s = (surface or "").strip()
    if looks_like_code(s):
        return "PART"
    if (known_people and person_key(s) in known_people) \
            or _looks_like_person(s, given_names):
        return "PERSON"
    return "PROJECT"


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
                 image_reader: Any = DEFAULT_IMAGE_READER, *, store: Any = None,
                 scope: Optional[Dict[str, Any]] = None, readonly: bool = False) -> None:
        self.vault = Path(vault)
        self.root = Path(root) if root is not None else self.vault / "data_in"
        # A named graph (council_core.kg_graphs): its own store, and only the
        # documents inside its folders (and of its file types).
        self.path = Path(store) if store is not None else store_path(self.vault)
        scope = scope or {}
        self._scope_folders = [str(f) for f in scope.get("folders") or []]
        self._scope_suffixes = {str(x).lower() for x in scope.get("suffixes") or []}
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
        if readonly:
            self._open_readonly()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        if existed:
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
                # The default rules are ON (user, 2026-10-21: "field rules
                # first" was a wrong assumption - Morphik needs no set-up
                # before a build). Fields… still unticks any of them.
                for r in DEFAULT_FIELD_RULES:
                    self.db.execute("INSERT OR IGNORE INTO field_rules VALUES "
                                    "(?, ?, 'confirmed')", (r.label, r.to_json()))
                # A store made while they started proposed: ticked once, unless
                # the user ever ticked or unticked a rule (their choice stands).
                if existed and self.db.execute(
                        "SELECT 1 FROM meta WHERE key='rules_on_by_default'").fetchone() is None:
                    if self.db.execute("SELECT 1 FROM decisions WHERE action='field_rule'"
                                       " LIMIT 1").fetchone() is None:
                        self.db.executemany(
                            "UPDATE field_rules SET status='confirmed' WHERE label=? AND"
                            " status='proposed'", [(r.label,) for r in DEFAULT_FIELD_RULES])
                self.db.execute("INSERT OR IGNORE INTO meta VALUES ('rules_on_by_default',"
                                " '1')")
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

    def _open_readonly(self) -> None:
        """For reading only - the Council's answers ask the graph on every
        question (kg_query): no schema script, no quick_check of the whole
        file (seconds on a big store, every turn), nothing written. A store
        that is not there, or a newer one, is refused as on a normal open."""
        self.db = None
        if not self.path.exists():
            raise KnowledgeGraphDamaged(f"{self.path} has not been built")
        uri = "file:" + "".join(
            ch if ch.isascii() and ch.isalnum() else
            "".join(f"%{b:02X}" for b in ch.encode("utf-8"))
            for ch in str(self.path.resolve())) + "?mode=ro"
        try:
            self.db = sqlite3.connect(uri, uri=True, timeout=self.BUSY_TIMEOUT_S)
            self.db.row_factory = sqlite3.Row
            row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'"
                                  ).fetchone()
            ver = int(row[0]) if row else None
        except (sqlite3.DatabaseError, ValueError) as exc:
            if self.db is not None:
                self.db.close()
            if _busy(exc):
                raise KnowledgeGraphBusy("the knowledge graph is being rebuilt") from exc
            raise KnowledgeGraphDamaged(f"the knowledge graph store {self.path} could "
                                        f"not be read ({exc})") from exc
        if ver is None or ver > SCHEMA_VERSION:
            self.db.close()
            raise KnowledgeGraphDamaged(f"{self.path} is not a store this Council reads")

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

    def confirm_rules(self, labels: Iterable[str]) -> List[str]:
        """Tick these field rules - the ones still proposed - in ONE
        transaction: all of them, or none when the store is busy. One commit
        per rule left some ticked and the rest not when the store was held
        elsewhere (review, 2026-10-20). Returns the labels ticked."""
        want = list(dict.fromkeys(str(x) for x in labels))
        current = {r.label: s for r, s in self.field_rules()}
        ticked = [x for x in want if current.get(x) == "proposed"]
        with self.db:
            for label in ticked:
                self.db.execute("UPDATE field_rules SET status='confirmed' WHERE label=?",
                                (label,))
                self._decide("field_rule", {"label": label, "status": "confirmed"})
        return ticked

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
                    keys(json.loads(fs.decode_text(p.read_bytes())))
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
                files = d.get("files")
                out.append({"id": str(d["id"]), "path": str(p), "name": p.name or str(p),
                            "exists": p.is_dir(), "added_ts": d.get("added_ts"),
                            # None: the whole folder; a list: only these
                            # files in it (a Ledger the Ledger tab added)
                            "files": ([str(f) for f in files] if isinstance(files, list)
                                      else None)})
        return out

    def _extra_bases(self) -> List[Tuple[str, Path]]:
        if getattr(self, "_bases", None) is None:
            self._bases = [(r["id"], Path(r["path"])) for r in self.extra_roots()]
        return self._bases

    def _save_roots(self, items: List[Dict[str, Any]], decision: str,
                    detail: Dict[str, Any]) -> None:
        keep = []
        for r in items:
            d = {"id": r["id"], "path": r["path"], "added_ts": r.get("added_ts")}
            if r.get("files") is not None:
                d["files"] = list(r["files"])
            keep.append(d)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                            (self.EXTRA_ROOTS_KEY, json.dumps(keep, ensure_ascii=False)))
            self._decide(decision, detail)
        self._bases = None

    def reads(self, path: Any) -> bool:
        """Whether a rebuild reads this file: under data_in, in a folder the
        user added, or one of the files a Ledger added (add_ledger)."""
        import os
        p = Path(os.path.realpath(_plain_path(Path(str(path)).expanduser())))
        if _overlaps(p, self.root, inside_only=True):
            return True
        for r in self.extra_roots():
            if r["files"] is None:
                if _overlaps(p, r["path"], inside_only=True) \
                        and not _overlaps(p, self.vault, inside_only=True):
                    return True
            elif _real(p.parent) == _real(r["path"]) \
                    and p.name.casefold() in {f.casefold() for f in r["files"]}:
                return True
        return False

    def add_ledger(self, csv: Any) -> Optional[Dict[str, Any]]:
        """Also read this one Ledger (a parent CSV; its schema, aliases and
        review workbooks beside it go with it) - only that file, not the
        folder it is in, which usually holds the template and inspection
        workbooks too. The Ledger tab's Rebuild calls it: a parent CSV is
        made beside its template, which is rarely under data_in, so an
        applied Ledger never reached the graph (review, 2026-10-19). Only
        READ, like an added folder. None when the graph reads it already."""
        import os
        p = Path(_plain_path(csv)).expanduser()
        if not p.is_file():
            raise ValueError(f"{csv} is not a file")
        p = Path(os.path.realpath(str(p)))
        if self.reads(p):
            return None
        if _overlaps(p, self.vault / STORE_DIR, inside_only=True):
            raise ValueError(f"{p} is in the graph's own store")
        folder = p.parent
        current = self.extra_roots()
        for r in current:
            if r["files"] is not None and _real(r["path"]) == _real(folder):
                r["files"] = r["files"] + [p.name]
                self._save_roots(current, "add_ledger", {"id": r["id"], "path": str(p)})
                return dict(r, exists=True)
        entry = {"id": self.root_id(folder), "path": str(folder),
                 "added_ts": time.time(), "files": [p.name]}
        self._save_roots(current + [entry], "add_ledger", {"id": entry["id"], "path": str(p)})
        return dict(entry, name=folder.name or str(folder), exists=True)

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
        current = []
        for r in self.extra_roots():
            if r["files"] is not None:
                # a Ledger's files: the whole folder now covers them when
                # they are in it; otherwise they stay as they are
                if not _overlaps(r["path"], p, inside_only=True):
                    current.append(r)
                continue
            if _overlaps(p, r["path"]):
                raise ValueError(f"{p} overlaps a folder already added ({r['path']})")
            current.append(r)
        entry = {"id": self.root_id(p), "path": str(p), "added_ts": time.time()}
        self._save_roots(current + [entry], "add_folder",
                         {"id": entry["id"], "path": entry["path"]})
        return dict(entry, name=p.name or str(p), exists=True, files=None)

    def remove_root(self, rid: str) -> None:
        """Stop reading an added folder. Its documents leave the graph at the
        next rebuild (they count as gone: their links and cached text go);
        the folder itself is not touched."""
        current = self.extra_roots()
        if not any(r["id"] == rid for r in current):
            raise KeyError(rid)
        self._save_roots([r for r in current if r["id"] != rid], "remove_folder", {"id": rid})

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
        roots = self.extra_roots()
        others = [Path(r["path"]) for r in roots if r["files"] is None]
        seen = {_real(p) for p in out}
        for r in roots:
            base = Path(r["path"])
            if not base.is_dir():
                continue
            if prog is not None:
                prog.update(f"listing {base}", force=True)
            if r["files"] is not None:
                # a Ledger's own files only - never what else is beside it
                got = self._files_under(base, cl, kg_excel, [base / f for f in r["files"]])
            else:
                got = self._files_under(base, cl, kg_excel, self._walk_added(base, others))
            for p in got:
                if _real(p) not in seen:
                    seen.add(_real(p))
                    out.append(p)
        if self._scope_folders or self._scope_suffixes:
            out = [p for p in out if self._in_scope(p)]
        return out

    def _in_scope(self, p: Path) -> bool:
        """A named graph reads only the documents inside its folders, of its
        file types."""
        if self._scope_suffixes and p.suffix.lower() not in self._scope_suffixes:
            return False
        return not self._scope_folders or any(
            _overlaps(p, f, inside_only=True) for f in self._scope_folders)

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
        # the closest folder: a Ledger's own folder can sit inside one the
        # user added whole (added after it)
        best = None
        for rid, base in self._extra_bases():
            try:
                rel = p.relative_to(base).as_posix()
            except ValueError:
                continue
            if best is None or len(rel) < len(best[1]):
                best = (rid, rel)
        if best is not None:
            return f"<{best[0]}>/{best[1]}"
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

    def _lookup_entity_by_name(self, name: str) -> Optional[Tuple[str, str]]:
        """Find an existing entity whose alias folds to ``name``'s folded
        form. Returns ``(eid, type)`` or None.

        Used by the wiki-link pass: a ``[[Jordan Blake]]`` the user wrote in
        a document resolves to the Jordan Blake who was created by a Ledger
        row, instead of creating a parallel PERSON."""
        fn = fold(name)
        if not fn:
            return None
        row = self.db.execute(
            "SELECT a.entity_id, e.type FROM aliases a JOIN entities e"
            " ON e.id=a.entity_id WHERE a.alias_norm=? AND e.merged_into IS NULL"
            " LIMIT 1", (fn,)).fetchone()
        if row is None:
            return None
        return (row["entity_id"], row["type"])

    def _known_people(self) -> Tuple[set, set]:
        """The people the graph knows, as the wiki-link pass's evidence that
        an unresolved ``[[Name]]`` is a person: their person keys, and the
        folded first word of every full name ('Priya Raman' -> 'priya').

        During a rebuild only the people THIS run supports (records, Ledger
        people, the user's) count: a stale PERSON 'Root Cause', typed by the
        old shape-only rule (review, 2026-10-19), must not vouch for itself.
        And a first name only from a value shaped like a name (_name_shape):
        an Owner or Approved-by column often holds a team ('Quality Team',
        'First Shift'), and its first word made [[Quality Escape]] and
        [[First Article]] people (review, 2026-10-19)."""
        keys: set = set()
        firsts: set = set()
        live = self._live
        for r in self.db.execute("SELECT id, key, name FROM entities"
                                 " WHERE type='PERSON' AND merged_into IS NULL"):
            if live is not None and r["id"] not in live:
                continue
            if r["key"]:
                keys.add(r["key"])
            # A one-word name may be a surname ('Blake'), or initials; an
            # initial ('J. Blake') or an acronym ('QA Lead') is no first name.
            shape = _name_shape(r["name"] or "")
            if shape is None:
                continue
            toks, named = shape
            if _name_word(named[0]) and not _acronym_first(toks):
                firsts.add(fold(named[0]).replace(" ", ""))
        return keys, firsts

    def _wikilink_refs(self, did: str, h: str, p: Path) -> List[WikiLinkRef]:
        """Every ``[[…]]`` in one document's text lines: the lines the
        gazetteer searches (doc_cache when it holds this version, else the
        file). A CSV is read by column and has none.

        Only a cached reading that holds a ``[[`` past its own opening
        ``[[`` is parsed: the lines are JSON ``[[page, line, "text"], …]``,
        so any later ``[[`` is inside a line's text - and most documents
        hold no wiki-link at all."""
        if p.suffix.lower() in _CSV:
            return []
        row = self.db.execute(
            "SELECT content_hash, CASE WHEN instr(substr(lines, 3), '[[') > 0"
            " THEN lines END AS lines FROM doc_cache WHERE document_id=?",
            (did,)).fetchone()
        if row is not None and row["content_hash"] == h:
            if row["lines"] is None:
                return []
            return scan_wikilinks([tuple(x) for x in json.loads(row["lines"])])
        return scan_wikilinks(document_lines(p))

    def _seed_wikilinks(self, usable: Sequence[Tuple[Path, str, str]], run_id: str,
                        stats: Dict[str, Any]) -> None:
        """Create (or resolve) an entity for every ``[[Name]]`` in every
        usable document and write a mention linking that entity to the
        document it appeared in.

        The references are found HERE, in every document's cached lines -
        not queued while documents were read: an unchanged document is not
        read, and the graph phase clears every mention and every non-user
        alias before this runs, so its wiki-links (and the entities only
        they supported) were deleted whenever ANOTHER document changed
        (review, 2026-10-19). One source for every document, re-read or not,
        so none is counted twice; the counters cover them all.

        An existing-entity match stays with the entity's type; an unresolved
        name is classified by ``_infer_wikilink_type`` and inserted at the
        inferred type with ``alias_source='inferred'`` (wiped on next
        rebuild and rebuilt from the same documents, so withdrawn wiki-links
        disappear cleanly). ``wikilinks_created`` counts entities that were
        not in the graph before; one that was (a wiki-link's own entity from
        the last rebuild, kept until the clean-up at the end) is matched."""
        surfaces = set()
        # Taken once, before any wiki-link adds a person: the type of a
        # reference must not depend on which document was read first.
        known_people, given_names = self._known_people()
        for p, did, h in usable:
            try:
                refs = self._wikilink_refs(did, h, p)
            except Exception as exc:                              # noqa: BLE001
                # One document's lines that cannot be had (a damaged cache
                # row, a file that went between the phases) must not stop
                # the graph being built: it is in one transaction.
                print(f"[wikilinks] skipped {self._rel(p)}: {exc!r}", flush=True)
                continue
            for ref in refs:
                stats["wikilink_mentions"] += 1
                surfaces.add(ref.surface.lower())
                match = self._lookup_entity_by_name(ref.surface)
                if match is not None:
                    eid, _etype = match
                    stats["wikilinks_matched"] += 1
                else:
                    etype = _infer_wikilink_type(ref.surface, known_people,
                                                 given_names)
                    known = self.db.execute(
                        "SELECT 1 FROM entities WHERE id=?",
                        (entity_id(etype, entity_key(etype, ref.surface)),)
                    ).fetchone() is not None
                    # The entity's name is the SURFACE (left of the pipe) — the
                    # user's canonical reference. ref.display is only the local
                    # alias used in that one line, so it is not passed as the
                    # entity's display name here.
                    eid = self._entity(etype, ref.surface,
                                       alias_source="inferred")
                    stats["wikilinks_matched" if known else "wikilinks_created"] += 1
                # The gazetteer's locator for the same line ('page' is a
                # workbook's sheet too): with 'sheet' here, _has_mention
                # missed it and one reference got two mentions (review,
                # 2026-10-19).
                loc: Dict[str, Any] = {"line": int(ref.line)}
                if ref.page is not None:
                    loc["page"] = ref.page
                snippet = f"[[{ref.surface}]]"
                try:
                    self._mention(eid, did, loc, ref.surface, snippet,
                                  "wikilink", run_id)
                except Exception as exc:                              # noqa: BLE001
                    # A mention row that is already there (same entity + doc +
                    # loc + method) is INSERT OR IGNORE's job; a plain Exception
                    # here means something more wrong than that, and the rebuild
                    # should keep going.
                    print(f"[wikilinks] skipped mention for {ref.surface!r}: {exc!r}",
                          flush=True)
        stats["wikilink_unique"] = len(surfaces)

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
        lock = _rebuild_lock(self.path)
        if not lock.acquire(blocking=False):
            # The Ledger tab's and the Connections tab's rebuilds ran at once
            # and the second died 'database is locked' (review, 2026-10-19)
            raise KnowledgeGraphBusy("the knowledge graph is being rebuilt already")
        try:
            with Progress("kg rebuild", listener=listener) as prog:
                token = _PROGRESS.set(prog)
                try:
                    stats = self._seed(use_collections=use_collections,
                                       gazetteer=gazetteer, on_progress=on_progress,
                                       prog=prog)
                    self._seed_finish(stats, prog)
                except sqlite3.OperationalError as exc:
                    # The lock above is this process's only: another Council
                    # rebuilding the same vault held the store past the busy
                    # timeout, and this rebuild died with a raw 'database is
                    # locked' (review, 2026-10-19). Its batches committed so
                    # far stay; nothing of the graph phase was written.
                    if _busy(exc):
                        raise KnowledgeGraphBusy(
                            "the knowledge graph is being rebuilt elsewhere — try "
                            "again when that finishes") from exc
                    raise
                finally:
                    _PROGRESS.reset(token)
                return stats
        finally:
            lock.release()

    def _seed_finish(self, stats: Dict[str, Any], prog) -> None:
        # the initials nobody has named yet: the summary line says who
        stats["initials_open"] = [r[0] for r in self.db.execute(
            "SELECT DISTINCT surface FROM review WHERE kind='initials'"
            " AND status='open' ORDER BY surface")]
        rels = stats.get("relations") or {}
        # Pre-compute the categorized unreadable breakdown so callers
        # that read the dict (the UI) do not need to repeat it.
        if stats.get("unreadable") and not stats.get("unreadable_reasons"):
            stats["unreadable_reasons"] = unreadable_breakdown(stats["unreadable"])
        # No links: why, for the summary line. No links, or no field rule
        # ticked yet (a first run: raw workbooks gave 1 link with every rule
        # proposed, and 25 once ticked - user, 2026-10-20): the proposed rules
        # the documents use, for the Connections tab to offer to tick. Never
        # fails the rebuild: it only explains it. With links and no rule
        # ticked, only when something besides a Ledger was read: a Ledger
        # needs no rule, and a Ledger-only vault paid ~1 s per rebuild for an
        # offer it never needs (review, 2026-10-20).
        links = int(rels.get("seeded") or 0) + int(rels.get("accepted") or 0)
        others = int(stats.get("workbooks") or 0) + int(stats.get("documents_other") or 0)
        if not links or (not stats.get("rules") and others):
            prog.update("working out why there are no links" if not links else
                        "looking for the field labels your documents use", force=True)
            try:
                why = self.why_no_links(stats, keep_labels=True)
            except Exception:
                why = None
            if why is not None:
                if not links and why["reason"] is not None:
                    stats["no_links"] = dict(why["reason"])
                stats["rules_to_tick"] = list(why["rules"]["proposed_seen"])
        prog.finish(f"{stats['documents']} documents ({stats['documents_read']} read, "
                    f"{stats['documents_unchanged']} unchanged, "
                    f"{len(stats['unreadable'])} not read); "
                    f"{rels.get('seeded', 0) + rels.get('accepted', 0)} links, "
                    f"{rels.get('suggested', 0)} suggested, "
                    f"{stats.get('open_reviews', 0)} question(s)")

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
                                 "ledgers": 0, "workbooks": 0,
                                 "documents_other": 0, "pictures": 0,
                                 "pictures_read": 0,
                                 "ledger_people": 0,
                                 "wikilink_mentions": 0,
                                 "wikilink_unique": 0,
                                 "wikilinks_matched": 0,
                                 "wikilinks_created": 0,
                                 "unreadable_reasons": {}}
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
        clip: Dict[str, bool] = {}

        def pictures_wait(cached: sqlite3.Row) -> bool:
            """An unchanged workbook whose pictures an earlier rebuild could
            not copy out (the clipboard was not safe then) is read again
            when it is safe now. Asked once per rebuild, and only then:
            Excel is not started for nothing."""
            if reader is None:
                return False
            summary = _reading_load(cached["records"]).get("summary") or {}
            if not int(summary.get("pictures_pending") or 0):
                return False
            if "ok" not in clip:
                import excel_open
                clip["ok"] = bool(excel_open.clipboard_safe()[0])
            return clip["ok"]
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
                    if cached is not None and pictures_wait(cached):
                        cached = None
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
                        # Obsidian-style [[Name]] references are found in
                        # these lines by the graph phase (_seed_wikilinks),
                        # for every document - read now or unchanged.
                        self.db.execute(
                            "INSERT OR REPLACE INTO doc_cache VALUES (?,?,?,?,?,?)",
                            (did, h, sig, _records_json(recs, reading),
                             json.dumps(lines, ensure_ascii=False), time.time()))
                        stats["documents_read"] += 1
                        readable.append((p, did, h))
                except Exception as exc:
                    # The STORE held by another Council's rebuild is not this
                    # file's fault: it is not counted 'unreadable' (seed()
                    # says the graph is busy).
                    if _busy(exc):
                        raise
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
                    else:
                        # Read, but without a workbook/ledger summary — a plain
                        # document (PDF, text, image). Counted so the Rebuild
                        # breakdown explains "243 files but only 2 workbooks".
                        stats["documents_other"] += 1
                    prog.advance()
                # Initials two Ledgers give to different people are nobody's
                # outside those Ledgers: asked, not picked.
                for k in clash:
                    self._people_all.pop(k, None)
                # The unique-people-from-Ledgers count: part of the breakdown
                # the Rebuild summary line shows, so the Ledger tab's "Rebuild
                # knowledge graph" button has visible payoff beyond a
                # workbook count.
                stats["ledger_people"] = len(self._people_all)

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
                # (A document read again with the same bytes and reader -
                # pictures copied out at last - changes no signature.)
                if gsig == self.meta("graph_sig") and not fallback \
                        and not stats["documents_read"]:
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
                # What a model read (its entities' mentions and names) stays,
                # as its links' evidence does: cleared below when stale.
                self.db.execute("DELETE FROM mentions WHERE method != 'model'")
                self.db.execute("DELETE FROM review WHERE status='open'")
                self.db.execute("DELETE FROM aliases WHERE source NOT IN ('user', 'model')")
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
                # A model's mentions, names and descriptions go with what it
                # read: the document changed or went.
                self.db.execute(
                    "DELETE FROM mentions WHERE method='model' AND document_id NOT IN"
                    " (SELECT document_id FROM extraction_done)")
                self.db.execute(
                    "DELETE FROM entity_props WHERE content_hash !="
                    " (SELECT content_hash FROM documents d WHERE d.id=entity_props.document_id)"
                    " OR document_id IN (SELECT id FROM documents WHERE status='missing')")
                self.db.execute(
                    "DELETE FROM aliases WHERE source='model' AND entity_id NOT IN"
                    " (SELECT entity_id FROM mentions WHERE method='model')")
                # what a model read still stands: its entities are this run's
                self._live |= {r[0] for r in self.db.execute(
                    "SELECT entity_id FROM mentions WHERE method='model'")}
                prog.update(f"linking {n_records} records", force=True)
                # Evidence and mentions are gathered, then written in batches;
                # nothing reads them until the gazetteer (which checks the
                # field mentions) - flushed before it.
                self._memo["buffer"] = {"evidence": [], "mentions": []}
                self._seed_records(kept, run_id)
                if use_collections:
                    prog.update("Collections")
                    stats["collections"] = self._seed_collections(run_id)
                self._flush()
                self._memo["buffer"] = None
                # Wiki-link mentions. Done AFTER the records pass (so a surface
                # that matches a record-created entity resolves to it) and
                # BEFORE the gazetteer (which can then find other mentions of
                # a brand-new PERSON a wiki-link named).
                prog.update("[[wiki-links]]")
                self._seed_wikilinks(usable, run_id, stats)
                # The initials questions' answers to choose from, AFTER the
                # wiki-link pass: a person only a [[Rita Tan]] names was never
                # offered for 'RT' (review, 2026-10-19).
                self._finish_initials()
                if gazetteer:
                    prog.stage("finding known names in the text", total=len(files),
                               unit="files")
                    stats["gazetteer_mentions"] = self._gazetteer(files, run_id, prog)
                # Still pending: no document gave evidence this run. One a model
                # proposed (KG2 evidence is kept across runs) is a suggestion; the
                # rest - the document changed or the rule was withdrawn - go. The
                # user's accepted/rejected ones stay.
                # (Only a model's evidence can be left on a pending link.) In
                # the graph at once, or waiting for the user - the graph's
                # setting (model_link_status).
                self.db.execute(
                    f"UPDATE relations SET status=? WHERE status='{_PENDING}'"
                    " AND id IN (SELECT relation_id FROM evidence)",
                    (self.model_link_status(),))
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
                self.db.execute("DELETE FROM entity_props WHERE entity_id NOT IN"
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
            # The wiki-link counters too: they read 0 on every unchanged
            # rebuild (review, 2026-10-19).
            for k in ("collections", "gazetteer_mentions", "records", "skipped_rows",
                      "wikilink_mentions", "wikilink_unique", "wikilinks_matched",
                      "wikilinks_created"):
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
        #    A picture's 'Project ID | Project Name' does not count: read at
        #    40%, it renamed another source's firm link (review, 2026-10-18).
        name_to_code: Dict[Tuple[str, str], str] = {}
        for rec, _d, _h in records():
            if any(v.from_image for v in rec.values):
                continue
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
        #    A name read from a picture explains no initial elsewhere: a CSV's
        #    'D. Whitfield' was guessed to be the picture's Dana Whitfield,
        #    and that guess then confirmed the picture (review, 2026-10-18).
        persons_full: Dict[str, str] = {}
        for rec, _d, _h in records():
            for v in rec.values:
                if v.rule.type == "PERSON" and not is_initial_form(person_key(v.raw)) \
                        and not _signed_initials(v) and not v.from_image:
                    persons_full[person_key(v.raw)] = self._entity("PERSON", v.raw)

        memo = self._memo

        def link(rec: Record, did: str, h: str) -> List[Tuple[Value, Optional[str], bool]]:
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
            return ents

        # Records that rest on a picture come last, so that "known from a
        # source that is not a picture" is checked against the whole vault,
        # in whatever order the files were read.
        self._known_elsewhere = set()
        for rec, did, h in records():
            if any(v.from_image for v in rec.values):
                continue
            for _v, eid, inferred in link(rec, did, h):
                # a guessed initial is not knowing the person (review iii #3);
                # a merge the user made is (#9): compared as merged
                if eid and not inferred:
                    self._known_elsewhere.add(self.resolve(eid))
        for rec, did, h in records():
            if any(v.from_image for v in rec.values):
                link(rec, did, h)

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
        """Each initials question, once every record and wiki-link is read:
        the people whose initials these are (to choose from, never chosen),
        and how often the initials were seen."""
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

        known = getattr(self, "_known_elsewhere", set())

        def sure(v: Value, eid: str) -> bool:
            """A picture's value counts as certain when the OCR was sure of
            it and the vault knows it from a source that is not a picture."""
            return (not v.from_image) or (int(v.locator.get("ocr") or 0) >= PICTURE_SURE
                                          and self.resolve(eid) in known)

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
            pictured = [v for v in (v_s, v_o) if v.from_image]
            checked = sure(v_s, s) and sure(v_o, o)
            st = "suggested" if (s in inferred_ids or o in inferred_ids
                                 or v_s.tentative or v_o.tentative or not checked) else status
            if pictured:
                loc = dict(loc)
                confs = [v.locator.get("ocr") for v in pictured]
                loc["ocr"] = None if any(c is None for c in confs) else min(confs)
                # worded from what the link IS: "every name found elsewhere"
                # was said of a link left suggested (review, 2026-10-18)
                loc["checked"] = checked and st != "suggested"
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
                " ON e.id=a.entity_id WHERE e.type != 'DOCUMENT'"
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

    def known_entities(self, ids: Iterable[str], every_type: bool = False):
        """kg_extract.Known for these entity ids (merged ones resolved):
        people, parts and projects, or ``every_type`` but documents."""
        from council_core import kg_extract as kx
        out = []
        for eid in dict.fromkeys(self.resolve(i) for i in ids):
            e = self.entity(eid)
            if e and (e["type"] in CORE_TYPES or every_type and e["type"] != "DOCUMENT"):
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
                    self._relate(link.subject, link.predicate, link.object,
                                 self.model_link_status(),
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

    # ── open extraction: what a model reads in every passage (Morphik-like) ──
    #: meta keys: where a model's links go ('graph' = in the graph at once,
    #: 'review' = suggested until accepted) and the graph's "what to look
    #: for" (Morphik's prompt override for entity extraction).
    MODEL_LINKS_KEY = "model_links"
    FOCUS_KEY = "focus"

    def model_link_status(self) -> str:
        """The status a model's link gets: 'seeded' (in the graph at once -
        the default, user 2026-10-21: "in the graph at once") or 'suggested'
        (this graph waits for the user's ✓ Accept)."""
        return "suggested" if self.meta(self.MODEL_LINKS_KEY) == "review" else "seeded"

    def set_model_links(self, where: str) -> None:
        """'graph' or 'review'. Links already found move with it (the user's
        own accepted / rejected ones stay as they are)."""
        if where not in ("graph", "review"):
            raise ValueError(where)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                            (self.MODEL_LINKS_KEY, where))
            new = "seeded" if where == "graph" else "suggested"
            # links that only a model's evidence holds up
            self.db.execute(
                "UPDATE relations SET status=?, updated_ts=? WHERE status IN"
                " ('seeded','suggested') AND id IN (SELECT relation_id FROM evidence"
                " WHERE method='model') AND id NOT IN (SELECT relation_id FROM evidence"
                " WHERE method != 'model')", (new, time.time()))
            self._decide("model_links", {"where": where})

    def focus(self) -> str:
        return self.meta(self.FOCUS_KEY) or ""

    def set_focus(self, text: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                            (self.FOCUS_KEY, " ".join(str(text or "").split())[:500]))

    def properties(self, eid: str) -> List[Dict[str, Any]]:
        """What models said this entity is, per document."""
        return [{"name": r["name"], "value": r["value"], "model": r["model"],
                 "path": r["path"], "file": self.display_path(r["path"])}
                for r in self.db.execute(
                    "SELECT p.*, d.path FROM entity_props p JOIN documents d ON"
                    " d.id=p.document_id WHERE p.entity_id=? ORDER BY d.path",
                    (self.resolve(eid),))]

    def _match_entity(self, name: str, etype: str) -> Optional[str]:
        """The graph's entity a model's name means, when exactly one fits:
        its own id (same type and key); else one whose name or alias is this
        name - of the same type, or of any type unless both are people,
        parts or projects of different types (Morphik merged a PERSON and a
        PART of one name: never here)."""
        eid = entity_id(etype, entity_key(etype, name))
        row = self.db.execute("SELECT id FROM entities WHERE id=?", (eid,)).fetchone()
        if row is not None:
            return self.resolve(eid)
        norms = {fold(name)}
        if etype == "PERSON":
            norms.add(person_key(name))
        norms.discard("")
        if not norms:
            return None
        marks = ",".join("?" * len(norms))
        cands = {(self.resolve(r[0]), r[1]) for r in self.db.execute(
            "SELECT a.entity_id, e.type FROM aliases a JOIN entities e ON e.id=a.entity_id"
            f" WHERE a.alias_norm IN ({marks}) AND e.type != 'DOCUMENT'", tuple(norms))}
        same = {c for c in cands if c[1] == etype}
        if len(same) == 1:
            return next(iter(same))[0]
        if same:
            return None
        ok = {c for c in cands if not (c[1] in CORE_TYPES and etype in CORE_TYPES)}
        return next(iter(ok))[0] if len(ok) == 1 else None

    def _chunk_loc(self, c: Dict[str, Any], k: Optional[int]) -> Dict[str, Any]:
        nos = c.get("line_nos") or []
        i = (k or 1) - 1
        line = nos[i] if 0 <= i < len(nos) else (c["first_line"] + i)
        loc: Dict[str, Any] = {"line": line}
        if c["page"] is not None:
            loc["page"] = c["page"]
        return loc

    def _open_chunks(self, d, lines, ments, doc_ents, max_lines: int,
                     overlap: int, index: Optional[Dict[str, Any]] = None
                     ) -> List[Dict[str, Any]]:
        """Every passage of a document (~40 lines, a small overlap; a PDF
        page at most): the open extraction reads them all, as Morphik read
        every chunk - not only the ones a known name was found in."""
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
                wtext = "\n".join(t for _l, t in window)
                if wtext.strip():
                    ents = set()
                    for line, _t in window:
                        ents |= by_spot.get((page, line), set())
                    if index is not None:
                        # a project named by a word of its name ('Atlas sync')
                        ents |= self._projects_named(wtext, index)
                    out.append({"document_id": d["id"], "path": d["path"],
                                "content_hash": d["content_hash"], "chunk": n,
                                "page": page, "first_line": window[0][0],
                                "line_nos": [ln for ln, _t in window], "text": wtext,
                                "entities": (sorted(ents) + sorted(doc_ents - ents))[:60]})
                n += 1
                if start + max_lines >= len(plines):
                    break
        return out

    def _model_entity(self, f, c: Dict[str, Any], run_id: str, model: str) -> str:
        """Write one entity a model found in passage ``c``: the graph's own
        when the name fits one, else a new one; its name as written, a
        mention at the line naming it, and what the model said it is."""
        if f.known is not None:
            eid = self.resolve(f.known.key)
        else:
            eid = next((m for m in (self._match_entity(n, f.type)
                                    for n in [f.name] + list(f.aliases)) if m), None) \
                or self._entity(f.type, f.name, alias_source="model")
        for n in [f.name] + list(f.aliases):
            self._alias(eid, n, "model")
        loc = self._chunk_loc(c, f.line)
        lines = c["text"].splitlines()
        text = lines[f.line - 1] if f.line and 0 < f.line <= len(lines) else f.name
        self._mention(eid, c["document_id"], loc, f.name, _snip(text), "model", run_id)
        if f.description:
            self.db.execute("INSERT OR IGNORE INTO entity_props VALUES (?,?,?,?,?,?,?)",
                            (self.resolve(eid), c["document_id"], c["content_hash"],
                             "description", f.description, model, run_id))
        return self.resolve(eid)

    def extract_from_text(self, workers: Dict[str, Any], *, model: str,
                          focus: Optional[str] = None, on_progress=None,
                          should_stop=lambda: False, only_new: bool = True,
                          max_lines: int = 40, overlap: int = 5) -> Dict[str, Any]:
        """_extract_from_text, with progress lines in the terminal."""
        from progress_report import Progress
        with Progress(f"kg build ({model})") as prog:
            token = _PROGRESS.set(prog)
            try:
                stats = self._extract_from_text(
                    workers, model=model, focus=focus, on_progress=on_progress,
                    should_stop=should_stop, only_new=only_new, max_lines=max_lines,
                    overlap=overlap)
            finally:
                _PROGRESS.reset(token)
            prog.finish(f"{stats['done']} of {stats['chunks']} passages read, "
                        f"{stats['entities']} entities, {stats['links']} link(s)"
                        + (" - stopped, run again to continue" if stats["stopped"] else ""))
            return stats

    def _extract_from_text(self, workers: Dict[str, Any], *, model: str,
                           focus: Optional[str], on_progress, should_stop,
                           only_new: bool, max_lines: int, overlap: int) -> Dict[str, Any]:
        """Morphik's graph build, done the Council's way: a model reads EVERY
        passage of every text document (workbook notes, text boxes and
        pictures included; CSV and JSON tables give theirs through their
        columns), names the entities in it - of any type - and the
        relationships between them (kg_extract.extract_open, which checks
        each against the passage). Entities are matched to the graph's by
        rules (_match_entity); links go in the graph at once or wait for the
        user, as this graph is set (model_link_status). Everything is cited
        at its page and line, with the model's name.

        Resumable as suggest_from_text is: a passage is done when a model of
        this run read this version of it without an error (recorded as
        'open|<model>'); a stopped build continues where it stopped, and an
        unchanged document with the same "what to look for" is not read
        again. ``workers``: as suggest_from_text."""
        import queue as _queue
        import threading as _threading
        from council_core import kg_extract as kx

        focus = self.focus() if focus is None else focus
        wk: Dict[str, Tuple[Any, str]] = {
            name: (w, model) if callable(w) else (w[0], str(w[1]))
            for name, w in workers.items()}
        keys = sorted({f"open|{m}" for _c, m in wk.values()})
        marks = ",".join("?" * len(keys))

        def is_done(doc_id: str, h: str, chunk: int) -> bool:
            return self.db.execute(
                "SELECT 1 FROM extraction_done WHERE document_id=? AND content_hash=? AND"
                f" chunk=? AND error='' AND model IN ({marks})",
                (doc_id, h, chunk, *keys)).fetchone() is not None

        run_id = str(uuid.uuid4())
        status = self.model_link_status()
        index = self._project_index()
        todo: List[Dict[str, Any]] = []
        read = skipped = 0
        prog = progress_now()
        docs = self._text_documents()
        if prog is not None:
            prog.stage("finding passages", total=len(docs), unit="documents")
        with self.db:
            for d in docs:
                if prog is not None:
                    prog.advance(what=self.display_path(d["path"]))
                sig = hashlib.sha256("\0".join(
                    ["open", d["content_hash"], focus, str(max_lines), str(overlap)])
                    .encode("utf-8")).hexdigest()
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
                ments, doc_ents = self._doc_context(d)
                chunks = self._open_chunks(d, lines, ments, doc_ents, max_lines, overlap,
                                           index)
                self.db.execute("INSERT OR REPLACE INTO extraction_docs VALUES (?,?,?)",
                                (d["id"], sig, json.dumps([c["chunk"] for c in chunks])))
                for c in chunks:
                    if only_new and is_done(c["document_id"], c["content_hash"], c["chunk"]):
                        continue
                    c["known"] = self.known_entities(c["entities"], every_type=True)
                    todo.append(c)
        stats = {"chunks": len(todo), "done": 0, "entities": 0, "new_entities": 0,
                 "links": 0, "rejected": 0, "errors": 0, "stopped": False,
                 "by_worker": {}, "documents_read": read, "documents_skipped": skipped,
                 "first_error": ""}
        with self.db:
            self.db.execute("INSERT INTO runs (id, kind, started_ts) VALUES (?,?,?)",
                            (run_id, f"build:{model}", time.time()))
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
                res = kx.extract_open(c["text"], c["known"], chat, focus)
                results.put((name, wmodel, c, res))

        threads = [_threading.Thread(target=work, args=(n, ch, m), name=f"kg-build-{n}",
                                     daemon=True) for n, (ch, m) in wk.items()]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads) or not results.empty():
            try:
                name, wmodel, c, res = results.get(timeout=0.2)
            except _queue.Empty:
                continue
            label = f"{wmodel} @ {name}"
            with self.db:
                before = self.db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
                ids: Dict[int, str] = {}
                for f in res.entities:
                    ids[id(f)] = self._model_entity(f, c, run_id, label)
                stats["new_entities"] += (self.db.execute(
                    "SELECT COUNT(*) FROM entities").fetchone()[0] - before)
                for link in res.links:
                    s, o = ids.get(id(link.subject)), ids.get(id(link.object))
                    if not s or not o:
                        continue
                    quote = link.quote + (f"  [{link.fixed}]" if link.fixed else "")
                    self._relate(s, link.predicate, o, status,
                                 doc_id=c["document_id"], content_hash=c["content_hash"],
                                 locator=self._chunk_loc(c, link.line), quote=quote,
                                 method="model", run_id=run_id, model=label)
                self.db.execute(
                    "INSERT OR REPLACE INTO extraction_done VALUES (?,?,?,?,?,?,?)",
                    (c["document_id"], c["content_hash"], c["chunk"], f"open|{wmodel}",
                     time.time(), len(res.links), res.error))
            stats["done"] += 1
            stats["entities"] += len(res.entities)
            stats["links"] += len(res.links)
            stats["rejected"] += len(res.rejected)
            if res.error:
                stats["errors"] += 1
                stats["first_error"] = stats["first_error"] or res.error
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

    def model_read(self) -> Dict[str, int]:
        """How much of the text a model has read: passages read, and the
        documents with at least one."""
        q = self.db.execute
        return {"passages": q("SELECT COUNT(*) FROM extraction_done WHERE error=''")
                .fetchone()[0],
                "documents": q("SELECT COUNT(DISTINCT document_id) FROM extraction_done"
                               " WHERE error=''").fetchone()[0],
                "text_documents": len(self._text_documents()),
                # reports, notes, PDFs: what has sentences (a workbook's notes
                # count above, but a workbook vault needs no model to link)
                "prose_documents": sum(1 for d in self._text_documents()
                                       if Path(d["path"]).suffix.lower() not in _WORKBOOK)}

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
        desc = self.db.execute("SELECT value FROM entity_props WHERE entity_id=? AND"
                               " name='description' ORDER BY length(value) DESC LIMIT 1",
                               (eid,)).fetchone()
        d["description"] = desc[0] if desc else ""
        return d

    def search(self, text: str, etype: Optional[str] = None, limit: int = 50,
               exact: bool = False) -> List[Dict[str, Any]]:
        """Entities whose name or any alias has a word starting with ``text``
        (accent- and case-insensitive), best first: exact, then prefix, then
        a later word's start. A query with a digit in it is a piece of a code
        ('7720' of BRG-7720, a serial's tail) and matches anywhere in one.
        Initials ('AK', 'A.K.') also find the people whose initials they are,
        and 'D. Whitfield' the people that initial and surname fit; letters
        typed in lower case ('al', 'jb') are a piece of a name first, so the
        people whose initials they spell come after every name hit.

        ``exact``: only the entries whose name or an alias IS ``text`` - for
        a lookup that wants the one entry meant, not a list (the 'Same as…'
        merge)."""
        from council_core import kg_excel
        # 'Lee, Carol' also searches as 'Carol Lee', whichever was seen.
        forms = {f for f in (fold(text), fold(person_display(text))) if f}
        if not forms:
            return []
        best: Dict[str, Tuple[int, Dict[str, Any]]] = {}

        def hit(r, score: int) -> None:
            cur = best.get(r["id"])
            if cur is None or score < cur[0]:
                best[r["id"]] = (score, {"id": r["id"], "type": r["type"],
                                         "name": r["name"]})
        for ft in forms:
            # On word starts: 'AK' found 'Jordan Blake' (blAKe) - review,
            # 2026-10-19. fold() leaves only [a-z0-9 ], so no LIKE wildcard
            # can be in ft, and every word follows exactly one space.
            code = any(ch.isdigit() for ch in ft)
            q = ("SELECT DISTINCT e.id, e.type, e.name, a.alias_norm FROM aliases a"
                 " JOIN entities e ON e.id=a.entity_id WHERE e.merged_into IS NULL")
            if code:
                q += " AND a.alias_norm LIKE ?"
                args: List[Any] = [f"%{ft}%"]
            else:
                q += " AND (a.alias_norm LIKE ? OR a.alias_norm LIKE ?)"
                args = [f"{ft}%", f"% {ft}%"]
            if etype:
                q += " AND e.type=?"
                args.append(etype)
            for r in self.db.execute(q, args):
                norm = r["alias_norm"]
                hit(r, 0 if norm == ft else 1 if norm.startswith(ft)
                    else 2 if f" {ft}" in f" {norm}" else 3)
        if etype in (None, "PERSON") and not exact:
            # Initials: every word's first letter, or the first and last
            # name's (as the initials questions offer their candidates).
            # Typed as initials ('AK', 'J.B.') they rank with a later word's
            # start; in lower case ('al', 'dan') they are a name fragment
            # first, and Anna Lopez came in among the Al... names at the same
            # rank, on every keystroke (review, 2026-10-19).
            if kg_excel.is_initials(text, loose=True):
                rank = 2 if kg_excel.is_initials(text) else 3
                want = kg_excel.initials_key(text).lower()
                if len(want) >= 2:
                    for r in self.db.execute(
                            "SELECT id, type, name, key FROM entities WHERE"
                            " type='PERSON' AND merged_into IS NULL AND key LIKE ?"
                            " AND key LIKE '% %'", (f"{want[0]}%",)):
                        toks = r["key"].split()
                        if want in ("".join(t[0] for t in toks), toks[0][0] + toks[-1][0]):
                            hit(r, rank)
            key = person_key(text)
            if is_initial_form(key):
                for eid in self.initial_candidates(key):
                    r = self.db.execute("SELECT id, type, name FROM entities WHERE id=?"
                                        " AND merged_into IS NULL", (eid,)).fetchone()
                    if r is not None:
                        hit(r, 2)
        ranked = sorted(best.values(), key=lambda x: (x[0], x[1]["name"].lower()))
        if exact:
            ranked = [x for x in ranked if x[0] == 0]
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

    # ── why no links ──
    def why_no_links(self, stats: Optional[Dict[str, Any]] = None, *,
                     keep_labels: bool = False) -> Dict[str, Any]:
        """Why the graph has no links - or not more of them - as plain
        facts, and the most likely reason first with what to do about it.

        A tester saw no links in Connections on a machine nobody else can
        see, and nothing said why (user, 2026-10-20): a Ledger that only the
        Ledger tab's Rebuild added to what the graph reads, every field rule
        still proposed (raw workbooks gave 1 link of 25 until they were
        ticked), inspector initials nobody had named.

        Read from the store and the last rebuild's cache - never a document,
        but for a CSV's header row - so the end of a rebuild can ask it.
        ``text`` holds counts and column / label names only, never a cell
        value or a file name: it can be copied into a message as it is.

        Keys: built, links (seeded + accepted), suggested, relations (by
        status), documents, ledgers, initials_waiting, questions, rules
        (confirmed, proposed, proposed_seen, confirmed_seen), reasons
        ([{code, short, todo}], most likely first), reason (the first, or
        None) and text.

        ``keep_labels``: keep the labels the documents use in the store for
        the next ask of this same graph (_labels_seen) - the rebuild's own
        call, which holds the store; a 'Why no links?' only reads them."""
        import field_search as fs
        import sample_ledger as sl
        from council_core import kg_excel
        q = self.db.execute
        last = q("SELECT stats, finished_ts FROM runs WHERE kind='seed' AND finished_ts"
                 " IS NOT NULL ORDER BY finished_ts DESC LIMIT 1").fetchone()
        if stats is None:
            stats = json.loads(last["stats"]) if last and last["stats"] else {}
        counts = self.counts()
        rels = {k: int(v) for k, v in counts["relations"].items()}
        links = rels.get("seeded", 0) + rels.get("accepted", 0)
        docs = {"read": 0, "ledgers": 0, "workbooks": 0, "text": 0, "unreadable": 0,
                "unreadable_reasons": dict(stats.get("unreadable_reasons")
                                           or unreadable_breakdown(stats.get("unreadable")
                                                                   or []))}
        led: Dict[str, Any] = {"known": 0, "read": 0, "waiting": 0, "unreadable": 0,
                               "not_there": 0, "not_ledger": 0, "rows": 0, "parts": 0,
                               "people": 0, "person_columns": [], "reading_columns": 0,
                               "empty": 0, "no_inspectors": 0, "no_signers": 0}
        known = set()
        #: The CSVs in added folders that were read, but not as a Ledger: an
        #: added Ledger among them lost its schema file.
        plain = set()
        for d in q(f"SELECT d.path, d.status, {_HEAD_SQL} AS records FROM documents d"
                   " LEFT JOIN doc_cache c ON c.document_id=d.id"
                   " WHERE d.status != 'missing'").fetchall():
            summary = _reading_load(d["records"]).get("summary") or {}
            if d["status"] != "ok":
                docs["unreadable"] += 1
                p = self.abs_path(d["path"])
                if p.suffix.lower() == ".csv" and kg_excel.is_ledger_csv(p):
                    known.add(_real(p))
                    led["unreadable"] += 1
                continue
            docs["read"] += 1
            kind = summary.get("kind")
            if kind == "workbook":
                docs["workbooks"] += 1
                continue
            if kind != "ledger":
                docs["text"] += 1
                if d["path"].lower().endswith(".csv") and self._EXTRA_RE.match(d["path"]):
                    plain.add(_real(self.abs_path(d["path"])))
                continue
            docs["ledgers"] += 1
            known.add(_real(self.abs_path(d["path"])))
            led["read"] += 1
            rows = int(summary.get("rows") or 0)
            led["rows"] += rows
            led["parts"] += int(summary.get("parts") or 0)
            led["people"] += len(summary.get("people") or [])
            pcols = list(summary.get("person_columns") or [])
            for c in pcols:
                # 'EC Pre [JB]' names a person: counted, never shown
                if sl.INSPECTOR_COL.match(c):
                    led["reading_columns"] += 1
                elif c not in led["person_columns"]:
                    led["person_columns"].append(c)
            if not rows:
                led["empty"] += 1
            elif not pcols and not summary.get("rule_columns"):
                led["no_inspectors"] += 1
            elif pcols and not summary.get("people"):
                led["no_signers"] += 1
        # The Ledgers the Ledger tab added (add_ledger) that no rebuild has
        # read yet, that are not there now, or that the last rebuild read as
        # a plain table (their schema file went: a rebuild never makes them
        # a Ledger again - review, 2026-10-20).
        for r in self.extra_roots():
            for f in r["files"] or []:
                p = Path(r["path"]) / f
                real = _real(p)
                if real in known:
                    continue
                known.add(real)
                led["not_ledger" if real in plain else
                    "waiting" if p.is_file() else "not_there"] += 1
        led["known"] = len(known)
        initials = q("SELECT COUNT(DISTINCT surface) FROM review WHERE kind='initials'"
                     " AND status='open'").fetchone()[0]
        confirmed = [r for r, _s in self.field_rules("confirmed")]
        proposed = [r for r, _s in self.field_rules("proposed")]
        seen = self._labels_seen({fs._norm_key(r.label) for r in confirmed + proposed},
                                 keep=keep_labels) if last is not None else set()
        rules = {"confirmed": len(confirmed), "proposed": len(proposed),
                 "proposed_seen": [r.label for r in proposed
                                   if fs._norm_key(r.label) in seen],
                 "confirmed_seen": [r.label for r in confirmed
                                    if fs._norm_key(r.label) in seen]}
        out: Dict[str, Any] = {
            "built": last is not None, "last_run": last["finished_ts"] if last else None,
            "links": links, "suggested": rels.get("suggested", 0), "relations": rels,
            "documents": docs, "ledgers": led, "initials_waiting": int(initials),
            "questions": int(counts["open_reviews"]), "rules": rules,
            "model": self.model_read(),
            "entities": {k: v for k, v in counts["entities"].items() if k != "DOCUMENT"}}
        out["reasons"] = _no_links_reasons(out)
        out["reason"] = out["reasons"][0] if out["reasons"] else None
        out["text"] = why_no_links_text(out)
        return out

    #: meta key: the last _labels_seen answer, as JSON {"key", "seen"}.
    LABELS_SEEN_KEY = "labels_seen"

    def _labels_seen(self, want: Collection[str] = (), max_lines: int = 200_000,
                     keep: bool = False) -> set:
        """The labels the documents use (column headers, 'Label:' keys,
        a table's header cells), folded as field_search compares them -
        from the last rebuild's cache: a workbook's and a Ledger's labels
        (not the Ledger's own ID / item / bookkeeping columns), a text
        document's lines; a CSV's header row is read from the file (its
        first 64 KB only). Stops once every label in ``want`` is seen, and
        reads at most ``max_lines`` text lines in all.

        The answer for this graph (its graph_sig: every document's content
        and reading, and the rules) and this ``want`` is kept in the store
        when ``keep`` (the rebuild's call) and given again without reading:
        an unchanged rebuild of 1,800 documents spent a third of its 3.2 s
        here (review, 2026-10-20)."""
        sig = self.meta("graph_sig")
        key = hashlib.sha256(json.dumps([sig, sorted(want), max_lines]).encode(
            "utf-8")).hexdigest() if sig else None
        if key is not None:
            try:
                kept = json.loads(self.meta(self.LABELS_SEEN_KEY) or "{}")
            except ValueError:
                kept = {}
            if isinstance(kept, dict) and kept.get("key") == key:
                return set(kept.get("seen") or [])
        seen = self._scan_labels(want, max_lines)
        if keep and key is not None:
            try:
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                                    (self.LABELS_SEEN_KEY,
                                     json.dumps({"key": key, "seen": sorted(seen)},
                                                ensure_ascii=False)))
            except sqlite3.Error:
                pass                     # only a shortcut: read again next time
        return seen

    def _scan_labels(self, want: Collection[str], max_lines: int) -> set:
        """_labels_seen's reading, uncached."""
        import field_search as fs
        import sample_ledger as sl
        want, seen, left = set(want), set(), max_lines

        def add(label: Any) -> None:
            label = str(label or "").strip()
            if label and len(label.split()) <= 5:          # a sentence is no label
                seen.add(fs._norm_key(label))
        for d in self.db.execute(f"SELECT d.id, d.path, {_HEAD_SQL} AS records FROM"
                                 " documents d JOIN doc_cache c ON c.document_id=d.id"
                                 " WHERE d.status='ok' ORDER BY d.path").fetchall():
            if want and want <= seen:
                break
            head = _reading_load(d["records"])
            if head.get("labels") is not None:
                summary = head.get("summary") or {}
                own = {summary.get("id_field"), summary.get("item_field"), *sl.META_COLS}
                for lab in head["labels"]:
                    if lab not in own:
                        add(lab)
                        if summary.get("kind") == "ledger" and ": " in str(lab):
                            # 'Part Attributes: Project' is read as 'Project'
                            add(str(lab).split(": ", 1)[1])
                continue
            if Path(d["path"]).suffix.lower() in _CSV:
                for lab in _csv_header(self.abs_path(d["path"])):
                    add(lab)
                continue
            if left <= 0:
                continue
            row = self.db.execute("SELECT lines FROM doc_cache WHERE document_id=?",
                                  (d["id"],)).fetchone()
            try:
                lines = json.loads(row["lines"] or "[]") if row else []
            except Exception:
                lines = []
            for x in lines:
                if left <= 0:
                    break
                left -= 1
                text = str(x[2] if isinstance(x, (list, tuple)) and len(x) > 2 else x)
                if text.lstrip().startswith("|"):           # a Markdown table row
                    for cell in text.strip().strip("|").split("|"):
                        add(cell)
                elif any(s in text for s in (":", "=", " - ", " – ", " — ")):
                    for k, _v in fs._kv_pairs(text):
                        add(k)
        seen.discard("")
        return seen

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
            "entity_props": [dict(r) for r in q("SELECT * FROM entity_props ORDER BY"
                                                " entity_id, document_id, name")],
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
