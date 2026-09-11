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


# ============================================================
# The probe answers through a file (adversarial review, 2026-09)
# ============================================================
#
# Its answer used to be a marker line on stdout, which any package imported
# during the check could break, fake, or hold open.

def _module(tmp_path, monkeypatch, name, body):
    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))


def test_a_banner_with_no_newline_is_not_a_crash(tmp_path, monkeypatch):
    """MEASURED: print('SDK v1.2 loaded', end='') glued the marker onto the
    banner, and a working Python was reported as having CRASHED (exit 0)."""
    _module(tmp_path, monkeypatch, "noisy_sdk_xyz",
            "import sys\nsys.stdout.write('SDK v1.2 loaded')\n")
    pr = pe.probe(sys.executable, modules=["json", "noisy_sdk_xyz"])
    assert pr.ok, (pr.missing, pr.error)


def test_import_output_cannot_answer_for_the_probe(tmp_path, monkeypatch):
    _module(tmp_path, monkeypatch, "spoof_sdk_xyz",
            "print('__PROBE__{\"tkinter\": \"8.6\", \"missing\": {}}')\n"
            "raise ImportError('really missing')\n")
    pr = pe.probe(sys.executable, modules=["spoof_sdk_xyz"])
    assert not pr.ok and "spoof_sdk_xyz" in pr.missing


def test_a_package_that_swaps_stdout_is_fine(tmp_path, monkeypatch):
    _module(tmp_path, monkeypatch, "swap_sdk_xyz",
            "import io, sys\nsys.stdout = io.StringIO()\n")
    assert pe.probe(sys.executable, modules=["swap_sdk_xyz"]).ok


def test_a_helper_process_holding_the_output_does_not_stall_it(
        tmp_path, monkeypatch):
    """MEASURED: an import that started a helper sharing stdout made
    probe(timeout=5) take 20 s and report a working SDK as hanging."""
    _module(tmp_path, monkeypatch, "helper_sdk_xyz",
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(20)'], stdout=sys.stdout, "
            "stderr=sys.stderr)\n")
    t0 = time.time()
    pr = pe.probe(sys.executable, modules=["helper_sdk_xyz"], timeout=15)
    assert pr.ok, (pr.missing, pr.error)
    assert time.time() - t0 < 12


def test_the_probe_sees_the_projects_own_modules(tmp_path):
    """The probe ran from the Council's cwd, so a helper module beside
    main.py was 'missing' although the app imports it fine."""
    (tmp_path / "camlib_xyz.py").write_text("X = 1\n", encoding="utf-8")
    assert not pe.probe(sys.executable, modules=["camlib_xyz"]).ok
    pr = pe.probe(sys.executable, modules=["camlib_xyz"],
                  path=[str(tmp_path)], cwd=str(tmp_path))
    assert pr.ok, pr.missing


def test_preflight_passes_a_project_with_a_helper_module(tmp_path):
    pdir = _built(tmp_path, "helper")
    (pdir / "camlib_xyz.py").write_text("def grab():\n    return 1\n",
                                        encoding="utf-8")
    app = pdir / "app.py"
    app.write_text(app.read_text(encoding="utf-8") + "\nimport camlib_xyz\n",
                   encoding="utf-8")
    pf = pe.preflight(pdir, "")
    assert pf.ok, pf.lines


def test_an_undeclared_startup_import_the_python_lacks_is_caught():
    """numpy is allowed undeclared, and the stdlib is judged by the
    Council's own version (tomllib, 3.11+) — so the gate cannot know whether
    the target has them. 'ready' then crashed on open."""
    pr = pe.probe(sys.executable, locate=["json", "no_such_module_xyz"])
    assert not pr.ok and list(pr.missing) == ["no_such_module_xyz"]


def test_a_stdlib_module_the_target_lacks_says_which_python_to_use():
    pr = pe.Probe(False, "3.9.21", "8.6", missing={"tomllib": pe.NOT_FOUND})
    lines = pe.describe(pe.Resolved("x", "x", "conda env 'x'"), pr)
    assert any("standard library" in l and "tomllib" in l for l in lines)


