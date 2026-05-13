"""
Vault analyst — pandas-backed helper functions over the user's vault, plus a
sandboxed runner that lets the council compute real answers instead of guessing
from sample rows.

The shape is taken from a local-RAG reference script:
  - A library of typed helper functions covering the most common CSV questions
    (count, average, std, sum, min/max, filter, groupby, correlation, etc.).
  - A code generator that asks the model to express a question as a single
    pandas expression assigning to `result_df`.
  - An AST + regex validator that blocks anything the council shouldn't run
    (network, filesystem writes, shells, eval/exec).
  - A locked-down `exec` with a restricted builtins map and a `safe_import`
    allowlist.

The council uses this from the Writer's pre-synthesis step: if the user is
asking something computational ("how many", "what percentage", "average X"),
the helpers run first and their result is injected as text the Writer can
quote directly.
"""

from __future__ import annotations

import ast
import io
import json
import re
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

try:
    import pandas as pd
except Exception as exc:  # pragma: no cover - imported at runtime
    raise RuntimeError(
        "vault_analyst requires pandas. Install with `conda install -c "
        "conda-forge pandas` in the council env."
    ) from exc

try:
    import numpy as np
except Exception:
    np = None  # numpy is optional but useful in user code


# ============================================================
# Path helpers
# ============================================================

def normalize_data_folders(data_folders: Any) -> List[Path]:
    """Accept str / Path / iterable; return a deduped list of existing dirs."""
    if data_folders is None:
        return []
    if isinstance(data_folders, (str, Path)):
        raw = [data_folders]
    else:
        raw = list(data_folders)

    folders: List[Path] = []
    seen: set[str] = set()
    for item in raw:
        try:
            p = Path(item).expanduser().resolve()
        except Exception:
            continue
        if p.is_dir() and str(p).lower() not in seen:
            folders.append(p)
            seen.add(str(p).lower())
    return folders


def is_path_inside_allowed_folders(path: str | Path, allowed_folders: Any) -> bool:
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        return False
    for folder in normalize_data_folders(allowed_folders):
        try:
            p.relative_to(folder)
            return True
        except ValueError:
            continue
    return False


def first_matching_root(path: Path, roots: Any) -> Optional[Path]:
    path = Path(path).expanduser().resolve()
    for root in normalize_data_folders(roots):
        try:
            path.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def safe_relative_path(path: Path, root: Optional[Path]) -> str:
    try:
        if root is not None:
            return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except Exception:
        pass
    return str(Path(path).resolve())


def _drop_protected(paths: List[Path], data_folder: Any) -> List[Path]:
    """Filter out any file under a protected vault subdir (conversation_logs etc.)."""
    try:
        from conversation_logger import is_protected_path
    except Exception:
        return paths
    folders = normalize_data_folders(data_folder)
    out: List[Path] = []
    for p in paths:
        protected = False
        for vd in folders:
            if is_protected_path(p, vd):
                protected = True
                break
        if not protected:
            out.append(p)
    return out


def list_csv_files(data_folder: Any, recursive: bool = True) -> List[Path]:
    """All CSVs under the given folder(s), deduped. Protected subdirs
    (conversation logs) are excluded — they're for the user only."""
    folders = normalize_data_folders(data_folder)
    out: List[Path] = []
    for folder in folders:
        if recursive:
            out.extend(sorted(folder.rglob("*.csv")))
        else:
            out.extend(sorted(folder.glob("*.csv")))

    deduped: dict[str, Path] = {}
    for path in out:
        try:
            deduped[str(path.resolve()).lower()] = path.resolve()
        except Exception:
            pass
    return _drop_protected(sorted(deduped.values()), folders)


_EXCEL_GLOBS = ("*.xlsx", "*.xls", "*.xlsm")


def list_excel_files(data_folder: Any, recursive: bool = True) -> List[Path]:
    """All Excel workbooks under the given folder(s), deduped."""
    folders = normalize_data_folders(data_folder)
    out: List[Path] = []
    for folder in folders:
        for pattern in _EXCEL_GLOBS:
            if recursive:
                out.extend(sorted(folder.rglob(pattern)))
            else:
                out.extend(sorted(folder.glob(pattern)))
    deduped: dict[str, Path] = {}
    for path in out:
        try:
            deduped[str(path.resolve()).lower()] = path.resolve()
        except Exception:
            pass
    return _drop_protected(sorted(deduped.values()), folders)


def list_data_files(data_folder: Any, recursive: bool = True) -> List[Path]:
    """CSV + Excel files combined — for helpers that want either."""
    return sorted(set(list_csv_files(data_folder, recursive=recursive))
                  | set(list_excel_files(data_folder, recursive=recursive)))


def read_table(path: Any, *, sheet: Optional[str] = None) -> pd.DataFrame:
    """Read a tabular file into a DataFrame. Supported formats:
       .csv / .tsv / .csv.gz / .xlsx / .xls / .xlsm / .parquet
    For Excel, `sheet` picks which tab (default = first sheet)."""
    p = Path(path)
    name = p.name.lower()
    suf = p.suffix.lower()
    if suf in (".xlsx", ".xls", ".xlsm"):
        return pd.read_excel(p, sheet_name=sheet if sheet else 0)
    if suf == ".tsv":
        return pd.read_csv(p, sep="\t")
    if suf == ".parquet":
        return pd.read_parquet(p)
    if name.endswith(".csv.gz") or (suf == ".gz" and p.stem.lower().endswith(".csv")):
        return pd.read_csv(p, compression="infer")
    return pd.read_csv(p)


def list_sqlite_files(data_folder: Any, recursive: bool = True) -> List[Path]:
    """All SQLite database files under the given folder(s), deduped."""
    folders = normalize_data_folders(data_folder)
    out: List[Path] = []
    patterns = ("*.db", "*.sqlite", "*.sqlite3")
    for folder in folders:
        for pat in patterns:
            if recursive:
                out.extend(sorted(folder.rglob(pat)))
            else:
                out.extend(sorted(folder.glob(pat)))
    deduped: dict[str, Path] = {}
    for path in out:
        try:
            deduped[str(path.resolve()).lower()] = path.resolve()
        except Exception:
            pass
    return _drop_protected(sorted(deduped.values()), folders)


def list_sqlite_tables(path: Any) -> List[str]:
    """Return the list of table names in a SQLite database (read-only)."""
    import sqlite3
    con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    try:
        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        return [r[0] for r in cur.fetchall() if r and r[0]]
    finally:
        con.close()


def read_sqlite_table(path: Any, table: str, *, limit: Optional[int] = None) -> pd.DataFrame:
    """Read a SQLite table into a DataFrame. Read-only connection so the
    analyst sandbox can never write back."""
    import sqlite3
    qname = '"' + str(table).replace('"', '""') + '"'
    sql = f"SELECT * FROM {qname}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(sql, con)
    finally:
        con.close()


def list_parquet_files(data_folder: Any, recursive: bool = True) -> List[Path]:
    """All Parquet files under the given folder(s), deduped."""
    folders = normalize_data_folders(data_folder)
    out: List[Path] = []
    for folder in folders:
        if recursive:
            out.extend(sorted(folder.rglob("*.parquet")))
        else:
            out.extend(sorted(folder.glob("*.parquet")))
    return _drop_protected(sorted({p.resolve() for p in out}), folders)


