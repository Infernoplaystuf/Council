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

  1. StatsCache  (per-folder CSV shards)
       ONE shard per data folder, mirroring the tree:
       ``.stats_cache/<folder-rel-path>/columns.csv`` — one row per
       (file, column), a plain readable stats table:
         file,rows,column,dtype,count,missing,min,max,mean,std,sum,
         n_unique,top,mtime,self_summary
       Each row carries the file's mtime, so "process unprocessed files"
       is: skip any file whose shard rows match its current mtime. New
       files are picked up on the next sweep; a changed file (new mtime)
       is recomputed; a lookup loads only the queried FOLDER's shard
       (partial load); processing a folder rewrites only that folder's
       shard (bounded write churn). The ``self_summary`` cell flags a
       file that carries its OWN summary (a "Total"/"Mean" footer row,
       or a column literally named min/max/mean/total/…). No full
       re-index; CSV is compact and human/model-readable.

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

# CSV-shard schema (one row per file × column). 'rows' is the file's
# total row count (repeated per row); per-column 'count' is non-null.
_SHARD_FIELDS = [
    "file", "rows", "column", "dtype", "count", "missing",
    "min", "max", "mean", "std", "sum", "n_unique", "top",
    "mtime", "self_summary",
]


def _encode_self_summary(sd: Dict[str, Any]) -> str:
    """Pack self-describing info into one compact, CSV-safe cell."""
    parts = []
    cols = sd.get("summary_columns") or []
    rows = sd.get("summary_rows") or []
    if cols:
        parts.append("scols:" + "|".join(str(c) for c in cols))
    if rows:
        parts.append("srows:" + "|".join(
            f"{r.get('label', '')}@{r.get('row_index', '')}" for r in rows))
    return ";".join(parts)


def _decode_self_summary(cell: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"summary_columns": [], "summary_rows": []}
    for part in str(cell or "").split(";"):
        part = part.strip()
        if part.startswith("scols:"):
            out["summary_columns"] = [c for c in part[6:].split("|") if c]
        elif part.startswith("srows:"):
            for item in part[6:].split("|"):
                if "@" in item:
                    lbl, _, idx = item.rpartition("@")
                    try:
                        out["summary_rows"].append(
                            {"label": lbl, "row_index": int(idx)})
                    except ValueError:
                        out["summary_rows"].append({"label": lbl, "row_index": -1})
    return out


