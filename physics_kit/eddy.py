"""
physics_kit.eddy — eddy currents: how deep they reach, the heat they make,
the drag they cause.

    skin_depth(f, resistivity, mu_r)          m: the current density falls to
                                              1/e (37 %) at this depth
    effective_depth(...)                      about 3 skin depths: below it an
                                              eddy-current probe sees almost nothing
    phase_lag_deg(depth, delta)               the signal's phase from that depth
    plate_loss_per_area(H0, f, t, rho, mu_r)  W/m² per face, a plate of ANY
                                              thickness in a tangential field of peak H0
    thin_plate_loss_density(B0, f, t, rho)    W/m³, a thin sheet in a perpendicular
                                              field (lamination / transformer loss)
    induction_heating(N, I, coil_length, d, f, rho, mu_r, work_length)
                                              W into a round bar inside a long coil
    reitz_forces(m, h, v, t, rho)             lift and drag on a magnet (a dipole)
                                              moving over a thin conducting sheet

The plate result is the exact 1-D solution (the field inside is
H0 cosh(kx)/cosh(kt/2), k = (1 + j)/delta): for t >> delta it is the
surface loss rho H0² / (2 delta); for t << delta it is the thin-sheet loss.

LIMITS
Linear materials: steel's permeability changes with the field, so a steel
answer is rough (say so to the user). Shapes are ideal (infinite plate, long
coil, thin sheet); a real 3-D part needs a finite-element solver or a
calibration against a measurement.
"""
from __future__ import annotations

import math
from typing import Dict, Tuple

from .units import MU_0


def skin_depth(f: float, resistivity: float, mu_r: float = 1.0) -> float:
    """delta = sqrt(2 rho / (omega mu)) = 1 / sqrt(pi f mu sigma)."""
    if f <= 0:
        raise ValueError("The frequency must be above zero.")
    return math.sqrt(resistivity / (math.pi * f * MU_0 * mu_r))


def frequency_for_depth(delta: float, resistivity: float, mu_r: float = 1.0) -> float:
    """The frequency whose skin depth is ``delta``."""
    return resistivity / (math.pi * MU_0 * mu_r * delta ** 2)


def effective_depth(f: float, resistivity: float, mu_r: float = 1.0) -> float:
    """~3 skin depths: the density is 5 % of the surface's there."""
    return 3.0 * skin_depth(f, resistivity, mu_r)


def density_at(depth: float, delta: float) -> float:
    """Current density at ``depth`` as a fraction of the surface's (thick plate)."""
    return math.exp(-depth / delta)


def phase_lag_deg(depth: float, delta: float) -> float:
    """The phase lag of the eddy current at ``depth`` (degrees): what an ECT
    instrument uses to tell a deep flaw from a shallow one."""
    return math.degrees(depth / delta)


def plate_loss_per_area(h0: float, f: float, thickness: float, resistivity: float,
                        mu_r: float = 1.0) -> float:
    """Power (W/m²) per face of a plate of ``thickness`` with a tangential
    field of peak ``h0`` (A/m) on both faces."""
    d = skin_depth(f, resistivity, mu_r)
    u = thickness / d
    if u > 50:
        shape = 1.0
    else:
        shape = (math.sinh(u) - math.sin(u)) / (math.cosh(u) + math.cos(u))
    return resistivity * h0 ** 2 / (2 * d) * shape


def thin_plate_loss_density(b0: float, f: float, thickness: float, resistivity: float) -> float:
    """W/m³ in a sheet much thinner than the skin depth, peak flux density
    ``b0`` (T) along it: pi² B0² t² f² / (6 rho)."""
    return math.pi ** 2 * b0 ** 2 * thickness ** 2 * f ** 2 / (6 * resistivity)


def induction_heating(turns: int, current_rms: float, coil_length: float, bar_diameter: float,
                      f: float, resistivity: float, mu_r: float = 1.0,
                      work_length: float = 0.0) -> Dict[str, float]:
    """Power into a round bar centred in a long solenoid (bar radius >> skin
    depth: the field H = N I / l sits on the bar's surface). Returns watts,
    W/m² and the skin depth. ``work_length``: heated length (default the coil's)."""
    d = skin_depth(f, resistivity, mu_r)
    h_rms = turns * current_rms / coil_length
    per_area = resistivity * h_rms ** 2 / d         # rho H_rms² / delta
    length = work_length or coil_length
    area = math.pi * bar_diameter * length
    note = "" if bar_diameter / 2 > 3 * d else (
        "the bar is not much thicker than the skin depth, so this over-estimates the power")
    return {"power_w": per_area * area, "surface_w_per_m2": per_area, "skin_depth_m": d,
            "h_rms_a_per_m": h_rms, "note": note}


def reitz_forces(moment: float, height: float, speed: float, thickness: float,
                 resistivity: float) -> Tuple[float, float, float]:
    """(lift N, drag N, characteristic speed w m/s) on a magnetic dipole of
    ``moment`` (A·m²) at ``height`` above a thin sheet, moving at ``speed``
    (Reitz, J. Appl. Phys. 41, 2067 (1970)). Drag peaks at v = w; at low
    speed it grows with v, at high speed lift takes over."""
    sigma = 1.0 / resistivity
    w = 2.0 / (MU_0 * sigma * thickness)
    f_image = 3 * MU_0 * moment ** 2 / (32 * math.pi * height ** 4)
    v2 = speed ** 2
    lift = f_image * v2 / (v2 + w ** 2)
    drag = f_image * w * speed / (v2 + w ** 2)
    return lift, drag, w


def magnet_moment(br: float, volume: float) -> float:
    """A permanent magnet's dipole moment (A·m²) from its remanence Br (T)
    and volume (m³): m = Br V / mu0 (N42 neodymium: Br ≈ 1.3 T)."""
    return br * volume / MU_0
