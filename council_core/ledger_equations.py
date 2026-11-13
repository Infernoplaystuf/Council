"""
council_core.ledger_equations — equations fitted to a Ledger's numbers
(Andy, 2026-11-13: he liked the equation deriving of his LMM Analysis Studio
script; the Analyze window's Equations page, built last).

Pick a result field and the input fields; the equation's coefficients come
out in the fields' own units, and how good it is is measured on parts the
fit did not see: the parts are split into up to 5 folds, each fold's parts
predicted from an equation fitted to the others (a part's flaws are never
split between the two, as they are not independent).

    KINDS                       linear, ridge, quadratic, power law
    fit(data, target, inputs, kind, group, per)  -> Equation
    compare(data, target, inputs, ...) -> each kind fitted, best first
    suggest_inputs(data, target, n, ...) -> the n fields most related to it
                                   (to tick as inputs; the fit marks them suggested)
    predict(eq, values)         -> the result, and the inputs outside the data's range
    sweep(eq, x, low, high, fixed) / surface(eq, x, y, ...) -> values for graphs
    save(eq, csv) / saved(csv)  -> '<csv stem>.equations.json' beside the Ledger

An equation is plain numbers (terms, coefficients, ranges) saved as JSON:
nothing is ever executed when one is loaded back. numpy only.

What it is not: a physical law. The "within" value is the 90th percentile
of the errors on unseen parts, not a calibrated interval; picking the best
of several kinds after comparing them flatters the winner a little, and so
does fitting inputs that were suggested by looking at all the data (the
description says so when they were).

Nothing here imports Qt.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from council_core import ledger_analytics as la
from sample_ledger import LedgerError

LINEAR, RIDGE, QUADRATIC, POWER = "Linear", "Ridge", "Quadratic", "Power law"
KINDS = [LINEAR, RIDGE, QUADRATIC, POWER]
KIND_HELP = {
    LINEAR: "result = a + b·x + c·y …",
    RIDGE: "linear, its coefficients shrunk a little (steadier when inputs move together)",
    QUADRATIC: "linear + squares + each pair multiplied (x·y): curves and interactions; "
               "4 varying inputs at most",
    POWER: "result = a · x^b · y^c … (ratios like Power / Speed); every value above 0",
}
RIDGE_ALPHA = 1.0
MAX_QUADRATIC = 4
MIN_ROWS = 10
MIN_PARTS = 3
FOLDS = 5
SCHEMA_VERSION = 1
WARNING = ("An equation fitted to these data, not an established physical law. "
           "'Within' is the 90th percentile of the errors on parts the fit did not see, "
           "not a calibrated prediction interval. Picking the best of several kinds "
           "flatters the winner's score a little.")


@dataclass
class Equation:
    kind: str
    target: str
    inputs: List[str]                    # the inputs that vary, in the equation
    terms: List[str]
    coefficients: List[float]
    intercept: float
    ranges: Dict[str, Dict[str, float]]  # input -> min / max / median of the data
    scores: Dict[str, float]             # cv_r2, cv_rmse, cv_mae, within_p90, fit_r2
    rows: int
    parts: int
    folds: int
    per: str = la.PER_ROW
    group: str = ""
    fixed: Dict[str, float] = field(default_factory=dict)   # inputs that never varied
    asked: List[str] = field(default_factory=list)          # the inputs asked for
    suggested: bool = False
    units: Dict[str, str] = field(default_factory=dict)
    created: str = ""
    schema_version: int = SCHEMA_VERSION
    warning: str = WARNING
    # the last fit's unseen-part predictions (not saved)
    actual: Optional[np.ndarray] = field(default=None, repr=False, compare=False)
    predicted: Optional[np.ndarray] = field(default=None, repr=False, compare=False)

    def text(self, digits: int = 6) -> str:
        """'Y = 1.2 + 0.3 × (X) …', or for a power law 'Y = 1.2 × X^0.98 …'."""
        g = f"{{:.{digits}g}}"
        if self.kind == POWER:
            out = f"{self.target} = {g.format(math.exp(self.intercept))}"
            for t, c in zip(self.terms, self.coefficients):
                out += f" × ({t})^{g.format(c)}"
            return out
        out = f"{self.target} = {g.format(self.intercept)}"
        for t, c in zip(self.terms, self.coefficients):
            out += f" {'+' if c >= 0 else '−'} {g.format(abs(c))} × ({t})"
        return out

    def to_json(self) -> Dict:
        d = asdict(self)
        d.pop("actual", None)
        d.pop("predicted", None)
        return d

    @classmethod
    def from_json(cls, d: Dict) -> "Equation":
        if d.get("schema_version") != SCHEMA_VERSION:
            raise LedgerError("this saved equation is from another version: fit it again")
        keep = {k: d[k] for k in cls.__dataclass_fields__ if k in d
                and k not in ("actual", "predicted")}
        eq = cls(**keep)
        if eq.kind not in KINDS or len(eq.terms) != len(eq.coefficients):
            raise LedgerError("the saved equation is not one this window can use")
        eq.coefficients = [float(c) for c in eq.coefficients]
        eq.intercept = float(eq.intercept)
        return eq


# ── the data ────────────────────────────────────────────────────────────
def number_fields(data: la.Data, group: str = "") -> List[str]:
    """The fields an equation can use: numbers, not the analysis's counts."""
    return [f for f in la.fields_of(data, group)
            if la._kind_in(data, f, group) == la.NUMBER and not data.fields[f].count]


