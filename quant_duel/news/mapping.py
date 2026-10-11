"""Which tickers a headline is about.

A headline from a per-ticker feed belongs to that ticker. Any headline also
maps to every ticker it names: a cashtag ($AAPL), the symbol in brackets
((AAPL) or NASDAQ:AAPL), or one of the ticker's aliases from config
(whole words, case-insensitive — "Apple" matches, "Pineapple" does not).
"""
from __future__ import annotations

import re
from typing import Dict, List, Sequence, Set


def map_tickers(title: str, tickers: Sequence[str],
                aliases: Dict[str, List[str]]) -> Set[str]:
    found: Set[str] = set()
    for t in tickers:
        sym = re.escape(t)
        if re.search(rf"(\${sym}\b|\({sym}\)|:\s?{sym}\b)", title):
            found.add(t)
            continue
        for alias in aliases.get(t, []):
            if re.search(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", title,
                         re.I):
                found.add(t)
                break
    return found
