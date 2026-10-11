"""One poll: every ticker's feed plus the general feeds, stored with their
ticker mappings. A failing feed is logged and skipped; the rest go on."""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Dict, List, Sequence

from .feeds import FeedError, fetch, parse
from .mapping import map_tickers
from .store import NewsStore


def poll(store: NewsStore, news_cfg: Dict[str, Any], tickers: Sequence[str],
         fetcher: Callable[[str, float], bytes] = fetch,
         now: Callable[[], dt.datetime] = lambda: dt.datetime.now(
             dt.timezone.utc)) -> Dict[str, Any]:
    aliases = news_cfg.get("aliases") or {}
    timeout = float(news_cfg.get("timeout_s", 20))
    sources: List[tuple] = []
    template = news_cfg.get("ticker_feed")
    skip = set(news_cfg.get("skip_ticker_feeds", []))
    if template:
        sources += [(f"ticker:{t}", template.format(ticker=t), t)
                    for t in tickers if t not in skip]
    sources += [(f["name"], f["url"], None)
                for f in news_cfg.get("feeds") or []]
    report = {"sources": len(sources), "items": 0, "new": 0, "errors": []}
    for name, url, own in sources:
        try:
            items = parse(fetcher(url, timeout))
        except FeedError as exc:
            store.log_poll(name, 0, 0, str(exc)[:500])
            report["errors"].append(f"{name}: {exc}")
            continue
        tickers_for: Dict[str, List[str]] = {}
        methods: Dict[str, Dict[str, str]] = {}
        for it in items:
            found = map_tickers(it.title, tickers, aliases)
            m = {t: "title" for t in found}
            if own:
                found.add(own)
                m[own] = "feed"
            tickers_for[it.title] = sorted(found)
            methods[it.title] = m
        new = store.add(name, items, tickers_for, now(), methods)
        store.log_poll(name, len(items), new)
        report["items"] += len(items)
        report["new"] += new
    return report
