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
Workbooks are read with openpyxl. Images are read by Tesseract (local OCR)
when it is installed, and by nothing otherwise — they are then saved for you
to read. A local Ollama vision model is used only when asked for, never by
default, because it can write numbers that are not in the picture. No source value is sent to
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
import os
import re
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sample_ledger import (INITIALS_SUFFIX, LedgerError, ParentCSV, TemplateField,
                           TemplateSchema, _text, fill_template, inspector_column,
                           normalize_label, sample_sort_key, schema_path_for)

ACCEPT_AT = 0.85
SUGGEST_AT = 0.6
TIE_GAP = 0.04
IMAGE_PENALTY = 0.8
SECTION_BONUS = 0.1
#: 127.0.0.1, not localhost: Windows tries IPv6 ::1 first and every call
#: waited ~2 s (review, 2026-10-19)
OLLAMA = "http://127.0.0.1:11434"


def enumerate_ollama_hosts(include_remote: Optional[bool] = None) -> List[str]:
    """Hosts the Ledger will query for installed US-origin models.

    This is Council's own host enumeration surface: ``COUNCIL_OLLAMA_HOST``
    (default localhost) is always included; ``COUNCIL_PI_HOSTS`` (a
    comma-separated list) is added when remote nodes are enabled —
    ``COUNCIL_REMOTE_NODES=1`` is Council's own opt-in. Pass
    ``include_remote=True`` to force the Pi list in without touching
    the env var (tests).

    Order matters: callers try the hosts in order and keep the first
    that has a US-origin model, so localhost comes first (lowest
    latency, no network leaving the LAN)."""
    main = os.environ.get("COUNCIL_OLLAMA_HOST", OLLAMA)
    hosts = [main]
    if include_remote is None:
        include_remote = os.environ.get(
            "COUNCIL_REMOTE_NODES", "").strip().lower() in (
            "1", "true", "yes", "on")
    if include_remote:
        for raw in os.environ.get("COUNCIL_PI_HOSTS", "").split(","):
            h = raw.strip()
            if h and h not in hosts:
                hosts.append(h)
    return hosts

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


# ── who logged a reading ────────────────────────────────────────────────
#: A label that introduces a person: "Inspector: JB", "Operator - MK".
_WHO_INLINE = re.compile(
    r"(?i)^\s*(?:inspector(?:\s+initials)?|initials|operator|technician|tech|analyst|"
    r"inspected\s+by|checked\s+by|logged\s+by)\s*(?:[:=\-–—]\s*|\s+by\s+)(?P<who>.+?)\s*$")
_WHO_BY = re.compile(r"(?i)^\s*(?:inspected|checked|logged)\s+by\s+(?P<who>.+?)\s*$")
#: A column / label that holds initials, with the value beside or below it.
#: 'Inspector Name', 'QC Inspector', 'Technician Initials', 'Operator ID'
#: count too: they never gave a person in the knowledge graph (review,
#: 2026-10-19).
_WHO_HEAD = re.compile(
    r"(?i)^\s*(?:"
    r"(?:(?:qc|qa|lead|nde|ndt|ndi)\s+)?"
    r"(?:inspector|technician|tech|operator|analyst|examiner)(?:'?s)?"
    r"(?:\s+(?:name|initials|id))?"
    r"|initials|inspected\s+by|checked\s+by|performed\s+by|by"
    r")\s*[:#]?\s*$")
#: "Run #", "Pass", "Cycle No." - numbers the runs of a repeating block.
_RUN_HEAD = re.compile(r"(?i)^\s*(?:run|pass|cycle|batch)\s*(?:#|no\.?|num(?:ber)?)?\s*$")
#: The column holding the reading in a long log, beside one naming what it is.
_VALUE_HEAD = re.compile(r"(?i)^\s*(?:response|value|reading|result|measured(?: value)?|"
                         r"measurement)\s*(?:\([^)]*\))?\s*$")
#: A number typed with its unit: "0.245 mm", "45°", '0.028"'.
_NUM_UNIT = re.compile(r"^\s*(?P<n>[+-]?(?:\d+\.?\d*|\.\d+))\s*(?P<u>°|\"|[A-Za-zµμ/]+)\s*$")
#: A table heading that is just someone's initials: "JB", "JB — 2026-09-18".
#: Matched on the stripped cell only (no overlapping whitespace runs: the
#: old pattern backtracked cubically on a space-padded cell).
_WHO_HEADING = re.compile(r"^(?P<who>[A-Z]{2,4})(?:\s*[-–—:,(][\d\s\-/.:,)]*)?$")


def _existing_cells(ws):
    """((row, col), value) of the cells the sheet really has. iter_rows()
    walks the sheet's whole dimension and makes a Cell for every position:
    one formatted empty cell at T1048576 turned a 5 KB file into 21 million
    cells, 4 GB and garbage-collector passes that froze the app for seconds
    (review, 2026-10-19)."""
    store = getattr(ws, "_cells", None)
    if isinstance(store, dict):
        return [(rc, cell.value) for rc, cell in sorted(store.items())]
    return [((c.row, c.column), c.value) for row in ws.iter_rows() for c in row]


def _initials(v) -> str:
    """'jb' / 'J.B.' -> 'JB'; a written name is kept as written; anything
    else (a number, a long sentence) is not a person."""
    s = re.sub(r"\.", "", str(v or "")).strip()
    if re.fullmatch(r"[A-Za-z]{1,4}", s):
        return s.upper()
    if re.fullmatch(r"[A-Za-z][A-Za-z '\-]{2,30}", s) and len(s.split()) <= 3:
        return " ".join(w.capitalize() for w in s.split())
    return ""


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


# ── preview (one workbook, no extraction) ──────────────────────────────
@dataclass
class _LabelHit:
    label: str
    field: str                       # the template field it binds to
    score: float
    where: str                       # "A3" / "D15"
    via: str                         # alias | match | ambiguous | none


@dataclass
class SheetPreview:
    """What ``preview_workbook`` found on one sheet."""
    name: str
    cells: int                       # non-empty cells
    part_id: str                     # the heuristics' pick, or ""
    part_how: str                    # how it was picked
    inspectors: List[str]            # unique initial tokens found
    sure: List[_LabelHit]            # confident label→field bindings
    maybe: List[_LabelHit]            # low-score suggestions
    unmatched_labels: List[Tuple[str, str]]  # (label_text, where)


@dataclass
class WorkbookPreview:
    """``preview_workbook``'s report. Plain-Python; the Ledger tab's
    preview dialog renders it as text, and tests assert on fields
    directly."""
    path: str
    sheets: List[SheetPreview]
    file_part_id: str                # inferred from the filename stem
    file_part_how: str
    pattern_mask: str = ""
    problems: List[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"Preview of {self.path}"]
        if self.file_part_id:
            lines.append(f"  Filename suggests part {self.file_part_id!r} "
                         f"({self.file_part_how})")
        else:
            lines.append("  Filename gives no part id "
                         "(no 'Part/Sample' keyword, no lone 2-8-digit number).")
        if self.pattern_mask:
            lines.append(f"  Pattern mask: {self.pattern_mask!r}")
        for s in self.sheets:
            lines.append(f"\n  Sheet {s.name!r} ({s.cells} filled cells)")
            lines.append(f"    Part on sheet: {s.part_id or '<not found>'}"
                         + (f" — {s.part_how}" if s.part_how else ""))
            lines.append("    Inspectors seen: "
                         + (", ".join(s.inspectors) or "<none>"))
            if s.sure:
                lines.append(f"    Confident labels ({len(s.sure)}):")
                for h in s.sure[:12]:
                    lines.append(f"      {h.label!r} @ {h.where} → "
                                 f"{h.field} ({h.score:.2f}, {h.via})")
                if len(s.sure) > 12:
                    lines.append(f"      …and {len(s.sure) - 12} more.")
            if s.maybe:
                lines.append(f"    Weak suggestions ({len(s.maybe)}):")
                for h in s.maybe[:8]:
                    lines.append(f"      {h.label!r} @ {h.where} → "
                                 f"{h.field} ({h.score:.2f}, {h.via})")
                if len(s.maybe) > 8:
                    lines.append(f"      …and {len(s.maybe) - 8} more.")
            if s.unmatched_labels:
                lines.append(f"    Unmatched label cells "
                             f"({len(s.unmatched_labels)}):")
                for label, where in s.unmatched_labels[:12]:
                    lines.append(f"      {label!r} @ {where}")
                if len(s.unmatched_labels) > 12:
                    lines.append(f"      …and {len(s.unmatched_labels) - 12} "
                                 "more.")
        for p in self.problems:
            lines.append(f"  ! {p}")
        return "\n".join(lines)


