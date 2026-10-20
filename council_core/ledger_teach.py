"""
council_core.ledger_teach — "Teach from a sample workbook": Council guesses
what each thing in ONE workbook is, the user answers, and the answers are
kept so the next Extract and knowledge-graph rebuild place them without a
model (user, 2026-10-20: "taking a sample workbook from the folder and I
line up where they match the templates, or say data found in the images and
what they are in the template. First I want an LLM guess to be made, then I
give an answer — hopefully this will help train the Ledger and knowledge
graph to work").

THE API (the Ledger tab's Teach dialog calls both on a WORKER thread: they
read a workbook - through Excel for .xls / .xlsb -, may ask a model and open
the knowledge graph; nothing here touches Qt)

    session = guess(csv_path, workbook, model_id=None, chat=None,
                    use_images=True, image_reader=None, pattern_mask=None,
                    cancel=None, progress=None)
        -> TeachSession
           .rows        [TeachRow] one per thing found, most in need of an
                        answer first; each with a stable .key, .label as
                        written, .where ('Sheet1!B4' or 'picture 1 on
                        Visual, row 2 column 1'), .example (a short value),
                        .count (values under it), .item (its flaw code when
                        one), .picture / .picture_text (what the image reader
                        read), and .guess: Guess(role, field, via, score)
           .choices     [(choice key, plain words)] for every row's dropdown
           .part        PartGuess (value, label, where, via, score) or None;
                        .parts every part number the workbook names
           .inspectors  [Person(initials, full_name from the people list,
                        where, count)] for the People section
           .problems, .hidden (rows not shown), .cancelled, .model,
           .pattern_mask / .pattern_saved, .describe() for the Ledger log
    report = learn(csv_path, session, answers, people=None, part=None,
                   save_pattern=False, connections_vault=None)
        answers  {row.key: choice key}: 'field:<template field>', 'part',
                 'inspector', 'item', 'ignore' or 'unsure' (choice_key /
                 parse_choice; a Guess or a (role, field) pair also does)
        people   {initials: full name} from the People section
        part     the part number the user confirmed or typed;
                 save_pattern=True keeps its shape as a part-name pattern
        connections_vault  the Connections vault (a path, or anything with
                 open_graph(), e.g. council_qt.tabs.connections.
                 ConnectionsActions): the graph's field rules the answers
                 prove are ticked. None: the graph is not touched.
        -> LearnReport: what was written where; .describe() for the log
    improvement(before, after) -> str     what 'Guess again' changed
    answers_from_guesses(session)          every row's guess as its answer
                                           (what the dropdowns pre-select)
    initials_in(text) -> ['JB', ...]       the initials a cell names: for
                                           the People section when the user
                                           answers 'Inspector' for a row
    examples(csv_path) -> [Example]        the answers kept (few-shot)
    label_examples / part_examples         the same, as ledger_llm's
                                           questions take them
    source_extract.load_patterns(csv)      the saved part-name patterns,
                                           newest first (the tab's pattern
                                           field can offer them)

ROLES: what a thing can be - a template field (ROLE_FIELD, with the field's
name), the Part number (ROLE_PART), an Inspector's initials or name label
(ROLE_INSPECTOR), a flaw / item code (ROLE_ITEM), not data (ROLE_IGNORE),
or not known (ROLE_UNSURE). A guess comes from the Ledger's rules
(VIA_RULE), the user's earlier answers (VIA_ALIAS), Council's model
(VIA_MODEL) or nothing (VIA_NONE).

HOW THE GUESS IS MADE: the workbook is read exactly as Extract reads it
(source_extract.extract: every sheet, text boxes, pictures through the
image reader given; the same matcher, item grids, tables, forms, part and
inspector passes) with a sheet reader that also notes what the passes
recognised. The Ledger's rules guess first; Council's model is asked only
where they stop - which field or role each label nobody placed is
(ledger_llm.map_labels) and, when no part number was found, which part the
workbook is about (ledger_llm.find_part) - with the user's own past answers
as examples. Only labels, short cell text and part numbers reach the model,
and it runs on this computer (ledger_llm.chat_for: US-origin models, a GGUF
always taken).

WHAT LEARN WRITES (all beside the parent CSV)
  <stem>.aliases.json  "map": label -> field (and the part-number label ->
                       the template's id field); "ignore"; "people";
                       "who_labels", "not_who_labels", "item_labels",
                       "patterns" (source_extract.Taught)
  <stem>.teach.jsonl   one line per answer: label, role, field, flaw code,
                       workbook, sheet - and for the part number, the part
                       the user confirmed. Never other cell values.
  the knowledge graph  (connections_vault) its PART rule for a part-number
                       label ticked, or the label added as a confirmed
                       other name for it; a label mapped to a field whose
                       name is a confirmed rule added as its other name.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import source_extract as se
from sample_ledger import LedgerError, ParentCSV, TemplateSchema, normalize_label

# ── roles and where a guess came from ───────────────────────────────────
ROLE_FIELD = "field"
ROLE_PART = "part"
ROLE_INSPECTOR = "inspector"
ROLE_ITEM = "item"
ROLE_IGNORE = "ignore"
ROLE_UNSURE = "unsure"
ROLES = (ROLE_FIELD, ROLE_PART, ROLE_INSPECTOR, ROLE_ITEM, ROLE_IGNORE, ROLE_UNSURE)

VIA_RULE = "rule"            # the Ledger's own matching
VIA_ALIAS = "alias"          # the user's earlier answer (aliases, who_labels ...)
VIA_MODEL = "model"          # Council's model
VIA_NONE = "none"            # nothing placed it

#: Things found shown at most: a workbook of many sheets would list hundreds
#: of rule-placed labels; the ones that need an answer come first.
MAX_ROWS = 150
#: An example value shown, at most (a note cell is not shown whole).
EXAMPLE_CHARS = 40
#: A picture's text kept beside its rows, at most.
PICTURE_TEXT_CHARS = 300
#: How sure the rules are of an inspector's label, a flaw-code column, or a
#: part read from the sheet's or file's name (no score of their own).
_MARKER_SCORE = 0.9
_SHEET_PART_SCORE = 0.8
_FILE_PART_SCORE = 0.6


def choice_key(role: str, field_name: str = "") -> str:
    """The dropdown's key for a role: 'field:<name>' or the role itself."""
    return f"{ROLE_FIELD}:{field_name}" if role == ROLE_FIELD else role


def parse_choice(answer: Any) -> Tuple[str, str]:
    """(role, field) of an answer: a choice key ('field:Power (W)', 'part'),
    a Guess, or a (role, field) pair. Anything else is ROLE_UNSURE."""
    if isinstance(answer, Guess):
        return answer.role, answer.field
    if isinstance(answer, (tuple, list)) and len(answer) == 2:
        role, fname = str(answer[0] or ""), str(answer[1] or "")
        return (role, fname if role == ROLE_FIELD else "") if role in ROLES else (ROLE_UNSURE, "")
    s = str(answer or "")
    if s.startswith(ROLE_FIELD + ":"):
        return ROLE_FIELD, s[len(ROLE_FIELD) + 1:]
    return (s, "") if s in ROLES and s != ROLE_FIELD else (ROLE_UNSURE, "")


