"""
frame_classes.py — make classes, mark frames, train a very simple classifier,
and KEEP the trained models: a library of them, every version of each, one
file that carries one to another PC, and a record of which version
classified which capture run.

The workflow a GUI binds to buttons:

    open_classifier(name)                         -> the saved classes
    add_class(name, new_class)                    -> classes + 1
    remove_class(name, selection)                 -> classes - 1 (only if unused)
    mark_frame(name, folder, frame, selection)    -> frame labelled with a class
    train(name)                                   -> a new model VERSION, and how good it is
    predict_frame(name, folder, frame)            -> that frame's likely class
    classify_folder(name, folder)                 -> every frame's likely class, RECORDED

The library — each returns the refreshed list too ("rows" for a listbox,
"table" for a table, "names"), so one button press updates the list:

    list_classifiers()                            -> every saved classifier
    save_as(name, new_name)                       -> a copy; never over another
    rename_classifier(name, new_name, current)    -> renamed; never over another
    delete_classifier(name, current)              -> MOVED to classifiers/.deleted
    list_versions(name)                           -> every version, newest first
    export_classifier(name, destination)          -> one .typhon-classifier.zip
    import_classifier(path, new_name)             -> a classifier from that file
    run_history(name, folder)                     -> which version classified what
    classified_with(folder)                       -> "Classified with frames v3 on ..."

A `name` may be what an entry holds ("frames") or a list's SELECTION — a
listbox row from list_classifiers ("frames   v3 (1a2b3c4d) · ...") or a table
row — so the same functions serve a name box and a model list.

THE CLASSIFIER
--------------
A random forest (scikit-learn) over each frame's features: a 32x32 greyscale
thumbnail plus its row and column brightness profiles and four whole-frame
statistics. The profiles are what a timing fault actually changes — WHERE the
light is — and they survive a few pixels of camera jitter that would move
every thumbnail pixel.

Tuned for a user who has marked a HANDFUL of frames, which is the normal case:

  * no bootstrap below 30 marked frames. Measured with 2 bad + 3 good marked:
    a standard (bootstrapped) forest scored 4/5 leave-one-out, because with one
    bad example left about a third of the trees never saw it; without
    bootstrap every tree sees every frame and it scored 5/5. At 30+ frames the
    usual bootstrap is back on, where it helps.
  * class_weight="balanced", so a class marked less often is not outvoted.
  * random_state fixed, so the same marks always give the same answers.

On the 120-frame sample capture, 3 bad + 4 good marked frames found all 10
bad-timing frames that frame_timing finds by a different method, with no false
alarms.

NO PICKLE. A fitted forest can only be saved by pickling it, and loading a
pickle runs code — the reason the policy gate refuses pickle in generated
apps. So "Train" stores the FEATURES (a plain .npz) and the forest is refit
from them when needed — about 0.1 s for a few dozen frames, and cached while
the app runs. Every .npz is read with allow_pickle=False, and its array
headers are checked BEFORE numpy allocates anything (see _read_model_bytes).

"Train" also reports an honest accuracy: leave-one-out (each marked frame
predicted from all the others) up to 20 frames, stratified k-fold beyond —
because a user marking ten frames has no held-out set.

WHERE IT KEEPS THINGS
---------------------
In the vault, never beside the frames:

    <vault>/classifiers/<name>/classes.json            classes + {frame path: class}
    <vault>/classifiers/<name>/versions/v<N>/model.npz  features, labels, paths
    <vault>/classifiers/<name>/versions/v<N>/classes.json  the marks it was trained on
    <vault>/classifiers/<name>/versions/v<N>/meta.json  number, sha256, when, frames
                                                       per class, accuracy, layout
    <vault>/classifiers/<name>/model.npz               copy of the CURRENT version
    <vault>/classifiers/<name>/runs.jsonl              one line per classified run
    <vault>/classifiers/.deleted/<name>_<stamp>/       what Delete moved aside

The capture folder is the raw data and is only ever READ — nothing here
writes into it, the run record included. A classifier is kept apart from any
one folder because the point of training one is to apply it to the NEXT
capture. Files are written atomically (temp file + replace; a version is a
temp FOLDER renamed into place), so a crash mid-save never leaves a half-
written label file or a half-written version.

VERSIONS
--------
Every Train that changes the model writes a new, immutable versions/v<N>; the
CURRENT model is the newest version. A version is identified by the sha256
of its CONTENT — the arrays, not the .npz bytes — and quoted as

    frames v3 (1a2b3c4d)            name, version number, first 8 of the sha

Content, not file bytes, because the same model must have the same id on the
PC that trained it and the PC it was carried to, whatever zip timestamps the
two numpy builds write. It also means Train with unchanged marks gives
exactly the same model (the forest is deterministic), and that is NOT a new
version: two numbers for one model would make the run record ambiguous.

model.npz at the top is a copy of the current version, kept so an older
build that knows nothing of versions still finds the current model. Its
content decides how the two agree, with no timestamps involved:

  * it is the newest version              -> nothing to do;
  * it is an OLDER version                -> a crash came between writing the
                                             version and the copy; the copy is
                                             rewritten (that content is still
                                             kept as its own version);
  * it is no version at all               -> it was trained before versions
                                             existed, or by an older build
                                             since: it is ADOPTED as the next
                                             version — never dropped;
  * it cannot be read                     -> reported and left exactly as it
                                             is; the next Train sets it aside
                                             as model.unusable-<stamp>.npz and
                                             says so. Never silently replaced.

A classifier from before versions (classes.json + model.npz only) therefore
keeps working unchanged: the first time anything opens it, its model becomes
v1 ("migrated") and its marks are untouched.

A marked frame whose FILE is gone (moved, or on another PC) is not lost to
the next Train: its features are taken from the current version, where they
were stored when it was trained. That is what lets an imported classifier be
trained further on the PC it was carried to.

THE LIBRARY
-----------
Save as copies (classes, every version; not the run record, which is the
original's history) and switches to the copy. Rename and Save as never
replace an existing classifier. Delete never deletes: it MOVES the folder to
classifiers/.deleted/<name>_<stamp> and says where, so moving it back
restores it. Rename and Delete are refused while this process is training or
classifying with that classifier, and Windows refuses to move a folder with a
file open inside it — reported as "in use by another program".

MOVING A CLASSIFIER TO ANOTHER PC
---------------------------------
export_classifier writes ONE file, <name>-v<N>.typhon-classifier.zip, holding
exactly: manifest.json (format, feature layout, when, where from, the sha256
of every other member), classes.json, the current version's model.npz and its
meta.json. The frame paths inside are this PC's — the model does not need the
frames, because the forest is refit from the stored features.

import_classifier trusts nothing in the file: only those four member names
(nothing is ever extracted by a name from the zip, so a "../" name cannot
land anywhere), size and member-count caps checked before reading, every
checksum, every array header before numpy allocates, allow_pickle=False, the
shapes and dtypes consistent, and the feature length equal to what THIS
build's features() produces. It never overwrites: a taken name imports as
<name>-2 and the summary says so. Where it came from is kept in classes.json
("origin") and in the version's meta.json ("imported_from").

THE RUN RECORD
--------------
classify_folder appends one line to the classifier's runs.jsonl: when, the
folder, each capture run in it (the "<stamp>" of "<stamp>_frame_000001.png")
with its frame count, the classifier, the version id and sha, and the count
per class. run_history and classified_with read it back, so the app can say
"Classified with frames v3 (1a2b3c4d) on 2026-10-02 14:03". The record lives
in the vault: the capture folder is never written to.

NOTHING A USER MARKED IS DESTROYED BY THE CLASSIFIER: a class that still labels
frames cannot be removed (re-mark those frames first), and re-marking a frame
is the user deliberately changing that one label.

Failures RAISE RuntimeError with a sentence a user can act on; a generated
handler shows it in a dialog and clears what the button fills. Soft cases that
are not failures — "Add class" with nothing typed — return the current classes
and say what to do, so the class list is not emptied by a stray click.

No function here is named like a call the policy gate denies (remove, unlink,
load, ...): a generated handler imports these by name, and the gate checks
imported names.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

THUMB = 32                    # thumbnail edge, pixels
TREES = 100                   # forest size: stable votes, ~0.1 s to fit
BOOTSTRAP_FROM = 30           # below this many marks every tree sees them all
LOO_MAX = 20                  # leave-one-out up to here; k-fold beyond
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif",
                  ".webp")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
# A folder named CON or NUL cannot be made on Windows — and an imported file
# chooses the name, so these are refused rather than failing half-way.
_DEVICE_NAMES = frozenset({"con", "prn", "aux", "nul",
                           *(f"com{i}" for i in range(1, 10)),
                           *(f"lpt{i}" for i in range(1, 10))})

#: Bumped whenever features() changes what it returns. A model made with
#: another layout cannot be refit or predicted with this one, and is refused
#: by name rather than failing deep inside the forest.
FEATURE_LAYOUT = 1

VERSIONS = "versions"
RUNS = "runs.jsonl"
DELETED = ".deleted"
_MODEL_ARRAYS = ("X", "y", "classes", "paths")
_VERSION_RE = re.compile(r"^v([1-9][0-9]{0,5})$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

EXPORT_FORMAT = "typhon-classifier"
EXPORT_FORMAT_VERSION = 1
EXPORT_SUFFIX = ".typhon-classifier.zip"
# member -> the most it may unpack to. One feature row is 1092 float32s
# (4.4 KB), so 512 MB of arrays is ~100,000 marked frames — far past a person
# marking by hand — while a zip bomb is refused before it is inflated.
_EXPORT_LIMITS = {"manifest.json": 1 << 20, "meta.json": 1 << 20,
                  "classes.json": 64 << 20, "model.npz": 512 << 20}
_EXPORT_MEMBERS = tuple(_EXPORT_LIMITS)
MAX_IMPORT_BYTES = 600 << 20          # the export file itself, on disk
MAX_MODEL_BYTES = 1 << 30             # all arrays of one model, unpacked


# ============================================================
# The store
# ============================================================

def _vault_root() -> Path:
    try:
        from gui_projects import resolve_vault_root
        return resolve_vault_root()
    except Exception:
        env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
        return Path(env).expanduser() if env else Path.home() / ".council" / "vault"


def _classifiers_root() -> Path:
    return _vault_root() / "classifiers"


def _name_of(value: Any) -> str:
    """A classifier name from what a port holds.

    An entry gives the name itself. A listbox gives its SELECTION, a list of
    rows; a list_classifiers row starts with the name ("frames   v3 ..."),
    and a table row is a tuple whose first cell is the name. A plain string
    is NOT split: "has space" typed in a name box must stay an invalid name,
    not quietly become "has"."""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        first = value[0]
        if isinstance(first, (list, tuple)):   # a table's selection: rows
            first = first[0] if first else ""
        text = str(first).strip()
        return text.split()[0] if text else ""
    return str(value or "").strip()


def store_dir(name: Any) -> Path:
    """<vault>/classifiers/<name>. The name is checked, so it can never
    reach outside that folder ("..\\..\\x" is not a name)."""
    n = _name_of(name)
    if not _NAME_RE.match(n) or n.lower() in _DEVICE_NAMES:
        raise RuntimeError(
            f"'{n}' is not a usable classifier name — use letters, digits, "
            f"'-' or '_' (e.g. frames)")
    return _classifiers_root() / n


def _load(name: Any) -> Dict[str, Any]:
    p = store_dir(name) / "classes.json"
    if not p.is_file():
        return {"version": 1, "classes": [], "labels": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read {p}: {exc}")
    if not isinstance(data, dict):
        raise RuntimeError(f"cannot read {p}: it is not a classifier's "
                           f"classes file (left as it is)")
    data.setdefault("classes", [])
    data.setdefault("labels", {})
    return data


def _atomic_write(path: Path, write) -> None:
    """Write via a temp file in the same folder, then replace — a crash
    mid-save leaves the old file, never a truncated one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    os.close(fd)
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_json(path: Path, obj: Any) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    _atomic_write(path, lambda tmp: Path(tmp).write_text(text, encoding="utf-8"))


