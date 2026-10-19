"""
Tests for the Ledger→KG rebuild hook on the Ledger tab + the Ledger
breakdown on the KG rebuild summary line.

The real knowledge_graph.KnowledgeGraph is not opened — the Ledger tab's
LedgerActions accepts a ``rebuild_kg`` override that lets the test substitute
a fake that returns a stats dict.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the Ledger tab needs PySide6")

from PySide6.QtWidgets import QApplication                               # noqa: E402

from council_core import knowledge_graph as kgm                           # noqa: E402
from council_qt.tabs.ledger import LedgerActions, LedgerTab               # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------- rebuild_summary_line (ledger)

def test_summary_names_the_people_when_a_ledger_was_read():
    line = kgm.rebuild_summary_line({
        "documents": 5, "ledgers": 1, "ledger_people": 3,
        "unreadable": [], "relations": {"seeded": 7},
    })
    assert "1 ledger" in line
    assert "3 people" in line
    assert "7 links" in line


def test_summary_uses_singular_person_when_one_person_is_named():
    line = kgm.rebuild_summary_line({
        "documents": 1, "ledgers": 1, "ledger_people": 1,
    })
    assert "1 ledger" in line
    assert "1 person" in line


def test_summary_omits_the_people_part_when_the_ledger_had_none():
    line = kgm.rebuild_summary_line({
        "documents": 1, "ledgers": 1, "ledger_people": 0,
    })
    assert "1 ledger" in line
    assert "people" not in line
    assert "person" not in line


def test_summary_without_a_ledger_does_not_claim_one():
    line = kgm.rebuild_summary_line({
        "documents": 10, "workbooks": 10, "ledger_people": 4,
    })
    # No "ledgers" key / zero → the people from a dangling earlier run must
    # not surface in a workbook-only summary.
    assert "ledger" not in line.lower()
    assert "people" not in line


# -------------------------------------------- LedgerActions.rebuild_kg hook

def test_rebuild_kg_delegates_to_the_override_when_one_is_supplied(qapp):
    seen = {}

    def fake(progress):
        seen["ran"] = True
        return {"documents": 42, "ledgers": 2, "ledger_people": 5}

    actions = LedgerActions(rebuild_kg=fake)
    out = actions.rebuild_kg(lambda line, frac=None: None)
    assert seen.get("ran")
    assert out["documents"] == 42
    assert out["ledgers"] == 2


# ---------------------------------------------- Ledger tab wiring

def _wait_for_idle(tab, qapp, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while tab.busy and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()


def test_rebuild_kg_button_runs_the_hook_and_shows_the_summary(qapp, tmp_path):
    stats = {"documents": 100, "ledgers": 1, "ledger_people": 4,
             "workbooks": 2, "unreadable": [], "relations": {"seeded": 11},
             "seconds": 0.5}

    def fake(progress):
        progress("rebuilding", 0.5)
        return stats

    actions = LedgerActions(rebuild_kg=fake)
    tab = LedgerTab(actions=actions)
    try:
        tab.rebuild_kg_btn.click()
        _wait_for_idle(tab, qapp)
        text = tab.status.text()
        assert "Rebuilt from 100 document" in text
        assert "1 ledger" in text
        assert "4 people" in text
        assert "11 links" in text
    finally:
        tab.deleteLater()


def test_rebuild_kg_button_appears_bold_after_a_successful_apply(qapp):
    from council_core.ledger_ingest import ApplySummary

    def fake_apply(review, fill_dir, use_images, progress):
        return ApplySummary(accepted=5, rows=5, csv_result="x.csv",
                            learned={}, ignored=[], filled=[], problems=[])

    # Bare LedgerActions() would be flagged by
    # test_no_tab_test_builds_actions_against_the_real_vault — pass an
    # opener so the AST check sees a kwarg. The vault is never reached
    # here anyway (apply is overridden), but the check cannot tell.
    actions = LedgerActions(opener=lambda _p: None)
    actions.apply = fake_apply
    tab = LedgerTab(actions=actions)
    try:
        tab.review.setText("some_review.xlsx")
        assert not tab.rebuild_kg_btn.font().bold()
        tab.apply_btn.click()
        _wait_for_idle(tab, qapp)
        assert tab._kg_dirty is True
        assert tab.rebuild_kg_btn.font().bold()
    finally:
        tab.deleteLater()


def test_rebuild_kg_button_clears_bold_after_a_rebuild(qapp):
    from council_core.ledger_ingest import ApplySummary

    def fake_apply(review, fill_dir, use_images, progress):
        return ApplySummary(accepted=1, rows=1, csv_result="y.csv",
                            learned={}, ignored=[], filled=[], problems=[])

    def fake_rebuild(progress):
        return {"documents": 1, "ledgers": 1, "ledger_people": 1}

    actions = LedgerActions(rebuild_kg=fake_rebuild)
    actions.apply = fake_apply
    tab = LedgerTab(actions=actions)
    try:
        tab.review.setText("r.xlsx")
        tab.apply_btn.click()
        _wait_for_idle(tab, qapp)
        assert tab._kg_dirty
        tab.rebuild_kg_btn.click()
        _wait_for_idle(tab, qapp)
        assert tab._kg_dirty is False
        assert not tab.rebuild_kg_btn.font().bold()
    finally:
        tab.deleteLater()
