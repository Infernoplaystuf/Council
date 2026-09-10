"""
`requires` — the packages a generated app imports, declared, checked and gated.

A camera app imports its vendor SDK, which the policy gate refused
unconditionally: 'pypylon' is not on the allowlist, in either mode. Now a
project declares what it needs, and each declared package:

  * joins THAT project's allowlist — an undeclared import is still refused,
    and a denied module (subprocess, pickle, ...) can never be declared in
  * is checked in the Python that will run the app before Run launches it
  * is checked again at startup by the generated main.py, so a hand-launch
    under the wrong Python fails with a list instead of a blank panel

And the gate is now ENFORCED at Run. It used to run only at Generate, whose
message said "the app will not run it" while Run launched it anyway.

Run:  python -m pytest tests/test_gui_requires.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_emit as ge            # noqa: E402
import gui_layout as gl          # noqa: E402
import gui_policy as pol         # noqa: E402
import gui_shapes as gs          # noqa: E402
import gui_spec as gsp           # noqa: E402

CAMERA = "from pypylon import pylon\ncam = pylon.InstantCamera()\n"


# ============================================================
# The allowlist
# ============================================================

@pytest.mark.parametrize("mode", pol.MODES)
def test_an_undeclared_vendor_import_is_still_refused(mode):
    ok, errs = pol.validate(CAMERA, mode)
    assert not ok
    assert any("add it to the project's requires" in e for e in errs)


@pytest.mark.parametrize("mode", pol.MODES)
def test_a_declared_vendor_import_is_allowed(mode):
    ok, errs = pol.validate(CAMERA, mode, extra_modules=["pypylon"])
    assert ok, errs


def test_declaring_one_package_does_not_open_the_door_to_others():
    code = CAMERA + "import metavision_core\n"
    ok, errs = pol.validate(code, "linked", extra_modules=["pypylon"])
    assert not ok and any("metavision_core" in e for e in errs)


@pytest.mark.parametrize("name", ["subprocess", "socket", "pickle",
                                  "importlib", "multiprocessing"])
def test_a_denied_module_cannot_be_declared_in(name):
    """Declaring is how a project widens its allowlist, so the declaration is
    gated too — otherwise requires would be a one-line bypass of the gate."""
    assert pol.check_requires([name])
    ok, _ = pol.validate(f"import {name}\n", "linked", extra_modules=[name])
    assert not ok


def test_council_engine_cannot_be_declared_in():
    assert pol.check_requires(["council_engine"])
    ok, _ = pol.validate("import council_engine\n", "linked",
                         extra_modules=["council_engine"])
    assert not ok


@pytest.mark.parametrize("bad", ["Pillow-SIMD", "my pkg", "1abc", "a..b"])
def test_a_declaration_must_be_an_import_name(bad):
    assert pol.check_requires([bad])


def test_dotted_declarations_are_fine():
    assert pol.check_requires(["metavision_core.event_io", "PIL"]) == []


def test_a_bad_declaration_fails_the_whole_project(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    ok, errs = pol.validate_dir(tmp_path, "linked", ["subprocess"])
    assert not ok and any("never permitted" in e for e in errs)


# ============================================================
# The wireframe
# ============================================================

def _project(requires=()):
    s = gs.new_shape("label", 0, 0)
    s.id, s.label = "l", "Camera"
    return gs.Project(project="cam", shapes=[s], requires=list(requires))


def test_requires_round_trips_through_a_gspec(tmp_path):
    p = tmp_path / "cam.gspec"
    gs.save_gspec(p, _project(["pypylon", "numpy"]))
    assert gs.load_gspec(p).requires == ["pypylon", "numpy"]


def test_only_a_project_that_declares_something_is_stamped_v3(tmp_path):
    """Content stamping: every existing wireframe keeps its old version and
    stays openable by older builds; only a declaring one becomes v3."""
    a, b = tmp_path / "a.gspec", tmp_path / "b.gspec"
    gs.save_gspec(a, _project([]))
    gs.save_gspec(b, _project(["pypylon"]))
    assert json.loads(a.read_text())["gspec_version"] < 3
    assert "requires" not in json.loads(a.read_text())
    assert json.loads(b.read_text())["gspec_version"] == 3


def test_validate_rejects_a_bad_declaration():
    proj = _project(["socket"])
    spec = gsp.build(proj.shapes, gl.infer(proj.shapes, 400, 300),
                     project="cam", requires=proj.requires)
    ok, errs = gsp.validate(spec)
    assert not ok and any("never permitted" in e for e in errs)


# ============================================================
# The generated startup check
# ============================================================

def _emit(tmp_path, requires):
    proj = _project(requires)
    spec = gsp.build(proj.shapes, gl.infer(proj.shapes, 400, 300),
                     project="cam", requires=proj.requires)
    ge.emit(spec, tmp_path)
    return tmp_path


def test_main_py_checks_every_declared_package(tmp_path):
    pdir = _emit(tmp_path, ["numpy", "definitely_missing_pkg_xyz"])
    src = (pdir / "main.py").read_text(encoding="utf-8")
    assert "import numpy" in src and "import definitely_missing_pkg_xyz" in src
    compile(src, "main.py", "exec")
    ok, errs = pol.validate(src, "linked", extra_modules=["numpy",
                            "definitely_missing_pkg_xyz"])
    assert ok, errs


def test_main_py_with_nothing_declared_is_unchanged(tmp_path):
    pdir = _emit(tmp_path, [])
    assert "_MISSING" not in (pdir / "main.py").read_text(encoding="utf-8")


def test_the_wrong_python_fails_at_startup_with_a_list(tmp_path):
    """Launched under a Python that lacks a declared package, the app says
    so and exits — before any widget exists."""
    pdir = _emit(tmp_path, ["json", "definitely_missing_pkg_xyz"])
    r = subprocess.run([sys.executable, "main.py"], cwd=str(pdir),
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, COUNCIL_PREVIEW_CONTROL="stdin"))
    assert r.returncode == 3
    assert "definitely_missing_pkg_xyz" in r.stderr
    assert "Run with" in r.stderr
    assert "json" not in r.stderr.split("missing:")[-1].split("Choose")[0]


# ============================================================
# The shipped example
# ============================================================

def test_barbie_v2_declares_what_it_actually_imports(tmp_path):
    """Its live view needs Pillow and its bad-timing scan needs numpy.
    Undeclared, a missing one meant a blank panel and a false zero."""
    import run_example_gui as rex
    proj = gs.load_gspec(Path(__file__).resolve().parent.parent
                         / "examples" / "gui" / "barbie_capture_v2.gspec")
    assert set(proj.requires) >= {"numpy", "PIL"}
    pdir = rex.build("barbie_capture_v2", project="b", vault_dir=tmp_path)
    ok, errs = pol.validate_dir(pdir, "linked", proj.requires)
    assert ok, errs


def test_the_model_is_taught_to_declare_requires():
    import gui_examples as gx
    text = gx.for_prompt()
    assert '"requires": [' in text
    assert "IMPORT name" in text
