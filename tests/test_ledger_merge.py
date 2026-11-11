"""
Merging a folder of filled-out copies of a template into one workbook
(council_core.ledger_merge) and its window in the Ledger tab - user,
2026-11-11: "a template and then a folder of filled out files ... merge them
together into a single output workbook with a sheet for each part. I then
want to be able to do some analysis on that data"; a sheet per filled file.

Every workbook is synthetic, built in tmp_path.

Run:  python -m pytest tests/test_ledger_merge.py -q
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import openpyxl
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
sys.path.insert(0, str(Path(__file__).parent))

from council_core import ledger_analytics as la  # noqa: E402
from council_core import ledger_merge as lm  # noqa: E402
from council_core import ledger_saved  # noqa: E402
from ledger_fixtures import ITEMS, make_template  # noqa: E402
from sample_ledger import FILLED_MARK, LedgerError, ParentCSV, read_template  # noqa: E402

FOD = "Flaw Attributes: Laser Team FOD (um)"


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_APP_DIR", str(tmp_path / "app"))
    return tmp_path / "app"


def fill(template: Path, out: Path, values: dict, per_item: dict = None,
         sheet_title: str = None) -> Path:
    """A copy of the template filled in by hand: ``values`` by field name,
    ``per_item`` {field: {item: value}}."""
    schema = read_template(template)
    shutil.copyfile(template, out)
    wb = openpyxl.load_workbook(out)
    for f in schema.fields:
        ws = wb[f.sheet]
        if not f.per_item and f.name in values:
            ws[f.cells[""]].value = values[f.name]
        for item, v in (per_item or {}).get(f.name, {}).items():
            if item in f.cells:
                ws[f.cells[item]].value = v
    if sheet_title:
        wb.worksheets[0].title = sheet_title
    wb.save(out)
    return out


@pytest.fixture
def books(tmp_path, app_dir):
    """The template and a folder of 6 copies: S-1..S-6 with Power 300+p and
    FOD per flaw."""
    template = make_template(tmp_path / "LMM Template.xlsx")
    folder = tmp_path / "filled"
    folder.mkdir()
    for p in range(1, 7):
        fill(template, folder / f"part {p}.xlsx",
             {"Part Number": f"S-{p}", "Power (W)": 300 + p * 3,
              "Frequency (kHz)": 20 + p, "Date": f"2026-0{p}-15"},
             {FOD: {it: 40 + p + i for i, it in enumerate(ITEMS)}})
    return template, folder


def test_every_copy_is_merged_into_one_workbook_with_a_sheet_each(books):
    template, folder = books
    res = lm.merge(template, folder)
    assert res.merged == 6 and res.parts == 6 and not res.problems
    assert Path(res.csv).parent == folder.with_name("filled merged")
    wb = openpyxl.load_workbook(res.workbook)
    assert wb.sheetnames == ["All data"] + [f"S-{p}" for p in range(1, 7)]
    assert lm.NOT_MERGED not in wb.sheetnames            # nothing to say
    # each sheet is the template filled with that copy's values
    schema = read_template(template)
    power = schema.field("Power (W)").cells[""]
    assert wb["S-4"][power].value == 312
    fod_1a = schema.field(FOD).cells["1A"]
    assert wb["S-2"][fod_1a].value == 42
    # All data: the file each row came from, then the parent CSV's columns
    data = wb["All data"]
    head = [c.value for c in data[1]]
    assert head[:3] == ["File", "Part Number", "Flaw #"]
    first = dict(zip(head, [c.value for c in data[2]]))
    assert first["File"] == "part 1.xlsx" and first["Part Number"] == "S-1"
    assert first["Power (W)"] == 303
    # the workbook is marked as the Council's, so a scan does not read it
    assert FILLED_MARK in (wb.properties.keywords or "")


def test_the_merge_is_a_ledger_that_analyze_reads(books):
    template, folder = books
    res = lm.merge(template, folder)
    data = la.load(res.csv)
    assert data.parts == [f"S-{p}" for p in range(1, 7)]
    assert "Power (W)" in data.number_fields and FOD in data.number_fields
    s = la.summary(data)
    assert not s.empty
    led = ledger_saved.load(res.csv)
    assert led.settings["kind"] == lm.KIND and led.sources == [str(folder)]
    assert led.runs[-1].kind == "merge" and led.runs[-1].files == 6
    assert lm.is_merge(res.csv)
    assert str(Path(res.csv)) in [s.csv for s in ledger_saved.listed()]


def test_files_that_cannot_be_merged_are_listed_not_dropped(books, tmp_path):
    template, folder = books
    # laid out otherwise: the template's words somewhere else
    other = openpyxl.Workbook()
    other.active["D9"] = "Part Number"
    other.active["E9"] = "S-99"
    other.save(folder / "laid out otherwise.xlsx")
    shutil.copyfile(template, folder / "blank copy.xlsx")    # nothing filled in
    (folder / "old.xls").write_bytes(b"not really")
    (folder / "~$part 1.xlsx").write_bytes(b"lock")       # Excel's own: not mentioned
    (folder / "notes.txt").write_text("not a workbook")
    res = lm.merge(template, folder)
    assert res.merged == 6
    why = {p.file: p.reason for p in res.left_out}
    assert set(why) == {"laid out otherwise.xlsx", "blank copy.xlsx", "old.xls"}
    assert "layout differs" in why["laid out otherwise.xlsx"]
    assert "nothing filled in" in why["blank copy.xlsx"]
    assert "save it as .xlsx" in why["old.xls"]
    nm = openpyxl.load_workbook(res.workbook)[lm.NOT_MERGED]
    assert {nm.cell(r, 1).value for r in range(2, nm.max_row + 1)} == set(why)


def test_a_part_in_two_files_keeps_the_first_and_lists_what_differs(books):
    template, folder = books
    fill(template, folder / "part 3 again.xlsx",
         {"Part Number": "S-3", "Power (W)": 999, "Frequency (kHz)": 23})
    res = lm.merge(template, folder)
    assert res.merged == 7 and res.parts == 6
    clash = [p for p in res.clashes if p.field == "Power (W)"]
    assert len(clash) == 1
    assert clash[0].file == "part 3 again.xlsx" and clash[0].value == "999"
    assert clash[0].kept == "309 (part 3.xlsx)"
    led = ParentCSV(res.csv)
    assert led.rows_for("S-3")[0]["Power (W)"] == "309"
    # both copies keep a sheet of their own
    names = openpyxl.load_workbook(res.workbook).sheetnames
    assert "S-3" in names and "S-3 (2)" in names


def test_a_copy_without_a_part_number_is_named_after_its_file(books):
    template, folder = books
    fill(template, folder / "mystery.xlsx", {"Power (W)": 320})
    res = lm.merge(template, folder)
    assert "mystery" in [c.part for c in res.copies]
    assert any(p.file == "mystery.xlsx" and "named 'mystery'" in p.reason
               for p in res.problems)


def test_a_workbook_with_a_filled_sheet_per_part_gives_each_sheet(books, tmp_path):
    template, folder = books
    one = fill(template, tmp_path / "a.xlsx", {"Part Number": "S-10", "Power (W)": 330},
               sheet_title="S-10")
    two = fill(template, tmp_path / "b.xlsx", {"Part Number": "S-11", "Power (W)": 333})
    wb = openpyxl.load_workbook(one)
    src = openpyxl.load_workbook(two).worksheets[0]
    ws = wb.create_sheet("S-11")
    for row in src.iter_rows():
        for c in row:
            if c.value is not None:
                ws[c.coordinate] = c.value
    wb.save(folder / "both parts.xlsx")
    res = lm.merge(template, folder)
    by_part = {c.part: c.label for c in res.copies}
    assert by_part["S-10"] == "both parts.xlsx [S-10]"
    assert by_part["S-11"] == "both parts.xlsx [S-11]"
    assert ParentCSV(res.csv).rows_for("S-11")[0]["Power (W)"] == "333"


def test_merging_again_takes_in_new_files_and_writes_over_its_own_files_only(books):
    template, folder = books
    res = lm.merge(template, folder)
    fill(template, folder / "part 7.xlsx", {"Part Number": "S-7", "Power (W)": 321})
    again = lm.merge_again(res.csv)
    assert again.parts == 7 and again.csv == res.csv
    assert "S-7" in ParentCSV(res.csv).sample_ids()
    assert len(ledger_saved.load(res.csv).runs) == 2
    # a CSV the merge did not write is never written over
    foreign = Path(res.csv)
    ledger_saved.path_for(foreign).unlink()
    with pytest.raises(LedgerError, match="not a merge the Council made"):
        lm.merge(template, folder, foreign.parent)
    # nor a workbook it did not write
    out = foreign.parent.with_name("second")
    book = Path(lm.merge(template, folder, out).workbook)
    book.unlink()
    openpyxl.Workbook().save(book)
    with pytest.raises(LedgerError, match="the Council did not write it"):
        lm.merge(template, folder, out)
    assert not list(out.glob("*.merging*"))           # nothing half-written left


def test_the_merge_is_not_saved_inside_the_folder_of_copies(books):
    template, folder = books
    with pytest.raises(LedgerError, match="outside the folder"):
        lm.merge(template, folder, folder / "out")


def test_a_merged_workbook_in_the_folder_is_not_read_as_a_copy(books, tmp_path):
    template, folder = books
    res = lm.merge(template, folder)
    shutil.copyfile(res.workbook, folder / "old merge.xlsx")
    again = lm.merge(template, folder, tmp_path / "second")
    assert again.parts == 6
    assert any(p.file == "old merge.xlsx" and "merged workbook" in p.reason
               for p in again.problems)


# ── a template of several pages ─────────────────────────────────────────
def two_page_template(path: Path) -> Path:
    """Page 1: Part Number, Date, Power; page 2: Part Number, Date, Hardness -
    the same 'Date' label on both pages, no heading."""
    wb = openpyxl.Workbook()
    p1 = wb.active
    p1.title = "Page 1"
    for col, label in enumerate(["Part Number", "Date", "Power (W)", "Operator"], 1):
        p1.cell(1, col, label)
    p2 = wb.create_sheet("Page 2")
    for col, label in enumerate(["Part Number", "Date", "Hardness (HRC)", "Checked by"], 1):
        p2.cell(1, col, label)
    wb.save(path)
    return path


def test_the_same_label_on_two_pages_is_kept_for_each_page(tmp_path):
    schema = read_template(two_page_template(tmp_path / "T2.xlsx"))
    names = [f.name for f in schema.fields]
    assert "Date" in names and "Page 2: Date" in names
    assert names.count("Part Number") == 1               # the ID is one field


def test_a_copy_of_several_pages_is_merged_page_by_page(tmp_path, app_dir):
    template = two_page_template(tmp_path / "T2.xlsx")
    folder = tmp_path / "copies"
    folder.mkdir()
    for p in range(1, 4):
        out = folder / f"c{p}.xlsx"
        shutil.copyfile(template, out)
        wb = openpyxl.load_workbook(out)
        wb["Page 1"]["A2"], wb["Page 1"]["B2"], wb["Page 1"]["C2"] = f"K-{p}", "2026-01-0" + str(p), 100 + p
        wb["Page 2"]["A2"], wb["Page 2"]["B2"], wb["Page 2"]["C2"] = f"K-{p}", "2026-02-0" + str(p), 50 + p
        wb.save(out)
    res = lm.merge(template, folder)
    assert res.merged == 3 and not res.problems
    row = ParentCSV(res.csv).rows_for("K-2")[0]
    assert row["Date"] == "2026-01-02" and row["Page 2: Date"] == "2026-02-02"
    assert row["Hardness (HRC)"] == "52"
    wb = openpyxl.load_workbook(res.workbook)
    assert wb.sheetnames[1:3] == ["K-1 Page 1", "K-1 Page 2"]
    assert wb["K-3 Page 2"]["C2"].value == 53


# ── the window in the Ledger tab ────────────────────────────────────────
def test_the_ledger_tab_merges_shows_the_merged_ledger_and_analyzes_it(books):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from test_ledger_tab import make_tab
    qapp = QApplication.instance() or QApplication([])
    template, folder = books
    tab, opened = make_tab(qapp)
    tab.template.setText(str(template))
    assert tab.merge_btn.isEnabled()
    tab.merge_btn.click()
    dlg = tab.merge_dialog
    assert dlg.template.text() == str(template)
    assert not dlg.merge_btn.isEnabled() and not dlg.analyze_btn.isEnabled()
    dlg.folder.setText(str(folder))
    assert dlg.out_dir.text() == str(folder.with_name("filled merged"))
    dlg.merge_btn.click()
    assert tab.wait(120)
    assert dlg.result is not None and "Merged 6 of 6" in dlg.report.toPlainText()
    assert tab.csv.text() == dlg.result.csv
    assert tab.analyze_btn.isEnabled()
    dlg.open_btn.click()
    assert opened == [dlg.result.workbook]
    dlg.analyze_btn.click()
    assert tab.wait(60)
    assert tab.analytics_dialog.data.parts == [f"S-{p}" for p in range(1, 7)]
    tab.analytics_dialog.close()
    # Update with new files merges the folder again
    fill(template, folder / "part 7.xlsx", {"Part Number": "S-7", "Power (W)": 321})
    assert tab.update_btn.isEnabled()
    tab.update_btn.click()
    assert tab.wait(120)
    assert "S-7" in ParentCSV(tab.csv.text()).sample_ids()
    dlg.close()


def test_a_merge_that_fails_says_why_in_the_window(books, tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from test_ledger_tab import make_tab
    qapp = QApplication.instance() or QApplication([])
    template, _folder = books
    empty = tmp_path / "empty"
    empty.mkdir()
    tab, _ = make_tab(qapp)
    tab.on_merge_window()
    dlg = tab.merge_dialog
    dlg.template.setText(str(template))
    dlg.folder.setText(str(empty))
    dlg.on_merge()
    assert tab.wait(60)
    assert dlg.result is None
    assert "no filled copy" in dlg.report.toPlainText()
    dlg.close()


# ── partly filled copies: nothing is filled in (user, 2026-11-11: "The
# filled copies may not be completely filled so I don't want anything to
# accidentally get filled in through hallucinations") ─────────────────────
def test_blanks_in_partly_filled_copies_stay_blank_everywhere(books, tmp_path):
    template, folder = books
    # S-2 has no Power and only two of its flaws filled
    fill(template, folder / "part 2.xlsx",
         {"Part Number": "S-2", "Frequency (kHz)": 22},
         {FOD: {"1A": 41, "2A": 42}})
    res = lm.merge(template, folder)
    rows = ParentCSV(res.csv).rows_for("S-2")
    assert {r["Power (W)"] for r in rows} == {""}
    assert {r["Date"] for r in rows} == {""}
    by_item = {r["Flaw #"]: r[FOD] for r in rows}
    assert by_item["1A"] == "41" and by_item["2A"] == "42"
    assert all(v == "" for it, v in by_item.items() if it not in ("1A", "2A"))
    # its sheet and All data show the blanks as blank
    wb = openpyxl.load_workbook(res.workbook)
    schema = read_template(template)
    assert wb["S-2"][schema.field("Power (W)").cells[""]].value is None
    assert wb["S-2"][schema.field(FOD).cells["3A"]].value is None
    data = wb["All data"]
    head = [c.value for c in data[1]]
    s2 = [dict(zip(head, [c.value for c in r])) for r in data.iter_rows(min_row=2)
          if r[1].value == "S-2"]
    assert s2 and all(r["Power (W)"] is None for r in s2)
    # Analyze counts them as missing, never as values
    d = la.load(res.csv)
    cov = la.coverage(d).set_index("Field")
    assert cov.loc["Power (W)", "Filled"] == 5 and cov.loc["Power (W)", "Could hold"] == 6
    summ = la.summary(d).set_index("Field")
    assert summ.loc["Power (W)", "Count"] == 5
    assert "S-2" in set(la.missing_by_part(d).iloc[:, 0])


def test_a_blank_is_not_filled_from_another_copy_of_the_same_part(books):
    template, folder = books
    fill(template, folder / "part 3.xlsx",                 # S-3 without its Power
         {"Part Number": "S-3", "Frequency (kHz)": 23})
    fill(template, folder / "part 3 again.xlsx",
         {"Part Number": "S-3", "Power (W)": 999, "Frequency (kHz)": 23})
    res = lm.merge(template, folder)
    assert {r["Power (W)"] for r in ParentCSV(res.csv).rows_for("S-3")} == {""}
    listed = [p for p in res.clashes if p.part == "S-3" and p.field == "Power (W)"]
    assert len(listed) == 1
    assert "left blank" in listed[0].reason
    assert listed[0].kept == "(blank) (part 3.xlsx)" and listed[0].value == "999"
    # the other copy keeps its own sheet with its own value
    wb = openpyxl.load_workbook(res.workbook)
    power = read_template(template).field("Power (W)").cells[""]
    assert wb["S-3"][power].value is None and wb["S-3 (2)"][power].value == 999


def test_a_cleared_cell_is_not_given_back_the_templates_text(books, tmp_path):
    """A template cell holding text (here a field cell typed into the
    template itself) that a copy cleared stays clear on that copy's sheet."""
    template, folder = books
    schema = read_template(template)
    cell = schema.field("Power (W)").cells[""]
    wb = openpyxl.load_workbook(template)
    wb.worksheets[0][cell] = "enter power"
    pre = tmp_path / "prefilled.xlsx"
    wb.save(pre)
    out = tmp_path / "copies2"
    out.mkdir()
    fill(pre, out / "a.xlsx", {"Part Number": "S-1", "Power (W)": None})
    wb = openpyxl.load_workbook(out / "a.xlsx")
    wb.worksheets[0][cell] = None                            # the copy cleared it
    wb.save(out / "a.xlsx")
    res = lm.merge(pre, out)
    assert openpyxl.load_workbook(res.workbook)["S-1"][cell].value is None


def test_the_merge_asks_no_model_and_runs_no_extract():
    """The merge reads cells and nothing else: none of the model or Extract
    modules is imported by it or its window."""
    import ast
    banned = {"ledger_llm", "source_extract", "ledger_ingest", "model_backends",
              "council_engine", "ledger_teach", "ledger_linking", "role_models"}
    root = Path(__file__).resolve().parents[1]
    for rel in ("council_core/ledger_merge.py", "council_qt/tabs/ledger_merge_dialog.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[-1] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names |= {(node.module or "").split(".")[-1]}
                names |= {a.name for a in node.names}
        assert not names & banned, (rel, names & banned)
