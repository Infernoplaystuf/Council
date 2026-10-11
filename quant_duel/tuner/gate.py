"""The validation gate: a proposal is adopted only if a long walk-forward
backtest says it predicts better.

Both settings — current and proposed — are run through the same
walk-forward backtest over the last ``backtest.validation_years`` of
labelled days up to the cutoff (the last completed trading day; the table
is cut with ``as_of`` so nothing later can be seen). Each is retrained on
its own window and refit on its own cadence, exactly as it would trade.
The proposal passes only if its mean per-prediction log loss is lower by
more than ``tuner.min_improvement`` AND that improvement is consistent
across days (paired t-statistic over daily log-loss gains of at least
``tuner.min_edge_t``, default 2). The margin alone is not enough: on prices
with no signal at all a tweak cleared a 0.0005 margin by luck with t = 1.3.
Recent live results play no part.

Note: the threshold only decides long/flat, so it cannot change log loss —
a threshold-only proposal can never pass this gate.
"""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

import pandas as pd

from ..backtest.metrics import prediction_metrics
from ..backtest.run import evaluate, paired_edge, predict
from ..features.build import feature_columns
from ..paper.books import BookConfig
from ..paper.daily import as_of

TRADING_DAYS = 252


@dataclass
class Run:
    """One setting's walk-forward predictions and metrics."""

    pred: pd.DataFrame
    metrics: Dict[str, float]
    start: dt.date
    end: dt.date


@dataclass
class Verdict:
    adopt: bool
    ll_current: float
    ll_proposed: float
    improvement: float
    margin: float
    edge_t: float
    start: str
    end: str
    current: Dict[str, float] = field(default_factory=dict)
    proposed: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _book_cfg(cfg: Dict, book: BookConfig) -> Dict:
    raw = copy.deepcopy(cfg)
    raw["backtest"].update(train_days=book.train_days,
                           test_days=book.refit_days,
                           threshold=book.threshold)
    raw["model"]["params"][book.model_kind] = dict(book.model_params)
    return raw


def walk_forward(table: pd.DataFrame, book: BookConfig, cfg: Dict,
                 seed: int, cutoff: dt.date) -> Run:
    cut = as_of(table, cutoff)
    labelled = sorted(cut.dropna(subset=["target"])["date"].unique())
    years = cfg["backtest"]["validation_years"]
    window = labelled[-int(years * TRADING_DAYS):]
    if len(window) < int(years * TRADING_DAYS) or \
            labelled.index(window[0]) < book.train_days // 2:
        raise ValueError(f"not enough history for a {years}-year validation "
                         f"ending {cutoff}")
    i = labelled.index(window[0])
    start_after = labelled[i - 1]
    raw = _book_cfg(cfg, book)
    feats = book.features(feature_columns(cut))
    pred = predict(cut, book.model_kind, raw, seed, features=feats,
                   start_after=start_after)
    return Run(pred, evaluate(pred, raw), min(pred["date"]),
               max(pred["date"]))


def gate(table: pd.DataFrame, current: BookConfig, proposed: BookConfig,
         cfg: Dict, seed: int, cutoff: dt.date,
         baseline: Optional[Run] = None) -> Verdict:
    base = baseline or walk_forward(table, current, cfg, seed, cutoff)
    prop = walk_forward(table, proposed, cfg, seed, cutoff)
    # Compare on the rows both predicted (all of them, unless one setting's
    # first fold starts later for lack of history).
    key = ["date", "ticker"]
    common = base.pred[key].merge(prop.pred[key], on=key)
    b = base.pred.merge(common, on=key)
    p = prop.pred.merge(common, on=key)
    edge = paired_edge(p, b)
    margin = float(cfg["tuner"]["min_improvement"])
    min_t = float(cfg["tuner"].get("min_edge_t", 2.0))
    ll_b = prediction_metrics(b["p"].to_numpy(),
                              b["target"].to_numpy())["log_loss"]
    ll_p = prediction_metrics(p["p"].to_numpy(),
                              p["target"].to_numpy())["log_loss"]
    improvement = ll_b - ll_p
    adopt = improvement > margin and edge["ll_edge_t"] >= min_t
    return Verdict(adopt=bool(adopt), ll_current=ll_b,
                   ll_proposed=ll_p, improvement=float(improvement),
                   margin=margin, edge_t=float(edge["ll_edge_t"]),
                   start=str(base.start), end=str(base.end),
                   current=dict(base.metrics), proposed=dict(prop.metrics))
