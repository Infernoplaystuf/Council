# ============================================================
# music_renderer.py  —  Render CompositionPlan to MIDI / MusicXML / WAV
# ============================================================
# Install:
#   pip install mido music21
#   (WAV preview): pip install pyfluidsynth  +  download a .sf2 soundfont
#
# Free soundfont: https://musescore.org/en/handbook/soundfonts-and-sfz-files
#   e.g. GeneralUser_GS.sf2  (10MB, free, general MIDI)
# ============================================================

from __future__ import annotations

import os
import struct
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from music_blocks import (
    CompositionPlan, ArrangementSection,
    ChordBlock, MelodicBlock, BassBlock, RhythmBlock, ProgressionBlock,
    NoteEvent,
)

# ── Optional imports ──────────────────────────────────────────
try:
    import mido
    from mido import MidiFile, MidiTrack, Message, MetaMessage
    _MIDO_OK = True
except ImportError:
    _MIDO_OK = False

try:
    import music21
    from music21 import stream, note, chord, meter, tempo, key, clef
    _MUSIC21_OK = True
except ImportError:
    _MUSIC21_OK = False

try:
    import fluidsynth
    _FLUIDSYNTH_OK = True
except ImportError:
    _FLUIDSYNTH_OK = False


# ============================================================
# MIDI constants
# ============================================================

# General MIDI program numbers (0-indexed)
GM_PROGRAMS = {
    "piano":        0,
    "epiano":       4,
    "organ":        16,
    "guitar":       24,
    "bass":         32,
    "strings":      48,
    "brass":        56,
    "saxophone":    64,
    "flute":        73,
    "synth_lead":   80,
    "synth_pad":    88,
    "drums":        0,   # channel 10 (9 in 0-indexed)
}

DRUM_CHANNEL = 9   # MIDI convention: channel 10 (0-indexed = 9)

# Drum note map (General MIDI)
DRUM_NOTES = {
    "kick":         36,
    "snare":        38,
    "hihat_closed": 42,
    "hihat_open":   46,
    "crash":        49,
    "ride":         51,
    "tom_high":     50,
    "tom_mid":      47,
    "tom_low":      43,
}


# ============================================================
# MIDI renderer
# ============================================================

def _beats_to_ticks(beats: float, ticks_per_beat: int) -> int:
    return int(round(beats * ticks_per_beat))


def _tempo_bpm_to_us(bpm: float) -> int:
    """BPM to microseconds per beat."""
    return int(round(60_000_000 / bpm))


