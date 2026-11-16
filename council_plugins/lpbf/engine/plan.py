"""
From a specification to specimens, and the deterministic checks every plan
passes before an engineer can approve it.

RUNS (`runs`)
The design's coded points scaled to each variable's range, every replicate
of every point, the fixed values added, then put in run order (shuffled
with the spec's seed when it asks for that, so the same spec always gives
the same order). Each run gets a specimen ID: <experiment id>-S001, ...

DERIVED VALUES
With laser power P (W), scan speed v (mm/s), hatch h (mm) and layer t (mm):
    LED = P / v            J/mm
    VED = P / (v * h * t)  J/mm^3
Simple process descriptors, not a physical model; they are added to every
run when the four values are known.

CHECKS (`check`) — plain code, no model
A BLOCK stops approval: a parameter the profile has no limit for, a range or
a value outside the limits, low not below high, a design that cannot be
made, more specimens than the build takes, any run outside the limits.
A WARNING is shown and approval is still possible: a run's VED outside the
profile's window, a parameter varied with only two levels in a design that
needs curvature, a Latin hypercube with fewer runs than ten per factor.
"""
from __future__ import annotations

import csv
import io
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from . import doe
from . import limits as lm
from .spec import ExperimentSpec, design_matrix

BLOCK, WARN = "block", "warn"


@dataclass
class Issue:
    level: str          # BLOCK | WARN
    where: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"level": self.level, "where": self.where,
                "message": self.message}

    def line(self) -> str:
        tag = "BLOCKED" if self.level == BLOCK else "warning"
        return f"[{tag}] {self.where}: {self.message}"


