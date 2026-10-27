"""Tables as nodes of the knowledge graph (Part A of the proposal agreed with
the user, 2026-10-27: "tables should also be ingested and have connections
made. If need be the tables can be their own node and not have the values be
individual nodes").

Toolkit-free. Read from the Contents layer (council_core/kg_contents.py),
which already holds every column of every sheet, CSV and text table:

  * a table is the columns of one sheet that share a header row (two or
    more); its node is a TABLE entity, one per table region, keyed by its
    document, sheet and header row;
  * a column's values that name an entity the graph knows (a part, person,
    project or organisation, by its name or alias) give one link per entity:
    TABLE LISTS entity, cited at the first row that names it and counted;
  * two tables that list the same parts (people, projects) are linked:
    SHARES_PARTS_WITH ... when they share at least SHARE_MIN of them or half
    of the smaller table's;
  * a workbook whose formulas read another workbook (an external link, or a
    '[file.xlsx]Sheet'!A1 formula) REFERENCES that document.

Every one of these comes from exact values or the file's own links, so they
go in the graph at once (status 'seeded'). Values are never nodes.
"""
from __future__ import annotations

import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote

#: Entity types a table's values may name (a model's CONCEPT 'Pass' would
#: otherwise be listed by every Result column).
LISTED_TYPES = ("PART", "PERSON", "PROJECT", "ORGANIZATION")
#: Two tables share their parts when they share at least this many of them -
#: or half of the smaller table's (a linked master and its build file share
#: their one part).
SHARE_MIN = 3
#: An entity listed by more tables than this says nothing about any two of
#: them (a project every log names); it is left out of the sharing.
SHARE_MAX_TABLES = 50
#: Different values of one column looked at (a 100k-row log of free text is
#: not a list of names).
MAX_DISTINCT = 20000
#: Bytes of sheet XML searched for '[file.xlsx]' formulas, per workbook.
MAX_FORMULA_BYTES = 20 << 20

SHARE_PREDICATE = {"PART": "SHARES_PARTS_WITH", "PERSON": "SHARES_PEOPLE_WITH",
                   "PROJECT": "SHARES_PROJECTS_WITH"}
#: A column of numbers names parts only under a label like these.
_ID_LABEL = re.compile(r"(?i)\b(part|sample|serial|s/?n|p/?n|pn|item|id|no\.?|number|"
                       r"specimen|coupon|unit|asset)\b")
_EXTERNAL_RELS = re.compile(r"^xl/externalLinks/_rels/externalLink\d+\.xml\.rels$")
_TARGET = re.compile(rb'Target="([^"]+)"')
_SHEET_XML = re.compile(r"^xl/worksheets/sheet\d+\.xml$")
_FORMULA_FILE = re.compile(rb"\[([^\]\[<>]{1,200}?\.(?:xlsx|xlsm|xlsb|xls|csv))\]", re.I)


@dataclass
class Column:
    field_id: int
    label: str
    n_cells: int
    n_numbers: int


@dataclass
class Table:
    document_id: str
    path: str
    sheet: str
    loc_sheet: str
    header_row: int
    columns: List[Column] = field(default_factory=list)
    alone: bool = True             # the only table on its sheet

    def key_for(self, path: str) -> str:
        """The node's key: its file's path (the same in any vault built from
        the same files), sheet and header row."""
        return f"{path}|{self.sheet}|{self.header_row}"

    @property
    def rows(self) -> int:
        return max((c.n_cells for c in self.columns), default=0)

    @property
    def name(self) -> str:
        base = PurePosixPath(self.path).name
        where = [base] + ([self.sheet] if self.sheet else [])
        if not self.alone:
            where.append(f"table at row {self.header_row}")
        return " › ".join(where)


