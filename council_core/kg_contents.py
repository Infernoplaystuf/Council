"""The graph's Contents layer: every file, sheet, header, label and value.

User, 2026-10-26: "the connections tab is too strict with the connections
it is trying to make. Basically ALL of the input data should be in the
knowledge graph for example an excel file has the head node with the file
name then lets say stat headers/labels and then that data is connected to
the labels". The people / parts / projects the field rules give stay as the
graph's meaning layer; this module keeps what they threw away: every cell of
every CSV and workbook, under the header or label it sits under.

Agreed with the user (proposal, 2026-10-26):
  * a numeric column is one summary (count, lowest, highest) that lists its
    values on request - not a dot per number;
  * at most ``DRAWN_VALUES`` different values of a column are listed, the
    rest stay searchable;
  * the same text value in several files is ONE value (``vals``), so files
    connect with no rules. Numbers are never merged: every '1' would link
    everything.

Structure, in the graph's own SQLite file (schema v5, ``SCHEMA_SQL``):

  contents_docs  one row per document read into the layer, at the content
                 hash it was read at (an unchanged file is not read again).
  fields         a column header ('column'), a label ('label': 'Inspector:'
                 beside its value, or 'Inspector: JB' in one cell), or the
                 cells under no label of a sheet ('loose'); per document and
                 sheet, with a summary of its values.
  vals           a distinct text value, folded (case and spacing), shared by
                 every file that has it.
  cells          every value: its field, row and column, its text, its
                 number when it is one, its ``vals`` id when it is text. A
                 row is the cells of one sheet's fields with the same row.
  vals_fts, fields_fts  full-text search over the values and the labels.

Text documents (md, txt, PDF, Word, HTML ...) and JSON (step 2, user
2026-10-26 "begin working on any next steps"): a text document's lines are
read as one-cell rows of a sheet per PDF page ('page 3'; one sheet with no
name otherwise), so 'Label: value' lines are labels and the rest are loose
lines; a line split by '|' or tabs is a row of cells, so a Markdown or
pasted table gets its headers like a sheet's (``unit`` 'line': a cell is
cited by page and line). A JSON file: each object is a row, each key a
field, a scalar under no key is loose (``unit`` 'json': cited by line).

Reading costs nothing extra: the readers the graph already runs feed this
layer the rows they read (``Sink``, set per document by the rebuild through
knowledge_graph._CONTENTS). A workbook is fed one row at a time with the next
as a look-ahead, as kg_excel's reader is; a sheet's cells go to a temporary
file, not memory, so a 100k-row sheet holds two rows.

How a grid is read (no rules - any header counts):
  * a header is a row of two or more short text cells, no 'Label:' among
    them, with values under at least half of them in the next row. Its
    table runs until a blank row; a cell under no title is '(column C)'.
  * outside a table: 'Label:' with a value to its right, 'Label: value' in
    one cell, or a short word cell with a value right beside it are a label
    and its value.
  * anything else is a loose cell of its sheet: kept and searchable.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Different values of one column listed in the Contents tree (the rest are
#: searchable). User, 2026-10-26: "about 200".
DRAWN_VALUES = 200
#: A column is numeric when at least this share of its cells are numbers.
NUMERIC_SHARE = 0.8
#: Characters of one cell kept (a pasted report in a cell stays searchable
#: by its start; the file has the rest).
MAX_CELL_CHARS = 2000
#: Words a label may have; longer text is a value or a note.
MAX_LABEL_WORDS = 6
LOOSE = "(no label)"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS contents_docs (
    document_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
    fields INTEGER NOT NULL, cells INTEGER NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    sheet TEXT NOT NULL,               -- '' for a CSV; 'Visual › picture F2'
    label TEXT NOT NULL, label_norm TEXT NOT NULL,
    kind TEXT NOT NULL,                -- column | label | loose
    loc_sheet TEXT NOT NULL DEFAULT '',-- the sheet a citation names
    image TEXT NOT NULL DEFAULT '',    -- a picture's anchor, read by OCR
    unit TEXT NOT NULL DEFAULT 'cell', -- cell (a grid) | line (text) | json
    first_row INTEGER, col INTEGER,    -- where the header / label is
    ord INTEGER NOT NULL,              -- order found
    n_cells INTEGER NOT NULL, n_numbers INTEGER NOT NULL,
    n_distinct INTEGER NOT NULL, num_min REAL, num_max REAL
);
CREATE INDEX IF NOT EXISTS fields_document ON fields(document_id);
CREATE INDEX IF NOT EXISTS fields_norm ON fields(label_norm);
CREATE TABLE IF NOT EXISTS vals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    norm TEXT NOT NULL UNIQUE, text TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0       -- cells that hold it
);
CREATE TABLE IF NOT EXISTS cells (
    field_id INTEGER NOT NULL, row INTEGER NOT NULL, col INTEGER NOT NULL,
    text TEXT NOT NULL, num REAL, value_id INTEGER
);
CREATE INDEX IF NOT EXISTS cells_field_row ON cells(field_id, row);
CREATE INDEX IF NOT EXISTS cells_value ON cells(value_id) WHERE value_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS cells_num ON cells(num) WHERE num IS NOT NULL;
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS vals_fts USING fts5(
    text, content='vals', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS fields_fts USING fts5(
    label, sheet, content='fields', content_rowid='id');
"""


def create(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA_SQL)
    # v5 -> v6: fields.unit (text and JSON in the layer)
    cols = {r[1] for r in db.execute("PRAGMA table_info(fields)")}
    if "unit" not in cols:
        db.execute("ALTER TABLE fields ADD COLUMN unit TEXT NOT NULL DEFAULT 'cell'")
    if fts_available(db):
        db.executescript(FTS_SQL)


_FTS: Dict[int, bool] = {}


def fts_available(db: sqlite3.Connection) -> bool:
    """SQLite's full-text search is built into the Python that ships with
    Windows and most Linux; without it search falls back to LIKE."""
    k = id(db)
    if k not in _FTS:
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp._kg_fts_probe USING fts5(x)")
            db.execute("DROP TABLE IF EXISTS temp._kg_fts_probe")
            _FTS[k] = True
        except sqlite3.Error:
            _FTS[k] = False
    return _FTS[k]


def _has_fts(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='vals_fts'").fetchone() \
        is not None


# ── cells as text and numbers ────────────────────────────────────────────
_NUMBER_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d+)?(?:[eE][-+]?\d+)?")


