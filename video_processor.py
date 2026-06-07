# ============================================================
# video_processor.py  —  Council Video Analysis Pipeline
# ============================================================
# Processes video files to extract:
#   1. Full transcript with timestamps (via faster-whisper)
#   2. Key frame descriptions (via LLaVA/Moondream via Ollama)
#   3. Vibe/style analysis (via council personalities)
#
# Dependencies:
#   pip install faster-whisper
#   conda install ffmpeg -c conda-forge   (or system ffmpeg)
#   ollama pull llava:7b                  (for frame description)
#   ollama pull moondream                 (smaller alternative)
#
# All processing is local — nothing leaves your machine.
# ============================================================

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ── Optional: faster-whisper ────────────────────────────────
try:
    from faster_whisper import WhisperModel as _WhisperModel
    _WHISPER_OK = True
except ImportError:
    _WhisperModel = None
    _WHISPER_OK = False

# Module-level Whisper cache — keyed by (model_size, device) so switching
# model size or device still triggers a fresh load, but repeated calls with
# the same config reuse the already-loaded model (saves 2-10s per video).
_WHISPER_CACHE: Dict[Tuple[str, str], Any] = {}


def _get_whisper_model(model_size: str, device: str) -> Any:
    """Return a cached WhisperModel, loading it only when config changes."""
    key = (model_size, device)
    if key not in _WHISPER_CACHE:
        _WHISPER_CACHE[key] = _WhisperModel(
            model_size,
            device=device,
            compute_type="float16" if device == "cuda" else "int8",
        )
    return _WHISPER_CACHE[key]


def unload_whisper() -> None:
    """Free all cached Whisper models (call when done processing videos)."""
    _WHISPER_CACHE.clear()


# ============================================================
# Video queue item
# ============================================================

@dataclass
class VideoQueueItem:
    """
    A single entry in the processing queue.

    video_type  : "raw"    — unedited footage, full analysis (roast + edit suggestions)
                  "edited" — finished cut, QC-only (audio loudness, visual polish, algorithm)
                  "custom" — uses the GUI's current options panel settings
    status      : "queued" | "processing" | "done" | "error" | "skipped"
    """
    path:              str
    video_type:        str   = "raw"      # raw | edited | custom
    label:             str   = ""         # friendly display name (defaults to filename)
    status:            str   = "queued"   # queued | processing | done | error | skipped
    added_at:          str   = ""         # ISO timestamp when enqueued
    result_path:       str   = ""         # vault path of saved VideoAnalysis JSON
    error_msg:         str   = ""         # populated on failure
    duration_s:        float = 0.0        # filled in after processing

    def __post_init__(self):
        if not self.label:
            self.label = Path(self.path).name
        if not self.added_at:
            self.added_at = datetime.now().isoformat(timespec="seconds")

    @property
    def status_icon(self) -> str:
        return {
            "queued":     "⏳",
            "processing": "🔄",
            "done":       "✓",
            "error":      "✗",
            "skipped":    "⏭",
        }.get(self.status, "?")

    @property
    def type_icon(self) -> str:
        return {"raw": "🎥 raw", "edited": "✂ edited", "custom": "⚙ custom"}.get(
            self.video_type, self.video_type)

    def to_dict(self) -> Dict:
        return {
            "path":       self.path,       "video_type": self.video_type,
            "label":      self.label,      "status":     self.status,
            "added_at":   self.added_at,   "result_path":self.result_path,
            "error_msg":  self.error_msg,  "duration_s": self.duration_s,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "VideoQueueItem":
        return cls(
            path        = d.get("path", ""),
            video_type  = d.get("video_type", "raw"),
            label       = d.get("label", ""),
            status      = d.get("status", "queued"),
            added_at    = d.get("added_at", ""),
            result_path = d.get("result_path", ""),
            error_msg   = d.get("error_msg", ""),
            duration_s  = d.get("duration_s", 0.0),
        )


# ── Per-type analysis flag presets ───────────────────────────
VIDEO_TYPE_PRESETS: Dict[str, Dict] = {
    "raw": {
        # Full treatment — this is unedited footage, needs everything
        "do_frames":          True,
        "do_audio_analysis":  True,
        "do_energy_profile":  True,
        "do_visual_analysis": True,
        "do_edit_suggestions":True,
        "do_roast":           True,
        "frame_interval_s":   10,
        "max_frames":         20,
    },
    "edited": {
        # QC pass only — the edit is done, focus on final output quality
        "do_frames":          True,
        "do_audio_analysis":  True,    # loudness / clipping check for export
        "do_energy_profile":  False,   # not useful for a finished cut
        "do_visual_analysis": True,    # final colour / exposure check
        "do_edit_suggestions":False,   # already edited
        "do_roast":           False,   # not helpful on locked picture
        "frame_interval_s":   15,
        "max_frames":         15,
    },
    "custom": {
        # GUI options panel controls everything — placeholder, overridden at runtime
        "do_frames":          True,
        "do_audio_analysis":  True,
        "do_energy_profile":  True,
        "do_visual_analysis": True,
        "do_edit_suggestions":True,
        "do_roast":           True,
        "frame_interval_s":   10,
        "max_frames":         20,
    },
}


# ── Optional: Pillow for frame loading ──────────────────────
try:
    from PIL import Image as _PILImage
    _PIL_OK = True
except ImportError:
    _PILImage = None
    _PIL_OK = False


# ============================================================
# Data classes
# ============================================================

@dataclass
class TranscriptSegment:
    start: float          # seconds
    end: float
    text: str
    speaker: str = ""     # future: diarization

    @property
    def timestamp(self) -> str:
        def _fmt(s: float) -> str:
            td = timedelta(seconds=int(s))
            return str(td)
        return f"[{_fmt(self.start)} → {_fmt(self.end)}]"

    def to_dict(self) -> Dict:
        return {"start": self.start, "end": self.end,
                "text": self.text, "speaker": self.speaker}


@dataclass
class FrameDescription:
    timestamp_s: float
    frame_path: str
    description: str
    model: str = ""

    def to_dict(self) -> Dict:
        return {"timestamp_s": self.timestamp_s,
                "description": self.description,
                "model": self.model}


@dataclass
class AudioQualityReport:
    """Results from ffmpeg audio analysis filters."""
    # astats output
    rms_db: float                  = 0.0
    peak_db: float                 = 0.0
    dynamic_range_db: float        = 0.0
    crest_factor: float            = 0.0
    # ebur128 output
    integrated_lufs: float         = 0.0   # target: -16 to -14 LUFS for YouTube
    loudness_range_lu: float       = 0.0   # LRA
    true_peak_dbtp: float          = 0.0   # target: ≤ -1.0 dBTP
    # silencedetect output
    silence_gaps: List            = field(default_factory=list)  # [(start, end, duration)]
    total_silence_s: float         = 0.0
    longest_silence_s: float       = 0.0
    silence_count: int             = 0
    # derived
    noise_floor_db: float          = 0.0
    has_clipping: bool             = False
    is_too_quiet: bool             = False
    is_normalisation_needed: bool  = False

    def to_dict(self) -> Dict:
        return {
            "rms_db":                  self.rms_db,
            "peak_db":                 self.peak_db,
            "dynamic_range_db":        self.dynamic_range_db,
            "integrated_lufs":         self.integrated_lufs,
            "loudness_range_lu":       self.loudness_range_lu,
            "true_peak_dbtp":          self.true_peak_dbtp,
            "silence_count":           self.silence_count,
            "total_silence_s":         self.total_silence_s,
            "longest_silence_s":       self.longest_silence_s,
            "has_clipping":            self.has_clipping,
            "is_too_quiet":            self.is_too_quiet,
            "is_normalisation_needed": self.is_normalisation_needed,
            "silence_gaps":            self.silence_gaps,
        }


@dataclass
class EnergyPoint:
    """A time window classified by energy level."""
    start_s: float
    end_s:   float
    wps:     float    # words per second in window
    score:   float    # 0.0 (dead) → 1.0 (peak energy)
    label:   str      # "high" | "normal" | "low" | "dead"
    note:    str = ""

    def timecode(self) -> str:
        def _fmt(s: float) -> str:
            td = timedelta(seconds=int(s))
            return str(td)
        return f"[{_fmt(self.start_s)} → {_fmt(self.end_s)}]"

    def to_dict(self) -> Dict:
        return {"start_s": self.start_s, "end_s": self.end_s,
                "wps": round(self.wps, 2), "score": round(self.score, 2),
                "label": self.label, "note": self.note}


@dataclass
class VisualIssue:
    """A visual quality issue detected from frame analysis."""
    timestamp_s: float
    issue_type:  str    # "dark" | "blown_out" | "black_bars" | "motion_blur" | "jitter"
    severity:    str    # "minor" | "moderate" | "severe"
    description: str

    def to_dict(self) -> Dict:
        return {"timestamp_s": self.timestamp_s, "issue_type": self.issue_type,
                "severity": self.severity, "description": self.description}


@dataclass
class EditSuggestion:
    """A concrete actionable edit recommendation."""
    suggestion_type: str           # "trim_start" | "trim_end" | "cut_section" |
                                   # "audio_filter" | "visual_filter" | "crop"
    priority:        str           # "high" | "medium" | "low"
    timecode:        Optional[str] = None
    description:     str           = ""
    ffmpeg_snippet:  Optional[str] = None  # ready-to-paste ffmpeg filter/flag

    def to_dict(self) -> Dict:
        return {"type": self.suggestion_type, "priority": self.priority,
                "timecode": self.timecode, "description": self.description,
                "ffmpeg": self.ffmpeg_snippet}


@dataclass
class RoastReport:
    """The Peasant's brutal honest critique of the spoken content."""
    grade:            str              = "?"    # A / B / C / D / F
    roast_text:       str              = ""     # full roast body
    boring_sections:  List[Tuple]     = field(default_factory=list)   # [(timecode, reason)]
    filler_word_hits: Dict[str, int]  = field(default_factory=dict)   # {word: count}
    logic_issues:     List[str]       = field(default_factory=list)
    clarity_issues:   List[str]       = field(default_factory=list)
    positive_notes:   List[str]       = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "grade":            self.grade,
            "roast_text":       self.roast_text,
            "boring_sections":  self.boring_sections,
            "filler_word_hits": self.filler_word_hits,
            "logic_issues":     self.logic_issues,
            "clarity_issues":   self.clarity_issues,
            "positive_notes":   self.positive_notes,
        }


# ============================================================
# Video context detection
# ============================================================

# Heuristic keyword banks used when no AI model is available
_CONTENT_KEYWORDS: Dict[str, List[str]] = {
    "gaming": [
        "gameplay", "let's play", "lets play", "game over", "respawn",
        "health bar", "inventory", "quest", "boss", "level up", "loot",
        "controller", "spawn", "kill", "death screen", "checkpoint",
        "cutscene", "npc", "fps", "moba", "rpg", "speedrun", "hitbox",
        "cooldown", "ability", "ultimate", "esports", "competitive",
    ],
    "tutorial": [
        "how to", "step by step", "in this tutorial", "today we learn",
        "guide", "walkthrough", "install", "configure", "setting up",
        "beginners", "advanced", "tips and tricks",
    ],
    "vlog": [
        "day in my life", "vlog", "daily routine", "morning routine",
        "come with me", "week in my life", "storytime",
    ],
    "review": [
        "review", "unboxing", "first impressions", "rating out of",
        "worth it", "should you buy", "pros and cons",
    ],
    "podcast": [
        "welcome back to", "today's episode", "my guest", "host",
        "podcast", "interview", "join me today",
    ],
    "coding": [
        "function", "variable", "class ", "import", "debug", "api",
        "framework", "library", "compiler", "syntax", "git",
    ],
    "fitness": [
        "workout", "reps", "sets", "exercise", "calories", "form",
        "muscle", "training", "gym", "cardio",
    ],
    "music": [
        "chord", "melody", "beat", "tempo", "rhythm", "lyrics",
        "mixing", "mastering", "sample", "track",
    ],
}


