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

    def slow_extract(csv, sources, part, use_images, progress,
                     pattern_mask=None, vision_model=None, cancel=None):
        ran_on.append(threading.current_thread())
        progress("reading workbooks: 1/2 workbooks (50%)", 0.5)
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
    assert "1/2 workbooks (50%)" in tab.status.text()
    assert (tab.progress.maximum(), tab.progress.value()) == (100, 50)   # a real percentage
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


#: What the tab says for a Cancel after every workbook was read.
_CANCELLED_READ_ALL = ("Cancelled after every workbook was read: every value the rules found "
                       "is in the review. Open it, check the amber and red rows, map the "
                       "leftover labels, save it, then Apply.")


def test_cancel_while_councils_model_is_asked_says_cancelled(qapp, books, monkeypatch):
    """Cancel pressed while Council's model was asked about the labels left
    over: the run stopped, but the tab said 'Review written…' and its log
    that the model had not answered - '(none answered)', 'did not answer:
    stopped by the caller' (review, 2026-10-20). Then it said 'Cancelled.
    The partial review…' of a review that had every value the rules found,
    and one pressed a moment earlier, before the model was asked, 'Review
    written…' (second review, 2026-10-20): both say the same, not
    'partial'."""
    import time
    import openpyxl
    import source_extract as se
    from council_core import ledger_llm
    template, src, _tmp = books
    wb = openpyxl.Workbook()
    wb.active["A1"], wb.active["A2"], wb.active["B2"] = "P/N: 3307", "Zorp rate", 7
    wb.save(src / "extra.xlsx")                           # a label no rule places
    tab, _ = make_tab(qapp)
    tab.template.setText(str(template))
    tab.create_btn.click()
    assert tab.wait()
    tab.add_sources([src])
    tab.ask_model.setChecked(True)
    tab._pick_model = lambda parent=None: "ollama:granite3.3:8b"
    asked = threading.Event()

    def chat_for(model_id, timeout=120, should_stop=None):
        def chat(prompt, schema):                 # council_engine's Ollama call
            if "Which part" in prompt:
                return '{"part": "", "evidence": ""}', "granite3.3:8b"
            asked.set()
            end = time.monotonic() + 30
            while not should_stop() and time.monotonic() < end:     # generating
                time.sleep(0.02)
            raise se.LedgerError("granite3.3:8b (Ollama) did not answer: stopped by the "
                                 "caller")
        return chat
    monkeypatch.setattr(ledger_llm, "chat_for", chat_for)
    tab.extract_btn.click()
    end = time.monotonic() + 60
    while not asked.is_set() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    assert asked.is_set() and tab.cancel_btn.isEnabled()
    tab.cancel_btn.click()
    assert tab.wait()
    assert tab.status.text() == _CANCELLED_READ_ALL
    log = tab.log.toPlainText()
    assert "did not answer" not in log and "none answered" not in log
    assert "Cancel stopped it while it was asked about the labels left over" in log
    assert "CANCELLED after every workbook was read" in log
    assert ("Cancelled by user while Council's model was asked about the labels left over; "
            "every value the rules found is in this review.") in log


