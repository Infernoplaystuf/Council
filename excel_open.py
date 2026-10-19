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
import threading
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
    """A plain-language reason a workbook could not be opened.

    ``kind`` is one of:
      * ``'password'`` — the workbook is password-protected to open.
      * ``'rights'`` — IRM / sensitivity label; this account cannot read.
      * ``'corrupt'`` — generic "could not be opened" (hresult 0x800A03EC):
        corrupt file, HTML saved with an .xls extension, Trust-Center
        File Block, or a non-workbook that happens to have a workbook
        extension.
      * ``'timeout'`` — Excel asked for input (sign-in) and the watchdog
        stopped it.
      * ``'unavailable'`` — Excel COM automation is not available on this
        machine (no Windows, no pywin32, no registered Excel).
      * ``'access'`` — access denied by the OS (locked file, missing
        permission on the folder).
      * ``'unknown'`` — fallback; the message includes diagnostic bytes.
    """

    def __init__(self, message: str, kind: str = "unknown"):
        super().__init__(message)
        self.kind = kind


# COM error codes we recognize. Values stored as *signed* 32-bit ints because
# that is what pywintypes.com_error reports. Excel's own 0x800A03EC overflows
# a signed int, so it is -2146827284.
_HRESULT_EXCEL_CANNOT_OPEN = -2146827284         # 0x800A03EC
_HRESULT_ACCESS_DENIED = -2147024891             # 0x80070005
_HRESULT_FILE_NOT_FOUND = -2147024894            # 0x80070002


def _com_error_hresult(exc) -> Optional[int]:
    """Pull the HRESULT from a pywintypes.com_error, or return None.

    The structure is ``(hresult, description, excepinfo, argerr)`` where
    ``excepinfo`` (when present) is ``(code, source, description, help_file,
    help_context, scode)``. The scode inside excepinfo is the one Excel
    itself raised, which is what we classify on."""
    args = getattr(exc, "args", None) or ()
    for i in (2, 0):
        if i < len(args):
            v = args[i]
            if isinstance(v, tuple) and len(v) >= 6 and isinstance(v[5], int):
                return v[5]
            if isinstance(v, int):
                return v
    return None


def _com_error_description(exc) -> str:
    args = getattr(exc, "args", None) or ()
    if len(args) >= 3 and isinstance(args[2], tuple) and len(args[2]) >= 3:
        text = args[2][2]
        if text:
            return str(text).strip()
    if len(args) >= 2 and isinstance(args[1], str):
        return args[1].strip()
    return str(exc).strip()


