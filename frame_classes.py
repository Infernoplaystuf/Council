"""
frame_classes.py — make classes, mark frames, train a very simple classifier,
and KEEP the trained models: a library of them that every app using the same
store shares, every version of each, a record of which app made each one, one
file that carries a classifier to another PC (or a whole app's worth of them,
for an app that goes its own way), and a record of which version classified
which capture run.

The workflow a GUI binds to buttons:

    open_classifier(name, current)                -> the saved classes, and where it came from
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
    add_tag(name, tag, show, current)             -> one of the user's own tags added
    remove_tag(name, tag, show, current)          -> ... and taken off again
    import_classifier(path, new_name, show, current) -> a classifier, or a bundle, from a file

and, around it:

    list_versions(name, current)                  -> every version, newest first
    export_classifier(name, destination, current) -> one <name>-v<N>.typhon-classifier.zip
    export_classifiers(destination, show)         -> ONE bundle of every classifier shown
    export_this_app(destination)                  -> one bundle of what THIS app made
    run_history(name, folder)                     -> which version classified what
    classified_with(folder)                       -> "Classified with frames v3 on ..."
    store_info()                                  -> where classifiers are kept; which app this is

A `name` may be what an entry holds ("frames") or a list's SELECTION — a
listbox row from list_classifiers ("frames   v3 (1a2b3c4d) · ..."), a table
row, or the same row as a dropdown's text — so the same functions serve a
name box, a model list and a model dropdown. A name typed in another case
("FRAMES") IS the classifier that exists ("frames"): its own spelling goes
into version ids, run records and exports.

``current`` is the classifier the window has OPEN (its name box). Refilling
a list drops its selection, so a press with nothing picked acts on the open
classifier — and a press that has to ask for something (a name, a pick, a
file) answers SOFTLY with the open classifier echoed back, so a slip never
blanks the window (see the end of this docstring). Delete alone always
needs a row picked.

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
    <store>/<name>/mirror.json             when and how big that copy was written
    <store>/<name>/runs.jsonl              one line per classified run
    <store>/.deleted/<name>_<stamp>/       what Delete moved aside
    <store>/.locks/<name>.lock             held while one app changes <name>

The capture folder is the raw data and is only ever READ — nothing here
writes into it, the run record included. A classifier is kept apart from any
one folder because the point of training one is to apply it to the NEXT
capture. A store pointed INTO a capture folder by mistake is caught where it
would matter: Mark and Train refuse, classify_folder classifies but records
nothing, and store_info warns. Files are written atomically (temp file +
replace; a version is a temp FOLDER renamed into place), so a crash mid-save
never leaves a half-written label file or a half-written version.

TWO APPS AT ONCE
----------------
The store is shared, so two apps (or two windows) can change one classifier
at the same moment. Every read-change-write — a mark, a class, a tag, a new
version, a run record — holds that classifier's lock file in <store>/.locks
(msvcrt / fcntl byte locks, which the OS drops if the app dies, so no lock
is ever left stuck). MEASURED without it: two processes marking 40 frames
each kept 39 of 80 marks; three opening a pre-versions classifier made v1,
v2 and v3 of one model. With it, a save Windows refused for a moment ("Access
is denied": another program had the file open — 1 save in 80, MEASURED) is
tried again for up to a second. Train computes outside the lock and, inside it,
looks again: a model another app saved meanwhile is not saved twice. A
classifier moved away (Delete or Rename in another app) while one is
classifying or training is never made again by the late write — the run is
"NOT recorded", the Train "nothing was saved".

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
build that knows nothing of versions still finds the current model. Each
time this module writes that copy it notes the file's size and modification
time in mirror.json — not to compare times, but to know whether the file is
still the one it wrote:

  * it is the newest version              -> nothing to do;
  * it is the copy this module wrote,     -> a crash came between writing a
    of an older version                      version and refreshing the copy;
                                             the copy is rewritten;
  * anything else that is not the newest  -> an older build trained since:
                                             it is ADOPTED as the next version
                                             — never dropped, even when its
                                             content equals an older version
                                             (that build's user put a mark
                                             back; MEASURED before mirror.json:
                                             such a Train was silently undone);
  * no mirror.json (written before it     -> decided by content: an older
    existed)                                 version's content is a stale
                                             copy, anything else is adopted;
                                             an older version that cannot be
                                             read is hashed from its model,
                                             and if even that fails nothing is
                                             adopted on a read — only Train
                                             may, because it makes a new
                                             current version anyway;
  * it cannot be read                     -> reported and left exactly as it
                                             is; the next Train sets it aside
                                             as model.unusable-<stamp>.npz and
                                             says so. Never silently replaced.

A DAMAGED newest version (its meta.json cut short by a power cut, say) is
left exactly as it is and reported; it is never quietly replaced by the one
before it as "current". Train is the way out every message names, and it
works: it numbers the new version past the damaged one, which then is
current again. Version numbers stop at v999999 (what the reader matches),
and an import may bring at most v900000, so there is always room to train.

A classifier from before versions (classes.json + model.npz only) therefore
keeps working unchanged: the first time anything opens it, its model becomes
v1 ("migrated") and its marks are untouched.

A marked frame whose FILE is gone (moved, or on another PC) is not lost to
the next Train: its features are taken from the current version, where they
were stored when it was trained. That is what lets an imported classifier be
trained further on the PC it was carried to. The marks an IMPORT brought are
never opened at all — classes.json lists them as "imported_labels": they
name paths on the PC that made them, and opening one is at best another
picture that happens to have that name here, at worst (a UNC path) a network
connection to whatever host the file names. They train from their stored
features, or are left out and counted; marking that frame here makes it this
PC's mark again.

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
"script" names what ran, with "script_id" (a hash of the script's full
path) and "script_folder" (its folder's name): two apps of their own that
are both main.py are two apps, and one copied elsewhere under its own
folder's name is still itself. A classifier made before origins were
recorded has NO about.json, and its origin reads "unknown (made before
origin tags)" — never filled in from whichever app opens it next, which
would be a guess written down as a record. An origin that names nobody ({})
reads the same.

WHOSE IT IS
-----------
Every shipped capture app calls its classifier "frames" until the user types
another name, so in a shared store one app's classifier is another's by
accident. MEASURED (Barbie v5 and Typhon built into one vault): Typhon, its
name box left alone, added its classes and marks to Barbie's classifier and
its Train became Barbie's current model — nothing asked. So only the app a
classifier BELONGS to changes it (Add class, Remove class, Mark, Train) or
renames or deletes it (the app that owns it would find it gone — MEASURED,
2026-10-06: Typhon's dropdown put Rename and Delete one click away for
every model in the store): the app that made it, or — for a copy — the app
that made the copy (its latest Save as or Import, recorded in the lineage
with the app's identity). Any app may open it, predict and classify with
it, tag it, copy it, export it. Another app is told whose it is and offered
the two ways on: Save as (its own copy, keeping the origin) or the tag
"shared", which lets every app change it. A classifier whose origin is
unknown is anyone's.

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
    "This app"                 what BELONGS to the running app (WHOSE IT IS):
                               what it made, and its copies and imports of
                               others' — same project id, or same project
                               and app name (a rebuilt project keeps its
                               classifiers)
    "App: Typhon"              made by any app called Typhon
    "Project: example_typhon"  made by that project
    "Tag: night shift"         tagged so by the user ("#night shift" too)
    "Origin unknown"           made before origins were recorded
    anything else              part of an app, project or example name, a
                               tag, or a classifier's name

App, project and free-word filters match PART of the name, ignoring case,
spacing and the kind of dash: "Barbie" and "App: Barbie Capture v5 - live"
both find "Barbie Capture v5 — live" (MEASURED before: only the exact
title, em dash included, found it).

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
shapes and dtypes consistent, the feature length equal to what THIS build's
features() produces, every field of meta.json and of each run record of the
kind it must be, and a version number that leaves room to train. It never
overwrites (see ORIGIN AND TAGS). The paths inside are never opened here
(see VERSIONS); a run record that does not say which PC made it is stamped
with the exporting PC's name, so it never answers for a folder here.

Export checks its own file with import's checks before writing it, so a
file that would be refused on the other PC is refused HERE, where it can
still be fixed. MEASURED before: Mark a class, Train, re-mark that frame,
Remove class — and the export, reported as written, was refused on the other
PC, taking every other classifier of its bundle with it. The exported class
list now includes every class the model can answer.

about.json carries its own format_version. One written by a NEWER build is
read but never rewritten here ("update this copy of the app"), and keys this
build does not know are kept through every write — MEASURED before: one Add
tag downgraded a newer about.json to format 1 and dropped its other keys.

AN APP OF ITS OWN
-----------------
An app may later run without the Council and its vault. Nothing here has to
change for that:

  * point its store at a folder of its own — set FRAME_CLASSES_STORE, or put
    classifier_store.json ({"store": "classifiers"}, relative to that file)
    beside the app;
  * export_this_app (or export_classifiers with a filter) writes everything
    that belongs to one app (see WHOSE IT IS) into ONE bundle,
    <label>-<stamp>.typhon-classifiers.zip: bundle.json (format
    "typhon-classifier-bundle", version 1, listing each classifier, its
    origin and checksums), a
    README.txt, and one complete single-classifier export per classifier;
  * the independent app imports that bundle into its own store with the
    same import_classifier, keeping every origin and adding the import to
    each lineage — which makes the importing app their owner (see WHOSE IT
    IS). A name already taken there is skipped and named; a prefix typed as
    the new name brings those in as <prefix><name>. A bundle is checked
    whole, one model unpacked at a time, and the models of all its
    classifiers together may unpack to MAX_BUNDLE_BYTES at most (MEASURED
    before: a 0.02 MB bundle of five held 526 MB once checked).

THE RUN RECORD
--------------
classify_folder appends one line to the classifier's runs.jsonl: when (and
"at", seconds since 1970 to the microsecond — "when" alone tied within a
second, and MEASURED the newest of two same-second runs was then reported
as the older), the folder, each capture run in it (the "<stamp>" of
"<stamp>_frame_000001.png") with its frame count, the classifier, the
version id and sha, the count per class, this PC's name and the app that
ran it. run_history and classified_with read it back, so the app can say
"Classified with frames v3 (1a2b3c4d) on 2026-10-02 14:03" — and, a capture
run at a time, which runs in the folder now that does not cover ("· not
classified yet: run 20261005_130000 (8 frames)"). A record keeps the name its
classifier had; it is shown as the classifier is called NOW ("hawks v3
(1a2b3c4d; then called frames)" after a Rename). The record lives
in the store: the capture folder is never written to. A folder is a path on
ONE PC, so the record of another PC (carried in by an import) never answers
for a folder here. Only the folder asked about is resolved; each record's
folder is compared as the text it was written as (MEASURED before:
resolving every record took 4 s at 5,000 records). A line that is not a
run record — cut short by a crash, or with a field of the wrong kind — is
skipped and counted, never an error that hides all the others.

NOTHING A USER MARKED IS DESTROYED BY THE CLASSIFIER: a class that still labels
frames cannot be removed (re-mark those frames first), and re-marking a frame
is the user deliberately changing that one label.

Failures RAISE RuntimeError with a sentence a user can act on; a generated
handler shows it in a dialog and clears what the button fills. Soft cases that
are not failures — "Add class" with nothing typed, or no model; Save as,
Rename or Import with no name typed, a name that is taken or one that cannot
be a name; Use selected or Delete with nothing picked; Import with no file
chosen, a file that is not there or one its checks refuse; Add class, Remove
class, Rename or Delete of another app's classifier (WHOSE IT IS) — return
the open classifier and its classes unchanged and say what to do, so a
stray click or a slip of the keyboard empties nothing. MEASURED before: each
of those raised, the handler blanked the name box and the class list, and
the next Train failed on ''. A press that takes a typed name answers
"cleared": "" once it used the name, the name as typed when it asks for
another — so New name never carries one press's name into the next (an
Import named a restored model after the last Save as). Summaries lead with one
short sentence and name files, not folders: the paths are in their own keys
("path", "moved_to", "store").

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
MIRROR = "mirror.json"
DELETED = ".deleted"
LOCKS = ".locks"
_MODEL_ARRAYS = ("X", "y", "classes", "paths")
_VERSION_RE = re.compile(r"^v([1-9][0-9]{0,5})$")
#: The last version number _VERSION_RE matches — a folder past it would be
#: a version nothing can see (MEASURED: v1000000 was written, never current).
_VERSION_MAX = 999999
#: The highest version an IMPORT may bring: anyone can write a file whose
#: own checksums agree, and one claiming v999999 left no room to train.
_IMPORT_VERSION_MAX = 900000
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

UNKNOWN_ORIGIN = "unknown (made before origin tags)"
ABOUT_FORMAT = "typhon-classifier-about"
ABOUT_FORMAT_VERSION = 1
TAG_MAX_LEN = 40
TAGS_MAX = 20
_LINEAGE_MAX = 1000
#: The tag that lets every app change a classifier, not only its owner
#: (see WHOSE IT IS).
SHARED_TAG = "shared"
#: Who an app is, as an origin and a lineage entry record it.
_IDENTITY = ("app", "project", "project_id", "script", "script_id",
             "script_folder")
#: How long a change waits for another app's change to the same classifier.
_LOCK_WAIT = 30.0

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
            folder = _resolved(path).parent
        except (OSError, ValueError, RuntimeError):
            continue
        if _looks_like_project(folder):
            return folder
    return None


# raw path (and, when relative, the folder it was relative to) -> resolved.
# The running app's files do not move while it runs, and resolving them was
# most of what a store lookup cost: MEASURED, "Which model?" resolved
# __main__ and argv[0] three times on every call before this.
_RESOLVED: Dict[Tuple[str, str], Path] = {}


def _resolved(p: Path) -> Path:
    key = (str(p), "" if p.is_absolute() else os.getcwd())
    hit = _RESOLVED.get(key)
    if hit is None:
        hit = p.resolve()
        if len(_RESOLVED) >= 64:
            _RESOLVED.clear()
        _RESOLVED[key] = hit
    return hit


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


def _script_ident() -> Tuple[str, str]:
    """(script_id, script_folder) of what ran, when it is not a Designer app.

    The name alone did not tell apps apart: MEASURED, two unrelated apps of
    their own, both main.py, each listed the other's classifiers as "This
    app". script_id is a hash of the script's full path — the same app on
    this PC; script_folder is the name of the folder holding it — the same
    app copied elsewhere under its own folder's name."""
    arg = sys.argv[0] if sys.argv else ""
    if not arg or arg in ("-c", "-m"):
        return "", ""
    try:
        p = _resolved(Path(arg))
    except (OSError, ValueError, RuntimeError):
        p = Path(arg)
    folder = p.parent.parent if p.name == "__main__.py" else p.parent
    text = "script\n" + os.path.normcase(str(p))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], folder.name


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
        sid, sfolder = _script_ident()
        return {"app": "", "project": "", "project_id": "", "example": "",
                "script": _script_name(), "script_id": sid,
                "script_folder": sfolder}
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
            "example": str(man.get("example") or ""), "script": "",
            "script_id": "", "script_folder": ""}
    return dict(_APP_CACHE[key])


