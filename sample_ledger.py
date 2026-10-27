"""
sample_ledger.py — read a data-entry template workbook, find every field it
tracks, and keep one parent CSV of samples built from those fields.

Step one of the spreadsheet-ingestion pipeline:

    read_template(xlsx)              -> TemplateSchema: every tracked field,
                                        the cell(s) it lives in, its unit,
                                        dropdown and pre-filled default
    ParentCSV.create(path, schema)   -> an empty parent CSV (+ its schema
                                        JSON beside it)
    ParentCSV(path).add_samples(rows)-> rows merged in by key, whole file kept
                                        in numerical sample order
    read_filled(schema, xlsx)        -> the rows held in a copy of the template
                                        that someone filled in
    fill_template(schema, rows, out) -> a copy of the template with one
                                        sample's rows written into it

Later steps (reading the varied source workbooks, and the data held in their
images) produce rows for add_samples; nothing here depends on how a row was
found.

HOW A TEMPLATE IS READ
----------------------
A SECTION starts at a heading in large type ("Part Attributes", "NDE
Inspection" ...). Each cell belongs to the nearest heading above it in its
column band, so a second block of headings further right (a "Tables for
Presentation" area) is its own section. A section that is mostly formulas
is DERIVED — calculated from the entry cells — and is skipped.

Inside a section, four layouts are recognised:

  * GRID — a header row whose label ("Flaw #") is followed by 3+ item codes
    (1A, 2A ... 10A), then one row per measurement with a label in the same
    column. Several grids with different codes (1A.., 1B.., 1C..) and the
    same rows are one set of fields. A label's block letter ("Laser Team FOD
    A (um)" in the A grid) is dropped so the A/B/C rows merge, and a label
    misspelt in one block ("Repsonse") joins the close match. Header cells
    after the codes ("Inspector Initials", "Date") are per-row extras that
    every item in that grid shares.
  * TABLE — 3+ adjacent labels with empty rows under them. The table holding
    the sample ID is the sample's own row; any other table repeats once per
    empty row under it before the next content (Concealment 1, 2 ... 6).
  * FORM — a short label with an empty cell to its right (or below, after a
    "Label:").
  * NOTE — a heading with nothing under it is a free-text field.

THE PARENT CSV
--------------
  * One row per sample, or, when the template has grids, one row per sample
    per item (Part Number + Flaw #). Part-level values repeat on each row.
  * Key columns first; sorted by natural number order of the sample (S-2
    before S-10), then items in template order (1A..10A, 1B.., 1C..).
  * Adding a row that already exists fills its blank cells. A cell already
    holding a different value is NOT overwritten unless overwrite=True; the
    clash is returned so it can be shown, never dropped silently.
  * `source_file` and `updated_at` say where each row last came from.
  * A field that appears in a newer template is added as a new column. No
    column is ever removed.
  * Written to a temp file and swapped in, so a crash mid-write cannot leave
    half a CSV. utf-8-sig so Excel opens it with the right characters.
"""
from __future__ import annotations

import csv
import difflib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

SOURCE_COL = "source_file"
UPDATED_COL = "updated_at"
META_COLS = (SOURCE_COL, UPDATED_COL)
#: the template's own name for a grid row's initials cell
INITIALS_SUFFIX = " — Inspector Initials"
#: one inspector's own reading of a field, kept beside the template's one value
INSPECTOR_COL = re.compile(r"^(?P<field>.+) \[(?P<who>[^\[\]]{1,32})\]$")


def inspector_column(field_name: str, who: str) -> str:
    return f"{field_name} [{who}]"

MAX_LABEL_LEN = 60          # longer text is an instruction, not a field
MIN_RUN = 3                 # adjacent header cells / item codes that make a table
HEADING_PT = 3              # a heading's font is this much bigger than body text
MAX_REPEATS = 20
_ID_PATTERNS = (
    r"\bsample\s*(id|#|no\.?|number|name)?\b",
    r"\bspecimen\s*(id|#|no\.?|number)?\b",
    r"\b(build|part|coupon|lot|serial)\s*(id|#|no\.?|number)\b",
    r"^\s*id\s*$",
)
_UNIT_RE = re.compile(r"^(.*?)\s*[\(\[]\s*([^\)\]]{1,20})\s*[\)\]]\s*$")
_ITEM_RE = re.compile(r"^[A-Za-z]{0,3}-?\d{1,4}[A-Za-z]{0,3}$")


class LedgerError(Exception):
    """A problem the user can act on: the message says what to do."""


# ── schema ──────────────────────────────────────────────────────────────
@dataclass
class TemplateField:
    name: str                   # parent-CSV header
    label: str                  # the label exactly as written in the template
    sheet: str
    section: str
    layout: str                 # table | form | repeat | note | grid | grid-extra
    cells: Dict[str, str]       # item -> cell; "" for a part-level field
    unit: str = ""
    choices: List[str] = field(default_factory=list)
    defaults: Dict[str, str] = field(default_factory=dict)   # item -> pre-filled value
    key: str = ""               # normalized label, for matching source data

    @property
    def per_item(self) -> bool:
        return self.layout in ("grid", "grid-extra")