def table(data: la.Data, fields: Sequence[str], group: str = "",
          per: str = la.PER_ROW) -> Tuple[pd.DataFrame, np.ndarray]:
    """``fields`` as numbers, a row per flaw (when any of them is per flaw
    and ``per`` says so) or per part, rows with all of them only; and each
    row's part."""
    flat = la.rows(data, group)
    per_flaw = per == la.PER_ROW and any(data.fields[f].per_item for f in fields)
    df = flat if per_flaw else la.per_part(data, flat)
    num = pd.DataFrame({f: [la.to_number(v) for v in df[f]] for f in fields}, dtype=float)
    num = num.replace([np.inf, -np.inf], np.nan)
    ok = num.notna().all(axis=1).to_numpy()
    parts = df[data.id_field].astype(str).str.lower().to_numpy()
    return num[ok].reset_index(drop=True), parts[ok]


# ── the maths ───────────────────────────────────────────────────────────
def _terms(kind: str, names: Sequence[str]) -> List[str]:
    if kind != QUADRATIC:
        return list(names)
    out = list(names)
    for i, a in enumerate(names):
        for b in names[i:]:
            out.append(f"{a}^2" if a == b else f"{a} · {b}")
    return out


def _design(kind: str, X: np.ndarray) -> np.ndarray:
    if kind == POWER:
        return np.log(X)
    if kind != QUADRATIC:
        return X
    cols = [X]
    k = X.shape[1]
    for i in range(k):
        for j in range(i, k):
            cols.append((X[:, i] * X[:, j])[:, None])
    return np.hstack(cols)


def _solve(kind: str, X: np.ndarray, y: np.ndarray) -> Tuple[float, np.ndarray]:
    """(intercept, coefficients) in the inputs' own units (log units for a
    power law: y fitted as log y)."""
    A = _design(kind, X)
    t = np.log(y) if kind == POWER else y
    if kind == RIDGE:
        mu, sd = A.mean(axis=0), A.std(axis=0)
        sd[sd == 0] = 1.0
        Z = (A - mu) / sd
        tc = t - t.mean()
        beta = np.linalg.solve(Z.T @ Z + RIDGE_ALPHA * np.eye(Z.shape[1]), Z.T @ tc)
        coef = beta / sd
        return float(t.mean() - coef @ mu), coef
    M = np.hstack([np.ones((len(A), 1)), A])
    sol, *_ = np.linalg.lstsq(M, t, rcond=None)
    return float(sol[0]), sol[1:]


def _apply(kind: str, intercept: float, coef: np.ndarray, X: np.ndarray) -> np.ndarray:
    out = intercept + _design(kind, X) @ coef
    return np.exp(out) if kind == POWER else out


