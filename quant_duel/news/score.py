"""Score pending (headline, ticker) pairs with the local LLM in small
batches, strict JSON in and out.

Only pairs that will be used get scored: per ticker-day, the
``max_per_ticker_day`` most recent (the rest are marked ``capped``, kept,
and never cost LLM time). A batch's reply must be
``{"scores": [{"id": 0, "s": 0.4}, ...]}`` with every s in [-1, 1]; an item
missing or malformed in the reply stays pending for another attempt (up to
``max_score_attempts``, then ``failed``). If the LLM is down the batch is
simply left for next time.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, List

from ..llm.client import ChatModel, LLMError, chat_json
from .sentiment import assign_days
from .store import NewsStore

SYSTEM = (
    "You rate how good or bad each headline is for the named stock's price "
    "over the next day, from -1 (very bad) to 1 (very good); 0 if neutral or "
    "unrelated. Reply with ONE JSON object only: "
    '{"scores": [{"id": <id>, "s": <number>}, ...]} with one entry per item.')


def parse_scores(obj: Dict[str, Any], ids: List[int]) -> Dict[int, float]:
    """Valid scores by id; anything malformed is simply absent."""
    out: Dict[int, float] = {}
    items = obj.get("scores")
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        i, s = it.get("id"), it.get("s")
        if isinstance(i, bool) or not isinstance(i, int) or i not in ids:
            continue
        if isinstance(s, bool) or not isinstance(s, (int, float)) or \
                not math.isfinite(s) or not -1.0 <= s <= 1.0:
            continue
        out.setdefault(i, float(s))
    return out


def score_pending(store: NewsStore, llm: ChatModel, news_cfg: Dict[str, Any],
                  model_name: str = "llm", limit: int = 400) -> Dict[str, int]:
    cutoff = news_cfg.get("cutoff", "16:00")
    cap = int(news_cfg.get("max_per_ticker_day", 10))
    batch = int(news_cfg.get("batch_size", 8))
    attempts = int(news_cfg.get("max_score_attempts", 3))
    pairs = store.pairs()
    live = assign_days(pairs[pairs["status"].isin(["pending", "scored"])],
                       cutoff, cap)
    capped = live[(live["rank"] >= cap) & (live["status"] == "pending")]
    with store.transaction():
        store.conn.executemany(
            "UPDATE scores SET status='capped' WHERE headline_id=? AND "
            "ticker=?", list(zip(capped["headline_id"], capped["ticker"])))
    todo = live[(live["rank"] < cap) & (live["status"] == "pending")]
    todo = todo.sort_values("published", ascending=False).head(limit)
    done = {"scored": 0, "unscored": 0, "capped": len(capped),
            "batches": 0, "llm_failed": 0}
    rows = list(todo.itertuples())
    for k in range(0, len(rows), batch):
        chunk = rows[k:k + batch]
        ids = list(range(len(chunk)))
        user = {"items": [{"id": i, "ticker": r.ticker, "headline": r.title}
                          for i, r in zip(ids, chunk)]}
        try:
            obj, _ = chat_json(llm, [{"role": "system", "content": SYSTEM},
                                     {"role": "user", "content":
                                      json.dumps(user)}],
                               max_tokens=20 + 16 * len(chunk))
        except LLMError:
            done["llm_failed"] += 1
            break                        # the LLM is down: try next poll
        done["batches"] += 1
        got = parse_scores(obj, ids)
        store.set_scores([{"headline_id": r.headline_id, "ticker": r.ticker,
                           "score": got.get(i), "max_attempts": attempts}
                          for i, r in zip(ids, chunk)], model_name)
        done["scored"] += len(got)
        done["unscored"] += len(chunk) - len(got)
    return done
