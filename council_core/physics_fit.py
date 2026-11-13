"""
council_core.physics_fit — fit a physics equation to data (user, 2026-11-05:
"Is there a way inside the council that we can work on creating physics
models?"; picked "fit equations to my data first, simulate second").

One input field (x) and one result field (y). The equation is either one of
the built-in physics laws (EQUATIONS: Arrhenius, Newton's cooling,
Hall-Petch, power law, ...) or the user's own, typed as text:

    y = A*exp(-Ea/(R*T))          # a built-in: input T, parameters A and Ea
    y = a + b*x^2                 # your own: x is the input field

    parse(text, inputs)         -> Equation: the parameters it has, compiled safely
    fit(eq, x, y, guesses)      -> Fit: each parameter with its standard error and
                                   95 % range, R², RMSE, AICc, residuals, notes
    predict(fit, x)             -> the value at x with a 95 % range for the mean and
                                   for one new measurement
    suggest(x_name, x_unit, y_name, y_unit) -> the built-ins worth trying, best first
    try_all(keys, x, y)         -> each of them fitted, ranked by AICc
    fit_figure(fit)             -> the data, the curve with its 95 % band, residuals
    save(fit, path)             -> appended to a JSON list of saved models

SAFE BY CONSTRUCTION
The text is parsed with `ast` and only numbers, names, + - * / ** (or ^) and
the maths functions in FUNCTIONS are allowed; anything else (attributes,
subscripts, lambdas, imports) is refused before anything is evaluated.

CONSTANTS
Names in CONSTANTS (R the gas constant, k_B, g_0, pi, ...) are fixed values,
not parameters. To fit a name that is also a constant, give it a starting
guess ("R=1"): a name with a guess is always a parameter.

UNITS
A built-in says what its parameters' units are in terms of x's and y's
(Arrhenius' Ea is J/mol when R is used). A law that needs an absolute
temperature (Arrhenius) converts a °C or °F input to kelvin and says so. Your
own equation's parameter units are not worked out.

Nothing here imports Qt.
"""
from __future__ import annotations

import ast
import datetime as _dt
import json
import math
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np


class FitError(ValueError):
    """The equation cannot be read, or the data cannot be fitted with it."""


# ── what an equation may use ────────────────────────────────────────────
FUNCTIONS: Dict[str, Callable] = {
    "exp": np.exp, "log": np.log, "ln": np.log, "log10": np.log10, "log2": np.log2,
    "sqrt": np.sqrt, "abs": np.abs, "sin": np.sin, "cos": np.cos, "tan": np.tan,
    "asin": np.arcsin, "acos": np.arccos, "atan": np.arctan, "arcsin": np.arcsin,
    "arccos": np.arccos, "arctan": np.arctan, "sinh": np.sinh, "cosh": np.cosh,
    "tanh": np.tanh, "erf": None, "erfc": None,
}
try:                                                      # scipy is in requirements.txt
    from scipy import special as _special
    FUNCTIONS["erf"], FUNCTIONS["erfc"] = _special.erf, _special.erfc
except Exception:                                         # noqa: BLE001
    del FUNCTIONS["erf"], FUNCTIONS["erfc"]

#: name -> (value, unit, what it is)
CONSTANTS: Dict[str, Tuple[float, str, str]] = {
    "pi": (math.pi, "", "pi"),
    "R": (8.314462618, "J/(mol·K)", "gas constant"),
    "k_B": (1.380649e-23, "J/K", "Boltzmann constant"),
    "k_B_eV": (8.617333262e-5, "eV/K", "Boltzmann constant in eV"),
    "N_A": (6.02214076e23, "1/mol", "Avogadro constant"),
    "h_P": (6.62607015e-34, "J·s", "Planck constant"),
    "c_0": (299792458.0, "m/s", "speed of light"),
    "g_0": (9.80665, "m/s²", "standard gravity"),
    "sigma_SB": (5.670374419e-8, "W/(m²·K⁴)", "Stefan-Boltzmann constant"),
    "q_e": (1.602176634e-19, "C", "elementary charge"),
    "eps_0": (8.8541878128e-12, "F/m", "vacuum permittivity"),
    "mu_0": (1.25663706212e-6, "N/A²", "vacuum permeability"),
}

