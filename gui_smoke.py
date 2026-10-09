"""
gui_smoke.py — run ONE model-written function (or handler) on sample data, in
a throwaway subprocess with a fence around it, and say exactly what went wrong.

WHY A RUN AND NOT ONLY A READ
gui_policy is a gate against code going wrong, not a correctness check, and it
says so. Measured against model-shaped handlers it PASSED all of these, each of
which fails (or does damage) at the user's first click:

    a port that does not exist            chart.set(fig) -> TypeError
    open(path, "w") on any path           shutil.move / os.rename
    while True: pass (a frozen window)    tkinter inside a Qt app

None is decidable by reading the source; all of them are obvious the moment
the code runs once. So the code-behind writer (gui_codebehind) runs every
candidate that passes its static gates here before a person is asked to
accept it, and the exact error text — with the logic.py / handlers.py line —
goes back to the model as the repair prompt.

THE FENCE
A sys.addaudithook in the child refuses, while the candidate runs: deleting,
moving or renaming files; writing anywhere outside the sandbox; changing the
working folder; starting a process; opening a socket or a URL; touching the
registry; and importing a GUI toolkit or council_engine. A refusal is recorded
AND raised, and a recorded refusal fails the run even when the candidate
catches the PermissionError and carries on — swallowing the refusal must not
turn it into a pass.

ctypes.dlopen is deliberately NOT fenced. numpy, Pillow and matplotlib load
native code lazily, and blocking it surfaced as "AttributeError: kernel32"
from deep inside matplotlib (measured on the prototype) — a misleading error
for the model to "fix". ctypes itself is refused statically by gui_policy.
For the same reason the heavy libraries a candidate names are imported BEFORE
the fence is armed, and bytecode writing is off (a .pyc beside a linked module
is a write outside the sandbox that nobody asked for).

WHAT IS FAKED
A linked module that declares COUNCIL_SMOKE_FAKE = True (frame_camera: it
opens camera SDKs) is replaced by a stand-in whose functions return their
documented result keys. Read with ast, like COUNCIL_REQUIRED_PORTS — the real
module is never imported to find out. In handler mode the window's ports are
fakes built from the project's real port table, with the real binders'
behaviour — including the chart port's writer, whose set() raises exactly the
TypeError the real _ProxyPort raises.

ANSWERS THROUGH A FILE, NOT STDOUT
The child writes its verdict to a JSON file in the sandbox, the lesson
python_envs.probe learned: a package printing while it imports, or a helper
process holding the pipe, made stdout markers unreliable there. The sandbox
is the one place the candidate MAY write, so the verdict carries a nonce the
parent hands the child on stdin (read before the candidate exists): a
verdict without it — one the candidate wrote, then left before the harness
did — is not believed.

THE FENCE NEVER COMES DOWN
It stays armed from before the candidate is imported until the process
ends: the returned value's repr() and the port checks call the candidate's
own __repr__/__str__, and those run fenced too. The child ends with
os._exit after the verdict is written, so exit handlers and finalizers the
candidate registered never run — there is no unfenced moment left for them.

Stdlib only, and the child half (``python gui_smoke.py <job.json>``) needs
nothing from the Council at all, so it runs under whichever interpreter the
project runs with — which is the one whose packages matter.
"""
from __future__ import annotations

import ast
import json
import os
import re
import secrets
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: A linked module sets this to True when merely running it in a smoke test
#: would reach hardware or open a window. Read with ast, never imported.
FAKE_MARKER = "COUNCIL_SMOKE_FAKE"

#: Wall-clock limit for one run, startup included. The prototype measured
#: ~1.2 s per candidate with numpy/PIL/matplotlib preloaded; a function on
#: three tiny sample frames that has not finished in this long would freeze
#: the window on real data.
DEFAULT_TIMEOUT = 8.0

#: A call slower than this on the sample data is reported (not failed): the
#: handler runs on the UI thread, so the window is frozen while it runs.
SLOW_CALL = 2.0

#: How much of what the candidate printed is kept for the report.
MAX_STDOUT = 2000
#: How much of a returned value's repr() is kept (SmokeResult.preview).
MAX_PREVIEW = 400

#: Heavy libraries, by the import root a candidate names, and what to import
#: before the fence goes up (see the module docstring).
PRELOAD = {
    "numpy": ("numpy",),
    "PIL": ("PIL.Image",),
    "pandas": ("pandas",),
    "matplotlib": ("matplotlib", "matplotlib.figure",
                   "matplotlib.backends.backend_agg"),
}

#: Tokens a sample argument may be, expanded in the child to real paths in
#: its sandbox. The parent cannot know the sandbox path in advance.
SAMPLE_TOKENS = ("<FOLDER>", "<PNG>", "<CSV>", "<TXT>", "<OUT>", "<SAVE>")

#: Value checks for what a port is about to be given ("expect" in a job).
CHECKS = ("text", "number", "bool", "list", "rows", "image", "any")


