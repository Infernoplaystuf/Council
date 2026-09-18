"""
The IDE: a buffer, a subprocess, and a gate in front of it.

The tab runs the buffer as a real Python process with the user's own
permissions. The scanner that warns about that had no toolkit references and
still lived inside a widget method that reaches for messagebox — so the one
security-relevant piece in the tab could only be tested by opening a window.

THE THREE DEFECTS ARE ALL ABOUT THE TIMEOUT
A blocking run that times out discards everything the script printed, even
though TimeoutExpired carries it. A streaming run that times out is reported as
an ordinary exit. And neither path has a busy guard, so a second run overwrites
the exact file the first subprocess is still executing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import ide_jobs, ide_trust  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_the_modules_import_no_toolkit():
    """Checked on the IMPORTS, not on the text. The first version searched for
    substrings and failed on the docstring explaining that the Tk version
    reaches for messagebox — the fourth time in this port a source assertion
    has tripped on the prose describing the thing it checks."""
    import ast

    for name in ("ide_trust.py", "ide_jobs.py"):
        tree = ast.parse((ROOT / "council_core" / name).read_text(
            encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for toolkit in ("tkinter", "PySide6", "PyQt5"):
            assert toolkit not in imported, f"{name} imports {toolkit}"


# ============================================================
# The scanner
# ============================================================

@pytest.mark.parametrize("line,label", [
    ("os.system('dir')", "shell command execution"),
    ("subprocess.run(['ls'])", "subprocess calls"),
    ("shutil.rmtree(p)", "directory delete/move"),
    ("os.remove(p)", "file/directory deletion"),
    ("p.unlink()", "Path.unlink (file deletion)"),
    ("eval(user_input)", "dynamic code evaluation"),
    ("exec(blob)", "dynamic code execution"),
    ("__import__('os')", "dynamic imports"),
    ("requests.get(url)", "outbound HTTP"),
    ("urllib.request.urlopen(u)", "outbound HTTP"),
    ("socket.socket()", "raw socket access"),
    ("os.environ['KEY']", "environment variable access"),
    ("pickle.loads(blob)", "pickle deserialisation (RCE risk)"),
])
def test_every_declared_pattern_is_actually_matched(line, label):
    """A pattern in the table that matches nothing is a warning the user will
    never see, and there is no way to notice from reading the table."""
    hits = ide_trust.scan(line)
    assert hits, f"{line!r} matched nothing"
    assert hits[0].label == label


def test_a_commented_out_call_is_not_flagged():
    """It is not a thing the script does, and flagging it teaches the user
    that the warning is noise."""
    assert ide_trust.scan("# os.system('rm -rf /')") == []


def test_a_line_is_flagged_once_however_many_patterns_it_matches():
    """A line doing two risky things is still one line to look at. Listing it
    twice makes the prompt look worse than the code is."""
    hits = ide_trust.scan("os.system(eval(x))")
    assert len(hits) == 1


def test_the_hit_carries_the_line_number_and_the_source():
    """"This script does subprocess calls" is not actionable, and neither is
    "line 42"."""
    hits = ide_trust.scan("print(1)\nimport os\nos.system('dir')\n")
    assert hits[0].line == 3
    assert "os.system" in hits[0].snippet


def test_a_very_long_line_is_truncated_in_the_prompt():
    code = "os.system('" + "x" * 500 + "')"
    assert len(ide_trust.scan(code)[0].snippet) <= ide_trust.SNIPPET


def test_ordinary_code_is_not_flagged():
    assert ide_trust.scan("import math\nprint(math.pi)\n") == []


def test_blank_lines_are_skipped():
    assert ide_trust.scan("\n\n   \n") == []


# ============================================================
# The prompt
# ============================================================

def test_the_prompt_names_each_hit():
    hits = ide_trust.scan("os.system('a')\npickle.loads(b)\n")
    text = ide_trust.message(hits)
    assert "Line 1" in text and "Line 2" in text
    assert "shell command execution" in text


def test_a_long_list_is_capped_and_says_how_many_more():
    """A dialog with eighty bullet points is one nobody reads."""
    code = "\n".join(f"os.system('{i}')" for i in range(25))
    text = ide_trust.message(ide_trust.scan(code))
    assert "and 15 more" in text
    assert text.count("• Line") == ide_trust.SHOWN


def test_the_prompt_asks_rather_than_announcing():
    text = ide_trust.message(ide_trust.scan("os.system('a')"))
    assert text.rstrip().endswith("?")


# ============================================================
# Trust
# ============================================================

def test_safe_code_needs_no_asking():
    assert ide_trust.check("print(1)", ide_trust.TrustStore()).allowed


def test_risky_code_asks():
    verdict = ide_trust.check("os.system('a')", ide_trust.TrustStore())
    assert not verdict.allowed
    assert verdict.needs_asking
    assert verdict.hits


def test_trusting_it_once_stops_the_asking():
    store = ide_trust.TrustStore()
    code = "os.system('a')"
    store.trust(code)
    assert ide_trust.check(code, store).allowed


def test_editing_one_character_asks_again():
    """The thing the user approved was that text. "Trust this file" would
    carry approval across a change they did not read."""
    store = ide_trust.TrustStore()
    store.trust("os.system('a')")
    assert not ide_trust.check("os.system('b')", store).allowed


def test_trust_is_not_written_anywhere():
    """An approval that survived a restart would be a permission the user
    granted once and cannot see or revoke."""
    source = (ROOT / "council_core" / "ide_trust.py").read_text(
        encoding="utf-8")
    for persisting in ("write_text", "json.dump", "open(", "Path("):
        assert persisting not in source


def test_forgetting_clears_every_approval():
    store = ide_trust.TrustStore()
    store.trust("os.system('a')")
    store.forget()
    assert not ide_trust.check("os.system('a')", store).allowed


def test_the_gate_returns_a_verdict_rather_than_opening_a_dialog():
    """The Tk version reaches for messagebox from inside the same method that
    does the scanning, which is why the scanner could not be tested."""
    from tests.source_checks import code_of
    source = (ROOT / "council_core" / "ide_trust.py").read_text(
        encoding="utf-8")
    body = code_of(source, "check")
    for dialog in ("askyesno", "QMessageBox", "input("):
        assert dialog not in body


# ============================================================
# File names
# ============================================================

def test_a_name_is_made_safe():
    assert ide_jobs.script_basename("My Script!.py") == "My_Script"


def test_the_extension_is_stripped_before_the_truncation():
    """THE DEFECT. Tk cuts to 60 and THEN strips ".py", so a 61-character name
    ending in ".py" is cut mid-extension, the strip never matches, and the file
    is called `..._p`."""
    name = "a" * 61 + ".py"
    result = ide_jobs.script_basename(name)
    assert not result.endswith("p" * 2)
    assert result == "a" * ide_jobs.MAX_NAME


def test_a_name_is_never_empty():
    for raw in ("", "   ", "...", "___", None):
        assert ide_jobs.script_basename(raw) == "script"


def test_a_name_never_escapes_the_workspace():
    assert "/" not in ide_jobs.script_basename("../../etc/passwd")
    assert "\\" not in ide_jobs.script_basename("..\\..\\windows")


# ============================================================
# Running
# ============================================================

class Runner:
    def __init__(self, *, blocking=None, streaming=None, raises=None):
        self.blocking = blocking or (0, "out", "err", Path("x.py"))
        self.streaming = streaming or (0, Path("x.py"))
        self.raises = raises
        self.seen = []

    def run_code(self, code, *, filename_hint, timeout_s, env_extra=None):
        self.seen.append(filename_hint)
        if self.raises:
            raise self.raises
        return self.blocking

    def run_code_streaming(self, code, *, filename_hint, timeout_s,
                           stdout_callback=None, stderr_callback=None):
        self.seen.append(filename_hint)
        if self.raises:
            raise self.raises
        if stdout_callback:
            for i in range(3):
                stdout_callback(f"line {i}\n")
        return self.streaming


def test_a_blocking_run_hands_back_everything():
    result = ide_jobs.run_blocking(Runner(), "print(1)", name="demo")
    assert result.ok
    assert result.stdout == "out" and result.stderr == "err"


def test_a_blocking_timeout_keeps_what_was_already_printed():
    """THE DEFECT. TimeoutExpired CARRIES the partial stdout and stderr, and
    the Tk handler formats the exception and drops them — so a script that
    printed three hundred useful lines and then hung shows a one-line
    traceback."""
    expired = subprocess.TimeoutExpired("py", 120,
                                        output=b"three hundred lines",
                                        stderr=b"and a warning")
    result = ide_jobs.run_blocking(Runner(raises=expired), "x", name="demo")
    assert result.timed_out
    assert "three hundred lines" in result.stdout
    assert "warning" in result.stderr


def test_a_timeout_is_not_reported_as_an_exit_code():
    expired = subprocess.TimeoutExpired("py", 120)
    result = ide_jobs.run_blocking(Runner(raises=expired), "x", name="demo")
    assert result.rc is None
    assert not result.ok
    assert "STOPPED" in result.summary("demo.py")


def test_a_streaming_run_calls_back_per_line():
    seen = []
    result = ide_jobs.run_streaming(Runner(), "x", name="demo",
                                    on_stdout=seen.append)
    assert result.ok
    assert seen == ["line 0\n", "line 1\n", "line 2\n"]


def test_a_streaming_timeout_is_reported_as_one():
    """`run_code_streaming` catches TimeoutExpired itself, kills the child and
    returns its return code like any other — so `time.sleep(300)` prints
    "Exited rc=1" after two minutes with nothing to say it was stopped."""
    result = ide_jobs.run_streaming(Runner(streaming=(1, Path("x.py"))), "x",
                                    name="demo", timeout_s=0)
    assert result.timed_out
    assert "STOPPED" in result.summary("demo.py")


def test_a_streaming_run_that_finishes_in_time_is_not_called_a_timeout():
    result = ide_jobs.run_streaming(Runner(), "x", name="demo",
                                    timeout_s=3600)
    assert not result.timed_out
    assert result.ok


def test_a_runner_that_blows_up_is_reported_not_raised():
    result = ide_jobs.run_blocking(Runner(raises=OSError("no python")), "x",
                                   name="demo")
    assert not result.ok
    assert "no python" in result.error
    assert "could not run" in result.summary("demo.py")


def test_the_derived_file_name_reaches_the_runner():
    runner = Runner()
    ide_jobs.run_blocking(runner, "x", name="My Script.py")
    assert runner.seen == ["My_Script.py"]


def test_a_snapshot_reports_where_it_went(tmp_path):
    class Librarian:
        def snapshot_code(self, code, *, label="council_code"):
            path = tmp_path / f"{label}.py"
            path.write_text(code, encoding="utf-8")
            return path

    result = ide_jobs.snapshot(Librarian(), "print(1)")
    assert result.ok and result.path.exists()


def test_a_failing_snapshot_is_reported_not_raised():
    class Librarian:
        def snapshot_code(self, code, *, label="council_code"):
            raise OSError("disk full")

    result = ide_jobs.snapshot(Librarian(), "x")
    assert not result.ok and "disk full" in result.error


# ============================================================
# The Qt tab
# ============================================================

pytest.importorskip("PySide6", reason="the IDE tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.ide import IdeActions, IdeTab, build_ide  # noqa: E402


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


@pytest.fixture
def tab(qapp, tmp_path):
    actions = IdeActions(workspace=tmp_path / "ws", vault_dir=tmp_path)
    actions._runner = Runner()
    view = IdeTab(actions=actions, confirm=lambda *a, **k: True)
    yield view
    import threading
    deadline = time.time() + 5.0
    while any(t.name.startswith("ide-") and t.is_alive()
              for t in threading.enumerate()) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    view.deleteLater()
    qapp.processEvents()


def test_the_factory_takes_a_window(qapp):
    view = build_ide(None)
    assert isinstance(view, IdeTab)
    view.deleteLater()


def test_a_blocking_run_shows_its_output(tab, qapp):
    tab.code.setPlainText("print(1)")
    tab.on_run()
    drive(qapp, tab)
    assert "out" in tab.output.toPlainText()
    assert "Exited rc=0" in tab.output.toPlainText()


def test_a_streaming_run_shows_each_line(tab, qapp):
    tab.code.setPlainText("print(1)")
    tab.on_run_streaming()
    drive(qapp, tab)
    text = tab.output.toPlainText()
    for i in range(3):
        assert f"line {i}" in text


def test_an_empty_buffer_runs_nothing(tab, qapp):
    tab.code.setPlainText("   ")
    tab.on_run()
    qapp.processEvents()
    assert tab.actions._runner.seen == []


def test_risky_code_is_refused_when_the_user_says_no(tab, qapp):
    tab.confirm = lambda *a, **k: False
    tab.code.setPlainText("import os\nos.system('dir')")
    tab.on_run()
    qapp.processEvents()
    assert tab.actions._runner.seen == []
    assert "Cancelled" in tab.output.toPlainText()


def test_risky_code_runs_once_approved(tab, qapp):
    tab.code.setPlainText("import os\nos.system('dir')")
    tab.on_run()
    drive(qapp, tab)
    assert tab.actions._runner.seen


def test_approving_once_does_not_ask_again(tab, qapp):
    asked = []
    tab.confirm = lambda *a, **k: (asked.append(1), True)[1]
    tab.code.setPlainText("import os\nos.system('dir')")
    tab.on_run()
    drive(qapp, tab)
    tab.on_run()
    drive(qapp, tab)
    assert len(asked) == 1


def test_a_second_run_while_one_is_going_is_refused(tab, qapp):
    """THE DEFECT. Neither Tk path has a busy guard, and both derive the same
    path from the same script name — so the second invocation overwrites the
    exact .py the first subprocess is still executing."""
    import threading
    release = threading.Event()
    calls = []

    def slow(code, name):
        calls.append(name)
        release.wait(3.0)
        return ide_jobs.RunResult(rc=0)

    tab.actions.run_blocking = slow
    tab.code.setPlainText("print(1)")
    tab.on_run()
    for _ in range(200):
        qapp.processEvents()
        if tab._busy:
            break
    tab.on_run()
    assert "already going" in tab.status.text()
    release.set()
    drive(qapp, tab)
    assert len(calls) == 1


def test_the_run_buttons_are_disabled_for_the_duration(tab, qapp):
    import threading
    release = threading.Event()

    def slow(code, name):
        release.wait(3.0)
        return ide_jobs.RunResult(rc=0)

    tab.actions.run_blocking = slow
    tab.code.setPlainText("print(1)")
    tab.on_run()
    for _ in range(200):
        qapp.processEvents()
        if not tab.run_btn.isEnabled():
            break
    assert not tab.run_btn.isEnabled() and not tab.stream_btn.isEnabled()
    release.set()
    drive(qapp, tab)
    assert tab.run_btn.isEnabled() and tab.stream_btn.isEnabled()


def test_a_timed_out_run_shows_its_partial_output(tab, qapp):
    expired = subprocess.TimeoutExpired("py", 120, output=b"got this far")
    tab.actions._runner = Runner(raises=expired)
    tab.code.setPlainText("print(1)")
    tab.on_run()
    drive(qapp, tab)
    text = tab.output.toPlainText()
    assert "got this far" in text
    assert "STOPPED" in text


def test_the_output_pane_is_capped(tab):
    """The Tk pane is unbounded, and a script printing a few hundred thousand
    lines takes the window with it."""
    assert 0 < tab.output.maximumBlockCount() <= 50_000


def test_the_output_pane_is_read_only(tab):
    assert tab.output.isReadOnly()


def test_output_does_not_steal_the_scrollbar(tab, qapp):
    """Tk calls see("end") on every line, so reading a traceback while output
    is still arriving is impossible."""
    for i in range(400):
        tab.append(f"line {i}")
    qapp.processEvents()
    bar = tab.output.verticalScrollBar()
    bar.setValue(0)
    tab.append("one more")
    qapp.processEvents()
    assert bar.value() == 0, "the pane scrolled while the reader was at the top"


def test_output_follows_when_the_reader_is_at_the_bottom(tab, qapp):
    for i in range(400):
        tab.append(f"line {i}")
    qapp.processEvents()
    bar = tab.output.verticalScrollBar()
    bar.setValue(bar.maximum())
    tab.append("the newest line")
    qapp.processEvents()
    assert bar.value() >= bar.maximum() - 4


def test_a_snapshot_reports_where_it_went(tab, qapp, tmp_path):
    saved = tmp_path / "snap.py"

    class Librarian:
        def snapshot_code(self, code, *, label="council_code"):
            saved.write_text(code, encoding="utf-8")
            return saved

    tab.actions._librarian = Librarian()
    tab.code.setPlainText("print(1)")
    tab.on_snapshot()
    deadline = time.time() + 5
    while "snapshot" not in tab.output.toPlainText() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert "snapshot saved" in tab.output.toPlainText()
    assert saved.exists()


def test_snapshotting_an_empty_buffer_says_so(tab, qapp):
    tab.code.setPlainText("  ")
    tab.on_snapshot()
    qapp.processEvents()
    assert "Nothing to snapshot" in tab.status.text()


def test_no_worker_touches_a_widget_directly():
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "tabs" / "ide.py").read_text(
        encoding="utf-8")
    for name in ("on_run", "on_run_streaming", "on_snapshot"):
        body = code_of(source, name)
        assert "_to_ui" in body, f"{name} has no marshalling seam"
        inner = body.split("def work", 1)[1].split("def show", 1)[0]
        for forbidden in ("self.output.setPlainText", "self.status.setText",
                          "self.run_btn"):
            assert forbidden not in inner, f"{name}'s worker touches a widget"


def test_the_streaming_callbacks_marshal_from_inside_themselves():
    """They fire on the RUNNER's drain threads, not on the worker this tab
    started — so marshalling around the call is not enough."""
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "tabs" / "ide.py").read_text(
        encoding="utf-8")
    body = code_of(source, "on_run_streaming")
    inner = body.split("def out", 1)[1].split("result =", 1)[0]
    assert "_to_ui" in inner