def _diag_bytes(path: Path) -> Tuple[int, str]:
    """(size, first-8-bytes-as-hex) for a diagnostic error message."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0, ""
    try:
        with open(path, "rb") as fh:
            return size, fh.read(8).hex()
    except OSError:
        return size, ""


def classify_open_failure(exc, path, timeout: float = 0,
                          stopped: Optional[list] = None) -> ExcelOpenError:
    """Translate the exception from ``Workbooks.Open`` into a named mode.

    The pywintypes.com_error class is not imported at module level because
    tests may run on non-Windows machines and this module should still
    import. We duck-type by inspecting ``args``."""
    p = Path(path)
    name = p.name
    description = _com_error_description(exc)
    hresult = _com_error_hresult(exc)

    if stopped:
        return ExcelOpenError(
            f"Excel did not open {name} within {int(timeout)} s and was "
            "stopped - it may be asking you to sign in or confirm something. "
            "Open the file in Excel yourself once, then try again",
            kind="timeout")

    if re.search(r"(?i)password", description):
        return ExcelOpenError(
            f"{name} is password-protected; open it in Excel, remove the "
            "password (or save an unprotected copy) and add that file",
            kind="password")

    if re.search(r"(?i)rights.?protected|information.?rights|sensitivity label",
                 description):
        return ExcelOpenError(
            f"{name} is rights-protected (IRM / sensitivity label); open it "
            "in Excel yourself first to confirm you can view it, then try "
            "the extract again", kind="rights")

    if hresult == _HRESULT_EXCEL_CANNOT_OPEN:
        size, head = _diag_bytes(p)
        return ExcelOpenError(
            f"{name}: Excel refused to open the file. Common causes: the "
            "file is corrupt, is HTML saved with an .xls extension, is "
            "blocked by Excel's Trust Center (File Block rules for the "
            "format), or is not actually a workbook. "
            f"(size {size} bytes, first bytes {head or '<unreadable>'})",
            kind="corrupt")

    if hresult == _HRESULT_ACCESS_DENIED:
        return ExcelOpenError(
            f"{name}: access denied by the operating system. The file may be "
            "locked by another process, on a share this account cannot read, "
            "or in a folder Excel cannot reach.", kind="access")

    if hresult == _HRESULT_FILE_NOT_FOUND:
        return ExcelOpenError(
            f"{name}: the file could not be found by Excel when it tried to "
            "open it. It may have been moved or deleted since the scan.",
            kind="unknown")

    size, head = _diag_bytes(p)
    short = (description or "no description")[:200]
    return ExcelOpenError(
        f"Excel could not open {name}: {short} "
        f"[hresult={hresult!r}, size={size}, head={head or '<none>'}, "
        f"suffix={p.suffix}]", kind="unknown")


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


#: What _clip_restore can put back after a picture is copied out: plain text
#: (CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE). Anything else on the
#: clipboard - a picture, files, formatted text (HTML, RTF), a password
#: manager's "keep out of history" marks, an app's own formats - would be
#: lost or changed, so the knowledge graph does not copy then. An allow-list:
#: a list of what to refuse missed HTML, RTF, JPEG and CF_ENHMETAFILE's
#: neighbours (review, 2026-10-18).
_TEXT_ONLY_FORMATS = {1, 7, 13, 16}

#: One picture copy through the clipboard at a time in this process: the
#: Ledger and the knowledge graph's rebuild could both read a workbook with
#: pictures at once, and one put back the other's picture as "the user's
#: text" (review, 2026-10-18). Held from the first picture of a workbook
#: until its clipboard is put back.
_CLIPBOARD_LOCK = threading.Lock()


#: PIDs of hidden Excel processes this process spawned through
#: read_with_excel but has not yet closed. The Ledger tab's Cancel
#: button reads this to taskkill a stuck Workbooks.Open — the only way
#: to unblock a worker thread sitting in a C-level COM call. Not a
#: leak: each read_with_excel's finally block removes its PID whether
#: or not app.Quit() succeeded.
_ACTIVE_EXCEL_PIDS: "set[int]" = set()
_ACTIVE_EXCEL_LOCK = threading.Lock()


def _register_excel_pid(app) -> Optional[int]:
    """The PID behind an Excel.Application COM object. Returns None
    outside Windows, when pywin32 is absent, or when the hwnd cannot
    be read (DispatchEx without a visible window — rare)."""
    try:
        import win32process
        hwnd = int(getattr(app, "Hwnd", 0))
        if not hwnd:
            return None
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return None
        with _ACTIVE_EXCEL_LOCK:
            _ACTIVE_EXCEL_PIDS.add(int(pid))
        return int(pid)
    except Exception:                                       # noqa: BLE001
        return None


def _unregister_excel_pid(pid: Optional[int]) -> None:
    if pid is None:
        return
    with _ACTIVE_EXCEL_LOCK:
        _ACTIVE_EXCEL_PIDS.discard(int(pid))


def kill_active_excels() -> int:
    """Terminate every hidden Excel this process spawned but has not
    yet closed. Returns the count taskkill was asked to end.

    The Ledger's Cancel button is the one caller: a Workbooks.Open
    blocked for 2 minutes on a modal dialog cannot be interrupted
    from Python, but killing the subprocess forces the COM call to
    raise and the worker thread to exit on the next iteration. The
    user's manually-opened Excel is NOT touched — only PIDs this
    process spawned via DispatchEx. No-op outside Windows or when
    pywin32 is absent."""
    if os.name != "nt":
        return 0
    with _ACTIVE_EXCEL_LOCK:
        pids = list(_ACTIVE_EXCEL_PIDS)
    if not pids:
        return 0
    import subprocess
    killed = 0
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, check=False, timeout=5)
            killed += 1
        except Exception:                                   # noqa: BLE001
            continue
    return killed


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


def _clip_seq() -> Optional[int]:
    """Windows' clipboard change counter (it goes up whenever anything is put
    on the clipboard), or None when it cannot be read."""
    try:
        import win32clipboard
        return int(win32clipboard.GetClipboardSequenceNumber())
    except Exception:
        pass
    try:
        import ctypes
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except Exception:
        return None


def _remote_session() -> bool:
    """This is a Remote Desktop session: its clipboard is shared with the
    computer the user connects from (SM_REMOTESESSION)."""
    if os.environ.get("SESSIONNAME", "").upper().startswith("RDP-"):
        return True
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))
    except Exception:
        return False


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
    or this is a Remote Desktop session (a picture could leave the PC), nor
    when the clipboard holds anything but plain text (only text is put back
    afterwards). Used by the knowledge graph's background rebuild, which the
    user is not watching; the Ledger's own reads decide for themselves."""
    if os.name != "nt":
        return False, "the pictures come out through the clipboard only on Windows"
    if cloud_clipboard_on():
        return False, "Windows syncs this PC's clipboard to your other devices"
    if _remote_session():
        return False, ("this is a Remote Desktop session, whose clipboard is shared with "
                       "the other computer")
    try:
        held = _clipboard_formats()
    except Exception:
        return False, "the clipboard could not be checked"
    if any(f not in _TEXT_ONLY_FORMATS for f in held):
        return False, "the clipboard holds something besides plain text, which copying would " \
                      "replace"
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
                    timeout: float = OPEN_TIMEOUT_S, careful: bool = False) -> ExcelBook:
    """Open `path` read-only in a hidden, macro-disabled Excel and copy out its
    cells, text boxes, charts and (optionally) pictures. app_factory is for
    tests: it returns an object shaped like Excel.Application.

    careful (the knowledge graph's background rebuild, which the user is not
    watching): the clipboard's change counter is noted when the read starts
    (just after clipboard_safe said yes) and after each copy. When anything
    else put something on the clipboard meanwhile - the user copying while
    the rebuild ran - no more pictures are copied (each is listed with no
    data, and a note says why) and the clipboard is left as it is, not put
    back over what the user copied."""
    path = Path(path).resolve()
    if kind_of(path) == "password":
        raise ExcelOpenError(
            f"{path.name} has a password to open; open it in Excel, save a "
            "copy without the password, and add that copy", kind="password")
    com = None
    if app_factory is None:
        if not excel_available():
            raise ExcelOpenError(
                f"{path.name} can only be opened by Excel, and Excel automation is not "
                "available here (it needs Windows, Excel and the pywin32 package)",
                kind="unavailable")
        import pythoncom
        from win32com.client import DispatchEx
        pythoncom.CoInitialize()                # per thread: the tab's worker too
        com = pythoncom

        def app_factory():
            return DispatchEx("Excel.Application")
    app = book = None
    clip = None
    seq = None                      # careful: the clipboard's counter as last seen
    clip_why = ""                   # careful: why no more pictures are copied
    if careful and want_pictures:
        seq = _clip_seq()
        clip = _clip_text()
        if seq is None:
            clip_why = "the clipboard could not be checked"
    locked = used_clip = False
    watchdog = None
    stopped = []
    pid: Optional[int] = None
    try:
        app = app_factory()
        _tie_excel(app)
        pid = _register_excel_pid(app)   # so the Ledger's Cancel can kill it
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
            raise classify_open_failure(e, path, timeout, stopped) from e
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
                    if not locked and not clip_why:
                        # careful does not wait: the other reading changes
                        # the clipboard anyway, and its pictures wait
                        locked = _CLIPBOARD_LOCK.acquire(blocking=not careful)
                        if not locked:
                            clip_why = "another reading was using the clipboard"
                        elif not careful:
                            clip = _clip_text() if com is not None else None
                    if careful and not clip_why and _clip_seq() != seq:
                        clip_why = "the clipboard changed while the workbook was being read"
                    if clip_why:
                        if not any(n.endswith(clip_why) for n in out.notes):
                            out.notes.append(f"{path.name} › {name}: picture(s) not copied: "
                                             f"{clip_why}")
                        out.pictures.append(Picture(name, anchor, b""))
                        continue
                    data_png = _copy_picture(sh)
                    used_clip = True
                    if careful:
                        seq = _clip_seq()
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
        _unregister_excel_pid(pid)
        try:
            # careful: only when nothing else was put there since the last
            # copy (that would be the user's, and stays)
            if used_clip and (not careful or (seq is not None and _clip_seq() == seq)):
                _clip_restore(clip)
        finally:
            if locked:
                _CLIPBOARD_LOCK.release()
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