@dataclass
class VideoContext:
    """
    Detected context/topic for a video.  Produced once, after transcription,
    and then injected into every subsequent AI critique prompt so that models
    don't confuse domain-specific vocabulary for errors.
    """
    content_type:  str       = "unknown"   # gaming | tutorial | vlog | review | ...
    topic:         str       = ""          # 1-sentence description of what the video is about
    domain_terms:  List[str] = field(default_factory=list)  # domain-specific vocab to not flag
    creator_role:  str       = ""          # "playing a game while commentating", etc.
    detected_by:   str       = "heuristic" # "heuristic" | "ai"

    @property
    def preamble(self) -> str:
        """
        A compact block injected at the top of every AI prompt that reads
        this video's content.  Prevents models from misidentifying domain
        vocabulary as errors.
        """
        if self.content_type == "unknown" and not self.topic:
            return ""
        lines = [
            "═══ VIDEO CONTEXT (read before critiquing — this shapes everything) ═══",
            f"Content type : {self.content_type}",
        ]
        if self.topic:
            lines.append(f"Topic        : {self.topic}")
        if self.creator_role:
            lines.append(f"Creator role : {self.creator_role}")
        if self.domain_terms:
            lines.append(
                "Domain vocab : "
                + ", ".join(f'"{t}"' for t in self.domain_terms[:12])
                + "  ← treat these as CORRECT in this context")
        lines.append(
            "IMPORTANT    : Do NOT flag domain-specific terminology as unclear, "
            "confusing, or wrong.  Do NOT assume references to real-world objects "
            "or proper nouns are errors — they may be in-game/in-context terminology.")
        lines.append("═" * 68)
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "content_type": self.content_type,
            "topic":        self.topic,
            "domain_terms": self.domain_terms,
            "creator_role": self.creator_role,
            "detected_by":  self.detected_by,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "VideoContext":
        return cls(
            content_type = d.get("content_type", "unknown"),
            topic        = d.get("topic",        ""),
            domain_terms = d.get("domain_terms", []),
            creator_role = d.get("creator_role", ""),
            detected_by  = d.get("detected_by",  "heuristic"),
        )


def detect_video_context(
    transcript_text: str,
    filename:        str,
    frame_descriptions: Optional[List] = None,   # List[FrameDescription]
    personality_model: Any = None,               # optional AI pass
    progress_cb: Optional[Callable[[str], None]] = None,
) -> VideoContext:
    """
    Detect what a video is about so AI critique models don't misinterpret
    domain-specific vocabulary.

    Two-pass strategy:
      1. Fast heuristic pass — keyword matching on filename + transcript.
         Instant, no API call required.
      2. AI refinement pass (if personality_model provided) — a short single
         call to get a precise content type, topic, and domain vocab list.
         Uses only the first ~500 chars of transcript for speed.
    """
    def _log(m): progress_cb(m) if progress_cb else None

    text_lower = (filename + " " + transcript_text).lower()

    # ── Pass 1: heuristic ────────────────────────────────────────────
    scores: Dict[str, int] = {k: 0 for k in _CONTENT_KEYWORDS}
    for content_type, keywords in _CONTENT_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                scores[content_type] += 1

    best_type  = max(scores, key=lambda k: scores[k])
    best_score = scores[best_type]
    h_type     = best_type if best_score >= 2 else "unknown"

    ctx = VideoContext(
        content_type = h_type,
        detected_by  = "heuristic",
    )

    # ── Pass 2: AI refinement ─────────────────────────────────────────
    if personality_model and transcript_text.strip():
        _log("  🔍 Context detection (AI pass) ...")
        excerpt = transcript_text[:600].strip()
        if len(transcript_text) > 600:
            excerpt += " [...]"

        frame_hint = ""
        if frame_descriptions:
            descs = [fd.description for fd in frame_descriptions[:4]
                     if getattr(fd, "description", "")]
            if descs:
                frame_hint = (
                    "\nVISUAL FRAMES (first few):\n"
                    + "\n".join(f"  - {d[:120]}" for d in descs))

        detect_prompt = (
            "You are a video content classifier. Identify what this video is about "
            "so that a critic can avoid misinterpreting domain-specific vocabulary.\n\n"
            f"FILENAME: {filename}\n\n"
            f"TRANSCRIPT EXCERPT:\n{excerpt}\n"
            f"{frame_hint}\n\n"
            "Respond in this EXACT format (one line per field, nothing else):\n"
            "CONTENT TYPE: <gaming | tutorial | vlog | review | podcast | coding | "
            "fitness | music | educational | other>\n"
            "TOPIC: <one sentence — what is this video specifically about?>\n"
            "DOMAIN TERMS: <up to 12 comma-separated domain-specific words/phrases "
            "that are correct in this context and should NOT be flagged as errors>\n"
            "CREATOR ROLE: <one phrase — what is the creator doing? "
            "e.g. 'playing a strategy game while commentating'>"
        )
        try:
            resp = personality_model.respond(detect_prompt, max_tokens=300)
            def _field(key: str) -> str:
                m = re.search(rf"{key}:\s*(.+)", resp, re.IGNORECASE)
                return m.group(1).strip() if m else ""

            ai_type   = _field("CONTENT TYPE").lower().split()[0] if _field("CONTENT TYPE") else h_type
            ai_topic  = _field("TOPIC")
            ai_role   = _field("CREATOR ROLE")
            ai_terms_raw = _field("DOMAIN TERMS")
            ai_terms  = [t.strip().strip('"\'') for t in ai_terms_raw.split(",")
                         if t.strip()] if ai_terms_raw else []

            ctx = VideoContext(
                content_type = ai_type or h_type,
                topic        = ai_topic,
                domain_terms = ai_terms,
                creator_role = ai_role,
                detected_by  = "ai",
            )
            _log(f"  ✓ Context: [{ctx.content_type}] {ctx.topic[:80] if ctx.topic else '—'}")
            if ctx.domain_terms:
                _log(f"    Domain vocab: {', '.join(ctx.domain_terms[:6])}")
        except Exception as e:
            _log(f"  ⚠ Context AI pass failed ({e}) — using heuristic result")

    elif h_type != "unknown":
        _log(f"  ✓ Context (heuristic): {h_type}")

    return ctx


@dataclass
class VideoAnalysis:
    video_path: str
    duration_s: float = 0.0
    transcript: List[TranscriptSegment] = field(default_factory=list)
    frame_descriptions: List[FrameDescription] = field(default_factory=list)
    vibe_summary: str = ""
    style_notes: List[str] = field(default_factory=list)
    vocabulary_notes: str = ""
    pacing_notes: str = ""
    processed_at: str = ""
    whisper_model: str = ""
    errors: List[str] = field(default_factory=list)
    # ── New extended analysis fields ──────────────────────────
    audio_quality:    Optional[AudioQualityReport] = None
    energy_profile:   List[EnergyPoint]            = field(default_factory=list)
    visual_issues:    List[VisualIssue]             = field(default_factory=list)
    edit_suggestions: List[EditSuggestion]          = field(default_factory=list)
    roast:            Optional[RoastReport]         = None
    video_context:    Optional[VideoContext]        = None   # detected content type/topic
    algorithm_notes:  str                           = ""     # Algorithm retention/packaging critique
    coach_notes:      str                           = ""     # Coach delivery/pacing critique
    cutter_notes:     str                           = ""     # Cutter timecoded edit decisions

    @property
    def full_transcript_text(self) -> str:
        return "\n".join(
            f"{s.timestamp}  {s.text.strip()}"
            for s in self.transcript
        )

    @property
    def plain_transcript_text(self) -> str:
        """Just the words, no timestamps — for style analysis."""
        return " ".join(s.text.strip() for s in self.transcript)

    def to_dict(self) -> Dict:
        return {
            "video_path":         self.video_path,
            "duration_s":         self.duration_s,
            "transcript":         [s.to_dict() for s in self.transcript],
            "frame_descriptions": [f.to_dict() for f in self.frame_descriptions],
            "vibe_summary":       self.vibe_summary,
            "style_notes":        self.style_notes,
            "vocabulary_notes":   self.vocabulary_notes,
            "pacing_notes":       self.pacing_notes,
            "processed_at":       self.processed_at,
            "whisper_model":      self.whisper_model,
            "errors":             self.errors,
            # extended
            "audio_quality":      self.audio_quality.to_dict() if self.audio_quality else None,
            "energy_profile":     [e.to_dict() for e in self.energy_profile],
            "visual_issues":      [v.to_dict() for v in self.visual_issues],
            "edit_suggestions":   [s.to_dict() for s in self.edit_suggestions],
            "roast":              self.roast.to_dict() if self.roast else None,
            "video_context":      self.video_context.to_dict() if self.video_context else None,
            "algorithm_notes":    self.algorithm_notes,
            "coach_notes":        self.coach_notes,
        }


