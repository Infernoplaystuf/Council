"""The Ledger's rule-based extract on layouts real inspection workbooks have
(review, 2026-10-19).

What independent testers found, and what each test pins:
- a reading typed with the signer's initials beside it ('31 (JB)') was kept
  as the text '31 (JB)', pre-accepted, and put down to the sheet heading's
  inspector;
- a row of FPI result codes ('NRI | IND') right above the next flaw table
  was taken as an inspector's heading: the People sheet asked 'Who is NRI?';
- digits inside a file name ('EC_log_week41', 'LMM1AB101_laser_build') became
  the part, pre-accepted;
- two workbooks with no part number and the same Frequency were one review
  row, and the second sample lost the value at Apply;
- a 'Parameter | Value' table made every part 'Value';
- 'S/N' was not read as the part's label;
- 'Owner', 'Tower' ... were offered as Power (W) on letters alone;
- a password-protected .xlsx was counted as an 'other' open failure.

And what a review of those fixes found (review, 2026-10-19):
- an S/N (signal-to-noise) column became every row's part, pre-accepted,
  and 'Inspector: SN' was no one;
- a heading of initials beside a written date, a time or a title ('Day 1')
  was no longer a heading;
- '50 (FSH)', '54 (NG)', '12 (IPM)' made people FSH, NG, IPM - and a mm/s
  field took the bare 12;
- a padded cell beside an initials heading backtracked for seconds;
- real slips ('Powr', 'Meda', 'Datte') no longer matched, and 'Powder' was
  still Power (W), pre-accepted;
- a row of 'NG' results with no label over the next table was an inspector;
- 'Run #: 3' named the part.

And what testers found after those (review, 2026-10-19):
- 'TBD', 'N/A', 'to be assigned', '-' in the Part Number was the part,
  pre-accepted, and Apply wrote it to the parent CSV; a part not of the
  user's part-name pattern was pre-accepted too;
- a 'Lot Number', 'Job Number', 'Drawing Number' column was every row's
  part, pre-accepted; 'Part Revision' named the part, and so did the
  'Lot Number: ...' cell under a 'Part Information' heading.

Synthetic data only (tests/ledger_fixtures.py).
"""
from __future__ import annotations

import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
import ledger_fixtures as lf  # noqa: E402
import source_extract as se  # noqa: E402
from sample_ledger import ParentCSV, read_template  # noqa: E402

POST = "NDE Inspection: Eddy Current Response Post-Conceal"
PRE = "NDE Inspection: Eddy Current Response Pre-Conceal"


@pytest.fixture
def ledger(tmp_path):
    schema = read_template(lf.make_template(tmp_path / "tmpl.xlsx"))
    csv = tmp_path / "parent.csv"
    ParentCSV.create(csv, schema)
    return schema, csv


def _decided(schema, wb, path, **kw):
    wb.save(path)
    run = se.extract(schema, [path], use_images=False, **kw)
    return run, se.decide(run, schema)


# ── 1. a reading signed in its cell ─────────────────────────────────────
def test_a_reading_signed_in_brackets_is_the_number_and_its_signer(ledger, tmp_path):
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Post-Conceal"
    ws["A1"] = "Part Number: LMM1AB102"
    ws["A2"] = "Inspector: MK"
    ws["B4"], ws["B5"] = "Flaw #", "EC Post-Conceal"
    typed = ["30", "31 (JB)", "32 [JB]", "33 (J.B.)", "34 (MK)", "35 (NA)"]
    for i, (it, v) in enumerate(zip(lf.ITEMS, typed)):
        ws.cell(4, 3 + i, it)
        ws.cell(5, 3 + i, v)
    ws["A8"], ws["B8"] = "Power (W)", "285 (JB)"        # a field nobody signs
    run, dec = _decided(schema, wb, tmp_path / "pc.xlsx")
    main = {d.obs.item: d.obs for d in dec if d.obs.field == POST}
    assert {it: (o.value, o.inspector) for it, o in main.items()} == {
        "1A": ("30", "MK"), "2A": ("31", "JB"), "3A": ("32", "JB"),
        "1B": ("33", "JB"), "2B": ("34", "MK"),
        "3B": ("35 (NA)", "MK")}          # 'NA' is no one: the cell is left as typed
    own = {(d.obs.field, d.obs.item): d for d in dec}
    assert own[(f"{POST} [JB]", "2A")].obs.value == "31" and own[(f"{POST} [JB]", "2A")].accept
    assert (f"{POST} [MK]", "2A") not in own              # not MK's reading
    power = [o for o in run.obs if o.field == "Power (W)"]
    assert [(o.value, o.inspector) for o in power] == [("285", "")]
    assert set(se.review_initials(dec)) == {"JB", "MK"}


