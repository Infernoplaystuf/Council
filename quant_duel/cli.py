"""Command line. Phase 1: ingest, build-features, backtest.

    python -m quant_duel.cli ingest          [--source synthetic] [--end 2026-10-08]
    python -m quant_duel.cli build-features  [--end ...]
    python -m quant_duel.cli backtest        [--models always_up,persistence,logistic]

Prices and features are shared by both nodes (data/shared/); --node only
matters from phase 3 on, where each node keeps its own ledger.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from . import config as cfgmod
from .backtest.run import compare_models, suspicious
from .features.build import build
from .ingest.sources import make_source
from .ingest.store import PriceStore, ingest
from .market_calendar import is_trading_day, previous_trading_day


def last_completed_day(now: Optional[dt.datetime] = None) -> dt.date:
    """The last trading day whose close has happened, in New York time
    (a day counts once it is past 16:30 there, so its bar is published)."""
    from zoneinfo import ZoneInfo
    now = now or dt.datetime.now(ZoneInfo("America/New_York"))
    day = now.date()
    if is_trading_day(day) and (now.hour, now.minute) >= (16, 30):
        return day
    return previous_trading_day(day)


def _features_path(cfg: cfgmod.Config) -> Path:
    return cfg.shared_dir / "features.parquet"


def cmd_ingest(cfg: cfgmod.Config, args) -> int:
    source = make_source(args.source or cfg["data"]["source"], cfg["seed"])
    end = dt.date.fromisoformat(args.end) if args.end else last_completed_day()
    start = dt.date.fromisoformat(cfg["data"]["history_start"])
    store = PriceStore(cfg.shared_dir / "prices")
    reports = ingest(store, source, cfg.tickers, start, end)
    bad = 0
    for r in reports:
        if "error" in r:
            bad += 1
            print(f"  {r['ticker']}: ERROR {r['error']}")
        elif r["problems"]:
            print(f"  {r['ticker']}: {r['rows']} rows; " + "; ".join(r["problems"]))
    print(f"ingest ({source.name}) to {end}: {len(reports) - bad}/"
          f"{len(reports)} tickers updated")
    return 1 if bad == len(reports) else 0


def cmd_build_features(cfg: cfgmod.Config, args) -> int:
    store = PriceStore(cfg.shared_dir / "prices")
    prices = store.load_all(cfg.tickers + cfg["universe"]["context"])
    table = build(prices, cfg["features"], cfg.tickers,
                  context=cfg["universe"]["benchmark"])
    if args.end:
        table = table[table["date"] <= dt.date.fromisoformat(args.end)]
    path = _features_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = table.copy()
    out["date"] = pd.to_datetime(out["date"])
    out.to_parquet(path)
    print(f"features: {len(table)} rows, {table['ticker'].nunique()} tickers, "
          f"{table['date'].min()} → {table['date'].max()} ({path})")
    return 0


def load_features(cfg: cfgmod.Config) -> pd.DataFrame:
    table = pd.read_parquet(_features_path(cfg))
    table["date"] = pd.to_datetime(table["date"]).dt.date
    return table


def cmd_backtest(cfg: cfgmod.Config, args) -> int:
    table = load_features(cfg)
    kinds = [k.strip() for k in args.models.split(",") if k.strip()]
    results = compare_models(table, kinds, cfg.raw, seed=cfg["seed"])
    with pd.option_context("display.width", 160, "display.max_columns", 20,
                           "display.float_format", "{:.4f}".format):
        print(results)
    for warning in suspicious(results):
        print("WARNING:", warning)
    out = cfg.root / "reports" / "backtest_latest.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(out)
    print(f"saved {out}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_duel")
    ap.add_argument("--node", default=None, help="A or B (from phase 3)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("--source", default=None)
    p.add_argument("--end", default=None)
    p = sub.add_parser("build-features")
    p.add_argument("--end", default=None)
    p = sub.add_parser("backtest")
    p.add_argument("--models", default="always_up,persistence,logistic")
    args = ap.parse_args(argv)
    cfg = cfgmod.load(args.node)
    return {"ingest": cmd_ingest, "build-features": cmd_build_features,
            "backtest": cmd_backtest}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
