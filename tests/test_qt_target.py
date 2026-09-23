"""
Tests for generating and running a project on the Qt target.

The Qt emitter has existed for a while and was reachable only from tests: every
production entry point called gui_emit.emit() without a target, so every project
was Tk. These cover the plumbing that makes `--target qt` real, and the two
refusals that keep it from breaking Tk projects.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gui_projects as gpj
import python_envs as pe
import run_example_gui as rex


# ======================================================================
# Which toolkit must an interpreter have?
# ======================================================================
def test_a_qt_app_does_not_need_tkinter():
    """The camera case, and the reason this function exists.

    A vendor-SDK environment — the one with pypylon or the Metavision
    bindings, which is the only interpreter that can reach the camera — very
    often has no tkinter. Requiring it would refuse to run a PySide6 app
    under exactly the Python that works.
    """
    vendor = {"tkinter": "", "tkinter_error": "ModuleNotFoundError: tkinter",
              "pyside6": "6.10.2"}
    assert pe.toolkit_missing(vendor, "qt") == ""


def test_a_tk_app_still_needs_tkinter():
    vendor = {"tkinter": "", "tkinter_error": "ModuleNotFoundError: tkinter",
              "pyside6": "6.10.2"}
    assert "no tkinter" in pe.toolkit_missing(vendor, "tk")


def test_a_qt_app_needs_pyside6():
    tk_only = {"tkinter": "8.6", "pyside6": "", "pyside6_error": "nope"}
    assert "no PySide6" in pe.toolkit_missing(tk_only, "qt")


def test_a_tk_app_does_not_need_pyside6():
    tk_only = {"tkinter": "8.6", "pyside6": "", "pyside6_error": "nope"}
    assert pe.toolkit_missing(tk_only, "tk") == ""


def test_the_default_toolkit_is_tk():
    """Nothing existing may change behaviour because a Qt target appeared."""
    tk_only = {"tkinter": "8.6", "pyside6": ""}
    assert pe.toolkit_missing(tk_only) == ""
    assert pe.wants_qt("") is False


# ======================================================================
# Recording the toolkit
# ======================================================================
def test_a_project_records_the_toolkit_it_was_created_for(tmp_path):
    gpj.create("qt_one", vault_dir=tmp_path, toolkit="qt")
    pdir = gpj.project_path("qt_one", tmp_path)
    assert gpj.load_manifest(pdir).toolkit == "qt"


def test_a_project_defaults_to_tk(tmp_path):
    gpj.create("tk_one", vault_dir=tmp_path)
    pdir = gpj.project_path("tk_one", tmp_path)
    assert gpj.load_manifest(pdir).toolkit == "tk"


def test_an_unknown_toolkit_is_refused(tmp_path):
    with pytest.raises(gpj.ProjectError, match="unknown toolkit"):
        gpj.create("bad", vault_dir=tmp_path, toolkit="swing")


def test_the_code_on_disk_outranks_the_manifest(tmp_path):
    """app.py is the fact; the manifest is only the intent.

    Editing a manifest must not change what a built project IS.
    """
    gpj.create("mixed", vault_dir=tmp_path, toolkit="qt")
    pdir = gpj.project_path("mixed", tmp_path)
    (pdir / "app.py").write_text("import tkinter\n", encoding="utf-8")
    assert gpj.toolkit_for(pdir) == "tk"


def test_a_project_with_no_code_yet_uses_the_manifest(tmp_path):
    gpj.create("fresh", vault_dir=tmp_path, toolkit="qt")
    pdir = gpj.project_path("fresh", tmp_path)
    assert gpj.toolkit_of(pdir) == ""
    assert gpj.toolkit_for(pdir) == "qt"


# ======================================================================
# Generating
# ======================================================================
def test_building_an_example_for_qt_writes_a_pyside6_app(tmp_path):
    pdir = rex.build("barbie_capture_v3", project="b_qt", vault_dir=tmp_path,
                     target="qt")
    assert "PySide6" in (pdir / "app.py").read_text(encoding="utf-8")
    assert gpj.toolkit_for(pdir) == "qt"


def test_building_records_the_toolkit_in_the_manifest(tmp_path):
    """Not just in the code. toolkit_for reads app.py first, so a manifest
    left saying "tk" beside a PySide6 app.py hides until something asks the
    manifest — which Run does, on a project whose app.py is not built yet."""
    pdir = rex.build("barbie_capture_v3", project="b_man", vault_dir=tmp_path,
                     target="qt")
    assert gpj.load_manifest(pdir).toolkit == "qt"


def test_building_an_example_still_defaults_to_tk(tmp_path):
    """The whole point of a default: no existing project changes."""
    pdir = rex.build("barbie_capture_v3", project="b_tk", vault_dir=tmp_path)
    source = (pdir / "app.py").read_text(encoding="utf-8")
    assert "tkinter" in source and "PySide6" not in source
    assert gpj.toolkit_for(pdir) == "tk"


def test_regenerating_cannot_switch_a_built_project_to_another_toolkit(tmp_path):
    """app.py is created once and never rewritten.

    Emitting a Qt ui/ beside a Tk app.py produces a project that cannot
    start, and the failure looks like a bug in the wireframe rather than in
    the request that caused it. So it is refused, with a sentence.
    """
    from council_core import designer_project as dp

    pdir = rex.build("barbie_capture_v3", project="flip", vault_dir=tmp_path)
    manifest = gpj.load_manifest(pdir)
    manifest.toolkit = "qt"                 # someone edits the manifest
    gpj.save_manifest(pdir, manifest)

    project = gpj.open_project("flip", vault_dir=tmp_path)
    out = dp.generate("flip", project.shapes, pdir, tmp_path)
    assert out.blocked, "a toolkit switch was emitted instead of refused"
    assert any("app.py is written in tk" in line for line in out.lines), out.lines


# ======================================================================
# Running
# ======================================================================
def test_a_qt_project_passes_its_own_policy_gate(tmp_path):
    """Before the toolkit was threaded through, this was refused.

    preflight called validate_dir with no toolkit, which defaults to "tk",
    and PySide6 is admitted by the toolkit argument alone — never by
    `requires`. So every correctly generated Qt app failed its own gate.
    """
    pdir = rex.build("barbie_capture_v3", project="gate_ok", vault_dir=tmp_path,
                     target="qt")
    pf = pe.preflight(pdir, "", "linked", rex._requires_of(pdir),
                      toolkit=gpj.toolkit_for(pdir))
    assert pf.ok, pf.lines


def test_the_same_qt_project_is_refused_when_checked_as_tk(tmp_path):
    """The counterfactual, so the fix cannot be quietly reverted."""
    pdir = rex.build("barbie_capture_v3", project="gate_bad",
                     vault_dir=tmp_path, target="qt")
    pf = pe.preflight(pdir, "", "linked", rex._requires_of(pdir),
                      toolkit="tk")
    assert not pf.ok
    assert any("PySide6" in line for line in pf.lines)
