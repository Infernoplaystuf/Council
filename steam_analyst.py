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
specialist:

    answer = answer_question(question, vault_dir)
    # → SteamAnalystResult(answer_text, sources, computed_values)

PHASE A: stubs. PHASE D will land the genre stats, comparable-titles
lookup, revenue-band estimator, and the prompt template the analyst
uses to surface a citation-attached answer block for injection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


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
        prompt injector. Mirrors ``vault_analyst.format_result_for_prompt``.
        PHASE D."""
        return f"[STEAM ANALYST RESULT — stub]\n{self.answer or self.error or '(empty)'}"


# ============================================================
# Public API — phase D
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


def answer_question(
    question: str,
    vault_dir: Any,
    *,
    goal: str = "",
) -> SteamAnalystResult:
    """Route a natural-language market question to the right computation.

    Examples that route to specific computations (all phase D):
      • "top selling [genre] games" → genre filter + revenue sort
      • "median revenue for [genre]" → distribution stat
      • "comparable titles to [game]" → tag + price + audience match
      • "what's trending in [genre]" → 7-day delta on concurrent players

    Falls back to a structured "no answer" result rather than letting
    the council invent one. PHASE D.
    """
    return SteamAnalystResult(
        error="answer_question: stub — phase D",
        confidence="low",
    )
