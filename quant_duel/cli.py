"""Command line.

    python -m quant_duel.cli ingest          [--source synthetic] [--end 2026-10-08]
    python -m quant_duel.cli build-features  [--end ...]
    python -m quant_duel.cli backtest        [--models always_up,persistence,logistic,boosting]
    python -m quant_duel.cli --node A daily  [--start 2026-09-01] [--end ...] [--no-ingest]
    python -m quant_duel.cli --node A export
    python -m quant_duel.cli --node A tune   [--day ...] [--proposal file.json]
    python -m quant_duel.cli --node A report [--day ...]
    python -m quant_duel.cli --node A news-poll  [--no-score]   (node A only)
    python -m quant_duel.cli --node A news-score
    python -m quant_duel.cli --node A news-status
    python -m quant_duel.cli experiment-init --start 2026-11-02 --end 2026-12-01
    python -m quant_duel.cli compare [--a exports/A] [--b exports/B]
    python -m quant_duel.cli replay --start 2026-09-01 --end 2026-09-30

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


def _news_path(cfg: cfgmod.Config) -> Path:
    return cfg.node_dir / "news.sqlite"


def load_sentiment(cfg: cfgmod.Config):
    """Daily sentiment on the news node; None on the other (never read)."""
    if not cfg.news_enabled:
        return None
    from .news.sentiment import daily_sentiment
    from .news.store import NewsStore
    path = _news_path(cfg)
    if not path.exists():
        return pd.DataFrame(columns=["date", "ticker", "sentiment", "n"])
    store = NewsStore(path)
    try:
        n = cfg["news"]
        return daily_sentiment(store.pairs(), n["cutoff"],
                               int(n["max_per_ticker_day"]))
    finally:
        store.close()


def _need_news(cfg: cfgmod.Config) -> None:
    _need_node(cfg)
    if not cfg.news_enabled:
        raise SystemExit(f"node {cfg.node_id} never sees news "
                         "(news_enabled: false)")


def cmd_news_poll(cfg: cfgmod.Config, args) -> int:
    from .news.poll import poll
    from .news.store import NewsStore
    _need_news(cfg)
    store = NewsStore(_news_path(cfg))
    try:
        rep = poll(store, cfg["news"], cfg.tickers)
        print(f"news-poll: {rep['sources']} feeds, {rep['items']} items, "
              f"{rep['new']} new" + (f", {len(rep['errors'])} feeds failed"
                                     if rep["errors"] else ""))
        for e in rep["errors"][:5]:
            print("  " + e)
    finally:
        store.close()
    if not args.no_score:
        return cmd_news_score(cfg, args)
    return 0


def cmd_news_score(cfg: cfgmod.Config, args) -> int:
    from .locks import LockBusy, file_lock
    from .news.score import score_pending
    from .news.store import NewsStore
    _need_news(cfg)
    llm = _llm(cfg)
    if llm is None:
        return 0
    store = NewsStore(_news_path(cfg))
    try:
        with file_lock(cfg.data_dir / "llm.lock"):
            rep = score_pending(store, llm, cfg["news"],
                                model_name=cfg["llm"]["model"])
    except LockBusy as exc:
        print(f"news-score skipped: {exc}")
        return 0
    finally:
        store.close()
    print(f"news-score: {rep['scored']} scored, {rep['unscored']} left for "
          f"later, {rep['capped']} over the per-day cap" +
          (" — LLM unavailable" if rep["llm_failed"] else ""))
    return 0


def cmd_news_status(cfg: cfgmod.Config, args) -> int:
    from .news.sentiment import assign_days
    from .news.store import NewsStore
    _need_news(cfg)
    if not _news_path(cfg).exists():
        print("no headlines yet — run news-poll (warm-up: 2–3 weeks before "
              "the start)")
        return 0
    store = NewsStore(_news_path(cfg))
    try:
        pairs = store.pairs()
        days = assign_days(pairs, cfg["news"]["cutoff"],
                           int(cfg["news"]["max_per_ticker_day"]))
        heads = store.frame("headlines")
    finally:
        store.close()
    print(f"{len(heads)} headlines, {len(pairs)} headline-ticker pairs; "
          f"{(pairs['status'] == 'scored').sum()} scored, "
          f"{(pairs['status'] == 'pending').sum()} pending, "
          f"{(pairs['status'] == 'capped').sum()} capped, "
          f"{(pairs['status'] == 'failed').sum()} failed; "
          f"{heads['published'].isna().sum()} without a usable time")
    if len(days):
        per = days.groupby("day").agg(pairs=("ticker", "size"),
                                      tickers=("ticker", "nunique"),
                                      scored=("status",
                                              lambda x: (x == "scored").sum()))
        print(per.tail(15).to_string())
    return 0


def cmd_experiment_init(cfg: cfgmod.Config, args) -> int:
    from . import experiment as ex
    try:
        e = ex.init(cfg.root / "experiment.yaml", cfg.root, cfg,
                    dt.date.fromisoformat(args.start),
                    dt.date.fromisoformat(args.end), name=args.name,
                    allow_past=args.allow_past)
    except ex.ExperimentError as exc:
        raise SystemExit(str(exc))
    print(f"wrote and froze {cfg.root / 'experiment.yaml'}: {e.name}, "
          f"{e.start} → {e.end} ({e.raw['trading_days']} trading days)")
    return 0


def cmd_compare(cfg: cfgmod.Config, args) -> int:
    from . import experiment as ex
    from .compare.report import CompareError, compare
    path = Path(args.experiment) if args.experiment else \
        cfg.root / "experiment.yaml"
    try:
        exp = ex.load(path)
    except ex.ExperimentError as exc:
        raise SystemExit(str(exc))
    exports = cfg.root / cfg["paper"]["exports_dir"]
    a = Path(args.a) if args.a else exports / "A"
    b = Path(args.b) if args.b else exports / "B"
    out = Path(args.out) if args.out else cfg.root / "reports" / \
        "compare" / exp.name
    try:
        res = compare(a, b, exp, out, exp.changed_files(path.parent))
    except (CompareError, ValueError) as exc:
        raise SystemExit(f"compare failed: {exc}")
    for w in res["warnings"]:
        print("WARNING:", w)
    print(res["verdict"])
    print(res["verdict_returns"])
    print(f"report: {res['out_dir'] / 'compare.md'}")
    return 0


def cmd_replay(cfg: cfgmod.Config, args) -> int:
    from . import experiment as ex
    from . import replay as rp
    from .compare.report import compare
    start, end = (dt.date.fromisoformat(args.start),
                  dt.date.fromisoformat(args.end))
    cfg_a = cfgmod.load("A", root=cfg.root)
    cfg_b = cfgmod.load("B", root=cfg.root)
    prices, table = build_features(cfg_a)
    hashes = PriceStore(cfg.shared_dir / "prices").day_hashes(
        _price_tickers(cfg))
    hashes = dict(zip(hashes["date"], hashes["hash"]))
    proposer, llm = None, None
    if args.proposer == "random":
        proposer = rp.random_proposer(cfg["tuner"]["bounds"], cfg["seed"])
    elif args.proposer == "llm":
        llm = _llm(cfg)
    out = rp.new_folder(cfg.data_dir / "replay", start, end)
    print(f"replay {start} → {end} into {out} (news disabled, proposer: "
          f"{args.proposer})")
    res = rp.run(cfg_a, cfg_b, table, prices, hashes, start, end, out,
                 proposer=proposer, llm=llm,
                 report_llm=_llm(cfg) if args.report else None,
                 write_reports=args.report)
    for node, rows in res["tunes"].items():
        if rows:
            print(f"  {node} tuner: " + ", ".join(f"{d} {s}"
                                                   for d, s in rows))
    e = ex.init(out / "experiment.yaml", cfg.root, cfg, start, end,
                name=f"replay-{out.name}", allow_past=True)
    r = compare(out / "exports" / "A", out / "exports" / "B", e,
                out / "compare")
    for w in r["warnings"]:
        print("WARNING:", w)
    print(r["verdict"])
    same = r["log_loss"]["identical"] and not any(
        "differ" in w for w in r["warnings"])
    print("replay check: " + ("PASS — both nodes identical with news "
                              "disabled" if same else
                              "FAIL — the nodes diverged without news"))
    print(f"report: {r['out_dir'] / 'compare.md'}")
    return 0 if same else 1


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
                                  hashes, sentiment=load_sentiment(cfg))
            for r in results:
                eq = ", ".join(f"{b} {v:,.0f}" for b, v in
                               sorted(r["equity"].items()))
                print(f"  {r['day']}: {eq}")
            if not results:
                print(f"nothing new to process up to {end} "
                      f"(last run {ledger.last_run()})")
            export_node(ledger, cfg.root / cfg["paper"]["exports_dir"] /
                        cfg.node_id, news=_news_for_export(cfg))
        finally:
            ledger.close()
    if args.report:
        return cmd_report(cfg, argparse.Namespace(day=None))
    return 0


def _news_for_export(cfg: cfgmod.Config):
    """(news db path, news settings) on the news node, else None."""
    if cfg.news_enabled and _news_path(cfg).exists():
        return _news_path(cfg), cfg["news"]
    return None


def _llm(cfg: cfgmod.Config):
    """The configured LLM client, or None (with the reason printed)."""
    from .llm.client import LLMError, OpenAIClient
    try:
        return OpenAIClient.from_config(cfg["llm"])
    except LLMError as exc:
        print(f"LLM unavailable: {exc}")
        return None


def _open_ledger(cfg: cfgmod.Config):
    from .paper.ledger import Ledger
    path = cfg.node_dir / "paper.sqlite"
    if not path.exists():
        raise SystemExit(f"no ledger yet at {path} — run daily first")
    return Ledger(path)


def cmd_tune(cfg: cfgmod.Config, args) -> int:
    import json as _json
    from .locks import LockBusy, file_lock
    from .tuner.run import tune
    _need_node(cfg)
    ledger = _open_ledger(cfg)
    try:
        day = dt.date.fromisoformat(args.day) if args.day else \
            ledger.last_run()
        if day is None:
            raise SystemExit("the ledger has no processed day yet")
        proposal = None
        if args.proposal:
            proposal = _json.loads(Path(args.proposal).read_text())
        _, table = build_features(cfg)
        try:
            with file_lock(cfg.data_dir / "llm.lock"):
                row = tune(ledger, cfg.raw, table, day,
                           news=cfg.news_enabled,
                           llm=None if proposal else _llm(cfg),
                           proposal=proposal,
                           sentiment=load_sentiment(cfg))
        except LockBusy as exc:
            print(f"tune skipped: {exc}")
            return 0
        print(f"tune {day} ({row['source']}): {row['status'].upper()}")
        for k in ("errors", "reason", "changes_json"):
            if row.get(k):
                print(f"  {k}: {row[k]}")
        if row.get("ll_current") is not None:
            print(f"  log loss {row['ll_current']:.5f} → "
                  f"{row['ll_proposed']:.5f} (improvement "
                  f"{row['improvement']:+.5f}, needs > {row['margin']} with t >= "
                  f"{cfg['tuner'].get('min_edge_t', 2.0)}; t "
                  f"{row['edge_t']:+.2f})")
        from .export.csv import export_node
        export_node(ledger, cfg.root / cfg["paper"]["exports_dir"] /
                    cfg.node_id, news=_news_for_export(cfg))
    finally:
        ledger.close()
    return 0


def cmd_report(cfg: cfgmod.Config, args) -> int:
    from .locks import LockBusy, file_lock
    from .report.daily import write_report
    _need_node(cfg)
    ledger = _open_ledger(cfg)
    try:
        day = dt.date.fromisoformat(args.day) if args.day else \
            ledger.last_run()
        if day is None:
            raise SystemExit("the ledger has no processed day yet")
        out = cfg.root / "reports" / cfg.node_id
        try:
            with file_lock(cfg.data_dir / "llm.lock"):
                path = write_report(ledger, day, out, _llm(cfg),
                                    words=cfg["report"]["words"])
        except LockBusy:
            path = write_report(ledger, day, out, None,
                                words=cfg["report"]["words"])
    finally:
        ledger.close()
    print(f"report: {path}")
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
                          cfg.node_id, news=_news_for_export(cfg))
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
    p.add_argument("--report", action="store_true",
                   help="write the daily report afterwards")
    sub.add_parser("export")
    p = sub.add_parser("tune")
    p.add_argument("--day", default=None, help="cutoff (default: last run)")
    p.add_argument("--proposal", default=None,
                   help="a JSON proposal file instead of asking the LLM")
    p = sub.add_parser("report")
    p.add_argument("--day", default=None)
    p = sub.add_parser("news-poll")
    p.add_argument("--no-score", action="store_true")
    sub.add_parser("news-score")
    sub.add_parser("news-status")
    p = sub.add_parser("experiment-init")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--name", default=None)
    p.add_argument("--allow-past", action="store_true",
                   help=argparse.SUPPRESS)
    p = sub.add_parser("compare")
    p.add_argument("--experiment", default=None)
    p.add_argument("--a", default=None, help="node A exports folder")
    p.add_argument("--b", default=None, help="node B exports folder")
    p.add_argument("--out", default=None)
    p = sub.add_parser("replay")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--proposer", choices=["random", "llm", "none"],
                   default="random")
    p.add_argument("--report", action="store_true",
                   help="write daily reports (uses the LLM if up)")
    args = ap.parse_args(argv)
    cfg = cfgmod.load(args.node, root=Path(args.root) if args.root
                      else cfgmod.ROOT)
    return {"ingest": cmd_ingest, "build-features": cmd_build_features,
            "backtest": cmd_backtest, "daily": cmd_daily,
            "export": cmd_export, "tune": cmd_tune,
            "report": cmd_report, "news-poll": cmd_news_poll,
            "news-score": cmd_news_score,
            "news-status": cmd_news_status,
            "experiment-init": cmd_experiment_init, "compare": cmd_compare,
            "replay": cmd_replay}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
