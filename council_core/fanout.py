"""
council_core.fanout — several coding agents on one task, side by side.

The way a lead engineer splits a change across a team, or the way Claude Code
hands parts of a large task to helper agents:

  1. PLAN     The planner (the Judge's model, unless model_slots.json gives a
              "planner" role its own) reads an index of the code folder and
              splits the task into UNITS. Every file belongs to at most one
              unit, so no two workers can touch the same file — their work
              cannot conflict. `check_plan` enforces that, and the user
              approves the split before anything runs.
  2. WORK     Each unit gets its own copy of the code, and a coder works on it
              — on this PC or on a machine set up in Machines & roles, spread
              round-robin, each machine running only as many at once as it
              allows (node_routing.host_slot). A worker may write ONLY its
              unit's files; anything else it returns is refused. Its Python
              files must compile; a syntax error goes back to it
              (MAX_ATTEMPTS tries).
  3. COMBINE  All units' files go into one more copy and the full tests run
              there — a unit's own copy cannot pass tests that need another
              unit's code. When they fail, a FIX ROUND sends the failure to
              every worker at once; each fixes only its own files, written
              straight into the combined copy (no two share a file), and the
              tests run again (MAX_FIX_ROUNDS).
  4. REVIEW   The Judge reads the combined change and the test result.
  5. PATCH    <vault>/.council_fanout/<job>/fanout.patch — a unified diff for
              `git apply`. The user's folder is NEVER written: the Council
              does not overwrite user files; the user applies the patch.

RUNNING CODE
Tests run the workers' code, on this PC, in the copies. That happens only when
the user gives a test command (it is their command, split with shlex and run
without a shell, with a time limit). With no test command nothing is run.

No Qt. `chat` is injectable (council_engine.local_chat by default) so a test
can drive the whole job without a model.
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import node_routing

DIR_NAME = ".council_fanout"
MAX_UNITS = 8
MAX_ATTEMPTS = 3
MAX_FIX_ROUNDS = 2
MAX_UNIT_CHARS = 40_000          # a unit's files, together, for one worker
MAX_FILE_CHARS = 200_000         # a file larger than this is never indexed
INDEX_CHARS = 12_000             # the index the planner reads
TEST_TIMEOUT_S = 300
OUTPUT_TAIL = 3_000
MAX_COPY_BYTES = 300 * 1024 * 1024
PLANNER = "planner"

CODE_SUFFIXES = {".py", ".pyi", ".js", ".ts", ".tsx", ".jsx", ".json",
                 ".toml", ".cfg", ".ini", ".yaml", ".yml", ".md", ".txt",
                 ".html", ".css", ".sh", ".bat", ".c", ".h", ".cpp", ".hpp",
                 ".rs", ".go", ".java", ".cs"}
SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
             "node_modules", "dist", "build", ".mypy_cache", ".pytest_cache",
             ".tox", ".idea", ".vscode", DIR_NAME}


class FanoutError(RuntimeError):
    pass


# ============================================================
# The index the planner reads
# ============================================================

def _skip(rel: PurePosixPath) -> bool:
    return any(part in SKIP_DIRS or part.startswith(".")
               for part in rel.parts[:-1])


def code_files(repo: Path) -> List[str]:
    """Every code file under `repo`, as forward-slash relative paths."""
    out = []
    for path in sorted(Path(repo).rglob("*")):
        if not path.is_file() or path.suffix.lower() not in CODE_SUFFIXES:
            continue
        rel = PurePosixPath(path.relative_to(repo).as_posix())
        if _skip(rel):
            continue
        try:
            if path.stat().st_size > MAX_FILE_CHARS:
                continue
        except OSError:
            continue
        out.append(str(rel))
    return out


def _outline(path: Path) -> str:
    """Top-level classes and functions of a Python file, one line each."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return ""
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(a.arg for a in node.args.args)
            names.append(f"def {node.name}({args})")
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body
                       if isinstance(n, (ast.FunctionDef,
                                         ast.AsyncFunctionDef))]
            names.append(f"class {node.name}: {', '.join(methods[:12])}")
    return "; ".join(names)


