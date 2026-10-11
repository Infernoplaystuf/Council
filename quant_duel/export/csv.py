"""Tidy CSVs, one file per table, every row tagged with its node:

    exports/<node>/predictions.csv   book, date, ticker, p, signal, target...
    exports/<node>/fills.csv         every paper fill with its cost
    exports/<node>/equity.csv        cash, holdings, equity per book per day
    exports/<node>/equity_curves.csv date × book, equity / start cash
    exports/<node>/holdings.csv      current holdings
    exports/<node>/models.csv        every model spec (window, hashes)
    exports/<node>/price_hashes.csv  the price-data hash of each run day
    exports/<node>/events.csv        refits, missing bars, tuner adoptions
    exports/<node>/tuner_log.csv     every proposal, its gate result, verdict
    exports/<node>/metrics.csv       prediction + strategy metrics per book

Long format throughout (Power BI and Tableau like it best); files are
written atomically so a reader never sees half a file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

from ..backtest import metrics as mx
from ..paper.ledger import Ledger


def write_csv(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)
    return path


def book_returns(ledger: Ledger) -> Dict[str, pd.DataFrame]:
    """Per book: date, daily return (vs previous equity, or start cash on
    the first day) and turnover (traded / equity before trading)."""
    eq = ledger.frame("equity").sort_values(["book", "date"])
    books = ledger.frame("books").set_index("book")
    out = {}
    for book, g in eq.groupby("book"):
        before = g["equity"].shift(1).fillna(books.loc[book, "start_cash"])
        out[book] = pd.DataFrame({
            "date": g["date"].to_numpy(),
            "ret": (g["equity"] / before - 1).to_numpy(),
            "turnover": (g["traded"] / before).to_numpy()})
    return out


def metrics(ledger: Ledger) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    preds = ledger.frame("predictions").dropna(subset=["target"])
    for book, r in book_returns(ledger).items():
        row: Dict[str, object] = {"book": book}
        p = preds[preds["book"] == book]
        if len(p):
            row.update(mx.prediction_metrics(p["p"].to_numpy(),
                                             p["target"].to_numpy()))
        row.update(mx.strategy_metrics(r["ret"], r["turnover"]))
        rows.append(row)
    return pd.DataFrame(rows)


def export_node(ledger: Ledger, out_dir: Path) -> List[Path]:
    node = ledger.meta("node_id") or "?"
    out_dir = Path(out_dir)

    def tag(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.insert(0, "node", node)
        return df

    written = []
    for name in ("predictions", "fills", "equity", "holdings", "events",
                 "tuner_log"):
        written.append(write_csv(tag(ledger.frame(name)),
                                 out_dir / f"{name}.csv"))
    models = ledger.frame("models")
    if len(models):
        specs = models["spec_json"].map(json.loads)
        for k in ("train_start", "train_end", "n_rows", "backend",
                  "data_hash", "fingerprint"):
            models[k] = specs.map(lambda s, k=k: s.get(k))
    written.append(write_csv(tag(models), out_dir / "models.csv"))
    runs = ledger.frame("runs")[["date", "price_hash"]]
    written.append(write_csv(tag(runs), out_dir / "price_hashes.csv"))
    eq = ledger.frame("equity")
    start = ledger.frame("books").set_index("book")["start_cash"]
    if len(eq):
        eq["index"] = eq["equity"] / eq["book"].map(start)
        curves = eq.pivot(index="date", columns="book", values="index")
        curves = curves.reset_index()
    else:
        curves = pd.DataFrame(columns=["date"])
    written.append(write_csv(tag(curves), out_dir / "equity_curves.csv"))
    m = metrics(ledger) if len(eq) else pd.DataFrame()
    written.append(write_csv(tag(m.replace([np.inf, -np.inf], np.nan)),
                             out_dir / "metrics.csv"))
    return written
