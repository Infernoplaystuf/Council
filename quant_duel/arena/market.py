"""What the models see: the morning packet and the data they may request.

Everything is computed from bars up to and including ``day`` (the last
close), so no model can see a price from the day it trades.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..news.store import NewsStore

UTC = dt.timezone.utc


def upto(df: pd.DataFrame, day: dt.date) -> pd.DataFrame:
    return df[df.index <= day]


def _rsi(close: pd.Series, n: int = 14) -> float:
    d = close.diff().dropna().tail(n)
    up, down = d.clip(lower=0).mean(), (-d.clip(upper=0)).mean()
    if len(d) < n or down == 0:
        return float("nan") if len(d) < n else 100.0
    return 100 - 100 / (1 + up / down)


def stats(df: pd.DataFrame) -> Optional[Dict[str, float]]:
    """Key numbers for one ticker from its bars (last row = last close)."""
    if len(df) < 2:
        return None
    c = df["adj_close"]
    last = df.iloc[-1]

    def ret(n):
        return float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100 if len(c) > n \
            else float("nan")

    year = df.tail(252)
    return {
        "open": float(last["open"]), "high": float(last["high"]),
        "low": float(last["low"]), "close": float(last["close"]),
        "volume_m": float(last["volume"]) / 1e6,
        "ret_1d": ret(1), "ret_5d": ret(5), "ret_20d": ret(20),
        "vs_ma50": (float(c.iloc[-1] / c.tail(50).mean() - 1) * 100
                    if len(c) >= 50 else float("nan")),
        "vs_ma200": (float(c.iloc[-1] / c.tail(200).mean() - 1) * 100
                     if len(c) >= 200 else float("nan")),
        "rsi14": _rsi(c),
        "vol20": float(c.pct_change().tail(20).std() * np.sqrt(252) * 100),
        "hi52": float(year["high"].max()), "lo52": float(year["low"].min()),
    }


def _f(x: float, nd: int = 1) -> str:
    return "-" if x is None or not np.isfinite(x) else f"{x:.{nd}f}"


def price_table(prices: Dict[str, pd.DataFrame], tickers: Sequence[str],
                day: dt.date) -> str:
    """One compact line per ticker (percents are percents)."""
    head = ("ticker,close,open,high,low,vol_M,ret1d%,ret5d%,ret20d%,"
            "vsMA50%,vsMA200%,RSI14,volat20%,hi52,lo52")
    lines = [head]
    for t in tickers:
        s = stats(upto(prices.get(t, pd.DataFrame()), day)) \
            if t in prices else None
        if not s:
            continue
        lines.append(",".join([
            t, _f(s["close"], 2), _f(s["open"], 2), _f(s["high"], 2),
            _f(s["low"], 2), _f(s["volume_m"]), _f(s["ret_1d"], 2),
            _f(s["ret_5d"]), _f(s["ret_20d"]), _f(s["vs_ma50"]),
            _f(s["vs_ma200"]), _f(s["rsi14"], 0), _f(s["vol20"], 0),
            _f(s["hi52"], 2), _f(s["lo52"], 2)]))
    return "\n".join(lines)


def headlines(news: Optional[NewsStore], since: dt.datetime,
              until: dt.datetime, tickers: Optional[Sequence[str]] = None,
              per_ticker: int = 2, limit: int = 40) -> List[Dict[str, Any]]:
    """Newest headlines per ticker published in [since, until)."""
    if news is None:
        return []
    h = news.frame("headlines")
    m = news.frame("headline_tickers")
    if h.empty or m.empty:
        return []
    df = m.merge(h, left_on="headline_id", right_on="id")
    df["published"] = pd.to_datetime(df["published"], utc=True,
                                     format="ISO8601", errors="coerce")
    df = df[(df["published"] >= since) & (df["published"] < until)]
    if tickers is not None:
        df = df[df["ticker"].isin(list(tickers))]
    df = (df.sort_values("published", ascending=False)
            .groupby("ticker").head(per_ticker)
            .head(limit))
    return [{"ticker": r.ticker, "title": r.title,
             "published": r.published.strftime("%Y-%m-%d %H:%M UTC")}
            for r in df.itertuples()]


def headline_text(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "(no new headlines)"
    return "\n".join(f"[{i['ticker']}] {i['published']}: {i['title']}"
                     for i in items)


# -- the data a model may request ----------------------------------------

TOOLS = {
    "history": "daily bars for one ticker: {\"tool\": \"history\", "
               "\"ticker\": \"NVDA\", \"days\": 30} (days up to 60)",
    "headlines": "recent headlines for one ticker: {\"tool\": \"headlines\", "
                 "\"ticker\": \"AAPL\"} (up to 10, last 7 days)",
    "compare": "a few tickers' stats side by side: {\"tool\": \"compare\", "
               "\"tickers\": [\"KO\", \"PEP\"]} (up to 5)",
}


def answer(req: Dict[str, Any], prices: Dict[str, pd.DataFrame],
           tickers: Sequence[str], day: dt.date,
           news: Optional[NewsStore], now: dt.datetime) -> str:
    """The reply to one data request, as text. Never raises."""
    tool = str(req.get("tool", "")).lower()
    tick = str(req.get("ticker", "")).upper()
    try:
        if tool == "history":
            if tick not in prices:
                return f"history: unknown ticker {tick!r}"
            days = max(1, min(60, int(req.get("days", 20))))
            df = upto(prices[tick], day).tail(days)
            rows = ["date,open,high,low,close,volume_M"] + [
                f"{d},{r.open:.2f},{r.high:.2f},{r.low:.2f},{r.close:.2f},"
                f"{r.volume / 1e6:.1f}" for d, r in df.iterrows()]
            return f"history {tick} ({len(df)} days):\n" + "\n".join(rows)
        if tool == "headlines":
            if tick not in prices:
                return f"headlines: unknown ticker {tick!r}"
            items = headlines(news, now - dt.timedelta(days=7), now, [tick],
                              per_ticker=10, limit=10)
            return f"headlines {tick}:\n" + headline_text(items)
        if tool == "compare":
            want = [str(t).upper() for t in (req.get("tickers") or [])][:5]
            want = [t for t in want if t in prices]
            if not want:
                return "compare: no known tickers given"
            return "compare:\n" + price_table(prices, want, day)
    except (TypeError, ValueError) as exc:
        return f"{tool}: bad request ({exc})"
    return f"unknown tool {tool!r}; use one of {sorted(TOOLS)}"