def cell_text(v: Any) -> str:
    from sample_ledger import _text
    s = _text(v)
    return s[:MAX_CELL_CHARS]


def as_number(v: Any, text: str) -> Optional[float]:
    """The number a cell holds: an int or float, or text that is only a
    number ('42.1', '1,250', '-3e-4'). A code with digits ('PN-1234',
    '0042' with its leading zero kept as typed) is text."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    s = text.strip()
    if not s or not any(ch.isdigit() for ch in s):
        return None
    if len(s) > 1 and s[0] == "0" and s[1].isdigit():
        return None                   # '0042' is a code, not 42
    if not _NUMBER_RE.fullmatch(s):
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def fold(text: str) -> str:
    """One value whatever its case and spacing: 'PN-4471', 'pn-4471 '."""
    return " ".join(text.casefold().split())


def label_norm(label: str) -> str:
    import field_search as fs
    return fs._norm_key(label) or fold(label)


def col_letter(c: int) -> str:
    s = ""
    while c > 0:
        c, rem = divmod(c - 1, 26)
        s = chr(65 + rem) + s
    return s


def _merged(v: Any) -> bool:
    return type(v).__name__ == "_Merged"


def _label_text(v: Any) -> str:
    return " ".join(str(v).split()).rstrip(" :")


def _wordish(v: Any) -> bool:
    """A cell that can be a title or a label: short text with a letter in
    it, not a number."""
    if not isinstance(v, str) or _merged(v):
        return False
    s = v.strip()
    if not s or len(s) > 80 or "\n" in s:
        return False
    if not re.search(r"[^\W\d_]", s):
        return False
    return as_number(v, s) is None and len(s.split()) <= MAX_LABEL_WORDS + 2


# ── collecting one document ──────────────────────────────────────────────
class _Field:
    __slots__ = ("idx", "sheet", "label", "kind", "loc_sheet", "image", "first_row",
                 "col", "n_cells", "n_numbers", "num_min", "num_max", "distinct", "unit")

    def __init__(self, idx, sheet, label, kind, loc_sheet, image, first_row, col,
                 unit="cell"):
        self.idx, self.sheet, self.label, self.kind = idx, sheet, label, kind
        self.loc_sheet, self.image, self.unit = loc_sheet, image, unit
        self.first_row, self.col = first_row, col
        self.n_cells = self.n_numbers = 0
        self.num_min: Optional[float] = None
        self.num_max: Optional[float] = None
        #: folded texts seen, up to a bound (a summary, not the store)
        self.distinct: set = set()


class Sink:
    """Everything one document holds, as fields and cells. Cells are spooled
    to a temporary file as they come (a sheet of millions of cells does not
    sit in memory); ``store`` writes them into the graph."""

    #: distinct values a field counts exactly; past it the count says "N+"
    DISTINCT_CAP = 100_000

    def __init__(self):
        self.fields: List[_Field] = []
        self._by_key: Dict[Tuple[str, str, str], _Field] = {}
        self._spool = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.n_cells = 0
        self.closed = False

    # one sheet / text box / picture of a workbook
    def grid(self, name: str, loc_sheet: str = "", image: str = "",
             unit: str = "cell") -> "GridContents":
        return GridContents(self, name, loc_sheet if unit != "cell" else (loc_sheet or name),
                            image, unit)

    def field(self, sheet: str, label: str, kind: str, *, loc_sheet: str = "",
              image: str = "", row: Optional[int] = None, col: Optional[int] = None,
              unit: str = "cell") -> _Field:
        key = (sheet, label_norm(label) if kind != "loose" else LOOSE, kind)
        f = self._by_key.get(key)
        if f is None:
            f = _Field(len(self.fields), sheet, label, kind,
                       loc_sheet if unit != "cell" else (loc_sheet or sheet), image,
                       row, col, unit)
            self.fields.append(f)
            self._by_key[key] = f
        return f

    def cell(self, f: _Field, row: int, col: int, v: Any) -> None:
        if self.closed:
            return                     # a read the rebuild left behind
        text = cell_text(v)
        if not text:
            return
        num = as_number(v, text)
        f.n_cells += 1
        if num is not None:
            f.n_numbers += 1
            f.num_min = num if f.num_min is None else min(f.num_min, num)
            f.num_max = num if f.num_max is None else max(f.num_max, num)
        if len(f.distinct) < self.DISTINCT_CAP:
            f.distinct.add(fold(text) if num is None else text)
        self.n_cells += 1
        self._spool.write(json.dumps([f.idx, row, col, text, num], ensure_ascii=False))
        self._spool.write("\n")

    def table(self, sheet: Optional[str], columns: Sequence[Any],
              rows: Iterable[Tuple[int, Sequence[Any]]]) -> None:
        """A CSV (or any frame): ``columns`` are its header, ``rows`` (row
        number a person sees, values in column order)."""
        name = sheet or ""
        fs_ = []
        for c, title in enumerate(columns, start=1):
            t = str(title or "").strip()
            if not t or t.lower().startswith("unnamed:"):
                t = f"(column {col_letter(c)})"
            fs_.append(self.field(name, t, "column", row=1, col=c))
        for rowno, vals in rows:
            for c, (f, v) in enumerate(zip(fs_, vals), start=1):
                if v is None or (isinstance(v, float) and v != v):
                    continue
                if isinstance(v, str) and (not v.strip() or v.strip().lower() == "nan"):
                    continue
                self.cell(f, int(rowno), c, v)

    def text_lines(self, lines: Iterable[Tuple[Any, int, str]]) -> None:
        """A text document's lines, ``(page or None, line, text)`` as
        knowledge_graph.document_lines gives them: a sheet per PDF page."""
        grid: Optional[GridContents] = None
        page: Any = object()
        for pno, ln, text in lines:
            if pno != page:
                if grid is not None:
                    grid.close()
                page = pno
                grid = self.grid(f"page {pno}" if pno else "",
                                 loc_sheet=str(pno) if pno else "", unit="line")
            cells = _line_cells(text)
            if cells:
                grid.feed(int(ln), cells)
            elif _TABLE_RULE.match(text.strip() or " ") and "-" in text:
                grid.bridge(int(ln))
        if grid is not None:
            grid.close()

    def json_data(self, text: str, data: Any) -> None:
        """A JSON file: each object a row (numbered in the order found),
        each scalar key a field, cited by the line it is on."""
        import field_search as fs
        cursor = [0]
        row = [0]

        def line_of(k, v) -> int:
            try:
                i, end = fs.json_pair_line(text, k, v, cursor[0])
            except Exception:
                return 0
            if i >= 0:
                cursor[0] = end
                return i + 1
            return 0

        def walk(o, key: str) -> None:
            if isinstance(o, dict):
                scalars = [(k, v) for k, v in o.items()
                           if not isinstance(v, (dict, list)) and v is not None]
                if scalars:
                    row[0] += 1
                    for k, v in scalars:
                        f = self.field("", str(k), "column", unit="json")
                        self.cell(f, row[0], line_of(k, v), v)
                for k, v in o.items():
                    if isinstance(v, (dict, list)):
                        walk(v, str(k))
            elif isinstance(o, list):
                for v in o:
                    if isinstance(v, (dict, list)):
                        walk(v, key)
                    elif v is not None:
                        row[0] += 1
                        f = self.field("", key, "column", unit="json") if key else \
                            self.field("", LOOSE, "loose", unit="json")
                        self.cell(f, row[0], 0, v)
            elif o is not None:
                self.cell(self.field("", LOOSE, "loose", unit="json"), 1, 0, o)
        walk(data, "")

    def frames(self, frames) -> None:
        for sheet, frame in frames:
            self.table(sheet, list(frame.columns),
                       zip(frame.index, frame.itertuples(index=False, name=None)))

    def cells(self) -> Iterable[list]:
        self._spool.flush()
        self._spool.seek(0)
        for line in self._spool:
            if line.strip():
                yield json.loads(line)

    def close(self) -> None:
        self.closed = True
        try:
            self._spool.close()
        except Exception:
            pass


class GridContents:
    """One sheet fed row by row (r, {column: value}), in row order, with the
    next row as a look-ahead - as kg_excel._GridReader is fed."""

    def __init__(self, sink: Sink, name: str, loc_sheet: str, image: str,
                 unit: str = "cell"):
        self.sink, self.name, self.loc_sheet, self.image = sink, name, loc_sheet, image
        self.unit = unit
        #: rows that hold nothing but join their neighbours (a Markdown
        #: table's '|---|---|' line between its header and its first row)
        self._bridged: set = set()
        self._pending: Optional[Tuple[int, Dict[int, Any]]] = None
        self._last_r: Optional[int] = None
        #: the running table: column -> field, and its span
        self._cols: Dict[int, _Field] = {}
        self._span: Tuple[int, int] = (0, -1)

    def bridge(self, r: int) -> None:
        self._bridged.add(r)

    def _next_to(self, a: int, b: int) -> bool:
        return b == a + 1 or (b > a and all(x in self._bridged for x in range(a + 1, b)))

    def feed(self, r: int, cells: Dict[int, Any]) -> None:
        if self._pending is not None:
            pr, pc = self._pending
            self._row(pr, pc, cells if self._next_to(pr, r) else {})
        self._pending = (r, cells)

    def close(self) -> None:
        if self._pending is not None:
            self._row(*self._pending, {})
            self._pending = None

    def _field(self, label: str, kind: str, r: int, c: int) -> _Field:
        return self.sink.field(self.name, label, kind, loc_sheet=self.loc_sheet,
                               image=self.image, row=r, col=c, unit=self.unit)

    def _is_header(self, cells: Dict[int, Any], nxt: Dict[int, Any]) -> bool:
        real = {c: v for c, v in cells.items() if not _merged(v)}
        titles = [c for c, v in real.items() if _wordish(v)]
        if len(titles) < 2 or 5 * len(titles) < 3 * len(real):
            return False
        if any(str(real[c]).strip().endswith(":") for c in titles):
            return False
        under = [c for c in titles if nxt.get(c) is not None and not _merged(nxt[c])]
        if 2 * len(under) < len(titles):
            return False
        # 'Part | 101 | Inspector | JB': labels and their values side by
        # side (a form), not a header - every other cell a word, the cells
        # between them not all words
        cols = sorted(real)
        if len(cols) % 2 == 0 and all(_wordish(real[c]) for c in cols[0::2]) and \
                not all(_wordish(real[c]) for c in cols[1::2]):
            return False
        return True

    def _row(self, r: int, cells: Dict[int, Any], nxt: Dict[int, Any]) -> None:
        if self._last_r is not None and not self._next_to(self._last_r, r):
            self._cols, self._span = {}, (0, -1)     # a blank row ends a table
        if self.unit == "line" and len(cells) < 2:
            self._cols, self._span = {}, (0, -1)     # so does a line of prose
        self._last_r = r
        left = dict(cells)
        if self._cols:
            c0, c1 = self._span
            for c in [c for c in left if c0 <= c <= c1]:
                v = left.pop(c)
                if _merged(v):
                    continue
                f = self._cols.get(c)
                if f is None:
                    f = self._cols[c] = self._field(f"(column {col_letter(c)})", "column",
                                                    r, c)
                self.sink.cell(f, r, c, v)
        elif self._is_header(cells, nxt):
            cols = sorted(c for c, v in cells.items() if not _merged(v))
            self._cols = {}
            for c in cols:
                v = cells[c]
                label = _label_text(v) if _wordish(v) else f"(column {col_letter(c)})"
                self._cols[c] = self._field(label, "column", r, c)
            self._span = (cols[0], cols[-1])
            return
        if left:
            self._forms(r, left)

    def _forms(self, r: int, cells: Dict[int, Any]) -> None:
        """Labels and their values outside a table; the rest loose."""
        from council_core.kg_excel import _se
        cols = sorted(cells)
        used: set = set()
        for i, c in enumerate(cols):
            if c in used:
                continue
            v = cells[c]
            if _merged(v):
                used.add(c)
                continue
            s = str(v).strip() if isinstance(v, str) else ""
            nc = cols[i + 1] if i + 1 < len(cols) else None
            right = cells.get(nc) if nc is not None and nc - c <= 2 else None
            if right is not None and _merged(right):
                right = None
            # 'Label:' with its value beside it
            if s.endswith(":") and len(s.split()) <= MAX_LABEL_WORDS and right is not None:
                self.sink.cell(self._field(_label_text(s), "label", r, c), r, nc, right)
                used |= {c, nc}
                continue
            # 'Label: value' in one cell
            if s and (":" in s or "=" in s):
                lab, val = _se()._split_inline(s)
                if val and len(lab.split()) <= MAX_LABEL_WORDS:
                    self.sink.cell(self._field(_label_text(lab), "label", r, c), r, c, val)
                    used.add(c)
                    continue
            # a short word cell with a value right beside it: 'Part | 101'
            if _wordish(v) and right is not None and nc == c + 1 and \
                    len(s.split()) <= MAX_LABEL_WORDS and not _wordish_title_row(cells):
                self.sink.cell(self._field(_label_text(s), "label", r, c), r, nc, right)
                used |= {c, nc}
                continue
        for c in cols:
            if c not in used and not _merged(cells[c]):
                self.sink.cell(self._field(LOOSE, "loose", r, c), r, c, cells[c])


_TABLE_RULE = re.compile(r"^[\s|:+\-=]+$")


def _line_cells(text: str) -> Dict[int, Any]:
    """One line of text as cells: split by '|' (a Markdown table row) or by
    tabs; else the whole line in one cell. A rule line ('|---|---|') and a
    blank line are no cells."""
    s = text.strip()
    if not s or (_TABLE_RULE.match(s) and ("-" in s or "=" in s)):
        return {}
    if s.count("|") >= 2 or (s.count("|") == 1 and " | " in s):
        parts = s.strip("|").split("|")
    elif "\t" in s:
        parts = s.split("\t")
    else:
        return {1: s}
    return {i: p.strip() for i, p in enumerate(parts, start=1) if p.strip()}


def _valueish(v: Any) -> bool:
    """A cell that reads as a value even though it is a word: a code with a
    digit ('L-3'), initials ('AK'), or not text at all."""
    if not isinstance(v, str):
        return True
    s = v.strip()
    return any(ch.isdigit() for ch in s) or (s.isupper() and len(s) <= 4)


def _wordish_title_row(cells: Dict[int, Any]) -> bool:
    """Three or more word cells side by side with no values: a row of titles
    no data followed (the last row of a sheet), not label/value pairs.
    'Lot | L-3 | Operator | AK' is pairs: every other cell reads as a value."""
    words = [c for c, v in cells.items() if _wordish(v)]
    if len(words) < 3 or len(words) != len(cells):
        return False
    cols = sorted(cells)
    if len(cols) % 2 == 0 and all(_valueish(cells[c]) for c in cols[1::2]):
        return False
    return True


# ── writing one document into the store ──────────────────────────────────
def current(db: sqlite3.Connection, did: str, content_hash: str) -> bool:
    row = db.execute("SELECT content_hash FROM contents_docs WHERE document_id=?",
                     (did,)).fetchone()
    return row is not None and row[0] == content_hash


def forget(db: sqlite3.Connection, did: str) -> None:
    """A document's fields and cells go (its values stay until ``finish``
    finds nothing holds them)."""
    fts = _has_fts(db)
    if fts:
        for fid, label, sheet in db.execute(
                "SELECT id, label, sheet FROM fields WHERE document_id=?", (did,)).fetchall():
            db.execute("INSERT INTO fields_fts(fields_fts, rowid, label, sheet)"
                       " VALUES('delete', ?, ?, ?)", (fid, label, sheet))
    db.execute("DELETE FROM cells WHERE field_id IN (SELECT id FROM fields WHERE"
               " document_id=?)", (did,))
    db.execute("DELETE FROM fields WHERE document_id=?", (did,))
    db.execute("DELETE FROM contents_docs WHERE document_id=?", (did,))


def store(db: sqlite3.Connection, did: str, content_hash: str, sink: Sink,
          values: Optional[Dict[str, int]] = None, batch: int = 20_000) -> Dict[str, int]:
    """Replace what the layer holds for one document with ``sink``'s.
    ``values`` caches folded text -> vals id across one rebuild."""
    values = {} if values is None else values
    fts = _has_fts(db)
    forget(db, did)
    ids: Dict[int, int] = {}
    for f in sink.fields:
        if not f.n_cells:
            continue
        cur = db.execute(
            "INSERT INTO fields (document_id, sheet, label, label_norm, kind, loc_sheet,"
            " image, unit, first_row, col, ord, n_cells, n_numbers, n_distinct, num_min,"
            " num_max) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, f.sheet, f.label, label_norm(f.label) if f.kind != "loose" else "",
             f.kind, f.loc_sheet, f.image, f.unit, f.first_row, f.col, f.idx, f.n_cells,
             f.n_numbers, len(f.distinct), f.num_min, f.num_max))
        ids[f.idx] = cur.lastrowid
        if fts:
            db.execute("INSERT INTO fields_fts(rowid, label, sheet) VALUES (?,?,?)",
                       (cur.lastrowid, f.label, f.sheet))
    rows: List[tuple] = []
    n = 0
    for fi, row, col, text, num in sink.cells():
        fid = ids.get(fi)
        if fid is None:
            continue
        vid = None
        if num is None:
            key = fold(text)
            vid = values.get(key)
            if vid is None:
                got = db.execute("SELECT id FROM vals WHERE norm=?", (key,)).fetchone()
                if got is None:
                    vid = db.execute("INSERT INTO vals (norm, text) VALUES (?,?)",
                                     (key, text)).lastrowid
                    if fts:
                        db.execute("INSERT INTO vals_fts(rowid, text) VALUES (?,?)",
                                   (vid, text))
                else:
                    vid = got[0]
                values[key] = vid
        rows.append((fid, row, col, text, num, vid))
        n += 1
        if len(rows) >= batch:
            db.executemany("INSERT INTO cells VALUES (?,?,?,?,?,?)", rows)
            rows = []
    if rows:
        db.executemany("INSERT INTO cells VALUES (?,?,?,?,?,?)", rows)
    db.execute("INSERT OR REPLACE INTO contents_docs VALUES (?,?,?,?,?)",
               (did, content_hash, len(ids), n, time.time()))
    return {"fields": len(ids), "cells": n}


def finish(db: sqlite3.Connection, keep: Iterable[str]) -> None:
    """After a rebuild's reading: documents gone or no longer readable leave
    the layer, every value's count is worked out again, and values nothing
    holds go."""
    keep = set(keep)
    for (did,) in db.execute("SELECT document_id FROM contents_docs").fetchall():
        if did not in keep:
            forget(db, did)
    # fields left by a document read before contents_docs was written
    for (did,) in db.execute("SELECT DISTINCT document_id FROM fields WHERE document_id"
                             " NOT IN (SELECT document_id FROM contents_docs)").fetchall():
        forget(db, did)
    db.execute("UPDATE vals SET n = (SELECT count(*) FROM cells WHERE value_id=vals.id)")
    if _has_fts(db):
        for vid, text in db.execute("SELECT id, text FROM vals WHERE n=0").fetchall():
            db.execute("INSERT INTO vals_fts(vals_fts, rowid, text) VALUES('delete', ?, ?)",
                       (vid, text))
    db.execute("DELETE FROM vals WHERE n=0")


# ── reading it back ──────────────────────────────────────────────────────
def locator(field: Dict[str, Any], row: int, col: int) -> Dict[str, Any]:
    """Where a cell is, in the graph's locator words (knowledge_graph.
    locator_text, kg_excel.preview): a workbook cell, a CSV row and column,
    a picture's row, a text document's page and line, a JSON file's line."""
    unit = field.get("unit") or "cell"
    if unit == "line":
        loc: Dict[str, Any] = {"line": row}
        if str(field.get("loc_sheet") or "").isdigit():
            loc["page"] = int(field["loc_sheet"])
        if field.get("kind") == "column":
            loc["column"] = field["label"]
        return loc
    if unit == "json":
        return {"line": col} if col else {}
    if not field.get("sheet") and not field.get("image"):
        return {"row": row, "column": field["label"]}
    loc = {"sheet": field.get("loc_sheet") or field["sheet"]}
    if field.get("image"):
        loc["image"] = field["image"]
        loc["row"] = row
    else:
        loc["cell"] = f"{col_letter(col)}{row}"
    if field.get("kind") == "column":
        loc["column"] = field["label"]
    return loc


