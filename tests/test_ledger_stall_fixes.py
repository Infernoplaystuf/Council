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
  the whole run, and Cancel during the last workbook was not recorded;
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

def test_the_stall_dump_is_written_to_a_file_by_faulthandler(tmp_path):
    out = tmp_path / "logs" / "stalls.txt"
    said = []
    with stall_watchdog.watch("test step", said.append, stall_s=0.5, sample_s=0.5,
                              dump_file=str(out)):
        time.sleep(1.6)                                    # nothing moves
    text = out.read_text(encoding="utf-8")
    assert "test step: watching" in text
    assert "most recent call first" in text                # faulthandler's dump
    assert said and str(out) in said[0]


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
