# ============================================================
# graph_data.py  —  Unified data loading for the Grapher tab
# ============================================================
# Loads CSV/TSV, Excel, JSON, NumPy, and plain text files
# into a unified DataSet object for plotting.
#
# Install:
#   pip install pandas numpy openpyxl
# ============================================================

from __future__ import annotations

import json
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import pandas as pd
    _PANDAS_OK = True
except ImportError:
    _PANDAS_OK = False

try:
    import openpyxl  # noqa — just checking availability
    _OPENPYXL_OK = True
except ImportError:
    _OPENPYXL_OK = False


# ============================================================
# DataSet — the unified data container
# ============================================================

@dataclass
class ColumnInfo:
    name: str
    dtype: str          # "numeric", "categorical", "datetime", "text"
    n_unique: int = 0
    n_null: int = 0
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    mean_val: Optional[float] = None


@dataclass
class DataSet:
    """
    Unified data container produced by all loaders.
    The GUI and plot engine work exclusively with DataSet objects.
    """
    name: str                          # filename stem
    source_path: Path
    format: str                        # "csv", "excel", "json", "numpy", "text"
    df: Any                            # pandas DataFrame
    columns: List[ColumnInfo] = field(default_factory=list)
    load_error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def numeric_columns(self) -> List[str]:
        return [c.name for c in self.columns if c.dtype == "numeric"]

    @property
    def categorical_columns(self) -> List[str]:
        return [c.name for c in self.columns if c.dtype == "categorical"]

    @property
    def all_columns(self) -> List[str]:
        return [c.name for c in self.columns]

    @property
    def shape(self) -> Tuple[int, int]:
        if self.df is None:
            return (0, 0)
        return self.df.shape

    def summary(self) -> str:
        """Compact text summary for injection into AI context."""
        if self.df is None:
            return f"DataSet '{self.name}': load error — {self.load_error}"
        lines = [
            f"DataSet: {self.name}",
            f"Source:  {self.source_path.name}",
            f"Shape:   {self.shape[0]} rows × {self.shape[1]} columns",
            f"Format:  {self.format}",
            "",
            "Columns:",
        ]
        for col in self.columns:
            if col.dtype == "numeric":
                lines.append(
                    f"  {col.name!r:30s} numeric   "
                    f"min={col.min_val:.4g}  max={col.max_val:.4g}  "
                    f"mean={col.mean_val:.4g}  nulls={col.n_null}"
                )
            else:
                lines.append(
                    f"  {col.name!r:30s} {col.dtype:12s} "
                    f"unique={col.n_unique}  nulls={col.n_null}"
                )
        return "\n".join(lines)

    def head_str(self, n: int = 5) -> str:
        if self.df is None:
            return ""
        return self.df.head(n).to_string()


# ============================================================
# Column introspection
# ============================================================

def _introspect_columns(df: Any) -> List[ColumnInfo]:
    """Analyse a DataFrame and return ColumnInfo for each column."""
    if not _PANDAS_OK:
        return []
    infos: List[ColumnInfo] = []
    for col in df.columns:
        series = df[col]
        n_null = int(series.isna().sum())

        if _is_numeric(series):
            numeric = _PANDAS_OK and pd.to_numeric(series, errors="coerce")
            infos.append(ColumnInfo(
                name=str(col),
                dtype="numeric",
                n_null=n_null,
                n_unique=int(series.nunique()),
                min_val=float(numeric.min()) if _PANDAS_OK else None,
                max_val=float(numeric.max()) if _PANDAS_OK else None,
                mean_val=float(numeric.mean()) if _PANDAS_OK else None,
            ))
        elif _is_datetime(series):
            infos.append(ColumnInfo(
                name=str(col), dtype="datetime",
                n_null=n_null, n_unique=int(series.nunique()),
            ))
        elif series.nunique() <= max(20, len(series) * 0.1):
            infos.append(ColumnInfo(
                name=str(col), dtype="categorical",
                n_null=n_null, n_unique=int(series.nunique()),
            ))
        else:
            infos.append(ColumnInfo(
                name=str(col), dtype="text",
                n_null=n_null, n_unique=int(series.nunique()),
            ))
    return infos


def _is_numeric(series: Any) -> bool:
    if not _PANDAS_OK:
        return False
    if pd.api.types.is_numeric_dtype(series):
        return True
    converted = pd.to_numeric(series, errors="coerce")
    return converted.notna().mean() > 0.7


def _is_datetime(series: Any) -> bool:
    if not _PANDAS_OK:
        return False
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    return False