def test_brackets_holding_a_unit_or_a_code_are_not_a_signer():
    for unit in ("mm", "MM", "in", "IN", "um", "deg", "kW", "KW", "Hz", "min"):
        assert se._signed_by(unit) == "", unit
    for code in ("NA", "AVG", "avg", "MAX", "OK", "TBD", "NRI", "CCW", "REF", "TYP"):
        assert se._signed_by(code) == "", code
    assert se._signed_by("Jb") == "" and se._signed_by("typ") == ""   # a word, not initials
    assert se._signed_by("JB") == se._signed_by("J.B.") == se._signed_by("j.b.") == "JB"
    assert se._signed_by("EC") == "EC"                                 # Eva Chen signs as EC


def test_a_value_with_its_unit_in_brackets_is_not_signed(ledger, tmp_path):
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Part Number: 101"
    ws["A2"] = "Inspector: MK"
    ws["B4"], ws["B5"] = "Flaw #", "EC Pre-Conceal"
    for i, (it, v) in enumerate(zip(["1A", "2A", "3A"], ["0.245 (mm)", "41", "42"])):
        ws.cell(4, 3 + i, it)
        ws.cell(5, 3 + i, v)
    run, _ = _decided(schema, wb, tmp_path / "unit.xlsx")
    got = {o.item: (o.value, o.inspector) for o in run.obs if o.field == PRE}
    assert got["1A"] == ("0.245 (mm)", "MK")


def test_brackets_holding_an_nde_unit_or_result_are_not_a_signer(ledger, tmp_path):
    """'50 (FSH)' is 50 % full screen height, '54 (NG)' no good, '53 (EA)'
    each: none is a person who signed the reading."""
    for word in ("FSH", "DB", "NG", "EA", "IPM", "RPM", "PSI", "PCS", "OD", "NOGO"):
        assert se._signed_by(word) == "", word
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pre"
    ws["A1"], ws["A2"] = "Part Number: 101", "Inspector: MK"
    ws["B4"], ws["B5"] = "Flaw #", "EC Pre-Conceal"
    typed = ["50 (FSH)", "51 (DB)", "53 (EA)", "54 (NG)", "55 (JB)"]
    for i, (it, v) in enumerate(zip(lf.ITEMS, typed)):
        ws.cell(4, 3 + i, it)
        ws.cell(5, 3 + i, v)
    run, dec = _decided(schema, wb, tmp_path / "tags.xlsx")
    got = {o.item: (o.value, o.inspector) for o in run.obs if o.field == PRE}
    assert got == {"1A": ("50 (FSH)", "MK"), "2A": ("51 (DB)", "MK"),
                   "3A": ("53 (EA)", "MK"), "1B": ("54 (NG)", "MK"),
                   "2B": ("55", "JB")}
    assert set(se.review_initials(dec)) == {"JB", "MK"}         # no 'Who is FSH?'


def test_a_unit_in_brackets_is_converted_or_kept_and_flagged(ledger, tmp_path):
    """'0.3 (kW)' is 300 W; '12 (IPM)' is in/min, which the template's mm/s
    cannot take as 12: the text is kept and the row is not pre-accepted."""
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Build"
    ws["A1"] = "Part Number: 101"
    ws["A3"], ws["B3"] = "Power (W)", "0.3 (KW)"
    ws["A4"], ws["B4"] = "Scan Speed (mm/s)", "12 (IPM)"
    ws["A5"], ws["B5"] = "Frequency (kHz)", "100 (RPM)"
    _, dec = _decided(schema, wb, tmp_path / "units.xlsx")
    got = {d.obs.field: (d.obs.value, d.accept, d.obs.note) for d in dec}
    assert got["Power (W)"] == ("300", True, "converted KW -> W")
    assert got["Scan Speed (mm/s)"][:2] == ("12 (IPM)", False)
    assert "not converted" in got["Scan Speed (mm/s)"][2]
    assert got["Frequency (kHz)"][:2] == ("100 (RPM)", False)


def test_long_cells_do_not_backtrack(ledger, tmp_path):
    """A padded cell on an initials heading's row took 6.5 s at 1,600
    spaces (cubic); a long run of digits in a reading took the signed-
    reading pattern quadratic time. Both are well under a second now."""
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Part Number: 101"
    ws["B2"], ws["D2"] = "JB", "Date" + " " * 2400 + "Sign"
    ws["B3"], ws["C3"] = "Flaw", "EC Pre"
    ws["B4"], ws["C4"] = "1A", "1" * 20000
    ws["B5"], ws["C5"] = "2A", 62
    wb.save(tmp_path / "pad.xlsx")
    t0 = time.perf_counter()
    run = se.extract(schema, [tmp_path / "pad.xlsx"], use_images=False)
    took = time.perf_counter() - t0
    assert took < 2, f"took {took:.1f}s"
    assert {o.inspector for o in run.obs if o.field == PRE} == {"JB"}


