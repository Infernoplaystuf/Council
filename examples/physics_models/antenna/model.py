"""
Radio antenna: element lengths to cut for a frequency, and how the antenna
will behave (impedance, SWR on your feed line, gain, pattern, bandwidth).

Solved with physics_kit.antennas' thin-wire method-of-moments solver, so
the lengths come from the physics (the end effect of a real wire's
thickness included), not from a rule of thumb.

Calibrate it: after building, sweep it with an antenna analyzer and put the
sweep in a table (frequency and SWR columns), or measure the resonant
frequency of a few test lengths. The model reads the sweep, says where your
antenna actually resonates and how much to trim.
"""
from __future__ import annotations

import math

from physics_kit import Chart, Fit, Input, Model, Result
from physics_kit import antennas as an
from physics_kit.units import ft_in

DIPOLE, MONOPOLE, YAGI = ("Half-wave dipole", "Quarter-wave ground plane (monopole)",
                          "Yagi-Uda beam")


def _design(p):
    f = p["frequency_mhz"] * 1e6
    lam = an.wavelength(f)
    radius = p["element_diameter_mm"] / 2000
    k = p["shortening_factor"]
    if p["antenna"] == DIPOLE:
        L = k * lam / 2 if k else an.resonant_dipole_length(f, radius)
        return f, lam, [("Dipole (total, tip to tip)", L, 0.0)], \
            (lambda ff: an.solve([an.Wire(0, 0, L, radius)], ff, pattern=False))
    if p["antenna"] == MONOPOLE:
        Ld = k * lam / 2 if k else an.resonant_dipole_length(f, radius)
        L = Ld / 2
        return f, lam, [("Vertical element", L, 0.0),
                        ("Radials (4, sloping 45° down)", L * 1.05, 0.0)], \
            (lambda ff: _mono(ff, L, radius))
    lengths, positions, _sol = an.yagi_design(f, int(p["elements"]), radius)
    if k:
        ratio = (k * lam / 2) / lengths[1]          # the driven element set by k, the rest kept in step
        lengths = [x * ratio for x in lengths]
    names = ["Reflector", "Driven element"] + [f"Director {i}" for i in range(1, len(lengths) - 1)]
    return f, lam, list(zip(names, lengths, positions)), \
        (lambda ff: an.yagi(ff, lengths, positions, radius))


def _mono(f, L, radius):
    s = an.solve([an.Wire(0, 0, 2 * L, radius)], f, pattern=False)
    s.impedance = s.impedance / 2
    return s


def compute(p, data) -> Result:
    res = Result()
    f, lam, parts, build = _design(p)
    radius = p["element_diameter_mm"] / 2000
    if p["antenna"] == DIPOLE:
        sol = an.dipole(f, parts[0][1], radius)
    elif p["antenna"] == MONOPOLE:
        sol = an.monopole(f, parts[0][1], radius)
    else:
        sol = an.yagi(f, [x[1] for x in parts], [x[2] for x in parts], radius)
    z = sol.impedance
    z0 = p["feed_impedance"]
    sweep = an.swr_sweep(build, f, span=0.12, points=13, z0=z0)
    bw = an.swr_bandwidth(sweep, 2.0)
    res.tables["Cut list"] = [
        {"Part": name, "Length (mm)": round(L * 1000, 1), "Length (ft-in)": ft_in(L),
         "Boom position (mm)": round(pos * 1000, 1) if p["antenna"] == YAGI else "",
         "Length (wavelengths)": round(L / lam, 4)} for name, L, pos in parts]
    if p["antenna"] == DIPOLE:
        res.tables["Cut list"].append({"Part": "Each half (from the feed point)",
                                       "Length (mm)": round(parts[0][1] * 500, 1),
                                       "Length (ft-in)": ft_in(parts[0][1] / 2),
                                       "Boom position (mm)": "", "Length (wavelengths)": ""})
    res.values.update({
        "Wavelength": (round(lam, 4), "m"),
        "Input resistance": (round(z.real, 1), "ohm"),
        "Input reactance": (round(z.imag, 1), "ohm"),
        f"SWR on {z0:g} ohm": round(an.swr(z, z0), 2),
        "Gain": (round(sol.directivity_dbi, 2), "dBi"),
        "2:1 SWR bandwidth": (f"{bw[0] / 1e6:.3f} - {bw[1] / 1e6:.3f}" if bw else
                              "never below 2:1 (needs a matching network)", "MHz"),
        "Quarter-wave coax section": (round(an.quarter_wave_stub(f, p["velocity_factor"]), 4), "m"),
    })
    if sol.front_to_back_db is not None and p["antenna"] == YAGI:
        res.values["Front-to-back"] = (round(sol.front_to_back_db, 1), "dB")
    res.charts.append(Chart("SWR across the band", [s[0] / 1e6 for s in sweep],
                            {"SWR": [min(s[1], 10.0) for s in sweep]},
                            x_label="MHz", y_label=f"SWR ({z0:g} ohm)"))
    if p["antenna"] == YAGI:
        angles = list(range(0, 360, 2))
        res.charts.append(Chart("Pattern seen from above (dBi)", angles,
                                {"gain": list(sol.pattern_phi)}, kind="polar"))
    else:
        n = len(sol.pattern_theta)
        res.charts.append(Chart("Pattern seen from the side (dBi, 0° = along the element)",
                                list(range(0, 2 * n, 2)), {"gain": list(sol.pattern_theta)},
                                kind="polar"))
    res.fit_constants = {}
    res.notes += [
        "Free space: a real antenna near the ground, a metal mast or the feed line differs. "
        "Cut each element a few % long, then trim while watching an analyzer.",
    ]
    if p["antenna"] == MONOPOLE:
        res.notes.append("A ground plane's radials sloping 45° down raise its resistance from "
                         "about 36 ohm toward 50 ohm; the model uses a perfect ground.")
    if p["antenna"] == YAGI and abs(z.real - z0) / z0 > 0.4:
        res.notes.append(f"The driven element's {z.real:.0f} ohm needs matching to {z0:g} ohm "
                         "(a gamma or hairpin match, or a 4:1 balun with a folded dipole).")
    _sweep_from_data(p, data, res, f, parts)
    return res


