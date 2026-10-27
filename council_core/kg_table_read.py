"""A model reads each table (Part C of the proposal agreed with the user,
2026-10-27): it is shown the table's card - its file, sheet, column labels
and first rows (kg_related.table_card) - and says in a sentence what the
table is, and which columns hold people, parts or projects.

Toolkit-free; the model call goes through an injected ``chat`` (the
Connections tab's local model). As with kg_extract, the model is asked for
little and checked hard:

  * a column must be one of the table's own labels (case and spacing aside);
  * the type must be PERSON, PART or PROJECT - what a field rule can read;
  * the column's values must fit the type as a rule would read them
    (kg_excel.plausible: a person is a name or initials, a part number has a
    digit, a project is short text) - most of them, not just one - and never
    be dates, amounts or sentences;
  * a label that names a date, measurement, status or note ('Start Date',
    'Unit Price', 'Notes') is never taken;
  * a label that already has a rule - ticked, proposed or rejected by the
    user - is left as it is.

What survives is a PROPOSED field rule: nothing is read with it until the
user ticks it ('Tick the suggested field rules', or Fields…); then every row
of every spreadsheet and CSV with that column is read with it at the next
rebuild. The sentence is the TABLE node's description.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Sequence, Tuple

#: What a column may be said to hold: the types a field rule reads.
TYPES = ("PERSON", "PART", "PROJECT")
#: Share of a column's values (of a sample) that must fit the type.
FIT_SHARE = 0.6
#: Values of a column looked at when checking the fit.
FIT_SAMPLE = 50
NUM_PREDICT = 600
SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "maxLength": 300},
        "columns": {"type": "array", "maxItems": 12, "items": {
            "type": "object",
            "properties": {"column": {"type": "string", "maxLength": 80},
                           "holds": {"type": "string", "enum": list(TYPES)}},
            "required": ["column", "holds"]}}},
    "required": ["description", "columns"],
}
_SYSTEM = (
    "You read one table from a user's files. Say in one plain sentence what the "
    "table records. Then list ONLY the columns whose values are people (names or "
    "initials), parts (part numbers, serial numbers, sample or specimen IDs) or "
    "projects (project or program names or codes). Leave out columns of "
    "measurements, dates, results, statuses, notes and quantities. Use the column "
    "labels exactly as written. Answer in JSON.")

ChatFn = Callable[..., str]


@dataclass
class Meaning:
    column: str                # the table's own label
    type: str


@dataclass
class Reading:
    description: str = ""
    meanings: List[Meaning] = field(default_factory=list)
    rejected: List[Tuple[Dict[str, Any], str]] = field(default_factory=list)
    error: str = ""


def build_prompt(card: str) -> List[Dict[str, str]]:
    return [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": card}]


def parse(reply: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", reply or "", re.S)
    if not m:
        raise ValueError("the model's answer has no JSON")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("the model's answer is not a JSON object")
    return data


#: Labels no person, part or project column has, whatever a model says: a
#: careless stand-in model that called every column of the synthetic test set
#: a person, part or project got 'Date', 'Notes', 'Unit Price' and 'Length'
#: past the value checks (2026-10-27).
_NOT_AN_ENTITY_LABEL = re.compile(
    r"(?i)(^\(column\b|\b(date|time|day|week|month|year|note|notes|comment|comments|"
    r"remark|remarks|description|price|cost|qty|quantity|amount|value|length|width|"
    r"height|depth|size|weight|status|result|results|reading|response|total|count|"
    r"percent|file|path|url|units?|orientation|region|location)\b|%)")
_DATE = re.compile(r"^\d{4}-\d{1,2}(-\d{1,2}([ T].*)?)?$|^\d{1,2}/\d{1,2}/\d{2,4}$")
_DATE_IN_LABEL = re.compile(r"\d{4}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}")
_MEASURE = re.compile(r"^[-+]?\d+(\.\d+)?\s*[A-Za-zµ°%\"']{1,4}$")       # 0.245 mm, 2x
_NUMBER = re.compile(r"^[-+$€£]?\d[\d,]*(\.\d+)?%?$")


def _who_head(label: str) -> bool:
    from council_core import kg_excel
    try:
        return bool(kg_excel._who_head(label))
    except Exception:                                     # noqa: BLE001
        return False


def _norm(s: str) -> str:
    return " ".join(str(s or "").split()).casefold()


def fits(etype: str, values: Sequence[str], label: str = "") -> bool:
    """Do most of a column's values read as ``etype``, as a field rule would
    read them (kg_excel.plausible)? Stricter than a rule, since no person
    chose the column: a part is never a measurement, nor a bare number
    unless its label says it is an ID; a project is a name or code (a
    capital or a digit in it), never initials; a person has no digits."""
    from council_core import kg_excel
    from council_core import kg_tables
    from council_core import knowledge_graph as kgm
    id_label = bool(kg_tables._ID_LABEL.search(label))
    sample = [" ".join(str(v).split()) for v in values if str(v).strip()][:FIT_SAMPLE]
    if not sample:
        return False
    rule = kgm.FieldRule("?", etype)

    def one(v: str) -> bool:
        if _DATE.match(v):
            return False                       # a date is never one of them
        if etype == "PART":
            if _NUMBER.match(v) and not (v.isdigit() and id_label):
                return False                   # 12.5, 1,200, $40, a reading of 320
            if _MEASURE.match(v):
                return False                   # 0.245 mm
        elif etype == "PROJECT":
            if (len(v.split()) > 6 or _NUMBER.match(v) or kg_excel.is_initials(v)
                    or not any(ch.isupper() or ch.isdigit() for ch in v)):
                return False                   # a sentence, a number, 'JB', 'used'
        elif any(ch.isdigit() for ch in v):
            return False                       # W-500 is nobody
        return kg_excel.plausible(rule, v)
    ok = sum(1 for v in sample if one(v))
    return ok >= FIT_SHARE * len(sample)


def check(data: Dict[str, Any], labels: Sequence[str],
          values: Callable[[str], Sequence[str]]) -> Reading:
    """The model's answer, checked against the table: ``labels`` its column
    labels, ``values(label)`` that column's different values."""
    out = Reading(description=" ".join(str(data.get("description") or "").split())[:300])
    by_norm = {_norm(x): x for x in labels}
    seen = set()
    raw = data.get("columns")
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        label = by_norm.get(_norm(c.get("column", "")))
        etype = str(c.get("holds") or "").upper()
        if label is None:
            out.rejected.append((c, "no such column in the table"))
        elif _NOT_AN_ENTITY_LABEL.search(label) or _DATE_IN_LABEL.search(label):
            out.rejected.append((c, "its label names a measurement, date or note"))
        elif etype not in TYPES:
            out.rejected.append((c, f"'{etype}' is not a person, part or project"))
        elif label in seen:
            continue
        elif etype != "PERSON" and _who_head(label):
            out.rejected.append((c, "its label names a person"))
        elif not fits(etype, values(label), label):
            out.rejected.append((c, f"its values do not read as a {etype.lower()}"))
        else:
            seen.add(label)
            out.meanings.append(Meaning(label, etype))
    return out


def read(card: str, labels: Sequence[str], values: Callable[[str], Sequence[str]],
         chat: ChatFn) -> Reading:
    """One table read by the model and checked. An error (no model running,
    an answer that is not JSON) comes back in ``error``, not raised."""
    try:
        reply = chat(build_prompt(card), num_predict=NUM_PREDICT, schema=SCHEMA)
        return check(parse(reply), labels, values)
    except Exception as exc:                              # noqa: BLE001
        return Reading(error=f"{type(exc).__name__}: {exc}"[:300])


def rule_for(label: str, etype: str):
    """The proposed field rule a column meaning becomes: a person works on
    the record's project; a part or project is just that."""
    from council_core import knowledge_graph as kgm
    if etype == "PERSON":
        return kgm.FieldRule(label, "PERSON", (("PROJECT", "WORKS_ON"),))
    return kgm.FieldRule(label, etype)
