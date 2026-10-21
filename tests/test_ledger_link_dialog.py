"""The Ledger tab's "Link by clicking…" window (ledger_link_dialog), offscreen:
real clicks on its template and sample tables."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from council_core import ledger_records as lr  # noqa: E402
from council_qt.tabs.ledger import LedgerActions, LedgerTab  # noqa: E402
from council_qt.tabs.ledger_link_dialog import LINKED_TINT, LinkDialog  # noqa: E402
from test_ledger_linking import plain_btb  # noqa: E402,F401


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def settle(dlg, until, timeout=20.0):
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    QApplication.processEvents()
    assert until()


def click(tabs, tab_name, r, c):
    """A mouse click on cell (r, c) of the named tab's table."""
    i = next(i for i in range(tabs.count()) if tabs.tabText(i) == tab_name)
    tabs.setCurrentIndex(i)
    view = tabs.widget(i)
    view.scrollTo(view.model().index(r - 1, c - 1))
    rect = view.visualRect(view.model().index(r - 1, c - 1))
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier,
                     QPoint(rect.center().x(), rect.center().y()))
    QApplication.processEvents()


def test_click_template_then_sample_and_save(qapp, plain_btb):  # noqa: F811
    csv, src = plain_btb
    saved = []
    dlg = LinkDialog(str(csv), [str(src / "BTB_Spider.xlsx"), str(src / "BTB_Saddle_BP.xlsx")],
                     on_saved=saved.append)
    dlg.show()
    settle(dlg, lambda: dlg.template is not None and dlg.sample is not None)
    assert not dlg.save_btn.isEnabled()
    # a sample click before a field: said, nothing linked
    click(dlg.sample_tabs, "Sheet", 2, 2)
    assert "First click the template cell" in dlg.status.text() and not dlg.links
    # the template's Health cell, then the sample's 'HP' header
    click(dlg.template_tabs, "Sheet", 1, 3)
    assert dlg.picked == "Health"
    click(dlg.sample_tabs, "Sheet", 2, 2)
    assert [k.sentence[:24] for k in dlg.links] == ["Column 'HP' is Health, i"]
    assert dlg._sample_models[0].tints[(2, 2)] == LINKED_TINT
    # clicking the same field again replaces its link, never doubles it
    click(dlg.template_tabs, "Sheet", 3, 3)
    click(dlg.sample_tabs, "Sheet", 4, 2)
    assert len(dlg.links) == 1 and dlg.link_list.count() == 1
    # a lookup in the other workbook
    dlg.field_list.setCurrentItem(dlg.field_list.findItems("Saddle BP", Qt.MatchExactly)[0])
    dlg.sample_choice.setCurrentIndex(1)
    dlg._read_sample(dlg.sample_choice.currentText())
    settle(dlg, lambda: dlg.sample is not None and dlg.sample.path.endswith("Saddle_BP.xlsx"))
    dlg.lookup_btn.setChecked(True)
    click(dlg.sample_tabs, "Sheet", 3, 1)
    assert dlg.links[-1].kind == "lookup" and not dlg.lookup_btn.isChecked()
    # Save: written, listed, and the list emptied
    dlg.save_btn.click()
    assert saved and len(saved[0]) == 2 and not dlg.links
    recipe = lr.load_recipe(csv)
    assert recipe.aliases["hp"] == "Health" and recipe.sources["Saddle BP"]["kind"] == "lookup"
    dlg.close()


def test_a_cell_that_is_no_field_says_so(qapp, plain_btb):  # noqa: F811
    csv, src = plain_btb
    dlg = LinkDialog(str(csv), [str(src / "BTB_Spider.xlsx")])
    dlg.show()
    settle(dlg, lambda: dlg.template is not None and dlg.sample is not None)
    click(dlg.template_tabs, "Sheet", 30, 20)
    assert "not one the Ledger fills" in dlg.status.text() and not dlg.picked
    dlg.close()


def test_the_deck_text_boxes_tab(qapp, tmp_path):
    pytest.importorskip("pptx")
    from test_ledger_records import simple_ledger
    from test_pptx_open import nde_deck
    from council_core import ledger_linking as ll
    _tpl, csv = simple_ledger(tmp_path, fields=("Position", "Reading"))
    deck = nde_deck(tmp_path / "d.pptx")
    dlg = LinkDialog(str(csv), [str(deck)])
    dlg.show()
    settle(dlg, lambda: dlg.sample is not None)
    names = [dlg.sample_tabs.tabText(i) for i in range(dlg.sample_tabs.count())]
    assert names[-1] == "Text boxes" and "Slide 2 table 1" in names
    t = dlg.pairing_table
    rows = {}
    for i in range(t.rowCount()):                          # the first of two alike: TS's
        rows.setdefault(t.item(i, 1).text(), i)
    i = rows["Eddy Current Response (%FSH)"]
    assert "a guess" in t.item(i, 3).text()
    dlg.pairing_boxes[i].setCurrentIndex(2)                 # table 2
    assert dlg.links[-1].sentence.endswith("goes with Slide 2 table 2")
    dlg.save_btn.click()
    assert ll.pptx_open.load_pairings(csv)["d.pptx"]["2"] == {
        "eddy current response fsh operator ts": [2]}
    dlg.close()


def test_the_tab_opens_it(qapp, plain_btb):  # noqa: F811
    csv, src = plain_btb
    tab = LedgerTab(None, LedgerActions(opener=lambda _p: None))
    tab.csv.setText(str(csv))
    tab._on_csv_changed()
    tab.add_sources([src])
    assert tab.link_btn.isEnabled()
    tab.link_btn.click()
    dlg = tab.link_dialog
    assert dlg is not None and dlg.isVisible()
    assert [dlg.sample_choice.itemText(i) for i in range(dlg.sample_choice.count())] == [
        str(src / "BTB_Saddle_BP.xlsx"), str(src / "BTB_Spider.xlsx")]
    settle(dlg, lambda: dlg.template is not None and dlg.sample is not None)
    dlg.close()
