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

SCHEMA_VERSION = 1
STORE_DIR = ".knowledge_graph"
STORE_NAME = "graph.sqlite"

ENTITY_TYPES = ("PERSON", "PART", "PROJECT", "DOCUMENT")
PREDICATES = ("LEADS", "CONTACT_FOR", "WORKS_ON", "OWNS", "USES_PART",
              "SUPERSEDES", "DOCUMENTED_IN")
STATUSES = ("seeded", "suggested", "accepted", "rejected")

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
    kind: str                    # row | record | document
    values: List[Value] = field(default_factory=list)


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
    for sheet, frame in fs._table_frames(p):
        cols = [(c, by_norm.get(fs._norm_key(c))) for c in frame.columns]
        cols = [(c, r) for c, r in cols if r is not None]
        if not cols:
            continue
        for idx, row in enumerate(frame.itertuples(index=False, name=None)):
            rowd = dict(zip(frame.columns, row))
            rec = Record(rel, "row")
            for c, r in cols:
                raw = rowd.get(c)
                if raw is None or (isinstance(raw, float) and raw != raw):
                    continue
                raw = str(raw).strip()
                if not raw or raw.lower() == "nan":
                    continue
                loc = {"row": idx + 2, "column": str(c)}
                if sheet is not None:
                    loc["sheet"] = sheet
                for one in (fs._split_values(raw) or [raw]):
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
                i = fs._line_of(lines, json.dumps(raw, ensure_ascii=False)[1:-1],
                                cursor[0])
                if i >= 0:
                    cursor[0] = i
                loc = {"line": i + 1} if i >= 0 else {}
                snip = fs._snippet(lines[i]) if i >= 0 else raw
                for one in (fs._split_values(raw) or [raw]):
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
    Record. When two rules match the same value on the same line ('Project'
    and 'Project ID'), the longer label wins."""
    import field_search as fs
    best: Dict[tuple, Value] = {}
    for r in rules:
        for loc in fs.field_value_locations(p, r.label):
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        try:
            self.db = sqlite3.connect(str(self.path))
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys = ON")
            if existed:
                self.db.execute("PRAGMA schema_version").fetchone()
                ok = self.db.execute("PRAGMA quick_check").fetchone()[0]
                if ok != "ok":
                    raise sqlite3.DatabaseError(ok)
            with self.db:
                self.db.executescript(SCHEMA_SQL)
                self.db.execute("INSERT OR IGNORE INTO meta VALUES "
                                "('schema_version', ?)", (str(SCHEMA_VERSION),))
                for r in DEFAULT_FIELD_RULES:
                    self.db.execute("INSERT OR IGNORE INTO field_rules VALUES "
                                    "(?, ?, 'proposed')", (r.label, r.to_json()))
        except sqlite3.DatabaseError as exc:
            raise KnowledgeGraphDamaged(
                f"the knowledge graph store {self.path} could not be read "
                f"({exc}); it was left untouched") from exc
        ver = self.meta("schema_version")
        if ver and int(ver) > SCHEMA_VERSION:
            raise KnowledgeGraphDamaged(
                f"{self.path} was written by a newer Council (schema {ver}); "
                f"this one reads up to {SCHEMA_VERSION}")

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
        rid = relation_id(s, pred, o)
        now = time.time()
        row = self.db.execute("SELECT status FROM relations WHERE id=?", (rid,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?,?)",
                            (rid, s, pred, o, status, now, now))
        elif row["status"] == "suggested" and status == "seeded":
            # Firmer evidence arrived; the user's accept/reject is never touched.
            self.db.execute("UPDATE relations SET status='seeded', updated_ts=? "
                            "WHERE id=?", (now, rid))
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
    def seed(self, *, use_collections: bool = True, gazetteer: bool = True,
             on_progress=None) -> Dict[str, Any]:
        """Rebuild everything that comes from documents, with no model:
        labelled fields (confirmed rules only), Collections, and a gazetteer
        pass. The user's decisions and accepted/rejected statuses survive.
        Returns the run's stats (also stored in ``runs``)."""
        run_id = str(uuid.uuid4())
        t0 = time.time()
        rules = [r for r, _s in self.field_rules("confirmed")]
        files = self.document_files()
        stats: Dict[str, Any] = {"documents": len(files), "records": 0,
                                 "unreadable": [], "rules": len(rules)}
        with self.db:
            self.db.execute("INSERT INTO runs (id, kind, started_ts) VALUES (?,?,?)",
                            (run_id, "seed", t0))
            # Derived rows are rebuilt; decisions and user statuses are kept.
            self.db.execute("DELETE FROM evidence WHERE method != 'model'")
            self.db.execute("DELETE FROM mentions")
            self.db.execute("DELETE FROM review WHERE status='open'")
            self.db.execute("DELETE FROM aliases WHERE source != 'user'")
            seen_paths = set()
            records: List[Tuple[Record, str, str]] = []
            for n, p in enumerate(files):
                if on_progress:
                    on_progress(n, len(files))
                try:
                    did, h = self._document(p, run_id)
                    seen_paths.add(self._rel(p))
                    why = missing_reader(p)
                    if why:
                        stats["unreadable"].append(f"{self._rel(p)}: {why}")
                        self.db.execute("UPDATE documents SET status='unreadable'"
                                        " WHERE id=?", (did,))
                        continue
                    for rec in read_records(p, self._rel(p), rules):
                        records.append((rec, did, h))
                except Exception as exc:
                    stats["unreadable"].append(f"{self._rel(p)}: {exc.__class__.__name__}")
            # Gone since the last run: kept (decisions may cite them), marked.
            for r in self.db.execute("SELECT id, path FROM documents").fetchall():
                if r["path"] not in seen_paths:
                    self.db.execute("UPDATE documents SET status='missing' WHERE id=?",
                                    (r["id"],))
            stats["records"] = len(records)
            self._seed_records(records, run_id)
            if use_collections:
                stats["collections"] = self._seed_collections(run_id)
            if gazetteer:
                stats["gazetteer_mentions"] = self._gazetteer(files, run_id)
            # A seeded/suggested relation whose evidence is all gone (the
            # document changed or the rule was withdrawn) goes too; the user's
            # accepted/rejected ones stay.
            self.db.execute(
                "DELETE FROM relations WHERE status IN ('seeded','suggested') AND id"
                " NOT IN (SELECT relation_id FROM evidence)")
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
        initial: 'd whitfield' -> Dana Whitfield, Dan Whitfield."""
        ini, surname = key.split()
        out = []
        for r in self.db.execute("SELECT id, key FROM entities WHERE type='PERSON'"
                                 " AND merged_into IS NULL"):
            toks = r["key"].split()
            if (len(toks) >= 2 and toks[-1] == surname and toks[0].startswith(ini)
                    and len(toks[0]) > 1):
                out.append(r["id"])
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
        # Initial forms of every full-named person: 'D. Whitfield'.
        initials: Dict[str, List[str]] = {}
        for r in self.db.execute("SELECT id, key FROM entities WHERE type='PERSON'"):
            toks = r["key"].split()
            if len(toks) >= 2 and len(toks[0]) > 1:
                initials.setdefault(f"{toks[0][0]} {toks[-1]}", []).append(r["id"])
        n = 0
        for p in files:
            if p.suffix.lower() in _TABULAR:
                continue
            row = self.db.execute("SELECT id FROM documents WHERE path=?",
                                  (self._rel(p),)).fetchone()
            if row is None:
                continue
            did = row["id"]
            for page, line, text in document_lines(p):
                loc = {"line": line}
                if page is not None:
                    loc["page"] = page
                for rx, eid, alias in terms:
                    if rx.search(text) and not self._has_mention(eid, did, loc):
                        self._mention(eid, did, loc, alias, _snip(text), "gazetteer", run_id)
                        n += 1
                for m in _INITIAL_NAME_RE.finditer(text):
                    key = f"{m.group(1).lower()} {fold(m.group(2))}"
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
        for rel in self.all_relations(include_rejected=True):
            for ev in self.evidence(rel["id"]) or [{}]:
                w.writerow([rel["subject"]["type"], rel["subject"]["name"],
                            rel["predicate"], rel["object"]["type"], rel["object"]["name"],
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
                i = int(locator["row"]) - 2
                if not 0 <= i < len(frame):
                    return []
                row = frame.iloc[i]
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


def _term_re(alias: str) -> re.Pattern:
    """Whole-token, case-insensitive match for one alias: 'PN-1234/A' does not
    match inside 'PN-1234/AB', 'Carol Lee' does not match 'Carol Leeds'."""
    return re.compile(r"(?<![\w/\-])" + re.escape(alias) + r"(?![\w/\-])", re.I)


def _snip(text: str, limit: int = 200) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1] + "…"
