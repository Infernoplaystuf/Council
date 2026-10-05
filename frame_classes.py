"""
frame_classes.py — make classes, mark frames, train a very simple classifier,
and KEEP the trained models: a library of them that every app using the same
store shares, every version of each, a record of which app made each one, one
file that carries a classifier to another PC (or a whole app's worth of them,
for an app that goes its own way), and a record of which version classified
which capture run.

The workflow a GUI binds to buttons:

    open_classifier(name)                         -> the saved classes, and where it came from
    add_class(name, new_class)                    -> classes + 1
    remove_class(name, selection)                 -> classes - 1 (only if unused)
    mark_frame(name, folder, frame, selection)    -> frame labelled with a class
    train(name)                                   -> a new model VERSION, and how good it is
    predict_frame(name, folder, frame)            -> that frame's likely class
    classify_folder(name, folder)                 -> every frame's likely class, RECORDED

The library. Each of these returns the refreshed list too ("rows" for a
listbox, "table" for a table, "names", "filters"), so one button press
updates the list; ``show`` is the list's filter (see FILTERING):

    list_classifiers(show)                        -> the saved classifiers, origin and tags
    save_as(name, new_name, show)                 -> a copy; never over another
    rename_classifier(name, new_name, current, show) -> renamed; never over another
    delete_classifier(name, current, show)        -> MOVED to <store>/.deleted
    add_tag(name, tag, show)                      -> one of the user's own tags added
    remove_tag(name, tag, show)                   -> ... and taken off again
    import_classifier(path, new_name, show)       -> a classifier, or a bundle, from a file

and, around it:

    list_versions(name)                           -> every version, newest first
    export_classifier(name, destination)          -> one <name>-v<N>.typhon-classifier.zip
    export_classifiers(destination, show)         -> ONE bundle of every classifier shown
    export_this_app(destination)                  -> one bundle of what THIS app made
    run_history(name, folder)                     -> which version classified what
    classified_with(folder)                       -> "Classified with frames v3 on ..."
    store_info()                                  -> where classifiers are kept; which app this is

A `name` may be what an entry holds ("frames") or a list's SELECTION — a
listbox row from list_classifiers ("frames   v3 (1a2b3c4d) · ..."), a table
row, or the same row as a dropdown's text — so the same functions serve a
name box, a model list and a model dropdown.

STANDS ALONE
------------
This module imports nothing of the Council's — only the standard library,
numpy, Pillow and scikit-learn. The two Council rules it needs, where the
vault is (gui_projects.resolve_vault_root) and which project folder the
running app is (gui_settings.app_folder), are restated here rather than
imported, and tests/test_frame_classes.py holds each pair together. That is
what lets a generated app, or an app that later leaves the Council behind,
carry this one file and keep its classifiers working; a test runs it from a
folder holding nothing else.

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
In ONE store, never beside the frames. classifier_store() is the only place
that decides where; by default it is the vault, which every app on this PC
shares:

    <store> = $FRAME_CLASSES_STORE                          if that is set, else
              "store" of classifier_store.json beside the app  if there is one, else
              <vault>/classifiers

    <store>/<name>/classes.json            classes + {frame path: class}
    <store>/<name>/about.json              origin (which app made it), lineage, tags
    <store>/<name>/versions/v<N>/model.npz  features, labels, paths
    <store>/<name>/versions/v<N>/classes.json  the marks it was trained on
    <store>/<name>/versions/v<N>/meta.json  number, sha256, when, frames per
                                            class, accuracy, feature layout
    <store>/<name>/model.npz               copy of the CURRENT version
    <store>/<name>/runs.jsonl              one line per classified run
    <store>/.deleted/<name>_<stamp>/       what Delete moved aside

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

ORIGIN AND TAGS
---------------
The store is shared, so every classifier says where it came from. Its
ORIGIN is recorded once, in about.json, when it is created — when its first
class is added — and is never rewritten afterwards (_write_about refuses):

    app         the app's name as its window shows it ("Typhon")
    project     the Designer project it was built as ("example_typhon")
    project_id  a stable id for that project (see _project_id)
    example     the shipped example the project was built from, if any
    created     when;  host: this PC's name (the host name, nothing more)

Outside a Designer app (a script, a test) "app" and "project" are empty and
"script" names what ran. A classifier made before origins were recorded has
NO about.json, and its origin reads "unknown (made before origin tags)" —
never filled in from whichever app opens it next, which would be a guess
written down as a record.

What happens to a classifier afterwards is appended to its LINEAGE, never
written over the origin: Save as keeps the origin and adds "copied from
frames v3 (1a2b3c4d)"; Rename keeps it and adds "renamed from frames";
Export carries all of it; Import keeps the file's origin and adds "imported
from frames-v3.typhon-classifier.zip on 2026-10-05". TAGS are the user's
own words (add_tag / remove_tag), separate from the origin.

Names are unique in a store whatever their case ("Frames" is "frames" on
Windows, so it is on every PC). A name that is taken is never overwritten:
Save as, Rename and Import refuse and ask for another name, saying which app
the existing one came from.

FILTERING
---------
``show`` narrows the list, so a store many apps use stays readable:

    "" / "All classifiers"     everything
    "This app"                 what the running app made (same project id, or
                               same project and app name — a rebuilt project
                               keeps its classifiers)
    "App: Typhon"              made by any app called Typhon
    "Project: example_typhon"  made by that project
    "Tag: night shift"         tagged so by the user ("#night shift" too)
    "Origin unknown"           made before origins were recorded
    anything else              that word as an app, project or tag, or in a name

"filters" in every library result lists the choices that exist, for a
dropdown.

THE LIBRARY
-----------
Save as copies (classes, every version, origin and tags; not the run record,
which is the original's history) and switches to the copy. Delete never
deletes: it MOVES the folder to <store>/.deleted/<name>_<stamp> and says
where, so moving it back restores it. Rename and Delete are refused while
this process is training or classifying with that classifier, and Windows
refuses to move a folder with a file open inside it — reported as "in use by
another program".

MOVING A CLASSIFIER TO ANOTHER PC
---------------------------------
export_classifier writes ONE file, <name>-v<N>.typhon-classifier.zip, that
needs nothing else to be understood — format "typhon-classifier", version 1:

    manifest.json  format, format_version, feature_layout, feature_length,
                   "features" and "model" (how a frame becomes numbers, how
                   the forest is rebuilt — in words, for a reader that is
                   not this module), name, version, version_id,
                   model_sha256, origin, lineage, tags, and the sha256 of
                   every other member
    model.npz      the current version's arrays (allow_pickle=False)
    meta.json      that version's meta
    classes.json   the marks
    runs.jsonl     the run record, when there is one
    README.txt     all of this, for a person or a program written later

The frame paths inside are the exporting PC's — the model does not need the
frames, because the forest is refit from the stored features.

import_classifier trusts nothing in the file: only those member names
(nothing is ever extracted by a name from the zip, so a "../" name cannot
land anywhere), size and member-count caps checked before reading, every
checksum, every array header before numpy allocates, allow_pickle=False, the
shapes and dtypes consistent, and the feature length equal to what THIS
build's features() produces. It never overwrites (see ORIGIN AND TAGS).

AN APP OF ITS OWN
-----------------
An app may later run without the Council and its vault. Nothing here has to
change for that:

  * point its store at a folder of its own — set FRAME_CLASSES_STORE, or put
    classifier_store.json ({"store": "classifiers"}, relative to that file)
    beside the app;
  * export_this_app (or export_classifiers with a filter) writes everything
    one app or project made into ONE bundle, <label>-<stamp>.typhon-
    classifiers.zip: bundle.json (format "typhon-classifier-bundle",
    version 1, listing each classifier, its origin and checksums), a
    README.txt, and one complete single-classifier export per classifier;
  * the independent app imports that bundle into its own store with the
    same import_classifier, keeping every origin and adding the import to
    each lineage. A name already taken there is skipped and named; a prefix
    typed as the new name brings those in as <prefix><name>.

THE RUN RECORD
--------------
classify_folder appends one line to the classifier's runs.jsonl: when, the
folder, each capture run in it (the "<stamp>" of "<stamp>_frame_000001.png")
with its frame count, the classifier, the version id and sha, the count per
class, this PC's name and the app that ran it. run_history and
classified_with read it back, so the app can say "Classified with frames v3
(1a2b3c4d) on 2026-10-02 14:03". The record lives in the store: the capture
folder is never written to. A folder is a path on ONE PC, so the record of
another PC (carried in by an import) never answers for a folder here.

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
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

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

#: Where the store is, when it is not the vault's (see classifier_store).
STORE_ENV = "FRAME_CLASSES_STORE"
STORE_CONFIG = "classifier_store.json"

VERSIONS = "versions"
RUNS = "runs.jsonl"
ABOUT = "about.json"
DELETED = ".deleted"
_MODEL_ARRAYS = ("X", "y", "classes", "paths")
_VERSION_RE = re.compile(r"^v([1-9][0-9]{0,5})$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

UNKNOWN_ORIGIN = "unknown (made before origin tags)"
ABOUT_FORMAT = "typhon-classifier-about"
TAG_MAX_LEN = 40
TAGS_MAX = 20
_LINEAGE_MAX = 1000

FILTER_ALL = "All classifiers"
FILTER_THIS_APP = "This app"
FILTER_UNKNOWN = "Origin unknown"

EXPORT_FORMAT = "typhon-classifier"
EXPORT_FORMAT_VERSION = 1
EXPORT_SUFFIX = ".typhon-classifier.zip"
# member -> the most it may unpack to. One feature row is 1092 float32s
# (4.4 KB), so 512 MB of arrays is ~100,000 marked frames — far past a person
# marking by hand — while a zip bomb is refused before it is inflated.
_EXPORT_LIMITS = {"manifest.json": 1 << 20, "meta.json": 1 << 20,
                  "classes.json": 64 << 20, "model.npz": 512 << 20,
                  "runs.jsonl": 64 << 20, "README.txt": 64 << 10}
_EXPORT_REQUIRED = ("manifest.json", "meta.json", "classes.json", "model.npz")
MAX_IMPORT_BYTES = 600 << 20          # the export file itself, on disk
MAX_MODEL_BYTES = 1 << 30             # all arrays of one model, unpacked

BUNDLE_FORMAT = "typhon-classifier-bundle"
BUNDLE_FORMAT_VERSION = 1
BUNDLE_SUFFIX = ".typhon-classifiers.zip"
MAX_BUNDLE_BYTES = 1 << 30            # a bundle on disk, and all it unpacks to
MAX_BUNDLE_MEMBERS = 500
_BUNDLE_MEMBER_RE = re.compile(r"^classifiers/[A-Za-z0-9][A-Za-z0-9_\-]{0,63}"
                               + re.escape(EXPORT_SUFFIX) + "$")

#: How a frame becomes numbers, in words — written into every export so a
#: program written later, without this module, can compute the same thing.
FEATURE_RECIPE = (
    "feature_layout 1: each frame becomes feature_length (1092) float32 "
    "numbers. Convert it to 8-bit greyscale (a 16-bit or float image is "
    "scaled by 1/256 first, not clamped; anything else with Pillow's "
    "convert('L')), resize to 32x32 with bilinear filtering and divide by "
    "255: 1024 numbers, row by row. Then the 32 row means of that 32x32 "
    "image, then its 32 column means, then its mean, its standard "
    "deviation, the share of values below 0.2 and the share above 0.8.")
MODEL_RECIPE = (
    "The forest is not stored: fit scikit-learn's "
    "RandomForestClassifier(n_estimators=100, bootstrap=(len(y) >= 30), "
    "class_weight='balanced', random_state=0, n_jobs=1) on X and y from "
    "model.npz; a prediction p is the class classes[p]. With the same "
    "scikit-learn this gives the same forest every time.")


# ============================================================
# Which PC, which app
# ============================================================

def _host() -> str:
    """This PC's name: the host name alone, no domain — the one thing about
    the PC an origin or a run record says."""
    name = os.environ.get("COMPUTERNAME", "")
    if not name and hasattr(os, "uname"):
        try:
            name = os.uname().nodename
        except OSError:
            name = ""
    return name.split(".")[0].strip()


def _looks_like_project(folder: Path) -> bool:
    """gui_settings._looks_like_project — restated (see STANDS ALONE)."""
    return (folder / "handlers.py").is_file() and (folder / "ui").is_dir()


def _app_folder() -> Optional[Path]:
    """The running app's project folder, or None when this is not one.

    gui_settings.app_folder's rule, restated (see STANDS ALONE): the folder
    of the `app` or `handlers` module a generated main.py imports, else
    __main__'s, else argv[0]'s — whichever first holds handlers.py and ui/.
    """
    candidates = []
    for name in ("app", "handlers", "__main__"):
        path = getattr(sys.modules.get(name), "__file__", None)
        if path:
            candidates.append(Path(path))
    if sys.argv and sys.argv[0]:
        candidates.append(Path(sys.argv[0]))
    for path in candidates:
        try:
            folder = path.resolve().parent
        except OSError:
            continue
        if _looks_like_project(folder):
            return folder
    return None


def _project_id(name: str, created: str) -> str:
    """A stable id for a Designer project: a hash of its name and the moment
    it was created, the two things create() writes ONCE into manifest.json.

    Derived, because there is nowhere this module could store one: the
    Designer's load_manifest drops keys it does not know, so an id written
    into manifest.json from here would vanish at the next Generate. The two
    values never change afterwards, so the id is the same after every
    Generate and on every PC the project folder is copied to — and a NEW
    project of the same name (built again, or with --force) gets a new one,
    which is right: it is another project. "" when the manifest has no
    creation time, because an id from the name alone would call two
    different projects one."""
    if not (name and created):
        return ""
    text = f"designer-project\n{name}\n{created}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _json_or_empty(p: Path) -> Dict[str, Any]:
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _script_name() -> str:
    """What ran, when it is not a Designer app: "x.py", or the package of a
    `python -m pkg` ("pytest", not "__main__.py"). "" for `python -c`."""
    arg = sys.argv[0] if sys.argv else ""
    if not arg or arg in ("-c", "-m"):
        return ""
    p = Path(arg)
    return p.parent.name if p.name == "__main__.py" else p.name


_APP_CACHE: Dict[Tuple[str, int, int], Dict[str, str]] = {}


def _this_app() -> Dict[str, str]:
    """Which app is running this — what a classifier made now will say it
    came from, and what the "This app" filter means.

    Read from the project folder itself: manifest.json (name, created,
    example) and project.gspec (the window's title, which is what the user
    calls the app). A file that cannot be read leaves its fields empty
    rather than guessed."""
    folder = _app_folder()
    if folder is None:
        return {"app": "", "project": "", "project_id": "", "example": "",
                "script": _script_name()}
    stamps = []
    for f in ("manifest.json", "project.gspec"):
        try:
            stamps.append((folder / f).stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    key = (os.path.normcase(str(folder)), stamps[0], stamps[1])
    if key not in _APP_CACHE:
        man = _json_or_empty(folder / "manifest.json")
        gspec = _json_or_empty(folder / "project.gspec")
        project = str(man.get("name") or folder.name)
        window = gspec.get("window")
        title = (str(window.get("title") or "").strip()
                 if isinstance(window, dict) else "")
        _APP_CACHE.clear()
        _APP_CACHE[key] = {
            "app": title if title and title != "Untitled" else project,
            "project": project,
            "project_id": _project_id(project, str(man.get("created") or "")),
            "example": str(man.get("example") or ""), "script": ""}
    return dict(_APP_CACHE[key])


def _app_label(me: Dict[str, Any]) -> str:
    """"Typhon (project example_typhon)", "the script x.py", ..."""
    app, project = str(me.get("app") or ""), str(me.get("project") or "")
    if project:
        return app if app in ("", project) else f"{app} (project {project})"
    if me.get("script"):
        return f"the script {me['script']}"
    return "Python, outside any app"


# ============================================================
# The store
# ============================================================

def _vault_root() -> Path:
    """The Council's vault: $COUNCIL_VAULT_ROOT, else ~/.council/vault.

    gui_projects.resolve_vault_root's rule, RESTATED rather than imported
    (see STANDS ALONE); a test holds the two together."""
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".council" / "vault"


def _config_folders() -> List[Path]:
    """Where a classifier_store.json may sit: the running app's project
    folder, then the folder of the script that was run (an app of its own
    may not look like a Designer project at all)."""
    out: List[Path] = []
    app = _app_folder()
    if app is not None:
        out.append(app)
    arg = sys.argv[0] if sys.argv else ""
    if arg and arg not in ("-c", "-m"):
        try:
            p = Path(arg).resolve()
            if p.is_file() and p.parent not in out:
                out.append(p.parent)
        except OSError:
            pass
    return out


def _store_from_config(cfg: Path) -> Path:
    try:
        doc = json.loads(cfg.read_text(encoding="utf-8"))
        text = (str(doc.get("store") or "").strip()
                if isinstance(doc, dict) else "")
        why = 'it names no folder (e.g. {"store": "classifiers"})'
    except (OSError, ValueError) as exc:
        text, why = "", f"it cannot be read ({exc})"
    if not text:
        # Not quietly the vault instead: whoever wrote this file wanted the
        # classifiers somewhere else, and new ones landing in the shared
        # vault would be found missing later, in the wrong place.
        raise RuntimeError(f"{cfg} says where this app keeps its "
                           f"classifiers, but {why}. Fix it or move it "
                           f"away; nothing was looked for anywhere else.")
    p = Path(text).expanduser()
    return p if p.is_absolute() else cfg.parent / p


def _store_choice() -> Tuple[Path, str]:
    env = os.environ.get(STORE_ENV, "").strip().strip('"')
    if env:
        p = Path(env).expanduser()
        if not p.is_absolute():
            p = (_app_folder() or Path.cwd()) / p
        return p, f"set by {STORE_ENV}"
    for folder in _config_folders():
        cfg = folder / STORE_CONFIG
        if cfg.is_file():
            return _store_from_config(cfg), f"set by {cfg}"
    return _vault_root() / "classifiers", "the vault, shared by every app"


def classifier_store() -> Path:
    """THE folder classifiers are kept in — decided here and nowhere else.

    $FRAME_CLASSES_STORE when it is set (relative to the app's folder);
    else the "store" of a classifier_store.json beside the app (relative to
    that file); else <vault>/classifiers, which every app on this PC shares.
    The first two are how an app that leaves the Council keeps classifiers
    of its own (see AN APP OF ITS OWN)."""
    return _store_choice()[0]


def _name_of(value: Any) -> str:
    """A classifier name from what a port holds.

    An entry gives the name itself. A listbox gives its SELECTION, a list of
    rows; a list_classifiers row starts with the name ("frames   v3 ..."),
    and a table row is a tuple whose first cell is the name. A dropdown
    gives a row as plain text, recognised by the row's own separators — so
    "has space" typed in a name box stays an invalid name, and is not
    quietly turned into "has"."""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        first = value[0]
        if isinstance(first, (list, tuple)):   # a table's selection: rows
            first = first[0] if first else ""
        text = str(first).strip()
        return text.split()[0] if text else ""
    text = str(value or "").strip()
    if "   " in text or " · " in text:
        return text.split()[0]
    return text


def _check_name(n: str) -> str:
    if not _NAME_RE.match(n) or n.lower() in _DEVICE_NAMES:
        raise RuntimeError(
            f"'{n}' is not a usable classifier name — use letters, digits, "
            f"'-' or '_' (e.g. frames)")
    return n


def store_dir(name: Any) -> Path:
    """<store>/<name>. The name is checked, so it can never reach outside
    that folder ("..\\..\\x" is not a name)."""
    return classifier_store() / _check_name(_name_of(name))


def _load(name: Any) -> Dict[str, Any]:
    p = store_dir(name) / "classes.json"
    if not p.is_file():
        return {"version": 1, "classes": [], "labels": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read {p}: {exc}")
    if isinstance(data, dict):
        data.setdefault("classes", [])
        data.setdefault("labels", {})
    if (not isinstance(data, dict) or not isinstance(data["classes"], list)
            or not isinstance(data["labels"], dict)):
        raise RuntimeError(f"cannot read {p}: it is not a classifier's "
                           f"classes file (left as it is)")
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


#: Windows refuses a path of this many characters or more unless long paths
#: are switched on for the whole PC — and says only "No such file or
#: directory". MEASURED: a store under this session's temp folder (188
#: characters deep) failed exactly so on the first Train.
_WIN_MAX_PATH = 260


def _os_error(exc: OSError,
              fix: str = "keep the classifiers in a shorter folder (see "
                         "classifier_store), or use a shorter name") -> str:
    """An OSError as words, naming the real cause — and ``fix`` — when
    Windows refused a path for its LENGTH."""
    text = str(exc)
    longest = max(len(str(getattr(exc, a, "") or ""))
                  for a in ("filename", "filename2"))
    if os.name == "nt" and longest >= _WIN_MAX_PATH:
        text += (f" — that path is {longest} characters and Windows refuses "
                 f"{_WIN_MAX_PATH} or more: {fix}")
    return text


_SHORTER_EXPORT = "save it in a shorter folder"


def _is_new(d: Path) -> bool:
    """Nothing of this classifier exists yet — so saving now CREATES it."""
    try:
        return not d.exists() or not any(d.iterdir())
    except OSError:
        return False


def _save(name: Any, data: Dict[str, Any]) -> None:
    d = store_dir(name)
    try:
        if _is_new(d):
            # The moment a classifier comes to exist: its origin is recorded
            # now, by the app doing it, and only now (see ORIGIN AND TAGS).
            # The name is checked against every other whatever its case,
            # because a store copied to Windows would merge "Frames" into
            # "frames".
            n = _name_of(name)
            clash = _taken(n)
            if clash and clash != n:
                raise RuntimeError(_clash_text(clash))
            _write_about(d, {"origin": _new_origin(), "lineage": [],
                             "tags": []})
        data["updated"] = _now()
        _write_json(d / "classes.json", data)
    except OSError as exc:
        raise RuntimeError(f"cannot save '{d.name}': {_os_error(exc)}")


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
# Origin, lineage and tags
# ============================================================

def _new_origin() -> Dict[str, Any]:
    return dict(_this_app(), created=_now(), host=_host())


def _unknown_origin() -> Dict[str, Any]:
    return {"app": UNKNOWN_ORIGIN, "project": "", "project_id": "",
            "example": "", "script": "", "created": "", "host": "",
            "unknown": True}


def _origin_text(o: Dict[str, Any]) -> str:
    """"Typhon (project example_typhon) on LAB-PC, 2026-10-05"."""
    if not o or o.get("unknown"):
        return UNKNOWN_ORIGIN
    where = f" on {o['host']}" if o.get("host") else ""
    when = f", {str(o['created'])[:10]}" if o.get("created") else ""
    return f"{_app_label(o)}{where}{when}"


def _origin_short(o: Dict[str, Any]) -> str:
    """For a list row: "from Typhon (example_typhon)"."""
    if not o or o.get("unknown"):
        return "origin unknown"
    app, project = str(o.get("app") or ""), str(o.get("project") or "")
    if project:
        return f"from {app}" if app in ("", project) else \
            f"from {app} ({project})"
    if o.get("script"):
        return f"from {o['script']}"
    return "from Python, outside any app"


def _event_text(e: Dict[str, Any]) -> str:
    when = str(e.get("when") or "")[:10]
    on = f" on {when}" if when else ""
    kind = e.get("event")
    if kind == "copied":
        return f"copied from {e.get('from_version') or e.get('from')}{on}"
    if kind == "renamed":
        return f"renamed from {e.get('from')}{on}"
    if kind == "imported":
        return f"imported from {e.get('file')}{on}"
    return f"{kind}{on}"


def _event(kind: str, **fields: Any) -> Dict[str, Any]:
    """One lineage entry: what happened, when, on which PC, by which app."""
    return dict(fields, event=kind, when=_now(), host=_host(),
                by=_app_label(_this_app()))


def _flat(obj: Any, what: str) -> Dict[str, Any]:
    """A record of plain values — what an origin or a lineage entry is.
    Anything else in one (a nested object, a 100 KB string) is refused:
    these come from files another PC wrote."""
    if not isinstance(obj, dict) or len(obj) > 40:
        raise ValueError(f"{what} is not a record")
    out: Dict[str, Any] = {}
    for k, v in obj.items():
        if not isinstance(k, str) or len(k) > 64:
            raise ValueError(f"{what} has a key that is not a short name")
        if isinstance(v, str):
            if len(v) > 4096:
                raise ValueError(f"{what}: '{k}' is too long")
        elif v is not None and not isinstance(v, (bool, int, float)):
            raise ValueError(f"{what}: '{k}' is not a plain value")
        out[k] = v
    return out


def _clean_tags(tags: Any) -> List[str]:
    if not isinstance(tags, list) or len(tags) > TAGS_MAX:
        raise ValueError(f"the tags are not a list of at most {TAGS_MAX}")
    out: List[str] = []
    for t in tags:
        if (not isinstance(t, str) or not t.strip() or len(t) > TAG_MAX_LEN
                or not t.isprintable()):
            raise ValueError("a tag is not a short line of text")
        if t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out


def _checked_about(doc: Any, origin_required: bool = True) -> Dict[str, Any]:
    """origin, lineage and tags, each checked — or ValueError."""
    if not isinstance(doc, dict):
        raise ValueError("it is not a record")
    origin = doc.get("origin")
    if origin is None and not origin_required:
        origin = _unknown_origin()
    origin = _flat(origin, "the origin")
    lineage = doc.get("lineage") or []
    if not isinstance(lineage, list) or len(lineage) > _LINEAGE_MAX:
        raise ValueError("the lineage is not a list")
    return {"origin": origin,
            "lineage": [_flat(e, "a lineage entry") for e in lineage],
            "tags": _clean_tags(doc.get("tags") or [])}


def _read_about(d: Path) -> Dict[str, Any]:
    """The origin, lineage and tags of the classifier in ``d``.

    No about.json: made before origins were recorded, so the origin is
    UNKNOWN ("recorded" False). A damaged one RAISES and is left exactly as
    it is — replacing it would replace the one record of where this came
    from."""
    p = d / ABOUT
    if not p.is_file():
        return {"origin": _unknown_origin(), "lineage": [], "tags": [],
                "recorded": False}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{p} cannot be read ({exc}) — it records where "
                           f"this classifier came from, so it was left as "
                           f"it is. Restore it from a backup or an export.")
    try:
        about = _checked_about(doc)
    except ValueError as exc:
        raise RuntimeError(f"{p} is damaged ({exc}) — it was left as it is. "
                           f"Restore it from a backup or an export.")
    about["recorded"] = True
    return about


def _write_about(d: Path, about: Dict[str, Any]) -> None:
    """Save origin, lineage and tags. The ORIGIN may never change: a write
    that would change it is refused here, whoever asks — the one place it
    is stored is the one place it is guarded."""
    p = d / ABOUT
    if p.is_file():
        old = _read_about(d)                 # damaged: raises, not replaced
        if old["origin"] != about["origin"]:
            raise RuntimeError(f"the origin of '{d.name}' is recorded once "
                               f"and never rewritten — nothing was saved")
    _write_json(p, {"format": ABOUT_FORMAT, "format_version": 1,
                    "origin": about["origin"],
                    "lineage": list(about.get("lineage") or []),
                    "tags": list(about.get("tags") or [])})


def _taken(n: str) -> Optional[str]:
    """The existing classifier the name ``n`` would clash with, whatever
    its case — or None."""
    root = classifier_store()
    try:
        entries = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        entries = []
    for e in entries:
        if e.name.lower() == n.lower():
            return e.name
    return None


def _clash_text(existing: str) -> str:
    try:
        whose = _origin_text(_read_about(classifier_store() / existing)["origin"])
    except RuntimeError:
        whose = "its origin cannot be read"
    return (f"'{existing}' already exists (from {whose}) — choose another "
            f"name (nothing was overwritten)")


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
        # Written straight into the temporary folder, not via _write_json's
        # own temp file: renaming the FOLDER is the one atomic step, and a
        # temp file inside a temp folder only made the deepest path longer
        # (Windows refuses paths over 260 characters).
        (tmp / "model.npz").write_bytes(model_bytes)
        (tmp / "classes.json").write_text(
            json.dumps(classes_doc, indent=2, ensure_ascii=False),
            encoding="utf-8")
        nums = _version_numbers(d)
        n = number or ((nums[-1] + 1) if nums else 1)
        for _ in range(1000):
            target = vroot / f"v{n}"
            if not target.exists():
                full = dict(meta, version=n)
                (tmp / "meta.json").write_text(
                    json.dumps(full, indent=2, ensure_ascii=False),
                    encoding="utf-8")
                try:
                    os.rename(tmp, target)
                    return full, target
                except OSError:
                    if not target.exists():
                        raise
            n += 1
        raise RuntimeError(f"cannot find a free version number in {vroot}")
    except OSError as exc:
        raise RuntimeError(f"cannot save a new version in {vroot}: "
                           f"{_os_error(exc)}")
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
    except (OSError, ValueError, TypeError):
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
        return ["Note: model.npz (the copy kept for older builds) cannot be "
                "read and was left as it is; the next Train sets it aside."]
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

    Says where it came from — in a store many apps share, the classifier
    called "frames" may be another app's. A model trained before versions
    existed becomes v1 here, the first time it is opened.

    Keys: name, classes, version, origin, tags, summary
    """
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
    origin, tags = "", []
    if _is_new(d):
        made = (f" It is new: it will be recorded as made by "
                f"{_app_label(_this_app())} when its first class is added.")
    else:
        try:
            about = _read_about(d)
            origin, tags = _origin_text(about["origin"]), about["tags"]
            made = (f" From {origin}"
                    + (f"; tags {', '.join(tags)}" if tags else "") + ".")
        except RuntimeError as exc:
            made = f" Where it came from cannot be read: {exc}"
    trained = (f" — trained, current model {version}" if version
               else " — not trained yet")
    return {"name": n, "classes": list(data["classes"]), "version": version,
            "origin": origin, "tags": list(tags),
            "summary": (f"Classifier '{n}': {len(data['labels'])} frame(s) "
                        f"marked ({_counts_text(data)}){trained}.{made}"
                        + (f" {note}" if note else ""))}


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

    Keys: summary, version, sha256
    """
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
                "host": _host(), "by": _app_label(_this_app()),
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

    Keys: label, version, summary
    """
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

    Each call is RECORDED in the classifier's runs.jsonl (in the store —
    the capture folder is only read): when, the folder, each capture run in
    it, the version id and sha, the count per class, this PC and the app.

    Keys: rows, counts, version, sha256, summary
    """
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
        me = _this_app()
        why = _append_run(d, {
            "when": _now(), "folder": str(f.resolve()), "runs": runs,
            "frames": len(rows), "unreadable": unreadable, "classifier": n,
            "version": meta["version"], "version_id": vid,
            "sha256": meta["sha256"], "counts": tally, "host": _host(),
            "by": {k: me[k] for k in ("app", "project", "project_id",
                                      "script")}})
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
        "updated": "", "origin": _unknown_origin(), "origin_text": "",
        "lineage": [], "history": [], "tags": [], "problem": ""}
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
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            info["problem"] = f"classes.json cannot be read ({exc})"
    try:
        about = _read_about(d)
        info.update(origin=about["origin"], lineage=about["lineage"],
                    tags=about["tags"],
                    history=[_event_text(e) for e in about["lineage"]])
    except RuntimeError as exc:
        info["problem"] = info["problem"] or str(exc).split(" — ")[0]
        info["origin"] = {}
    info["origin_text"] = (_origin_text(info["origin"]) if info["origin"]
                           else "origin cannot be read")
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


def _state_text(info: Dict[str, Any]) -> str:
    if info["version"]:
        return f"v{info['version']} ({info['sha256'][:8]})"
    return "trained (before versions)" if info["trained"] else "not trained"


def _row_text(info: Dict[str, Any]) -> str:
    """One listbox row. The NAME COMES FIRST and has no spaces, so a row
    picked in the list names its classifier (see _name_of)."""
    k = len(info["classes"])
    origin = (_origin_short(info["origin"]) if info["origin"]
              else "origin cannot be read")
    text = (f"{info['name']}   {_state_text(info)} · {k} "
            f"class{'' if k == 1 else 'es'} · {info['frames']} marked · "
            f"{origin}")
    if info["tags"]:
        text += f" · tags: {', '.join(info['tags'])}"
    if info["updated"]:
        text += f" · {info['updated'][:16]}"
    if info["problem"]:
        text += f" · PROBLEM: {info['problem']}"
    return text


def _parse_show(show: Any) -> Tuple[str, str]:
    """A filter — what a filter box or dropdown holds — as (kind, value).
    See FILTERING in the module docstring."""
    text = (_picked(show) if isinstance(show, (list, tuple))
            else str(show or "").strip())
    low = text.lower()
    if low in ("", "all", "*", FILTER_ALL.lower()):
        return "all", ""
    if low.startswith(FILTER_THIS_APP.lower()):
        return "this", ""
    if low in (FILTER_UNKNOWN.lower(), "unknown", "origin: unknown"):
        return "unknown", ""
    head, sep, rest = text.partition(":")
    if sep and head.strip().lower() in ("app", "project", "tag"):
        return head.strip().lower(), rest.strip()
    if text.startswith("#"):
        return "tag", text[1:].strip()
    return "any", text


def _same_app(origin: Dict[str, Any], me: Dict[str, str]) -> bool:
    """Whether ``origin`` is the running app's: the same project id — or,
    for a project built again (a new id), the same project AND app name."""
    if not origin or origin.get("unknown"):
        return False
    if me.get("project_id") and origin.get("project_id") == me["project_id"]:
        return True
    if me.get("project"):
        return (origin.get("project") == me["project"]
                and origin.get("app") == me["app"])
    return (bool(me.get("script")) and not origin.get("project")
            and origin.get("script") == me["script"])


def _matches(info: Dict[str, Any], how: str, value: str,
             me: Dict[str, str]) -> bool:
    o, v = info["origin"] or {}, value.lower()
    tags = [t.lower() for t in info["tags"]]
    if how == "all":
        return True
    if how == "this":
        return _same_app(o, me)
    if how == "unknown":
        return bool(o.get("unknown"))
    if how == "tag":
        return v in tags
    known = not o.get("unknown")
    if how == "app":
        return known and str(o.get("app") or "").lower() == v
    if how == "project":
        return known and str(o.get("project") or "").lower() == v
    words = ({str(o.get(k) or "").lower() for k in ("app", "project",
                                                     "example")}
             if known else set())
    return v in words or v in tags or v in info["name"].lower()


def _show_label(how: str, value: str, me: Dict[str, str]) -> str:
    if how == "all":
        return FILTER_ALL
    if how == "this":
        return f"{FILTER_THIS_APP} ({_app_label(me)})"
    if how == "unknown":
        return FILTER_UNKNOWN
    if how == "any":
        return f"'{value}'"
    return f"{how.capitalize()}: {value}"


def _filters(found: List[Dict[str, Any]]) -> List[str]:
    """The filter choices that exist in this store, for a dropdown."""
    apps: List[str] = []
    projects: List[str] = []
    tags: List[str] = []
    unknown = False
    for i in found:
        o = i["origin"] or {}
        if o.get("unknown"):
            unknown = True
        else:
            if o.get("app") and o["app"] not in apps:
                apps.append(str(o["app"]))
            if o.get("project") and o["project"] not in projects:
                projects.append(str(o["project"]))
        for t in i["tags"]:
            if t.lower() not in (x.lower() for x in tags):
                tags.append(t)
    out = [FILTER_ALL, FILTER_THIS_APP]
    out += [f"App: {a}" for a in sorted(apps, key=str.lower)]
    out += [f"Project: {p}" for p in sorted(projects, key=str.lower)]
    out += [f"Tag: {t}" for t in sorted(tags, key=str.lower)]
    if unknown:
        out.append(FILTER_UNKNOWN)
    return out


def _library(show: Any = "") -> Dict[str, Any]:
    root = classifier_store()
    found = []
    if root.is_dir():
        for d in sorted(root.iterdir(), key=lambda e: e.name.lower()):
            if d.is_dir() and _NAME_RE.match(d.name):
                found.append(_describe(d))
    me = _this_app()
    how, value = _parse_show(show)
    shown = [i for i in found if _matches(i, how, value, me)]
    return {"details": shown, "total": len(found),
            "label": _show_label(how, value, me), "filtered": how != "all",
            "names": [i["name"] for i in shown],
            "rows": [_row_text(i) for i in shown],
            "table": [(i["name"], _state_text(i), i["origin_text"],
                       ", ".join(i["tags"]), ", ".join(i["classes"]),
                       str(i["frames"]), i["updated"][:16]) for i in shown],
            "filters": _filters(found)}


def _listed(lib: Dict[str, Any]) -> Dict[str, Any]:
    """The keys every library function returns, so one press refreshes the
    list, the table and the filter dropdown together."""
    return {"rows": lib["rows"], "table": lib["table"],
            "names": lib["names"], "filters": lib["filters"]}


def list_classifiers(show: Any = "") -> Dict[str, Any]:
    """The saved classifiers: name, current version, where each came from,
    its tags, its classes, how many frames are marked, when it last changed.
    ``show`` filters them (see FILTERING) — "This app", "App: Typhon",
    "Tag: lab" — so a store many apps share stays readable.

    "rows" is one line each, for a listbox (the name comes first, so a
    picked row names its classifier); "table" is (name, version, origin,
    tags, classes, frames, updated) for a table; "filters" is the filter
    choices that exist, for a dropdown; "details" is a dict per classifier,
    for code; "store" is the folder they are kept in.

    Keys: rows, table, names, details, filters, store, summary
    """
    lib = _library(show)
    k, total = len(lib["names"]), lib["total"]
    store, why = _store_choice()
    listed = ", ".join(lib["names"][:6]) + (" ..." if k > 6 else "")
    if not total:
        summary = ("No saved classifiers yet — type a name, add classes and "
                   "mark frames.")
    elif not k:
        summary = (f"None of the {total} saved classifiers match "
                   f"{lib['label']} — choose {FILTER_ALL} to see them all.")
    elif lib["filtered"]:
        summary = (f"{k} of {total} saved classifiers ({lib['label']}): "
                   f"{listed}")
    else:
        summary = (f"{total} saved classifier{'' if total == 1 else 's'}: "
                   f"{listed}")
    if not why.startswith("the vault"):
        summary += f" Kept in {store} ({why})."
    return dict(_listed(lib), details=lib["details"], store=str(store),
                summary=summary)


def store_info() -> Dict[str, Any]:
    """Where classifiers are kept and why there, and which app is running —
    what a classifier made now will say it came from.

    Keys: store, how, app, summary
    """
    store, why = _store_choice()
    label = _app_label(_this_app())
    host = _host()
    return {"store": str(store), "how": why, "app": label,
            "summary": f"Classifiers are kept in {store} ({why}). New ones "
                       f"made here are recorded as made by {label}"
                       + (f" on {host}" if host else "") + "."}


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
    clash = _taken(new)
    if clash or target.exists():
        raise RuntimeError(_clash_text(clash or new))
    return new, target


def _in_use(exc: OSError, n: str, d: Path) -> RuntimeError:
    if isinstance(exc, PermissionError):
        return RuntimeError(f"'{n}' is in use by another program — a file "
                            f"inside {d} is open. Close it and try again; "
                            f"nothing was changed.")
    return RuntimeError(f"cannot move {d}: {_os_error(exc)}. Nothing was "
                        f"changed.")


def _copy_ignore(_dir: str, names: List[str]) -> List[str]:
    """What a copy leaves behind: the run record (the original's history),
    half-written temp files and folders, and files set aside as unusable."""
    return [x for x in names
            if x == RUNS or x.startswith(".tmp-") or ".unusable-" in x
            or re.match(r"^(classes\.json|model\.npz|meta\.json|about\.json)"
                        r"\.\w+$", x)]


def _stage_into_place(build, target: Path) -> None:
    """Build a classifier folder in a hidden staging folder beside the
    others, then rename it to ``target`` in one step — a copy or an import
    is all there or not there, and an existing folder is never replaced
    (the rename refuses)."""
    root = classifier_store()
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=str(root)))
    try:
        staged = tmp / "c"
        build(staged)
        if target.exists() or _taken(target.name):
            raise FileExistsError(str(target))
        os.rename(staged, target)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def save_as(name: Any, new_name: Any, show: Any = "") -> Dict[str, Any]:
    """Copy a classifier under a new name — its classes, marks, every
    version, origin and tags — and switch to the copy. The copy keeps the
    ORIGINAL's origin (it was made there) and its lineage adds "copied from
    <name> <version>". Never replaces an existing classifier. The run record
    stays with the original: it is the original's history.

    Keys: name, classes, rows, table, names, filters, summary
    """
    n, d = _existing(name, "copy")
    new, target = _free_target(new_name, "press Save as")
    _load(n)                      # a damaged classes.json is reported, not copied
    about = _read_about(d)        # ... and so is a damaged about.json
    with _using(d):
        notes = _reconcile(d, n)            # a pre-versions model becomes v1 first
        newest = _newest(d)
        copied = _event("copied", **{
            "from": n, "from_version": _vid(n, newest[0]) if newest else "",
            "sha256": newest[0]["sha256"] if newest else ""})

        def build(staged: Path) -> None:
            shutil.copytree(d, staged, ignore=_copy_ignore)
            _write_about(staged, dict(about,
                                      lineage=about["lineage"] + [copied]))
        try:
            _stage_into_place(build, target)
        except FileExistsError:
            raise RuntimeError(_clash_text(_taken(new) or new))
        except OSError as exc:
            raise RuntimeError(f"cannot copy '{n}' to '{new}': "
                               f"{_os_error(exc)}")
    lib = _library(show)
    newest = _newest(target)
    what = (f"with its {len(_version_numbers(target))} version(s), current "
            f"{_vid(new, newest[0])}" if newest else "(not trained yet)")
    note = (" " + " ".join(notes)) if notes else ""
    return dict(_listed(lib), name=new, classes=list(_load(new)["classes"]),
                summary=f"Saved '{n}' as '{new}' {what}. It keeps its origin "
                        f"({_origin_text(about['origin'])}) and records "
                        f"that it was {_event_text(copied)}. Now using "
                        f"'{new}'.{note}")


def rename_classifier(name: Any, new_name: Any, current: Any = "",
                      show: Any = "") -> Dict[str, Any]:
    """Rename a classifier: its marks, versions, origin, tags and run record
    move with it, and its lineage adds "renamed from <old name>". Never
    replaces an existing classifier, and is refused while it is training or
    classifying. ``current`` is the name the window has open: "name" comes
    back as the new name when that was the one renamed, so the name box
    follows it — and unchanged otherwise.

    Keys: name, rows, table, names, filters, summary
    """
    n, d = _existing(name, "rename")
    new = _name_of(new_name)
    if not new:
        raise RuntimeError("type the new name first, then press Rename")
    target = store_dir(new)
    if new == n:
        raise RuntimeError(f"'{n}' already has that name")
    if new.lower() != n.lower():
        clash = _taken(new)
        if clash or target.exists():
            raise RuntimeError(_clash_text(clash or new))
    about = _read_about(d)        # damaged: refused before anything moves
    _refuse_if_busy(d, n, "rename it")
    try:
        os.rename(d, target)
    except OSError as exc:
        raise _in_use(exc, n, d)
    _forget(d)
    renamed = _event("renamed", **{"from": n})
    try:
        _write_about(target, dict(about, lineage=about["lineage"] + [renamed]))
        noted = ""
    except (RuntimeError, OSError) as exc:
        noted = f" (Its history does not say so: {exc}.)"
    cur = _name_of(current)
    lib = _library(show)
    return dict(_listed(lib),
                name=new if not cur or cur.lower() == n.lower() else cur,
                summary=f"Renamed '{n}' to '{new}'. Its versions, origin and "
                        f"run record went with it.{noted}")


def delete_classifier(name: Any, current: Any = "",
                      show: Any = "") -> Dict[str, Any]:
    """"Delete" a classifier by MOVING it to <store>/.deleted/<name>_<stamp>
    — nothing is erased, and the summary says where it went so moving it
    back restores it. Refused while it is training or classifying.
    ``current`` is the name the window has open: when that is the one
    deleted, "name" and "classes" come back empty so the window stops
    showing it; otherwise they are the open classifier's.

    Keys: name, classes, moved_to, rows, table, names, filters, summary
    """
    n, d = _existing(name, "delete")
    _refuse_if_busy(d, n, "delete it")
    bin_dir = classifier_store() / DELETED
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
    lib = _library(show)
    return dict(_listed(lib), name=keep, classes=classes, moved_to=str(dest),
                summary=f"Deleted '{n}': moved to {dest}. Nothing was erased "
                        f"— move that folder back into {dest.parent.parent} "
                        f"(named {n}) to restore it.")


def _tag_of(tag: Any) -> str:
    text = " ".join((_picked(tag) if isinstance(tag, (list, tuple))
                     else str(tag or "")).split())
    if len(text) > TAG_MAX_LEN:
        raise RuntimeError(f"a tag is at most {TAG_MAX_LEN} characters — "
                           f"'{text[:TAG_MAX_LEN]}...' is {len(text)}")
    if not text.isprintable():
        raise RuntimeError("a tag is one line of ordinary text")
    return text


def add_tag(name: Any, tag: Any, show: Any = "") -> Dict[str, Any]:
    """Tag a classifier with words of the user's own ("lab 2", "night
    shift") — what the list can then be filtered by. Separate from its
    origin, which no tag changes. A tag already there (in any case) is not
    added twice; nothing typed is a soft case, not an error.

    Keys: name, tags, cleared, rows, table, names, filters, summary
    """
    n, d = _existing(name, "tag")
    t = _tag_of(tag)
    about = _read_about(d)
    tags = list(about["tags"])
    if not t:
        summary = "Type a tag (e.g. lab-2), then Add tag."
    elif t.lower() in (x.lower() for x in tags):
        summary = f"'{n}' is already tagged '{t}'."
    else:
        if len(tags) >= TAGS_MAX:
            raise RuntimeError(f"'{n}' already has {TAGS_MAX} tags — take "
                               f"one off first")
        tags.append(t)
        _write_about(d, dict(about, tags=tags))
        summary = f"Tagged '{n}': {', '.join(tags)}."
    return dict(_listed(_library(show)), name=n, tags=tags, cleared="",
                summary=summary)


def remove_tag(name: Any, tag: Any, show: Any = "") -> Dict[str, Any]:
    """Take one of the user's tags off a classifier (any case). The tag may
    be typed, or picked in a list of its tags. Its origin is not a tag and
    cannot be taken off.

    Keys: name, tags, rows, table, names, filters, summary
    """
    n, d = _existing(name, "untag")
    t = _tag_of(tag)
    about = _read_about(d)
    tags = list(about["tags"])
    keep = [x for x in tags if x.lower() != t.lower()]
    if not t:
        summary = "Pick or type the tag to take off."
    elif len(keep) == len(tags):
        summary = (f"'{n}' is not tagged '{t}' (its tags: "
                   f"{', '.join(tags) or 'none'}).")
    else:
        _write_about(d, dict(about, tags=keep))
        tags = keep
        summary = (f"Took '{t}' off '{n}'. Its tags: "
                   f"{', '.join(tags) or 'none'}.")
    return dict(_listed(_library(show)), name=n, tags=tags, summary=summary)


def list_versions(name: Any) -> Dict[str, Any]:
    """Every version of a classifier, newest first: number, id, when, how
    many frames of each class, and the accuracy measured when it was made.
    "versions" is the meta.json of each, for code.

    Keys: rows, versions, current, summary
    """
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
# Export: one file per classifier, one bundle per app
# ============================================================

EXPORT_README = f"""\
A Typhon classifier export: format "{EXPORT_FORMAT}", format_version
{EXPORT_FORMAT_VERSION}. Written by frame_classes.py.

Everything needed to USE this classifier is in this file; the frames it was
trained on are not needed. A zip holding exactly these members:

  manifest.json  What this is. format and format_version; feature_layout,
                 feature_length and "features" (how a frame becomes
                 numbers); "model" (how the forest is rebuilt); name,
                 version, version_id ("frames v3 (1a2b3c4d)") and
                 model_sha256; origin (the app and project that made it,
                 when, on which PC), lineage (copies, renames and imports
                 since) and tags; "members": the sha256 of every member
                 below.
  model.npz      numpy arrays, no Python objects - read it with
                 np.load(f, allow_pickle=False):
                   X        float32 [frames, feature_length]  each marked frame
                   y        int32   [frames]   the class of each, an index into classes
                   classes  str     [k]        the class names
                   paths    str     [frames]   where each frame was, on the PC that trained it
  meta.json      This version: number, sha256, when, frames per class, and
                 the accuracy measured when it was made.
  classes.json   The marks: {{"classes": [...], "labels": {{frame path: class}}}}.
  runs.jsonl     (only when there is one) One JSON object per line: each
                 time this classifier classified a folder - when, which
                 folder on which PC, which version, how many of each class.
  README.txt     This text.

To classify a frame without the program that wrote this: compute its
features as manifest "features" says, fit the forest manifest "model"
describes on X and y, predict, and look the answer up in classes.

model_sha256 is the sha256 of the arrays' CONTENT (not of model.npz's bytes),
so the same model has the same id on every PC.

