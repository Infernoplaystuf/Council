# ============================================================
# idea_engine.py  —  Continuous video idea generation engine
# ============================================================
# Provides:
#   IdeaItem          — a fully developed video idea
#   IdeationSettings  — configuration for the overnight loop
#   IdeaStore         — vault read/write for ideas
#   IdeationLoop      — background thread that generates ideas continuously
# ============================================================

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# ============================================================
# IdeaItem
# ============================================================

@dataclass
class IdeaItem:
    """A single fully fleshed-out video idea."""

    id:                str   = field(default_factory=lambda: str(uuid.uuid4())[:8])
    title:             str   = ""
    hook:              str   = ""
    premise:           str   = ""
    outline:           List[str] = field(default_factory=list)
    thumbnail_concept: str   = ""
    target_audience:   str   = ""
    why_it_works:      str   = ""
    title_variants:    List[str] = field(default_factory=list)
    tags:              List[str] = field(default_factory=list)
    difficulty:        str   = ""       # easy / medium / hard
    estimated_length:  str   = ""       # e.g. "8-12 minutes"
    production_notes:  str   = ""

    # Raw ideator output (before pitcher development)
    raw_idea:          str   = ""
    hook_angle:        str   = ""
    emotional_trigger: str   = ""
    format_suggestion: str   = ""
    seed_used:         str   = ""

    # Meta
    generated_at:      str   = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    ideator_model:     str   = "ideator"
    pitcher_model:     str   = "pitcher"
    niche_seed:        str   = ""

    # User interaction
    rating:            int   = 0        # 0=unrated 1-5 stars
    status:            str   = "new"    # new / saved / archived / in-production
    notes:             str   = ""       # user's own notes on the idea

    # Refinement tracking
    refined_from:      str   = ""       # ID of the original idea this was refined from

    # Thumbnail image (local only — never pushed to git)
    # Stores the filename only (relative to vault/idea_images/)
    thumbnail_image_path: str = ""

    # ── Derived ───────────────────────────────────────────────

    @property
    def display_title(self) -> str:
        return self.title or self.raw_idea[:60] or f"Idea {self.id}"

    @property
    def status_icon(self) -> str:
        return {
            "new":           "🆕",
            "saved":         "⭐",
            "archived":      "📦",
            "in-production": "🎬",
        }.get(self.status, "🆕")

    @property
    def difficulty_icon(self) -> str:
        return {"easy": "🟢", "medium": "🟡", "hard": "🔴"}.get(
            self.difficulty.split()[0].lower() if self.difficulty else "", "⚪")

    # ── Serialisation ─────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id":                self.id,
            "title":             self.title,
            "hook":              self.hook,
            "premise":           self.premise,
            "outline":           self.outline,
            "thumbnail_concept": self.thumbnail_concept,
            "target_audience":   self.target_audience,
            "why_it_works":      self.why_it_works,
            "title_variants":    self.title_variants,
            "tags":              self.tags,
            "difficulty":        self.difficulty,
            "estimated_length":  self.estimated_length,
            "production_notes":  self.production_notes,
            "raw_idea":          self.raw_idea,
            "hook_angle":        self.hook_angle,
            "emotional_trigger": self.emotional_trigger,
            "format_suggestion": self.format_suggestion,
            "seed_used":         self.seed_used,
            "generated_at":      self.generated_at,
            "ideator_model":     self.ideator_model,
            "pitcher_model":     self.pitcher_model,
            "niche_seed":        self.niche_seed,
            "rating":            self.rating,
            "status":            self.status,
            "notes":             self.notes,
            "refined_from":      self.refined_from,
            "thumbnail_image_path": self.thumbnail_image_path,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IdeaItem":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ============================================================
# IdeationSettings
# ============================================================

# Roles that should never participate in brainstorming — too specialised,
# delivery/technical focused, or too generic to produce useful video concepts.
BRAINSTORM_EXCLUDED = frozenset({
    "eye", "cutter", "coach", "chat", "ideator", "pitcher", "judge",
})


@dataclass
class IdeationSettings:
    """Configuration for an ideation session."""

    # Seed topics/niche — free text, comma-separated or newline-separated
    seeds:                str        = ""
    # Video style preference
    style:                str        = "any"
    # Cooldown AFTER a cycle finishes before starting the next one.
    # The models always run to full completion regardless of this value.
    interval_s:           int        = 90
    # Hard cap per session (prevents runaway overnight loops)
    max_per_session:      int        = 50
    # Inject ContentStyleManager context into prompts
    use_content_style:    bool       = True
    # Reference recent video analyses from vault
    use_video_analyses:   bool       = True
    # Number of past ideas to inject as "do not repeat" context
    anti_repeat_lookback: int        = 20
    # Roles that contribute a quick proposal before the ideator evaluates them.
    # Empty list = no brainstorm phase (ideator generates alone).
    brainstorm_roles:     List[str]  = field(default_factory=lambda: [
        "writer", "strategist", "director", "content", "algorithm", "sage",
    ])

    # Auto-refinement: every N new ideas, pick one 3+ star idea and refine it
    refine_starred:       bool       = True
    refine_min_rating:    int        = 3    # minimum star rating to qualify
    refine_every_n:       int        = 5    # refine one starred idea every N cycles

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seeds":                self.seeds,
            "style":                self.style,
            "interval_s":           self.interval_s,
            "max_per_session":      self.max_per_session,
            "use_content_style":    self.use_content_style,
            "use_video_analyses":   self.use_video_analyses,
            "anti_repeat_lookback": self.anti_repeat_lookback,
            "brainstorm_roles":     self.brainstorm_roles,
            "refine_starred":       self.refine_starred,
            "refine_min_rating":    self.refine_min_rating,
            "refine_every_n":       self.refine_every_n,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IdeationSettings":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ============================================================
