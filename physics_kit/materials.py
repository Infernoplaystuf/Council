"""
physics_kit.materials — a starter table of material properties.

Every number is a typical handbook value at room temperature and says how
sure it is (``sure``): "good" (varies a few % between sources), "fair"
(±20 %, depends on grade or temper), "rough" (order of magnitude only -
calibrate it with your own measurements). Real parts vary: when you have a
datasheet or a measurement, connect it as data and it wins.

    get(name) -> Material   (case and spaces ignored; aliases such as "Al", "copper")
    names()   -> every material's name
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Material:
    name: str
    density: float                 # kg/m³
    youngs_modulus: float          # Pa
    resistivity: Optional[float]   # Ω·m (None: an insulator)
    mu_r: float = 1.0              # relative permeability (iron and steel: field dependent)
    hardness_hv: Optional[float] = None   # Vickers, kgf/mm²
    erosion: str = "ductile"       # "ductile" (worst near 20-30°) or "brittle" (worst at 90°)
    erosion_ratio_ref: Optional[float] = None
    """mass lost / mass of sand hitting it at 100 m/s and its worst angle
    (~200 µm silica sand) - ROUGH starter values only"""
    erosion_exponent: float = 2.4  # n in E ∝ V^n
    sure: str = "fair"
    note: str = ""

    @property
    def conductivity(self) -> Optional[float]:
        return 1.0 / self.resistivity if self.resistivity else None

    @property
    def iacs_percent(self) -> Optional[float]:
        """Conductivity as % IACS (annealed copper = 100 %)."""
        return 1.7241e-8 / self.resistivity * 100.0 if self.resistivity else None


_TABLE: List[Material] = [
    Material("Copper", 8960, 117e9, 1.68e-8, 1.0, 50, "ductile", 1.5e-3, 2.4, "good",
             "annealed, pure"),
    Material("Aluminium 6061-T6", 2700, 68.9e9, 3.99e-8, 1.0, 107, "ductile", 2.0e-3, 2.4,
             "good", "the common extrusion alloy"),
    Material("Aluminium (pure)", 2700, 69e9, 2.65e-8, 1.0, 25, "ductile", 2.5e-3, 2.4, "good"),
    Material("Brass C260", 8530, 110e9, 6.2e-8, 1.0, 100, "ductile", 1.5e-3, 2.4, "fair",
             "cartridge brass"),
    Material("Mild steel 1018", 7870, 205e9, 1.59e-7, 200.0, 126, "ductile", 5e-4, 2.4, "fair",
             "mu_r of steel depends strongly on the field: 100-2000; treat it as rough"),
    Material("Stainless steel 304", 8000, 193e9, 7.2e-7, 1.008, 200, "ductile", 4e-4, 2.4,
             "good", "non-magnetic when annealed"),
    Material("Titanium Ti-6Al-4V", 4430, 114e9, 1.78e-6, 1.0, 349, "ductile", 6e-4, 2.4, "good"),
    Material("PVC (rigid)", 1380, 3.0e9, None, 1.0, None, "ductile", 2e-3, 2.6, "fair",
             "E is 2.4-4.1 GPa between makers: calibrate a struck-tube model by ear"),
    Material("HDPE", 950, 0.8e9, None, 1.0, None, "ductile", 1e-3, 2.7, "fair"),
    Material("Glass (soda-lime)", 2500, 70e9, None, 1.0, 550, "brittle", 3e-3, 2.8, "fair"),
    Material("Concrete", 2400, 30e9, None, 1.0, None, "brittle", 5e-3, 2.5, "rough",
             "strength and erosion vary widely with the mix"),
    Material("Granite", 2700, 50e9, None, 1.0, None, "brittle", 1e-3, 2.5, "rough"),
    Material("Sandstone", 2300, 15e9, None, 1.0, None, "brittle", 2e-2, 2.5, "rough"),
    Material("Hardwood (oak, along grain)", 750, 11e9, None, 1.0, None, "ductile", None, 2.4,
             "rough", "for struck bars; varies with species and moisture"),
    Material("Rosewood (along grain)", 850, 16e9, None, 1.0, None, "ductile", None, 2.4,
             "rough", "the classic marimba wood"),
]

_ALIASES = {"al": "Aluminium 6061-T6", "aluminum": "Aluminium 6061-T6",
            "aluminium": "Aluminium 6061-T6", "6061": "Aluminium 6061-T6",
            "cu": "Copper", "copper": "Copper", "brass": "Brass C260",
            "steel": "Mild steel 1018", "mild steel": "Mild steel 1018",
            "stainless": "Stainless steel 304", "ss304": "Stainless steel 304",
            "titanium": "Titanium Ti-6Al-4V", "ti64": "Titanium Ti-6Al-4V",
            "pvc": "PVC (rigid)", "hdpe": "HDPE", "glass": "Glass (soda-lime)",
            "concrete": "Concrete", "granite": "Granite", "sandstone": "Sandstone",
            "oak": "Hardwood (oak, along grain)", "rosewood": "Rosewood (along grain)"}

_BY_KEY: Dict[str, Material] = {m.name.lower().replace(" ", ""): m for m in _TABLE}


def names() -> List[str]:
    return [m.name for m in _TABLE]


def get(name: str) -> Material:
    key = (name or "").strip().lower()
    key = _ALIASES.get(key, key).lower().replace(" ", "")
    if key in _BY_KEY:
        return _BY_KEY[key]
    for k, m in _BY_KEY.items():
        if key and key in k:
            return m
    raise KeyError(f"No material '{name}'. Known: {', '.join(names())}")


def conductive() -> List[str]:
    return [m.name for m in _TABLE if m.resistivity]


def erodible() -> List[str]:
    return [m.name for m in _TABLE if m.erosion_ratio_ref]
