"""The shared price cache: one parquet file per ticker, fetched incrementally,
plus a content hash per trading day.

Both nodes read the same files, so they see byte-identical prices. The
per-day hash (over every ticker's row for that day, rounded to 6 significant
digits) is what ``compare`` checks across exports to prove it.

Incremental: only days after the last stored day are fetched, plus a short
overlap. If the overlap's adjusted closes moved (a dividend or split
re-adjusted history) the ticker is re-fetched from the start.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd

from . import validate
from .sources import COLUMNS, DataSource

OVERLAP_DAYS = 10
ADJ_TOLERANCE = 1e-6


class PriceStore:
    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def path(self, ticker: str) -> Path:
        return self.folder / f"{ticker}.parquet"

    def load(self, ticker: str) -> pd.DataFrame:
        p = self.path(ticker)
        if not p.exists():
            return pd.DataFrame(columns=COLUMNS,
                                index=pd.Index([], name="date"))
        df = pd.read_parquet(p)
        df.index = pd.Index(pd.to_datetime(df.index).date, name="date")
        return df

    def save(self, ticker: str, df: pd.DataFrame) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        out = df.copy()
        out.index = pd.to_datetime(pd.Index(out.index, name="date"))
        tmp = self.path(ticker).with_suffix(".tmp")
        out.to_parquet(tmp)
        tmp.replace(self.path(ticker))

    def update(self, ticker: str, source: DataSource, start: dt.date,
               end: dt.date) -> Dict[str, object]:
        """Fetch what is missing up to ``end``; returns a small report."""
        have = self.load(ticker)
        refetched = False
        if have.empty:
            new = source.fetch(ticker, start, end)
            merged = new
        else:
            overlap_start = sorted(have.index)[max(0, len(have) - OVERLAP_DAYS)]
            new = source.fetch(ticker, overlap_start, end)
            common = have.index.intersection(new.index)
            if len(common):
                a = have.loc[common, "adj_close"]
                b = new.loc[common, "adj_close"]
                if ((a - b).abs() / a).max() > ADJ_TOLERANCE:
                    new = source.fetch(ticker, start, end)    # re-adjusted
                    merged = new
                    refetched = True
            if not refetched:
                merged = pd.concat([have[have.index < overlap_start], new])
        merged = validate.clean(merged)
        self.save(ticker, merged)
        return {"ticker": ticker, "rows": len(merged),
                "added": len(merged) - len(have), "refetched": refetched,
                "problems": validate.problems(ticker, merged)}

    def load_all(self, tickers: Sequence[str]) -> Dict[str, pd.DataFrame]:
        return {t: self.load(t) for t in tickers}

    # -- hashes -----------------------------------------------------------
    def day_hashes(self, tickers: Sequence[str]) -> pd.DataFrame:
        """date → sha256 of every ticker's row that day."""
        parts: Dict[dt.date, List[str]] = {}
        for t in sorted(tickers):
            df = self.load(t)
            for day, row in df.iterrows():
                vals = ",".join(f"{float(row[c]):.6g}" for c in COLUMNS)
                parts.setdefault(day, []).append(f"{t}:{vals}")
        rows = [{"date": d, "hash": hashlib.sha256(
            "|".join(v).encode()).hexdigest()[:16]}
            for d, v in sorted(parts.items())]
        return pd.DataFrame(rows, columns=["date", "hash"])

    def write_hashes(self, tickers: Sequence[str],
                     path: Optional[Path] = None) -> Path:
        path = path or (self.folder / "day_hashes.csv")
        self.day_hashes(tickers).to_csv(path, index=False)
        return path


def ingest(store: PriceStore, source: DataSource, tickers: Sequence[str],
           start: dt.date, end: dt.date) -> List[Dict[str, object]]:
    """Update every ticker; a failure on one does not stop the rest."""
    reports = []
    for t in tickers:
        try:
            reports.append(store.update(t, source, start, end))
        except Exception as exc:                          # noqa: BLE001
            reports.append({"ticker": t, "error": str(exc)})
    store.write_hashes(tickers)
    return reports
