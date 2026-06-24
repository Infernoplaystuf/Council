"""
gdd_parser.py — markdown Game Design Document → structured ``GDD``.

The output is the input to ``gdd_planner.py`` (commit 2), which turns
the structured data into a per-file Plan, which is then executed by
``gdd_builder.py`` (commit 3) to scaffold a runnable Godot project.

Parser strategy
---------------
GDDs in the wild aren't standardised — they range from neat
markdown with H2 section headers to a single wall of prose. We
work in two passes:

  1. **Section pass** — tokenise the markdown into ``{section_name
     → body}``. Headers are H1/H2/H3 and are normalised
     (lowercase, alias-resolved, e.g. "Gameplay" → "mechanics",
     "Characters" → "entities"). Body is everything until the next
     header at the same or higher level.

  2. **Field pass** — for each canonical section, extract structured
     content with section-specific regex:
       title         — H1 line or "**Title:** ..." line
       genre / hook  — H2 section or "Genre: ..." line
       entities      — bullets like "- **Player** — fast, light"
                       or "* Enemy: shoots projectiles"
       mechanics     — bullets in the mechanics section
       scenes        — bullets under "Scenes" / "Levels" / "World"
       controls      — bullets like "- W / Up — jump"
       win / lose    — first sentence of the section

The parser is **strict-but-forgiving**: when a section is missing it
returns an empty list / empty string rather than raising. The caller
(planner) treats absent fields as "user didn't specify, fill in with
genre defaults".

LLM fallback
------------
``distill_section(model, raw_text, kind)`` is exposed for the
planner to use when a section's plain text doesn't yield structured
bullets. It asks the model to produce a JSON list. Optional —
the parser itself doesn't call the model; the planner decides
when to invoke it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# Canonical section names + aliases
# ============================================================

# Map of canonical name → set of aliases (lowercased) that map to it.
# Headers in the GDD are tested for substring match against these.
SECTION_ALIASES = {
    "title":        ("title", "game name", "name", "project name", "game"),
    "genre":        ("genre", "category", "type of game"),
    "hook":         ("hook", "elevator pitch", "one-liner", "tagline",
                      "pitch", "concept"),
    "premise":      ("premise", "synopsis", "story", "setting",
                      "narrative", "background", "lore", "world"),
    "mechanics":    ("mechanics", "gameplay", "core gameplay",
                      "core loop", "gameplay loop", "rules",
                      "systems", "features"),
    "entities":     ("entities", "characters", "actors", "cast",
                      "enemies", "npcs", "objects", "items"),
    "scenes":       ("scenes", "levels", "stages", "rooms", "areas",
                      "worlds", "maps", "screens"),
    "controls":     ("controls", "input", "controller", "keyboard",
                      "buttons", "input scheme"),
    "win_condition": ("win condition", "win", "victory", "winning",
                       "objective", "goal", "success"),
    "lose_condition": ("lose condition", "loss", "lose", "fail",
                        "failure", "defeat", "game over"),
    "audio":        ("audio", "sound", "music", "sfx"),
    "art":          ("art", "art style", "visuals", "aesthetics"),
}


# ============================================================
# Data model
# ============================================================

@dataclass
class Entity:
    """One actor / item / interactable described in the GDD."""
    name:        str
    role:        str = ""          # "player" | "enemy" | "npc" | "item" | "obstacle" | "other"
    description: str = ""
    behaviors:   List[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return _slug(self.name)


@dataclass
class Scene:
    """One scene / level / screen described in the GDD."""
    name:        str
    kind:        str = "level"     # "level" | "menu" | "cutscene" | "ui"
    description: str = ""

    @property
    def slug(self) -> str:
        return _slug(self.name)


@dataclass
class GDD:
    """Structured Game Design Document. Output of ``parse_gdd``."""
    title:           str = ""
    genre:           str = ""
    hook:            str = ""
    premise:         str = ""
    entities:        List[Entity] = field(default_factory=list)
    mechanics:       List[str] = field(default_factory=list)
    scenes:          List[Scene] = field(default_factory=list)
    controls:        Dict[str, str] = field(default_factory=dict)
    win_condition:   str = ""
    lose_condition:  str = ""
    raw_sections:    Dict[str, str] = field(default_factory=dict)
    parse_warnings:  List[str] = field(default_factory=list)

    def is_minimal(self) -> bool:
        """True when the GDD is too thin for the planner to work
        with — used by the GUI to nudge the user to add detail."""
        return not self.title or not self.mechanics or not self.entities


# ============================================================
# Markdown tokenisation
# ============================================================

# H1 / H2 / H3 headers. We capture the level (#-count) so the body
# extractor knows when a section ends.
_HEADER_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)


def _split_sections(md: str) -> Tuple[str, List[Tuple[str, str, int]]]:
    """Return (title_line_text, [(header_text, body, level), ...]).

    The title is the first H1 line if present; otherwise empty
    string. Sections are everything from one header to the next
    of equal or higher level.
    """
    title = ""
    sections: List[Tuple[str, str, int]] = []
    matches = list(_HEADER_RE.finditer(md))
    if not matches:
        return title, [("", md.strip(), 0)]
    # H1 first → title
    if matches[0].group(1) == "#":
        title = matches[0].group(2).strip()
    for i, m in enumerate(matches):
        header = m.group(2).strip()
        level = len(m.group(1))
        start = m.end()
        end = len(md)
        # Find the next header at level ≤ this one — that closes us
        for j in range(i + 1, len(matches)):
            if len(matches[j].group(1)) <= level:
                end = matches[j].start()
                break
        body = md[start:end].strip()
        sections.append((header, body, level))
    return title, sections


# ============================================================
# Canonical section resolution
# ============================================================

def _canonical(header: str) -> str:
    """Match a header line to a canonical section name. Returns
    empty string when no alias matches.

    Uses word-boundary matching so "game" in the title aliases does
    NOT swallow a "Gameplay" header. We also iterate the aliases
    longest-first so the more specific match wins (e.g. "game name"
    beats "game", "core gameplay" beats "gameplay").
    """
    h = header.strip().lower()
    # strip leading numbering like "1.", "2)" so "1. Entities" works
    h = re.sub(r"^\d+[.)\s]+", "", h)
    best: Tuple[str, int] = ("", 0)
    for canon, aliases in SECTION_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if re.search(r"\b" + re.escape(alias) + r"\b", h):
                if len(alias) > best[1]:
                    best = (canon, len(alias))
                break
    return best[0]


# ============================================================
# Bullet / list extraction
# ============================================================

_BULLET_RE = re.compile(
    r"^[\s]*[-*•+]\s+(.+?)\s*$|^[\s]*\d+[.)]\s+(.+?)\s*$",
    re.MULTILINE,
)


def _bullets(text: str) -> List[str]:
    """Pull bullet / numbered-list items out of a block of text.
    Returns each as a stripped string."""
    out: List[str] = []
    for m in _BULLET_RE.finditer(text or ""):
        item = (m.group(1) or m.group(2) or "").strip()
        if item:
            out.append(item)
    return out


# ============================================================
# Entity parsing
# ============================================================

# Entity bullets often follow patterns like:
#   - **Player** — fast, light, double-jump
#   * Enemy: shoots projectiles
#   - Boss (final encounter)
_ENTITY_BULLET_RE = re.compile(
    # Name is **Bold**, __Bold__, or unstyled capitalised text.
    # Separator accepts em-dash, en-dash, colon, or any run of
    # ASCII hyphens — many authors write "--" for the em-dash.
    # The unstyled-name branch deliberately excludes the hyphen so
    # a leading "- Hero -- fast" bullet (after the leading list-marker
    # has already been stripped) doesn't pull the dash into the name.
    r"^(?:\*\*([^*]+)\*\*|__([^_]+)__|([A-Z][\w\s']{1,30}))"
    r"\s*(?:[—–:]|-{1,3})\s*(.+)$",
)

_ROLE_HINTS = {
    "player":    ("player", "protagonist", "main character", "hero",
                  "playable", "you"),
    "enemy":     ("enemy", "enemies", "monster", "boss", "minion",
                  "opponent", "foe", "antagonist", "mob",
                  # Common hostile-actor nouns
                  "drone", "bot", "robot", "ai", "ghost", "zombie",
                  "skeleton", "slime", "demon", "imp", "soldier",
                  "guard", "knight", "warrior", "shooter",
                  "creature", "beast",
                  # Hostile-action verbs (apply when in description)
                  "patrols", "patrol", "patrolling",
                  "attacks", "attack", "attacking",
                  "shoots", "shoot", "shooting",
                  "chases", "chase", "chasing", "pursues",
                  "alerts", "hostile", "spawns after",
                  "hunts", "ambushes", "ambush",
                  "fires at", "targets the"),
    "npc":       ("npc", "ally", "merchant", "vendor", "shopkeeper",
                  "questgiver", "quest giver", "guide", "companion",
                  "friendly", "civilian", "villager"),
    "item":      ("item", "pickup", "powerup", "power-up", "weapon",
                  "consumable", "key", "collectible", "treasure",
                  "loot", "cache", "pile", "chest", "coin", "gem",
                  "shard", "fragment", "orb"),
    "obstacle":  ("obstacle", "hazard", "trap", "spike", "platform",
                  "wall", "barrier", "door", "gate", "switch",
                  "lever"),
}


def _infer_role(name: str, description: str = "") -> str:
    """Guess the entity's role from name + description text."""
    blob = (name + " " + description).lower()
    for role, hints in _ROLE_HINTS.items():
        for h in hints:
            if re.search(r"\b" + re.escape(h) + r"\b", blob):
                return role
    return "other"


