"""
steam_analyst.py — deterministic query layer over ingested Steam data.

The hard rule (carried over from the data-analyst pattern in the
previous build): **the council never reads raw Steam JSON directly**.
The model would hallucinate concrete numbers from training memory
("according to Steam, X has 50K MAU…") rather than from current
cached data. Instead:

  1. ``steam_ingest`` writes cache files to ``vault/steam/`` (protected)
  2. This module reads the cache and computes deterministic answers
  3. The council sees only the computed answer + citations, not rows

Public API mirrors ``vault_analyst`` in shape so the Council's
auto-summon routing can treat the Steam Market Analyst as another
specialist.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ============================================================
# Result type
# ============================================================

@dataclass
class SteamAnalystResult:
    """Result of a single analyst computation."""
    answer:           str = ""
    confidence:       str = "medium"           # "high" | "medium" | "low"
    computed_values:  Dict[str, Any] = field(default_factory=dict)
    sources:          List[Path] = field(default_factory=list)
    error:            str = ""

    def to_injection_block(self) -> str:
        """Format as a ``[STEAM ANALYST RESULT]`` block for the council
        prompt injector. Mirrors ``vault_analyst.format_result_for_prompt``."""
        lines = [
            "[STEAM ANALYST RESULT — computed from local Steam cache, "
            f"confidence={self.confidence}]",
        ]
        if self.error:
            lines.append(f"  error: {self.error}")
            return "\n".join(lines)
        if self.answer:
            for ln in self.answer.splitlines():
                lines.append(f"  {ln}")
        if self.computed_values:
            lines.append("  computed:")
            for k, v in self.computed_values.items():
                lines.append(f"    {k}: {v}")
        if self.sources:
            lines.append("  sources:")
            for s in self.sources:
                lines.append(f"    - {s.name}")
        return "\n".join(lines)


# ============================================================
# Cache I/O
# ============================================================

def list_cached_pulls(vault_dir: Any) -> List[Path]:
    """Return JSONL files under vault/steam/, newest first."""
    from steam_ingest import STEAM_CACHE_SUBDIR
    base = Path(vault_dir).expanduser().resolve() / STEAM_CACHE_SUBDIR
    if not base.exists():
        return []
    files = [p for p in base.glob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except Exception as exc:
        print(f"[steam_analyst] could not read {path}: {exc!r}")
    return out


def _latest_pull(vault_dir: Any, source: str,
                  request_prefix: str = "") -> Optional[Path]:
    """Return the most recent cached file matching the given source +
    request prefix, or None."""
    pulls = list_cached_pulls(vault_dir)
    for p in pulls:
        name = p.name
        if not name.startswith(f"{source}_"):
            continue
        rest = name[len(source) + 1:]
        if request_prefix and not rest.startswith(request_prefix):
            continue
        return p
    return None


# ============================================================
# Owners parser (SteamSpy returns ranges as strings)
# ============================================================

# SteamSpy gives "owners": "20,000,000 .. 50,000,000" — parse into
# (lower, upper) integers so we can do real math.

_OWNERS_RE = re.compile(
    r'([\d,]+)\s*\.\.\s*([\d,]+)',
)


def _parse_owners(s: str) -> Tuple[int, int]:
    if not s:
        return (0, 0)
    m = _OWNERS_RE.search(s)
    if not m:
        return (0, 0)
    try:
        lo = int(m.group(1).replace(",", ""))
        hi = int(m.group(2).replace(",", ""))
        return lo, hi
    except Exception:
        return (0, 0)


def _owners_midpoint(s: str) -> int:
    lo, hi = _parse_owners(s)
    return (lo + hi) // 2 if (lo or hi) else 0


# ============================================================
# Routes — keyword → computation
# ============================================================

def _looks_like_top_question(q: str) -> bool:
    return bool(re.search(r"\btop\b|biggest|highest|most\s+(?:played|popular|owned)",
                          q, re.IGNORECASE))


_GENRE_VOCAB = {
    "platformer", "metroidvania", "roguelike", "roguelite",
    "shooter", "fps", "rpg", "jrpg", "puzzle", "deck-builder",
    "deckbuilder", "visual-novel", "vn", "city-builder",
    "survival", "horror", "walking-sim", "rhythm", "racing",
    "sports", "strategy", "tactics", "bullet-hell", "twin-stick",
    "rts", "4x", "moba", "fighting", "stealth", "co-op",
    "couch-co-op", "soulslike", "souls-like", "tower-defense",
    "open-world", "sandbox", "crafting", "management", "sim",
}


def _looks_like_genre_question(q: str) -> Optional[str]:
    """Return the genre substring referenced in the question, if any.

    Prefer matching a known genre vocabulary word (case-insensitive,
    hyphen-tolerant). Falls back to ``"X games"`` / ``"genre: X"`` /
    ``"for X games"`` heuristics for genres not in the vocab.
    """
    if not q:
        return None
    # Vocabulary match — strongest signal
    ql = q.lower().replace("_", "-")
    for token in _GENRE_VOCAB:
        # Match the genre as a whole word, hyphenated or not
        pat = r"\b" + re.escape(token.replace("-", " ")) + r"\b"
        if re.search(pat, ql) or re.search(
            r"\b" + re.escape(token) + r"\b", ql,
        ):
            # Return the matched form in title case for the cache lookup
            return token.replace("-", " ").title()

    # Fallback patterns — for genres we haven't enumerated.
    fallback_pats = [
        r"\b(?:for|in|of)\s+([A-Za-z][\w\-]{2,20})\s+(?:games?|titles?)\b",
        r"\b([A-Za-z][\w\-]{2,20})\s+games?\b",
        r"\bgenre[: ]+([A-Za-z][\w\-\s]{2,20})",
    ]
    for pat in fallback_pats:
        m = re.search(pat, q, re.IGNORECASE)
        if m:
            g = m.group(1).strip()
            # Filter common false positives — demonstratives, generic
            # descriptors, and broad-category words that aren't a real
            # genre to scope a SteamSpy pull against.
            if g.lower() in {
                "good", "current", "recent", "top", "played", "popular",
                "buy", "indie", "any", "all", "video", "computer",
                "these", "those", "this", "that", "such", "the",
                "many", "most", "some", "new", "old", "best",
                "your", "their", "my", "our", "his", "her",
                "online", "offline", "mobile", "console", "pc",
            }:
                continue
            if 3 <= len(g) <= 30:
                return g
    return None


def _looks_like_revenue_question(q: str) -> bool:
    # Stem-style match so "earn" / "earning" / "earns" / "earnings" all
    # hit. Same for "sales" / "sell", "owners" / "owned" / "own".
    return bool(re.search(
        r"\b(?:revenue|earn|sell|sales|gross|owner|own|profit)",
        q, re.IGNORECASE,
    ))


# ============================================================
# Computations
# ============================================================

def _compute_top(records: List[Dict[str, Any]], k: int = 10,
                 sort_key: str = "ccu") -> List[Dict[str, Any]]:
    """Top-K by ``sort_key`` (default: ccu = current concurrent users).
    Falls back to players_2weeks if ccu is missing."""
    def _score(r):
        for k_try in (sort_key, "ccu", "players_2weeks", "current"):
            v = r.get(k_try)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
        return 0.0
    return sorted(records, key=_score, reverse=True)[:k]


def _compute_revenue_band(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Estimate revenue distribution using owners × initialprice."""
    revenues = []
    for r in records:
        owners_str = r.get("owners", "")
        lo, hi = _parse_owners(owners_str)
        # initialprice is in cents according to SteamSpy
        try:
            price_cents = float(r.get("initialprice") or 0)
        except Exception:
            price_cents = 0.0
        price = price_cents / 100.0
        if (lo or hi) and price > 0:
            mid = (lo + hi) / 2.0
            revenues.append(mid * price)
    if not revenues:
        return {}
    revenues.sort()
    return {
        "n":     len(revenues),
        "min":   round(revenues[0], 0),
        "p25":   round(revenues[len(revenues) // 4], 0),
        "median": round(statistics.median(revenues), 0),
        "p75":   round(revenues[3 * len(revenues) // 4], 0),
        "max":   round(revenues[-1], 0),
    }


def _format_top_block(top: List[Dict[str, Any]], header: str) -> str:
    if not top:
        return f"{header}: (no records to rank)"
    lines = [header + ":"]
    for r in top:
        name = r.get("name") or f"appid {r.get('appid')}"
        ccu = r.get("ccu") or r.get("current") or r.get("players_2weeks") or "?"
        owners = r.get("owners") or ""
        bits = [f"  • {name} — players: {ccu}"]
        if owners:
            bits.append(f"owners: {owners}")
        if r.get("developer"):
            bits.append(f"dev: {r['developer']}")
        lines.append(" | ".join(bits))
    return "\n".join(lines)


# ============================================================
# Public answer router
# ============================================================

def answer_question(
    question: str,
    vault_dir: Any,
    *,
    goal: str = "",
) -> SteamAnalystResult:
    """Route a natural-language market question to the right computation.

    Examples:
      • "top played puzzle games"     → genre filter + top by ccu
      • "median revenue for roguelikes" → genre filter + revenue stats
      • "what's hot on Steam right now" → top in last 2 weeks
      • "concurrent players in deck builders" → genre + sort by ccu

    Falls back to a structured "no answer" result rather than letting
    the council invent one.
    """
    question_l = (question or "").strip()
    if not question_l:
        return SteamAnalystResult(
            error="empty question", confidence="low",
        )

    # ── Try to scope by genre ──
    genre_hint = _looks_like_genre_question(question_l)
    records: List[Dict[str, Any]] = []
    sources: List[Path] = []
    if genre_hint:
        # Need a cached genre pull. We accept any prefix that includes
        # the genre name with underscores swapped for spaces.
        path = _latest_pull(vault_dir, "steamspy",
                             f"genre_{genre_hint}")
        # Be forgiving: also try a slugified variant
        if path is None:
            slug = re.sub(r"[^A-Za-z0-9]+", "_", genre_hint)
            path = _latest_pull(vault_dir, "steamspy", f"genre_{slug}")
        if path is None:
            return SteamAnalystResult(
                answer=(
                    f"No cached SteamSpy data for genre {genre_hint!r}. "
                    f"Open the Steam Market tab and click "
                    f"'Pull genre' for this genre to populate the cache."
                ),
                confidence="low",
            )
        records = _read_jsonl(path)
        sources.append(path)
    else:
        # Fall back to the global top-100-in-2-weeks pull
        path = _latest_pull(vault_dir, "steamspy", "top100in2weeks")
        if path is None:
            return SteamAnalystResult(
                answer=(
                    "No cached Steam data. Open the Steam Market tab and "
                    "click 'Pull top sellers' to populate the cache, "
                    "then ask again."
                ),
                confidence="low",
            )
        records = _read_jsonl(path)
        sources.append(path)

    if not records:
        return SteamAnalystResult(
            error=f"cached pull {sources[0].name} parsed to zero records",
            confidence="low",
        )

    # ── Augment with SteamCharts if available (gives current ccu) ──
    sc_path = _latest_pull(vault_dir, "steamcharts", "top")
    if sc_path is not None:
        sc_records = _read_jsonl(sc_path)
        by_appid = {r.get("appid"): r for r in sc_records}
        for r in records:
            sc = by_appid.get(r.get("appid"))
            if sc and isinstance(sc.get("current"), (int, float)):
                # Don't clobber SteamSpy's players_2weeks; add ccu
                r.setdefault("ccu", sc["current"])
        sources.append(sc_path)

    # ── Decide which computation ──
    out_lines: List[str] = []
    computed: Dict[str, Any] = {}

    want_revenue = _looks_like_revenue_question(question_l)
    want_top     = _looks_like_top_question(question_l) or not want_revenue

    if want_top:
        top = _compute_top(records, k=10)
        out_lines.append(_format_top_block(
            top,
            f"Top by current/recent players"
            + (f" — {genre_hint}" if genre_hint else ""),
        ))
        computed["top_n"] = len(top)

    if want_revenue:
        band = _compute_revenue_band(records)
        if band:
            out_lines.append(
                f"Estimated revenue distribution"
                + (f" ({genre_hint})" if genre_hint else "")
                + f" — n={band['n']}, "
                + f"median ≈ ${band['median']:,.0f}, "
                + f"p25 ≈ ${band['p25']:,.0f}, "
                + f"p75 ≈ ${band['p75']:,.0f}"
            )
            computed.update({
                "rev_n": band["n"],
                "rev_p25": band["p25"],
                "rev_median": band["median"],
                "rev_p75": band["p75"],
            })
        else:
            out_lines.append(
                "Revenue estimate unavailable for this slice "
                "(missing owners or initialprice)."
            )

    return SteamAnalystResult(
        answer="\n".join(out_lines),
        confidence="medium" if sources else "low",
        computed_values=computed,
        sources=sources,
    )
