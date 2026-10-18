"""
excel_open.py — workbooks openpyxl cannot open, opened through the Excel
installed on this PC; and the text boxes and charts openpyxl drops.

Why: real source workbooks may be rights-protected (IRM / sensitivity labels),
old .xls, or binary .xlsb. openpyxl reads none of those. The installed Excel
opens what the signed-in user is allowed to open, so the reader asks it for
the cell values, text boxes, charts and pictures - nothing more.

HOW EXCEL IS DRIVEN (everything stays on this machine)
  * its own hidden instance (DispatchEx), never the user's open Excel;
  * macros, events and link updates OFF before the workbook opens
    (AutomationSecurity = force-disable: a workbook's Auto_Open / Workbook_Open
    would otherwise run, with the user's rights);
  * read-only, not added to recent files, closed without saving; no copy of
    the workbook - decrypted or not - is ever written;
  * a password-protected workbook is refused with a message, never prompted for.
  * Pictures come out through the clipboard (Excel offers no other way, and
    rights protection may forbid even that - then the picture is reported, not
    read). Text on the clipboard beforehand is put back afterwards.

TEXT BOXES AND CHARTS
  For .xlsx / .xlsm they are read straight from the file's XML (no Excel
  needed); through Excel for the rest. Each becomes a small table the reader
  already understands: a text box's lines ("Laser Power (W): 300"), a chart's
  categories against its series. A chart whose series are named by initials
  ("JB", "MK") becomes one row per reading with an Inspector column, under the
  chart's title as the measurement.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

Cells = Dict[Tuple[int, int], object]

OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
#: Excel's numeric error codes as COM returns them (#DIV/0!, #N/A, ...)
_COM_ERRORS = range(-2146826288, -2146826244)
MSO_PICTURE, MSO_CHART, MSO_TEXTBOX, MSO_LINKED_PICTURE = 13, 3, 17, 11


class ExcelOpenError(Exception):
    """A plain-language reason a workbook could not be opened."""


@dataclass
class Extra:
    """A text box or chart turned into cells, shown as its own 'sheet'."""
    sheet: str
    name: str                      # "Sheet1 › Chart 2"
    cells: Cells


@dataclass
class Picture:
    sheet: str
    anchor: str
    data: bytes
    ext: str = "png"


@dataclass
class ExcelBook:
    sheets: List[Tuple[str, Cells]] = field(default_factory=list)
    extras: List[Extra] = field(default_factory=list)
    pictures: List[Picture] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# ── what kind of file is it ─────────────────────────────────────────────
def kind_of(path) -> str:
    """'zip' (xlsx/xlsm); 'rights' (rights-protected: IRM / an encrypting
    sensitivity label - Excel opens it for a user allowed to); 'password'
    (a password to open: refused - Excel would sit on a hidden prompt, measured);
    'legacy' (.xls / other OLE workbook); 'unknown'. Told apart by the stream
    names inside the OLE container (MS-OFFCRYPTO): IRM uses the
    DRMEncrypted data space, a password the StrongEncryption one."""
    p = Path(path)
    with open(p, "rb") as fh:
        head = fh.read(8)
        if head == OLE_MAGIC:
            body = head + fh.read(8 << 20)
            if "EncryptedPackage".encode("utf-16-le") in body:
                return "rights" if "DRMEncrypted".encode("utf-16-le") in body else "password"
            return "legacy"
    if zipfile.is_zipfile(p):
        return "zip"
    return "unknown"


def excel_available() -> bool:
    """Windows, pywin32, and an Excel that registers for automation."""
    if os.name != "nt":
        return False
    try:
        import win32com.client  # noqa: F401
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Excel.Application\CurVer"))
        return True
    except Exception:
        return False


# ── tables made of text boxes and charts ────────────────────────────────
def text_cells(text: str) -> Cells:
    lines = [ln.strip() for ln in re.split(r"[\r\n\v]+", text or "") if ln.strip()]
    return {(i, 1): ln for i, ln in enumerate(lines, start=1)}


def _is_person(name: str) -> bool:
    s = re.sub(r"\.", "", str(name or "")).strip()
    return bool(re.fullmatch(r"[A-Z]{2,4}", s)) or bool(
        re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", s))


def chart_cells(title: str, cat_title: str, categories: Sequence,
                series: Sequence[Tuple[str, Sequence]]) -> Cells:
    """Categories down the side, one column per series - or, when every series
    is named by someone's initials, one row per reading with an Inspector
    column and the chart's title as the measurement."""
    cells: Cells = {}
    if title:
        cells[(1, 1)] = title
    top = 3 if title else 1
    cats = list(categories) or list(range(1, 1 + max((len(v) for _, v in series), default=0)))
    head = cat_title or "Flaw"
    people = series and all(_is_person(n) for n, _ in series) and title
    if people:
        cells[(top, 1)], cells[(top, 2)], cells[(top, 3)] = head, "Inspector", title
        r = top + 1
        for name, vals in series:
            for cat, v in zip(cats, vals):
                if v is None or v == "":
                    continue
                cells[(r, 1)], cells[(r, 2)], cells[(r, 3)] = cat, name, v
                r += 1
        return cells
    cells[(top, 1)] = head
    for j, (name, _) in enumerate(series, start=2):
        cells[(top, j)] = name or f"Series {j - 1}"
    for i, cat in enumerate(cats, start=top + 1):
        cells[(i, 1)] = cat
        for j, (_, vals) in enumerate(series, start=2):
            k = i - top - 1
            if k < len(vals) and vals[k] not in (None, ""):
                cells[(i, j)] = vals[k]
    return cells