def index(repo: Path, limit: int = INDEX_CHARS) -> str:
    """The folder as the planner sees it: each file, its size, and (Python)
    its top-level names — cut at `limit` characters."""
    lines = []
    for rel in code_files(repo):
        path = Path(repo) / rel
        try:
            n = len(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        outline = _outline(path) if rel.endswith(".py") else ""
        lines.append(f"{rel} ({n} chars)" + (f": {outline}" if outline
                                              else ""))
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + "\n… (index cut — the folder is larger)"
    return text


# ============================================================
# The plan
# ============================================================

PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "contract": {"type": "string"},
        "units": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"},
                           "title": {"type": "string"},
                           "files": {"type": "array",
                                     "items": {"type": "string"}},
                           "new_files": {"type": "array",
                                         "items": {"type": "string"}},
                           "instructions": {"type": "string"}},
            "required": ["id", "title", "files", "instructions"]}},
    },
    "required": ["summary", "contract", "units"],
}

PLAN_INSTRUCTIONS = """You lead a team of coding agents. Split the task into \
independent UNITS that can be done at the same time by different people.

Rules:
- Every file belongs to at most ONE unit. Two units never share a file.
- Prefer few, small units: at most {max_units}, each a few files.
- "files" are existing files the unit may change; "new_files" are files it \
creates. Use paths exactly as the index writes them.
- "contract" says what the units agree on: the names, arguments and return \
values one unit's code calls in another's. Each worker sees only its own \
files and the contract, so the contract must be complete.
- If the task cannot be split, make one unit.

Reply with JSON only: {{"summary": "...", "contract": "...", "units": \
[{{"id": "u1", "title": "...", "files": [...], "new_files": [...], \
"instructions": "..."}}]}}"""


@dataclass
class Unit:
    id: str
    title: str
    files: List[str]
    new_files: List[str] = field(default_factory=list)
    instructions: str = ""
    why_not: str = ""

    @property
    def owned(self) -> List[str]:
        return list(self.files) + list(self.new_files)


@dataclass
class Plan:
    summary: str
    contract: str
    units: List[Unit]
    rejected: List[Unit] = field(default_factory=list)


def _clean_rel(raw: str) -> Optional[str]:
    """A safe relative path, or None: no absolute paths, no '..', no
    drive letters, nothing in a skipped folder."""
    p = str(raw or "").strip().replace("\\", "/")
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return None
    rel = PurePosixPath(p)
    if any(part in ("", ".", "..") for part in rel.parts) or _skip(rel):
        return None
    return str(rel)


def check_plan(raw: Dict[str, Any], repo: Path,
               max_units: int = MAX_UNITS) -> Plan:
    """The planner's JSON as a Plan whose units can safely run at once.
    A unit that names a missing file, an unsafe path, a file another unit
    already owns, or too much code is REJECTED with the reason."""
    existing = set(code_files(repo))
    plan = Plan(str(raw.get("summary") or ""), str(raw.get("contract") or ""),
                [])
    owned: Dict[str, str] = {}
    seen_ids = set()
    for i, item in enumerate(raw.get("units") or []):
        if not isinstance(item, dict):
            continue
        uid = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("id") or "")) \
            or f"u{i + 1}"
        while uid in seen_ids:
            uid += "x"
        seen_ids.add(uid)
        unit = Unit(uid, str(item.get("title") or uid), [], [],
                    str(item.get("instructions") or ""))
        for key, must_exist in (("files", True), ("new_files", False)):
            for raw_path in item.get(key) or []:
                rel = _clean_rel(raw_path)
                if rel is None:
                    unit.why_not = f"unsafe path {raw_path!r}"
                elif must_exist and rel not in existing:
                    unit.why_not = f"{rel} is not in the folder"
                elif not must_exist and (Path(repo) / rel).exists():
                    unit.why_not = f"{rel} already exists (list it in files)"
                elif rel in owned:
                    unit.why_not = f"{rel} is already in unit {owned[rel]}"
                else:
                    (unit.files if must_exist else unit.new_files).append(rel)
                if unit.why_not:
                    break
            if unit.why_not:
                break
        if not unit.why_not and not unit.owned:
            unit.why_not = "it names no files"
        if not unit.why_not:
            size = sum(len((Path(repo) / f).read_text(
                encoding="utf-8", errors="replace")) for f in unit.files)
            if size > MAX_UNIT_CHARS:
                unit.why_not = (f"its files hold {size} characters — more "
                                f"than one worker can read ({MAX_UNIT_CHARS})")
        if not unit.why_not and len(plan.units) >= max_units:
            unit.why_not = f"more than {max_units} units"
        if unit.why_not:
            plan.rejected.append(unit)
            continue
        for rel in unit.owned:
            owned[rel] = unit.id
        plan.units.append(unit)
    return plan