# ── 2. result codes are not an inspector's heading ──────────────────────
def _stacked_blocks(path, results):
    """'Inspector: AK'; per block a Flaw # row, an EC Pre row and an FPI row
    of result codes, stacked with no blank row between blocks."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pre"
    ws["A1"] = "Inspector: AK"
    r = 2
    for bi, b in enumerate("ABC"):
        ws.cell(r, 2, "Flaw #")
        ws.cell(r + 1, 2, "EC Pre (%FSH)")
        ws.cell(r + 2, 2, "FPI")
        for i in range(3):
            ws.cell(r, 3 + i, f"{i + 1}{b}")
            ws.cell(r + 1, 3 + i, 50 + 10 * bi + i)
            ws.cell(r + 2, 3 + i, results[i])
        r += 3
    wb.save(path)
    return path


@pytest.mark.parametrize("results", [["NRI", "IND", "NRI"], ["OK", "NG", "OK"],
                                     ["ACC", "REJ", "NCR"]])
def test_result_codes_over_the_next_table_are_not_an_inspector(ledger, tmp_path, results):
    schema, _ = ledger
    src = _stacked_blocks(tmp_path / "101 inspection.xlsx", results)
    run = se.extract(schema, [src], use_images=False, part="101")
    dec = se.decide(run, schema)
    pre = [d.obs for d in dec if d.obs.field == PRE]
    assert len(pre) == 9 and {o.inspector for o in pre} == {"AK"}
    assert set(se.review_initials(dec)) == {"AK"}           # no 'Who is NRI?'
    assert not any(d.obs.field.endswith(("[NRI]", "[IND]", "[NG]", "[REJ]")) for d in dec)


def test_an_initials_heading_beside_its_date_or_another_still_counts(ledger, tmp_path):
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Part Number: 101"
    ws["B2"], ws["C2"] = "JB", datetime(2026, 9, 18)      # a typed date beside it
    ws["H2"] = "MK — 2026-09-19"                         # side by side
    for left, base in ((2, 60), (8, 70)):
        ws.cell(3, left, "Flaw")
        ws.cell(3, left + 1, "EC Pre")
        for i, it in enumerate(["1A", "2A"], start=4):
            ws.cell(i, left, it)
            ws.cell(i, left + 1, base + i)
    run, _ = _decided(schema, wb, tmp_path / "side.xlsx")
    got = sorted((o.item, o.value, o.inspector) for o in run.obs if o.field == PRE)
    assert got == [("1A", "64", "JB"), ("1A", "74", "MK"),
                   ("2A", "65", "JB"), ("2A", "75", "MK")]


@pytest.mark.parametrize("beside", ["18-Sep-2026", "Sep 18, 2026", "2026-09-18 08:30",
                                    "Day 1", "EC Pre-Conceal readings", "Date: 9/18/2026"])
def test_an_initials_heading_beside_a_written_date_or_a_title_counts(ledger, tmp_path,
                                                                     beside):
    """Only a typed '2026-09-18' could share the heading's row: 'JB' beside
    '18-Sep-2026' or 'Day 1' lost its readings' inspector and was offered
    as a label to map."""
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Part Number: 101"
    ws["B2"], ws["D2"] = "JB", beside
    ws["B3"], ws["C3"] = "Flaw", "EC Pre"
    ws["B4"], ws["C4"] = "1A", 61
    ws["B5"], ws["C5"] = "2A", 62
    run, _ = _decided(schema, wb, tmp_path / "beside.xlsx")
    assert {o.inspector for o in run.obs if o.field == PRE} == {"JB"}
    assert "JB" not in {o.label for o in run.unmatched}


@pytest.mark.parametrize("results", [["NG", "NG", "NG"], ["GO", "NOGO", "GO"],
                                     ["XR", "XR", "XR"]])
def test_a_row_of_results_with_no_label_is_not_an_inspector(ledger, tmp_path, results):
    """No 'FPI' label to give the row away: the codes alone, side by side
    over one table, are results - not inspectors' headings."""
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pre"
    ws["A1"] = "Inspector: AK"
    r = 2
    for bi, b in enumerate("ABC"):
        ws.cell(r, 2, "Flaw #")
        ws.cell(r + 1, 2, "EC Pre (%FSH)")
        for i in range(3):
            ws.cell(r, 3 + i, f"{i + 1}{b}")
            ws.cell(r + 1, 3 + i, 50 + 10 * bi + i)
            ws.cell(r + 2, 3 + i, results[i])
        r += 3
    run, dec = _decided(schema, wb, tmp_path / "ng.xlsx", part="101")
    assert {o.inspector for o in run.obs if o.field == PRE} == {"AK"}
    assert set(se.review_initials(dec)) == {"AK"}


def test_a_result_after_its_row_label_is_not_an_inspector(ledger, tmp_path):
    """'Result | RLI' over the next table: RLI is the label's value."""
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["A2"] = "Part Number: 101", "Inspector: AK"
    ws["B3"], ws["C3"] = "Result", "RLI"
    ws["B4"], ws["C4"], ws["D4"] = "Flaw", "EC Pre", "EC Post"
    ws["B5"], ws["C5"], ws["D5"] = "1A", 61, 21
    run, dec = _decided(schema, wb, tmp_path / "rli.xlsx")
    assert {o.inspector for o in run.obs if o.field == PRE} == {"AK"}
    assert set(se.review_initials(dec)) == {"AK"}


def test_the_knowledge_graph_does_not_take_result_codes_for_initials():
    from council_core.kg_excel import is_initials
    for code in ("NRI", "IND", "NDI", "REJ", "ACC", "RJT", "NCR", "OK", "NA", "TBD"):
        assert not is_initials(code), code
    assert is_initials("EC") and is_initials("JB")


