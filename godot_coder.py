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
  • Atomic in-place updates: every attempt writes to a sibling
    ``<target>.anvil_tmp`` first and then ``os.replace()``-es it
    into the real path, so a crash mid-attempt leaves either the
    old or new full file content — never a partial. Note that
    full pre-validation isolation (write-validate-then-swap) is
    NOT possible because ``godot --check-only`` validates the
    project, not a single file; the real path must be in place
    while Godot reads it. The backup-and-restore path covers the
    "validation failed" case.
  • If validation fails on the last attempt, the previous file
    content is restored via the same atomic ``os.replace`` swap.
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
    """ReAct loop state for GDScript generation.

    ``history`` accumulates a record per attempt with ``code`` and
    ``stderr_summary`` so the FIX prompt can show the model a
    compressed "tried-and-failed" log. Without that, small models
    oscillate between two bad fixes — they don't know their own
    history. Default ``max_attempts`` is 3 to match the small-model
    regime; raise it for ≥14B models if you want.
    """
    task:          str
    goal:          str = ""               # distilled user-intent anchor
    target_path:   Optional[Path] = None  # where the script will be written
    code:          str = ""
    stdout:        str = ""
    stderr:        str = ""
    returncode:    int = -1
    attempt:       int = 0
    max_attempts:  int = 3
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
Fix it — but DO NOT retry an approach that has already failed below.

ORIGINAL TASK:
{task}

YOUR MOST RECENT CODE (attempt {attempt}/{max_attempts}):
```gdscript
{code}
```

GODOT --check-only OUTPUT (rc={returncode}):
stderr:
{stderr}

{history_block}\
REFLECTION:
- What did Godot complain about THIS time?
- Which of your previous fix directions also failed (see above)?
  Do not repeat them.
- What is a different minimal fix?
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
# Stderr summarisation — compress a Godot --check-only stderr
# block into one line for the retry-history log.
# ============================================================

# Patterns that look like the actually useful error line in a Godot
# stderr dump. We try each in order and keep the first match.
_STDERR_INTEREST_PATTERNS = (
    re.compile(r"Parse Error:[^\n]+"),
    re.compile(r"Invalid get index[^\n]+"),
    re.compile(r"Cannot find type[^\n]+"),
    re.compile(r"ERROR:[^\n]+"),
    re.compile(r"SCRIPT ERROR:[^\n]+"),
    re.compile(r"Compile Error:[^\n]+"),
)


