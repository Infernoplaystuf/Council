"""
Water erosion on various materials: how fast water wears a surface away,
and how long a part or a bank lasts, compared across materials.

Pick the mechanism that matches what you're looking at:
  * Sand-laden water hitting a surface (a pipe bend, pump casing, nozzle,
    spillway): solid-particle erosion, E = K V^n f(angle).
  * Flowing water on a bed or bank (soil, sand, gravel, clay): it only
    erodes above a critical shear stress (Shields for grains, excess
    shear for cohesive soil).
  * Sliding wear (a seal or bearing in water): Archard's law.

HOW SURE: the forms are standard, the constants are not. Erosion ratios vary
by 10x with particle size, shape and hardness. Treat the starter numbers as
order-of-magnitude and CALIBRATE: put measured erosion (a table of speed and
erosion ratio, or speed and mass loss per mass of sand) in the connected data
and use Calibrate to fit K and n for your material and sand.
"""
from __future__ import annotations

import math

from physics_kit import Chart, Fit, Input, Model, Result
from physics_kit import erosion as er
from physics_kit import materials as mat

IMPACT, BED, SLIDING = ("Sand-laden water hitting a surface", "Flowing water on a bed or bank",
                        "Sliding wear")
YEAR = 365.25 * 24 * 3600
SOILS = {  # name -> (d50 m, grain density, cohesive tau_c Pa or None)
    "Fine sand (0.2 mm)": (0.0002, 2650.0, None),
    "Coarse sand (1 mm)": (0.001, 2650.0, None),
    "Gravel (10 mm)": (0.010, 2650.0, None),
    "Cobbles (100 mm)": (0.100, 2650.0, None),
    "Soft clay (cohesive)": (0.00002, 2650.0, 1.0),
    "Stiff clay (cohesive)": (0.00002, 2650.0, 10.0),
}


def _impact(p, data, res):
    m = mat.get(p["material"])
    k = p["erosion_ratio_override"] or m.erosion_ratio_ref
    n = p["exponent_override"] or m.erosion_exponent
    ductile = m.erosion == "ductile"
    v, a, c = p["speed_m_s"], p["angle_deg"], p["sand_kg_per_m3"]
    rate = er.particle_erosion_rate(v, a, c, m.density, k, n, ductile)
    life = er.time_to_wear(p["wear_allowance_mm"] / 1000, rate)
    res.values.update({
        "Erosion ratio": (f"{er.particle_erosion_ratio(v, a, k, n, ductile):.3g}", "kg/kg"),
        "Wear rate": (round(rate * YEAR * 1000, 4), "mm/year"),
        "Life of the wear allowance": (round(life / YEAR, 2) if math.isfinite(life) else "∞", "years"),
    })
    src = "your calibrated values" if p["erosion_ratio_override"] else f"the starter table ({m.sure})"
    res.notes.append(f"{m.name}: K = {k:.3g} at 100 m/s, n = {n:g}, {m.erosion} (from {src}).")
    rows = []
    for name in mat.erodible():
        mm = mat.get(name)
        r = er.particle_erosion_rate(v, a, c, mm.density, mm.erosion_ratio_ref, mm.erosion_exponent,
                                     mm.erosion == "ductile")
        lf = er.time_to_wear(p["wear_allowance_mm"] / 1000, r)
        rows.append({"Material": name, "Behaves": mm.erosion,
                     "Wear rate (mm/year)": round(r * YEAR * 1000, 4),
                     "Life (years)": round(lf / YEAR, 2) if math.isfinite(lf) else "∞",
                     "How sure": mm.sure})
    rows.sort(key=lambda r: r["Wear rate (mm/year)"])
    res.tables["Materials compared (same water, speed and angle)"] = rows
    speeds = [v * (0.2 + 0.1 * i) for i in range(30)]
    res.charts.append(Chart("Wear rate against speed", speeds, {
        m.name: [er.particle_erosion_rate(s, a, c, m.density, k, n, ductile) * YEAR * 1000
                 for s in speeds]}, x_label="m/s", y_label="mm/year", log_y=True))
    angles = list(range(0, 91, 3))
    res.charts.append(Chart("Effect of impact angle", angles, {
        "ductile (metals, plastics)": [er.finnie_angle(x, True) for x in angles],
        "brittle (glass, stone, concrete)": [er.finnie_angle(x, False) for x in angles]},
        x_label="angle from the surface (°)", y_label="relative erosion"))
    res.fit_constants = {"fa": er.finnie_angle(a, ductile)}
    if ductile and a > 60:
        res.notes.append("Ductile materials wear most at shallow angles (15-30°): a bend's outer "
                         "wall, where the flow grazes it, usually goes first.")


