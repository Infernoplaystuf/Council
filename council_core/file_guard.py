"""
council_core.file_guard — a file that stalls the Council is skipped, and
stays skipped until it changes.

User 2026-10-22: "have the council (connections, ledgers, and any other
relevant tabs) skip files that take too long to process and cause the app
to stall out. I've noticed some files have something inside them that
breaks the system."

Every bulk read (the Ledger's Extract, the Connections graph's rebuild, the
Vault's indexes) goes through ``read(path, fn, tab=...)``:

* A file on the skip list is not opened: ``Skipped`` is raised at once, with
  why it was put there. The list is one for the whole app
  (``<app dir>/skipped_files.json``), so a file that stalled the Ledger is
  skipped by the Connections graph too. A file is on it only as it was: once
  it changes (size or modified time) it is read again.

* A read that takes longer than ``time_limit()`` (COUNCIL_FILE_TIME_LIMIT_S,
  default 180 s - past Excel's own 2-minute open timeout, which says better
  why) is left behind: the file goes on the list, ``Skipped`` is raised, and
  the run goes on with the next file. A Python thread cannot be stopped, so
  the read runs on a helper thread of its own and is abandoned there; what
  it returns at last is thrown away.

* A file that stalls the whole app - one whose reader holds the interpreter
  so nothing else runs, or that crashes it - cannot be caught while it
  happens. So the file being read is written down first
  (``<app dir>/reading_now.json``) and crossed out after. When the app is
  closed normally its own notes go too; a note left by an earlier session is
  a file that session never finished: the next read of anything puts it on
  the list ("the Council stopped while reading it").

``clear()`` takes files off the list (the tabs' "Retry skipped files").
Nothing here deletes or changes a user's file: the list is the Council's own
state.
"""
from __future__ import annotations

import atexit
import contextlib
import contextvars
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

#: seconds one file may take, unless COUNCIL_FILE_TIME_LIMIT_S says otherwise
#: (0 = no limit). Past excel_open.OPEN_TIMEOUT_S (120 s), so a workbook
#: Excel cannot open is said as that.
DEFAULT_TIME_LIMIT_S = 180.0

#: MB a file's read may add to the app's memory, unless
#: COUNCIL_FILE_MEMORY_LIMIT_MB says otherwise (0 = no limit)
DEFAULT_MEMORY_LIMIT_MB = 4096
#: MB an Office file (a zip) may unpack to before it is opened at all, unless
#: COUNCIL_FILE_UNPACKED_LIMIT_MB says otherwise (0 = no limit): its sheets'
#: XML becomes ~10x as much memory in a reader
DEFAULT_UNPACKED_LIMIT_MB = 1024
_ZIPPED = (".xlsx", ".xlsm", ".xltx", ".xltm", ".pptx", ".potx", ".docx", ".dotx")

SKIP_NAME = "skipped_files.json"
READING_NAME = "reading_now.json"
LOCK_NAME = "file_guard.lock"

#: this process, in the notes of what is being read
SESSION = uuid.uuid4().hex

_lock = threading.RLock()

#: how deep this process holds the lock file (_locked is re-entered: recover
#: calls add)
_held = [0]


@contextlib.contextmanager
def _locked():
    """This process's lock, and the lock file's: the Ledger's helper
    processes (council_core.ledger_workers) write the same two JSON files,
    and a read-modify-write of one by two processes at once lost the other's
    entry. Best effort: a lock file that cannot be taken in ~10 s is gone
    on without."""
    with _lock:
        if _held[0]:                    # taken already, further up this thread
            _held[0] += 1
            try:
                yield
            finally:
                _held[0] -= 1
            return
        fh = None
        try:
            d = _state_dir()
            d.mkdir(parents=True, exist_ok=True)
            fh = open(d / LOCK_NAME, "a+b")
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except (OSError, ImportError):
            pass
        _held[0] = 1
        try:
            yield
        finally:
            _held[0] = 0
            if fh is not None:
                try:
                    if os.name == "nt":
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except (OSError, ImportError):
                    pass
                fh.close()
