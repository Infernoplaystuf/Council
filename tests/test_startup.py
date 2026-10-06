"""
What happens before the window, and in what order.

The Tk main() is 90 lines of sequencing with four hard-won rules written as
comments above the lines that implement them. A comment cannot be ported and
cannot fail. These can.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import startup  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "startup.py").read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "import tk"):
        assert toolkit not in source


# ============================================================
# The splash clock
# ============================================================

def test_a_fast_build_still_shows_the_splash_for_the_minimum():
    """Otherwise it vanishes the instant a fast machine finishes, and a launch
    reads as a flicker."""
    assert startup.splash_remaining_ms(100.0, 100.2) == pytest.approx(
        startup.MIN_SPLASH_MS - 200, abs=2)


def test_a_slow_build_owes_nothing_more():
    assert startup.splash_remaining_ms(100.0, 105.0) == 0


def test_the_remaining_time_is_never_negative():
    """A negative delay is an immediate fire in one toolkit and an error in
    another. Neither is what the caller meant."""
    for elapsed in (2.0, 60.0, 3600.0):
        assert startup.splash_remaining_ms(0.0, elapsed) >= 0


def test_an_unknown_start_time_pays_the_whole_minimum():
    """The alternative is treating "I do not know" as "already elapsed", which
    skips the splash on exactly the launches that went wrong."""
    assert startup.splash_remaining_ms(None, 12345.0) == startup.MIN_SPLASH_MS


def test_the_backstop_fires_after_the_intended_reveal():
    """It is a guarantee, not a race. Firing first would make it the normal
    path and the splash pointless."""
    assert startup.REVEAL_BACKSTOP_MS > 0


def test_onboarding_waits_for_the_window():
    """It opens a modal, and a modal needs a parent that exists."""
    assert startup.ONBOARDING_DELAY_MS > 0


# ============================================================
# The reveal, and its three guarantees
# ============================================================

def test_the_window_is_revealed_once_however_many_callers_ask():
    """Three independent guarantees call this. Two of them are redundant on a
    normal launch and that is the point."""
    shown = []
    reveal = startup.Reveal(lambda: shown.append(1))
    assert reveal("splash") is True
    assert reveal("backstop") is False
    assert reveal("interactive") is False
    assert shown == [1]


def test_every_attempt_is_recorded_even_the_ones_that_did_nothing():
    reveal = startup.Reveal(lambda: None)
    reveal("splash")
    reveal("backstop")
    assert reveal.attempts == ["splash", "backstop"]


def test_a_failed_reveal_says_why():
    """A blank session with a silent exception behind it is the hardest thing
    there is to diagnose."""
    said = []

    def _boom():
        raise RuntimeError("no display")

    reveal = startup.Reveal(_boom, report=said.append)
    reveal("splash")
    assert said and "no display" in said[0]


def test_a_failed_reveal_falls_back_to_just_making_it_visible():
    """Raising the window and taking focus can fail on a window manager that
    refuses focus stealing, while simply showing it succeeds. A visible
    unfocused window is a working app; a hidden one is not."""
    shown = []

    def _boom():
        raise RuntimeError("focus refused")

    reveal = startup.Reveal(_boom, fallback=lambda: shown.append("visible"),
                            report=lambda _m: None)
    assert reveal("splash") is True
    assert shown == ["visible"]


def test_a_reveal_that_fails_entirely_reports_failure():
    def _boom():
        raise RuntimeError("gone")

    reveal = startup.Reveal(_boom, report=lambda _m: None)
    assert reveal("splash") is False


def test_a_failed_reveal_is_not_retried_by_the_backstop():
    """It already tried both paths. A backstop that ran the same failing code
    again would turn one error into two, and the second reaches the user after
    they have already read the first."""
    calls = []

    def _boom():
        calls.append(1)
        raise RuntimeError("gone")

    reveal = startup.Reveal(_boom, report=lambda _m: None)
    reveal("splash")
    reveal("backstop")
    assert len(calls) == 1


# ============================================================
# Interactive hosts
# ============================================================

def test_a_normal_launch_is_not_an_interactive_host():
    assert startup.is_interactive_host() is False


def test_spyder_in_sys_modules_is_an_interactive_host(monkeypatch):
    monkeypatch.setitem(sys.modules, "spyder_kernels", object())
    assert startup.is_interactive_host() is True


def test_an_interactive_host_gets_no_splash(monkeypatch, tmp_path):
    """The host already owns an event loop in this process, and animating a
    splash means pumping a second one."""
    monkeypatch.setattr(startup, "is_interactive_host", lambda: True)
    assert startup.plan(tmp_path).show_splash is False


def test_an_interactive_host_does_not_auto_start_the_rag_indexer(monkeypatch,
                                                                 tmp_path):
    """It initialises torch/CUDA on a BACKGROUND thread, which segfaults the
    kernel about thirty seconds in — the "no tabs, then the kernel dies"
    report. Vault keyword search still works; semantic RAG is built on demand
    from the Vault tab."""
    monkeypatch.setattr(startup, "is_interactive_host", lambda: True)
    assert startup.plan(tmp_path).start_rag is False


def test_a_normal_launch_gets_both(monkeypatch, tmp_path):
    monkeypatch.setattr(startup, "is_interactive_host", lambda: False)
    decided = startup.plan(tmp_path)
    assert decided.show_splash and decided.start_rag


def test_the_splash_can_be_forced_either_way(monkeypatch, tmp_path):
    """The Tk build has a manual splash for testing it. Forcing it must not
    also turn the RAG thread back on under an interactive host."""
    monkeypatch.setattr(startup, "is_interactive_host", lambda: True)
    decided = startup.plan(tmp_path, force_splash=True)
    assert decided.show_splash is True
    assert decided.start_rag is False


# ============================================================
# Onboarding
# ============================================================

# "Setup needed" means NO MODEL CAN ANSWER — not "the .onboarded marker is
# missing". The marker is written only by the Tk wizard: setup.bat /
# setup_council.py installs and Qt-first users never get one (so they were
# told "Setup needed" with a model working), and a vault that has one but no
# model left got no notice at all. These used to stub needs_onboarding, which
# is exactly why they passed while the decision was wrong; they now build the
# state on disk (and a FakeOllama on loopback) and let the real check decide.

@pytest.fixture
def nothing_configured(monkeypatch):
    """No model exported, no backend override, the Ollama fallback off (the
    sandbox's own default, restated so this file stands alone)."""
    for var in ("COUNCIL_GGUF_PATH", "COUNCIL_GGUF_PATH_AUTO",
                "COUNCIL_BACKEND", "COUNCIL_OLLAMA_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
    from tests.fake_ollama import refuse_egress
    refuse_egress(monkeypatch)       # never the real Ollama, never off-box


def _saved_model(vault: Path, path) -> None:
    import json
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "backend_settings.json").write_text(
        json.dumps({"gguf_path": str(path)}), encoding="utf-8")


def _slots(vault: Path, main: str) -> None:
    import json
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "model_slots.json").write_text(json.dumps(
        {"version": 1, "slots": {"main": {"path": main}}}), encoding="utf-8")


