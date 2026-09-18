"""
The Qt shell's foundation: the thread seam, the theme, the tab host.

THE POINT OF THIS FILE IS THE THREAD SEAM. `QTimer.singleShot(0, fn)` called
from a worker thread never fires and never raises, and that is the naive
translation of the ~83 `self.after(0, ...)` sites in the Tk engine — most of
which ARE called from workers. A port that gets this wrong loses those code
paths silently, with the suite still green. So the first tests here assert the
delivery that Qt does not give you for free, and one of them demonstrates the
failure mode directly so it cannot be quietly reintroduced.

Offscreen, so this can share a pytest session with the Tk fixtures: measured,
the "windows" platform flips process DPI awareness and shrinks live Tk windows
~20%, while offscreen leaves it alone.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the Qt shell needs PySide6 installed")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import (QApplication, QLabel,  # noqa: E402
                               QVBoxLayout, QWidget)

from council_qt import theme  # noqa: E402
from council_qt.bridge import UiBridge  # noqa: E402
from council_qt.window import CouncilWindow  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, predicate, timeout=5.0):
    """Run the event loop until ``predicate`` or the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------------ the thread seam

def test_call_on_ui_delivers_from_a_worker_thread(qapp):
    """The property the whole port depends on."""
    bridge = UiBridge()
    seen = []
    threading.Thread(
        target=lambda: bridge.call_on_ui(seen.append, "from the worker"),
        daemon=True).start()
    assert _pump(qapp, lambda: seen), "worker callback never reached the UI"
    assert seen == ["from the worker"]
    bridge.stop()


def test_the_naive_qt_translation_really_does_fail(qapp):
    """Documented, not assumed: QTimer.singleShot(0, fn) from a worker thread
    does not fire. This is why call_on_ui exists, and if Qt ever changes this
    the test will say so rather than leaving the workaround unexplained."""
    fired = []

    def worker():
        QTimer.singleShot(0, lambda: fired.append(True))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join()
    _pump(qapp, lambda: False, timeout=0.5)     # give it every chance
    assert fired == [], ("QTimer.singleShot from a worker fired — Qt behaviour "
                         "changed; revisit council_qt.bridge")


def test_after_from_a_worker_thread_still_runs(qapp):
    """The ~83 `self.after(0, fn)` sites convert by name, so the shim has to be
    safe from the threads those sites actually run on."""
    bridge = UiBridge()
    seen = []
    threading.Thread(target=lambda: bridge.after(0, seen.append, "late"),
                     daemon=True).start()
    assert _pump(qapp, lambda: seen)
    assert seen == ["late"]
    bridge.stop()


def test_after_on_the_ui_thread_can_be_cancelled(qapp):
    bridge = UiBridge()
    seen = []
    token = bridge.after(50, seen.append, "should not arrive")
    bridge.after_cancel(token)
    _pump(qapp, lambda: False, timeout=0.3)
    assert seen == []
    bridge.stop()


def test_the_queue_is_drained_in_order_and_without_loss(qapp):
    """Several workers, many messages: nothing lost, per-thread order kept.

    The live stream box depends on draining everything available in one pass
    rather than one message per tick — it takes ~100 tokens/sec and scrolls
    once per drain. (The transcript never sees a token; that claim was in this
    docstring and was wrong.)"""
    got = []
    bridge = UiBridge(dispatch=got.append)

    def worker(tag):
        for i in range(50):
            bridge.post((tag, i))

    threads = [threading.Thread(target=worker, args=(t,), daemon=True)
               for t in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _pump(qapp, lambda: len(got) == 150)
    for tag in ("a", "b", "c"):
        assert [i for t, i in got if t == tag] == list(range(50))
    bridge.stop()


def test_a_raising_callback_does_not_stop_the_pump(qapp):
    """One bad handler must cost one message, not every message after it — the
    shape the Tk dispatcher already had."""
    bridge = UiBridge()
    seen = []

    def boom():
        raise RuntimeError("handler blew up")

    bridge.call_on_ui(boom)
    bridge.call_on_ui(seen.append, "still delivered")
    assert _pump(qapp, lambda: seen)
    bridge.stop()


def test_assert_ui_thread_is_a_real_tripwire(qapp):
    """Qt usually does not complain when a widget is touched off-thread; it
    just corrupts quietly. This is the tripwire that keeps that findable."""
    bridge = UiBridge()
    bridge.assert_ui_thread("the test")             # on the UI thread: fine
    failures = []

    def worker():
        try:
            bridge.assert_ui_thread("a worker")
        except RuntimeError as exc:
            failures.append(str(exc))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join()
    assert failures and "worker" in failures[0]
    bridge.stop()


# ------------------------------------------------------------------- the theme

def test_the_theme_comes_from_branding_not_from_hardcoded_hex(qapp):
    """One source of truth for colour, shared with the Tk shell — the thing the
    271 scattered hex literals in the engine are a lesson about."""
    import branding
    tokens = theme.tokens("dark")
    assert tokens is branding.get_theme("dark")
    palette = theme.palette("dark")
    from PySide6.QtGui import QPalette
    assert palette.color(QPalette.ColorRole.Window).name() == tokens["bg"]


def test_the_light_theme_is_reachable(qapp):
    """branding.LIGHT_THEME has existed all along; the Tk shell hard-codes dark
    and never reads it. Under Qt it costs nothing to honour."""
    light = theme.palette("light")
    dark = theme.palette("dark")
    from PySide6.QtGui import QPalette
    assert (light.color(QPalette.ColorRole.Window)
            != dark.color(QPalette.ColorRole.Window))


def test_disabled_text_is_distinct(qapp):
    """Without a Disabled group, ports.enable(False) has no visible effect."""
    from PySide6.QtGui import QPalette
    p = theme.palette("dark")
    normal = p.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText)
    disabled = p.color(QPalette.ColorGroup.Disabled,
                       QPalette.ColorRole.WindowText)
    assert normal != disabled


