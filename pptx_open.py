"""
pptx_open.py — a PowerPoint deck read as the Ledger reads a workbook (user,
2026-10-21: "a powerpoint with slides in it with an image, textbox, and
multiple tables. The Textbox gives a hint for what the tables store").

Each TABLE on a slide becomes a small sheet of its own: the slide's title
and the slide-wide text on top, then the text boxes that belong to that
table (the nearest one, above or to the left first), one line per
paragraph, then a blank row, then the table. Both Ledgers then read it as
they read a block on a sheet: the sample Ledger takes a heading above a
table as its section ("Visual Inspection") and a line like "Inspector: JB"
or "JB — 2026-09-18" as who signed it; the table Ledger takes "Operator:
JB" as that block's label. A slide with no table is one sheet of its text.

Pictures are handed back for the Ledger's image reader (OCR or a local
vision model), named for their slide and the text nearest them, so a
section word or initials there still reach the matcher. Charts are not
read: a chart is drawn from data that is elsewhere (user, 2026-10-20). A
fill on a table cell or a text box (or a highlighted run) is kept as that
cell's colour: the highlighter works in PowerPoint too.

Only the standard library: the .pptx is a zip of XML. .ppt (the old binary
format) is not read; save it as .pptx.
"""
from __future__ import annotations

import json
import posixpath
import re
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
R_ID = "{%s}id" % NS["r"]
R_EMBED = "{%s}embed" % NS["r"]
TABLE_URI = "http://schemas.openxmlformats.org/drawingml/2006/table"
SUFFIXES = (".pptx", ".pptm")

#: text this far from every table (as a share of the slide's height) is the
#: slide's, not one table's
FAR = 0.35
#: shapes read from one slide at most (a slide of thousands of shapes is a
#: drawing, not a report)
MAX_SHAPES = 2000
MAX_TABLE_CELLS = 100_000


@dataclass
class Box:
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    known: bool = False


@dataclass
class TextShape:
    lines: List[str]
    box: Box
    title: bool = False
    colour: str = ""              # its fill
    colours: List[str] = field(default_factory=list)      # per line (a highlighted run)
    outline: str = ""             # its line's colour
    groups: Tuple[int, ...] = ()  # the groups it is in, outermost first


@dataclass
class TableShape:
    rows: List[List[str]]
    fills: Dict[Tuple[int, int], str]
    box: Box
    name: str = ""
    borders: set = field(default_factory=set)              # its cells' border colours
    groups: Tuple[int, ...] = ()

    @property
    def marks(self) -> set:
        """The colours a text box may share with it to say it belongs to it."""
        return set(self.fills.values()) | set(self.borders)


#: how a text box was paired with its table(s)
BY_GROUP, BY_COLOUR, BY_TITLE, BY_ONLY = "group", "colour", "title", "only table"
BY_PLACE, BY_SLIDE = "place", "slide"
BY_USER = "your link"         # set in the Ledger's click-to-link window
#: the pairings that are the user's own (a group, a shared colour, a link)
#: or not a guess at all; any other pairing on a slide of several tables is
#: a guess
SURE_PAIRINGS = (BY_GROUP, BY_COLOUR, BY_TITLE, BY_ONLY, BY_USER)


# ── the user's own pairings (the click-to-link window) ──────────────────
#: deck file name -> slide number (as text) -> text key -> the tables (1, 2
#: ...) it goes with; [] = the whole slide's
Pairings = Dict[str, Dict[str, Dict[str, List[int]]]]
_LOCAL = threading.local()
try:        # a guarded read runs on a thread of its own (council_core.file_guard)
    from council_core import file_guard as _file_guard
    _file_guard.carry(_LOCAL)
except Exception:                                           # noqa: BLE001
    pass


def pairings_path_for(csv_path) -> Path:
    return Path(csv_path).with_suffix(".pairings.json")


def load_pairings(csv_path) -> Pairings:
    p = pairings_path_for(csv_path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}
    return data.get("decks", {}) if isinstance(data, dict) else {}


def save_pairings(csv_path, pairings: Pairings) -> None:
    from sample_ledger import _atomic_write_text
    _atomic_write_text(pairings_path_for(csv_path),
                       json.dumps({"version": 1, "decks": pairings}, indent=1,
                                  ensure_ascii=False))


