"""``compare``: read both nodes' exports and write the verdict.

Output (``reports/compare/<name>/``): ``compare.md`` plus CSVs —
``metrics.csv`` (per node and book), ``paired.csv`` (the A-vs-B tests),
``price_check.csv``, ``tuner_timeline.csv``, ``equity_curves.csv`` — and
``equity.svg`` (A live, B live, control, SPY; indexed to 1 at the start).

Checks before any verdict: both nodes' daily price hashes match on every
day of the window; both controls (frozen, no news) made the same
predictions; both used the same model backend. A failed check is stated at
the top of the report — a comparison of nodes that saw different data
says nothing about news.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..backtest import metrics as mx
from ..experiment import Experiment
from . import stats

COLORS = {"A live": "#2a78d6", "B live": "#eb6834", "control": "#1baf7a",
          "SPY": "#eda100"}
COLORS_DARK = {"A live": "#3987e5", "B live": "#d95926", "control": "#199e70",
               "SPY": "#c98500"}


class CompareError(ValueError):
    pass


def _read(folder: Path, name: str, required: bool = True) -> pd.DataFrame:
    path = Path(folder) / f"{name}.csv"
    if not path.exists():
        if required:
            raise CompareError(f"missing {path} — run export on that node")
        return pd.DataFrame()
    df = pd.read_csv(path)
    for c in ("date", "day", "signal_date"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c]).dt.date
    return df


def load_node(folder: Path, expect: str) -> Dict[str, pd.DataFrame]:
    out = {n: _read(folder, n) for n in ("books", "predictions", "equity",
                                         "price_hashes", "models")}
    out["tuner_log"] = _read(folder, "tuner_log", required=False)
    nodes = set(out["equity"]["node"]) if len(out["equity"]) else set()
    if nodes and nodes != {expect}:
        raise CompareError(f"{folder} holds node {sorted(nodes)}, expected "
                           f"{expect}")
    return out


def _window(df: pd.DataFrame, exp: Experiment, col: str = "date"):
    return df[(df[col] >= exp.start) & (df[col] <= exp.end)]


def book_returns(node: Dict[str, pd.DataFrame], book: str,
                 exp: Experiment) -> pd.Series:
    """Daily returns over the window, the first measured from the equity
    the day before the start (or the starting cash)."""
    eq = node["equity"]
    eq = eq[eq["book"] == book].sort_values("date")
    before = eq[eq["date"] < exp.start]
    start_cash = float(node["books"].set_index("book").loc[book,
                                                           "start_cash"])
    base = float(before["equity"].iloc[-1]) if len(before) else start_cash
    w = _window(eq, exp)
    values = np.concatenate([[base], w["equity"].to_numpy(dtype=float)])
    return pd.Series(values[1:] / values[:-1] - 1, index=w["date"].to_numpy())


def scored(node: Dict[str, pd.DataFrame], book: str,
           exp: Experiment) -> pd.DataFrame:
    p = node["predictions"]
    p = p[(p["book"] == book) & p["target"].notna()]
    return _window(p, exp)


def price_check(a, b, exp: Experiment) -> pd.DataFrame:
    ha = _window(a["price_hashes"], exp)[["date", "price_hash"]]
    hb = _window(b["price_hashes"], exp)[["date", "price_hash"]]
    j = ha.merge(hb, on="date", how="outer", suffixes=("_A", "_B"))
    j["match"] = (j["price_hash_A"] == j["price_hash_B"]) & \
        j["price_hash_A"].notna()
    return j.sort_values("date").reset_index(drop=True)


def control_check(a, b, exp: Experiment) -> Dict[str, float]:
    ca, cb = scored(a, "control", exp), scored(b, "control", exp)
    allc_a = _window(a["predictions"][a["predictions"]["book"] == "control"],
                     exp)
    allc_b = _window(b["predictions"][b["predictions"]["book"] == "control"],
                     exp)
    j = allc_a.merge(allc_b, on=["date", "ticker"], suffixes=("_a", "_b"))
    gap = float((j["p_a"] - j["p_b"]).abs().max()) if len(j) else float("nan")
    return {"rows_a": float(len(allc_a)), "rows_b": float(len(allc_b)),
            "common": float(len(j)), "max_abs_p_gap": gap,
            "scored_a": float(len(ca)), "scored_b": float(len(cb))}


def node_metrics(node, name: str, exp: Experiment) -> List[Dict[str, Any]]:
    rows = []
    for book in ("live", "control", "spy"):
        row: Dict[str, Any] = {"node": name, "book": book}
        s = scored(node, book, exp)
        if len(s):
            row.update(mx.prediction_metrics(s["p"].to_numpy(),
                                             s["target"].to_numpy()))
        r = book_returns(node, book, exp)
        if len(r):
            row.update(mx.strategy_metrics(pd.Series(r.to_numpy())))
        rows.append(row)
    return rows


def timeline(a, b, exp: Experiment) -> pd.DataFrame:
    rows = []
    for name, node in (("A", a), ("B", b)):
        t = node["tuner_log"]
        if t.empty:
            continue
        t = _window(t, exp, "day")
        for r in t.itertuples():
            rows.append({"node": name, "day": r.day, "status": r.status,
                         "changes": r.changes_json if isinstance(
                             r.changes_json, str) else "",
                         "improvement": r.improvement,
                         "edge_t": r.edge_t})
    return pd.DataFrame(rows, columns=["node", "day", "status", "changes",
                                       "improvement", "edge_t"])


def curves(a, b, exp: Experiment) -> pd.DataFrame:
    series = {"A live": book_returns(a, "live", exp),
              "B live": book_returns(b, "live", exp),
              "control": book_returns(a, "control", exp),
              "SPY": book_returns(a, "spy", exp)}
    df = pd.DataFrame({k: (1 + v).cumprod() for k, v in series.items()})
    df.index.name = "date"
    return df.reset_index()


def equity_svg(df: pd.DataFrame, title: str) -> str:
    """A static line chart: one axis (growth of 1), direct labels at the
    line ends plus a legend; benchmarks dashed so identity is not colour
    alone. Light and dark steps of the same hues."""
    W, H, L, R, T, B = 760, 360, 56, 120, 40, 36
    names = [c for c in COLORS if c in df.columns]
    vals = df[names].to_numpy(dtype=float)
    n = len(df)
    lo = min(1.0, np.nanmin(vals)) if n else 0.99
    hi = max(1.0, np.nanmax(vals)) if n else 1.01
    pad = (hi - lo) * 0.08 or 0.01
    lo, hi = lo - pad, hi + pad

    def x(i):
        return L + (W - L - R) * (i / max(n - 1, 1))

    def y(v):
        return T + (H - T - B) * (1 - (v - lo) / (hi - lo))
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'width="{W}" height="{H}" role="img" aria-label="{title}">',
           "<style>",
           ".bg{fill:#fcfcfb}.ink{fill:#0b0b0b}.ink2{fill:#52514e}"
           ".grid{stroke:#e4e3df;stroke-width:1}.base{stroke:#a3a29c}",
           *[f".s{i}{{stroke:{COLORS[nm]}}}.f{i}{{fill:{COLORS[nm]}}}"
             for i, nm in enumerate(names)],
           "@media (prefers-color-scheme: dark){.bg{fill:#1a1a19}"
           ".ink{fill:#f0efec}.ink2{fill:#c3c2b7}.grid{stroke:#33332f}"
           ".base{stroke:#6b6a64}" + "".join(
               f".s{i}{{stroke:{COLORS_DARK[nm]}}}.f{i}{{fill:"
               f"{COLORS_DARK[nm]}}}" for i, nm in enumerate(names)) + "}",
           "text{font:12px system-ui,-apple-system,Segoe UI,sans-serif}",
           "</style>",
           f'<rect class="bg" width="{W}" height="{H}"/>',
           f'<text class="ink" x="{L}" y="22" style="font-weight:600">'
           f"{title}</text>"]
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        out.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y(v):.1f}"'
                   f' y2="{y(v):.1f}"/>')
        out.append(f'<text class="ink2" x="{L - 6}" y="{y(v) + 4:.1f}" '
                   f'text-anchor="end">{v:.3f}</text>')
    out.append(f'<line class="base" x1="{L}" x2="{W - R}" y1="{y(1):.1f}" '
               f'y2="{y(1):.1f}" stroke-dasharray="2 3"/>')
    if n:
        for i in sorted({0, n // 2, n - 1}):
            out.append(f'<text class="ink2" x="{x(i):.1f}" y="{H - 12}" '
                       f'text-anchor="middle">{df["date"].iloc[i]}</text>')
    ends = []
    for i, nm in enumerate(names):
        pts = " ".join(f"{x(k):.1f},{y(v):.1f}"
                       for k, v in enumerate(df[nm]) if np.isfinite(v))
        dash = ' stroke-dasharray="6 4"' if nm in ("control", "SPY") else ""
        out.append(f'<polyline class="s{i}" fill="none" stroke-width="2" '
                   f'stroke-linejoin="round" points="{pts}"{dash}/>')
        if n:
            ends.append([y(df[nm].iloc[-1]), i, nm, df[nm].iloc[-1]])
    ends.sort()
    for k in range(1, len(ends)):                     # keep labels apart
        ends[k][0] = max(ends[k][0], ends[k - 1][0] + 14)
    for yy, i, nm, v in ends:
        out.append(f'<circle class="f{i}" cx="{x(n - 1):.1f}" '
                   f'cy="{y(v):.1f}" r="4"/>')
        out.append(f'<text class="ink" x="{W - R + 10}" y="{yy + 4:.1f}">'
                   f"{nm} {v:.3f}</text>")
    lx = L
    for i, nm in enumerate(names):                    # legend row
        dash = ' stroke-dasharray="6 4"' if nm in ("control", "SPY") else ""
        out.append(f'<line class="s{i}" x1="{lx}" x2="{lx + 18}" y1="{T - 6}"'
                   f' y2="{T - 6}" stroke-width="2"{dash}/>')
        out.append(f'<text class="ink2" x="{lx + 24}" y="{T - 2}">{nm}</text>')
        lx += 24 + 8 * len(nm) + 20
    out.append("</svg>")
    return "\n".join(out)


def _fmt(v, nd=4):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "–"
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def compare(a_dir: Path, b_dir: Path, exp: Experiment, out_dir: Path,
            changed_files: Optional[List[str]] = None) -> Dict[str, Any]:
    a, b = load_node(a_dir, "A"), load_node(b_dir, "B")
    bs = exp.bootstrap
    kw = dict(resamples=int(bs.get("resamples", 10000)),
              seed=int(bs.get("seed", 0)),
              confidence=float(bs.get("confidence", 0.95)))
    warnings: List[str] = []
    prices = price_check(a, b, exp)
    bad = prices[~prices["match"]]
    if len(bad):
        warnings.append(f"PRICE DATA DIFFERS on {len(bad)} of {len(prices)} "
                        "days (or is missing on one node) — the nodes did "
                        "not see identical prices; the comparison is not "
                        "valid until this is explained.")
    if prices.empty:
        warnings.append("No price hashes in the window — did both nodes run?")
    ctrl = control_check(a, b, exp)
    if not (ctrl["max_abs_p_gap"] <= 1e-9) or ctrl["rows_a"] != ctrl["rows_b"]:
        warnings.append(f"The two CONTROL books differ (max |p gap| "
                        f"{_fmt(ctrl['max_abs_p_gap'], 6)}, rows "
                        f"{ctrl['rows_a']:.0f} vs {ctrl['rows_b']:.0f}) — "
                        "they are frozen and never see news, so they "
                        "should match exactly.")
    ba = set(a["models"]["backend"].dropna()) if "backend" in a["models"] \
        else set()
    bb = set(b["models"]["backend"].dropna()) if "backend" in b["models"] \
        else set()
    if ba != bb:
        warnings.append(f"Model backends differ: A {sorted(ba)}, B "
                        f"{sorted(bb)}.")
    if changed_files:
        warnings.append("Settings changed since the protocol was frozen: " +
                        ", ".join(changed_files))
    ll = stats.paired_log_loss(scored(a, "live", exp), scored(b, "live", exp),
                               **kw)
    ret = stats.paired_returns(book_returns(a, "live", exp),
                               book_returns(b, "live", exp), **kw)
    if ll["n_days"] < 15:
        warnings.append(f"Only {ll['n_days']:.0f} scored days — too few for "
                        "much statistical power.")
    v_ll = stats.verdict(ll, "log loss (B − A)",
                         "Node A (news) predicted better than node B.",
                         "Node A (news) predicted worse than node B.")
    v_ret = stats.verdict(dict(ret, identical=ll["identical"]),
                          "daily return (A − B)",
                          "Node A (news) earned more per day than node B.",
                          "Node A (news) earned less per day than node B.")
    metrics = pd.DataFrame(node_metrics(a, "A", exp) +
                           node_metrics(b, "B", exp))
    acc = metrics.get("accuracy", pd.Series(np.nan, index=metrics.index))
    learned = metrics["book"] != "spy"
    for r in metrics[learned & (acc > 0.60)].itertuples():
        warnings.append(f"{r.node} {r.book} accuracy {r.accuracy:.1%} is far "
                        "too good for daily stock direction — investigate "
                        "leakage before believing anything in this report.")
    hot = metrics[learned & (acc > 0.55) & (acc <= 0.60)]
    if len(hot):
        warnings.append(
            "Accuracy above 55% (" + ", ".join(
                f"{r.node} {r.book} {r.accuracy:.1%}"
                for r in hot.itertuples()) + f") over {ll['n_days']:.0f} "
            "days. Tickers move together, so a short window swings widely "
            "and this is probably luck — but rule out leakage before "
            "believing it.")
    tl = timeline(a, b, exp)
    cv = curves(a, b, exp)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paired = pd.DataFrame([dict(test="log_loss_B_minus_A", **ll),
                           dict(test="daily_return_A_minus_B", **ret)])
    for name, df in (("metrics", metrics), ("paired", paired),
                     ("price_check", prices), ("tuner_timeline", tl),
                     ("equity_curves", cv)):
        df.to_csv(out_dir / f"{name}.csv", index=False)
    (out_dir / "equity.svg").write_text(
        equity_svg(cv, f"Growth of 1 — {exp.start} to {exp.end}"),
        encoding="utf-8")
    md = _markdown(exp, warnings, ll, ret, v_ll, v_ret, metrics, prices, ctrl,
                   tl)
    (out_dir / "compare.md").write_text(md, encoding="utf-8")
    return {"out_dir": out_dir, "warnings": warnings, "log_loss": ll,
            "returns": ret, "verdict": v_ll, "verdict_returns": v_ret}


def _markdown(exp, warnings, ll, ret, v_ll, v_ret, metrics, prices, ctrl,
              tl) -> str:
    L = [f"# Quant Duel — A (news) vs B (no news)", "",
         f"**{exp.name}** · {exp.start} → {exp.end} · "
         f"{ll['n_days']:.0f} scored days · {ll['n_rows']:.0f} paired "
         "predictions", ""]
    if warnings:
        L += ["## ⚠ Warnings", ""] + [f"- {w}" for w in warnings]
        L += [""]
    else:
        L += ["Checks passed: identical prices on every day, identical "
              "control books, same model backend.", ""]
    L += ["## Verdict", "", f"**Primary (log loss):** {v_ll}", "",
          f"**Returns:** {v_ret}", "",
          "## Primary metric — live log loss (lower is better)", "",
          "| | A (news) | B (no news) | B − A | 95% CI | t | p |",
          "|---|---:|---:|---:|---|---:|---:|",
          f"| log loss | {ll['ll_a']:.5f} | {ll['ll_b']:.5f} | "
          f"{ll['diff']:+.5f} | [{ll['ci_low']:+.5f}, {ll['ci_high']:+.5f}] | "
          f"{ll['t']:+.2f} | {ll['p_value']:.3f} |", "",
          "Paired on the same (date, ticker) predictions; the interval is a "
          "bootstrap over trading days (tickers on one day are not "
          "independent).", "",
          "## Daily returns — live, A − B", "",
          f"Mean {ret['mean_diff'] * 1e4:+.2f} bps/day, 95% CI "
          f"[{ret['ci_low'] * 1e4:+.2f}, {ret['ci_high'] * 1e4:+.2f}] bps, "
          f"t {ret['t']:+.2f}, p {ret['p_value']:.3f}, "
          f"{ret['n_days']:.0f} days.", "",
          "## All metrics", "",
          "| node | book | n | accuracy | log loss | brier | auc | return | "
          "sharpe | max DD |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"
          "---:|"]
    for r in metrics.to_dict("records"):
        L.append(f"| {r['node']} | {r['book']} | {_fmt(r.get('n'), 0)} | "
                 f"{_fmt(r.get('accuracy'))} | {_fmt(r.get('log_loss'))} | "
                 f"{_fmt(r.get('brier'))} | {_fmt(r.get('auc'))} | "
                 f"{_fmt(r.get('cum_return'))} | {_fmt(r.get('sharpe'), 2)} | "
                 f"{_fmt(r.get('max_drawdown'))} |")
    L += ["", "## Equity curves", "", "![equity](equity.svg)", "",
          "(values in `equity_curves.csv`)", "",
          "## Data checks", "",
          f"- Price hashes: {int(prices['match'].sum())} of {len(prices)} "
          "days match.",
          f"- Controls: {ctrl['common']:.0f} common predictions, max |p gap| "
          f"{_fmt(ctrl['max_abs_p_gap'], 8)}.", "",
          "## Tuner timeline", ""]
    if tl.empty:
        L.append("No tuner rounds in the window.")
    else:
        L += ["| node | day | status | changes | improvement | t |",
              "|---|---|---|---|---:|---:|"]
        for r in tl.itertuples():
            ch = r.changes
            try:
                ch = ", ".join(f"{k}: {v[0]} → {v[1]}" for k, v in
                               json.loads(ch).items()) if ch else ""
            except (ValueError, AttributeError, TypeError, IndexError,
                    KeyError):
                pass
            L.append(f"| {r.node} | {r.day} | {r.status} | {ch} | "
                     f"{_fmt(r.improvement, 5)} | {_fmt(r.edge_t, 2)} |")
    L += ["", "_Paper trading only — no real orders were ever placed._", ""]
    return "\n".join(L)