class StatsCache:
    """Per-folder CSV-shard store of column stats. Each data folder gets
    ONE shard — ``<vault>/.stats_cache/<folder-rel-path>/columns.csv`` —
    with one row per (file, column). Benefits over a single file:

      • Partial load — a lookup touches only the queried folder's shard.
      • Bounded write churn — processing files in a folder rewrites only
        that folder's shard, not the whole vault's cache.
      • Human- and model-readable: a shard IS a tidy stats table.

    Incremental & mtime-keyed: each row carries the file's mtime, so a
    file is 'processed' only while its rows match the file's current
    mtime. New files are picked up on the next sweep; a changed file is
    recomputed; nothing else is touched. Public API (get / is_current /
    process_unprocessed / stats) is unchanged from the prior version."""

    DIRNAME = ".stats_cache"
    SHARD_NAME = "columns.csv"

    def __init__(self, vault_dir: Optional[Any] = None) -> None:
        self.root = Path(vault_dir) if vault_dir is not None else _vault_root()
        self.dir = self.root / self.DIRNAME
        self._lock = threading.Lock()
        # In-process memo of loaded shards: folder-key -> {basename: row-dict}
        self._shards: Dict[str, Dict[str, List[Dict[str, str]]]] = {}

    # ---- shard location + io ----
    def _shard_path(self, folder: Path) -> Path:
        """Mirror the folder tree under .stats_cache/. Folders outside the
        vault root fall back to a hash-named shard so nothing escapes."""
        folder = Path(folder)
        try:
            rel = folder.resolve().relative_to(self.root.resolve())
            return self.dir / rel / self.SHARD_NAME
        except Exception:
            h = hashlib.sha1(str(folder.resolve()).encode("utf-8")).hexdigest()[:16]
            return self.dir / ("_ext_" + h) / self.SHARD_NAME

    def _load_shard(self, folder: Path) -> Dict[str, List[Dict[str, str]]]:
        """Return {basename: [row-dict, ...]} for a folder's shard."""
        import csv
        key = str(self._shard_path(folder))
        if key in self._shards:
            return self._shards[key]
        by_file: Dict[str, List[Dict[str, str]]] = {}
        p = self._shard_path(folder)
        if p.exists():
            try:
                with p.open("r", encoding="utf-8", newline="") as fh:
                    for row in csv.DictReader(fh):
                        by_file.setdefault(row.get("file", ""), []).append(row)
            except Exception:
                by_file = {}
        self._shards[key] = by_file
        return by_file

    def _write_shard(self, folder: Path,
                     by_file: Dict[str, List[Dict[str, str]]]) -> None:
        import csv
        p = self._shard_path(folder)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".csv.tmp")
            with tmp.open("w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=_SHARD_FIELDS)
                w.writeheader()
                for rows in by_file.values():
                    for r in rows:
                        w.writerow({k: r.get(k, "") for k in _SHARD_FIELDS})
            tmp.replace(p)
        except Exception:
            pass

    # ---- stats <-> rows ----
    @staticmethod
    def _stats_to_rows(path: Path, stats: Dict[str, Any],
                       mtime: float) -> List[Dict[str, str]]:
        ss = _encode_self_summary(stats.get("self_describing") or {})
        rows: List[Dict[str, str]] = []
        for col, s in (stats.get("column_stats") or {}).items():
            rows.append({
                "file": path.name,
                "rows": str(stats.get("rows", "")),
                "column": str(col),
                "dtype": s.get("dtype", ""),
                "count": str(s.get("count", "")),
                "missing": str(s.get("missing", "")),
                "min": "" if s.get("min") is None else str(s.get("min")),
                "max": "" if s.get("max") is None else str(s.get("max")),
                "mean": "" if s.get("mean") is None else str(s.get("mean")),
                "std": "" if s.get("std") is None else str(s.get("std")),
                "sum": "" if s.get("sum") is None else str(s.get("sum")),
                "n_unique": "" if s.get("n_unique") is None else str(s.get("n_unique")),
                "top": "" if s.get("top") is None else str(s.get("top")),
                "mtime": repr(float(mtime)),
                "self_summary": ss,
            })
        if not rows:    # a file with no parseable columns still gets a marker row
            rows.append({"file": path.name, "rows": str(stats.get("rows", "")),
                         "column": "", "dtype": "", "count": "", "missing": "",
                         "min": "", "max": "", "mean": "", "std": "", "sum": "",
                         "n_unique": "", "top": "", "mtime": repr(float(mtime)),
                         "self_summary": ss})
        return rows

    @staticmethod
    def _rows_to_stats(rows: List[Dict[str, str]]) -> Dict[str, Any]:
        def _f(v):
            return None if v in ("", None) else float(v)
        def _i(v):
            return None if v in ("", None) else int(float(v))
        cs: Dict[str, Any] = {}
        rows_total = 0
        for r in rows:
            col = r.get("column", "")
            if r.get("rows"):
                try:
                    rows_total = int(float(r["rows"]))
                except ValueError:
                    pass
            if not col:
                continue
            if r.get("dtype") == "numeric":
                cs[col] = {"dtype": "numeric", "count": _i(r.get("count")),
                           "missing": _i(r.get("missing")), "min": _f(r.get("min")),
                           "max": _f(r.get("max")), "mean": _f(r.get("mean")),
                           "std": _f(r.get("std")), "sum": _f(r.get("sum"))}
            else:
                cs[col] = {"dtype": "text", "count": _i(r.get("count")),
                           "missing": _i(r.get("missing")),
                           "n_unique": _i(r.get("n_unique")),
                           "top": (r.get("top") or None)}
        sd = _decode_self_summary(rows[0].get("self_summary", "")) if rows else {}
        return {"rows": rows_total, "columns": len(cs),
                "column_stats": cs, "self_describing": sd}

    # ---- public API (unchanged signatures) ----
    def is_current(self, path: Any) -> bool:
        """True iff the file's shard rows match its CURRENT mtime."""
        p = Path(path)
        rows = self._load_shard(p.parent).get(p.name)
        if not rows:
            return False
        try:
            return abs(float(rows[0].get("mtime", "nan")) - _mtime(p)) < 1e-6
        except (ValueError, TypeError):
            return False

    def get(self, path: Any, *, compute_if_missing: bool = True,
            sep: str = ",", save: bool = True) -> Optional[Dict[str, Any]]:
        """Return cached stats for a file, recomputing if missing/stale.
        ``save=False`` updates the in-memory shard but defers the disk
        write (process_unprocessed flushes once per folder)."""
        p = Path(path)
        by_file = self._load_shard(p.parent)
        rows = by_file.get(p.name)
        if rows and self.is_current(p):
            return self._rows_to_stats(rows)
        if not compute_if_missing:
            return None
        mt = _mtime(p)
        stats = compute_column_stats(p, sep=sep)
        with self._lock:
            by_file[p.name] = self._stats_to_rows(p, stats, mt)
            if save:
                self._write_shard(p.parent, by_file)
        return stats

    def process_unprocessed(self, folders: Any, *,
                            list_files=None,
                            limit: Optional[int] = None,
                            on_progress=None) -> Dict[str, int]:
        """Compute + store stats for every CSV under ``folders`` not
        already current. Groups by folder and writes each folder's shard
        ONCE (bounded write churn). ``list_files`` may be
        vault_analyst.list_csv_files; otherwise we glob *.csv."""
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
        # NEVER process our own shard files — they're .csv under the
        # vault, so a walk that includes .stats_cache would try to cache
        # the cache (wrong counts + a shard-of-a-shard). Drop anything
        # living under the cache dir.
        cache_dir = self.dir.resolve()
        def _under_cache(f: Path) -> bool:
            try:
                return cache_dir in f.resolve().parents
            except Exception:
                return False
        files = [f for f in files if not _under_cache(f)]
        todo = [f for f in files if not self.is_current(f)]
        if limit is not None:
            todo = todo[:limit]

        # Group the work by parent folder so each shard is written once.
        from collections import OrderedDict
        by_folder: "OrderedDict[str, List[Path]]" = OrderedDict()
        for f in todo:
            by_folder.setdefault(str(f.parent), []).append(f)

        processed = 0
        total = len(todo)
        for folder_str, fs in by_folder.items():
            folder = Path(folder_str)
            shard = self._load_shard(folder)
            for f in fs:
                self.get(f, save=False)      # updates the in-memory shard
                processed += 1
                if on_progress:
                    try:
                        on_progress(processed, total, f.name)
                    except Exception:
                        pass
            with self._lock:
                self._write_shard(folder, shard)   # one write per folder
        return {"seen": len(files), "processed": processed,
                "already_current": len(files) - len(todo)}

    def stats(self) -> Dict[str, Any]:
        shards = list(self.dir.rglob(self.SHARD_NAME)) if self.dir.exists() else []
        n_files = 0
        for sp in shards:
            try:
                # rows minus header, then distinct files ~ approximated by
                # counting; cheap enough for a status line.
                with sp.open("r", encoding="utf-8") as fh:
                    files = {ln.split(",", 1)[0] for ln in fh.read().splitlines()[1:] if ln}
                n_files += len(files)
            except Exception:
                pass
        return {"files_cached": n_files, "shards": len(shards),
                "cache_dir": str(self.dir)}


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
