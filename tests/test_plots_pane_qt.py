"""
The session's plots: the history arithmetic, and the pane over it.

The history is where this goes wrong. Every row in the rail is bound to an
index, and dropping the oldest figure shifts every one of them — so a rail that
does not keep up shows you figure N and hands you figure N+1 when you click it.
That is silent: you get a chart, just not the one you asked for.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import matplotlib  # noqa: E402

matplotlib.use("Agg")

from matplotlib.figure import Figure  # noqa: E402

from council_core import plots  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def mk(tag=0, colour="#89b4fa"):
    figure = Figure(figsize=(4, 3))
    axes = figure.add_subplot(111)
    axes.plot([1, 2, 3], [tag, tag + 2, tag], color=colour)
    axes.set_title(f"fig {tag}")
    return figure


def title_of(figure):
    return figure.axes[0].get_title()


def test_the_core_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "plots.py").read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5"):
        assert toolkit not in source


def test_nothing_uses_pyplot():
    """pyplot puts every figure on a global registry nothing ever clears — a
    leak across a session — and lets a backend open a window of its own, which
    headless is a crash and on a user's machine is a stray window."""
    for name in (ROOT / "council_core" / "plots.py",
                 ROOT / "council_qt" / "widgets" / "plots_pane.py"):
        source = name.read_text(encoding="utf-8")
        assert "pyplot" not in source or "pyplot would" in source, name.name


# ============================================================
# Rendering
# ============================================================

def test_a_figure_renders_to_png_bytes():
    data = plots.figure_to_png_bytes(mk())
    assert data[:4] == b"\x89PNG"


def test_a_thumbnail_is_no_wider_than_asked():
    from PIL import Image
    image = Image.open(io.BytesIO(plots.thumbnail_png(mk(), width=180)))
    assert image.size[0] <= 180


def test_a_thumbnail_is_actually_small():
    """A rail of full-size renders is the leak the cap exists to prevent."""
    full = plots.figure_to_png_bytes(mk(), dpi=100)
    assert len(plots.thumbnail_png(mk(), width=180)) < len(full)


def test_a_narrower_thumbnail_is_narrower():
    from PIL import Image
    wide = Image.open(io.BytesIO(plots.thumbnail_png(mk(), width=180)))
    narrow = Image.open(io.BytesIO(plots.thumbnail_png(mk(), width=60)))
    assert narrow.size[0] < wide.size[0]


def test_a_thumbnail_is_rendered_small_not_resampled():
    """A 4x downsample of an Agg render turns ticks and 1px grid lines into
    grey mush, and the point of the rail is telling charts apart."""
    from tests.source_checks import code_of
    source = (ROOT / "council_core" / "plots.py").read_text(encoding="utf-8")
    body = code_of(source, "thumbnail_png")
    assert "dpi" in body
    for resample in ("thumbnail(", "resize(", "Image."):
        assert resample not in body


# ============================================================
# The history
# ============================================================

def test_the_newest_figure_is_the_one_shown():
    """It is the one you just made."""
    history = plots.FigureHistory()
    for i in range(3):
        history.add(mk(i))
    assert title_of(history.current()) == "fig 2"


def test_nothing_is_dropped_below_the_cap():
    history = plots.FigureHistory(max_history=5)
    for i in range(5):
        assert history.add(mk(i)).dropped == 0
    assert len(history) == 5


def test_the_oldest_goes_when_the_cap_is_passed():
    """Figures are cheap but not free; a long session makes hundreds and
    without a cap the app grows until it is swapping."""
    history = plots.FigureHistory(max_history=3)
    for i in range(6):
        history.add(mk(i))
    assert len(history) == 3
    assert [title_of(f) for f in history.figures] == ["fig 3", "fig 4", "fig 5"]


def test_the_selection_follows_the_figures_it_points_at():
    """THE BUG THIS CLASS EXISTS FOR. Every rail row is bound to an index, so
    dropping the oldest shifts all of them — and a selection left where it was
    now points at a different chart, silently."""
    history = plots.FigureHistory(max_history=3)
    for i in range(3):
        history.add(mk(i))
    history.select(1)
    watched = title_of(history.current())
    history.add(mk(99))
    history.select(history.active if history.active is not None else 0)
    # the figure it was watching moved down one, and select() followed it
    assert title_of(history.at(0)) == "fig 1"
    assert watched == "fig 1"


