"""field_search value splitting and row/line locators.

The knowledge graph reads people, parts and projects out of labelled fields, so
a value that is broken apart becomes two wrong entities: 'Lee, Carol' two
people, 'PN-1234/A' two parts, 'Bearings & Seals Program' two projects. These
tests pin the splitting rules and the locators the graph cites.
"""
from __future__ import annotations

import pytest

import field_search as fs


@pytest.mark.parametrize("text, expected", [
    # Lists that really are lists keep splitting.
    ("Bob and Alice", ["Bob", "Alice"]),
    ("Bob; Alice", ["Bob", "Alice"]),
    ("Bob Smith, Alice Jones, Carol Lee", ["Bob Smith", "Alice Jones", "Carol Lee"]),
    ("Bob/Alice", ["Bob", "Alice"]),
    ("Bob; Reviewer: Alice", ["Bob"]),
    # 'Last, First' is one person, with or without an initial or suffix.
    ("Lee, Carol", ["Lee, Carol"]),
    ("Lee, Carol A.", ["Lee, Carol A."]),
    ("Whitfield, D.", ["Whitfield, D."]),
    ("Smith, Bob; Lee, Carol", ["Smith, Bob", "Lee, Carol"]),
    ("Smith, Bob and Lee, Carol", ["Smith, Bob", "Lee, Carol"]),
    ("Bob Smith, Jr.", ["Bob Smith, Jr."]),
    # Part codes with a revision or variant after '/'.
    ("PN-1234/A", ["PN-1234/A"]),
    ("PN-1234/A, PN-2201/B", ["PN-1234/A", "PN-2201/B"]),
    ("BRG-77/2", ["BRG-77/2"]),
    # Names that contain '&' or 'and'.
    ("Bearings & Seals Program", ["Bearings & Seals Program"]),
    ("Research and Development Project", ["Research and Development Project"]),
    ("R&D", ["R&D"]),
    ("Bob & Alice", ["Bob", "Alice"]),
])
def test_split_values(text, expected):
    assert fs._split_values(text) == expected


def test_last_first_value_found_in_text_field(tmp_path):
    f = tmp_path / "memo.txt"
    f.write_text("Owner: Lee, Carol\nProgram: Bearings & Seals Program\n",
                 encoding="utf-8")
    assert fs.extract_field_value(f, "owner") == ["Lee, Carol"]
    assert fs.extract_field_value(f, "program") == ["Bearings & Seals Program"]


def test_file_rows_keep_a_last_first_name_whole(tmp_path):
    (tmp_path / "a.txt").write_text("Point of Contact: Lee, Carol\n",
                                    encoding="utf-8")
    rows = fs.field_value_file_rows(tmp_path, "point of contact")
    assert [r["value"] for r in rows] == ["Lee, Carol"]


def test_field_value_locations_text_lines(tmp_path):
    f = tmp_path / "memo.md"
    f.write_text("# Status\n\nOwner: Dana Whitfield\n\nNotes\nOwner: Lee, Carol\n",
                 encoding="utf-8")
    locs = fs.field_value_locations(f, "owner")
    assert [(l["value"], l["line"]) for l in locs] == [
        ("Dana Whitfield", 3), ("Lee, Carol", 6)]
    assert all(l["kind"] == "line" for l in locs)
    assert locs[0]["snippet"] == "Owner: Dana Whitfield"


def test_field_value_locations_heading_value_on_next_line(tmp_path):
    f = tmp_path / "h.md"
    f.write_text("## Point of Contact\nBob Smith\n", encoding="utf-8")
    locs = fs.field_value_locations(f, "point of contact")
    assert [(l["value"], l["line"]) for l in locs] == [("Bob Smith", 2)]


def test_field_value_locations_csv_rows(tmp_path):
    f = tmp_path / "tracker.csv"
    f.write_text("id,Owner,Part\n1,Dana Whitfield,PN-1234/A\n2,,PN-9\n"
                 "3,\"Lee, Carol\",PN-2201/B\n", encoding="utf-8")
    locs = fs.field_value_locations(f, "owner")
    # row = the spreadsheet row a person sees (header is row 1).
    assert [(l["value"], l["row"], l["column"]) for l in locs] == [
        ("Dana Whitfield", 2, "Owner"), ("Lee, Carol", 4, "Owner")]
    assert all(l["kind"] == "row" for l in locs)
    parts = fs.field_value_locations(f, "part")
    assert [l["value"] for l in parts] == ["PN-1234/A", "PN-9", "PN-2201/B"]


def test_field_value_locations_xlsx_sheet_and_row(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    f = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["nothing here"])
    ws2 = wb.create_sheet("Parts")
    ws2.append(["P/N", "Lead"])
    ws2.append(["PN-1234/A", "Whitfield, Dana"])
    wb.save(f)
    locs = fs.field_value_locations(f, "lead")
    assert [(l["value"], l["sheet"], l["row"]) for l in locs] == [
        ("Whitfield, Dana", "Parts", 2)]
