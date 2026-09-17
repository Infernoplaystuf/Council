"""
B4 — a failed chart export must not be reported as a success.

From docs/qt_migration/phase6_port_requirements.md. `MatplotlibRenderer.render`
catches a plotting exception, draws a figure that says "Plot error: ..." and
returns it. On screen that is right — a message beats a blank pane. As an
export it is wrong twice: the caller sees a truthy figure, saves it, and prints
a tick, so the user gets a PNG of an error message and is told it worked.

This is in shared code, so the fix serves both front ends and so do these
tests. They need matplotlib but no display and no toolkit.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("MPLBACKEND", "Agg")

ge = pytest.importorskip("graph_engine", reason="needs matplotlib")
if not getattr(ge, "_MPL_OK", False):
    pytest.skip("matplotlib is not installed", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def renderer():
    return ge.MatplotlibRenderer()


@pytest.fixture
def error_figure():
    """A figure marked the way render() marks one it could not draw."""
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    ax.text(0.5, 0.5, "Plot error:\nno numeric columns")
    ge.mark_error(fig, "no numeric columns")
    yield fig
    plt.close(fig)


@pytest.fixture
def real_figure():
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [2, 4, 8])
    yield fig
    plt.close(fig)


# -- the marker ---------------------------------------------------------------

def test_a_real_figure_reports_no_error(real_figure):
    assert ge.error_of(real_figure) == ""


def test_a_marked_figure_reports_why(error_figure):
    assert ge.error_of(error_figure) == "no numeric columns"


def test_marking_is_best_effort_and_never_raises():
    """The marker is set on a matplotlib Figure, which the renderer does not
    own. If a future version rejects attributes, the export check degrades to
    "no error known" rather than taking the chart down with it."""
    class Unmarkable:
        __slots__ = ()

    ge.mark_error(Unmarkable(), "something")          # must not raise
    assert ge.error_of(Unmarkable()) == ""


# -- the refusal --------------------------------------------------------------

def test_saving_an_error_figure_is_refused(renderer, error_figure, tmp_path):
    """The bug, directly: this used to write the PNG and return a path, and
    the caller printed a tick."""
    with pytest.raises(ge.ExportRefused) as caught:
        renderer.save(error_figure, tmp_path / "chart.png")
    assert "no numeric columns" in str(caught.value)
    assert not (tmp_path / "chart.png").exists(), (
        "an error figure was written to disk anyway")


def test_saving_a_real_figure_still_works(renderer, real_figure, tmp_path):
    out = renderer.save(real_figure, tmp_path / "chart.png")
    assert Path(out).exists()
    assert Path(out).stat().st_size > 0


def test_the_refusal_says_what_went_wrong(renderer, error_figure, tmp_path):
    """"Export failed" alone sends the user looking for a disk problem."""
    with pytest.raises(ge.ExportRefused) as caught:
        renderer.save(error_figure, tmp_path / "chart.png")
    message = str(caught.value)
    assert "could not be drawn" in message
    assert "nothing to export" in message


def test_showing_an_error_figure_on_screen_is_still_allowed(renderer,
                                                            error_figure):
    """to_bytes is the display path. Refusing there would replace a useful
    message with a blank pane, which is the opposite of the fix."""
    data = renderer.to_bytes(error_figure)
    assert data[:4] == b"\x89PNG"


# -- render marks what it could not draw --------------------------------------

def test_a_plotting_failure_comes_back_marked(renderer):
    """Proves render() marks it, not just that mark_error works."""
    class Exploding:
        """A DataSet whose frame blows up when plotted."""
        name = "boom"

        class _DF:
            empty = False

            def copy(self):
                return self

            def select_dtypes(self, include=None):
                raise ValueError("no columns at all")

            def __getattr__(self, item):
                raise ValueError("no columns at all")

        df = _DF()

    spec = ge.PlotSpec(plot_type="bar")
    fig = renderer.render(spec, Exploding())
    if fig is None:
        pytest.skip("this spec is refused before it reaches the plot call")
    assert ge.error_of(fig), "render() returned an unmarked error figure"


def test_an_unsupported_type_still_returns_none(renderer):
    """Unchanged: None means "there is no static chart for this", which the
    caller's else-branch already explains properly."""
    assert "MPL_SUPPORTED" in dir(ge)


# -- both front ends ----------------------------------------------------------

#: Every module that calls MatplotlibRenderer.save. Found by searching rather
#: than remembered — the first version of this file had a section headed "both
#: front ends" that only ever read council_gui_engine.py, and missed that
#: grapher_app.py has the identical bare save(). Making save() refuse turned a
#: lie into an uncaught exception there, in a module nobody had checked.
EXPORT_CALLERS = ("council_gui_engine.py", "grapher_app.py")


def test_every_module_that_saves_a_figure_is_known_to_this_file():
    """The list above must not go stale silently.

    A new caller that saves without catching the refusal gets an uncaught
    exception where the old code got a wrong tick, and nothing would say so.
    """
    import ast
    found = []
    for path in sorted(ROOT.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "mpl_r.save(" in source or ".save(fig" in source:
            found.append(path.name)
    unknown = sorted(set(found) - set(EXPORT_CALLERS))
    assert not unknown, (
        f"these save a figure and are not covered here: {unknown}")


@pytest.mark.parametrize("module", EXPORT_CALLERS)
def test_every_export_call_site_handles_the_refusal(module):
    """Tk is being deprecated, but until it is, a caller must not meet a raised
    refusal with nothing — an uncaught exception in a button handler is a worse
    outcome than the original wrong tick."""
    source = (ROOT / module).read_text(encoding="utf-8")
    assert "ExportRefused" in source, f"{module} does not know about it"
    index = source.index("mpl_r.save(fig, Path(path_str))")
    window = source[index - 300:index + 500]
    assert "except ge.ExportRefused" in window, (
        f"{module} saves without catching the refusal")
    assert "Export failed" in window


@pytest.mark.parametrize("module", EXPORT_CALLERS)
def test_the_tick_is_guarded_by_the_refusal_handler(module):
    """Ordering is the whole bug. A tick printed regardless of the write is a
    tick that means nothing."""
    source = (ROOT / module).read_text(encoding="utf-8")
    save_at = source.index("mpl_r.save(fig, Path(path_str))")
    tick_at = source.index("Exported: {saved}", save_at)
    refusal_at = source.index("except ge.ExportRefused", save_at - 300)
    assert save_at < refusal_at < tick_at, (
        f"{module}'s success line is not guarded by the refusal handler")
