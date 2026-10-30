"""
council_core.ledger_questions — the Ledger asks the user what it cannot tell
on its own, and checks its own matches against the answers (user,
2026-10-30: "add a chat function in the ledger process directly ... I have a
filled template in and then the ledger finds it and requests info from me
of if it is a filled copy or not to help use to figure out answers"; and
"it can re-run after it runs once and asks questions and recieves the
answers and uses those to see if the data connections it made was
correct").

The questions come from a written review (ledger_review.Review), never
during a read: Extract never stops to wait for an answer. Four kinds:

    copy   a sheet laid out like the template (ledger_examples.is_filled_copy):
           is it a filled copy? trust its values / a guide only / a normal
           source / leave it out
    part   a file whose part was only guessed (file name, model, index,
           title): is it that part, another one, or no part file at all?
    clash  files disagree on one value: which is right (or another value)?
    match  a label matched to a field whose values differ from a trusted
           filled copy's: is the label that field?

An answer is written into the review (its Accept, Value and Part cells,
ledger_review.save), so Apply, the Excel review and the review windows
work as before. Answers are kept per Ledger, beside its parent CSV
(``<csv>.answers.json``), by file name and value, and applied again to
every later review of the Ledger (apply_saved): the next Extract or Update
does not ask again.

Re-checking (recheck) is the loop: the answers applied, then the matches
the run made compared with what the user confirmed - a trusted filled
copy's values. A label whose values agree with it is confirmed (its values
for the parts the copy does not cover are accepted when the label was the
only doubt); one that disagrees is a new question. It reads no workbook
again, so a round takes seconds; Re-run with my answers still reads them
all.

Nothing here imports Qt.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from council_core.ledger_examples import distinctive, value_key

#: at most this many 'which value is right' questions a round: the rest
#: are in the review, as before
CLASH_MAX = 15
#: at most this many values of a clash offered as answers
CLASH_CHOICES = 6
#: the re-checks a run makes by itself after answers (the Ledger tab)
ROUNDS_MAX = 3
#: a label is confirmed by at least this many agreeing values that say
#: something (ledger_examples.distinctive) and none that disagrees
CONFIRM_MIN = 2

#: why a value was held back that is only about its label's match
#: (source_extract.review_reasons): a label confirmed by a filled copy
#: answers them
LABEL_REASONS = ("label placed by the model", "label could be more than one field",
                 "label only a loose match for the field")


def _part_reasons() -> Tuple[str, ...]:
    from source_extract import PART_REASONS
    return tuple(PART_REASONS)


# ── the questions ───────────────────────────────────────────────────────
@dataclass
class Choice:
    key: str
    label: str


@dataclass
class Question:
    id: str                       # stable across runs: 'copy|file|sheet', …
    kind: str                     # copy | part | clash | match
    text: str
    choices: List[Choice]
    #: what a typed answer may be besides a choice: 'part', 'value' or ''
    free: str = ""
    facts: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Question":
        return cls(d["id"], d["kind"], d["text"],
                   [Choice(**c) for c in d.get("choices") or []],
                   d.get("free", ""), dict(d.get("facts") or {}))


COPY_TRUST, COPY_GUIDE, COPY_SOURCE, COPY_OUT = "trust", "guide", "source", "out"
PART_YES, PART_OTHER, PART_OUT = "yes", "other", "out"
CLASH_NONE, CLASH_TYPED = "none", "typed"
MATCH_YES, MATCH_NO = "yes", "no"


def _file(where: str) -> str:
    return str(where or "").split(" › ", 1)[0]


def _sheet_where(where: str) -> str:
    """'f.xlsx › Sheet!B7' -> 'f.xlsx › Sheet'."""
    return str(where or "").split("!", 1)[0]


def _held(it) -> List[str]:
    return [w.strip() for w in (it.held or "").split(";") if w.strip()]


def _values(review):
    return [i for i in review.items if i.sheet == "Values" and i.fields]


def _cell(it) -> Tuple[str, str, Tuple[str, ...]]:
    return it.part, it.item, tuple(it.fields)


def _from(it, wheres: Iterable[str]) -> bool:
    """Was ``it`` read on one of the sheets ``wheres`` ('file › sheet'),
    itself or a cell its note says the same value is also at."""
    ws = set(wheres)
    return any(_sheet_where(w) in ws for w in [it.where] + list(it.also))


def _copies(review) -> List[Dict[str, Any]]:
    return [c for c in review.filled_copies if c.get("where")]


def copy_qid(where: str) -> str:
    f, _, sheet = str(where).partition(" › ")
    return f"copy|{f}|{sheet}"


def open_questions(review, answers: Optional[Dict[str, Dict[str, Any]]] = None,
                   clash_max: int = CLASH_MAX) -> List[Question]:
    """The questions ``review`` raises that ``answers`` has not answered,
    filled copies first, then parts, label checks and clashes. A sample
    Ledger's review only ([] for a table Ledger's)."""
    if getattr(review, "kind", "") != "samples":
        return []
    answers = answers or {}
    vals = _values(review)
    out: List[Question] = []
    trusted = _trusted_wheres(review, answers)
    for c in _copies(review):
        q = _copy_question(c, vals)
        if q is not None and q.id not in answers:
            out.append(q)
    # a filled copy's file: its part is the copy question's, unless the
    # answer was to read it like any other file
    copy_files = {_file(c["where"]) for c in _copies(review)
                  if (answers.get(copy_qid(c["where"])) or {}).get("choice") != COPY_SOURCE}
    out += [q for q in _part_questions(vals, copy_files) if q.id not in answers]
    out += [q for q in _match_questions(vals, trusted) if q.id not in answers]
    clashes = [q for q in _clash_questions(vals) if q.id not in answers]
    out += clashes[:max(0, int(clash_max))]
    return out


def _copy_question(c: Dict[str, Any], vals) -> Optional[Question]:
    where = str(c["where"])
    f, _, sheet = where.partition(" › ")
    mine = [i for i in vals if _from(i, [where])]
    parts = [p for p in c.get("parts") or [] if p]
    part = (f" for part {parts[0]}" if len(parts) == 1 else
            f" for parts {', '.join(parts[:4])}" + (" …" if len(parts) > 4 else "")
            if parts else "")
    text = (f"The sheet '{sheet}' in {f} is laid out like the template{part}, with "
            f"{len(mine)} value(s) filled in. Is it a filled copy of the template?")
    return Question(copy_qid(where), "copy", text, [
        Choice(COPY_TRUST, "Yes - trust its values"),
        Choice(COPY_GUIDE, "Yes, but only as a guide"),
        Choice(COPY_SOURCE, "No - read it like any other file"),
        Choice(COPY_OUT, "Leave it out")], "",
        {"where": where, "file": f, "sheet": sheet, "parts": parts, "values": len(mine)})


def _part_questions(vals, copies: Sequence[str]) -> List[Question]:
    """``copies``: the files a copy question is about - asked that first."""
    reasons = _part_reasons()
    by: Dict[Tuple[str, str], List[Any]] = defaultdict(list)
    for i in vals:
        if not i.part or i.accept in ("Y", "N") or _file(i.where) in copies:
            continue
        if any(w in reasons for w in _held(i)):
            by[(_file(i.where), i.part)].append(i)
    out = []
    for (f, part), items in by.items():
        how = next((i.part_from for i in items if i.part_from), "")
        labels = list(dict.fromkeys(i.label for i in items if i.label))[:3]
        text = (f"{f}: I took its part to be {part}"
                + (f" ({how})" if how else "")
                + f". {len(items)} value(s) wait on it"
                + (f", e.g. {', '.join(labels)}" if labels else "")
                + f". Is {f} part {part}?")
        out.append(Question(f"part|{f}|{part}", "part", text, [
            Choice(PART_YES, f"Yes, it is {part}"),
            Choice(PART_OUT, "It is no part's file - leave it out")], "part",
            {"file": f, "part": part, "values": len(items)}))
    return out


def _clash_questions(vals) -> List[Question]:
    groups: Dict[Tuple[str, str, Tuple[str, ...]], List[Any]] = defaultdict(list)
    for i in vals:
        if i.part:
            groups[_cell(i)].append(i)
    out = []
    for (part, item, fields), items in groups.items():
        if not any(i.clash for i in items) or any(i.accept == "Y" for i in items):
            continue
        live = [i for i in items if i.accept != "N"]
        by_value: Dict[str, List[Any]] = {}
        for i in live:
            by_value.setdefault(value_key(i.value), []).append(i)
        if len(by_value) < 2:
            continue
        choices, shown = [], []
        for k, its in list(by_value.items())[:CLASH_CHOICES]:
            files = list(dict.fromkeys(_file(w) for i in its for w in [i.where] + i.also))
            choices.append(Choice(f"v:{k}", f"{its[0].value}  ({', '.join(files[:3])}"
                                  + (" …" if len(files) > 3 else "") + ")"))
            shown.append(its[0].value)
        choices.append(Choice(CLASH_NONE, "None of these"))
        name = " ".join(x for x in (part, item) if x)
        text = (f"{name} · {fields[0]}: the files disagree - "
                + " vs ".join(str(v) for v in shown) + ". Which is right?")
        out.append(Question(f"clash|{part}|{item}|{fields[0]}", "clash", text, choices,
                            "value", {"part": part, "item": item, "field": fields[0]}))
    return out


# ── checking matches against a trusted filled copy ──────────────────────
def _trusted_wheres(review, answers) -> List[str]:
    return [c["where"] for c in _copies(review)
            if (answers.get(copy_qid(c["where"])) or {}).get("choice") == COPY_TRUST]


@dataclass
class LabelCheck:
    label: str
    field: str
    agree: int = 0                # values that say something, agreeing
    differ: List[Tuple[str, str, str, str]] = field(default_factory=list)
    #: (part, its value here, the copy's, where)

    @property
    def confirmed(self) -> bool:
        return self.agree >= CONFIRM_MIN and not self.differ


def label_checks(vals, trusted: Sequence[str]) -> Dict[Tuple[str, str], LabelCheck]:
    """Every label (with its field) read outside the trusted copies, compared
    with the copies' values for the same part, item and field."""
    if not trusted:
        return {}
    truth: Dict[Tuple[str, str, Tuple[str, ...]], Any] = {}
    for i in vals:
        if _from(i, trusted) and i.accept != "N":
            truth.setdefault(_cell(i), i)
    checks: Dict[Tuple[str, str], LabelCheck] = {}
    for i in vals:
        if not i.label or _sheet_where(i.where) in trusted:
            continue
        if _norm(i.label) == _norm(i.fields[0]):
            # the field's own name: the match is not in doubt, the value is
            continue
        t = truth.get(_cell(i))
        if t is None or t is i:
            continue
        key = (_norm(i.label), i.fields[0])
        c = checks.setdefault(key, LabelCheck(i.label, i.fields[0]))
        if value_key(i.value) == value_key(t.value):
            c.agree += distinctive(i.value)
        else:
            c.differ.append((" ".join(x for x in (i.part, i.item) if x), i.value, t.value,
                             _file(i.where)))
    return checks


