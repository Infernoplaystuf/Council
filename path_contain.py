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
"""
from __future__ import annotations

import os
from typing import Any

_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def canonical(path: Any) -> str:
    """``path`` as one comparable string: absolute, symlinks and junctions
    followed, 8.3 short names expanded (realpath, for every component that
    exists), the \\\\?\\ prefix realpath keeps on a long result dropped, and
    case folded on Windows. Raises OSError/ValueError/TypeError for what is
    not a path."""
    s = os.path.realpath(os.fspath(path))
    if os.name == "nt":
        if s.startswith(_UNC_PREFIX):
            s = "\\\\" + s[len(_UNC_PREFIX):]
        elif s.startswith(_PREFIX):
            s = s[len(_PREFIX):]
    return os.path.normcase(s)


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
