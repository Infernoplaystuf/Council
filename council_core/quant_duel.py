"""
council_core.quant_duel — the Council runs the Quant Duel on its nodes.

The Quant Duel (its own checkout; branch `quant-duel` of this repository) is a
paper-trading experiment: node A reads news, node B never does, and a month
of daily predictions says whether news helped. It runs as its OWN processes —
the Council never imports it — through its command line:

    run-due --plan-json   which jobs are due now for a node
    <job>                 daily · news-poll · news-score · tune · report
    status --json         a node's books, last tuning round, news counts

WHERE ITS LLM WORK RUNS — THE NODE SYSTEM
The duel's LLM jobs (news scoring, tuning, report summaries) are sent where
Machines & roles binds the `quant` role (node_routing.SERVICE_ROLES), exactly
like a council role: routing on, the machine enabled, a binding. Then:
  * the job is told the machine's Ollama address (QUANT_DUEL_LLM_URL, and
    QUANT_DUEL_LLM_ALLOW_REMOTE because the Council vouches for it — the same
    rule as the engine's `is_allowed_host`);
  * it holds one of that machine's `parallel` call slots (host_slot) while
    it runs, so a Pi never gets more calls than it is set up for;
  * the job's last line, `QD-LLM: ok|failed …`, rests a failing machine
    (mark_failed: 30 s doubling to 5 min) or clears it (mark_ok);
  * while the machine rests, the job runs on this PC (fallback "here") or
    is skipped (fallback "fail").
Unbound, the duel uses the LLM in its own config.yaml (this PC's Ollama by
default) and holds no Council slot: a weekly tuning round spends minutes on
backtests, and holding this PC's single slot that long would freeze the
council's own chat.

OPT-IN
Nothing runs until the user turns it on (quant_duel.json in the vault). Once
on, the duel fetches daily prices from Yahoo Finance and — node A only —
headlines from RSS feeds; that is the only traffic it adds. Paper trading
only: the duel has no way to place an order.

No Qt here; a runner is injectable so tests never start a process.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import node_routing as nr

FILE_NAME = "quant_duel.json"
ROLE = "quant"
NODES = ("A", "B")
DISCLOSURE = (
    "When on, the duel fetches daily prices from Yahoo Finance and (node A) "
    "news headlines from RSS feeds. Its LLM runs on this PC, or on the "
    "machine Machines & roles binds to the 'quant' role. Paper trading "
    "only — it cannot place orders.")

#: Per-job time limits (seconds).
TIMEOUTS = {"daily": 90 * 60, "news-poll": 40 * 60, "news-score": 40 * 60,
            "tune": 3 * 3600, "report": 30 * 60, "status": 120,
            "run-due": 120, "check": 300, "compare": 1800,
            "replay": 6 * 3600}

#: (cmd, env, timeout, cwd) -> (returncode, combined output)
Runner = Callable[[List[str], Dict[str, str], int, str], Tuple[int, str]]


class QuantDuelError(RuntimeError):
    pass


# ============================================================
# Settings
# ============================================================

@dataclass
class Settings:
    enabled: bool = False
    root: str = ""                      # the quant-duel checkout
    python: str = ""                    # its interpreter; "" = find one
    nodes: List[str] = field(default_factory=lambda: list(NODES))
    schedule: bool = False              # run due jobs while the app is open

    def to_json(self) -> dict:
        return asdict(self)


def path_for(vault_dir: Path) -> Path:
    return Path(vault_dir) / FILE_NAME


def load(vault_dir: Path) -> Settings:
    try:
        data = json.loads(path_for(vault_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    nodes = [n for n in data.get("nodes", NODES) if n in NODES] or list(NODES)
    return Settings(enabled=bool(data.get("enabled", False)),
                    root=str(data.get("root", "") or ""),
                    python=str(data.get("python", "") or ""),
                    nodes=nodes, schedule=bool(data.get("schedule", False)))


def save(vault_dir: Path, settings: Settings) -> Path:
    path = path_for(vault_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings.to_json(), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def python_for(settings: Settings) -> str:
    """The checkout's own venv if it has one (its packages, not ours)."""
    if settings.python:
        return settings.python
    root = Path(settings.root)
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe",
                "venv/bin/python", "venv/Scripts/python.exe"):
        if (root / rel).exists():
            return str(root / rel)
    return sys.executable