@contextmanager
def user_pairings(csv_path):
    """Decks read inside this (on this thread) use the Ledger's own
    pairings: Extract, Apply and Teach each read under it."""
    old = getattr(_LOCAL, "pairings", None)
    _LOCAL.pairings = load_pairings(csv_path) if csv_path else {}
    try:
        yield _LOCAL.pairings
    finally:
        _LOCAL.pairings = old


def text_key(lines: List[str]) -> str:
    """A text box as the pairings know it: its words, lower case."""
    return re.sub(r"[^a-z0-9]+", " ", " ".join(lines).lower()).strip()[:160]

@dataclass
class PictureShape:
    data: bytes
    fmt: str
    box: Box
    name: str = ""


@dataclass
class SlideSheet:
    """One table (or a slide's text) laid out as cells."""
    name: str
    cells: Dict[Tuple[int, int], str]
    colours: Dict[Tuple[int, int], str] = field(default_factory=dict)
    slide: int = 0
    #: lines put above this table only by where they sit on the slide (one
    #: table of several): (row, col) -> why ("nearest to it")
    guessed: Dict[Tuple[int, int], str] = field(default_factory=dict)

    @property
    def place_note(self) -> str:
        """Said on every value of a table whose text was paired by place."""
        if not self.guessed:
            return ""
        texts = ", ".join(f"'{self.cells[k]}'" for k in sorted(self.guessed)[:3])
        return f"{texts} {PLACE_NOTE}"


#: on a value whose table's heading or signer came from where a text box sits
PLACE_NOTE = ("was matched to this table by its place on the slide: group the text box "
              "with its table in PowerPoint (or give them the same colour) to say so")


@dataclass
class SlidePicture:
    sheet: str                    # 'Slide 3 · Visual Inspection — Inspector: JB'
    anchor: str                   # 'picture 1'
    data: bytes
    fmt: str
    slide: int = 0


@dataclass
class Pairing:
    slide: int
    text: str                     # the text box's first line
    tables: List[str]             # the sheets it went above; [] = the whole slide's
    how: str                      # BY_*
    key: str = ""                 # text_key of the text box
    numbers: List[int] = field(default_factory=list)       # its tables, 1, 2 ...
    choices: List[str] = field(default_factory=list)       # every table of the slide

    @property
    def guessed(self) -> bool:
        return self.how not in SURE_PAIRINGS

    def describe(self) -> str:
        to = ", ".join(self.tables) if self.tables else "every table on the slide"
        why = {BY_GROUP: "grouped with it", BY_COLOUR: "the same colour",
               BY_TITLE: "the slide's title", BY_ONLY: "the slide's only table",
               BY_PLACE: "nearest to it — a guess", BY_SLIDE: "over or away from the "
               "tables — a guess", BY_USER: "your link"}.get(self.how, self.how)
        return f"Slide {self.slide}: '{self.text}' → {to} ({why})"


@dataclass
class Deck:
    sheets: List[SlideSheet] = field(default_factory=list)
    pictures: List[SlidePicture] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    pairings: List[Pairing] = field(default_factory=list)

    def guesses(self, name: str = "") -> List[str]:
        """Lines for the log: each text box paired with a table by place."""
        return [f"{name + ' › ' if name else ''}{p.describe()}: group them (or colour "
                "them alike) to say so" for p in self.pairings if p.guessed]


class PptxError(Exception):
    pass


# ── the zip ─────────────────────────────────────────────────────────────
def _rels(z: zipfile.ZipFile, part: str) -> Dict[str, str]:
    """rId -> the target part's path, for one part."""
    folder, name = posixpath.split(part)
    rels = posixpath.join(folder, "_rels", name + ".rels")
    try:
        root = ET.fromstring(z.read(rels))
    except KeyError:
        return {}
    out = {}
    for rel in root.findall("rel:Relationship", NS):
        target = rel.get("Target", "")
        if rel.get("TargetMode") == "External":
            continue
        out[rel.get("Id", "")] = posixpath.normpath(
            target[1:] if target.startswith("/") else posixpath.join(folder, target))
    return out


