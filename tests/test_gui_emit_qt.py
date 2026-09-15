"""
The PySide6 emit target.

NONE OF THIS NEEDS PySide6 INSTALLED. The emitter writes text and the policy
gate reads an AST, so the whole contract below is checkable on the project's
floor interpreter — which is the point: `council` (Python 3.11) has no PySide6,
and a test suite that skipped itself there would go green while the Qt target
rotted. The tests that genuinely need a running Qt (a real window, a real
signal) belong in a separate, explicitly-skipped file; these do not.

WHAT IS BEING PINNED
  * the Tk target did not move — same bytes, same files, for every example
  * the Qt target emits a project that COMPILES, contains no tkinter, and keeps
    the marker gui_runner greps for to decide whether Stop can be clean
  * the two targets agree about everything toolkit-neutral: the port names, the
    handler stubs, the sequence links
  * the hardened gate lets correct Qt code through and refuses the escapes,
    including the two spellings the root-name allowlist used to miss
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_emit as ge            # noqa: E402
import gui_emit_qt as geq        # noqa: E402
import gui_layout as gl          # noqa: E402
import gui_policy as gp          # noqa: E402
import gui_spec as gsp           # noqa: E402
from gui_shapes import load_gspec  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted(p.stem for p in (ROOT / "examples" / "gui").glob("*.gspec"))


def build(name):
    proj = load_gspec(ROOT / "examples" / "gui" / f"{name}.gspec")
    tree = gl.infer(proj.shapes, proj.canvas.w, proj.canvas.h)
    return gsp.build(
        proj.shapes, tree, {}, project=name, mode=proj.mode,
        title=proj.window.title, min_w=proj.window.min_w,
        min_h=proj.window.min_h, root_bg=proj.window.bg,
        root_fg=proj.window.fg, root_font=proj.window.font,
        requires=getattr(proj, "requires", []) or [])


def emit(name, tmp_path, target):
    spec = build(name)
    out = tmp_path / target / name
    ge.emit(spec, out, target=target)
    return spec, out


def sources(pdir):
    return {p.relative_to(pdir).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(pdir.rglob("*.py"))}


# ---------------------------------------------------------------- the Tk half

@pytest.mark.parametrize("name", EXAMPLES)
def test_the_default_target_is_still_tk_byte_for_byte(name, tmp_path):
    """Adding a second target must not move the first one by a single byte.

    An existing project regenerates through this path on every Generate; a
    change here would show up as a diff in a user's working tree that nobody
    asked for."""
    _, explicit = emit(name, tmp_path / "a", "tk")
    spec = build(name)
    default = tmp_path / "b" / name
    ge.emit(spec, default)                      # no target= at all
    assert sources(explicit) == sources(default)


def test_tk_output_still_imports_tkinter_and_says_so():
    """The guard against a refactor quietly swapping the default."""
    spec = build("image_viewer")
    assert "import tkinter as tk" in ge.emit_main_ui(spec)
    assert ge.DEFAULT_TARGET == "tk"


def test_an_unknown_target_is_refused_by_name():
    with pytest.raises(ValueError) as exc:
        ge._backend("gtk")
    assert "gtk" in str(exc.value)


# ---------------------------------------------------------------- the Qt half

@pytest.mark.parametrize("name", EXAMPLES)
def test_the_qt_target_emits_a_project_that_compiles(name, tmp_path):
    _, out = emit(name, tmp_path, "qt")
    for rel, src in sources(out).items():
        compile(src, rel, "exec")               # raises SyntaxError if not


@pytest.mark.parametrize("name", EXAMPLES)
def test_a_qt_project_contains_no_tkinter_anywhere(name, tmp_path):
    """Including main.py's missing-package report, which in the Tk target opens
    a tkinter messagebox — the one case that would fire for a Qt app is PySide6
    itself being absent, so reaching for tkinter there is exactly backwards."""
    _, out = emit(name, tmp_path, "qt")
    for rel, src in sources(out).items():
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] == "tkinter"
                               for a in node.names), rel
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "tkinter", rel


@pytest.mark.parametrize("name", EXAMPLES)
def test_a_qt_project_keeps_the_marker_stop_is_detected_by(name, tmp_path):
    """gui_runner decides whether Stop can be clean by grepping ui/main_ui.py
    for this literal. Rename it and every Qt preview goes back to being killed
    with TerminateProcess — no on_close, no camera released."""
    _, out = emit(name, tmp_path, "qt")
    src = (out / "ui" / "main_ui.py").read_text(encoding="utf-8")
    assert "_watch_for_stop(self)" in src


@pytest.mark.parametrize("name", EXAMPLES)
def test_qt_code_avoids_the_load_spelling_the_gate_denies(name, tmp_path):
    """QPixmap.load(path) is the natural Qt spelling and the project's own gate
    refuses it — `.load` is denied unless the receiver is in
    SAFE_LOAD_RECEIVERS, and a local variable can never be in that set. The
    constructor form does the same job."""
    _, out = emit(name, tmp_path, "qt")
    for rel, src in sources(out).items():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Attribute):
                assert node.attr != "load", f"{rel}: line {node.lineno}"


@pytest.mark.parametrize("name", EXAMPLES)
def test_both_targets_agree_about_the_toolkit_neutral_half(name, tmp_path):
    """handlers.py is append-only and toolkit-neutral, so the SAME wireframe
    must produce the same handlers whichever target wrote them — otherwise a
    project could not change target without its behaviour file drifting."""
    _, tk_out = emit(name, tmp_path / "tk_", "tk")
    _, qt_out = emit(name, tmp_path / "qt_", "qt")
    assert (tk_out / "handlers.py").read_text(encoding="utf-8") == \
           (qt_out / "handlers.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", EXAMPLES)
def test_both_targets_declare_the_same_ports(name, tmp_path):
    """The ports IR is toolkit-neutral; only the binding underneath differs.
    app.py and handlers.py are written against these names and are never
    rewritten, so they must not depend on the target."""
    _, tk_out = emit(name, tmp_path / "tk_", "tk")
    _, qt_out = emit(name, tmp_path / "qt_", "qt")

    def names(pdir):
        src = (pdir / "ui" / "ports.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ClassDef) and node.name == "Ports":
                for item in node.body:
                    if isinstance(item, ast.Assign) and any(
                            getattr(t, "id", "") == "_names"
                            for t in item.targets):
                        return ast.literal_eval(item.value)
        return ()

    assert names(tk_out) == names(qt_out)


def test_the_qt_app_py_builds_a_qapplication_and_runs_it(tmp_path):
    """app.py is created ONCE and never rewritten, so the toolkit is baked in
    here — which is why a project's target cannot be flipped in place."""
    _, out = emit("image_viewer", tmp_path, "qt")
    src = (out / "app.py").read_text(encoding="utf-8")
    assert "QApplication" in src and "app.exec()" in src
    assert "HandlerMixin, MainUi" in src        # handlers still win over stubs


