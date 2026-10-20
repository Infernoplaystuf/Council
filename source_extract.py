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
'TBD', 'N/A', '-' in a Part Number are no part; a 'Lot Number', 'Heat #',
'Drawing No.', 'P.O. Number', 'Part Name' or a 'Part Information' heading is
not the Part Number ('Customer P/N', 'Serial No.', 'Sample Number' are); a
part that does not fit the part-name pattern you gave is kept, never
pre-accepted.

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
import errno
import io
import json
import os
import re
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
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
#: The same, a bare 'part' left as typed: what a label says in so many
#: words ('Part Revision' is not 'Part Number Revision').
_ABBREV_TYPED_RE = re.compile(r"\b(" + "|".join(
    re.escape(k) for k in sorted(_ABBREV, key=len, reverse=True) if k != "part") + r")\b")
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


def tokens(text: str, typed: bool = False) -> List[str]:
    """Normalised words of a label: units and block letters out, short forms
    spelt out. typed: a bare 'part' stays 'part' (not 'part number')."""
    base, _ = split_unit(text)
    base = re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", base)    # OCR's "PartNumber"
    s = re.sub(r"p\s*/\s*n", " pn ", base.lower())
    # Not 'S/N' -> serial number here: in an EC or UT table S/N is the
    # signal-to-noise column, which became every row's part, pre-accepted,
    # and 'Inspector: SN' was no one (review, 2026-10-19). find_part reads an
    # S/N label by its value (_SN_LABEL).
    s = re.sub(r"[#]", " number ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = (_ABBREV_TYPED_RE if typed else _ABBREV_RE).sub(
        lambda m: _ABBREV[m.group(1)], s)                    # one pass: no re-expansion
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
        self._id_tok = set(tokens(schema.id_field or ""))

    def is_generic(self, label: str) -> bool:
        """A one-word label shared by 3+ fields ("Date", "Notes")."""
        s = set(tokens(label))
        return len(s) == 1 and sum(1 for t in self.targets
                                   if s <= set(self._tok[t.name])) >= 3

    def spelt_out(self, label) -> bool:
        """The id field's own words typed in the label - 'Customer P/N',
        'Part No./Rev', 'Supplier Part Number' - not read into a bare 'part'
        ('Part Revision')."""
        return bool(self._id_tok) and self._id_tok <= set(tokens(label, typed=True))

    def names_other(self, label) -> bool:
        """A word of another kind of identifier in the label, spelt out as
        the id field's or not: 'Part No./Rev', 'Part Number per Drawing'."""
        return isinstance(label, str) and bool(
            (set(tokens(label)) - self._id_tok) & _OTHER_ID_WORDS)

    def other_id(self, label) -> bool:
        """An id label of another kind of identifier: 'Lot Number', 'Heat #',
        'Work Order Number', 'Drawing No.', 'Part Revision' - a word of
        _OTHER_ID_WORDS the template's own id field does not have - or one
        of someone else's ('Customer Number', 'Report No.': _ID_OWNER_WORDS,
        with no word of the id field's). 'Serial Number', 'S/N', 'Sample
        ID' are not; a template whose id field is 'Lot Number' keeps its own
        label. Nor is one that spells out the id field's words: 'Customer
        P/N', 'Part No./Rev', 'Part Number per Drawing', 'Build Part Number'
        were the part, rightly, until a bare 'part' read as 'part number'
        put them with 'Part Revision' (review, 2026-10-19)."""
        if not isinstance(label, str) or self.spelt_out(label):
            return False
        extra = set(tokens(label)) - self._id_tok
        if extra & _OTHER_ID_WORDS:
            return True
        return bool(extra & _ID_OWNER_WORDS) and not (
            set(tokens(label, typed=True)) & (self._id_tok - _GENERIC_ID_WORDS))

    def other_id_label(self, label) -> bool:
        """'Lot Number', 'Heat #', 'Part Revision': another identifier's
        label written like the id field's (with a word of it). match() gives
        it no field now, but it is still a label, never a value: a 'Part
        Number | Lot Number' header row made the part 'Lot Number'. 'Lot
        7734-B' and 'Rev B' are values, and so is a note: a label is a few
        words, no digits or sentence marks, not opening with 'No' - 'No
        issues, see report 12', 'No change to program' were labels, and a
        Notes field lost them (review, 2026-10-19)."""
        if not isinstance(label, str):
            return False
        s = label.strip()
        n = len(s.split())
        if not (1 <= n <= 4 and _ID_LABEL_SHAPE.fullmatch(s)):
            return False
        w = tokens(s)
        if not w or not set(w) & self._id_tok:
            return False
        if n > 2 and w[-1] not in _GENERIC_ID_WORDS:
            return False                 # 'Part reworked per drawing' is a note
        return self.other_id(s)

    def is_label(self, v) -> bool:
        """A label of a field, or another identifier's label."""
        return isinstance(v, str) and (self.match(v).target is not None
                                       or self.other_id_label(v))

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
        # 'Lot Number' is not the Part Number: it took the id column of a
        # table, pre-accepted (review, 2026-10-19)
        other_id = bool((sset - self._id_tok) & _ANY_OTHER_ID_WORDS) and self.other_id(label)
        scored = []
        for t in self.targets:
            if per_item is not None and t.per_item != per_item and not t.is_id:
                continue
            if t.is_id and other_id:
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
            if not sset & fset and not any(a == b + "s" or b == a + "s"
                                           for a in sset for b in fset):
                # no word in common (a plural is the word: 'Dates', 'Amplitudes'):
                # letters alone made 'Owner' Power (W) at 0.8 and 'Parameter'
                # the Part Number (review, 2026-10-19). A slip of the field's
                # word ('Frequncy', 'Powr') is offered for review, never
                # pre-accepted: 'Powder' was Power (W) at 0.91 with the alloy
                # as its value.
                s = min(s, (ACCEPT_AT if _near_typo(src, ft) else SUGGEST_AT) - 0.01)
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


def _near_typo(src: Sequence[str], field_toks: Sequence[str]) -> bool:
    """A label word that is a slip of one of the field's words ('Lenght',
    'Frequncy', 'Powr', 'Meda', 'Medai', 'Datte') or its first letters
    ('Ampl'): same first letter, two letters swapped or nearly the same
    letters - stricter for a short word, where one letter is more of the word
    ('Data' is not 'Date', 'Medal' not 'Media'). 'Owner' / 'Tower' are not
    'Power', 'Partner' is not 'Part', and a short word with a letter more is
    a word of its own ('Powder') unless the letter is a doubled one or a
    plural 's' (review, 2026-10-19)."""
    for a in src:
        for b in field_toks:
            if a[0] != b[0]:
                continue
            if len(a) >= 3 and len(b) > len(a) and b.startswith(a):
                return True
            short = min(len(a), len(b))
            if short < 4:
                continue
            if len(a) == len(b):
                d = [i for i in range(len(a)) if a[i] != b[i]]
                if len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] \
                        and a[d[1]] == b[d[0]]:
                    return True                                  # 'Notse', 'Lenght'
            if len(a) > len(b) and len(b) <= 6 and a != b + "s" and not any(
                    a[i] == a[i - 1] and a[:i] + a[i + 1:] == b for i in range(1, len(a))):
                continue
            if difflib.SequenceMatcher(None, a, b).ratio() >= (0.85 if short <= 5 else 0.8):
                return True
    return False


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
#: A date typed as text: '2026-09-22', '22.09.2026', '9/22/26', '09/22' - not
#: a run's number ('EC-1', 'EC (1)', 'EC 1/2').
_DATE_TEXT = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
                        r"|(?<!\d)\d{2}/\d{2}(?!\d)")
#: Short words an inspection sheet writes where initials could stand that are
#: nobody's initials: result codes (FPI's 'NRI' no relevant indication, 'IND',
#: 'NDI', 'ACC', 'REJ', 'RJT', 'NCR'), outcomes ('OK', 'NA', 'TBD') and a
#: reading's qualifier ('31 (AVG)'). A row of 'NRI | IND' over the next table
#: was taken as an inspector's heading (review, 2026-10-19). Not 'EC': Eva
#: Chen signs as EC. council_core.kg_excel's stop list takes these too.
NOT_INITIALS = frozenset({
    "nri", "ind", "ndi", "acc", "rej", "rjt", "ncr", "ok", "na", "tbd",
    "pass", "fail", "nde", "fpi", "avg", "max", "min", "std", "nogo",
})
#: What a cell holds as a reading in words: FPI's results, an outcome.
_RESULT_WORDS = frozenset({"nri", "ind", "ndi", "rli", "rni", "acc", "rej", "rjt", "pass",
                           "fail", "ok", "ng", "nogo", "go"})
