"""The paper ledger: one SQLite file per node (``data/<node>/paper.sqlite``).

Holdings are kept as market value per ticker, not share counts, and are
moved day to day by *adjusted* returns read from one consistent price file
— so a split or a dividend re-adjustment cannot corrupt the books (a raw
share count would halve in value on a 2:1 split). Share counts in ``fills``
are informational (notional / raw fill price).

Accounting identity, checked in tests: cash + Σ holdings = equity, every
day, for every book. Costs can take cash a few basis points below zero
after a full rotation; that is bookkeeping, not leverage.

Each trading day is processed once, inside one transaction; a re-run of a
finished day is a no-op.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pandas as pd

from .books import BookConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS books (
    book TEXT PRIMARY KEY, kind TEXT NOT NULL, config_json TEXT,
    cash REAL NOT NULL, start_cash REAL NOT NULL);
CREATE TABLE IF NOT EXISTS holdings (
    book TEXT NOT NULL, ticker TEXT NOT NULL, value REAL NOT NULL,
    PRIMARY KEY (book, ticker));
CREATE TABLE IF NOT EXISTS models (
    model_id TEXT NOT NULL, book TEXT NOT NULL, first_day TEXT NOT NULL,
    spec_json TEXT NOT NULL, note TEXT, PRIMARY KEY (book, model_id));
CREATE TABLE IF NOT EXISTS predictions (
    book TEXT NOT NULL, date TEXT NOT NULL, ticker TEXT NOT NULL,
    p REAL NOT NULL, signal INTEGER NOT NULL, model_id TEXT,
    target REAL, fwd_return REAL, PRIMARY KEY (book, date, ticker));
CREATE TABLE IF NOT EXISTS orders (
    book TEXT NOT NULL, signal_date TEXT NOT NULL, ticker TEXT NOT NULL,
    weight REAL NOT NULL, filled_on TEXT,
    PRIMARY KEY (book, signal_date, ticker));
CREATE TABLE IF NOT EXISTS fills (
    book TEXT NOT NULL, date TEXT NOT NULL, ticker TEXT NOT NULL,
    notional REAL NOT NULL, price REAL NOT NULL, shares REAL NOT NULL,
    cost REAL NOT NULL);
CREATE TABLE IF NOT EXISTS equity (
    book TEXT NOT NULL, date TEXT NOT NULL, cash REAL NOT NULL,
    holdings REAL NOT NULL, equity REAL NOT NULL, traded REAL NOT NULL,
    PRIMARY KEY (book, date));
CREATE TABLE IF NOT EXISTS runs (
    date TEXT PRIMARY KEY, price_hash TEXT, finished_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    date TEXT NOT NULL, book TEXT, kind TEXT NOT NULL, message TEXT NOT NULL);
"""