def _save(name: Any, data: Dict[str, Any]) -> None:
    data["updated"] = _now()
    _write_json(store_dir(name) / "classes.json", data)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _counts(data: Dict[str, Any]) -> Dict[str, int]:
    c = {k: 0 for k in data["classes"]}
    for cls in data["labels"].values():
        c[cls] = c.get(cls, 0) + 1
    return c


def _counts_text(data: Dict[str, Any]) -> str:
    c = _counts(data)
    if not c:
        return "no classes yet"
    return ", ".join(f"{k} {v}" for k, v in c.items())


def _picked(selection: Any) -> str:
    """A listbox port's value is its SELECTION (a list); accept a str too."""
    if isinstance(selection, (list, tuple)):
        return str(selection[0]).strip() if selection else ""
    return str(selection or "").strip()


def _frame_path(folder: Any, frame: Any) -> Path:
    f = str(frame or "").strip()
    if not f:
        raise RuntimeError("no frame on screen — choose a folder of frames first")
    p = Path(f)
    if not p.is_absolute():
        base = str(folder or "").strip().strip('"')
        if not base:
            raise RuntimeError("no folder chosen — pick the folder of frames first")
        p = Path(base) / p
    if not p.is_file():
        raise RuntimeError(f"{p} is not a file")
    return p.resolve()


# ============================================================
# Features
# ============================================================

