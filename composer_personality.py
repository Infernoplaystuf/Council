# ============================================================
# composer_personality.py  —  Composer council role
# ============================================================
# The Composer is a council personality that:
#   1. Analyses a genre/style description
#   2. Selects blocks from the library
#   3. Assembles them into a CompositionPlan (structured JSON)
#   4. Can be invoked by the orchestrator like any other role
#
# The model outputs a JSON CompositionPlan which is then
# passed to the renderer to produce MIDI / MusicXML / WAV.
# ============================================================

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from music_blocks import (
    BlockLibrary, CompositionPlan, parse_composition_plan,
    GENRE_PROFILES, CHORD_FORMULAS, SCALE_FORMULAS, DURATIONS,
)

import council_engine as ce


# ============================================================
# Composer system prompt
# ============================================================

def _build_composer_system_prompt(library: BlockLibrary) -> str:
    catalogue = library.to_catalogue()
    chord_list = ", ".join(sorted(CHORD_FORMULAS.keys()))
    scale_list = ", ".join(sorted(SCALE_FORMULAS.keys()))
    genre_list = ", ".join(sorted(GENRE_PROFILES.keys()))
    duration_list = ", ".join(f"{k}={v}" for k, v in list(DURATIONS.items())[:10])

    return f"""You are the COMPOSER — a council role specialising in music composition.
You think in terms of building blocks: chord progressions, melodic phrases, rhythmic patterns, and bass lines
assembled together like Scratch blocks to form complete pieces.

=== YOUR OUTPUT CONTRACT ===
You ALWAYS respond with a single valid JSON object representing a CompositionPlan.
No prose before or after. No markdown fences. Pure JSON only.

=== JSON SCHEMA ===
{{
  "title": "string",
  "genre": "string",
  "key": "string (e.g. C, F#, Bb)",
  "mode": "string (e.g. major, minor, dorian, mixolydian)",
  "tempo": number (BPM),
  "time_signature": [numerator, denominator],
  "description": "string — brief human-readable summary",
  "sections": [
    {{
      "name": "string (e.g. intro, verse, chorus, bridge, outro)",
      "bars": integer,
      "tempo": number,
      "time_signature": [numerator, denominator],
      "progression": {{
        "name": "string",
        "key": "string",
        "mode": "string",
        "tags": ["string"],
        "chords": [
          {{
            "name": "string",
            "root": "string",
            "quality": "string",
            "duration": number (beats),
            "octave": integer (3-5),
            "inversion": integer (0=root),
            "velocity": integer (0-127),
            "tags": []
          }}
        ]
      }},
      "melody": {{
        "name": "string",
        "key": "string",
        "scale": "string",
        "tags": [],
        "notes": [
          {{"pitch": integer (MIDI 0-127, or -1 for rest), "duration": number (beats), "velocity": integer}}
        ]
      }},
      "bass": {{
        "name": "string",
        "root": "string",
        "tags": [],
        "notes": [
          {{"pitch": integer (MIDI 0-127, or -1 for rest), "duration": number (beats), "velocity": integer}}
        ]
      }},
      "rhythm": {{
        "name": "string",
        "time_signature": [numerator, denominator],
        "tags": [],
        "pattern": [[duration_beats, velocity], ...]
      }}
    }}
  ]
}}

=== AVAILABLE CHORD QUALITIES ===
{chord_list}

=== AVAILABLE SCALES ===
{scale_list}

=== DURATION VALUES (in beats, 1 beat = 1 quarter note) ===
{duration_list}
whole=4.0, half=2.0, quarter=1.0, eighth=0.5, sixteenth=0.25
triplet_eighth=0.333

=== MIDI NOTE REFERENCE ===
Middle C (C4) = MIDI 60. Each octave = 12 semitones.
C4=60, D4=62, E4=64, F4=65, G4=67, A4=69, B4=71
C5=72, C3=48, C2=36 (bass register)
Bass lines: use octave 2-3 (MIDI 36-55)
Melody: use octave 4-5 (MIDI 60-83)
Chords: use octave 3-4 (MIDI 48-71)

=== AVAILABLE GENRES ===
{genre_list}

=== BLOCK LIBRARY (existing blocks you CAN reference) ===
{catalogue}

=== COMPOSITION RULES ===
1. Match chord qualities to genre — jazz uses maj7/min7/dom7, metal uses dim/aug/m, pop uses maj/min/add9
2. Bass notes should be the root of the current chord, one octave below the chord voicing
3. Melody should be diatonic to the key/scale, with occasional chromatic passing tones
4. Rhythm patterns must sum to exactly (time_sig_numerator * 4/time_sig_denominator) beats per bar
5. Voice leading: move chord voices by the smallest interval possible between chords
6. Sections should have contrasting energy: verse=moderate, chorus=high, bridge=shift
7. Typical structure: intro (4-8 bars) → verse → chorus → verse → chorus → bridge → chorus → outro
8. For odd time signatures (5/4, 7/8 etc.) — pattern durations must sum to the correct beat count

=== THINKING PROCESS ===
Before outputting JSON, internally reason through:
1. What genre characteristics apply? (tempo range, feel, chord colors, scale)
2. What key and mode fits the mood?
3. What progression structure creates the right harmonic tension/release?
4. How should the melody move relative to the chords?
5. What rhythmic feel drives the piece?
Then output the JSON.
"""


