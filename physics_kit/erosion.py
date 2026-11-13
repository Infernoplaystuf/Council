"""
physics_kit.erosion — how fast water wears a material away.

THREE MECHANISMS, PICK THE ONE THAT MATCHES
1. Sand or grit carried by water hitting a surface (a pipe bend, a pump, a
   nozzle, a spillway): solid-particle erosion,
       E = K V^n f(angle)          (mass lost / mass of particles that hit)
   Finnie's angle function for ductile materials (worst near 18-30°) or
   sin² for brittle ones (worst at 90°). K is the material's erosion ratio
   at 100 m/s (materials.Material.erosion_ratio_ref).
2. Flowing water on soil, sediment or soft rock (a stream bank, a channel):
   it moves the bed only above a critical shear stress,
       Shields:  tau_c = theta_c (rho_s - rho) g d        (sand and gravel)
       excess shear:  rate = k_d (tau - tau_c)            (cohesive soil)
   with k_d = 0.2 tau_c^-0.5 cm³/(N·s) (Hanson & Simon, 2001) when unmeasured.
3. Sliding contact (a seal, a bearing): Archard, V = K F s / H.

    finnie_angle(alpha_deg, ductile)        f(alpha), 1 at its worst angle
    particle_erosion_ratio(V, alpha, ...)   E (kg/kg)
    particle_erosion_rate(...)              m/s of surface lost, from the
                                            particle mass flux
    bed_shear_stress(depth, slope)          Pa, open channel (rho g R S)
    bed_shear_from_velocity(U, depth, d50)  Pa, from the mean speed (log law)
    shields_critical_stress(d, rho_s)       Pa, the bed starts to move above it
    excess_shear_rate(tau, tau_c, k_d)      m/s of bed lost
    archard_volume(K, F, s, H)              m³ worn away
    time_to_wear(thickness, rate)           s

HOW SURE
The forms are standard; the constants are not. Erosion ratios vary by 10x
with the particle's size, shape and hardness, so the starter values are
order-of-magnitude: CALIBRATE K (or k_d) with a measured mass loss or depth.
"""
from __future__ import annotations

import math
from typing import Optional

from .units import G, NU_WATER, RHO_WATER

FINNIE_PEAK = math.degrees(math.atan(1 / 3))   # 18.43°, where Finnie's curve changes form
_FINNIE_MAX = None


def _finnie_raw(a: float) -> float:
    if a <= math.atan(1 / 3):
        return math.sin(2 * a) - 3 * math.sin(a) ** 2
    return math.cos(a) ** 2 / 3


def finnie_angle(alpha_deg: float, ductile: bool = True) -> float:
    """The angle function, scaled so its worst angle gives 1."""
    a = math.radians(max(0.0, min(90.0, alpha_deg)))
    if not ductile:
        return math.sin(a) ** 2
    global _FINNIE_MAX
    if _FINNIE_MAX is None:
        _FINNIE_MAX = max(_finnie_raw(math.radians(x / 10)) for x in range(0, 901))
    return max(0.0, _finnie_raw(a)) / _FINNIE_MAX


def particle_erosion_ratio(speed: float, alpha_deg: float, ratio_ref: float,
                           exponent: float = 2.4, ductile: bool = True,
                           speed_ref: float = 100.0) -> float:
    """E = K (V / V_ref)^n f(alpha): kg of material lost per kg of particles."""
    return ratio_ref * (speed / speed_ref) ** exponent * finnie_angle(alpha_deg, ductile)


def particle_erosion_rate(speed: float, alpha_deg: float, concentration: float,
                          target_density: float, ratio_ref: float, exponent: float = 2.4,
                          ductile: bool = True) -> float:
    """Surface lost (m/s) where particles at ``concentration`` (kg of solids
    per m³ of water) hit at ``speed`` and ``alpha_deg``: the mass flux onto
    the surface is c V sin(alpha)."""
    flux = concentration * speed * max(math.sin(math.radians(alpha_deg)), 0.02)
    e = particle_erosion_ratio(speed, alpha_deg, ratio_ref, exponent, ductile)
    return e * flux / target_density


def bed_shear_stress(depth: float, slope: float, rho: float = RHO_WATER) -> float:
    """Pa, a wide channel: tau = rho g h S."""
    return rho * G * depth * slope


def bed_shear_from_velocity(mean_speed: float, depth: float, d50: float,
                            rho: float = RHO_WATER) -> float:
    """Pa, from the depth-averaged speed with the rough-wall log law
    (U / u* = 5.75 log10(12.2 h / k_s), k_s = 2.5 d50)."""
    ks = 2.5 * d50
    ratio = 5.75 * math.log10(max(12.2 * depth / ks, 1.0001))
    u_star = mean_speed / ratio
    return rho * u_star ** 2


def shields_parameter_critical(d: float, rho_s: float = 2650.0, rho: float = RHO_WATER,
                               nu: float = NU_WATER) -> float:
    """theta_c from the Soulsby-Whitehouse fit to Shields' curve."""
    s = rho_s / rho
    d_star = d * (G * (s - 1) / nu ** 2) ** (1 / 3)
    return 0.30 / (1 + 1.2 * d_star) + 0.055 * (1 - math.exp(-0.020 * d_star))


def shields_critical_stress(d: float, rho_s: float = 2650.0, rho: float = RHO_WATER) -> float:
    """Pa: the bed shear stress above which grains of size ``d`` (m) move."""
    return shields_parameter_critical(d, rho_s, rho) * (rho_s - rho) * G * d


def erodibility_hanson_simon(tau_c: float) -> float:
    """k_d (m³/(N·s)) from tau_c (Pa) when unmeasured: 0.2 tau_c^-0.5 cm³/(N·s)."""
    return 0.2 * tau_c ** -0.5 * 1e-6


def excess_shear_rate(tau: float, tau_c: float, k_d: Optional[float] = None) -> float:
    """m/s of bed lost: k_d (tau - tau_c), zero below the threshold."""
    k = erodibility_hanson_simon(tau_c) if k_d is None else k_d
    return max(0.0, k * (tau - tau_c))


def archard_volume(k: float, load: float, distance: float, hardness_pa: float) -> float:
    """m³ worn: K F s / H (K dimensionless, 1e-8 to 1e-2)."""
    return k * load * distance / hardness_pa


def hv_to_pa(hv: float) -> float:
    """Vickers hardness (kgf/mm²) to Pa."""
    return hv * 9.80665e6


def time_to_wear(thickness: float, rate: float) -> float:
    """Seconds until ``thickness`` is gone at ``rate`` (m/s); inf when it doesn't wear."""
    return float("inf") if rate <= 0 else thickness / rate