def preview_workbook(path, schema, aliases=None,
                     pattern_mask: Optional[str] = None, ignore=(),
                     part: str = "") -> WorkbookPreview:
    """Open ``path`` and report what the Ledger extractor can and can't
    see — per sheet: non-empty cell count, the part its values would get
    (and how), inspector initials, the labels the extract binds to a
    template field (confident / weak) and the labels it leaves for
    'Labels to map'.

    Runs extract()'s own passes on the workbook (same matcher, ignore list,
    item grids, tables, forms, workbook-level part choice, Part override),
    so the preview says what the batch will do: a context-free sweep over
    every string cell listed data values (names, results, item codes) as
    'unmatched labels' - and "Ask Council" sent them to the model - and
    disagreed with the extract about bindings (review, 2026-10-19). No
    review workbook is written, no pictures are copied."""
    p = Path(path)
    mask_re = mask_to_regex(pattern_mask) if pattern_mask else None
    file_part = _part_from_filename(p.stem, mask_re)
    by_mask = bool(mask_re and file_part and mask_re.search(p.stem))
    file_how = (f"filename {p.name!r}, mask-matched" if by_mask
                else (f"filename {p.name!r}" if file_part else ""))
    report = WorkbookPreview(path=str(p), sheets=[], file_part_id=file_part,
                             file_part_how=file_how,
                             pattern_mask=pattern_mask or "")
    try:
        grids, _images = load_workbook_grids(p, report.problems,
                                             want_pictures=False)
    except LedgerError as e:
        report.problems.append(str(e))
        return report
    m = Matcher(schema, aliases, ignore)
    codes = ItemCodes(schema.items)
    exs = [_Extractor(g, m, codes, "", mask_re) for g in grids]
    for ex in exs:
        try:
            ex.find_part()
            ex.find_inspectors()
        except Exception as exc:                           # noqa: BLE001
            report.problems.append(f"{ex.g.sheet}: {exc!r}")
    # the part each sheet's values get - extract()'s own rule
    found = {ex.part for ex in exs if ex.part}
    if part:
        book_part, how = part, "the Part number you typed"
    elif len(found) == 1:
        book_part, how = next(iter(found)), "the one part number in the workbook"
    elif not found:
        book_part = file_part
        how = f"file name {p.name!r} — check it" if book_part else ""
    else:
        book_part, how = "", ""
    for ex in exs:
        if part:
            ex.part, ex.part_how = part, how
        elif not ex.part:
            ex.part, ex.part_how = book_part, how
        try:
            ex.item_grids()
            ex.tables()
            ex.forms()
        except Exception as exc:                           # noqa: BLE001
            report.problems.append(f"{ex.g.sheet}: {exc!r}")
        inspectors = sorted({who for _r, _c, who in ex.markers})
        sure: List[_LabelHit] = []
        maybe: List[_LabelHit] = []
        seen = set()
        for o in ex.out:                                   # one line per label
            key = (o.label, o.field)
            if key in seen:
                continue
            seen.add(key)
            hit = _LabelHit(o.label, o.field, o.score, o.where, o.via)
            (sure if o.score >= ACCEPT_AT and o.via in ("alias", "match") else maybe).append(hit)
        unmatched: List[Tuple[str, str]] = []
        names = set()
        for o in ex.unmatched:                             # what 'Labels to map' gets
            label = (o.label or "").strip()
            if label and label not in names:
                names.add(label)
                unmatched.append((label, o.where))
        if not ex.part and ex.out:
            report.problems.append(f"{ex.g.sheet}: {len(ex.out)} value(s) would get no "
                                   f"{schema.id_field} — type one in Part number, or set "
                                   "a part-name pattern")
        report.sheets.append(SheetPreview(
            name=ex.g.sheet, cells=len(ex.g.cells),
            part_id=ex.part, part_how=ex.part_how,
            inspectors=inspectors,
            sure=sure, maybe=maybe,
            unmatched_labels=unmatched[:200]))
    return report


#: Workbook files the reader takes: openpyxl reads .xlsx/.xlsm; the installed
#: Excel opens .xls, .xlsb and rights-protected files (excel_open.py).
WORKBOOK_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".xlsb")


_SYSTEM_DIRS = {"$recycle.bin", "system volume information", "recycler"}


def _is_link_dir(path: str) -> bool:
    try:
        isj = getattr(os.path, "isjunction", None)
        return os.path.islink(path) or bool(isj and isj(path))
    except OSError:
        return True


#: Windows' MAX_PATH: with long paths off (LongPathsEnabled=0, the default)
#: a path this long or longer cannot be opened, listed or even seen.
_MAX_PATH = 260


def _long_path(path: str) -> str:
    """`path` with Windows' \\\\?\\ prefix, which lifts MAX_PATH for this
    one call; unchanged elsewhere."""
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):                                  # \\server\share
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def path_too_long(path) -> int:
    """The length of `path` when Windows refuses it only for being longer than
    MAX_PATH (long paths off, the file there all the same); else 0."""
    if os.name != "nt":
        return 0
    p = os.path.abspath(str(path))
    if len(p) < _MAX_PATH or os.path.exists(p):
        return 0
    return len(p) if os.path.exists(_long_path(p)) else 0


def workbook_files(folder) -> List[Path]:
    """The workbooks under a folder — recursively — not Excel's ~$ lock files.

    Folders whose names start with "." or "~$" are pruned at every depth so
    hidden caches (``.git``, ``.cache``), VCS metadata and Excel autosave
    directories don't leak into the scan; ``~$`` files anywhere are skipped
    for the same reason (Excel's lockfile for an open workbook).

    Listed through the \\\\?\\ prefix on Windows: a folder whose path is
    longer than MAX_PATH could not be listed and its workbooks were skipped
    without a word (review, 2026-10-19). They are returned as plain paths;
    reading one says the path is too long (load_workbook_grids)."""
    root = Path(folder)
    top = _long_path(str(root))
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(top):
        # In-place prune so os.walk does not descend into hidden/lock dirs,
        # junctions / links (a loop read the same workbooks many times) or a
        # drive's system folders (deleted workbooks came back) - review,
        # 2026-10-19.
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and not d.startswith("~$")
                       and d.lower() not in _SYSTEM_DIRS
                       and not _is_link_dir(os.path.join(dirpath, d))]
        rel = os.path.relpath(dirpath, top)
        here = root if rel == os.curdir else root / rel     # the folder as it was given
        for name in filenames:
            if name.startswith("~$"):
                continue
            if Path(name).suffix.lower() in WORKBOOK_SUFFIXES:
                out.append(here / name)
    return sorted(out)


def load_workbook_grids(path, notes: Optional[List[str]] = None,
                        want_pictures: bool = True, data: Optional[bytes] = None,
                        careful: bool = False) -> Tuple[List[Grid], List[SourceImage]]:
    """Every sheet as a Grid, every text box and chart as a small Grid of its
    own, and the pictures. openpyxl when it can open the file; the installed
    Excel when it cannot (.xls, .xlsb, rights-protected). `notes` collects
    what could not be read (a picture Excel would not copy out).

    want_pictures=False leaves the pictures out: Excel copies each one out
    through the clipboard, which the knowledge graph's background rebuild
    does not touch (Excel then lists each picture with no data, so it can
    be counted).

    data: the workbook's bytes, already read (an .xlsx / .xlsm): it is
    parsed from them, so the file is not held open while openpyxl works -
    Excel could not save it meanwhile.

    careful: Excel copies pictures only while nothing else uses the clipboard
    (excel_open.read_with_excel; the knowledge graph's background rebuild)."""
    import openpyxl
    from openpyxl.utils import get_column_letter
    import excel_open
    path = Path(path)
    kind = excel_open.kind_of(path) if path.exists() else "unknown"
    if not path.exists():
        n = path_too_long(path)
        if n:
            raise LedgerError(f"{path.name}: path too long: {n} characters — enable Windows "
                              "long paths or move the folder")
        raise LedgerError(f"{path.name} not found")
    if kind == "password":
        # from ExcelOpenError('password'), so the run's summary counts it as
        # a password, not as 'other'
        why = (f"{path.name} has a password to open; open it in Excel, save a "
               "copy without the password, and add that copy")
        raise LedgerError(why) from excel_open.ExcelOpenError(why, kind="password")
    if kind != "zip" or path.suffix.lower() in (".xls", ".xlsb"):
        return _grids_from_excel(path, notes, want_pictures, careful)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data) if data is not None else path,
                                    data_only=True)
    except Exception as e:
        raise LedgerError(f"could not open {path.name}: {e}") from e
    grids, images = [], []
    for ws in wb.worksheets:
        cells = {}
        for (r, c), v in _existing_cells(ws):
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            if isinstance(v, str) and v.strip().upper() in _EXCEL_ERRORS:
                continue
            cells[(r, c)] = v
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
        for img in (getattr(ws, "_images", []) if want_pictures else []):
            try:
                data = img._data()
                fr = img.anchor._from
                anchor = f"{get_column_letter(fr.col + 1)}{fr.row + 1}"
            except Exception:
                continue
            images.append(SourceImage(path.name, ws.title, anchor, data,
                                      (getattr(img, "format", None) or "png").lower()))
    wb.close()
    for ex in excel_open.zip_extras(io.BytesIO(data) if data is not None else path):
        # text boxes and charts
        grids.append(Grid(path.name, ex.name, ex.cells))
    return grids, images


def _grids_from_excel(path: Path, notes: Optional[List[str]], want_pictures: bool = True,
                      careful: bool = False):
    import excel_open
    try:
        book = excel_open.read_with_excel(path, want_pictures=want_pictures,
                                          **({"careful": True} if careful else {}))
    except excel_open.ExcelOpenError as e:
        raise LedgerError(str(e)) from e
    grids: List[Grid] = []
    for name, cells in book.sheets:
        fixed = {k: (_Merged(v[1]) if isinstance(v, tuple) and v and v[0] == "__merged__"
                     else v) for k, v in cells.items()}
        fixed = {k: v for k, v in fixed.items()
                 if not (isinstance(v, str) and v.strip().upper() in _EXCEL_ERRORS)}
        grids.append(Grid(path.name, name, fixed))
    for ex in book.extras:
        grids.append(Grid(path.name, ex.name, ex.cells))
    images = [SourceImage(path.name, pic.sheet, pic.anchor, pic.data, pic.ext)
              for pic in book.pictures]
    if notes is not None:
        notes.extend(book.notes)
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
    inspector: str = ""        # who logged it (EC readings and the like)

    @property
    def key(self) -> Tuple[str, str, str, str]:
        return (self.part, self.item, self.field, self.where)


