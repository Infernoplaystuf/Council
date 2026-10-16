"""
excel_open — workbooks openpyxl cannot open (rights-protected, .xls, .xlsb),
opened through the installed Excel; and text boxes / charts.

Most of this runs anywhere: a stand-in for Excel.Application checks how Excel
is driven (macros off, read-only, closed unsaved) and how its values come
back. The tests marked `real Excel` drive the installed Excel (hidden, no
window) and are skipped where there is none.

Run:  python -m pytest tests/test_excel_open.py -q
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import excel_open  # noqa: E402
from excel_open import (OLE_MAGIC, ExcelOpenError, _chart_from_xml, chart_cells,  # noqa: E402
                        kind_of, read_with_excel, text_cells)


# ── telling files apart ────────────────────────────────────────────────
def _ole(path, *names):
    path.write_bytes(OLE_MAGIC + b"\0" * 504 + b"".join(n.encode("utf-16-le") + b"\0\0"
                                                        for n in names))
    return path


def test_kinds_of_file(tmp_path):
    wb = openpyxl.Workbook()
    wb.save(tmp_path / "a.xlsx")
    assert kind_of(tmp_path / "a.xlsx") == "zip"
    assert kind_of(_ole(tmp_path / "old.xls", "Workbook")) == "legacy"
    assert kind_of(_ole(tmp_path / "irm.xlsx", "EncryptedPackage",
                        "DRMEncryptedDataSpace")) == "rights"
    assert kind_of(_ole(tmp_path / "pw.xlsx", "EncryptedPackage", "EncryptionInfo",
                        "StrongEncryptionDataSpace")) == "password"
    (tmp_path / "x.xlsx").write_bytes(b"not a workbook")
    assert kind_of(tmp_path / "x.xlsx") == "unknown"


def test_a_password_file_is_refused_before_excel_is_asked(tmp_path):
    pw = _ole(tmp_path / "pw.xlsx", "EncryptedPackage", "StrongEncryptionDataSpace")
    asked = []
    with pytest.raises(ExcelOpenError, match="password to open"):
        read_with_excel(pw, app_factory=lambda: asked.append(1))
    assert asked == []


# ── text boxes and charts as tables ─────────────────────────────────────
def test_text_box_lines_become_label_value_cells():
    assert text_cells("Scan Speed (mm/s): 1800\r\nSpot Size (mm): 0.05\n") == {
        (1, 1): "Scan Speed (mm/s): 1800", (2, 1): "Spot Size (mm): 0.05"}


def test_a_chart_of_inspectors_becomes_one_row_per_reading():
    cells = chart_cells("EC Mid-Conceal", "", ["1A", "2A"], [("JB", [30, 31]), ("MK", [32, None])])
    rows = sorted((r, c, v) for (r, c), v in cells.items())
    assert cells[(3, 1)] == "Flaw" and cells[(3, 2)] == "Inspector" and cells[(3, 3)] == "EC Mid-Conceal"
    assert [(cells[(r, 1)], cells[(r, 2)], cells[(r, 3)]) for r in (4, 5, 6)] == [
        ("1A", "JB", 30), ("2A", "JB", 31), ("1A", "MK", 32)]
    assert (7, 1) not in cells and rows                  # a blank reading is not a row


def test_a_chart_of_measurements_is_a_wide_table():
    cells = chart_cells("", "Flaw", ["1A", "2A"], [("EC Pre", [70, 71]), ("EC Post", [20, 21])])
    assert [cells[(1, c)] for c in (1, 2, 3)] == ["Flaw", "EC Pre", "EC Post"]
    assert [cells[(3, c)] for c in (1, 2, 3)] == ["2A", 71, 21]


CHART_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
 <c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>EC Pre-Conceal</a:t></a:r></a:p></c:rich></c:tx></c:title>
  <c:plotArea><c:barChart>
   <c:ser><c:tx><c:v>JB</c:v></c:tx>
    <c:cat><c:strLit><c:ptCount val="2"/><c:pt idx="0"><c:v>1A</c:v></c:pt><c:pt idx="1"><c:v>2A</c:v></c:pt></c:strLit></c:cat>
    <c:val><c:numLit><c:ptCount val="2"/><c:pt idx="0"><c:v>70</c:v></c:pt><c:pt idx="1"><c:v>71.5</c:v></c:pt></c:numLit></c:val></c:ser>
   <c:ser><c:tx><c:strRef><c:f>S!B1</c:f><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>MK</c:v></c:pt></c:strCache></c:strRef></c:tx>
    <c:val><c:numRef><c:f>S!B2:B3</c:f><c:numCache><c:ptCount val="2"/><c:pt idx="1"><c:v>73</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser>
  </c:barChart></c:plotArea></c:chart></c:chartSpace>"""


