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

import re
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

_TABULAR = {".csv", ".tsv", ".xlsx", ".xls", ".parquet"}
_TEXTUAL = {".txt", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl",
            ".ndjson", ".yaml", ".yml", ".html", ".htm", ".xml", ".ini",
            ".cfg", ".csv", ".tsv"}
_EXTRACTABLE = {".pdf", ".docx"}
_VALUE_LINE_RE = re.compile(r"[:\-–—=]\s*(.+)$")


def _norm(s) -> str:
    """Lower-case + collapse non-alphanumerics to single spaces (so 'Point of
    Contact', 'point_of_contact' and 'POINT-OF-CONTACT' compare equal)."""
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


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
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if fn in _norm(line):
            m = _VALUE_LINE_RE.search(line)
            if m and m.group(1).strip():
                return [m.group(1).strip()]
            for j in range(i + 1, min(i + 3, len(lines))):
                if lines[j].strip():
                    return [lines[j].strip()]
    return None


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
                                limit: int = 5000, max_files: int = 100000,
                                text_max_chars: int = 200000,
                                on_progress: Optional[Callable[[int, int],
                                                               None]] = None
                                ) -> List[Tuple[str, str]]:
    """Files under ``root`` where ``field`` is associated with ``value``.
    Returns ``[(abs_path, context)]``. Field-aware (see module docstring).

    Scales to large vaults: tabular files are checked HEADER-FIRST (only the
    matching column is read, and files without the column are skipped without a
    full read); text reads are bounded; ``max_files`` defaults high enough not
    to silently truncate. ``on_progress(scanned, total)`` is called ~every 100
    files so the UI can show progress."""
    root = Path(root)
    fn = _norm(field)
    vn = str(value or "").strip().lower()
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
    files = files[:max_files]
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
        if len(out) >= limit:
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
                if any(vn in v.lower() for v in vals):
                    out.append((str(p), f"column '{col}' = '{value}'"))
            elif suf in _TEXTUAL or suf in _EXTRACTABLE:
                if not fn:
                    continue
                text = _read_text(p, max_chars=text_max_chars)
                if not text:
                    continue
                lines = text.splitlines()
                for k, line in enumerate(lines):
                    if fn in _norm(line):
                        window = " ".join(lines[k:k + 3]).lower()
                        if vn in window:
                            out.append((str(p), line.strip()[:140]))
                            break
        except Exception:
            continue
    if on_progress is not None:
        try:
            on_progress(total, total)
        except Exception:
            pass
    return out
