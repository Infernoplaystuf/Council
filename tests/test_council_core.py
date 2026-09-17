"""
council_core — the shared logic layer, and the rule that keeps it shared.

Needs no toolkit at all, which is the point: if these tests ever require
tkinter or PySide6 to run, something has leaked into the layer both front ends
import and the port has quietly become a fork.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import vault_ops  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOOLKITS = {"tkinter", "PySide6", "PyQt5", "PyQt6", "PySide2", "shiboken6"}


# ------------------------------------------------------------------ the rule

@pytest.mark.parametrize("module", sorted(
    p.name for p in (ROOT / "council_core").glob("*.py")))
def test_council_core_imports_no_toolkit(module):
    """The same guard the repo already puts on the gui_* modules.

    A toolkit import here would mean the shared layer can only be used by one
    front end, which is the whole failure mode this package exists to prevent.
    """
    src = (ROOT / "council_core" / module).read_text(encoding="utf-8")
    banned = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            banned += [a.name.split(".")[0] for a in node.names
                       if a.name.split(".")[0] in TOOLKITS]
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in TOOLKITS:
                banned.append(root)
    assert not banned, f"council_core/{module} imports {banned}"


def test_both_front_ends_call_the_same_function():
    """The extraction is only worth anything if BOTH sides use it.

    Asserted on the source rather than by running two GUIs: what matters is
    that neither front end has its own copy of what indexing means, and that is
    a property of the code, not of a run."""
    tk_src = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    qt_src = (ROOT / "council_qt" / "tabs" / "vault.py").read_text(encoding="utf-8")
    for src, who in ((tk_src, "the Tk shell"), (qt_src, "the Qt tab")):
        assert "vault_ops" in src, f"{who} does not use council_core.vault_ops"
        assert "build_keyword_index" in src

    # And the Tk shell may not still be doing the work itself. Bounded by the
    # AST rather than by a character count: the next method along
    # (_vmgr_build_descriptions) legitimately calls rebuild-like APIs of its
    # own, and a fixed window reaches into it.
    tree = ast.parse(tk_src)
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_vmgr_build_keyword_index")
    body = ast.get_source_segment(tk_src, method) or ""
    assert "vault_ops.build_keyword_index" in body
    assert ".rebuild(" not in body, (
        "the Tk shell still calls rebuild() directly — the extraction did not "
        "take")


# --------------------------------------------------------- build_keyword_index

class _FakeIndex:
    """Stands in for VaultIndex: rebuild(progress=...) -> count."""

    def __init__(self, files=("a.csv", "b.csv"), fail=False):
        self.files = list(files)
        self.fail = fail
        self.records = {name: {} for name in self.files}

    def rebuild(self, *, progress=None, **_kw):
        if self.fail:
            raise OSError("vault is unreadable")
        for i, name in enumerate(self.files, 1):
            if progress:
                progress(i, len(self.files), name)
        return len(self.files)


def test_a_successful_build_reports_what_it_did():
    result = vault_ops.build_keyword_index(_FakeIndex())
    assert result.ok
    assert result.indexed == 2 and result.total == 2
    assert "2 files (re)indexed" in result.message


def test_progress_is_forwarded_untouched():
    """VaultIndex already fires progress(done, total, name); this layer passes
    it through rather than inventing a second protocol."""
    seen = []
    vault_ops.build_keyword_index(_FakeIndex(("one.csv", "two.csv")),
                                  on_progress=lambda *a: seen.append(a))
    assert seen == [(1, 2, "one.csv"), (2, 2, "two.csv")]


def test_a_missing_index_is_an_answer_not_an_exception():
    """The shell's lazy getter returns None when the index cannot be built.
    Returning a result keeps the caller's success and failure paths identical."""
    result = vault_ops.build_keyword_index(None)
    assert not result.ok
    assert result.message == "Vault index unavailable."


def test_a_failing_rebuild_is_reported_not_raised():
    """An unreadable file is a normal outcome; the user needs the reason, not a
    traceback on a console they are not looking at."""
    result = vault_ops.build_keyword_index(_FakeIndex(fail=True))
    assert not result.ok
    assert "failed" in result.message and "unreadable" in result.message
    assert isinstance(result.error, OSError)


def test_long_names_are_trimmed_the_same_way_for_both_front_ends():
    """Both shells show this line. If each trimmed its own way, the same index
    run would read differently depending on which front end ran it."""
    long_name = "a_very_long_export_filename_from_the_warehouse_system.csv"
    line = vault_ops.progress_line(3, 9, long_name)
    assert line.startswith("Indexing 3/9 — ")
    assert line.endswith("…")
    assert len(vault_ops.short_name(long_name)) <= vault_ops.NAME_LIMIT
    assert vault_ops.short_name("short.csv") == "short.csv"
