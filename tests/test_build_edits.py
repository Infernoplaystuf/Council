"""The Build tab, step 3: Edit mode.

council_core.build_edits (edit blocks, matching, the plan card's mark),
council_core.build_session (a branch per session, a commit per turn, Undo,
the grouped review, Merge) and the chat and tab offscreen, with a fake model.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import build_edits as be  # noqa: E402
from council_core import build_session as bs  # noqa: E402
from council_core import worktree as wt  # noqa: E402


def block(path, search, replace, reason=""):
    why = f"Why: {reason}\n" if reason else ""
    return f"{why}{path}\n<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"


# ── edit blocks ──────────────────────────────────────────────────────────
def test_blocks_are_read_with_their_path_and_why():
    reply = ("I will rename it.\n\n" + "Why: clearer name\n`app/main.py`\n```python\n"
             "<<<<<<< SEARCH\nold()\n=======\nnew()\n>>>>>>> REPLACE\n```\n\n"
             "<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n"
             "app/x.py\n<<<<<<< SEARCH\nstopped half")
    got = be.parse_blocks(reply, default_path="open.py")
    assert [(b.path, b.search, b.replace, b.reason, b.index) for b in got] == [
        ("app/main.py", "old()", "new()", "clearer name", 1),
        ("open.py", "a", "b", "", 2)]
    assert be.prose(reply).startswith("I will rename it.")
    assert "SEARCH" not in be.prose(reply) and "clearer name" not in be.prose(reply)


def test_line_numbers_copied_from_the_ask_view_are_dropped():
    reply = block("a.py", " 3| x = 1\n 4| y = 2", " 3| x = 10\n 4| y = 2")
    b = be.parse_blocks(reply)[0]
    assert (b.search, b.replace) == ("x = 1\ny = 2", "x = 10\ny = 2")


TEXT = "def f():\n    x = 1\n    return x\n\n\ndef g():\n    x = 1\n    return x + 1\n"


def test_an_exact_unique_match_is_used():
    e = be.resolve(TEXT, be.Block("a.py", "    x = 1\n    return x\n", "    return 2\n"))
    assert e.ok and e.how == "exact"
    assert TEXT[:e.start] + e.new + TEXT[e.end:] == TEXT.replace(
        "    x = 1\n    return x\n\n", "    return 2\n\n", 1)


def test_a_match_in_two_places_is_refused_with_the_lines():
    e = be.resolve(TEXT, be.Block("a.py", "    x = 1", "    x = 2"))
    assert not e.ok and "2 times (lines 2, 7)" in e.error


def test_trailing_spaces_and_an_indent_shift_still_match():
    e = be.resolve(TEXT, be.Block("a.py", "    x = 1   \n    return x + 1", "    return 9"))
    assert e.ok and e.how == "spaces"
    e = be.resolve(TEXT, be.Block("a.py", "x = 1\nreturn x + 1", "y = 3\nreturn y"))
    assert e.ok and e.how == "indent" and e.new == "    y = 3\n    return y"
    assert TEXT[:e.start].endswith("def g():\n")


def test_text_that_is_not_there_is_refused_with_the_closest():
    e = be.resolve(TEXT, be.Block("a.py", "def h():\n    x = 1", "pass"))
    assert not e.ok and "is not in a.py" in e.error and "closest text is at line" in e.error


def test_the_proposal_refuses_outside_paths_overlaps_and_unreadable_files(tmp_path):
    files = {"a.py": TEXT, "bin.dat": None}

    def read(rel):
        if rel == "bin.dat":
            raise ValueError("it is a binary file")
        return files.get(rel)
    blocks = [be.Block("../etc/passwd", "x", "y", index=1),
              be.Block("a.py", "def f():\n    x = 1", "def f():\n    x = 5", index=2),
              be.Block("a.py", "    x = 1\n    return x\n", "", index=3),
              be.Block("new/b.py", "", "print('b')", index=4),
              be.Block("bin.dat", "x", "y", index=5)]
    prop = be.propose(blocks, tmp_path, read)
    by = {e.block.index: e for _c, e in prop.edits()}
    assert "inside the workspace folder" in by[1].error
    assert by[2].ok and "overlaps edit 2" in by[3].error
    assert by[4].ok and "binary" in by[5].error
    new = next(c for c in prop.changes if c.path == "new/b.py")
    assert not new.exists and new.diff(by[4]).startswith("--- /dev/null")


def test_only_accepted_edits_are_in_the_result():
    prop = be.propose([be.Block("a.py", "    return x\n\n", "    return -x\n\n", index=1),
                       be.Block("a.py", "    return x + 1", "    return x + 2", index=2)],
                      Path("."), lambda rel: TEXT)
    fc = prop.changes[0]
    a, b = fc.edits
    assert fc.after() == TEXT                       # nothing accepted yet
    b.accepted, a.accepted = True, False
    assert fc.after() == TEXT.replace("x + 1", "x + 2")
    a.accepted = True
    assert "return -x" in fc.after() and "x + 2" in fc.after()
    assert "-    return x + 1\n+    return x + 2" in fc.diff(b)


def test_enclosing_names_the_innermost_def():
    src = "class A:\n    def m(self):\n        return 1\n\nX = 1\n"
    assert be.enclosing(src, 3) == "A.m" and be.enclosing(src, 5) == ""


# ── the plan card ────────────────────────────────────────────────────────
PLAN = ("I need two files.\n\nPLAN\nFiles: app.py, util.py, notes.py (new)\n"
        "1. call `load_rows()` from `main()`\n2. add `save_rows()` to util.py\n"
        "{questions}CONFIDENCE: {pct}% — the column names\nEND PLAN\n")


def test_a_plan_is_read():
    p = be.parse_plan(PLAN.format(questions="Questions:\n- Which sheet?\n", pct=85))
    assert p.files == ["app.py", "util.py", "notes.py (new)"]
    assert p.steps[0].startswith("1. call") and p.questions == ["Which sheet?"]
    assert p.confidence == 85 and p.unsure == "the column names"
    assert be.parse_plan("no plan here") is None
    assert "PLAN" not in be.prose(PLAN.format(questions="", pct=85))


def test_the_mark_starts_from_the_model_and_council_checks_lower_it():
    def mark(pct, questions="", have=("app.py", "util.py"), names=("load_rows", "main")):
        p = be.parse_plan(PLAN.format(questions=questions, pct=pct))
        return be.mark(p, lambda f: f in have, lambda n: n in names)
    assert mark(85)[0] == be.AMBER                 # save_rows is not in the code
    level, why = mark(85, names=("load_rows", "main", "save_rows"))
    assert level == be.GREEN and "85% sure" in why[0]
    level, why = mark(85, have=("app.py",), names=("load_rows", "main", "save_rows"))
    assert level == be.AMBER and "Not in the folder: util.py." in why
    level, why = mark(90, names=())
    assert level == be.AMBER and any("Not in the code" in w for w in why)
    assert mark(95, "Questions:\n- Which sheet?\n",
                names=("load_rows", "main", "save_rows"))[0] == be.AMBER
    assert mark(30, names=("load_rows", "main", "save_rows"))[0] == be.RED
    assert mark(60, have=(), names=())[0] == be.RED


# ── sessions ─────────────────────────────────────────────────────────────
def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text(TEXT, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "start")
    return root


def test_a_session_needs_a_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(wt.WorktreeError, match="not a git repository"):
        bs.Session.start(tmp_path / "vault", plain)


def test_each_turn_is_one_commit_and_undo_reverts_it(tmp_path, repo):
    vault = tmp_path / "vault"
    s = bs.Session.start(vault, repo)
    assert s.tree.is_dir() and ".council_build" in s.tree.parts
    (s.tree / "app.py").write_text(TEXT + "# mine\n", encoding="utf-8")   # by hand
    s.commit_own_edits()
    text = (s.tree / "app.py").read_text(encoding="utf-8").replace("x + 1", "x + 2")
    (s.tree / "app.py").write_text(text, encoding="utf-8")
    rec = bs.edit_record("app.py", text, 8, "    return x + 2", "    return x + 1", "two")
    turn = s.commit_turn("make g return x + 2", [rec])
    assert rec.where == "g" and turn.number == 1
    assert s.job.log()[0].endswith("Turn 1: make g return x + 2")
    assert s.job.log()[1].endswith("Your edits")
    assert (repo / "app.py").read_text(encoding="utf-8") == TEXT      # untouched
    again = bs.Session.load(vault, s.id)
    assert again is not None and again.turns[0].commit == turn.commit
    s.undo_turn()
    assert (s.tree / "app.py").read_text(encoding="utf-8") == TEXT + "# mine\n"
    assert s.last_turn() is None and bs.Session.load(vault, s.id).turns[0].undone
    with pytest.raises(wt.WorktreeError, match="No turn to undo"):
        s.undo_turn()
    s.end()
    assert not s.tree.exists() and bs.Session.load(vault, s.id) is None
    assert "council/" not in git(repo, "branch")


def test_the_review_groups_by_function_with_warnings_first(tmp_path, repo):
    s = bs.Session.start(tmp_path / "vault", repo)
    text = TEXT.replace("    return x\n\n", "    return x * 2\n\n").replace(
        "return x + 1", "return x + undefined_name")
    (s.tree / "app.py").write_text(text, encoding="utf-8")
    s.commit_turn("double f", [bs.edit_record("app.py", text, 3, "", "", "double it"),
                               bs.edit_record("app.py", text, 8, "", "", "use the name")])
    (s.tree / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
    s.commit_turn("add extra", [bs.edit_record("extra.py", "VALUE = 1\n", 1, "VALUE = 1", "",
                                               "a constant")])
    groups = s.review()
    titles = [g.title() for g in groups]
    assert titles[0] == "app.py · f" and groups[0].warnings      # the file's findings
    assert "undefined_name" in " ".join(groups[0].warnings)
    assert set(titles) == {"app.py · f", "app.py · g", "extra.py · top level"}
    g = next(g for g in groups if g.where == "g")
    assert g.summary() == "Changed in `g` (+1 −1 lines): use the name"
    new = next(g for g in groups if g.path == "extra.py")
    assert new.summary().startswith("New file") and "a constant" in new.summary()
    md = bs.review_markdown(groups)
    assert md.startswith("**3 changes in 2 files** · 1 with warnings") and "(ask:2)" in md
    s.end()


def test_merge_is_refused_over_the_users_work_then_lands(tmp_path, repo):
    s = bs.Session.start(tmp_path / "vault", repo)
    (s.tree / "app.py").write_text(TEXT.replace("x + 1", "x + 3"), encoding="utf-8")
    s.commit_turn("three", [])
    (repo / "notes.txt").write_text("mine", encoding="utf-8")       # not committed
    with pytest.raises(wt.WorktreeError, match="uncommitted changes"):
        s.merge()
    assert s.tree.is_dir()
    (repo / "notes.txt").unlink()
    s.merge()
    assert "x + 3" in (repo / "app.py").read_text(encoding="utf-8")
    assert not s.tree.exists()


# ── the chat and the tab ─────────────────────────────────────────────────
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.build import BuildActions, BuildTab  # noqa: E402
from council_qt.widgets.build_chat import GO_TEXT  # noqa: E402


class Script:
    """A fake model that answers with the next reply in a list."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, messages, on_token=None, should_stop=None):
        self.seen.append(messages)
        reply = self.replies.pop(0) if self.replies else "Done."
        if on_token:
            on_token(reply)
        return reply


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


