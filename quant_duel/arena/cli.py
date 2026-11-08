"""The arena's command line.

    python -m quant_duel.arena run [--now ISO] [--no-ingest] [--no-news]
                                   [--only NAME] [--source synthetic]
    python -m quant_duel.arena report [--day YYYY-MM-DD]
    python -m quant_duel.arena status

``run`` is the daily job. In order: fetch prices to the last close and
poll headlines; fill every book's due orders at their open and mark every
close; then, on a trading day before the open, ask each model for today's
order. A run on a Sunday also writes the weekly leaderboard, and the first
run after the last day writes the final one. Running twice is harmless: a
day already decided is not asked again.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

import yaml

from .. import config as cfgmod
from ..cli import last_completed_day
from ..ingest.sources import make_source
from ..ingest.store import PriceStore, ingest
from ..locks import LockBusy, file_lock
from ..market_calendar import (is_trading_day, next_trading_day,
                               previous_trading_day, trading_days)
from ..news.poll import poll
from ..news.store import NewsStore
from . import book as bk
from . import market, report, trader

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
BENCH = "Benchmark"
OPEN = (9, 30)


class Arena:
    def __init__(self, root: Path = cfgmod.ROOT):
        self.root = Path(root)
        self.cfg: Dict[str, Any] = yaml.safe_load(
            (self.root / "arena.yaml").read_text(encoding="utf-8"))
        self.qcfg = cfgmod.load(None, root=self.root)
        self.dir = self.root / self.cfg["dir"]
        self.reports = self.root / self.cfg["reports"]
        self.start = dt.date.fromisoformat(str(self.cfg["start"]))
        self.end = dt.date.fromisoformat(str(self.cfg["end"]))
        self.tickers: List[str] = self.qcfg.tickers
        self.store = PriceStore(self.qcfg.shared_dir / "prices")

    # -- books -----------------------------------------------------------
    def book_path(self, name: str) -> Path:
        return self.dir / "books" / f"{name}.json"

    def books(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        bench = self.cfg.get("benchmark", "SPY")
        entries = [(c["name"], c["model"]) for c in self.cfg["contestants"]]
        entries.append((BENCH, f"{bench} buy and hold"))
        for name, model in entries:
            b = bk.load(self.book_path(name)) or bk.new_book(
                name, model, float(self.cfg["start_cash"]))
            b["model"] = model
            out[name] = b
        return out

    def prices(self):
        return self.store.load_all(self.tickers)

    def news(self) -> NewsStore:
        return NewsStore(self.dir / "news.sqlite")


def _have_day(prices, bench: str):
    idx = set(prices.get(bench).index) if bench in prices else set()
    return lambda d: d in idx


def _holdings_text(b, prices, day) -> str:
    eq = bk.equity(b, prices, day)
    rows = []
    for t, n in sorted(b["shares"].items()):
        p = bk.price_on(prices, t, day, "close") or 0
        rows.append(f"  {t}: {n:.4f} shares, ${n * p:,.2f} "
                    f"({n * p / eq * 100:.1f}%)")
    return "\n".join(rows) or "  (none: all cash)"


def _board_text(books, day) -> str:
    rows = []
    for b in books.values():
        m = b["marks"]
        last = [v for k, v in m.items() if k <= str(day)]
        eq = last[-1] if last else b["start_cash"]
        rows.append((eq, b["name"]))
    rows.sort(reverse=True)
    return "\n".join(f"  {i}. {n}: ${e:,.2f}" for i, (e, n) in
                     enumerate(rows, 1))


def plan(now_ny: dt.datetime) -> Optional[dt.date]:
    """The day this run's orders fill at the open of, or None."""
    today = now_ny.date()
    if is_trading_day(today) and (now_ny.hour, now_ny.minute) < OPEN:
        return today
    return None


def cmd_run(arena: Arena, args) -> int:
    now_ny = (dt.datetime.fromisoformat(args.now).replace(tzinfo=NY)
              if args.now else dt.datetime.now(NY))
    now_utc = now_ny.astimezone(UTC)
    close_day = last_completed_day(now_ny)
    cfg = arena.cfg
    bench = cfg.get("benchmark", "SPY")
    log_dir = arena.dir / "logs" / now_ny.strftime("%Y-%m-%d")
    log_dir.mkdir(parents=True, exist_ok=True)
    say = lambda m: print(f"[{dt.datetime.now():%H:%M:%S}] {m}", flush=True)
    try:
        with file_lock(arena.dir / "run.lock"):
            return _run(arena, args, now_ny, now_utc, close_day, bench,
                        log_dir, say)
    except LockBusy:
        say("another arena run is in progress; nothing done")
        return 0


