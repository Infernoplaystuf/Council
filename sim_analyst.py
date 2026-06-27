"""
sim_analyst.py — deterministic compute layer over recorded sim runs.

The Sim Analyst specialist (sim_analyst id in specialists.py) is the
voice that answers natural-language questions about simulation
data. Its system prompt makes one ABSOLUTE PROMISE: it never invents
specific metric values, run counts, or event timestamps. The
council enforces this by *injecting computed results* into the
prompt before the analyst gets a chance to guess — same defense
pattern used by ``vault_analyst.py`` (CSVs) and
``steam_analyst.py`` (Steam cache).

Public API
----------
``SimAnalystResult`` — answer payload returned by every computation.
Carries the human-readable text plus computed_values, sources
(run ids), and a ``to_injection_block()`` that formats the block
the GUI pastes into the protected Council slot.

``answer_question(query, vault_dir, *, sim_name="")`` — top-level
router: pattern-matches the user's free-text query, picks the right
compute helper, returns a SimAnalystResult.

Underlying helpers (also usable directly from tests / future UI):

  summarise_runs(runs)           — last-N quick overview
  group_by(runs, key)            — per-{persona, param} buckets
  median_iqr(values)             — (p25, median, p75) + N
  best_per_persona(runs, metric) — top run per persona name
  correlate(runs, param, metric) — Pearson-ish over float pairs
  failures(runs)                 — failed runs with errors

What this module does NOT do
----------------------------
- Run sims itself (that's ``sim_runner``)
- Persist anything (that's ``sim_recorder``)
- Talk to the council directly (that's ``council_gui_engine``)

The Council pipeline calls ``answer_question`` BEFORE deliberation,
formats ``to_injection_block()``, attaches it to the protected
injection slot, then the model deliberates — but every concrete
number the model can cite comes from values computed here.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ============================================================
# Result type
# ============================================================

@dataclass
class SimAnalystResult:
    """One analyst answer."""
    answer:          str = ""
    confidence:      str = "medium"      # "high" | "medium" | "low"
    computed_values: Dict[str, Any] = field(default_factory=dict)
    sources:         List[str] = field(default_factory=list)  # run ids
    error:           str = ""

    def to_injection_block(self) -> str:
        """Format as a ``[SIM ANALYST RESULT]`` block for the
        protected injection slot. Mirrors ``steam_analyst``."""
        lines = [
            "[SIM ANALYST RESULT — computed from vault/simulations/, "
            f"confidence={self.confidence}]",
        ]
        if self.error:
            lines.append(f"  error: {self.error}")
            return "\n".join(lines)
        if self.answer:
            for ln in self.answer.splitlines():
                lines.append(f"  {ln}")
        if self.computed_values:
            lines.append("  computed:")
            for k, v in self.computed_values.items():
                lines.append(f"    {k}: {v}")
        if self.sources:
            shown = self.sources[:8]
            extra = len(self.sources) - len(shown)
            lines.append("  sources (run ids):")
            for r in shown:
                lines.append(f"    - {r}")
            if extra > 0:
                lines.append(f"    … plus {extra} more.")
        return "\n".join(lines)


# ============================================================
# Loader — pulls runs from SimRecorder
# ============================================================

def _load_index(vault_dir: Any,
                  *, sim_name: str = "",
                  limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return index entries from SimRecorder, newest first.

    Doesn't load full run files — the index already carries the
    fields the analyst needs (params + metrics summary + ok flag).
    For per-event analysis the caller can load_run() on demand.
    """
    try:
        from sim_recorder import SimRecorder
    except Exception as exc:
        print(f"[sim_analyst] could not import sim_recorder: {exc!r}")
        return []
    try:
        rec = SimRecorder(vault_dir)
    except Exception as exc:
        print(f"[sim_analyst] could not open recorder: {exc!r}")
        return []
    return rec.list_runs(sim_name=sim_name, limit=limit)


# ============================================================
# Pure compute helpers (no I/O, pure functions, unit-testable)
# ============================================================

