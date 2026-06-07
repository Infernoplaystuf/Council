# ============================================================
# music_blocks.py  —  Musical primitive block library
# ============================================================
# The building block system for the Composer role.
# Blocks are named, typed primitives that can be assembled
# into full compositions like Scratch snaps code together.
#
# Block types:
#   ChordBlock      — a chord voicing with duration
#   MelodicBlock    — a sequence of notes forming a phrase
#   RhythmBlock     — a rhythmic pattern (hits + rests)
#   BassBlock       — a bass line pattern
#   ProgressionBlock — an ordered sequence of ChordBlocks
#   ArrangementBlock — layered multi-part structure
#
# Install:
#   pip install mido music21
#   (for WAV preview: pip install fluidsynth — optional)
# ============================================================

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


# ============================================================
# Music theory constants
# ============================================================

# MIDI note numbers — middle C = 60 (C4)
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Interval names → semitone count
INTERVALS = {
    "P1": 0, "m2": 1, "M2": 2, "m3": 3, "M3": 4,
    "P4": 5, "A4": 6, "d5": 6, "P5": 7, "m6": 8,
    "M6": 9, "m7": 10, "M7": 11, "P8": 12,
}

# Chord formulas — intervals from root (semitones)
CHORD_FORMULAS: Dict[str, List[int]] = {
    # Triads
    "maj":      [0, 4, 7],
    "min":      [0, 3, 7],
    "dim":      [0, 3, 6],
    "aug":      [0, 4, 8],
    "sus2":     [0, 2, 7],
    "sus4":     [0, 5, 7],
    # Sevenths
    "maj7":     [0, 4, 7, 11],
    "min7":     [0, 3, 7, 10],
    "dom7":     [0, 4, 7, 10],
    "dim7":     [0, 3, 6, 9],
    "m7b5":     [0, 3, 6, 10],   # half-diminished
    "minmaj7":  [0, 3, 7, 11],
    "augmaj7":  [0, 4, 8, 11],
    "aug7":     [0, 4, 8, 10],
    # Extensions
    "maj9":     [0, 4, 7, 11, 14],
    "min9":     [0, 3, 7, 10, 14],
    "dom9":     [0, 4, 7, 10, 14],
    "maj11":    [0, 4, 7, 11, 14, 17],
    "min11":    [0, 3, 7, 10, 14, 17],
    "maj13":    [0, 4, 7, 11, 14, 17, 21],
    "dom13":    [0, 4, 7, 10, 14, 17, 21],
    # Jazz / altered
    "maj7#11":  [0, 4, 7, 11, 18],   # Lydian sound
    "dom7b9":   [0, 4, 7, 10, 13],
    "dom7#9":   [0, 4, 7, 10, 15],   # Hendrix chord
    "dom7b5":   [0, 4, 6, 10],
    "dom7#5":   [0, 4, 8, 10],
    # Added tones
    "add9":     [0, 4, 7, 14],
    "madd9":    [0, 3, 7, 14],
    "6":        [0, 4, 7, 9],
    "m6":       [0, 3, 7, 9],
    "69":       [0, 4, 7, 9, 14],
}

# Scale formulas — semitone steps from root
SCALE_FORMULAS: Dict[str, List[int]] = {
    "major":            [0, 2, 4, 5, 7, 9, 11],
    "natural_minor":    [0, 2, 3, 5, 7, 8, 10],
    "harmonic_minor":   [0, 2, 3, 5, 7, 8, 11],
    "melodic_minor":    [0, 2, 3, 5, 7, 9, 11],
    "dorian":           [0, 2, 3, 5, 7, 9, 10],
    "phrygian":         [0, 1, 3, 5, 7, 8, 10],
    "lydian":           [0, 2, 4, 6, 7, 9, 11],
    "mixolydian":       [0, 2, 4, 5, 7, 9, 10],
    "locrian":          [0, 1, 3, 5, 6, 8, 10],
    "pentatonic_major": [0, 2, 4, 7, 9],
    "pentatonic_minor": [0, 3, 5, 7, 10],
    "blues":            [0, 3, 5, 6, 7, 10],
    "whole_tone":       [0, 2, 4, 6, 8, 10],
    "diminished":       [0, 2, 3, 5, 6, 8, 9, 11],
    "chromatic":        list(range(12)),
}

