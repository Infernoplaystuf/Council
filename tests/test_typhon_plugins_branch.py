"""
The typhon-plugins branch, beside today's Typhon (plugin design 7.1, 7.2;
stage P0 row 1; docs/typhon_plugins_branch.md):

  * the branch gate's list (tools/typhon_plugins_gate.py) holds every Typhon
    test file;
  * run-typhon-plugins.bat starts this checkout's Council on a side vault,
    app folder and plugin home, creates nothing under ~/.council, refuses a
    side folder that is the Council's own, and clears an inherited
    classifier-store override;
  * with that environment, every folder the Council and Typhon resolve is a
    side one;
  * tools/copy_classifier_library.py brings a library into the side store
    reading the original only — the original is the same, file for file,
    byte for byte and time for time.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import typhon_plugins_gate as gate          # noqa: E402
import copy_classifier_library as ccl        # noqa: E402
from tests import sandbox_vault              # noqa: E402

LAUNCHER = ROOT / "run-typhon-plugins.bat"
windows_only = pytest.mark.skipif(os.name != "nt",
                                  reason="a Windows batch file")

#: What the launcher sets, and the session sandbox sets too.
SIDE_VARIABLES = ("COUNCIL_VAULT_ROOT", "COUNCIL_APP_DIR",
                  "TYPHON_PLUGIN_HOME", "COUNCIL_SKIP_PATH_MIGRATION")


# ======================================================================
# The gate's list
# ======================================================================
def test_the_gate_holds_every_typhon_test_file():
    assert gate.list_problems() == []


def test_the_gate_holds_the_branchs_own_tests():
    files = gate.gate_files()
    for mine in ("tests/test_camera_rig.py",
                 "tests/test_typhon_plugins_branch.py",
                 "tests/test_typhon_plugins_example.py"):
        assert mine in files, mine
    assert len(files) == len(set(files))
    assert files[:len(gate.TYPHON_FILES)] == list(gate.TYPHON_FILES)


def test_the_gate_holds_the_policy_test_before_qt_migration_needs_it():
    """qt-migration's policy-gate fix makes tests/test_gui_policy.py build
    every example through gui_examples.names() and name frame_camera, so
    merging it would stop the gate ('names Typhon but is not in the gate')
    until the file was listed (P1 review, 16 October 2026). It is listed
    already: it builds and policy-checks every example, typhon_plugins
    included."""
    assert "tests/test_gui_policy.py" in gate.TYPHON_FILES


def test_todays_typhon_lists_no_plugin_module_among_its_python_scripts(
        tmp_path):
    """Settings > Python Scripts follows import statements and constant
    import_module() names from the app's files. The rig's lazy import of the
    plugin host put the host, the lists, the manifest reader, the plugin API
    and python_envs on today's Typhon's list, which never loads them, and
    made the cold list slower (P1 review: 35 files to 41 for the typhon
    example). Only the rig itself is new on the list."""
    import gui_settings
    app = tmp_path / "app"
    app.mkdir()
    (app / "app.py").write_text("import frame_camera\n", encoding="utf-8")
    names = {s.name for s in gui_settings.scripts_in_use(app, ROOT,
                                                         modules={})}
    assert "frame_camera" in names and "council_core.camera_rig" in names
    for gone in ("council_core.plugin_host", "council_core.plugin_lists",
                 "council_core.plugin_manifest", "typhon_plugin_api"):
        assert gone not in names, gone


def test_a_new_typhon_test_file_left_out_is_named(tmp_path):
    (tmp_path / "tests").mkdir()
    for f in gate.TYPHON_FILES:
        (tmp_path / f).write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_new_thing.py").write_text(
        "import frame_camera\n", encoding="utf-8")
    (tmp_path / "tests" / "test_unrelated.py").write_text(
        "import json\n", encoding="utf-8")
    problems = gate.list_problems(tmp_path)
    assert problems == [
        "tests/test_new_thing.py names Typhon but is not in the gate: add "
        "it to TYPHON_FILES, or to NOT_GATED with why"]


def test_only_a_known_flaky_failure_reruns_a_file():
    f, name = gate.KNOWN_FLAKY[0].split("::")
    assert gate._only_flaky(f, [name])
    assert not gate._only_flaky(f, [name, "test_other"])
    assert not gate._only_flaky("tests/test_frame_camera.py", [name])
    assert not gate._only_flaky(f, [])


def test_the_gate_runs_under_an_interpreter_without_pytest_timeout(
        tmp_path, monkeypatch):
    """The `pylon` env has no pytest-timeout: given --timeout, pytest
    refused the option and every file ran nothing (rc 4). The option is
    passed only where the plugin is; the gate's own per-file limit holds
    either way."""
    import importlib.util
    assert gate.has_pytest_timeout(sys.executable) is (
        importlib.util.find_spec("pytest_timeout") is not None)
    assert gate.has_pytest_timeout(str(tmp_path / "no_python.exe")) is False
    tree = tmp_path / "tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "tests" / "test_tiny.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(gate, "has_pytest_timeout", lambda python: False)
    out = tmp_path / "out"
    rc = gate.run(["tests/test_tiny.py"], tree=tree, python=sys.executable,
                  jobs=1, out=out, say=lambda line: None)
    assert rc == 0
    assert "--timeout" not in (out / "tests__test_tiny.py.log").read_text(
        encoding="utf-8")
    assert gate._counts(out / "tests__test_tiny.py.xml")["passed"] == 1


# ======================================================================
# The launcher
# ======================================================================
def _run(bat, *args, env_extra=None, home=None):
    env = {k: v for k, v in os.environ.items()
           if k not in SIDE_VARIABLES + ("TYPHON_PLUGINS_VAULT",
                                         "TYPHON_PLUGINS_APP",
                                         "TYPHON_PLUGINS_SIDE",
                                         "FRAME_CLASSES_STORE")}
    if home is not None:
        env["USERPROFILE"] = str(home)
    env.update(env_extra or {})
    return subprocess.run(["cmd", "/d", "/c", str(bat), *args], env=env,
                          capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL)


def _where(out):
    return dict(line.split("=", 1) for line in out.splitlines()
                if "=" in line)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@windows_only
def test_where_names_the_side_folders_and_creates_nothing(home):
    done = _run(LAUNCHER, "--where", home=home)
    assert done.returncode == 0, done.stdout + done.stderr
    where = _where(done.stdout)
    assert Path(where["vault"]) == home / "Typhon-plugins-vault"
    assert Path(where["app"]) == home / "Typhon-plugins-app"
    assert Path(where["plugin_home"]) == home / "Typhon-plugins-app" / "typhon"
    assert Path(where["classifiers"]) == \
        home / "Typhon-plugins-vault" / "classifiers"
    assert Path(where["checkout"]) == ROOT
    assert list(home.iterdir()) == [], "--where creates nothing"


@windows_only
@pytest.mark.parametrize("variable,value", [
    ("TYPHON_PLUGINS_VAULT", r"{home}\.council\vault"),
    ("TYPHON_PLUGINS_VAULT", r"{home}\.COUNCIL"),
    ("TYPHON_PLUGINS_APP", r"{home}\.council"),
    ("TYPHON_PLUGINS_APP", r"{home}\.council\side"),
])
def test_a_side_folder_inside_the_councils_own_is_refused(
        home, variable, value):
    done = _run(LAUNCHER, "--where", home=home,
                env_extra={variable: value.format(home=home)})
    assert done.returncode == 3, done.stdout
    assert "Refused" in done.stdout and ".council" in done.stdout
    assert list(home.iterdir()) == []


@windows_only
@pytest.mark.parametrize("inherited", [
    r"{home}\Typhon-plugins-vault",          # the same folder
    r"{home}\Typhon-plugins-vault\inner",    # inside the side vault
    r"{home}",                               # holding it
])
def test_the_vault_this_window_already_names_is_refused(home, inherited):
    """A window whose COUNCIL_VAULT_ROOT names a vault — the folder a
    Council started there uses — must not become the side vault."""
    done = _run(LAUNCHER, "--where", home=home, env_extra={
        "COUNCIL_VAULT_ROOT": inherited.format(home=home)})
    assert done.returncode == 3, done.stdout
    assert "COUNCIL_VAULT_ROOT" in done.stdout


@windows_only
def test_a_side_councils_own_window_may_start_it_again(home):
    done = _run(LAUNCHER, "--where", home=home, env_extra={
        "COUNCIL_VAULT_ROOT": str(home / "Typhon-plugins-vault"),
        "COUNCIL_APP_DIR": str(home / "Typhon-plugins-app"),
        "TYPHON_PLUGINS_SIDE": "1"})
    assert done.returncode == 0, done.stdout
    other = _run(LAUNCHER, "--where", home=home, env_extra={
        "COUNCIL_VAULT_ROOT": str(home / "elsewhere")})
    assert other.returncode == 0, "an unrelated vault is not in the way"


@windows_only
def test_starting_sets_the_side_environment_and_nothing_under_council(
        home, tmp_path):
    """The launcher, copied beside a stand-in run-windows.bat that records
    the environment it was handed: the side variables are set, the
    inherited classifier-store override is gone, the side folders exist —
    and there is no ~/.council."""
    folder = tmp_path / "checkout"
    folder.mkdir()
    shutil.copy2(LAUNCHER, folder / LAUNCHER.name)
    record = tmp_path / "env.txt"
    (folder / "run-windows.bat").write_text(
        f'@echo off\r\nset > "{record}"\r\necho args=%*\r\nexit /b 7\r\n',
        encoding="ascii")
    done = _run(folder / LAUNCHER.name, "--advanced", home=home, env_extra={
        "FRAME_CLASSES_STORE": str(home / ".council" / "vault" /
                                   "classifiers"),
        "COUNCIL_PYTHON": sys.executable})
    assert done.returncode == 7, done.stdout + done.stderr
    assert "args=--advanced" in done.stdout
    env = dict(line.split("=", 1) for line in
               record.read_text(encoding="mbcs").splitlines() if "=" in line)
    upper = {k.upper(): v for k, v in env.items()}
    assert Path(upper["COUNCIL_VAULT_ROOT"]) == home / "Typhon-plugins-vault"
    assert Path(upper["COUNCIL_APP_DIR"]) == home / "Typhon-plugins-app"
    assert Path(upper["TYPHON_PLUGIN_HOME"]) == \
        home / "Typhon-plugins-app" / "typhon"
    assert upper["COUNCIL_SKIP_PATH_MIGRATION"] == "1"
    assert upper["TYPHON_PLUGINS_SIDE"] == "1"
    assert "FRAME_CLASSES_STORE" not in upper
    for made in ("Typhon-plugins-vault", "Typhon-plugins-app"):
        assert (home / made).is_dir(), made
    assert (home / "Typhon-plugins-app" / "typhon").is_dir()
    assert not (home / ".council").exists()


def test_the_launcher_and_the_test_sandbox_redirect_the_same_variables():
    """Both stand a Council beside the real one; the sandbox learned which
    variables that takes the hard way (tests/sandbox_vault.py: the
    migration moved the user's files)."""
    text = LAUNCHER.read_text(encoding="utf-8")
    for name in SIDE_VARIABLES:
        assert f'set "{name}=' in text, name
        assert name in sandbox_vault.SANDBOX_ENV, name
    assert 'set "COUNCIL_SKIP_PATH_MIGRATION=1"' in text
    assert 'set "FRAME_CLASSES_STORE="' in text


# ======================================================================
# The Council and Typhon with the launcher's environment
# ======================================================================
def test_every_folder_resolves_to_the_side_ones(tmp_path, monkeypatch):
    vault = tmp_path / "Typhon-plugins-vault"
    app = tmp_path / "Typhon-plugins-app"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    monkeypatch.setenv("COUNCIL_APP_DIR", str(app))
    monkeypatch.setenv("TYPHON_PLUGIN_HOME", str(app / "typhon"))
    monkeypatch.setenv("COUNCIL_SKIP_PATH_MIGRATION", "1")
    monkeypatch.delenv("FRAME_CLASSES_STORE", raising=False)
    for mod in ("app", "handlers"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "not_an_app.py")])
    from council_core import paths, vault_setup
    import frame_classes
    import gui_projects
    assert paths.vault_dir() == vault.resolve()
    assert paths.app_dir() == app.resolve()
    assert not vault_setup.migration_enabled()
    assert gui_projects.resolve_vault_root() == vault
    assert frame_classes.classifier_store() == vault / "classifiers"
    real = Path.home() / ".council"
    for p in (paths.vault_dir(), paths.app_dir(),
              frame_classes.classifier_store()):
        assert real not in Path(p).resolve().parents and Path(p) != real


