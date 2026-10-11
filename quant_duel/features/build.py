"""The feature table: one row per (date, ticker).

TIMING RULE (enforced here and in tests): the features on the row for day t
are computed only from bars dated t or earlier — every operation is a
backward-looking rolling window or a shift into the past. The target on that
row is the direction of the NEXT trading day's return (t → t+1), and
``fwd_return`` is that return; both are labels, never features.

Prices are adjusted closes, so splits and dividends do not look like moves.
"""
from __future__ import annotations

from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

LABELS = ("target", "fwd_return")
ID_COLUMNS = ("date", "ticker")


class LeakageError(RuntimeError):
    """A feature that could not have been known at day t's close."""


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window, min_periods=window).mean()
    loss = (-delta.clip(upper=0)).rolling(window, min_periods=window).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(loss != 0, 100.0)


def ticker_features(df: pd.DataFrame, cfg: Dict) -> pd.DataFrame:
    """Features for one ticker's bars (index = date), labels included."""
    on = cfg["enabled"]
    close = df["adj_close"].astype(float)
    logret = np.log(close).diff()
    out = pd.DataFrame(index=df.index)
    if on.get("returns", True):
        for k in cfg["return_lookbacks"]:
            out[f"ret_{k}"] = close / close.shift(k) - 1
    if on.get("ma_ratio", True):
        for w in cfg["ma_windows"]:
            out[f"ma_ratio_{w}"] = close / close.rolling(w, min_periods=w).mean() - 1
    if on.get("volatility", True):
        for w in cfg["vol_windows"]:
            out[f"vol_{w}"] = logret.rolling(w, min_periods=w).std()
    if on.get("rsi", True):
        out["rsi"] = _rsi(close, cfg["rsi_window"]) / 100.0
    if on.get("volume_z", True):
        w = cfg["volume_z_window"]
        vol = np.log1p(df["volume"].astype(float))
        mean = vol.rolling(w, min_periods=w).mean()
        std = vol.rolling(w, min_periods=w).std()
        out["volume_z"] = (vol - mean) / std.replace(0, np.nan)
    if on.get("day_of_week", True):
        dow = pd.to_datetime(pd.Index(df.index)).dayofweek
        for d in range(5):
            out[f"dow_{d}"] = (np.asarray(dow) == d).astype(float)
    # Labels: tomorrow's return, as seen from today's close.
    fwd = close.shift(-1) / close - 1
    out["fwd_return"] = fwd
    out["target"] = (fwd > 0).astype(float).where(fwd.notna())
    return out


def build(prices: Dict[str, pd.DataFrame], cfg: Dict,
          tickers: Sequence[str], context: str = "SPY") -> pd.DataFrame:
    """The long feature table for ``tickers``. Rows whose features are not
    yet defined (warm-up windows) are dropped; the newest day is kept with an
    empty target, since it is the day being predicted."""
    frames: List[pd.DataFrame] = []
    ctx = None
    if cfg["enabled"].get("spy_context", True) and context in prices \
            and not prices[context].empty:
        c = prices[context]["adj_close"].astype(float)
        lr = np.log(c).diff()
        ctx = pd.DataFrame({"spy_ret_1": c / c.shift(1) - 1,
                            "spy_ret_5": c / c.shift(5) - 1,
                            "spy_ret_20": c / c.shift(20) - 1,
                            "spy_vol_20": lr.rolling(20, min_periods=20).std()},
                           index=prices[context].index)
    for t in tickers:
        df = prices.get(t)
        if df is None or df.empty:
            continue
        f = ticker_features(df, cfg)
        if ctx is not None:
            f = f.join(ctx, how="left")
        f.insert(0, "ticker", t)
        frames.append(f)
    if not frames:
        return pd.DataFrame()
    table = pd.concat(frames)
    table.index.name = "date"
    table = table.reset_index()
    feats = feature_columns(table)
    table = table.dropna(subset=feats).sort_values(["date", "ticker"])
    return table.reset_index(drop=True)


def feature_columns(table: pd.DataFrame) -> List[str]:
    return [c for c in table.columns if c not in LABELS + ID_COLUMNS]


def guard(table: pd.DataFrame, features: Sequence[str],
          max_agreement: float = 0.9) -> None:
    """Refuse a feature that could only be known after day t's close.

    Two checks: no label column among the features, and no feature whose
    sign (above/below its median) agrees with the target on more than
    ``max_agreement`` of rows — no honest daily feature comes close; a
    column computed from tomorrow's price does."""
    bad = [f for f in features if f in LABELS]
    if bad:
        raise LeakageError(f"label column(s) used as features: {bad}")
    labelled = table.dropna(subset=["target"])
    if len(labelled) < 50:
        return
    y = labelled["target"].to_numpy()
    for f in features:
        x = labelled[f].to_numpy(dtype=float)
        if np.nanstd(x) == 0:
            continue
        med = np.nanmedian(x)
        # Both > and >= the median: a 0/1 feature has median 0 or 1, and
        # only one of the two splits separates its values.
        agree = 0.0
        for hi in (x > med, x >= med):
            agree = max(agree, (hi == y).mean(), (hi != y).mean())
        if agree > max_agreement:
            raise LeakageError(
                f"feature {f!r} agrees with tomorrow's direction on "
                f"{agree:.0%} of rows — it must be using future data")