def counts(db: sqlite3.Connection) -> Dict[str, int]:
    one = db.execute("SELECT count(*), coalesce(sum(fields), 0), coalesce(sum(cells), 0)"
                     " FROM contents_docs").fetchone()
    vals = db.execute("SELECT count(*) FROM vals").fetchone()[0]
    return {"files": one[0], "fields": one[1], "cells": one[2], "values": vals}


def files(db: sqlite3.Connection) -> List[Dict[str, Any]]:
    out = []
    for r in db.execute(
            "SELECT c.document_id, d.path, c.fields, c.cells,"
            " (SELECT count(DISTINCT sheet) FROM fields f WHERE f.document_id=c.document_id)"
            " FROM contents_docs c JOIN documents d ON d.id=c.document_id"
            " ORDER BY d.path COLLATE NOCASE"):
        out.append({"document_id": r[0], "path": r[1], "fields": r[2], "cells": r[3],
                    "sheets": r[4]})
    return out


def sheets(db: sqlite3.Connection, did: str) -> List[Dict[str, Any]]:
    return [{"sheet": r[0], "fields": r[1], "cells": r[2]} for r in db.execute(
        "SELECT sheet, count(*), sum(n_cells) FROM fields WHERE document_id=?"
        " GROUP BY sheet ORDER BY min(ord)", (did,))]