# ======================================================================
# Copying a classifier library in, reading the original only
# ======================================================================
def _snapshot(folder):
    """Every file and folder under `folder`: its kind, bytes and times."""
    out = {}
    for root, dirs, names in os.walk(folder):
        for d in dirs:
            p = Path(root) / d
            out[p.relative_to(folder).as_posix() + "/"] = (
                "dir", p.stat().st_mtime_ns)
        for n in names:
            p = Path(root) / n
            st = p.stat()
            out[p.relative_to(folder).as_posix()] = (
                hashlib.sha256(p.read_bytes()).hexdigest(), st.st_size,
                st.st_mtime_ns)
    return out


@pytest.fixture
def library(tmp_path):
    store = tmp_path / "original" / "classifiers"
    for name in ("frames", "welds"):
        d = store / name
        (d / "versions" / "v1").mkdir(parents=True)
        (d / "classes.json").write_text('{"classes": ["good"]}',
                                        encoding="utf-8")
        (d / "about.json").write_text('{"origin": {"app": "Typhon"}}',
                                      encoding="utf-8")
        (d / "versions" / "v1" / "model.npz").write_bytes(os.urandom(4096))
        (d / ".tmp-half").mkdir()
        (d / ".tmp-half" / "x").write_bytes(b"half a version")
    (store / ".locks").mkdir()
    (store / ".locks" / "frames.lock").write_bytes(b"")
    (store / ".deleted" / "old_20260101").mkdir(parents=True)
    (store / ".deleted" / "old_20260101" / "classes.json").write_text(
        "{}", encoding="utf-8")
    return store


