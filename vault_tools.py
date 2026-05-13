"""
Vault ergonomics — small helpers for browsing the vault, detecting
duplicates, and searching past conversations. Used by chat intents
(`vault stats`, `find duplicates`, `search history for X`, etc.) and
available in the analyst sandbox too.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ============================================================
# Vault stats
# ============================================================

def vault_stats(vault_dir: Path) -> Dict[str, Any]:
    """Return a snapshot of the vault: per-extension counts and sizes,
    last-modified timestamp, total file count, total size.

    Excludes the index file and the pipelines/out/ folder (since those
    are generated artifacts).
    """
    vault_dir = Path(vault_dir)
    if not vault_dir.exists():
        return {"error": f"vault not found: {vault_dir}"}

    by_ext: Dict[str, Dict[str, int]] = defaultdict(lambda: {"count": 0, "size": 0})
    total_files = 0
    total_size = 0
    latest_mtime = 0.0
    largest: List[Tuple[int, str]] = []

    skip_segments = {"__pycache__", "_wf_stage"}
    for p in vault_dir.rglob("*"):
        if not p.is_file():
            continue
        parts_lower = [s.lower() for s in p.parts]
        if any(seg in parts_lower for seg in skip_segments):
            continue
        # Skip the generated index + denylist files
        if p.name in ("vault_index.json", "fuzzy_denylist.json"):
            continue
        # Skip the modified pipelines folder
        if "pipelines" in parts_lower and "out" in parts_lower:
            try:
                idx_p = parts_lower.index("pipelines")
                if idx_p + 1 < len(parts_lower) and parts_lower[idx_p + 1] == "out":
                    continue
            except ValueError:
                pass
        try:
            st = p.stat()
        except Exception:
            continue
        ext = p.suffix.lower() or "(none)"
        by_ext[ext]["count"] += 1
        by_ext[ext]["size"]  += int(st.st_size)
        total_files += 1
        total_size  += int(st.st_size)
        if st.st_mtime > latest_mtime:
            latest_mtime = st.st_mtime
        largest.append((int(st.st_size), str(p.relative_to(vault_dir))))

    largest.sort(reverse=True)
    return {
        "vault_dir":   str(vault_dir),
        "total_files": total_files,
        "total_size":  total_size,
        "by_ext":      {k: dict(v) for k, v in sorted(by_ext.items(),
                          key=lambda kv: -kv[1]["count"])},
        "last_modified_ts": latest_mtime,
        "last_modified_iso": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_mtime))
            if latest_mtime else ""
        ),
        "largest_files": [(s, n) for s, n in largest[:10]],
    }


def format_vault_stats(stats: Dict[str, Any]) -> str:
    if "error" in stats:
        return f"vault_stats error: {stats['error']}"
    lines = [
        f"Vault: {stats['vault_dir']}",
        f"  total files: {stats['total_files']}",
        f"  total size:  {_human_size(stats['total_size'])}",
        f"  last modified: {stats['last_modified_iso']}",
        "  by extension:",
    ]
    for ext, info in stats["by_ext"].items():
        lines.append(f"    {ext:<10} {info['count']:>5} files  "
                     f"{_human_size(info['size']):>10}")
    if stats["largest_files"]:
        lines.append("  largest files:")
        for size, name in stats["largest_files"][:5]:
            lines.append(f"    {_human_size(size):>10}  {name}")
    return "\n".join(lines)


def _human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


# ============================================================
# Duplicate file detection
# ============================================================

def find_duplicate_files(
    vault_dir: Path,
    *,
    extensions: Optional[Iterable[str]] = None,
    min_size_bytes: int = 64,
) -> List[List[str]]:
    """Find groups of files in the vault with identical SHA-256 hashes.

    Returns a list of duplicate groups; each group is a list of
    file paths (relative to vault_dir). Tiny files are skipped to
    avoid grouping every 0-byte placeholder.
    """
    vault_dir = Path(vault_dir)
    if not vault_dir.exists():
        return []

    ext_filter = ({e.lower() if e.startswith(".") else f".{e.lower()}"
                   for e in extensions} if extensions else None)

    by_hash: Dict[str, List[str]] = defaultdict(list)
    for p in vault_dir.rglob("*"):
        if not p.is_file():
            continue
        if ext_filter and p.suffix.lower() not in ext_filter:
            continue
        try:
            if p.stat().st_size < min_size_bytes:
                continue
        except Exception:
            continue
        if p.name in ("vault_index.json", "fuzzy_denylist.json"):
            continue
        try:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            by_hash[h.hexdigest()].append(str(p.relative_to(vault_dir)))
        except Exception:
            continue

    return [paths for paths in by_hash.values() if len(paths) > 1]


def format_duplicates(groups: List[List[str]]) -> str:
    if not groups:
        return "No duplicate files found."
    lines = [f"Found {len(groups)} duplicate group(s):"]
    for i, group in enumerate(groups, start=1):
        lines.append(f"  Group {i} ({len(group)} copies):")
        for path in group:
            lines.append(f"    {path}")
    return "\n".join(lines)


# ============================================================
# Conversation history search
# ============================================================

_HISTORY_SUBDIR = "conversations"


def _conversations_dir(vault_dir: Path) -> Path:
    return Path(vault_dir) / _HISTORY_SUBDIR


def _load_conversations(vault_dir: Path) -> List[Tuple[Path, List[Dict[str, Any]]]]:
    """Read every conversation file in vault/conversations/.

    Returns [(file_path, [turn_dict, ...]), ...]. Conversations are
    stored as JSONL by ConversationStore.append, one turn per line.
    """
    out: List[Tuple[Path, List[Dict[str, Any]]]] = []
    convo_dir = _conversations_dir(vault_dir)
    if not convo_dir.exists():
        return out
    for p in sorted(convo_dir.glob("*.jsonl")) + sorted(convo_dir.glob("*.json")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        turns: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                turn = json.loads(line)
                if isinstance(turn, dict):
                    turns.append(turn)
            except Exception:
                continue
        if turns:
            out.append((p, turns))
    return out


def query_history_search(
    vault_dir: Path,
    query: str,
    *,
    limit: int = 10,
    who_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Substring-search past conversations for turns containing `query`.

    Returns up to `limit` matches, each annotated with the source file
    name and the turn's timestamp + speaker.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    out: List[Dict[str, Any]] = []
    for path, turns in _load_conversations(vault_dir):
        for turn in turns:
            text = str(turn.get("text", ""))
            who = str(turn.get("who", ""))
            if who_filter and who.lower() != who_filter.lower():
                continue
            if q in text.lower():
                out.append({
                    "session":   path.stem,
                    "ts":        turn.get("ts", ""),
                    "who":       who,
                    "text":      text,
                })
                if len(out) >= limit:
                    return out
    return out


def recent_queries(
    vault_dir: Path,
    *,
    n: int = 10,
) -> List[Dict[str, Any]]:
    """Return the last `n` user turns across all conversations, newest first."""
    user_turns: List[Dict[str, Any]] = []
    for path, turns in _load_conversations(vault_dir):
        for turn in turns:
            if str(turn.get("who", "")).lower() == "user":
                user_turns.append({
                    "session": path.stem,
                    "ts":      turn.get("ts", ""),
                    "text":    str(turn.get("text", "")),
                })
    user_turns.sort(key=lambda t: t.get("ts", ""), reverse=True)
    return user_turns[:n]


def format_history_hits(hits: List[Dict[str, Any]]) -> str:
    if not hits:
        return "No matches found in past conversations."
    lines = [f"Found {len(hits)} match(es):"]
    for i, h in enumerate(hits, start=1):
        snippet = h.get("text", "")[:200].replace("\n", " ")
        lines.append(f"  [{i}] {h['session']}  {h['ts']}  {h['who']}:")
        lines.append(f"      {snippet}{'...' if len(h.get('text', '')) > 200 else ''}")
    return "\n".join(lines)
