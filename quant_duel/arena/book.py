"""One contestant's paper book: cash, shares, orders, daily marks.

Stored as JSON (data/arena/books/<name>.json), written atomically. An order
names target holdings as percents of the book's money; it fills at the open
of its ``fill_day`` once that day's bar is in the price cache, paying
``cost_bps`` on every dollar traded. Fractional shares are allowed. Prices
are the unadjusted open and close (dividends are not paid in).

Paper only: an order here is a row in a file, nothing else.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from ..market_calendar import trading_days


def new_book(name: str, model: str, cash: float) -> Dict[str, Any]:
    return {"name": name, "model": model, "start_cash": cash, "cash": cash,
            "shares": {}, "orders": [], "marks": {}, "trades": []}


def load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(path: Path, book: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(book, indent=1, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def clean_weights(raw: Any, tickers: Sequence[str]) -> Dict[str, float]:
    """Percents per known ticker, >= 0, summing to at most 100."""
    out: Dict[str, float] = {}
    if isinstance(raw, list):           # [{"ticker": .., "percent": ..}]
        raw = {str(r.get("ticker")): r.get("percent", r.get("weight"))
               for r in raw if isinstance(r, dict)}
    if not isinstance(raw, dict):
        return out
    known = {t.upper(): t for t in tickers}
    for k, v in raw.items():
        t = known.get(str(k).upper().strip())
        try:
            w = float(str(v).rstrip("%"))
        except (TypeError, ValueError):
            continue
        if t and w > 0:
            out[t] = out.get(t, 0.0) + w
    total = sum(out.values())
    if total > 100:
        out = {t: w * 100 / total for t, w in out.items()}
    return {t: round(w, 4) for t, w in out.items() if w >= 0.01}


def price_on(prices: Dict[str, pd.DataFrame], ticker: str, day: dt.date,
             col: str) -> Optional[float]:
    """``col`` on ``day``; for a close, the last known one if it is missing."""
    df = prices.get(ticker)
    if df is None or df.empty:
        return None
    if day in df.index:
        return float(df.at[day, col])
    if col != "close":
        return None
    before = df[df.index <= day]
    return float(before["close"].iloc[-1]) if len(before) else None


def equity(book: Dict[str, Any], prices: Dict[str, pd.DataFrame],
           day: dt.date, col: str = "close") -> float:
    total = book["cash"]
    for t, n in book["shares"].items():
        p = price_on(prices, t, day, col) or price_on(prices, t, day, "close")
        total += n * (p or 0.0)
    return total


def fill(book: Dict[str, Any], order: Dict[str, Any],
         prices: Dict[str, pd.DataFrame], cost_bps: float) -> None:
    day = dt.date.fromisoformat(order["fill_day"])
    order["status"] = "filled"
    if order.get("action") != "trade":
        return                              # hold: nothing changes
    opens = {t: price_on(prices, t, day, "open")
             for t in set(book["shares"]) | set(order["weights"])}
    missing = [t for t, p in opens.items() if not p]
    if missing:
        order["status"] = "failed"
        order["note"] = f"no open price on {day} for {', '.join(missing)}"
        return
    eq = book["cash"] + sum(n * opens[t] for t, n in book["shares"].items())
    rate = cost_bps / 1e4
    # Value to keep for costs, so the book never goes below zero cash.
    turnover = sum(abs(order["weights"].get(t, 0) / 100 * eq
                       - book["shares"].get(t, 0) * opens[t]) for t in opens)
    budget = eq - turnover * rate
    cash = book["cash"]
    for t in sorted(opens):
        have = book["shares"].get(t, 0.0)
        want = order["weights"].get(t, 0) / 100 * budget / opens[t]
        delta = want - have
        if abs(delta * opens[t]) < 0.01:
            continue
        value = delta * opens[t]
        cost = abs(value) * rate
        cash -= value + cost
        book["trades"].append({"day": str(day), "ticker": t,
                               "shares": round(delta, 6),
                               "price": round(opens[t], 4),
                               "value": round(value, 2),
                               "cost": round(cost, 2)})
        if want > 1e-9:
            book["shares"][t] = want
        else:
            book["shares"].pop(t, None)
    book["cash"] = max(cash, 0.0) if abs(cash) < 0.01 else cash
    order["equity_at_open"] = round(eq, 2)


def advance(book: Dict[str, Any], prices: Dict[str, pd.DataFrame],
            start: dt.date, last_day: dt.date, have_day,
            cost_bps: float) -> None:
    """Fill due orders and mark every close up to ``last_day``, in order.
    ``have_day(day)`` says whether that day's bars are in the cache."""
    pending = sorted((o for o in book["orders"] if o["status"] == "pending"),
                     key=lambda o: o["fill_day"])
    for day in trading_days(start, last_day):
        if not have_day(day):
            break                            # wait for the bars
        for o in pending:
            if o["fill_day"] == str(day) and o["status"] == "pending":
                fill(book, o, prices, cost_bps)
        book["marks"][str(day)] = round(equity(book, prices, day), 2)


def marks(book: Dict[str, Any]) -> pd.Series:
    s = pd.Series(book["marks"], dtype=float)
    s.index = pd.to_datetime(s.index).date
    return s.sort_index()


def summary(book: Dict[str, Any], week_ago: Optional[dt.date] = None
            ) -> Dict[str, Any]:
    m = marks(book)
    start = book["start_cash"]
    last = float(m.iloc[-1]) if len(m) else start
    peak = m.cummax() if len(m) else m
    dd = float(((m / peak) - 1).min() * 100) if len(m) else 0.0
    prev = None
    if week_ago is not None and len(m):
        before = m[m.index <= week_ago]
        prev = float(before.iloc[-1]) if len(before) else start
    orders = book["orders"]
    return {
        "name": book["name"], "model": book["model"], "equity": last,
        "total_pct": (last / start - 1) * 100,
        "week_pct": None if prev is None else (last / prev - 1) * 100,
        "max_dd_pct": dd,
        "trades": len(book["trades"]),
        "costs": sum(t["cost"] for t in book["trades"]),
        "days_decided": sum(o["status"] != "failed" and o.get("action")
                            in ("trade", "hold") for o in orders),
        "days_failed": sum(o.get("action") == "error" for o in orders),
        "holdings": {t: n for t, n in book["shares"].items()},
        "cash": book["cash"],
        "last_reason": next((o.get("reason", "") for o in reversed(orders)
                             if o.get("action") in ("trade", "hold")), ""),
    }


__all__ = ["new_book", "load", "save", "clean_weights", "equity", "fill",
           "advance", "marks", "summary", "price_on"]
