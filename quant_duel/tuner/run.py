"""One tuning round for one node, every outcome logged to ``tuner_log``:

``skipped``    a change was already adopted this ISO week (max one/week)
``llm_failed`` the LLM was down, slow, or never produced usable JSON
``no_change``  the LLM proposed nothing
``invalid``    the proposal broke the schema or bounds (reasons logged)
``rejected``   valid, but the validation gate said no
``adopted``    valid and better by more than the margin → live settings
               updated (the next ``daily`` fits a model with them)
``error``      something else went wrong (e.g. too little history)

Only the live book can change; the control book is frozen by the ledger.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional

import pandas as pd

from ..backtest import metrics as mx
from ..llm.client import ChatModel, LLMError, chat_json
from ..paper.books import BookConfig, canonical
from ..paper.ledger import Ledger
from . import gate as gatemod
from .schema import ProposalError, check, describe

SYSTEM = (
    "You tune a paper-trading stock model that predicts whether each stock "
    "closes up tomorrow. Reply with ONE JSON object and nothing else: "
    '{{"changes": {{...}}, "reason": "one sentence"}}. "changes" may only '
    'use '
    "the keys in SCHEMA, within their min/max. A change is adopted only if "
    "a {years}-year walk-forward backtest's mean log loss improves by more "
    "than {margin}, consistently across days; recent live results alone never justify a change, and "
    "the threshold cannot change log loss. Change at most {max_changes} "
    'settings. If nothing seems worth testing, reply {{"changes": {{}}, '
    '"reason": "..."}}.')


def _round(d: Dict[str, Any], nd: int = 4) -> Dict[str, Any]:
    """Floats rounded; NaN/inf become null (JSON has no NaN)."""
    import math
    return {k: ((round(v, nd) if math.isfinite(v) else None)
                if isinstance(v, float) else v)
            for k, v in d.items()}


def live_summary(ledger: Ledger, days: int = 20) -> Dict[str, Any]:
    """Recent live vs control prediction quality and returns."""
    eq = ledger.frame("equity")
    if eq.empty:
        return {"days": 0}
    recent = sorted(eq["date"].unique())[-days:]
    p = ledger.frame("predictions").dropna(subset=["target"])
    p = p[p["date"].isin(recent)]
    out: Dict[str, Any] = {"days": len(recent)}
    for book in ("live", "control"):
        b = p[p["book"] == book]
        if len(b):
            m = mx.prediction_metrics(b["p"].to_numpy(), b["target"].to_numpy())
            out[book] = _round({k: m[k] for k in ("n", "accuracy",
                                                   "log_loss")})
    for book in ("live", "control", "spy"):
        e = eq[(eq["book"] == book) & eq["date"].isin(recent)]["equity"]
        before = eq[(eq["book"] == book) & (eq["date"] < recent[0])]["equity"]
        start = before.iloc[-1] if len(before) else \
            ledger.frame("books").set_index("book").loc[book, "start_cash"]
        if len(e):
            out.setdefault("return", {})[book] = round(e.iloc[-1] / start - 1, 4)
    return out


def history(ledger: Ledger, n: int = 3) -> List[Dict[str, Any]]:
    log = ledger.frame("tuner_log")
    rows = []
    for r in log.tail(n).itertuples():
        rows.append({"day": r.day, "status": r.status,
                     "changes": json.loads(r.changes_json)
                     if r.changes_json else None,
                     "improvement": None if pd.isna(r.improvement)
                     else round(r.improvement, 5)})
    return rows


def sentiment_summary(sent: Optional[pd.DataFrame],
                      day: dt.date) -> Dict[str, Any]:
    if sent is None or sent.empty:
        return {"days": 0}
    s = sent[sent["date"] <= day]
    return {"days": int(s["date"].nunique()),
            "ticker_days": int(len(s)),
            "mean_abs": round(float(s["sentiment"].abs().mean()), 3)
            if len(s) else 0.0}


def prompt(cfg: Dict, current: BookConfig, news: bool, live: Dict,
           backtest: Dict[str, float], past: List[Dict],
           sentiment: Optional[Dict[str, Any]] = None
           ) -> List[Dict[str, str]]:
    t = cfg["tuner"]
    system = SYSTEM.format(years=cfg["backtest"]["validation_years"],
                           margin=t["min_improvement"],
                           max_changes=t["max_changes"])
    shown = {k: v for k, v in current.to_dict().items()
             if k not in ("extra", "refit_days")
             and (news or k != "sentiment_weight")}
    body = {
        "CURRENT": shown,
        "SCHEMA": describe(current, t["bounds"], news),
        "BACKTEST_OF_CURRENT": _round({k: backtest[k] for k in (
            "log_loss", "accuracy", "auc", "sharpe", "cum_return")
            if k in backtest}),
        "LIVE_RECENT": live,
        "RECENT_PROPOSALS": past,
    }
    if news and sentiment is not None:
        body["SENTIMENT_HISTORY"] = sentiment
    user = canonical(body)
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def tune(ledger: Ledger, cfg: Dict, table: pd.DataFrame, day: dt.date, *,
         news: bool, llm: Optional[ChatModel] = None,
         proposal: Optional[Dict[str, Any]] = None,
         sentiment: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """Run one round; returns the logged row (plus ``id``). Either ``llm``
    asks the model for a proposal, or ``proposal`` is given directly.
    ``sentiment`` (news node only) feeds the overlay in the gate."""
    if not news:
        sentiment = None
    current = ledger.book_config("live")
    t = cfg["tuner"]
    row: Dict[str, Any] = {"day": day.isoformat(),
                           "source": "llm" if proposal is None else "manual",
                           "current_json": current.to_json()}

    def log(**kw) -> Dict[str, Any]:
        row.update(kw)
        with ledger.transaction():
            row["id"] = ledger.log_tuning({k: v for k, v in row.items()
                                           if k != "id"})
            if row["status"] == "adopted":
                ledger.set_book_config(
                    "live", BookConfig.from_dict(json.loads(
                        row["proposed_json"])))
                ledger.event(day, "live", "tuned", row["changes_json"])
        return row

    week = ledger.adopted_in_week(day)
    if week:
        return log(status="skipped",
                   errors=f"a change was already adopted this week ({week})")
    try:
        baseline = gatemod.walk_forward(table, current, cfg, cfg["seed"], day,
                                        sentiment)
    except ValueError as exc:
        return log(status="error", errors=str(exc))
    if proposal is None:
        if llm is None:
            return log(status="llm_failed", errors="no LLM configured")
        messages = prompt(cfg, current, news, live_summary(ledger),
                          baseline.metrics, history(ledger),
                          sentiment_summary(sentiment, day))
        try:
            proposal, raw = chat_json(llm, messages,
                                      max_tokens=t.get("max_tokens", 300))
        except LLMError as exc:
            return log(status="llm_failed", errors=str(exc))
        row["raw_reply"] = raw[:4000]
    if isinstance(proposal, dict) and proposal.get("changes") == {}:
        return log(status="no_change", reason=str(proposal.get("reason", ""))[:500])
    try:
        checked = check(proposal, current, t["bounds"], news,
                        max_changes=t["max_changes"])
    except ProposalError as exc:
        return log(status="invalid", errors="; ".join(exc.errors),
                   changes_json=json.dumps(proposal, default=str)[:4000])
    row.update(reason=checked.reason,
               changes_json=canonical({k: list(v) for k, v in
                                       checked.changed.items()}),
               proposed_json=checked.book.to_json())
    try:
        v = gatemod.gate(table, current, checked.book, cfg, cfg["seed"], day,
                         baseline=baseline, sentiment=sentiment)
    except ValueError as exc:
        return log(status="error", errors=str(exc))
    return log(status="adopted" if v.adopt else "rejected",
               ll_current=v.ll_current, ll_proposed=v.ll_proposed,
               improvement=v.improvement, margin=v.margin, edge_t=v.edge_t,
               detail_json=canonical(_round({
                   "start": v.start, "end": v.end,
                   **{f"current_{k}": x for k, x in v.current.items()},
                   **{f"proposed_{k}": x for k, x in v.proposed.items()}},
                   6)))