@dataclass
class TemplateSchema:
    template_path: str
    fields: List[TemplateField]
    id_field: str
    item_field: str = ""        # "Flaw #" when the template has grids
    items: List[str] = field(default_factory=list)

    @property
    def key_columns(self) -> List[str]:
        return [self.id_field] + ([self.item_field] if self.item_field else [])

    @property
    def columns(self) -> List[str]:
        rest = [f.name for f in self.fields if f.name != self.id_field]
        return self.key_columns + rest

    def field(self, name: str) -> TemplateField:
        return next(f for f in self.fields if f.name == name)

    def takes_inspectors(self, name: str) -> bool:
        """A per-flaw reading the template records ONE of, with an Inspector
        Initials cell beside it (the EC responses, FPI, NDE Team FOD): when
        several people log their own, the parent CSV keeps each one in its own
        column, "<field> [initials]". Not the Visual Inspector 1..4 slots,
        which are per-person already."""
        names = {f.name for f in self.fields}
        return (name in names and f"{name}{INITIALS_SUFFIX}" in names
                and self.field(name).layout == "grid"
                and not re.search(r"\bInspector \d+$", name))

    def inspector_base(self, column: str) -> Optional[str]:
        """'X [JB]' -> 'X' when X takes inspectors, else None."""
        m = INSPECTOR_COL.match(column)
        return m.group("field") if m and self.takes_inspectors(m.group("field")) else None

    def save(self, path) -> None:
        data = asdict(self)
        _atomic_write_text(Path(path), json.dumps(data, indent=2))

    @classmethod
    def load(cls, path) -> "TemplateSchema":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["fields"] = [TemplateField(**f) for f in data["fields"]]
        return cls(**data)

    def describe(self) -> str:
        part = [f for f in self.fields if not f.per_item]
        item = [f for f in self.fields if f.per_item]
        head = (f"{Path(self.template_path).name}: {len(part)} part-level fields, "
                f"sample ID {self.id_field!r}")
        if self.item_field:
            head += (f"; {len(item)} fields per {self.item_field} x "
                     f"{len(self.items)} items ({self.items[0]} .. {self.items[-1]})")
        lines = [head]
        section = None
        for f in self.fields:
            if f.section != section:
                section = f.section
                lines.append(f"\n[{section}]")
            first = next(iter(f.cells.values()))
            extra = f"  one of {f.choices}" if f.choices else ""
            d = sorted(set(f.defaults.values()))
            if d:
                extra += f"  pre-filled {d[0]!r}" if len(d) == 1 else f"  pre-filled {d}"
            lines.append(f"  {first:<6} {f.layout:<10} {f.name}{extra}")
        return "\n".join(lines)


def normalize_label(text: str) -> str:
    """'Laser Power (W):' -> 'laser power'. Used to match source labels.

    Runs of whitespace are collapsed BEFORE the unit pattern: its lazy
    '(.*?)' then '\\s*' backtracked over every space, so a cell padded to
    Excel's 32,767 characters held the GIL for 2.5-3.8 s per call and froze
    the app for 10-14 s (end-to-end test, 2026-10-19). The result is the
    same: the last step folds every non-alphanumeric run anyway. The marks
    at the end are stripped, not matched: '[:#*]+$' rescanned a cell of
    32,000 ':' or '*' from each of them for 2 s (review, 2026-10-20)."""
    text = " ".join(str(text).split())
    # the unit is the last 26 characters at most (one space each side of its
    # brackets): read there, not tried from every bracket of a long cell
    head, tail = text[:-40], text[-40:]
    text = (head + _UNIT_RE.sub(r"\1", tail)) or text
    text = text.strip().rstrip(":#*")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _clean(label: str) -> str:
    return re.sub(r"\s*[:*]+\s*$", "", " ".join(str(label).split()))


def _split_unit(label: str) -> Tuple[str, str]:
    label = " ".join(str(label).split())          # see normalize_label
    m = _UNIT_RE.match(label)
    return (m.group(1).strip(), m.group(2).strip()) if m and m.group(1) else (label, "")


def _looks_like_id(label: str) -> bool:
    low = label.lower()
    return any(re.search(p, low) for p in _ID_PATTERNS)


#: What a workbook cell cannot hold (openpyxl's ILLEGAL_CHARACTERS_RE):
#: control characters other than tab and line breaks.
_ILLEGAL_XL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+")


