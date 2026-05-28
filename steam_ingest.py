"""
steam_ingest.py — pull Steam market data into the local cache.

Strategy: "Both with fallback" — when the user has set a Steam Web API
key in onboarding, prefer the official API; otherwise fall back to
SteamSpy (free, anonymous) plus SteamCharts scraping for concurrent-
player numbers.

Output: JSONL files under ``vault/steam/`` — one per source per pull
session — like ``vault/steam/steamspy_top100_2026-05-27.jsonl``.

That folder is in ``conversation_logger.PROTECTED_SUBDIRS`` so the
council never reads the raw rows directly. The Steam Market Analyst
in ``steam_analyst.py`` is the only thing that opens these files and
it surfaces results with citations rather than passing rows up to
the model.

PHASE A: stub. PHASE D will land:

  • SteamSpy client (https://steamspy.com/api.php — free, rate-limited)
  • SteamCharts scrape (HTML, brittle — wrap in a per-pull try/except)
  • Steam Web API client (https://api.steampowered.com — needs key)
  • Source-precedence: if SWA key set, try SWA first; on failure or
    missing key, fall back to SteamSpy. SteamCharts always
    supplements for concurrent-player snapshots.
  • Per-pull manifest with ts + source + record count for the analyst
  • Strict opt-in: the first pull pops a confirmation dialog
    ("Anvil is about to make N HTTP requests to <hosts>. Continue?")

Non-goals: real-time pulls during deliberation. Pulls are explicit
and produce cached files; the analyst always reads from cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# Where the cache lives — must match the entry in PROTECTED_SUBDIRS.
STEAM_CACHE_SUBDIR = "steam"


@dataclass
class IngestResult:
    """Summary of one ingestion run for the user-facing manifest."""
    source:        str               # "steamspy" | "steamcharts" | "swa"
    ts:            str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    )
    files_written: List[Path] = field(default_factory=list)
    record_count:  int = 0
    error:         str = ""


# ============================================================
# Public API — phase D
# ============================================================

def steam_cache_dir(vault_dir: Any) -> Path:
    """Return ``<vault>/steam/`` and ensure it exists."""
    p = Path(vault_dir).expanduser().resolve() / STEAM_CACHE_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def ingest_top_sellers(
    vault_dir: Any,
    *,
    web_api_key: str = "",
    limit: int = 100,
) -> IngestResult:
    """Pull top-sellers list. Prefer Steam Web API when key is set,
    fall back to SteamSpy otherwise.

    PHASE D. Returns an empty result for now.
    """
    return IngestResult(
        source="steamspy" if not web_api_key else "swa",
        error="ingest_top_sellers: stub — phase D",
    )


def ingest_by_genre(
    vault_dir: Any,
    genre: str,
    *,
    web_api_key: str = "",
) -> IngestResult:
    """Pull genre-specific market data. PHASE D."""
    return IngestResult(
        source="steamspy" if not web_api_key else "swa",
        error="ingest_by_genre: stub — phase D",
    )


def ingest_concurrent_players(
    vault_dir: Any,
    app_ids: List[int],
) -> IngestResult:
    """Pull current concurrent-player snapshots from SteamCharts.

    Scraping HTML is brittle by nature; this function must catch
    layout changes and write whatever it could parse, even partial.
    PHASE D.
    """
    return IngestResult(
        source="steamcharts",
        error="ingest_concurrent_players: stub — phase D",
    )
