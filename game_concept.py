"""
game_concept.py — game-shaped sibling to ``idea_engine.IdeaItem``.

Rather than retarget the existing 1234-line ``IdeaItem`` (video-focused,
shared with `main` and Work-Build), this module ships a parallel
``GameConcept`` schema that has only game-development fields and lives
in its own vault subdir.

Public:

  GameConcept       — dataclass for one concept (title, hook, genre,
                      mechanics, comparable titles, etc.)
  GameConceptStore  — JSON-per-file store under vault/game_concepts/
  brainstorm_concepts(seed, model, n=3) — synchronous council call
                      that returns N concepts parsed from a single
                      structured prompt
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ============================================================
# GameConcept
# ============================================================

# Free-text genre — common values get autocomplete in the UI but the
# field is open. Steam genre taxonomy is broad and overlapping; forcing
# an enum would lose nuance.
COMMON_GENRES = (
    "Platformer", "Metroidvania", "Roguelike", "Top-down Shooter",
    "Twin-stick Shooter", "FPS", "Puzzle", "Puzzle-Platformer",
    "Visual Novel", "Bullet Hell", "Tower Defense", "Deck Builder",
    "RPG", "JRPG", "Action RPG", "Tactics", "Strategy", "4X",
    "Survival", "Crafting", "City Builder", "Management Sim",
    "Walking Sim", "Horror", "Stealth", "Co-op",
    "Couch Co-op", "Asymmetric Multiplayer", "Battle Royale",
    "Sports", "Racing", "Rhythm", "Music",
)

COMMON_PLATFORMS = ("PC (Steam)", "PC (itch)", "Web", "Console", "Mobile")


@dataclass
class GameConcept:
    """One game-development concept."""

    # ---- Identity ----------------------------------------------------
    id:    str   = field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str   = ""

    # ---- Core pitch --------------------------------------------------
    hook:               str = ""           # one-sentence elevator
    premise:            str = ""           # 2–3 sentence setup
    target_audience:    str = ""
    why_it_works:       str = ""

    # ---- Genre / shape -----------------------------------------------
    genre:              str = ""           # primary, free text
    sub_genres:         List[str] = field(default_factory=list)
    platforms:          List[str] = field(default_factory=list)
    art_style:          str = ""           # "pixel", "low-poly 3D", etc.
    engine:             str = "Godot 4"
    play_length:        str = ""           # "60–90 minutes", "20+ hours"
    player_count:       str = "single-player"

    # ---- Mechanics ---------------------------------------------------
    core_loop:          str = ""           # the moment-to-moment loop
    mechanics:          List[str] = field(default_factory=list)
    progression:        str = ""           # what gets harder / unlocks
    win_loss:           str = ""           # how players succeed / fail

    # ---- Market positioning -----------------------------------------
    comparable_titles:  List[str] = field(default_factory=list)
    differentiator:     str = ""           # what sets it apart from comps
    monetization:       str = ""           # premium, F2P, ads, DLC...
    estimated_dev_time: str = ""           # "1 week", "3 months"

    # ---- Generation provenance --------------------------------------
    seed_used:          str = ""
    raw_brainstorm:     str = ""
    generated_at:       str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    generator_model:    str = ""

    # ---- User interaction -------------------------------------------
    rating: int = 0                        # 0 = unrated, 1–5 stars
    status: str = "new"                    # new | saved | archived | prototyped
    notes:  str = ""

    # ---- Derived ----------------------------------------------------

    @property
    def display_title(self) -> str:
        return self.title or self.hook[:60] or f"Concept {self.id}"

    @property
    def status_icon(self) -> str:
        return {
            "new":         "🆕",
            "saved":       "⭐",
            "archived":    "📦",
            "prototyped":  "🛠",
        }.get(self.status, "🆕")

    @property
    def slug(self) -> str:
        """URL-safe slug for use as a project folder name."""
        s = re.sub(r"[^a-z0-9]+", "_",
                   (self.title or self.id).lower()).strip("_")
        return s[:40] or self.id

    # ---- Serialisation ----------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GameConcept":
        valid = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in valid})


# ============================================================
# Store
# ============================================================

class GameConceptStore:
    """JSON-per-concept storage under ``vault/game_concepts/``.

    Mirrors ``idea_engine.IdeaStore`` in shape so the GUI can follow
    the same patterns (light index + on-demand load).
    """

    def __init__(self, vault_dir: Any):
        self.vault_dir = Path(vault_dir).expanduser().resolve()
        self.dir = self.vault_dir / "game_concepts"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.dir / "_index.json"
        self._index: List[Dict[str, Any]] = []
        self._load_index()

    # ---- Index ------------------------------------------------------

    def _load_index(self) -> None:
        if not self._index_path.exists():
            self._index = []
            return
        try:
            self._index = json.loads(
                self._index_path.read_text(encoding="utf-8")
            )
        except Exception:
            self._index = []

    def _save_index(self) -> None:
        try:
            self._index_path.write_text(
                json.dumps(self._index, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"[GameConceptStore] save_index failed: {exc!r}")

    # ---- CRUD -------------------------------------------------------

    def save(self, concept: GameConcept) -> Path:
        fname = f"{concept.generated_at[:10]}_{concept.id}.json".replace(":", "-")
        fpath = self.dir / fname
        try:
            fpath.write_text(
                json.dumps(concept.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"[GameConceptStore] save failed: {exc!r}")
            return fpath
        # Update index (newest first)
        entry = {
            "id":           concept.id,
            "title":        concept.display_title,
            "genre":        concept.genre,
            "status":       concept.status,
            "rating":       concept.rating,
            "generated_at": concept.generated_at,
            "file":         fname,
        }
        self._index = [e for e in self._index if e.get("id") != concept.id]
        self._index.insert(0, entry)
        self._save_index()
        return fpath

    def load(self, concept_id: str) -> Optional[GameConcept]:
        for entry in self._index:
            if entry.get("id") == concept_id:
                fpath = self.dir / entry["file"]
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    return GameConcept.from_dict(data)
                except Exception:
                    return None
        return None

    def delete(self, concept_id: str) -> bool:
        before = len(self._index)
        for entry in list(self._index):
            if entry.get("id") == concept_id:
                try:
                    (self.dir / entry["file"]).unlink(missing_ok=True)
                except Exception:
                    pass
                self._index.remove(entry)
        if len(self._index) < before:
            self._save_index()
            return True
        return False

    def list_index(self) -> List[Dict[str, Any]]:
        return list(self._index)

    def count(self) -> int:
        return len(self._index)


# ============================================================
# Brainstorming
# ============================================================

_BRAINSTORM_SYSTEM = (
    "You are a game-design ideation engine. The user gives you a seed "
    "(genre, vibe, mechanic, or constraint). You output STRICTLY JSON: "
    "a list of distinct game concepts, no prose, no markdown fence."
)

_BRAINSTORM_INSTRUCTIONS = """\
Generate {n} distinct game concepts based on the seed below. Each
concept must be a fresh angle — do not repeat title, hook, or core
mechanic across concepts. Aim for things a small team could ship in
Godot in under six months.