def problem(settings: Settings) -> str:
    """Why the duel cannot run, or "" when it can."""
    if not settings.root:
        return "Choose the Quant Duel folder (a checkout of its branch)."
    root = Path(settings.root)
    if not (root / "quant_duel" / "cli.py").exists():
        return f"{root} is not a Quant Duel checkout (no quant_duel/cli.py)."
    if not (root / "config.yaml").exists():
        return f"{root} has no config.yaml."
    py = python_for(settings)
    if os.path.isabs(py) and not Path(py).exists():
        return f"The Python interpreter {py} does not exist."
    return ""


# ============================================================
# Where the LLM work goes
# ============================================================

@dataclass(frozen=True)
class Endpoint:
    machine: str                        # what to show: "this PC" or a name
    url: Optional[str] = None           # Ollama base for the job, None = own
    gate: str = ""                      # node URL whose slot is held
    skip: str = ""                      # why the LLM job must not run


def endpoint(routing: Optional[nr.Routing] = None) -> Endpoint:
    target = nr.route(ROLE, routing)
    if target is None:
        return Endpoint("this PC")
    rest = nr.cooling(target.url)
    if rest > 0:
        if target.fallback == "fail":
            return Endpoint(target.node, skip=f"{target.node} is resting for "
                            f"{rest:.0f}s after a failure (fallback: fail)")
        return Endpoint(f"this PC ({target.node} resting {rest:.0f}s)")
    return Endpoint(target.node, url=target.url.rstrip("/") + "/v1",
                    gate=target.url)