def _app_label(me: Dict[str, Any]) -> str:
    """"Typhon (project example_typhon)", "the script x.py", ..."""
    app, project = str(me.get("app") or ""), str(me.get("project") or "")
    if project:
        return app if app in ("", project) else f"{app} (project {project})"
    if me.get("script"):
        return f"the script {me['script']}"
    return "Python, outside any app"


def _app_flat(me: Dict[str, Any]) -> str:
    """The same without brackets, for inside a bracket of its own: "Typhon,
    project example_typhon" (MEASURED before: "(This app (Typhon (project
    example_typhon)))")."""
    app, project = str(me.get("app") or ""), str(me.get("project") or "")
    if project:
        return app if app in ("", project) else f"{app}, project {project}"
    return _app_label(me)


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
            p = _resolved(Path(arg))
            if p.is_file() and p.parent not in out:
                out.append(p.parent)
        except (OSError, ValueError, RuntimeError):
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


def _holds_frames(folder: Path) -> bool:
    """Whether ``folder`` itself holds captured frames (it is looked at, not
    searched: the first few hundred entries are enough to tell)."""
    try:
        with os.scandir(folder) as it:
            for k, e in enumerate(it):
                if k > 500:
                    break
                if (e.name.lower().endswith(IMAGE_SUFFIXES)
                        and e.is_file(follow_symlinks=False)):
                    return True
    except OSError:
        return False
    return False


def _inside(child: Path, parent: Path) -> bool:
    """Whether ``child`` is ``parent`` or somewhere inside it."""
    try:
        c = os.path.normcase(str(child.resolve()))
        p = os.path.normcase(str(parent.resolve()))
    except (OSError, ValueError, RuntimeError):
        return False
    return _text_inside(c, p)


def _text_inside(c: str, p: str) -> bool:
    """The same for two normalised path TEXTS — nothing is opened, so a path
    taken from another PC's file costs no lookup (see VERSIONS)."""
    p = p.rstrip("\\/")
    return bool(p) and (c == p or c.startswith(p + os.sep)
                        or c.startswith(p + "/"))


def _capture_refusal(d: Path, folder: Any, done: str) -> str:
    return (f"the classifiers are kept inside the capture folder "
            f"{Path(str(folder)).name or folder} ({d.parent}), and a capture "
            f"folder is only ever read — point {STORE_ENV} or "
            f"{STORE_CONFIG} at another folder. Nothing was {done}.")


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


_PICK_OR_TYPE = "pick a classifier in the list, or type its name, first"


def _check_name(n: str) -> str:
    if not n:
        # What Use selected sends with nothing picked, and what a blanked
        # name box holds — "'' is not a usable name" told nobody what to do.
        raise RuntimeError(_PICK_OR_TYPE)
    if not _NAME_RE.match(n) or n.lower() in _DEVICE_NAMES:
        raise RuntimeError(
            f"'{n}' is not a usable classifier name — use letters, digits, "
            f"'-' or '_' (e.g. frames)")
    return n


def _canon(name: Any) -> str:
    """The classifier's own name for what was typed or picked: "FRAMES" is
    the existing "frames" — on every PC, not only where the file system
    ignores case — so version ids, run records, lineage and exports carry
    the one spelling the list shows (MEASURED before: a record and an
    export said "FRAMES" while the list said "frames")."""
    n = _check_name(_name_of(name))
    return _taken(n) or n


def store_dir(name: Any) -> Path:
    """<store>/<name>, under the existing classifier's own spelling. The
    name is checked, so it can never reach outside that folder ("..\\..\\x"
    is not a name)."""
    return classifier_store() / _canon(name)


def _load_dir(d: Path) -> Dict[str, Any]:
    p = d / "classes.json"
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
    if not isinstance(data.get("imported_labels", []), list):
        data["imported_labels"] = []
    return data


def _load(name: Any) -> Dict[str, Any]:
    return _load_dir(store_dir(name))


def _atomic_write(path: Path, write) -> None:
    """Write via a temp file in the same folder, then replace — a crash
    mid-save leaves the old file, never a truncated one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    os.close(fd)
    try:
        write(tmp)
        _replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _replace(src: str, dst: Path) -> None:
    """os.replace, waiting out a moment when another program holds ``dst``
    open. Windows refuses to replace a file that is open without delete
    sharing — another app READING it (a list, a Train), or a virus scanner
    looking at what was just written. MEASURED: two apps marking one
    classifier, under its lock, still had 1 save in 80 refused "Access is
    denied" this way; a short wait lets it through."""
    for attempt in range(40):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 39:
                raise
            time.sleep(0.025)


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


_CLASSIFIER_FILES = ("classes.json", ABOUT, "model.npz", VERSIONS)


def _is_classifier(d: Path) -> bool:
    """``d`` holds a classifier: its marks, its origin, or a model. A folder
    holding only a run record is not one — that is what a late write used
    to leave behind after another app moved the classifier away."""
    try:
        return any((d / f).exists() for f in _CLASSIFIER_FILES)
    except OSError:
        return False


def _is_new(d: Path) -> bool:
    """Nothing of this classifier exists yet — so saving now CREATES it.
    MEASURED before: a stray runs.jsonl left in the folder made the next
    classifier of that name "not new", and its origin was never recorded."""
    return not _is_classifier(d)


def _gone(d: Path, doing: str) -> str:
    return (f"'{d.name}' was moved or deleted (by another app?) while it was "
            f"{doing} — nothing was saved, and it was not made again")


def _save_dir(d: Path, data: Dict[str, Any]) -> None:
    """classes.json, recording the origin first when this CREATES the
    classifier. The caller holds the classifier's lock (_locked)."""
    try:
        if _is_new(d):
            # The moment a classifier comes to exist: its origin is recorded
            # now, by the app doing it, and only now (see ORIGIN AND TAGS).
            _write_about(d, {"origin": _new_origin(), "lineage": [],
                             "tags": []})
        data["updated"] = _now()
        _write_json(d / "classes.json", data)
    except OSError as exc:
        raise RuntimeError(f"cannot save '{d.name}': {_os_error(exc)}")


# ============================================================
# One app at a time per classifier
# ============================================================

# lock file -> [a lock for this process's threads, how deeply held, fd]
_LOCKS: Dict[str, list] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_path(d: Path) -> Path:
    """<store>/.locks/<name>.lock — beside the classifier, not in it, so a
    held lock never keeps Windows from moving the folder; and by the name
    in lower case, because names are unique whatever their case."""
    return d.parent / LOCKS / f"{d.name.lower()}.lock"


def _os_lock(p: Path, name: str) -> int:
    """An OS byte lock on ``p``, waiting up to _LOCK_WAIT for another app.
    The OS drops it when the process ends, however it ends — a lock file
    that merely EXISTS would be left stuck by a crash."""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o666)
    except OSError as exc:
        raise RuntimeError(f"cannot change '{name}': its lock file cannot be "
                           f"made ({_os_error(exc)})")
    deadline = time.monotonic() + _LOCK_WAIT
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() > deadline:
                os.close(fd)
                raise RuntimeError(
                    f"'{name}' is being changed by another app and has been "
                    f"for {_LOCK_WAIT:.0f} s — try again in a moment. "
                    f"Nothing was changed.")
            time.sleep(0.01)


def _os_unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextlib.contextmanager
def _locked(d: Path) -> Iterator[None]:
    """Hold classifier ``d``'s lock: one read-change-write at a time, across
    apps and threads (see TWO APPS AT ONCE). Re-entrant within a thread, so
    Train may hold it around a reconcile that takes it too."""
    p = _lock_path(d)
    key = os.path.normcase(str(p))
    with _LOCKS_GUARD:
        entry = _LOCKS.setdefault(key, [threading.RLock(), 0, None])
    entry[0].acquire()
    try:
        if not entry[1]:
            entry[2] = _os_lock(p, d.name)
        entry[1] += 1
        try:
            yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                fd, entry[2] = entry[2], None
                _os_unlock(fd)
    finally:
        entry[0].release()


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


def _names_nobody(o: Dict[str, Any]) -> bool:
    """An origin with no word in it ({}) says no more than "unknown" does —
    MEASURED before: an import whose origin was {} was neither a known
    origin nor listed under "Origin unknown"."""
    return not o.get("unknown") and not any(
        isinstance(v, str) and v.strip() for v in o.values())


def _origin_text(o: Dict[str, Any], dated: bool = True) -> str:
    """"Typhon (project example_typhon) on LAB-PC, 2026-10-05"."""
    if not o or o.get("unknown"):
        return UNKNOWN_ORIGIN
    where = f" on {o['host']}" if o.get("host") else ""
    when = (f", {str(o['created'])[:10]}" if dated and o.get("created")
            else "")
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
    """One lineage entry: what happened, when, on which PC, by which app —
    "by" in words, and by_app, by_project ... the app's identity, because a
    copy or an import BELONGS to the app that made it (see WHOSE IT IS)."""
    me = _this_app()
    return dict(fields, event=kind, when=_now(), host=_host(),
                by=_app_label(me),
                **{f"by_{k}": str(me.get(k) or "") for k in _IDENTITY})