def test_chart_values_come_from_the_saved_cache_or_literals():
    cells = _chart_from_xml(CHART_XML)
    got = {(cells[(r, 1)], cells[(r, 2)]): cells[(r, 3)] for r in range(4, 8) if (r, 1) in cells}
    assert got == {("1A", "JB"): 70, ("2A", "JB"): 71.5, ("2A", "MK"): 73}


# ── how Excel is driven: a stand-in for Excel.Application ───────────────
class _Range:
    def __init__(self, row=1, col=1, value=None, merged=None):
        self.Row, self.Column, self.Value = row, col, value
        self._merged = merged
        self.MergeCells = merged is not None

    @property
    def MergeArea(self):
        r, c, nr, nc = self._merged
        area = _Range(r, c)
        area.Rows = type("n", (), {"Count": nr})()
        area.Columns = type("n", (), {"Count": nc})()
        return area


class _Shape:
    def __init__(self, typ, text="", chart=None):
        self.Type, self.HasChart = typ, chart is not None
        self.Chart = chart
        self.TopLeftCell = type("c", (), {"Address": lambda self, a, b: "E2"})()
        tr = type("tr", (), {"Text": text})()
        self.TextFrame2 = type("tf", (), {"HasText": bool(text), "TextRange": tr})()


class _Series:
    def __init__(self, name, x, y):
        self.Name, self.XValues, self.Values = name, x, y


class _Coll:
    def __init__(self, items):
        self._items = items
        self.Count = len(items)

    def Item(self, i):
        return self._items[i - 1]


class _Chart:
    HasTitle = True
    ChartTitle = type("t", (), {"Text": "EC Pre-Conceal"})()

    def SeriesCollection(self):
        return _Coll([_Series("JB", ("1A", "2A"), (70.0, 71.0))])

    def Axes(self, i):
        return type("ax", (), {"HasTitle": False})()