@pytest.fixture
def side_home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    return h


def test_the_copy_reads_the_original_only(library, tmp_path, side_home):
    before = _snapshot(library)
    dest = tmp_path / "side" / "classifiers"
    done = ccl.copy_library(library, dest)
    assert done.copied == ["frames", "welds"] and not done.skipped
    assert _snapshot(library) == before, "the original changed"
    for name in ("frames", "welds"):
        for rel in ("classes.json", "about.json", "versions/v1/model.npz"):
            assert (dest / name / rel).read_bytes() == \
                (library / name / rel).read_bytes()
        assert not (dest / name / ".tmp-half").exists()
    assert sorted(p.name for p in dest.iterdir()) == ["frames", "welds"], \
        "no locks, no deleted classifiers, no temporary folders"


def test_a_classifier_the_destination_has_is_left_as_it_is(
        library, tmp_path, side_home):
    dest = tmp_path / "side" / "classifiers"
    (dest / "frames").mkdir(parents=True)
    (dest / "frames" / "classes.json").write_text("mine", encoding="utf-8")
    done = ccl.copy_library(library, dest)
    assert done.copied == ["welds"] and list(done.skipped) == ["frames"]
    assert (dest / "frames" / "classes.json").read_text(
        encoding="utf-8") == "mine"
    again = ccl.copy_library(library, dest)
    assert again.copied == [] and sorted(again.skipped) == ["frames", "welds"]


