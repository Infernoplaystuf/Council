"""
typhon_plugin_api.check — the plugin author's check (docs/typhon_plugins_
design.md 3.11, 9; stage P5 row 2; the guide: docs/typhon_camera_plugins.md).

    python -m typhon_plugin_api.check <plugin folder> [--no-tests]

Run with a Council checkout on PYTHONPATH (or from inside one). It checks a
plugin's folder the way Typhon reads one from someone else, then drives it,
offscreen, in the fake host (typhon_plugin_api.fake_host) — temporary
plugin home and vault, simulated cameras — and finally runs the folder's
own tests/ with pytest, if it has any. Nothing is written into the folder.

What it checks:
  * the folder: plugin.json under the third-party rules (an id with a dot
    and no underscore, not typhon.*; the API version this Typhon has), no
    compiled code, at most 2,000 files;
  * the code: it imports Typhon only through typhon_plugin_api, never
    pickle, and Qt only from a panel module (not the entry's);
  * loading: the entry imports from the folder's own bytes and the object
    activates;
  * a CAMERA plugin (the contract of tests/contracts/test_camera_plugins.py,
    for one plugin): a backend name with a dot; available() answers without
    raising; discover() lists CameraInfo of its own backend; for the first
    camera found, open, limits, area, settings, start, read (a frame that
    owns a uint8 or uint16 array, within the timeout plus 100 ms), stop,
    close — twice; capabilities only those the manifest declares and Typhon
    knows; categories, record_fields (plain JSON), review_source; and then
    Typhon itself connects that camera and records 3 frames, and the saved
    files, the frames CSV and the end record must agree. With no camera
    connected the open checks are skipped and said so;
  * a FEATURE plugin: with the simulated camera, Typhon records 3 frames
    with it switched on and nothing goes wrong;
  * its own tests/, when the folder has them.

Exit status 0 when nothing failed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

#: The Council checkout this module is part of.
COUNCIL = Path(__file__).resolve().parent.parent

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

#: Modules of the Council a plugin may not import (it imports
#: typhon_plugin_api only).
COUNCIL_MODULES = ("council_core", "council_qt", "frame_camera",
                   "frame_classes", "frame_roi", "typhon_plugins",
                   "council_engine")
QT = ("PySide6", "PyQt5", "PyQt6", "PySide2")


class Report:
    def __init__(self, out: Callable[[str], Any] = print) -> None:
        self.lines: List[tuple] = []
        self.out = out

    def add(self, status: str, what: str, detail: str = "") -> None:
        self.lines.append((status, what, detail))
        self.out(f"  {status}  {what}" + (f" — {detail}" if detail else ""))

    def check(self, what: str, fn: Callable[[], Any]) -> Any:
        """Run `fn`; PASS unless it raises (FAIL, with what it raised).
        An AssertionError's message is the reason."""
        try:
            result = fn()
        except _Skip as exc:
            self.add(SKIP, what, str(exc))
            return None
        except Exception as exc:                          # noqa: BLE001
            self.add(FAIL, what, f"{type(exc).__name__}: {exc}"
                     if not isinstance(exc, AssertionError) else str(exc))
            return None
        self.add(PASS, what, result if isinstance(result, str) else "")
        return result

    @property
    def failed(self) -> int:
        return sum(1 for s, _w, _d in self.lines if s == FAIL)


class _Skip(Exception):
    pass


# ======================================================================
# The folder and its code
# ======================================================================
def _imports(path: Path) -> List[str]:
    tree = ast.parse(path.read_bytes(), filename=str(path))
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and \
                node.module:
            names.append(node.module)
    return names


def check_code(folder: Path, entry_module: str) -> str:
    entry = folder / (entry_module.replace(".", "/") + ".py")
    bad: List[str] = []
    for path in sorted(folder.rglob("*.py")):
        rel = path.relative_to(folder).as_posix()
        if rel.startswith("tests/"):
            continue
        for name in _imports(path):
            top = name.split(".")[0]
            if top in COUNCIL_MODULES:
                bad.append(f"{rel} imports {name}: import Typhon only "
                           f"through typhon_plugin_api")
            elif top == "pickle":
                bad.append(f"{rel} imports pickle: Typhon's house rule is "
                           f"JSON only")
            elif top in QT and path == entry:
                bad.append(f"{rel} (the entry) imports {top}: Qt goes in a "
                           f"panel module only, so a plugin loads without "
                           f"a window")
    assert not bad, "; ".join(bad)
    return ""