# ── 3. digits inside a file name ────────────────────────────────────────
def test_a_number_inside_a_word_of_the_file_name_is_not_a_part():
    assert se._part_from_filename("EC_log_week41") == ""
    assert se._part_from_filename("LMM1AB101_laser_build") == ""
    assert se._part_from_filename("101 inspection") == "101"
    assert se._part_from_filename("line3_2048") == "2048"
    assert se._part_from_filename("inspection_2041_run30") == ""      # two: no guess
    mask = se.mask_to_regex("@@@#@@###")
    assert se._part_from_filename("EC_log_week41", mask) == ""
    assert se._part_from_filename("run_2026", mask) == ""             # not the user's shape
    assert se._part_from_filename("LMM1AB101_laser_build", mask) == "LMM1AB101"


def test_a_part_from_the_file_name_is_never_pre_accepted(ledger, tmp_path):
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Settings"
    ws["A1"], ws["B1"] = "Frequency (kHz)", 100
    ws["A2"], ws["B2"] = "Power (W)", 250
    _, dec = _decided(schema, wb, tmp_path / "run_2026.xlsx")
    assert {d.obs.part for d in dec} == {"2026"}
    assert all(d.obs.part_how.startswith("file name") for d in dec)
    assert not any(d.accept for d in dec)


# ── 4. part-less workbooks keep their own rows ──────────────────────────
def test_two_partless_workbooks_with_the_same_values_keep_their_own_rows(ledger, tmp_path):
    schema, csv = ledger
    parts = {"alpha": "AAA1AA111", "beta": "BBB2BB222"}
    sources = []
    for name, power in (("alpha", 280), ("beta", 290)):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Build"
        ws["A1"] = "Laser build record"
        ws["A3"], ws["B3"] = "Frequency (kHz)", 50
        ws["A4"], ws["B4"] = "Date", "2026-10-05"
        ws["A5"], ws["B5"] = "Power (W)", power
        wb.save(tmp_path / f"{name}_log.xlsx")
        sources.append(tmp_path / f"{name}_log.xlsx")
    run = se.extract(schema, sources, use_images=False)
    dec = se.decide(run, schema)
    freq = [d for d in dec if d.obs.field == "Frequency (kHz)"]
    assert sorted(d.obs.where.split(" › ")[0] for d in freq) == ["alpha_log.xlsx",
                                                                 "beta_log.xlsx"]
    assert not any("also at" in d.obs.note or d.conflict for d in dec)

    review = tmp_path / "review.xlsx"
    se.write_review(review, run, dec, schema, str(csv))
    wb = openpyxl.load_workbook(review)
    ws = wb["Values"]
    head = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        src = str(row[head.index("Source")].value or "")
        row[head.index("Part")].value = parts[src.split("_")[0]]
        row[head.index("Accept")].value = "Y"
    wb.save(review)
    se.apply_review(review, use_images=False)
    ledger_csv = ParentCSV(csv)
    for name, power in (("alpha", "280"), ("beta", "290")):
        row = ledger_csv.rows_for(parts[name])[0]
        assert (row["Frequency (kHz)"], row["Date"], row["Power (W)"]) == \
            ("50", "2026-10-05", power), name


# ── 5. 'Parameter | Value' is not the part number ───────────────────────
def test_a_parameter_value_table_does_not_name_the_part(ledger, tmp_path):
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Build"
    ws["A1"] = "P/N: 401"
    ws["A3"], ws["B3"] = "Freq (kHz)", 50
    st = wb.create_sheet("Settings")
    st["A1"], st["B1"] = "Parameter", "Value"
    st["A2"], st["B2"] = "Scan Speed", 1200
    run, _ = _decided(schema, wb, tmp_path / "plain_401.xlsx")
    assert {(o.field, o.part) for o in run.obs} == {("Frequency (kHz)", "401"),
                                                    ("Scan Speed (mm/s)", "401")}


def test_only_a_sure_label_names_the_part(ledger):
    """find_part's own rule, whatever the matcher scores: a label that only
    looks like 'Part Number' is not it."""
    schema, _ = ledger
    m = se.Matcher(schema)
    ex = se._Extractor(se.Grid("f.xlsx", "S", {}), m, se.ItemCodes(schema.items), "")
    pn = m.by_name[schema.id_field]
    assert not ex._sure_id("Parameter", se.Match(pn, 0.6, via="match"))
    assert not ex._sure_id("Partner", se.Match(pn, 0.78, via="match"))
    assert ex._sure_id("Part No.", se.Match(pn, 0.7, via="match"))       # shares a word
    assert ex._sure_id("Serial Number", se.Match(pn, 0.67, via="match"))
    assert ex._sure_id("Serial No.", se.Match(pn, 0.67, via="match"))
    assert ex._sure_id("Lot", se.Match(pn, 1.0, via="alias"))
    assert ex._sure_id("Prt Nmbr", se.Match(pn, 0.9, via="match"))
    # 'number' alone is no part's word: these share nothing else
    for label, score in (("Run Number", 0.76), ("Lot Number", 0.76), ("Job Number", 0.67),
                         ("Heat Number", 0.82), ("Drawing Number", 0.64), ("Run #", 0.76)):
        assert not ex._sure_id(label, se.Match(pn, score, via="match")), label


