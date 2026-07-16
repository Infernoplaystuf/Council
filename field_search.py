"""
field_search.py — find files where a labeled FIELD has a given VALUE, and read a
field's value out of one file. Deterministic, offline, field-AWARE (so a file
where 'Point of Contact' is 'Bob' matches, not just any file mentioning Bob).

Powers:
  * "find all files with Bob listed as the point of contact"  (value + field)
  * "find files with the same point of contact as report.csv" (extract the
    field's value from that file, then search)

Matching:
  * Tabular (CSV/TSV/Excel/Parquet): a COLUMN whose name matches the field
    contains the value.
  * Text (.txt/.md/.json/.log/...): the field label and the value co-occur on
    the same line or within the next couple of lines (a 'section').
  * PDF/DOCX: same text logic over extracted text (optional; via vault_rag).

All reads are bounded and read-only. Optional deps degrade gracefully.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

_TABULAR = {".csv", ".tsv", ".xlsx", ".xls", ".parquet"}
_TEXTUAL = {".txt", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl",
            ".ndjson", ".yaml", ".yml", ".html", ".htm", ".xml", ".ini",
            ".cfg", ".csv", ".tsv"}
_EXTRACTABLE = {".pdf", ".docx"}

# A field label is followed by its value after ':' / '=' / a SPACED dash. The
# dash must be spaced so hyphenated values ('Jean-Luc') are not split.
_SEP_RE = re.compile(r"[:=]|(?<=\s)[-–—](?=\s)")
_BULLET_RE = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s+")
_EMPH_RE = re.compile(r"[*`]+")          # markdown emphasis; NOT '_' (_norm eats it)
# camelCase / PascalCase word boundaries: pointOfContact -> point Of Contact,
# POCName -> POC Name.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# One field may list several values: 'Bob, Alice', 'Bob and Alice', 'Bob; Alice'.
_VAL_SPLIT_RE = re.compile(r"\s*(?:[,;/]|\band\b|&)\s*", re.I)
# A value ends where the next field starts. Only ':' / '=' — a spaced dash
# would eat 'Bob Smith - Engineering'.
_NEW_FIELD_RE = re.compile(r"[:=]")
# Several fields can share a line: 'Point of Contact: Bob; Reviewer: Alice'.
_SEG_SPLIT_RE = re.compile(r"\s*;\s*")
# A markdown table separator row: |---|:--:|
_RULE_RE = re.compile(r"^[\s:\-–—|]+$")
# "key": "value"  /  "key": 12.5  — for JSON that won't parse (truncated by the
# read cap, or embedded in prose).
_JSON_PAIR_RE = re.compile(
    r'"([^"\n]{1,80})"\s*:\s*(?:"((?:[^"\\]|\\.)*)"'
    r'|([-+]?\d[\d.eE+\-]*|true|false|null))')


def _norm(s) -> str:
    """Lower-case + collapse non-alphanumerics to single spaces (so 'Point of
    Contact', 'point_of_contact' and 'POINT-OF-CONTACT' compare equal)."""
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _norm_key(s) -> str:
    """_norm for a FIELD NAME, splitting camelCase first.

    JSON keys are routinely 'pointOfContact'. _norm lower-cases before it
    splits, so it would see one token 'pointofcontact' and never match the
    field 'point of contact'."""
    return _norm(_CAMEL_RE.sub(" ", str(s or "")))


def _label_is(text, fn: str) -> bool:
    """True when ``text`` reads as the LABEL for field ``fn`` — not a sentence
    that merely mentions it. ``fn`` must appear as a contiguous run of whole
    tokens, and the text must be label-short (a heading/key, not prose). This
    is what stops 'Bob met the point of contact yesterday' from being treated
    as a 'Point of Contact' field."""
    t = _norm_key(_EMPH_RE.sub("", _BULLET_RE.sub("", str(text or ""))))
    if not t or not fn:
        return False
    tt, ft = t.split(), fn.split()
    if len(tt) > len(ft) + 3:
        return False
    return any(tt[i:i + len(ft)] == ft for i in range(len(tt) - len(ft) + 1))


def _split_values(v: str):
    """One field's value text -> the individual values it lists.

    Stops at the point another field begins: 'Bob; Reviewer: Alice' is Bob, not
    Bob AND 'Reviewer: Alice'. Only a colon/equals ends a value — a spaced dash
    does not, or 'Bob Smith - Engineering' would be thrown away."""
    out = []
    for part in _VAL_SPLIT_RE.split(str(v or "")):
        part = _EMPH_RE.sub("", part).strip().strip(".").strip()
        if not part:
            continue
        if _NEW_FIELD_RE.search(part):
            break
        out.append(part)
    return out


def _kv_pairs(line: str):
    """Every 'key: value' pair on ONE line, not just the first.

    Records are often written one-per-line ('Point of Contact: Bob; Reviewer:
    Alice'), so splitting a line at its first separator both misses the later
    fields and lets the first field's value swallow them."""
    s = _EMPH_RE.sub("", _BULLET_RE.sub("", line or "")).strip()
    if not s:
        return []
    pairs = []
    for seg in _SEG_SPLIT_RE.split(s):
        seg = seg.strip()
        if not seg:
            continue
        m = _SEP_RE.search(seg)
        if m:
            pairs.append((seg[:m.start()].strip(), seg[m.end():].strip()))
        elif pairs:
            # No separator: a continuation of the previous field's value
            # ('Contact: Bob; Alice' lists two contacts).
            k, v = pairs[-1]
            pairs[-1] = (k, f"{v}; {seg}")
    return pairs


def _scalars(o):
    """Every scalar leaf of a JSON value, so a field whose value is an object
    ({'name': 'Bob', 'email': ...}) still yields something matchable."""
    if isinstance(o, dict):
        for v in o.values():
            yield from _scalars(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _scalars(v)
    elif o is not None and not isinstance(o, bool):
        yield str(o)


def _json_field_values(text: str, fn: str):
    """Values for field ``fn`` read STRUCTURALLY out of JSON, or None if the
    text isn't JSON.

    JSON records are routinely minified onto a single line, which defeats every
    line-oriented rule: the whole record is one 'line', so proximity matching
    says a field's value is anything else in the record, and first-separator
    splitting sees a key of '{"job"'. Parsing gives the exact key->value
    mapping, so 'point_of_contact' is Bob no matter what else the record says
    about Alice."""
    t = (text or "").strip()
    if not t or t[0] not in "[{":
        return None
    vals: List[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if _label_is(k, fn):
                    vals.extend(_scalars(v))
                else:
                    walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    try:
        walk(json.loads(t))
        return vals
    except Exception:
        pass
    # JSON Lines, or a truncated/oversized read: try per-line objects.
    got_any = False
    for line in t.splitlines():
        line = line.strip().rstrip(",")
        if not line or line[0] not in "[{":
            continue
        try:
            walk(json.loads(line))
            got_any = True
        except Exception:
            continue
    if got_any:
        return vals
    # Still not parseable (truncated by the read cap, or embedded in prose).
    # Fall back to reading "key": value pairs textually — still structural
    # (a key maps to ITS value), never proximity.
    for m in _JSON_PAIR_RE.finditer(t):
        key = m.group(1)
        if _label_is(key, fn):
            val = m.group(2) if m.group(2) is not None else m.group(3)
            if val:
                vals.append(val)
    return vals or None


# A token shorter than this must match EXACTLY. Measured against real names:
# every pair of DIFFERENT people that is one edit apart is short —
# bob/rob, tim/tom, dan/don, jon/jan, kim/tim, sam/pam, ron/don, ann/anna —
# while every genuine typo of a name is longer: smith/smtih, johnson/johsnon,
# anderson/andersen, mueller/muller, patel/patell. Length is what separates
# "a typo" from "a different person", so fuzzy matching stops below it.
_FUZZY_MIN_LEN = 5


def _edit1(a: str, b: str) -> bool:
    """True if one insert/delete/substitute turns ``a`` into ``b``."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            diff += 1
            if diff > 1:
                return False
            if len(a) == len(b):
                i += 1
            j += 1
        else:
            i += 1
            j += 1
    return True


def _transposed(a: str, b: str) -> bool:
    """True if ``a`` and ``b`` differ only by one ADJACENT swap.

    Separate from _edit1 because a transposition is edit-distance TWO —
    'smtih' vs 'smith' is the most common typo there is and a plain
    edit-distance-1 rule misses it entirely."""
    if len(a) != len(b):
        return False
    d = [k for k in range(len(a)) if a[k] != b[k]]
    return (len(d) == 2 and d[1] == d[0] + 1
            and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])


def _is_typo_of(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` are the same word with one typo in it."""
    if a == b:
        return True
    if len(a) < _FUZZY_MIN_LEN or len(b) < _FUZZY_MIN_LEN:
        return False           # too short — a single edit is a different name
    return _edit1(a, b) or _transposed(a, b)


def _match_detail(field_value, query, *, fuzzy: bool = True):
    """(matched, note) for ``query`` against one field value.

    Token-aware, NOT substring: every token of the query must appear as a WHOLE
    token of the field value. 'Bob' matches 'Bob Smith', 'Smith, Bob' and
    'bob.smith@x.com', but NOT 'Bobby'; 'Bob Smith' needs both tokens.

    With ``fuzzy`` (default), a token may also match through ONE typo — but
    only when both tokens are long enough that a typo is the likelier
    explanation than a different name (see _FUZZY_MIN_LEN). ``note`` names the
    typo when one was used, so a fuzzy hit is never presented as an exact one.
    """
    q = _norm(query).split()
    if not q:
        return False, ""
    v = _norm(field_value).split()
    vs = set(v)
    notes = []
    for t in q:
        if t in vs:
            continue
        near = next((w for w in v if _is_typo_of(t, w)), None) if fuzzy else None
        if near is None:
            return False, ""
        notes.append(f"{near!r}≈{t!r}")
    return True, ("spelling: " + ", ".join(notes) if notes else "")


def _value_matches(field_value, query, *, fuzzy: bool = True) -> bool:
    """True when ``query`` names the same value as ``field_value``."""
    return _match_detail(field_value, query, fuzzy=fuzzy)[0]


def _field_values_in_text(text: str, fn: str, *, max_hits: int = 100):
    """Every value ASSIGNED to field ``fn`` in ``text``.

    Only the value side of the field is returned — never nearby text — which is
    what makes a search field-aware. Handles 'Field: value', 'Field = value',
    'Field - value', '**Field:** value', '- Field: value', several fields on
    one line, a 'Field' heading with the value on the next line, and markdown
    '| Field | value |'. JSON is read structurally, not as lines."""
    # JSON FIRST. A minified record puts the whole object on one line, so every
    # line rule below breaks on it: proximity would call anything in the record
    # the field's value, and splitting at the first ':' yields a key of '{"job"'.
    js = _json_field_values(text, fn)
    if js is not None:
        return js[:max_hits]

    vals = []
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        if len(vals) >= max_hits:
            break
        line = raw.strip()
        if not line:
            continue
        # markdown table row: | Field | value |
        if line.startswith("|") and line.count("|") >= 2:
            cells = [c.strip() for c in line.strip("|").split("|")]
            for j, cell in enumerate(cells[:-1]):
                if _label_is(cell, fn):
                    nxt = cells[j + 1].strip()
                    if nxt and not _RULE_RE.match(nxt):
                        vals.extend(_split_values(nxt))
            continue
        pairs = _kv_pairs(line)
        if pairs:
            for key, val in pairs:
                if val and _label_is(key, fn):
                    vals.extend(_split_values(val))
            continue
        # heading style: the line IS the label -> value on the next non-empty
        # line, unless that line starts a different field.
        if _label_is(line, fn):
            for j in range(i + 1, min(i + 4, len(lines))):
                nxt = lines[j].strip()
                if not nxt:
                    continue
                if nxt.startswith("|") or _kv_pairs(nxt):
                    break        # the next field began; this heading has no value
                vals.extend(_split_values(nxt))
                break
    return vals


def _read_text(p: Path, max_chars: int = 400000) -> str:
    suf = p.suffix.lower()
    if suf in _EXTRACTABLE:
        try:
            import vault_rag
            return (vault_rag._extract_text(p) or "")[:max_chars]
        except Exception:
            return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def extract_field_value(path: Any, field: str,
                        *, max_values: int = 10) -> Optional[List[str]]:
    """Return the value(s) of the labeled ``field`` in one file, or None.

    Text: 'Field: value' on a line, or a 'Field' heading followed by the value
    on the next non-empty line. Tabular: the distinct values of a column whose
    name matches ``field``."""
    p = Path(path)
    suf = p.suffix.lower()
    fn = _norm(field)
    if not fn:
        return None
    if suf in _TABULAR:
        try:
            import vault_analyst as va
            df = va.read_table(p)
            col = va.match_column_name(df.columns, field)
            if col is None:
                return None
            vals: List[str] = []
            for v in df[col].dropna().astype(str):
                v = v.strip()
                if v and v not in vals:
                    vals.append(v)
                if len(vals) >= max_values:
                    break
            return vals or None
        except Exception:
            return None
    text = _read_text(p)
    if not text:
        return None
    vals: List[str] = []
    for v in _field_values_in_text(text, fn):
        if v not in vals:
            vals.append(v)
        if len(vals) >= max_values:
            break
    return vals or None


def _table_columns(p: Path) -> List[str]:
    """The column names of a tabular file WITHOUT reading all its rows — the
    cheap first pass so a CSV that lacks the field column is skipped without a
    full read (the key optimisation for a big vault)."""
    suf = p.suffix.lower()
    try:
        import pandas as pd
        if suf in (".csv", ".tsv"):
            sep = "\t" if suf == ".tsv" else ","
            return list(pd.read_csv(p, sep=sep, nrows=0,
                                    on_bad_lines="skip").columns)
        if suf in (".xlsx", ".xls"):
            return list(pd.read_excel(p, nrows=0).columns)
        if suf == ".parquet":
            import pyarrow.parquet as _pq  # type: ignore
            return list(_pq.ParquetFile(str(p)).schema.names)
    except Exception:
        pass
    try:
        import vault_analyst as va
        return list(va.read_table(p).columns)
    except Exception:
        return []


def _table_column_values(p: Path, col: str) -> List[str]:
    """Read ONLY the given column of a tabular file (not the whole frame)."""
    suf = p.suffix.lower()
    try:
        import pandas as pd
        if suf in (".csv", ".tsv"):
            sep = "\t" if suf == ".tsv" else ","
            s = pd.read_csv(p, sep=sep, usecols=[col],
                            on_bad_lines="skip")[col]
            return s.dropna().astype(str).tolist()
        if suf in (".xlsx", ".xls"):
            s = pd.read_excel(p, usecols=[col])[col]
            return s.dropna().astype(str).tolist()
    except Exception:
        pass
    try:
        import vault_analyst as va
        df = va.read_table(p)
        if col in df.columns:
            return df[col].dropna().astype(str).tolist()
    except Exception:
        pass
    return []


def find_files_with_field_value(root: Any, field: str, value: str, *,
                                limit: Optional[int] = None,
                                max_files: Optional[int] = None,
                                text_max_chars: int = 200000,
                                on_progress: Optional[Callable[[int, int],
                                                               None]] = None,
                                stats: Optional[dict] = None
                                ) -> List[Tuple[str, str]]:
    """EVERY file under ``root`` where ``field`` is associated with ``value``.
    Returns ``[(abs_path, context)]``. Field-aware (see module docstring).

    ALL means all. By default this scans every file and returns every match —
    there is no hit cap and no file cap. "Search all files for the point of
    contact" has to answer for the whole vault or the answer is worthless: a
    silently short list is indistinguishable from a complete one, so a missing
    file reads as "that person isn't on it".

    ``limit`` / ``max_files`` are opt-in and OFF by default. When either is
    set and actually fires, it is recorded in ``stats`` — nothing truncates
    quietly.

    ``stats`` (optional dict) is filled in with the coverage of the run:
        scanned        files actually examined
        total_files    files found under root
        hits           matches returned
        truncated      True if a cap cut the results short
        truncated_why  plain-English reason, or ""
    so the caller can state "searched all 8,412 files" rather than implying it.

    Scales: tabular files are checked HEADER-FIRST (only a matching column is
    read; a file without the column is skipped without a full read) and text
    reads are bounded. ``on_progress(scanned, total)`` fires ~every 100 files.
    """
    root = Path(root)
    fn = _norm(field)
    vn = str(value or "").strip().lower()
    if stats is not None:
        stats.update({"scanned": 0, "total_files": 0, "hits": 0,
                      "truncated": False, "truncated_why": ""})
    if not vn:
        return []
    try:
        import conversation_logger as _cl
    except Exception:
        _cl = None
    try:
        files = [p for p in sorted(root.rglob("*"))
                 if p.is_file() and not p.name.startswith(".")]
    except Exception:
        files = []
    total_found = len(files)
    if max_files is not None and total_found > max_files:
        files = files[:max_files]
        if stats is not None:
            stats["truncated"] = True
            stats["truncated_why"] = (
                f"only the first {max_files} of {total_found} files were "
                f"scanned (max_files)")
    total = len(files)
    out: List[Tuple[str, str]] = []
    try:
        import vault_analyst as va
        _match_col = va.match_column_name
    except Exception:
        _match_col = None
    for i, p in enumerate(files):
        if on_progress is not None and i % 100 == 0:
            try:
                on_progress(i, total)
            except Exception:
                pass
        if limit is not None and len(out) >= limit:
            if stats is not None:
                stats["truncated"] = True
                stats["truncated_why"] = (
                    f"stopped after {limit} matches (limit); there may be more")
            break
        if _cl is not None:
            try:
                if _cl.is_protected_path(p, root):
                    continue
            except Exception:
                pass
        suf = p.suffix.lower()
        try:
            if suf in _TABULAR:
                cols = _table_columns(p)
                col = (_match_col(cols, field)
                       if (cols and _match_col and fn) else None)
                if col is None:
                    continue                       # no such column — no full read
                vals = _table_column_values(p, col)
                hit = None
                for v in vals:
                    # Split the cell the way the TEXT path already did. One
                    # cell can list several people — "Bob Jones, Alice Smith" —
                    # and matching the cell whole means its tokens pool, so a
                    # query for "Bob Smith" matched a cell naming Bob Jones and
                    # Alice Smith: a person who is not on it, invented out of
                    # two who are. The text path split; this one did not, and
                    # the two drifted apart.
                    for one in (_split_values(v) or [v]):
                        ok, note = _match_detail(one, value)
                        if ok:
                            # Report the value ACTUALLY in the cell, and name
                            # the typo when one was needed — a near-match must
                            # never read as an exact one.
                            hit = (f"column '{col}' = {one!r}"
                                   + (f"  [{note}]" if note else ""))
                            break
                    if hit:
                        break
                if hit:
                    out.append((str(p), hit))
            elif suf in _TEXTUAL or suf in _EXTRACTABLE:
                if not fn:
                    continue
                text = _read_text(p, max_chars=text_max_chars)
                if not text:
                    continue
                for v in _field_values_in_text(text, fn):
                    ok, note = _match_detail(v, value)
                    if ok:
                        out.append((str(p),
                                    (f"{field}: {v}"[:140]
                                     + (f"  [{note}]" if note else ""))))
                        break
        except Exception:
            continue
    if on_progress is not None:
        try:
            on_progress(total, total)
        except Exception:
            pass
    if stats is not None:
        stats["scanned"] = total
        stats["total_files"] = total_found
        stats["hits"] = len(out)
    return out


def coverage_line(stats: dict) -> str:
    """One plain sentence describing how complete a search was.

    A negative result is only trustworthy if the search was complete, so the
    coverage is stated either way rather than left for the reader to assume.
    """
    if not stats:
        return ""
    scanned = stats.get("scanned", 0)
    total = stats.get("total_files", scanned)
    if stats.get("truncated"):
        return (f"⚠ PARTIAL — {stats.get('truncated_why') or 'results were '
                'truncated'}. Treat a missing file as unknown, not absent.")
    if total:
        return f"Searched all {total:,} file(s) in the vault."
    return "No files to search."