def read_excel_sheets(path: Any) -> Dict[str, pd.DataFrame]:
    """Read every sheet of an Excel workbook into a dict {sheet_name: df}."""
    p = Path(path)
    return pd.read_excel(p, sheet_name=None)


def excel_inventory(
    data_folder: Any,
    recursive: bool = True,
    max_files: Optional[int] = None,
) -> pd.DataFrame:
    """One row per (workbook, sheet): sheet name, row count, column count,
    column-list preview. Mirrors csv_inventory for Excel."""
    folders = normalize_data_folders(data_folder)
    rows: list[dict[str, Any]] = []
    xls = list_excel_files(folders, recursive=recursive)
    if max_files is not None:
        xls = xls[:max_files]
    for p in xls:
        root = first_matching_root(p, folders)
        try:
            xl = pd.ExcelFile(str(p))
        except Exception as exc:
            rows.append({
                "workbook":      p.name,
                "relative_path": safe_relative_path(p, root),
                "sheet":         "",
                "status":        f"open error: {exc}",
                "columns":       "",
                "column_count":  None,
                "row_count":     None,
            })
            continue
        for sname in xl.sheet_names:
            try:
                df = xl.parse(sname, nrows=5)
                rows.append({
                    "workbook":      p.name,
                    "relative_path": safe_relative_path(p, root),
                    "sheet":         sname,
                    "status":        "ok",
                    "columns":       ", ".join(map(str, df.columns)),
                    "column_count":  len(df.columns),
                    "row_count":     None,  # nrows=5 only; full count via separate helper
                })
            except Exception as exc:
                rows.append({
                    "workbook":      p.name,
                    "relative_path": safe_relative_path(p, root),
                    "sheet":         sname,
                    "status":        f"sheet read error: {exc}",
                    "columns":       "",
                    "column_count":  None,
                    "row_count":     None,
                })
    return pd.DataFrame(rows)


# ============================================================
# Column resolution (case- and substring-tolerant)
# ============================================================

def find_column_case_insensitive(df: pd.DataFrame, column_name: str) -> Optional[str]:
    target = str(column_name).strip().lower()
    for col in df.columns:
        if str(col).strip().lower() == target:
            return col
    return None


def find_columns_contains(df: pd.DataFrame, text: str) -> List[str]:
    target = str(text).strip().lower()
    return [col for col in df.columns if target in str(col).strip().lower()]


# ============================================================
# Per-CSV aggregate helpers
# Each returns a DataFrame with one row per input CSV.
# ============================================================

def csv_inventory(
    data_folder: Any,
    recursive: bool = True,
    max_files: Optional[int] = None,
) -> pd.DataFrame:
    folders = normalize_data_folders(data_folder)
    rows: list[dict[str, Any]] = []
    csvs = list_csv_files(folders, recursive=recursive)
    if max_files is not None:
        csvs = csvs[:max_files]

    for csv_path in csvs:
        root = first_matching_root(csv_path, folders)
        try:
            df_head = pd.read_csv(csv_path, nrows=5)
            rows.append({
                "csv": csv_path.name,
                "relative_path": safe_relative_path(csv_path, root),
                "root_folder": str(root) if root else "",
                "status": "ok",
                "columns": ", ".join(map(str, df_head.columns)),
                "column_count": len(df_head.columns),
            })
        except Exception as exc:
            rows.append({
                "csv": csv_path.name,
                "relative_path": safe_relative_path(csv_path, root),
                "root_folder": str(root) if root else "",
                "status": f"error: {exc}",
                "columns": "",
                "column_count": None,
            })
    return pd.DataFrame(rows)


def count_rows_per_csv(data_folder: Any, recursive: bool = True) -> pd.DataFrame:
    folders = normalize_data_folders(data_folder)
    rows: list[dict[str, Any]] = []
    for csv_path in list_csv_files(folders, recursive=recursive):
        root = first_matching_root(csv_path, folders)
        try:
            df = pd.read_csv(csv_path)
            rows.append({
                "csv": csv_path.name,
                "relative_path": safe_relative_path(csv_path, root),
                "status": "ok",
                "row_count": len(df),
                "column_count": len(df.columns),
            })
        except Exception as exc:
            rows.append({
                "csv": csv_path.name,
                "relative_path": safe_relative_path(csv_path, root),
                "status": f"error: {exc}",
                "row_count": None,
                "column_count": None,
            })
    return pd.DataFrame(rows)


def _numeric_aggregate_per_csv(
    data_folder: Any,
    column_name: str,
    *,
    op: str,
    exclude_zero: bool,
    recursive: bool,
) -> pd.DataFrame:
    folders = normalize_data_folders(data_folder)
    rows: list[dict[str, Any]] = []
    for csv_path in list_csv_files(folders, recursive=recursive):
        root = first_matching_root(csv_path, folders)
        try:
            df = pd.read_csv(csv_path)
            actual_col = find_column_case_insensitive(df, column_name)
            if actual_col is None:
                rows.append({
                    "csv": csv_path.name,
                    "relative_path": safe_relative_path(csv_path, root),
                    "status": f"missing column: {column_name}",
                    "column_used": None,
                    "value": None,
                    "rows_used": 0,
                    "total_rows": len(df),
                })
                continue

            values = pd.to_numeric(df[actual_col], errors="coerce")
            mask = values.notna()
            if exclude_zero:
                mask = mask & (values != 0)
            filtered = values[mask]

            if op == "mean":
                value = float(filtered.mean()) if filtered.size else None
            elif op == "std":
                value = float(filtered.std()) if filtered.size >= 2 else None
            elif op == "sum":
                value = float(filtered.sum()) if filtered.size else 0.0
            elif op == "min":
                value = float(filtered.min()) if filtered.size else None
            elif op == "max":
                value = float(filtered.max()) if filtered.size else None
            else:
                value = None

            rows.append({
                "csv": csv_path.name,
                "relative_path": safe_relative_path(csv_path, root),
                "status": "ok",
                "column_used": actual_col,
                "value": value,
                "rows_used": int(filtered.count()),
                "total_rows": len(df),
            })
        except Exception as exc:
            rows.append({
                "csv": csv_path.name,
                "relative_path": safe_relative_path(csv_path, root),
                "status": f"error: {exc}",
                "column_used": None,
                "value": None,
                "rows_used": 0,
                "total_rows": None,
            })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.rename(columns={"value": op})
    return out


def average_numeric_column_per_csv(data_folder, column_name, *, exclude_zero=False, recursive=True):
    return _numeric_aggregate_per_csv(data_folder, column_name, op="mean",
                                       exclude_zero=exclude_zero, recursive=recursive)


def std_numeric_column_per_csv(data_folder, column_name, *, exclude_zero=False, recursive=True):
    return _numeric_aggregate_per_csv(data_folder, column_name, op="std",
                                       exclude_zero=exclude_zero, recursive=recursive)


def sum_numeric_column_per_csv(data_folder, column_name, *, exclude_zero=False, recursive=True):
    return _numeric_aggregate_per_csv(data_folder, column_name, op="sum",
                                       exclude_zero=exclude_zero, recursive=recursive)


def min_max_numeric_column_per_csv(data_folder, column_name, *, exclude_zero=False, recursive=True):
    """Returns both min and max in the same frame."""
    mn = _numeric_aggregate_per_csv(data_folder, column_name, op="min",
                                     exclude_zero=exclude_zero, recursive=recursive)
    mx = _numeric_aggregate_per_csv(data_folder, column_name, op="max",
                                     exclude_zero=exclude_zero, recursive=recursive)
    if mn.empty:
        return mn
    merged = mn[["csv", "relative_path", "status", "column_used", "rows_used", "total_rows"]].copy()
    merged["min"] = mn["min"]
    merged["max"] = mx["max"]
    return merged