def job_env(ep: Endpoint, base: Optional[Dict[str, str]] = None
            ) -> Dict[str, str]:
    env = dict(os.environ if base is None else base)
    for k in ("QUANT_DUEL_LLM_URL", "QUANT_DUEL_LLM_ALLOW_REMOTE"):
        env.pop(k, None)
    if ep.url:
        env["QUANT_DUEL_LLM_URL"] = ep.url
        env["QUANT_DUEL_LLM_ALLOW_REMOTE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def llm_state(output: str) -> Optional[str]:
    """'ok' / 'failed' from the job's QD-LLM line, None if it used no LLM."""
    for line in reversed((output or "").splitlines()):
        if line.startswith("QD-LLM: "):
            return line.split()[1]
    return None


# ============================================================
# Running
# ============================================================

def _subprocess(cmd: List[str], env: Dict[str, str], timeout: int,
                cwd: str) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, env=env, cwd=cwd or None, capture_output=True,
                           text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, f"{out}\n(timed out after {timeout}s)"
    except OSError as exc:
        return 127, f"could not start: {exc}"


def command(settings: Settings, node: Optional[str],
            args: Sequence[str]) -> List[str]:
    cmd = [python_for(settings), "-m", "quant_duel.cli", "--root",
           str(Path(settings.root))]
    if node:
        cmd += ["--node", node]
    return cmd + list(args)


@dataclass
class JobResult:
    node: Optional[str]
    args: List[str]
    rc: Optional[int]
    output: str = ""
    machine: str = "this PC"
    llm: Optional[str] = None           # ok / failed / None
    skipped: str = ""

    @property
    def ok(self) -> bool:
        return not self.skipped and self.rc == 0

    def line(self) -> str:
        who = f"node {self.node}" if self.node else "duel"
        what = " ".join(self.args)
        if self.skipped:
            return f"{who}: {what} — skipped: {self.skipped}"
        llm = f", LLM {self.llm} on {self.machine}" if self.llm else ""
        return f"{who}: {what} — rc={self.rc}{llm}"


def run(settings: Settings, node: Optional[str], args: Sequence[str], *,
        llm: bool = False, runner: Optional[Runner] = None,
        routing: Optional[nr.Routing] = None) -> JobResult:
    """Run one duel command. An LLM job is routed and gated as above.
    Never raises."""
    why = problem(settings)
    if why:
        return JobResult(node, list(args), None, skipped=why)
    ep = endpoint(routing) if llm else Endpoint("this PC")
    if ep.skip:
        return JobResult(node, list(args), None, machine=ep.machine,
                         skipped=ep.skip)
    cmd = command(settings, node, args)
    timeout = TIMEOUTS.get(args[0] if args else "", 3600)
    go = runner or _subprocess
    if ep.gate:
        with nr.host_slot(ep.gate, routing):
            rc, out = go(cmd, job_env(ep), timeout, settings.root)
    else:
        rc, out = go(cmd, job_env(ep), timeout, settings.root)
    state = llm_state(out)
    if ep.gate and state == "failed":
        nr.mark_failed(ep.gate)
    elif ep.gate and state == "ok":
        nr.mark_ok(ep.gate)
    return JobResult(node, list(args), rc, out, ep.machine, state)


def _last_json(output: str) -> Any:
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if line.startswith(("[", "{")):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise QuantDuelError("the duel printed no JSON:\n" + (output or "")[-600:])


def due(settings: Settings, node: str, runner: Optional[Runner] = None
        ) -> List[Dict[str, Any]]:
    """The jobs due now for ``node`` (the duel's own schedule)."""
    r = run(settings, node, ["run-due", "--plan-json"], runner=runner)
    if not r.ok:
        raise QuantDuelError(r.skipped or r.output[-600:])
    jobs = _last_json(r.output)
    if not isinstance(jobs, list):
        raise QuantDuelError("run-due --plan-json did not return a list")
    return jobs


def steps(job: Dict[str, Any]) -> List[Tuple[List[str], bool]]:
    """How the Council runs one due job: (args, uses the LLM). A news poll
    is split so only its scoring holds a machine slot, not the fetching."""
    name, args = job["job"], list(job.get("args") or [job["job"]])
    if name == "news-poll":
        return [(["news-poll", "--no-score"], False), (["news-score"], True)]
    return [(args, name in ("tune", "report"))]


def tick(settings: Settings, runner: Optional[Runner] = None,
         routing: Optional[nr.Routing] = None,
         log: Optional[Callable[[str], None]] = None) -> List[JobResult]:
    """Run whatever is due on every node. Never raises."""
    say = log or (lambda _m: None)
    if not settings.enabled:
        return []
    why = problem(settings)
    if why:
        say(why)
        return []
    out: List[JobResult] = []
    for node in settings.nodes:
        try:
            jobs = due(settings, node, runner)
        except QuantDuelError as exc:
            say(f"node {node}: could not plan: {exc}")
            continue
        for job in jobs:
            stop = False
            for args, llm in steps(job):
                r = run(settings, node, args, llm=llm, runner=runner,
                        routing=routing)
                out.append(r)
                say(r.line())
                if job["job"] == "daily" and not r.ok:
                    stop = True          # no report on a failed day
            if stop:
                break
    return out


def status(settings: Settings, node: str, runner: Optional[Runner] = None
           ) -> Dict[str, Any]:
    r = run(settings, node, ["status", "--json"], runner=runner)
    if not r.ok:
        raise QuantDuelError(r.skipped or r.output[-600:])
    st = _last_json(r.output)
    if not isinstance(st, dict):
        raise QuantDuelError("status --json did not return an object")
    return st


def status_rows(statuses: Dict[str, Any]) -> List[Tuple[str, ...]]:
    """(node, last day, live, control, SPY, last tuning, news, due) — the
    tab's table, already in words. A node whose status failed says why."""
    rows = []
    for node, st in statuses.items():
        if isinstance(st, Exception) or not isinstance(st, dict):
            rows.append((node, "—", "—", "—", "—", "—", "—",
                         f"error: {st}"))
            continue

        def book(name):
            b = (st.get("books") or {}).get(name)
            return "—" if not b else (f"{b['equity']:,.0f} "
                                      f"({b['total_pct']:+.2f}%)")
        t = st.get("tuner_last") or {}
        n = st.get("news")
        rows.append((
            f"{node} ({'news' if st.get('news_enabled') else 'no news'})",
            st.get("last_run") or "not started", book("live"),
            book("control"), book("spy"),
            f"{t['day']} {t['status']}" if t else "—",
            f"{n['headlines']} / {n['scored']} scored" if n else "—",
            ", ".join(st.get("due") or []) or "nothing"))
    return rows


def where_llm_runs(routing: Optional[nr.Routing] = None) -> str:
    """One sentence for the tab."""
    ep = endpoint(routing)
    if ep.skip:
        return f"LLM jobs are paused: {ep.skip}."
    if ep.url:
        return (f"LLM jobs run on {ep.machine} ({ep.url}), holding one of its "
                "call slots while they run.")
    return (f"LLM jobs run on {ep.machine}, with the model in the duel's "
            "config.yaml. Bind the 'quant' role in Machines & roles to send "
            "them to another machine.")


__all__ = ["FILE_NAME", "ROLE", "NODES", "DISCLOSURE", "Settings", "load",
           "save", "problem", "python_for", "Endpoint", "endpoint", "job_env",
           "llm_state", "command", "JobResult", "run", "due", "steps", "tick",
           "status", "status_rows", "where_llm_runs", "QuantDuelError"]
