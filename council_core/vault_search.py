"""
council_core.vault_search — finding a file in the vault, by name or by content.

The instant search is the one Vault feature a user reaches for without
thinking, and it had drifted: the Tk tab searched filenames AND indexed
content, the Qt tab searched filenames only. Same box, same button, quietly
different answers. It lives here now and both call it.

WHAT A MATCH IS
Two different questions share one box:
  * by NAME — every word of the term appears somewhere in the path, or the
    basename matches a `#`/`*` wildcard (`job_####`). No index needed.
  * by CONTENT — a value or a column name in the data index. Needs the index
    built, and says so rather than silently finding nothing.

Every hit carries the REASON it matched ("name match", "pattern match",
'contains "acme"', 'column "Job ID"'). A list of bare filenames makes the user
guess why each one is there, and the reason is most of the answer when the
match came from inside a spreadsheet.

The index half is best-effort by design. A missing or stale index degrades the
search to filenames; it never takes the search down with it.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple


# ── Filename wildcard patterns ──────────────────────────────────────────────
# Users reference files by shape, not spelling: "job_####" means "job_ then any
# four characters" (job_1234, job_0087, job_ab12), and "report_*" means "report_
# then anything". The resolvers below used pure substring matching, so `#`/`*`
# were treated as literal characters and never matched. `compile_name_pattern`
# turns such a token into a safe, anchored, case-insensitive regex:
#   #  → any single character        (the user's "any 4 characters")
#   *  → any run of characters (incl. empty)
#   ?  → any single character
# It returns None when the token has no `#`/`*` wildcard, so callers keep their
# plain-substring behaviour for ordinary names. Pure stdlib, fully offline. The
# generated regex has no nested quantifiers, so there is no catastrophic-
# backtracking risk regardless of user input.

_NAME_WILDCARD_CHARS = ("#", "*")


def compile_name_pattern(token):
    """Compile a filename-wildcard token to a case-insensitive ``re.Pattern``,
    or return ``None`` when ``token`` contains no ``#``/``*`` wildcard."""
    token = (token or "").strip().strip("'\"`")
    if not token:
        return None
    if not any(c in token for c in _NAME_WILDCARD_CHARS):
        return None
    parts = []
    for ch in token:
        if ch == "#" or ch == "?":
            parts.append(".")          # any single character
        elif ch == "*":
            parts.append(".*")         # any run (incl. empty)
        else:
            parts.append(re.escape(ch))
    try:
        return re.compile("".join(parts), re.IGNORECASE)
    except re.error:
        return None


def name_matches_pattern(pat, filename: str) -> bool:
    """True when ``filename`` matches the compiled pattern. Anchored: the
    pattern must span the whole basename OR the whole stem (so ``job_####``
    matches ``job_1234.csv`` via the stem and ``job_####.csv`` via the name)."""
    if pat is None or not filename:
        return False
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return bool(pat.fullmatch(filename) or pat.fullmatch(stem))


def search_vault_filenames(in_dir, term, limit: int = 200):
    """Files under ``in_dir`` whose path/name contains every word of ``term``
    (case-insensitive), OR whose basename matches a ``#``/``*`` wildcard
    pattern in ``term`` (e.g. ``job_####``). App-generated output dirs are
    skipped. Returns a list of (abs_path, reason). Pure + UI-free so it's
    unit-testable."""
    skip = {"derived", "deferred_results", "converted_mongo", "__pycache__",
            ".vault_index", ".stats_cache", "conversation_logs", ".git"}
    # Wildcard mode: match the compiled pattern against each basename. This
    # takes precedence because re.findall(r"[a-z0-9]+", ...) below would
    # silently drop `#`/`*`/`_` and collapse "job_####" to just "job".
    pat = compile_name_pattern(term)
    out = []
    if pat is not None:
        try:
            for dp, dn, fn in os.walk(str(in_dir)):
                dn[:] = [d for d in dn if d not in skip and not d.startswith(".")]
                for f in fn:
                    if f.startswith("."):
                        continue
                    if name_matches_pattern(pat, f):
                        out.append((os.path.join(dp, f), "pattern match"))
                        if len(out) >= limit:
                            return out
        except Exception:
            pass
        return out
    words = [w for w in re.findall(r"[a-z0-9]+", (term or "").lower())
             if len(w) > 0]
    if not words:
        return []
    try:
        for dp, dn, fn in os.walk(str(in_dir)):
            dn[:] = [d for d in dn if d not in skip and not d.startswith(".")]
            for f in fn:
                if f.startswith("."):
                    continue
                full = os.path.join(dp, f)
                full_lc = full.lower()   # lower once per file, not per word
                if all(w in full_lc for w in words):
                    out.append((full, "name match"))
                    if len(out) >= limit:
                        return out
    except Exception:
        pass
    return out


# ============================================================
# Name + content, together
# ============================================================

@dataclass
class SearchResult:
    """Hits as (path, reason) pairs, plus the line that goes under them."""
    ok: bool
    message: str
    hits: List[Tuple[str, str]] = field(default_factory=list)
    searched_content: bool = False


NOTHING_FOUND = ("No files match by name or indexed content. Try a different "
                 "term, or add the data in this tab.")


def search_vault(term: str, in_dir: Path, index: Any = None) -> SearchResult:
    """Find vault files by name or by indexed content. No model, no network.

    ``index`` is a data_index-shaped object or None. When it is None — or when
    it raises, which a half-built index does — the name half still answers.
    That degradation is the point: a search that returns something useful
    beats one that returns an error because an optional index was not ready.
    """
    term = (term or "").strip()
    if not term:
        return SearchResult(True, "Type something to search for.")

    in_dir = Path(in_dir)
    hits: List[Tuple[str, str]] = []
    seen = set()

    def add(path, reason: str) -> None:
        try:
            key = str(Path(path).resolve()).lower()
        except Exception:                                 # noqa: BLE001
            key = str(path).lower()
        if key not in seen:
            seen.add(key)
            hits.append((str(path), reason))

    for full, reason in search_vault_filenames(in_dir, term):
        add(full, reason)

    searched_content = False
    if index is not None:
        try:
            index.refresh()
            for hit in (index.search_value(term, max_per_file=1) or []):
                name = hit.get("file") if isinstance(hit, dict) else None
                if name:
                    path = in_dir / name
                    add(path if path.exists() else name,
                        f"contains \u201c{term}\u201d")
            searched_content = True
        except Exception:                                 # noqa: BLE001
            pass
        try:
            for profile, exact in (index.find_files_with_column(term) or []):
                path = in_dir / profile.name
                add(path if path.exists() else profile.name,
                    f"column \u201c{exact}\u201d")
            searched_content = True
        except Exception:                                 # noqa: BLE001
            pass

    if not hits:
        tail = ("" if searched_content else
                "  (Content was not searched — build the keyword index to "
                "search inside files.)")
        return SearchResult(True, NOTHING_FOUND + tail,
                            searched_content=searched_content)
    return SearchResult(True, f"{len(hits)} match(es)", hits=hits,
                        searched_content=searched_content)
