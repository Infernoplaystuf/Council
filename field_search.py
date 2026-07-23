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

_TABULAR = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".parquet"}
# .xlsm is macro-enabled Excel and openpyxl reads it exactly like .xlsx. It was
# absent, so every macro workbook in a vault — which in a manufacturing shop is
# most of them (travelers, routers, inspection sheets) — was skipped.
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


# ============================================================
# Field-NAME drift matching (SUGGESTION, never silent substitution)
#
# A field's LABEL drifts across files — "Point of Contact" becomes
# "Router_Point_of_Contact" (prefix), "Point of Contact (Primary)" (suffix),
# "pointOfContact", "POC". Given the user's requested field and the vault's
# REAL harvested vocabulary, rank the labels that plausibly ARE the same field,
# each with a confidence and a one-phrase reason a non-technical user can read.
#
# This is deliberately built for SUGGESTION, not silent substitution. Three
# independent designs that tried to return the single right field silently were
# each broken by adversarial review on the same axis: to admit an arbitrary
# prefix like "Router_", any lexicon-free rule must accept any leading word,
# which lets a MEANINGFUL prefix ("Bill of Materials", "Contract Number") slip
# in as a confident wrong answer. There is no lexicon-free way to tell a
# meaningless prefix from a meaningful one — that distinction IS a lexicon, and
# a hardcoded word list is exactly the overfitting that failed before.
#
# So the human adjudicates. A wrong suggestion shown with its confidence and
# reason is harmless; a wrong silent substitution is the false positive that
# started this whole line of work. The scorer therefore favours RECALL (surface
# the plausible candidates) while the confidence + "why" carry the honesty.
#
# NO domain word list anywhere below. Every constant is a threshold, a weight,
# an English plural suffix (grammar, not vocabulary), or a regex over
# orthography. IDF is learned from the vault's own labels at call time.
_PLURAL_SUFFIXES = ("ies", "ses", "xes", "zes", "ches", "shes", "s")


def _singularize(tok: str) -> str:
    """Fold an English plural token to a singular STEM, algorithmically.

    Not a word list — suffix stripping. 'contacts'->'contact',
    'entries'->'entri' (stem; the same fold is applied to both sides so the
    stems still compare equal). Short tokens are left alone so 'is'/'as' are
    not mangled."""
    if len(tok) <= 3:
        return tok
    for suf in _PLURAL_SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 2:
            if suf == "ies":
                return tok[:-3] + "i"
            if suf in ("ses", "xes", "zes", "ches", "shes"):
                return tok[:-2]
            return tok[:-1]
    return tok


def _key_tokens(label: str) -> List[str]:
    """A field label -> its normalised word tokens (camelCase already split)."""
    return _norm_key(label).split()


def _contiguous_index(hay: List[str], needle: List[str]) -> int:
    """Index where ``needle`` occurs as a contiguous run in ``hay``, or -1."""
    if not needle or len(needle) > len(hay):
        return -1
    for i in range(len(hay) - len(needle) + 1):
        if hay[i:i + len(needle)] == needle:
            return i
    return -1


def _idf_map(available) -> Dict[str, float]:
    """Inverse document frequency of each token across the vault's labels.

    Learned from the data, not hardcoded: a token in many labels ('name',
    'id', 'number', 'date') is weak evidence; a rare one is strong. Used only
    to gate the loosest tier so a shared COMMON token cannot, by itself,
    suggest a match."""
    import math
    labels = [str(x) for x in (available or [])]
    n = len(labels) or 1
    df: Dict[str, int] = {}
    for lab in labels:
        for t in set(_key_tokens(lab)):
            df[t] = df.get(t, 0) + 1
    return {t: math.log((n + 1.0) / (c + 1.0)) + 1.0 for t, c in df.items()}