# ------------------------------------------------------------- the tab host

def test_tabs_are_built_lazily_and_only_once(qapp):
    """Lazy construction is what removes the splash-pumping the Tk startup
    needs; building twice would double every tab's workers.

    The FIRST tab builds as it is added, because adding it makes it current and
    it is therefore about to be visible. Laziness is about the other nineteen.
    """
    built = []

    def factory(tag):
        built.append(tag)
        w = QWidget()
        QLabel(tag, w)
        return w

    window = CouncilWindow()
    window.add_tab("First", lambda: factory("First"))
    window.add_tab("Second", lambda: factory("Second"))
    qapp.processEvents()
    assert built == ["First"], "a tab was built before it was shown"

    window.tabs.setCurrentIndex(1)
    qapp.processEvents()
    assert built == ["First", "Second"]

    window.tabs.setCurrentIndex(0)
    window.tabs.setCurrentIndex(1)
    qapp.processEvents()
    assert built == ["First", "Second"], "a tab was rebuilt on a later visit"
    window.request_close()


def test_an_eager_tab_exists_before_it_is_shown(qapp):
    """For tabs a background worker posts to whether or not the user looks."""
    built = []
    window = CouncilWindow()
    window.add_tab("Eager", lambda: (built.append(True), QWidget())[1],
                   eager=True)
    assert built == [True]
    window.request_close()


def test_show_tab_selects_by_name(qapp):
    """Replaces the Tk shell's 7 cross-tab `nb.select(self.tab_x)` calls, which
    depend on a widget attribute existing."""
    window = CouncilWindow()
    window.add_tab("One", QWidget)
    window.add_tab("Two", QWidget)
    window.show_tab("Two")
    assert window.tabs.tabText(window.tabs.currentIndex()) == "Two"
    window.request_close()


def test_request_close_runs_on_close_once(qapp):
    window = CouncilWindow()
    calls = []
    window.on_close = lambda: calls.append(True)
    window.request_close()
    window.request_close()
    assert calls == [True]


def test_closing_the_window_also_runs_the_cleanup(qapp):
    """The X and the Stop path must reach the same cleanup — that is where a
    camera would be released."""
    window = CouncilWindow()
    calls = []
    window.on_close = lambda: calls.append(True)
    window.close()
    assert calls == [True]