def test_a_run_number_does_not_name_the_part(ledger, tmp_path):
    """'Run #: 3' made the part '3', pre-accepted."""
    schema, _ = ledger
    for i, label in enumerate(["Run #: 3", "Run Number: 3", "Heat Number: 3"]):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Build"
        ws["A1"] = label
        ws["A3"], ws["B3"] = "Frequency (kHz)", 50
        run, dec = _decided(schema, wb, tmp_path / f"LMM1AB101_g{i}.xlsx")
        assert [(o.field, o.part) for o in run.obs] == [("Frequency (kHz)", "")], label
        assert not any(d.accept for d in dec)


# ── 6. S/N names the part ───────────────────────────────────────────────
def test_s_n_is_the_serial_number_label(ledger, tmp_path):
    schema, _ = ledger
    for i, (label, inline) in enumerate([("S/N", True), ("S/N", False), ("SN", False),
                                         ("S/N:", False), ("S/N #", True),
                                         ("Serial No.", False), ("Serial Number", False)]):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Build"
        pid = f"BCD2EF34{i}"
        if inline:
            ws["A2"] = f"{label}: {pid}"
        else:
            ws["A2"], ws["B2"] = label, pid
        ws["A4"], ws["B4"] = "Freq (kHz)", 50 + i
        run, _ = _decided(schema, wb, tmp_path / f"case{i}.xlsx")
        assert [(o.field, o.part) for o in run.obs] == [("Frequency (kHz)", pid)], label
        assert not run.unmatched, label                 # the S/N label is not a label to map


@pytest.mark.parametrize("head", ["S/N", "S/N Ratio", "SN", "S/N (dB)"])
def test_a_signal_to_noise_column_is_not_the_part(ledger, tmp_path, head):
    """In an EC or UT table S/N is signal-to-noise: its 3.2, 4.2, 5.2 became
    every row's part, pre-accepted, over the sheet's 'Part Number'."""
    schema, _ = ledger
    for top in (True, False):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "EC"
        if top:
            ws["A1"] = "Part Number: LMM1AB101"
        ws["A2"] = "Inspector: JB"
        ws["B3"], ws["C3"], ws["D3"] = "Flaw #", "EC Pre-Conceal", head
        for i, it in enumerate(["1A", "2A", "3A"]):
            ws.cell(4 + i, 2, it)
            ws.cell(4 + i, 3, 60 + i)
            ws.cell(4 + i, 4, 3.2 + i)
        _, dec = _decided(schema, wb, tmp_path / f"snr_{top}.xlsx")
        pre = [d for d in dec if d.obs.field == PRE]
        assert len(pre) == 3
        assert {d.obs.part for d in pre} == ({"LMM1AB101"} if top else {""}), (head, top)
        assert all(d.accept for d in pre) if top else not any(d.accept for d in pre)


def test_a_signal_to_noise_row_or_value_is_not_the_part(ledger, tmp_path):
    schema, _ = ledger
    wb = openpyxl.Workbook()                     # an item grid with an S/N (dB) row
    ws = wb.active
    ws.title = "EC"
    ws["A1"] = "Part Number: LMM1AB101"
    ws["B3"] = "Flaw #"
    for i, it in enumerate(["1A", "2A", "3A"]):
        ws.cell(3, 3 + i, it)
        ws.cell(4, 3 + i, 60 + i)
        ws.cell(5, 3 + i, 3 + i)
    ws["B4"], ws["B5"] = "EC Pre-Conceal", "S/N (dB)"
    run, _ = _decided(schema, wb, tmp_path / "grid.xlsx")
    assert not any(o.field == schema.id_field for o in run.obs)   # no 'Part Number = 3'
    assert {o.label for o in run.unmatched} == {"S/N (dB)"}
    assert {o.part for o in run.obs} == {"LMM1AB101"}
    for i, typed in enumerate(["S/N: 3.2", "S/N: 12 dB", "S/N: 12"]):   # a ratio on a form
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "EC"
        ws["A1"] = typed
        ws["A3"], ws["B3"] = "Frequency (kHz)", 100
        run, _ = _decided(schema, wb, tmp_path / f"form{i}.xlsx")
        assert [o.part for o in run.obs] == [""], typed


@pytest.mark.parametrize("who", ["Inspector: SN", "SN"])
def test_an_inspector_who_signs_sn_is_a_person(ledger, tmp_path, who):
    """'SN' read as 'serial number', a word of the template: 'Inspector:
    SN' was no one and went to the labels to map."""
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pre"
    ws["A1"] = "Part Number: 101"
    ws["B2"] = who
    ws["B3"], ws["C3"] = "Flaw", "EC Pre"
    ws["B4"], ws["C4"] = "1A", 61
    ws["B5"], ws["C5"] = "2A", 62
    run, dec = _decided(schema, wb, tmp_path / "sn.xlsx")
    assert {o.inspector for o in run.obs if o.field == PRE} == {"SN"}
    assert set(se.review_initials(dec)) == {"SN"}
    assert not run.unmatched


