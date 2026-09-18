"""
The startup chain, driven.

This is the one code path where every failure is invisible — no window, no
error — so it is also the one that most needs to be runnable without a display.
`build()` stops short of `app.exec()` for exactly that reason.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import startup  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("PySide6", reason="the launch chain needs PySide6")

from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from council_qt import launch as qt_launch  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


class FakeWindow(QMainWindow):
    """A window that records what the chain did to it."""

    def __init__(self):
        super().__init__()
        self.closed = False
        self.status = ""
        self.on_close = None

    def request_close(self):
        self.closed = True

    def set_status(self, text):
        self.status = text


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Its own vault, and no splash window — this suite must open nothing."""
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("COUNCIL_NO_SPLASH", "1")


def _pump_until(app, predicate, seconds=8.0):
    deadline = time.time() + seconds
    while time.time() < deadline and not predicate():
        app.processEvents()
        time.sleep(0.01)
    return predicate()


def build(**kwargs):
    kwargs.setdefault("window_factory", FakeWindow)
    return qt_launch.build([], **kwargs)


# ============================================================
# The order
# ============================================================

def test_the_window_is_built_hidden(qapp):
    """Reveal before the build finishes and the user watches an empty window
    fill in."""
    _app, window, _plan = build()
    assert not window.isVisible()


def test_it_reveals_itself_without_being_asked(qapp):
    """Three independent guarantees, and a test that pumps must see at least
    one of them fire."""
    app, window, _plan = build()
    assert _pump_until(app, window.isVisible), "the window never appeared"


def test_the_crash_hooks_are_installed_before_the_window(qapp, monkeypatch):
    """A failure during construction is otherwise an unhandled traceback in a
    console the user may not have."""
    order = []
    import crash_reporter
    monkeypatch.setattr(crash_reporter, "install",
                        lambda *a, **k: order.append("hooks"))

    class Recording(FakeWindow):
        def __init__(self):
            order.append("window")
            super().__init__()

    build(window_factory=Recording)
    assert order[:2] == ["hooks", "window"]


def test_a_broken_crash_reporter_does_not_stop_the_launch(qapp, monkeypatch):
    """A crash reporter that stops the launch is worse than no crash reporter,
    because the failure it causes is the one nobody expected."""
    import crash_reporter

    def _boom(*_a, **_k):
        raise RuntimeError("no vault")

    monkeypatch.setattr(crash_reporter, "install", _boom)
    _app, window, _plan = build()
    assert window is not None


def test_the_crash_hook_does_not_build_a_widget(qapp):
    """It is called from whichever thread died. Building a widget there is an
    access violation on Windows, not an error message."""
    from tests.source_checks import code_of

    source = (ROOT / "council_qt" / "launch.py").read_text(encoding="utf-8")
    body = code_of(source, "_report_crash")
    for widget_ish in ("QMessageBox", "QDialog", "QWidget", "show_dialog"):
        assert widget_ish not in body


# ============================================================
# Shutdown
# ============================================================

def test_the_close_work_is_wired(qapp):
    """`on_close` is an empty base method, and nothing assigning it meant
    closing the app did NONE of the close-time work for the whole of phase 5."""
    called = []
    _app, window, _plan = build(shutdown=lambda: called.append(1))
    assert window.on_close is not None
    window.on_close()
    assert called == [1]


def test_quitting_however_it_happens_closes_the_window(qapp):
    """aboutToQuit fires for the window's X, quit(), and a session logout, so
    teardown hangs off that rather than off one path remembering to call it."""
    app, window, _plan = build()
    app.aboutToQuit.emit()
    assert window.closed


# ============================================================
# Interactive hosts
# ============================================================

def test_an_interactive_host_is_revealed_immediately(qapp, monkeypatch):
    """No deferred timers: the host's loop may pump them only intermittently,
    and a window that appears "eventually" reads as one that never appeared."""
    monkeypatch.setattr(startup, "is_interactive_host", lambda: True)
    _app, window, plan = build()
    assert plan.interactive
    assert window.isVisible(), "an interactive host waited on a timer"


