"""
PVC pipe organ (and struck PVC or metal chimes): what length of pipe to cut
for each note.

Two instruments, because a pipe makes a note two ways:
  * Pipe organ: the AIR inside rings. Open pipes (both ends open, like a
    flue pipe) or stopped pipes (one end capped, an octave lower for the
    same length, a darker sound).
  * Struck tubes (chimes): the TUBE itself rings when you hit it. Its
    overtones are not harmonic, which is what makes it sound like a bell.

Calibrate it: cut one pipe, measure its pitch with a tuner or the
microphone, put the measurements in a table (length and frequency) and use
"Calibrate". The end correction (organ) or the stiffness (chimes) is fitted
and every other pipe in the cut list follows.
"""
from __future__ import annotations

import math

from physics_kit import Chart, Fit, Input, Model, Result
from physics_kit import acoustics as ac
from physics_kit import materials as mat
from physics_kit.units import ft_in

ORGAN, CHIMES = "Pipe organ (air column)", "Struck tubes (chimes)"
OPEN, STOPPED = "Open both ends", "Stopped (one end capped)"
STICK_M = 3.048                       # a 10 ft stick of PVC
TUBE_MATERIALS = ["PVC (rigid)", "Aluminium 6061-T6", "Copper", "Brass C260",
                  "Mild steel 1018"]


def _sticks(lengths, stick=STICK_M, kerf=0.003):
    """How many sticks the cut list needs (first fit, longest first)."""
    free = []
    for L in sorted(lengths, reverse=True):
        need = L + kerf
        for i, f in enumerate(free):
            if f >= need:
                free[i] -= need
                break
        else:
            if need > stick:
                return None
            free.append(stick - need)
    return len(free)


def compute(p, data) -> Result:
    notes = ac.scale_notes(p["first_note"], p["scale"], p["notes"])
    od, idia = ac.pvc_sch40(p["pipe_size"])
    res = Result()
    rows, lengths, freqs = [], [], []
    c = ac.speed_of_sound(p["temperature_c"])
    if p["instrument"] == ORGAN:
        stopped = p["pipe_type"] == STOPPED
        k = p["end_correction"]
        for n in notes:
            f = ac.note_frequency(n, p["a4_hz"])
            L = ac.pipe_length(f, idia, stopped, p["temperature_c"], k)
            if stopped:
                over = [ac.stopped_pipe_frequency(L, idia, p["temperature_c"], m, k) for m in (2, 3)]
            else:
                over = [ac.open_pipe_frequency(L, idia, p["temperature_c"], h, k) for h in (2, 3)]
            rows.append({"Note": n, "Target (Hz)": round(f, 2), "Cut length (mm)": round(L * 1000, 1),
                         "Cut length (ft-in)": ft_in(L), "Next overtones (Hz)":
                         ", ".join(f"{o:.0f}" for o in over)})
            lengths.append(L)
            freqs.append(f)
        res.fit_constants = {"c": c, "r": idia / 2}
        res.notes += [
            f"Speed of sound {c:.1f} m/s at {p['temperature_c']:g} °C. Pitch rises about "
            "3 cents per °C, so tune at the temperature you'll play at.",
            f"End correction {k:g} x radius per open end. A flue pipe's mouth usually adds "
            "more, so the first pipe may sound a little flat: measure it and calibrate.",
        ]
        if lengths and min(lengths) < 6 * idia:
            res.notes.append("The highest pipes are short for their width (under 6 diameters): "
                             "they speak poorly. Use a narrower pipe size for the top notes.")
    else:
        m = mat.get(p["tube_material"])
        e = (p["youngs_modulus_gpa"] or m.youngs_modulus / 1e9) * 1e9
        rho = m.density
        I, A = ac.tube_section(od, idia)
        for n in notes:
            f = ac.note_frequency(n, p["a4_hz"])
            L = ac.bar_length(f, e, rho, I, A)
            modes = ac.free_bar_frequencies(L, e, rho, I, A, 3)
            hang = ac.free_bar_node_positions(L)[0]
            rows.append({"Note": n, "Target (Hz)": round(f, 2), "Cut length (mm)": round(L * 1000, 1),
                         "Cut length (ft-in)": ft_in(L), "Hang point from end (mm)": round(hang * 1000, 1),
                         "Overtones (Hz)": ", ".join(f"{x:.0f}" for x in modes[1:])})
            lengths.append(L)
            freqs.append(f)
        res.fit_constants = {"I": I, "A": A, "rho": rho}
        res.notes += [
            f"{m.name}: E = {e / 1e9:.3g} GPa, density {rho:g} kg/m³ ({m.sure} value). "
            + (m.note or ""),
            "Hang or support each tube at the hang point (22.4 % from each end): it rings longest there.",
            "The overtones are 2.76x and 5.40x the note, so chimes sound bell-like, not like a pipe.",
        ]
    res.tables["Cut list"] = rows
    sticks = _sticks(lengths)
    total = sum(lengths)
    res.values.update({
        "Notes": len(rows),
        "Total pipe": (round(total, 3), "m"),
        "10 ft sticks needed": sticks if sticks is not None else "a pipe is longer than a stick",
        "Longest pipe": (round(max(lengths) * 1000, 1), "mm"),
        "Shortest pipe": (round(min(lengths) * 1000, 1), "mm"),
        "Inside diameter": (round(idia * 1000, 2), "mm"),
    })
    res.charts.append(Chart("Cut length per note", list(range(1, len(rows) + 1)),
                            {"length (mm)": [L * 1000 for L in lengths]},
                            x_label="note (1 = " + rows[0]["Note"] + ")", y_label="mm",
                            kind="scatter"))
    _compare(p, data, res, idia, c)
    return res