# ── 7. letters alone do not make a label a field ────────────────────────
def test_a_label_sharing_no_word_with_a_field_is_not_offered(ledger):
    schema, _ = ledger
    m = se.Matcher(schema)
    for label in ("Owner", "Tower", "Lower", "Mower", "Flower", "Parameter", "Partner",
                  "Powder", "Powder Lot", "Powder Type", "Medal", "Medial", "Data"):
        for per_item in (False, None, True):
            assert m.match(label, per_item=per_item).target is None, (label, per_item)
    assert m.match("Location", per_item=True).target is None     # not Flaw Orientation
    assert m.match("Power (W)").target.name == "Power (W)"
    assert m.match("Amplitudes").sure                            # a plural is the word
    assert m.match("Orientations", per_item=True).sure


@pytest.mark.parametrize("label,field,per_item", [
    ("Frequncy", "Frequency (kHz)", None), ("Amplitde", "Concealment: Amplitude", None),
    ("Ampl", "Concealment: Amplitude", None), ("Powr", "Power (W)", False),
    ("Powr (W)", "Power (W)", False), ("Pwer", "Power (W)", False),
    ("Meda", "Concealment: Media", False), ("Medai", "Concealment: Media", False),
    ("Nots", "Concealment: Notes", False), ("Notse", "Concealment: Notes", False),
    ("Datte", "Date", False),
    ("Lenght", "Flaw Attributes: Flaw Length (in)", True),
    ("Orientaton", "Flaw Attributes: Flaw Orientation (deg)", True)])
def test_a_slip_of_a_field_word_is_offered_but_never_sure(ledger, label, field, per_item):
    """A one-letter slip of a short word ('Powr', 'Meda') stopped matching;
    any slip is a suggestion to review, never pre-accepted: the same letters
    made 'Powder' Power (W) at 0.91."""
    schema, _ = ledger
    mt = se.Matcher(schema).match(label, per_item=per_item)
    assert mt.target is not None and mt.target.name == field, label
    assert mt.score >= se.SUGGEST_AT and not mt.sure, (label, mt.score)


def test_a_powder_row_on_a_build_sheet_is_not_the_power(ledger, tmp_path):
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Build"
    ws["A1"] = "Part Number: 101"
    ws["A3"], ws["B3"] = "Powder", "Ti-6Al-4V"
    ws["A4"], ws["B4"] = "Frequency (kHz)", 50
    run, dec = _decided(schema, wb, tmp_path / "powder.xlsx")
    assert not any(d.obs.field == "Power (W)" for d in dec)
    assert [(o.label, o.value) for o in run.unmatched] == [("Powder", "Ti-6Al-4V")]


def test_an_owner_column_beside_the_settings_is_not_the_power(ledger, tmp_path):
    schema, _ = ledger
    wb = openpyxl.load_workbook(lf.make_laser_log(tmp_path / "laser_log.xlsx"))
    wb["Build"]["A7"], wb["Build"]["B7"] = "Owner", "JB"
    run, dec = _decided(schema, wb, tmp_path / "laser_log_owner.xlsx")
    power = [d for d in dec if d.obs.field == "Power (W)"]
    assert [(d.obs.value, d.conflict) for d in power] == [("285", "")]    # no 'JB' clash
    assert any(o.label == "Owner" for o in run.unmatched)


# ── a password-protected workbook ───────────────────────────────────────
def test_a_password_workbook_is_counted_as_password_protected(ledger, tmp_path):
    import excel_open
    from council_core.ledger_ingest import summarize_open_failures
    schema, _ = ledger
    pw = tmp_path / "pw.xlsx"
    pw.write_bytes(excel_open.OLE_MAGIC + b"\0" * 504 + b"".join(
        n.encode("utf-16-le") for n in ("EncryptedPackage", "StrongEncryptionDataSpace")))
    run = se.extract(schema, [pw], use_images=False)
    assert run.open_failures == [("pw.xlsx", "password")]
    assert "password to open" in run.problems[0]
    assert "1 password-protected" in summarize_open_failures(run.open_failures)


# ── 8. a placeholder is not a part ──────────────────────────────────────
PLACEHOLDERS = ["TBD", "N/A", "n/a", "to be assigned", "pending", "unknown", "-", "?", "none"]


def _build_sheet(wb, title="Build"):
    ws = wb.active
    ws.title = title
    return ws