def median_iqr(values: Iterable[float]) -> Optional[Dict[str, float]]:
    """Compute (n, min, p25, median, p75, max) over a numeric iterable.

    Returns None on empty input; numeric-only — non-numeric values
    are dropped silently so an upstream "score" field that's a string
    in one run doesn't break the whole aggregation.
    """
    nums = [float(v) for v in values
            if isinstance(v, (int, float))]
    if not nums:
        return None
    nums.sort()
    n = len(nums)
    return {
        "n":      n,
        "min":    nums[0],
        "p25":    nums[n // 4],
        "median": statistics.median(nums),
        "p75":    nums[3 * n // 4],
        "max":    nums[-1],
    }


def group_by(runs: List[Dict[str, Any]], key: str) -> Dict[Any, List[Dict[str, Any]]]:
    """Bucket runs by a per-run key — looks in params first, then in
    top-level entry fields. Used by per-persona / per-param analyses.
    """
    out: Dict[Any, List[Dict[str, Any]]] = {}
    for r in runs:
        params = r.get("params") or {}
        if key in params:
            bucket_key = params[key]
        elif key in r:
            bucket_key = r[key]
        else:
            continue
        out.setdefault(bucket_key, []).append(r)
    return out


def best_per_persona(
    runs: List[Dict[str, Any]],
    metric: str,
    *,
    higher_is_better: bool = True,
) -> List[Tuple[str, Dict[str, Any]]]:
    """Top run by ``metric`` within each persona_name bucket.

    Returns ``[(persona_name, run_entry), ...]`` sorted by metric
    descending (or ascending when ``higher_is_better=False``).
    Personas with no numeric metric value are skipped.
    """
    groups = group_by(runs, "persona_name")
    out: List[Tuple[str, Dict[str, Any]]] = []
    for persona, rs in groups.items():
        # Pick the best run in this bucket
        best: Optional[Dict[str, Any]] = None
        best_val: Optional[float] = None
        for r in rs:
            v = (r.get("metrics") or {}).get(metric)
            if not isinstance(v, (int, float)):
                continue
            if best_val is None or (
                (v > best_val) if higher_is_better else (v < best_val)
            ):
                best_val = v
                best = r
        if best is not None:
            out.append((str(persona), best))
    out.sort(
        key=lambda t: (t[1].get("metrics") or {}).get(metric, 0),
        reverse=higher_is_better,
    )
    return out


def pearson(xs: Iterable[float], ys: Iterable[float]) -> Optional[Dict[str, float]]:
    """Pearson r over two equal-length numeric sequences.

    Returns ``{"n", "r"}`` or None when fewer than 3 valid numeric
    pairs remain or either side has zero variance. Pure stdlib — no
    scipy. Shared by the sim analyst (correlate) and the Steam Market
    analyst so neither hand-rolls the formula.
    """
    pairs: List[Tuple[float, float]] = [
        (float(x), float(y)) for x, y in zip(xs, ys)
        if isinstance(x, (int, float)) and isinstance(y, (int, float))
    ]
    if len(pairs) < 3:
        return None
    xs2 = [p for p, _ in pairs]
    ys2 = [q for _, q in pairs]
    n = len(pairs)
    mx = sum(xs2) / n
    my = sum(ys2) / n
    num = sum((x - mx) * (y - my) for x, y in pairs)
    denx = math.sqrt(sum((x - mx) ** 2 for x in xs2))
    deny = math.sqrt(sum((y - my) ** 2 for y in ys2))
    if denx == 0 or deny == 0:
        return None
    return {"n": n, "r": round(num / (denx * deny), 4)}


def correlate(
    runs: List[Dict[str, Any]],
    param: str,
    metric: str,
) -> Optional[Dict[str, float]]:
    """Pearson correlation between a numeric param and metric.

    Returns ``{"n", "r", "param_range", "metric_range"}`` or None when
    fewer than 3 numeric pairs are available. Math lives in ``pearson``.
    """
    xs: List[float] = []
    ys: List[float] = []
    for r in runs:
        p = (r.get("params") or {}).get(param)
        m = (r.get("metrics") or {}).get(metric)
        if isinstance(p, (int, float)) and isinstance(m, (int, float)):
            xs.append(float(p))
            ys.append(float(m))
    res = pearson(xs, ys)
    if res is None:
        return None
    return {
        **res,
        "param_range":  f"{min(xs)}..{max(xs)}",
        "metric_range": f"{min(ys):.3f}..{max(ys):.3f}",
    }


def failures(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return only the runs that didn't succeed (ok flag is False)."""
    return [r for r in runs if not r.get("ok")]


def summarise_runs(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Quick overview of a sweep: count, success rate, time span,
    distinct sim_names, personas seen, metric names seen.
    """
    if not runs:
        return {"n": 0}
    ok_n = sum(1 for r in runs if r.get("ok"))
    started = [r.get("started_at", "") for r in runs if r.get("started_at")]
    metric_names: set = set()
    persona_names: set = set()
    sim_names: set = set()
    for r in runs:
        sim_names.add(r.get("sim_name", "?"))
        for k in (r.get("metrics") or {}).keys():
            metric_names.add(k)
        pn = (r.get("params") or {}).get("persona_name")
        if pn:
            persona_names.add(str(pn))
    return {
        "n":             len(runs),
        "ok":            ok_n,
        "failed":        len(runs) - ok_n,
        "sim_names":     sorted(sim_names),
        "personas_seen": sorted(persona_names),
        "metrics_seen":  sorted(metric_names),
        "first":         min(started) if started else "",
        "latest":        max(started) if started else "",
    }


# ============================================================
# Query routing
# ============================================================

# Pattern keywords for each computation. Order matters — the router
# picks the first match. Generic "summarise" stays last so a more
# specific question is preferred when it overlaps.

_PERSONA_PATTERNS = (
    re.compile(r"\b(?:by|per|across|each|grouped\s+by)\s+persona", re.I),
    re.compile(r"\bcompare\s+persona", re.I),
    re.compile(r"\bwhich\s+persona", re.I),
    re.compile(r"\bbest\s+persona", re.I),
)

_CORRELATION_PATTERNS = (
    re.compile(r"\b(?:correlat|relate|relationship\s+between)", re.I),
    re.compile(r"\b(?:effect|impact)\s+of\s+\w+\s+on\b", re.I),
    re.compile(r"\b(?:how|does)\s+\w+\s+affect", re.I),
)

_FAILURES_PATTERNS = (
    re.compile(r"\b(?:fail|failed|failure|crash|error|crashed)", re.I),
    re.compile(r"\bwhich\s+runs?\s+(?:didn'?t|did\s+not)\b", re.I),
)

_BEST_PATTERNS = (
    re.compile(r"\b(?:best|highest|max|maximum|top)\s+\w+", re.I),
    re.compile(r"\b(?:which|what)\s+(?:params|combo|combination)\b", re.I),
)

_RECENT_PATTERNS = (
    re.compile(r"\b(?:last|recent|latest)\s+(?:sweep|sim|run|sims|runs)", re.I),
    re.compile(r"\bwhat\s+(?:did|do)\s+the\s+(?:last|recent|latest)", re.I),
)

_SUMMARY_PATTERNS = (
    re.compile(r"\b(?:summary|summarise|summarize|overview|report)\b", re.I),
    re.compile(r"\bwhat\s+(?:did|do)\s+the\s+sims?\s+show", re.I),
)

# Metric name guesser — pulls a candidate metric out of the query
# so "which persona has the highest score" knows to look up "score".
_METRIC_NAME_RE = re.compile(
    r"\b(?:metric|score|value|of|the)\s+([a-z_][a-z0-9_]{2,32})",
    re.IGNORECASE,
)

# Param name guesser for correlate / impact-of queries
_PARAM_OF_RE = re.compile(
    r"\b(?:of|impact\s+of|effect\s+of|how\s+does)\s+"
    r"([a-z_][a-z0-9_.]{2,32})\s+(?:on|affect|relate)",
    re.IGNORECASE,
)


def _loose_name_match(known, ql: str) -> Optional[str]:
    """Tolerant name → query match for namespaced/underscored keys.

    Lets "XP growth" match the param ``balance.XP_GROWTH`` and "king"
    match the metric ``t_king``: drops the namespace prefix
    (``balance.`` / ``brain.`` / ``persona.``), splits the remainder on
    ``_``/punctuation into tokens, and requires at least half of those
    tokens to appear (stem-matched) in the query. Returns the
    best-scoring name, or None when nothing clears the bar.
    """
    import re as _re
    best, best_score = None, 0.0
    for k in known:
        base = str(k).split(".")[-1]
        toks = [t for t in _re.split(r"[_\W]+", base.lower()) if len(t) >= 2]
        if not toks:
            continue
        hits = 0
        for t in toks:
            stem = t if len(t) <= 4 else t[: len(t) - 2]
            if stem in ql:
                hits += 1
        if hits == 0:
            continue
        score = hits / len(toks) + 0.01 * hits
        if score > best_score:
            best, best_score = k, score
    return best if best_score >= 0.5 else None


def _pick_metric(query: str, runs: List[Dict[str, Any]]) -> Optional[str]:
    """Guess which metric the user is asking about. Prefer an explicit
    name in the query if it matches a known metric; otherwise fall
    back to the most common metric across the run set.
    """
    known = set()
    for r in runs:
        for m in (r.get("metrics") or {}).keys():
            known.add(m)
    # Explicit substring hit
    ql = query.lower()
    for m in sorted(known, key=len, reverse=True):
        if m.lower() in ql:
            return m
    # Tolerant token-stem match ("survives longest" → survived_s).
    loose = _loose_name_match(known, ql)
    if loose is not None:
        return loose
    # Match by guesser
    m = _METRIC_NAME_RE.search(query)
    if m and m.group(1) in known:
        return m.group(1)
    # Fallback: the metric most runs have a numeric value for
    counts: Dict[str, int] = {}
    for r in runs:
        for k, v in (r.get("metrics") or {}).items():
            if isinstance(v, (int, float)):
                counts[k] = counts.get(k, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _pick_param(query: str, runs: List[Dict[str, Any]]) -> Optional[str]:
    """Guess which param the user is correlating against."""
    known: set = set()
    for r in runs:
        for k, v in (r.get("params") or {}).items():
            if isinstance(v, (int, float)):
                known.add(k)
    ql = query.lower()
    # Prefer longer matches so "persona.greed" wins over "greed"
    for k in sorted(known, key=len, reverse=True):
        if k.lower() in ql:
            return k
    # Tolerant token-stem match ("XP growth" → balance.XP_GROWTH).
    loose = _loose_name_match(known, ql)
    if loose is not None:
        return loose
    m = _PARAM_OF_RE.search(query)
    if m and m.group(1) in known:
        return m.group(1)
    return None


# ============================================================
# Public routes
# ============================================================

def answer_question(
    query: str,
    vault_dir: Any,
    *,
    sim_name: str = "",
    limit: int = 200,
) -> SimAnalystResult:
    """Pattern-match the user's free-text query and dispatch."""
    q = (query or "").strip()
    if not q:
        return SimAnalystResult(error="empty question",
                                 confidence="low")
    runs = _load_index(vault_dir, sim_name=sim_name, limit=limit)
    if not runs:
        return SimAnalystResult(
            answer=("No sim runs recorded yet. Open the 🎲 "
                    "Simulations tab and kick off a sweep first."),
            confidence="low",
        )

    # ── Failures first — explicit failure questions take priority
    if any(p.search(q) for p in _FAILURES_PATTERNS):
        return _route_failures(runs)
    # ── Per-persona comparisons
    if any(p.search(q) for p in _PERSONA_PATTERNS):
        return _route_per_persona(q, runs)
    # ── Correlation
    if any(p.search(q) for p in _CORRELATION_PATTERNS):
        return _route_correlation(q, runs)
    # ── Best / top run
    if any(p.search(q) for p in _BEST_PATTERNS):
        return _route_best_run(q, runs)
    # ── Recent / latest
    if any(p.search(q) for p in _RECENT_PATTERNS):
        return _route_recent(runs)
    # ── Generic summary fallback
    if any(p.search(q) for p in _SUMMARY_PATTERNS):
        return _route_summary(runs)
    # ── No idiom matched — give the summary anyway so the user
    # always gets *something* concrete rather than a "couldn't
    # parse" wall.
    return _route_summary(runs)


# ── Routes ──────────────────────────────────────────────────────

def _route_summary(runs: List[Dict[str, Any]]) -> SimAnalystResult:
    s = summarise_runs(runs)
    lines = [
        f"Overview of {s['n']} run(s).",
        f"  {s['ok']} ok, {s['failed']} failed.",
        f"  Time span: {s['first']} → {s['latest']}",
    ]
    if s.get("sim_names"):
        lines.append("  Sim names: " + ", ".join(s["sim_names"]))
    if s.get("personas_seen"):
        lines.append("  Personas seen: " + ", ".join(s["personas_seen"]))
    if s.get("metrics_seen"):
        lines.append("  Metrics seen: " + ", ".join(s["metrics_seen"]))
    return SimAnalystResult(
        answer="\n".join(lines),
        confidence="high",
        computed_values=s,
        sources=[r["id"] for r in runs[:8]],
    )


def _route_recent(runs: List[Dict[str, Any]]) -> SimAnalystResult:
    latest = runs[:8]
    lines = ["Most recent runs:"]
    for r in latest:
        lines.append(_brief_run_line(r))
    return SimAnalystResult(
        answer="\n".join(lines),
        confidence="high",
        sources=[r["id"] for r in latest],
    )


def _route_failures(runs: List[Dict[str, Any]]) -> SimAnalystResult:
    bad = failures(runs)
    if not bad:
        return SimAnalystResult(
            answer=f"None of the {len(runs)} runs failed.",
            confidence="high",
            computed_values={"n_failed": 0, "n_total": len(runs)},
        )
    lines = [f"{len(bad)} of {len(runs)} runs failed."]
    for r in bad[:8]:
        lines.append(_brief_run_line(r))
        # Pull the error from the full record if available
        from sim_recorder import SimRecorder
        try:
            rec = SimRecorder(Path(r.get("file", "")).parent.parent
                                if r.get("file") else ".")
            full = rec.load_run(r["id"])
            if full and full.error:
                lines.append(f"    error: {full.error[:120]}")
        except Exception:
            pass
    return SimAnalystResult(
        answer="\n".join(lines),
        confidence="high",
        computed_values={"n_failed": len(bad), "n_total": len(runs)},
        sources=[r["id"] for r in bad[:8]],
    )


def _route_per_persona(query: str,
                         runs: List[Dict[str, Any]]) -> SimAnalystResult:
    metric = _pick_metric(query, runs)
    if metric is None:
        return SimAnalystResult(
            answer=("No numeric metrics found across the recorded runs. "
                    "Confirm your sim emits ANVIL_METRIC lines."),
            confidence="low",
        )
    groups = group_by(runs, "persona_name")
    if not groups:
        return SimAnalystResult(
            answer=("No runs include a persona_name. Pick a persona in "
                    "the Sim tab, or add a persona axis to the sweep."),
            confidence="low",
        )
    lines = [
        f"Distribution of metric `{metric}` per persona:",
    ]
    computed: Dict[str, Any] = {}
    sources: List[str] = []
    for persona, rs in sorted(groups.items()):
        vals = [(r.get("metrics") or {}).get(metric) for r in rs]
        stats = median_iqr(vals)
        sources.extend(r["id"] for r in rs[:3])
        if stats is None:
            lines.append(f"  {persona}: no numeric values for `{metric}`")
            continue
        lines.append(
            f"  {persona:14s} n={stats['n']:3d}  "
            f"median={stats['median']:>8.2f}  "
            f"p25={stats['p25']:>8.2f}  p75={stats['p75']:>8.2f}"
        )
        computed[f"{persona}.median_{metric}"] = stats["median"]
        computed[f"{persona}.n"] = stats["n"]
    return SimAnalystResult(
        answer="\n".join(lines),
        confidence="high",
        computed_values=computed,
        sources=sources[:8],
    )


def _route_correlation(query: str,
                         runs: List[Dict[str, Any]]) -> SimAnalystResult:
    metric = _pick_metric(query, runs)
    param = _pick_param(query, runs)
    if metric is None or param is None:
        return SimAnalystResult(
            answer=("Couldn't identify which param ↔ metric pair to "
                    "correlate. Mention both names explicitly "
                    "(e.g. 'how does difficulty affect score')."),
            confidence="low",
        )
    stats = correlate(runs, param, metric)
    if stats is None:
        return SimAnalystResult(
            answer=(f"Fewer than 3 numeric (param, metric) pairs for "
                    f"({param!r}, {metric!r}) — not enough data to "
                    "correlate."),
            confidence="low",
        )
    r = stats["r"]
    strength = (
        "strong negative" if r <= -0.7 else
        "moderate negative" if r <= -0.4 else
        "weak negative" if r <  -0.1 else
        "no clear" if abs(r) <  0.1 else
        "weak positive" if r <  0.4 else
        "moderate positive" if r <  0.7 else
        "strong positive"
    )
    return SimAnalystResult(
        answer=(
            f"Correlation between `{param}` and `{metric}` "
            f"is r={r:+.3f} ({strength} relationship; n={stats['n']}).\n"
            f"  param range: {stats['param_range']}\n"
            f"  metric range: {stats['metric_range']}"
        ),
        confidence="high",
        computed_values={"r": r, "n": stats["n"],
                          "param": param, "metric": metric},
        sources=[r["id"] for r in runs[:8]],
    )


def _route_best_run(query: str,
                      runs: List[Dict[str, Any]]) -> SimAnalystResult:
    metric = _pick_metric(query, runs)
    if metric is None:
        return SimAnalystResult(
            answer="No numeric metrics to rank by.",
            confidence="low",
        )
    higher = "lowest" not in query.lower() and "min" not in query.lower()
    eligible = [
        r for r in runs
        if isinstance((r.get("metrics") or {}).get(metric), (int, float))
    ]
    if not eligible:
        return SimAnalystResult(
            answer=f"No runs have a numeric `{metric}` value.",
            confidence="low",
        )
    eligible.sort(
        key=lambda r: (r.get("metrics") or {}).get(metric, 0),
        reverse=higher,
    )
    top = eligible[0]
    bp = best_per_persona(eligible, metric, higher_is_better=higher)
    lines = [
        f"{'Best' if higher else 'Worst'} run by `{metric}`:",
        "  " + _brief_run_line(top) + f"  ({metric}={top['metrics'][metric]})",
    ]
    if bp:
        lines.append(f"\n{'Top' if higher else 'Bottom'} per persona:")
        for persona, r in bp[:8]:
            val = r["metrics"][metric]
            lines.append(f"  {persona:14s}  {metric}={val}  ({r['id']})")
    return SimAnalystResult(
        answer="\n".join(lines),
        confidence="high",
        computed_values={f"top_{metric}": top["metrics"][metric]},
        sources=[top["id"]] + [r["id"] for _p, r in bp[:8]],
    )


# ── Helpers ─────────────────────────────────────────────────────

def _brief_run_line(r: Dict[str, Any]) -> str:
    """Single-line summary for an index entry — used in answers."""
    sim = r.get("sim_name", "?")
    backend = r.get("backend", "?")
    persona = (r.get("params") or {}).get("persona_name")
    persona_chip = f" [{persona}]" if persona else ""
    metrics = r.get("metrics") or {}
    metric_chip = ""
    if metrics:
        m_brief = ", ".join(
            f"{k}={v}" for k, v in list(metrics.items())[:2]
        )
        metric_chip = f"  → {m_brief}"
    return f"  {r.get('id','?')}  {sim}/{backend}{persona_chip}{metric_chip}"
