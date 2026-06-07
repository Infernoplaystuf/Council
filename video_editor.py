# ============================================================
# video_editor.py  —  FFmpeg-driven video editor
# ============================================================
# Takes structured edit actions from the Cutter model and
# applies them to a video file using FFmpeg.
#
# Entirely local — no internet, no API, no accounts.
# Requires FFmpeg to be installed and on PATH.
#   Windows: winget install Gyan.FFmpeg
#            or: https://ffmpeg.org/download.html
# ============================================================

from __future__ import annotations

import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable, List, Optional


# ── Time helpers ────────────────────────────────────────────

def _parse_time(s: str) -> float:
    """Parse HH:MM:SS, MM:SS, or bare seconds into float seconds."""
    s = s.strip()
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        else:
            return float(s)
    except (ValueError, IndexError):
        return 0.0


def _fmt_time(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    td = timedelta(seconds=int(seconds))
    return str(td).zfill(8)  # 0:MM:SS → pads to consistent width


# ── EditAction ──────────────────────────────────────────────

@dataclass
class EditAction:
    """
    A single actionable edit that can be passed to VideoEditor.apply().

    Types:
      cut          — remove a time range from the video
      normalize    — loudness normalisation (EBU R128 / loudnorm)
      denoise      — audio noise reduction (arnndn or afftdn)
      crop         — aspect-ratio crop (e.g. "9:16", "1:1", "16:9")
      silence_cut  — remove all silence gaps above a threshold
      fade_in      — apply fade-in to first N seconds of audio+video
      fade_out     — apply fade-out to last N seconds of audio+video
    """
    type:    str            # cut | normalize | denoise | crop | silence_cut | fade_in | fade_out
    start_s: float = 0.0   # cut / speed / fade: start time (seconds)
    end_s:   float = 0.0   # cut: end time (seconds)
    value:   str   = ""    # crop → "9:16"; silence_cut → threshold in s; fade → duration s
    reason:  str   = ""    # from model analysis
    enabled: bool  = True  # user can toggle off in UI

    @property
    def label(self) -> str:
        if self.type == "cut":
            dur = self.end_s - self.start_s
            return (f"CUT  {_fmt_time(self.start_s)} → {_fmt_time(self.end_s)}"
                    f"  ({dur:.0f}s)  —  {self.reason}")
        if self.type == "normalize":
            return f"NORMALIZE AUDIO  —  {self.reason}"
        if self.type == "denoise":
            return f"DENOISE AUDIO  —  {self.reason}"
        if self.type == "crop":
            return f"CROP to {self.value or '9:16'}  —  {self.reason}"
        if self.type == "silence_cut":
            thr = self.value or "0.5"
            return f"REMOVE SILENCE > {thr}s  —  {self.reason}"
        if self.type in ("fade_in", "fade_out"):
            dur = self.value or "1.5"
            return f"{self.type.replace('_',' ').upper()} {dur}s  —  {self.reason}"
        return f"{self.type.upper()}  —  {self.reason}"

    @property
    def icon(self) -> str:
        return {
            "cut":          "✂",
            "normalize":    "🔊",
            "denoise":      "🔇",
            "crop":         "📐",
            "silence_cut":  "⏩",
            "fade_in":      "🌅",
            "fade_out":     "🌇",
        }.get(self.type, "⚙")


# ── Parser ──────────────────────────────────────────────────

# Patterns that recognise timecodes in cutter output
_TC_PATTERN = re.compile(
    r"(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)"     # start
    r"\s*(?:→|->|–|-|to)\s*"
    r"(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)",     # end
    re.IGNORECASE,
)

# Recognise single timecodes (for suggestions without an end time)
_SINGLE_TC = re.compile(r"\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?")


def parse_edit_actions(cutter_text: str, video_duration_s: float = 0.0) -> List[EditAction]:
    """
    Parse a Cutter model response into a list of EditActions.

    Looks for:
      - EDIT ACTIONS: block (structured)
      - ESTIMATED CUT LIST: block (prose with timecodes)
      - JUMP CUT OPPORTUNITIES: block (prose with timecodes)
      - Audio filter keywords (normalize, denoise, loudness)
    """
    actions: List[EditAction] = []

    # ── 1. Structured EDIT ACTIONS: block ─────────────────────
    ea_block_m = re.search(
        r"EDIT ACTIONS?:\s*\n(.*?)(?=\n[A-Z][A-Z ]+:|$)",
        cutter_text, re.DOTALL | re.IGNORECASE)
    if ea_block_m:
        for line in ea_block_m.group(1).splitlines():
            line = line.strip().lstrip("-•* ")
            if not line:
                continue
            # [CUT] HH:MM:SS → HH:MM:SS | reason
            m = re.match(
                r"\[?CUT\]?\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:→|->|–|-)\s*"
                r"(\d{1,2}:\d{2}(?::\d{2})?)\s*[|:—\-]?\s*(.*)",
                line, re.IGNORECASE)
            if m:
                actions.append(EditAction(
                    type="cut",
                    start_s=_parse_time(m.group(1)),
                    end_s=_parse_time(m.group(2)),
                    reason=m.group(3).strip() or "cutter flagged",
                ))
                continue
            # [NORMALIZE] | reason
            if re.search(r"\[?(NORMALIZE|LOUDNORM)\]?", line, re.IGNORECASE):
                reason = re.sub(r"\[?NORMALIZE\]?\s*[|:—\-]?\s*", "", line,
                                flags=re.IGNORECASE).strip()
                actions.append(EditAction(type="normalize", reason=reason or "audio normalisation"))
                continue
            # [DENOISE] | reason
            if re.search(r"\[?(DENOISE|NOISE)\]?", line, re.IGNORECASE):
                reason = re.sub(r"\[?DENOISE\]?\s*[|:—\-]?\s*", "", line,
                                flags=re.IGNORECASE).strip()
                actions.append(EditAction(type="denoise", reason=reason or "background noise"))
                continue
            # [CROP] 9:16 | reason
            cm = re.match(r"\[?CROP\]?\s+([\d:]+)\s*[|:—\-]?\s*(.*)", line, re.IGNORECASE)
            if cm:
                actions.append(EditAction(
                    type="crop", value=cm.group(1), reason=cm.group(2).strip()))
                continue
            # [SILENCE REMOVE] threshold=0.5s | reason
            sm = re.match(r"\[?SILENCE[_ ](?:REMOVE|CUT)\]?\s*(?:threshold=)?(\S+)?\s*[|:—\-]?\s*(.*)",
                          line, re.IGNORECASE)
            if sm:
                actions.append(EditAction(
                    type="silence_cut",
                    value=sm.group(1) or "0.5",
                    reason=sm.group(2).strip() or "long silence gaps"))
                continue

    # ── 2. Prose cut list (ESTIMATED CUT LIST, JUMP CUT sections) ─
    for section_name in ("ESTIMATED CUT LIST", "JUMP CUT OPPORTUNITIES"):
        sec_m = re.search(
            section_name + r"[^:]*:\s*\n(.*?)(?=\n[A-Z][A-Z ]+:|$)",
            cutter_text, re.DOTALL | re.IGNORECASE)
        if not sec_m:
            continue
        block = sec_m.group(1)
        for line in block.splitlines():
            line = line.strip().lstrip("-•* ")
            if not line:
                continue
            # Look for HH:MM:SS → HH:MM:SS ranges
            tc_m = _TC_PATTERN.search(line)
            if tc_m:
                start = _parse_time(tc_m.group(1))
                end   = _parse_time(tc_m.group(2))
                if end > start and (end - start) < 300:   # sanity: <5 min cut
                    reason = line[:200]
                    # Deduplicate against already-parsed actions
                    already = any(
                        abs(a.start_s - start) < 2 and abs(a.end_s - end) < 2
                        for a in actions if a.type == "cut"
                    )
                    if not already:
                        actions.append(EditAction(
                            type="cut", start_s=start, end_s=end, reason=reason))

    # ── 3. Audio recommendations (anywhere in the text) ───────
    text_lower = cutter_text.lower()
    if not any(a.type == "normalize" for a in actions):
        if any(kw in text_lower for kw in
               ("normaliz", "loudnorm", "inconsistent level", "too quiet",
                "too loud", "audio level", "volume")):
            actions.append(EditAction(
                type="normalize",
                reason="audio levels inconsistent (detected in analysis)"))
    if not any(a.type == "denoise" for a in actions):
        if any(kw in text_lower for kw in
               ("background noise", "hum", "hiss", "room noise",
                "denoise", "noise floor", "audio noise")):
            actions.append(EditAction(
                type="denoise",
                reason="background noise detected in analysis"))

    # ── 4. Sort cuts chronologically ──────────────────────────
    cuts     = sorted([a for a in actions if a.type == "cut"],
                      key=lambda a: a.start_s)
    non_cuts = [a for a in actions if a.type != "cut"]
    return cuts + non_cuts


# ── FFmpeg availability ─────────────────────────────────────

def ffmpeg_available() -> bool:
    """Return True if ffmpeg is on PATH and responds to --version."""
    try:
        subprocess.run(["ffmpeg", "-version"],
                       capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ffmpeg_version() -> str:
    """Return short ffmpeg version string, or empty string."""
    try:
        r = subprocess.run(["ffmpeg", "-version"],
                           capture_output=True, text=True, timeout=5)
        first = r.stdout.splitlines()[0] if r.stdout else ""
        return first
    except Exception:
        return ""


# ── VideoEditor ─────────────────────────────────────────────

class VideoEditor:
    """
    Applies a list of EditActions to a video file using FFmpeg.

    Usage:
        editor = VideoEditor()
        editor.apply(
            input_path  = "raw.mp4",
            actions     = parse_edit_actions(cutter_notes),
            output_path = "edited.mp4",
            progress_cb = print,
        )
    """

    def apply(
        self,
        input_path:  str,
        actions:     List[EditAction],
        output_path: str,
        progress_cb: Callable[[str], None] = print,
    ) -> bool:
        """
        Apply enabled actions to input_path and write to output_path.
        Returns True on success.
        """
        if not ffmpeg_available():
            progress_cb("✗ FFmpeg not found. Install: winget install Gyan.FFmpeg")
            return False

        enabled = [a for a in actions if a.enabled]
        if not enabled:
            progress_cb("No actions enabled — nothing to do.")
            return False

        inp   = Path(input_path)
        out   = Path(output_path)
        tmpdir = Path(tempfile.mkdtemp(prefix="council_edit_"))

        try:
            current = inp

            # ── Pass 1: Apply cuts ─────────────────────────────
            cuts = [a for a in enabled if a.type == "cut"]
            if cuts:
                progress_cb(f"  ✂ Applying {len(cuts)} cut(s)…")
                current = self._apply_cuts(current, cuts, tmpdir, progress_cb)
                if current is None:
                    return False

            # ── Pass 2: Audio filters ──────────────────────────
            audio_actions = [a for a in enabled
                             if a.type in ("normalize", "denoise", "silence_cut")]
            if audio_actions:
                progress_cb(f"  🔊 Applying {len(audio_actions)} audio filter(s)…")
                current = self._apply_audio(current, audio_actions, tmpdir, progress_cb)
                if current is None:
                    return False

            # ── Pass 3: Video filters (crop, fade) ─────────────
            video_actions = [a for a in enabled
                             if a.type in ("crop", "fade_in", "fade_out")]
            if video_actions:
                progress_cb(f"  📐 Applying {len(video_actions)} video filter(s)…")
                current = self._apply_video_filters(
                    current, video_actions, tmpdir, progress_cb)
                if current is None:
                    return False

            # ── Final copy to output ───────────────────────────
            if current != out:
                import shutil
                shutil.copy2(current, out)

            progress_cb(f"  ✓ Edit complete → {out.name}")
            return True

        except Exception as e:
            progress_cb(f"  ✗ Edit failed: {e}")
            return False
        finally:
            # Clean up temp files (keep output)
            for f in tmpdir.iterdir():
                if f != out:
                    try:
                        f.unlink()
                    except Exception:
                        pass
            try:
                tmpdir.rmdir()
            except Exception:
                pass

    # ── Cut implementation ─────────────────────────────────────

    def _apply_cuts(
        self,
        inp:        Path,
        cuts:       List[EditAction],
        tmpdir:     Path,
        progress_cb: Callable,
    ) -> Optional[Path]:
        """
        Remove time ranges from the video using FFmpeg's filter_complex
        trim+concat approach (re-encodes, handles any container).
        """
        # Build list of segments to KEEP (invert the cut list)
        duration = self._get_duration(inp)
        if duration <= 0:
            progress_cb("  ✗ Could not read video duration.")
            return None

        # Sort cuts and clip to [0, duration]
        sorted_cuts = sorted(
            [(max(0.0, c.start_s), min(duration, c.end_s))
             for c in cuts if c.end_s > c.start_s],
            key=lambda x: x[0])

        # Merge overlapping cuts
        merged: list = []
        for start, end in sorted_cuts:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append([start, end])

        # Invert to get keep segments
        keep: list = []
        pos = 0.0
        for cut_s, cut_e in merged:
            if pos < cut_s:
                keep.append((pos, cut_s))
            pos = cut_e
        if pos < duration:
            keep.append((pos, duration))

        if not keep:
            progress_cb("  ✗ Cuts would remove the entire video.")
            return None

        if len(keep) == 1 and keep[0] == (0.0, duration):
            progress_cb("  (cuts have no effect — segments cover full video)")
            return inp

        # Build filter_complex
        n = len(keep)
        vparts, aparts = [], []
        filter_parts   = []
        for i, (start, end) in enumerate(keep):
            filter_parts.append(
                f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}];"
                f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]")
            vparts.append(f"[v{i}]")
            aparts.append(f"[a{i}]")

        concat = "".join(vparts) + "".join(aparts) + f"concat=n={n}:v=1:a=1[vout][aout]"
        filter_complex = ";".join(filter_parts) + ";" + concat

        out = tmpdir / f"cut_{uuid.uuid4().hex[:6]}.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", str(inp),
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            str(out),
        ]
        return self._run_ffmpeg(cmd, out, progress_cb)

    # ── Audio filter implementation ────────────────────────────

    def _apply_audio(
        self,
        inp:        Path,
        actions:    List[EditAction],
        tmpdir:     Path,
        progress_cb: Callable,
    ) -> Optional[Path]:
        """Apply audio filters: loudnorm, arnndn/afftdn, silenceremove."""
        filters = []
        for a in actions:
            if a.type == "normalize":
                # EBU R128 two-pass normalisation target: -16 LUFS (YouTube standard)
                filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
            elif a.type == "denoise":
                # arnndn is the best available; fall back to afftdn
                filters.append("arnndn=m=std")   # will be replaced if fails
            elif a.type == "silence_cut":
                thr = float(a.value or "0.5")
                # Remove silence: start=1 means cut from start too
                filters.append(
                    f"silenceremove=start_periods=1:start_silence={thr}:"
                    f"stop_periods=-1:stop_silence={thr}")

        if not filters:
            return inp

        af = ",".join(filters)
        out = tmpdir / f"audio_{uuid.uuid4().hex[:6]}.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", str(inp),
            "-af", af,
            "-c:v", "copy",       # don't re-encode video in audio-only pass
            "-c:a", "aac", "-b:a", "192k",
            str(out),
        ]
        result = self._run_ffmpeg(cmd, out, progress_cb)
        if result is None:
            # arnndn might not be compiled in — retry with afftdn
            progress_cb("  ↺ arnndn unavailable, retrying with afftdn…")
            af2 = af.replace("arnndn=m=std", "afftdn=nf=-25")
            cmd[cmd.index(af)] = af2
            result = self._run_ffmpeg(cmd, out, progress_cb)
        return result

    # ── Video filter implementation ────────────────────────────

    def _apply_video_filters(
        self,
        inp:        Path,
        actions:    List[EditAction],
        tmpdir:     Path,
        progress_cb: Callable,
    ) -> Optional[Path]:
        """Apply crop and fade filters."""
        vfilters = []
        afilters = []

        for a in actions:
            if a.type == "crop":
                ratio = a.value or "9:16"
                parts = ratio.split(":")
                if len(parts) == 2:
                    try:
                        w_r, h_r = float(parts[0]), float(parts[1])
                        # Crop from center — pick the dimension that constrains
                        vfilters.append(
                            f"crop=if(gt(iw/ih\\,{w_r}/{h_r})\\,"
                            f"ih*{w_r}/{h_r}\\,iw):"
                            f"if(gt(iw/ih\\,{w_r}/{h_r})\\,ih\\,iw*{h_r}/{w_r})")
                    except ValueError:
                        progress_cb(f"  ⚠ Invalid crop ratio: {ratio}")
            elif a.type == "fade_in":
                dur = float(a.value or "1.5")
                vfilters.append(f"fade=t=in:st=0:d={dur}")
                afilters.append(f"afade=t=in:st=0:d={dur}")
            elif a.type == "fade_out":
                dur    = float(a.value or "1.5")
                length = self._get_duration(inp)
                if length > 0:
                    st = max(0, length - dur)
                    vfilters.append(f"fade=t=out:st={st}:d={dur}")
                    afilters.append(f"afade=t=out:st={st}:d={dur}")

        if not vfilters and not afilters:
            return inp

        out = tmpdir / f"vfilt_{uuid.uuid4().hex[:6]}.mp4"
        cmd = ["ffmpeg", "-y", "-i", str(inp)]
        if vfilters:
            cmd += ["-vf", ",".join(vfilters)]
        if afilters:
            cmd += ["-af", ",".join(afilters)]
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "fast",
                "-c:a", "aac", "-b:a", "192k", str(out)]
        return self._run_ffmpeg(cmd, out, progress_cb)

    # ── FFmpeg subprocess ──────────────────────────────────────

    def _run_ffmpeg(
        self,
        cmd:        list,
        expected_out: Path,
        progress_cb:  Callable,
    ) -> Optional[Path]:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,   # 1 hour max
            )
            if result.returncode != 0:
                # Show last few lines of stderr for diagnosis
                stderr_tail = "\n".join(
                    result.stderr.splitlines()[-8:]) if result.stderr else ""
                progress_cb(f"  ✗ FFmpeg error (code {result.returncode}):\n{stderr_tail}")
                return None
            if not expected_out.exists() or expected_out.stat().st_size == 0:
                progress_cb("  ✗ FFmpeg produced no output file.")
                return None
            return expected_out
        except subprocess.TimeoutExpired:
            progress_cb("  ✗ FFmpeg timed out after 1 hour.")
            return None
        except Exception as e:
            progress_cb(f"  ✗ FFmpeg exception: {e}")
            return None

    def _get_duration(self, path: Path) -> float:
        """Use ffprobe to get video duration in seconds."""
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 str(path)],
                capture_output=True, text=True, timeout=15)
            return float(r.stdout.strip())
        except Exception:
            return 0.0
