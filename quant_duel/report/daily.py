"""``reports/<node>/YYYY-MM-DD.md``: a ~150-word LLM summary of the day
(signals, paper P&L vs control and SPY, tuner changes) above a table of the
exact numbers it was given.

The numbers come from the ledger, never from the LLM — the model only
writes prose from the facts handed to it. If the LLM is down the report is
still written, numbers only, with a one-line note.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..llm.client import ChatModel, LLMError
from ..paper.books import canonical
from ..paper.ledger import Ledger

MAX_WORDS = 250


def facts(ledger: Ledger, day: dt.date) -> Dict[str, Any]:
    d = day.isoformat()
    eq = ledger.frame("equity")
    if not (eq["date"] == d).any():
        raise ValueError(f"no paper-trading day {d} in the ledger")
    start = ledger.frame("books").set_index("book")["start_cash"]
    out: Dict[str, Any] = {"node": ledger.meta("node_id"), "date": d}
    pnl = {}
    for book, g in eq.sort_values("date").groupby("book"):
        g = g[g["date"] <= d]
        today = g["equity"].iloc[-1]
        prev = g["equity"].iloc[-2] if len(g) > 1 else start[book]
        pnl[book] = {"equity": round(float(today), 2),
                     "day_pct": round(100 * (today / prev - 1), 3),
                     "total_pct": round(100 * (today / start[book] - 1), 3)}
    out["pnl"] = pnl
    p = ledger.frame("predictions", "book='live' AND date=?", (d,))
    if len(p):
        top = p.sort_values("p", ascending=False).head(5)
        out["signals"] = {
            "long": int(p["signal"].sum()), "of": int(len(p)),
            "top": [{"ticker": r.ticker, "p_up": round(r.p, 3)}
                    for r in top.itertuples()]}
    scored = ledger.frame("predictions").dropna(subset=["target"])
    if len(scored):
        last = scored["date"].max()
        s = scored[scored["date"] == last]
        out["yesterday_accuracy"] = {
            book: round(float(((g["p"] >= 0.5) == (g["target"] == 1)).mean()),
                        3) for book, g in s.groupby("book")}
    log = ledger.frame("tuner_log")
    week_ago = (day - dt.timedelta(days=7)).isoformat()
    recent = log[(log["day"] > week_ago) & (log["day"] <= d)]
    out["tuner"] = [{"day": r.day, "status": r.status,
                     "changes": json.loads(r.changes_json)
                     if r.status in ("adopted", "rejected") else None}
                    for r in recent.itertuples()]
    return out


PROMPT = (
    "Write a plain-text summary of about {words} words of today's paper "
    "trading for node {node}. Cover the signals, the live strategy's P&L "
    "versus the frozen control and buy-and-hold SPY, and any tuner changes. "
    "Use ONLY the numbers in FACTS; do not invent numbers, do not give "
    "investment advice, no headings or lists.")


def clean_summary(text: str) -> str:
    text = re.sub(r"```.*?```", "", text or "", flags=re.S).strip()
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    words = text.split()
    if not words:
        raise LLMError("empty summary")
    if len(words) > MAX_WORDS:
        text = " ".join(words[:MAX_WORDS]) + " …"
    return text


def _table(f: Dict[str, Any]) -> List[str]:
    lines = ["| book | equity | day % | total % |", "|---|---:|---:|---:|"]
    for book in ("live", "control", "spy"):
        if book in f["pnl"]:
            x = f["pnl"][book]
            lines.append(f"| {book} | {x['equity']:,.2f} | {x['day_pct']:+.3f} "
                         f"| {x['total_pct']:+.3f} |")
    sig = f.get("signals")
    if sig:
        lines += ["", f"Signals for the next session: long {sig['long']} of "
                  f"{sig['of']}. Highest P(up): " + ", ".join(
                      f"{t['ticker']} {t['p_up']:.3f}" for t in sig["top"])]
    if f.get("yesterday_accuracy"):
        lines += ["", "Accuracy of the last scored day: " + ", ".join(
            f"{b} {a:.1%}" for b, a in sorted(f["yesterday_accuracy"].items()))]
    for t in f.get("tuner", []):
        lines.append(f"- tuner {t['day']}: {t['status']}" +
                     (f" {canonical(t['changes'])}" if t["changes"] else ""))
    return lines


def write_report(ledger: Ledger, day: dt.date, out_dir: Path,
                 llm: Optional[ChatModel], words: int = 150) -> Path:
    f = facts(ledger, day)
    summary, note = None, None
    if llm is None:
        note = "LLM summary skipped: no LLM configured."
    else:
        try:
            summary = clean_summary(llm.chat(
                [{"role": "system", "content": PROMPT.format(
                    words=words, node=f["node"])},
                 {"role": "user", "content": "FACTS " + canonical(f)}],
                max_tokens=int(words * 2.5)))
        except LLMError as exc:
            note = f"LLM summary skipped: {exc}"
    body = [f"# Paper trading — node {f['node']} — {f['date']}", ""]
    body += [summary] if summary else [f"_{note}_"]
    body += ["", "## Numbers (from the ledger)", ""] + _table(f)
    body += ["", "_Paper trading only — no real orders._", ""]
    path = Path(out_dir) / f"{f['date']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(body), encoding="utf-8")
    os.replace(tmp, path)
    return path