class _Extractor:
    def __init__(self, grid: Grid, m: Matcher, codes: ItemCodes, part_hint: str,
                 mask_re: "Optional[re.Pattern]" = None):
        self.g, self.m, self.codes = grid, m, codes
        self.part = part_hint
        self.mask_re = mask_re
        self.used = set()
        self.out: List[Obs] = []
        self.unmatched: List[Obs] = []
        self.markers: List[Tuple[int, int, str]] = []   # (row, col, who)
        #: called every so often inside the long passes (extract: Cancel,
        #: the stall watchdog's beat, progress); may raise _Cancelled
        self.tick: Optional[Callable[[], None]] = None
        # The cells of each row, made once. Every 'what is on row r' used
        # to scan the whole sheet - context() 25 times per call - so one
        # 2,000-row sheet took 4.6 minutes, 87% of it here (review,
        # 2026-10-19).
        self.rows: Dict[int, List[Tuple[int, object]]] = defaultdict(list)
        for (r, c), v in sorted(grid.cells.items()):
            self.rows[r].append((c, v))
        self._ctx: Dict[Tuple[int, int], List[str]] = {}
        self._ticks = 0

    def _tick(self) -> None:
        self._ticks += 1
        if self.tick is not None and self._ticks % 200 == 0:
            self.tick()

    def row(self, r: int) -> List[Tuple[int, object]]:
        """(column, value) of row r's cells, left to right."""
        return self.rows.get(r, [])

    # context = sheet name + heading-like rows above
    def context(self, r: int) -> List[str]:
        key = (r, len(self.markers))
        got = self._ctx.get(key)
        if got is not None:
            return list(got)
        ctx = [self.g.sheet]
        people = {mr for mr, _mc, _ in self.markers}   # "Inspector: JB  Date: ..." rows
        for rr in range(max(1, r - 25), r):
            if rr in people:
                continue                                  # say who, not what
            row = self.row(rr)
            if len(row) == 1 and isinstance(row[0][1], str):
                ctx.append(row[0][1])
        self._ctx[key] = ctx
        return list(ctx)

    def emit(self, part, item, label, r, c, value, match: Match, field_name=None,
             vr=None, vc=None, who=None):
        txt = _text(value)
        if txt == "" or isinstance(value, _Merged):
            return None
        where = self.g.where(vr or r, vc or c)
        fname = field_name or (match.target.fields[0] if match.target else "")
        note = ""
        val_unit = ""
        if isinstance(value, str) and match.target and match.target.unit:
            nu = _NUM_UNIT.match(txt)
            if nu and nu.group("u").lower() in UNITS:     # a unit we know, not "2x"
                txt, val_unit = nu.group("n"), nu.group("u")
        if match.target and match.target.unit:
            src_unit = val_unit or split_unit(label)[1]
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
        if o.field and self.m.schema.takes_inspectors(o.field):
            o.inspector = who or self.inspector_for(vr or r, vc or c)
        (self.out if match.target else self.unmatched).append(o)
        return o

    # who logged what: markers found before the values are read
    def find_inspectors(self) -> None:
        """'Inspector: JB' cells, an initials label with the initials beside
        it, and a table heading that is only initials ('JB — 2026-09-18').
        They are taken as markers - not values - so they are not offered as
        values of some field."""
        known = set()
        for t in self.m.targets:
            known.update(self.m._tok[t.name])
            known.update(self.m._sec[t.name])
        for (r, c), v in sorted(self.g.cells.items()):
            if not isinstance(v, str) or isinstance(v, _Merged) or (r, c) in self.used:
                continue
            self._tick()

            def person(text) -> str:
                """Initials or a name - never a word of the template ('Date')."""
                w = _initials(text)
                return w if w and not set(tokens(w)) & known else ""
            m = _WHO_INLINE.match(v) or _WHO_BY.match(v)
            who = person(m.group("who")) if m else ""
            if not who and _WHO_HEAD.match(v):
                for dc in (1, 2):
                    x = self.g.cells.get((r, c + dc))
                    if x is not None:
                        who = person(x) if isinstance(x, str) else ""
                        if who:
                            self.used.add((r, c + dc))
                        break
            if not who and len(v) <= 60:
                # a heading of initials and a date is short; a long padded
                # cell made this pattern backtrack for 54 s with the GIL held
                # (review, 2026-10-19)
                h = _WHO_HEADING.match(v.strip())
                if h and person(h.group("who")) and not self.codes.canon(v) \
                        and self._heads_a_table(r, c):
                    who = h.group("who")
            if who:
                self.markers.append((r, c, who))
                self.used.add((r, c))
                # "Inspector: JB   Date: 2026-09-12": when THEY inspected, not
                # the part's build Date
                for c2, x in self.row(r):
                    if c2 != c and isinstance(x, str) \
                            and re.match(r"(?i)^\s*date\b", x):
                        self.used.add((r, c2))
                        if not _split_inline(x)[1]:
                            self.used.add((r, c2 + 1))
        if not self.markers:
            h = re.match(r"^\s*([A-Z]{2,4})\b", self.g.sheet)
            if h and not set(tokens(h.group(1))) & known and h.group(1) not in ("PART", "NDE"):
                self.markers.append((0, 0, h.group(1)))

    def _heads_a_table(self, r, c) -> bool:
        """A heading, not a value: no numbers on its row, and right under it a
        header row of 2+ labels that starts at its column. (Initials typed in
        an 'Inspector Initials' column sit on a row of readings - not this.)"""
        if any(not isinstance(v, str) for _c, v in self.row(r)):
            return False
        below = sorted(cc for cc, v in self.row(r + 1)
                       if isinstance(v, str) and c - 1 <= cc <= c + 8)
        return len(below) >= 2 and below[0] <= c + 1

    def inspector_for(self, r, c) -> str:
        """The nearest marker above the value (the lowest row above it), and of
        two at that row the one at or left of its column."""
        above = [m for m in self.markers if m[0] < r or (m[0] == r and m[1] < c)]
        if not above:
            return ""
        top = max(m[0] for m in above)
        row = [m for m in above if m[0] == top]
        left = [m for m in row if m[1] <= c]
        return (max(left, key=lambda m: m[1]) if left
                else min(row, key=lambda m: abs(m[1] - c)))[2]

    def _who_columns(self, r, skip) -> Dict[int, bool]:
        """Columns of header row r that hold initials ('Inspector Initials')."""
        return {c: True for c, v in self.row(r)
                if c not in skip and isinstance(v, str) and _WHO_HEAD.match(v)}

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
            if self.m.is_item_header(label):
                continue                       # "Flaw #" is not "Part #"
            mt = self.m.match(label, per_item=False)
            if mt.target and mt.target.is_id and mt.score >= SUGGEST_AT:
                val = inline if inline else self._value_near(r, c)
                if val and self.codes.canon(val):
                    continue                   # a flaw code is never a part number
                if val and not self.m.match(val).target:
                    if not self._is_table_header(r, c):
                        self.part = _text(val)
                        self.part_how = f"{self.m.schema.id_field} on {self.g.sheet}" + (
                            f", read from image {self.g.from_image}" if self.g.from_image else "")
                        return
        sid = _part_from_name(self.g.sheet, self.mask_re)
        if sid:
            self.part = sid
            self.part_how = f"sheet name {self.g.sheet!r}"
            return
        self.part_how = ""

    def _is_table_header(self, r, c) -> bool:
        hits = sum(1 for c2, v in self.row(r)
                   if c2 != c and isinstance(v, str)
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
            who_cols = self._who_columns(r, {c for c, _ in hits} | {label_col})
            ctx = self.context(r)
            rr = r + 1
            blanks = 0
            while blanks < 2 and rr < r + 60:
                self._tick()
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
                who = next((w for w in (_initials(self.g.cells.get((rr, wc)))
                                        for wc in who_cols) if w), None)
                for c, it in hits:
                    if (rr, c) in self.g.cells:
                        self.emit(self.part, it, label, rr, label_col,
                                  self.g.cells[(rr, c)], mt, vr=rr, vc=c, who=who)
                        self.used.add((rr, c))
                rr += 1

    # TABLE: header row + rows
    def tables(self) -> None:
        by_row: Dict[int, List[int]] = defaultdict(list)
        for (r, c), v in self.g.cells.items():
            if isinstance(v, str) and (r, c) not in self.used:
                by_row[r].append(c)
        for r in sorted(by_row):
            self._tick()
            cols = sorted(c for c in by_row[r] if (r, c) not in self.used)
            for run in self._header_runs(r, cols):
                self._table(r, run)

    def _header_runs(self, r, cols) -> List[List[int]]:
        """A header row splits where a column has nothing in it: settings in
        B:C beside a flaw table in E:I are two things, not one wide table.
        (Measured: the build record's 8 settings were read as table columns.)"""
        runs: List[List[int]] = []
        for c in cols:
            if runs and c == runs[-1][-1] + 1:
                runs[-1].append(c)
            else:
                runs.append([c])
        return runs

    def _table(self, r, cols) -> None:
        if len(cols) < 2:
            return
        ctx = self.context(r)
        heads = {}
        item_col = who_col = run_col = value_col = None
        ids: List[Tuple[float, int]] = []
        for c in cols:
            v = self.g.text(r, c)
            if self.m.is_item_header(v):
                item_col = c
                continue
            if _WHO_HEAD.match(v):
                who_col = c
                continue
            if _RUN_HEAD.match(v):
                run_col = c                       # "Run #" is not "Part #"
                continue
            if _VALUE_HEAD.match(v):
                value_col = c
                continue
            mt = self.m.match(v, ctx)
            if mt.target and mt.target.is_id and mt.score >= SUGGEST_AT:
                ids.append((mt.score, c))
                continue
            heads[c] = (v, mt)
        # the best Part Number column wins; a weaker one is an ordinary column
        id_col = max(ids)[1] if ids else None
        for score, c in ids:
            if c != id_col:
                heads[c] = (self.g.text(r, c), Match(None, score, via="none"))
        if item_col is None:                     # a column of item codes?
            for c in cols:
                vals = [self.g.cells.get((rr, c)) for rr in range(r + 1, r + 6)]
                if sum(1 for v in vals if self.codes.canon(v)) >= 2:
                    item_col = c
                    heads.pop(c, None)
                    break
        per_item = item_col is not None
        data_rows = self._data_rows(r, cols, item_col)
        if not data_rows:
            return
        stage_col = self._stage_col(heads, data_rows, ctx) \
            if per_item and value_col is not None else None
        matched = [c for c, (_, mt) in heads.items() if mt.target]
        if len(matched) + (item_col is not None) + (id_col is not None) \
                + (stage_col is not None) < 2:
            return
        # values under a header must not be more labels
        filled = sum(1 for c in heads for d in data_rows[:3] if (d, c) in self.g.cells)
        if heads and filled == 0 and stage_col is None:
            return
        for c in cols:
            self.used.add((r, c))
        if stage_col is not None:
            self._long_rows(r, data_rows, item_col, id_col, who_col, stage_col,
                            value_col, ctx)
            return
        final = {c: (label, self.m.match(label, ctx, per_item=per_item))
                 for c, (label, _) in heads.items()}
        runs_seen: Dict[str, int] = defaultdict(int)
        for d in data_rows:
            self._tick()
            part = _text(self.g.cells.get((d, id_col))) if id_col else self.part
            item = self.codes.canon(self.g.cells.get((d, item_col))) if per_item else ""
            if per_item and not item:
                continue
            # which run of a repeating block (Concealment 1, 2 ...) this row is:
            # its Run # when there is one, else its order among this part's rows
            run_no = _num(self.g.cells.get((d, run_col))) if run_col else None
            i = int(run_no) - 1 if run_no and run_no >= 1 and run_no == int(run_no) \
                else runs_seen[part]
            runs_seen[part] += 1
            for c, (label, mt) in final.items():
                if (d, c) not in self.g.cells:
                    continue
                fname = None
                if mt.target and len(mt.target.fields) > 1:   # repeat group
                    if i >= len(mt.target.fields):
                        continue
                    fname = mt.target.fields[i]
                o = self.emit(part, item, label, r, c, self.g.cells[(d, c)], mt,
                              field_name=fname, vr=d, vc=c,
                              who=_initials(self.g.cells.get((d, who_col))) or None
                              if who_col else None)
                if o is not None and id_col:
                    o.part_how = f"{self.m.schema.id_field} column on {self.g.sheet}"
                self.used.add((d, c))
            for c in (id_col, item_col, who_col, run_col):
                if c:
                    self.used.add((d, c))

    def _data_rows(self, r, cols, item_col) -> List[int]:
        """Rows under a header until an empty one. In a flaw table one blank
        row is stepped over when the row after it has a flaw code (a gap left
        while typing), never into a different table."""
        rows: List[int] = []
        rr = r + 1
        while rr < r + 500:
            if any((rr, c) in self.g.cells for c in cols):
                rows.append(rr)
            elif rows and item_col is not None \
                    and self.codes.canon(self.g.cells.get((rr + 1, item_col))):
                pass
            else:
                break
            rr += 1
        return rows

    def _stage_col(self, heads, data_rows, ctx) -> Optional[int]:
        """A LONG LOG: one row per reading, a column naming what was measured
        ('Pre-Conceal', 'EC Post') beside a Response / Value column. The column
        whose entries match per-flaw fields is the one that says which."""
        best = None
        for c in heads:
            vals = [self.g.text(d, c) for d in data_rows[:12]]
            vals = [v for v in vals if v]
            if len(vals) < 2:
                continue
            hits = sum(1 for v in set(vals)
                       if self.m.match(v, ctx, per_item=True).score >= SUGGEST_AT)
            if hits and hits >= len(set(vals)) / 2 and (best is None or hits > best[0]):
                best = (hits, c)
        return best[1] if best else None

    def _long_rows(self, r, data_rows, item_col, id_col, who_col, stage_col,
                   value_col, ctx) -> None:
        head = self.g.text(r, value_col) or ""
        for d in data_rows:
            item = self.codes.canon(self.g.cells.get((d, item_col)))
            stage = self.g.text(d, stage_col)
            if not item or not stage or (d, value_col) not in self.g.cells:
                continue
            part = _text(self.g.cells.get((d, id_col))) if id_col else self.part
            label = f"{head} {stage}".strip()
            mt = self.m.match(label, ctx, per_item=True)
            o = self.emit(part, item, label, d, stage_col, self.g.cells[(d, value_col)], mt,
                          vr=d, vc=value_col,
                          who=_initials(self.g.cells.get((d, who_col))) or None
                          if who_col else None)
            if o is not None and id_col:
                o.part_how = f"{self.m.schema.id_field} column on {self.g.sheet}"
            # the rest of the row (date, inspector, part, flaw) says which reading
            for c, _v in self.row(d):
                self.used.add((d, c))
            self._tick()

    # LABEL / VALUE
    def forms(self) -> None:
        for (r, c), v in sorted(self.g.cells.items()):
            if (r, c) in self.used or not isinstance(v, str) or isinstance(v, _Merged):
                continue
            label, inline = _split_inline(v)
            if len(label) > 60:
                continue
            self._tick()
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
                    # another label (of any field, per-flaw ones too) is not a value,
                    # nor is a lone block letter or flaw code ("B" beside "Date")
                    if isinstance(x, str) and not _num(x) and (
                            self.m.match(x).target or self.codes.canon(x)
                            or re.fullmatch(r"[A-Za-z]", x.strip())):
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


def _part_from_name(name: str, mask_re: "Optional[re.Pattern]" = None) -> str:
    if re.fullmatch(r"(?i)sheet\s*\d+", name.strip()):
        return ""
    if mask_re is not None:
        m = mask_re.search(name)
        if m:
            return m.group(0)
    m = re.fullmatch(r"(?i)\s*(?:part|sample|specimen|p/?n|s/?n)\s*(?:no\.?|#)?\s*[-_ ]?\s*([A-Za-z]*\d[\w\-]*)\s*", name)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{1,8}", name.strip()):
        return name.strip()
    return ""


def mask_to_regex(mask: str) -> "re.Pattern":
    """Compile a user-supplied part-name pattern into a regex.

    Grammar is tiny:
      * ``@``  matches any ASCII letter (case-insensitive).
      * ``#``  matches any digit.
      * any other character matches itself LITERALLY (so ``P/N-###`` requires
        the literal prefix ``P/N-``).

    Runs collapse, so ``@@@#@@###`` compiles to
    ``[A-Za-z]{3}\\d[A-Za-z]{2}\\d{3}``. The match is bounded on both sides
    by a non-alphanumeric lookaround so a mask that is all letters and digits
    does not pick up a longer surrounding run.

    Raises LedgerError on an empty mask, a mask with no concrete character,
    or a regex that fails to compile."""
    if mask is None or not mask.strip():
        raise LedgerError("part name pattern is empty")
    parts: List[str] = []
    i = 0
    while i < len(mask):
        ch = mask[i]
        if ch in "@#":
            run = 1
            while i + run < len(mask) and mask[i + run] == ch:
                run += 1
            cls = "[A-Za-z]" if ch == "@" else r"\d"
            parts.append(cls + (f"{{{run}}}" if run > 1 else ""))
            i += run
        else:
            parts.append(re.escape(ch))
            i += 1
    body = "".join(parts)
    if not body:
        raise LedgerError(f"part name pattern {mask!r} compiles to nothing")
    try:
        return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])",
                          re.IGNORECASE)
    except re.error as e:
        raise LedgerError(f"part name pattern {mask!r}: {e}") from e


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