_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Load,
                  ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub,
                  ast.UAdd)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Equation:
    text: str                     # as typed, "y = ..." form
    lhs: str                      # the result's name ("y", "k", ...)
    rhs: str                      # the expression
    inputs: Tuple[str, ...]       # names bound to data
    params: Tuple[str, ...]       # names fitted, in order of first use
    constants: Tuple[str, ...]    # constants it uses
    _code: object = None
    fixed: Dict[str, float] = field(default_factory=dict)   # the model's own fixed values

    @property
    def fixed_text(self) -> str:
        return ", ".join(f"{k} = {v:.6g}" for k, v in self.fixed.items())

    def __call__(self, values: Dict[str, np.ndarray], p: Sequence[float]) -> np.ndarray:
        ns: Dict[str, object] = dict(FUNCTIONS)
        ns.update({c: CONSTANTS[c][0] for c in self.constants})
        ns.update(self.fixed)
        ns.update(zip(self.params, (float(v) for v in p)))
        ns.update(values)
        with np.errstate(all="ignore"):
            out = eval(self._code, {"__builtins__": {}}, ns)   # noqa: S307 - checked by parse()
        return np.broadcast_to(np.asarray(out, dtype=float),
                               np.shape(next(iter(values.values())))).astype(float)


def parse(text: str, inputs: Sequence[str] = ("x",),
          force_params: Sequence[str] = (), fixed: Optional[Dict[str, float]] = None
          ) -> Equation:
    """``text`` ("y = a*exp(b*x)" or just the right-hand side) as an Equation.
    ``inputs`` are the names bound to data; every other name that is not a
    function, a constant or in ``fixed`` (values the model supplies, such as
    the speed of sound at the chosen temperature) is a parameter, and so is
    any name in ``force_params`` (a name the user gave a starting guess)."""
    fixed = {k: float(v) for k, v in (fixed or {}).items()}
    raw = (text or "").strip()
    if not raw:
        raise FitError("Type an equation, for example  y = a*exp(-b*x)")
    lhs, rhs = "y", raw
    if "=" in raw:
        left, _, right = raw.partition("=")
        if "=" in right:
            raise FitError("Use one '=' only: the result's name on the left, the formula on the right.")
        left = left.strip()
        if not _NAME.match(left):
            raise FitError(f"The left of '=' should be one name (the result), not '{left}'.")
        lhs, rhs = left, right.strip()
    rhs_py = rhs.replace("^", "**").replace("·", "*").replace("×", "*").replace("−", "-")
    try:
        tree = ast.parse(rhs_py, mode="eval")
    except SyntaxError as e:
        raise FitError(f"Can't read the formula '{rhs}': {e.msg}.") from None
    used: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise FitError(f"'{ast.unparse(node) if hasattr(ast, 'unparse') else node}' "
                           "is not allowed: use numbers, names, + - * / ^ and maths "
                           "functions such as exp, log, sqrt, sin.")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise FitError("Only numbers may appear in the formula, not text.")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                name = getattr(node.func, "id", "?")
                raise FitError(f"'{name}(...)' is not a function the fit knows. Known: "
                               + ", ".join(sorted(FUNCTIONS)) + ".")
            if node.keywords:
                raise FitError("Functions take plain arguments here, no name=value.")
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in calls and node.id not in used:
            used.append(node.id)
    inputs = tuple(inputs)
    missing = [i for i in inputs if i not in used]
    if missing:
        raise FitError(f"The formula doesn't use the input {', '.join(missing)}. "
                       f"In your own equation, write {inputs[0]} for the input field.")
    if lhs in used:
        raise FitError(f"'{lhs}' is the result; it can't also be on the right.")
    forced = set(force_params)
    params = tuple(n for n in used if n not in inputs
                   and (n in forced or (n not in CONSTANTS and n not in FUNCTIONS
                                        and n not in fixed)))
    consts = tuple(n for n in used if n in CONSTANTS and n not in params and n not in inputs
                   and n not in fixed)
    fixed = {k: v for k, v in fixed.items() if k in used and k not in params}
    if not params:
        raise FitError("The formula has nothing to fit: add at least one parameter, "
                       "for example  y = a*x + b.")
    code = compile(tree, "<equation>", "eval")
    return Equation(text=f"{lhs} = {rhs}", lhs=lhs, rhs=rhs, inputs=inputs, params=params,
                    constants=consts, _code=code, fixed=fixed)


# ── built-in physics laws ───────────────────────────────────────────────
@dataclass
class Law:
    key: str
    name: str
    text: str                     # with its own input name
    input: str
    why: str                      # one line: where it is used
    units: Dict[str, str] = field(default_factory=dict)   # param -> "[y]/[x]" style
    absolute_temperature: bool = False
    guess: Optional[Callable[[np.ndarray, np.ndarray], Dict[str, float]]] = None
    keywords: Tuple[str, ...] = ()


def _lin(x, y) -> Tuple[float, float]:
    """Intercept, slope of the least-squares line."""
    if len(x) < 2 or np.ptp(x) == 0:
        return float(np.mean(y)), 0.0
    b, a = np.polyfit(x, y, 1)
    return float(a), float(b)


def _g_line(x, y):
    a, b = _lin(x, y)
    return {"a": a, "b": b}


def _g_quad(x, y):
    if len(x) >= 3 and np.ptp(x) > 0:
        c, b, a = np.polyfit(x, y, 2)
        return {"a": float(a), "b": float(b), "c": float(c)}
    return _g_line(x, y) | {"c": 0.0}


