"""
council_core.ledger_groups — a Ledger's parts in sub-groups, each written to
a workbook of its own beside the full one (user, 2026-10-26: "in the review
step I'd like to be able to create sub groups ... data that has changed over
time. So earlier data has some datapoints that aren't tracked anymore or got
condensed down to just yes or no ... slice the data into smaller output
workbooks in addition to the full workbook"; "The output workbooks for the
full and group should have separate sheets of the filled out template for
each part number ... ordered in numerical order. Data is most commonly
separated by part name but can be date or source folder").

A group takes its parts by part name (``ABC*, *-R1``, or a part-name pattern
``@@@#@@###``), by a date field (from / to), or by source folder - and by
hand: parts ticked in or out. Its fields are each tracked, not tracked (left
out of its workbook, and not asked about in review for its parts), or Yes/No
(a value is Yes - or only the values listed - and a blank is No).

Groups are kept in the saved Ledger (council_core.ledger_saved), so an
update puts new parts in them by the same rules. ``write_all`` writes the
full workbook and one per group: the template once per part, each part's
sheet(s) in numerical order (sample_ledger.sample_sort_key). A workbook is
only ever written over when the Ledger wrote it (its FILLED_MARK).
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from sample_ledger import (FILLED_MARK, SOURCE_COL, LedgerError, ParentCSV, _typed,
                           is_filled_template, sample_sort_key, xl_text)

#: how a group takes its parts
BY = {"part": "Part name", "date": "Date", "folder": "Source folder",
      "hand": "Picked by hand"}
#: what a group does with a field
TRACKED, OFF, YES_NO = "tracked", "off", "yes/no"
FIELD_MODES = {TRACKED: "Tracked", OFF: "Not tracked", YES_NO: "Yes / No"}
ALL_PARTS = "all parts"


@dataclass
class Group:
    name: str
    by: str = "part"               # a BY key
    pattern: str = ""              # by part name
    date_field: str = ""           # by date: the field, and the range (inclusive)
    date_from: str = ""
    date_to: str = ""
    folder: str = ""               # by source folder
    include: List[str] = field(default_factory=list)      # ticked in by hand
    exclude: List[str] = field(default_factory=list)      # ticked out by hand
    off: List[str] = field(default_factory=list)          # fields not tracked
    #: Yes/No fields -> the values that are Yes ('' : any value)
    yes_no: Dict[str, str] = field(default_factory=dict)

    def mode(self, fname: str) -> str:
        if fname in self.off:
            return OFF
        return YES_NO if fname in self.yes_no else TRACKED

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Group":
        g = cls(name=str(d.get("name") or "group"))
        for k in ("by", "pattern", "date_field", "date_from", "date_to", "folder"):
            setattr(g, k, str(d.get(k) or getattr(g, k)))
        g.include = [str(x) for x in d.get("include") or []]
        g.exclude = [str(x) for x in d.get("exclude") or []]
        g.off = [str(x) for x in d.get("off") or []]
        g.yes_no = {str(k): str(v or "") for k, v in (d.get("yes_no") or {}).items()}
        return g


def load_groups(csv) -> List[Group]:
    from council_core import ledger_saved
    led = ledger_saved.load(csv)
    return [Group.from_dict(g) for g in (led.groups if led else [])]


def save_groups(csv, groups: Sequence[Group], template: str = "") -> None:
    """Into the saved Ledger (made now when there is none)."""
    from council_core import ledger_saved
    names = [g.name.strip().lower() for g in groups]
    if any(not n for n in names) or len(set(names)) != len(names):
        raise LedgerError("each group needs a name of its own")
    if ALL_PARTS in names:
        raise LedgerError(f"'{ALL_PARTS}' is the full workbook's name: name the group "
                          "otherwise")
    led = ledger_saved.load(csv) or ledger_saved.SavedLedger(csv=str(csv), template=template)
    led.groups = [asdict(g) for g in groups]
    ledger_saved.save(led)


# ── which parts ─────────────────────────────────────────────────────────
@dataclass
class PartFacts:
    values: Dict[str, str] = field(default_factory=dict)  # field -> its first value
    sources: Set[str] = field(default_factory=set)        # workbook names, lower case


def facts_from_ledger(ledger: ParentCSV) -> Dict[str, PartFacts]:
    """Each part of the parent CSV: its values and the workbooks they came
    from."""
    out: Dict[str, PartFacts] = {}
    for r in ledger.rows:
        part = str(r.get(ledger.id_field, "") or "").strip()
        if not part:
            continue
        f = out.setdefault(part, PartFacts())
        for k, v in r.items():
            if v and k not in f.values:
                f.values[k] = str(v)
        for name in re.split(r"\s*,\s*", str(r.get(SOURCE_COL, "") or "")):
            if name:
                f.sources.add(name.lower())
    return out


def add_decisions(facts: Dict[str, PartFacts], decisions: Iterable) -> None:
    """A run's values too (source_extract.Decision): a part new to the
    Ledger joins its groups by the values just read."""
    for d in decisions:
        o = d.obs
        if not o.part:
            continue
        f = facts.setdefault(o.part, PartFacts())
        if o.value not in ("", None) and o.field not in f.values:
            f.values[o.field] = str(o.value)
        f.sources.add(str(o.where).split(" › ", 1)[0].strip().lower())


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%d %b %Y",
                 "%d %B %Y", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y", "%Y%m%d",
                 "%Y-%m", "%Y")


def parse_date(text: Any) -> Optional[date]:
    """A date as a workbook or a person writes it ('2026-10-01', '10/1/2026'
    - month first -, '1 Oct 2026', an Excel day number, '2024'); None when
    it is not one."""
    s = str(text or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    head = s.split(" ")[0] if re.match(r"\d{4}-\d\d-\d\d[ T]", s) else s
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    try:
        n = float(s)
    except ValueError:
        return None
    if 20000 < n < 80000:                         # an Excel day number
        return date(1899, 12, 30) + timedelta(days=int(n))
    return None


def _part_matches(part: str, pattern: str) -> bool:
    from source_extract import mask_to_regex
    for piece in re.split(r"\s*[,;|]\s*", pattern.strip()):
        if not piece:
            continue
        if ("@" in piece or "#" in piece) and not any(c in piece for c in "*?["):
            try:
                if mask_to_regex(piece, exact=True).fullmatch(part):
                    return True
            except LedgerError:
                pass
            continue
        if fnmatch(part.lower(), piece.lower()):
            return True
    return False


def folder_names(csv, folder: str) -> Set[str]:
    """The workbooks the Ledger read from ``folder`` (or below it), by name -
    the parent CSV names a value's workbooks, not their folders."""
    from council_core import ledger_saved
    led = ledger_saved.load(csv)
    if led is None or not folder.strip():
        return set()
    root = ledger_saved.file_key(folder).rstrip("\\/")
    return {Path(k).name.lower() for k in led.files
            if k.startswith(root + os.sep) or k.startswith(root + "/")}


