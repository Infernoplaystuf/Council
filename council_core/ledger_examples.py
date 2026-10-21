"""
council_core.ledger_examples — the Ledger learns by example from filled
copies of the template (user, 2026-10-21: "Lets do a learn by example from
filled copies addition").

A filled copy of the template is a worked answer: it says which value is
which field. So is every record already accepted into the parent CSV. When
the same part (or record) is in a messy source too, the values line up:
the label or column whose values keep being one field's values is that
field - 'HP' held 6300, 4480 and 7980 where the filled copy has Health
6300, 4480 and 7980.

    template_layout(path)            the template's own words, by sheet and cell
    is_filled_copy(cells, layout)    a sheet laid out like the template
    filled_rows(cells, schema, ...)  a filled copy's rows (sample Ledger), by cell
    Examples                         the worked answers, by part/record and field
    learn_labels(run, schema, ex)    the sample Ledger's unplaced labels, learned
    learn_column(...)                which field one column of a table is

What is learned is only ever a suggestion (via / how 'example', amber,
noted with the example it came from): accepting it teaches the label, as
any other answer does. Evidence is counted, never taken from one value:
EXAMPLE_MIN agreeing values, at least EXAMPLE_SHARE of those compared, and
at least one that says something (not 0, 1, Yes, OK ...).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

#: the least number of agreeing values that teaches a label
EXAMPLE_MIN = 2
#: of the values compared, at least this share must agree
EXAMPLE_SHARE = 0.8
#: a filled copy has at least this share of the template's own words in place
LAYOUT_SHARE = 0.8
#: and the template has at least this many words to compare
LAYOUT_MIN_WORDS = 4
#: values that agree too easily (M, F, Yes) teach only when this many do,
#: and none disagrees
COMMON_MIN = 4
#: an example-learned value's score: under every pre-accept threshold
EXAMPLE_SCORE = 0.75
#: values that agree too easily to count as evidence on their own
TRIVIAL = {"", "0", "1", "y", "n", "yes", "no", "ok", "pass", "fail", "true", "false",
           "x", "na", "n a", "none", "seen", "not seen", "m", "f", "-", "nil", "tbd"}
EXAMPLE_NOTE = "learned from your filled copies"


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text if text is not None else "").lower()).strip()


def value_key(v: Any) -> str:
    """A value as examples compare it: numbers by value (6300 = 6300.0 =
    '6,300'), text by its words (case and punctuation aside)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        f = float(v)
        return repr(round(f, 9)) if f == f else ""
    s = str(v).strip()
    t = s.replace(",", "")
    if re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", t):
        try:
            return repr(round(float(t), 9))
        except ValueError:
            pass
    return _norm(s)


def distinctive(v: Any) -> bool:
    """A value whose agreement says something: not blank, not a yes/no or
    0/1, not one letter."""
    k = value_key(v)
    if not k or k in TRIVIAL or k in ("0.0", "1.0"):
        return False
    return len(k) >= 2


# ── a filled copy of the template ───────────────────────────────────────
def template_layout(path) -> Dict[str, Dict[Tuple[int, int], str]]:
    """The template's own words by sheet and cell: what a filled copy has
    in the same places."""
    import openpyxl
    out: Dict[str, Dict[Tuple[int, int], str]] = {}
    if not path or not Path(path).is_file():
        return out
    try:
        # not read_only: a template is small, and a read-only workbook's
        # parser, collected later on another thread, crashed the process
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception:                                       # noqa: BLE001
        return out
    for ws in wb.worksheets:
        words: Dict[Tuple[int, int], str] = {}
        for (r, c), cell in list(ws._cells.items())[:200_000]:
            v = cell.value
            if isinstance(v, str) and re.search(r"[A-Za-z]", v) and len(v) <= 120 \
                    and not v.startswith("="):
                words[(r, c)] = _norm(v)
        if words:
            out[ws.title] = words
    return out