def _structural_affixes(available) -> set:
    """Tokens that RECUR as a prefix or suffix across the vault's labels.

    This is the lexicon-free discriminator for the hardest case: a single-word
    field ("material") sitting inside a longer label. Structure alone cannot
    tell drift ("Router_Material") from a different field ("Material Cost") —
    both add one word. But "Router_" recurs as the FIRST token of many labels
    (a schema generation stamped it on everything), while "Cost" is a one-off
    trailing word. So a first/last token seen on >=2 labels is treated as an
    affix (a low-information wrapper), and an extra token that is NOT is treated
    as substantive (it makes a different field). No word is named; the vault's
    own positional frequencies decide."""
    first: Dict[str, int] = {}
    last: Dict[str, int] = {}
    for raw in (available or []):
        toks = _key_tokens(raw)
        if len(toks) < 2:
            continue
        first[toks[0]] = first.get(toks[0], 0) + 1
        last[toks[-1]] = last.get(toks[-1], 0) + 1
    out = {t for t, c in first.items() if c >= 2}
    out |= {t for t, c in last.items() if c >= 2}
    return out


def _affix_phrase(extra_before: int, extra_after: int) -> str:
    if extra_before and extra_after:
        return "same words with text around them"
    if extra_before:
        return "same words with a prefix"
    return "same words with extra words after"


def _acronym_of(tokens: List[str]) -> str:
    return "".join(t[0] for t in tokens if t)


def field_name_candidates(requested, available, *,
                          min_confidence: float = 0.55,
                          limit: int = 12) -> List[Dict[str, Any]]:
    """Rank the vault's real field labels that plausibly ARE ``requested``.

    ``requested`` — the field the user typed. ``available`` — the vault's real
    harvested field vocabulary (JSON keys + table headers). Returns, DESCENDING
    by confidence::

        [{"label": <real label as it appears>, "confidence": 0.0-1.0,
          "why": "<one phrase>"}]

    Suggestion-grade: the caller SHOWS these and lets the user include them; it
    must never silently substitute. Field-agnostic — it never needs to know
    which field was asked for. See the module block above for why silent
    matching is unsafe and this is not.
    """
    req_raw = str(requested or "").strip()
    req = _key_tokens(req_raw)
    if not req:
        return []
    req_norm = " ".join(req)
    req_sing = [_singularize(t) for t in req]
    idf = _idf_map(available)
    affixes = _structural_affixes(available)

    seen_norm = set()
    out: List[Dict[str, Any]] = []
    for raw in (available or []):
        label = str(raw).strip()
        lab = _key_tokens(label)
        if not lab:
            continue
        lab_norm = " ".join(lab)
        # De-dup labels that normalise identically (pointOfContact vs
        # point_of_contact), keeping the first surface form.
        if lab_norm in seen_norm:
            continue

        conf = 0.0
        why = ""
        lab_sing = [_singularize(t) for t in lab]

        # IDF-weighted "how much of the LABEL does the request explain". Extra
        # words in the label that are RARE (high IDF) are substantive — they
        # make a DIFFERENT field ("Material Cost", "Bill of Materials"), so they
        # tank the score. Extra words that are COMMON in the vault's own labels
        # (low IDF) are affixes/noise ("Router_" repeated across a whole schema
        # generation, "_1", generic qualifiers) and barely dent it. This is the
        # lexicon-free way to tell a meaningful prefix from a meaningless one:
        # the vault's own token frequencies decide, not a hardcoded word list.
        # It also means precision scales with how consistently a drift repeats —
        # a one-off oddball label is correctly low-confidence.
        def _explains(req_toks, lab_toks):
            req_idf = sum(idf.get(t, 3.0) for t in req_toks)
            rem = list(req_toks)
            extra = []
            for t in lab_toks:
                if t in rem:
                    rem.remove(t)
                else:
                    extra.append(t)
            # A structural affix (a wrapper the vault stamps on many labels)
            # barely counts against the match; a substantive extra word — one
            # that makes a different field — counts in full.
            extra_idf = sum((0.12 if t in affixes else 1.0) * idf.get(t, 3.0)
                            for t in extra)
            if req_idf + extra_idf <= 0:
                return 1.0
            return req_idf / (req_idf + extra_idf)

        if lab_norm == req_norm:
            conf, why = 1.0, "exact match"
        else:
            idx = _contiguous_index(lab, req)
            idx_s = _contiguous_index(lab_sing, req_sing)
            if idx >= 0:
                # Request appears intact; the rest of the label is affixes,
                # scored by how substantive those affixes are.
                ratio = _explains(req, lab)
                conf = 0.55 + 0.42 * ratio
                why = _affix_phrase(idx, len(lab) - idx - len(req))
            elif lab_sing == req_sing:
                conf, why = 0.95, "singular/plural of the same name"
            elif idx_s >= 0:
                ratio = _explains(req_sing, lab_sing)
                conf = 0.52 + 0.40 * ratio
                why = "singular/plural, " + _affix_phrase(
                    idx_s, len(lab) - idx_s - len(req))
            elif set(req).issubset(set(lab)):
                # Reordered. Word order often flips meaning ("part number" vs
                # "number of parts"), so this is a MAYBE, scaled by extra-word
                # substance and never confident on its own.
                conf = 0.45 + 0.20 * _explains(req, lab)
                why = "same words, different order"
            elif set(req_sing).issubset(set(lab_sing)):
                conf = 0.42 + 0.18 * _explains(req_sing, lab_sing)
                why = "same words (singular/plural), different order"
            elif (len(lab) == 1 and len(req) > 1
                  and lab_norm == _acronym_of(req)):
                conf, why = 0.80, "acronym of " + req_raw
            elif (len(req) == 1 and len(lab) > 1
                  and req_norm == _acronym_of(lab)):
                conf, why = 0.72, "the label these are the initials of"
            else:
                # Fuzzy, gated hard so a near-spelling of ONE token cannot
                # match a different field. Require: an exactly-shared token to
                # anchor (the sibling), AND every remaining request token to be
                # a one-edit typo of a label token. "Contract Number" shares 0
                # tokens with "point of contact", so it never engages — the
                # contact/contract trap dies without naming either word.
                shared = set(req) & set(lab)
                if shared and len(req) == len(lab):
                    used = list(lab)
                    ok = True
                    typo_used = False
                    for rt in req:
                        if rt in used:
                            used.remove(rt)
                            continue
                        hit = next((lt for lt in used if _is_typo_of(rt, lt)),
                                   None)
                        if hit is None:
                            ok = False
                            break
                        used.remove(hit)
                        typo_used = True
                    if ok and typo_used:
                        conf, why = 0.62, "possible spelling variant"
                if conf == 0.0:
                    # Last resort, IDF-gated: enough of the request's RARE
                    # tokens are present. A common shared token alone cannot
                    # trip this, so "Contact Method" does not suggest for
                    # "point of contact" on the strength of "contact".
                    total = sum(idf.get(t, 1.0) for t in req)
                    got = sum(idf.get(t, 1.0) for t in req if t in lab)
                    frac = got / total if total else 0.0
                    if frac >= 0.66 and len(set(req) & set(lab)) >= 1:
                        conf = 0.5 + 0.1 * (frac - 0.66) / 0.34
                        why = "shares its most distinctive words"

        if conf >= min_confidence:
            seen_norm.add(lab_norm)
            out.append({"label": label, "confidence": round(conf, 3),
                        "why": why})

    out.sort(key=lambda d: (-d["confidence"], d["label"].lower()))
    return out[:limit]


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