def xl_text(v):
    """`v` with the control characters a workbook cell cannot hold put as
    a space (and trimmed): text pasted from another system ('Remark' +
    CHAR(1)) read through Excel made openpyxl raise IllegalCharacterError,
    and the whole extract wrote no review (review, 2026-10-20). Anything
    that is not text is returned as it is."""
    if not isinstance(v, str) or not _ILLEGAL_XL.search(v):
        return v
    return _ILLEGAL_XL.sub(" ", v).strip()


def _text(v) -> str:
    """A cell value as CSV text: 12.0 -> '12', dates ISO, None -> ''."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="seconds").replace(" 00:00:00", "")
    if isinstance(v, date):
        return v.isoformat()
    return str(v).strip()


# ── reading the template ────────────────────────────────────────────────
def _open(path, data_only: bool):
    try:
        import openpyxl
    except ImportError as e:            # pragma: no cover - in requirements.txt
        raise LedgerError("openpyxl is needed to read .xlsx: pip install openpyxl") from e
    path = Path(path)
    if not path.exists():
        raise LedgerError(f"workbook not found: {path}")
    try:
        return openpyxl.load_workbook(path, data_only=data_only)
    except Exception as e:
        raise LedgerError(f"could not open {path.name} as a workbook "
                          f"(.xlsx/.xlsm; save .xls files as .xlsx first): {e}") from e


def read_template(path, id_field: Optional[str] = None) -> TemplateSchema:
    path = Path(path)
    wb = _open(path, data_only=False)    # formulas visible: they mark derived areas
    raw: List[TemplateField] = []
    items: List[str] = []
    item_labels: Counter = Counter()
    for ws in wb.worksheets:
        _SheetReader(ws, raw, items, item_labels).read()
    wb.close()
    if not raw:
        raise LedgerError(f"no field labels found in {path.name}: the template "
                          "needs header cells or 'Label:' cells with space for a value")

    id_name = _pick_id(raw, id_field, path.name)
    fields = _dedupe(raw, id_name)
    item_field = item_labels.most_common(1)[0][0] if item_labels else ""
    return TemplateSchema(str(path), fields, id_name, item_field, items)


def _dedupe(raw: List[TemplateField], id_name: str) -> List[TemplateField]:
    """Unique CSV headers: the same name twice keeps the first; a part-level
    field whose plain name collides gets its section in front."""
    out: List[TemplateField] = []
    seen = set()
    for f in raw:
        if f.name == id_name and id_name.lower() in seen:
            continue
        if f.name.lower() in seen and f.section:
            f.name = f"{f.section}: {f.name}"
        if f.name.lower() in seen:
            continue
        seen.add(f.name.lower())
        out.append(f)
    return out


def _pick_id(fields: List[TemplateField], wanted: Optional[str], fname: str) -> str:
    part = [f for f in fields if not f.per_item]
    if wanted:
        for f in part:
            if wanted.lower() in (f.name.lower(), f.label.lower(), f.key):
                return f.name
        raise LedgerError(f"id_field {wanted!r} is not a field in {fname}; "
                          f"fields are: {[f.name for f in part]}")
    for f in part:
        if _looks_like_id(f.label):
            return f.name
    raise LedgerError(f"no sample-ID field found in {fname}. Pass id_field= with "
                      "the label that numbers the samples (e.g. 'Sample ID').")


class _SheetReader:
    def __init__(self, ws, out, items, item_labels):
        from openpyxl.utils import get_column_letter
        self.ws, self.out, self.items, self.item_labels = ws, out, items, item_labels
        self.col = get_column_letter
        self.vals: Dict[Tuple[int, int], object] = {}
        self.text: Dict[Tuple[int, int], str] = {}
        sizes: Counter = Counter()
        # the cells the sheet has, not every position in its dimension: one
        # formatted cell at the sheet's far corner made millions (review,
        # 2026-10-19)
        store = getattr(ws, "_cells", None)
        existing = ([cell for _rc, cell in sorted(store.items())] if isinstance(store, dict)
                    else [c for row in ws.iter_rows() for c in row])
        for c in existing:
            if c.value in (None, ""):
                continue
            self.vals[(c.row, c.column)] = c.value
            if isinstance(c.value, str) and not c.value.startswith("="):
                self.text[(c.row, c.column)] = c.value.strip()
                sizes[c.font.sz or 11] += 1
        body = sizes.most_common(1)[0][0] if sizes else 11
        self.headings = sorted(
            (rc for rc, v in self.text.items()
             if (ws.cell(*rc).font.sz or 11) >= body + HEADING_PT),
            key=lambda rc: (rc[1], rc[0]))
        self.merged_into, self.last_col, self.wide = {}, {}, set()
        for rng in ws.merged_cells.ranges:
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    self.merged_into[(r, c)] = (rng.min_row, rng.min_col)
            self.last_col[(rng.min_row, rng.min_col)] = rng.max_col
            if rng.max_col - rng.min_col >= 3 and rng.min_row == rng.max_row:
                self.wide.add((rng.min_row, rng.min_col))
        self.choices = _dropdowns(ws)
        self.used = set(self.headings)

    # cells
    def empty(self, r, c) -> bool:
        return self.merged_into.get((r, c), (r, c)) not in self.vals \
            and (r, c) not in self.vals

    def ref(self, r, c) -> str:
        return f"{self.col(c)}{r}"

    def section_of(self, r, c) -> Optional[Tuple[int, int]]:
        bands = sorted({hc for _, hc in self.headings})
        band = max((b for b in bands if b <= c), default=None)
        if band is None:
            return None
        above = [h for h in self.headings if h[1] == band and h[0] <= r]
        return max(above) if above else None

    def name_of(self, sec) -> str:
        return _clean(self.text[sec]) if sec else ""

    def read(self) -> None:
        by_sec: Dict[object, List[Tuple[int, int]]] = {}
        for rc in self.vals:
            by_sec.setdefault(self.section_of(*rc), []).append(rc)
        derived = set()
        for sec, cells in by_sec.items():
            formulas = sum(1 for rc in cells if str(self.vals[rc]).startswith("="))
            if sec is not None and formulas > 0.3 * len(cells):
                derived.add(sec)
        order = sorted(by_sec, key=lambda s: (s[1], s[0]) if s else (0, 0))
        for sec in order:
            if sec in derived:
                continue
            cells = set(by_sec[sec])
            before = len(self.out)
            self.grids(sec, cells)
            self.tables(sec, cells)
            self.forms(sec, cells)
            if sec is not None and len(self.out) == before:
                self.note(sec)

    # GRID ───────────────────────────────────────────────────────────────
    def grids(self, sec, cells) -> None:
        sname = self.name_of(sec)
        merged: Dict[str, TemplateField] = {}       # field key within section
        spellings: Dict[str, Counter] = {}
        row_keys: List[str] = []                    # the section's first grid, in order
        for (r, c) in sorted(cells):
            if (r, c) not in self.text or (r, c) in self.used:
                continue
            codes = []
            cc = c + 1
            while (r, cc) in self.text and _ITEM_RE.match(self.text[(r, cc)]):
                codes.append((cc, self.text[(r, cc)]))
                cc += 1
            if len(codes) < MIN_RUN:
                continue
            extras = []
            while (r, cc) in self.text and len(self.text[(r, cc)]) <= MAX_LABEL_LEN:
                extras.append((cc, _clean(self.text[(r, cc)])))
                cc += 1
            self.item_labels[_clean(self.text[(r, c)])] += 1
            for _, code in codes:
                if code not in self.items:
                    self.items.append(code)
            for rc in [(r, c)] + [(r, x) for x, _ in codes + extras]:
                self.used.add(rc)
            block = _common_suffix([code for _, code in codes])
            first_grid = not row_keys
            rr = r + 1
            while (rr, c) in self.text and not self._is_grid_header(rr, c):
                label = self.text[(rr, c)]
                self.used.add((rr, c))
                clean = _clean(label)
                if block:
                    clean = re.sub(rf"\s+{re.escape(block)}(?=\s|\(|$)", "", clean,
                                   flags=re.I).strip()
                k = normalize_label(clean)
                pos = rr - r - 1
                if k not in merged and pos < len(row_keys):
                    # same row of an earlier grid, spelt slightly differently
                    # ("Repsonse"): position decides, spelling only confirms
                    if difflib.SequenceMatcher(None, k, row_keys[pos]).ratio() >= 0.85:
                        k = row_keys[pos]
                if first_grid:
                    row_keys.append(k)
                spellings.setdefault(k, Counter())[clean] += 1
                f = merged.get(k)
                if f is None:
                    name, unit = _split_unit(clean)
                    f = merged[k] = TemplateField(
                        name=clean, label=label, sheet=self.ws.title, section=sname,
                        layout="grid", cells={}, unit=unit, key=k)
                    self.out.append(f)
                for x, code in codes:
                    f.cells[code] = self.ref(rr, x)
                    self.used.add((rr, x))
                    v = self.vals.get((rr, x))
                    if v not in (None, "") and not str(v).startswith("="):
                        f.defaults[code] = _text(v)
                    ch = self.choices.get(self.ref(rr, x))
                    if ch:
                        f.choices = ch
                for x, extra in extras:
                    ek = f"{k}|{normalize_label(extra)}"
                    ef = merged.get(ek)
                    if ef is None:
                        ef = merged[ek] = TemplateField(
                            name=f"{clean} — {extra}", label=extra, sheet=self.ws.title,
                            section=sname, layout="grid-extra", cells={}, key=ek)
                        self.out.append(ef)
                    for _, code in codes:
                        ef.cells[code] = self.ref(rr, x)
                    self.used.add((rr, x))
                rr += 1
        # most common spelling wins; section in front so grids never collide
        for k, f in merged.items():
            base = k.split("|")[0]
            best = spellings[base].most_common(1)[0][0]
            if f.layout == "grid":
                f.name = best
            else:
                f.name = f"{best} — {f.label}"
            f.name = f"{sname}: {f.name}" if sname else f.name

    def _is_grid_header(self, r, c) -> bool:
        return sum(1 for x in range(c + 1, c + 1 + MIN_RUN)
                   if (r, x) in self.text and _ITEM_RE.match(self.text[(r, x)])) >= MIN_RUN

    # TABLE ──────────────────────────────────────────────────────────────
    def tables(self, sec, cells) -> None:
        sname = self.name_of(sec)
        rows: Dict[int, List[int]] = {}
        for (r, c) in cells:
            if (r, c) in self.text and (r, c) not in self.used and (r, c) not in self.wide \
                    and len(self.text[(r, c)]) <= MAX_LABEL_LEN:
                rows.setdefault(r, []).append(c)
        for r, cols in sorted(rows.items()):
            cols.sort()
            run: List[int] = []
            for c in cols + [None]:
                if run and (c is None or c != run[-1] + 1):
                    self._table(sname, r, run)
                    run = []
                if c is not None:
                    run.append(c)

    def _table(self, sname, r, run) -> None:
        if len(run) < MIN_RUN or sum(self.empty(r + 1, c) for c in run) < len(run) * 0.6:
            return
        labels = [_clean(self.text[(r, c)]) for c in run]
        has_id = any(_looks_like_id(l) for l in labels)
        n = 1
        if not has_id:      # a repeating table: one entry per empty row under it
            n = 0
            while n < MAX_REPEATS and all(self.empty(r + 1 + n, c)
                                          for c in range(run[0], run[-1] + 1)) \
                    and self.section_of(r + 1 + n, run[0]) == self.section_of(r, run[0]) \
                    and (r + 1 + n, run[0]) not in self.headings:
                n += 1
            n = max(n, 1)
        for c, label in zip(run, labels):
            self.used.add((r, c))
            name, unit = _split_unit(label)
            for i in range(n):
                vr = r + 1 + i
                fname = label if has_id or not sname else (
                    f"{sname} {i + 1}: {label}" if n > 1 else f"{sname}: {label}")
                f = TemplateField(
                    name=fname, label=self.text[(r, c)], sheet=self.ws.title,
                    section=sname, layout="table" if has_id else "repeat",
                    cells={"": self.ref(vr, c)}, unit=unit,
                    choices=self.choices.get(self.ref(vr, c), []),
                    key=normalize_label(name))
                v = self.vals.get((vr, c))
                if v not in (None, "") and not str(v).startswith("="):
                    f.defaults[""] = _text(v)
                self.out.append(f)
                self.used.add((vr, c))

    # FORM / NOTE ────────────────────────────────────────────────────────
    def forms(self, sec, cells) -> None:
        sname = self.name_of(sec)
        for (r, c) in sorted(cells):
            v = self.text.get((r, c))
            if v is None or (r, c) in self.used or (r, c) in self.wide \
                    or len(v) > MAX_LABEL_LEN \
                    or self.merged_into.get((r, c), (r, c)) != (r, c):
                continue
            rc = self.last_col.get((r, c), c) + 1
            if self.empty(r, rc):
                vr, vc = r, rc
            elif v.rstrip().endswith(":") and self.empty(r + 1, c):
                vr, vc = r + 1, c
            else:
                continue
            clean = _clean(v)
            name, unit = _split_unit(clean)
            self.used.add((r, c))
            self.out.append(TemplateField(
                name=clean, label=v, sheet=self.ws.title, section=sname, layout="form",
                cells={"": self.ref(vr, vc)}, unit=unit,
                choices=self.choices.get(self.ref(vr, vc), []), key=normalize_label(name)))

    def note(self, sec) -> None:
        label = self.text[sec]
        self.out.append(TemplateField(
            name=_clean(label), label=label, sheet=self.ws.title,
            section=_clean(label), layout="note", cells={"": self.ref(sec[0] + 1, sec[1])},
            key=normalize_label(label)))


def _common_suffix(codes: List[str]) -> str:
    """['1A', '2A', '10A'] -> 'A'; ['1', '2'] -> ''."""
    tails = {re.sub(r"^.*\d", "", c) for c in codes}
    return tails.pop() if len(tails) == 1 else ""


def _dropdowns(ws) -> Dict[str, List[str]]:
    """value cell -> allowed values, for in-cell list validations."""
    out: Dict[str, List[str]] = {}
    try:
        from openpyxl.utils.cell import get_column_letter, range_boundaries
        for dv in ws.data_validations.dataValidation:
            if dv.type != "list" or not dv.formula1:
                continue
            f = dv.formula1.strip()
            if not f.startswith('"'):
                continue                # a range reference; values live elsewhere
            vals = [v.strip() for v in f.strip('"').split(",") if v.strip()]
            for rng in str(dv.sqref).split():
                c1, r1, c2, r2 = range_boundaries(rng)
                for r in range(r1, min(r2, r1 + MAX_REPEATS) + 1):
                    for c in range(c1, c2 + 1):
                        out[f"{get_column_letter(c)}{r}"] = vals
    except Exception:
        pass                            # dropdowns are a nicety, never fatal
    return out


# ── a filled-in template <-> rows ───────────────────────────────────────
def read_filled(schema: TemplateSchema, path) -> List[Dict[str, str]]:
    """Rows from a workbook laid out exactly like the template. Values equal
    to the template's own pre-filled defaults count as entered."""
    wb = _open(path, data_only=True)
    try:
        def get(f: TemplateField, item: str) -> str:
            ws = wb[f.sheet] if f.sheet in wb.sheetnames else wb.worksheets[0]
            return _text(ws[f.cells[item]].value)
        part = {f.name: get(f, "") for f in schema.fields if not f.per_item}
        if not schema.item_field:
            return [part]
        rows = []
        for item in schema.items:
            row = dict(part)
            row[schema.item_field] = item
            for f in schema.fields:
                if f.per_item and item in f.cells:
                    row[f.name] = get(f, item)
            rows.append(row)
        return rows
    finally:
        wb.close()


