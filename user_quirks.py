"""
user_quirks.py — a personality layer that learns the USER, gated
against self-poisoning.

The Council's existing memories learn the JOB (RoleMemoryManager: task
lessons per role) and the PROJECT (shared observer-written facts). This
module learns the USER — durable preferences and habits like "prefers
tables over prose", "works in materials science", "wants units in SI",
"dislikes hedging" — and injects them into every personality's prompt
as a USER PROFILE block.

The poisoning problem, and the two gates that prevent it
--------------------------------------------------------
A profile built from one or two conversations is worse than none: a
single sarcastic session, a borrowed laptop, or one weird experiment
would permanently skew every future answer. Two independent gates keep
the profile honest:

  GATE 1 — DORMANCY (global). The profile stays completely inactive —
  nothing is injected anywhere — until observations have accumulated
  from at least COUNCIL_QUIRKS_MIN_SESSIONS distinct sessions
  (default 10). Observation never stops; injection waits for volume.

  GATE 2 — CORROBORATION (per quirk). Even once active, a quirk enters
  the compiled profile only if it was observed in at least
  COUNCIL_QUIRKS_MIN_SESSIONS_PER_QUIRK distinct sessions (default 3).
  Session-distinct, not message-distinct: repeating yourself five times
  in one conversation is one data point, not five.

Additional hygiene:
  • The compiled profile is capped (bullets per category + total) so
    the prompt cost stays bounded forever.
  • Extraction asks only for DURABLE preferences; the prompt explicitly
    excludes one-off requests and topic-of-the-day content.
  • The whole layer is observational data + text rendering. It never
    executes anything, and clear() wipes it instantly.
  • COUNCIL_QUIRKS_ENABLE=0 turns the entire layer off.

Explicit bypass (apply vs observe)
----------------------------------
Sometimes the user's NORMAL preferences are wrong for the CURRENT ask
(they usually want terse tables, today they want a flowing essay).
Three escalating overrides:
  1. Just ask — the injected block ends with "Honour them unless the
     current request says otherwise", so an explicit request wins at
     the prompt level with no toggling.
  2. COUNCIL_QUIRKS_APPLY=0 (GUI: the "👤 Profile" toolbar checkbox) —
     stops INJECTION on the next message while observation keeps
     running. The profile keeps maturing; nothing learned is lost.
     Checked at respond() time in council_engine, not here.
  3. COUNCIL_QUIRKS_ENABLE=0 — kills the whole layer, observation
     included, and wipes the injected block on the next update.

Storage: ~/.council/vault/.user_quirks.jsonl (append-only JSONL, same
pattern as the agent audit logs; honours COUNCIL_VAULT_ROOT).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import agent_logs  # _vault_root, _now_ms, _iter_jsonl — same store conventions

_LOG = logging.getLogger("user_quirks")

# Fixed taxonomy — free-form categories would defeat clustering.
CATEGORIES = ("tone", "format", "domain", "workflow", "terminology",
              "correction")

_MAX_OBSERVATIONS_PER_TURN = 3
_MAX_BULLETS_PER_CATEGORY = 4
_MAX_TOTAL_BULLETS = 12
_QUIRK_TEXT_CAP = 160


def _enabled() -> bool:
    return os.environ.get("COUNCIL_QUIRKS_ENABLE", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _min_sessions() -> int:
    try:
        return max(1, int(os.environ.get("COUNCIL_QUIRKS_MIN_SESSIONS", "10")))
    except ValueError:
        return 10


def _min_sessions_per_quirk() -> int:
    try:
        return max(1, int(os.environ.get(
            "COUNCIL_QUIRKS_MIN_SESSIONS_PER_QUIRK", "3")))
    except ValueError:
        return 3


# ============================================================
# Observation store
# ============================================================

class UserQuirksLog:
    """Append-only JSONL of quirk observations.

    Record schema:
        {"ts": int_ms, "session": str, "category": str, "text": str}
    """

    DEFAULT_FILENAME = ".user_quirks.jsonl"

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else (
            agent_logs._vault_root() / self.DEFAULT_FILENAME)
        self._lock = threading.Lock()

    @classmethod
    def default(cls) -> "UserQuirksLog":
        return cls()

    def append(self, *, session: str, category: str, text: str) -> Optional[Dict[str, Any]]:
        category = str(category or "").strip().lower()
        text = " ".join(str(text or "").split())[:_QUIRK_TEXT_CAP]
        if category not in CATEGORIES or not text:
            return None
        rec = {
            "ts":       agent_logs._now_ms(),
            "session":  str(session or "unknown"),
            "category": category,
            "text":     text,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(agent_logs._iter_jsonl(self.path))

    def clear(self) -> bool:
        """Wipe every observation. The user's escape hatch."""
        with self._lock:
            try:
                if self.path.exists():
                    self.path.unlink()
                return True
            except OSError:
                return False


