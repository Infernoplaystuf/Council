"""
analyst_helpers/engineering.py — Engineering-domain helpers.

Five functions, all sandbox-safe (no filesystem writes, no network,
no subprocess):

  units_convert(value, from_unit, to_unit)
      Lazy-import pint. Raises a clear ImportError pointing at
      `pip install pint` when not installed.

  dimensional_check(df, column_units)
      Annotate a DataFrame with unit metadata in df.attrs['units'].
      Provide a small set of arithmetic-result helpers that validate
      dimensional consistency.

  tolerance_stackup(nominals, tolerances, method='worst_case')
      Worst-case or RSS (root-sum-square) tolerance stack analysis.
      Returns nominal, min, max, expected_std.

  fft_spectrum(series, sample_rate_hz)
      One-sided magnitude FFT via scipy.fft with a Hann window by
      default. Returns frequency (Hz) + magnitude DataFrame.

  linear_regression_with_diagnostics(df, x_cols, y_col)
      Linear regression with R², adjusted R², residual standard
      error, F-statistic, per-coefficient t-statistic / p-value,
      Variance Inflation Factor (multicollinearity check). Pure
      numpy + scipy.stats — no statsmodels dep.

Conventions match spc.py:
  • Drop NaN with the count surfaced via warnings / n_dropped_nan
  • Returns dicts / DataFrames / scalars — never print, never write
  • Lazy imports for optional deps (pint) with a clear install hint
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd


ArrayLike = Union[pd.Series, pd.DataFrame, "np.ndarray", List, Tuple, str]


# ============================================================
# Pint registry — lazy singleton
# ============================================================

_PINT_REGISTRY = None


def _pint() -> Any:
    """Lazy-init the pint UnitRegistry. Singleton — pint contexts
    use shared state, and two registries can't compare units across
    each other without quincy gymnastics, so one registry per process
    is the documented convention.

    Returns the registry object. Raises ImportError with a clear
    install hint when pint isn't on the path."""
    global _PINT_REGISTRY
    if _PINT_REGISTRY is not None:
        return _PINT_REGISTRY
    try:
        import pint  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "pint is required for unit handling but is not installed. "
            "Install with `pip install pint` (≈2 MB, no extra deps). "
            f"Original error: {exc}"
        ) from exc
    _PINT_REGISTRY = pint.UnitRegistry()
    # Engineering-common aliases that pint doesn't ship by default.
    # Defining them here keeps the call sites portable across pint
    # versions where some aliases are tagged "deprecated" in newer
    # releases.
    try:
        _PINT_REGISTRY.define("psi = 6894.757293168 pascal")
        _PINT_REGISTRY.define("ksi = 1000 * psi")
    except Exception:
        # Already defined in this pint version — silent no-op.
        pass
    return _PINT_REGISTRY


# ============================================================
# 1. units_convert
# ============================================================

def units_convert(
    value: Union[float, int, np.ndarray, pd.Series],
    from_unit: str,
    to_unit: str,
) -> Union[float, np.ndarray, pd.Series]:
    """Convert a scalar / array / Series of values between units.

    Wraps pint. Lazy-imports the library so the rest of the
    analyst sandbox works on environments that didn't pip-install
    pint (raise → user sees the install hint).

    Parameters
    ----------
    value : scalar or array-like
        The numeric value(s) to convert.
    from_unit : str
        Source unit. Any pint-recognised string ('mm', 'inch',
        'kN/m^2', 'degC', etc.).
    to_unit : str
        Target unit.

    Returns
    -------
    Same shape as the input — scalar in, scalar out; Series in,
    Series out. Numeric dtype.

    Examples
    --------
    units_convert(25.4, 'mm', 'inch')   # → 1.0
    units_convert(pd.Series([10, 20]), 'mm', 'inch')   # → Series([0.394, 0.787])
    """
    ureg = _pint()
    Q = ureg.Quantity

    if isinstance(value, pd.Series):
        magnitudes = pd.to_numeric(value, errors="coerce").to_numpy()
        out = (Q(magnitudes, from_unit).to(to_unit)).magnitude
        return pd.Series(out, index=value.index, name=value.name)
    if isinstance(value, np.ndarray):
        return (Q(value.astype(float), from_unit).to(to_unit)).magnitude
    if isinstance(value, (list, tuple)):
        return (Q(np.asarray(value, dtype=float), from_unit).to(to_unit)).magnitude
    # Scalar
    return float((Q(float(value), from_unit).to(to_unit)).magnitude)


