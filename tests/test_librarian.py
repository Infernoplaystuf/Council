"""
The Librarian: the vault root, previewed and committed.

Against real files and real git, because every defect here is about what git
and the filesystem actually do — a suite that stubbed subprocess would prove
none of it.

THE DEFECT THAT NAMES THE CLASS
Tk lists `p.name` and looks the file back up through `safe_name()`, which
rewrites every run of characters outside [A-Za-z0-9._-] to "_". The list shows
"Q3 notes.md"; Preview asks for it; read_text looks for "Q3_notes.md"; the user
gets "Vault item not found" for a file they can see on the same screen.

An identifier recovered from display text — the same shape as the chart overlay
resolver, the specialist pin, the job queue and the session list.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import librarian, modes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

HAS_GIT = __import__("shutil").which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git is not on PATH")


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "Q3 notes.md").write_text("quarterly notes", encoding="utf-8")
    (root / "personality_backends.json").write_text("{}", encoding="utf-8")
    (root / ".gitignore").write_text("ignored", encoding="utf-8")
    (root / "subdir").mkdir()
    return root


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True)


def make_repo(root: Path, name: str, *, commit: bool) -> Path:
    repo = root / ".git_clones" / name
    repo.mkdir(parents=True)
    git(["init"], repo)
    (repo / "f.txt").write_text("hi", encoding="utf-8")
    if commit:
        git(["add", "-A"], repo)
        git(["-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-m", "x"], repo)
    return repo


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "librarian.py").read_text(
        encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5"):
        assert toolkit not in source


# ============================================================
# What the Librarian shows
# ============================================================

def test_a_name_with_a_space_survives(vault):
    """THE DEFECT. Tk cannot open a file it lists, because the name makes a
    round trip through safe_name()."""
    found = {p.name for p in librarian.entries(vault)}
    assert "Q3 notes.md" in found
    listed = next(p for p in librarian.entries(vault)
                  if p.name == "Q3 notes.md")
    assert listed.exists(), "the listed path does not point at the file"
    assert librarian.preview(listed) == "quarterly notes"


def test_directories_are_not_listed(vault):
    """A directory in this list has no preview and no size."""
    assert "subdir" not in {p.name for p in librarian.entries(vault)}


def test_dotted_files_are_listed(vault):
    """`personality_backends.json` and its neighbours are exactly what someone
    opens this tab to look at. The Qt Vault tab's walk() skips every dotted
    entry, which is why the rule is written once rather than twice."""
    (vault / ".hidden_notes").write_text("x", encoding="utf-8")
    assert ".hidden_notes" in {p.name for p in librarian.entries(vault)}


def test_gitignore_is_not_listed(vault):
    """Git's own machinery, not a vault item."""
    assert ".gitignore" not in {p.name for p in librarian.entries(vault)}


def test_the_list_is_sorted_case_insensitively(vault):
    for name in ("zebra.txt", "Apple.txt", "mango.txt"):
        (vault / name).write_text("x", encoding="utf-8")
    names = [p.name for p in librarian.entries(vault)]
    assert names == sorted(names, key=str.lower)


def test_a_vault_that_is_not_there_is_empty_not_an_error(tmp_path):
    """The tab opens before the first run has created anything."""
    assert librarian.entries(tmp_path / "never-made") == []


# ============================================================
# Preview
# ============================================================

def test_a_big_file_is_capped(vault):
    """The vault root holds append-only logs that every other reader truncates
    deliberately. Loading one whole into a text widget is a frozen window."""
    big = vault / "verdict_history.jsonl"
    big.write_bytes(b"x" * 500_000)
    text = librarian.preview(big, limit=2_000)
    assert len(text) < 3_000
    assert "truncated" in text


