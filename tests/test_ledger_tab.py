"""
The 📑 Ledger tab, driven like a user would: template -> parent CSV, a folder
of sources -> review workbook, the review checked -> apply with filled
templates. Offscreen (no window), no Excel, no file dialogs, a stand-in image
reader.

Run:  python -m pytest tests/test_ledger_tab.py -q
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from ledger_fixtures import (CONCEALMENT_IMAGE_ROWS, ITEMS, fake_reader,  # noqa: E402
                             make_laser_log, make_nde_book, make_summary, make_template)
from test_ledger_ingest import accept  # noqa: E402

from council_qt.tabs.ledger import LedgerActions, LedgerTab, build_ledger  # noqa: E402
from sample_ledger import ParentCSV  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def books(tmp_path):
    src = tmp_path / "sources"
    src.mkdir()
    make_laser_log(src / "laser_log.xlsx")
    make_nde_book(src / "NDE results.xlsx")
    make_summary(src / "summary.xlsx")
    return make_template(tmp_path / "LMM Template.xlsx"), src, tmp_path


def make_tab(qapp, **kw):
    opened = []
    actions = LedgerActions(image_reader=fake_reader(CONCEALMENT_IMAGE_ROWS),
                            opener=opened.append)
    for name, fn in kw.items():
        setattr(actions, name, fn)
    tab = LedgerTab(None, actions)
    return tab, opened


def test_the_whole_flow_from_the_tab(qapp, books):
    template, src, tmp = books
    tab, opened = make_tab(qapp)
    assert not tab.create_btn.isEnabled() and not tab.extract_btn.isEnabled()

    tab.template.setText(str(template))
    tab.create_btn.click()
    assert tab.wait()
    csv = Path(tab.csv.text())
    assert csv == tmp / "LMM_Template_parent.csv" and csv.exists()
    assert "created" in tab.status.text()

    tab.add_sources([src])
    tab.add_sources([src])                               # once only
    assert tab.source_paths() == [str(src)] and tab.extract_btn.isEnabled()
    tab.extract_btn.click()
    assert tab.wait()
    review = Path(tab.review.text())
    assert review.exists() and review.parent == tmp
    values, pre, todo = (int(tab.stat[k].text()) for k in ("values", "pre_accepted",
                                                            "to_review"))
    assert values == pre + todo and pre > 0 and todo > 0
    assert tab.stat["images"].text() == "1 / 1"
    assert tab.stat["problems"].text() == "0"
    assert "values found" in tab.log.toPlainText()

    tab.open_btn.click()
    assert opened == [str(review)]

    accept(review, [("101", "", "Power (W)", "285"),
                    ("101", None, "Flaw Attributes: Laser Team FOD (um)", None),
                    ("101", "", "Concealment 1: Media", None)])
    tab.fill_dir.setText(str(tmp / "filled"))
    tab.apply_btn.click()
    assert tab.wait()
    assert "rows added" in tab.status.text()
    ledger = ParentCSV(csv)
    assert ledger.sample_ids() == ["101", "102"]
    assert [r["Flaw #"] for r in ledger.rows_for("101")] == ITEMS
    r = ledger.get("101", "2B")
    assert r["Power (W)"] == "285" and r["Concealment 1: Media"] == "ceramic"
    assert r["Flaw Attributes: Laser Team FOD (um)"] == "404"
    assert sorted(p.name for p in (tmp / "filled").iterdir()) == [
        "LMM_Template_101.xlsx", "LMM_Template_102.xlsx"]


def test_work_runs_off_the_gui_thread_with_buttons_locked(qapp, books):
    template, src, tmp = books
    gate, ran_on = threading.Event(), []

    def slow_extract(csv, sources, part, use_images, progress):
        ran_on.append(threading.current_thread())
        progress("reading image 1…")
        gate.wait(10)
        raise RuntimeError("stopped by the test")

    tab, _ = make_tab(qapp, extract=slow_extract)
    tab.csv.setText(str(tmp / "x.csv"))
    tab.add_sources([src])
    tab.extract_btn.click()
    assert tab.busy and tab.progress.isVisibleTo(tab)
    assert not any(b.isEnabled() for b in tab._busy_buttons)
    tab.extract_btn.click()                              # ignored while busy
    for _ in range(50):
        QApplication.processEvents()
    assert "reading image 1" in tab.status.text()
    gate.set()
    assert tab.wait()
    assert ran_on and ran_on[0] is not threading.main_thread() and len(ran_on) == 1
    assert tab.status.text().startswith("failed: RuntimeError")
    assert "Traceback" in tab.log.toPlainText()
    assert tab.extract_btn.isEnabled() and not tab.progress.isVisibleTo(tab)


def test_pipeline_errors_are_shown_in_plain_words(qapp, books):
    _t, src, tmp = books
    tab, _ = make_tab(qapp)
    tab.csv.setText(str(tmp / "missing.csv"))
    tab.add_sources([src])
    tab.extract_btn.click()
    assert tab.wait()
    assert tab.status.text().startswith("error: parent CSV not found")
    assert "Traceback" not in tab.log.toPlainText()


def test_sources_can_be_removed_and_cleared(qapp, books):
    _t, src, _tmp = books
    tab, _ = make_tab(qapp)
    tab.csv.setText("p.csv")
    tab.add_sources([src / "summary.xlsx", src / "laser_log.xlsx"])
    tab.sources.item(0).setSelected(True)
    tab.remove_selected()
    assert tab.source_paths() == [str(src / "laser_log.xlsx")]
    tab.clear_sources()
    assert tab.source_paths() == [] and not tab.extract_btn.isEnabled()


def test_registry_factory(qapp):
    assert isinstance(build_ledger(None), LedgerTab)
