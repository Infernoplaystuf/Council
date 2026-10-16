"""
council_core.analyst_floor — the analyst child's file fence, under the
sandbox's own checks.

The sandbox (vault_analyst.execute_pandas_code) keeps model-written code
inside the data folders with checks written in Python: an AST validator, a
guarded import and getattr, proxies for pandas / numpy / pathlib, and helpers
that contain every path they are given. Each of those is a check on one ROUTE
to a file, and a review of each round of fixes found another route (a helper
that handed back a real pathlib object, a dunder the validator did not know,
...). This module is the floor under all of them: whatever route the code
takes, the file operation itself is checked.

It is a PEP 578 audit hook (sys.addaudithook), installed by
council_core.analyst_child BEFORE any model code runs. It cannot be removed
once installed, and it sees the operation, not the code that asked for it:

  "open"                          every file open — read or write
  "os.listdir", "os.scandir"      every directory listing
  "os.remove", "os.rename",       every file-changing call (os.unlink raises
  "os.replace", "os.mkdir",       "os.remove", os.replace "os.rename"), and
  "os.rmdir", "os.chmod",         shutil's own events where it has them
  "os.utime", "os.truncate",
  "shutil.*"
  "sqlite3.connect"               SQLite opens its file in C (no "open")
  "os.link", "os.symlink",        refused outright: a link would make one
  "_winapi.CreateJunction"        place look like another
  "os.system", "os.exec",         refused outright: a program started from
  "os.spawn", "os.posix_spawn",   the run is a fresh process WITHOUT this
  "os.fork", "os.forkpty",        hook, which could then read and write any
  "subprocess.Popen",             file — the one route the rule below could
  "_winapi.CreateProcess",        not otherwise hold for. The layers above
  "os.startfile", "pty.spawn"     refuse `import subprocess` / `os` first;
                                  this keeps a regression there contained.

Each path is checked against an allow-list fixed at install time:

  READ-ONLY   the run's data folders; Python's own installation (sys.prefix,
              sys.base_prefix, site-packages, the standard library); the
              app's code folder; single files the sandbox's own helpers read
              (the SQL / Mongo bridge's connection registries)
  READ-WRITE  the vault's data_out and .stats_cache; the run's own temporary
              folders (the code, call.json and report folder, and the
              working folder the child runs in, which is also its temp
              folder); single files the helpers append to (the bridge's
              db_audit.log and its rotations)
  NEVER       the rest of the vault (even when it lies inside a read-only
              root such as the app folder), and everything not listed

The deepest root that holds a path decides (data_in is read-only although
the vault around it is not readable at all). Paths are resolved and compared
the way path_contain does it — realpath (symlinks, junctions, `..` and 8.3
short names), the \\\\?\\ prefix dropped, case folded — with os.path
functions captured when this module is imported, before any model code
exists. Devices (\\\\.\\PhysicalDrive0, NUL, CON, ...) are refused from their
spelling. A network share (\\\\server\\share) is refused from its spelling,
without a lookup, UNLESS a root is on that same share — a vault on a network
drive (nx_guard does the same): then a path on that share is resolved and
judged by the roots like any other, and a path on any other share is still
refused unresolved (resolving it would ask the network for the host).

Making a folder that already exists (os.mkdir, which Path.mkdir(exist_ok=
True) calls before it swallows FileExistsError) changes nothing and is
allowed wherever the folder is.

A refusal raises PermissionError at the call that tried it, with a plain
reason, and is RECORDED: council_core.analyst_child ends the run with that
reason as its error even when the code caught the exception, so swallowing a
refusal never turns it into a result. An import the run may not make
(refused_imports: matplotlib, whose start-up writes into the user's home) is
different: it touched no file, so it fails as a missing package would — a
ModuleNotFoundError with the plain reason, which the code (or a library's
optional-dependency probe) may catch — and is not recorded.

What it does not see:
  - os.stat / os.path.exists / os.path.getsize / os.path.isdir and the
    other stat calls: CPython raises no audit event for them, so code can
    ask whether a guessed path exists and how big it is — metadata, not
    content. Listing a folder (the probe that finds names) is refused.
  - file I/O done inside native code without an audit event (a C library
    opening a file itself). The sandbox's import allowlist and its contained
    helpers stay the guard there.
"""
from __future__ import annotations

