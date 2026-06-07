"""
compare_runners.py — blind A/B/.../N evaluation of local ModelRunners.

Pure orchestration; no Tk imports here so the logic is easy to unit-test
and to drive from a CLI. ``compare_panel.py`` wraps this in a Tkinter
window for the GUI.

Pipeline:

    1. Build N runners (each via inferno_local.model_runner.build_runner —
       cloud backends already blocked at the factory).
    2. Fire concurrent ``chat`` calls against the same prompt on a
       ThreadPoolExecutor (one worker per runner). Streaming is
       deliberately skipped for the blind path — partial reveals could
       leak identity (e.g. a streaming Granite vs a streaming Phi will
       have distinct emit patterns).
    3. Anonymise into A, B, C, ... with column order randomised per run.
    4. Caller renders columns + collects user ranking (best→worst).
    5. ``reveal(run, ranking)`` returns ground truth + appends to a
       local jsonl log at ``~/.council/vault/.compare_log.jsonl``.

Nothing about the comparison leaves the machine: every runner enforces
its own loopback check; the log is local; the orchestrator never opens
a socket.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from inferno_local import model_runner

_LOG = logging.getLogger("compare_runners")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_run_id() -> str:
    return f"cmp-{_now_ms()}-{random.randint(1000, 9999)}"


@dataclass
class RunnerCandidate:
    """One backend config under test. ``label`` is the display name used
    *after* reveal — never shown during the blind phase."""
    label: str
    config: Dict[str, Any]


@dataclass
class CompareColumn:
    """One result in a CompareRun. ``column_id`` is the public anonymised
    name (A, B, C, ...); ``hidden_label`` is the ground-truth identity
    that ``reveal()`` will surface."""
    column_id: str
    text: str
    elapsed_s: float
    error: Optional[str] = None
    hidden_label: str = ""              # ground truth (kept server-side)
    hidden_config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CompareRun:
    run_id: str
    prompt: str
    columns: List[CompareColumn]
    started_at_ms: int
    revealed: bool = False
    ranking: Optional[List[str]] = None    # column_ids best -> worst


# ============================================================
# Orchestration
# ============================================================

def run_blind(prompt: str,
              candidates: Sequence[RunnerCandidate],
              *,
              temperature: float = 0.2,
              max_tokens: int = 600,
              timeout_s: float = 180.0,
              shuffle: bool = True,
              system_prompt: Optional[str] = None,
              rng: Optional[random.Random] = None) -> CompareRun:
    """Run every candidate on ``prompt`` concurrently and return the
    blinded result set. ``rng`` is exposed for testability — tests can
    pin a seeded Random for deterministic shuffles."""
    if not candidates:
        raise ValueError("compare_runners.run_blind: no candidates")
    rng = rng or random.Random()

    # Build runners up front so a bad config bubbles before any inference.
    built: List[tuple[RunnerCandidate, model_runner.ModelRunner]] = []
    for c in candidates:
        r = model_runner.build_runner(c.config)   # cloud-blocked at factory
        built.append((c, r))

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    results: Dict[str, CompareColumn] = {}
    lock = threading.Lock()

    def _worker(idx: int, cand: RunnerCandidate, r: model_runner.ModelRunner):
        t0 = time.time()
        try:
            out = r.chat(messages, temperature=temperature,
                         max_tokens=max_tokens)
            err = None
        except Exception as exc:
            out = ""
            err = repr(exc)
        elapsed = time.time() - t0
        with lock:
            results[cand.label] = CompareColumn(
                column_id="",          # filled in after shuffle
                text=out,
                elapsed_s=elapsed,
                error=err,
                hidden_label=cand.label,
                hidden_config=dict(cand.config),
            )

    with ThreadPoolExecutor(max_workers=len(built)) as pool:
        futures = [pool.submit(_worker, i, c, r)
                   for i, (c, r) in enumerate(built)]
        for fut in as_completed(futures, timeout=timeout_s):
            try:
                fut.result()
            except Exception as exc:
                _LOG.warning("compare worker raised: %r", exc)

    # Randomise column order; assign A/B/C/...
    ordered_labels = [c.label for c in candidates]
    if shuffle:
        rng.shuffle(ordered_labels)
    cols: List[CompareColumn] = []
    for i, lbl in enumerate(ordered_labels):
        col = results.get(lbl)
        if col is None:
            # Worker never landed (timeout) — fabricate a placeholder so the
            # column id sequence stays contiguous.
            col = CompareColumn(
                column_id=chr(ord("A") + i),
                text="",
                elapsed_s=0.0,
                error="(no result — timed out)",
                hidden_label=lbl,
                hidden_config=dict(next(c.config for c in candidates if c.label == lbl)),
            )
        col.column_id = chr(ord("A") + i)
        cols.append(col)

    return CompareRun(
        run_id=_new_run_id(),
        prompt=prompt,
        columns=cols,
        started_at_ms=_now_ms(),
    )


# ============================================================
# Reveal + local audit log
# ============================================================

def reveal(run: CompareRun,
           ranking: Optional[Sequence[str]] = None,
           *,
           log_path: Optional[Path] = None) -> Dict[str, Any]:
    """Unmask the columns and append a record to the local log.

    ``ranking`` is the user's best→worst ordering of column_ids
    (["B", "A", "C"]). Pass None to skip ranking but still reveal.
    ``log_path`` defaults to ~/.council/vault/.compare_log.jsonl —
    callers should not override unless they know what they're doing.
    Returns a JSON-serialisable dict the GUI / CLI can render directly.
    """
    run.revealed = True
    if ranking is not None:
        run.ranking = list(ranking)
    record = {
        "run_id":    run.run_id,
        "prompt":    run.prompt,
        "started_at_ms": run.started_at_ms,
        "ranking":   run.ranking,
        "columns":   [
            {
                "column_id":   c.column_id,
                "identity":    c.hidden_label,
                "backend":     c.hidden_config.get("backend"),
                "elapsed_s":   round(c.elapsed_s, 2),
                "error":       c.error,
                "chars":       len(c.text or ""),
            }
            for c in run.columns
        ],
    }
    _append_log(record, log_path)
    return record


def _default_log_path() -> Path:
    if os.environ.get("COUNCIL_VAULT_ROOT"):
        return Path(os.environ["COUNCIL_VAULT_ROOT"]).expanduser().resolve() \
            / ".compare_log.jsonl"
    return Path.home() / ".council" / "vault" / ".compare_log.jsonl"


def _append_log(record: Dict[str, Any],
                log_path: Optional[Path] = None) -> None:
    p = log_path or _default_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        _LOG.warning("compare log append failed: %r", exc)


# ============================================================
# Headline-rank summariser (used by the wizard's "what should I use
# by default" prompt). Reads the log and tallies per-identity wins.
# ============================================================

def winners_summary(log_path: Optional[Path] = None,
                    top_k: int = 5) -> List[Dict[str, Any]]:
    p = log_path or _default_log_path()
    if not p.exists():
        return []
    wins: Dict[str, int] = {}
    runs = 0
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                runs += 1
                ranking = rec.get("ranking") or []
                if not ranking:
                    continue
                # Map column_id (A, B, ...) -> identity
                col_to_id = {c["column_id"]: c["identity"] for c in rec.get("columns", [])}
                if ranking:
                    winner_id = col_to_id.get(ranking[0])
                    if winner_id:
                        wins[winner_id] = wins.get(winner_id, 0) + 1
    except Exception as exc:
        _LOG.warning("winners_summary read failed: %r", exc)
    ordered = sorted(wins.items(), key=lambda kv: -kv[1])[:top_k]
    return [{"identity": k, "wins": v, "runs": runs} for k, v in ordered]