@pytest.mark.parametrize("typed", PLACEHOLDERS)
def test_a_placeholder_in_the_part_number_is_no_part(ledger, tmp_path, typed):
    """'Part Number: TBD' made the part 'TBD', its values pre-accepted, and
    Apply wrote them to the parent CSV."""
    schema, csv = ledger
    for layout in ("inline", "beside", "column"):
        wb = openpyxl.Workbook()
        ws = _build_sheet(wb)
        if layout == "column":
            ws["A3"], ws["B3"], ws["C3"] = "Part Number", "Frequency (kHz)", "Power (W)"
            ws["A4"], ws["B4"], ws["C4"] = typed, 50, 250
        else:
            if layout == "inline":
                ws["A1"] = f"Part Number: {typed}"
            else:
                ws["A1"], ws["B1"] = "Part Number", typed
            ws["A3"], ws["B3"] = "Frequency (kHz)", 50
        src = tmp_path / f"build_{layout}.xlsx"
        run, dec = _decided(schema, wb, src)
        assert run.obs and {o.part for o in run.obs} == {""}, (typed, layout)
        assert not any(d.accept for d in dec), (typed, layout)
        review = tmp_path / f"review_{layout}.xlsx"
        se.write_review(review, run, dec, schema, str(csv))
        se.apply_review(review, use_images=False)
        assert ParentCSV(csv).sample_ids() == [], (typed, layout)


def test_a_placeholder_part_leaves_the_part_to_the_other_ways(ledger, tmp_path):
    """No part on the sheet: its name, the workbook, the file name are tried."""
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = _build_sheet(wb, "Part 2041")
    ws["A1"] = "Part Number: TBD"
    ws["A3"], ws["B3"] = "Frequency (kHz)", 50
    _, dec = _decided(schema, wb, tmp_path / "sheet.xlsx")
    assert [(d.obs.part, d.obs.part_how) for d in dec] == [("2041", "sheet name 'Part 2041'")]

    wb = openpyxl.Workbook()
    ws = _build_sheet(wb)
    ws["A1"], ws["B1"] = "Part Number", "N/A"
    ws["A3"], ws["B3"] = "Frequency (kHz)", 50
    wb.create_sheet("Cover")["A1"] = "S/N: LMM1AB101"
    _, dec = _decided(schema, wb, tmp_path / "cover.xlsx")
    assert [(d.obs.part, d.obs.part_how) for d in dec] == [
        ("LMM1AB101", "the one part number in the workbook")]

    wb = openpyxl.Workbook()
    ws = _build_sheet(wb)
    ws["A1"] = "Part Number: to be assigned"
    ws["A3"], ws["B3"] = "Frequency (kHz)", 50
    _, dec = _decided(schema, wb, tmp_path / "LMM1AB102_build.xlsx", pattern_mask="@@@#@@###")
    assert [d.obs.part for d in dec] == ["LMM1AB102"]
    assert dec[0].obs.part_how.startswith("file name") and not dec[0].accept


def test_placeholders_are_known():
    for typed in PLACEHOLDERS + ["TBA", "n.a.", "NA", "--", "(TBD)", "Not assigned",
                                 "to be determined", " tbd "]:
        assert se.is_placeholder(typed), typed
    for real in ("LMM1AB101", "AB-2041", "101", "TBD-101", "N/A-7", "Pending 4", "NAB1"):
        assert not se.is_placeholder(real), real
    from council_core import kg_excel
    assert kg_excel.split_people("to be assigned") == [] and kg_excel.split_people("N/A") == []


def test_a_part_not_of_the_users_pattern_is_kept_but_not_pre_accepted(ledger, tmp_path):
    schema, _ = ledger
    for i, (typed, fits) in enumerate([("AB-2041", False), ("LMM1AB101", True)]):
        wb = openpyxl.Workbook()
        ws = _build_sheet(wb)
        ws["A1"] = f"Part Number: {typed}"
        ws["A3"], ws["B3"] = "Frequency (kHz)", 50
        ws["D3"], ws["E3"], ws["F3"] = "Part Number", "Power (W)", "Frequency (kHz)"
        ws["D4"], ws["E4"], ws["F4"] = typed, 250, 50
        _, dec = _decided(schema, wb, tmp_path / f"mask{i}.xlsx", pattern_mask="@@@#@@###")
        assert dec and {d.obs.part for d in dec} == {typed}
        if fits:
            assert all(d.accept for d in dec if not d.conflict)
            assert not any(se.MISFIT_PART_HOW in d.obs.part_how for d in dec)
        else:
            assert not any(d.accept for d in dec), typed
            assert all(d.obs.part_how.endswith("does not fit the part-name pattern")
                       for d in dec), [d.obs.part_how for d in dec]
    wb = openpyxl.Workbook()
    ws = _build_sheet(wb)
    ws["A1"] = "Part Number: AB-2041"
    ws["A3"], ws["B3"] = "Frequency (kHz)", 50
    # the Part you typed is yours, whatever its shape
    _, dec = _decided(schema, wb, tmp_path / "typed.xlsx", pattern_mask="@@@#@@###",
                      part="AB-2041")
    assert [(d.obs.part, d.accept) for d in dec] == [("AB-2041", True)]
    # and the preview says what the extract does
    wb.save(tmp_path / "preview.xlsx")
    rep = se.preview_workbook(tmp_path / "preview.xlsx", schema, pattern_mask="@@@#@@###")
    assert rep.sheets[0].part_id == "AB-2041"
    assert rep.sheets[0].part_how.endswith(se.MISFIT_PART_HOW)


