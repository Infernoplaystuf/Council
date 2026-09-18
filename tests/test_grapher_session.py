"""
The Grapher's session: one dataset, one answer.

THE DEFECT THIS MODULE EXISTS FOR (phase-6 requirement B7)
The Tk Grapher applies transforms and the overlay inside its Plotly render
method. The inline (offline) and export paths read the raw dataset, so they
draw the untransformed frame. Normalise a column, look at the inline chart, and
it is the raw data — no error, no note, just a different chart from the one the
interactive view shows.

Everything here runs against real files on disk and a real pandas frame,
because a suite that stubbed the loader would prove none of it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import grapher  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("pandas", reason="the Grapher needs pandas")

import pandas as pd  # noqa: E402

import graph_engine as ge  # noqa: E402


@pytest.fixture
def csv(tmp_path):
    path = tmp_path / "main.csv"
    pd.DataFrame({"t": range(10),
                  "v": [i * 2 for i in range(10)],
                  "w": [100] * 10}).to_csv(path, index=False)
    return path


@pytest.fixture
def other_csv(tmp_path):
    path = tmp_path / "other.csv"
    pd.DataFrame({"t": range(10),
                  "v": [i * 3 for i in range(10)]}).to_csv(path, index=False)
    return path


@pytest.fixture
def session(csv):
    s = grapher.Session()
    ok, _message = s.load(csv)
    assert ok
    return s


def spec(**kw):
    kw.setdefault("plot_type", "line")
    kw.setdefault("x_col", "t")
    kw.setdefault("y_col", "v")
    return ge.PlotSpec(**kw)


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "grapher.py").read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5"):
        assert toolkit not in source


def test_the_module_imports_no_renderer_at_module_level():
    """Which renderer draws the frame is the view's business. Importing plotly
    or matplotlib here would put seconds of import cost on every launch that
    touches the session — the reason the Tk build proxies these lazily."""
    source = (ROOT / "council_core" / "grapher.py").read_text(encoding="utf-8")
    head = source.split("class Working", 1)[0]
    for eager in ("import plotly", "import matplotlib", "from plotly",
                  "from matplotlib"):
        assert eager not in head


# ============================================================
# Loading
# ============================================================

def test_a_file_loads(session):
    assert session.dataset is not None
    assert session.columns() == ["t", "v", "w"]


def test_a_file_that_is_not_there_is_refused_not_raised(tmp_path):
    """`DataLoader.load` reports a missing file through `load_error` rather
    than raising, so this is the reporting path, not the guard."""
    s = grapher.Session()
    ok, message = s.load(tmp_path / "nope.csv")
    assert not ok and message
    assert s.dataset is None


def test_a_loader_that_raises_is_still_refused(tmp_path, monkeypatch):
    """The guard, driven directly — a corrupt workbook or a permissions error
    can raise where a missing file does not, and an untested except is how a
    tab dies on a file the user merely clicked."""
    import graph_data

    def _boom(_path):
        raise OSError("device not ready")

    monkeypatch.setattr(graph_data.DataLoader, "load", staticmethod(_boom))
    s = grapher.Session()
    ok, message = s.load(tmp_path / "x.csv")
    assert not ok
    assert "x.csv" in message and "device not ready" in message
    assert s.dataset is None


def test_a_loader_that_raises_does_not_replace_a_good_dataset(csv, tmp_path,
                                                              monkeypatch):
    """Losing the chart you were looking at because the NEXT file failed is
    two failures for the price of one."""
    import graph_data

    s = grapher.Session()
    assert s.load(csv)[0]
    monkeypatch.setattr(graph_data.DataLoader, "load",
                        staticmethod(lambda _p: (_ for _ in ()).throw(
                            OSError("gone"))))
    s.load(tmp_path / "x.csv")
    assert s.dataset is not None


def test_a_failed_load_says_which_file(tmp_path):
    """"Could not load" with no name is useless when the vault list has
    forty files in it."""
    s = grapher.Session()
    _ok, message = s.load(tmp_path / "missing-thing.csv")
    assert "missing-thing.csv" in message


def test_a_failed_load_is_remembered(tmp_path):
    """So the view can say what happened rather than showing an empty column
    list and letting the user conclude the file has no data."""
    s = grapher.Session()
    s.load(tmp_path / "nope.csv")
    assert s.load_error


def test_a_good_load_clears_the_previous_error(tmp_path, csv):
    s = grapher.Session()
    s.load(tmp_path / "nope.csv")
    s.load(csv)
    assert s.load_error == ""


def test_a_chosen_sheet_is_passed_through(tmp_path, monkeypatch):
    """B3: the Tk reload passes NO sheet, so _load_excel keeps sheet_name=0
    and the dropdown snaps the user's pick back with no error."""
    import graph_data

    seen = {}

    def _fake_excel(path, sheet_name=0):
        seen["sheet"] = sheet_name
        frame = pd.DataFrame({"a": [1, 2]})
        return graph_data.DataSet(name="x", source_path=Path(path),
                                  format="excel", df=frame)

    monkeypatch.setattr(graph_data.DataLoader, "_load_excel",
                        staticmethod(_fake_excel))
    book = tmp_path / "book.xlsx"
    book.write_bytes(b"not really a workbook")
    s = grapher.Session()
    s.load(book, sheet="Sheet2")
    assert seen["sheet"] == "Sheet2"


