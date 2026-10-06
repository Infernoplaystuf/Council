"""
The Diagnostics tab: the dependency report, the real vault, and Copy.

Driven through the real tab in a real CouncilWindow — its worker, its bridge,
its buttons — over the session's sandbox vault (or this test's own).

WHAT WAS WRONG
It listed five package versions and said "vault: engine not loaded in this
process" — it read the vault out of the Tk engine module, which the Qt app
never imports. Tk's tab is mostly the optional-feature report (on the dev PC:
11 features missing, 8 counting SQLAlchemy's three drivers with it), and has
a Copy button. None of that was in Qt.
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

pytest.importorskip("PySide6", reason="the Diagnostics tab needs PySide6")

from PySide6.QtWidgets import (QApplication, QLabel,  # noqa: E402
                               QPlainTextEdit, QPushButton)

from council_qt.window import CouncilWindow  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def diagnostics(qapp, tmp_path, monkeypatch):
    """The tab, its report delivered. Its own vault, so the path it names is
    one this test knows."""
    vault = tmp_path / "my vault"
    vault.mkdir()
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    from council_qt.tabs.diagnostics import build_diagnostics
    window = CouncilWindow()
    window.add_tab("Diagnostics", lambda: build_diagnostics(window),
                   eager=True)
    page = window.tab("Diagnostics")
    output = page.findChild(QPlainTextEdit)
    assert _pump(qapp, lambda: "optional" in output.toPlainText()), (
        f"the report never arrived: {output.toPlainText()!r}")
    yield window, page, output, vault
    _pump(qapp, lambda: False, timeout=0.2)
    window.request_close()
    qapp.processEvents()


def _button(page, text):
    return next(b for b in page.findChildren(QPushButton)
                if text.lower() in b.text().lower())


def test_the_report_names_the_real_vault(diagnostics):
    _window, _page, output, vault = diagnostics
    text = output.toPlainText()
    assert "engine not loaded" not in text
    assert str(vault.resolve()) in text, text


def test_the_report_lists_every_missing_optional_feature(diagnostics):
    """The same check Tk's tab runs, with the same count — and each missing
    feature named with the line that installs it."""
    import dependency_check

    _window, _page, output, _vault = diagnostics
    text = output.toPlainText()
    statuses = dependency_check.check_all()
    missing = [s for s in statuses if not s.ok]
    if missing:
        assert f"Missing optional dependencies ({len(missing)})" in text
        for status in missing:
            assert status.spec.name in text, status.spec.name
            assert status.spec.install in text, status.spec.install
    else:
        assert "All optional dependencies are installed" in text
    assert f"Available optional features ({len(statuses) - len(missing)})" \
        in text or len(missing) == len(statuses)


def test_the_status_line_counts_them(diagnostics):
    import dependency_check

    _window, page, _output, _vault = diagnostics
    statuses = dependency_check.check_all()
    ok = sum(1 for s in statuses if s.ok)
    labels = " ".join(label.text() for label in page.findChildren(QLabel))
    assert f"✓ {ok} available" in labels, labels
    assert f"✗ {len(statuses) - ok} missing" in labels, labels


def test_copy_puts_the_whole_report_on_the_clipboard(qapp, diagnostics):
    _window, page, output, _vault = diagnostics
    clipboard = QApplication.clipboard()
    clipboard.setText("something else")
    _button(page, "Copy").click()
    qapp.processEvents()
    assert clipboard.text() == output.toPlainText()
    labels = " ".join(label.text() for label in page.findChildren(QLabel))
    assert "Copied" in labels, labels


def test_refresh_runs_it_again(qapp, diagnostics):
    _window, page, output, _vault = diagnostics
    output.setPlainText("stale")
    _button(page, "Re-check").click()
    assert _pump(qapp, lambda: "optional" in output.toPlainText())


def test_the_report_never_imports_the_engine():
    """Seconds, and CUDA DLLs loaded off the main thread, for a panel. Read
    from the code (import statements), not the text: the prose explaining
    why mentions the import it avoids."""
    import ast

    def imported(tree):
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    engines = {"council_engine", "council_gui_engine"}
    for path in (ROOT / "council_core" / "diagnostics.py",
                 ROOT / "council_qt" / "tabs" / "diagnostics.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not imported(tree) & engines, path.name
    source = (ROOT / "dependency_check.py").read_text(encoding="utf-8")
    summary = next(node for node in ast.parse(source).body
                   if isinstance(node, ast.FunctionDef)
                   and node.name == "system_summary")
    assert not imported(summary) & engines