def thumbnail(path: Any):
    """A frame as a flat THUMBxTHUMB greyscale vector in [0, 1].

    16-bit scans are scaled rather than clamped (convert("L") would push a
    16-bit slice to near-white), matching how the live view shows them."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(f"{exc.name} is not installed, so frames cannot be "
                           f"classified")
    with Image.open(path) as im:
        if im.mode in ("I", "I;16", "I;16B", "I;16L", "F"):
            im = im.point(lambda v: v * (1.0 / 256)).convert("L")
        else:
            im = im.convert("L")
        small = im.resize((THUMB, THUMB), Image.BILINEAR)
        return np.asarray(small, dtype=np.float32).reshape(-1) / 255.0


def features(path: Any):
    """The vector the forest sees: thumbnail pixels, then the row and column
    brightness profiles, then mean / spread / dark share / bright share."""
    import numpy as np
    t = thumbnail(path)
    img = t.reshape(THUMB, THUMB)
    return np.concatenate([t, img.mean(axis=1), img.mean(axis=0),
                           [t.mean(), t.std(), (t < 0.2).mean(),
                            (t > 0.8).mean()]]).astype(np.float32)


_FEATURE_LENGTH: List[int] = []


def _feature_length() -> int:
    """How many numbers features() gives per frame IN THIS BUILD — measured
    by running it on a tiny picture, so it cannot disagree with features()
    the way a hand-kept constant could (1092 today: 1024 + 32 + 32 + 4)."""
    if not _FEATURE_LENGTH:
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError(f"{exc.name} is not installed, so frames cannot "
                               f"be classified")
        buf = io.BytesIO()
        Image.new("L", (8, 8)).save(buf, "PNG")
        buf.seek(0)
        _FEATURE_LENGTH.append(int(features(buf).shape[0]))
    return _FEATURE_LENGTH[0]


def _require_sklearn():
    try:
        from sklearn.ensemble import RandomForestClassifier
    except ImportError:
        raise RuntimeError("scikit-learn is not installed, so the classifier "
                           "cannot train (pip install scikit-learn)")
    return RandomForestClassifier


def _forest(X, y):
    """A fitted forest. Deterministic: the same marks give the same model."""
    RandomForestClassifier = _require_sklearn()
    return RandomForestClassifier(
        n_estimators=TREES, bootstrap=len(y) >= BOOTSTRAP_FROM,
        class_weight="balanced", random_state=0, n_jobs=1).fit(X, y)


def _accuracy(X, y) -> str:
    """An honest estimate from the marked frames alone."""
    import numpy as np
    n = len(y)
    smallest = int(np.bincount(y).min()) if n else 0
    if n <= 2:
        return "Too few frames for an accuracy estimate — mark more."
    if n > LOO_MAX and smallest < 2:
        return ("No accuracy estimate: one class has a single marked frame — "
                "mark at least two of each.")
    if n <= LOO_MAX:
        right = 0
        for i in range(n):
            keep = np.arange(n) != i
            if len(set(y[keep].tolist())) < 2:
                continue                  # holding this out empties a class
            right += int(_forest(X[keep], y[keep]).predict(X[i:i + 1])[0] == y[i])
        return (f"Leave-one-out accuracy {right / n:.0%} ({right}/{n}): each "
                f"marked frame predicted from all the others.")
    from sklearn.model_selection import StratifiedKFold
    folds = min(5, smallest)
    right = 0
    for tr, te in StratifiedKFold(folds, shuffle=True, random_state=0).split(X, y):
        right += int((_forest(X[tr], y[tr]).predict(X[te]) == y[te]).sum())
    return (f"{folds}-fold accuracy {right / n:.0%} ({right}/{n}): each marked "
            f"frame predicted by a forest that never saw it.")


# ============================================================
# Model files: plain arrays, checked before they are trusted
# ============================================================

def _npz_bytes(model: Dict[str, Any]) -> bytes:
    import numpy as np
    buf = io.BytesIO()
    np.savez_compressed(buf, X=np.asarray(model["X"], dtype=np.float32),
                        y=np.asarray(model["y"], dtype=np.int32),
                        classes=np.asarray(model["classes"]),
                        paths=np.asarray(model["paths"]))
    return buf.getvalue()


def _read_model_bytes(data: bytes, where: str) -> Dict[str, Any]:
    """A model.npz's arrays — X, y, classes, paths — or RuntimeError.

    Every header is read and checked BEFORE numpy allocates the array: an
    .npy header names its own shape, and a crafted one claiming a billion
    rows in a few bytes would otherwise have numpy reserve gigabytes. A
    header holding Python objects is refused outright — reading those IS
    unpickling — and the arrays are then loaded with allow_pickle=False
    regardless."""
    import numpy as np
    from numpy.lib import format as npf
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise RuntimeError(f"{where} is not a model file ({exc})")
    try:
        with zf:
            infos = zf.infolist()
            got = sorted(i.filename for i in infos)
            want = sorted(f"{k}.npy" for k in _MODEL_ARRAYS)
            if got != want:
                raise RuntimeError(f"{where} holds {', '.join(got) or 'nothing'}"
                                   f" — a model holds exactly "
                                   f"{', '.join(want)}")
            if sum(i.file_size for i in infos) > MAX_MODEL_BYTES:
                raise RuntimeError(f"{where} unpacks to more than "
                                   f"{MAX_MODEL_BYTES >> 20} MB — refused")
            for info in infos:
                with zf.open(info) as fh:
                    major, _minor = npf.read_magic(fh)
                    if major == 1:
                        shape, _f, dtype = npf.read_array_header_1_0(fh)
                    elif major == 2:
                        shape, _f, dtype = npf.read_array_header_2_0(fh)
                    else:
                        raise RuntimeError(f"{where}: {info.filename} is an "
                                           f"array format this build does "
                                           f"not read (version {major})")
                if dtype.hasobject:
                    raise RuntimeError(
                        f"{where}: {info.filename} holds Python objects — "
                        f"refused, because reading them would mean "
                        f"unpickling")
                if math.prod(shape) * dtype.itemsize > info.file_size:
                    raise RuntimeError(
                        f"{where}: {info.filename} claims shape {shape} but "
                        f"holds only {info.file_size} bytes")
        with np.load(io.BytesIO(data), allow_pickle=False) as m:
            arrays = {k: m[k] for k in _MODEL_ARRAYS}
    except RuntimeError:
        raise
    except Exception as exc:          # zlib/CRC errors, a truncated array ...
        raise RuntimeError(f"{where} is damaged "
                           f"({type(exc).__name__}: {exc})")
    return _checked_model(arrays, where)


def _checked_model(a: Dict[str, Any], where: str) -> Dict[str, Any]:
    """The arrays agree with each other and with this build's features —
    or RuntimeError saying which does not. Returns them normalised: X
    float32, y int32, classes and paths as lists of str."""
    import numpy as np
    X, y, classes, paths = a["X"], a["y"], a["classes"], a["paths"]
    if X.ndim != 2 or X.dtype.kind != "f":
        raise RuntimeError(f"{where}: the features are not a table of numbers")
    n, width = X.shape
    expected = _feature_length()
    if width != expected:
        raise RuntimeError(
            f"{where} has {width} features per frame, but this build makes "
            f"{expected} — it was made by a different version of the "
            f"classifier. Train it again here from the marked frames.")
    if n < 2:
        raise RuntimeError(f"{where} holds fewer than two frames")
    if y.shape != (n,) or y.dtype.kind not in "iu":
        raise RuntimeError(f"{where}: the labels do not match the frames "
                           f"({y.shape} labels for {n} frames)")
    if classes.ndim != 1 or classes.dtype.kind != "U" or len(classes) < 2:
        raise RuntimeError(f"{where}: the class names are missing")
    names = [str(c) for c in classes]
    if len(set(names)) != len(names) or not all(c.strip() for c in names):
        raise RuntimeError(f"{where}: the class names are blank or repeated")
    if paths.shape != (n,) or paths.dtype.kind != "U":
        raise RuntimeError(f"{where}: the frame list does not match the "
                           f"frames ({paths.shape} for {n})")
    if int(y.min()) < 0 or int(y.max()) >= len(names):
        raise RuntimeError(f"{where}: a label names a class that is not there")
    if len(set(y.tolist())) < 2:
        raise RuntimeError(f"{where}: every frame is in one class — a "
                           f"classifier needs two")
    if not np.isfinite(X).all():
        raise RuntimeError(f"{where}: the features hold NaN or infinity")
    return {"X": X.astype(np.float32, copy=False),
            "y": y.astype(np.int32, copy=False),
            "classes": names, "paths": [str(p) for p in paths]}


def _model_sha(model: Dict[str, Any]) -> str:
    """sha256 of a model's CONTENT — what its version is known by.

    Over the arrays in a fixed form, not the .npz bytes: a zip carries
    timestamps and compression choices that differ between numpy builds, and
    the same model must have the same id on both PCs."""
    import numpy as np
    h = hashlib.sha256(b"typhon-classifier-model/1\n")
    X = np.ascontiguousarray(model["X"], dtype="<f4")
    y = np.ascontiguousarray(model["y"], dtype="<i8")
    h.update(f"X {X.shape}\n".encode())
    h.update(X.tobytes())
    h.update(f"y {y.shape}\n".encode())
    h.update(y.tobytes())
    for key in ("classes", "paths"):
        h.update(f"\n{key} ".encode())
        h.update(json.dumps([str(v) for v in model[key]],
                            ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()


def _read_model_file(p: Path) -> Dict[str, Any]:
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"cannot read {p}: {exc}")
    return _read_model_bytes(data, str(p))


# ============================================================
# Versions
# ============================================================

def _vid(name: str, meta: Dict[str, Any]) -> str:
    """The id a person reads and quotes: "frames v3 (1a2b3c4d)"."""
    return f"{name} v{meta['version']} ({str(meta['sha256'])[:8]})"


def _version_numbers(d: Path) -> List[int]:
    vroot = d / VERSIONS
    if not vroot.is_dir():
        return []
    out = []
    for e in vroot.iterdir():
        m = _VERSION_RE.match(e.name)
        if m and e.is_dir():
            out.append(int(m.group(1)))
    return sorted(out)


def _read_meta(vdir: Path) -> Dict[str, Any]:
    p = vdir / "meta.json"
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{vdir.name} of this classifier is damaged ({p}: "
                           f"{exc}). It was left as it is: press Train to make "
                           f"a new version from the marked frames.")
    m = _VERSION_RE.match(vdir.name)
    if (not isinstance(meta, dict) or not m
            or meta.get("version") != int(m.group(1))
            or not _SHA_RE.match(str(meta.get("sha256", "")))):
        raise RuntimeError(f"{vdir.name} of this classifier is damaged ({p} "
                           f"does not describe it). It was left as it is: "
                           f"press Train to make a new version.")
    return meta


def _newest(d: Path) -> Optional[Tuple[Dict[str, Any], Path]]:
    """(meta, folder) of the newest version, None when there is none. A
    damaged newest version RAISES: quietly using the one before would make
    "current" mean something the user never chose."""
    nums = _version_numbers(d)
    if not nums:
        return None
    vdir = d / VERSIONS / f"v{nums[-1]}"
    return _read_meta(vdir), vdir


def _write_version(d: Path, model_bytes: bytes, classes_doc: Dict[str, Any],
                   meta: Dict[str, Any], number: int = 0
                   ) -> Tuple[Dict[str, Any], Path]:
    """A new immutable version under d/versions, complete or not at all.

    Written into a hidden temporary folder and RENAMED into place, which is
    one step: there is never a v<N> with a model and no meta. The number is
    the next free one (or ``number``, for an import keeping its source's),
    and a rename that loses a race to another writer just takes the next."""
    vroot = d / VERSIONS
    tmp: Optional[Path] = None
    try:
        vroot.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=str(vroot)))
        (tmp / "model.npz").write_bytes(model_bytes)
        _write_json(tmp / "classes.json", classes_doc)
        nums = _version_numbers(d)
        n = number or ((nums[-1] + 1) if nums else 1)
        for _ in range(1000):
            target = vroot / f"v{n}"
            if not target.exists():
                full = dict(meta, version=n)
                _write_json(tmp / "meta.json", full)
                try:
                    os.rename(tmp, target)
                    return full, target
                except OSError:
                    if not target.exists():
                        raise
            n += 1
        raise RuntimeError(f"cannot find a free version number in {vroot}")
    except OSError as exc:
        raise RuntimeError(f"cannot save a new version in {vroot}: {exc}")
    finally:
        if tmp is not None and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def _write_mirror(d: Path, vdir: Path) -> str:
    """model.npz at the top := the current version's — for older builds.
    Returns "" or a note: the copy failing must not fail what it serves,
    because the version itself is already safe."""
    src = vdir / "model.npz"
    try:
        _atomic_write(d / "model.npz", lambda tmp: shutil.copyfile(src, tmp))
        return ""
    except OSError as exc:
        return (f"Note: could not refresh model.npz, the copy kept for older "
                f"builds ({exc}).")


def _set_aside(p: Path, why: str) -> Path:
    """Rename ``p`` out of the way — kept, never deleted."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = p.with_name(f"{p.stem}.{why}-{stamp}{p.suffix}")
    k = 2
    while dest.exists():
        dest = p.with_name(f"{p.stem}.{why}-{stamp}_{k}{p.suffix}")
        k += 1
    os.rename(p, dest)
    return dest


def _per_class(model: Dict[str, Any]) -> Dict[str, int]:
    out = {c: 0 for c in model["classes"]}
    for i in model["y"].tolist():
        out[model["classes"][int(i)]] += 1
    return out


def _snapshot(d: Path) -> Dict[str, Any]:
    """The marks as they are now, for a version's classes.json."""
    try:
        data = json.loads((d / "classes.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {"classes": list(data.get("classes") or []),
                    "labels": dict(data.get("labels") or {})}
    except (OSError, ValueError):
        pass
    return {"classes": [], "labels": {}}


# (path, mtime_ns, size) -> content sha of a top-level model.npz, so the
# check every Predict makes reads the file once, not every time.
_ROOT_SHA: Dict[Tuple[str, int, int], str] = {}


def _root_model_sha(p: Path) -> str:
    st = p.stat()
    key = (str(p), st.st_mtime_ns, st.st_size)
    if key not in _ROOT_SHA:
        for old in [k for k in _ROOT_SHA if k[0] == key[0]]:
            del _ROOT_SHA[old]                 # that file has changed since
        while len(_ROOT_SHA) >= 32:
            _ROOT_SHA.pop(next(iter(_ROOT_SHA)))
        _ROOT_SHA[key] = _model_sha(_read_model_file(p))
    return _ROOT_SHA[key]


def _adopt(d: Path, name: str, how: str, why: str) -> Dict[str, Any]:
    """Make the top-level model.npz the next version, byte for byte."""
    root = d / "model.npz"
    data = root.read_bytes()
    model = _read_model_bytes(data, str(root))
    meta = {"sha256": _model_sha(model),
            "created": time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(root.stat().st_mtime)),
            "how": how, "note": why,
            "frames": len(model["y"]), "frames_per_class": _per_class(model),
            "classes": model["classes"], "accuracy": "",
            "feature_layout": FEATURE_LAYOUT,
            "feature_length": int(model["X"].shape[1])}
    meta, _vdir = _write_version(d, data, _snapshot(d), meta)
    return meta


def _reconcile(d: Path, name: str, write: bool = False) -> List[str]:
    """Bring the top-level model.npz and the versions into agreement (the
    rules are in the module docstring) and return what a person should be
    told about it. ``write`` is True only for Train, the one action allowed
    to set an unreadable file aside; everything else reports it and leaves
    it exactly where it is."""
    root = d / "model.npz"
    nums = _version_numbers(d)
    if not root.is_file():
        if nums:
            newest = _newest(d)
            note = _write_mirror(d, newest[1])     # a pure addition
            return [note] if note else []
        return []
    try:
        sha = _root_model_sha(root)
    except RuntimeError as exc:
        if write:
            kept = _set_aside(root, "unusable")
            return [f"The old model.npz could not be read ({exc}); it was kept "
                    f"as {kept.name}."]
        if not nums:
            raise RuntimeError(
                f"the trained model {root} cannot be read ({exc}). It was left "
                f"as it is: press Train to train a new version from the marked "
                f"frames (your marks are kept).")
        return [f"Note: model.npz (the copy kept for older builds) cannot be "
                f"read and was left as it is; the next Train sets it aside."]
    if not nums:
        meta = _adopt(d, name, "migrated",
                      "trained before versions existed; accuracy not recorded")
        return [f"The model trained before versions is now {_vid(name, meta)} "
                f"— nothing was lost."]
    meta, vdir = _newest(d)
    if sha == meta["sha256"]:
        return []
    known = set()
    for n in nums[:-1]:
        try:
            known.add(_read_meta(d / VERSIONS / f"v{n}")["sha256"])
        except RuntimeError:
            continue
    if sha in known:
        note = _write_mirror(d, vdir)   # stale copy; its content is a version
        return [note] if note else []
    meta = _adopt(d, name, "adopted",
                  "a model.npz trained without versions (an older build) "
                  "after the last version; kept as the newest")
    return [f"A model trained by an older build is now {_vid(name, meta)}."]


# Refitting is ~0.1 s, but Predict is pressed repeatedly: keep the last few
# forests. Keyed by the version's folder and sha — a version never changes,
# so there is no modification time to race.
_CACHE: Dict[Tuple[str, str], Tuple[Any, Dict[str, Any]]] = {}
_CACHE_MAX = 8


def _load_model(name: Any):
    """(forest, X, classes, paths, meta, notes) for the current version."""
    d = store_dir(name)
    n = _name_of(name)
    notes = _reconcile(d, n)
    newest = _newest(d)
    if newest is None:
        raise RuntimeError("no trained model yet — mark some frames in at "
                           "least two classes, then press Train")
    meta, vdir = newest
    key = (os.path.normcase(str(vdir)), meta["sha256"])
    hit = _CACHE.get(key)
    if hit is None:
        model = _read_model_file(vdir / "model.npz")
        if _model_sha(model) != meta["sha256"]:
            raise RuntimeError(
                f"{_vid(n, meta)} is damaged: {vdir / 'model.npz'} no longer "
                f"matches its checksum. It was left as it is: press Train to "
                f"make a new version from the marked frames.")
        hit = (_forest(model["X"], model["y"]), model)
        while len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = hit
    forest, model = hit
    return forest, model["X"], model["classes"], model["paths"], meta, notes


def _nearest(X, x) -> int:
    """Index of the most similar marked frame — shown with each prediction so
    the user can see WHY, which a forest's vote alone does not say."""
    import numpy as np
    return int(np.argmin(((X - x) ** 2).sum(axis=1)))


# Classifiers this process is training or classifying with right now — so
# Rename and Delete can refuse instead of moving a folder out from under them.
_BUSY: Dict[str, int] = {}
_BUSY_LOCK = threading.Lock()


@contextlib.contextmanager
def _using(d: Path) -> Iterator[None]:
    key = os.path.normcase(str(d))
    with _BUSY_LOCK:
        _BUSY[key] = _BUSY.get(key, 0) + 1
    try:
        yield
    finally:
        with _BUSY_LOCK:
            _BUSY[key] -= 1
            if not _BUSY[key]:
                del _BUSY[key]


def _refuse_if_busy(d: Path, name: str, doing: str) -> None:
    with _BUSY_LOCK:
        busy = _BUSY.get(os.path.normcase(str(d)), 0)
    if busy:
        raise RuntimeError(f"'{name}' is in use — it is training or "
                           f"classifying right now. Wait for that to finish, "
                           f"then {doing} again.")


def _forget(d: Path) -> None:
    """Drop cached forests of a classifier that moved."""
    head = os.path.normcase(str(d)) + os.sep
    for key in [k for k in _CACHE if k[0].startswith(head)]:
        _CACHE.pop(key, None)


# ============================================================
# The button functions
# ============================================================

def open_classifier(name: Any) -> Dict[str, Any]:
    """Load a classifier's classes, creating nothing until something is added.

    A model trained before versions existed becomes v1 here, the first time
    it is opened. Keys: name, classes, version, summary."""
    n = _name_of(name)
    d = store_dir(name)
    data = _load(name)
    version, note = "", ""
    try:
        notes = _reconcile(d, n)
        newest = _newest(d)
        if newest:
            version = _vid(n, newest[0])
        note = " ".join(notes)
    except RuntimeError as exc:
        note = f"The trained model has a problem: {exc}"
    trained = (f" — trained, current model {version}" if version
               else " — not trained yet")
    return {"name": n, "classes": list(data["classes"]), "version": version,
            "summary": (f"Classifier '{n}': {len(data['labels'])} frame(s) "
                        f"marked ({_counts_text(data)}){trained}"
                        + (f". {note}" if note else ""))}


def add_class(name: Any, new_class: Any) -> Dict[str, Any]:
    data = _load(name)
    cls = str(new_class or "").strip()
    if not cls:
        return {"classes": list(data["classes"]), "cleared": "",
                "summary": "Type a class name in New class, then Add class."}
    if cls in data["classes"]:
        return {"classes": list(data["classes"]), "cleared": "",
                "summary": f"'{cls}' is already a class."}
    data["classes"].append(cls)
    _save(name, data)
    return {"classes": list(data["classes"]), "cleared": "",
            "summary": f"Added '{cls}'. Pick it in the list, then mark frames."}


def remove_class(name: Any, selection: Any) -> Dict[str, Any]:
    data = _load(name)
    cls = _picked(selection)
    if not cls:
        return {"classes": list(data["classes"]),
                "summary": "Pick a class in the list to remove it."}
    n = _counts(data).get(cls, 0)
    if n:
        # Refused, not an error: removing it would throw away n labels the
        # user made. Re-marking those frames is the deliberate way out.
        return {"classes": list(data["classes"]),
                "summary": f"Not removed: '{cls}' still labels {n} frame(s). "
                           f"Re-mark them as another class first."}
    data["classes"] = [c for c in data["classes"] if c != cls]
    _save(name, data)
    return {"classes": list(data["classes"]),
            "summary": f"Removed '{cls}'."}


def mark_frame(name: Any, folder: Any, frame: Any,
               selection: Any) -> Dict[str, Any]:
    data = _load(name)
    cls = _picked(selection)
    if not cls:
        raise RuntimeError("pick a class in the Classes list first")
    if cls not in data["classes"]:
        raise RuntimeError(f"'{cls}' is not a class of '{_name_of(name)}' — "
                           f"Open it again to refresh the list")
    p = _frame_path(folder, frame)
    was = data["labels"].get(str(p))
    data["labels"][str(p)] = cls
    _save(name, data)
    change = f" (was '{was}')" if was and was != cls else ""
    return {"summary": f"{p.name} marked '{cls}'{change}. "
                       f"Marked so far: {_counts_text(data)}."}


def train(name: Any) -> Dict[str, Any]:
    """Fit on the marked frames and keep the result as a new version.

    A marked frame whose file is gone keeps counting: its features come from
    the current version, which stored them when it was trained. Marks that
    give exactly the current model make no new version (see VERSIONS).
    Keys: summary, version, sha256."""
    import numpy as np
    n = _name_of(name)
    d = store_dir(name)
    data = _load(name)
    used = [c for c, k in _counts(data).items() if k]
    if len(used) < 2:
        raise RuntimeError(f"mark frames in at least two classes before "
                           f"training (marked: {_counts_text(data)})")
    with _using(d):
        notes = _reconcile(d, n, write=True) if d.is_dir() else []
        stored: Dict[str, Any] = {}
        prior = None
        try:
            newest = _newest(d)
            if newest:
                old = _read_model_file(newest[1] / "model.npz")
                if _model_sha(old) != newest[0]["sha256"]:
                    raise RuntimeError("it no longer matches its checksum")
                stored = {p: old["X"][i] for i, p in enumerate(old["paths"])}
                prior = newest
        except RuntimeError as exc:
            # Not fatal: the marks are still here, so this Train simply makes
            # a new version without borrowing from the damaged one.
            notes.append(f"The current version could not be read ({exc}), so "
                         f"no stored features were reused.")
        classes = [c for c in data["classes"] if c in used]
        rows, ys, paths, missing, reused = [], [], [], [], 0
        for path, cls in sorted(data["labels"].items()):
            if cls not in classes:
                continue
            if Path(path).is_file():
                try:
                    rows.append(features(path))
                except RuntimeError:
                    raise
                except Exception:
                    missing.append(Path(path).name)
                    continue
            elif path in stored:
                rows.append(stored[path])
                reused += 1
            else:
                missing.append(Path(path).name)
                continue
            ys.append(classes.index(cls))
            paths.append(path)
        if len({*ys}) < 2:
            raise RuntimeError("fewer than two classes still have readable "
                               "frames "
                               + (f"(missing: {', '.join(missing[:3])})"
                                  if missing else ""))
        model = {"X": np.stack(rows).astype(np.float32),
                 "y": np.asarray(ys, dtype=np.int32),
                 "classes": classes, "paths": paths}
        _require_sklearn()      # before saving: a model nothing can use is no model
        sha = _model_sha(model)
        extra = ""
        if reused:
            extra += (f" Reused the stored features of {reused} marked "
                      f"frame(s) whose files are not here.")
        if missing:
            extra += f" Skipped {len(missing)} missing frame(s)."
        head = (f"Trained a random forest on {len(ys)} frames in "
                f"{len(classes)} classes.")
        if prior and prior[0]["sha256"] == sha:
            # Same marks, same deterministic forest: the same model. Its
            # accuracy was measured when it was made, so it is not re-run
            # (leave-one-out refits the forest once per frame).
            meta, vdir = prior
            quality = meta.get("accuracy") or _accuracy(model["X"], model["y"])
            vid = _vid(n, meta)
            tail = f" Unchanged: still {vid} — no new version."
        else:
            quality = _accuracy(model["X"], model["y"])
            meta, vdir = _write_version(d, _npz_bytes(model), _snapshot(d), {
                "sha256": sha, "created": _now(), "how": "trained",
                "frames": len(ys), "frames_per_class": _per_class(model),
                "classes": classes, "accuracy": quality,
                "feature_layout": FEATURE_LAYOUT,
                "feature_length": int(model["X"].shape[1]),
                "reused_features": reused, "skipped_missing": len(missing)})
            vid = _vid(n, meta)
            tail = f" Saved as {vid}."
        mirror = _write_mirror(d, vdir)
        if mirror:
            notes.append(mirror)
    note = (" " + " ".join(notes)) if notes else ""
    return {"summary": f"{head} {quality}{extra}{tail}{note}",
            "version": vid, "sha256": sha}


def _share_text(proba) -> Tuple[int, str]:
    best = int(proba.argmax())
    return best, f"{float(proba[best]):.0%}"


def predict_frame(name: Any, folder: Any, frame: Any) -> Dict[str, Any]:
    """The frame on screen's likely class, with the version that said so.
    Keys: label, version, summary."""
    forest, X, classes, paths, meta, notes = _load_model(name)
    p = _frame_path(folder, frame)
    x = features(p)
    best, share = _share_text(forest.predict_proba(x[None, :])[0])
    cls = classes[int(forest.classes_[best])]
    near = Path(paths[_nearest(X, x)]).name
    vid = _vid(_name_of(name), meta)
    note = (" " + " ".join(notes)) if notes else ""
    return {"label": cls, "version": vid,
            "summary": (f"{p.name} looks like '{cls}' ({share} of the forest "
                        f"agrees); most similar marked frame {near}. "
                        f"[{vid}]{note}")}


def _run_of(filename: str) -> str:
    """The capture run a frame file belongs to: "20261002_101500" for
    "20261002_101500_frame_000001.png" (frame_camera's naming), else the
    name without its trailing frame number ("frame" for "frame_0001")."""
    stem = Path(filename).stem
    head, found, _ = stem.partition("_frame_")
    if found and head:
        return head
    return re.sub(r"[_\-. ]*\d+$", "", stem) or "frames"


def _append_run(d: Path, record: Dict[str, Any]) -> str:
    """Add one line to runs.jsonl; "" when written, else why not.

    Appended, never rewritten — history is not edited. A crash mid-line
    leaves a partial last line, so a new record starts on a fresh line and
    the reader skips (and counts) what it cannot parse."""
    p = d / RUNS
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        d.mkdir(parents=True, exist_ok=True)
        lead = ""
        if p.is_file() and p.stat().st_size:
            with open(p, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    lead = "\n"
        with open(p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(lead + line)
            fh.flush()
            os.fsync(fh.fileno())
        return ""
    except OSError as exc:
        return str(exc)


def classify_folder(name: Any, folder: Any) -> Dict[str, Any]:
    """Every image in ``folder``, in capture order, with its likely class.
    A row shows the forest's agreement when it is below 80%, so an uncertain
    call does not read like a certain one.

    Each call is RECORDED in the classifier's runs.jsonl (in the vault —
    the capture folder is only read): when, the folder, each capture run in
    it, the version id and sha, and the count per class.
    Keys: rows, counts, version, sha256, summary."""
    n = _name_of(name)
    d = store_dir(name)
    with _using(d):
        forest, X, classes, _paths, meta, notes = _load_model(name)
        f = Path(str(folder or "").strip().strip('"'))
        if not str(folder or "").strip():
            raise RuntimeError("no folder chosen — pick the folder of frames first")
        if not f.is_dir():
            raise RuntimeError(f"{f} is not a folder")

        def _natkey(p):
            return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower())
                    for t in re.split(r"(\d+)", p.name)]

        frames = sorted((p for p in f.iterdir()
                         if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES),
                        key=_natkey)
        if not frames:
            raise RuntimeError(f"no images in {f}")
        import numpy as np
        feats, names, unreadable = [], [], 0
        for p in frames:
            try:
                feats.append(features(p))
            except RuntimeError:
                raise
            except Exception:
                unreadable += 1
                continue
            names.append(p.name)
        rows, tally = [], {c: 0 for c in classes}
        if feats:
            probas = forest.predict_proba(np.stack(feats))  # one pass, not per frame
            for nm, pr in zip(names, probas):
                best, share = _share_text(pr)
                cls = classes[int(forest.classes_[best])]
                tally[cls] += 1
                rows.append(f"{nm}   {cls}" + ("" if pr[best] >= 0.8 else f"  ({share})"))
    vid = _vid(n, meta)
    recorded = ""
    if rows:
        runs: Dict[str, int] = {}
        for nm in names:
            run = _run_of(nm)
            runs[run] = runs.get(run, 0) + 1
        why = _append_run(d, {
            "when": _now(), "folder": str(f.resolve()), "runs": runs,
            "frames": len(rows), "unreadable": unreadable, "classifier": n,
            "version": meta["version"], "version_id": vid,
            "sha256": meta["sha256"], "counts": tally})
        recorded = (f" — classified with {vid}, recorded." if not why else
                    f" — classified with {vid} (NOT recorded: {why}).")
    summary = (f"{len(rows)} frames: "
               + ", ".join(f"{c} {k}" for c, k in tally.items())
               + (f"; {unreadable} unreadable" if unreadable else "")
               + recorded + ((" " + " ".join(notes)) if notes else ""))
    return {"rows": rows, "counts": tally, "version": vid,
            "sha256": meta["sha256"], "summary": summary}


# ============================================================
# The library
# ============================================================

def _describe(d: Path) -> Dict[str, Any]:
    """What the list shows for one classifier folder. READ-ONLY: listing
    migrates nothing and sets nothing aside, and a damaged classifier is a
    row saying so rather than an error that hides all the others."""
    info: Dict[str, Any] = {
        "name": d.name, "trained": False, "version": 0, "version_id": "",
        "sha256": "", "classes": [], "frames": 0, "counts": {},
        "updated": "", "origin": {}, "problem": ""}
    p = d / "classes.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a classes file")
            info["classes"] = [str(c) for c in data.get("classes") or []]
            labels = dict(data.get("labels") or {})
            info["frames"] = len(labels)
            counts = {c: 0 for c in info["classes"]}
            for c in labels.values():
                counts[str(c)] = counts.get(str(c), 0) + 1
            info["counts"] = counts
            info["updated"] = str(data.get("updated") or "")
            info["origin"] = dict(data.get("origin") or {})
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            info["problem"] = f"classes.json cannot be read ({exc})"
    try:
        newest = _newest(d)
    except RuntimeError as exc:
        newest = None
        info["trained"] = True
        info["problem"] = info["problem"] or str(exc).split(". ")[0]
    if newest:
        meta = newest[0]
        info.update(trained=True, version=int(meta["version"]),
                    version_id=_vid(d.name, meta), sha256=meta["sha256"])
        info["updated"] = max(info["updated"], str(meta.get("created") or ""))
    elif (d / "model.npz").is_file():
        info["trained"] = True                  # before versions; not yet v1
    return info


