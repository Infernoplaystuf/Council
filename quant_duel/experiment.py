"""``experiment.yaml``: the protocol, written and frozen BEFORE the start.

It fixes the window (about 21 trading days), the universe, the primary
metric (mean per-prediction log loss of each node's live strategy) and the
secondary metrics, the bootstrap settings, and hashes of ``config.yaml``
and both node files — so ``compare`` can say if anything changed after the
fact. ``init`` never overwrites an existing file.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .market_calendar import trading_days

PRIMARY = "live_log_loss"
SECONDARY = ["accuracy", "brier", "auc", "cum_return", "sharpe",
             "max_drawdown"]


class ExperimentError(ValueError):
    pass


def file_hash(path: Path) -> Optional[str]:
    path = Path(path)
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


@dataclass(frozen=True)
class Experiment:
    raw: Dict[str, Any]

    @property
    def start(self) -> dt.date:
        return dt.date.fromisoformat(str(self.raw["start"]))

    @property
    def end(self) -> dt.date:
        return dt.date.fromisoformat(str(self.raw["end"]))

    @property
    def name(self) -> str:
        return str(self.raw.get("name", "experiment"))

    @property
    def bootstrap(self) -> Dict[str, Any]:
        return dict(self.raw.get("bootstrap") or {})

    def changed_files(self, root: Path) -> List[str]:
        """Settings files whose hash differs from when it was frozen."""
        out = []
        frozen = self.raw.get("hashes") or {}
        for rel, h in frozen.items():
            if file_hash(Path(root) / rel) != h:
                out.append(rel)
        return out


def load(path: Path) -> Experiment:
    path = Path(path)
    if not path.exists():
        raise ExperimentError(f"no experiment file at {path} — write one "
                              "with experiment-init before the start")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for k in ("start", "end"):
        if k not in raw:
            raise ExperimentError(f"{path} has no {k!r}")
    exp = Experiment(raw)
    if exp.end < exp.start:
        raise ExperimentError("end is before start")
    return exp


def init(path: Path, root: Path, cfg, start: dt.date, end: dt.date,
         name: Optional[str] = None, allow_past: bool = False,
         today: Optional[dt.date] = None) -> Experiment:
    path = Path(path)
    if path.exists():
        raise ExperimentError(f"{path} already exists and is frozen; it is "
                              "never overwritten")
    today = today or dt.date.today()
    if start <= today and not allow_past:
        raise ExperimentError(f"start {start} is not in the future; the "
                              "protocol must be frozen before the start")
    days = trading_days(start, end)
    if not days:
        raise ExperimentError("no trading days in the window")
    if days[0] != start:
        raise ExperimentError(f"{start} is not a trading day")
    raw = {
        "name": name or f"quant-duel-{start}",
        "start": start.isoformat(), "end": end.isoformat(),
        "trading_days": len(days),
        "universe": list(cfg.tickers),
        "primary_metric": {
            "name": PRIMARY,
            "definition": "mean per-prediction log loss of each node's live "
                          "strategy over the window; A vs B paired on the "
                          "same (date, ticker) predictions"},
        "secondary_metrics": SECONDARY + ["vs control", "vs SPY"],
        "bootstrap": {"resamples": 10000, "confidence": 0.95,
                      "seed": int(cfg["seed"]), "unit": "trading day"},
        "hashes": {rel: file_hash(Path(root) / rel) for rel in
                   ("config.yaml", "nodes/A.yaml", "nodes/B.yaml")},
        "frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Frozen experiment protocol — do not edit after the "
                   "start.\n" + yaml.safe_dump(raw, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(path)
    return Experiment(raw)