@dataclass
class SmokeResult:
    """What one smoke run found. ``ok`` is the verdict; the rest says why."""
    ok: bool = False
    #: False when the run could not start at all (no interpreter, no file).
    ran: bool = False
    #: "TypeError: ..." — the exception the candidate raised, if any.
    error: str = ""
    #: "logic.py:12" — where it was raised, in the candidate's own file.
    where: str = ""
    #: The source line at ``where``.
    line_text: str = ""
    #: Every action the fence refused.
    blocked: List[str] = field(default_factory=list)
    #: Problems with what the candidate produced (a list for a label...).
    problems: List[str] = field(default_factory=list)
    #: handler mode: port -> the type it was set to.
    sets: Dict[str, str] = field(default_factory=dict)
    #: function mode: the keys of the dict it returned.
    result_keys: List[str] = field(default_factory=list)
    #: function mode: repr() of what it returned on the sample data, cut to
    #: MAX_PREVIEW — the review shows it ("{'status': '3 PNG file(s)'...")
    #: and a grader can check the answer, not only its shape.
    preview: str = ""
    #: A deliberate refusal on the sample data (raise ValueError / an
    #: "error" key) — the documented failure path, so not a fault, but said.
    soft: str = ""
    stdout: str = ""
    seconds: float = 0.0          # the whole run, interpreter start included
    call_seconds: float = 0.0     # the candidate's own call
    timed_out: bool = False
    #: Why the run was not attempted (the interpreter is missing...).
    skipped: str = ""
    notes: List[str] = field(default_factory=list)

    def faults(self) -> List[str]:
        """Repair-ready lines: what to tell the model, most important first."""
        out: List[str] = []
        if self.timed_out:
            out.append(f"smoke run: it did not finish within "
                       f"{self.seconds:.0f} s on three small sample files — "
                       f"no waiting or endless loops; the window freezes "
                       f"while it runs")
        for b in self.blocked:
            out.append(f"smoke run blocked: {b}")
        if self.error and not self.timed_out:
            where = f" (at {self.where}" + (
                f": {self.line_text.strip()}" if self.line_text else "") + ")" \
                if self.where else ""
            out.append(f"smoke run raised {self.error}{where}")
        out.extend(f"smoke run: {p}" for p in self.problems)
        if not self.ran and not self.skipped and not out:
            out.append("smoke run could not start")
        return out

    def summary(self) -> str:
        """One line for a log or the review dialog."""
        if self.skipped:
            return f"smoke run skipped: {self.skipped}"
        if self.ok:
            what = (f"returned keys {', '.join(self.result_keys)}"
                    if self.result_keys else
                    (f"set {', '.join(sorted(self.sets))}" if self.sets
                     else "ran"))
            extra = f"; {self.soft}" if self.soft else ""
            shown = (f" — on the sample data it returned {self.preview}"
                     if self.preview else "")
            return (f"smoke run passed in {self.call_seconds:.2f} s "
                    f"({what}{extra}; {self.seconds:.1f} s with startup)"
                    f"{shown}")
        faults = self.faults()
        return "smoke run FAILED: " + ("; ".join(faults) if faults
                                       else "no verdict")

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


# ============================================================
# Parent side
# ============================================================

def preload_for(source: str) -> List[str]:
    """The heavy imports to make before the fence, from the import roots the
    candidate's source names. Only those: pandas alone is ~0.5 s."""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return []
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
    out: List[str] = []
    for root in ("numpy", "PIL", "pandas", "matplotlib"):
        if root in roots:
            out.extend(PRELOAD[root])
    return out


def imported_roots(source: str) -> List[str]:
    """Every import root ``source`` names, at any depth."""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return []
    roots: List[str] = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        for n in names:
            r = n.split(".")[0]
            if r not in roots:
                roots.append(r)
    return roots


def is_faked(module: str, root: Any = None) -> bool:
    """Whether ``module`` (a file under ``root``, default the Council's own
    folder) declares FAKE_MARKER = True. Parsed, never imported."""
    base = Path(root) if root else Path(__file__).resolve().parent
    rel = str(module).replace(".", "/")
    for p in (base / f"{rel}.py", base / rel / "__init__.py"):
        if p.is_file():
            try:
                tree = ast.parse(p.read_text(encoding="utf-8",
                                             errors="replace"))
            except (OSError, SyntaxError):
                return False
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == FAKE_MARKER
                        for t in node.targets):
                    return (isinstance(node.value, ast.Constant)
                            and node.value.value is True)
            return False
    return False


def fakes_for(modules: Sequence[str], root: Any = None
              ) -> Dict[str, Dict[str, List[str]]]:
    """{module: {function: [result keys]}} for every module in ``modules``
    that declares FAKE_MARKER. The stand-in's functions return a dict of
    those keys, so code that reads result["summary"] keeps going."""
    out: Dict[str, Dict[str, List[str]]] = {}
    for m in modules:
        if not is_faked(m, root):
            continue
        try:
            from council_core import designer_wiring
            info = designer_wiring.module_info(m, root)
            out[m] = {f.name: list(f.result_keys) for f in info.functions}
        except Exception:                                # noqa: BLE001
            out[m] = {}
    return out


#: Committed memory one smoke run may use (MB). Three tiny sample files need
#: a fraction of this; a candidate allocating more gets a MemoryError (a
#: fault the model is told about) instead of draining the machine's commit,
#: which every later process needs to start at all.
MEMORY_LIMIT_MB = 4096


def _spawn(argv: List[str], cwd: str, env: Dict[str, str], stdin: str,
           timeout: float) -> Dict[str, Any]:
    """{returncode, stderr, timed_out, infra}. Through
    council_core.child_proc when it is there — the child and everything it
    starts in a Job Object, killed together on a timeout, and a process the
    machine could not start reported as such (``infra``) — else a plain
    subprocess.run."""
    try:
        from council_core import child_proc
    except Exception:                                    # noqa: BLE001
        child_proc = None
    if child_proc is not None:
        r = child_proc.run(argv, cwd=cwd, env=env, timeout=timeout,
                           input=stdin, memory_limit_mb=MEMORY_LIMIT_MB,
                           retries=1, commit_wait=20.0)
        return {"returncode": r.returncode, "stderr": r.stderr,
                "timed_out": r.timed_out, "infra": r.infra}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, input=stdin,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        return {"returncode": None, "stderr": "", "timed_out": True,
                "infra": ""}
    return {"returncode": proc.returncode, "stderr": proc.stderr or "",
            "timed_out": False, "infra": ""}