# ======================================================================
# A camera plugin
# ======================================================================
def camera_checks(report: Report, fake: Any, m: Any) -> None:
    import numpy as np

    import typhon_plugin_api as api
    from council_core import camera_capabilities as caps_rule
    from council_core import plugin_host as ph

    plugin = fake.instance(m.id)

    def backend() -> str:
        name = getattr(plugin, "backend", "")
        assert isinstance(name, str) and name, \
            "the plugin object has no `backend` (its cameras' family name)"
        problem = ph.backend_problem(name, builtin=False)
        assert not problem, problem
        return name
    name = report.check("its backend name", backend)
    if name is None:
        return

    def available() -> str:
        answer = plugin.available()
        assert isinstance(answer, tuple) and len(answer) == 2, \
            "available() returns (ready, what to install)"
        ok, hint = answer
        if not ok:
            assert hint, "available() says no without saying what to install"
            raise _Skip(f"not available here: {hint}")
        return ""
    ready = report.check("available() answers", available)
    if ready is None:
        report.add(SKIP, "the camera checks", "its SDK is not available")
        return

    def discover() -> List[Any]:
        found = plugin.discover()
        assert isinstance(found, list), "discover() returns a list"
        for info in found:
            assert isinstance(info, api.CameraInfo), \
                f"{info!r} is not an api.CameraInfo"
            assert info.backend == name, \
                f"{info.key}'s backend is {info.backend!r}, not {name!r}"
            assert info.kind in ("frame", "event"), \
                f"{info.key}'s kind is {info.kind!r}"
        return found
    found = report.check("discover() lists its cameras", discover)
    if not found:
        report.add(SKIP, "open, read and record", "no camera found: connect "
                   "one and run the check again")
        return
    info = found[0]
    declared = set(m.capabilities)

    def open_read_close() -> str:
        for _round in range(2):
            device = plugin.open(info)
            try:
                limits = device.limits()
                assert isinstance(limits, api.Limits), \
                    "limits() returns an api.Limits"
                roi = device.roi()
                assert isinstance(roi, api.Roi), "roi() returns an api.Roi"
                got = device.set_roi(roi)
                assert isinstance(got, api.Roi) and got == roi, \
                    f"set_roi(roi()) gave {got}, not {roi}"
                for setting in device.settings():
                    assert isinstance(setting, api.Setting), \
                        f"{setting!r} is not an api.Setting"
                caps = plugin.capabilities(device)
                extra = set(caps) - declared
                assert not extra, (f"it confirms {sorted(extra)}, which its "
                                   f"manifest's capabilities do not declare")
                unknown = set(caps) - set(caps_rule.known(caps))
                assert not unknown, f"Typhon does not know {sorted(unknown)}"
                fields = plugin.record_fields(device)
                json.dumps(fields, allow_nan=False)
                device.start()
                frame = None
                deadline = time.monotonic() + 5
                while frame is None and time.monotonic() < deadline:
                    began = time.monotonic()
                    frame = device.read(1000)
                    took = time.monotonic() - began
                    assert took < 1.1, (f"read(1000) took {took:.2f} s: it "
                                        f"returns within the timeout + 0.1 s")
                assert frame is not None, "no frame within 5 s of start()"
                assert isinstance(frame, api.Frame), \
                    "read() returns an api.Frame"
                image = frame.image
                assert isinstance(image, np.ndarray), \
                    "a frame's image is a numpy array"
                assert image.dtype in (np.uint8, np.uint16), \
                    f"a frame's image is uint8 or uint16, not {image.dtype}"
                assert image.ndim in (2, 3), "shape (h, w) or (h, w, 3)"
                assert image.base is None or image.flags.owndata, \
                    "a frame owns its array (a copy, never a view of an " \
                    "SDK buffer)"
                device.stop()
            finally:
                device.close()
        return f"{info.label}: {image.shape[1]} x {image.shape[0]} {image.dtype}"
    report.check("open, area, settings, start, read, stop, close (twice)",
                 open_read_close)

    def hints() -> str:
        made = plugin.categories()
        assert isinstance(made, api.CategoryHints), \
            "categories() returns an api.CategoryHints"
        source = plugin.review_source()
        assert source is None or isinstance(source, api.ReviewSource), \
            "review_source() is None or an api.ReviewSource"
        return ""
    report.check("categories() and review_source()", hints)

    def record() -> str:
        fake.connect(info.kind, label=info.label)
        run = fake.capture(frames=3)
        problems = run.problems()
        assert problems == [], "; ".join(problems)
        assert m.id in run.record.get("plugins", {}), \
            "the run's camera record names the plugin"
        return f"{len(run.pngs())} frames saved, records agree"
    report.check("Typhon connects it and records 3 frames", record)