def test_tomllib_under_python_39_is_refused(tmp_path):
    py39 = _env_or_skip("VoxRecorder")
    pdir = _built(tmp_path, "toml")
    app = pdir / "app.py"
    app.write_text(app.read_text(encoding="utf-8") + "\nimport tomllib\n",
                   encoding="utf-8")
    pf = pe.preflight(pdir, py39)
    assert not pf.ok, pf.lines
    assert any("tomllib" in l and "standard library" in l
               for l in pf.lines), pf.lines
    assert not any("does not compile" in l for l in pf.lines), pf.lines


def test_startup_imports_skip_optional_and_lazy_ones(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("import os\nfrom pathlib import Path\nfrom . import x\n"
                 "try:\n    import optional_xyz\nexcept ImportError:\n"
                 "    pass\n"
                 "def f():\n    import lazy_xyz\n", encoding="utf-8")
    assert pe.startup_imports([str(f)]) == ["os", "pathlib"]


# ============================================================
# Run and Stop while the check is still running
# ============================================================

def _pump(q, until, seconds=30):
    import queue
    end = time.time() + seconds
    while time.time() < end and not until():
        try:
            q.get(timeout=0.2)()
        except queue.Empty:
            pass


def test_stop_during_the_check_cancels_the_run(tmp_path):
    """MEASURED: stop() returned False, nothing was logged, and the preview
    opened when the check finished — a camera app opening its device after
    the last thing the user pressed was Stop."""
    import queue
    pdir = _built(tmp_path, "cancel")
    log, q = [], queue.Queue()
    t = run.run_checked(pdir, log=log.append, call_soon=q.put)
    assert run.stop(pdir) is True
    t.join(timeout=90)
    _pump(q, lambda: any("cancelled" in l for l in log))
    try:
        assert any("Run cancelled: Stop was pressed" in l for l in log), log
        assert not any("preview running" in l for l in log)
        assert not run.is_running(pdir)
    finally:
        run.stop(pdir, grace=3)


def test_a_newer_run_replaces_one_still_being_checked(tmp_path):
    import queue
    pdir = _built(tmp_path, "twice")
    log, q = [], queue.Queue()
    t1 = run.run_checked(pdir, log=log.append, call_soon=q.put)
    t2 = run.run_checked(pdir, log=log.append, call_soon=q.put)
    t1.join(timeout=90)
    t2.join(timeout=90)
    _pump(q, lambda: any("preview running" in l for l in log)
          and any("replaced" in l for l in log))
    try:
        assert sum("preview running" in l for l in log) == 1, log
        assert any("a newer Run replaced it" in l for l in log)
    finally:
        run.stop(pdir, grace=3)


def test_a_check_that_raises_says_so_instead_of_hanging(tmp_path,
                                                        monkeypatch):
    """MEASURED: an exception in preflight killed the worker silently and
    Run sat at 'checking ...' for ever."""
    import queue

    def boom(*a, **k):
        raise ValueError("bad probe answer")
    pdir = _built(tmp_path, "boom")
    monkeypatch.setattr(pe, "preflight", boom)
    log, q = [], queue.Queue()
    t = run.run_checked(pdir, log=log.append, call_soon=q.put)
    t.join(timeout=30)
    _pump(q, lambda: len(log) > 1, seconds=5)
    assert any("check itself failed" in l and "bad probe answer" in l
               for l in log), log


def test_run_example_saves_an_absolute_python_path(tmp_path, monkeypatch):
    """A relative --python was saved as typed, and the designer's Run later
    resolved it against a different working directory."""
    import run_example_gui as rex
    if not sys.platform.startswith("win"):
        pytest.skip("uses python.exe as the relative path")
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "vault"))
    exe = Path(sys.executable)
    monkeypatch.chdir(exe.parent)
    rc = rex.main(["barbie_capture", "--project", "abs", "--no-run",
                   "--python", exe.name])
    assert rc == 0
    pdir = next(p for p in (tmp_path / "vault").rglob("abs") if p.is_dir())
    saved = gpj.load_manifest(pdir).python
    assert Path(saved).is_absolute() and Path(saved) == exe
