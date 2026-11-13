"""
council_core.ledger_merge — merge a folder of filled-out copies of a
template into one workbook (user, 2026-11-11: "a template and then a folder
of filled out files and I want to merge them together into a single output
workbook with a sheet for each part. I then want to be able to do some
analysis on that data"; a sheet per filled file was picked).

No model and no guessing: each copy is read from the template's own cells
(the cells ``sample_ledger.read_template`` found for each field), so a copy
must keep the template's layout. One whose layout differs is listed, not
read: Extract (step 3) reads files of any layout by their labels.

    merge(template, folder, out_dir)  -> MergeResult

writes into ``out_dir`` (never into the folder of copies):

* ``<template> - merged.csv`` and its schema: an ordinary parent CSV, so the
  Analyze window, Groups and the Connections graph read it as they read any
  Ledger. Saved as a Ledger too (kind "merge"); "Update with new files"
  merges its folder again.
* ``<template> - merged.xlsx``: an "All data" sheet (the parent CSV: a row
  per part, or per part per flaw, with the file it came from), a "Not
  merged" sheet when anything was left out or disagrees, then the template
  filled in once per copy, a sheet each (a template of several sheets:
  '<part> <sheet>'), in part order.

A part found in two copies keeps the first copy's values in the parent CSV
(both copies still get their own sheet); every value the second disagrees
on is listed on "Not merged". Nothing here imports Qt.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    import pandas as pd

from sample_ledger import (SOURCE_COL, LedgerError, ParentCSV, TemplateSchema,
                           _text, _typed, is_filled_template, mark_filled,
                           read_template, sample_sort_key, schema_path_for, xl_text)

#: what a merged Ledger's settings say (ledger_saved.SavedLedger.settings["kind"])
KIND = "merge"
#: files read as copies; anything else in the folder is not looked at
EXCEL = (".xlsx", ".xlsm")
#: Excel files openpyxl cannot read: listed with what to do
OLD_EXCEL = (".xls", ".xlsb")
#: the share of the template's words a copy's sheet must have in the same
#: cells to be read as a copy (Andy, 2026-11-11: skip files that don't match
#: at least 75% of the template); the merge window can change it
MIN_MATCH = 0.75
ALL_DATA = "All data"
NOT_MERGED = "Not merged"
CHECK = "Check"
FILE_COL = "File"
#: what the Check sheet says of each value (Andy's LMM Analysis Studio
#: script, 2026-11-13: a mapping audit and a reason for every blank)
READ, MOVED, BLANK, NO_SHEET = ("read", "read at moved cell", "blank in the copy",
                                "sheet not found")
CHECK_COLS = [FILE_COL, "Part", "Field", "Item", "Template cell", "Read from", "Value",
              "Status"]


@dataclass
class Problem:
    file: str                 # file name (relative to the folder)
    reason: str
    part: str = ""
    field: str = ""
    kept: str = ""            # the value the parent CSV kept, and from which file
    value: str = ""           # this file's value


@dataclass
class Copy:
    path: Path
    part: str
    rows: List[Dict[str, str]]
    sheets: Dict[str, str]    # template sheet -> the copy's sheet it was read from
    named_by_file: bool = False
    label: str = ""           # file name, '[sheet]' after it when a file has several
    moved: Dict[str, "Placement"] = field(default_factory=dict)  # template sheet -> where it moved
    #: (field, item, template cell, cell read in the copy ('' none), value, status)
    audit: List[Tuple[str, str, str, str, str, str]] = field(default_factory=list)


@dataclass
class MergeResult:
    csv: str
    workbook: str
    template: str
    folder: str
    files: int = 0            # Excel files found
    merged: int = 0           # copies read into the Ledger
    parts: int = 0
    problems: List[Problem] = field(default_factory=list)
    copies: List[Copy] = field(default_factory=list)

    @property
    def left_out(self) -> List[Problem]:
        return [p for p in self.problems if not p.field]

    @property
    def clashes(self) -> List[Problem]:
        return [p for p in self.problems if p.field]

    def describe(self) -> str:
        head = (f"Merged {self.merged} of {self.files} file(s) into {self.parts} part(s): "
                f"{Path(self.workbook).name}")
        lines = [head]
        if self.left_out:
            lines.append(f"{len(self.left_out)} file(s) or note(s) on the "
                         f"'{NOT_MERGED}' sheet:")
            lines += [f"  {p.file}: {p.reason}" for p in self.left_out[:20]]
            if len(self.left_out) > 20:
                lines.append(f"  … and {len(self.left_out) - 20} more")
        if self.clashes:
            parts = sorted({p.part for p in self.clashes})
            lines.append(f"{len(self.clashes)} value(s) differ between copies of the same "
                         f"part ({', '.join(parts[:8])}{' …' if len(parts) > 8 else ''}): "
                         "the first file's are kept, both are listed.")
        return "\n".join(lines)


# ── where things go ─────────────────────────────────────────────────────
def default_out_dir(folder) -> Path:
    """Beside the folder of copies, not in it: '<folder> merged'."""
    folder = Path(folder)
    return folder.with_name(f"{folder.name} merged")


def check_path(csv) -> Path:
    """Where a merge writes what it read from each copy, cell by cell (read by
    the Analyze window's Check page)."""
    csv = Path(csv)
    return csv.with_name(f"{csv.stem} - check.csv")


def check_rows(res: "MergeResult") -> List[List[str]]:
    """Each value of each copy: file, part, field, item, template cell, the
    cell read, the value, and its status (read / read at moved cell / blank
    in the copy / sheet not found)."""
    return [[c.label, c.part, fname, item, tcell, cell, value, status]
            for c in res.copies for fname, item, tcell, cell, value, status in c.audit]


def _write_check(res: "MergeResult", path: Path) -> None:
    import csv as _csv
    tmp = path.with_name(path.name + ".writing")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.writer(fh)
        w.writerow(CHECK_COLS)
        w.writerows(check_rows(res))
    try:
        os.replace(tmp, path)
    except PermissionError as e:
        tmp.unlink(missing_ok=True)
        raise LedgerError(f"could not write {path.name}: it is probably open in Excel. "
                          "Close it and merge again.") from e


def read_check(csv) -> Optional[pd.DataFrame]:
    """What a merge read from each copy (check_path), or None when the Ledger
    is not a merge or was merged before the check was kept."""
    import pandas as pd
    path = check_path(csv)
    if not path.is_file():
        return None
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def output_paths(template, out_dir) -> Tuple[Path, Path]:
    stem = Path(template).stem
    out = Path(out_dir)
    return out / f"{stem} - merged.csv", out / f"{stem} - merged.xlsx"


def find_files(folder, recursive: bool = True,
               skip: Sequence[Path] = ()) -> Tuple[List[Path], List[Problem]]:
    """The Excel files under ``folder`` (Excel's own '~$' lock files and the
    paths in ``skip`` left out), and a Problem for each old-format one."""
    folder = Path(folder)
    if not folder.is_dir():
        raise LedgerError(f"folder not found: {folder}")
    skip_set = {Path(p).resolve() for p in skip}
    found = folder.rglob("*") if recursive else folder.glob("*")
    books, problems = [], []
    for p in sorted(found, key=lambda q: str(q).lower()):
        if not p.is_file() or p.name.startswith("~$") or p.resolve() in skip_set:
            continue
        ext = p.suffix.lower()
        if ext in EXCEL:
            books.append(p)
        elif ext in OLD_EXCEL:
            problems.append(Problem(_rel(p, folder), f"{ext} cannot be read: save it as "
                                                     ".xlsx in Excel, then merge again"))
    return books, problems


def _rel(path: Path, folder: Path) -> str:
    try:
        return str(Path(path).relative_to(folder))
    except ValueError:
        return Path(path).name


# ── reading one copy ────────────────────────────────────────────────────
def _cells(ws) -> Dict[Tuple[int, int], Any]:
    return {rc: c.value for rc, c in list(ws._cells.items())[:500_000]
            if c.value is not None}


def label_layout(template, schema: TemplateSchema) -> Dict[str, Dict[Tuple[int, int], str]]:
    """The template's own labels and headings by sheet and cell - what a
    filled copy keeps in the same places. Only text typed into the template:
    not a formula's result (an AVERAGE over blank cells reads '#DIV/0!' in
    the template and a number in a copy), and not a field's value cell (a
    pre-filled or placeholder value a copy types over). Andy, 2026-11-11:
    the match is to look at the headers, not the values."""
    import openpyxl
    from openpyxl.utils.cell import column_index_from_string, coordinate_from_string
    from council_core.ledger_examples import _norm
    value_cells: Dict[str, set] = {}
    for f in schema.fields:
        for a1 in f.cells.values():
            try:
                col, row = coordinate_from_string(a1)
            except Exception:                               # noqa: BLE001
                continue
            value_cells.setdefault(f.sheet, set()).add((row, column_index_from_string(col)))
    out: Dict[str, Dict[Tuple[int, int], str]] = {}
    try:
        wb = openpyxl.load_workbook(template, data_only=False)   # formulas as written
    except Exception:                                       # noqa: BLE001
        return out
    try:
        for ws in wb.worksheets:
            skip = value_cells.get(ws.title, set())
            words: Dict[Tuple[int, int], str] = {}
            for rc, cell in list(ws._cells.items())[:200_000]:
                v = cell.value
                if (isinstance(v, str) and not v.startswith("=") and rc not in skip
                        and len(v) <= 120 and re.search(r"[A-Za-z]", v)
                        and not v.strip().startswith("#")):
                    words[rc] = _norm(v)
            if words:
                out[ws.title] = words
    finally:
        wb.close()
    return out


def missing_labels(cells: Dict[str, Dict[Tuple[int, int], Any]], words, most: int = 5
                   ) -> Tuple[List[str], int]:
    """The template labels the best-matching sheet of a copy does not have
    in place (the first ``most``), and how many in all."""
    from council_core.ledger_examples import _norm
    if not cells or not words:
        return [], 0
    best = max(cells.values(), key=lambda c: layout_share(c, words))
    gone = [w for rc, w in words.items() if _norm(best.get(rc)) != w]
    return list(dict.fromkeys(gone))[:most], len(gone)


def layout_share(cells: Dict[Tuple[int, int], Any], words: Dict[Tuple[int, int], str]) -> float:
    """The share of the template sheet's words this sheet has in the same
    cells (0..1)."""
    from council_core.ledger_examples import _norm
    if not words:
        return 0.0
    return sum(1 for rc, w in words.items() if _norm(cells.get(rc)) == w) / len(words)


def match_sheets(cells: Dict[str, Dict[Tuple[int, int], Any]],
                 layout: Dict[str, Dict[Tuple[int, int], str]],
                 template_sheets: Sequence[str],
                 min_share: float = MIN_MATCH,
                 placements: Optional[Dict[str, "Placement"]] = None) -> Dict[str, str]:
    """Template sheet -> the copy's sheet laid out like it (``min_share`` of
    the template's words in the same cells): the one of the same name when
    it is, else the best laid-out one not taken yet. A template sheet with
    too few words to tell (a blank page) takes the copy's sheet of the same
    name, or the one in the same place. ``placements``: when given, a sheet
    whose rows or columns moved is matched too (``min_share`` of the words
    found where they moved), and its Placement put in it.
    ``cells``: the copy's sheets (title -> {(row, col): value}), in order."""
    from council_core.ledger_examples import LAYOUT_MIN_WORDS
    names = list(cells)
    out: Dict[str, str] = {}
    for i, t in enumerate(template_sheets):
        words = layout.get(t, {})
        free = [n for n in names if n not in out.values()]
        if len(words) < LAYOUT_MIN_WORDS:
            if t in free:
                out[t] = t
            elif i < len(names) and names[i] in free:
                out[t] = names[i]
            continue
        shares = {n: layout_share(cells[n], words) for n in free}
        if t in shares and shares[t] >= min_share:
            out[t] = t
            continue
        best = max(shares, key=shares.get, default=None)
        if best is not None and shares[best] >= min_share:
            out[t] = best
            continue
        if placements is None:
            continue
        # no sheet in place: one whose rows / columns moved (same name first)
        tried = {n: place(cells[n], words) for n in free}
        best = max(tried, key=lambda n: (tried[n].share, n == t), default=None)
        if best is not None and tried[best].share >= min_share and tried[best].moved:
            out[t] = best
            placements[t] = tried[best]
    return out


def best_share(cells: Dict[str, Dict[Tuple[int, int], Any]], words) -> float:
    return max((max(layout_share(c, words), place(c, words).share)
                for c in cells.values()), default=0.0)


# ── a copy whose rows or columns moved ──────────────────────────────────
# Andy, 2026-11-13 (from his LMM Analysis Studio script): a copy with rows
# inserted or deleted, or a table moved down, is still a copy - read each
# value at the place its labels moved to. Still no model and no guessing:
# the template's rows and columns are lined up with the copy's by the labels
# they hold, in order, and a value is read where its row and column went.
@dataclass
class Placement:
    """Where a template sheet's rows and columns are in a copy's sheet."""
    rows: Dict[int, int]            # template row -> the copy's row (anchors)
    cols: Dict[int, int]            # template column -> the copy's column (anchors)
    share: float = 0.0              # the template's labels found where they moved

    @staticmethod
    def _go(anchors: Dict[int, int], n: int) -> int:
        """n moved as the nearest anchor at or before it moved (the label
        above a value, or to its left); before every anchor, as the first."""
        if not anchors or n in anchors:
            return anchors.get(n, n)
        keys = sorted(anchors)
        before = [k for k in keys if k <= n]
        k = before[-1] if before else keys[0]
        return n + anchors[k] - k

    def cell(self, row: int, col: int) -> Tuple[int, int]:
        return self._go(self.rows, row), self._go(self.cols, col)

    def a1(self, address: str) -> str:
        from openpyxl.utils.cell import (column_index_from_string, coordinate_from_string,
                                         get_column_letter)
        col, row = coordinate_from_string(address)
        r, c = self.cell(row, column_index_from_string(col))
        return f"{get_column_letter(c)}{r}"

    @property
    def moved(self) -> bool:
        return any(k != v for k, v in self.rows.items()) or any(
            k != v for k, v in self.cols.items())

    def describe(self) -> str:
        """'rows from 5 on are 2 lower; columns from D on are 1 to the right'."""
        from openpyxl.utils import get_column_letter
        out = []
        for what, anchors, name in (("rows", self.rows, str),
                                    ("columns", self.cols, get_column_letter)):
            last = 0
            for k in sorted(anchors):
                d = anchors[k] - k
                if d != last:
                    side = (("lower" if d > 0 else "higher") if what == "rows"
                            else ("to the right" if d > 0 else "to the left"))
                    out.append(f"{what} from {name(k)} on are {abs(d)} {side}"
                               if d else f"{what} from {name(k)} on are back in place")
                    last = d
        return "; ".join(out) or "in place"


def _line_up(template: Dict[int, frozenset], copy: Dict[int, frozenset]) -> Dict[int, int]:
    """Template rows (or columns) paired with the copy's by the labels they
    hold, kept in order (a diff of the two lists): equal ones, and in a run
    of changed ones those sharing most of their labels."""
    from difflib import SequenceMatcher
    t_keys, c_keys = sorted(template), sorted(copy)
    t_sig = [template[k] for k in t_keys]
    c_sig = [copy[k] for k in c_keys]
    sm = SequenceMatcher(None, t_sig, c_sig, autojunk=False)
    pairs: Dict[int, int] = {}
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                pairs[t_keys[i]] = c_keys[j]
        elif op == "replace":
            j = j1
            for i in range(i1, i2):
                for jj in range(j, j2):
                    a, b = t_sig[i], c_sig[jj]
                    if len(a & b) * 2 >= max(len(a), len(b)):
                        pairs[t_keys[i]] = c_keys[jj]
                        j = jj + 1
                        break
    return pairs


def place(cells: Dict[Tuple[int, int], Any], words: Dict[Tuple[int, int], str]
          ) -> Placement:
    """How the template sheet (its ``words``) lines up with a copy's sheet,
    and the share of its labels found once rows and columns are moved."""
    from council_core.ledger_examples import _norm
    vocab = set(words.values())
    seen = {rc: _norm(v) for rc, v in cells.items() if isinstance(v, str)}
    seen = {rc: w for rc, w in seen.items() if w in vocab}

    def by(axis: int, src) -> Dict[int, frozenset]:
        out: Dict[int, set] = {}
        for rc, w in src.items():
            out.setdefault(rc[axis], set()).add(w)
        return {k: frozenset(v) for k, v in out.items()}
    p = Placement(_line_up(by(0, words), by(0, seen)), _line_up(by(1, words), by(1, seen)))
    if words:
        p.share = sum(1 for (r, c), w in words.items()
                      if seen.get(p.cell(r, c)) == w) / len(words)
    return p


def _entered(schema: TemplateSchema, rows: List[Dict[str, str]]) -> int:
    """Values in the copy other than the template's own pre-filled ones."""
    n = 0
    for f in schema.fields:
        if f.name == schema.id_field:
            continue
        for row in rows[:1] if not f.per_item else rows:
            v = row.get(f.name, "")
            item = row.get(schema.item_field, "") if f.per_item else ""
            if v and v != str(f.defaults.get(item, "")):
                n += 1
    return n


def _rows_from(schema: TemplateSchema, ws_of: Dict[str, Any],
               placed: Optional[Dict[str, Placement]] = None,
               audit: Optional[list] = None) -> List[Dict[str, str]]:
    """The copy's rows, read at the template's cells (where they moved, on a
    sheet in ``placed``); ``audit`` gets (field, item, template cell, cell
    read, value, status) for every value."""
    placed = placed or {}
    audit = audit if audit is not None else []

    def get(f, item: str) -> str:
        ws = ws_of.get(f.sheet)
        tcell = f.cells[item]
        if ws is None:
            audit.append((f.name, item, tcell, "", "", NO_SHEET))
            return ""
        p = placed.get(f.sheet)
        cell = p.a1(tcell) if p else tcell
        v = _text(ws[cell].value)
        audit.append((f.name, item, tcell, cell, xl_text(v),
                      BLANK if not str(v).strip() else MOVED if cell != tcell else READ))
        return v
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


def read_copies(schema: TemplateSchema, layout, path: Path, folder: Path,
                min_share: float = MIN_MATCH) -> Tuple[List[Copy], List[Problem]]:
    """The filled copies in one workbook, read by the template's cells, and
    what to say about it. A template of one sheet: every sheet laid out like
    it is a copy (a workbook with a sheet per part). Of several sheets: one
    copy, its sheets matched to the template's."""
    import openpyxl
    name = _rel(path, folder)
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:                                  # noqa: BLE001
        return [], [Problem(name, f"could not be opened: {e}")]
    try:
        if ALL_DATA in wb.sheetnames and is_filled_template(path):
            return [], [Problem(name, "a merged workbook the Council made, not a copy")]
        tsheets = list(dict.fromkeys(f.sheet for f in schema.fields))
        cells = {ws.title: _cells(ws) for ws in wb.worksheets}
        placed: Dict[str, Placement] = {}
        found = match_sheets(cells, layout, tsheets, min_share, placed)
        id_sheet = schema.field(schema.id_field).sheet
        if id_sheet not in found:
            words = layout.get(id_sheet, {})
            got = best_share(cells, words)
            gone, n_gone = missing_labels(cells, words)
            more = f" (+{n_gone - len(gone)} more)" if n_gone > len(gone) else ""
            return [], [Problem(name, f"skipped: matches {got:.0%} of the template's layout "
                                      f"(needs {min_share:.0%}); read it with Extract "
                                      "instead. Template labels not in place: "
                                      + ", ".join(f"'{w}'" for w in gone) + more)]
        sets = [(found, placed)]
        if len(tsheets) == 1:                # more sheets laid out like the template
            rest = [t for t in cells if t != found[id_sheet]]
            while rest:
                more_placed: Dict[str, Placement] = {}
                more = match_sheets({t: cells[t] for t in rest}, layout, tsheets,
                                    min_share, more_placed)
                if id_sheet not in more:
                    break
                sets.append((more, more_placed))
                rest.remove(more[id_sheet])
        read = []
        for m, pl in sets:
            audit: list = []
            read.append((m, pl, _rows_from(schema, {t: wb[s] for t, s in m.items()}, pl,
                                           audit), audit))
    finally:
        wb.close()
    copies, notes = [], []
    several = len(read) > 1
    for m, pl, rows, audit in read:
        label = f"{name} [{m[id_sheet]}]" if several else name
        pid = xl_text(rows[0].get(schema.id_field, "")).strip()
        if not pid and not _entered(schema, rows):
            if not several:
                notes.append(Problem(label, "nothing filled in (an empty copy of the "
                                            "template)"))
            continue
        named = False
        if not pid:
            pid, named = (path.stem if not several else f"{path.stem} {m[id_sheet]}"), True
            for r in rows:
                r[schema.id_field] = pid
        copies.append(Copy(path, pid, rows, m, named, label, moved=dict(pl), audit=audit))
        for t, p in pl.items():
            notes.append(Problem(label, f"read with its layout moved{' on ' + t if len(tsheets) > 1 else ''}"
                                        f" ({p.describe()}): {p.share:.0%} of the template's "
                                        "labels found where they moved. Check its values.",
                                 part=pid))
        missing = [t for t in tsheets if t not in m]
        if missing:
            notes.append(Problem(label, f"sheet(s) {', '.join(missing)} not found laid out "
                                        "like the template: their fields are blank",
                                 part=pid))
    return copies, notes


# ── the merge ───────────────────────────────────────────────────────────
def _check_csv(csv: Path) -> None:
    """A parent CSV the merge wrote before is made again from the copies;
    any other file is never written over."""
    from council_core import ledger_saved
    if csv.exists():
        led = ledger_saved.load(csv)
        if led is None or led.settings.get("kind") != KIND:
            raise LedgerError(f"{csv.name} is there already and is not a merge the Council "
                              "made: pick another folder to save to")


def _swap_in(tmp_csv: Path, csv: Path, schema: TemplateSchema) -> None:
    """The CSV built beside (and its schema) put in place of the old one."""
    try:
        os.replace(tmp_csv, csv)
    except PermissionError as e:
        raise LedgerError(f"could not write {csv.name}: it is probably open in Excel. "
                          "Close it and merge again.") from e
    schema.save(schema_path_for(csv))
    tmp_schema = schema_path_for(tmp_csv)
    if tmp_schema.exists():
        tmp_schema.unlink()


def merge(template, folder, out_dir=None, *, recursive: bool = True,
          min_match: float = MIN_MATCH,
          progress: Optional[Callable[..., None]] = None,
          cancel=None) -> MergeResult:
    """Every filled copy of ``template`` under ``folder`` into one parent CSV
    and one workbook in ``out_dir`` (default: '<folder> merged' beside it).
    ``progress(text, fraction)``; ``cancel`` an Event (or anything with
    is_set) to stop between files. ``min_match``: the share of the
    template's layout a copy must match (0..1); a file under it is skipped
    and listed with its share."""
    from council_core import file_guard
    template, folder = Path(template), Path(folder)
    out_dir = Path(out_dir) if out_dir else default_out_dir(folder)
    if not template.is_file():
        raise LedgerError(f"template not found: {template}")
    if not 0 < min_match <= 1:
        raise LedgerError(f"the layout match must be between 1% and 100%, not {min_match:.0%}")
    if out_dir.resolve() == folder.resolve() or folder.resolve() in out_dir.resolve().parents:
        raise LedgerError("save the merge outside the folder of copies, so it is not read "
                          "as a copy the next time")
    say = progress or (lambda *_a, **_k: None)
    schema = read_template(template)
    layout = label_layout(template, schema)
    csv_path, book_path = output_paths(template, out_dir)
    books, problems = find_files(folder, recursive, skip=[template, book_path])
    res = MergeResult(str(csv_path), str(book_path), str(template), str(folder),
                      files=len(books) + len(problems), problems=problems)
    for i, path in enumerate(books):
        if cancel is not None and cancel.is_set():
            raise LedgerError("merge cancelled: nothing written")
        say(f"reading {path.name}", (i + 1) / (len(books) + 1))
        try:
            copies, notes = file_guard.read(
                path, lambda p=path: read_copies(schema, layout, p, folder, min_match),
                tab="Ledger")
        except file_guard.Skipped as e:
            copies, notes = [], [Problem(_rel(path, folder), str(e))]
        res.problems.extend(notes)
        for copy in copies:
            if copy.named_by_file:
                res.problems.append(Problem(copy.label, f"no {schema.id_field}: named "
                                                        f"'{copy.part}' after the file",
                                            part=copy.part))
            res.copies.append(copy)
    if not res.copies:
        raise LedgerError(f"no filled copy of {template.name} found in {folder}"
                          + (f": {res.problems[0].file}: {res.problems[0].reason}"
                             if res.problems else ""))
    res.copies.sort(key=lambda c: (sample_sort_key(c.part),
                                    str(c.path.with_suffix("")).lower(), c.label))

    out_dir.mkdir(parents=True, exist_ok=True)
    say("writing the merged Ledger", None)
    _check_csv(csv_path)
    tmp_csv = csv_path.with_name(csv_path.stem + ".merging.csv")
    for stale in (tmp_csv, schema_path_for(tmp_csv)):
        if stale.exists():
            stale.unlink()
    ledger = ParentCSV.create(tmp_csv, schema)
    # A part in two copies: the parent CSV holds the first copy's values
    # only, blanks included - a blank is never filled from the other copy
    # (user, 2026-11-11: "I don't want anything to accidentally get filled
    # in"). Every value that differs, a blank on either side too, is listed.
    first: Dict[str, Copy] = {}
    per_item = {f.name for f in schema.fields if f.per_item}
    fields = [f.name for f in schema.fields if f.name != schema.id_field]
    for c in res.copies:
        key = c.part.lower()
        kept = first.get(key)
        if kept is None:
            first[key] = c
            ledger.add_samples([dict(r, **{SOURCE_COL: str(c.path)}) for r in c.rows],
                               source=str(c.path))
            continue
        said = 0
        for k_row, c_row in zip(kept.rows, c.rows):
            item = c_row.get(schema.item_field, "") if schema.item_field else ""
            for f in fields:
                if f not in per_item and item and item != kept.rows[0].get(
                        schema.item_field, ""):
                    continue                      # a part's value: once, on its first row
                old = xl_text(k_row.get(f, "")).strip()
                new = xl_text(c_row.get(f, "")).strip()
                if old == new:
                    continue
                said += 1
                res.problems.append(Problem(
                    c.label, "same part as another file, different value"
                    + ("" if old else " (blank in the kept file: left blank)"),
                    part=f"{c.part} / {item}" if f in per_item and item else c.part,
                    field=f, kept=f"{old or '(blank)'} ({kept.label})",
                    value=new or "(blank)"))
        if not said:
            res.problems.append(Problem(c.label, f"same part as {kept.label}, same values: "
                                                 "not added twice", part=c.part))
    res.merged = len(res.copies)
    res.parts = len(first)
    say("writing the merged workbook", None)
    try:
        write_workbook(schema, ParentCSV(tmp_csv), res, folder, book_path)
        _swap_in(tmp_csv, csv_path, schema)
        _write_check(res, check_path(csv_path))
    finally:
        for stale in (tmp_csv, schema_path_for(tmp_csv)):
            if stale.exists():
                stale.unlink()
    _save_ledger(res, recursive, min_match)
    return res


# ── the workbook ────────────────────────────────────────────────────────
def _autosize(ws, widest: int = 50) -> None:
    from openpyxl.utils import get_column_letter
    widths: Dict[int, int] = {}
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 200)):
        for c in row:
            if c.value is not None:
                widths[c.column] = max(widths.get(c.column, 0), len(str(c.value)))
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = min(widest, max(8, w + 2))


