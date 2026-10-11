"""From scored headlines to one sentiment value per (trading day, ticker),
and the bounded overlay that applies it.

Day t's value for a ticker is the mean score of that ticker's scored
headlines counting for t (published after t-1's cutoff and strictly before
t's — see ``timing``), at most ``news.max_per_ticker_day`` of them, the
most recent first. No headlines → no row (the overlay then adds nothing).

The overlay: ``p_adj = clip(p + w * sentiment, EPS, 1 - EPS)``.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

import numpy as np
import pandas as pd

from ..models.base import EPS
from .timing import sentiment_day


def assign_days(pairs: pd.DataFrame, cutoff: str, cap: int) -> pd.DataFrame:
    """Add ``day`` (the trading day each pair counts for) and ``rank`` (its
    recency rank within that ticker-day, 0 = newest). Pairs without a
    usable publish time are dropped."""
    df = pairs.dropna(subset=["published"]).copy()
    if df.empty:
        return df.assign(day=pd.Series(dtype=object),
                         rank=pd.Series(dtype=int))
    df["day"] = [sentiment_day(t.to_pydatetime(), cutoff)
                 for t in df["published"]]
    df = df.sort_values(["ticker", "day", "published", "headline_id"],
                        ascending=[True, True, False, True])
    df["rank"] = df.groupby(["ticker", "day"]).cumcount()
    return df


def daily_sentiment(pairs: pd.DataFrame, cutoff: str = "16:00",
                    cap: int = 10, until: Optional[dt.date] = None
                    ) -> pd.DataFrame:
    """date, ticker, sentiment, n — from scored pairs only."""
    scored = pairs[pairs["status"] == "scored"]
    df = assign_days(scored, cutoff, cap)
    df = df[df["rank"] < cap]
    if until is not None:
        df = df[df["day"] <= until]
    if df.empty:
        return pd.DataFrame(columns=["date", "ticker", "sentiment", "n"])
    out = df.groupby(["day", "ticker"])["score"].agg(["mean", "count"])
    out = out.reset_index().rename(columns={"day": "date", "mean":
                                            "sentiment", "count": "n"})
    return out


def overlay(p: np.ndarray, sentiment: np.ndarray, w: float) -> np.ndarray:
    s = np.nan_to_num(np.asarray(sentiment, dtype=float), nan=0.0)
    return np.clip(np.asarray(p, dtype=float) + w * s, EPS, 1 - EPS)


def lookup(sent: Optional[pd.DataFrame], dates, tickers) -> np.ndarray:
    """Sentiment for each (date, ticker), NaN where there is none."""
    if sent is None or sent.empty:
        return np.full(len(dates), np.nan)
    m = sent.set_index(["date", "ticker"])["sentiment"]
    idx = pd.MultiIndex.from_arrays([list(dates), list(tickers)])
    return m.reindex(idx).to_numpy(dtype=float)
