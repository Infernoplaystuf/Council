"""
tools/typhon_plugins_timing.py — frame timing inside the real Qt Typhon with
a deliberately slow plugin, at Windows' default timer and at a 1 ms timer
(docs/typhon_plugins_design.md 10.2; stage P1 row 3).

WHY
A plugin on its own thread still shares Python's lock (the GIL) with the
camera's grab thread. The design's research measured that in a benchmark
process: a plugin holding the lock 20 ms per call made 313-340 of about 575
frames of a 150 fps camera late, mostly because of Windows' 15.6 ms timer
tick — and 0 with the process timer at 1 ms (timeBeginPeriod(1)) and
Python's switch interval at 1 ms. Inside a running Qt event loop the same
plugin made 113-125 late, so the design asked for the numbers again INSIDE
THE REAL TYPHON before choosing whether the core sets the finer timer while
it records (stage P3 row 5). This takes them.

WHAT ONE RUN IS
A child process builds nothing: it imports the "Typhon (plugins)" app the
parent built (examples/gui/typhon_plugins.gspec, generated for Qt) and makes
its window offscreen — the same window, live-view timer, slider and capture
code as the real app; never shown. It connects the simulated frame camera,
paced on a FIXED schedule (frame i is due at t0 + i/fps, as a real camera's
sensor clock is; a grab thread that falls behind gets the waiting frames at
once, as from a camera's buffer), starts a capture into a temporary folder
through the app's own Start button, runs Qt's event loop for `seconds`, and
stops through the Stop button. A plugin from the rig's plugin host
(frame_camera._RIG.plugins) gets every live frame through a LiveFeed —
on_frame on its own thread — and busies Python for `slow_ms` per frame
(pure Python, holding the lock), optionally capped to 25 % of the time.

  late      a frame reached the end of the grab thread's step (handed to
            the writers and the live view) more than one frame period
            after it was due
  read_late the same, measured when read() returned it

"1ms" runs ask Windows for a 1 ms timer (winmm timeBeginPeriod(1), and
SetProcessInformation so Windows 11 honours it even with no visible window)
and set Python's switch interval to 1 ms (default 5 ms). Each run is its own
process: the timer and the switch interval are per process. `timer_ms`
records how long a 1 ms Event.wait really took (median of 50), which shows
the timer in force.

    python tools/typhon_plugins_timing.py                       # the matrix
    python tools/typhon_plugins_timing.py --repeats 1 --seconds 2
    python tools/typhon_plugins_timing.py --out results.json

Everything in temporary folders (vault, app folder, plugin home, the capture
folder), removed afterwards. Offscreen; no window is shown; nothing leaves
the PC.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

TREE = Path(__file__).resolve().parents[1]

#: The plugins measured: (name, busy ms per frame, duty cap or None).
PLUGINS = {
    "none": (0.0, None),
    "noop": (0.0, None),
    "slow5": (5.0, None),
    "slow20": (20.0, None),
    "slow20cap": (20.0, 0.25),
}
#: default: as today. 1ms: Python's switch interval AND Windows' timer at
#: 1 ms (what P3 row 5 would set). switch / period: each of the two alone,
#: to tell which one matters.
TIMERS = ("default", "1ms", "switch", "period")


# ======================================================================
# One run, in its own process
# ======================================================================
def _set_fine_timer(switch: bool = True, period: bool = True
                    ) -> Dict[str, Any]:
    """1 ms switch interval and/or process timer; what Windows says after."""
    out: Dict[str, Any] = {}
    if switch:
        sys.setswitchinterval(0.001)
    if os.name != "nt" or not period:
        return out
    import ctypes
    from ctypes import wintypes

    class _Throttle(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG),
                    ("ControlMask", wintypes.ULONG),
                    ("StateMask", wintypes.ULONG)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    # ProcessPowerThrottling (4); PROCESS_POWER_THROTTLING_IGNORE_TIMER_
    # RESOLUTION (4) in ControlMask with it clear in StateMask: "always
    # honour this process's timer resolution requests" (Windows 11).
    state = _Throttle(1, 4, 0)
    kernel.SetProcessInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    out["honour_timer"] = bool(kernel.SetProcessInformation(
        kernel.GetCurrentProcess(), 4, ctypes.byref(state),
        ctypes.sizeof(state)))
    out["timeBeginPeriod"] = int(ctypes.WinDLL("winmm").timeBeginPeriod(1))
    return out


def _timer_resolution_ms() -> Optional[float]:
    """Windows' current timer resolution (NtQueryTimerResolution), ms."""
    if os.name != "nt":
        return None
    import ctypes

    low, high, now = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
    try:
        ctypes.WinDLL("ntdll").NtQueryTimerResolution(
            ctypes.byref(low), ctypes.byref(high), ctypes.byref(now))
    except (OSError, AttributeError):
        return None
    return now.value / 10000.0


