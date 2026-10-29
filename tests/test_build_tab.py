"""The Build tab, step 1: files, an editor with colours, and Run.

council_core.code_highlight (spans over the whole text), council_core.
build_files (list, open, save, new, run) and the Qt tab and editor, offscreen.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import build_files as bf  # noqa: E402
from council_core import code_highlight as ch  # noqa: E402

pytest.importorskip("pygments")


# ── colours ──────────────────────────────────────────────────────────────
def _kinds(text, name="x.py"):
    return {text[s:s + n]: k for s, n, k in ch.spans(text, name)}


def test_python_is_coloured_by_kind():
    got = _kinds("def run(n):\n    # count\n    return len('ab') + 42 or None\n")
    assert got["def"] == "keyword" and got["run"] == "definition"
    assert got["# count"] == "comment" and got["'ab'"] == "string"
    assert got["42"] == "number" and got["len"] == "builtin"
    assert got["None"] == "constant" and got["or"] == "keyword"
    assert _kinds("except OSError:\n    pass\n")["OSError"] == "builtin"


def test_a_string_over_several_lines_is_one_string():
    """Lexed line by line, the docstring's second line would be code."""
    text = 'x = 1\n"""first\nimport os\nlast"""\ny = 2\n'
    strings = [(s, n) for s, n, k in ch.spans(text, "a.py") if k == "string"]
    assert len(strings) == 1
    s, n = strings[0]
    assert text[s:s + n] == '"""first\nimport os\nlast"""'
    assert "import" not in _kinds(text)


def test_offsets_are_the_texts_own():
    """get_tokens would strip the leading blank lines and shift every span."""
    text = "\n\n\tdef f():\n\t\tpass\n"
    for s, n, kind in ch.spans(text, "a.py"):
        assert text[s:s + n].strip(), (s, n, kind)
    assert _kinds(text)["def"] == "keyword"


def test_one_lines_share_is_clipped_to_it():
    text = 'a = """one\ntwo"""\n'
    all_spans = ch.spans(text, "a.py")
    ends = [s + n for s, n, _k in all_spans]
    line2 = text.index("two")
    assert ch.in_range(all_spans, ends, line2, len('two"""')) == [(0, 6, "string")]