def planner_role(slots: Any = None) -> str:
    if slots is None:
        try:
            from . import model_slots
            slots = model_slots.current()
        except Exception:                                 # noqa: BLE001
            return "judge"
    return PLANNER if PLANNER in (getattr(slots, "roles", {}) or {}) \
        else "judge"


def _parse_json(text: str, what: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    try:
        obj = json.loads(raw)
    except ValueError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise FanoutError(f"the {what} did not reply with JSON")
        try:
            obj = json.loads(match.group(0))
        except ValueError as exc:
            raise FanoutError(f"the {what}'s JSON is broken: {exc}")
    if not isinstance(obj, dict):
        raise FanoutError(f"the {what}'s reply is not a JSON object")
    return obj


def make_plan(task: str, repo: Path, *, chat: Optional[Callable] = None,
              max_units: int = MAX_UNITS, role: Optional[str] = None) -> Plan:
    chat = chat or _default_chat()
    text = chat([{"role": "system",
                  "content": PLAN_INSTRUCTIONS.format(max_units=max_units)},
                 {"role": "user",
                  "content": f"TASK:\n{task}\n\nTHE FOLDER:\n{index(repo)}"}],
                role=role or planner_role(), json_schema=PLAN_SCHEMA,
                num_predict=2000, temperature=0.1, timeout=300)
    return check_plan(_parse_json(text, "planner"), repo, max_units)


def _default_chat() -> Callable:
    import council_engine
    return council_engine.local_chat


# ============================================================
# Workspaces
# ============================================================

def _ignore(_dir: str, names: List[str]) -> List[str]:
    return [n for n in names if n in SKIP_DIRS]


def copy_repo(repo: Path, dest: Path) -> Path:
    """A working copy of `repo` (skipping .git, caches, venvs). Refuses a
    folder over MAX_COPY_BYTES rather than filling the disk."""
    total = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
        if total > MAX_COPY_BYTES:
            raise FanoutError(
                f"{repo} is over {MAX_COPY_BYTES // 2**20} MB without .git and "
                "caches; pick a smaller folder.")
    shutil.copytree(repo, dest, ignore=_ignore)
    return dest


def _write_owned(workdir: Path, unit: Unit,
                 files: Sequence[Dict[str, Any]]) -> Tuple[List[str],
                                                           List[str]]:
    """Write the worker's files into its copy — only the unit's own.
    Returns (written, refused)."""
    allowed = set(unit.owned)
    written, refused = [], []
    for f in files:
        rel = _clean_rel(str(f.get("path", "")))
        content = f.get("content")
        if rel is None or rel not in allowed or not isinstance(content, str):
            refused.append(str(f.get("path", "")))
            continue
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".fanout-tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
        written.append(rel)
    return written, refused


def run_tests(command: str, workdir: Path,
              timeout: int = TEST_TIMEOUT_S) -> Tuple[bool, str]:
    """The user's test command in `workdir`: (passed, output tail). No
    shell; a command that cannot start or runs too long fails."""
    if not command.strip():
        return True, "(no test command)"
    try:
        argv = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as exc:
        return False, f"the test command cannot be read: {exc}"
    if argv and argv[0] in ("python", "python3", "py"):
        argv[0] = sys.executable
    # No bytecode cache: a fix round rewrites a file within the same second,
    # and when the new file has the same size (`a + b` → `a * b`) Python's
    # mtime+size check keeps running the stale cached version.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        done = subprocess.run(argv, cwd=str(workdir), capture_output=True,
                              text=True, timeout=timeout, env=env,
                              errors="replace")
    except subprocess.TimeoutExpired:
        return False, f"the tests ran longer than {timeout} s and were stopped"
    except OSError as exc:
        return False, f"the test command could not start: {exc}"
    out = (done.stdout or "") + (done.stderr or "")
    return done.returncode == 0, out[-OUTPUT_TAIL:]