# ============================================================
# Composer personality wrapper
# ============================================================

@dataclass
class ComposerResult:
    plan: Optional[CompositionPlan]
    raw_json: str
    parse_error: str = ""
    event_log: List[str] = field(default_factory=list)


class ComposerPersonality:
    """
    Wraps a PersonalityModel as the Composer council role.
    Exposes .compose(prompt) → ComposerResult
    and .respond(prompt) for council orchestrator compatibility.
    """

    def __init__(
        self,
        personality_model: ce.PersonalityModel,
        library: BlockLibrary,
        event_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.model    = personality_model
        self.library  = library
        self._event_cb = event_callback

        # Inject the system prompt
        self.model.extra_context = _build_composer_system_prompt(library)

    def _emit(self, phase: str, msg: str):
        if self._event_cb:
            self._event_cb(phase, msg)

    def compose(self, prompt: str) -> ComposerResult:
        """
        Main entry: given a natural language prompt, return a ComposerResult
        with a parsed CompositionPlan ready for rendering.
        """
        self._emit("compose_start", f"Composing: {prompt[:80]}")

        # Enrich prompt with genre profile if genre is mentioned
        enriched = self._enrich_prompt(prompt)

        raw = self.model.respond(enriched)
        self._emit("compose_raw", f"Raw output ({len(raw)} chars)")

        # Parse
        plan = parse_composition_plan(raw)
        result = ComposerResult(plan=plan, raw_json=raw)

        if plan is None:
            result.parse_error = "Failed to parse JSON from model output"
            self._emit("compose_error", result.parse_error)
        else:
            self._emit("compose_done",
                       f"Plan parsed: {plan.title} | "
                       f"{len(plan.sections)} sections | "
                       f"{plan.tempo} BPM | {plan.key} {plan.mode}")
        return result

    def respond(self, prompt: str, **kwargs) -> str:
        """Council orchestrator compatibility — returns raw JSON string."""
        result = self.compose(prompt)
        return result.raw_json

    def _enrich_prompt(self, prompt: str) -> str:
        """Inject genre profile if a known genre is mentioned."""
        p_lower = prompt.lower()
        matched_genre = None
        for genre in GENRE_PROFILES:
            if genre in p_lower:
                matched_genre = genre
                break

        if matched_genre:
            profile = GENRE_PROFILES[matched_genre]
            tempo_min, tempo_max = profile["tempo_range"]
            profile_note = (
                f"\n\nGENRE CONTEXT for '{matched_genre}':\n"
                f"  Typical tempo: {tempo_min}–{tempo_max} BPM\n"
                f"  Feel: {profile['feel']}\n"
                f"  Recommended scales: {', '.join(profile['scales'])}\n"
                f"  Recommended chord colors: {', '.join(profile['chord_colors'])}\n"
            )
            return prompt + profile_note

        return prompt


# ============================================================
# Routing — teach the council router about composition requests
# ============================================================

COMPOSER_KEYWORDS = [
    "compose", "composition", "write a song", "create a song",
    "music for", "melody for", "chord progression for",
    "generate music", "make music", "musical", "soundtrack",
    "jazz piece", "blues piece", "classical piece", "ambient piece",
    "song in", "track in", "piece in", "riff", "theme",
    "bpm", "time signature", "in the style of",
]


def patch_routing(council_engine_module: Any) -> None:
    """
    Add composer routing to council_engine._ROUTE_PATTERNS.
    Call after importing council_engine.
    """
    patterns = getattr(council_engine_module, "_ROUTE_PATTERNS", None)
    if patterns is None:
        return

    # Insert composer route with high priority (before writer default)
    composer_entry = (
        "composer",
        COMPOSER_KEYWORDS,
        8,
    )
    # Insert before the final writer default entry
    for i, entry in enumerate(patterns):
        if entry[0] == "writer" and not entry[1]:  # writer default (empty keywords)
            patterns.insert(i, composer_entry)
            print("[Composer] Patched routing table")
            return

    patterns.append(composer_entry)
    print("[Composer] Appended to routing table")
