"""
council_core.task_chain — connected tasks: plan, small workers, checks, one answer.

WHY THIS SHAPE
Multi-agent systems get their results mostly from structure, not from how many
models run at once: a hard goal is broken into small steps with checkable
results; each step is done in its own short context; each result is checked
before anything is built on it; and one careful pass writes the answer from
checked parts. Small local models follow long, open-ended instructions badly
but short, concrete ones well — so this is the shape that suits them.

    PLAN       Strategist (main model) -> 1..5 steps as JSON, each with the
               condition a correct result must meet and the earlier steps it
               needs.
    WORK       each step: the sandboxed READ-ONLY agent (safe_agent), on the
               Intern's model slot — the fast model in the Balanced preset —
               seeing only its own step and short results of the steps it
               depends on.
    CHECK      hard checks first (did it finish; did a data step actually
               look at data), then the Judge's pass/fail against the step's
               condition. A failed step is retried ONCE with the reason.
    SYNTHESISE Writer (main model) answers the goal from the step results
               only, citing steps, and says what could not be verified.

A step that still fails after its retry is kept, marked unverified, and the
chain goes on: one weak step should cost a caveat, not the whole run.

EVERYTHING IS INJECTED
`chat(role, messages, max_tokens)` and `work(role, task, on_step)` are the only
ways out, so the orchestration is testable without a model and each role lands
on its own model slot (council_core.model_slots) through the engine's
`local_chat(role=...)`. Nothing here decides a thread; the job runner calls it
on its worker and checks `should_stop` between calls.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

PLANNER, WORKER, CHECKER, WRITER = "strategist", "intern", "judge", "writer"
ESCALATE = "coder"          # who retries a failed step (see TaskChain)

MAX_STEPS = 5
RESULT_FOR_NEXT = 800          # chars of a dependency's result a step sees
RESULT_FOR_WRITER = 1500       # chars of each step's result the writer sees
OVERVIEW_CHARS = 1500          # the data overview every prompt carries

Chat = Callable[[str, List[Dict[str, str]], int], str]


class ChainCancelled(Exception):
    pass


# ============================================================
# Data
# ============================================================

@dataclass
class PlannedStep:
    number: int
    task: str
    check: str = "It answers the task directly and is supported by the data."
    needs: List[int] = field(default_factory=list)


@dataclass
class WorkerResult:
    answer: str
    stopped_reason: str = "done"         # done | max_steps | error | ...
    tools_used: List[str] = field(default_factory=list)


@dataclass
class StepResult:
    step: PlannedStep
    answer: str = ""
    verified: bool = False
    problems: List[str] = field(default_factory=list)
    attempts: int = 0
    tools_used: List[str] = field(default_factory=list)


@dataclass
class ChainResult:
    goal: str
    plan: List[PlannedStep]
    plan_source: str                     # "model" | "fallback"
    steps: List[StepResult]
    answer: str = ""

    @property
    def verified_count(self) -> int:
        return sum(1 for s in self.steps if s.verified)


@dataclass
class ChainEvent:
    stage: str                           # plan | work | check | retry | answer
    text: str
    step: int = 0
    ok: Optional[bool] = None


# ============================================================
# JSON out of a model reply
# ============================================================

def first_json(text: str) -> Optional[Any]:
    """The first JSON object or array in ``text``, or None.

    Models wrap JSON in prose and code fences; this scans for each '{' / '['
    and lets the real decoder decide where the value ends.
    """
    decoder = json.JSONDecoder()
    text = text or ""
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                value, _end = decoder.raw_decode(text, i)
                return value
            except ValueError:
                continue
    return None


class PlanError(ValueError):
    pass


def parse_plan(reply: str, max_steps: int = MAX_STEPS) -> List[PlannedStep]:
    """Steps from the planner's reply. Raises PlanError saying what is wrong,
    so the planner can be asked once to fix exactly that."""
    data = first_json(reply)
    if isinstance(data, dict):
        data = data.get("steps")
    if not isinstance(data, list) or not data:
        raise PlanError('expected {"steps": [...]} with at least one step')
    steps: List[PlannedStep] = []
    for i, item in enumerate(data[:max_steps], start=1):
        if isinstance(item, str):
            item = {"task": item}
        if not isinstance(item, dict) or not str(item.get("task", "")).strip():
            raise PlanError(f"step {i} has no task")
        needs = []
        for n in item.get("needs") or []:
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if 1 <= n < i:                # only EARLIER steps; no cycles
                needs.append(n)
        check = str(item.get("check") or "").strip()
        steps.append(PlannedStep(i, str(item["task"]).strip(),
                                 check or PlannedStep(0, "").check, needs))
    return steps


_TRIVIAL_STEP = re.compile(
    r"^\s*(read|open|load|import|view|access|get)\s+(in\s+)?(the\s+|a\s+)?"
    r"[\w\-. /\\']+\.(csv|tsv|txt|xlsx|xls|json|parquet|md)'?"
    r"(\s+file)?\s*[.!]?\s*$", re.IGNORECASE)


def prune_trivial_steps(steps: List[PlannedStep]) -> List[PlannedStep]:
    """Drop steps that only open a file, when other steps follow.

    Every step can use every tool, so "Read the sales.csv file" has no result
    of its own — and on the first real run the checker rejected exactly that
    step twice for not "confirming the file was read". The next step reads
    the file anyway. Steps are renumbered; a need on a dropped step is
    dropped with it.
    """
    kept = [s for s in steps if not _TRIVIAL_STEP.match(s.task)]
    if not kept or len(kept) == len(steps):
        return steps
    renumber = {s.number: i for i, s in enumerate(kept, start=1)}
    return [PlannedStep(renumber[s.number], s.task, s.check,
                        [renumber[n] for n in s.needs if n in renumber])
            for s in kept]


def parse_verdict(reply: str) -> Optional[Dict[str, Any]]:
    """{"pass": bool, "reason": str} from the checker, or None if unreadable."""
    data = first_json(reply)
    if isinstance(data, dict) and isinstance(data.get("pass"), bool):
        return {"pass": data["pass"], "reason": str(data.get("reason", ""))}
    low = (reply or "").strip().lower()
    if low.startswith("pass"):
        return {"pass": True, "reason": reply.strip()[4:].strip(" :-")}
    if low.startswith("fail"):
        return {"pass": False, "reason": reply.strip()[4:].strip(" :-")}
    return None


# ============================================================
# Prompts
# ============================================================

PLAN_SYSTEM = """You plan work for a careful junior assistant.
The assistant can only READ the user's data folder: list files, search file
contents, read a file, and run pandas analysis on tabular files. It cannot
write, delete, browse the web or run programs.

