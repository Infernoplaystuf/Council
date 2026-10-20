"""The Ledger extract no longer freezes or crawls (review, 2026-10-19).

What the stall hunt measured, and what each test pins:
- one 2,000-row sheet took 276 s (every 'what is on row r' scanned the
  whole sheet); 1,000 rows now take a few seconds;
- one formatted cell at row 1,048,576 made openpyxl build 21 million
  cells (4 GB, multi-second GC pauses holding the GIL);
- a space-padded cell made the initials-heading pattern backtrack for
  54 s with the GIL held;
- the review writer and 'Other values offered' were quadratic;
- Cancel did nothing inside a workbook, a Cancel that killed Excel lost
  the whole run, and Cancel during the last workbook was not recorded -
  and then a Cancel pressed after the last workbook was read made a whole
  review a cancelled, 'partial' one;
- Apply re-read the sources without the part-name mask;
- the stall dump could not be written while the app was frozen.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).parent))
import ledger_fixtures as lf  # noqa: E402
import source_extract as se  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from council_core import stall_watchdog  # noqa: E402
from sample_ledger import ParentCSV  # noqa: E402


def _log_book(path: Path, rows: int, cols: int = 20) -> Path:
    """A long inspection log: header + rows of mixed text and numbers."""
    head = ["Part Number", "Flaw ID", "Inspector", "Date", "Stage", "Response",
            "Notes", "Laser Power (W)", "Frequency (kHz)", "Scan Speed (mm/s)",
            "Comment", "Operator", "Batch", "Station", "Shift", "Lot",
            "Length (mm)", "Orientation (deg)", "Passes", "Remarks"][:cols]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Inspection Log"
    ws.append(head)
    words = ["ok", "see note", "rework", "pass", "visual check", "porosity"]
    for r in range(rows):
        row = []
        for c, h in enumerate(head):
            if h == "Part Number":
                row.append(f"PN-{1000 + r // 50}")
            elif h == "Flaw ID":
                row.append(lf.ITEMS[r % len(lf.ITEMS)])
            elif h in ("Inspector", "Operator"):
                row.append(["JB", "MK", "TR"][r % 3])
            elif h == "Date":
                row.append(f"2026-09-{1 + r % 28:02d}")
            elif h == "Stage":
                row.append(["EC Pre", "EC Post", "Pre-Conceal"][r % 3])
            elif h in ("Notes", "Comment", "Remarks", "Station", "Shift"):
                row.append(words[(r + c) % len(words)])
            else:
                row.append(round(10 + (r * 7 + c) % 90 + 0.5, 2))
        ws.append(row)
    wb.save(path)
    return path


@pytest.fixture()
def ledger(tmp_path):
    tpl = lf.make_template(tmp_path / "template.xlsx")
    ps = li.create_parent(tpl)
    return Path(ps.csv), ParentCSV(ps.csv).schema


# ---------------------------------------------- speed

def test_a_1000_row_sheet_is_read_in_seconds_not_minutes(tmp_path, ledger):
    csv, schema = ledger
    src = _log_book(tmp_path / "log.xlsx", 1000)
    t0 = time.perf_counter()
    run = se.extract(schema, [src], use_images=False)
    took = time.perf_counter() - t0
    assert run.obs, "the log gave values"
    # was ~80 s on this shape; generous for a slow CI box
    assert took < 30, f"took {took:.1f}s"


def test_one_far_away_formatted_cell_does_not_fill_the_sheet(tmp_path):
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in range(1, 6):
        ws.cell(r, 1, f"Label {r}")
        ws.cell(r, 2, r * 10)
    ws.cell(1048576, 20).font = Font(bold=True)           # styled, no value
    wb.save(tmp_path / "stray.xlsx")
    t0 = time.perf_counter()
    grids, _images = se.load_workbook_grids(tmp_path / "stray.xlsx")
    assert time.perf_counter() - t0 < 5
    assert len(grids[0].cells) == 10


def test_the_initials_heading_pattern_is_linear_on_padded_cells():
    cell = "NOTE:" + " " * 4000 + "x"
    t0 = time.perf_counter()
    assert se._WHO_HEADING.match(cell.strip()) is None
    assert time.perf_counter() - t0 < 0.5                  # was 54 s
    for ok in ("JB", "JB — 2026-09-18", "MK - 09/18", "TR (2026-09-18)"):
        assert se._WHO_HEADING.match(ok), ok
    for no in ("Notes", "JB was here", "1A"):
        assert not se._WHO_HEADING.match(no), no


def test_other_values_offered_is_capped(ledger):
    _csv, schema = ledger
    f = next(x for x in schema.fields if not x.per_item and x.name != schema.id_field)
    obs = [se.Obs("101", "", f.name, str(v), 0.9, "match", "x", f"s.xlsx!B{v}")
           for v in range(1, 101)]
    decs = se.decide(se.Extraction(["s.xlsx"], obs=obs), schema)
    text = decs[0].conflict
    assert text.endswith(", and 79 more")
    assert text.count(",") == se.OTHERS_SHOWN


def test_write_review_with_many_rows_is_fast(tmp_path, ledger):
    csv, schema = ledger
    f = next(x for x in schema.fields if not x.per_item and x.name != schema.id_field)
    obs = [se.Obs(str(1000 + i), "", f.name, str(i), 0.5, "ambiguous", "x",
                  f"s.xlsx!B{i}") for i in range(6000)]
    run = se.Extraction(["s.xlsx"], obs=obs)
    decs = se.decide(run, schema)
    t0 = time.perf_counter()
    se.write_review(tmp_path / "r.xlsx", run, decs, schema, str(csv))
    assert time.perf_counter() - t0 < 20                   # was ~10 s at 6k, 75 s at 16k
    ws = openpyxl.load_workbook(tmp_path / "r.xlsx")["Values"]
    assert ws.cell(2, 1).fill.fgColor.rgb.endswith("FFF2CC")   # amber row kept


# ---------------------------------------------- cancel

class _Prog:
    """A progress stand-in that presses Cancel once a sheet is being read."""
    def __init__(self, cancel, when):
        self.cancel, self.when = cancel, when

    def stage(self, *a, **k):
        pass

    def update(self, what="", **k):
        if self.when in str(what):
            self.cancel.set()

    def advance(self, *a, **k):
        pass


def test_cancel_inside_a_big_workbook_stops_it_and_keeps_the_earlier_one(tmp_path, ledger):
    _csv, schema = ledger
    small = lf.make_laser_log(tmp_path / "a_laser.xlsx")
    big = _log_book(tmp_path / "b_big.xlsx", 1500)
    cancel = threading.Event()
    run = se.extract(schema, [small, big], use_images=False, cancel=cancel,
                     progress=_Prog(cancel, "b_big.xlsx: finding values"))
    assert run.cancelled
    assert run.obs and all("a_laser" in o.where for o in run.obs)
    assert any("while reading 'b_big.xlsx'" in p for p in run.problems)


def test_cancel_that_kills_excel_mid_read_ends_cleanly(tmp_path, ledger, monkeypatch):
    _csv, schema = ledger
    a = lf.make_laser_log(tmp_path / "a.xlsx")
    b = tmp_path / "b.xls"
    b.write_bytes(b"x")
    cancel = threading.Event()
    real = se.load_workbook_grids

    def fake(path, *args, **kw):
        if Path(path).name == "b.xls":
            cancel.set()                                   # Cancel killed Excel...
            raise RuntimeError("com_error(-2147023174, 'The RPC server is unavailable')")
        return real(path, *args, **kw)
    monkeypatch.setattr(se, "load_workbook_grids", fake)
    run = se.extract(schema, [a, b], use_images=False, cancel=cancel)
    assert run.cancelled and run.obs                       # a's values kept
    assert not run.open_failures                           # b is not blamed


def test_a_workbook_that_fails_oddly_does_not_end_the_run(tmp_path, ledger, monkeypatch):
    _csv, schema = ledger
    a = tmp_path / "a.xlsx"
    a.write_bytes(b"x")
    b = lf.make_laser_log(tmp_path / "b.xlsx")
    real = se.load_workbook_grids

    def fake(path, *args, **kw):
        if Path(path).name == "a.xlsx":
            raise RuntimeError("something unexpected")
        return real(path, *args, **kw)
    monkeypatch.setattr(se, "load_workbook_grids", fake)
    run = se.extract(schema, [a, b], use_images=False)
    assert not run.cancelled
    assert run.open_failures == [("a.xlsx", "other")]
    assert run.obs and any("something unexpected" in p for p in run.problems)


def test_cancel_during_the_last_workbook_is_recorded(tmp_path, ledger):
    _csv, schema = ledger
    big = _log_book(tmp_path / "only.xlsx", 1500)
    cancel = threading.Event()
    run = se.extract(schema, [big], use_images=False, cancel=cancel,
                     progress=_Prog(cancel, "finding values"))
    assert run.cancelled and not run.obs


class _CancelWhenRead(_Prog):
    """Presses Cancel once a workbook has been read to the end."""
    def update(self, what="", **k):
        pass

    def advance(self, *a, what="", **k):
        if self.when in str(what):
            self.cancel.set()


def _partless(path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Build"
    ws["A1"], ws["B1"] = "Frequency (kHz)", 55
    ws["A2"], ws["B2"] = "Zorp rate", 7
    wb.save(path)
    return path


def test_a_cancel_after_the_last_workbook_was_read_is_not_a_cancelled_run(tmp_path, ledger):
    """Pressed after the last workbook was read, it marked a whole review
    'Cancelled... partial review' (review, 2026-10-19)."""
    csv, schema = ledger
    a = lf.make_laser_log(tmp_path / "a_laser.xlsx")
    b = _partless(tmp_path / "b_nopart.xlsx")
    cancel = threading.Event()
    run = se.extract(schema, [a, b], use_images=False, cancel=cancel,
                     progress=_CancelWhenRead(cancel, "b_nopart.xlsx read in"))
    assert cancel.is_set() and not run.cancelled
    assert not any("ancel" in p for p in run.problems)
    assert {o.where.split(" › ")[0] for o in run.obs} == {"a_laser.xlsx", "b_nopart.xlsx"}

    # the Ledger tab's call, Council's model ticked: a whole review, and the
    # model is not asked about the labels after the Cancel
    asked = []

    def chat(prompt, _schema):
        asked.append(prompt)
        return json.dumps({"part": "", "evidence": ""} if "Which part" in prompt
                          else {"mappings": []}), "fake-granite"
    cancel = threading.Event()
    s = li._extract(str(csv), [str(a), str(b)], tmp_path / "review.xlsx", "", False, None,
                    _CancelWhenRead(cancel, "b_nopart.xlsx read in"), None, cancel, None,
                    True, chat)
    assert cancel.is_set() and not s.cancelled and s.values
    assert asked and all("Which part" in p for p in asked)
    assert any("Cancelled before Council's model was asked about the labels" in p
               for p in s.problems)


def test_a_cancel_that_cut_the_last_workbook_short_is_still_a_cancelled_run(
        tmp_path, ledger, monkeypatch):
    """Cut short where the read swallowed it - Excel killed while it copied
    a picture out, the model stopped mid-answer: that workbook is not in
    the review, and the run is cancelled."""
    _csv, schema = ledger
    a = lf.make_laser_log(tmp_path / "a_laser.xlsx")
    b = _partless(tmp_path / "b_nopart.xlsx")
    cancel = threading.Event()

    def stopped_model(name, grids):
        cancel.set()                         # Cancel pressed: the model stops
        return "", ""
    run = se.extract(schema, [a, b], use_images=False, cancel=cancel,
                     part_finder=stopped_model)
    assert run.cancelled
    assert run.obs and all("a_laser" in o.where for o in run.obs)
    assert any("while reading 'b_nopart.xlsx'" in p for p in run.problems)

    real = se.load_workbook_grids

    def killed_while_copying(path, *args, **kw):
        out = real(path, *args, **kw)
        if Path(path).name == "b_nopart.xlsx":
            cancel.set()                     # what Excel gave back is cut short
        return out
    monkeypatch.setattr(se, "load_workbook_grids", killed_while_copying)
    cancel = threading.Event()
    run = se.extract(schema, [a, b], use_images=False, cancel=cancel)
    assert run.cancelled
    assert run.obs and all("a_laser" in o.where for o in run.obs)


# ---------------------------------------------- Apply keeps the mask

def test_apply_reads_the_sources_with_the_extracts_mask_and_part(tmp_path, ledger, monkeypatch):
    csv, schema = ledger
    src = lf.make_laser_log(tmp_path / "insp_ABC1DE234_run3.xlsx")
    s = li.run_extract(str(csv), [str(src)], use_images=False, pattern_mask="@@@#@@###",
                       part="")
    meta = json.loads(openpyxl.load_workbook(s.review)["_run"]["A1"].value)
    assert meta["pattern_mask"] == "@@@#@@###"
    seen = {}
    real = se.extract

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)
    monkeypatch.setattr(se, "extract", spy)
    li.run_apply(s.review, use_images=False)
    assert seen.get("pattern_mask") == "@@@#@@###"


# ---------------------------------------------- the stall dump

def test_a_frozen_app_gets_a_faulthandler_dump_in_the_file(tmp_path):
    """A C call that holds the GIL freezes the whole app and the Python
    watchdog with it; faulthandler's own thread still writes the stacks."""
    import re as _re
    out = tmp_path / "logs" / "stalls.txt"
    with stall_watchdog.watch("frozen step", lambda _t: None, stall_s=1.0, sample_s=0.5,
                              dump_file=str(out)):
        time.sleep(0.6)
        _re.match(r"(a+)+$", "a" * 28 + "b")          # ~5 s holding the GIL
    text = out.read_text(encoding="utf-8")
    assert "frozen step: watching" in text
    assert "most recent call first" in text                # faulthandler's dump
    assert "test_a_frozen_app_gets_a_faulthandler_dump" in text