A reader must refuse a format_version above the one it knows, and a
feature_layout it does not compute; nothing here is ever unpickled.
"""

BUNDLE_README = f"""\
A bundle of Typhon classifiers: format "{BUNDLE_FORMAT}", format_version
{BUNDLE_FORMAT_VERSION}. Written by frame_classes.py, usually to move every
classifier one app made into a store of that app's own.

  bundle.json        What this is: format, format_version, when, on which
                     PC, by which app ("made_by"), which classifiers were
                     chosen ("selection"), and for each one: "file" (its
                     member below), name, version_id, model_sha256, "sha256"
                     (of that member), origin, tags. "skipped" lists any
                     that could not be exported, and why.
  classifiers/<name>{EXPORT_SUFFIX}
                     One COMPLETE single-classifier export each (format
                     "{EXPORT_FORMAT}") - read the README.txt inside any of
                     them. Import each exactly as that file on its own.
  README.txt         This text.
"""


def _peek_kind(p: Path) -> str:
    """"single" / "bundle" for a file this module wrote, else "" — so an
    existing file is only ever replaced by an export of the same kind."""
    try:
        with zipfile.ZipFile(p) as zf:
            names = set(zf.namelist())
            for member, fmt, kind in (("manifest.json", EXPORT_FORMAT, "single"),
                                      ("bundle.json", BUNDLE_FORMAT, "bundle")):
                if member in names:
                    info = zf.getinfo(member)
                    if info.file_size > (1 << 20):
                        return ""
                    if json.loads(zf.read(info)).get("format") == fmt:
                        return kind
    except Exception:                                    # noqa: BLE001
        return ""
    return ""


def _export_target(destination: Any, base: str, suffix: str = EXPORT_SUFFIX,
                   kind: str = "single") -> Path:
    text = str(destination or "").strip().strip('"')
    if not text:
        raise RuntimeError("choose where to save the export first — a folder, "
                           "or a file name")
    p = Path(text).expanduser()
    if p.is_dir():
        out = p / f"{base}{suffix}"
        k = 2
        while out.exists():                 # never over a file already there
            out = p / f"{base} ({k}){suffix}"
            k += 1
        return out
    if not p.name.lower().endswith(".zip"):
        p = p.with_name(p.name + suffix)
    if not p.parent.is_dir():
        raise RuntimeError(f"{p.parent} is not a folder — choose a folder "
                           f"that exists")
    if p.exists() and _peek_kind(p) != kind:
        # A save dialog may have asked "replace?", but this is not an export
        # of the same kind: refusing costs a second try, replacing could
        # cost the user a file.
        raise RuntimeError(f"{p} already exists and is not a classifier "
                           f"{'export' if kind == 'single' else 'bundle'} — "
                           f"choose another name (it was not touched)")
    return p


def _runs_for_export(d: Path) -> Tuple[bytes, int, int]:
    """(runs.jsonl bytes with every readable record, records, unreadable).
    Re-written from the parsed records, so a partial line left by a crash
    is not carried into a file that must check out end to end."""
    try:
        records, bad = _read_runs(d / RUNS)
    except RuntimeError:
        return b"", 0, 0
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    return text.encode("utf-8"), len(records), bad


def _export_bytes(n: str, d: Path) -> Tuple[bytes, Dict[str, Any]]:
    """One classifier's export file, in memory, and what it holds."""
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
        about = _read_about(d)
        runs_bytes, runs, bad_runs = _runs_for_export(d)
    vid = _vid(n, meta)
    members = {"classes.json": classes_bytes,
               "meta.json": json.dumps(meta, indent=2,
                                       ensure_ascii=False).encode("utf-8"),
               "model.npz": model_bytes,
               "README.txt": EXPORT_README.encode("utf-8")}
    if runs_bytes:
        members["runs.jsonl"] = runs_bytes
    manifest = {
        "format": EXPORT_FORMAT, "format_version": EXPORT_FORMAT_VERSION,
        "written_by": "frame_classes", "created": _now(), "host": _host(),
        "name": n, "version": meta["version"], "version_id": vid,
        "model_sha256": meta["sha256"], "classes": model["classes"],
        "feature_layout": FEATURE_LAYOUT,
        "feature_length": int(model["X"].shape[1]),
        "features": FEATURE_RECIPE, "model": MODEL_RECIPE,
        "origin": about["origin"], "lineage": about["lineage"],
        "tags": about["tags"],
        "members": {k: hashlib.sha256(v).hexdigest()
                    for k, v in members.items()}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2,
                                                ensure_ascii=False))
        zf.writestr("README.txt", members["README.txt"])
        zf.writestr("meta.json", members["meta.json"])
        zf.writestr("classes.json", classes_bytes)
        if runs_bytes:
            zf.writestr("runs.jsonl", runs_bytes)
        # Already compressed by numpy: deflating it again is CPU for nothing.
        zf.writestr(zipfile.ZipInfo("model.npz", (1980, 1, 1, 0, 0, 0)),
                    model_bytes, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue(), {"version_id": vid, "version": meta["version"],
                            "sha256": meta["sha256"], "about": about,
                            "runs": runs, "bad_runs": bad_runs,
                            "notes": notes}


def export_classifier(name: Any, destination: Any) -> Dict[str, Any]:
    """Write the current version as ONE file another PC can import — the
    model, its marks, its origin, lineage and tags, and its run record.

    ``destination`` is a folder (the file is named <name>-v<N>.typhon-
    classifier.zip, never over an existing file) or a file name (".typhon-
    classifier.zip" is added when it has no .zip; an existing file is
    replaced only when it is itself a classifier export).

    Keys: path, version, sha256, summary
    """
    n, d = _existing(name, "export")
    data, ex = _export_bytes(n, d)
    out = _export_target(destination, f"{n}-v{ex['version']}")
    try:
        _atomic_write(out, lambda tmp: Path(tmp).write_bytes(data))
    except OSError as exc:
        raise RuntimeError(f"cannot write {out}: "
                           f"{_os_error(exc, _SHORTER_EXPORT)}")
    notes = list(ex["notes"])
    if ex["bad_runs"]:
        notes.append(f"{ex['bad_runs']} unreadable line(s) of its run record "
                     f"were left out.")
    tags = ex["about"]["tags"]
    return {"path": str(out), "version": ex["version_id"],
            "sha256": ex["sha256"],
            "summary": f"Exported {ex['version_id']} to {out} "
                       f"({max(1, len(data) // 1024)} KB) — from "
                       f"{_origin_text(ex['about']['origin'])}"
                       + (f", tags {', '.join(tags)}" if tags else "")
                       + f", {ex['runs']} classified run(s) on record. On "
                       f"the other PC, press Import and pick this file — the "
                       f"frames are not needed there."
                       + ((" " + " ".join(notes)) if notes else "")}


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "-", text).strip("-")[:48] or "classifiers"