def _folds(parts: np.ndarray, n: int) -> List[np.ndarray]:
    """Parts dealt into ``n`` folds (largest first, to the smallest fold),
    so each fold's rows are whole parts."""
    uniq, counts = np.unique(parts, return_counts=True)
    order = sorted(zip(uniq, counts), key=lambda t: (-t[1], str(t[0])))
    sizes = [0] * n
    of: Dict[str, int] = {}
    for p, c in order:
        k = int(np.argmin(sizes))
        of[p] = k
        sizes[k] += c
    fold = np.array([of[p] for p in parts])
    return [np.where(fold == k)[0] for k in range(n)]


def _candidates(data: la.Data, target: str, group: str) -> List[str]:
    """Inputs a suggestion may pick: not the target, not an inspector's
    reading of it, not a field stacked from the same table run."""
    base = la.base_field(target)
    return [f for f in number_fields(data, group)
            if f != target and la.base_field(f) != base]


def suggest_inputs(data: la.Data, target: str, n: int = 3, group: str = "",
                   per: str = la.PER_ROW) -> List[str]:
    """The ``n`` fields most correlated with ``target`` (either way), each
    with at least MIN_ROWS values beside it."""
    scored = {}
    for f in _candidates(data, target, group):
        num, _p = table(data, [target, f], group, per)
        if len(num) >= MIN_ROWS and num[f].nunique() > 1 and num[target].nunique() > 1:
            r = num[f].corr(num[target])
            if pd.notna(r):
                scored[f] = abs(float(r))
    return sorted(scored, key=lambda f: -scored[f])[:n]


