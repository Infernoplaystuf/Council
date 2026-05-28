# ============================================================
# specialists.py  —  Personal Specialists registry
# ============================================================
# A Personal Specialist is a *named lens* on the shared vault. Each
# one is just config — name, icon, domain keywords, and a system-
# prompt overlay that tells the underlying personality how to think
# about the question. There is exactly one knowledge pool (the vault).
# Specialists do NOT have separate data folders; that would silo
# information that needs to flow across domains.
#
# Cross-domain queries are handled by the existing Council deliberation:
# if a query matches multiple specialists' keywords, all of them are
# summoned in parallel and the Judge synthesises their drafts.
# ============================================================

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# Data model
# ============================================================

@dataclass
class Specialist:
    """
    A single Personal Specialist. Stored as one entry in
    vault/specialists.json. No separate file storage — the vault
    is the shared knowledge pool.
    """
    id: str                                  # url-safe slug, e.g. "sales"
    name: str                                # "Sales Specialist"
    icon: str = "🎓"                         # emoji shown in lists
    description: str = ""                    # one-line summary
    domain_keywords: List[str] = field(default_factory=list)
    system_prompt_overlay: str = ""          # extra context injected per query
    base_personality: str = "writer"         # which existing role wears the lens
    enabled: bool = True
    created_at: str = ""                     # ISO timestamp
    updated_at: str = ""

    # ---- Serialisation -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Specialist":
        # Tolerate unknown keys silently — config files outlive code shape
        valid = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in valid})

    # ---- Derived helpers ---------------------------------------------------

    def matches(self, query: str) -> int:
        """
        Return the number of distinct domain keywords that appear in `query`
        (case-insensitive). 0 means no match → not summoned automatically.
        """
        if not self.enabled or not self.domain_keywords:
            return 0
        ql = query.lower()
        return sum(1 for kw in self.domain_keywords
                   if kw and kw.lower() in ql)

    def context_block(self) -> str:
        """
        Format the specialist's system prompt + identity for injection into
        the underlying personality's `extra_context`. Kept short — the goal
        is to nudge the lens, not replace the personality.
        """
        parts = [
            f"PERSONAL SPECIALIST CONSULTED: {self.icon} {self.name}",
            f"Expertise: {self.description}" if self.description else "",
            "",
            "Lens to apply when answering:",
            self.system_prompt_overlay.strip()
                if self.system_prompt_overlay else "(no overlay configured)",
        ]
        return "\n".join(p for p in parts if p is not None)


# ============================================================
# Registry
# ============================================================

