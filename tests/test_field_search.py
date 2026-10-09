"""field_search value splitting and row/line locators.

The knowledge graph reads people, parts and projects out of labelled fields, so
a value that is broken apart becomes two wrong entities: 'Lee, Carol' two
people, 'PN-1234/A' two parts, 'Bearings & Seals Program' two projects. These
tests pin the splitting rules and the locators the graph cites.
"""
from __future__ import annotations

import pytest

import field_search as fs


SPLIT_CASES = [
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
    # A group noun keeps only the 'and' it closes: the trailing team or
    # company is its own value, and two groups are two values (review of the
    # merge: these regressed against qt-migration).
    ("Carol Lee and Tomas Echeverria, Test Team",
     ["Carol Lee", "Tomas Echeverria", "Test Team"]),
    ("Ann Smith and Bob Jones, Acme Inc", ["Ann Smith", "Bob Jones", "Acme Inc"]),
    ("Dana Whitfield & Marcus Oyelaran, Systems",
     ["Dana Whitfield", "Marcus Oyelaran", "Systems"]),
    ("Alice Jones, Bob Smith and the Test Team",
     ["Alice Jones", "Bob Smith", "the Test Team"]),
    ("Lab Team and QA Team", ["Lab Team", "QA Team"]),
    ("Ops Team and QA Team", ["Ops Team", "QA Team"]),
    # A comma list ending in 'and' is a list, never 'Last, First'.
    ("Alice, Bob and Carol", ["Alice", "Bob", "Carol"]),
    ("Alice, Bob, and Carol", ["Alice", "Bob", "Carol"]),
    # A first name AND a surname after the comma is two people.
    ("Bob, Alice Smith", ["Bob", "Alice Smith"]),
    # ...but a suffix after the first name is still one person.
    ("Smith, John Jr.", ["Smith, John Jr."]),
]


@pytest.mark.parametrize("text, expected", SPLIT_CASES)
def test_split_values(text, expected):
    assert fs._split_values(text) == expected


@pytest.mark.parametrize("text, _expected", SPLIT_CASES)
def test_split_values_is_idempotent(text, _expected):
    # field_value_file_rows splits values extract_field_value already split.
    once = fs._split_values(text)
    assert [x for v in once for x in fs._split_values(v)] == once


@pytest.mark.parametrize("text, kw, expected", [
    # Only a field that names ONE person reads 'Last, First'.
    ("Bob, Alice", {"field": "Attendees"}, ["Bob", "Alice"]),
    ("Helios, Atlas", {"field": "Project"}, ["Helios", "Atlas"]),
    ("Helios, Atlas", {"kind": "PROJECT"}, ["Helios", "Atlas"]),
    ("Smith, Bob", {"kind": "PART"}, ["Smith", "Bob"]),
    ("Lee, Carol", {"field": "Point of Contact"}, ["Lee, Carol"]),
    ("Lee, Carol", {"field": "Router_Point_of_Contact"}, ["Lee, Carol"]),
    ("Lee, Carol", {"kind": "PERSON"}, ["Lee, Carol"]),
    ("Whitfield, D.", {"field": "Attendees"}, ["Whitfield, D."]),
])
def test_last_first_follows_the_field(text, kw, expected):
    assert fs._split_values(text, **kw) == expected


def test_tally_reads_lists_and_projects_as_separate_values(tmp_path):
    (tmp_path / "memo.txt").write_text("Attendees: Bob, Alice\n", encoding="utf-8")
    (tmp_path / "t.csv").write_text(
        'Project,Point of Contact\n"Helios, Atlas","Lee, Carol"\n', encoding="utf-8")
    assert fs.field_value_counts(tmp_path, "attendees") == [("Alice", 1), ("Bob", 1)]
    assert fs.field_value_counts(tmp_path, "project") == [("Atlas", 1), ("Helios", 1)]
    assert fs.field_value_counts(tmp_path, "point of contact") == [("Lee, Carol", 1)]


def test_tally_and_search_agree_on_a_trailing_team(tmp_path):
    (tmp_path / "a.txt").write_text(
        "Point of Contact: Dana Whitfield and Marcus Oyelaran, Systems\n",
        encoding="utf-8")
    counts = dict(fs.field_value_counts(tmp_path, "point of contact"))
    assert counts.get("Dana Whitfield") == 1 and counts.get("Marcus Oyelaran") == 1
    hits = fs.find_files_with_field_value(tmp_path, "point of contact", "Dana Whitfield")
    assert hits and "and Marcus" not in hits[0][1]


def test_a_person_who_is_not_in_the_cell_is_not_found(tmp_path):
    (tmp_path / "two_people.csv").write_text(
        'Project,Point of Contact\nNorthwind,"Bob, Alice Smith"\n', encoding="utf-8")
    assert fs.find_files_with_field_value(tmp_path, "point of contact", "Bob Smith") == []


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
