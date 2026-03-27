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
    # Interval between idea cycles (sleep time AFTER a cycle completes)
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


# ============================================================
# Prompt builders
# ============================================================

def _build_brainstorm_prompt(settings: IdeationSettings) -> str:
    """Short prompt sent to each brainstorm contributor for a quick raw proposal."""
    seeds_line  = f"\nNiche / seed topics: {settings.seeds.strip()}" if settings.seeds.strip() else ""
    style_line  = f"\nPreferred style: {settings.style}" if settings.style and settings.style != "any" else ""
    return (
        f"You are contributing one raw video concept to a brainstorm for a solo creator.{seeds_line}{style_line}\n\n"
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
) -> str:
    """Build the user-turn prompt sent to the ideator model."""
    parts = []

    if settings.seeds.strip():
        parts.append(f"NICHE / SEED TOPICS:\n{settings.seeds.strip()}")

    if settings.style and settings.style != "any":
        parts.append(f"PREFERRED FORMAT STYLE: {settings.style}")

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
        f"Every section must be genuinely complete — no placeholders."
    )


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


def _parse_pitcher_response(text: str) -> Dict[str, Any]:
    """Extract structured fields from the pitcher's response."""
    result: Dict[str, Any] = {}

    def _extract(label: str, next_labels: List[str]) -> str:
        alts = "|".join(re.escape(l) for l in [label] + next_labels)
        # Build a pattern that stops at the next labelled section
        stop = r"(?=\n(?:" + "|".join(
            re.escape(l) for l in [
                "HOOK:", "PREMISE:", "OUTLINE:", "THUMBNAIL CONCEPT:",
                "TARGET AUDIENCE:", "WHY IT WORKS:", "TITLE VARIANTS:",
                "TAGS:", "DIFFICULTY:", "ESTIMATED LENGTH:", "PRODUCTION NOTES:",
            ]
        ) + r"))"
        m = re.search(
            label + r"\s*(.+?)" + stop,
            text, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    result["title"]             = _extract("TITLE:",             [])
    result["hook"]              = _extract("HOOK:",              [])
    result["premise"]           = _extract("PREMISE:",           [])
    result["thumbnail_concept"] = _extract("THUMBNAIL CONCEPT:", [])
    result["target_audience"]   = _extract("TARGET AUDIENCE:",   [])
    result["why_it_works"]      = _extract("WHY IT WORKS:",      [])
    result["difficulty"]        = _extract("DIFFICULTY:",        [])
    result["estimated_length"]  = _extract("ESTIMATED LENGTH:",  [])
    result["production_notes"]  = _extract("PRODUCTION NOTES:",  [])

    # Outline — numbered list
    outline_m = re.search(
        r"OUTLINE:\s*(.+?)(?=\nTHUMBNAIL CONCEPT:|\nTARGET AUDIENCE:|$)",
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
        r"TITLE VARIANTS:\s*(.+?)(?=\nTAGS:|\nDIFFICULTY:|$)",
        text, re.DOTALL | re.IGNORECASE)
    if tv_m:
        result["title_variants"] = [
            re.sub(r"^[-•\s]+", "", line).strip()
            for line in tv_m.group(1).splitlines()
            if line.strip() and line.strip() not in ("-", "•")
        ]

    # Tags — comma-separated line
    tags_m = re.search(
        r"TAGS:\s*(.+?)(?=\nDIFFICULTY:|\nESTIMATED LENGTH:|$)",
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

        # ── 2. Brainstorm phase ───────────────────────────────
        # Each enabled model throws in a quick raw concept; ideator evaluates them.
        brainstorm_proposals: List[tuple] = []
        active_brainstorm = {
            role: model for role, model in self.brainstorm_models.items()
            if role in (self.settings.brainstorm_roles or [])
        }
        if active_brainstorm:
            brainstorm_prompt = _build_brainstorm_prompt(self.settings)
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
        )

        self.progress_cb("  … Ideator evaluating and selecting raw idea…"
                         if brainstorm_proposals else "  … Ideator generating raw idea…")
        raw_response   = self.ideator_model.respond(ideator_prompt)
        ideator_fields = _parse_ideator_response(raw_response)

        raw_idea_text = raw_response  # full ideator output passed to pitcher

        # ── 4. Pitcher pass ───────────────────────────────────
        self.progress_cb("  … Pitcher developing pitch…")
        pitcher_prompt   = _build_pitcher_prompt(raw_idea_text, self.settings.seeds)
        pitched_response = self.pitcher_model.respond(pitcher_prompt)
        pitcher_fields   = _parse_pitcher_response(pitched_response)

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
