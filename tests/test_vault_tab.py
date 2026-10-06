"""
The Vault tab's index buttons, driven through the REAL tab and the REAL
actions object.

WHY A FILE OF ITS OWN
Two one-line stubs at the bottom of VaultActions —

    def build_descriptions(self):  self._later("Descriptions")
    def build_embeddings(self):    self._later("Embeddings")

— were defined AFTER the real methods, so Python kept the stubs. The tab calls
`build_descriptions(on_progress=...)`, the stub takes no such argument, and the
worker died of a TypeError: nothing in the log, "Generating descriptions…" on
the status line for good, and all three index buttons disabled for the rest of
the session. Nothing caught it because the tests that existed injected fakes
with the right signature, or probed the actions with no arguments — which the
stubs answered with an honest-looking NotYetExtracted.

So these tests construct nothing fake above the core: a CouncilWindow (with its
bridge, so delivery is queued as in the app), a VaultTab, a VaultActions over a
temp vault, and the tab's own click handlers.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip("PySide6", reason="the Vault tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_core import vault_ops  # noqa: E402
from council_qt.tabs.vault import VaultActions, VaultTab  # noqa: E402
from council_qt.window import CouncilWindow  # noqa: E402
from tests.fake_ollama import refuse_egress  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A temp vault with one CSV — a TABULAR file, which the description
    builder describes from its schema with no model call at all."""
    refuse_egress(monkeypatch)          # nothing here may reach a real model
    root = tmp_path / "vault"
    (root / "data_in").mkdir(parents=True)
    (root / "data_in" / "orders.csv").write_text(
        "order_id,customer,total\n1,acme,5.00\n2,globex,7.50\n",
        encoding="utf-8")
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(root))
    return root


@pytest.fixture
def tab(qapp, vault):
    window = CouncilWindow()
    view = VaultTab(window, VaultActions(vault))
    yield view
    # Let any index worker post back before the window goes.
    _pump(qapp, lambda: view._btn_keyword.isEnabled(), timeout=10.0)
    window.request_close()
    qapp.processEvents()


def _buttons(view):
    return (view._btn_keyword, view._btn_descriptions, view._btn_embeddings)


def _all_enabled(view):
    return all(button.isEnabled() for button in _buttons(view))


def _log(view):
    return view.log.toPlainText()


# ============================================================
# The stubs are gone: the real operations run
# ============================================================

def test_the_actions_object_keeps_the_real_index_methods():
    """The stubs were later definitions of the same names, so they won.
    A signature check is the cheapest way to say "the real one is in force"."""
    import inspect
    for name in ("build_descriptions", "build_embeddings"):
        params = inspect.signature(getattr(VaultActions, name)).parameters
        assert "on_progress" in params, (
            f"VaultActions.{name} is a stub again: {list(params)}")


def test_build_descriptions_runs_from_the_tab_and_gives_the_buttons_back(
        tab, qapp):
    """The whole path, real: keyword index, then Descriptions from the tab's
    own handler. A CSV is described from its schema, so this needs no model —
    and the outcome is the operation's own sentence, not a TypeError."""
    keyword = tab.actions.build_keyword_index()
    assert keyword.ok, keyword.message

    tab.on_descriptions()
    assert not tab._btn_descriptions.isEnabled(), (
        "the run did not disable the buttons — it may never have started")
    assert _pump(qapp, lambda: _all_enabled(tab)), (
        "the index buttons never came back; log:\n" + _log(tab))
    assert "Descriptions complete" in tab.index_status.text(), (
        tab.index_status.text())
    assert "TypeError" not in _log(tab)


# ============================================================
# A failure gives the buttons back too
# ============================================================

def test_a_descriptions_run_that_raises_still_gives_the_buttons_back(
        tab, qapp, monkeypatch):
    """The worker used to be `result = run(on_progress)` with nothing around
    it: anything that escaped the operation killed the thread before the
    re-enable was posted. vault_ops catches what it expects; this is the
    case it does not."""
    keyword = tab.actions.build_keyword_index()
    assert keyword.ok, keyword.message

    def _boom(index, on_progress=None):
        raise RuntimeError("the index file vanished mid-run")

    monkeypatch.setattr(vault_ops, "build_descriptions", _boom)
    tab.on_descriptions()
    assert _pump(qapp, lambda: _all_enabled(tab)), (
        "a failing run left all three index buttons disabled")
    said = tab.index_status.text() + "\n" + _log(tab)
    assert "vanished mid-run" in said, said


