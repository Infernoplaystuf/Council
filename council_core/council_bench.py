"""
council_core.council_bench — measure the whole council on a fixed set of
questions, so a change can be judged by numbers rather than by feel.

Every speed change trades against quality: answering a "quick" question with
one member, skipping the cross-fire when drafts agree, revising instead of
re-debating, thinking less on the Peasant's step. Whether each one is worth
it depends on your models and your machines, and only running the same
questions before and after can say.

A RUN asks every question in a set through the real council
(council_turn.run_turn — the same path as the Council tab, minus the vault
brief and the Analyst, so runs are comparable) and records per question:

    calls, model seconds and wall seconds      (turn_meter)
    model loads and loading seconds            (turn_meter)
    seconds per step — drafts, Peasant, rebuttals, cross-fire, ranking,
      Writer, critique                         (from the phase events)
    depth, verdict, Judge confidence
    correct?                                   (the question's checks)

to <vault>/.council_bench/<label>-<time>.jsonl, and a summary line per run
to <vault>/.council_bench/runs.jsonl. `compare` puts two runs side by side.

QUESTION SETS. `DEFAULT_SET` needs no files: arithmetic, units, short code,
general knowledge, planning — each with checks a script can apply
(must contain, must not contain, a number within a tolerance, Python that
parses). Your own questions, about your vault, go in
<vault>/.council_bench/questions.json in the same shape:

    [{"id": "seal", "text": "Which seal does the P-200 need?",
      "checks": [{"contains": ["EPDM"]}]}]

Run it from the Council Map tab (Benchmark…) or:

    python -m council_core.council_bench run --label baseline
    python -m council_core.council_bench compare baseline after-depth

Nothing here talks to anything but your own models. It writes only into
.council_bench.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

DIR_NAME = ".council_bench"


# ============================================================
# Questions and checks
# ============================================================

@dataclass
class Question:
    id: str
    text: str
    kind: str = "general"
    checks: List[Dict[str, Any]] = field(default_factory=list)
    depth: str = "auto"


DEFAULT_SET: List[Question] = [
    Question("hello", "Hi! Thanks for the help yesterday.", "chat",
             [{"not_contains": ["```"]}]),
    Question("arith", "What is 17 multiplied by 23?", "math",
             [{"number": 391}]),
    Question("pct", "A batch of 1,250 parts had 35 rejects. What is the "
             "reject rate as a percentage?", "math",
             [{"number": 2.8, "tol": 0.05}]),
    Question("units", "How many millimetres are in 3.5 inches?", "math",
             [{"number": 88.9, "tol": 0.1}]),
    Question("temp", "Convert 212 degrees Fahrenheit to Celsius.", "math",
             [{"number": 100}]),
    Question("days", "How many days are there from 2026-01-01 to "
             "2026-03-01?", "math", [{"number": 59}]),
    Question("capital", "What is the capital of Australia?", "fact",
             [{"contains": ["Canberra"]}]),
    Question("boil", "At sea level, at what temperature in Celsius does "
             "water boil?", "fact", [{"number": 100}]),
    Question("planets", "How many planets are in our solar system?", "fact",
             [{"number": 8}]),
    Question("pid", "In a PID controller, what does the I term respond "
             "to?", "fact",
             [{"any": ["accumulated", "integral", "sum of", "over time",
                       "past error"]}]),
    Question("ohm", "A 12 V supply drives a 4 ohm resistor. What current "
             "flows, in amps?", "math", [{"number": 3}]),
    Question("py_rev", "Write a Python function reverse_words(s) that "
             "returns the words of s in reverse order.", "code",
             [{"python_parses": True}, {"contains": ["def reverse_words"]}]),
    Question("py_median", "Write a Python function median(values) that "
             "returns the median of a non-empty list of numbers, without "
             "using the statistics module.", "code",
             [{"python_parses": True}, {"contains": ["def median"]},
              {"not_contains": ["import statistics"]}]),
    Question("py_bug", "What is wrong with this Python?\n```python\n"
             "def mean(xs):\n    return sum(xs) / len(x)\n```", "code",
             [{"any": ["len(xs)", "NameError", "undefined", "not defined",
                       "typo"]}]),
    Question("sql", "Write a SQL query that counts rows per status in a "
             "table called orders.", "code",
             [{"contains": ["GROUP BY"]}, {"any": ["COUNT(", "count("]}]),
    Question("plan", "Plan the steps to move a small team's shared files "
             "from a USB drive to a NAS without losing anything.", "plan",
             [{"any": ["backup", "back up", "copy"]},
              {"any": ["verify", "check", "checksum", "compare"]}]),
    Question("tradeoff", "Compare SQLite and PostgreSQL for a single-user "
             "desktop app. Which would you pick and why?", "plan",
             [{"contains": ["SQLite"]}]),
    Question("explain", "Explain in two or three sentences what a "
             "checksum is for.", "fact",
             [{"any": ["corrupt", "integrity", "error", "changed",
                       "detect"]}]),
    Question("trick", "How many times does the letter r appear in the word "
             "strawberry?", "fact", [{"number": 3}]),
    Question("negate", "Without writing any code, describe what a hash "
             "map is.", "chat", [{"not_contains": ["```"]}]),
]


_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_FENCE = re.compile(r"```(?:python|py)?[^\n]*\n(.*?)```", re.DOTALL)


def check(answer: str, checks: Sequence[Dict[str, Any]]) -> List[str]:
    """The checks `answer` fails ("" list = correct)."""
    text = answer or ""
    low = text.lower()
    failed = []
    for c in checks:
        if "contains" in c:
            miss = [w for w in c["contains"] if w.lower() not in low]
            if miss:
                failed.append(f"missing {miss}")
        if "any" in c and not any(w.lower() in low for w in c["any"]):
            failed.append(f"none of {c['any']}")
        if "not_contains" in c:
            bad = [w for w in c["not_contains"] if w.lower() in low]
            if bad:
                failed.append(f"contains {bad}")
        if "regex" in c and not re.search(c["regex"], text,
                                          re.IGNORECASE | re.MULTILINE):
            failed.append(f"no match for {c['regex']!r}")
        if "number" in c:
            want = float(c["number"])
            tol = float(c.get("tol", 1e-9))
            nums = []
            for raw in _NUM.findall(text):
                try:
                    nums.append(float(raw.replace(",", "")))
                except ValueError:
                    pass
            if not any(abs(n - want) <= tol for n in nums):
                failed.append(f"no {want:g}")
        if c.get("python_parses"):
            blocks = _FENCE.findall(text)
            if not blocks:
                failed.append("no Python block")
            else:
                import ast
                for b in blocks:
                    try:
                        ast.parse(b)
                    except SyntaxError as exc:
                        failed.append(f"Python does not parse: {exc.msg}")
                        break
    return failed


def bench_dir(vault_dir: Path) -> Path:
    return Path(vault_dir) / DIR_NAME


def load_questions(vault_dir: Path, which: str = "default") -> List[Question]:
    """"default", "vault" (questions.json in the bench folder) or "all"."""
    out: List[Question] = []
    if which in ("default", "all"):
        out += DEFAULT_SET
    if which in ("vault", "all"):
        path = bench_dir(vault_dir) / "questions.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        for i, q in enumerate(raw if isinstance(raw, list) else []):
            if isinstance(q, dict) and q.get("text"):
                out.append(Question(str(q.get("id") or f"vault{i + 1}"),
                                    str(q["text"]), str(q.get("kind") or
                                                        "vault"),
                                    list(q.get("checks") or []),
                                    str(q.get("depth") or "auto")))
    return out


# ============================================================
# A run
# ============================================================

#: Phase text -> step, for the per-step seconds.
STEPS = (("drafting", "draft"), ("cross-examining", "peasant"),
         ("questions on cross-fire", "peasant"), ("rebuttal", "rebuttal"),
         ("cross-fire", "cross_fire"), ("ranking", "ranking"),
         ("synthesizing", "writer"), ("revising", "writer"),
         ("critiquing", "critique"), ("adversarial", "peasant"))


def step_of(phase: str) -> str:
    low = phase.lower()
    for key, step in STEPS:
        if key in low:
            return step
    return "other"


@dataclass
class Outcome:
    id: str
    kind: str
    ok: bool
    correct: bool
    failed: List[str]
    depth: str = ""
    verdict: str = ""
    confidence: int = 0
    calls: int = 0
    wall: float = 0.0
    model_s: float = 0.0
    loads: int = 0
    load_s: float = 0.0
    steps: Dict[str, float] = field(default_factory=dict)
    error: str = ""


def run_one(q: Question, models: Any, *, judge: Any = None,
            depth: Optional[str] = None,
            turn: Optional[Callable[..., Any]] = None) -> Outcome:
    """One question through the council, measured."""
    if turn is None:
        from .council_turn import run_turn as turn
    marks: List[tuple] = []

    def on_event(ev: Any) -> None:
        if getattr(ev, "kind", "") == "phase":
            marks.append((time.monotonic(), step_of(ev.text)))

    t0 = time.monotonic()
    try:
        res = turn(q.text, models, judge=judge, on_event=on_event,
                   depth=depth or q.depth)
    except Exception as exc:                              # noqa: BLE001
        return Outcome(q.id, q.kind, False, False, ["crashed"],
                       error=repr(exc), wall=time.monotonic() - t0)
    t1 = time.monotonic()
    steps: Dict[str, float] = {}
    for (ta, step), (tb, _n) in zip(marks, marks[1:] + [(t1, "")]):
        steps[step] = round(steps.get(step, 0.0) + (tb - ta), 2)
    meter = getattr(res, "meter", {}) or {}
    answer = getattr(res, "answer", "") or ""
    failed = check(answer, q.checks) if getattr(res, "ok", False) \
        else ["no answer"]
    return Outcome(
        q.id, q.kind, bool(getattr(res, "ok", False)), not failed, failed,
        depth=getattr(res, "depth", ""), verdict=getattr(res, "verdict", ""),
        confidence=int(getattr(res, "confidence", 0) or 0),
        calls=int(meter.get("calls") or 0), wall=round(t1 - t0, 2),
        model_s=float(meter.get("seconds") or 0.0),
        loads=int(meter.get("loads") or 0),
        load_s=float(meter.get("load_s") or 0.0), steps=steps,
        error=getattr(res, "message", "") if not getattr(res, "ok", False)
        else "")


def summarise(outcomes: Sequence[Outcome]) -> Dict[str, Any]:
    n = len(outcomes) or 1
    steps: Dict[str, float] = {}
    for o in outcomes:
        for k, v in o.steps.items():
            steps[k] = round(steps.get(k, 0.0) + v, 1)
    by_depth: Dict[str, int] = {}
    for o in outcomes:
        by_depth[o.depth or "?"] = by_depth.get(o.depth or "?", 0) + 1
    return {
        "questions": len(outcomes),
        "correct": sum(o.correct for o in outcomes),
        "accuracy": round(sum(o.correct for o in outcomes) / n, 3),
        "passed": sum(o.verdict == "PASS" for o in outcomes),
        "errors": sum(not o.ok for o in outcomes),
        "mean_calls": round(sum(o.calls for o in outcomes) / n, 1),
        "mean_wall_s": round(sum(o.wall for o in outcomes) / n, 1),
        "total_wall_s": round(sum(o.wall for o in outcomes), 1),
        "loads": sum(o.loads for o in outcomes),
        "load_s": round(sum(o.load_s for o in outcomes), 1),
        "steps_s": steps, "depths": by_depth,
    }


def run(questions: Sequence[Question], models: Any, vault_dir: Path, *,
        label: str = "run", judge: Any = None, depth: Optional[str] = None,
        progress: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        turn: Optional[Callable[..., Any]] = None,
        now: Optional[float] = None) -> Dict[str, Any]:
    """Ask every question, write the results, return the run's summary."""
    label = re.sub(r"[^\w.-]+", "_", label.strip() or "run")[:40]
    stamp = time.strftime("%Y%m%d-%H%M%S",
                          time.localtime(time.time() if now is None else now))
    folder = bench_dir(vault_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{label}-{stamp}.jsonl"
    outcomes: List[Outcome] = []
    for i, q in enumerate(questions, 1):
        if should_stop is not None and should_stop():
            break
        o = run_one(q, models, judge=judge, depth=depth, turn=turn)
        outcomes.append(o)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(o), ensure_ascii=False) + "\n")
        if progress is not None:
            mark = "✓" if o.correct else "✗"
            progress(f"[{i}/{len(questions)}] {mark} {q.id}: {o.calls} calls, "
                     f"{o.wall:.0f} s, {o.depth or '?'}, {o.verdict or '-'}"
                     + (f" — {'; '.join(o.failed)}" if o.failed else ""))
    summary = dict(summarise(outcomes), label=label, stamp=stamp,
                   file=path.name, depth=depth or "per question")
    with (folder / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary


def runs(vault_dir: Path) -> List[Dict[str, Any]]:
    try:
        lines = (bench_dir(vault_dir) / "runs.jsonl").read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for raw in lines:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def find_run(vault_dir: Path, label: str) -> Optional[Dict[str, Any]]:
    """The latest run with this label (or this file name)."""
    for r in reversed(runs(vault_dir)):
        if label in (r.get("label"), r.get("file"), r.get("stamp")):
            return r
    return None


def compare(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    """Two runs side by side."""
    def row(name: str, key: str, fmt: str = "{:g}", better: str = "low"):
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            return f"  {name:<22} {va!s:>10} {vb!s:>10}"
        d = vb - va
        arrow = "" if not d else ("better" if (d < 0) == (better == "low")
                                  else "worse")
        return (f"  {name:<22} {fmt.format(va):>10} {fmt.format(vb):>10}"
                f"   {d:+g} {arrow}")
    lines = [f"  {'':<22} {a.get('label', 'A'):>10} {b.get('label', 'B'):>10}",
             row("questions", "questions"),
             row("correct", "correct", better="high"),
             row("accuracy", "accuracy", "{:.0%}", better="high"),
             row("Judge PASS", "passed", better="high"),
             row("errors", "errors"),
             row("mean calls", "mean_calls"),
             row("mean seconds", "mean_wall_s"),
             row("total seconds", "total_wall_s"),
             row("model loads", "loads"),
             row("loading seconds", "load_s")]
    steps = sorted(set(a.get("steps_s", {})) | set(b.get("steps_s", {})))
    if steps:
        lines.append("  seconds per step:")
        for s in steps:
            va = a.get("steps_s", {}).get(s, 0.0)
            vb = b.get("steps_s", {}).get(s, 0.0)
            lines.append(f"    {s:<20} {va:>10g} {vb:>10g}   {vb - va:+g}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="council_bench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--label", default="run")
    r.add_argument("--set", default="default",
                   choices=("default", "vault", "all"))
    r.add_argument("--depth", default=None,
                   choices=(None, "auto", "quick", "standard", "deep"))
    r.add_argument("--vault", default=None)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--vault", default=None)
    args = ap.parse_args(argv)
    from . import paths
    vault = Path(args.vault) if args.vault else paths.vault_dir()
    if args.cmd == "compare":
        ra, rb = find_run(vault, args.a), find_run(vault, args.b)
        if ra is None or rb is None:
            print("No such run:", args.a if ra is None else args.b)
            return 2
        print(compare(ra, rb))
        return 0
    from .council_turn import load_personalities
    models, problem = load_personalities(vault, session_id="bench")
    if models is None:
        print("Could not load the council:", problem)
        return 2
    summary = run(load_questions(vault, args.set), models, vault,
                  label=args.label, depth=args.depth, progress=print)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["Question", "DEFAULT_SET", "check", "load_questions", "run_one",
           "run", "summarise", "runs", "find_run", "compare", "Outcome"]