import _thread
import os
import sys
from typing import Any, Iterable, List, Optional, Sequence, Tuple

# Captured at import, before any model code runs: what the floor resolves
# and compares paths with. Nothing model code does to the os / os.path
# module attributes afterwards changes these.
_REALPATH = os.path.realpath
_NORMCASE = os.path.normcase
_NORMPATH = os.path.normpath
_ISDIR = os.path.isdir
_SPLITDRIVE = os.path.splitdrive
_BASENAME = os.path.basename
_FSPATH = os.fspath
_FSDECODE = os.fsdecode
_SEP = os.sep
_NT = os.name == "nt"
_GET_IDENT = _thread.get_ident
_ADD_HOOK = sys.addaudithook
_WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT
                | os.O_TRUNC)

_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"
_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                          | {f"COM{i}" for i in range(1, 10)}
                          | {f"LPT{i}" for i in range(1, 10)})

#: Access levels, in the order a tie at the same root is broken (the more
#: permissive explicit grant wins over NEVER at exactly the same folder).
NEVER, READ, WRITE = 0, 1, 2

#: What the run is told when the floor refuses. One plain sentence.
_ALLOWED_SHORT = ("it reads only the data folders and Python's own "
                  "libraries, and writes only into data_out and the stats "
                  "cache")


class Refused(PermissionError):
    """The file fence refused an operation."""


def _plain(value: Any) -> Optional[str]:
    """``value`` as a plain path string, converted ONCE; None for a file
    descriptor (already opened, so already checked) or no path at all."""
    if value is None or isinstance(value, (int, bool)):
        return None
    if isinstance(value, str):
        return "".join((value,))          # an exact str, whatever the subclass
    if isinstance(value, (bytes, bytearray)):
        return _FSDECODE(bytes(value))
    v = _FSPATH(value)                     # TypeError for a non-path
    return _plain(v)


def network_or_device(s: str) -> bool:
    """A UNC share (\\\\server\\share, \\\\?\\UNC\\...) or a device
    (\\\\.\\PhysicalDrive0, NUL, CON), read from the spelling alone — no
    lookup (path_contain.network_or_device, with captured functions)."""
    if not _NT:
        return False
    drive = _SPLITDRIVE(s)[0]
    if drive.startswith("\\\\") or drive.startswith("//"):
        # \\?\C: is a local drive spelled long; anything else after \\ is a
        # share or a device.
        return not (len(drive) == 6 and drive[:4] in ("\\\\?\\", "//?/")
                    and drive[5] == ":")
    base = _BASENAME(s.rstrip("\\/")).split(".")[0].strip().upper()
    return base in _DEVICE_NAMES


def unc_share(s: str) -> Optional[str]:
    """The share a UNC spelling names — ``\\\\server\\share``, case-folded,
    from \\\\server\\share\\..., //server/share/... or \\\\?\\UNC\\server\\
    share\\... — read from the spelling alone; None for a local path or a
    device (\\\\.\\PhysicalDrive0, \\\\?\\GLOBALROOT\\..., NUL)."""
    if not _NT:
        return None
    t = s.replace("/", "\\")
    head = t[:8].upper()
    if head in ("\\\\?\\UNC\\", "\\\\.\\UNC\\"):
        t = "\\\\" + t[8:]
    elif t[:4] in ("\\\\?\\", "\\\\.\\"):
        return None                         # a device, or \\?\C: (local)
    if not t.startswith("\\\\"):
        return None
    drive = _SPLITDRIVE(t)[0]
    if drive.count("\\") < 3 or drive.endswith("\\"):
        return None                         # \\server alone: not a share
    base = _BASENAME(t.rstrip("\\/")).split(".")[0].strip().upper()
    if base in _DEVICE_NAMES:
        return None
    return _NORMCASE(drive)


def canonical(s: str) -> str:
    """``s`` as one comparable string: absolute, symlinks and junctions
    followed, 8.3 short names expanded, the \\\\?\\ prefix realpath keeps on
    a long result dropped, case folded on Windows (path_contain.canonical,
    with captured functions). Raises OSError / ValueError for what is not a
    usable path."""
    real = _REALPATH(s)
    if _NT:
        if real.startswith(_UNC_PREFIX):
            real = "\\\\" + real[len(_UNC_PREFIX):]
        elif real.startswith(_PREFIX):
            real = real[len(_PREFIX):]
    return _NORMCASE(real)