def test_an_embeddings_run_that_raises_still_gives_the_buttons_back(
        tab, qapp, monkeypatch):
    """Same seam, the other layer. The start line is stubbed only so the
    test never loads a sentence-transformers model (it is installed here,
    and a real run would download one)."""
    monkeypatch.setattr(
        vault_ops, "starting_embeddings",
        lambda index: vault_ops.IndexResult(True, "Embedding 1 files…",
                                            total=1))

    def _boom(index, on_progress=None):
        raise MemoryError("the embedder ran out of memory")

    monkeypatch.setattr(vault_ops, "build_embeddings", _boom)
    tab.on_embeddings()
    assert _pump(qapp, lambda: _all_enabled(tab)), (
        "a failing run left all three index buttons disabled")
    said = tab.index_status.text() + "\n" + _log(tab)
    assert "ran out of memory" in said, said


def test_the_failure_is_reported_as_one(tab, qapp, monkeypatch):
    """Not just buttons back: the line says it failed, in the error colour's
    level, so it cannot be read as a finished run."""
    keyword = tab.actions.build_keyword_index()
    assert keyword.ok, keyword.message
    levels = []
    real_append = tab.append
    monkeypatch.setattr(tab, "append",
                        lambda message, level="info": (
                            levels.append((message, level)),
                            real_append(message, level)))

    def _boom(index, on_progress=None):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(vault_ops, "build_descriptions", _boom)
    tab.on_descriptions()
    assert _pump(qapp, lambda: _all_enabled(tab))
    assert any("disk gone" in message and level == "err"
               for message, level in levels), levels


# ============================================================
# A failure the operation does NOT raise is still a failure
# ============================================================
# The two real failures a user meets — no model to describe a text file, an
# embedding model that cannot load (offline, not cached) — never raise:
# VaultIndex swallows them. generate_descriptions stores an empty
# description and counts the file as "updated"; build_embeddings prints to
# stderr and returns 0. So both came back as green success ("Descriptions
# complete — 1 files summarized.", "Vectors ready — 0 files (None-dim…)"),
# and the next click offered the same work again. Driven through the REAL
# VaultIndex; only the model call is stood in.

def _levels(tab, monkeypatch):
    levels = []
    real_append = tab.append
    monkeypatch.setattr(tab, "append",
                        lambda message, level="info": (
                            levels.append((message, level)),
                            real_append(message, level)))
    return levels


def test_a_text_file_no_model_could_describe_is_not_summarized(
        tab, qapp, vault, monkeypatch):
    import council_engine

    (vault / "data_in" / "notes.txt").write_text(
        "Shift notes: press 4 was down for two hours on Tuesday.\n",
        encoding="utf-8")
    assert tab.actions.build_keyword_index().ok

    def _no_model(*_a, **_k):
        raise council_engine.BackendUnavailable("no model is set")

    monkeypatch.setattr(council_engine, "local_chat", _no_model)
    levels = _levels(tab, monkeypatch)
    tab.on_descriptions()
    assert _pump(qapp, lambda: _all_enabled(tab))

    said = tab.index_status.text()
    assert "Descriptions complete" not in said, said
    assert "1 could not be described" in said, said
    assert "1 of 2 files summarized" in said, said    # the CSV needs no model
    assert any("could not be described" in message and level == "err"
               for message, level in levels), levels
    notes = [r for r in tab.actions.vault_index().records.values()
             if r.get("name") == "notes.txt"]
    assert notes and not notes[0].get("description"), notes


def test_an_embedding_model_that_cannot_load_is_a_failure(
        tab, qapp, monkeypatch):
    import vault_embeddings

    assert tab.actions.build_keyword_index().ok

    def _offline(self):
        raise RuntimeError(
            f"Could not load embedding model {self.model_name!r}: "
            "huggingface.co is unreachable\n\nFix one of these: …")

    monkeypatch.setattr(vault_embeddings.EmbeddingIndex, "_get_model",
                        _offline)
    levels = _levels(tab, monkeypatch)
    tab.on_embeddings()
    assert _pump(qapp, lambda: _all_enabled(tab))

    said = tab.index_status.text()
    assert "Vectors ready" not in said, said
    assert "Could not load embedding model" in said, said
    assert "Fix one of these" not in said, "the status line is one line"
    assert any("Could not load embedding model" in message and level == "err"
               for message, level in levels), levels
