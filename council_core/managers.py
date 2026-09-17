"""
council_core.managers — content style and council instructions.

Two small stores that both front ends need and neither owns. They were already
module-level classes in council_gui_engine.py with no widget in sight; they are
moved verbatim.

`ContentStyleManager` holds the script templates and the style the council
writes in. `InstructionManager` holds the standing instructions a user adds
("always show your working as a table") — which are persistent, user-authored,
and therefore the kind of thing that must not quietly behave differently in a
second front end.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_SCRIPT_TEMPLATES = {
    "explainer": {
        "name": "Explainer / Educational",
        "description": "Teaching the viewer something clearly",
        "structure": [
            "HOOK (0:00-0:30): Lead with the surprising fact or the payoff — why should they care?",
            "SETUP (0:30-1:30): Define the problem or concept in plain language.",
            "SECTION 1 (1:30-4:00): Core concept — one main idea, explained with an example.",
            "SECTION 2 (4:00-6:30): Deeper dive or second angle — add nuance or a complication.",
            "SECTION 3 (6:30-8:30): Practical application — what does the viewer do with this?",
            "RECAP (8:30-9:30): Summarise the 3 key points in one sentence each.",
            "CTA/OUTRO (9:30-10:00): Call to action. Natural, not forced.",
        ]
    },
    "comedy_retrospective": {
        "name": "Comedy / Retrospective",
        "description": "Looking back at something with humour and self-awareness",
        "structure": [
            "HOOK (0:00-0:30): Most absurd or funniest moment first — then 'let me explain'.",
            "CONTEXT (0:30-1:30): Set the scene. Who were you, why did this exist?",
            "THE THING (1:30-5:00): Walk through it with running commentary. Lean into the absurdity.",
            "TURNING POINT (5:00-7:00): The moment you realise how unhinged it was.",
            "REFLECTION (7:00-9:00): What you'd do differently / what it says about that time.",
            "CALLBACK (9:00-9:45): Return to the opening hook with new context.",
            "OUTRO (9:45-10:00): Short, punchy. Leave them on a laugh.",
        ]
    },
    "project_showcase": {
        "name": "Project Showcase / Build Log",
        "description": "Showing off something you built",
        "structure": [
            "HOOK (0:00-0:30): Show the final result first. Make them want to know how.",
            "THE PROBLEM (0:30-1:30): Why did you build this? What was broken or missing?",
            "THE APPROACH (1:30-3:00): How you decided to solve it — options you considered.",
            "BUILD SECTION 1 (3:00-5:30): First major step — keep it visual, show don't tell.",
            "BUILD SECTION 2 (5:30-8:00): Second step — include a failure or pivot if there was one.",
            "RESULT (8:00-9:00): Show it working. Be honest about limitations.",
            "WHAT I LEARNED (9:00-9:45): One or two genuine takeaways.",
            "OUTRO (9:45-10:00): What's next. CTA.",
        ]
    },
    "powerpoint_roast": {
        "name": "PowerPoint / Document Roast",
        "description": "Revisiting old work with comedic commentary",
        "structure": [
            "HOOK (0:00-0:30): Title slide reveal — just let the title land.",
            "INTRO (0:30-1:30): What was this, when did you make it, why does it exist?",
            "SLIDE BY SLIDE (1:30-7:30): Work through it. Commentary on each slide. Don't rush.",
            "HIGHLIGHT (7:30-8:30): The single most unhinged moment. Give it space.",
            "VERDICT (8:30-9:30): Would past-you have been proud? Were you right?",
            "OUTRO (9:30-10:00): Tease the next one or invite viewers to share their worst.",
        ]
    },
    "tutorial": {
        "name": "Tutorial / How-To",
        "description": "Teaching the viewer to do something step by step",
        "structure": [
            "HOOK (0:00-0:30): Show the finished result — what they'll be able to do.",
            "REQUIREMENTS (0:30-1:30): What they need before starting. Be specific.",
            "STEP 1 (1:30-3:30): First step — clear, slow, no assumptions.",
            "STEP 2 (3:30-5:30): Second step — mention common mistakes here.",
            "STEP 3 (5:30-7:30): Third step — most tutorials lose people here, be extra clear.",
            "TROUBLESHOOTING (7:30-8:30): Top 2-3 things that go wrong and how to fix them.",
            "RESULT (8:30-9:30): Show the working result. Recap the steps briefly.",
            "OUTRO (9:30-10:00): Where to go next. CTA.",
        ]
    },
}

class ContentStyleManager:
    """
    Persists cross-session learning for the Content Creator personality.
    Stores style preferences, what worked, audience notes, and script templates.
    """

    def __init__(self, path: Path):
        self.path = path
        self._data: Dict = {}
        self._load()
        self._ensure_templates()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _ensure_templates(self) -> None:
        """Write default templates to vault if not already present."""
        if "templates" not in self._data:
            self._data["templates"] = DEFAULT_SCRIPT_TEMPLATES
            self._save()

    # ── Style memory ─────────────────────────────────────────────────────

    def add_style_note(self, note: str, category: str = "general") -> None:
        """Record something that worked or a style preference."""
        notes = self._data.setdefault("style_notes", [])
        notes.append({
            "note": note.strip(),
            "category": category,
            "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        })
        # Keep last 50 notes
        self._data["style_notes"] = notes[-50:]
        self._save()

    def set_audience(self, description: str) -> None:
        self._data["audience"] = description.strip()
        self._save()

    def set_tone(self, description: str) -> None:
        self._data["tone"] = description.strip()
        self._save()

    def get_style_notes(self, category: str = "") -> List[Dict]:
        notes = self._data.get("style_notes", [])
        if category:
            notes = [n for n in notes if n.get("category") == category]
        return notes

    # ── Templates ────────────────────────────────────────────────────────

    def get_templates(self) -> Dict:
        return self._data.get("templates", DEFAULT_SCRIPT_TEMPLATES)

    def add_template(self, key: str, name: str, description: str,
                     structure: List[str]) -> None:
        templates = self._data.setdefault("templates", {})
        templates[key] = {"name": name, "description": description, "structure": structure}
        self._save()

    def best_template_for(self, query: str) -> Optional[Dict]:
        """Return the most relevant template based on query keywords."""
        q = query.lower()
        templates = self.get_templates()
        _signals = {
            "powerpoint_roast":    ["powerpoint", "ppt", "slides", "presentation", "college", "roast"],
            "comedy_retrospective":["funny", "unhinged", "comedy", "absurd", "joke", "old", "past"],
            "project_showcase":    ["project", "build", "built", "made", "showcase", "raspberry", "council", "ai"],
            "tutorial":            ["tutorial", "how to", "how do", "step by step", "guide"],
            "explainer":           ["explain", "what is", "why does", "overview", "introduction"],
        }
        best_key = None
        best_score = 0
        for key, signals in _signals.items():
            score = sum(1 for s in signals if s in q)
            if score > best_score and key in templates:
                best_score = score
                best_key = key
        if best_key:
            return {"key": best_key, **templates[best_key]}
        return None

    # ── Context block for injection ──────────────────────────────────────

    def build_context_block(self, query: str) -> str:
        """Build a context string to prepend to the Content Creator's prompt."""
        parts: List[str] = []

        audience = self._data.get("audience", "")
        tone     = self._data.get("tone", "")
        if audience or tone:
            aud_lines = []
            if audience:
                aud_lines.append(f"  Audience: {audience}")
            if tone:
                aud_lines.append(f"  Tone: {tone}")
            parts.append("CREATOR PROFILE:\n" + "\n".join(aud_lines))

        notes = self.get_style_notes()
        if notes:
            note_lines = [f"  • [{n['category']}] {n['note']}" for n in notes[-10:]]
            parts.append("CONTENT STYLE MEMORY (what has worked for this creator):\n"
                         + "\n".join(note_lines))

        template = self.best_template_for(query)
        if template:
            struct_lines = ["  " + s for s in template.get("structure", [])]
            parts.append(
                f"SCRIPT TEMPLATE — {template['name']} ({template['description']}):\n"
                + "\n".join(struct_lines)
                + "\n\nUse this structure as your scaffold. Adapt timing to actual content length."
            )

        return "\n\n".join(parts) if parts else ""