def run_job(job: Dict[str, Any], *, python: str = "",
            timeout: float = DEFAULT_TIMEOUT) -> SmokeResult:
    """Run one job in a fresh sandbox and subprocess. NEVER RAISES.

    ``job`` is the child's whole input (see smoke_function / smoke_handler).
    ``python`` is the interpreter — the project's own, so a package it lacks
    is the failure the app would have too. The sandbox is a new temp folder,
    removed afterwards; nothing else is written."""
    res = SmokeResult()
    python = python or sys.executable
    if not python or not Path(python).is_file():
        res.skipped = f"no Python interpreter at {python!r}"
        return res
    t0 = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="cb_smoke_",
                                         ignore_cleanup_errors=True) as tmp:
            sandbox = os.path.realpath(tmp)
            # The child's own limit too (its watchdog): the parent may be
            # gone before its timeout fires — see _child_main.
            job = dict(job, sandbox=sandbox, timeout=float(timeout))
            for name, text in (job.get("files") or {}).items():
                (Path(sandbox) / name).write_text(text, encoding="utf-8")
            job_path = Path(sandbox) / "_smoke_job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            out_path = Path(sandbox) / "_smoke_out.json"
            scratch = Path(sandbox) / "_tmp"
            scratch.mkdir()
            env = dict(os.environ)
            env.update({
                "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                "COUNCIL_NO_DIALOGS": "1", "QT_QPA_PLATFORM": "offscreen",
                "MPLBACKEND": "Agg", "COUNCIL_VAULT_ROOT": sandbox,
                # tempfile writes stay inside the fence.
                "TMP": str(scratch), "TEMP": str(scratch),
                "TMPDIR": str(scratch)})
            env.pop("PYTHONPATH", None)
            # On stdin, not in the job file: the candidate can read the
            # sandbox, and the child consumes stdin before it runs.
            nonce = secrets.token_hex(16)
            argv = [python, str(Path(__file__).resolve()), str(job_path)]
            ran = _spawn(argv, sandbox, env, nonce + "\n", timeout)
            if ran.get("infra"):
                # The MACHINE could not start the run (out of virtual
                # memory: 0xC0000142 and friends, measured in the phi4
                # benchmark). Not the candidate's fault — blaming it would
                # spend a repair round on nothing.
                res.skipped = (f"this PC could not start the smoke run "
                               f"({ran['infra']})")
                res.seconds = time.perf_counter() - t0
                return res
            if ran.get("timed_out"):
                res.ran = True
                res.timed_out = True
                res.seconds = time.perf_counter() - t0
                res.error = f"timed out after {timeout:.0f} s"
                return res
            stderr = ran.get("stderr") or ""
            res.ran = True
            res.seconds = time.perf_counter() - t0
            if not out_path.is_file():
                tail = [ln for ln in stderr.strip().splitlines() if ln.strip()]
                res.error = tail[-1] if tail else (
                    f"the smoke run exited with code {ran.get('returncode')} "
                    f"and no verdict")
                return res
            try:
                got = json.loads(out_path.read_text(encoding="utf-8"))
            except ValueError:
                got = None
            if not isinstance(got, dict) or got.get("nonce") != nonce:
                res.error = ("the smoke run's verdict was not the harness's "
                             "own — the code wrote over it or ended the run "
                             "before the harness could answer")
                return res
    except Exception as exc:                             # noqa: BLE001
        res.error = res.error or f"smoke harness failed: {exc!r}"
        res.seconds = time.perf_counter() - t0
        return res
    for key in ("ok", "error", "where", "line_text", "blocked", "problems",
                "sets", "result_keys", "preview", "soft", "stdout",
                "call_seconds",
                "notes"):
        if key in got:
            setattr(res, key, got[key])
    if res.call_seconds and res.call_seconds > SLOW_CALL:
        res.notes.append(f"slow: {res.call_seconds:.1f} s on three small "
                         f"sample files — the window is frozen while it runs")
    return res


def smoke_function(files: Dict[str, str], function: str, args: Sequence[Any],
                   expect: Dict[str, str], *, module: str = "logic",
                   app_root: Any = None, python: str = "",
                   fakes: Optional[Dict[str, Dict[str, List[str]]]] = None,
                   timeout: float = DEFAULT_TIMEOUT) -> SmokeResult:
    """Call ``module.function(*args)`` once and check what it returns.

    ``files`` are written into the sandbox (``{"logic.py": text}``);
    ``args`` may use SAMPLE_TOKENS; ``expect`` maps each result key the
    links read to a CHECKS value. ``app_root`` puts the Council's folder on
    the child's path — linked mode; None for a standalone project."""
    source = "\n".join(files.values())
    job = {"mode": "function", "files": dict(files), "module": module,
           "function": function, "args": list(args), "expect": dict(expect),
           "app_root": str(app_root) if app_root else "",
           "preload": preload_for(source), "fakes": dict(fakes or {})}
    return run_job(job, python=python, timeout=timeout)


