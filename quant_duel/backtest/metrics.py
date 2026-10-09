"""Prediction metrics (per row) and strategy metrics (per day)."""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

TRADING_DAYS = 252
EPS = 1e-4


def prediction_metrics(p: np.ndarray, y: np.ndarray) -> Dict[str, float]:
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    y = np.asarray(y, dtype=float)
    out = {
        "n": float(len(y)),
        "accuracy": float(((p >= 0.5) == (y == 1)).mean()),
        "log_loss": float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))),
        "brier": float(np.mean((p - y) ** 2)),
        "up_rate": float(y.mean()),
    }
    try:
        from sklearn.metrics import roc_auc_score
        out["auc"] = float(roc_auc_score(y, p)) if len(set(y)) > 1 \
            and len(set(np.round(p, 12))) > 1 else 0.5
    except Exception:                                     # noqa: BLE001
        out["auc"] = float("nan")
    return out


def strategy_metrics(daily: pd.Series, turnover: pd.Series | None = None
                     ) -> Dict[str, float]:
    """``daily``: the strategy's net return per day."""
    r = daily.fillna(0.0).to_numpy(dtype=float)
    equity = np.cumprod(1 + r)
    peak = np.maximum.accumulate(equity)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    out = {
        "days": float(len(r)),
        "cum_return": float(equity[-1] - 1) if len(r) else 0.0,
        "ann_return": float(equity[-1] ** (TRADING_DAYS / len(r)) - 1)
        if len(r) else 0.0,
        "sharpe": float(r.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else 0.0,
        "max_drawdown": float((equity / peak - 1).min()) if len(r) else 0.0,
    }
    if turnover is not None:
        out["turnover"] = float(turnover.mean())
    return out
