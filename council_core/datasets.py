"""
council_core.datasets — Typhon's datasets: named sets of saved frames picked
by hand, from any run in any capture folder, kept so that a plugin (the
classifier first) can be handed the whole set at once.

WHAT A DATASET IS
A list of REFERENCES to frames where they were recorded, never copies (Andy,
2 November 2026: "Just do references"). Each entry says which file, which run
and frame number (from the name Typhon gives every frame,
<YYYYMMDD_HHMMSS[_n]>_frame_<number>), the folder it was in, the camera that
took it and the crop box shown when it was added, if any. Adding a frame
costs a line of JSON, not a copy of the picture.

WHERE IT LIVES
<vault>/datasets/<name>/dataset.json, one folder per dataset, so every
project and every plugin of this vault sees the same sets. The vault is
$COUNCIL_VAULT_ROOT, else ~/.council/vault (gui_projects.resolve_vault_root's
rule, restated so this module stands alone; a test holds the two together).
The plugin Typhon's launcher points it at its side vault, so its datasets are
its own.

NOTHING HERE TOUCHES A FRAME
The frames are only looked at (is the file there?). A frame moved or deleted
after it was added stays in the dataset and is reported as missing, so the
Datasets window can say so; removing it from the dataset is the user's call.
Deleting a dataset MOVES its folder to <vault>/datasets/.deleted/ (moving it
back restores it), as the classifier store does with a deleted classifier.

SAFE WITH TWO APPS AT ONCE
Each change takes the dataset's lock file (created exclusively, so a second
app waits), reads the file again, changes it and writes it atomically
(a temporary file, then a rename over the old one). A lock left behind by an
app that crashed is taken over after STALE_LOCK_S.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

#: The file's own version.
SCHEMA = 1
#: The folder under the vault.
FOLDER = "datasets"
#: The file inside a dataset's folder.
FILE = "dataset.json"
#: Where a deleted dataset's folder is moved.
DELETED = ".deleted"
#: The lock beside the file.
LOCK = ".lock"
#: How long a change waits for another app's lock.
LOCK_WAIT_S = 5.0
#: A lock older than this was left by an app that stopped.
STALE_LOCK_S = 30.0
#: The longest dataset name.
MAX_NAME = 64
#: Letters, digits, spaces, dots, dashes and underscores; not starting with
#: a dot (that would hide it, and .deleted is ours).
NAME_RE = re.compile(r"^[A-Za-z0-9_\- ][A-Za-z0-9_.\- ]*$")
#: Names Windows refuses for a folder.
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10))}
#: A frame Typhon saved: its run and frame number (capture_review.RUN_OF,
#: with the number kept).
FRAME_RE = re.compile(r"^(\d{8}_\d{6}(?:_\d+)?)_frame_(\d+)", re.IGNORECASE)


class DatasetError(RuntimeError):
    """A dataset change that could not be made; the text says why."""


# ---------------------------------------------------------------------------
# Where
# ---------------------------------------------------------------------------
def vault_root() -> Path:
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".council" / "vault"


def root() -> Path:
    """<vault>/datasets (not created until a dataset is)."""
    return vault_root() / FOLDER


def check_name(name: Any) -> str:
    """The name, stripped, or DatasetError saying what is wrong with it."""
    text = str(name or "").strip()
    if not text:
        raise DatasetError("type a name for the dataset")
    if len(text) > MAX_NAME:
        raise DatasetError(f"a dataset name is at most {MAX_NAME} characters")
    if not NAME_RE.match(text) or text.endswith("."):
        raise DatasetError(f"'{text}' cannot be a dataset name: use letters, "
                           f"digits, spaces, dots, dashes and underscores")
    if text.casefold() in RESERVED:
        raise DatasetError(f"'{text}' is a name Windows keeps for itself")
    return text


def _folder_of(name: str) -> Path:
    """The folder of an existing dataset whose name matches `name` in any
    case (Windows folders ignore case), else where a new one would go."""
    want = name.casefold()
    base = root()
    try:
        for entry in os.scandir(base):
            if entry.is_dir() and entry.name.casefold() == want:
                return Path(entry.path)
    except OSError:
        pass
    return base / name


def path_of(name: Any) -> Path:
    """The dataset.json of dataset `name` (it may not exist yet)."""
    return _folder_of(check_name(name)) / FILE


def exists(name: Any) -> bool:
    try:
        return path_of(name).is_file()
    except DatasetError:
        return False


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _empty(name: str) -> Dict[str, Any]:
    now = _now()
    return {"schema": SCHEMA, "name": name, "created": now, "updated": now,
            "frames": []}


def _read(path: Path) -> Dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise DatasetError(f"{path} cannot be read ({exc}); it is left as "
                           f"it is") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("frames"), list):
        raise DatasetError(f"{path} is not a dataset file; it is left as "
                           f"it is")
    if int(doc.get("schema") or 0) > SCHEMA:
        raise DatasetError(f"{path} was written by a newer Typhon; it is "
                           f"left as it is")
    doc["frames"] = [f for f in doc["frames"]
                     if isinstance(f, dict) and f.get("path")]
    return doc


def _write(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dataset_", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _locked(folder: Path) -> Iterator[None]:
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / LOCK
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue                      # just let go: try again
            if age > STALE_LOCK_S:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise DatasetError(f"another app is changing the dataset in "
                                   f"{folder}; try again in a moment")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def load(name: Any) -> Dict[str, Any]:
    """The dataset as stored: {"schema", "name", "created", "updated",
    "frames": [entry, ...]}."""
    path = path_of(name)
    try:
        return _read(path)
    except FileNotFoundError:
        raise DatasetError(f"there is no dataset named "
                           f"'{check_name(name)}'") from None


def names() -> List[str]:
    """Every dataset's name, in name order."""
    found = []
    try:
        for entry in os.scandir(root()):
            if (entry.is_dir() and not entry.name.startswith(".")
                    and (Path(entry.path) / FILE).is_file()):
                found.append(entry.name)
    except OSError:
        return []
    return sorted(found, key=str.casefold)