def _flat(obj: Any, what: str) -> Dict[str, Any]:
    """A record of plain values — what an origin or a lineage entry is.
    Anything else in one (a nested object, a 100 KB string, NaN) is refused:
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
        elif isinstance(v, float) and not math.isfinite(v):
            raise ValueError(f"{what}: '{k}' is not a number")
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
    if _names_nobody(origin):
        origin = _unknown_origin()
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
                "recorded": False, "newer": False}
    doc = _about_doc(p)
    try:
        about = _checked_about(doc)
    except ValueError as exc:
        raise RuntimeError(f"{p} is damaged ({exc}) — it was left as it is. "
                           f"Restore it from a backup or an export.")
    fv = doc.get("format_version")
    about["recorded"] = True
    # Read all the same — origin, lineage and tags are what any format has —
    # but never written back by this build (_write_about refuses).
    about["newer"] = (isinstance(fv, int) and not isinstance(fv, bool)
                      and fv > ABOUT_FORMAT_VERSION)
    return about


def _about_doc(p: Path) -> Dict[str, Any]:
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{p} cannot be read ({exc}) — it records where "
                           f"this classifier came from, so it was left as "
                           f"it is. Restore it from a backup or an export.")
    if not isinstance(doc, dict):
        raise RuntimeError(f"{p} is damaged (it is not a record) — it was "
                           f"left as it is. Restore it from a backup or an "
                           f"export.")
    return doc


def _newer_text(name: str) -> str:
    return (f"'{name}' was last saved by a newer build of the app, whose "
            f"record of where it came from this build does not fully know "
            f"— update this copy of the app to change it. Nothing was "
            f"changed.")


def _write_about(d: Path, about: Dict[str, Any]) -> None:
    """Save origin, lineage and tags. The ORIGIN may never change: a write
    that would change it is refused here, whoever asks — the one place it
    is stored is the one place it is guarded.

    Everything else the file holds is KEPT: it is read again and only those
    three are replaced, so keys a later build added survive. A file written
    by a NEWER format is never rewritten. MEASURED before: one Add tag
    turned a newer about.json into format 1 and dropped two of its keys."""
    p = d / ABOUT
    doc: Dict[str, Any] = {}
    if p.is_file():
        old = _read_about(d)                 # damaged: raises, not replaced
        if old["newer"]:
            raise RuntimeError(_newer_text(d.name))
        if old["origin"] != about["origin"]:
            raise RuntimeError(f"the origin of '{d.name}' is recorded once "
                               f"and never rewritten — nothing was saved")
        doc = _about_doc(p)
    # A reader refuses more than _LINEAGE_MAX entries (they come from other
    # PCs' files too), so the oldest go before that — a thousand copies,
    # renames and imports in, not a case a person reaches.
    doc.update(format=ABOUT_FORMAT, format_version=ABOUT_FORMAT_VERSION,
               origin=about["origin"],
               lineage=list(about.get("lineage") or [])[-_LINEAGE_MAX:],
               tags=list(about.get("tags") or []))
    try:
        _write_json(p, doc)
    except OSError as exc:
        raise RuntimeError(f"cannot save '{d.name}': {_os_error(exc)} — "
                           f"nothing was changed")


def _owner(about: Dict[str, Any],
           honour_shared: bool = True) -> Optional[Dict[str, Any]]:
    """The app a classifier BELONGS to (see WHOSE IT IS), or None when it
    is anyone's: origin unknown, tagged "shared", or a copy whose maker was
    not recorded. A Save as or an Import makes the app that did it the
    owner of that copy; otherwise it is the app that made it."""
    origin = about.get("origin") or {}
    if not about.get("recorded") or not origin or origin.get("unknown"):
        return None
    if honour_shared and any(t.lower() == SHARED_TAG
                             for t in about.get("tags") or []):
        return None
    for e in reversed(about.get("lineage") or []):
        if e.get("event") in ("copied", "imported"):
            who = {k: str(e.get(f"by_{k}") or "") for k in _IDENTITY}
            return who if any(who.values()) else None
    return origin


def _owner_refusal(d: Path, doing: str) -> str:
    """Why this app may not change the classifier in ``d`` (see WHOSE IT
    IS) — "" when it may. The words a refusal says, whether it raises
    (_check_owner) or answers softly (Add class, Remove class, Rename,
    Delete: a press the window must survive)."""
    if _is_new(d):
        return ""
    try:
        about = _read_about(d)
    except RuntimeError:
        return ""   # a damaged record is reported where it is read; work goes on
    owner = _owner(about)
    if owner is None or _same_app(owner, _this_app()):
        return ""
    return (f"'{d.name}' belongs to {_app_label(owner)}, not to this app — "
            f"Save as to make a copy of your own (it keeps where it came "
            f"from), or tag it '{SHARED_TAG}' to let every app change it. "
            f"Nothing was {doing}.")


def _check_owner(d: Path, doing: str) -> None:
    """Refuse a change to another app's classifier (see WHOSE IT IS).
    MEASURED before: Typhon, its name box left at the shipped default,
    added its classes and marks to Barbie's "frames" and its Train became
    Barbie's current model — nothing asked."""
    why = _owner_refusal(d, doing)
    if why:
        raise RuntimeError(why)


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


def _clash_head(existing: str) -> str:
    """"'frames' already exists (from Typhon (project ...) on PC, date)"."""
    try:
        whose = _origin_text(_read_about(classifier_store() / existing)["origin"])
    except RuntimeError:
        whose = "its origin cannot be read"
    return f"'{existing}' already exists (from {whose})"


def _clash_text(existing: str) -> str:
    return (f"{_clash_head(existing)} — choose another name (nothing was "
            f"overwritten)")


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