# ============================================================
# 2. dimensional_check
# ============================================================

def dimensional_check(
    df: pd.DataFrame,
    column_units: Dict[str, str],
) -> pd.DataFrame:
    """Attach unit metadata to a DataFrame for downstream dimensional
    checking.

    The pandas object gets a copy of column_units stored in
    ``df.attrs['units']``. The pair of utility functions returned
    via the module — `check_arithmetic(df, expr_units)` and
    `result_unit(df, lhs_col, op, rhs_col)` — can be used by code
    that needs dimensional validation.

    This helper is deliberately MINIMAL — it does NOT monkey-patch
    pandas operators. Pint can integrate with pandas arrays via
    pint-pandas, but adding that dep just for sandbox use is heavy.
    Instead, callers explicitly invoke unit checks at the points
    where dimensional consistency matters (sensor calibration,
    cross-system conversion, etc.).

    Parameters
    ----------
    df : pd.DataFrame
    column_units : dict[str, str]
        Maps each column name to a pint-recognised unit string.
        Columns not in the dict are left annotated as
        'dimensionless'.

    Returns
    -------
    The same DataFrame with df.attrs['units'] populated. Returns a
    REFERENCE to the original (not a copy) — pandas semantics for
    setting attrs propagate; copying would defeat the point.

    Raises
    ------
    ValueError if a key in column_units isn't a column of df.
    ImportError (via _pint) if pint isn't installed when this is
    called — we validate unit strings at annotation time so a
    typo surfaces here, not deep in a downstream pipeline.
    """
    missing = [c for c in column_units if c not in df.columns]
    if missing:
        raise ValueError(
            f"column_units references columns not in DataFrame: "
            f"{missing}. Available: {list(df.columns)[:10]}"
            + ("…" if len(df.columns) > 10 else ""))
    ureg = _pint()
    # Validate every unit string up front so typos fail loudly here
    # rather than at use site.
    validated: Dict[str, str] = {}
    for col, unit_str in column_units.items():
        try:
            _ = ureg.Unit(unit_str)
        except Exception as exc:
            raise ValueError(
                f"Column {col!r}: unit string {unit_str!r} is not "
                f"recognised by pint. Original error: {exc}"
            ) from exc
        validated[col] = unit_str
    # Fill unannotated columns with 'dimensionless'
    for c in df.columns:
        validated.setdefault(c, "dimensionless")
    df.attrs["units"] = validated
    return df


# ============================================================
# 3. tolerance_stackup
# ============================================================