def _norm(label: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(label).lower()).split())


def match_qid(label: str, fname: str) -> str:
    return f"match|{_norm(label)}|{fname}"


def _match_questions(vals, trusted: Sequence[str]) -> List[Question]:
    out = []
    for (_lab, fname), c in label_checks(vals, trusted).items():
        if not c.differ:
            continue
        part, here, there, f = c.differ[0]
        n = len(c.differ)
        text = (f"I matched the label '{c.label}' to {fname}. Your filled copy disagrees "
                f"for {part}: {here} in {f}, {there} in the copy"
                + (f", and {n - 1} more" if n > 1 else "")
                + (f" ({c.agree} agree)" if c.agree else "")
                + f". Is '{c.label}' {fname}?")
        out.append(Question(match_qid(c.label, fname), "match", text, [
            Choice(MATCH_YES, f"Yes - keep it (the copy wins where they differ)"),
            Choice(MATCH_NO, f"No - '{c.label}' is not {fname}")], "",
            {"label": c.label, "field": fname, "differ": n, "agree": c.agree}))
    return out


# ── answers: kept per Ledger ────────────────────────────────────────────
def answers_path(csv_path) -> Path:
    return Path(csv_path).with_suffix(".answers.json")


def load_answers(csv_path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(answers_path(csv_path).read_text(encoding="utf-8")) or {}
    except Exception:                                       # noqa: BLE001
        return {}
    got = data.get("answers") if isinstance(data, dict) else None
    return {str(k): v for k, v in (got or {}).items() if isinstance(v, dict)}


def _write(csv_path, answers: Dict[str, Dict[str, Any]]) -> None:
    p = answers_path(csv_path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"answers": answers}, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def save_answer(csv_path, qid: str, choice: str, value: str = "",
                question: Optional[Question] = None) -> Dict[str, Dict[str, Any]]:
    """Keep the answer to ``qid`` (the question's own words with it, for the
    list of answers); returns every answer."""
    answers = load_answers(csv_path)
    a: Dict[str, Any] = {"choice": choice, "at": datetime.now().isoformat(timespec="seconds")}
    if value:
        a["value"] = str(value)
    if question is not None:
        a["question"] = question.text
        a["said"] = next((c.label for c in question.choices if c.key == choice),
                         value or choice)
    answers[qid] = a
    _write(csv_path, answers)
    return answers


def forget_answer(csv_path, qid: str) -> Dict[str, Dict[str, Any]]:
    answers = load_answers(csv_path)
    if answers.pop(qid, None) is not None:
        _write(csv_path, answers)
    return answers


# ── answers -> the review ───────────────────────────────────────────────
@dataclass
class Outcome:
    accepted: int = 0
    set_aside: int = 0
    parts_changed: int = 0
    #: values read elsewhere that agree with a trusted filled copy
    confirmed: int = 0
    #: labels whose values agree with it
    labels_confirmed: List[str] = field(default_factory=list)
    rows: int = 0                  # review rows changed

    def describe(self) -> str:
        bits = []
        if self.confirmed:
            bits.append(f"{self.confirmed} value(s) agree with your filled copy")
        if self.labels_confirmed:
            bits.append(f"{len(self.labels_confirmed)} label match(es) confirmed by it ("
                        + ", ".join(self.labels_confirmed[:4])
                        + (" …" if len(self.labels_confirmed) > 4 else "") + ")")
        if self.accepted:
            bits.append(f"{self.accepted} accepted")
        if self.set_aside:
            bits.append(f"{self.set_aside} set aside")
        if self.parts_changed:
            bits.append(f"{self.parts_changed} given another part")
        return "; ".join(bits) if bits else "nothing in the review changed"


def plan(review, answers: Dict[str, Dict[str, Any]]
         ) -> Tuple[Dict[Tuple[str, int], Dict[str, Any]], Outcome]:
    """What ``answers`` change in ``review``: {(sheet, row): ledger_review
    .save's answer} and what that comes to. Only rows whose Accept, Value or
    Part change are in it. An answer a later review no longer fits (its
    value gone) changes nothing - its question is asked again."""
    edits: Dict[Tuple[str, int], Dict[str, Any]] = {}
    out = Outcome()
    if getattr(review, "kind", "") != "samples" or not answers:
        return edits, out
    vals = _values(review)
    state = {(i.sheet, i.row): i.accept for i in vals}

    def put(it, accept: str, **more) -> None:
        a = edits.setdefault((it.sheet, it.row), {})
        a.update(more)
        a["accept"] = accept
        state[(it.sheet, it.row)] = accept

    def choose(winners, losers) -> None:
        for i in winners:
            put(i, "Y")
        for i in losers:
            put(i, "N")

    # 1. filled copies
    copies = {c["where"]: c for c in _copies(review)}
    trusted = []
    for where in copies:
        a = answers.get(copy_qid(where)) or {}
        mine = [i for i in vals if _from(i, [where])]
        if a.get("choice") == COPY_TRUST:
            trusted.append(where)
            truth = {_cell(i): value_key(i.value) for i in mine}
            for i in vals:
                k = truth.get(_cell(i))
                if k is None:
                    continue
                if value_key(i.value) == k:
                    # each other file's reading of it agrees: the row's own
                    # and those its note says are 'also at' the same value
                    out.confirmed += sum(1 for w in [i.where] + i.also
                                         if _sheet_where(w) != where)
                    put(i, "Y")
                elif not _from(i, [where]):
                    put(i, "N")
        elif a.get("choice") in (COPY_GUIDE, COPY_OUT):
            # its values not used: the rows only it holds are set aside (one
            # also read in another file stays that file's)
            for i in mine:
                if all(_sheet_where(w) == where for w in [i.where] + i.also):
                    put(i, "N")
    # 2. parts
    reasons = _part_reasons()
    for qid, a in answers.items():
        if not qid.startswith("part|"):
            continue
        _k, f, part = qid.split("|", 2)
        mine = [i for i in vals if _file(i.where) == f and i.part == part]
        choice, new = a.get("choice"), str(a.get("value") or "").strip()
        if choice == PART_OUT:
            for i in mine:
                put(i, "N")
            continue
        if choice == PART_OTHER and new and new != part:
            taken = {(_cell(o)[1:], value_key(o.value)) for o in vals if o.part == new}
            rivals = {_cell(o)[1:] for o in vals if o.part == new}
            for i in mine:
                key = (_cell(i)[1:], value_key(i.value))
                only_part = _held(i) and all(w in reasons for w in _held(i))
                ok = only_part and not i.clash and (key in taken or key[0] not in rivals)
                more = {"part": new}
                if ok and state.get((i.sheet, i.row)) not in ("Y", "N"):
                    put(i, "Y", **more)
                else:
                    edits.setdefault((i.sheet, i.row), {}).update(more)
                out.parts_changed += 1
            continue
        if choice == PART_YES:
            for i in mine:
                if (_held(i) and all(w in reasons for w in _held(i)) and not i.clash
                        and state.get((i.sheet, i.row)) not in ("Y", "N")):
                    put(i, "Y")
    # 3. label checks against the trusted copies
    if trusted:
        checks = label_checks(vals, trusted)
        for (lab, fname), c in checks.items():
            said = (answers.get(match_qid(c.label, fname)) or {}).get("choice")
            if said == MATCH_NO:
                for i in vals:
                    if i.label and _norm(i.label) == lab and i.fields[0] == fname \
                            and not _from(i, trusted):
                        put(i, "N")
                continue
            if not (c.confirmed or said == MATCH_YES):
                continue
            out.labels_confirmed.append(f"'{c.label}' is {fname}")
            for i in vals:
                if not (i.label and _norm(i.label) == lab and i.fields[0] == fname):
                    continue
                why = _held(i)
                if (why and all(w in LABEL_REASONS or w.startswith("label matched by")
                                for w in why)
                        and not i.clash and state.get((i.sheet, i.row)) not in ("Y", "N")):
                    put(i, "Y")
    # 4. clashes
    groups: Dict[Tuple[str, str, Tuple[str, ...]], List[Any]] = defaultdict(list)
    for i in vals:
        groups[_cell(i)].append(i)
    for qid, a in answers.items():
        if not qid.startswith("clash|"):
            continue
        _k, part, item, fname = qid.split("|", 3)
        items = groups.get((part, item, (fname,)), [])
        if not items:
            continue
        choice = str(a.get("choice") or "")
        if choice == CLASH_NONE:
            choose([], items)
        elif choice.startswith("v:"):
            win = [i for i in items if value_key(i.value) == choice[2:]]
            if win:
                choose(win, [i for i in items if i not in win])
        elif choice == CLASH_TYPED and str(a.get("value") or "").strip():
            typed = str(a["value"]).strip()
            win = [i for i in items if value_key(i.value) == value_key(typed)]
            if not win:
                first = items[0]
                edits.setdefault((first.sheet, first.row), {}).update(
                    value=typed, value_col=first.value_col)
                win = [first]
            choose(win, [i for i in items if i not in win])
    # what changed
    before = {(i.sheet, i.row): i for i in vals}
    keep = {}
    for k, a in edits.items():
        it = before[k]
        if a.get("accept") == it.accept and "value" not in a and not a.get("part"):
            continue
        keep[k] = a
        if a.get("accept") != it.accept:
            out.accepted += a.get("accept") == "Y"
            out.set_aside += a.get("accept") == "N"
    out.rows = len(keep)
    return keep, out


def _saved(review, edits) -> Any:
    """``edits`` written into the review workbook and into ``review`` as it is
    held (reading 16,000 values back took 3 s)."""
    from council_core import ledger_review
    if not edits:
        return review
    ledger_review.save(review.path, edits)
    at = {(i.sheet, i.row): i for i in review.items}
    for k, a in edits.items():
        it = at.get(k)
        if it is None:
            continue
        if "accept" in a:
            it.accept = a["accept"] or ""
        if "value" in a:
            it.value = str(a["value"])
        if a.get("part"):
            it.part = str(a["part"])
    return review


def apply(review_path, answers: Dict[str, Dict[str, Any]]) -> Tuple[Any, Outcome]:
    """``answers`` written into the review workbook; returns the review as it
    is now and what changed."""
    from council_core import ledger_review
    review = ledger_review.load(review_path)
    edits, out = plan(review, answers)
    return _saved(review, edits), out


def apply_saved(review, csv_path) -> Tuple[Any, Outcome]:
    """The Ledger's kept answers applied to a review just written (Extract,
    Update, Re-run): ``review`` loaded already; returns it as it is now."""
    edits, out = plan(review, load_answers(csv_path))
    return _saved(review, edits), out


@dataclass
class Round:
    outcome: Outcome
    questions: List[Question]
    filled: int = 0               # filled templates written again
    problems: List[str] = field(default_factory=list)


def recheck(review_path, csv_path, say: Optional[Callable[[str], None]] = None,
            write_filled: bool = True) -> Round:
    """One round of the loop: every kept answer applied to the review, the
    matches checked against the trusted filled copies, the filled templates
    written again from it, and the questions still open (new ones among
    them). Reads no source workbook."""
    from council_core import ledger_review
    answers = load_answers(csv_path)
    review, out = apply(review_path, answers)
    r = Round(out, open_questions(review, answers))
    if write_filled and out.rows:
        try:
            r.filled = len(ledger_review.write_all_filled(review, say))
            ledger_review.write_full_filled(review, say)
        except Exception as e:                              # noqa: BLE001
            r.problems.append(f"the filled templates were not written again: {e}")
    return r


# ── a typed answer ──────────────────────────────────────────────────────
_YES = re.compile(r"^\s*(y|yes|yeah|yep|yup|correct|right|true|sure|ok(ay)?|it is|that'?s right)"
                  r"\b", re.I)
_NO = re.compile(r"^\s*(n|no|nope|wrong|false|it'?s not|it is not|not)\b", re.I)
_OUT = re.compile(r"\b(leave (it|this|that)? ?out|skip|ignore|exclude|don'?t use|do not use)\b",
                  re.I)
_GUIDE = re.compile(r"\b(guide|example|reference|only to learn|just to learn)\b", re.I)
_NONE = re.compile(r"\b(none|neither|nothing)\b", re.I)
_PARTISH = re.compile(r"\b(?=[A-Za-z0-9_.-]*\d)[A-Za-z0-9][A-Za-z0-9_.-]{1,40}\b")


def interpret(q: Question, text: str, chat: Optional[Callable] = None
              ) -> Optional[Tuple[str, str]]:
    """What a typed reply to ``q`` says, as (choice key, value), or None
    when it cannot be told. Plain words first; Council's model (``chat``,
    ledger_llm's (prompt, json schema) -> (text, name)) only when they do
    not tell. The caller shows it back to be confirmed - nothing is done
    on a guess."""
    t = " ".join(str(text or "").split())
    if not t:
        return None
    got = _by_rules(q, t)
    if got is None and chat is not None:
        got = _by_model(q, t, chat)
    return got


def _by_rules(q: Question, t: str) -> Optional[Tuple[str, str]]:
    keys = {c.key for c in q.choices}
    for c in q.choices:                     # the choice's own words, or its number
        if t.casefold() == c.label.casefold() or t == str(q.choices.index(c) + 1):
            return c.key, ""
    if q.kind == "copy":
        if _OUT.search(t):
            return COPY_OUT, ""
        if _GUIDE.search(t):
            return COPY_GUIDE, ""
        if _NO.search(t) or re.search(r"\bnormal|any other|regular\b", t, re.I):
            return COPY_SOURCE, ""
        if _YES.search(t) or re.search(r"\btrust|filled copy|use (its|the) values\b", t, re.I):
            return COPY_TRUST, ""
        return None
    if q.kind == "part":
        if _OUT.search(t) or re.search(r"\bno part\b", t, re.I):
            return PART_OUT, ""
        guess = str(q.facts.get("part") or "")
        cands = [m.group(0) for m in _PARTISH.finditer(t)]
        other = [c for c in cands if c.casefold() != guess.casefold()]
        if other:
            return PART_OTHER, other[-1]
        if _YES.search(t) or any(c.casefold() == guess.casefold() for c in cands):
            return PART_YES, ""
        return None
    if q.kind == "match":
        if _NO.search(t):
            return MATCH_NO, ""
        if _YES.search(t):
            return MATCH_YES, ""
        return None
    if q.kind == "clash":
        if _NONE.search(t) and not re.search(r"\d", t):
            return CLASH_NONE, ""
        for c in q.choices:
            if c.key.startswith("v:"):
                shown = c.label.rsplit("  (", 1)[0]
                if value_key(t) == c.key[2:] or value_key(t) == value_key(shown):
                    return c.key, ""
        nums = re.findall(r"[-+]?\d[\d,]*\.?\d*", t)
        for n in nums:
            k = value_key(n)
            if f"v:{k}" in keys:
                return f"v:{k}", ""
        if len(t) <= 60 and (nums or not _YES.search(t)):
            return CLASH_TYPED, (nums[0] if len(nums) == 1 and len(t) > len(nums[0]) + 8
                                 else t)
        return None
    return None


def _by_model(q: Question, t: str, chat: Callable) -> Optional[Tuple[str, str]]:
    keys = [c.key for c in q.choices] + (
        [PART_OTHER] if q.free == "part" else [CLASH_TYPED] if q.free == "value" else [])
    lines = [f"- {c.key}: {c.label}" for c in q.choices]
    if q.free == "part":
        lines.append(f"- {PART_OTHER}: another part number (put it in value)")
    if q.free == "value":
        lines.append(f"- {CLASH_TYPED}: another value (put it in value)")
    prompt = ("A data-entry tool asked the user a question and the user typed a reply. "
              "Say which answer the reply means. Use only the keys listed; if the reply "
              "means none of them, answer the key \"unclear\".\n\n"
              f"Question: {q.text}\nAnswers:\n" + "\n".join(lines)
              + f"\n\nReply: {t}\n\nAnswer as JSON: {{\"key\": ..., \"value\": ...}}")
    schema = {"type": "object", "properties": {
        "key": {"type": "string", "enum": keys + ["unclear"]},
        "value": {"type": "string"}}, "required": ["key"]}
    try:
        text, _name = chat(prompt, schema)
        m = re.search(r"\{.*\}", text or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception:                                       # noqa: BLE001
        return None
    key = str(data.get("key") or "")
    if key not in keys:
        return None
    value = str(data.get("value") or "").strip()
    if key in (PART_OTHER, CLASH_TYPED) and not value:
        return None
    return key, value if key in (PART_OTHER, CLASH_TYPED) else ""


def said(q: Question, choice: str, value: str = "") -> str:
    """An answer in words, as the chat shows it back."""
    if choice == PART_OTHER:
        return f"It is part {value}"
    if choice == CLASH_TYPED:
        return f"The right value is {value}"
    return next((c.label for c in q.choices if c.key == choice), choice)
