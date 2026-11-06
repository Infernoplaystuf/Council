"""
Eddy currents: how deep they reach, how much heat they make, how hard they
brake a moving magnet.

Four questions, one per "Task":
  * Eddy-current testing: the skin depth and the deepest flaw a probe can
    see at a frequency, and the frequency to pick for a depth.
  * Induction heating: watts into a round bar inside a coil.
  * Plate in an AC field: loss in a plate or sheet of any thickness.
  * Magnetic braking: lift and drag on a magnet moving over a sheet.

Calibrate it: the conductivity of a real alloy differs from the table
(temper, alloy, temperature). Measure it with a conductivity meter (%IACS),
or scan a reference standard with flaws of known depth and record the phase
angle of each: "Calibrate" fits the conductivity from those.
"""
from __future__ import annotations

import math

from physics_kit import Chart, Fit, Input, Model, Result
from physics_kit import eddy as ed
from physics_kit import materials as mat

ECT, HEAT, PLATE, BRAKE = ("Eddy-current testing (depth of reach)", "Induction heating",
                           "Plate in an AC field (loss)", "Magnetic braking")


def _conductor(p, data, res):
    """resistivity, mu_r and where the conductivity came from."""
    m = mat.get(p["material"])
    rho, src = m.resistivity, f"{m.name} from the table ({m.sure})"
    if p["conductivity_ms_m"]:
        rho, src = 1.0 / (p["conductivity_ms_m"] * 1e6), "your conductivity input"
    elif data:
        hit = data.find_column("iacs") or data.find_column("conductivity")
        if hit:
            t, col = hit
            for r in data.rows(t):
                name = str(next((v for k, v in r.items() if "material" in k.lower()), ""))
                if name and name.lower().split()[0] in m.name.lower():
                    v = r.get(col)
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        continue
                    if "iacs" in col.lower():
                        rho = 1.7241e-8 / (v / 100.0)
                    else:
                        rho = 1.0 / (v * 1e6)        # MS/m
                    src = f"your measurement in '{t}' ({v:g} {'%IACS' if 'iacs' in col.lower() else 'MS/m'})"
                    break
    mu = p["mu_r"] or m.mu_r
    res.values["Conductivity"] = (round(1 / rho / 1e6, 3), "MS/m")
    res.values["Conductivity (%IACS)"] = round(1.7241e-8 / rho * 100, 1)
    res.notes.append(f"Conductivity from {src}.")
    if mu > 1.5:
        res.notes.append("A magnetic material: its permeability changes with the field, so "
                         "these answers are rough (within a factor of 2 or so).")
    return rho, mu


def compute(p, data) -> Result:
    res = Result()
    rho, mu = _conductor(p, data, res)
    f = p["frequency_hz"]
    d = ed.skin_depth(f, rho, mu)
    res.values["Skin depth"] = (round(d * 1000, 4), "mm")
    t = p["thickness_mm"] / 1000
    depths = [i / 50 * max(3 * d, t) for i in range(51)]
    res.charts.append(Chart("Current density below the surface", [x * 1000 for x in depths],
                            {"% of surface": [100 * ed.density_at(x, d) for x in depths]},
                            x_label="depth (mm)", y_label="%"))
    res.fit_constants = {"f": f, "mu_r": mu}
    task = p["task"]
    if task == ECT:
        res.values["Effective depth (3 skin depths)"] = (round(3 * d * 1000, 3), "mm")
        target = p["target_depth_mm"] / 1000
        if target > 0:
            f1 = ed.frequency_for_depth(target, rho, mu)
            res.values["Frequency for 1 skin depth at the target"] = (round(f1, 1), "Hz")
            res.values["Phase lag at the target"] = (round(ed.phase_lag_deg(target, d), 1), "°")
        freqs = [10 ** (1 + i * 0.1) for i in range(51)]
        res.charts.append(Chart("Skin depth against frequency", freqs,
                                {"skin depth (mm)": [ed.skin_depth(x, rho, mu) * 1000 for x in freqs]},
                                x_label="Hz", y_label="mm", log_x=True, log_y=True))
        res.tables["Depth of reach"] = [
            {"Frequency (Hz)": x, "Skin depth (mm)": round(ed.skin_depth(x, rho, mu) * 1000, 4),
             "Effective depth (mm)": round(3 * ed.skin_depth(x, rho, mu) * 1000, 4)}
            for x in (100, 1e3, 1e4, 1e5, 1e6, 1e7)]
        if t and t < d:
            res.notes.append("The part is thinner than a skin depth: the probe also sees its far "
                             "side, and thickness changes look like flaws.")
    elif task == HEAT:
        h = ed.induction_heating(int(p["coil_turns"]), p["coil_current_a"], p["coil_length_mm"] / 1000,
                                 p["bar_diameter_mm"] / 1000, f, rho, mu)
        res.values["Power into the bar"] = (round(h["power_w"], 1), "W")
        res.values["Surface power density"] = (round(h["surface_w_per_m2"] / 1e4, 3), "W/cm²")
        res.values["Field in the coil"] = (round(h["h_rms_a_per_m"]), "A/m rms")
        if h["note"]:
            res.notes.append("Note: " + h["note"] + ".")
        freqs = [10 ** (2 + i * 0.1) for i in range(41)]
        res.charts.append(Chart("Heating power against frequency", freqs, {"W": [
            ed.induction_heating(int(p["coil_turns"]), p["coil_current_a"], p["coil_length_mm"] / 1000,
                                 p["bar_diameter_mm"] / 1000, x, rho, mu)["power_w"] for x in freqs]},
            x_label="Hz", y_label="W", log_x=True))
        res.notes.append("Assumes a long coil tight around the bar; a short or loose coil gives less.")
    elif task == PLATE:
        h0 = p["field_peak_a_per_m"]
        per_face = ed.plate_loss_per_area(h0, f, t, rho, mu)
        res.values["Loss per face"] = (round(per_face, 4), "W/m²")
        res.values["Loss per volume"] = (round(2 * per_face / t, 2), "W/m³")
        res.values["Thickness / skin depth"] = round(t / d, 3)
        thick = [t * (0.1 + 0.1 * i) for i in range(40)]
        res.charts.append(Chart("Loss against plate thickness", [x * 1000 for x in thick],
                                {"W/m² per face": [ed.plate_loss_per_area(h0, f, x, rho, mu) for x in thick]},
                                x_label="thickness (mm)", y_label="W/m²"))
        res.notes.append("Exact 1-D solution: an infinite plate with the same tangential field on "
                         "both faces. Lamination loss is the thin end of this curve.")
    else:
        mvol = p["magnet_volume_cm3"] * 1e-6
        moment = ed.magnet_moment(p["magnet_br_t"], mvol)
        h = p["magnet_height_mm"] / 1000
        lift, drag, w = ed.reitz_forces(moment, h, p["speed_m_s"], t, rho)
        res.values["Drag"] = (round(drag, 4), "N")
        res.values["Lift"] = (round(lift, 4), "N")
        res.values["Speed of most drag (w)"] = (round(w, 3), "m/s")
        speeds = [w * 10 ** (-2 + i * 0.1) for i in range(41)]
        res.charts.append(Chart("Forces against speed", speeds, {
            "drag (N)": [ed.reitz_forces(moment, h, v, t, rho)[1] for v in speeds],
            "lift (N)": [ed.reitz_forces(moment, h, v, t, rho)[0] for v in speeds]},
            x_label="m/s", y_label="N", log_x=True))
        res.notes.append("Reitz's thin-sheet result for a point dipole: good when the magnet is "
                         "small next to its height and the sheet is wide; rough for a big magnet close up.")
    return res