def fill_template(schema: TemplateSchema, rows: List[Dict[str, str]], out_path,
                  template_path=None) -> List[str]:
    """Write ONE sample's rows into a copy of the template. Returns warnings
    (a per-grid extra that differs between the items sharing its cell)."""
    import openpyxl
    src = Path(template_path or schema.template_path)
    out_path = Path(out_path)
    if out_path.resolve() == src.resolve():
        raise LedgerError("refusing to write over the template itself")
    ids = {r.get(schema.id_field, "") for r in rows}
    if len(ids) != 1:
        raise LedgerError(f"fill_template takes one sample's rows, got {sorted(ids)}")
    shutil.copyfile(src, out_path)
    wb = openpyxl.load_workbook(out_path)
    warnings: List[str] = []
    written: Dict[Tuple[str, str], str] = {}

    def put(f: TemplateField, item: str, value) -> None:
        value = xl_text(value)          # a parent CSV may hold CHAR(1)
        if value in (None, ""):
            return
        ws = wb[f.sheet] if f.sheet in wb.sheetnames else wb.worksheets[0]
        cell = f.cells[item]
        prev = written.get((f.sheet, cell))
        if prev is not None and prev != str(value):
            warnings.append(f"{f.name} at {cell}: {prev!r} kept, {value!r} differs")
            return
        written[(f.sheet, cell)] = str(value)
        ws[cell].value = _typed(value)

    for f in schema.fields:
        if not f.per_item:
            put(f, "", rows[0].get(f.name))
    for row in rows:
        item = row.get(schema.item_field, "")
        for f in schema.fields:
            if f.per_item and item in f.cells:
                put(f, item, row.get(f.name))
    # so a recursive source scan knows this is the Ledger's own output, not
    # an inspection workbook (review, 2026-10-19)
    # No last-saved-by: Excel writes one when a person saves the copy, and a
    # copy someone kept typing into is read again (is_filled_template).
    try:
        wb.properties.keywords = FILLED_MARK
        wb.properties.lastModifiedBy = None
    except Exception:
        pass
    wb.save(out_path)
    return warnings