def tables(db: sqlite3.Connection, documents: Optional[Iterable[str]] = None) -> List[Table]:
    """Every table of the Contents layer: the column fields of one sheet that
    share a header row, two or more of them. ``documents``: only these."""
    want = set(documents) if documents is not None else None
    found: Dict[Tuple[str, str, int], Table] = {}
    rows = db.execute(
        "SELECT f.id, f.document_id, f.sheet, f.loc_sheet, f.label, f.first_row, f.n_cells,"
        " f.n_numbers, d.path FROM fields f JOIN documents d ON d.id=f.document_id"
        " WHERE f.kind='column' AND f.first_row IS NOT NULL AND d.status='ok'"
        " ORDER BY f.document_id, f.sheet, f.first_row, f.col")
    for r in rows:
        did = r[1]
        if want is not None and did not in want:
            continue
        k = (did, r[2], int(r[5]))
        t = found.get(k)
        if t is None:
            t = found[k] = Table(did, r[8], r[2], r[3] or r[2], int(r[5]))
        t.columns.append(Column(int(r[0]), r[4], int(r[6]), int(r[7])))
    out = [t for t in found.values() if len(t.columns) >= 2]
    per_sheet: Dict[Tuple[str, str], int] = {}
    for t in out:
        per_sheet[(t.document_id, t.sheet)] = per_sheet.get((t.document_id, t.sheet), 0) + 1
    for t in out:
        t.alone = per_sheet[(t.document_id, t.sheet)] == 1
    return out


def column_values(db: sqlite3.Connection, col: Column) -> List[Tuple[str, int, int]]:
    """(text, first row, rows) of each different value of a column that may
    name something: text, or numbers under an ID-like label. [] for a column
    of measurements or one too varied to be a list."""
    numeric = col.n_cells and col.n_numbers >= 0.8 * col.n_cells
    if numeric and not _ID_LABEL.search(col.label):
        return []
    got = db.execute("SELECT text, MIN(row), COUNT(*) FROM cells WHERE field_id=?"
                     " GROUP BY text LIMIT ?", (col.field_id, MAX_DISTINCT + 1)).fetchall()
    if len(got) > MAX_DISTINCT:
        return []
    return [(str(t), int(r), int(n)) for t, r, n in got if str(t).strip()]


def external_references(path: Path) -> List[str]:
    """The file names a workbook's formulas read: its external links' targets,
    and any '[name.xlsx]' written into a formula. [] for a file that is not
    an .xlsx-family zip."""
    names: List[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            members = z.namelist()
            for m in members:
                if _EXTERNAL_RELS.match(m):
                    for t in _TARGET.findall(z.read(m)):
                        names.append(t.decode("utf-8", "replace"))
            budget = MAX_FORMULA_BYTES
            for m in members:
                if not _SHEET_XML.match(m) or budget <= 0:
                    continue
                with z.open(m) as fh:
                    tail = b""
                    while budget > 0:
                        chunk = fh.read(min(1 << 20, budget))
                        if not chunk:
                            break
                        budget -= len(chunk)
                        names += [x.decode("utf-8", "replace")
                                  for x in _FORMULA_FILE.findall(tail + chunk)]
                        tail = chunk[-256:]
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError):
        return []
    out = []
    for n in names:
        base = re.split(r"[\\/]", unquote(n).replace("file:///", ""))[-1].strip()
        if base and base not in out:
            out.append(base)
    return out


def resolve_reference(name: str, from_path: str, paths: Sequence[str]) -> Optional[str]:
    """The document path a referenced file name is: the one in the same
    folder, else the only one of that name. None when it is not in the graph
    or the name is not unique."""
    want = name.casefold()
    same = [p for p in paths if PurePosixPath(p).name.casefold() == want]
    if not same:
        return None
    folder = str(PurePosixPath(from_path).parent)
    near = [p for p in same if str(PurePosixPath(p).parent) == folder and p != from_path]
    if len(near) == 1:
        return near[0]
    others = [p for p in same if p != from_path]
    return others[0] if len(others) == 1 else None


def shared_pairs(listed: Dict[str, Dict[str, Set[str]]]
                 ) -> List[Tuple[str, str, str, List[str]]]:
    """(table a, table b, entity type, shared entity ids) for every two
    tables that share enough of one type: listed is table -> type -> ids."""
    by_entity: Dict[Tuple[str, str], List[str]] = {}
    for t, types in listed.items():
        for etype, ids in types.items():
            if etype not in SHARE_PREDICATE:
                continue
            for e in ids:
                by_entity.setdefault((etype, e), []).append(t)
    pairs: Dict[Tuple[str, str, str], List[str]] = {}
    for (etype, e), ts in by_entity.items():
        if len(ts) < 2 or len(ts) > SHARE_MAX_TABLES:
            continue
        ts = sorted(ts)
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                pairs.setdefault((a, b, etype), []).append(e)
    out = []
    for (a, b, etype), shared in pairs.items():
        smaller = min(len(listed[a].get(etype, ())), len(listed[b].get(etype, ())))
        if len(shared) >= SHARE_MIN or len(shared) * 2 >= smaller:
            out.append((a, b, etype, sorted(shared)))
    return out
