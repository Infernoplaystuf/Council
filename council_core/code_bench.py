"""
council_core.code_bench — measure the council's coding on the project's
own history: can it redo changes you already made?

A past commit that changed code AND its tests is a ready-made task with an
answer key:

  1. a worktree is made at that commit, and the commit's CODE changes are
     undone (its test changes kept) — committed as the task's starting
     point;
  2. the commit's tests are run there: they must FAIL (if they pass
     without the change, the task proves nothing and is skipped);
  3. the council gets the commit message as the task, plans (approved
     automatically) and works — council_core.code_agent, exactly as a real
     job;
  4. the commit's tests are run again: SOLVED when they pass.

Recorded per task: solved, the plan's steps and how many were done, model
calls, seconds, and the files touched; per run a summary line in
<vault>/.council_bench/code-runs.jsonl. Every worktree and branch made is
removed afterwards; the project folder is never touched.

Pick small commits (MAX_CODE_LINES changed in non-test files by default):
the point is a repeatable measure, and small tasks are what local models
should get right first.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import code_agent as ca
from . import project as pj
from . import worktree as wt

MAX_CODE_LINES = 200
TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")
CODE_SUFFIXES = (".py",)


def is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path))


@dataclass
class Task:
    commit: str
    parent: str
    message: str
    code_files: List[str]
    test_files: List[str]
    code_lines: int


def candidates(repo: Path, *, limit: int = 200,
               max_code_lines: int = MAX_CODE_LINES) -> List[Task]:
    """Recent non-merge commits that changed both code and tests."""
    repo = wt.require_repo(repo)
    log = wt._git(["log", f"-{limit}", "--no-merges", "--format=%H %P"],
                  repo).stdout.splitlines()
    out: List[Task] = []
    for line in log:
        parts = line.split()
        if len(parts) != 2:
            continue                      # root commit or a merge
        sha, parent = parts
        stat = wt._git(["diff", "--numstat", parent, sha], repo).stdout
        code, tests, lines = [], [], 0
        for row in stat.splitlines():
            cols = row.split("\t")
            if len(cols) != 3 or cols[0] == "-":
                continue
            path = cols[2]
            if not path.endswith(CODE_SUFFIXES):
                continue
            if is_test(path):
                tests.append(path)
            else:
                code.append(path)
                lines += int(cols[0]) + int(cols[1])
        if code and tests and lines <= max_code_lines:
            msg = wt._git(["log", "-1", "--format=%B", sha], repo).stdout
            out.append(Task(sha, parent, msg.strip(), code, tests, lines))
    return out


def prepare(repo: Path, where: Path, task: Task) -> wt.Job:
    """A worktree at the commit with its code changes undone (tests kept),
    committed as the start."""
    job = wt.create(repo, where, "bench-" + task.commit[:10],
                    base=task.commit)
    for path in task.code_files:
        existed = wt._git(["cat-file", "-e", f"{task.parent}:{path}"],
                          job.path, check=False).returncode == 0
        if existed:
            job.git("checkout", task.parent, "--", path)
        else:
            job.git("rm", "-q", "--", path)
    job.git("commit", "-q", "-am", "bench: the change, undone")
    job.base = job.git("rev-parse", "HEAD").stdout.strip()
    return job


@dataclass
class Outcome:
    commit: str
    task: str
    valid: bool = True
    solved: bool = False
    steps: int = 0
    steps_done: int = 0
    calls: int = 0
    seconds: float = 0.0
    touched: List[str] = field(default_factory=list)
    detail: str = ""


def _tests_pass(ws, files: Sequence[str]) -> bool:
    ok = True
    for f in files:
        if not (Path(ws.root) / f).exists():
            continue
        passed, _out = ws.run_tests(f)
        ok = ok and passed
    return ok


def run_task(vault_dir: Path, project: pj.Project, task: Task, *,
             chat: Optional[Callable] = None,
             chat_tools: Optional[Callable] = None,
             say: Callable[[str], None] = lambda s: None) -> Outcome:
    from .project_tools import Workspace
    from .turn_meter import TurnMeter
    where = pj.project_dir(vault_dir, project) / "bench"
    first = task.message.splitlines()[0] if task.message else task.commit
    out = Outcome(task.commit[:10], first)
    job = prepare(project.root, where, task)
    try:
        bench_project = pj.Project(project.name + " (bench)", str(job.path),
                                   test_command=project.test_command
                                   or "python -m pytest -q",
                                   gui_checks=project.gui_checks,
                                   references=project.references)
        ws = Workspace(job.path, test_command=bench_project.test_command)
        if _tests_pass(ws, task.test_files):
            out.valid = False
            out.detail = "its tests pass without the change; skipped"
            return out
        t0 = time.monotonic()
        with TurnMeter() as meter:
            plan = ca.make_plan(vault_dir, bench_project, task.message,
                                chat=chat)
            rec = ca.new_job(vault_dir, bench_project, task.message, plan)
            rec = ca.run_job(vault_dir, bench_project, rec, chat=chat,
                             chat_tools=chat_tools, job=job,
                             must_pass=task.test_files,
                             on_event=lambda k, t: say(f"  {k}: {t[:200]}")
                             if k in ("phase", "check") else None)
        out.seconds = round(time.monotonic() - t0, 1)
        out.calls = meter.result.calls
        steps = ca.plan_of(rec).steps
        out.steps, out.steps_done = len(steps), sum(s.status == "done"
                                                   for s in steps)
        out.touched = job.changed_files()
        job.reset_uncommitted()
        out.solved = _tests_pass(ws, task.test_files)
        out.detail = rec.status
        return out
    except Exception as exc:                              # noqa: BLE001
        out.detail = f"error: {exc!r}"
        return out
    finally:
        wt.discard(job)
        shutil.rmtree(pj.project_dir(vault_dir, pj.Project(
            project.name + " (bench)", "")), ignore_errors=True)


def run(vault_dir: Path, project: pj.Project, *, label: str = "code",
        count: int = 5, tasks: Optional[Sequence[Task]] = None,
        chat: Optional[Callable] = None,
        chat_tools: Optional[Callable] = None,
        say: Callable[[str], None] = print,
        should_stop: Callable[[], bool] = lambda: False) -> Dict[str, Any]:
    """Run `count` history tasks; write results; return the summary."""
    tasks = list(tasks) if tasks is not None else \
        candidates(project.root)[:count]
    from .council_bench import bench_dir
    folder = bench_dir(vault_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = folder / f"code-{re.sub(r'[^\w.-]+', '_', label)}-{stamp}.jsonl"
    outcomes: List[Outcome] = []
    for i, t in enumerate(tasks, 1):
        if should_stop():
            break
        say(f"[{i}/{len(tasks)}] {t.commit[:10]} {t.message.splitlines()[0][:70]}")
        o = run_task(vault_dir, project, t, chat=chat, chat_tools=chat_tools,
                     say=say)
        outcomes.append(o)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(o)) + "\n")
        say(f"    {'SOLVED' if o.solved else 'not solved'} — {o.detail}; "
            f"{o.steps_done}/{o.steps} steps, {o.calls} calls, {o.seconds} s")
    valid = [o for o in outcomes if o.valid]
    n = len(valid) or 1
    summary = {"label": label, "stamp": stamp, "file": path.name,
               "tasks": len(outcomes), "valid": len(valid),
               "solved": sum(o.solved for o in valid),
               "solve_rate": round(sum(o.solved for o in valid) / n, 3),
               "mean_calls": round(sum(o.calls for o in valid) / n, 1),
               "mean_seconds": round(sum(o.seconds for o in valid) / n, 1)}
    with (folder / "code-runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary) + "\n")
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    """python -m council_core.code_bench PROJECT [--count 5] [--label x]"""
    import argparse
    ap = argparse.ArgumentParser(prog="code_bench")
    ap.add_argument("project")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--label", default="code")
    ap.add_argument("--vault", default=None)
    args = ap.parse_args(argv)
    from . import paths
    vault = Path(args.vault) if args.vault else paths.vault_dir()
    project = pj.load(vault, args.project)
    if project is None:
        print(f"No project named {args.project!r} (make it in the Code tab).")
        return 2
    print(json.dumps(run(vault, project, label=args.label, count=args.count),
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["Task", "Outcome", "candidates", "prepare", "run_task", "run",
           "is_test", "main"]
