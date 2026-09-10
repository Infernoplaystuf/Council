"""
Clean Stop — a preview gets to release its hardware before it goes.

On Windows, Popen.terminate() is TerminateProcess: an immediate hard kill in
which no finally, atexit or window-close handler runs. Measured before this
change: stop() returned in 0.01 s with exit code 1 and none of three cleanup
markers written. For a camera app that means StopGrabbing and Close never run
and a recording is never finalised — and if the designer itself crashed, the
preview was orphaned with nothing able to stop it.

Now Stop sends "stop" on the child's stdin, the generated app closes itself
through on_close, and kill() is only the fallback. Every test below runs real
processes; on_close writes a marker file standing in for a camera release.

Run:  python -m pytest tests/test_gui_stop.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import gui_emit as ge            # noqa: E402
import gui_policy as pol         # noqa: E402
import gui_runner as run         # noqa: E402
import run_example_gui as rex    # noqa: E402

STUB_TAIL = ('        after this returns, and closes anyway if this raises."""\n'
             '        pass')


def _project(tmp_path, name, on_close_body):
    """A real generated project whose on_close runs ``on_close_body``."""
    pdir = rex.build("barbie_capture", project=name, vault_dir=tmp_path / "v")
    h = pdir / "handlers.py"
    src = h.read_text(encoding="utf-8")
    assert STUB_TAIL in src, "the generated on_close stub changed shape"
    marker = (pdir / "closed.txt").as_posix()
    body = "".join(f"        {line}\n" for line in
                   on_close_body.replace("MARKER", marker).splitlines())
    h.write_text(src.replace(STUB_TAIL, STUB_TAIL[:-len("        pass")] + body),
                 encoding="utf-8")
    return pdir


def _wait_until(pred, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


# ============================================================
# What generation writes
# ============================================================

def test_the_generated_ui_listens_for_stop_and_passes_the_gate(tmp_path):
    pdir = rex.build("barbie_capture", project="gen", vault_dir=tmp_path)
    src = (pdir / "ui" / "main_ui.py").read_text(encoding="utf-8")
    assert "_watch_for_stop(self)" in src
    assert "WM_DELETE_WINDOW" in src and "def request_close" in src
    for mode in pol.MODES:
        ok, errs = pol.validate(src, mode)
        assert ok, (mode, errs)


def test_a_new_project_gets_a_documented_on_close_to_fill_in(tmp_path):
    pdir = rex.build("barbie_capture", project="doc", vault_dir=tmp_path)
    src = (pdir / "handlers.py").read_text(encoding="utf-8")
    assert "def on_close(self)" in src and "camera" in src


# ============================================================
# The four ways an app ends
# ============================================================

def test_stop_runs_on_close_and_exits_cleanly(tmp_path):
    pdir = _project(tmp_path, "s1", "open(r'MARKER', 'w').write('released')")
    lines = []
    pv = run.start(pdir, on_line=lambda t, lv: lines.append((lv, t)))
    try:
        time.sleep(3)
        assert pv.stop() is True, "the app had to be killed"
        assert pv.proc.returncode == 0
        assert (pdir / "closed.txt").read_text() == "released"
        assert _wait_until(lambda: any("closed cleanly" in t for _, t in lines))
    finally:
        pv._kill()


def test_a_crashed_designer_still_lets_the_app_clean_up(tmp_path):
    """The designer dies with no atexit and no stop_all. The preview's stdin
    closes with it, and the app treats that end-of-file as a stop request."""
    pdir = _project(tmp_path, "s2", "open(r'MARKER', 'w').write('released')")
    pidfile = tmp_path / "child.pid"
    helper = tmp_path / "designer.py"
    helper.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, r'{REPO}')\n"
        "import gui_runner as run\n"
        f"pv = run.start(r'{pdir}')\n"
        f"open(r'{pidfile}', 'w').write(str(pv.pid))\n"
        "time.sleep(3)\n"
        "os._exit(0)\n", encoding="utf-8")
    subprocess.run([sys.executable, str(helper)], timeout=60)
    assert _wait_until(lambda: (pdir / "closed.txt").exists()), \
        "the orphaned app never ran on_close"
    pid = int(pidfile.read_text())
    assert _wait_until(lambda: str(pid) not in subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True,
        text=True).stdout) if os.name == "nt" else True