def _read_model_bytes(data: bytes, where: str, cap: int = 0,
                      cap_what: str = "") -> Dict[str, Any]:
    """A model.npz's arrays — X, y, classes, paths — or RuntimeError.

    Every header is read and checked BEFORE numpy allocates the array: an
    .npy header names its own shape, and a crafted one claiming a billion
    rows in a few bytes would otherwise have numpy reserve gigabytes. A
    header holding Python objects is refused outright — reading those IS
    unpickling — and the arrays are then loaded with allow_pickle=False
    regardless. ``cap`` (MAX_MODEL_BYTES unless smaller) is the most all
    of it may unpack to; "unpacked" in the result is what it did."""
    import numpy as np
    from numpy.lib import format as npf
    cap = min(cap, MAX_MODEL_BYTES) if cap > 0 else MAX_MODEL_BYTES
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
            unpacked = sum(i.file_size for i in infos)
            if unpacked > cap:
                raise RuntimeError(f"{where} unpacks to more than "
                                   f"{cap >> 20} MB{cap_what} — refused")
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
    return dict(_checked_model(arrays, where), unpacked=unpacked)


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
    and a rename that loses a race to another writer just takes the next.

    Never past v{_VERSION_MAX}: a folder the version reader does not match
    would be a version nothing can see. And never into a classifier that is
    no longer there — another app may have moved it away meanwhile, and
    making the folder again would leave a classifier nobody made."""
    if number and not 1 <= number <= _VERSION_MAX:
        raise RuntimeError(f"v{number} is past the last version number, "
                           f"v{_VERSION_MAX} — nothing was saved")
    if not _is_classifier(d):
        raise RuntimeError(_gone(d, "training"))
    vroot = d / VERSIONS
    tmp: Optional[Path] = None
    try:
        vroot.mkdir(exist_ok=True)
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
            if n > _VERSION_MAX:
                raise RuntimeError(
                    f"'{d.name}' has reached v{_VERSION_MAX}, the last "
                    f"version number — Save as to carry on under a new name. "
                    f"Nothing was saved.")
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


def _stamp(p: Path) -> Dict[str, int]:
    st = p.stat()
    return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}


def _write_stamp(d: Path, meta: Optional[Dict[str, Any]] = None) -> None:
    """mirror.json := the size and modification time of model.npz as this
    module just left it — how the next look tells "the copy I wrote" from
    "something an older build wrote since" without comparing any times.
    Advisory: failing to write it only means the next look decides by
    content, as it did before mirror.json existed."""
    try:
        doc = dict(_stamp(d / "model.npz"))
        if meta:
            doc.update(version=meta.get("version"), sha256=meta.get("sha256"))
        _write_json(d / MIRROR, doc)
    except OSError:
        pass


def _read_stamp(d: Path) -> Optional[Dict[str, Any]]:
    p = d / MIRROR
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(doc, dict) or not isinstance(doc.get("size"), int)
            or not isinstance(doc.get("mtime_ns"), int)):
        return None
    return doc


def _write_mirror(d: Path, vdir: Path,
                  meta: Optional[Dict[str, Any]] = None) -> str:
    """model.npz at the top := the current version's — for older builds.
    Returns "" or a note: the copy failing must not fail what it serves,
    because the version itself is already safe."""
    src = vdir / "model.npz"
    try:
        _atomic_write(d / "model.npz", lambda tmp: shutil.copyfile(src, tmp))
    except OSError as exc:
        return (f"Note: could not refresh model.npz, the copy kept for older "
                f"builds ({exc}).")
    _write_stamp(d, meta)
    return ""


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
    """Make the top-level model.npz the next version, byte for byte — and
    note it as the copy of that version, which it now is."""
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
    _write_stamp(d, meta)
    return meta


_ADOPTED_WHY = ("a model.npz trained without versions (an older build) after "
                "the last version; kept as the newest")


def _reconcile_plan(d: Path, write: bool) -> Tuple[str, Any]:
    """What _reconcile has to do, decided by looking only (the rules are in
    the module docstring, under VERSIONS): ("ok", notes), ("mirror",
    (meta, vdir)), ("adopt", (how, why)) or ("set_aside", error)."""
    root = d / "model.npz"
    nums = _version_numbers(d)
    newest = None
    damaged = False
    if nums:
        try:
            newest = _newest(d)
        except RuntimeError:
            # Left as it is and reported by whatever reads the newest
            # version; Train numbers a new one past it (see VERSIONS).
            damaged = True
    if not root.is_file():
        return ("mirror", newest) if newest else ("ok", [])
    try:
        sha = _root_model_sha(root)
    except RuntimeError as exc:
        if write:
            return "set_aside", exc
        if not nums:
            raise RuntimeError(
                f"the trained model {root} cannot be read ({exc}). It was left "
                f"as it is: press Train to train a new version from the marked "
                f"frames (your marks are kept).")
        return "ok", ["Note: model.npz (the copy kept for older builds) cannot "
                      "be read and was left as it is; the next Train sets it "
                      "aside."]
    if not nums:
        return "adopt", ("migrated", "trained before versions existed; "
                                     "accuracy not recorded")
    if newest and sha == newest[0]["sha256"]:
        return "ok", []
    stamp = _read_stamp(d)
    if stamp is not None:
        st = _stamp(root)
        if (stamp["size"], stamp["mtime_ns"]) == (st["size"], st["mtime_ns"]):
            # The copy this module wrote, of an older version: a crash came
            # between writing a version and refreshing the copy.
            return ("mirror", newest) if newest else ("ok", [])
        # Not the file this module left: an older build trained since — kept
        # even when its content equals an older version (MEASURED before
        # mirror.json: such a Train was silently undone).
        return "adopt", ("adopted", _ADOPTED_WHY)
    # No stamp (written before mirror.json existed): decided by content. An
    # older version whose meta cannot be read is hashed from its model.
    known, unreadable = set(), 0
    for n in (nums if damaged else nums[:-1]):
        vdir = d / VERSIONS / f"v{n}"
        try:
            known.add(_read_meta(vdir)["sha256"])
        except RuntimeError:
            try:
                known.add(_model_sha(_read_model_file(vdir / "model.npz")))
            except RuntimeError:
                unreadable += 1
    if sha in known:
        return ("mirror", newest) if newest else ("ok", [])
    if unreadable and not write:
        # Adopting would make an unverified model current on a READ
        # (MEASURED before: a stale copy plus one unreadable older meta made
        # Open replace the user's current model). Train may: it makes a new
        # current version anyway, and keeps this one as a version first.
        return "ok", ["Note: model.npz differs from the current version, and "
                      "an older version cannot be read to tell whether it is "
                      "a stale copy; it was left as it is (the next Train "
                      "keeps it as a version)."]
    return "adopt", ("adopted", _ADOPTED_WHY)


def _reconcile(d: Path, name: str, write: bool = False) -> List[str]:
    """Bring the top-level model.npz and the versions into agreement (the
    rules are in the module docstring) and return what a person should be
    told about it. ``write`` is True only for Train, the one action allowed
    to set an unreadable file aside; everything else reports it and leaves
    it exactly where it is.

    Decided first by looking only, so an Open or a Predict with nothing to
    do takes no lock and writes nothing; when there is something to do it
    is decided AGAIN under the classifier's lock — another app may have
    just done it (MEASURED before: three apps opening a classifier from
    before versions made v1, v2 and v3 of one model)."""
    action, arg = _reconcile_plan(d, write)
    if action == "ok":
        return list(arg)
    with _locked(d):
        action, arg = _reconcile_plan(d, write)
        if action == "ok":
            return list(arg)
        if action == "mirror":
            meta, vdir = arg
            note = _write_mirror(d, vdir, meta)
            return [note] if note else []
        if action == "set_aside":
            kept = _set_aside(d / "model.npz", "unusable")
            return [f"The old model.npz could not be read ({arg}); it was "
                    f"kept as {kept.name}."]
        how, why = arg
        meta = _adopt(d, name, how, why)
        if how == "migrated":
            return [f"The model trained before versions is now "
                    f"{_vid(name, meta)} — nothing was lost."]
        return [f"A model trained by an older build is now "
                f"{_vid(name, meta)}."]


# Refitting is ~0.1 s, but Predict is pressed repeatedly: keep the last few
# forests. Keyed by the version's folder and sha — a version never changes,
# so there is no modification time to race.
_CACHE: Dict[Tuple[str, str], Tuple[Any, Dict[str, Any]]] = {}
_CACHE_MAX = 8


def _load_model(name: Any):
    """(forest, X, classes, paths, meta, notes) for the current version."""
    d = store_dir(name)
    n = d.name
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

def _echo(current: Any) -> Tuple[str, List[str]]:
    """The classifier the window has open, and its classes — what a press
    that only ASKS for something hands back, so the window stays as it was
    (MEASURED before: such a press raised, the generated handler cleared the
    name box and the class list, and the next Train failed on '')."""
    cur = _name_of(current)
    if not cur:
        return "", []
    try:
        d = store_dir(cur)
        return d.name, list(_load_dir(d)["classes"])
    except RuntimeError:
        return cur, []


def open_classifier(name: Any, current: Any = "") -> Dict[str, Any]:
    """Load a classifier's classes, creating nothing until something is added.

    Says where it came from — in a store many apps share, the classifier
    called "frames" may be another app's — and what happened to it since
    ("history": "copied from frames v2 (...) on ...", one line each, for a
    list). A model trained before versions existed becomes v1 here, the
    first time it is opened. With nothing picked, ``current`` — the one the
    window has open — is opened again; with neither, it says to pick one.

    Keys: name, classes, version, origin, tags, history, summary
    """
    if not _name_of(name):
        if not _name_of(current):
            return {"name": "", "classes": [], "version": "", "origin": "",
                    "tags": [], "history": [],
                    "summary": "Pick a classifier in the list, or type its "
                               "name, then open it."}
        name = current
    d = store_dir(name)
    n = d.name
    data = _load_dir(d)
    version, problem, damaged, notes = "", "", False, []
    try:
        notes = _reconcile(d, n)
    except RuntimeError as exc:
        problem = str(exc)
    if not problem:
        try:
            newest = _newest(d)
            if newest:
                version = _vid(n, newest[0])
        except RuntimeError:
            damaged = True
    k = len(data["labels"])
    marked = f"{k} frame{'' if k == 1 else 's'} marked"
    new = _is_new(d)
    if version:
        head = f"Opened {version}: {marked}."
    elif damaged:
        head = (f"Opened '{n}': {marked}; its newest version is damaged and "
                f"was left as it is — press Train to make a new one.")
    elif problem:
        head = f"Opened '{n}': {marked}. The trained model has a problem: " \
               f"{problem}"
    elif new:
        head = (f"'{n}' is new: it will be recorded as made by "
                f"{_app_label(_this_app())} when its first class is added.")
    else:
        head = f"Opened '{n}' (not trained yet): {marked}."
    origin, tags, history, made = "", [], [], ""
    if not new:
        try:
            about = _read_about(d)
            origin, tags = _origin_text(about["origin"]), about["tags"]
            history = [_event_text(e) for e in about["lineage"]]
            # The origin's date is left to the "origin" key when a later
            # event is shown: the status line is short (Typhon's is 336x72).
            made = (f" From {_origin_text(about['origin'], dated=not history)}"
                    + (f"; {history[-1]}" if history else "")
                    + (f"; tags {', '.join(tags)}" if tags else "") + ".")
        except RuntimeError as exc:
            made = f" Where it came from cannot be read: {exc}"
    return {"name": n, "classes": list(data["classes"]), "version": version,
            "origin": origin, "tags": list(tags), "history": history,
            "summary": head + made + "".join(f" {x}" for x in notes)}


def add_class(name: Any, new_class: Any) -> Dict[str, Any]:
    cls = str(new_class or "").strip()
    if not _name_of(name):
        # No model in the box (a fresh Typhon's starts empty): asked about,
        # and the class just typed is kept for the Add class that follows.
        return {"classes": [], "cleared": cls,
                "summary": "Pick a saved model, or type a new model's name, "
                           "first — then Add class."}
    d = store_dir(name)
    if not cls:
        return {"classes": list(_load_dir(d)["classes"]), "cleared": "",
                "summary": "Type a class name in New class, then Add class."}
    refused = _owner_refusal(d, "added")
    if refused:
        # An answer, like "still labels 3 frames": raising made the handler
        # blank the class list and the class just typed (MEASURED).
        return {"classes": list(_load_dir(d)["classes"]), "cleared": cls,
                "summary": refused}
    with _locked(d):
        data = _load_dir(d)
        if cls in data["classes"]:
            return {"classes": list(data["classes"]), "cleared": "",
                    "summary": f"'{cls}' is already a class."}
        data["classes"].append(cls)
        _save_dir(d, data)
    return {"classes": list(data["classes"]), "cleared": "",
            "summary": f"Added '{cls}' to '{d.name}'. Pick it in the list, "
                       f"then mark frames."}


def remove_class(name: Any, selection: Any) -> Dict[str, Any]:
    d = store_dir(name)
    cls = _picked(selection)
    if not cls:
        return {"classes": list(_load_dir(d)["classes"]),
                "summary": "Pick a class in the list to remove it."}
    refused = _owner_refusal(d, "removed")
    if refused:
        return {"classes": list(_load_dir(d)["classes"]), "summary": refused}
    with _locked(d):
        data = _load_dir(d)
        n = _counts(data).get(cls, 0)
        if n:
            # Refused, not an error: removing it would throw away n labels
            # the user made. Re-marking those frames is the deliberate way
            # out.
            return {"classes": list(data["classes"]),
                    "summary": f"Not removed: '{cls}' still labels {n} "
                               f"frame(s). Re-mark them as another class "
                               f"first."}
        data["classes"] = [c for c in data["classes"] if c != cls]
        _save_dir(d, data)
    return {"classes": list(data["classes"]),
            "summary": f"Removed '{cls}'."}


def mark_frame(name: Any, folder: Any, frame: Any,
               selection: Any) -> Dict[str, Any]:
    d = store_dir(name)
    cls = _picked(selection)
    if not cls:
        raise RuntimeError("pick a class in the Classes list first")
    p = _frame_path(folder, frame)
    if _inside(d, p.parent):
        raise RuntimeError(_capture_refusal(d, p.parent, "marked"))
    _check_owner(d, "marked")
    with _locked(d):
        data = _load_dir(d)
        if cls not in data["classes"]:
            raise RuntimeError(f"'{cls}' is not a class of '{d.name}' — "
                               f"Open it again to refresh the list")
        was = data["labels"].get(str(p))
        data["labels"][str(p)] = cls
        # Marked HERE now, on this PC's own file: no longer a mark that an
        # import brought (see VERSIONS).
        imported = data.get("imported_labels")
        if isinstance(imported, list) and str(p) in imported:
            data["imported_labels"] = [x for x in imported if x != str(p)]
        _save_dir(d, data)
    change = f" (was '{was}')" if was and was != cls else ""
    return {"summary": f"{p.name} marked '{cls}'{change}. "
                       f"Marked so far: {_counts_text(data)}."}


def _first_sentence(exc: Exception) -> str:
    return str(exc).split(". ")[0]


def train(name: Any) -> Dict[str, Any]:
    """Fit on the marked frames and keep the result as a new version.

    A marked frame whose file is gone keeps counting: its features come from
    the current version, which stored them when it was trained — and a mark
    an IMPORT brought is never opened at all (see VERSIONS). Marks that give
    exactly the current model make no new version. A damaged newest version
    is left as it is, and the new one is numbered past it.

    The forest and its accuracy are computed with no lock held; the
    classifier is then looked at AGAIN under its lock, so a model another
    app saved meanwhile is not saved a second time, and one moved away
    meanwhile is not made again (see TWO APPS AT ONCE).

    Keys: summary, version, sha256
    """
    import numpy as np
    d = store_dir(name)
    n = d.name
    data = _load_dir(d)
    used = [c for c, k in _counts(data).items() if k]
    if len(used) < 2:
        raise RuntimeError(f"mark frames in at least two classes before "
                           f"training (marked: {_counts_text(data)})")
    _check_owner(d, "trained")
    # The capture folders are only ever read: a store inside the folder of a
    # marked frame is refused here, by the paths' TEXT — nothing is opened.
    here = os.path.normcase(str(d.resolve()))
    for parent in {os.path.normcase(os.path.dirname(str(p)))
                   for p in data["labels"]}:
        if _text_inside(here, parent):
            raise RuntimeError(_capture_refusal(d, parent, "trained"))
    imported = {str(p) for p in data.get("imported_labels") or []}
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
            notes.append(f"The current version could not be read "
                         f"({_first_sentence(exc)}), so no stored features "
                         f"were reused.")
        classes = [c for c in data["classes"] if c in used]
        rows, ys, paths, missing, reused = [], [], [], [], 0
        for path, cls in sorted(data["labels"].items()):
            if cls not in classes:
                continue
            if path in imported:
                # A path on the PC that made the mark: never opened here.
                if path in stored:
                    rows.append(stored[path])
                    reused += 1
                else:
                    missing.append(Path(path).name)
                    continue
            elif Path(path).is_file():
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
        # Same marks, same deterministic forest: the same model. Its accuracy
        # was measured when it was made, so it is not re-run (leave-one-out
        # refits the forest once per frame).
        quality = ("" if prior and prior[0]["sha256"] == sha
                   else _accuracy(model["X"], model["y"]))
        with _locked(d):
            if not _is_classifier(d):
                raise RuntimeError(_gone(d, "training"))
            try:
                current = _newest(d)
            except RuntimeError:
                current = None
            made = not (current and current[0]["sha256"] == sha)
            if not made:
                meta, vdir = current
                quality = (meta.get("accuracy") or quality
                           or _accuracy(model["X"], model["y"]))
                vid = _vid(n, meta)
                tail = (f" Unchanged: still {vid} — no new version."
                        if prior and prior[0]["sha256"] == sha else
                        f" Another app saved this same model a moment ago: "
                        f"{vid} — no new version.")
            else:
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
            mirror = _write_mirror(d, vdir, meta)
            if mirror:
                notes.append(mirror)
    if made:
        try:
            about = _read_about(d)
        except RuntimeError:
            about = None
        owner = _owner(about, honour_shared=False) if about else None
        if owner and not _same_app(owner, _this_app()):
            # The store is shared: the current version is every app's.
            notes.append(f"'{n}' belongs to {_app_label(owner)}; {vid} is "
                         f"its current model there too.")
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
    n = _canon(name)
    forest, X, classes, paths, meta, notes = _load_model(n)
    p = _frame_path(folder, frame)
    x = features(p)
    best, share = _share_text(forest.predict_proba(x[None, :])[0])
    cls = classes[int(forest.classes_[best])]
    near = Path(paths[_nearest(X, x)]).name
    vid = _vid(n, meta)
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
    the reader skips (and counts) what it cannot parse. Under the
    classifier's lock: an append is seek-to-end-then-write on Windows, and
    two apps appending at once could write at the same place (MEASURED: one
    record in 800 lost). Never into a classifier moved away meanwhile."""
    p = d / RUNS
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with _locked(d):
            if not _is_classifier(d):
                return ("it was moved or deleted (by another app?) while it "
                        "was classifying")
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
    except (OSError, RuntimeError) as exc:
        return str(exc)


