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
  * an Excel that will not start, or refuses every call (a dialog of its own
    waiting - its license agreement), before the workbook opens or while it
    is read, is said so once per Ledger extract, and the one it started is
    ended, never left running hidden; a call refused for a moment (a new
    Excel still busy) is asked again.
  * every Excel object a read held - in an error's frames too - is let go
    before COM is closed on its thread (_drop_frames): an Excel told to quit
    then ends at once, and COM's memory is not left corrupt.
  * a path longer than Excel opens (EXCEL_MAX_PATH) and a 0-byte file are
    said so before Excel starts.
  * Pictures come from a copy of the workbook Excel saves as .xlsx in a
    temporary folder, read from its zip (zip_pictures) - not through the
    clipboard, which took over the user's (2026-10-22). Only when Excel will
    not save a copy (rights protection may forbid it) are they copied through
    the clipboard, carefully: not when it holds anything but plain text, and
    no more once the user copies something; text there beforehand is put back.

TEXT BOXES AND CHARTS
  For .xlsx / .xlsm they are read straight from the file's XML (no Excel
  needed); through Excel for the rest. Each becomes a small table the reader
  already understands: a text box's lines ("Laser Power (W): 300"), a chart's
  categories against its series. A chart whose series are named by initials
  ("JB", "MK") becomes one row per reading with an Inspector column, under the
  chart's title as the measurement.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import zipfile
from contextlib import contextmanager
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
        machine (no Windows, no pywin32, no registered Excel); or Excel
        could not be started, or refuses every call (a dialog of its own
        waiting) - then ``down`` says why (_excel_down).
      * ``'access'`` — access denied by the OS (locked file, missing
        permission on the folder).
      * ``'path'`` — the path is longer than Excel opens (EXCEL_MAX_PATH),
        or than Windows allows (MAX_PATH, long paths off): checked before
        Excel starts.
      * ``'empty'`` — a 0-byte file: there is nothing in it to read.
      * ``'cancelled'`` — the Ledger's Cancel was pressed while this
        read was starting Excel (kill_active_excels).
      * ``'unknown'`` — fallback; the message includes diagnostic bytes.

    ``for_good``: reading the same bytes again cannot help (an empty file;
    Excel saying the file's format or extension is not valid), so the
    knowledge graph remembers it by content hash (kg_excel.Unreadable).
    """

    down = ""
    for_good = False

    def __init__(self, message: str, kind: str = "unknown"):
        super().__init__(message)
        self.kind = kind


# COM error codes we recognize. Values stored as *signed* 32-bit ints because
# that is what pywintypes.com_error reports. Excel's own 0x800A03EC overflows
# a signed int, so it is -2146827284.
_HRESULT_EXCEL_CANNOT_OPEN = -2146827284         # 0x800A03EC
_HRESULT_ACCESS_DENIED = -2147024891             # 0x80070005
_HRESULT_FILE_NOT_FOUND = -2147024894            # 0x80070002
#: Excel refusing a call: a dialog of its own is up (its license agreement,
#: measured on this PC) - RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER.
_HRESULTS_REFUSED = (-2147418111, -2147417846)   # 0x80010001, 0x8001010A
#: Excel gone in the middle of a call: RPC_E_DISCONNECTED (0x80010108),
#: RPC_S_SERVER_UNAVAILABLE (0x800706BA), RPC_S_CALL_FAILED (0x800706BE) -
#: it closed or crashed, or was ended from outside (Task Manager, Cancel).
_HRESULTS_GONE = (-2147417848, -2147023174, -2147023170)

#: The longest full path the installed Excel opens. Measured on this PC
#: (Microsoft 365): a valid .xls opens at 255 characters; at 256-259 Excel
#: says only 'Open method of Workbooks class failed', which read as a
#: corrupt file - and the 218 this code assumed was wrong (review,
#: 2026-10-20). Longer paths are refused before Excel starts.
EXCEL_MAX_PATH = 255


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


def _refused(exc) -> bool:
    """`exc` is Excel refusing the call ('Call was rejected by callee')."""
    args = getattr(exc, "args", None) or ()
    return any(isinstance(c, int) and c in _HRESULTS_REFUSED
               for c in (args[0] if args else None, _com_error_hresult(exc)))


def _gone(exc) -> bool:
    """`exc` is Excel gone in the middle of a call (_HRESULTS_GONE)."""
    args = getattr(exc, "args", None) or ()
    return any(isinstance(c, int) and c in _HRESULTS_GONE
               for c in (args[0] if args else None, _com_error_hresult(exc)))


def _refusal(exc, obj=None, name: str = ""):
    """The refusal behind `exc` - Excel refusing a call on `obj` looking up
    `name` - or None. pywin32's late-bound object (what DispatchEx gives
    here: no makepy cache for Excel) turns a refused name lookup into a bare
    AttributeError ('Excel.Application.Hwnd'), the HRESULT lost: a refusing
    Excel was read as any other failure, never asked again nor remembered
    (review, 2026-10-20). The name is looked up again through the object's
    own IDispatch, which keeps it: refused there too, that refusal. Answered
    there, the lookup was refused a moment ago (nothing else makes pywin32
    fail a name Excel knows): `exc` itself, to be asked again."""
    if _refused(exc):
        return exc
    ole = (getattr(obj, "__dict__", None) or {}).get("_oleobj_")
    if not isinstance(exc, AttributeError) or ole is None or not name:
        return None
    try:
        ole.GetIDsOfNames(0, name)
    except Exception as e:                                  # noqa: BLE001
        return e if _refused(e) else None
    return exc


def _gone_behind(exc, obj=None, name: str = ""):
    """The error saying Excel is gone (_gone) behind `exc` - Excel's `obj`
    failing `name` - or None. pywin32's late-bound object swallows the
    error of a name it looks up on an object for the first time: Excel
    ended in the middle of a read raised a bare AttributeError
    ('<unknown>.Shapes'), the HRESULT lost - and each new worksheet's Name,
    UsedRange and Shapes are such names - so the read ended 'could not be
    read (AttributeError(...))', kind 'other' (2 runs in 2 with real
    Excel ended by taskkill, measured here; review, 2026-10-20). The name
    is looked up again through the object's own IDispatch, as _refusal
    does: RPC_S_SERVER_UNAVAILABLE there (measured), that error."""
    if _gone(exc):
        return exc
    ole = (getattr(obj, "__dict__", None) or {}).get("_oleobj_")
    if not isinstance(exc, AttributeError) or ole is None or not name:
        return None
    try:
        ole.GetIDsOfNames(0, name)
    except Exception as e:                                  # noqa: BLE001
        return e if _gone(e) else None
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
        # 0x800A03EC is Excel's catch-all: its own words say which. A moved
        # file, a path over Excel's limit or a file open elsewhere were all
        # reported as corrupt, with the words thrown away (review,
        # 2026-10-19). A path over EXCEL_MAX_PATH no longer gets here
        # (read_with_excel); the 218 said here was wrong (review, 2026-10-20).
        said = f" Excel said: {description[:200]}" if description else ""
        if re.search(r"(?i)couldn.?t find|could not find|cannot find|"
                     r"not found|check the spelling|path.*too long", description):
            return ExcelOpenError(
                f"{name}: Excel could not find the file - moved, renamed, or a "
                f"path longer than Excel accepts ({EXCEL_MAX_PATH} characters: "
                f"{len(str(p))} here).{said}", kind="unknown")
        if re.search(r"(?i)cannot access|can.?t access|being used|is locked|"
                     r"read-only|permission", description):
            return ExcelOpenError(
                f"{name}: Excel cannot access the file - open in another "
                f"program, locked, or on a share this account cannot read.{said}",
                kind="access")
        size, head = _diag_bytes(p)
        err = ExcelOpenError(
            f"{name}: Excel refused to open the file. Common causes: the "
            "file is corrupt, is HTML saved with an .xls extension, is "
            "blocked by Excel's Trust Center (File Block rules for the "
            "format), or is not actually a workbook. "
            f"(size {size} bytes, first bytes {head or '<unreadable>'}){said}",
            kind="corrupt")
        # Excel's own words that these bytes are no workbook of this kind:
        # the same file is refused again, and the knowledge graph started a
        # hidden Excel for it on every rebuild, unchanged or not (review,
        # 2026-10-20). 'Open method of Workbooks class failed' says nothing
        # of the kind (a path too long gave it too), nor does a Trust Center
        # block (a setting the user may change): those are asked again.
        err.for_good = bool(re.search(r"(?i)file format or file extension is not valid",
                                      description))
        return err

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
    DRMEncrypted data space, a password the StrongEncryption one. An .xls
    with a password to open is told by the FILEPASS record at the start of
    its Workbook stream (_ole_kind).

    The directory is read first: the scan of the first 8 MB for the stream
    names ran before it and found an 'EncryptedPackage' anywhere - an .xls
    whose only encrypted thing is an embedded document (a password .docx
    in an object's storage) was refused as 'password', and the knowledge
    graph marked it unreadable for good, though Excel opens it without
    asking (review, 2026-10-19). The scan is kept for a container whose
    directory this reader does not understand."""
    p = Path(path)
    with open(p, "rb") as fh:
        head = fh.read(8)
        if head == OLE_MAGIC:
            kind = _ole_kind(fh)
            if kind is not None:
                return kind
            fh.seek(0)
            body = fh.read(8 << 20)
            if "EncryptedPackage".encode("utf-16-le") in body:
                return "rights" if "DRMEncrypted".encode("utf-16-le") in body else "password"
            return "legacy"
    if zipfile.is_zipfile(p):
        return "zip"
    return "unknown"