# ── 9. another kind of identifier is not the part ───────────────────────
OTHER_IDS = ["Lot Number", "Job Number", "Heat Number", "Work Order Number", "Drawing Number",
             "Revision", "Part Revision", "Lot No.", "Heat #", "Dwg No", "PO Number"]


@pytest.mark.parametrize("label", OTHER_IDS)
def test_another_identifiers_column_is_not_the_part_column(ledger, tmp_path, label):
    """A 'Lot Number' column was the Part Number column: every row's part
    was its lot, pre-accepted."""
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = _build_sheet(wb)
    ws["A1"] = "Part records"
    ws["A3"], ws["B3"], ws["C3"] = label, "Frequency (kHz)", "Power (W)"
    ws["A4"], ws["B4"], ws["C4"] = "AB-2041", 50, 250
    ws["A5"], ws["B5"], ws["C5"] = "AB-2042", 60, 260
    run, dec = _decided(schema, wb, tmp_path / "table.xlsx")
    assert run.obs and {o.part for o in run.obs} == {""}, label
    assert not any(d.accept for d in dec), label
    # its values are offered to map, not lost
    assert sorted(o.value for o in run.unmatched if o.label == label) == ["AB-2041", "AB-2042"]


@pytest.mark.parametrize("label", OTHER_IDS)
def test_another_identifier_on_a_form_does_not_name_the_part(ledger, tmp_path, label):
    """Under a 'Part Information' heading the 'Lot Number: AB-2041' cell was
    the part; 'Part Revision: B' named the part on its own."""
    schema, _ = ledger
    for heading in (True, False):
        wb = openpyxl.Workbook()
        ws = _build_sheet(wb)
        if heading:
            ws["A1"] = "Part Information"
        ws["A2"] = f"{label}: AB-2041"
        ws["A4"], ws["B4"] = "Frequency (kHz)", 50
        run, dec = _decided(schema, wb, tmp_path / f"form_{heading}.xlsx")
        assert [(o.field, o.part) for o in run.obs] == [("Frequency (kHz)", "")], label
        assert not any(d.accept for d in dec), label
        assert [(o.label, o.value) for o in run.unmatched] == [(label, "AB-2041")]


def test_a_lot_column_beside_the_part_column_is_a_label_not_the_part(ledger, tmp_path):
    """'Lot Number' matches no field now, but it is still a label: the
    'Part Number | Lot Number' header must not read as a Part Number with
    the value 'Lot Number' beside it - for this sheet and the next."""
    schema, _ = ledger
    wb = openpyxl.Workbook()
    ws = _build_sheet(wb)
    rows = [["Part Number", "Lot Number", "Frequency (kHz)", "Power (W)"],
            ["AB-2041", "L-77", 50, 250], ["AB-2042", "L-78", 60, 260]]
    for r, row in enumerate(rows, 3):
        for c, v in enumerate(row, 1):
            ws.cell(r, c, v)
    notes = wb.create_sheet("Notes")
    notes["A1"], notes["B1"] = "Date", "2026-10-05"
    run, dec = _decided(schema, wb, tmp_path / "lots.xlsx")
    assert {(o.field, o.part) for o in run.obs} == {
        ("Frequency (kHz)", "AB-2041"), ("Power (W)", "AB-2041"),
        ("Frequency (kHz)", "AB-2042"), ("Power (W)", "AB-2042"), ("Date", "")}
    assert sorted(o.value for o in run.unmatched if o.label == "Lot Number") == ["L-77", "L-78"]
    assert all(d.accept for d in dec if d.obs.part)


def test_the_parts_own_labels_still_name_it(ledger, tmp_path):
    """Serial Number, S/N, Part No. name the part, with a heading over them
    or in a table; a template whose id IS the lot keeps its own label."""
    schema, _ = ledger
    for i, label in enumerate(["Serial Number", "S/N", "Part No.", "Part #"]):
        wb = openpyxl.Workbook()
        ws = _build_sheet(wb)
        ws["A1"] = "Part Information"
        ws["A2"] = f"{label}: AB-2041"
        ws["A4"], ws["B4"] = "Frequency (kHz)", 50
        _, dec = _decided(schema, wb, tmp_path / f"own{i}.xlsx")
        assert [(d.obs.part, d.accept) for d in dec] == [("AB-2041", True)], label
    m = se.Matcher(schema)
    for label in ("Serial Number", "S/N", "Sample ID", "Specimen ID", "Part Number"):
        assert not m.other_id(label), label
    lot = replace(schema, id_field="Lot Number")
    assert not se.Matcher(lot).other_id("Lot Number")
    assert se.Matcher(lot).other_id("Heat Number")
    pn = m.by_name[schema.id_field]
    ex = se._Extractor(se.Grid("f.xlsx", "S", {}), m, se.ItemCodes(schema.items), "")
    assert not ex._sure_id("Part Revision", se.Match(pn, 0.87, via="match"))
    assert ex._sure_id("Part Revision", se.Match(pn, 1.0, via="alias"))      # you mapped it