def _g_power(x, y):
    m = (x > 0) & (y > 0)
    if m.sum() >= 2:
        la, b = _lin(np.log(x[m]), np.log(y[m]))
        return {"a": math.exp(la), "b": b}
    return {"a": float(np.mean(y)) or 1.0, "b": 1.0}


def _g_exp(x, y):
    sign = 1.0 if np.median(y) >= 0 else -1.0
    m = sign * y > 0
    if m.sum() >= 2:
        la, b = _lin(x[m], np.log(sign * y[m]))
        return {"a": sign * math.exp(la), "b": b}
    return {"a": float(np.mean(y)) or 1.0, "b": 0.0}


def _g_decay(x, y):
    order = np.argsort(x)
    xs, ys = x[order], y[order]
    c = float(ys[-1])
    span = float(np.ptp(xs)) or 1.0
    return {"y0": float(ys[0] - c) or 1.0, "tau": span / 3.0, "c": c}


def _g_arrhenius(t, y):
    m = (t > 0) & (y > 0)
    if m.sum() >= 2:
        la, slope = _lin(1.0 / t[m], np.log(y[m]))
        return {"A": math.exp(min(la, 700.0)), "Ea": -slope * CONSTANTS["R"][0]}
    return {"A": float(np.mean(y)) or 1.0, "Ea": 1e4}


def _g_newton(t, y):
    order = np.argsort(t)
    ts, ys = t[order], y[order]
    return {"T_env": float(ys[-1]), "T0": float(ys[0]),
            "k": 3.0 / (float(np.ptp(ts)) or 1.0)}


def _g_hall_petch(d, y):
    m = d > 0
    a, b = _lin(1.0 / np.sqrt(d[m]), y[m]) if m.sum() >= 2 else (float(np.mean(y)), 0.0)
    return {"sigma_0": a, "k_HP": b}


def _g_inverse_square(x, y):
    m = x != 0
    c, a = _lin(1.0 / x[m] ** 2, y[m]) if m.sum() >= 2 else (0.0, 1.0)
    return {"a": a, "c": c}


def _g_logistic(x, y):
    lo, hi = float(np.min(y)), float(np.max(y))
    mid = (lo + hi) / 2
    x0 = float(x[np.argmin(np.abs(y - mid))])
    k = 4.0 / (float(np.ptp(x)) or 1.0)
    order = np.argsort(x)
    if y[order][0] > y[order][-1]:
        k = -k
    return {"L": hi if k > 0 else hi, "k": k, "x0": x0}


def _g_saturation(x, y):
    top = float(np.max(y)) or 1.0
    half = x[np.argmin(np.abs(y - top / 2))]
    return {"V_max": top * 1.2, "K_m": float(half) or 1.0}


def _g_gauss(x, y):
    c = float(np.median(y))
    i = int(np.argmax(np.abs(y - c)))
    return {"A": float(y[i] - c), "mu": float(x[i]),
            "s": (float(np.ptp(x)) or 1.0) / 6.0, "c": c}


def _g_damped(t, y):
    c = float(np.mean(y))
    order = np.argsort(t)
    ts, ys = t[order], y[order] - c
    span = float(np.ptp(ts)) or 1.0
    f = 1.0 / span
    if len(ts) >= 8:
        dt = np.median(np.diff(ts)) or span / len(ts)
        grid = np.arange(ts[0], ts[-1], dt) if dt > 0 else ts
        if len(grid) >= 8:
            yi = np.interp(grid, ts, ys)
            spec = np.abs(np.fft.rfft(yi * np.hanning(len(yi))))
            freqs = np.fft.rfftfreq(len(yi), dt)
            if len(spec) > 1:
                f = float(freqs[1 + int(np.argmax(spec[1:]))])
    return {"A": float(np.max(np.abs(ys))) or 1.0, "tau": span / 2.0, "f": f,
            "phi": 0.0, "c": c}


TIME_WORDS = ("time", "age", "hour", "minute", "second", "day", "elapsed", "duration",
              "cycle", "t_")
TEMP_WORDS = ("temp", "temperature", "°c", "degc", "kelvin", "deg c", "°f")
SIZE_WORDS = ("grain", "diameter", "size", "particle", "thickness", "width")
DIST_WORDS = ("distance", "radius", "range", "separation", "gap")
STRENGTH_WORDS = ("strength", "yield", "hardness", "stress", "uts")