def fit(data: la.Data, target: str, inputs: Sequence[str] = (), kind: str = LINEAR,
        group: str = "", per: str = la.PER_ROW, suggested: bool = False) -> Equation:
    """``target`` from ``inputs``. ``suggested``: the inputs came from
    suggest_inputs (said in the description: it flatters the score)."""
    if kind not in KINDS:
        raise LedgerError(f"unknown equation kind: {kind}")
    if target not in data.fields:
        raise LedgerError(f"no field {target}")
    asked = list(dict.fromkeys(inputs))
    if not asked or target in asked:
        raise LedgerError("pick one or more inputs, not the result itself")
    bad = [f for f in asked if f not in data.fields or data.fields[f].kind != la.NUMBER]
    if bad:
        raise LedgerError(f"not number fields: {', '.join(bad)}")
    num, parts = table(data, [target] + asked, group, per)
    n_parts = len(set(parts))
    if len(num) < MIN_ROWS:
        raise LedgerError(f"only {len(num)} row(s) have {target} and every input filled; "
                          f"{MIN_ROWS} are needed")
    if n_parts < MIN_PARTS:
        raise LedgerError(f"those rows come from {n_parts} part(s); {MIN_PARTS} are needed "
                          "to test the equation on parts it did not see")
    fixed, active = {}, []
    for f in asked:
        v = num[f].to_numpy()
        if np.ptp(v) <= 1e-12 * max(1.0, float(np.max(np.abs(v)))):
            fixed[f] = float(np.median(v))
        else:
            active.append(f)
    if not active:
        raise LedgerError("none of the inputs varies in these rows: nothing to fit")
    y = num[target].to_numpy(float)
    if np.ptp(y) <= 1e-12 * max(1.0, float(np.max(np.abs(y)))):
        raise LedgerError(f"{target} never varies in these rows: nothing to predict")
    if kind == QUADRATIC and len(active) > MAX_QUADRATIC:
        raise LedgerError(f"a quadratic takes {MAX_QUADRATIC} varying inputs at most "
                          f"({len(active)} picked)")
    if kind == POWER and ((num[active] <= 0).any().any() or (y <= 0).any()):
        raise LedgerError("a power law needs every value above 0 (it is fitted on logs)")
    n_terms = len(_terms(kind, active))
    if len(num) < max(MIN_ROWS, 3 * n_terms):
        raise LedgerError(f"{len(num)} rows for {n_terms} terms: at least {3 * n_terms} "
                          "are needed")
    X = num[active].to_numpy(float)
    n_folds = min(FOLDS, n_parts)
    pred = np.full(len(y), np.nan)
    for test in _folds(parts, n_folds):
        train = np.setdiff1d(np.arange(len(y)), test)
        b0, b = _solve(kind, X[train], y[train])
        pred[test] = _apply(kind, b0, b, X[test])
    if not np.isfinite(pred).all():
        raise LedgerError("the equation gave no number for some unseen parts: try "
                          "another kind or fewer inputs")
    b0, b = _solve(kind, X, y)
    if not (np.isfinite(b).all() and math.isfinite(b0)):
        raise LedgerError("the fit did not settle (inputs that move together exactly?): "
                          "drop one, or try Ridge")
    fitted = _apply(kind, b0, b, X)
    err = y - pred
    ss = float(((y - y.mean()) ** 2).sum())
    scores = {"cv_r2": 1 - float((err ** 2).sum()) / ss,
              "cv_rmse": float(np.sqrt((err ** 2).mean())),
              "cv_mae": float(np.abs(err).mean()),
              "within_p90": float(np.quantile(np.abs(err), 0.9)),
              "fit_r2": 1 - float(((y - fitted) ** 2).sum()) / ss}
    ranges = {f: {"min": float(X[:, i].min()), "max": float(X[:, i].max()),
                  "median": float(np.median(X[:, i]))} for i, f in enumerate(active)}
    eq = Equation(kind=kind, target=target, inputs=active, terms=_terms(kind, active),
                  coefficients=[float(c) for c in b], intercept=b0, ranges=ranges,
                  scores=scores, rows=len(y), parts=n_parts, folds=n_folds, per=per,
                  group=group, fixed=fixed, asked=list(asked), suggested=bool(suggested),
                  units={f: data.fields[f].unit for f in [target] + asked},
                  created=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    eq.actual, eq.predicted = y, pred
    return eq


def compare(data: la.Data, target: str, inputs: Sequence[str] = (), group: str = "",
            per: str = la.PER_ROW, suggested: bool = False) -> pd.DataFrame:
    """Each kind fitted to the same rows, best (smallest error on unseen
    parts) first; a kind that cannot be fitted says why."""
    out = []
    for k in KINDS:
        try:
            e = fit(data, target, inputs, k, group, per, suggested)
            out.append({"Kind": k, "R² on unseen parts": round(e.scores["cv_r2"], 4),
                        "Typical error (RMSE)": e.scores["cv_rmse"],
                        "Within (90%)": e.scores["within_p90"],
                        "R² on its own data": round(e.scores["fit_r2"], 4),
                        "Rows": e.rows, "Parts": e.parts, "Why not": ""})
        except LedgerError as err:
            out.append({"Kind": k, "Why not": str(err)})
    cols = ["Kind", "R² on unseen parts", "Typical error (RMSE)", "Within (90%)",
            "R² on its own data", "Rows", "Parts", "Why not"]
    res = pd.DataFrame(out, columns=cols)
    res["_k"] = res["Typical error (RMSE)"].fillna(np.inf)
    return res.sort_values("_k", kind="stable").drop(columns="_k").reset_index(drop=True)


# ── using an equation ───────────────────────────────────────────────────
def predict(eq: Equation, values: Dict[str, float]) -> Tuple[float, List[str]]:
    """The result for ``values`` (an input left out takes its median), and
    each input outside the range the equation was fitted on."""
    x = np.array([[float(values.get(f, eq.ranges[f]["median"])) for f in eq.inputs]])
    if not np.isfinite(x).all():
        raise LedgerError("every input needs a number")
    if eq.kind == POWER and (x <= 0).any():
        raise LedgerError("a power law takes inputs above 0 only")
    y = float(_apply(eq.kind, eq.intercept, np.asarray(eq.coefficients), x)[0])
    outside = []
    for f, v in zip(eq.inputs, x[0]):
        lo, hi = eq.ranges[f]["min"], eq.ranges[f]["max"]
        if not lo - 1e-9 <= v <= hi + 1e-9:
            outside.append(f"{f} {v:g} is outside {lo:g} to {hi:g}")
    return y, outside


def _base(eq: Equation, fixed: Optional[Dict[str, float]]) -> Dict[str, float]:
    return {f: float((fixed or {}).get(f, eq.ranges[f]["median"])) for f in eq.inputs}


def sweep(eq: Equation, x: str, low: float, high: float,
          fixed: Optional[Dict[str, float]] = None, n: int = 201) -> pd.DataFrame:
    """The result as ``x`` goes from ``low`` to ``high``, the other inputs
    held (at ``fixed``, else their medians)."""
    if low >= high:
        raise LedgerError("the low end must be below the high end")
    base = _base(eq, fixed)
    xs = np.linspace(low, high, n)
    X = np.array([[v if f == x else base[f] for f in eq.inputs] for v in xs])
    ys = _apply(eq.kind, eq.intercept, np.asarray(eq.coefficients), X)
    lo, hi = eq.ranges[x]["min"], eq.ranges[x]["max"]
    return pd.DataFrame({x: xs, f"Predicted {eq.target}": ys,
                         "Outside the data": (xs < lo - 1e-9) | (xs > hi + 1e-9)})


def surface(eq: Equation, x: str, y: str, xr: Tuple[float, float], yr: Tuple[float, float],
            fixed: Optional[Dict[str, float]] = None, n: int = 75):
    """(X grid, Y grid, predicted) over ``xr`` × ``yr``, the rest held."""
    if x == y:
        raise LedgerError("pick two different inputs")
    if xr[0] >= xr[1] or yr[0] >= yr[1]:
        raise LedgerError("each low end must be below its high end")
    base = _base(eq, fixed)
    gx, gy = np.meshgrid(np.linspace(*xr, n), np.linspace(*yr, n))
    cols = [gx.ravel() if f == x else gy.ravel() if f == y else
            np.full(gx.size, base[f]) for f in eq.inputs]
    z = _apply(eq.kind, eq.intercept, np.asarray(eq.coefficients), np.column_stack(cols))
    return gx, gy, z.reshape(gx.shape)


def outside(eq: Equation, name: str, low: float, high: float) -> bool:
    r = eq.ranges[name]
    return low < r["min"] - 1e-9 or high > r["max"] + 1e-9


# ── saved beside the Ledger ─────────────────────────────────────────────
def saved_path(csv) -> Path:
    csv = Path(csv)
    return csv.with_name(f"{csv.stem}.equations.json")


def saved(csv) -> List[Equation]:
    """The equations saved for this Ledger, oldest first (any the window
    cannot use are skipped)."""
    p = saved_path(csv)
    if not p.is_file():
        return []
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for d in items if isinstance(items, list) else []:
        try:
            out.append(Equation.from_json(d))
        except (LedgerError, TypeError, KeyError, ValueError):
            continue
    return out


def save(eq: Equation, csv) -> Path:
    """Appended to the Ledger's saved equations (JSON, written whole)."""
    p = saved_path(csv)
    items = [e.to_json() for e in saved(csv)] + [eq.to_json()]
    tmp = p.with_name(p.name + ".writing")
    tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
    import os
    os.replace(tmp, p)
    return p


# ── figures ─────────────────────────────────────────────────────────────
def _note_outside(ax, show: bool) -> None:
    if show:
        ax.text(0.01, 0.01, "Outside the data: the equation is guessing here",
                transform=ax.transAxes, fontsize=8, color="#a04000",
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "#a04000"})


