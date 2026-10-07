"""
council_core.usage_log — what every model call cost, kept for the weekly
placement review.

council_engine reports each call's stats in one place (`_record_stats`): the
role, the model, the backend, tokens, seconds and speed. Until now only the
LAST call per role was kept in memory. `install` subscribes to that report
and appends one line per call to

    <vault>/.council_usage/calls-YYYY-MM.jsonl

so a week of calls can be read back: which role used which model, on which
machine, how often and how fast. That is what the controller reads when it
decides whether models should move between machines.

What a line holds: time, role, model, backend, host (the Ollama server that
answered; "local" for an in-process GGUF model), prompt and reply tokens,
seconds, speeds, and waits. NEVER the prompt or the answer — this is a meter,
not a transcript.

The dot-folder keeps it out of every vault content search (vault_index skips
dot-folders; conversation_logger.PROTECTED_SUBDIRS lists it for the walks
that do not). Appending a line is the only write; a damaged line is skipped
on read, never "repaired". Nothing here talks to a network.
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

DIR_NAME = ".council_usage"
LOCAL_HOST = "local"

#: The stats keys a line keeps; anything else in the report is dropped.
FIELDS = ("role", "model", "backend", "host", "prompt_tokens", "gen_tokens",
          "seconds", "gen_tok_s", "prompt_tok_s", "ttft_s", "wait_s",
          "load_s", "truncated")

_lock = threading.Lock()
_installed: Optional[Callable[[Dict[str, Any]], None]] = None


def usage_dir(vault_dir: Path) -> Path:
    return Path(vault_dir) / DIR_NAME


def _file_for(vault_dir: Path, ts: float) -> Path:
    return usage_dir(vault_dir) / time.strftime("calls-%Y-%m.jsonl",
                                                time.localtime(ts))


def entry_from_stats(stats: Dict[str, Any],
                     now: Optional[float] = None) -> Dict[str, Any]:
    """One line's worth of a call's stats."""
    t = time.time() if now is None else now
    # Rounded DOWN: round() can land a call up to half a millisecond in the
    # future, and a read made in that instant then leaves it out.
    out: Dict[str, Any] = {"ts": math.floor(t * 1000) / 1000}
    for key in FIELDS:
        if key in stats and stats[key] is not None:
            out[key] = stats[key]
    if not out.get("host"):
        out["host"] = LOCAL_HOST if stats.get("backend") == "gguf" else \
            stats.get("host") or LOCAL_HOST
    return out


def record(vault_dir: Path, stats: Dict[str, Any],
           now: Optional[float] = None) -> None:
    """Append one call. Never raises: a meter must not fail a model call."""
    try:
        entry = entry_from_stats(stats, now)
        path = _file_for(vault_dir, entry["ts"])
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:                                     # noqa: BLE001
        pass


def install(vault_dir: Path) -> bool:
    """Start recording every model call into this vault. Idempotent.

    Returns False when the engine cannot be imported (nothing to record)."""
    global _installed
    try:
        import council_engine
    except Exception:                                     # noqa: BLE001
        return False
    with _lock:
        if _installed is not None:
            council_engine.remove_stats_listener(_installed)

        def listener(stats: Dict[str, Any], _vault=Path(vault_dir)) -> None:
            record(_vault, stats)

        _installed = listener
    council_engine.add_stats_listener(listener)
    return True


def uninstall() -> None:
    global _installed
    with _lock:
        fn, _installed = _installed, None
    if fn is None:
        return
    try:
        import council_engine
        council_engine.remove_stats_listener(fn)
    except Exception:                                     # noqa: BLE001
        pass


def _month_files(vault_dir: Path, since: float, until: float) -> List[Path]:
    """The monthly files that can hold calls in [since, until]."""
    names, t = [], since
    while True:
        name = _file_for(vault_dir, t).name
        if name not in names:
            names.append(name)
        if t >= until:
            break
        t = min(until, t + 20 * 86400)
    return [usage_dir(vault_dir) / n for n in names]


def read(vault_dir: Path, since: float,
         until: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every recorded call with since <= ts <= until, oldest first."""
    until = time.time() if until is None else until
    out: List[Dict[str, Any]] = []
    for path in _month_files(vault_dir, since, until):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                entry = json.loads(raw)
                ts = float(entry.get("ts", 0))
            except (ValueError, TypeError, AttributeError):
                continue
            if since <= ts <= until:
                out.append(entry)
    out.sort(key=lambda e: e["ts"])
    return out