def derived(row: Dict[str, float]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    p, v = row.get("laser_power"), row.get("scan_speed")
    h, t = row.get("hatch_spacing"), row.get("layer_thickness")
    if p is not None and v:
        out["LED_J_per_mm"] = round(p / v, 6)
        if h and t:
            out["VED_J_per_mm3"] = round(p / (v * h * t), 4)
    return out


def runs(spec: ExperimentSpec, experiment_id: str) -> List[Dict[str, Any]]:
    """Every specimen of the plan, in run order."""
    coded = design_matrix(spec)
    values = doe.scale(coded, [v.low for v in spec.variables],
                       [v.high for v in spec.variables]) if spec.variables \
        else np.zeros((len(coded), 0))
    rows: List[Dict[str, Any]] = []
    std = 0
    for rep in range(1, spec.replicates + 1):
        for i, point in enumerate(values):
            std += 1
            row: Dict[str, Any] = {"std_order": std, "point": i + 1,
                                   "replicate": rep}
            for var, x in zip(spec.variables, point):
                row[var.key] = round(float(x), 6)
            for f in spec.fixed:
                row[f.key] = f.value
            row.update(derived(row))
            rows.append(row)
    order = list(range(len(rows)))
    if spec.randomize:
        order = list(np.random.default_rng(spec.seed).permutation(len(rows)))
    out = []
    for n, idx in enumerate(order, start=1):
        row = dict(rows[int(idx)])
        row["run_order"] = n
        row["specimen_id"] = f"{experiment_id}-S{n:03d}"
        out.append(row)
    return out


def check(spec: ExperimentSpec, profile: Optional[Dict[str, Any]],
          plan_runs: Optional[Sequence[Dict[str, Any]]] = None) -> List[Issue]:
    """Every problem with the spec and its runs against the profile."""
    issues: List[Issue] = []
    if not profile:
        return [Issue(BLOCK, "profile", "No machine limits profile is "
                      "chosen; nothing can be checked without one.")]
    limits = profile.get("limits", {})
    if not spec.variables:
        issues.append(Issue(BLOCK, "plan", "Nothing is varied."))
    keys = [v.key for v in spec.variables] + [f.key for f in spec.fixed]
    for key in sorted({k for k in keys if keys.count(k) > 1}):
        issues.append(Issue(BLOCK, key, "is given more than once."))

    def rng(key: str):
        r = limits.get(key)
        return (float(r["min"]), float(r["max"])) if r else None

    for v in spec.variables:
        if v.key not in lm.PARAMETERS:
            issues.append(Issue(BLOCK, v.key, "is not a parameter this "
                                "plugin knows."))
            continue
        r = rng(v.key)
        if r is None:
            issues.append(Issue(BLOCK, v.key, "has no limit in the profile, "
                                "so it cannot be checked. Add one first."))
            continue
        if not v.low < v.high:
            issues.append(Issue(BLOCK, v.key, f"low {lm.fmt(v.low, v.key)} is "
                                f"not below high {lm.fmt(v.high, v.key)}."))
        for name, x in (("low", v.low), ("high", v.high)):
            if not (r[0] <= x <= r[1]):
                issues.append(Issue(BLOCK, v.key, f"{name} {lm.fmt(x, v.key)} "
                                    f"is outside the limit {r[0]:g} to "
                                    f"{r[1]:g} {lm.PARAMETERS[v.key].unit}."))
    for f in spec.fixed:
        if f.key not in lm.PARAMETERS:
            issues.append(Issue(BLOCK, f.key, "is not a parameter this plugin "
                                "knows."))
            continue
        r = rng(f.key)
        if r is None:
            issues.append(Issue(BLOCK, f.key, "has no limit in the profile, "
                                "so it cannot be checked. Add one first."))
        elif not (r[0] <= f.value <= r[1]):
            issues.append(Issue(BLOCK, f.key, f"fixed value "
                                f"{lm.fmt(f.value, f.key)} is outside the "
                                f"limit {r[0]:g} to {r[1]:g} "
                                f"{lm.PARAMETERS[f.key].unit}."))
    for key in ("laser_power", "scan_speed", "hatch_spacing",
                "layer_thickness"):
        if key not in keys:
            issues.append(Issue(WARN, key, "is neither varied nor fixed; "
                                "record the value used, or energy density "
                                "cannot be worked out."))
    try:
        n = spec.run_count() if spec.variables else 0
        design_matrix(spec) if spec.variables else None
    except doe.DesignError as exc:
        issues.append(Issue(BLOCK, "design", str(exc)))
        n = 0
    cap = int(profile.get("max_specimens", 0) or 0)
    if cap and n > cap:
        issues.append(Issue(BLOCK, "plan", f"{n} specimens is more than the "
                            f"{cap} one build takes in this profile."))
    if spec.design == "latin_hypercube" and spec.variables \
            and spec.runs < 10 * len(spec.variables):
        issues.append(Issue(WARN, "design", f"{spec.runs} runs for "
                            f"{len(spec.variables)} factors is thin for a "
                            f"model; about {10 * len(spec.variables)} is "
                            f"usual."))
    if spec.design == "fractional_factorial" and spec.center_points == 0:
        issues.append(Issue(WARN, "design", "a 2-level design cannot show "
                            "curvature; add centre points."))
    if plan_runs:
        window = profile.get("ved_window")
        outside = 0
        for row in plan_runs:
            sid = row.get("specimen_id", "?")
            for key in list(lm.PARAMETERS):
                if key in row and rng(key):
                    lo, hi = rng(key)
                    x = float(row[key])
                    if not (lo - 1e-9 <= x <= hi + 1e-9):
                        issues.append(Issue(BLOCK, sid, f"{key} "
                                            f"{lm.fmt(x, key)} is outside "
                                            f"{lo:g} to {hi:g}."))
            ved = row.get("VED_J_per_mm3")
            if window and ved is not None and not (window[0] <= ved <= window[1]):
                outside += 1
        if outside:
            issues.append(Issue(WARN, "VED", f"{outside} of {len(plan_runs)} "
                                f"runs fall outside the VED window "
                                f"{window[0]:g} to {window[1]:g} J/mm^3 "
                                f"(advice, not a limit)."))
    return issues


def blocked(issues: Sequence[Issue]) -> bool:
    return any(i.level == BLOCK for i in issues)


# ----------------------------------------------------------------------
# Manifest
# ----------------------------------------------------------------------
LEAD_COLUMNS = ["experiment_id", "specimen_id", "run_order", "std_order",
                "point", "replicate"]


def manifest_columns(plan_runs: Sequence[Dict[str, Any]]) -> List[str]:
    keys: List[str] = []
    for row in plan_runs:
        for k in row:
            if k not in keys and k not in LEAD_COLUMNS:
                keys.append(k)
    params = [k for k in lm.PARAMETERS if k in keys]
    rest = [k for k in keys if k not in params]
    return LEAD_COLUMNS + params + rest


def manifest_csv(experiment_id: str, plan_runs: Sequence[Dict[str, Any]],
                 header: Dict[str, str]) -> str:
    """The manifest as CSV text: '#' comment lines (experiment, profile,
    approval, units), then one row per specimen in run order."""
    buf = io.StringIO()
    for k, v in header.items():
        buf.write(f"# {k}: {v}\n")
    units = ", ".join(f"{p.key} [{p.unit}]" for p in lm.PARAMETERS.values())
    buf.write(f"# units: {units}, LED_J_per_mm [J/mm], VED_J_per_mm3 [J/mm^3]\n")
    cols = manifest_columns(plan_runs)
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for row in plan_runs:
        w.writerow({"experiment_id": experiment_id, **row})
    return buf.getvalue()


def write_text_atomic(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)
    return path


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------
ID_NAMES = ("specimen_id", "specimen", "specimen id", "sample id",
            "sample_id", "sample", "part id", "part_id", "id")


def read_table(path: Path):
    """A CSV or Excel file as a DataFrame ('#' lines in a CSV skipped)."""
    import pandas as pd
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path)
    return pd.read_csv(path, comment="#")


