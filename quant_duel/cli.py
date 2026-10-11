"""Command line.

    python -m quant_duel.cli ingest          [--source synthetic] [--end 2026-10-08]
    python -m quant_duel.cli build-features  [--end ...]
    python -m quant_duel.cli backtest        [--models always_up,persistence,logistic,boosting]
    python -m quant_duel.cli --node A daily  [--start 2026-09-01] [--end ...] [--no-ingest]
    python -m quant_duel.cli --node A export

Prices and features are shared by both nodes (data/shared/); each node keeps
its own paper ledger (data/<node>/paper.sqlite) and exports (exports/<node>/).
``daily`` = ingest + build-features + one paper step per unprocessed trading
day + export. Paper trading only: nothing here can place an order.
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
from .features.build import build, feature_columns
from .ingest.sources import make_source
from .ingest.store import PriceStore, ingest
from .market_calendar import is_trading_day, previous_trading_day
from .models.base import Boosting, make_model


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


def _price_tickers(cfg: cfgmod.Config) -> list:
    return list(dict.fromkeys(cfg.tickers + cfg["universe"]["context"]))


def build_features(cfg: cfgmod.Config, end: Optional[dt.date] = None):
    """(prices, feature table) from the shared price cache; the table is
    also saved to data/shared/features.parquet."""
    store = PriceStore(cfg.shared_dir / "prices")
    prices = store.load_all(_price_tickers(cfg))
    table = build(prices, cfg["features"], cfg.tickers,
                  context=cfg["universe"]["benchmark"])
    if end:
        table = table[table["date"] <= end]
    path = _features_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = table.copy()
    out["date"] = pd.to_datetime(out["date"])
    tmp = path.with_suffix(".tmp")
    out.to_parquet(tmp)
    tmp.replace(path)
    return prices, table


def cmd_build_features(cfg: cfgmod.Config, args) -> int:
    end = dt.date.fromisoformat(args.end) if args.end else None
    _, table = build_features(cfg, end)
    path = _features_path(cfg)
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
    if results.attrs.get("best_baseline") and "ll_edge" in results:
        print(f"ll_edge: log-loss gain per day over the best baseline "
              f"({results.attrs['best_baseline']}); |t| < 2 is noise")
    if "boosting" in kinds:
        _print_importances(table, cfg)
    for warning in suspicious(results):
        print("WARNING:", warning)
    out = cfg.root / "reports" / "backtest_latest.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(out)
    print(f"saved {out}")
    return 0


def _print_importances(table: pd.DataFrame, cfg: cfgmod.Config) -> None:
    """Which features the boosted model leans on, fit on the most recent
    training window (for reading, not for trading)."""
    bt = cfg["backtest"]
    labelled = table.dropna(subset=["target"])
    dates = sorted(labelled["date"].unique())[-bt["train_days"]:]
    train = labelled[labelled["date"].isin(set(dates))]
    feats = feature_columns(table)
    model = make_model("boosting", cfg["model"]["params"], seed=cfg["seed"])
    assert isinstance(model, Boosting)
    model.fit(train[feats], train["target"].to_numpy())
    top = model.importances().head(10)
    print(f"boosting ({model.backend}) top features, latest window: " +
          ", ".join(f"{k} {v:.0f}" for k, v in top.items()))


def _need_node(cfg: cfgmod.Config) -> None:
    if cfg.node_id not in ("A", "B"):
        raise SystemExit("this command belongs to a node: pass --node A or "
                         "--node B")


def cmd_daily(cfg: cfgmod.Config, args) -> int:
    from .export.csv import export_node
    from .paper.books import BookConfig
    from .paper.daily import node_lock, run_through
    from .paper.ledger import Ledger
    _need_node(cfg)
    end = dt.date.fromisoformat(args.end) if args.end else last_completed_day()
    with node_lock(cfg.node_dir / "daily.lock"):
        if not args.no_ingest:
            if cmd_ingest(cfg, argparse.Namespace(source=args.source,
                                                  end=end.isoformat())):
                print("ingest failed for every ticker — nothing to do")
                return 1
        prices, table = build_features(cfg)
        hashes = PriceStore(cfg.shared_dir / "prices").day_hashes(
            _price_tickers(cfg))
        hashes = dict(zip(hashes["date"], hashes["hash"]))
        ledger = Ledger(cfg.node_dir / "paper.sqlite")
        try:
            if not ledger.initialized:
                ledger.init(cfg.node_id, float(cfg["paper"]["start_cash"]),
                            BookConfig.from_config(cfg),
                            cfg["universe"]["benchmark"])
                print(f"new ledger for node {cfg.node_id} "
                      f"({ledger.path})")
            start = dt.date.fromisoformat(args.start) if args.start else end
            results = run_through(ledger, cfg, table, prices, start, end,
                                  hashes)
            for r in results:
                eq = ", ".join(f"{b} {v:,.0f}" for b, v in
                               sorted(r["equity"].items()))
                print(f"  {r['day']}: {eq}")
            if not results:
                print(f"nothing new to process up to {end} "
                      f"(last run {ledger.last_run()})")
            export_node(ledger, cfg.root / cfg["paper"]["exports_dir"] /
                        cfg.node_id)
        finally:
            ledger.close()
    return 0


def cmd_export(cfg: cfgmod.Config, args) -> int:
    from .export.csv import export_node
    from .paper.ledger import Ledger
    _need_node(cfg)
    path = cfg.node_dir / "paper.sqlite"
    if not path.exists():
        print(f"no ledger yet at {path} — run daily first")
        return 1
    ledger = Ledger(path)
    try:
        out = export_node(ledger, cfg.root / cfg["paper"]["exports_dir"] /
                          cfg.node_id)
    finally:
        ledger.close()
    print(f"exported {len(out)} files to {out[0].parent}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_duel")
    ap.add_argument("--node", default=None, help="A or B")
    ap.add_argument("--root", default=None, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("--source", default=None)
    p.add_argument("--end", default=None)
    p = sub.add_parser("build-features")
    p.add_argument("--end", default=None)
    p = sub.add_parser("backtest")
    p.add_argument("--models",
                   default="always_up,persistence,logistic,boosting")
    p = sub.add_parser("daily")
    p.add_argument("--start", default=None,
                   help="first day for a new ledger (default: --end)")
    p.add_argument("--end", default=None,
                   help="last day to process (default: last completed day)")
    p.add_argument("--source", default=None)
    p.add_argument("--no-ingest", action="store_true")
    sub.add_parser("export")
    args = ap.parse_args(argv)
    cfg = cfgmod.load(args.node, root=Path(args.root) if args.root
                      else cfgmod.ROOT)
    return {"ingest": cmd_ingest, "build-features": cmd_build_features,
            "backtest": cmd_backtest, "daily": cmd_daily,
            "export": cmd_export}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