# ── .xlsx / .xlsm: text boxes and charts from the file's own XML ───────
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
      "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}


def _rels(z: zipfile.ZipFile, part: str) -> Dict[str, str]:
    d, f = part.rsplit("/", 1) if "/" in part else ("", part)
    rp = f"{d}/_rels/{f}.rels" if d else f"_rels/{f}.rels"
    if rp not in z.namelist():
        return {}
    out = {}
    for rel in ET.fromstring(z.read(rp)).findall("pr:Relationship", NS):
        tgt = rel.get("Target", "")
        if rel.get("TargetMode") == "External":
            continue
        full = os.path.normpath(os.path.join(d, tgt)).replace("\\", "/") \
            if not tgt.startswith("/") else tgt.lstrip("/")
        out[rel.get("Id")] = full
    return out


def _all_text(el) -> str:
    paras = []
    for p in el.iter(f"{{{NS['a']}}}p"):
        paras.append("".join(t.text or "" for t in p.iter(f"{{{NS['a']}}}t")))
    return "\n".join(paras)


def _cache(el) -> List:
    """Values of a c:cat / c:val / c:tx: the cached copy Excel saves (numCache
    / strCache), or literal values (numLit / strLit)."""
    if el is None:
        return []
    pts: Dict[int, object] = {}
    for kind in ("numCache", "strCache", "numLit", "strLit"):
        for cache in el.iter(f"{{{NS['c']}}}{kind}"):
            for pt in cache.findall("c:pt", NS):
                v = pt.find("c:v", NS)
                if v is None:
                    continue
                txt = v.text or ""
                try:
                    num = float(txt)
                    val: object = int(num) if num.is_integer() else num
                except ValueError:
                    val = txt
                pts[int(pt.get("idx", len(pts)))] = val
            n = cache.find("c:ptCount", NS)
            count = int(n.get("val")) if n is not None else (max(pts) + 1 if pts else 0)
            return [pts.get(i) for i in range(count)]
    v = el.find("c:v", NS)
    return [v.text] if v is not None else []


def _chart_from_xml(xml: bytes) -> Optional[Cells]:
    root = ET.fromstring(xml)
    title_el = root.find(".//c:chart/c:title", NS)
    title = _all_text(title_el).strip() if title_el is not None else ""
    cat_title = ""
    for ax in root.iter(f"{{{NS['c']}}}catAx"):
        t = ax.find("c:title", NS)
        if t is not None:
            cat_title = _all_text(t).strip()
    series, cats = [], []
    for ser in root.iter(f"{{{NS['c']}}}ser"):
        name = _cache(ser.find("c:tx", NS))
        vals = _cache(ser.find("c:val", NS)) or _cache(ser.find("c:yVal", NS))
        cat = _cache(ser.find("c:cat", NS)) or _cache(ser.find("c:xVal", NS))
        cats = cats or cat
        series.append((str(name[0]) if name else "", vals))
    if not series:
        return None
    return chart_cells(title, cat_title, cats, series)