def unified_diff(repo: Path, workdir: Path, paths: Sequence[str]) -> str:
    """git-apply-able diff of `paths` between the folder and a copy."""
    chunks = []
    for rel in sorted(set(paths)):
        old_path, new_path = Path(repo) / rel, Path(workdir) / rel
        old = old_path.read_text(encoding="utf-8", errors="replace") \
            if old_path.exists() else ""
        new = new_path.read_text(encoding="utf-8", errors="replace") \
            if new_path.exists() else ""
        if old == new:
            continue
        diff = difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile="/dev/null" if not old_path.exists() else f"a/{rel}",
            tofile=f"b/{rel}")
        text = "".join(line if line.endswith("\n") else
                       line + "\n\\ No newline at end of file\n"
                       for line in diff)
        if not old_path.exists():
            text = f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n" + text
        else:
            text = f"diff --git a/{rel} b/{rel}\n" + text
        chunks.append(text)
    return "".join(chunks)


# ============================================================
# Workers
# ============================================================

WORK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "files": {"type": "array", "items": {
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "content": {"type": "string"}},
            "required": ["path", "content"]}},
        "notes": {"type": "string"},
    },
    "required": ["files"],
}

WORK_INSTRUCTIONS = """You are one of several coding agents working at the \
same time on different parts of one change. Do ONLY your unit.

Return the COMPLETE new content of every file you change or create — whole \
files, not fragments. You may only write the files listed as yours. Keep to \
the contract exactly: other agents are writing the other side of it right \
now and cannot see your code.

Reply with JSON only: {"files": [{"path": "...", "content": "..."}], \
"notes": "..."}"""


@dataclass
class Target:
    name: str                      # "This PC" or the routing machine name
    host: Optional[str]            # None = this PC's own path


def worker_targets(routing: Any = None) -> List[Target]:
    """This PC plus every machine set up in Machines & roles."""
    out = [Target("This PC", None)]
    for node in node_routing.enabled_nodes(routing):
        out.append(Target(node.name, node.url))
    return out


@dataclass
class UnitResult:
    unit: str
    title: str
    machine: str = ""
    status: str = "waiting"        # waiting|working|checking|done|failed|error
    attempts: int = 0
    written: List[str] = field(default_factory=list)
    refused: List[str] = field(default_factory=list)
    tests_passed: Optional[bool] = None
    test_output: str = ""
    notes: str = ""
    error: str = ""
    seconds: float = 0.0
    fixed_in_round: int = 0


def _unit_prompt(task: str, plan: Plan, unit: Unit, workdir: Path,
                 feedback: str = "") -> str:
    parts = [f"THE WHOLE TASK:\n{task}", f"THE CONTRACT:\n{plan.contract}",
             f"YOUR UNIT ({unit.id}): {unit.title}\n{unit.instructions}",
             "YOUR FILES: " + ", ".join(unit.owned)]
    others = [f"{u.id}: {', '.join(u.owned)}" for u in plan.units
              if u.id != unit.id]
    if others:
        parts.append("OTHER AGENTS OWN (do not write these):\n"
                     + "\n".join(others))
    for rel in unit.files:
        text = (workdir / rel).read_text(encoding="utf-8", errors="replace")
        parts.append(f"--- {rel} (current) ---\n{text}")
    for rel in unit.new_files:
        if (workdir / rel).exists():
            text = (workdir / rel).read_text(encoding="utf-8",
                                             errors="replace")
            parts.append(f"--- {rel} (your draft so far) ---\n{text}")
        else:
            parts.append(f"--- {rel} (new — you create it) ---")
    if feedback:
        parts.append(f"YOUR LAST TRY DID NOT WORK:\n{feedback}\n"
                     "Fix it and return the files again.")
    return "\n\n".join(parts)


def compile_errors(workdir: Path, paths: Sequence[str]) -> str:
    """Syntax errors in the written Python files, or "" — checked without
    running anything."""
    errors = []
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        try:
            compile((Path(workdir) / rel).read_text(encoding="utf-8"), rel,
                    "exec", dont_inherit=True)
        except SyntaxError as exc:
            errors.append(f"{rel}, line {exc.lineno}: {exc.msg}")
        except (OSError, ValueError) as exc:
            errors.append(f"{rel}: {exc}")
    return "\n".join(errors)


