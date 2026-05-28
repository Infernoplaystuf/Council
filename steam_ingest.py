"""
steam_ingest.py — pull Steam market data into the local cache.

Strategy: "Both with fallback" — when the user has set a Steam Web
API key in onboarding, prefer the official API; otherwise fall back
to SteamSpy (free, anonymous). SteamCharts is scraped separately
for concurrent-player snapshots — it always supplements the other
source rather than replacing it.

Output: JSONL files under ``vault/steam/`` — one per source per
pull — like ``vault/steam/steamspy_top100in2weeks_2026-05-27.jsonl``.

That folder is in ``conversation_logger.PROTECTED_SUBDIRS`` so the
council never reads the raw rows directly. ``steam_analyst`` reads
the cache and produces deterministic answers with citations.

Network policy:
  • All HTTP via urllib (stdlib — no requests dependency)
  • Conservative timeouts (10s per request, 30s per pull)
  • Hard-coded user agent identifying as Anvil
  • Strict opt-in is the caller's responsibility — this module
    does NOT pop confirmation dialogs; the UI does, and only
    invokes these functions once the user has agreed
  • No retries beyond the per-call urllib default — if SteamSpy
    is being slow, write what we got and let the user re-pull
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


STEAM_CACHE_SUBDIR = "steam"
ANVIL_UA = "Anvil/0.1 (+https://github.com/Infernoplaystuf/Council)"
HTTP_TIMEOUT = 10.0


@dataclass
class IngestResult:
    """Summary of one ingestion run for the user-facing manifest."""
    source:        str               # "steamspy" | "steamcharts" | "swa"
    request:       str = ""          # the SteamSpy endpoint / SWA action
    ts:            str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    )
    files_written: List[Path] = field(default_factory=list)
    record_count:  int = 0
    error:         str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.record_count > 0


# ============================================================
# Cache layout
# ============================================================

def steam_cache_dir(vault_dir: Any) -> Path:
    p = Path(vault_dir).expanduser().resolve() / STEAM_CACHE_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_path(vault_dir: Any, source: str, slug: str) -> Path:
    """Date-stamped filename so repeat pulls don't overwrite history."""
    safe_slug = re.sub(r"[^A-Za-z0-9_\-]+", "_", slug)[:60]
    date = datetime.now().strftime("%Y-%m-%d")
    return steam_cache_dir(vault_dir) / f"{source}_{safe_slug}_{date}.jsonl"


def _write_jsonl(path: Path, records: List[Dict[str, Any]]) -> int:
    """Write ``records`` to ``path``, one JSON object per line. Returns
    the number actually written."""
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            try:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                n += 1
            except Exception:
                continue
    return n


# ============================================================
# HTTP helper
# ============================================================