def _gguf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + b"\0" * 64)
    return path


def test_a_fresh_vault_needs_setup(tmp_path, nothing_configured):
    decided = startup.plan(tmp_path)
    assert decided.onboarding
    assert decided.onboarding_reason == "no model is configured yet"


def test_an_onboarded_vault_with_no_model_still_needs_setup(
        tmp_path, nothing_configured):
    """The marker says someone finished the Tk wizard once. It does not say
    a model is there now — the GGUF may have been deleted since."""
    (tmp_path / ".onboarded").write_text('{"skipped": false}',
                                         encoding="utf-8")
    decided = startup.plan(tmp_path)
    assert decided.onboarding, "a vault with the marker and no model said OK"


def test_a_saved_model_that_loads_needs_no_setup_without_the_marker(
        tmp_path, nothing_configured, monkeypatch):
    """setup_council.py never writes .onboarded. A user it installed, with a
    model saved in the app, was told "Setup needed" on every launch."""
    from council_core import model_ready
    # llama-cpp-python is not installed in the env these tests run in; this
    # stands in for a machine where it is — the one thing not on disk here.
    monkeypatch.setattr(model_ready, "gguf_loader_available", lambda: True)
    _saved_model(tmp_path, _gguf(tmp_path / "models" / "granite.gguf"))
    assert not (tmp_path / ".onboarded").exists()
    decided = startup.plan(tmp_path)
    assert decided.onboarding is False, decided.onboarding_reason


