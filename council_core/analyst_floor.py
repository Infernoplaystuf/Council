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

Each path is checked against an allow-list fixed at install time:

  READ-ONLY   the run's data folders; Python's own installation (sys.prefix,
              sys.base_prefix, site-packages, the standard library); the
              app's code folder
  READ-WRITE  the vault's data_out and .stats_cache; the run's own temporary
              folders (the code, call.json and report folder, and the
              working folder the child runs in)
  NEVER       the rest of the vault (even when it lies inside a read-only
              root such as the app folder), and everything not listed

The deepest root that holds a path decides (data_in is read-only although
the vault around it is not readable at all). Paths are resolved and compared
the way path_contain does it — realpath (symlinks, junctions, `..` and 8.3
short names), the \\\\?\\ prefix dropped, case folded — with os.path
functions captured when this module is imported, before any model code
exists; network shares and devices (\\\\server\\share, \\\\.\\PhysicalDrive0,
NUL, CON, ...) are refused from their spelling, without a lookup.

A refusal raises PermissionError at the call that tried it, with a plain
reason, and is RECORDED: council_core.analyst_child ends the run with that
reason as its error even when the code caught the exception, so swallowing a
refusal never turns it into a result.

What it does not see: file I/O done inside native code without an audit
event (a C library opening a file itself). The sandbox's import allowlist
and its contained helpers stay the guard there.
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
        for paths, level in ((never, NEVER), (read_only, READ),
                             (read_write, WRITE)):
            for p in paths or ():
                try:
                    s = _plain(p)
                    if not s or network_or_device(s):
                        continue
                    roots.append((canonical(s), level))
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
        if network_or_device(s):
            return (f"{verb} {shown} is not allowed: it is a network share or "
                    f"a device, and the sandbox uses local folders only")
        c, err = self._canonical(s)
        if err:
            return f"{verb} {shown} is not allowed: it cannot be resolved ({err})"
        if _NT and c.startswith("\\\\"):
            return (f"{verb} {shown} is not allowed: it leads to a network "
                    f"share or a device, and the sandbox uses local folders "
                    f"only")
        lvl = self.level(c)
        if lvl == WRITE or (lvl == READ and not write):
            return ""
        if lvl == READ:
            return (f"{verb} {shown} is not allowed: it is read-only in the "
                    f"sandbox, which writes only into data_out and the stats "
                    f"cache")
        return (f"{verb} {shown} is not allowed: it is outside the folders "
                f"the sandbox may use ({_ALLOWED_SHORT})")

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


def _on_import(floor: Floor, args: tuple) -> None:
    root = str(_arg(args, 0) or "").split(".")[0]
    why = floor.refused_imports.get(root)
    if why:
        floor.refuse(why, "the sandbox refused")


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
    "os.mkdir": _writes((0, "making the folder")),
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
           "python_roots", "NEVER", "READ", "WRITE"]
