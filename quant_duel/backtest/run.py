"""Run a model through the walk-forward backtest and report it next to
buy-and-hold.

``predict`` gives the out-of-sample prediction for every test row of every
fold (each fold's model fit on its training rows only). ``evaluate`` turns
predictions into prediction metrics and strategy metrics. ``compare_models``
does that for several models over the SAME folds and days, with
buy-and-hold (equal weight) and SPY alongside.
"""
from __future__ import annotations

from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

from ..features.build import feature_columns, guard
from ..models.base import make_model
from . import metrics as mx
from . import strategy as st
from .walkforward import folds


def predict(table: pd.DataFrame, kind: str, cfg: Dict, seed: int = 0,
            features: Sequence[str] | None = None) -> pd.DataFrame:
    """Out-of-sample predictions: date, ticker, p, target, fwd_return, fold."""
    bt = cfg["backtest"]
    labelled = table.dropna(subset=["target"])
    features = list(features or feature_columns(table))
    guard(labelled, features)
    out: List[pd.DataFrame] = []
    by_date = {d: g for d, g in labelled.groupby("date")}
    for f in folds(list(by_date), train_days=bt["train_days"],
                   test_days=bt["test_days"],
                   min_train_days=bt["min_train_days"]):
        train = pd.concat([by_date[d] for d in f.train])
        test = pd.concat([by_date[d] for d in f.test])
        model = make_model(kind, cfg["model"]["params"], seed=seed)
        model.fit(train[features], train["target"].to_numpy())
        res = test[["date", "ticker", "target", "fwd_return"]].copy()
        res["p"] = model.predict_proba(test[features])
        res["fold"] = f.index
        out.append(res)
    if not out:
        raise ValueError("not enough history for one walk-forward fold")
    pred = pd.concat(out, ignore_index=True)
    pred.attrs["always_long"] = kind == "always_up"
    return pred


def evaluate(pred: pd.DataFrame, cfg: Dict) -> Dict[str, float]:
    bt = cfg["backtest"]
    pos = st.positions(pred, bt["threshold"], pred.attrs.get("always_long",
                                                             False))
    net, turnover = st.daily_returns(pred, pos, bt["cost_bps"])
    out = mx.prediction_metrics(pred["p"].to_numpy(),
                                pred["target"].to_numpy())
    out.update(mx.strategy_metrics(net, turnover))
    return out


def compare_models(table: pd.DataFrame, kinds: Sequence[str], cfg: Dict,
                   seed: int = 0) -> pd.DataFrame:
    """One row per model plus buy-and-hold and SPY, same days for all."""
    rows = {}
    preds = {}
    for k in kinds:
        preds[k] = predict(table, k, cfg, seed)
        rows[k] = evaluate(preds[k], cfg)
    ref = next(iter(preds.values()))
    bh = st.buy_and_hold(ref, cfg["backtest"]["cost_bps"])
    rows["buy_and_hold"] = mx.strategy_metrics(bh)
    bench = cfg["universe"].get("benchmark", "SPY")
    spy = st.single_ticker_hold(ref, bench, cfg["backtest"]["cost_bps"])
    if len(spy):
        rows[f"hold_{bench}"] = mx.strategy_metrics(spy)
    df = pd.DataFrame(rows).T
    cols = ["n", "accuracy", "log_loss", "brier", "auc", "up_rate",
            "cum_return", "ann_return", "sharpe", "max_drawdown", "turnover",
            "days"]
    return df.reindex(columns=[c for c in cols if c in df.columns])


def suspicious(results: pd.DataFrame, max_accuracy: float = 0.55,
               sharpe_margin: float = 1.0) -> List[str]:
    """Results too good to believe without a leakage hunt."""
    out = []
    bh = results.loc["buy_and_hold", "sharpe"] if "buy_and_hold" in \
        results.index else 0.0
    for name, row in results.iterrows():
        if name.startswith(("buy_and_hold", "hold_")):
            continue
        if not np.isnan(row.get("accuracy", np.nan)) and \
                row["accuracy"] > max_accuracy:
            out.append(f"{name}: accuracy {row['accuracy']:.1%} > "
                       f"{max_accuracy:.0%} — check for leakage")
        if row.get("sharpe", 0) > bh + sharpe_margin:
            out.append(f"{name}: Sharpe {row['sharpe']:.2f} far above "
                       f"buy-and-hold {bh:.2f} — check for leakage")
    return out