FIX_INSTRUCTIONS = """You are one of several coding agents. Everyone's work has been put together and the tests FAILED. Read the failure. If it involves your files, return the COMPLETE fixed content of the files you change; if it does not involve your files, return an empty list. You may only write your own files.

Reply with JSON only: {"files": [{"path": "...", "content": "..."}], "notes": "..."}"""


def fix_unit(task: str, plan: Plan, unit: Unit, combined: Path,
             target: Target, failure: str, *, chat: Callable,
             should_stop: Optional[Callable[[], bool]] = None,
             role: str = "coder") -> Tuple[List[str], List[str]]:
    """One worker's turn in a fix round, on the COMBINED copy. Returns
    (written, refused)."""
    prompt = _unit_prompt(task, plan, unit, combined) + \
        f"\n\nTHE COMBINED TESTS FAILED:\n{failure}"
    text = _chat_on(chat, target, [
        {"role": "system", "content": FIX_INSTRUCTIONS},
        {"role": "user", "content": prompt}],
        role=role, json_schema=WORK_SCHEMA,
        num_predict=_reply_tokens(unit, combined), temperature=0.2,
        timeout=300, should_stop=should_stop)
    files = _parse_json(text, "worker").get("files") or []
    before = {rel: (combined / rel).read_text(encoding="utf-8")
              if (combined / rel).exists() else None for rel in unit.owned}
    written, refused = _write_owned(combined, unit, files)
    broken = compile_errors(combined, written)
    if broken:
        # A fix that does not compile is worse than none: put the unit's
        # previous files back.
        for rel in written:
            if before.get(rel) is None:
                (combined / rel).unlink(missing_ok=True)
            else:
                (combined / rel).write_text(before[rel], encoding="utf-8")
        refused.append(f"(a fix that does not compile: {broken})")
        written = []
    return written, refused


def _reply_tokens(unit: Unit, workdir: Path) -> int:
    chars = sum(len((workdir / f).read_text(encoding="utf-8",
                                            errors="replace"))
                for f in unit.files if (workdir / f).exists())
    return max(1500, min(8192, int(chars / 3 * 1.3) + 800))


def run_unit(task: str, plan: Plan, unit: Unit, workdir: Path,
             target: Target, *, test_command: str = "",
             chat: Optional[Callable] = None,
             should_stop: Optional[Callable[[], bool]] = None,
             on_update: Optional[Callable[[UnitResult], None]] = None,
             role: str = "coder") -> UnitResult:
    """One worker: write → test → fix, up to MAX_ATTEMPTS, in `workdir`."""
    chat = chat or _default_chat()
    result = UnitResult(unit.id, unit.title, target.name)
    t0 = time.monotonic()

    def update(status: str) -> None:
        result.status = status
        result.seconds = round(time.monotonic() - t0, 1)
        if on_update is not None:
            on_update(result)

    feedback = ""
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if should_stop is not None and should_stop():
                raise FanoutError("stopped")
            result.attempts = attempt
            update("working")
            text = _chat_on(chat, target, [
                {"role": "system", "content": WORK_INSTRUCTIONS},
                {"role": "user", "content": _unit_prompt(
                    task, plan, unit, workdir, feedback)}],
                role=role, json_schema=WORK_SCHEMA,
                num_predict=_reply_tokens(unit, workdir), temperature=0.2,
                timeout=300, should_stop=should_stop)
            reply = _parse_json(text, "worker")
            written, refused = _write_owned(workdir, unit,
                                            reply.get("files") or [])
            result.written = sorted(set(result.written) | set(written))
            result.refused += refused
            result.notes = str(reply.get("notes") or "")
            if not written:
                feedback = ("You returned none of your files"
                            + (f" (refused: {', '.join(refused)})"
                               if refused else "") + ".")
                continue
            update("checking")
            broken = compile_errors(workdir, written)
            if not broken:
                update("done")
                return result
            feedback = broken
        update("failed")
    except Exception as exc:                              # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
        update("error")
    return result