# IdeaStore
# ============================================================

class IdeaStore:
    """
    Persists IdeaItem objects to vault/ideas/.
    Each idea is saved as a separate JSON file named by its timestamp + id.
    An index file (ideas_index.json) is maintained for fast listing.
    """

    def __init__(self, vault_dir: Path):
        self.vault_dir = Path(vault_dir)
        self.ideas_dir = self.vault_dir / "ideas"
        self.ideas_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.ideas_dir / "_index.json"
        self._index: List[Dict] = []
        self._load_index()

    # ── Index ─────────────────────────────────────────────────

    def _load_index(self):
        try:
            if self._index_path.exists():
                self._index = json.loads(
                    self._index_path.read_text(encoding="utf-8"))
        except Exception:
            self._index = []

    def _save_index(self):
        try:
            self._index_path.write_text(
                json.dumps(self._index, indent=2, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    # ── CRUD ──────────────────────────────────────────────────

    def save(self, item: IdeaItem):
        """Save or update an idea."""
        fname = f"{item.generated_at[:10]}_{item.id}.json".replace(":", "-")
        fpath = self.ideas_dir / fname
        fpath.write_text(
            json.dumps(item.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8")

        # Update index
        entry = {
            "id":           item.id,
            "title":        item.display_title,
            "generated_at": item.generated_at,
            "status":       item.status,
            "rating":       item.rating,
            "difficulty":   item.difficulty,
            "niche_seed":   item.niche_seed,
            "file":         fname,
        }
        self._index = [e for e in self._index if e.get("id") != item.id]
        self._index.insert(0, entry)
        self._save_index()

    def load(self, idea_id: str) -> Optional[IdeaItem]:
        """Load a full IdeaItem by ID."""
        for entry in self._index:
            if entry.get("id") == idea_id:
                fpath = self.ideas_dir / entry["file"]
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    return IdeaItem.from_dict(data)
                except Exception:
                    return None
        return None

    def list_index(self) -> List[Dict]:
        """Return the lightweight index (no full text — fast to load)."""
        return list(self._index)

    def list_all(self) -> List[IdeaItem]:
        """Load all ideas (can be slow if many). Prefer list_index() for display."""
        items = []
        for entry in self._index:
            fpath = self.ideas_dir / entry.get("file", "")
            if fpath.exists():
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    items.append(IdeaItem.from_dict(data))
                except Exception:
                    pass
        return items

    def recent_titles(self, n: int = 20) -> List[str]:
        """Return the n most recent idea titles for anti-repeat injection."""
        return [e.get("title", "") for e in self._index[:n] if e.get("title")]

    def delete(self, idea_id: str):
        for entry in list(self._index):
            if entry.get("id") == idea_id:
                fpath = self.ideas_dir / entry["file"]
                try:
                    fpath.unlink(missing_ok=True)
                except Exception:
                    pass
                self._index.remove(entry)
        self._save_index()

    def count(self) -> int:
        return len(self._index)

    def get_taste_context(
        self,
        max_positive: int = 8,
        max_negative: int = 5,
        min_positive_rating: int = 3,
    ) -> str:
        """
        Build a taste-profile block from rated ideas with notes.
        Returned as a ready-to-inject prompt string, or "" if nothing rated yet.

        - Positive examples: rated >= min_positive_rating, sorted highest first
        - Negative examples: rated 1-2, sorted lowest first
        - Notes are included inline — they are the most valuable signal
        - Unrated ideas (rating=0) are excluded
        """
        # Quick check via index before loading full files
        if not any(e.get("rating", 0) for e in self.list_index()):
            return ""

        all_items = self.list_all()
        rated = [i for i in all_items if i.rating and i.rating > 0]
        if not rated:
            return ""

        positive = sorted(
            [i for i in rated if i.rating >= min_positive_rating],
            key=lambda x: (-x.rating, x.generated_at),
        )[:max_positive]

        negative = sorted(
            [i for i in rated if i.rating < min_positive_rating],
            key=lambda x: (x.rating, x.generated_at),
        )[:max_negative]

        if not positive and not negative:
            return ""

        stars = lambda n: "★" * n + "☆" * (5 - n)
        lines = ["CREATOR'S TASTE PROFILE (learned from their ratings and notes):"]

        if positive:
            lines.append("\nIDEAS THE CREATOR RATED HIGHLY — aim for this quality and style:")
            for item in positive:
                line = f"  {stars(item.rating)}  \"{item.title or item.display_title}\""
                if item.notes and item.notes.strip():
                    line += f"\n      Note: {item.notes.strip()}"
                lines.append(line)

        if negative:
            lines.append("\nIDEAS THE CREATOR RATED POORLY — avoid these patterns:")
            for item in negative:
                line = f"  {stars(item.rating)}  \"{item.title or item.display_title}\""
                if item.notes and item.notes.strip():
                    line += f"\n      Note: {item.notes.strip()}"
                lines.append(line)

        lines.append(
            "\nUse the high-rated examples to calibrate tone, format, and premise quality. "
            "Use the low-rated examples (especially with notes) to avoid the same mistakes."
        )
        return "\n".join(lines)


# ============================================================
# Prompt builders
# ============================================================

def _build_brainstorm_prompt(settings: IdeationSettings, taste_context: str = "") -> str:
    """Short prompt sent to each brainstorm contributor for a quick raw proposal."""
    seeds_line  = f"\nNiche / seed topics: {settings.seeds.strip()}" if settings.seeds.strip() else ""
    style_line  = f"\nPreferred style: {settings.style}" if settings.style and settings.style != "any" else ""
    taste_block = f"\n\n{taste_context.strip()}" if taste_context.strip() else ""
    return (
        f"You are contributing one raw video concept to a brainstorm for a solo creator."
        f"{seeds_line}{style_line}{taste_block}\n\n"
        f"Write 1-3 sentences — the concept only, no headers, no bullet points.\n"
        f"Frame it as what the video IS or investigates, not as a story about a character.\n"
        f"Do NOT start with 'A [person/creator/expert] does/creates/builds/assembles'.\n"
        f"Think: punchy question, experiment, ranking, investigation, or counter-intuitive take.\n"
        f"One concept. Make it count."
    )


def _build_ideator_prompt(
    settings: IdeationSettings,
    recent_titles: List[str],
    content_style_text: str = "",
    video_context_text: str = "",
    brainstorm_proposals: Optional[List[tuple]] = None,  # [(role, text), ...]
    taste_context: str = "",
) -> str:
    """Build the user-turn prompt sent to the ideator model."""
    parts = []

    if settings.seeds.strip():
        parts.append(f"NICHE / SEED TOPICS:\n{settings.seeds.strip()}")

    if settings.style and settings.style != "any":
        parts.append(f"PREFERRED FORMAT STYLE: {settings.style}")

    # Taste profile — injected early so it shapes generation before anti-repeat
    if taste_context.strip():
        parts.append(taste_context.strip())

    if content_style_text.strip():
        parts.append(
            f"CREATOR CONTENT STYLE (from past video analyses):\n"
            f"{content_style_text.strip()[:800]}")

    if video_context_text.strip():
        parts.append(
            f"RECENT VIDEO CONTEXT:\n{video_context_text.strip()[:400]}")

    if recent_titles:
        titles_txt = "\n".join(f"  - {t}" for t in recent_titles[:20])
        parts.append(
            f"IDEAS ALREADY GENERATED THIS SESSION (do NOT repeat these "
            f"— find a fresh angle):\n{titles_txt}")

    if brainstorm_proposals:
        prop_lines = []
        for role, text in brainstorm_proposals:
            prop_lines.append(f"[{role.upper()}]: {text.strip()}")
        parts.append(
            "COUNCIL BRAINSTORM PROPOSALS — other models have each thrown in a raw concept.\n"
            "Your job: identify the strongest foundation (or synthesise the best elements\n"
            "from multiple proposals) into ONE compelling raw idea. You may discard weak\n"
            "proposals entirely. Do not feel bound to any single proposal — use them as\n"
            "creative fuel, not a brief.\n\n"
            + "\n\n".join(prop_lines))
        parts.append(
            "Evaluate the proposals above, then produce your own RAW IDEA output "
            "following your format exactly. The final idea must be better than any single "
            "proposal on its own.")
    else:
        parts.append(
            "Generate ONE raw video idea. Follow your output format exactly.")

    return "\n\n".join(parts)


def _build_pitcher_prompt(raw_idea_text: str, seeds: str = "") -> str:
    """Build the user-turn prompt sent to the pitcher model."""
    niche_line = f"(Creator niche / context: {seeds.strip()})\n\n" if seeds.strip() else ""
    return (
        f"{niche_line}"
        f"RAW IDEA FROM IDEATOR:\n"
        f"{raw_idea_text.strip()}\n\n"
        f"Develop this into a full pitch. Follow your output format exactly. "
        f"Every section must be genuinely complete — no placeholders.\n\n"
        f"After completing ALL sections, add this block — be STRICT about what counts as weak:\n"
        f"GAPS:\n"
        f"List ONLY sections that are genuinely underdeveloped — too vague, too short, "
        f"or missing specific actionable content. Do NOT flag a section just because it "
        f"could theoretically be better. Only flag it if a specialist would meaningfully "
        f"improve it. Use one bullet per gap:\n"
        f"- SECTION NAME: one sentence on what specifically is missing.\n"
        f"If all sections are solid, write: GAPS: none\n"
        f"Aim for 0-2 gaps, not a full list — most sections should be complete."
    )


def _parse_pitcher_gaps(text: str) -> Dict[str, str]:
    """
    Parse the pitcher's GAPS: block.
    Returns {section_name_upper: reason_text} for each flagged gap.
    Returns {} if the pitcher wrote 'none' or the block is absent.
    """
    m = re.search(r"GAPS:\s*\n(.*?)(?=\n[A-Z][A-Z ]+:|$)", text,
                  re.DOTALL | re.IGNORECASE)
    if not m:
        return {}
    block = m.group(1).strip()
    if block.lower() in ("none", "none.", "n/a", ""):
        return {}
    gaps: Dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip().lstrip("-• ").strip()
        if not line:
            continue
        if ":" in line:
            sec, _, reason = line.partition(":")
            sec = sec.strip().upper()
            reason = reason.strip()
            if sec:
                gaps[sec] = reason
        else:
            # No colon — just treat the whole line as a gap name
            sec = line.strip().upper()
            if sec:
                gaps[sec] = "flagged as weak by pitcher"
    return gaps


# Maps section names to ordered list of preferred specialist roles.
# The first role in the list that is available in brainstorm_models is used.
_GAP_ROLE_MAP: Dict[str, List[str]] = {
    "TITLE":             ["writer", "content", "director"],
    "HOOK":              ["writer", "content", "director"],
    "PREMISE":           ["writer", "sage", "strategist"],
    "OUTLINE":           ["strategist", "writer", "director"],
    "THUMBNAIL CONCEPT": ["director", "artist", "content"],
    "TARGET AUDIENCE":   ["algorithm", "strategist", "content"],
    "WHY IT WORKS":      ["algorithm", "strategist", "sage"],
    "TITLE VARIANTS":    ["writer", "content", "algorithm"],
    "PRODUCTION NOTES":  ["director", "writer", "strategist"],
    "TAGS":              ["algorithm", "content", "writer"],
    "DIFFICULTY":        ["strategist", "director", "writer"],
    "ESTIMATED LENGTH":  ["director", "strategist", "writer"],
}


def _build_gap_filler_prompt(
    section: str,
    reason: str,
    full_pitch_so_far: str,
    raw_idea: str,
    seeds: str = "",
) -> str:
    """
    Prompt sent to a specialist to fill one specific weak/missing section
    from the pitcher's first pass.
    """
    niche_line = f"Creator niche: {seeds.strip()}\n\n" if seeds.strip() else ""
    return (
        f"{niche_line}"
        f"You are helping complete a video idea pitch. The pitcher has developed most of "
        f"the pitch but flagged one section as weak or missing.\n\n"
        f"ORIGINAL RAW IDEA:\n{raw_idea.strip()}\n\n"
        f"PITCHER'S CURRENT PITCH:\n{full_pitch_so_far.strip()}\n\n"
        f"SECTION NEEDED: {section}\n"
        f"ISSUE FLAGGED: {reason}\n\n"
        f"Write ONLY the {section} section — nothing else, no preamble.\n"
        f"Start your response with the header exactly: {section}:\n"
        f"Make it specific, concrete, and genuinely strong — not a placeholder.\n"
        f"Do not repeat or rephrase what the pitcher already wrote well. "
        f"Fix the specific issue flagged above."
    )


_PITCHER_FORMAT_REMINDER = """\
Output the complete pitch using EXACTLY these headers and no others:
TITLE: [title]
HOOK: [hook]
PREMISE:
[premise]
OUTLINE:
  1. [section]
  ...
THUMBNAIL CONCEPT:
[concept]
TARGET AUDIENCE:
[audience]
WHY IT WORKS:
[reasoning]
TITLE VARIANTS:
  - [alt]
  - [alt]
TAGS: [comma-separated tags]
DIFFICULTY: [easy/medium/hard — biggest challenge]
ESTIMATED LENGTH: [X-Y minutes — why]
PRODUCTION NOTES:
[notes]
"""


def _build_pitcher_merge_prompt(
    original_pitch: str,
    gap_fills: List[tuple],   # [(section, role, fill_text), ...]
    seeds: str = "",
) -> str:
    """
    Prompt for the pitcher's final synthesis pass.
    Merges specialist gap fills into the original pitch to produce a complete, coherent output.
    Includes an explicit format reminder so the model doesn't produce prose instead of headers.
    """
    niche_line = f"(Creator niche: {seeds.strip()})\n\n" if seeds.strip() else ""
    # Summarise gap fills compactly — just section + first 300 chars of fill
    fills_block = "\n\n".join(
        f"[{section} — improved by {role}]:\n{text.strip()[:300]}"
        + ("…" if len(text.strip()) > 300 else "")
        for section, role, text in gap_fills
    )
    return (
        f"{niche_line}"
        f"You produced an initial pitch and flagged some weak sections. "
        f"Specialist models have rewritten those sections. "
        f"Produce the FINAL MERGED PITCH — take the best version of each section.\n\n"
        f"YOUR ORIGINAL PITCH (keep strong sections as-is):\n"
        f"---\n{original_pitch.strip()[:2000]}\n---\n\n"
        f"SPECIALIST IMPROVEMENTS (use these for the flagged sections):\n"
        f"---\n{fills_block}\n---\n\n"
        f"{_PITCHER_FORMAT_REMINDER}\n"
        f"Rules:\n"
        f"- Use the specialist version for improved sections, your original for the rest\n"
        f"- No GAPS: block in the final output\n"
        f"- Every section fully written — no placeholders"
    )


def _build_refinement_prompt(item: "IdeaItem") -> str:
    """
    Prompt sent to the pitcher when refining an existing 3+ star idea.
    Incorporates user notes, keeps the original concept but improves every section.
    """
    notes_block = (
        f"\nCREATOR'S NOTES ON THIS IDEA:\n{item.notes.strip()}\n"
        if item.notes and item.notes.strip()
        else ""
    )
    existing = (
        f"TITLE: {item.title}\n"
        f"HOOK: {item.hook}\n"
        f"PREMISE: {item.premise}\n"
        f"OUTLINE:\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(item.outline)) + "\n"
        f"THUMBNAIL CONCEPT: {item.thumbnail_concept}\n"
        f"TARGET AUDIENCE: {item.target_audience}\n"
        f"WHY IT WORKS: {item.why_it_works}\n"
        f"TITLE VARIANTS:\n" + "\n".join(f"  - {v}" for v in item.title_variants) + "\n"
        f"DIFFICULTY: {item.difficulty}\n"
        f"ESTIMATED LENGTH: {item.estimated_length}\n"
        f"PRODUCTION NOTES: {item.production_notes}\n"
    )
    return (
        f"This is a REFINEMENT pass — the creator has rated this idea highly "
        f"and wants it made even stronger.\n"
        f"{notes_block}\n"
        f"EXISTING PITCH TO IMPROVE:\n{existing}\n\n"
        f"Your task:\n"
        f"1. Sharpen the TITLE — make it more specific, clickable, and differentiated.\n"
        f"2. Strengthen the HOOK — it should create immediate tension or curiosity.\n"
        f"3. Improve the OUTLINE — add concrete specifics, cut vague filler sections.\n"
        f"4. Upgrade THUMBNAIL CONCEPT — more visual, more scroll-stopping.\n"
        f"5. Incorporate the creator's notes if provided.\n"
        f"6. Keep the core concept — do not replace the idea, make it the best version of itself.\n\n"
        f"Output the full improved pitch using your standard format. "
        f"Every section must be noticeably better than the version above."
    )


def _build_pitcher_completion_prompt(
    partial_response: str,
    missing_fields: List[str],
    seeds: str = "",
) -> str:
    """
    Retry prompt sent to pitcher when it missed required sections.
    Asks it to complete ONLY the missing parts.
    """
    niche_line = f"(Creator niche: {seeds.strip()})\n\n" if seeds.strip() else ""
    fields_str = ", ".join(missing_fields)
    return (
        f"{niche_line}"
        f"Your previous pitch was INCOMPLETE. "
        f"The following required sections were missing or too short: {fields_str}\n\n"
        f"Your partial output so far:\n{partial_response.strip()}\n\n"
        f"Output ONLY the missing sections using their exact headers. "
        f"Do not repeat or summarise sections that are already complete. "
        f"Be thorough — every section must be fully written, not a placeholder."
    )


# Fields the pitcher MUST produce for an idea to be accepted.
# Any idea missing these is retried or discarded.
_REQUIRED_PITCHER_FIELDS: List[str] = [
    "title", "hook", "premise", "outline",
    "thumbnail_concept", "target_audience", "why_it_works",
    "title_variants", "difficulty", "estimated_length",
]

# Minimum content thresholds (field → min length / min count)
_FIELD_MIN: Dict[str, int] = {
    "title":             5,
    "hook":              20,
    "premise":           40,
    "outline":           3,   # min 3 outline items
    "thumbnail_concept": 15,
    "target_audience":   10,
    "why_it_works":      30,
    "title_variants":    2,   # min 2 variants
    "difficulty":        2,
    "estimated_length":  2,
}

# How many times the pitcher is allowed to retry filling missing sections
_MAX_PITCHER_RETRIES = 2


def _missing_pitcher_fields(fields: Dict[str, Any]) -> List[str]:
    """
    Return the HEADER names of any pitcher sections that are absent or too short.
    Used both to decide whether to retry and to build the retry prompt.
    """
    header_map = {
        "title":             "TITLE",
        "hook":              "HOOK",
        "premise":           "PREMISE",
        "outline":           "OUTLINE",
        "thumbnail_concept": "THUMBNAIL CONCEPT",
        "target_audience":   "TARGET AUDIENCE",
        "why_it_works":      "WHY IT WORKS",
        "title_variants":    "TITLE VARIANTS",
        "difficulty":        "DIFFICULTY",
        "estimated_length":  "ESTIMATED LENGTH",
    }
    missing = []
    for key, header in header_map.items():
        val = fields.get(key)
        min_threshold = _FIELD_MIN.get(key, 1)
        if val is None:
            missing.append(header)
        elif isinstance(val, list):
            if len(val) < min_threshold:
                missing.append(header)
        elif isinstance(val, str):
            if len(val.strip()) < min_threshold:
                missing.append(header)
    return missing


# ============================================================
# Response parsers
# ============================================================

def _parse_ideator_response(text: str) -> Dict[str, str]:
    """Extract structured fields from the ideator's response."""
    result: Dict[str, str] = {}
    fields = [
        ("raw_idea",          r"RAW IDEA:\s*(.+?)(?=\n[A-Z][A-Z ]+:|$)"),
        ("hook_angle",        r"HOOK ANGLE:\s*(.+?)(?=\n[A-Z][A-Z ]+:|$)"),
        ("emotional_trigger", r"EMOTIONAL TRIGGER:\s*(.+?)(?=\n[A-Z][A-Z ]+:|$)"),
        ("format_suggestion", r"FORMAT SUGGESTION:\s*(.+?)(?=\n[A-Z][A-Z ]+:|$)"),
        ("seed_used",         r"SEED USED:\s*(.+?)(?=\n[A-Z][A-Z ]+:|$)"),
    ]
    for key, pattern in fields:
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m:
            result[key] = m.group(1).strip()
    # fallback: if parsing mostly failed, store the whole response
    if not result.get("raw_idea"):
        result["raw_idea"] = text.strip()
    return result


def _normalize_pitcher_text(text: str) -> str:
    """
    Normalise LLM output before parsing:
    - Strip markdown bold/italic around headers  (**TITLE:** → TITLE:)
    - Strip markdown heading markers              (### TITLE: → TITLE:)
    - Remove [ROLE filled SECTION]: merge markers
    - Strip GAPS: block entirely (we parse it separately; don't let it
      bleed into section content via the lookahead)
    """
    # Remove markdown bold/italic wrapping headers
    text = re.sub(r'\*{1,3}([A-Z][A-Z ]+:)', r'\1', text)
    # Remove markdown heading markers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove merge-pass role attribution lines
    text = re.sub(r'^\[.*?(?:filled|improved).*?\]:\s*', '', text,
                  flags=re.MULTILINE | re.IGNORECASE)
    # Strip everything from GAPS: onward so it doesn't interfere with parsing
    text = re.sub(r'\nGAPS:.*', '', text, flags=re.DOTALL | re.IGNORECASE)
    return text


def _parse_pitcher_response(text: str) -> Dict[str, Any]:
    """Extract structured fields from the pitcher's response."""
    text   = _normalize_pitcher_text(text)
    result: Dict[str, Any] = {}

    # All known section headers — used to build stop lookaheads
    _ALL_HEADERS = [
        "HOOK:", "PREMISE:", "OUTLINE:", "THUMBNAIL CONCEPT:",
        "TARGET AUDIENCE:", "WHY IT WORKS:", "TITLE VARIANTS:",
        "TAGS:", "DIFFICULTY:", "ESTIMATED LENGTH:", "PRODUCTION NOTES:",
        "GAPS:",    # safety net — already stripped above but keep here too
    ]

    _stop = r"(?=\n(?:" + "|".join(re.escape(h) for h in _ALL_HEADERS) + r")|\Z)"

    def _extract(label: str) -> str:
        m = re.search(
            re.escape(label) + r"[ \t]*(.+?)" + _stop,
            text, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    result["title"]             = _extract("TITLE:")
    result["hook"]              = _extract("HOOK:")
    result["premise"]           = _extract("PREMISE:")
    result["thumbnail_concept"] = _extract("THUMBNAIL CONCEPT:")
    result["target_audience"]   = _extract("TARGET AUDIENCE:")
    result["why_it_works"]      = _extract("WHY IT WORKS:")
    result["difficulty"]        = _extract("DIFFICULTY:")
    result["estimated_length"]  = _extract("ESTIMATED LENGTH:")
    result["production_notes"]  = _extract("PRODUCTION NOTES:")

    # Outline — numbered list (stop at THUMBNAIL CONCEPT or any known header)
    outline_m = re.search(
        r"OUTLINE:[ \t]*(.+?)" + _stop,
        text, re.DOTALL | re.IGNORECASE)
    if outline_m:
        raw_outline = outline_m.group(1).strip()
        result["outline"] = [
            line.strip() for line in raw_outline.splitlines()
            if re.match(r"^\s*\d+\.", line.strip()) or
               re.match(r"^\s*[-•]", line.strip())
        ]

    # Title variants — bullet list
    tv_m = re.search(
        r"TITLE VARIANTS:[ \t]*(.+?)" + _stop,
        text, re.DOTALL | re.IGNORECASE)
    if tv_m:
        result["title_variants"] = [
            re.sub(r"^[-•\s]+", "", line).strip()
            for line in tv_m.group(1).splitlines()
            if line.strip() and line.strip() not in ("-", "•")
        ]

    # Tags — comma-separated line
    tags_m = re.search(
        r"TAGS:[ \t]*(.+?)" + _stop,
        text, re.DOTALL | re.IGNORECASE)
    if tags_m:
        result["tags"] = [
            t.strip() for t in tags_m.group(1).replace("\n", ",").split(",")
            if t.strip()
        ]

    return result


# ============================================================
# IdeationLoop
# ============================================================

class IdeationLoop:
    """
    Background thread that continuously generates video ideas.

    Workflow per cycle:
      1. ideator_model  → raw idea (hook angle, format, emotional trigger)
      2. pitcher_model  → fully fleshed pitch (title, outline, thumbnail, etc.)
      3. Save IdeaItem to IdeaStore
      4. Call progress_cb and idea_cb
      5. Sleep for settings.interval_s
      6. Repeat until stopped or max_per_session reached
    """

    def __init__(
        self,
        ideator_model,          # PersonalityModel or any .respond(text) object
        pitcher_model,          # PersonalityModel or any .respond(text) object
        store: IdeaStore,
        settings: IdeationSettings,
        progress_cb: Callable[[str], None] = print,
        idea_cb: Callable[["IdeaItem"], None] = lambda _: None,
        content_style_manager=None,   # optional ContentStyleManager
        brainstorm_models: Optional[Dict[str, Any]] = None,  # {role: model}
    ):
        self.ideator_model         = ideator_model
        self.pitcher_model         = pitcher_model
        self.store                 = store
        self.settings              = settings
        self.progress_cb           = progress_cb
        self.idea_cb               = idea_cb
        self.content_style_manager = content_style_manager
        self.brainstorm_models     = brainstorm_models or {}

        self._thread:     Optional[threading.Thread] = None
        self._stop_flag:  bool = False
        self._pause_flag: bool = False
        self._count:      int  = 0   # ideas generated this session
        self._running:    bool = False
        self._session_id: str  = ""   # set in start()

    # ── Control ───────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._pause_flag

    @property
    def count(self) -> int:
        return self._count

    @property
    def session_id(self) -> str:
        """Folder-safe timestamp set when the loop starts, e.g. '3_27_26_16_00_00'."""
        return self._session_id

    def start(self):
        if self._running:
            return
        now = datetime.now()
        self._session_id = f"{now.month}_{now.day}_{now.strftime('%y_%H_%M_%S')}"
        self._stop_flag  = False
        self._pause_flag = False
        self._count      = 0
        self._running    = True
        self._thread     = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.progress_cb(
            f"▶ Ideation loop started "
            f"(interval: {self.settings.interval_s}s, "
            f"max: {self.settings.max_per_session} ideas, "
            f"session: {self._session_id})")

    def stop(self):
        self._stop_flag  = True
        self._pause_flag = False
        self.progress_cb("■ Ideation loop stopping after current cycle…")

    def pause(self):
        self._pause_flag = not self._pause_flag
        if self._pause_flag:
            self.progress_cb("⏸ Ideation loop paused.")
        else:
            self.progress_cb("▶ Ideation loop resumed.")

    # ── Main loop ─────────────────────────────────────────────

    def _loop(self):
        try:
            while not self._stop_flag:
                if self._count >= self.settings.max_per_session:
                    self.progress_cb(
                        f"✓ Ideation session complete — "
                        f"{self._count} ideas generated (limit reached).")
                    break

                # Pause handling
                while self._pause_flag and not self._stop_flag:
                    time.sleep(0.5)
                if self._stop_flag:
                    break

                # Generate one idea
                try:
                    item = self._run_one_cycle()
                    if item:
                        self._count += 1
                        self.store.save(item)
                        self.idea_cb(item)
                        self.progress_cb(
                            f"  ✓ Idea {self._count}: {item.display_title}")
                except Exception as e:
                    import traceback
                    self.progress_cb(f"  ✗ Cycle error: {e}")
                    self.progress_cb(traceback.format_exc()[:300])

                # Auto-refinement: every N cycles refine a top-rated idea
                if (self.settings.refine_starred
                        and self._count > 0
                        and self._count % self.settings.refine_every_n == 0):
                    try:
                        self._run_refinement_cycle()
                    except Exception as e:
                        self.progress_cb(f"  ⚠ Refinement pass error: {e}")

                if self._stop_flag:
                    break

                # Sleep with interruptible short-sleeps
                elapsed = 0
                while elapsed < self.settings.interval_s and not self._stop_flag:
                    time.sleep(min(1.0, self.settings.interval_s - elapsed))
                    elapsed += 1.0
                    if self._pause_flag:
                        while self._pause_flag and not self._stop_flag:
                            time.sleep(0.5)

        finally:
            self._running = False
            self.progress_cb(
                f"■ Ideation loop stopped. "
                f"Total ideas this session: {self._count}")

    def _run_one_cycle(self) -> Optional[IdeaItem]:
        """Run one brainstorm → ideator → pitcher cycle and return an IdeaItem."""

        # ── 1. Build shared context ────────────────────────────
        content_style_text = ""
        if self.settings.use_content_style and self.content_style_manager:
            try:
                content_style_text = str(self.content_style_manager)
            except Exception:
                pass

        recent_titles = self.store.recent_titles(self.settings.anti_repeat_lookback)

        # Taste profile — built once per cycle, injected into both brainstorm and ideator
        taste_context = self.store.get_taste_context()
        if taste_context:
            self.progress_cb(
                f"  … Taste profile loaded "
                f"(rated ideas: {len([i for i in self.store.list_index() if i.get('rating',0)])})")

        # ── 2. Brainstorm phase ───────────────────────────────
        # Each enabled model throws in a quick raw concept; ideator evaluates them.
        brainstorm_proposals: List[tuple] = []
        active_brainstorm = {
            role: model for role, model in self.brainstorm_models.items()
            if role in (self.settings.brainstorm_roles or [])
        }
        if active_brainstorm:
            brainstorm_prompt = _build_brainstorm_prompt(self.settings, taste_context)
            self.progress_cb(
                f"  … Brainstorm round ({len(active_brainstorm)} contributors)…")
            for role, model in active_brainstorm.items():
                try:
                    proposal = model.respond(brainstorm_prompt)
                    brainstorm_proposals.append((role, proposal.strip()))
                    self.progress_cb(f"    ↳ {role} contributed a concept")
                except Exception as e:
                    self.progress_cb(f"    ↳ {role} skipped ({e})")

        # ── 3. Ideator pass ───────────────────────────────────
        ideator_prompt = _build_ideator_prompt(
            settings             = self.settings,
            recent_titles        = recent_titles,
            content_style_text   = content_style_text,
            brainstorm_proposals = brainstorm_proposals or None,
            taste_context        = taste_context,
        )

        self.progress_cb("  … Ideator evaluating and selecting raw idea…"
                         if brainstorm_proposals else "  … Ideator generating raw idea…")
        raw_response   = self.ideator_model.respond(ideator_prompt)
        ideator_fields = _parse_ideator_response(raw_response)

        raw_idea_text = raw_response  # full ideator output passed to pitcher

        # ── 4. Pitcher first pass ─────────────────────────────
        self.progress_cb("  … Pitcher developing full pitch…")
        pitcher_prompt   = _build_pitcher_prompt(raw_idea_text, self.settings.seeds)
        pitched_response = self.pitcher_model.respond(pitcher_prompt)
        pitcher_fields   = _parse_pitcher_response(pitched_response)

        # ── 4b. Gap-filling pass ──────────────────────────────
        # The pitcher flags weak/missing sections in its GAPS: block.
        # We also check for structurally missing fields.
        # Specialists fill each gap, then the pitcher merges everything.
        pitcher_gaps   = _parse_pitcher_gaps(pitched_response)
        missing_fields = _missing_pitcher_fields(pitcher_fields)

        # Build the combined gap set: section_name → reason
        all_gaps: Dict[str, str] = {}
        for sec in missing_fields:
            all_gaps[sec] = all_gaps.get(sec, "section missing or too short")
        for sec, reason in pitcher_gaps.items():
            # Only add if we don't already have it, or reason is more informative
            if sec not in all_gaps or all_gaps[sec] == "section missing or too short":
                all_gaps[sec] = reason

        if all_gaps:
            gap_names = ", ".join(all_gaps.keys())
            self.progress_cb(
                f"  ⚠ Pitcher flagged gaps: {gap_names} — routing to specialists…")

            gap_fills: List[tuple] = []   # (section, role, fill_text)
            available_specialists  = {
                role: model for role, model in self.brainstorm_models.items()
                if role in (self.settings.brainstorm_roles or [])
            }

            for section, reason in all_gaps.items():
                preferred = _GAP_ROLE_MAP.get(section, ["writer", "strategist", "content"])
                chosen_role  = None
                chosen_model = None
                for r in preferred:
                    if r in available_specialists:
                        chosen_role  = r
                        chosen_model = available_specialists[r]
                        break

                if not chosen_model:
                    self.progress_cb(
                        f"    ↳ No specialist available for {section} — pitcher will retry")
                    continue

                self.progress_cb(f"    ↳ {chosen_role} filling {section}…")
                try:
                    fill_prompt = _build_gap_filler_prompt(
                        section, reason, pitched_response,
                        raw_idea_text, self.settings.seeds)
                    fill_text = chosen_model.respond(fill_prompt)
                    gap_fills.append((section, chosen_role, fill_text))
                    self.progress_cb(f"    ✓ {chosen_role} completed {section}")
                except Exception as e:
                    self.progress_cb(f"    ✗ {chosen_role} failed on {section}: {e}")

            # ── 4c. Pitcher merge pass ────────────────────────
            if gap_fills:
                self.progress_cb(
                    f"  … Pitcher merging {len(gap_fills)} specialist contribution(s)…")
                merge_prompt     = _build_pitcher_merge_prompt(
                    pitched_response, gap_fills, self.settings.seeds)
                pitched_response = self.pitcher_model.respond(merge_prompt)
                pitcher_fields   = _parse_pitcher_response(pitched_response)

        # ── 4d. Final safety net: pitcher-only retries ────────
        # Used only if gaps remain after the specialist pass.
        missing = _missing_pitcher_fields(pitcher_fields)
        attempt = 0
        while missing and attempt < _MAX_PITCHER_RETRIES:
            attempt += 1
            self.progress_cb(
                f"  ⚠ Still missing after specialist pass: {', '.join(missing)} "
                f"— pitcher retry {attempt}/{_MAX_PITCHER_RETRIES}…")
            retry_prompt      = _build_pitcher_completion_prompt(
                pitched_response, missing, self.settings.seeds)
            completion        = self.pitcher_model.respond(retry_prompt)
            pitched_response  = pitched_response + "\n" + completion
            pitcher_fields    = _parse_pitcher_response(pitched_response)
            missing           = _missing_pitcher_fields(pitcher_fields)

        if missing:
            self.progress_cb(
                f"  ✗ Idea discarded — still missing {missing} after all passes. "
                f"Moving to next cycle.")
            return None

        self.progress_cb("  ✓ Pitch complete — all sections filled and merged.")

        # ── 5. Assemble IdeaItem ──────────────────────────────
        item = IdeaItem(
            raw_idea          = ideator_fields.get("raw_idea", ""),
            hook_angle        = ideator_fields.get("hook_angle", ""),
            emotional_trigger = ideator_fields.get("emotional_trigger", ""),
            format_suggestion = ideator_fields.get("format_suggestion", ""),
            seed_used         = ideator_fields.get("seed_used", ""),
            niche_seed        = self.settings.seeds[:120],
            # Pitcher fields
            title             = pitcher_fields.get("title", ""),
            hook              = pitcher_fields.get("hook", ""),
            premise           = pitcher_fields.get("premise", ""),
            outline           = pitcher_fields.get("outline", []),
            thumbnail_concept = pitcher_fields.get("thumbnail_concept", ""),
            target_audience   = pitcher_fields.get("target_audience", ""),
            why_it_works      = pitcher_fields.get("why_it_works", ""),
            title_variants    = pitcher_fields.get("title_variants", []),
            tags              = pitcher_fields.get("tags", []),
            difficulty        = pitcher_fields.get("difficulty", ""),
            estimated_length  = pitcher_fields.get("estimated_length", ""),
            production_notes  = pitcher_fields.get("production_notes", ""),
            ideator_model     = getattr(self.ideator_model, "name", "ideator"),
            pitcher_model     = getattr(self.pitcher_model, "name", "pitcher"),
        )
        return item

    def _run_refinement_cycle(self):
        """
        Pick the highest-rated unrefined idea and run a pitcher refinement pass.
        The refined version is saved as a NEW IdeaItem (refined_from = original id).
        """
        min_rating = self.settings.refine_min_rating
        all_items  = self.store.list_all()

        # IDs that are already refined versions of something
        refined_ids = {i.refined_from for i in all_items if i.refined_from}

        # Candidates: rated >= min, not already refined, not already a refinement
        candidates = [
            i for i in all_items
            if i.rating >= min_rating
            and i.id not in refined_ids
            and not i.refined_from
        ]
        if not candidates:
            self.progress_cb(
                f"  ✨ No unrefined {min_rating}+ star ideas to refine yet.")
            return

        # Pick the highest-rated (break ties by most recent)
        target = max(candidates, key=lambda i: (i.rating, i.generated_at))
        self.progress_cb(
            f"  ✨ Auto-refining: {target.display_title} "
            f"({'★' * target.rating}) …")

        prompt   = _build_refinement_prompt(target)
        response = self.pitcher_model.respond(prompt)
        fields   = _parse_pitcher_response(response)

        refined = IdeaItem(
            raw_idea          = target.raw_idea,
            hook_angle        = target.hook_angle,
            emotional_trigger = target.emotional_trigger,
            format_suggestion = target.format_suggestion,
            seed_used         = target.seed_used,
            niche_seed        = target.niche_seed,
            title             = fields.get("title") or target.title,
            hook              = fields.get("hook") or target.hook,
            premise           = fields.get("premise") or target.premise,
            outline           = fields.get("outline") or target.outline,
            thumbnail_concept = fields.get("thumbnail_concept") or target.thumbnail_concept,
            target_audience   = fields.get("target_audience") or target.target_audience,
            why_it_works      = fields.get("why_it_works") or target.why_it_works,
            title_variants    = fields.get("title_variants") or target.title_variants,
            tags              = fields.get("tags") or target.tags,
            difficulty        = fields.get("difficulty") or target.difficulty,
            estimated_length  = fields.get("estimated_length") or target.estimated_length,
            production_notes  = fields.get("production_notes") or target.production_notes,
            ideator_model     = target.ideator_model,
            pitcher_model     = getattr(self.pitcher_model, "name", "pitcher"),
            rating            = target.rating,
            status            = target.status,
            notes             = target.notes,
            refined_from      = target.id,
        )
        self.store.save(refined)
        self.idea_cb(refined)
        self.progress_cb(
            f"  ✓ Refined idea saved: {refined.display_title}")

    def run_one_now(self) -> Optional["IdeaItem"]:
        """
        Synchronously run one cycle outside the loop (e.g. triggered by a button).
        Blocks until the cycle completes.
        """
        try:
            item = self._run_one_cycle()
            if item:
                self.store.save(item)
                self.idea_cb(item)
                self.progress_cb(f"✓ Single idea: {item.display_title}")
            return item
        except Exception as e:
            import traceback
            self.progress_cb(f"✗ Single idea error: {e}")
            self.progress_cb(traceback.format_exc()[:300])
            return None
