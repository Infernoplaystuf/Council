"""physics_kit.units — the few conversions the models need, SI inside."""
from __future__ import annotations

INCH = 0.0254            # m
FOOT = 0.3048            # m
MM = 1e-3
CM = 1e-2
GAUSS = 1e-4             # T
MPH = 0.44704            # m/s
LBF = 4.4482216152605    # N
PSI = 6894.757293168     # Pa

C_LIGHT = 299_792_458.0  # m/s
MU_0 = 1.25663706212e-6  # N/A²
EPS_0 = 8.8541878128e-12 # F/m
G = 9.80665              # m/s²
RHO_WATER = 998.2        # kg/m³ at 20 °C
NU_WATER = 1.004e-6      # m²/s at 20 °C


def c_to_k(t_c: float) -> float:
    return t_c + 273.15


def f_to_c(t_f: float) -> float:
    return (t_f - 32.0) * 5.0 / 9.0


def m_to_in(x: float) -> float:
    return x / INCH


def in_to_m(x: float) -> float:
    return x * INCH


def ft_in(x_m: float) -> str:
    """A length as feet and inches to the nearest 1/16 inch, for a cut list."""
    total = round(x_m / INCH * 16) / 16
    ft = int(total // 12)
    inch = total - 12 * ft
    whole = int(inch)
    frac16 = int(round((inch - whole) * 16))
    if frac16 == 16:
        whole, frac16 = whole + 1, 0
    frac = ""
    if frac16:
        n, d = frac16, 16
        while n % 2 == 0:
            n, d = n // 2, d // 2
        frac = f" {n}/{d}"
    return (f"{ft}' " if ft else "") + f'{whole}{frac}"'