def _theme(z: zipfile.ZipFile) -> Dict[str, str]:
    """The deck's theme colours by scheme name ('accent1' -> 'RRGGBB')."""
    names = [n for n in z.namelist() if re.fullmatch(r"ppt/theme/theme\d+\.xml", n)]
    if not names:
        return {}
    text = z.read(sorted(names, key=lambda n: int(re.findall(r"\d+", n)[-1]))[0]) \
        .decode("utf-8", "replace")
    scheme = re.search(r"<a:clrScheme\b.*?</a:clrScheme>", text, re.S)
    out: Dict[str, str] = {}
    if scheme:
        for name, body in re.findall(r"<a:(\w+)>(.*?)</a:\1>", scheme.group(0), re.S):
            m = re.search(r'(?:srgbClr val|lastClr)="([0-9A-Fa-f]{6})"', body)
            if m:
                out[name] = m.group(1).upper()
    for alias, base in (("bg1", "lt1"), ("tx1", "dk1"), ("bg2", "lt2"), ("tx2", "dk2")):
        if base in out:
            out.setdefault(alias, out[base])
    return out


def _colour(el: Optional[ET.Element], theme: Dict[str, str]) -> str:
    """'RRGGBB' of a solid fill (or a highlight), '' for none or white."""
    if el is None:
        return ""
    c = el.find("a:srgbClr", NS)
    rgb = ""
    if c is not None:
        rgb = (c.get("val") or "").upper()
    else:
        c = el.find("a:schemeClr", NS)
        if c is not None:
            rgb = theme.get(c.get("val") or "", "")
    return "" if not re.fullmatch(r"[0-9A-F]{6}", rgb) or rgb == "FFFFFF" else rgb


# ── shapes ──────────────────────────────────────────────────────────────
def _xfrm(el: Optional[ET.Element]) -> Box:
    if el is None:
        return Box()
    off, ext = el.find("a:off", NS), el.find("a:ext", NS)
    if off is None or ext is None:
        return Box()
    return Box(int(off.get("x", 0)), int(off.get("y", 0)), int(ext.get("cx", 0)),
               int(ext.get("cy", 0)), True)


def _paragraphs(body: Optional[ET.Element], theme) -> Tuple[List[str], List[str]]:
    """The lines of a text body (one per paragraph, line breaks kept as
    lines) and the highlight colour of each."""
    lines: List[str] = []
    colours: List[str] = []
    if body is None:
        return lines, colours
    for p in body.findall("a:p", NS):
        cur, hl = "", ""
        for node in p:
            tag = node.tag.split("}")[-1]
            if tag in ("r", "fld"):
                t = node.find("a:t", NS)
                cur += t.text or "" if t is not None else ""
                rpr = node.find("a:rPr", NS)
                if rpr is not None and not hl:
                    hl = _colour(rpr.find("a:highlight", NS), theme)
            elif tag == "br":
                if cur.strip():
                    lines.append(" ".join(cur.split()))
                    colours.append(hl)
                cur = ""
        if cur.strip():
            lines.append(" ".join(cur.split()))
            colours.append(hl)
    return lines, colours


#: 'Eddy Current Response (%FSH) — Operator: TS': a heading and a label run
#: together on one line of a text box
_JOINED = re.compile(r"\s+[—–|]\s+|\s+-\s+(?=[A-Za-z][^:]{0,30}:)")
_LABEL_PIECE = re.compile(r"^[A-Za-z][^:]{0,40}:\s*\S")


def _split_joined(lines: List[str], colours: List[str]) -> Tuple[List[str], List[str]]:
    """A line that runs a heading and a 'label: value' together, split into
    one line each, so the label is read as one ('Operator: TS' signs the
    table) and the heading as the table's section."""
    out, cols = [], []
    for line, k in zip(lines, colours):
        parts = [x.strip() for x in _JOINED.split(line) if x.strip()]
        if len(parts) > 1 and any(_LABEL_PIECE.match(x) for x in parts):
            out += parts
            cols += [k] * len(parts)
        else:
            out.append(line)
            cols.append(k)
    return out, cols


