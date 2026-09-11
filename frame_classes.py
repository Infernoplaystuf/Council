"""
frame_classes.py — make classes, mark frames, train a very simple classifier.

The workflow a GUI binds to buttons:

    open_classifier(name)                         -> the saved classes
    add_class(name, new_class)                    -> classes + 1
    remove_class(name, selection)                 -> classes - 1 (only if unused)
    mark_frame(name, folder, frame, selection)    -> frame labelled with a class
    train(name)                                   -> a model, and how good it is
    predict_frame(name, folder, frame)            -> that frame's likely class
    classify_folder(name, folder)                 -> every frame's likely class

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
the app runs.

"Train" also reports an honest accuracy: leave-one-out (each marked frame
predicted from all the others) up to 20 frames, stratified k-fold beyond —
because a user marking ten frames has no held-out set.

WHERE IT KEEPS THINGS
---------------------
In the vault, never beside the frames:

    <vault>/classifiers/<name>/classes.json   classes + {frame path: class}
    <vault>/classifiers/<name>/model.npz      thumbnails, labels, paths

The capture folder is the raw data and is only ever READ. A classifier is kept
apart from any one folder because the point of training one is to apply it to
the NEXT capture. Files are written atomically (temp file + replace), so a
crash mid-save never leaves a half-written label file.

NOTHING A USER MARKED IS DESTROYED BY THE CLASSIFIER: a class that still labels
frames cannot be removed (re-mark those frames first), and re-marking a frame
is the user deliberately changing that one label.

Failures RAISE RuntimeError with a sentence a user can act on; a generated
handler shows it in a dialog and clears what the button fills. Soft cases that
are not failures — "Add class" with nothing typed — return the current classes
and say what to do, so the class list is not emptied by a stray click.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

THUMB = 32                    # thumbnail edge, pixels
TREES = 100                   # forest size: stable votes, ~0.1 s to fit
BOOTSTRAP_FROM = 30           # below this many marks every tree sees them all
LOO_MAX = 20                  # leave-one-out up to here; k-fold beyond
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif",
                  ".webp")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


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


def store_dir(name: Any) -> Path:
    """<vault>/classifiers/<name>. The name is checked, so it can never
    reach outside that folder ("..\\..\\x" is not a name)."""
    n = str(name or "").strip()
    if not _NAME_RE.match(n):
        raise RuntimeError(
            f"'{n}' is not a usable classifier name — use letters, digits, "
            f"'-' or '_' (e.g. frames)")
    return _vault_root() / "classifiers" / n


def _load(name: Any) -> Dict[str, Any]:
    p = store_dir(name) / "classes.json"
    if not p.is_file():
        return {"version": 1, "classes": [], "labels": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read {p}: {exc}")
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


def _save(name: Any, data: Dict[str, Any]) -> None:
    data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    text = json.dumps(data, indent=2, ensure_ascii=False)
    _atomic_write(store_dir(name) / "classes.json",
                  lambda tmp: Path(tmp).write_text(text, encoding="utf-8"))


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


# Refitting is ~0.1 s, but Predict is pressed repeatedly: keep the last forest
# per model file, keyed by its modification time so a new Train is picked up.
_CACHE: Dict[str, Tuple[float, Any]] = {}


def _load_model(name: Any):
    p = store_dir(name) / "model.npz"
    if not p.is_file():
        raise RuntimeError("no trained model yet — mark some frames in at "
                           "least two classes, then press Train")
    import numpy as np
    m = np.load(p, allow_pickle=False)          # plain arrays only, no pickle
    X, y = m["X"], m["y"]
    classes = [str(c) for c in m["classes"]]
    paths = [str(q) for q in m["paths"]]
    stamp = p.stat().st_mtime
    hit = _CACHE.get(str(p))
    if hit is None or hit[0] != stamp:
        hit = (stamp, _forest(X, y))
        _CACHE[str(p)] = hit
    return hit[1], X, classes, paths


def _nearest(X, x) -> int:
    """Index of the most similar marked frame — shown with each prediction so
    the user can see WHY, which a forest's vote alone does not say."""
    import numpy as np
    return int(np.argmin(((X - x) ** 2).sum(axis=1)))


# ============================================================
# The button functions
# ============================================================