MODEL = Model(
    name="Eddy currents",
    goal="Work out eddy-current depth of reach, heating and braking for a conductor.",
    inputs=[
        Input("task", ECT, choices=[ECT, HEAT, PLATE, BRAKE]),
        Input("material", "Aluminium 6061-T6", choices=mat.conductive()),
        Input("conductivity_ms_m", 0.0, unit="MS/m", low=0, high=70,
              help="0 uses the table (or a measured conductivity in the connected data)."),
        Input("mu_r", 0.0, low=0, high=5000, help="0 uses the table."),
        Input("frequency_hz", 100_000.0, unit="Hz", low=1, high=1e8),
        Input("thickness_mm", 2.0, unit="mm", low=0.001, high=500),
        Input("target_depth_mm", 1.0, unit="mm", low=0, high=100, help="Testing: the flaw depth to reach."),
        Input("field_peak_a_per_m", 1000.0, unit="A/m", low=0, high=1e7, help="Plate: peak field."),
        Input("coil_turns", 10, low=1, high=10000, help="Heating."),
        Input("coil_current_a", 100.0, unit="A rms", low=0, high=1e5, help="Heating."),
        Input("coil_length_mm", 100.0, unit="mm", low=1, high=5000, help="Heating."),
        Input("bar_diameter_mm", 25.0, unit="mm", low=0.1, high=1000, help="Heating."),
        Input("magnet_br_t", 1.3, unit="T", low=0.1, high=1.6, help="Braking: N42 is about 1.3 T."),
        Input("magnet_volume_cm3", 7.5, unit="cm³", low=0.01, high=1000, help="Braking."),
        Input("magnet_height_mm", 10.0, unit="mm", low=0.1, high=500, help="Braking: centre above the sheet."),
        Input("speed_m_s", 1.0, unit="m/s", low=0, high=1000, help="Braking."),
    ],
    compute=compute,
    assumptions=["Linear, uniform conductor; sinusoidal fields.",
                 "Ideal shapes: an infinite plate, a long coil, a thin wide sheet."],
    limits=["Magnetic steels: permeability is field dependent (rough answers).",
            "Real 3-D parts and probes need a finite-element model or a calibration."],
    fits=[
        Fit("Conductivity from phase lag on a reference standard",
            "phase = (d*1e-3)*sqrt(pi*f*mu_0*mu_r*sigma*1e6)*180/pi", "d",
            sets={"sigma": "conductivity_ms_m"}, x_unit="mm", x_hint="depth", y_hint="phase",
            help="Flaw depths (mm) on a reference block and the phase lag (°) the probe shows at each."),
    ],
    data_help="A table of measured conductivities (a material column and a %IACS or MS/m "
              "column), or phase readings against known flaw depths on a reference standard.",
)
