"""
The Tk guard itself: a Tk window is refused in this process and in a python
child, and importing tkinter is not.

Every check first asks whether the guard is in place and stops there if not,
so a broken guard fails these tests WITHOUT a Tk window being made to find
out.

Run:  python -m pytest tests/test_no_tk_guard.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import no_tk_guard as guard  # noqa: E402

tkinter = pytest.importorskip("tkinter")


def test_the_guard_is_in_force_for_the_session():
    assert guard.installed()
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(guard.HOOK_DIR)


def test_a_tk_root_is_refused():
    assert getattr(tkinter, "_council_no_tk", False), "the guard is not in place"
    with pytest.raises(guard.TkWindowRefused, match="Tk GUIs are deprecated"):
        tkinter.Tk()


def test_a_toplevel_is_refused():
    assert getattr(tkinter, "_council_no_tk", False), "the guard is not in place"
    with pytest.raises(guard.TkWindowRefused,
                       match=r"tests must not open a Tk window"):
        tkinter.Toplevel()


def test_a_subclass_of_tk_is_refused_too():
    """The Tk console IS a tk.Tk subclass."""
    assert getattr(tkinter, "_council_no_tk", False), "the guard is not in place"

    class Console(tkinter.Tk):
        def __init__(self):
            super().__init__()

    with pytest.raises(guard.TkWindowRefused):
        Console()


def test_the_message_names_where_to_read_why():
    assert guard.MESSAGE.startswith(
        "Tk GUIs are deprecated - tests must not open a Tk window")
    assert "docs/qt_migration" in guard.MESSAGE
    assert "tests/README.md" in guard.MESSAGE
    root = Path(__file__).resolve().parent.parent
    assert (root / "tests" / "README.md").is_file()
    assert (root / "docs" / "qt_migration" / "measurements.md").is_file()


CHILD = textwrap.dedent('''
    import sys
    import tkinter
    if not getattr(tkinter, "_council_no_tk", False):
        print("UNGUARDED")
        sys.exit(2)                  # stop BEFORE a window could be made
    if sys.argv[1] == "import-only":
        print("IMPORTED")
        sys.exit(0)
    tkinter.Tk()
''')


def _child(tmp_path, mode):
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    return subprocess.run([sys.executable, str(script), mode],
                          capture_output=True, text=True, timeout=60,
                          cwd=str(tmp_path))


def test_a_python_child_is_refused_too(tmp_path):
    """A generated app or a driver runs as a child; the guard reaches it
    through PYTHONPATH."""
    r = _child(tmp_path, "make-a-root")
    assert "UNGUARDED" not in r.stdout, "the child has no guard"
    assert r.returncode != 0
    assert "TkWindowRefused" in r.stderr
    assert "Tk GUIs are deprecated - tests must not open a Tk window" in r.stderr


def test_a_child_may_still_import_tkinter(tmp_path):
    r = _child(tmp_path, "import-only")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "IMPORTED" in r.stdout


OTHER_SITE = textwrap.dedent('''
    import importlib.machinery, os, sys
    os.environ["OTHER_SITE_RAN"] = "1"

    class OtherHook:                  # another tool wrapping tkinter's import
        def find_spec(self, name, path=None, target=None):
            if name != "tkinter":
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            run = spec.loader.exec_module
            def wrapped(module):
                run(module)
                module._other_hook = True
            spec.loader.exec_module = wrapped
            return spec

    sys.meta_path.insert(0, OtherHook())
''')


def test_the_child_hook_composes_with_a_sitecustomize_it_shadows(tmp_path):
    """Being first on PYTHONPATH hides any other sitecustomize (coverage's,
    say), so the hook runs the next one — and that one's own tkinter import
    hook still runs, while the refusal is applied last and stays."""
    other = tmp_path / "other_site"
    other.mkdir()
    (other / "sitecustomize.py").write_text(OTHER_SITE, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(guard.HOOK_DIR), str(other)])
    r = subprocess.run(
        [sys.executable, "-c",
         "import os, tkinter; "
         "print(os.environ.get('OTHER_SITE_RAN'), "
         "getattr(tkinter, '_other_hook', False), "
         "getattr(tkinter, '_council_no_tk', False))"],
        capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["1", "True", "True"]


def test_a_refusal_is_recorded_when_asked(tmp_path, monkeypatch):
    """COUNCIL_NO_TK_LOG is how a whole run shows the guard never fired."""
    assert getattr(tkinter, "_council_no_tk", False), "the guard is not in place"
    log = tmp_path / "no_tk.log"
    monkeypatch.setenv("COUNCIL_NO_TK_LOG", str(log))
    with pytest.raises(guard.TkWindowRefused):
        tkinter.Tk()
    pid, test, what = log.read_text(encoding="utf-8").strip().split("\t")
    assert pid == str(os.getpid())
    assert "test_a_refusal_is_recorded_when_asked" in test
    assert what == "tkinter.Tk()"
