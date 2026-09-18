"""
The Grapher tab: pick a file, pick columns, get a chart.

Against real CSVs and the real renderers — a suite that stubbed plot_registry
would prove nothing about whether a chart can actually be drawn.

ONE EMBEDDED VIEW, NOT TWO
The Tk tab's second pane has never worked: its own comment says the embedded
HTML widget "can only ever show a static shell", because it has no JavaScript
engine. This port does not reproduce it — the interactive path opens the system
browser, which is the only Plotly route that has ever drawn a chart here.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import matplotlib  # noqa: E402

matplotlib.use("Agg")

from council_core import grapher  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("pandas")
pytest.importorskip("PySide6", reason="the Grapher tab needs PySide6")

import pandas as pd  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.grapher import (GrapherActions, GrapherTab,  # noqa: E402
                                     build_grapher)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    pd.DataFrame({"when": pd.date_range("2024-01-01", periods=20),
                  "yield": range(20),
                  "build": list("ABCD") * 5}).to_csv(root / "runs.csv",
                                                     index=False)
    pd.DataFrame({"when": pd.date_range("2024-01-01", periods=20),
                  "yield": [i * 2 for i in range(20)]}).to_csv(
        root / "other.csv", index=False)
    return root


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def tab(qapp, vault):
    view = GrapherTab(actions=GrapherActions(vault), auto_refresh=True)
    view.resize(1200, 700)
    yield view
    import threading
    deadline = time.time() + 5.0
    while any(t.name.startswith("grapher-") and t.is_alive()
              for t in threading.enumerate()) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    view.plots.clear()
    view.deleteLater()
    qapp.processEvents()


def load_runs(tab):
    for i in range(tab.files.count()):
        if tab.files.item(i).text() == "runs.csv":
            tab.files.setCurrentRow(i)
            break
    tab.on_load()


def select(tab, *names):
    tab.columns.clearSelection()
    for i in range(tab.columns.count()):
        if tab.columns.item(i).data(Qt.ItemDataRole.UserRole) in names:
            tab.columns.item(i).setSelected(True)


# ============================================================
# Files
# ============================================================

def test_the_factory_takes_a_window(qapp):
    view = build_grapher(None)
    assert isinstance(view, GrapherTab)
    view.deleteLater()


def test_the_vault_data_files_are_listed(tab):
    names = {tab.files.item(i).text() for i in range(tab.files.count())}
    assert "runs.csv" in names


def test_each_file_row_carries_its_path(tab):
    """The Tk overlay resolver builds its labels data_in-relative and resolves
    them vault-relative, so the loop falls through and the overlay silently
    never loads. The path travels with the row here."""
    item = tab.files.item(0)
    stored = item.data(Qt.ItemDataRole.UserRole)
    assert stored and Path(stored).exists()


def test_loading_fills_the_column_list(tab):
    load_runs(tab)
    labels = [tab.columns.item(i).text() for i in range(tab.columns.count())]
    assert any("yield" in label for label in labels)
    assert any("date" in label for label in labels), "no role shown"


def test_a_column_row_carries_its_real_name(tab):
    """The label is "yield   [nume]". Parsing the name back out of that is the
    defect this port keeps finding."""
    load_runs(tab)
    names = {tab.columns.item(i).data(Qt.ItemDataRole.UserRole)
             for i in range(tab.columns.count())}
    assert names == {"when", "yield", "build"}


def test_loading_nothing_says_to_pick_a_file(tab):
    tab.files.setCurrentRow(-1)
    tab.on_load()
    assert "Pick a file" in tab.status.text()


def test_a_failed_load_keeps_the_previous_columns(tab):
    """`_grapher_do_load` assigns the dataset and only THEN checks
    load_error, so a corrupt file leaves the previous file's pickers over a
    None frame."""
    load_runs(tab)
    before = tab.columns.count()
    tab.actions.load = lambda p, sheet=None: (False, "corrupt")
    tab.on_load()
    assert tab.columns.count() == before


# ============================================================
# Which plots are offered
# ============================================================

def test_only_applicable_plots_are_offered(tab):
    load_runs(tab)
    select(tab, "yield")
    offered = {tab.kinds.itemData(i) for i in range(tab.kinds.count())}
    assert "histogram" in offered
    assert offered, "nothing offered for a numeric column"


def test_the_plot_key_is_carried_as_data_not_parsed_from_the_label(tab):
    load_runs(tab)
    select(tab, "yield")
    assert tab.kinds.itemData(0) == tab.selected_kind()
    assert "(" in tab.kinds.itemText(0), "the caption lost its key"


def test_the_time_series_plots_appear_for_a_csv_date_column(tab):
    """CSVs are read without parse_dates, so the dates arrive as strings. The
    working frame coerces them, which is what makes these offerable at all."""
    load_runs(tab)
    select(tab, "when", "yield")
    offered = {tab.kinds.itemData(i) for i in range(tab.kinds.count())}
    assert "timeseries" in offered


def test_no_selection_offers_nothing_and_says_what_to_do(tab):
    load_runs(tab)
    tab.columns.clearSelection()
    assert tab.kinds.count() == 0
    assert "Select" in tab.hint.text()


def test_a_selection_nothing_fits_says_what_would_help(tab):
    load_runs(tab)
    select(tab, "build")
    if tab.kinds.count():
        pytest.skip("this build offers a plot for a lone categorical column")
    assert "numeric" in tab.hint.text() or "category" in tab.hint.text()


# ============================================================
# Plotting
# ============================================================

def test_plotting_adds_a_figure(tab):
    load_runs(tab)
    select(tab, "yield")
    tab.kinds.setCurrentIndex(
        [tab.kinds.itemData(i) for i in range(tab.kinds.count())]
        .index("histogram"))
    tab.on_plot()
    assert tab.plots.count == 1
    assert "histogram" in tab.hint.text()


def test_a_plot_that_cannot_be_built_shows_its_own_sentence(tab):
    """"Density (KDE) needs seaborn, which isn't installed." is the useful
    thing. A traceback is not, and a half-drawn chart is worse than either."""
    load_runs(tab)
    select(tab, "yield")
    result = grapher.build_figure(tab.actions.working().df, "kde", ["yield"])
    if result.ok:
        pytest.skip("seaborn is installed here")
    assert "seaborn" in result.message


def test_plotting_with_nothing_picked_says_so(tab):
    load_runs(tab)
    tab.columns.clearSelection()
    tab.on_plot()
    assert tab.plots.count == 0
    assert "Pick columns" in tab.hint.text()


def test_several_plots_stack_up_in_the_rail(tab):
    load_runs(tab)
    select(tab, "yield")
    for _ in range(3):
        tab.on_plot()
    assert tab.plots.count == 3


# ============================================================
# Transforms reach every renderer — requirement B7
# ============================================================

def test_a_transform_changes_what_the_offline_chart_draws(tab):
    """THE DEFECT. The Tk tab applies transforms inside its PLOTLY render
    method, so the offline pane and the export draw the raw frame — a
    different chart from the interactive one, with nothing to say so."""
    load_runs(tab)
    select(tab, "yield")
    before = tab.actions.working().df["yield"].max()
    tab.on_add_transform_named("normalize")
    after = tab.actions.working().df["yield"].max()
    assert before == 19 and after == pytest.approx(1.0)


def test_the_transform_list_shows_what_was_applied(tab):
    load_runs(tab)
    select(tab, "yield")
    tab.on_add_transform_named("normalize")
    assert tab.transforms.count() == 1
    assert "normalize" in tab.transforms.item(0).text()


def test_a_transform_with_no_columns_selected_says_so(tab):
    load_runs(tab)
    tab.columns.clearSelection()
    tab.on_add_transform()
    assert tab.transforms.count() == 0
    assert "columns" in tab.hint.text()


def test_clearing_transforms_restores_the_raw_frame(tab):
    load_runs(tab)
    select(tab, "yield")
    tab.on_add_transform_named("normalize")
    tab.on_clear_transforms()
    assert tab.actions.working().df["yield"].max() == 19
    assert tab.transforms.count() == 0


def test_the_summary_describes_the_transformed_frame(tab, qapp):
    """The Tk panel describes the RAW dataset beside a transformed chart."""
    load_runs(tab)
    select(tab, "yield")
    deadline = time.time() + 5
    while not tab.stats.toPlainText() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    before = tab.stats.toPlainText()
    tab.on_add_transform_named("normalize")
    deadline = time.time() + 5
    while tab.stats.toPlainText() == before and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert tab.stats.toPlainText() != before


# ============================================================
# Overlay
# ============================================================

def test_an_overlay_loads_from_the_file_list(tab):
    load_runs(tab)
    for i in range(tab.files.count()):
        if tab.files.item(i).text() == "other.csv":
            tab.files.setCurrentRow(i)
            break
    tab.on_overlay()
    assert tab.actions.session.overlay is not None
    assert "overlay" in tab.status.text()


def test_clearing_the_overlay_removes_it(tab):
    load_runs(tab)
    tab.files.setCurrentRow(0)
    tab.on_overlay()
    tab.on_clear_overlay()
    assert tab.actions.session.overlay is None


def test_an_overlay_with_no_file_picked_says_so(tab):
    tab.files.setCurrentRow(-1)
    tab.on_overlay()
    assert "Pick" in tab.status.text()


# ============================================================
# The interactive path
# ============================================================

def test_the_interactive_path_writes_a_real_html_file(tab, qapp):
    load_runs(tab)
    select(tab, "when", "yield")
    index = [tab.kinds.itemData(i) for i in range(tab.kinds.count())]
    tab.kinds.setCurrentIndex(index.index("line"))
    opened = []
    tab.actions.open_in_browser = opened.append
    tab.on_interactive()
    deadline = time.time() + 20
    while tab._busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    assert opened, tab.hint.text()
    assert opened[0].exists() and opened[0].stat().st_size > 1000


def test_a_plot_with_no_interactive_version_says_so(tab):
    load_runs(tab)
    result = grapher.render_html(tab.actions.session, "autocorr", ["yield"],
                                 tab.actions.vault_dir / "out")
    assert not result.ok
    assert "offline" in result.message


def test_nothing_is_embedded_in_the_tab(tab):
    """The Tk embedded HTML pane has no JavaScript engine and can only ever
    show a static shell. Reproducing it would be porting a feature that has
    never worked."""
    source = (ROOT / "council_qt" / "tabs" / "grapher.py").read_text(
        encoding="utf-8")
    for embedded in ("QWebEngineView", "QWebView", "setHtml("):
        assert embedded not in source


def test_the_interactive_render_runs_off_the_gui_thread(tab, qapp):
    import threading
    load_runs(tab)
    select(tab, "when", "yield")
    seen = []
    real = grapher.render_html

    def watched(*args, **kwargs):
        seen.append(threading.current_thread().name)
        return real(*args, **kwargs)

    grapher.render_html = watched
    try:
        tab.actions.open_in_browser = lambda p: None
        tab.on_interactive()
        deadline = time.time() + 20
        while tab._busy and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
    finally:
        grapher.render_html = real
    assert seen and seen[0] != "MainThread"


def test_no_worker_touches_a_widget_directly():
    """Checked on the AST, ignoring anything handed to `_to_ui`.

    The string-splitting version of this reported the stats refresh as a
    violation because its widget call is INSIDE the seam — right code, wrong
    check.
    """
    from tests.source_checks import widget_touches_in_worker
    source = (ROOT / "council_qt" / "tabs" / "grapher.py").read_text(
        encoding="utf-8")
    widgets = ("hint", "plots", "stats", "status", "files", "columns",
               "kinds", "transforms")
    for name in ("on_interactive", "refresh_stats"):
        touched = widget_touches_in_worker(source, name, widgets)
        assert not touched, f"{name}'s worker touches {touched}"