def _d(day: dt.date) -> str:
    return day.isoformat()


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _one(self, sql: str, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return row[0] if row else None

    # -- setup ------------------------------------------------------------
    @property
    def initialized(self) -> bool:
        return self._one("SELECT COUNT(*) FROM books") > 0

    def init(self, node_id: str, start_cash: float, control: BookConfig,
             benchmark: str) -> None:
        """Create the books. ``control`` is frozen here for the life of the
        ledger; ``live`` starts as an identical copy."""
        if self.initialized:
            raise RuntimeError("ledger already initialized")
        with self.transaction():
            for book, kind, conf in (("live", "model", control.to_json()),
                                     ("control", "model", control.to_json()),
                                     ("spy", "benchmark", None)):
                self.conn.execute(
                    "INSERT INTO books VALUES (?,?,?,?,?)",
                    (book, kind, conf, start_cash, start_cash))
            for k, v in (("node_id", node_id), ("benchmark", benchmark),
                         ("start_cash", repr(start_cash))):
                self.conn.execute("INSERT INTO meta VALUES (?,?)", (k, v))

    def meta(self, key: str) -> Optional[str]:
        return self._one("SELECT value FROM meta WHERE key=?", (key,))

    def book_config(self, book: str) -> BookConfig:
        import json
        raw = self._one("SELECT config_json FROM books WHERE book=?", (book,))
        if raw is None:
            raise KeyError(f"book {book!r} has no model config")
        return BookConfig.from_dict(json.loads(raw))

    def set_book_config(self, book: str, conf: BookConfig) -> None:
        if book != "live":
            raise PermissionError("only the live book can change")
        self.conn.execute("UPDATE books SET config_json=? WHERE book=?",
                          (conf.to_json(), book))

    # -- cash and holdings ------------------------------------------------
    def cash(self, book: str) -> float:
        return float(self._one("SELECT cash FROM books WHERE book=?", (book,)))

    def set_cash(self, book: str, cash: float) -> None:
        self.conn.execute("UPDATE books SET cash=? WHERE book=?", (cash, book))

    def holdings(self, book: str) -> Dict[str, float]:
        return dict(self.conn.execute(
            "SELECT ticker, value FROM holdings WHERE book=?", (book,)))

    def set_holdings(self, book: str, values: Dict[str, float]) -> None:
        self.conn.execute("DELETE FROM holdings WHERE book=?", (book,))
        self.conn.executemany(
            "INSERT INTO holdings VALUES (?,?,?)",
            [(book, t, v) for t, v in sorted(values.items()) if v != 0.0])

    # -- runs ---------------------------------------------------------------
    def has_run(self, day: dt.date) -> bool:
        return self._one("SELECT 1 FROM runs WHERE date=?", (_d(day),)) is not None

    def last_run(self) -> Optional[dt.date]:
        v = self._one("SELECT MAX(date) FROM runs")
        return dt.date.fromisoformat(v) if v else None

    def mark_run(self, day: dt.date, price_hash: Optional[str]) -> None:
        self.conn.execute(
            "INSERT INTO runs VALUES (?,?,?)",
            (_d(day), price_hash,
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))

    def event(self, day: dt.date, book: Optional[str], kind: str,
              message: str) -> None:
        self.conn.execute("INSERT INTO events VALUES (?,?,?,?)",
                          (_d(day), book, kind, message))

    # -- models -------------------------------------------------------------
    def add_model(self, book: str, model_id: str, first_day: dt.date,
                  spec_json: str, note: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO models VALUES (?,?,?,?,?)",
            (model_id, book, _d(first_day), spec_json, note))

    def current_model(self, book: str) -> Optional[str]:
        return self._one(
            "SELECT spec_json FROM models WHERE book=? "
            "ORDER BY first_day DESC, rowid DESC LIMIT 1", (book,))

    # -- predictions and orders --------------------------------------------
    def add_predictions(self, book: str, day: dt.date, rows: pd.DataFrame,
                        model_id: Optional[str]) -> None:
        self.conn.executemany(
            "INSERT INTO predictions VALUES (?,?,?,?,?,?,NULL,NULL)",
            [(book, _d(day), r.ticker, float(r.p), int(r.signal), model_id)
             for r in rows.itertuples()])

    def score(self, day: dt.date, outcomes: pd.DataFrame) -> int:
        """Fill in the realised label for predictions made on ``day``."""
        cur = self.conn.executemany(
            "UPDATE predictions SET target=?, fwd_return=? "
            "WHERE date=? AND ticker=?",
            [(float(r.target), float(r.fwd_return), _d(day), r.ticker)
             for r in outcomes.itertuples()])
        return cur.rowcount

    def add_orders(self, book: str, day: dt.date,
                   weights: Dict[str, float]) -> None:
        self.conn.executemany(
            "INSERT INTO orders VALUES (?,?,?,?,NULL)",
            [(book, _d(day), t, w) for t, w in sorted(weights.items())])

    def pending_orders(self, book: str) -> Optional[Dict[str, float]]:
        """The newest unfilled order set (target weights), or None."""
        day = self._one("SELECT MAX(signal_date) FROM orders "
                        "WHERE book=? AND filled_on IS NULL", (book,))
        if day is None:
            return None
        return dict(self.conn.execute(
            "SELECT ticker, weight FROM orders WHERE book=? AND "
            "signal_date=?", (book, day)))

    def close_orders(self, book: str, day: dt.date) -> None:
        self.conn.execute("UPDATE orders SET filled_on=? WHERE book=? AND "
                          "filled_on IS NULL", (_d(day), book))

    def add_fill(self, book: str, day: dt.date, ticker: str, notional: float,
                 price: float, cost: float) -> None:
        self.conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?)",
                          (book, _d(day), ticker, notional, price,
                           notional / price if price else 0.0, cost))

    def add_equity(self, book: str, day: dt.date, cash: float,
                   holdings: float, traded: float) -> None:
        self.conn.execute("INSERT INTO equity VALUES (?,?,?,?,?,?)",
                          (book, _d(day), cash, holdings, cash + holdings,
                           traded))

    # -- reading ------------------------------------------------------------
    def frame(self, table: str, where: str = "", args=()) -> pd.DataFrame:
        if table not in {"books", "holdings", "models", "predictions",
                         "orders", "fills", "equity", "runs", "events",
                         "meta"}:
            raise ValueError(table)
        sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
        return pd.read_sql_query(sql, self.conn, params=args)

    def books(self) -> List[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT book FROM books ORDER BY rowid")]