def test_a_sequence_link_survives_the_port(tmp_path):
    """The frame browser is the piece with the most behaviour in it — folder to
    ordered files to one decoded image — and it is wired from `drives`."""
    _, out = emit("image_viewer", tmp_path, "qt")
    src = (out / "ui" / "ports.py").read_text(encoding="utf-8")
    assert "_FrameBrowser(" in src
    assert "SCAN_MS = 150" in src and "SHOW_MS = 30" in src


def test_the_composite_class_names_match_the_tk_targets():
    """They are the region ids in ui/widgets.py. A different name would make
    emit() report every preserved region as orphaned and move the user's code
    into .backups/."""
    assert geq.COMPOSITE_KINDS == ge.COMPOSITE_KINDS


# ------------------------------------------------------------------- the gate

@pytest.mark.parametrize("name", EXAMPLES)
def test_a_generated_qt_project_passes_its_own_gate(name, tmp_path):
    spec, out = emit(name, tmp_path, "qt")
    ok, errs = gp.validate_dir(out, spec.mode, spec.requires, toolkit="qt")
    assert ok, errs


@pytest.mark.parametrize("name", EXAMPLES)
def test_the_same_project_is_refused_on_a_tk_target(name, tmp_path):
    """PySide6 is admitted by the TARGET, not by a declaration — so a Tk
    project that somehow imported it is still a defect the gate catches."""
    spec, out = emit(name, tmp_path, "qt")
    ok, _ = gp.validate_dir(out, spec.mode, spec.requires, toolkit="tk")
    assert not ok