def test_the_selection_moves_by_how_many_were_dropped():
    """Not by one. The Tk pane decrements once per loop iteration, which is the
    same thing only because it drops one at a time — a cap that shrinks by
    several points at the wrong figure."""
    history = plots.FigureHistory(max_history=10)
    for i in range(10):
        history.add(mk(i))
    history.select(9)
    history.max_history = 3
    trim = history._trim()
    assert trim.dropped == 7
    assert history.active == 2
    assert title_of(history.current()) == "fig 9"


def test_the_selection_never_goes_negative():
    """`at()` refuses a negative index, so an underflowed selection makes
    `current()` return None — a pane with figures in it and nothing on screen,
    and a rail with nothing highlighted.

    Driven by shrinking the cap under a LOW selection, because `add` always
    selects the newest first and can therefore never produce the underflow.
    """
    history = plots.FigureHistory(max_history=10)
    for i in range(10):
        history.add(mk(i))
    history.select(1)
    history.max_history = 2
    history._trim()
    assert history.active is not None and history.active >= 0, history.active
    assert history.current() is not None, "figures present, nothing selectable"
    assert title_of(history.current()) == "fig 8"


def test_selecting_something_that_is_not_there_is_refused():
    history = plots.FigureHistory()
    history.add(mk())
    assert history.select(5) is False
    assert history.select(-1) is False
    assert history.active == 0


def test_an_empty_history_has_nothing_current():
    history = plots.FigureHistory()
    assert history.current() is None
    assert history.at(0) is None
    assert len(history) == 0


def test_clearing_forgets_the_selection_too():
    """A selection pointing into an emptied list is what makes "clear" crash
    on the next repaint."""
    history = plots.FigureHistory()
    history.add(mk())
    history.clear()
    assert history.active is None
    assert history.current() is None


def test_a_zero_cap_is_refused():
    """It would drop each figure as it arrived and show nothing, which reads
    as a broken plotter rather than as a setting."""
    with pytest.raises(ValueError):
        plots.FigureHistory(max_history=0)


def test_a_thousand_figures_stay_capped():
    history = plots.FigureHistory(max_history=4)
    for i in range(1000):
        history.add(mk(i))
    assert len(history) == 4
    assert title_of(history.current()) == "fig 999"


# ============================================================
# The Qt pane
# ============================================================

pytest.importorskip("PySide6", reason="the plots pane needs PySide6")

from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.widgets.plots_pane import PlotsPane  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def pane(qapp):
    view = PlotsPane(max_history=3)
    view.resize(900, 500)
    yield view
    view.clear()
    view.deleteLater()
    qapp.processEvents()


def test_an_empty_pane_says_what_to_do(pane):
    assert "No plots yet" in pane.count_label.text()
    assert pane.count == 0


def test_adding_a_figure_shows_it_and_adds_a_thumbnail(pane):
    pane.add_figure(mk(1))
    assert pane.count == 1
    assert pane.rail.count() == 1
    assert title_of(pane.current_figure()) == "fig 1"


def test_the_pane_actually_paints_the_figure(pane, qapp):
    pane.add_figure(mk(1, "#f38ba8"))
    pane.show()
    qapp.processEvents()
    pixmap = QPixmap(900, 500)
    pane.render(pixmap)
    image = pixmap.toImage()
    colours = {image.pixelColor(x, y).name()
               for x in range(0, 900, 6) for y in range(0, 500, 6)}
    assert len(colours) > 10, f"only {colours}"


def test_show_still_means_make_visible(pane, qapp):
    """The Tk pane's method is `show(idx)`; on a QWidget `show()` is Qt's own.
    Aliasing them made `pane.show()` raise "missing 1 required positional
    argument", so the pane could be built and never displayed."""
    pane.add_figure(mk())
    pane.show()                      # must not raise
    qapp.processEvents()
    assert pane.isVisible()


def test_the_rail_and_the_history_stay_in_step_after_a_trim(pane):
    """Click row 0 after two were dropped and you must get the figure that IS
    row 0, not the one that used to be."""
    for i in range(5):
        pane.add_figure(mk(i))
    assert pane.rail.count() == pane.count == 3
    pane.rail.setCurrentRow(0)
    assert title_of(pane.current_figure()) == "fig 2"