def _http_get(url: str, *, timeout: float = HTTP_TIMEOUT,
               accept: str = "application/json") -> str:
    """GET ``url`` and return the body as text. Raises on HTTP error /
    network error — callers wrap in try/except and set IngestResult.error.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ANVIL_UA,
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        # Try a few encodings — SteamCharts is HTML and may declare
        # something different from utf-8
        charset = "utf-8"
        try:
            charset = resp.headers.get_content_charset() or "utf-8"
        except Exception:
            pass
        return body.decode(charset, errors="replace")


# ============================================================
# SteamSpy
# ============================================================
# SteamSpy: https://steamspy.com/api.php
#
# Endpoints we care about:
#   ?request=top100in2weeks  — top 100 by recent player count
#   ?request=top100owned     — top 100 by lifetime owners
#   ?request=top100forever   — top 100 by all-time peak
#   ?request=genre&genre=X   — list of games in a genre
#
# Response: JSON object keyed by appid, value is per-game stats
# (name, developer, publisher, owners (string range), price,
# initialprice, discount, players_2weeks, etc.)
#
# Rate limit: SteamSpy asks for "max 1 request/sec for top100 and
# 1 request/60s for full app details". We make at most 1 request
# per call here; bulk patterns are the caller's job.

_STEAMSPY_TOP_REQUESTS = (
    "top100in2weeks", "top100owned", "top100forever",
)


def ingest_steamspy_top(
    vault_dir: Any,
    request: str = "top100in2weeks",
) -> IngestResult:
    """Pull a SteamSpy top-100 endpoint. ``request`` must be one of
    ``top100in2weeks``, ``top100owned``, ``top100forever``.
    """
    if request not in _STEAMSPY_TOP_REQUESTS:
        return IngestResult(
            source="steamspy", request=request,
            error=f"unknown SteamSpy top request: {request!r}",
        )
    url = f"https://steamspy.com/api.php?request={request}"
    try:
        body = _http_get(url)
    except Exception as exc:
        return IngestResult(source="steamspy", request=request,
                            error=f"http error: {exc!r}")
    try:
        data = json.loads(body)
    except Exception as exc:
        return IngestResult(source="steamspy", request=request,
                            error=f"json parse error: {exc!r}")
    if not isinstance(data, dict):
        return IngestResult(source="steamspy", request=request,
                            error=f"unexpected payload shape: {type(data).__name__}")
    # Convert to a flat list of records (appid as a field)
    records: List[Dict[str, Any]] = []
    for appid_str, entry in data.items():
        if not isinstance(entry, dict):
            continue
        try:
            appid = int(appid_str)
        except Exception:
            appid = entry.get("appid", 0)
        r = dict(entry)
        r["appid"] = appid
        r["_source"] = "steamspy"
        r["_request"] = request
        records.append(r)
    if not records:
        return IngestResult(source="steamspy", request=request,
                            error="empty response from SteamSpy")
    path = _cache_path(vault_dir, "steamspy", request)
    written = _write_jsonl(path, records)
    return IngestResult(
        source="steamspy", request=request,
        files_written=[path], record_count=written,
    )


def ingest_steamspy_genre(
    vault_dir: Any,
    genre: str,
) -> IngestResult:
    """Pull SteamSpy's genre listing. ``genre`` is a free-text genre
    name like "Puzzle", "Action", "Strategy" — SteamSpy is fairly
    forgiving."""
    if not genre.strip():
        return IngestResult(source="steamspy", request="genre",
                            error="genre is required")
    qs = urllib.parse.urlencode({"request": "genre", "genre": genre})
    url = f"https://steamspy.com/api.php?{qs}"
    try:
        body = _http_get(url)
    except Exception as exc:
        return IngestResult(source="steamspy", request="genre",
                            error=f"http error: {exc!r}")
    try:
        data = json.loads(body)
    except Exception as exc:
        return IngestResult(source="steamspy", request="genre",
                            error=f"json parse error: {exc!r}")
    if not isinstance(data, dict) or not data:
        return IngestResult(source="steamspy", request="genre",
                            error=f"empty / unexpected payload for genre {genre!r}")
    records: List[Dict[str, Any]] = []
    for appid_str, entry in data.items():
        if not isinstance(entry, dict):
            continue
        try:
            appid = int(appid_str)
        except Exception:
            appid = entry.get("appid", 0)
        r = dict(entry)
        r["appid"] = appid
        r["_source"] = "steamspy"
        r["_request"] = f"genre/{genre}"
        records.append(r)
    path = _cache_path(vault_dir, "steamspy", f"genre_{genre}")
    written = _write_jsonl(path, records)
    return IngestResult(
        source="steamspy", request=f"genre/{genre}",
        files_written=[path], record_count=written,
    )


# ============================================================
# SteamCharts (scraping)
# ============================================================
# We pull steamcharts.com/top — an HTML table of the current top
# concurrent-player games. Brittle by definition; the parser catches
# layout changes and writes whatever it got.

_SC_ROW_RE = re.compile(
    r'<tr id="app-(\d+)"[^>]*>'
    r'.*?<a [^>]*>(.*?)</a>'                         # game name
    r'.*?<td class="num"[^>]*>([\d,\.]+)</td>'        # current
    r'.*?<td class="num period-col gain"[^>]*>(.*?)</td>'   # gain
    r'.*?<td class="num gain"[^>]*>(.*?)</td>',           # peak (24h)
    re.DOTALL,
)


def ingest_steamcharts_top(vault_dir: Any) -> IngestResult:
    """Scrape the current SteamCharts top-games table. Best-effort;
    writes whatever rows the regex matched."""
    url = "https://steamcharts.com/top"
    try:
        body = _http_get(url, accept="text/html")
    except Exception as exc:
        return IngestResult(source="steamcharts", request="top",
                            error=f"http error: {exc!r}")
    records: List[Dict[str, Any]] = []
    for m in _SC_ROW_RE.finditer(body):
        appid_s, name_html, current_s, gain_s, peak_s = m.groups()
        try:
            appid = int(appid_s)
        except Exception:
            continue
        # Strip HTML tags from the name field
        name = re.sub(r"<[^>]+>", "", name_html).strip()

        def _num(s):
            s = re.sub(r"[^\d.\-]+", "", s or "")
            try:
                return float(s) if "." in s else int(s)
            except Exception:
                return None

        records.append({
            "appid":    appid,
            "name":     name,
            "current":  _num(current_s),
            "gain_pct": (gain_s or "").strip(),
            "peak_24h": _num(peak_s),
            "_source":  "steamcharts",
            "_request": "top",
        })
    if not records:
        return IngestResult(
            source="steamcharts", request="top",
            error="SteamCharts top page parsed to zero rows — "
                  "the HTML layout may have changed.",
        )
    path = _cache_path(vault_dir, "steamcharts", "top")
    written = _write_jsonl(path, records)
    return IngestResult(
        source="steamcharts", request="top",
        files_written=[path], record_count=written,
    )


# ============================================================
# Steam Web API (needs user-provided key)
# ============================================================
# Most genuinely useful Web API calls (ownership, achievements) are
# per-user and not what we want. For market data we mainly use:
#   ISteamApps/GetAppList — the master id→name list (no key needed)
#   IStoreService/GetAppList — paged with optional filters (key needed)
#
# For now we expose just GetAppList (no key) so the user has a
# canonical mapping table for everything else.

def ingest_swa_app_list(
    vault_dir: Any,
    *,
    web_api_key: str = "",
) -> IngestResult:
    """Pull the public Steam ISteamApps/GetAppList endpoint.

    No key needed for this endpoint. Returned cache is the canonical
    appid → name mapping that the analyst can use to humanise rows
    from SteamSpy / SteamCharts.
    """
    url = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
    try:
        body = _http_get(url)
    except Exception as exc:
        return IngestResult(source="swa", request="GetAppList",
                            error=f"http error: {exc!r}")
    try:
        data = json.loads(body)
    except Exception as exc:
        return IngestResult(source="swa", request="GetAppList",
                            error=f"json parse error: {exc!r}")
    apps = (
        data.get("applist", {}).get("apps", [])
        if isinstance(data, dict) else []
    )
    if not apps:
        return IngestResult(source="swa", request="GetAppList",
                            error="empty applist")
    records = [
        {"appid": a.get("appid"), "name": a.get("name"),
         "_source": "swa", "_request": "GetAppList"}
        for a in apps if a.get("appid") is not None
    ]
    path = _cache_path(vault_dir, "swa", "GetAppList")
    written = _write_jsonl(path, records)
    return IngestResult(
        source="swa", request="GetAppList",
        files_written=[path], record_count=written,
    )


# ============================================================
# Combined "Both with fallback" helper
# ============================================================

def ingest_top_sellers(
    vault_dir: Any,
    *,
    web_api_key: str = "",
) -> List[IngestResult]:
    """Pull a sensible default set of top-sellers data with fallback:

      1. SteamSpy top100in2weeks (always — fast, free, no key)
      2. SteamCharts top (always — supplements with current CCU)
      3. SWA GetAppList if web_api_key was provided OR no key needed

    Returns the list of IngestResult so the UI can render the per-
    source outcome.
    """
    results: List[IngestResult] = []
    results.append(ingest_steamspy_top(vault_dir, "top100in2weeks"))
    results.append(ingest_steamcharts_top(vault_dir))
    # SWA GetAppList is keyless; pull regardless of key
    results.append(ingest_swa_app_list(vault_dir, web_api_key=web_api_key))
    return results
