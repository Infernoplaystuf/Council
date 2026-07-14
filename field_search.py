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
from typing import Any, List, Optional, Tuple

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


def find_files_with_field_value(root: Any, field: str, value: str, *,
                                limit: int = 500, max_files: int = 1000
                                ) -> List[Tuple[str, str]]:
    """Files under ``root`` where ``field`` is associated with ``value``.
    Returns ``[(abs_path, context)]``. Field-aware (see module docstring)."""
    root = Path(root)
    fn = _norm(field)
    vn = str(value or "").strip().lower()
    if not vn:
        return []
    try:
        import conversation_logger as _cl
    except Exception:
        _cl = None
    out: List[Tuple[str, str]] = []
    scanned = 0
    for p in sorted(root.rglob("*")):
        if scanned >= max_files or len(out) >= limit:
            break
        if not p.is_file() or p.name.startswith("."):
            continue
        if _cl is not None:
            try:
                if _cl.is_protected_path(p, root):
                    continue
            except Exception:
                pass
        suf = p.suffix.lower()
        try:
            if suf in _TABULAR:
                scanned += 1
                import vault_analyst as va
                df = va.read_table(p)
                col = va.match_column_name(df.columns, field) if fn else None
                if col is not None:
                    ser = df[col].astype(str).str.lower()
                    if ser.str.contains(re.escape(vn), regex=True,
                                        na=False).any():
                        out.append((str(p), f"column '{col}' = '{value}'"))
            elif suf in _TEXTUAL or suf in _EXTRACTABLE:
                scanned += 1
                text = _read_text(p)
                if not text:
                    continue
                lines = text.splitlines()
                for i, line in enumerate(lines):
                    if fn in _norm(line):
                        window = " ".join(lines[i:i + 3]).lower()
                        if vn in window:
                            out.append((str(p), line.strip()[:140]))
                            break
        except Exception:
            continue
    return out
