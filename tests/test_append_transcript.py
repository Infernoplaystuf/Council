"""
Reports from other tabs reach the Council transcript.

THE GAP
The IDE, Librarian and Nodes tabs each end an operation with

    append = getattr(self.window, "append_transcript", None)
    if append is None:
        return

— and CouncilWindow had no append_transcript, so every one of those reports
(the snapshot path, the commit receipt with its SHA, the node rebuild result)
was dropped without a word. The Tk shell writes all of them into the Council
transcript.

These tests drive the real window, the real Council tab and, for the end-to-
end case, the real IDE tab saving a real snapshot into a temp vault.
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
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip("PySide6", reason="the Qt window needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.council import CouncilActions, CouncilTab  # noqa: E402
from council_qt.window import CouncilWindow  # noqa: E402

COUNCIL = "⚖ Council"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(root))
    return root


@pytest.fixture
def window(qapp, vault):
    """The app's window with the Council tab registered the way the app
    registers it: eagerly, first."""
    win = CouncilWindow()
    win.add_tab(COUNCIL,
                lambda: CouncilTab(win, actions=CouncilActions(
                    vault_dir=vault, demo_mode=True), demo_mode=True),
                eager=True)
    yield win
    deadline = time.monotonic() + 5.0
    while (any(t.name.startswith("ide-") and t.is_alive()
               for t in threading.enumerate())
           and time.monotonic() < deadline):
        qapp.processEvents()
        time.sleep(0.005)
    win.request_close()
    qapp.processEvents()


def _transcript(win):
    return win.tab(COUNCIL).transcript.toPlainText()


def test_the_window_has_the_method_the_tabs_look_for(qapp):
    """The three tabs reach for it with getattr(..., None) and return quietly
    when it is missing — so its absence was invisible."""
    win = CouncilWindow()
    try:
        assert callable(getattr(win, "append_transcript", None))
    finally:
        win.request_close()


def test_an_ide_snapshot_is_reported_in_the_council_transcript(
        qapp, window, vault):
    """End to end, all real: the IDE tab saves the buffer through the
    engine's Librarian into the temp vault, and its report lands in the
    Council transcript, labelled as coming from the IDE."""
    from council_qt.tabs.ide import IdeActions, IdeTab

    ide = IdeTab(window, actions=IdeActions(vault_dir=vault))
    ide.code.setPlainText("print('hello from the snapshot')\n")
    ide.on_snapshot()
    assert _pump(qapp, lambda: "snapshot" in _transcript(window), 20.0), (
        "the IDE's report never reached the Council transcript")
    text = _transcript(window)
    saved = list(vault.glob("council_code_*.py"))
    assert saved, "the snapshot was not written"
    assert str(saved[0]) in text
    assert "IDE" in text, f"the notice does not say where it came from:\n{text}"


def test_a_report_from_a_worker_thread_lands_on_the_gui_thread(qapp, window):
    """The tabs call it from their UI-thread callbacks today, but a report is
    exactly the kind of thing a worker will be tempted to send directly. It
    must hop to the GUI thread rather than write a widget from the worker."""
    tab = window.tab(COUNCIL)
    landed_on = []
    real = tab.append

    def recording(*args, **kwargs):
        landed_on.append(threading.current_thread() is threading.main_thread())
        return real(*args, **kwargs)

    tab.append = recording
    worker = threading.Thread(
        target=lambda: window.append_transcript(
            "Librarian", "rebuilt from a worker", "final", source="Nodes"),
        name="test-notice")
    worker.start()
    worker.join(5)
    assert _pump(qapp, lambda: "rebuilt from a worker" in _transcript(window))
    assert landed_on and all(landed_on), (
        "the transcript was written from the worker thread")


def test_a_notice_is_labelled_and_is_never_the_answer(qapp, window):
    """A notice is the app reporting on another tab, not a personality
    answering. Kind "final" from a caller must not make it the last answer —
    Save answer would then save a commit receipt."""
    tab = window.tab(COUNCIL)
    window.append_transcript("Writer", "a commit receipt", "final",
                             source="Librarian")
    text = _transcript(window)
    assert "a commit receipt" in text
    assert "Notice" in text and "Librarian" in text, text
    assert tab._last_answer is None
    assert not tab.save_frame.isVisibleTo(tab)


def test_a_notice_before_the_council_tab_exists_is_kept_and_shown(qapp,
                                                                  vault):
    """Lazily built, or not built yet: the notice waits for the transcript
    rather than vanishing, and the status bar carries it meanwhile."""
    from PySide6.QtWidgets import QWidget

    win = CouncilWindow()
    try:
        # The first tab added is built at once (it is current), so a plain
        # page goes first and the Council tab stays a placeholder.
        win.add_tab("Elsewhere", QWidget, eager=True)
        win.add_tab(COUNCIL, lambda: CouncilTab(
            win, actions=CouncilActions(vault_dir=vault, demo_mode=True),
            demo_mode=True))
        assert win.tab(COUNCIL) is None, "the Council tab was built eagerly"
        win.append_transcript("Librarian", "held for later", "final",
                              source="Nodes")
        assert "held for later" in win._status.text()
        win.show_tab(COUNCIL)                    # builds it
        qapp.processEvents()
        assert win.tab(COUNCIL) is not None
        assert "held for later" in _transcript(win)
    finally:
        win.request_close()