def _looks_textual(p: Path, probe: int = 8192) -> bool:
    """Is this file plain text, judged by its BYTES rather than its name?

    An extension whitelist cannot answer "did you search all my files". A
    vault is full of readable text with names nobody whitelisted — README,
    Makefile, run_output.dat, batch.rpt, notes.text, job_314 with no suffix at
    all. Every one was skipped silently. So the question the loop asks is no
    longer "do I recognise this extension" but "is this actually text".

    NUL bytes are the classic binary tell; beyond that, a high proportion of
    undecodable bytes means it is not text we can field-search. Reads at most
    `probe` bytes, so this costs one small read per unknown file.
    """
    try:
        with open(p, "rb") as fh:
            chunk = fh.read(probe)
    except Exception:
        return False
    if not chunk:
        return False
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    # Not clean UTF-8 — allow a little corruption (a stray latin-1 byte in an
    # otherwise textual log) but not a lot.
    bad = sum(1 for b in chunk if b < 9 or (13 < b < 32))
    return (bad / len(chunk)) < 0.05


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
                                text_max_chars: int = 5_000_000,
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
    # scanned must count files we actually OPENED. It used to be assigned
    # len(files) at the end — the number ENUMERATED — so coverage_line reported
    # "Searched all 20 file(s)" after reading 9 of them. Same class of bug as
    # every other count in this app that was right in one unit and wrong in the
    # equivalent one; here it made the coverage line, whose entire job is to be
    # trustworthy, into the least trustworthy thing on screen.
    _read_count = 0
    _skipped: List[Tuple[str, str]] = []
    _partial: List[str] = []
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
                if not cols:
                    _skipped.append((p.name, "could not read the table header"))
                    continue
                _read_count += 1
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
            elif suf in _TEXTUAL or suf in _EXTRACTABLE or _looks_textual(p):
                # `or _looks_textual(p)` is the difference between "I searched
                # the extensions I know" and "I searched your files". The
                # whitelist silently dropped README, Makefile, run_output.dat,
                # batch.rpt and every extensionless file — while still counting
                # them as scanned.
                if not fn:
                    continue
                text = _read_text(p, max_chars=text_max_chars)
                if not text:
                    _skipped.append((p.name, "unreadable or empty"))
                    continue
                # A field can sit anywhere in a file, so a truncated read that
                # finds nothing is NOT a "no". This app's own primary shape is
                # a field JSON with every key on ONE line, where the tail is as
                # likely to hold the point of contact as the head — measured, a
                # 260 KB one-line JSON lost its POC to the cap and reported
                # "Searched all files" with a straight face.
                if (text_max_chars is not None
                        and len(text) >= text_max_chars):
                    _partial.append(p.name)
                _read_count += 1
                for v in _field_values_in_text(text, fn):
                    ok, note = _match_detail(v, value)
                    if ok:
                        out.append((str(p),
                                    (f"{field}: {v}"[:140]
                                     + (f"  [{note}]" if note else ""))))
                        break
            else:
                # Genuinely binary and not an extractable document. Say so —
                # a file we cannot read must be reported, never absorbed into
                # a "searched everything" count.
                _skipped.append((p.name, f"unsupported file type ({suf or 'no extension'})"))
        except Exception as exc:
            _skipped.append((p.name, f"read failed: {exc.__class__.__name__}"))
            continue
    if on_progress is not None:
        try:
            on_progress(total, total)
        except Exception:
            pass
    if stats is not None:
        stats["scanned"] = _read_count          # files actually OPENED
        stats["total_files"] = total_found
        stats["hits"] = len(out)
        stats["skipped"] = len(_skipped)
        stats["skipped_detail"] = _skipped[:50]
        stats["partial"] = len(_partial)
        stats["partial_detail"] = _partial[:50]
        # A skipped or partially-read file means the answer is not complete.
        # Say so through the SAME flag the caps use, so every incomplete run
        # reaches the user by one path instead of some being announced and
        # others staying quiet.
        if _skipped and not stats.get("truncated"):
            stats["truncated"] = True
            stats["truncated_why"] = (
                f"{len(_skipped)} of {total_found} file(s) could not be read "
                f"(e.g. {_skipped[0][0]} — {_skipped[0][1]})")
        if _partial and not stats.get("truncated"):
            stats["truncated"] = True
            stats["truncated_why"] = (
                f"{len(_partial)} file(s) were larger than the "
                f"{text_max_chars:,}-char read limit and were only searched "
                f"up to it (e.g. {_partial[0]})")
    return out


