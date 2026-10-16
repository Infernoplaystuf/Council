"""
source_extract.py — pull values out of source workbooks of any layout and
line them up with the template's fields, for review, then into the parent CSV
and filled copies of the template.

Step two of the spreadsheet-ingestion pipeline (step one is sample_ledger.py).

    python source_extract.py extract  parent.csv  a.xlsx b.xlsx ...  -o review.xlsx
        -> review.xlsx: every value found, where it came from, how sure the
           match is, and an Accept column; labels it could not place, with a
           "Map to" dropdown; template fields nothing filled; embedded images
    python source_extract.py apply  review.xlsx  [--fill-dir out/]
        -> mappings you chose are remembered (parent.aliases.json), sources
           are read again with them, accepted values go into the parent CSV,
           and --fill-dir writes one filled template per part

EVERYTHING STAYS ON THIS MACHINE
--------------------------------
Workbooks are read with openpyxl. Images are read by a local OCR engine or a
local Ollama vision model at localhost when one is installed, and by nothing
otherwise — they are then saved for you to read. No source value is sent to
any model: the optional --llm mapping suggestion sends only unmatched LABELS
to a local Ollama text model.

LAYOUTS RECOGNISED IN A SOURCE SHEET
------------------------------------
  * ITEM GRID — a row of 3+ item codes (1A, 2A ... or "Flaw 1A", "A-1")
    with measurement labels down the side: the template's own layout.
  * TABLE — a header row of 2+ labels that match fields, rows of values
    under it. A column of item codes makes it one row per item ("flaws down
    the side"); a Part Number column makes it one row per part; otherwise
    each row is one run of a repeating block (Concealment 1, 2 ...) or, for
    a single row, the part's own values.
  * LABEL / VALUE — a matched label with its value to the right (or below),
    or "Label: value" in one cell.

Which part a value belongs to: a Part Number found on that sheet, else a
sheet named like a part ("Part 101", "101"), else the one Part Number found
in the workbook, else a number in the file name (flagged), else --part.

HOW A LABEL IS MATCHED TO A FIELD
---------------------------------
Your remembered mappings win outright. Otherwise the label and each field's
name are normalised (units stripped, EC -> eddy current, P/N -> part number,
...) and compared by spelling and by shared words, with a bonus when the
sheet or a nearby heading names the field's section ("NDE", "Visual"). A
clear best match at >= 0.85 is pre-accepted; 0.6-0.85, or two fields nearly
tied, is shown with its best guess and left for you to accept.

A value in a different unit from the field (mm for um, inch for mm) is
converted when both units are known lengths, angles or times, and the note
says so. Values read from images are never pre-accepted.
"""
from __future__ import annotations

import base64
import difflib
import io
import json
import re
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sample_ledger import (LedgerError, ParentCSV, TemplateField, TemplateSchema,
                           _text, fill_template, normalize_label, sample_sort_key,
                           schema_path_for)

ACCEPT_AT = 0.85
SUGGEST_AT = 0.6
TIE_GAP = 0.04
IMAGE_PENALTY = 0.8
SECTION_BONUS = 0.1
OLLAMA = "http://localhost:11434"

# ── label normalisation ─────────────────────────────────────────────────
_ABBREV = {
    "ec": "eddy current", "pn": "part number", "p n": "part number",
    "part no": "part number", "part num": "part number", "part": "part number",
    "freq": "frequency", "pwr": "power", "spd": "speed", "len": "length",
    "lgth": "length", "orient": "orientation", "ori": "orientation",
    "insp": "inspector", "init": "initials", "inits": "initials",
    "temp": "temperature", "no": "number", "num": "number", "qty": "amount",
    "amt": "amount", "amp": "amplitude", "vis": "visual", "conc": "conceal",
    "concealment": "conceal", "pre concealment": "pre conceal",
    "post concealment": "post conceal", "mid concealment": "mid conceal",
    "response": "", "repsonse": "", "reading": "", "value": "", "measured": "",
}
_ABBREV_RE = re.compile(r"\b(" + "|".join(
    re.escape(k) for k in sorted(_ABBREV, key=len, reverse=True)) + r")\b")
_STOP = {"the", "of", "a", "an", "at", "to", "for", "and", "per"}
UNITS = {   # unit -> (dimension, factor to SI)
    "um": ("len", 1e-6), "µm": ("len", 1e-6), "μm": ("len", 1e-6),
    "micron": ("len", 1e-6), "microns": ("len", 1e-6), "mm": ("len", 1e-3),
    "cm": ("len", 1e-2), "m": ("len", 1.0), "in": ("len", 0.0254),
    "inch": ("len", 0.0254), "inches": ("len", 0.0254), '"': ("len", 0.0254),
    "mil": ("len", 2.54e-5), "mils": ("len", 2.54e-5),
    "deg": ("ang", 1.0), "degrees": ("ang", 1.0), "°": ("ang", 1.0),
    "rad": ("ang", 57.29577951308232),
    "s": ("time", 1.0), "sec": ("time", 1.0), "min": ("time", 60.0),
    "mins": ("time", 60.0), "h": ("time", 3600.0), "hr": ("time", 3600.0),
    "hrs": ("time", 3600.0),
    "mm/s": ("speed", 1e-3), "m/s": ("speed", 1.0), "in/s": ("speed", 0.0254),
    "w": ("power", 1.0), "kw": ("power", 1e3),
    "hz": ("freq", 1.0), "khz": ("freq", 1e3), "mhz": ("freq", 1e6),
}
_UNIT_PAREN = re.compile(r"[\(\[]\s*([^\)\]]{1,12})\s*[\)\]]")


def split_unit(label: str) -> Tuple[str, str]:
    """'Flaw Length (mm)' -> ('Flaw Length', 'mm'); 'FOD um' -> ('FOD', 'um')."""
    s = " ".join(str(label).split())
    m = _UNIT_PAREN.search(s)
    if m and m.group(1).strip().lower() in UNITS:
        return (s[:m.start()] + s[m.end():]).strip(" :-"), m.group(1).strip()
    words = s.rstrip(" :").split()
    if len(words) > 1 and words[-1].lower() in UNITS and words[-1].lower() not in ("m", "s", "h", "in"):
        return " ".join(words[:-1]), words[-1]
    return s, (m.group(1).strip() if m else "")