def test_cancel_before_councils_model_is_asked_says_the_same(qapp, books, monkeypatch):
    """Pressed after the last workbook was read and before Council's model
    was asked: the same words as pressed while it is asked - it said
    'Review written…' (second review, 2026-10-20)."""
    import openpyxl
    from council_core import ledger_ingest as li
    from council_core import ledger_llm
    template, src, _tmp = books
    wb = openpyxl.Workbook()
    wb.active["A1"], wb.active["A2"], wb.active["B2"] = "P/N: 3307", "Zorp rate", 7
    wb.save(src / "extra.xlsx")                           # a label no rule places
    tab, _ = make_tab(qapp)
    tab.template.setText(str(template))
    tab.create_btn.click()
    assert tab.wait()
    tab.add_sources([src])
    tab.ask_model.setChecked(True)
    tab._pick_model = lambda parent=None: "ollama:granite3.3:8b"
    asked = []

    def chat_for(model_id, timeout=120, should_stop=None):
        def chat(prompt, schema):
            asked.append(prompt)
            return '{"part": "", "evidence": ""}', "granite3.3:8b"
        return chat
    monkeypatch.setattr(ledger_llm, "chat_for", chat_for)
    real = li.extract

    def read_then_cancel(*a, **k):                         # pressed as the last one is read
        run = real(*a, **k)
        k["cancel"].set()
        return run
    monkeypatch.setattr(li, "extract", read_then_cancel)
    tab.extract_btn.click()
    assert tab.wait()
    assert not any("Which part" not in p for p in asked)   # not asked about the labels
    assert tab.status.text() == _CANCELLED_READ_ALL
    log = tab.log.toPlainText()
    assert "CANCELLED after every workbook was read" in log and "partial" not in log
    assert ("Cancelled before Council's model was asked about the labels left over; "
            "every value the rules found is in this review.") in log


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


def test_diagnose_a_file_runs_it_apart_and_opens_the_report(qapp, books, monkeypatch, tmp_path):
    """"Diagnose a file…" (council_core.ledger_diagnose): the file picked is
    read in a separate process, each line shows in the log as it comes, and
    the report is opened to send back."""
    monkeypatch.setenv("COUNCIL_APP_DIR", str(tmp_path / "app"))
    template, src, _ = books
    tab, opened = make_tab(qapp)
    tab.template.setText(str(template))
    tab.on_create()
    assert tab.wait(60)
    picked = src / "laser_log.xlsx"
    monkeypatch.setattr(tab, "_browse_diagnose_file", lambda start: str(picked))
    assert tab.diagnose_btn.isEnabled()
    tab.on_diagnose()
    assert tab.wait(300)
    log = tab.log.toPlainText()
    assert "== step: table reader" in log
    assert "Diagnosis of laser_log.xlsx: finished normally" in log
    assert opened and Path(opened[-1]).name == "report.txt"
    assert "whole Extract into" in Path(opened[-1]).read_text(encoding="utf-8")


def test_review_by_clicking_opens_over_the_review(qapp, books):
    """After Extract, "Review by clicking…" reads the review workbook and
    opens the click-through window over it (ledger_review_dialog)."""
    template, src, _ = books
    tab, _ = make_tab(qapp)
    assert not tab.review_btn.isEnabled()
    tab.template.setText(str(template))
    tab.on_create()
    assert tab.wait(60)
    tab.add_sources([src])
    tab.on_extract()
    assert tab.wait(120)
    assert tab.review_btn.isEnabled()
    tab.on_review_window()
    assert tab.wait(60)
    dlg = tab.review_dialog
    assert dlg.review.path == tab.review.text()
    assert "need a decision" in tab.log.toPlainText()
    dlg.close()


def test_the_operator_to_prefer_goes_into_the_extract(qapp, books, monkeypatch):
    """user, 2026-10-23: one workbook's several readings - the operator
    typed is used first (source_extract.decide), and the review keeps it."""
    template, src, _ = books
    tab, _ = make_tab(qapp)
    tab.template.setText(str(template))
    tab.on_create()
    assert tab.wait(60)
    tab.add_sources([src])
    tab.prefer_operator.setText("jb, AK")
    from council_core import ledger_ingest
    seen = {}
    real = ledger_ingest.run_extract

    def spy(*a, **kw):
        seen["prefer"] = kw.get("prefer")
        return real(*a, **kw)
    monkeypatch.setattr(ledger_ingest, "run_extract", spy)
    tab.on_extract()
    assert tab.wait(120)
    assert seen["prefer"] == ["jb", "AK"]
    import json
    import openpyxl
    meta = json.loads(openpyxl.load_workbook(tab.review.text())["_run"]["A1"].value)
    assert meta["prefer"] == ["jb", "AK"]