_recovered = False

#: threading.local()s a read needs from the thread that asked for it - the
#: deck links of a Ledger run (pptx_open), its Cancel scope (excel_open):
#: copied into the reading thread, and what it set copied back when the
#: read is done in time. Context variables (the Connections graph's image
#: reader) go across with the context.
_CARRIED: List[threading.local] = []


def carry(local: threading.local) -> None:
    """Have every guarded read see the asking thread's ``local``."""
    with _lock:
        if not any(x is local for x in _CARRIED):
            _CARRIED.append(local)


class Skipped(Exception):
    """This file was not read: it is on the skip list, or took too long now."""

    def __init__(self, path, reason: str):
        super().__init__(reason)
        self.path = str(path)
        self.reason = reason


@dataclass
class Entry:
    path: str
    reason: str
    tab: str
    when: float
    size: int
    mtime: float

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


# ── where ───────────────────────────────────────────────────────────────
def _state_dir() -> Path:
    from council_core import paths
    return paths.app_dir()


def skip_file() -> Path:
    return _state_dir() / SKIP_NAME


def reading_file() -> Path:
    return _state_dir() / READING_NAME


def time_limit() -> float:
    raw = os.environ.get("COUNCIL_FILE_TIME_LIMIT_S", "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_TIME_LIMIT_S
    except ValueError:
        return DEFAULT_TIME_LIMIT_S


def _env_mb(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return max(0, int(float(raw))) if raw else default
    except ValueError:
        return default


def memory_limit_mb() -> int:
    return _env_mb("COUNCIL_FILE_MEMORY_LIMIT_MB", DEFAULT_MEMORY_LIMIT_MB)


def unpacked_limit_mb() -> int:
    return _env_mb("COUNCIL_FILE_UNPACKED_LIMIT_MB", DEFAULT_UNPACKED_LIMIT_MB)


def process_memory_mb() -> Optional[float]:
    """This process's memory now (working set / resident), MB; None when it
    cannot be told."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            c = Counters()
            c.cb = ctypes.sizeof(c)
            k32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                                   wintypes.DWORD]
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return c.WorkingSetSize / 1e6
            return None
        with open("/proc/self/statm", "rb") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except Exception:                                     # noqa: BLE001
        return None


def unpacked_mb(path) -> Optional[float]:
    """What an Office file (a zip) unpacks to, MB, from its own directory -
    nothing is unpacked to tell. None for any other file, or a broken zip
    (its reader says why)."""
    if Path(str(path)).suffix.lower() not in _ZIPPED:
        return None
    try:
        import zipfile
        with zipfile.ZipFile(path) as z:
            return sum(i.file_size for i in z.infolist()) / 1e6
    except Exception:                                     # noqa: BLE001
        return None


def _key(path) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path).absolute())


def _stamp(path) -> tuple:
    try:
        st = os.stat(path)
        return int(st.st_size), float(st.st_mtime)
    except OSError:
        return -1, -1.0


# ── the two JSON files ──────────────────────────────────────────────────
def _load(path: Path) -> Dict[str, Any]:
    for wait in _RETRY:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except PermissionError:
            # Windows, while another process replaces it (the Ledger's
            # helpers): read again in a moment, never taken as empty at once
            time.sleep(wait)
        except (OSError, ValueError):
            return {}
    return {}


#: the waits between tries of a file another process holds for a moment
_RETRY = (0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.4)


def _save(path: Path, data: Dict[str, Any]) -> None:
    """Atomic: a temp file of this thread's own, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        for wait in _RETRY:
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                time.sleep(wait)        # Windows: another process is reading it
        else:
            os.replace(tmp, path)
        _cache["stamp"] = None                 # a coarse clock could hide the change
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ── the skip list ───────────────────────────────────────────────────────
_cache: Dict[str, Any] = {"stamp": None, "data": {}}


def _load_skips() -> Dict[str, Any]:
    """The skip list, read again only when its file changed: asked once per
    file read, by a Vault index of thousands."""
    f = skip_file()
    try:
        st = f.stat()
        stamp = (str(f), st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _lock:
        if _cache["stamp"] != stamp:
            _cache["stamp"], _cache["data"] = stamp, _load(f)
        return dict(_cache["data"])


def entries() -> List[Entry]:
    """Every file on the skip list, newest first."""
    data = _load_skips()
    out = []
    for key, d in data.items():
        try:
            out.append(Entry(key, str(d.get("reason", "")), str(d.get("tab", "")),
                             float(d.get("when", 0)), int(d.get("size", -1)),
                             float(d.get("mtime", -1))))
        except (TypeError, ValueError, AttributeError):
            continue
    return sorted(out, key=lambda e: -e.when)


def skipped(path) -> Optional[Entry]:
    """Why ``path`` is skipped - None when it is not on the list, or has
    changed since it was put there."""
    key = _key(path)
    for e in entries():
        if e.path == key:
            return e if (e.size, e.mtime) == _stamp(path) else None
    return None


def add(path, reason: str, tab: str = "") -> Entry:
    """Put ``path`` on the skip list, as it is now."""
    size, mtime = _stamp(path)
    e = Entry(_key(path), reason, tab, time.time(), size, mtime)
    with _locked():
        data = _load(skip_file())
        data[e.path] = {k: v for k, v in e.as_dict().items() if k != "path"}
        _save(skip_file(), data)
    return e


def clear(paths=None) -> int:
    """Take ``paths`` (all, when None) off the skip list. How many went."""
    with _locked():
        data = _load(skip_file())
        if paths is None:
            gone = len(data)
            data = {}
        else:
            keys = {_key(p) for p in paths}
            gone = sum(1 for k in list(data) if k in keys and data.pop(k, None) is not None)
        _save(skip_file(), data)
    return gone


# ── what is being read now ──────────────────────────────────────────────
def _note(token: str, path, tab: str) -> None:
    with _locked():
        data = _load(reading_file())
        data[token] = {"path": _key(path), "tab": tab, "session": SESSION,
                       "pid": os.getpid(), "started": time.time()}
        _save(reading_file(), data)


def _unnote(token: str) -> None:
    with _locked():
        data = _load(reading_file())
        if data.pop(token, None) is not None:
            _save(reading_file(), data)


def _forget_session() -> None:
    """A normal exit: what this session was reading did not stall it."""
    try:
        with _locked():
            data = _load(reading_file())
            mine = [k for k, v in data.items()
                    if isinstance(v, dict) and v.get("session") == SESSION]
            if mine:
                for k in mine:
                    data.pop(k, None)
                _save(reading_file(), data)
    except Exception:                                     # noqa: BLE001
        pass


atexit.register(_forget_session)


def forget_pid(pid: int) -> None:
    """Cross out what process ``pid`` was reading: a helper process the
    Ledger stopped itself (Cancel), whose reads did not stall anything
    (council_core.ledger_workers). Left in, the next read would put each of
    its files on the skip list."""
    try:
        with _locked():
            data = _load(reading_file())
            gone = [k for k, v in data.items()
                    if isinstance(v, dict) and v.get("pid") == pid]
            if gone:
                for k in gone:
                    data.pop(k, None)
                _save(reading_file(), data)
    except Exception:                                     # noqa: BLE001
        pass


def _alive(pid) -> bool:
    """Whether process ``pid`` (not this one) is still running."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # LIMITED_QUERY
            if not handle:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == 259                   # STILL_ACTIVE
        except Exception:                                 # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def recover() -> List[Entry]:
    """The files an earlier session was reading when it stopped - stalled
    or crashed - put on the skip list (once per process; read() calls it)."""
    global _recovered
    with _lock:
        if _recovered:
            return []
    with _locked():
        if _recovered:
            return []
        _recovered = True
        data = _load(reading_file())
        # another Council still running reads its own files: not stale
        stale = {k: v for k, v in data.items()
                 if not isinstance(v, dict) or (v.get("session") != SESSION
                                                and not _alive(v.get("pid")))}
        if not stale:
            return []
        for k in stale:
            data.pop(k, None)
        _save(reading_file(), data)
    out = []
    for v in stale.values():
        if isinstance(v, dict) and v.get("path"):
            out.append(add(v["path"], "the Council stopped while reading it (it stalled "
                                      "or crashed the app)", str(v.get("tab", ""))))
    return out


# ── the guarded read ────────────────────────────────────────────────────
def read(path, fn: Callable[[], Any], *, tab: str = "",
         limit: Optional[float] = None,
         should_stop: Optional[Callable[[], bool]] = None,
         note_after: float = 0.0) -> Any:
    """``fn()`` - the read of ``path`` - unless the file is on the skip
    list, or until it takes longer than ``limit`` seconds (``time_limit()``
    when None; 0 = none). Raises ``Skipped`` then; ``fn``'s own exceptions
    pass through. ``should_stop`` (a Cancel) is asked while it waits: the
    read is left behind then too, but the file is not put on the list.

    ``note_after``: write down what is being read only once it has taken
    that long (the Vault's thousands of small files: two writes each would
    cost more than the reads). 0 - the Ledger, the Connections graph - is
    before the read, so a file that crashes the app at once is caught too."""
    recover()
    on_list = skipped(path)
    if on_list is not None:
        raise Skipped(path, f"skipped: {on_list.reason} (Retry skipped files to read it again)")
    limit = time_limit() if limit is None else limit
    # an Office file that would unpack to more than the app can hold is not
    # opened at all (a 'zip bomb', or sheets with millions of formatted cells)
    cap_unpacked = unpacked_limit_mb()
    if cap_unpacked:
        size = unpacked_mb(path)
        if size is not None and size > cap_unpacked:
            reason = f"unpacks to {size:,.0f} MB, more than {cap_unpacked:,} MB"
            add(path, reason, tab)
            raise Skipped(path, f"skipped: {reason} - not opened")
    cap_memory = memory_limit_mb()
    start_mb = process_memory_mb() if cap_memory else None
    token = uuid.uuid4().hex
    noted = note_after <= 0
    if noted:
        _note(token, path, tab)
    box: Dict[str, Any] = {}
    context = contextvars.copy_context()
    with _lock:
        carried = list(_CARRIED)
    asked = [dict(getattr(local, "__dict__", {})) for local in carried]

    def run() -> None:
        for local, values in zip(carried, asked):
            for k, v in values.items():
                setattr(local, k, v)
        try:
            box["value"] = context.run(fn)
        except BaseException as exc:                      # noqa: BLE001
            box["error"] = exc
        finally:
            box["locals"] = [dict(getattr(local, "__dict__", {})) for local in carried]

    worker = threading.Thread(target=run, daemon=True,
                              name=f"file-read:{Path(str(path)).name[:40]}")
    started = time.monotonic()
    worker.start()
    try:
        while True:
            worker.join(0.05)
            if not worker.is_alive():
                break
            if should_stop is not None and should_stop():
                raise Skipped(path, "stopped: the run was cancelled while reading it")
            spent = time.monotonic() - started
            if not noted and spent > note_after:
                _note(token, path, tab)
                noted = True
            if limit and spent > limit:
                add(path, f"took longer than {limit:g} s to read", tab)
                raise Skipped(path, f"skipped: took longer than {limit:g} s to read - "
                                    "left behind; skipped from now on until it changes")
            if start_mb is not None:
                now_mb = process_memory_mb()
                if now_mb is not None and now_mb - start_mb > cap_memory:
                    reason = f"used more than {cap_memory:,} MB of memory to read"
                    add(path, reason, tab)
                    raise Skipped(path, f"skipped: {reason} - left behind; skipped from "
                                        "now on until it changes")
    finally:
        if noted:
            _unnote(token)
    for local, values in zip(carried, box.get("locals") or []):
        for k, v in values.items():
            setattr(local, k, v)
    if "error" in box:
        raise box["error"]
    return box.get("value")