def _inside(c: str, root: str) -> bool:
    if c == root:
        return True
    return c.startswith(root if root.endswith(_SEP) else root + _SEP)


def python_roots() -> List[str]:
    """Python's own installation: the prefixes, the standard library and
    site-packages (the user's too, when it exists) — where every import a
    library makes while the code runs comes from."""
    import site
    import sysconfig
    out: List[str] = []
    for p in (sys.prefix, sys.base_prefix, sys.exec_prefix,
              sys.base_exec_prefix):
        if p:
            out.append(p)
    try:
        paths = sysconfig.get_paths()
        for key in ("stdlib", "platstdlib", "purelib", "platlib"):
            if paths.get(key):
                out.append(paths[key])
    except Exception:                                     # noqa: BLE001
        pass
    try:
        out.extend(site.getsitepackages())
    except Exception:                                     # noqa: BLE001
        pass
    try:
        user = site.getusersitepackages()
        if user and os.path.isdir(user):
            out.append(user)
    except Exception:                                     # noqa: BLE001
        pass
    return out


class Floor:
    """The allow-list and the audit hook. Build it, then install()."""

    def __init__(self, read_only: Iterable[Any] = (),
                 read_write: Iterable[Any] = (),
                 never: Iterable[Any] = (),
                 refused_imports: Optional[dict] = None) -> None:
        roots: List[Tuple[str, int]] = []
        #: The network shares roots are on (a vault on a network drive):
        #: a path on one of THESE shares is resolved and judged by the roots;
        #: any other network path is refused from its spelling.
        self.net_shares: set = set()
        for paths, level in ((never, NEVER), (read_only, READ),
                             (read_write, WRITE)):
            for p in paths or ():
                try:
                    s = _plain(p)
                    if not s:
                        continue
                    share = unc_share(s)
                    if network_or_device(s) and share is None:
                        continue                # a device is never a root
                    c = canonical(s)
                    for sh in (share, unc_share(c)):
                        if sh:
                            self.net_shares.add(sh)
                    roots.append((c, level))
                except (OSError, ValueError, TypeError):
                    continue
        # Deepest first; at the same folder the higher grant first.
        roots.sort(key=lambda r: (-len(r[0]), -r[1]))
        self.roots: Tuple[Tuple[str, int], ...] = tuple(roots)
        self.refusals: List[str] = []
        self._busy: set = set()
        self.installed = False
        #: input string -> (canonical form, "") or ("", error text). The
        #: decision is a pure function of the resolved real path, and realpath
        #: dominates the per-open cost (~200 us on Windows: a lookup per path
        #: component), so a run that opens the same file many times — the
        #: per-file stats helpers over a folder — pays it once. Bounded; a run
        #: never sees enough distinct paths to need eviction finer than this.
        self._canon: dict = {}
        #: {top-level module name: why it may not be loaded while the code
        #: runs} — for a package whose start-up reaches outside the
        #: allow-list (matplotlib makes and reads its config folder in the
        #: user's home), so the run is told why in words, not with a path.
        self.refused_imports = dict(refused_imports or {})

    # ---- the rule -----------------------------------------------------------
    def _canonical(self, s: str) -> Tuple[str, str]:
        """(canonical form, "") or ("", error) for the raw string ``s``,
        memoised. realpath is the per-open cost; the answer only depends on
        the resolved path, so the same string resolves once."""
        hit = self._canon.get(s)
        if hit is not None:
            return hit
        try:
            out = (canonical(s), "")
        except (OSError, ValueError) as exc:
            out = ("", str(exc))
        if len(self._canon) >= 8192:
            self._canon.clear()
        self._canon[s] = out
        return out

    def level(self, c: str) -> int:
        """The access the deepest root holding canonical path ``c`` grants;
        NEVER when none does."""
        for root, lvl in self.roots:
            if _inside(c, root):
                return lvl
        return NEVER

    def why_not(self, value: Any, write: bool, verb: str) -> str:
        """"" when the run may ``verb`` the path ``value`` (writing when
        ``write``), else the plain reason it may not."""
        try:
            s = _plain(value)
        except TypeError:
            return (f"{verb} {value!r} is not allowed: it is not a path the "
                    f"sandbox can check")
        if s is None:
            return ""
        shown = s if len(s) <= 300 else s[:300] + "..."
        if not s.strip():
            return f"{verb} an empty path is not allowed"
        if network_or_device(s) and unc_share(s) not in self.net_shares:
            # Refused from the spelling: resolving it would ask the network
            # for the host first. A share a root is on is resolved below.
            return (f"{verb} {shown} is not allowed: it is a network share or "
                    f"a device, and {self._net_words()}")
        c, err = self._canonical(s)
        if err:
            return f"{verb} {shown} is not allowed: it cannot be resolved ({err})"
        if (_NT and c.startswith("\\\\")
                and unc_share(c) not in self.net_shares):
            return (f"{verb} {shown} is not allowed: it leads to a network "
                    f"share or a device, and {self._net_words()}")
        lvl = self.level(c)
        if lvl == WRITE or (lvl == READ and not write):
            return ""
        if lvl == READ:
            return (f"{verb} {shown} is not allowed: it is read-only in the "
                    f"sandbox, which writes only into data_out and the stats "
                    f"cache")
        return (f"{verb} {shown} is not allowed: it is outside the folders "
                f"the sandbox may use ({_ALLOWED_SHORT})")

    def _net_words(self) -> str:
        if self.net_shares:
            return ("the sandbox uses only the share its own folders are on")
        return "the sandbox uses local folders only"

    def refuse(self, why: str, head: str = "the sandbox's file fence "
                                          "refused") -> None:
        msg = f"{head}: {why}"
        self.refusals.append(msg)
        raise Refused(msg)

    def check(self, value: Any, write: bool, verb: str) -> None:
        why = self.why_not(value, write, verb)
        if why:
            self.refuse(why)

    # ---- the hook -----------------------------------------------------------
    def audit(self, event: str, args: tuple) -> None:
        handler = _EVENTS.get(event)
        if handler is None:
            return
        me = _GET_IDENT()
        if me in self._busy:
            return                          # the floor's own work
        self._busy.add(me)
        try:
            handler(self, args)
        finally:
            self._busy.discard(me)

    def install(self) -> "Floor":
        """Add the hook. It stays for the life of the process."""
        if not self.installed:
            _ADD_HOOK(self.audit)
            self.installed = True
        return self

    @property
    def first_refusal(self) -> str:
        return self.refusals[0] if self.refusals else ""


