"""Headlines, their tickers and their scores: ``data/A/news.sqlite``.

Everything is kept — every headline, every ticker mapping, every score —
so a longer future experiment can train sentiment as a real feature.
A headline's id is a hash of its normalised title, so the same story seen
in several feeds is stored once (with the earliest publish time) and
mapped to every ticker that saw it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence

import pandas as pd

from .feeds import Item
from .timing import effective_time, parse_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS headlines (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, link TEXT, source TEXT NOT NULL,
    raw_time TEXT, published TEXT, fetched TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS headline_tickers (
    headline_id TEXT NOT NULL, ticker TEXT NOT NULL, method TEXT NOT NULL,
    PRIMARY KEY (headline_id, ticker));
CREATE TABLE IF NOT EXISTS scores (
    headline_id TEXT NOT NULL, ticker TEXT NOT NULL, score REAL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    model TEXT, scored_at TEXT, PRIMARY KEY (headline_id, ticker));
CREATE TABLE IF NOT EXISTS polls (
    at TEXT NOT NULL, source TEXT NOT NULL, items INTEGER NOT NULL,
    new INTEGER NOT NULL, error TEXT);
"""


def headline_id(title: str) -> str:
    norm = " ".join(title.lower().split())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def _iso(t: Optional[dt.datetime]) -> Optional[str]:
    return t.isoformat(timespec="seconds") if t else None


class NewsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
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

    def add(self, source: str, items: Iterable[Item],
            tickers_for: Dict[str, Sequence[str]], fetched: dt.datetime,
            method_for: Optional[Dict[str, Dict[str, str]]] = None) -> int:
        """Store ``items``; ``tickers_for[title]`` lists the tickers of each.
        Returns how many headlines were new."""
        new = 0
        with self.transaction():
            for it in items:
                hid = headline_id(it.title)
                pub = effective_time(parse_time(it.raw_time), fetched)
                row = self.conn.execute(
                    "SELECT published FROM headlines WHERE id=?",
                    (hid,)).fetchone()
                if row is None:
                    self.conn.execute(
                        "INSERT INTO headlines VALUES (?,?,?,?,?,?,?)",
                        (hid, it.title, it.link, source, it.raw_time,
                         _iso(pub), _iso(fetched)))
                    new += 1
                elif pub is not None and (row[0] is None or _iso(pub) < row[0]):
                    self.conn.execute("UPDATE headlines SET published=? "
                                      "WHERE id=?", (_iso(pub), hid))
                methods = (method_for or {}).get(it.title, {})
                for t in tickers_for.get(it.title, ()):
                    self.conn.execute(
                        "INSERT OR IGNORE INTO headline_tickers VALUES (?,?,?)",
                        (hid, t, methods.get(t, "title")))
                    self.conn.execute(
                        "INSERT OR IGNORE INTO scores (headline_id, ticker, "
                        "status) VALUES (?,?,'pending')", (hid, t))
        return new

    def log_poll(self, source: str, items: int, new: int,
                 error: Optional[str] = None) -> None:
        self.conn.execute("INSERT INTO polls VALUES (?,?,?,?,?)",
                          (_iso(dt.datetime.now(dt.timezone.utc)), source,
                           items, new, error))

    def pairs(self) -> pd.DataFrame:
        """Every (headline, ticker) pair with its publish time and score."""
        df = pd.read_sql_query(
            "SELECT h.id AS headline_id, t.ticker, h.title, h.source, "
            "h.published, h.fetched, s.score, s.status, s.attempts "
            "FROM headline_tickers t JOIN headlines h ON h.id = t.headline_id "
            "JOIN scores s ON s.headline_id = t.headline_id AND "
            "s.ticker = t.ticker", self.conn)
        df["published"] = pd.to_datetime(df["published"], utc=True,
                                         format="ISO8601")
        return df

    def set_scores(self, results: List[Dict], model: str) -> None:
        now = _iso(dt.datetime.now(dt.timezone.utc))
        with self.transaction():
            for r in results:
                if r.get("score") is None:
                    self.conn.execute(
                        "UPDATE scores SET attempts = attempts + 1, "
                        "status = CASE WHEN attempts + 1 >= ? THEN 'failed' "
                        "ELSE 'pending' END WHERE headline_id=? AND ticker=?",
                        (r["max_attempts"], r["headline_id"], r["ticker"]))
                else:
                    self.conn.execute(
                        "UPDATE scores SET score=?, status='scored', "
                        "attempts = attempts + 1, model=?, scored_at=? "
                        "WHERE headline_id=? AND ticker=?",
                        (float(r["score"]), model, now, r["headline_id"],
                         r["ticker"]))

    def frame(self, table: str) -> pd.DataFrame:
        if table not in {"headlines", "headline_tickers", "scores", "polls"}:
            raise ValueError(table)
        return pd.read_sql_query(f"SELECT * FROM {table}", self.conn)