_FIELD_COLS = ("id, document_id, sheet, label, label_norm, kind, loc_sheet, image, unit,"
               " first_row, col, n_cells, n_numbers, n_distinct, num_min, num_max")


def _field_dict(r) -> Dict[str, Any]:
    d = dict(zip([c.strip() for c in _FIELD_COLS.split(",")], r))
    d["numeric"] = bool(d["n_cells"]) and d["n_numbers"] >= NUMERIC_SHARE * d["n_cells"]
    return d


def field(db: sqlite3.Connection, fid: int) -> Optional[Dict[str, Any]]:
    r = db.execute(f"SELECT {_FIELD_COLS} FROM fields WHERE id=?", (fid,)).fetchone()
    return _field_dict(r) if r else None


def fields_of(db: sqlite3.Connection, did: str, sheet: Optional[str] = None
              ) -> List[Dict[str, Any]]:
    q = f"SELECT {_FIELD_COLS} FROM fields WHERE document_id=?"
    args: tuple = (did,)
    if sheet is not None:
        q += " AND sheet=?"
        args += (sheet,)
    return [_field_dict(r) for r in db.execute(q + " ORDER BY ord", args)]


def summary(f: Dict[str, Any]) -> str:
    """'412 values, 38.2 to 51.7' / '96 values, 12 different'."""
    n = f["n_cells"]
    words = f"{n:,} value" + ("" if n == 1 else "s")
    if f["numeric"] and f["num_min"] is not None:
        return f"{words}, {_num(f['num_min'])} to {_num(f['num_max'])}"
    k = f["n_distinct"]
    more = "+" if k >= Sink.DISTINCT_CAP else ""
    return f"{words}, {k:,}{more} different" if k != n else words


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def field_values(db: sqlite3.Connection, fid: int, limit: int = DRAWN_VALUES
                 ) -> Dict[str, Any]:
    """What a field holds, for the tree: its text values by how often they
    occur (at most ``limit``), or - numeric - its cells in row order (at
    most ``limit``). ``more`` = how many are not listed."""
    f = field(db, fid)
    if f is None:
        return {"numeric": False, "values": [], "more": 0}
    if f["numeric"]:
        got = [{"text": t, "row": r, "col": c, "value_id": v} for t, r, c, v in db.execute(
            "SELECT text, row, col, value_id FROM cells WHERE field_id=? ORDER BY row, col"
            " LIMIT ?", (fid, limit))]
        return {"numeric": True, "values": got, "more": max(0, f["n_cells"] - len(got))}
    got = [{"text": t, "value_id": v, "count": n, "row": r, "col": c}
           for v, t, n, r, c in db.execute(
               "SELECT value_id, min(text), count(*), min(row), min(col) FROM cells"
               " WHERE field_id=? GROUP BY coalesce(value_id, -rowid)"
               " ORDER BY count(*) DESC, min(row) LIMIT ?", (fid, limit))]
    total = db.execute("SELECT count(DISTINCT coalesce(value_id, -rowid)) FROM cells"
                       " WHERE field_id=?", (fid,)).fetchone()[0]
    return {"numeric": False, "values": got, "more": max(0, total - len(got))}