ESCAPES = [
    ("QProcess by import",
     "from PySide6.QtCore import QProcess\nQProcess().start('cmd')\n"),
    ("QProcess by attribute",
     "from PySide6 import QtCore\nQtCore.QProcess()\n"),
    ("QtNetwork as a submodule path",
     "from PySide6.QtNetwork import QNetworkAccessManager\n"),
    ("QtNetwork as an imported name",
     "from PySide6 import QtNetwork\n"),
    ("QtNetwork by plain import",
     "import PySide6.QtNetwork\n"),
    ("QDesktopServices opening a URL",
     "from PySide6.QtGui import QDesktopServices\n"),
    ("QSettings writing the registry",
     "from PySide6.QtCore import QSettings\n"),
    ("QPluginLoader loading native code",
     "from PySide6.QtCore import QPluginLoader\n"),
    ("QtQml evaluating JavaScript",
     "from PySide6.QtQml import QQmlEngine\n"),
    ("QtSql reaching a database",
     "from PySide6.QtSql import QSqlDatabase\n"),
    ("QDir.removeRecursively, Qt's rmtree",
     "from PySide6.QtCore import QDir\nQDir('x').removeRecursively()\n"),
    ("a denied name through getattr",
     "from PySide6 import QtCore\nf = getattr(QtCore, 'QProcess')\n"),
    ("a second Qt binding",
     "from PyQt6.QtWidgets import QWidget\n"),
    ("a star import",
     "from PySide6.QtWidgets import *\n"),
]


@pytest.mark.parametrize("what,src", ESCAPES, ids=[e[0] for e in ESCAPES])
def test_the_gate_refuses_every_qt_escape(what, src):
    """Before this rule existed, ONE declaration (`requires: PySide6`) admitted
    all of these, because the allowlist matched the ROOT module name and every
    one of them lives under that same root."""
    ok, errs = gp.validate(src, "linked", [], toolkit="qt")
    assert not ok, f"{what} was allowed"
    assert errs


LEGIT = [
    "from PySide6.QtWidgets import QLabel, QGridLayout\nw = QLabel()\n",
    "from PySide6.QtCore import Qt, QTimer\nQTimer.singleShot(10, lambda: None)\n",
    "from PySide6.QtGui import QPixmap, QImage\np = QPixmap('a.png')\n",
    "from PySide6.QtWidgets import QFileDialog\n"
    "p, _ = QFileDialog.getOpenFileName(None, 'x')\n",
    "from PySide6.QtWidgets import QApplication\n"
    "import sys\nsys.exit(QApplication([]).exec())\n",
]


@pytest.mark.parametrize("src", LEGIT, ids=range(len(LEGIT)))
def test_the_gate_allows_ordinary_qt_code(src):
    """The other half of a gate that is worth having: `exec` is in
    DENIED_BUILTINS but NOT in DENIED_ATTRS, and it must stay that way — every
    Qt app ends in app.exec(), and every modal dialog is QDialog.exec()."""
    ok, errs = gp.validate(src, "linked", [], toolkit="qt")
    assert ok, errs


def test_declaring_the_toolkit_in_requires_is_refused():
    """`requires` is for what the APP needs. Declaring PySide6 there was how
    the whole binding got admitted at once."""
    assert gp.check_requires(["PySide6"], "linked")
    assert gp.check_requires(["PyQt6"], "linked")
    assert gp.check_requires(["numpy", "PIL"], "linked") == []


# --------------------------------------------------- the toolkit is write-once

@pytest.mark.parametrize("first,second", [("tk", "qt"), ("qt", "tk")])
def test_a_project_cannot_be_regenerated_into_the_other_toolkit(
        first, second, tmp_path):
    """app.py runs the event loop and is created ONCE. Regenerating ui/ for the
    other toolkit would leave a project whose app.py cannot drive its own UI —
    it would die on the first import, naming a module the user never mentioned.
    The refusal says the real thing instead."""
    spec = build("image_viewer")
    pdir = tmp_path / "p"
    ge.emit(spec, pdir, target=first)
    with pytest.raises(ValueError) as exc:
        ge.emit(spec, pdir, target=second)
    assert "app.py" in str(exc.value)
    ge.emit(spec, pdir, target=first)           # its own toolkit still fine


@pytest.mark.parametrize("target", ["tk", "qt"])
def test_the_toolkit_is_readable_back_out_of_the_code(target, tmp_path):
    """The manifest records intent; app.py is the fact. They can only disagree
    if someone edits the manifest, which is exactly when the truth matters."""
    import gui_projects as gpj
    spec = build("image_viewer")
    pdir = tmp_path / "p"
    assert gpj.toolkit_of(pdir) == ""           # nothing written yet
    ge.emit(spec, pdir, target=target)
    assert gpj.toolkit_of(pdir) == target


def test_an_old_manifest_with_no_toolkit_key_reads_as_tk(tmp_path):
    """Every project written before the Qt target is a Tk project, so the
    default is the answer rather than a guess."""
    import json

    import gui_projects as gpj
    pdir = tmp_path / "p"
    pdir.mkdir()
    (pdir / "manifest.json").write_text(
        json.dumps({"name": "old", "mode": "linked"}), encoding="utf-8")
    assert gpj.load_manifest(pdir).toolkit == "tk"