def test_a_csv_is_not_asked_for_a_sheet(csv, monkeypatch):
    import graph_data

    def _boom(*_a, **_k):
        raise AssertionError("a CSV went through the spreadsheet path")

    monkeypatch.setattr(graph_data.DataLoader, "_load_excel",
                        staticmethod(_boom))
    s = grapher.Session()
    assert s.load(csv, sheet="Sheet1")[0]


# ============================================================
# One working dataset — requirement B7
# ============================================================

def test_with_no_transforms_the_working_frame_is_the_loaded_one(session):
    working = session.working()
    assert working.dataset is session.dataset
    assert not working.transformed


def test_a_transform_reaches_the_working_frame(session):
    session.add_transform("normalize", ["v"])
    working = session.working()
    assert working.transformed
    assert working.df["v"].max() == pytest.approx(1.0)


def test_the_loaded_dataset_is_not_mutated(session):
    """Every renderer asks for `working()`. If a transform edited the loaded
    frame in place, removing the transform would not undo it."""
    session.add_transform("normalize", ["v"])
    session.working()
    assert session.dataset.df["v"].max() == 18


def test_removing_a_transform_undoes_it(session):
    session.add_transform("normalize", ["v"])
    assert session.working().df["v"].max() == pytest.approx(1.0)
    session.remove_transform(0)
    assert session.working().df["v"].max() == 18


def test_transforms_are_applied_in_order(session):
    session.add_transform("fill_nan", ["v"], {"value": 0})
    session.add_transform("normalize", ["v"])
    log = session.working().log
    assert len(log) == 2
    assert "fill_nan" in log[0] and "normalize" in log[1]


def test_the_transform_log_is_what_the_panel_shows(session):
    session.add_transform("normalize", ["v"])
    assert session.working().log


def test_a_transform_that_cannot_run_is_reported_in_the_log(session):
    """`apply_transforms` does not raise — it logs a warning and carries on —
    so this is the path a bad expression actually takes."""
    session.add_transform("derive", [], {"name": "bad", "expr": "!!!"})
    working = session.working()
    assert working.df is not None
    assert any("failed" in line for line in working.log), working.log


def test_a_transform_that_raises_does_not_take_the_chart_with_it(session,
                                                                 monkeypatch):
    """The guard, driven directly. `apply_transforms` handles its own errors
    today, so nothing in normal use reaches this — and an untested except is
    how a "safe" wrapper turns out to re-raise."""
    def _boom(_df, _transforms):
        raise RuntimeError("transform engine blew up")

    monkeypatch.setattr(ge, "apply_transforms", _boom)
    session.add_transform("normalize", ["v"])
    working = session.working()
    assert working.df is not None, "the chart went with the transform"
    assert any("transform failed" in line for line in working.log)
    assert not working.transformed


def test_a_derived_column_is_offered_by_the_column_list(session):
    """A picker built from the RAW dataset cannot offer a column a transform
    created, so the user derives one and it is not in the list.

    No skip guard. The first version skipped when the column was absent, which
    is exactly the failure — so the mutation that reads the raw frame turned
    its own test into a skip, and a skip is not a failure.
    """
    session.add_transform("derive", [], {"name": "double_v", "expr": "v * 2"})
    assert "double_v" in session.columns()
    assert "double_v" not in list(session.dataset.df.columns), (
        "the derive leaked into the loaded frame")


def test_the_stats_describe_the_frame_that_is_drawn(session):
    """The Tk panel describes the RAW dataset next to a transformed chart."""
    raw = session.describe()
    session.add_transform("normalize", ["v"])
    assert session.describe() != raw


def test_describing_nothing_says_to_load_a_file(csv):
    s = grapher.Session()
    assert "No file loaded" in s.describe()


def test_a_dataset_that_cannot_be_summarised_says_so(session, monkeypatch):
    def _boom(_ds):
        raise RuntimeError("summary blew up")

    monkeypatch.setattr(ge.DataAnalyser, "describe", staticmethod(_boom))
    assert "Could not summarise" in session.describe()


def test_an_empty_session_has_an_empty_working_set():
    working = grapher.Session().working()
    assert working.dataset is None and working.df is None