def make_tab(tmp_path, folder, model, review=None, yes=True):
    actions = BuildActions(
        vault_dir=tmp_path / "vault", state_file=tmp_path / "state.json",
        unsaved_dir=tmp_path / "unsaved", confirm=lambda t, x: yes,
        ask_folder=lambda start: "", ask_name=lambda t, l: None, chat=model,
        review=review or (lambda md: ("cancel", None)))
    if folder is not None:
        actions.save_state({"folder": str(folder)})
    tab = BuildTab(None, actions)
    tab.chat_panel.set_mode("edit")
    return tab


def ask(qapp, tab, text):
    tab.chat_panel.input.setPlainText(text)
    tab.chat_panel.send()
    pump(qapp, lambda: not tab.chat_panel.is_busy())


@pytest.fixture
def plain(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "app.py").write_text(TEXT, encoding="utf-8")
    (root / "util.py").write_text("def save_rows():\n    pass\n", encoding="utf-8")
    return root


def test_edits_are_shown_accepted_one_by_one_and_go_into_the_editor(qapp, tmp_path, plain):
    model = Script("Two changes.\n\n" + block("app.py", "    return x + 1", "    return x + 5", "five")
                   + "\n" + block("app.py", "def f():", "def first():", "rename"))
    tab = make_tab(tmp_path, plain, model)
    doc = tab.open_file(plain / "app.py")
    ask(qapp, tab, "return five from g")
    panel = tab.chat_panel
    shown = model.seen[0][-1]["content"]
    assert "Request: return five from g" in shown and "Open file: app.py" in shown
    assert "1| " not in shown                          # no numbers to copy in Edit mode
    assert panel.proposal is not None and not panel.apply_btn.isHidden()
    view = panel.view.toPlainText()
    assert "Proposed edits · 2 of 2 can be applied" in view and "+    return x + 5" in view
    panel.send("another question")                      # held until they are decided
    assert "Apply or reject" in panel.status.text()
    panel.decide(1, True)
    panel.decide(2, False)
    panel.apply()
    assert "return x + 5" in doc.editor.toPlainText() and "def f():" in doc.editor.toPlainText()
    assert doc.modified() and (plain / "app.py").read_text(encoding="utf-8") == TEXT
    steps = [s["text"] for s in panel.turns()[-1].steps]
    assert steps[0] == "Read app.py (9 lines)"
    assert any(s.startswith("Applied edit 1 to app.py line 8: five") for s in steps)
    assert any(s.startswith("Rejected edit 2") for s in steps)
    assert "In the editor, not saved yet" in " ".join(steps)
    assert "▾ " in panel.view.toPlainText()             # the step list is open
    doc.editor.undo()                                    # one Ctrl+Z takes it back
    assert doc.editor.toPlainText() == TEXT
    doc.editor.redo()
    tab.undo_turn()
    assert doc.editor.toPlainText() == TEXT
    tab.deleteLater()