def value(db: sqlite3.Connection, vid: int) -> Optional[Dict[str, Any]]:
    r = db.execute("SELECT id, text, n FROM vals WHERE id=?", (vid,)).fetchone()
    if r is None:
        return None
    files_n = db.execute("SELECT count(DISTINCT f.document_id) FROM cells c JOIN fields f"
                         " ON f.id=c.field_id WHERE c.value_id=?", (vid,)).fetchone()[0]
    return {"id": r[0], "text": r[1], "count": r[2], "files": files_n}


def places(db: sqlite3.Connection, *, value_id: Optional[int] = None,
           number: Optional[float] = None, field_id: Optional[int] = None,
           row: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """Every cell that holds a value (``value_id``), a number (``number``),
    or one cell (``field_id`` + ``row``), with its file, sheet, label and
    locator."""
    if value_id is not None:
        where, args = "c.value_id=?", (value_id,)
    elif number is not None:
        where, args = "c.num=?", (number,)
    else:
        where, args = "c.field_id=? AND c.row=?", (field_id, row)
    out = []
    for r in db.execute(
            f"SELECT c.field_id, c.row, c.col, c.text, d.path, {', '.join('f.' + x.strip() for x in _FIELD_COLS.split(','))}"
            " FROM cells c JOIN fields f ON f.id=c.field_id JOIN documents d"
            f" ON d.id=f.document_id WHERE {where} ORDER BY d.path, f.ord, c.row LIMIT ?",
            args + (limit,)):
        f = _field_dict(r[5:])
        out.append({"field_id": r[0], "row": r[1], "col": r[2], "text": r[3], "path": r[4],
                    "document_id": f["document_id"], "sheet": f["sheet"], "label": f["label"],
                    "kind": f["kind"], "locator": locator(f, r[1], r[2])})
    return out


def row_cells(db: sqlite3.Connection, field_id: int, row: int) -> List[Dict[str, Any]]:
    """The whole row a cell is in: every field of its sheet with a cell on
    that row, in column order. A label's value on that row is in it too."""
    f = field(db, field_id)
    if f is None:
        return []
    return [{"label": lab, "text": t, "col": c, "field_id": fid} for fid, lab, t, c in db.execute(
        "SELECT f.id, f.label, c.text, c.col FROM fields f JOIN cells c ON c.field_id=f.id"
        " AND c.row=? WHERE f.document_id=? AND f.sheet=? ORDER BY c.col",
        (row, f["document_id"], f["sheet"]))]


def same_fields(db: sqlite3.Connection, fid: int, limit: int = 100) -> List[Dict[str, Any]]:
    """The fields of other files with the same label (case, spacing and
    punctuation aside): 'Hardness (HRC)' in every workbook that has it."""
    f = field(db, fid)
    if f is None or not f["label_norm"]:
        return []
    return [dict(_field_dict(r[1:]), path=r[0]) for r in db.execute(
        f"SELECT d.path, {', '.join('f.' + x.strip() for x in _FIELD_COLS.split(','))}"
        " FROM fields f JOIN documents d ON d.id=f.document_id"
        " WHERE f.label_norm=? AND f.id != ? ORDER BY d.path LIMIT ?",
        (f["label_norm"], fid, limit))]


def _fts_query(text: str) -> str:
    toks = re.findall(r"\w+", text, flags=re.UNICODE)
    return " ".join(f'"{t}"*' for t in toks)


def search(db: sqlite3.Connection, text: str, limit: int = 50) -> Dict[str, List[Dict[str, Any]]]:
    """Files, fields and values whose words start with what was typed (all
    of the words), and - a number typed - the cells holding that number."""
    text = (text or "").strip()
    out: Dict[str, List[Dict[str, Any]]] = {"files": [], "fields": [], "values": [],
                                            "numbers": []}
    if not text:
        return out
    like = f"%{text.casefold()}%"
    for r in db.execute(
            "SELECT c.document_id, d.path, c.cells FROM contents_docs c JOIN documents d"
            " ON d.id=c.document_id WHERE lower(d.path) LIKE ? ORDER BY d.path LIMIT ?",
            (like, limit)):
        out["files"].append({"document_id": r[0], "path": r[1], "cells": r[2]})
    q = _fts_query(text)
    cols = ", ".join("f." + x.strip() for x in _FIELD_COLS.split(","))
    if q and _has_fts(db):
        frows = db.execute(
            f"SELECT d.path, {cols} FROM fields_fts s JOIN fields f ON f.id=s.rowid"
            " JOIN documents d ON d.id=f.document_id WHERE fields_fts MATCH ?"
            " ORDER BY rank LIMIT ?", (q, limit)).fetchall()
        vrows = db.execute(
            "SELECT v.id, v.text, v.n FROM vals_fts s JOIN vals v ON v.id=s.rowid"
            " WHERE vals_fts MATCH ? ORDER BY v.n DESC, rank LIMIT ?", (q, limit)).fetchall()
    else:
        frows = db.execute(
            f"SELECT d.path, {cols} FROM fields f JOIN documents d ON d.id=f.document_id"
            " WHERE lower(f.label) LIKE ? OR lower(f.sheet) LIKE ? LIMIT ?",
            (like, like, limit)).fetchall()
        vrows = db.execute("SELECT id, text, n FROM vals WHERE norm LIKE ? ORDER BY n DESC"
                           " LIMIT ?", (like, limit)).fetchall()
    for r in frows:
        out["fields"].append(dict(_field_dict(r[1:]), path=r[0]))
    for vid, t, n in vrows:
        out["values"].append({"value_id": vid, "text": t, "count": n})
    num = as_number(text, text)
    if num is not None:
        n = db.execute("SELECT count(*) FROM cells WHERE num=?", (num,)).fetchone()[0]
        if n:
            out["numbers"].append({"number": num, "text": text, "count": n})
    return out


# ── what a question names, for the Council's answers (step 3) ────────────
#: Words a question is made of that name no value or header on their own.
_QSTOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are",
          "was", "were", "be", "been", "who", "what", "which", "where", "when", "how",
          "why", "does", "do", "did", "it", "its", "this", "that", "these", "those", "me",
          "us", "show", "list", "all", "any", "about", "tell", "give", "find", "from",
          "by", "at", "as", "there", "have", "has", "had", "many", "much", "value",
          "values", "file", "files", "my", "our", "i", "we", "you", "can", "could",
          "would", "should", "please", "than", "more", "less", "each", "every", "say",
          "said", "says", "know", "known", "get", "got", "see", "notes", "note"}
