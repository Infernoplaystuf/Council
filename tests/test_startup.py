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

def test_a_fresh_vault_needs_onboarding(tmp_path, monkeypatch):
    import onboarding
    monkeypatch.setattr(onboarding, "needs_onboarding", lambda _v: True)
    decided = startup.plan(tmp_path)
    assert decided.onboarding
    assert decided.onboarding_reason


def test_a_configured_vault_does_not(tmp_path, monkeypatch):
    import onboarding
    monkeypatch.setattr(onboarding, "needs_onboarding", lambda _v: False)
    assert startup.plan(tmp_path).onboarding is False


def test_a_vault_that_cannot_be_read_does_not_force_setup(tmp_path,
                                                          monkeypatch):
    """Showing the wizard because a path could not be read walks a configured
    user back through setup they already did."""
    import onboarding

    def _boom(_vault):
        raise OSError("drive not ready")

    monkeypatch.setattr(onboarding, "needs_onboarding", _boom)
    decided = startup.plan(tmp_path)
    assert decided.onboarding is False
    assert "could not check" in decided.onboarding_reason


def test_a_broken_vault_does_not_stop_the_launch(tmp_path, monkeypatch):
    import onboarding

    def _boom(_vault):
        raise OSError("drive not ready")

    monkeypatch.setattr(onboarding, "needs_onboarding", _boom)
    startup.plan(tmp_path)          # must not raise