def numeric_summary_per_csv(
    data_folder: Any,
    column_name: Optional[str] = None,
    *,
    exclude_zero: bool = False,
    recursive: bool = True,
) -> pd.DataFrame:
    folders = normalize_data_folders(data_folder)
    rows: list[dict[str, Any]] = []
    for csv_path in list_csv_files(folders, recursive=recursive):
        root = first_matching_root(csv_path, folders)
        try:
            df = pd.read_csv(csv_path)
            if column_name:
                actual_col = find_column_case_insensitive(df, column_name)
                if actual_col is None:
                    rows.append({"csv": csv_path.name, "status": f"missing column: {column_name}"})
                    continue
                candidate_cols = [actual_col]
            else:
                candidate_cols = list(df.columns)

            for col in candidate_cols:
                values = pd.to_numeric(df[col], errors="coerce")
                mask = values.notna()
                if exclude_zero:
                    mask = mask & (values != 0)
                filtered = values[mask]
                if filtered.empty:
                    continue
                rows.append({
                    "csv": csv_path.name,
                    "relative_path": safe_relative_path(csv_path, root),
                    "column": col,
                    "count": int(filtered.count()),
                    "mean": float(filtered.mean()),
                    "median": float(filtered.median()),
                    "std": float(filtered.std()) if filtered.size >= 2 else None,
                    "min": float(filtered.min()),
                    "max": float(filtered.max()),
                })
        except Exception as exc:
            rows.append({"csv": csv_path.name, "status": f"error: {exc}"})
    return pd.DataFrame(rows)


def count_matching_rows_per_csv(
    data_folder: Any,
    column_name: str,
    *,
    equals: Any = None,
    contains: Optional[str] = None,
    case_sensitive: bool = False,
    recursive: bool = True,
) -> pd.DataFrame:
    folders = normalize_data_folders(data_folder)
    rows: list[dict[str, Any]] = []
    for csv_path in list_csv_files(folders, recursive=recursive):
        root = first_matching_root(csv_path, folders)
        try:
            df = pd.read_csv(csv_path)
            actual_col = find_column_case_insensitive(df, column_name)
            if actual_col is None:
                # also try substring match against header names
                similar = find_columns_contains(df, column_name)
                if similar:
                    actual_col = similar[0]
            if actual_col is None:
                rows.append({
                    "csv": csv_path.name,
                    "relative_path": safe_relative_path(csv_path, root),
                    "status": f"missing column: {column_name}",
                    "match_count": None,
                    "total_rows": len(df),
                })
                continue

            series = df[actual_col]
            if equals is not None:
                if case_sensitive:
                    mask = series.astype(str) == str(equals)
                else:
                    mask = series.astype(str).str.lower() == str(equals).lower()
            elif contains is not None:
                mask = series.astype(str).str.contains(
                    str(contains), case=case_sensitive, na=False, regex=False,
                )
            else:
                mask = pd.Series([False] * len(df), index=df.index)

            rows.append({
                "csv": csv_path.name,
                "relative_path": safe_relative_path(csv_path, root),
                "status": "ok",
                "column_used": actual_col,
                "match_count": int(mask.sum()),
                "total_rows": len(df),
                "match_pct": round(100 * mask.sum() / max(1, len(df)), 2),
            })
        except Exception as exc:
            rows.append({
                "csv": csv_path.name,
                "status": f"error: {exc}",
                "match_count": None,
                "total_rows": None,
            })
    return pd.DataFrame(rows)


def filter_rows_across_csvs(
    data_folder: Any,
    column_name: str,
    *,
    equals: Any = None,
    contains: Optional[str] = None,
    case_sensitive: bool = False,
    recursive: bool = True,
    max_rows_per_csv: Optional[int] = None,
) -> pd.DataFrame:
    folders = normalize_data_folders(data_folder)
    frames: list[pd.DataFrame] = []
    for csv_path in list_csv_files(folders, recursive=recursive):
        root = first_matching_root(csv_path, folders)
        try:
            df = pd.read_csv(csv_path)
            actual_col = find_column_case_insensitive(df, column_name)
            if actual_col is None:
                continue
            series = df[actual_col]
            if equals is not None:
                if case_sensitive:
                    mask = series.astype(str) == str(equals)
                else:
                    mask = series.astype(str).str.lower() == str(equals).lower()
            elif contains is not None:
                mask = series.astype(str).str.contains(
                    str(contains), case=case_sensitive, na=False, regex=False,
                )
            else:
                mask = pd.Series([False] * len(df), index=df.index)

            sub = df.loc[mask].copy()
            if max_rows_per_csv is not None:
                sub = sub.head(max_rows_per_csv)
            if not sub.empty:
                sub.insert(0, "source_csv", csv_path.name)
                sub.insert(1, "relative_path", safe_relative_path(csv_path, root))
                frames.append(sub)
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def groupby_mean_per_csv(
    data_folder: Any,
    group_col: str,
    value_col: str,
    *,
    exclude_zero: bool = False,
    recursive: bool = True,
) -> pd.DataFrame:
    folders = normalize_data_folders(data_folder)
    frames: list[pd.DataFrame] = []
    for csv_path in list_csv_files(folders, recursive=recursive):
        root = first_matching_root(csv_path, folders)
        try:
            df = pd.read_csv(csv_path)
            actual_group = find_column_case_insensitive(df, group_col)
            actual_value = find_column_case_insensitive(df, value_col)
            if actual_group is None or actual_value is None:
                frames.append(pd.DataFrame([{
                    "csv": csv_path.name,
                    "status": f"missing column(s): group={group_col}, value={value_col}",
                }]))
                continue

            values = pd.to_numeric(df[actual_value], errors="coerce")
            work = df.copy()
            work[actual_value] = values
            work = work[work[actual_value].notna()]
            if exclude_zero:
                work = work[work[actual_value] != 0]
            if work.empty:
                frames.append(pd.DataFrame([{
                    "csv": csv_path.name,
                    "status": "no valid numeric rows",
                }]))
                continue
            grouped = (
                work.groupby(actual_group, dropna=False)[actual_value]
                .agg(["count", "mean", "min", "max"])
                .reset_index()
            )
            grouped.insert(0, "csv", csv_path.name)
            grouped.insert(1, "relative_path", safe_relative_path(csv_path, root))
            grouped = grouped.rename(columns={actual_group: "group"})
            frames.append(grouped)
        except Exception as exc:
            frames.append(pd.DataFrame([{"csv": csv_path.name, "status": f"error: {exc}"}]))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ============================================================
# Text cleaning + date extraction
# ============================================================

def clean_string_column(
    df: pd.DataFrame,
    column: str,
    *,
    lowercase: bool = True,
    strip: bool = True,
    collapse_whitespace: bool = True,
    nfkc: bool = True,
) -> pd.DataFrame:
    """Normalize a string column in place: optional lowercase, strip
    whitespace, collapse runs of internal whitespace, and apply NFKC
    Unicode normalization. Returns the same df with the column updated."""
    col = column if column in df.columns else find_column_case_insensitive(df, column)
    if col is None:
        return df
    s = df[col].astype(str)
    if nfkc:
        import unicodedata as _ud
        s = s.map(lambda x: _ud.normalize("NFKC", x))
    if strip:
        s = s.str.strip()
    if collapse_whitespace:
        s = s.str.replace(r"\s+", " ", regex=True)
    if lowercase:
        s = s.str.lower()
    df[col] = s
    return df