def _parse_entities_block(text: str) -> List[Entity]:
    """Extract a list of Entity from an 'Entities' / 'Characters' /
    'Enemies' block. Falls back to one Entity per bullet if the
    explicit name-description shape isn't there."""
    out: List[Entity] = []
    for raw in _bullets(text):
        m = _ENTITY_BULLET_RE.match(raw)
        if m:
            name = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            desc = m.group(4).strip()
        else:
            # No structured name — split on the first sentence as the
            # description and use the first 2-3 words as the name.
            first_words = " ".join(raw.split()[:3]).rstrip(":,.;")
            name = first_words.title() if first_words else "Entity"
            desc = raw
        # Pull comma-separated behaviours after "—" or "(...)"
        behaviors = [b.strip() for b in re.split(r"[,;]", desc)
                     if b.strip()]
        ent = Entity(
            name=name, role=_infer_role(name, desc),
            description=desc, behaviors=behaviors,
        )
        out.append(ent)
    return out


# ============================================================
# Scene parsing
# ============================================================

_SCENE_KIND_HINTS = {
    "menu":     ("menu", "title screen", "main menu", "pause"),
    "cutscene": ("cutscene", "intro", "outro", "ending", "credits"),
    "ui":       ("ui", "hud", "interface", "inventory"),
}