LAWS: Dict[str, Law] = {law.key: law for law in (
    Law("line", "Straight line", "y = a + b*x", "x",
        "Hooke's law, Ohm's law, thermal expansion, any proportional relation",
        {"a": "[y]", "b": "[y]/[x]"}, guess=_g_line),
    Law("quadratic", "Quadratic", "y = a + b*x + c*x^2", "x",
        "constant acceleration (s = s0 + v0 t + ½ a t²), drag at low speed, curvature",
        {"a": "[y]", "b": "[y]/[x]", "c": "[y]/[x]²"}, guess=_g_quad),
    Law("power", "Power law", "y = a*x^b", "x",
        "scaling laws, Paris crack growth (da/dN = C ΔK^m), drag, flow through pipes",
        {"a": "[y]/[x]^b", "b": "none"}, guess=_g_power),
    Law("exponential", "Exponential", "y = a*exp(b*x)", "x",
        "growth or decay with no floor: radioactive decay, attenuation (Beer-Lambert)",
        {"a": "[y]", "b": "1/[x]"}, guess=_g_exp, keywords=TIME_WORDS),
    Law("decay", "Exponential decay to a floor", "y = y0*exp(-x/tau) + c", "x",
        "relaxation, RC charging, settling to a steady value",
        {"y0": "[y]", "tau": "[x]", "c": "[y]"}, guess=_g_decay, keywords=TIME_WORDS),
    Law("newton_cooling", "Newton's law of cooling", "T = T_env + (T0 - T_env)*exp(-k*t)", "t",
        "a part cooling or heating toward its surroundings over time",
        {"T_env": "[y]", "T0": "[y]", "k": "1/[x]"}, guess=_g_newton,
        keywords=TIME_WORDS),
    Law("arrhenius", "Arrhenius", "k = A*exp(-Ea/(R*T))", "T",
        "rates against temperature: diffusion, creep, reaction, ageing, viscosity",
        {"A": "[y]", "Ea": "J/mol"}, absolute_temperature=True, guess=_g_arrhenius,
        keywords=TEMP_WORDS),
    Law("hall_petch", "Hall-Petch", "sigma = sigma_0 + k_HP/sqrt(d)", "d",
        "yield strength or hardness against grain size",
        {"sigma_0": "[y]", "k_HP": "[y]·[x]^½"}, guess=_g_hall_petch, keywords=SIZE_WORDS),
    Law("inverse_square", "Inverse square", "y = a/x^2 + c", "x",
        "intensity, gravity or field strength against distance",
        {"a": "[y]·[x]²", "c": "[y]"}, guess=_g_inverse_square, keywords=DIST_WORDS),
    Law("logistic", "Logistic (S-curve)", "y = L/(1 + exp(-k*(x - x0)))", "x",
        "a switch-over: transformation fraction, saturation with a threshold",
        {"L": "[y]", "k": "1/[x]", "x0": "[x]"}, guess=_g_logistic),
    Law("saturation", "Saturation (Langmuir / Michaelis-Menten)", "y = V_max*x/(K_m + x)", "x",
        "adsorption, rates that level off, sensor response",
        {"V_max": "[y]", "K_m": "[x]"}, guess=_g_saturation),
    Law("gaussian", "Gaussian peak", "y = A*exp(-(x - mu)^2/(2*s^2)) + c", "x",
        "a spectral or diffraction peak, a beam profile",
        {"A": "[y]", "mu": "[x]", "s": "[x]", "c": "[y]"}, guess=_g_gauss),
    Law("damped", "Damped oscillation", "y = A*exp(-t/tau)*cos(2*pi*f*t + phi) + c", "t",
        "vibration ringing down, a spring-mass-damper, an LC circuit",
        {"A": "[y]", "tau": "[x]", "f": "1/[x]", "phi": "rad", "c": "[y]"},
        guess=_g_damped, keywords=TIME_WORDS),
)}


def law_equation(law: Law, force_params: Sequence[str] = ()) -> Equation:
    return parse(law.text, (law.input,), force_params)


# ── units ───────────────────────────────────────────────────────────────
_CELSIUS = re.compile(r"^\s*(°\s*c|deg\s*c|degc|celsius|c)\s*$", re.I)
_FAHRENHEIT = re.compile(r"^\s*(°\s*f|deg\s*f|degf|fahrenheit|f)\s*$", re.I)
_KELVIN = re.compile(r"^\s*(k|kelvin|°\s*k)\s*$", re.I)


def to_kelvin(x: np.ndarray, unit: str) -> Tuple[np.ndarray, str]:
    """``x`` as kelvin, and a note saying what was done ('' when nothing)."""
    u = unit or ""
    if _CELSIUS.match(u):
        return x + 273.15, "Temperatures were in °C: converted to kelvin (+273.15) for the fit."
    if _FAHRENHEIT.match(u):
        return (x - 32.0) * 5.0 / 9.0 + 273.15, ("Temperatures were in °F: converted to "
                                                  "kelvin for the fit.")
    if _KELVIN.match(u):
        return x, ""
    if len(x) and np.nanmax(x) < 200:
        return x, ("These temperatures have no unit and are all below 200: if they are °C, "
                   "set the field's unit to °C so they are converted to kelvin, or the "
                   "activation energy will be wrong.")
    return x, ""