def test_a_slow_but_running_app_is_reported_without_faulthandler(tmp_path):
    """Nothing moves but the app runs: the Python watchdog reports it, and
    faulthandler - which reads other threads unlocked, and crashed the
    process once while a thread exited - stays quiet."""
    out = tmp_path / "logs" / "stalls.txt"
    said = []
    with stall_watchdog.watch("slow step", said.append, stall_s=1.0, sample_s=0.5,
                              dump_file=str(out)):
        time.sleep(2.5)                                    # GIL free: not frozen
    assert said and str(out) in said[0]
    assert "most recent call first" not in out.read_text(encoding="utf-8")


def test_cancel_is_only_offered_for_the_extract(tmp_path):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from council_qt.tabs.ledger import LedgerActions, LedgerTab
    QApplication.instance() or QApplication([])
    gate = threading.Event()
    seen = {}

    class Acts(LedgerActions):
        def apply(self, review, fill_dir, use_images, progress):
            seen["cancel_enabled"] = tab.cancel_btn.isEnabled()
            gate.wait(5)
            raise se.LedgerError("stop")

    tab = LedgerTab(actions=Acts())
    try:
        tab.review.setText(str(tmp_path / "r.xlsx"))
        tab.on_apply()
        time.sleep(0.2)
        QApplication.processEvents()
        assert tab.busy and not tab.cancel_btn.isEnabled()
        gate.set()
        assert tab.wait(10)
    finally:
        gate.set()
        tab.deleteLater()