def _summarise_stderr(stderr: str) -> str:
    """Compress a Godot stderr dump into a single representative line
    for the retry-history log. Falls back to the first non-empty line."""
    if not stderr:
        return "(no stderr)"
    for pat in _STDERR_INTEREST_PATTERNS:
        m = pat.search(stderr)
        if m:
            return m.group(0).strip()[:200]
    # Fallback: first non-empty line
    for line in stderr.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return "(no stderr)"


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
        max_attempts: int = 3,
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

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        """Write ``content`` to ``target`` atomically.

        We write to ``<target>.anvil_tmp``, fsync, then ``os.replace``
        the temp into place. On POSIX and Windows (same drive) the
        replace is atomic — readers see either the old file or the
        new file, never a half-written one. A crash mid-attempt
        leaves the original target intact because the temp file is
        the one that may end up orphaned.
        """
        tmp = target.with_suffix(target.suffix + ".anvil_tmp")
        try:
            data = content.encode("utf-8")
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except Exception:
                    # fsync isn't supported on every fs; the os.replace
                    # below is still atomic, just less crash-durable.
                    pass
            os.replace(tmp, target)
        finally:
            # Clean up the orphan tmp if os.replace didn't get to it
            # (e.g. write raised before replace).
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    @staticmethod
    def _atomic_restore(target: Path, backup_bytes: bytes) -> None:
        """Restore ``target`` from ``backup_bytes`` atomically. Same
        write-tmp-then-os.replace pattern as ``_atomic_write``."""
        tmp = target.with_suffix(target.suffix + ".anvil_bak")
        try:
            with open(tmp, "wb") as fh:
                fh.write(backup_bytes)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except Exception:
                    pass
            os.replace(tmp, target)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

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
                # Build a compressed retry-history block so the model
                # can see which directions have already failed without
                # blowing the context budget on full failed code dumps.
                # The most recent attempt's full code is in {code};
                # everything earlier is a one-line "we tried X, got Y".
                history_lines = []
                for h in state.history[:-1]:
                    summary = (h.get("stderr_summary") or "(no stderr)")[:200]
                    history_lines.append(
                        f"  • attempt {h['attempt']}: {summary}"
                    )
                if history_lines:
                    history_block = (
                        "EARLIER ATTEMPTS (all failed — do not retry these "
                        "fixes):\n"
                        + "\n".join(history_lines)
                        + "\n\n"
                    )
                else:
                    history_block = ""
                # #17: feed the model the COMPRESSED error (the one
                # representative line) plus a short tail, not 2000 raw
                # chars of Godot noise — keeps the FIX prompt small
                # enough for an 8B to actually use it.
                _err_summary = _summarise_stderr(state.stderr or "")
                _err_tail = "\n".join(
                    (state.stderr or "").splitlines()[-8:])
                prompt = FIX_PROMPT_TEMPLATE.format(
                    task=task,
                    attempt=attempt - 1,
                    max_attempts=self.max_attempts,
                    code=state.code,
                    returncode=state.returncode,
                    stderr=(_err_summary + "\n---\n" + _err_tail)[:900],
                    history_block=history_block,
                    goal_header=_goal_header(goal),
                    goal_reminder=_goal_reminder(goal),
                )

            self._emit("generate",
                       f"Attempt {attempt}/{self.max_attempts} — writing GDScript…")
            try:
                raw = self.model.respond(prompt)
            except Exception as exc:
                state.stderr = f"model.respond crashed: {exc!r}"
                state.history.append({
                    "attempt": attempt, "code": "",
                    "stderr_summary": f"model.respond crashed: {exc!r}",
                })
                self._emit("result", f"Attempt {attempt} ✗ MODEL ERROR")
                continue

            state.code = _extract_code(raw)

            if not state.code.strip():
                state.stderr = "model returned empty code"
                state.history.append({
                    "attempt": attempt, "code": "",
                    "stderr_summary": "empty model output",
                })
                self._emit("result", f"Attempt {attempt} ✗ EMPTY")
                continue

            # Atomic write: write to <target>.anvil_tmp + os.replace.
            # A crash mid-write leaves the original file intact.
            try:
                self._atomic_write(target, state.code)
            except Exception as exc:
                state.stderr = f"could not write target: {exc!r}"
                state.history.append({
                    "attempt": attempt, "code": state.code,
                    "stderr_summary": f"write failed: {exc!r}",
                })
                self._emit("result", f"Attempt {attempt} ✗ WRITE FAIL")
                continue

            self._emit("validate",
                       f"Attempt {attempt} — godot --check-only…")
            rc, out, err = _check_only(self.godot_binary, self.project_root)
            state.returncode = rc
            state.stdout = out
            state.stderr = err
            state.passed = (rc == 0)
            # Compress this attempt's stderr into a single-line summary
            # for the next iteration's history block.
            stderr_summary = _summarise_stderr(err)
            state.history.append({
                "attempt": attempt, "code": state.code,
                "stderr_summary": stderr_summary,
            })
            self._emit(
                "result",
                f"Attempt {attempt} {'✓ PASSED' if state.passed else '✗ FAILED'} "
                f"(rc={rc})\n" + (err[:400] if err else ""),
            )
            if state.passed:
                state.final_code = state.code
                return state

        # ── All attempts failed — atomic roll back ────────────
        try:
            if existed and state.backup is not None:
                self._atomic_restore(target, state.backup)
                self._emit("rollback",
                           "All attempts failed — restored original file.")
            elif not existed and target.exists():
                target.unlink()
                self._emit("rollback",
                           "All attempts failed — removed partial file.")
        except Exception as exc:
            self._emit("rollback", f"rollback failed: {exc!r}")

        return state