# ── inside an OLE (compound file) container ─────────────────────────────
#: Sector numbers from here up end a chain or mark FAT / DIFAT sectors (MS-CFB).
_MAXREGSECT = 0xFFFFFFFA


class _Ole:
    """Just enough of a compound file (MS-CFB) to list its streams and read
    the start of one; olefile is not a dependency. Raises ValueError (or
    struct.error) on anything it does not understand."""

    def __init__(self, fh):
        self.fh = fh
        fh.seek(0)
        h = fh.read(512)
        if len(h) < 512 or h[:8] != OLE_MAGIC:
            raise ValueError("not a compound file")
        major, _order, shift, mini_shift = struct.unpack_from("<HHHH", h, 0x1A)
        if shift not in (9, 12) or mini_shift != 6:
            raise ValueError("odd sector size")
        self.ss, self.mss, self.v3 = 1 << shift, 1 << mini_shift, major == 3
        n_fat, dir_start = struct.unpack_from("<II", h, 0x2C)
        self.cutoff, self.minifat_start, _n_mini, difat_next = struct.unpack_from(
            "<IIII", h, 0x38)
        fh.seek(0, 2)
        self.nsect = fh.tell() // self.ss          # no chain is longer than the file
        difat = list(struct.unpack_from("<109I", h, 0x4C))
        seen: set = set()
        while difat_next < _MAXREGSECT and difat_next not in seen and len(difat) < n_fat + 109:
            seen.add(difat_next)
            more = struct.unpack(f"<{self.ss // 4}I", self._sector(difat_next))
            difat.extend(more[:-1])
            difat_next = more[-1]
        self.fat_sectors = [s for s in difat if s < _MAXREGSECT][:n_fat]
        self._fat: Dict[int, tuple] = {}
        self.entries = self._directory(dir_start)

    def _sector(self, n: int) -> bytes:
        self.fh.seek((n + 1) * self.ss)
        b = self.fh.read(self.ss)
        if len(b) < self.ss:
            raise ValueError("a sector past the end of the file")
        return b

    def _next(self, n: int) -> int:
        per = self.ss // 4
        k = n // per
        if k not in self._fat:
            if k >= len(self.fat_sectors):
                raise ValueError("a sector outside the FAT")
            self._fat[k] = struct.unpack(f"<{per}I", self._sector(self.fat_sectors[k]))
        return self._fat[k][n % per]

    def _chain(self, start: int, nxt=None):
        nxt, n = nxt or self._next, 0
        while start < _MAXREGSECT:
            yield start
            n += 1
            if n > self.nsect:
                raise ValueError("a sector chain loops")
            start = nxt(start)

    def _directory(self, start: int) -> List[tuple]:
        """(name, type, left, right, child, first sector, size) per entry."""
        out = []
        for s in self._chain(start):
            b = self._sector(s)
            for i in range(0, self.ss, 128):
                nlen = struct.unpack_from("<H", b, i + 0x40)[0]
                left, right, child = struct.unpack_from("<III", b, i + 0x44)
                first, size = struct.unpack_from("<IQ", b, i + 0x74)
                name = b[i:i + max(0, min(nlen, 64) - 2)].decode("utf-16-le", "replace")
                out.append((name, b[i + 0x42], left, right, child, first,
                            size & 0xFFFFFFFF if self.v3 else size))
        return out

    def root_names(self) -> Dict[str, int]:
        """The streams and storages directly in the root, by name - not one
        of the same name inside an embedded object's storage."""
        out: Dict[str, int] = {}
        todo, seen = [self.entries[0][4]] if self.entries else [], set()
        while todo:
            i = todo.pop()
            if i >= len(self.entries) or i in seen:
                continue
            seen.add(i)
            e = self.entries[i]
            out.setdefault(e[0], i)
            todo += [e[2], e[3]]
        return out

    def read(self, i: int, limit: int) -> bytes:
        """The first `limit` bytes of stream `i`."""
        _name, _typ, _l, _r, _c, first, size = self.entries[i]
        want, buf = min(size, limit), bytearray()
        if size >= self.cutoff:
            for s in self._chain(first):
                if len(buf) >= want:
                    break
                buf += self._sector(s)
            return bytes(buf[:want])
        # a small stream lives in the mini stream (the root's own chain)
        root = list(self._chain(self.entries[0][5]))
        minifat = list(self._chain(self.minifat_start))
        per = self.ss // 4

        def next_mini(m: int) -> int:
            if m // per >= len(minifat):
                raise ValueError("a mini sector outside the mini FAT")
            return struct.unpack_from("<I", self._sector(minifat[m // per]), (m % per) * 4)[0]
        for m in self._chain(first, next_mini):
            if len(buf) >= want:
                break
            off = m * self.mss
            if off // self.ss >= len(root):
                raise ValueError("a mini sector past the mini stream")
            b = self._sector(root[off // self.ss])
            buf += b[off % self.ss:off % self.ss + self.mss]
        return bytes(buf[:want])


def _ole_kind(fh) -> Optional[str]:
    """'password', 'rights' or 'legacy' for an OLE container, from its
    directory; None when the directory cannot be read (kind_of then scans
    the bytes). A password-to-open .xls made Excel show a VISIBLE 'Password'
    dialog on the user's desktop - Password= in Workbooks.Open does not stop
    that for .xls - and the read sat on it for the 2-minute watchdog and was
    called a sign-in timeout (review, 2026-10-19). An encrypted .xlsx /
    .xlsb whose directory lies past the first 8 MB is found here too. Only
    the root's streams count: one inside an embedded object's storage is
    that object's. A Workbook stream not understood stays 'legacy': Excel is
    asked, as before."""
    try:
        ole = _Ole(fh)
        names = ole.root_names()
    except Exception:                                       # noqa: BLE001
        return None
    if "EncryptedPackage" in names or "EncryptionInfo" in names:
        return "rights" if any("DRMEncrypted" in e[0] for e in ole.entries) else "password"
    try:
        for stream in ("Workbook", "Book"):              # BIFF8 / BIFF5
            if stream in names:
                fp = _filepass(ole.read(names[stream], 8192))
                if fp is not None and not _default_password_opens(fp):
                    return "password"
                break
    except Exception:                                       # noqa: BLE001
        pass
    return "legacy"


_BIFF_BOF = (0x0809, 0x0409, 0x0209, 0x0009)
_BIFF_FILEPASS, _BIFF_EOF = 0x002F, 0x000A
#: The password Excel encrypts an .xls with when it has none to open (one
#: whose structure is protected with a password, measured with Excel 16):
#: Excel opens it without asking, so it is read as before (MS-OFFCRYPTO 2.3.6).
_DEFAULT_XLS_PASSWORD = "VelvetSweatshop"


def _filepass(stream: bytes) -> Optional[bytes]:
    """The body of the FILEPASS record of a BIFF Workbook stream's globals
    (it follows BOF: the rest of the file is encrypted), or None. Record
    headers stay plain after it, so the scan can go on safely."""
    pos = 0
    for n in range(64):
        if pos + 4 > len(stream):
            return None
        typ, length = struct.unpack_from("<HH", stream, pos)
        if n == 0 and typ not in _BIFF_BOF:
            return None                                     # not a BIFF stream
        if typ == _BIFF_FILEPASS:
            return stream[pos + 4:pos + 4 + length]
        if typ == _BIFF_EOF:
            return None
        pos += 4 + length
    return None


def _rc4(key: bytes, data: bytes) -> bytes:
    s, j = list(range(256)), 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out, i, j = bytearray(), 0, 0
    for b in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def _xor_verifier(password: str) -> int:
    """MS-OFFCRYPTO 2.3.7.1: the 16-bit verifier of XOR obfuscation."""
    v = 0
    for b in reversed([len(password)] + list(password.encode("latin-1", "replace"))):
        v = (((v >> 14) & 1) | ((v << 1) & 0x7FFF)) ^ b
    return v ^ 0xCE4B


def _default_password_opens(fp: bytes, password: str = _DEFAULT_XLS_PASSWORD) -> bool:
    """FILEPASS body `fp` verifies against `password` (Excel's default, so
    Excel opens the file without asking): XOR obfuscation, RC4 (MD5) or RC4
    CryptoAPI (SHA-1), each as MS-OFFCRYPTO 2.3.5 - 2.3.7 checks a password.
    False for anything else: a password Excel would ask for."""
    try:
        if len(fp) == 4:                                    # BIFF5: XOR key, verifier
            return struct.unpack_from("<H", fp, 2)[0] == _xor_verifier(password)
        kind, major, minor = struct.unpack_from("<HHH", fp, 0)
        if kind == 0:                                       # XOR obfuscation
            return struct.unpack_from("<H", fp, 4)[0] == _xor_verifier(password)
        if kind != 1:
            return False
        pw = password.encode("utf-16-le")
        if (major, minor) == (1, 1):                        # RC4, 40-bit, MD5
            salt, verifier = fp[6:22], fp[22:54]
            h1 = hashlib.md5((hashlib.md5(pw).digest()[:5] + salt) * 16).digest()[:5]
            key = hashlib.md5(h1 + struct.pack("<I", 0)).digest()
            plain = _rc4(key, verifier)
            return len(plain) == 32 and hashlib.md5(plain[:16]).digest() == plain[16:]
        if major in (2, 3, 4) and minor == 2:               # RC4 CryptoAPI
            hsize = struct.unpack_from("<I", fp, 10)[0]
            bits = struct.unpack_from("<I", fp, 14 + 16)[0] or 40
            v = 14 + hsize
            salt_size = struct.unpack_from("<I", fp, v)[0]
            salt = fp[v + 4:v + 4 + salt_size]
            enc = fp[v + 4 + salt_size:v + 20 + salt_size]
            hash_size = struct.unpack_from("<I", fp, v + 20 + salt_size)[0]
            enc_hash = fp[v + 24 + salt_size:v + 24 + salt_size + hash_size]
            h = hashlib.sha1(hashlib.sha1(salt + pw).digest() + struct.pack("<I", 0)).digest()
            key = h[:5] + b"\0" * 11 if bits == 40 else h[:bits // 8]
            plain = _rc4(key, enc + enc_hash)
            return hashlib.sha1(plain[:16]).digest() == plain[16:36]
    except (struct.error, ValueError, ZeroDivisionError):
        return False
    return False


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


def zip_pictures(path) -> Dict[Tuple[str, str], List[Tuple[bytes, str]]]:
    """Every embedded picture of an .xlsx / .xlsm, by (sheet, top-left cell
    'D5'), in drawing order: (its bytes, its extension). Read from the zip
    alone - no workbook is loaded. A linked picture (not in the file) and a
    drawing it does not understand are skipped; never raises."""
    out: Dict[Tuple[str, str], List[Tuple[bytes, str]]] = {}
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return out
    with z:
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
        except Exception:
            return out
        names = set(z.namelist())
        wrels = _rels(z, "xl/workbook.xml")
        for sh in wb.findall("m:sheets/m:sheet", NS):
            sheet = sh.get("name", "")
            part = wrels.get(sh.get(f"{{{NS['r']}}}id"), "")
            for dpart in _rels(z, part).values():
                if "/drawings/" not in dpart or dpart not in names:
                    continue
                drels = _rels(z, dpart)
                try:
                    droot = ET.fromstring(z.read(dpart))
                except Exception:
                    continue
                for anchor in list(droot):
                    fr = anchor.find("xdr:from", NS)
                    try:
                        col = int(fr.find("xdr:col", NS).text)
                        row = int(fr.find("xdr:row", NS).text)
                    except Exception:
                        continue
                    for pic in anchor.iter(f"{{{NS['xdr']}}}pic"):
                        blip = pic.find(".//a:blip", NS)
                        media = drels.get(blip.get(f"{{{NS['r']}}}embed"), "") \
                            if blip is not None else ""
                        if media not in names:
                            continue
                        try:
                            data = z.read(media)
                        except Exception:
                            continue
                        ext = media.rsplit(".", 1)[-1].lower() if "." in media else "png"
                        out.setdefault((sheet, _a1(row + 1, col + 1)), []).append((data, ext))
    return out


def _pictures_from_a_copy(book, path: Path, folder: str) -> Tuple[Optional[Dict], str]:
    """The open workbook's pictures without the clipboard (user, 2026-10-22:
    "There may be a clipboard issue where stuff can't be copied because the
    clip board is consumed somewhere"): Excel saves a copy of it as .xlsx in
    ``folder`` (the original is never written: it is open read-only, and
    SaveAs moves the open workbook onto the copy), and the pictures are read
    from that file's zip (zip_pictures). (None, why) when Excel will not save
    one - a rights-protected workbook may forbid it - or the copy cannot be
    read: the caller copies through the clipboard then, carefully."""
    copy = Path(folder) / "copy.xlsx"
    try:
        _ask(lambda: book.SaveAs(str(copy), 51), book, "SaveAs")     # xlOpenXMLWorkbook
    except Exception as e:                                  # noqa: BLE001
        why = e.why if isinstance(e, _Refused) else e
        return None, f"Excel would not save a copy to read them from ({_short(why)})"
    if not copy.is_file():
        return None, "Excel saved no copy to read them from"
    pics = zip_pictures(copy)
    if not pics and not zipfile.is_zipfile(copy):
        return None, "the copy Excel saved could not be read (rights protection?)"
    return pics, ""


def _short(e) -> str:
    return ((_com_error_description(e) if not isinstance(e, str) else e) or repr(e)
            ).rstrip(". ")[:160]


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

#: How many times kill_active_excels (the Ledger's Cancel) has run. A read
#: notes it when it is called - inside a Ledger extract, when the extract
#: began (cancel_scope) - and looks again before it starts Excel and once its
#: PID is registered: Cancel pressed in the ~0.5 s DispatchEx takes found no
#: PID to end, and the read went on to the end or to the 2-minute watchdog
#: (review, 2026-10-19). A count rather than a flag left set until the next
#: extract: the knowledge graph's reads that start after a Cancel are not
#: stopped by it. Read and bumped under _ACTIVE_EXCEL_LOCK, so a Cancel
#: either sees the PID or is seen by the read.
_CANCELS = 0

#: The Cancel count when this thread's Ledger extract began (cancel_scope),
#: and why Excel could not be started in it, or refuses every call
#: (``down``: _excel_down's arguments; None while it can).
_SCOPE = threading.local()
try:        # a guarded read runs on a thread of its own (council_core.file_guard)
    from council_core import file_guard as _file_guard
    _file_guard.carry(_SCOPE)
except Exception:                                           # noqa: BLE001
    pass

#: Hidden Excels this process is starting now (DispatchEx waiting) and has
#: ever started: an EXCEL.EXE a failed DispatchEx left behind is ended only
#: when no other read was starting one meanwhile (_end_unanswered).
_STARTING = {"now": 0, "ever": 0}


def _cancels() -> int:
    with _ACTIVE_EXCEL_LOCK:
        return _CANCELS


@contextmanager
def cancel_scope():
    """A Ledger extract (source_extract.extract) on this thread: a read inside
    it is stopped by a Cancel pressed at any time since the scope began, not
    only by one pressed after that read started. A read noted the count when
    it was called, after kind_of had read the file (up to 8 MB, longer on a
    share) and the 'through Excel' line was showing: a Cancel then found no
    PID, was not seen by the read, and the workbook was opened all the same
    (review, 2026-10-19). Reads on other threads - the knowledge graph's -
    still count from their own start.

    An Excel that could not be started, or refuses every call, is not tried
    again inside the scope: every .xls / .xlsb cost ~93 s, at Extract and
    again at Apply, each failing the same way (review, 2026-10-20). Each
    extract - Apply's too - tries once more: the user may have answered
    Excel's dialog meanwhile."""
    before = getattr(_SCOPE, "since", None)
    down = getattr(_SCOPE, "down", None)
    _SCOPE.since = _cancels()
    _SCOPE.down = None
    try:
        yield
    finally:
        _SCOPE.since = before
        _SCOPE.down = down


def _excel_down(why: str, refusing: bool = False) -> ExcelOpenError:
    """Excel could not be started - or started but refuses every call
    (`refusing`) - for `why`. The message names no workbook: the Ledger puts
    every workbook it left unread on one line (source_extract._extract_one;
    ``down`` says it is this)."""
    again = "extract" if getattr(_SCOPE, "since", None) is not None else "try"
    head = ("Excel started but refuses every call" if refusing
            else "Excel could not be started")
    err = ExcelOpenError(
        f"{head}: {why} — open Excel once yourself (it may be asking you to accept "
        f"its license), then {again} again", kind="unavailable")
    err.down = why
    return err


def _excel_failed(exc, path: Path, cancels: int, refusing: bool = False,
                  left: str = "") -> ExcelOpenError:
    """Excel could not be started, or still refused a call (`refusing`), with
    `exc` - `left` saying what it may have left running: the error to raise.
    Inside a Ledger extract it is remembered, so the extract's other
    workbooks fail at once (cancel_scope). A Cancel pressed meanwhile is
    raised instead (_stop_if_cancelled) and nothing is remembered."""
    _stop_if_cancelled(path, cancels)
    why = (_com_error_description(exc) or repr(exc)).rstrip(". ")[:200] + left
    if getattr(_SCOPE, "since", None) is not None:
        _SCOPE.down = (why, refusing)
    return _excel_down(why, refusing)


#: How long a call Excel refuses is asked again. A refusal can be momentary
#: (a new Excel still busy: 'the application is busy'): app.Hwnd was read
#: once, so an Excel refusing calls was neither tied nor registered for
#: Cancel; and one refused property set or Workbooks.Open marked Excel down
#: for the whole extract (review, 2026-10-20).
_REFUSED_WAIT_S = 2.0


class _Refused(Exception):
    """Excel still refused a call after _REFUSED_WAIT_S; `why`, its refusal."""

    def __init__(self, why):
        super().__init__(why)
        self.why = why


def _ask(call, obj, name: str, wait: Optional[float] = None, stopped=()):
    """call() - `name` read, set or called on Excel's `obj` - asked again for
    `wait` seconds (_REFUSED_WAIT_S) while Excel refuses it (_refusal);
    _Refused when it still does then, at once when the watchdog stopped this
    Excel (`stopped`). Excel gone meanwhile is raised as the error saying so
    (_gone_behind); any other failure as it came."""
    import time
    handling = sys.exc_info()[1]            # the caller's, if any (_drop_frames)
    end = time.monotonic() + (_REFUSED_WAIT_S if wait is None else wait)
    while True:
        try:
            return call()
        except Exception as e:
            why = gone = None
            try:
                why = _refusal(e, obj, name)
                if why is None:
                    gone = _gone_behind(e, obj, name)
                    if gone is None or gone is e:
                        raise
                    raise gone from e
                if stopped or time.monotonic() >= end:
                    if not _refused(why):
                        raise       # pywin32's lookup kept failing; Excel's answered
                    raise _Refused(why) from e
                # refused for a moment: asked again, its error let go first
                _drop_frames(why, keep=handling)
            finally:
                # The refusal kept here held this frame through its traceback,
                # and this frame held Excel's objects (`obj`, `call`): a cycle
                # only gc freed, after COM was closed on the thread - Excel
                # busy once and then answering froze a Ledger extract for
                # 60-120 s (5 runs in 11, real Excel; review, 2026-10-20).
                why = gone = None
        time.sleep(0.1)


def _drop_frames(exc, keep=None) -> None:
    """Clear the locals of the finished frames `exc` - and the errors it came
    from - passed through: they held Excel's objects (_ask's frame the
    Workbooks collection Excel would not open a workbook with, pywin32's
    own frames the object asked). Released after CoUninitialize, with the
    error, they kept that hidden Excel running for a minute after it was
    told to quit, and left COM's memory corrupt: a later workbook's Excel
    crashed the Council - an access violation in pywin32's typecomp.Bind,
    on the Ledger's worker thread 3 times in 3 (measured here; review,
    2026-10-20). `keep`: the error the caller was handling when the read
    began, and those it came from - not this read's - left as they are."""
    import traceback
    todo, seen = [exc], set()
    while todo:
        x = todo.pop()
        if x is None or x is keep or id(x) in seen:
            continue
        seen.add(id(x))
        traceback.clear_frames(x.__traceback__)   # a frame still running: skipped
        todo += [x.__cause__, x.__context__]


def _put(app, attr: str, value) -> None:
    """app.<attr> = value - through pywin32's object's own IDispatch: its
    setattr turns every failure, a refusal too, into AttributeError
    ('Property ... can not be set'), so a refused macro setting was not
    seen (review, 2026-10-20). A test's fake is set as it is."""
    ole = (getattr(app, "__dict__", None) or {}).get("_oleobj_")
    if ole is None:
        setattr(app, attr, value)
        return
    import pythoncom
    ole.Invoke(ole.GetIDsOfNames(0, attr), 0, pythoncom.DISPATCH_PROPERTYPUT, 0, value)


def _excel_pid(app, wait: Optional[float] = None) -> Optional[int]:
    """The PID behind an Excel.Application COM object, from its window -
    asked again for `wait` seconds (_REFUSED_WAIT_S) while Excel refuses the
    call. None outside Windows, when pywin32 is absent, when the window
    cannot be read (a fake, an Excel still refusing) or names no process."""
    handling = sys.exc_info()[1]            # the caller's, if any (_drop_frames)
    try:
        import win32process
        hwnd = int(_ask(lambda: app.Hwnd, app, "Hwnd", wait))
        if not hwnd:
            return None
        return int(win32process.GetWindowThreadProcessId(hwnd)[1]) or None
    except Exception as e:                                  # noqa: BLE001
        # Its refusal, kept, kept Excel itself alive past CoUninitialize
        # (an Excel refusing from the start; review, 2026-10-20)
        _drop_frames(e, keep=handling)
        return None


def _register_excel_pid(pid: Optional[int]) -> Optional[int]:
    """Note `pid`, a hidden Excel this process started, for the Ledger's
    Cancel (kill_active_excels)."""
    if not pid:
        return None
    with _ACTIVE_EXCEL_LOCK:
        _ACTIVE_EXCEL_PIDS.add(int(pid))
    return int(pid)


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
    pywin32 is absent - but a read still starting its Excel then stops
    itself (_CANCELS)."""
    global _CANCELS
    with _ACTIVE_EXCEL_LOCK:
        _CANCELS += 1
        pids = list(_ACTIVE_EXCEL_PIDS)
    if os.name != "nt":
        return 0
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


def _stop_if_cancelled(path: Path, cancels: int) -> None:
    """Raise ExcelOpenError('cancelled') when kill_active_excels ran since
    the read (or its extract: cancel_scope) noted `cancels`; read_with_excel's
    finally then quits its Excel, if it has one, before any workbook is
    opened in it."""
    if _cancels() != cancels:
        raise ExcelOpenError(f"{path.name}: cancelled before it was opened",
                             kind="cancelled")


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


def _a1(row: int, col: int) -> str:
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


#: How many questions _merged_text_cells may ask beyond one per text cell
#: (what asking cell by cell cost); a sheet with fewer text cells than this
#: is asked at most twice as many as it has.
_MERGE_SLACK = 32


def _merged_text_cells(ws, used, cells: Cells) -> List[Tuple[int, int]]:
    """The text cells of sheet `ws` that are part of a merged range. Two COM
    calls per text cell (Cells(r, c).MergeCells) took 24-39 s for a
    12,000-cell .xls and set off the stall watchdog in ordinary reads
    (review, 2026-10-19). Now one question for the whole used range -
    Excel's MergeCells is False when nothing in a range is merged, True when
    all of it is, None when some is - and only when some is, the same
    question for halves of it, down to the text cells that are merged.

    Halving only the longer side did no better than cell by cell, and often
    worse, on a table with a merged cell in every row (a Remarks column G:H)
    or a form (A:B label, C value, D:E label, F value): every half of the
    rows was mixed, down to single rows (review, 2026-10-19). Now when both
    halves along one side are mixed the other side is tried too, columns
    winning when both are: such a sheet takes a handful of questions. And
    the questions are counted - past one per text cell plus _MERGE_SLACK,
    what is left is asked cell by cell, so it never costs much more than
    the old loop."""
    texts = [k for k, v in cells.items() if isinstance(v, str)]
    if not texts:
        return []
    budget = len(texts) + min(_MERGE_SLACK, len(texts))
    count = {"asked": 1, "left": len(texts)}      # questions; text cells not yet settled
    try:
        whole = used.MergeCells
    except Exception:
        whole = None

    def ask(pts):
        """MergeCells of the box around `pts`; '?' when it cannot be asked."""
        count["asked"] += 1
        r1, r2 = min(p[0] for p in pts), max(p[0] for p in pts)
        c1, c2 = min(p[1] for p in pts), max(p[1] for p in pts)
        try:
            if (r1, c1) == (r2, c2):
                return ws.Cells(r1, c1).MergeCells
            return ws.Range(f"{_a1(r1, c1)}:{_a1(r2, c2)}").MergeCells
        except Exception:
            return "?"

    def halves(pts, axis):
        lo, hi = min(p[axis] for p in pts), max(p[axis] for p in pts)
        if lo == hi:
            return None
        mid = (lo + hi) // 2
        return [p for p in pts if p[axis] <= mid], [p for p in pts if p[axis] > mid]

    def room() -> bool:
        """Two more questions keep the total within the budget, whatever
        they answer (cell by cell, each costs what it settles)."""
        return count["asked"] + count["left"] + 2 <= budget
    found: set = set()
    todo = [(texts, whole)]
    while todo:
        pts, merged = todo.pop()
        if merged is not None and merged != "?":
            count["left"] -= len(pts)
            if merged:                           # every cell in the box is merged
                found.update(pts)
            continue
        if len(pts) > 1 and merged != "?" and room():
            by_row, by_col = halves(pts, 0), halves(pts, 1)
            rows = max(p[0] for p in pts) - min(p[0] for p in pts)
            cols = max(p[1] for p in pts) - min(p[1] for p in pts)
            tried = []                           # (by columns?, [(half, answer)])
            for split in ((by_row, by_col) if rows >= cols else (by_col, by_row)):
                if split is None or (tried and not room()):
                    continue
                tried.append((split is by_col, [(h, ask(h)) for h in split]))
                if any(a is not None for _h, a in tried[-1][1]):
                    break                        # a half settled: split this way
            settled = [t for t in tried if any(a is not None for _h, a in t[1])]
            # every half mixed, both ways: by columns
            chosen = settled[0] if settled else max(tried, key=lambda t: t[0])
            todo.extend(chosen[1])
            continue
        for p in pts:                            # cell by cell, as before
            count["left"] -= 1
            a = ask([p])
            if a and a != "?":
                found.add(p)
    return [k for k in texts if k in found]


_A1_AREA = re.compile(r"\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?")


def _col_of(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _merged_area(ws, r: int, c: int) -> Tuple[int, int, int, int]:
    """(first row, first column, last row, last column) of the merged range
    holding cell (r, c): its address ('$G$2:$H$2') in one call, parsed.
    Row, Column, Rows.Count and Columns.Count were 7 calls and more for each
    merged text cell (review, 2026-10-19); they are asked only for an
    address not of that form (a whole row, '$3:$3')."""
    area = ws.Cells(r, c).MergeArea
    try:
        m = _A1_AREA.fullmatch(str(area.Address))
    except Exception:
        m = None
    if m:
        r1, c1 = int(m[2]), _col_of(m[1])
        return (r1, c1, int(m[4]), _col_of(m[3])) if m[3] else (r1, c1, r1, c1)
    r1, c1 = int(area.Row), int(area.Column)
    return r1, c1, r1 + int(area.Rows.Count) - 1, c1 + int(area.Columns.Count) - 1


def _fill_merged(ws, used, cells: Cells) -> None:
    """A merged label reads the same from every cell it covers: each cell of
    a merged range whose text it holds gets ('__merged__', text), unless it
    has a value of its own. A range is asked about once, for its first text
    cell (by rows) - the others in it add nothing."""
    filled: set = set()
    for r, c in _merged_text_cells(ws, used, cells):
        if (r, c) in filled:
            continue
        v = cells[(r, c)]
        try:
            r1, c1, r2, c2 = _merged_area(ws, r, c)
        except Exception:
            continue
        for rr in range(r1, r2 + 1):
            for cc in range(c1, c2 + 1):
                cells.setdefault((rr, cc), ("__merged__", v))
                filled.add((rr, cc))


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
    handling = sys.exc_info()[1]            # the caller's, if any (_drop_frames)
    # a Cancel after this stops the read (_CANCELS) - inside a Ledger extract,
    # one since the extract began (cancel_scope)
    since = getattr(_SCOPE, "since", None)
    cancels = _cancels() if since is None else since
    path = Path(path).resolve()
    _stop_if_cancelled(path, cancels)       # pressed already: no Excel at all
    if len(str(path)) > EXCEL_MAX_PATH:
        # Excel said only 'Open method of Workbooks class failed', read as a
        # corrupt file (review, 2026-10-20): said for what it is, no Excel
        raise ExcelOpenError(
            f"{path.name}: path too long for Excel: {len(str(path))} characters (Excel "
            f"opens paths of up to {EXCEL_MAX_PATH}) — move the folder or shorten its name",
            kind="path")
    try:
        empty = path.stat().st_size == 0
    except OSError:
        empty = False                       # kind_of says why
    if empty:
        # Excel 'read' a 0-byte .xls as one blank sheet, and the run said
        # nothing of it (review, 2026-10-20)
        err = ExcelOpenError(f"{path.name}: empty file (0 bytes) — there is nothing in it "
                             "to read", kind="empty")
        err.for_good = True
        raise err
    if kind_of(path) == "password":
        raise ExcelOpenError(
            f"{path.name} has a password to open; open it in Excel, save a "
            "copy without the password, and add that copy", kind="password")
    if getattr(_SCOPE, "down", None):
        # Excel could not be started earlier in this extract: not again for
        # every workbook, ~93 s each (review, 2026-10-20)
        raise _excel_down(*_SCOPE.down)
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
    copy_dir = ""                   # where Excel saved the copy the pictures come from
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
    held = None                     # a handle to this read's Excel (_hold_excel)
    answered = False                # its window said its PID
    try:
        with _ACTIVE_EXCEL_LOCK:
            alone = not _STARTING["now"]        # no other read starting one
            _STARTING["now"] += 1
            _STARTING["ever"] += 1
            mine = _STARTING["ever"]
        before = _excel_processes() if com is not None else None
        try:
            app = app_factory()
        except Exception as e:
            # 'Server execution failed', after ~93 s here (its license
            # agreement waiting): said so, and not tried again by the rest
            # of the extract (review, 2026-10-20)
            with _ACTIVE_EXCEL_LOCK:
                alone = alone and _STARTING["ever"] == mine
            left = _end_unanswered(before, alone) if com is not None else ""
            raise _excel_failed(e, path, cancels, left=left) from e
        finally:
            with _ACTIVE_EXCEL_LOCK:
                _STARTING["now"] -= 1
        started = _new_excel(before) if before is not None else None
        pid = _tie_excel(app)
        answered, created = pid is not None, None
        if pid is None and started is not None:
            # It refuses every call, app.Hwnd too: it was neither tied nor
            # registered for Cancel, and a refused Quit left it running - a
            # hidden Excel sat on its license agreement for half an hour
            # (review, 2026-10-20). Found as the one Excel that started.
            pid, created = started
            tie_to_this_process(pid)
        if com is not None and pid is not None:
            held = _hold_excel(pid, created)
        _register_excel_pid(pid)             # so the Ledger's Cancel can kill it
        _stop_if_cancelled(path, cancels)    # ...pressed before the PID was there
        watchdog = _watchdog(app, timeout, stopped, pid) if com is not None else None
        # refusing, and not found to be ended: its refused Quit leaves it
        left = _LEFT_RUNNING if com is not None and held is None else ""
        for attr, val in (("Visible", False), ("DisplayAlerts", False),
                          ("AskToUpdateLinks", False), ("EnableEvents", False),
                          ("ScreenUpdating", False), ("AutomationSecurity", 3)):
            try:
                _ask(lambda a=attr, v=val: _put(app, a, v), app, attr, stopped=stopped)
            except _Refused as e:
                # still refused: macros not turned off, among the rest -
                # nothing is opened in it
                raise _excel_failed(e.why, path, cancels, True, left) from e.why
            except Exception as e:
                _drop_frames(e, keep=handling)  # it held Excel (_drop_frames)
        _stop_if_cancelled(path, cancels)
        try:
            # A password the workbook does not have, so a protected file fails
            # here instead of prompting. A password .xls did not: Excel showed
            # its Password dialog anyway (measured), so kind_of refuses every
            # password-to-open file above, before Excel starts. A refused
            # call is asked again (_ask): it never reached the workbook.
            books = _ask(lambda: app.Workbooks, app, "Workbooks", stopped=stopped)
            book = _ask(lambda: books.Open(str(path), UpdateLinks=0, ReadOnly=True,
                                           IgnoreReadOnlyRecommended=True, AddToMru=False,
                                           Notify=False, Password="council-no-password",
                                           WriteResPassword="council-no-password"),
                        books, "Open", stopped=stopped)
        except _Refused as e:
            if stopped:
                raise classify_open_failure(e.why, path, timeout, stopped) from e.why
            raise _excel_failed(e.why, path, cancels, True, left) from e.why
        except Exception as e:
            raise classify_open_failure(e, path, timeout, stopped) from e
        if watchdog is not None:
            watchdog.cancel()          # opened: copying a big workbook out may take a while
        out = ExcelBook()
        # The pictures come from a copy Excel saves as .xlsx, never through
        # the clipboard, which took over the user's: what they copied while a
        # workbook was read was overwritten, and only plain text was put back
        # (user, 2026-10-22). Only when Excel will not save a copy is the
        # clipboard used - carefully, as the knowledge graph's rebuild does:
        # not when it holds anything but text, stopped when the user copies.
        copied: Optional[Dict[Tuple[str, str], List[Tuple[bytes, str]]]] = None
        if want_pictures:
            copy_dir = tempfile.mkdtemp(prefix="council-xl-")
            copied, copy_why = _pictures_from_a_copy(book, path, copy_dir)
            if copied is None and not careful:
                careful = True
                seq = _clip_seq()
                clip = _clip_text()
                safe, clip_why = clipboard_safe()
                if safe and seq is None:
                    clip_why = "the clipboard could not be checked"
                if clip_why:
                    clip_why = f"{copy_why}, and {clip_why}"
        # Each call the copy cannot do without is asked again while Excel
        # refuses it (_ask): its license agreement came up while a workbook
        # was copied out (~0.7-1.7 s after it starts, measured here), and the
        # raw 'Call was rejected by callee' ended the read as 'other', each
        # workbook after it starting an Excel of its own (review, 2026-10-20).
        # Still refused, it is said so once per extract (the except below).
        for ws in _ask(lambda: book.Worksheets, book, "Worksheets"):
            name = str(_ask(lambda w=ws: w.Name, ws, "Name"))
            cells: Cells = {}
            used = _ask(lambda w=ws: w.UsedRange, ws, "UsedRange")
            r0 = int(_ask(lambda u=used: u.Row, used, "Row"))
            c0 = int(_ask(lambda u=used: u.Column, used, "Column"))
            data = _ask(lambda u=used: u.Value, used, "Value")
            if not isinstance(data, tuple):
                data = ((data,),)
            elif data and not isinstance(data[0], tuple):
                data = (data,)
            for i, row in enumerate(data):
                for j, v in enumerate(row):
                    v = _plain(v)
                    if v is not None:
                        cells[(r0 + i, c0 + j)] = v
            _fill_merged(ws, used, cells)     # a merged label, in every cell it covers
            out.sheets.append((name, cells))
            n = 0
            shapes = _ask(lambda w=ws: w.Shapes, ws, "Shapes")
            for k in range(1, int(_ask(lambda s=shapes: s.Count, shapes, "Count")) + 1):
                sh = _ask(lambda s=shapes, i=k: s.Item(i), shapes, "Item")
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
                elif typ in (MSO_PICTURE, MSO_LINKED_PICTURE) and copied is not None:
                    got = copied.get((name, anchor)) or []
                    if got:
                        data, ext = got.pop(0)
                        out.pictures.append(Picture(name, anchor, data, ext))
                    else:
                        # a linked picture, not saved in the file
                        out.pictures.append(Picture(name, anchor, b""))
                        out.notes.append(f"{path.name} › {name}!{anchor}: a picture could not "
                                         "be read (a linked picture, not saved in the "
                                         "workbook)")
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
                    _COPY_WHY.why = ""
                    data_png = _copy_picture(sh)
                    used_clip = True
                    if careful:
                        seq = _clip_seq()
                    if data_png:
                        out.pictures.append(Picture(name, anchor, data_png))
                    elif getattr(_COPY_WHY, "why", "") == "busy":
                        # Listed with no data, so it is counted as not read
                        # (and read again later): it left the count and the
                        # Images sheet, and the note blamed the workbook
                        # (review, 2026-10-19). The rest are not tried: each
                        # would wait for the clipboard again.
                        clip_why = ("the clipboard was busy (another program held it "
                                    "open); read the workbook again later")
                        out.pictures.append(Picture(name, anchor, b""))
                        out.notes.append(f"{path.name} › {name}!{anchor}: a picture could not "
                                         f"be copied out: {clip_why}")
                    else:
                        out.notes.append(f"{path.name} › {name}!{anchor}: a picture could not "
                                         "be copied out (the workbook may forbid copying)")
        return out
    except Exception as e:
        _drop_frames(e, keep=handling)
        if book is None or isinstance(e, ExcelOpenError):
            raise                   # before it opened: said above
        # While the workbook was copied out (review, 2026-10-20):
        why = e.why if isinstance(e, _Refused) else _refusal(e)
        if why is not None:
            # still refused: said once per extract, as an Excel refusing
            # before the workbook opened is
            raise _excel_failed(why, path, cancels, True, left) from why
        if _gone(e):
            # closed, crashed or ended from outside: this workbook only -
            # the next one starts an Excel of its own (a Cancel meanwhile
            # is said as one)
            _stop_if_cancelled(path, cancels)
            gone = (_com_error_description(e) or repr(e)).rstrip(". ")[:200]
            raise ExcelOpenError(f"{path.name}: Excel stopped while the workbook was being "
                                 f"read ({gone}) — it may have closed or crashed on it",
                                 kind="unavailable") from e
        raise
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if book is not None:
            try:
                book.Close(SaveChanges=False)
            except Exception:
                pass
        quit_refused = False
        if app is not None:
            try:
                app.Quit()
            except Exception:
                quit_refused = True
        # every Excel object let go before CoUninitialize, not only the
        # workbook and Excel itself (see _drop_frames)
        book = app = books = ws = used = shapes = sh = ch = ax = sc = item = None
        if held is not None:
            # Quit refused ('Call was rejected by callee': its license
            # agreement waiting), or an Excel that never answered: ended,
            # not left running hidden (review, 2026-10-20)
            _let_go(held, end=quit_refused or not answered)
        _unregister_excel_pid(pid)
        if copy_dir:
            # after Excel let go of it (it held the copy open)
            shutil.rmtree(copy_dir, ignore_errors=True)
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


def _tie_excel(app) -> Optional[int]:
    """The hidden Excel `app` dies with this process (tie_to_this_process).
    Its PID (_excel_pid: asked again for a moment while Excel refuses
    calls), or None when its window would not say."""
    pid = _excel_pid(app)
    if pid is not None:
        tie_to_this_process(pid)
    return pid


# ── the Excel a read started, found by its process ──────────────────────
_KERNEL32 = None
_PROCESS_TERMINATE, _PROCESS_QUERY_LIMITED = 0x0001, 0x1000


def _k32():
    """kernel32 with this module's own argtypes (ctypes.windll's are shared
    with other modules)."""
    global _KERNEL32
    if _KERNEL32 is None:
        import ctypes
        from ctypes import wintypes as wt
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateToolhelp32Snapshot.restype = wt.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [wt.HANDLE, ctypes.c_void_p]
        k.OpenProcess.restype = wt.HANDLE
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.c_void_p] * 4
        k.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
        k.CloseHandle.argtypes = [wt.HANDLE]
        _KERNEL32 = k
    return _KERNEL32


def _created_at(handle) -> Optional[int]:
    """When the process behind `handle` started (a FILETIME), or None."""
    import ctypes
    t = [ctypes.c_ulonglong() for _ in range(4)]
    if _k32().GetProcessTimes(handle, *[ctypes.byref(x) for x in t]):
        return int(t[0].value)
    return None


def _processes_named(image: str) -> Optional[set]:
    """(pid, start time) of every process of image `image` ('EXCEL.EXE')
    running now - the start time tells a PID Windows gave to a new process
    from the one before - or None when they cannot be listed (not Windows)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes as wt

        class Entry(ctypes.Structure):                    # PROCESSENTRY32W
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                        ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                        ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
        k = _k32()
        snap = k.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return None
        pids = []
        try:
            e = Entry()
            e.dwSize = ctypes.sizeof(Entry)
            more = k.Process32FirstW(snap, ctypes.byref(e))
            while more:
                if e.szExeFile.lower() == image.lower():
                    pids.append(int(e.th32ProcessID))
                more = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
        out = set()
        for pid in pids:
            h = k.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
            try:
                out.add((pid, _created_at(h) if h else None))
            finally:
                if h:
                    k.CloseHandle(h)
        return out
    except Exception:                                       # noqa: BLE001
        return None


def _excel_processes() -> Optional[set]:
    return _processes_named("EXCEL.EXE")


_NTDLL = None


def _command_line(pid: int) -> Optional[str]:
    """Process `pid`'s command line (NtQueryInformationProcess's
    ProcessCommandLineInformation, Windows 8.1 on), or None."""
    global _NTDLL
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes as wt
        if _NTDLL is None:
            nt = ctypes.WinDLL("ntdll")
            nt.NtQueryInformationProcess.restype = ctypes.c_long
            nt.NtQueryInformationProcess.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wt.ULONG, ctypes.POINTER(wt.ULONG)]
            _NTDLL = nt

        class Text(ctypes.Structure):                      # UNICODE_STRING
            _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT),
                        ("Buffer", ctypes.c_void_p)]
        k = _k32()
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED, False, int(pid))
        if not h:
            return None
        try:
            size = wt.ULONG(0)
            _NTDLL.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if _NTDLL.NtQueryInformationProcess(h, 60, buf, size.value, ctypes.byref(size)):
                return None
            text = Text.from_buffer(buf)
            return ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
        finally:
            k.CloseHandle(h)
    except Exception:                                       # noqa: BLE001
        return None


def _for_automation(pid: int) -> bool:
    """EXCEL.EXE `pid` was started by COM for automation ('/automation
    -Embedding' on its command line): a DispatchEx's, never an Excel the
    user opened."""
    line = (_command_line(pid) or "").lower()
    return "/automation" in line and "-embedding" in line


def _new_excels(before: Optional[set]) -> Optional[List[Tuple[int, Optional[int]]]]:
    """(pid, start time) of each EXCEL.EXE running now that was not in
    `before` (_excel_processes, taken just before DispatchEx) and that COM
    started for automation (_for_automation) - none the user opened meanwhile,
    nor one another read of this process registered. None when they cannot
    be listed."""
    after = _excel_processes()
    if before is None or after is None:
        return None
    with _ACTIVE_EXCEL_LOCK:
        others = set(_ACTIVE_EXCEL_PIDS)
    return [p for p in after - before if p[0] not in others and _for_automation(p[0])]


def _new_excel(before: Optional[set]) -> Optional[Tuple[int, Optional[int]]]:
    """(pid, start time) of the one new EXCEL.EXE (_new_excels): the Excel
    DispatchEx started - a new process each time - whose window would not
    say (it refuses calls). None when there is none, or more than one
    (another started meanwhile: which is this read's cannot be told)."""
    new = _new_excels(before)
    return new[0] if new and len(new) == 1 else None


#: What a problem line adds when a failed DispatchEx may have left an Excel.
_LEFT_RUNNING = " (a hidden Excel it started may still be running: Task Manager can end it)"


def _end_unanswered(before: Optional[set], alone: bool) -> str:
    """DispatchEx failed - 'Server execution failed' after ~93 s here: COM
    started an EXCEL.EXE that never answered (its license agreement up), and
    it was left running hidden, never tied, registered or ended - one per
    extract (review, 2026-10-20). It is ended when it can be told for
    certain: the one new automation Excel (_new_excels), no other read of
    this process starting one meanwhile (`alone`). '' when none was left
    running, else what the problem line adds (_LEFT_RUNNING)."""
    new = _new_excels(before)
    if new == []:
        return ""                       # none started, or it has gone
    held = _hold_excel(*new[0]) if alone and new and len(new) == 1 else None
    if held is None:
        return _LEFT_RUNNING
    _let_go(held, end=True)
    return ""


def _hold_excel(pid: Optional[int], created: Optional[int] = None):
    """A handle to Excel process `pid`, taken while it is known to be the
    one this read started (started at `created`, when given): ending it
    through the handle can never end another process Windows has given the
    PID since (Cancel's kill, or the watchdog's, may have ended it first).
    None when it cannot be opened."""
    if os.name != "nt" or not pid:
        return None
    try:
        k = _k32()
        h = k.OpenProcess(_PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED, False, int(pid))
        if h and created is not None and _created_at(h) != created:
            k.CloseHandle(h)
            return None
        return h or None
    except Exception:                                       # noqa: BLE001
        return None


def _let_go(held, end: bool) -> None:
    """Close `held` (_hold_excel), first ending its process when `end`."""
    try:
        k = _k32()
        try:
            if end:
                k.TerminateProcess(held, 1)
        finally:
            k.CloseHandle(held)
    except Exception:                                       # noqa: BLE001
        pass


def _watchdog(app, timeout: float, stopped: list, pid: Optional[int] = None):
    """Stop THIS hidden Excel (by its process id `pid`, else the one its
    window names - never another Excel) if it has not finished within
    `timeout` seconds."""
    import threading
    if pid is None:
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


def _clipboard_dib(timeout: float = 5.0) -> Optional[bytes]:
    """The clipboard's bitmap (CF_DIB) through pywin32, which lets other
    threads run while Windows waits. PIL's ImageGrab holds the GIL through
    its own 0.5 s retry and Excel's rendering of the bitmap - the whole app
    froze meanwhile (review, 2026-10-19). The clipboard busy (another app,
    Win+V history) is retried for ``timeout`` seconds, sleeping between
    tries; None when it never opens or holds no bitmap."""
    import time
    import win32clipboard
    import win32con
    end = time.monotonic() + timeout
    while True:
        try:
            win32clipboard.OpenClipboard()
            break
        except Exception:
            if time.monotonic() > end:
                _COPY_WHY.why = "busy"
                return None
            time.sleep(0.05)
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB):
            return None
        return win32clipboard.GetClipboardData(win32con.CF_DIB)
    finally:
        win32clipboard.CloseClipboard()