def _row_text(info: Dict[str, Any]) -> str:
    """One listbox row. The NAME COMES FIRST and has no spaces, so a row
    picked in the list names its classifier (see _name_of)."""
    if info["version"]:
        state = f"v{info['version']} ({info['sha256'][:8]})"
    elif info["trained"]:
        state = "trained (before versions)"
    else:
        state = "not trained"
    k = len(info["classes"])
    text = (f"{info['name']}   {state} · {k} class{'' if k == 1 else 'es'} · "
            f"{info['frames']} marked")
    if info["updated"]:
        text += f" · {info['updated'][:16]}"
    if info["problem"]:
        text += f" · PROBLEM: {info['problem']}"
    return text


def _library() -> Dict[str, Any]:
    root = _classifiers_root()
    found = []
    if root.is_dir():
        for d in sorted(root.iterdir(), key=lambda e: e.name.lower()):
            if d.is_dir() and _NAME_RE.match(d.name):
                found.append(_describe(d))
    return {"details": found,
            "names": [i["name"] for i in found],
            "rows": [_row_text(i) for i in found],
            "table": [(i["name"],
                       f"v{i['version']} ({i['sha256'][:8]})" if i["version"]
                       else ("before versions" if i["trained"] else "not trained"),
                       ", ".join(i["classes"]), str(i["frames"]),
                       i["updated"][:16]) for i in found]}