# ============================================================
# FFmpeg utilities
# ============================================================

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _ffprobe_duration(video_path: str) -> float:
    """Get video duration in seconds using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            video_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return float(data.get("format", {}).get("duration", 0))
    except Exception:
        pass
    return 0.0


def _extract_audio(video_path: str, out_wav: str,
                   progress_cb: Optional[Callable[[str], None]] = None) -> bool:
    """Extract audio track from video as 16kHz mono WAV for Whisper."""
    if not _ffmpeg_available():
        if progress_cb:
            progress_cb("✗ FFmpeg not found — install with: conda install ffmpeg -c conda-forge")
        return False
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",                    # no video
        "-acodec", "pcm_s16le",   # 16-bit PCM
        "-ar", "16000",           # 16kHz sample rate (Whisper optimal)
        "-ac", "1",               # mono
        out_wav,
    ]
    if progress_cb:
        progress_cb(f"  Extracting audio → {Path(out_wav).name} ...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            if progress_cb:
                progress_cb(f"  ✗ FFmpeg error: {result.stderr[:200]}")
            return False
        if progress_cb:
            progress_cb(f"  ✓ Audio extracted ({Path(out_wav).stat().st_size // 1024} KB)")
        return True
    except subprocess.TimeoutExpired:
        if progress_cb:
            progress_cb("  ✗ Audio extraction timed out")
        return False
    except Exception as e:
        if progress_cb:
            progress_cb(f"  ✗ Audio extraction error: {e}")
        return False


def _extract_frames(video_path: str, out_dir: str,
                    interval_s: int = 10,
                    max_frames: int = 30,
                    progress_cb: Optional[Callable[[str], None]] = None) -> List[Tuple[float, str]]:
    """
    Extract one frame every interval_s seconds from the video.
    Returns list of (timestamp_seconds, frame_path) tuples.
    """
    if not _ffmpeg_available():
        return []

    Path(out_dir).mkdir(parents=True, exist_ok=True)

    if progress_cb:
        progress_cb(f"  Extracting frames every {interval_s}s ...")

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"fps=1/{interval_s}",
        "-vframes", str(max_frames),
        "-q:v", "3",              # quality 1-31, lower=better
        "-f", "image2",
        os.path.join(out_dir, "frame_%04d.jpg"),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            if progress_cb:
                progress_cb(f"  ✗ Frame extraction error: {result.stderr[:200]}")
            return []
    except Exception as e:
        if progress_cb:
            progress_cb(f"  ✗ Frame extraction error: {e}")
        return []

    # Collect extracted frames with timestamps
    frames: List[Tuple[float, str]] = []
    frame_files = sorted(Path(out_dir).glob("frame_*.jpg"))
    for i, fp in enumerate(frame_files):
        ts = i * interval_s
        frames.append((float(ts), str(fp)))

    if progress_cb:
        progress_cb(f"  ✓ Extracted {len(frames)} frames")
    return frames


# ============================================================
# Whisper transcription
# ============================================================

def transcribe_audio(
    wav_path: str,
    model_size: str = "base",
    device: str = "cuda",
    progress_cb: Optional[Callable[[str], None]] = None,
) -> List[TranscriptSegment]:
    """
    Transcribe a WAV file using faster-whisper.
    Returns list of TranscriptSegment with timestamps.
    """
    if not _WHISPER_OK:
        if progress_cb:
            progress_cb("✗ faster-whisper not installed — run: pip install faster-whisper")
        return []

    cached = (model_size, device) in _WHISPER_CACHE
    if progress_cb:
        status = "reusing cached" if cached else "loading"
        progress_cb(f"  Whisper ({model_size}) on {device} — {status} ...")

    try:
        model = _get_whisper_model(model_size, device)
    except Exception as e:
        if progress_cb:
            progress_cb(f"  ✗ Whisper load error: {e}")
            progress_cb("    Trying CPU fallback ...")
        try:
            model = _get_whisper_model(model_size, "cpu")
        except Exception as e2:
            if progress_cb:
                progress_cb(f"  ✗ CPU fallback also failed: {e2}")
            return []

    if progress_cb:
        progress_cb("  Transcribing — this may take a minute ...")

    try:
        segments, info = model.transcribe(
            wav_path,
            beam_size=5,
            language=None,          # auto-detect
            word_timestamps=False,
            vad_filter=True,        # skip silent sections
            vad_parameters={"min_silence_duration_ms": 500},
        )

        result: List[TranscriptSegment] = []
        for seg in segments:
            result.append(TranscriptSegment(
                start=seg.start,
                end=seg.end,
                text=seg.text,
            ))
            if progress_cb and len(result) % 10 == 0:
                progress_cb(f"  ... {len(result)} segments transcribed")

        if progress_cb:
            lang = getattr(info, "language", "?")
            prob = getattr(info, "language_probability", 0)
            progress_cb(
                f"  ✓ Transcribed {len(result)} segments "
                f"(language: {lang}, confidence: {prob:.0%})"
            )
        return result

    except Exception as e:
        if progress_cb:
            progress_cb(f"  ✗ Transcription error: {e}")
        return []


# ============================================================
# Frame description via Ollama vision models
# ============================================================

def _image_to_base64(image_path: str) -> Optional[str]:
    """Load an image and encode as base64."""
    try:
        if _PIL_OK:
            # Resize to max 512px on longest side to keep tokens manageable
            img = _PILImage.open(image_path)
            img.thumbnail((512, 512), _PILImage.LANCZOS)
            import io as _io
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        else:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None


def describe_frame(
    image_path: str,
    ollama_host: str = "http://localhost:11434",
    model: str = "llava:7b",
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Send a frame to a vision model via Ollama and get a description.
    Returns description string or empty string on failure.
    """
    import urllib.request
    import urllib.error

    b64 = _image_to_base64(image_path)
    if not b64:
        return ""

    prompt = (
        "Describe this video frame briefly. Focus on: "
        "what is shown on screen, any text visible, "
        "the setting, and what the person (if present) appears to be doing. "
        "Keep it to 2-3 sentences."
    )

    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 150},
    }).encode("utf-8")

    url = ollama_host.rstrip("/") + "/api/generate"
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "").strip()
    except Exception as e:
        if progress_cb:
            progress_cb(f"    ✗ Frame description error: {e}")
        return ""


def describe_frames(
    frames: List[Tuple[float, str]],
    ollama_host: str = "http://localhost:11434",
    model: str = "llava:7b",
    progress_cb: Optional[Callable[[str], None]] = None,
    max_workers: int = 4,
) -> List[FrameDescription]:
    """
    Describe a list of (timestamp, path) frame tuples in parallel.
    Frames are independent so we use a thread pool for 4-6x speedup.
    Results are returned in the original timestamp order.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    if not frames:
        return []

    if progress_cb:
        progress_cb(f"  Describing {len(frames)} frames with {model} "
                    f"({min(max_workers, len(frames))} workers) ...")

    # Thread-safe counter for progress reporting
    _done_count = 0
    _lock = threading.Lock()

    def _describe_one(item: Tuple[int, float, str]) -> Tuple[int, FrameDescription]:
        nonlocal _done_count
        idx, ts, path = item
        desc = describe_frame(path, ollama_host=ollama_host, model=model)
        with _lock:
            _done_count += 1
            if progress_cb:
                td = timedelta(seconds=int(ts))
                progress_cb(f"  [{_done_count}/{len(frames)}] Frame at {td} ✓")
        return idx, FrameDescription(timestamp_s=ts, frame_path=path,
                                     description=desc, model=model)

    workers = min(max_workers, len(frames))
    indexed = [(i, ts, path) for i, (ts, path) in enumerate(frames)]
    result_map: dict = {}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_describe_one, item): item[0] for item in indexed}
        for future in as_completed(futures):
            try:
                idx, fd = future.result()
                result_map[idx] = fd
            except Exception as e:
                orig_idx = futures[future]
                ts, path = frames[orig_idx]
                result_map[orig_idx] = FrameDescription(
                    timestamp_s=ts, frame_path=path, description="", model=model)
                if progress_cb:
                    progress_cb(f"  ✗ Frame {orig_idx} error: {e}")

    results = [result_map[i] for i in range(len(frames))]
    if progress_cb:
        described = sum(1 for r in results if r.description)
        progress_cb(f"  ✓ Described {described}/{len(frames)} frames")
    return results


# ============================================================
# Council vibe analysis
# ============================================================

def analyse_vibe(
    analysis: VideoAnalysis,
    personality_model: Any,         # PersonalityModel with .respond()
    content_style_manager: Any,     # ContentStyleManager
    progress_cb: Optional[Callable[[str], None]] = None,
    video_context: Optional[VideoContext] = None,
) -> VideoAnalysis:
    """
    Run the council's Content Creator personality over the transcript
    to extract style notes, vocabulary patterns, and vibe summary.
    Saves findings to ContentStyleManager for cross-session learning.
    """
    if not analysis.transcript:
        if progress_cb:
            progress_cb("  ✗ No transcript to analyse")
        return analysis

    plain = analysis.plain_transcript_text
    if not plain.strip():
        return analysis

    # Truncate to ~4000 chars to stay within context
    sample = plain[:4000]
    if len(plain) > 4000:
        sample += "\n\n[... transcript continues ...]"

    # Build frame context if available
    frame_ctx = ""
    if analysis.frame_descriptions:
        frame_lines = []
        for fd in analysis.frame_descriptions[:10]:
            td = timedelta(seconds=int(fd.timestamp_s))
            if fd.description:
                frame_lines.append(f"  [{td}] {fd.description}")
        if frame_lines:
            frame_ctx = "\nVISUAL CONTEXT (frame descriptions):\n" + "\n".join(frame_lines)

    if progress_cb:
        progress_cb("  Analysing vibe with Content Creator ...")

    _ctx_preamble = (video_context.preamble + "\n\n") if video_context and video_context.preamble else ""

    vibe_prompt = f"""{_ctx_preamble}You are analysing a video transcript to learn a creator's style.
Extract actionable style insights the council can use when writing future scripts for this creator.

VIDEO TRANSCRIPT SAMPLE:
{sample}
{frame_ctx}

Analyse and respond in this EXACT format:

VIBE SUMMARY:
<2-3 sentences describing the overall feel, energy, and personality of this creator>

VOCABULARY & PHRASING:
<What words/phrases do they favour? Any signature expressions? Formal or casual? Short or long sentences?>

PACING & STRUCTURE:
<How do they structure ideas? Do they meander or stay tight? How do they transition between topics?>

HUMOUR STYLE:
<Do they use humour? How? Self-deprecating, deadpan, absurdist, observational? Any patterns?>

WHAT TO REPLICATE:
- <specific thing that should be carried into future scripts>
- <another specific thing>
- <third thing if relevant>

