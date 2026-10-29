"""
council_core.build_files — the Build tab's files: list a folder, open a file
as text, save it back, make a new one, and run a Python file.

No Qt here, so every rule below is tested without a window.

OPENING
  * Text only. A file whose first 8 KB hold a NUL byte is binary and is not
    opened; nor is one over MAX_OPEN_BYTES.
  * The encoding is kept (UTF-8, UTF-8 with a BOM, else Windows-1252, else
    Latin-1) and so are the line endings: a CRLF file is edited as plain
    lines and written back CRLF, so saving a file you changed one line of
    does not rewrite every line of it in git.

SAVING NEVER LOSES SOMEONE ELSE'S CHANGE
  * The file is written to a temporary file beside it and moved over the old
    one (os.replace), so a crash mid-save leaves the old file whole.
  * The file's size and modified time when it was opened are kept. If either
    differs at Save, something else changed the file since (another editor,
    git, a model): Save refuses with ChangedOnDisk and the tab asks before
    overwriting it.
  * A new file is never written over an existing one.

LISTING
  One folder at a time (the tree opens folders as they are expanded), folders
  first, at most MAX_ENTRIES, without the folders nobody edits by hand
  (.git, __pycache__, virtual environments, node_modules, tool caches).

RUNNING
  run_file starts ``python -u file`` in the file's own folder, so its imports
  and relative paths work as they do from a terminal, and streams each output
  line to a callback. Stop kills it. It runs with the user's own permissions,
  as the IDE tab's Run does: the tab puts council_core.ide_trust's check in
  front of it. The sandbox comes with the Build tab's Run and Test step.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Union

PathLike = Union[str, Path]

MAX_OPEN_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 2000
SNIFF_BYTES = 8192

#: Folders the tree leaves out: version control, caches, environments.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".venv", "venv", "node_modules", ".idea", ".vs",
})


class FileProblem(Exception):
    """A file that cannot be opened, saved or made; the message says why."""


class ChangedOnDisk(FileProblem):
    """The file changed on disk since it was opened."""


@dataclass
class Entry:
    name: str
    path: Path
    is_dir: bool


@dataclass
class Listing:
    entries: List[Entry]
    truncated: bool = False
    error: str = ""


@dataclass
class OpenedFile:
    path: Path
    text: str              # with "\n" line endings, whatever the file had
    encoding: str
    newline: str           # what the file had: "\n" or "\r\n"
    stamp: tuple           # (size, mtime_ns) when read; see changed_on_disk


def _stamp(path: Path) -> tuple:
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)


def list_dir(folder: PathLike, limit: int = MAX_ENTRIES) -> Listing:
    """What the tree shows inside ``folder``: folders first, then files, each
    by name (case-insensitive)."""
    folder = Path(folder)
    dirs: List[Entry] = []
    files: List[Entry] = []
    truncated = False
    try:
        with os.scandir(folder) as it:
            for e in it:
                if len(dirs) + len(files) >= limit:
                    truncated = True
                    break
                try:
                    is_dir = e.is_dir()
                except OSError:
                    continue
                if is_dir:
                    if e.name not in SKIP_DIRS:
                        dirs.append(Entry(e.name, Path(e.path), True))
                else:
                    files.append(Entry(e.name, Path(e.path), False))
    except OSError as exc:
        return Listing([], error=f"{folder} could not be listed: {exc.strerror or exc}")
    key = lambda e: e.name.lower()                        # noqa: E731
    return Listing(sorted(dirs, key=key) + sorted(files, key=key), truncated)


def _decode(raw: bytes):
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8-sig"
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def open_text(path: PathLike) -> OpenedFile:
    """The file as text for the editor, or FileProblem saying why not."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise FileProblem(f"{path.name} could not be opened: {exc.strerror or exc}")
    if size > MAX_OPEN_BYTES:
        raise FileProblem(f"{path.name} is {size / 1048576:.1f} MB; the editor opens "
                          f"files up to {MAX_OPEN_BYTES // 1048576} MB")
    try:
        raw = path.read_bytes()
        stamp = _stamp(path)
    except OSError as exc:
        raise FileProblem(f"{path.name} could not be read: {exc.strerror or exc}")
    if b"\x00" in raw[:SNIFF_BYTES]:
        raise FileProblem(f"{path.name} is not a text file")
    text, encoding = _decode(raw)
    newline = "\r\n" if "\r\n" in text else "\n"
    text = text.replace("\r\n", "\n")
    return OpenedFile(path, text, encoding, newline, stamp)