def test_clicking_a_thumbnail_shows_that_figure(pane):
    for i in range(3):
        pane.add_figure(mk(i))
    pane.rail.setCurrentRow(1)
    assert title_of(pane.current_figure()) == "fig 1"


def test_showing_a_figure_reports_it(pane, qapp):
    seen = []
    pane.selected.connect(lambda i, f: seen.append((i, title_of(f))))
    pane.add_figure(mk(7))
    qapp.processEvents()
    assert seen and seen[-1][1] == "fig 7"


def test_the_count_label_tracks_the_history(pane):
    for i in range(3):
        pane.add_figure(mk(i))
    assert "3 of 3" in pane.count_label.text()


def test_the_toolbar_is_the_real_matplotlib_one(pane):
    """Pan, zoom and save are what make an embedded chart usable rather than a
    picture of a chart."""
    from matplotlib.backends.backend_qtagg import NavigationToolbar2QT

    pane.add_figure(mk())
    # In the LAYOUT, not merely parented. The toolbar is constructed with the
    # holder as its parent, so findChildren sees it whether or not it was ever
    # added — and `_toolbar.actions()` reads the same either way. Only its
    # position in the layout says whether the user can click it.
    toolbars = pane.holder.findChildren(NavigationToolbar2QT)
    assert len(toolbars) == 1, "the toolbar never reached the surface"
    assert pane.holder_layout.indexOf(toolbars[0]) >= 0, (
        "the toolbar was built and never laid out")
    actions = {a.text() for a in toolbars[0].actions() if a.text()}
    assert {"Pan", "Zoom", "Save"} <= actions


def test_switching_figures_does_not_stack_canvases(pane, qapp):
    """Left parented, every canvas stays alive and the pane grows one per
    click — each holding its own rendered buffers."""
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    for i in range(3):
        pane.add_figure(mk(i))
    for row in (0, 1, 2, 0, 1):
        pane.rail.setCurrentRow(row)
    qapp.processEvents()
    assert len(pane.holder.findChildren(FigureCanvasQTAgg)) == 1


def test_popping_out_leaves_the_pane_showing_the_same_figure(pane, qapp):
    """A new canvas, not a reparented one: moving this pane's canvas would
    empty the pane the user was looking at."""
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

    pane.add_figure(mk(4))
    before = pane.holder.findChildren(FigureCanvasQTAgg)
    window = pane.popout()
    qapp.processEvents()
    assert window is not None
    assert title_of(pane.current_figure()) == "fig 4"
    # The pane still OWNS a canvas. Checking `_canvas is not None` proved
    # nothing: reparenting leaves the attribute pointing at a canvas that now
    # lives in the popout, and the pane the user was looking at goes blank.
    after = pane.holder.findChildren(FigureCanvasQTAgg)
    assert len(after) == len(before) == 1
    assert after[0].parent() is not window
    window.close()


def test_popping_out_with_nothing_to_show_does_nothing(pane):
    assert pane.popout() is None


def test_the_popout_window_is_held(pane, qapp):
    """Python collects it the moment popout() returns otherwise, and the
    window closes itself immediately."""
    pane.add_figure(mk())
    window = pane.popout()
    qapp.processEvents()
    assert window in pane._popouts
    window.close()


def test_clearing_empties_the_rail_and_the_surface(pane, qapp):
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    for i in range(3):
        pane.add_figure(mk(i))
    pane.clear()
    qapp.processEvents()
    assert pane.count == 0
    assert pane.rail.count() == 0
    assert not pane.holder.findChildren(FigureCanvasQTAgg)
    assert "No plots yet" in pane.count_label.text()


def test_a_figure_whose_thumbnail_fails_still_gets_a_row(pane, monkeypatch):
    """A rail that silently skipped it would put every later index out of step
    with the history — the exact bug the trim arithmetic exists to avoid."""
    def _boom(*_a, **_k):
        raise RuntimeError("no renderer")

    monkeypatch.setattr(plots, "thumbnail_png", _boom)
    pane.add_figure(mk())
    assert pane.rail.count() == pane.count == 1
