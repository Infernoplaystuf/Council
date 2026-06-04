"""
analyst_helpers/spc.py — Statistical Process Control helpers.

The four functions in this module:

  process_capability(series, lsl=None, usl=None, subgroup_size=None)
      → Cp / Cpk (short-term) + Pp / Ppk (long-term) + normality.
        Both lsl and usl may be None for one-sided specs.

  control_chart_limits(series, chart_type='xbar', subgroup_size=None)
      → center / UCL / LCL using hardcoded NIST/ASTM constants for
        X-bar, R, Individuals, Moving Range, p, and np charts.

  western_electric_rules(series, ucl, lcl, center)
      → One row per Nelson/WE rule violation: index, rule_number,
        description, zone.

  gage_rr(df, part_col, operator_col, measurement_col, tolerance=None)
      → ANOVA-method Gage R&R. EXISTS but is NOT registered in the
        sandbox per the project owner. See __init__.py.

Conventions shared with vault_analyst's older helpers:
  • Series / file-path / array-like input — _coerce_series normalises.
  • NaN is DROPPED (count surfaced via n_dropped_nan / warnings); the
    helper never silently imputes.
  • Outputs are dicts or DataFrames. Never print, never write.
  • All constants hardcoded from public standards. No network calls.

Sigma-estimation note for process_capability:
  Short-term sigma (used for Cp/Cpk) is estimated from R-bar / d2 when
  subgroup_size is provided. Long-term sigma (used for Pp/Ppk) is the
  sample standard deviation. Without subgroup_size, Cp/Cpk are None —
  the helper does NOT fall back to long-term and label it "short-term"
  because that's the classic Cpk reporting trap.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd


# ============================================================
# Hardcoded SPC constants — NIST/ASTM / Montgomery 7th ed.
# ============================================================

# d2 — divisor that converts R-bar to short-term sigma. Indexed by
# subgroup size n (2..25). Values per Montgomery Appendix Table VI.
_D2_CONSTANTS: Dict[int, float] = {
    2: 1.128,  3: 1.693,  4: 2.059,  5: 2.326,  6: 2.534,
    7: 2.704,  8: 2.847,  9: 2.970, 10: 3.078, 11: 3.173,
    12: 3.258, 13: 3.336, 14: 3.407, 15: 3.472, 16: 3.532,
    17: 3.588, 18: 3.640, 19: 3.689, 20: 3.735, 21: 3.778,
    22: 3.819, 23: 3.858, 24: 3.895, 25: 3.931,
}

# A2 — multiplier on R-bar for the X-bar chart's UCL / LCL.
#       UCL_xbar = grand_mean + A2 * R_bar
_A2_CONSTANTS: Dict[int, float] = {
    2: 1.880,  3: 1.023,  4: 0.729,  5: 0.577,  6: 0.483,
    7: 0.419,  8: 0.373,  9: 0.337, 10: 0.308, 11: 0.285,
    12: 0.266, 13: 0.249, 14: 0.235, 15: 0.223, 16: 0.212,
    17: 0.203, 18: 0.194, 19: 0.187, 20: 0.180, 21: 0.173,
    22: 0.167, 23: 0.162, 24: 0.157, 25: 0.153,
}

# D3 / D4 — multipliers on R-bar for the R chart's LCL / UCL.
_D3_CONSTANTS: Dict[int, float] = {
    2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0,
    7: 0.076, 8: 0.136, 9: 0.184, 10: 0.223, 11: 0.256,
    12: 0.283, 13: 0.307, 14: 0.328, 15: 0.347, 16: 0.363,
    17: 0.378, 18: 0.391, 19: 0.403, 20: 0.415, 21: 0.425,
    22: 0.434, 23: 0.443, 24: 0.451, 25: 0.459,
}
_D4_CONSTANTS: Dict[int, float] = {
    2: 3.267,  3: 2.574,  4: 2.282,  5: 2.114,  6: 2.004,
    7: 1.924,  8: 1.864,  9: 1.816, 10: 1.777, 11: 1.744,
    12: 1.717, 13: 1.693, 14: 1.672, 15: 1.653, 16: 1.637,
    17: 1.622, 18: 1.608, 19: 1.597, 20: 1.585, 21: 1.575,
    22: 1.566, 23: 1.557, 24: 1.548, 25: 1.541,
}

# E2 — multiplier for the Individuals chart (n=1 subgroups). Uses
# moving range of 2: UCL_I = mean + E2 * MR-bar. E2(2) = 2.660.
_E2_MR2: float = 2.660
# D4 for the moving-range chart at n=2 is 3.267 (matches _D4[2]).


# ============================================================
# Coercion — accept Series / array / DataFrame / file path
# ============================================================

ArrayLike = Union[pd.Series, pd.DataFrame, "np.ndarray", List, Tuple, str]


def _coerce_series(x: ArrayLike) -> np.ndarray:
    """Normalise the caller's input to a 1-D float numpy array with
    NaN preserved. Caller drops NaN and reports the count.

    Accepts:
      • pd.Series                — directly coerced via to_numeric
      • pd.DataFrame             — must have exactly one column
      • list / tuple / ndarray   — float-cast
      • str (.csv / .tsv path)   — reads first column. Matches the
        existing helpers' "accept a file path" convention.

    Raises ValueError on the obvious misuses (multi-column DataFrame,
    non-1-D ndarray, empty input).
    """
    # File-path shortcut. Bounded to short strings to avoid mistaking
    # long inline content for a path. The two-suffix check is enough
    # — passing arbitrary file extensions here would be a footgun
    # we don't want to expose.
    if isinstance(x, str) and len(x) < 4096 and x.endswith((".csv", ".tsv")):
        sep = "\t" if x.endswith(".tsv") else ","
        df = pd.read_csv(x, sep=sep)
        if df.shape[1] == 0:
            raise ValueError(f"{x!r} has no columns to coerce.")
        return pd.to_numeric(df.iloc[:, 0], errors="coerce").to_numpy()

    if isinstance(x, pd.Series):
        return pd.to_numeric(x, errors="coerce").to_numpy(dtype=float)

    if isinstance(x, pd.DataFrame):
        if x.shape[1] != 1:
            raise ValueError(
                "Helper expects a single-column input; got "
                f"{x.shape[1]} columns. Pass df[col] not the DataFrame."
            )
        return pd.to_numeric(x.iloc[:, 0], errors="coerce").to_numpy(dtype=float)

    arr = np.asarray(x, dtype=float)
    if arr.ndim != 1:
        raise ValueError(f"Expected 1-D input; got shape {arr.shape}.")
    if arr.size == 0:
        raise ValueError("Empty input.")
    return arr


# ============================================================
# Normality
# ============================================================

def _normality_test(arr: np.ndarray) -> Dict[str, Any]:
    """Shapiro-Wilk for n < 5000, Anderson-Darling otherwise.

    Returns:
        {"test": str, "p": float|nan, "ok": bool, "note": str,
         optionally "statistic" + "critical_5pct" for AD path}.

    For n < 8 the test power is too low to be meaningful — returns
    ok=True with a note steering the caller toward a probability
    plot or larger sample.
    """
    n = len(arr)
    if n < 8:
        return {
            "test": "shapiro",
            "p":    float("nan"),
            "ok":   True,
            "note": (f"n={n} too small for meaningful normality test — "
                     "assuming normal; verify with a probability plot."),
        }

    # scipy.stats is intentionally a lazy import — keeps the module
    # importable in environments where scipy didn't make it (e.g. a
    # malformed bundle). The caller surfaces a clean error then.
    from scipy import stats as _st

    if n < 5000:
        stat, p = _st.shapiro(arr)
        return {
            "test": "shapiro",
            "p":    float(p),
            "ok":   bool(p >= 0.05),
            "note": "",
        }
    # Anderson-Darling doesn't return a p-value directly; we compare
    # the statistic against scipy's tabulated 5 % critical value.
    result = _st.anderson(arr, dist="norm")
    crit5 = float(result.critical_values[2])    # 5% critical value
    return {
        "test":           "anderson-darling",
        "p":              float("nan"),
        "statistic":      float(result.statistic),
        "critical_5pct":  crit5,
        "ok":             bool(float(result.statistic) < crit5),
        "note":           "",
    }


# ============================================================
# 1. process_capability
# ============================================================

def process_capability(
    series: ArrayLike,
    lsl: Optional[float] = None,
    usl: Optional[float] = None,
    *,
    subgroup_size: Optional[int] = None,
) -> Dict[str, Any]:
    """Cp / Cpk (short-term) and Pp / Ppk (long-term) plus normality.

    Cpk on non-normal data is misleading — caller MUST check
    `normality_ok` before reporting Cpk to stakeholders. Warnings
    list flags issues the model should mention in its answer.

    Parameters
    ----------
    series : array-like or file path
        Measurement values. Single column. NaN values are dropped
        and reported via ``n_dropped_nan``.
    lsl : float, optional
        Lower specification limit. Pass None for upper-only specs.
    usl : float, optional
        Upper specification limit. Pass None for lower-only specs.
    subgroup_size : int, optional
        Rational subgroup size (2-25) used for the short-term sigma
        estimate via R-bar / d2. When omitted, Cp and Cpk come back
        as None and the helper relies on Pp / Ppk only. The classic
        "Cpk reported from total sigma" trap is avoided this way.

    Returns
    -------
    dict
        n, mean, std, min, max, lsl, usl, Cp, Cpk, Pp, Ppk,
        normality_test, normality_p, normality_ok, warnings,
        n_dropped_nan.

    Raises
    ------
    ValueError if neither lsl nor usl is supplied, if lsl >= usl,
    or if fewer than 2 finite values remain after NaN drop.
    """
    raw = _coerce_series(series)
    n_total = len(raw)
    mask = ~np.isnan(raw)
    arr = raw[mask]
    n_dropped = int(n_total - len(arr))

    out: Dict[str, Any] = {
        "n":              int(len(arr)),
        "n_dropped_nan":  n_dropped,
        "lsl":            lsl,
        "usl":            usl,
        "warnings":       [],
        "Cp":  None, "Cpk": None, "Pp": None, "Ppk": None,
    }

    if lsl is None and usl is None:
        raise ValueError("At least one of lsl / usl must be supplied.")
    if lsl is not None and usl is not None and lsl >= usl:
        raise ValueError(f"lsl ({lsl}) must be < usl ({usl}).")
    if len(arr) < 2:
        raise ValueError(f"Need ≥ 2 finite values; got {len(arr)}.")

    mu = float(np.mean(arr))
    sd = float(np.std(arr, ddof=1))   # sample stdev → long-term sigma
    out.update({
        "mean": mu, "std": sd,
        "min":  float(np.min(arr)),
        "max":  float(np.max(arr)),
    })

    if sd == 0:
        out["warnings"].append(
            "All values are identical (std=0) — capability indices "
            "are undefined.")
        norm = _normality_test(arr)
        out["normality_test"] = norm["test"]
        out["normality_p"]    = norm["p"]
        out["normality_ok"]   = norm["ok"]
        if norm.get("note"):
            out["warnings"].append(norm["note"])
        return out

    # ── Long-term (Pp, Ppk) — always defined when sd > 0 ──
    if lsl is not None and usl is not None:
        out["Pp"] = (usl - lsl) / (6.0 * sd)
    ppl = ((mu - lsl) / (3.0 * sd)) if lsl is not None else None
    ppu = ((usl - mu) / (3.0 * sd)) if usl is not None else None
    if ppl is not None and ppu is not None:
        out["Ppk"] = float(min(ppl, ppu))
    elif ppl is not None:
        out["Ppk"] = float(ppl)
    else:
        out["Ppk"] = float(ppu)

    # ── Short-term (Cp, Cpk) — needs subgroup_size to estimate sigma_within ──
    sigma_short: Optional[float] = None
    if subgroup_size is not None:
        if subgroup_size not in _D2_CONSTANTS:
            out["warnings"].append(
                f"subgroup_size={subgroup_size} outside the supported "
                "2-25 range; Cp / Cpk skipped (use Pp / Ppk for "
                "long-term assessment).")
        else:
            n_full = len(arr) // subgroup_size
            if n_full < 2:
                out["warnings"].append(
                    f"Only {n_full} full subgroup(s) of size "
                    f"{subgroup_size}; need ≥ 2 to estimate R-bar. "
                    "Cp / Cpk skipped.")
            else:
                trimmed = arr[:n_full * subgroup_size].reshape(
                    n_full, subgroup_size)
                ranges = trimmed.max(axis=1) - trimmed.min(axis=1)
                r_bar = float(np.mean(ranges))
                sigma_short = r_bar / _D2_CONSTANTS[subgroup_size]
                if n_full * subgroup_size < len(arr):
                    out["warnings"].append(
                        f"Dropped {len(arr) - n_full * subgroup_size} "
                        "trailing values that didn't fill a complete "
                        "subgroup.")
    else:
        out["warnings"].append(
            "subgroup_size not provided — Cp / Cpk skipped. Use "
            "Pp / Ppk for long-term assessment, or pass subgroup_size "
            "to get short-term capability.")

    if sigma_short is not None and sigma_short > 0:
        if lsl is not None and usl is not None:
            out["Cp"] = (usl - lsl) / (6.0 * sigma_short)
        cpl = ((mu - lsl) / (3.0 * sigma_short)) if lsl is not None else None
        cpu = ((usl - mu) / (3.0 * sigma_short)) if usl is not None else None
        if cpl is not None and cpu is not None:
            out["Cpk"] = float(min(cpl, cpu))
        elif cpl is not None:
            out["Cpk"] = float(cpl)
        else:
            out["Cpk"] = float(cpu)

    # ── Normality test — always run, gates whether Cpk is meaningful ──
    norm = _normality_test(arr)
    out["normality_test"] = norm["test"]
    out["normality_p"]    = norm["p"]
    out["normality_ok"]   = norm["ok"]
    if norm.get("note"):
        out["warnings"].append(norm["note"])
    if not norm["ok"]:
        out["warnings"].append(
            "Data is non-normal at α=0.05 — Cpk values are unreliable. "
            "Consider a non-normal capability method (Box-Cox transform "
            "or fitting the appropriate distribution) before reporting "
            "Cpk to stakeholders.")
    return out


# ============================================================
# 2. control_chart_limits
# ============================================================

def control_chart_limits(
    series: ArrayLike,
    chart_type: str = "xbar",
    subgroup_size: Optional[int] = None,
) -> Dict[str, Any]:
    """Compute SPC chart center line and 3-sigma control limits.

    Parameters
    ----------
    series : array-like or file path
        Raw measurements. Layout depends on chart_type:
        • xbar / r          — flat 1-D series; reshape into subgroups
                              of size `subgroup_size`.
        • i (individuals)   — flat 1-D series; treated one-at-a-time.
        • mr (moving range) — flat 1-D series; moving range of 2.
        • p (proportion)    — series of proportions (0..1) per subgroup.
        • np (count)        — series of nonconforming counts per subgroup.
    chart_type : str
        One of 'xbar', 'r', 'i', 'mr', 'p', 'np'.
    subgroup_size : int, optional
        Required for 'xbar', 'r', 'p', 'np'. Must be 2-25.

    Returns
    -------
    dict
        ``{'center': float, 'ucl': float, 'lcl': float,
           'chart_type': str, 'n_subgroups': int,
           'constants_used': {<name>: float, ...}}``

    Raises
    ------
    ValueError on unknown chart_type, missing subgroup_size where
    required, or insufficient data.
    """
    raw = _coerce_series(series)
    arr = raw[~np.isnan(raw)]
    chart = chart_type.lower().strip()

    if chart in ("xbar", "r"):
        if subgroup_size is None or subgroup_size not in _D2_CONSTANTS:
            raise ValueError(
                f"chart_type={chart!r} requires subgroup_size 2-25; "
                f"got {subgroup_size!r}.")
        n_full = len(arr) // subgroup_size
        if n_full < 2:
            raise ValueError(
                f"Need ≥ 2 full subgroups of size {subgroup_size}; "
                f"got {n_full}.")
        groups = arr[:n_full * subgroup_size].reshape(n_full, subgroup_size)
        x_bars = groups.mean(axis=1)
        ranges = groups.max(axis=1) - groups.min(axis=1)
        grand_mean = float(x_bars.mean())
        r_bar      = float(ranges.mean())

        if chart == "xbar":
            a2 = _A2_CONSTANTS[subgroup_size]
            return {
                "chart_type":  "xbar",
                "center":      grand_mean,
                "ucl":         grand_mean + a2 * r_bar,
                "lcl":         grand_mean - a2 * r_bar,
                "n_subgroups": int(n_full),
                "constants_used": {"A2": a2, "R_bar": r_bar},
            }
        # chart == "r"
        d3 = _D3_CONSTANTS[subgroup_size]
        d4 = _D4_CONSTANTS[subgroup_size]
        return {
            "chart_type":  "r",
            "center":      r_bar,
            "ucl":         d4 * r_bar,
            "lcl":         d3 * r_bar,
            "n_subgroups": int(n_full),
            "constants_used": {"D3": d3, "D4": d4, "R_bar": r_bar},
        }

    if chart == "i":
        if len(arr) < 2:
            raise ValueError(f"Individuals chart needs ≥ 2 values; got {len(arr)}.")
        mr = np.abs(np.diff(arr))
        mr_bar = float(mr.mean())
        mean = float(arr.mean())
        return {
            "chart_type":  "i",
            "center":      mean,
            "ucl":         mean + _E2_MR2 * mr_bar,
            "lcl":         mean - _E2_MR2 * mr_bar,
            "n_subgroups": int(len(arr)),
            "constants_used": {"E2": _E2_MR2, "MR_bar": mr_bar},
        }

    if chart == "mr":
        if len(arr) < 2:
            raise ValueError(f"Moving-range chart needs ≥ 2 values; got {len(arr)}.")
        mr = np.abs(np.diff(arr))
        mr_bar = float(mr.mean())
        # D4 at n=2 = 3.267, D3 at n=2 = 0
        return {
            "chart_type":  "mr",
            "center":      mr_bar,
            "ucl":         _D4_CONSTANTS[2] * mr_bar,
            "lcl":         0.0,
            "n_subgroups": int(len(mr)),
            "constants_used": {"D4_n2": _D4_CONSTANTS[2], "MR_bar": mr_bar},
        }

    if chart == "p":
        if subgroup_size is None or subgroup_size < 1:
            raise ValueError("p-chart requires subgroup_size >= 1.")
        p_bar = float(arr.mean())
        spread = 3.0 * math.sqrt(max(0.0, p_bar * (1 - p_bar) / subgroup_size))
        return {
            "chart_type":  "p",
            "center":      p_bar,
            "ucl":         min(1.0, p_bar + spread),
            "lcl":         max(0.0, p_bar - spread),
            "n_subgroups": int(len(arr)),
            "constants_used": {"p_bar": p_bar, "n": subgroup_size},
        }

    if chart == "np":
        if subgroup_size is None or subgroup_size < 1:
            raise ValueError("np-chart requires subgroup_size >= 1.")
        np_bar = float(arr.mean())
        p_bar = np_bar / subgroup_size
        spread = 3.0 * math.sqrt(max(0.0, np_bar * (1 - p_bar)))
        return {
            "chart_type":  "np",
            "center":      np_bar,
            "ucl":         np_bar + spread,
            "lcl":         max(0.0, np_bar - spread),
            "n_subgroups": int(len(arr)),
            "constants_used": {"np_bar": np_bar, "p_bar": p_bar, "n": subgroup_size},
        }

    raise ValueError(
        f"Unknown chart_type={chart_type!r}. Expected one of "
        "'xbar', 'r', 'i', 'mr', 'p', 'np'.")


# ============================================================
# 3. western_electric_rules
# ============================================================

def western_electric_rules(
    series: ArrayLike,
    ucl: float,
    lcl: float,
    center: float,
) -> pd.DataFrame:
    """Apply the classic Western Electric / Nelson rules to a series.

    Returns one DataFrame row per VIOLATION with columns:
        index           int    — position in the original series
        value           float  — value at that position
        rule_number     int    — 1, 2, 3, or 4
        description     str    — human-readable rule text
        zone            str    — 'A' (>2σ), 'B' (1-2σ), 'C' (<1σ),
                                 'beyond' (>3σ)
        side            str    — 'above' or 'below' center

    Rules (3-sigma chart assumed; sigma derived as (UCL - center)/3):

        1. One point beyond 3σ from center.
        2. Two of three consecutive points beyond 2σ on the same side.
        3. Four of five consecutive points beyond 1σ on the same side.
        4. Eight consecutive points on the same side of center.
    """
    arr = _coerce_series(series)
    if math.isnan(ucl) or math.isnan(lcl) or math.isnan(center):
        raise ValueError("ucl / lcl / center must all be finite numbers.")
    if ucl <= center or lcl >= center:
        raise ValueError(
            f"Expected lcl < center < ucl; got lcl={lcl}, "
            f"center={center}, ucl={ucl}.")
    sigma = (ucl - center) / 3.0
    if sigma <= 0:
        raise ValueError(f"Derived sigma <= 0 from UCL-center={ucl-center}.")

    # Pre-compute per-point classifications.
    zones: List[str]      = []
    sides: List[str]      = []   # 'above' / 'below' / 'on'
    z_scores: List[float] = []
    for v in arr:
        if math.isnan(v):
            zones.append("nan"); sides.append("on"); z_scores.append(float("nan"))
            continue
        z = (v - center) / sigma
        if abs(z) > 3.0:
            zones.append("beyond")
        elif abs(z) > 2.0:
            zones.append("A")
        elif abs(z) > 1.0:
            zones.append("B")
        else:
            zones.append("C")
        sides.append("above" if z > 0 else ("below" if z < 0 else "on"))
        z_scores.append(z)

    violations: List[Dict[str, Any]] = []

    # Rule 1 — single point beyond 3σ.
    for i, z in enumerate(zones):
        if z == "beyond":
            violations.append({
                "index":       int(i),
                "value":       float(arr[i]),
                "rule_number": 1,
                "description": "Single point beyond 3σ (outside control limits)",
                "zone":        z,
                "side":        sides[i],
            })

    def _trailing_window(i: int, k: int) -> List[int]:
        """Indices [i-k+1 .. i] clamped to valid range."""
        start = max(0, i - k + 1)
        return list(range(start, i + 1))

    # Rule 2 — 2 of 3 consecutive points beyond 2σ on the SAME side.
    for i in range(len(arr)):
        idx_window = _trailing_window(i, 3)
        if len(idx_window) < 3:
            continue
        for side in ("above", "below"):
            beyond_2sig = sum(
                1 for j in idx_window
                if sides[j] == side and zones[j] in ("A", "beyond")
            )
            if beyond_2sig >= 2 and sides[i] == side and zones[i] in ("A", "beyond"):
                violations.append({
                    "index":       int(i),
                    "value":       float(arr[i]),
                    "rule_number": 2,
                    "description": (
                        f"2 of 3 consecutive points beyond 2σ "
                        f"({side} center)"),
                    "zone":        zones[i],
                    "side":        side,
                })
                break  # one violation report per index per rule

    # Rule 3 — 4 of 5 consecutive points beyond 1σ on the SAME side.
    for i in range(len(arr)):
        idx_window = _trailing_window(i, 5)
        if len(idx_window) < 5:
            continue
        for side in ("above", "below"):
            beyond_1sig = sum(
                1 for j in idx_window
                if sides[j] == side and zones[j] in ("B", "A", "beyond")
            )
            if (beyond_1sig >= 4 and sides[i] == side
                    and zones[i] in ("B", "A", "beyond")):
                violations.append({
                    "index":       int(i),
                    "value":       float(arr[i]),
                    "rule_number": 3,
                    "description": (
                        f"4 of 5 consecutive points beyond 1σ "
                        f"({side} center)"),
                    "zone":        zones[i],
                    "side":        side,
                })
                break

    # Rule 4 — 8 consecutive points on the same side of center.
    for i in range(len(arr)):
        idx_window = _trailing_window(i, 8)
        if len(idx_window) < 8:
            continue
        for side in ("above", "below"):
            if all(sides[j] == side for j in idx_window):
                violations.append({
                    "index":       int(i),
                    "value":       float(arr[i]),
                    "rule_number": 4,
                    "description": f"8 consecutive points on the same side of center ({side})",
                    "zone":        zones[i],
                    "side":        side,
                })
                break

    if not violations:
        return pd.DataFrame(columns=[
            "index", "value", "rule_number", "description", "zone", "side"
        ])
    return pd.DataFrame(violations).sort_values(
        by=["index", "rule_number"]
    ).reset_index(drop=True)


# ============================================================
# 4. gage_rr — implemented but NOT registered in the sandbox.
#    See __init__.py / module header.
# ============================================================

def gage_rr(
    df: pd.DataFrame,
    part_col: str,
    operator_col: str,
    measurement_col: str,
    *,
    tolerance: Optional[float] = None,
) -> Dict[str, Any]:
    """ANOVA-method Gage R&R study.

    PROJECT NOTE: this function is implemented but NOT registered in
    the analyst sandbox. To enable, add ``"gage_rr": gage_rr`` to the
    SPC block in analyst_helpers/__init__.py and update the analyst
    prompt + smoke tests in lockstep.

    Requires a balanced (or near-balanced) parts × operators ×
    replicates layout. Raises ValueError on unbalanced data rather
    than silently imputing — Gage R&R math fundamentally needs
    balance and quiet imputation produces misleading variance
    components.

    Parameters
    ----------
    df : pd.DataFrame
    part_col, operator_col, measurement_col : str
    tolerance : float, optional
        Engineering tolerance (USL - LSL). When supplied, the result
        includes ``pct_tolerance = 100 * 6 * total_grr_sd / tolerance``.

    Returns
    -------
    dict with repeatability_sd, reproducibility_sd, part_to_part_sd,
    total_grr_sd, total_variation_sd, pct_study_var_grr / repeat /
    reprod, pct_tolerance (or None), ndc (number of distinct
    categories), n_parts, n_operators, n_replicates, anova_table
    DataFrame, warnings list.
    """
    for col in (part_col, operator_col, measurement_col):
        if col not in df.columns:
            raise ValueError(f"Column {col!r} not in DataFrame.")

    # Drop NaN in any of the three columns with a clear count surface.
    sub = df[[part_col, operator_col, measurement_col]].copy()
    n_total = len(sub)
    sub = sub.dropna()
    n_dropped = n_total - len(sub)
    warnings_list: List[str] = []
    if n_dropped:
        warnings_list.append(f"Dropped {n_dropped} row(s) with NaN.")
    if len(sub) < 4:
        raise ValueError(f"Need ≥ 4 rows after NaN drop; got {len(sub)}.")

    # Verify balance — every (part, operator) cell must have the same
    # number of measurements. Unbalanced designs need a different ANOVA
    # path that we don't ship here.
    counts = sub.groupby([part_col, operator_col]).size()
    if counts.nunique() != 1:
        raise ValueError(
            "Gage R&R requires a balanced design — every "
            "(part, operator) cell must contain the same number of "
            f"measurements. Got counts: {counts.value_counts().to_dict()}."
        )
    n_replicates = int(counts.iloc[0])
    n_parts      = int(sub[part_col].nunique())
    n_operators  = int(sub[operator_col].nunique())
    if n_replicates < 2:
        raise ValueError(
            f"Need ≥ 2 replicates per (part, operator) cell; got "
            f"{n_replicates}. Run the study with more measurements.")
    if n_parts < 2 or n_operators < 2:
        raise ValueError(
            f"Need ≥ 2 parts and ≥ 2 operators; got "
            f"{n_parts} parts × {n_operators} operators.")

    grand_mean = float(sub[measurement_col].mean())

    # Sum of squares decomposition (Type I, ANOVA Gage R&R).
    part_means     = sub.groupby(part_col)[measurement_col].mean()
    operator_means = sub.groupby(operator_col)[measurement_col].mean()
    cell_means     = sub.groupby([part_col, operator_col])[measurement_col].mean()

    ss_parts = (n_operators * n_replicates
                * ((part_means - grand_mean) ** 2).sum())
    ss_operators = (n_parts * n_replicates
                    * ((operator_means - grand_mean) ** 2).sum())

    # Interaction: ss_pxo = n_replicates * Σ (cell_mean - part_mean - op_mean + grand_mean)^2
    interaction_terms = []
    for (p, o), cm in cell_means.items():
        pm = part_means[p]
        om = operator_means[o]
        interaction_terms.append((cm - pm - om + grand_mean) ** 2)
    ss_pxo = float(n_replicates * sum(interaction_terms))

    # Total SS
    ss_total = float(((sub[measurement_col] - grand_mean) ** 2).sum())
    ss_repeat = float(ss_total - ss_parts - ss_operators - ss_pxo)

    # Degrees of freedom
    df_parts     = n_parts - 1
    df_operators = n_operators - 1
    df_pxo       = (n_parts - 1) * (n_operators - 1)
    df_repeat    = n_parts * n_operators * (n_replicates - 1)
    df_total     = n_parts * n_operators * n_replicates - 1

    # Mean squares
    ms_parts     = ss_parts     / df_parts     if df_parts     else 0.0
    ms_operators = ss_operators / df_operators if df_operators else 0.0
    ms_pxo       = ss_pxo       / df_pxo       if df_pxo       else 0.0
    ms_repeat    = ss_repeat    / df_repeat    if df_repeat    else 0.0

    # Variance components (AIAG style; ignore interaction if F(pxo) NS).
    var_repeat = ms_repeat
    # Operator-by-part interaction
    var_pxo = max(0.0, (ms_pxo - ms_repeat) / n_replicates)
    var_op  = max(0.0, (ms_operators - ms_pxo) / (n_parts * n_replicates))
    var_part = max(0.0, (ms_parts - ms_pxo) / (n_operators * n_replicates))

    var_reprod = var_op + var_pxo
    var_grr    = var_repeat + var_reprod
    var_total  = var_grr + var_part

    sd_repeat  = math.sqrt(var_repeat)
    sd_reprod  = math.sqrt(var_reprod)
    sd_part    = math.sqrt(var_part)
    sd_grr     = math.sqrt(var_grr)
    sd_total   = math.sqrt(var_total)

    # 5.15 × σ is the AIAG default total study variation span.
    pct_grr     = (5.15 * sd_grr    / (5.15 * sd_total)) * 100 if sd_total > 0 else float("nan")
    pct_repeat  = (5.15 * sd_repeat / (5.15 * sd_total)) * 100 if sd_total > 0 else float("nan")
    pct_reprod  = (5.15 * sd_reprod / (5.15 * sd_total)) * 100 if sd_total > 0 else float("nan")
    pct_tol     = ((6.0 * sd_grr / tolerance) * 100
                    if (tolerance is not None and tolerance > 0) else None)

    # ndc = 1.41 * (σ_part / σ_grr)
    ndc = 1.41 * (sd_part / sd_grr) if sd_grr > 0 else float("nan")

    # F-stats + p-values for the ANOVA table (lazy scipy import)
    from scipy import stats as _st
    f_parts     = (ms_parts     / ms_pxo)     if ms_pxo     else float("nan")
    f_operators = (ms_operators / ms_pxo)     if ms_pxo     else float("nan")
    f_pxo       = (ms_pxo       / ms_repeat)  if ms_repeat  else float("nan")
    p_parts     = (1 - _st.f.cdf(f_parts, df_parts, df_pxo))         if df_pxo > 0 else float("nan")
    p_operators = (1 - _st.f.cdf(f_operators, df_operators, df_pxo)) if df_pxo > 0 else float("nan")
    p_pxo       = (1 - _st.f.cdf(f_pxo, df_pxo, df_repeat))          if df_repeat > 0 else float("nan")

    anova = pd.DataFrame([
        {"source": "Parts",        "SS": ss_parts,     "df": df_parts,
         "MS": ms_parts,     "F": f_parts,     "p": p_parts},
        {"source": "Operators",    "SS": ss_operators, "df": df_operators,
         "MS": ms_operators, "F": f_operators, "p": p_operators},
        {"source": "Parts*Op",     "SS": ss_pxo,       "df": df_pxo,
         "MS": ms_pxo,       "F": f_pxo,       "p": p_pxo},
        {"source": "Repeatability","SS": ss_repeat,    "df": df_repeat,
         "MS": ms_repeat,    "F": float("nan"),"p": float("nan")},
        {"source": "Total",        "SS": ss_total,     "df": df_total,
         "MS": float("nan"), "F": float("nan"),"p": float("nan")},
    ])

    return {
        "repeatability_sd":      sd_repeat,
        "reproducibility_sd":    sd_reprod,
        "part_to_part_sd":       sd_part,
        "total_grr_sd":          sd_grr,
        "total_variation_sd":    sd_total,
        "pct_study_var_grr":     pct_grr,
        "pct_study_var_repeat":  pct_repeat,
        "pct_study_var_reprod":  pct_reprod,
        "pct_tolerance":         pct_tol,
        "ndc":                   ndc,
        "n_parts":               n_parts,
        "n_operators":           n_operators,
        "n_replicates":          n_replicates,
        "anova_table":           anova,
        "warnings":              warnings_list,
    }