def summary(name: Any) -> Dict[str, Any]:
    """{"name", "frames", "missing", "runs", "updated"} for a list."""
    doc = load(name)
    frames = doc["frames"]
    missing = sum(1 for f in frames if not Path(f["path"]).is_file())
    runs = sorted({f.get("run") or "" for f in frames} - {""})
    return {"name": doc.get("name") or check_name(name),
            "frames": len(frames), "missing": missing, "runs": runs,
            "updated": doc.get("updated", "")}


def listing() -> List[Dict[str, Any]]:
    """summary() of every dataset; one that cannot be read says why."""
    out = []
    for n in names():
        try:
            out.append(summary(n))
        except DatasetError as exc:
            out.append({"name": n, "frames": 0, "missing": 0, "runs": [],
                        "updated": "", "problem": str(exc)})
    return out


def frames(name: Any) -> List[Dict[str, Any]]:
    """The dataset's entries, each with "exists": whether the file is
    still there."""
    out = []
    for f in load(name)["frames"]:
        entry = dict(f)
        entry["exists"] = Path(entry["path"]).is_file()
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------
def describe(path: Any) -> Dict[str, Any]:
    """What a frame's own name says: {"name", "run", "frame"} — run "" and
    frame None for a file Typhon did not name."""
    p = Path(str(path))
    found = FRAME_RE.match(p.name)
    return {"name": p.name, "run": found.group(1) if found else "",
            "frame": int(found.group(2)) if found else None}


def _key(path: Any) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _crop(value: Any) -> Optional[List[int]]:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        parts = [p for p in re.split(r"[,\s]+", value.strip()) if p]
    else:
        parts = list(value)
    try:
        box = [int(round(float(v))) for v in parts]
    except (TypeError, ValueError):
        return None
    return box if len(box) == 4 and box[2] > 0 and box[3] > 0 else None


def entry_for(path: Any, *, camera: str = "", crop: Any = None
              ) -> Dict[str, Any]:
    """The entry a frame gets when it is added."""
    p = Path(str(path)).expanduser()
    if not p.is_absolute():
        raise DatasetError(f"{p} is not a full path to a frame")
    p = Path(os.path.abspath(str(p)))
    if not p.is_file():
        raise DatasetError(f"{p} is not a file")
    entry = {"path": str(p), "folder": str(p.parent)}
    entry.update(describe(p))
    if camera:
        entry["camera"] = str(camera)
    box = _crop(crop)
    if box is not None:
        entry["crop"] = box
    entry["added"] = _now()
    return entry


