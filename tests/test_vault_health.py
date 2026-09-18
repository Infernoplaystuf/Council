"""
Vault Health: what is in the vault, and how big.

THREE SILENT DEFECTS
The panel whose entire job is showing memory files does not show four of them,
because it iterates a hardcoded tuple of role names that three live roles and
the user-profile key are missing from.

A 5 GiB file in the vault root renders as "5MB", because the formatter's loop
divides three times and appends "MB".

And each population loop is wrapped in a single broad `except Exception: pass`
spanning the WHOLE loop, so the first unreadable entry silently ends the list —
a short vault, not a broken one.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import vault_health as vh  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

#: The three roles and one key the Tk tuple misses, verified against
#: council_engine.MEMORY_WRITE_ROLES and _USER_PROFILE_KEY.
MISSED_BY_TK = ("coach", "ideator", "pitcher", "_user_profile")


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    memory = root / "memory"
    memory.mkdir(parents=True)
    for role in ("judge", "writer", "_project", *MISSED_BY_TK):
        (memory / f"memory_{role}.md").write_text("one\ntwo\n",
                                                  encoding="utf-8")
    (root / "wishlist.md").write_text(
        "# Wishes\n- [ ] one\n- [x] two\n- [ ] three\n", encoding="utf-8")
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    (root / "data_in").mkdir()
    return root


@pytest.fixture
def memory(vault):
    return vault / "memory"


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "vault_health.py").read_text(
        encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5"):
        assert toolkit not in source


# ============================================================
# Size formatting
# ============================================================

@pytest.mark.parametrize("size,expected", [
    (0, "0 B"), (500, "500 B"), (2048, "2.0 KB"),
    (5 * 1024 ** 2, "5.0 MB"),
])
def test_small_sizes_read_normally(size, expected):
    assert vh.human_size(size) == expected


def test_a_gigabyte_file_is_not_reported_as_megabytes():
    """THE DEFECT. Tk's loop divides three times and appends "MB", so 5 GiB
    renders as "5MB" — and the vault root is exactly where multi-gigabyte CSVs
    live."""
    text = vh.human_size(5 * 1024 ** 3)
    assert text.endswith("GB")
    assert text.startswith("5")


def test_sizes_past_a_terabyte_still_say_gb_rather_than_wrapping():
    """Better a large GB number than a unit the loop never reaches."""
    assert vh.human_size(7 * 1024 ** 4).endswith("GB")


def test_both_qt_tabs_use_one_formatter():
    """Tk has two — this one and _fmt_bytes — and they disagree above a
    gigabyte."""
    pytest.importorskip("PySide6")
    from council_qt.tabs.vault import _human
    assert _human is vh.human_size


# ============================================================
# Which memory files are shown
# ============================================================

def test_every_memory_file_on_disk_is_listed(memory):
    """Globbed, not looked up from a list of roles. The Tk tuple is missing
    coach, ideator, pitcher and _user_profile — three live roles and the one
    file holding durable user preferences, in a panel whose entire job is
    showing memory files."""
    labels = {e.label for e in vh.memory_files(memory)}
    for role in MISSED_BY_TK:
        assert role in labels, f"{role} is invisible again"


def test_a_role_nobody_listed_still_appears(memory):
    """A role that starts writing memory shows up without anyone remembering
    to add it — which a constant re-derived from MEMORY_WRITE_ROLES would
    still not manage."""
    (memory / "memory_brand_new_role.md").write_text("x", encoding="utf-8")
    assert "brand_new_role" in {e.label for e in vh.memory_files(memory)}


def test_files_that_are_not_memory_are_not_listed(memory):
    (memory / "notes.md").write_text("x", encoding="utf-8")
    assert "notes" not in {e.label for e in vh.memory_files(memory)}


def test_the_memory_label_drops_the_prefix(memory):
    assert "memory_judge" not in {e.label for e in vh.memory_files(memory)}
    assert "judge" in {e.label for e in vh.memory_files(memory)}


def test_a_missing_memory_directory_is_empty_not_an_error(tmp_path):
    """`Path.glob()` returns nothing for a directory that is not there rather
    than raising, so this is the normal path, not the guard."""
    assert vh.memory_files(tmp_path / "never-made") == []


def test_a_memory_directory_that_cannot_be_read_is_empty_not_fatal(
        tmp_path, monkeypatch):
    """The guard, driven directly — a permissions error raises where a missing
    directory does not, and a dead tab is worse than an empty panel."""
    def _boom(self, *a, **k):
        raise OSError("access denied")

    monkeypatch.setattr(Path, "glob", _boom)
    assert vh.memory_files(tmp_path) == []


def test_every_memory_entry_carries_its_path(memory):
    for entry in vh.memory_files(memory):
        assert entry.path is not None and entry.path.exists()


# ============================================================
# The vault root
# ============================================================

def test_the_vault_root_lists_files_and_directories(vault):
    labels = {e.label for e in vh.vault_files(vault)}
    assert "notes.txt" in labels
    assert "data_in/" in labels


def test_a_directory_has_no_size(vault):
    """A directory's st_size is its entry size, not what is in it — showing
    it invites the reader to add the column up."""
    entry = next(e for e in vh.vault_files(vault) if e.label == "data_in/")
    assert entry.size == "—"


def test_a_missing_vault_is_empty_not_an_error(tmp_path):
    assert vh.vault_files(tmp_path / "never-made") == []


# ============================================================
# One bad entry does not truncate the panel
# ============================================================

def test_a_file_that_cannot_be_stat_ed_is_reported_in_place(vault,
                                                            monkeypatch):
    """THE DEFECT. The Tk try spans the WHOLE loop, so the first failure ends
    the list and the user sees a short vault rather than a broken one."""
    real_stat = Path.stat
    bad = vault / "notes.txt"

    def watched(self, *args, **kwargs):
        if self == bad:
            raise OSError("access denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", watched)
    entries = vh.vault_files(vault)
    labels = [e.label for e in entries]
    assert "notes.txt" in labels
    assert len(labels) >= 3, f"the list was truncated: {labels}"
    broken = next(e for e in entries if e.label == "notes.txt")
    assert broken.problem
    assert broken.size == vh.UNREADABLE


def test_a_bad_mtime_does_not_truncate_the_panel(vault, monkeypatch):
    """On Windows `datetime.fromtimestamp()` raises OSError for a negative
    POSIX timestamp. One file with a bad mtime and the rest of the vault is
    simply not shown."""
    from datetime import datetime

    real = datetime.fromtimestamp
    calls = {"n": 0}

    def watched(timestamp, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("Invalid argument")
        return real(timestamp, *args, **kwargs)

    monkeypatch.setattr("council_core.vault_health.datetime",
                        type("D", (), {"fromtimestamp": staticmethod(watched)}))
    entries = vh.vault_files(vault)
    assert len(entries) >= 3, "one bad mtime truncated the list"
    assert any(e.modified == vh.UNREADABLE for e in entries)


def test_every_entry_keeps_its_row_even_when_all_of_them_are_broken(vault,
                                                                    monkeypatch):
    """A missing row is indistinguishable from a file that is not there. With
    the Tk loop's single try, the FIRST failure ends the list — so a vault
    where nothing can be stat'd shows nothing at all."""
    expected = len(list(vault.iterdir()))

    def _boom(self, *a, **k):
        raise OSError("denied")

    monkeypatch.setattr(Path, "stat", _boom)
    entries = vh.vault_files(vault)
    assert len(entries) == expected
    assert all(e.problem for e in entries)