def classify_folder(name: Any, folder: Any) -> Dict[str, Any]:
    """Every image in ``folder``, in capture order, with its likely class.
    A row shows the forest's agreement when it is below 80%, so an uncertain
    call does not read like a certain one.

    Each call is RECORDED in the classifier's runs.jsonl (in the store —
    the capture folder is only read): when, the folder, each capture run in
    it, the version id and sha, the count per class, this PC and the app.
    "classified_with" is the line classified_with(folder) gives from now
    on, made from the record just written (no store search), so one link
    can fill the window's "classified with" line too; a run that could not
    be recorded says so there.

    Keys: rows, counts, version, sha256, classified_with, summary
    """
    d = store_dir(name)
    n = d.name
    with _using(d):
        forest, X, classes, _paths, meta, notes = _load_model(n)
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
    recorded = line = ""
    if rows:
        record: Dict[str, Any] = {}
        if _inside(d, f):
            # MEASURED before: with the store pointed into the capture
            # folder, this said "recorded" and the folder gained the record.
            why = ("the classifiers are kept inside this capture folder, "
                   "which is only ever read")
        else:
            runs: Dict[str, int] = {}
            for nm in names:
                run = _run_of(nm)
                runs[run] = runs.get(run, 0) + 1
            me = _this_app()
            record = {
                "when": _now(), "at": time.time(), "folder": str(f.resolve()),
                "runs": runs, "frames": len(rows), "unreadable": unreadable,
                "classifier": n, "version": meta["version"],
                "version_id": vid, "sha256": meta["sha256"], "counts": tally,
                "host": _host(),
                "by": {k: str(me.get(k) or "") for k in
                       ("app", "project", "project_id", "script",
                        "script_id")}}
            why = _append_run(d, record)
        recorded = (f" — classified with {vid}, recorded." if not why else
                    f" — classified with {vid} (NOT recorded: {why}).")
        line = (_latest_text(record) if not why else
                f"Classified with {vid} just now — NOT recorded: {why}")
    summary = (f"{len(rows)} frames: "
               + ", ".join(f"{c} {k}" for c, k in tally.items())
               + (f"; {unreadable} unreadable" if unreadable else "")
               + recorded + ((" " + " ".join(notes)) if notes else ""))
    return {"rows": rows, "counts": tally, "version": vid,
            "sha256": meta["sha256"], "classified_with": line,
            "summary": summary}


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
        "lineage": [], "history": [], "tags": [], "owner": {}, "problem": ""}
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
        # The app it BELONGS to (WHOSE IT IS), "shared" or not: what "This
        # app" lists. A copy whose maker was not recorded falls back to
        # where it was made.
        owner = _owner(about, honour_shared=False)
        info["owner"] = owner if owner is not None else (
            about["origin"] if about.get("recorded") else {})
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
    for a project built again (a new id), the same project AND app name.
    Outside a Designer project: the same script (a hash of its full path)
    — or, for that app copied elsewhere, the same script name in a folder
    of the same name. Two apps that are both main.py are two apps."""
    if not origin or origin.get("unknown"):
        return False
    if me.get("project_id") and origin.get("project_id") == me["project_id"]:
        return True
    if me.get("project"):
        return (origin.get("project") == me["project"]
                and origin.get("app") == me["app"])
    if not me.get("script") or origin.get("project"):
        return False
    if me.get("script_id") and origin.get("script_id") == me["script_id"]:
        return True
    return (origin.get("script") == me["script"]
            and str(origin.get("script_folder") or "")
            == str(me.get("script_folder") or ""))


_DASH = re.compile(r"\s*[‐-―−\-]+\s*")


def _norm(text: Any) -> str:
    """For matching a filter: lower case, one kind of dash, single spaces —
    "Barbie Capture v5 — live" and "barbie  capture v5 - live" are one."""
    return " ".join(_DASH.sub(" - ", str(text or "")).lower().split())


def _matches(info: Dict[str, Any], how: str, value: str,
             me: Dict[str, str]) -> bool:
    """Whether a classifier passes the filter. App, project and free words
    match PART of a name (MEASURED before: only the exact window title, em
    dash included, found anything); a tag matches whole."""
    o, v = info["origin"] or {}, _norm(value)
    tags = [_norm(t) for t in info["tags"]]
    if how == "all":
        return True
    if how == "this":
        # What BELONGS to it, not what it made: MEASURED before, Typhon's
        # trained copy of Barbie's model was missing from "This app" and from
        # its spin-off bundle — the one model only Typhon may change — while
        # Barbie's "This app" listed it.
        return _same_app(info.get("owner") or {}, me)
    if how == "unknown":
        return bool(o.get("unknown"))
    if how == "tag":
        return v in tags
    known = not o.get("unknown")
    if how == "app":
        return known and v in _norm(o.get("app"))
    if how == "project":
        return known and v in _norm(o.get("project"))
    words = ([_norm(o.get(k)) for k in ("app", "project", "example")]
             if known else [])
    return (any(v in w for w in words if w) or any(v in t for t in tags)
            or v in _norm(info["name"]))


def _show_label(how: str, value: str, me: Dict[str, str]) -> str:
    if how == "all":
        return FILTER_ALL
    if how == "this":
        return f"{FILTER_THIS_APP}: {_app_flat(me)}"
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


def _few(names: List[str], most: int = 6) -> str:
    text = ", ".join(names[:most])
    return text + (f" and {len(names) - most} more" if len(names) > most
                   else "")


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
    some = f"saved classifier{'' if total == 1 else 's'}"
    if not total:
        summary = ("No saved classifiers yet — type a name, add classes and "
                   "mark frames.")
    elif not k:
        summary = (f"None of the {total} {some} match {lib['label']} — "
                   f"choose {FILTER_ALL} to see them all.")
    elif lib["filtered"]:
        summary = f"{k} of {total} {some} ({lib['label']}): {_few(lib['names'])}."
    else:
        summary = f"{total} {some}: {_few(lib['names'])}."
    if not why.startswith("the vault"):
        summary += f" Kept in {store} ({why})."
    return dict(_listed(lib), details=lib["details"], store=str(store),
                summary=summary)


def store_info() -> Dict[str, Any]:
    """Where classifiers are kept and why there, and which app is running —
    what a classifier made now will say it came from. A store that sits in
    a folder of captured frames is pointed out: the capture folder is only
    ever read, so Mark, Train and the run record will not write there.

    Keys: store, how, app, summary
    """
    store, why = _store_choice()
    label = _app_label(_this_app())
    host = _host()
    warn = ""
    if _holds_frames(store) or _holds_frames(store.parent):
        warn = (" Careful: that is (in) a capture folder, which is only "
                "ever read — Mark, Train and the run record will not write "
                f"there; point {STORE_ENV} or {STORE_CONFIG} at another "
                "folder.")
    return {"store": str(store), "how": why, "app": label,
            "summary": f"Classifiers are kept in {store} ({why}). New ones "
                       f"made here are recorded as made by {label}"
                       + (f" on {host}" if host else "") + "." + warn}


def _existing(name: Any, doing: str) -> Tuple[str, Path]:
    n = _name_of(name)
    if not n:
        raise RuntimeError(f"pick a classifier in the list (or type its name) "
                           f"to {doing} it")
    d = store_dir(n)
    if not d.is_dir():
        raise RuntimeError(f"there is no classifier '{d.name}' to {doing}")
    return d.name, d


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
            or re.match(r"^(classes\.json|model\.npz|meta\.json|about\.json|"
                        r"mirror\.json)\.\w+$", x)]


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


def _new_name(new_name: Any) -> str:
    """The name typed for a copy, a rename or an import: "" when nothing
    was typed, else a usable name (an unusable one RAISES; the library
    presses ask about it first, see _unusable)."""
    new = _name_of(new_name)
    return _check_name(new) if new else ""


def _unusable(new_name: Any, nothing: str) -> str:
    """What to say when the name typed for a copy, a rename or an import
    cannot be a name — "" when it can, or when none was typed. ASKED, not
    raised: it is a name to correct and press again, and MEASURED before,
    the raise made the generated handler blank the open model's name and
    classes ('my birds' for Save as, 'a/b' for Rename)."""
    new = _name_of(new_name)
    if not new:
        return ""
    try:
        _check_name(new)
    except RuntimeError as exc:
        return f"{_sentence(exc)} Nothing was {nothing}."
    return ""


def _sentence(exc: Any) -> str:
    """A refusal's words as a sentence for the status line: a capital
    first letter and a full stop ("there is no classifier 'x' to copy" ->
    "There is no classifier 'x' to copy.")."""
    text = str(exc).strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def _typed(new_name: Any) -> str:
    """The New name box's text as typed — what a press that asks for
    another name hands back as "cleared", so the box keeps it to correct."""
    return str(new_name or "").strip() if not isinstance(
        new_name, (list, tuple)) else _name_of(new_name)


def save_as(name: Any, new_name: Any, show: Any = "") -> Dict[str, Any]:
    """Copy a classifier under a new name — its classes, marks, every
    version, origin and tags — and switch to the copy. The copy keeps the
    ORIGINAL's origin (it was made there) and its lineage adds "copied from
    <name> <version>"; the copy BELONGS to the app that made it (see WHOSE
    IT IS). Never replaces an existing classifier: a taken name, none
    typed, or one that cannot be a name is asked about — the open
    classifier stays open. The run record stays with the original: it is
    the original's history.

    "cleared" is what the New name box should hold afterwards: "" once the
    name was used, the name as typed when the press asks for another.

    Keys: name, classes, cleared, rows, table, names, filters, summary
    """
    typed = _typed(new_name)
    if not _name_of(name):
        return dict(_listed(_library(show)), name="", classes=[],
                    cleared=typed,
                    summary="Open (or type) the classifier to copy, then "
                            "press Save as.")
    try:
        n, d = _existing(name, "copy")
    except RuntimeError as exc:
        keep, classes = _echo(name)
        return dict(_listed(_library(show)), name=keep, classes=classes,
                    cleared=typed,
                    summary=f"{_sentence(exc)} Nothing was copied.")
    bad = _unusable(new_name, "copied")
    new = "" if bad else _new_name(new_name)
    clash = _taken(new) if new else None
    if not new or clash:
        return dict(_listed(_library(show)), name=n,
                    classes=list(_load_dir(d)["classes"]), cleared=typed,
                    summary=(bad or (_clash_text(clash) + "." if clash else
                                     "Type the new name in New name, then "
                                     "press Save as.")))
    target = classifier_store() / new
    _load_dir(d)                  # a damaged classes.json is reported, not copied
    about = _read_about(d)        # ... and so is a damaged about.json
    if about["newer"]:
        raise RuntimeError(_newer_text(n))
    notes: List[str] = []
    with _using(d), _locked(d):
        notes = _reconcile(d, n)            # a pre-versions model becomes v1 first
        try:
            newest = _newest(d)
        except RuntimeError as exc:
            newest = None
            notes.append(f"Its newest version is damaged "
                         f"({_first_sentence(exc)}) and was copied as it is.")
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
            return dict(_listed(_library(show)), name=n,
                        classes=list(_load_dir(d)["classes"]), cleared=typed,
                        summary=_clash_text(_taken(new) or new) + ".")
        except OSError as exc:
            raise RuntimeError(f"cannot copy '{n}' to '{new}': "
                               f"{_os_error(exc)}")
    lib = _library(show)
    try:
        copy = _newest(target)
    except RuntimeError:
        copy = None
    what = f"({_vid(new, copy[0])})" if copy else "(not trained yet)"
    source = f", copied from {copied['from_version']}" if newest else ""
    note = (" " + " ".join(notes)) if notes else ""
    return dict(_listed(lib), name=new,
                classes=list(_load_dir(target)["classes"]), cleared="",
                summary=f"Saved '{n}' as '{new}' {what}{source}; its origin "
                        f"is kept. Now using '{new}'.{note}")


def rename_classifier(name: Any, new_name: Any, current: Any = "",
                      show: Any = "") -> Dict[str, Any]:
    """Rename a classifier: its marks, versions, origin, tags and run record
    move with it, and its lineage adds "renamed from <old name>". Never
    replaces an existing classifier, and is refused while it is training or
    classifying. ``name`` is the row picked in the list; with none picked,
    ``current`` — the name the window has open — is renamed. "name" comes
    back as the new name when the open one was renamed, so the name box
    follows it, and as the open one otherwise. Nothing typed, nothing
    picked, a taken name or one that cannot be a name is asked about, never
    an error — and so is another app's classifier (see WHOSE IT IS): the
    app that owns it would find it gone. "cleared" is what the New name box
    should hold afterwards (see save_as).

    Keys: name, cleared, rows, table, names, filters, summary
    """
    keep, _classes = _echo(current)
    typed = _typed(new_name)

    def ask(text: str) -> Dict[str, Any]:
        return dict(_listed(_library(show)), name=keep, cleared=typed,
                    summary=text)

    if not (_name_of(name) or _name_of(current)):
        return ask("Pick the classifier to rename in the list, then press "
                   "Rename.")
    try:
        n, d = _existing(name if _name_of(name) else current, "rename")
    except RuntimeError as exc:
        return ask(f"{_sentence(exc)} Nothing was renamed.")
    bad = _unusable(new_name, "renamed")
    if bad:
        return ask(bad)
    new = _new_name(new_name)
    if not new:
        return ask(f"Type the new name for '{n}' in New name, then press "
                   f"Rename.")
    if new == n:
        return ask(f"'{n}' already has that name.")
    target = classifier_store() / new
    if new.lower() != n.lower():
        clash = _taken(new)
        if clash or target.exists():
            return ask(_clash_text(clash or new) + ".")
    refused = _owner_refusal(d, "renamed")
    if refused:
        return ask(refused)
    about = _read_about(d)        # damaged: refused before anything moves
    if about["newer"]:
        raise RuntimeError(_newer_text(n))
    _refuse_if_busy(d, n, "rename it")
    with _locked(d):
        try:
            os.rename(d, target)
        except OSError as exc:
            raise _in_use(exc, n, d)
    _forget(d)
    renamed = _event("renamed", **{"from": n})
    try:
        with _locked(target):
            _write_about(target,
                         dict(about, lineage=about["lineage"] + [renamed]))
        noted = ""
    except (RuntimeError, OSError) as exc:
        noted = f" (Its history does not say so: {exc}.)"
    cur = _name_of(current)
    lib = _library(show)
    return dict(_listed(lib),
                name=new if not cur or cur.lower() == n.lower() else keep,
                cleared="",
                summary=f"Renamed '{n}' to '{new}'. Its versions, origin and "
                        f"run record went with it.{noted}")


def delete_classifier(name: Any, current: Any = "",
                      show: Any = "") -> Dict[str, Any]:
    """"Delete" a classifier by MOVING it to <store>/.deleted/<name>_<stamp>
    — nothing is erased, and the summary says where it went so moving it
    back restores it. Refused while it is training or classifying. It needs
    a row PICKED: with none, it asks — Delete never falls back to the open
    classifier. ``current`` is the name the window has open: when that is
    the one deleted, "name" and "classes" come back empty so the window
    stops showing it; otherwise they are the open classifier's. Another
    app's classifier is not deleted (see WHOSE IT IS) — asked about, like
    a pick that is missing: MEASURED before, Typhon deleted Barbie's
    "frames" with one press, and Barbie's next Open said "'frames' is new".

    Keys: name, classes, moved_to, rows, table, names, filters, summary
    """
    keep, classes = _echo(current)

    def ask(text: str) -> Dict[str, Any]:
        return dict(_listed(_library(show)), name=keep, classes=classes,
                    moved_to="", summary=text)

    if not _name_of(name):
        return ask("Pick the classifier to delete in the list, then press "
                   "Delete.")
    n, d = _existing(name, "delete")
    refused = _owner_refusal(d, "deleted")
    if refused:
        return ask(refused)
    _refuse_if_busy(d, n, "delete it")
    bin_dir = classifier_store() / DELETED
    bin_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = bin_dir / f"{n}_{stamp}"
    k = 2
    while dest.exists():
        dest = bin_dir / f"{n}_{stamp}_{k}"
        k += 1
    with _locked(d):
        try:
            os.rename(d, dest)
        except OSError as exc:
            raise _in_use(exc, n, d)
    _forget(d)
    if not keep or keep.lower() == n.lower():
        keep, classes = "", []
    lib = _library(show)
    return dict(_listed(lib), name=keep, classes=classes, moved_to=str(dest),
                summary=f"Deleted '{n}': moved aside to "
                        f"{DELETED}{os.sep}{dest.name} in the store — move "
                        f"it back to restore it.")


def _tag_of(tag: Any) -> str:
    text = " ".join((_picked(tag) if isinstance(tag, (list, tuple))
                     else str(tag or "")).split())
    if len(text) > TAG_MAX_LEN:
        raise RuntimeError(f"a tag is at most {TAG_MAX_LEN} characters — "
                           f"'{text[:TAG_MAX_LEN]}...' is {len(text)}")
    if not text.isprintable():
        raise RuntimeError("a tag is one line of ordinary text")
    return text


def add_tag(name: Any, tag: Any, show: Any = "",
            current: Any = "") -> Dict[str, Any]:
    """Tag a classifier with words of the user's own ("lab 2", "night
    shift") — what the list can then be filtered by. Separate from its
    origin, which no tag changes; the tag "shared" lets every app change
    it (see WHOSE IT IS). A tag already there (in any case) is not added
    twice; nothing typed is a soft case, not an error. The row picked in
    the list is tagged — with none picked, ``current``, the open one.

    Keys: name, tags, cleared, rows, table, names, filters, summary
    """
    t = _tag_of(tag)
    if not (_name_of(name) or _name_of(current)):
        return dict(_listed(_library(show)), name="", tags=[], cleared=t,
                    summary="Pick the classifier to tag in the list, then "
                            "press Add tag.")
    n, d = _existing(name if _name_of(name) else current, "tag")
    with _locked(d):
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


def remove_tag(name: Any, tag: Any, show: Any = "",
               current: Any = "") -> Dict[str, Any]:
    """Take one of the user's tags off a classifier (any case). The tag may
    be typed, or picked in a list of its tags. Its origin is not a tag and
    cannot be taken off. The row picked in the list is untagged — with none
    picked, ``current``, the open one.

    Keys: name, tags, rows, table, names, filters, summary
    """
    t = _tag_of(tag)
    if not (_name_of(name) or _name_of(current)):
        return dict(_listed(_library(show)), name="", tags=[],
                    summary="Pick the classifier to untag in the list, then "
                            "press Remove tag.")
    n, d = _existing(name if _name_of(name) else current, "untag")
    with _locked(d):
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


def list_versions(name: Any, current: Any = "") -> Dict[str, Any]:
    """Every version of a classifier, newest first: number, id, when, how
    many frames of each class, and the accuracy measured when it was made.
    A damaged version is a row saying so. "versions" is the meta.json of
    each, for code. The row picked in the list — with none picked,
    ``current``, the open one.

    Keys: rows, versions, current, summary
    """
    if not (_name_of(name) or _name_of(current)):
        return {"rows": [], "versions": [], "current": "",
                "summary": "Pick a classifier in the list to see its "
                           "versions."}
    n, d = _existing(name if _name_of(name) else current,
                     "list the versions of")
    try:
        notes = _reconcile(d, n)
    except RuntimeError as exc:
        notes = [f"The trained model has a problem: {exc}"]
    rows, metas = [], []
    for num in reversed(_version_numbers(d)):
        vdir = d / VERSIONS / f"v{num}"
        try:
            meta = _read_meta(vdir)
        except RuntimeError as exc:
            rows.append(f"v{num}   DAMAGED: {_first_sentence(exc)}")
            continue
        metas.append(meta)
        fpc = meta.get("frames_per_class")
        per = (", ".join(f"{c} {k}" for c, k in fpc.items())
               if isinstance(fpc, dict) else "")
        acc = str(meta.get("accuracy") or "").split(":")[0]
        how = {"migrated": " · from before versions",
               "adopted": " · from an older build",
               "imported": " · imported"}.get(str(meta.get("how")), "")
        rows.append(f"v{num} ({meta['sha256'][:8]})   "
                    f"{str(meta.get('created') or '')[:16]} · "
                    f"{meta.get('frames', '?')} frames: {per}"
                    + (f" · {acc}" if acc else "") + how)
    current_id = ""
    if rows:
        try:
            newest = _newest(d)
            current_id = _vid(n, newest[0]) if newest else ""
        except RuntimeError:
            notes.append("The newest version is damaged — press Train to make "
                         "a new one.")
    note = (" " + " ".join(notes)) if notes else ""
    summary = (f"'{n}' has {len(rows)} version(s); the current model is "
               f"{current_id}.{note}" if current_id else
               f"'{n}' has {len(rows)} version(s).{note}" if rows else
               f"'{n}' has not been trained yet.{note}")
    return {"rows": rows, "versions": metas, "current": current_id,
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
        # A FOLDER that is not there — not a file name: Typhon's export box
        # is a folder picker, and MEASURED before, a typed folder that did
        # not exist became "<that folder>.typhon-classifier.zip" beside it.
        raise RuntimeError(f"{p} is not a folder — choose a folder that "
                           f"exists, or a file name ending in .zip (nothing "
                           f"was exported)")
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
    """One classifier's export file, in memory, and what it holds — checked
    with import's own checks before it is handed back, so a file the other
    PC would refuse is refused HERE, where it can still be fixed."""
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
        try:
            parsed = (json.loads(doc.read_text(encoding="utf-8"))
                      if doc.is_file() else {"classes": [], "labels": {}})
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"cannot read {doc}: {exc} — nothing exported")
        if not isinstance(parsed, dict) or not isinstance(
                parsed.get("classes", []), list):
            raise RuntimeError(f"cannot read {doc}: it is not a classifier's "
                               f"classes file — nothing exported")
        # Every class the model can ANSWER is listed, even one the marks no
        # longer use. MEASURED before: Mark a class, Train, re-mark that
        # frame, Remove class — and the other PC refused the export ("the
        # model has classes its classes.json does not"), with every other
        # classifier of its bundle.
        listed = [str(c) for c in parsed.get("classes") or []]
        parsed["classes"] = listed + [c for c in model["classes"]
                                      if c not in listed]
        classes_bytes = json.dumps(parsed, indent=2,
                                   ensure_ascii=False).encode("utf-8")
        about = _read_about(d)
        runs_bytes, runs, bad_runs = _runs_for_export(d)
        answers, width = list(model["classes"]), int(model["X"].shape[1])
        del model
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
        "model_sha256": meta["sha256"], "classes": answers,
        "feature_layout": FEATURE_LAYOUT, "feature_length": width,
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
    data = buf.getvalue()
    try:
        _read_export_bytes(data, f"the export of '{n}'")
    except RuntimeError as exc:
        raise RuntimeError(f"'{n}' was not exported: the other PC would "
                           f"refuse the file ({exc})")
    return data, {"version_id": vid, "version": meta["version"],
                  "sha256": meta["sha256"], "about": about,
                  "runs": runs, "bad_runs": bad_runs, "notes": notes}


def export_classifier(name: Any, destination: Any,
                      current: Any = "") -> Dict[str, Any]:
    """Write the current version as ONE file another PC can import — the
    model, its marks, its origin, lineage and tags, and its run record. The
    row picked in the list is exported — with none picked, ``current``, the
    open one.

    ``destination`` is a folder that exists (the file is named <name>-v<N>.
    typhon-classifier.zip, never over an existing file) or a file name
    ending in .zip (an existing file is replaced only when it is itself a
    classifier export). Anything else is a folder that is not there, and is
    refused. The summary names the file; "path" is where it is.

    Keys: path, version, sha256, summary
    """
    n, d = _existing(name if _name_of(name) else current, "export")
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
    return {"path": str(out), "version": ex["version_id"],
            "sha256": ex["sha256"],
            "summary": f"Exported {ex['version_id']} as {out.name} "
                       f"({max(1, len(data) // 1024)} KB). On the other PC, "
                       f"press Import and pick it."
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
    k = len(names)
    tail = ("" if not skipped else
            " Not in it: " + "; ".join(f"{s['name']} ({_first_sentence(s['why'])})"
                                       for s in skipped) + ".")
    return {"path": str(out), "names": names,
            "skipped": [s["name"] for s in skipped],
            "summary": f"Exported {k} classifier{'' if k == 1 else 's'} "
                       f"({lib['label']}) as {out.name} "
                       f"({max(1, out.stat().st_size // 1024)} KB): "
                       f"{_few(names)}. Import that one file on the other "
                       f"PC.{tail}"}


def export_this_app(destination: Any) -> Dict[str, Any]:
    """Everything that belongs to the RUNNING app (what it made, and its
    own copies and imports — see WHOSE IT IS), in one bundle: the models an
    app going its own way can change. export_classifiers with the "This
    app" filter, for a button that needs no filter box.

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


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _counts_ok(v: Any) -> bool:
    return isinstance(v, dict) and all(isinstance(k, str) and _int(c)
                                       for k, c in v.items())