def write_workbook(schema: TemplateSchema, ledger: ParentCSV, res: MergeResult,
                   folder: Path, out: Path) -> str:
    """'All data', 'Not merged' (when there is something on it), then a
    template-shaped sheet per copy. Never over a file the Council did not
    write. Atomic."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from council_core.ledger_groups import _title
    if out.exists() and not is_filled_template(out):
        raise LedgerError(f"{out.name} is there already and the Council did not write it: "
                          "move it, or save the merge to another folder")
    wb = openpyxl.load_workbook(schema.template_path)
    originals = list(wb.worksheets)
    several = len(originals) > 1
    used = {ALL_DATA.lower(), NOT_MERGED.lower(), CHECK.lower()}
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    try:
        # the per-copy sheets, from each copy's own values
        for c in res.copies:
            sheets = {}
            for ws in originals:
                copy = wb.copy_worksheet(ws)
                copy.title = _title(c.part, ws.title, several, used)
                sheets[ws.title] = copy
            first = sheets[originals[0].title]

            def put(f, item: str, value) -> None:
                # every field cell as the copy has it, a blank too: a value the
                # template pre-fills does not come back where the copy has none
                value = xl_text(value)
                cell = f.cells.get(item)
                if cell:
                    sheets.get(f.sheet, first)[cell].value = (
                        None if value in (None, "") else _typed(value))
            for f in schema.fields:
                if not f.per_item:
                    put(f, "", c.rows[0].get(f.name))
            for row in c.rows:
                item = row.get(schema.item_field, "")
                for f in schema.fields:
                    if f.per_item and item in f.cells:
                        put(f, item, row.get(f.name))
        for ws in originals:
            wb.remove(ws)

        # All data: the parent CSV, the file each row came from in front
        data = wb.create_sheet(ALL_DATA, 0)
        cols = [c for c in ledger.columns if c != SOURCE_COL and c != "updated_at"]
        data.append([FILE_COL] + cols)
        for r in ledger.rows:
            src = r.get(SOURCE_COL, "")
            data.append([_rel(Path(src), folder) if src else ""]
                        + [_typed(xl_text(r.get(c, ""))) for c in cols])
        for cell in data[1]:
            cell.font, cell.fill = bold, head_fill
        data.freeze_panes = "B2" if not schema.item_field else "C2"
        data.auto_filter.ref = data.dimensions
        _autosize(data)

        if res.problems:
            nm = wb.create_sheet(NOT_MERGED, 1)
            nm.append([FILE_COL, "Part", "What", "Field", "Kept (from)", "This file's value"])
            for p in res.problems:
                nm.append([p.file, p.part, p.reason, p.field, p.kept, p.value])
            for cell in nm[1]:
                cell.font, cell.fill = bold, head_fill
            nm.freeze_panes = "A2"
            _autosize(nm, widest=70)

        # Check: every value of every copy, the cell it was read from
        ck = wb.create_sheet(CHECK, 2 if res.problems else 1)
        ck.append(CHECK_COLS)
        moved_fill = PatternFill("solid", fgColor="FFF2CC")
        blank_fill = PatternFill("solid", fgColor="EDEDED")
        for row in check_rows(res):
            ck.append([_typed(v) if i == 6 else v for i, v in enumerate(row)])
            if row[-1] in (MOVED, BLANK, NO_SHEET):
                for cell in ck[ck.max_row]:
                    cell.fill = moved_fill if row[-1] == MOVED else blank_fill
        for cell in ck[1]:
            cell.font, cell.fill = bold, head_fill
        ck.freeze_panes = "A2"
        ck.auto_filter.ref = ck.dimensions
        _autosize(ck, widest=45)
        wb.active = 0
        mark_filled(wb)
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=out.stem, suffix=".writing.xlsx")
        os.close(fd)
        try:
            wb.save(tmp)
            try:
                os.replace(tmp, out)
            except PermissionError as e:
                raise LedgerError(f"could not write {out.name}: it is probably open in "
                                  "Excel. Close it and merge again.") from e
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    finally:
        wb.close()
    return str(out)


# ── saved as a Ledger ───────────────────────────────────────────────────
def _save_ledger(res: MergeResult, recursive: bool, min_match: float = MIN_MATCH) -> None:
    """On the list of saved Ledgers, with a run per merge; its folder is
    merged again by "Update with new files"."""
    from council_core import ledger_saved
    led = ledger_saved.load(res.csv) or ledger_saved.SavedLedger(csv=res.csv)
    led.template = res.template
    led.sources = [res.folder]
    led.settings = {"kind": KIND, "recursive": bool(recursive), "workbook": res.workbook,
                    "min_match": float(min_match)}
    if not led.name:
        led.name = f"{Path(res.template).stem} merged ({Path(res.folder).name})"
    led.files = {}
    for c in res.copies:
        st = ledger_saved.stamp(c.path)
        if st is not None:
            led.files[ledger_saved.file_key(c.path)] = st
    now = datetime.now()
    rid = f"{now:%Y%m%d_%H%M%S}"
    while led.run(rid):
        rid += "_"
    values = sum(1 for c in res.copies for r in c.rows for k, v in r.items() if v)
    led.runs.append(ledger_saved.Run(
        id=rid, name=f"Merged {res.merged} file(s), {now:%d %b %Y}", kind=KIND,
        at=now.isoformat(timespec="seconds"), files=res.merged, values=values,
        to_review=len(res.clashes)))
    ledger_saved.save(led)


def is_merge(csv) -> bool:
    """A Ledger the merge made (its folder is merged again, not extracted)."""
    from council_core import ledger_saved
    led = ledger_saved.load(csv) if csv else None
    return led is not None and led.settings.get("kind") == KIND


def merge_again(csv, progress=None, cancel=None) -> MergeResult:
    """A merged Ledger's folder merged again, with the same template and
    settings (new, changed and removed copies all taken in)."""
    from council_core import ledger_saved
    led = ledger_saved.load(csv)
    if led is None or led.settings.get("kind") != KIND or not led.sources:
        raise LedgerError(f"{Path(csv).name} is not a merge the Council made")
    return merge(led.template, led.sources[0], Path(csv).parent,
                 recursive=bool(led.settings.get("recursive", True)),
                 min_match=float(led.settings.get("min_match", MIN_MATCH)),
                 progress=progress, cancel=cancel)


__all__ = ["KIND", "MIN_MATCH", "layout_share", "MergeResult", "Problem", "Copy", "merge", "merge_again", "is_merge",
           "default_out_dir", "output_paths", "find_files", "read_copies", "match_sheets",
           "write_workbook"]
