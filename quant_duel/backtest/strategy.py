"""Long/flat per ticker on a P(up) threshold, equal weight, with costs.

On day t the signal is known at t's close; the position is held from t's
close to t+1's close and earns ``fwd_return``. Every change of position —
flat to long or long to flat — costs ``cost_bps`` on the traded weight. The
portfolio is equally weighted over the tickers that have a prediction that
day; buy-and-hold is the same portfolio always long (and it is charged one
entry cost).
"""
from __future__ import annotations

from typing import Tuple

import pandas as pd


def positions(pred: pd.DataFrame, threshold: float,
              always_long: bool = False) -> pd.Series:
    if always_long:
        return pd.Series(1.0, index=pred.index)
    return (pred["p"] >= threshold).astype(float)


def daily_returns(pred: pd.DataFrame, pos: pd.Series, cost_bps: float
                  ) -> Tuple[pd.Series, pd.Series]:
    """(net return per day, turnover per day) of the equal-weight book."""
    df = pred[["date", "ticker", "fwd_return"]].copy()
    df["pos"] = pos.to_numpy()
    df = df.sort_values(["ticker", "date"])
    df["prev"] = df.groupby("ticker")["pos"].shift(1).fillna(0.0)
    n = df.groupby("date")["ticker"].transform("count")
    df["w"] = df["pos"] / n
    df["trade"] = (df["pos"] - df["prev"]).abs() / n
    gross = (df["w"] * df["fwd_return"]).groupby(df["date"]).sum()
    turnover = df.groupby("date")["trade"].sum()
    net = gross - turnover * cost_bps / 1e4
    return net.sort_index(), turnover.sort_index()


def buy_and_hold(pred: pd.DataFrame, cost_bps: float) -> pd.Series:
    """Equal weight in every ticker, always long; one entry cost."""
    gross = pred.groupby("date")["fwd_return"].mean().sort_index()
    out = gross.copy()
    if len(out):
        out.iloc[0] -= cost_bps / 1e4
    return out


def single_ticker_hold(pred: pd.DataFrame, ticker: str,
                       cost_bps: float) -> pd.Series:
    """Buy and hold one ticker (SPY) over the same days."""
    r = pred[pred["ticker"] == ticker].set_index("date")["fwd_return"]
    r = r.sort_index().copy()
    if len(r):
        r.iloc[0] -= cost_bps / 1e4
    return r

