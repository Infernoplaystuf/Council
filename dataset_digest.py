"""
dataset_digest.py — a compact, model-free overview of everything in data_in.

This is the grounding layer for "make the model an expert on the data it's fed":
instead of fine-tuning weights (impractical offline + prone to hallucinating
values), we give the model a reliable, always-current map of the WHOLE dataset
on every data question, and let the existing retrieval pull the details. Built
from vault_analyst.folder_data_summary (one row per file: type / rows / cols /
column names), cached by a cheap (file-count, latest-mtime) signature so it only
rebuilds when data_in actually changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Tuple

# Module-level cache: rebuild only when data_in changes.
_CACHE: dict = {"sig": None, "text": ""}


def _data_root(vault_dir: Any) -> Path:
    try:
        import data_index
        return data_index.input_dir(Path(vault_dir))
    except Exception:
        return Path(vault_dir) / "data_in"


def _signature(root: Path) -> Tuple[int, float]:
    """Cheap change-detector: (file count, latest mtime) under root."""
    n = 0
    latest = 0.0
    try:
        for p in root.rglob("*"):
            if p.is_file():
                n += 1
                try:
                    latest = max(latest, p.stat().st_mtime)
                except Exception:
                    pass
    except Exception:
        pass
    return (n, latest)


def build_digest(vault_dir: Any, *, max_files: int = 200,
                 max_chars: int = 4000) -> str:
    """A plain-text overview of data_in: counts by type, then one line per file
    with its type, dimensions, and column names. Model-free; bounded."""
    root = _data_root(vault_dir)
    if not root.exists():
        return ""
    try:
        import vault_analyst as va
        df = va.folder_data_summary(root, recursive=True, max_files=max_files)
    except Exception:
        return ""
    if df is None or len(df) == 0:
        return ""
    lines = []
    try:
        by_type = df["type"].value_counts().to_dict()
        counts = ", ".join(f"{v} {k}" for k, v in by_type.items())
    except Exception:
        counts = f"{len(df)} files"
    lines.append(f"{len(df)} data file(s) in data_in — {counts}.")
    lines.append("Files (name — type, rows x cols — columns):")
    for _, r in df.iterrows():
        name = str(r.get("file", "?"))
        typ = str(r.get("type", "?"))
        dims = ""
        try:
            rows, cols = r.get("rows"), r.get("columns")
            if rows is not None and cols is not None and str(rows) != "nan":
                dims = f", {int(float(rows))}x{int(float(cols))}"
        except Exception:
            pass
        colnames = r.get("column_names") or ""
        cn = str(colnames)[:140] if colnames else ""
        lines.append(f"  - {name} ({typ}{dims})" + (f" — {cn}" if cn else ""))
        if sum(len(x) for x in lines) > max_chars:
            lines.append("  ... (more files not shown)")
            break
    return "\n".join(lines)[:max_chars]


def get_digest(vault_dir: Any, *, force: bool = False,
               max_chars: int = 4000) -> str:
    """Cached digest — rebuilds only when data_in changed since last call."""
    root = _data_root(vault_dir)
    sig = _signature(root)
    if not force and _CACHE.get("sig") == sig and _CACHE.get("text"):
        return _CACHE["text"][:max_chars]
    text = build_digest(vault_dir, max_chars=max_chars)
    _CACHE["sig"] = sig
    _CACHE["text"] = text
    return text
