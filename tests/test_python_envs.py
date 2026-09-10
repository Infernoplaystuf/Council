"""
The per-project interpreter — which Python runs a generated app, and can it.

A camera app cannot run under the Council's own Python: vendor SDKs ship
compiled for particular Pythons and live in their own env. Each project names
its interpreter in its manifest, and before a launch that interpreter checks
itself — tkinter, every required package (imported, so a wrong-ABI build is
caught), and that the generated files compile under it.

Several tests use real conda envs on the machine and skip when absent.

Run:  python -m pytest tests/test_python_envs.py -q
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_projects as gpj        # noqa: E402
import gui_runner as run          # noqa: E402
import python_envs as pe          # noqa: E402


def _env_or_skip(name):
    py = pe.find_env(name)
    if not py:
        pytest.skip(f"conda env {name!r} not on this machine")
    return py


# ============================================================
# Resolving the setting
# ============================================================

def test_blank_means_the_councils_own_python():
    res = pe.resolve("")
    assert res.error == "" and res.python == sys.executable
    assert res.label == pe.DEFAULT_LABEL


def test_a_packaged_build_has_no_python_to_fall_back_to(monkeypatch):
    """In DatasInferno.exe sys.executable is the app itself; launching main.py
    with it would start a second Council, not the preview."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    res = pe.resolve("")
    assert res.python == "" and "choose one" in res.error


def test_a_conda_env_resolves_by_name():
    py = _env_or_skip("council")
    assert pe.resolve("council").python == py
    assert Path(py).is_file()


def test_an_unknown_env_says_which_envs_exist():
    res = pe.resolve("definitely_not_an_env_xyz")
    assert res.python == "" and "no conda env named" in res.error


def test_an_env_folder_resolves_to_its_python(tmp_path):
    env = tmp_path / "myenv"
    env.mkdir()
    fake = env / "python.exe"
    fake.write_text("")
    assert pe.resolve(str(env)).python == str(fake)


def test_a_missing_path_is_an_error_not_a_guess():
    res = pe.resolve(r"C:\no\such\place\python.exe")
    assert res.python == "" and "does not exist" in res.error


def test_list_envs_finds_this_machines_envs():
    names = [n for n, _ in pe.list_envs()]
    if not names:
        pytest.skip("no conda on this machine")
    assert len(names) == len(set(names)), "an env is listed twice"
    assert all(Path(p).is_file() for _, p in pe.list_envs())


# ============================================================
# The self-check
# ============================================================

def test_the_councils_python_checks_itself():
    pr = pe.probe(sys.executable)
    assert pr.ok, pr
    assert pr.version.startswith(f"{sys.version_info[0]}.{sys.version_info[1]}")
    assert pr.tkinter


def test_a_missing_package_is_named_with_what_to_install():
    pr = pe.probe(sys.executable, modules=["definitely_missing_pkg_xyz"])
    assert not pr.ok
    assert "definitely_missing_pkg_xyz" in pr.missing
    lines = pe.describe(pe.resolve(""), pr)
    assert lines[0].startswith("Not started")
    assert any("install definitely_missing_pkg_xyz" in l for l in lines)


@pytest.mark.parametrize("module,hint", [
    ("PIL", "Pillow"), ("cv2", "opencv-python"),
    ("metavision_core", "Metavision SDK"), ("pypylon", "pypylon"),
])
def test_install_hints_name_the_package_not_the_import(module, hint):
    assert hint in pe.install_hint(module)


def test_a_file_that_does_not_compile_is_reported(tmp_path):
    bad = tmp_path / "broken.py"
    bad.write_text("def (:\n", encoding="utf-8")
    pr = pe.probe(sys.executable, files=[str(bad)])
    assert not pr.ok and str(bad) in pr.compile_errors


def test_newer_syntax_is_caught_under_an_older_target(tmp_path):
    """The policy gate judges code against the Council's own Python, so a
    `match` passes it and then crashes a 3.9 vendor env. Measured on 3.9."""
    py39 = _env_or_skip("VoxRecorder")
    f = tmp_path / "uses_match.py"
    f.write_text("match 1:\n    case 1:\n        pass\n", encoding="utf-8")
    pr = pe.probe(py39, files=[str(f)])
    assert pr.version.startswith("3.9")
    assert str(f) in pr.compile_errors