WHAT TO AVOID:
- <anything that seemed off or inconsistent with their natural voice>
"""

    try:
        response = personality_model.respond(vibe_prompt)
        analysis.vibe_summary = _extract_section(response, "VIBE SUMMARY")
        analysis.vocabulary_notes = _extract_section(response, "VOCABULARY & PHRASING")
        analysis.pacing_notes = _extract_section(response, "PACING & STRUCTURE")

        # Extract style notes to save
        replicate = _extract_bullets(response, "WHAT TO REPLICATE")
        avoid     = _extract_bullets(response, "WHAT TO AVOID")
        humour    = _extract_section(response, "HUMOUR STYLE")

        analysis.style_notes = replicate + avoid

        # Save to ContentStyleManager for cross-session learning
        if analysis.vibe_summary:
            content_style_manager.add_style_note(
                f"[from video] {analysis.vibe_summary}", "tone"
            )
        if analysis.vocabulary_notes:
            content_style_manager.add_style_note(
                f"[from video] {analysis.vocabulary_notes}", "tone"
            )
        if analysis.pacing_notes:
            content_style_manager.add_style_note(
                f"[from video] {analysis.pacing_notes}", "pacing"
            )
        if humour:
            content_style_manager.add_style_note(
                f"[from video] Humour style: {humour}", "tone"
            )
        for note in replicate:
            content_style_manager.add_style_note(
                f"[from video — replicate] {note}", "general"
            )
        for note in avoid:
            content_style_manager.add_style_note(
                f"[from video — avoid] {note}", "general"
            )

        if progress_cb:
            progress_cb(f"  ✓ Vibe analysis complete — {len(analysis.style_notes)} style notes saved")

    except Exception as e:
        analysis.errors.append(f"Vibe analysis error: {e}")
        if progress_cb:
            progress_cb(f"  ✗ Vibe analysis error: {e}")

    return analysis


def _extract_section(text: str, header: str) -> str:
    """Extract content under a section header."""
    pattern = re.compile(
        rf"{re.escape(header)}:\s*\n(.*?)(?=\n[A-Z &]+:|\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(text)
    if m:
        return m.group(1).strip()
    return ""


def _extract_bullets(text: str, header: str) -> List[str]:
    """Extract bullet points under a section header."""
    section = _extract_section(text, header)
    if not section:
        return []
    bullets = []
    for line in section.splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if line and len(line) > 10:
            bullets.append(line)
    return bullets


# ============================================================
# Audio quality analysis
# ============================================================

def analyse_audio_quality(
    wav_path: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> AudioQualityReport:
    """
    Run ffmpeg astats, ebur128, and silencedetect on the WAV file.
    Returns an AudioQualityReport with noise/loudness/silence data.
    """
    report = AudioQualityReport()
    if not _ffmpeg_available():
        return report

    def _log(m): progress_cb(m) if progress_cb else None

    # ── Pass 1: astats ─────────────────────────────────────────
    _log("  Audio quality — running astats ...")
    try:
        cmd = [
            "ffmpeg", "-y", "-i", wav_path,
            "-filter:a", "astats",
            "-f", "null", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        stderr = result.stderr

        def _ffparse(pattern: str, text: str, group: int = 1) -> Optional[float]:
            m = re.search(pattern, text)
            return float(m.group(group)) if m else None

        rms   = _ffparse(r"RMS level dB:\s*([-\d.]+)", stderr)
        peak  = _ffparse(r"Peak level dB:\s*([-\d.]+)", stderr)
        dr    = _ffparse(r"Dynamic range:\s*([\d.]+)",  stderr)
        crest = _ffparse(r"Crest factor:\s*([\d.]+)",   stderr)

        if rms   is not None: report.rms_db          = rms
        if peak  is not None: report.peak_db         = peak
        if dr    is not None: report.dynamic_range_db = dr
        if crest is not None: report.crest_factor    = crest

        # Clipping: peak very close to 0 dBFS
        if report.peak_db is not None and report.peak_db > -0.5:
            report.has_clipping = True

        # Too quiet: RMS below -40 dB
        if report.rms_db < -40:
            report.is_too_quiet = True

        # Noise floor estimate: RMS trough is a rough proxy
        trough = _ffparse(r"RMS trough dB:\s*([-\d.]+)", stderr)
        if trough is not None:
            report.noise_floor_db = trough

        _log(f"    RMS: {report.rms_db:.1f} dB  Peak: {report.peak_db:.1f} dB  "
             f"DR: {report.dynamic_range_db:.1f} dB")
    except Exception as e:
        _log(f"    ✗ astats error: {e}")

    # ── Pass 2: ebur128 loudness ───────────────────────────────
    _log("  Audio quality — running loudness analysis ...")
    try:
        cmd = [
            "ffmpeg", "-y", "-i", wav_path,
            "-filter:a", "ebur128=peak=true",
            "-f", "null", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        stderr = result.stderr

        il = re.search(r"I:\s*([-\d.]+)\s*LUFS",       stderr)
        lra= re.search(r"LRA:\s*([\d.]+)\s*LU",         stderr)
        tp = re.search(r"Peak:\s*([-\d.]+)\s*dBTP",     stderr)

        if il:  report.integrated_lufs   = float(il.group(1))
        if lra: report.loudness_range_lu = float(lra.group(1))
        if tp:  report.true_peak_dbtp    = float(tp.group(1))

        # Normalisation needed if not in -16 to -12 LUFS window
        if not (-18 <= report.integrated_lufs <= -12):
            report.is_normalisation_needed = True

        _log(f"    LUFS: {report.integrated_lufs:.1f}  LRA: {report.loudness_range_lu:.1f} LU  "
             f"TruePeak: {report.true_peak_dbtp:.1f} dBTP")
    except Exception as e:
        _log(f"    ✗ ebur128 error: {e}")

    # ── Pass 3: silence detection ──────────────────────────────
    _log("  Audio quality — detecting silence gaps ...")
    try:
        cmd = [
            "ffmpeg", "-y", "-i", wav_path,
            "-filter:a", "silencedetect=n=-40dB:d=0.75",
            "-f", "null", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        stderr = result.stderr

        starts   = [float(m.group(1)) for m in
                    re.finditer(r"silence_start:\s*([\d.]+)", stderr)]
        ends     = [float(m.group(1)) for m in
                    re.finditer(r"silence_end:\s*([\d.]+)",   stderr)]
        durations= [float(m.group(1)) for m in
                    re.finditer(r"silence_duration:\s*([\d.]+)", stderr)]

        gaps = []
        for i, (s, e, d) in enumerate(zip(starts, ends, durations)):
            gaps.append((round(s, 2), round(e, 2), round(d, 2)))

        report.silence_gaps    = gaps
        report.silence_count   = len(gaps)
        report.total_silence_s = round(sum(d for _, _, d in gaps), 2)
        report.longest_silence_s = round(
            max((d for _, _, d in gaps), default=0.0), 2)

        _log(f"    Silence gaps: {len(gaps)}  "
             f"total {report.total_silence_s:.1f}s  "
             f"longest {report.longest_silence_s:.1f}s")
    except Exception as e:
        _log(f"    ✗ silence detect error: {e}")

    return report


# ============================================================
# Energy profile
# ============================================================

def detect_energy_profile(
    transcript: List[TranscriptSegment],
    duration_s: float,
    window_s: float = 30.0,
) -> List[EnergyPoint]:
    """
    Slice the transcript into windows and compute words-per-second.
    Returns a list of EnergyPoint objects, labelled high/normal/low/dead.
    """
    if not transcript or duration_s <= 0:
        return []

    windows: List[EnergyPoint] = []
    start = 0.0

    while start < duration_s:
        end      = min(start + window_s, duration_s)
        in_win   = [s for s in transcript if s.start >= start and s.start < end]
        words    = sum(len(s.text.split()) for s in in_win)
        win_dur  = end - start
        wps      = words / win_dur if win_dur > 0 else 0.0

        # Scoring: typical conversational pace ~2-3 wps, heavy explanation ~1-2 wps
        if   wps >= 3.0:  label, score = "high",   min(1.0, wps / 4.5)
        elif wps >= 1.5:  label, score = "normal",  wps / 3.0
        elif wps >= 0.3:  label, score = "low",     wps / 3.0
        else:              label, score = "dead",    0.0

        note = ""
        if label == "dead":
            note = "⚠ No speech — dead air. Cut or add content."
        elif label == "low":
            note = "⚠ Very slow pacing. Consider tightening or cutting."
        elif label == "high":
            note = "✓ High energy / fast delivery."

        windows.append(EnergyPoint(
            start_s=start, end_s=end, wps=round(wps, 2),
            score=round(score, 3), label=label, note=note))
        start = end

    return windows


# ============================================================
# Visual issue detection from frames
# ============================================================

def detect_visual_issues(
    frame_descriptions: List[FrameDescription],
    frames_raw: Optional[List[Tuple[float, str]]] = None,
) -> List[VisualIssue]:
    """
    Detect dark frames, blown-out highlights, possible black bars,
    and motion blur by analysing pixel data (if Pillow available)
    and frame descriptions.
    """
    issues: List[VisualIssue] = []

    # ── Description-based heuristics ────────────────────────────
    dark_kw   = {"dark", "dim", "shadowy", "under", "hard to see", "barely visible"}
    bright_kw = {"over", "blown", "white", "blinding", "washed", "glare"}
    blur_kw   = {"blurry", "blur", "out of focus", "fuzzy", "motion blur"}
    jitter_kw = {"shaky", "shaking", "wobbling", "unstable", "handheld"}

    for fd in frame_descriptions:
        if not fd.description:
            continue
        desc_l = fd.description.lower()
        td = str(timedelta(seconds=int(fd.timestamp_s)))

        if any(k in desc_l for k in dark_kw):
            issues.append(VisualIssue(
                timestamp_s=fd.timestamp_s, issue_type="dark",
                severity="moderate",
                description=f"Frame at {td} appears dark/underexposed"))
        if any(k in desc_l for k in bright_kw):
            issues.append(VisualIssue(
                timestamp_s=fd.timestamp_s, issue_type="blown_out",
                severity="moderate",
                description=f"Frame at {td} may be overexposed/blown out"))
        if any(k in desc_l for k in blur_kw):
            issues.append(VisualIssue(
                timestamp_s=fd.timestamp_s, issue_type="motion_blur",
                severity="minor",
                description=f"Frame at {td} shows blur or focus issue"))
        if any(k in desc_l for k in jitter_kw):
            issues.append(VisualIssue(
                timestamp_s=fd.timestamp_s, issue_type="jitter",
                severity="moderate",
                description=f"Frame at {td} appears shaky/unstable"))

    # ── Pixel-level analysis (requires Pillow + raw frame paths) ─
    if _PIL_OK and frames_raw:
        import statistics
        prev_pixels: Optional[Any] = None
        brightness_vals: List[float] = []

        for ts, fp in frames_raw:
            if not os.path.exists(fp):
                continue
            try:
                img = _PILImage.open(fp).convert("RGB")
                w, h = img.size
                pixels = img.getdata()
                brightness = sum(0.299*r + 0.587*g + 0.114*b
                                 for r, g, b in pixels) / len(pixels)
                brightness_vals.append(brightness)
                td = str(timedelta(seconds=int(ts)))

                # Dark frame
                if brightness < 30:
                    severity = "severe" if brightness < 15 else "moderate"
                    issues.append(VisualIssue(
                        timestamp_s=ts, issue_type="dark",
                        severity=severity,
                        description=f"Frame at {td}: very low brightness "
                                    f"({brightness:.0f}/255)"))

                # Blown out
                if brightness > 230:
                    issues.append(VisualIssue(
                        timestamp_s=ts, issue_type="blown_out",
                        severity="moderate",
                        description=f"Frame at {td}: overexposed "
                                    f"({brightness:.0f}/255)"))

                # Black bar detection — check top/bottom 10% rows
                rows_top    = img.crop((0, 0, w, h // 10))
                rows_bottom = img.crop((0, h - h // 10, w, h))
                cols_left   = img.crop((0, 0, w // 10, h))
                cols_right  = img.crop((w - w // 10, 0, w, h))

                def _mean_brightness(im) -> float:
                    px = list(im.getdata())
                    if not px: return 128.0
                    return sum(0.299*r+0.587*g+0.114*b for r,g,b in px) / len(px)

                bar_bri = [_mean_brightness(p)
                           for p in (rows_top, rows_bottom, cols_left, cols_right)]
                if any(b < 18 for b in bar_bri):
                    issues.append(VisualIssue(
                        timestamp_s=ts, issue_type="black_bars",
                        severity="minor",
                        description=f"Frame at {td}: possible black bars/letterbox "
                                    f"— check crop setting"))

                # Motion/jitter estimation via frame diff
                if prev_pixels is not None:
                    img_sm    = img.resize((64, 36))
                    curr_data = list(img_sm.getdata())
                    diff = sum(abs(c[0]-p[0]) + abs(c[1]-p[1]) + abs(c[2]-p[2])
                               for c, p in zip(curr_data, prev_pixels)) / (64*36*3)
                    if diff > 60:
                        issues.append(VisualIssue(
                            timestamp_s=ts, issue_type="jitter",
                            severity="moderate" if diff > 90 else "minor",
                            description=f"Frame at {td}: large inter-frame motion "
                                        f"(Δ={diff:.0f}) — possible camera jitter"))
                    prev_pixels = list(img.resize((64, 36)).getdata())
                else:
                    prev_pixels = list(img.resize((64, 36)).getdata())

            except Exception:
                pass

    # Deduplicate by (issue_type, timestamp_s) keeping first occurrence
    seen: set = set()
    unique: List[VisualIssue] = []
    for vi in issues:
        key = (vi.issue_type, int(vi.timestamp_s))
        if key not in seen:
            seen.add(key)
            unique.append(vi)
    return sorted(unique, key=lambda x: x.timestamp_s)


# ============================================================
# Edit suggestion generation
# ============================================================

def generate_edit_suggestions(
    analysis: "VideoAnalysis",
) -> List[EditSuggestion]:
    """
    Aggregate all analysis findings into concrete, prioritised edit suggestions
    with ready-to-paste FFmpeg filter chains.
    """
    suggestions: List[EditSuggestion] = []
    vp = analysis.video_path

    # ── Temporal crop — trim dead air from start/end ────────────
    if analysis.transcript:
        first_word = analysis.transcript[0].start
        last_word  = analysis.transcript[-1].end
        if first_word > 3.0:
            suggestions.append(EditSuggestion(
                suggestion_type="trim_start",
                priority="high",
                timecode=f"0:00 → {timedelta(seconds=int(first_word))}",
                description=(f"No speech in first {first_word:.1f}s — trim the intro. "
                             f"Start video at ~{timedelta(seconds=int(first_word))}."),
                ffmpeg_snippet=(f'ffmpeg -ss {first_word:.2f} -i "{vp}" '
                                f'-c copy trimmed.mp4')))

        tail_dead = analysis.duration_s - last_word
        if tail_dead > 3.0:
            suggestions.append(EditSuggestion(
                suggestion_type="trim_end",
                priority="high",
                timecode=f"{timedelta(seconds=int(last_word))} → end",
                description=(f"No speech in last {tail_dead:.1f}s — trim the outro. "
                             f"End video at ~{timedelta(seconds=int(last_word))}."),
                ffmpeg_snippet=(f'ffmpeg -i "{vp}" '
                                f'-t {last_word:.2f} -c copy trimmed.mp4')))

    # ── Cut dead/low-energy sections ────────────────────────────
    dead_windows = [e for e in analysis.energy_profile if e.label == "dead"]
    for w in dead_windows:
        # Only suggest cutting if inside the video body (not the trim zones above)
        if w.start_s > 5 and w.end_s < analysis.duration_s - 5:
            suggestions.append(EditSuggestion(
                suggestion_type="cut_section",
                priority="high",
                timecode=w.timecode(),
                description=f"Dead air — no speech {w.timecode()}. Cut or add B-roll/music.",
                ffmpeg_snippet=None))  # multi-cut requires a proper edit list

    consecutive_low = 0
    for w in analysis.energy_profile:
        if w.label in ("low", "dead"):
            consecutive_low += 1
        else:
            consecutive_low = 0
        if consecutive_low >= 3:
            suggestions.append(EditSuggestion(
                suggestion_type="cut_section",
                priority="medium",
                timecode=w.timecode(),
                description=(f"Extended low-energy stretch ending at "
                             f"{timedelta(seconds=int(w.end_s))}. "
                             f"Consider cutting or speeding up (1.3-1.5x)."),
                ffmpeg_snippet=(f'ffmpeg -i "{vp}" '
                                f'-filter:v "setpts=0.75*PTS" '
                                f'-filter:a "atempo=1.333" sped_up.mp4')))
            break  # one suggestion for the whole stretch

    # ── Silence gap cuts ─────────────────────────────────────────
    aq = analysis.audio_quality
    if aq:
        long_silences = [(s, e, d) for s, e, d in aq.silence_gaps if d > 2.5]
        for s, e, d in long_silences[:6]:  # cap at 6 to avoid spam
            suggestions.append(EditSuggestion(
                suggestion_type="cut_section",
                priority="medium",
                timecode=f"{timedelta(seconds=int(s))} → {timedelta(seconds=int(e))}",
                description=f"Silence gap of {d:.1f}s — dead air, consider cutting.",
                ffmpeg_snippet=None))

        # ── Audio filter chain ────────────────────────────────────
        filters: List[str] = []
        notes:   List[str] = []

        if aq.noise_floor_db < -55:
            notes.append(f"Low noise floor ({aq.noise_floor_db:.1f} dB) — "
                         "background noise present.")
            filters.append("highpass=f=80")          # remove low-end rumble
            filters.append("afftdn=nf=-25")           # AI noise reduction

        if aq.has_clipping:
            notes.append(f"⚠ CLIPPING detected (peak {aq.peak_db:.1f} dB). "
                         "Reduce input gain before next recording.")
            filters.append("alimiter=level_in=0.9:level_out=0.9")

        if aq.is_too_quiet:
            notes.append(f"Audio is very quiet (RMS {aq.rms_db:.1f} dB). "
                         "Boost or normalise.")

        if aq.is_normalisation_needed:
            notes.append(f"Loudness {aq.integrated_lufs:.1f} LUFS — "
                         f"target is -14 LUFS for YouTube.")
            filters.append("loudnorm=I=-14:TP=-1:LRA=11")

        if aq.loudness_range_lu > 12:
            notes.append(f"High dynamic range ({aq.loudness_range_lu:.1f} LU) — "
                         "delivery may be uneven.")
            filters.append("acompressor=threshold=-20dB:ratio=3:attack=5:release=50")
            filters.append("dynaudnorm=p=0.9:m=100")

        if filters:
            chain = ",".join(filters)
            cmd   = f'ffmpeg -i "{vp}" -filter:a "{chain}" fixed_audio.mp4'
            desc  = ("Recommended audio filter chain:\n" +
                     "\n".join(f"  • {n}" for n in notes))
            suggestions.append(EditSuggestion(
                suggestion_type="audio_filter",
                priority="high" if aq.has_clipping else "medium",
                description=desc,
                ffmpeg_snippet=cmd))

        if aq.true_peak_dbtp > -1.0:
            suggestions.append(EditSuggestion(
                suggestion_type="audio_filter",
                priority="high",
                description=f"True peak {aq.true_peak_dbtp:.1f} dBTP exceeds -1 dBTP limit. "
                            f"Apply a true-peak limiter.",
                ffmpeg_snippet=(f'ffmpeg -i "{vp}" '
                                f'-filter:a "alimiter=level_in=1:level_out=0.891:'
                                f'limit=0.891:attack=5:release=50:asc=1" '
                                f'limited.mp4')))

    # ── Visual issue suggestions ──────────────────────────────────
    dark_frames  = [v for v in analysis.visual_issues if v.issue_type == "dark"]
    bright_frames= [v for v in analysis.visual_issues if v.issue_type == "blown_out"]
    jitter_count = sum(1 for v in analysis.visual_issues if v.issue_type == "jitter")
    bar_count    = sum(1 for v in analysis.visual_issues if v.issue_type == "black_bars")

    if len(dark_frames) >= 2:
        suggestions.append(EditSuggestion(
            suggestion_type="visual_filter",
            priority="medium",
            description=(f"{len(dark_frames)} dark frames detected — "
                         f"consider a brightness/contrast lift."),
            ffmpeg_snippet=(f'ffmpeg -i "{vp}" '
                            f'-filter:v "eq=brightness=0.05:contrast=1.1:saturation=1.05" '
                            f'brightened.mp4')))

    if len(bright_frames) >= 2:
        suggestions.append(EditSuggestion(
            suggestion_type="visual_filter",
            priority="medium",
            description=(f"{len(bright_frames)} overexposed frames — "
                         f"add exposure reduction."),
            ffmpeg_snippet=(f'ffmpeg -i "{vp}" '
                            f'-filter:v "eq=brightness=-0.05:contrast=0.95" '
                            f'exposure_fixed.mp4')))

    if jitter_count >= 3:
        suggestions.append(EditSuggestion(
            suggestion_type="visual_filter",
            priority="medium",
            description=(f"{jitter_count} jittery frames detected — "
                         f"apply video stabilisation."),
            ffmpeg_snippet=(f'ffmpeg -i "{vp}" '
                            f'-filter:v "vidstabdetect=shakiness=10:accuracy=15" '
                            f'-f null - && '
                            f'ffmpeg -i "{vp}" '
                            f'-filter:v "vidstabtransform=smoothing=30:input=transforms.trf" '
                            f'stabilised.mp4')))

    if bar_count >= 2:
        suggestions.append(EditSuggestion(
            suggestion_type="crop",
            priority="low",
            description=("Possible letterbox/pillarbox black bars detected. "
                         "Crop to remove — adjust w:h:x:y to your actual frame."),
            ffmpeg_snippet=(f'ffmpeg -i "{vp}" '
                            f'-filter:v "crop=iw:ih*0.9:0:ih*0.05" '
                            f'cropped.mp4')))

    # Sort by priority
    order = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda s: order.get(s.priority, 3))
    return suggestions


# ============================================================
# The Peasant Roast — brutal transcript critique
# ============================================================

_FILLER_WORDS = {
    "um", "uh", "er", "ah", "like", "you know", "basically",
    "literally", "actually", "kind of", "sort of", "i mean",
    "right", "okay so", "so yeah", "anyway", "whatever",
    "and stuff", "or something", "i guess", "i think", "just",
}

def _count_filler_words(text: str) -> Dict[str, int]:
    text_l = text.lower()
    counts: Dict[str, int] = {}
    for fw in _FILLER_WORDS:
        pattern = r'\b' + re.escape(fw) + r'\b'
        n = len(re.findall(pattern, text_l))
        if n > 0:
            counts[fw] = n
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def roast_transcript(
    analysis: "VideoAnalysis",
    writer_model: Any,
    sage_model: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    video_context: Optional[VideoContext] = None,
) -> RoastReport:
    """
    Have the council brutally critique the spoken content.
    Writer (the Peasant) delivers the roast; Sage adds logic analysis.
    Both are honest and unsparing — sugarcoating is explicitly forbidden.
    """
    def _log(m): progress_cb(m) if progress_cb else None

    report = RoastReport()
    if not analysis.transcript:
        report.roast_text = "No transcript available — nothing to roast."
        return report

    plain = analysis.plain_transcript_text
    timed = analysis.full_transcript_text
    dur   = timedelta(seconds=int(analysis.duration_s))

    # ── Count filler words ────────────────────────────────────────
    report.filler_word_hits = _count_filler_words(plain)
    total_fillers = sum(report.filler_word_hits.values())
    total_words   = len(plain.split())
    filler_pct    = (total_fillers / total_words * 100) if total_words else 0

    filler_summary = ""
    if report.filler_word_hits:
        top = list(report.filler_word_hits.items())[:8]
        filler_summary = (
            f"\nFILLER WORD COUNT ({total_fillers} hits, {filler_pct:.1f}% of words):\n" +
            "\n".join(f"  '{w}' — {n}×" for w, n in top)
        )

    # ── Energy context for boring section detection ────────────────
    dead_txt = ""
    dead_segs = [e for e in analysis.energy_profile
                 if e.label in ("dead", "low")]
    if dead_segs:
        dead_txt = "\n\nLOW/DEAD ENERGY SECTIONS (transcript analysis):\n"
        for e in dead_segs[:10]:
            dead_txt += f"  {e.timecode()} — {e.wps:.1f} wps ({e.label})\n"

    # ── Transcript sample (cap to ~6000 chars) ────────────────────
    sample = timed[:6000]
    if len(timed) > 6000:
        sample += "\n\n[... transcript continues — first 6000 chars shown ...]"

    # ── WRITER ROAST PROMPT ───────────────────────────────────────
    _ctx_preamble = (video_context.preamble + "\n\n") if video_context and video_context.preamble else ""
    _log("  🔥 Running Peasant Roast (Writer) ...")
    roast_prompt = f"""{_ctx_preamble}You are a brutally honest video editor and content critic — no gloves, no mercy, no politeness.