# Duration names → beats (at 1 beat = 1 quarter note)
DURATIONS: Dict[str, float] = {
    "whole":          4.0,
    "dotted_half":    3.0,
    "half":           2.0,
    "dotted_quarter": 1.5,
    "quarter":        1.0,
    "dotted_eighth":  0.75,
    "eighth":         0.5,
    "sixteenth":      0.25,
    "thirtysecond":   0.125,
    "triplet_quarter": 2/3,
    "triplet_eighth":  1/3,
    "triplet_sixteenth": 1/6,
}

# Genre → typical characteristics
GENRE_PROFILES: Dict[str, Dict[str, Any]] = {
    "jazz":         {"tempo_range": (60, 180),  "feel": "swing",    "scales": ["dorian", "mixolydian", "melodic_minor"], "chord_colors": ["maj7", "min7", "dom7", "m7b5", "maj7#11"]},
    "blues":        {"tempo_range": (60, 130),  "feel": "shuffle",  "scales": ["blues", "pentatonic_minor"],             "chord_colors": ["dom7", "dom9", "dom7#9"]},
    "pop":          {"tempo_range": (90, 140),  "feel": "straight", "scales": ["major", "natural_minor"],                "chord_colors": ["maj", "min", "maj7", "add9"]},
    "rock":         {"tempo_range": (100, 160), "feel": "straight", "scales": ["pentatonic_minor", "natural_minor"],     "chord_colors": ["maj", "min", "dom7", "sus4"]},
    "classical":    {"tempo_range": (40, 160),  "feel": "straight", "scales": ["major", "harmonic_minor"],               "chord_colors": ["maj", "min", "dim", "aug", "dom7"]},
    "electronic":   {"tempo_range": (100, 150), "feel": "straight", "scales": ["minor", "dorian", "phrygian"],           "chord_colors": ["min7", "maj7", "sus2", "dom7"]},
    "bossa_nova":   {"tempo_range": (120, 160), "feel": "straight", "scales": ["major", "lydian"],                       "chord_colors": ["maj7", "min7", "dom7b9", "maj7#11"]},
    "funk":         {"tempo_range": (90, 120),  "feel": "groove",   "scales": ["pentatonic_minor", "dorian"],            "chord_colors": ["dom7", "min7", "dom9", "69"]},
    "reggae":       {"tempo_range": (60, 100),  "feel": "offbeat",  "scales": ["major", "pentatonic_major"],             "chord_colors": ["maj", "min", "dom7"]},
    "metal":        {"tempo_range": (100, 220), "feel": "straight", "scales": ["phrygian", "locrian", "diminished"],     "chord_colors": ["min", "dim", "aug", "dom7b5"]},
    "soul":         {"tempo_range": (60, 110),  "feel": "groove",   "scales": ["dorian", "pentatonic_minor"],            "chord_colors": ["maj7", "min7", "dom9", "6"]},
    "country":      {"tempo_range": (80, 140),  "feel": "straight", "scales": ["major", "pentatonic_major"],             "chord_colors": ["maj", "dom7", "6", "add9"]},
    "ambient":      {"tempo_range": (60, 90),   "feel": "straight", "scales": ["lydian", "major", "whole_tone"],         "chord_colors": ["maj7", "maj9", "sus2", "maj7#11"]},
}


# ============================================================
# Note utilities
# ============================================================

def note_name_to_midi(name: str, octave: int = 4) -> int:
    """Convert note name + octave to MIDI number. e.g. "C", 4 → 60"""
    name = name.strip().upper().replace("BB", "A#").replace("EB", "D#")
    name = name.replace("AB", "G#").replace("DB", "C#").replace("GB", "F#")
    name = name.replace("B#", "C").replace("E#", "F")
    if name not in NOTE_NAMES:
        raise ValueError(f"Unknown note: {name!r}")
    return NOTE_NAMES.index(name) + (octave + 1) * 12


