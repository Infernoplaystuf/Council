"""One paper-trading day, run after the close of trading day ``d``:

1. **Settle** each book: holdings move from the previous close to ``d``'s
   fill point by adjusted returns; the orders placed at the previous close
   fill there (``paper.fill``: ``open`` = ``d``'s open, ``close`` = ``d``'s
   close) with ``backtest.cost_bps`` on every traded dollar; then mark to
   ``d``'s close.
2. **Score** the previous day's predictions (their label — the move into
   ``d``'s close — is now known).
3. **Predict** every ticker from features dated ``d`` or earlier, for the
   live and the control book, and place orders: equal weight over the
   tickers predicted, long where P(up) clears the book's threshold. On the
   news node only, the LIVE book's P(up) gets the sentiment overlay
   (``p + w * sentiment``, clipped) from headlines published before ``d``'s
   cutoff; the control book never sees news.
   The SPY book buys the benchmark once and holds it.

Everything for day ``d`` is one transaction. ``run_through`` steps through
every missed trading day in order, each seeing only the data it would have
seen live (``as_of``) — so catching up gives exactly the ledger that daily
runs would have.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Dict, List, Mapping, Optional

import pandas as pd

from ..locks import file_lock
from ..market_calendar import trading_days
from ..news.sentiment import lookup, overlay
from . import artifact
from .books import MODEL_BOOKS, BookConfig
from .ledger import Ledger


def as_of(table: pd.DataFrame, day: dt.date) -> pd.DataFrame:
    """The feature table as it stood at ``day``'s close: no later rows, and
    no label on ``day`` itself (tomorrow has not happened)."""
    out = table[table["date"] <= day].copy()
    today = out["date"] == day
    out.loc[today, ["target", "fwd_return"]] = float("nan")
    return out


def _bar(prices: Mapping[str, pd.DataFrame], ticker: str, day: dt.date):
    df = prices.get(ticker)
    if df is None or day not in df.index:
        return None
    row = df.loc[day]
    factor = float(row["adj_close"]) / float(row["close"])
    return {"open": float(row["open"]), "close": float(row["close"]),
            "adj_open": float(row["open"]) * factor,
            "adj_close": float(row["adj_close"])}


def _settle(ledger: Ledger, book: str, prices, last: Optional[dt.date],
            day: dt.date, fill_at: str, cost: float) -> None:
    cash = ledger.cash(book)
    hold = ledger.holdings(book)
    orders = ledger.pending_orders(book)
    bars = {t: _bar(prices, t, day)
            for t in set(hold) | set(orders or {})}
    prev = {t: _bar(prices, t, last) if last else None for t in hold}

    def to_open(t: str, v: float) -> float:
        b, p = bars.get(t), prev.get(t)
        if b is None or p is None:
            return v
        return v * b["adj_open"] / p["adj_close"]

    def intraday(t: str, v: float) -> float:
        b = bars.get(t)
        return v if b is None else v * b["adj_close"] / b["adj_open"]

    missing = sorted(t for t, b in bars.items() if b is None)
    if missing:
        ledger.event(day, book, "missing_bar",
                     "no price bar for " + ", ".join(missing) +
                     " — held at last value, not traded")
    hold = {t: to_open(t, v) for t, v in hold.items()}
    if fill_at == "close":
        hold = {t: intraday(t, v) for t, v in hold.items()}
    traded = 0.0
    if orders is not None:
        equity = cash + sum(hold.values())
        for t in sorted(set(hold) | set(orders)):
            b = bars.get(t)
            if b is None:
                continue
            target = orders.get(t, 0.0) * equity
            trade = target - hold.get(t, 0.0)
            if abs(trade) < 1e-9:
                continue
            fee = abs(trade) * cost
            cash -= trade + fee
            hold[t] = target
            traded += abs(trade)
            ledger.add_fill(book, day, t, trade, b[fill_at], fee)
        ledger.close_orders(book, day)
    if fill_at == "open":
        hold = {t: intraday(t, v) for t, v in hold.items()}
    hold = {t: v for t, v in hold.items() if abs(v) > 1e-9}
    ledger.set_cash(book, cash)
    ledger.set_holdings(book, hold)
    ledger.add_equity(book, day, cash, sum(hold.values()), traded)


def _model_for(ledger: Ledger, book: str, conf: BookConfig,
               table: pd.DataFrame, day: dt.date, seed: int,
               cache: Dict[str, tuple]):
    """The book's current model: reuse (refit from its spec) until it has
    served ``refit_days`` days or the book's settings change, then fit a
    new one on the latest window — the walk-forward cadence."""
    raw = ledger.current_model(book)
    note = ""
    if raw is not None:
        spec = artifact.Spec(json.loads(raw))
        served = len(trading_days(spec.first_day, day)) - 1
        if spec.book.digest != conf.digest:
            note = "settings changed"
        elif served >= conf.refit_days:
            note = f"scheduled refit after {served} days"
        else:
            key = spec.to_json()
            if key in cache:
                return cache[key][0], spec
            try:
                model = artifact.refit(spec, table)
                cache[key] = (model, spec)
                return model, spec
            except artifact.ArtifactMismatch as exc:
                note = f"{exc}; fitted afresh"
                ledger.event(day, book, "refit", note)
    key = f"new:{conf.to_json()}"
    if key in cache:
        model, spec = cache[key]
    else:
        model, spec = artifact.fit(conf, table, day, seed)
        cache[key] = (model, spec)
        cache[spec.to_json()] = (model, spec)
    ledger.add_model(book, spec.model_id, day, spec.to_json(),
                     note or "first model")
    return model, spec


def _predict(ledger: Ledger, book: str, table: pd.DataFrame, day: dt.date,
             seed: int, cache: Dict[str, tuple],
             sentiment: Optional[pd.DataFrame] = None) -> None:
    conf = ledger.book_config(book)
    model, spec = _model_for(ledger, book, conf, table, day, seed, cache)
    rows = table[table["date"] == day]
    p = model.predict_proba(rows[spec.data["features"]])
    out = pd.DataFrame({"ticker": rows["ticker"].to_numpy(), "p": p})
    if sentiment is not None:
        s = lookup(sentiment, [day] * len(out), out["ticker"])
        out["p_base"] = p
        out["sentiment"] = s
        out["p"] = overlay(p, s, conf.sentiment_weight)
    if conf.model_kind == "always_up":
        out["signal"] = 1
    else:
        out["signal"] = (out["p"] >= conf.threshold).astype(int)
    ledger.add_predictions(book, day, out, spec.model_id)
    n = len(out)
    ledger.add_orders(book, day, {r.ticker: r.signal / n
                                  for r in out.itertuples()})


def step(ledger: Ledger, cfg, table: pd.DataFrame,
         prices: Mapping[str, pd.DataFrame], day: dt.date,
         price_hash: Optional[str] = None,
         cache: Optional[Dict[str, tuple]] = None,
         sentiment: Optional[pd.DataFrame] = None) -> Dict[str, object]:
    """Process trading day ``day`` once (see the module docstring).
    ``sentiment`` (date, ticker, sentiment) is given on the news node only;
    only its rows for ``day`` are read."""
    if ledger.has_run(day):
        return {"day": day, "skipped": True}
    last = ledger.last_run()
    if last is not None and day <= last:
        raise ValueError(f"{day} is before the last processed day {last}")
    cut = as_of(table, day)
    if not (cut["date"] == day).any():
        raise ValueError(f"no feature rows for {day}")
    paper = cfg["paper"]
    cost = cfg["backtest"]["cost_bps"] / 1e4
    bench = ledger.meta("benchmark")
    cache = {} if cache is None else cache
    with ledger.transaction():
        for book in ledger.books():
            _settle(ledger, book, prices, last, day, paper["fill"], cost)
        scored = 0
        if last is not None:
            done = cut[cut["date"] == last].dropna(subset=["target"])
            scored = ledger.score(last, done)
        today = None
        if sentiment is not None:
            today = sentiment[sentiment["date"] == day]
        for book in MODEL_BOOKS:
            _predict(ledger, book, cut, day, cfg["seed"], cache,
                     today if book == "live" else None)
        if not ledger.holdings("spy") and ledger.pending_orders("spy") is None:
            ledger.add_orders("spy", day, {bench: 1.0})
        ledger.mark_run(day, price_hash)
    eq = ledger.frame("equity", "date=?", (day.isoformat(),))
    return {"day": day, "skipped": False, "scored": scored,
            "equity": dict(zip(eq["book"], eq["equity"]))}


def run_through(ledger: Ledger, cfg, table: pd.DataFrame,
                prices: Mapping[str, pd.DataFrame], start: dt.date,
                end: dt.date, hashes: Optional[Mapping[dt.date, str]] = None,
                sentiment: Optional[pd.DataFrame] = None
                ) -> List[Dict[str, object]]:
    """Every unprocessed trading day from ``start`` (or the day after the
    last run) to ``end``, in order."""
    last = ledger.last_run()
    days = sorted(d for d in table["date"].unique()
                  if start <= d <= end and (last is None or d > last))
    cache: Dict[str, tuple] = {}
    out = []
    for d in days:
        out.append(step(ledger, cfg, table, prices, d,
                        (hashes or {}).get(d), cache, sentiment))
        # Only the models of the newest day can be reused.
        keep = {k: v for k, v in cache.items() if not k.startswith("new:")}
        cache.clear()
        cache.update(keep)
    return out


def node_lock(path: Path):
    """One daily run per node at a time (a stale lock is broken)."""
    return file_lock(path)