def actual_figure(eq: Equation):
    """Each row's result against what the equation said for it without
    seeing its part."""
    from matplotlib.figure import Figure
    fig = Figure(figsize=(6, 4.2), dpi=100)
    ax = fig.add_subplot(111)
    if eq.actual is None:
        ax.text(0.5, 0.5, "Fit the equation again to see this (unseen-part predictions "
                          "are not saved).", ha="center", va="center", wrap=True, fontsize=9)
        ax.axis("off")
        return fig
    a, p = eq.actual, eq.predicted
    ax.scatter(a, p, s=14, alpha=0.7, color="#3b6ea5")
    lo, hi = float(min(a.min(), p.min())), float(max(a.max(), p.max()))
    ax.plot([lo, hi], [lo, hi], "--", color="black", linewidth=1, label="exact")
    ax.set_xlabel(f"{la.label(eq.target, eq.units.get(eq.target, ''), 50)}: measured",
                  fontsize=8)
    ax.set_ylabel("predicted for a part the fit did not see", fontsize=8)
    ax.set_title(f"{eq.kind}: R² {eq.scores['cv_r2']:.3f} on unseen parts", fontsize=9)
    ax.legend(fontsize=7)
    ax.grid(True, alpha=0.25)
    ax.tick_params(labelsize=7)
    fig.tight_layout()
    return fig


