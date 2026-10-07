"""
council_core.past_decisions — how the council decided similar questions
before, across sessions.

    <vault>/.council_memory/decisions.jsonl     one line per deliberation

After each deliberated question: the question, the Judge's verdict, the
start of the answer and the panel (`record`). Before the next: the most
similar earlier questions (`recall`), as a PAST DECISIONS block that goes to
the Judge — with the vault's evidence, for ranking and critique — and to the
Writer. The Judge is asked for consistency, not obedience: the user may have
changed their mind, or the earlier answer may have been wrong.

Why not council_memory: it stores in chromadb with an embedding model, and
building one may download that model. This is keyword overlap over a JSONL
file — nothing loaded, nothing downloaded, fast for thousands of entries.
The dot-folder keeps it out of the vault index and every vault search.

COUNCIL_PAST_DECISIONS=0 turns recall and recording off. Never raises.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

DIR_NAME = ".council_memory"
FILE_NAME = "decisions.jsonl"
ANSWER_CHARS = 600
MIN_SCORE = 0.25
MAX_RECALL = 2

_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-]{2,}")
_STOP = frozenset("""the and for with that this from what which when where
how why who are was were will would could should can does did have has had
not but you your our their there them they its into about over than then
also just only some any all more most much many such very please tell show
give make need want use using used""".split())

_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("COUNCIL_PAST_DECISIONS", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def path_for(vault_dir: Path) -> Path:
    return Path(vault_dir) / DIR_NAME / FILE_NAME


def _terms(text: str) -> set:
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP}


@dataclass
class Decision:
    ts: str
    question: str
    verdict: str
    answer: str
    panel: List[str]
    score: float = 0.0


def record(vault_dir: Path, question: str, answer: str, verdict: str,
           panel: Sequence[str] = ()) -> bool:
    """Append one deliberation. Only questions the Judge gave a verdict on
    are worth recalling."""
    if not enabled() or not (question or "").strip() \
            or not (answer or "").strip() or not verdict:
        return False
    from datetime import datetime, timezone
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "question": question.strip()[:1000],
             "verdict": verdict,
             "answer": answer.strip()[:ANSWER_CHARS],
             "panel": list(panel)}
    try:
        p = path_for(vault_dir)
        with _lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _load(vault_dir: Path) -> List[Dict[str, Any]]:
    try:
        lines = path_for(vault_dir).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("question"):
            out.append(entry)
    return out


def recall(vault_dir: Path, question: str, *, k: int = MAX_RECALL,
           min_score: float = MIN_SCORE) -> List[Decision]:
    """The `k` earlier decisions most like `question` — by the share of the
    question's key words they also use — newest first among equals."""
    if not enabled():
        return []
    want = _terms(question)
    if not want:
        return []
    scored = []
    for i, e in enumerate(_load(vault_dir)):
        have = _terms(e.get("question", ""))
        if not have:
            continue
        overlap = len(want & have)
        score = overlap / max(len(want), len(have))
        if score >= min_score:
            scored.append((score, i, e))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [Decision(str(e.get("ts", "")), str(e["question"]),
                     str(e.get("verdict", "")), str(e.get("answer", "")),
                     list(e.get("panel") or []), round(s, 2))
            for s, _i, e in scored[:k]]


HEADER = ("PAST DECISIONS — how the council answered similar questions "
          "before. Be consistent with them unless the evidence or the user "
          "now says otherwise; the user may have changed their mind, and an "
          "earlier answer may have been wrong.\n")


def block(decisions: Sequence[Decision]) -> str:
    if not decisions:
        return ""
    parts = [HEADER]
    for d in decisions:
        parts.append(f"\n[{d.ts[:10]}] Q: {d.question}\nVerdict: {d.verdict}"
                     f"\nA (start): {d.answer}\n")
    return "".join(parts).rstrip()


def note(decisions: Sequence[Decision]) -> str:
    if not decisions:
        return ""
    when = ", ".join(d.ts[:10] for d in decisions)
    return (f"Past decisions: {len(decisions)} similar question(s) "
            f"answered before ({when}).")


__all__ = ["DIR_NAME", "Decision", "enabled", "record", "recall", "block",
           "note", "path_for"]