# ============================================================
# Extraction (one small model call per deliberation)
# ============================================================

_EXTRACT_PROMPT = """\
You observe a user's message to a local AI assistant and extract DURABLE \
user preferences — things that will still be true next month.

Output a JSON array (possibly empty) of at most {cap} objects:
  [{{"category": "<one of: tone|format|domain|workflow|terminology|correction>", \
"text": "<one short sentence>"}}]

STRICT RULES:
- Only durable, repeatable preferences or traits. NOT the topic of this \
message, NOT one-off requests, NOT facts about the data being discussed.
- "correction" = the user corrected the assistant's style or approach.
- When in doubt, output fewer. An empty array [] is a good answer.
- Output ONLY the JSON array.

USER MESSAGE:
{user_text}
"""


def extract_quirks(user_text: str,
                   llm_call: Callable[[str], str],
                   *,
                   cap: int = _MAX_OBSERVATIONS_PER_TURN
                   ) -> List[Dict[str, str]]:
    """Ask the LOCAL model for durable-preference observations in the
    user's message. Defensive parse; returns [] on any failure."""
    if not (user_text or "").strip():
        return []
    try:
        reply = llm_call(_EXTRACT_PROMPT.format(
            cap=cap, user_text=user_text.strip()[:2000]))
    except Exception as exc:
        _LOG.debug("quirk extraction call failed: %r", exc)
        return []
    # Find the first JSON array in the reply.
    m = re.search(r"\[.*?\]", reply or "", re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    out: List[Dict[str, str]] = []
    if isinstance(arr, list):
        for item in arr[:cap]:
            if (isinstance(item, dict)
                    and str(item.get("category", "")).strip().lower() in CATEGORIES
                    and str(item.get("text", "")).strip()):
                out.append({
                    "category": str(item["category"]).strip().lower(),
                    "text":     str(item["text"]).strip()[:_QUIRK_TEXT_CAP],
                })
    return out


# ============================================================
# Clustering + compilation (pure, deterministic — no model)
# ============================================================

_WORD_RE = re.compile(r"[a-z0-9]+")
# Generous stopword set: extraction phrasings of the SAME quirk vary
# ("prefers tables over prose" / "prefers tables instead of prose"), so
# we strip the glue words and match on content tokens only.
_STOPWORDS = frozenset(
    "the a an of to in for and or with on as is are be been was were "
    "user prefers prefer likes like wants want uses use asks ask asked "
    "not no than rather instead over under always usually often very "
    "more less most when while also it this that they them their into "
    "from by at should would tends tend".split())


def _tokens(text: str) -> frozenset:
    return frozenset(w for w in _WORD_RE.findall(text.lower())
                     if w not in _STOPWORDS)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_observations(records: List[Dict[str, Any]],
                         *,
                         similarity: float = 0.5
                         ) -> List[Dict[str, Any]]:
    """Greedy within-category clustering by token Jaccard. Returns one
    dict per cluster: {category, text (longest variant), sessions
    (distinct set), count, last_ts}. Deterministic — record order is
    file order, which is append (time) order."""
    clusters: List[Dict[str, Any]] = []
    for rec in records:
        cat = rec.get("category", "")
        txt = rec.get("text", "")
        toks = _tokens(txt)
        if not toks:
            continue
        home = None
        for c in clusters:
            if c["category"] == cat and _jaccard(c["_tokens"], toks) >= similarity:
                home = c
                break
        if home is None:
            home = {"category": cat, "text": txt, "_tokens": toks,
                    "sessions": set(), "count": 0, "last_ts": 0}
            clusters.append(home)
        home["count"] += 1
        home["sessions"].add(str(rec.get("session", "unknown")))
        ts = int(rec.get("ts", 0))
        if ts >= home["last_ts"]:
            home["last_ts"] = ts
            # Keep the freshest phrasing as the representative text and
            # widen the cluster's token set so drifting rewordings of
            # the same quirk keep matching.
            home["text"] = txt
            home["_tokens"] = home["_tokens"] | toks
    return clusters


def profile_status(log: Optional[UserQuirksLog] = None) -> Dict[str, Any]:
    """Where the profile stands: dormant/active, session progress, and
    how many quirks have cleared the corroboration gate."""
    log = log or UserQuirksLog.default()
    records = log.all()
    sessions = {str(r.get("session", "unknown")) for r in records}
    need = _min_sessions()
    clusters = cluster_observations(records)
    confirmed = [c for c in clusters
                 if len(c["sessions"]) >= _min_sessions_per_quirk()]
    active = _enabled() and len(sessions) >= need
    return {
        "enabled":            _enabled(),
        "active":             active,
        "sessions_observed":  len(sessions),
        "sessions_required":  need,
        "observations":       len(records),
        "quirks_candidate":   len(clusters),
        "quirks_confirmed":   len(confirmed),
    }


def compile_profile(log: Optional[UserQuirksLog] = None) -> str:
    """Render the ACTIVE profile as a markdown block, or "" while
    dormant / disabled / nothing confirmed. Both anti-poisoning gates
    are enforced here, so callers can't accidentally bypass them."""
    if not _enabled():
        return ""
    log = log or UserQuirksLog.default()
    records = log.all()
    sessions = {str(r.get("session", "unknown")) for r in records}
    if len(sessions) < _min_sessions():           # GATE 1 — dormancy
        return ""
    clusters = cluster_observations(records)
    need_k = _min_sessions_per_quirk()
    confirmed = [c for c in clusters
                 if len(c["sessions"]) >= need_k]  # GATE 2 — corroboration
    if not confirmed:
        return ""
    # Strongest first: corroborating sessions, then recency.
    confirmed.sort(key=lambda c: (-len(c["sessions"]), -c["last_ts"]))

    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    total = 0
    for c in confirmed:
        bucket = by_cat.setdefault(c["category"], [])
        if len(bucket) >= _MAX_BULLETS_PER_CATEGORY or total >= _MAX_TOTAL_BULLETS:
            continue
        bucket.append(c)
        total += 1

    lines: List[str] = []
    for cat in CATEGORIES:
        for c in by_cat.get(cat, []):
            lines.append(f"- [{cat}] {c['text']} "
                         f"(seen in {len(c['sessions'])} sessions)")
    if not lines:
        return ""
    # ASCII-only on purpose: this block lands in model prompts and may
    # be echoed by debug prints on cp1252 Windows consoles.
    return ("Durable user preferences, each independently confirmed "
            f"in {need_k}+ sessions. Honour them unless the current "
            "request says otherwise.\n" + "\n".join(lines))


# ============================================================
# Per-deliberation hook (called from the GUI's memory-update path)
# ============================================================

def update_after_deliberation(user_text: str,
                              session: str,
                              llm_call: Callable[[str], str],
                              *,
                              memory_manager=None,
                              log: Optional[UserQuirksLog] = None
                              ) -> Dict[str, Any]:
    """Observe → store → (when both gates pass) recompile the injected
    profile. Returns profile_status() plus 'observed_now'.

    ``memory_manager`` is a council_engine.RoleMemoryManager; when the
    profile is active the compiled block is written under the special
    user-profile key so every personality picks it up on its next
    respond() call. While dormant the key is kept EMPTY — observation
    without influence."""
    log = log or UserQuirksLog.default()
    observed = 0
    if _enabled():
        for ob in extract_quirks(user_text, llm_call):
            if log.append(session=session,
                          category=ob["category"], text=ob["text"]):
                observed += 1

    status = profile_status(log)
    status["observed_now"] = observed

    if memory_manager is not None:
        try:
            import council_engine as _ce
            key = _ce._USER_PROFILE_KEY
            compiled = compile_profile(log)   # "" while gated → key stays empty
            if (memory_manager.read(key) or "").strip() != compiled.strip():
                memory_manager.update(key, compiled)
        except Exception as exc:
            _LOG.debug("profile write skipped: %r", exc)
    return status