def test_a_hung_app_is_still_killed_after_the_grace_period(tmp_path):
    pdir = _project(tmp_path, "s3", "import time\ntime.sleep(60)")
    lines = []
    pv = run.start(pdir, on_line=lambda t, lv: lines.append((lv, t)))
    try:
        time.sleep(3)
        t0 = time.time()
        assert pv.stop(grace=1.0) is False
        assert time.time() - t0 < 10
        assert pv.forced and not pv.running
        assert _wait_until(lambda: any("within 1 s" in t for _, t in lines))
    finally:
        pv._kill()


def test_closing_the_window_with_the_listener_live_is_clean(tmp_path):
    """The reader thread is still parked on stdin when the window's X closes
    the app. Reading sys.stdin there can abort the interpreter at shutdown
    ('could not acquire lock for stdin'); the raw-fd read must not."""
    pdir = _project(tmp_path, "s4", "open(r'MARKER', 'w').write('released')")
    drv = tmp_path / "xclose.py"
    drv.write_text(
        "from pathlib import Path\n"
        "main_py = Path.cwd() / 'main.py'\n"
        "boot = main_py.read_text(encoding='utf-8').split('from app import main')[0]\n"
        "exec(compile(boot, str(main_py), 'exec'), {'__file__': str(main_py)})\n"
        "import tkinter as tk\n"
        "from app import App\n"
        "root = tk.Tk(); app = App(root); app.pack()\n"
        "root.after(1200, app.request_close)\n"
        "root.mainloop()\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(drv)], cwd=str(pdir),
                       env=dict(os.environ, COUNCIL_PREVIEW_CONTROL="stdin"),
                       stdin=subprocess.PIPE, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr[-500:]
    assert "Fatal Python error" not in r.stderr
    assert (pdir / "closed.txt").exists()


def test_a_failing_on_close_still_closes_the_window(tmp_path):
    pdir = _project(tmp_path, "s5", "raise RuntimeError('camera busy')")
    lines = []
    pv = run.start(pdir, on_line=lambda t, lv: lines.append((lv, t)))
    try:
        time.sleep(3)
        assert pv.stop() is True
        assert _wait_until(lambda: any("on_close failed" in t and
                                       "camera busy" in t for _, t in lines))
    finally:
        pv._kill()


def test_a_project_generated_before_clean_stop_is_killed_at_once(tmp_path):
    """No listener in its ui/: waiting out the grace period would only make
    Stop feel broken, so it is killed straight away and the log says why."""
    pdir = tmp_path / "legacy"
    pdir.mkdir()
    (pdir / "main.py").write_text("import time\ntime.sleep(60)\n",
                                  encoding="utf-8")
    lines = []
    pv = run.start(pdir, on_line=lambda t, lv: lines.append((lv, t)))
    try:
        assert pv.listens is False
        t0 = time.time()
        pv.stop()
        assert time.time() - t0 < 3
        assert _wait_until(lambda: any("Generate it again" in t
                                       for _, t in lines))
    finally:
        pv._kill()


def test_stop_all_waits_on_one_shared_deadline(tmp_path):
    """Three hung apps closing at designer exit take one grace, not three."""
    pdirs = [_project(tmp_path, f"a{i}", "import time\ntime.sleep(60)")
             for i in range(3)]
    pvs = [run.start(p) for p in pdirs]
    try:
        time.sleep(3)
        t0 = time.time()
        run.stop_all(grace=1.5)
        took = time.time() - t0
        assert all(not pv.running for pv in pvs)
        assert took < 1.5 * 2, f"stop_all took {took:.1f}s for three apps"
    finally:
        for pv in pvs:
            pv._kill()


def test_non_ascii_output_survives_the_pipe(tmp_path):
    """A child Python writes its locale code page unless told otherwise, and
    the runner reads UTF-8 — measured garbled on a 3.9 child."""
    pdir = tmp_path / "utf"
    pdir.mkdir()
    (pdir / "main.py").write_text("print('Δ café µm')\n", encoding="utf-8")
    lines = []
    pv = run.start(pdir, on_line=lambda t, lv: lines.append(t))
    pv.proc.wait(timeout=30)
    assert _wait_until(lambda: any("Δ café µm" in t for t in lines))