_QTOKEN = re.compile(r"[^\W_][\w./\-#]*", re.UNICODE)
MAX_QUESTION_WORDS = 6
#: Rows shown under a header a question names when it names no value.
FIRST_ROWS = 6


def _q_tokens(question: str) -> List[str]:
    return [t.rstrip(".,;:?!/-").casefold() for t in _QTOKEN.findall(question or "")
            if t.rstrip(".,;:?!/-")]


def question_rows(db: sqlite3.Connection, question: str, *, max_rows: int = 12,
                  max_fields: int = 8, max_cells: int = 25) -> Dict[str, Any]:
    """The rows of the user's files a question is about, with no model call:
    the values it names exactly (any run of up to six words: 'PN-4471',
    'Nordic Forge'), the numbers it names, the lines that share two or more
    of its words, and the headers or labels it names ('hardness' -> 'Hardness
    (HRC)'). A row scores by how many of those it holds (a value a whole
    column repeats counts for less), and the best ``max_rows`` come back
    whole: {rows: [{path, sheet, row, field_id, locator, cells, matched}],
    fields: [field dicts with 'path'], words: what matched}. Headers named
    but no value: the fields alone (their summaries say what they hold)."""
    import math
    out: Dict[str, Any] = {"rows": [], "fields": [], "words": []}
    toks = _q_tokens(question)
    if not toks or db.execute("SELECT 1 FROM sqlite_master WHERE name='cells'").fetchone() \
            is None:
        return out
    tokset = set(toks)
    sig = [t for t in toks if t not in _QSTOP and len(t) >= 2]
    # 1. headers and labels it names
    fields_hit: Dict[str, List[str]] = {}
    for (norm,) in db.execute("SELECT DISTINCT label_norm FROM fields WHERE label_norm != ''"):
        words = [w for w in norm.split() if w not in _QSTOP]
        if not words:
            continue
        have = [w for w in words if w in tokset]
        if len(have) == len(words) or (words[0] in tokset and 2 * len(have) >= len(words)):
            fields_hit[norm] = have
    # 2. values it names exactly, longest first
    grams: Dict[str, Tuple[int, int]] = {}
    for i in range(len(toks)):
        for j in range(i + 1, min(len(toks), i + MAX_QUESTION_WORDS) + 1):
            g = " ".join(toks[i:j])
            if j - i == 1 and (g in _QSTOP or len(g) < 2):
                continue
            grams.setdefault(g, (i, j))
    hits: List[Tuple[int, int, int, str, int]] = []      # i, j, value id, text, n
    keys = list(grams)
    for k in range(0, len(keys), 400):
        part = keys[k:k + 400]
        for vid, norm, text, n in db.execute(
                f"SELECT id, norm, text, n FROM vals WHERE norm IN ({','.join('?' * len(part))})",
                part):
            i, j = grams[norm]
            hits.append((i, j, vid, text, n))
    hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
    used: set = set()
    weights: Dict[int, float] = {}        # value id -> weight
    for i, j, vid, text, n in hits:
        span = set(range(i, j))
        if span & used:
            continue
        used |= span
        w = 1.0 / math.log(2 + max(n, 1) / 10)
        if j - i == 1 and not re.search(r"[\d\-/#]", text):
            w *= 0.3                     # one plain word ('Notes'), not a code
        weights[vid] = w
        out["words"].append(text)
    # 3. numbers it names
    nums = [as_number(t, t) for t in toks]
    nums = [x for x in nums if x is not None]
    # 4. lines sharing two or more of its words (text documents, notes cells)
    words = sorted({t for t in sig if not any(ch.isdigit() for ch in t) and len(t) >= 3})
    line_hits: Dict[int, float] = {}
    if len(words) >= 2 and _has_fts(db):
        q = " OR ".join(f'"{w}"*' for w in words)
        for vid, text in db.execute(
                "SELECT v.id, v.text FROM vals_fts s JOIN vals v ON v.id=s.rowid"
                " WHERE vals_fts MATCH ? ORDER BY rank LIMIT 40", (q,)):
            have = {w for w in words if re.search(r"\b" + re.escape(w), text.casefold())}
            if len(have) >= 2 and vid not in weights:
                line_hits[vid] = 0.5 * len(have)
    # rows that hold them
    score: Dict[Tuple[int, int], float] = {}
    matched: Dict[Tuple[int, int], List[str]] = {}

    def add(fid: int, row: int, w: float, what: str) -> None:
        key = (fid, row)
        score[key] = score.get(key, 0.0) + w
        matched.setdefault(key, []).append(what)
    for vid, w in list(weights.items()) + list(line_hits.items()):
        for fid, row, text in db.execute(
                "SELECT field_id, row, text FROM cells WHERE value_id=? LIMIT 300", (vid,)):
            add(fid, row, w, text)
    for x in nums:
        for fid, row, text in db.execute(
                "SELECT field_id, row, text FROM cells WHERE num=? LIMIT 300", (x,)):
            add(fid, row, 0.8, text)
    # one row of a sheet, whichever of its cells matched
    rows: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    fmeta: Dict[int, Dict[str, Any]] = {}
    for (fid, row), sc in score.items():
        f = fmeta.get(fid) or fmeta.setdefault(fid, field(db, fid) or {})
        if not f:
            continue
        key = (f["document_id"], f["sheet"], row)
        r = rows.get(key)
        if r is None:
            r = rows[key] = {"document_id": f["document_id"], "sheet": f["sheet"], "row": row,
                             "field_id": fid, "score": 0.0, "matched": []}
        r["score"] += sc
        r["matched"] += matched[(fid, row)]
    ranked = sorted(rows.values(), key=lambda r: (-r["score"], r["row"]))
    paths = {}
    for r in ranked[:max(max_rows * 3, max_rows)]:
        cells = row_cells(db, r["field_id"], r["row"])
        labels = {label_norm(c["label"]) for c in cells}
        if fields_hit and labels & set(fields_hit):
            r["score"] += 1.0             # it has a header the question names
        r["cells"] = cells[:max_cells]
        r["more_cells"] = max(0, len(cells) - max_cells)
        f = fmeta[r["field_id"]]
        col = next((c["col"] for c in cells if c["field_id"] == r["field_id"]), f.get("col") or 1)
        r["locator"] = locator(f, r["row"], col)
        if r["document_id"] not in paths:
            got = db.execute("SELECT path FROM documents WHERE id=?",
                             (r["document_id"],)).fetchone()
            paths[r["document_id"]] = got[0] if got else ""
        r["path"] = paths[r["document_id"]]
    ranked = [r for r in ranked if "cells" in r]
    ranked.sort(key=lambda r: (-r["score"], r["path"], r["row"]))
    out["rows"] = ranked[:max_rows]
    if not out["rows"] and fields_hit:
        # a header named and no value: the first rows under the busiest one,
        # so 'which flaw failed' sees the table, not only its summary
        top = db.execute(
            f"SELECT id FROM fields WHERE label_norm IN ({','.join('?' * len(fields_hit))})"
            " ORDER BY n_cells DESC LIMIT 1", list(fields_hit)).fetchone()
        if top is not None:
            f = field(db, top[0]) or {}
            got = db.execute("SELECT path FROM documents WHERE id=?",
                             (f.get("document_id"),)).fetchone()
            for (row, col) in db.execute(
                    "SELECT row, col FROM cells WHERE field_id=? ORDER BY row LIMIT ?",
                    (top[0], FIRST_ROWS)).fetchall():
                cells = row_cells(db, top[0], row)
                out["rows"].append({
                    "document_id": f["document_id"], "sheet": f["sheet"], "row": row,
                    "field_id": top[0], "score": 0.0, "matched": [], "first": True,
                    "cells": cells[:max_cells], "more_cells": max(0, len(cells) - max_cells),
                    "locator": locator(f, row, col), "path": got[0] if got else ""})
    if fields_hit:
        cols = ", ".join("f." + x.strip() for x in _FIELD_COLS.split(","))
        marks = ",".join("?" * len(fields_hit))
        for r in db.execute(
                f"SELECT d.path, {cols} FROM fields f JOIN documents d ON d.id=f.document_id"
                f" WHERE f.label_norm IN ({marks}) ORDER BY f.n_cells DESC LIMIT ?",
                list(fields_hit) + [max_fields]):
            out["fields"].append(dict(_field_dict(r[1:]), path=r[0]))
    return out