def summarise(calls: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per (role, model, host): calls, tokens, time, typical speed and wait,
    busiest first."""
    groups: Dict[tuple, Dict[str, Any]] = {}
    for c in calls:
        key = (c.get("role") or "(no role)", c.get("model") or "?",
               c.get("host") or LOCAL_HOST)
        g = groups.setdefault(key, {"role": key[0], "model": key[1],
                                    "host": key[2], "calls": 0,
                                    "gen_tokens": 0, "seconds": 0.0,
                                    "_speeds": [], "_waits": [],
                                    "truncated": 0})
        g["calls"] += 1
        g["gen_tokens"] += int(c.get("gen_tokens") or 0)
        g["seconds"] += float(c.get("seconds") or 0.0)
        if c.get("gen_tok_s"):
            g["_speeds"].append(float(c["gen_tok_s"]))
        if c.get("wait_s"):
            g["_waits"].append(float(c["wait_s"]))
        if c.get("truncated"):
            g["truncated"] += 1
    out = []
    for g in groups.values():
        speeds, waits = sorted(g.pop("_speeds")), sorted(g.pop("_waits"))
        g["median_tok_s"] = speeds[len(speeds) // 2] if speeds else None
        g["median_wait_s"] = waits[len(waits) // 2] if waits else None
        g["seconds"] = round(g["seconds"], 1)
        out.append(g)
    out.sort(key=lambda g: (-g["seconds"], -g["calls"], g["role"]))
    return out


# ============================================================
# Tool calls — kept apart from model calls, in tools-YYYY-MM.jsonl
# ============================================================
#
# One line per tool call a council member made: time, role, tool, ok,
# seconds, cached. Never the arguments or the result. The weekly review and
# the Council Map read it to show which tools earn their place.

TOOL_FIELDS = ("role", "tool", "ok", "seconds", "cached")


def _tool_file_for(vault_dir: Path, ts: float) -> Path:
    return usage_dir(vault_dir) / time.strftime("tools-%Y-%m.jsonl",
                                                time.localtime(ts))


def record_tool(vault_dir: Path, call: Dict[str, Any],
                now: Optional[float] = None) -> None:
    """Append one tool call. Never raises."""
    try:
        t = time.time() if now is None else now
        entry: Dict[str, Any] = {"ts": math.floor(t * 1000) / 1000}
        entry.update({k: call[k] for k in TOOL_FIELDS if k in call})
        path = _tool_file_for(vault_dir, entry["ts"])
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:                                     # noqa: BLE001
        pass


def read_tools(vault_dir: Path, since: float,
               until: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every recorded tool call with since <= ts <= until, oldest first."""
    until = time.time() if until is None else until
    names = []
    t = since
    while True:
        name = _tool_file_for(vault_dir, t).name
        if name not in names:
            names.append(name)
        if t >= until:
            break
        t = min(until, t + 20 * 86400)
    out: List[Dict[str, Any]] = []
    for name in names:
        try:
            text = (usage_dir(vault_dir) / name).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                entry = json.loads(raw)
                ts = float(entry.get("ts", 0))
            except (ValueError, TypeError, AttributeError):
                continue
            if since <= ts <= until:
                out.append(entry)
    out.sort(key=lambda e: e["ts"])
    return out


def summarise_tools(calls: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per tool: calls, failures, cache hits, roles that used it, most used
    first."""
    groups: Dict[str, Dict[str, Any]] = {}
    for c in calls:
        name = c.get("tool") or "?"
        g = groups.setdefault(name, {"tool": name, "calls": 0, "failed": 0,
                                     "cached": 0, "seconds": 0.0,
                                     "roles": set()})
        g["calls"] += 1
        g["failed"] += 0 if c.get("ok", True) else 1
        g["cached"] += 1 if c.get("cached") else 0
        g["seconds"] += float(c.get("seconds") or 0.0)
        if c.get("role"):
            g["roles"].add(c["role"])
    out = []
    for g in groups.values():
        g["roles"] = sorted(g["roles"])
        g["seconds"] = round(g["seconds"], 2)
        out.append(g)
    out.sort(key=lambda g: (-g["calls"], g["tool"]))
    return out


__all__ = ["DIR_NAME", "LOCAL_HOST", "FIELDS", "usage_dir", "record",
           "install", "uninstall", "read", "summarise", "entry_from_stats",
           "record_tool", "read_tools", "summarise_tools", "TOOL_FIELDS"]
