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

# "Setup needed" is decided by whether a model can answer
# (council_core.model_ready), not by the Tk wizard's .onboarded marker. These
# used to stub onboarding.needs_onboarding — which is why they passed while
# the notice was wrong both ways — and now build the state on disk.

@pytest.fixture
def no_model(monkeypatch, tmp_path):
    """Nothing that could answer: no exported or saved model, no backend
    override, the Ollama fallback off (as the sandbox already has it)."""
    for var in ("COUNCIL_GGUF_PATH", "COUNCIL_GGUF_PATH_AUTO",
                "COUNCIL_BACKEND", "COUNCIL_OLLAMA_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
    return tmp_path / "vault"


def _status_of(window):
    """FakeWindow records it; the real CouncilWindow shows it in a label."""
    label = getattr(window, "_status", None)
    return label.text() if label is not None else window.status


def test_an_unconfigured_vault_is_reported_on_screen(qapp, no_model):
    """The wizard is Tk-only for now. A user left silently without a model gets
    an app whose every answer is "no judge model is loaded", and no idea why.

    The real window, and a vault that HAS the Tk wizard's .onboarded marker:
    the marker said "done", so this vault got no notice at all before."""
    from council_qt.window import CouncilWindow
    no_model.mkdir(parents=True, exist_ok=True)
    (no_model / ".onboarded").write_text("{}", encoding="utf-8")
    app, window, plan = build(window_factory=CouncilWindow)
    try:
        assert plan.onboarding
        assert _pump_until(app, lambda: bool(_status_of(window)))
        assert "Setup needed" in _status_of(window)
        assert "Models tab" in _status_of(window)
    finally:
        window.request_close()


def test_a_configured_vault_says_nothing(qapp, no_model, monkeypatch,
                                         tmp_path):
    """A setup notice on an app that is already set up is noise, and noise is
    what teaches people to ignore the status bar.

    The real window, a model saved in the app and NO .onboarded marker —
    the setup_council.py install, which was told "Setup needed" every launch."""
    import json

    from council_core import model_ready
    from council_qt.window import CouncilWindow
    # llama-cpp-python is not installed where these tests run; this stands in
    # for a machine where it is.
    monkeypatch.setattr(model_ready, "gguf_loader_available", lambda: True)
    model = tmp_path / "models" / "granite.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"GGUF" + b"\0" * 64)
    no_model.mkdir(parents=True, exist_ok=True)
    (no_model / "backend_settings.json").write_text(
        json.dumps({"gguf_path": str(model)}), encoding="utf-8")
    app, window, plan = build(window_factory=CouncilWindow)
    try:
        assert not plan.onboarding, plan.onboarding_reason
        _pump_until(app, lambda: False, seconds=1.0)
        assert _status_of(window) == ""
    finally:
        window.request_close()


def test_a_host_with_a_wizard_gets_to_use_it(qapp, no_model):
    """The seam for the moment a Qt wizard exists: give the window an
    `open_onboarding` and it is called instead of the notice."""
    opened = []

    class WithWizard(FakeWindow):
        def open_onboarding(self, **kwargs):
            opened.append(kwargs)

    app, _window, _plan = build(window_factory=WithWizard)
    assert _pump_until(app, lambda: bool(opened))
    assert "vault_dir" in opened[0]


def test_a_failing_wizard_is_reported_and_survived(qapp, no_model, capsys):
    """An app that will not start because its optional setup wizard is unhappy
    has turned a nudge into a wall — but a wizard that fails SILENTLY leaves a
    user unconfigured with no idea why. So: reported, and not fatal.

    The first version of this only asserted `window is not None`, which an
    exception escaping a Qt slot does not disturb — so it passed with the
    handler deleted and proved nothing.
    """
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


# ============================================================
# The vault's folders, and an upgrader's old files
# ============================================================

@pytest.fixture
def upgrader(tmp_path, monkeypatch):
    """An app folder from an older build, with its files where it kept them,
    and the migration switched on — pointed at THIS test's folders only. The
    repo root is redirected too: the real one is this checkout, and a move
    out of it into a temp vault would be a move into the bin."""
    from council_core import vault_setup
    app = tmp_path / "app"
    app.mkdir()
    (app / "node_registry.json").write_text('{"nodes": ["pi"]}',
                                            encoding="utf-8")
    (app / "personality_backends.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COUNCIL_APP_DIR", str(app))
    monkeypatch.setenv("COUNCIL_SKIP_PATH_MIGRATION", "0")
    monkeypatch.setattr(vault_setup, "REPO_ROOT", tmp_path / "repo")
    return app


def test_a_launch_moves_an_upgraders_files_into_the_vault(qapp, tmp_path,
                                                          upgrader):
    """The Tk engine does this at import; the Qt launch never did, so a user
    upgrading straight into Qt found their node registry and model pins
    "gone" — still in the old app folder, unread."""
    build()
    vault = tmp_path / "vault"
    assert (vault / "node_registry.json").read_text() == '{"nodes": ["pi"]}'
    assert (vault / "personality_backends.json").exists()
    assert not (upgrader / "node_registry.json").exists()


def test_a_launch_sets_up_the_data_folders(qapp, tmp_path):
    """data_in/ is where every message about adding data points; a fresh Qt
    vault did not have one."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    (vault / "orders.csv").write_text("id\n1\n", encoding="utf-8")
    build()
    assert (vault / "data_in" / "README.txt").is_file()
    assert (vault / "data_out" / "README.txt").is_file()
    assert (vault / "data_in" / "orders.csv").is_file()
    for folder in ("logs", "workspace", "tmp"):
        assert (vault / folder).is_dir(), folder


def test_a_launch_does_not_file_the_apps_settings_as_data(qapp, tmp_path,
                                                          monkeypatch):
    """The review's probe, as a test: a model saved the way the app saves one
    (onboarding.save_gguf_path, model_slots.save), then a Qt launch. On the
    base there was no data_in/ at all; with batch 0 both files were copied
    into it and the data index offered them as datasets."""
    import onboarding
    from council_core import model_slots

    # save_gguf_path also exports the choice; recorded so it is put back.
    monkeypatch.setenv("COUNCIL_GGUF_PATH", "placeholder")
    monkeypatch.delenv("COUNCIL_GGUF_PATH")
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    model = tmp_path / "models" / "granite.gguf"
    model.parent.mkdir()
    model.write_bytes(b"GGUF" + b"\0" * 64)
    assert onboarding.save_gguf_path(vault, str(model)) is None
    model_slots.save(vault, model_slots.SlotConfig(slots={
        "main": model_slots.Slot("main", str(model)),
        "coding": model_slots.Slot("coding", "ollama:llama3.1:8b")}))
    assert (vault / "backend_settings.json").is_file()
    assert (vault / "model_slots.json").is_file()
    build()
    build()
    landed = sorted(p.name for p in (vault / "data_in").iterdir())
    assert landed == ["README.txt"], landed


def test_a_launch_respects_the_skip_switch(qapp, tmp_path, upgrader,
                                           monkeypatch):
    monkeypatch.setenv("COUNCIL_SKIP_PATH_MIGRATION", "1")
    build()
    assert (upgrader / "node_registry.json").exists()
    assert not (tmp_path / "vault" / "node_registry.json").exists()


# ============================================================
# The saved engine settings
# ============================================================

ENGINE_VARS = ("COUNCIL_GGUF_N_CTX", "COUNCIL_GGUF_GPU_LAYERS",
               "COUNCIL_EMBED_DEVICE")


@pytest.fixture
def engine_env(monkeypatch):
    """The three engine variables unset for the test AND put back after it.
    setenv first, so monkeypatch records the original state: delenv alone
    records nothing for a variable that was absent, and whatever the launch
    set would then leak into every later test."""
    for var in ENGINE_VARS:
        monkeypatch.setenv(var, "placeholder")
        monkeypatch.delenv(var)


def _engine_settings(tmp_path, **saved):
    import json
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "backend_settings.json").write_text(json.dumps(saved),
                                                 encoding="utf-8")


def test_the_engine_settings_saved_in_the_vault_are_applied(qapp, tmp_path,
                                                            engine_env):
    """A context size saved in Tk's Engine dialog was silently dropped by the
    Qt app: the Tk console copies the saved knobs into the environment the
    engine reads at model load, and the Qt launch never did."""
    _engine_settings(tmp_path, n_ctx="16384", gpu_layers="20",
                     embed_device="cuda")
    build()
    assert os.environ.get("COUNCIL_GGUF_N_CTX") == "16384"
    assert os.environ.get("COUNCIL_GGUF_GPU_LAYERS") == "20"
    assert os.environ.get("COUNCIL_EMBED_DEVICE") == "cuda"


def test_an_exported_engine_setting_beats_the_saved_one(qapp, tmp_path,
                                                        engine_env,
                                                        monkeypatch):
    """Tk's precedence, kept: a launch-time export is how a user gets out of
    a saved setting that crashes the GPU."""
    _engine_settings(tmp_path, n_ctx="16384", gpu_layers="20")
    monkeypatch.setenv("COUNCIL_GGUF_GPU_LAYERS", "0")
    build()
    assert os.environ.get("COUNCIL_GGUF_GPU_LAYERS") == "0"
    assert os.environ.get("COUNCIL_GGUF_N_CTX") == "16384"


def test_a_blank_saved_setting_is_not_applied(qapp, tmp_path, engine_env):
    """The Engine dialog saves "" for a field left blank."""
    _engine_settings(tmp_path, n_ctx="", gpu_layers="", embed_device="")
    build()
    for var in ENGINE_VARS:
        assert var not in os.environ, var


def test_the_tk_console_applies_them_through_the_same_function():
    """One rule for both front ends, so they cannot disagree about which
    value wins."""
    from tests.source_checks import code_of

    source = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    body = code_of(source, "_load_backend_settings")
    assert "engine_settings.apply(" in body
    assert "COUNCIL_GGUF_N_CTX" not in body, (
        "the Tk console still applies the knobs with its own loop")