#: Units a reading's brackets hold that UNITS cannot convert: the ones
#: council_core.kg_excel's stop list has ('psi', 'rpm', 'ipm' ...) and an
#: NDE sheet's ('FSH' full screen height, 'dB', 'Vpp', 'div'). '12 (IPM)'
#: was a reading signed by IPM, and the in/min dropped (review, 2026-10-19).
_UNIT_WORDS = frozenset({
    "psi", "ksi", "mpa", "gpa", "rpm", "ipm", "fpm", "ppm", "sec", "fsh", "db", "dbm",
    "vpp", "div", "pct", "lbs", "lbf", "ghz",
})
#: What else a reading's brackets hold in capitals: a direction, a kind of
#: value or a result ('45 (CCW)', '0.5 (REF)', '54 (NG)' no good, '3 (EA)'
#: each, '0.5 (OD)'), kept with the reading - not a signer.
_NOT_SIGNERS = frozenset({"cw", "ccw", "ref", "typ", "nom", "est", "rms", "ng", "go",
                          "ea", "pc", "pcs", "nd", "pk", "pp", "id", "od"})
#: A reading signed in its cell: '31 (JB)', '31 [JB]', '0.245 mm (J.B.)'.
#: Tried on a short cell only, and '\d+(\.\d*)?' cannot split a run of digits
#: two ways: a long one made '\d+\.?\d*' backtrack (review, 2026-10-19).
_SIGNED = re.compile(r"^(?P<v>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:\s*(?:°|\"|[A-Za-zµμ/]+))?)"
                     r"\s*[\(\[]\s*(?P<who>[A-Za-z][A-Za-z.\s]{1,7})\s*[\)\]]$")
#: A reading signed without brackets: '47/JB', '48 - JB', '51-JB', '49 JB',
#: '52 JB.' - a number, then a '/' or a dash (sep) or only a space, then
#: two or three capitals. Tried on a short cell only; one way to split it.
_SIGNED_BARE = re.compile(r"^(?P<v>[+-]?(?:\d+(?:\.\d*)?|\.\d+))"
                          r"(?:\s*(?P<sep>[/\-–—])\s*|\s+)"
                          r"(?P<who>[A-Z]\.?\s?[A-Z]\.?(?:\s?[A-Z]\.?)?)$")
#: The same in small letters after a '/' or a dash, or glued on in capitals:
#: '51/jb', '52JB' - never split, only told (UNSURE_SIGNER_NOTE). Not '33
#: mV', '12 ms': a unit after a space.
_SIGNED_LOOSE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:\s*[/\-–—]\s*"
                           r"(?P<who>[A-Za-z]\.?\s?[A-Za-z]\.?(?:\s?[A-Za-z]\.?)?)"
                           r"|(?P<glued>[A-Z]{2,3}\.?))$")
#: The note on a reading that ends in initials no one signs with on its
#: sheet ('49 JB', '47/RLI'): a signer, a unit or a code? Kept as typed,
#: never pre-accepted (review, 2026-10-20).
UNSURE_SIGNER_NOTE = "ends in initials or a unit: who signed it? check it"
#: The note on a reading of a table beside another that has no label column
#: of its own: the field is the other table's label, a guess (review,
#: 2026-10-20).
_BESIDE_NOTE = "a table beside another, with no labels of its own: which field? check it"
#: A number with its unit in brackets: '0.3 (kW)', '12 (IPM)', '50 [%FSH]'.
_NUM_PAREN_UNIT = re.compile(r"^(?P<n>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*[\(\[]\s*"
                             r"(?P<u>°|\"|%?[A-Za-zµμ/]{1,8})\s*[\)\]]$")
#: A short code in capitals ('NRI', 'NG', 'NOGO'): what a row of results
#: holds beside a code that looks like initials.
_CODE_CELL = re.compile(r"[A-Z][A-Z0-9/\-]{1,5}")
#: The serial number's label - 'S/N', 'SN', 'S/N:', 'S/N #' - and nothing
#: more: 'S/N Ratio', 'S/N (dB)' are signal-to-noise. Matched on a short,
#: stripped label (single optional spaces: no backtracking).
_SN_LABEL = re.compile(r"(?i)s\s?/?\s?n\s?(?:#|no\.?)?\s?[:.]?")
#: Words a label shares with the part number's that do not make it one:
#: 'Run Number', 'Lot Number', 'Heat Number' share only 'number'.
_GENERIC_ID_WORDS = frozenset({"number", "id"})
#: Words that make an id label another kind of identifier - a lot, a heat, a
#: job, a drawing, a revision - not the part's: a 'Lot Number' column was
#: every row's part, pre-accepted, and 'Part Revision: B' named the part
#: (review, 2026-10-19). Serial, sample, specimen are the part's own.
_OTHER_ID_WORDS = frozenset({
    "lot", "heat", "melt", "batch", "run", "job", "work", "order", "wo", "po", "purchase",
    "drawing", "dwg", "revision", "rev", "version", "ver", "fixture", "tool",
    "station", "machine", "cert", "certificate", "procedure",
    "invoice", "ticket", "ncr", "step", "employee", "badge",
    "tracking", "shipment",
})
#: Whose number it is, or what it numbers: another's identifier only with no
#: word of the part's - 'Customer Number', 'Report No.', 'Sheet No.' - for
#: 'Customer P/N', 'Supplier Part Number', 'Build Part Number', 'Part
#: Number - Sheet 1' name the part (review, 2026-10-19).
_ID_OWNER_WORDS = frozenset({
    "customer", "vendor", "supplier", "build", "program", "project", "report",
    "operation", "op", "document", "doc", "file", "page", "sheet",
})
_ANY_OTHER_ID_WORDS = _OTHER_ID_WORDS | _ID_OWNER_WORDS
#: Words that say a label numbers the part itself, with only 'number' or
#: 'id' of the part number's: 'Serial No.', 'Sample Number' - a sample is
#: what a Ledger row is (its value was lost, review 2026-10-19).
_PART_OWN_WORDS = frozenset({"serial", "sample", "specimen", "coupon"})
#: Words that make a label sharing the part number's word something else of
#: the part: a heading over a block ('Part Information', 'Part Details') or
#: the part's name or kind ('Part Name: Bracket'). 'Part Information' over
#: 'Customer | Acme' made the part 'Customer', pre-accepted (review,
#: 2026-10-19).
_NOT_ID_WORDS = frozenset({
    "information", "info", "details", "detail", "data", "name", "description", "desc",
    "title", "type", "family", "status", "material", "amount", "quantity", "count",
    "weight", "location", "notes", "note", "comments", "remarks",
})
#: Another identifier's label as typed ('Lot Number', 'Heat #', 'Drawing
#: No.'): letters and a few marks, no digits or sentence marks, not opening
#: with 'No'/'Not' (a note: 'No change to program').
_ID_LABEL_SHAPE = re.compile(r"(?i)(?!no\b|not\b)[a-z][a-z.#/&\s-]{0,38}:?")
#: What a cell holds where a value is not known yet: 'TBD', 'N/A', 'n.a.',
#: '-', '?', 'pending', 'to be assigned'. Not a part number: 'TBD' was the
#: part, pre-accepted, and Apply wrote its values (review, 2026-10-19).
#: council_core.kg_excel takes it for 'nobody signed'. 'XXX', '0',
#: 'awaiting', 'later' and 'n/a (rework)' are stand-ins too: each was the
#: part, pre-accepted (review, 2026-10-19).
PLACEHOLDER = re.compile(
    r"(?i)[\s(\[#]*(?:n\s*/\s*a|n\.?\s*a\.?|[-–—]+|\?+|x+|0+|tb[adc]|none|nil|null|unknown"
    r"|pending|unassigned|awaiting(?:\s+[a-z/#]+){0,2}|later|to\s+follow"
    r"|not\s+(?:yet\s+)?(?:assigned|known|available|applicable)"
    r"|to\s+be\s+(?:assigned|determined|confirmed|advised))[\s)\].!]*"
    r"(?:[(\[][^()\[\]]{0,30}[)\]][\s.!]*)?")