def export_classifiers(destination: Any, show: Any = "") -> Dict[str, Any]:
    """Every classifier the filter ``show`` lists, in ONE bundle file — what
    an app that is leaving the Council imports into a store of its own (see
    AN APP OF ITS OWN). Each classifier goes in as a complete single export,
    origin, tags and run record included; one that has not been trained has
    no model to carry and is listed as skipped. Never over a file that is
    not a bundle.

    Keys: path, names, skipped, summary
    """
    lib = _library(show)
    if not lib["details"]:
        raise RuntimeError(f"no classifier matches {lib['label']} — nothing "
                           f"was exported")
    me = _this_app()
    entries, blobs, skipped = [], {}, []
    for info in lib["details"]:
        n = info["name"]
        try:
            data, ex = _export_bytes(n, classifier_store() / n)
        except (RuntimeError, OSError) as exc:
            skipped.append({"name": n, "why": str(exc)})
            continue
        member = f"classifiers/{n}{EXPORT_SUFFIX}"
        blobs[member] = data
        entries.append({"file": member, "name": n,
                        "version_id": ex["version_id"],
                        "model_sha256": ex["sha256"],
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "origin": ex["about"]["origin"],
                        "tags": ex["about"]["tags"]})
    if not entries:
        raise RuntimeError(
            f"none of the {len(lib['details'])} classifier(s) in "
            f"{lib['label']} could be exported — "
            + "; ".join(f"{s['name']}: {s['why']}" for s in skipped[:3]))
    how, value = _parse_show(show)
    label = {"all": "all", "this": me["project"] or me["script"] or "this-app",
             "unknown": "origin-unknown"}.get(how, value)
    index = {"format": BUNDLE_FORMAT, "format_version": BUNDLE_FORMAT_VERSION,
             "written_by": "frame_classes", "created": _now(),
             "host": _host(), "made_by": me, "selection": lib["label"],
             "classifiers": entries, "skipped": skipped}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = _export_target(destination, f"{_slug(label)}-{stamp}",
                         BUNDLE_SUFFIX, kind="bundle")

    def _w(tmp):
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bundle.json", json.dumps(index, indent=2,
                                                  ensure_ascii=False))
            zf.writestr("README.txt", BUNDLE_README)
            for member, data in blobs.items():
                zf.writestr(zipfile.ZipInfo(member, (1980, 1, 1, 0, 0, 0)),
                            data, compress_type=zipfile.ZIP_STORED)
    try:
        _atomic_write(out, _w)
    except OSError as exc:
        raise RuntimeError(f"cannot write {out}: "
                           f"{_os_error(exc, _SHORTER_EXPORT)}")
    names = [e["name"] for e in entries]
    tail = ("" if not skipped else
            " Not in it: " + "; ".join(f"{s['name']} ({s['why']})"
                                       for s in skipped) + ".")
    return {"path": str(out), "names": names,
            "skipped": [s["name"] for s in skipped],
            "summary": f"Exported {len(names)} classifier(s) of "
                       f"{lib['label']} to {out} "
                       f"({max(1, out.stat().st_size // 1024)} KB): "
                       f"{', '.join(names)}. Import that one file to bring "
                       f"them all in.{tail}"}


