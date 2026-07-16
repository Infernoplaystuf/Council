"""
analyst_helpers/stats.py — Research / statistics helpers.

Five sandbox-safe functions. Pure numpy + scipy.stats — no
statsmodels, no sympy, nothing heavyweight.

  descriptive_stats_rigorous(series, column=None)
      mean / median / std / SE / 95% CI on the mean (t-distribution) /
      skewness / kurtosis / IQR / n / n_missing. The bar that "give me
      summary stats" should clear when the user is publishing or
      shipping a number.

  compare_groups(df, value_col, group_col, paired=False)
      AUTO-SELECTS the right test by checking normality (Shapiro) and
      equal variance (Levene), then picks t / Welch's t / Mann-Whitney
      U / Wilcoxon signed-rank. Always returns an effect size alongside
      the p-value. The rationale string explains why that test was
      chosen — this is the value-add over a generic t-test helper.

  multiple_comparison_correction(p_values, method='fdr_bh')
      Bonferroni / Holm-Bonferroni / Benjamini-Hochberg FDR.
      Implemented directly, no statsmodels.

  correlation_with_significance(df, cols=None, method='pearson')
      Correlation matrix + matched p-value matrix with FDR-corrected
      flags. Pearson / Spearman / Kendall.

  bootstrap_ci(series, statistic_fn=np.mean, n_boot=10000, ci=0.95,
                random_state=None, column=None)
      Percentile-bootstrap confidence interval for an arbitrary
      statistic. Reproducible via random_state.

Conventions match the other analyst_helpers modules:
  • Drop NaN with count surfaced
  • Return dicts / DataFrames / tuples — never print, never write
  • column= kwarg for multi-column DataFrame inputs (consistent with
    spc / engineering helpers)
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd


ArrayLike = Union[pd.Series, pd.DataFrame, "np.ndarray", List, Tuple, str]


# ============================================================
# 1. descriptive_stats_rigorous
# ============================================================

def descriptive_stats_rigorous(
    series: ArrayLike,
    *,
    column: Optional[str] = None,
) -> Dict[str, Any]:
    """Sample stats with the bits people actually use when publishing.

    Parameters
    ----------
    series : array-like, file path, Series, or DataFrame
        Numeric values. ``column`` selects when ``series`` is a
        multi-column DataFrame / CSV.

    Returns
    -------
    dict
        n              : int   — finite observations after NaN drop
        n_missing      : int
        mean           : float
        median         : float
        std            : float — sample stdev (ddof=1)
        se_mean        : float — std / sqrt(n)
        ci95_lower     : float — t-distribution CI on the mean
        ci95_upper     : float
        min            : float
        q25            : float
        q75            : float
        max            : float
        iqr            : float — q75 - q25
        skewness       : float — bias-corrected (Fisher-Pearson)
        kurtosis       : float — excess kurtosis (normal = 0)

    Raises
    ------
    ValueError if fewer than 2 finite values remain.
    """
    from .spc import _coerce_series   # reuse the DataFrame logic
    raw = _coerce_series(series, column=column)
    n_total = len(raw)
    arr = raw[~np.isnan(raw)]
    n = len(arr)
    n_missing = int(n_total - n)
    if n < 2:
        raise ValueError(f"Need ≥ 2 finite values; got {n}.")

    from scipy import stats as _st

    mean = float(np.mean(arr))
    std  = float(np.std(arr, ddof=1))
    se   = std / math.sqrt(n) if n > 0 else float("nan")
    # 95% CI on the mean via t-distribution with df = n - 1
    t_crit = float(_st.t.ppf(0.975, df=n - 1)) if n > 1 else float("nan")
    ci_half = t_crit * se if not math.isnan(t_crit) else float("nan")
    q25 = float(np.percentile(arr, 25))
    q75 = float(np.percentile(arr, 75))
    # bias-corrected skewness (Fisher-Pearson) and excess kurtosis
    skew = float(_st.skew(arr, bias=False))
    kurt = float(_st.kurtosis(arr, fisher=True, bias=False))

    return {
        "n":            n,
        "n_missing":    n_missing,
        "mean":         mean,
        "median":       float(np.median(arr)),
        "std":          std,
        "se_mean":      float(se),
        "ci95_lower":   float(mean - ci_half) if not math.isnan(ci_half) else float("nan"),
        "ci95_upper":   float(mean + ci_half) if not math.isnan(ci_half) else float("nan"),
        "min":          float(np.min(arr)),
        "q25":          q25,
        "q75":          q75,
        "max":          float(np.max(arr)),
        "iqr":          q75 - q25,
        "skewness":     skew,
        "kurtosis":     kurt,
    }


# ============================================================
# 2. compare_groups — auto-pick the right test
# ============================================================

def compare_groups(
    df: pd.DataFrame,
    value_col: str,
    group_col: str,
    *,
    paired: bool = False,
    alpha: float = 0.05,
) -> Dict[str, Any]:
    """Two-group comparison with automatic test selection. The value-
    add over a generic t-test helper is the rationale string — the
    model gets to tell the user *why* a particular test was chosen,
    instead of running a vanilla t-test on data that violates its
    assumptions.

    Decision tree
    -------------
        1. Shapiro-Wilk on each group → normal? (alpha = 0.05)
        2. If both normal:
             a. Levene's test for equal variance → equal?
             b. equal     → independent two-sample t-test
                unequal   → Welch's t-test
        3. If either non-normal:
             paired=False → Mann-Whitney U
             paired=True  → Wilcoxon signed-rank

    Parameters
    ----------
    df : pd.DataFrame
        Long-form data: one row per observation.
    value_col : str
        Numeric measurement.
    group_col : str
        Group / label column. Must have exactly two unique values
        after NaN drop.
    paired : bool
        Treat values as paired (matched / repeated measures). For
        paired data the two groups must have the same n and the
        order is significant — the helper takes the values in
        their natural row order within each group.
    alpha : float
        Significance level for normality / variance pre-tests
        (default 0.05). Does NOT affect the reported p-value of
        the final comparison.

    Returns
    -------
    dict
        test_used        : str — e.g. "Welch's t-test"
        rationale        : str — why this test was chosen
        statistic        : float
        p_value          : float
        effect_size      : float — Cohen's d for parametric tests,
                                    rank-biserial r for Mann-Whitney
        effect_size_name : str — what the number means
        n_per_group      : dict[group → int]
        warnings         : list[str]

    Raises
    ------
    ValueError when the group_col doesn't have exactly 2 unique
    values, when paired groups have different lengths, or when
    either group is < 3 after NaN drop.
    """
    for c in (value_col, group_col):
        if c not in df.columns:
            raise ValueError(f"Column {c!r} not in DataFrame.")

    sub = df[[value_col, group_col]].dropna()
    if len(sub) < 4:
        raise ValueError(f"Need ≥ 4 paired observations; got {len(sub)}.")

    groups = sub[group_col].unique().tolist()
    if len(groups) != 2:
        raise ValueError(
            f"compare_groups expects EXACTLY 2 groups; found "
            f"{len(groups)}: {groups[:5]}. For >2 groups use ANOVA "
            "(not yet implemented in the helpers — use scipy.stats.f_oneway "
            "directly in the model code).")

    g1, g2 = groups
    a = pd.to_numeric(sub.loc[sub[group_col] == g1, value_col],
                       errors="coerce").dropna().to_numpy()
    b = pd.to_numeric(sub.loc[sub[group_col] == g2, value_col],
                       errors="coerce").dropna().to_numpy()
    if len(a) < 3 or len(b) < 3:
        raise ValueError(
            f"Each group needs ≥ 3 values; got {g1}: n={len(a)}, "
            f"{g2}: n={len(b)}.")

    warnings_list: List[str] = []
    if paired:
        if len(a) != len(b):
            raise ValueError(
                f"Paired analysis requires equal-length groups; "
                f"got {g1}: n={len(a)}, {g2}: n={len(b)}.")

    from scipy import stats as _st

    # Normality pre-tests. Shapiro-Wilk loses power above ~5000;
    # we don't reach that on typical analyst data so don't worry
    # about the AD fallback here.
    p_norm_a = float(_st.shapiro(a).pvalue) if len(a) >= 3 else 1.0
    p_norm_b = float(_st.shapiro(b).pvalue) if len(b) >= 3 else 1.0
    both_normal = (p_norm_a >= alpha) and (p_norm_b >= alpha)

    if paired:
        if both_normal:
            t_stat, p_val = _st.ttest_rel(a, b)
            test_used = "Paired t-test"
            rationale = (
                f"Both groups passed Shapiro-Wilk normality "
                f"(p={p_norm_a:.3f}, p={p_norm_b:.3f}); "
                f"paired=True → paired t-test.")
            # Cohen's d for paired: mean(diff) / std(diff, ddof=1)
            diff = a - b
            mean_diff = float(np.mean(diff))
            sd_diff   = float(np.std(diff, ddof=1))
            d = (mean_diff / sd_diff) if sd_diff > 0 else float("nan")
            return {
                "test_used":        test_used,
                "rationale":        rationale,
                "statistic":        float(t_stat),
                "p_value":          float(p_val),
                "effect_size":      d,
                "effect_size_name": "Cohen's d (paired)",
                "n_per_group":      {str(g1): int(len(a)), str(g2): int(len(b))},
                "warnings":         warnings_list,
            }
        # Non-normal paired → Wilcoxon signed-rank
        stat, p_val = _st.wilcoxon(a, b)
        test_used = "Wilcoxon signed-rank test"
        rationale = (
            f"At least one group failed normality "
            f"(p={p_norm_a:.3f}, p={p_norm_b:.3f}); paired=True → "
            "Wilcoxon signed-rank (non-parametric paired).")
        # Rank-biserial r for Wilcoxon — using the standardised
        # statistic approximation. scipy doesn't return it directly;
        # we compute from the signed ranks.
        diff = a - b
        diff = diff[diff != 0]   # discard ties
        if len(diff) > 0:
            ranks = _st.rankdata(np.abs(diff))
            pos_sum = float(ranks[diff > 0].sum())
            neg_sum = float(ranks[diff < 0].sum())
            total = pos_sum + neg_sum
            r_rb = (pos_sum - neg_sum) / total if total > 0 else float("nan")
        else:
            r_rb = float("nan")
        return {
            "test_used":        test_used,
            "rationale":        rationale,
            "statistic":        float(stat),
            "p_value":          float(p_val),
            "effect_size":      r_rb,
            "effect_size_name": "Rank-biserial r",
            "n_per_group":      {str(g1): int(len(a)), str(g2): int(len(b))},
            "warnings":         warnings_list,
        }

    # Unpaired
    if both_normal:
        # Equal-variance check (Levene's is robust to non-normality)
        p_lev = float(_st.levene(a, b).pvalue)
        equal_var = p_lev >= alpha
        t_stat, p_val = _st.ttest_ind(a, b, equal_var=equal_var)
        test_used = ("Independent t-test (equal variance)"
                      if equal_var else "Welch's t-test (unequal variance)")
        rationale = (
            f"Both groups passed Shapiro normality "
            f"(p={p_norm_a:.3f}, p={p_norm_b:.3f}); Levene's variance "
            f"test p={p_lev:.3f} → "
            + ("equal variance assumed."
                if equal_var else "variances differ → Welch's."))
        # Cohen's d. Pooled std for equal-variance, Welch's d
        # (average σ) otherwise.
        n_a, n_b = len(a), len(b)
        s_a, s_b = float(np.std(a, ddof=1)), float(np.std(b, ddof=1))
        if equal_var:
            s_pool = math.sqrt(
                ((n_a - 1) * s_a ** 2 + (n_b - 1) * s_b ** 2)
                / (n_a + n_b - 2)
            ) if (n_a + n_b - 2) > 0 else float("nan")
        else:
            # Average of the two variances
            s_pool = math.sqrt((s_a ** 2 + s_b ** 2) / 2.0)
        mean_diff = float(np.mean(a) - np.mean(b))
        d = (mean_diff / s_pool) if s_pool and s_pool > 0 else float("nan")
        return {
            "test_used":        test_used,
            "rationale":        rationale,
            "statistic":        float(t_stat),
            "p_value":          float(p_val),
            "effect_size":      d,
            "effect_size_name": "Cohen's d",
            "n_per_group":      {str(g1): int(len(a)), str(g2): int(len(b))},
            "warnings":         warnings_list,
        }

    # Non-normal unpaired → Mann-Whitney U
    u_stat, p_val = _st.mannwhitneyu(a, b, alternative="two-sided")
    test_used = "Mann-Whitney U test"
    rationale = (
        f"At least one group failed normality "
        f"(p={p_norm_a:.3f}, p={p_norm_b:.3f}); paired=False → "
        "Mann-Whitney U (non-parametric unpaired).")
    # Rank-biserial r from U, signed so POSITIVE means group 1 is larger —
    # the same convention as the Wilcoxon branch above ((pos-neg)/total) and as
    # Cohen's d in the parametric branch.
    #
    # This was `1 - 2U/(n1*n2)`, which is the sign the OTHER way round: scipy's
    # mannwhitneyu(a, b) returns U for the FIRST sample, so a group-1-dominant
    # comparison gives U ~= n1*n2 and the old form returned r = -1 for data
    # where group 1 is clearly larger. Verified: a=[10..17] vs b=[1..8]
    # reported -1.0 while the Wilcoxon branch reported +1.0 on the same
    # numbers, so the paired and unpaired paths of one function disagreed about
    # which group was bigger.
    n_a, n_b = len(a), len(b)
    r_rb = (2.0 * float(u_stat)) / (n_a * n_b) - 1.0
    return {
        "test_used":        test_used,
        "rationale":        rationale,
        "statistic":        float(u_stat),
        "p_value":          float(p_val),
        "effect_size":      float(r_rb),
        "effect_size_name": "Rank-biserial r",
        "n_per_group":      {str(g1): int(len(a)), str(g2): int(len(b))},
        "warnings":         warnings_list,
    }


# ============================================================
# 3. multiple_comparison_correction
# ============================================================

def multiple_comparison_correction(
    p_values: Sequence[float],
    method: str = "fdr_bh",
    *,
    alpha: float = 0.05,
) -> Dict[str, Any]:
    """Adjust a family of p-values for multiple comparisons.

    Three methods, all implemented directly (no statsmodels):

      bonferroni  — multiply each p by m. Conservative.
      holm        — step-down Bonferroni. Less conservative than
                    plain Bonferroni; still strong FWER control.
      fdr_bh      — Benjamini-Hochberg False Discovery Rate.
                    Recommended when you have >5 comparisons and
                    you're hunting (not confirming) signals.

    Returns
    -------
    dict
        method            : str
        n_tests           : int
        p_raw             : np.ndarray
        p_adjusted        : np.ndarray
        reject_null       : np.ndarray (bool) — p_adjusted < alpha
        alpha             : float
    """
    arr = np.asarray(list(p_values), dtype=float)
    m = len(arr)
    if m == 0:
        raise ValueError("p_values is empty.")
    if np.any((arr < 0) | (arr > 1)):
        raise ValueError(
            "All p-values must be in [0, 1]. "
            f"Got min={arr.min()}, max={arr.max()}.")

    method_norm = method.lower().strip()

    if method_norm == "bonferroni":
        p_adj = np.minimum(1.0, arr * m)
    elif method_norm == "holm":
        # Holm-Bonferroni: sort ascending, multiply ith smallest by (m - i)
        order = np.argsort(arr)
        sorted_p = arr[order]
        adj_sorted = np.minimum(1.0, sorted_p * (m - np.arange(m)))
        # Enforce monotone non-decreasing
        adj_sorted = np.maximum.accumulate(adj_sorted)
        p_adj = np.empty_like(arr)
        p_adj[order] = adj_sorted
    elif method_norm in ("fdr_bh", "bh"):
        # Benjamini-Hochberg
        order = np.argsort(arr)
        sorted_p = arr[order]
        ranks = np.arange(1, m + 1, dtype=float)
        adj_sorted = sorted_p * m / ranks
        # Enforce monotone non-increasing FROM THE TOP
        adj_sorted = np.minimum.accumulate(adj_sorted[::-1])[::-1]
        adj_sorted = np.minimum(1.0, adj_sorted)
        p_adj = np.empty_like(arr)
        p_adj[order] = adj_sorted
    else:
        raise ValueError(
            f"Unknown method={method!r}. Expected 'bonferroni', "
            "'holm', or 'fdr_bh'.")

    return {
        "method":      method_norm,
        "n_tests":     m,
        "p_raw":       arr,
        "p_adjusted":  p_adj,
        "reject_null": p_adj < alpha,
        "alpha":       float(alpha),
    }


# ============================================================
# 4. correlation_with_significance
# ============================================================

def correlation_with_significance(
    df: pd.DataFrame,
    cols: Optional[Sequence[str]] = None,
    method: str = "pearson",
    *,
    fdr_alpha: float = 0.05,
) -> Dict[str, Any]:
    """All-pairs correlation matrix + matched p-value matrix.

    Parameters
    ----------
    df : pd.DataFrame
    cols : sequence of str, optional
        Subset of columns to correlate. If None, uses every
        numeric column.
    method : {'pearson', 'spearman', 'kendall'}
    fdr_alpha : float
        Significance threshold for the FDR-corrected significance
        mask (Benjamini-Hochberg on the upper-triangle p-values).

    Returns
    -------
    dict
        corr                : pd.DataFrame — correlation matrix
        p_values            : pd.DataFrame — pairwise p-values
                              (diagonal is NaN)
        significant_fdr     : pd.DataFrame — bool mask after FDR
                              correction (diagonal False)
        method              : str
        n                   : pd.DataFrame — pairwise sample sizes
                              after listwise NaN drop
    """
    if cols is None:
        cols = list(df.select_dtypes(include="number").columns)
    if len(cols) < 2:
        raise ValueError(
            f"Need ≥ 2 numeric columns; got {len(cols)}.")
    method_norm = method.lower().strip()
    if method_norm not in ("pearson", "spearman", "kendall"):
        raise ValueError(
            f"Unknown method={method!r}. Expected "
            "'pearson' / 'spearman' / 'kendall'.")
    sub = df[list(cols)].apply(pd.to_numeric, errors="coerce")

    from scipy import stats as _st

    n = len(cols)
    corr_mat = pd.DataFrame(np.eye(n), index=cols, columns=cols, dtype=float)
    p_mat    = pd.DataFrame(np.full((n, n), np.nan),
                             index=cols, columns=cols, dtype=float)
    n_mat    = pd.DataFrame(np.zeros((n, n), dtype=int),
                             index=cols, columns=cols)

    # Upper-triangle (i < j) — store and mirror to lower
    raw_p: List[float] = []
    pair_idx: List[Tuple[int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            xi = sub.iloc[:, i]
            xj = sub.iloc[:, j]
            mask = xi.notna() & xj.notna()
            x = xi[mask].to_numpy(dtype=float)
            y = xj[mask].to_numpy(dtype=float)
            n_pair = len(x)
            n_mat.iloc[i, j] = n_pair
            n_mat.iloc[j, i] = n_pair
            if n_pair < 3 or np.std(x) == 0 or np.std(y) == 0:
                # Indeterminate correlation — leave as NaN.
                corr_mat.iloc[i, j] = np.nan
                corr_mat.iloc[j, i] = np.nan
                p_mat.iloc[i, j] = np.nan
                p_mat.iloc[j, i] = np.nan
                raw_p.append(np.nan)
                pair_idx.append((i, j))
                continue
            if method_norm == "pearson":
                r, p = _st.pearsonr(x, y)
            elif method_norm == "spearman":
                r, p = _st.spearmanr(x, y)
            else:
                r, p = _st.kendalltau(x, y)
            corr_mat.iloc[i, j] = float(r)
            corr_mat.iloc[j, i] = float(r)
            p_mat.iloc[i, j]    = float(p)
            p_mat.iloc[j, i]    = float(p)
            raw_p.append(float(p))
            pair_idx.append((i, j))

    # FDR correction across the upper-triangle p-values
    finite_idx = [k for k, p in enumerate(raw_p) if not math.isnan(p)]
    if finite_idx:
        finite_p = np.array([raw_p[k] for k in finite_idx], dtype=float)
        adj = multiple_comparison_correction(
            finite_p, method="fdr_bh", alpha=fdr_alpha,
        )
        reject = adj["reject_null"]
    else:
        reject = np.array([], dtype=bool)

    sig_mat = pd.DataFrame(np.zeros((n, n), dtype=bool),
                            index=cols, columns=cols)
    for k_local, k_global in enumerate(finite_idx):
        i, j = pair_idx[k_global]
        sig_mat.iloc[i, j] = bool(reject[k_local])
        sig_mat.iloc[j, i] = bool(reject[k_local])

    return {
        "corr":            corr_mat,
        "p_values":        p_mat,
        "significant_fdr": sig_mat,
        "method":          method_norm,
        "n":               n_mat,
        "fdr_alpha":       float(fdr_alpha),
    }


# ============================================================
# 5. bootstrap_ci
# ============================================================

def bootstrap_ci(
    series: ArrayLike,
    statistic_fn: Optional[Callable[[np.ndarray], float]] = None,
    n_boot: int = 10000,
    ci: float = 0.95,
    *,
    random_state: Optional[int] = None,
    column: Optional[str] = None,
) -> Tuple[float, float, float]:
    """Percentile-bootstrap confidence interval for an arbitrary
    sample statistic.

    Parameters
    ----------
    series : array-like, file path, Series, or DataFrame
    statistic_fn : callable, optional
        Function reducing a 1-D array to a scalar. Defaults to
        ``np.mean``. Pass ``np.median`` for the median CI,
        ``lambda a: np.percentile(a, 90)`` for a percentile, etc.
    n_boot : int
        Number of bootstrap resamples. 10 000 is a good default
        for 95% CIs; the rule of thumb is 1000 / (1 - ci).
    ci : float
        Confidence level in (0, 1). Default 0.95.
    random_state : int, optional
        Seed for numpy's default_rng. Pass an explicit int for
        reproducible results — important for any number that will
        end up in a report.
    column : str, optional
        Column to use when ``series`` is a multi-column DataFrame.

    Returns
    -------
    (point_estimate, lower, upper)
        Point estimate of the statistic on the full sample, plus
        the lower and upper percentile bounds of the bootstrap
        distribution.
    """
    from .spc import _coerce_series   # reuse the DataFrame logic
    raw = _coerce_series(series, column=column)
    arr = raw[~np.isnan(raw)]
    n = len(arr)
    if n < 2:
        raise ValueError(f"Need ≥ 2 finite values; got {n}.")
    if not (0.0 < ci < 1.0):
        raise ValueError(f"ci must be in (0, 1); got {ci}.")
    if n_boot < 100:
        raise ValueError(
            f"n_boot < 100 produces unstable CIs; got {n_boot}.")

    if statistic_fn is None:
        statistic_fn = np.mean

    rng = np.random.default_rng(random_state)
    # Resample with replacement, n_boot times, in a single vectorised
    # draw → O(n_boot × n) memory but ~30× faster than a Python loop
    # for typical (10 000, 1000) sizes.
    indices = rng.integers(0, n, size=(n_boot, n))
    samples = arr[indices]
    boot_stats = np.array([statistic_fn(s) for s in samples], dtype=float)

    point_est = float(statistic_fn(arr))
    alpha = (1.0 - ci) / 2.0
    lower = float(np.percentile(boot_stats, 100 * alpha))
    upper = float(np.percentile(boot_stats, 100 * (1.0 - alpha)))
    return point_est, lower, upper