def response_figure(eq: Equation, data: la.Data, x: str, low: float, high: float,
                    fixed: Optional[Dict[str, float]] = None, measured: bool = True):
    from matplotlib.figure import Figure
    df = sweep(eq, x, low, high, fixed)
    fig = Figure(figsize=(6, 4.2), dpi=100)
    ax = fig.add_subplot(111)
    ycol = f"Predicted {eq.target}"
    ax.plot(df[x], df[ycol], color="#c0392b", linewidth=2, label="equation")
    if measured:
        num, _p = table(data, [x, eq.target], eq.group, eq.per)
        if len(num):
            ax.scatter(num[x], num[eq.target], s=10, alpha=0.3, color="#3b6ea5",
                       label="measured (other inputs vary)")
    ax.set_xlabel(la.label(x, eq.units.get(x, ""), 50), fontsize=8)
    ax.set_ylabel(la.label(eq.target, eq.units.get(eq.target, ""), 50), fontsize=8)
    held = [f for f in eq.inputs if f != x]
    ax.set_title(f"{eq.kind}" + (", others held" if held else ""), fontsize=9)
    ax.legend(fontsize=7)
    ax.grid(True, alpha=0.25)
    ax.tick_params(labelsize=7)
    _note_outside(ax, outside(eq, x, low, high))
    fig.tight_layout()
    return fig, df


def surface_figure(eq: Equation, data: la.Data, x: str, y: str, xr, yr,
                   fixed: Optional[Dict[str, float]] = None, contour: bool = False,
                   measured: bool = True):
    from matplotlib.figure import Figure
    gx, gy, z = surface(eq, x, y, xr, yr, fixed)
    fig = Figure(figsize=(6.2, 4.4), dpi=100)
    ax = fig.add_subplot(111)
    if contour:
        filled = ax.contourf(gx, gy, z, levels=16, cmap="viridis")
        lines = ax.contour(gx, gy, z, levels=8, colors="k", linewidths=0.6, alpha=0.5)
        ax.clabel(lines, inline=True, fontsize=7)
        fig.colorbar(filled, ax=ax, label=f"predicted {la.label(eq.target, '', 30)}")
    else:
        mesh = ax.pcolormesh(gx, gy, z, shading="auto", cmap="viridis")
        fig.colorbar(mesh, ax=ax, label=f"predicted {la.label(eq.target, '', 30)}")
    if measured:
        num, _p = table(data, [x, y], eq.group, eq.per)
        if len(num):
            ax.scatter(num[x], num[y], s=8, color="white", edgecolor="black",
                       linewidth=0.4, alpha=0.8, label="inputs measured")
            ax.legend(fontsize=7, loc="upper right")
    ax.set_xlabel(la.label(x, eq.units.get(x, ""), 40), fontsize=8)
    ax.set_ylabel(la.label(y, eq.units.get(y, ""), 40), fontsize=8)
    ax.tick_params(labelsize=7)
    _note_outside(ax, outside(eq, x, *xr) or outside(eq, y, *yr))
    fig.tight_layout()
    grid = pd.DataFrame({x: gx.ravel(), y: gy.ravel(), f"Predicted {eq.target}": z.ravel()})
    return fig, grid


def describe(eq: Equation) -> str:
    """The equation and how good it is, in words."""
    s = eq.scores
    unit = eq.units.get(eq.target, "")
    u = f" {unit}" if unit else ""
    lines = [eq.text(),
             "",
             f"On parts it did not see ({eq.folds} folds of {eq.parts} parts, {eq.rows} rows): "
             f"R² {s['cv_r2']:.3f}, typical error {s['cv_rmse']:.4g}{u}, "
             f"9 in 10 within ±{s['within_p90']:.4g}{u}.",
             f"On its own data: R² {s['fit_r2']:.3f}"
             + (" (much better than on unseen parts: it may be fitting noise)"
                if s["fit_r2"] - s["cv_r2"] > 0.15 else "") + "."]
    if eq.fixed:
        lines.append("Held the same in every row, so not in the equation: "
                     + ", ".join(f"{f} = {v:g}" for f, v in eq.fixed.items()) + ".")
    if eq.suggested:
        lines.append("The inputs were suggested by looking at all the data, which flatters "
                     "the score a little; check it on parts measured later.")
    lines += ["", eq.warning]
    return "\n".join(lines)