def changed_on_disk(opened: OpenedFile) -> bool:
    """True when the file is no longer what was opened (or has gone)."""
    try:
        return _stamp(opened.path) != opened.stamp
    except OSError:
        return True


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_text(opened: OpenedFile, text: str, force: bool = False) -> OpenedFile:
    """Write ``text`` over the file ``opened`` came from, in its own encoding
    and line endings, and return the new OpenedFile (its stamp updated).
    ChangedOnDisk when the file changed since it was opened, unless
    ``force``."""
    path = opened.path
    if not force and path.exists() and changed_on_disk(opened):
        raise ChangedOnDisk(f"{path.name} was changed on disk after you opened it")
    body = text.replace("\r\n", "\n")
    if opened.newline != "\n":
        body = body.replace("\n", opened.newline)
    try:
        data = body.encode(opened.encoding)
    except UnicodeEncodeError:
        # A character the file's old encoding cannot hold (an emoji typed
        # into a Windows-1252 file): written as UTF-8 rather than refused.
        data, opened.encoding = body.encode("utf-8"), "utf-8"
    try:
        _write_atomic(path, data)
        stamp = _stamp(path)
    except OSError as exc:
        raise FileProblem(f"{path.name} could not be saved: {exc.strerror or exc}")
    return OpenedFile(path, text.replace("\r\n", "\n"), opened.encoding,
                      opened.newline, stamp)


def new_file(folder: PathLike, name: str) -> Path:
    """An empty file ``name`` inside ``folder`` (a relative path such as
    'pkg/util.py' makes its folders). Never replaces a file, and never lands
    outside ``folder``."""
    folder = Path(folder).resolve()
    name = (name or "").strip().replace("\\", "/")
    if not name or name.endswith("/"):
        raise FileProblem("give the new file a name")
    target = (folder / name).resolve()
    try:
        target.relative_to(folder)
    except ValueError:
        raise FileProblem(f"{name} is outside {folder.name}")
    if any(part in SKIP_DIRS for part in target.relative_to(folder).parts[:-1]):
        raise FileProblem(f"{name} is inside a folder the editor does not show")
    if target.exists():
        raise FileProblem(f"{target.name} already exists")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "x", encoding="utf-8"):
            pass
    except FileExistsError:
        raise FileProblem(f"{target.name} already exists")
    except OSError as exc:
        raise FileProblem(f"{target.name} could not be made: {exc.strerror or exc}")
    return target


@dataclass
class RunOutcome:
    returncode: Optional[int]
    seconds: float
    stopped: bool = False
    error: str = ""

    def summary(self, name: str) -> str:
        if self.error:
            return f"[{name} did not start: {self.error}]"
        if self.stopped:
            return f"[{name} stopped after {self.seconds:.1f} s]"
        return f"[{name} exited with code {self.returncode} after {self.seconds:.1f} s]"


def run_file(python: str, path: PathLike, on_line: Callable[[str], None],
             stop: Optional[threading.Event] = None,
             args: Optional[List[str]] = None) -> RunOutcome:
    """Run ``python -u path`` in the file's folder, calling ``on_line`` with
    each line of its output (stdout and stderr together, in order). Returns
    when it exits or ``stop`` is set (it is then killed)."""
    path = Path(path)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [python, "-u", str(path), *(args or [])], cwd=str(path.parent),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", env=env, creationflags=flags)
    except OSError as exc:
        return RunOutcome(None, 0.0, error=str(exc.strerror or exc))

    def drain() -> None:
        for line in proc.stdout:
            try:
                on_line(line.rstrip("\n"))
            except Exception:                             # noqa: BLE001
                pass
        proc.stdout.close()

    reader = threading.Thread(target=drain, name="build-run-out", daemon=True)
    reader.start()
    stopped = False
    while proc.poll() is None:
        if stop is not None and stop.wait(0.1):
            stopped = True
            proc.kill()
            break
        if stop is None:
            time.sleep(0.1)
    proc.wait()
    reader.join(timeout=5)
    return RunOutcome(proc.returncode, time.monotonic() - started, stopped)