def _sweep_from_data(p, data, res, f, parts) -> None:
    """An analyzer sweep (frequency + SWR columns): where it really resonates."""
    if not data:
        return
    for t in data.names():
        cols = data.columns(t)
        fc = next((c for c in cols if "freq" in c.lower() or "mhz" in c.lower()), None)
        sc = next((c for c in cols if "swr" in c.lower()), None)
        if not (fc and sc):
            continue
        pts = data.pairs(t, fc, sc)
        if not pts:
            continue
        fm, sm = min(pts, key=lambda x: x[1])
        f_meas = fm * (1e6 if fm < 1e5 else 1.0)
        ratio = f_meas / f
        driven = parts[1][1] if p["antenna"] == YAGI else parts[0][1]
        res.values["Measured resonance"] = (round(f_meas / 1e6, 4), "MHz")
        res.values["Measured best SWR"] = round(sm, 2)
        verb = "lengthen" if ratio > 1 else "shorten"
        res.notes.append(
            f"Your sweep in '{t}' is lowest at {f_meas / 1e6:.3f} MHz ({(ratio - 1) * 100:+.1f} % "
            f"from the target). To move it, {verb} the elements by about "
            f"{abs(1 - ratio) * 100:.1f} % ({abs(driven * (ratio - 1)) * 1000:.0f} mm on the "
            f"{'driven element' if p['antenna'] == YAGI else 'element'}).")
        res.charts.append(Chart(f"Measured sweep ({t})", [x[0] for x in pts],
                                {"SWR": [x[1] for x in pts]}, x_label=fc, y_label="SWR"))
        return


MODEL = Model(
    name="Radio antenna",
    goal="Design a wire antenna for a frequency: what to cut, and how it will perform.",
    inputs=[
        Input("antenna", DIPOLE, choices=[DIPOLE, MONOPOLE, YAGI]),
        Input("frequency_mhz", 146.0, unit="MHz", low=0.1, high=3000),
        Input("elements", 3, low=2, high=12, help="Yagi only: reflector + driven + directors."),
        Input("element_diameter_mm", 6.0, unit="mm", low=0.1, high=60),
        Input("feed_impedance", 50.0, unit="ohm", low=25, high=600),
        Input("velocity_factor", 0.66, low=0.5, high=1.0,
              help="Of your coax (0.66 solid PE, 0.78-0.85 foam)."),
        Input("shortening_factor", 0.0, low=0.0, high=1.0,
              help="0: the solver finds the resonant length. Otherwise the half-wave "
                   "element is this x wavelength / 2 (calibrate it from measurements)."),
    ],
    compute=compute,
    assumptions=["Straight, parallel, perfectly conducting wires in free space.",
                 "Centre-fed by a 1 V delta gap; no feed line or balun modelled."],
    limits=["No ground (except the monopole's perfect plane), mast or nearby objects.",
            "Element diameter well under a segment length (thin wire)."],
    fits=[
        Fit("Shortening factor from measured resonances", "f = K*c_0/(2*L)", "L",
            sets={"K": "shortening_factor"}, x_hint="length", y_hint="freq",
            help="Element lengths (m) you tried and the frequency (Hz) each resonated at.",
            when={"antenna": DIPOLE}),
    ],
    data_help="An antenna analyzer sweep (frequency and SWR columns), or a table of "
              "element lengths and the resonant frequency you measured for each.",
)