# ---- what each event means ---------------------------------------------------
def _arg(args: tuple, i: int) -> Any:
    return args[i] if len(args) > i else None


def _on_open(floor: Floor, args: tuple) -> None:
    path, mode, flags = (tuple(args) + (None, None, None))[:3]
    if isinstance(mode, str):
        writing = any(ch in mode for ch in "wax+")
    else:
        writing = bool((flags or 0) & _WRITE_FLAGS)
    floor.check(path, writing, "writing" if writing else "reading")


def _on_list(floor: Floor, args: tuple) -> None:
    path = _arg(args, 0)
    floor.check("." if path is None else path, False, "listing")


def _writes(*spec: Tuple[int, str]):
    def handler(floor: Floor, args: tuple) -> None:
        for i, verb in spec:
            value = _arg(args, i)
            if value is not None:
                floor.check(value, True, verb)
    return handler


def _copies(src: int, dst: int):
    def handler(floor: Floor, args: tuple) -> None:
        if _arg(args, src) is not None:
            floor.check(args[src], False, "copying")
        if _arg(args, dst) is not None:
            floor.check(args[dst], True, "copying onto")
    return handler


def _never(what: str):
    def handler(floor: Floor, args: tuple) -> None:
        floor.refuse(f"{what} is not allowed in the sandbox")
    return handler


def _on_mkdir(floor: Floor, args: tuple) -> None:
    """Making a folder: refused outside the writable roots — unless it
    already exists, when os.mkdir changes nothing (it fails with
    FileExistsError, which Path.mkdir(exist_ok=True) swallows; the SQL /
    Mongo bridge's audit append makes the vault folder that way). A network
    or device spelling is not asked about."""
    path = _arg(args, 0)
    why = floor.why_not(path, True, "making the folder")
    if not why:
        return
    try:
        s = _plain(path)
    except TypeError:
        s = None
    if s and not network_or_device(s) and _ISDIR(s):
        return
    floor.refuse(why)