def test_a_block_that_does_not_match_is_asked_for_again_once(qapp, tmp_path, plain):
    model = Script(block("app.py", "    return x + 1", "    return 0") + "\n"
                   + block("app.py", "    return y", "    return 1"),
                   block("app.py", "    x = 1\n    return x\n", "    return 1\n"))
    tab = make_tab(tmp_path, plain, model)
    tab.open_file(plain / "app.py")
    ask(qapp, tab, "simplify")
    assert len(model.seen) == 2
    assert "could not be applied" in model.seen[1][-1]["content"]
    assert "Block 2 (app.py): The SEARCH text is not in app.py" in model.seen[1][-1]["content"]
    prop = tab.chat_panel.proposal
    assert [e.ok for _c, e in prop.edits()] == [True, True]
    assert "asked the model again" in tab.chat_panel.view.toPlainText()
    tab.chat_panel.accept_all()
    tab.chat_panel.apply()
    text = tab.doc_for(plain / "app.py").editor.toPlainText()
    assert "return 0" in text and "def f():\n    return 1\n" in text
    assert any(s.get("kind") == "retry" for s in tab.chat_panel.turns()[-3].steps)
    tab.deleteLater()


def test_a_bigger_request_gets_a_plan_card_and_go_shows_its_files(qapp, tmp_path, plain):
    model = Script(PLAN.format(questions="", pct=85).replace("load_rows", "g").replace("main", "f"),
                   block("util.py", "    pass", "    return 1", "rows"))
    tab = make_tab(tmp_path, plain, model)
    tab.open_file(plain / "app.py")
    ask(qapp, tab, "save the rows too")
    panel = tab.chat_panel
    card = panel.turns()[-1].steps[0]
    assert card["kind"] == "plan" and card["mark"] == "green"
    assert "Plan · 🟢 Green" in panel.view.toPlainText() and not panel.go_btn.isHidden()
    assert panel.proposal is None
    panel.go()
    pump(qapp, lambda: not panel.is_busy())
    shown = model.seen[1][-1]["content"]
    assert f"Request: {GO_TEXT}" in shown and "Also open: util.py" in shown
    assert panel.proposal is not None and not not panel.go_btn.isHidden()
    panel.accept_all()
    panel.apply()
    assert "return 1" in tab.doc_for(plain / "util.py").editor.toPlainText()
    tab.deleteLater()


