"""
council_core.grapher_files — B2 and B3, made impossible rather than fixed.

Both defects come from the same habit: treating a label as an address, and
letting a control write to a variable nothing reads. These tests hold the two
structural choices that remove them — an entry carries its path, and the sheet
travels in the load request.

No toolkit, no display, no pandas needed for most of it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import grapher_files as gf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def vault(tmp_path):
    """A data_in with a few files, one of them nested."""
    in_dir = tmp_path / "data_in"
    (in_dir / "q3").mkdir(parents=True)
    (in_dir / "sales.csv").write_text("a,b\n1,2\n")
    (in_dir / "q3" / "invoices.csv").write_text("a,b\n3,4\n")
    (in_dir / "derived").mkdir()
    (in_dir / "derived" / "old_chart.csv").write_text("x\n1\n")
    return in_dir


def test_the_shared_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "grapher_files.py").read_text(
        encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "PyQt6"):
        assert toolkit not in source


# ============================================================
# B2 — a label is not an address
# ============================================================

def test_a_label_inside_data_in_is_short_and_readable(vault):
    entry = gf.entry_for(vault / "q3" / "invoices.csv", vault)
    assert entry.label == "q3/invoices.csv"
    assert entry.path == vault / "q3" / "invoices.csv"


def test_a_label_outside_data_in_is_the_full_path(tmp_path, vault):
    """A bare basename would collide with a file of the same name in the
    vault, and the user could not tell which one they picked."""
    outsider = tmp_path / "elsewhere" / "sales.csv"
    outsider.parent.mkdir()
    outsider.write_text("a\n1\n")
    entry = gf.entry_for(outsider, vault, origin="browsed")
    assert entry.label == str(outsider)
    assert entry.origin == "browsed"


def test_labels_use_forward_slashes(vault):
    """The label is compared as text and shown to a user; a backslash makes
    the same file look like a different one on another platform."""
    entry = gf.entry_for(vault / "q3" / "invoices.csv", vault)
    assert "\\" not in entry.label


def test_resolving_is_a_lookup_and_not_path_arithmetic(vault):
    """THE WHOLE OF B2. The Tk overlay resolver re-derives the path
    vault-relative while the labels were built data_in-relative, so
    'sales.csv' is compared against 'data_in/sales.csv' and never matches —
    and the loop falls through in silence."""
    entries = gf.scan_input_files(vault)
    found = gf.resolve(entries, "sales.csv")
    assert found is not None, "the common case still does not resolve"
    assert found.path.name == "sales.csv"
    assert found.path.exists()


def test_a_browsed_entry_resolves_by_its_own_label(tmp_path, vault):
    """The audit's correction to the original finding: browsed entries carry
    ABSOLUTE labels and always did resolve, so the overlay works in the rare
    path and fails silently in the common one — which is worse than broken,
    because the user has seen it work."""
    outsider = tmp_path / "extra.csv"
    outsider.write_text("a\n1\n")
    entries = gf.merge_entries(gf.scan_input_files(vault),
                               [gf.entry_for(outsider, vault, "browsed")])
    assert gf.resolve(entries, str(outsider)) is not None
    assert gf.resolve(entries, "sales.csv") is not None, (
        "the two conventions still cannot coexist")


def test_a_label_that_names_nothing_returns_none_not_a_bad_path(vault):
    entries = gf.scan_input_files(vault)
    assert gf.resolve(entries, "gone.csv") is None


def test_an_unresolved_selection_has_something_to_say():
    """The Tk loop falls through in silence: the user clicks Overlay and
    nothing at all happens — no chart, no error, no clue."""
    message = gf.unresolved_message("gone.csv")
    assert "gone.csv" in message
    assert "refresh" in message.lower()


def test_generated_output_is_not_offered_as_input(vault):
    """A chart built from last week's chart is not a thing anybody wants."""
    labels = {e.label for e in gf.scan_input_files(vault)}
    assert not any(label.startswith("derived/") for label in labels)


def test_the_newest_file_comes_first(vault):
    """The file a user wants is almost always the one they just put there, and
    an alphabetical list buries it."""
    import os
    import time
    newest = vault / "zzz_latest.csv"
    newest.write_text("a\n1\n")
    os.utime(newest, (time.time() + 60, time.time() + 60))
    entries = gf.scan_input_files(vault)
    assert entries[0].path.name == "zzz_latest.csv"


def test_refreshing_the_scan_keeps_what_the_user_added(tmp_path, vault):
    """The Tk version reassigns the combo's values wholesale on refresh, which
    silently discards every Browse and sample entry."""
    outsider = tmp_path / "browsed.csv"
    outsider.write_text("a\n1\n")
    added = gf.entry_for(outsider, vault, "browsed")
    kept = gf.merge_entries(gf.scan_input_files(vault), [added])
    refreshed = gf.merge_entries(gf.scan_input_files(vault),
                                 [e for e in kept if e.origin != "data_in"])
    assert any(e.path == outsider for e in refreshed)