def _chat_on(chat: Callable, target: Target, messages, **kw) -> str:
    """Ask on the worker's machine; if that machine does not answer before
    replying anything, rest it and ask on this PC instead."""
    if target.host is None:
        return chat(messages, **kw)
    if node_routing.cooling(target.host) > 0:
        return chat(messages, **kw)
    try:
        text = chat(messages, host=target.host, **kw)
        node_routing.mark_ok(target.host)
        return text
    except Exception as exc:                              # noqa: BLE001
        if getattr(exc, "partial", ""):
            raise
        name = type(exc).__name__
        if name in ("GenerationCancelled",):
            raise
        node_routing.mark_failed(target.host)
        return chat(messages, **kw)


# ============================================================
# The job
# ============================================================

REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"verdict": {"type": "string",
                               "enum": ["approve", "needs_work"]},
                   "summary": {"type": "string"},
                   "concerns": {"type": "array",
                                "items": {"type": "string"}}},
    "required": ["verdict", "summary", "concerns"],
}

REVIEW_INSTRUCTIONS = """Several coding agents each changed different files \
for one task. Review the combined change: does it do the task, do the parts \
fit the contract and each other, did the tests pass, is anything risky? \
Reply with JSON only: {"verdict": "approve" | "needs_work", "summary": "...", \
"concerns": ["..."]}"""

REVIEW_DIFF_CHARS = 14_000


@dataclass
class JobResult:
    id: str
    workdir: str
    units: List[UnitResult]
    tests_passed: Optional[bool] = None
    test_output: str = ""
    review: Dict[str, Any] = field(default_factory=dict)
    fix_rounds: int = 0
    patch: str = ""                # path of fanout.patch
    changed: List[str] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0

    def text(self) -> str:
        L = [f"Fan-out job {self.id} — {self.seconds:.0f} s", ""]
        for u in self.units:
            L.append(f"  {u.unit} {u.title}: {u.status} on {u.machine} "
                     f"({u.attempts} tr{'y' if u.attempts == 1 else 'ies'}"
                     f", {u.seconds:.0f} s)"
                     + (f" — {u.error}" if u.error else "")
                     + (f" — refused writes: "
                        f"{', '.join(dict.fromkeys(u.refused))}"
                        if u.refused else ""))
        L.append("")
        if self.error:
            L.append(f"Stopped: {self.error}")
        if self.tests_passed is not None:
            L.append("Combined tests: " + ("PASSED" if self.tests_passed
                                           else "FAILED")
                     + (f" after {self.fix_rounds} fix round(s)"
                        if self.fix_rounds else ""))
        if self.review:
            L += ["", f"Judge: {self.review.get('verdict', '?').upper()} — "
                  f"{self.review.get('summary', '')}"]
            L += [f"  • {c}" for c in self.review.get("concerns") or []]
        if self.patch:
            L += ["", f"Patch ({len(self.changed)} file(s)): {self.patch}",
                  "Apply it yourself in your folder: git apply "
                  f"\"{self.patch}\" — the Council never writes there."]
        return "\n".join(L)


def jobs_dir(vault_dir: Path) -> Path:
    return Path(vault_dir) / DIR_NAME