def field_value_counts(root: Any, field: str, *,
                       max_files: Optional[int] = None,
                       text_max_chars: int = 5_000_000,
                       on_progress: Optional[Callable[[int, int],
                                                      None]] = None,
                       stats: Optional[dict] = None
                       ) -> List[Tuple[str, int]]:
    """Every DISTINCT value of ``field`` across the vault, with a file count.

    Answers "what are all the names, and how often does each appear, for the
    point of contact" — the DISTRIBUTION of a field, not the files matching one
    value. Returns ``[(value, n_files)]`` sorted by count descending, then
    value. Counts FILES, not occurrences: a value listed five times in one file
    counts once for that file, because "how often it appears" across files is
    the question people mean.

    Same coverage discipline as find_files_with_field_value: no caps by
    default, every readable file examined, and ``stats`` filled with the true
    scanned/total/skipped so the caller can say how complete the tally is. A
    partial tally is worse than useless if it looks complete — a name that
    appears in unread files would read as rarer than it is.
    """
    root = Path(root)
    fn = _norm(field)
    if stats is not None:
        stats.update({"scanned": 0, "total_files": 0, "distinct": 0,
                      "files_with_field": 0, "truncated": False,
                      "truncated_why": ""})
    if not fn:
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

    # value (normalised for grouping) -> [display value, set of file indices].
    # Files are grouped by the NORMALISED value so "Bob Smith" and "bob smith"
    # are one person, but the first-seen surface form is what we show.
    groups: Dict[str, List[Any]] = {}
    read_count = 0
    skipped: List[Tuple[str, str]] = []
    partial: List[str] = []

    for i, p in enumerate(files):
        if on_progress is not None and i % 100 == 0:
            try:
                on_progress(i, total)
            except Exception:
                pass
        if _cl is not None:
            try:
                if _cl.is_protected_path(p, root):
                    continue
            except Exception:
                pass
        suf = p.suffix.lower()
        if not (suf in _TABULAR or suf in _TEXTUAL or suf in _EXTRACTABLE
                or _looks_textual(p)):
            skipped.append((p.name, f"unsupported file type "
                                    f"({suf or 'no extension'})"))
            continue
        try:
            vals = extract_field_value(p, field, max_values=1000)
        except Exception as exc:
            skipped.append((p.name, f"read failed: {exc.__class__.__name__}"))
            continue
        read_count += 1
        if not vals:
            continue
        # Distinct values WITHIN this file, so one file contributes at most 1
        # to each value's count regardless of repeats.
        seen_here = set()
        for raw in vals:
            for one in (_split_values(raw) or [raw]):
                one = str(one).strip()
                key = _norm(one)
                if not key or key in seen_here:
                    continue
                seen_here.add(key)
                g = groups.get(key)
                if g is None:
                    groups[key] = [one, 1]
                else:
                    g[1] += 1

    if on_progress is not None:
        try:
            on_progress(total, total)
        except Exception:
            pass

    out = sorted(((disp, cnt) for disp, cnt in groups.values()),
                 key=lambda t: (-t[1], t[0].lower()))
    if stats is not None:
        stats["scanned"] = read_count
        stats["total_files"] = total_found
        stats["distinct"] = len(out)
        stats["files_with_field"] = sum(c for _v, c in out)
        stats["skipped"] = len(skipped)
        stats["skipped_detail"] = skipped[:50]
        if skipped and not stats.get("truncated"):
            stats["truncated"] = True
            stats["truncated_why"] = (
                f"{len(skipped)} of {total_found} file(s) could not be read "
                f"(e.g. {skipped[0][0]} — {skipped[0][1]})")
    return out


