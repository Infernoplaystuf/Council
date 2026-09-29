"""
tests/sandbox_vault.py — the session's throwaway vault.

MEASURED 2026-09-29: a smoke_test run rewrote three index files in the user's
real ~/.council/vault, and other tests read (and cached) the user's real
model_slots.json. Every lookup the app makes must now land in the sandbox.
"""
from __future__ import annotations

import os
from pathlib import Path

from tests import sandbox_vault

REAL = (Path.home() / ".council" / "vault").resolve()
SANDBOX = Path(sandbox_vault.VAULT).resolve()


def test_the_session_vault_is_a_throwaway_one():
    assert SANDBOX != REAL
    assert SANDBOX.is_dir()
    assert Path(os.environ["COUNCIL_VAULT_ROOT"]).resolve() == SANDBOX


def test_the_apps_own_lookup_lands_in_it():
    from council_core import paths
    assert paths.vault_dir() == SANDBOX


def test_the_engines_lookups_land_in_it():
    """The engine resolves the vault itself for the GPU sentinel and, through
    model_slots, for the role map — the two reads of the real vault seen."""
    import council_engine
    from council_core import model_slots
    assert council_engine._gpu_sentinel_path().parent.resolve() == SANDBOX

    # The role map current() serves is the one saved in the sandbox.
    cfg = model_slots.SlotConfig(
        {model_slots.MAIN: model_slots.Slot(model_slots.MAIN),
         "fast": model_slots.Slot("fast", "C:/sandbox/fast.gguf")},
        {"peasant": "fast"})
    written = model_slots.save(SANDBOX, cfg)
    try:
        model_slots.invalidate()
        assert model_slots.current().roles == {"peasant": "fast"}
    finally:
        Path(written).unlink(missing_ok=True)
        model_slots.invalidate()


def test_a_test_can_still_point_somewhere_else(monkeypatch, tmp_path):
    from council_core import paths
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "mine"))
    assert paths.vault_dir() == (tmp_path / "mine").resolve()