# ── the Ledger's template fields, looked up (step 4) ─────────────────────
# Short forms labels often use, so 'Part No' finds 'Part Number'.
_ABBREV = {"no": "number", "nr": "number", "num": "number", "qty": "quantity",
           "temp": "temperature", "dia": "diameter", "desc": "description",
           "ref": "reference", "rev": "revision", "wt": "weight"}


def _label_words(norm: str) -> List[str]:
    return [_ABBREV.get(w, w) for w in norm.split() if w not in _QSTOP]


def field_lookup(db: sqlite3.Connection, names: Sequence[str], *, max_labels: int = 6,
                 max_files: int = 20) -> List[Dict[str, Any]]:
    """Where each of a template's field names is in the user's files, so a
    Ledger template is matched to its sources in one look ('Hardness is in
    143 of 190 workbooks: as Hardness (HRC) in 120, as HRC in 23'). For each
    name: the labels that match it - the same words first, then labels that
    hold all its words or whose words it holds, then near spellings - each
    with the files it is in, how many values, and a few examples.
    [{name, labels: [{label, exact, files, cells, numeric, num_min, num_max,
    examples, places: [{path, sheet, n_cells}]}]}]"""
    import difflib
    out: List[Dict[str, Any]] = []
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='fields'").fetchone() is None:
        return [{"name": n, "labels": []} for n in names]
    norms = [r[0] for r in db.execute(
        "SELECT DISTINCT label_norm FROM fields WHERE label_norm != ''")]
    for name in names:
        key = label_norm(name)
        words = set(_label_words(key))
        scored: List[Tuple[float, str]] = []
        for n in norms:
            if n == key:
                scored.append((3.0, n))
                continue
            lw = set(_label_words(n))
            if words and lw and (words <= lw or lw <= words):
                scored.append((2.0 + len(words & lw) / max(len(words | lw), 1), n))
                continue
            if words and len(words) == len(lw) and all(
                    any(x.startswith(w) or w.startswith(x) for x in lw) for w in words):
                scored.append((1.9, n))         # 'Part No' / 'Part Number'
                continue
            ratio = difflib.SequenceMatcher(None, key, n).ratio()
            if ratio >= 0.85:
                scored.append((1.0 + ratio, n))
        scored.sort(key=lambda x: (-x[0], x[1]))
        labels = []
        for score, n in scored[:max_labels]:
            rows = db.execute(
                "SELECT f.id, f.label, f.sheet, f.n_cells, f.n_numbers, f.num_min, f.num_max,"
                " d.path FROM fields f JOIN documents d ON d.id=f.document_id"
                " WHERE f.label_norm=? ORDER BY f.n_cells DESC", (n,)).fetchall()
            if not rows:
                continue
            cells = sum(r[3] for r in rows)
            numbers = sum(r[4] for r in rows)
            mins = [r[5] for r in rows if r[5] is not None]
            maxs = [r[6] for r in rows if r[6] is not None]
            ids = [r[0] for r in rows[:50]]
            examples = [t for (t,) in db.execute(
                f"SELECT text FROM cells WHERE field_id IN ({','.join('?' * len(ids))})"
                " GROUP BY text ORDER BY count(*) DESC LIMIT 5", ids)]
            label = max((r[1] for r in rows), key=lambda t: sum(1 for r in rows if r[1] == t))
            labels.append({
                "label": label, "norm": n, "exact": score >= 3.0,
                "files": len({r[7] for r in rows}), "cells": cells,
                "numeric": bool(cells) and numbers >= NUMERIC_SHARE * cells,
                "num_min": min(mins) if mins else None, "num_max": max(maxs) if maxs else None,
                "examples": examples,
                "places": [{"field_id": r[0], "path": r[7], "sheet": r[2], "label": r[1],
                            "n_cells": r[3]} for r in rows[:max_files]]})
        out.append({"name": name, "labels": labels})
    return out


def lookup_summary(lab: Dict[str, Any]) -> str:
    """'in 143 files, 412 values, 38.2 to 51.7' / '..., e.g. Pass, Fail'."""
    s = f"in {_plural_n(lab['files'], 'file')}, {_plural_n(lab['cells'], 'value')}"
    if lab["numeric"] and lab["num_min"] is not None:
        return f"{s}, {_num(lab['num_min'])} to {_num(lab['num_max'])}"
    if lab["examples"]:
        return f"{s}, e.g. " + ", ".join(lab["examples"][:3])
    return s


def _plural_n(n: int, one: str) -> str:
    return f"{n:,} {one}" + ("" if n == 1 else "s")
