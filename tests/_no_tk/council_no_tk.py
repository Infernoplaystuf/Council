"""
council_no_tk — every Tk root and Toplevel is refused. The Tk GUIs are
deprecated, and no test may open a Tk window.

tests/no_tk_guard.py loads this twice over:

  * in the pytest process, where install() patches tkinter at once if it is
    loaded, or the moment something imports it;
  * in every python CHILD a test starts, through sitecustomize.py beside this
    file — no_tk_guard puts this folder first on PYTHONPATH, and a child
    inherits it. Generated apps, gui_runner previews, drivers and probes all
    run that way.

What is refused is tkinter.Tk() — and with it Tcl(), which is Tk(useTk=0),
and the default root a widget or variable made with no master would create —
and tkinter.Toplevel(). Importing tkinter, or a module built on it, and
reading Tk code as text are untouched: plenty of tests still do that.

Standard library only, and syntax an old Python still parses: a child may be
another interpreter (a camera SDK's conda env, a 3.9 probe), and a
sitecustomize that fails would break every child, not just Tk ones.
"""
import sys

#: What a refused Tk root or Toplevel says.
MESSAGE = ("Tk GUIs are deprecated - tests must not open a Tk window "
           "(see docs/qt_migration/measurements.md section 4 and "
           "tests/README.md)")


class TkWindowRefused(RuntimeError):
    """A test (or a program a test started) tried to open a Tk window."""


def _refuse(what):
    raise TkWindowRefused("%s [%s was called]" % (MESSAGE, what))


def patch(tkinter_module):
    """Make tkinter.Tk() and tkinter.Toplevel() raise. Idempotent.

    __init__ on the CLASSES, so a subclass (the Tk console is one) and a
    name bound before this ran (`from tkinter import Tk`) are refused too.
    The original is never called: the refusal comes before Tcl exists, so
    there is no interpreter left for the garbage collector to free on the
    wrong thread."""
    tk = tkinter_module
    if getattr(tk, "_council_no_tk", False):
        return

    def tk_init(self, *args, **kwargs):
        _refuse("tkinter.Tk()")

    def toplevel_init(self, *args, **kwargs):
        _refuse("tkinter.Toplevel()")

    tk.Tk.__init__ = tk_init
    tk.Toplevel.__init__ = toplevel_init
    tk._council_no_tk = True


class _Finder(object):
    """Patches tkinter as it is imported. Finds nothing else."""

    def find_spec(self, name, path=None, target=None):
        if name != "tkinter":
            return None
        import importlib.machinery
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module

        def exec_and_patch(module):
            exec_module(module)
            patch(module)

        spec.loader.exec_module = exec_and_patch
        return spec


def install():
    """Refuse Tk windows in THIS process, from now on."""
    loaded = sys.modules.get("tkinter")
    if loaded is not None:
        patch(loaded)
        return
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())


def installed():
    """Whether a Tk window would be refused here right now."""
    loaded = sys.modules.get("tkinter")
    if loaded is not None:
        return bool(getattr(loaded, "_council_no_tk", False))
    return any(isinstance(f, _Finder) for f in sys.meta_path)


def run_next_sitecustomize(here):
    """Run the sitecustomize this folder's one shadows, if there is one.

    Putting a sitecustomize first on PYTHONPATH hides any other — an
    interpreter's own, or coverage's for measuring subprocesses — so the
    next one on sys.path is found and run too. (None of this machine's
    conda envs has one.)"""
    import importlib.machinery
    import importlib.util
    import os
    mine = os.path.normcase(os.path.abspath(here))
    rest = [p for p in sys.path
            if p and os.path.normcase(os.path.abspath(p)) != mine]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