def _table(tbl: ET.Element, box: Box, theme) -> TableShape:
    rows: List[List[str]] = []
    fills: Dict[Tuple[int, int], str] = {}
    borders: set = set()
    n = 0
    for i, tr in enumerate(tbl.findall("a:tr", NS)):
        row: List[str] = []
        for j, tc in enumerate(tr.findall("a:tc", NS)):
            n += 1
            if n > MAX_TABLE_CELLS:
                break
            if tc.get("hMerge") == "1" or tc.get("vMerge") == "1":
                row.append("")                       # covered by a merged cell
                continue
            lines, _ = _paragraphs(tc.find("a:txBody", NS), theme)
            row.append(" ".join(lines))
            pr = tc.find("a:tcPr", NS)
            k = _colour(pr.find("a:solidFill", NS), theme) if pr is not None else ""
            if k:
                fills[(i, j)] = k
            if pr is not None:
                for side in ("lnL", "lnR", "lnT", "lnB"):
                    ln = pr.find(f"a:{side}", NS)
                    b = _colour(ln.find("a:solidFill", NS), theme) if ln is not None else ""
                    if b and b != "000000":
                        borders.add(b)
        rows.append(row)
    return TableShape(rows, fills, box, borders=borders)


def _shapes(tree: ET.Element, z: zipfile.ZipFile, rels: Dict[str, str], theme,
            notes: List[str], label: str, scale=(0, 0, 1.0, 1.0, 0, 0),
            groups: Tuple[int, ...] = (), counter: Optional[List[int]] = None):
    """Text boxes, tables and pictures of a shape tree, groups flattened
    (their children placed on the slide)."""
    texts: List[TextShape] = []
    tables: List[TableShape] = []
    pics: List[PictureShape] = []
    ox, oy, sx, sy, cx0, cy0 = scale
    counter = counter if counter is not None else [0]

    def place(b: Box) -> Box:
        if not b.known:
            return b
        return Box(int(ox + (b.x - cx0) * sx), int(oy + (b.y - cy0) * sy),
                   int(b.w * sx), int(b.h * sy), True)

    for node in list(tree)[:MAX_SHAPES]:
        tag = node.tag.split("}")[-1]
        if tag == "sp":
            body = node.find("p:txBody", NS)
            lines, cols = _split_joined(*_paragraphs(body, theme))
            if not lines:
                continue
            ph = node.find("p:nvSpPr/p:nvPr/p:ph", NS)
            title = ph is not None and ph.get("type") in ("title", "ctrTitle")
            sppr = node.find("p:spPr", NS)
            fill = _colour(sppr.find("a:solidFill", NS), theme) if sppr is not None else ""
            ln = sppr.find("a:ln", NS) if sppr is not None else None
            outline = _colour(ln.find("a:solidFill", NS), theme) if ln is not None else ""
            texts.append(TextShape(lines, place(_xfrm(sppr.find("a:xfrm", NS)
                                                      if sppr is not None else None)),
                                   title, fill, cols, outline, groups))
        elif tag == "graphicFrame":
            data = node.find("a:graphic/a:graphicData", NS)
            box = place(_xfrm(node.find("p:xfrm", NS)))
            if data is None:
                continue
            uri = data.get("uri", "")
            if uri == TABLE_URI:
                tbl = data.find("a:tbl", NS)
                if tbl is not None:
                    t = _table(tbl, box, theme)
                    t.groups = groups
                    name = node.find("p:nvGraphicFramePr/p:cNvPr", NS)
                    t.name = name.get("name", "") if name is not None else ""
                    tables.append(t)
            elif uri.endswith("/chart"):
                notes.append(f"{label}: a chart is not read (a chart is drawn from data "
                             "that is elsewhere)")
        elif tag == "pic":
            blip = node.find("p:blipFill/a:blip", NS)
            rid = blip.get(R_EMBED) if blip is not None else None
            part = rels.get(rid or "")
            if not part:
                continue
            try:
                data = z.read(part)
            except KeyError:
                continue
            sppr = node.find("p:spPr", NS)
            pics.append(PictureShape(data, Path(part).suffix.lstrip(".").lower() or "png",
                                     place(_xfrm(sppr.find("a:xfrm", NS)
                                                 if sppr is not None else None))))
        elif tag == "grpSp":
            g = node.find("p:grpSpPr/a:xfrm", NS)
            inner = (0, 0, 1.0, 1.0, 0, 0)
            if g is not None:
                off, ext = g.find("a:off", NS), g.find("a:ext", NS)
                coff, cext = g.find("a:chOff", NS), g.find("a:chExt", NS)
                if None not in (off, ext, coff, cext):
                    gw, gh = int(cext.get("cx", 0)) or 1, int(cext.get("cy", 0)) or 1
                    o = place(Box(int(off.get("x", 0)), int(off.get("y", 0)),
                                  int(ext.get("cx", 0)), int(ext.get("cy", 0)), True))
                    inner = (o.x, o.y, o.w / gw, o.h / gh, int(coff.get("x", 0)),
                             int(coff.get("y", 0)))
            counter[0] += 1
            t2, tb2, p2 = _shapes(node, z, rels, theme, notes, label, inner,
                                  groups + (counter[0],), counter)
            texts += t2
            tables += tb2
            pics += p2
    return texts, tables, pics