# ============================================================
# The summary
# ============================================================

@pytest.mark.parametrize("text,expected", [
    ("- [ ] a\n- [x] b\n", (2, 1, 1)),
    ("", (0, 0, 0)),
    ("# heading\njust prose\n", (0, 0, 0)),
    ("- [X] shouty\n", (1, 0, 1)),
    ("  - [ ] indented\n", (1, 1, 0)),
])
def test_wishlist_counts(text, expected):
    assert vh.wishlist_counts(text) == expected


def test_a_heading_is_not_a_wish():
    assert vh.wishlist_counts("# Wishes\n## More\n") == (0, 0, 0)


def test_the_summary_counts_the_wishlist(vault, memory):
    summary = vh.summarise(vault, memory)
    assert (summary.wishlist_total, summary.wishlist_pending,
            summary.wishlist_filled) == (3, 2, 1)


def test_the_summary_measures_the_project_context(vault, memory):
    summary = vh.summarise(vault, memory)
    assert summary.project_exists
    assert summary.project_lines == 2


def test_no_project_context_is_said_rather_than_shown_as_zero(tmp_path):
    summary = vh.summarise(tmp_path, tmp_path / "memory")
    assert not summary.project_exists
    assert "none yet" in "\n".join(summary.lines())


def test_a_wishlist_that_cannot_be_read_is_noted(vault, memory, monkeypatch):
    def _boom(self, *a, **k):
        raise OSError("denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    summary = vh.summarise(vault, memory)
    assert any("could not be read" in note for note in summary.notes)


def test_the_summary_survives_a_vault_that_is_not_there(tmp_path):
    summary = vh.summarise(tmp_path / "nope", tmp_path / "nope" / "memory")
    assert summary.lines()


# ============================================================
# Gathering
# ============================================================

def test_gather_returns_all_three_panels(vault, memory):
    report = vh.gather(vault, memory)
    assert report.memory and report.vault
    assert report.summary.wishlist_total == 3


def test_gather_defaults_the_memory_directory(vault):
    assert vh.gather(vault).memory


# ============================================================
# The Qt tab
# ============================================================

pytest.importorskip("PySide6", reason="the Vault Health tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.vault_health import (VaultHealthActions,  # noqa: E402
                                          VaultHealthTab,
                                          build_vault_health)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def drive(qapp, tab, seconds=8.0):
    deadline = time.time() + seconds
    while tab._busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert not tab._busy, "still reading"


@pytest.fixture
def tab(qapp, vault, memory):
    view = VaultHealthTab(actions=VaultHealthActions(vault, memory),
                          auto_refresh=True)
    drive(qapp, view)
    yield view
    import threading
    deadline = time.time() + 5.0
    while any(t.name.startswith("vault-health") and t.is_alive()
              for t in threading.enumerate()) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    view.deleteLater()
    qapp.processEvents()


def test_the_factory_takes_a_window(qapp):
    view = build_vault_health(None)
    assert isinstance(view, VaultHealthTab)
    view.deleteLater()


def test_both_trees_fill(tab):
    assert tab.memory_tree.topLevelItemCount() >= len(MISSED_BY_TK)
    assert tab.vault_tree.topLevelItemCount() >= 3


def test_the_roles_tk_misses_are_on_screen(tab):
    labels = {tab.memory_tree.topLevelItem(i).text(0)
              for i in range(tab.memory_tree.topLevelItemCount())}
    for role in MISSED_BY_TK:
        assert role in labels


def test_the_summary_pane_is_filled(tab):
    assert "Wishlist" in tab.summary.toPlainText()


def test_the_summary_is_read_only(tab):
    assert tab.summary.isReadOnly()


def test_each_row_carries_its_path(tab):
    item = tab.vault_tree.topLevelItem(0)
    from PySide6.QtCore import Qt
    stored = item.data(0, Qt.ItemDataRole.UserRole)
    assert stored and Path(stored).exists()


def test_the_gather_runs_off_the_gui_thread(qapp, vault, memory):
    """Nothing in the Tk tab does — it runs iterdir, N stat() calls and a
    read_text on the UI thread, including once during startup."""
    import threading
    seen = []
    actions = VaultHealthActions(vault, memory)
    real = actions.gather
    actions.gather = lambda: (seen.append(
        threading.current_thread().name), real())[1]
    view = VaultHealthTab(actions=actions, auto_refresh=True)
    drive(qapp, view)
    assert seen and seen[0] != "MainThread"
    view.deleteLater()


def test_a_second_refresh_while_one_runs_is_skipped(qapp, vault, memory):
    import threading
    release = threading.Event()
    calls = []
    actions = VaultHealthActions(vault, memory)

    def slow():
        calls.append(1)
        release.wait(3.0)
        return vh.Report()

    actions.gather = slow
    view = VaultHealthTab(actions=actions, auto_refresh=False)
    view.refresh()
    for _ in range(200):
        qapp.processEvents()
        if view._busy:
            break
    view.refresh()
    release.set()
    drive(qapp, view)
    assert len(calls) == 1
    view.deleteLater()


def test_a_failing_gather_releases_the_busy_flag(qapp, vault, memory):
    """Otherwise every later refresh is skipped and the tab reads "Reading…"
    for the rest of the session."""
    actions = VaultHealthActions(vault, memory)

    def _boom():
        raise OSError("disk gone")

    actions.gather = _boom
    view = VaultHealthTab(actions=actions, auto_refresh=True)
    deadline = time.time() + 5
    while view._busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert not view._busy
    view.deleteLater()


def test_unreadable_entries_are_counted_on_the_status_line(qapp, vault,
                                                           memory,
                                                           monkeypatch):
    def _boom(self, *a, **k):
        raise OSError("denied")

    monkeypatch.setattr(Path, "stat", _boom)
    view = VaultHealthTab(actions=VaultHealthActions(vault, memory),
                          auto_refresh=True)
    drive(qapp, view)
    assert "unreadable" in view.status.text()
    view.deleteLater()


def test_opening_the_wishlist_when_there_is_none_says_so(qapp, tmp_path):
    """Tk shows a MODAL for this — a dialog to dismiss in exchange for
    information a label could have carried."""
    opened = []
    actions = VaultHealthActions(tmp_path, tmp_path / "memory")
    actions.open_folder = lambda p=None: opened.append(p)
    view = VaultHealthTab(actions=actions, auto_refresh=True)
    drive(qapp, view)
    view.on_open_wishlist()
    assert "No wishlist" in view.status.text()
    assert opened == []
    view.deleteLater()


def test_opening_the_wishlist_opens_the_file(tab):
    opened = []
    tab.actions.open_folder = lambda p=None: opened.append(p)
    tab.on_open_wishlist()
    assert opened and opened[0].name == "wishlist.md"


def test_a_failing_open_is_reported_not_raised(tab):
    def _boom(path=None):
        raise OSError("no handler")

    tab.actions.open_folder = _boom
    tab.on_open_vault()
    assert "Could not open" in tab.status.text()


def test_no_worker_touches_a_widget_directly():
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "tabs" / "vault_health.py").read_text(
        encoding="utf-8")
    body = code_of(source, "refresh")
    assert "_to_ui" in body
    inner = body.split("def work", 1)[1]
    for forbidden in ("self.memory_tree", "self.vault_tree",
                      "self.summary.setPlainText", "self.status.setText"):
        assert forbidden not in inner, "the worker touches a widget"