def is_filled_copy(cells: Dict[Tuple[int, int], Any],
                   layout: Dict[str, Dict[Tuple[int, int], str]]) -> Optional[str]:
    """The template sheet this sheet is a filled copy of (its words in the
    same cells, LAYOUT_SHARE of them), or None."""
    best, best_share = None, 0.0
    for name, words in layout.items():
        if len(words) < LAYOUT_MIN_WORDS:
            continue
        same = sum(1 for rc, w in words.items() if _norm(cells.get(rc)) == w)
        share = same / len(words)
        if share >= LAYOUT_SHARE and share > best_share:
            best, best_share = name, share
    return best


def filled_rows(cells: Dict[Tuple[int, int], Any], schema, template_sheet: str
                ) -> List[Dict[str, str]]:
    """A filled copy's rows, read by the template's own cells (as
    sample_ledger.read_filled reads a whole workbook): one per item, the
    part-level fields on each. [] when it names no part."""
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
    from sample_ledger import _text

    def at(a1: str) -> str:
        try:
            col, row = coordinate_from_string(a1)
            return _text(cells.get((row, column_index_from_string(col))))
        except Exception:                                   # noqa: BLE001
            return ""
    mine = [f for f in schema.fields if f.sheet == template_sheet] or list(schema.fields)
    part = {f.name: at(f.cells[""]) for f in mine if not f.per_item and "" in f.cells}
    if not part.get(schema.id_field):
        return []
    if not schema.item_field:
        return [part]
    rows = []
    for item in schema.items:
        row = dict(part)
        row[schema.item_field] = item
        for f in mine:
            if f.per_item and item in f.cells:
                row[f.name] = at(f.cells[item])
        rows.append(row)
    return rows


# ── the worked answers ──────────────────────────────────────────────────
@dataclass
class Examples:
    """Worked answers: per key (a part, or a record's key), per item ('' for
    none), the field -> value they hold, and where each came from."""
    values: Dict[str, Dict[str, Dict[str, str]]] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict)        # key -> where from

    def add(self, key: Any, item: Any, row: Dict[str, Any], source: str,
            skip: Iterable[str] = ()) -> None:
        k = _norm(key)
        if not k:
            return
        skip = set(skip)
        mine = self.values.setdefault(k, {}).setdefault(_norm(item), {})
        for f, v in row.items():
            if f in skip or v in (None, ""):
                continue
            mine.setdefault(f, str(v))
        self.sources.setdefault(k, source)

    def __bool__(self) -> bool:
        return bool(self.values)

    def fields_for(self, key: Any, item: Any = "") -> Dict[str, str]:
        return self.values.get(_norm(key), {}).get(_norm(item), {})

    def where(self, key: Any) -> str:
        return self.sources.get(_norm(key), "")


def sample_examples(schema, rows: Iterable[Dict[str, Any]], source: str,
                    into: Optional[Examples] = None) -> Examples:
    """The sample Ledger's rows (a filled copy's, or the parent CSV's) as
    worked answers, by part and item."""
    ex = into if into is not None else Examples()
    id_field = getattr(schema, "id_field", "")
    item_field = getattr(schema, "item_field", "") or ""
    meta = {id_field, item_field, "source_file", "updated_at"}
    for row in rows:
        src = str(row.get("source_file") or source)
        ex.add(row.get(id_field), row.get(item_field, "") if item_field else "", row, src,
               skip=meta)
    return ex


# ── learning (the sample Ledger) ────────────────────────────────────────
@dataclass
class Learned:
    field: str
    agree: int
    compared: int
    sources: List[str]

    def note(self, label: str) -> str:
        where = ", ".join(sorted(set(self.sources))[:2])
        return (f"{EXAMPLE_NOTE} ({where}): '{label}' held {self.field}'s values "
                f"{self.agree} of {self.compared} times")


def _decide(counts: Counter, compared: int, distinct_hits: Dict[str, Set[str]]
            ) -> Optional[Tuple[str, int]]:
    if not counts:
        return None
    ranked = counts.most_common()
    best, n = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == n:
        return None                                          # two fields as likely
    if n < EXAMPLE_SHARE * compared:
        return None
    if not distinct_hits.get(best) and not (n >= COMMON_MIN and n == compared):
        # values that agree too easily (M / F, Yes / No) count only when
        # many agree and none disagrees
        return None
    if n < EXAMPLE_MIN and n != compared:
        # one agreeing value is enough only when nothing disagreed (a part's
        # one Scan Speed, 1200, equal to no other field of the answer)
        return None
    return best, n