def export_this_app(destination: Any) -> Dict[str, Any]:
    """Everything the RUNNING app made, in one bundle — export_classifiers
    with the "This app" filter, for a button that needs no filter box.

    Keys: path, names, skipped, summary
    """
    return export_classifiers(destination, FILTER_THIS_APP)


# ============================================================
# Import
# ============================================================

def _json_member(data: bytes, what: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"{what} is not readable ({exc}) — nothing was "
                           f"imported")


def _zip_members(zf: zipfile.ZipFile, label: str, limits: Dict[str, int],
                 max_members: int) -> Dict[str, bytes]:
    """Every member of ``zf``, each a name ``limits`` allows and read with
    that hard cap — any other name, a repeat, or too many members is
    refused before anything is read."""
    infos = zf.infolist()
    if len(infos) > max_members:
        raise RuntimeError(f"{label} holds {len(infos)} entries, more than "
                           f"it may — refused, nothing was imported")
    blobs: Dict[str, bytes] = {}
    for info in infos:
        if info.filename not in limits:
            raise RuntimeError(f"{label} holds {info.filename!r}, which is "
                               f"not part of it — refused, nothing was "
                               f"imported")
        if info.filename in blobs:
            raise RuntimeError(f"{label} holds {info.filename} twice — "
                               f"refused")
        cap = limits[info.filename]
        if info.file_size > cap:
            raise RuntimeError(f"{info.filename} in {label} unpacks to "
                               f"{info.file_size:,} bytes, over the {cap:,} "
                               f"limit — refused, nothing was imported")
        with zf.open(info) as fh:
            data = fh.read(cap + 1)               # bounded, whatever it says
        if len(data) > cap:
            raise RuntimeError(f"{info.filename} in {label} is over the "
                               f"{cap:,}-byte limit — refused")
        blobs[info.filename] = data
    return blobs