def open_classifier(name: Any) -> Dict[str, Any]:
    """Load a classifier's classes, creating nothing until something is added."""
    data = _load(name)
    trained = (store_dir(name) / "model.npz").is_file()
    return {"classes": list(data["classes"]),
            "summary": (f"Classifier '{str(name).strip()}': "
                        f"{len(data['labels'])} frame(s) marked "
                        f"({_counts_text(data)})"
                        + (" — trained" if trained else " — not trained yet"))}


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
        raise RuntimeError(f"'{cls}' is not a class of '{name}' — Open it "
                           f"again to refresh the list")
    p = _frame_path(folder, frame)
    was = data["labels"].get(str(p))
    data["labels"][str(p)] = cls
    _save(name, data)
    change = f" (was '{was}')" if was and was != cls else ""
    return {"summary": f"{p.name} marked '{cls}'{change}. "
                       f"Marked so far: {_counts_text(data)}."}


def train(name: Any) -> Dict[str, Any]:
    import numpy as np
    data = _load(name)
    used = [c for c, n in _counts(data).items() if n]
    if len(used) < 2:
        raise RuntimeError(f"mark frames in at least two classes before "
                           f"training (marked: {_counts_text(data)})")
    classes = [c for c in data["classes"] if c in used]
    rows, ys, paths, missing = [], [], [], []
    for path, cls in sorted(data["labels"].items()):
        if cls not in classes:
            continue
        if not Path(path).is_file():
            missing.append(Path(path).name)
            continue
        try:
            rows.append(features(path))
        except RuntimeError:
            raise
        except Exception:
            missing.append(Path(path).name)
            continue
        ys.append(classes.index(cls))
        paths.append(path)
    if len({*ys}) < 2:
        raise RuntimeError("fewer than two classes still have readable frames "
                           + (f"(missing: {', '.join(missing[:3])})" if missing else ""))
    X = np.stack(rows).astype(np.float32)
    y = np.asarray(ys, dtype=np.int32)
    _require_sklearn()          # before saving: a model nothing can use is no model
    quality = _accuracy(X, y)
    out = store_dir(name) / "model.npz"

    def _w(tmp):
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, X=X, y=y, classes=np.asarray(classes),
                                paths=np.asarray(paths))
    _atomic_write(out, _w)

    note = f" Skipped {len(missing)} missing frame(s)." if missing else ""
    return {"summary": f"Trained a random forest on {len(y)} frames in "
                       f"{len(classes)} classes. {quality}{note}"}


def _share_text(proba) -> Tuple[int, str]:
    best = int(proba.argmax())
    return best, f"{float(proba[best]):.0%}"


def predict_frame(name: Any, folder: Any, frame: Any) -> Dict[str, Any]:
    forest, X, classes, paths = _load_model(name)
    p = _frame_path(folder, frame)
    x = features(p)
    best, share = _share_text(forest.predict_proba(x[None, :])[0])
    cls = classes[int(forest.classes_[best])]
    near = Path(paths[_nearest(X, x)]).name
    return {"label": cls,
            "summary": (f"{p.name} looks like '{cls}' ({share} of the forest "
                        f"agrees); most similar marked frame {near}.")}


def classify_folder(name: Any, folder: Any) -> Dict[str, Any]:
    """Every image in ``folder``, in capture order, with its likely class.
    A row shows the forest's agreement when it is below 80%, so an uncertain
    call does not read like a certain one."""
    forest, X, classes, _paths = _load_model(name)
    d = Path(str(folder or "").strip().strip('"'))
    if not str(folder or "").strip():
        raise RuntimeError("no folder chosen — pick the folder of frames first")
    if not d.is_dir():
        raise RuntimeError(f"{d} is not a folder")

    def _natkey(p):
        return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower())
                for t in re.split(r"(\d+)", p.name)]

    frames = sorted((p for p in d.iterdir()
                     if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES),
                    key=_natkey)
    if not frames:
        raise RuntimeError(f"no images in {d}")
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
    summary = (f"{len(rows)} frames: "
               + ", ".join(f"{c} {n}" for c, n in tally.items())
               + (f"; {unreadable} unreadable" if unreadable else ""))
    return {"rows": rows, "counts": tally, "summary": summary}