def extract_dates(
    df: pd.DataFrame,
    column: str,
    *,
    new_column: Optional[str] = None,
    errors: str = "coerce",
) -> pd.DataFrame:
    """Parse a string column into pandas datetimes. By default writes
    the parsed value back into `column`; pass `new_column` to keep the
    original. Unparseable values become NaT."""
    col = column if column in df.columns else find_column_case_insensitive(df, column)
    if col is None:
        return df
    parsed = pd.to_datetime(df[col], errors=errors)
    target = new_column or col
    df[target] = parsed
    return df


# ============================================================
# Top-N / value-counts wrapper
# ============================================================

def top_n_per_csv(
    data_folder: Any,
    column: str,
    *,
    n: int = 10,
    recursive: bool = True,
) -> pd.DataFrame:
    """Top-N value counts for `column` in each CSV. Returns one long
    frame: csv, relative_path, rank, value, count."""
    folders = normalize_data_folders(data_folder)
    rows: list[dict[str, Any]] = []
    for csv_path in list_csv_files(folders, recursive=recursive):
        root = first_matching_root(csv_path, folders)
        try:
            df = pd.read_csv(csv_path)
        except Exception as exc:
            rows.append({
                "csv": csv_path.name,
                "relative_path": safe_relative_path(csv_path, root),
                "status": f"read error: {exc}",
            })
            continue
        col = find_column_case_insensitive(df, column)
        if col is None:
            rows.append({
                "csv": csv_path.name,
                "relative_path": safe_relative_path(csv_path, root),
                "status": f"missing column: {column}",
            })
            continue
        vc = df[col].astype(str).value_counts(dropna=False).head(int(n))
        for rank, (value, count) in enumerate(vc.items(), start=1):
            rows.append({
                "csv": csv_path.name,
                "relative_path": safe_relative_path(csv_path, root),
                "rank": rank,
                "value": str(value),
                "count": int(count),
            })
    return pd.DataFrame(rows)


# ============================================================
# Schema documentation
# ============================================================

def schema_doc_from_csv(path: Any) -> str:
    """Generate a Markdown schema doc from summarize_csv output.

    Useful for onboarding teammates onto a new dataset without writing
    docs by hand. Returns a Markdown string the caller can save."""
    p = Path(path)
    try:
        prof = summarize_csv(p)
    except Exception as exc:
        return f"# {p.name}\n\nCould not profile file: {exc}\n"
    if prof.empty:
        return f"# {p.name}\n\nEmpty or unreadable.\n"

    try:
        full_df = pd.read_csv(p, nrows=1)  # for total-column count, not values
        n_cols = len(full_df.columns)
    except Exception:
        n_cols = len(prof)
    try:
        nrows_total = int(prof["non_null"].max() + prof["null_pct"].max() / 100 * prof["non_null"].max())
    except Exception:
        nrows_total = None

    md: list[str] = [
        f"# {p.name}",
        "",
        f"_Schema documentation, auto-generated from `summarize_csv`._",
        "",
        f"- Path: `{p}`",
        f"- Columns: {n_cols}",
    ]
    if nrows_total:
        md.append(f"- Approx rows: {nrows_total}")
    md.append("")
    md.append("| Column | Type | Non-null | Null % | Unique | Notes |")
    md.append("|---|---|---:|---:|---:|---|")
    for _, row in prof.iterrows():
        notes_parts = []
        if row.get("mean") is not None:
            try:
                notes_parts.append(f"mean={float(row['mean']):.2f}")
                notes_parts.append(f"min={row['min']}")
                notes_parts.append(f"max={row['max']}")
            except Exception:
                pass
        elif row.get("top_values"):
            notes_parts.append("top: " + str(row["top_values"]))
        md.append("| `{c}` | {dt} | {nn} | {npct} | {u} | {notes} |".format(
            c=row["column"],
            dt=row["dtype"],
            nn=row["non_null"],
            npct=row["null_pct"],
            u=row["unique"],
            notes=", ".join(notes_parts),
        ))
    md.append("")
    return "\n".join(md)


# ============================================================
# Data-quality checks
# ============================================================