def param_unit(rule: str, x_unit: str, y_unit: str) -> str:
    """A law's unit rule ("[y]/[x]") with the fields' units put in."""
    if not rule or rule == "none":
        return "" if rule != "none" else "(none)"
    if "[" not in rule:
        return rule
    if ("[x]" in rule and not x_unit) or ("[y]" in rule and not y_unit):
        return ""
    def wrap(u):
        return f"({u})" if re.search(r"[/·* ]", u) else u
    return rule.replace("[x]", wrap(x_unit)).replace("[y]", wrap(y_unit))


# ── fitting ─────────────────────────────────────────────────────────────
@dataclass
class Param:
    name: str
    value: float
    stderr: float                 # nan when it can't be worked out
    low: float                    # 95 % range
    high: float
    unit: str = ""


@dataclass
class Fit:
    equation: Equation
    law: Optional[Law]
    x: np.ndarray                 # as fitted (kelvin for an absolute-temperature law)
    y: np.ndarray
    params: List[Param]
    covariance: np.ndarray
    r2: float
    rmse: float
    aicc: float
    dof: int
    notes: List[str] = field(default_factory=list)
    x_name: str = "x"
    y_name: str = "y"
    x_unit: str = ""
    y_unit: str = ""
    x_shift: str = ""             # "C" / "F" when x was converted to kelvin

    @property
    def n(self) -> int:
        return int(len(self.x))

    @property
    def values(self) -> np.ndarray:
        return np.array([p.value for p in self.params])

    def curve(self, x: np.ndarray) -> np.ndarray:
        return self.equation({self.equation.inputs[0]: np.asarray(x, dtype=float)}, self.values)

    @property
    def residuals(self) -> np.ndarray:
        return self.y - self.curve(self.x)

    def title(self) -> str:
        return self.law.name if self.law else "Your equation"

    def table(self) -> List[Dict[str, object]]:
        """A row per parameter, for a table."""
        return [{"Parameter": p.name, "Value": p.value,
                 "± (1 std error)": p.stderr, "95% low": p.low, "95% high": p.high,
                 "Unit": p.unit} for p in self.params]

    def summary(self) -> str:
        bits = [f"{self.equation.text}", f"n = {self.n}", f"R² = {self.r2:.4f}",
                f"RMSE = {self.rmse:.4g}" + (f" {self.y_unit}" if self.y_unit else "")]
        return "   ".join(bits)


def _starts(eq: Equation, guess: Dict[str, float], x: np.ndarray, y: np.ndarray,
            tries: int) -> List[np.ndarray]:
    k = len(eq.params)
    base = np.array([guess.get(p, 1.0) for p in eq.params], dtype=float)
    base[~np.isfinite(base)] = 1.0
    out = [base]
    rng = np.random.default_rng(12345)
    yscale = float(np.max(np.abs(y))) or 1.0
    xscale = float(np.max(np.abs(x))) or 1.0
    scales = np.array([1.0, yscale, xscale, 1.0 / xscale])
    for _ in range(tries):
        mag = 10.0 ** rng.uniform(-3, 3, k) * rng.choice(scales, k)
        sign = rng.choice([-1.0, 1.0], k)
        start = np.where(np.isin(np.array(eq.params), list(guess)),
                         base * (10.0 ** rng.normal(0, 0.5, k)), sign * mag)
        out.append(start)
    return out


def _best(eq, resid, guess, x, y, k, tries, least_squares):
    """The lowest-cost least-squares solution over the starting points."""
    best = None
    for start in _starts(eq, guess, x, y, tries):
        try:
            res = least_squares(resid, start, method="lm" if len(x) > k else "trf",
                                x_scale="jac", max_nfev=400 * (k + 1))
        except Exception:                                 # noqa: BLE001 - try the next start
            continue
        if not np.all(np.isfinite(res.x)):
            continue
        cost = float(np.sum(res.fun ** 2))
        if best is None or cost < best[0] * (1 - 1e-9):
            best = (cost, res)
        if best and len(guess) == k and best[0] <= 1e-12 * (np.sum((y - y.mean()) ** 2) + 1e-300):
            break
    return best