def _on_import(floor: Floor, args: tuple) -> None:
    """An import the run may not make fails as a MISSING package would: a
    ModuleNotFoundError with the plain reason, which the code — or a
    library's optional-dependency probe (pd.show_versions, pandas' plotting
    backend) — may catch. Not recorded: no file was touched, so it does not
    end a run that caught it (2026-10-16 review: a df.plot() made in passing,
    or pd.show_versions(), failed runs that had their answer)."""
    root = str(_arg(args, 0) or "").split(".")[0]
    why = floor.refused_imports.get(root)
    if why:
        raise ModuleNotFoundError(f"the sandbox refused: {why}", name=root)


def _on_sqlite(floor: Floor, args: tuple) -> None:
    db = _arg(args, 0)
    try:
        text = _plain(db)
    except TypeError:
        text = None
    if text is None or text in ("", ":memory:"):
        return
    if text.lower().startswith("file:"):
        body, _q, query = text[5:].partition("?")
        if body in ("", ":memory:") or "mode=memory" in query:
            return
        read_only = "mode=ro" in query or "immutable=1" in query
        floor.check(body, not read_only,
                    "reading" if read_only else "opening the database")
        return
    floor.check(text, True, "opening the database")


_EVENTS = {
    "open": _on_open,
    "os.listdir": _on_list,
    "os.scandir": _on_list,
    "os.remove": _writes((0, "deleting")),
    "os.rmdir": _writes((0, "deleting")),
    "os.mkdir": _on_mkdir,
    "os.rename": _writes((0, "moving"), (1, "moving onto")),
    "os.replace": _writes((0, "moving"), (1, "moving onto")),
    "os.chmod": _writes((0, "changing")),
    "os.chown": _writes((0, "changing")),
    "os.utime": _writes((0, "changing")),
    "os.truncate": _writes((0, "changing")),
    "os.chflags": _writes((0, "changing")),
    "os.lchflags": _writes((0, "changing")),
    "os.setxattr": _writes((0, "changing")),
    "os.removexattr": _writes((0, "changing")),
    "os.link": _never("making a link"),
    "os.symlink": _never("making a link"),
    "_winapi.CreateJunction": _never("making a link"),
    # A started program is a new process without this hook: it could reach
    # any file. Refused outright (the layers above refuse the imports that
    # reach these first; this holds if they ever regress).
    "os.system": _never("starting a program"),
    "os.exec": _never("starting a program"),
    "os.spawn": _never("starting a program"),
    "os.posix_spawn": _never("starting a program"),
    "os.fork": _never("starting a program"),
    "os.forkpty": _never("starting a program"),
    "os.startfile": _never("starting a program"),
    "os.startfile/2": _never("starting a program"),
    "subprocess.Popen": _never("starting a program"),
    "_winapi.CreateProcess": _never("starting a program"),
    "pty.spawn": _never("starting a program"),
    "shutil.rmtree": _writes((0, "deleting")),
    "shutil.move": _writes((0, "moving"), (1, "moving onto")),
    "shutil.copyfile": _copies(0, 1),
    "shutil.copytree": _copies(0, 1),
    "shutil.copymode": _writes((1, "changing")),
    "shutil.copystat": _writes((1, "changing")),
    "shutil.chown": _writes((0, "changing")),
    "shutil.make_archive": _writes((0, "writing")),
    "shutil.unpack_archive": _copies(0, 1),
    "sqlite3.connect": _on_sqlite,
    "import": _on_import,
}


def install(read_only: Iterable[Any] = (), read_write: Iterable[Any] = (),
            never: Iterable[Any] = (),
            refused_imports: Optional[dict] = None) -> Floor:
    """Build the floor for these roots and install its audit hook."""
    return Floor(read_only, read_write, never, refused_imports).install()


__all__ = ["Floor", "Refused", "install", "canonical", "network_or_device",
           "unc_share", "python_roots", "NEVER", "READ", "WRITE"]