#: The keyword a filled template carries in its document properties.
FILLED_MARK = "council-ledger-filled-template"


def _filled_state(p) -> str:
    """'' (not filled by the Ledger), 'filled', or 'edited': filled, then
    saved by someone else - Excel writes who saved it last (lastModifiedBy),
    which fill_template leaves out."""
    import zipfile
    p = Path(p)
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        return ""
    try:
        with zipfile.ZipFile(p) as z:
            core = z.read("docProps/core.xml")
    except Exception:
        return ""
    if FILLED_MARK.encode() not in core:
        return ""
    m = re.search(rb"<cp:lastModifiedBy>([^<]*)</cp:lastModifiedBy>", core)
    return "edited" if m and m.group(1).strip() else "filled"


def is_filled_template(p) -> bool:
    """A template the Ledger filled (fill_template) and nobody saved since,
    told from its document properties without opening the workbook. One a
    person kept typing into is a source again (synthetic test, 2026-10-27:
    such copies were left out of a folder scan)."""
    return _filled_state(p) == "filled"


def is_edited_filled_template(p) -> bool:
    """A template the Ledger filled that someone saved since (read again)."""
    return _filled_state(p) == "edited"


def _typed(v):
    """'285' -> 285 so Excel formulas over the cell still add up."""
    s = str(v).strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d*\.\d+(e-?\d+)?", s, re.I):
        return float(s)
    return s