def zip_extras(path) -> List[Extra]:
    """Text boxes and charts of an .xlsx / .xlsm, by sheet. Never raises on a
    drawing it does not understand: that drawing is skipped."""
    out: List[Extra] = []
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return out
    with z:
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
        except Exception:
            return out
        wrels = _rels(z, "xl/workbook.xml")
        for sh in wb.findall("m:sheets/m:sheet", NS):
            sheet = sh.get("name", "")
            part = wrels.get(sh.get(f"{{{NS['r']}}}id"), "")
            for dpart in _rels(z, part).values():
                if "/drawings/" not in dpart or dpart not in z.namelist():
                    continue
                drels = _rels(z, dpart)
                try:
                    droot = ET.fromstring(z.read(dpart))
                except Exception:
                    continue
                n = 0
                for anchor in list(droot):
                    for sp in anchor.iter(f"{{{NS['xdr']}}}sp"):
                        body = sp.find("xdr:txBody", NS)
                        text = _all_text(body) if body is not None else ""
                        if text.strip():
                            n += 1
                            out.append(Extra(sheet, f"{sheet} › text box {n}", text_cells(text)))
                    for ch in anchor.iter(f"{{{NS['c']}}}chart"):
                        cpart = drels.get(ch.get(f"{{{NS['r']}}}id"), "")
                        if cpart not in z.namelist():
                            continue
                        try:
                            cells = _chart_from_xml(z.read(cpart))
                        except Exception:
                            cells = None
                        if cells:
                            n += 1
                            out.append(Extra(sheet, f"{sheet} › chart {n}", cells))
    return out


# ── anything else: through the installed Excel ──────────────────────────
def _plain(v):
    """A COM value as openpyxl would give it."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in _COM_ERRORS:
        return None                                   # #DIV/0!, #N/A ...
    if isinstance(v, float):
        return int(v) if v.is_integer() else v
    if hasattr(v, "year") and hasattr(v, "month"):    # pywintypes.datetime
        try:
            return datetime(v.year, v.month, v.day, v.hour, v.minute, v.second)
        except Exception:
            return str(v)
    if isinstance(v, str) and not v.strip():
        return None
    return v


def _seq(v) -> List:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return [_plain(v)]


def _clip_text() -> Optional[str]:
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(13):          # CF_UNICODETEXT
                return win32clipboard.GetClipboardData(13)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass
    return None


#: Clipboard formats that copying a picture out would destroy for good:
#: _clip_restore puts back text only. CF_BITMAP, CF_METAFILEPICT, CF_TIFF,
#: CF_DIB, CF_ENHMETAFILE, CF_HDROP (copied files), CF_DIBV5.
_NOT_TEXT_FORMATS = {2, 3, 6, 8, 14, 15, 17}
_NOT_TEXT_NAMES = ("png", "jfif", "gif", "filename", "shell idlist", "filegroupdescriptor")


def _clipboard_formats() -> set:
    """What the clipboard holds now: its format numbers (registered formats
    as their lower-cased names)."""
    import win32clipboard
    out: set = set()
    win32clipboard.OpenClipboard()
    try:
        fmt = win32clipboard.EnumClipboardFormats(0)
        while fmt:
            if fmt >= 0xC000:
                try:
                    out.add(win32clipboard.GetClipboardFormatName(fmt).lower())
                except Exception:
                    out.add(fmt)
            else:
                out.add(fmt)
            fmt = win32clipboard.EnumClipboardFormats(fmt)
    finally:
        win32clipboard.CloseClipboard()
    return out


def cloud_clipboard_on() -> bool:
    """Windows' 'Sync across your devices' for the clipboard (Settings ›
    System › Clipboard), unless a policy turns it off."""
    try:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Policies\Microsoft\Windows\System") as k:
                if int(winreg.QueryValueEx(k, "AllowCrossDeviceClipboard")[0]) == 0:
                    return False
        except OSError:
            pass
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Clipboard") as k:
            return int(winreg.QueryValueEx(k, "EnableCloudClipboard")[0]) == 1
    except Exception:
        return False


def clipboard_safe() -> Tuple[bool, str]:
    """(True, '') when pictures may be copied out through the clipboard now;
    else (False, why). Not when Windows syncs the clipboard to other devices
    (a picture could leave the PC), nor when the clipboard holds a picture or
    files the copy would replace (only text is put back afterwards). Used by
    the knowledge graph's background rebuild, which the user is not
    watching; the Ledger's own reads decide for themselves."""
    if os.name != "nt":
        return False, "the pictures come out through the clipboard only on Windows"
    if cloud_clipboard_on():
        return False, "Windows syncs this PC's clipboard to your other devices"
    try:
        held = _clipboard_formats()
    except Exception:
        return False, "the clipboard could not be checked"
    for f in held:
        if (isinstance(f, int) and f in _NOT_TEXT_FORMATS) or (
                isinstance(f, str) and any(n in f for n in _NOT_TEXT_NAMES)):
            return False, "the clipboard holds a picture or files that copying would replace"
    return True, ""