def test_an_interactive_host_gets_no_splash_window(qapp, monkeypatch):
    monkeypatch.setattr(startup, "is_interactive_host", lambda: True)
    _app, _window, plan = build()
    assert plan.show_splash is False


# ============================================================
# Onboarding
# ============================================================

def test_an_unconfigured_vault_is_reported_on_screen(qapp, monkeypatch):
    """The wizard is Tk-only for now. A user left silently without a model gets
    an app whose every answer is "no judge model is loaded", and no idea why."""
    import onboarding
    monkeypatch.setattr(onboarding, "needs_onboarding", lambda _v: True)
    app, window, plan = build()
    assert plan.onboarding
    assert _pump_until(app, lambda: bool(window.status))
    assert "Setup needed" in window.status
    assert "Models tab" in window.status


def test_a_configured_vault_says_nothing(qapp, monkeypatch):
    """A setup notice on an app that is already set up is noise, and noise is
    what teaches people to ignore the status bar."""
    import onboarding
    monkeypatch.setattr(onboarding, "needs_onboarding", lambda _v: False)
    app, window, _plan = build()
    _pump_until(app, lambda: False, seconds=1.0)
    assert window.status == ""


def test_a_host_with_a_wizard_gets_to_use_it(qapp, monkeypatch):
    """The seam for the moment a Qt wizard exists: give the window an
    `open_onboarding` and it is called instead of the notice."""
    import onboarding
    monkeypatch.setattr(onboarding, "needs_onboarding", lambda _v: True)
    opened = []

    class WithWizard(FakeWindow):
        def open_onboarding(self, **kwargs):
            opened.append(kwargs)

    app, _window, _plan = build(window_factory=WithWizard)
    assert _pump_until(app, lambda: bool(opened))
    assert "vault_dir" in opened[0]


def test_a_failing_wizard_is_reported_and_survived(qapp, monkeypatch, capsys):
    """An app that will not start because its optional setup wizard is unhappy
    has turned a nudge into a wall — but a wizard that fails SILENTLY leaves a
    user unconfigured with no idea why. So: reported, and not fatal.

    The first version of this only asserted `window is not None`, which an
    exception escaping a Qt slot does not disturb — so it passed with the
    handler deleted and proved nothing.
    """
    import onboarding
    monkeypatch.setattr(onboarding, "needs_onboarding", lambda _v: True)

    class Broken(FakeWindow):
        def open_onboarding(self, **_kwargs):
            raise RuntimeError("wizard is broken")

    app, window, _plan = build(window_factory=Broken)
    _pump_until(app, lambda: False, seconds=1.5)
    assert "onboarding failed" in capsys.readouterr().out, (
        "the wizard blew up and nothing said so")
    # And the app is still running and still usable afterwards.
    assert not window.closed
    window.set_status("still alive")
    assert window.status == "still alive"


# ============================================================
# Tabs
# ============================================================

def test_the_tabs_are_registered_before_the_reveal(qapp, monkeypatch):
    """Otherwise the window appears and then grows tabs, which is exactly the
    "empty window filling in" the splash exists to hide.

    Tested under an INTERACTIVE host, because that is the only path where the
    ordering can actually break: there the reveal is a direct call rather than
    a timer, so scheduling it before registration really would show an empty
    window. On the normal path a timer cannot fire before `build()` returns,
    which made the first version of this test unable to fail.
    """
    monkeypatch.setattr(startup, "is_interactive_host", lambda: True)
    seen = []

    class Recording(FakeWindow):
        def show(self):
            seen.append("shown")
            super().show()

    _app, window, _plan = build(
        window_factory=Recording,
        register=lambda w: seen.append("registered"))
    assert window.isVisible()
    assert seen and seen[0] == "registered", (
        f"the window was revealed before its tabs existed: {seen}")


def test_the_vault_is_created_if_it_is_not_there(qapp, tmp_path):
    """A launch that resolves a vault path and then fails on every read because
    nothing made the directory is a worse first run than no vault at all."""
    build()
    assert (tmp_path / "vault").is_dir()
