"""Checks on downloaded prices, before anything is built on them."""
from __future__ import annotations

from typing import List

import pandas as pd

from ..market_calendar import trading_days


def problems(ticker: str, df: pd.DataFrame) -> List[str]:
    """Everything wrong with ``df`` (empty list = clean)."""
    out: List[str] = []
    if df.empty:
        return [f"{ticker}: no rows"]
    if df.index.has_duplicates:
        dups = df.index[df.index.duplicated()].tolist()
        out.append(f"{ticker}: duplicate dates {dups[:5]}")
    if not df.index.is_monotonic_increasing:
        out.append(f"{ticker}: dates out of order")
    expected = set(trading_days(min(df.index), max(df.index)))
    missing = sorted(expected - set(df.index))
    if missing:
        out.append(f"{ticker}: {len(missing)} missing trading day(s), e.g. "
                   f"{[d.isoformat() for d in missing[:5]]}")
    extra = sorted(set(df.index) - expected)
    if extra:
        out.append(f"{ticker}: {len(extra)} row(s) on non-trading days, e.g. "
                   f"{[d.isoformat() for d in extra[:5]]}")
    prices = df[["open", "high", "low", "close", "adj_close"]]
    if prices.isna().any().any():
        out.append(f"{ticker}: {int(prices.isna().sum().sum())} missing price(s)")
    if (prices <= 0).any().any():
        out.append(f"{ticker}: non-positive prices")
    if (df["high"] < df["low"]).any():
        out.append(f"{ticker}: high below low on "
                   f"{int((df['high'] < df['low']).sum())} day(s)")
    ratio = (df["adj_close"] / df["close"]).dropna()
    if len(ratio) and ((ratio > 1.0001).any() or (ratio <= 0).any()):
        out.append(f"{ticker}: adjusted close above close (bad adjustment)")
    return out


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicate dates (keep the last) and sort. Gaps are reported, not
    filled — a filled price is an invented one."""
    df = df[~df.index.duplicated(keep="last")]
    return df.sort_index()
