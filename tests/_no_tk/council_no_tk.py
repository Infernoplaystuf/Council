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

With COUNCIL_NO_TK_LOG set to a file, every refusal is also appended to it
(pid, the test that was running, what was called) — how a run shows the
guard never fired outside the tests of the guard itself.

Standard library only, and syntax an old Python still parses: a child may be
another interpreter (a camera SDK's conda env, a 3.9 probe), and a
sitecustomize that fails would break every child, not just Tk ones.
"""
import sys

#: What a refused Tk root or Toplevel says.
MESSAGE = ("Tk GUIs are deprecated - tests must not open a Tk window "
           "(see docs/qt_migration/measurements.md section 4 and "
           "tests/README.md)")

#: The environment variable naming a file to record refusals in.
LOG_ENV = "COUNCIL_NO_TK_LOG"


class TkWindowRefused(BaseException):
    """A test (or a program a test started) tried to open a Tk window.

    A BaseException, like KeyboardInterrupt, ON PURPOSE. Tk tests guarded
    their root with `except Exception: skip` ("no display") — the old tk_root
    fixture did, the Tk agent-panel test did, smoke_test's grapher check
    printed "skipped" and returned — so an ordinary exception here would
    have become a quiet skip or a passing check instead of a failure."""


def _record(what):
    import os
    log = os.environ.get(LOG_ENV)
    if not log:
        return
    try:
        with open(log, "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%s\n" % (
                os.getpid(), os.environ.get("PYTEST_CURRENT_TEST", "-"), what))
    except Exception:                                # noqa: BLE001
        pass


def _refuse(what):
    _record(what)
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
    """Patches tkinter as it is imported. Finds nothing itself.

    The spec comes from the finders AFTER this one on sys.meta_path, so
    another import hook that also wraps tkinter (a coverage or tracing tool)
    still gets to; this one patches last, so the refusal is what stays."""

    def find_spec(self, name, path=None, target=None):
        if name != "tkinter":
            return None
        finders = list(sys.meta_path)
        later = finders[finders.index(self) + 1:] if self in finders else []
        spec = None
        for finder in later:
            find = getattr(finder, "find_spec", None)
            if find is not None:
                spec = find(name, path, target)
                if spec is not None:
                    break
        if spec is None:
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
    for finder in list(sys.meta_path):
        if isinstance(finder, _Finder):
            sys.meta_path.remove(finder)
    sys.meta_path.insert(0, _Finder())       # first, ahead of later hooks


def installed():
    """Whether a Tk window would be refused here right now."""
    loaded = sys.modules.get("tkinter")
    if loaded is not None:
        return bool(getattr(loaded, "_council_no_tk", False))
    return bool(sys.meta_path) and isinstance(sys.meta_path[0], _Finder)


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