def fit(eq: Equation, x: Sequence[float], y: Sequence[float],
        guesses: Optional[Dict[str, float]] = None, law: Optional[Law] = None,
        x_name: str = "x", y_name: str = "y", x_unit: str = "", y_unit: str = "",
        tries: int = 40) -> Fit:
    """Least squares fit of ``eq`` to the points (x, y). ``guesses`` are the
    user's starting values (any parameter); a built-in ``law`` adds its own
    starting values worked out from the data, and its units."""
    from scipy.optimize import least_squares
    from scipy.stats import t as student_t

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    keep = np.isfinite(x) & np.isfinite(y)
    notes: List[str] = []
    if (~keep).any():
        notes.append(f"{int((~keep).sum())} point(s) without both values were left out.")
    x, y = x[keep], y[keep]
    shift = ""
    if law is not None and law.absolute_temperature:
        x, note = to_kelvin(x, x_unit)
        if note:
            notes.append(note)
            if "°C" in note:
                shift = "C"
            elif "°F" in note:
                shift = "F"
    k = len(eq.params)
    if len(x) <= k:
        raise FitError(f"{len(x)} point(s) for {k} parameter(s): a fit needs more points "
                       "than parameters.")
    if np.ptp(x) == 0:
        raise FitError(f"Every {x_name} is the same ({x[0]:g}): nothing to fit against.")
    guess: Dict[str, float] = {}
    if law is not None and law.guess is not None:
        try:
            guess.update({kk: v for kk, v in law.guess(x, y).items() if np.isfinite(v)})
        except Exception:                                 # noqa: BLE001 - a guess is a guess
            pass
    guess.update(guesses or {})
    name = eq.inputs[0]

    def resid(p):
        r = eq({name: x}, p) - y
        r[~np.isfinite(r)] = 1e12 * (float(np.max(np.abs(y))) + 1.0)
        return r

    best = None
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # wild starts overflow; they lose
        best = _best(eq, resid, guess, x, y, k, tries, least_squares)
    if best is None:
        raise FitError("The fit didn't converge from any starting point. Give starting "
                       "guesses for the parameters (for example  Ea=50000, A=1e6).")
    sse, res = best
    resid_final = res.fun
    big = 1e11 * (float(np.max(np.abs(y))) + 1.0)
    if np.any(np.abs(resid_final) >= big):
        raise FitError("The equation can't be evaluated at some of the points (a log of a "
                       "negative number, a division by zero, ...). Check the equation or the data.")
    n = len(x)
    dof = n - k
    s2 = sse / dof
    J = res.jac
    try:
        cov = np.linalg.pinv(J.T @ J) * s2
    except np.linalg.LinAlgError:
        cov = np.full((k, k), np.nan)
    tq = float(student_t.ppf(0.975, dof))
    params: List[Param] = []
    for i, pname in enumerate(eq.params):
        se = math.sqrt(cov[i, i]) if np.isfinite(cov[i, i]) and cov[i, i] >= 0 else float("nan")
        v = float(res.x[i])
        unit = param_unit(law.units.get(pname, ""), "K" if shift or (law and law.absolute_temperature and _KELVIN.match(x_unit or "")) else x_unit, y_unit) if law else ""
        params.append(Param(pname, v, se, v - tq * se, v + tq * se, unit))
    sst = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - sse / sst if sst > 0 else float("nan")
    rmse = math.sqrt(sse / n)
    aic = n * math.log(max(sse / n, 1e-300)) + 2 * k
    aicc = aic + (2 * k * (k + 1)) / (n - k - 1) if n - k - 1 > 0 else float("inf")
    # what a physicist would want told
    for i, p in enumerate(params):
        if np.isfinite(p.stderr) and p.value != 0 and p.stderr > abs(p.value):
            notes.append(f"{p.name} is not pinned down by this data (its error is larger "
                         "than its value): more points, or a wider range of "
                         f"{x_name}, would help.")
    if k > 1 and np.all(np.isfinite(cov)):
        sd = np.sqrt(np.clip(np.diag(cov), 1e-300, None))
        corr = cov / np.outer(sd, sd)
        for i in range(k):
            for j in range(i + 1, k):
                if abs(corr[i, j]) > 0.99:
                    notes.append(f"{params[i].name} and {params[j].name} move together "
                                 f"(r = {corr[i, j]:+.3f}): this data can't separate them "
                                 "well, so their ranges are wide together.")
    if n < 3 * k:
        notes.append(f"Only {n} points for {k} parameters: treat the ranges with care.")
    eq_law = law
    return Fit(equation=eq, law=eq_law, x=x, y=y, params=params, covariance=cov, r2=r2,
               rmse=rmse, aicc=aicc, dof=dof, notes=notes, x_name=x_name, y_name=y_name,
               x_unit=x_unit, y_unit=y_unit, x_shift=shift)


def fit_law(key: str, x, y, guesses: Optional[Dict[str, float]] = None, **kw) -> Fit:
    law = LAWS[key]
    eq = law_equation(law, tuple(guesses or ()))
    return fit(eq, x, y, guesses, law=law, **kw)


@dataclass
class Prediction:
    x: float                      # as the user gave it
    value: float
    mean_low: float               # 95 % range for the curve itself
    mean_high: float
    new_low: float                # 95 % range for one new measurement
    new_high: float
    outside: bool                 # beyond the data's range (an extrapolation)