def test_a_plan_with_questions_or_missing_files_is_not_green(qapp, tmp_path, plain):
    model = Script(PLAN.format(questions="Questions:\n- Which sheet?\n", pct=90))
    tab = make_tab(tmp_path, plain, model)
    ask(qapp, tab, "do the rows")
    card = tab.chat_panel.turns()[-1].steps[0]
    assert card["mark"] in ("amber", "red") and card["questions"] == ["Which sheet?"]
    assert "Questions for you" in tab.chat_panel.view.toPlainText()
    tab.deleteLater()


def test_clicking_a_step_opens_the_file_at_its_line(qapp, tmp_path, plain):
    model = Script(block("util.py", "    pass", "    return 2"))
    tab = make_tab(tmp_path, plain, model)
    ask(qapp, tab, "return two")
    panel = tab.chat_panel
    step = next(s for s in panel.turns()[-1].steps if s.get("kind") == "edit")
    panel.follow(step["target"])
    doc = tab.current()
    assert doc.path == plain / "util.py" and doc.editor.textCursor().blockNumber() == 1
    panel.follow("steps:1")
    assert "diff" in panel.view.toPlainText()
    tab.deleteLater()


def test_a_session_commits_each_turn_and_merge_brings_it_home(qapp, tmp_path, repo):
    model = Script(block("app.py", "    return x + 1", "    return x + 7", "seven"),
                   block("app.py", "def f():", "def first():", "rename"))
    merged = []

    def review(md):
        merged.append(md)
        return ("merge", None)
    tab = make_tab(tmp_path, repo, model, review=review)
    assert tab.session_btn.isEnabled()
    tab.open_file(repo / "app.py")
    tab.start_session()
    s = tab.session
    assert s is not None and tab.root == s.tree and tab.project == repo
    assert tab.current().path == s.tree / "app.py"
    ask(qapp, tab, "seven")
    tab.chat_panel.accept_all()
    tab.chat_panel.apply()
    assert (s.tree / "app.py").read_text(encoding="utf-8").count("x + 7") == 1
    assert (repo / "app.py").read_text(encoding="utf-8") == TEXT      # not yet
    assert "Committed as turn 1" in tab.chat_panel.view.toPlainText()
    ask(qapp, tab, "rename f")
    tab.chat_panel.accept_all()
    tab.chat_panel.apply()
    tab.undo_turn()                                      # the rename goes
    assert "def f():" in tab.current().editor.toPlainText()
    assert "Undid turn 2" in tab.chat_panel.view.toPlainText()
    tab.review_and_merge()
    assert "app.py · g" in merged[0] and "seven" in merged[0]
    assert tab.session is None and tab.root == repo
    assert "x + 7" in (repo / "app.py").read_text(encoding="utf-8")
    assert "def f():" in (repo / "app.py").read_text(encoding="utf-8")
    assert tab.current().path == repo / "app.py" and not s.tree.exists()
    tab.deleteLater()