For each concept include these fields:
  title             — short, evocative
  hook              — single sentence elevator pitch
  premise           — 2–3 sentence setup
  genre             — primary genre
  sub_genres        — array of 0–3 modifiers
  core_loop         — what the player does minute-to-minute
  mechanics         — array of 3–6 specific mechanics
  art_style         — short phrase ("pixel", "low-poly 3D", "ink-wash 2D")
  play_length       — "60 min" / "5–10 hr" / etc.
  player_count      — "single-player", "co-op (2)", etc.
  target_audience   — short audience description
  comparable_titles — array of 1–4 existing Steam titles
  differentiator    — what sets this apart from the comps
  why_it_works      — 1–2 sentence pitch for why this should exist

Output a JSON array of length {n}. NO other text. NO markdown.

SEED:
{seed}

JSON OUTPUT:
"""


_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_brainstorm_output(text: str) -> List[Dict[str, Any]]:
    """Extract a JSON array of concept dicts from a model response.

    Tolerant: tries the whole response first, then a regex-extracted
    JSON-array substring. Returns ``[]`` on any failure rather than
    raising so the caller can surface a "no concepts produced" line
    instead of a crash.
    """
    text = (text or "").strip()
    if not text:
        return []
    # Strip optional markdown fence
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    candidates = [text]
    m = _JSON_ARRAY_RE.search(text)
    if m:
        candidates.append(m.group(0))
    for raw in candidates:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except Exception:
            continue
    return []


def brainstorm_concepts(
    seed: str,
    model: Any,
    n: int = 3,
    *,
    extra_context: str = "",
) -> List[GameConcept]:
    """Generate ``n`` ``GameConcept`` objects from a free-text seed.

    ``model`` must expose ``respond(prompt, max_tokens=...)`` — i.e. a
    ``council_engine.PersonalityModel``. Returns an empty list if the
    model returned no parseable JSON; caller surfaces that to the user.
    """
    n = max(1, min(int(n), 8))
    prompt = (
        _BRAINSTORM_SYSTEM + "\n\n"
        + _BRAINSTORM_INSTRUCTIONS.format(n=n, seed=seed.strip() or "(no seed)")
    )
    if extra_context:
        prompt = extra_context.strip() + "\n\n" + prompt

    try:
        raw = model.respond(prompt, max_tokens=2400)
    except Exception as exc:
        print(f"[brainstorm_concepts] model.respond failed: {exc!r}")
        return []

    dicts = _parse_brainstorm_output(raw)
    out: List[GameConcept] = []
    for d in dicts:
        try:
            # The model often returns ``mechanics`` as a list of strings
            # or a single string — normalise to list-of-strings.
            for k in ("sub_genres", "mechanics", "comparable_titles", "platforms"):
                v = d.get(k)
                if isinstance(v, str):
                    d[k] = [s.strip() for s in re.split(r"[,;]", v) if s.strip()]
                elif v is None:
                    d[k] = []
            concept = GameConcept.from_dict(d)
            concept.raw_brainstorm = (raw or "")[:2000]
            concept.seed_used = seed.strip()
            concept.generator_model = getattr(model, "name", "")
            out.append(concept)
        except Exception as exc:
            print(f"[brainstorm_concepts] concept parse failed: {exc!r}")
            continue
    return out
