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

Synthetic data only (tests/ledger_fixtures.py).
"""
from __future__ import annotations

import sys
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
    assert ex._sure_id("Lot", se.Match(pn, 1.0, via="alias"))
    assert ex._sure_id("Prt Nmbr", se.Match(pn, 0.9, via="match"))


# ── 6. S/N names the part ───────────────────────────────────────────────
def test_s_n_is_the_serial_number_label(ledger, tmp_path):
    schema, _ = ledger
    assert se.tokens("S/N") == se.tokens("S/N:") == se.tokens("SN") == \
        se.tokens("Serial No.") == se.tokens("Serial Number") == ["serial", "number"]
    assert "serial" not in se.tokens("Pass/No Go")
    for i, (label, inline) in enumerate([("S/N", True), ("S/N", False), ("SN", False),
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


# ── 7. letters alone do not make a label a field ────────────────────────
def test_a_label_sharing_no_word_with_a_field_is_not_offered(ledger):
    schema, _ = ledger
    m = se.Matcher(schema)
    for label in ("Owner", "Tower", "Lower", "Mower", "Flower", "Parameter", "Partner"):
        for per_item in (False, None, True):
            assert m.match(label, per_item=per_item).target is None, (label, per_item)
    assert m.match("Location", per_item=True).target is None     # not Flaw Orientation
    # real slips and short forms still match
    assert m.match("Frequncy").target.name == "Frequency (kHz)"
    assert m.match("Amplitde").target.name == "Concealment: Amplitude"
    assert m.match("Ampl").target.name == "Concealment: Amplitude"
    assert m.match("Lenght", per_item=True).target.name == \
        "Flaw Attributes: Flaw Length (in)"
    assert m.match("Orientaton", per_item=True).target.name == \
        "Flaw Attributes: Flaw Orientation (deg)"
    assert m.match("Power (W)").target.name == "Power (W)"


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