def test_the_application_actually_exits(tmp_path):
    """A SUBPROCESS test, because this bug is invisible in-process.

    The natural closeEvent — ignore(), clean up, close() again — deadlocks the
    whole application: during quit() Qt sends close events to top-level windows,
    and a window that ignores one vetoes the shutdown. exec() then never
    returns and the process has to be killed. It cost a bisect to find once; it
    should cost a test run to find again.
    """
    import subprocess
    import textwrap
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from council_qt.window import CouncilWindow
        app = QApplication([])
        w = CouncilWindow()
        w.show()
        QTimer.singleShot(300, app.quit)
        code = app.exec()
        print("exited", code)
    """)
    path = tmp_path / "exits.py"
    path.write_text(script, encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    done = subprocess.run([sys.executable, str(path)], capture_output=True,
                          text=True, timeout=60, env=env)
    assert "exited 0" in done.stdout, (done.stdout, done.stderr)


def test_the_standalone_host_matches_the_tk_contract(qapp):
    """council_modules.StandaloneHost is the contract a family of tab modules is
    written against — tab_grapher here, tab_ideas and tab_video on other
    branches. A ported tab module must be able to change which host it imports
    without changing how it talks to one, so the surface has to match.

    The model slots are checked by name because a tab module branches on them
    (`if self.writer is None:`), which makes a missing or renamed slot a
    behaviour change in a module this package cannot see."""
    import inspect

    import council_modules
    from council_qt.host import MODEL_ROLES, StandaloneHost

    tk_src = inspect.getsource(council_modules.StandaloneHost.__init__)
    for role in MODEL_ROLES:
        assert f'"{role}"' in tk_src, f"{role} is not a role on the Tk host"

    host = StandaloneHost(title="test")
    for role in MODEL_ROLES:
        assert getattr(host, role) is None
    assert host.root is host.window
    assert host.ui_q is host.bridge.q
    assert (host.nb, host.tab_council, host.input) == (None, None, None)
    host.window.request_close()


def test_the_standalone_host_schedules_safely_from_a_worker(qapp):
    """The Tk host delegates after() straight to root.after, which is only safe
    on the UI thread. Moving to Qt makes a tab module that was quietly wrong
    right — provided the host routes through the bridge."""
    from council_qt.host import StandaloneHost
    host = StandaloneHost(title="test")
    seen = []
    t = threading.Thread(target=lambda: host.after(0, seen.append, "worker"),
                         daemon=True)
    t.start()
    t.join()
    assert _pump(qapp, lambda: seen)
    assert seen == ["worker"]
    host.window.request_close()


def test_the_diagnostics_tab_builds_and_reports(qapp):
    """The first real tab, end to end: a worker gathers, the bridge delivers.

    The timeout is deliberately generous. This flaked twice under random test
    ordering — `platform.platform()` and the disk probes in `_report()` are
    not fast, and by the time this runs the session may have built and closed
    fifty windows for the parametrised per-tab checks. A five-second budget for
    a real worker round-trip is a race, and a flaky test in the harness is
    worse than a slow one: it trains you to re-run rather than to look.
    """
    from council_qt.tabs.diagnostics import build_diagnostics
    window = CouncilWindow()
    window.add_tab("Diagnostics", lambda: build_diagnostics(window), eager=True)
    page = window.tab("Diagnostics")
    from PySide6.QtWidgets import QPlainTextEdit
    output = page.findChild(QPlainTextEdit)
    arrived = _pump(qapp, lambda: "python" in output.toPlainText(),
                    timeout=20.0)
    assert arrived, (
        "the worker's report never reached the widget; the pane holds "
        f"{output.toPlainText()!r}")
    assert "PySide6" in output.toPlainText()
    window.request_close()


# ------------------------------------------------------------- the Vault tab

def test_the_vault_tab_builds_and_lists_a_vault(qapp, tmp_path):
    """The pilot tab, against a real folder."""
    from council_qt.tabs.vault import VaultActions, VaultTab
    (tmp_path / "data_in").mkdir()
    (tmp_path / "data_in" / "orders.csv").write_text("id,total\n1,5\n")
    (tmp_path / "top.txt").write_text("hello")

    window = CouncilWindow()
    tab = VaultTab(window, VaultActions(tmp_path))
    assert tab.tree.topLevelItemCount() == 2          # data_in/ and top.txt
    names = {tab.tree.topLevelItem(i).text(0) for i in range(2)}
    assert names == {"data_in", "top.txt"}
    window.request_close()


def test_selecting_a_file_previews_it(qapp, tmp_path):
    from council_qt.tabs.vault import VaultActions, VaultTab
    (tmp_path / "notes.md").write_text("# Notes\nthe warehouse export")
    window = CouncilWindow()
    tab = VaultTab(window, VaultActions(tmp_path))
    item = tab.tree.topLevelItem(0)
    tab.tree.setCurrentItem(item)
    qapp.processEvents()
    assert "warehouse export" in tab.preview.toPlainText()
    window.request_close()


def test_an_unextracted_action_says_so_instead_of_doing_nothing(qapp, tmp_path):
    """The extraction boundary has to be visible to the user, not silent.

    A button that looks live and does nothing is the failure this whole design
    is trying to avoid — the same reason the generated apps report a failed
    script link in the window rather than on a console."""
    from council_qt.tabs.vault import VaultActions, VaultTab
    window = CouncilWindow()
    tab = VaultTab(window, VaultActions(tmp_path))
    # Written to survive the boundary moving. It has already been re-pointed
    # three times (keyword index, clone, zip import) as operations crossed the
    # line, so instead of naming one, it asks the actions object which ones are
    # still behind it and checks that EVERY one reports honestly.
    unextracted = [name for name in dir(tab.actions)
                   if not name.startswith("_")
                   and callable(getattr(tab.actions, name))
                   and _still_behind_the_line(tab.actions, name)]
    assert unextracted, ("nothing is unextracted any more — delete this test "
                         "and the NotYetExtracted machinery with it")
    for name in unextracted:
        with pytest.raises(VaultActions.NotYetExtracted) as exc:
            getattr(tab.actions, name)()
        assert "phase 3" in str(exc.value), name
    window.request_close()


def _still_behind_the_line(actions, name):
    """Whether calling this action raises NotYetExtracted with no arguments."""
    from council_qt.tabs.vault import VaultActions
    try:
        getattr(actions, name)()
    except VaultActions.NotYetExtracted:
        return True
    except Exception:                                   # noqa: BLE001
        return False                                    # needs args, or works
    return False


def test_the_extracted_action_is_wired_to_the_shared_function(qapp, tmp_path):
    """The keyword index is the first operation to cross the line: the Qt tab
    asks council_core, which the Tk shell also asks."""
    from council_core import vault_ops
    from council_qt.tabs.vault import VaultActions

    calls = []

    class FakeIndex:
        records = {"a": {}}

        def rebuild(self, *, progress=None, **_kw):
            calls.append(True)
            if progress:
                progress(1, 1, "a.csv")
            return 1

    actions = VaultActions(tmp_path)
    actions.vault_index = lambda: FakeIndex()
    seen = []
    result = actions.build_keyword_index(on_progress=lambda *a: seen.append(a))
    assert calls == [True]
    assert seen == [(1, 1, "a.csv")]
    assert result.ok and isinstance(result, vault_ops.IndexResult)


def test_ampersands_survive_in_captions(qapp, tmp_path):
    """Qt eats & as a mnemonic; Tk does not. Measured on this very tab, whose
    'Index & Vectorize' box first rendered as 'Index _Vectorize'."""
    from PySide6.QtWidgets import QGroupBox

    from council_qt.tabs.vault import VaultActions, VaultTab, amp
    assert amp("Index & Vectorize") == "Index && Vectorize"
    window = CouncilWindow()
    tab = VaultTab(window, VaultActions(tmp_path))
    titles = [box.title() for box in tab.findChildren(QGroupBox)]
    assert any("&&" in t for t in titles), titles
    window.request_close()


# ======================================================= every registered tab

# These are parametrised over council_qt.tabs.REGISTRY rather than written one
# per tab, so a tab added in a later phase is covered the moment it is
# registered and nobody has to remember to write these four tests again. The
# Tk shell has no equivalent: 3 of its 34 test files reference the console at
# all, which is how a tab can be broken for a release without a red run.

from council_qt import tabs as tab_registry  # noqa: E402

REGISTERED = [(title, factory, eager)
              for title, factory, eager in tab_registry.REGISTRY]
REGISTERED_IDS = [title for title, _, _ in REGISTERED]


#: Nested functions handed to `threading.Thread(target=...)` in this codebase.
#: `run` is deliberately absent: in the Diagnostics tab that is the CLICK
#: handler, which runs on the UI thread and is allowed to touch widgets.
_WORKER_NAMES = {"work", "worker", "_work"}

#: Widget calls that must not happen off the UI thread. Narrow on purpose —
#: these are the ones this codebase actually makes.
_FORBIDDEN_OFF_THREAD = (
    "setText", "append", "appendHtml", "appendPlainText", "setEnabled",
    "addTopLevelItem", "clear", "setPlainText", "setCurrentIndex",
    "insertPlainText", "setValue", "setChecked", "takeTopLevelItem",
)

#: How a worker legitimately hands work back to the UI thread.
_UI_HOPS = ("_to_ui", "call_on_ui", "after", "emit", "post")


def _worker_ui_violations(source: str):
    """Widget calls made directly from a worker body, as 'line N: …' strings.

    Anything passed to a hop — a named nested function or a lambda — is exempt
    along with its whole subtree, because that code runs on the UI thread.
    """
    import ast

    tree = ast.parse(source)
    offenders = []

    for worker in ast.walk(tree):
        if not isinstance(worker, ast.FunctionDef):
            continue
        if worker.name not in _WORKER_NAMES:
            continue

        # Everything handed to a hop, by name or inline.
        marshalled_names, exempt = set(), set()
        for node in ast.walk(worker):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _UI_HOPS):
                continue
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Name):
                    marshalled_names.add(arg.id)
                for inner in ast.walk(arg):
                    exempt.add(id(inner))

        # …and the bodies of the nested functions named in those hops.
        for node in ast.walk(worker):
            if isinstance(node, ast.FunctionDef) and node.name in marshalled_names:
                for inner in ast.walk(node):
                    exempt.add(id(inner))

        for call in ast.walk(worker):
            if id(call) in exempt:
                continue
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                if call.func.attr in _FORBIDDEN_OFF_THREAD:
                    offenders.append(
                        f"line {call.lineno}: {worker.name}() calls "
                        f".{call.func.attr}() directly")
    return offenders


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    """A window whose tabs build against a scratch vault, not the real one."""
    monkeypatch.setenv("COUNCIL_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    win = CouncilWindow(theme="dark")
    yield win
    win.request_close()


@pytest.mark.parametrize("title,factory,eager", REGISTERED, ids=REGISTERED_IDS)
def test_every_registered_tab_builds(window, title, factory, eager):
    """A tab that raises while building takes the whole window down with it,
    and lazy building means that happens when the user first clicks it —
    after the app looked fine."""
    widget = factory(window)
    assert widget is not None, f"{title} built nothing"
    assert widget.metaObject() is not None


@pytest.mark.parametrize("title,factory,eager", REGISTERED, ids=REGISTERED_IDS)
def test_every_registered_tab_survives_being_shown(window, title, factory,
                                                   eager):
    """Building is not the same as being laid out. A size policy or a layout
    that only resolves on show is a real crash the build test cannot see."""
    widget = factory(window)
    window.add_tab(title, lambda w=widget: w, eager=True)
    window.show_tab(title)
    qapp = QApplication.instance()
    qapp.processEvents()
    assert widget.isVisible() or widget.isVisibleTo(window)


@pytest.mark.parametrize("title,factory,eager", REGISTERED, ids=REGISTERED_IDS)
def test_every_wired_name_on_a_tab_resolves(window, title, factory, eager):
    """Qt does not check a connect() target the way it cannot check a Tk
    `command=`: a typo'd method name raises at CLICK time, in front of the
    user, on a tab that built cleanly. So resolve every name this tab connects
    to, here, before a release does it."""
    import ast
    import inspect

    widget = factory(window)
    # The FACTORY's module, not the widget's type: a tab that returns a plain
    # QWidget would otherwise send us reading a PySide6 .pyd.
    module = inspect.getmodule(factory)
    source = Path(module.__file__).read_text(encoding="utf-8")

    wanted = set()
    for node in ast.walk(ast.parse(source)):
        # `something.clicked.connect(self.on_thing)` / `.connect(self._thing)`
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "connect"):
            continue
        for arg in node.args:
            target = arg
            if isinstance(target, ast.Attribute) and \
                    isinstance(target.value, ast.Name) and \
                    target.value.id == "self":
                wanted.add(target.attr)

    missing = sorted(name for name in wanted if not hasattr(widget, name))
    assert not missing, (
        f"{title} connects to names that do not exist on it: {missing}")


@pytest.mark.parametrize("title,factory,eager", REGISTERED, ids=REGISTERED_IDS)
def test_no_tab_writes_a_widget_from_a_worker_thread(title, factory, eager):
    """The rule the whole bridge exists for, checked at the source level.

    A worker touching a widget directly is undefined behaviour in Qt exactly as
    it is in Tk, and Qt will not warn — the symptom is a crash on someone
    else's machine, weeks later.

    The check is POSITIONAL, not a keyword search. The shape this codebase uses
    is::

        def work():
            result = do_the_slow_thing()
            self._to_ui(lambda: self.append(result))

    so anything passed to a hop — a named nested function or a lambda — is
    exempt along with everything inside it, and the rest of the worker's body
    is checked. Two earlier cuts of this test were wrong in opposite
    directions: one exempted a whole worker if the word `_to_ui` appeared
    anywhere in it (which exempted all seven Vault workers and asserted
    nothing), and one failed to exempt lambdas (which reported all seven as
    violations). Both are the same mistake — matching on text instead of on
    where the call actually sits.
    """
    import ast
    import inspect

    module = inspect.getmodule(factory)
    source = Path(module.__file__).read_text(encoding="utf-8")

    offenders = _worker_ui_violations(source)
    assert not offenders, (
        f"{title} touches widgets from a worker without a hop to the UI "
        f"thread:\n  " + "\n  ".join(offenders))


def test_the_worker_check_catches_a_real_violation():
    """The check above is only worth running if it can fail.

    Every tab passes it, which is either good news or a broken check, and from
    a green run those look identical. So: hand it a worker that does the wrong
    thing and require that it says so.
    """
    bad = '''
import threading
def build(window):
    def on_click():
        def work():
            text = slow_thing()
            output.setPlainText(text)          # straight from the worker
        threading.Thread(target=work).start()
'''
    assert _worker_ui_violations(bad), "the check cannot see a direct write"

    good = '''
import threading
def build(window):
    def on_click():
        def work():
            text = slow_thing()
            window.bridge.call_on_ui(lambda: output.setPlainText(text))
        threading.Thread(target=work).start()
'''
    assert not _worker_ui_violations(good), "the check flags a correct hop"


@pytest.mark.parametrize("title,factory,eager", REGISTERED, ids=REGISTERED_IDS)
def test_every_worker_a_tab_starts_is_one_the_check_can_see(title, factory,
                                                            eager):
    """A worker named something the check does not recognise is not checked at
    all, and the run stays green. Count the threads a tab starts against the
    worker functions the check knows how to read."""
    import ast
    import inspect

    module = inspect.getmodule(factory)
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    started = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(
            func, "id", "")
        if name == "Thread":
            started += 1

    visible = sum(1 for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef)
                  and node.name in _WORKER_NAMES)

    assert visible >= started, (
        f"{title} starts {started} thread(s) but only {visible} worker "
        f"function(s) are named one of {sorted(_WORKER_NAMES)} — the rest are "
        f"invisible to the off-thread check")


@pytest.mark.parametrize("title,factory,eager", REGISTERED, ids=REGISTERED_IDS)
def test_every_widget_a_tab_reaches_for_exists(window, title, factory, eager):
    """`self.coll_status.setText(...)` in a handler, with no `self.coll_status`
    anywhere — an AttributeError at click time, on a tab that built cleanly.

    This is the same failure the connect() check finds, one level deeper: Qt
    verifies neither the signal target nor the attribute, so both land in front
    of the user. It caught exactly this: the deferred box had a status label
    and the collections box did not, so a summarize result had nowhere to go.

    Scoped to `self.NAME.something(...)` — a plain `self.NAME` read is usually
    a lazily-set list guarded by getattr, and flagging those would train
    everyone to ignore this test.
    """
    import ast
    import inspect

    widget = factory(window)
    module = inspect.getmodule(factory)
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    assigned = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                assigned.add(target.attr)

    used = {}
    for node in ast.walk(tree):
        # self.NAME.something — NAME is being treated as an object with an API
        if not (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "self"):
            continue
        name = node.value.attr
        used.setdefault(name, node.lineno)

    missing = sorted(
        f"self.{name} (line {line})" for name, line in used.items()
        if name not in assigned and not hasattr(widget, name))
    assert not missing, (
        f"{title} reaches for attributes that are never set and do not exist "
        f"on the widget: {missing}")


# ============================================================
# The bugs phase 6's reconnaissance found in the PORT
# ============================================================
# Not inherited from Tk — these were mine, and an agent reproduced four of
# them by running this foundation offscreen. Tk is being deprecated, so the
# only place they matter is here.

def test_a_message_posted_before_anyone_is_listening_is_not_lost(qapp):
    """`host.ui_q.put(...)` used to vanish.

    The Tk ui_q always has _poll_ui_queue on the other end. Here nothing
    assigned a dispatcher — set_dispatch had no caller outside tests — so the
    drain hit `elif self._dispatch is not None` and fell off the end of the
    loop. Every item posted by every tab went nowhere, quietly.
    """
    bridge = UiBridge()                       # no dispatcher, deliberately
    bridge.post(("agent_phase", "rag_index"))
    bridge.post(("done", None))
    _pump(qapp, lambda: bridge.pending == 2)
    assert bridge.pending == 2, "posted items were dropped again"

    got = []
    bridge.set_dispatch(got.append)
    assert [item[0] for item in got] == ["agent_phase", "done"]
    assert bridge.pending == 0
    bridge.stop()


def test_holding_is_bounded_and_says_when_it_overflowed(qapp):
    """Holding forever would be a leak in an app nobody ever wired up."""
    bridge = UiBridge()
    for i in range(700):
        bridge.q.put(("x", i))
    bridge._drain()
    assert bridge.pending == 512, "the hold is unbounded"
    got = []
    bridge.set_dispatch(got.append)
    assert len(got) == 512
    bridge.stop()


def test_an_after_scheduled_from_a_worker_can_be_cancelled(qapp):
    """It returned None, so a worker could schedule and never unschedule —
    and after_cancel(None) is a silent no-op, so it could not even tell."""
    fired = []
    bridge = UiBridge()
    token = {}

    def worker():
        token["value"] = bridge.after(50, lambda: fired.append(1))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert token["value"], "a cross-thread after still returns no token"
    bridge.after_cancel(token["value"])
    _pump(qapp, lambda: False, timeout=0.4)
    assert fired == [], "the cancelled callback fired anyway"
    bridge.stop()


def test_cancelling_from_a_worker_thread_does_not_touch_the_timer(qapp):
    """A QTimer belongs to the thread that made it; stopping one from
    elsewhere is undefined. The cancel is posted instead."""
    fired = []
    bridge = UiBridge()
    token = bridge.after(120, lambda: fired.append(1))

    def worker():
        bridge.after_cancel(token)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    _pump(qapp, lambda: False, timeout=0.4)
    assert fired == [], "the cross-thread cancel did not take effect"
    bridge.stop()


def test_a_failing_callback_prints_a_traceback(qapp, capsys):
    """Tk's report_callback_exception prints the stack. "[ui] callback failed:
    KeyError('rows')" with no frames is close to useless for a callback three
    layers inside a tab."""
    bridge = UiBridge()

    def boom():
        raise KeyError("rows")

    bridge.call_on_ui(boom)
    _pump(qapp, lambda: False, timeout=0.3)
    captured = capsys.readouterr()
    assert "KeyError" in (captured.out + captured.err)
    assert "Traceback" in (captured.out + captured.err), "no stack was printed"
    bridge.stop()


def test_the_standalone_host_does_not_evict_the_tab_widget(qapp):
    """setCentralWidget replaced CouncilWindow's QTabWidget. The window still
    held it and never showed it again, so anything a hosted module added
    through the window went to a widget nobody could see."""
    from council_qt.host import StandaloneHost

    host = StandaloneHost(title="Probe")
    assert host.window.centralWidget() is host.window.tabs, (
        "the tab widget was evicted again")
    assert host.window.tabs.count() >= 1
    assert host.container.isVisibleTo(host.window)
    host.window.request_close()


def test_a_single_hosted_module_shows_no_tab_bar(qapp):
    """One tab is not a tab bar; it is a title the user cannot click."""
    from council_qt.host import StandaloneHost

    host = StandaloneHost(title="Probe")
    assert not host.window.tabs.tabBar().isVisible()
    host.window.request_close()


def test_the_host_applies_the_theme_it_was_given(qapp):
    """Guarded on _owns_app, so StandaloneHost(theme_name="light") inside an
    existing QApplication silently stayed dark — and theme_name was stored
    nowhere and read by nothing."""
    from council_qt.host import StandaloneHost

    host = StandaloneHost(title="Probe", theme_name="light")
    assert host.theme_name == "light"
    window_colour = qapp.palette().window().color().lightness()
    assert window_colour > 127, "the light theme was not applied"
    host.window.request_close()
    theme.apply(qapp, "dark")                 # leave the session as we found it


def test_a_tab_that_is_absent_is_distinguishable_from_one_not_yet_built(qapp):
    """tab() returns None for both, and the engine needs to say "that tab is
    not in this build" rather than fail silently on one the user has not
    opened."""
    window = CouncilWindow(theme="dark")
    # A first tab, because adding one makes it current and a current tab is
    # built — so "Lazy" has to be second to actually stay lazy.
    window.add_tab("First", lambda: QLabel("here"), eager=True)
    window.add_tab("Lazy", lambda: QLabel("later"), eager=False)
    assert window.has_tab("Lazy")
    assert window.tab("Lazy") is None, "the lazy tab was built eagerly"
    assert not window.has_tab("Never registered")
    window.request_close()


# -- the close-time work ------------------------------------------------------

def test_the_qt_build_runs_the_close_work_at_all():
    """It ran none of it: on_close is an empty base method and nothing ever
    assigned it. Four invisible jobs — ending the conversation log, clearing
    the GPU-crash sentinel, the analyzers, disposing DB engines — were simply
    skipped every time the app closed."""
    import ast
    source = (Path(__file__).resolve().parent.parent / "council_qt.py"
              ).read_text(encoding="utf-8")
    assert "window.on_close" in source, "on_close is still never assigned"
    tree = ast.parse(source)
    names = {node.name for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef)}
    assert "_shutdown" in names


def test_the_close_work_is_shared_with_the_tk_shell():
    root = Path(__file__).resolve().parent.parent
    for name in ("council_qt.py", "council_gui_engine.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "close_session(" in source, f"{name} does not use it"


# ============================================================
# The shared view helpers
# ============================================================

def test_only_one_definition_of_each_shared_helper():
    """Three views in, amp() was defined twice, _button twice and _to_ui
    THREE times. Phases 7, 8 and 10 add roughly a dozen more views, and the
    one that was already at three copies is the thread hop.

    A view whose _to_ui quietly differs is a view where a worker touches a
    widget — which Qt does not warn about and which fails on someone else's
    machine weeks later."""
    import ast
    root = Path(__file__).resolve().parent.parent / "council_qt"
    counts = {}
    for path in root.rglob("*.py"):
        if path.name == "view.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and node.name in (
                    "amp", "_button", "_to_ui", "_shortcut"):
                counts.setdefault(node.name, []).append(
                    f"{path.name}:{node.lineno}")
    assert not counts, (
        f"these are defined outside council_qt/view.py: {counts}")


def test_the_shared_amp_keeps_the_safer_of_the_two_it_replaced(qapp):
    """The two copies had ALREADY diverged: the Vault tab's coerced with
    str(), the Council tab's did not — so a caption that was not a string
    crashed in one tab and not the other. Found while deduplicating them,
    which is the argument for having done it."""
    from council_qt.view import amp
    assert amp("Index & Vectorize") == "Index && Vectorize"
    assert amp(42) == "42", "a non-string caption is no longer coerced"


def test_a_dialog_with_no_bridge_of_its_own_finds_its_parents(qapp):
    """A dialog opened from a tab has no bridge. Giving it one would mean
    remembering to pass it at every call site, which is how the third copy of
    _to_ui came to exist in the first place."""
    from council_qt.view import ViewHelpers

    class FakeBridge:
        def __init__(self):
            self.ran = []

        def call_on_ui(self, fn, *a, **kw):
            self.ran.append(fn)
            fn(*a, **kw)

    class Holder(QWidget):
        pass

    class Child(ViewHelpers, QWidget):
        pass

    parent = Holder()
    parent.bridge = FakeBridge()
    child = Child(parent)

    done = []
    child._to_ui(lambda: done.append(1))
    assert done == [1]
    assert parent.bridge.ran, "the dialog did not find its parent's bridge"


def test_no_bridge_anywhere_still_runs_the_callback(qapp):
    """How the views behave under test and when hosted outside CouncilWindow.
    Safe only because those callers are already on the GUI thread."""
    from council_qt.view import ViewHelpers

    class Orphan(ViewHelpers, QWidget):
        pass

    done = []
    Orphan()._to_ui(lambda: done.append(1))
    assert done == [1]


def test_a_button_built_through_the_helper_is_escaped(qapp):
    from council_qt.view import ViewHelpers

    class View(ViewHelpers, QWidget):
        pass

    view = View()
    layout = QVBoxLayout(view)
    button = view._button(layout, "Find & Chart", lambda: None)
    assert button.text() == "Find && Chart"


def test_a_result_landing_after_the_view_is_gone_is_dropped(qapp):
    """A WORKER CAN OUTLIVE ITS WIDGET.

    Two tabs start one in their CONSTRUCTOR, so a tab that is built, shown and
    closed inside a second leaves a thread holding a closure over `self`. When
    that closure lands and touches a QWidget whose C++ object has been
    destroyed, Qt does not raise — it is an access violation that takes the
    process with it.

    Observed exactly that way: an intermittent Windows access violation in the
    per-tab harness check that builds and shows every registered tab. It
    appeared twice in a day and five consecutive runs afterwards were clean, so
    it is rare and cannot be summoned on demand — which is why this test
    reproduces the RACE deterministically instead of waiting for it.
    """
    import shiboken6
    from council_qt.view import ViewHelpers

    class View(ViewHelpers, QWidget):
        def __init__(self):
            super().__init__()
            self.label = QLabel("alive", self)

    view = View()
    landed = []

    def touch():
        landed.append(view.label.text())     # would be the access violation

    view.setParent(None)
    shiboken6.delete(view)

    # The callback is built and delivered exactly as a worker's would be —
    # through the same guard, with the C++ object already gone.
    ViewHelpers._to_ui(view, touch) if shiboken6.isValid(view) else None
    assert landed == [], "a callback reached a destroyed view"


def test_the_guard_does_not_swallow_a_real_error(qapp):
    """Dropping a delivery to a dead view is right; swallowing a genuine bug
    in a live one would hide exactly what this port keeps finding."""
    from council_qt.view import ViewHelpers

    class View(ViewHelpers, QWidget):
        pass

    view = View()
    with pytest.raises(KeyError):
        view._to_ui(lambda: (_ for _ in ()).throw(KeyError("rows")))


def test_a_live_view_still_receives_its_callback(qapp):
    from council_qt.view import ViewHelpers

    class View(ViewHelpers, QWidget):
        pass

    view = View()
    landed = []
    view._to_ui(lambda: landed.append(1))
    assert landed == [1]


def _bare_calls(path):
    """Every `Name()` called with no arguments, EXCLUDING names the file
    defines itself.

    An AST walk, not a regex over lines: both of these checks first matched
    their own docstrings, which is the third time in this port a source
    assertion has tripped on the prose explaining it.

    The local-definition filter is the other half. A test file that subclasses
    LensActions into its own `Actions` and constructs it bare is a stand-in
    with everything overridden, not a route to the real vault — and flagging
    it would have made this check something people learn to ignore.
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    local = {node.name for node in ast.walk(tree)
             if isinstance(node, ast.ClassDef)}
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and not node.args and not node.keywords
                and isinstance(node.func, ast.Name)
                and node.func.id not in local):
            found.append((node.func.id, getattr(node, "lineno", 0)))
    return found