# ── Tesseract: the image reader used by default ─────────────────────────
# Why Tesseract and nothing else by default: it is classic OCR (begun at HP,
# developed by Google, Apache-2.0 - US origin, as CLAUDE.md requires of
# models) and it only transcribes the glyphs it sees. It can MISREAD a digit,
# but it has no language model that could write a plausible number that is
# not there - a vision model can. Values read from images are never
# pre-accepted either way; a person checks each one in the review.
TESSERACT_ENV = "COUNCIL_TESSERACT"


def find_tesseract() -> Optional[Path]:
    """The tesseract executable: $COUNCIL_TESSERACT, PATH, the usual Windows
    install folders, or a conda env named 'tesseract'. Never downloaded."""
    import os
    import shutil
    env = os.environ.get(TESSERACT_ENV, "").strip()
    if env:
        return Path(env) if Path(env).is_file() else None
    hit = shutil.which("tesseract")
    if hit:
        return Path(hit)
    exe = "tesseract.exe" if os.name == "nt" else "tesseract"
    for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        cand = Path(base) / "Tesseract-OCR" / exe
        if base and cand.is_file():
            return cand
    for conda in ("miniconda3", "anaconda3", "miniforge3"):
        for sub in (("Library", "bin", exe), ("bin", exe)):
            cand = Path.home().joinpath(conda, "envs", "tesseract", *sub)
            if cand.is_file():
                return cand
    return None


def _tessdata_for(exe: Path) -> Optional[Path]:
    """A conda build's language data sits beside it; its activation script
    normally says so in TESSDATA_PREFIX, which a direct call does not run."""
    for d in (exe.parent.parent / "share" / "tessdata", exe.parent / "tessdata"):
        if (d / "eng.traineddata").is_file():
            return d
    return None