# ============================================================
# Loaders
# ============================================================

class DataLoader:
    """
    Unified loader — detects format by extension and loads
    into a DataSet. All errors are soft: returns a DataSet
    with load_error set rather than raising.
    """

    SUPPORTED_EXTENSIONS = {
        ".csv", ".tsv", ".txt", ".log",
        ".xlsx", ".xls",
        ".json",
        ".npy", ".npz",
    }

    @classmethod
    def can_load(cls, path: Path) -> bool:
        return path.suffix.lower() in cls.SUPPORTED_EXTENSIONS

    @classmethod
    def load(cls, path: Path, **kwargs) -> DataSet:
        ext = path.suffix.lower()
        if ext in (".csv", ".tsv"):
            return cls._load_csv(path, **kwargs)
        elif ext in (".txt", ".log"):
            return cls._load_text(path, **kwargs)
        elif ext in (".xlsx", ".xls"):
            return cls._load_excel(path, **kwargs)
        elif ext == ".json":
            return cls._load_json(path, **kwargs)
        elif ext in (".npy", ".npz"):
            return cls._load_numpy(path, **kwargs)
        else:
            ds = DataSet(name=path.stem, source_path=path, format="unknown",
                         df=None)
            ds.load_error = f"Unsupported extension: {ext}"
            return ds

    # ── CSV / TSV ─────────────────────────────────────────────

    @classmethod
    def _load_csv(cls, path: Path, **kwargs) -> DataSet:
        if not _PANDAS_OK:
            ds = DataSet(name=path.stem, source_path=path, format="csv", df=None)
            ds.load_error = "pandas not installed"
            return ds
        try:
            sep = "\t" if path.suffix.lower() == ".tsv" else None
            df = pd.read_csv(path, sep=sep, engine="python", **kwargs)
            # Try to coerce object/string columns to numeric (pandas 3.x compatible)
            for col in df.select_dtypes(include=["object", "str"]).columns:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    try:
                        converted = pd.to_numeric(df[col], errors="coerce")
                        # Only replace if most values converted successfully
                        if converted.notna().mean() > 0.7:
                            df[col] = converted
                    except Exception:
                        pass
            ds = DataSet(name=path.stem, source_path=path,
                         format="csv", df=df)
            ds.columns = _introspect_columns(df)
            return ds
        except Exception as e:
            ds = DataSet(name=path.stem, source_path=path, format="csv", df=None)
            ds.load_error = str(e)
            return ds

    # ── Plain text / log ──────────────────────────────────────

    @classmethod
    def _load_text(cls, path: Path, **kwargs) -> DataSet:
        if not _PANDAS_OK:
            ds = DataSet(name=path.stem, source_path=path, format="text", df=None)
            ds.load_error = "pandas not installed"
            return ds
        try:
            text = path.read_text(encoding="utf-8", errors="replace")

            # Strategy 1: try as whitespace-separated table
            try:
                df = pd.read_csv(path, sep=r"\s+", engine="python",
                                 comment="#")
                if df.shape[1] >= 2 and df.shape[0] >= 3:
                    ds = DataSet(name=path.stem, source_path=path,
                                 format="text", df=df)
                    ds.columns = _introspect_columns(df)
                    return ds
            except Exception:
                pass

            # Strategy 2: extract all numbers per line
            rows = []
            for line in text.splitlines():
                nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", line)
                if nums:
                    rows.append([float(n) for n in nums])

            if rows:
                max_cols = max(len(r) for r in rows)
                padded = [r + [float("nan")] * (max_cols - len(r)) for r in rows]
                df = pd.DataFrame(padded,
                                  columns=[f"col_{i}" for i in range(max_cols)])
                ds = DataSet(name=path.stem, source_path=path,
                             format="text", df=df)
                ds.columns = _introspect_columns(df)
                ds.metadata["extraction"] = "numeric_regex"
                return ds

            ds = DataSet(name=path.stem, source_path=path, format="text", df=None)
            ds.load_error = "No numeric data found in text file"
            return ds
        except Exception as e:
            ds = DataSet(name=path.stem, source_path=path, format="text", df=None)
            ds.load_error = str(e)
            return ds

    # ── Excel ─────────────────────────────────────────────────

    @classmethod
    def _load_excel(cls, path: Path, sheet_name: int = 0, **kwargs) -> DataSet:
        if not _PANDAS_OK:
            ds = DataSet(name=path.stem, source_path=path, format="excel", df=None)
            ds.load_error = "pandas not installed"
            return ds
        try:
            xl = pd.ExcelFile(path)
            sheets = xl.sheet_names
            df = pd.read_excel(path, sheet_name=sheet_name, **kwargs)
            ds = DataSet(name=path.stem, source_path=path, format="excel", df=df)
            ds.columns = _introspect_columns(df)
            ds.metadata["sheets"] = sheets
            ds.metadata["active_sheet"] = sheets[sheet_name] if isinstance(sheet_name, int) else sheet_name
            return ds
        except Exception as e:
            ds = DataSet(name=path.stem, source_path=path, format="excel", df=None)
            ds.load_error = str(e)
            return ds

    # ── JSON ──────────────────────────────────────────────────

    @classmethod
    def _load_json(cls, path: Path, **kwargs) -> DataSet:
        if not _PANDAS_OK:
            ds = DataSet(name=path.stem, source_path=path, format="json", df=None)
            ds.load_error = "pandas not installed"
            return ds
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))

            # Try various JSON shapes
            if isinstance(raw, list) and raw and isinstance(raw[0], dict):
                df = pd.DataFrame(raw)
            elif isinstance(raw, dict):
                # Check if values are all lists (columnar format)
                if all(isinstance(v, list) for v in raw.values()):
                    df = pd.DataFrame(raw)
                # Check if it has a "data" key
                elif "data" in raw and isinstance(raw["data"], list):
                    df = pd.DataFrame(raw["data"])
                else:
                    # Flatten one level
                    df = pd.json_normalize(raw)
            else:
                df = pd.DataFrame({"value": raw if isinstance(raw, list) else [raw]})

            ds = DataSet(name=path.stem, source_path=path, format="json", df=df)
            ds.columns = _introspect_columns(df)
            return ds
        except Exception as e:
            ds = DataSet(name=path.stem, source_path=path, format="json", df=None)
            ds.load_error = str(e)
            return ds

    # ── NumPy ─────────────────────────────────────────────────

    @classmethod
    def _load_numpy(cls, path: Path, **kwargs) -> DataSet:
        if not _PANDAS_OK:
            ds = DataSet(name=path.stem, source_path=path, format="numpy", df=None)
            ds.load_error = "pandas not installed"
            return ds
        try:
            if path.suffix.lower() == ".npy":
                arr = np.load(path, allow_pickle=False)
                if arr.ndim == 1:
                    df = pd.DataFrame({"values": arr})
                elif arr.ndim == 2:
                    df = pd.DataFrame(arr,
                                      columns=[f"col_{i}" for i in range(arr.shape[1])])
                else:
                    # Flatten higher dims to 2D
                    df = pd.DataFrame(arr.reshape(arr.shape[0], -1))
                    df.columns = [f"feat_{i}" for i in range(df.shape[1])]
                metadata = {"original_shape": list(arr.shape), "dtype": str(arr.dtype)}
            else:  # .npz
                npz = np.load(path, allow_pickle=False)
                arrays = {k: npz[k] for k in npz.files}
                metadata = {k: {"shape": list(v.shape), "dtype": str(v.dtype)}
                            for k, v in arrays.items()}
                # Put first 1D/2D array into df, rest as metadata
                df = None
                for k, arr in arrays.items():
                    if arr.ndim == 1:
                        df = pd.DataFrame({"values": arr})
                        break
                    elif arr.ndim == 2:
                        df = pd.DataFrame(arr,
                                          columns=[f"{k}_{i}" for i in range(arr.shape[1])])
                        break
                if df is None:
                    ds = DataSet(name=path.stem, source_path=path,
                                 format="numpy", df=None)
                    ds.load_error = "No 1D/2D arrays found in .npz"
                    return ds

            ds = DataSet(name=path.stem, source_path=path,
                         format="numpy", df=df)
            ds.columns = _introspect_columns(df)
            ds.metadata.update(metadata)
            return ds
        except Exception as e:
            ds = DataSet(name=path.stem, source_path=path, format="numpy", df=None)
            ds.load_error = str(e)
            return ds


# ============================================================
# Vault scanner
# ============================================================

def scan_vault_for_data(vault_dir: Path) -> List[Path]:
    """
    Return all loadable data files in the vault, sorted by modification time.
    """
    files: List[Path] = []
    for p in vault_dir.rglob("*"):
        if p.is_file() and DataLoader.can_load(p):
            # Skip hidden dirs
            if any(part.startswith(".") for part in p.parts):
                continue
            files.append(p)
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