def test_a_dry_run_copies_nothing(library, tmp_path, side_home):
    dest = tmp_path / "side" / "classifiers"
    done = ccl.copy_library(library, dest, dry_run=True)
    assert done.copied == ["frames", "welds"] and not dest.exists()
    assert done.lines()[0].startswith("would copy 2 classifier(s)")


def test_the_copy_refuses_the_councils_own_folder_and_overlaps(
        library, tmp_path, side_home):
    real = side_home / ".council" / "vault" / "classifiers"
    for dest, words in ((real, "Council's own app folder"),
                        (library, "both the original"),
                        (library / "inner", "inside the original"),
                        (library.parent, "is inside")):
        with pytest.raises(ValueError, match=words):
            ccl.copy_library(library, dest)
    with pytest.raises(ValueError, match="no classifier store"):
        ccl.copy_library(tmp_path / "missing", tmp_path / "side")
    assert not real.exists()
    assert ccl.main(["--from", str(library), "--to", str(real)]) == 2


def test_frame_classes_lists_the_copies_in_the_side_store(
        tmp_path, side_home, monkeypatch):
    """End to end with the real library code: classifiers made in one store,
    copied, are listed from the other with where they came from."""
    import frame_classes as fc

    original = tmp_path / "original_vault"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(original))
    monkeypatch.delenv(fc.STORE_ENV, raising=False)
    for mod in ("app", "handlers"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    fc.add_class("frames", "good")
    fc.add_class("frames", "bad timing")
    made = fc.list_classifiers()
    assert made["names"] == ["frames"]
    source = original / "classifiers"
    before = _snapshot(source)
    side = tmp_path / "side_vault"
    done = ccl.copy_library(source, side / "classifiers")
    assert done.copied == ["frames"]
    assert _snapshot(source) == before
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(side))
    listed = fc.list_classifiers()
    assert listed["names"] == ["frames"]
    assert Path(listed["store"]) == side / "classifiers"
    assert fc.open_classifier("frames")["classes"] == ["good", "bad timing"]
    assert listed["details"][0]["origin"] == made["details"][0]["origin"]
