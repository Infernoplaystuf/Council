"""
council_core.build_sandbox — how the Build tab runs code: Run, Test, and the
test rounds the model starts after a fix.

THE SANDBOX (light, as agreed: plan step A2+B3, decision D4 "start light")
  * OFFLINE: council_core/_build_sandbox/ goes first on the child's
    PYTHONPATH; its sitecustomize installs an audit hook that refuses any
    connection or name lookup off this PC (council_build_sandbox.py says
    exactly what is covered). The refusal is printed in the output.
  * A JOB OBJECT on Windows (council_core.child_proc's): the whole process
    tree is killed by Stop, by the time limit and when the Council closes,
    and each process is capped at a memory limit, so a runaway loop gets a
    MemoryError instead of the PC's memory.
  * NO CONSOLE WINDOW, stdin closed, a time limit (RUN_TIMEOUT_S for Run,
    TEST_TIMEOUT_S for Test).
  * In a session the code runs in the session's copy, so what it writes
    lands there, not in the project folder.
It does not confine what the code reads or writes on disk; ide_trust asks
before Run when the code can affect the machine (as the IDE tab did).

PREVIEW (widget_classes, module_name)
The open file's first Qt widget class is built offscreen by council_core.
gui_check (a child process with a time limit and a memory cap): a picture
of it, its widget tree, and any error, which "Fix this" sends to the chat.

TESTS (has_tests, test_argv, TestOutcome)
`python -m pytest -q` with the "Run with" Python, in the folder, when the
folder has tests (a tests/ folder, test_*.py or *_test.py files, or a
pytest/tox config). Its result is a TestOutcome: passed, the output, and
the failure's signature.

STOP WHEN STUCK (signature, stuck)
A failure's signature is what stays the same when the same bug fails again:
pytest's FAILED/ERROR lines, else the last exception line, with addresses,
temp paths and timings taken out. Two rounds in a row with the same signature
mean the fix did not change anything: the loop stops and the model is asked
for a blocker instead of another fix.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import child_proc

HOOK_DIR = Path(__file__).resolve().parent / "_build_sandbox"
RUN_TIMEOUT_S = 30 * 60
TEST_TIMEOUT_S = 5 * 60
RUN_MEMORY_MB = 2048
TEST_MEMORY_MB = 4096
#: Rounds the model may run the tests by itself after a fix.
MAX_ROUNDS = 3


@dataclass
class Outcome:
    returncode: Optional[int]
    seconds: float
    stopped: bool = False
    timed_out: bool = False
    error: str = ""
    sandboxed: bool = False      # ran inside a Job Object (Windows)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not (self.stopped or self.timed_out or self.error)

    @property
    def failed(self) -> bool:
        """It ran and failed (not stopped by the user)."""
        return not self.ok and not self.stopped

    def summary(self, name: str) -> str:
        if self.error:
            return f"[{name} did not start: {self.error}]"
        if self.stopped:
            return f"[{name} stopped after {self.seconds:.1f} s]"
        if self.timed_out:
            return f"[{name} ran longer than its limit and was stopped after {self.seconds:.0f} s]"
        return f"[{name} exited with code {self.returncode} after {self.seconds:.1f} s]"


def sandbox_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The child's environment: the sandbox's folder first on PYTHONPATH
    (what was there stays after it), output unbuffered and in utf-8, and
    nothing that would make Python load a startup file of someone else's."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT")}
    rest = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join([str(HOOK_DIR)] + rest)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                "COUNCIL_SANDBOX": "1", "COUNCIL_SANDBOX_OFFLINE": "1",
                "COUNCIL_SANDBOX_MERGE": "1"})
    env.update(extra or {})
    return env


def run(argv: Sequence[str], cwd: Path, on_line: Callable[[str], None],
        stop: Optional[threading.Event] = None, *,
        timeout: float = RUN_TIMEOUT_S, memory_mb: int = RUN_MEMORY_MB,
        env: Optional[Dict[str, str]] = None) -> Outcome:
    """Run ``argv`` in the sandbox, calling ``on_line`` with each line of its
    output (stdout and stderr in order). Returns when it exits, ``stop`` is
    set or the time limit passes (the whole tree is then killed)."""
    windows = sys.platform == "win32"
    job = child_proc._Job(memory_mb) if windows else None          # noqa: SLF001
    flags = 0
    if windows:
        flags = child_proc._CREATE_NO_WINDOW                       # noqa: SLF001
        if job is not None and job.handle and child_proc._NTDLL is not None:  # noqa: SLF001
            flags |= child_proc._CREATE_SUSPENDED                  # noqa: SLF001
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(argv), cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env or sandbox_env(),
            creationflags=flags, start_new_session=not windows)
    except OSError as exc:
        if job is not None:
            job.close()
        return Outcome(None, 0.0, error=str(exc.strerror or exc))
    out = Outcome(None, 0.0)
    if windows and flags & child_proc._CREATE_SUSPENDED:          # noqa: SLF001
        out.sandboxed = job.assign(proc)
        child_proc._resume(proc)                                   # noqa: SLF001

    def drain() -> None:
        for line in proc.stdout:
            try:
                on_line(line.rstrip("\n"))
            except Exception:                             # noqa: BLE001
                pass
        proc.stdout.close()

    reader = threading.Thread(target=drain, name="build-run-out", daemon=True)
    reader.start()
    while proc.poll() is None:
        if stop is not None and stop.wait(0.1):
            out.stopped = True
            break
        if stop is None:
            time.sleep(0.1)
        if timeout and time.monotonic() - started > timeout:
            out.timed_out = True
            break
    if out.stopped or out.timed_out:
        child_proc._kill_tree(proc, job)                           # noqa: SLF001
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    if job is not None:
        if job.handle and job.active():
            job.kill()                     # what it left running goes too
        job.close()
    reader.join(timeout=5)
    out.returncode = proc.returncode
    out.seconds = time.monotonic() - started
    return out


# ── tests ──────────────────────────────────────────────────────────────
_TEST_FILE = re.compile(r"^(test_.*|.*_test)\.py$")


def has_tests(folder: Path) -> bool:
    """Whether ``folder`` looks like it has pytest tests (looked for near
    the top only: a quick check, not a walk)."""
    folder = Path(folder)
    for name in ("pytest.ini", "tox.ini", "conftest.py"):
        if (folder / name).is_file():
            return True
    for sub in (folder, folder / "tests", folder / "test"):
        try:
            if any(_TEST_FILE.match(p.name) for p in sub.iterdir() if p.is_file()):
                return True
        except OSError:
            continue
    try:
        text = (folder / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
        return "[tool.pytest" in text
    except OSError:
        return False


def test_argv(python: str, target: str = "") -> List[str]:
    argv = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no"]
    return argv + ([target] if target else [])


@dataclass
class TestOutcome:
    outcome: Outcome
    output: str

    @property
    def passed(self) -> bool:
        return self.outcome.ok

    def signature(self) -> str:
        return signature(self.output)

    def headline(self) -> str:
        """pytest's last summary line ("2 failed, 5 passed in 0.31s")."""
        for ln in reversed(self.output.splitlines()):
            s = ln.strip().strip("=").strip()
            if re.search(r"\b(passed|failed|error|errors|no tests ran)\b", s):
                return s
        return self.outcome.summary("The tests")

    def missing_pytest(self) -> bool:
        return "No module named pytest" in self.output


