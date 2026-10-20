"""Apply does what the reviewer chose, and the review is always written
(review, 2026-10-20).

What independent testers found, and what each test pins:
- a value marked N went into the parent CSV: read again, the same reading
  was found first at its 'also at' cell (a label learned at Apply ranked it
  higher), so it was 'not seen in the review' and taken as just mapped;
  Apply also said more values were accepted than were;
- a control character in a cell read through Excel ('Remark' + CHAR(1))
  made openpyxl raise IllegalCharacterError: no review, the whole extract
  failed;
- Apply after a cancelled Extract read every source again and wrote values
  from workbooks the review never showed;
- both rows of a red clash accepted: Apply kept one, silently;
- a label mapped on 'Labels to map' in a workbook whose part only
  Council's model found was dropped at Apply (read again without the
  model, its values had no part);
- the People sheet counted a signed reading twice (its template cell and
  its signer's own column), and 'First seen at' could be another
  inspector's cell.

Synthetic data only (tests/ledger_fixtures.py); the model is a fake.
"""
from __future__ import annotations

import csv
import json
import sys
import threading
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
import ledger_fixtures as lf  # noqa: E402
import sample_ledger as sl  # noqa: E402
import source_extract as se  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402

PRE = "NDE Inspection: Eddy Current Response Pre-Conceal"


@pytest.fixture()
def ledger(tmp_path):
    ps = li.create_parent(lf.make_template(tmp_path / "template.xlsx"))
    return Path(ps.csv), sl.ParentCSV(ps.csv).schema


class _NoProg:
    def update(self, *a, **k):
        pass

    def stage(self, *a, **k):
        pass

    def advance(self, *a, **k):
        pass


def _values(review):
    wb = openpyxl.load_workbook(review)
    return wb, [dict(zip(se.VAL_HEAD, (c.value for c in row)), row=row[0].row)
                for row in wb["Values"].iter_rows(min_row=2)]