def _run(arena, args, now_ny, now_utc, close_day, bench, log_dir, say):
    cfg = arena.cfg
    if not args.no_ingest:
        src = make_source(args.source or arena.qcfg["data"]["source"],
                          arena.qcfg["seed"])
        start = dt.date.fromisoformat(arena.qcfg["data"]["history_start"])
        reps = ingest(arena.store, src, arena.tickers, start, close_day)
        bad = [r["ticker"] for r in reps if "error" in r or not r["rows"]]
        say(f"prices to {close_day}: {len(reps) - len(bad)}/{len(reps)} "
            f"tickers" + (f" (failed: {', '.join(bad)})" if bad else ""))
    news = arena.news()
    try:
        if not args.no_news:
            rep = poll(news, arena.qcfg["news"], arena.tickers)
            say(f"headlines: {rep['new']} new of {rep['items']} "
                f"({len(rep['errors'])} feeds failed)")
        prices = arena.prices()
        have = _have_day(prices, bench)
        books = arena.books()
        cost = float(cfg["cost_bps"])

        # Settle: fill due orders at their open, mark every close.
        last = min(close_day, arena.end)
        for b in books.values():
            bk.advance(b, prices, arena.start, last, have, cost)
            bk.save(arena.book_path(b["name"]), b)

        fill_day = plan(now_ny)
        if args.fill_day:
            fill_day = dt.date.fromisoformat(args.fill_day)
        if fill_day is None:
            say("no market open ahead today: no orders")
        elif not (arena.start <= fill_day <= arena.end):
            say(f"{fill_day} is outside the duel ({arena.start} to "
                f"{arena.end}): no orders")
        else:
            data_day = previous_trading_day(fill_day)
            if not have(data_day):
                say(f"no prices for {data_day} yet: no orders today "
                    "(books hold)")
            else:
                _decide(arena, books, prices, news, data_day, fill_day,
                        now_ny, now_utc, log_dir, args.only, say)

        today = now_ny.date()
        final = close_day >= arena.end and have(arena.end)
        if args.report or today.weekday() == 6 or final:
            out = report.write(arena, books, min(close_day, arena.end),
                               final=final)
            say(f"leaderboard written: {out}")
        _status(arena, books, close_day)
        return 0
    finally:
        news.close()