# ── stop when stuck ─────────────────────────────────────────────────────
_NOISE = [(re.compile(r"0x[0-9a-fA-F]+"), "0x…"),
          (re.compile(r"\b\d+(\.\d+)?s\b"), "…s"),
          (re.compile(r"\S*[\\/](?:tmp|Temp|pytest-of-[^\\/\s]+)[\\/]\S*"), "<tmp>"),
          (re.compile(r"line \d+"), "line …")]
_EXC = re.compile(r"^\s*(?:E\s+)?([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)|"
                  r"AssertionError|StopIteration)\b:?(.*)$")


def _clean(s: str) -> str:
    for rx, sub in _NOISE:
        s = rx.sub(sub, s)
    return " ".join(s.split())


def signature(output: str) -> str:
    """What stays the same when the same failure happens again: pytest's
    FAILED/ERROR lines (sorted), else the last exception line, else the
    last line; addresses, temp paths, timings and line numbers taken out.
    "" for empty output."""
    lines = [ln.rstrip() for ln in (output or "").splitlines() if ln.strip()]
    failed = sorted({_clean(ln) for ln in lines
                     if ln.startswith(("FAILED ", "ERROR "))})
    if failed:
        return "\n".join(failed)
    for ln in reversed(lines):
        m = _EXC.match(ln)
        if m:
            return _clean(f"{m.group(1)}:{m.group(2)}")
    return _clean(lines[-1]) if lines else ""


def stuck(signatures: List[str]) -> bool:
    """The last two rounds failed the same way."""
    return len(signatures) >= 2 and bool(signatures[-1]) and \
        signatures[-1] == signatures[-2]


# ── Preview ────────────────────────────────────────────────────────────
_WIDGET_BASES = re.compile(r"(Widget|Window|Dialog|Frame|View|Wizard|Box)$")


def widget_classes(text: str) -> List[str]:
    """The top-level classes in Python ``text`` that look like Qt widgets
    (a base named Q…, …Widget, …Window, …Dialog …), in file order."""
    import ast
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for b in node.bases:
            name = b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "")
            if name and ((name.startswith("Q") and name[1:2].isupper())
                         or _WIDGET_BASES.search(name)):
                out.append(node.name)
                break
    return out


def module_name(root: Path, path: Path) -> Optional[str]:
    """``path`` as a module importable from ``root`` ("pkg.view"), or None
    when it is outside ``root`` or not a .py file."""
    try:
        rel = Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return None
    if rel.suffix != ".py" or not all(p.isidentifier() for p in rel.with_suffix("").parts):
        return None
    return ".".join(rel.with_suffix("").parts)


__all__ = ["widget_classes", "module_name", "Outcome", "TestOutcome", "run", "sandbox_env", "has_tests",
           "test_argv", "signature", "stuck", "HOOK_DIR", "MAX_ROUNDS",
           "RUN_TIMEOUT_S", "TEST_TIMEOUT_S"]