_META_TEXT = ("created", "how", "accuracy", "note", "host", "by",
              "trained_how")
_META_INT = ("frames", "feature_layout", "feature_length", "reused_features",
             "skipped_missing")


def _meta_ok(meta: Any) -> bool:
    """Every field of a version's meta.json that this module reads is of
    the kind it must be. MEASURED before: frames_per_class as text made
    Versions raise AttributeError for that classifier for good."""
    if not isinstance(meta, dict):
        return False
    if any(k in meta and not isinstance(meta[k], str) for k in _META_TEXT):
        return False
    if any(k in meta and not _int(meta[k]) for k in _META_INT):
        return False
    if "frames_per_class" in meta and not _counts_ok(meta["frames_per_class"]):
        return False
    return "classes" not in meta or (
        isinstance(meta["classes"], list)
        and all(isinstance(c, str) for c in meta["classes"]))


_RUN_TEXT = ("when", "folder", "host", "classifier", "version_id", "sha256")


def _run_ok(rec: Any) -> bool:
    """A run record whose every field this module reads is of the kind it
    must be. MEASURED before: one record with a NUL in its folder made
    "Which model?" raise for EVERY folder on the PC — Delete did not help,
    deleted classifiers' records are searched too — and counts that were a
    list raised AttributeError."""
    if not isinstance(rec, dict):
        return False
    if any(k in rec and not isinstance(rec[k], str) for k in _RUN_TEXT):
        return False
    folder = rec.get("folder")
    if isinstance(folder, str) and ("\x00" in folder or len(folder) > 4096):
        return False
    if any(k in rec and not _counts_ok(rec[k]) for k in ("counts", "runs")):
        return False
    if any(k in rec and not _int(rec[k])
           for k in ("frames", "unreadable", "version")):
        return False
    at = rec.get("at")
    if at is not None and (not isinstance(at, (int, float))
                           or isinstance(at, bool) or not math.isfinite(at)):
        return False
    return "by" not in rec or isinstance(rec["by"], dict)