def role_text(role: str, schema_item_field: str = "") -> str:
    """A role in the dialog's words."""
    return {ROLE_PART: "Part number",
            ROLE_INSPECTOR: "Inspector (initials or name)",
            ROLE_ITEM: f"{schema_item_field or 'Flaw / item'} code",
            ROLE_IGNORE: "Ignore (not data)",
            ROLE_UNSURE: "Unsure"}.get(role, role)


def choices(schema: TemplateSchema) -> List[Tuple[str, str]]:
    """Every answer a row can be given, (choice key, plain words): the
    roles, then each template field but the part number's (that is 'Part
    number')."""
    out = [(r, role_text(r, schema.item_field))
           for r in (ROLE_PART, ROLE_INSPECTOR, ROLE_ITEM, ROLE_IGNORE, ROLE_UNSURE)]
    out += [(choice_key(ROLE_FIELD, f.name), f.name + (" (per " + schema.item_field + ")"
                                                       if f.per_item and schema.item_field
                                                       else ""))
            for f in schema.fields if f.name != schema.id_field]
    return out


# ── what the dialog shows ───────────────────────────────────────────────
@dataclass
class Guess:
    role: str = ROLE_UNSURE
    field: str = ""          # the template field (ROLE_FIELD)
    via: str = VIA_NONE
    score: float = 0.0

    @property
    def key(self) -> str:
        """The dropdown's choice key for this guess."""
        return choice_key(self.role, self.field)

    def text(self, item_field: str = "") -> str:
        return self.field if self.role == ROLE_FIELD else role_text(self.role, item_field)

    def source(self) -> str:
        """Where the guess came from, in the dialog's words."""
        return {VIA_RULE: "the Ledger's rules", VIA_ALIAS: "your earlier answer",
                VIA_MODEL: "Council's model", VIA_NONE: "nothing"}.get(self.via, self.via)


@dataclass
class TeachRow:
    """One thing found in the workbook: a label (with the values under or
    beside it, grouped), a part number's label, an inspector's label or
    heading, a flaw-code column, or a picture's text."""
    key: str                 # '<sheet or picture>|<label normalised>': stable
    kind: str                # 'label' | 'part' | 'inspector' | 'item'
    label: str               # as written
    where: str               # 'Sheet1!B4', 'picture 1 on Visual, row 2 column 1'
    sheet: str
    example: str = ""        # one value under / beside it (short)
    count: int = 0           # values under it
    item: str = ""           # its flaw code, when all its values have one
    guess: Guess = field(default_factory=Guess)
    note: str = ""
    picture: str = ""        # 'picture 1 on Visual' for a thing read from a picture
    picture_text: str = ""   # what the image reader read in it
    initials: List[str] = field(default_factory=list)   # an inspector row's people
    shape: str = ""          # an inspector row's: label | column | heading | sheet name
    order: Tuple[int, int, int] = (0, 0, 0)             # (source, row, column)


@dataclass
class PartGuess:
    value: str
    label: str = ""          # the label beside it, when one
    where: str = ""
    via: str = VIA_RULE
    score: float = 0.0
    how: str = ""            # in plain words


@dataclass
class Person:
    initials: str
    full_name: str = ""      # from the Ledger's people list
    where: str = ""          # first seen
    count: int = 0


@dataclass
class TeachSession:
    csv: str
    workbook: str
    choices: List[Tuple[str, str]] = field(default_factory=list)
    item_field: str = ""
    id_field: str = ""
    rows: List[TeachRow] = field(default_factory=list)
    part: Optional[PartGuess] = None
    parts: List[str] = field(default_factory=list)
    inspectors: List[Person] = field(default_factory=list)
    model: str = ""          # the model that answered; '' : none was asked
    model_picked: bool = False       # a model was given (it may not have been needed)
    pattern_mask: str = ""
    pattern_saved: bool = False      # the Ledger's saved pattern (the field was empty)
    pictures: int = 0
    pictures_read: int = 0
    hidden: int = 0
    cancelled: bool = False
    problems: List[str] = field(default_factory=list)

    def row(self, key: str) -> Optional[TeachRow]:
        return next((r for r in self.rows if r.key == key), None)

    def describe(self) -> str:
        """What was found and guessed, for the Ledger log."""
        name = Path(self.workbook).name
        if self.cancelled and not self.rows:
            return f"Teach — {name}: cancelled; nothing to answer."
        by = Counter(r.guess.via for r in self.rows)
        head = (f"Teach — {name}: {len(self.rows)} thing(s) found"
                + (f" ({self.hidden} more not shown)" if self.hidden else "")
                + (f"; Council's model: {self.model}" if self.model else
                   "; Council's model was not needed: the rules placed everything"
                   if self.model_picked else "; the rules only"))
        lines = [head,
                 f"  placed by the rules: {by[VIA_RULE] + by[VIA_ALIAS]} ({by[VIA_ALIAS]} by "
                 f"your earlier answers); guessed by Council's model: {by[VIA_MODEL]}; "
                 f"unsure: {by[VIA_NONE]}"]
        if self.cancelled:
            lines.append("  Cancel stopped Council's model: what it had not answered is "
                         "the rules' guess")
        if self.part is not None:
            more = [p for p in self.parts if p != self.part.value]
            lines.append(f"  part number: {self.part.value} — {self.part.how}"
                         + (f" (also named: {', '.join(more[:5])})" if more else ""))
        else:
            lines.append("  part number: not found — type it in the Teach dialog")
        if self.inspectors:
            lines.append("  inspectors: " + ", ".join(
                f"{p.initials} ({p.full_name or 'no full name yet'})"
                for p in self.inspectors))
        if self.pattern_mask:
            lines.append(f"  part-name pattern {self.pattern_mask!r}"
                         + (" (this Ledger's saved one)" if self.pattern_saved else ""))
        if self.pictures:
            lines.append(f"  pictures: {self.pictures_read} of {self.pictures} read")
        return "\n".join(lines + [f"  ! {p}" for p in self.problems])


# ── the examples file (few-shot) ────────────────────────────────────────
@dataclass
class Example:
    """One answer the user gave, kept for Council's model's next questions.
    Only the label and what it is - and, for the part number, the part the
    user confirmed: never another cell value."""
    label: str
    role: str
    field: str = ""
    item: str = ""
    value: str = ""
    workbook: str = ""
    sheet: str = ""
    ts: str = ""


def examples_path_for(csv_path) -> Path:
    """'<stem>.teach.jsonl' beside the parent CSV."""
    return Path(csv_path).with_suffix(".teach.jsonl")


def examples(csv_path) -> List[Example]:
    """Every answer kept for this Ledger, oldest first; a damaged line is
    skipped, a missing file is none."""
    p = examples_path_for(csv_path)
    out: List[Example] = []
    try:
        text = p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        return out
    known = set(Example.__dataclass_fields__)
    for line in text.splitlines():
        try:
            d = json.loads(line)
        except Exception:                                   # noqa: BLE001
            continue
        if isinstance(d, dict) and d.get("role") in ROLES:
            out.append(Example(**{k: str(v or "") for k, v in d.items() if k in known}))
    return out