def smoke_handler(source: str, handler: str, ports: Sequence[Dict[str, Any]],
                  *, app_root: Any = None, python: str = "",
                  files: Optional[Dict[str, str]] = None,
                  fakes: Optional[Dict[str, Dict[str, List[str]]]] = None,
                  timeout: float = DEFAULT_TIMEOUT) -> SmokeResult:
    """Press one handler of a whole handlers.py against fake ports.

    ``ports`` rows carry name, kind, type, binder, writer and an optional
    sample (a SAMPLE_TOKENS value or a literal) for get()."""
    all_files = dict(files or {})
    all_files["handlers.py"] = source
    preload = preload_for(source)
    if any(p.get("writer") == "figure_for_drawing"
           and f".{p.get('name')}." in source for p in ports):
        # The fake chart builds a real Figure; matplotlib (and numpy under
        # it) must be loaded before the fence — numpy sets environment
        # variables as it imports.
        preload += [m for m in PRELOAD["matplotlib"] if m not in preload]
    job = {"mode": "handler", "files": all_files, "handler": handler,
           "ports": [dict(p) for p in ports],
           "app_root": str(app_root) if app_root else "",
           "preload": preload, "fakes": dict(fakes or {})}
    return run_job(job, python=python, timeout=timeout)


# ============================================================
# Child side — runs as `python gui_smoke.py <job.json>`
# ============================================================

