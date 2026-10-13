"""
council_core.answer_reuse — show an earlier answer at once when the same
question is asked again.

    <vault>/.council_memory/answers.jsonl     one line per PASSED answer

past_decisions keeps the start of every deliberated answer for the Judge to
stay consistent with; this keeps the WHOLE answer of each question the Judge
passed, with a fingerprint of the vault files it rested on (the passages the
Librarian found, the data files the Analyst read). Asking the same thing
again shows that answer immediately — "Answered on 3 Oct (Judge: PASS).
[Use it] [Ask the council again]" — instead of a fresh deliberation.

"The same thing" is strict, because a confident wrong reuse is worse than a
slow right answer:

  * the question's words match the earlier one's almost exactly
    (MIN_SCORE, word overlap) AND every number in it is the same — "a pump
    for 40 l/min" is not "a pump for 50 l/min";
  * it is not a follow-up ("and the other one?", "what about…"), whose
    meaning depends on the conversation;
  * the Judge passed it, within MAX_AGE_DAYS;
  * every file it rested on still exists unchanged (a fingerprint of each
    file's size and modification time) — an edited vault means ask again.

The user decides: nothing is reused without the click. Appending a line is
the only write. COUNCIL_ANSWER_REUSE=0 turns it off. Never raises.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

DIR_NAME = ".council_memory"
FILE_NAME = "answers.jsonl"
MIN_SCORE = 0.9
MAX_AGE_DAYS = 30
MIN_WORDS = 4
ANSWER_CHARS = 60_000

_lock = threading.Lock()
_WORD = re.compile(r"[a-z0-9]+(?:[.'][a-z0-9]+)*")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_FOLLOW_UP = re.compile(
    r"^\s*(and|also|what about|how about|then|but|so|ok so|same for|"
    r"it|its|it's|that|this|those|these|they|them|the other|another|"
    r"why not|what if|and if|now)\b", re.IGNORECASE)
_STOP = frozenset("a an the is are was were be to of in on for and or".split())


def enabled() -> bool:
    return os.environ.get("COUNCIL_ANSWER_REUSE", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def path_for(vault_dir: Path) -> Path:
    return Path(vault_dir) / DIR_NAME / FILE_NAME


def _words(q: str) -> List[str]:
    return [w for w in _WORD.findall((q or "").lower()) if w not in _STOP]


def _numbers(q: str) -> List[str]:
    return sorted(n.replace(",", ".") for n in _NUM.findall(q or ""))


def similarity(a: str, b: str) -> float:
    """Word overlap of two questions, 0–1; 0 when their numbers differ."""
    if _numbers(a) != _numbers(b):
        return 0.0
    wa, wb = set(_words(a)), set(_words(b))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def is_follow_up(q: str) -> bool:
    return bool(_FOLLOW_UP.match(q or "")) or len(_words(q)) < MIN_WORDS


def fingerprint(path: Path) -> str:
    try:
        st = Path(path).stat()
    except OSError:
        return "missing"
    return hashlib.sha1(f"{st.st_size}:{int(st.st_mtime)}".encode()) \
        .hexdigest()[:12]


def source_paths(vault_dir: Path, names: Iterable[str]) -> List[Path]:
    """The vault files `names` (as the Librarian reports sources) refer to."""
    root = Path(vault_dir)
    out = []
    for n in names or ():
        n = str(n or "").strip()
        if not n or n == "?":
            continue
        p = Path(n)
        if not p.is_absolute():
            p = root / n
        if p.exists():
            out.append(p)
            continue
        hits = [h for h in root.rglob(Path(n).name)
                if h.is_file() and not any(
                    part.startswith(".") for part in h.relative_to(root).parts)]
        out.extend(hits[:1])
    return out


@dataclass
class Reusable:
    ts: float
    question: str
    answer: str
    score: float
    sources: Dict[str, str] = field(default_factory=dict)

    def note(self) -> str:
        when = time.strftime("%d %b %Y", time.localtime(self.ts))
        src = (f", from {len(self.sources)} vault file(s) that have not "
               "changed since" if self.sources else "")
        return (f"You asked this on {when} and the Judge passed the answer"
                f"{src}. It is shown below. Use it, or ask the council "
                "again.")


def record(vault_dir: Path, question: str, answer: str, verdict: str,
           sources: Iterable[Path] = (), now: Optional[float] = None) -> bool:
    """Keep a PASSED answer for reuse. Follow-ups are not kept: their
    meaning was the conversation's."""
    if not enabled() or verdict != "PASS" or not (answer or "").strip() \
            or is_follow_up(question):
        return False
    root = Path(vault_dir)
    srcs: Dict[str, str] = {}
    for p in sources or ():
        try:
            key = Path(p).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        srcs[key] = fingerprint(Path(p))
    entry = {"ts": time.time() if now is None else now,
             "question": question.strip()[:2000],
             "answer": answer.strip()[:ANSWER_CHARS], "sources": srcs}
    try:
        p = path_for(vault_dir)
        with _lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def find(vault_dir: Path, question: str, *, now: Optional[float] = None,
         max_age_days: float = MAX_AGE_DAYS) -> Optional[Reusable]:
    """The most recent passed answer to this same question whose files are
    unchanged, or None."""
    if not enabled() or is_follow_up(question):
        return None
    now = time.time() if now is None else now
    try:
        lines = path_for(vault_dir).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    root = Path(vault_dir)
    for raw in reversed(lines):
        try:
            e = json.loads(raw)
            ts = float(e.get("ts", 0))
        except (ValueError, TypeError, AttributeError):
            continue
        if now - ts > max_age_days * 86400:
            break                                 # older ones are older still
        score = similarity(question, str(e.get("question", "")))
        if score < MIN_SCORE:
            continue
        srcs = e.get("sources") or {}
        if any(fingerprint(root / rel) != fp for rel, fp in srcs.items()):
            continue                              # a file changed: ask again
        return Reusable(ts, str(e["question"]), str(e.get("answer", "")),
                        round(score, 2), dict(srcs))
    return None


__all__ = ["Reusable", "record", "find", "similarity", "is_follow_up",
           "source_paths", "enabled", "path_for"]