def midi_to_note_name(midi: int) -> Tuple[str, int]:
    """Convert MIDI number to (note_name, octave). e.g. 60 → ("C", 4)"""
    octave = (midi // 12) - 1
    name = NOTE_NAMES[midi % 12]
    return name, octave


def chord_midi_notes(root: str, quality: str, octave: int = 4) -> List[int]:
    """Return MIDI note numbers for a chord."""
    if quality not in CHORD_FORMULAS:
        raise ValueError(f"Unknown chord quality: {quality!r}. "
                         f"Available: {list(CHORD_FORMULAS.keys())}")
    root_midi = note_name_to_midi(root, octave)
    return [root_midi + interval for interval in CHORD_FORMULAS[quality]]


def scale_midi_notes(root: str, scale: str, octave: int = 4, octaves: int = 1) -> List[int]:
    """Return MIDI note numbers for a scale across N octaves."""
    if scale not in SCALE_FORMULAS:
        raise ValueError(f"Unknown scale: {scale!r}")
    root_midi = note_name_to_midi(root, octave)
    notes = []
    for oct_offset in range(octaves):
        for step in SCALE_FORMULAS[scale]:
            notes.append(root_midi + step + oct_offset * 12)
    return notes


# ============================================================
# Block dataclasses
# ============================================================

@dataclass
class NoteEvent:
    """A single note with pitch, duration, and velocity."""
    pitch: int          # MIDI note number (0-127), or -1 for rest
    duration: float     # in beats
    velocity: int = 80  # 0-127

    @classmethod
    def rest(cls, duration: float) -> "NoteEvent":
        return cls(pitch=-1, duration=duration, velocity=0)

    @classmethod
    def from_name(cls, name: str, octave: int, duration: float,
                  velocity: int = 80) -> "NoteEvent":
        return cls(pitch=note_name_to_midi(name, octave),
                   duration=duration, velocity=velocity)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChordBlock:
    """A chord voicing — root, quality, optional inversion, duration."""
    name: str               # human label, e.g. "Cmaj7 bar 1"
    root: str               # "C", "F#", etc.
    quality: str            # key in CHORD_FORMULAS
    duration: float         # beats
    octave: int = 4
    inversion: int = 0      # 0=root, 1=first, 2=second, etc.
    velocity: int = 80
    tags: List[str] = field(default_factory=list)

    def midi_notes(self) -> List[int]:
        notes = chord_midi_notes(self.root, self.quality, self.octave)
        if self.inversion > 0:
            for _ in range(self.inversion):
                notes = notes[1:] + [notes[0] + 12]
        return notes

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChordBlock":
        return cls(**d)


@dataclass
class MelodicBlock:
    """A melodic phrase — ordered sequence of NoteEvents."""
    name: str
    notes: List[NoteEvent]
    key: str = "C"
    scale: str = "major"
    tags: List[str] = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        return sum(n.duration for n in self.notes)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MelodicBlock":
        notes = [NoteEvent(**n) for n in d.pop("notes")]
        return cls(notes=notes, **d)


@dataclass
class RhythmBlock:
    """
    A rhythmic pattern expressed as a list of (duration, velocity) pairs.
    velocity=0 means rest.
    """
    name: str
    pattern: List[Tuple[float, int]]   # [(duration_beats, velocity), ...]
    time_signature: Tuple[int, int] = (4, 4)
    tags: List[str] = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        return sum(d for d, _ in self.pattern)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "pattern": self.pattern,
            "time_signature": list(self.time_signature),
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RhythmBlock":
        d = dict(d)
        d["time_signature"] = tuple(d.get("time_signature", [4, 4]))
        d["pattern"] = [tuple(p) for p in d["pattern"]]
        return cls(**d)


@dataclass
class BassBlock:
    """A bass line — notes tied to a chord context."""
    name: str
    notes: List[NoteEvent]
    root: str = "C"
    tags: List[str] = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        return sum(n.duration for n in self.notes)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BassBlock":
        notes = [NoteEvent(**n) for n in d.pop("notes")]
        return cls(notes=notes, **d)


@dataclass
class ProgressionBlock:
    """An ordered sequence of ChordBlocks forming a progression."""
    name: str
    chords: List[ChordBlock]
    key: str = "C"
    mode: str = "major"
    tags: List[str] = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        return sum(c.duration for c in self.chords)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "chords": [c.to_dict() for c in self.chords],
            "key": self.key,
            "mode": self.mode,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProgressionBlock":
        d = dict(d)
        chords = [ChordBlock.from_dict(c) for c in d.pop("chords")]
        return cls(chords=chords, **d)


@dataclass
class ArrangementSection:
    """One section (e.g. verse, chorus) with parts per instrument."""
    name: str                                  # "verse", "chorus", etc.
    bars: int                                  # number of bars
    tempo: float                               # BPM
    time_signature: Tuple[int, int]
    progression: Optional[ProgressionBlock]
    melody: Optional[MelodicBlock]
    bass: Optional[BassBlock]
    rhythm: Optional[RhythmBlock]
    extra_parts: Dict[str, MelodicBlock] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "bars": self.bars,
            "tempo": self.tempo,
            "time_signature": list(self.time_signature),
            "progression": self.progression.to_dict() if self.progression else None,
            "melody": self.melody.to_dict() if self.melody else None,
            "bass": self.bass.to_dict() if self.bass else None,
            "rhythm": self.rhythm.to_dict() if self.rhythm else None,
            "extra_parts": {k: v.to_dict() for k, v in self.extra_parts.items()},
        }