def _png(width: int, height: int, level: int) -> bytes:
    """A valid greyscale PNG, stdlib only, so sample frames exist even where
    Pillow does not."""
    raw = b"".join(b"\x00" + bytes([level]) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0,
                                         0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _seed_samples(sandbox: str) -> Dict[str, str]:
    """Sample data the candidate is pointed at: three tiny frames, a CSV and
    a text file in sample_folder, and an empty output folder."""
    folder = os.path.join(sandbox, "sample_folder")
    out = os.path.join(sandbox, "output")
    os.makedirs(folder, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    for i, level in enumerate((20, 128, 235)):
        with open(os.path.join(folder, f"frame_{i:03d}.png"), "wb") as fh:
            fh.write(_png(16, 12, level))
    with open(os.path.join(folder, "data.csv"), "w", encoding="utf-8",
              newline="") as fh:
        fh.write("frame,brightness,temperature\n")
        for i in range(5):
            fh.write(f"{i},{20 + 40 * i},{21.5 + i / 2}\n")
    with open(os.path.join(folder, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("sample notes\nsecond line\n")
    return {"<FOLDER>": folder, "<PNG>": os.path.join(folder, "frame_000.png"),
            "<CSV>": os.path.join(folder, "data.csv"),
            "<TXT>": os.path.join(folder, "notes.txt"), "<OUT>": out,
            "<SAVE>": os.path.join(out, "result.csv")}


def _expand(value: Any, tokens: Dict[str, str]) -> Any:
    if isinstance(value, str) and value in tokens:
        return tokens[value]
    if isinstance(value, list):
        return [_expand(v, tokens) for v in value]
    return value


def _shown(value: Any) -> str:
    """A value as two presses are compared on: its repr with object
    addresses dropped, and an image by its size, not its identity."""
    if type(value).__module__.split(".")[0] in ("PIL", "numpy"):
        shape = getattr(value, "shape", None) or getattr(value, "size", "")
        return f"<{type(value).__name__} {shape}>"
    try:
        text = repr(value)
    except Exception:                                    # noqa: BLE001
        text = f"<{type(value).__name__}>"
    return re.sub(r" at 0x[0-9A-Fa-f]+", "", text)[:300]


def _inputs_text(inputs: Dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in inputs.items()) or "the same"


_BROAD = ("Exception", "BaseException")


def broad_handler_at(source: str, lineno: int) -> str:
    """How the except clause is spelled ("except Exception", "except:")
    when line ``lineno`` of ``source`` is a ``raise`` whose innermost
    handler catches everything; "" otherwise.

    raise ValueError("why") is the documented refusal, and the smoke run
    lets it pass on the sample data. Raised from a catch-all, it is not a
    refusal but a relabelled crash: qwen2.5's K7 (2026-10-05) wrote
    `except Exception as e: raise ValueError(f"Failed to save: {e}")`
    around an f-string that itself raised ("Invalid format specifier"),
    the run called that deliberate, the writer accepted it, and the hidden
    test read an empty file. A handler that names what it catches (except
    ValueError: around int(text)) still refuses deliberately — and so does
    a catch-all around the code's OWN `raise ValueError(...)` (the child
    checks that with raises_at)."""
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return ""
    inner = None                  # the innermost handler raising there
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and any(
                isinstance(sub, ast.Raise)
                and sub.lineno <= lineno <= (sub.end_lineno or sub.lineno)
                for stmt in node.body for sub in ast.walk(stmt)):
            if inner is None or node.lineno > inner.lineno:
                inner = node
    if inner is None:
        return ""
    if inner.type is None:
        return "except:"
    caught = inner.type.elts if isinstance(inner.type, ast.Tuple) \
        else [inner.type]
    for n in caught:
        name = n.id if isinstance(n, ast.Name) else \
            n.attr if isinstance(n, ast.Attribute) else ""
        if name in _BROAD:
            return f"except {name}"
    return ""


def raises_at(source: str, lineno: int) -> bool:
    """Whether line ``lineno`` of ``source`` is a ``raise`` statement — the
    code refusing on purpose, not a call on that line failing."""
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(n, ast.Raise)
               and n.lineno <= lineno <= (n.end_lineno or n.lineno)
               for n in ast.walk(tree))


def _check_value(value: Any, check: str, what: str) -> str:
    """Why ``value`` cannot be shown where ``what`` is, or ""."""
    tname = type(value).__name__
    mod = type(value).__module__.split(".")[0]
    if check == "text":
        if isinstance(value, (list, tuple, dict, set, bytes)):
            return (f"{what} shows text, but got a {tname} — make it a "
                    f"string (', '.join(...) for a list)")
    elif check == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            try:
                float(str(value))
            except (TypeError, ValueError):
                return f"{what} shows a number, but got a {tname}"
    elif check == "list":
        if isinstance(value, (str, bytes, dict)) or not isinstance(
                value, (list, tuple)):
            return (f"{what} takes a list of items, but got a {tname}"
                    + (" — a string would be split into letters"
                       if isinstance(value, str) else ""))
    elif check == "rows":
        if not isinstance(value, (list, tuple)):
            return f"{what} takes a list of rows, but got a {tname}"
        for row in list(value)[:50]:
            if not isinstance(row, (list, tuple)):
                return (f"{what} takes rows that are lists or tuples of "
                        f"cells, but a row is a {type(row).__name__}")
    elif check == "image":
        if value is not None and mod not in ("PIL", "numpy"):
            return (f"{what} shows a PIL image or a numpy array, but got a "
                    f"{tname}" + (" — open the file with PIL.Image.open "
                                  "first" if isinstance(value, str) else ""))
    return ""


def _child_main(job_path: str) -> None:            # pragma: no cover - child
    import io
    import traceback

    # The parent's nonce, before anything else runs (see the module
    # docstring). Run by hand, stdin may be absent: no nonce, same verdict.
    try:
        nonce = (sys.stdin.readline() if sys.stdin else "").strip()
    except Exception:                                    # noqa: BLE001
        nonce = ""
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    sandbox = os.path.realpath(job["sandbox"])
    out_path = os.path.join(sandbox, "_smoke_out.json")
    os.chdir(sandbox)
    sys.dont_write_bytecode = True
    here = os.path.normcase(os.path.realpath(os.path.dirname(__file__)))
    # The script's own folder is sys.path[0]. It stays only for a LINKED
    # project, which may import the Council's linked modules — exactly as
    # main.py arranges at runtime — and comes AFTER the sandbox, so the
    # candidate's logic.py is the one imported.
    sys.path[:] = [p for p in sys.path
                   if os.path.normcase(os.path.realpath(p or ".")) != here]
    sys.path.insert(0, sandbox)
    if job.get("app_root"):
        sys.path.insert(1, job["app_root"])
    tokens = _seed_samples(sandbox)
    record: Dict[str, Any] = {"blocked": [], "problems": [], "sets": {},
                              "errors": [], "notes": [],
                              # what the current press wrote, port by port
                              "press": {}}

    # ---- stand-ins for hardware modules -------------------------------
    import types
    for mod_name, functions in (job.get("fakes") or {}).items():
        fake = types.ModuleType(mod_name)
        fake.__file__ = f"<smoke stand-in for {mod_name}>"

        def make(fname, keys, mod_name=mod_name):
            def stand_in(*_a, **_k):
                record["notes"].append(f"{mod_name}.{fname} was a stand-in "
                                       f"(it reaches hardware)")
                return {k: "" for k in keys} if keys else None
            stand_in.__name__ = fname
            return stand_in
        for fname, keys in functions.items():
            setattr(fake, fname, make(fname, list(keys or [])))
        sys.modules[mod_name] = fake

    for lib in job.get("preload") or ():
        try:
            __import__(lib)
        except Exception:                                # noqa: BLE001
            pass

    # ---- the fence ----------------------------------------------------
    # Held from BEFORE the candidate runs: the inside-the-sandbox check
    # calls these, and a candidate that rebinds os.path.realpath (or fspath,
    # or normcase) could otherwise make a write outside look inside. The
    # static gate refuses the rebinding too; this is the floor under it.
    _realpath = os.path.realpath
    _normcase = os.path.normcase
    _fspath = os.fspath
    _fsdecode = os.fsdecode
    _sep = os.sep
    _exit = os._exit
    norm_box = _normcase(sandbox)
    devnull = _normcase(_realpath(os.devnull))
    armed = [False]
    blocked_events = {
        "os.remove", "os.rmdir", "os.rename", "shutil.rmtree", "shutil.move",
        "os.system", "subprocess.Popen", "os.exec", "os.spawn",
        "os.posix_spawn", "os.startfile", "os.kill", "os.chdir",
        "os.symlink", "os.link", "os.truncate",
        # A process by the primitives under subprocess: multiprocessing and
        # concurrent.futures' ProcessPoolExecutor start their workers with
        # _winapi.CreateProcess (Windows) or a fork — and a worker is a
        # second Python with no fence at all.
        "_winapi.CreateProcess", "_winapi.OpenProcess",
        "_winapi.TerminateProcess", "_winapi.CreateJunction",
        "_winapi.CreateFile", "_winapi.CreateNamedPipe",
        "os.fork", "os.forkpty", "_posixsubprocess.fork_exec",
        "socket.connect", "socket.bind", "socket.getaddrinfo",
        "socket.gethostbyname", "socket.sendto", "urllib.Request",
        "http.client.connect", "ftplib.connect", "smtplib.connect",
        "webbrowser.open", "winreg.CreateKey", "winreg.DeleteKey",
        "winreg.DeleteValue", "winreg.SetValue", "winreg.SaveKey",
        "winreg.ConnectRegistry"}
    write_events = {"os.mkdir": 0, "os.chmod": 0, "os.utime": 0,
                    "shutil.copyfile": 1, "shutil.copytree": 1,
                    "shutil.make_archive": 0, "shutil.unpack_archive": 1}
    blocked_imports = {"tkinter", "_tkinter", "PySide6", "PySide2", "PyQt5",
                       "PyQt6", "wx", "kivy", "council_engine"}
    blocked_imports |= set(job.get("block_imports") or ())
    # The interpreter's own internals (frames, gc, settrace, addaudithook)
    # are NOT fenced here: their audit events fire constantly inside normal
    # libraries — numpy, Pillow and socket all read a frame or walk gc as
    # they work — so a runtime block is false positives, not safety. The
    # static gate (gui_codebehind.policy_faults) refuses those spellings in
    # the CANDIDATE's own code, which is what reaches a person, and only a
    # candidate that passed it is ever smoke-run.

    def inside(path: Any) -> bool:
        try:
            p = _fspath(path)
            p = _normcase(_realpath(_fsdecode(p) if isinstance(p, bytes)
                                    else p))
        except Exception:                                # noqa: BLE001
            return False
        return p == norm_box or p.startswith(norm_box + _sep) \
            or p == devnull

    def hook(event: str, args: tuple) -> None:
        if not armed[0]:
            return
        why = ""
        if event in blocked_events:
            shown = ", ".join(repr(a) for a in list(args)[:2]
                              if isinstance(a, (str, bytes, int, tuple)))
            why = f"{event}({shown})"
        elif event == "open":
            path, mode, flags = (list(args) + [None, None, None])[:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) \
                or (mode is None and isinstance(flags, int) and flags & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND
                    | os.O_TRUNC))
            if writing and isinstance(path, (str, bytes, os.PathLike)) \
                    and not inside(path):
                why = (f"open({os.fspath(path)!r}, {mode!r}) writes outside "
                       f"the folders it was given")
        elif event in write_events:
            idx = write_events[event]
            target = args[idx] if len(args) > idx else None
            if isinstance(target, (str, bytes, os.PathLike)) \
                    and not inside(target):
                why = f"{event}({os.fspath(target)!r}) writes outside the " \
                      f"folders it was given"
        elif event == "sqlite3.connect":
            # SQLite opens its file in C: no "open" event. In memory, or a
            # file inside the sandbox, only; a "file:" URI can say anything.
            db = args[0] if args else ""
            text = _fsdecode(db) if isinstance(db, (bytes, os.PathLike)) \
                else str(db)
            if text not in ("", ":memory:") and (
                    text.lower().startswith("file:") or not inside(text)):
                why = (f"sqlite3.connect({text!r}) writes outside the "
                       f"folders it was given")
        elif event == "import" and args:
            root = str(args[0]).split(".")[0]
            if root in blocked_imports:
                why = (f"import {args[0]} — code behind a GUI must not open "
                       f"a toolkit or a model; return values instead"
                       if root != "council_engine" else
                       "import council_engine — a generated app must not "
                       "load the Council's model")
        if why:
            record["blocked"].append(why)
            raise PermissionError("smoke-run blocked: " + why)

    sys.addaudithook(hook)

    # ---- the watchdog ----------------------------------------------------
    # The parent's timeout is not enough on its own: the Designer runs the
    # job on a daemon thread, so closing the Council mid-run left a looping
    # child running forever. A second later than the parent would have.
    # (A C call that holds the GIL can still outlast it.)
    limit = float(job.get("timeout") or 0)
    if limit > 0:
        import threading
        dog = threading.Timer(limit + 1.0, _exit, args=(124,))
        dog.daemon = True
        dog.start()

    # ---- fake ports (handler mode) -------------------------------------
    class FakeChartWidget:
        def figure_for_drawing(self):
            from matplotlib.figure import Figure
            self.fig = Figure()
            return self.fig

        def redraw(self):
            record["sets"].setdefault("__chart__", "redrawn")

    class FakePort:
        """A port with the real binder's behaviour — and its STATE: what a
        press set is what the next press reads (a listbox's items, a
        label's text), as in the window."""

        def __init__(self, spec):
            self.name, self.kind = spec["name"], spec.get("kind", "")
            self.type = spec.get("type", "str")
            self.binder = spec.get("binder", "var")
            self.writer = spec.get("writer", "")
            self.sample = _expand(spec.get("sample"), tokens)
            self.widget = FakeChartWidget() \
                if self.writer == "figure_for_drawing" else None
            self.value = self.sample if self.sample is not None else {
                "path": tokens["<FOLDER>"], "int": 3, "float": 0.5,
                "bool": True, "rows": [("a", "1")],
                "str": ["a"] if self.binder == "list" else "sample"
            }.get(self.type, "sample")
            #: A listbox's items or a table's rows, as the last press left
            #: them.
            self.held = (["alpha", "beta"] if self.binder == "list" else
                         [("a", "1"), ("b", "2")] if self.binder == "table"
                         else None)

        def get(self):
            if self.binder in ("proxy", "event"):
                raise TypeError(f"port {self.name!r} is write-only "
                                f"({self.kind} has no value to read)")
            return self.value

        def items(self):
            if self.binder != "list":
                raise AttributeError(f"port {self.name!r} is a {self.kind}; "
                                     f"only a listbox has items()")
            return list(self.held)

        def rows(self):
            if self.binder != "table":
                raise AttributeError(f"port {self.name!r} is a {self.kind}; "
                                     f"only a table has rows()")
            return list(self.held)

        def set(self, value):
            if self.binder == "event":
                raise TypeError(f"port {self.name!r} is a {self.kind} — it "
                                f"has no value to set; use .enable(True/False)")
            if self.writer == "figure_for_drawing":
                # What the real _ProxyPort does: widget.figure_for_drawing(v)
                raise TypeError("figure_for_drawing() takes 1 positional "
                                "argument but 2 were given")
            check = {"image": "image", "rows": "rows"}.get(self.type) or (
                "list" if self.binder == "list" else
                "number" if self.kind in ("progressbar",) else "any")
            problem = _check_value(value, check, f"port {self.name!r} "
                                                 f"({self.kind})")
            if problem:
                raise TypeError(problem)
            if self.binder in ("var", "text") and self.kind in (
                    "label", "entry", "text", "status_bar") and isinstance(
                    value, (list, dict, tuple, set)):
                record["problems"].append(_check_value(
                    value, "text", f"port {self.name!r} ({self.kind})"))
            record["sets"][self.name] = type(value).__name__
            if self.binder in ("list", "table"):
                self.held = list(value)
            elif self.binder != "proxy":
                self.value = value
            shown = _shown(value)
            if self.writer == "append":
                record["press"].setdefault(self.name, []).append(shown)
            else:
                record["press"][self.name] = shown

        def clear(self):
            pass

        def enable(self, on=True):
            pass

    class FakePorts:
        def __init__(self, specs):
            object.__setattr__(self, "_names", [s["name"] for s in specs])
            for s in specs:
                object.__setattr__(self, s["name"], FakePort(s))

        def __getattr__(self, name):          # only for a missing port
            raise AttributeError(f"this app has no port {name!r} (its ports: "
                                 f"{', '.join(self._names)})")

        def __getitem__(self, name):
            return getattr(self, name)

    def own_frames(exc):
        """(the whole traceback, its frames in the candidate's files)."""
        tb = traceback.extract_tb(getattr(exc, "__traceback__", None))
        return tb, [f for f in tb if os.path.basename(f.filename) in
                    ("handlers.py", "logic.py")]

    def source_of(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except Exception:                                # noqa: BLE001
            return ""

    def relabelled(exc, own):
        """(the crash, how the catch-all is spelled) when the candidate's
        ValueError was raised from a handler that catches everything over
        something that was NOT its own refusal — see broad_handler_at;
        (None, "") for a refusal it meant."""
        crash = getattr(exc, "__context__", None)
        if crash is None or not own:
            return None, ""
        spelled = broad_handler_at(source_of(own[-1].filename),
                                   own[-1].lineno)
        if not spelled:
            return None, ""
        if isinstance(crash, ValueError):
            # `raise ValueError(result["error"])` inside the try, relabelled
            # by the catch-all (qwen2.5-coder's K4 does exactly that): still
            # the refusal the code meant.
            ctb, cown = own_frames(crash)
            if cown and ctb and cown[-1] is ctb[-1] and raises_at(
                    source_of(cown[-1].filename), cown[-1].lineno):
                return None, ""
        return crash, spelled

    def crash_report(crash, spelled):
        """(the error line, the crash's own frames): the error that was
        relabelled, at the line that raised it."""
        return (f"{type(crash).__name__}: {crash} — caught by `{spelled}` "
                f"and raised again as ValueError; a refusal is for input the "
                f"user can fix, not for a crash in the code"), \
            own_frames(crash)[1]

    class Host:
        def report_error(self, what, exc):
            tb, own = own_frames(exc)
            # raise ValueError("why") in the body itself: the message the
            # user is meant to see, not a fault — unless a catch-all raised
            # it over a crash.
            deliberate = bool(isinstance(exc, ValueError) and own and tb
                              and own[-1] is tb[-1])
            error = f"{type(exc).__name__}: {exc}"
            if deliberate:
                crash, spelled = relabelled(exc, own)
                if crash is not None:
                    deliberate = False
                    error, crash_own = crash_report(crash, spelled)
                    own = crash_own or own
            record["errors"].append({
                "error": error,
                "where": (f"{os.path.basename(own[-1].filename)}:"
                          f"{own[-1].lineno}" if own else ""),
                "line_text": own[-1].line if own else "",
                "deliberate": deliberate})

        def clear_ports(self, *names):
            pass

        def request_close(self):
            record["problems"].append("it closes the window")

    # ---- repeated presses (handler mode, a port with a "sample2") -------
    def press(app, inputs):
        """One press with ``inputs`` typed in: (what it wrote, port by
        port; why it failed, or "")."""
        for name, value in inputs.items():
            getattr(app.ports, name).value = value
        record["press"] = {}
        errors, problems = len(record["errors"]), len(record["problems"])
        getattr(app, job["handler"])()
        said = f"pressed with {_inputs_text(inputs)}"
        record["problems"][problems:] = [
            f"{said}: {p}" for p in record["problems"][problems:] if p]
        bad = [e for e in record["errors"][errors:] if not e["deliberate"]]
        if bad:
            return {}, (f"{said}, it raised {bad[0]['error'][:300]}"
                        + (f" (at {bad[0]['where']})" if bad[0]["where"]
                           else ""))
        return dict(record["press"]), ""

    def repeat_problem(app_cls, again):
        """Press with the samples, then with other inputs, then with the
        samples again — and the other way round, on a fresh window. A task
        that starts every press from the ORIGINAL data shows the same for
        the same inputs whatever came before. One press cannot tell: K2's
        "filter the ORIGINAL list" passed it for 4 of 5 models that each
        filtered what the LAST press had left (2026-10-05)."""
        first = {s["name"]: FakePort(s).value for s in again}
        other = {s["name"]: _expand(s["sample2"], tokens) for s in again}
        for one, two in ((first, other), (other, first)):
            app = app_cls.__new__(app_cls)
            app.ports = FakePorts(job.get("ports") or [])
            seen = []
            for inputs in (one, two, one):
                writes, error = press(app, inputs)
                if error:
                    return error
                seen.append(writes)
            if seen[2] != seen[0]:
                port = next(k for k in list(seen[0]) + list(seen[2])
                            if seen[0].get(k) != seen[2].get(k))
                return (f"pressed with {_inputs_text(one)}, then with "
                        f"{_inputs_text(two)}, then with "
                        f"{_inputs_text(one)} again, it set {port} to "
                        f"{seen[2].get(port, '(nothing)')} the third time "
                        f"but to {seen[0].get(port, '(nothing)')} the first "
                        f"— each press must start from the ORIGINAL data, "
                        f"not from what the last press left")
        return ""

    # ---- run -----------------------------------------------------------
    real_stdout, real_stderr = sys.stdout, sys.stderr
    captured = io.StringIO()
    verdict: Dict[str, Any] = {"ok": False}
    t0 = time.perf_counter()
    try:
        sys.stdout = sys.stderr = captured
        armed[0] = True
        if job["mode"] == "function":
            mod = __import__(job.get("module") or "logic")
            fn = getattr(mod, job["function"])
            args = [_expand(a, tokens) for a in job.get("args") or []]
            t0 = time.perf_counter()
            result = fn(*args)
            verdict["call_seconds"] = time.perf_counter() - t0
            # Still armed: repr()/str()/get() below call the candidate's own
            # methods on what it returned.
            expect = job.get("expect") or {}
            if not isinstance(result, dict):
                record["problems"].append(
                    f"it returned a {type(result).__name__}, not a dict with "
                    f"keys {', '.join(expect) or '(any)'}")
            else:
                verdict["result_keys"] = [str(k) for k in result]
                try:
                    verdict["preview"] = repr(result)[:MAX_PREVIEW]
                except Exception:                        # noqa: BLE001
                    verdict["preview"] = "(not printable)"
                if result.get("error"):
                    verdict["soft"] = (f"it reported an error on the sample "
                                       f"data: {result['error']!s:.200}")
                else:
                    missing = [k for k in expect if k not in result]
                    if missing:
                        record["problems"].append(
                            f"the returned dict has no key(s) "
                            f"{', '.join(map(repr, missing))} (it has "
                            f"{', '.join(map(repr, result)) or 'none'})")
                    for key, check in expect.items():
                        if key in result:
                            problem = _check_value(result[key], check,
                                                   f"result[{key!r}]")
                            if problem:
                                record["problems"].append(problem)
        else:
            mod = __import__("handlers")
            mixin = getattr(mod, "HandlerMixin")
            app_cls = type("SmokeApp", (mixin, Host), {})
            app = app_cls.__new__(app_cls)
            app.ports = FakePorts(job.get("ports") or [])
            t0 = time.perf_counter()
            getattr(app, job["handler"])()
            verdict["call_seconds"] = time.perf_counter() - t0
            if record["errors"]:
                first = record["errors"][0]
                verdict.update(where=first["where"],
                               line_text=first["line_text"] or "")
                if first["deliberate"] and not record["blocked"]:
                    verdict["soft"] = (f"it raised {first['error'][:200]} on "
                                       f"the sample data")
                else:
                    verdict["error"] = first["error"][:600]
            again = [s for s in job.get("ports") or [] if "sample2" in s]
            if again and not verdict.get("error") and not record["blocked"]:
                why = repeat_problem(app_cls, again)
                if why:
                    record["problems"].append(why)
        verdict["ok"] = not (record["problems"] or record["blocked"]
                             or verdict.get("error"))
    except BaseException as exc:                         # noqa: BLE001
        verdict["call_seconds"] = time.perf_counter() - t0
        tb, own = own_frames(exc)
        name = type(exc).__name__
        if isinstance(exc, SystemExit):
            msg = "SystemExit — it calls sys.exit()/exit(), which closes the app"
        else:
            msg = f"{name}: {exc}"
        deliberate = (isinstance(exc, ValueError) and own
                      and own[-1] is tb[-1] and not record["blocked"])
        if deliberate:
            crash, spelled = relabelled(exc, own)
            if crash is not None:
                deliberate = False
                msg, crash_own = crash_report(crash, spelled)
                own = crash_own or own
        if deliberate:
            # The documented failure path: raise ValueError("why"). On three
            # sample frames that may be the honest answer — said, not failed.
            verdict["ok"] = not record["problems"]
            verdict["soft"] = (f"it raised ValueError on the sample data: "
                               f"{exc!s:.200}")
        else:
            verdict["error"] = msg[:600]
        if own:
            verdict["where"] = (f"{os.path.basename(own[-1].filename)}:"
                                f"{own[-1].lineno}")
            verdict["line_text"] = own[-1].line or ""
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
    verdict["blocked"] = record["blocked"]
    verdict["problems"] = [p for p in record["problems"] if p]
    verdict["sets"] = record["sets"]
    verdict["notes"] = sorted(set(record["notes"]))
    verdict["stdout"] = captured.getvalue()[:MAX_STDOUT]
    if verdict["blocked"]:
        verdict["ok"] = False
    verdict["nonce"] = nonce
    # Still armed (the sandbox is where this file lives, so the fence lets
    # it through). Then out, at once: no exit handler or finalizer the
    # candidate left behind gets a turn — and none would be fenced-free.
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(verdict, fh, default=str)
    try:
        real_stdout.flush()
        real_stderr.flush()
    except Exception:                                    # noqa: BLE001
        pass
    _exit(0)


if __name__ == "__main__":                          # pragma: no cover - child
    _child_main(sys.argv[1])
