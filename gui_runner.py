"""
gui_runner.py — preview process management for the GUI Designer.

WHY NOT council_engine.LocalRunner
----------------------------------
LocalRunner.run_code uses subprocess.run(capture_output=True, timeout=...),
which BLOCKS until the child exits. A Tk mainloop() never exits, so a preview
launched that way would sit invisible until the timeout fired and then be killed
with no output at all — the worst possible failure: no window, no error, no
clue. This module uses Popen with no timeout and drains the pipes on daemon
threads, which is the shape LocalRunner.run_code_streaming already uses
correctly and the reason that one is safe to imitate.

WHY EVERY PREVIEW IS TRACKED
----------------------------
A preview is a real OS process holding a real window. If the tab closes, the
project switches, or the app exits without killing it, the user is left with an
orphaned window that no longer corresponds to anything on screen and cannot be
stopped from the app that started it. So every launch is registered, one preview
per project is enforced, and an atexit hook is the backstop for the path nobody
remembered.
"""
from __future__ import annotations

import atexit
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

# Traceback frames pointing at generated code, so the log can say WHICH line of
# ui/ failed rather than making the user read a stack.
_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+)')

# Every live preview, keyed by resolved project directory. Module-level because
# the atexit hook has to reach them without a reference to the tab.
_LIVE: Dict[str, "Preview"] = {}
_LOCK = threading.RLock()


@dataclass
class Preview:
    """One running preview process."""
    project: Path
    proc: subprocess.Popen
    on_line: Optional[Callable[[str, str], None]] = None   # (text, level)
    on_exit: Optional[Callable[[int], None]] = None
    _threads: List[threading.Thread] = field(default_factory=list)
    _tail: List[str] = field(default_factory=list)

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def stop(self, timeout: float = 3.0) -> None:
        """Terminate, then kill if it will not go.

        terminate() first so the child can close its window cleanly; kill()
        after a grace period because a Tk app stuck in a modal dialog will
        ignore the polite request and must not survive its parent."""
        if self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=timeout)
        except Exception:
            pass


def _emit(pv: Preview, text: str, level: str) -> None:
    if pv.on_line:
        try:
            pv.on_line(text, level)
        except Exception:
            pass


def _drain(pv: Preview, stream, level: str) -> None:
    """Read a pipe to EOF, line by line, into the callback.

    Line-buffered and unbuffered on the child side (-u), so output appears while
    the preview runs rather than arriving in a lump when it dies."""
    try:
        for line in stream:
            text = line.rstrip("\n")
            if level == "error":
                # Keep a bounded tail so a crash can be explained afterwards
                # without holding the whole session's stderr in memory.
                pv._tail.append(text)
                del pv._tail[:-200]
            _emit(pv, text, level)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _watch(pv: Preview) -> None:
    """Wait for exit, explain a failure, then deregister."""
    code = pv.proc.wait()
    for t in pv._threads:
        t.join(timeout=2.0)
    if code != 0:
        _emit(pv, f"preview exited with code {code}", "error")
        blame = explain_failure("\n".join(pv._tail), pv.project)
        if blame:
            _emit(pv, blame, "error")
    else:
        _emit(pv, "preview closed", "info")
    with _LOCK:
        if _LIVE.get(str(pv.project)) is pv:
            _LIVE.pop(str(pv.project), None)
    if pv.on_exit:
        try:
            pv.on_exit(code)
        except Exception:
            pass


def explain_failure(stderr_text: str, project: Path) -> str:
    """Point at the last generated-code frame in a traceback.

    A Tk traceback is mostly Tk's own frames; the line that matters is the last
    one inside this project's ui/. Surfacing it turns 'it crashed' into 'line 42
    of main_ui.py', which is the difference between a usable error and a wall of
    text the user scrolls past."""
    if not stderr_text:
        return ""
    proj = str(Path(project).resolve()).lower()
    last = None
    for m in _FRAME_RE.finditer(stderr_text):
        fpath, line = m.group(1), m.group(2)
        low = fpath.replace("\\", "/").lower()
        if proj.replace("\\", "/") in low or "/ui/" in low:
            last = (fpath, line)
    if not last:
        return ""
    name = Path(last[0]).name
    tail = stderr_text.strip().splitlines()[-1] if stderr_text.strip() else ""
    return f"  -> {name}, line {last[1]}" + (f": {tail}" if tail else "")


def is_running(project) -> bool:
    with _LOCK:
        pv = _LIVE.get(str(Path(project).resolve()))
    return bool(pv and pv.running)


def get(project) -> Optional[Preview]:
    with _LOCK:
        return _LIVE.get(str(Path(project).resolve()))


def start(project, *, on_line: Optional[Callable[[str, str], None]] = None,
          on_exit: Optional[Callable[[int], None]] = None,
          entry: str = "launch.py") -> Preview:
    """Launch the project's preview. A second Run stops the first (spec 9).

    NO TIMEOUT. A GUI runs until the user closes it; a timeout here would kill
    a working preview mid-use, which is precisely the LocalRunner failure this
    module exists to avoid."""
    proj = Path(project).resolve()
    launch = proj / entry
    if not launch.is_file():
        raise FileNotFoundError(f"no {entry} in {proj} — generate the project first")

    stop(proj)      # one preview per project

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-u", str(launch)],
        cwd=str(proj),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    pv = Preview(project=proj, proc=proc, on_line=on_line, on_exit=on_exit)
    for stream, level in ((proc.stdout, "info"), (proc.stderr, "error")):
        t = threading.Thread(target=_drain, args=(pv, stream, level),
                             daemon=True)
        t.start()
        pv._threads.append(t)
    threading.Thread(target=_watch, args=(pv,), daemon=True).start()

    with _LOCK:
        _LIVE[str(proj)] = pv
    _emit(pv, f"preview started (pid {pv.pid})", "info")
    return pv


def stop(project) -> bool:
    """Stop one project's preview. True if something was running."""
    key = str(Path(project).resolve())
    with _LOCK:
        pv = _LIVE.pop(key, None)
    if pv is None:
        return False
    was = pv.running
    pv.stop()
    return was


def stop_all() -> int:
    """Stop every preview. The tab-close / app-exit path."""
    with _LOCK:
        pvs = list(_LIVE.values())
        _LIVE.clear()
    for pv in pvs:
        pv.stop()
    return len(pvs)


# The backstop. Tab close and project switch call stop() explicitly; this
# catches the paths nobody remembered, including an unhandled exception on the
# way out. Without it a crash in the designer leaves an orphan window the user
# cannot connect to anything.
atexit.register(stop_all)