def list_classifiers() -> Dict[str, Any]:
    """Every saved classifier: its name, whether it is trained, its current
    version, its classes, how many frames are marked, when it last changed.

    Keys: rows (one line each, for a listbox — the name comes first), table
    (name, version, classes, frames, updated — for a table), names, details
    (a dict per classifier, for code), summary."""
    lib = _library()
    k = len(lib["names"])
    summary = (f"{k} saved classifier{'' if k == 1 else 's'}: "
               + ", ".join(lib["names"][:6]) + (" ..." if k > 6 else "")
               if k else "No saved classifiers yet — type a name, add "
                         "classes and mark frames.")
    return {"rows": lib["rows"], "table": lib["table"], "names": lib["names"],
            "details": lib["details"], "summary": summary}


def _existing(name: Any, doing: str) -> Tuple[str, Path]:
    n = _name_of(name)
    if not n:
        raise RuntimeError(f"pick a classifier in the list (or type its name) "
                           f"to {doing} it")
    d = store_dir(n)
    if not d.is_dir():
        raise RuntimeError(f"there is no classifier '{n}' to {doing}")
    return n, d


def _free_target(new_name: Any, doing: str) -> Tuple[str, Path]:
    new = _name_of(new_name)
    if not new:
        raise RuntimeError(f"type the new name first, then {doing}")
    target = store_dir(new)
    if target.exists():
        raise RuntimeError(f"'{new}' already exists — choose another name "
                           f"(nothing was overwritten)")
    return new, target


