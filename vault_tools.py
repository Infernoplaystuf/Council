"""
Vault ergonomics — small helpers for browsing the vault, detecting
duplicates, and searching past conversations. Used by chat intents
(`vault stats`, `find duplicates`, `search history for X`, etc.) and
available in the analyst sandbox too.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Protected-path guard. Without this import the calls to is_protected_path()
# below raised NameError every time — silently swallowed by their
# `except Exception: pass`, so the protection check NEVER ran and the vault
# tools would read/grep files under protected dirs (conversation_logs,
# pipelines/out, …). Import the real check so it actually applies; fall
# back to "not protected" (the prior effective behaviour) only if the
# logger module can't be imported.
try:
    from conversation_logger import is_protected_path
except Exception:  # pragma: no cover - defensive
    def is_protected_path(path: Any, vault_dir: Any) -> bool:  # type: ignore
        return False


# ============================================================
# Vault stats
# ============================================================

def list_subfolders(
    root: Path,
    *,
    max_depth: int = 1,
    include_counts: bool = True,
) -> List[Dict[str, Any]]:
    """List the subfolders of `root` with optional per-folder file counts.

    `max_depth=1` lists only immediate children. Bigger numbers descend
    recursively up to that depth. Hidden folders (`.git`, `__pycache__`,
    `.venv`, etc.) are skipped — they're rarely what the user means.
    """
    root = Path(root)
    if not root.exists():
        return []
    SKIP = {"__pycache__", ".git", ".venv", "venv", ".idea", ".vscode",
            "node_modules", ".DS_Store"}
    out: List[Dict[str, Any]] = []

    def _walk(p: Path, depth: int):
        if depth > max_depth:
            return
        for child in sorted(p.iterdir()):
            if not child.is_dir():
                continue
            if child.name in SKIP or child.name.startswith("."):
                continue
            entry: Dict[str, Any] = {
                "name": child.name,
                "path": str(child),
                "depth": depth,
                "relative_path": str(child.relative_to(root)),
            }
            if include_counts:
                try:
                    entry["files"] = sum(
                        1 for c in child.rglob("*") if c.is_file()
                    )
                    entry["subfolders"] = sum(
                        1 for c in child.iterdir() if c.is_dir()
                    )
                except Exception:
                    entry["files"] = None
                    entry["subfolders"] = None
            out.append(entry)
            _walk(child, depth + 1)

    _walk(root, 1)
    return out


def format_subfolder_listing(
    root: Path,
    folders: List[Dict[str, Any]],
    *,
    show_root_files: bool = True,
) -> str:
    """Pretty-print a folder listing in tree form."""
    root = Path(root)
    lines = [f"Folder: {root}"]
    if not folders:
        lines.append("  (no subfolders)")
    else:
        for f in folders:
            indent = "  " * f.get("depth", 1)
            counts = ""
            if f.get("files") is not None:
                counts = f" — {f['files']} files"
                if f.get("subfolders"):
                    counts += f", {f['subfolders']} subfolders"
            lines.append(f"{indent}{f['name']}/{counts}")
    if show_root_files and root.exists():
        try:
            top_files = sorted(p.name for p in root.iterdir()
                               if p.is_file() and not p.name.startswith("."))
            if top_files:
                lines.append("")
                lines.append(f"Top-level files in {root.name}/:")
                for n in top_files[:20]:
                    lines.append(f"  {n}")
                if len(top_files) > 20:
                    lines.append(f"  ... ({len(top_files) - 20} more)")
        except Exception:
            pass
    return "\n".join(lines)


# ============================================================
# Filesystem helpers — tree, grep across files, schema lookup, recent
# ============================================================

def tree(
    root: Path,
    *,
    max_depth: int = 3,
    show_files: bool = False,
    file_limit_per_dir: int = 20,
) -> str:
    """Visual hierarchy of a folder.

    `max_depth` limits recursion. `show_files` includes file names
    under each folder (capped by `file_limit_per_dir`). Skips
    hidden/build directories.
    """
    root = Path(root)
    SKIP = {"__pycache__", ".git", ".venv", "venv", "node_modules",
            ".idea", ".vscode", ".DS_Store"}
    lines: list[str] = [f"{root.name}/"]

    def _walk(p: Path, prefix: str, depth: int):
        if depth > max_depth:
            return
        try:
            kids = sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
        except Exception:
            return
        # Filter
        kids = [k for k in kids if k.name not in SKIP and not k.name.startswith(".")]
        if not show_files:
            kids = [k for k in kids if k.is_dir()]
        elif len(kids) > file_limit_per_dir:
            kids = kids[:file_limit_per_dir] + ["__more__"]
        for i, k in enumerate(kids):
            last = (i == len(kids) - 1)
            branch = "└── " if last else "├── "
            if k == "__more__":
                lines.append(prefix + branch + f"... ({file_limit_per_dir}+ more)")
                continue
            label = k.name + ("/" if k.is_dir() else "")
            lines.append(prefix + branch + label)
            if k.is_dir():
                new_prefix = prefix + ("    " if last else "│   ")
                _walk(k, new_prefix, depth + 1)
    _walk(root, "", 1)
    return "\n".join(lines)


# Text-like file types that participate in grep / pattern searches.
_GREPPABLE_SUFFIXES = {
    ".csv", ".tsv", ".txt", ".md", ".markdown", ".json", ".jsonl",
    ".xml", ".html", ".htm", ".yaml", ".yml", ".toml", ".ini",
    ".log", ".rst", ".ang", ".d3dpipeline", ".py", ".sh", ".bat",
    ".sql", ".cfg",
}


def find_files_containing_text(
    folder: Any,
    query: str,
    *,
    case_sensitive: bool = False,
    max_hits: int = 100,
    context_chars: int = 60,
) -> List[Dict[str, Any]]:
    """Grep-style search across every text-like file under `folder`.

    Returns up to `max_hits` matches with file path, line number, and
    a ±context_chars window around the hit. Honors protected-paths
    (conversation_logs/ never gets scanned).
    """
    folder = Path(folder)
    if not folder.exists() or not query.strip():
        return []
    flags = 0 if case_sensitive else re.IGNORECASE
    rx = re.compile(re.escape(query), flags)
    out: List[Dict[str, Any]] = []
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _GREPPABLE_SUFFIXES:
            continue
        try:
            if is_protected_path(p, folder.parent):
                continue
        except Exception:
            pass
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = rx.search(line)
            if not m:
                continue
            start = max(0, m.start() - context_chars)
            end = min(len(line), m.end() + context_chars)
            out.append({
                "path":    str(p.relative_to(folder)),
                "line":    lineno,
                "context": line[start:end].strip(),
            })
            if len(out) >= max_hits:
                return out
    return out


def find_files_with_column(
    folder: Any,
    column_name: str,
    *,
    fuzzy: bool = True,
) -> List[Dict[str, Any]]:
    """Find every CSV / Excel sheet under `folder` that has a column
    matching `column_name`. Case-insensitive substring match by default.

    For Excel files with multiple sheets, each matching sheet is its
    own result row.
    """
    folder = Path(folder)
    if not folder.exists():
        return []
    needle = column_name.strip().lower()
    out: List[Dict[str, Any]] = []
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        try:
            if suf == ".csv":
                try:
                    import pandas as _pd
                    df = _pd.read_csv(p, nrows=0)   # header only — fast
                    cols = [str(c) for c in df.columns]
                except Exception:
                    continue
                matches = [c for c in cols
                           if (needle in c.lower() if fuzzy
                               else c.lower() == needle)]
                if matches:
                    out.append({
                        "path": str(p.relative_to(folder)),
                        "sheet": "",
                        "matched_columns": matches,
                    })
            elif suf in (".xlsx", ".xls", ".xlsm"):
                try:
                    import pandas as _pd
                    xl = _pd.ExcelFile(p)
                    for sname in xl.sheet_names:
                        df = xl.parse(sname, nrows=0)
                        cols = [str(c) for c in df.columns]
                        matches = [c for c in cols
                                   if (needle in c.lower() if fuzzy
                                       else c.lower() == needle)]
                        if matches:
                            out.append({
                                "path": str(p.relative_to(folder)),
                                "sheet": sname,
                                "matched_columns": matches,
                            })
                except Exception:
                    continue
        except Exception:
            continue
    return out


def recent_files(
    folder: Any,
    *,
    since_days: int = 7,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return files modified within the last `since_days` days,
    newest first, up to `limit` entries."""
    import time as _t
    folder = Path(folder)
    if not folder.exists():
        return []
    cutoff = _t.time() - since_days * 86400
    rows: List[Dict[str, Any]] = []
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except Exception:
            continue
        if st.st_mtime < cutoff:
            continue
        try:
            if is_protected_path(p, folder.parent):
                continue
        except Exception:
            pass
        rows.append({
            "path":  str(p.relative_to(folder)),
            "mtime": st.st_mtime,
            "iso":   _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(st.st_mtime)),
            "size":  st.st_size,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


# ============================================================
# Pattern-search families — roman numerals, dates, money, emails,
# phone numbers, URLs, version strings
# ============================================================

# Standard Roman numeral validation regex (1 .. 3999). Word-bounded.
# Uppercase by default because mixed-case "Mxx" is rarely intentional
# and lowercase 'i'/'v'/'x' appears too often in regular text.
_ROMAN_NUMERAL_RE = re.compile(
    r"\b(M{1,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3}))\b"
)

# Short letter pairs that ARE valid Roman numerals but ALSO common
# English abbreviations. Counts in this set go to ambiguous_count
# rather than the primary count.
_AMBIGUOUS_ROMAN = {
    "ML",   # machine learning / milliliter (1050)
    "MD",   # M.D., Maryland (1500)
    "MC",   # Master of Ceremonies (1100)
    "MV",   # music video, megavolt (1005)
    "MI",   # Michigan, Marketing Insights (1001)
    "DC",   # Washington DC (600)
    "DI",   # digital input, drive-in (501)
    "DL",   # download, driver license (550)
    "LI",   # Long Island (51)
    "LV",   # Las Vegas (55)
    "CL",   # chlorine (also the element, 150)
    "CM",   # centimeter (900)
    "CD",   # compact disc (400)
    "XL",   # extra large (40)
}


def _roman_to_int(s: str) -> int:
    table = {"I":1,"V":5,"X":10,"L":50,"C":100,"D":500,"M":1000}
    total = 0
    prev = 0
    for ch in reversed(s):
        v = table.get(ch, 0)
        if v < prev: total -= v
        else: total += v; prev = v
    return total


def find_roman_numerals(
    folder: Any,
    *,
    min_length: int = 2,
    top_n: int = 20,
) -> "Any":
    """Tally Roman numerals across vault text files. Skips single-letter
    matches by default (`min_length=2`) because "I" / "V" / "X" / "L" /
    "C" / "D" / "M" as standalone letters cause overwhelming false
    positives ("I think...", "section X", "Plan B revision V").

    Returns a DataFrame: roman, integer, count, files.
    """
    folder = Path(folder)
    if not folder.exists():
        import pandas as pd
        return pd.DataFrame()
    counts: Dict[str, int] = {}
    amb_counts: Dict[str, int] = {}
    file_sets: Dict[str, set] = {}
    for p in folder.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _GREPPABLE_SUFFIXES:
            continue
        try:
            if is_protected_path(p, folder.parent):
                continue
        except Exception:
            pass
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _ROMAN_NUMERAL_RE.finditer(text):
            tok = m.group(1)
            if len(tok) < min_length:
                continue
            file_sets.setdefault(tok, set()).add(p.name)
            if tok in _AMBIGUOUS_ROMAN:
                amb_counts[tok] = amb_counts.get(tok, 0) + 1
            else:
                counts[tok] = counts.get(tok, 0) + 1
    import pandas as pd
    if not counts and not amb_counts:
        return pd.DataFrame()
    rows = []
    for r in set(counts) | set(amb_counts):
        rows.append({
            "roman":           r,
            "integer":         _roman_to_int(r),
            "count":           counts.get(r, 0),
            "ambiguous_count": amb_counts.get(r, 0),
            "files":           len(file_sets[r]),
        })
    df = pd.DataFrame(rows).sort_values("count", ascending=False).reset_index(drop=True)
    return df.head(top_n)


# Date extraction — common formats. Tags each match with the format
# that matched so the caller can audit.
_DATE_PATTERNS = [
    ("YYYY-MM-DD",   re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")),
    ("MM/DD/YYYY",   re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")),
    ("DD/MM/YYYY",   re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")),   # same regex, different interp
    ("DD.MM.YYYY",   re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b")),
    ("Month DD, YYYY", re.compile(
        r"\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+\d{4})\b",
        re.IGNORECASE,
    )),
    ("YYYY-MM-DD hh:mm", re.compile(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})")),
]


def find_dates_in_text(text: str, *, dedupe: bool = True) -> List[Dict[str, str]]:
    """Extract dates from `text` in common formats. Each match returns
    {date_string, format}. With dedupe=True (default), each unique
    (string, format) pair appears only once."""
    seen: set = set()
    out: List[Dict[str, str]] = []
    for fmt, rx in _DATE_PATTERNS:
        # Only run the first MM/DD/YYYY regex once (avoid duplicate hits)
        if fmt == "DD/MM/YYYY":
            continue
        for m in rx.finditer(text):
            s = m.group(1)
            key = (s, fmt) if dedupe else (s, fmt, m.start())
            if key in seen:
                continue
            seen.add(key)
            out.append({"date": s, "format": fmt})
    return out


_MONEY_RE = re.compile(
    # $1,234.56  €500  £42.10  ¥1000  JPY 100  USD 50.00
    r"(?<![\w])("
    r"[\$€£¥₹]\s?\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?"
    r"|(?:USD|EUR|GBP|JPY|CNY|INR|CAD|AUD|CHF)\s+\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?"
    r")\b"
)


def find_money_amounts(folder: Any, *, max_hits: int = 200) -> List[Dict[str, Any]]:
    """Find currency amounts in text files."""
    folder = Path(folder)
    if not folder.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in folder.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _GREPPABLE_SUFFIXES:
            continue
        try:
            if is_protected_path(p, folder.parent):
                continue
        except Exception:
            pass
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _MONEY_RE.finditer(text):
            out.append({"path": str(p.relative_to(folder)),
                        "amount": m.group(1).strip()})
            if len(out) >= max_hits:
                return out
    return out


_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL_RE   = re.compile(r"\bhttps?://[^\s\"'<>)]+", re.IGNORECASE)
_PHONE_RE = re.compile(
    r"(?<!\d)("
    r"\+?\d{1,3}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}"
    r"|\(\d{3}\)\s?\d{3}[\s.-]?\d{4}"
    r"|\d{3}[\s.-]\d{3}[\s.-]\d{4}"
    r")(?!\d)"
)
_VERSION_RE = re.compile(
    r"\b(v?\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.]+)?)\b"
)


def _pattern_scan(folder: Path, pattern: "re.Pattern",
                  max_hits: int = 200) -> List[Dict[str, Any]]:
    """Generic pattern-walker used by find_emails / find_phone_numbers /
    find_urls / find_versions."""
    out: List[Dict[str, Any]] = []
    folder = Path(folder)
    if not folder.exists():
        return out
    for p in folder.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _GREPPABLE_SUFFIXES:
            continue
        try:
            if is_protected_path(p, folder.parent):
                continue
        except Exception:
            pass
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in pattern.finditer(text):
            out.append({"path": str(p.relative_to(folder)),
                        "match": m.group(0).strip()})
            if len(out) >= max_hits:
                return out
    return out


def find_emails(folder: Any, *, max_hits: int = 200) -> List[Dict[str, Any]]:
    return _pattern_scan(Path(folder), _EMAIL_RE, max_hits=max_hits)


def find_phone_numbers(folder: Any, *, max_hits: int = 200) -> List[Dict[str, Any]]:
    return _pattern_scan(Path(folder), _PHONE_RE, max_hits=max_hits)


def find_urls(folder: Any, *, max_hits: int = 200) -> List[Dict[str, Any]]:
    return _pattern_scan(Path(folder), _URL_RE, max_hits=max_hits)


def find_versions(folder: Any, *, max_hits: int = 200) -> List[Dict[str, Any]]:
    return _pattern_scan(Path(folder), _VERSION_RE, max_hits=max_hits)


# ============================================================
# Atomic-element search (periodic-table tally over vault content)
# ============================================================

# Canonical periodic table, lowercase name -> exact symbol (1..118).
_ELEMENTS: Dict[str, str] = {
    "hydrogen":"H", "helium":"He", "lithium":"Li", "beryllium":"Be",
    "boron":"B", "carbon":"C", "nitrogen":"N", "oxygen":"O", "fluorine":"F",
    "neon":"Ne", "sodium":"Na", "magnesium":"Mg", "aluminum":"Al",
    "aluminium":"Al", "silicon":"Si", "phosphorus":"P", "sulfur":"S",
    "sulphur":"S", "chlorine":"Cl", "argon":"Ar", "potassium":"K",
    "calcium":"Ca", "scandium":"Sc", "titanium":"Ti", "vanadium":"V",
    "chromium":"Cr", "manganese":"Mn", "iron":"Fe", "cobalt":"Co",
    "nickel":"Ni", "copper":"Cu", "zinc":"Zn", "gallium":"Ga",
    "germanium":"Ge", "arsenic":"As", "selenium":"Se", "bromine":"Br",
    "krypton":"Kr", "rubidium":"Rb", "strontium":"Sr", "yttrium":"Y",
    "zirconium":"Zr", "niobium":"Nb", "molybdenum":"Mo", "technetium":"Tc",
    "ruthenium":"Ru", "rhodium":"Rh", "palladium":"Pd", "silver":"Ag",
    "cadmium":"Cd", "indium":"In", "tin":"Sn", "antimony":"Sb",
    "tellurium":"Te", "iodine":"I", "xenon":"Xe", "cesium":"Cs",
    "caesium":"Cs", "barium":"Ba", "lanthanum":"La", "cerium":"Ce",
    "praseodymium":"Pr", "neodymium":"Nd", "promethium":"Pm", "samarium":"Sm",
    "europium":"Eu", "gadolinium":"Gd", "terbium":"Tb", "dysprosium":"Dy",
    "holmium":"Ho", "erbium":"Er", "thulium":"Tm", "ytterbium":"Yb",
    "lutetium":"Lu", "hafnium":"Hf", "tantalum":"Ta", "tungsten":"W",
    "wolfram":"W", "rhenium":"Re", "osmium":"Os", "iridium":"Ir",
    "platinum":"Pt", "gold":"Au", "mercury":"Hg", "thallium":"Tl",
    "lead":"Pb", "bismuth":"Bi", "polonium":"Po", "astatine":"At",
    "radon":"Rn", "francium":"Fr", "radium":"Ra", "actinium":"Ac",
    "thorium":"Th", "protactinium":"Pa", "uranium":"U", "neptunium":"Np",
    "plutonium":"Pu", "americium":"Am", "curium":"Cm", "berkelium":"Bk",
    "californium":"Cf", "einsteinium":"Es", "fermium":"Fm", "mendelevium":"Md",
    "nobelium":"No", "lawrencium":"Lr", "rutherfordium":"Rf", "dubnium":"Db",
    "seaborgium":"Sg", "bohrium":"Bh", "hassium":"Hs", "meitnerium":"Mt",
    "darmstadtium":"Ds", "roentgenium":"Rg", "copernicium":"Cn", "nihonium":"Nh",
    "flerovium":"Fl", "moscovium":"Mc", "livermorium":"Lv", "tennessine":"Ts",
    "oganesson":"Og",
}

# Reverse map (symbol -> canonical lowercase name). Where multiple
# common names share a symbol (aluminum/aluminium, sulfur/sulphur,
# wolfram/tungsten), the first definition wins as the "canonical" key.
_SYMBOL_TO_NAME: Dict[str, str] = {}
for _name, _sym in _ELEMENTS.items():
    _SYMBOL_TO_NAME.setdefault(_sym, _name)

# Precompiled regexes.
# Names: case-insensitive, word boundaries.
_NAME_RE = re.compile(
    r"\b(" + "|".join(sorted(_ELEMENTS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
# Symbols: case-sensitive, word boundaries, longest first so "Si" is
# preferred over "S" + "i" and "Fe" over "F" + "e".
_SYMBOL_RE = re.compile(
    r"\b(" + "|".join(
        re.escape(s) for s in sorted(_SYMBOL_TO_NAME, key=len, reverse=True)
    ) + r")\b"
)

# Single-letter symbols are extremely prone to false positives in
# regular text ("plan B", "vitamin C", "section H"). We still report
# them but in a separate column so the caller can decide whether to
# trust them.
_SINGLE_LETTER_SYMS = {s for s in _SYMBOL_TO_NAME if len(s) == 1}

# Two-letter symbols that ARE common English words. Case-sensitive
# regex with word boundaries doesn't help here — they're real words
# spelled exactly like the symbols. Hits attributed to these go in
# the `ambiguous_hits` column and are excluded from the default rank.
_AMBIGUOUS_SYMS = {
    "In",   # preposition
    "As",   # conjunction
    "He",   # pronoun
    "No",   # negative
    "At",   # preposition
    "Be",   # verb
    "Co",   # "Smith & Co.", "Co-op"
    "Si",   # Italian / Spanish "yes"; common as variable name in code
    "Md",   # "M.D." medical doctor
    "Pa",   # "pa" (informal for father), "Pa." (Pennsylvania abbr.)
}

# Element NAMES that are also common English words. Their `name` hits
# go into the `ambiguous_hits` column too, since "team lead", "iron
# out", "gold standard", "silver lining" etc. inflate the count.
_AMBIGUOUS_NAMES = {
    "lead", "tin", "iron", "gold", "silver", "copper", "nickel",
    "mercury", "neon", "argon",
}


def _scan_text_for_elements(text: str) -> Dict[str, Dict[str, int]]:
    """Return per-element counts found in `text`.

    Output: { canonical_name: {"name", "symbol", "single_letter",
                               "ambiguous_name", "ambiguous_symbol"} }
    The `ambiguous_*` buckets isolate hits where the token is also a
    common English word and likely a false positive.
    """
    counts: Dict[str, Dict[str, int]] = {}

    def _entry(nm: str) -> Dict[str, int]:
        return counts.setdefault(nm, {
            "name": 0, "symbol": 0, "single_letter": 0,
            "ambiguous_name": 0, "ambiguous_symbol": 0,
        })

    # Names (case-insensitive)
    for m in _NAME_RE.finditer(text):
        matched = m.group(1).lower()
        sym = _ELEMENTS[matched]
        name = _SYMBOL_TO_NAME[sym]
        e = _entry(name)
        if matched in _AMBIGUOUS_NAMES:
            e["ambiguous_name"] += 1
        else:
            e["name"] += 1
    # Symbols (case-sensitive)
    for m in _SYMBOL_RE.finditer(text):
        sym = m.group(1)
        name = _SYMBOL_TO_NAME[sym]
        e = _entry(name)
        if sym in _AMBIGUOUS_SYMS:
            e["ambiguous_symbol"] += 1
        else:
            e["symbol"] += 1
        if len(sym) == 1:
            e["single_letter"] += 1
    return counts


# File types we'll scan as plain text. BSON / xlsx need special handling.
_ELEMENT_SCAN_TEXT_SUFFIXES = {
    ".csv", ".tsv", ".txt", ".md", ".markdown", ".json", ".jsonl",
    ".xml", ".html", ".htm", ".yaml", ".yml", ".toml", ".ini",
    ".log", ".rst", ".ang",            # EBSD .ang files are plain text
    ".d3dpipeline",
}
_ELEMENT_SCAN_EXCEL_SUFFIXES = {".xlsx", ".xls", ".xlsm"}


def find_atomic_elements_in_folder(
    folder: Any,
    *,
    recursive: bool = True,
    discount_single_letter: bool = True,
) -> "Any":
    """Walk `folder` and tally how often each atomic element appears,
    broken out by detection method (proper name vs. symbol).

    Returns a pandas DataFrame with columns:
      element, symbol, name_hits, symbol_hits, single_letter_hits,
      total, total_excluding_single_letter, files
    sorted by `total_excluding_single_letter` descending (this is the
    more reliable ranking — single-letter symbols are prone to false
    positives like "vitamin C" or "plan B").

    `discount_single_letter` controls which "total" is the primary
    sort key. Set False to rank by raw total instead.
    """
    folder = Path(folder)
    if not folder.exists():
        import pandas as pd
        return pd.DataFrame()

    # name -> aggregated counts + set of files mentioning it
    total: Dict[str, Dict[str, int]] = {}
    file_sets: Dict[str, set] = {}

    def _accumulate(per_text: Dict[str, Dict[str, int]], source_name: str):
        for nm, c in per_text.items():
            agg = total.setdefault(nm, {
                "name": 0, "symbol": 0, "single_letter": 0,
                "ambiguous_name": 0, "ambiguous_symbol": 0,
            })
            for k in ("name", "symbol", "single_letter",
                      "ambiguous_name", "ambiguous_symbol"):
                agg[k] += c[k]
            file_sets.setdefault(nm, set()).add(source_name)

    walker = folder.rglob("*") if recursive else folder.glob("*")
    for p in walker:
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        # Skip protected paths so conversation logs don't leak into results
        try:
            if is_protected_path(p, folder.parent):
                continue
        except Exception:
            pass

        if suf in _ELEMENT_SCAN_TEXT_SUFFIXES:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            counts = _scan_text_for_elements(text)
            if counts:
                _accumulate(counts, p.name)

        elif suf in _ELEMENT_SCAN_EXCEL_SUFFIXES:
            try:
                import openpyxl as _oxl
                wb = _oxl.load_workbook(p, read_only=True, data_only=True)
            except Exception:
                continue
            try:
                file_text_parts: List[str] = []
                for ws in wb.worksheets:
                    # Use first ~5000 rows; bigger workbooks rarely need more
                    for row in ws.iter_rows(values_only=True, max_row=5000):
                        for v in row:
                            if v is None:
                                continue
                            file_text_parts.append(str(v))
                blob = " ".join(file_text_parts)
                counts = _scan_text_for_elements(blob)
                if counts:
                    _accumulate(counts, p.name)
            finally:
                try: wb.close()
                except Exception: pass

    if not total:
        import pandas as pd
        return pd.DataFrame()

    import pandas as pd
    rows: List[Dict[str, Any]] = []
    for name, c in total.items():
        confident = c["name"] + c["symbol"] - c["single_letter"]
        rows.append({
            "element":              name.capitalize(),
            "symbol":               _ELEMENTS[name],
            "name_hits":            c["name"],
            "symbol_hits":          c["symbol"],
            "single_letter_hits":   c["single_letter"],
            "ambiguous_name_hits":  c["ambiguous_name"],
            "ambiguous_symbol_hits": c["ambiguous_symbol"],
            "confident_total":      confident,
            "raw_total":            c["name"] + c["symbol"]
                                    + c["ambiguous_name"]
                                    + c["ambiguous_symbol"],
            "files":                len(file_sets.get(name, ())),
        })
    df = pd.DataFrame(rows)
    sort_col = "confident_total" if discount_single_letter else "raw_total"
    df = df.sort_values(sort_col, ascending=False).reset_index(drop=True)
    return df


def format_element_ranking(df, *, top_n: int = 10) -> str:
    """Pretty-print the element tally with the false-positive caveat."""
    if df is None or len(df) == 0:
        return "No atomic-element mentions found."
    top = df.head(top_n)
    lines = [f"Top {len(top)} atomic elements (ranked by confident hits):"]
    lines.append(
        "  Confident = name + multi-letter symbol, EXCLUDING:"
    )
    lines.append(
        "    - single-letter symbols (H, C, N, O, B, F, P, S, K, V, I, U, W, Y)"
    )
    lines.append(
        "    - common-English-word symbols (In, As, He, No, At, Be, Co, Si, ...)"
    )
    lines.append(
        "    - common-English-word names (lead, tin, iron, gold, silver, ...)"
    )
    lines.append("")
    lines.append(
        f"  {'#':<3}{'element':<13}{'sym':<5}"
        f"{'name':>7}{'sym':>6}{'amb-n':>7}{'amb-s':>7}"
        f"{'confident':>11}{'files':>7}"
    )
    for i, row in top.iterrows():
        lines.append(
            f"  {i+1:<3}{row['element']:<13}{row['symbol']:<5}"
            f"{int(row['name_hits']):>7}"
            f"{int(row['symbol_hits']):>6}"
            f"{int(row['ambiguous_name_hits']):>7}"
            f"{int(row['ambiguous_symbol_hits']):>7}"
            f"{int(row['confident_total']):>11}"
            f"{int(row['files']):>7}"
        )
    # Top-ambiguous summary so user can see which counts were filtered out
    amb_rows = df[(df['ambiguous_name_hits'] > 0)
                  | (df['ambiguous_symbol_hits'] > 0)].head(5)
    if len(amb_rows):
        lines.append("")
        lines.append("Filtered (likely false positives — common English tokens):")
        for _, r in amb_rows.iterrows():
            parts = []
            if r['ambiguous_name_hits']:
                parts.append(f"name '{r['element'].lower()}' = {int(r['ambiguous_name_hits'])}")
            if r['ambiguous_symbol_hits']:
                parts.append(f"symbol '{r['symbol']}' = {int(r['ambiguous_symbol_hits'])}")
            lines.append(f"  {r['element']:<12} ({', '.join(parts)})")
    return "\n".join(lines)


def vault_stats(vault_dir: Path) -> Dict[str, Any]:
    """Return a snapshot of the vault: per-extension counts and sizes,
    last-modified timestamp, total file count, total size.

    Excludes the index file and the pipelines/out/ folder (since those
    are generated artifacts).
    """
    vault_dir = Path(vault_dir)
    if not vault_dir.exists():
        return {"error": f"vault not found: {vault_dir}"}

    by_ext: Dict[str, Dict[str, int]] = defaultdict(lambda: {"count": 0, "size": 0})
    total_files = 0
    total_size = 0
    latest_mtime = 0.0
    largest: List[Tuple[int, str]] = []

    skip_segments = {"__pycache__", "_wf_stage"}
    for p in vault_dir.rglob("*"):
        if not p.is_file():
            continue
        parts_lower = [s.lower() for s in p.parts]
        if any(seg in parts_lower for seg in skip_segments):
            continue
        # Skip the generated index + denylist files
        if p.name in ("vault_index.json", "fuzzy_denylist.json"):
            continue
        # Skip the modified pipelines folder
        if "pipelines" in parts_lower and "out" in parts_lower:
            try:
                idx_p = parts_lower.index("pipelines")
                if idx_p + 1 < len(parts_lower) and parts_lower[idx_p + 1] == "out":
                    continue
            except ValueError:
                pass
        try:
            st = p.stat()
        except Exception:
            continue
        ext = p.suffix.lower() or "(none)"
        by_ext[ext]["count"] += 1
        by_ext[ext]["size"]  += int(st.st_size)
        total_files += 1
        total_size  += int(st.st_size)
        if st.st_mtime > latest_mtime:
            latest_mtime = st.st_mtime
        largest.append((int(st.st_size), str(p.relative_to(vault_dir))))

    largest.sort(reverse=True)
    return {
        "vault_dir":   str(vault_dir),
        "total_files": total_files,
        "total_size":  total_size,
        "by_ext":      {k: dict(v) for k, v in sorted(by_ext.items(),
                          key=lambda kv: -kv[1]["count"])},
        "last_modified_ts": latest_mtime,
        "last_modified_iso": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_mtime))
            if latest_mtime else ""
        ),
        "largest_files": [(s, n) for s, n in largest[:10]],
    }


def format_vault_stats(stats: Dict[str, Any]) -> str:
    if "error" in stats:
        return f"vault_stats error: {stats['error']}"
    lines = [
        f"Vault: {stats['vault_dir']}",
        f"  total files: {stats['total_files']}",
        f"  total size:  {_human_size(stats['total_size'])}",
        f"  last modified: {stats['last_modified_iso']}",
        "  by extension:",
    ]
    for ext, info in stats["by_ext"].items():
        lines.append(f"    {ext:<10} {info['count']:>5} files  "
                     f"{_human_size(info['size']):>10}")
    if stats["largest_files"]:
        lines.append("  largest files:")
        for size, name in stats["largest_files"][:5]:
            lines.append(f"    {_human_size(size):>10}  {name}")
    return "\n".join(lines)


def _human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


# ============================================================
# Duplicate file detection
# ============================================================

def find_duplicate_files(
    vault_dir: Path,
    *,
    extensions: Optional[Iterable[str]] = None,
    min_size_bytes: int = 64,
) -> List[List[str]]:
    """Find groups of files in the vault with identical SHA-256 hashes.

    Returns a list of duplicate groups; each group is a list of
    file paths (relative to vault_dir). Tiny files are skipped to
    avoid grouping every 0-byte placeholder.
    """
    vault_dir = Path(vault_dir)
    if not vault_dir.exists():
        return []

    ext_filter = ({e.lower() if e.startswith(".") else f".{e.lower()}"
                   for e in extensions} if extensions else None)

    by_hash: Dict[str, List[str]] = defaultdict(list)
    for p in vault_dir.rglob("*"):
        if not p.is_file():
            continue
        if ext_filter and p.suffix.lower() not in ext_filter:
            continue
        try:
            if p.stat().st_size < min_size_bytes:
                continue
        except Exception:
            continue
        if p.name in ("vault_index.json", "fuzzy_denylist.json"):
            continue
        try:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            by_hash[h.hexdigest()].append(str(p.relative_to(vault_dir)))
        except Exception:
            continue

    return [paths for paths in by_hash.values() if len(paths) > 1]


def format_duplicates(groups: List[List[str]]) -> str:
    if not groups:
        return "No duplicate files found."
    lines = [f"Found {len(groups)} duplicate group(s):"]
    for i, group in enumerate(groups, start=1):
        lines.append(f"  Group {i} ({len(group)} copies):")
        for path in group:
            lines.append(f"    {path}")
    return "\n".join(lines)


# ============================================================
# Conversation history search
# ============================================================

_HISTORY_SUBDIR = "conversations"


def _conversations_dir(vault_dir: Path) -> Path:
    return Path(vault_dir) / _HISTORY_SUBDIR


def _load_conversations(vault_dir: Path) -> List[Tuple[Path, List[Dict[str, Any]]]]:
    """Read every conversation file in vault/conversations/.

    Returns [(file_path, [turn_dict, ...]), ...]. Conversations are
    stored as JSONL by ConversationStore.append, one turn per line.
    """
    out: List[Tuple[Path, List[Dict[str, Any]]]] = []
    convo_dir = _conversations_dir(vault_dir)
    if not convo_dir.exists():
        return out
    for p in sorted(convo_dir.glob("*.jsonl")) + sorted(convo_dir.glob("*.json")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        turns: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                turn = json.loads(line)
                if isinstance(turn, dict):
                    turns.append(turn)
            except Exception:
                continue
        if turns:
            out.append((p, turns))
    return out


def query_history_search(
    vault_dir: Path,
    query: str,
    *,
    limit: int = 10,
    who_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Substring-search past conversations for turns containing `query`.

    Returns up to `limit` matches, each annotated with the source file
    name and the turn's timestamp + speaker.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    out: List[Dict[str, Any]] = []
    for path, turns in _load_conversations(vault_dir):
        for turn in turns:
            text = str(turn.get("text", ""))
            who = str(turn.get("who", ""))
            if who_filter and who.lower() != who_filter.lower():
                continue
            if q in text.lower():
                out.append({
                    "session":   path.stem,
                    "ts":        turn.get("ts", ""),
                    "who":       who,
                    "text":      text,
                })
                if len(out) >= limit:
                    return out
    return out


def recent_queries(
    vault_dir: Path,
    *,
    n: int = 10,
) -> List[Dict[str, Any]]:
    """Return the last `n` user turns across all conversations, newest first."""
    user_turns: List[Dict[str, Any]] = []
    for path, turns in _load_conversations(vault_dir):
        for turn in turns:
            if str(turn.get("who", "")).lower() == "user":
                user_turns.append({
                    "session": path.stem,
                    "ts":      turn.get("ts", ""),
                    "text":    str(turn.get("text", "")),
                })
    user_turns.sort(key=lambda t: t.get("ts", ""), reverse=True)
    return user_turns[:n]


def export_transcript_as_markdown(
    vault_dir: Path,
    session_id: str,
    *,
    output_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Render a session's conversation as a Markdown file.

    Reads from vault/conversations/<session_id>.jsonl (the user-visible
    transcript stream, NOT the protected conversation_logs/ folder).
    Writes to vault/data_out/transcript_<session_id>.md by default.
    Returns the output path on success, None if the session file is
    missing or empty.
    """
    src = Path(vault_dir) / _HISTORY_SUBDIR / f"{session_id}.jsonl"
    if not src.exists():
        # Some ConversationStore variants use .json
        src = src.with_suffix(".json")
        if not src.exists():
            return None

    turns: List[Dict[str, Any]] = []
    try:
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                if isinstance(t, dict):
                    turns.append(t)
            except Exception:
                continue
    except Exception:
        return None

    if not turns:
        return None

    lines: List[str] = [f"# Session {session_id}", ""]
    last_who = None
    for t in turns:
        who = str(t.get("who", "")).strip() or "unknown"
        ts  = str(t.get("ts", "")).strip()
        text = str(t.get("text", "")).rstrip()
        if who != last_who:
            lines.append(f"## {who}")
            lines.append("")
            last_who = who
        if ts:
            lines.append(f"_[{ts}]_")
        lines.append("")
        lines.append(text)
        lines.append("")

    if output_dir is None:
        output_dir = Path(vault_dir) / "data_out"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"transcript_{session_id}.md"
    n = 2
    while out_path.exists():
        out_path = output_dir / f"transcript_{session_id}_v{n}.md"
        n += 1
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_path


def format_history_hits(hits: List[Dict[str, Any]]) -> str:
    if not hits:
        return "No matches found in past conversations."
    lines = [f"Found {len(hits)} match(es):"]
    for i, h in enumerate(hits, start=1):
        snippet = h.get("text", "")[:200].replace("\n", " ")
        lines.append(f"  [{i}] {h['session']}  {h['ts']}  {h['who']}:")
        lines.append(f"      {snippet}{'...' if len(h.get('text', '')) > 200 else ''}")
    return "\n".join(lines)