def test_the_whole_file_is_never_read_into_memory(vault, monkeypatch):
    """Slicing after an unbounded read gives the same STRING and still pulls a
    500 MB log into RAM — which is the cost the cap exists to avoid. The first
    version of this only checked the output, so `read()` with no argument
    survived it.
    """
    import io

    asked = []
    real_open = Path.open

    def watched_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        real_read = handle.read

        def read(size=-1):
            asked.append(size)
            return real_read(size)

        handle.read = read
        return handle

    monkeypatch.setattr(Path, "open", watched_open)
    big = vault / "big.log"
    big.write_bytes(b"x" * 100_000)
    librarian.preview(big, limit=1_000)
    assert asked, "the file was not read through Path.open"
    assert all(0 < n <= 1_001 for n in asked), (
        f"read() was asked for {asked} — an unbounded read")


def test_a_small_file_is_not_annotated(vault):
    assert librarian.preview(vault / "Q3 notes.md") == "quarterly notes"


def test_the_cap_says_how_to_see_the_rest(vault):
    big = vault / "big.log"
    big.write_bytes(b"x" * 5_000)
    assert "Open the file" in librarian.preview(big, limit=100)


def test_a_file_that_cannot_be_read_says_so_rather_than_raising(vault):
    """A preview that throws takes the tab with it, on a file the user merely
    double-clicked."""
    text = librarian.preview(vault / "does-not-exist.txt")
    assert "Could not read" in text


def test_binary_content_does_not_raise(vault):
    binary = vault / "thing.bin"
    binary.write_bytes(bytes(range(256)))
    assert isinstance(librarian.preview(binary), str)


# ============================================================
# Committing
# ============================================================

@needs_git
def test_a_first_commit_creates_the_repository(vault):
    result = librarian.commit_all(vault, "first")
    assert result.ok, result.message
    assert (vault / ".git").exists()


@needs_git
def test_the_commit_output_is_carried_through_on_success(vault):
    """It holds the SHA and the files-changed count — the receipt. A port
    written from a summary would drop the line users see today.

    Asserted on the FILES-CHANGED count specifically: the first version
    accepted "commit" anywhere in the message, and the fallback string is
    literally "commit OK.", so it passed with the output dropped.
    """
    result = librarian.commit_all(vault, "first")
    assert "file" in result.message and "changed" in result.message, (
        f"the git output was dropped: {result.message!r}")


@needs_git
def test_nothing_to_commit_is_a_success(vault):
    """`git commit` exits 1 on a clean tree. Tk treats any non-zero return as
    a failure, so pressing Commit twice reports FAIL for a repository that is
    perfectly fine."""
    assert librarian.commit_all(vault, "first").ok
    again = librarian.commit_all(vault, "again")
    assert again.ok
    assert again.nothing_to_commit
    assert "already saved" in again.message


@needs_git
def test_an_empty_message_is_refused_before_git_runs(vault):
    result = librarian.commit_all(vault, "   ")
    assert not result.ok
    assert not (vault / ".git").exists(), "git ran for a message git would "\
                                          "have rejected"


@needs_git
def test_every_git_call_has_a_timeout():
    """Tk has none on any of the three, and `git add -A` over a live vault
    walks .chromadb, conversation_logs and every cloned reference repo."""
    from tests.source_checks import code_of
    source = (ROOT / "council_core" / "librarian.py").read_text(
        encoding="utf-8")
    assert "subprocess.run(" not in source, (
        "a raw subprocess.run bypasses changelog._git's timeout")
    for name in ("commit_all", "_ensure_repo"):
        assert "TimeoutExpired" in code_of(source, name)


@needs_git
def test_a_timeout_during_setup_is_reported(vault, monkeypatch):
    """_ensure_repo's own guard: `git init` can hang too."""
    def _slow(*_a, **_k):
        raise subprocess.TimeoutExpired("git", 20)

    monkeypatch.setattr(librarian, "_git", _slow)
    result = librarian.commit_all(vault, "msg")
    assert not result.ok
    assert "longer than" in result.message