Your job is to roast this transcript and give the creator the hard truth they desperately need.

VIDEO STATS:
- Duration: {dur}
- Words spoken: {total_words:,}
- Average words/min: {(total_words / (analysis.duration_s / 60)):.0f}
{filler_summary}
{dead_txt}

TRANSCRIPT (with timestamps):
{sample}

Your task — be SPECIFIC, be BRUTAL, be HONEST. Reference actual timestamps and actual quotes.

Write your critique in this EXACT format:

OVERALL GRADE: [A/B/C/D/F]

THE ROAST:
<3–6 paragraphs of honest, unsparing critique. Name the actual problems. Quote back their own words against them. If it's bad, say it's bad. If they repeat themselves, quote it twice so they feel the pain. Call out every "um", "like", and "basically" by name. If a section is boring, explain WHY it's boring. This is not a time for compliments — unless something is genuinely good, in which case acknowledge it briefly before going back to the problems.>

BORING PATCHES:
- [timestamp] <reason this section drags / what could fix it>
- [timestamp] <another boring patch>

LOGIC / CLARITY ISSUES:
- <something confusing, contradictory, or never resolved>
- <another clarity issue>

FILLER WORD INTERVENTION:
<Short, direct paragraph calling out their worst filler habits with counts. Be mean about it.>

WHAT ACTUALLY WORKED:
- <genuine positive if there is one — only mention if truly earned>