def is_placeholder(v) -> bool:
    """A blank's stand-in ('TBD', 'N/A', '-'), not a value. Tried on a short
    cell only."""
    s = _text(v) if v is not None else ""
    return bool(s) and len(s) <= 40 and bool(PLACEHOLDER.fullmatch(s))


def _signed_by(text: str) -> str:
    """The initials in a reading's brackets ('J.B.' -> 'JB'), or '' when the
    brackets hold a unit ('0.245 (mm)', '(IN)', '(IPM)', '(FSH)') or a code
    ('(NA)', '(AVG)', '(NG)')."""
    raw = " ".join(text.split())
    if not (re.fullmatch(r"(?:[A-Z]\.?\s?){2,4}", raw)
            or re.fullmatch(r"(?:[A-Za-z]\.\s?){2,4}", raw)):
        return ""                         # 'Jb', 'avg', 'mm': not someone signing
    t = re.sub(r"[.\s]", "", raw).lower()
    return "" if t in UNITS or t in NOT_INITIALS or t in _NOT_SIGNERS \
        or t in _UNIT_WORDS else t.upper()


def _acronym_initials(text) -> bool:
    """'EC', 'PN', 'E.C.' typed in capitals: two or three letters that
    _ABBREV spells out into several words ('eddy current', 'part number').
    Eva Chen signs as EC: 'Inspector: EC' was no one, as tokens('EC') is
    a template's words (review, 2026-10-20). 'FREQ', 'TEMP', 'NO' shorten
    one word and stay words."""
    s = re.sub(r"[.\s]", "", str(text or ""))
    return bool(re.fullmatch(r"[A-Z]{2,3}", s)) and len(_ABBREV.get(s.lower(), "").split()) > 1


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
    found = {ex.part for ex in exs if ex.part} | {
        p for ex in exs for _r, _c, p, _h in ex.part_labels if p}
    if part:
        book_part, how = part, "the Part number you typed"
    elif len(found) == 1:
        book_part, how = next(iter(found)), "the one part number in the workbook"
    elif not found:
        book_part = file_part
        how = f"{FILE_PART_HOW} {p.name!r} — check it" if book_part else ""
    else:
        book_part, how = "", ""
    for ex in exs:
        if part:
            ex.part, ex.part_how = part, how
            ex.part_labels = []
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
        # the sheet's other parts, each from its own label down (find_part)
        more = "".join(f"; {p or 'no part yet'} from row {r} down" + (
            f" ({SHEET_PARTS_HOW}: check it)" if SHEET_PARTS_HOW in h else "")
            for i, (r, _c, p, h) in enumerate(ex.part_labels) if i or not p)
        report.sheets.append(SheetPreview(
            name=ex.g.sheet, cells=len(ex.g.cells),
            part_id=ex.part,
            part_how=(ex.part_how if ex.part == part else _fit_mask(ex.part, ex.part_how,
                                                                     mask_re)) + more,
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


def _long_paths_on() -> bool:
    """Whether this program may use paths of MAX_PATH or more: Windows 10
    1607+ with long paths enabled, asked of Windows itself (it weighs the
    policy and the program's manifest). Older Windows: never."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        f = ctypes.WinDLL("ntdll").RtlAreLongPathsEnabled
        f.restype = ctypes.c_ubyte
        return bool(f())
    except Exception:                                       # noqa: BLE001
        return False


def too_long_to_write(path) -> int:
    """The length of `path` when Windows will not let a file be written
    there - MAX_PATH (260) characters or more, long paths off - else 0."""
    if os.name != "nt":
        return 0
    n = len(os.path.abspath(str(path)))
    return n if n >= _MAX_PATH and not _long_paths_on() else 0


#: What a review path Windows cannot write is told (ledger_ingest says it
#: before a workbook is read).
REVIEW_TOO_LONG = ("review path too long ({n} characters) — enable Windows long paths "
                   "or pick a shorter folder")


#: The one problem line a run gets when its pictures cannot be saved for the
#: length of their path.
_PICTURES_TOO_LONG = "pictures not saved: path too long"


def _too_long(path, e: OSError) -> int:
    """The length of `path` when writing there failed for that length - on
    Windows with long paths off, WinError 206 for a folder of 248+
    characters and 'not found' for a file of 260+; ENAMETOOLONG elsewhere -
    else 0."""
    n = len(os.path.abspath(str(path)))
    if getattr(e, "winerror", None) == 206 or e.errno == errno.ENAMETOOLONG:
        return n
    if os.name == "nt" and n >= _MAX_PATH and isinstance(e, FileNotFoundError):
        return n
    return 0


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
        # from an ExcelOpenError of kind 'password', so the batch's roll-up
        # says 'password-protected', not 'other' (review, 2026-10-19)
        msg = (f"{path.name} has a password to open; open it in Excel, save a "
               "copy without the password, and add that copy")
        raise LedgerError(msg) from excel_open.ExcelOpenError(msg, kind="password")
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
        #: (row, col, part, how) of each part's label when the sheet has
        #: more than one part (find_part, part_at)
        self.part_labels: List[Tuple[int, int, str, str]] = []
        self._unsure_labels: set = set()                 # (row, col): _later_part
        self._signers: Optional[set] = None              # _sheet_signers
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
        # a number with its unit or its signer is short: these patterns are
        # not tried on a long cell (a padded one froze the app before)
        short = isinstance(value, str) and len(txt) <= 40
        signed = _SIGNED.match(txt.strip()) if short else None
        unsure = False
        if signed and _signed_by(signed.group("who")):
            # '31 (JB)': the reading is 31 and JB signed it - not the text
            # '31 (JB)', pre-accepted and put down to the sheet's 'Inspector:
            # MK' (review, 2026-10-19)
            txt, who = signed.group("v").strip(), _signed_by(signed.group("who"))
        elif short and match.target and self.m.schema.takes_inspectors(fname):
            # '47/JB', '48 - JB', '49 JB' were the text, pre-accepted as the
            # heading's MK's (review, 2026-10-20). Only a reading someone
            # signs ('2041-AB' is a lot), only initials (_signed_by: no unit,
            # code or stand-in), and only someone signing on this sheet
            # (_sheet_signers): after a '/' or a dash too, '45-HRC', '47/RLI',
            # '48-LOW' were signed by HRC, RLI, LOW, pre-accepted (review,
            # 2026-10-20). Else kept as typed, never pre-accepted - and so
            # are '51/jb', '52JB'.
            bare = _SIGNED_BARE.match(txt.strip())
            by = _signed_by(bare.group("who")) if bare else ""
            if by and by in self._sheet_signers():
                txt, who = bare.group("v"), by
            elif by:
                unsure = True
            else:
                loose = _SIGNED_LOOSE.match(txt.strip())
                unsure = bool(loose and _signed_by(
                    (loose.group("who") or loose.group("glued")).upper()))
        if short and match.target and match.target.unit:
            nu = _NUM_UNIT.match(txt)
            pu = _NUM_PAREN_UNIT.match(txt.strip())
            if nu and nu.group("u").lower() in UNITS:     # a unit we know, not "2x"
                txt, val_unit = nu.group("n"), nu.group("u")
            elif pu and pu.group("u").lower() in UNITS:   # '0.3 (kW)': converted below
                txt, val_unit = pu.group("n"), pu.group("u")
            elif pu and pu.group("u").lower().lstrip("%") in _UNIT_WORDS:
                # '12 (IPM)' in mm/s: kept as typed, and said so - never the
                # bare 12, pre-accepted as 12 mm/s (review, 2026-10-19)
                note = (f"unit {pu.group('u')!r} vs template {match.target.unit!r}: "
                        "not converted")
        if match.target and match.target.unit and not note:
            src_unit = val_unit or split_unit(label)[1]
            if src_unit and src_unit.lower() != match.target.unit.lower():
                f = _unit_factor(src_unit, match.target.unit)
                num = _num(txt)
                if f is not None and num is not None:
                    txt = _fmt(num * f)
                    note = f"converted {src_unit} -> {match.target.unit}"
                else:
                    note = f"unit {src_unit!r} vs template {match.target.unit!r}: not converted"
        if unsure:
            note = _join(note, UNSURE_SIGNER_NOTE)
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

    def _sheet_signers(self) -> set:
        """Initials of who signs on this sheet: its inspector headings and
        labels, and the readings signed in brackets ('41 [JB]'). Not a
        '47/RLI' itself: that would vouch for itself. Made once, when a
        reading first needs it."""
        if self._signers is None:
            got = {w for _r, _c, w in self.markers}
            for v in self.g.cells.values():
                s = v.strip() if isinstance(v, str) and len(v) <= 40 else ""
                m = _SIGNED.match(s) if s else None
                if m:
                    got.add(_signed_by(m.group("who")))
            self._signers = got - {""}
        return self._signers

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

            def person(text, acronym=False) -> str:
                """Initials or a name - never a word of the template ('Date').
                acronym: where it can only be someone, 'EC' and 'PN' are
                initials too, not 'eddy current' (_acronym_initials)."""
                w = _initials(text)
                if w and set(tokens(w)) & known and not (acronym and _acronym_initials(text)):
                    return ""
                return w
            m = _WHO_INLINE.match(v) or _WHO_BY.match(v)
            who = person(m.group("who"), acronym=True) if m else ""
            if not who and _WHO_HEAD.match(v):
                for dc in (1, 2):
                    x = self.g.cells.get((r, c + dc))
                    if x is not None:
                        # 'EC' beside 'Inspector Initials' is Eva Chen only
                        # where that pair is all its row holds: in a header
                        # row ('Inspector | PN | Flaw | EC Pre') the next
                        # column's title was the inspector and the table was
                        # lost (review, 2026-10-20)
                        who = person(x, acronym=self._lone_pair(r, c, c + dc)) \
                            if isinstance(x, str) else ""
                        if who:
                            self.used.add((r, c + dc))
                        break
            if not who:
                h = self._heading_who(v, person, self.g.cells.get((r, c + 1)))
                if h and self._heads_a_table(r, c, person):
                    who = h
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
            if h and not set(tokens(h.group(1))) & known and h.group(1) not in ("PART", "NDE") \
                    and h.group(1).lower() not in NOT_INITIALS:
                self.markers.append((0, 0, h.group(1)))

    def _lone_pair(self, r, c, cx) -> bool:
        """A label at (r, c) and its value at (r, cx) are all row r holds
        (a date and its label aside), and under the value is no reading
        ('Inspector Initials | EC' over a table titled 'Flaw | EC Post'): a
        label and its value, not two titles of a header row."""
        for c2, x in self.row(r):
            if c2 in (c, cx) or isinstance(x, date):
                continue
            s = x.strip() if isinstance(x, str) and len(x) <= 60 else ""
            if not (s and (re.match(r"(?i)^date\b", s) or _DATE_TEXT.fullmatch(s))):
                return False
        under = self.g.cells.get((r + 1, cx))
        if under is None or isinstance(under, (date, _Merged)):
            return True
        s = _text(under).strip()
        return not (isinstance(under, (int, float)) or _num(s) is not None
                    or self.codes.canon(s) or self._looks_like_id(s)
                    or _CODE_CELL.fullmatch(s) or is_placeholder(s))

    def _heading_who(self, v, person, beside=None) -> str:
        """The initials a table heading is ('JB', 'JB — 2026-09-18'), or ''.
        Never a result code ('NRI', 'IND', 'OK') nor a flaw code. beside:
        the cell right of it (its date, typed as one)."""
        if not isinstance(v, str) or len(v) > 60:
            # a heading of initials and a date is short; a long padded
            # cell made this pattern backtrack for 54 s with the GIL held
            # (review, 2026-10-19)
            return ""
        h = _WHO_HEADING.match(v.strip())
        # 'EC — 2026-09-22' is Eva Chen's heading: with its date, initials
        # spelling out a template's words are still someone's. Her readings
        # went to the heading before (review, 2026-10-20). A bare 'EC' over
        # a table is the method's title, and so are 'EC-1', 'EC: 2', 'EC
        # (1)', 'PN-1': a run's number is no date - they made EC and PN the
        # inspectors, pre-accepted (review, 2026-10-20).
        dated = bool(h) and (bool(_DATE_TEXT.search(v.strip()[h.end("who"):]))
                             or isinstance(beside, date))
        if not h or h.group("who").lower() in NOT_INITIALS \
                or not person(h.group("who"), acronym=dated) or self.codes.canon(v):
            return ""
        return h.group("who")

    def _heads_a_table(self, r, c, person) -> bool:
        """A heading, not a value: right under it a header row of 2+ labels
        that starts at its column, and nothing on its row that makes it one
        of a row of results: no number (initials typed in an 'Inspector
        Initials' column sit on a row of readings), no other short code
        ('FPI | NRI | IND', 'NG | NG | NG' over the next flaw table) unless
        that is another inspector's heading over a table of its own, side by
        side, and no row label just before it over the table ('Result |
        RLI'). Its date in any form, 'Day 1' or a title may share its row:
        only a typed '2026-09-18' could before (review, 2026-10-19)."""
        below = sorted(cc for cc, v in self.row(r + 1)
                       if isinstance(v, str) and c - 1 <= cc <= c + 8)
        if len(below) < 2 or below[0] > c + 1:
            return False
        under = {cc for cc, _v in self.row(r + 1)}
        for c2, v in self.row(r):
            if c2 == c or isinstance(v, date):            # a datetime too
                continue
            if not isinstance(v, str):
                return False
            s = v.strip() if len(v) <= 60 else ""
            if _CODE_CELL.fullmatch(s) or s.lower() in NOT_INITIALS:
                # a gap in the header row between the two: two tables
                apart = any(cc not in under for cc in range(min(c, c2) + 1, max(c, c2)))
                if not (apart and self._heading_who(v, person)):
                    return False
            elif below[0] <= c2 < c:
                return False
        return True

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
        """The sheet's part: its first part label with a value. The same
        label again lower down with another part ('Part Number: LMM3EF305'
        under the 'Part Number: LMM3EF304' block) starts that part's block:
        the second coupon's readings were put on the first, pre-accepted
        (review, 2026-10-20). See _part_blocks."""
        self.part_labels = []
        self._unsure_labels = set()
        key = None
        lead: List[Tuple[int, int, str]] = []      # 'Part Number: TBD' above the first
        for (r, c), v in sorted(self.g.cells.items()):
            if not isinstance(v, str):
                continue
            if key is not None and not self._same_id_label(v, key):
                continue
            got = self._part_label(r, c, v)
            if got is None:
                continue
            if key is None:
                if not got:
                    # 'Part Number: TBD' over the upper block: its readings
                    # were put on the lower block's part, pre-accepted
                    # (review, 2026-10-20)
                    lead.append((r, c, got))
                    continue
                self.part = got
                self.part_how = f"{self.m.schema.id_field} on {self.g.sheet}" + (
                    f", read from image {self.g.from_image}" if self.g.from_image else "")
                label = _split_inline(v)[0]
                key = (normalize_label(label), _id_label_key(label))
                self.part_labels = lead
            elif not self._later_part(r, c, v, got):
                continue
            self.part_labels.append((r, c, got))
        if key is not None:
            self._part_blocks()
            return
        sid = _part_from_name(self.g.sheet, self.mask_re)
        if sid:
            self.part = sid
            self.part_how = f"sheet name {self.g.sheet!r}"
            return
        self.part_how = ""

    def _part_label(self, r, c, v) -> Optional[str]:
        """The part a part label at (r, c) names, '' for one naming no part
        yet ('Part Number: TBD'), None when the cell is no part label."""
        label, inline = _split_inline(v)
        if self.m.is_item_header(label) or (len(label) <= 30 and _RUN_HEAD.match(label)):
            return None                        # "Flaw #", "Run #" are not "Part #"
        if len(label) <= 12 and _SN_LABEL.fullmatch(label):
            # 'S/N: LMM1AB101' names the part; an S/N column of 3.2, 4.2
            # (signal-to-noise) does not: only a part-like value typed
            # beside the label (review, 2026-10-19)
            val = inline or next((x for x in (self.g.cells.get((r, c + dc))
                                              for dc in (1, 2, 3))
                                  if x is not None and not isinstance(x, _Merged)), None)
            if self._looks_like_id(val):
                self.used.add((r, c))
                return _text(val)
            return None
        mt = self.m.match(label, per_item=False)
        if self._sure_id(label, mt):
            val = inline if inline else self._value_near(r, c)
            if val and self.codes.canon(val):
                return None                    # a flaw code is never a part number
            if is_placeholder(val):
                # 'Part Number: TBD' names no part: the sheet name, the
                # workbook, the file name or the model may (review,
                # 2026-10-19: 'TBD' was the part, pre-accepted)
                return ""
            if not inline and isinstance(val, str) and _split_inline(val)[1] \
                    and self.m.is_label(_split_inline(val)[0]):
                # 'Lot Number: AB-2041' below is a label with its own
                # value, not this one's; 'AB-12:01', 'LMM1AB101 Rev: B'
                # are part numbers with a colon in them (review,
                # 2026-10-19: any colon lost the part)
                return None
            if val and not self.m.is_label(val):
                if mt.via != "alias" and self.m.names_other(label) \
                        and not self._looks_like_id(val):
                    return None                # 'Part No. Rev: B' is the revision
                if not self._is_table_header(r, c):
                    return _text(val)
        return None

    def _same_id_label(self, v, key) -> bool:
        """v is the sheet's first part label typed again, told cheaply - a
        match() for every cell of a 5,000-row sheet would be the freeze
        again: the same text, or a short label of the same words ('Part
        No.:', 'P/N:' under 'Part Number:' - its readings stayed on the
        first part, review 2026-10-20). 'Serial No.' under 'Part Number' is
        another of the part's labels, not another part."""
        label = _split_inline(v)[0] if len(v) <= 200 else ""
        if not label or len(label) > 60:
            return False
        if normalize_label(label) == key[0]:
            return True
        return len(label) <= 30 and bool(_ID_LABEL_SHAPE.fullmatch(label)) \
            and _id_label_key(label) == key[1]

    def _later_part(self, r, c, v, got) -> bool:
        """A part label after the sheet's first starts a block only when its
        value is shaped like a part ('LMM3EF305', your part-name pattern) or
        names none yet, and it is no column's title: 'Part Number | Status |
        Notes' over a table of parts made 'Status' the part of every reading
        below it, pre-accepted, and lost the workbook its one part (review,
        2026-10-20). A table's own Part Number column says each row's part.
        One on a row of other titles or labels is kept, never sure."""
        if got and not self._looks_like_id(got):
            return False
        if _split_inline(v)[1]:
            return True                        # 'Part Number: LMM3EF305' in one cell
        if self._looks_like_id(self.g.cells.get((r + 1, c))):
            return False                       # a column of part numbers under it
        if any(isinstance(x, str) and x.strip() and _text(x).strip() != got
               for c2, x in self.row(r) if c2 != c):
            self._unsure_labels.add((r, c))
        return True

    def _part_blocks(self) -> None:
        """More than one part on the sheet: each part label's block is the
        rows from it down to the next one's (part_at). Only when every later
        label is below the one before, with readings between them, are the
        blocks sure; two labels on one row, two typed one under the other
        with nothing between ('Part Number: X', 'Part Number: Y' over one
        table) or one naming no part yet ('TBD') over readings, above or
        below, leave every value of the sheet to check, never pre-accepted
        (SHEET_PARTS_HOW). A 'TBD' over an empty block is a spare coupon's,
        left blank: nothing to check - the whole sheet went amber (review,
        2026-10-20)."""
        labels, self.part_labels = self.part_labels, []
        labels = [x for x in labels if x[2] or self._readings_after(x[0], labels)]
        if len({p for _r, _c, p in labels}) < 2:
            return                             # one part, typed once or again
        blocks = [labels[0]]
        unclear = not labels[0][2]             # readings under a 'TBD' above the part
        for r, c, p in labels[1:]:
            pr, _pc, pp = blocks[-1]
            if p == pp:
                continue                       # the same part again
            if not p or r == pr or not self._readings_between(pr, r) \
                    or (r, c) in self._unsure_labels:
                unclear = True
            blocks.append((r, c, p))
        from openpyxl.utils import get_column_letter
        self.part_labels = [
            (r, c, p, (f"{self.m.schema.id_field} at {self.g.sheet}!{get_column_letter(c)}{r}"
                       + ("" if p else ", no part yet") if i or not p else self.part_how)
             + (f" — {SHEET_PARTS_HOW}: check it" if unclear else ""))
            for i, (r, c, p) in enumerate(blocks)]

    def _readings_after(self, r, labels) -> bool:
        """Readings in the block of the part label at row r, down to the
        next label's row: a number, typed or as text ('47/JB'), or a result
        ('NRI', 'IND'). An empty block's flaw codes and labels are none."""
        end = min((x[0] for x in labels if x[0] > r), default=max(self.rows, default=r) + 1)
        for rr in range(r + 1, end):
            for _c, v in self.row(rr):
                if isinstance(v, (bool, _Merged)):
                    continue
                if isinstance(v, (int, float)):
                    return True
                s = v.strip() if isinstance(v, str) and len(v) <= 20 else ""
                if s and (re.match(r"[+-]?\.?\d", s) and not self.codes.canon(s)
                          or s.lower() in _RESULT_WORDS):
                    return True
        return False

    def _readings_between(self, r1, r2) -> bool:
        """A number or a flaw code on a row between r1 and r2: the block of
        the part label at r1 has readings of its own."""
        for rr in range(r1 + 1, r2):
            for _c, v in self.row(rr):
                if isinstance(v, (int, float)) and not isinstance(v, bool) \
                        or self.codes.canon(v):
                    return True
        return False

    def part_at(self, r, c) -> Tuple[str, str]:
        """(part, how) of a value at (r, c): the part label's at or above
        its row - of two on that row the one at or left of its column. how
        is '' on a sheet of one part (its part_how says)."""
        if not self.part_labels:
            return self.part, ""
        above = [x for x in self.part_labels if x[0] <= r]
        if not above:
            # above the first part label: the sheet's part, as before - but
            # never sure when the sheet's parts are not
            first = self.part_labels[0]
            return self.part, (first[3] if SHEET_PARTS_HOW in first[3] else "")
        top = max(x[0] for x in above)
        row = [x for x in above if x[0] == top]
        left = [x for x in row if x[1] <= c]
        pick = max(left, key=lambda x: x[1]) if left else min(row, key=lambda x: x[1])
        return pick[2], pick[3]

    def _sure_id(self, label, mt: Match) -> bool:
        """A label that IS the part number's: an alias, a sure match, or one
        sharing a word with it ('Part No.', 'P/N') or saying serial or sample
        ('Serial No.', 'Sample Number'). A 'Parameter | Value' table matched
        'Part Number' at 0.6 and made every part 'Value'; 'Partner' did too;
        and 'Run #', 'Lot Number',
        'Heat Number' share only the word 'number' (review, 2026-10-19). One
        naming another kind of identifier ('Part Revision') is not it either
        (Matcher.other_id), nor a heading or the part's name ('Part
        Information', 'Part Name': _NOT_ID_WORDS), unless you mapped it.

        `mt` is the label's match without context: a heading's word above a
        table ('Part Number: LMM1AB101') lifted 'P.O. Number' to 0.9, a sure
        match, and every row's part was 1, 2 ... (review, 2026-10-19)."""
        if not (mt.target and mt.target.is_id and mt.score >= SUGGEST_AT):
            return False
        if mt.via == "alias":
            return True
        if self.m.other_id(label):
            return False                   # 'Part Revision', 'Lot Number'
        words = set(tokens(label))
        if (words - self.m._id_tok) & _NOT_ID_WORDS:
            return False                   # 'Part Information', 'Part Name'
        return mt.score >= ACCEPT_AT or bool(words & _PART_OWN_WORDS) or bool(
            (words & set(self.m._tok[mt.target.name])) - _GENERIC_ID_WORDS)

    def _is_id_label(self, label) -> bool:
        """_sure_id for a table's or a form's label, matched without the
        headings above it."""
        return self._sure_id(label, self.m.match(label, per_item=False))

    def _id_column(self, r, c, label) -> bool:
        """Column c under header row r is the part number's: its label is
        the part number's (_is_id_label) - and when the label names another
        identifier as well ('Part No./Rev'), the column holds part numbers,
        not 'A', 'B'."""
        mt = self.m.match(label, per_item=False)
        if not self._sure_id(label, mt):
            return False
        if mt.via == "alias" or not self.m.names_other(label):
            return True
        vals = [v for v in (self.g.cells.get((rr, c)) for rr in range(r + 1, r + 4))
                if v is not None and not is_placeholder(v)]
        return not vals or any(self._looks_like_id(v) for v in vals)

    def _looks_like_id(self, val) -> bool:
        """A part's serial number, not a signal-to-noise reading: the user's
        part shape, letters with digits ('LMM1AB101', 'SN-0042') or a whole
        number of 3+ digits - never '3.2', '12', '12 dB' or a flaw code."""
        s = _text(val).strip() if val is not None else ""
        if not s or len(s) > 40 or self.codes.canon(s) or is_placeholder(s):
            return False
        if self.mask_re is not None and self.mask_re.search(s):
            return True
        if _NUM_UNIT.match(s):
            return False                                   # '12 dB'
        return bool(re.search(r"[A-Za-z]", s) and re.search(r"\d", s)
                    or re.fullmatch(r"\d{3,}", s))

    def _is_table_header(self, r, c) -> bool:
        hits = sum(1 for c2, v in self.row(r)
                   if c2 != c and isinstance(v, str)
                   and (self.m.match(v).score >= SUGGEST_AT or self.m.is_item_header(v)
                        or self.m.other_id_label(v)))
        return hits >= 1 and self.m.is_label(self.g.cells.get((r, c + 1)))

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
                              if self._labels_below(r, cc)), None)
            if label_col is None:
                continue
            for c, _ in hits:
                self.used.add((r, c))
            who_cols = self._who_columns(r, {c for c, _ in hits} | {label_col})
            ctx = self.context(r)
            # Tables side by side on the header row (a Pre-Conceal grid in
            # B:E, a Post-Conceal grid in H:K): each reads its own label
            # column. The right one's readings were filed under the left
            # one's field, and their per-inspector copies pre-accepted
            # (review, 2026-10-20).
            blocks, cols = self._grid_blocks(r, hits)
            cols[0] = label_col
            # who columns between two tables are the right one's from the
            # empty spacer column between them on: 'Inspector Initials'
            # typed left of a table's labels was the left table's, and the
            # right one's readings had no inspector (review, 2026-10-20)
            starts = [0] + [self._block_start(r, left[-1][0], lc or b[0][0])
                            for left, lc, b in zip(blocks, cols[1:], blocks[1:])] + [1 << 30]
            for i, block in enumerate(blocks):
                lc = cols[i]
                guess = lc is None
                if guess:
                    # its own header but no label column of its own: the
                    # one beside it is a guess, never pre-accepted
                    lc = next(x for x in reversed(cols[:i]) if x is not None)
                self._grid(r, block, lc, ctx, [wc for wc in who_cols
                                               if starts[i] <= wc < starts[i + 1]], guess)

    def _labels_below(self, r, cc) -> bool:
        return any(self.g.text(rr, cc) for rr in range(r + 1, r + 12))

    def _grid_blocks(self, r, hits) -> Tuple[List[List[Tuple[int, str]]], List[Optional[int]]]:
        """A header row's item codes split into the tables side by side, and
        each later table's own label column (None: it has none). A column
        between two codes starts another table when it is titled like a
        grid's own ('Flaw #') or its first row of readings is a template
        field's label ('EC Post-Conceal'). An 'Inspector Initials' column, a
        summary ('Avg'), a note ('ok') or an empty spacer column between
        two groups of codes is the same table: each split one, the right
        group's readings lost to Labels to map, or its inspectors, or
        amber (review, 2026-10-20)."""
        first = next((rr for rr in range(r + 1, r + 12)
                      if any((rr, c) in self.g.cells for c, _ in hits)), r + 1)
        blocks: List[List[Tuple[int, str]]] = [[hits[0]]]
        own: List[Optional[int]] = [None]
        for (c0, _), (c1, it) in zip(hits, hits[1:]):
            gap = range(c1 - 1, c0, -1)
            heads = [cc for cc in gap if self.m.is_item_header(self.g.text(r, cc) or "")]
            lab = next((cc for cc in heads if self.g.text(first, cc)), None) \
                or next((cc for cc in gap if self._grid_label_col(r, first, cc)), None)
            if heads or lab is not None:
                blocks.append([])
                own.append(lab)
            blocks[-1].append((c1, it))
        return blocks, own

    def _grid_label_col(self, r, first, cc) -> bool:
        """Column cc's first row of readings is a template field's label, and
        its title is not 'Inspector Initials' (where 'EC' is Eva Chen)."""
        head = self.g.text(r, cc)
        t = self.g.text(first, cc)
        if not t or len(t) > 60 or (head and _WHO_HEAD.match(head)):
            return False
        mt = self.m.match(t, per_item=True)
        return mt.target is not None and mt.score >= SUGGEST_AT

    def _block_start(self, r, last, lc) -> int:
        """The first column of the table whose labels are in column lc,
        beside one whose last code is in column `last`: right of the empty
        column between them on header row r, else lc."""
        for cc in range(lc - 1, last, -1):
            if (r, cc) not in self.g.cells:
                return cc + 1
        return lc

    def _grid(self, r, hits, label_col, ctx, who_cols, guess=False) -> None:
        """One item grid: codes `hits` on header row r, labels down
        label_col. guess: the labels are another table's (_grid_blocks)."""
        self.used.add((r, label_col))                # the grid's own "Flaw #" cell
        # Each code's own 'Inspector Initials' column: of two in one grid
        # ('1A 2A 3A | Inspector Initials | 1B 2B 3B | Inspector Initials')
        # the nearest after it - before it where they come first. The first
        # one's initials were every group's (review, 2026-10-20).
        before = any(wc < hits[0][0] for wc in who_cols)
        order = {c: sorted(who_cols, key=lambda wc, c=c: ((wc > c) if before else (wc < c),
                                                          abs(wc - c)))
                 for c, _ in hits}
        rr = r + 1
        blanks = 0
        while blanks < 2 and rr < r + 60:
            self._tick()
            label = self.g.text(rr, label_col)
            if label is None:
                blanks += 1
                rr += 1
                continue
            if sum(1 for c, _ in hits if self.codes.canon(self.g.cells.get((rr, c)))) \
                    >= min(3, len(hits)):
                break                              # the next grid's header
            blanks = 0
            self.used.add((rr, label_col))
            mt = self.m.match(label, ctx, per_item=True)
            if guess and mt.target is not None:
                mt = Match(mt.target, mt.score, mt.guesses, via="ambiguous")
            for c, it in hits:
                if (rr, c) in self.g.cells:
                    who = next((w for w in (_initials(self.g.cells.get((rr, wc)))
                                            for wc in order[c]) if w), None)
                    part, how = self.part_at(rr, c)
                    o = self.emit(part, it, label, rr, label_col,
                                  self.g.cells[(rr, c)], mt, vr=rr, vc=c, who=who)
                    if o is not None:
                        o.part_how = how or o.part_how
                        if guess:
                            o.note = _join(o.note, _BESIDE_NOTE)
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

    def _sub_heads(self, r, cols) -> Dict[int, str]:
        """A two-row header: a parent merged over some columns ('Eddy
        Current Response' over B:C) and a label under each of them ('Pre-
        Conceal' | 'Post-Conceal'). {column: its label under the parent},
        or {} when row r+1 is not that. The Post column was filed as Pre-
        Conceal: both read 'Eddy Current Response' (review, 2026-10-20)."""
        if len({self.g.text(r, c) for c in cols}) < 2:
            return {}                          # one title merged over a table
        spans = set()
        for c in cols:
            v, nxt = self.g.cells.get((r, c)), self.g.cells.get((r, c + 1))
            if isinstance(nxt, _Merged) and c + 1 in cols and str(nxt) == str(v):
                spans.update((c, c + 1))
        out: Dict[int, str] = {}
        for c in cols:
            x = self.g.cells.get((r + 1, c))
            if x is None or isinstance(x, _Merged) and c not in spans:
                continue                       # a header of one row, or merged down
            if c not in spans or not isinstance(x, str) or isinstance(x, _Merged) \
                    or len(x) > 60 or _num(x) is not None or self.codes.canon(x) \
                    or is_placeholder(x) or x.strip().lower() in _RESULT_WORDS:
                # a row of readings, not of labels: 'KP | NRI | ok' under
                # 'Inspector | FPI Result' (merged) was a header, and lost
                # (review, 2026-10-20)
                return {}
            out[c] = x.strip()
        return out if spans and set(out) == spans else {}

    def _parents_above(self, r, cols) -> Dict[int, str]:
        """Header row r's columns under a parent merged over some of them on
        the row above, which holds nothing else: 'Eddy Current Response'
        over B:C, with 'Flaw | Pre-Conceal | Post-Conceal | FPI' all on row
        r. {column: its parent}, or {}: a title merged over the whole
        header is not a parent. The table was lost to Labels to map
        (review, 2026-10-20)."""
        lo, hi = cols[0], cols[-1]
        out: Dict[int, str] = {}
        for c, v in self.row(r - 1):
            if c < lo or c > hi:
                continue
            if isinstance(v, _Merged):
                if out.get(c - 1) != str(v):
                    return {}                  # merged in from outside the header
                out[c] = str(v)
                continue
            nxt = self.g.cells.get((r - 1, c + 1))
            if not isinstance(v, str) or len(v) > 60 or not isinstance(nxt, _Merged) \
                    or str(nxt) != v or _num(v) is not None or self.codes.canon(v) \
                    or (r - 1, c) in self.used or _split_inline(v)[1]:
                # a cell of its own, or a label with its value ('Part
                # Number: X', 'Inspector: KP' merged over two): no parents
                return {}
            out[c] = v
        if not out or not set(out) < set(cols) \
                or isinstance(self.g.cells.get((r - 1, max(out) + 1)), _Merged):
            return {}
        return {c: " ".join(p.split()) for c, p in out.items()}

    def _table(self, r, cols) -> None:
        if len(cols) < 2:
            return
        ctx = self.context(r)
        heads = {}
        item_col = who_col = run_col = value_col = None
        ids: List[Tuple[float, int]] = []
        lookalike = False
        below = next((run for run in self._header_runs(r + 1, sorted(
            c for c, v in self.row(r + 1) if isinstance(v, str) and (r + 1, c) not in self.used))
            if cols[0] in run), [])
        if below and set(cols) <= set(self._parents_above(r + 1, below)):
            return                                # parents over the header under it
        sub = self._sub_heads(r, cols)
        above = {} if sub else self._parents_above(r, cols)
        top = r + 1 if sub else r                 # the header's last row
        for c in cols:
            v = self.g.text(r, c)
            if c in sub:
                v = f"{v} {sub[c]}"               # 'Eddy Current Response Post-Conceal'
            elif c in above:
                v = f"{above[c]} {v}"
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
                if self._id_column(r, c, v):
                    ids.append((mt.score, c))
                    continue
                # like the Part Number's but not it ('P.O. Number', 'Item No.',
                # 'Part Name'): an ordinary column, its values offered to map.
                # It was every row's part, 1, 2 ..., pre-accepted, over the
                # sheet's own 'Part Number: ...' (review, 2026-10-19).
                lookalike = True
                mt = Match(None, mt.score, mt.guesses, via="none")
            heads[c] = (v, mt)
        # the best Part Number column wins; a weaker one is an ordinary column
        id_col = max(ids)[1] if ids else None
        for score, c in ids:
            if c != id_col:
                heads[c] = (self.g.text(r, c), Match(None, score, via="none"))
        if item_col is None:                     # a column of item codes?
            for c in cols:
                vals = [self.g.cells.get((rr, c)) for rr in range(top + 1, top + 6)]
                if sum(1 for v in vals if self.codes.canon(v)) >= 2:
                    item_col = c
                    heads.pop(c, None)
                    break
        per_item = item_col is not None
        data_rows = self._data_rows(top, cols, item_col)
        if not data_rows:
            return
        stage_col = self._stage_col(heads, data_rows, ctx) \
            if per_item and value_col is not None else None
        matched = [c for c, (_, mt) in heads.items() if mt.target]
        if len(matched) + (item_col is not None) + (id_col is not None or lookalike) \
                + (stage_col is not None) < 2:
            return
        # values under a header must not be more labels
        filled = sum(1 for c in heads for d in data_rows[:3] if (d, c) in self.g.cells)
        if heads and filled == 0 and stage_col is None:
            return
        for c in cols:
            self.used.update(((r, c), (top, c)))
        self.used.update((r - 1, c) for c in above)
        if stage_col is not None:
            self._long_rows(r, data_rows, item_col, id_col, who_col, stage_col,
                            value_col, ctx)
            return
        final = {}
        for c, (label, _) in heads.items():
            mt = self.m.match(label, ctx, per_item=per_item)
            if mt.target is not None and mt.target.is_id:
                # a column that is not the part's ('Customer P/N' beside
                # 'Part Number', 'P.O. Number'): never a Part Number value -
                # Apply put that in the row's Part Number (review, 2026-10-19)
                mt = Match(None, mt.score, mt.guesses, via="none")
            final[c] = (label, mt)
        runs_seen: Dict[str, int] = defaultdict(int)
        for d in data_rows:
            self._tick()
            part, part_how = self._row_part(d, id_col) if id_col else self.part_at(d, cols[0])
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
                if o is not None and part_how:
                    o.part_how = part_how
                self.used.add((d, c))
            for c in (id_col, item_col, who_col, run_col):
                if c:
                    self.used.add((d, c))

    def _row_part(self, d, id_col) -> Tuple[str, str]:
        """(part, how) of table row d from its Part Number cell. A placeholder
        ('TBD', 'N/A', '-') is no part, as a blank cell is: 'TBD' was the
        row's part, pre-accepted (review, 2026-10-19)."""
        v = self.g.cells.get((d, id_col))
        how = f"{self.m.schema.id_field} column on {self.g.sheet}"
        if is_placeholder(v):
            return "", f"{how}: {_text(v).strip()!r} is not a part number"
        return _text(v), how

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
            part, part_how = self._row_part(d, id_col) if id_col \
                else self.part_at(d, item_col)
            label = f"{head} {stage}".strip()
            mt = self.m.match(label, ctx, per_item=True)
            o = self.emit(part, item, label, d, stage_col, self.g.cells[(d, value_col)], mt,
                          vr=d, vc=value_col,
                          who=_initials(self.g.cells.get((d, who_col))) or None
                          if who_col else None)
            if o is not None and part_how:
                o.part_how = part_how
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
                if self._is_id_label(label):
                    self.used.add((r, c))      # find_part's: the part, or none
                    continue
                # 'P.O. Number', 'Mold Number', 'Part Name' look like the
                # Part Number's label and are not it: their value went
                # nowhere, not even to Labels to map (review, 2026-10-19)
                mt = Match(None, mt.score, mt.guesses, via="none")
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
                            self.m.is_label(x) or self.codes.canon(x)
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
            part, how = self.part_at(r, c)
            o = self.emit(part, "", label, r, c, val, mt, field_name=fname, vr=vr, vc=vc)
            if o is not None and how:
                o.part_how = how


def _split_inline(v: str) -> Tuple[str, str]:
    """'Operator: AK' -> ('Operator', 'AK'); OCR's 'PartNumber:2041' too.
    Times like 10:30 stay whole: the label must have a letter in it."""
    m = re.match(r"^\s*([^:=]{1,60}?)\s*[:=]\s*(.+)$", v)
    if m and re.search(r"[A-Za-z]", m.group(1)) and not m.group(2).startswith(("/", "\\")):
        return m.group(1), m.group(2).strip()
    return v.strip(), ""


def _id_label_key(label: str) -> Tuple[str, ...]:
    """What a part label says, however spelt: 'Part Number', 'Part No.',
    'P/N', 'PART #' are ('part', 'number'); 'S/N', 'SN #' are the serial."""
    s = label.strip()
    if len(s) <= 12 and _SN_LABEL.fullmatch(s):
        return ("s/n",)
    return tuple(tokens(s)) or (normalize_label(s),)


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
    # Cancelled only when a workbook was skipped or cut short (_extract_all
    # says so): a Cancel pressed after the last one was read made a whole
    # review 'Cancelled… partial review' (review, 2026-10-19). One that cut
    # a read short in a way the read swallowed is caught after the read
    # and after the model's question (_extract_one).
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


def _excel_down_line(problems: List[str], msg: str, name: str) -> None:
    """Excel could not be started (excel_open: the rest of the extract does
    not try again): one problem line for the run - `msg`, the same for every
    workbook - naming each workbook it left unread, not one line per
    workbook (review, 2026-10-20)."""
    head = f"{msg} (not read: "
    for i, p in enumerate(problems):
        if p.startswith(head) and p.endswith(")"):
            problems[i] = f"{p[:-1]}, {name})"
            return
    problems.append(f"{head}{name})")


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
        msg = (str(e) if isinstance(e, LedgerError)
               else f"{src.name}: could not be read ({e!r})")
        # Keep the Excel-open-failure category so the Ledger log can
        # summarize the batch: "N files failed to open - K rights, ...".
        cause = e.__cause__ if isinstance(e, LedgerError) else e
        kind = (getattr(cause, "kind", "unknown")
                if cause is not None and type(cause).__name__ == "ExcelOpenError"
                else "other")
        if kind == "unavailable" and getattr(cause, "down", ""):
            _excel_down_line(run.problems, msg, label or src.name)
        else:
            run.problems.append(msg)
        run.open_failures.append((label or src.name, kind))
        _secs = _time.monotonic() - _t0
        run.timings.append((label or src.name, _secs))
        prog.advance(what=f"{src.name}: not read ({_secs:.1f}s)")
        return
    if cancel is not None and cancel.is_set():
        # pressed while Excel read it: what came back may be cut short
        # (a picture Excel did not copy out is a note, not an error)
        raise _Cancelled()
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
            # every part cleaned: a sheet, anchor or extension may hold
            # characters Windows refuses in a file name ("?" did)
            stem = Path(label).with_suffix("").as_posix() if label else src.stem
            p = d / (f"{_safe(stem)}_{_safe(img.sheet)}_{_safe(img.anchor)}_{i + 1}"
                     f".{_safe(img.ext) or 'png'}")
            made = not d.exists()
            try:
                # the folder is made here, for a picture being saved: made
                # up front, a review folder path of 248+ characters failed
                # the whole Extract with WinError 206, pictures ticked or
                # not (review, 2026-10-19)
                d.mkdir(parents=True, exist_ok=True)
                p.write_bytes(img.data)
                img.saved_as = str(p)
            except OSError as e:          # one picture never stops the run
                if made:
                    try:
                        d.rmdir()         # nothing was saved in it
                    except OSError:
                        pass
                n = _too_long(p, e)
                if not n:
                    run.problems.append(f"{src.name} › {img.sheet}: a picture could not be "
                                        f"saved ({e.strerror or e}); it is still read")
                elif not any(x.startswith(_PICTURES_TOO_LONG) for x in run.problems):
                    run.problems.append(f"{_PICTURES_TOO_LONG} ({n} characters) — enable "
                                        "Windows long paths or move the folder")
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
    # a sheet's other parts too: a sheet holding two is no workbook of one
    found = {ex.part for ex in exs if ex.part} | {
        p for ex in exs for _r, _c, p, _h in ex.part_labels if p}
    if part:
        book_part, how = part, "given on the command line"
    elif len(found) == 1:
        book_part, how = found.pop(), "the one part number in the workbook"
    elif not found:
        book_part = _part_from_filename(src.stem, mask_re)
        how = f"{FILE_PART_HOW} {src.name!r} — check it" if book_part else ""
    else:
        book_part, how = "", ""
    if part_finder is not None and not part and not book_part and any(
            not ex.part for ex in exs):
        here["what"] = f"{src.name}: asking Council's model which part it is"
        prog.update(here["what"], force=True)
        found_part, where = part_finder(label or src.name, grids)
        _wd.beat()
        if cancel is not None and cancel.is_set():
            raise _Cancelled()                # the model's answer was cut short
        if found_part:
            book_part, how = found_part, f"{MODEL_PART_HOW} from {where} — check it"
    for ex in exs:
        if part:
            ex.part, ex.part_how = part, how
            ex.part_labels = []                # the Part you typed: every value's
        elif not ex.part:
            ex.part, ex.part_how = book_part, how
        here["what"] = f"{src.name}: finding values on sheet {ex.g.sheet!r}"
        prog.update(here["what"])
        ex.item_grids()
        ex.tables()
        ex.forms()
        for o in ex.out + ex.unmatched:
            o.part_how = o.part_how or ex.part_how
            if o.part != part:                       # not the Part you typed
                o.part_how = _fit_mask(o.part, o.part_how, mask_re)
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
    if mask_re is not None:
        return ""          # the user said what a part looks like; this is not one
    # a number on its own ('101 inspection', 'line3_2048'), never one inside a
    # longer word: 'week41' made part 41 and 'LMM1AB101' part 101, pre-accepted
    # (review, 2026-10-19). Two numbers: no guess.
    if len(re.findall(r"\d{2,8}", stem)) != 1:
        return ""
    nums = re.findall(r"(?<![A-Za-z\d.])\d{2,8}(?![A-Za-z\d.])", stem)
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
    groups: Dict[Tuple[str, str, str, str], List[Obs]] = defaultdict(list)
    for o in run.obs:
        # A value with no part is its own workbook's: two part-less workbooks
        # with the same Frequency were one review row ('also at ...'), the
        # reviewer typed one part in it, and the other sample lost the value
        # at Apply (review, 2026-10-19).
        book = "" if o.part else o.where.split(" › ", 1)[0]
        groups[(o.part, o.item, o.field, book)].append(o)
        if o.inspector:
            own = replace(o, field=inspector_column(o.field, o.inspector))
            groups[(o.part, o.item, own.field, book)].append(own)
    out = []
    for (part, item, fname, _book), obs in groups.items():
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
                  # nor a reading ending in initials no one signs with here
                  # ('49 JB'), nor one of a table with no labels of its own
                  and UNSURE_SIGNER_NOTE not in o.note and _BESIDE_NOTE not in o.note
                  and not o.part_how.startswith(MODEL_PART_HOW)
                  # a part read from the file name says '— check it': it is
                  # (digits of 'EC_log_week41' were pre-accepted as part 41)
                  and not o.part_how.startswith(FILE_PART_HOW)
                  # nor one that is not the shape of your part-name pattern
                  and MISFIT_PART_HOW not in o.part_how
                  # nor one of a sheet whose parts' blocks are not clear
                  and SHEET_PARTS_HOW not in o.part_how)
            out.append(Decision(o, ok, text))
    item_rank = {it: i for i, it in enumerate(schema.items)}
    order = {f.name: i for i, f in enumerate(schema.fields)}
    out.sort(key=lambda d: (sample_sort_key(d.obs.part), item_rank.get(d.obs.item, -1),
                            order.get(d.obs.field, 1e9), -d.obs.score))
    return out


#: How a part Council's model suggested is described (never pre-accepted).
MODEL_PART_HOW = "suggested by Council's model"
#: How a part read from the workbook's file name is described (never
#: pre-accepted either).
FILE_PART_HOW = "file name"
#: Added to how a part was found when you gave a part-name pattern and the
#: part does not fit it: kept, never pre-accepted (review, 2026-10-19).
MISFIT_PART_HOW = "does not fit the part-name pattern"
#: Added to how a part was found on a sheet holding more than one part
#: whose blocks are not clear (two part labels on one row, or one under
#: the other with no readings between): kept, never pre-accepted (review,
#: 2026-10-20).
SHEET_PARTS_HOW = "more than one part number on this sheet"


def _fit_mask(part: str, how: str, mask_re: "Optional[re.Pattern]") -> str:
    """`how`, saying so when the user's part-name pattern is set and `part`
    is not of that shape ('AB-2041' for '@@@#@@###')."""
    if mask_re is None or not part or MISFIT_PART_HOW in how \
            or mask_re.fullmatch(part.strip()):
        return how
    return f"{how} — {MISFIT_PART_HOW}" if how else MISFIT_PART_HOW


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
    except OSError as e:
        n = _too_long(path, e)
        if not n:
            raise
        # a raw FileNotFoundError [Errno 2] after every workbook was read
        # (review, 2026-10-19)
        raise LedgerError(f"could not write {path.name}: "
                          + REVIEW_TOO_LONG.format(n=n)) from e


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
    # never the part-number field, as in the extract (ledger_llm.place_labels):
    # the Preview offered 'Part Number' for a label the extract would not
    # put there (review, 2026-10-19)
    id_field = getattr(schema, "id_field", None)
    fields = [f.name for f in schema.fields if not id_field or f.name != id_field]
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