def feature_checks(report: Report, fake: Any, m: Any) -> None:
    def record() -> str:
        fake.connect("frame")
        run = fake.capture(frames=3)
        problems = run.problems()
        assert problems == [], "; ".join(problems)
        assert m.id not in fake.faults, \
            f"it was switched off: {fake.faults[m.id].reason}"
        return ""
    report.check("a 3-frame recording with it on (simulated camera)", record)


# ======================================================================
# The whole check
# ======================================================================
def run_check(folder: Any, *, own_tests: bool = True,
              out: Callable[[str], Any] = print) -> Report:
    from council_core import plugin_host as ph
    from council_core import plugin_manifest as pm
    from typhon_plugin_api.fake_host import FakeHost

    folder = Path(folder).resolve()
    report = Report(out)
    out(f"Checking {folder}")

    m = report.check("plugin.json (the rules for a plugin from someone "
                     "else)", lambda: pm.read(folder, builtin=False))
    if m is None:
        return report

    def rules() -> str:
        assert "_" not in m.id, (f"the id {m.id!r} holds an underscore: "
                                 f"lower-case letters and digits between "
                                 f"dots")
        problem = pm.api_problem(m.api)
        assert not problem, problem
        compiled, too_many = ph._compiled_files(folder)
        assert not compiled, (f"compiled code ({', '.join(compiled[:3])}): "
                              f"ship source only, and delete __pycache__")
        assert not too_many, f"more than {ph.MAX_FILES} files"
        return f"{m.id} {m.version}, a {m.kind} plugin, plugin API {m.api}"
    if report.check("the folder", rules) is None:
        return report
    report.check("its imports", lambda: check_code(folder, m.module))
    if m.python:
        problem = ph.python_problem(m.python)
        report.add(FAIL if problem else PASS, "its Python packages",
                   problem)

    with tempfile.TemporaryDirectory(prefix="typhon_check_") as tmp:
        fake = FakeHost(tmp)
        try:
            def load() -> str:
                fake.load(folder)
                made = fake.instance(m.id)
                assert made is not None, (
                    "it did not load: " + fake.host.state(m.id)[1])
                return ""
            if report.check("it loads and activates", load) is not None:
                if m.kind == "camera":
                    camera_checks(report, fake, m)
                elif m.kind == "feature":
                    feature_checks(report, fake, m)
        finally:
            fake.close()

    tests = folder / "tests"
    if own_tests and tests.is_dir():
        report.check("its own tests (pytest)", lambda: _own_tests(tests))
    elif own_tests:
        report.add(SKIP, "its own tests", "the folder has no tests/")
    return report


def _own_tests(tests: Path) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(COUNCIL)] + [p for p in env.get("PYTHONPATH", "").split(
            os.pathsep) if p])
    env["PYTHONDONTWRITEBYTECODE"] = "1"        # nothing into the folder
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("COUNCIL_NO_DIALOGS", "1")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--rootdir", str(tests), str(tests)],
        env=env, capture_output=True, text=True, cwd=str(tests))
    tail = (done.stdout.strip().splitlines() or [""])[-1]
    if done.returncode == 5:
        raise _Skip("tests/ holds no tests")
    assert done.returncode == 0, (done.stdout[-3000:] + done.stderr[-2000:])
    return tail


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m typhon_plugin_api.check",
        description="Check a Typhon plugin's folder the way Typhon reads "
                    "it, and drive it in the fake host.")
    parser.add_argument("folder", help="the plugin's folder (holding "
                                       "plugin.json)")
    parser.add_argument("--no-tests", action="store_true",
                        help="do not run the folder's own tests/")
    args = parser.parse_args(argv)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
    sys.dont_write_bytecode = True
    if str(COUNCIL) not in sys.path:
        sys.path.insert(0, str(COUNCIL))
    report = run_check(args.folder, own_tests=not args.no_tests)
    failed = report.failed
    print(f"\n{'FAILED' if failed else 'OK'}: {failed} failed, "
          f"{sum(1 for s, *_ in report.lines if s == PASS)} passed, "
          f"{sum(1 for s, *_ in report.lines if s == SKIP)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