def by_rule(g: Group, part: str, facts: Optional[PartFacts],
            in_folder: Optional[Set[str]] = None) -> bool:
    if g.by == "part":
        return bool(g.pattern.strip()) and _part_matches(part, g.pattern)
    if g.by == "date":
        d = parse_date((facts.values if facts else {}).get(g.date_field, ""))
        lo, hi = parse_date(g.date_from), parse_date(g.date_to)
        return d is not None and (lo is not None or hi is not None) and \
            (lo is None or d >= lo) and (hi is None or d <= hi)
    if g.by == "folder":
        return bool(facts and in_folder and facts.sources & in_folder)
    return False


def members(g: Group, facts: Dict[str, PartFacts], csv=None) -> List[str]:
    """The group's parts, in numerical order: its rule's, plus the ones
    ticked in, less the ones ticked out."""
    in_folder = folder_names(csv, g.folder) if g.by == "folder" and csv else set()
    out = []
    picked_in = {p.lower() for p in g.include}
    picked_out = {p.lower() for p in g.exclude}
    for part, f in facts.items():
        key = part.lower()
        if key in picked_out:
            continue
        if key in picked_in or by_rule(g, part, f, in_folder):
            out.append(part)
    known = {p.lower() for p in out}
    out += [p for p in g.include if p.lower() not in known and p.lower() not in picked_out
            and p not in facts]
    return sorted(dict.fromkeys(out), key=sample_sort_key)


# ── in review ───────────────────────────────────────────────────────────
NOT_TRACKED_NOTE = "not tracked for"


def set_aside_untracked(decisions: Sequence, groups: Sequence[Group],
                        facts: Dict[str, PartFacts], csv=None, schema=None) -> int:
    """A value of a field no group of its part tracks is set aside (N, not
    asked about): 'not tracked for <group>'. A part in no group, or in one
    that tracks the field, keeps it. Returns how many."""
    if not groups:
        return 0
    member_of: Dict[str, List[Group]] = {}
    for g in groups:
        for p in members(g, facts, csv):
            member_of.setdefault(p.lower(), []).append(g)
    n = 0
    for d in decisions:
        o = d.obs
        mine = member_of.get(str(o.part).lower()) if o.part else None
        if not mine or d.alternate:
            continue
        base = (schema.inspector_base(o.field) if schema is not None else None) or o.field
        if all(base in g.off for g in mine):
            d.accept = False
            d.alternate = f"{NOT_TRACKED_NOTE} {', '.join(g.name for g in mine)}"
            n += 1
    return n


# ── the workbooks ───────────────────────────────────────────────────────
def as_yes_no(value: Any, rule: str) -> str:
    """A Yes/No field's value: blank is No; any value is Yes, or only the
    ones listed in ``rule`` ('pass, ok, y')."""
    v = str(value or "").strip()
    if not v:
        return "No"
    if not rule.strip():
        return "Yes"
    yes = {x.strip().lower() for x in re.split(r"[,;|]", rule) if x.strip()}
    return "Yes" if v.lower() in yes else "No"


