"""
godot_coder.py — GDScript-targeted variant of CoderAgent.

The existing ``coder_agent.CoderAgent`` is a self-correcting ReAct loop
that writes Python, executes it, reads the traceback, and reflects.
For Godot we want the same shape but:

  • Output is GDScript (4.x), not Python
  • "Execute" means ``godot --headless --check-only`` against the
    user's project after writing the new script in place
  • "Stderr" parsing knows GDScript-specific error patterns
    (``Parse Error: …``, ``Invalid get index …``, autoload-resolution
    failures, etc.)
  • The FIRST_ATTEMPT / FIX prompts are GDScript-shaped — Godot 4 API
  • Goal anchor (from ``goal_anchor``) is threaded the same way as in
    ``coder_agent`` so retries stay locked on intent

Why a separate module rather than a subclass:
  • Prompt templates are substantially different
  • The runner is Godot, not the local Python interpreter
  • Validation lives in ``godot --headless --check-only``, not in
    ``runner.workspace``
A class hierarchy would force one side to bend; siblings stay readable.

Safety:
  • The agent writes to a CALLER-CHOSEN path inside the project. The
    caller is expected to confirm-overwrite with the user before
    invoking on an existing file. ``run()`` will refuse paths that
    escape the project root.
  • If validation fails on the last attempt, the previous file content
    is restored (the agent keeps a backup of the original bytes).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional


# ============================================================
# State
# ============================================================

@dataclass
class GodotAgentState:
    """ReAct loop state for GDScript generation."""
    task:          str
    goal:          str = ""               # distilled user-intent anchor
    target_path:   Optional[Path] = None  # where the script will be written
    code:          str = ""
    stdout:        str = ""
    stderr:        str = ""
    returncode:    int = -1
    attempt:       int = 0
    max_attempts:  int = 6
    passed:        bool = False
    history:       List[dict] = field(default_factory=list)
    final_code:    str = ""
    backup:        Optional[bytes] = None  # original bytes of target_path, if any
    event_log:     List[str] = field(default_factory=list)

    def log(self, msg: str) -> None:
        self.event_log.append(msg)


# ============================================================
# Prompts
# ============================================================

SYSTEM_PROMPT = """\
You are the GODOT CODER — a senior GDScript engineer fluent in Godot 4.x.

Rules:
1. Output ONLY one fenced GDScript code block. No prose before it, no
   prose after.
2. Always start with the appropriate `extends` (or `class_name`) line.
3. Use Godot 4 APIs only — no Godot 3 holdovers (no `KEY_*` constants
   that were renamed, no `tool` keyword — use `@tool` instead, no
   `export` — use `@export`, no `onready` — use `@onready`).
4. Include type hints where they help readability.
5. Never use placeholder comments like "# implement this". Write real
   code.
6. If the task references an existing scene/node, assume nodes are
   reachable via `get_node("…")` or `@onready var x = $Path/To/Node`.

Output format — STRICTLY:
```gdscript
# your code here
```
"""

FIRST_ATTEMPT_PROMPT = """\
{goal_header}Write a GDScript file that solves the following task.

TASK:
{task}

Target file path (relative to project root): {rel_target}

