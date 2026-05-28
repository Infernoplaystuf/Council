# ============================================================
# coder_agent.py  —  Self-correcting coding agent
# ============================================================
# Replaces the single-shot Coder with a ReAct loop:
#   write code → execute → read error → reflect → fix → repeat
#   Up to MAX_ATTEMPTS before giving up.
#
# Backend: GGUF only, via council_engine.PersonalityModel.respond().
# No LangGraph / langchain_ollama dependency — those used to provide
# an alternate Ollama-backed path but the app is GGUF-only now, and
# carrying the langchain stack just to wrap our existing loop in a
# state machine wasn't worth the import-time cost (or the silent
# DLL-load failures on Windows when transitive torch wheels mismatch
# the CUDA toolkit). The ReAct loop below is the only path.
# ============================================================

from __future__ import annotations

import re
import sys
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import council_engine as ce


# ============================================================
# State schema
# ============================================================

@dataclass
class AgentState:
    task: str                          # original user request
    code: str = ""                     # latest generated code
    filename: str = "solution.py"
    stdout: str = ""
    stderr: str = ""
    returncode: int = -1
    attempt: int = 0
    max_attempts: int = 8
    passed: bool = False
    history: List[Dict[str, str]] = field(default_factory=list)
    final_code: str = ""
    explanation: str = ""
    event_log: List[str] = field(default_factory=list)

    def log(self, msg: str) -> None:
        self.event_log.append(msg)


# ============================================================
# Prompts
# ============================================================

SYSTEM_PROMPT = """\
You are the CODER — an elite software engineer focused on robust, production-quality code.

Rules:
1. Output ONLY a fenced Python code block. No prose before it, no prose after.
2. The code must be completely self-contained and runnable as a standalone script.
3. Include all imports. Handle edge cases. Add clear error messages.
4. Never use placeholder comments like "# implement this". Write real code.
5. If the task is ambiguous, make the most reasonable assumption and note it in a comment.

Output format — STRICTLY:
```python
# your code here
```
"""

FIX_PROMPT_TEMPLATE = """\
{goal_header}Your previous attempt failed. Fix it.

ORIGINAL TASK:
{task}

YOUR PREVIOUS CODE (attempt {attempt}/{max_attempts}):
```python
{code}
```

EXECUTION RESULT:
Return code: {returncode}
stdout:
{stdout}
stderr:
{stderr}

REFLECTION:
- What went wrong?
- What is the root cause?
- What is the minimal fix?

{goal_reminder}Now output the corrected code. ONLY a fenced Python code block. No other text.
```python
"""

FIRST_ATTEMPT_PROMPT = """\
{goal_header}Write a Python script that solves the following task completely.

TASK:
{task}

{goal_reminder}Output ONLY a fenced Python code block. No explanations, no prose.
```python
"""

EXPLAIN_PROMPT = """\
{goal_header}The following Python code solves this task:

TASK:
{task}

CODE:
```python
{code}
```

Write a brief (3-5 sentence) explanation of how the code works and any important design decisions.
Do NOT repeat the code. Plain prose only.
"""


def _goal_header(goal: str) -> str:
    """Top-of-prompt goal anchor. Empty string when no goal is set."""
    if not goal:
        return ""
    return (
        f"[USER GOAL — your single objective for this turn]\n  {goal}\n\n"
    )


def _goal_reminder(goal: str) -> str:
    """Bottom-of-prompt goal reminder. Empty string when no goal is set."""
    if not goal:
        return ""
    return f"⚑ REMEMBER — the user's goal is: {goal}\n\n"


# ============================================================
# Code extraction
# ============================================================

def _extract_code(text: str) -> str:
    """Extract first Python code block from model output."""
    # Try fenced block first
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Try unfenced — everything that looks like Python
    lines = text.splitlines()
    code_lines = []
    in_code = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("import ", "from ", "def ", "class ", "#!", "if __name__")):
            in_code = True
        if in_code:
            code_lines.append(line)
    if code_lines:
        return "\n".join(code_lines).strip()
    # Last resort: return the whole text
    return text.strip()