_BAD_TITLE = re.compile(r"[\[\]:*?/\\]")


def _title(part: str, sheet: str, several: bool, used: Set[str]) -> str:
    base = _BAD_TITLE.sub("_", f"{part} {sheet}" if several else part).strip() or "part"
    base = base[:31]
    title, n = base, 2
    while title.lower() in used:
        tail = f" ({n})"
        title, n = base[:31 - len(tail)] + tail, n + 1
    used.add(title.lower())
    return title


def write_workbook(csv, parts: Sequence[str], out_path, off: Sequence[str] = (),
                   yes_no: Optional[Dict[str, str]] = None,
                   ledger: Optional[ParentCSV] = None) -> Optional[str]:
    """One workbook: the template filled in once per part, a sheet each (a
    template of several sheets: '<part> <sheet>'), in numerical order.
    Fields in ``off`` are left out; ``yes_no`` fields are written Yes/No.
    Never writes over a file the Ledger did not write. Atomic. None when
    there are no parts."""
    import openpyxl
    from sample_ledger import TemplateSchema, schema_path_for
    out = Path(out_path)
    if out.exists() and not is_filled_template(out):
        raise LedgerError(f"{out.name} is there already and the Ledger did not write it: "
                          "move it, or name the group otherwise")
    schema = TemplateSchema.load(schema_path_for(csv))
    template = Path(schema.template_path)
    if out.resolve() == template.resolve():
        raise LedgerError("refusing to write over the template itself")
    ledger = ledger or ParentCSV(csv)
    parts = sorted(dict.fromkeys(parts), key=sample_sort_key)
    if not parts:
        return None
    yes_no = yes_no or {}
    off_set = set(off)
    wb = openpyxl.load_workbook(template)
    originals = list(wb.worksheets)
    several = len(originals) > 1
    used: Set[str] = set()
    try:
        for part in parts:
            rows = ledger.rows_for(part)
            sheets = {}
            for ws in originals:
                copy = wb.copy_worksheet(ws)
                copy.title = _title(part, ws.title, several, used)
                sheets[ws.title] = copy
            first = sheets[originals[0].title]

            def put(f, item: str, value) -> None:
                if f.name in off_set:
                    return
                if f.name in yes_no:
                    value = as_yes_no(value, yes_no[f.name])
                value = xl_text(value)
                if value in (None, ""):
                    return
                ws = sheets.get(f.sheet, first)
                cell = f.cells.get(item)
                if cell:
                    ws[cell].value = _typed(value)
            if rows:
                for f in schema.fields:
                    if not f.per_item:
                        put(f, "", rows[0].get(f.name))
                for row in rows:
                    item = row.get(schema.item_field, "")
                    for f in schema.fields:
                        if f.per_item and item in f.cells:
                            put(f, item, row.get(f.name))
            elif yes_no:                              # no values: every Yes/No is No
                for f in schema.fields:
                    if f.name in yes_no and not f.per_item:
                        put(f, "", "")
        for ws in originals:
            wb.remove(ws)
        try:
            wb.properties.keywords = FILLED_MARK
        except Exception:                                   # noqa: BLE001
            pass
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".writing" + out.suffix)
        wb.save(tmp)
    finally:
        wb.close()
    os.replace(tmp, out)
    return str(out)


def output_dir(csv) -> Path:
    """Where the workbooks go: '<csv>_workbooks' beside the parent CSV - a
    folder of their own, not among Apply's one-file-per-part templates."""
    return Path(csv).with_name(Path(csv).stem + "_workbooks")


def _file_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" .") or "group"


def write_all(csv, progress: Optional[Callable[[str], None]] = None) -> List[str]:
    """The full workbook ('<template> - all parts.xlsx') and one per group
    ('<template> - <group>.xlsx'), from the parent CSV. A group with no
    parts writes nothing."""
    from sample_ledger import TemplateSchema, schema_path_for
    ledger = ParentCSV(csv)
    if ledger.schema is None:
        return []
    stem = Path(TemplateSchema.load(schema_path_for(csv)).template_path).stem
    folder = output_dir(csv)
    facts = facts_from_ledger(ledger)
    out = []
    say = progress or (lambda _m: None)
    say(f"writing the full workbook ({len(facts)} parts)")
    full = write_workbook(csv, list(facts), folder / f"{_file_name(stem)} - {ALL_PARTS}.xlsx",
                          ledger=ledger)
    if full:
        out.append(full)
    for g in load_groups(csv):
        parts = members(g, facts, csv)
        say(f"writing the {g.name} workbook ({len(parts)} parts)")
        p = write_workbook(csv, parts, folder / f"{_file_name(stem)} - {_file_name(g.name)}.xlsx",
                           off=g.off, yes_no=g.yes_no, ledger=ledger)
        if p:
            out.append(p)
    return out
