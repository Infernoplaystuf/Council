"""Fixes from the synthetic Ledger / Connections test (2026-10-27):
docs/handoff/synthetic_test_fixes_2026-10-27.md."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")
sys.path.insert(0, str(Path(__file__).parent))

import ledger_fixtures as lf  # noqa: E402
import source_extract as se  # noqa: E402
from council_core import knowledge_graph as kgm  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from sample_ledger import (ParentCSV, fill_template, is_edited_filled_template,  # noqa: E402
                           is_filled_template)


@pytest.fixture
def ledger(tmp_path):
    tpl = lf.make_template(tmp_path / "template.xlsx")
    ps = li.create_parent(tpl)
    return tpl, ParentCSV(ps.csv).schema


def _powers(dec, part):
    return {str(d.obs.value): d.accept for d in dec
            if d.obs.part == part and d.obs.field == "Power (W)"}


def test_a_table_longer_than_500_rows_keeps_its_parts(tmp_path, ledger):
    """Rows past 500 lost their part (the table stopped at 500)."""
    _tpl, schema = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Settings"
    ws.append(["Part Number", "Power (W)", "Frequency (kHz)"])
    for i in range(700):
        ws.append([f"P{1000 + i}", 200 + i % 50, 20])
    wb.save(tmp_path / "long.xlsx")
    run = se.extract(schema, [tmp_path / "long.xlsx"], use_images=False)
    parts = {o.part for o in run.obs if o.field == "Power (W)"}
    assert "P1699" in parts and "" not in parts


def test_an_inventory_sheet_is_never_pre_accepted(tmp_path, ledger):
    """A stock list with a Part Number and a Power (W) column among many
    other words: its values were pre-accepted and Apply wrote them."""
    _tpl, schema = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Stock"
    ws.append(["Part Number", "Description", "Qty On Hand", "Power (W)", "Bin", "Supplier"])
    rows = [("2041", "Cooling fan", 77, 0, "B12", "Acme fans"),
            ("2042", "Mains lead", 6, 120, "B13", "Volt supply"),
            ("PS-500", "Bench power supply", 44, 750, "C01", "Volt supply"),
            ("PS-750", "Rack supply", 31, 120, "C02", "Acme racks"),
            ("FAN-12", "Spare blower", 61, 120, "C03", "Acme fans")]
    for r in rows:
        ws.append(list(r))
    wb.save(tmp_path / "inventory.xlsx")
    dec = se.decide(se.extract(schema, [tmp_path / "inventory.xlsx"], use_images=False),
                    schema)
    assert dec and not any(d.accept for d in dec)


def test_a_filled_template_someone_saved_is_read_again(tmp_path, ledger):
    tpl, schema = ledger
    out = tmp_path / "filled.xlsx"
    fill_template(schema, [{"Part Number": "2041", "Flaw #": "1A", "Power (W)": "255"}],
                  out, tpl)
    assert is_filled_template(out) and not is_edited_filled_template(out)
    wb = openpyxl.load_workbook(out)
    wb.properties.lastModifiedBy = "J. Blake"     # as Excel does on save
    wb.save(out)
    assert is_edited_filled_template(out) and not is_filled_template(out)


def test_a_csv_is_a_ledger_source(tmp_path, ledger):
    _tpl, schema = ledger
    src = tmp_path / "settings.csv"
    src.write_text("Part Number;Power (W);Frequency (kHz)\n3021;270;40\n3022;330;20\n",
                   encoding="utf-8")
    run = se.extract(schema, [src], use_images=False)
    assert _powers(se.decide(run, schema), "3021").keys() == {"270"}


def test_a_part_named_in_the_sheet_title_is_used(tmp_path, ledger):
    _tpl, schema = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Settings"
    ws["A1"] = "Part 3005 - laser settings"
    ws["A3"], ws["B3"] = "Power (W)", 285
    ws["A4"], ws["B4"] = "Frequency (kHz)", 30
    wb.save(tmp_path / "settings.xlsx")
    run = se.extract(schema, [tmp_path / "settings.xlsx"], use_images=False)
    assert {o.part for o in run.obs if o.field == "Power (W)"} == {"3005"}


def test_a_sheet_with_no_word_of_the_template_is_not_read(tmp_path, ledger):
    _tpl, schema = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2025"
    ws.append(["Region", "Customer", "Product", "Revenue", "Quarter"])
    for i in range(300):
        ws.append(["North", f"Customer {i}", "Widget B", 1000 + i, "Q1"])
    wb.save(tmp_path / "sales.xlsx")
    run = se.extract(schema, [tmp_path / "sales.xlsx"], use_images=False)
    assert run.unrelated == ["sales.xlsx › 2025"] and not run.obs


def test_people_columns_are_offered_not_ticked(tmp_path):
    kg = kgm.KnowledgeGraph(tmp_path)
    status = {r.label: s for r, s in kg.field_rules()}
    assert status["Employee"] == status["Inspector"] == "proposed"
    assert status["Program Lead"] == "confirmed"
    kg.close()
