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

WHY STOP ASKS BEFORE IT KILLS
-----------------------------
A preview may be driving hardware. Stop sends "stop" on the child's stdin and
waits for it to close itself — running its on_close, where a camera app stops
its grab and releases the device — and only kills it if it will not. If the
designer itself dies, the child's stdin reaches end-of-file and it closes the
same way. See Preview.stop for what the old terminate() actually did.
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


# How long a preview gets to close itself after Stop before it is killed. A
# camera app has to stop its grab, close the device and finalise a file; five
# seconds is generous for that and short enough that a hung app does not make
# Stop feel broken.
STOP_GRACE = 5.0


@dataclass
class Preview:
    """One running preview process."""
    project: Path
    proc: subprocess.Popen
    on_line: Optional[Callable[[str, str], None]] = None   # (text, level)
    on_exit: Optional[Callable[[int], None]] = None
    # Whether this app's generated code listens for a stop request. Projects
    # generated before the stop listener existed do not, and waiting out the
    # grace period on them would only make Stop slow.
    listens: bool = False
    forced: bool = False       # killed because it would not close itself
    stopping: bool = False     # Stop was pressed — its exit is not a crash
    grace: float = STOP_GRACE  # what the last stop() actually waited
    _threads: List[threading.Thread] = field(default_factory=list)
    _tail: List[str] = field(default_factory=list)

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def ask_to_stop(self) -> None:
        """Send the polite request and return without waiting."""
        self.stopping = True
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.write("stop\n")
                self.proc.stdin.flush()
                self.proc.stdin.close()      # end-of-file backs it up
        except Exception:
            pass                            # already gone, or not listening

    def stop(self, grace: float = STOP_GRACE) -> bool:
        """Ask the app to close; kill it only if it will not. True = it closed
        by itself.

        On Windows, Popen.terminate() IS TerminateProcess — an immediate hard
        kill in which no finally, atexit or window-close handler runs. Measured:
        stop() returned in 0.01 s, exit code 1, and none of three cleanup
        markers was written. A camera app stopped that way never called
        StopGrabbing or Close and never finalised its recording. So the request
        goes over stdin, which the generated app listens to (gui_emit
        STOP_WATCHER), and kill() is only the fallback for a hung app."""
        if self.proc.poll() is not None:
            return True
        self.grace = grace
        self.ask_to_stop()
        if self.listens:
            try:
                self.proc.wait(timeout=grace)
                return True
            except subprocess.TimeoutExpired:
                pass
        return self._kill()

    def _kill(self) -> bool:
        if self.proc.poll() is not None:
            return True
        self.forced = True
        try:
            self.proc.kill()
            self.proc.wait(timeout=5.0)
        except Exception:
            pass
        return False


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
    if pv.forced:
        # Not a crash: Stop was pressed and the app did not close itself.
        why = ("it was generated before clean Stop existed — Generate it "
               "again" if not pv.listens else
               f"it did not close within {pv.grace:g} s (hung?)")
        _emit(pv, f"preview killed: {why}", "error")
    elif code != 0:
        _emit(pv, f"preview exited with code {code}", "error")
        blame = explain_failure("\n".join(pv._tail), pv.project)
        if blame:
            _emit(pv, blame, "error")
    else:
        _emit(pv, "preview closed cleanly" if pv.stopping
              else "preview closed", "info")
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
          entry: str = "") -> Preview:
    """Launch the project's preview. A second Run stops the first (spec 9).

    NO TIMEOUT. A GUI runs until the user closes it; a timeout here would kill
    a working preview mid-use, which is precisely the LocalRunner failure this
    module exists to avoid.

    ``entry`` defaults to main.py, falling back to launch.py for a project
    generated before the rename that has not been regenerated since."""
    proj = Path(project).resolve()
    if entry:
        launch = proj / entry
    else:
        launch = next((proj / n for n in ("main.py", "launch.py")
                       if (proj / n).is_file()), proj / "main.py")
        entry = launch.name
    if not launch.is_file():
        raise FileNotFoundError(
            f"no main.py in {proj} — generate the project first")

    stop(proj)      # one preview per project

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # The pipes below are decoded as UTF-8, but a child Python on Windows
    # writes its locale code page (cp1252) unless told otherwise — measured on
    # a 3.9 child — so non-ASCII output arrived garbled or raised in the child.
    env["PYTHONIOENCODING"] = "utf-8"
    # A native SDK crash (an access violation inside a camera driver) is
    # otherwise a bare exit code with nothing to explain it.
    env["PYTHONFAULTHANDLER"] = "1"
    # Tells the generated app to listen on stdin for a clean-close request.
    env["COUNCIL_PREVIEW_CONTROL"] = "stdin"
    proc = subprocess.Popen(
        [sys.executable, "-u", str(launch)],
        cwd=str(proj),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    pv = Preview(project=proj, proc=proc, on_line=on_line, on_exit=on_exit,
                 listens=_listens_for_stop(proj))
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


def _listens_for_stop(proj: Path) -> bool:
    """Whether this project's generated UI carries the stop listener."""
    try:
        src = (proj / "ui" / "main_ui.py").read_text(encoding="utf-8",
                                                     errors="replace")
    except OSError:
        return False
    return "_watch_for_stop(self)" in src


def stop(project, grace: float = STOP_GRACE) -> bool:
    """Stop one project's preview, cleanly if it will go. True if something
    was running."""
    key = str(Path(project).resolve())
    with _LOCK:
        pv = _LIVE.pop(key, None)
    if pv is None:
        return False
    was = pv.running
    pv.stop(grace)
    return was


def stop_all(grace: float = STOP_GRACE) -> int:
    """Stop every preview — the tab-close / app-exit path.

    Every preview is asked FIRST and then all are waited on against one shared
    deadline, so three camera apps closing take one grace period, not three."""
    import time
    with _LOCK:
        pvs = list(_LIVE.values())
        _LIVE.clear()
    for pv in pvs:
        if pv.running:
            pv.grace = grace
            pv.ask_to_stop()
    deadline = time.monotonic() + grace
    for pv in pvs:
        if not pv.running:
            continue
        if pv.listens:
            try:
                pv.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                continue
            except subprocess.TimeoutExpired:
                pass
        pv._kill()
    return len(pvs)


# The backstop. Tab close and project switch call stop() explicitly; this
# catches the paths nobody remembered, including an unhandled exception on the
# way out. If the designer dies without running it (killed, or a hard crash),
# each preview's stdin pipe closes with it, and the generated app treats that
# end-of-file as a stop request — so it still closes cleanly.
atexit.register(stop_all)