@needs_git
def test_a_timeout_during_the_commit_is_reported(vault, monkeypatch):
    """The guard in `commit_all` itself. The repository already exists here,
    so `_ensure_repo` returns early and cannot catch this on its behalf — the
    first version of this test let it, and the mutation survived."""
    librarian.commit_all(vault, "first")
    assert (vault / ".git").exists(), "premise gone"

    def _slow(*_a, **_k):
        raise subprocess.TimeoutExpired("git", 20)

    monkeypatch.setattr(librarian, "_git", _slow)
    result = librarian.commit_all(vault, "second")
    assert not result.ok
    assert "longer than" in result.message


@needs_git
def test_git_missing_during_setup_is_reported_in_plain_words(vault,
                                                             monkeypatch):
    def _gone(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(librarian, "_git", _gone)
    result = librarian.commit_all(vault, "msg")
    assert not result.ok
    assert "not installed" in result.message


@needs_git
def test_git_vanishing_mid_commit_is_reported(vault, monkeypatch):
    """`commit_all`'s own guard, with the repository already made so
    `_ensure_repo` returns before it can catch this instead."""
    librarian.commit_all(vault, "first")

    def _gone(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(librarian, "_git", _gone)
    result = librarian.commit_all(vault, "second")
    assert not result.ok
    assert "not installed" in result.message


# ============================================================
# Repositories inside the vault
# ============================================================

@needs_git
def test_an_embedded_repo_with_commits_is_reported_as_a_link(vault):
    """`git add -A` records it as a GITLINK — a pointer, not the files — so
    the commit succeeds and the cloned material it looked like it was backing
    up is not in it. The Tk build says nothing."""
    make_repo(vault, "NXWorkshop", commit=True)
    result = librarian.commit_all(vault, "with a clone")
    assert result.ok
    assert [p.name for p in result.linked_repos] == ["NXWorkshop"]
    assert "NOT in this commit" in librarian.describe(result)


@needs_git
def test_an_embedded_repo_with_no_commits_stops_the_add_and_says_which(vault):
    """Worse than a silent link: `git add -A` fails outright with "does not
    have a commit checked out", which says nothing about what to do."""
    make_repo(vault, "half_cloned", commit=False)
    result = librarian.commit_all(vault, "msg")
    assert not result.ok
    assert [p.name for p in result.linked_repos] == ["half_cloned"]
    assert "half_cloned" in librarian.describe(result)


@needs_git
def test_a_clean_vault_reports_no_linked_repos(vault):
    assert librarian.commit_all(vault, "msg").linked_repos == []


def test_the_vault_root_is_not_reported_as_its_own_embedded_repo(vault):
    """It IS a repository after the first commit, and listing it would tell
    the user their vault is not in their vault."""
    (vault / ".git").mkdir()
    assert vault not in librarian.embedded_repos(vault)


def test_describing_a_plain_result_adds_nothing(vault):
    result = librarian.CommitResult(True, "commit OK.")
    assert librarian.describe(result) == "commit OK."


# ============================================================
# Advanced mode
# ============================================================

def test_a_normal_build_is_not_advanced(monkeypatch):
    monkeypatch.delenv("COUNCIL_ADVANCED", raising=False)
    assert modes.advanced([]) is False


def test_the_command_line_flag_turns_it_on(monkeypatch):
    monkeypatch.delenv("COUNCIL_ADVANCED", raising=False)
    assert modes.advanced(["app.py", "--advanced"]) is True


@pytest.mark.parametrize("value", ["1", "true", "YES", " yes "])
def test_the_environment_variable_accepts_what_the_tk_build_accepts(
        value, monkeypatch):
    monkeypatch.setenv("COUNCIL_ADVANCED", value)
    assert modes.advanced([]) is True


def test_a_falsy_value_does_not_turn_it_on(monkeypatch):
    monkeypatch.setenv("COUNCIL_ADVANCED", "no")
    assert modes.advanced([]) is False


def test_the_advanced_tabs_are_not_in_the_default_build(monkeypatch):
    """Registering them unconditionally puts "commit my entire vault to git"
    in front of every user — a behaviour change dressed as a port."""
    from council_qt.tabs import ADVANCED_REGISTRY, REGISTRY, registry
    default_titles = {t for t, _f, _e in REGISTRY}
    for title, _factory, _eager in ADVANCED_REGISTRY:
        assert title not in default_titles
    assert len(registry(True)) == len(REGISTRY) + len(ADVANCED_REGISTRY)
    assert len(registry(False)) == len(REGISTRY)


def test_the_entry_point_asks_before_registering_the_advanced_tabs():
    from tests.source_checks import code_of
    source = (ROOT / "council_qt.py").read_text(encoding="utf-8")
    body = code_of(source, "_register_tabs")
    assert "modes.advanced()" in body


# ============================================================
# The Qt tab
# ============================================================

pytest.importorskip("PySide6", reason="the Librarian tab needs PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

from council_qt.tabs.librarian import (LibrarianActions,  # noqa: E402
                                       LibrarianTab, build_librarian)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def tab(qapp, vault):
    answers = []

    def ask_text(*_a, **_k):
        return answers.pop(0) if answers else None

    view = LibrarianTab(actions=LibrarianActions(vault), ask_text=ask_text)
    view.answers = answers
    yield view
    import threading
    import time
    deadline = time.time() + 5.0
    while any(t.name.startswith("librarian-") and t.is_alive()
              for t in threading.enumerate()) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    view.deleteLater()
    qapp.processEvents()


def pump(qapp, tab, seconds=20.0):
    import time
    deadline = time.time() + seconds
    while tab._busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    assert not tab._busy, "still committing"


def test_the_tab_lists_the_vault(tab):
    names = {tab.files.item(i).text() for i in range(tab.files.count())}
    assert "Q3 notes.md" in names
    assert ".gitignore" not in names


def test_the_factory_takes_a_window(qapp):
    view = build_librarian(None)
    assert isinstance(view, LibrarianTab)
    view.deleteLater()


def test_each_row_carries_its_path_not_just_its_name(tab):
    """Recovering the path from the row's TEXT is the defect this tab exists
    to stop repeating — it is what makes a file with a space unopenable."""
    row = next(i for i in range(tab.files.count())
               if tab.files.item(i).text() == "Q3 notes.md")
    stored = tab.files.item(row).data(Qt.ItemDataRole.UserRole)
    assert stored and Path(stored).exists()
    assert Path(stored).name == "Q3 notes.md"


def test_selecting_a_row_gives_back_a_real_path(tab):
    tab.files.setCurrentRow(0)
    path = tab.selected_path()
    assert path is not None and path.exists()


def test_nothing_selected_gives_no_path(tab):
    assert tab.selected_path() is None


def test_previewing_a_file_with_a_space_shows_its_contents(tab):
    """End to end: the row the user double-clicks opens the file it names."""
    row = next(i for i in range(tab.files.count())
               if tab.files.item(i).text() == "Q3 notes.md")
    tab._preview_item(tab.files.item(row))
    assert "quarterly notes" in tab._preview_text.toPlainText()


def test_the_preview_dialog_is_reused(tab, vault):
    """Tk builds a fresh Toplevel per double-click and keeps no reference, so
    ten previews leave ten 800x600 windows for the life of the session."""
    (vault / "second.txt").write_text("second file", encoding="utf-8")
    tab.refresh()
    for i in range(tab.files.count()):
        tab._preview_item(tab.files.item(i))
    panes = tab.findChildren(QPlainTextEdit)
    assert len(panes) == 1, f"{len(panes)} preview widgets left behind"


def test_the_preview_dialog_shows_which_file_it_is(tab):
    row = next(i for i in range(tab.files.count())
               if tab.files.item(i).text() == "Q3 notes.md")
    tab._preview_item(tab.files.item(row))
    assert tab._preview_dialog.windowTitle() == "Q3 notes.md"


def test_the_preview_is_read_only(tab):
    """It is a viewer. An editable pane that discards what you type is worse
    than one you cannot type in."""
    tab._preview_item(tab.files.item(0))
    assert tab._preview_text.isReadOnly()


def test_refreshing_re_reads_the_vault(tab, vault):
    before = tab.files.count()
    (vault / "new_thing.txt").write_text("x", encoding="utf-8")
    tab.refresh()
    assert tab.files.count() == before + 1


@needs_git
def test_committing_runs_off_the_gui_thread(tab, qapp):
    """Three git calls back to back over a vault holding .chromadb and every
    cloned reference repo. On the UI thread the window is gone for the
    duration."""
    import threading
    seen = []
    real = tab.actions.commit

    def watched(message):
        seen.append(threading.current_thread().name)
        return real(message)

    tab.actions.commit = watched
    tab.answers.append("a message")
    tab.on_commit()
    pump(qapp, tab)
    assert seen and seen[0] != "MainThread"


@needs_git
def test_committing_reports_the_result(tab, qapp):
    tab.answers.append("first commit")
    tab.on_commit()
    pump(qapp, tab)
    assert tab.status.text()
    assert "FAIL" not in tab.status.text().upper()


@needs_git
def test_a_second_commit_is_reported_as_success(tab, qapp):
    tab.answers.append("first")
    tab.on_commit()
    pump(qapp, tab)
    tab.answers.append("second")
    tab.on_commit()
    pump(qapp, tab)
    assert "already saved" in tab.status.text()


def test_cancelling_the_message_commits_nothing(tab, qapp):
    """No answer queued -> the dialog cancels."""
    called = []
    tab.actions.commit = lambda m: called.append(m)
    tab.on_commit()
    qapp.processEvents()
    assert called == []


@needs_git
def test_the_commit_button_is_disabled_for_the_duration(tab, qapp):
    """An ignored click reads as a broken button, and a second git process on
    the same repository is worse than that."""
    import threading
    release = threading.Event()

    def slow(_message):
        release.wait(3.0)
        return librarian.CommitResult(True, "done")

    tab.actions.commit = slow
    tab.answers.append("msg")
    tab.on_commit()
    for _ in range(200):
        qapp.processEvents()
        if not tab.commit_btn.isEnabled():
            break
    assert not tab.commit_btn.isEnabled()
    release.set()
    pump(qapp, tab)
    assert tab.commit_btn.isEnabled()


def test_a_second_commit_while_one_runs_is_refused(tab, qapp):
    import threading
    release = threading.Event()
    calls = []

    def slow(_message):
        calls.append(1)
        release.wait(3.0)
        return librarian.CommitResult(True, "done")

    tab.actions.commit = slow
    tab.answers.extend(["one", "two"])
    tab.on_commit()
    for _ in range(200):
        qapp.processEvents()
        if tab._busy:
            break
    tab.on_commit()
    assert "already running" in tab.status.text()
    release.set()
    pump(qapp, tab)
    assert len(calls) == 1


def test_a_failing_open_folder_is_reported_not_raised(tab):
    """The Qt VaultActions.open_folder has no try/except of its own, so a
    missing vault raises straight out of the button handler."""
    def _boom():
        raise OSError("no such folder")

    tab.actions.open_folder = _boom
    tab.on_open_folder()
    assert "Could not open" in tab.status.text()


def test_no_worker_touches_a_widget_directly():
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "tabs" / "librarian.py").read_text(
        encoding="utf-8")
    body = code_of(source, "on_commit")
    assert "_to_ui" in body
    inner = body.split("def work", 1)[1].split("def show", 1)[0]
    for forbidden in ("self.status.setText", "self.commit_btn", "self.files"):
        assert forbidden not in inner, "the worker touches a widget"