def learn_labels(run, schema, examples: Examples) -> Dict[str, Learned]:
    """Labels no rule placed whose values are one field's in the worked
    answers for the same part (and flaw): their values become suggested rows
    (via 'example'), in place of their 'Labels to map' rows. Returns what
    was learned, by label words."""
    import source_extract as se
    if not examples:
        return {}
    known = {f.name for f in schema.fields}
    per_item = {f.name for f in schema.fields if f.per_item}
    counts: Dict[str, Counter] = defaultdict(Counter)
    compared: Counter = Counter()
    distinct: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    where: Dict[str, List[str]] = defaultdict(list)
    for o in run.unmatched:
        lab = _norm(o.label)
        if not lab or not o.part:
            continue
        if o.item:
            answers = dict(examples.fields_for(o.part, ""))
            answers.update(examples.fields_for(o.part, o.item))
        else:
            # a part-level value: the part's own fields, kept on every flaw's row
            answers = {}
            for by_item in examples.values.get(_norm(o.part), {}).values():
                for f, v in by_item.items():
                    if f not in per_item:
                        answers.setdefault(f, v)
        if not answers:
            continue
        vk = value_key(o.value)
        if not vk:
            continue
        compared[lab] += 1
        for f, v in answers.items():
            if f in known and value_key(v) == vk:
                counts[lab][f] += 1
                if distinctive(o.value):
                    distinct[lab][f].add(vk)
                where[lab].append(examples.where(o.part))
    learned: Dict[str, Learned] = {}
    for lab, c in counts.items():
        got = _decide(c, compared[lab], distinct[lab])
        if got:
            learned[lab] = Learned(got[0], got[1], compared[lab], where[lab])
    if not learned:
        return learned
    keep = []
    for o in run.unmatched:
        hit = learned.get(_norm(o.label))
        if hit is None or se.needs_item(schema, hit.field, o.item):
            keep.append(o)
            continue
        run.obs.append(replace(o, field=hit.field, score=EXAMPLE_SCORE, via="example",
                               note=se._join(o.note, hit.note(o.label))))
    run.unmatched = keep
    return learned


# ── learning (the table Ledger) ─────────────────────────────────────────
def learn_column(values: Sequence[Any], keys: Sequence[Any], examples: Examples,
                 free: Sequence[str]) -> Optional[Learned]:
    """Which of the ``free`` fields one column of a table is: its values,
    row by row, against the worked answer of each row's record (``keys``,
    the record's key value per row)."""
    counts: Counter = Counter()
    distinct: Dict[str, Set[str]] = defaultdict(set)
    compared = 0
    srcs: List[str] = []
    for v, k in zip(values, keys):
        answers = examples.fields_for(k, "")
        vk = value_key(v)
        if not answers or not vk:
            continue
        compared += 1
        for f in free:
            if f in answers and value_key(answers[f]) == vk:
                counts[f] += 1
                if distinctive(v):
                    distinct[f].add(vk)
                srcs.append(examples.where(k))
    got = _decide(counts, compared, distinct)
    return Learned(got[0], got[1], compared, srcs) if got else None


def key_column(columns: Sequence[Tuple[int, Sequence[Any]]], examples: Examples
               ) -> Optional[Tuple[int, int]]:
    """The column of a table whose values are the worked answers' record
    keys (a 'Creature' column holding the names a filled copy has under
    Name): (its index, how many matched), or None."""
    best = None
    for j, vals in columns:
        vals = [v for v in vals if v not in (None, "")]
        if len(vals) < EXAMPLE_MIN:
            continue
        hits = sum(1 for v in vals if _norm(v) in examples.values)
        if hits >= EXAMPLE_MIN and hits >= 0.5 * len(vals) and (best is None or hits > best[1]):
            best = (j, hits)
    return best