def _bed(p, data, res):
    d50, rho_s, tau_c_cohesive = SOILS[p["soil"]]
    depth, v = p["water_depth_m"], p["speed_m_s"]
    tau = er.bed_shear_from_velocity(v, depth, max(d50, 0.0005))
    if p["tau_c_override"]:
        tau_c = p["tau_c_override"]
    elif tau_c_cohesive is not None:
        tau_c = tau_c_cohesive
    else:
        tau_c = er.shields_critical_stress(d50, rho_s)
    kd = p["kd_override"] * 1e-6 if p["kd_override"] else None
    rate = er.excess_shear_rate(tau, tau_c, kd)
    res.values.update({
        "Bed shear stress": (round(tau, 3), "Pa"),
        "Critical shear stress": (round(tau_c, 3), "Pa"),
        "Moving?": "yes" if tau > tau_c else "no: below the threshold",
        "Erosion rate": (round(rate * 3600 * 1000, 4), "mm/hour"),
    })
    res.notes.append("Rate from the excess-shear law with k_d = 0.2 tau_c^-0.5 cm³/(N·s) unless "
                     "you give k_d. For sand and gravel the rate is rough: it's a transport "
                     "problem; the threshold (moving or not) is the reliable part.")
    rows = []
    for name, (d, rs, tc) in SOILS.items():
        tcr = tc if tc is not None else er.shields_critical_stress(d, rs)
        t2 = er.bed_shear_from_velocity(v, depth, max(d, 0.0005))
        rows.append({"Bed": name, "Critical shear (Pa)": round(tcr, 3),
                     "Shear at this flow (Pa)": round(t2, 3), "Erodes?": "yes" if t2 > tcr else "no",
                     "Rate (mm/hour)": round(er.excess_shear_rate(t2, tcr) * 3600 * 1000, 4)})
    res.tables["Beds compared (same flow)"] = rows
    speeds = [0.05 * i for i in range(1, 61)]
    res.charts.append(Chart("Shear stress against flow speed", speeds, {
        "bed shear (Pa)": [er.bed_shear_from_velocity(s, depth, max(d50, 0.0005)) for s in speeds],
        "threshold (Pa)": [tau_c] * len(speeds)}, x_label="m/s", y_label="Pa"))
    res.fit_constants = {"tau_c": tau_c}


def _sliding(p, data, res):
    m = mat.get(p["material"])
    if not m.hardness_hv:
        res.notes.append(f"{m.name} has no hardness in the table: Archard needs one.")
        return
    H = er.hv_to_pa(m.hardness_hv)
    vol = er.archard_volume(p["archard_k"], p["load_n"], p["sliding_m_per_year"], H)
    area = p["contact_area_mm2"] * 1e-6
    depth = vol / area if area else float("nan")
    life = er.time_to_wear(p["wear_allowance_mm"] / 1000, depth / YEAR)
    res.values.update({"Volume worn": (f"{vol * 1e9:.4g}", "mm³/year"),
                       "Depth worn": (round(depth * 1000, 4), "mm/year"),
                       "Life of the wear allowance": (round(life / YEAR, 2) if math.isfinite(life)
                                                      else "∞", "years")})
    res.notes.append("Archard's K ranges from 1e-8 (lubricated) to 1e-2 (dry, severe); water "
                     "lubricates some pairs and corrodes others. Calibrate K from a test.")