# ── natural sort ────────────────────────────────────────────────────────
def sample_sort_key(sample_id) -> tuple:
    """'S-2' < 'S-10'; '7' < '12'; blanks last. Case-insensitive."""
    s = str(sample_id).strip().lower()
    if not s:
        return ((2, 0.0, ""),)
    key = []
    for p in re.split(r"(\d+(?:\.\d+)?)", s):
        if p:
            key.append((0, float(p), "") if p[0].isdigit() else (1, 0.0, p))
    return tuple(key)


# ── the parent CSV ──────────────────────────────────────────────────────
@dataclass
class AddResult:
    added: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    # (row key, column, value in CSV, value offered) — not applied
    conflicts: List[Tuple[str, str, str, str]] = field(default_factory=list)
    unknown_columns: List[str] = field(default_factory=list)
    skipped_no_key: int = 0

    def describe(self) -> str:
        s = (f"{len(self.added)} rows added, {len(self.updated)} updated, "
             f"{len(self.unchanged)} unchanged")
        if self.conflicts:
            s += f", {len(self.conflicts)} conflicting values kept as they were"
        if self.skipped_no_key:
            s += f", {self.skipped_no_key} rows skipped (no sample ID / item)"
        if self.unknown_columns:
            s += f"; not in template: {self.unknown_columns}"
        return s