Break the user's goal into 1 to {n} steps — as FEW as the goal needs.
Each step is one piece of work with a result someone could check; a step may
use several tools, so do NOT split "read the file" and "analyse it" into
separate steps. Use only file and column names that appear under DATA
AVAILABLE — never invent one. Later steps may use the results of earlier
ones: list those step numbers in "needs".

Reply with JSON only, no prose:
{{"steps": [{{"task": "...", "needs": [], "check": "what a correct result must contain"}}]}}"""

WORK_TEMPLATE = """OVERALL GOAL: {goal}
{data}
YOUR STEP ({number} of {total}): {task}
{deps}
Do only your step. Use the tools to look at the actual data. If the data does
not contain the answer, say so plainly instead of guessing."""

CHECK_SYSTEM = """You check one step of a larger task.
Decide whether the RESULT meets the CONDITION for the STEP. Be strict about
invented facts, and fair about a result that honestly says the data lacks
something.
Reply with JSON only: {"pass": true or false, "reason": "one sentence"}"""

WRITE_SYSTEM = """You write the final answer to the user's goal.
Use ONLY the step results below. Cite the step you rely on like [2]. If a step
is marked UNVERIFIED, use it cautiously and say what is uncertain. Do not add
facts the steps do not contain."""

#: Words that make a step a data step — one that must actually look at data.
_DATA_WORDS = re.compile(
    r"\b(file|files|csv|excel|xlsx|parquet|json|column|columns|row|rows|"
    r"table|dataset|data|read|search|list|count|sum|average|mean|total)\b",
    re.IGNORECASE)


def needs_data(task: str) -> bool:
    return bool(_DATA_WORDS.search(task or ""))


_ACTION_START = re.compile(r'^\s*(```\w*\s*)?\{\s*"action"\s*:', re.IGNORECASE)


def looks_like_tool_call(answer: str) -> bool:
    """An "answer" that is really the agent protocol — a tool call or its
    malformed start — rather than a result."""
    if _ACTION_START.match(answer or ""):
        return True
    data = first_json(answer or "")
    return (isinstance(data, dict) and "action" in data
            and len((answer or "").strip()) < 2000
            and (answer or "").strip().startswith(("{", "```")))


# ============================================================
# The chain
# ============================================================

class TaskChain:
    """Runs one goal through plan -> work -> check -> synthesise."""

    def __init__(self, chat: Chat,
                 work: Callable[[str, str, Callable[[Any], None]],
                                WorkerResult],
                 *, on_event: Optional[Callable[[ChainEvent], None]] = None,
                 should_stop: Optional[Callable[[], bool]] = None,
                 max_steps: int = MAX_STEPS, retries: int = 1,
                 data_overview: Optional[Callable[[], str]] = None,
                 escalate_role: str = ESCALATE):
        self.chat = chat
        self.work = work
        # Who retries a failed step. Cheap first, strong second: on a real
        # Balanced run Llama 3.2 3B (the Intern's slot) could not drive the
        # tools at all, while Phi-4 planned well — so a retry goes to the
        # Coder's slot, which is Phi-4 under that preset. With one model this
        # is the same model, and nothing changes.
        self.escalate_role = escalate_role or WORKER
        # What data exists — file names, CSV headers, row counts — read
        # deterministically. Without it the planner guessed: on the first real
        # run it invented a "Total Sales Amount" column that did not exist.
        self.data_overview = data_overview or (lambda: "")
        self._overview = ""
        self.on_event = on_event or (lambda ev: None)
        self.should_stop = should_stop or (lambda: False)
        self.max_steps = max(1, min(MAX_STEPS, int(max_steps)))
        self.retries = max(0, int(retries))

    def _emit(self, stage: str, text: str, step: int = 0,
              ok: Optional[bool] = None) -> None:
        self.on_event(ChainEvent(stage, text, step, ok))

    def _stop_check(self) -> None:
        if self.should_stop():
            raise ChainCancelled()

    def _ask(self, role: str, system: str, user: str,
             max_tokens: int) -> str:
        self._stop_check()
        return self.chat(role, [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                         max_tokens)

    # -- plan --------------------------------------------------------------
    def plan(self, goal: str):
        system = PLAN_SYSTEM.format(n=self.max_steps)
        user = f"GOAL: {goal}"
        if self._overview:
            user = f"DATA AVAILABLE:\n{self._overview}\n\n{user}"
        reply = self._ask(PLANNER, system, user, 700)
        try:
            steps = prune_trivial_steps(parse_plan(reply, self.max_steps))
            return steps, "model"
        except PlanError as first:
            fix = self._ask(PLANNER, system,
                            f"{user}\n\nYour previous reply could not "
                            f"be used ({first}). Reply again with the JSON "
                            "only.", 700)
            try:
                return (prune_trivial_steps(parse_plan(fix, self.max_steps)),
                        "model")
            except PlanError:
                # A goal the planner cannot break down is still a goal:
                # one step, the whole thing, checked like any other.
                return [PlannedStep(1, goal)], "fallback"

    # -- work + check ------------------------------------------------------
    def _task_text(self, goal: str, step: PlannedStep, total: int,
                   done: Dict[int, StepResult], critique: str = "") -> str:
        deps = ""
        for n in step.needs:
            r = done.get(n)
            if r is not None:
                deps += (f"\nRESULT OF STEP {n}"
                         f"{'' if r.verified else ' (UNVERIFIED)'}: "
                         f"{r.answer[:RESULT_FOR_NEXT]}")
        if deps:
            deps += "\n"
        data = (f"\nFILES YOU CAN READ:\n{self._overview}\n"
                if self._overview else "")
        text = WORK_TEMPLATE.format(goal=goal, data=data, number=step.number,
                                    total=total, task=step.task, deps=deps)
        if critique:
            text += (f"\n\nA reviewer rejected your previous answer: "
                     f"{critique}\nFix exactly that.")
        return text

    def hard_problems(self, step: PlannedStep,
                      result: WorkerResult) -> List[str]:
        """Checks that need no model — they catch what a small model cannot
        catch in itself."""
        problems = []
        if result.stopped_reason and result.stopped_reason != "done":
            problems.append(f"the worker stopped before finishing "
                            f"({result.stopped_reason})")
        if len((result.answer or "").strip()) < 15:
            problems.append("the answer is empty or too short to use")
        if looks_like_tool_call(result.answer):
            # Measured: a worker's malformed tool call came back as its
            # "answer", and the checker PASSED it. Caught here instead.
            problems.append("the answer is a tool call, not a result — run "
                            "the tool and report what it returned")
        if needs_data(step.task) and not result.tools_used:
            problems.append("it answered a data question without looking at "
                            "any data")
        return problems

    def check(self, step: PlannedStep, answer: str) -> Dict[str, Any]:
        reply = self._ask(
            CHECKER, CHECK_SYSTEM,
            f"STEP: {step.task}\nCONDITION: {step.check}\n\n"
            f"RESULT:\n{answer[:3000]}", 200)
        verdict = parse_verdict(reply)
        if verdict is None:
            return {"pass": True, "reason": "the checker's reply was "
                    "unreadable, so this step is not independently verified",
                    "unread": True}
        return verdict

    def run_step(self, goal: str, step: PlannedStep, total: int,
                 done: Dict[int, StepResult]) -> StepResult:
        out = StepResult(step)
        critique = ""
        for attempt in range(1 + self.retries):
            out.attempts = attempt + 1
            out.problems = []        # a clean retry must not inherit the old
            self._stop_check()
            role = WORKER if attempt == 0 else self.escalate_role
            self._emit("work" if attempt == 0 else "retry",
                       f"step {step.number}/{total}: {step.task}"
                       + (f" — retrying as {role}: {critique}"
                          if attempt else ""),
                       step.number)
            result = self.work(role, self._task_text(goal, step, total, done,
                                                     critique),
                               lambda ev: self._stop_check())
            out.answer = (result.answer or "").strip()
            out.tools_used = list(result.tools_used or [])
            problems = self.hard_problems(step, result)
            if not problems:
                verdict = self.check(step, out.answer)
                if not verdict["pass"]:
                    problems = [verdict["reason"] or "the checker rejected it"]
                elif verdict.get("unread"):
                    out.problems = [verdict["reason"]]
            if not problems:
                out.verified = not out.problems
                self._emit("check", f"step {step.number}: "
                           + ("passed" if out.verified else out.problems[0]),
                           step.number, ok=True)
                return out
            critique = "; ".join(problems)
            out.problems = problems
            self._emit("check", f"step {step.number} failed: {critique}",
                       step.number, ok=False)
        out.verified = False
        return out

    # -- synthesise --------------------------------------------------------
    def synthesise(self, goal: str, results: Sequence[StepResult]) -> str:
        parts = []
        for r in results:
            flag = "" if r.verified else \
                f" (UNVERIFIED: {'; '.join(r.problems) or 'not checked'})"
            parts.append(f"[{r.step.number}] {r.step.task}{flag}\n"
                         f"{r.answer[:RESULT_FOR_WRITER] or '(no result)'}")
        return self._ask(WRITER, WRITE_SYSTEM,
                         f"GOAL: {goal}\n\nSTEP RESULTS:\n\n"
                         + "\n\n".join(parts), 1400).strip()

    # -- the whole thing ---------------------------------------------------
    def run(self, goal: str) -> ChainResult:
        goal = (goal or "").strip()
        try:
            self._overview = (self.data_overview() or "")[:OVERVIEW_CHARS]
        except Exception:                                 # noqa: BLE001
            self._overview = ""
        self._emit("plan", "planning…")
        plan, source = self.plan(goal)
        self._emit("plan", f"{len(plan)} step(s)"
                   + (" — the planner's reply was unusable, so the goal runs "
                      "as one step" if source == "fallback" else "")
                   + ":\n" + "\n".join(f"  {s.number}. {s.task}"
                                       + (f"  (uses {', '.join(map(str, s.needs))})"
                                          if s.needs else "")
                                       for s in plan))
        done: Dict[int, StepResult] = {}
        results: List[StepResult] = []
        for step in plan:
            r = self.run_step(goal, step, len(plan), done)
            done[step.number] = r
            results.append(r)
        self._emit("answer", "writing the answer from the checked steps…")
        answer = self.synthesise(goal, results)
        result = ChainResult(goal, plan, source, results, answer)
        self._emit("answer", f"done — {result.verified_count} of "
                   f"{len(results)} step(s) verified", ok=True)
        return result


# ============================================================
# The report
# ============================================================

def report_markdown(result: ChainResult) -> str:
    lines = ["# Connected-tasks report", "",
             f"**Goal:** {result.goal}", "",
             f"**Verified:** {result.verified_count} of {len(result.steps)} "
             "step(s)", "", "## Answer", "", result.answer or "(none)", "",
             "## Steps", ""]
    for r in result.steps:
        mark = "✓" if r.verified else "⚠"
        lines.append(f"### {mark} {r.step.number}. {r.step.task}")
        lines.append(f"*Condition:* {r.step.check}  ")
        if r.step.needs:
            lines.append(f"*Uses steps:* {', '.join(map(str, r.step.needs))}  ")
        lines.append(f"*Attempts:* {r.attempts}   *Tools:* "
                     f"{', '.join(r.tools_used) or 'none'}")
        if r.problems:
            lines.append(f"*Not verified:* {'; '.join(r.problems)}")
        lines += ["", r.answer or "(no result)", ""]
    if result.plan_source == "fallback":
        lines += ["_The planner's reply could not be used, so the goal ran as "
                  "a single step._", ""]
    return "\n".join(lines)
