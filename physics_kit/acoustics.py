"""
physics_kit.acoustics — what note a pipe, bar or tube makes, and what size
makes a given note.

    note_frequency("A4") -> 440.0          equal temperament, A4 = 440 Hz by default
    nearest_note(261.0)  -> ("C4", -4.4)   the note and how many cents off
    speed_of_sound(20)   -> 343.2 m/s      in dry air at a temperature in °C

    AIR COLUMNS (a pipe's air vibrates)
    open_pipe_frequency(L, d)      both ends open (a flue organ pipe, a flute)
    stopped_pipe_frequency(L, d)   one end closed (a stopped or "gedackt" pipe,
                                   a pan pipe): an octave lower, odd overtones only
    pipe_length(f, d, stopped)     the physical length that sounds f
    The end correction (the air just past an open end moves too) is
    END_CORRECTION x the radius per open end - 0.6133 for an unflanged
    end (Levine & Schwinger, 1948). A flue pipe's mouth adds more than an
    open end, so a real pipe built to these lengths usually sounds a little
    flat: calibrate the factor by ear or with a tuner and it is exact.

    SOLIDS (the material vibrates: a struck bar, tube or chime)
    free_bar_frequencies(L, E, rho, I, A)  free-free Euler-Bernoulli modes
    tube_section(do, di) / bar_section(w, t)  -> (I, A)
    bar_length(f, ...)             the length whose first mode is f
    Its overtones are NOT harmonic (2.76x, 5.40x the fundamental), which is
    why a struck tube sounds like a chime.

    HELMHOLTZ (a bottle, a cavity with a neck)
    helmholtz_frequency(V, neck_area, neck_length)

    PVC_SCH40 / PVC_SCH40_ID       nominal size -> outside, inside diameter (m),
                                   ASTM D1785 Schedule 40
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Sequence, Tuple

from .units import INCH

A4_HZ = 440.0
END_CORRECTION = 0.6133           # x radius, per unflanged open end
FLANGED_END_CORRECTION = 0.8216   # x radius, per flanged open end

_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLAT = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#", "Cb": "B", "Fb": "E",
         "E#": "F", "B#": "C"}
_NOTE = re.compile(r"^\s*([A-Ga-g])\s*([#b♯♭]?)\s*(-?\d+)\s*$")

#: Free-free beam: beta_n * L for the first modes.
FREE_FREE_BETA_L = (4.730040745, 7.853204624, 10.99560784, 14.13716549, 17.27875966)


# ── notes ────────────────────────────────────────────────────────────────
def midi_number(note: str) -> int:
    """'C4' -> 60, 'A4' -> 69, 'Bb3' -> 58."""
    m = _NOTE.match(note or "")
    if not m:
        raise ValueError(f"'{note}' is not a note: write it like C4, F#3 or Bb2.")
    letter, acc, octave = m.group(1).upper(), m.group(2), int(m.group(3))
    acc = {"♯": "#", "♭": "b"}.get(acc, acc)
    name = letter + acc
    shift = 0
    if name in _FLAT:
        if name == "Cb":
            shift = -12          # Cb4 is B3
        if name == "B#":
            shift = 12           # B#3 is C4
        name = _FLAT[name]
    return 12 * (octave + 1) + _NAMES.index(name) + shift


def note_name(midi: int) -> str:
    return f"{_NAMES[midi % 12]}{midi // 12 - 1}"


def note_frequency(note, a4: float = A4_HZ) -> float:
    """Equal-tempered frequency of a note name or MIDI number."""
    n = note if isinstance(note, int) else midi_number(note)
    return a4 * 2.0 ** ((n - 69) / 12.0)


def nearest_note(f: float, a4: float = A4_HZ) -> Tuple[str, float]:
    """The nearest note to ``f`` and how far off it is in cents (+ = sharp)."""
    if f <= 0:
        raise ValueError("A frequency must be above zero.")
    n = 69 + 12 * math.log2(f / a4)
    k = int(round(n))
    return note_name(k), 100.0 * (n - k)


def cents(f: float, ref: float) -> float:
    return 1200.0 * math.log2(f / ref)


SCALES: Dict[str, Sequence[int]] = {
    "Major": (0, 2, 4, 5, 7, 9, 11),
    "Natural minor": (0, 2, 3, 5, 7, 8, 10),
    "Major pentatonic": (0, 2, 4, 7, 9),
    "Minor pentatonic": (0, 3, 5, 7, 10),
    "Chromatic": tuple(range(12)),
}


def scale_notes(start: str, scale: str = "Major", count: int = 8) -> List[str]:
    """``count`` notes of ``scale`` from ``start`` up ('C4', 'Major', 8 -> C4 ... C5)."""
    steps = SCALES[scale]
    base = midi_number(start)
    out = []
    i = 0
    while len(out) < count:
        octave, deg = divmod(i, len(steps))
        out.append(note_name(base + 12 * octave + steps[deg]))
        i += 1
    return out


# ── air ──────────────────────────────────────────────────────────────────
def speed_of_sound(t_c: float = 20.0) -> float:
    """Dry air, m/s (331.3 at 0 °C; 343.2 at 20 °C)."""
    return 331.3 * math.sqrt(1.0 + t_c / 273.15)


def open_pipe_frequency(length: float, inner_diameter: float, t_c: float = 20.0,
                        harmonic: int = 1, end_correction: float = END_CORRECTION) -> float:
    """Both ends open: f_n = n c / (2 (L + 2 k r))."""
    r = inner_diameter / 2
    return harmonic * speed_of_sound(t_c) / (2.0 * (length + 2 * end_correction * r))


def stopped_pipe_frequency(length: float, inner_diameter: float, t_c: float = 20.0,
                           mode: int = 1, end_correction: float = END_CORRECTION) -> float:
    """One end closed: f = (2n - 1) c / (4 (L + k r)) - odd harmonics only."""
    r = inner_diameter / 2
    return (2 * mode - 1) * speed_of_sound(t_c) / (4.0 * (length + end_correction * r))


def pipe_length(f: float, inner_diameter: float, stopped: bool = False, t_c: float = 20.0,
                end_correction: float = END_CORRECTION) -> float:
    """The physical length of pipe that sounds ``f`` (m)."""
    c, r = speed_of_sound(t_c), inner_diameter / 2
    if stopped:
        return c / (4.0 * f) - end_correction * r
    return c / (2.0 * f) - 2 * end_correction * r


# ── solids ───────────────────────────────────────────────────────────────
def tube_section(outer_d: float, inner_d: float) -> Tuple[float, float]:
    """(second moment of area I m⁴, area A m²) of a round tube."""
    i = math.pi / 64.0 * (outer_d ** 4 - inner_d ** 4)
    a = math.pi / 4.0 * (outer_d ** 2 - inner_d ** 2)
    return i, a


def bar_section(width: float, thickness: float) -> Tuple[float, float]:
    """(I, A) of a rectangular bar vibrating across its thickness."""
    return width * thickness ** 3 / 12.0, width * thickness


def free_bar_frequencies(length: float, youngs_modulus: float, density: float,
                         second_moment: float, area: float, modes: int = 3) -> List[float]:
    """Free-free bending modes (struck bar or hanging chime), Hz."""
    k = math.sqrt(youngs_modulus * second_moment / (density * area))
    return [(bl ** 2) / (2 * math.pi * length ** 2) * k for bl in FREE_FREE_BETA_L[:modes]]


def bar_length(f: float, youngs_modulus: float, density: float, second_moment: float,
               area: float) -> float:
    """The free-free length whose first mode is ``f`` (m)."""
    k = math.sqrt(youngs_modulus * second_moment / (density * area))
    return math.sqrt(FREE_FREE_BETA_L[0] ** 2 * k / (2 * math.pi * f))


def free_bar_node_positions(length: float) -> Tuple[float, float]:
    """Where to hang or support a free-free bar for its first mode (from one
    end): 22.4 % in from each end - supported there it rings longest."""
    return 0.2242 * length, 0.7758 * length


# ── Helmholtz ────────────────────────────────────────────────────────────
def helmholtz_frequency(volume: float, neck_area: float, neck_length: float,
                        t_c: float = 20.0) -> float:
    """f = c / (2 pi) sqrt(A / (V L_eff)), L_eff = L + 1.7 r (both neck ends flanged)."""
    r = math.sqrt(neck_area / math.pi)
    l_eff = neck_length + 1.7 * r
    return speed_of_sound(t_c) / (2 * math.pi) * math.sqrt(neck_area / (volume * l_eff))


# ── PVC pipe sizes (ASTM D1785 Schedule 40) ─────────────────────────────
#: nominal size -> (outside diameter, wall thickness), inches
_SCH40_IN = {
    "1/2": (0.840, 0.109), "3/4": (1.050, 0.113), "1": (1.315, 0.133),
    "1-1/4": (1.660, 0.140), "1-1/2": (1.900, 0.145), "2": (2.375, 0.154),
    "2-1/2": (2.875, 0.203), "3": (3.500, 0.216), "4": (4.500, 0.237),
}
PVC_SIZES = list(_SCH40_IN)


def pvc_sch40(nominal: str) -> Tuple[float, float]:
    """(outside diameter, inside diameter) in metres of Schedule 40 PVC."""
    key = nominal.replace('"', "").replace(" in", "").strip()
    if key not in _SCH40_IN:
        raise KeyError(f"No Schedule 40 size '{nominal}'. Known: {', '.join(PVC_SIZES)}")
    od, wall = _SCH40_IN[key]
    return od * INCH, (od - 2 * wall) * INCH