class SpecialistRegistry:
    """
    Loads, persists, and queries the list of specialists.
    Stored at vault/specialists.json. First read seeds the file with
    the default specialists if it doesn't exist yet.
    """

    def __init__(self, vault_dir: Path):
        self.vault_dir = vault_dir
        self.path = vault_dir / "specialists.json"
        self._items: List[Specialist] = []
        self._load_or_seed()

    # ---- Load / save -------------------------------------------------------

    def _load_or_seed(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._items = [Specialist.from_dict(d) for d in raw]
                return
            except Exception as e:
                print(f"[Specialists] Failed to parse {self.path}: {e}")
                # Fall through to seed defaults

        # No file or unreadable — seed with sensible defaults
        self._items = list(default_specialists())
        self.save()

    def save(self) -> None:
        try:
            self.vault_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps([s.to_dict() for s in self._items],
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[Specialists] Save failed: {e}")

    # ---- CRUD --------------------------------------------------------------

    def all(self, *, enabled_only: bool = False) -> List[Specialist]:
        if enabled_only:
            return [s for s in self._items if s.enabled]
        return list(self._items)

    def get(self, sid: str) -> Optional[Specialist]:
        return next((s for s in self._items if s.id == sid), None)

    def add(self, spec: Specialist) -> None:
        # Deduplicate by id; replace if exists
        existing = self.get(spec.id)
        now = _now_iso()
        spec.updated_at = now
        if existing is None:
            spec.created_at = now
            self._items.append(spec)
        else:
            spec.created_at = existing.created_at or now
            self._items = [spec if s.id == spec.id else s for s in self._items]
        self.save()

    def remove(self, sid: str) -> bool:
        before = len(self._items)
        self._items = [s for s in self._items if s.id != sid]
        if len(self._items) < before:
            self.save()
            return True
        return False

    # ---- Query matching ----------------------------------------------------

    def match(self, query: str, *, max_specialists: int = 3
              ) -> List[Tuple[Specialist, int]]:
        """
        Return specialists ranked by keyword-match strength. Ties broken
        by registry order. Capped to `max_specialists` so a query that
        loosely overlaps with everything doesn't pull in the whole list.
        """
        scored = [(s, s.matches(query)) for s in self._items if s.enabled]
        scored = [(s, n) for (s, n) in scored if n > 0]
        scored.sort(key=lambda r: r[1], reverse=True)
        return scored[:max_specialists]


# ============================================================
# Defaults
# ============================================================

def default_specialists() -> List[Specialist]:
    """Pre-built specialists shipped with Anvil.

    Game-development focused. The previous data-analyst trio (Sales /
    Inventory / Customer) lived here on Work-Build; on the game-dev
    branch we seed a Game Designer, Genre Analyst, and Steam Market
    Analyst as the default specialist roster.
    Users can edit, disable, or delete freely in the Specialists tab.
    """
    return [
        Specialist(
            id="game_designer",
            name="Game Designer",
            icon="🎮",
            description=(
                "Mechanics, core loops, pacing, balance, level design, "
                "player psychology."
            ),
            domain_keywords=[
                # Mechanics + loops
                "mechanic", "mechanics", "loop", "core loop", "gameplay",
                "balance", "tuning", "difficulty", "pacing",
                # Level + content
                "level", "level design", "boss", "puzzle", "encounter",
                "enemy", "enemies", "weapon", "ability", "skill",
                # Player-side
                "player", "controls", "feel", "juice", "feedback",
                "tutorial", "onboarding", "progression",
                # Game-design vocabulary
                "design", "playtest", "iteration", "prototype",
                "fun", "flow", "challenge", "reward",
            ],
            system_prompt_overlay=(
                "You are a senior game designer. When the user asks "
                "about a mechanic, level, or balance question, work in "
                "concrete terms: numbers, tunable parameters, named "
                "comparable titles. Never hand-wave with 'make it feel "
                "good' — specify what *good* means (target time-to-kill, "
                "damage curves, jump arcs, etc.). Translate every "
                "recommendation into something the user can change in "
                "Godot today: an exported variable, a node tree, a "
                "signal connection, an autoload. When you reference an "
                "existing game by name, make sure it actually exists "
                "and is well-known enough that the user has heard of "
                "it; otherwise say 'a game in the style of …' instead."
            ),
            base_personality="writer",
        ),
        Specialist(
            id="genre_analyst",
            name="Genre Analyst",
            icon="🎭",
            description=(
                "Tropes, conventions, comparable titles, market "
                "positioning — what makes a genre identifiable."
            ),
            domain_keywords=[
                "genre", "sub-genre", "trope", "convention",
                "comparable", "comparables", "comp", "comps",
                "similar to", "like", "positioning", "audience",
                "platformer", "metroidvania", "roguelike", "roguelite",
                "shooter", "fps", "rpg", "jrpg", "puzzle",
                "visual novel", "vn", "deck builder", "deckbuilder",
                "city builder", "survival", "horror", "walking sim",
                "rhythm", "racing", "sports", "strategy", "4x",
                "tactics", "bullet hell", "twin-stick",
            ],
            system_prompt_overlay=(
                "You are a games genre analyst. When asked about a "
                "concept, identify which sub-genre it lands in, name "
                "three to five comparable titles that actually exist "
                "(do not invent fake names — if you're unsure, say "
                "'in the style of X' instead), and call out which "
                "expected conventions of that sub-genre the concept "
                "satisfies, subverts, or omits. End every answer with "
                "one sentence on positioning: who buys this genre and "
                "what makes them choose one game over another."
            ),
            base_personality="writer",
        ),
        Specialist(
            id="steam_market_analyst",
            name="Steam Market Analyst",
            icon="📈",
            description=(
                "Reads cached Steam data to answer market questions "
                "with citations — never invents numbers."
            ),
            domain_keywords=[
                "steam", "steamspy", "steamcharts", "ccu",
                "concurrent players", "owners", "owned",
                "revenue", "sales", "earnings", "market",
                "trending", "top sellers", "top selling",
                "what sells", "what's hot", "what is hot",
                "comparable revenue", "indie revenue",
            ],
            system_prompt_overlay=(
                "You are a Steam-market analyst. ABSOLUTE RULE: never "
                "invent specific numeric values (player counts, owners, "
                "revenue, prices). If a Steam Analyst Result block is "
                "in your context, base your answer on those computed "
                "values and cite them. If no analyst block is present, "
                "tell the user 'I have no fresh cache for this — open "
                "the 📈 Steam Market tab and pull current data' rather "
                "than guessing. When discussing trends, distinguish "
                "between 'currently popular' (CCU snapshot), 'recently "
                "trending' (2-week deltas), and 'all-time strong' "
                "(lifetime owners) — they answer different questions."
            ),
            base_personality="writer",
        ),
    ]


# ============================================================
# Helpers
# ============================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def slugify(name: str) -> str:
    """Turn 'Sales Specialist' → 'sales_specialist' for use as an id."""
    s = name.strip().lower().replace(" ", "_")
    s = _SLUG_RE.sub("", s)
    return s or "specialist"