# ============================================================
# Intent: "which files have <value> as the <field>?"
#
# The three regexes in the console matched 6 of 15 realistic phrasings of this
# question — including, measured, the user's own words ("search all files for
# bob as point of contact"), which fell through to the model with capped
# context. The other 9 got a guess instead of a search.
#
# More regexes is not the answer; there is always a tenth phrasing. Instead:
# pull a CANDIDATE (field, value) out of the sentence loosely, then VALIDATE
# the field against the vault's real vocabulary. A field the vault does not
# have never routes, so a loose parse cannot hijack ordinary chat — the guard
# is the data, not the grammar.
# ============================================================

# Words that mean "a file", so a question is about finding files at all.
_FILE_NOUN_RE = re.compile(
    r"\b(files?|documents?|docs?|reports?|records?|sheets?|everything|"
    r"anywhere|anything)\b", re.I)
# Openers that mean "search", ANCHORED to the start of the sentence.
#
# Anchoring matters: an unanchored 'is' matched any sentence containing the
# word, so the STATEMENT "the owner of this project is unclear to me" routed as
# a search for owner='project'. A search is asked for at the start — "is bob
# the point of contact anywhere?" — while a passing mention of a field mid-
# sentence is just conversation.
_SEARCH_VERB_RE = re.compile(
    r"^\s*(find|list|show|search|get|which|what|who|how\s+many|count|"
    r"tell\s+me|give\s+me|is|are|does|do)\b", re.I)