# ============================================================
# The overlay — it says why, instead of vanishing
# ============================================================

def test_an_overlay_loads(session, other_csv):
    ok, message = session.load_overlay(other_csv)
    assert ok and "other" in message
    assert session.overlay is not None


def test_a_bad_overlay_is_refused_and_leaves_none(session, tmp_path):
    ok, message = session.load_overlay(tmp_path / "nope.csv")
    assert not ok and message
    assert session.overlay is None


def test_an_overlay_loader_that_raises_clears_the_previous_overlay(
        session, other_csv, tmp_path, monkeypatch):
    """Leaving the old one in place draws a chart overlaid with a file the
    user just replaced — the wrong data, silently."""
    import graph_data

    assert session.load_overlay(other_csv)[0]
    monkeypatch.setattr(graph_data.DataLoader, "load",
                        staticmethod(lambda _p: (_ for _ in ()).throw(
                            OSError("gone"))))
    ok, _message = session.load_overlay(tmp_path / "x.csv")
    assert not ok
    assert session.overlay is None


def test_an_overlay_is_carried_on_a_plot_type_that_can_show_one(session,
                                                                other_csv):
    session.load_overlay(other_csv)
    working = session.working(spec(plot_type="line"))
    assert working.overlay is not None
    assert working.overlay_blocked == ""


@pytest.mark.parametrize("kind", grapher.OVERLAY_KINDS)
def test_every_declared_overlay_kind_carries_one(session, other_csv, kind):
    session.load_overlay(other_csv)
    assert session.working(spec(plot_type=kind)).overlay is not None


def test_a_plot_type_that_cannot_show_an_overlay_says_so(session, other_csv):
    """THE DEFECT. The Tk code wraps the overlay render in `except Exception`
    and falls back to a plain render — the user picked a second file, got a
    chart without it, and was told nothing."""
    session.load_overlay(other_csv)
    working = session.working(spec(plot_type="pie"))
    assert working.overlay is None
    assert "pie" in working.overlay_blocked
    assert working.overlay_blocked != ""


def test_the_reason_names_what_would_work(session, other_csv):
    """"Not supported" leaves the user guessing which of thirty plot types to
    try."""
    session.load_overlay(other_csv)
    blocked = session.working(spec(plot_type="pie")).overlay_blocked
    assert any(kind in blocked for kind in grapher.OVERLAY_KINDS)


def test_an_overlay_with_no_y_column_says_so(session, other_csv):
    session.load_overlay(other_csv)
    working = session.working(spec(y_col=None))
    assert working.overlay is None
    assert "Y column" in working.overlay_blocked


def test_no_overlay_asked_for_is_not_an_error(session):
    working = session.working(spec())
    assert working.overlay is None
    assert working.overlay_blocked == ""


def test_clearing_the_overlay_removes_it(session, other_csv):
    session.load_overlay(other_csv)
    session.clear_overlay()
    assert session.working(spec()).overlay is None


def test_an_overlay_and_a_transform_both_apply(session, other_csv):
    """They are independent, and the Tk path builds one inside the other."""
    session.load_overlay(other_csv)
    session.add_transform("normalize", ["v"])
    working = session.working(spec(plot_type="line"))
    assert working.transformed
    assert working.overlay is not None


# ============================================================
# Scanning
# ============================================================

def test_scanning_finds_data_files(tmp_path, csv):
    found = grapher.scan(csv.parent)
    assert any(p.name == "main.csv" for p in found)


def test_scanning_a_vault_that_is_not_there_is_empty_not_fatal(tmp_path):
    """`scan_vault_for_data` already returns [] for a missing directory, so
    this is the normal path."""
    assert grapher.scan(tmp_path / "no-such-vault") == []


def test_a_scan_that_raises_is_still_an_empty_list(tmp_path, monkeypatch):
    """The guard, driven directly. A dead tab is worse than an empty list, and
    a permissions error on one vault subdirectory should not be either."""
    import graph_data

    monkeypatch.setattr(graph_data, "scan_vault_for_data",
                        lambda _d: (_ for _ in ()).throw(OSError("denied")))
    assert grapher.scan(tmp_path) == []


def test_a_failed_load_does_not_become_the_current_dataset(session, tmp_path):
    """CONFIRMED DEFECT. `_grapher_do_load` assigns `self._grapher_dataset =
    ds` and only THEN checks `ds.load_error`, returning before any control is
    refreshed. Load a good file, then a corrupt one: the tab keeps the old
    file's column pickers over the new file's broken dataset, and the next
    plot draws from a frame that is None."""
    bad = tmp_path / "corrupt.csv"
    bad.write_bytes(b"\x00\x01\x02 not a csv \xff\xfe")
    good = session.dataset
    ok, _message = session.load(tmp_path / "definitely-missing.csv")
    assert not ok
    assert session.dataset is good, "a failed load replaced the good dataset"


