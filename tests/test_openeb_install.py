"""The EVK4's OpenEB installer and Typhon finding its build (Andy, 16
November 2026: "include the install for the OpenEB stuff in there to help
users easily add the needed packages"), and today's Typhon searching every
camera kind its set-up ticked ("does the app still force either basler or
EVK support?")."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from council_core import camera_setup as cs                # noqa: E402
from council_core import openeb_env, openeb_install         # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A LOCALAPPDATA and a system drive of the test's own."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv(openeb_env.ENV, raising=False)
    monkeypatch.setattr(openeb_env, "default_root",
                        lambda: tmp_path / "none")
    for name in ("MV_HAL_PLUGIN_PATH", "HDF5_PLUGIN_PATH"):
        monkeypatch.delenv(name, raising=False)
    openeb_env._done.clear()
    yield tmp_path
    openeb_env._done.clear()


def a_build(root: Path) -> Path:
    for part in openeb_env.paths(root).values():
        part.mkdir(parents=True, exist_ok=True)
    return root


# ----------------------------------------------------------------------
# Finding the build
# ----------------------------------------------------------------------
def test_no_build_changes_nothing(home, monkeypatch):
    before = os.environ.get("PATH")
    assert openeb_env.find_root() is None
    assert openeb_env.prepare() == []
    assert os.environ.get("PATH") == before
    assert "none found" in openeb_env.describe()


def test_the_installers_pointer_file_is_followed(home, monkeypatch):
    root = a_build(home / "oe")
    pointer = openeb_env.pointer_file()
    assert pointer == home / "local" / "Council" / "openeb.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"root": str(root)}), encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PATH", "old")
    assert openeb_env.find_root() == root
    did = openeb_env.prepare()
    where = openeb_env.paths(root)
    assert os.environ["PATH"].startswith(str(where["bin"]) + os.pathsep)
    assert os.environ["MV_HAL_PLUGIN_PATH"] == str(where["plugins"])
    assert os.environ["HDF5_PLUGIN_PATH"] == str(where["hdf5"])
    assert sys.path[0] == str(where["site"])
    assert len(did) == 4
    assert openeb_env.prepare() == [], "done once per process"


def test_the_users_own_settings_win(home, monkeypatch):
    root = a_build(home / "mine")
    monkeypatch.setenv(openeb_env.ENV, str(root))
    monkeypatch.setenv("MV_HAL_PLUGIN_PATH", "C:/my/plugins")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PATH", "old")
    openeb_env.prepare()
    assert os.environ["MV_HAL_PLUGIN_PATH"] == "C:/my/plugins"


def test_a_broken_pointer_file_is_no_build(home):
    pointer = openeb_env.pointer_file()
    pointer.parent.mkdir(parents=True)
    pointer.write_text("{not json", encoding="utf-8")
    assert openeb_env.find_root() is None


def test_the_evk_backend_looks_for_the_build_before_importing(home,
                                                              monkeypatch):
    from council_core import cameras
    calls = []
    monkeypatch.setattr(openeb_env, "prepare",
                        lambda *a, **k: calls.append(1) or [])
    monkeypatch.setitem(sys.modules, "metavision_hal", None)
    with pytest.raises(cameras.CameraError):
        cameras.EvkBackend().sdk()
    assert calls, "prepare() ran before import metavision_hal"


# ----------------------------------------------------------------------
# The installer
# ----------------------------------------------------------------------
def test_the_dry_run_names_every_step_and_runs_none(home, capsys,
                                                     monkeypatch):
    ran = []
    monkeypatch.setattr(openeb_install.subprocess, "call",
                        lambda *a, **k: ran.append(a) or 0)
    root = home / "oe"
    assert openeb_install.main(["--dir", str(root), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert not ran and not root.exists()
    assert not openeb_install.pointer_file().exists()
    for words in ("git clone --branch 5.2.0",
                  "vcpkg/archive/refs/tags/2024.11.16.zip",
                  "install --triplet x64-windows --x-install-root installed",
                  "-m pip install numpy",
                  f"-DCMAKE_INSTALL_PREFIX={root / 'install'}",
                  f"-DPYTHON3_SITE_PACKAGES={root / 'install' / 'site-packages'}",
                  f"-DPython3_EXECUTABLE={sys.executable}",
                  "-DBUILD_TESTING=OFF", "--target install",
                  "wdi-simple.exe -n \"EVK\" -m \"Prophesee\" -v 0x04b4 "
                  "-p 0x00f5"):
        assert words in out, words
    assert "requirements_openeb.txt pins old versions" in out


def test_all_python_packages_is_opt_in(home, capsys):
    openeb_install.main(["--dir", str(home / "oe"), "--dry-run",
                         "--all-python-packages"])
    assert "requirements_openeb.txt" in capsys.readouterr().out.split(
        "== 4.")[1].split("== 5.")[0]


def test_a_missing_tool_stops_it_and_says_what_to_install(home, capsys,
                                                         monkeypatch):
    monkeypatch.setattr(openeb_install, "is_windows", lambda: True)
    monkeypatch.setattr(openeb_install.shutil, "which", lambda name: None)
    monkeypatch.setattr(openeb_install, "find_visual_studio", lambda: None)
    monkeypatch.setattr(openeb_install, "long_paths_enabled", lambda: True)
    assert openeb_install.main(["--dir", str(home / "oe")]) == 1
    out = capsys.readouterr().out
    assert "STOPPED: install these first" in out
    assert "Git is needed" in out and "CMake is needed" in out
    assert "Visual Studio 2022" in out
    assert not (home / "oe").exists(), "nothing made before the checks pass"


def test_it_is_for_windows(home, capsys, monkeypatch):
    monkeypatch.setattr(openeb_install, "is_windows", lambda: False)
    assert openeb_install.main(["--dir", str(home / "oe")]) == 1
    assert "this installer is for Windows" in capsys.readouterr().out


def test_steps_that_finished_are_not_repeated(home, capsys, monkeypatch):
    layout = openeb_install.Layout(home / "oe")
    (layout.src).mkdir(parents=True)
    (layout.src / "CMakeLists.txt").write_text("", encoding="utf-8")
    layout.mark("vcpkg")
    layout.mark("install")
    ran = []
    monkeypatch.setattr(openeb_install.subprocess, "call",
                        lambda argv, **k: ran.append(argv) or 0)
    openeb_install.fetch_source(layout, False)
    openeb_install.fetch_dependencies(layout, False)
    openeb_install.build(layout, False)
    assert ran == []
    out = capsys.readouterr().out
    assert out.count("already") == 3


def test_a_failed_command_says_to_run_it_again(home, monkeypatch):
    monkeypatch.setattr(openeb_install.subprocess, "call", lambda *a, **k: 2)
    with pytest.raises(openeb_install.Stop) as stop:
        openeb_install.run(["cmake", "--build", "."])
    assert "code 2" in str(stop.value) and "carries on" in str(stop.value)


def test_the_pointer_file_is_what_typhon_reads(home):
    layout = openeb_install.Layout(a_build(home / "oe"))
    written = openeb_install.write_pointer(layout, False)
    assert written == openeb_env.pointer_file()
    assert openeb_env.find_root() == home / "oe"


def test_the_driver_runs_elevated_when_wdi_simple_is_there(home,
                                                          monkeypatch):
    layout = openeb_install.Layout(home / "oe")
    layout.root.mkdir()
    wdi = layout.root / "wdi-simple.exe"
    wdi.write_bytes(b"")
    ran = []
    monkeypatch.setattr(openeb_install.subprocess, "call",
                        lambda argv, **k: ran.append(argv) or 0)
    openeb_install.driver(layout, None, False)
    assert ran and ran[0][0] == "powershell"
    script = ran[0][-1]
    assert "-Verb RunAs" in script
    assert script.count("0x04b4") == 3 and "0x00f5" in script


def test_install_steps_match_the_set_up_guide():
    """The wizard's EVK4 steps and the installer give the same driver
    commands, and the guide offers the installer for this Python."""
    assert [" ".join(f'"{a}"' if a in ("EVK", "Prophesee") else a
                     for a in args) for args in openeb_install.WDI_ARGS] == \
        [c.replace("wdi-simple.exe ", "") for c in cs.WDI_COMMANDS]
    steps = cs.guide("prophesee", python="C:/py/python.exe").steps
    assert steps[1].command == \
        '"C:/py/python.exe" -m council_core.openeb_install'


# ----------------------------------------------------------------------
# Today's Typhon: every ticked kind
# ----------------------------------------------------------------------
def test_built_in_families_are_basler_and_the_evk4():
    families = cs.builtin_families()
    assert [(f.key, f.label) for f in families] == [
        ("basler", "Basler"), ("prophesee", "Prophesee EVK4")]
    assert families[0].scan is not None and families[1].scan is None
    assert families[1].guide("C:/py.exe").steps[1].command.startswith(
        '"C:/py.exe"')


def test_builtin_choices_reads_both_file_shapes(tmp_path):
    path = cs.setup_path(tmp_path)
    assert cs.builtin_choices(path) is None
    cs.save_choice(path, "prophesee")
    assert cs.builtin_choices(path) == ["prophesee"]
    cs.save_families(path, ["prophesee", "acme.thermal", "basler"])
    assert cs.builtin_choices(path) == ["prophesee", "basler"]
    cs.save_families(path, ["acme.thermal"])
    assert cs.builtin_choices(path) is None


def test_a_basler_and_evk4_set_up_searches_both(tmp_path, monkeypatch):
    import frame_camera
    from council_core import cameras

    monkeypatch.setattr(frame_camera, "_plugin_host", lambda: None)
    path = cs.setup_path(tmp_path)
    cs.save_families(path, ["basler", "prophesee"])
    monkeypatch.setattr(frame_camera._LIVE, "setup_path", path)
    asked = []

    def discover(backends):
        asked.append([type(b).__name__ for b in backends or []])
        return cameras.Discovery([], [])

    monkeypatch.setattr(cameras, "discover", discover)
    frame_camera.list_cameras()
    assert asked[0] == ["BaslerBackend", "EvkBackend", "SyntheticBackend"]
    assert "Basler and Prophesee" in " ".join(frame_camera._scan_context(
        None))