def test_a_session_survives_a_restart_and_end_leaves_the_folder(qapp, tmp_path, repo):
    tab = make_tab(tmp_path, repo, Script())
    tab.start_session()
    sid = tab.session.id
    tab.deleteLater()
    again = make_tab(tmp_path, None, Script())
    assert again.session is not None and again.session.id == sid
    assert again.project == repo and "session copy" in again.folder_label.text()
    again.end_session()
    assert again.session is None and again.root == repo
    assert "council/" not in git(repo, "branch")
    again.deleteLater()


def test_asking_about_a_review_group_puts_its_diff_before_the_model(qapp, tmp_path, repo):
    model = Script(block("app.py", "    return x + 1", "    return x + 8", "eight"),
                   "It adds seven.")
    tab = make_tab(tmp_path, repo, model, review=lambda md: ("ask", 0))
    tab.start_session()
    tab.open_file(tab.root / "app.py")
    ask(qapp, tab, "eight")
    tab.chat_panel.accept_all()
    tab.chat_panel.apply()
    tab.review_and_merge()
    assert tab.session is not None and tab.chat_panel.mode() == "ask"
    assert tab.chat_panel.input.toPlainText() == "About app.py · g: "
    tab.chat_panel.input.setPlainText("About app.py · g: why?")
    tab.chat_panel.send()
    pump(qapp, lambda: not tab.chat_panel.is_busy())
    assert "+    return x + 8" in model.seen[1][-1]["content"]
    tab.session.end()
    tab.deleteLater()


def test_a_folder_that_is_not_a_repository_says_why_there_is_no_session(qapp, tmp_path, plain):
    tab = make_tab(tmp_path, plain, Script())
    assert not tab.session_btn.isEnabled()
    assert "needs the folder to be a git repository" in tab.session_label.text()
    tab.deleteLater()
