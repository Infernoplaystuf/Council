"""
tools/typhon_plugins_gate.py — the branch gate of `typhon-plugins`.

The plugin design (docs/typhon_plugins_design.md, 7.2) puts one gate on
every commit of the branch: every existing Typhon test passes UNCHANGED,
plus the branch's own tests and, from stage P3, the plugin contract suites.
This runs exactly that set, one pytest process per file (a file's Qt, its
camera fakes and its module state never leak into the next one), a few at a
time, offscreen, each with its own temporary folder.

    python tools/typhon_plugins_gate.py               # the whole gate
    python tools/typhon_plugins_gate.py --list        # the files, nothing run
    python tools/typhon_plugins_gate.py --check-list  # is every Typhon test
                                                      # file in the gate?
    python tools/typhon_plugins_gate.py tests/test_frame_camera.py ...
                                                      # some of them

Options: --jobs N (default 4), --python EXE (default: this interpreter),
--out DIR (logs and junit files; default a new folder under the system
temp), --tree DIR (the checkout to test; default the one this file is in).

Exit status 0 only when every file passed (a file that collects nothing,
such as tests/test_basler_real.py outside the `pylon` environment, counts as
passed and is named). A failure of a test on KNOWN_FLAKY alone reruns that
file once and says so; anything else is a failure, reported as it is.

WHAT IS "A TYPHON TEST FILE"
GATE_FILES below, by hand, grouped. `--check-list` (and
tests/test_typhon_plugins_branch.py) compares it with every test file that
names Typhon's modules, its examples or the example folder (TYPHON_WORDS),
so a Typhon test file added later — on the branch or merged from
qt-migration — cannot be left out silently: it must be added here or put on
NOT_GATED with the reason.

RETIRING, NOT FIXING (design 7.2, 8.2)
A Barbie test that fails on the branch is retired there and then: its name
goes on RETIRED in tests/test_retired_tests.py and in CLAUDE.md's "Retired
tests" list, with the reason, and the test is deleted (a test shared with
Typhon loses only its Barbie parameters). It is not fixed and not left red.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Sequence

TREE = Path(__file__).resolve().parents[1]

#: The existing Typhon test files, as qt-migration has them. Every one must
#: pass unchanged on the branch.
TYPHON_FILES = (
    # the camera module, the capture and its records
    "tests/test_frame_camera.py",
    "tests/test_frame_camera_setup.py",
    "tests/test_typhon.py",
    "tests/test_typhon_capture.py",
    "tests/test_cameras.py",
    "tests/test_capture.py",
    "tests/test_capture_data_safety.py",
    "tests/test_capture_review.py",
    "tests/test_capture_tab.py",
    "tests/test_fast_png.py",
    "tests/test_event_playback.py",
    "tests/test_reduce.py",
    "tests/test_triggered.py",
    # camera settings, presets, set-up and the Basler code
    "tests/test_camera_settings.py",
    "tests/test_camera_settings_window.py",
    "tests/test_camera_presets.py",
    "tests/test_camera_setup.py",
    "tests/test_camera_wizard.py",
    "tests/test_settings_tabs.py",
    "tests/test_basler_scan.py",
    "tests/test_basler_trigger.py",
    "tests/test_basler_real.py",       # collects nothing outside `pylon`
    # the libraries the feature plugins will wrap (Barbie v1-v3 use them too)
    "tests/test_frame_classes.py",
    "tests/test_frame_timing.py",
    "tests/test_gui_roi.py",
    # Barbie v4/v5: free checks on frame_camera while they pass (8.2)
    "tests/test_barbie_v4.py",
    # the Designer: builds, updates and wires Typhon, or every example (so a
    # new example, typhon_plugins.gspec, is checked by them too)
    "tests/test_designer_examples.py",
    "tests/test_designer_update.py",
    "tests/test_designer_wiring.py",
    "tests/test_designer_wiring_qt.py",
    "tests/test_designer_zoom_qt.py",
    "tests/test_designer_geometry.py",
    "tests/test_designer_tab.py",
    "tests/test_designer_codebehind.py",
    "tests/test_gui_settings.py",
    "tests/test_gui_smoke.py",
    # builds and policy-checks every example, typhon_plugins included, once
    # qt-migration's policy-gate fix (e40d319, 6884069) is merged: listed
    # before that merge, which would otherwise stop --check-list
    "tests/test_gui_policy.py",
    "tests/test_gui_examples.py",
    "tests/test_qt_wireframes.py",
    "tests/test_qt_target.py",
    "tests/test_gui_emit.py",
    "tests/test_gui_emit_qt.py",
    "tests/test_gui_qt_runtime.py",
    "tests/test_gui_requires.py",
    "tests/test_gui_errors.py",
    "tests/test_gui_stop.py",
    "tests/test_gui_codebehind.py",
    "tests/test_gui_describe.py",
    "tests/test_gui_describe_small.py",
    "tests/test_python_envs.py",
    "tests/test_project_workspace.py",
    # a retired test must not come back in a merge
    "tests/test_retired_tests.py",
)

#: The branch's own test files: found by name, so a new one is in the gate
#: the moment it exists. The contract suites (stage P3) join here.
BRANCH_GLOBS = (
    "tests/test_typhon_plugins*.py",
    "tests/test_camera_rig*.py",
    "tests/contracts/test_*.py",
    "typhon_plugins/*/tests/test_*.py",
)

#: Test files that name a Typhon word but are not Typhon tests, and why.
NOT_GATED: Dict[str, str] = {
    "tests/test_var_graph.py":
        "uses frame_timing.py's name as a sample script's; tests the "
        "variable graph, not Typhon",
}

#: Words that make a test file a Typhon test file (--check-list).
TYPHON_WORDS = re.compile(
    r"frame_camera|typhon|barbie_capture|"
    r"council_core(?:\.|\s+import\s+)(?:cameras|capture|camera_\w+|"
    r"triggered|reduce|event_playback|basler_scan|live_display)\b|"
    r"settings_tabs|camera_settings_window|capture_review|camera_wizard|"
    r"preset_picker|classifier_picker|frame_classes|frame_timing|frame_roi|"
    r"gui_examples|EXAMPLES_DIR|\"examples\"\s*/\s*\"gui\"",
    re.IGNORECASE)

#: Tests known to fail now and then for reasons outside the branch. A failure
#: of these alone reruns the file once.
KNOWN_FLAKY = (
    "tests/test_gui_settings.py::"
    "test_the_warm_list_test_still_catches_a_path_per_module",
)

#: Seconds one file may take before it is stopped and counted failed.
FILE_TIMEOUT = 40 * 60


def branch_files(tree: Path = TREE) -> List[str]:
    out: List[str] = []
    for pattern in BRANCH_GLOBS:
        for p in sorted(tree.glob(pattern)):
            rel = p.relative_to(tree).as_posix()
            if rel not in out:
                out.append(rel)
    return out


def gate_files(tree: Path = TREE) -> List[str]:
    """Every file of the gate, the existing Typhon files first."""
    out = [f for f in TYPHON_FILES]
    out += [f for f in branch_files(tree) if f not in out]
    return out


def typhon_test_files(tree: Path = TREE) -> List[str]:
    """Every test file that names a Typhon word (see TYPHON_WORDS)."""
    out = []
    for p in sorted((tree / "tests").glob("test_*.py")):
        text = p.read_text(encoding="utf-8", errors="replace")
        if TYPHON_WORDS.search(text):
            out.append(p.relative_to(tree).as_posix())
    return out


def list_problems(tree: Path = TREE) -> List[str]:
    """What is wrong with GATE_FILES against the tree, one line each."""
    gate = set(gate_files(tree))
    problems = []
    for f in sorted(gate):
        if not (tree / f).is_file():
            problems.append(f"{f} is in the gate but is not there")
    for f in typhon_test_files(tree):
        if f not in gate and f not in NOT_GATED:
            problems.append(f"{f} names Typhon but is not in the gate: add "
                            f"it to TYPHON_FILES, or to NOT_GATED with why")
    return problems


def _short_temp() -> Path:
    """A short root for the per-file temporary folders: pytest's own names
    under a long root pass Windows' 260-character limit."""
    base = Path(tempfile.gettempdir())
    root = Path(tempfile.mkdtemp(prefix="tpg", dir=str(base)))
    return root


