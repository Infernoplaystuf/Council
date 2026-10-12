"""
council_core.turn_context — the context a turn is given, shared by both shells.

The Qt Council sent only the typed question to the models: a fact planted in a
vault reached 0 of 13 prompts (docs/qt_migration/remaining_scope_2026-10-06.md).
The Tk shell's context code — file and folder injection, vault and value
matches, the analyst step — was 3,186 toolkit-free lines inside the Tk module,
which the Qt shell cannot import. It moved here; the Tk module re-exports it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "turn_context.py").read_text(
        encoding="utf-8")
    for toolkit in ("import tkinter", "from tkinter", "PySide6",
                    "council_gui_engine import", "import council_gui_engine"):
        assert toolkit not in source, toolkit


def test_the_tk_engine_calls_the_same_functions():
    """Re-exported, not copied: one body, two shells."""
    import council_gui_engine as cge
    from council_core import turn_context as tc
    for name in ("_inject_file_contents", "_inject_file_contents_impl",
                 "_run_analyst_step", "_extract_file_paths",
                 "_read_file_for_injection", "_get_vault_index",
                 "_get_data_index", "_register_data_index",
                 "_FILE_INJECT_CACHE", "looks_like_file_count"):
        assert getattr(cge, name) is getattr(tc, name), name


def test_the_rebound_instances_are_not_copied_into_the_tk_engine():
    """A copy of a name rebound later goes stale silently — so they are not
    re-exported at all, and a stale read fails loudly."""
    import council_gui_engine as cge
    for name in ("_VAULT_INDEX_INSTANCE", "_DATA_INDEX_INSTANCE",
                 "_REFERENCED_FILES_MAP"):
        assert not hasattr(cge, name), name


def test_setting_the_tk_vault_still_steers_the_moved_code(tmp_path,
                                                         monkeypatch):
    import council_gui_engine as cge
    from council_core import turn_context as tc
    monkeypatch.setattr(cge, "VAULT_DIR", tmp_path / "tk_vault")
    assert tc._vault() == tmp_path / "tk_vault"


def test_no_vault_directory_name_is_left_in_the_moved_code():
    """Every VAULT_DIR read became _vault(); a bare VAULT_DIR here would be a
    NameError on the first turn that reached it."""
    import re
    source = (ROOT / "council_core" / "turn_context.py").read_text(
        encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    assert not re.search(r"\bVAULT_DIR\b", code)