@dataclass
class CompositionPlan:
    """
    The complete assembly plan output by the Composer role.
    This is the structured representation that the renderer turns
    into MIDI / MusicXML / WAV.
    """
    title: str
    genre: str
    key: str
    mode: str                           # "major", "minor", etc.
    tempo: float                        # BPM
    time_signature: Tuple[int, int]
    sections: List[ArrangementSection]
    description: str = ""              # human-readable summary

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "genre": self.genre,
            "key": self.key,
            "mode": self.mode,
            "tempo": self.tempo,
            "time_signature": list(self.time_signature),
            "sections": [s.to_dict() for s in self.sections],
            "description": self.description,
        }

    def to_json(self, path: Optional[Path] = None) -> str:
        s = json.dumps(self.to_dict(), indent=2)
        if path:
            path.write_text(s, encoding="utf-8")
        return s


# ============================================================
# Block Library — persistent JSON store
# ============================================================

class BlockLibrary:
    """
    Persistent library of named blocks.
    Stored as JSON in vault/music_blocks/.
    The Composer role reads this to assemble compositions.
    """

    BLOCK_TYPES = {
        "chord":       (ChordBlock,       "chord_blocks.json"),
        "melody":      (MelodicBlock,     "melody_blocks.json"),
        "rhythm":      (RhythmBlock,      "rhythm_blocks.json"),
        "bass":        (BassBlock,        "bass_blocks.json"),
        "progression": (ProgressionBlock, "progression_blocks.json"),
    }

    def __init__(self, library_dir: Path):
        self.dir = library_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._load_all()

    def _load_all(self):
        for btype, (cls, fname) in self.BLOCK_TYPES.items():
            path = self.dir / fname
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                self._cache[btype] = raw
            else:
                self._cache[btype] = {}

    def _save(self, btype: str):
        _, fname = self.BLOCK_TYPES[btype]
        path = self.dir / fname
        path.write_text(json.dumps(self._cache[btype], indent=2), encoding="utf-8")

    def add(self, btype: str, block: Any) -> None:
        """Add a block to the library."""
        self._cache.setdefault(btype, {})[block.name] = block.to_dict()
        self._save(btype)

    def get(self, btype: str, name: str) -> Optional[Any]:
        """Retrieve a block by type and name."""
        raw = self._cache.get(btype, {}).get(name)
        if raw is None:
            return None
        cls, _ = self.BLOCK_TYPES[btype]
        return cls.from_dict(dict(raw))

    def list_blocks(self, btype: Optional[str] = None,
                    tags: Optional[List[str]] = None) -> Dict[str, List[str]]:
        """List all block names, optionally filtered by type and tags."""
        result: Dict[str, List[str]] = {}
        types = [btype] if btype else list(self.BLOCK_TYPES.keys())
        for t in types:
            names = []
            for name, data in self._cache.get(t, {}).items():
                if tags:
                    block_tags = data.get("tags", [])
                    if not any(tag in block_tags for tag in tags):
                        continue
                names.append(name)
            result[t] = sorted(names)
        return result

    def search(self, query: str) -> Dict[str, List[str]]:
        """Fuzzy search block names and tags."""
        q = query.lower()
        result: Dict[str, List[str]] = {}
        for btype, blocks in self._cache.items():
            matches = []
            for name, data in blocks.items():
                if (q in name.lower() or
                        any(q in t.lower() for t in data.get("tags", []))):
                    matches.append(name)
            if matches:
                result[btype] = matches
        return result

    def to_catalogue(self) -> str:
        """Return a compact text catalogue for injection into model context."""
        lines = ["=== BLOCK LIBRARY CATALOGUE ===\n"]
        for btype, blocks in self._cache.items():
            if not blocks:
                continue
            lines.append(f"[{btype.upper()} BLOCKS]")
            for name, data in blocks.items():
                tags = data.get("tags", [])
                tag_str = f"  tags: {', '.join(tags)}" if tags else ""
                lines.append(f"  • {name}{tag_str}")
            lines.append("")
        lines.append("=== END CATALOGUE ===")
        return "\n".join(lines)

    def seed_defaults(self) -> None:
        """
        Populate library with a starter set of common blocks.
        Call once on first run.
        """
        # ── Chord progressions ───────────────────────────────
        def _prog(name, key, mode, chords_spec, tags):
            """chords_spec: [(root, quality, duration_beats), ...]"""
            chords = [
                ChordBlock(name=f"{r}{q}", root=r, quality=q,
                           duration=d, octave=4)
                for r, q, d in chords_spec
            ]
            return ProgressionBlock(name=name, chords=chords,
                                    key=key, mode=mode, tags=tags)

        progressions = [
            _prog("I-IV-V-I (C major)",  "C", "major",
                  [("C","maj",4),("F","maj",4),("G","maj",4),("C","maj",4)],
                  ["pop","rock","country","major"]),
            _prog("I-V-vi-IV (C major)", "C", "major",
                  [("C","maj",4),("G","maj",4),("A","min",4),("F","maj",4)],
                  ["pop","major","anthemic"]),
            _prog("ii-V-I (C major jazz)", "C", "major",
                  [("D","min7",4),("G","dom7",4),("C","maj7",8)],
                  ["jazz","major"]),
            _prog("i-VII-VI-VII (A minor)", "A", "minor",
                  [("A","min",4),("G","maj",4),("F","maj",4),("G","maj",4)],
                  ["rock","minor","modal"]),
            _prog("i-iv-v (A natural minor)", "A", "minor",
                  [("A","min",4),("D","min",4),("E","min",4),("A","min",4)],
                  ["minor","classical"]),
            _prog("I-vi-IV-V (C major)", "C", "major",
                  [("C","maj",4),("A","min",4),("F","maj",4),("G","maj",4)],
                  ["pop","50s","doo-wop"]),
            _prog("12-bar blues (A)", "A", "blues",
                  [("A","dom7",4),("A","dom7",4),("A","dom7",4),("A","dom7",4),
                   ("D","dom7",4),("D","dom7",4),("A","dom7",4),("A","dom7",4),
                   ("E","dom7",4),("D","dom7",4),("A","dom7",4),("E","dom7",4)],
                  ["blues","jazz"]),
            _prog("Autumn Leaves style (G minor jazz)", "G", "minor",
                  [("C","min7",4),("F","dom7",4),("Bb","maj7",4),("Eb","maj7",4),
                   ("A","m7b5",4),("D","dom7",4),("G","min7",8)],
                  ["jazz","minor","standard"]),
            _prog("i-bVII-bVI-bVII (modal rock)", "A", "dorian",
                  [("A","min7",4),("G","maj7",4),("F","maj7",4),("G","maj7",4)],
                  ["rock","modal","dorian","electronic"]),
            _prog("Andalusian cadence (A phrygian)", "A", "phrygian",
                  [("A","min",4),("G","maj",4),("F","maj",4),("E","maj",4)],
                  ["flamenco","metal","phrygian","classical"]),
        ]
        for p in progressions:
            self.add("progression", p)

        # ── Rhythm patterns ──────────────────────────────────
        def _rhythm(name, pattern, ts, tags):
            return RhythmBlock(name=name, pattern=pattern,
                               time_signature=ts, tags=tags)

        q, e, s = 1.0, 0.5, 0.25
        rhythms = [
            _rhythm("4/4 straight quarter", [(q,80),(q,80),(q,80),(q,80)], (4,4), ["straight","simple"]),
            _rhythm("4/4 eighth groove",     [(e,80),(e,70)]*4,             (4,4), ["groove","pop"]),
            _rhythm("4/4 backbeat",          [(q,60),(q,100),(q,60),(q,100)],(4,4),["rock","pop","backbeat"]),
            _rhythm("4/4 syncopated",        [(e,80),(e,0),(s,80),(s,70),(e,80),(e,0),(s,80),(s,0),(e,80),(e,70)], (4,4), ["syncopated","funk","soul"]),
            _rhythm("4/4 shuffle",           [(2/3,80),(1/3,70),(2/3,80),(1/3,70),(2/3,80),(1/3,70),(2/3,80),(1/3,70)], (4,4), ["shuffle","blues","jazz","swing"]),
            _rhythm("3/4 waltz",             [(q,90),(q,60),(q,60)],        (3,4), ["waltz","3/4","classical"]),
            _rhythm("6/8 compound",          [(e,80),(e,60),(e,70),(e,80),(e,60),(e,70)], (6,8), ["6/8","compound","folk"]),
            _rhythm("5/4 odd meter",         [(q,80),(q,80),(q,80),(q,80),(q,80)], (5,4), ["odd","5/4","progressive"]),
            _rhythm("7/8 odd meter",         [(e,80),(e,80),(e,80),(q,80),(e,80),(e,80)], (7,8), ["odd","7/8","progressive"]),
            _rhythm("4/4 bossa nova",        [(e,80),(e,0),(s,80),(s,0),(e,80),(e,0),(s,0),(s,80),(e,80),(e,0),(s,80),(s,0),(e,80),(e,0),(s,80),(s,0)], (4,4), ["bossa","latin","groove"]),
        ]
        for r in rhythms:
            self.add("rhythm", r)

        print(f"[BlockLibrary] Seeded {len(progressions)} progressions, "
              f"{len(rhythms)} rhythms.")


