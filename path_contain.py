"""
path_contain.py — is this path inside that folder, asked the one way that
holds on Windows.

Pure stdlib, and imported by BOTH environments (the app, and nx_guard in the
DREAM3D-NX env), so a containment check means the same thing wherever it is
made.

Why a module for one question
-----------------------------
Every containment check here compares two paths, and both must be read the
same way or the answer is wrong in one direction or the other:

  * 8.3 short names. C:\\Users\\X\\APPDAT~1 and C:\\Users\\X\\AppData are one
    folder. Path.resolve() expands the short name of a component that exists,
    so the target and the root must BOTH be resolved.
  * The \\\\?\\ prefix. realpath keeps it when the long-name result is too
    long to use without it (260 characters, LongPathsEnabled=0). Measured: a
    vault reached through its short name, C:\\...\\CONTAI~1\\LONGVA~1, resolved
    to the 250-character long name for data_out but to \\\\?\\C:\\...\\x.py for a
    file in it — two spellings of one place, so DataIndex.safe_write_path
    refused every write under that vault as "outside write_root".
  * Symlinks and junctions. Followed, on both sides: a junction inside the
    output area that points at the vault root is the vault root, not the
    output area.
  * Case. Windows paths compare case-blind.

A string prefix test on the canonical forms is used, not Path.relative_to on
unresolved paths: "C:\\vault\\data_out_old" is not inside "C:\\vault\\data_out".

Comparing is not asking
-----------------------
The canonical form is for COMPARING only. It drops the \\\\?\\ prefix, and a
long name past 260 characters cannot be used without it (LongPathsEnabled=0,
the Windows default): os.path.lexists() of the 318-character canonical form
of an existing file said False (measured), so nx_guard took a data_out file
the run did not make for a new one and let a model script overwrite it. A
question for the FILE SYSTEM (exists, is it a folder, what is in it) is asked
of resolved()'s second spelling, which keeps — or adds — the prefix.
"""
from __future__ import annotations

import os
from typing import Any, Tuple

_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"
# The longest path a call without \\?\ accepts (MAX_PATH less its NUL).
_MAX_PLAIN = 259


def _compare_form(real: str) -> str:
    if os.name == "nt":
        if real.startswith(_UNC_PREFIX):
            real = "\\\\" + real[len(_UNC_PREFIX):]
        elif real.startswith(_PREFIX):
            real = real[len(_PREFIX):]
    return os.path.normcase(real)


def _fs_form(real: str, room: int = 0) -> str:
    """``real`` (a realpath result: absolute, normalised, backslashes) in a
    spelling every file-system call accepts — with ``room`` characters more
    added to it (a folder a file is about to be made in). The prefix goes on
    only when the path is too long without it: with \\\\?\\ Windows stops
    normalising the name, so "x.dream3d." would name a different file from
    the one the unprefixed spelling a filter is handed opens."""
    if os.name != "nt" or real.startswith(_PREFIX) or \
            len(real) + room <= _MAX_PLAIN:
        return real
    if real.startswith("\\\\"):
        return _UNC_PREFIX + real[2:]
    return _PREFIX + real


def canonical(path: Any) -> str:
    """``path`` as one comparable string: absolute, symlinks and junctions
    followed, 8.3 short names expanded (realpath, for every component that
    exists), the \\\\?\\ prefix realpath keeps on a long result dropped, and
    case folded on Windows. For comparing only — see "Comparing is not
    asking". Raises OSError/ValueError/TypeError for what is not a path."""
    return _compare_form(os.path.realpath(os.fspath(path)))


def resolved(path: Any, room: int = 0) -> Tuple[str, str]:
    """(canonical form, file-system spelling) of ``path``, from ONE realpath
    call: the first to compare with other canonical forms, the second to ask
    the file system about (lexists, isdir, scandir) — it works whatever the
    length, and still does with ``room`` more characters joined to it (a
    name for a file to be made in the folder ``path``). Raises like
    canonical()."""
    real = os.path.realpath(os.fspath(path))
    return _compare_form(real), _fs_form(real, room)


def is_under(child: Any, parent: Any) -> bool:
    """True if ``child`` is ``parent`` or inside it. Both are canonicalised
    (see canonical); anything that is not a path is never inside anything."""
    try:
        c, p = canonical(child), canonical(parent)
    except (OSError, ValueError, TypeError):
        return False
    if c == p:
        return True
    return c.startswith(p if p.endswith(os.sep) else p + os.sep)


def network_or_device(path: Any) -> bool:
    """Is ``path`` a UNC share (\\\\server\\share, \\\\?\\UNC\\...) or a device
    (\\\\.\\PhysicalDrive0, NUL, CON)? Read from the spelling alone — no
    lookup: resolving \\\\some-host\\share asks the network for the host
    (1.3 s measured, and a name query on the LAN) before it can say no."""
    if os.name != "nt":
        return False
    try:
        s = os.fspath(path)
    except TypeError:
        return False
    if isinstance(s, bytes):
        s = os.fsdecode(s)
    drive = os.path.splitdrive(s)[0]
    if drive.startswith("\\\\") or drive.startswith("//"):
        # \\?\C: is a local drive spelled long; everything else after \\ is
        # a share or a device.
        return not (len(drive) == 6 and drive[:4] in ("\\\\?\\", "//?/")
                    and drive[5] == ":")
    # A bare device name, in any folder: C:\out\NUL is NUL.
    base = os.path.basename(s.rstrip("\\/")).split(".")[0].strip().upper()
    return base in _DEVICE_NAMES


_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                          | {f"COM{i}" for i in range(1, 10)}
                          | {f"LPT{i}" for i in range(1, 10)})


def has_stream_name(path: Any) -> bool:
    """Does ``path`` name an NTFS alternate data stream (``x.txt:hidden``)?

    A stream lives INSIDE a file: writing x.txt:s changes x.txt while
    exists('x.txt:s') says False, so an overwrite check that looks at the
    name is blind to it. No pipeline output needs one."""
    if os.name != "nt":
        return False
    try:
        s = os.fspath(path)
    except TypeError:
        return False
    if isinstance(s, bytes):
        s = os.fsdecode(s)
    # splitdrive takes C:, \\server\share and \\?\C: off the front; a colon
    # anywhere after that is a stream name (C:\a\b.txt:s, \\?\C:\a:b).
    return ":" in os.path.splitdrive(s)[1]