def _in_use(exc: OSError, n: str, d: Path) -> RuntimeError:
    if isinstance(exc, PermissionError):
        return RuntimeError(f"'{n}' is in use by another program — a file "
                            f"inside {d} is open. Close it and try again; "
                            f"nothing was changed.")
    return RuntimeError(f"cannot move {d}: {exc}. Nothing was changed.")


def _copy_ignore(_dir: str, names: List[str]) -> List[str]:
    """What a copy leaves behind: the run record (the original's history),
    half-written temp files and folders, and files set aside as unusable."""
    return [x for x in names
            if x == RUNS or x.startswith(".tmp-") or ".unusable-" in x
            or re.match(r"^(classes\.json|model\.npz|meta\.json)\.\w+$", x)]


def _stage_into_place(build, target: Path) -> None:
    """Build a classifier folder in a hidden staging folder beside the
    others, then rename it to ``target`` in one step — a copy or an import
    is all there or not there, and an existing folder is never replaced
    (the rename refuses)."""
    root = _classifiers_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=str(root)))
    try:
        staged = tmp / "c"
        build(staged)
        if target.exists():
            raise FileExistsError(str(target))
        os.rename(staged, target)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def save_as(name: Any, new_name: Any) -> Dict[str, Any]:
    """Copy a classifier under a new name — its classes, marks and every
    version — and switch to the copy. Never replaces an existing
    classifier. The run record stays with the original: it is the
    original's history.
    Keys: name, classes, rows, table, names, summary."""
    n, d = _existing(name, "copy")
    new, target = _free_target(new_name, "press Save as")
    _load(n)                      # a damaged classes.json is reported, not copied
    with _using(d):
        notes = _reconcile(d, n)            # a pre-versions model becomes v1 first

        def build(staged: Path) -> None:
            shutil.copytree(d, staged, ignore=_copy_ignore)
            doc = staged / "classes.json"
            if doc.is_file():
                data = json.loads(doc.read_text(encoding="utf-8"))
                data["origin"] = {"how": "copied", "from": n, "when": _now()}
                _write_json(doc, data)
        try:
            _stage_into_place(build, target)
        except FileExistsError:
            raise RuntimeError(f"'{new}' already exists — choose another name "
                               f"(nothing was overwritten)")
        except OSError as exc:
            raise RuntimeError(f"cannot copy '{n}' to '{new}': {exc}")
    lib = _library()
    newest = _newest(target)
    what = (f"with its {len(_version_numbers(target))} version(s), current "
            f"{_vid(new, newest[0])}" if newest else "(not trained yet)")
    note = (" " + " ".join(notes)) if notes else ""
    return {"name": new, "classes": list(_load(new)["classes"]),
            "rows": lib["rows"], "table": lib["table"], "names": lib["names"],
            "summary": f"Saved '{n}' as '{new}' {what}. Now using '{new}'."
                       f"{note}"}


def rename_classifier(name: Any, new_name: Any,
                      current: Any = "") -> Dict[str, Any]:
    """Rename a classifier: its marks, versions and run record move with it.
    Never replaces an existing classifier, and is refused while it is
    training or classifying. ``current`` is the name the window has open:
    "name" comes back as the new name when that was the one renamed, so the
    name box follows it — and unchanged otherwise.
    Keys: name, rows, table, names, summary."""
    n, d = _existing(name, "rename")
    new = _name_of(new_name)
    if not new:
        raise RuntimeError("type the new name first, then press Rename")
    target = store_dir(new)
    case_only = new != n and new.lower() == n.lower()
    if new == n:
        raise RuntimeError(f"'{n}' already has that name")
    if target.exists() and not case_only:
        raise RuntimeError(f"'{new}' already exists — choose another name "
                           f"(nothing was overwritten)")
    _refuse_if_busy(d, n, "rename it")
    try:
        os.rename(d, target)
    except OSError as exc:
        raise _in_use(exc, n, d)
    _forget(d)
    cur = _name_of(current)
    lib = _library()
    return {"name": new if not cur or cur.lower() == n.lower() else cur,
            "rows": lib["rows"], "table": lib["table"], "names": lib["names"],
            "summary": f"Renamed '{n}' to '{new}'. Its versions and run record "
                       f"went with it."}


def delete_classifier(name: Any, current: Any = "") -> Dict[str, Any]:
    """"Delete" a classifier by MOVING it to classifiers/.deleted/<name>_<stamp>
    — nothing is erased, and the summary says where it went so moving it
    back restores it. Refused while it is training or classifying.
    ``current`` is the name the window has open: when that is the one
    deleted, "name" and "classes" come back empty so the window stops
    showing it; otherwise they are the open classifier's.
    Keys: name, classes, moved_to, rows, table, names, summary."""
    n, d = _existing(name, "delete")
    _refuse_if_busy(d, n, "delete it")
    bin_dir = _classifiers_root() / DELETED
    bin_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = bin_dir / f"{n}_{stamp}"
    k = 2
    while dest.exists():
        dest = bin_dir / f"{n}_{stamp}_{k}"
        k += 1
    try:
        os.rename(d, dest)
    except OSError as exc:
        raise _in_use(exc, n, d)
    _forget(d)
    cur = _name_of(current)
    keep = "" if not cur or cur.lower() == n.lower() else cur
    classes: List[str] = []
    if keep:
        try:
            classes = list(_load(keep)["classes"])
        except RuntimeError:
            keep = ""
    lib = _library()
    return {"name": keep, "classes": classes, "moved_to": str(dest),
            "rows": lib["rows"], "table": lib["table"], "names": lib["names"],
            "summary": f"Deleted '{n}': moved to {dest}. Nothing was erased — "
                       f"move that folder back into {dest.parent.parent} "
                       f"(named {n}) to restore it."}


def list_versions(name: Any) -> Dict[str, Any]:
    """Every version of a classifier, newest first: number, id, when, how
    many frames of each class, and the accuracy measured when it was made.
    Keys: rows, versions (the meta of each), current, summary."""
    n, d = _existing(name, "list the versions of")
    notes = _reconcile(d, n)
    rows, metas = [], []
    for num in reversed(_version_numbers(d)):
        vdir = d / VERSIONS / f"v{num}"
        try:
            meta = _read_meta(vdir)
        except RuntimeError as exc:
            rows.append(f"v{num}   DAMAGED: {str(exc).split('. ')[0]}")
            continue
        metas.append(meta)
        per = ", ".join(f"{c} {k}" for c, k in
                        (meta.get("frames_per_class") or {}).items())
        acc = str(meta.get("accuracy") or "").split(":")[0]
        how = {"migrated": " · from before versions",
               "adopted": " · from an older build",
               "imported": " · imported"}.get(str(meta.get("how")), "")
        rows.append(f"v{num} ({meta['sha256'][:8]})   "
                    f"{str(meta.get('created') or '')[:16]} · "
                    f"{meta.get('frames', '?')} frames: {per}"
                    + (f" · {acc}" if acc else "") + how)
    current = ""
    if rows:
        try:
            current = _vid(n, _newest(d)[0])
        except RuntimeError:
            notes.append("The newest version is damaged — press Train to make "
                         "a new one.")
    note = (" " + " ".join(notes)) if notes else ""
    summary = (f"'{n}' has {len(rows)} version(s); the current model is "
               f"{current}.{note}" if current else
               f"'{n}' has {len(rows)} version(s).{note}" if rows else
               f"'{n}' has not been trained yet.{note}")
    return {"rows": rows, "versions": metas, "current": current,
            "summary": summary}