# ============================================================
# Composition plan parser
# ============================================================

def parse_composition_plan(json_str: str) -> Optional[CompositionPlan]:
    """
    Parse the JSON output from the Composer role into a CompositionPlan.
    Returns None on parse failure.
    """
    # Strip markdown fences if present
    json_str = re.sub(r"```(?:json)?\s*", "", json_str).strip().rstrip("`").strip()

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"[CompositionPlan] JSON parse error: {e}")
        return None

    sections = []
    for sec in data.get("sections", []):
        prog = None
        if sec.get("progression"):
            try:
                prog = ProgressionBlock.from_dict(sec["progression"])
            except Exception:
                pass

        melody = None
        if sec.get("melody"):
            try:
                melody = MelodicBlock.from_dict(sec["melody"])
            except Exception:
                pass

        bass = None
        if sec.get("bass"):
            try:
                bass = BassBlock.from_dict(sec["bass"])
            except Exception:
                pass

        rhythm = None
        if sec.get("rhythm"):
            try:
                rhythm = RhythmBlock.from_dict(sec["rhythm"])
            except Exception:
                pass

        ts = tuple(sec.get("time_signature", [4, 4]))
        sections.append(ArrangementSection(
            name=sec.get("name", "section"),
            bars=sec.get("bars", 8),
            tempo=float(sec.get("tempo", data.get("tempo", 120))),
            time_signature=ts,
            progression=prog,
            melody=melody,
            bass=bass,
            rhythm=rhythm,
        ))

    return CompositionPlan(
        title=data.get("title", "Untitled"),
        genre=data.get("genre", ""),
        key=data.get("key", "C"),
        mode=data.get("mode", "major"),
        tempo=float(data.get("tempo", 120)),
        time_signature=tuple(data.get("time_signature", [4, 4])),
        sections=sections,
        description=data.get("description", ""),
    )