def _csv_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# ── N means N ───────────────────────────────────────────────────────────
def _inspection_and_log(src: Path) -> None:
    """The same reading in an inspection workbook and in a weekly log
    (signed MK there), and one reading only in the log."""
    src.mkdir()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "EC"
    ws["A1"], ws["B1"] = "Part Number", "LMM9ZZ910"
    ws["A3"], ws["B3"] = "Flaw", "EC Pre-Conceal"
    ws["A4"], ws["B4"] = "1A", 55
    wb.save(src / "LMM9ZZ910 inspection.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Log"
    ws.append(["Part Number", "Flaw", "Stage", "Response", "Inspector"])
    ws.append(["LMM9ZZ910", "1A", "Pre-Conceal", 55, "MK"])       # the same reading
    ws.append(["LMM9ZZ910", "2A", "Pre-Conceal", 47, "MK"])       # only here
    wb.save(src / "EC_log.xlsx")


def test_a_value_marked_n_stays_out_when_read_again_at_its_also_at_cell(tmp_path, ledger):
    csv_path, _schema = ledger
    _inspection_and_log(tmp_path / "src")
    s = li.run_extract(csv_path, [tmp_path / "src"], tmp_path / "review.xlsx",
                       use_images=False)
    wb, rows = _values(s.review)
    pre = [r for r in rows if str(r["Field"] or "").startswith(PRE)]
    main = next(r for r in pre if r["Item"] == "1A" and r["Field"] == PRE)
    assert "also at EC_log.xlsx › Log!D2" in (main["Note"] or "")
    for r in pre:
        # accept the log's own 2A reading (it teaches 'Response Pre-Conceal');
        # 55 for 1A is wrong, everywhere
        wb["Values"].cell(r["row"], 1).value = "Y" if r["Item"] == "2A" else "N"
    wb.save(s.review)
    a = li.run_apply(s.review, use_images=False)
    assert a.learned                                     # the label was learned...
    row1a = next(r for r in _csv_rows(csv_path) if r["Flaw #"] == "1A")
    assert row1a[PRE] == "" and row1a.get(PRE + " [MK]", "") == ""   # ...and N is N
    row2a = next(r for r in _csv_rows(csv_path) if r["Flaw #"] == "2A")
    assert row2a[PRE] == "47" and row2a[PRE + " [MK]"] == "47"
    assert a.accepted == 2                       # 2A and MK's own copy of it, no more


def test_a_value_left_blank_on_values_is_not_taken_by_a_label_learned_at_apply(
        tmp_path, ledger):
    """Part 402's 'Scan rate' row was left blank (amber). On part 401's the
    reviewer typed another field and accepted it, which teaches the label:
    read again, 402's reading is that field - and still not accepted."""
    csv_path, _schema = ledger
    for part in ("401", "402"):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"], ws["B1"] = "Part Number", part
        ws["A2"], ws["B2"] = "Scan rate", 1200                   # an unsure match
        wb.save(tmp_path / f"build_{part}.xlsx")
    s = li.run_extract(csv_path, [tmp_path / "build_401.xlsx", tmp_path / "build_402.xlsx"],
                       tmp_path / "review.xlsx", use_images=False)
    wb, rows = _values(s.review)
    scan = {r["Part"]: r for r in rows if r["Source label"] == "Scan rate"}
    assert scan["402"]["Field"] == "Scan Speed (mm/s)" and not scan["402"]["Accept"]
    wb["Values"].cell(scan["401"]["row"], 1).value = "Y"
    wb["Values"].cell(scan["401"]["row"], 4).value = "Frequency (kHz)"   # typed
    wb.save(s.review)
    a = li.run_apply(s.review, use_images=False)
    assert a.learned == {"scan rate": "Frequency (kHz)"}
    got = {r["Part Number"]: r["Frequency (kHz)"] for r in _csv_rows(csv_path)}
    assert got["401"] == "1200" and got.get("402", "") == ""


# ── control characters ──────────────────────────────────────────────────
def test_a_control_character_does_not_stop_the_review(tmp_path, ledger):
    """openpyxl refuses CHAR(1) in a cell: every sheet write_review fills
    is given text without it."""
    csv_path, schema = ledger
    obs = [se.Obs("101\x02", "", "Power (W)", "285\x01", 1.0, "alias", "Power\x03 (W)",
                  "a.xls › Sheet\x04!B2", note="see log\x07", inspector="JB"),
           se.Obs("101", "1A", PRE, "63", 1.0, "alias", "EC Pre", "a.xls › Sheet!C4",
                  inspector="J\x05B")]
    run = se.Extraction(["a.xls"], obs=obs,
                        unmatched=[se.Obs("101", "", "", "see log\x07", 0.0, "none",
                                          "Remark\x01", "a.xls › Sheet!A8")],
                        images=[se.ImageNote("a.xls", "Sheet", "F2", "", "ocr",
                                             "Media | ceramic\x01")],
                        problems=["a.xls: odd text\x0b in a cell"])
    review = tmp_path / "review.xlsx"
    se.write_review(review, run, se.decide(run, schema), schema, str(csv_path))
    wb = openpyxl.load_workbook(review)
    text = [str(c.value) for ws in wb.worksheets for row in ws.iter_rows()
            for c in row if c.value is not None]
    assert not any(sl._ILLEGAL_XL.search(t) for t in text)
    vals = [dict(zip(se.VAL_HEAD, r)) for r in wb["Values"].iter_rows(min_row=2,
                                                                     values_only=True)]
    assert any(r["Value"] == "285" and r["Part"] == "101" for r in vals)
    lab = list(wb["Labels to map"].iter_rows(min_row=2, values_only=True))
    assert lab[0][2] == "Remark" and lab[0][3] == "see log"
    assert any("odd text" in t and "in a cell" in t for t in text)


def test_a_control_character_read_through_excel_reaches_the_csv_and_templates_clean(
        tmp_path, ledger, monkeypatch):
    """The whole loop on a workbook 'read through Excel' (the .xls reader
    is faked): the review is written, Apply writes the value without the
    control character to the parent CSV and to a filled template."""
    csv_path, schema = ledger
    src = tmp_path / "PN216_ctrl.xls"
    src.write_bytes(b"not read: the reader below stands in for Excel")

    def fake_grids(path, notes=None, *a, **k):
        return [se.Grid(Path(path).name, "Sheet1", {
            (1, 1): "Part Number", (1, 2): "216",
            (2, 1): "Power (W)", (2, 2): "285\x01",
            (3, 1): "Date", (3, 2): "2026-10-19\x01",
            (8, 1): "Remark\x01", (8, 2): "see log\x07"})], []
    monkeypatch.setattr(se, "load_workbook_grids", fake_grids)
    s = li.run_extract(csv_path, [src], tmp_path / "review.xlsx", use_images=False)
    wb, rows = _values(s.review)
    for r in rows:
        wb["Values"].cell(r["row"], 1).value = "Y"
    wb.save(s.review)
    li.run_apply(s.review, fill_dir=tmp_path / "filled", use_images=False)
    row = next(r for r in _csv_rows(csv_path) if r["Part Number"] == "216")
    assert row["Power (W)"] == "285" and row["Date"] == "2026-10-19"
    assert not sl._ILLEGAL_XL.search(Path(csv_path).read_text(encoding="utf-8-sig"))
    out = openpyxl.load_workbook(next((tmp_path / "filled").glob("*.xlsx"))).active
    assert out[schema.field("Power (W)").cells[""]].value == 285


def test_the_parent_csv_and_a_filled_template_take_no_control_character(tmp_path, ledger):
    csv_path, schema = ledger
    led = sl.ParentCSV(csv_path)
    led.add_samples([{"Part Number": "301", "Flaw #": "1A", "Date": "2026\x01-10-19"}])
    assert sl.ParentCSV(csv_path).get("301", "1A")["Date"] == "2026 -10-19"
    # a CSV a person (or an older Council) wrote with one in it still fills
    text = Path(csv_path).read_text(encoding="utf-8-sig").replace("2026 -10-19",
                                                                  "2026-10-19\x01")
    Path(csv_path).write_text(text, encoding="utf-8-sig")
    out = tmp_path / "filled.xlsx"
    sl.fill_template(schema, sl.ParentCSV(csv_path).rows_for("301"), out)
    got = openpyxl.load_workbook(out).active[schema.field("Date").cells[""]].value
    assert got == "2026-10-19"


# ── Apply reads only what the review holds ──────────────────────────────
def test_apply_after_a_cancelled_extract_reads_only_the_workbooks_in_the_review(
        tmp_path, ledger):
    csv_path, _schema = ledger
    a = lf.make_laser_log(tmp_path / "laser_101.xlsx")
    wb = openpyxl.load_workbook(a)
    wb["Build"]["A3"] = "P/N: 103"
    wb.save(tmp_path / "laser_103.xlsx")
    cancel = threading.Event()

    def progress(line, frac=None):
        if " read in " in line:
            cancel.set()                           # Cancel, once the first is read
    s = li.run_extract(csv_path, [a, tmp_path / "laser_103.xlsx"], tmp_path / "review.xlsx",
                       use_images=False, progress=progress, cancel=cancel)
    assert s.cancelled
    wb, rows = _values(s.review)
    assert {r["Part"] for r in rows} == {"101"}
    meta = json.loads(wb["_run"]["A1"].value)
    assert len(meta["sources"]) == 2 and meta["read"] == [0]
    read_me = [c.value for c in wb["Read me"]["A"] if c.value]
    assert any("laser_103.xlsx" in t and "not read" in t for t in read_me)
    for r in rows:                                 # accept all: FOD is learned
        wb["Values"].cell(r["row"], 1).value = "Y"
    wb.save(s.review)
    lines = []
    ap = li.run_apply(s.review, use_images=False, progress=lambda l, f=None: lines.append(l))
    assert ap.learned
    assert {r["Part Number"] for r in _csv_rows(csv_path)} == {"101"}
    assert not any("laser_103" in line for line in lines)


def test_an_older_review_without_the_list_still_reads_every_source(tmp_path, ledger):
    csv_path, schema = ledger
    a = lf.make_laser_log(tmp_path / "laser_101.xlsx")
    s = li.run_extract(csv_path, [a], tmp_path / "review.xlsx", use_images=False)
    wb = openpyxl.load_workbook(s.review)
    meta = json.loads(wb["_run"]["A1"].value)
    assert "read" not in meta                      # every source was read
    ap = li.run_apply(s.review, use_images=False)
    assert ap.rows and {r["Part Number"] for r in _csv_rows(csv_path)} == {"101"}


# ── both rows of a red clash ────────────────────────────────────────────
def test_two_different_values_accepted_for_one_cell_are_both_left_out(tmp_path, ledger):
    csv_path, _schema = ledger
    (tmp_path / "src").mkdir()
    for name, power in (("build.xlsx", 285), ("summary.xlsx", 290)):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"], ws["B1"] = "Part Number", "LMM9ZZ909"
        ws["A2"], ws["B2"] = "Power (W)", power
        ws["A3"], ws["B3"] = "Frequency (kHz)", 50
        wb.save(tmp_path / "src" / name)
    s = li.run_extract(csv_path, [tmp_path / "src"], tmp_path / "review.xlsx",
                       use_images=False)
    wb, rows = _values(s.review)
    power = [r for r in rows if r["Field"] == "Power (W)"]
    assert len(power) == 2 and all(r["Other values offered"] for r in power)
    for r in rows:
        wb["Values"].cell(r["row"], 1).value = "Y"
    wb.save(s.review)
    read_me = " ".join(c.value for c in wb["Read me"]["A"] if c.value)
    assert "both left out at Apply" in read_me
    a = li.run_apply(s.review, use_images=False)
    got = {r["Power (W)"] for r in _csv_rows(csv_path)}
    assert got == {""}                                  # neither, not one of them
    assert {r["Frequency (kHz)"] for r in _csv_rows(csv_path)} == {"50"}
    said = [p for p in a.problems if "285" in p and "290" in p]
    assert said == ["2 values accepted for LMM9ZZ909 Power (W) (285, 290) — kept none; "
                    "choose one"]
    assert a.accepted == 1                              # the Frequency
    assert said[0] in a.describe()


# ── a label mapped in a workbook whose part the model found ─────────────
class FakeModel:
    """Finds the part; places no label."""
    def __init__(self, part="XYZ9AB123-R2"):
        self.part = part

    def __call__(self, prompt, schema):
        if "Which part" in prompt:
            return json.dumps({"part": self.part, "evidence": "Serial"}), "fake-granite"
        return json.dumps({}), "fake-granite"


def test_a_label_mapped_in_a_workbook_whose_part_the_model_found_keeps_that_part(
        tmp_path, ledger):
    csv_path, schema = ledger
    freq = next(f.name for f in schema.fields if "Frequency" in f.name)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Serial: XYZ9AB123-R2"
    ws["A3"], ws["B3"] = "Power (W)", 280
    ws["A4"], ws["B4"] = "Zorp rate", 55
    wb.save(tmp_path / "untitled.xlsx")
    s = li._extract(str(csv_path), [str(tmp_path / "untitled.xlsx")], tmp_path / "r.xlsx",
                    "", False, None, _NoProg(), None, None, None, True, FakeModel())
    wb = openpyxl.load_workbook(s.review)
    lab = wb["Labels to map"]
    row = next(r for r in lab.iter_rows(min_row=2) if r[2].value == "Zorp rate")
    assert row[7].value == "XYZ9AB123-R2"                 # the part the row shows
    row[0].value = freq                                   # mapped, as the Read me says
    wb.save(s.review)
    meta = json.loads(wb["_run"]["A1"].value)
    assert meta["model_parts"]["untitled.xlsx"][0] == "XYZ9AB123-R2"
    a = li.run_apply(s.review, use_images=False)
    got = {r["Part Number"]: r[freq] for r in _csv_rows(csv_path) if r[freq]}
    assert got == {"XYZ9AB123-R2": "55"}
    assert a.accepted == 1
    # the model's part is still never pre-accepted: Power was not accepted
    assert not any(r["Power (W)"] for r in _csv_rows(csv_path))


def test_a_mapped_value_with_no_part_is_said_not_dropped(tmp_path, ledger):
    csv_path, schema = ledger
    freq = next(f.name for f in schema.fields if "Frequency" in f.name)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A3"], ws["B3"] = "Power (W)", 280
    ws["A4"], ws["B4"] = "Zorp rate", 55
    wb.save(tmp_path / "untitled.xlsx")
    s = li.run_extract(csv_path, [tmp_path / "untitled.xlsx"], tmp_path / "r.xlsx",
                       use_images=False)
    wb = openpyxl.load_workbook(s.review)
    row = next(r for r in wb["Labels to map"].iter_rows(min_row=2)
               if r[2].value == "Zorp rate")
    row[0].value = freq
    wb.save(s.review)
    a = li.run_apply(s.review, use_images=False)
    assert a.accepted == 0
    assert any(p.startswith("1 accepted or mapped values have no Part Number")
               for p in a.problems)


# ── People: each reading once, at its signer's cell ─────────────────────
def test_people_count_each_reading_once_at_its_signers_cell(tmp_path, ledger):
    csv_path, schema = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "EC"
    ws["A1"], ws["A2"] = "Part Number: LMM9ZZ908", "Inspector: MK"
    ws["B3"], ws["C3"], ws["D3"], ws["E3"] = "Flaw #", "1A", "2A", "3A"
    ws["B4"] = "Eddy Current Response Pre-Conceal"
    ws["C4"], ws["D4"], ws["E4"] = 40, 41, 42
    wb.save(tmp_path / "mk.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "EC"
    ws["A1"], ws["A2"] = "Part Number: LMM9ZZ908", "Inspector: JB"
    ws["B3"], ws["C3"], ws["D3"], ws["E3"] = "Flaw #", "1A", "2A", "3A"
    ws["B4"] = "Eddy Current Response Pre-Conceal"
    ws["C4"], ws["D4"], ws["E4"] = 40, 50, 51                    # 1A agrees with MK
    wb.save(tmp_path / "jb.xlsx")
    run = se.extract(schema, [tmp_path / "jb.xlsx", tmp_path / "mk.xlsx"], use_images=False)
    dec = se.decide(run, schema)
    shared = next(d for d in dec if d.obs.field == PRE and d.obs.item == "1A")
    assert shared.obs.inspector == "JB/MK" and shared.obs.where.startswith("jb.xlsx")
    people = se.review_initials(dec)
    assert people["MK"] == (3, "mk.xlsx › EC!C4")        # not 6, not JB's cell
    assert people["JB"] == (3, "jb.xlsx › EC!C4")
