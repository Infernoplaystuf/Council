"""``check``: the pre-flight list for a node, as code. Run it on each
machine before the warm-up and again before the start; every line is
PASS, WARN or FAIL with what to do about it.
"""
from __future__ import annotations

import datetime as dt
import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

Line = Tuple[str, str, str]           # (status, item, detail)


def _ok(item, detail=""):
    return ("PASS", item, detail)


def _warn(item, detail):
    return ("WARN", item, detail)


def _fail(item, detail):
    return ("FAIL", item, detail)


def run_checks(cfg, *, network: bool = True,
               ntp: Optional[Callable[[], Optional[bool]]] = None
               ) -> List[Line]:
    from . import config as cfgmod
    from . import experiment as ex
    from .models.base import resolve_backend
    out: List[Line] = []
    v = sys.version_info
    out.append(_ok("python", f"{v.major}.{v.minor}") if v >= (3, 11) else
               _fail("python", f"{v.major}.{v.minor}; 3.11+ needed"))
    for mod in ("pandas", "numpy", "sklearn", "scipy", "pyarrow", "yaml"):
        try:
            importlib.import_module(mod)
            out.append(_ok(f"package {mod}"))
        except ImportError:
            out.append(_fail(f"package {mod}",
                             "missing — pip install -r requirements.txt"))
    try:
        importlib.import_module("yfinance")
        out.append(_ok("package yfinance"))
    except ImportError:
        out.append(_warn("package yfinance", "missing — prices cannot be "
                         "fetched (pip install yfinance)"))
    backend = resolve_backend(cfg["model"]["params"].get("boosting", {})
                              .get("backend", "auto"))
    out.append(_ok("boosting backend", backend + " — must be the same on "
                   "both nodes"))
    try:
        other = "B" if cfg.node_id == "A" else "A"
        cfgmod.check_identical(cfg, cfgmod.load(other, root=cfg.root))
        out.append(_ok("node files", f"{cfg.node_id} news="
                       f"{cfg.news_enabled}; shared settings identical"))
    except Exception as exc:                              # noqa: BLE001
        out.append(_fail("node files", str(exc)))
    for d in (cfg.data_dir, cfg.root / "logs", cfg.root / "reports"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_probe"
            probe.write_text("x")
            probe.unlink()
            out.append(_ok(f"writable {d.name}/"))
        except OSError as exc:
            out.append(_fail(f"writable {d.name}/", str(exc)))
    free = shutil.disk_usage(cfg.root).free / 1e9
    out.append(_ok("disk", f"{free:.1f} GB free") if free > 2 else
               _warn("disk", f"only {free:.1f} GB free"))
    synced = (ntp or _ntp_synced)()
    out.append(_ok("clock", "NTP synchronised") if synced else
               _warn("clock", "NTP sync not confirmed (timedatectl) — "
                     "both machines must agree on the time")
               if synced is None else _fail("clock", "NTP not synchronised"))
    prices = cfg.shared_dir / "prices"
    n = len(list(prices.glob("*.parquet"))) if prices.exists() else 0
    out.append(_ok("prices", f"{n} tickers cached") if n >= len(cfg.tickers)
               else _warn("prices", f"{n}/{len(cfg.tickers)} tickers cached "
                          "— run ingest"))
    llm = cfg["llm"]
    from .llm.server import is_up
    server = (llm.get("server") or {}).get("command")
    if network and is_up(llm["url"]):
        out.append(_ok("LLM", f"answering at {llm['url']}"))
    elif server:
        exe = Path(str(server[0])).expanduser()
        out.append(_ok("LLM", f"started on demand ({exe.name})")
                   if exe.exists() or "/" not in str(server[0]) else
                   _fail("LLM", f"server binary not found: {exe}"))
    else:
        out.append(_warn("LLM", f"nothing at {llm['url']} and no "
                         "llm.server.command — tuner, scoring and report "
                         "summaries will skip"))
    mf = llm.get("model_file")
    if mf:
        p = Path(str(mf)).expanduser()
        out.append(_ok("model file", f"{p.name} sha256 "
                       f"{ex.file_hash(p)} — compare on both nodes")
                   if p.exists() else _fail("model file", f"missing: {p}"))
    exp_path = cfg.root / "experiment.yaml"
    if exp_path.exists():
        e = ex.load(exp_path)
        changed = e.changed_files(cfg.root)
        today = dt.date.today()
        state = "running" if e.start <= today <= e.end else \
            ("not started" if today < e.start else "finished")
        out.append(_ok("experiment", f"{e.name} {e.start}→{e.end} "
                       f"({state})") if not changed else
                   _fail("experiment", "settings changed since frozen: " +
                         ", ".join(changed)))
    else:
        out.append(_warn("experiment", "no experiment.yaml yet — write it "
                         "with experiment-init before the start (daily "
                         "then trades only inside the window)"))
    if cfg.news_enabled:
        from .news.feeds import FeedError, fetch, parse
        url = (cfg["news"].get("ticker_feed") or "").format(
            ticker=cfg.tickers[0])
        if network and url:
            try:
                k = len(parse(fetch(url, 15)))
                out.append(_ok("news feed", f"{k} items from {cfg.tickers[0]}"
                               "'s feed"))
            except FeedError as exc:
                out.append(_warn("news feed", str(exc)[:160]))
    return out


def _ntp_synced() -> Optional[bool]:
    try:
        p = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized",
                            "--value"], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    v = p.stdout.strip().lower()
    return True if v == "yes" else False if v == "no" else None