WHAT MUST CHANGE:
- <1 non-negotiable thing to fix before the next video>
- <another must-fix>
- <third must-fix>
"""

    try:
        roast_response = writer_model.respond(roast_prompt)

        # Parse grade
        gm = re.search(r"OVERALL GRADE:\s*([A-F][+-]?)", roast_response, re.IGNORECASE)
        report.grade = gm.group(1).upper() if gm else "?"

        # Parse full roast
        roast_body = _extract_section(roast_response, "THE ROAST")
        report.roast_text = roast_body or roast_response[:2000]

        # Parse boring patches
        boring_sec = _extract_section(roast_response, "BORING PATCHES")
        for line in boring_sec.splitlines():
            line = line.strip().lstrip("-•* ")
            m = re.match(r"(\[[\d:→ ]+\])\s*(.*)", line)
            if m:
                report.boring_sections.append((m.group(1), m.group(2)))
            elif line and len(line) > 10:
                report.boring_sections.append(("", line))

        # Parse logic issues
        logic_sec = _extract_section(roast_response, "LOGIC / CLARITY ISSUES")
        report.logic_issues = [
            l.strip().lstrip("-•* ") for l in logic_sec.splitlines()
            if l.strip().lstrip("-•* ") and len(l.strip()) > 10
        ]

        # Parse positives
        pos_sec = _extract_section(roast_response, "WHAT ACTUALLY WORKED")
        report.positive_notes = [
            l.strip().lstrip("-•* ") for l in pos_sec.splitlines()
            if l.strip().lstrip("-•* ") and len(l.strip()) > 5
        ]

        # Clarity issues from "WHAT MUST CHANGE"
        must_sec = _extract_section(roast_response, "WHAT MUST CHANGE")
        report.clarity_issues = [
            l.strip().lstrip("-•* ") for l in must_sec.splitlines()
            if l.strip().lstrip("-•* ") and len(l.strip()) > 10
        ]

        _log(f"  ✓ Roast complete — Grade: {report.grade}")
    except Exception as e:
        report.roast_text = f"Roast failed: {e}"
        report.grade = "?"
        _log(f"  ✗ Roast error: {e}")

    # ── SAGE LOGIC ANALYSIS (optional bonus pass) ──────────────────
    if sage_model and len(plain) > 100:
        _log("  🧠 Sage logic/clarity analysis ...")
        sage_prompt = f"""{_ctx_preamble}Analyse this video transcript for logical consistency, clarity, and structure.
Be precise and critical. Your job is diagnosis, not encouragement.

TRANSCRIPT:
{plain[:4000]}

Respond in this EXACT format:

LOGICAL ISSUES:
- <direct statement of a logical gap, contradiction, or unresolved claim>
- <another issue>

STRUCTURAL ISSUES:
- <is the argument/explanation structured well? where does it break down?>

CLARITY ISSUES:
- <something that will confuse the viewer — be specific>
"""
        try:
            sage_response = sage_model.respond(sage_prompt)
            logical  = _extract_bullets(sage_response, "LOGICAL ISSUES")
            clarity  = _extract_bullets(sage_response, "CLARITY ISSUES")
            struct   = _extract_bullets(sage_response, "STRUCTURAL ISSUES")
            if logical: report.logic_issues   += logical
            if clarity: report.clarity_issues += clarity
            if struct:  report.clarity_issues += struct
            _log(f"  ✓ Sage found {len(logical)+len(clarity)+len(struct)} issues")
        except Exception as e:
            _log(f"  ✗ Sage analysis error: {e}")

    return report


# ============================================================
# Crop suggestion (visual — letterbox/safe-zone analysis)
# ============================================================

def suggest_crop(
    frames_raw: List[Tuple[float, str]],
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Analyse frame edges to detect consistent black bars and
    suggest an ffmpeg crop filter. Returns a crop string or None.
    """
    def _log(m): progress_cb(m) if progress_cb else None

    if not _PIL_OK or not frames_raw:
        return None

    crop_samples = []
    for ts, fp in frames_raw[:min(10, len(frames_raw))]:
        if not os.path.exists(fp):
            continue
        try:
            img = _PILImage.open(fp).convert("L")  # grayscale
            w, h = img.size

            # Find the first/last non-black row/column (threshold=16)
            def _first_nonblack_row(pix, W, H, threshold=16):
                for row in range(H):
                    if any(pix[row * W + col] > threshold for col in range(W)):
                        return row
                return 0

            def _last_nonblack_row(pix, W, H, threshold=16):
                for row in range(H - 1, -1, -1):
                    if any(pix[row * W + col] > threshold for col in range(W)):
                        return row
                return H - 1

            pixels = list(img.getdata())
            top    = _first_nonblack_row(pixels, w, h)
            bottom = _last_nonblack_row(pixels, w, h)
            crop_samples.append((top, bottom, w, h))
        except Exception:
            pass

    if not crop_samples:
        return None

    # Use the median top/bottom values across sampled frames
    import statistics as _stats
    tops    = [s[0] for s in crop_samples]
    bottoms = [s[1] for s in crop_samples]
    _, _, W, H = crop_samples[0]

    med_top    = int(_stats.median(tops))
    med_bottom = int(_stats.median(bottoms))

    if med_top > 4 or med_bottom < H - 4:
        crop_h = med_bottom - med_top
        crop_y = med_top
        _log(f"  ✓ Black bars detected — suggested crop: h={crop_h} y={crop_y}")
        return f"crop={W}:{crop_h}:0:{crop_y}"

    return None  # No significant bars


# ============================================================
# Algorithm retention / packaging pass
# ============================================================

def _run_algorithm_pass(
    analysis: VideoAnalysis,
    algorithm_model: Any,
    progress_cb: Optional[Callable[[str], None]] = None,
    video_context: Optional[VideoContext] = None,
) -> VideoAnalysis:
    """
    Have the Algorithm model analyse the transcript for retention risk, hook
    mechanics, open loops, and pattern interrupt gaps.
    Appends high-impact findings to edit_suggestions and stores full notes.
    """
    def _log(m): progress_cb(m) if progress_cb else None

    plain = analysis.plain_transcript_text
    timed = analysis.full_transcript_text
    dur   = timedelta(seconds=int(analysis.duration_s))

    # Build energy context
    dead_segs = [e for e in analysis.energy_profile if e.label in ("dead", "low")]
    energy_ctx = ""
    if dead_segs:
        energy_ctx = "\n\nLOW-ENERGY WINDOWS (word-density analysis):\n" + "".join(
            f"  {e.timecode()} — {e.label} ({e.wps:.1f} wps)\n"
            for e in dead_segs[:8])

    sample = timed[:5000]
    if len(timed) > 5000:
        sample += "\n\n[... transcript continues ...]"

    _ctx_preamble = (video_context.preamble + "\n\n") if video_context and video_context.preamble else ""

    prompt = (
        f"{_ctx_preamble}"
        f"You are reviewing a video transcript for retention and platform performance.\n"
        f"Duration: {dur}  |  Words: {len(plain.split()):,}\n"
        f"{energy_ctx}\n\n"
        f"TRANSCRIPT (with timestamps):\n{sample}\n\n"
        f"Analyse for retention and platform mechanics. Use this EXACT format:\n\n"
        f"HOOK VERDICT: strong | weak | missing — <one sentence why>\n\n"
        f"RETENTION RISK POINTS:\n"
        f"- [timestamp] <specific reason viewers will drop here>\n"
        f"- [timestamp] <another drop risk>\n\n"
        f"OPEN LOOPS:\n"
        f"<Does this video create and close curiosity gaps? List any that are opened "
        f"but never paid off, or promised early but delivered too late.>\n\n"
        f"PATTERN INTERRUPT GAPS:\n"
        f"- [timestamp range] <60+ second stretch with no format change — what to add>\n\n"
        f"ALGORITHM RECOMMENDATION:\n"
        f"<Single highest-impact structural change to improve watch time. Be specific.>\n\n"
        f"TITLE / DESCRIPTION BRIEF:\n"
        f"<3 concrete title variants ranked best to worst, based on what this video "
        f"actually contains. Then: first 2 lines of description for SEO.>"
    )

    _log("  📦 Running Algorithm pass ...")
    try:
        resp = algorithm_model.respond(prompt)
        analysis.algorithm_notes = resp

        # Extract high-impact items as edit suggestions
        hook_m = re.search(r"HOOK VERDICT:\s*(\w+)", resp, re.IGNORECASE)
        if hook_m and hook_m.group(1).lower() in ("weak", "missing"):
            analysis.edit_suggestions.append(EditSuggestion(
                suggestion_type="hook",
                priority="high",
                description=f"Algorithm: Hook is {hook_m.group(1)} — "
                            + (_extract_section(resp, "HOOK VERDICT") or
                               "strengthen or add an open loop in the first 30 seconds."),
            ))

        algo_rec = _extract_section(resp, "ALGORITHM RECOMMENDATION")
        if algo_rec and len(algo_rec) > 10:
            analysis.edit_suggestions.append(EditSuggestion(
                suggestion_type="retention",
                priority="high",
                description=f"Algorithm: {algo_rec[:300]}",
            ))

        # Pull pattern interrupt gaps as medium suggestions
        pi_section = _extract_section(resp, "PATTERN INTERRUPT GAPS")
        for line in pi_section.splitlines():
            line = line.strip().lstrip("-•* ")
            if len(line) > 15:
                analysis.edit_suggestions.append(EditSuggestion(
                    suggestion_type="pacing",
                    priority="medium",
                    description=f"Pattern interrupt needed: {line[:200]}",
                ))

        _log(f"  ✓ Algorithm pass complete")
    except Exception as e:
        analysis.algorithm_notes = f"Algorithm pass failed: {e}"
        _log(f"  ✗ Algorithm pass error: {e}")

    return analysis


# ============================================================
# Coach delivery pass
# ============================================================

