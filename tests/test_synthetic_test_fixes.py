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


# ── the five gaps the rerun found (Andy approved 1-4, then 5, 2026-10-27) ──
def _book(path, sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(list(r))
    wb.save(path)
    return path


PRE = "NDE Inspection: Eddy Current Response Pre-Conceal"
POST = "NDE Inspection: Eddy Current Response Post-Conceal"


def _signed(run, field):
    return {(o.part, o.item, str(o.value), o.inspector) for o in run.obs if o.field == field}


def test_initials_named_only_in_the_sheet_name_sign_its_readings(tmp_path, ledger):
    _tpl, schema = ledger
    head = ("Part Number", "Flaw #", "Eddy Current Response Pre-Conceal")
    f = _book(tmp_path / "ec_by_inspector.xlsx", {"EC JB": [head, ("2041", "1A", 41)],
                                                  "EC MK": [head, ("2041", "1A", 47)]})
    run = se.extract(schema, [f], use_images=False)
    assert _signed(run, PRE) == {("2041", "1A", "41", "JB"), ("2041", "1A", "47", "MK")}


def test_initials_in_a_column_header_sign_that_column(tmp_path, ledger):
    _tpl, schema = ledger
    f = _book(tmp_path / "ec.xlsx", {"EC": [
        ("Part Number", "Flaw #", "Eddy Current Response Pre-Conceal (JB)",
         "Eddy Current Response Post-Conceal (MK)"),
        ("2041", "1A", 41, 52)]})
    run = se.extract(schema, [f], use_images=False)
    assert _signed(run, PRE) == {("2041", "1A", "41", "JB")}
    assert _signed(run, POST) == {("2041", "1A", "52", "MK")}


def test_a_stage_named_only_in_the_sheet_name(tmp_path, ledger):
    _tpl, schema = ledger
    head = ("Part Number", "Flaw #", "Inspector", "Response")
    f = _book(tmp_path / "by_stage.xlsx", {"Pre-Conceal": [head, ("2041", "1A", "RT", 41)],
                                           "Post-Conceal": [head, ("2041", "1A", "RT", 52)]})
    run = se.extract(schema, [f], use_images=False)
    # 'Response' beside 'Inspector' in the header row is no one
    assert _signed(run, PRE) == {("2041", "1A", "41", "RT")}
    assert _signed(run, POST) == {("2041", "1A", "52", "RT")}


def test_a_part_named_only_in_an_index_workbook(tmp_path, ledger):
    _tpl, schema = ledger
    _book(tmp_path / "Campaign_index.xlsx", {"Index": [
        ("Part Number", "Settings file", "Project"),
        ("2041", "B_settings_1.xlsx", "Ironbark"), ("2042", "B_settings_2.xlsx", "Ironbark")]})
    for k, p in ((1, 255), (2, 270)):
        _book(tmp_path / f"B_settings_{k}.xlsx", {"Settings": [("Power (W)", p),
                                                               ("Frequency (kHz)", 20)]})
    run = se.extract(schema, sorted(tmp_path.glob("*.xlsx")), use_images=False)
    got = {(o.part, str(o.value)) for o in run.obs if o.field == "Power (W)"}
    assert got == {("2041", "255"), ("2042", "270")}
    o = next(o for o in run.obs if o.field == "Power (W)")
    assert o.part_how.startswith(se.INDEX_PART_HOW)
    assert not any("values have no" in p for p in run.problems)
    # named by another file: checked, never pre-accepted
    assert not any(d.accept for d in se.decide(run, schema) if d.obs.field == "Power (W)")


def test_a_filled_template_edited_outside_excel_is_read_again(tmp_path, ledger):
    tpl, schema = ledger
    wb = openpyxl.load_workbook(tpl)
    wb.properties.lastModifiedBy = "Template Author"     # a template saved in Excel
    wb.save(tpl)
    out = tmp_path / "filled.xlsx"
    fill_template(schema, [{"Part Number": "2041", "Flaw #": "1A", "Power (W)": "255"}],
                  out, tpl)
    assert is_filled_template(out)
    wb = openpyxl.load_workbook(out)                      # opened and saved, unchanged
    wb.save(out)
    assert is_filled_template(out)
    wb = openpyxl.load_workbook(out)                      # a script types a value in;
    wb.active["C3"] = 270                                 # no last-saved-by is written
    wb.save(out)
    assert is_edited_filled_template(out) and not is_filled_template(out)