def id_column(columns: Sequence[str]) -> str:
    low = {str(c).strip().lower(): c for c in columns}
    for name in ID_NAMES:
        if name in low:
            return low[name]
    return ""


def join_results(plan_runs: Sequence[Dict[str, Any]], results) -> Dict[str, Any]:
    """The plan's runs joined with measured results on specimen ID.

    Returns {table (DataFrame), id_column, matched, unmatched_ids,
    missing_specimens, responses (the numeric measured columns)}."""
    import pandas as pd
    col = id_column(list(results.columns))
    if not col:
        raise ValueError("The results file has no specimen ID column "
                         "(a column called specimen_id, specimen or "
                         "sample id).")
    res = results.copy()
    res["specimen_id"] = res[col].astype(str).str.strip()
    if col != "specimen_id":
        res = res.drop(columns=[col])
    plan = pd.DataFrame(list(plan_runs))
    plan["specimen_id"] = plan["specimen_id"].astype(str)
    clash = [c for c in res.columns if c in plan.columns and c != "specimen_id"]
    res = res.rename(columns={c: f"{c} (measured)" for c in clash})
    table = plan.merge(res, on="specimen_id", how="left", indicator=True)
    matched = int((table["_merge"] == "both").sum())
    table = table.drop(columns=["_merge"])
    known = set(plan["specimen_id"])
    unmatched = sorted(set(res["specimen_id"]) - known)
    missing = sorted(known - set(res["specimen_id"]))
    responses = [c for c in res.columns if c != "specimen_id"
                 and pd.api.types.is_numeric_dtype(res[c])]
    return {"table": table, "id_column": col, "matched": matched,
            "unmatched_ids": unmatched, "missing_specimens": missing,
            "responses": responses}


def summary(table, columns: Sequence[str]):
    """count, mean, std, min, median, max per column."""
    import pandas as pd
    rows = []
    for c in columns:
        s = pd.to_numeric(table[c], errors="coerce").dropna()
        if s.empty:
            continue
        rows.append({"column": c, "count": int(s.count()),
                     "mean": float(s.mean()),
                     "std": float(s.std()) if s.count() > 1 else math.nan,
                     "min": float(s.min()), "median": float(s.median()),
                     "max": float(s.max())})
    return pd.DataFrame(rows)


def correlations(table, inputs: Sequence[str], responses: Sequence[str]):
    """Pearson r of each varied input with each response (rows: inputs).
    An input that is constant in the data gets no r: its effect cannot be
    learned from this data."""
    import pandas as pd
    out = pd.DataFrame(index=list(inputs), columns=list(responses), dtype=float)
    for i in inputs:
        x = pd.to_numeric(table[i], errors="coerce")
        for r in responses:
            y = pd.to_numeric(table[r], errors="coerce")
            ok = x.notna() & y.notna()
            if ok.sum() >= 3 and x[ok].nunique() > 1 and y[ok].nunique() > 1:
                out.loc[i, r] = float(np.corrcoef(x[ok], y[ok])[0, 1])
    return out
