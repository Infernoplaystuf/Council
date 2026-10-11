"""``replay``: the full daily / weekly-tune loop over a past window, both
nodes simulated on one machine, news disabled — to shake out bugs before
going live.

Each trading day both nodes run the daily step (from data cut at that
day); after the last trading day of each ISO week both run a tuning round
with that day as the cutoff (as the Saturday job would live). Proposals
come from:

* ``random`` (default) — a seeded generator of valid proposals, the SAME
  for both nodes, which exercises the schema, the gate and adoption
  without an LLM;
* ``llm`` — the configured local LLM;
* ``none`` — no tuning.

With news disabled the two nodes must end byte-identical, so the replay's
own ``compare`` report doubles as a check of the whole pipeline: any
difference between A and B is a bug.

Everything goes to a NEW folder ``data/replay/<start>_<end>[-n]/`` (its own
ledgers, exports, reports, experiment.yaml); nothing live is touched.
"""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

import numpy as np
import pandas as pd

from .export.csv import export_node
from .paper.books import GROUPS, BookConfig
from .paper.daily import step
from .paper.ledger import Ledger
from .report.daily import write_report
from .tuner.run import tune

Proposer = Callable[[str, dt.date, BookConfig], Optional[Dict[str, Any]]]


def random_proposer(bounds: Mapping[str, Any], seed: int) -> Proposer:
    """Valid, seeded proposals: one or two settings moved within bounds.
    Depends only on (seed, day, current settings) — never on the node."""
    def propose(node: str, day: dt.date, current: BookConfig):
        rng = np.random.default_rng([seed, day.toordinal()])
        changes: Dict[str, Any] = {}
        options = ["model_params", "train_days", "features_enabled"]
        for what in rng.choice(options, size=rng.integers(1, 3),
                               replace=False):
            if what == "model_params":
                kb = bounds.get(current.model_kind, {})
                if not kb:
                    continue
                name = sorted(kb)[rng.integers(len(kb))]
                lo, hi = kb[name]
                if name in ("C", "learning_rate") and lo > 0:
                    v = float(math.exp(rng.uniform(math.log(lo),
                                                   math.log(hi))))
                    v = float(f"{v:.3g}")
                elif isinstance(lo, int) and isinstance(hi, int):
                    v = int(rng.integers(lo, hi + 1))
                else:
                    v = round(float(rng.uniform(lo, hi)), 3)
                if v != current.model_params.get(name):
                    changes["model_params"] = {name: v}
            elif what == "train_days":
                lo, hi = bounds["train_days"]
                v = int(rng.integers(-(-lo // 21), hi // 21 + 1) * 21)
                if v != current.train_days:
                    changes["train_days"] = v
            else:
                g = [g for g, _ in GROUPS][rng.integers(len(GROUPS))]
                changes["features_enabled"] = {
                    g: not current.features_enabled.get(g, True)}
        if not changes:                  # every draw matched the current
            g = [g for g, _ in GROUPS][rng.integers(len(GROUPS))]
            changes["features_enabled"] = {
                g: not current.features_enabled.get(g, True)}
        return {"changes": changes, "reason": "replay: random probe"}
    return propose


def new_folder(base: Path, start: dt.date, end: dt.date) -> Path:
    name = f"{start}_{end}"
    path = Path(base) / name
    k = 2
    while path.exists():
        path = Path(base) / f"{name}-{k}"
        k += 1
    path.mkdir(parents=True)
    return path


def run(cfg_a, cfg_b, table: pd.DataFrame, prices: Mapping[str, pd.DataFrame],
        hashes: Mapping[dt.date, str], start: dt.date, end: dt.date,
        out: Path, proposer: Optional[Proposer] = None, llm=None,
        report_llm=None, write_reports: bool = False) -> Dict[str, Any]:
    """Replay [start, end] for both nodes into ``out``."""
    days = sorted(d for d in table["date"].unique() if start <= d <= end)
    if not days:
        raise ValueError(f"no trading days with data in {start}..{end}")
    ledgers = {}
    for cfg in (cfg_a, cfg_b):
        led = Ledger(out / cfg.node_id / "paper.sqlite")
        led.init(cfg.node_id, float(cfg["paper"]["start_cash"]),
                 BookConfig.from_config(cfg), cfg["universe"]["benchmark"])
        ledgers[cfg.node_id] = (led, cfg)
    caches: Dict[str, Dict] = {n: {} for n in ledgers}
    tunes = {n: [] for n in ledgers}
    try:
        for i, d in enumerate(days):
            week_end = i == len(days) - 1 or \
                days[i + 1].isocalendar()[:2] != d.isocalendar()[:2]
            for node, (led, cfg) in ledgers.items():
                step(led, cfg.raw, table, prices, d, hashes.get(d),
                     caches[node])
                if week_end and (proposer is not None or llm is not None):
                    prop = proposer(node, d, led.book_config("live")) \
                        if proposer else None
                    row = tune(led, cfg.raw, table, d, news=False,
                               llm=llm, proposal=prop)
                    tunes[node].append((d, row["status"]))
                if write_reports:
                    write_report(led, d, out / "reports" / node, report_llm,
                                 words=cfg["report"]["words"])
        for node, (led, cfg) in ledgers.items():
            export_node(led, out / "exports" / node)
    finally:
        for led, _ in ledgers.values():
            led.close()
    return {"out": out, "days": len(days), "tunes": tunes}