def _clip_restore(text: Optional[str]) -> None:
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            if text is not None:
                win32clipboard.SetClipboardText(text, 13)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass


#: How long Excel may take to open one workbook before it is stopped. A
#: rights-protected file can ask the user to sign in, and a hidden Excel would
#: wait for that forever (measured with a password prompt: the reader hung).
OPEN_TIMEOUT_S = 120


def read_with_excel(path, want_pictures: bool = True, app_factory=None,
                    timeout: float = OPEN_TIMEOUT_S) -> ExcelBook:
    """Open `path` read-only in a hidden, macro-disabled Excel and copy out its
    cells, text boxes, charts and (optionally) pictures. app_factory is for
    tests: it returns an object shaped like Excel.Application."""
    path = Path(path).resolve()
    if kind_of(path) == "password":
        raise ExcelOpenError(f"{path.name} has a password to open; open it in Excel, save "
                             "a copy without the password, and add that copy")
    com = None
    if app_factory is None:
        if not excel_available():
            raise ExcelOpenError(
                f"{path.name} can only be opened by Excel, and Excel automation is not "
                "available here (it needs Windows, Excel and the pywin32 package)")
        import pythoncom
        from win32com.client import DispatchEx
        pythoncom.CoInitialize()                # per thread: the tab's worker too
        com = pythoncom

        def app_factory():
            return DispatchEx("Excel.Application")
    app = book = None
    clip = _clip_text() if want_pictures and com is not None else None
    used_clip = False
    watchdog = None
    stopped = []
    try:
        app = app_factory()
        _tie_excel(app)
        watchdog = _watchdog(app, timeout, stopped) if com is not None else None
        for attr, val in (("Visible", False), ("DisplayAlerts", False),
                          ("AskToUpdateLinks", False), ("EnableEvents", False),
                          ("ScreenUpdating", False), ("AutomationSecurity", 3)):
            try:
                setattr(app, attr, val)
            except Exception:
                pass
        try:
            # A password the workbook does not have: a protected file fails here
            # instead of putting a password prompt in front of the user.
            book = app.Workbooks.Open(str(path), UpdateLinks=0, ReadOnly=True,
                                      IgnoreReadOnlyRecommended=True, AddToMru=False,
                                      Notify=False, Password="council-no-password",
                                      WriteResPassword="council-no-password")
        except Exception as e:
            msg = str(e)
            if stopped:
                raise ExcelOpenError(
                    f"Excel did not open {path.name} within {int(timeout)} s and was "
                    "stopped - it may be asking you to sign in or confirm something. "
                    "Open the file in Excel yourself once, then try again") from e
            if re.search(r"(?i)password", msg):
                raise ExcelOpenError(f"{path.name} is password-protected; open it in Excel, "
                                     "remove the password (or save an unprotected copy) "
                                     "and add that file") from e
            raise ExcelOpenError(f"Excel could not open {path.name}: {msg[:300]}") from e
        if watchdog is not None:
            watchdog.cancel()          # opened: copying a big workbook out may take a while
        out = ExcelBook()
        for ws in book.Worksheets:
            name = str(ws.Name)
            cells: Cells = {}
            used = ws.UsedRange
            r0, c0 = int(used.Row), int(used.Column)
            data = used.Value
            if not isinstance(data, tuple):
                data = ((data,),)
            elif data and not isinstance(data[0], tuple):
                data = (data,)
            for i, row in enumerate(data):
                for j, v in enumerate(row):
                    v = _plain(v)
                    if v is not None:
                        cells[(r0 + i, c0 + j)] = v
            # a merged label reads the same from every cell it covers
            for (r, c), v in list(cells.items()):
                if not isinstance(v, str):
                    continue
                try:
                    cell = ws.Cells(r, c)
                    if cell.MergeCells:
                        area = cell.MergeArea
                        for rr in range(int(area.Row), int(area.Row) + int(area.Rows.Count)):
                            for cc in range(int(area.Column),
                                            int(area.Column) + int(area.Columns.Count)):
                                cells.setdefault((rr, cc), ("__merged__", v))
                except Exception:
                    pass
            out.sheets.append((name, cells))
            n = 0
            for k in range(1, int(ws.Shapes.Count) + 1):
                sh = ws.Shapes.Item(k)
                try:
                    typ = int(sh.Type)
                except Exception:
                    continue
                anchor = _cell_of(sh)
                has_chart = False
                try:
                    has_chart = bool(sh.HasChart)
                except Exception:
                    has_chart = typ == MSO_CHART
                if has_chart:
                    try:
                        ch = sh.Chart
                        title = str(ch.ChartTitle.Text) if ch.HasTitle else ""
                        cat_title = ""
                        try:
                            ax = ch.Axes(1)
                            cat_title = str(ax.AxisTitle.Text) if ax.HasTitle else ""
                        except Exception:
                            pass
                        series, cats = [], []
                        sc = ch.SeriesCollection()
                        for s in range(1, int(sc.Count) + 1):
                            item = sc.Item(s)
                            cats = cats or _seq(item.XValues)
                            series.append((str(item.Name or ""), _seq(item.Values)))
                        if series:
                            n += 1
                            out.extras.append(Extra(name, f"{name} › chart {n}",
                                                    chart_cells(title, cat_title, cats, series)))
                    except Exception as e:
                        out.notes.append(f"{path.name} › {name}: a chart could not be read ({e})")
                    continue
                text = ""
                try:
                    if sh.TextFrame2.HasText:
                        text = str(sh.TextFrame2.TextRange.Text)
                except Exception:
                    pass
                if text.strip():
                    n += 1
                    out.extras.append(Extra(name, f"{name} › text box {n}", text_cells(text)))
                    continue
                if typ in (MSO_PICTURE, MSO_LINKED_PICTURE) and not want_pictures:
                    # listed, never copied: no clipboard (the caller counts them)
                    out.pictures.append(Picture(name, anchor, b""))
                elif typ in (MSO_PICTURE, MSO_LINKED_PICTURE) and want_pictures:
                    data_png = _copy_picture(sh)
                    used_clip = True
                    if data_png:
                        out.pictures.append(Picture(name, anchor, data_png))
                    else:
                        out.notes.append(f"{path.name} › {name}!{anchor}: a picture could not "
                                         "be copied out (the workbook may forbid copying)")
        return out
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if book is not None:
            try:
                book.Close(SaveChanges=False)
            except Exception:
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        book = app = None
        if used_clip:
            _clip_restore(clip)
        if com is not None:
            com.CoUninitialize()