class _Sheet:
    Name = "EC"

    def __init__(self):
        when = dt.datetime(2026, 9, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.UsedRange = _Range(1, 1, (("Flaw report", None, None),
                                       ("Part Number", 2041.0, when),
                                       ("Power (W)", -2146826281, 285.5)))
        self.Shapes = _Coll([_Shape(17, "Spot Size (mm): 0.05"), _Shape(3, chart=_Chart())])

    def Cells(self, r, c):
        return _Range(r, c, merged=(1, 1, 1, 3) if (r, c) == (1, 1) else None)


class _App:
    def __init__(self, log, fail=None):
        self.log, self.fail = log, fail
        app = self

        class Books:
            def Open(self, path, **kw):
                app.log.append(("open", Path(path).name, kw))
                if app.fail:
                    raise Exception(app.fail)
                return app._book
        self.Workbooks = Books()

        class Book:
            Worksheets = [_Sheet()]

            def Close(self, SaveChanges=True):
                app.log.append(("close", SaveChanges))
        self._book = Book()

    def __setattr__(self, k, v):
        if k[0].isupper() and k != "Workbooks":
            self.log.append(("set", k, v))
        object.__setattr__(self, k, v)

    def Quit(self):
        self.log.append(("quit",))


def test_excel_is_driven_read_only_with_macros_off_and_closed_unsaved(tmp_path):
    f = tmp_path / "book.xlsb"
    f.write_bytes(b"PK\x03\x04")
    log = []
    book = read_with_excel(f, want_pictures=False, app_factory=lambda: _App(log))
    sets = {e[1]: e[2] for e in log if e[0] == "set"}
    assert sets["AutomationSecurity"] == 3 and sets["Visible"] is False
    assert sets["EnableEvents"] is False and sets["DisplayAlerts"] is False
    opened = next(e for e in log if e[0] == "open")[2]
    assert opened["ReadOnly"] is True and opened["UpdateLinks"] == 0 and opened["AddToMru"] is False
    assert ("close", False) in log and log[-1] == ("quit",)
    (name, cells), = book.sheets
    assert cells[(2, 2)] == 2041 and cells[(3, 3)] == 285.5          # floats as Excel shows them
    assert cells[(2, 3)] == dt.datetime(2026, 9, 12)                 # COM dates, naive
    assert (3, 2) not in cells                                       # #DIV/0! is no value
    assert cells[(1, 2)] == ("__merged__", "Flaw report")            # a merged title
    assert [e.name for e in book.extras] == ["EC › text box 1", "EC › chart 2"]
    assert book.extras[0].cells == {(1, 1): "Spot Size (mm): 0.05"}


def test_an_excel_failure_says_what_happened(tmp_path):
    f = tmp_path / "x.xls"
    f.write_bytes(OLE_MAGIC)
    log = []
    with pytest.raises(ExcelOpenError, match="Excel could not open x.xls: corrupt"):
        read_with_excel(f, app_factory=lambda: _App(log, fail="corrupt file"))
    assert log[-1] == ("quit",)                                      # Excel still closed


def test_folders_bring_every_workbook_kind(tmp_path):
    from council_core.ledger_ingest import collect_sources
    for n in ("a.xlsx", "b.xlsm", "c.xls", "d.xlsb", "~$a.xlsx", "notes.txt"):
        (tmp_path / n).write_bytes(b"")
    assert sorted(p.name for p in collect_sources([tmp_path])) == [
        "a.xlsx", "b.xlsm", "c.xls", "d.xlsb"]


# ── the installed Excel itself ──────────────────────────────────────────
real_excel = pytest.mark.skipif(not excel_open.excel_available(),
                                reason="no Excel automation here (Windows + Excel + pywin32)")


def _excel_count() -> int:
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq EXCEL.EXE"], capture_output=True,
                         text=True).stdout
    return out.count("EXCEL.EXE")


@real_excel
def test_real_excel_xls_xlsb_text_box_chart_and_password(tmp_path):
    from ledger_bench import _excel_save
    from source_extract import LedgerError, load_workbook_grids
    before = _excel_count()
    wb = openpyxl.Workbook()
    wb.active["A1"], wb.active["B1"] = "Power (W)", 285
    wb.save(tmp_path / "s.xlsx")

    def box_and_chart(sheet):
        sheet.Shapes.AddTextbox(1, 20, 20, 200, 40).TextFrame2.TextRange.Text = "Spot Size (mm): 0.05"
        ch = sheet.ChartObjects().Add(240, 20, 300, 200).Chart
        ser = ch.SeriesCollection().NewSeries()
        ser.Name, ser.Values, ser.XValues = "EC Pre", (70, 71), ("1A", "2A")
    _excel_save(tmp_path / "s.xlsx", tmp_path / "old.xls", 56, edit=box_and_chart)
    grids, _ = load_workbook_grids(tmp_path / "old.xls")
    names = {g.sheet: g.cells for g in grids}
    assert names["Sheet"][(1, 2)] == 285
    assert any("text box" in n and c == {(1, 1): "Spot Size (mm): 0.05"} for n, c in names.items())
    chart = next(c for n, c in names.items() if "chart" in n)
    # Excel titles a one-series chart after its series; the table sits under it
    assert chart[(1, 1)] == "EC Pre" and chart[(3, 1)] == "Flaw" and chart[(3, 2)] == "EC Pre"
    assert (chart[(5, 1)], chart[(5, 2)]) == ("2A", 71)

    wb.save(tmp_path / "s.xlsx")
    _excel_save(tmp_path / "s.xlsx", tmp_path / "pw.xlsx", 51, password="x")
    with pytest.raises(LedgerError, match="password to open"):
        load_workbook_grids(tmp_path / "pw.xlsx")
    import time                                         # Excel takes a moment to exit
    for _ in range(40):
        if _excel_count() <= before:
            break
        time.sleep(0.5)
    assert _excel_count() <= before                     # every hidden Excel was closed
