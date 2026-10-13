"""
council_core.verdict_log — which members actually help, by kind of
question; and which panels debate with copies of one model.

    <vault>/.council_usage/verdicts-YYYY-MM.jsonl     one line per question

WHO HELPS. Panels are fixed per route (council_turn.PANEL_FOR_ROUTE), and
nothing recorded whether a member that sits on, say, every data question
ever wins, or whether its points make it into the answer. After each
deliberated question this keeps: the route and depth, the panel, the
Judge's winner and scores, the verdict and confidence, the calls the
question cost, and for each member its self-confidence and its INFLUENCE —
the share of its draft's content words that reached the final answer (a
rough, model-free measure: an objection the Writer took up counts, a draft
nobody used does not). Never the question or the answers: a meter, not a
transcript. `summarise` turns a week of lines into, per route and member:
questions sat on, wins, mean score, mean influence. The weekly placement
review shows it (WHO HELPS) and the controller reads it.

MODEL MIX. A debate between copies of one model mostly repeats itself — the
same blind spots, and drafts that "agree" for the wrong reason (which also
makes depth.agree skip the cross-fire). `shared_models` lists, per route,
the members of its panel that answer with the same model, for the weekly
report and the Role specs cards.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .usage_log import usage_dir

_lock = threading.Lock()
_WORD = re.compile(r"[a-z0-9_]{4,}")
_STOP = frozenset("""that this with from have will would could should their
there they them then than what when where which while your about into over
also just only some more most such very been being does done each other
those these here confidence""".split())


def _file_for(vault_dir: Path, ts: float) -> Path:
    return usage_dir(vault_dir) / time.strftime("verdicts-%Y-%m.jsonl",
                                                time.localtime(ts))


def _words(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def influence(draft: str, final: str) -> float:
    """Share of the draft's content words that appear in the final answer,
    0–1."""
    d, f = _words(draft), _words(final)
    if not d or not f:
        return 0.0
    return round(len(d & f) / len(d), 3)


def entry(result: Any, candidates: Dict[str, Dict[str, Any]],
          now: Optional[float] = None) -> Dict[str, Any]:
    """One line for a finished question (a council_turn.TurnResult and the
    orchestrator's candidates)."""
    scores: Dict[str, Any] = {}
    try:
        for e in reversed(getattr(result, "events", []) or []):
            if e.who == "Judge" and e.text.startswith("Ranking:"):
                scores = json.loads(e.text.split("Ranking:\n", 1)[-1]) \
                    .get("scores") or {}
                break
    except Exception:                                     # noqa: BLE001
        scores = {}
    final = getattr(result, "answer", "") or ""
    members = {}
    for role, c in (candidates or {}).items():
        members[role] = {
            "self_confidence": c.get("self_confidence"),
            "score": scores.get(role),
            "influence": influence(c.get("answer", "") + "\n"
                                   + c.get("rebuttal", ""), final),
        }
    t = time.time() if now is None else now
    return {"ts": math.floor(t * 1000) / 1000,
            "route": getattr(result, "route", "") or "",
            "depth": getattr(result, "depth", "") or "",
            "panel": list(getattr(result, "panel", []) or []),
            "synth": getattr(result, "synth", "") or "",
            "winner": getattr(result, "winner", "") or "",
            "verdict": getattr(result, "verdict", "") or "",
            "confidence_pct": getattr(result, "confidence", 0) or 0,
            "calls": (getattr(result, "meter", {}) or {}).get("calls"),
            "members": members}


def record(vault_dir: Path, line: Dict[str, Any]) -> None:
    """Append one question's line. Never raises."""
    try:
        path = _file_for(vault_dir, float(line.get("ts") or time.time()))
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:                                     # noqa: BLE001
        pass


def read(vault_dir: Path, since: float,
         until: Optional[float] = None) -> List[Dict[str, Any]]:
    until = time.time() if until is None else until
    out: List[Dict[str, Any]] = []
    t, names = since, []
    while True:
        n = _file_for(vault_dir, t).name
        if n not in names:
            names.append(n)
        if t >= until:
            break
        t = min(until, t + 20 * 86400)
    for n in names:
        try:
            text = (usage_dir(vault_dir) / n).read_text(encoding="utf-8",
                                                       errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                e = json.loads(raw)
                ts = float(e.get("ts", 0))
            except (ValueError, TypeError, AttributeError):
                continue
            if since <= ts <= until:
                out.append(e)
    out.sort(key=lambda e: e["ts"])
    return out


def summarise(lines: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per (route, member): questions, wins, mean Judge score, mean
    influence on the final answer, mean self-confidence."""
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for line in lines:
        route = line.get("route") or "(none)"
        for role, m in (line.get("members") or {}).items():
            g = groups.setdefault((route, role), {
                "route": route, "role": role, "questions": 0, "wins": 0,
                "_score": [], "_infl": [], "_conf": []})
            g["questions"] += 1
            g["wins"] += 1 if line.get("winner") == role else 0
            for key, src in (("_score", "score"), ("_infl", "influence"),
                             ("_conf", "self_confidence")):
                v = m.get(src)
                if isinstance(v, (int, float)):
                    g[key].append(float(v))
    out = []
    for g in groups.values():
        for key, name in (("_score", "mean_score"), ("_infl", "mean_influence"),
                          ("_conf", "mean_confidence")):
            vals = g.pop(key)
            g[name] = round(sum(vals) / len(vals), 2) if vals else None
        out.append(g)
    out.sort(key=lambda g: (g["route"], -g["questions"], g["role"]))
    return out


def summary_lines(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """WHO HELPS, for the weekly report."""
    out = []
    for g in rows:
        infl = g.get("mean_influence")
        score = g.get("mean_score")
        flag = ""
        if g["questions"] >= 5 and g["wins"] == 0 and (infl or 0) < 0.15:
            flag = " — never wins and little of it reaches the answer"
        score_txt = "?" if score is None else f"{score:g}%"
        infl_txt = "?" if infl is None else f"{infl:.0%}"
        out.append(f"  {g['route']}: {g['role']} on {g['questions']} "
                   f"question(s), won {g['wins']}, mean score {score_txt}, "
                   f"influence {infl_txt}{flag}")
    return out


def shared_models(role_models: Dict[str, str],
                  panels: Optional[Dict[str, Tuple[List[str], str]]] = None
                  ) -> List[Tuple[str, str, List[str]]]:
    """(route, model, roles) for every panel with two or more members on
    the same model. `role_models` maps role → model name ("" = the main
    model, which counts as one model too)."""
    if panels is None:
        from .council_turn import PANEL_FOR_ROUTE as panels
    out = []
    for route, (panel, _synth) in panels.items():
        if route.startswith("_"):
            continue
        by_model: Dict[str, List[str]] = {}
        for role in panel:
            if role == "peasant":
                continue                  # the Peasant questions; it does not draft against them
            model = role_models.get(role, "") or "(main model)"
            by_model.setdefault(model, []).append(role)
        for model, roles in by_model.items():
            if len(roles) >= 2:
                out.append((route, model, roles))
    return out


def mix_lines(shared: Sequence[Tuple[str, str, List[str]]]) -> List[str]:
    return [f"  {route}: {', '.join(roles)} all answer with {model}"
            for route, model, roles in shared]


__all__ = ["influence", "entry", "record", "read", "summarise",
           "summary_lines", "shared_models", "mix_lines"]