def _event_wait_ms(n: int = 50) -> float:
    """Median time a 1 ms threading.Event.wait takes: the timer in force."""
    import threading

    event = threading.Event()
    took = []
    for _ in range(n):
        started = time.perf_counter()
        event.wait(0.001)
        took.append((time.perf_counter() - started) * 1000.0)
    return round(statistics.median(took), 3)


def _percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[k]


def child(spec: Dict[str, Any]) -> Dict[str, Any]:
    """One measured capture; returns the numbers."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
    timer = spec["timer"]
    slow_ms, duty = PLUGINS[spec["plugin"]]
    fps = float(spec["fps"])
    period = 1.0 / fps
    out: Dict[str, Any] = {"timer": timer, "plugin": spec["plugin"],
                           "fps": fps, "seconds": spec["seconds"],
                           "switch_interval_ms_before":
                               sys.getswitchinterval() * 1000.0}
    if timer != "default":
        out.update(_set_fine_timer(switch=timer in ("1ms", "switch"),
                                   period=timer in ("1ms", "period")))
    pdir = Path(spec["project"])
    sys.path.insert(0, str(TREE))
    sys.path.insert(0, str(pdir))

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    import frame_camera
    from council_core import cameras, capture
    from council_core import plugin_manifest as pm

    np = cameras._numpy()

    class FixedScheduleCamera(cameras.SyntheticDevice):
        """The simulated frame camera on a sensor clock: frame i is due at
        t0 + i * period whatever the grab thread does; a late grab gets
        the waiting frames at once, as from a camera's buffer."""

        def start(self) -> None:
            super().start()
            self._t0 = time.perf_counter()
            self._n = 0

        def read(self, timeout_ms: int = 1000) -> Any:
            if self._armed:
                return super().read(timeout_ms)
            step = self._interval()
            due = self._t0 + (self._n + 1) * step
            wait = due - time.perf_counter()
            if wait > timeout_ms / 1000.0:
                time.sleep(timeout_ms / 1000.0)
                return None
            if wait > 0:
                time.sleep(wait)
            self._n += 1
            got = time.perf_counter()
            made = self._with_downsample(self._picture(np))
            made.meta["bench_due"] = due
            made.meta["bench_read"] = got
            return made

    real_open = cameras.SyntheticBackend.open

    def bench_open(self: Any, info: Any) -> Any:
        if info.kind == "frame":
            return FixedScheduleCamera(info)
        return real_open(self, info)
    cameras.SyntheticBackend.open = bench_open

    qapp = QApplication.instance() or QApplication([])
    capture.warm_imports()
    import app as generated

    window = generated.App()
    window.resize(1504, 1016)
    window.on_btn_scan_for_cameras()
    rows = window.ports.cameras.items()
    index = next(i for i, r in enumerate(rows)
                 if "Simulated" in str(r) and str(r).endswith("frame"))
    window.ports.cameras.widget.setCurrentRow(index)
    window.on_btn_connect()
    slot = frame_camera._RIG.active
    # The FPS box beside Start, as a user sets it: Start applies it.
    window.ports.frame_rate.set(str(int(fps) if fps == int(fps) else fps))
    frame_camera.apply_frame_rate(fps)
    out["timer_resolution_ms"] = _timer_resolution_ms()
    out["event_wait_1ms"] = _event_wait_ms()
    out["switch_interval_ms"] = sys.getswitchinterval() * 1000.0

    host = frame_camera._RIG.plugins
    feed = None
    if spec["plugin"] != "none":
        doc = {"schema": 1, "id": "typhon.bench", "short": "bench",
               "name": "Slow bench plugin", "version": "0.1.0",
               "api": pm.API_VERSION, "kind": "feature", "scope": "camera",
               "entry": "plugin:Bench", "budget_ms": 5}

        class Bench:
            def activate(self, ctx: Any) -> None:
                self.ctx = ctx

            def on_frame(self, view: Any) -> None:
                if slow_ms <= 0:
                    return
                until = time.perf_counter() + slow_ms / 1000.0
                while time.perf_counter() < until:     # pure Python: the GIL
                    sum(range(200))
        host.adopt(pm.check(doc, builtin=True), Bench)
        feed = host.feed("typhon.bench", duty=duty)

    late: List[float] = []
    read_late: List[float] = []
    recording = {"on": False}

    def on_frame(shot: Any) -> None:
        now = time.perf_counter()
        meta = shot.meta or {}
        due = meta.get("bench_due")
        if recording["on"] and due is not None:
            late.append(now - due)
            read_late.append(meta["bench_read"] - due)
        if feed is not None:
            feed.offer(shot)

    folder = Path(spec["folder"])
    window.ports.capture_folder.set(str(folder))

    def begin() -> None:
        slot.session.on_frame = on_frame
        window.on_btn_start_capture()
        recording["on"] = True
        QTimer.singleShot(int(float(spec["seconds"]) * 1000), finish)

    held: Dict[str, Any] = {}

    def finish() -> None:
        recording["on"] = False
        held["session"] = slot.session
        window.on_btn_stop_capture()
        deadline = time.monotonic() + 30
        while slot.session is not None and slot.session.saving and \
                time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        held["stats"] = slot.session.run_stats()
        qapp.quit()

    QTimer.singleShot(500, begin)        # the live view runs first, as in use
    qapp.exec()
    stats = held["stats"]
    if feed is not None:
        out["feed"] = feed.stats()
        feed.stop()
    frame_camera.disconnect()
    host.close()

    lateness = [x * 1000.0 for x in late]
    out.update({
        "frames": len(lateness),
        "late": sum(1 for x in late if x > period),
        "late_2": sum(1 for x in late if x > 2 * period),
        "median_ms": round(statistics.median(lateness), 3)
        if lateness else 0.0,
        "p99_ms": round(_percentile(lateness, 0.99), 3),
        "max_ms": round(max(lateness), 3) if lateness else 0.0,
        "read_late": sum(1 for x in read_late if x > period),
        "read_p99_ms": round(_percentile([x * 1000.0 for x in read_late],
                                         0.99), 3),
        "grabbed": stats.grabbed, "saved": stats.recorded,
        "not_saved_storage": stats.skipped,
        "faults": sorted(host.faults),
    })
    try:
        import PySide6

        out["pyside6"] = PySide6.__version__
    except Exception:                                     # noqa: BLE001
        pass
    out["python"] = sys.version.split()[0]
    return out