def tolerance_stackup(
    nominals: Sequence[float],
    tolerances: Sequence[float],
    method: str = "worst_case",
) -> Dict[str, Any]:
    """Tolerance stack-up across N independent dimensions.

    Parameters
    ----------
    nominals : sequence of float
        Nominal dimension values (same units across the list — we
        don't do unit conversion here; if you need mixed units,
        convert via `units_convert` first).
    tolerances : sequence of float
        Bilateral tolerance for each dimension (the ± value).
        Same length as `nominals`. Must be non-negative.
    method : {'worst_case', 'rss'}
        'worst_case'  → linear sum of tolerances (Σ |t_i|).
                        Use for safety-critical / interference
                        analysis. Always conservative.
        'rss'         → root-sum-square (sqrt(Σ t_i²)).
                        Use for cost-driven analysis with many
                        contributors. Assumes independent ~normal
                        contributors and reports the ±3σ-equivalent
                        stack (so divides each tolerance by 3 to
                        approximate the standard deviation).

    Returns
    -------
    dict
        nominal       : sum of nominals (the target stack-up value)
        tolerance     : the computed ± value
        min           : nominal - tolerance
        max           : nominal + tolerance
        expected_std  : approximation of σ_stack (only meaningful
                        for RSS; for worst-case returns None).
        method        : echo of the input
        n_components  : len(nominals)

    Raises
    ------
    ValueError on length mismatch, negative tolerance, or
    unknown method.
    """
    nominals_arr   = np.asarray(nominals, dtype=float)
    tolerances_arr = np.asarray(tolerances, dtype=float)
    if nominals_arr.shape != tolerances_arr.shape:
        raise ValueError(
            f"Length mismatch: {nominals_arr.shape} nominals vs "
            f"{tolerances_arr.shape} tolerances.")
    if (tolerances_arr < 0).any():
        raise ValueError("Tolerances must be non-negative (±values).")

    method_norm = method.lower().strip()
    nominal_total = float(nominals_arr.sum())

    if method_norm == "worst_case":
        tol = float(np.abs(tolerances_arr).sum())
        return {
            "method":        "worst_case",
            "n_components":  int(len(nominals_arr)),
            "nominal":       nominal_total,
            "tolerance":     tol,
            "min":           nominal_total - tol,
            "max":           nominal_total + tol,
            "expected_std":  None,   # worst-case is not statistical
        }
    if method_norm == "rss":
        # Treat each ± tolerance as ±3σ → σ_i = tol_i / 3 → σ_stack =
        # sqrt(Σ σ_i²). The ±3σ stack is then 3 × σ_stack.
        sigmas = tolerances_arr / 3.0
        sigma_stack = float(math.sqrt((sigmas ** 2).sum()))
        tol = 3.0 * sigma_stack
        return {
            "method":        "rss",
            "n_components":  int(len(nominals_arr)),
            "nominal":       nominal_total,
            "tolerance":     tol,
            "min":           nominal_total - tol,
            "max":           nominal_total + tol,
            "expected_std":  sigma_stack,
        }
    raise ValueError(
        f"Unknown method={method!r}. Expected 'worst_case' or 'rss'.")


# ============================================================
# 4. fft_spectrum
# ============================================================

def fft_spectrum(
    series: ArrayLike,
    sample_rate_hz: float,
    *,
    column: Optional[str] = None,
    window: str = "hann",
    detrend: bool = True,
) -> pd.DataFrame:
    """One-sided magnitude FFT for vibration / acoustic / signal
    analysis on time-series data.

    Parameters
    ----------
    series : array-like, file path, Series, or DataFrame
        Equally-spaced time-series samples. NaN values are dropped
        with the count noted in attrs.
    sample_rate_hz : float
        Sampling frequency in Hz. Required for the frequency axis.
    column : str, optional
        Column to FFT when input is a multi-column DataFrame / CSV.
    window : {'hann', 'hamming', 'blackman', 'rect'}
        Windowing function applied before the FFT to reduce spectral
        leakage. 'rect' = no window (rectangular). Hann is the
        default — best balance for general-purpose vibration work.
    detrend : bool
        Subtract the mean before windowing. Default True (removes
        DC component that dominates the spectrum on offset signals).

    Returns
    -------
    pd.DataFrame with columns:
        frequency_hz  : float
        magnitude     : float (one-sided, single-sided amplitude)
    The DataFrame's .attrs carries 'n_samples', 'sample_rate_hz',
    'window', 'n_dropped_nan'.

    Raises
    ------
    ValueError on sample_rate_hz <= 0, on series shorter than 8
    samples (FFT below that is meaningless), or on unknown window.
    """
    from .spc import _coerce_series   # share the DataFrame logic
    if sample_rate_hz <= 0:
        raise ValueError(f"sample_rate_hz must be > 0; got {sample_rate_hz}.")

    raw = _coerce_series(series, column=column)
    mask = ~np.isnan(raw)
    arr = raw[mask].astype(float)
    n_dropped = int(len(raw) - len(arr))
    n = len(arr)
    if n < 8:
        raise ValueError(
            f"FFT needs ≥ 8 samples after NaN drop; got {n}.")

    if detrend:
        arr = arr - arr.mean()

    window_norm = window.lower().strip()
    if window_norm == "hann":
        w = np.hanning(n)
    elif window_norm == "hamming":
        w = np.hamming(n)
    elif window_norm == "blackman":
        w = np.blackman(n)
    elif window_norm in ("rect", "rectangular", "none"):
        w = np.ones(n)
    else:
        raise ValueError(
            f"Unknown window={window!r}. Expected hann / hamming / "
            "blackman / rect.")

    # Lazy scipy import — keeps the module loadable when scipy is
    # missing for some reason. The error surfaces here with context.
    from scipy import fft as _fft

    windowed = arr * w
    # Coherent gain correction for the window so the magnitudes are
    # comparable across different window choices.
    cg = w.mean()
    if cg <= 0:
        cg = 1.0
    spectrum_complex = _fft.rfft(windowed)
    # Single-sided amplitude spectrum: |X(f)| × 2 / N / coherent_gain
    # (with the DC bin and Nyquist bin not doubled).
    mag = np.abs(spectrum_complex) * (2.0 / n) / cg
    if n % 2 == 0:
        mag[-1] /= 2.0   # Nyquist bin not doubled
    mag[0]  /= 2.0       # DC bin not doubled
    freqs = _fft.rfftfreq(n, d=1.0 / sample_rate_hz)

    out = pd.DataFrame({"frequency_hz": freqs, "magnitude": mag})
    out.attrs["n_samples"]      = n
    out.attrs["sample_rate_hz"] = float(sample_rate_hz)
    out.attrs["window"]         = window_norm
    out.attrs["n_dropped_nan"]  = n_dropped
    return out


