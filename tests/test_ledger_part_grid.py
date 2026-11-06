"""Parts' tables side by side on one sheet, each under its own Part Number
(user, 2026-10-30: "many tables being in the file on the same sheet that
belong to different parts"; then "yes" to accepting values directly under
their own Part Number). Every value of such a sheet went to review as
'more than one part on the sheet' (160 tables: 5,920 of 5,920)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ledger_fixtures as lf  # noqa: E402
import source_extract as se  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from sample_ledger import ParentCSV  # noqa: E402

HEAD = ["Flaw ID", "EC Pre-Conceal", "EC Post-Conceal", "Inspector"]


@pytest.fixture()
def schema(tmp_path):
    tpl = lf.make_template(tmp_path / "template.xlsx")
    return ParentCSV(li.create_parent(tpl).csv).schema


def _block(ws, r0, c0, part, k):
    ws.cell(r0, c0, "Part Number")
    ws.cell(r0, c0 + 1, part)
    for j, h in enumerate(HEAD):
        ws.cell(r0 + 2, c0 + j, h)
    for i in range(3):
        ws.cell(r0 + 3 + i, c0, lf.ITEMS[i])
        ws.cell(r0 + 3 + i, c0 + 1, round(0.1 + k / 100 + i / 1000, 3))
        ws.cell(r0 + 3 + i, c0 + 2, round(0.5 + k / 100 + i / 1000, 3))
        ws.cell(r0 + 3 + i, c0 + 3, "JB")


def _book(path, places, extra=None):
    """places: (row, col, part) of each block's Part Number label."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Parts"
    for k, (r, c, part) in enumerate(places):
        _block(ws, r, c, part, k)
    if extra:
        extra(ws)
    wb.save(path)
    return path


def _check(run, schema, places):
    """Each reading on its own block's part, and none held for the sheet."""
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
    cols = sorted({c for _r, c, _p in places})
    got = 0
    for o in run.obs:
        if o.field.startswith("Power") or "!" not in o.where:
            continue
        letters, row = coordinate_from_string(o.where.split("!")[1])
        col = column_index_from_string(letters)
        band = max(c for c in cols if c <= col)
        want = max((x for x in places if x[1] == band and x[0] <= row), key=lambda x: x[0])
        assert o.part == want[2], (o.where, o.part, want)
        got += 1
    assert got >= 6 * len(places)
    dec = se.decide(run, schema)
    held = [d for d in dec if "more than one part on the sheet" in se.review_reasons(d, schema)]
    return dec, held


def test_tables_side_by_side_each_take_their_own_part_and_are_accepted(tmp_path, schema):
    places = [(1, 1, "4001"), (1, 7, "4002"), (12, 1, "4003"), (12, 7, "4004")]
    run = se.extract(schema, [_book(tmp_path / "grid.xlsx", places)], use_images=False)
    dec, held = _check(run, schema, places)
    assert not held
    assert sum(d.accept for d in dec) >= 24


def test_staggered_blocks_go_by_their_column_first(tmp_path, schema):
    # the right-hand column's blocks start lower than the left's
    places = [(1, 1, "4001"), (1, 7, "4002"), (12, 1, "4003"), (17, 7, "4004")]
    run = se.extract(schema, [_book(tmp_path / "stagger.xlsx", places)], use_images=False)
    _dec, held = _check(run, schema, places)
    assert not held


def test_a_label_row_with_other_titles_is_still_checked(tmp_path, schema):
    places = [(1, 1, "4001"), (1, 7, "4002")]

    def title(ws):
        ws.cell(1, 12, "Weekly summary")
    run = se.extract(schema, [_book(tmp_path / "titled.xlsx", places, title)],
                     use_images=False)
    _dec, held = _check(run, schema, places)
    assert held                                   # not sure whose: every value checked


def test_a_tbd_block_with_readings_is_still_checked(tmp_path, schema):
    places = [(1, 1, "4001"), (1, 7, "TBD")]
    run = se.extract(schema, [_book(tmp_path / "tbd.xlsx", places)], use_images=False)
    dec = se.decide(run, schema)
    assert any("more than one part on the sheet" in se.review_reasons(d, schema) for d in dec)
