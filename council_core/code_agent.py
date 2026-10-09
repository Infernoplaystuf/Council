"""
council_core.code_agent — the council develops a code project the way a
careful engineer does: plan, agree the plan, small steps, each one checked
and committed, notes for whoever picks it up next.

    1. PLAN      The planner (fanout.planner_role: the "planner" model, else
                 the Judge's) reads the project's brief, its code map, the
                 references that match the task and the last job's handoff
                 note, and writes a short plan: the goal and 1–8 steps,
                 each with the files it expects to touch, how it will be
                 checked (tests, GUI, both, or by reading), and whether it
                 starts with a failing test. The USER approves the plan —
                 and may edit it — before anything runs.
    2. WORK      A job gets its own git worktree and branch (council_core.
                 worktree). The coder works one step at a time with the
                 project tools (council_core.project_tools: read, find,
                 search, edit by exact replacement, create, run tests,
                 build the GUI offscreen), in one running conversation per
                 step, until it calls step_done.
    3. CHECK     After step_done the app runs the step's check itself: the
                 project's tests (a failure counts only if it did NOT fail
                 before the job — the baseline — so a project with known
                 failures can still be worked on) and the GUI checks. A
                 failed check goes back to the coder, up to MAX_FIX_ROUNDS.
    4. COMMIT    A step that passes is committed on the job's branch with its
                 summary. A step that cannot pass is reset and the job stops
                 there: later steps build on it.
    5. NOTES     The job ends with a handoff note (what was done, what is
                 left, decisions, problems), saved in the project's notes/
                 and read by the next job; and, when the coder learned
                 something future jobs need, a PROPOSED addition to the
                 project brief for the user to accept.

The user merges the branch (worktree.merge) or discards it. Nothing in the
project folder changes until they do.

TEST FIRST. For a feature or a bug fix in a project with tests, the
planner marks the step test_first and the coder is told to write the test,
run it and see it fail, then make it pass. A test that already fails is a
target a small model can iterate on; "make it better" is not.

Model calls go through council_engine.chat_tools (native tool calling on an
Ollama model with the "tools" capability, a schema-constrained emulation
elsewhere) and council_engine.local_chat; both are injectable for tests.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import code_map as cm
from . import project as pj
from . import worktree as wt
from .project_tools import Workspace

MAX_STEPS = 8
MAX_TURNS = 30              # model turns per step
MAX_FIX_ROUNDS = 3          # failed checks sent back per step
TOOL_RESULT_CHARS = 6000
NOTE_CHARS = 3000

CHECKS = ("tests", "gui", "tests+gui", "none")

PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "steps": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "check": {"type": "string", "enum": list(CHECKS)},
                "test_first": {"type": "boolean"},
                "details": {"type": "string"}},
            "required": ["title", "files", "check", "details"]}},
        "risks": {"type": "string"},
    },
    "required": ["goal", "steps"],
}

PLAN_INSTRUCTIONS = """You plan changes to a code project for a coding agent \
that works one small step at a time. Read the brief, the code map, the \
references and the last handoff note.