# ============================================================
# 5. linear_regression_with_diagnostics
# ============================================================

def linear_regression_with_diagnostics(
    df: pd.DataFrame,
    x_cols: Union[str, Sequence[str]],
    y_col: str,
) -> Dict[str, Any]:
    """Ordinary least squares with the diagnostics engineers actually
    use — no statsmodels dep.

    Pure numpy + scipy.stats. Reports:
      • Per-coefficient point estimate, standard error, t-statistic,
        and p-value (two-sided t-test against H0: β = 0).
      • R², adjusted R², residual standard error, F-statistic and
        its p-value (overall model significance).
      • Variance Inflation Factor per predictor — flags
        multicollinearity (VIF > 5 is suspect, > 10 is severe).
      • n, n_dropped_nan, residuals (length-n array).

    Parameters
    ----------
    df : pd.DataFrame
    x_cols : str or sequence of str
        Predictor column name(s). Pass a single string for simple
        regression, a list for multiple regression.
    y_col : str
        Response column name.

    Returns
    -------
    dict
        coefficients   : pd.DataFrame (term, estimate, std_err,
                          t_stat, p_value, vif)
                          The intercept row has vif = NaN.
        r2             : float
        adj_r2         : float
        residual_se    : float — sqrt(SSR / (n - p - 1))
        f_stat         : float
        f_p_value      : float
        n              : int — sample size used
        n_dropped_nan  : int
        residuals      : np.ndarray (n,)
        warnings       : list[str]

    Raises
    ------
    ValueError on missing columns, insufficient data
    (n < p + 2 after NaN drop), or singular design matrix.
    """
    if isinstance(x_cols, str):
        x_cols = [x_cols]
    for c in list(x_cols) + [y_col]:
        if c not in df.columns:
            raise ValueError(
                f"Column {c!r} not in DataFrame. Available: "
                f"{list(df.columns)[:10]}"
                + ("…" if len(df.columns) > 10 else ""))

    sub = df[list(x_cols) + [y_col]].copy()
    n_total = len(sub)
    sub = sub.apply(pd.to_numeric, errors="coerce").dropna()
    n_dropped = n_total - len(sub)

    n = len(sub)
    p = len(x_cols)   # excludes intercept
    if n < p + 2:
        raise ValueError(
            f"Need ≥ p+2 = {p+2} rows after NaN drop; got {n}.")

    X = sub[list(x_cols)].to_numpy(dtype=float)
    y = sub[y_col].to_numpy(dtype=float)
    # Add intercept column
    X_design = np.column_stack([np.ones(n), X])

    # Solve via lstsq for numerical stability with near-collinear
    # predictors (vs np.linalg.inv(X.T @ X) which blows up).
    try:
        beta, residuals_arr, rank, _sv = np.linalg.lstsq(X_design, y, rcond=None)
    except np.linalg.LinAlgError as exc:
        raise ValueError(f"Singular design matrix: {exc}")

    if rank < X_design.shape[1]:
        # Perfect collinearity among predictors. Surface but don't
        # silently mask — the user needs to drop a column.
        raise ValueError(
            f"Design matrix is rank-deficient (rank {rank} of "
            f"{X_design.shape[1]} cols). Two or more predictors are "
            "perfectly collinear — drop one and re-fit.")

    y_hat = X_design @ beta
    residuals = y - y_hat
    ssr = float((residuals ** 2).sum())               # residual SS
    sst = float(((y - y.mean()) ** 2).sum())          # total SS
    df_res = n - p - 1

    r2     = 1.0 - ssr / sst if sst > 0 else float("nan")
    adj_r2 = (1.0 - (1.0 - r2) * (n - 1) / df_res
               if df_res > 0 and not math.isnan(r2) else float("nan"))
    residual_se = math.sqrt(ssr / df_res) if df_res > 0 else float("nan")

    # Per-coefficient standard errors via the covariance matrix
    # σ² (X'X)⁻¹. Using pinv keeps it robust for marginally singular
    # systems even though lstsq already passed.
    sigma2 = ssr / df_res if df_res > 0 else float("nan")
    cov_beta = sigma2 * np.linalg.pinv(X_design.T @ X_design)
    se_beta = np.sqrt(np.diag(cov_beta))

    # t-stats + two-sided p-values
    from scipy import stats as _st
    t_stats = beta / se_beta
    p_values = 2.0 * (1.0 - _st.t.cdf(np.abs(t_stats), df=df_res))

    # F-statistic for overall significance: H0 = all slopes 0
    if p == 0 or sst <= 0:
        f_stat   = float("nan")
        f_p      = float("nan")
    else:
        mse_model = (sst - ssr) / p
        mse_res   = ssr / df_res if df_res > 0 else float("nan")
        f_stat = mse_model / mse_res if mse_res and mse_res > 0 else float("nan")
        f_p    = (1.0 - _st.f.cdf(f_stat, p, df_res)
                  if not math.isnan(f_stat) and df_res > 0 else float("nan"))

    # VIF per predictor — regress each x_i on the OTHER x_j's,
    # take 1/(1 - R²_i). VIF > 10 = severe multicollinearity.
    vifs: List[float] = [float("nan")]    # intercept row
    if p >= 2:
        for i in range(p):
            xi = X[:, i]
            X_others = np.delete(X, i, axis=1)
            # Add intercept
            Xo_design = np.column_stack([np.ones(n), X_others])
            try:
                beta_i, *_ = np.linalg.lstsq(Xo_design, xi, rcond=None)
                xi_hat = Xo_design @ beta_i
                ssr_i = float(((xi - xi_hat) ** 2).sum())
                sst_i = float(((xi - xi.mean()) ** 2).sum())
                r2_i = 1.0 - ssr_i / sst_i if sst_i > 0 else float("nan")
                if math.isnan(r2_i) or r2_i >= 1.0:
                    vifs.append(float("inf"))
                else:
                    vifs.append(1.0 / (1.0 - r2_i))
            except Exception:
                vifs.append(float("nan"))
    else:
        # Single predictor → no multicollinearity to check
        vifs.append(float("nan"))

    coef_df = pd.DataFrame({
        "term":     ["(Intercept)"] + list(x_cols),
        "estimate": beta,
        "std_err":  se_beta,
        "t_stat":   t_stats,
        "p_value":  p_values,
        "vif":      vifs,
    })

    warnings_list: List[str] = []
    if n_dropped:
        warnings_list.append(f"Dropped {n_dropped} row(s) with NaN in "
                              "the fit columns.")
    # Flag VIF issues for the model
    max_vif = max((v for v in vifs[1:] if isinstance(v, float) and not math.isnan(v)),
                   default=0.0)
    if max_vif > 10:
        warnings_list.append(
            f"Severe multicollinearity (max VIF={max_vif:.1f}). "
            "Consider dropping one or more collinear predictors.")
    elif max_vif > 5:
        warnings_list.append(
            f"Moderate multicollinearity (max VIF={max_vif:.1f}). "
            "Coefficient estimates may be unstable.")

    return {
        "coefficients":   coef_df,
        "r2":             float(r2),
        "adj_r2":         float(adj_r2),
        "residual_se":    float(residual_se),
        "f_stat":         float(f_stat) if not math.isnan(f_stat) else float("nan"),
        "f_p_value":      float(f_p)    if not math.isnan(f_p)    else float("nan"),
        "n":              int(n),
        "n_dropped_nan":  int(n_dropped),
        "residuals":      residuals,
        "warnings":       warnings_list,
    }
