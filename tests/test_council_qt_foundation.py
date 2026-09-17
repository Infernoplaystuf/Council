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
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

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

    The transcript coalescing depends on draining everything available in one
    pass rather than one message per tick."""
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
    """The first real tab, end to end: a worker gathers, the bridge delivers."""
    from council_qt.tabs.diagnostics import build_diagnostics
    window = CouncilWindow()
    window.add_tab("Diagnostics", lambda: build_diagnostics(window), eager=True)
    page = window.tab("Diagnostics")
    from PySide6.QtWidgets import QPlainTextEdit
    output = page.findChild(QPlainTextEdit)
    assert _pump(qapp, lambda: "python" in output.toPlainText())
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
