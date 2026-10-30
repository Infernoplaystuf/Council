"""The Build tab, step 4: run and test in the sandbox, Fix this, the test
rounds the model runs by itself, stop when stuck, and Preview.

council_core.build_sandbox (offline, one output stream, the process tree
killed, failure signatures) and the tab offscreen with a scripted fake model.
Every network target is refused before a packet is sent: 192.0.2.1 is
TEST-NET-1 (routed nowhere) and .invalid names never resolve, and each child
first checks that the sandbox is in place and stops if not.
"""
from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import build_sandbox as sb  # noqa: E402

GUARD_CHECK = (
    "import sys\n"
    "if 'council_build_sandbox' not in sys.modules:\n"
    "    print('NO SANDBOX'); sys.exit(3)\n")


def run_script(tmp_path, code, env=None, **kw):
    script = tmp_path / "script.py"
    script.write_text(GUARD_CHECK + code, encoding="utf-8")
    lines = []
    out = sb.run([sys.executable, "-u", str(script)], tmp_path, lines.append,
                 env=env, **kw)
    return out, lines


def only_the_sandbox(extra=None):
    """The sandbox's environment without the tests' own offline guard on
    PYTHONPATH, so it is the sandbox that refuses."""
    env = sb.sandbox_env(extra)
    env["PYTHONPATH"] = str(sb.HOOK_DIR)
    return env


# ── the sandbox ─────────────────────────────────────────────────────────
def test_code_run_in_the_sandbox_cannot_reach_the_network(tmp_path):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    code = (
        "import socket, urllib.request\n"
        "for what, call in [\n"
        "    ('lookup', lambda: socket.getaddrinfo('council-test.invalid', 80)),\n"
        "    ('connect', lambda: socket.create_connection(('192.0.2.1', 80), timeout=2)),\n"
        "    ('url', lambda: urllib.request.urlopen('http://council-test.invalid/', timeout=2)),\n"
        "]:\n"
        "    try:\n"
        "        call(); print(what, 'REACHED')\n"
        "    except OSError as exc:\n"
        "        print(what, 'refused', type(exc).__name__)\n"
        f"s = socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
        "print('loopback ok'); s.close()\n")
    try:
        out, lines = run_script(tmp_path, code, env=only_the_sandbox())
    finally:
        server.close()
    text = "\n".join(lines)
    assert "NO SANDBOX" not in text and out.returncode == 0, text
    assert "lookup refused" in text and "connect refused" in text and "url refused" in text
    assert "REACHED" not in text and "loopback ok" in text
    assert "[Council sandbox] refused looking up council-test.invalid:80" in text


def test_errors_arrive_in_order_with_the_prints(tmp_path):
    out, lines = run_script(tmp_path, "print('first')\nraise ValueError('boom')\n")
    assert out.returncode == 1 and out.failed and not out.ok
    assert lines[0] == "first" and lines[-1] == "ValueError: boom"
    assert lines.index("first") < lines.index("Traceback (most recent call last):")


def test_the_sitecustomize_it_shadows_still_runs(tmp_path):
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    (theirs / "sitecustomize.py").write_text("import builtins\nbuiltins.THEIRS = 1\n",
                                             encoding="utf-8")
    env = only_the_sandbox()
    env["PYTHONPATH"] = os.pathsep.join([str(sb.HOOK_DIR), str(theirs)])
    out, lines = run_script(tmp_path, "import builtins\nprint(getattr(builtins, 'THEIRS', 0))\n",
                            env=env)
    assert lines == ["1"], lines


def test_stop_and_the_time_limit_end_the_whole_tree(tmp_path):
    import threading
    code = ("import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(60)\n")
    stop = threading.Event()
    pids = []

    def line(text):
        pids.append(text)
        stop.set()
    script = tmp_path / "tree.py"
    script.write_text(code, encoding="utf-8")
    out = sb.run([sys.executable, "-u", str(script)], tmp_path, line, stop)
    assert out.stopped and not out.failed
    child = int(pids[0])
    deadline = time.time() + 10
    while time.time() < deadline and _alive(child):
        time.sleep(0.1)
    assert not _alive(child)
    out, _ = run_script(tmp_path, "import time\ntime.sleep(30)\n", timeout=1)
    assert out.timed_out and "longer than its limit" in out.summary("x")


def _alive(pid):
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259                 # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:                                         # a zombie is not running
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