def _add_examples(csv_path, new: Sequence[Example]) -> int:
    """Append the answers not kept already (the same label, role, field,
    flaw code and value); how many were added."""
    def key(e: Example):
        return (normalize_label(e.label), e.role, e.field, e.item, e.value)
    have = {key(e) for e in examples(csv_path)}
    lines = []
    for e in new:
        if key(e) in have:
            continue
        have.add(key(e))
        lines.append(json.dumps(asdict(e), ensure_ascii=False))
    if lines:
        with open(examples_path_for(csv_path), "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return len(lines)


def _ask_roles(schema: TemplateSchema) -> Dict[str, str]:
    """How the roles are offered to the model beside the template's fields:
    names in brackets, which no template field has."""
    return {ROLE_PART: "[the part number / serial of the sample]",
            ROLE_INSPECTOR: "[an inspector's initials or name]",
            ROLE_ITEM: f"[a {schema.item_field or 'flaw / item'} code]",
            ROLE_IGNORE: "[not data: a title, heading or note]"}


def label_examples(csv_path, schema: TemplateSchema, roles: bool = False
                   ) -> List[Tuple[str, Optional[str], str]]:
    """The kept answers as ledger_llm.map_labels takes them, oldest first:
    (label, the field it means or None, why). ``roles``: the model is also
    offered the roles (Teach's question): a role's answer is its bracketed
    name (_ask_roles); else (Extract's question, fields only) it is None,
    with why. A field the template no longer has is left out."""
    fields = {f.name for f in schema.fields if f.name != schema.id_field}
    ask = _ask_roles(schema)
    out: List[Tuple[str, Optional[str], str]] = []
    for e in examples(csv_path):
        if not e.label.strip() or e.role == ROLE_UNSURE:
            continue
        if e.role == ROLE_FIELD:
            if e.field in fields:
                out.append((e.label, e.field, ""))
            continue
        out.append((e.label, ask[e.role] if roles else None,
                    "" if roles else f"the user said: {role_text(e.role, schema.item_field)}"))
    return out


def part_examples(csv_path) -> List[Tuple[str, str]]:
    """(part number, its label) the user confirmed, newest first: what
    ledger_llm.find_part shows of how their parts look."""
    return [(e.value, e.label) for e in reversed(examples(csv_path))
            if e.role == ROLE_PART and e.value]


# ── reading one workbook as Extract does ────────────────────────────────
class _Reader(se._Extractor):
    """source_extract's sheet reader, noting what its passes recognised -
    the part number's labels and columns, the flaw-code columns, where each
    value's label is - so Teach can show them and ask about them. It reads
    exactly as the extract does: nothing is decided here."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.part_hits: List[Tuple[int, int, Any, str]] = []   # (row, col, cell, part)
        self.id_cols: List[Tuple[int, int, str]] = []          # (header row, col, label)
        self.item_heads: List[Tuple[int, int]] = []            # (header row, col)
        #: id(obs) -> (its label's cell, the value as the cell holds it: an
        #: example is shown as written, not converted to the field's unit)
        self.label_at: Dict[int, Tuple[int, int, Any]] = {}

    def _part_label(self, r, c, v):
        got = super()._part_label(r, c, v)
        if got:
            self.part_hits.append((r, c, v, got))
        return got

    def _id_column(self, r, c, label) -> bool:
        ok = super()._id_column(r, c, label)
        if ok:
            self.id_cols.append((r, c, label))
        return ok

    def _grid(self, r, hits, label_col, ctx, who_cols, guess=False) -> None:
        self.item_heads.append((r, label_col))
        return super()._grid(r, hits, label_col, ctx, who_cols, guess)

    def _data_rows(self, r, cols, item_col):
        rows = super()._data_rows(r, cols, item_col)
        if item_col is not None and rows:
            self.item_heads.append((r, item_col))
        return rows

    def emit(self, part, item, label, r, c, value, match, field_name=None,
             vr=None, vc=None, who=None):
        o = super().emit(part, item, label, r, c, value, match, field_name=field_name,
                         vr=vr, vc=vc, who=who)
        if o is not None:
            self.label_at[id(o)] = (r, c, value)
        return o


def _a1(r: int, c: int) -> str:
    from openpyxl.utils import get_column_letter
    return f"{get_column_letter(c)}{r}"


def _clip(v: Any, most: int = EXAMPLE_CHARS) -> str:
    s = " ".join(str(v if v is not None else "").split())
    return s if len(s) <= most else s[:most - 1].rstrip() + "…"


def _short_text(v: Any, most: int = 60) -> str:
    """A cell's text when it is a short label-like one, else ''."""
    if not isinstance(v, str):
        return ""
    s = " ".join(v.split())
    return s if 0 < len(s) <= most and re.search(r"[A-Za-z]", s) else ""


class _Source:
    """One grid the workbook gave: a sheet, a text box, or a picture read
    as a table - its name in the dialog, and how to say where a cell is."""

    def __init__(self, ex: _Reader, index: int, picture: str = "", text: str = ""):
        self.ex, self.index = ex, index
        self.picture, self.text = picture, text
        self.name = picture or ex.g.sheet

    def where(self, r: int, c: int) -> str:
        if (r, c) == (0, 0):
            return f"sheet name {self.ex.g.sheet!r}"
        if self.picture:
            return f"{self.picture}, row {r} column {c}"
        return f"{self.ex.g.sheet}!{_a1(r, c)}"


class _Rows:
    """The things found, one per (source, label), as they are gathered."""

    def __init__(self):
        self.by_key: Dict[str, TeachRow] = {}

    def add(self, src: _Source, kind: str, label: str, r: int, c: int, guess: Guess,
            example: str = "", count: int = 1, where: str = "", **more) -> Optional[TeachRow]:
        norm = normalize_label(label)
        if not norm:
            return None
        key = f"{src.name}|{norm}"
        row = self.by_key.get(key)
        if row is not None:
            row.count += count
            if not row.example and example:
                row.example = _clip(example)
            for k in more.get("initials") or ():
                if k not in row.initials:      # 'Inspector: JB' here, 'Inspector: MK' there
                    row.initials.append(k)
            return row
        row = TeachRow(key=key, kind=kind, label=" ".join(str(label).split()),
                       where=where or src.where(r, c), sheet=src.ex.g.sheet,
                       example=_clip(example),
                       count=count, guess=guess, picture=src.picture,
                       picture_text=_clip(src.text, PICTURE_TEXT_CHARS) if src.picture else "",
                       order=(src.index, r, c), **more)
        self.by_key[key] = row
        return row


def _markers(src: _Source, rows: _Rows, people: Dict[str, List[Any]]) -> set:
    """The inspectors' labels and headings the reader took as markers
    ('Inspector: JB', 'Initials | MK', 'JB — 2026-09-18', a sheet named
    'JB ...'), and the columns of initials under an 'Inspector' title.
    Returns the cells they are in."""
    ex, m = src.ex, src.ex.m
    cells = set()
    for r, c, who in ex.markers:
        _people_add(people, who, src.where(r, c), n=0)
        if (r, c) == (0, 0):
            rows.add(src, "inspector", ex.g.sheet, 0, 0,
                     Guess(ROLE_INSPECTOR, "", VIA_RULE, _MARKER_SCORE), example=who,
                     initials=[who], shape="sheet name")
            continue
        cells.add((r, c))
        v = ex.g.cells.get((r, c))
        text = " ".join(str(v or "").split())
        hit = se._WHO_INLINE.match(v) or se._WHO_BY.match(v)
        label, shape = text, "heading"
        if hit:
            label, shape = hit.string[:hit.start("who")].strip(" :=-–—") or text, "label"
        elif m.who_head(v):
            shape = "label"
        else:
            lab, inline = se._split_inline(v) if len(text) <= 200 else ("", "")
            if inline and m.key_of(lab) in m.who:
                label, shape = lab, "label"
        via = VIA_ALIAS if shape == "label" and m.key_of(label) in m.who else VIA_RULE
        rows.add(src, "inspector", label, r, c,
                 Guess(ROLE_INSPECTOR, "", via, _MARKER_SCORE), example=who,
                 initials=[who], shape=shape)
    # a column of initials under an 'Inspector Initials' title (a table's,
    # a grid's): the reader reads who signed each row by it
    for (r, c), v in sorted(ex.g.cells.items()):
        if (r, c) in cells or isinstance(v, se._Merged) or not _short_text(v) \
                or not m.who_head(v):
            continue
        found: List[str] = []
        for rr in range(r + 1, r + 9):
            for k in initials_in(ex.g.cells.get((rr, c))):
                if k not in found:
                    found.append(k)
                    _people_add(people, k, src.where(rr, c), n=0)
        if not found:
            continue
        via = VIA_ALIAS if m.key_of(v) in m.who else VIA_RULE
        rows.add(src, "inspector", v, r, c, Guess(ROLE_INSPECTOR, "", via, _MARKER_SCORE),
                 example=", ".join(found[:3]), initials=found, shape="column")
        cells.add((r, c))
    return cells


def initials_in(text: Any) -> List[str]:
    """The initials a cell names ('JB', 'j.b.', both of 'JB/MK'); a written
    name needs no answer and is left out."""
    from council_core.kg_excel import is_initials       # 'seen', 'OK', 'TBD' are no one
    out: List[str] = []
    for w in re.split(r"[/,;&+]", str(text or "")):
        k = se._initials(w.strip())
        if re.fullmatch(r"[A-Z]{2,4}", k) and k not in out and is_initials(w.strip(), loose=True):
            out.append(k)
    return out


def _people_add(people: Dict[str, List[Any]], who: str, where: str, n: int = 1) -> None:
    """``n`` more values signed by ``who`` (initials_in); where they were
    first seen."""
    for k in initials_in(who):
        got = people.setdefault(k, [0, where])
        got[0] += n


def _parts(src: _Source, rows: _Rows, schema: TemplateSchema, wb_name: str,
           found: List[PartGuess]) -> None:
    """The part number's labels and columns the reader used, and the part
    each names; a part read from the sheet's or the file's name."""
    ex, m = src.ex, src.ex.m
    for r, c, v, got in ex.part_hits:
        lab, inline = se._split_inline(v) if isinstance(v, str) and len(v) <= 200 else ("", "")
        label = lab if inline else " ".join(str(v).split())
        mt = m.match(label, per_item=False)
        via = VIA_ALIAS if mt.via == "alias" else VIA_RULE
        score = mt.score if mt.target is not None else _MARKER_SCORE    # 'S/N'
        rows.add(src, "part", label, r, c, Guess(ROLE_PART, "", via, round(score, 2)),
                 example=got)
        found.append(PartGuess(got, label, src.where(r, c), via, round(score, 2),
                               f"{schema.id_field} at {src.where(r, c)}"
                               + (" (your earlier answer)" if via == VIA_ALIAS else "")))
    for r, c, label in ex.id_cols:
        if (r, c) not in ex.used:
            continue                        # a table the reader did not take
        vals = [se._text(ex.g.cells.get((rr, c))) for rr in range(r + 1, r + 40)]
        vals = [x for x in vals if x and not se.is_placeholder(x)]
        mt = m.match(label, per_item=False)
        via = VIA_ALIAS if mt.via == "alias" else VIA_RULE
        rows.add(src, "part", label, r, c, Guess(ROLE_PART, "", via, round(mt.score, 2)),
                 example=vals[0] if vals else "", count=len(vals))
        for x in dict.fromkeys(vals):
            found.append(PartGuess(x, label, src.where(r, c), via, round(mt.score, 2),
                                   f"{schema.id_field} column at {src.where(r, c)}"))
    if ex.part and not ex.part_hits:
        if ex.part_how.startswith("sheet name"):
            found.append(PartGuess(ex.part, "", src.where(0, 0), VIA_RULE, _SHEET_PART_SCORE,
                                   f"the sheet's name {ex.g.sheet!r}"))
        elif ex.part_how.startswith(se.FILE_PART_HOW):
            found.append(PartGuess(ex.part, "", f"file name {wb_name!r}", VIA_RULE,
                                   _FILE_PART_SCORE, f"the file's name {wb_name!r} — check it"))


def _items(src: _Source, rows: _Rows) -> None:
    """The flaw-code columns: an item grid's 'Flaw #' cell, a table's
    column of flaw codes."""
    ex, m = src.ex, src.ex.m
    for r, c in ex.item_heads:
        at = next(((rr, c) for rr in (r, r - 1) if ex.g.text(rr, c) and (rr, c) in ex.used),
                  None)
        if at is None:
            continue
        label = ex.g.text(*at)
        code = next((ex.codes.canon(ex.g.cells.get(p)) for p in
                     [(rr, c) for rr in range(r + 1, r + 6)]
                     + [(r, cc) for cc in range(c + 1, c + 6)]
                     if ex.codes.canon(ex.g.cells.get(p))), "")
        via = VIA_ALIAS if m.key_of(label) in m.item_heads else VIA_RULE
        rows.add(src, "item", label, at[0], at[1], Guess(ROLE_ITEM, "", via, _MARKER_SCORE),
                 example=code or "")


def _values(src: _Source, rows: _Rows, schema: TemplateSchema,
            people: Dict[str, List[Any]]) -> None:
    """The labels of the values the reader found - placed by the rules or
    left for 'Labels to map' - one row per label, its values grouped."""
    ex = src.ex

    def where(o: se.Obs) -> Tuple[int, int, str]:
        """The label's cell (emit noted it), else where the value is."""
        at = ex.label_at.get(id(o))
        return (at[0], at[1], "") if at else (0, 0, o.where.split(" › ", 1)[-1])

    def written(o: se.Obs) -> str:
        at = ex.label_at.get(id(o))
        return se._text(at[2]) if at else o.value
    groups: Dict[str, List[se.Obs]] = defaultdict(list)
    for o in ex.out + ex.unmatched:
        if o.label and normalize_label(o.label):
            groups[normalize_label(o.label)].append(o)
        if o.inspector:
            r, c, w = where(o)
            _people_add(people, o.inspector, w or src.where(r, c))
    per_item = {f.name for f in schema.fields if f.per_item}
    for _norm, obs in groups.items():
        first = obs[0]
        r, c, at = where(first)
        matched = [o for o in obs if o.field]
        note = ""
        if matched:
            fname = Counter(o.field for o in matched).most_common(1)[0][0]
            mine = [o for o in matched if o.field == fname]
            via = VIA_ALIAS if any(o.via == "alias" for o in mine) else VIA_RULE
            g = Guess(ROLE_PART if fname == schema.id_field else ROLE_FIELD,
                      "" if fname == schema.id_field else fname, via,
                      round(max(o.score for o in mine), 2))
            if any(o.via == "ambiguous" for o in mine) and first.guesses:
                note = "two fields nearly tied: " + ", ".join(
                    f"{n} ({s})" for n, s in first.guesses[:2])
        elif normalize_label(first.label) in ex.m.ignore:
            g = Guess(ROLE_IGNORE, "", VIA_ALIAS, 1.0)      # you said so before
        else:
            g = Guess(ROLE_UNSURE, "", VIA_NONE, 0.0)
            near = [(n, s) for n, s in first.guesses[:2] if s >= 0.3]
            if near:
                note = "nearest fields by spelling: " + ", ".join(f"{n} ({s})" for n, s in near)
        items = sorted({o.item for o in obs if o.item},
                       key=lambda x: schema.items.index(x) if x in schema.items else 1 << 20)
        item = items[0] if len(items) == 1 else ""
        if len(items) > 1:
            note = se._join(note, f"{len(items)} {schema.item_field or 'item'} codes "
                                  f"({items[0]} … {items[-1]})")
        elif g.role == ROLE_FIELD and g.field in per_item and not item:
            note = se._join(note, f"no {schema.item_field or 'item'} code beside it")
        if any(o.from_image for o in obs):
            note = se._join(note, "read from a picture: check the value")
        rows.add(src, "label", first.label, r, c, g, example=written(first), count=len(obs),
                 where=at, item=item, note=note)


def _picture_alone(src: _Source, rows: _Rows) -> None:
    """A picture whose text gave no label: its first line, for the user to
    say what it is."""
    first = next((_short_text(v) for _rc, v in sorted(src.ex.g.cells.items())
                  if _short_text(v)), "")
    if first and not any(r.picture == src.picture for r in rows.by_key.values()):
        rows.add(src, "label", first, 1, 1, Guess(ROLE_UNSURE, "", VIA_NONE, 0.0),
                 note="a picture's text: say what it is, or Ignore")


def _label_near(ex: se._Extractor, r: int, c: int, value: str) -> str:
    """The label of a part number the model found at (r, c): 'Serial' of
    'Serial: XYZ9AB123-R2', or the short text left of it or above it."""
    v = ex.g.cells.get((r, c))
    if isinstance(v, str) and len(v) <= 200:
        lab, inline = se._split_inline(v)
        if inline and value.replace(" ", "").lower() in inline.replace(" ", "").lower():
            return lab
    for p in ((r, c - 1), (r - 1, c)):
        s = _short_text(ex.g.cells.get(p), 40)
        if s and not se._split_inline(s)[1]:
            return s.rstrip(" :")
    return ""


# ── the guess ───────────────────────────────────────────────────────────
def guess(csv_path, workbook, *, model_id: Optional[str] = None,
          chat: Optional[Callable] = None, use_images: bool = True,
          image_reader: Optional[se.ImageReader] = None,
          pattern_mask: Optional[str] = None, cancel: Optional[Any] = None,
          progress: Optional[Callable[[str, Optional[float]], None]] = None,
          max_rows: int = MAX_ROWS) -> TeachSession:
    """Council's guess of what each thing in ``workbook`` is, for the user to
    answer (see the module's docstring). Run it on a worker thread.

    ``model_id``: the model the user picked ('ollama:<name>' or a GGUF path,
    ledger_llm.chat_for); ``chat``: one to use instead (tests); neither: the
    Ledger's rules only. ``image_reader``: what reads the pictures (the
    Ledger's default - Tesseract when installed - when None and
    ``use_images``). ``pattern_mask``: the user's part-name pattern; empty,
    the Ledger's saved one. ``cancel``: a threading.Event (the read and the
    model's questions stop). ``progress``: listener(line, fraction)."""
    from council_core import ledger_llm
    from progress_report import Progress
    ledger = ParentCSV(csv_path)
    schema = ledger.schema
    if schema is None:
        raise LedgerError(f"{Path(csv_path).name} has no schema beside it; create it from a "
                          "template first")
    wb = Path(workbook)
    if not wb.exists() and not se.path_too_long(wb):
        raise LedgerError(f"{wb.name} not found")
    try:
        mapping, ignore = se.load_aliases(csv_path)
    except Exception as exc:                                # noqa: BLE001
        raise LedgerError(f"{se.aliases_path_for(csv_path).name} could not be read (fix or "
                          f"remove it): {exc}") from exc
    taught = se.load_taught(csv_path)
    mask, saved = (pattern_mask or "").strip(), False
    if not mask and taught.patterns:
        mask, saved = taught.patterns[0], True
    if mask:
        se.mask_to_regex(mask)                              # a bad one is said now
    stop = cancel.is_set if cancel is not None else None
    ask = report = None
    if chat is not None or model_id:
        report = ledger_llm.AssistReport()
        # a model that is not US-origin is refused here, before the read
        ask = ledger_llm.one_shot(chat or ledger_llm.chat_for(model_id, should_stop=stop),
                                  report, stop)
    s = TeachSession(csv=str(csv_path), workbook=str(wb), choices=choices(schema),
                     item_field=schema.item_field, id_field=schema.id_field,
                     pattern_mask=mask, pattern_saved=saved, model_picked=ask is not None)
    with Progress("ledger teach", listener=progress) as prog:
        if use_images and image_reader is None:
            prog.update("starting the local image reader…")
            image_reader = se.default_image_reader()
        readers: List[_Reader] = []
        run = se.extract(schema, [wb], mapping, ignore, image_reader=image_reader,
                         use_images=use_images, progress=prog, pattern_mask=mask or None,
                         cancel=cancel, taught=taught, reader_cls=_Reader, readers=readers)
        # not the review's 'N values have no Part Number - fill the Part
        # column in the review': Teach asks for the part itself
        s.problems += [p for p in run.problems if "fill the Part column" not in p]
        s.pictures = len(run.images)
        s.pictures_read = sum(1 for n in run.images if n.reader not in ("", "none installed")
                              and not n.text.startswith(("reader failed", "(no text found)",
                                                         "not read")))
        if run.cancelled or (cancel is not None and cancel.is_set()):
            s.cancelled = True
            return s
        if not readers:
            return s                                        # not read: the problems say why
        prog.stage("lining up what was found")
        rows, people, found = _Rows(), {}, []
        sources = _sources(readers, run.images)
        for src in sources:
            _markers(src, rows, people)
            _parts(src, rows, schema, wb.name, found)
            _items(src, rows)
            _values(src, rows, schema, people)
        for src in sources:
            if src.picture:
                _picture_alone(src, rows)
        if ask is not None:
            _ask_labels(s, rows, schema, ask, report, prog, csv_path, people)
            if not found and not s.cancelled:
                _ask_part(s, rows, sources, wb.name, mask, ask, report, prog, csv_path, found)
            s.model = report.model or ("Council's model" if report.labels_asked
                                       or report.parts_asked else "")
            if report.unavailable and not s.model.endswith("(did not answer)"):
                s.model = (s.model or "Council's model") + " (did not answer)"
            s.problems += report.problems
            if cancel is not None and cancel.is_set():
                s.cancelled = True
        s.parts = list(dict.fromkeys(p.value for p in found))
        s.part = found[0] if found else None
        known = se.load_people(csv_path)
        s.inspectors = [Person(k, known.get(k, ""), where, n)
                        for k, (n, where) in sorted(people.items())]
        s.rows, s.hidden = _ordered(list(rows.by_key.values()), max_rows)
        if s.hidden:
            s.problems.append(f"{s.hidden} more thing(s) found are not shown (the surest rule "
                              "matches): answer these, Save, then Guess again")
        prog.finish(f"{len(s.rows)} thing(s) to answer")
    return s


def _sources(readers: Sequence[_Reader], images) -> List[_Source]:
    """Each reader as the dialog names it: a picture's grid is 'picture N
    on <sheet>', with the text its reader read."""
    named: Dict[Tuple[str, str], Tuple[str, str]] = {}
    per_sheet: Counter = Counter()
    for n in images:
        per_sheet[n.sheet] += 1
        key = (n.sheet, Path(n.saved_as).name if n.saved_as else n.anchor)
        named.setdefault(key, (f"picture {per_sheet[n.sheet]} on {n.sheet}", n.text))
    out = []
    for i, ex in enumerate(readers):
        pic, text = named.get((ex.g.sheet, ex.g.from_image), ("", "")) if ex.g.from_image \
            else ("", "")
        out.append(_Source(ex, i, pic or (f"picture on {ex.g.sheet}" if ex.g.from_image else ""),
                           text))
    return out


def _ordered(rows: List[TeachRow], most: int) -> Tuple[List[TeachRow], int]:
    """Most in need of an answer first: the part number's, the inspectors',
    the flaw-code columns', then the labels the model guessed or nobody
    placed, then the rules' weak and sure matches, then those placed by an
    earlier answer. ``most`` of them, and how many were left out."""
    kind = {"part": 0, "inspector": 1, "item": 2, "label": 3}

    def need(r: TeachRow) -> int:
        g = r.guess
        if g.via == VIA_MODEL:
            return 0
        if g.via == VIA_NONE:
            return 1
        if g.via == VIA_RULE:
            return 2 if g.score < se.ACCEPT_AT else 3
        return 4
    rows.sort(key=lambda r: (kind.get(r.kind, 9), need(r) if r.kind == "label" else 0,
                             r.order))
    most = max(0, int(most))
    return rows[:most], max(0, len(rows) - most)


def _ask_labels(s: TeachSession, rows: _Rows, schema: TemplateSchema, ask, report,
                prog, csv_path, people: Dict[str, List[Any]]) -> None:
    """Council's model on the labels nobody placed: which field - or role -
    each is, with the user's past answers as examples. Only labels are
    sent."""
    from council_core import ledger_llm
    unsure = [r for r in rows.by_key.values() if r.kind == "label" and r.guess.via == VIA_NONE]
    labels = sorted({r.label for r in unsure if len(r.label) <= 80})[:150]
    if not labels or report.unavailable:
        return
    roles = _ask_roles(schema)
    fields = [f.name for f in schema.fields if f.name != schema.id_field] + list(roles.values())
    prog.stage(f"asking Council's model about {len(labels)} label(s)")
    report.labels_asked = len(labels)
    report.asking = "the labels nobody placed"
    pairs = label_examples(csv_path, schema, roles=True)
    try:
        mapping, report.model = ledger_llm.map_labels(
            labels, fields, ask, examples=ledger_llm.pick_examples(pairs, labels))
    except (ledger_llm.ModelUnavailable, ledger_llm.ModelFailedAgain):
        report.labels_failed = True
        return                                              # said once, by one_shot
    except Exception as exc:                                # noqa: BLE001
        if not report.cancelled:
            report.labels_failed = True
            report.problems.append(f"Council's model was not asked about the labels: {exc}")
        return
    back = {v: k for k, v in roles.items()}
    for r in unsure:
        answer = mapping.get(r.label)
        if not answer:
            continue
        role = back.get(answer, ROLE_FIELD)
        r.guess = Guess(role, answer if role == ROLE_FIELD else "", VIA_MODEL,
                        ledger_llm.MODEL_SCORE)
        r.note = "; ".join(x for x in r.note.split("; ") if x and not x.startswith("nearest"))
        report.labels_mapped += 1
        if role == ROLE_INSPECTOR:
            # the initials beside it go in the People section, to be named
            _people_add(people, r.example, r.where, n=0)
            r.initials = initials_in(r.example)


def _ask_part(s: TeachSession, rows: _Rows, sources: Sequence[_Source], wb_name: str,
              mask: str, ask, report, prog, csv_path, found: List[PartGuess]) -> None:
    """No part number found by the rules: Council's model is asked which
    part the workbook is about (its answer must be written there), with the
    part numbers the user confirmed before as examples."""
    from council_core import ledger_llm
    if report.unavailable:
        return
    prog.stage("asking Council's model which part this workbook is about")
    grids = [src.ex.g for src in sources]
    part, where = ledger_llm.find_part(wb_name, grids, mask or None, ask, report,
                                       examples=part_examples(csv_path))
    if not part:
        return
    label, at = "", None
    for src in sources:
        hit = next(((r, c) for (r, c) in src.ex.g.cells if src.ex.g.where(r, c) == where), None)
        if hit is not None:
            label, at = _label_near(src.ex, hit[0], hit[1], part), (src, hit)
            break
    shown = at[0].where(*at[1]) if at else where
    found.append(PartGuess(part, label, shown, VIA_MODEL, ledger_llm.MODEL_SCORE,
                           f"suggested by Council's model from {shown} — check it"))
    if not label or at is None:
        return
    src, (r, c) = at
    row = rows.by_key.get(f"{src.name}|{normalize_label(label)}")
    if row is None:
        rows.add(src, "part", label, r, c, Guess(ROLE_PART, "", VIA_MODEL,
                                                  ledger_llm.MODEL_SCORE), example=part)
    elif row.guess.via in (VIA_NONE, VIA_MODEL):
        row.guess = Guess(ROLE_PART, "", VIA_MODEL, ledger_llm.MODEL_SCORE)
        row.example = _clip(part)
        row.note = "; ".join(x for x in row.note.split("; ")
                             if x and not x.startswith("nearest"))
    else:
        row.note = se._join(row.note, f"Council's model read the part number {part} here")


# ── the answers ─────────────────────────────────────────────────────────
@dataclass
class LearnReport:
    workbook: str = ""
    mapped: Dict[str, str] = field(default_factory=dict)     # label -> field
    ignored: List[str] = field(default_factory=list)
    who_labels: List[str] = field(default_factory=list)
    not_who: List[str] = field(default_factory=list)
    item_labels: List[str] = field(default_factory=list)
    part_labels: List[str] = field(default_factory=list)     # now the part number's
    people: Dict[str, str] = field(default_factory=dict)
    part: str = ""
    pattern: str = ""
    examples: int = 0
    kg: List[str] = field(default_factory=list)
    unchanged: int = 0       # answers the rules already read that way
    unsure: int = 0
    conflicts: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def learned(self) -> bool:
        return bool(self.mapped or self.ignored or self.who_labels or self.not_who
                    or self.item_labels or self.part_labels or self.people or self.pattern
                    or self.examples)

    def describe(self) -> str:
        """What was learned, for the Ledger log and the dialog."""
        name = Path(self.workbook).name if self.workbook else "the sample workbook"
        lines = [f"Learned from {name}:"]
        if self.mapped:
            lines.append(f"  {len(self.mapped)} label(s) mapped to template fields: "
                         + "; ".join(f"{k} → {v}" for k, v in list(self.mapped.items())[:12])
                         + (" …" if len(self.mapped) > 12 else ""))
        if self.part_labels:
            lines.append("  part-number label(s): " + ", ".join(self.part_labels))
        if self.who_labels:
            lines.append("  inspector label(s): " + ", ".join(self.who_labels))
        if self.not_who:
            lines.append("  not an inspector: " + ", ".join(self.not_who))
        if self.item_labels:
            lines.append("  flaw-code column(s): " + ", ".join(self.item_labels))
        if self.ignored:
            lines.append("  ignored (not data): " + ", ".join(self.ignored[:12])
                         + (" …" if len(self.ignored) > 12 else ""))
        if self.people:
            lines.append("  people: " + ", ".join(f"{k} = {v}"
                                                  for k, v in sorted(self.people.items())))
        if self.part:
            lines.append(f"  part number: {self.part}")
        if self.pattern:
            lines.append(f"  part-name pattern saved: {self.pattern!r} (Extract uses it when "
                         "the pattern field is empty)")
        if self.unchanged:
            lines.append(f"  {self.unchanged} answer(s) the rules already read that way")
        if self.examples:
            lines.append(f"  {self.examples} answer(s) kept as examples for Council's model")
        lines += [f"  knowledge graph: {k}" for k in self.kg]
        if len(lines) == 1:
            lines.append("  nothing new")
        return "\n".join(lines + [f"  ! {c}" for c in self.conflicts + self.problems])


def learn(csv_path, session: TeachSession, answers: Dict[str, Any],
          people: Optional[Dict[str, str]] = None, *, part: Optional[str] = None,
          save_pattern: bool = False, connections_vault: Any = None) -> LearnReport:
    """Keep what the user's answers mean (see the module's docstring), so
    the next Extract, Apply and rebuild use them without a model. Run it on
    a worker thread: it may open the knowledge graph.

    Raises LedgerError, writing nothing, when the aliases file cannot be
    read: the user's mappings would be lost."""
    ledger = ParentCSV(csv_path)
    schema = ledger.schema
    if schema is None:
        raise LedgerError(f"{Path(csv_path).name} has no schema beside it")
    ap = se.aliases_path_for(csv_path)
    if ap.exists():
        try:
            if not isinstance(json.loads(ap.read_text(encoding="utf-8")), dict):
                raise ValueError("not a JSON object")
        except Exception as exc:                            # noqa: BLE001
            raise LedgerError(f"{ap.name} could not be read, so nothing was learned (fix or "
                              f"remove it): {exc}") from exc
    mapping, ignore = se.load_aliases(csv_path)
    taught = se.load_taught(csv_path)
    m = se.Matcher(schema, mapping, ignore, taught=taught)
    rep = LearnReport(workbook=session.workbook)
    fields = {f.name for f in schema.fields}
    new_map, new_ignore = dict(mapping), list(ignore)
    who, not_who, items = list(taught.who), list(taught.not_who), list(taught.items)
    kept: List[Example] = []
    field_labels: List[Tuple[str, str]] = []
    seen: Dict[str, Tuple[str, str, str]] = {}
    book = Path(session.workbook).name

    def drop(lst: List[str], key: str) -> None:
        lst[:] = [x for x in lst if normalize_label(x) != key]

    def add(lst: List[str], text: str, out: List[str]) -> None:
        if normalize_label(text) not in {normalize_label(x) for x in lst}:
            lst.append(text)
            out.append(text)

    for row in session.rows:
        if row.key not in answers:
            continue
        role, fname = parse_choice(answers[row.key])
        if role == ROLE_FIELD and fname == schema.id_field:
            role, fname = ROLE_PART, ""
        if role == ROLE_UNSURE:
            rep.unsure += 1
            continue
        if role == ROLE_FIELD and fname not in fields:
            rep.problems.append(f"{fname!r} is not a field of this Ledger's template: "
                                f"{row.label!r} was not learned")
            continue
        key = normalize_label(row.label)
        if not key:
            continue
        said = seen.get(key)
        if said is not None and said[:2] != (role, fname):
            rep.conflicts.append(f"{row.label!r} was answered two ways ({said[2]} and "
                                 f"{row.where}): the first answer was kept")
            continue
        seen[key] = (role, fname, row.where)
        # a heading of initials ('JB — 2026-09-18') or a sheet named for
        # someone is a value, not a label: it can only be someone, or no one
        heading = row.kind == "inspector" and row.shape in ("heading", "sheet name")
        if row.kind == "inspector" and role != ROLE_INSPECTOR:
            # the Ledger's own words read it as a person: no one, now
            for t in (row.initials if heading else [row.label]):
                add(not_who, t, rep.not_who)
        if heading:
            if role == ROLE_INSPECTOR:
                for t in row.initials:
                    drop(not_who, normalize_label(t))
                rep.unchanged += _rules_knew(row, role, fname, m)
            continue
        if _rules_knew(row, role, fname, m):
            rep.unchanged += 1
        elif role in (ROLE_FIELD, ROLE_PART):
            new_map[key] = fname if role == ROLE_FIELD else schema.id_field
            if role == ROLE_FIELD:
                rep.mapped[row.label] = fname
            for lst in (new_ignore, who, items):
                drop(lst, key)
        elif role == ROLE_INSPECTOR:
            drop(not_who, key)
            add(who, row.label, rep.who_labels)
            new_map.pop(key, None)
            for lst in (new_ignore, items):
                drop(lst, key)
        elif role == ROLE_ITEM:
            add(items, row.label, rep.item_labels)
            new_map.pop(key, None)
            for lst in (new_ignore, who):
                drop(lst, key)
        elif role == ROLE_IGNORE:
            if key not in {normalize_label(x) for x in new_ignore}:
                new_ignore.append(key)
                rep.ignored.append(row.label)
            new_map.pop(key, None)
            for lst in (who, items):
                drop(lst, key)
        if role == ROLE_PART:
            rep.part_labels.append(row.label)
        elif role == ROLE_FIELD:
            field_labels.append((row.label, fname))
        kept.append(Example(row.label, role, fname, row.item if role == ROLE_FIELD else "",
                            row.example if role == ROLE_PART and len(row.example)
                            <= EXAMPLE_CHARS else "", book, row.sheet,
                            datetime.now().isoformat(timespec="seconds")))
    if new_map != mapping or sorted(set(new_ignore)) != sorted(set(ignore)):
        se.save_aliases(csv_path, new_map, new_ignore)
    if (who, not_who, items) != (taught.who, taught.not_who, taught.items):
        taught.who, taught.not_who, taught.items = who, not_who, items
        taught = se.save_taught(csv_path, taught)
    # who the inspectors are
    if people:
        known = se.load_people(csv_path)
        new = {k: v for k, v in people.items() if " ".join(str(v or "").split())
               and known.get(se._initials(k)) != " ".join(str(v).split())}
        if new:
            se.save_people(csv_path, new)
            rep.people = {se._initials(k): " ".join(str(v).split()) for k, v in new.items()}
    # the part number, and its shape as a part-name pattern
    part = " ".join(str(part or "").split())
    if part:
        rep.part = part
        label = session.part.label if session.part is not None else ""
        kept.append(Example(label, ROLE_PART, "", "", part, book, "",
                            datetime.now().isoformat(timespec="seconds")))
    if save_pattern:
        if not part:
            rep.problems.append("no part number was given, so no part-name pattern was saved")
        else:
            mask = se.mask_from_part(part)
            se.mask_to_regex(mask)                          # a pattern Extract can read
            taught.patterns = [mask] + [p for p in taught.patterns if p != mask]
            se.save_taught(csv_path, taught)
            rep.pattern = mask
    rep.examples = _add_examples(csv_path, kept)
    if connections_vault is not None and (rep.part_labels or field_labels):
        rep.kg = _teach_graph(connections_vault, schema, rep.part_labels, field_labels)
    return rep


def _rules_knew(row: TeachRow, role: str, fname: str, m: se.Matcher) -> bool:
    """Did the Ledger already read ``row`` the way the user answered - an
    earlier answer, or a rule sure of it? Then nothing is written for it
    (an example is kept). A one-word label shared by many fields ('Date')
    is not remembered when the rules guessed it right: too generic
    (apply_review's rule)."""
    g = row.guess
    if (g.role, g.field) != (role, fname):
        return False
    if g.via == VIA_ALIAS:
        return True
    if g.via != VIA_RULE:
        return False
    if role == ROLE_FIELD:
        return g.score >= se.ACCEPT_AT or m.is_generic(row.label)
    return True


def _teach_graph(vault: Any, schema: TemplateSchema, part_labels: Sequence[str],
                 field_labels: Sequence[Tuple[str, str]]) -> List[str]:
    """Tick the knowledge graph's field rules the answers prove: a label
    answered as the part number is the graph's PART - its rule ticked, or
    the label added as a confirmed other name of the id field's rule (also
    ticked); a label mapped to a template field whose name is a CONFIRMED
    rule is added as that rule's other name. Inspectors need nothing ticked:
    the Ledger's rows name them (its who_labels). A graph being rebuilt
    elsewhere is said, never waited for. What was done, as lines."""
    from council_core import knowledge_graph as kgm
    import field_search as fs
    try:
        kg = vault.open_graph() if hasattr(vault, "open_graph") else kgm.KnowledgeGraph(vault)
    except kgm.KnowledgeGraphBusy:
        return ["being rebuilt elsewhere, so its field rules were not ticked — Save again "
                "when it finishes"]
    except Exception as exc:                                # noqa: BLE001
        return [f"not updated: {exc}"]
    out: List[str] = []
    try:
        with kg:
            rules = {fs._norm_key(r.label): (r, st) for r, st in kg.field_rules()}
            idf = schema.field(schema.id_field)
            base = next((rules[k] for k in (fs._norm_key(idf.label), fs._norm_key(idf.name),
                                            fs._norm_key("Part Number"))
                         if k in rules and rules[k][0].type == "PART"), None)
            for label in dict.fromkeys(part_labels):
                k = fs._norm_key(label)
                got = rules.get(k)
                if got is not None:
                    rule, status = got
                    if rule.type != "PART":
                        out.append(f"{label!r} is its {rule.type.lower()} rule {rule.label!r}; "
                                   "left as it is")
                    elif status != "confirmed":
                        kg.set_rule_status(rule.label, "confirmed")
                        rules[k] = (rule, "confirmed")
                        out.append(f"ticked {rule.label!r} (a part number)")
                    continue
                if base is None:
                    out.append(f"no part-number rule to add {label!r} to")
                    continue
                kg.add_label_synonym(label, base[0].label)
                rules[k] = (base[0].renamed(label), "confirmed")
                out.append(f"added {label!r} as another name for {base[0].label!r}")
            if part_labels and base is not None and base[1] != "confirmed":
                kg.set_rule_status(base[0].label, "confirmed")
                out.append(f"ticked {base[0].label!r} (the template's part number)")
            for label, fname in field_labels:
                f = schema.field(fname)
                plain = fname.split(": ", 1)[-1]
                got = next((rules[k] for k in (fs._norm_key(se.split_unit(f.label)[0]),
                                               fs._norm_key(f.label), fs._norm_key(plain))
                            if k in rules), None)
                k = fs._norm_key(label)
                if got is None or got[1] != "confirmed" or k in rules:
                    continue
                kg.add_label_synonym(label, got[0].label)
                rules[k] = (got[0].renamed(label), "confirmed")
                out.append(f"added {label!r} as another name for {got[0].label!r}")
    except Exception as exc:                                # noqa: BLE001
        if kgm._busy(exc) or isinstance(exc, kgm.KnowledgeGraphBusy):
            out.append("being rebuilt elsewhere, so not every field rule was ticked — Save "
                       "again when it finishes")
        else:
            out.append(f"not every field rule was ticked: {exc}")
    return out


def improvement(before: TeachSession, after: TeachSession) -> str:
    """What 'Guess again' shows after Save: the things the model had to
    guess (or nobody placed) that the Ledger's rules and the user's answers
    place now, and what is still unsure."""
    was = {r.key: r for r in before.rows}
    now = [r for r in after.rows if r.key in was
           and was[r.key].guess.via in (VIA_MODEL, VIA_NONE)
           and r.guess.via in (VIA_ALIAS, VIA_RULE)]
    still = sum(1 for r in after.rows if r.guess.via == VIA_NONE)
    lines = [f"{len(now)} thing(s) Council's model had to guess, or nobody placed, are now "
             "placed by the rules and your answers"
             + (": " + ", ".join(f"{r.label} → {r.guess.text(after.item_field)}"
                                 for r in now[:8]) + (" …" if len(now) > 8 else "")
                if now else "")]
    if before.part is not None and after.part is not None \
            and before.part.via == VIA_MODEL and after.part.via in (VIA_ALIAS, VIA_RULE):
        lines.append(f"the part number {after.part.value} is now read by the rules "
                     f"({after.part.how})")
    lines.append(f"{still} still unsure" if still else "nothing left unsure")
    return "; ".join(lines) + "."


def answers_from_guesses(session: TeachSession) -> Dict[str, str]:
    """Every row's guess as its answer: what the dialog pre-selects."""
    return {r.key: r.guess.key for r in session.rows}


__all__ = [
    "ROLES", "ROLE_FIELD", "ROLE_PART", "ROLE_INSPECTOR", "ROLE_ITEM", "ROLE_IGNORE",
    "ROLE_UNSURE", "VIA_RULE", "VIA_ALIAS", "VIA_MODEL", "VIA_NONE", "MAX_ROWS",
    "Guess", "TeachRow", "PartGuess", "Person", "TeachSession", "LearnReport", "Example",
    "choice_key", "parse_choice", "role_text", "choices", "guess", "learn", "improvement",
    "answers_from_guesses", "initials_in", "examples", "examples_path_for", "label_examples",
    "part_examples",
]
