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


def list_csv_files(data_folder: Any, recursive: bool = True) -> List[Path]:
    """All CSVs under the given folder(s), deduped."""
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
    return sorted(deduped.values())


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