def test_unknown_types_and_huge_files_are_plain():
    assert ch.spans("whatever", "notes.zzzunknown") == []
    assert ch.spans("x = 1\n" * (ch.MAX_CHARS // 6 + 10), "big.py") == []
    assert ch.language("a.py") == "Python" and ch.language("a.zzz") == "Plain text"


# ── files ────────────────────────────────────────────────────────────────
def test_the_listing_puts_folders_first_and_skips_caches(tmp_path):
    for d in ("zeta", "Alpha", ".git", "__pycache__", "node_modules"):
        (tmp_path / d).mkdir()
    for f in ("b.py", "A.txt"):
        (tmp_path / f).write_text("", encoding="utf-8")
    got = bf.list_dir(tmp_path)
    assert [e.name for e in got.entries] == ["Alpha", "zeta", "A.txt", "b.py"]
    assert not got.truncated and not got.error
    assert bf.list_dir(tmp_path, limit=2).truncated
    assert bf.list_dir(tmp_path / "missing").error


def test_crlf_and_the_encoding_survive_a_save(tmp_path):
    f = tmp_path / "w.py"
    f.write_bytes("caf\xe9 = 1\r\nb = 2\r\n".encode("cp1252"))
    opened = bf.open_text(f)
    assert opened.text == "caf\xe9 = 1\nb = 2\n" and opened.newline == "\r\n"
    assert opened.encoding == "cp1252"
    bf.save_text(opened, opened.text.replace("2", "3"))
    assert f.read_bytes() == "caf\xe9 = 1\r\nb = 3\r\n".encode("cp1252")
    assert [p.name for p in tmp_path.iterdir()] == ["w.py"]        # no temp left


def test_a_bom_is_kept(tmp_path):
    f = tmp_path / "b.py"
    f.write_bytes(b"\xef\xbb\xbfx = 1\n")
    opened = bf.open_text(f)
    assert opened.text == "x = 1\n"
    bf.save_text(opened, "x = 2\n")
    assert f.read_bytes() == b"\xef\xbb\xbfx = 2\n"


def test_binary_and_huge_files_are_not_opened(tmp_path, monkeypatch):
    b = tmp_path / "a.bin"
    b.write_bytes(b"\x89PNG\x00\x00")
    with pytest.raises(bf.FileProblem, match="not a text file"):
        bf.open_text(b)
    monkeypatch.setattr(bf, "MAX_OPEN_BYTES", 10)
    t = tmp_path / "t.txt"
    t.write_text("x" * 20, encoding="utf-8")
    with pytest.raises(bf.FileProblem, match="editor opens"):
        bf.open_text(t)


def test_a_file_changed_on_disk_is_not_saved_over(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("a = 1\n", encoding="utf-8")
    opened = bf.open_text(f)
    time.sleep(0.01)
    f.write_text("a = 1\nmine = 'theirs'\n", encoding="utf-8")
    with pytest.raises(bf.ChangedOnDisk):
        bf.save_text(opened, "a = 2\n")
    assert "theirs" in f.read_text(encoding="utf-8")
    again = bf.save_text(opened, "a = 2\n", force=True)
    assert f.read_text(encoding="utf-8") == "a = 2\n"
    bf.save_text(again, "a = 3\n")                       # the new stamp is kept
    assert f.read_text(encoding="utf-8") == "a = 3\n"


def test_a_new_file_never_replaces_one_or_leaves_the_folder(tmp_path):
    made = bf.new_file(tmp_path, "pkg/util.py")
    assert made == (tmp_path / "pkg" / "util.py").resolve() and made.read_text() == ""
    made.write_text("keep", encoding="utf-8")
    with pytest.raises(bf.FileProblem, match="already exists"):
        bf.new_file(tmp_path, "pkg/util.py")
    assert made.read_text() == "keep"
    with pytest.raises(bf.FileProblem, match="outside"):
        bf.new_file(tmp_path / "pkg", "../../escape.py")
    with pytest.raises(bf.FileProblem):
        bf.new_file(tmp_path, "  ")


def test_run_streams_the_output_from_the_files_own_folder(tmp_path):
    (tmp_path / "helper.py").write_text("WORD = 'from-helper'\n", encoding="utf-8")
    f = tmp_path / "main.py"
    f.write_text("import sys, helper\nprint(helper.WORD)\n"
                 "print('to-err', file=sys.stderr)\nraise SystemExit(3)\n",
                 encoding="utf-8")
    lines = []
    out = bf.run_file(sys.executable, f, lines.append)
    assert lines == ["from-helper", "to-err"] and out.returncode == 3
    assert "exited with code 3" in out.summary("main.py")


def test_stop_ends_a_run(tmp_path):
    f = tmp_path / "slow.py"
    f.write_text("import time\nprint('started')\ntime.sleep(60)\n", encoding="utf-8")
    stop = threading.Event()
    lines = []
    threading.Timer(1.0, stop.set).start()
    out = bf.run_file(sys.executable, f, lines.append, stop)
    assert out.stopped and out.seconds < 30 and lines == ["started"]
    assert "stopped" in out.summary("slow.py")


def test_a_missing_python_is_said(tmp_path):
    out = bf.run_file(str(tmp_path / "no-python.exe"), tmp_path / "x.py", print)
    assert out.error and "did not start" in out.summary("x.py")


# ── the Qt tab ───────────────────────────────────────────────────────────
pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.build import BuildActions, BuildTab, build_build  # noqa: E402
from council_qt.widgets.code_editor import CodeEditor, FindBar  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def pump(qapp, until, seconds=10.0):
    deadline = time.time() + seconds
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert until(), "timed out"


class Answers:
    """The user's side: yes/no answers and names, and what was asked."""

    def __init__(self, yes=True, name=None, folder=""):
        self.yes, self.name, self.folder = yes, name, folder
        self.asked = []

    def confirm(self, title, text):
        self.asked.append(title)
        return self.yes


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("VALUE = 7\n", encoding="utf-8")
    (root / "main.py").write_text(
        "from pkg.mod import VALUE\nprint('value', VALUE)\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    return root


def make_tab(tmp_path, folder=None, answers=None, window=None):
    answers = answers or Answers()
    actions = BuildActions(
        vault_dir=tmp_path / "vault", state_file=tmp_path / "state.json",
        unsaved_dir=tmp_path / "unsaved", confirm=answers.confirm,
        ask_folder=lambda start: answers.folder,
        ask_name=lambda title, label: answers.name)
    if folder is not None:
        actions.save_state({"folder": str(folder)})
    return BuildTab(window, actions), answers


def tree_names(item):
    return [item.child(i).text(0) for i in range(item.childCount())]


def test_the_tab_is_registered_beside_code():
    from council_qt import tabs
    titles = [t for t, _f, _e in tabs.registry()]
    assert "🧱 Build" in titles
    assert titles.index("🧱 Build") + 1 == titles.index("💻 Code")
    assert tabs.build_build is build_build


def test_it_opens_the_scratch_folder_then_remembers_a_chosen_one(qapp, tmp_path, project):
    tab, answers = make_tab(tmp_path)
    assert tab.root == tmp_path / "vault" / "workspace" and tab.root.is_dir()
    answers.folder = str(project)
    tab.on_open_folder()
    assert tab.root == project
    again, _ = make_tab(tmp_path)
    assert again.root == project
    for t in (tab, again):
        t.deleteLater()


def test_the_tree_lists_and_opens_folders_as_they_are_expanded(qapp, tmp_path, project):
    tab, _ = make_tab(tmp_path, project)
    top = tab.tree.invisibleRootItem()
    assert tree_names(top) == ["pkg/", "main.py"]               # no __pycache__
    pkg = top.child(0)
    assert tree_names(pkg) == ["…"]
    pkg.setExpanded(True)
    qapp.processEvents()
    assert tree_names(pkg) == ["mod.py"]
    tab._activated(pkg.child(0))
    assert tab.current().path == project / "pkg" / "mod.py"
    tab.deleteLater()


def test_an_open_file_is_coloured(qapp, tmp_path, project):
    tab, _ = make_tab(tmp_path, project)
    doc = tab.open_file(project / "main.py")
    ed = doc.editor
    kinds = {ed.toPlainText()[s:s + n]: k for s, n, k in ed.highlighter.spans()}
    assert kinds["from"] == "keyword" and kinds["'value'"] == "string"
    formats = ed.document().firstBlock().layout().formats()
    assert formats and formats[0].start == 0 and formats[0].length == 4      # "from"
    assert "Python" in tab.status.text()
    assert tab.open_file(project / "main.py") is doc                        # one tab per file
    assert tab.editors.count() == 1
    tab.deleteLater()


def test_typing_recolours_after_a_pause(qapp, tmp_path, project):
    tab, _ = make_tab(tmp_path, project)
    ed = tab.open_file(project / "main.py").editor
    ed.moveCursor(QTextCursor.MoveOperation.End)
    ed.insertPlainText("# done\n")
    pump(qapp, lambda: any(k == "comment" for _s, _n, k in ed.highlighter.spans()))
    tab.deleteLater()


def test_an_edit_marks_the_tab_and_save_writes_it(qapp, tmp_path, project):
    tab, _ = make_tab(tmp_path, project)
    doc = tab.open_file(project / "main.py")
    doc.editor.moveCursor(QTextCursor.MoveOperation.End)
    doc.editor.insertPlainText("print('more')\n")
    assert tab.editors.tabText(0) == "main.py •"
    assert tab.save_current()
    assert tab.editors.tabText(0) == "main.py"
    assert project.joinpath("main.py").read_text(encoding="utf-8").endswith("print('more')\n")
    tab.deleteLater()


def test_a_file_changed_on_disk_is_saved_over_only_when_the_user_says(qapp, tmp_path, project):
    tab, answers = make_tab(tmp_path, project, Answers(yes=False))
    doc = tab.open_file(project / "main.py")
    time.sleep(0.01)
    project.joinpath("main.py").write_text("THEIRS = 1\n", encoding="utf-8")
    doc.editor.insertPlainText("# mine\n")
    assert not tab.save_current()
    assert answers.asked == ["Changed on disk"]
    assert project.joinpath("main.py").read_text(encoding="utf-8") == "THEIRS = 1\n"
    answers.yes = True
    assert tab.save_current()
    assert project.joinpath("main.py").read_text(encoding="utf-8").startswith("# mine")
    tab.deleteLater()


def test_closing_an_unsaved_tab_asks(qapp, tmp_path, project):
    tab, answers = make_tab(tmp_path, project, Answers(yes=False))
    doc = tab.open_file(project / "main.py")
    doc.editor.insertPlainText("x = 1\n")
    assert not tab.close_tab(0) and tab.editors.count() == 1
    answers.yes = True
    assert tab.close_tab(0) and tab.editors.count() == 0 and not tab.docs
    assert answers.asked == ["Unsaved changes", "Unsaved changes"]
    tab.deleteLater()


def test_the_councils_close_keeps_unsaved_edits_aside(qapp, tmp_path, project):
    class Window:
        close_hooks = []
    window = Window()
    tab, _ = make_tab(tmp_path, project, window=window)
    doc = tab.open_file(project / "main.py")
    doc.editor.insertPlainText("unsaved = True\n")
    for hook in window.close_hooks:
        hook()
    kept = list((tmp_path / "unsaved").iterdir())
    assert len(kept) == 1 and kept[0].name.endswith("-main.py")
    assert "unsaved = True" in kept[0].read_text(encoding="utf-8")
    assert "unsaved" not in project.joinpath("main.py").read_text(encoding="utf-8")
    later, _ = make_tab(tmp_path, project)
    assert "kept in" in later.status.text()
    for t in (tab, later):
        t.deleteLater()


def test_new_file_makes_and_opens_it(qapp, tmp_path, project):
    tab, answers = make_tab(tmp_path, project, Answers(name="tools/new.py"))
    tab.on_new_file()
    assert (project / "tools" / "new.py").is_file()
    assert tab.current().path.name == "new.py"
    assert "tools/" in tree_names(tab.tree.invisibleRootItem())
    answers.name = "main.py"
    tab.on_new_file()
    assert "already exists" in tab.status.text()
    tab.deleteLater()


def test_run_saves_and_runs_the_file_in_its_folder(qapp, tmp_path, project):
    tab, _ = make_tab(tmp_path, project)
    doc = tab.open_file(project / "main.py")
    doc.editor.moveCursor(QTextCursor.MoveOperation.End)
    doc.editor.insertPlainText("print('edited')\n")
    tab.on_run()
    pump(qapp, lambda: not tab.is_running() and "exited" in tab.output.toPlainText())
    out = tab.output.toPlainText()
    assert "value 7" in out and "edited" in out and "exited with code 0" in out
    assert not doc.modified()
    tab.deleteLater()


def test_stop_ends_a_running_file(qapp, tmp_path, project):
    tab, _ = make_tab(tmp_path, project)
    project.joinpath("slow.py").write_text(
        "import time\nprint('going', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
    tab.open_file(project / "slow.py")
    tab.on_run()
    assert not tab.run_btn.isEnabled() and tab.stop_btn.isEnabled()
    pump(qapp, lambda: "going" in tab.output.toPlainText())
    tab.on_stop()
    pump(qapp, lambda: not tab.is_running())
    assert "stopped" in tab.output.toPlainText() and tab.run_btn.isEnabled()
    tab.deleteLater()


def test_risky_code_runs_only_when_the_user_agrees(qapp, tmp_path, project):
    tab, answers = make_tab(tmp_path, project, Answers(yes=False))
    project.joinpath("risky.py").write_text("import os\nos.system('echo hi')\n",
                                            encoding="utf-8")
    tab.open_file(project / "risky.py")
    tab.on_run()
    assert answers.asked == ["Confirm code execution"] and not tab.is_running()
    assert "[Cancelled]" in tab.output.toPlainText()
    tab.deleteLater()


def test_only_python_files_run(qapp, tmp_path, project):
    tab, _ = make_tab(tmp_path, project)
    project.joinpath("notes.md").write_text("# notes\n", encoding="utf-8")
    tab.open_file(project / "notes.md")
    tab.on_run()
    assert "not a Python file" in tab.status.text() and not tab.is_running()
    tab.deleteLater()


# ── the editor ───────────────────────────────────────────────────────────
@pytest.fixture
def editor(qapp):
    ed = CodeEditor(filename="a.py")
    yield ed
    ed.deleteLater()


def _select_all(ed):
    cur = ed.textCursor()
    cur.select(QTextCursor.SelectionType.Document)
    ed.setTextCursor(cur)


def test_enter_keeps_the_indent_and_adds_one_after_a_colon(editor):
    editor.setPlainText("    if x:")
    editor.moveCursor(QTextCursor.MoveOperation.End)
    editor.new_line()
    editor.insertPlainText("y = 1")
    editor.new_line()
    assert editor.toPlainText() == "    if x:\n        y = 1\n        "


def test_tab_and_shift_tab_move_the_selected_lines(editor):
    editor.setPlainText("a = 1\n\nb = 2")
    _select_all(editor)
    editor.indent_lines()
    assert editor.toPlainText() == "    a = 1\n\n    b = 2"
    editor.dedent_lines()
    assert editor.toPlainText() == "a = 1\n\nb = 2"
    editor.undo()                                              # one step each
    assert editor.toPlainText() == "    a = 1\n\n    b = 2"


def test_ctrl_slash_comments_and_uncomments(editor):
    editor.setPlainText("    a = 1\n    b = 2")
    _select_all(editor)
    editor.toggle_comment()
    assert editor.toPlainText() == "    # a = 1\n    # b = 2"
    editor.toggle_comment()
    assert editor.toPlainText() == "    a = 1\n    b = 2"


def test_find_wraps_and_replace_all_is_one_undo(editor, qapp):
    bar = FindBar()
    bar.attach(editor)
    editor.setPlainText("cat dog cat\ncat")
    bar.find_box.setText("cat")
    assert bar.find_next() and bar.find_next() and bar.find_next()
    assert bar.find_next()                                    # wrapped to the top
    assert editor.textCursor().selectionStart() == 0
    bar.replace_box.setText("cats")
    assert bar.replace_all() == 3
    assert editor.toPlainText() == "cats dog cats\ncats"
    editor.undo()
    assert editor.toPlainText() == "cat dog cat\ncat"
    bar.find_box.setText("bird")
    assert not bar.find_next() and bar.result.text() == "Not found"
    bar.deleteLater()


def test_the_gutter_fits_the_line_count(editor):
    small = editor.gutter_width()
    editor.setPlainText("\n" * 12000)
    assert editor.gutter_width() > small