# The joins between a field and its value, in either order.
_REL_RE = re.compile(
    r"\s+(?:listed\s+|shown\s+|set\s+|marked\s+|given\s+|named\s+)?"
    r"(?:as|is|are|was|were|=|equals?|for|of|on|to)\s+"
    r"(?:the\s+|a\s+|an\s+|their\s+|its\s+)?", re.I)
_TRIM_RE = re.compile(r"^[\s'\"`,:;.\-]+|[\s'\"`,:;.\-?!]+$")
# A "who ..." question asks for a value, not a file list — see the guard in
# parse_field_value_intent. The \b matters: without it this also swallows
# "whole", "whoever", ... and refuses to route a legitimate search.
_WHO_RE = re.compile(r"^\s*who\b", re.I)

# An AGGREGATION question asks for the DISTRIBUTION of a field's values across
# files — "all the names and how often they appear", "how many files per point
# of contact", "distinct values of X" — NOT for the files where the field has
# one specific value. The two share almost every word, so the field:value
# parser happily forced an aggregation into a search, grabbing a stray word out
# of the sentence as the "value": measured, "all names and how often they
# appear ... for point of contact" parsed to value='searchable' and answered
# "no files where 'point of contact' is 'searchable'". Detected here so the
# field:value parser can decline and the caller can route to the counter.
_AGG_INTENT_RE = re.compile(
    r"\bhow\s+(?:often|many|frequently)\b"
    r"|\b(?:value|name)\s+counts?\b"
    r"|\bcount\s+(?:of|by|per)\b"
    r"|\b(?:each|every|all|distinct|unique|different)\b[^.?!]*\b"
    r"(?:appears?|appearing|occurs?|occurrence|frequency|how\s+many|count)\b"
    r"|\b(?:distinct|unique|all\s+the|every|list\s+all|what\s+are\s+all)\b"
    r"[^.?!]*\bvalues?\b"
    r"|\bbreak\s*down\b|\bdistribution\s+of\b|\btally\b|\bhistogram\b",
    re.I,
)


def looks_like_aggregation(text: str) -> bool:
    """True when the text asks for a field's value DISTRIBUTION, not its files.

    Kept deliberately narrow: it must see an explicit counting/enumeration
    cue ("how often", "how many ... per", "distinct values", "value counts",
    "breakdown of"). A plain "which files have X as the point of contact" has
    none of these and is unaffected."""
    return bool(_AGG_INTENT_RE.search(str(text or "")))


def _clean_phrase(s: str) -> str:
    return _TRIM_RE.sub("", str(s or "")).strip()


def looks_like_file_search(text: str) -> bool:
    """Is this asking to find files at all? (cheap pre-filter)"""
    t = str(text or "")
    return bool(_FILE_NOUN_RE.search(t) or _SEARCH_VERB_RE.search(t))


def parse_field_value_intent(text: str, known_fields) -> Optional[Tuple[str, str]]:
    """(field, value) when ``text`` asks which files carry a field's value.

    ``known_fields`` is the vault's real field vocabulary — CSV column names,
    text labels. The field MUST be one of them: that is what makes a loose
    parse safe. "which files mention the blorp of bob" parses fine and then
    routes nowhere, because no file has a blorp.

    Returns None when the sentence does not carry a known field + a value.
    """
    t = _clean_phrase(text)
    if not t or not looks_like_file_search(t):
        return None
    # "who is the point of contact on job 412?" asks for a NAME, not a list of
    # files. Parsed loosely it yields field='point of contact', value='job 412'
    # — a search that matches nothing and answers "No files found" with total
    # confidence, turning a good question into a false negative. A "who"
    # question wants a value out of a file; it is not this route's job.
    if _WHO_RE.match(t):
        return None
    # An aggregation ("all names and how often they appear for point of
    # contact") is NOT a field:value search. Forced into one, it grabbed a
    # stray sentence word as the value and answered "no files where 'point of
    # contact' is 'searchable'". Decline here so the caller routes it to the
    # counter instead.
    if looks_like_aggregation(t):
        return None
    norm_t = _norm(t)
    if not norm_t:
        return None

    # Longest known field first: 'point of contact' must win over 'contact'.
    cands = sorted({_norm(f) for f in (known_fields or []) if _norm(f)},
                   key=len, reverse=True)
    field_n = next((f for f in cands
                    if re.search(rf"\b{re.escape(f)}\b", norm_t)), None)
    if not field_n:
        return None

    # Split the normalised sentence on the field: the value sits on one side.
    m = re.search(rf"\b{re.escape(field_n)}\b", norm_t)
    before, after = norm_t[:m.start()], norm_t[m.end():]

    # Prefer a value AFTER the field ("point of contact is bob", "poc bob").
    val = _value_after(after)
    if not val:
        val = _value_before(before)
    if not val:
        return None
    return field_n, val