def test_an_import_that_crashes_the_interpreter_is_named(tmp_path, monkeypatch):
    """Measured on this machine: an env whose numpy was pip-installed dies
    inside the import with 0xC06D007F and no traceback. find_spec would call
    it fine; the probe must say WHICH package took the interpreter down."""
    (tmp_path / "crashme_xyz.py").write_text("import os\nos._exit(7)\n",
                                             encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    pr = pe.probe(sys.executable, modules=["json", "crashme_xyz", "csv"])
    assert not pr.ok
    assert "crashme_xyz" in pr.missing
    assert "CRASHED" in pr.missing["crashme_xyz"]


def test_the_probe_never_raises_on_a_bad_interpreter(tmp_path):
    pr = pe.probe(str(tmp_path / "not_python.exe"))
    assert not pr.ok and pr.error


# ============================================================
# Where the setting lives, and that the runner uses it
# ============================================================

def test_the_manifest_keeps_the_setting(tmp_path):
    pdir = gpj.create("p", vault_dir=tmp_path)
    man = gpj.load_manifest(pdir)
    assert man.python == ""
    man.python = "pylon"
    gpj.save_manifest(pdir, man)
    assert gpj.load_manifest(pdir).python == "pylon"
    # and it is a real key on disk, not something a later save would drop
    assert json.loads((pdir / "manifest.json").read_text())["python"] == "pylon"


def test_the_runner_launches_under_the_chosen_python(tmp_path):
    target = pe.find_env("VoxRecorder") or sys.executable
    (tmp_path / "main.py").write_text(
        "import sys\nprint('child=' + sys.executable)\n", encoding="utf-8")
    out = []
    pv = run.start(tmp_path, on_line=lambda t, lv: out.append(t),
                   python=target)
    pv.proc.wait(timeout=60)
    end = time.time() + 5
    while time.time() < end and not any(t.startswith("child=") for t in out):
        time.sleep(0.1)
    got = next(t for t in out if t.startswith("child="))[len("child="):]
    assert Path(got).resolve() == Path(target).resolve()


def test_run_example_records_the_choice_and_refuses_a_missing_env(
        tmp_path, monkeypatch):
    import run_example_gui as rex
    # main() builds into the default vault; if the refusal ever regressed it
    # must build into a temp folder, never the user's real one.
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "vault"))
    pdir = rex.build("barbie_capture", project="p1", vault_dir=tmp_path,
                     python="council")
    assert gpj.load_manifest(pdir).python == "council"

    with pytest.raises(SystemExit) as exc:
        rex.main(["barbie_capture", "--project", "p2", "--no-run",
                  "--python", "definitely_not_an_env_xyz"])
    assert "no conda env named" in str(exc.value)


# ============================================================
# preflight / run_checked / the Run-with widget — the logic that used to
# live inside the designer tab, where nothing could test it
# ============================================================

def _built(tmp_path, name="pre"):
    import run_example_gui as rex
    return rex.build("barbie_capture", project=name, vault_dir=tmp_path)


def test_preflight_passes_a_clean_project(tmp_path):
    pf = pe.preflight(_built(tmp_path), "")
    assert pf.ok and pf.python == sys.executable
    assert pf.lines[0].startswith("Run with")


def test_preflight_stops_at_the_gate_before_touching_any_python(tmp_path):
    pdir = _built(tmp_path)
    h = pdir / "handlers.py"
    h.write_text("from pypylon import pylon\n" + h.read_text(encoding="utf-8"),
                 encoding="utf-8")
    pf = pe.preflight(pdir, "definitely_not_an_env_xyz")
    assert not pf.ok and pf.python == ""
    assert "policy gate refused" in pf.lines[0]
    assert any("pypylon" in l for l in pf.lines)


def test_preflight_reports_a_declared_package_the_python_lacks(tmp_path):
    pf = pe.preflight(_built(tmp_path), "", requires=["definitely_missing_xyz"])
    assert not pf.ok
    assert any("missing definitely_missing_xyz" in l for l in pf.lines)


@pytest.mark.parametrize("spec", ["", "pylon", r"C:\envs\cam\python.exe"])
def test_dropdown_choices_round_trip_to_the_setting(spec):
    assert pe.spec_from_choice(pe.display(spec)) == spec


def test_dropdown_lists_default_envs_and_browse():
    vals = pe.choices()
    assert vals[0] == pe.DEFAULT_LABEL and vals[-1] == pe.BROWSE_LABEL
    assert pe.choices(r"C:\x\python.exe")[-2] == r"C:\x\python.exe"


def test_run_checked_launches_through_the_callbacks(tmp_path):
    """The designer's Run, with Tk's after() replaced by a plain queue: every
    UI touch must go through call_soon, so the worker never touches Tk."""
    import queue
    pdir = _built(tmp_path, "rc")
    log, q = [], queue.Queue()
    t = run.run_checked(pdir, log=log.append, call_soon=q.put)
    t.join(timeout=90)
    end = time.time() + 30
    while time.time() < end and not any("preview running" in l for l in log):
        try:
            q.get(timeout=0.5)()
        except queue.Empty:
            pass
    try:
        assert log[0].startswith("checking")
        assert any("ready" in l for l in log)
        assert any("preview running" in l for l in log)
    finally:
        run.stop(pdir, grace=3)


def test_the_run_with_widget_saves_to_the_open_project(tmp_path, tk_root):
    import tkinter as tk
    import gui_runwith as grw
    py = pe.find_env("council")
    if not py:
        pytest.skip("conda env 'council' not on this machine")
    pdir = _built(tmp_path, "rw")
    top = tk.Toplevel(tk_root)
    try:
        logged = []
        box = grw.RunWithBox(top, get_dir=lambda: pdir, log=logged.append)
        box.sync()
        assert box.var.get() == pe.DEFAULT_LABEL
        box.fill()
        assert "conda: council" in box.box.cget("values")
        box.var.set("conda: council")
        box._picked()
        assert gpj.load_manifest(pdir).python == "council"
        box.var.set("something else")
        box.sync()
        assert box.var.get() == "conda: council"
    finally:
        top.destroy()


def test_project_files_are_everything_the_app_runs(tmp_path):
    import run_example_gui as rex
    pdir = rex.build("barbie_capture", project="pf", vault_dir=tmp_path)
    names = {Path(f).name for f in pe.project_files(pdir)}
    assert {"main_ui.py", "ports.py", "widgets.py", "app.py", "handlers.py",
            "main.py"} <= names