def tsv_to_boxes(tsv: str, confs: Optional[List[float]] = None
                 ) -> List[Tuple[float, float, float, float, str]]:
    """Tesseract TSV words -> phrase boxes (x0, y0, x1, y1, text).

    Words of one Tesseract line closer than ~0.8 x the text height are one
    phrase ("Laser Power (W)"); a wider gap is a table column boundary, which
    boxes_to_rows then aligns across rows. ``confs``, when given, gets each
    box's confidence (its least sure word's TSV 'conf'), in the same order."""
    lines: Dict[Tuple[str, ...], List[Tuple[float, float, float, float, str, float]]] = {}
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        try:
            x, y, w, h = (float(v) for v in f[6:10])
        except ValueError:
            continue
        try:
            conf = max(float(f[10]), 0.0)
        except ValueError:
            conf = 0.0
        lines.setdefault(tuple(f[1:5]), []).append((x, y, x + w, y + h, f[11].strip(), conf))
    out = []
    for words in lines.values():
        words.sort(key=lambda b: b[0])
        hs = sorted(b[3] - b[1] for b in words)
        gap_max = hs[len(hs) // 2] * 0.8
        cur = words[0]
        for b in words[1:]:
            if b[0] - cur[2] <= gap_max:
                cur = (cur[0], min(cur[1], b[1]), b[2], max(cur[3], b[3]),
                       cur[4] + " " + b[4], min(cur[5], b[5]))
            else:
                out.append(cur)
                cur = b
        out.append(cur)
    if confs is not None:
        confs.extend(b[5] for b in out)
    return [b[:5] for b in out]


def rows_with_confidence(tsv: str) -> Tuple[List[List[str]], List[List[float]]]:
    """Tesseract TSV -> (rows, confidences): boxes_to_rows' rows, and beside
    each cell how sure Tesseract was of it (its least sure word; 0 for an
    empty cell). By position, not by word: the knowledge graph trusted a
    misread 'PN-7'+'7' because 'PN-77' was read sure in another cell (review,
    2026-10-18)."""
    confs: List[float] = []
    boxes = tsv_to_boxes(tsv, confs)
    rows = boxes_to_rows(boxes)
    # the same boxes once more with their numbers for text: boxes_to_rows
    # places a box by its position only, so each cell's numbers say which
    # boxes it was made of
    marked = boxes_to_rows([b[:4] + (f"<{i}>",) for i, b in enumerate(boxes)])
    sure = [[min((confs[int(i)] for i in re.findall(r"<(\d+)>", cell)), default=0.0)
             for cell in row] for row in marked]
    return rows, sure


def word_confidences(tsv: str) -> Dict[str, float]:
    """How sure Tesseract was of each word it read (its TSV 'conf', 0-100),
    the lowest when a word was read more than once. The knowledge graph
    trusts a value read from a picture only when the OCR was sure of it."""
    out: Dict[str, float] = {}
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        try:
            conf = float(f[10])
        except ValueError:
            continue
        word = f[11].strip()
        out[word] = min(conf, out.get(word, conf))
    return out


def _for_ocr(data: bytes) -> bytes:
    """Greyscale, and small screenshots enlarged: Tesseract wants text about
    20-30 px high and misreads the 10-12 px of a pasted screenshot."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("L")
    except Exception:
        return data
    if max(img.size) < 1600:
        k = 3 if max(img.size) < 800 else 2
        img = img.resize((img.width * k, img.height * k), Image.LANCZOS)
    img = _strip_table_lines(img)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _long_runs(mask, min_len: int) -> List[Tuple[int, int]]:
    import numpy as np
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return [(int(a), int(b)) for a, b in zip(np.where(d == 1)[0], np.where(d == -1)[0])
            if b - a >= min_len]


def _strip_table_lines(img, max_thick: int = 12):
    """Whiten a table's cell borders. With them in, Tesseract read a bordered
    3x3 table as the one word "wom"; without them, every cell (measured on
    the test pictures). A border is a long straight dark run - text never
    makes one - and THIN: a run as long but over many neighbouring rows is a
    filled bar (white text on a dark header), left alone."""
    try:
        import numpy as np
    except Exception:
        return img
    from PIL import Image
    a = np.asarray(img).copy()
    dark = a < 200
    h, w = a.shape
    for axis, length, min_len in ((0, h, max(40, w // 4)), (1, w, max(40, h // 3))):
        hits: Dict[int, List[Tuple[int, int]]] = {}
        for i in range(length):
            line = dark[i] if axis == 0 else dark[:, i]
            got = _long_runs(line, min_len)
            if got:
                hits[i] = got
        i = 0
        idx = sorted(hits)
        while i < len(idx):                    # bands of neighbouring lines
            j = i
            while j + 1 < len(idx) and idx[j + 1] == idx[j] + 1:
                j += 1
            if idx[j] - idx[i] + 1 <= max_thick:
                for k in idx[i:j + 1]:
                    for s, e in hits[k]:
                        if axis == 0:
                            a[k, s:e] = 255
                        else:
                            a[s:e, k] = 255
            i = j + 1
    return Image.fromarray(a)


def tesseract_reader(exe: Optional[Path] = None, timeout: float = 60) -> Optional[ImageReader]:
    exe = Path(exe) if exe else find_tesseract()
    if exe is None:
        return None
    import subprocess
    cmd = [str(exe), "stdin", "stdout", "--psm", "11"]
    data_dir = _tessdata_for(exe)
    if data_dir is not None:
        cmd += ["--tessdata-dir", str(data_dir)]
    cmd += ["-l", "eng", "tsv"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def tsv(data: bytes) -> str:
        done = subprocess.run(cmd, input=_for_ocr(data), capture_output=True,
                              timeout=timeout, creationflags=flags)
        if done.returncode != 0:
            raise RuntimeError(done.stderr.decode("utf-8", "replace").strip()[-300:]
                               or f"tesseract exited {done.returncode}")
        return done.stdout.decode("utf-8", "replace")

    def read(data: bytes):
        return boxes_to_rows(tsv_to_boxes(tsv(data))) or None

    def with_confidence(data: bytes):
        """(rows, confidences) from one Tesseract run: beside each cell of
        rows, how sure it was of that cell (rows_with_confidence)."""
        rows, sure = rows_with_confidence(tsv(data))
        return (rows or None), sure
    read.name = "Tesseract (local)"
    read.with_confidence = with_confidence
    return read


def _ollama(path: str, payload: Optional[dict] = None, timeout=10,
             host: Optional[str] = None):
    """Call an Ollama endpoint. ``host`` defaults to the Council-standard
    OLLAMA constant (localhost); callers that want a Pi node pass its
    full ``http://pi:11434`` URL. See ``enumerate_ollama_hosts`` for how
    Council decides which hosts are reachable."""
    base = (host or OLLAMA).rstrip("/")
    req = urllib.request.Request(base + path,
                                 method="POST" if payload else "GET",
                                 data=json.dumps(payload).encode() if payload else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


#: Model families of US origin (CLAUDE.md: only these may be recommended).
US_VISION_FAMILIES = ("llama", "gemma", "phi", "granite", "olmo")


def ollama_vision_model() -> Optional[str]:
    """The first installed Ollama model of a US-origin family that reports the
    vision capability. Used only when asked for: a vision model can write a
    number that is not in the picture, so it is never the default reader."""
    try:
        names = [m["name"] for m in _ollama("/api/tags").get("models", [])]
    except Exception:
        return None
    for n in names:
        if not n.lower().startswith(US_VISION_FAMILIES):
            continue
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
    """Tesseract when it is installed, else nothing: the images are saved for
    a person to read. A vision model is never chosen on its own."""
    return tesseract_reader()


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
    #: One entry per workbook that Excel could not open: ``(name, kind)``
    #: where ``kind`` is one of ExcelOpenError's named modes. Used by the
    #: Ledger log summary at the end of a batch.
    open_failures: List[Tuple[str, str]] = field(default_factory=list)
    #: Per-workbook wall-clock timings as ``(name, seconds)``. The Ledger
    #: log appends the slowest few at the end of a batch so a stall is
    #: easy to attribute without reading the terminal.
    timings: List[Tuple[str, float]] = field(default_factory=list)
    #: True when the extract exited early because the Ledger tab's
    #: Cancel event fired. Surfaced via ``ExtractSummary.cancelled``.
    cancelled: bool = False


class _Cancelled(Exception):
    """Cancel was pressed while a workbook was being read."""


def extract(schema: TemplateSchema, sources: Sequence, aliases=None, ignore=(),
            image_dir=None, image_reader: Optional[ImageReader] = None,
            part: str = "", use_images: bool = True, progress=None,
            pattern_mask: Optional[str] = None,
            cancel: Optional[Any] = None,
            log: Optional[Callable[[str], None]] = None,
            part_finder: Optional[Callable[[str, List["Grid"]], Tuple[str, str]]] = None
            ) -> Extraction:
    """`progress`: a progress_report.Progress; each workbook is one step, and
    what it is doing (opening through Excel, reading picture 3 of 7) is shown.

    ``pattern_mask`` is an optional user-supplied part-name shape (see
    ``mask_to_regex``). When set, it is tried first on filenames and sheet
    names before the keyword / lone-number heuristics.

    ``cancel`` is an optional ``threading.Event``: when its ``is_set()``
    returns True, the run stops - between workbooks, and inside one every
    few hundred cells and before each picture - with
    ``Extraction.cancelled = True``; what the interrupted workbook gave is
    dropped, the workbooks before it are kept. The Ledger tab's Cancel
    button sets it and also calls ``excel_open.kill_active_excels()`` to
    unblock a Workbooks.Open sitting on a modal Excel dialog (a read whose
    Excel is still starting, or that has not started one yet, stops itself:
    excel_open.cancel_scope).

    ``log``: where the stall watchdog's stack dump goes (the Ledger log);
    it is also written to <app dir>/logs/ledger_stalls.txt by faulthandler,
    which works while the app is frozen.

    ``part_finder(name, grids) -> (part, where)``: asked for a workbook whose
    values found no part number (Council's model - council_core.ledger_llm);
    what it finds is a suggestion, never pre-accepted."""
    import time as _time
    from progress_report import NullProgress
    from council_core import stall_watchdog
    prog = progress or NullProgress()
    mask_re = mask_to_regex(pattern_mask) if pattern_mask else None
    m = Matcher(schema, aliases, ignore)
    codes = ItemCodes(schema.items)
    run = Extraction([str(s) for s in sources])
    reader = image_reader if image_reader is not None else (
        default_image_reader() if use_images else None)
    # The watchdog dumps every thread's stack when nothing has moved for
    # 15 s: to the Ledger log (not the progress line, whose heartbeat
    # re-printed the whole dump every 20 s - review, 2026-10-19) and, via
    # faulthandler, to a file even while a C call holds the GIL.
    prog.stage("reading workbooks", total=len(sources), unit="workbooks")

    def _stall(text: str) -> None:
        prog.update("no progress for a while — the stack dump is in the Ledger log")
        if log is not None:
            log(text)
    _wd_cm = stall_watchdog.watch("ledger extract", _stall, stall_s=15.0,
                                   dump_file=stall_watchdog.default_dump_file(
                                       "ledger_stalls.txt"))
    _wd = _wd_cm.__enter__()
    here = {"what": ""}

    def tick() -> None:
        """Inside a workbook, every few hundred cells: Cancel, the
        watchdog's beat, and a progress line that moves."""
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        _wd.beat()
        prog.update(here["what"])
    import excel_open
    try:
        # a Cancel (kill_active_excels) at any time from here stops this
        # run's Excel reads, also one pressed before a read began
        with excel_open.cancel_scope():
            _extract_all(sources, run, prog, _wd, tick, here, cancel, image_dir, reader,
                         m, codes, mask_re, part, schema, _time, part_finder)
    finally:
        try:
            _wd_cm.__exit__(None, None, None)
        except Exception:                                   # noqa: BLE001
            pass
    if cancel is not None and cancel.is_set() and not run.cancelled:
        # pressed during the last workbook (or the only one)
        run.cancelled = True
        run.problems.append("Cancelled by user.")
    return run


def source_labels(sources: Sequence) -> Dict[str, str]:
    """How each source is named in the review: its file name, or - when
    two picked workbooks share one (a folder per sample, each with its
    'laser_log.xlsx') - its path from the folder they share. Apply matches
    the reviewer's choices by that name, so a shared one merged the
    samples and all but one were lost (review, 2026-10-19)."""
    paths = [Path(s) for s in sources]
    by_name: Dict[str, List[Path]] = defaultdict(list)
    for p in paths:
        by_name[p.name.lower()].append(p)
    out: Dict[str, str] = {}
    for same in by_name.values():
        if len(same) == 1:
            out[str(same[0])] = same[0].name
            continue
        try:
            base = Path(os.path.commonpath([str(p.parent) for p in same]))
            for p in same:
                out[str(p)] = p.relative_to(base).as_posix()
        except ValueError:                                   # other drives
            for p in same:
                out[str(p)] = p.as_posix()
    return out


def _extract_all(sources, run, prog, _wd, tick, here, cancel, image_dir, reader,
                 m, codes, mask_re, part, schema, _time, part_finder=None) -> None:
    labels = source_labels(sources)
    for src in sources:
        if cancel is not None and cancel.is_set():
            run.cancelled = True
            run.problems.append("Cancelled by user before "
                                f"{Path(src).name!r}.")
            break
        kept = (len(run.obs), len(run.unmatched), len(run.images), len(run.problems))
        try:
            _extract_one(Path(src), run, prog, _wd, tick, here, cancel, image_dir,
                         reader, m, codes, mask_re, part, schema, _time,
                         labels.get(str(src)) or Path(src).name, part_finder)
        except _Cancelled:
            # half a workbook is not offered for review: what it gave goes
            del run.obs[kept[0]:], run.unmatched[kept[1]:], run.images[kept[2]:]
            del run.problems[kept[3]:]
            run.cancelled = True
            run.problems.append(f"Cancelled by user while reading {Path(src).name!r}; "
                                "the workbooks before it are in this review.")
            break


def _extract_one(src, run, prog, _wd, tick, here, cancel, image_dir, reader,
                 m, codes, mask_re, part, schema, _time, label: str = "",
                 part_finder=None) -> None:
    _t0 = _time.monotonic()
    via_excel = src.suffix.lower() in (".xls", ".xlsb") or (
        src.exists() and __import__("excel_open").kind_of(src) == "rights")
    prog.update(f"opening {src.name}" + (" through Excel (up to 2 min)" if via_excel else ""),
                force=via_excel)
    _wd.beat()
    try:
        grids, images = load_workbook_grids(src, run.problems)
        _wd.beat()
    except Exception as e:                    # noqa: BLE001
        if cancel is not None and cancel.is_set():
            # Cancel killed the Excel reading it: the COM error that
            # follows used to escape and lose the whole run (review,
            # 2026-10-19)
            raise _Cancelled() from e
        run.problems.append(str(e) if isinstance(e, LedgerError)
                            else f"{src.name}: could not be read ({e!r})")
        # Keep the Excel-open-failure category so the Ledger log can
        # summarize the batch: "N files failed to open - K rights, ...".
        cause = e.__cause__ if isinstance(e, LedgerError) else e
        kind = (getattr(cause, "kind", "unknown")
                if cause is not None and type(cause).__name__ == "ExcelOpenError"
                else "other")
        run.open_failures.append((label or src.name, kind))
        _secs = _time.monotonic() - _t0
        run.timings.append((label or src.name, _secs))
        prog.advance(what=f"{src.name}: not read ({_secs:.1f}s)")
        return
    if label and label != src.name:            # a name another source shares
        for g in grids:
            g.file = label
        for img in images:
            img.file = label
    for i, img in enumerate(images):
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        _wd.beat()
        prog.update(f"{src.name}: picture {i + 1} of {len(images)}")
        if not img.data:
            # listed by Excel but not copied out (the clipboard was busy):
            # counted as not read, never saved or read as an empty file
            run.images.append(ImageNote(img.file, img.sheet, img.anchor, "", "",
                                        "not read: Excel could not copy it out (see the "
                                        "problems)"))
            continue
        if image_dir:
            d = Path(image_dir)
            d.mkdir(parents=True, exist_ok=True)
            # every part cleaned: a sheet, anchor or extension may hold
            # characters Windows refuses in a file name ("?" did)
            stem = Path(label).with_suffix("").as_posix() if label else src.stem
            p = d / (f"{_safe(stem)}_{_safe(img.sheet)}_{_safe(img.anchor)}_{i + 1}"
                     f".{_safe(img.ext) or 'png'}")
            try:
                p.write_bytes(img.data)
                img.saved_as = str(p)
            except OSError as e:          # one picture never stops the run
                run.problems.append(f"{src.name} › {img.sheet}: a picture could not be "
                                    f"saved ({e.strerror or e}); it is still read")
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
            note.text = "not read: install Tesseract (local OCR) to read images"
        run.images.append(note)

    prog.update(f"{src.name}: finding values on {len(grids)} sheets/tables")
    # pass 1: part numbers per sheet; then a workbook-wide fallback
    exs = [_Extractor(g, m, codes, "", mask_re) for g in grids]
    for ex in exs:
        ex.tick = tick
        here["what"] = f"{src.name}: reading sheet {ex.g.sheet!r}"
        ex.find_part()
        ex.find_inspectors()
    found = {ex.part for ex in exs if ex.part}
    if part:
        book_part, how = part, "given on the command line"
    elif len(found) == 1:
        book_part, how = found.pop(), "the one part number in the workbook"
    elif not found:
        book_part = _part_from_filename(src.stem, mask_re)
        how = f"file name {src.name!r} — check it" if book_part else ""
    else:
        book_part, how = "", ""
    if part_finder is not None and not part and not book_part and any(
            not ex.part for ex in exs):
        here["what"] = f"{src.name}: asking Council's model which part it is"
        prog.update(here["what"], force=True)
        found_part, where = part_finder(label or src.name, grids)
        _wd.beat()
        if found_part:
            book_part, how = found_part, f"{MODEL_PART_HOW} from {where} — check it"
    for ex in exs:
        if part:
            ex.part, ex.part_how = part, how
        elif not ex.part:
            ex.part, ex.part_how = book_part, how
        here["what"] = f"{src.name}: finding values on sheet {ex.g.sheet!r}"
        prog.update(here["what"])
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
    _secs = _time.monotonic() - _t0
    run.timings.append((label or src.name, _secs))
    prog.advance(what=f"{src.name} read in {_secs:.1f}s")
    _wd.beat()
    orphans = sum(1 for ex in exs for o in ex.out if not o.part)
    if orphans:
        run.problems.append(f"{src.name}: {orphans} values have no {schema.id_field} "
                            "— fill the Part column in the review, or re-run with --part")


def _part_from_filename(stem: str, mask_re: "Optional[re.Pattern]" = None) -> str:
    if mask_re is not None:
        m = mask_re.search(stem)
        if m:
            return m.group(0)
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
    pre-accepted.

    A reading someone signed (an EC response with initials) is offered twice:
    in that person's own column, "<field> [JB]", where it is the only value
    and can be pre-accepted; and as a candidate for the template's single
    cell, where readings that disagree clash - the reviewer accepts one."""
    groups: Dict[Tuple[str, str, str], List[Obs]] = defaultdict(list)
    for o in run.obs:
        groups[(o.part, o.item, o.field)].append(o)
        if o.inspector:
            own = replace(o, field=inspector_column(o.field, o.inspector))
            groups[(o.part, o.item, own.field)].append(own)
    out = []
    for (part, item, fname), obs in groups.items():
        obs.sort(key=lambda o: -o.score)
        values = Counter(o.value for o in obs)
        clash = len(values) > 1
        firsts: Dict[str, Obs] = {}
        who: Dict[str, List[str]] = defaultdict(list)
        for o in obs:                     # the same value found twice: one row
            if o.inspector and o.inspector not in who[o.value]:
                who[o.value].append(o.inspector)
            if o.value in firsts:
                firsts[o.value].note = _join(firsts[o.value].note, f"also at {o.where}")
                continue
            firsts[o.value] = o = replace(o)
        for v, o in firsts.items():        # agreeing inspectors share one row
            if who[v]:                     # one signer of an unsigned top row too
                o.inspector = "/".join(sorted(who[v]))
        obs = list(firsts.values())
        # labelled once per group, and only the first OTHERS_SHOWN listed:
        # thousands of readings under one key (no part number found) made
        # this quadratic and overflowed Excel's 32,767-character cell
        # (review, 2026-10-19)
        labelled = sorted(((f"{v} ({'/'.join(who[v])})" if who[v] else v), v)
                          for v in values) if clash else []
        for o in obs:
            text = ""
            if clash:
                others = []
                for t, v in labelled:
                    if v != o.value:
                        others.append(t)
                        if len(others) >= OTHERS_SHOWN:
                            break
                more = len(values) - 1 - len(others)
                text = ", ".join(others) + (f", and {more} more" if more > 0 else "")
            ok = (not clash and o is obs[0] and o.score >= ACCEPT_AT and
                  o.via in ("alias", "match") and bool(part) and not o.from_image
                  and "not converted" not in o.note
                  and not o.part_how.startswith(MODEL_PART_HOW))
            out.append(Decision(o, ok, text))
    item_rank = {it: i for i, it in enumerate(schema.items)}
    order = {f.name: i for i, f in enumerate(schema.fields)}
    out.sort(key=lambda d: (sample_sort_key(d.obs.part), item_rank.get(d.obs.item, -1),
                            order.get(d.obs.field, 1e9), -d.obs.score))
    return out


#: How a part Council's model suggested is described (never pre-accepted).
MODEL_PART_HOW = "suggested by Council's model"

#: How many other values a clashing review row lists.
OTHERS_SHOWN = 20


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
        initials = f.field + INITIALS_SUFFIX
        if f.inspector and f.item and schema.takes_inspectors(f.field):
            target.setdefault(initials, f.inspector)    # whose value went in
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
            "Source label", "Source", "Other values offered", "Note", "Part from",
            "Inspector"]
LAB_HEAD = ["Map to field", "Ignore", "Source label", "Example value", "Count",
            "Best guess", "Source", "Part", "Item"]
PEOPLE_HEAD = ["Initials", "Full name", "Values signed", "First seen at"]


def review_initials(decisions: Iterable[Decision]) -> Dict[str, Tuple[int, str]]:
    """Every inspector's initials the run found ('JB', and both of 'JB/MK'),
    with how many values they signed and where first. Written names are
    left out: they need no answer."""
    out: Dict[str, Tuple[int, str]] = {}
    for d in decisions:
        for who in re.split(r"[/,;&+]", d.obs.inspector or ""):
            key = _initials(who)
            if not re.fullmatch(r"[A-Z]{2,4}", key):
                continue
            n, first = out.get(key, (0, d.obs.where))
            out[key] = (n + 1, first)
    return out


def write_review(path, run: Extraction, decisions: List[Decision], schema: TemplateSchema,
                 csv_path: str, pattern_mask: str = "", part: str = "") -> None:
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
        "3. People: type the full name for each inspector's initials (amber rows). It is "
        "remembered, and the knowledge graph links their inspections to that person.",
        "4. Save this workbook, then press Apply in the Ledger tab (or run:   "
        "python source_extract.py apply \"<this file>\").",
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
    # the row number is counted, not asked: ws.max_row / ws[n] measure the
    # whole sheet each time - 16,000 rows took over a minute (review,
    # 2026-10-19)
    row_n = 1
    for d in decisions:
        o = d.obs
        ws.append(["Y" if d.accept else "", o.part, o.item, o.field, o.value, o.score,
                   o.via, o.label, o.where, d.conflict, o.note, o.part_how, o.inspector])
        row_n += 1
        fill = red if d.conflict else (amber if not d.accept else None)
        if fill is not None:
            for col in range(1, len(VAL_HEAD) + 1):
                ws.cell(row_n, col).fill = fill
    yn = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    ws.add_data_validation(yn)
    yn.add(f"A2:A{max(ws.max_row, 2) + 200}")
    fdv = DataValidation(type="list", formula1=field_list, allow_blank=True)
    ws.add_data_validation(fdv)
    fdv.add(f"D2:D{max(ws.max_row, 2) + 200}")
    _style(ws, bold, [8, 10, 7, 48, 16, 11, 11, 30, 50, 18, 34, 28, 10])

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

    # Who each inspector's initials are. The knowledge graph never guesses
    # a person from 'JB', and nothing let the user say who JB is, so a
    # Ledger gave parts but no people and no links (review, 2026-10-19).
    ppl = wb.create_sheet("People", 3)
    ppl.append(PEOPLE_HEAD)
    known = load_people(csv_path)
    for key, (n, first) in sorted(review_initials(decisions).items()):
        ppl.append([key, known.get(key, ""), n, first])
        if not known.get(key):
            ppl.cell(ppl.max_row, 2).fill = amber
    _style(ppl, bold, [10, 32, 7, 50])

    meta = wb.create_sheet("_run")
    meta["A1"] = json.dumps({"csv": str(csv_path), "sources": run.sources,
                             "pattern_mask": pattern_mask or "", "part": part or ""})
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


def load_ignore_files(csv_path) -> List[str]:
    """Filename glob patterns the user has marked for a Ledger batch to
    skip (e.g. ``cover*.xlsx``, ``agenda*.xlsx``).

    Not every workbook in a folder has Ledger data — some are meeting
    notes, cover sheets, administrative docs — and running the full
    extraction against them is a cost with nothing to show. Patterns
    match the file's name only (not its full path) and are
    case-insensitive, honouring ``fnmatch`` semantics.

    Written and read via the same aliases file; `save_aliases` keeps
    this section intact across edits of the label mappings."""
    p = aliases_path_for(csv_path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    patterns = data.get("ignore_files") or []
    return [str(x) for x in patterns if isinstance(x, str) and x.strip()]


def save_ignore_files(csv_path, patterns: Sequence[str]) -> None:
    """Persist filename glob patterns next to the aliases file.
    Other sections — ``map``, ``ignore``, ``people`` — are preserved."""
    p = aliases_path_for(csv_path)
    data: Dict[str, Any] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    cleaned = sorted({pat.strip() for pat in patterns
                      if isinstance(pat, str) and pat.strip()})
    data["ignore_files"] = cleaned
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def file_matches_ignore(name: str, patterns: Sequence[str]) -> bool:
    """True when ``name`` (not its path, just the base name) matches
    any of ``patterns`` under ``fnmatch`` semantics, case-insensitively.
    An empty pattern list never matches."""
    import fnmatch
    if not patterns:
        return False
    n = str(name).lower()
    return any(fnmatch.fnmatchcase(n, pat.lower()) for pat in patterns)


def load_people(csv_path) -> Dict[str, str]:
    """Who each inspector's initials stand for: the "people" section of the
    same aliases file, {"JB": "Jordan Blake"}, written by a person. The
    knowledge graph (council_core/kg_excel.py) reads it so that 'JB' in the
    parent CSV is linked to Jordan Blake without a guess. Keys are the
    initials as the Ledger reads them (upper case, no dots); a damaged or
    missing section is {}."""
    p = aliases_path_for(csv_path)
    if not p.exists():
        return {}
    try:
        people = json.loads(p.read_text(encoding="utf-8")).get("people", {})
    except Exception:
        return {}
    if not isinstance(people, dict):
        return {}
    out = {}
    for k, v in people.items():
        key = _initials(k)
        if key and isinstance(v, str) and v.strip():
            out[key] = " ".join(v.split())
    return out


def save_people(csv_path, people: Dict[str, str]) -> Dict[str, str]:
    """Add these initials -> full names to the "people" section (a name
    given now replaces the one before; blank names are skipped). Every other
    section is kept; a file that does not parse is NOT overwritten - the
    user's label mappings would go with it. Returns the whole list."""
    p = aliases_path_for(csv_path)
    data: Dict[str, Any] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise LedgerError(f"{p.name} could not be read, so the people list was not "
                              f"saved (fix or remove the file): {e}") from e
        if not isinstance(data, dict):
            raise LedgerError(f"{p.name} is not a Ledger aliases file; people not saved")
    have = data.get("people") if isinstance(data.get("people"), dict) else {}
    for k, v in people.items():
        key, name = _initials(k), " ".join(str(v or "").split())
        if key and name:
            have[key] = name
    data["people"] = dict(sorted(have.items()))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)
    return load_people(csv_path)


def save_aliases(csv_path, mapping: Dict[str, str], ignore: List[str]) -> None:
    """The label mappings and ignores, written atomically. Any other section
    of the file (the "people" list of initials) is kept as it was: it used to
    be dropped by the first save."""
    p = aliases_path_for(csv_path)
    data: Dict[str, object] = {"map": dict(sorted(mapping.items())),
                               "ignore": sorted(set(ignore))}
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            old = {}
        if isinstance(old, dict):
            data.update({k: v for k, v in old.items() if k not in ("map", "ignore")})
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
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
    #: initials -> full name the reviewer typed on the People sheet
    people: Dict[str, str] = field(default_factory=dict)


def apply_review(review_path, fill_dir=None, image_reader: Optional[ImageReader] = None,
                 use_images: bool = True, progress=None) -> ApplyResult:
    import openpyxl
    from progress_report import NullProgress
    prog = progress or NullProgress()
    prog.stage("reading the review workbook")
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

    def placeable(fname) -> bool:      # a template field, or one person's column of one
        return fname in valid or bool(fname and schema.inspector_base(str(fname)))
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

    people: Dict[str, str] = {}
    if "People" in wb.sheetnames:                  # older reviews have none
        for row in wb["People"].iter_rows(min_row=2, values_only=True):
            if row and row[0] and len(row) > 1 and str(row[1] or "").strip():
                people[str(row[0])] = str(row[1])
    if people:
        known = load_people(csv_path)
        people = {k: v for k, v in people.items()
                  if known.get(_initials(k)) != " ".join(v.split())}
        if people:
            save_people(csv_path, people)

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
        if yes and fname in valid and label and via in ("match", "ambiguous", "model") \
                and not judge.is_generic(str(label)) and (_num(score) or 0) < ACCEPT_AT:
            learned.setdefault(normalize_label(label), str(fname))
    wb.close()
    if learned:
        mapping.update(learned)
        save_aliases(csv_path, mapping, ignore)

    # read the sources as the extract did: same part-name mask, same Part
    run = extract(schema, meta["sources"], mapping, ignore, image_reader=image_reader,
                  use_images=use_images, progress=prog,
                  pattern_mask=meta.get("pattern_mask") or None,
                  part=meta.get("part") or "")
    prog.stage("matching your choices")
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
        if acc and (where, fname, item) not in seen and placeable(fname) and part:
            decisions.append(Decision(Obs(part, item, fname, value, 1.0, "review",
                                          "", where), True))
            accepted += 1

    rows = rows_from(decisions, schema)
    prog.stage("writing the parent CSV", total=len(rows), unit="rows")
    res = ledger.add_samples(rows, source=", ".join(Path(s).name for s in meta["sources"])[:200])
    prog.advance(len(rows))
    filled, warnings = [], []
    if fill_dir:
        d = Path(fill_dir)
        d.mkdir(parents=True, exist_ok=True)
        stem = Path(schema.template_path).stem
        parts = sorted({r[schema.id_field] for r in rows}, key=sample_sort_key)
        prog.stage("filling templates", total=len(parts), unit="parts")
        for p in parts:
            out = d / f"{_safe(stem)}_{_safe(p)}.xlsx"
            prog.update(f"part {p}")
            warnings += fill_template(schema, ledger.rows_for(p), out)
            filled.append(str(out))
            prog.advance(what=f"{out.name} written")
    return ApplyResult(learned, ignored, accepted, len(rows), res.describe(), filled,
                       warnings + [f"conflict kept: {c}" for c in res.conflicts],
                       people=people)


# ── optional: a local model suggests mappings for leftover labels ──────
#: Families Council recommends: see [[us-models-only]]. The CLI --llm
#: option used to default to qwen2.5, which this policy does not
#: allow; the mapping helpers auto-detect a US-origin model from the
#: ones Ollama has installed, with Granite preferred.
US_TEXT_FAMILIES = ("granite", "llama", "phi", "gemma", "olmo", "gpt-oss")
#: Preference order inside US_TEXT_FAMILIES — Granite first because the
#: [[us-models-only]] memory flags it as the user's preferred family;
#: within a family a longer tag beats a shorter one so an 8B beats a 2B.
_FAMILY_RANK = {f: i for i, f in enumerate(US_TEXT_FAMILIES)}


def first_us_text_model(
        installed: Optional[Sequence[str]] = None,
        *,
        hosts: Optional[Sequence[str]] = None,
        installed_per_host: Optional[Dict[str, Sequence[str]]] = None,
) -> Optional[Tuple[str, str]]:
    """The best-fit installed Ollama model to ask for label mappings,
    or None when no host has a US-origin one.

    Returns ``(host, model_tag)`` so the caller knows where to send the
    chat request. Legacy single-host callers can pass ``installed`` (a
    sequence of model names on localhost); the result is still a tuple,
    with the host set to the Council default.

    Preference: within a host, family order in US_TEXT_FAMILIES (Granite
    first), then longer tag, then name. ACROSS hosts, the first that
    has a US-origin model wins — localhost first by default (see
    ``enumerate_ollama_hosts``)."""
    def _rank(n):
        lo = n.lower()
        for i, fam in enumerate(US_TEXT_FAMILIES):
            if lo.startswith(fam):
                return (i, -len(n), n)
        return (len(US_TEXT_FAMILIES), 0, n)

    def _pick(names):
        candidates = [n for n in names
                      if n and n.lower().startswith(US_TEXT_FAMILIES)]
        if not candidates:
            return None
        candidates.sort(key=_rank)
        return candidates[0]

    if installed is not None:
        tag = _pick(list(installed))
        return (OLLAMA, tag) if tag else None

    host_list = list(hosts) if hosts is not None else enumerate_ollama_hosts()
    for host in host_list:
        if installed_per_host is not None:
            names = list(installed_per_host.get(host) or [])
        else:
            try:
                data = _ollama("/api/tags", timeout=5, host=host)
                names = [m["name"] for m in (data.get("models") or [])]
            except Exception:
                continue
        tag = _pick(names)
        if tag is not None:
            return (host, tag)
    return None


#: What a mapping request's JSON answer must look like. The GGUF path
#: enforces this via grammar; the Ollama path via its "format" field.
_MAPPING_SCHEMA = {
    "type": "object",
    "additionalProperties": {"type": ["string", "null"]},
}


def _council_chat(prompt: str, *, timeout: float
                  ) -> Optional[Tuple[str, str]]:
    """Delegate a mapping prompt to Council's own ``local_chat`` so
    the call runs on whatever backend Council itself uses — the main
    GGUF when ``COUNCIL_GGUF_PATH`` is set, else the role's assigned
    Ollama. GGUF files should always be accepted by every LLM call in
    Council per the user's standing instruction.

    Returns ``(text, model_label)`` or None when council_engine cannot
    be imported (test environments without the engine) or the call
    fails. Callers fall back to a direct Ollama HTTP request."""
    try:
        import council_engine as ce
    except Exception:                                       # noqa: BLE001
        return None
    try:
        text = ce.local_chat(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            num_predict=2048,
            json_schema=_MAPPING_SCHEMA,
            timeout=int(max(5, timeout)))
    except Exception:                                       # noqa: BLE001
        return None
    # local_chat returns the raw text; its stats carry the model name,
    # but we don't need the exact tag here. "council local_chat" is a
    # clear label the dialog can show.
    return text, "council local_chat"


def suggest_mappings_for_labels(labels: Sequence[str], schema: TemplateSchema,
                                 *, model: Optional[str] = None,
                                 host: Optional[str] = None,
                                 timeout: float = 120,
                                 prefer_council: bool = True,
                                 chat: Optional[Callable] = None,
                                 ) -> Tuple[Dict[str, Optional[str]], str]:
    """Ask a local Ollama TEXT model which template field each label
    means. Returns ``(mapping, model_label)`` where ``mapping`` is
    ``{label: field_name_or_None}`` and ``model_label`` reads
    ``"<tag> @ <host>"`` so the Preview dialog can tell the user
    which model actually answered.

    Pure helper — takes a sequence of label strings, not an
    ``Extraction``. The caller writes any accepted suggestions as
    aliases via ``save_aliases``.

    Only labels are sent (to the chosen host, localhost by default,
    or a Pi node from COUNCIL_PI_HOSTS when COUNCIL_REMOTE_NODES=1).
    Values are never sent. Raises LedgerError when no reachable host
    has a US-origin text model, when the chosen model is not US-origin,
    when the chosen Ollama cannot be reached, or when the model's
    answer is not JSON. See [[us-models-only]]."""
    """Ask a local text model which template field each label means.
    Returns ``(mapping, model_label)``.

    Resolution order (unless the caller forces one via ``model`` /
    ``host`` / ``prefer_council=False``):
      1. Council's own ``local_chat`` — GGUF when ``COUNCIL_GGUF_PATH``
         is set, else the role's Ollama. GGUF files are always
         accepted here per the user's standing instruction.
      2. First US-origin Ollama model on localhost / ``COUNCIL_PI_HOSTS``
         (see ``enumerate_ollama_hosts``).

    Only labels are sent, never values. See [[us-models-only]]."""
    labels = [str(x).strip() for x in labels if str(x).strip()]
    labels = sorted({x for x in labels})[:150]
    if not labels:
        return {}, ""
    fields = [f.name for f in schema.fields]
    prompt = ("Map each spreadsheet label to the one template field it "
              "means, or null if none. Reply with JSON only: "
              "{\"label\": \"field or null\"}.\n\n"
              f"Fields:\n{json.dumps(fields)}\n\n"
              f"Labels:\n{json.dumps(labels)}")

    # ── Path 0: the model the user picked (council_core.ledger_llm.chat_for) ──
    if chat is not None:
        from council_core.ledger_llm import map_labels
        try:
            return map_labels(labels, fields, chat)
        except LedgerError:
            raise
        except Exception as exc:                            # noqa: BLE001
            raise LedgerError(f"the model did not answer: {exc}") from exc

    # ── Path 1: Council's local_chat (GGUF or role Ollama) ──
    if prefer_council and model is None and host is None:
        ans = _council_chat(prompt, timeout=timeout)
        if ans is not None:
            raw, model_label = ans
            try:
                sugg = json.loads(raw)
            except Exception:
                sugg = None
            if isinstance(sugg, dict):
                return _coerce_mapping(labels, fields, sugg), model_label
            # Fall through to Ollama if Council's reply was not JSON —
            # a tiny GGUF without grammar can wander. Ollama still has
            # a shot with its own format=json.

    # ── Path 2: direct Ollama (localhost + Pi hosts) ──
    if model is None:
        pick = first_us_text_model()
        if pick is None:
            raise LedgerError(
                "No US-origin Ollama text model is reachable on this PC "
                "or any configured Pi node, and Council's own local_chat "
                "did not answer. Install an Ollama model "
                "(e.g. `ollama pull granite3.3:8b`) or set "
                "COUNCIL_GGUF_PATH to a US-origin GGUF. "
                "COUNCIL_REMOTE_NODES=1 + COUNCIL_PI_HOSTS brings Pi "
                "nodes into the search.")
        host, model = pick
    else:
        host = host or OLLAMA
    if not model.lower().startswith(US_TEXT_FAMILIES):
        raise LedgerError(
            f"{model!r} is not a US-origin family (see [[us-models-only]])."
            " Pick a Granite / Llama / Phi / Gemma / Olmo / gpt-oss tag.")
    try:
        out = _ollama("/api/chat", {
            "model": model, "stream": False, "format": "json",
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt}]},
                      timeout=timeout, host=host)
    except Exception as exc:
        raise LedgerError(
            f"local model {model!r} on {host} did not answer: {exc}") from exc
    try:
        sugg = json.loads(out["message"]["content"])
    except Exception as exc:
        raise LedgerError(
            f"local model {model!r} on {host} did not return JSON: {exc}"
        ) from exc
    return _coerce_mapping(labels, fields, sugg), f"{model} @ {host}"