# ---------------------------------------------- Excel pictures off the GIL

class _Shape:
    def __init__(self):
        self.copied = 0

    def CopyPicture(self, **kw):
        self.copied += 1


def _dib(color=(200, 30, 90)) -> bytes:
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (9, 5), color).save(b, "BMP")
    return b.getvalue()[14:]                               # CF_DIB: no file header


def test_a_picture_is_read_through_pywin32_not_imagegrab(monkeypatch):
    import io
    import excel_open
    from PIL import Image, ImageGrab
    monkeypatch.setattr(excel_open, "_clipboard_dib", lambda timeout=5.0: _dib())
    monkeypatch.setattr(ImageGrab, "grabclipboard",
                        lambda: pytest.fail("ImageGrab holds the GIL; not used"))
    shape = _Shape()
    png = excel_open._copy_picture(shape)
    assert shape.copied == 1
    img = Image.open(io.BytesIO(png))
    assert img.size == (9, 5) and img.convert("RGB").getpixel((0, 0)) == (200, 30, 90)


def test_imagegrab_is_still_the_fallback(monkeypatch):
    import excel_open
    from PIL import Image, ImageGrab

    def broken(timeout=5.0):
        raise OSError("no pywin32 clipboard")
    monkeypatch.setattr(excel_open, "_clipboard_dib", broken)
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: Image.new("RGB", (4, 4)))
    assert excel_open._copy_picture(_Shape())