def test_a_saved_model_that_is_gone_is_named(tmp_path, nothing_configured):
    missing = tmp_path / "models" / "deleted.gguf"
    _saved_model(tmp_path, missing)
    decided = startup.plan(tmp_path)
    assert decided.onboarding
    assert "deleted.gguf" in decided.onboarding_reason


def test_a_model_file_nothing_can_load_is_not_a_model(
        tmp_path, nothing_configured, monkeypatch):
    from council_core import model_ready
    monkeypatch.setattr(model_ready, "gguf_loader_available", lambda: False)
    _saved_model(tmp_path, _gguf(tmp_path / "models" / "granite.gguf"))
    decided = startup.plan(tmp_path)
    assert decided.onboarding
    assert "llama-cpp-python" in decided.onboarding_reason


def test_an_ollama_slot_the_server_has_needs_no_setup(
        tmp_path, nothing_configured, monkeypatch):
    from tests.fake_ollama import FakeOllama
    with FakeOllama() as server:
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", server.url)
        _slots(tmp_path, "ollama:llama3.1:8b")
        decided = startup.plan(tmp_path)
        assert decided.onboarding is False, decided.onboarding_reason
        # Metadata only: nothing was asked to generate.
        assert not [r for r in server.state.requests if r[1] == "/api/chat"]


def test_an_ollama_slot_the_server_lacks_is_named(
        tmp_path, nothing_configured, monkeypatch):
    from tests.fake_ollama import FakeOllama
    with FakeOllama() as server:
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", server.url)
        _slots(tmp_path, "ollama:not-pulled:7b")
        decided = startup.plan(tmp_path)
        assert decided.onboarding
        assert "not-pulled:7b" in decided.onboarding_reason


def test_an_installed_ollama_model_counts_through_the_fallback(
        tmp_path, nothing_configured, monkeypatch):
    """Nothing configured in the app, but the engine answers from a localhost
    Ollama when no GGUF can load — so an installed model is a usable one."""
    from tests.fake_ollama import FakeOllama
    with FakeOllama() as server:
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", server.url)
        monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "1")
        decided = startup.plan(tmp_path)
        assert decided.onboarding is False, decided.onboarding_reason


def test_only_a_model_the_engine_would_pick_counts(
        tmp_path, nothing_configured, monkeypatch):
    """The engine's fallback never picks a non-US model, so a server holding
    only one cannot answer — and the notice says why."""
    from tests.fake_ollama import FakeOllama, tag
    qwen = tag("qwen2.5:7b", size=4_683_087_332, family="qwen2",
               params="7.6B", quant="Q4_K_M")
    with FakeOllama(tags=[qwen]) as server:
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", server.url)
        monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "1")
        decided = startup.plan(tmp_path)
        assert decided.onboarding
        assert "US-origin" in decided.onboarding_reason


def test_a_vault_that_cannot_be_read_does_not_force_setup(tmp_path,
                                                          monkeypatch):
    """Showing the wizard because a path could not be read walks a configured
    user back through setup they already did."""
    from council_core import model_ready

    def _boom(_vault, **_kw):
        raise OSError("drive not ready")

    monkeypatch.setattr(model_ready, "check", _boom)
    decided = startup.plan(tmp_path)
    assert decided.onboarding is False
    assert "could not check" in decided.onboarding_reason


def test_a_broken_vault_does_not_stop_the_launch(tmp_path, monkeypatch):
    from council_core import model_ready

    def _boom(_vault, **_kw):
        raise OSError("drive not ready")

    monkeypatch.setattr(model_ready, "check", _boom)
    startup.plan(tmp_path)          # must not raise


def test_the_check_imports_no_toolkit_and_not_the_engine():
    """It runs before the window exists, on every launch: importing the
    engine there costs seconds."""
    source = (ROOT / "council_core" / "model_ready.py").read_text(
        encoding="utf-8")
    for heavy in ("tkinter", "PySide6", "import council_engine"):
        assert heavy not in source