def _read_export_bytes(raw: bytes, label: str,
                       budget: int = 0, budget_what: str = ""
                       ) -> Dict[str, Any]:
    """Every member of one classifier export, checked. Nothing is written
    before this returns, and nothing is ever extracted by a name taken from
    the zip. The model's arrays are checked and then DROPPED — what comes
    back holds its classes, frame count and packed bytes, so a bundle of
    many is never all unpacked at once (MEASURED before: a 0.02 MB bundle
    of five held 526 MB once checked). ``budget`` caps what the model may
    unpack to, below MAX_MODEL_BYTES."""
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
    if not _int(fv) or fv < 1:
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
    if (not isinstance(meta, dict) or not _int(meta.get("version"))
            or meta["version"] < 1
            or not _SHA_RE.match(str(meta.get("sha256", "")))
            or meta["sha256"] != manifest.get("model_sha256")
            or not _meta_ok(meta)):
        raise RuntimeError(f"{label}: meta.json does not describe its model "
                           f"— refused")
    if meta["version"] > _IMPORT_VERSION_MAX:
        raise RuntimeError(f"{label} says it is version {meta['version']}; "
                           f"an import may be at most version "
                           f"{_IMPORT_VERSION_MAX}, which leaves room to "
                           f"train it further here — refused, nothing was "
                           f"imported")
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
                    if not _run_ok(rec):
                        raise ValueError("a line is not a run record")
                    runs.append(rec)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"{label}: its run record is not readable "
                               f"({exc}) — refused")
    # A record that does not say which PC made it was made on the PC that
    # exported it: said so, so it never answers for a folder HERE (see THE
    # RUN RECORD) — and its folder is never looked up on this PC.
    source_host = str(manifest.get("host") or "") or "another PC"
    for rec in runs:
        if not str(rec.get("host") or "").strip():
            rec["host"] = source_host
    model = _read_model_bytes(blobs["model.npz"], f"model.npz in {label}",
                              cap=budget, cap_what=budget_what)
    if _model_sha(model) != meta["sha256"]:
        raise RuntimeError(f"model.npz in {label} is not the model its "
                           f"meta.json describes — refused")
    # Every class the model can answer is a class: an export written before
    # this (marks re-marked and the class removed after Train) lists fewer.
    classes = list(doc["classes"]) + [c for c in model["classes"]
                                      if c not in doc["classes"]]
    return {"manifest": manifest, "meta": meta,
            "classes": dict(doc, classes=classes),
            "model_classes": list(model["classes"]),
            "frames": int(len(model["y"])), "unpacked": model["unpacked"],
            "model_bytes": blobs["model.npz"], "about": about, "runs": runs,
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
    anything is imported — a bundle with one bad member imports nothing.
    One model is unpacked at a time, and all of them together may unpack to
    MAX_BUNDLE_BYTES at most: the cap is on the bundle, not on each of the
    up to MAX_BUNDLE_MEMBERS classifiers in it."""
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"cannot read {p}: {exc}")
    if size > MAX_BUNDLE_BYTES:
        raise RuntimeError(f"{p.name} is {size >> 20} MB — larger than any "
                           f"classifier bundle; refused")
    try:
        # Read from the file: the members are read (capped) one by one, so
        # a whole second copy of the bundle is never held as well.
        zf = zipfile.ZipFile(p)
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
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
    if not _int(fv) or fv < 1:
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
    for e in entries:
        nm = str(e.get("name") or "")
        if not _NAME_RE.match(nm) or nm.lower() in _DEVICE_NAMES:
            raise RuntimeError(f"{p.name}: {nm!r} cannot be a classifier "
                               f"name — refused, nothing was imported")
    out = []
    used = 0
    for e in entries:
        data = blobs[e["file"]]
        if hashlib.sha256(data).hexdigest() != str(e.get("sha256")):
            raise RuntimeError(f"{e['file']} in {p.name} does not match its "
                               f"checksum — the file is damaged or was "
                               f"altered; nothing was imported")
        left = MAX_BUNDLE_BYTES - used
        if left <= 0:
            raise RuntimeError(f"{p.name} unpacks to more than "
                               f"{MAX_BUNDLE_BYTES >> 20} MB — refused")
        got = _read_export_bytes(
            data, f"{e['file']} in {p.name}", budget=left,
            budget_what=(f" (what is left of the {MAX_BUNDLE_BYTES >> 20} MB "
                         f"a whole bundle may unpack to)"))
        used += got["unpacked"]
        if (got["manifest"].get("name") != e.get("name")
                or got["meta"]["sha256"] != e.get("model_sha256")):
            raise RuntimeError(f"{p.name}: {e['file']} is not the classifier "
                               f"bundle.json says it is — refused")
        out.append((e, got))
    return index, out


def _import_path(path: Any) -> str:
    """The file a picker holds, as text — "" when none was chosen."""
    return (_picked(path) if isinstance(path, (list, tuple))
            else str(path or "").strip().strip('"'))


def _same_classifier(existing: str, got: Dict[str, Any]) -> bool:
    """Whether the classifier ``existing`` here already IS what ``got``
    holds: the same current model and the same origin, and either the same
    name (it is where the file was exported from, or an import of it) or a
    lineage that records importing this very file. A copy that merely
    holds the same model under another name is another classifier."""
    d = classifier_store() / existing
    try:
        newest = _newest(d)
        about = _read_about(d)
    except RuntimeError:
        return False
    if (newest is None or newest[0]["sha256"] != got["meta"]["sha256"]
            or about["origin"] != got["about"]["origin"]):
        return False
    if existing.lower() == str(got["manifest"].get("name") or "").lower():
        return True
    return any(e.get("event") == "imported"
               and e.get("file_sha256") == got["file_sha256"]
               for e in about["lineage"])


def _import_one(got: Dict[str, Any], target: str, source: str) -> str:
    """Put one checked export into the store as ``target``; returns its
    version id. All there or not there (staged, then renamed); never over
    anything (FileExistsError when the name was taken meanwhile).

    Every mark it brings is listed as "imported_labels": a path on the PC
    that made it, which is never opened here (see VERSIONS)."""
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
    labels = dict(doc.get("labels") or {})
    marks = {"classes": list(doc["classes"]), "labels": labels}
    about = got["about"]
    event = _event("imported", file=source, file_sha256=got["file_sha256"],
                   from_name=imported_from["source_name"],
                   from_version=imported_from["source_version_id"],
                   from_host=imported_from["source_host"])
    runs = "".join(json.dumps(r, ensure_ascii=False) + "\n"
                   for r in got["runs"])

    def build(staged: Path) -> None:
        staged.mkdir(parents=True)
        # The marks and the origin first: a version is only ever written
        # into a folder that already is a classifier (_write_version).
        _write_json(staged / "classes.json",
                    dict(marks, version=1, updated=stamp,
                         imported_labels=sorted(labels)))
        _write_about(staged, {"origin": about["origin"],
                              "lineage": about["lineage"] + [event],
                              "tags": about["tags"]})
        meta2, vdir = _write_version(staged, got["model_bytes"], marks, vmeta,
                                     number=int(meta["version"]))
        _write_mirror(staged, vdir, meta2)
        if runs:
            (staged / RUNS).write_text(runs, encoding="utf-8")

    try:
        _stage_into_place(build, store_dir(target))
    except FileExistsError:
        raise
    except OSError as exc:
        raise RuntimeError(f"cannot import into {store_dir(target)}: "
                           f"{_os_error(exc)}")
    return _vid(target, meta)


def _soft_import(text: str, show: Any, current: Any,
                 cleared: str = "") -> Dict[str, Any]:
    """An Import that asks for something (a file, another name) or could
    not import what it was given: the open classifier and its classes
    echoed back, nothing imported. ``cleared`` is the New name to keep."""
    keep, classes = _echo(current)
    return dict(_listed(_library(show)), name=keep, classes=classes,
                version="", imported=[], skipped=[], cleared=cleared,
                summary=text)


def _not_imported(exc: Exception, show: Any, current: Any,
                  typed: str) -> Dict[str, Any]:
    """Why nothing was imported, as an answer (see AN IMPORT NEVER BLANKS
    THE WINDOW in import_classifier)."""
    return _soft_import(f"{_sentence(exc)} Nothing was imported.", show,
                        current, typed)


def _import_file(path: Any) -> Path:
    """The file a picker names, which must be there."""
    p = Path(_import_path(path)).expanduser()
    if not p.is_file():
        raise RuntimeError(f"{p.name or p} is not a file — choose the exported "
                           f"classifier (*{EXPORT_SUFFIX})")
    return p


def import_classifier(path: Any, new_name: Any = "", show: Any = "",
                      current: Any = "") -> Dict[str, Any]:
    """A classifier from an export file — or every classifier in a bundle,
    with ``new_name`` as the prefix for names that are taken (see
    import_bundle) — checked from end to end first (see MOVING A CLASSIFIER
    TO ANOTHER PC).

    It keeps the file's ORIGIN (where it was made, not where it was
    imported) and adds "imported from <file> on <date>" to its lineage. The
    version keeps its number and sha, so "frames v3 (1a2b3c4d)" is the same
    model on both PCs. It is imported under ``new_name`` when one is typed,
    else under its own name. A name that is taken is NEVER overwritten: it
    is asked about — another name, please — with the window's open
    classifier (``current``) handed back as it was; unless that classifier
    already is this one (same model, same origin), which is said and
    changes nothing. No file chosen is asked about the same way.

    AN IMPORT NEVER BLANKS THE WINDOW. Nothing about the file chosen or the
    name typed raises: a file that is not there, one the checks refuse (a
    member reaching outside, not a zip, an altered checksum, ...), a name
    that cannot be one — each is the summary, "imported" is empty, and the
    open classifier comes back as it was. MEASURED before: each of those
    raised, and the generated handler blanked the open model's name and
    classes. "cleared" is what the New name box should hold afterwards: ""
    once the name was used, the name as typed when another is asked for.

    Keys: name, classes, version, imported, skipped, cleared, rows, table, names, filters, summary
    """
    typed = _typed(new_name)
    if not _import_path(path):
        return _soft_import(f"Choose the exported classifier file "
                            f"(*{EXPORT_SUFFIX} or *{BUNDLE_SUFFIX}), then "
                            f"press Import.", show, current, typed)
    try:
        p = _import_file(path)
        if _peek_kind(p) == "bundle":
            return import_bundle(p, new_name, show, current)
        got = _read_export(p)
        bad = _unusable(new_name, "imported")
        if bad:
            return _soft_import(bad, show, current, typed)
        return _import_checked(p, got, new_name, show, current, typed)
    except RuntimeError as exc:
        return _not_imported(exc, show, current, typed)


def _import_checked(p: Path, got: Dict[str, Any], new_name: Any, show: Any,
                    current: Any, typed: str) -> Dict[str, Any]:
    """import_classifier, once the file has passed every check."""
    manifest, meta, doc = got["manifest"], got["meta"], got["classes"]
    wanted = _new_name(new_name)
    if not wanted:
        wanted = str(manifest.get("name") or "")
        if not _NAME_RE.match(wanted) or wanted.lower() in _DEVICE_NAMES:
            return _soft_import(f"The classifier in {p.name} is called "
                                f"{wanted!r}, which cannot be a name here — "
                                f"type a name in New name and press Import "
                                f"again. Nothing was imported.", show, current,
                                typed)
    clash = _taken(wanted)
    if clash and _same_classifier(clash, got):
        lib = _library(show)
        return dict(_listed(lib), name=clash,
                    classes=list(_load(clash)["classes"]),
                    version=_vid(clash, meta), imported=[], skipped=[],
                    cleared="",
                    summary=f"'{clash}' already is this classifier "
                            f"({_vid(clash, meta)}) — nothing was imported "
                            f"or changed.")
    taken = ("{} — type another name in New name and press Import again. "
             "Nothing was imported.")
    if clash:
        return _soft_import(taken.format(_clash_head(clash)), show, current,
                            typed)
    try:
        vid = _import_one(got, wanted, p.name)
    except FileExistsError:
        return _soft_import(taken.format(f"'{wanted}' was taken while "
                                         f"importing"), show, current, typed)
    lib = _library(show)
    same = [i["name"] for i in lib["details"]
            if i["sha256"] == meta["sha256"] and i["name"] != wanted]
    k = len(doc["classes"])
    origin = got["about"]["origin"]
    maker = UNKNOWN_ORIGIN if origin.get("unknown") else _app_label(origin)
    called = str(manifest.get("name") or "")
    return dict(_listed(lib), name=wanted, classes=list(doc["classes"]),
                version=vid, imported=[wanted], skipped=[], cleared="",
                summary=f"Imported {vid} from {p.name} — {got['frames']} "
                        f"frames in {k} class{'' if k == 1 else 'es'}, made "
                        f"by {maker}."
                        + (f" Named as typed in New name (the file calls it "
                           f"'{called}')." if called and called != wanted
                           else "")
                        + (f" {len(got['runs'])} classified run(s) on "
                           f"record." if got["runs"] else "")
                        + (f" It is the same model as '{same[0]}'."
                           if same else ""))


def import_bundle(path: Any, prefix: Any = "", show: Any = "",
                  current: Any = "") -> Dict[str, Any]:
    """Every classifier in a bundle (export_classifiers), each kept with its
    origin and its import added to its lineage. The whole bundle is checked
    before anything is imported.

    Never overwrites. A classifier already here (same name, model and
    origin) is skipped as already here. One whose name is TAKEN by another
    is skipped and named — type a prefix such as "lab2-" as the new name and
    import again to bring those in as "lab2-<name>"; the prefix is used only
    for names that are taken, so importing again is always safe. "name" is
    the first classifier imported — or, when none was, the first already
    here, else the window's open one (``current``): an Import never blanks
    the window, a bundle the checks refuse included (see import_classifier).
    "cleared" keeps the prefix only when nothing came in and the names are
    still taken.

    Keys: name, classes, version, imported, skipped, cleared, rows, table, names, filters, summary
    """
    typed = _typed(prefix)
    if not _import_path(path):
        return _soft_import(f"Choose the bundle (*{BUNDLE_SUFFIX}), then "
                            f"press Import.", show, current, typed)
    try:
        return _import_bundle(_import_file(path), prefix, show, current,
                              typed)
    except RuntimeError as exc:
        return _not_imported(exc, show, current, typed)


def _import_bundle(p: Path, prefix: Any, show: Any, current: Any,
                   typed: str) -> Dict[str, Any]:
    """import_bundle, once its file is known to be there."""
    pre = _name_of(prefix)
    if pre and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,31}$", pre):
        return _soft_import(f"'{pre}' cannot start a classifier name — use "
                            f"letters, digits, '-' or '_' (e.g. lab2-). "
                            f"Nothing was imported.", show, current, typed)
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
    first = (imported or already or [""])[0]
    if first:
        shown, classes = first, list(_load(first)["classes"])
    else:
        shown, classes = _echo(current)
    return dict(_listed(lib), name=shown, classes=classes,
                version="", imported=imported, skipped=clashed,
                cleared=typed if clashed and not (imported or already)
                else "",
                summary=text + ". Nothing was overwritten.")


# ============================================================
# The run record
# ============================================================

def _folder_key(folder: Any) -> str:
    """The folder ASKED ABOUT, resolved once — the one path this PC looks
    up. "" when it cannot be (a name Windows refuses, ...)."""
    try:
        return os.path.normcase(str(Path(str(folder).strip().strip('"'))
                                    .expanduser().resolve()))
    except (OSError, ValueError, RuntimeError):
        return ""


def _record_key(rec: Dict[str, Any]) -> str:
    """A record's folder as the TEXT it was written as — str(resolve()) on
    the PC that classified, so it compares with _folder_key as it is.
    Nothing is looked up: MEASURED before, resolving every record's folder
    took 4 s at 5,000 records on the window's thread, and a record carried
    in by an import could name a UNC path, i.e. a network connection."""
    return os.path.normcase(str(rec.get("folder") or ""))


#: What Delete appends to a name in <store>/.deleted: _<YYYYmmdd_HHMMSS>,
#: then _<k> when that second was taken (delete_classifier).
_DELETED_STAMP = re.compile(r"_\d{8}_\d{6}(?:_\d+)?$")


def _runs_files(name: str) -> List[Tuple[Path, bool, str]]:
    """(runs.jsonl, deleted?, what that classifier is called NOW) for one
    classifier, or for every one — those in <store>/.deleted too: what
    classified a run stays true after the classifier is deleted.

    The name NOW is its folder's: a record keeps the name it was written
    under, and Rename moves the record with the folder (see _version_text).
    A deleted one is called what it was when Delete moved it aside."""
    if name:
        d = store_dir(name)
        return [(d / RUNS, False, d.name)]
    root = classifier_store()
    out: List[Tuple[Path, bool, str]] = []
    if root.is_dir():
        for d in sorted(root.iterdir(), key=lambda e: e.name.lower()):
            if d.is_dir() and _NAME_RE.match(d.name):
                out.append((d / RUNS, False, d.name))
        trash = root / DELETED
        if trash.is_dir():
            for d in sorted(trash.iterdir()):
                if d.is_dir():
                    out.append((d / RUNS, True,
                                _DELETED_STAMP.sub("", d.name)))
    return out


def _read_runs(p: Path) -> Tuple[List[Dict[str, Any]], int]:
    """The records of one runs.jsonl, oldest first, and how many lines were
    not records — cut short by a crash, or with a field of the wrong kind
    (see _run_ok): skipped and counted, never an error that hides the
    others."""
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
        if _run_ok(rec):
            records.append(rec)
        else:
            bad += 1
    return records, bad


def _here(rec: Dict[str, Any]) -> bool:
    """Whether a run was classified on THIS PC. A record from before PCs
    were recorded was made where it is kept: there was no import then (an
    import says which PC its host-less records came from)."""
    host = str(rec.get("host") or "")
    return not host or host.lower() == _host().lower()


def _when(rec: Dict[str, Any]) -> float:
    """When a record was made, in seconds: "at" (to the microsecond) when
    it has one — "when" alone ties within a second, and MEASURED the newest
    of two same-second runs was then reported as the older."""
    at = rec.get("at")
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        return float(at)
    try:
        return time.mktime(time.strptime(str(rec.get("when") or "")[:19],
                                         "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return 0.0


def _version_text(rec: Dict[str, Any]) -> str:
    """The version a record names, called what its classifier is called
    NOW: "hawks v2 (8296caaf; then called birds)" after birds was renamed
    hawks. The record keeps the name it was written under (history is not
    edited); the sha says which model it was. MEASURED before: the line
    said "birds v2" after the rename — a name the dropdown no longer listed,
    and, once a new 'birds' was made, another, untrained model."""
    vid = str(rec.get("version_id") or "?")
    now = str(rec.get("called") or "")
    was = str(rec.get("classifier") or "")
    if not now or not was or now == was:
        return vid
    sha = str(rec.get("sha256") or "")[:8]
    return f"{now} v{rec.get('version', '?')} ({sha}; then called {was})"


def _run_line(rec: Dict[str, Any], with_folder: bool) -> str:
    counts = ", ".join(f"{c} {k}" for c, k in (rec.get("counts") or {}).items())
    runs = list((rec.get("runs") or {}).items())
    which = ", ".join(f"{r} ({k})" for r, k in runs[:3])
    if len(runs) > 3:
        which += f" +{len(runs) - 3} more"
    text = (f"{str(rec.get('when', '?'))[:16]}  {_version_text(rec)}"
            f"{' (deleted)' if rec.get('deleted') else ''}  "
            f"{rec.get('frames', 0)} frames: {counts}")
    if which:
        text += f"  · runs {which}"
    if not _here(rec):
        text += f"  · on {rec.get('host')}"
    if with_folder:
        text += f"  · {rec.get('folder', '?')}"
    return text


def _latest_text(r: Dict[str, Any]) -> str:
    """"Classified with frames v3 (1a2b3c4d) on 2026-10-02 14:03 — good 110,
    bad timing 10": one record as the window's line. One wording for
    run_history's "latest" and classify_folder's own answer."""
    counts = ", ".join(f"{c} {k}" for c, k in (r.get("counts") or {}).items())
    return (f"Classified with {_version_text(r)}"
            f"{' (since deleted)' if r.get('deleted') else ''} on "
            f"{str(r.get('when', '?'))[:16]}"
            + ("" if _here(r) else f" on {r.get('host')}")
            + f" — {counts}")


def run_history(name: Any = "", folder: Any = "") -> Dict[str, Any]:
    """Which classifier version classified which capture runs — newest
    first. By classifier, by folder, or both; with no classifier named,
    every classifier's record is searched (deleted ones included). A folder
    is a path on one PC, so searching by folder finds only this PC's
    records; by classifier alone, records carried in from another PC are
    listed too, each saying which PC.

    Keys: rows, records, latest, summary
    """
    n = _canon(name) if _name_of(name) else ""
    f = str(folder or "").strip().strip('"')
    if not n and not f:
        raise RuntimeError("choose a classifier or a folder to see what was "
                           "classified")
    want = _folder_key(f) if f else ""
    found: List[Tuple[float, int, Dict[str, Any]]] = []
    bad, seq = 0, 0
    for p, deleted, called in _runs_files(n):
        recs, b = _read_runs(p)
        bad += b
        for rec in recs:
            seq += 1            # file order breaks a tie: the later line is newer
            if f and (not want or not _here(rec) or _record_key(rec) != want):
                continue
            seen = dict(rec, called=called)
            if deleted:
                seen["deleted"] = True
            found.append((_when(rec), seq, seen))
    found.sort(key=lambda t: (t[0], t[1]), reverse=True)
    records = [r for _t, _s, r in found]
    rows = [_run_line(r, with_folder=not f) for r in records]
    latest = _latest_text(records[0]) if records else ""
    where = f" for {Path(f).name or f}" if f else ""
    who = f" by '{n}'" if n else ""
    summary = (f"{len(records)} classified run(s){who}{where}."
               if records else f"Nothing classified{who}{where} yet.")
    if bad:
        summary += (f" {bad} line(s) of the record could not be read and were "
                    f"skipped (left as they are).")
    return {"rows": rows, "records": records, "latest": latest,
            "summary": summary}


def _runs_in(folder: Any) -> Dict[str, int]:
    """{capture run: frames} for the pictures in ``folder`` now — what a
    record is compared with (_run_of names each run). Empty when it cannot
    be listed."""
    runs: Dict[str, int] = {}
    try:
        with os.scandir(str(folder).strip().strip('"')) as it:
            for e in it:
                if (e.name.lower().endswith(IMAGE_SUFFIXES)
                        and e.is_file()):
                    run = _run_of(e.name)
                    runs[run] = runs.get(run, 0) + 1
    except (OSError, ValueError):
        return {}
    return runs


def _few_runs(runs: List[Tuple[str, int]], most: int = 2) -> str:
    """"run 20261005_130000 (8 frames)", or "3 runs (24 frames): a, b and
    1 more" — short, for a line two rows high."""
    if len(runs) == 1:
        r, k = runs[0]
        return f"run {r} ({k} frame{'' if k == 1 else 's'})"
    total = sum(k for _r, k in runs)
    names = ", ".join(r for r, _k in runs[:most])
    more = f" and {len(runs) - most} more" if len(runs) > most else ""
    return f"{len(runs)} runs ({total} frames): {names}{more}"