def _run_coach_pass(
    analysis: VideoAnalysis,
    coach_model: Any,
    progress_cb: Optional[Callable[[str], None]] = None,
    video_context: Optional[VideoContext] = None,
) -> VideoAnalysis:
    """
    Have the Coach model analyse the transcript for delivery issues:
    pacing, energy, breath, uptalk, filler habits, confidence reads.
    Appends delivery-related findings to edit_suggestions.
    """
    def _log(m): progress_cb(m) if progress_cb else None

    plain = analysis.plain_transcript_text
    timed = analysis.full_transcript_text
    dur   = timedelta(seconds=int(analysis.duration_s))
    total_words = len(plain.split())

    # Build energy context for delivery correlation
    energy_ctx = ""
    if analysis.energy_profile:
        dead  = sum(1 for e in analysis.energy_profile if e.label == "dead")
        low   = sum(1 for e in analysis.energy_profile if e.label == "low")
        high  = sum(1 for e in analysis.energy_profile if e.label == "high")
        energy_ctx = (
            f"\nWORD-DENSITY ENERGY: {high} high / {low} low / {dead} dead windows\n"
            "Dead windows often correlate with trailing energy, off-mic asides, "
            "or the creator losing confidence in the material.")
        dead_segs = [e for e in analysis.energy_profile if e.label == "dead"][:6]
        if dead_segs:
            energy_ctx += "\nDead windows:\n" + "".join(
                f"  {e.timecode()}\n" for e in dead_segs)

    # Filler word data from roast if already run
    filler_ctx = ""
    if analysis.roast and analysis.roast.filler_word_hits:
        top = sorted(analysis.roast.filler_word_hits.items(),
                     key=lambda x: x[1], reverse=True)[:6]
        total = sum(analysis.roast.filler_word_hits.values())
        filler_ctx = (
            f"\nFILLER WORD PRE-COUNT ({total} total — "
            f"{total/total_words*100:.1f}% of words):\n"
            + "".join(f"  '{w}' — {n}×\n" for w, n in top))

    sample = timed[:5500]
    if len(timed) > 5500:
        sample += "\n\n[... transcript continues ...]"

    _ctx_preamble = (video_context.preamble + "\n\n") if video_context and video_context.preamble else ""

    prompt = (
        f"{_ctx_preamble}"
        f"You are reviewing a video transcript for DELIVERY quality — how the creator "
        f"sounds, not what they say.\n"
        f"Duration: {dur}  |  Words: {total_words:,}  |  "
        f"Average WPM: {(total_words / max(analysis.duration_s / 60, 0.1)):.0f}\n"
        f"{energy_ctx}{filler_ctx}\n\n"
        f"TRANSCRIPT (with timestamps):\n{sample}\n\n"
        f"Diagnose delivery. Use this EXACT format:\n\n"
        f"DELIVERY GRADE: [A/B/C/D/F]\n\n"
        f"PACING ISSUES:\n"
        f"- [timestamp] <specific pacing problem — rushing, dragging, no variation>\n\n"
        f"ENERGY ISSUES:\n"
        f"- [timestamp] <where energy collapses or stays flat when content demands a peak>\n\n"
        f"CLARITY ISSUES:\n"
        f"- <habit reducing comprehension — swallowed endings, mumbling, running words>\n\n"
        f"CONFIDENCE ISSUES:\n"
        f"- <specific hedging patterns, uptalk on statements, self-undermining phrases>\n\n"
        f"WORST HABIT:\n"
        f"<The single most damaging delivery habit in this recording. Quote a specific "
        f"example from the transcript.>\n\n"
        f"DRILL RECOMMENDATIONS:\n"
        f"- <one specific exercise to fix the worst habit — actionable in 10 minutes>\n"
        f"- <one thing to do differently before the next recording>\n\n"
        f"WHAT'S WORKING:\n"
        f"- <one genuine delivery strength if present>"
    )

    _log("  🎙 Running Coach pass ...")
    try:
        resp = coach_model.respond(prompt)
        analysis.coach_notes = resp

        # Parse grade
        gm = re.search(r"DELIVERY GRADE:\s*([A-F][+-]?)", resp, re.IGNORECASE)
        delivery_grade = gm.group(1).upper() if gm else "?"

        # Extract worst habit as a high-priority edit suggestion
        worst = _extract_section(resp, "WORST HABIT")
        if worst and len(worst) > 10:
            analysis.edit_suggestions.append(EditSuggestion(
                suggestion_type="delivery",
                priority="high",
                description=f"Coach [Grade {delivery_grade}] Worst habit: {worst[:280]}",
            ))

        # Drills as medium suggestions
        drills = _extract_section(resp, "DRILL RECOMMENDATIONS")
        for line in drills.splitlines():
            line = line.strip().lstrip("-•* ")
            if len(line) > 10:
                analysis.edit_suggestions.append(EditSuggestion(
                    suggestion_type="delivery",
                    priority="medium",
                    description=f"Coach drill: {line[:240]}",
                ))

        # Pacing and confidence issues as low suggestions
        for section_name, stype in [
            ("PACING ISSUES", "pacing"), ("CONFIDENCE ISSUES", "delivery")
        ]:
            section = _extract_section(resp, section_name)
            for line in section.splitlines():
                line = line.strip().lstrip("-•* ")
                if len(line) > 15:
                    analysis.edit_suggestions.append(EditSuggestion(
                        suggestion_type=stype,
                        priority="low",
                        description=f"Coach: {line[:220]}",
                    ))

        _log(f"  ✓ Coach pass complete — Grade: {delivery_grade}")
    except Exception as e:
        analysis.coach_notes = f"Coach pass failed: {e}"
        _log(f"  ✗ Coach pass error: {e}")

    return analysis


def _run_cutter_pass(
    analysis:     VideoAnalysis,
    cutter_model: Any,
    progress_cb:  Optional[Callable[[str], None]] = None,
    video_context: Optional[VideoContext]          = None,
) -> VideoAnalysis:
    """
    Have the Cutter model produce timecoded edit decisions from the transcript.
    Stores prose analysis in cutter_notes and also appends to edit_suggestions.
    The EDIT ACTIONS: block in the output is machine-parseable by video_editor.
    """
    def _log(m): progress_cb(m) if progress_cb else None

    timed = analysis.full_transcript_text
    dur   = timedelta(seconds=int(analysis.duration_s))

    # Build silence context from audio quality report
    silence_ctx = ""
    if analysis.audio_quality and analysis.audio_quality.silence_gaps:
        gaps = analysis.audio_quality.silence_gaps[:8]
        silence_ctx = "\n\nDETECTED SILENCE GAPS (from audio analysis):\n" + "".join(
            f"  {_fmt_time(g[0])} → {_fmt_time(g[1])}  ({g[2]:.1f}s)\n"
            for g in gaps if len(g) >= 3)

    # Low-energy windows
    dead_segs = [e for e in analysis.energy_profile if e.label in ("dead", "low")]
    energy_ctx = ""
    if dead_segs:
        energy_ctx = "\n\nLOW-ENERGY / DEAD WINDOWS:\n" + "".join(
            f"  {e.timecode()} — {e.label}\n" for e in dead_segs[:10])

    sample = timed[:6000]
    if len(timed) > 6000:
        sample += "\n\n[... transcript continues ...]"

    _ctx_preamble = (video_context.preamble + "\n\n") if video_context and video_context.preamble else ""

    prompt = (
        f"{_ctx_preamble}"
        f"You are reviewing a video transcript as an editor. "
        f"Your job is to identify every section that should be cut or cleaned up.\n"
        f"Duration: {dur}\n"
        f"{silence_ctx}{energy_ctx}\n\n"
        f"TRANSCRIPT (with timestamps):\n{sample}\n\n"
        f"Produce your edit analysis using this EXACT format:\n\n"
        f"OVERALL PACING VERDICT: tight | slightly loose | needs significant cutting\n\n"
        f"ESTIMATED CUT LIST:\n"
        f"- [HH:MM:SS → HH:MM:SS] <reason: dead air / rambling / repeated point / mistake>\n\n"
        f"JUMP CUT OPPORTUNITIES:\n"
        f"- [HH:MM:SS → HH:MM:SS] <slow patch that could be speed-ramped or cut>\n\n"
        f"AUDIO NOTES:\n"
        f"<note any level inconsistency, background noise, or clipping>\n\n"
        f"BEST CLIP FOR SHORT/TEASER:\n"
        f"[HH:MM:SS → HH:MM:SS] <why this is the most shareable moment>\n\n"
        f"Then output a machine-readable block — EXACTLY this format, no deviations:\n\n"
        f"EDIT ACTIONS:\n"
        f"[CUT] HH:MM:SS → HH:MM:SS | <one-line reason>\n"
        f"[NORMALIZE] | <one-line reason>  (only if audio levels are inconsistent)\n"
        f"[DENOISE] | <one-line reason>    (only if background noise is audible)\n"
        f"[CROP] 9:16 | <one-line reason>  (only if short-form repost is suitable)\n\n"
        f"List every cut from ESTIMATED CUT LIST and JUMP CUT OPPORTUNITIES in "
        f"the EDIT ACTIONS block. Be precise with timestamps. "
        f"Only include NORMALIZE/DENOISE/CROP if genuinely warranted."
    )

    _log("  ✂ Running Cutter pass…")
    try:
        resp = cutter_model.respond(prompt)
        analysis.cutter_notes = resp

        # Extract cuts from EDIT ACTIONS block and add to edit_suggestions
        from video_editor import parse_edit_actions
        actions = parse_edit_actions(resp, analysis.duration_s)
        cut_count = sum(1 for a in actions if a.type == "cut")
        for action in actions:
            if action.type == "cut":
                analysis.edit_suggestions.append(EditSuggestion(
                    suggestion_type="cut_section",
                    priority="high",
                    timecode=f"{_fmt_time(action.start_s)} → {_fmt_time(action.end_s)}",
                    description=f"Cutter: {action.reason[:200]}",
                    ffmpeg_snippet=(
                        f"# Remove {_fmt_time(action.start_s)}→{_fmt_time(action.end_s)}: "
                        f"use council ✂ Auto-Edit panel"),
                ))

        _log(f"  ✓ Cutter pass complete — {cut_count} cut(s) identified")
    except Exception as e:
        analysis.cutter_notes = f"Cutter pass failed: {e}"
        _log(f"  ✗ Cutter pass error: {e}")

    return analysis


def _fmt_time(seconds: float) -> str:
    from datetime import timedelta
    return str(timedelta(seconds=int(seconds))).zfill(8)


# ============================================================
# Main pipeline
# ============================================================