def _x_in(fit_: Fit, xv: np.ndarray) -> np.ndarray:
    if fit_.x_shift == "C":
        return xv + 273.15
    if fit_.x_shift == "F":
        return (xv - 32.0) * 5.0 / 9.0 + 273.15
    return xv


def _band(fit_: Fit, xs: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Curve, half-width of its 95 % range, half-width for a new point, at
    already-converted xs (delta method)."""
    from scipy.stats import t as student_t
    p = fit_.values
    name = fit_.equation.inputs[0]
    yhat = fit_.equation({name: xs}, p)
    grads = np.zeros((len(xs), len(p)))
    for i in range(len(p)):
        h = 1e-6 * max(abs(p[i]), 1e-8)
        up, dn = p.copy(), p.copy()
        up[i] += h
        dn[i] -= h
        grads[:, i] = (fit_.equation({name: xs}, up) - fit_.equation({name: xs}, dn)) / (2 * h)
    var = np.einsum("ij,jk,ik->i", grads, fit_.covariance, grads)
    var = np.where(np.isfinite(var) & (var >= 0), var, np.nan)
    tq = float(student_t.ppf(0.975, fit_.dof))
    s2 = float(np.sum(fit_.residuals ** 2)) / fit_.dof
    return yhat, tq * np.sqrt(var), tq * np.sqrt(var + s2)


def predict(fit_: Fit, x: float) -> Prediction:
    """The fitted value at ``x`` (in the field's own unit) with its ranges."""
    xv = _x_in(fit_, np.array([float(x)]))
    y, half, half_new = (a[0] for a in _band(fit_, xv))
    lo, hi = float(np.min(fit_.x)), float(np.max(fit_.x))
    return Prediction(float(x), float(y), float(y - half), float(y + half),
                      float(y - half_new), float(y + half_new),
                      outside=bool(xv[0] < lo or xv[0] > hi))


# ── which laws to try ───────────────────────────────────────────────────
def _has(text: str, words: Sequence[str]) -> bool:
    t = (text or "").lower()
    return any(w in t for w in words)


def suggest(x_name: str, x_unit: str = "", y_name: str = "", y_unit: str = "") -> List[str]:
    """The built-ins worth trying for this pair, most likely first."""
    x_text, y_text = f"{x_name} {x_unit}", f"{y_name} {y_unit}"
    x_temp = _has(x_text, TEMP_WORDS) or bool(_CELSIUS.match(x_unit or "")
                                              or _FAHRENHEIT.match(x_unit or "")
                                              or _KELVIN.match(x_unit or ""))
    y_temp = _has(y_text, TEMP_WORDS)
    x_time = _has(x_text, TIME_WORDS) or (x_unit or "").strip().lower() in (
        "s", "sec", "min", "h", "hr", "hrs", "ms", "days", "d")
    first: List[str] = []
    if x_temp and not y_temp:
        first.append("arrhenius")
    if x_time and y_temp:
        first.append("newton_cooling")
    if x_time:
        first += ["decay", "exponential", "damped"]
    if _has(x_text, SIZE_WORDS) and _has(y_text, STRENGTH_WORDS):
        first.append("hall_petch")
    if _has(x_text, DIST_WORDS):
        first.append("inverse_square")
    rest = ["line", "power", "quadratic", "exponential", "saturation", "logistic"]
    out: List[str] = []
    for k in first + rest:
        if k not in out:
            out.append(k)
    return out


@dataclass
class Ranked:
    key: str
    fit: Optional[Fit]
    error: str = ""


def try_all(keys: Sequence[str], x, y, **kw) -> List[Ranked]:
    """Each law in ``keys`` fitted to the same points, best (lowest AICc) first;
    the ones that failed last, with why."""
    out: List[Ranked] = []
    for key in keys:
        try:
            out.append(Ranked(key, fit_law(key, x, y, **kw)))
        except FitError as e:
            out.append(Ranked(key, None, str(e)))
    ok = sorted((r for r in out if r.fit), key=lambda r: r.fit.aicc)
    return ok + [r for r in out if not r.fit]


def ranking_rows(ranked: Sequence[Ranked]) -> List[Dict[str, object]]:
    best = next((r.fit.aicc for r in ranked if r.fit and np.isfinite(r.fit.aicc)), None)
    rows = []
    for r in ranked:
        law = LAWS[r.key]
        if r.fit:
            d = r.fit.aicc - best if best is not None and np.isfinite(r.fit.aicc) else float("nan")
            rows.append({"Equation": law.name, "Formula": law.text, "R²": r.fit.r2,
                         "RMSE": r.fit.rmse, "ΔAICc": d,
                         "Verdict": ("best" if d == 0 else "about as good" if d < 2 else
                                     "weaker" if d < 10 else "much weaker")})
        else:
            rows.append({"Equation": law.name, "Formula": law.text, "R²": "", "RMSE": "",
                         "ΔAICc": "", "Verdict": f"failed: {r.error}"})
    return rows


RANK_NOTE = ("Ranked by AICc: how well each equation fits, with a penalty for each extra "
             "parameter. A ΔAICc under 2 is a tie; then pick the one the physics of your "
             "process supports, not the one with the higher R².")


# ── chart ───────────────────────────────────────────────────────────────
def fit_figure(fit_: Fit):
    """Data, the fitted curve with its 95 % band, and the residuals beneath
    (a matplotlib Figure; no pyplot, so safe off the main thread)."""
    from matplotlib.figure import Figure
    fig = Figure(figsize=(7.5, 5.2), dpi=100)
    gs = fig.add_gridspec(3, 1, hspace=0.08)
    ax = fig.add_subplot(gs[:2, 0])
    rx = fig.add_subplot(gs[2, 0], sharex=ax)
    shown_x = _shown_x(fit_, fit_.x)
    lo, hi = float(np.min(fit_.x)), float(np.max(fit_.x))
    pad = 0.04 * (hi - lo)
    xs = np.linspace(lo - pad, hi + pad, 300)
    yhat, half, _ = _band(fit_, xs)
    sx = _shown_x(fit_, xs)
    ax.fill_between(sx, yhat - half, yhat + half, color="#c0392b", alpha=0.15, linewidth=0,
                    label="95% range of the curve")
    ax.plot(sx, yhat, color="#c0392b", linewidth=1.4, label=fit_.title())
    ax.scatter(shown_x, fit_.y, s=16, color="#3b6ea5", zorder=3, label="data")
    ax.legend(loc="best", fontsize=8, frameon=False)
    ax.set_ylabel(_axis(fit_.y_name, fit_.y_unit), fontsize=8)
    ax.set_title(f"{fit_.equation.text}    R² = {fit_.r2:.4f}", fontsize=9)
    ax.tick_params(labelsize=7, labelbottom=False)
    ax.grid(True, alpha=0.25)
    rx.axhline(0, color="#7f8c8d", linewidth=0.9)
    rx.scatter(shown_x, fit_.residuals, s=12, color="#3b6ea5")
    rx.set_ylabel("residual", fontsize=8)
    rx.set_xlabel(_axis(fit_.x_name, fit_.x_unit), fontsize=8)
    rx.tick_params(labelsize=7)
    rx.grid(True, alpha=0.25)
    fig.subplots_adjust(left=0.11, right=0.97, top=0.93, bottom=0.1)
    return fig


def _shown_x(fit_: Fit, xs: np.ndarray) -> np.ndarray:
    """x back in the field's own unit for the chart."""
    if fit_.x_shift == "C":
        return xs - 273.15
    if fit_.x_shift == "F":
        return (xs - 273.15) * 9.0 / 5.0 + 32.0
    return xs


def _axis(name: str, unit: str) -> str:
    return f"{name} ({unit})" if unit and f"({unit})" not in name else name


# ── saving ──────────────────────────────────────────────────────────────
def to_record(fit_: Fit, source: str = "", group: str = "") -> Dict[str, object]:
    """The fit as plain JSON: enough to use it again without the data."""
    return {
        "saved": _dt.datetime.now().isoformat(timespec="seconds"),
        "equation": fit_.equation.text, "law": fit_.law.key if fit_.law else "",
        "law_name": fit_.title(), "input": fit_.equation.inputs[0],
        "x_field": fit_.x_name, "x_unit": fit_.x_unit, "x_converted_from": fit_.x_shift,
        "y_field": fit_.y_name, "y_unit": fit_.y_unit,
        "parameters": [{"name": p.name, "value": p.value, "stderr": p.stderr,
                        "low95": p.low, "high95": p.high, "unit": p.unit}
                       for p in fit_.params],
        "constants": {c: CONSTANTS[c][0] for c in fit_.equation.constants},
        "fixed": dict(fit_.equation.fixed),
        "covariance": [[float(v) for v in row] for row in fit_.covariance],
        "n": fit_.n, "r2": fit_.r2, "rmse": fit_.rmse, "aicc": fit_.aicc,
        "x_range": [float(np.min(_shown_x(fit_, fit_.x))), float(np.max(_shown_x(fit_, fit_.x)))],
        "source": source, "group": group, "notes": list(fit_.notes),
    }


def _clean(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def save(fit_: Fit, path, source: str = "", group: str = "") -> Path:
    """Append the fit to the JSON list at ``path`` (made when missing)."""
    path = Path(path)
    items: List[dict] = []
    if path.exists():
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                items = []
        except (OSError, ValueError):
            items = []
    items.append(_clean(to_record(fit_, source, group)))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def models_path(csv) -> Path:
    """Where a Ledger's saved models live: beside its CSV."""
    p = Path(csv)
    return p.with_name(f"{p.stem}_models.json")
