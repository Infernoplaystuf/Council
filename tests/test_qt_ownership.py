"""PySide6 6.12 leaves a widget Python-owned when it is added to a layout that
has no widget yet, though Qt has given it a parent: two owners, and a segfault
in QObject::~QObject when the garbage collector takes the view apart in the
wrong order (council_qt.view.hand_to_qt). Every Ledger view hands its
children to Qt once they are built."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
shiboken6 = pytest.importorskip("shiboken6")
from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QVBoxLayout, QWidget  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from council_qt.tabs.ledger import LedgerActions, LedgerTab  # noqa: E402
from council_qt.tabs.ledger_link_dialog import LinkDialog  # noqa: E402
from council_qt.view import hand_to_qt  # noqa: E402
from test_ledger_link_dialog import settle  # noqa: E402
from test_ledger_linking import plain_btb  # noqa: E402,F401


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def double_owned(root) -> list:
    """The children of ``root`` with a Qt parent that Python owns as well.
    Not by root.findChildren: under 6.12 that hands each child it returns
    to its parent, and would hide what it looks for."""
    def under_root(o) -> bool:
        p = o.parent()
        while p is not None:
            if p is root:
                return True
            p = p.parent()
        return False
    return sorted(type(o).__name__ for o in shiboken6.getAllValidWrappers()
                  if isinstance(o, QObject) and o is not root and shiboken6.isValid(o)
                  and shiboken6.ownedByPython(o) and under_root(o))


def test_hand_to_qt_gives_the_parent_the_child(qapp):
    w = QWidget()
    outer = QVBoxLayout(w)
    row = QHBoxLayout()
    b = QPushButton("x")
    row.addWidget(b)                    # a layout with no widget yet
    outer.addLayout(row)
    assert b.parent() is w
    hand_to_qt(w)
    assert not shiboken6.ownedByPython(b) and b.parent() is w and double_owned(w) == []


def test_the_ledger_tab_owns_nothing_twice(qapp):
    tab = LedgerTab(None, LedgerActions(opener=lambda _p: None))
    assert double_owned(tab) == []
    shiboken6.delete(tab)


def test_the_link_dialog_owns_nothing_twice(qapp, plain_btb):  # noqa: F811
    csv, src = plain_btb
    dlg = LinkDialog(str(csv), [str(src / "BTB_Spider.xlsx")])
    settle(dlg, lambda: dlg.template is not None and dlg.sample is not None)
    assert double_owned(dlg) == []
    dlg.close()


def test_the_teach_dialog_owns_nothing_twice(qapp, tmp_path):
    from test_ledger_teach_dialog import choose, close, guess, ledger, make_tab, open_teach
    csv, _schema, src, untitled, _sample, vault = ledger.__wrapped__(tmp_path)
    tab, _picked = make_tab(qapp, csv, vault, [src])
    dlg = open_teach(tab)
    assert double_owned(dlg) == []
    dlg.use_model.setChecked(False)
    choose(dlg, untitled)
    guess(dlg)                          # its rows, built after the dialog
    assert dlg.table.rowCount() and double_owned(dlg) == []
    close(dlg)
    assert double_owned(tab) == []


def test_the_deck_text_boxes_tab_owns_nothing_twice(qapp, tmp_path):
    pytest.importorskip("pptx")
    from test_ledger_records import simple_ledger
    from test_pptx_open import nde_deck
    _tpl, csv = simple_ledger(tmp_path, fields=("Position", "Reading"))
    dlg = LinkDialog(str(csv), [str(nde_deck(tmp_path / "d.pptx"))])
    settle(dlg, lambda: dlg.sample is not None)
    assert dlg.pairing_table.rowCount() and double_owned(dlg) == []
    dlg.close()


def test_the_pick_model_dialog_owns_nothing_twice(qapp):
    from council_qt.tabs import ledger_dialogs as ld
    dlg = ld.PickModelDialog(lister=lambda: [])
    assert double_owned(dlg) == []
    dlg.close()