# ============================================================
# Export and import: one file, to carry a classifier to another PC
# ============================================================

def _peek_export(p: Path) -> bool:
    """Whether ``p`` is a classifier export (so replacing it is safe)."""
    try:
        with zipfile.ZipFile(p) as zf:
            info = zf.getinfo("manifest.json")
            if info.file_size > _EXPORT_LIMITS["manifest.json"]:
                return False
            return json.loads(zf.read(info)).get("format") == EXPORT_FORMAT
    except Exception:                                    # noqa: BLE001
        return False


def _export_target(destination: Any, base: str) -> Path:
    text = str(destination or "").strip().strip('"')
    if not text:
        raise RuntimeError("choose where to save the export first — a folder, "
                           "or a file name")
    p = Path(text).expanduser()
    if p.is_dir():
        out = p / f"{base}{EXPORT_SUFFIX}"
        k = 2
        while out.exists():                 # never over a file already there
            out = p / f"{base} ({k}){EXPORT_SUFFIX}"
            k += 1
        return out
    if not p.name.lower().endswith(".zip"):
        p = p.with_name(p.name + EXPORT_SUFFIX)
    if not p.parent.is_dir():
        raise RuntimeError(f"{p.parent} is not a folder — choose a folder "
                           f"that exists")
    if p.exists() and not _peek_export(p):
        # A save dialog may have asked "replace?", but this is not a
        # classifier export: refusing costs a second try, replacing could
        # cost the user a file.
        raise RuntimeError(f"{p} already exists and is not a classifier "
                           f"export — choose another name (it was not "
                           f"touched)")
    return p


def export_classifier(name: Any, destination: Any) -> Dict[str, Any]:
    """Write the current version as ONE file another PC can import.

    ``destination`` is a folder (the file is named <name>-v<N>.typhon-
    classifier.zip, never over an existing file) or a file name (".typhon-
    classifier.zip" is added when it has no .zip; an existing file is
    replaced only when it is itself a classifier export).
    Keys: path, version, sha256, summary."""
    n, d = _existing(name, "export")
    with _using(d):
        notes = _reconcile(d, n)
        newest = _newest(d)
        if newest is None:
            raise RuntimeError(f"'{n}' has no trained model to export — "
                               f"press Train first")
        meta, vdir = newest
        model_bytes = (vdir / "model.npz").read_bytes()
        model = _read_model_bytes(model_bytes, str(vdir / "model.npz"))
        if _model_sha(model) != meta["sha256"]:
            raise RuntimeError(f"{_vid(n, meta)} is damaged — its model no "
                               f"longer matches its checksum, so it was not "
                               f"exported. Press Train to make a new version.")
        doc = d / "classes.json"
        classes_bytes = (doc.read_bytes() if doc.is_file() else json.dumps(
            {"classes": model["classes"], "labels": {}}).encode("utf-8"))
        try:
            parsed = json.loads(classes_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"cannot read {doc}: {exc} — nothing exported")
        if not isinstance(parsed, dict):
            raise RuntimeError(f"cannot read {doc}: it is not a classifier's "
                               f"classes file — nothing exported")
        meta_bytes = json.dumps(meta, indent=2, ensure_ascii=False).encode()
    vid = _vid(n, meta)
    out = _export_target(destination, f"{n}-v{meta['version']}")
    members = {"classes.json": classes_bytes, "meta.json": meta_bytes,
               "model.npz": model_bytes}
    manifest = {
        "format": EXPORT_FORMAT, "format_version": EXPORT_FORMAT_VERSION,
        "feature_layout": FEATURE_LAYOUT,
        "feature_length": int(model["X"].shape[1]),
        "created": _now(), "source_name": n, "version": meta["version"],
        "version_id": vid, "model_sha256": meta["sha256"],
        "members": {k: hashlib.sha256(v).hexdigest()
                    for k, v in members.items()}}

    def _w(tmp):
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2,
                                                    ensure_ascii=False))
            zf.writestr("classes.json", classes_bytes)
            zf.writestr("meta.json", meta_bytes)
            # Already compressed by numpy: deflating it again is CPU for
            # nothing.
            zf.writestr(zipfile.ZipInfo("model.npz", (1980, 1, 1, 0, 0, 0)),
                        model_bytes, compress_type=zipfile.ZIP_STORED)
    try:
        _atomic_write(out, _w)
    except OSError as exc:
        raise RuntimeError(f"cannot write {out}: {exc}")
    note = (" " + " ".join(notes)) if notes else ""
    return {"path": str(out), "version": vid, "sha256": meta["sha256"],
            "summary": f"Exported {vid} to {out} "
                       f"({max(1, out.stat().st_size // 1024)} KB). On the "
                       f"other PC, press Import and pick this file — the "
                       f"frames are not needed there.{note}"}


def _json_member(data: bytes, what: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"{what} is not readable ({exc}) — nothing was "
                           f"imported")


def _read_export(p: Path) -> Dict[str, Any]:
    """Every member of an export, checked. Nothing is written before this
    returns, and nothing is ever extracted by a name taken from the zip."""
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"cannot read {p}: {exc}")
    if size > MAX_IMPORT_BYTES:
        raise RuntimeError(f"{p.name} is {size >> 20} MB — larger than any "
                           f"classifier export; refused")
    raw = p.read_bytes()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise RuntimeError(f"{p.name} is not a classifier export (not a zip "
                           f"file: {exc})")
    blobs: Dict[str, bytes] = {}
    try:
        with zf:
            infos = zf.infolist()
            if len(infos) > len(_EXPORT_MEMBERS):
                raise RuntimeError(f"{p.name} holds {len(infos)} entries; a "
                                   f"classifier export holds "
                                   f"{len(_EXPORT_MEMBERS)} — refused, "
                                   f"nothing was imported")
            for info in infos:
                if info.filename not in _EXPORT_LIMITS:
                    raise RuntimeError(
                        f"{p.name} holds {info.filename!r}, which is not part "
                        f"of a classifier export — refused, nothing was "
                        f"imported")
                if info.filename in blobs:
                    raise RuntimeError(f"{p.name} holds {info.filename} twice "
                                       f"— refused")
                cap = _EXPORT_LIMITS[info.filename]
                if info.file_size > cap:
                    raise RuntimeError(
                        f"{info.filename} in {p.name} unpacks to "
                        f"{info.file_size:,} bytes, over the {cap:,} limit — "
                        f"refused, nothing was imported")
                with zf.open(info) as fh:
                    data = fh.read(cap + 1)           # bounded, whatever it says
                if len(data) > cap:
                    raise RuntimeError(f"{info.filename} in {p.name} is over "
                                       f"the {cap:,}-byte limit — refused")
                blobs[info.filename] = data
    except RuntimeError:
        raise
    except Exception as exc:                  # CRC mismatch, bad deflate data
        raise RuntimeError(f"{p.name} is damaged ({type(exc).__name__}: "
                           f"{exc}) — nothing was imported")
    lacking = [m for m in _EXPORT_MEMBERS if m not in blobs]
    if lacking:
        raise RuntimeError(f"{p.name} is not a complete classifier export "
                           f"(no {', '.join(lacking)})")

    manifest = _json_member(blobs["manifest.json"], f"manifest.json in {p.name}")
    if not isinstance(manifest, dict) or manifest.get("format") != EXPORT_FORMAT:
        raise RuntimeError(f"{p.name} is not a Typhon classifier export")
    fv = manifest.get("format_version")
    if not isinstance(fv, int) or fv < 1:
        raise RuntimeError(f"{p.name} has no usable format version")
    if fv > EXPORT_FORMAT_VERSION:
        raise RuntimeError(f"{p.name} was written by a newer build (format "
                           f"{fv}; this one reads {EXPORT_FORMAT_VERSION}) — "
                           f"update this copy of the app to import it")
    if (manifest.get("feature_layout") != FEATURE_LAYOUT
            or manifest.get("feature_length") != _feature_length()):
        raise RuntimeError(
            f"{p.name} was made with features this build does not make "
            f"(layout {manifest.get('feature_layout')}, "
            f"{manifest.get('feature_length')} per frame; this build: layout "
            f"{FEATURE_LAYOUT}, {_feature_length()}) — retrain it on this PC "
            f"or bring both PCs to the same version of the app")
    sums = manifest.get("members")
    expected = set(_EXPORT_MEMBERS) - {"manifest.json"}
    if not isinstance(sums, dict) or set(sums) != expected:
        raise RuntimeError(f"{p.name}: the manifest does not list its members "
                           f"— refused")
    for member in sorted(expected):
        if hashlib.sha256(blobs[member]).hexdigest() != str(sums[member]):
            raise RuntimeError(f"{member} in {p.name} does not match its "
                               f"checksum — the file is damaged or was "
                               f"altered; nothing was imported")

    meta = _json_member(blobs["meta.json"], f"meta.json in {p.name}")
    if (not isinstance(meta, dict) or not isinstance(meta.get("version"), int)
            or meta["version"] < 1 or meta["version"] > 999999
            or not _SHA_RE.match(str(meta.get("sha256", "")))
            or meta["sha256"] != manifest.get("model_sha256")):
        raise RuntimeError(f"{p.name}: meta.json does not describe its model "
                           f"— refused")
    doc = _json_member(blobs["classes.json"], f"classes.json in {p.name}")
    if (not isinstance(doc, dict) or not isinstance(doc.get("classes"), list)
            or not isinstance(doc.get("labels", {}), dict)
            or not all(isinstance(c, str) for c in doc["classes"])
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in doc.get("labels", {}).items())):
        raise RuntimeError(f"{p.name}: classes.json is not a classifier's "
                           f"classes — refused")
    stray = {v for v in doc.get("labels", {}).values()} - set(doc["classes"])
    if stray:
        raise RuntimeError(f"{p.name}: a mark names a class that is not in "
                           f"the list ({sorted(stray)[0]!r}) — refused")
    model = _read_model_bytes(blobs["model.npz"], f"model.npz in {p.name}")
    if _model_sha(model) != meta["sha256"]:
        raise RuntimeError(f"model.npz in {p.name} is not the model its "
                           f"meta.json describes — refused")
    if not set(model["classes"]) <= set(doc["classes"]):
        raise RuntimeError(f"{p.name}: the model has classes its classes.json "
                           f"does not — refused")
    return {"manifest": manifest, "meta": meta, "classes": doc,
            "model": model, "model_bytes": blobs["model.npz"],
            "file_sha256": hashlib.sha256(raw).hexdigest()}


