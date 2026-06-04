"""
row_citations.py — surface specific CSV/XLSX rows behind an LLM answer.

When Granite says "RET-99001 was the spec-mismatch return for C0042" we
want to be able to point an auditor at the exact row in returns.csv (or
the exact sheet+row in products.xlsx). This module finds the candidate
identifiers in the model's answer, looks them up in the cited source
files, and returns the matching rows as a compact follow-up block to
splice under the answer.

The strategy is intentionally light:
  - Extract uppercase-token identifiers (\\bC0042, RET-99001, SKU-1004,
    ORD-99003 — any ALLCAPS-digit or letter-digit mix) from the answer.
  - For each cited source path (CSV / TSV / XLSX), scan rows and report
    the first row that contains the identifier in any cell.

This is deterministic Python, not an LLM call — the model's text is
already there, we're just attaching ground truth.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# Match codes like C0042, SKU-1004, RET-99001, ORD-12345, WO-2030,
# or any 5+ chars all-caps or alnum-with-dash that look identifier-y.
_ID_RX = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){0,3}\b")
# Filter out obvious English noise (RFP, GPU, AND, OR, etc.).
_STOP_IDS = {
    "AND", "OR", "NOT", "RFP", "GPU", "CPU", "RAM", "VRAM", "CUDA",
    "PDF", "CSV", "XLSX", "JSON", "USA", "EU", "USD", "EUR",
    "ID", "OK", "NO", "YES", "TBD", "TODO", "NA", "URL",
    "SOURCE", "FILE", "RAG", "LLM", "AI", "ML", "DB", "SQL",
    "FYI", "PR", "QA",
}


def extract_identifiers(text: str) -> List[str]:
    raw = _ID_RX.findall(text)
    seen: List[str] = []
    for tok in raw:
        if tok in _STOP_IDS:
            continue
        if len(tok) < 3:
            continue
        # Must contain at least one digit OR a hyphen — pure-letter
        # acronyms like "ABCD" rarely turn out to be data identifiers.
        if not any(ch.isdigit() for ch in tok) and "-" not in tok:
            continue
        if tok not in seen:
            seen.append(tok)
    return seen


def _matches_row(row: Dict[str, str], needle: str) -> bool:
    n = needle.lower()
    for v in row.values():
        if v is None:
            continue
        if n in str(v).lower():
            return True
    return False


def _scan_csv(path: Path, ids: List[str],
              max_rows: int = 20_000,
              per_id_cap: int = 2) -> List[Tuple[str, Dict[str, str]]]:
    """Scan a CSV and return up to ``per_id_cap`` rows per identifier.

    Each row is attributed to ONE identifier — the most specific one it
    matches, where specificity = longest identifier string. That keeps
    a row containing both ``PART-3001`` and ``WO-99002`` recorded under
    ``WO-99002`` (the more discriminating ID), so frequent identifiers
    don't crowd out rare ones in the cited block.
    """
    counts: Dict[str, int] = {ident: 0 for ident in ids}
    hits: List[Tuple[str, Dict[str, str]]] = []
    ids_by_specificity = sorted(ids, key=lambda s: -len(s))
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if i > max_rows:
                    break
                for ident in ids_by_specificity:
                    if counts[ident] >= per_id_cap:
                        continue
                    if _matches_row(row, ident):
                        hits.append((ident, dict(row)))
                        counts[ident] += 1
                        break
                if all(c >= per_id_cap for c in counts.values()):
                    break
    except Exception:
        return hits
    return hits


def _scan_json(path: Path, ids: List[str],
               per_id_cap: int = 2) -> List[Tuple[str, Dict[str, str]]]:
    """Scan a JSON file laid out as a list of records (the common case
    for analyst JSON dumps — defects, customers, work orders, etc.)
    and return up to ``per_id_cap`` matches per identifier.
    Falls back to flattening a single-object JSON with a top-level
    list-shaped value (e.g. {"defects": [...]}). Best-effort — unusual
    nesting yields no hits, never an exception.
    """
    out: List[Tuple[str, Dict[str, str]]] = []
    counts: Dict[str, int] = {ident: 0 for ident in ids}
    ids_by_specificity = sorted(ids, key=lambda s: -len(s))
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            blob = json.load(f)
    except Exception:
        return out
    records: List[Dict] = []
    if isinstance(blob, list):
        records = [r for r in blob if isinstance(r, dict)]
    elif isinstance(blob, dict):
        for v in blob.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                records = [r for r in v if isinstance(r, dict)]
                break
    for rec in records:
        # Flatten one level so a nested {"row": {...}} still works.
        flat = {k: ("" if v is None else (json.dumps(v) if isinstance(v, (list, dict)) else str(v)))
                for k, v in rec.items()}
        for ident in ids_by_specificity:
            if counts[ident] >= per_id_cap:
                continue
            if _matches_row(flat, ident):
                out.append((ident, flat))
                counts[ident] += 1
                break
    return out


def _scan_xlsx(path: Path, ids: List[str],
               max_rows: int = 20_000,
               per_id_cap: int = 2) -> List[Tuple[str, Dict[str, str]]]:
    try:
        import openpyxl as _xl
    except Exception:
        return []
    out: List[Tuple[str, Dict[str, str]]] = []
    counts: Dict[str, int] = {ident: 0 for ident in ids}
    ids_by_specificity = sorted(ids, key=lambda s: -len(s))
    try:
        wb = _xl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return out
    for sh in wb.worksheets:
        it = sh.iter_rows(values_only=True)
        try:
            header = next(it)
        except StopIteration:
            continue
        headers = [
            (str(h).strip() if h is not None else f"col_{i+1}")
            for i, h in enumerate(header)
        ]
        for i, row in enumerate(it):
            if i > max_rows:
                break
            rowmap = {"__sheet__": sh.title}
            for hi, h in enumerate(headers):
                v = row[hi] if hi < len(row) else None
                rowmap[h] = "" if v is None else str(v)
            for ident in ids_by_specificity:
                if counts[ident] >= per_id_cap:
                    continue
                if _matches_row(rowmap, ident):
                    out.append((ident, rowmap))
                    counts[ident] += 1
                    break
    return out


def cite_rows(
    answer_text: str,
    cited_paths: Iterable[Path],
    *,
    max_rows_per_file: int = 12,
    rows_per_identifier: int = 2,
) -> str:
    """Return a Markdown 'Row-level citations' block for splicing under
    the LLM's answer. Empty string when nothing matches.

    `cited_paths` should be the file paths the LLM was given as
    [SOURCE #N] context. We scan ONLY those — never the rest of the
    vault — so we don't surface a row from a file the model never saw.
    """
    ids = extract_identifiers(answer_text)
    if not ids:
        return ""
    sections: List[str] = []
    for p in cited_paths:
        p = Path(p)
        if not p.exists():
            continue
        suf = p.suffix.lower()
        rows: List[Tuple[str, Dict[str, str]]] = []
        if suf in (".csv", ".tsv"):
            rows = _scan_csv(p, ids, per_id_cap=rows_per_identifier)
        elif suf in (".xlsx", ".xlsm"):
            rows = _scan_xlsx(p, ids, per_id_cap=rows_per_identifier)
        elif suf == ".json":
            rows = _scan_json(p, ids, per_id_cap=rows_per_identifier)
        else:
            continue
        if not rows:
            continue
        rows = rows[:max_rows_per_file]
        section = [f"\n**{p.name}**"]
        for ident, row in rows:
            sheet = row.pop("__sheet__", None)
            pairs = ", ".join(
                f"{k}={v}" for k, v in row.items()
                if v and len(str(v)) < 80
            )
            section.append(
                f"- `{ident}`" + (f" (sheet: *{sheet}*)" if sheet else "")
                + f" — {pairs}"
            )
        sections.append("\n".join(section))
    if not sections:
        return ""
    return "\n\n---\n**Row-level citations** (identifiers detected in the answer)\n" + "\n".join(sections)