def detect_data_quality_issues(df: pd.DataFrame) -> pd.DataFrame:
    """Run a battery of common data-quality checks against `df` and
    return a one-row-per-issue DataFrame.

    Checks performed:
      - duplicate rows
      - all-null columns
      - mixed dtypes within a single column (object cols where >5% of
        non-null values fail to coerce to numeric while others succeed)
      - leading/trailing whitespace in string columns
      - suspiciously round numeric columns (mean/std all integer)
      - encoding artifacts (presence of Unicode replacement char U+FFFD)
      - inconsistent date formats in date-like columns
    """
    rows: list[dict[str, Any]] = []

    def _issue(severity, where, kind, message):
        rows.append({
            "severity": severity, "column": where, "kind": kind,
            "message": message,
        })

    if df is None or df.empty:
        _issue("info", "(table)", "empty", "DataFrame is empty")
        return pd.DataFrame(rows)

    # 1. Duplicate rows
    try:
        dup = int(df.duplicated().sum())
        if dup > 0:
            _issue("warning", "(rows)", "duplicate_rows",
                   f"{dup} duplicate row(s)")
    except Exception:
        pass

    for col in df.columns:
        s = df[col]
        # 2. All-null column
        if s.notna().sum() == 0:
            _issue("warning", str(col), "all_null", "every value is null")
            continue

        # 3. Mixed numeric / non-numeric within object column
        if s.dtype == "object":
            non_null = s.dropna()
            if not non_null.empty:
                coerced = pd.to_numeric(non_null, errors="coerce")
                num_ok = coerced.notna().sum()
                num_total = len(non_null)
                if 0 < num_ok < num_total and num_ok / num_total > 0.05:
                    _issue("warning", str(col), "mixed_types",
                           f"{num_ok}/{num_total} values parse as numeric — "
                           f"column may have mixed numeric/text entries")

        # 4. Leading / trailing whitespace
        if s.dtype == "object":
            try:
                trimmable = s.dropna().astype(str).map(
                    lambda x: x != x.strip()
                ).sum()
                if trimmable > 0:
                    _issue("info", str(col), "whitespace",
                           f"{trimmable} value(s) have leading/trailing whitespace")
            except Exception:
                pass

        # 5. Encoding artifacts (U+FFFD)
        if s.dtype == "object":
            try:
                bad = s.dropna().astype(str).str.contains("�").sum()
                if bad > 0:
                    _issue("warning", str(col), "encoding",
                           f"{bad} value(s) contain U+FFFD (replacement char) — "
                           f"likely a decoding issue upstream")
            except Exception:
                pass

        # 6. Suspiciously round numerics (entire column = integers stored as float)
        if pd.api.types.is_float_dtype(s):
            non_null = s.dropna()
            if not non_null.empty and (non_null == non_null.astype(int)).all():
                _issue("info", str(col), "float_with_no_decimals",
                       "all values are whole numbers — consider an int dtype")

    # 7. Date columns with inconsistent formats — detect on object cols
    #    whose values include common date separators but parse to >2 distinct
    #    inferred formats.
    for col in df.columns:
        s = df[col]
        if s.dtype != "object":
            continue
        sample = s.dropna().astype(str).head(50)
        if sample.empty:
            continue
        looks_dateish = sample.str.contains(r"\d{2,4}[-/.\\]\d{1,2}").sum()
        if looks_dateish < max(3, len(sample) // 4):
            continue
        formats = set()
        for v in sample:
            # crude format inference — replace digits with 'd' so we group by shape
            formats.add(re.sub(r"\d", "d", v))
            if len(formats) > 5:
                break
        if len(formats) > 2:
            _issue("warning", str(col), "mixed_date_format",
                   f"date-like column shows {len(formats)} distinct value shapes")

    return pd.DataFrame(rows)


def detect_data_quality_issues_per_csv(
    data_folder: Any,
    *,
    recursive: bool = True,
) -> pd.DataFrame:
    """Run detect_data_quality_issues across every CSV in the folder.
    Returns a long frame with a leading `csv` column."""
    folders = normalize_data_folders(data_folder)
    frames: list[pd.DataFrame] = []
    for csv_path in list_csv_files(folders, recursive=recursive):
        try:
            df = pd.read_csv(csv_path)
            issues = detect_data_quality_issues(df)
            if not issues.empty:
                issues.insert(0, "csv", csv_path.name)
                frames.append(issues)
        except Exception as exc:
            frames.append(pd.DataFrame([{
                "csv": csv_path.name,
                "severity": "error",
                "column": "",
                "kind": "read_error",
                "message": str(exc),
            }]))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ============================================================
# CSV splitter
# ============================================================

def split_csv_by_column(
    path: Any,
    column: str,
    output_dir: Any,
    *,
    sanitize: bool = True,
) -> pd.DataFrame:
    """Split a CSV into per-value files. For each distinct value in
    `column`, writes <output_dir>/<stem>_<value>.csv with only the
    matching rows. Returns a summary DataFrame (one row per output)."""
    p = Path(path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(p)
    col = column if column in df.columns else find_column_case_insensitive(df, column)
    if col is None:
        return pd.DataFrame([{"status": f"missing column: {column}"}])

    summary: list[dict[str, Any]] = []
    for value, sub in df.groupby(col, dropna=False):
        vname = str(value)
        if sanitize:
            vname = re.sub(r"[^A-Za-z0-9._-]+", "_", vname).strip("_") or "blank"
        out_path = out_dir / f"{p.stem}_{vname}.csv"
        n = 2
        while out_path.exists():
            out_path = out_dir / f"{p.stem}_{vname}_v{n}.csv"
            n += 1
        sub.to_csv(out_path, index=False)
        summary.append({
            "value":     str(value),
            "rows":      int(len(sub)),
            "output":    str(out_path),
        })
    return pd.DataFrame(summary)


def pivot_per_csv(
    data_folder: Any,
    index_col: str,
    columns_col: str,
    value_col: str,
    *,
    agg: str = "sum",
    recursive: bool = True,
) -> pd.DataFrame:
    """Pivot each CSV in the folder: rows=index_col, cols=columns_col,
    values=value_col aggregated by `agg`. Returns one long frame with
    a leading `csv` column distinguishing rows from different files."""
    folders = normalize_data_folders(data_folder)
    frames: list[pd.DataFrame] = []
    for csv_path in list_csv_files(folders, recursive=recursive):
        root = first_matching_root(csv_path, folders)
        try:
            df = pd.read_csv(csv_path)
            ix = find_column_case_insensitive(df, index_col)
            cx = find_column_case_insensitive(df, columns_col)
            vx = find_column_case_insensitive(df, value_col)
            if not (ix and cx and vx):
                frames.append(pd.DataFrame([{
                    "csv": csv_path.name,
                    "status": f"missing column(s): index={index_col}, "
                              f"columns={columns_col}, values={value_col}",
                }]))
                continue
            df[vx] = pd.to_numeric(df[vx], errors="coerce")
            df = df[df[vx].notna()]
            if df.empty:
                frames.append(pd.DataFrame([{
                    "csv": csv_path.name,
                    "status": "no numeric values to pivot",
                }]))
                continue
            piv = (df.pivot_table(
                       index=ix, columns=cx, values=vx, aggfunc=agg,
                       fill_value=0)
                     .reset_index())
            piv.insert(0, "csv", csv_path.name)
            piv.insert(1, "relative_path", safe_relative_path(csv_path, root))
            frames.append(piv)
        except Exception as exc:
            frames.append(pd.DataFrame([{
                "csv": csv_path.name,
                "status": f"error: {exc}",
            }]))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def correlation_per_csv(
    data_folder: Any,
    x_col: str,
    y_col: str,
    *,
    exclude_zero: bool = False,
    recursive: bool = True,
) -> pd.DataFrame:
    folders = normalize_data_folders(data_folder)
    rows: list[dict[str, Any]] = []
    for csv_path in list_csv_files(folders, recursive=recursive):
        root = first_matching_root(csv_path, folders)
        try:
            df = pd.read_csv(csv_path)
            actual_x = find_column_case_insensitive(df, x_col)
            actual_y = find_column_case_insensitive(df, y_col)
            if actual_x is None or actual_y is None:
                rows.append({
                    "csv": csv_path.name,
                    "status": f"missing column(s): x={x_col}, y={y_col}",
                    "correlation": None,
                    "rows_used": 0,
                })
                continue
            x = pd.to_numeric(df[actual_x], errors="coerce")
            y = pd.to_numeric(df[actual_y], errors="coerce")
            mask = x.notna() & y.notna()
            if exclude_zero:
                mask = mask & (x != 0) & (y != 0)
            rows_used = int(mask.sum())
            corr = x[mask].corr(y[mask]) if rows_used >= 2 else None
            rows.append({
                "csv": csv_path.name,
                "relative_path": safe_relative_path(csv_path, root),
                "status": "ok" if rows_used >= 2 else "not enough valid rows",
                "x_column_used": actual_x,
                "y_column_used": actual_y,
                "correlation": float(corr) if corr is not None else None,
                "rows_used": rows_used,
                "total_rows": len(df),
            })
        except Exception as exc:
            rows.append({
                "csv": csv_path.name,
                "status": f"error: {exc}",
                "correlation": None,
                "rows_used": 0,
            })
    return pd.DataFrame(rows)


# ============================================================
# CSV-level helpers (single file)
# ============================================================

def summarize_csv(path: Any) -> pd.DataFrame:
    """One-call profile of a single CSV.

    Returns a DataFrame with one row per column: dtype, null %, unique
    count, top values (for categoricals), mean/std/min/max (for numerics).
    Saves the model from hand-rolling 15 lines of pandas.
    """
    p = Path(path)
    df = pd.read_csv(p)
    rows: List[Dict[str, Any]] = []
    total = max(1, len(df))
    for col in df.columns:
        s = df[col]
        nulls = int(s.isna().sum())
        rec: Dict[str, Any] = {
            "csv":          p.name,
            "column":       str(col),
            "dtype":        str(s.dtype),
            "non_null":     int(s.count()),
            "null_pct":     round(100 * nulls / total, 2),
            "unique":       int(s.nunique(dropna=True)),
        }
        # Try numeric coercion to see if we have aggregatable values
        coerced = pd.to_numeric(s, errors="coerce")
        if coerced.notna().sum() >= max(3, total * 0.5):
            valid = coerced.dropna()
            rec["mean"]   = float(valid.mean())
            rec["std"]    = float(valid.std()) if valid.size >= 2 else None
            rec["min"]    = float(valid.min())
            rec["max"]    = float(valid.max())
            rec["median"] = float(valid.median())
            rec["top_values"] = ""
        else:
            # Categorical / text — show top values
            try:
                vc = s.astype(str).value_counts(dropna=True).head(5)
                rec["top_values"] = ", ".join(
                    f"{idx}({cnt})" for idx, cnt in vc.items()
                )
            except Exception:
                rec["top_values"] = ""
            rec["mean"] = rec["std"] = rec["min"] = rec["max"] = rec["median"] = None
        rows.append(rec)
    return pd.DataFrame(rows)


def detect_outliers(
    df: pd.DataFrame,
    column: str,
    *,
    method: str = "iqr",
    threshold: float = 1.5,
) -> pd.DataFrame:
    """Return rows where `column` is an outlier.

    method='iqr'  — value outside [Q1 - k*IQR, Q3 + k*IQR] with k=threshold
    method='zscore' — |z| > threshold
    Adds an `outlier_score` column showing how far out the value is.
    """
    if column not in df.columns:
        actual = find_column_case_insensitive(df, column)
        if actual is None:
            return pd.DataFrame()
        column = actual
    values = pd.to_numeric(df[column], errors="coerce")
    work = df.loc[values.notna()].copy()
    work[column] = values[values.notna()]
    if work.empty:
        return work

    method = (method or "iqr").lower()
    if method == "zscore":
        mu = work[column].mean()
        sd = work[column].std() or 1.0
        z = (work[column] - mu) / sd
        mask = z.abs() > threshold
        work["outlier_score"] = z.abs()
    else:  # iqr
        q1 = work[column].quantile(0.25)
        q3 = work[column].quantile(0.75)
        iqr = q3 - q1
        lower = q1 - threshold * iqr
        upper = q3 + threshold * iqr
        mask = (work[column] < lower) | (work[column] > upper)
        work["outlier_score"] = work[column].apply(
            lambda v: max(lower - v, v - upper) / max(iqr, 1e-9)
        )
    return work.loc[mask].sort_values("outlier_score", ascending=False)


def time_series_resample(
    df: pd.DataFrame,
    date_col: str,
    value_col: str,
    *,
    freq: str = "M",
    agg: str = "sum",
) -> pd.DataFrame:
    """Aggregate `value_col` over `date_col` at the given frequency.

    freq follows pandas offset aliases: 'D' (day), 'W' (week), 'M'
    (month), 'Q' (quarter), 'Y' (year). `agg` is any pandas agg name
    ('sum', 'mean', 'min', 'max', 'count', 'median').
    """
    dc = date_col if date_col in df.columns else find_column_case_insensitive(df, date_col)
    vc = value_col if value_col in df.columns else find_column_case_insensitive(df, value_col)
    if dc is None or vc is None:
        return pd.DataFrame()
    work = df.copy()
    work[dc] = pd.to_datetime(work[dc], errors="coerce")
    work[vc] = pd.to_numeric(work[vc], errors="coerce")
    work = work[work[dc].notna() & work[vc].notna()]
    if work.empty:
        return work
    grouped = work.set_index(dc)[vc].resample(freq).agg(agg)
    out = grouped.reset_index()
    out.columns = [dc, f"{vc}_{agg}"]
    return out


# ============================================================
# Cross-CSV helpers
# ============================================================

def join_csvs_on_column(
    left: Any,
    right: Any,
    on: str,
    *,
    how: str = "inner",
    suffixes: tuple = ("_left", "_right"),
) -> pd.DataFrame:
    """Read two CSVs and join them on a column.

    `left` and `right` are CSV paths. `on` is the column name (case-
    insensitive). `how` is any pandas join kind: 'inner', 'left',
    'right', 'outer'.
    """
    df_l = pd.read_csv(left) if not isinstance(left, pd.DataFrame) else left
    df_r = pd.read_csv(right) if not isinstance(right, pd.DataFrame) else right
    col_l = on if on in df_l.columns else find_column_case_insensitive(df_l, on)
    col_r = on if on in df_r.columns else find_column_case_insensitive(df_r, on)
    if col_l is None or col_r is None:
        return pd.DataFrame()
    # Rename to a common column name so merge works cleanly
    df_l = df_l.rename(columns={col_l: on})
    df_r = df_r.rename(columns={col_r: on})
    return pd.merge(df_l, df_r, on=on, how=how, suffixes=suffixes)


def compare_two_csvs(
    a: Any,
    b: Any,
    *,
    on: Optional[str] = None,
) -> pd.DataFrame:
    """Row-level diff between two CSV snapshots.

    With `on`: rows are matched by that key column. Result has a `status`
    column with 'added' / 'removed' / 'changed' / 'same' and per-column
    columns showing left/right values where they differ.

    Without `on`: returns rows present in exactly one CSV (treats every
    row as a fingerprint). Faster but less informative.
    """
    df_a = pd.read_csv(a) if not isinstance(a, pd.DataFrame) else a
    df_b = pd.read_csv(b) if not isinstance(b, pd.DataFrame) else b

    if on:
        col_a = on if on in df_a.columns else find_column_case_insensitive(df_a, on)
        col_b = on if on in df_b.columns else find_column_case_insensitive(df_b, on)
        if col_a is None or col_b is None:
            return pd.DataFrame([{"status": "error",
                                  "message": f"join column {on!r} missing"}])
        df_a = df_a.rename(columns={col_a: on}).set_index(on)
        df_b = df_b.rename(columns={col_b: on}).set_index(on)
        all_keys = df_a.index.union(df_b.index)
        rows: List[Dict[str, Any]] = []
        common_cols = [c for c in df_a.columns if c in df_b.columns]
        for k in all_keys:
            in_a = k in df_a.index
            in_b = k in df_b.index
            if in_a and not in_b:
                rows.append({on: k, "status": "removed"})
            elif in_b and not in_a:
                rows.append({on: k, "status": "added"})
            else:
                ra = df_a.loc[k]
                rb = df_b.loc[k]
                diffs = {}
                for c in common_cols:
                    va = ra[c] if not isinstance(ra, pd.DataFrame) else ra[c].iloc[0]
                    vb = rb[c] if not isinstance(rb, pd.DataFrame) else rb[c].iloc[0]
                    if pd.isna(va) and pd.isna(vb):
                        continue
                    if va != vb:
                        diffs[f"{c}_a"] = va
                        diffs[f"{c}_b"] = vb
                rec = {on: k, "status": "changed" if diffs else "same"}
                rec.update(diffs)
                rows.append(rec)
        return pd.DataFrame(rows)
    # No key — set-style diff
    sa = set(map(tuple, df_a.astype(str).values.tolist()))
    sb = set(map(tuple, df_b.astype(str).values.tolist()))
    added   = [list(t) + ["added"]   for t in sb - sa]
    removed = [list(t) + ["removed"] for t in sa - sb]
    cols = list(df_a.columns) + ["status"]
    return pd.DataFrame(added + removed, columns=cols)


def preview_csv_inventory(folders: Any, max_files: int = 25, max_cols: int = 30) -> str:
    """Compact, prompt-friendly listing of CSVs and their columns."""
    normalized = normalize_data_folders(folders)
    csvs = list_csv_files(normalized)
    lines = [f"CSV files found: {len(csvs)}"]
    for csv_path in csvs[:max_files]:
        root = first_matching_root(csv_path, normalized)
        try:
            df_head = pd.read_csv(csv_path, nrows=5)
            cols = list(df_head.columns)
            shown_cols = cols[:max_cols]
            more = "" if len(cols) <= max_cols else f" ... (+{len(cols) - max_cols} more)"
            lines.append(f"- {csv_path.name}")
            lines.append(f"  relative_path: {safe_relative_path(csv_path, root)}")
            lines.append(f"  columns: {shown_cols}{more}")
        except Exception as exc:
            lines.append(f"- {csv_path.name}  (error reading columns: {exc})")
    if len(csvs) > max_files:
        lines.append(f"... {len(csvs) - max_files} more CSV files not shown.")
    return "\n".join(lines)


# ============================================================
# Sandboxed code execution
# ============================================================

DANGEROUS_PATTERNS = [
    r"\bimport\s+os\b",
    r"\bimport\s+sys\b",
    r"\bimport\s+subprocess\b",
    r"\bimport\s+shutil\b",
    r"\bimport\s+socket\b",
    r"\bimport\s+requests\b",
    r"\bimport\s+urllib\b",
    r"\bfrom\s+os\b",
    r"\bfrom\s+sys\b",
    r"\bfrom\s+subprocess\b",
    r"\bfrom\s+shutil\b",
    r"\bfrom\s+socket\b",
    r"\bfrom\s+requests\b",
    r"\bfrom\s+urllib\b",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bcompile\s*\(",
    r"\binput\s*\(",
    r"\bopen\s*\([^)]*,\s*['\"][wa+x]",
    r"\.to_csv\s*\(",
    r"\.to_excel\s*\(",
    r"\.to_parquet\s*\(",
    r"\.to_pickle\s*\(",
    r"\.unlink\s*\(",
    r"\.rmdir\s*\(",
    r"\.remove\s*\(",
    r"\.rename\s*\(",
    r"\brmtree\s*\(",
    r"\bsystem\s*\(",
    r"\bpopen\s*\(",
]


def validate_generated_code(code: str) -> Tuple[bool, str]:
    if not code.strip():
        return False, "Generated code is empty."
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, code, flags=re.IGNORECASE):
            return False, f"Blocked unsafe pattern: {pattern}"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"Syntax error: {exc}"

    forbidden_import_roots = {
        "os", "sys", "subprocess", "shutil", "socket",
        "requests", "urllib", "http", "ftplib",
    }
    forbidden_calls = {"eval", "exec", "compile", "input"}
    forbidden_attrs = {
        "unlink", "rmdir", "remove", "rename",
        "to_csv", "to_excel", "to_parquet", "to_pickle", "to_sql",
        "system", "popen", "Popen",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in forbidden_import_roots:
                    return False, f"Forbidden import: {alias.name}"
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in forbidden_import_roots:
                return False, f"Forbidden import from: {node.module}"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                return False, f"Forbidden call: {node.func.id}"
            if isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_attrs:
                return False, f"Forbidden method: {node.func.attr}"
    return True, "ok"


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    allowed_roots = {"pandas", "pathlib", "numpy", "math", "re", "json", "statistics", "collections"}
    root = name.split(".")[0]
    if root not in allowed_roots:
        raise ImportError(f"Import blocked by sandbox: {name}")
    return __import__(name, globals, locals, fromlist, level)


def execute_pandas_code(
    code: str,
    allowed_folders: List[Path],
) -> Tuple[Optional[pd.DataFrame], str]:
    ok, msg = validate_generated_code(code)
    if not ok:
        return None, f"SAFETY CHECK FAILED: {msg}"

    normalized_folders = normalize_data_folders(allowed_folders)
    if not normalized_folders:
        return None, "No allowed folders configured for the analyst."

    safe_builtins = {
        "__import__": _safe_import,
        "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
        "enumerate": enumerate, "float": float, "int": int,
        "isinstance": isinstance, "len": len, "list": list, "max": max,
        "min": min, "print": print, "range": range, "round": round,
        "set": set, "sorted": sorted, "str": str, "sum": sum,
        "tuple": tuple, "zip": zip,
        "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    }

    globals_dict: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "pd": pd,
        "Path": Path,
        "DATA_FOLDERS": [str(p) for p in normalized_folders],
        "DATA_FOLDER": str(normalized_folders[0]),
        "list_csv_files": list_csv_files,
        "find_column_case_insensitive": find_column_case_insensitive,
        "find_columns_contains": find_columns_contains,
        "csv_inventory": csv_inventory,
        "count_rows_per_csv": count_rows_per_csv,
        "average_numeric_column_per_csv": average_numeric_column_per_csv,
        "std_numeric_column_per_csv": std_numeric_column_per_csv,
        "sum_numeric_column_per_csv": sum_numeric_column_per_csv,
        "min_max_numeric_column_per_csv": min_max_numeric_column_per_csv,
        "numeric_summary_per_csv": numeric_summary_per_csv,
        "count_matching_rows_per_csv": count_matching_rows_per_csv,
        "filter_rows_across_csvs": filter_rows_across_csvs,
        "groupby_mean_per_csv": groupby_mean_per_csv,
        "correlation_per_csv": correlation_per_csv,
        # Newer single-file + cross-file helpers
        "summarize_csv":          summarize_csv,
        "detect_outliers":        detect_outliers,
        "time_series_resample":   time_series_resample,
        "join_csvs_on_column":    join_csvs_on_column,
        "compare_two_csvs":       compare_two_csvs,
        # Excel + CSV-or-Excel helpers
        "list_excel_files":       list_excel_files,
        "list_data_files":        list_data_files,
        "read_table":             read_table,
        "read_excel_sheets":      read_excel_sheets,
        "excel_inventory":        excel_inventory,
        # Parquet / SQLite + pivot
        "list_parquet_files":     list_parquet_files,
        "list_sqlite_files":      list_sqlite_files,
        "list_sqlite_tables":     list_sqlite_tables,
        "read_sqlite_table":      read_sqlite_table,
        "pivot_per_csv":          pivot_per_csv,
        # Text cleaning + top-N + schema doc
        "clean_string_column":    clean_string_column,
        "extract_dates":          extract_dates,
        "top_n_per_csv":          top_n_per_csv,
        "schema_doc_from_csv":    schema_doc_from_csv,
        # Data quality + splitter
        "detect_data_quality_issues":         detect_data_quality_issues,
        "detect_data_quality_issues_per_csv": detect_data_quality_issues_per_csv,
        "split_csv_by_column":                split_csv_by_column,
    }
    if np is not None:
        globals_dict["np"] = np

    locals_dict: dict[str, Any] = {}
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exec(code, globals_dict, locals_dict)
    except Exception:
        log = "EXECUTION ERROR:\n" + traceback.format_exc()
        if stdout_buf.getvalue():
            log += "\nSTDOUT:\n" + stdout_buf.getvalue()
        if stderr_buf.getvalue():
            log += "\nSTDERR:\n" + stderr_buf.getvalue()
        return None, log

    result_df = locals_dict.get("result_df", globals_dict.get("result_df"))

    log_parts: list[str] = []
    if stdout_buf.getvalue():
        log_parts.append("STDOUT:\n" + stdout_buf.getvalue())
    if stderr_buf.getvalue():
        log_parts.append("STDERR:\n" + stderr_buf.getvalue())

    if result_df is None:
        log_parts.append("Code did not create `result_df`.")
        return None, "\n\n".join(log_parts) if log_parts else "no result_df"

    if isinstance(result_df, pd.Series):
        result_df = result_df.reset_index()
    if isinstance(result_df, dict):
        result_df = pd.DataFrame([result_df])
    if not isinstance(result_df, pd.DataFrame):
        log_parts.append(f"result_df is not a DataFrame; got {type(result_df)}")
        return None, "\n\n".join(log_parts)

    if not log_parts:
        log_parts.append("ok")
    return result_df, "\n\n".join(log_parts)


# ============================================================
# Code generation prompt
# ============================================================

def build_pandas_code_prompt(question: str, data_folders: List[Path], inventory: str) -> str:
    folder_lines = "\n".join(f"- {p}" for p in data_folders)
    return f"""You are writing a single pandas snippet that answers the user's question
about CSV files in their vault. Output ONLY executable Python code — no
markdown fences, no commentary, no explanation.

User question:
{question}

Available data folders:
{folder_lines}

CSV inventory (column names taken from real files):
{inventory}

Available variables:
- DATA_FOLDERS  (list[str]) — pass this as `data_folder=DATA_FOLDERS` to helpers
- DATA_FOLDER   (str)       — first folder, for legacy single-folder calls
- pd            (pandas)
- Path          (pathlib.Path)
- np            (numpy, may be None)

Available helper functions (prefer these over raw pandas — they handle
case-insensitive column matching, NaN/zero filtering, and multi-CSV scans):
  list_csv_files(data_folder, recursive=True)
  csv_inventory(data_folder, recursive=True, max_files=None)
  count_rows_per_csv(data_folder, recursive=True)
  find_column_case_insensitive(df, column_name)
  find_columns_contains(df, text)
  average_numeric_column_per_csv(data_folder, column_name, exclude_zero=False)
  std_numeric_column_per_csv(data_folder, column_name, exclude_zero=False)
  sum_numeric_column_per_csv(data_folder, column_name, exclude_zero=False)
  min_max_numeric_column_per_csv(data_folder, column_name, exclude_zero=False)
  numeric_summary_per_csv(data_folder, column_name=None, exclude_zero=False)
  count_matching_rows_per_csv(data_folder, column_name, equals=None, contains=None,
                              case_sensitive=False)
  filter_rows_across_csvs(data_folder, column_name, equals=None, contains=None,
                          case_sensitive=False, max_rows_per_csv=None)
  groupby_mean_per_csv(data_folder, group_col, value_col, exclude_zero=False)
  correlation_per_csv(data_folder, x_col, y_col, exclude_zero=False)
  summarize_csv(path)                          # one-row-per-column profile
  detect_outliers(df, column, method="iqr", threshold=1.5)
  time_series_resample(df, date_col, value_col, freq="M", agg="sum")
  join_csvs_on_column(left_path, right_path, on, how="inner")
  compare_two_csvs(a_path, b_path, on=None)   # row-level diff snapshot

Excel + cross-format support:
  list_excel_files(data_folder, recursive=True)
  list_data_files(data_folder, recursive=True)  # CSV + Excel combined
  read_table(path, sheet=None)                  # CSV, TSV, .csv.gz,
                                                # XLSX, Parquet -> df
  read_excel_sheets(path)                       # all sheets -> {name: df}
  excel_inventory(data_folder, recursive=True)

Parquet + SQLite:
  list_parquet_files(data_folder, recursive=True)
  list_sqlite_files(data_folder, recursive=True)
  list_sqlite_tables(path)                      # list[str] of table names
  read_sqlite_table(path, table, limit=None)    # df from a SQLite table

Pivot tables:
  pivot_per_csv(data_folder, index_col, columns_col, value_col, agg="sum")

Text + dates + value counts + schema doc:
  clean_string_column(df, column, lowercase=True, strip=True, ...)
  extract_dates(df, column, new_column=None, errors="coerce")
  top_n_per_csv(data_folder, column, n=10)     # value_counts per file
  schema_doc_from_csv(path)                    # -> markdown string

Data quality + splitter:
  detect_data_quality_issues(df)
    issues table: duplicates, all-null cols, mixed types, whitespace,
    encoding artifacts, suspicious round numerics, mixed date formats
  detect_data_quality_issues_per_csv(folder)
  split_csv_by_column(path, column, output_dir)
    writes one CSV per distinct value of `column`

Rules:
- Assign the final answer to a DataFrame named `result_df`.
- No filesystem writes, no network, no subprocess, no eval/exec.
- Prefer helpers over hand-rolled pandas when one fits.

Examples:

Q: How many rows are in each CSV?
result_df = count_rows_per_csv(DATA_FOLDERS)

Q: What is the average rating for each CSV, ignoring zeros?
result_df = average_numeric_column_per_csv(DATA_FOLDERS, column_name="rating", exclude_zero=True)

Q: Count games whose all_platforms column contains PlayStation 5.
result_df = count_matching_rows_per_csv(DATA_FOLDERS, column_name="all_platforms",
                                        contains="PlayStation 5")

Q: What percentage of games are on PlayStation vs Xbox?
# Two count_matching_rows queries concatenated — match_pct column is the answer.
ps = count_matching_rows_per_csv(DATA_FOLDERS, column_name="all_platforms",
                                 contains="PlayStation")
xb = count_matching_rows_per_csv(DATA_FOLDERS, column_name="all_platforms",
                                 contains="Xbox")
ps.insert(0, "platform", "PlayStation")
xb.insert(0, "platform", "Xbox")
result_df = pd.concat([ps, xb], ignore_index=True)

Q: Show all rows where the developer is Rockstar Games.
result_df = filter_rows_across_csvs(DATA_FOLDERS, column_name="developers",
                                    contains="Rockstar Games", max_rows_per_csv=50)

Q: What is the correlation between metacritic and user_rating?
result_df = correlation_per_csv(DATA_FOLDERS, x_col="metacritic", y_col="user_rating")

Now answer the user question. Output only the code, no fences.
"""


def extract_python_code(model_text: str) -> str:
    """Strip optional ```python``` fences from model output."""
    text = (model_text or "").strip()
    fence = re.search(r"```(?:python)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


# ============================================================
# Computational question detection
# ============================================================

_COMPUTE_KEYWORDS = (
    "how many", "count", "number of", "total",
    "average", "mean", "median", "std", "standard deviation",
    "sum ", "min ", "max ", "minimum", "maximum",
    "percent", "percentage", "%", "ratio", "proportion",
    "compare", " vs ", " versus ",
    "correlation", "corr ",
    "group by", "groupby", "by month", "by year", "by category",
    "filter", "rows with", "rows where", "rows that",
    "which file", "which files", "across files", "across the files",
)


def looks_computational(query: str) -> bool:
    """Heuristic: does the user's question want a numeric/aggregate answer?"""
    q = (query or "").lower()
    return any(kw in q for kw in _COMPUTE_KEYWORDS)


# ============================================================
# DataFrame -> prompt-friendly text
# ============================================================

def format_result_for_prompt(df: pd.DataFrame, *, max_rows: int = 30, max_chars: int = 4000) -> str:
    if df is None or df.empty:
        return "(analyst returned an empty result)"
    head = df.head(max_rows)
    text = head.to_string(index=False)
    if len(df) > len(head):
        text += f"\n... ({len(df) - len(head)} more rows omitted)"
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (truncated)"
    return text