def _cell_of(shape) -> str:
    """The cell under a shape's top-left corner, as 'D5'.

    Range.Address is a property WITH arguments: from Python it is
    GetAddress(False, False); Address(False, False) calls the string '$D$5'
    and fails (measured on a user's workbook: every picture was at '?', and
    '?' in the saved picture's file name stopped the whole extraction)."""
    try:
        tl = shape.TopLeftCell
    except Exception:
        return "unknown"
    try:
        return str(tl.GetAddress(False, False))
    except Exception:
        pass
    try:
        return str(tl.Address).replace("$", "")
    except Exception:
        return "unknown"


#: Windows job objects holding every hidden Excel this process started.
#: Closing the last handle - this process ending, in any way - ends them.
_JOBS: list = []


def tie_to_this_process(pid: int) -> bool:
    """Put process `pid` in a job that Windows closes when THIS process ends,
    killing it. A rebuild's worker is a daemon thread: when the Council quits
    while it is inside Excel, read_with_excel's finally (Close, Quit) never
    runs and the hidden Excel stayed, with the workbook open (review,
    2026-10-16). True when tied; False where it cannot be (not Windows, no
    pywin32, already in a job that forbids it)."""
    if os.name != "nt":
        return False
    try:
        import win32api
        import win32con
        import win32job
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                                      False, int(pid))
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return False
    _JOBS.append(job)
    return True


def _tie_excel(app) -> None:
    """The hidden Excel `app` dies with this process (tie_to_this_process)."""
    try:
        import win32process
        pid = win32process.GetWindowThreadProcessId(int(app.Hwnd))[1]
    except Exception:
        return
    tie_to_this_process(pid)


def _watchdog(app, timeout: float, stopped: list):
    """Stop THIS hidden Excel (by its process id, never another Excel) if it
    has not finished within `timeout` seconds."""
    import threading
    try:
        import win32process
        pid = win32process.GetWindowThreadProcessId(int(app.Hwnd))[1]
    except Exception:
        return None

    def stop():
        stopped.append(pid)
        try:
            import signal
            os.kill(pid, signal.SIGTERM)          # TerminateProcess on Windows
        except Exception:
            pass
    t = threading.Timer(timeout, stop)
    t.daemon = True
    t.start()
    return t


def _copy_picture(shape) -> Optional[bytes]:
    try:
        from PIL import Image, ImageGrab
        shape.CopyPicture(Appearance=1, Format=2)         # as shown, bitmap
        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "PNG")
            return buf.getvalue()
    except Exception:
        pass
    return None
