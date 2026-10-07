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

No pickle. Writes are SQLite transactions (atomic). A damaged store is
reported (KnowledgeGraphDamaged), never silently replaced.
"""
from __future__ import annotations

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
PREDICATES = ("LEADS", "CONTACT_FOR", "WORKS_ON", "OWNS", "USES_PART",
              "SUPERSEDES", "DOCUMENTED_IN")
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


def _loc_json(loc: Dict[str, Any]) -> str:
    keep = {k: loc[k] for k in ("sheet", "row", "column", "columns", "page", "line")
            if loc.get(k) is not None}
    return json.dumps(keep, sort_keys=True, ensure_ascii=False)


def locator_text(loc: Any) -> str:
    """A locator as a person reads it: 'sheet Projects, row 2 (Program Lead)',
    'page 1, line 5', 'line 4'."""
    if isinstance(loc, str):
        try:
            loc = json.loads(loc)
        except Exception:
            return loc
    loc = loc or {}
    bits = []
    if loc.get("sheet"):
        bits.append(f"sheet {loc['sheet']}")
    if loc.get("page"):
        bits.append(f"page {loc['page']}")
    if loc.get("row"):
        bits.append(f"row {loc['row']}")
    if loc.get("line"):
        bits.append(f"line {loc['line']}")
    s = ", ".join(bits) or "whole document"
    cols = loc.get("columns") or ([loc["column"]] if loc.get("column") else [])
    if cols:
        s += f" ({', '.join(cols)})"
    return s


# ── reading records out of documents ─────────────────────────────────────
@dataclass
class Value:
    rule: FieldRule
    raw: str
    locator: Dict[str, Any]
    snippet: str


@dataclass
class Record:
    path: str                    # relative to the scanned root
    kind: str                    # row | record | document | skipped
    values: List[Value] = field(default_factory=list)
    #: kind 'skipped': a table row that could not be read (more cells than
    #: the header), reported in the run's coverage instead of vanishing.
    skipped_row: int = 0


_TABULAR = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}
_TEXT = {".txt", ".md", ".markdown", ".rst", ".log", ".pdf", ".docx",
         ".html", ".htm", ".xml", ".yaml", ".yml", ".ini", ".cfg"}
_JSON = {".json"}
SUPPORTED_SUFFIXES = _TABULAR | _TEXT | _JSON


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
    need = {".xlsx": "openpyxl", ".xlsm": "openpyxl", ".pdf": "pypdf",
            ".xls": "xlrd"}.get(suf)
    if need and iu.find_spec(need) is None:
        return f"needs the {need} package, which is not installed"
    return ""


def read_records(p: Path, rel: str, rules: Sequence[FieldRule]) -> List[Record]:
    suf = p.suffix.lower()
    if suf in _TABULAR:
        return _table_records(p, rel, rules)
    if suf in _JSON:
        return _json_records(p, rel, rules)
    return _text_records(p, rel, rules)


#: Part of every doc_cache signature. The reading code's own bytes are in the
#: signature too (see _reader_sig), so this needs bumping only for a change
#: elsewhere that alters what a document reads as.
READER_VERSION = 1


def _reader_sig(rules: Sequence[FieldRule]) -> str:
    """What a cached reading depends on besides the document's bytes: the
    confirmed rules, the reading code (this module, field_search, vault_rag)
    and the reader libraries' versions. Any change = read again: a cache
    that outlived a parser fix would keep the old answers for good."""
    import importlib.util as iu
    h = hashlib.sha256(f"reader {READER_VERSION}".encode("utf-8"))
    for r in sorted(rules, key=lambda r: r.label):
        h.update(r.to_json().encode("utf-8"))
    for mod in ("field_search", "vault_rag"):
        try:
            spec = iu.find_spec(mod)
            h.update(Path(spec.origin).read_bytes() if spec and spec.origin else b"-")
        except Exception:
            h.update(b"?")
    h.update(Path(__file__).read_bytes())
    try:
        from importlib import metadata as md
        for lib in ("pandas", "openpyxl", "pypdf", "python-docx", "xlrd"):
            try:
                h.update(f"{lib}={md.version(lib)}".encode("utf-8"))
            except md.PackageNotFoundError:
                h.update(f"{lib}=-".encode("utf-8"))
    except Exception:
        h.update(b"no-metadata")
    return h.hexdigest()


def _records_json(records: Sequence[Record]) -> str:
    return json.dumps([{"kind": r.kind, "skipped_row": r.skipped_row,
                        "values": [[v.rule.label, v.raw, v.locator, v.snippet]
                                   for v in r.values]} for r in records],
                      ensure_ascii=False)


def _records_load(text: str, rel: str, rules_by_label: Dict[str, FieldRule]
                  ) -> List[Record]:
    """Records from doc_cache. A label no confirmed rule has raises KeyError
    (the caller reads the document instead)."""
    return [Record(rel, d["kind"], [Value(rules_by_label[lab], raw, loc, snip)
                                    for lab, raw, loc, snip in d["values"]],
                   skipped_row=int(d.get("skipped_row") or 0))
            for d in json.loads(text)]


def document_lines(p: Path) -> List[Tuple[Optional[int], int, str]]:
    """Every text line of a document as ``(page or None, line, text)`` —
    for the gazetteer pass. Tables give one line per row ('row' = line)."""
    import field_search as fs
    suf = p.suffix.lower()
    out: List[Tuple[Optional[int], int, str]] = []
    if suf == ".pdf":
        for pno, text in enumerate(fs._pdf_pages(p), start=1):
            for i, ln in enumerate(text.splitlines(), start=1):
                out.append((pno, i, ln))
        return out
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

    def __init__(self, vault: Any, root: Any = None) -> None:
        self.vault = Path(vault)
        self.root = Path(root) if root is not None else self.vault / "data_in"
        self.path = store_path(self.vault)
        #: During seed(): the entities this run supports (see seed).
        self._live: Optional[set] = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._check_before_writing()
        self.db = None
        try:
            self.db = sqlite3.connect(str(self.path))
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
            ro = sqlite3.connect(uri, uri=True)
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
                if suf in _TABULAR:
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

    # ── documents ──
    def document_files(self) -> List[Path]:
        """Supported files under ``root``, skipping dot-folders and the
        protected app folders."""
        if not self.root.exists():
            return []
        try:
            import conversation_logger as cl
        except Exception:
            cl = None
        out = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            rel = p.relative_to(self.root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if cl is not None and cl.is_protected_path(p, self.vault):
                continue
            out.append(p)
        return out

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def _document(self, p: Path, run_id: str) -> Tuple[str, str]:
        rel = self._rel(p)
        h = _file_hash(p)
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
        row = self.db.execute("SELECT name FROM entities WHERE id=?", (eid,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO entities (id, type, key, name, created_ts)"
                            " VALUES (?,?,?,?,?)", (eid, etype, key, name, time.time()))
        elif etype == "PERSON" and len(name) > len(row["name"]):
            # 'Bob Smith' then 'Bob Smith, Jr.': keep the fullest form.
            self.db.execute("UPDATE entities SET name=? WHERE id=?", (name, eid))
        if etype != "DOCUMENT":
            self._alias(eid, raw, alias_source)
        return eid

    def _alias(self, eid: str, alias: str, source: str) -> None:
        eid = self.resolve(eid)          # a merged entity's names live on its survivor
        alias = " ".join(str(alias).split())
        if alias:
            self.db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?,?,?)",
                            (eid, alias, fold(alias), source))

    def resolve(self, eid: str) -> str:
        """Follow user merges to the surviving entity."""
        seen = set()
        while eid and eid not in seen:
            seen.add(eid)
            row = self.db.execute("SELECT merged_into FROM entities WHERE id=?",
                                  (eid,)).fetchone()
            if not row or not row["merged_into"]:
                return eid
            eid = row["merged_into"]
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
        row = self.db.execute("SELECT status FROM relations WHERE id=?", (rid,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?,?)",
                            (rid, s, pred, o, status, now, now))
        elif row["status"] == _PENDING or (row["status"] == "suggested"
                                           and status == "seeded"):
            # First evidence this run (seed() marked every derived status
            # pending), or firmer evidence; the user's accept/reject is never
            # touched.
            self.db.execute("UPDATE relations SET status=?, updated_ts=? "
                            "WHERE id=?", (status, now, rid))
        self.db.execute(
            "INSERT OR IGNORE INTO evidence (relation_id, document_id, content_hash,"
            " locator, quote, method, model, run_id) VALUES (?,?,?,?,?,?,?,?)",
            (rid, doc_id, content_hash, _loc_json(locator), quote, method, model, run_id))
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
             on_progress=None) -> Dict[str, Any]:
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
        run_id = str(uuid.uuid4())
        t0 = time.time()
        rules = [r for r, _s in self.field_rules("confirmed")]
        by_label = {r.label: r for r in rules}
        sig = _reader_sig(rules)
        files = self.document_files()
        stats: Dict[str, Any] = {"documents": len(files), "records": 0,
                                 "unreadable": [], "skipped_rows": [],
                                 "rules": len(rules), "documents_read": 0,
                                 "documents_unchanged": 0}
        with self.db:
            self.db.execute("INSERT INTO runs (id, kind, started_ts) VALUES (?,?,?)",
                            (run_id, "seed", t0))
        # ── 1. reading (batched commits) ──
        seen_paths = set()
        readable: List[Tuple[Path, str, str]] = []
        last, n_batch = time.time(), 0
        try:
            for n, p in enumerate(files):
                if on_progress:
                    on_progress(n, len(files))
                rel = self._rel(p)
                try:
                    did, h = self._document(p, run_id)
                    seen_paths.add(rel)
                    why = missing_reader(p)
                    if why:
                        stats["unreadable"].append(f"{rel}: {why}")
                        self.db.execute("UPDATE documents SET status='unreadable'"
                                        " WHERE id=?", (did,))
                    elif self._cached(did, h, sig) is not None:
                        stats["documents_unchanged"] += 1
                        readable.append((p, did, h))
                    else:
                        recs = read_records(p, rel, rules)
                        lines = [] if p.suffix.lower() in _TABULAR else document_lines(p)
                        self.db.execute(
                            "INSERT OR REPLACE INTO doc_cache VALUES (?,?,?,?,?,?)",
                            (did, h, sig, _records_json(recs),
                             json.dumps(lines, ensure_ascii=False), time.time()))
                        stats["documents_read"] += 1
                        readable.append((p, did, h))
                except Exception as exc:
                    # Listed but not readable now (held open with an exclusive
                    # lock by another program, a parse error): 'unreadable',
                    # with what it had - not 'missing'. It never reached
                    # seen_paths, so it was marked missing and its model links
                    # were deleted (review, 2026-10-07: locked once, its links
                    # were gone for good).
                    stats["unreadable"].append(f"{rel}: {exc.__class__.__name__}")
                    seen_paths.add(rel)
                    self.db.execute("UPDATE documents SET status='unreadable' WHERE path=?",
                                    (rel,))
                n_batch += 1
                if n_batch >= self.SEED_BATCH or time.time() - last >= self.SEED_BATCH_S:
                    self.db.commit()
                    last, n_batch = time.time(), 0
            self.db.commit()
        except BaseException:
            self.db.rollback()          # the batch in progress; earlier ones stay
            raise
        # ── 2. the graph (one transaction) ──
        try:
            with self.db:
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
                self._live = {r[0] for r in self.db.execute(
                    "SELECT entity_id FROM aliases WHERE source='user'"
                    " UNION SELECT merged_into FROM entities WHERE merged_into IS NOT NULL"
                    " UNION SELECT subject_id FROM relations WHERE status='accepted'"
                    " UNION SELECT object_id FROM relations WHERE status='accepted'")}
                records: List[Tuple[Record, str, str]] = []
                for p, did, h in readable:
                    row = self._cached(did, h, sig)
                    try:
                        recs = (_records_load(row["records"], self._rel(p), by_label)
                                if row is not None else None)
                    except Exception:
                        recs = None
                    if recs is None:            # not expected: read it after all
                        try:
                            recs = read_records(p, self._rel(p), rules)
                        except Exception as exc:
                            stats["unreadable"].append(
                                f"{self._rel(p)}: {exc.__class__.__name__}")
                            continue
                    for rec in recs:
                        if rec.kind == "skipped":
                            stats["skipped_rows"].append(
                                f"{rec.path}: row {rec.skipped_row} has more cells "
                                "than the header")
                            continue
                        records.append((rec, did, h))
                # Gone since the last run: kept (decisions may cite them), marked.
                for r in self.db.execute("SELECT id, path FROM documents").fetchall():
                    if r["path"] not in seen_paths:
                        self.db.execute("UPDATE documents SET status='missing' WHERE id=?",
                                        (r["id"],))
                # The cache holds a document's text: none is kept for a file
                # that was deleted or can no longer be read.
                keep = [did for _p, did, _h in readable]
                self.db.execute("CREATE TEMP TABLE IF NOT EXISTS _kg_keep (id TEXT PRIMARY KEY)")
                self.db.execute("DELETE FROM _kg_keep")
                self.db.executemany("INSERT OR IGNORE INTO _kg_keep VALUES (?)",
                                    [(d,) for d in keep])
                self.db.execute("DELETE FROM doc_cache WHERE document_id NOT IN"
                                " (SELECT id FROM _kg_keep)")
                stats["records"] = len(records)
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
                self._seed_records(records, run_id)
                if use_collections:
                    stats["collections"] = self._seed_collections(run_id)
                if gazetteer:
                    stats["gazetteer_mentions"] = self._gazetteer(files, run_id)
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
                self.db.execute("UPDATE runs SET finished_ts=?, stats=? WHERE id=?",
                                (time.time(), json.dumps(stats), run_id))
        finally:
            self._live = None
        stats["seconds"] = round(time.time() - t0, 3)
        return stats

    def _seed_records(self, records, run_id: str) -> None:
        # 1. Aliases first: 'Project Name' beside 'Project ID' ties the name
        #    to the code, so 'Helios Turbine Upgrade' elsewhere is PRJ-0915.
        name_to_code: Dict[Tuple[str, str], str] = {}
        for rec, _d, _h in records:
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
        #    Marcus Oyelaran no matter which file was read first.
        persons_full: Dict[str, str] = {}
        for rec, _d, _h in records:
            for v in rec.values:
                if v.rule.type == "PERSON" and not is_initial_form(person_key(v.raw)):
                    persons_full[person_key(v.raw)] = self._entity("PERSON", v.raw)

        for rec, did, h in records:
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
        left unlinked with a review."""
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
        self.db.execute(
            "INSERT OR IGNORE INTO mentions (entity_id, document_id, locator, surface,"
            " snippet, method, run_id) VALUES (?,?,?,?,?,?,?)",
            (eid, did, _loc_json(loc), surface, snippet, method, run_id))

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
        # about one project; otherwise the user decides.
        n_proj = len({e for _v, e in main.get("PROJECT", [])})
        firm = rec.kind in ("row", "record") or n_proj <= 1
        status = "seeded" if firm else "suggested"

        def rel(s, pred, o, v_s: Value, v_o: Value, at: Value):
            # ``at`` is the field that DEFINES the link (the person's field,
            # the 'Supersedes' field, the part's field), so the citation
            # points at the line that says it.
            loc = at.locator
            if rec.kind == "row":
                # The row, and the two cells the link rests on.
                loc = {k: v_s.locator.get(k) for k in ("sheet", "row")}
                loc["columns"] = list(dict.fromkeys(
                    [v_s.locator.get("column"), v_o.locator.get("column")]))
            quote = v_s.snippet if v_s.snippet == v_o.snippet else \
                f"{v_s.snippet} | {v_o.snippet}"
            st = "suggested" if (s in inferred_ids or o in inferred_ids) else status
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

    def _gazetteer(self, files: Sequence[Path], run_id: str) -> int:
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
            terms.append((_term_re(alias), r["entity_id"], alias))
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
            if p.suffix.lower() in _TABULAR:
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
        """The readable documents whose free text may hold links (tables and
        JSON give theirs through labelled fields)."""
        return [d for d in self.db.execute(
                    "SELECT id, path, content_hash FROM documents WHERE status='ok'"
                    " ORDER BY path").fetchall()
                if Path(d["path"]).suffix.lower() not in _TABULAR | _JSON]

    def _doc_lines(self, d) -> Optional[List[Tuple[Optional[int], int, str]]]:
        try:
            return self._cached_lines(d["id"], d["content_hash"], self.root / d["path"])
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
        with self.db:
            for d in self._text_documents():
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
                    line = (c["first_line"] + (link.line or 1) - 1)
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

    def neighbors(self, eid: str, *, include_rejected: bool = False
                  ) -> List[Dict[str, Any]]:
        """Every relation touching ``eid``, each with its evidence: what a
        person needs to see WHY two things are linked."""
        eid = self.resolve(eid)
        q = ("SELECT * FROM relations WHERE (subject_id=? OR object_id=?)")
        if not include_rejected:
            q += " AND status != 'rejected'"
        out = []
        for r in self.db.execute(q + " ORDER BY predicate", (eid, eid)):
            other = r["object_id"] if r["subject_id"] == eid else r["subject_id"]
            out.append({"relation_id": r["id"], "predicate": r["predicate"],
                        "direction": "out" if r["subject_id"] == eid else "in",
                        "status": r["status"], "other": self.entity(other),
                        "evidence": self.evidence(r["id"])})
        return out

    def evidence(self, rid: str) -> List[Dict[str, Any]]:
        return [{"path": e["path"], "document_id": e["document_id"],
                 "locator": json.loads(e["locator"]),
                 "where": locator_text(e["locator"]), "quote": e["quote"],
                 "method": e["method"], "model": e["model"],
                 "stale": e["content_hash"] != e["doc_hash"]}
                for e in self.db.execute(
                    "SELECT ev.*, d.path, d.content_hash AS doc_hash FROM evidence ev"
                    " JOIN documents d ON d.id=ev.document_id WHERE ev.relation_id=?"
                    " ORDER BY d.path, ev.locator", (rid,))]

    def mentions(self, eid: str) -> List[Dict[str, Any]]:
        return [{"path": m["path"], "where": locator_text(m["locator"]),
                 "locator": json.loads(m["locator"]), "surface": m["surface"],
                 "snippet": m["snippet"], "method": m["method"]}
                for m in self.db.execute(
                    "SELECT m.*, d.path FROM mentions m JOIN documents d ON"
                    " d.id=m.document_id WHERE m.entity_id=? ORDER BY d.path, m.locator",
                    (self.resolve(eid),))]

    def reviews(self, status: str = "open") -> List[Dict[str, Any]]:
        out = []
        for r in self.db.execute(
                "SELECT rv.*, d.path FROM review rv LEFT JOIN documents d ON"
                " d.id=rv.document_id WHERE rv.status=? ORDER BY rv.id", (status,)):
            out.append({"id": r["id"], "kind": r["kind"], "surface": r["surface"],
                        "candidates": [self.entity(c) for c in json.loads(r["candidates"])],
                        "path": r["path"], "where": locator_text(r["locator"]),
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


def source_context(root: Any, rel_path: str, locator: Any, *, radius: int = 3
                   ) -> List[Tuple[str, str, bool]]:
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