def _read_export_bytes(raw: bytes, label: str) -> Dict[str, Any]:
    """Every member of one classifier export, checked. Nothing is written
    before this returns, and nothing is ever extracted by a name taken from
    the zip."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise RuntimeError(f"{label} is not a classifier export (not a zip "
                           f"file: {exc})")
    try:
        with zf:
            blobs = _zip_members(zf, label, _EXPORT_LIMITS,
                                 len(_EXPORT_LIMITS))
    except RuntimeError:
        raise
    except Exception as exc:                  # CRC mismatch, bad deflate data
        raise RuntimeError(f"{label} is damaged ({type(exc).__name__}: "
                           f"{exc}) — nothing was imported")
    lacking = [m for m in _EXPORT_REQUIRED if m not in blobs]
    if lacking:
        raise RuntimeError(f"{label} is not a complete classifier export "
                           f"(no {', '.join(lacking)})")

    manifest = _json_member(blobs["manifest.json"], f"manifest.json in {label}")
    if not isinstance(manifest, dict) or manifest.get("format") != EXPORT_FORMAT:
        raise RuntimeError(f"{label} is not a Typhon classifier export")
    fv = manifest.get("format_version")
    if not isinstance(fv, int) or fv < 1:
        raise RuntimeError(f"{label} has no usable format version")
    if fv > EXPORT_FORMAT_VERSION:
        raise RuntimeError(f"{label} was written by a newer build (format "
                           f"{fv}; this one reads {EXPORT_FORMAT_VERSION}) — "
                           f"update this copy of the app to import it")
    if (manifest.get("feature_layout") != FEATURE_LAYOUT
            or manifest.get("feature_length") != _feature_length()):
        raise RuntimeError(
            f"{label} was made with features this build does not make "
            f"(layout {manifest.get('feature_layout')}, "
            f"{manifest.get('feature_length')} per frame; this build: layout "
            f"{FEATURE_LAYOUT}, {_feature_length()}) — retrain it on this PC "
            f"or bring both PCs to the same version of the app")
    sums = manifest.get("members")
    present = set(blobs) - {"manifest.json"}
    if not isinstance(sums, dict) or set(sums) != present:
        raise RuntimeError(f"{label}: the manifest does not list exactly its "
                           f"members — refused")
    for member in sorted(present):
        if hashlib.sha256(blobs[member]).hexdigest() != str(sums[member]):
            raise RuntimeError(f"{member} in {label} does not match its "
                               f"checksum — the file is damaged or was "
                               f"altered; nothing was imported")
    try:
        about = _checked_about(manifest, origin_required=False)
    except ValueError as exc:
        raise RuntimeError(f"{label}: where it came from is not readable "
                           f"({exc}) — refused")

    meta = _json_member(blobs["meta.json"], f"meta.json in {label}")
    if (not isinstance(meta, dict) or not isinstance(meta.get("version"), int)
            or meta["version"] < 1 or meta["version"] > 999999
            or not _SHA_RE.match(str(meta.get("sha256", "")))
            or meta["sha256"] != manifest.get("model_sha256")):
        raise RuntimeError(f"{label}: meta.json does not describe its model "
                           f"— refused")
    doc = _json_member(blobs["classes.json"], f"classes.json in {label}")
    if (not isinstance(doc, dict) or not isinstance(doc.get("classes"), list)
            or not isinstance(doc.get("labels", {}), dict)
            or not all(isinstance(c, str) for c in doc["classes"])
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in doc.get("labels", {}).items())):
        raise RuntimeError(f"{label}: classes.json is not a classifier's "
                           f"classes — refused")
    stray = {v for v in doc.get("labels", {}).values()} - set(doc["classes"])
    if stray:
        raise RuntimeError(f"{label}: a mark names a class that is not in "
                           f"the list ({sorted(stray)[0]!r}) — refused")
    runs: List[Dict[str, Any]] = []
    if "runs.jsonl" in blobs:
        try:
            for line in blobs["runs.jsonl"].decode("utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        raise ValueError("a line is not a record")
                    runs.append(rec)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"{label}: its run record is not readable "
                               f"({exc}) — refused")
    model = _read_model_bytes(blobs["model.npz"], f"model.npz in {label}")
    if _model_sha(model) != meta["sha256"]:
        raise RuntimeError(f"model.npz in {label} is not the model its "
                           f"meta.json describes — refused")
    if not set(model["classes"]) <= set(doc["classes"]):
        raise RuntimeError(f"{label}: the model has classes its classes.json "
                           f"does not — refused")
    return {"manifest": manifest, "meta": meta, "classes": doc,
            "model": model, "model_bytes": blobs["model.npz"],
            "about": about, "runs": runs,
            "runs_bytes": blobs.get("runs.jsonl", b""),
            "file_sha256": hashlib.sha256(raw).hexdigest()}


def _read_capped(p: Path, cap: int, what: str) -> bytes:
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"cannot read {p}: {exc}")
    if size > cap:
        raise RuntimeError(f"{p.name} is {size >> 20} MB — larger than any "
                           f"{what}; refused")
    try:
        return p.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"cannot read {p}: {exc}")


def _read_export(p: Path) -> Dict[str, Any]:
    return _read_export_bytes(_read_capped(p, MAX_IMPORT_BYTES,
                                           "classifier export"), p.name)


def _read_bundle(p: Path) -> Tuple[Dict[str, Any],
                                   List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
    """A bundle's index and every classifier in it, ALL checked before
    anything is imported — a bundle with one bad member imports nothing."""
    raw = _read_capped(p, MAX_BUNDLE_BYTES, "classifier bundle")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise RuntimeError(f"{p.name} is not a classifier bundle ({exc})")
    try:
        with zf:
            names = [i.filename for i in zf.infolist()]
            if "bundle.json" not in names:
                raise RuntimeError(f"{p.name} is not a classifier bundle (no "
                                   f"bundle.json)")
            limits = {"bundle.json": 1 << 20, "README.txt": 64 << 10}
            limits.update({n: MAX_IMPORT_BYTES for n in names
                           if _BUNDLE_MEMBER_RE.match(n)})
            if sum(i.file_size for i in zf.infolist()) > MAX_BUNDLE_BYTES:
                raise RuntimeError(f"{p.name} unpacks to more than "
                                   f"{MAX_BUNDLE_BYTES >> 20} MB — refused")
            blobs = _zip_members(zf, p.name, limits, MAX_BUNDLE_MEMBERS + 2)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"{p.name} is damaged ({type(exc).__name__}: "
                           f"{exc}) — nothing was imported")
    index = _json_member(blobs["bundle.json"], f"bundle.json in {p.name}")
    if not isinstance(index, dict) or index.get("format") != BUNDLE_FORMAT:
        raise RuntimeError(f"{p.name} is not a Typhon classifier bundle")
    fv = index.get("format_version")
    if not isinstance(fv, int) or fv < 1:
        raise RuntimeError(f"{p.name} has no usable format version")
    if fv > BUNDLE_FORMAT_VERSION:
        raise RuntimeError(f"{p.name} was written by a newer build (bundle "
                           f"format {fv}; this one reads "
                           f"{BUNDLE_FORMAT_VERSION}) — update this copy of "
                           f"the app to import it")
    entries = index.get("classifiers")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError(f"{p.name} lists no classifiers")
    listed = [e.get("file") if isinstance(e, dict) else None for e in entries]
    members = set(blobs) - {"bundle.json", "README.txt"}
    if (len(set(listed)) != len(listed) or set(listed) != members
            or not all(isinstance(f, str) and _BUNDLE_MEMBER_RE.match(f)
                       for f in listed)):
        raise RuntimeError(f"{p.name}: bundle.json does not list exactly the "
                           f"classifiers in it — refused, nothing was "
                           f"imported")
    out = []
    for e in entries:
        data = blobs[e["file"]]
        if hashlib.sha256(data).hexdigest() != str(e.get("sha256")):
            raise RuntimeError(f"{e['file']} in {p.name} does not match its "
                               f"checksum — the file is damaged or was "
                               f"altered; nothing was imported")
        got = _read_export_bytes(data, f"{e['file']} in {p.name}")
        if (got["manifest"].get("name") != e.get("name")
                or got["meta"]["sha256"] != e.get("model_sha256")):
            raise RuntimeError(f"{p.name}: {e['file']} is not the classifier "
                               f"bundle.json says it is — refused")
        out.append((e, got))
    return index, out


def _import_path(path: Any) -> Path:
    text = (_picked(path) if isinstance(path, (list, tuple))
            else str(path or "").strip().strip('"'))
    if not text:
        raise RuntimeError(f"choose the exported classifier file "
                           f"(*{EXPORT_SUFFIX} or *{BUNDLE_SUFFIX}) first")
    p = Path(text).expanduser()
    if not p.is_file():
        raise RuntimeError(f"{p} is not a file")
    return p


def _same_classifier(existing: str, got: Dict[str, Any]) -> bool:
    """Whether the classifier ``existing`` here already IS what ``got``
    holds: the same current model and the same origin."""
    d = classifier_store() / existing
    try:
        newest = _newest(d)
        about = _read_about(d)
    except RuntimeError:
        return False
    return (newest is not None and newest[0]["sha256"] == got["meta"]["sha256"]
            and about["origin"] == got["about"]["origin"])


def _import_one(got: Dict[str, Any], target: str, source: str) -> str:
    """Put one checked export into the store as ``target``; returns its
    version id. All there or not there (staged, then renamed); never over
    anything (FileExistsError when the name was taken meanwhile)."""
    manifest, meta, doc = got["manifest"], got["meta"], got["classes"]
    stamp = _now()
    imported_from = {"file": source, "file_sha256": got["file_sha256"],
                     "source_name": str(manifest.get("name") or ""),
                     "source_version_id": str(manifest.get("version_id") or ""),
                     "source_host": str(manifest.get("host") or ""),
                     "exported": str(manifest.get("created") or ""),
                     "imported": stamp}
    vmeta = dict(meta, how="imported", imported_from=imported_from)
    if meta.get("how") and meta.get("how") != "imported":
        vmeta["trained_how"] = meta["how"]
    marks = {"classes": list(doc["classes"]),
             "labels": dict(doc.get("labels") or {})}
    about = got["about"]
    event = _event("imported", file=source, file_sha256=got["file_sha256"],
                   from_name=imported_from["source_name"],
                   from_version=imported_from["source_version_id"],
                   from_host=imported_from["source_host"])

    def build(staged: Path) -> None:
        staged.mkdir(parents=True)
        _, vdir = _write_version(staged, got["model_bytes"], marks, vmeta,
                                 number=int(meta["version"]))
        _write_mirror(staged, vdir)
        _write_json(staged / "classes.json",
                    dict(marks, version=1, updated=stamp))
        _write_about(staged, {"origin": about["origin"],
                              "lineage": about["lineage"] + [event],
                              "tags": about["tags"]})
        if got["runs_bytes"]:
            (staged / RUNS).write_bytes(got["runs_bytes"])

    try:
        _stage_into_place(build, store_dir(target))
    except FileExistsError:
        raise
    except OSError as exc:
        raise RuntimeError(f"cannot import into {store_dir(target)}: "
                           f"{_os_error(exc)}")
    return _vid(target, meta)


def import_classifier(path: Any, new_name: Any = "",
                      show: Any = "") -> Dict[str, Any]:
    """A classifier from an export file — or every classifier in a bundle,
    with ``new_name`` as the prefix for names that are taken (see
    import_bundle) — checked from end to end first (see MOVING A CLASSIFIER
    TO ANOTHER PC).

    It keeps the file's ORIGIN (where it was made, not where it was
    imported) and adds "imported from <file> on <date>" to its lineage. The
    version keeps its number and sha, so "frames v3 (1a2b3c4d)" is the same
    model on both PCs. It is imported under ``new_name`` when one is typed,
    else under its own name. A name that is taken is NEVER overwritten: it
    is refused, asking for another name — unless that classifier already is
    this one (same model, same origin), which is said and changes nothing.

    Keys: name, classes, version, imported, skipped, rows, table, names, filters, summary
    """
    p = _import_path(path)
    if _peek_kind(p) == "bundle":
        return import_bundle(p, new_name, show)
    got = _read_export(p)
    manifest, meta, doc = got["manifest"], got["meta"], got["classes"]
    typed = _name_of(new_name)
    if typed:
        wanted = _check_name(typed)
    else:
        wanted = str(manifest.get("name") or "")
        if not _NAME_RE.match(wanted) or wanted.lower() in _DEVICE_NAMES:
            raise RuntimeError(f"the classifier in {p.name} is called "
                               f"{wanted!r}, which cannot be a name here — "
                               f"type a name in New name and press Import "
                               f"again. Nothing was imported.")
    clash = _taken(wanted)
    if clash and _same_classifier(clash, got):
        lib = _library(show)
        return dict(_listed(lib), name=clash,
                    classes=list(_load(clash)["classes"]),
                    version=_vid(clash, meta), imported=[], skipped=[],
                    summary=f"'{clash}' already is this classifier "
                            f"({_vid(clash, meta)}, from "
                            f"{_origin_text(got['about']['origin'])}) — "
                            f"nothing was imported or changed.")
    if clash:
        raise RuntimeError(
            _clash_text(clash)
            .replace("choose another name", "type another name in New name "
                     "and press Import again")
            .replace("nothing was overwritten", "nothing was imported"))
    try:
        vid = _import_one(got, wanted, p.name)
    except FileExistsError:
        raise RuntimeError(f"'{wanted}' was taken while importing — type "
                           f"another name in New name and press Import "
                           f"again. Nothing was imported.")
    lib = _library(show)
    same = [i["name"] for i in lib["details"]
            if i["sha256"] == meta["sha256"] and i["name"] != wanted]
    frames = len(got["model"]["y"])
    return dict(_listed(lib), name=wanted, classes=list(doc["classes"]),
                version=vid, imported=[wanted], skipped=[],
                summary=f"Imported {vid} from {p.name}: {frames} trained "
                        f"frames, classes {', '.join(doc['classes'])}; made "
                        f"by {_origin_text(got['about']['origin'])}"
                        + (f"; {len(got['runs'])} classified run(s) on "
                           f"record" if got["runs"] else "")
                        + ". It works without the frames."
                        + (f" It is the same model as '{same[0]}'."
                           if same else ""))


def import_bundle(path: Any, prefix: Any = "",
                  show: Any = "") -> Dict[str, Any]:
    """Every classifier in a bundle (export_classifiers), each kept with its
    origin and its import added to its lineage. The whole bundle is checked
    before anything is imported.

    Never overwrites. A classifier already here (same name, model and
    origin) is skipped as already here. One whose name is TAKEN by another
    is skipped and named — type a prefix such as "lab2-" as the new name and
    import again to bring those in as "lab2-<name>"; the prefix is used only
    for names that are taken, so importing again is always safe.

    Keys: name, classes, version, imported, skipped, rows, table, names, filters, summary
    """
    p = _import_path(path)
    pre = _name_of(prefix)
    if pre and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,31}$", pre):
        raise RuntimeError(f"'{pre}' cannot start a classifier name — use "
                           f"letters, digits, '-' or '_' (e.g. lab2-)")
    index, items = _read_bundle(p)
    imported, already, clashed = [], [], []
    for entry, got in items:
        name = entry["name"]
        done = False
        for cand in [name] + ([pre + name] if pre else []):
            if not _NAME_RE.match(cand) or cand.lower() in _DEVICE_NAMES:
                continue
            clash = _taken(cand)
            if clash and _same_classifier(clash, got):
                already.append(clash)
                done = True
                break
            if clash:
                continue
            try:
                _import_one(got, cand, f"{p.name} ({entry['file']})")
            except FileExistsError:
                continue
            imported.append(cand)
            done = True
            break
        if not done:
            clashed.append(name)
    lib = _library(show)
    made = index.get("made_by") if isinstance(index.get("made_by"), dict) else {}
    whose = _app_label(made) if made else "an unknown app"
    text = (f"From {p.name} ({index.get('selection') or 'classifiers'}, "
            f"exported by {whose}"
            + (f" on {index['host']}" if index.get("host") else "")
            + f"): imported {len(imported)}"
            + (f" ({', '.join(imported)})" if imported else ""))
    if already:
        text += f"; already here {len(already)} ({', '.join(already)})"
    if clashed:
        text += (f"; NOT imported {len(clashed)} ({', '.join(clashed)}) — "
                 f"the name is taken here by another classifier. Type a "
                 f"prefix such as lab2- in New name and press Import again "
                 f"to bring {'those' if len(clashed) > 1 else 'it'} in as "
                 f"lab2-{clashed[0]}" + (" and so on" if len(clashed) > 1
                                         else ""))
    first = imported[0] if imported else ""
    return dict(_listed(lib), name=first,
                classes=list(_load(first)["classes"]) if first else [],
                version="", imported=imported, skipped=clashed,
                summary=text + ". Nothing was overwritten.")


# ============================================================
# The run record
# ============================================================

def _folder_key(folder: Any) -> str:
    return os.path.normcase(str(Path(str(folder).strip().strip('"'))
                                .expanduser().resolve()))


def _runs_files(name: str) -> List[Tuple[Path, bool]]:
    """(runs.jsonl, deleted?) for one classifier, or for every one — those
    in <store>/.deleted too: what classified a run stays true after the
    classifier is deleted."""
    if name:
        return [(store_dir(name) / RUNS, False)]
    root = classifier_store()
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


def _here(rec: Dict[str, Any]) -> bool:
    """Whether a run was classified on THIS PC. A record from before PCs
    were recorded was made where it is kept: there was no import then."""
    host = str(rec.get("host") or "")
    return not host or host.lower() == _host().lower()


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
    if not _here(rec):
        text += f"  · on {rec.get('host')}"
    if with_folder:
        text += f"  · {rec.get('folder', '?')}"
    return text


def run_history(name: Any = "", folder: Any = "") -> Dict[str, Any]:
    """Which classifier version classified which capture runs — newest
    first. By classifier, by folder, or both; with no classifier named,
    every classifier's record is searched (deleted ones included). A folder
    is a path on one PC, so searching by folder finds only this PC's
    records; by classifier alone, records carried in from another PC are
    listed too, each saying which PC.

    Keys: rows, records, latest, summary
    """
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
            if want and (not _here(rec)
                         or _folder_key(rec.get("folder", "")) != want):
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
                  f"{str(r.get('when', '?'))[:16]}"
                  + ("" if _here(r) else f" on {r.get('host')}")
                  + f" — {counts}")
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
    and when — from every classifier's record on this PC. A blank folder is
    not an error (this may run as soon as a folder box changes): it just
    says so.

    Keys: classified_with, rows, records, summary
    """
    if not str(folder or "").strip():
        return {"classified_with": "", "rows": [], "records": [],
                "summary": "Choose a folder of frames to see what classified "
                           "it."}
    h = run_history("", folder)
    line = h["latest"] or "Not classified yet — press Classify all frames."
    return {"classified_with": line, "rows": h["rows"],
            "records": h["records"], "summary": h["summary"]}