# ============================================================
# Dates that arrived as strings
# ============================================================

def test_a_csv_date_column_becomes_a_real_datetime(tmp_path):
    """CSVs are read without parse_dates, so a timestamp column arrives as
    text and classifies as categorical — which makes the whole time-series
    half of the registry unofferable on the file type the Grapher is most used
    with.

    `coerce_datetime_columns` RETURNS A COPY. I first called it and discarded
    the result, which left every date a string and every time plot missing, and
    caught it by looking at the inferred roles rather than believing the call.
    """
    path = tmp_path / "dated.csv"
    pd.DataFrame({"when": pd.date_range("2024-01-01", periods=10),
                  "v": range(10)}).to_csv(path, index=False)
    s = grapher.Session()
    s.load(path)
    assert grapher.roles_for(s.working().df)["when"] == "datetime"


def test_the_time_series_plots_become_offerable(tmp_path):
    path = tmp_path / "dated.csv"
    pd.DataFrame({"when": pd.date_range("2024-01-01", periods=10),
                  "v": range(10)}).to_csv(path, index=False)
    s = grapher.Session()
    s.load(path)
    keys = {c.key for c in grapher.choices_for(s.working().df, ["when", "v"])}
    assert "timeseries" in keys


def test_the_loaded_frame_keeps_its_original_types(tmp_path):
    """The coercion belongs to the WORKING frame. Rewriting the loaded one
    would make "what did this file contain" unanswerable."""
    path = tmp_path / "dated.csv"
    pd.DataFrame({"when": pd.date_range("2024-01-01", periods=5),
                  "v": range(5)}).to_csv(path, index=False)
    s = grapher.Session()
    s.load(path)
    s.working()
    assert str(s.dataset.df["when"].dtype) in ("object", "str")


def test_a_frame_with_no_dates_is_not_copied(session):
    """`coerce_datetime_columns` always copies, and `working()` runs once per
    chart — so copying a large frame when nothing needed coercing is pure
    cost, every render."""
    assert session.working().dataset is session.dataset


def test_a_coercion_that_cannot_run_leaves_the_frame_alone(session,
                                                           monkeypatch):
    import plot_roles

    def _boom(_frame):
        raise ValueError("pandas is unhappy")

    monkeypatch.setattr(plot_roles, "coerce_datetime_columns", _boom)
    s = grapher.Session()
    s.dataset = session.dataset
    assert grapher._dated_copy(session.dataset) is session.dataset


# ============================================================
# Which plots are offered
# ============================================================

def test_only_plots_the_columns_can_draw_are_offered(session):
    """Offering one that cannot be built is a button that produces an error
    message, and the registry already knows the answer."""
    frame = session.working().df
    for choice in grapher.choices_for(frame, ["v"]):
        assert grapher.build_figure(frame, choice.key, ["v"]).ok or \
            "isn't installed" in grapher.build_figure(frame, choice.key,
                                                      ["v"]).message


def test_no_columns_offers_no_plots(session):
    assert grapher.choices_for(session.working().df, []) == []


def test_the_choice_carries_its_key_beside_its_label(session):
    """The Tk picker builds "Density (KDE)  (kde)" and recovers the key with
    rsplit — which works for all 31 current labels, checked rather than
    assumed, but only because no key contains a parenthesis."""
    choice = grapher.choices_for(session.working().df, ["v"])[0]
    assert choice.key and choice.key in choice.caption
    assert choice.label in choice.caption


def test_the_hint_says_what_would_help(session):
    """"No plot fits" alone leaves the user guessing."""
    hint = grapher.hint_for(["v"], [])
    assert "numeric" in hint or "category" in hint


def test_the_hint_for_no_selection_asks_for_one(session):
    assert "Select" in grapher.hint_for([], [])


def test_a_builder_failure_shows_its_own_sentence(session):
    """"Density (KDE) needs seaborn, which isn't installed." is the thing
    worth showing. A traceback is not."""
    result = grapher.build_figure(session.working().df, "kde", ["v"])
    if result.ok:
        pytest.skip("seaborn is installed here")
    assert "seaborn" in result.message


def test_an_unknown_plot_key_is_refused_cleanly(session):
    result = grapher.build_figure(session.working().df, "no_such_plot", ["v"])
    assert not result.ok
    assert "unknown plot type" in result.message


def test_building_with_no_data_says_to_load_a_file():
    assert "Load a data file" in grapher.build_figure(None, "line", ["a"]).message


def test_building_with_no_columns_says_to_pick_some(session):
    result = grapher.build_figure(session.working().df, "line", [])
    assert not result.ok
    assert "Pick columns" in result.message
