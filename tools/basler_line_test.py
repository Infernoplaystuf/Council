"""
tools/basler_line_test.py — a Basler camera's trigger cable, checked on the
PC the camera is plugged into (5 November 2026; council_core/basler_lines.py).

WHAT IT DOES, IN ORDER
  1. Lists every Basler pylon can see: model, serial, its input lines and
     each one's level now, and how Typhon watches them — by a counter of
     the camera's (no pulse missed) or by reading the line's level (a very
     short pulse can be missed).
  2. "Find my trigger line": watches every input of the first camera (or
     --serial's) for --seconds while you send a few pulses down the cable,
     and says which line they came on.
  3. With --runs: three short runs from that line in "Typhon (plugins)" —
     Triggered: exact (the camera's own trigger), Triggered: snapshot and
     Triggered: record (the camera live, the line watched) — each for
     --run-seconds while you send pulses; then how many frames each saved
     and after which triggers.

Everything is written under --out (default: a new basler_line_test_<time>
folder in the current folder): report.txt (what was printed) and each run's
files. Nothing else is touched — the runs use a throw-away plugin home,
project and vault in that folder. Offline: nothing leaves the PC.

    python tools/basler_line_test.py
    python tools/basler_line_test.py --seconds 20 --runs
    python tools/basler_line_test.py --serial 40012345 --line Line1 --runs

--fake runs the same against the test suite's fake Basler, with pulses sent
on Line1 by the script itself (a check of the script; no camera needed).
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Report:
    """Printed, and kept for report.txt."""

    def __init__(self) -> None:
        self.lines: List[str] = []

    def __call__(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(str(text))

    def save(self, folder: Path) -> Path:
        path = folder / "report.txt"
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path


def _fake() -> Any:
    """The test suite's fake Basler with its lines' levels, and a thread
    pulsing Line1 twice a second until the process ends."""
    from tests.test_basler_trigger import FakePylon
    from tests.test_typhon_plugins_basler_lines import LevelBasler

    cam = LevelBasler()

    def pulse_forever() -> None:
        while True:
            time.sleep(0.5)
            cam.pulse("Line1", 0.005)
    threading.Thread(target=pulse_forever, daemon=True).start()
    return FakePylon(cam)


def _wait(seconds: float, tick: Callable[[float], None]) -> None:
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return
        tick(left)
        time.sleep(min(1.0, left))


# ======================================================================
# 1 and 2: the cameras, their lines, and the line finder
# ======================================================================
def check_lines(sdk: Any, serial: str, seconds: float, edge: str,
                say: Report) -> Optional[tuple]:
    from council_core import basler_lines as bl
    from council_core import cameras

    backend = cameras.BaslerBackend(sdk)
    if not backend.available():
        say(f"pylon is not available: {backend.install_hint}")
        return None
    infos = backend.discover()
    if not infos:
        say("No Basler camera found. Check it is plugged in and that the "
            "pylon Viewer sees it.")
        return None
    say(f"{len(infos)} Basler camera(s):")
    for info in infos:
        say(f"  {info.label}")
    chosen = next((i for i in infos if not serial or i.serial == serial),
                  None)
    if chosen is None:
        say(f"No camera with serial {serial}.")
        return None
    say("")
    say(f"Camera: {chosen.label}")
    device = backend.open(chosen)
    try:
        lines = device.input_lines()
        if not lines:
            say("  It has no input line (every line is set to Output, or it "
                "has no LineSelector).")
            return chosen, None
        levels = device.line_states(lines)
        words = {True: "HIGH", False: "low", None: "cannot be read"}
        say("  Input lines now: " + ", ".join(
            f"{l} {words[levels.get(l)]}" for l in lines))
        say("")
        say(f"Find my trigger line: watching {', '.join(lines)} for "
            f"{seconds:g} s — send a few pulses down the trigger cable now.")
        watcher = device.watch_lines(lines, None, edge)
        try:
            say("  Watched by: " + ", ".join(
                f"{l} {watcher.method.get(l, '?')}" for l in lines))
            if watcher.note:
                say(f"  Note: {watcher.note}")
            _wait(seconds, lambda left: print(
                f"    {left:4.0f} s left — pulses so far: "
                + ", ".join(f"{l} {n}" for l, n in watcher.counts.items()),
                flush=True))
        finally:
            watcher.stop()
        if watcher.error:
            say(f"  While watching: {watcher.error}")
        say("  " + bl.find_words(watcher))
        seen = {l: n for l, n in watcher.counts.items() if n}
        return chosen, (max(seen, key=seen.get) if seen else None)
    finally:
        try:
            device.close()
        except Exception:                                 # noqa: BLE001
            pass


# ======================================================================
# 3: short runs from the line in "Typhon (plugins)"
# ======================================================================
def _side_project(folder: Path) -> Path:
    """A throw-away "Typhon (plugins)" project, plugin home and vault under
    `folder`, frame_camera attached to it (as the tests do)."""
    from council_core import camera_setup as cs
    from council_core import plugin_host as ph
    from council_core import plugin_lists as pl
    import frame_camera

    home = folder / "plugin_home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ[pl.HOME_ENV] = str(home)
    project = folder / "project"
    project.mkdir(parents=True, exist_ok=True)
    builtin = [pid for pid, _f in ph.BUILTIN_PLUGINS]
    pl.make_project(project, pl.read_base_set(home, builtin), home)
    frame_camera.disconnect()
    frame_camera._RIG._plugins = None
    frame_camera._LIVE.setup_path = cs.setup_path(project)
    frame_camera._LIVE.found = None
    return project


def runs(sdk: Any, serial: str, line: str, seconds: float, folder: Path,
         say: Report) -> None:
    import frame_camera
    from council_core import triggered as tg

    _side_project(folder)
    host = frame_camera._plugin_host()
    if sdk is not None:
        host.instance("typhon.basler").sdk = sdk
    rows = frame_camera.list_cameras()["rows"]
    row = next((r for r in rows if serial and serial in r), None) or next(
        (r for r in rows if "simulated" not in r.lower()), None)
    if row is None:
        say("No Basler in Typhon's camera list: " + "; ".join(rows))
        return
    frame_camera.connect(row)
    say(f"Connected in Typhon (plugins): {row}")
    plans = ((tg.MODE_EXACT, {tg.PER_KEY: 1}),
             (tg.MODE_SNAPSHOT, {tg.PER_KEY: 2}),
             (tg.MODE_RECORD, {tg.RECORD_KEY: 0.5}))
    try:
        for mode, extra in plans:
            say("")
            say(f"{mode} from {line}: {seconds:g} s — send a few pulses "
                f"now.")
            try:
                frame_camera.set_camera_setting(tg.MODE_KEY, mode)
                frame_camera.set_camera_setting(tg.SOURCE_KEY, line)
                for key, value in extra.items():
                    frame_camera.set_camera_setting(key, value)
                out_dir = folder / "runs" / mode.split(": ")[-1]
                started = frame_camera.start(str(out_dir))
            except Exception as exc:                      # noqa: BLE001
                say(f"  Refused: {exc}")
                continue
            say(f"  {started.get('summary', '')}")
            session = frame_camera._LIVE.session
            _wait(seconds, lambda left: print(
                f"    {left:4.0f} s left — frames saved: "
                f"{session.stats().recorded}", flush=True))
            frame_camera.stop()
            say("  " + _run_words(out_dir))
    finally:
        frame_camera.disconnect()


def _run_words(out_dir: Path) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ends = list(out_dir.glob("*_run_end.json"))
        if ends:
            break
        time.sleep(0.1)
    found = sorted(out_dir.glob("*_frames.csv"))
    if not found:
        return "No frames file was written."
    with open(found[-1], newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    triggers = sorted({r.get("trigger", "") for r in rows} - {""},
                      key=lambda t: int(t) if t.isdigit() else 0)
    return (f"{len(rows)} frame(s) saved, after {len(triggers)} trigger(s)"
            + (f" ({', '.join(triggers)})" if triggers else "")
            + f" — {found[-1].parent}")


# ======================================================================
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--serial", default="", help="the camera's serial "
                        "number (default: the first Basler found)")
    parser.add_argument("--seconds", type=float, default=10.0,
                        help="how long Find my trigger line watches")
    parser.add_argument("--edge", default="RisingEdge",
                        choices=("RisingEdge", "FallingEdge", "AnyEdge"))
    parser.add_argument("--line", default="", help="the trigger line for "
                        "--runs (default: the one found)")
    parser.add_argument("--runs", action="store_true",
                        help="also run exact, snapshot and record from it")
    parser.add_argument("--run-seconds", type=float, default=15.0)
    parser.add_argument("--out", default="", help="where the report and "
                        "runs go (default: a new folder here)")
    parser.add_argument("--fake", action="store_true",
                        help="the test suite's fake Basler, pulsed on Line1")
    args = parser.parse_args(argv)

    folder = Path(args.out or f"basler_line_test_"
                  f"{time.strftime('%Y%m%d_%H%M%S')}").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
    os.environ["COUNCIL_VAULT_ROOT"] = str(folder / "vault")
    say = Report()
    say(f"Basler trigger-cable test, {time.strftime('%Y-%m-%d %H:%M:%S')}")
    say(f"Output: {folder}")
    say("")
    sdk = _fake() if args.fake else None
    try:
        found = check_lines(sdk, args.serial, args.seconds, args.edge, say)
        if found is None:
            return 1
        chosen, line = found
        line = args.line or line
        if args.runs:
            say("")
            if not line:
                say("No line to run from: none pulsed. Give one with "
                    "--line Line1.")
            else:
                runs(sdk, chosen.serial, line, args.run_seconds, folder, say)
    except Exception as exc:                              # noqa: BLE001
        say(f"Stopped by an error: {type(exc).__name__}: {exc}")
        return 2
    finally:
        say("")
        say(f"Report: {say.save(folder)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