def _env(temp: Path) -> Dict[str, str]:
    env = dict(os.environ)
    env.update(QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1",
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
               PYTEST_DEBUG_TEMPROOT=str(temp))
    return env


def has_pytest_timeout(python: str) -> bool:
    """Whether `python` has the pytest-timeout plugin (its --timeout option
    is refused, and the file runs nothing, where it is missing)."""
    try:
        done = subprocess.run(
            [python, "-c", "import importlib.util, sys; sys.exit(0 if "
             "importlib.util.find_spec('pytest_timeout') else 1)"],
            capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _stem(f: str) -> str:
    return f.replace("/", "__")


def _counts(xml: Path) -> Dict[str, object]:
    out: Dict[str, object] = {"passed": 0, "failed": 0, "errors": 0,
                              "skipped": 0, "bad": []}
    if not xml.is_file():
        return out
    try:
        root = ET.parse(str(xml)).getroot()
    except ET.ParseError:
        return out
    for tc in root.iter("testcase"):
        kids = {c.tag for c in tc}
        if "failure" in kids:
            out["failed"] = int(out["failed"]) + 1
            out["bad"].append(tc.get("name", ""))          # type: ignore
        elif "error" in kids:
            out["errors"] = int(out["errors"]) + 1
            out["bad"].append(tc.get("name", ""))          # type: ignore
        elif "skipped" in kids:
            out["skipped"] = int(out["skipped"]) + 1
        else:
            out["passed"] = int(out["passed"]) + 1
    return out


def _only_flaky(f: str, bad: Sequence[str]) -> bool:
    flaky = {k.split("::", 1)[1] for k in KNOWN_FLAKY
             if k.split("::", 1)[0] == f}
    names = {b.split("[", 1)[0] for b in bad}
    return bool(names) and names <= flaky


def _say(line: str) -> None:
    print(line, flush=True)


def run(files: Sequence[str], *, tree: Path = TREE, python: str = "",
        jobs: int = 4, out: Optional[Path] = None,
        say=_say) -> int:
    """Run `files` one process each, `jobs` at a time. 0 when all passed."""
    python = python or sys.executable
    # pytest-timeout's per-test limit where the interpreter has it (the
    # `pylon` env has not: pytest refused the option and ran nothing);
    # FILE_TIMEOUT below bounds every file either way.
    timeout_args = ([f"--timeout={FILE_TIMEOUT // 2}"]
                    if has_pytest_timeout(python) else [])
    out = Path(out) if out else Path(tempfile.mkdtemp(prefix="typhon_gate_"))
    out.mkdir(parents=True, exist_ok=True)
    temp_root = _short_temp()
    queue = list(files)
    running: List[dict] = []
    results: Dict[str, dict] = {}
    retried: List[str] = []
    started = time.time()
    n = 0

    def launch(f: str) -> None:
        nonlocal n
        n += 1
        temp = temp_root / f"j{n:03d}"
        temp.mkdir()
        base = out / _stem(f)
        cmd = [python, "-m", "pytest", f, "-q", "-rfEs", "-p",
               "no:cacheprovider", *timeout_args,
               f"--junitxml={base}.xml", "-o", "junit_family=xunit2"]
        log = open(f"{base}.log", "w", encoding="utf-8")
        p = subprocess.Popen(cmd, cwd=str(tree), env=_env(temp), stdout=log,
                             stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL)
        running.append({"file": f, "p": p, "log": log, "t": time.time()})

    while queue or running:
        while queue and len(running) < max(1, jobs):
            launch(queue.pop(0))
        time.sleep(0.5)
        for r in running[:]:
            rc = r["p"].poll()
            if rc is None and time.time() - r["t"] > FILE_TIMEOUT:
                subprocess.run(["taskkill", "/F", "/T", "/PID",
                                str(r["p"].pid)], capture_output=True)
                rc = "TIMEOUT"
            if rc is None:
                continue
            r["log"].close()
            running.remove(r)
            f = r["file"]
            c = _counts(out / f"{_stem(f)}.xml")
            if (rc == 1 and f not in retried
                    and _only_flaky(f, c["bad"])):        # type: ignore
                retried.append(f)
                say(f"  {f}: only known-flaky test(s) failed "
                    f"({', '.join(c['bad'])}); running the file again")
                queue.insert(0, f)
                continue
            results[f] = {"rc": rc, "secs": round(time.time() - r["t"], 1),
                          **c}
            ok = rc in (0, 5)
            say(f"  {'ok  ' if ok else 'FAIL'} {f}: {c['passed']} passed, "
                f"{c['failed']} failed, {c['errors']} errors, "
                f"{c['skipped']} skipped (rc={rc}, {results[f]['secs']} s)")
    total = {k: sum(int(r[k]) for r in results.values())
             for k in ("passed", "failed", "errors", "skipped")}
    bad = [f for f, r in results.items() if r["rc"] not in (0, 5)]
    empty = [f for f, r in results.items() if r["rc"] == 5]
    summary = {"tree": str(tree), "python": python, "files": len(results),
               "seconds": round(time.time() - started), "totals": total,
               "failed_files": bad, "collected_nothing": empty,
               "reran_for_known_flaky": retried, "results": results}
    tmp = out / "gate.json.tmp"
    tmp.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    os.replace(tmp, out / "gate.json")
    say(f"gate: {len(results)} files, {total['passed']} passed, "
        f"{total['failed']} failed, {total['errors']} errors, "
        f"{total['skipped']} skipped in {summary['seconds']} s")
    if empty:
        say("collected nothing (counted as passed): " + ", ".join(empty))
    if retried:
        say("rerun once for a known-flaky test: " + ", ".join(retried))
    for f in bad:
        r = results[f]
        say(f"FAILED {f} (rc={r['rc']}): {', '.join(r['bad']) or 'see log'}"
            f"  -> {out / (_stem(f) + '.log')}")
    say(f"logs: {out}")
    if bad:
        # pytest keeps a failing test's folder; so does the gate, to look in.
        say(f"temporary folders of the run, kept: {temp_root}")
    else:
        # The gate's own folders, made above; nothing else is under them.
        shutil.rmtree(temp_root, ignore_errors=True)
    return 0 if not bad else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="*", help="some gate files (default all)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check-list", action="store_true")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--python", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--tree", default=str(TREE))
    a = ap.parse_args(argv)
    tree = Path(a.tree).resolve()
    if a.list:
        for f in gate_files(tree):
            print(f)
        return 0
    if a.check_list:
        problems = list_problems(tree)
        for p in problems:
            print(p)
        print("the gate list is complete" if not problems
              else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    problems = list_problems(tree)
    if problems and not a.files:
        for p in problems:
            print("gate list:", p)
        return 2
    files = [f.replace("\\", "/") for f in a.files] or gate_files(tree)
    _say(f"typhon-plugins gate: {len(files)} file(s), {a.jobs} at a time, "
         f"in {tree}")
    return run(files, tree=tree, python=a.python, jobs=a.jobs,
               out=Path(a.out) if a.out else None)


if __name__ == "__main__":
    sys.exit(main())