class VideoProcessor:
    """
    Orchestrates the full video → analysis pipeline.
    All work happens in a background thread; progress is
    reported via progress_cb(message: str).
    """

    def __init__(
        self,
        vault_dir: Path,
        tmp_dir: Path,
        ollama_host: str = "http://localhost:11434",
    ):
        self.vault_dir    = vault_dir
        self.tmp_dir      = tmp_dir
        self.ollama_host  = ollama_host
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._video_dir   = vault_dir / "video_analyses"
        self._video_dir.mkdir(parents=True, exist_ok=True)

    def available_vision_models(self) -> List[str]:
        """Query Ollama for installed models that support vision."""
        import urllib.request
        _vision_models = {"llava", "moondream", "llava-phi3", "bakllava",
                         "llava-llama3", "minicpm-v", "cogvlm"}
        try:
            url = self.ollama_host.rstrip("/") + "/api/tags"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
                installed = [m["name"] for m in data.get("models", [])]
                return [m for m in installed
                        if any(v in m.lower() for v in _vision_models)]
        except Exception:
            return []

    def process(
        self,
        video_path: str,
        *,
        whisper_model: str = "base",
        whisper_device: str = "cuda",
        do_frames: bool = True,
        frame_interval_s: int = 10,
        max_frames: int = 20,
        vision_model: str = "llava:7b",
        personality_model: Any = None,
        content_style_manager: Any = None,
        sage_model: Any = None,               # optional second model for logic critique
        do_audio_analysis: bool = True,        # run ffmpeg audio quality pass
        do_energy_profile: bool = True,        # transcript word-density energy map
        do_visual_analysis: bool = True,       # pixel-level frame inspection
        do_edit_suggestions: bool = True,      # aggregate all findings → suggestions
        do_roast: bool = True,                 # run Peasant Roast critique
        algorithm_model: Any = None,           # Algorithm — retention/hook/packaging pass
        coach_model: Any = None,               # Coach — delivery/pacing/vocal habits pass
        cutter_model: Any = None,              # Cutter — timecoded edit decisions
        progress_cb: Optional[Callable[[str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> VideoAnalysis:
        """
        Run the full pipeline. Returns a VideoAnalysis object.

        New flags (all default True):
          do_audio_analysis   — ffmpeg astats / ebur128 / silencedetect
          do_energy_profile   — transcript word-density map
          do_visual_analysis  — pixel-level frame quality checks
          do_edit_suggestions — aggregate all findings into actionable suggestions
          do_roast            — Writer brutally critiques the spoken content
          sage_model          — if provided, Sage also analyses logic/clarity
          algorithm_model     — if provided, Algorithm analyses retention/packaging
          coach_model         — if provided, Coach analyses delivery/pacing/habits

        cancelled: optional callable that returns True if user clicked Stop.
        """
        def _log(msg: str):
            if progress_cb:
                progress_cb(msg)

        def _cancelled() -> bool:
            return cancelled() if cancelled else False

        analysis = VideoAnalysis(
            video_path=video_path,
            processed_at=datetime.now().isoformat(timespec="seconds"),
            whisper_model=whisper_model,
        )

        video_p = Path(video_path)
        if not video_p.exists():
            analysis.errors.append(f"File not found: {video_path}")
            _log(f"✗ File not found: {video_path}")
            return analysis

        _log(f"▶ Processing: {video_p.name}")

        # ── Step 0: Duration ─────────────────────────────────────
        analysis.duration_s = _ffprobe_duration(video_path)
        if analysis.duration_s > 0:
            _log(f"  Duration: {timedelta(seconds=int(analysis.duration_s))}")

        if _cancelled():
            _log("  ✗ Cancelled")
            return analysis

        # ── Step 1: Audio extraction ──────────────────────────────
        _log("▶ Step 1/8 — Audio extraction")
        wav_path = str(self.tmp_dir / f"video_audio_{int(time.time())}.wav")
        audio_ok = _extract_audio(video_path, wav_path, progress_cb=_log)
        # Keep a reference — some later steps reuse the wav
        _wav_path_for_analysis = wav_path if audio_ok else None

        if _cancelled():
            return analysis

        # ── Step 2: Transcription ─────────────────────────────────
        if audio_ok and _WHISPER_OK:
            _log("▶ Step 2/8 — Whisper transcription")
            analysis.transcript = transcribe_audio(
                wav_path,
                model_size=whisper_model,
                device=whisper_device,
                progress_cb=_log,
            )
        elif not _WHISPER_OK:
            _log("⚠ Step 2/8 — Whisper not installed (skipping transcription)")
            _log("  Install: pip install faster-whisper")
        else:
            _log("⚠ Step 2/8 — Audio extraction failed (skipping transcription)")

        if _cancelled():
            return analysis

        # ── Step 2b: Video context detection ─────────────────────────
        _log("▶ Step 2b/8 — Video context detection")
        _plain_so_far = " ".join(s.text.strip() for s in analysis.transcript)
        analysis.video_context = detect_video_context(
            transcript_text    = _plain_so_far,
            filename           = Path(video_path).name,
            frame_descriptions = None,   # frames not extracted yet
            personality_model  = personality_model,  # AI pass if available
            progress_cb        = _log,
        )

        if _cancelled():
            return analysis

        # ── Step 3: Frame extraction + description ─────────────────
        _frames_raw: List[Tuple[float, str]] = []
        if do_frames and _ffmpeg_available():
            _log("▶ Step 3/8 — Frame extraction")
            frames_dir = str(self.tmp_dir / f"frames_{int(time.time())}")
            _frames_raw = _extract_frames(
                video_path, frames_dir,
                interval_s=frame_interval_s,
                max_frames=max_frames,
                progress_cb=_log,
            )

            if _frames_raw and not _cancelled():
                vision_models = self.available_vision_models()
                if vision_models:
                    _log(f"▶ Step 3b/8 — Frame description ({vision_model})")
                    analysis.frame_descriptions = describe_frames(
                        _frames_raw,
                        ollama_host=self.ollama_host,
                        model=vision_model,
                        progress_cb=_log,
                    )
                else:
                    _log("⚠ Step 3b/8 — No vision model found in Ollama")
                    _log("  Install: ollama pull llava:7b  or  ollama pull moondream")
                    analysis.frame_descriptions = [
                        FrameDescription(ts, fp, "") for ts, fp in _frames_raw
                    ]
        elif not _ffmpeg_available():
            _log("⚠ Step 3/8 — FFmpeg not found (skipping frames)")
            _log("  Install: conda install ffmpeg -c conda-forge")
        else:
            _log("  Step 3/8 — Frame extraction skipped (disabled)")

        if _cancelled():
            return analysis

        # ── Step 3c: Refine context with frame descriptions ──────────
        if (analysis.frame_descriptions
                and analysis.video_context
                and analysis.video_context.detected_by == "heuristic"
                and personality_model):
            _log("  Refining context using frame descriptions ...")
            analysis.video_context = detect_video_context(
                transcript_text    = _plain_so_far,
                filename           = Path(video_path).name,
                frame_descriptions = analysis.frame_descriptions,
                personality_model  = personality_model,
                progress_cb        = _log,
            )

        if _cancelled():
            return analysis

        # ── Step 4: Vibe analysis ─────────────────────────────────
        if personality_model and analysis.transcript:
            _log("▶ Step 4/8 — Council vibe analysis")
            analysis = analyse_vibe(
                analysis,
                personality_model=personality_model,
                content_style_manager=content_style_manager,
                progress_cb=_log,
                video_context=analysis.video_context,
            )
        elif not analysis.transcript:
            _log("  Step 4/8 — Skipping vibe analysis (no transcript)")
        else:
            _log("  Step 4/8 — Skipping vibe analysis (no personality model)")

        if _cancelled():
            return analysis

        # ── Step 5: Audio quality analysis ────────────────────────
        if do_audio_analysis and _wav_path_for_analysis:
            _log("▶ Step 5/8 — Audio quality analysis (ffmpeg)")
            analysis.audio_quality = analyse_audio_quality(
                _wav_path_for_analysis, progress_cb=_log)
        else:
            _log("  Step 5/8 — Audio quality analysis skipped")

        if _cancelled():
            return analysis

        # ── Step 6: Energy profile ────────────────────────────────
        if do_energy_profile and analysis.transcript:
            _log("▶ Step 6/8 — Energy profile (word density)")
            analysis.energy_profile = detect_energy_profile(
                analysis.transcript, analysis.duration_s)
            dead  = sum(1 for e in analysis.energy_profile if e.label == "dead")
            low   = sum(1 for e in analysis.energy_profile if e.label == "low")
            high  = sum(1 for e in analysis.energy_profile if e.label == "high")
            _log(f"  ✓ {len(analysis.energy_profile)} windows — "
                 f"{high} high / {low} low / {dead} dead")
        else:
            _log("  Step 6/8 — Energy profile skipped (no transcript)")

        if _cancelled():
            return analysis

        # ── Step 7: Visual issue detection ────────────────────────
        if do_visual_analysis and (analysis.frame_descriptions or _frames_raw):
            _log("▶ Step 7/8 — Visual issue detection")
            analysis.visual_issues = detect_visual_issues(
                analysis.frame_descriptions,
                frames_raw=_frames_raw if _frames_raw else None,
            )
            # Suggest crop if black bars found
            bar_issues = [v for v in analysis.visual_issues
                          if v.issue_type == "black_bars"]
            if bar_issues and _frames_raw:
                crop_str = suggest_crop(_frames_raw, progress_cb=_log)
                if crop_str:
                    from dataclasses import fields as _dfields
                    # Store as an early edit suggestion (inserted before generate step)
                    analysis.edit_suggestions.append(EditSuggestion(
                        suggestion_type="crop",
                        priority="medium",
                        description=f"Black bars detected — crop filter: {crop_str}",
                        ffmpeg_snippet=(
                            f'ffmpeg -i "{video_path}" '
                            f'-filter:v "{crop_str}" cropped.mp4')))
            _log(f"  ✓ {len(analysis.visual_issues)} visual issues found")
        else:
            _log("  Step 7/8 — Visual analysis skipped (no frames)")

        if _cancelled():
            return analysis

        # ── Step 7b: Edit suggestions ─────────────────────────────
        if do_edit_suggestions:
            _log("▶ Step 7b/8 — Generating edit suggestions")
            new_suggestions = generate_edit_suggestions(analysis)
            analysis.edit_suggestions = (
                analysis.edit_suggestions + new_suggestions)
            high_p = sum(1 for s in analysis.edit_suggestions
                         if s.priority == "high")
            _log(f"  ✓ {len(analysis.edit_suggestions)} suggestions "
                 f"({high_p} high priority)")

        if _cancelled():
            return analysis

        # ── Step 8: Peasant Roast ─────────────────────────────────
        if do_roast and personality_model and analysis.transcript:
            _log("▶ Step 8/8 — 🔥 Peasant Roast (brutal content critique)")
            analysis.roast = roast_transcript(
                analysis,
                writer_model=personality_model,
                sage_model=sage_model,
                progress_cb=_log,
                video_context=analysis.video_context,
            )
            if analysis.roast:
                _log(f"  Grade: {analysis.roast.grade}  "
                     f"Filler words: {sum(analysis.roast.filler_word_hits.values())}  "
                     f"Boring patches: {len(analysis.roast.boring_sections)}")
        elif not analysis.transcript:
            _log("  Step 8/8 — Roast skipped (no transcript)")
        else:
            _log("  Step 8/8 — Roast skipped (no personality model)")

        if _cancelled():
            return analysis

        # ── Step 8a: Algorithm retention/packaging pass ───────────
        if algorithm_model and analysis.transcript:
            _log("▶ Step 8a — 📦 Algorithm retention & packaging pass")
            analysis = _run_algorithm_pass(
                analysis,
                algorithm_model=algorithm_model,
                progress_cb=_log,
                video_context=analysis.video_context,
            )
        else:
            reason = "no transcript" if not analysis.transcript else "no algorithm model"
            _log(f"  Step 8a — Algorithm pass skipped ({reason})")

        if _cancelled():
            return analysis

        # ── Step 8b: Coach delivery pass ──────────────────────────
        if coach_model and analysis.transcript:
            _log("▶ Step 8b — 🎙 Coach delivery & pacing pass")
            analysis = _run_coach_pass(
                analysis,
                coach_model=coach_model,
                progress_cb=_log,
                video_context=analysis.video_context,
            )
        else:
            reason = "no transcript" if not analysis.transcript else "no coach model"
            _log(f"  Step 8b — Coach pass skipped ({reason})")

        if _cancelled():
            return analysis

        # ── Step 8c: Cutter edit decisions ────────────────────────
        if cutter_model and analysis.transcript:
            _log("▶ Step 8c — ✂ Cutter timecoded edit pass")
            analysis = _run_cutter_pass(
                analysis,
                cutter_model=cutter_model,
                progress_cb=_log,
                video_context=analysis.video_context,
            )
        else:
            reason = "no transcript" if not analysis.transcript else "no cutter model"
            _log(f"  Step 8c — Cutter pass skipped ({reason})")

        if _cancelled():
            return analysis

        # ── Save analysis to vault ────────────────────────────────
        safe = re.sub(r"[^\w\-]", "_", video_p.stem)[:40]
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self._video_dir / f"{safe}_{ts}.json"
        try:
            out_path.write_text(
                json.dumps(analysis.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            _log(f"\n✓ Analysis saved → vault/video_analyses/{out_path.name}")
        except Exception as e:
            _log(f"\n✗ Save error: {e}")

        # ── Save transcript to vault ──────────────────────────────
        if analysis.transcript:
            tx_path = self._video_dir / f"{safe}_{ts}_transcript.txt"
            try:
                tx_path.write_text(analysis.full_transcript_text, encoding="utf-8")
                _log(f"✓ Transcript saved → vault/video_analyses/{tx_path.name}")
            except Exception:
                pass

        # Cleanup temp audio
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass

        _log("\n✓ Video processing complete.")
        return analysis

    def list_analyses(self) -> List[Path]:
        """Return all saved analysis JSON files, newest first."""
        return sorted(
            self._video_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def load_analysis(self, path: Path) -> Optional[VideoAnalysis]:
        """Load a saved VideoAnalysis from JSON."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            a = VideoAnalysis(
                video_path=data.get("video_path", ""),
                duration_s=data.get("duration_s", 0),
                vibe_summary=data.get("vibe_summary", ""),
                vocabulary_notes=data.get("vocabulary_notes", ""),
                pacing_notes=data.get("pacing_notes", ""),
                style_notes=data.get("style_notes", []),
                processed_at=data.get("processed_at", ""),
                whisper_model=data.get("whisper_model", ""),
                errors=data.get("errors", []),
            )
            for s in data.get("transcript", []):
                a.transcript.append(TranscriptSegment(**s))
            for f in data.get("frame_descriptions", []):
                a.frame_descriptions.append(FrameDescription(
                    timestamp_s=f.get("timestamp_s", 0),
                    frame_path="",
                    description=f.get("description", ""),
                    model=f.get("model", ""),
                ))
            return a
        except Exception:
            return None
