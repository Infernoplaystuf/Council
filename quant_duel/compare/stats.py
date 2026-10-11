"""Paired statistics for A vs B.

The unit of resampling is the trading day: all tickers on one day share
the market's move, so their prediction errors are correlated and treating
rows as independent would make the intervals far too narrow.

* ``paired_log_loss``: on the (date, ticker) predictions both nodes made,
  ``diff = log_loss_B - log_loss_A`` per row (positive = A better); the
  estimate is the mean over rows, with a day-block bootstrap interval and
  a t-statistic over daily mean differences.
* ``paired_returns``: daily live returns, ``A - B``; mean, t-test across
  days and a bootstrap interval.
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd
from scipy import stats as sps

EPS = 1e-4


def _ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1 - EPS)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def day_bootstrap(sums: np.ndarray, counts: np.ndarray, resamples: int,
                  seed: int, confidence: float) -> tuple:
    """Percentile interval of sum(sums)/sum(counts), resampling days."""
    rng = np.random.default_rng(seed)
    d = len(sums)
    idx = rng.integers(0, d, size=(resamples, d))
    est = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    a = (1 - confidence) / 2
    return float(np.quantile(est, a)), float(np.quantile(est, 1 - a))


def _t(daily: np.ndarray) -> tuple:
    """(t, two-sided p) of the daily mean against zero. A spread that is
    only float noise counts as none: t is ±inf (or 0 for a zero mean)."""
    if len(daily) < 2:
        return 0.0, 1.0
    mean, sd = float(np.mean(daily)), float(np.std(daily, ddof=1))
    if sd <= 1e-12 * max(1.0, abs(mean)):
        return (0.0, 1.0) if mean == 0 else (float(np.sign(mean) * np.inf),
                                             0.0)
    t, p = sps.ttest_1samp(daily, 0.0)
    return float(t), float(p)


def paired_log_loss(a: pd.DataFrame, b: pd.DataFrame, resamples: int = 10000,
                    seed: int = 0, confidence: float = 0.95
                    ) -> Dict[str, float]:
    """``a``/``b``: date, ticker, p, target (scored rows only)."""
    j = a[["date", "ticker", "p", "target"]].merge(
        b[["date", "ticker", "p", "target"]], on=["date", "ticker"],
        suffixes=("_a", "_b"))
    if j.empty:
        raise ValueError("the nodes share no scored predictions")
    if not np.array_equal(j["target_a"].to_numpy(), j["target_b"].to_numpy()):
        raise ValueError("the nodes disagree on outcomes — they did not see "
                         "the same prices")
    y = j["target_a"].to_numpy(dtype=float)
    ll_a = _ll(j["p_a"].to_numpy(dtype=float), y)
    ll_b = _ll(j["p_b"].to_numpy(dtype=float), y)
    j["diff"] = ll_b - ll_a
    g = j.groupby("date")["diff"].agg(["sum", "count"])
    sums, counts = g["sum"].to_numpy(), g["count"].to_numpy(dtype=float)
    lo, hi = day_bootstrap(sums, counts, resamples, seed, confidence)
    t, p = _t(sums / counts)
    return {"n_rows": float(len(j)), "n_days": float(len(g)),
            "ll_a": float(ll_a.mean()), "ll_b": float(ll_b.mean()),
            "diff": float(j["diff"].mean()), "ci_low": lo, "ci_high": hi,
            "t": t, "p_value": p,
            "identical": bool(np.allclose(j["p_a"], j["p_b"],
                                          rtol=0, atol=1e-12)),
            "unmatched_a": float(len(a) - len(j)),
            "unmatched_b": float(len(b) - len(j))}


def paired_returns(ra: pd.Series, rb: pd.Series, resamples: int = 10000,
                   seed: int = 0, confidence: float = 0.95
                   ) -> Dict[str, float]:
    """Daily returns indexed by date; ``diff = A - B``."""
    j = pd.concat([ra.rename("a"), rb.rename("b")], axis=1, join="inner")
    d = (j["a"] - j["b"]).to_numpy(dtype=float)
    if len(d) == 0:
        raise ValueError("no common days")
    lo, hi = day_bootstrap(d, np.ones(len(d)), resamples, seed + 1,
                           confidence)
    t, p = _t(d)
    return {"n_days": float(len(d)), "mean_diff": float(d.mean()),
            "ci_low": lo, "ci_high": hi, "t": t, "p_value": p}


def verdict(res: Dict[str, float], what: str, better: str,
            worse: str) -> str:
    """One plain sentence: is the interval clear of zero?"""
    lo, hi = res["ci_low"], res["ci_high"]
    if res.get("identical"):
        return (f"The two nodes made identical predictions, so there is no "
                f"difference in {what} to test.")
    if lo > 0:
        return (f"{better} The 95% interval for the difference in {what} "
                f"[{lo:+.5f}, {hi:+.5f}] lies above zero, so this is "
                "distinguishable from noise.")
    if hi < 0:
        return (f"{worse} The 95% interval for the difference in {what} "
                f"[{lo:+.5f}, {hi:+.5f}] lies below zero, so this is "
                "distinguishable from noise.")
    return (f"The 95% interval for the difference in {what} "
            f"[{lo:+.5f}, {hi:+.5f}] includes zero: the difference is NOT "
            "distinguishable from noise.")