def _coerce_mapping(labels: Sequence[str], fields: Sequence[str],
                    sugg: Any) -> Dict[str, Optional[str]]:
    """Keep only the suggestions whose field is in the schema; nulls and
    unknown fields drop to None. ``sugg`` may be any shape (the model
    sometimes answers with lists or strings); non-dicts return all-None."""
    valid = set(fields)
    result: Dict[str, Optional[str]] = {}
    for label in labels:
        s = sugg.get(label) if isinstance(sugg, dict) else None
        if isinstance(s, str) and s in valid:
            result[label] = s
        else:
            result[label] = None
    return result


def suggest_with_llm(run: Extraction, schema: TemplateSchema,
                     model: Optional[str] = None):
    """Ask a local Ollama TEXT model which field each unmatched label is.
    Only labels are sent (to localhost), never values. Suggestions go in
    the 'Best guess' column; nothing is accepted on a model's say-so.

    Backward-compatible wrapper around ``suggest_mappings_for_labels``:
    default model moved from qwen (not US-origin) to the first installed
    US-origin text model. See [[us-models-only]]."""
    try:
        sugg, _m = suggest_mappings_for_labels(
            [o.label for o in run.unmatched], schema, model=model)
    except LedgerError as exc:
        run.problems.append(f"local model suggestion skipped: {exc}")
        return
    for o in run.unmatched:
        s = sugg.get(o.label)
        if s is not None:
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
    e.add_argument("--llm", nargs="?", const="",
                   help="ask a local Ollama text model (US-origin, auto-"
                        "detected if blank) to guess unmatched labels")
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
                files += workbook_files(p) if p.is_dir() else [p]
            out = Path(args.out) if args.out else Path(args.csv).with_name(
                f"review_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
            mapping, ignore = load_aliases(args.csv)
            from progress_report import Progress
            with Progress("ledger extract") as prog:
                run = extract(ledger.schema, files, mapping, ignore,
                              image_dir=out.with_name(out.stem + "_images"),
                              part=args.part, use_images=not args.no_images, progress=prog)
            if args.llm is not None:        # "" from the --llm flag means auto-detect
                suggest_with_llm(run, ledger.schema, args.llm or None)
            dec = decide(run, ledger.schema)
            write_review(out, run, dec, ledger.schema, args.csv, part=args.part or "")
            ok = sum(d.accept for d in dec)
            print(f"{len(files)} files: {len(dec)} values found, {ok} pre-accepted, "
                  f"{len(dec) - ok} to review, {len(run.unmatched)} values under labels "
                  f"that matched no field, {len(run.images)} images")
            for p in run.problems:
                print(f"  ! {p}")
            print(f"review: {out}")
        else:
            from progress_report import Progress
            with Progress("ledger apply") as prog:
                r = apply_review(args.review, args.fill_dir, use_images=not args.no_images,
                                 progress=prog)
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