def schema_path_for(csv_path) -> Path:
    return Path(csv_path).with_suffix(".schema.json")


class ParentCSV:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            raise LedgerError(f"parent CSV not found: {self.path}. "
                              "Create it with ParentCSV.create(path, schema).")
        sp = schema_path_for(self.path)
        self.schema = TemplateSchema.load(sp) if sp.exists() else None
        self.columns, self.rows = self._read()
        self.keys = self.schema.key_columns if self.schema else self.columns[:1]
        missing = [k for k in self.keys if k not in self.columns]
        if missing:
            raise LedgerError(f"{self.path.name} has no {missing} column but its "
                              f"schema keys on it; was the CSV edited by hand?")
        self.id_field = self.keys[0]
        self._item_rank = {it: i for i, it in
                           enumerate(self.schema.items if self.schema else [])}

    @classmethod
    def create(cls, path, schema: TemplateSchema) -> "ParentCSV":
        """New parent CSV, or add the schema's new fields to an existing one."""
        path = Path(path)
        wanted = schema.columns + list(META_COLS)
        if path.exists():
            ledger = cls(path)
            if ledger.keys != schema.key_columns:
                raise LedgerError(
                    f"{path.name} is keyed on {ledger.keys} but the template's keys "
                    f"are {schema.key_columns}; refusing to mix them.")
            new = [c for c in wanted if c not in ledger.columns]
            schema.save(schema_path_for(path))
            ledger.schema = schema
            ledger._item_rank = {it: i for i, it in enumerate(schema.items)}
            if new:
                body = [c for c in ledger.columns if c not in META_COLS]
                ledger.columns = body + [c for c in new if c not in META_COLS] \
                    + list(META_COLS)
                ledger.save()
            return ledger
        path.parent.mkdir(parents=True, exist_ok=True)
        schema.save(schema_path_for(path))
        _write_csv(path, wanted, [])
        return cls(path)

    def _read(self) -> Tuple[List[str], List[Dict[str, str]]]:
        with open(self.path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise LedgerError(f"{self.path.name} has no header row")
            return list(reader.fieldnames), [dict(r) for r in reader]

    def _key(self, row) -> Tuple[str, ...]:
        return tuple(str(row.get(k, "")).strip().lower() for k in self.keys)

    def sample_ids(self) -> List[str]:
        seen: List[str] = []
        for r in self.rows:
            if r[self.id_field] not in seen:
                seen.append(r[self.id_field])
        return seen

    def get(self, sample_id, item: Optional[str] = None) -> Optional[Dict[str, str]]:
        want = (_text(sample_id).lower(),) + ((str(item).lower(),) if item else ())
        return next((r for r in self.rows if self._key(r)[:len(want)] == want), None)

    def rows_for(self, sample_id) -> List[Dict[str, str]]:
        sid = _text(sample_id).lower()
        return [r for r in self.rows if r[self.id_field].lower() == sid]

    def add_samples(self, rows: Iterable[Dict[str, object]], source: str = "",
                    overwrite: bool = False) -> AddResult:
        """Merge rows in by key, then save in sample order.

        Row keys may be the CSV header or any spelling that normalizes the
        same ('laser power' for 'Laser Power (W)'). A row's own source_file,
        when it has one, is written for it instead of `source` (the
        workbooks its values came from - source_extract.rows_from)."""
        res = AddResult()
        # a fuzzy spelling finds a template column, never one person's copy of it
        by_name = {normalize_label(c): c for c in self.columns
                   if not (self.schema and self.schema.inspector_base(c))}
        by_name.update({c.lower(): c for c in self.columns})
        now = datetime.now().isoformat(timespec="seconds")
        index = {self._key(r): r for r in self.rows}

        for incoming in rows:
            mapped: Dict[str, str] = {}
            src = xl_text(_text(incoming.get(SOURCE_COL))) or source
            for k, v in incoming.items():
                if self.schema and self.schema.inspector_base(str(k)):
                    # exact only: normalize_label reads "[JB]" as a unit and
                    # would put JB's reading in the template's own column
                    col = by_name.get(str(k).lower())
                    if col is None:
                        col = self._add_inspector_column(str(k))
                        by_name[col.lower()] = col
                else:
                    col = by_name.get(str(k).lower()) or by_name.get(normalize_label(k))
                if col is None:
                    if str(k) not in res.unknown_columns:
                        res.unknown_columns.append(str(k))
                    continue
                # as the review shows it: no control character (xl_text)
                if col not in META_COLS and xl_text(_text(v)) != "":
                    mapped[col] = xl_text(_text(v))
            key = self._key(mapped)
            label = " / ".join(mapped.get(k, "") for k in self.keys)
            if not all(key):
                res.skipped_no_key += 1
                continue
            existing = index.get(key)
            if existing is None:
                row = {c: "" for c in self.columns}
                row.update(mapped)
                row[SOURCE_COL], row[UPDATED_COL] = src, now
                self.rows.append(row)
                index[key] = row
                res.added.append(label)
                continue
            changed = False
            for col, val in mapped.items():
                old = existing.get(col, "")
                if old == val or col in self.keys:
                    continue
                if old == "" or overwrite:
                    existing[col] = val
                    changed = True
                else:
                    res.conflicts.append((label, col, old, val))
            if changed:
                existing[SOURCE_COL], existing[UPDATED_COL] = src, now
                res.updated.append(label)
            else:
                res.unchanged.append(label)

        self.save()
        return res

    def _add_inspector_column(self, col: str) -> str:
        """'<field> [JB]' joins the CSV right after <field> and the other
        inspectors' columns for it, in initials order."""
        base = self.schema.inspector_base(col)
        mine = [c for c in self.columns if c == base or self.schema.inspector_base(c) == base]
        at = self.columns.index(mine[-1]) + 1 if mine else len(
            [c for c in self.columns if c not in META_COLS])
        for i, c in enumerate(mine[1:], start=self.columns.index(mine[0]) + 1):
            if c.lower() > col.lower():
                at = i
                break
        self.columns.insert(at, col)
        for r in self.rows:
            r.setdefault(col, "")
        return col

    def _sort_key(self, row) -> tuple:
        key = (sample_sort_key(row.get(self.id_field, "")),)
        if len(self.keys) > 1:
            item = row.get(self.keys[1], "")
            rank = self._item_rank.get(item)
            key += ((0, rank, ()) if rank is not None else (1, 0, sample_sort_key(item)),)
        return key

    def save(self) -> None:
        self.rows.sort(key=self._sort_key)
        _write_csv(self.path, self.columns, self.rows)


def _write_csv(path: Path, columns: List[str], rows: List[Dict[str, str]]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in columns})
        try:
            os.replace(tmp, path)
        except PermissionError as e:
            raise LedgerError(f"could not write {path.name}: it is probably open "
                              "in Excel. Close it and try again.") from e
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ── command line ────────────────────────────────────────────────────────
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Read a template workbook and "
                                 "create (or extend) the parent sample CSV.")
    ap.add_argument("template", help="the template .xlsx")
    ap.add_argument("--csv", help="parent CSV path (default: <template>_parent.csv)")
    ap.add_argument("--id-field", help="label of the sample-ID field, if not detected")
    ap.add_argument("--add", nargs="*", default=[], metavar="FILLED_XLSX",
                    help="filled-in copies of the template to add as samples")
    a = ap.parse_args(argv)
    try:
        schema = read_template(a.template, a.id_field)
        csv_path = Path(a.csv) if a.csv else Path(a.template).with_name(
            Path(a.template).stem.replace(" ", "_") + "_parent.csv")
        ledger = ParentCSV.create(csv_path, schema)
        print(schema.describe())
        for filled in a.add:
            res = ledger.add_samples(read_filled(schema, filled), source=Path(filled).name)
            print(f"\n{Path(filled).name}: {res.describe()}")
            for c in res.conflicts[:20]:
                print(f"  conflict {c}")
    except LedgerError as e:
        print(f"error: {e}")
        return 1
    print(f"\nparent CSV: {ledger.path} ({len(ledger.sample_ids())} samples, "
          f"{len(ledger.rows)} rows, {len(ledger.columns)} columns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