# ======================================================================
# The matrix
# ======================================================================
def build_app(where: Path) -> Path:
    """Build "Typhon (plugins)" for Qt into a temporary vault."""
    sys.path.insert(0, str(TREE))
    import run_example_gui as rex

    return rex.build("typhon_plugins", project="timing",
                     vault_dir=where / "vault", target="qt")


def sandbox_env(where: Path) -> Dict[str, str]:
    env = dict(os.environ)
    env.update({
        "QT_QPA_PLATFORM": "offscreen", "COUNCIL_NO_DIALOGS": "1",
        "COUNCIL_VAULT_ROOT": str(where / "vault"),
        "COUNCIL_APP_DIR": str(where),
        "TYPHON_PLUGIN_HOME": str(where / "typhon"),
        "COUNCIL_SKIP_PATH_MIGRATION": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    env.pop("FRAME_CLASSES_STORE", None)
    return env


def run_matrix(plugins: List[str], timers: List[str], repeats: int,
               seconds: float, fps: float, python: str,
               keep: bool = False, say=print) -> List[Dict[str, Any]]:
    where = Path(tempfile.mkdtemp(prefix="tptiming_"))
    results: List[Dict[str, Any]] = []
    try:
        env = sandbox_env(where)
        os.environ.update({k: env[k] for k in (
            "COUNCIL_VAULT_ROOT", "COUNCIL_APP_DIR", "TYPHON_PLUGIN_HOME",
            "COUNCIL_SKIP_PATH_MIGRATION")})
        pdir = build_app(where)
        for rep in range(repeats):
            for timer in timers:
                for plugin in plugins:
                    folder = where / f"runs_{rep}_{timer}_{plugin}"
                    spec = {"timer": timer, "plugin": plugin,
                            "seconds": seconds, "fps": fps,
                            "project": str(pdir), "folder": str(folder)}
                    proc = subprocess.run(
                        [python, str(Path(__file__).resolve()), "--child",
                         json.dumps(spec)], capture_output=True, text=True,
                        timeout=180 + seconds * 4, env=env, cwd=str(TREE),
                        encoding="utf-8")
                    line = next((l for l in reversed(proc.stdout.splitlines())
                                 if l.startswith("{")), "")
                    if proc.returncode != 0 or not line:
                        result = {"timer": timer, "plugin": plugin,
                                  "error": (proc.stderr or proc.stdout)[-800:]}
                    else:
                        result = json.loads(line)
                    result["repeat"] = rep + 1
                    results.append(result)
                    say(row(result))
                    shutil.rmtree(folder, ignore_errors=True)
    finally:
        if not keep:
            shutil.rmtree(where, ignore_errors=True)
    return results


def row(r: Dict[str, Any]) -> str:
    if "error" in r:
        return f"{r['timer']:>7} {r['plugin']:>10}  ERROR {r['error'][-300:]}"
    feed = r.get("feed") or {}
    return (f"{r['timer']:>7} {r['plugin']:>10}  late {r['late']:>3}/"
            f"{r['frames']:<4} (>2 periods {r['late_2']:>3})  median "
            f"{r['median_ms']:6.2f} ms  p99 {r['p99_ms']:6.2f} ms  max "
            f"{r['max_ms']:6.1f} ms  read-late {r['read_late']:>3}  saved "
            f"{r['saved']}/{r['grabbed']}  timer {r['timer_resolution_ms']}"
            f" ms, 1ms-wait {r['event_wait_1ms']:.2f} ms  plugin "
            f"{feed.get('delivered', '-')} calls, "
            f"{feed.get('dropped', '-')} dropped")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--plugins", default="none,noop,slow5,slow20,slow20cap")
    ap.add_argument("--timers", default="default,1ms")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--fps", type=float, default=150.0)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", help="write every run's numbers here (JSON)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the temporary folders")
    args = ap.parse_args(argv)
    if args.child:
        print(json.dumps(child(json.loads(args.child))))
        return 0
    plugins = [p for p in args.plugins.split(",") if p]
    timers = [t for t in args.timers.split(",") if t]
    for p in plugins:
        if p not in PLUGINS:
            ap.error(f"unknown plugin {p!r}: {', '.join(PLUGINS)}")
    for t in timers:
        if t not in TIMERS:
            ap.error(f"unknown timer {t!r}: {', '.join(TIMERS)}")
    started = time.time()
    results = run_matrix(plugins, timers, args.repeats, args.seconds,
                         args.fps, args.python, keep=args.keep)
    print(f"{len(results)} runs in {time.time() - started:.0f} s")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2) + "\n",
                                  encoding="utf-8")
    return 0 if all("error" not in r for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