def _compare(p, data, res, idia, c) -> None:
    """Measured pipes (a table with a length and a frequency column) against the model."""
    if not data:
        return
    for t in data.names():
        cols = data.columns(t)
        lc = next((x for x in cols if "length" in x.lower()), None)
        fc = next((x for x in cols if any(w in x.lower() for w in ("freq", "hz", "pitch"))), None)
        if not (lc and fc):
            continue
        unit = data.unit(t, lc).lower() or ("mm" if "mm" in lc.lower() else "m")
        scale = {"mm": 1e-3, "cm": 1e-2, "in": 0.0254, "inch": 0.0254, "m": 1.0}.get(unit, 1.0)
        rows = []
        for L, f in data.pairs(t, lc, fc):
            Lm = L * scale
            if p["instrument"] == ORGAN:
                stopped = p["pipe_type"] == STOPPED
                pred = (ac.stopped_pipe_frequency if stopped else ac.open_pipe_frequency)(
                    Lm, idia, p["temperature_c"], 1, p["end_correction"])
            else:
                m = mat.get(p["tube_material"])
                e = (p["youngs_modulus_gpa"] or m.youngs_modulus / 1e9) * 1e9
                od, _ = ac.pvc_sch40(p["pipe_size"])
                I, A = ac.tube_section(od, idia)
                pred = ac.free_bar_frequencies(Lm, e, m.density, I, A, 1)[0]
            rows.append({"Length": L, "Measured (Hz)": f, "Model (Hz)": round(pred, 2),
                         "Off (cents)": round(ac.cents(f, pred), 1)})
        if rows:
            res.tables[f"Measured vs model ({t})"] = rows
            worst = max(abs(r["Off (cents)"]) for r in rows)
            res.notes.append(f"{len(rows)} measured pipe(s) in '{t}': the model is within "
                             f"{worst:.0f} cents of them. Over ~10 cents is audible: use Calibrate.")
        return


MODEL = Model(
    name="PVC pipe organ",
    goal="Work out what to cut to build a PVC pipe organ (or a set of struck chimes) "
         "that plays a chosen scale in tune.",
    inputs=[
        Input("instrument", ORGAN, choices=[ORGAN, CHIMES]),
        Input("first_note", "C4", help="The lowest note, like C4 (middle C) or A3."),
        Input("scale", "Major", choices=list(ac.SCALES)),
        Input("notes", 8, low=1, high=61, help="How many pipes."),
        Input("pipe_size", "1/2", choices=ac.PVC_SIZES, help="Schedule 40 nominal size (inches)."),
        Input("pipe_type", OPEN, choices=[OPEN, STOPPED], help="Organ only."),
        Input("temperature_c", 20.0, unit="°C", low=-20, high=45),
        Input("a4_hz", 440.0, unit="Hz", low=400, high=480, help="Concert pitch."),
        Input("end_correction", ac.END_CORRECTION, unit="x radius", low=0.0, high=3.0,
              help="Per open end; 0.6133 for a plain open end. Calibrate it from a measured pipe."),
        Input("tube_material", "PVC (rigid)", choices=TUBE_MATERIALS, help="Chimes only."),
        Input("youngs_modulus_gpa", 0.0, unit="GPa", low=0, high=400,
              help="Chimes only: 0 uses the material table. Calibrate it from a measured tube."),
    ],
    compute=compute,
    assumptions=[
        "Equal temperament from A4 = a4_hz.",
        "Organ: a plane wave in the pipe; the end correction is the only effect of the ends.",
        "Chimes: a free-free Euler-Bernoulli tube (long compared to its diameter).",
        "Schedule 40 inside diameters (ASTM D1785).",
    ],
    limits=[
        "Wide, short pipes (under ~6 diameters long) don't follow the formula well.",
        "A flue pipe's voicing (mouth height, windway) shifts its pitch: calibrate from one pipe.",
        "PVC's stiffness varies between makers by up to ±30 %, so chimes need calibrating by ear.",
    ],
    fits=[
        Fit("End correction from measured open pipes", "f = c/(2*(L + 2*k*r))", "L",
            sets={"k": "end_correction"}, x_hint="length", y_hint="freq",
            help="A table of pipe lengths (m) and the frequency each one sounds.",
            when={"instrument": ORGAN, "pipe_type": OPEN}),
        Fit("End correction from measured stopped pipes", "f = c/(4*(L + k*r))", "L",
            sets={"k": "end_correction"}, x_hint="length", y_hint="freq",
            when={"instrument": ORGAN, "pipe_type": STOPPED}),
        Fit("Tube stiffness from measured chimes",
            "f = 22.37330/(2*pi*L^2)*sqrt(E_gpa*1e9*I/(rho*A))", "L",
            sets={"E_gpa": "youngs_modulus_gpa"}, x_hint="length", y_hint="freq",
            help="Lengths (m) of struck tubes and the note each rings at.",
            when={"instrument": CHIMES}),
    ],
    data_help="A table of pipes you've cut and measured (a length column and a frequency "
              "column), or a folder with one. It's compared with the model and can calibrate it.",
)
