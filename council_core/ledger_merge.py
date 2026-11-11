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
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sample_ledger import (SOURCE_COL, LedgerError, ParentCSV, TemplateSchema,
                           _text, _typed, is_filled_template, mark_filled,
                           read_template, sample_sort_key, schema_path_for, xl_text)

#: what a merged Ledger's settings say (ledger_saved.SavedLedger.settings["kind"])
KIND = "merge"
#: files read as copies; anything else in the folder is not looked at
EXCEL = (".xlsx", ".xlsm")
#: Excel files openpyxl cannot read: listed with what to do
OLD_EXCEL = (".xls", ".xlsb")
ALL_DATA = "All data"
NOT_MERGED = "Not merged"
FILE_COL = "File"


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


def match_sheets(cells: Dict[str, Dict[Tuple[int, int], Any]],
                 layout: Dict[str, Dict[Tuple[int, int], str]],
                 template_sheets: Sequence[str]) -> Dict[str, str]:
    """Template sheet -> the copy's sheet laid out like it: the one of the
    same name when its words are in place, else the best laid-out one not
    taken yet. A template sheet with too few words to tell (a blank page)
    takes the copy's sheet of the same name, or the one in the same place.
    ``cells``: the copy's sheets (title -> {(row, col): value}), in order."""
    from council_core.ledger_examples import LAYOUT_MIN_WORDS, is_filled_copy
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
        one = {t: words}
        order = ([t] if t in free else []) + [n for n in free if n != t]
        for n in order:
            if is_filled_copy(cells[n], one):
                out[t] = n
                break
    return out


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


def _rows_from(schema: TemplateSchema, ws_of: Dict[str, Any]) -> List[Dict[str, str]]:
    def get(f, item: str) -> str:
        ws = ws_of.get(f.sheet)
        return _text(ws[f.cells[item]].value) if ws is not None else ""
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


def read_copies(schema: TemplateSchema, layout, path: Path, folder: Path
                ) -> Tuple[List[Copy], List[Problem]]:
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
        found = match_sheets(cells, layout, tsheets)
        id_sheet = schema.field(schema.id_field).sheet
        if id_sheet not in found:
            return [], [Problem(name, "its layout differs from the template (the "
                                      "template's words are not in the same cells): "
                                      "read it with Extract instead")]
        sets = [found]
        if len(tsheets) == 1:                # more sheets laid out like the template
            rest = [t for t in cells if t != found[id_sheet]]
            while rest:
                more = match_sheets({t: cells[t] for t in rest}, layout, tsheets)
                if id_sheet not in more:
                    break
                sets.append(more)
                rest.remove(more[id_sheet])
        read = [(m, _rows_from(schema, {t: wb[s] for t, s in m.items()})) for m in sets]
    finally:
        wb.close()
    copies, notes = [], []
    several = len(read) > 1
    for m, rows in read:
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
        copies.append(Copy(path, pid, rows, m, named, label))
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
          progress: Optional[Callable[..., None]] = None,
          cancel=None) -> MergeResult:
    """Every filled copy of ``template`` under ``folder`` into one parent CSV
    and one workbook in ``out_dir`` (default: '<folder> merged' beside it).
    ``progress(text, fraction)``; ``cancel`` an Event (or anything with
    is_set) to stop between files."""
    from council_core import file_guard
    from council_core.ledger_examples import template_layout
    template, folder = Path(template), Path(folder)
    out_dir = Path(out_dir) if out_dir else default_out_dir(folder)
    if not template.is_file():
        raise LedgerError(f"template not found: {template}")
    if out_dir.resolve() == folder.resolve() or folder.resolve() in out_dir.resolve().parents:
        raise LedgerError("save the merge outside the folder of copies, so it is not read "
                          "as a copy the next time")
    say = progress or (lambda *_a, **_k: None)
    schema = read_template(template)
    layout = template_layout(template)
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
                path, lambda p=path: read_copies(schema, layout, p, folder), tab="Ledger")
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
    finally:
        for stale in (tmp_csv, schema_path_for(tmp_csv)):
            if stale.exists():
                stale.unlink()
    _save_ledger(res, recursive)
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
    used = {ALL_DATA.lower(), NOT_MERGED.lower()}
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
def _save_ledger(res: MergeResult, recursive: bool) -> None:
    """On the list of saved Ledgers, with a run per merge; its folder is
    merged again by "Update with new files"."""
    from council_core import ledger_saved
    led = ledger_saved.load(res.csv) or ledger_saved.SavedLedger(csv=res.csv)
    led.template = res.template
    led.sources = [res.folder]
    led.settings = {"kind": KIND, "recursive": bool(recursive), "workbook": res.workbook}
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
                 progress=progress, cancel=cancel)


__all__ = ["KIND", "MergeResult", "Problem", "Copy", "merge", "merge_again", "is_merge",
           "default_out_dir", "output_paths", "find_files", "read_copies", "match_sheets",
           "write_workbook"]
