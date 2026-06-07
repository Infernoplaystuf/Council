"""
sim_runner.py — pluggable backends that produce ``SimRun`` records.

Two backends ship today:

  * GodotSimRunner — spawns ``godot --headless --path <project>`` for
    a configurable wall-clock window, parses lines emitted by the game
    that follow the Anvil telemetry convention, and records the
    aggregated metrics + event list.

  * PythonSimRunner — imports a user-supplied module exposing
    ``simulate(params) -> dict`` and runs it in-process. Designed for
    fast pure-math sweeps (economy balance, combat curves,
    procgen-seed exploration) where standing up a real Godot loop is
    overkill.

Telemetry convention (Godot backend)
------------------------------------
Game code writes structured lines to stdout. Anvil parses lines that
start with one of two prefixes::

    ANVIL_METRIC: <name> = <value>
    ANVIL_EVENT:  <name>  [k=v ...]

Metric values are floats; non-numeric values are kept as strings.
Events carry an arbitrary set of key=value pairs in their data dict.
A timestamp ``t`` (seconds since run start) is auto-attached.

Any other stdout / stderr line is captured in the tail buffer
(last ~4 KB each direction) for diagnosis but does not affect
metrics. Crash detection: a non-zero exit code or stderr containing
``SCRIPT ERROR`` / ``Parse Error:`` flips ``SimRun.error``.

Python-backend contract
-----------------------
The user's ``simulate(params)`` returns a dict shaped like::

    {
        "metrics": {"final_gold": 42, "days": 30},   # required
        "events":  [{"t": 0.0, "name": "spawned", "data": {...}}],
        "stdout":  "optional log tail",
    }

Any field is optional except ``metrics`` (empty is allowed; absent
is treated as a contract violation and surfaces as ``error``).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from sim_recorder import SimEvent, SimRun


# ============================================================
# Constants
# ============================================================

TAIL_BUFFER_BYTES = 4096
DEFAULT_TIMEOUT_S = 30.0

# Stderr lines containing any of these substrings raise the run's
# error flag even when the process exited cleanly. Godot will often
# print SCRIPT ERROR + return 0 for a script that loaded but
# misbehaved at runtime.
_GODOT_CRASH_HINTS = (
    "SCRIPT ERROR", "Parse Error:", "Cannot find type",
    "Invalid get index", "Stack frames:",
)


# ============================================================
# Telemetry line parser
# ============================================================

# Metric: "ANVIL_METRIC: jump_height = 32.5"
_METRIC_RE = re.compile(
    r"^\s*ANVIL_METRIC\s*:\s*"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\s*"
    r"=\s*(?P<value>.+?)\s*$",
)

# Event:  "ANVIL_EVENT: player_died x=100.5 y=42 cause=spike"
_EVENT_RE = re.compile(
    r"^\s*ANVIL_EVENT\s*:\s*"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\s*"
    r"(?P<rest>.*)$",
)

# Single kv-pair inside an event line; values may be unquoted, quoted,
# or numeric.
_KV_RE = re.compile(
    r"(?P<k>[A-Za-z_][A-Za-z0-9_.]*)\s*=\s*"
    r"(?:\"(?P<sq>[^\"]*)\"|(?P<v>[^\s]+))",
)


def parse_telemetry_line(line: str) -> Optional[Dict[str, Any]]:
    """Recognise one ``ANVIL_METRIC`` or ``ANVIL_EVENT`` line.

    Returns ``{'kind': 'metric'|'event', ...}`` or None for non-
    telemetry lines. Pure / deterministic — safe to unit-test.
    """
    if not line or "ANVIL_" not in line:
        return None
    m = _METRIC_RE.match(line)
    if m:
        raw = m.group("value").strip()
        try:
            value: Any = float(raw)
        except ValueError:
            value = raw
        return {"kind": "metric", "name": m.group("name"), "value": value}
    m = _EVENT_RE.match(line)
    if m:
        data: Dict[str, Any] = {}
        for kv in _KV_RE.finditer(m.group("rest")):
            key = kv.group("k")
            sq = kv.group("sq")
            v = kv.group("v")
            raw = sq if sq is not None else (v or "")
            # Try numeric first, fall back to string
            try:
                data[key] = float(raw)
            except ValueError:
                data[key] = raw
        return {"kind": "event", "name": m.group("name"), "data": data}
    return None


# ============================================================
# Tail buffer — bounded stdout/stderr capture
# ============================================================

class _TailBuffer:
    """Captures the most recent ``max_bytes`` of a text stream.

    Lines arrive one at a time via append(); we keep a deque sized
    by total byte count so the buffer can't grow without bound when
    a chatty game prints millions of lines.
    """

    def __init__(self, max_bytes: int = TAIL_BUFFER_BYTES):
        self.max_bytes = max_bytes
        self._lines: deque[str] = deque()
        self._bytes = 0
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            self._bytes += len(line) + 1
            while self._bytes > self.max_bytes and self._lines:
                dropped = self._lines.popleft()
                self._bytes -= len(dropped) + 1

    def text(self) -> str:
        with self._lock:
            return "\n".join(self._lines)


# ============================================================
# Abstract runner
# ============================================================

class SimRunner(ABC):
    """One concrete sim invocation. Configured per-run with the
    params + duration the caller chose."""

    backend: str = "abstract"

    @abstractmethod
    def run(self, *, params: Dict[str, Any],
            sim_name: str = "") -> SimRun:
        ...


# ============================================================
# Godot backend
# ============================================================

class GodotSimRunner(SimRunner):
    """Spawn ``godot --headless --path <project_root>`` and parse
    ANVIL_METRIC / ANVIL_EVENT lines from its stdout.

    The runner enforces a wall-clock timeout (``duration_s``) and
    terminates the subprocess politely on expiry. A clean exit before
    the timeout is fine — many sims emit a final metric then quit.

    Parameter overrides are written to ``<project>/anvil_params.json``
    before launch; the game side (anvil_telemetry.gd autoload, see
    commit 2) reads the file in _ready() and applies overrides. This
    runner only handles the file write — installing the autoload is
    a one-time setup step the user does in the project.
    """

    backend = "godot"

    def __init__(
        self,
        project_root: Any,
        *,
        godot_binary: str = "godot",
        duration_s: float = DEFAULT_TIMEOUT_S,
        scene: Optional[str] = None,
        on_line: Optional[Callable[[str, str], None]] = None,
    ):
        self.project_root = Path(project_root).expanduser().resolve()
        self.godot_binary = godot_binary
        self.duration_s = max(0.5, float(duration_s))
        self.scene = scene
        self.on_line = on_line or (lambda stream, text: None)

    # ----------------------------------------------------------------

    def _write_params(self, params: Dict[str, Any]) -> None:
        """Drop the param overrides into the project as
        ``anvil_params.json``. The game-side autoload reads this on
        _ready. If the project doesn't have the autoload installed,
        the file is harmless — Godot just ignores it.
        """
        if not params:
            return
        import json as _json
        target = self.project_root / "anvil_params.json"
        try:
            target.write_text(
                _json.dumps(params, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"[GodotSimRunner] could not write params: {exc!r}")

    def _build_args(self) -> List[str]:
        args = [self.godot_binary, "--headless",
                "--path", str(self.project_root)]
        if self.scene:
            args.append(self.scene)
        return args

    def run(self, *, params: Dict[str, Any],
            sim_name: str = "") -> SimRun:
        run = SimRun(sim_name=sim_name or self.project_root.name,
                     backend=self.backend, params=dict(params))
        self._write_params(params)

        # Windows: suppress the extra console window
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            except Exception:
                startupinfo = None
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        out_tail = _TailBuffer()
        err_tail = _TailBuffer()
        metrics: Dict[str, float] = {}
        events: List[SimEvent] = []
        t0 = time.monotonic()

        try:
            proc = subprocess.Popen(
                self._build_args(),
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1, text=True,
                encoding="utf-8", errors="replace",
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except FileNotFoundError:
            run.error = (
                f"Godot binary not found: {self.godot_binary!r}. "
                "Configure it in the Godot Workspace settings."
            )
            return run
        except Exception as exc:
            run.error = f"could not launch godot: {exc!r}"
            return run

        # ── Drain stdout / stderr in daemon threads ─────────────
        def _drain(stream, label, tail):
            try:
                for raw in iter(stream.readline, ""):
                    if not raw:
                        break
                    text = raw.rstrip("\r\n")
                    tail.append(text)
                    try:
                        self.on_line(label, text)
                    except Exception:
                        pass
                    if label == "stdout":
                        parsed = parse_telemetry_line(text)
                        if parsed is None:
                            continue
                        if parsed["kind"] == "metric":
                            try:
                                if isinstance(parsed["value"], (int, float)):
                                    metrics[parsed["name"]] = float(parsed["value"])
                                else:
                                    # non-numeric metrics are surfaced too
                                    # but stored as a string under the same key
                                    metrics[parsed["name"]] = parsed["value"]
                            except Exception:
                                pass
                        elif parsed["kind"] == "event":
                            events.append(SimEvent(
                                t=round(time.monotonic() - t0, 3),
                                name=parsed["name"],
                                data=parsed["data"],
                            ))
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = threading.Thread(
            target=_drain, args=(proc.stdout, "stdout", out_tail),
            daemon=True, name="anvil-sim-stdout")
        t_err = threading.Thread(
            target=_drain, args=(proc.stderr, "stderr", err_tail),
            daemon=True, name="anvil-sim-stderr")
        t_out.start(); t_err.start()

        # ── Wait up to duration_s for natural exit ──────────────
        try:
            rc = proc.wait(timeout=self.duration_s)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
            except Exception:
                pass
            rc = proc.returncode if proc.returncode is not None else -1
            run.error = (
                f"timeout after {self.duration_s:.1f}s — "
                "process was terminated"
            )
        except Exception as exc:
            run.error = f"wait failed: {exc!r}"
            rc = -1

        # Wait briefly for drainers to flush
        for t in (t_out, t_err):
            try:
                t.join(timeout=2.0)
            except Exception:
                pass

        run.exit_code = rc
        run.duration_s = round(time.monotonic() - t0, 3)
        run.metrics = metrics
        run.events = events
        run.stdout_tail = out_tail.text()
        run.stderr_tail = err_tail.text()

        # Crash detection on stderr — Godot can return 0 for runtime
        # SCRIPT ERRORs that you'd absolutely want flagged.
        if not run.error:
            if rc != 0:
                run.error = f"exit code {rc}"
            else:
                stderr_blob = run.stderr_tail
                for hint in _GODOT_CRASH_HINTS:
                    if hint in stderr_blob:
                        run.error = f"crash hint in stderr: {hint!r}"
                        break

        return run


# ============================================================
# Python backend
# ============================================================

class PythonSimRunner(SimRunner):
    """Run a user-supplied ``simulate(params)`` in-process.

    The simulator module lives somewhere the caller specifies — for
    Anvil, that's ``vault/simulations/<name>.py`` — and must expose a
    top-level ``simulate(params: dict) -> dict`` callable. The return
    contract is described in the module docstring above.

    This runner is *fast*: no subprocess overhead, no JSON marshalling,
    no Godot startup time. It's intended for pure-math sims where the
    user just wants to sweep numbers — economy curves, balance
    spreadsheets, probabilistic combat models. Real Godot game loops
    should still go through GodotSimRunner.
    """

    backend = "python"

    def __init__(
        self,
        module_or_path: Any,
        *,
        function_name: str = "simulate",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self.target = module_or_path
        self.function_name = function_name
        self.timeout_s = max(0.5, float(timeout_s))

    # ----------------------------------------------------------------

    def _resolve_callable(self) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        """Resolve the target to a callable. Accepts a callable
        directly, a module object, or a filesystem path to a .py file."""
        target = self.target
        if callable(target) and not isinstance(target, (Path, str)):
            return target
        if hasattr(target, self.function_name) and not isinstance(
            target, (str, Path)
        ):
            return getattr(target, self.function_name)
        path = Path(target)
        if not path.exists() or path.suffix.lower() != ".py":
            raise FileNotFoundError(
                f"PythonSimRunner: not a .py file: {path}"
            )
        spec = importlib.util.spec_from_file_location(
            f"_anvil_sim_{path.stem}", path,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"could not build spec for {path}")
        mod = importlib.util.module_from_spec(spec)
        # Run the module — careful: user code can do anything in
        # this process. The user wrote it and pointed Anvil at it
        # explicitly, so the trust model is "this is your code".
        spec.loader.exec_module(mod)
        fn = getattr(mod, self.function_name, None)
        if not callable(fn):
            raise AttributeError(
                f"{path}: no callable named {self.function_name!r}"
            )
        return fn

    def run(self, *, params: Dict[str, Any],
            sim_name: str = "") -> SimRun:
        run = SimRun(sim_name=sim_name or "python_sim",
                     backend=self.backend, params=dict(params))
        try:
            fn = self._resolve_callable()
        except Exception as exc:
            run.error = f"could not load simulate(): {exc!r}"
            return run
        t0 = time.monotonic()
        # Timeout via a daemon thread is the only stdlib option that
        # doesn't require signals (signals are main-thread only on
        # Unix and not portable to Windows). On expiry we mark the
        # run as timed out and bail — Python doesn't give us a clean
        # way to actually kill another thread's work.
        result_holder: Dict[str, Any] = {}
        err_holder: Dict[str, str] = {}

        def _worker():
            try:
                result_holder["value"] = fn(dict(params))
            except Exception as exc:
                err_holder["err"] = (
                    f"{exc!r}\n" + traceback.format_exc(limit=8)
                )

        th = threading.Thread(
            target=_worker, daemon=True, name="anvil-python-sim",
        )
        th.start()
        th.join(timeout=self.timeout_s)
        run.duration_s = round(time.monotonic() - t0, 3)

        if th.is_alive():
            run.error = (
                f"timeout after {self.timeout_s:.1f}s — Python sim "
                "thread still running"
            )
            return run
        if "err" in err_holder:
            run.error = f"simulate() raised: {err_holder['err']}"
            return run
        result = result_holder.get("value")
        if not isinstance(result, dict) or "metrics" not in result:
            run.error = (
                "simulate() must return a dict with a 'metrics' key — "
                f"got {type(result).__name__}"
            )
            return run

        # Normalise output
        try:
            metrics_raw = result.get("metrics") or {}
            run.metrics = {
                str(k): float(v) if isinstance(v, (int, float)) else v
                for k, v in metrics_raw.items()
            }
        except Exception as exc:
            run.error = f"could not normalise metrics: {exc!r}"
            return run
        try:
            events_raw = result.get("events") or []
            evs: List[SimEvent] = []
            for e in events_raw:
                if isinstance(e, dict):
                    evs.append(SimEvent(
                        t=float(e.get("t", 0.0)),
                        name=str(e.get("name", "")),
                        data=dict(e.get("data") or {}),
                    ))
                elif isinstance(e, SimEvent):
                    evs.append(e)
            run.events = evs
        except Exception:
            pass

        if isinstance(result.get("stdout"), str):
            run.stdout_tail = result["stdout"][-TAIL_BUFFER_BYTES:]
        run.exit_code = 0
        return run