def _infer_scene_kind(name: str, description: str = "") -> str:
    blob = (name + " " + description).lower()
    for kind, hints in _SCENE_KIND_HINTS.items():
        for h in hints:
            if re.search(r"\b" + re.escape(h) + r"\b", blob):
                return kind
    return "level"


def _parse_scenes_block(text: str) -> List[Scene]:
    out: List[Scene] = []
    for raw in _bullets(text):
        m = _ENTITY_BULLET_RE.match(raw)
        if m:
            name = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            desc = m.group(4).strip()
        else:
            first_words = " ".join(raw.split()[:5]).rstrip(":,.;")
            name = first_words
            desc = raw
        out.append(Scene(
            name=name or "Scene",
            kind=_infer_scene_kind(name, desc),
            description=desc,
        ))
    return out


# ============================================================
# Controls parsing
# ============================================================

# Controls bullets follow patterns like:
#   - W / Up — jump
#   * Space: shoot
#   - Left click → attack
_CONTROL_RE = re.compile(
    r"^(?P<key>[\w\s/+\-]+?)\s*[—:\->→]+\s*(?P<action>.+)$",
)


def _parse_controls_block(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in _bullets(text):
        m = _CONTROL_RE.match(raw)
        if not m:
            continue
        key = m.group("key").strip().lower()
        action = m.group("action").strip()
        if key and action:
            out[key] = action
    return out


# ============================================================
# Short-field extraction (title / genre / hook from inline patterns)
# ============================================================

_INLINE_FIELD_RE = re.compile(
    # Three positions accept ``**`` so we match all three common forms:
    #   **Genre:** value     (colon inside the bold)
    #   **Genre**: value     (colon outside the bold)
    #   Genre: value         (no bold)
    r"^\s*(?:\*\*)?(?P<key>title|game name|name|genre|hook|tagline|"
    r"pitch|elevator pitch|concept|premise)"
    r"\s*(?:\*\*)?\s*[:\-—]\s*(?:\*\*)?\s*(?P<val>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _inline_fields(text: str) -> Dict[str, str]:
    """Scrape ``Title: ...`` style lines from any text block."""
    out: Dict[str, str] = {}
    for m in _INLINE_FIELD_RE.finditer(text):
        key = m.group("key").strip().lower()
        val = m.group("val").strip()
        # Strip surrounding markdown emphasis
        val = re.sub(r"^\*+|\*+$", "", val).strip()
        out[key] = val
    return out


# ============================================================
# Top-level parser
# ============================================================

def parse_gdd(text: str) -> GDD:
    """Parse a markdown GDD into a structured ``GDD`` dataclass.

    Tolerates a wide range of layouts — when a section is missing
    or unrecognised the corresponding field stays empty and a
    warning lands in ``parse_warnings``. Callers decide how to
    fill the gaps (default behaviour is "ask the planner to use
    genre defaults").
    """
    gdd = GDD()
    if not text or not text.strip():
        gdd.parse_warnings.append("empty document")
        return gdd

    title, sections = _split_sections(text)
    if title:
        gdd.title = title

    # First-pass inline fields — scan the WHOLE document for
    # ``Title: ...`` / ``**Genre:** ...`` style lines. They commonly
    # appear in an H1's body (before the first H2) but some authors
    # sprinkle them anywhere.
    inline = _inline_fields(text)
    if "title" in inline or "game name" in inline or "name" in inline:
        gdd.title = (inline.get("title") or inline.get("game name")
                     or inline.get("name") or gdd.title)
    if "genre" in inline:
        gdd.genre = inline["genre"]
    for k in ("hook", "tagline", "pitch", "elevator pitch", "concept"):
        if k in inline and not gdd.hook:
            gdd.hook = inline[k]
    if "premise" in inline and not gdd.premise:
        gdd.premise = inline["premise"]

    # Walk sections — last wins per canonical name
    for header, body, _level in sections:
        canon = _canonical(header)
        if not canon:
            continue
        gdd.raw_sections[canon] = body
        if canon == "genre" and not gdd.genre:
            # First non-empty line
            for ln in body.splitlines():
                ln = ln.strip()
                if ln and not ln.startswith(("#", ">", "-", "*")):
                    gdd.genre = ln
                    break
        elif canon == "hook" and not gdd.hook:
            for ln in body.splitlines():
                ln = ln.strip()
                if ln and not ln.startswith(("#", ">", "-", "*")):
                    gdd.hook = ln
                    break
        elif canon == "premise":
            gdd.premise = _first_paragraph(body)
        elif canon == "mechanics":
            bullets = _bullets(body)
            if bullets:
                gdd.mechanics = bullets
            else:
                # No bullets — split sentences as fallback
                gdd.mechanics = _sentences(body)
        elif canon == "entities":
            gdd.entities = _parse_entities_block(body)
        elif canon == "scenes":
            gdd.scenes = _parse_scenes_block(body)
        elif canon == "controls":
            gdd.controls = _parse_controls_block(body)
        elif canon == "win_condition":
            gdd.win_condition = _first_sentence(body)
        elif canon == "lose_condition":
            gdd.lose_condition = _first_sentence(body)

    # Final default-warning pass
    if not gdd.title:
        gdd.parse_warnings.append("no title — looked for H1 and 'Title:' lines")
    if not gdd.mechanics:
        gdd.parse_warnings.append("no mechanics extracted — add a ## Mechanics section")
    if not gdd.entities:
        gdd.parse_warnings.append("no entities extracted — add a ## Entities / Characters section")
    if not gdd.scenes:
        gdd.parse_warnings.append("no scenes/levels listed — planner will use a single Main scene")
    return gdd


# ============================================================
# Small text helpers
# ============================================================

def _first_paragraph(text: str) -> str:
    paras = re.split(r"\n\s*\n", (text or "").strip())
    return (paras[0] if paras else "").strip()


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"[^.!?\n]+[.!?]?", text)
    return m.group(0).strip() if m else text.split("\n", 1)[0].strip()


def _sentences(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
            if s.strip()]


_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def _slug(s: str) -> str:
    return _SLUG_RE.sub("_", str(s)).strip("_").lower() or "x"


# ============================================================
# Optional LLM distill hook (planner uses this when bullets are absent)
# ============================================================

_DISTILL_PROMPT = """\
You are extracting structured data from a Game Design Document.
Below is the {kind} section of a GDD as written by the user.

Output ONLY a JSON array of short strings (1-3 items per array entry,
each ≤ 80 characters). No prose, no markdown fence. Each entry should
be one item suitable for a bullet list.

For ``mechanics``: each entry is a single gameplay mechanic.
For ``entities``: each entry is "Name — short description".
For ``scenes``:   each entry is "Name — short description".

SECTION TEXT:
{text}

JSON ARRAY:
"""


def distill_section(model: Any, raw_text: str, kind: str) -> List[str]:
    """Ask the model to extract bullets from prose.

    ``kind`` is one of ``mechanics`` / ``entities`` / ``scenes``.
    Returns ``[]`` on any failure rather than raising — callers
    treat that as a "no extraction" cue.
    """
    if model is None or not raw_text or not raw_text.strip():
        return []
    try:
        prompt = _DISTILL_PROMPT.format(kind=kind, text=raw_text[:4000])
        raw = model.respond(prompt, max_tokens=400)
    except Exception:
        return []
    text = (raw or "").strip()
    # Strip optional code fence
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]