class MidiRenderer:
    """Renders a CompositionPlan to a MIDI file."""

    TICKS_PER_BEAT = 480

    def render(self, plan: CompositionPlan, output_path: Path) -> Path:
        if not _MIDO_OK:
            raise ImportError("pip install mido")

        mid = MidiFile(type=1, ticks_per_beat=self.TICKS_PER_BEAT)

        # Track 0: tempo + time signature meta
        meta_track = MidiTrack()
        mid.tracks.append(meta_track)
        meta_track.append(MetaMessage("track_name", name=plan.title, time=0))

        # Use first section's tempo
        first_tempo = plan.sections[0].tempo if plan.sections else plan.tempo
        meta_track.append(MetaMessage(
            "set_tempo",
            tempo=_tempo_bpm_to_us(first_tempo), time=0
        ))
        ts = plan.time_signature
        meta_track.append(MetaMessage(
            "time_signature",
            numerator=ts[0], denominator=ts[1],
            clocks_per_click=24, notated_32nd_notes_per_beat=8,
            time=0
        ))
        meta_track.append(MetaMessage("end_of_track", time=0))

        # Build per-section tracks
        chord_events:  List[Tuple[int, int, int, int]] = []  # (tick, pitch, vel, dur_ticks)
        melody_events: List[Tuple[int, int, int, int]] = []
        bass_events:   List[Tuple[int, int, int, int]] = []
        rhythm_events: List[Tuple[int, int, int, int]] = []  # drum hits

        current_tick = 0

        for section in plan.sections:
            sec_tempo  = section.tempo
            bars       = section.bars
            ts_num, ts_den = section.time_signature
            beats_per_bar  = ts_num * (4 / ts_den)
            section_beats  = bars * beats_per_bar

            # Tempo change for section
            # (simplified: we embed tempo change at section start via meta_track later)

            # ── Chord track ───────────────────────────────────
            if section.progression:
                tick = current_tick
                prog = section.progression
                # Loop chords to fill section duration
                total_prog_beats = prog.total_duration
                filled = 0.0
                while filled < section_beats - 0.01:
                    for chord_blk in prog.chords:
                        if filled >= section_beats:
                            break
                        dur = min(chord_blk.duration, section_beats - filled)
                        dur_ticks = _beats_to_ticks(dur, self.TICKS_PER_BEAT)
                        for pitch in chord_blk.midi_notes():
                            chord_events.append((tick, pitch, chord_blk.velocity, dur_ticks))
                        tick += dur_ticks
                        filled += dur

            # ── Melody track ──────────────────────────────────
            if section.melody:
                tick = current_tick
                for ev in section.melody.notes:
                    dur_ticks = _beats_to_ticks(ev.duration, self.TICKS_PER_BEAT)
                    if ev.pitch >= 0:
                        melody_events.append((tick, ev.pitch, ev.velocity, dur_ticks))
                    tick += dur_ticks

            # ── Bass track ────────────────────────────────────
            if section.bass:
                tick = current_tick
                for ev in section.bass.notes:
                    dur_ticks = _beats_to_ticks(ev.duration, self.TICKS_PER_BEAT)
                    if ev.pitch >= 0:
                        bass_events.append((tick, ev.pitch, ev.velocity, dur_ticks))
                    tick += dur_ticks

            # ── Rhythm track ──────────────────────────────────
            if section.rhythm:
                tick = current_tick
                filled = 0.0
                pat = section.rhythm.pattern
                pat_dur = section.rhythm.total_duration
                while filled < section_beats - 0.01:
                    for dur, vel in pat:
                        if filled >= section_beats:
                            break
                        dur_ticks = _beats_to_ticks(dur, self.TICKS_PER_BEAT)
                        if vel > 0:
                            # Alternate between kick, snare, hihat
                            beat_pos = filled % beats_per_bar
                            if beat_pos < 0.1 or abs(beat_pos - 2.0) < 0.1:
                                drum_note = DRUM_NOTES["kick"]
                            elif abs(beat_pos - 1.0) < 0.1 or abs(beat_pos - 3.0) < 0.1:
                                drum_note = DRUM_NOTES["snare"]
                            else:
                                drum_note = DRUM_NOTES["hihat_closed"]
                            rhythm_events.append((tick, drum_note, vel, dur_ticks // 2))
                        tick += dur_ticks
                        filled += dur

            section_ticks = _beats_to_ticks(section_beats, self.TICKS_PER_BEAT)
            current_tick += section_ticks

        # ── Write tracks ─────────────────────────────────────
        self._write_track(mid, "Chords",  chord_events,  channel=0, program=GM_PROGRAMS["piano"])
        self._write_track(mid, "Melody",  melody_events, channel=1, program=GM_PROGRAMS["epiano"])
        self._write_track(mid, "Bass",    bass_events,   channel=2, program=GM_PROGRAMS["bass"])
        self._write_track(mid, "Drums",   rhythm_events, channel=DRUM_CHANNEL, program=0)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        mid.save(str(output_path))
        return output_path

    def _write_track(
        self,
        mid: Any,
        name: str,
        events: List[Tuple[int, int, int, int]],
        channel: int,
        program: int,
    ):
        """Write absolute-timed note events as a delta-time MIDI track."""
        if not events:
            return

        track = MidiTrack()
        mid.tracks.append(track)
        track.append(MetaMessage("track_name", name=name, time=0))
        if channel != DRUM_CHANNEL:
            track.append(Message("program_change", channel=channel,
                                 program=program, time=0))

        # Convert absolute ticks to delta ticks
        # events: (abs_tick, pitch, velocity, duration_ticks)
        msg_list = []  # (abs_tick, type, channel, pitch, velocity)
        for abs_tick, pitch, vel, dur in events:
            msg_list.append((abs_tick,         "note_on",  channel, pitch, vel))
            msg_list.append((abs_tick + dur,   "note_off", channel, pitch, 0))

        msg_list.sort(key=lambda x: (x[0], 0 if x[1] == "note_off" else 1))

        prev_tick = 0
        for abs_tick, mtype, ch, pitch, vel in msg_list:
            delta = abs_tick - prev_tick
            track.append(Message(mtype, channel=ch, note=pitch,
                                 velocity=vel, time=delta))
            prev_tick = abs_tick

        track.append(MetaMessage("end_of_track", time=0))


# ============================================================
# MusicXML renderer
# ============================================================

class MusicXMLRenderer:
    """Renders a CompositionPlan to MusicXML via music21."""

    def render(self, plan: CompositionPlan, output_path: Path) -> Path:
        if not _MUSIC21_OK:
            raise ImportError("pip install music21")

        score = stream.Score()
        score.metadata = music21.metadata.Metadata()
        score.metadata.title = plan.title

        # Chord part
        chord_part = stream.Part(id="chords")
        chord_part.partName = "Chords"
        melody_part = stream.Part(id="melody")
        melody_part.partName = "Melody"
        bass_part   = stream.Part(id="bass")
        bass_part.partName   = "Bass"

        for sec_idx, section in enumerate(plan.sections):
            ts_num, ts_den = section.time_signature
            m21_ts = meter.TimeSignature(f"{ts_num}/{ts_den}")
            m21_tempo = tempo.MetronomeMark(number=section.tempo)

            # ── Chord measures ────────────────────────────────
            if section.progression:
                beats_per_bar = ts_num * (4.0 / ts_den)
                filled = 0.0
                current_measure = stream.Measure()
                current_measure.insert(0, m21_ts)
                current_measure.insert(0, m21_tempo)
                measure_filled = 0.0

                prog = section.progression
                chord_iter = (c for _ in range(999) for c in prog.chords)
                for chord_blk in chord_iter:
                    if filled >= section.bars * beats_per_bar:
                        break
                    dur_beats = min(chord_blk.duration, beats_per_bar - measure_filled)
                    m21_chord = chord.Chord(chord_blk.midi_notes())
                    m21_chord.duration.quarterLength = dur_beats
                    current_measure.append(m21_chord)
                    measure_filled += dur_beats
                    filled += dur_beats
                    if abs(measure_filled - beats_per_bar) < 0.01:
                        chord_part.append(current_measure)
                        current_measure = stream.Measure()
                        measure_filled = 0.0
                if measure_filled > 0:
                    chord_part.append(current_measure)

            # ── Melody measures ───────────────────────────────
            if section.melody:
                current_measure = stream.Measure()
                current_measure.insert(0, m21_ts)
                current_measure.insert(0, m21_tempo)
                measure_filled = 0.0
                beats_per_bar = ts_num * (4.0 / ts_den)

                for ev in section.melody.notes:
                    if ev.pitch < 0:
                        n = note.Rest()
                    else:
                        n = note.Note()
                        n.pitch.midi = ev.pitch
                    n.duration.quarterLength = ev.duration
                    current_measure.append(n)
                    measure_filled += ev.duration
                    if abs(measure_filled - beats_per_bar) < 0.01:
                        melody_part.append(current_measure)
                        current_measure = stream.Measure()
                        measure_filled = 0.0
                if measure_filled > 0:
                    melody_part.append(current_measure)

            # ── Bass measures ─────────────────────────────────
            if section.bass:
                current_measure = stream.Measure()
                current_measure.insert(0, m21_ts)
                current_measure.insert(0, m21_tempo)
                measure_filled = 0.0
                beats_per_bar = ts_num * (4.0 / ts_den)

                for ev in section.bass.notes:
                    if ev.pitch < 0:
                        n = note.Rest()
                    else:
                        n = note.Note()
                        n.pitch.midi = ev.pitch
                    n.duration.quarterLength = ev.duration
                    current_measure.append(n)
                    measure_filled += ev.duration
                    if abs(measure_filled - beats_per_bar) < 0.01:
                        bass_part.append(current_measure)
                        current_measure = stream.Measure()
                        measure_filled = 0.0
                if measure_filled > 0:
                    bass_part.append(current_measure)

        for part in [chord_part, melody_part, bass_part]:
            if len(part) > 0:
                score.append(part)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        score.write("musicxml", fp=str(output_path))
        return output_path


# ============================================================
# WAV renderer (FluidSynth)
# ============================================================

class WavRenderer:
    """Renders a MIDI file to WAV using FluidSynth and a soundfont."""

    DEFAULT_SOUNDFONT_PATHS = [
        Path("~/.local/share/sounds/sf2/GeneralUser_GS.sf2").expanduser(),
        Path("C:/soundfonts/GeneralUser_GS.sf2"),
        Path("/usr/share/sounds/sf2/FluidR3_GM.sf2"),
        Path("/usr/share/soundfonts/GeneralUser_GS_v1.471.sf2"),
    ]

    def __init__(self, soundfont_path: Optional[Path] = None):
        self.soundfont = soundfont_path or self._find_soundfont()

    def _find_soundfont(self) -> Optional[Path]:
        for p in self.DEFAULT_SOUNDFONT_PATHS:
            if p.exists():
                return p
        return None

    def available(self) -> bool:
        return _FLUIDSYNTH_OK and self.soundfont is not None and self.soundfont.exists()

    def render(self, midi_path: Path, output_path: Path,
               sample_rate: int = 44100) -> Path:
        if not _FLUIDSYNTH_OK:
            raise ImportError(
                "pip install pyfluidsynth\n"
                "Also install FluidSynth system library:\n"
                "  Windows: choco install fluidsynth\n"
                "  conda:   conda install -c conda-forge fluidsynth"
            )
        if not self.soundfont:
            raise FileNotFoundError(
                "No soundfont found. Download GeneralUser_GS.sf2 and place in:\n"
                "  C:/soundfonts/GeneralUser_GS.sf2\n"
                "  or ~/.local/share/sounds/sf2/GeneralUser_GS.sf2\n"
                "Free download: https://schristiancollins.com/generaluser.php"
            )

        fs = fluidsynth.Synth(samplerate=float(sample_rate))
        fs.start(driver="file", midi_driver=None)
        sfid = fs.sfload(str(self.soundfont))
        fs.program_select(0, sfid, 0, 0)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fs.midi_to_audio(str(midi_path), str(output_path))
        fs.delete()
        return output_path

    def soundfont_status(self) -> str:
        if not _FLUIDSYNTH_OK:
            return "pyfluidsynth not installed"
        if not self.soundfont:
            return "No soundfont found"
        return f"Soundfont: {self.soundfont}"


# ============================================================
# Master renderer
# ============================================================

class CompositionRenderer:
    """
    Coordinates all three renderers.
    Given a CompositionPlan, produces MIDI + MusicXML + optional WAV.
    """

    def __init__(
        self,
        output_dir: Path,
        soundfont_path: Optional[Path] = None,
    ):
        self.output_dir  = output_dir
        self.midi_r      = MidiRenderer()
        self.xml_r       = MusicXMLRenderer()
        self.wav_r       = WavRenderer(soundfont_path)

    def render_all(
        self,
        plan: CompositionPlan,
        render_wav: bool = True,
    ) -> Dict[str, Optional[Path]]:
        """
        Render to all available formats.
        Returns dict of {"midi": path, "musicxml": path, "wav": path}
        """
        safe_title = "".join(c if c.isalnum() or c in "_ -" else "_"
                             for c in plan.title)[:60]
        results: Dict[str, Optional[Path]] = {}

        # MIDI
        midi_path = self.output_dir / f"{safe_title}.mid"
        try:
            self.midi_r.render(plan, midi_path)
            results["midi"] = midi_path
            print(f"  ✓ MIDI:     {midi_path}")
        except Exception as e:
            results["midi"] = None
            print(f"  ✗ MIDI failed: {e}")

        # MusicXML
        xml_path = self.output_dir / f"{safe_title}.musicxml"
        try:
            self.xml_r.render(plan, xml_path)
            results["musicxml"] = xml_path
            print(f"  ✓ MusicXML: {xml_path}")
        except Exception as e:
            results["musicxml"] = None
            print(f"  ✗ MusicXML failed: {e}")

        # WAV (only if MIDI succeeded and fluidsynth available)
        if render_wav and results["midi"] and self.wav_r.available():
            wav_path = self.output_dir / f"{safe_title}.wav"
            try:
                self.wav_r.render(results["midi"], wav_path)
                results["wav"] = wav_path
                print(f"  ✓ WAV:      {wav_path}")
            except Exception as e:
                results["wav"] = None
                print(f"  ✗ WAV failed: {e}")
        else:
            results["wav"] = None
            if render_wav:
                print(f"  ⚠ WAV skipped: {self.wav_r.soundfont_status()}")

        # Save plan JSON
        json_path = self.output_dir / f"{safe_title}.json"
        json_path.write_text(plan.to_json(), encoding="utf-8")
        results["json"] = json_path
        print(f"  ✓ Plan JSON: {json_path}")

        return results
