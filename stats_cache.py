"""
stats_cache.py — incremental, mtime-keyed precompute of common column
statistics, plus a save/retrieve cache for arbitrary query reports.

Why
---
Most data questions a user asks about a vault CSV are the same handful
of statistics — count, missing, min, max, mean, std, sum, distinct
values. Computing them on every query re-reads the file each time. This
module computes them ONCE per file (the first time the file is seen),
stores a tiny JSON record, and serves it instantly thereafter.

Two caches, both under ``<vault>/.stats_cache/``:

  1. StatsCache  (column_stats.json)
       One record per file: per-column stats + whether the file already
       carries its OWN summary (a "Total"/"Mean" footer row, or a column
       literally named min/max/mean/total/…). Keyed by (path, mtime), so
       "process unprocessed files" is just: skip any file whose
       (path, mtime) is already recorded. New files added later are
       picked up on the next sweep; a changed file (new mtime) is
       recomputed. No full re-index.

  2. QueryReportCache  (query_reports.jsonl)
       Save the result of ANY computed report — e.g. "stats of just the
       first 200 rows of folder X" — keyed by a normalised query string
       plus the (path, mtime) fingerprint of its inputs. Not computed by
       default; once a user asks for it and it's computed, it's stored
       and retrievable instantly until an input file changes.

Design choices
--------------
  • Memory-bounded precompute. Stats are accumulated by STREAMING the
    file in chunks (running count/min/max/sum/sumsq), so a one-time
    pass over a multi-GB CSV never materialises the whole file — the
    same lesson as folder_data_summary's OOM fix.
  • Exact where it's cheap to stream: count, missing, min, max, mean,
    std (sample), sum. Distinct-value counts are tracked up to a cap
    (then reported as "N+") so cardinality can't blow memory. Median /
    arbitrary quantiles are NOT precomputed (they need the full column
    in memory); the analyst computes those on demand.
  • Tiny on disk: ~1–2 KB JSON per file. 1000 files ≈ a couple of MB.
  • Honest staleness: everything is keyed on file mtime. A file edited
    without its mtime changing (rare) would serve a stale record until
    the next real change.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Tunables
_CHUNK_ROWS = 50_000          # rows per streaming chunk
_UNIQUE_CAP = 10_000          # stop counting distinct values past this
_SELF_DESC_SCAN_ROWS = 50     # rows to scan head+tail for summary rows
_SUMMARY_COL_NAMES = {
    "min", "max", "mean", "average", "avg", "median", "std", "stddev",
    "sum", "total", "totals", "count", "variance", "var",
}
_SUMMARY_ROW_LABELS = {
    "total", "totals", "sum", "mean", "average", "avg", "median",
    "grand total", "subtotal", "std", "count", "summary",
}


def _vault_root() -> Path:
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".council" / "vault"


def _mtime(path: Any) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return -1.0


# ============================================================
# Pure computation
# ============================================================

def detect_self_describing(path: Any, *, sep: str = ",") -> Dict[str, Any]:
    """Look for summary info the file ALREADY carries, so the analyst can
    point at it instead of recomputing — and so a footer like a 'Total'
    row isn't mistaken for data.

    Returns {"summary_columns": [...], "summary_rows": [{row, label}...]}.
    Cheap: reads the header + the first/last handful of rows only.
    """
    import pandas as pd
    out: Dict[str, Any] = {"summary_columns": [], "summary_rows": []}
    try:
        head = pd.read_csv(path, sep=sep, nrows=_SELF_DESC_SCAN_ROWS,
                           dtype=str, keep_default_na=False)
    except Exception:
        return out
    # Columns whose NAME announces a statistic.
    for c in head.columns:
        if str(c).strip().lower() in _SUMMARY_COL_NAMES:
            out["summary_columns"].append(str(c))
    # Rows whose first cell is a summary label (footer/total rows). We
    # check the head sample; a full-file tail scan would need the whole
    # file, so this catches header-adjacent and small-file summaries.
    if len(head.columns):
        first_col = head.columns[0]
        for i, val in enumerate(head[first_col].tolist()):
            if str(val).strip().lower() in _SUMMARY_ROW_LABELS:
                out["summary_rows"].append(
                    {"row_index": int(i), "label": str(val).strip()})
    return out


def compute_column_stats(path: Any, *, sep: str = ",") -> Dict[str, Any]:
    """Stream a CSV in chunks and return exact, memory-bounded column
    stats. Never holds the whole file in memory.

    Shape:
        {
          "rows": int, "columns": int,
          "column_stats": {
             colname: {
               "dtype": "numeric"|"text",
               "count": int, "missing": int,
               # numeric:
               "min": float, "max": float, "mean": float,
               "std": float, "sum": float,
               # text:
               "n_unique": int, "n_unique_capped": bool, "top": str,
             }, ...
          },
          "self_describing": {...},   # from detect_self_describing
          "error": str (only on failure),
        }
    """
    import pandas as pd
    import numpy as np

    num: Dict[str, Dict[str, float]] = {}     # numeric accumulators
    txt: Dict[str, Dict[str, Any]] = {}       # text accumulators
    columns: List[str] = []
    total_rows = 0

    try:
        reader = pd.read_csv(path, sep=sep, chunksize=_CHUNK_ROWS,
                             low_memory=False)
        for chunk in reader:
            if not columns:
                columns = [str(c) for c in chunk.columns]
            total_rows += len(chunk)
            for c in chunk.columns:
                col = chunk[c]
                name = str(c)
                is_num = pd.api.types.is_numeric_dtype(col)
                missing = int(col.isna().sum())
                if is_num:
                    a = num.setdefault(name, {
                        "count": 0, "missing": 0, "sum": 0.0,
                        "sumsq": 0.0, "min": math.inf, "max": -math.inf})
                    vals = col.dropna().to_numpy(dtype="float64", copy=False)
                    a["missing"] += missing
                    if vals.size:
                        a["count"] += int(vals.size)
                        a["sum"] += float(vals.sum())
                        a["sumsq"] += float(np.square(vals).sum())
                        a["min"] = min(a["min"], float(vals.min()))
                        a["max"] = max(a["max"], float(vals.max()))
                else:
                    a = txt.setdefault(name, {
                        "count": 0, "missing": 0,
                        "uniques": set(), "capped": False, "freq": {}})
                    a["missing"] += missing
                    nn = col.dropna().astype(str)
                    a["count"] += int(nn.size)
                    if not a["capped"]:
                        for v in nn:
                            if len(a["uniques"]) < _UNIQUE_CAP:
                                a["uniques"].add(v)
                            else:
                                a["capped"] = True
                                break
                    # cheap mode tracking (head of value counts in chunk)
                    vc = nn.value_counts()
                    for v, n in vc.head(20).items():
                        a["freq"][v] = a["freq"].get(v, 0) + int(n)
    except Exception as exc:
        return {"rows": total_rows, "columns": len(columns),
                "column_stats": {}, "self_describing": {},
                "error": f"{type(exc).__name__}: {exc}"}

    col_stats: Dict[str, Any] = {}
    for name, a in num.items():
        n = a["count"]
        mean = (a["sum"] / n) if n else None
        if n > 1:
            var = (a["sumsq"] - n * mean * mean) / (n - 1)
            std = math.sqrt(var) if var > 0 else 0.0
        else:
            std = None
        col_stats[name] = {
            "dtype": "numeric",
            "count": n, "missing": a["missing"],
            "min": (a["min"] if n else None),
            "max": (a["max"] if n else None),
            "mean": (round(mean, 6) if mean is not None else None),
            "std":  (round(std, 6) if std is not None else None),
            "sum":  round(a["sum"], 6),
        }
    for name, a in txt.items():
        top = max(a["freq"].items(), key=lambda kv: kv[1])[0] if a["freq"] else None
        col_stats[name] = {
            "dtype": "text",
            "count": a["count"], "missing": a["missing"],
            "n_unique": len(a["uniques"]),
            "n_unique_capped": bool(a["capped"]),
            "top": (str(top)[:80] if top is not None else None),
        }

    return {
        "rows": total_rows,
        "columns": len(columns),
        "column_stats": col_stats,
        "self_describing": detect_self_describing(path, sep=sep),
    }


# ============================================================
# Persistent per-file stats cache (incremental)
# ============================================================

class StatsCache:
    """mtime-keyed per-file column-stats store. ``process_unprocessed``
    computes stats only for files not already recorded at their current
    mtime — so newly-added files are handled incrementally and nothing
    is recomputed needlessly."""

    DIRNAME = ".stats_cache"
    FILENAME = "column_stats.json"

    def __init__(self, vault_dir: Optional[Any] = None) -> None:
        root = Path(vault_dir) if vault_dir is not None else _vault_root()
        self.dir = root / self.DIRNAME
        self.path = self.dir / self.FILENAME
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                return d if isinstance(d, dict) else {}
        except Exception:
            pass
        return {}

    def _save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)        # atomic on the same filesystem
        except Exception:
            pass

    @staticmethod
    def _key(path: Any) -> str:
        try:
            return str(Path(path).resolve())
        except Exception:
            return str(path)

    def is_current(self, path: Any) -> bool:
        """True iff we already have stats for this file at its CURRENT
        mtime (i.e. it's 'processed' and unchanged)."""
        entry = self._data.get(self._key(path))
        return bool(entry) and entry.get("mtime") == _mtime(path)

    def get(self, path: Any, *, compute_if_missing: bool = True,
            sep: str = ",", save: bool = True) -> Optional[Dict[str, Any]]:
        """Return the cached stats for a file, recomputing if missing or
        stale (unless compute_if_missing=False).

        ``save=False`` updates the in-memory record but skips the disk
        write — used by process_unprocessed to write ONCE at the end of a
        sweep instead of rewriting the whole file per processed file
        (which was O(N²) write volume on a cold sweep)."""
        key = self._key(path)
        mt = _mtime(path)
        entry = self._data.get(key)
        if entry and entry.get("mtime") == mt:
            return entry["stats"]
        if not compute_if_missing:
            return None
        stats = compute_column_stats(path, sep=sep)
        with self._lock:
            self._data[key] = {"mtime": mt, "stats": stats}
            if save:
                self._save()
        return stats

    def process_unprocessed(self, folders: Any, *,
                            list_files=None,
                            limit: Optional[int] = None,
                            on_progress=None) -> Dict[str, int]:
        """Compute + store stats for every CSV under ``folders`` that
        isn't already current. Returns counts. ``list_files`` lets the
        caller pass vault_analyst.list_csv_files; if None we glob *.csv.
        """
        if list_files is not None:
            files = [Path(p) for p in list_files(folders)]
        else:
            files = []
            roots = folders if isinstance(folders, (list, tuple)) else [folders]
            for r in roots:
                try:
                    files.extend(Path(r).rglob("*.csv"))
                except Exception:
                    continue
        todo = [f for f in files if not self.is_current(f)]
        if limit is not None:
            todo = todo[:limit]
        processed = 0
        for i, f in enumerate(todo, 1):
            # save=False: accumulate in memory, write once at the end —
            # avoids rewriting the whole cache file per processed file.
            self.get(f, save=False)
            processed += 1
            if on_progress:
                try:
                    on_progress(i, len(todo), f.name)
                except Exception:
                    pass
        if processed:
            with self._lock:
                self._save()                # single write for the sweep
        return {"seen": len(files), "processed": processed,
                "already_current": len(files) - len(todo)}

    def stats(self) -> Dict[str, Any]:
        return {"files_cached": len(self._data),
                "cache_path": str(self.path)}


# ============================================================
# Query-report cache (save/retrieve arbitrary computed reports)
# ============================================================

class QueryReportCache:
    """Save the result of any computed report and retrieve it later.

    A report is keyed on (normalised query text + the (path, mtime)
    fingerprint of its input files), so it stays valid until an input
    changes. Append-only JSONL; newest entry for a key wins."""

    DIRNAME = ".stats_cache"
    FILENAME = "query_reports.jsonl"

    def __init__(self, vault_dir: Optional[Any] = None) -> None:
        root = Path(vault_dir) if vault_dir is not None else _vault_root()
        self.dir = root / self.DIRNAME
        self.path = self.dir / self.FILENAME
        self._lock = threading.Lock()

    @staticmethod
    def make_key(query: str, input_paths: Any) -> str:
        """Stable digest of the query + its inputs' mtimes."""
        q = " ".join(str(query or "").split()).lower()
        fp = []
        paths = input_paths if isinstance(input_paths, (list, tuple)) else [input_paths]
        for p in sorted(str(x) for x in paths):
            fp.append(f"{p}:{_mtime(p)}")
        h = hashlib.sha1(("|".join([q] + fp)).encode("utf-8")).hexdigest()
        return h[:20]

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.path.exists():
            return None
        hit = None
        try:
            with self._lock, self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("key") == key:
                        hit = rec            # keep scanning → newest wins
        except Exception:
            return None
        return hit

    def put(self, key: str, *, query: str, report: Any,
            meta: Optional[Dict[str, Any]] = None) -> None:
        rec = {"key": key, "query": str(query), "report": report}
        if meta:
            rec["meta"] = dict(meta)
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def get_or_compute(self, query: str, input_paths: Any, compute) -> Any:
        """Return the saved report for (query, inputs) or compute+save it.
        ``compute`` is a zero-arg callable returning a JSON-able report."""
        key = self.make_key(query, input_paths)
        hit = self.get(key)
        if hit is not None:
            return hit["report"]
        report = compute()
        self.put(key, query=query, report=report)
        return report