# ── which text belongs to which table ───────────────────────────────────
def _distance(t: Box, b: Box) -> float:
    """How far text is from a table; text is looked for above and to the
    left of its table first, so below and right count double."""
    tx, ty = t.x + t.w / 2, t.y + t.h / 2
    dy = 0 if b.y <= ty <= b.y + b.h else (b.y - ty if ty < b.y else 2 * (ty - b.y - b.h))
    dx = 0 if b.x <= tx <= b.x + b.w else (b.x - tx if tx < b.x else 2 * (tx - b.x - b.w))
    return dx + dy


def _owners(text: TextShape, tables: List[TableShape], slide_h: int
            ) -> Tuple[List[int], str]:
    """The tables a text box belongs to, and how that was told ([] = the
    whole slide's). The user's own say first: a text box GROUPED with tables
    belongs to them; one filled or outlined in a colour a table has (a cell's
    fill or border) belongs to it. Then the title, the only table, and last
    where it sits: nearest, above and to the left first - a guess."""
    if text.title:
        return [], BY_TITLE
    if not tables:
        return [], BY_SLIDE
    for g in reversed(text.groups):                       # the innermost group first
        mine = [i for i, t in enumerate(tables) if g in t.groups]
        if mine:
            return mine, BY_GROUP
    marks = {k for k in (text.colour, text.outline) if k} | {k for k in text.colours if k}
    if marks:
        mine = [i for i, t in enumerate(tables) if marks & t.marks]
        if mine:
            return mine, BY_COLOUR
    if len(tables) == 1:
        return [0], BY_ONLY
    if not text.box.known:
        return [], BY_SLIDE
    known = [i for i, t in enumerate(tables) if t.box.known]
    if not known:
        return [], BY_SLIDE
    # over several tables side by side: the whole slide's
    above = [i for i in known if text.box.y + text.box.h <= tables[i].box.y + 1]
    spans = [i for i in above if text.box.x <= tables[i].box.x + tables[i].box.w / 2
             <= text.box.x + text.box.w]
    if len(spans) >= 2 and len({tables[i].box.y // max(1, slide_h // 20) for i in spans}) == 1:
        return [], BY_SLIDE
    dist = {i: _distance(text.box, tables[i].box) for i in known}
    best = min(dist, key=dist.get)
    if dist[best] > FAR * max(slide_h, 1):
        return [], BY_SLIDE
    return [best], BY_PLACE


def _slide_size(z: zipfile.ZipFile) -> Tuple[int, int]:
    try:
        root = ET.fromstring(z.read("ppt/presentation.xml"))
        s = root.find("p:sldSz", NS)
        if s is not None:
            return int(s.get("cx", 9144000)), int(s.get("cy", 6858000))
    except Exception:                                       # noqa: BLE001
        pass
    return 9144000, 6858000


def _slide_parts(z: zipfile.ZipFile) -> List[str]:
    root = ET.fromstring(z.read("ppt/presentation.xml"))
    rels = _rels(z, "ppt/presentation.xml")
    out = []
    lst = root.find("p:sldIdLst", NS)
    for sid in (lst.findall("p:sldId", NS) if lst is not None else []):
        if sid.get("show") == "0":
            continue
        part = rels.get(sid.get(R_ID, ""))
        if part:
            out.append(part)
    return out


# ── the deck ────────────────────────────────────────────────────────────
def read_pptx(path, want_pictures: bool = True, pairings: Optional[Pairings] = None) -> Deck:
    """``pairings``: the user's own (load_pairings); by default the ones of
    the Ledger being read (user_pairings)."""
    path = Path(path)
    deck = Deck()
    if pairings is None:
        pairings = getattr(_LOCAL, "pairings", None) or {}
    mine_all = pairings.get(path.name, {})
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise PptxError(f"{path.name} is not a .pptx (an old .ppt, or damaged): save it "
                        "as .pptx in PowerPoint and add that copy") from e
    with z:
        if "ppt/presentation.xml" not in z.namelist():
            raise PptxError(f"{path.name} is not a PowerPoint deck")
        theme = _theme(z)
        _w, slide_h = _slide_size(z)
        for n, part in enumerate(_slide_parts(z), start=1):
            label = f"Slide {n}"
            try:
                root = ET.fromstring(z.read(part))
            except (KeyError, ET.ParseError) as e:
                deck.notes.append(f"{path.name} › {label}: could not be read ({e})")
                continue
            if root.get("show") == "0":
                continue                                   # a hidden slide
            tree = root.find("p:cSld/p:spTree", NS)
            if tree is None:
                continue
            texts, tables, pics = _shapes(tree, z, _rels(z, part), theme, deck.notes, label)
            texts.sort(key=lambda t: (not t.title, t.box.y, t.box.x))
            tables.sort(key=lambda t: (t.box.y, t.box.x))
            owned: Dict[int, List[TextShape]] = {}
            slide_wide: List[TextShape] = []
            how: Dict[int, str] = {}                       # id(text) -> how it was paired
            names = [label if len(tables) == 1 else f"{label} table {i + 1}"
                     for i in range(len(tables))]
            yours = mine_all.get(str(n), {})
            for t in texts:
                own, h = _owners(t, tables, slide_h)
                said = yours.get(text_key(t.lines))
                if said is not None and tables and not t.title:
                    own = [i - 1 for i in said if 1 <= i <= len(tables)]
                    h = BY_USER
                how[id(t)] = h
                if not own:
                    slide_wide.append(t)
                for i in own:
                    owned.setdefault(i, []).append(t)
                if tables:
                    deck.pairings.append(Pairing(n, t.lines[0][:60], [names[i] for i in own],
                                                 h, text_key(t.lines), [i + 1 for i in own],
                                                 list(names)))
            if not tables and slide_wide:
                deck.sheets.append(_lay_out(label, slide_wide, [], None, n, how))
            for i, table in enumerate(tables):
                deck.sheets.append(_lay_out(names[i], slide_wide, owned.get(i, []), table, n,
                                            how))
            if want_pictures:
                title = next((" ".join(t.lines) for t in texts if t.title), "")
                for k, pic in enumerate(pics, start=1):
                    near = _nearest_text(pic.box, texts, slide_h)
                    sheet = " · ".join(x for x in (label, title[:60], near) if x)
                    deck.pictures.append(SlidePicture(sheet[:120], f"picture {k}", pic.data,
                                                      pic.fmt, n))
    return deck


def _nearest_text(box: Box, texts: List[TextShape], slide_h: int) -> str:
    cands = [t for t in texts if t.box.known and box.known and not t.title]
    if not cands:
        return ""
    best = min(cands, key=lambda t: _distance(t.box, box))
    if _distance(best.box, box) > FAR * max(slide_h, 1):
        return ""
    return " — ".join(best.lines)[:100]


def _lay_out(name: str, slide_wide: List[TextShape], owned: List[TextShape],
             table: Optional[TableShape], slide: int,
             how: Optional[Dict[int, str]] = None) -> SlideSheet:
    cells: Dict[Tuple[int, int], str] = {}
    colours: Dict[Tuple[int, int], str] = {}
    guessed: Dict[Tuple[int, int], str] = {}
    r = 1
    for t in slide_wide + owned:
        h = (how or {}).get(id(t), "")
        for line, hl in zip(t.lines, t.colours or [""] * len(t.lines)):
            cells[(r, 1)] = line
            k = hl or t.colour
            if k:
                colours[(r, 1)] = k
            if table is not None and h not in SURE_PAIRINGS and h:
                guessed[(r, 1)] = h
            r += 1
    if table is not None:
        if r > 1:
            r += 1                                       # a blank row: a heading, not a header
        for i, row in enumerate(table.rows):
            for j, v in enumerate(row):
                if v.strip():
                    cells[(r + i, 1 + j)] = v.strip()
                k = table.fills.get((i, j))
                if k:
                    colours[(r + i, 1 + j)] = k
    return SlideSheet(name, cells, colours, slide, guessed)