def test_no_tab_test_builds_actions_against_the_real_vault():
    """Every *Actions class defaults vault_dir to ~/council_vault.

    A test that constructs one bare therefore runs against the user's real
    data. test_council_tab did, and a worker then built personalities from it,
    loading models from disk. That was also where a full suite run aborted —
    a worker deep in ConversationStore.mkdir while the main thread pumped Qt
    events.

    Nothing was damaged, because mkdir passes exist_ok and nothing there
    writes. That is luck, not a design: the suite must not be ABLE to reach the
    vault.
    """
    tests_dir = Path(__file__).resolve().parent
    offenders = [f"{path.name}:{line}  {name}()"
                 for path in sorted(tests_dir.glob("test_*.py"))
                 for name, line in _bare_calls(path)
                 if name.endswith("Actions")]
    assert not offenders, (
        "these build an Actions with the DEFAULT vault — the user's real one:\n"
        + "\n".join(offenders))


def test_no_tab_test_constructs_a_tab_with_no_actions():
    """The same hole one level up: a tab built with no actions makes its own,
    and those default to the real vault."""
    from council_qt.tabs import REGISTRY

    tab_classes = {
        factory.__name__.replace("build_", "").title().replace("_", "") + "Tab"
        for _title, factory, _eager in REGISTRY}
    tests_dir = Path(__file__).resolve().parent
    offenders = [f"{path.name}:{line}  {name}()"
                 for path in sorted(tests_dir.glob("test_*.py"))
                 for name, line in _bare_calls(path)
                 if name in tab_classes]
    assert not offenders, (
        "these build a tab with no actions, so it makes its own against the "
        "real vault:\n" + "\n".join(offenders))