def tokens(text: str) -> List[str]:
    """Normalised words of a label: units and block letters out, short forms
    spelt out."""
    base, _ = split_unit(text)
    base = re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", base)    # OCR's "PartNumber"
    s = re.sub(r"p\s*/\s*n", " pn ", base.lower())
    s = re.sub(r"[#]", " number ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = _ABBREV_RE.sub(lambda m: _ABBREV[m.group(1)], s)   # one pass: no re-expansion
    out: List[str] = []
    for w in s.split():
        if w in _STOP or (len(w) == 1 and w.isalpha()) or (out and out[-1] == w):
            continue                     # "Part #" -> part number number -> part number
        out.append(w)
    return out


def _unit_factor(src: str, dst: str) -> Optional[float]:
    a, b = UNITS.get(src.strip().lower()), UNITS.get(dst.strip().lower())
    if not a or not b or a[0] != b[0]:
        return None
    return a[1] / b[1]


# ── item codes ──────────────────────────────────────────────────────────
class ItemCodes:
    """'Flaw 1A', '1-A', 'A1', '#1a' -> '1A' when 1A is a template item."""

    def __init__(self, items: Sequence[str]):
        self.items = list(items)
        self.lookup = {i.lower(): i for i in items}

    def canon(self, value) -> Optional[str]:
        if value is None or not self.items:
            return None
        s = str(value).strip()
        if not s or len(s) > 16:
            return None
        if s.lower() in self.lookup:
            return self.lookup[s.lower()]
        s2 = re.sub(r"(?i)^(flaw|defect|feature|notch|site|item)\s*(no\.?|#)?\s*", "", s)
        s2 = re.sub(r"[\s#_\-.]", "", s2)
        if s2.lower() in self.lookup:
            return self.lookup[s2.lower()]
        m = re.fullmatch(r"([A-Za-z])(\d+)", s2)          # A1 -> 1A
        if m and f"{m.group(2)}{m.group(1)}".lower() in self.lookup:
            return self.lookup[f"{m.group(2)}{m.group(1)}".lower()]
        return None


# ── the field matcher ───────────────────────────────────────────────────
@dataclass
class Target:
    """What a source label can map to. A repeat group ("Concealment: Media")
    stands for Concealment 1..n: Media, resolved by row order."""
    name: str                        # field name, or repeat-group name
    section: str
    text: str                        # what is compared against
    fields: List[str]                # 1 field, or the runs of a repeat group
    per_item: bool = False
    unit: str = ""
    is_id: bool = False


@dataclass
class Match:
    target: Optional[Target]
    score: float
    guesses: List[Tuple[str, float]] = field(default_factory=list)
    via: str = ""                    # alias | match | ambiguous | none

    @property
    def sure(self) -> bool:
        return self.target is not None and self.via in ("alias", "match") \
            and self.score >= ACCEPT_AT


class Matcher:
    def __init__(self, schema: TemplateSchema, aliases: Optional[Dict[str, str]] = None,
                 ignore: Iterable[str] = ()):
        self.schema = schema
        self.aliases = {normalize_label(k): v for k, v in (aliases or {}).items()}
        self.ignore = {normalize_label(x) for x in ignore}
        self.targets: List[Target] = []
        groups: Dict[str, Target] = {}
        for f in schema.fields:
            plain = f.name.split(": ", 1)[1] if f.section and f.name.startswith(f.section + ": ") else f.name
            if f.layout == "repeat":
                gname = f"{f.section}: {_strip_unit(f.label)}"
                g = groups.get(gname)
                if g is None:
                    g = groups[gname] = Target(gname, f.section, _strip_unit(f.label), [],
                                               unit=f.unit)
                    self.targets.append(g)
                g.fields.append(f.name)
                continue
            if re.match(r"^.+ \d+: ", plain):           # "Concealment 1: Media" is a run
                plain = plain.split(": ", 1)[1]
            self.targets.append(Target(
                f.name, f.section, plain.replace(" — ", " "), [f.name],
                per_item=f.per_item, unit=f.unit or split_unit(f.label)[1],
                is_id=(f.name == schema.id_field)))
        self.by_name = {t.name: t for t in self.targets}
        for t in self.targets:
            for fn in t.fields:
                self.by_name.setdefault(fn, t)
        self._tok = {t.name: tokens(t.text) for t in self.targets}
        self._sec = {t.name: set(tokens(t.section)) for t in self.targets}

    def is_generic(self, label: str) -> bool:
        """A one-word label shared by 3+ fields ("Date", "Notes")."""
        s = set(tokens(label))
        return len(s) == 1 and sum(1 for t in self.targets
                                   if s <= set(self._tok[t.name])) >= 3

    def is_item_header(self, label: str) -> bool:
        if not self.schema.item_field:
            return False
        t = tokens(label)
        return t in (tokens(self.schema.item_field), ["flaw"], ["flaw", "id"],
                     ["defect"], ["defect", "number"], ["feature"], ["item"])

    def match(self, label, context: Sequence[str] = (), per_item: Optional[bool] = None) -> Match:
        """per_item: True inside an item grid/table, False outside, None = any."""
        if not isinstance(label, str):
            return Match(None, 0.0, via="none")
        key = normalize_label(label)
        if not key or key in self.ignore:
            return Match(None, 0.0, via="none")
        if key in self.aliases:
            t = self.by_name.get(self.aliases[key])
            if t is not None:
                return Match(t, 1.0, via="alias")
        src = tokens(label)
        if not src:
            return Match(None, 0.0, via="none")
        ctx = set()
        if per_item and self.schema.item_field:   # in a flaw table, "flaw" is implied
            ctx.update(tokens(self.schema.item_field))
        for c in context:
            ctx.update(tokens(c))
        joined = " ".join(src)
        sset = set(src)
        snums = {w for w in src if w.isdigit()}
        scored = []
        for t in self.targets:
            if per_item is not None and t.per_item != per_item and not t.is_id:
                continue
            ft = self._tok[t.name]
            if not ft:
                continue
            fset = set(ft)
            s = difflib.SequenceMatcher(None, joined, " ".join(ft)).ratio()
            s = max(s, len(sset & fset) / max(len(sset), len(fset)))
            small, large = sorted((len(sset), len(fset)))
            if sset <= fset or fset <= sset:            # one label inside the other
                s = max(s, 0.6 + 0.4 * small / large)
            fnums = {w for w in ft if w.isdigit()}
            if snums and fnums and snums != fnums:      # Inspector 2 is not Inspector 1
                s *= 0.5
            if ctx & (self._sec[t.name] | (fset - sset)):
                s += SECTION_BONUS
            scored.append((s, t))
        if not scored:
            return Match(None, 0.0, via="none")
        scored.sort(key=lambda x: -x[0])
        raw, best = scored[0]
        best_s = raw
        # a one-word label shared by many fields ("Date", "Notes") is never sure
        if self.is_generic(label):
            best_s = min(best_s, ACCEPT_AT - 0.05)
        guesses = [(t.name, round(min(s, 1.0), 2)) for s, t in scored[:3]]
        shown = round(min(best_s, 1.0), 2)
        if best_s < SUGGEST_AT:
            return Match(None, shown, guesses, via="none")
        if len(scored) > 1 and raw - scored[1][0] < TIE_GAP:
            return Match(best, shown, guesses, via="ambiguous")
        return Match(best, shown, guesses, via="match")


def _strip_unit(label: str) -> str:
    return re.sub(r"\s*[:*]+\s*$", "", split_unit(label)[0])


# ── source grids ────────────────────────────────────────────────────────
@dataclass
class Grid:
    """One sheet, or one image read as a table."""
    file: str
    sheet: str
    cells: Dict[Tuple[int, int], object]
    from_image: str = ""             # image file name when read from an image

    def text(self, r, c) -> Optional[str]:
        v = self.cells.get((r, c))
        return v.strip() if isinstance(v, str) and v.strip() else None

    def where(self, r, c) -> str:
        from openpyxl.utils import get_column_letter
        place = f"{get_column_letter(c)}{r}"
        if self.from_image:
            return f"{self.file} › {self.sheet} › image {self.from_image} (row {r}, col {c})"
        return f"{self.file} › {self.sheet}!{place}"


@dataclass
class SourceImage:
    file: str
    sheet: str
    anchor: str
    data: bytes
    ext: str
    saved_as: str = ""


def load_workbook_grids(path) -> Tuple[List[Grid], List[SourceImage]]:
    import openpyxl
    from openpyxl.utils import get_column_letter
    path = Path(path)
    if path.suffix.lower() == ".xls":
        raise LedgerError(f"{path.name} is the old .xls format; open it in Excel and "
                          "Save As .xlsx, then add that file")
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:
        raise LedgerError(f"could not open {path.name}: {e}") from e
    grids, images = [], []
    for ws in wb.worksheets:
        cells = {}
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                if isinstance(v, str) and v.strip().upper() in _EXCEL_ERRORS:
                    continue
                cells[(c.row, c.column)] = v
        # a merged label reads the same from every cell it covers
        for rng in ws.merged_cells.ranges:
            top = cells.get((rng.min_row, rng.min_col))
            if top is None:
                continue
            for r in range(rng.min_row, rng.max_row + 1):
                for cc in range(rng.min_col, rng.max_col + 1):
                    if (r, cc) != (rng.min_row, rng.min_col) and isinstance(top, str):
                        cells.setdefault((r, cc), _Merged(top))
        grids.append(Grid(path.name, ws.title, cells))
        for img in getattr(ws, "_images", []):
            try:
                data = img._data()
                fr = img.anchor._from
                anchor = f"{get_column_letter(fr.col + 1)}{fr.row + 1}"
            except Exception:
                continue
            images.append(SourceImage(path.name, ws.title, anchor, data,
                                      (getattr(img, "format", None) or "png").lower()))
    wb.close()
    return grids, images


_EXCEL_ERRORS = {"#N/A", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#NULL!"}


class _Merged(str):
    """Text repeated into a merged range: a label everywhere, a value nowhere."""


# ── observations ────────────────────────────────────────────────────────
@dataclass
class Obs:
    part: str
    item: str
    field: str                 # schema field name ("" when unmatched)
    value: str
    score: float
    via: str                   # alias | match | ambiguous | image | none
    label: str                 # source label as written
    where: str
    note: str = ""
    guesses: List[Tuple[str, float]] = field(default_factory=list)
    part_how: str = ""         # how the part number was decided
    from_image: bool = False

    @property
    def key(self) -> Tuple[str, str, str, str]:
        return (self.part, self.item, self.field, self.where)


class _Extractor:
    def __init__(self, grid: Grid, m: Matcher, codes: ItemCodes, part_hint: str):
        self.g, self.m, self.codes = grid, m, codes
        self.part = part_hint
        self.used = set()
        self.out: List[Obs] = []
        self.unmatched: List[Obs] = []

    # context = sheet name + heading-like rows above
    def context(self, r: int) -> List[str]:
        ctx = [self.g.sheet]
        for rr in range(max(1, r - 25), r):
            row = [(c, v) for (r2, c), v in self.g.cells.items() if r2 == rr]
            if len(row) == 1 and isinstance(row[0][1], str):
                ctx.append(row[0][1])
        return ctx

    def emit(self, part, item, label, r, c, value, match: Match, field_name=None,
             vr=None, vc=None):
        txt = _text(value)
        if txt == "" or isinstance(value, _Merged):
            return None
        where = self.g.where(vr or r, vc or c)
        fname = field_name or (match.target.fields[0] if match.target else "")
        note = ""
        if match.target and match.target.unit:
            src_unit = split_unit(label)[1]
            if src_unit and src_unit.lower() != match.target.unit.lower():
                f = _unit_factor(src_unit, match.target.unit)
                num = _num(txt)
                if f is not None and num is not None:
                    txt = _fmt(num * f)
                    note = f"converted {src_unit} -> {match.target.unit}"
                else:
                    note = f"unit {src_unit!r} vs template {match.target.unit!r}: not converted"
        score = match.score * (IMAGE_PENALTY if self.g.from_image else 1.0)
        o = Obs(part=part or "", item=item or "", field=fname if match.target else "",
                value=txt, score=round(score, 2),
                via="image" if self.g.from_image and match.target else match.via,
                label=str(label), where=where, note=note, guesses=match.guesses,
                from_image=bool(self.g.from_image))
        (self.out if match.target else self.unmatched).append(o)
        return o

    def run(self) -> None:
        self.find_part()
        self.item_grids()
        self.tables()
        self.forms()

    # part number on this sheet
    def find_part(self) -> None:
        for (r, c), v in sorted(self.g.cells.items()):
            if not isinstance(v, str):
                continue
            label, inline = _split_inline(v)
            mt = self.m.match(label, per_item=False)
            if mt.target and mt.target.is_id and mt.score >= SUGGEST_AT:
                val = inline if inline else self._value_near(r, c)
                if val and not self.m.match(val).target:
                    if not self._is_table_header(r, c):
                        self.part = _text(val)
                        self.part_how = f"{self.m.schema.id_field} on {self.g.sheet}" + (
                            f", read from image {self.g.from_image}" if self.g.from_image else "")
                        return
        sid = _part_from_name(self.g.sheet)
        if sid:
            self.part = sid
            self.part_how = f"sheet name {self.g.sheet!r}"
            return
        self.part_how = ""

    def _is_table_header(self, r, c) -> bool:
        hits = sum(1 for (r2, c2), v in self.g.cells.items()
                   if r2 == r and c2 != c and isinstance(v, str)
                   and (self.m.match(v).score >= SUGGEST_AT or self.m.is_item_header(v)))
        return hits >= 1 and self.g.cells.get((r, c + 1)) is not None \
            and isinstance(self.g.cells.get((r, c + 1)), str) \
            and self.m.match(self.g.cells[(r, c + 1)]).target is not None

    def _value_near(self, r, c):
        for dc in (1, 2, 3):
            v = self.g.cells.get((r, c + dc))
            if v is not None and not isinstance(v, _Merged):
                return v
        v = self.g.cells.get((r + 1, c))
        return v if not isinstance(v, _Merged) else None

    # ITEM GRID: item codes across a row, labels down a column
    def item_grids(self) -> None:
        rows: Dict[int, List[Tuple[int, str]]] = defaultdict(list)
        for (r, c), v in self.g.cells.items():
            it = self.codes.canon(v)
            if it:
                rows[r].append((c, it))
        for r, hits in sorted(rows.items()):
            if len(hits) < 3:
                continue
            hits.sort()
            first = hits[0][0]
            label_col = next((cc for cc in range(first - 1, 0, -1)
                              if any(self.g.text(rr, cc) for rr in range(r + 1, r + 12))), None)
            if label_col is None:
                continue
            for c, _ in hits:
                self.used.add((r, c))
            self.used.add((r, label_col))            # the grid's own "Flaw #" cell
            ctx = self.context(r)
            rr = r + 1
            blanks = 0
            while blanks < 2 and rr < r + 60:
                label = self.g.text(rr, label_col)
                if label is None:
                    blanks += 1
                    rr += 1
                    continue
                if sum(1 for c, _ in hits if self.codes.canon(self.g.cells.get((rr, c)))) >= 3:
                    break                          # the next grid's header
                blanks = 0
                self.used.add((rr, label_col))
                mt = self.m.match(label, ctx, per_item=True)
                for c, it in hits:
                    if (rr, c) in self.g.cells:
                        self.emit(self.part, it, label, rr, label_col,
                                  self.g.cells[(rr, c)], mt, vr=rr, vc=c)
                        self.used.add((rr, c))
                rr += 1

    # TABLE: header row + rows
    def tables(self) -> None:
        by_row: Dict[int, List[int]] = defaultdict(list)
        for (r, c), v in self.g.cells.items():
            if isinstance(v, str) and (r, c) not in self.used:
                by_row[r].append(c)
        for r in sorted(by_row):
            cols = sorted(c for c in by_row[r] if (r, c) not in self.used)
            if len(cols) < 2:
                continue
            ctx = self.context(r)
            heads = {}
            item_col = id_col = None
            for c in cols:
                v = self.g.text(r, c)
                if self.m.is_item_header(v):
                    item_col = c
                    continue
                mt = self.m.match(v, ctx)
                if mt.target and mt.target.is_id and mt.score >= SUGGEST_AT:
                    id_col = c
                    continue
                heads[c] = (v, mt)
            matched = [c for c, (_, mt) in heads.items() if mt.target]
            if len(matched) + (item_col is not None) + (id_col is not None) < 2:
                continue
            if item_col is None:                     # a column of item codes?
                for c in cols:
                    vals = [self.g.cells.get((rr, c)) for rr in range(r + 1, r + 6)]
                    if sum(1 for v in vals if self.codes.canon(v)) >= 2:
                        item_col = c
                        heads.pop(c, None)
                        break
            data_rows = []
            rr = r + 1
            while rr < r + 500:
                if not any((rr, c) in self.g.cells for c in cols):
                    break
                data_rows.append(rr)
                rr += 1
            if not data_rows:
                continue
            # values under a header must not be more labels
            filled = sum(1 for c in heads for d in data_rows[:3] if (d, c) in self.g.cells)
            if heads and filled == 0:
                continue
            for c in cols:
                self.used.add((r, c))
            per_item = item_col is not None
            final = {c: (label, self.m.match(label, ctx, per_item=per_item))
                     for c, (label, _) in heads.items()}
            for i, d in enumerate(data_rows):
                part = _text(self.g.cells.get((d, id_col))) if id_col else self.part
                item = self.codes.canon(self.g.cells.get((d, item_col))) if per_item else ""
                if per_item and not item:
                    continue
                for c, (label, mt) in final.items():
                    if (d, c) not in self.g.cells:
                        continue
                    fname = None
                    if mt.target and len(mt.target.fields) > 1:   # repeat group
                        if i >= len(mt.target.fields):
                            continue
                        fname = mt.target.fields[i]
                    o = self.emit(part, item, label, r, c, self.g.cells[(d, c)], mt,
                                  field_name=fname, vr=d, vc=c)
                    if o is not None and id_col:
                        o.part_how = f"{self.m.schema.id_field} column on {self.g.sheet}"
                    self.used.add((d, c))
                if id_col:
                    self.used.add((d, id_col))
                if item_col:
                    self.used.add((d, item_col))

    # LABEL / VALUE
    def forms(self) -> None:
        for (r, c), v in sorted(self.g.cells.items()):
            if (r, c) in self.used or not isinstance(v, str) or isinstance(v, _Merged):
                continue
            label, inline = _split_inline(v)
            if len(label) > 60:
                continue
            ctx = self.context(r)
            mt = self.m.match(label, ctx, per_item=False)
            if mt.target and mt.target.is_id:
                self.used.add((r, c))
                continue
            if inline:
                vr, vc, val = r, c, inline
            else:
                vr = vc = val = None
                for dc in (1, 2, 3):
                    x = self.g.cells.get((r, c + dc))
                    if x is None or isinstance(x, _Merged):
                        continue
                    if isinstance(x, str) and self.m.match(x, per_item=False).target \
                            and not _num(x):
                        break
                    vr, vc, val = r, c + dc, x
                    break
                if val is None:
                    continue
            if not mt.target and _num(label) is not None:
                continue                         # a stray number, not a label
            fname = mt.target.fields[0] if mt.target else None
            self.used.add((r, c))
            if vr is not None:
                self.used.add((vr, vc))
            self.emit(self.part, "", label, r, c, val, mt, field_name=fname, vr=vr, vc=vc)


def _split_inline(v: str) -> Tuple[str, str]:
    """'Operator: AK' -> ('Operator', 'AK'); OCR's 'PartNumber:2041' too.
    Times like 10:30 stay whole: the label must have a letter in it."""
    m = re.match(r"^\s*([^:=]{1,60}?)\s*[:=]\s*(.+)$", v)
    if m and re.search(r"[A-Za-z]", m.group(1)) and not m.group(2).startswith(("/", "\\")):
        return m.group(1), m.group(2).strip()
    return v.strip(), ""


def _part_from_name(name: str) -> str:
    if re.fullmatch(r"(?i)sheet\s*\d+", name.strip()):
        return ""
    m = re.fullmatch(r"(?i)\s*(?:part|sample|specimen|p/?n|s/?n)\s*(?:no\.?|#)?\s*[-_ ]?\s*([A-Za-z]*\d[\w\-]*)\s*", name)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{1,8}", name.strip()):
        return name.strip()
    return ""


def _num(s) -> Optional[float]:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _fmt(x: float) -> str:
    s = f"{x:.6g}"
    return s if "e" not in s else f"{x:.6f}".rstrip("0").rstrip(".")


# ── images ──────────────────────────────────────────────────────────────
ImageReader = Callable[[bytes], Optional[List[List[str]]]]   # -> rows of cells


def boxes_to_rows(boxes: List[Tuple[float, float, float, float, str]]) -> List[List[str]]:
    """OCR boxes (x0, y0, x1, y1, text) -> table rows, columns aligned by x.

    Rows: boxes whose vertical centres are within 0.6 x the median box height.
    Columns: left edges clustered across all rows, so a blank cell stays blank."""
    if not boxes:
        return []
    hs = sorted(b[3] - b[1] for b in boxes)
    h = hs[len(hs) // 2]
    tol = max(h * 0.6, 1.0)
    rows: List[List[Tuple[float, float, float, float, str]]] = []
    for b in sorted(boxes, key=lambda b: (b[1] + b[3]) / 2):
        cy = (b[1] + b[3]) / 2
        if rows and abs(cy - sum((x[1] + x[3]) / 2 for x in rows[-1]) / len(rows[-1])) <= tol:
            rows[-1].append(b)
        else:
            rows.append([b])
    # one line of text OCR'd as two touching / overlapping boxes is one cell;
    # a real table cell boundary leaves a wider gap than a fraction of a letter
    for i, row in enumerate(rows):
        row.sort(key=lambda b: b[0])
        joined = [row[0]]
        for b in row[1:]:
            a = joined[-1]
            gap = b[0] - a[2]
            if gap < h * 0.3:
                ta, tb = a[4], b[4]
                if gap < 0 and ta and tb and ta[-1] == tb[0]:
                    tb = tb[1:]                 # the overlap was read twice
                sep = "" if gap < h * 0.15 else " "
                joined[-1] = (a[0], min(a[1], b[1]), b[2], max(a[3], b[3]), ta + sep + tb)
            else:
                joined.append(b)
        rows[i] = joined
    boxes = [b for row in rows for b in row]
    widths = sorted(b[2] - b[0] for b in boxes)
    ctol = max(widths[len(widths) // 2] * 0.5, 4.0)
    lefts: List[float] = []
    for x in sorted(b[0] for b in boxes):
        if not lefts or x - lefts[-1] > ctol:
            lefts.append(x)
    out = []
    for row in rows:
        cells = [""] * len(lefts)
        for b in sorted(row, key=lambda b: b[0]):
            ci = min(range(len(lefts)), key=lambda i: abs(lefts[i] - b[0]))
            cells[ci] = (cells[ci] + " " + b[4]).strip()
        out.append(cells)
    return out


def rapidocr_reader() -> Optional[ImageReader]:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return None
    import os
    # onnxruntime's default spreads over every core and runs ~2.5x SLOWER for
    # small images on a many-core machine (measured: 32 s -> 12 s for three
    # table screenshots on 28 cores). Detection keeps its full size, so small
    # text in a big screenshot is still found.
    threads = max(1, min(8, (os.cpu_count() or 4)))
    engine = RapidOCR(intra_op_num_threads=threads, inter_op_num_threads=1)

    def read(data: bytes):
        result, _ = engine(data)
        if not result:
            return None
        boxes = []
        for pts, text, _score in result:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            boxes.append((min(xs), min(ys), max(xs), max(ys), text))
        return boxes_to_rows(boxes)
    read.name = "RapidOCR (local)"
    return read


def _ollama(path: str, payload: Optional[dict] = None, timeout=10):
    req = urllib.request.Request(OLLAMA + path, method="POST" if payload else "GET",
                                 data=json.dumps(payload).encode() if payload else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ollama_vision_model() -> Optional[str]:
    """The first installed Ollama model that reports the vision capability."""
    try:
        names = [m["name"] for m in _ollama("/api/tags").get("models", [])]
    except Exception:
        return None
    for n in names:
        try:
            caps = _ollama("/api/show", {"model": n}).get("capabilities", [])
        except Exception:
            continue
        if "vision" in caps:
            return n
    return None


VISION_PROMPT = (
    "Transcribe the text in this image exactly. If it is a table, output it as a "
    "markdown table with the same rows and columns, keeping blank cells blank. "
    "Otherwise output one 'label: value' per line. Do not add, guess or explain "
    "anything. If the image has no text, output NONE.")


def parse_vision_text(text: str) -> Optional[List[List[str]]]:
    """A vision model's markdown table / 'label: value' lines -> rows."""
    text = (text or "").strip()
    if not text or text.upper().startswith("NONE"):
        return None
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or re.fullmatch(r"\|?\s*:?-{2,}.*", line):
            continue
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip("|").split("|")])
        elif ":" in line:
            a, b = line.split(":", 1)
            rows.append([a.strip(" *-"), b.strip()])
    return rows or None


def ollama_vision_reader(model: Optional[str] = None) -> Optional[ImageReader]:
    model = model or ollama_vision_model()
    if not model:
        return None

    def read(data: bytes):
        out = _ollama("/api/chat", {
            "model": model, "stream": False, "options": {"temperature": 0},
            "messages": [{"role": "user", "content": VISION_PROMPT,
                          "images": [base64.b64encode(data).decode()]}]}, timeout=300)
        return parse_vision_text(out.get("message", {}).get("content", ""))
    read.name = f"Ollama {model} (local)"
    return read


def default_image_reader() -> Optional[ImageReader]:
    return rapidocr_reader() or ollama_vision_reader()


def rows_to_grid(rows: List[List[str]], img: SourceImage) -> Grid:
    cells = {}
    for r, row in enumerate(rows, start=1):
        for c, v in enumerate(row, start=1):
            if str(v).strip():
                cells[(r, c)] = str(v).strip()     # as read: "0.030" keeps its zero
    return Grid(img.file, img.sheet, cells,
                from_image=Path(img.saved_as).name if img.saved_as else img.anchor)


# ── a whole run ─────────────────────────────────────────────────────────
@dataclass
class ImageNote:
    file: str
    sheet: str
    anchor: str
    saved_as: str
    reader: str
    text: str
    values: int = 0


@dataclass
class Extraction:
    sources: List[str]
    obs: List[Obs] = field(default_factory=list)
    unmatched: List[Obs] = field(default_factory=list)
    images: List[ImageNote] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


def extract(schema: TemplateSchema, sources: Sequence, aliases=None, ignore=(),
            image_dir=None, image_reader: Optional[ImageReader] = None,
            part: str = "", use_images: bool = True) -> Extraction:
    m = Matcher(schema, aliases, ignore)
    codes = ItemCodes(schema.items)
    run = Extraction([str(s) for s in sources])
    reader = image_reader if image_reader is not None else (
        default_image_reader() if use_images else None)
    for src in sources:
        src = Path(src)
        try:
            grids, images = load_workbook_grids(src)
        except LedgerError as e:
            run.problems.append(str(e))
            continue
        for i, img in enumerate(images):
            if image_dir:
                d = Path(image_dir)
                d.mkdir(parents=True, exist_ok=True)
                p = d / f"{_safe(src.stem)}_{_safe(img.sheet)}_{img.anchor}_{i + 1}.{img.ext}"
                p.write_bytes(img.data)
                img.saved_as = str(p)
            note = ImageNote(img.file, img.sheet, img.anchor, img.saved_as, "", "")
            if reader is not None:
                note.reader = getattr(reader, "name", "image reader")
                try:
                    rows = reader(img.data)
                except Exception as e:
                    rows = None
                    note.text = f"reader failed: {e}"
                if rows:
                    note.text = "\n".join(" | ".join(r) for r in rows)[:2000]
                    grids.append(rows_to_grid(rows, img))
                elif not note.text:
                    note.text = "(no text found)"
            else:
                note.reader = "none installed"
                note.text = "not read: install a local OCR engine or vision model"
            run.images.append(note)

        # pass 1: part numbers per sheet; then a workbook-wide fallback
        exs = [_Extractor(g, m, codes, "") for g in grids]
        for ex in exs:
            ex.find_part()
        found = {ex.part for ex in exs if ex.part}
        if part:
            book_part, how = part, "given on the command line"
        elif len(found) == 1:
            book_part, how = found.pop(), "the one part number in the workbook"
        elif not found:
            book_part = _part_from_filename(src.stem)
            how = f"file name {src.name!r} — check it" if book_part else ""
        else:
            book_part, how = "", ""
        for ex in exs:
            if part:
                ex.part, ex.part_how = part, how
            elif not ex.part:
                ex.part, ex.part_how = book_part, how
            ex.item_grids()
            ex.tables()
            ex.forms()
            for o in ex.out + ex.unmatched:
                o.part_how = o.part_how or ex.part_how
            n = len(ex.out)
            run.obs.extend(ex.out)
            run.unmatched.extend(ex.unmatched)
            if ex.g.from_image:
                for note in run.images:
                    if ex.g.from_image in (Path(note.saved_as).name if note.saved_as else "",
                                           note.anchor):
                        note.values += n
        orphans = sum(1 for ex in exs for o in ex.out if not o.part)
        if orphans:
            run.problems.append(f"{src.name}: {orphans} values have no {schema.id_field} "
                                "— fill the Part column in the review, or re-run with --part")
    return run


def _part_from_filename(stem: str) -> str:
    m = re.search(r"(?i)(?:part|sample|p/?n|s/?n)[\s_\-#]*([A-Za-z]*\d[\w\-]*)", stem)
    if m:
        return m.group(1)
    nums = re.findall(r"(?<![\d.])\d{2,8}(?![\d.])", stem)
    return nums[0] if len(nums) == 1 else ""


def _safe(s: str) -> str:
    return re.sub(r"[^\w\-]+", "_", s)[:40]


# ── merging observations into rows ──────────────────────────────────────
@dataclass
class Decision:
    obs: Obs
    accept: bool
    conflict: str = ""               # other values offered for the same cell


def decide(run: Extraction, schema: TemplateSchema) -> List[Decision]:
    """Pick one value per (part, item, field); a clash between sources is never
    pre-accepted."""
    groups: Dict[Tuple[str, str, str], List[Obs]] = defaultdict(list)
    for o in run.obs:
        groups[(o.part, o.item, o.field)].append(o)
    out = []
    for (part, item, fname), obs in groups.items():
        obs.sort(key=lambda o: -o.score)
        values = Counter(o.value for o in obs)
        clash = len(values) > 1
        firsts: Dict[str, Obs] = {}
        for o in obs:                     # the same value found twice: one row
            if o.value in firsts:
                firsts[o.value].note = _join(firsts[o.value].note, f"also at {o.where}")
                continue
            firsts[o.value] = o
        obs = list(firsts.values())
        for o in obs:
            others = sorted(v for v in values if v != o.value)
            ok = (not clash and o is obs[0] and o.score >= ACCEPT_AT and
                  o.via in ("alias", "match") and bool(part) and not o.from_image
                  and "not converted" not in o.note)
            out.append(Decision(o, ok, ", ".join(others) if clash else ""))
    item_rank = {it: i for i, it in enumerate(schema.items)}
    order = {f.name: i for i, f in enumerate(schema.fields)}
    out.sort(key=lambda d: (sample_sort_key(d.obs.part), item_rank.get(d.obs.item, -1),
                            order.get(d.obs.field, 1e9), -d.obs.score))
    return out


def _join(a: str, b: str) -> str:
    return f"{a}; {b}" if a else b


def rows_from(decisions: Iterable[Decision], schema: TemplateSchema) -> List[Dict[str, str]]:
    """Accepted values -> parent-CSV rows. A part-level value lands on every
    item row of its part, as the parent CSV keeps it."""
    part_vals: Dict[str, Dict[str, str]] = defaultdict(dict)
    item_vals: Dict[Tuple[str, str], Dict[str, str]] = defaultdict(dict)
    for d in decisions:
        if not d.accept or not d.obs.part or not d.obs.field:
            continue
        f = d.obs
        target = item_vals[(f.part, f.item)] if f.item else part_vals[f.part]
        target.setdefault(f.field, f.value)
    rows = []
    parts = sorted(set(part_vals) | {p for p, _ in item_vals}, key=sample_sort_key)
    for p in parts:
        if not schema.item_field:
            rows.append({schema.id_field: p, **part_vals[p]})
            continue
        for it in schema.items:
            row = {schema.id_field: p, schema.item_field: it, **part_vals[p]}
            row.update(item_vals.get((p, it), {}))
            rows.append(row)
    return rows


# ── the review workbook ─────────────────────────────────────────────────
VAL_HEAD = ["Accept", "Part", "Item", "Field", "Value", "Confidence", "Matched by",
            "Source label", "Source", "Other values offered", "Note", "Part from"]
LAB_HEAD = ["Map to field", "Ignore", "Source label", "Example value", "Count",
            "Best guess", "Source", "Part", "Item"]


def write_review(path, run: Extraction, decisions: List[Decision], schema: TemplateSchema,
                 csv_path: str) -> None:
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = openpyxl.Workbook()
    bold = Font(bold=True)
    amber = PatternFill("solid", fgColor="FFF2CC")
    red = PatternFill("solid", fgColor="F8CBAD")

    info = wb.active
    info.title = "Read me"
    lines = [
        "Review of extracted values",
        "",
        "1. Values: check each row. Accept = Y goes into the parent CSV; blank or N "
        "does not. Rows already Y were clear matches. Amber rows need a decision; red "
        "rows are clashes between sources — accept one value only.",
        "2. Labels to map: labels that matched no field. Pick a field in 'Map to field' "
        "(it is remembered for every future file), or put Y in Ignore.",
        "3. Save this workbook, then run:   python source_extract.py apply \"<this file>\"",
        "",
        f"Parent CSV: {csv_path}",
        f"Made: {datetime.now().isoformat(timespec='seconds')}",
        "Sources:",
    ] + [f"  {s}" for s in run.sources] + ([""] + ["Problems:"] +
                                           [f"  {p}" for p in run.problems] if run.problems else [])
    for i, line in enumerate(lines, start=1):
        info.cell(i, 1, line)
    info["A1"].font = Font(bold=True, size=14)
    info.column_dimensions["A"].width = 120

    fields_ws = wb.create_sheet("Fields")
    for i, f in enumerate(schema.fields, start=1):
        fields_ws.cell(i, 1, f.name)
    fields_ws.sheet_state = "hidden"
    field_list = f"=Fields!$A$1:$A${len(schema.fields)}"

    ws = wb.create_sheet("Values", 1)
    ws.append(VAL_HEAD)
    for d in decisions:
        o = d.obs
        ws.append(["Y" if d.accept else "", o.part, o.item, o.field, o.value, o.score,
                   o.via, o.label, o.where, d.conflict, o.note, o.part_how])
        if d.conflict:
            for c in ws[ws.max_row]:
                c.fill = red
        elif not d.accept:
            for c in ws[ws.max_row]:
                c.fill = amber
    yn = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    ws.add_data_validation(yn)
    yn.add(f"A2:A{max(ws.max_row, 2) + 200}")
    fdv = DataValidation(type="list", formula1=field_list, allow_blank=True)
    ws.add_data_validation(fdv)
    fdv.add(f"D2:D{max(ws.max_row, 2) + 200}")
    _style(ws, bold, [8, 10, 7, 48, 16, 11, 11, 30, 50, 18, 34, 28])

    lab = wb.create_sheet("Labels to map", 2)
    lab.append(LAB_HEAD)
    grouped: Dict[Tuple[str, str], List[Obs]] = defaultdict(list)
    for o in run.unmatched:
        grouped[(normalize_label(o.label), o.where.split("!")[0])].append(o)
    for (_, _), obs in sorted(grouped.items()):
        o = obs[0]
        guess = ", ".join(f"{g} ({s})" for g, s in o.guesses[:3])
        lab.append(["", "", o.label, o.value, len(obs), guess, o.where, o.part, o.item])
    dv2 = DataValidation(type="list", formula1=field_list, allow_blank=True)
    lab.add_data_validation(dv2)
    dv2.add(f"A2:A{max(lab.max_row, 2) + 100}")
    yn2 = DataValidation(type="list", formula1='"Y"', allow_blank=True)
    lab.add_data_validation(yn2)
    yn2.add(f"B2:B{max(lab.max_row, 2) + 100}")
    _style(lab, bold, [44, 8, 34, 18, 7, 60, 50, 10, 7])

    miss = wb.create_sheet("Missing", 3)
    miss.append(["Part", "Field", "Items with no value", "Of"])
    have = defaultdict(set)
    for d in decisions:
        have[(d.obs.part, d.obs.field)].add(d.obs.item)
    parts = sorted({d.obs.part for d in decisions if d.obs.part}, key=sample_sort_key)
    for p in parts:
        for f in schema.fields:
            if f.name == schema.id_field:
                continue
            got = have.get((p, f.name), set())
            if f.per_item:
                gap = [it for it in schema.items if it in f.cells and it not in got]
                if gap:
                    miss.append([p, f.name, _span(gap, schema.items), len(f.cells)])
            elif not got:
                miss.append([p, f.name, "", ""])
    _style(miss, bold, [10, 60, 40, 6])

    im = wb.create_sheet("Images", 4)
    im.append(["File", "Sheet", "Anchor", "Saved as", "Read by", "Values used", "Text read"])
    for n in run.images:
        im.append([n.file, n.sheet, n.anchor, n.saved_as, n.reader, n.values, n.text])
    _style(im, bold, [28, 16, 8, 60, 26, 10, 80])

    meta = wb.create_sheet("_run")
    meta["A1"] = json.dumps({"csv": str(csv_path), "sources": run.sources})
    meta.sheet_state = "hidden"
    wb.active = 1
    _save_wb(wb, Path(path))


def _span(gap: List[str], items: List[str]) -> str:
    return ", ".join(gap) if len(gap) <= 12 else f"{len(gap)} items ({gap[0]} .. {gap[-1]})"


def _style(ws, bold, widths) -> None:
    from openpyxl.utils import get_column_letter
    for c in ws[1]:
        c.font = bold
    ws.freeze_panes = "A2"
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions


def _save_wb(wb, path: Path) -> None:
    try:
        wb.save(path)
    except PermissionError as e:
        raise LedgerError(f"could not write {path.name}: it is probably open in Excel. "
                          "Close it and try again.") from e


# ── aliases (what you taught it) ────────────────────────────────────────
def aliases_path_for(csv_path) -> Path:
    return Path(csv_path).with_suffix(".aliases.json")


def load_aliases(csv_path) -> Tuple[Dict[str, str], List[str]]:
    p = aliases_path_for(csv_path)
    if not p.exists():
        return {}, []
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("map", {}), data.get("ignore", [])


def save_aliases(csv_path, mapping: Dict[str, str], ignore: List[str]) -> None:
    p = aliases_path_for(csv_path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"map": dict(sorted(mapping.items())),
                               "ignore": sorted(set(ignore))}, indent=2), encoding="utf-8")
    tmp.replace(p)


# ── apply a reviewed workbook ───────────────────────────────────────────
@dataclass
class ApplyResult:
    learned: Dict[str, str]
    ignored: List[str]
    accepted: int
    rows: int
    csv_result: str
    filled: List[str]
    warnings: List[str]


def apply_review(review_path, fill_dir=None, image_reader: Optional[ImageReader] = None,
                 use_images: bool = True) -> ApplyResult:
    import openpyxl
    review_path = Path(review_path)
    wb = openpyxl.load_workbook(review_path, data_only=True)
    try:
        meta = json.loads(wb["_run"]["A1"].value)
    except Exception as e:
        raise LedgerError(f"{review_path.name} is not a review workbook from "
                          "source_extract.py (its hidden _run sheet is missing)") from e
    csv_path = meta["csv"]
    ledger = ParentCSV(csv_path)
    schema = ledger.schema
    if schema is None:
        raise LedgerError(f"{csv_path} has no schema beside it; run sample_ledger.py first")
    valid = {f.name for f in schema.fields}
    mapping, ignore = load_aliases(csv_path)
    judge = Matcher(schema)

    learned, ignored = {}, []
    lab = wb["Labels to map"]
    for row in lab.iter_rows(min_row=2, values_only=True):
        to, ign, label = row[0], row[1], row[2]
        if not label:
            continue
        if to and str(to) in valid:
            learned[normalize_label(label)] = str(to)
        elif str(ign or "").strip().upper() == "Y":
            ignored.append(normalize_label(label))
    mapping.update(learned)
    ignore = sorted(set(ignore) | set(ignored))
    if learned or ignored:
        save_aliases(csv_path, mapping, ignore)

    # the reviewer's choices, by where each value came from
    choice: Dict[Tuple[str, str, str], Tuple[bool, str, str, str]] = {}
    for row in wb["Values"].iter_rows(min_row=2, values_only=True):
        acc, part, item, fname, value, score, via, label, where = row[:9]
        if not where:
            continue
        yes = str(acc or "").strip().upper() == "Y"
        choice[(str(where), str(fname or ""), _text(item))] = (
            yes, _text(part), _text(value), str(fname or ""))
        # accepting an unsure match teaches it, like a mapping on Labels to map;
        # a one-word label ("Date") is too generic to remember
        if yes and fname in valid and label and via in ("match", "ambiguous") \
                and not judge.is_generic(str(label)) and (_num(score) or 0) < ACCEPT_AT:
            learned.setdefault(normalize_label(label), str(fname))
    wb.close()
    if learned:
        mapping.update(learned)
        save_aliases(csv_path, mapping, ignore)

    run = extract(schema, meta["sources"], mapping, ignore, image_reader=image_reader,
                  use_images=use_images)
    decisions = decide(run, schema)
    accepted = 0
    for d in decisions:
        o = d.obs
        c = choice.get((o.where, o.field, o.item))
        if c is not None:
            d.accept, part, value, _ = c
            o.part = part or o.part
            o.value = value if value != "" else o.value
        elif normalize_label(o.label) in learned:
            d.accept = bool(o.part) and not d.conflict      # you mapped it just now
        else:
            d.accept = False                                # not seen in review
        accepted += d.accept
    # rows edited in the review whose source re-read differently (a Field or
    # Part typed in by hand) are taken as typed
    seen = {(d.obs.where, d.obs.field, d.obs.item) for d in decisions}
    for (where, fname, item), (acc, part, value, _) in choice.items():
        if acc and (where, fname, item) not in seen and fname in valid and part:
            decisions.append(Decision(Obs(part, item, fname, value, 1.0, "review",
                                          "", where), True))
            accepted += 1

    rows = rows_from(decisions, schema)
    res = ledger.add_samples(rows, source=", ".join(Path(s).name for s in meta["sources"])[:200])
    filled, warnings = [], []
    if fill_dir:
        d = Path(fill_dir)
        d.mkdir(parents=True, exist_ok=True)
        stem = Path(schema.template_path).stem
        for p in sorted({r[schema.id_field] for r in rows}, key=sample_sort_key):
            out = d / f"{_safe(stem)}_{_safe(p)}.xlsx"
            warnings += fill_template(schema, ledger.rows_for(p), out)
            filled.append(str(out))
    return ApplyResult(learned, ignored, accepted, len(rows), res.describe(), filled,
                       warnings + [f"conflict kept: {c}" for c in res.conflicts])


# ── optional: a local model suggests mappings for leftover labels ──────
def suggest_with_llm(run: Extraction, schema: TemplateSchema, model: str = "qwen2.5:7b-instruct-q4_K_M"):
    """Ask a local Ollama TEXT model which field each unmatched label is.
    Only labels are sent (to localhost), never values. Suggestions go in the
    'Best guess' column; nothing is accepted on a model's say-so."""
    labels = sorted({o.label for o in run.unmatched})[:150]
    if not labels:
        return
    fields = [f.name for f in schema.fields]
    prompt = ("Map each spreadsheet label to the one template field it means, or null "
              "if none. Reply with JSON only: {\"label\": \"field or null\"}.\n\n"
              f"Fields:\n{json.dumps(fields)}\n\nLabels:\n{json.dumps(labels)}")
    try:
        out = _ollama("/api/chat", {"model": model, "stream": False, "format": "json",
                                    "options": {"temperature": 0},
                                    "messages": [{"role": "user", "content": prompt}]},
                      timeout=300)
        sugg = json.loads(out["message"]["content"])
    except Exception as e:
        run.problems.append(f"local model suggestion skipped: {e}")
        return
    valid = set(fields)
    for o in run.unmatched:
        s = sugg.get(o.label)
        if isinstance(s, str) and s in valid:
            o.guesses = [(f"{s} [model]", 0.0)] + o.guesses


# ── command line ────────────────────────────────────────────────────────
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="read source workbooks into a review workbook")
    e.add_argument("csv", help="the parent CSV made by sample_ledger.py")
    e.add_argument("sources", nargs="+", help="source .xlsx files (or folders of them)")
    e.add_argument("-o", "--out", help="review workbook (default: review_<time>.xlsx "
                   "beside the parent CSV)")
    e.add_argument("--part", default="", help="part number for every source, if not in them")
    e.add_argument("--no-images", action="store_true", help="do not read embedded images")
    e.add_argument("--llm", nargs="?", const="qwen2.5:7b-instruct-q4_K_M",
                   help="ask a local Ollama model to guess unmatched labels")
    a = sub.add_parser("apply", help="put a reviewed workbook's accepted values in the CSV")
    a.add_argument("review")
    a.add_argument("--fill-dir", help="also write one filled template per part here")
    a.add_argument("--no-images", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "extract":
            ledger = ParentCSV(args.csv)
            if ledger.schema is None:
                raise LedgerError("no schema beside the CSV; run sample_ledger.py first")
            files = []
            for s in args.sources:
                p = Path(s)
                files += sorted(x for x in p.glob("*.xlsx") if not x.name.startswith("~$")) \
                    if p.is_dir() else [p]
            out = Path(args.out) if args.out else Path(args.csv).with_name(
                f"review_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
            mapping, ignore = load_aliases(args.csv)
            run = extract(ledger.schema, files, mapping, ignore,
                          image_dir=out.with_name(out.stem + "_images"),
                          part=args.part, use_images=not args.no_images)
            if args.llm:
                suggest_with_llm(run, ledger.schema, args.llm)
            dec = decide(run, ledger.schema)
            write_review(out, run, dec, ledger.schema, args.csv)
            ok = sum(d.accept for d in dec)
            print(f"{len(files)} files: {len(dec)} values found, {ok} pre-accepted, "
                  f"{len(dec) - ok} to review, {len(run.unmatched)} values under labels "
                  f"that matched no field, {len(run.images)} images")
            for p in run.problems:
                print(f"  ! {p}")
            print(f"review: {out}")
        else:
            r = apply_review(args.review, args.fill_dir, use_images=not args.no_images)
            if r.learned:
                print(f"learned {len(r.learned)} label mappings")
            print(f"{r.accepted} values accepted -> {r.rows} rows: {r.csv_result}")
            for f in r.filled:
                print(f"  filled {f}")
            for w in r.warnings[:30]:
                print(f"  ! {w}")
    except LedgerError as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