def test_merging_does_not_duplicate_a_file_already_in_the_list(vault):
    entries = gf.scan_input_files(vault)
    again = gf.merge_entries(entries, [gf.entry_for(vault / "sales.csv", vault)])
    assert len(again) == len(entries)


# ============================================================
# B3 — the sheet has to travel
# ============================================================

def test_the_sheet_is_part_of_the_request():
    """There is no parameter anywhere in the Tk chain to carry it: the combo
    writes to a variable the loader never reads, so choosing a sheet reloads
    sheet 0 and then resets the combo to sheet 0."""
    request = gf.LoadRequest(path=Path("book.xlsx"), sheet="Q3")
    assert request.sheet == "Q3"


def test_a_request_with_no_sheet_asks_for_nothing(monkeypatch, tmp_path):
    """Passing sheet_name=None would override pandas' own default."""
    seen = {}

    class Loader:
        @staticmethod
        def load(path, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")

    import types
    monkeypatch.setitem(sys.modules, "graph_data",
                        types.SimpleNamespace(DataLoader=Loader))
    target = tmp_path / "x.csv"
    target.write_text("a\n1\n")
    gf.load_dataset(gf.LoadRequest(path=target))
    assert "sheet_name" not in seen


def test_a_requested_sheet_reaches_the_loader(monkeypatch, tmp_path):
    seen = {}

    class Loader:
        @staticmethod
        def load(path, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")

    import types
    monkeypatch.setitem(sys.modules, "graph_data",
                        types.SimpleNamespace(DataLoader=Loader))
    target = tmp_path / "x.xlsx"
    target.write_text("x")
    gf.load_dataset(gf.LoadRequest(path=target, sheet="Q3"))
    assert seen.get("sheet_name") == "Q3", (
        "the sheet still does not reach the loader")


def test_the_outcome_reports_the_sheet_that_was_actually_loaded(monkeypatch,
                                                                tmp_path):
    """So the control can show the truth. The Tk version repopulates the combo
    from metadata that always names the first sheet, which is what snaps the
    user's choice back."""
    import types

    class Dataset:
        load_error = None
        shape = (10, 3)
        metadata = {"sheets": ["Q1", "Q3"], "active_sheet": "Q3"}

    monkeypatch.setitem(sys.modules, "graph_data", types.SimpleNamespace(
        DataLoader=types.SimpleNamespace(load=lambda p, **k: Dataset())))
    target = tmp_path / "book.xlsx"
    target.write_text("x")
    outcome = gf.load_dataset(gf.LoadRequest(path=target, sheet="Q3"))
    assert outcome.ok
    assert outcome.loaded_sheet == "Q3"
    assert outcome.sheets == ["Q1", "Q3"]
    assert "sheet Q3" in outcome.message


# ============================================================
# The one the first pass missed
# ============================================================

def test_a_failed_load_hands_back_no_dataset(tmp_path):
    """The Tk code assigns self._grapher_dataset = ds and only THEN checks
    ds.load_error, returning before any control is refreshed — so the tab is
    left describing a file that did not open, and drilling into correlations
    on it crashes. There is nothing to install when ok is False."""
    outcome = gf.load_dataset(gf.LoadRequest(path=tmp_path / "missing.csv"))
    assert not outcome.ok
    assert outcome.dataset is None
    assert "no longer exists" in outcome.message


def test_a_loader_that_raises_is_reported_not_propagated(monkeypatch, tmp_path):
    import types
    monkeypatch.setitem(sys.modules, "graph_data", types.SimpleNamespace(
        DataLoader=types.SimpleNamespace(
            load=lambda p, **k: (_ for _ in ()).throw(ValueError("bad csv")))))
    target = tmp_path / "x.csv"
    target.write_text("a\n1\n")
    outcome = gf.load_dataset(gf.LoadRequest(path=target))
    assert not outcome.ok
    assert outcome.dataset is None
    assert "bad csv" in outcome.message
    assert outcome.error is not None


def test_a_dataset_carrying_load_error_is_still_a_failure(monkeypatch, tmp_path):
    """DataLoader reports some failures by returning a dataset with
    load_error set rather than by raising. Both are failures."""
    import types

    class Broken:
        load_error = "no columns found"
        shape = (0, 0)
        metadata = {}

    monkeypatch.setitem(sys.modules, "graph_data", types.SimpleNamespace(
        DataLoader=types.SimpleNamespace(load=lambda p, **k: Broken())))
    target = tmp_path / "x.csv"
    target.write_text("x")
    outcome = gf.load_dataset(gf.LoadRequest(path=target))
    assert not outcome.ok
    assert outcome.dataset is None
    assert "no columns found" in outcome.message