def compute(p, data) -> Result:
    res = Result()
    {IMPACT: _impact, BED: _bed, SLIDING: _sliding}[p["mechanism"]](p, data, res)
    _measured(p, data, res)
    return res


def _measured(p, data, res) -> None:
    if not data or p["mechanism"] != IMPACT:
        return
    for t in data.names():
        cols = data.columns(t)
        vc = next((c for c in cols if "speed" in c.lower() or "velocity" in c.lower()), None)
        ec = next((c for c in cols if "erosion" in c.lower() or "ratio" in c.lower()), None)
        if vc and ec and data.pairs(t, vc, ec):
            res.notes.append(f"'{t}' has {len(data.pairs(t, vc, ec))} erosion measurements: use "
                             "Calibrate to fit K and n for your material and sand.")
            return


MODEL = Model(
    name="Water erosion",
    goal="Estimate how fast water erodes a material or a bed, and compare materials.",
    inputs=[
        Input("mechanism", IMPACT, choices=[IMPACT, BED, SLIDING]),
        Input("material", "Mild steel 1018", choices=mat.erodible()),
        Input("speed_m_s", 5.0, unit="m/s", low=0.01, high=300),
        Input("angle_deg", 30.0, unit="°", low=1, high=90, help="Impact: angle from the surface."),
        Input("sand_kg_per_m3", 10.0, unit="kg/m³", low=0, high=1000, help="Impact: solids in the water."),
        Input("wear_allowance_mm", 3.0, unit="mm", low=0.01, high=500,
              help="How much can wear before it fails (a pipe's corrosion allowance, a liner)."),
        Input("erosion_ratio_override", 0.0, low=0, high=1,
              help="Impact: K at 100 m/s; 0 uses the starter table. Calibrate it."),
        Input("exponent_override", 0.0, low=0, high=5, help="Impact: n; 0 uses the table."),
        Input("soil", "Coarse sand (1 mm)", choices=list(SOILS), help="Bed."),
        Input("water_depth_m", 1.0, unit="m", low=0.01, high=100, help="Bed."),
        Input("tau_c_override", 0.0, unit="Pa", low=0, high=1000, help="Bed: 0 works it out."),
        Input("kd_override", 0.0, unit="cm³/(N·s)", low=0, high=100, help="Bed: 0 uses Hanson & Simon."),
        Input("archard_k", 1e-5, low=1e-10, high=1e-1, help="Sliding."),
        Input("load_n", 100.0, unit="N", low=0, high=1e6, help="Sliding."),
        Input("sliding_m_per_year", 1e6, unit="m/year", low=0, high=1e10, help="Sliding."),
        Input("contact_area_mm2", 100.0, unit="mm²", low=0.01, high=1e6, help="Sliding."),
    ],
    compute=compute,
    assumptions=["Erosion ratio E = K (V/100 m/s)^n f(angle) (Finnie for ductile, sin² for brittle).",
                 "Bed shear from the depth-averaged speed with the rough-wall log law.",
                 "Shields threshold from the Soulsby-Whitehouse fit."],
    limits=["Starter erosion constants are order-of-magnitude: calibrate before trusting a life.",
            "No corrosion, cavitation or droplet impact (they add to, or dominate, erosion).",
            "A real bend or pump concentrates impacts: the worst spot wears several times faster."],
    fits=[
        Fit("K and n from erosion tests", "E = K*(V/100)^n*fa", "V",
            sets={"K": "erosion_ratio_override", "n": "exponent_override"},
            x_hint="speed", y_hint="erosion",
            help="Test speeds (m/s) and the erosion ratio (kg lost per kg of sand) at your angle.",
            when={"mechanism": IMPACT}),
    ],
    data_help="Erosion test results (a speed column and an erosion ratio column), or a "
              "folder of test reports.",
)
