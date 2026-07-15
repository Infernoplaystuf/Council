"""
plot_roles.py — what KIND of thing is each column?

Every column gets exactly one role: datetime / boolean / numeric / categorical
/ text. The plot registry (plot_registry.py) uses those roles to decide which
plots are even meaningful for a given column selection, so the user is only
offered charts that can actually be drawn from their data.

Offline, read-only, pandas-only. Nothing here mutates the caller's frame.

Ordering matters more than it looks:

  * datetime is tested FIRST. graph_data._introspect_columns tests a
    >70%-to_numeric-coercion rule before its datetime branch, and
    pd.to_numeric SUCCEEDS on real datetime64 values — so dates were coming
    back as 'numeric' with epoch-nanosecond min/max (min=1.7e18), which then
    got offered as a Y axis and fed to the model as a value range. Every
    time-series feature was quietly undermined by that one ordering.

  * boolean is tested BEFORE numeric, because pandas' is_numeric_dtype()
    returns True for a bool Series. Testing numeric first (the obvious order)
    silently classifies every True/False column as numeric.
"""
from __future__ import annotations

import warnings
from typing import Dict, List

try:
    import pandas as pd
    from pandas.api import types as pdt
    _PANDAS_OK = True
except ImportError:                                    # pragma: no cover
    _PANDAS_OK = False

DATETIME = "datetime"
BOOLEAN = "boolean"
NUMERIC = "numeric"
CATEGORICAL = "categorical"
TEXT = "text"

ROLES = (DATETIME, BOOLEAN, NUMERIC, CATEGORICAL, TEXT)

# A string column is only re-read as dates when this share of its non-empty
# values parse. Below it, the column is far more likely to be free text that
# happens to contain a few date-ish tokens.
_DATE_PARSE_RATIO = 0.8
# Splitting categorical from text: few distinct values relative to the rows.
_CAT_MAX_UNIQUE = 20
_CAT_MAX_RATIO = 0.05


def _looks_like_dates(s) -> bool:
    """True when an object/string column parses cleanly as dates.

    Guarded hard: pd.to_datetime is happy to turn plain integers into 1970-era
    timestamps, so only genuine string columns are probed, and only when most
    of their values parse."""
    if not pdt.is_object_dtype(s) and not pdt.is_string_dtype(s):
        return False
    non_null = s.dropna()
    if non_null.empty:
        return False
    sample = non_null.head(200)
    # All-digit strings ("2024", "10") parse as dates but almost never are.
    text = sample.astype(str)
    if text.str.fullmatch(r"\s*\d+(\.\d+)?\s*").all():
        return False
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            parsed = pd.to_datetime(sample, errors="coerce")
        except Exception:
            return False
    return (parsed.notna().sum() / len(sample)) >= _DATE_PARSE_RATIO


def coerce_datetime_columns(df):
    """A COPY of ``df`` with string date columns parsed to real datetimes.

    CSVs are read without parse_dates, so dates arrive as strings and classify
    as categorical/text. Call this once after loading and the whole time-series
    half of the registry (timeseries, rolling mean, lag, autocorrelation,
    resample) becomes available."""
    if not _PANDAS_OK or df is None:
        return df
    out = df.copy()
    for col in out.columns:
        try:
            if _looks_like_dates(out[col]):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    out[col] = pd.to_datetime(out[col], errors="coerce")
        except Exception:
            continue
    return out


def infer_roles(df, *, coerce_dates: bool = False) -> Dict[str, str]:
    """Map every column of ``df`` to exactly one role in ROLES.

    ``coerce_dates`` probes string columns for dates WITHOUT modifying the
    frame — use it when you want the roles to reflect what the data means
    rather than how it happened to be parsed."""
    if not _PANDAS_OK or df is None:
        return {}
    roles: Dict[str, str] = {}
    n = len(df)
    for col in df.columns:
        s = df[col]
        try:
            if pdt.is_datetime64_any_dtype(s):
                roles[col] = DATETIME
            elif pdt.is_bool_dtype(s):          # BEFORE numeric: bool is numeric
                roles[col] = BOOLEAN
            elif pdt.is_numeric_dtype(s):
                roles[col] = NUMERIC
            elif coerce_dates and _looks_like_dates(s):
                roles[col] = DATETIME
            else:
                nun = s.nunique(dropna=True)
                limit = max(_CAT_MAX_UNIQUE, _CAT_MAX_RATIO * n)
                roles[col] = CATEGORICAL if nun <= limit else TEXT
        except Exception:
            roles[col] = TEXT
    return roles


def columns_with_role(roles: Dict[str, str], role: str) -> List[str]:
    """Every column having ``role``, in frame order."""
    return [c for c, r in roles.items() if r == role]


def count_role(roles: Dict[str, str], cols, role: str) -> int:
    """How many of ``cols`` have ``role`` — the primitive the registry's
    applicability rules are written in."""
    return sum(1 for c in cols if roles.get(c) == role)