def _decide(arena, books, prices, news, data_day, fill_day, now_ny, now_utc,
            log_dir, only, say):
    cfg = arena.cfg
    bench = cfg.get("benchmark", "SPY")
    b = books[BENCH]
    if not b["orders"]:
        b["orders"].append({"fill_day": str(fill_day),
                            "decided": now_utc.isoformat(timespec="seconds"),
                            "close_day": str(data_day), "action": "trade",
                            "weights": {bench: 100.0}, "status": "pending",
                            "reason": "buy and hold"})
        bk.save(arena.book_path(BENCH), b)
    days_left = len(trading_days(fill_day, arena.end))
    since = (dt.datetime.combine(previous_trading_day(data_day),
                                 dt.time(16, 0), NY).astimezone(UTC))
    since = max(since, now_utc - dt.timedelta(hours=cfg["headline_hours"]))
    items = market.headlines(news, since, now_utc,
                             per_ticker=cfg["headlines_per_ticker"],
                             limit=cfg["max_headlines"])
    table = market.price_table(prices, arena.tickers, data_day)
    board = _board_text(books, data_day)
    for c in cfg["contestants"]:
        name, model = c["name"], c["model"]
        if only and name.lower() not in [o.lower() for o in only]:
            continue
        b = books[name]
        if any(o["fill_day"] == str(fill_day) for o in b["orders"]):
            say(f"{name}: already decided for {fill_day}")
            continue
        eq = bk.equity(b, prices, data_day)
        packet = trader.PACKET.format(
            today=now_ny.strftime("%A %Y-%m-%d %H:%M"), close_day=data_day,
            fill_day=fill_day, days_left=days_left, end=arena.end,
            equity=eq, total_pct=(eq / b["start_cash"] - 1) * 100,
            cash=b["cash"], holdings=_holdings_text(b, prices, data_day),
            board=board, table=table, news=market.headline_text(items))
        system = trader.system_prompt(
            name, cfg, arena.tickers,
            int(c.get("max_requests", cfg["max_requests"])))
        url = cfg["ollama"]

        def chat(messages, _m=model, _c=c):
            return trader.ollama_chat(
                url, _m, messages, temperature=float(cfg["temperature"]),
                num_ctx=int(_c.get("num_ctx", cfg["num_ctx"])),
                max_tokens=int(_c.get("reply_tokens", cfg["reply_tokens"])),
                timeout_s=float(cfg["timeout_s"]),
                json_format=bool(_c.get("json_format", True)),
                think=_c.get("think"))

        def answer(req):
            return market.answer(req, prices, arena.tickers, data_day, news,
                                 now_utc)

        t0 = dt.datetime.now()
        decision, transcript = trader.morning(
            chat, system, packet, answer,
            max_requests=int(c.get("max_requests", cfg["max_requests"])),
            max_rounds=int(cfg["max_rounds"]))
        secs = (dt.datetime.now() - t0).total_seconds()
        trader.unload(url, model)
        order = {"fill_day": str(fill_day), "close_day": str(data_day),
                 "decided": now_utc.isoformat(timespec="seconds"),
                 "action": decision["action"],
                 "reason": str(decision.get("reason", ""))[:600],
                 "requests": decision.get("requests", []),
                 "seconds": round(secs, 1), "status": "pending"}
        if order["action"] == "trade":
            order["weights"] = bk.clean_weights(decision.get("holdings"),
                                                arena.tickers)
            if not order["weights"] and decision.get("holdings") is None:
                order["action"] = "error"
                order["reason"] = "trade with no holdings"
        if order["action"] == "error":
            order["status"] = "failed"
        b["orders"].append(order)
        bk.save(arena.book_path(name), b)
        (log_dir / f"{name}.json").write_text(json.dumps(
            {"order": order, "decision": decision, "transcript": transcript},
            indent=1, default=str), encoding="utf-8")
        what = order["action"]
        if what == "trade":
            what += " " + ", ".join(f"{t} {w:.0f}%" for t, w in sorted(
                order["weights"].items(), key=lambda kv: -kv[1])[:6])
        say(f"{name} ({model}): {what} [{len(order['requests'])} requests, "
            f"{secs:.0f}s]")


def _status(arena, books, close_day) -> Dict[str, Any]:
    week_ago = close_day - dt.timedelta(days=7)
    st = {"as_of": str(close_day), "start": str(arena.start),
          "end": str(arena.end),
          "books": [bk.summary(b, week_ago) for b in books.values()]}
    st["books"].sort(key=lambda s: -s["equity"])
    (arena.dir / "status.json").write_text(json.dumps(st, indent=1),
                                           encoding="utf-8")
    return st


def cmd_report(arena: Arena, args) -> int:
    day = (dt.date.fromisoformat(args.day) if args.day
           else last_completed_day())
    books = arena.books()
    final = day >= arena.end
    print(report.write(arena, books, min(day, arena.end), final=final))
    return 0


def cmd_status(arena: Arena, args) -> int:
    books = arena.books()
    st = _status(arena, books, last_completed_day())
    for i, s in enumerate(st["books"], 1):
        print(f"{i}. {s['name']:<10} ${s['equity']:>10,.2f} "
              f"{s['total_pct']:+6.2f}%  trades {s['trades']}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_duel.arena")
    ap.add_argument("--root", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--now", default=None, help="pretend time, New York")
    p.add_argument("--fill-day", default=None, help=argparse.SUPPRESS)
    p.add_argument("--no-ingest", action="store_true")
    p.add_argument("--no-news", action="store_true")
    p.add_argument("--source", default=None)
    p.add_argument("--only", nargs="*", default=None)
    p.add_argument("--report", action="store_true")
    p = sub.add_parser("report")
    p.add_argument("--day", default=None)
    sub.add_parser("status")
    args = ap.parse_args(argv)
    arena = Arena(Path(args.root) if args.root else cfgmod.ROOT)
    return {"run": cmd_run, "report": cmd_report,
            "status": cmd_status}[args.cmd](arena, args)


if __name__ == "__main__":
    sys.exit(main())