Rules:
- 1 to {max_steps} steps, in order. Each step is small: one idea, a few \
files, finished and checked before the next starts.
- "files": the files the step will change or create, as the code map \
writes them.
- "check": how the step is proved: "tests" (the project's tests), "gui" \
(build the changed widget offscreen), "tests+gui", or "none" (only when \
nothing can be run, e.g. documentation).
- "test_first": true for a feature or a bug fix when the project has \
tests — the agent writes a failing test first, then makes it pass.
- "details": exactly what to do, naming the real classes and functions \
from the code map. Follow the brief's rules.
Reply with JSON only."""

CODER_SYSTEM = """You are a careful software engineer working on ONE step \
of a plan, in your own copy of the project. Use the tools; do not answer \
in prose until the step is done.

How to work:
1. Look before you change: find_symbol / search_code / read_file the code \
you will touch.
2. Change code with edit_file: "old" must be text copied exactly from \
read_file (without the line numbers) and must occur once. Keep edits \
small. create_file only for new files.
3. Check your work: run_tests, and gui_check for widgets. Fix what fails.
4. When the step is finished and its check passes, call step_done with a \
one-line summary.
Follow the project brief's rules. Do not change files the step does not \
need. Never delete tests to make them pass."""

TEST_FIRST = """THIS STEP IS TEST-FIRST: first write (or extend) a test \
that shows the required behaviour, run it with run_tests and see it FAIL, \
then change the code until it passes."""


@dataclass
class Step:
    title: str
    files: List[str] = field(default_factory=list)
    check: str = "tests"
    test_first: bool = False
    details: str = ""
    status: str = "pending"        # pending / done / failed / skipped
    summary: str = ""
    commit: str = ""


@dataclass
class Plan:
    goal: str
    steps: List[Step]
    risks: str = ""

    def text(self) -> str:
        lines = [f"GOAL: {self.goal}"]
        for i, s in enumerate(self.steps, 1):
            tf = " [test first]" if s.test_first else ""
            lines.append(f"{i}. {s.title} — check: {s.check}{tf}\n"
                         f"   files: {', '.join(s.files) or '?'}\n"
                         f"   {s.details}")
        if self.risks:
            lines.append(f"RISKS: {self.risks}")
        return "\n".join(lines)


def plan_from_json(obj: Dict[str, Any], max_steps: int = MAX_STEPS) -> Plan:
    steps = []
    for s in (obj.get("steps") or [])[:max_steps]:
        if not isinstance(s, dict) or not str(s.get("title", "")).strip():
            continue
        check = str(s.get("check") or "tests")
        steps.append(Step(str(s["title"]).strip(),
                          [str(f) for f in s.get("files") or []],
                          check if check in CHECKS else "tests",
                          bool(s.get("test_first")),
                          str(s.get("details") or "").strip()))
    if not steps:
        raise ValueError("the plan has no steps")
    return Plan(str(obj.get("goal") or "").strip(), steps,
                str(obj.get("risks") or "").strip())


# ============================================================
# Context: what every model call about this project starts from
# ============================================================

def latest_note(vault_dir: Path, project: pj.Project) -> str:
    notes = sorted((pj.project_dir(vault_dir, project) / "notes")
                   .glob("*.md")) if (pj.project_dir(vault_dir, project)
                                      / "notes").is_dir() else []
    if not notes:
        return ""
    return notes[-1].read_text(encoding="utf-8", errors="replace")[:NOTE_CHARS]


def context(vault_dir: Path, project: pj.Project, focus: str, *,
            root: Optional[Path] = None, map_chars: int = 6000) -> str:
    root = Path(root or project.root)
    parts = [pj.brief(vault_dir, project, root)]
    cmap = cm.for_project(root, pj.project_dir(vault_dir, project)
                          / "code_map.json")
    parts.append(cmap.render(focus=focus, max_chars=map_chars))
    if project.references:
        refs = pj.references(project).block(focus)
        if refs:
            parts.append(refs)
    note = latest_note(vault_dir, project)
    if note:
        parts.append("THE LAST JOB'S HANDOFF NOTE:\n" + note)
    return "\n\n".join(p for p in parts if p)


def _default_chat():
    import council_engine
    return council_engine.local_chat


def _default_chat_tools():
    import council_engine
    return council_engine.chat_tools


def make_plan(vault_dir: Path, project: pj.Project, task: str, *,
              chat: Optional[Callable[..., str]] = None,
              role: Optional[str] = None,
              max_steps: int = MAX_STEPS) -> Plan:
    """The planner's plan for `task` (to show the user, not to run yet)."""
    from .fanout import planner_role
    chat = chat or _default_chat()
    msgs = [{"role": "system",
             "content": PLAN_INSTRUCTIONS.format(max_steps=max_steps)},
            {"role": "user",
             "content": f"TASK:\n{task}\n\n"
                        + context(vault_dir, project, task)
                        + ("\n\nThe project has a test command."
                           if project.test_command else
                           "\n\nThe project has NO test command: use 'gui' "
                           "or 'none' checks.")}]
    raw = chat(msgs, role=role or planner_role(), json_schema=PLAN_SCHEMA,
               num_predict=2000, temperature=0.1, timeout=600)
    from .council_schemas import _parse
    obj = _parse(raw)
    if not isinstance(obj, dict):
        raise ValueError("the planner did not reply with a plan")
    plan = plan_from_json(obj, max_steps)
    if not project.test_command:
        for s in plan.steps:
            s.test_first = False
            if s.check in ("tests", "tests+gui"):
                s.check = "gui" if project.gui_checks else "none"
    return plan


# ============================================================
# Tests: what failed before the job is not this job's failure
# ============================================================

_FAIL_ID = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


def failing_ids(output: str) -> set:
    return {m.group(1) for m in _FAIL_ID.finditer(output or "")}


@dataclass
class Verdict:
    ok: bool
    text: str


def check_step(ws: Workspace, step: Step, baseline: Optional[set]) -> Verdict:
    """The step's check, run by the app (not the model's word for it)."""
    lines, ok = [], True
    if step.check in ("tests", "tests+gui") and ws.test_command:
        passed, out = ws.run_tests()
        if not passed:
            now = failing_ids(out)
            new = now - (baseline or set())
            if baseline is not None and now and not new:
                lines.append("Tests: only failures that were already failing "
                             f"before this job ({len(now)}).")
            else:
                ok = False
                lines.append("TESTS FAIL" + (
                    " — new failures: " + ", ".join(sorted(new)[:10])
                    if new else "") + "\n" + out[-3000:])
        else:
            lines.append("Tests pass.")
    if step.check in ("gui", "tests+gui"):
        results = ws.gui()
        for r in results:
            if not r.ok:
                ok = False
            lines.append(r.summary(2000))
        if not results:
            lines.append("No GUI check is set up for this project.")
    if step.check == "none":
        lines.append("No automatic check for this step.")
    return Verdict(ok, "\n\n".join(lines))


# ============================================================
# The job
# ============================================================

@dataclass
class JobRecord:
    id: str
    project: str
    task: str
    plan: Dict[str, Any]
    branch: str = ""
    base: str = ""
    worktree: str = ""
    status: str = "planned"   # planned / running / done / failed / stopped / merged / discarded
    started: float = 0.0
    finished: float = 0.0
    note: str = ""
    baseline: List[str] = field(default_factory=list)


def jobs_dir(vault_dir: Path, project: pj.Project) -> Path:
    return pj.project_dir(vault_dir, project) / "jobs"


def save_record(vault_dir: Path, project: pj.Project, rec: JobRecord) -> None:
    d = jobs_dir(vault_dir, project)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{rec.id}.json.tmp"
    tmp.write_text(json.dumps(asdict(rec), indent=2), encoding="utf-8")
    tmp.replace(d / f"{rec.id}.json")


def list_records(vault_dir: Path, project: pj.Project) -> List[JobRecord]:
    out = []
    for f in sorted(jobs_dir(vault_dir, project).glob("*.json")) \
            if jobs_dir(vault_dir, project).is_dir() else []:
        try:
            out.append(JobRecord(**json.loads(f.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    return out


def plan_of(rec: JobRecord) -> Plan:
    p = rec.plan
    return Plan(p.get("goal", ""), [Step(**s) for s in p.get("steps", [])],
                p.get("risks", ""))


def new_job(vault_dir: Path, project: pj.Project, task: str,
            plan: Plan) -> JobRecord:
    rec = JobRecord(wt.job_id(task), project.name, task, asdict(plan))
    save_record(vault_dir, project, rec)
    return rec


def _trim(messages: List[Dict[str, Any]], budget_chars: int) -> None:
    """Shorten the oldest tool results until the conversation fits."""
    def size():
        return sum(len(str(m.get("content") or "")) for m in messages)
    for m in messages[2:]:
        if size() <= budget_chars:
            return
        if m.get("role") == "tool" and len(str(m.get("content"))) > 200:
            m["content"] = str(m["content"])[:200] + " …[trimmed: read again " \
                "if needed]"


def _window_chars(role: str) -> int:
    try:
        import council_engine
        n = council_engine.effective_n_ctx(council_engine._slot_for_role(role))
    except Exception:                                     # noqa: BLE001
        n = 8192
    return int(max(4096, n - 2500) * 3.2)


def run_job(vault_dir: Path, project: pj.Project, rec: JobRecord, *,
            chat_tools: Optional[Callable[..., Dict[str, Any]]] = None,
            chat: Optional[Callable[..., str]] = None,
            role: str = "coder",
            on_event: Optional[Callable[[str, str], None]] = None,
            should_stop: Optional[Callable[[], bool]] = None,
            max_turns: int = MAX_TURNS,
            window_chars: Optional[int] = None,
            job: Optional[wt.Job] = None,
            must_pass: Sequence[str] = (),
            tool_names: Optional[Sequence[str]] = None,
            extra_system: str = "") -> JobRecord:
    """Carry out an approved plan. Blocking — run it on a worker.
    `on_event(kind, text)` narrates: phase / tool / result / check / note."""
    chat_tools = chat_tools or _default_chat_tools()
    chat = chat or _default_chat()
    say = on_event or (lambda kind, text: None)
    stop = should_stop or (lambda: False)
    plan = plan_of(rec)
    pdir = pj.project_dir(vault_dir, project)

    if job is None:            # a prepared worktree (the benchmark) or a new one
        job = wt.create(project.root, pdir / "worktrees", rec.id)
    rec.branch, rec.base, rec.worktree = job.branch, job.base, str(job.path)
    rec.status, rec.started = "running", time.time()
    save_record(vault_dir, project, rec)
    say("phase", f"Working on branch {job.branch} in {job.path}")

    refs = pj.references(project) if project.references else None
    ws = Workspace(job.path, test_command=project.test_command,
                   gui_checks=project.gui_checks, references=refs,
                   code_map=lambda: cm.for_project(job.path),
                   out_dir=pdir / "jobs" / rec.id)
    baseline: Optional[set] = None
    if project.test_command:
        say("phase", "Running the tests once before any change (baseline)")
        passed, out = ws.run_tests()
        baseline = set() if passed else failing_ids(out)
        # Tests the job exists to make pass (the benchmark's target tests)
        # are never excused as "already failing".
        baseline = {t for t in baseline
                    if not any(t.startswith(f) for f in must_pass)}
        rec.baseline = sorted(baseline)
        if not passed:
            say("check", f"{len(baseline)} test(s) already fail before the "
                         "job; only NEW failures will count.")

    tools = ws.tools()
    if tool_names:              # an agent profile's own tool list
        keep = set(tool_names) | {"step_done"}
        tools = {n: f for n, f in tools.items() if n in keep}
    specs = [{"name": n, "description": f.help, "parameters": f.params}
             for n, f in tools.items()]
    budget = window_chars or _window_chars(role)
    done_summaries: List[str] = []

    for i, step in enumerate(plan.steps, 1):
        if stop():
            rec.status = "stopped"
            break
        say("phase", f"Step {i}/{len(plan.steps)}: {step.title}")
        ws.done, ws.touched = None, []
        focus = f"{rec.task}\n{step.title}\n{step.details}\n" + \
            " ".join(step.files)
        user = [f"TASK: {rec.task}", "THE PLAN:\n" + plan.text(),
                f"YOUR STEP ({i} of {len(plan.steps)}): {step.title}\n"
                f"Files: {', '.join(step.files) or '(find them)'}\n"
                f"Check: {step.check}\n{step.details}"]
        if step.test_first and project.test_command:
            user.append(TEST_FIRST)
        if done_summaries:
            user.append("STEPS ALREADY DONE:\n" + "\n".join(done_summaries))
        messages = [
            {"role": "system", "content": CODER_SYSTEM
             + (f"\n\nYOUR ROLE ON THIS PROJECT:\n{extra_system}"
                if extra_system else "") + "\n\n" + context(
                vault_dir, project, focus, root=job.path,
                map_chars=min(6000, budget // 6))},
            {"role": "user", "content": "\n\n".join(user)}]
        fixes = nudges = 0
        verdict: Optional[Verdict] = None
        for _turn in range(max_turns):
            if stop():
                break
            _trim(messages, budget)
            try:
                reply = chat_tools(messages, specs, role=role,
                                   num_predict=1800, timeout=900)
            except Exception as exc:                      # noqa: BLE001
                say("result", f"The model call failed: {exc}")
                break
            calls = reply.get("tool_calls") or []
            content = str(reply.get("content") or "").strip()
            if not calls:
                if ws.done is None and nudges < 2:
                    nudges += 1
                    messages.append({"role": "assistant",
                                     "content": content or "(no reply)"})
                    messages.append({"role": "user", "content":
                                     "Use the tools to make the change "
                                     "(edit_file / create_file), check it, "
                                     "then call step_done."})
                    continue
                break
            messages.append({"role": "assistant", "content": content,
                             "tool_calls": [{"function": {
                                 "name": c["name"],
                                 "arguments": c.get("arguments") or {}}}
                                 for c in calls]})
            for c in calls:
                name, args = c.get("name"), c.get("arguments") or {}
                fn = tools.get(name)
                if fn is None:
                    ok, msg = False, f"unknown tool {name!r}"
                else:
                    try:
                        ok, msg, _p = fn(args)
                    except Exception as exc:              # noqa: BLE001
                        ok, msg = False, f"{name} raised {exc!r}"
                say("tool", f"{name}({_args_line(args)}) → "
                            f"{'ok' if ok else 'FAILED'}")
                say("result", str(msg)[:1500])
                messages.append({"role": "tool", "tool_name": name,
                                 "content": str(msg)[:TOOL_RESULT_CHARS]})
            if ws.done is None:
                continue
            # The app checks the step itself — not the model's word for it.
            say("phase", f"Checking step {i}")
            verdict = check_step(ws, step, baseline)
            say("check", verdict.text[:3000])
            if verdict.ok:
                break
            if fixes >= MAX_FIX_ROUNDS:
                break
            fixes += 1
            ws.done = None
            messages.append({"role": "user", "content":
                             "THE CHECK FAILED:\n" + verdict.text[-4000:]
                             + "\n\nFix it, check again, then call "
                               "step_done."})
        _transcript(pdir / "jobs" / f"{rec.id}.transcript.jsonl", i,
                    step.title, messages)
        if ws.done is not None and verdict is not None and verdict.ok:
            sha = job.commit(f"Step {i}: {step.title}\n\n{ws.done}")
            step.status, step.summary, step.commit = "done", ws.done, sha or ""
            done_summaries.append(f"{i}. {step.title} — {ws.done}")
            say("phase", f"Step {i} committed ({(sha or 'no changes')[:8]})")
        else:
            why = ("stopped" if stop() else "its check did not pass"
                   if verdict is not None else "the coder did not finish it")
            step.status, step.summary = "failed", why
            job.reset_uncommitted()
            say("phase", f"Step {i} failed ({why}); its edits were undone "
                         "and the job stops here.")
            rec.status = "stopped" if stop() else "failed"
            break
        rec.plan = asdict(plan)
        save_record(vault_dir, project, rec)
    else:
        rec.status = "done"

    rec.plan = asdict(plan)
    rec.finished = time.time()
    try:
        rec.note = write_note(vault_dir, project, rec, plan, job, chat, role)
        say("note", rec.note)
        propose_brief_addition(vault_dir, project, rec, plan, chat, role)
    except Exception as exc:                              # noqa: BLE001
        say("note", f"The handoff note could not be written: {exc}")
    save_record(vault_dir, project, rec)
    return rec


def _transcript(path: Path, step: int, title: str,
                messages: List[Dict[str, Any]]) -> None:
    """Every message of a step, appended to the job's transcript — what the
    coder was told, what it called, what came back — to read after a job
    that went wrong. Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for n, m in enumerate(messages):
                fh.write(json.dumps({
                    "step": step, "title": title, "n": n,
                    "role": m.get("role"), "tool": m.get("tool_name", ""),
                    "calls": m.get("tool_calls") or [],
                    "content": str(m.get("content") or "")[:8000]},
                    ensure_ascii=False) + "\n")
    except Exception:                                     # noqa: BLE001
        pass


def _args_line(args: Dict[str, Any]) -> str:
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", "⏎")
        parts.append(f"{k}={s[:60] + '…' if len(s) > 60 else s}")
    return ", ".join(parts)


# ============================================================
# Notes for the next job, and the brief
# ============================================================

NOTE_INSTRUCTIONS = """Write a short handoff note for the next engineer \
who continues this project. Sections: Done, Left to do, Decisions (and \
why), Problems. Use the facts below; do not invent. Plain text, at most \
25 lines."""


def write_note(vault_dir: Path, project: pj.Project, rec: JobRecord,
               plan: Plan, job: wt.Job, chat: Callable[..., str],
               role: str) -> str:
    facts = [f"Task: {rec.task}", f"Branch: {rec.branch} "
             f"(status: {rec.status})", "Steps:"]
    for i, s in enumerate(plan.steps, 1):
        facts.append(f"  {i}. [{s.status}] {s.title} — {s.summary}")
    try:
        facts.append("Files changed: " + ", ".join(job.changed_files()))
    except Exception:                                     # noqa: BLE001
        pass
    if rec.baseline:
        facts.append(f"Tests failing before the job: {len(rec.baseline)}")
    text = "\n".join(facts)
    try:
        summary = chat([{"role": "system", "content": NOTE_INSTRUCTIONS},
                        {"role": "user", "content": text}],
                       role=role, num_predict=700, temperature=0.2,
                       timeout=300)
    except Exception:                                     # noqa: BLE001
        summary = ""
    note = (f"# {time.strftime('%Y-%m-%d %H:%M')} — {rec.task}\n\n"
            + (summary.strip() + "\n\n" if summary.strip() else "")
            + "## Record\n" + text + "\n")
    d = pj.project_dir(vault_dir, project) / "notes"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rec.id}.md").write_text(note, encoding="utf-8")
    return note


BRIEF_INSTRUCTIONS = """From this job, is there anything EVERY future \
job on this project must know that the brief does not already say — a \
rule, where something lives, how to run or check something? Reply with \
the short addition (bullet points), or exactly NONE."""


def propose_brief_addition(vault_dir: Path, project: pj.Project,
                           rec: JobRecord, plan: Plan,
                           chat: Callable[..., str], role: str) -> str:
    current = pj.council_brief(vault_dir, project)
    try:
        reply = chat([{"role": "system", "content": BRIEF_INSTRUCTIONS},
                      {"role": "user", "content":
                       f"BRIEF NOW:\n{pj.brief(vault_dir, project)}\n\n"
                       f"THE JOB'S NOTE:\n{rec.note}"}],
                     role=role, num_predict=300, temperature=0.1,
                     timeout=300).strip()
    except Exception:                                     # noqa: BLE001
        return ""
    if not reply or reply.upper().startswith("NONE"):
        return ""
    pj.propose_brief(vault_dir, project,
                     (current.rstrip() + "\n\n" if current.strip() else "")
                     + reply)
    return reply


__all__ = ["Plan", "Step", "JobRecord", "make_plan", "plan_from_json",
           "new_job", "run_job", "check_step", "failing_ids", "context",
           "list_records", "plan_of", "save_record", "latest_note"]