def run_job(task: str, repo: Path, plan: Plan, vault_dir: Path, *,
            test_command: str = "", chat: Optional[Callable] = None,
            targets: Optional[Sequence[Target]] = None,
            should_stop: Optional[Callable[[], bool]] = None,
            on_update: Optional[Callable[[UnitResult], None]] = None,
            review: bool = True) -> JobResult:
    """Run an approved plan end to end. Never writes to `repo`."""
    chat = chat or _default_chat()
    repo = Path(repo).resolve()
    t0 = time.monotonic()
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    work = jobs_dir(vault_dir) / job_id
    # Checked BEFORE anything is created: the job folder must not be in the
    # code folder, which the Council never writes to.
    if work.resolve().is_relative_to(repo) or \
            repo.is_relative_to(jobs_dir(vault_dir).resolve()):
        raise FanoutError("the job folder would be inside the code folder "
                          "(or the other way round); keep the vault outside "
                          "the code folder")
    work.mkdir(parents=True)
    targets = list(targets or worker_targets())
    result = JobResult(job_id, str(work), [])
    lock = threading.Lock()

    def updated(r: UnitResult) -> None:
        if on_update is not None:
            with lock:
                on_update(r)

    try:
        dirs = {u.id: copy_repo(repo, work / u.id) for u in plan.units}
        calls = [
            (lambda u=u, t=targets[i % len(targets)]: run_unit(
                task, plan, u, dirs[u.id], t, test_command=test_command,
                chat=chat, should_stop=should_stop, on_update=updated))
            for i, u in enumerate(plan.units)]
        outcomes = node_routing.run_parallel(calls)
        result.units = [o.value if o.ok else UnitResult(
            u.id, u.title, status="error", error=str(o.error))
            for o, u in zip(outcomes, plan.units)]

        combined = copy_repo(repo, work / "combined")
        for unit, r in zip(plan.units, result.units):
            for rel in r.written:
                src, dst = dirs[unit.id] / rel, combined / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                result.changed.append(rel)
        result.changed.sort()
        if result.changed:
            result.tests_passed, result.test_output = run_tests(
                test_command, combined)
            fixable = [(i, u) for i, (u, r) in enumerate(
                zip(plan.units, result.units)) if r.written]
            while (result.tests_passed is False and fixable
                   and result.fix_rounds < MAX_FIX_ROUNDS
                   and not (should_stop and should_stop())):
                result.fix_rounds += 1
                failure = result.test_output
                fixes = node_routing.run_parallel([
                    (lambda i=i, u=u: fix_unit(
                        task, plan, u, combined, targets[i % len(targets)],
                        failure, chat=chat, should_stop=should_stop))
                    for i, u in fixable])
                touched = False
                for (i, u), o in zip(fixable, fixes):
                    r = result.units[i]
                    if o.ok:
                        written, refused = o.value
                        r.refused += refused
                        if written:
                            touched = True
                            r.fixed_in_round = result.fix_rounds
                            result.changed = sorted(set(result.changed)
                                                    | set(written))
                    else:
                        r.error = f"fix round: {o.error}"
                if not touched:
                    break
                result.tests_passed, result.test_output = run_tests(
                    test_command, combined)
            diff = unified_diff(repo, combined, result.changed)
            patch = work / "fanout.patch"
            patch.write_text(diff, encoding="utf-8")
            result.patch = str(patch)
            if review and not (should_stop and should_stop()):
                result.review = _review(task, plan, diff, result, chat)
        else:
            result.error = "no worker produced any change"
    except Exception as exc:                              # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    result.seconds = round(time.monotonic() - t0, 1)
    (work / "job.json").write_text(json.dumps({
        "task": task, "repo": str(repo), "plan": asdict(plan),
        "result": asdict(result)}, indent=2), encoding="utf-8")
    return result


def _review(task: str, plan: Plan, diff: str, result: JobResult,
            chat: Callable) -> Dict[str, Any]:
    shown = diff if len(diff) <= REVIEW_DIFF_CHARS else \
        diff[:REVIEW_DIFF_CHARS] + "\n… (diff cut)"
    tests = ("no test command" if result.tests_passed is None or
             result.test_output == "(no test command)" else
             ("PASSED" if result.tests_passed else
              f"FAILED:\n{result.test_output[-1500:]}"))
    try:
        text = chat([{"role": "system", "content": REVIEW_INSTRUCTIONS},
                     {"role": "user", "content":
                      f"TASK:\n{task}\n\nCONTRACT:\n{plan.contract}\n\n"
                      f"TESTS: {tests}\n\nTHE CHANGE:\n{shown}"}],
                    role="judge", json_schema=REVIEW_SCHEMA,
                    num_predict=900, temperature=0.1, timeout=300)
        obj = _parse_json(text, "judge")
        return {"verdict": str(obj.get("verdict") or "?"),
                "summary": str(obj.get("summary") or ""),
                "concerns": [str(c) for c in obj.get("concerns") or []]}
    except Exception as exc:                              # noqa: BLE001
        return {"verdict": "?", "summary": f"the review failed: {exc}",
                "concerns": []}


__all__ = ["DIR_NAME", "MAX_UNITS", "MAX_ATTEMPTS", "FanoutError", "Unit",
           "Plan", "Target", "UnitResult", "JobResult", "PLAN_SCHEMA",
           "WORK_SCHEMA", "REVIEW_SCHEMA", "code_files", "index",
           "check_plan", "make_plan", "planner_role", "copy_repo",
           "run_tests", "unified_diff", "worker_targets", "run_unit",
           "fix_unit", "compile_errors", "MAX_FIX_ROUNDS",
           "run_job", "jobs_dir"]