# ---------------------------------------------- what a folder scan reads

def _accept_all(review: str) -> None:
    wb = openpyxl.load_workbook(review)
    v = wb["Values"]
    for r in range(2, v.max_row + 1):
        v.cell(r, 1, "Y")
    wb.save(review)


def test_same_named_workbooks_in_sibling_folders_are_both_applied(tmp_path, ledger):
    """A folder per sample, each with its own laser_log.xlsx: Apply matched
    the reviewer's choices by file name, so one sample was lost."""
    csv, _schema = ledger
    root = tmp_path / "samples"
    for part, power in (("201", 280), ("202", 300)):
        d = root / part
        d.mkdir(parents=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = f"Part {part}"
        ws["A1"], ws["B1"] = "Laser Power (W)", power
        wb.save(d / "laser_log.xlsx")
    s = li.run_extract(str(csv), [str(root)], use_images=False)
    wheres = {r[8] for r in openpyxl.load_workbook(s.review)["Values"].iter_rows(
        min_row=2, values_only=True)}
    assert any(w.startswith("201/laser_log.xlsx") for w in wheres)
    assert any(w.startswith("202/laser_log.xlsx") for w in wheres)
    _accept_all(s.review)
    li.run_apply(s.review, use_images=False)
    parts = {r[ParentCSV(csv).schema.id_field] for r in ParentCSV(csv).rows}
    assert {"201", "202"} <= parts


def test_a_folder_scan_leaves_the_ledgers_own_outputs_out(tmp_path, ledger):
    csv, schema = ledger
    root = tmp_path / "inspections"
    root.mkdir()
    lf.make_laser_log(root / "laser_101.xlsx")
    s = li.run_extract(str(csv), [str(root)], use_images=False)
    review = Path(s.review)
    (root / "old_review.xlsx").write_bytes(review.read_bytes())   # a review kept here
    from sample_ledger import fill_template
    fill_template(schema, [{schema.id_field: "101"}] if not schema.item_field else
                  [{schema.id_field: "101", schema.item_field: schema.items[0]}],
                  root / "filled_101.xlsx")
    kept, skipped = li.collect_sources([str(root)], template_path=schema.template_path)
    assert [p.name for p in kept] == ["laser_101.xlsx"]
    assert {p.name for p in skipped} == {"old_review.xlsx", "filled_101.xlsx"}
    # picked by name, a file is read as asked
    kept2, _ = li.collect_sources([str(root / "filled_101.xlsx")],
                                  template_path=schema.template_path)
    assert [p.name for p in kept2] == ["filled_101.xlsx"]


def test_a_folder_scan_does_not_follow_a_junction(tmp_path):
    _winapi = pytest.importorskip("_winapi")
    if not hasattr(_winapi, "CreateJunction"):
        pytest.skip("no junctions here")
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    lf.make_laser_log(root / "real" / "a.xlsx")
    _winapi.CreateJunction(str(root / "real"), str(root / "loop"))
    assert [p.name for p in se.workbook_files(root)] == ["a.xlsx"]


# ---------------------------------------------- huge padded cells (end-to-end test, 2026-10-19)

@pytest.mark.parametrize("cell", ["NOTE:" + " " * 32000 + "x", "MK" + " " * 32000,
                                  "  EC  Pre " + " " * 20000 + "(%FSH)"])
def test_normalizing_a_cell_padded_to_excels_limit_is_instant(cell):
    import sample_ledger as sl
    t0 = time.perf_counter()
    sl.normalize_label(cell)
    sl._split_unit(cell)
    assert time.perf_counter() - t0 < 0.2                  # was 2.5-3.8 s holding the GIL


@pytest.mark.parametrize("label,want", [("Flaw Length (in)", "flaw length"),
                                        ("  EC   Pre  (%FSH) ", "ec pre"),
                                        ("Power (W)", "power"), ("Notes:", "notes")])
def test_normalize_label_gives_the_same_names(label, want):
    import sample_ledger as sl
    assert sl.normalize_label(label) == want


def test_a_sheet_of_padded_cells_extracts_in_seconds(tmp_path, ledger):
    _csv, schema = ledger
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in range(1, 41):
        ws.cell(r, 1, "NOTE:" + " " * 8000 + f"x{r}")
        ws.cell(r, 2, "MK" + " " * 8000)
    ws["D1"], ws["E1"] = "Power (W)", 280
    wb.save(tmp_path / "padded.xlsx")
    t0 = time.perf_counter()
    run = se.extract(schema, [tmp_path / "padded.xlsx"], use_images=False)
    assert time.perf_counter() - t0 < 10                  # was minutes, GUI frozen
    assert any(o.field.startswith("Power") for o in run.obs)