def _make_filename(task: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", "", task.lower()).split()[:5]
    return "_".join(words) or "solution"


# ============================================================
# Execution
# ============================================================

def _run_code(code: str, filename: str, workspace: Path, timeout: int = 30) -> Tuple[int, str, str]:
    """Write code to file and execute it. Returns (returncode, stdout, stderr)."""
    path = workspace / filename
    path.write_text(code, encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True, text=True,
            cwd=str(workspace), timeout=timeout,
        )
        return result.returncode, result.stdout[:4000], result.stderr[:4000]
    except subprocess.TimeoutExpired:
        return -1, "", f"TimeoutExpired: script exceeded {timeout}s"
    except Exception as e:
        return -1, "", str(e)


# ============================================================
# Self-correcting ReAct loop (the only path now)
# ============================================================

def _run_react_loop(
    task: str,
    personality_model: Any,          # ce.PersonalityModel
    runner: ce.LocalRunner,
    max_attempts: int = 8,
    event_callback: Optional[Callable[[str, str], None]] = None,
    goal: str = "",
) -> AgentState:
    """
    Write → execute → reflect → fix loop, backed by the GGUF runtime
    via council_engine.PersonalityModel.respond().

    event_callback(phase, message) fires on every state transition so
    the GUI can stream status into a live log panel.
    """
    def _emit(phase: str, msg: str):
        if event_callback:
            event_callback(phase, msg)

    state = AgentState(task=task, max_attempts=max_attempts)
    state.filename = _make_filename(task) + ".py"

    for attempt in range(1, max_attempts + 1):
        state.attempt = attempt

        # Build prompt — first attempt uses a fresh template; subsequent
        # attempts include the failed code + stderr so the model can
        # reason about the failure mode.
        if attempt == 1:
            prompt = FIRST_ATTEMPT_PROMPT.format(
                task=task,
                goal_header=_goal_header(goal),
                goal_reminder=_goal_reminder(goal),
            )
        else:
            prompt = FIX_PROMPT_TEMPLATE.format(
                task=task,
                attempt=attempt - 1,
                max_attempts=max_attempts,
                code=state.code,
                returncode=state.returncode,
                stdout=state.stdout[:1500],
                stderr=state.stderr[:1500],
                goal_header=_goal_header(goal),
                goal_reminder=_goal_reminder(goal),
            )

        _emit("generate", f"Attempt {attempt}/{max_attempts} — writing code…")
        raw = personality_model.respond(prompt)
        state.code = _extract_code(raw)
        state.history.append({"attempt": attempt, "code": state.code})

        # Execute
        _emit("execute", f"Attempt {attempt} — running…")

        # Dream3D static validation before execution — catches pipeline
        # mistakes that would otherwise burn a full execute attempt on
        # a deterministic-fail script.
        try:
            from dream3d_primer import PipelineValidator
            valid, issues = PipelineValidator.validate(state.code)
            if not valid:
                feedback = PipelineValidator.format_feedback(issues)
                _emit("validate", "Static validation issues found")
                state.returncode = -2
                state.stdout     = ""
                state.stderr     = feedback
                state.passed     = False
                _emit("result", f"Attempt {attempt} ✗ STATIC FAIL\n{feedback[:400]}")
                continue
        except ImportError:
            # dream3d_primer isn't always present — that's fine, just
            # skip static validation and rely on runtime execution.
            pass

        rc, stdout, stderr = _run_code(
            state.code,
            f"{_make_filename(task)}_v{attempt}.py",
            runner.workspace,
        )
        state.returncode = rc
        state.stdout = stdout
        state.stderr = stderr
        state.passed = rc == 0

        status = "✓ PASSED" if state.passed else f"✗ FAILED (rc={rc})"
        _emit("result",
              f"Attempt {attempt} {status}\n"
              f"stdout: {stdout[:400]}\nstderr: {stderr[:400]}")

        if state.passed:
            _emit("explain", "Generating explanation…")
            explain_prompt = EXPLAIN_PROMPT.format(
                task=task, code=state.code,
                goal_header=_goal_header(goal),
            )
            state.explanation = personality_model.respond(explain_prompt)
            state.final_code = state.code
            return state

    # Exhausted all attempts — return the last attempt as best effort.
    _emit("give_up", f"Exhausted {max_attempts} attempts. Returning best effort.")
    state.final_code = state.code
    state.explanation = (
        f"Could not produce passing code in {max_attempts} attempts. "
        "Returning last generated version — review stderr above."
    )
    return state


# ============================================================
# Public API
# ============================================================

class CoderAgent:
    """
    Drop-in replacement for the single-shot Coder ModelAgent.
    Runs a self-correcting ReAct loop on the GGUF backend.
    """

    def __init__(
        self,
        personality_model: Any,        # ce.PersonalityModel
        runner: ce.LocalRunner,
        max_attempts: int = 8,
        event_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.model = personality_model
        self.runner = runner
        self.max_attempts = max_attempts
        self.event_callback = event_callback
        print("[CoderAgent] Ready (GGUF ReAct loop, max_attempts="
              f"{max_attempts})")

    @property
    def uses_langgraph(self) -> bool:
        # Kept for backwards compatibility with any caller that asks —
        # always False now that LangGraph has been removed.
        return False

    def run(self, task: str, goal: str = "") -> AgentState:
        """
        Run the coding agent on a task.

        ``goal`` is the optional one-line user-goal anchor for this turn.
        When provided it is rendered at the top + bottom of every retry
        prompt so the model doesn't drift across attempts (the FIX prompt
        contains the failed code + stderr, which can dominate attention
        on small models and shove the actual ask out of focus).

        Returns AgentState with .final_code, .explanation, .passed, .event_log
        """
        return _run_react_loop(
            task=task,
            personality_model=self.model,
            runner=self.runner,
            max_attempts=self.max_attempts,
            event_callback=self.event_callback,
            goal=goal,
        )