def _runs_note(present: Dict[str, int],
               records: List[Dict[str, Any]]) -> str:
    """What the newest record does NOT cover of the runs in the folder now:
    "not classified yet: run X (8 frames)", and runs an older record covers
    ("run Y with frames v1"). "" when the newest covers them all."""
    if not records or not present:
        return ""
    newest = records[0].get("runs") or {}
    fresh: List[Tuple[str, int]] = []
    older: Dict[str, List[str]] = {}
    for run, k in present.items():
        done = int(newest.get(run) or 0)
        if done >= k:
            continue
        rec = next((r for r in records[1:]
                    if int((r.get("runs") or {}).get(run) or 0) >= k), None)
        if rec is not None and not done:
            older.setdefault(_version_text(rec), []).append(run)
        else:
            fresh.append((run, k - done))
    parts = []
    for version, runs in older.items():
        which = (f"run {runs[0]}" if len(runs) == 1
                 else f"{len(runs)} runs")
        parts.append(f"{which} with {version}")
    if fresh:
        parts.append(f"not classified yet: {_few_runs(sorted(fresh))}")
    return "; ".join(parts)


def classified_with(folder: Any) -> Dict[str, Any]:
    """One line for the window: which version last classified this folder,
    and when — from every classifier's record on this PC — and which of the
    capture runs in it now that record does not cover. A blank folder is
    not an error (this may run as soon as a folder box changes): it just
    says so.

    PER RUN, NOT PER FOLDER. Typhon writes every run into the capture
    folder, and a record lists the runs it classified. MEASURED before: run
    120000 classified, then run 130000 written into the same folder — the
    line still read "Classified with frames v1 ... — good 5, bad timing 3",
    as if the new run had been classified too. Now it adds "· not
    classified yet: run 20261005_130000 (6 frames)" (or the older version
    a run was classified with).

    Keys: classified_with, rows, records, summary
    """
    if not str(folder or "").strip():
        return {"classified_with": "", "rows": [], "records": [],
                "summary": "Choose a folder of frames to see what classified "
                           "it."}
    h = run_history("", folder)
    line = h["latest"] or "Not classified yet — press Classify all frames."
    note = _runs_note(_runs_in(folder), h["records"]) if h["latest"] else ""
    if note:
        line += f" · {note}"
    return {"classified_with": line, "rows": h["rows"],
            "records": h["records"], "summary": h["summary"]}