def _free_name(base: str) -> str:
    root = _classifiers_root()
    if not (root / base).exists():
        return base
    for k in range(2, 10000):
        suffix = f"-{k}"
        cand = base[:64 - len(suffix)] + suffix
        if not (root / cand).exists():
            return cand
    raise RuntimeError(f"no free name like '{base}' — rename some classifiers")


def import_classifier(path: Any, new_name: Any = "") -> Dict[str, Any]:
    """A classifier from an export file, checked from end to end first (see
    the module docstring). Imported under ``new_name`` when given, else
    under the name it was exported with — and when that is taken, under the
    next free "<name>-2", saying so. Never overwrites. The version keeps its
    number and sha, so "frames v3 (1a2b3c4d)" is the same model on both
    PCs; where it came from is kept with it.
    Keys: name, classes, version, rows, table, names, summary."""
    text = str(path or "").strip().strip('"')
    if not text:
        raise RuntimeError("choose the exported classifier file "
                           f"(*{EXPORT_SUFFIX}) first")
    p = Path(text).expanduser()
    if not p.is_file():
        raise RuntimeError(f"{p} is not a file")
    got = _read_export(p)
    manifest, meta, doc = got["manifest"], got["meta"], got["classes"]
    wanted = _name_of(new_name)
    if wanted:
        store_dir(wanted)                     # a typed name must be valid
    else:
        wanted = str(manifest.get("source_name") or "")
        if not _NAME_RE.match(wanted) or wanted.lower() in _DEVICE_NAMES:
            wanted = "imported"
    origin = {"how": "imported", "file": str(p.resolve()),
              "file_sha256": got["file_sha256"],
              "source_name": str(manifest.get("source_name") or ""),
              "source_version_id": str(manifest.get("version_id") or ""),
              "exported": str(manifest.get("created") or ""),
              "imported": _now()}
    vmeta = dict(meta, how="imported", imported_from=origin)
    if meta.get("how") and meta.get("how") != "imported":
        vmeta["trained_how"] = meta["how"]
    marks = {"classes": list(doc["classes"]),
             "labels": dict(doc.get("labels") or {})}

    def build(staged: Path) -> None:
        staged.mkdir(parents=True)
        _, vdir = _write_version(staged, got["model_bytes"], marks, vmeta,
                                 number=int(meta["version"]))
        _write_mirror(staged, vdir)
        _write_json(staged / "classes.json",
                    dict(marks, version=1, updated=_now(), origin=origin))

    target = ""
    for _ in range(20):
        target = _free_name(wanted)
        try:
            _stage_into_place(build, store_dir(target))
            break
        except FileExistsError:
            continue                    # taken between the check and the move
        except OSError as exc:
            raise RuntimeError(f"cannot import into {store_dir(target)}: {exc}")
    else:
        raise RuntimeError(f"no free name like '{wanted}' — nothing was "
                           f"imported")
    vid = _vid(target, meta)
    lib = _library()
    same = [i["name"] for i in lib["details"]
            if i["sha256"] == meta["sha256"] and i["name"] != target]
    renamed = (f" as '{target}', because '{wanted}' already exists"
               if target != wanted else "")
    frames = len(got["model"]["y"])
    return {"name": target, "classes": list(doc["classes"]), "version": vid,
            "rows": lib["rows"], "table": lib["table"], "names": lib["names"],
            "summary": f"Imported {vid} from {p.name}{renamed}: {frames} "
                       f"trained frames, classes "
                       f"{', '.join(doc['classes'])}. It works without the "
                       f"frames."
                       + (f" It is the same model as '{same[0]}'."
                          if same else "")}


# ============================================================
# The run record
# ============================================================

def _folder_key(folder: Any) -> str:
    return os.path.normcase(str(Path(str(folder).strip().strip('"'))
                                .expanduser().resolve()))


def _runs_files(name: str) -> List[Tuple[Path, bool]]:
    """(runs.jsonl, deleted?) for one classifier, or for every one — those
    in classifiers/.deleted too: what classified a run stays true after
    the classifier is deleted."""
    if name:
        return [(store_dir(name) / RUNS, False)]
    root = _classifiers_root()
    out: List[Tuple[Path, bool]] = []
    if root.is_dir():
        for d in sorted(root.iterdir(), key=lambda e: e.name.lower()):
            if d.is_dir() and _NAME_RE.match(d.name):
                out.append((d / RUNS, False))
        trash = root / DELETED
        if trash.is_dir():
            for d in sorted(trash.iterdir()):
                if d.is_dir():
                    out.append((d / RUNS, True))
    return out


def _read_runs(p: Path) -> Tuple[List[Dict[str, Any]], int]:
    if not p.is_file():
        return [], 0
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"cannot read {p}: {exc}")
    records, bad = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(rec, dict):
            records.append(rec)
        else:
            bad += 1
    return records, bad


def _run_line(rec: Dict[str, Any], with_folder: bool) -> str:
    counts = ", ".join(f"{c} {k}" for c, k in (rec.get("counts") or {}).items())
    runs = list((rec.get("runs") or {}).items())
    which = ", ".join(f"{r} ({k})" for r, k in runs[:3])
    if len(runs) > 3:
        which += f" +{len(runs) - 3} more"
    text = (f"{str(rec.get('when', '?'))[:16]}  {rec.get('version_id', '?')}"
            f"{' (deleted)' if rec.get('deleted') else ''}  "
            f"{rec.get('frames', 0)} frames: {counts}")
    if which:
        text += f"  · runs {which}"
    if with_folder:
        text += f"  · {rec.get('folder', '?')}"
    return text


def run_history(name: Any = "", folder: Any = "") -> Dict[str, Any]:
    """Which classifier version classified which capture runs — newest
    first. By classifier, by folder, or both; with no classifier named,
    every classifier's record is searched (deleted ones included).
    Keys: rows, records, latest, summary."""
    n = _name_of(name)
    f = str(folder or "").strip().strip('"')
    if not n and not f:
        raise RuntimeError("choose a classifier or a folder to see what was "
                           "classified")
    want = _folder_key(f) if f else ""
    records, bad = [], 0
    for p, deleted in _runs_files(n):
        recs, b = _read_runs(p)
        bad += b
        for rec in recs:
            if want and _folder_key(rec.get("folder", "")) != want:
                continue
            records.append(dict(rec, deleted=True) if deleted else rec)
    records.sort(key=lambda r: str(r.get("when", "")), reverse=True)
    rows = [_run_line(r, with_folder=not f) for r in records]
    latest = ""
    if records:
        r = records[0]
        counts = ", ".join(f"{c} {k}" for c, k in (r.get("counts") or {}).items())
        latest = (f"Classified with {r.get('version_id', '?')}"
                  f"{' (since deleted)' if r.get('deleted') else ''} on "
                  f"{str(r.get('when', '?'))[:16]} — {counts}")
    where = f" for {Path(f).name or f}" if f else ""
    who = f" by '{n}'" if n else ""
    summary = (f"{len(records)} classified run(s){who}{where}."
               if records else f"Nothing classified{who}{where} yet.")
    if bad:
        summary += (f" {bad} line(s) of the record could not be read and were "
                    f"skipped (left as they are).")
    return {"rows": rows, "records": records, "latest": latest,
            "summary": summary}


def classified_with(folder: Any) -> Dict[str, Any]:
    """One line for the window: which version last classified this folder,
    and when — from every classifier's record. A blank folder is not an
    error (this may run as soon as a folder box changes): it just says so.
    Keys: classified_with, rows, records, summary."""
    if not str(folder or "").strip():
        return {"classified_with": "", "rows": [], "records": [],
                "summary": "Choose a folder of frames to see what classified "
                           "it."}
    h = run_history("", folder)
    line = h["latest"] or "Not classified yet — press Classify all frames."
    return {"classified_with": line, "rows": h["rows"],
            "records": h["records"], "summary": h["summary"]}