# ── tests and signatures ────────────────────────────────────────────────
def test_a_folder_with_tests_is_found(tmp_path):
    assert not sb.has_tests(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("", encoding="utf-8")
    assert sb.has_tests(tmp_path)
    other = tmp_path / "o"
    other.mkdir()
    (other / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    assert sb.has_tests(other)


def test_the_same_failure_has_the_same_signature():
    one = ("FAILED test_app.py::test_add - assert -1 == 5\n"
           "1 failed, 2 passed in 0.31s")
    two = ("FAILED test_app.py::test_add - assert -1 == 5\n"
           "1 failed, 2 passed in 1.07s")
    assert sb.signature(one) == sb.signature(two) != ""
    assert sb.stuck([sb.signature(one), sb.signature(two)])
    other = "FAILED test_app.py::test_add - assert 6 == 5\n1 failed in 0.2s"
    assert not sb.stuck([sb.signature(one), sb.signature(other)])
    tb = ('  File "/tmp/pytest-of-x/a/app.py", line 3, in f\n'
          "ValueError: bad <object at 0x7f00ab>")
    tb2 = ('  File "/tmp/pytest-of-x/b/app.py", line 9, in f\n'
           "ValueError: bad <object at 0x7f99cd>")
    assert sb.signature(tb) == sb.signature(tb2) == "ValueError: bad <object at 0x…>"
    assert not sb.stuck(["", ""]) and not sb.stuck(["x"])


def test_widget_classes_and_their_module():
    src = ("from PySide6.QtWidgets import QWidget\nimport PySide6.QtWidgets as w\n"
           "class Helper:\n    pass\n"
           "class Main(QWidget):\n    pass\n"
           "class Other(w.QDialog):\n    pass\n"
           "class Mine(BaseWindow):\n    pass\n")
    assert sb.widget_classes(src) == ["Main", "Other", "Mine"]
    assert sb.widget_classes("def (") == []
    root = Path("/x/proj")
    assert sb.module_name(root, root / "pkg" / "view.py") == "pkg.view"
    assert sb.module_name(root, Path("/y/a.py")) is None
    assert sb.module_name(root, root / "my-app.py") is None


# ── the tab ──────────────────────────────────────────────────────────────
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.build import BuildActions, BuildTab  # noqa: E402


def block(path, search, replace, reason=""):
    why = f"Why: {reason}\n" if reason else ""
    return f"{why}{path}\n<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"


class Script:
    """A fake model that answers with the next reply in a list."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, messages, on_token=None, should_stop=None):
        self.seen.append(messages)
        reply = self.replies.pop(0) if self.replies else "I need the spec."
        if on_token:
            on_token(reply)
        return reply


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def pump(qapp, until, seconds=60.0):
    deadline = time.time() + seconds
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    assert until(), "timed out"


APP = "def add(a, b):\n    return a - b\n"
TEST = "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text(APP, encoding="utf-8")
    (root / "test_app.py").write_text(TEST, encoding="utf-8")
    return root


def make_tab(tmp_path, folder, model, previews=None):
    actions = BuildActions(
        vault_dir=tmp_path / "vault", state_file=tmp_path / "state.json",
        unsaved_dir=tmp_path / "unsaved", confirm=lambda t, x: True,
        ask_folder=lambda start: "", ask_name=lambda t, l: None, chat=model,
        review=lambda md: ("cancel", None),
        show_preview=(lambda png, text: previews.append((png, text)))
        if previews is not None else None)
    actions.save_state({"folder": str(folder)})
    return BuildTab(None, actions)


def settle(qapp, tab, seconds=120.0):
    """Until no run and no chat answer is going, and none starts (a test
    round can start a model turn, which can start the next round)."""
    def idle():
        return not tab.is_running() and not tab.chat_panel.is_busy()
    deadline = time.time() + seconds
    quiet = 0
    while quiet < 10 and time.time() < deadline:
        qapp.processEvents()
        quiet = quiet + 1 if idle() else 0
        time.sleep(0.02)
    assert idle(), "timed out"


def test_test_runs_the_folders_tests_and_a_failure_offers_fix_this(qapp, tmp_path, project):
    tab = make_tab(tmp_path, project, Script())
    assert tab.on_test()
    settle(qapp, tab)
    out = tab.output.toPlainText()
    assert "offline in the sandbox" in out and "Tests failed: 1 failed" in out
    assert "assert -1 == 5" in out
    assert tab.fix_btn.isEnabled() and tab.chat_panel.use_output.isChecked()
    empty = tmp_path / "empty"
    empty.mkdir()
    tab.open_folder(empty)
    assert not tab.on_test() and "No tests found" in tab.status.text()
    tab.deleteLater()


def test_fix_this_then_the_tests_run_again_by_themselves(qapp, tmp_path, project):
    model = Script(block("app.py", "    return a - b", "    return a + b", "add, not subtract"))
    tab = make_tab(tmp_path, project, model)
    tab.on_test()
    settle(qapp, tab)
    tab.on_fix()
    settle(qapp, tab)
    shown = model.seen[0][-1]["content"]
    assert "Request: Fix this: The tests failed (1 failed" in shown
    assert "assert -1 == 5" in shown                       # the output went with it
    panel = tab.chat_panel
    assert panel.mode() == "edit" and panel.proposal is not None
    panel.accept_all()
    panel.apply()
    settle(qapp, tab)
    assert "Tests passed: 1 passed" in tab.output.toPlainText()
    steps = [s["text"] for s in panel.turns()[-1].steps]
    assert any(s.startswith("Ran the tests (round 1 of 3): 1 passed") for s in steps)
    assert "pass after round 1" in panel.status.text() and tab._fix is None
    tab.deleteLater()


def test_the_same_failure_twice_stops_and_asks_what_is_in_the_way(qapp, tmp_path, project):
    model = Script(block("app.py", "def add(a, b):", "def add(a, b):  # checked"),
                   "What I tried: a comment.\nThe error: add subtracts.\n"
                   "What I need from you: is add meant to add?")
    tab = make_tab(tmp_path, project, model)
    tab.on_test()
    settle(qapp, tab)
    tab.on_fix()
    settle(qapp, tab)
    tab.chat_panel.accept_all()
    tab.chat_panel.apply()
    settle(qapp, tab)
    assert len(model.seen) == 2                            # no second fix asked for
    blocker = model.seen[1][-1]["content"]
    assert "Stop trying to fix it now: the same failure came back" in blocker
    assert "What I need from you" in blocker and "Request:" not in blocker
    view = tab.chat_panel.view.toPlainText()
    assert "Council · stuck" in view and "is add meant to add?" in view
    assert "Council stopped the fix" in view and tab._fix is None
    tab.deleteLater()


def test_the_rounds_stop_after_three(qapp, tmp_path, project):
    fixes = [block("app.py", f"    return a {old} b", f"    return a {new} b")
             for old, new in (("-", "*"), ("*", "//"), ("//", "**"))]
    model = Script(*fixes, "What I need from you: the rule for add.")
    tab = make_tab(tmp_path, project, model)
    tab.on_test()
    settle(qapp, tab)
    tab.on_fix()
    for _ in range(3):
        settle(qapp, tab)
        tab.chat_panel.accept_all()
        tab.chat_panel.apply()
    settle(qapp, tab)
    asked = [m[-1]["content"] for m in model.seen]
    assert len(asked) == 4
    assert "Request: The tests still fail after your fix (round 1 of 3)" in asked[1]
    assert "Request: The tests still fail after your fix (round 2 of 3)" in asked[2]
    assert "the tests still fail after 3 rounds" in asked[3]
    steps = [st["text"] for t in tab.chat_panel.turns() for st in t.steps]
    assert "Stopped after 3 rounds" in steps and tab._fix is None
    tab.deleteLater()


def test_an_edit_that_does_not_match_twice_stops_too(qapp, tmp_path, project):
    bad = block("app.py", "    return b - a", "    return a + b")
    model = Script(bad, bad, "What I need from you: the current file.")
    tab = make_tab(tmp_path, project, model)
    tab.open_file(project / "app.py")
    tab.chat_panel.set_mode("edit")
    tab.chat_panel.input.setPlainText("make add add")
    tab.chat_panel.send()
    settle(qapp, tab)
    assert len(model.seen) == 3
    assert "could not be matched to the file twice" in model.seen[2][-1]["content"]
    steps = [s["text"] for s in tab.chat_panel.turns()[1].steps]
    assert any(s.startswith("Stopped: 1 edit(s) did not match twice") for s in steps)
    tab.deleteLater()


def test_a_failed_run_offers_fix_this_without_test_rounds(qapp, tmp_path, project):
    (project / "boom.py").write_text("raise RuntimeError('broken')\n", encoding="utf-8")
    model = Script(block("boom.py", "raise RuntimeError('broken')", "print('fine')"))
    tab = make_tab(tmp_path, project, model)
    tab.open_file(project / "boom.py")
    tab.on_run()
    settle(qapp, tab)
    assert tab.fix_btn.isEnabled()
    tab.on_fix()
    settle(qapp, tab)
    assert "Fix this: boom.py failed" in model.seen[0][-1]["content"]
    assert "RuntimeError: broken" in model.seen[0][-1]["content"]
    assert tab._fix is None                               # a run is not re-run by itself
    tab.chat_panel.accept_all()
    tab.chat_panel.apply()
    settle(qapp, tab)
    assert "Tests" not in tab.output.toPlainText().split("[boom.py exited")[-1]
    tab.deleteLater()


def test_preview_builds_the_widget_and_a_broken_one_offers_fix_this(qapp, tmp_path, project):
    (project / "view.py").write_text(
        "from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget\n\n\n"
        "class View(QWidget):\n"
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "        QVBoxLayout(self).addWidget(QLabel('Hello'))\n", encoding="utf-8")
    (project / "broken.py").write_text(
        "from PySide6.QtWidgets import QWidget\n\n\n"
        "class Broken(QWidget):\n"
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "        raise ValueError('no layout')\n", encoding="utf-8")
    previews = []
    tab = make_tab(tmp_path, project, Script(), previews)
    tab.open_file(project / "view.py")
    tab.on_preview()
    pump(qapp, lambda: not tab.is_running(), seconds=120)
    assert previews and previews[0][0].endswith(".png") and Path(previews[0][0]).is_file()
    assert "'Hello'" in previews[0][1] and not tab.fix_btn.isEnabled()
    tab.open_file(project / "broken.py")
    tab.on_preview()
    pump(qapp, lambda: not tab.is_running(), seconds=120)
    assert tab.fix_btn.isEnabled() and "no layout" in tab.output.toPlainText()
    tab.open_file(project / "app.py")
    tab.on_preview()
    assert "No Qt widget class" in tab.status.text()
    tab.deleteLater()