def add_frames(name: Any, paths: Iterable[Any], *, camera: str = "",
               crop: Any = None) -> Dict[str, Any]:
    """Add frames to dataset `name`, making it if it is new. A frame
    already in it is not added twice; a path that is not a file refuses
    the whole call (nothing is added). {"summary", "added", "already",
    "frames"}."""
    name = check_name(name)
    wanted = [entry_for(p, camera=camera, crop=crop) for p in paths]
    if not wanted:
        raise DatasetError("no frames to add: show a saved frame first")
    path = _folder_of(name) / FILE
    with _locked(path.parent):
        try:
            doc = _read(path)
        except FileNotFoundError:
            doc = _empty(path.parent.name)
        have = {_key(f["path"]) for f in doc["frames"]}
        added = 0
        for entry in wanted:
            k = _key(entry["path"])
            if k in have:
                continue
            have.add(k)
            doc["frames"].append(entry)
            added += 1
        already = len(wanted) - added
        if added:
            doc["updated"] = _now()
            _write(path, doc)
        elif not path.is_file():
            _write(path, doc)
    total = len(doc["frames"])
    said = (f"{added} frame{'s' if added != 1 else ''} added to "
            f"'{doc['name']}'")
    if already:
        said += f" ({already} already in it)"
    said += f". It holds {total} frame{'s' if total != 1 else ''}."
    return {"summary": said, "added": added, "already": already,
            "frames": total, "name": doc["name"]}


def remove_frames(name: Any, paths: Iterable[Any]) -> Dict[str, Any]:
    """Take frames out of the dataset (the files are not touched)."""
    gone = {_key(p) for p in paths}
    path = path_of(name)
    with _locked(path.parent):
        try:
            doc = _read(path)
        except FileNotFoundError:
            raise DatasetError(f"there is no dataset named "
                               f"'{check_name(name)}'") from None
        before = len(doc["frames"])
        doc["frames"] = [f for f in doc["frames"]
                         if _key(f["path"]) not in gone]
        removed = before - len(doc["frames"])
        if removed:
            doc["updated"] = _now()
            _write(path, doc)
    total = len(doc["frames"])
    return {"summary": f"{removed} frame{'s' if removed != 1 else ''} taken "
                       f"out of '{doc['name']}' (the files are untouched). "
                       f"It holds {total}.",
            "removed": removed, "frames": total}


def merge(into: Any, others: Iterable[Any]) -> Dict[str, Any]:
    """Add every frame of `others` to `into` (made if new). The others are
    left as they are."""
    name = check_name(into)
    others = [o for o in others if check_name(o).casefold() != name.casefold()]
    sources = [load(o)["frames"] for o in others]    # each must exist
    path = _folder_of(name) / FILE
    with _locked(path.parent):
        try:
            doc = _read(path)
        except FileNotFoundError:
            doc = _empty(path.parent.name)
        have = {_key(f["path"]) for f in doc["frames"]}
        added = 0
        for source in sources:
            for f in source:
                k = _key(f["path"])
                if k in have:
                    continue
                have.add(k)
                doc["frames"].append(dict(f))
                added += 1
        doc["updated"] = _now()
        _write(path, doc)
    return {"summary": f"{added} frame{'s' if added != 1 else ''} added to "
                       f"'{doc['name']}'. It holds {len(doc['frames'])}.",
            "added": added, "frames": len(doc["frames"])}


def rename(name: Any, new: Any) -> Dict[str, Any]:
    """Give a dataset another name (its folder is renamed)."""
    old_path = path_of(name)
    if not old_path.is_file():
        raise DatasetError(f"there is no dataset named '{check_name(name)}'")
    new = check_name(new)
    target = _folder_of(new)
    same = target.name.casefold() == old_path.parent.name.casefold()
    if target.exists() and not same:
        raise DatasetError(f"a dataset named '{target.name}' already exists")
    with _locked(old_path.parent):
        doc = _read(old_path)
        doc["name"] = new
        doc["updated"] = _now()
        _write(old_path, doc)
    if same:
        tmp = old_path.parent.with_name(f".renaming_{os.getpid()}")
        os.replace(old_path.parent, tmp)
        os.replace(tmp, root() / new)
    else:
        os.replace(old_path.parent, root() / new)
    return {"summary": f"'{check_name(name)}' is now '{new}'.", "name": new}


def delete(name: Any) -> Dict[str, Any]:
    """Move the dataset's folder to <vault>/datasets/.deleted/ (moving it
    back restores it). The frames are not touched."""
    path = path_of(name)
    if not path.is_file():
        raise DatasetError(f"there is no dataset named '{check_name(name)}'")
    folder = path.parent
    aside = root() / DELETED
    aside.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = aside / f"{folder.name}_{stamp}"
    n = 2
    while target.exists():
        target = aside / f"{folder.name}_{stamp}_{n}"
        n += 1
    with _locked(folder):
        os.makedirs(target, exist_ok=False)
        os.replace(path, target / FILE)
    try:
        os.rmdir(folder)                  # empty now that the lock is gone
    except OSError:
        pass
    return {"summary": f"'{folder.name}' moved to {target} (the frames are "
                       f"untouched; move the folder back to restore it).",
            "moved_to": str(target)}