Output ONLY a fenced GDScript code block. No explanations, no prose.
{goal_reminder}```gdscript
"""

FIX_PROMPT_TEMPLATE = """\
{goal_header}Your previous attempt did not parse cleanly under Godot.
Fix it.

ORIGINAL TASK:
{task}

YOUR PREVIOUS CODE (attempt {attempt}/{max_attempts}):
```gdscript
{code}
```

GODOT --check-only OUTPUT (rc={returncode}):
stderr:
{stderr}

REFLECTION:
- What did Godot complain about?
- What is the minimal fix?
- Keep type hints; do not drop features unless the task itself was
  ambiguous.

{goal_reminder}Now output the corrected GDScript. ONLY a fenced
GDScript code block. No other text.
```gdscript
"""


def _goal_header(goal: str) -> str:
    if not goal:
        return ""
    return (
        f"[USER GOAL — your single objective for this turn]\n  {goal}\n\n"
    )


def _goal_reminder(goal: str) -> str:
    if not goal:
        return ""
    return f"⚑ REMEMBER — the user's goal is: {goal}\n\n"


# ============================================================
# Code extraction
# ============================================================

_FENCE_RE = re.compile(
    r"```(?:gdscript|gd|godot)?\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def _extract_code(text: str) -> str:
    """Pull the first fenced GDScript block out of a model response.

    Tolerant: if no fence is found, returns lines from the first
    ``extends`` / ``class_name`` to the end (the model occasionally
    forgets the closing fence).
    """
    if not text:
        return ""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    lines = text.splitlines()
    start = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(("extends ", "class_name ", "@tool", "@icon")):
            start = i
            break
    if start >= 0:
        return "\n".join(lines[start:]).strip()
    return text.strip()


# ============================================================
# Validation — godot --headless --check-only
# ============================================================

def _check_only(godot_binary: str, project_root: Path,
                timeout: float = 30.0) -> tuple[int, str, str]:
    """Run ``godot --headless --check-only --path <project>`` and return
    (returncode, stdout, stderr). Failures to launch surface as rc=127
    with a self-explanatory stderr."""
    # Windows: suppress the console-window pop
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        except Exception:
            startupinfo = None
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [godot_binary, "--headless", "--path", str(project_root),
             "--check-only"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        return result.returncode, (result.stdout or "")[:4000], \
               (result.stderr or "")[:4000]
    except FileNotFoundError:
        return 127, "", (
            f"[godot_coder] Could not find Godot binary: "
            f"{godot_binary!r}. Pick the executable in the Godot "
            f"Workspace settings."
        )
    except subprocess.TimeoutExpired:
        return -1, "", "[godot_coder] godot --check-only timed out"
    except Exception as exc:
        return -1, "", f"[godot_coder] godot --check-only crashed: {exc!r}"


# ============================================================
# Public API
# ============================================================

class GodotCoder:
    """GDScript ReAct loop.

    Usage:

        agent = GodotCoder(personality_model, project_root, on_event)
        state = agent.run(task, target_path, goal="")
        if state.passed:
            print(state.final_code)
        else:
            print(state.stderr)
            # original file content has been restored

    ``target_path`` is the absolute path the script will be written
    to. It MUST live inside ``project_root`` — otherwise ``run()``
    raises ValueError. Existing content is backed up to ``state.backup``
    and restored if all attempts fail.
    """

    def __init__(
        self,
        personality_model: Any,
        project_root: Any,
        *,
        godot_binary: str = "godot",
        max_attempts: int = 6,
        event_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.model = personality_model
        self.project_root = Path(project_root).expanduser().resolve()
        self.godot_binary = godot_binary
        self.max_attempts = max_attempts
        self.event_callback = event_callback or (lambda phase, msg: None)

    # ----------------------------------------------------------------

    def _emit(self, phase: str, msg: str) -> None:
        try:
            self.event_callback(phase, msg)
        except Exception:
            pass

    def _check_inside_project(self, target: Path) -> Path:
        """Resolve + assert target is inside project_root. Returns the
        resolved Path; raises ValueError otherwise."""
        t = Path(target).expanduser().resolve()
        try:
            t.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError(
                f"target_path {t} escapes project_root {self.project_root}"
            ) from exc
        return t

    # ----------------------------------------------------------------

    def run(
        self,
        task: str,
        target_path: Any,
        *,
        goal: str = "",
    ) -> GodotAgentState:
        """Drive the ReAct loop. Writes ``state.final_code`` to disk on
        success; restores the original bytes (or removes the file) on
        full failure."""
        state = GodotAgentState(task=task, goal=goal,
                                  max_attempts=self.max_attempts)
        target = self._check_inside_project(target_path)
        state.target_path = target

        # Back up existing content so we can roll back on full failure
        if target.exists():
            try:
                state.backup = target.read_bytes()
            except Exception as exc:
                state.log(f"could not read existing file: {exc!r}")
                state.backup = None
            existed = True
        else:
            state.backup = None
            existed = False

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            state.stderr = f"could not create parent dir: {exc!r}"
            return state

        rel_target = str(target.relative_to(self.project_root)).replace("\\", "/")

        # ── Loop ───────────────────────────────────────────────
        for attempt in range(1, self.max_attempts + 1):
            state.attempt = attempt
            if attempt == 1:
                prompt = FIRST_ATTEMPT_PROMPT.format(
                    task=task, rel_target=rel_target,
                    goal_header=_goal_header(goal),
                    goal_reminder=_goal_reminder(goal),
                )
            else:
                prompt = FIX_PROMPT_TEMPLATE.format(
                    task=task,
                    attempt=attempt - 1,
                    max_attempts=self.max_attempts,
                    code=state.code,
                    returncode=state.returncode,
                    stderr=(state.stderr or "")[:2000],
                    goal_header=_goal_header(goal),
                    goal_reminder=_goal_reminder(goal),
                )

            self._emit("generate",
                       f"Attempt {attempt}/{self.max_attempts} — writing GDScript…")
            try:
                raw = self.model.respond(prompt)
            except Exception as exc:
                state.stderr = f"model.respond crashed: {exc!r}"
                self._emit("result", f"Attempt {attempt} ✗ MODEL ERROR")
                continue

            state.code = _extract_code(raw)
            state.history.append({"attempt": attempt, "code": state.code})

            if not state.code.strip():
                state.stderr = "model returned empty code"
                self._emit("result", f"Attempt {attempt} ✗ EMPTY")
                continue

            try:
                target.write_text(state.code, encoding="utf-8")
            except Exception as exc:
                state.stderr = f"could not write target: {exc!r}"
                self._emit("result", f"Attempt {attempt} ✗ WRITE FAIL")
                continue

            self._emit("validate",
                       f"Attempt {attempt} — godot --check-only…")
            rc, out, err = _check_only(self.godot_binary, self.project_root)
            state.returncode = rc
            state.stdout = out
            state.stderr = err
            state.passed = (rc == 0)
            self._emit(
                "result",
                f"Attempt {attempt} {'✓ PASSED' if state.passed else '✗ FAILED'} "
                f"(rc={rc})\n" + (err[:400] if err else ""),
            )
            if state.passed:
                state.final_code = state.code
                return state

        # ── All attempts failed — roll back ───────────────────
        try:
            if existed and state.backup is not None:
                target.write_bytes(state.backup)
                self._emit("rollback",
                           "All attempts failed — restored original file.")
            elif not existed and target.exists():
                target.unlink()
                self._emit("rollback",
                           "All attempts failed — removed partial file.")
        except Exception as exc:
            self._emit("rollback", f"rollback failed: {exc!r}")

        return state