#: Why the last _copy_picture on this thread gave nothing: 'busy' when the
#: clipboard could not be opened (another program held it open), else ''
#: (Excel refused: the workbook may forbid copying).
_COPY_WHY = threading.local()


def _clipboard_busy(timeout: float = 0.5) -> bool:
    """The clipboard cannot be opened now: another program holds it open."""
    import time
    try:
        import win32clipboard
    except Exception:
        return False
    end = time.monotonic() + timeout
    while True:
        try:
            win32clipboard.OpenClipboard()
        except Exception:
            if time.monotonic() > end:
                return True
            time.sleep(0.05)
            continue
        win32clipboard.CloseClipboard()
        return False


def _copy_picture(shape) -> Optional[bytes]:
    _COPY_WHY.why = ""
    try:
        from PIL import Image, ImageGrab
        try:
            shape.CopyPicture(Appearance=1, Format=2)     # as shown, bitmap
        except Exception:
            # Excel could not put it on the clipboard: another program
            # holding the clipboard open, or the workbook forbids copying
            if _clipboard_busy():
                _COPY_WHY.why = "busy"
            return None
        img = None
        try:
            dib = _clipboard_dib()
            if dib:
                from PIL import BmpImagePlugin
                img = BmpImagePlugin.DibImageFile(io.BytesIO(dib))
        except Exception:
            img = None
        if img is None and getattr(_COPY_WHY, "why", "") == "busy":
            # the clipboard never opened in 5 s: ImageGrab's own retry could
            # not do better, and it holds the GIL while it tries - the whole
            # app froze once more per workbook (review, 2026-10-19)
            return None
        if img is None:
            img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "PNG")
            return buf.getvalue()
    except Exception:
        pass
    return None