# Words that are never the value being searched for.
_VALUE_STOP = {
    "find", "list", "show", "search", "get", "which", "what", "who", "how",
    "many", "count", "tell", "give", "me", "all", "the", "a", "an", "of",
    "for", "with", "have", "has", "having", "that", "is", "are", "was",
    "were", "as", "in", "on", "to", "and", "or", "files", "file", "documents",
    "document", "docs", "doc", "reports", "report", "records", "record",
    "everything", "anywhere", "anything", "listed", "their", "its", "my",
    "please", "sheets", "sheet", "same", "does", "do", "us", "it", "this",
    "them", "any",
    # Relative/interrogative connectors. Without these, "files WHERE bob is
    # listed as poc" yields the value "where bob" and finds nothing — the
    # search runs, matches nobody, and reports a confident empty list.
    "where", "when", "whose", "whom", "there", "here", "contain", "contains",
    "containing", "mention", "mentions", "mentioning", "include", "includes",
    "including", "shows", "showing", "marked", "set", "given", "named",
    "assigned", "owns", "own", "owned",
}


def _take_name(tokens) -> str:
    """The leading run of non-stopword tokens — the value."""
    out = []
    for tok in tokens:
        if tok in _VALUE_STOP:
            if out:
                break
            continue
        out.append(tok)
    return " ".join(out)


def _value_after(after: str) -> str:
    a = _REL_RE.sub(" ", " " + after, count=1).strip() if after.strip() else ""
    return _take_name(a.split()) if a else ""


def _value_before(before: str) -> str:
    # "...bob as the point of contact" -> the value is the tail of `before`.
    toks = before.split()
    tail = []
    for tok in reversed(toks):
        if tok in _VALUE_STOP:
            if tail:
                break
            continue
        tail.append(tok)
    return " ".join(reversed(tail))


def coverage_line(stats: dict) -> str:
    """One plain sentence describing how complete a search was.

    A negative result is only trustworthy if the search was complete, so the
    coverage is stated either way rather than left for the reader to assume.
    """
    if not stats:
        return ""
    scanned = stats.get("scanned", 0)
    total = stats.get("total_files", scanned)
    # "Searched all N" is only sayable when N were actually READ. This used to
    # print the enumerated count, so it said "Searched all 20 file(s)" after
    # opening 9 — the one sentence whose job is to let a user trust a negative
    # result was the thing lying about it.
    if total and scanned < total:
        why = stats.get("truncated_why") or "some files could not be read"
        return (f"⚠ PARTIAL — searched {scanned:,} of {total:,} file(s): "
                f"{why}. Treat a missing file as unknown, not absent.")
    if stats.get("truncated"):
        # Resolve the reason on its own line, and keep every f-string on ONE
        # line. This expression used to span a newline INSIDE the braces, which
        # is PEP 701 — Python 3.12+ only. This app's supported floor is 3.11
        # (installs.txt pins python=3.11; setup.bat says "Python 3.11+"), where
        # a single-quoted f-string may not cross a newline at all, so the file
        # was an unimportable SyntaxError: "unterminated string literal".
        why = stats.get("truncated_why") or "results were truncated"
        return (f"⚠ PARTIAL — {why}. "
                "Treat a missing file as unknown, not absent.")
    if total:
        return f"Searched all {total:,} file(s) in the vault."
    return "No files to search."
