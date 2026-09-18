"""
council_core.ide_jobs — running the buffer, and saying what happened.

Two ways to run: blocking, which hands back everything at the end, and
streaming, which calls back per line. Both go through `council_engine
.LocalRunner`, which writes the buffer into the workspace and starts a real
Python subprocess.

THREE THINGS THE TK TAB GETS WRONG, AND THEY ARE ALL ABOUT THE TIMEOUT

*A blocking run that times out throws away everything the script printed.*
`run_code` is `subprocess.run(..., timeout=120)`, which raises TimeoutExpired —
and TimeoutExpired CARRIES the partial stdout and stderr. The Tk handler
formats the exception and drops them, so a script that printed three hundred
useful lines and then hung shows the user a one-line traceback.

*A streaming run that times out is reported as an ordinary exit.*
`run_code_streaming` catches TimeoutExpired itself, kills the child and returns
its return code like any other. The tab prints "Exited rc=1" — so
`time.sleep(300)` looks like a script that failed, not one that was stopped at
two minutes.

*Two runs share one file.* Neither path has a busy guard, and both derive the
same path from the same script name. Start a long streaming run, edit the
buffer, press Run: the second invocation overwrites the exact .py the first
subprocess is still executing, and the traceback you get back points at code
that is no longer what ran.

A NAME IS TRUNCATED BEFORE ITS EXTENSION IS STRIPPED
`_safe_script_basename` cuts to 60 characters and THEN strips a trailing ".py",
so a 61-character name ending in ".py" is cut mid-extension and the strip never
fires — leaving a file called `something_very_long....p`. Stripped first here.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

#: How long a run gets before it is stopped. The Tk number.
TIMEOUT_S = 120

#: The longest a derived file name may be, before the extension.
MAX_NAME = 60

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def script_basename(name: str) -> str:
    """A user's script name as a file name, without its extension.

    THE EXTENSION IS STRIPPED FIRST. The Tk helper truncates to 60 and then
    strips ".py", so a 61-character name ending in ".py" is cut mid-extension
    — the strip never matches and the file is called `...p`.
    """
    name = _UNSAFE.sub("_", str(name or "").strip()).strip("._-")
    if name.lower().endswith(".py"):
        name = name[:-3]
    name = name[:MAX_NAME].strip("._-")
    return name or "script"


@dataclass
class RunResult:
    """What a run did. `timed_out` is what neither Tk path reports."""
    rc: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    path: Optional[Path] = None
    #: True when the run was STOPPED at the timeout rather than finishing.
    timed_out: bool = False
    #: Set when the run could not start at all.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.timed_out and self.rc == 0

    def summary(self, name: str) -> str:
        """The line the output pane ends with."""
        if self.error:
            return f"[{name}] could not run: {self.error}"
        if self.timed_out:
            return (f"[{name}] STOPPED after {TIMEOUT_S}s — it was still "
                    f"running. Anything printed before that is above.")
        return f"[{name}] Exited rc={self.rc}"


def run_blocking(runner: Any, code: str, *, name: str = "script",
                 timeout_s: int = TIMEOUT_S) -> RunResult:
    """Run the buffer and hand back everything at once. NEVER raises.

    A timeout keeps the partial output: `TimeoutExpired` carries `.stdout` and
    `.stderr`, and throwing them away is the difference between "your script
    hung" and "your script hung, here is what it had printed".
    """
    filename = f"{script_basename(name)}.py"
    try:
        rc, out, err, path = runner.run_code(code, filename_hint=filename,
                                             timeout_s=timeout_s)
    except subprocess.TimeoutExpired as expired:
        return RunResult(stdout=_text(expired.stdout),
                         stderr=_text(expired.stderr),
                         timed_out=True)
    except Exception as exc:                              # noqa: BLE001
        return RunResult(error=repr(exc))
    return RunResult(rc=rc, stdout=out or "", stderr=err or "", path=path)


def run_streaming(runner: Any, code: str, *, name: str = "script",
                  timeout_s: int = TIMEOUT_S,
                  on_stdout: Optional[Callable[[str], None]] = None,
                  on_stderr: Optional[Callable[[str], None]] = None
                  ) -> RunResult:
    """Run the buffer, calling back per line. NEVER raises.

    THE CALLBACKS FIRE ON THE RUNNER'S OWN DRAIN THREADS, not on whatever
    thread called this — `run_code_streaming` starts two daemon readers inside
    council_engine. A Qt caller must therefore marshal from inside the
    callback, not merely from around this call.

    A timeout is reported. `run_code_streaming` swallows TimeoutExpired itself,
    kills the child and returns its return code, so the caller cannot tell a
    stopped run from a failed one without being told.
    """
    filename = f"{script_basename(name)}.py"
    watcher = _TimeoutWatcher(timeout_s)
    try:
        rc, path = runner.run_code_streaming(
            code, filename_hint=filename, timeout_s=timeout_s,
            stdout_callback=on_stdout, stderr_callback=on_stderr)
    except subprocess.TimeoutExpired:
        return RunResult(timed_out=True)
    except Exception as exc:                              # noqa: BLE001
        return RunResult(error=repr(exc))
    return RunResult(rc=rc, path=path, timed_out=watcher.expired())


class _TimeoutWatcher:
    """Whether a streaming run lasted long enough to have been killed.

    `run_code_streaming` handles its own timeout and returns a normal result,
    so the only signal left is the clock. Measured rather than guessed from the
    return code, because a script that genuinely exits -9 is a different thing
    from one that was stopped.
    """

    def __init__(self, timeout_s: int):
        import time
        self._timeout = timeout_s
        self._started = time.monotonic()

    def expired(self) -> bool:
        import time
        # A small margin: the runner's own kill-and-wait takes a moment, so a
        # run stopped at exactly the limit comes back slightly past it.
        return (time.monotonic() - self._started) >= self._timeout


def snapshot(librarian: Any, code: str, *,
             label: str = "council_code") -> RunResult:
    """Save the buffer into the vault. Blocking — call it from a worker.

    A disk write, which the Tk tab does on the GUI thread.
    """
    try:
        path = librarian.snapshot_code(code, label=label)
    except Exception as exc:                              # noqa: BLE001
        return RunResult(error=repr(exc))
    return RunResult(rc=0, path=Path(path))


def _text(raw: Any) -> str:
    """Bytes or str from a TimeoutExpired, as text."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)