class InstructionManager:
    """Persistent, toggleable list of council-wide instructions."""

    def __init__(self, path: Path):
        self.path = path
        self._instructions: List[Dict] = []
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._instructions = data if isinstance(data, list) else []
        except Exception:
            self._instructions = []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._instructions, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def add(self, name: str, text: str) -> Dict:
        import uuid as _uuid
        entry = {
            "id":     _uuid.uuid4().hex[:8],
            "name":   name.strip() or text[:40].strip(),
            "text":   text.strip(),
            "active": True,
        }
        self._instructions.append(entry)
        self._save()
        return entry

    def toggle(self, entry_id: str) -> bool:
        """Flip active state. Returns new state."""
        for e in self._instructions:
            if e["id"] == entry_id:
                e["active"] = not e["active"]
                self._save()
                return e["active"]
        return False

    def remove(self, entry_id: str) -> None:
        self._instructions = [e for e in self._instructions if e["id"] != entry_id]
        self._save()

    def update_text(self, entry_id: str, new_text: str) -> None:
        for e in self._instructions:
            if e["id"] == entry_id:
                e["text"] = new_text.strip()
                self._save()
                return

    def all(self) -> List[Dict]:
        return list(self._instructions)

    def active_text(self) -> str:
        """Return all active instructions joined, ready to inject into context."""
        parts = [e["text"] for e in self._instructions if e.get("active")]
        return "\n".join(parts)

    def active_count(self) -> int:
        return sum(1 for e in self._instructions if e.get("active"))
