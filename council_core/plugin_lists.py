"""
council_core.plugin_lists — which Typhon plugins a project runs, and with
what settings: the app-wide base set and each project's own list.

THE TWO FILES (docs/typhon_plugins_design.md 4.1, 4.2)
    <plugin home>/base_set.json      the app-wide BASE SET: the plugins a new
                                     project starts with, and each plugin's
                                     DEFAULT settings — the level the user
                                     works at
    <project>/typhon_plugins.json    one project's own list: the base set it
                                     was made with, the plugins it ADDED, the
                                     base-set plugins it switched OFF, and the
                                     settings it OVERRIDES

YOUR ANSWERS, AS THESE FILES KEEP THEM (15 October 2026)
  * Question 1 — "a plugin added to the app-wide base set reaches only
    projects created afterwards". So a project keeps a COPY of the base set
    as it was when the project was made (`base_set` in its file), and what a
    project runs is that copy, plus `added`, minus `off`. A later change to
    the base set's list — an addition or a removal — reaches projects made
    after it, never one that already exists.
  * Question 14 — "a setting changed in a plugin window changes only this
    project, with a later 'make this the default for every project'
    button". So a plugin's settings in a project are the base set's
    DEFAULTS with this project's overrides laid over them key by key; saving
    in a plugin's window writes this project's overrides only; and
    `make_default` copies them into the base set, where every project that
    has not overridden those keys follows them. The DEFAULTS are followed,
    not copied: that is what "for every project" means.

THE PLUGIN HOME, RECORDED HOME-RELATIVE (design 4.1)
`~/.council/typhon/` by default, or the folder TYPHON_PLUGIN_HOME names
(tests; the plugin checkout's side Council, docs/typhon_plugins_branch.md).
A project records the plugin home it was made with, written home-relative
("~/Typhon-plugins-app/typhon") when it is inside the home folder, so it
finds the same folder whether it was started from the Council or
double-clicked, and on another account with the same layout. The variable,
when set, wins over the recorded folder. A recorded folder that is missing
is named in a note and the default is used — READ, never created: reading
here never writes anything, and no second base set is ever made silently.

WRITTEN WHOLE, UNDER A LOCK, NEVER OVER A FILE IT CANNOT READ
JSON, never pickle. Every change re-reads the file under an OS lock on a
sidecar (`.<name>.lock`, as camera_presets does), changes it, writes it whole
to a temporary name unique to this writer (process, thread and a counter —
two threads sharing one temporary name lost a block in the design's
research), flushes it to disk and renames it into place. A file that is not
one of these, is damaged, or was written by a newer Typhon is refused and
left exactly as it is: an ordinary change never writes over it. A project's
file is CREATED exclusively, so two windows making it at once cannot both
believe it new.

PLAIN VALUES ONLY
A setting is a name and a plain value — true/false, text, a whole number
that fits 64 bits, a finite number — as a preset's are
(camera_presets._scalar). Nothing here holds code, and a project names its
plugins by id only (design 4.1: never plugin code inside a project).

No Qt here (council_core's rule), and nothing of a plugin's own is read.
"""
from __future__ import annotations

import itertools
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, Iterator, List, Mapping, Optional,
                    Sequence, Tuple)

#: The environment variable that names the plugin home (design 4.1).
HOME_ENV = "TYPHON_PLUGIN_HOME"
#: The plugin home when nothing else names one.
DEFAULT_HOME_TEXT = "~/.council/typhon"
#: Inside the plugin home: third-party plugins, one folder each (Q7).
PLUGINS_DIRNAME = "plugins"
#: Inside the plugin home: approvals (stage P5; named here so the layout is
#: whole).
APPROVALS_FILE = "approvals.json"

BASE_SET_FILE = "base_set.json"
BASE_SET_FORMAT = "typhon-base-set"
PROJECT_FILE = "typhon_plugins.json"
PROJECT_FORMAT = "typhon-plugins"
#: Bump if either file's shape changes. A file of a NEWER version is
#: refused, not rewritten: an older Typhon must not destroy what a newer one
#: saved.
FORMAT_VERSION = 1

#: Far bigger than either file; something else has this name.
MAX_FILE_BYTES = 1024 * 1024
#: One plugin's settings, as JSON.
MAX_SETTINGS_BYTES = 64 * 1024
MAX_KEY = 64
MAX_TEXT_VALUE = 4096
#: The widest whole number a setting holds (camera_presets.WIDEST_INT).
WIDEST_INT = 2 ** 63
#: How long a change waits for another window's change to the same file.
LOCK_SECONDS = 10.0

_LOCK = threading.RLock()
_TEMP_NUMBERS = itertools.count(1)


class PluginListError(Exception):
    """A list file Typhon will not read or change, or a value it will not
    store. `path` names the file when there is one; it is left as it is."""

    def __init__(self, message: str, path: Optional[Path] = None,
                 newer: bool = False):
        super().__init__(message)
        self.path = path
        self.newer = newer


# ======================================================================
# The plugin home
# ======================================================================
@dataclass(frozen=True)
class Home:
    """Which plugin home is in use, and why: `source` is "env" (the
    variable), "project" (the folder the project recorded) or "default";
    `note` says anything the user should hear (a recorded folder missing)."""
    path: Path
    source: str
    note: str = ""


def default_home() -> Path:
    return Path.home() / ".council" / "typhon"


def home_text(path: Any) -> str:
    """`path` as a project records it: "~/..." inside the home folder,
    absolute (forward slashes) otherwise."""
    resolved = Path(path).expanduser().resolve()
    try:
        return "~/" + resolved.relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def expand(text: Any) -> Path:
    """A recorded folder ("~/..." or absolute) as a path on this PC."""
    return Path(str(text)).expanduser()


def env_home() -> Optional[Path]:
    """The folder TYPHON_PLUGIN_HOME names, or None when it is not set."""
    value = os.environ.get(HOME_ENV, "").strip().strip('"')
    return Path(value).expanduser() if value else None


def resolve_home(recorded: Optional[str] = None) -> Home:
    """The plugin home to use: TYPHON_PLUGIN_HOME when set; else the folder
    a project recorded, when it exists; else ~/.council/typhon. A recorded
    folder that is missing is named in the note — and NOT created."""
    from_env = env_home()
    if from_env is not None:
        note = ""
        if recorded and expand(recorded).resolve() != from_env.resolve():
            note = (f"{HOME_ENV} is set, so the plugin home is {from_env}, "
                    f"not {recorded} that this project was made with")
        return Home(from_env, "env", note)
    if recorded:
        folder = expand(recorded)
        if folder.is_dir():
            return Home(folder, "project")
        fallback = default_home()
        return Home(fallback, "default",
                    f"this project was made with the plugin home {recorded}, "
                    f"which is not on this PC — using {fallback} instead "
                    f"(nothing was created)")
    return Home(default_home(), "default")


#: While the plugin Typhon runs BESIDE today's Typhon (branch typhon-plugins,
#: until the switch-over at stage P7), it never writes under ~/.council,
#: today's app folder (docs/typhon_plugins_branch.md): a base set there is
#: refused, and so is a project recording a plugin home there. The build
#: refuses it too (designer_examples.plugin_home_problem). This matters for
#: a double-clicked app whose recorded plugin home is gone: the home falls
#: back to ~/.council/typhon (design 4.1). False at the switch-over.
BESIDE_TODAYS_TYPHON = True


def inside_council(path: Any) -> bool:
    """Whether `path` is ~/.council or inside it."""
    try:
        target = Path(path).expanduser().resolve()
        real = (Path.home() / ".council").resolve()
    except OSError:
        return False
    return target == real or real in target.parents


def side_problem(path: Any, what: str) -> str:
    """Why `what` at `path` may not be written while the plugin Typhon runs
    beside today's (BESIDE_TODAYS_TYPHON), or ""."""
    if not BESIDE_TODAYS_TYPHON or not inside_council(path):
        return ""
    return (f"{what} would be {Path(path).expanduser()}, inside "
            f"{Path.home() / '.council'}, today's Typhon's own folder, where "
            f"the plugin Typhon never writes while it runs beside it — start "
            f"it from run-typhon-plugins.bat, which sets {HOME_ENV} to a "
            f"side plugin home (docs/typhon_plugins_branch.md). Nothing was "
            f"written")


def plugins_folder(home: Any) -> Path:
    """Where third-party plugins live: one folder each, copied in by the
    user and approved once per plugin (question 7)."""
    return Path(home) / PLUGINS_DIRNAME


# ======================================================================
# Values
# ======================================================================
def plain(value: Any) -> bool:
    """Whether `value` is a plain setting value."""
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return len(value) <= MAX_TEXT_VALUE
    if isinstance(value, int):
        return -WIDEST_INT <= value < WIDEST_INT
    return isinstance(value, float) and math.isfinite(value)


def check_settings(values: Any, what: str = "the settings") -> Dict[str, Any]:
    """`values` as a plain {name: value} dict, or PluginListError."""
    if not isinstance(values, Mapping):
        raise PluginListError(f"{what} are not name: value pairs")
    out: Dict[str, Any] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key or len(key) > MAX_KEY:
            raise PluginListError(f"{what}: {key!r} is not a setting name "
                                  f"(text, 1-{MAX_KEY} characters)")
        if not plain(value):
            raise PluginListError(
                f"{what}: {key} = {value!r} is not a plain value (true or "
                f"false, text, a whole number, or a finite number)")
        out[key] = value
    size = len(json.dumps(out, ensure_ascii=False))
    if size > MAX_SETTINGS_BYTES:
        raise PluginListError(f"{what} are {size:,} bytes as JSON — over the "
                              f"{MAX_SETTINGS_BYTES:,} a plugin's settings "
                              f"may be")
    return out


def _ids(value: Any, what: str, path: Path) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v
                                              for v in value):
        raise PluginListError(f"{path.name}: {what} is not a list of plugin "
                              f"ids", path)
    out: List[str] = []
    for item in value:
        if item not in out:
            out.append(item)
    return out


def _settings_block(value: Any, path: Path) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, dict):
        raise PluginListError(f"{path.name}: settings is not plugin id: "
                              f"{{name: value}}", path)
    out: Dict[str, Dict[str, Any]] = {}
    for pid, values in value.items():
        if not isinstance(pid, str) or not pid:
            raise PluginListError(f"{path.name}: {pid!r} in settings is not "
                                  f"a plugin id", path)
        try:
            out[pid] = check_settings(values, f"{path.name}: {pid}'s "
                                              f"settings")
        except PluginListError as exc:
            raise PluginListError(str(exc), path) from None
    return out


# ======================================================================
# Reading
# ======================================================================
def _read_doc(path: Path, fmt: str) -> Optional[Dict[str, Any]]:
    """The document in `path` (None when there is no file), checked for
    `fmt` and its version; PluginListError for anything else."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PluginListError(f"cannot read {path}: {exc}", path) from exc
    if size > MAX_FILE_BYTES:
        raise PluginListError(f"{path} is {size:,} bytes — not a plugin list",
                              path)
    try:
        text = path.read_text(encoding="utf-8-sig")
        doc = json.loads(text, parse_constant=_no_constant)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PluginListError(f"{path} is damaged ({str(exc)[:120]}); it is "
                              f"left as it is", path) from exc
    if not isinstance(doc, dict) or doc.get("format") != fmt:
        raise PluginListError(f"{path} is not a {fmt} file; it is left as it "
                              f"is", path)
    version = doc.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) \
            or version < 1:
        raise PluginListError(f"{path} has no format_version; it is left as "
                              f"it is", path)
    if version > FORMAT_VERSION:
        raise PluginListError(f"{path} was written by a newer Typhon "
                              f"(format_version {version}); it is left as "
                              f"it is", path, newer=True)
    return doc


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a number JSON allows")


@dataclass
class BaseSet:
    """The app-wide base set: the plugins every new project starts with,
    and each plugin's default settings. `written` is False for the base set
    a plugin home has before anything was changed (all built-ins, no
    defaults), which is never written just by reading it."""
    plugins: List[str]
    settings: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    path: Optional[Path] = None
    written: bool = False

    def as_doc(self) -> Dict[str, Any]:
        return {"format": BASE_SET_FORMAT, "format_version": FORMAT_VERSION,
                "plugins": list(self.plugins),
                "settings": {k: dict(v) for k, v in self.settings.items()}}


def read_base_set(home: Any, default_plugins: Sequence[str]) -> BaseSet:
    """The base set in `home`, or — with no file there — every plugin of
    `default_plugins` (the built-ins) and no default settings. Reading never
    writes."""
    path = Path(home) / BASE_SET_FILE
    doc = _read_doc(path, BASE_SET_FORMAT)
    if doc is None:
        return BaseSet(list(default_plugins), {}, path, written=False)
    return BaseSet(_ids(doc.get("plugins", []), "plugins", path),
                   _settings_block(doc.get("settings", {}), path), path,
                   written=True)


@dataclass
class ProjectList:
    """One project's own list (typhon_plugins.json)."""
    path: Path
    plugin_home: str
    base_set: List[str]
    added: List[str] = field(default_factory=list)
    off: List[str] = field(default_factory=list)
    settings: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    made: str = ""

    def runs(self) -> List[str]:
        """What the project runs: the base set it was made with, plus what
        it added, minus what it switched off — in that order."""
        out = [p for p in self.base_set if p not in self.off]
        out += [p for p in self.added if p not in out and p not in self.off]
        return out

    def as_doc(self) -> Dict[str, Any]:
        return {"format": PROJECT_FORMAT, "format_version": FORMAT_VERSION,
                "plugin_home": self.plugin_home, "made": self.made,
                "base_set": list(self.base_set), "added": list(self.added),
                "off": list(self.off),
                "settings": {k: dict(v) for k, v in self.settings.items()}}


def project_path(project_dir: Any) -> Path:
    return Path(project_dir) / PROJECT_FILE


def read_project(project_dir: Any) -> Optional[ProjectList]:
    """The project's list, or None when it has none."""
    path = project_path(project_dir)
    doc = _read_doc(path, PROJECT_FORMAT)
    if doc is None:
        return None
    home = doc.get("plugin_home", "")
    made = doc.get("made", "")
    if not isinstance(home, str) or not isinstance(made, str):
        raise PluginListError(f"{path.name}: plugin_home and made are text",
                              path)
    return ProjectList(path=path, plugin_home=home,
                       base_set=_ids(doc.get("base_set", []), "base_set",
                                     path),
                       added=_ids(doc.get("added", []), "added", path),
                       off=_ids(doc.get("off", []), "off", path),
                       settings=_settings_block(doc.get("settings", {}),
                                                path),
                       made=made)


def effective_settings(base: BaseSet, project: Optional[ProjectList],
                       pid: str) -> Dict[str, Any]:
    """A plugin's settings in a project: the base set's defaults with the
    project's overrides laid over them key by key (Q14)."""
    out = dict(base.settings.get(pid, {}))
    if project is not None:
        out.update(project.settings.get(pid, {}))
    return out


# ======================================================================
# Writing
# ======================================================================
def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _try_lock(handle: Any) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _locked(path: Path, timeout: float = LOCK_SECONDS) -> Iterator[None]:
    """One read-change-write of `path` at a time: this process's threads and
    every other window (an OS lock on a sidecar; the OS drops it if the
    process dies, and the empty sidecar stays)."""
    with _LOCK:
        lock = _lock_path(path)
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock, "a+b")
        except OSError as exc:
            raise PluginListError(f"cannot save {path}: {exc}", path) from exc
        try:
            deadline = time.monotonic() + timeout
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    raise PluginListError(
                        f"another window is changing {path} and has not "
                        f"finished — try again", path)
                time.sleep(0.01)
            try:
                yield
            finally:
                _unlock(handle)
        finally:
            handle.close()


def _temp_name(path: Path) -> Path:
    """A temporary name no other writer uses: process, thread, counter."""
    return path.with_name(f".{path.name}.{os.getpid()}."
                          f"{threading.get_ident()}.{next(_TEMP_NUMBERS)}.tmp")


def _dump(doc: Mapping[str, Any]) -> str:
    return json.dumps(dict(doc), indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n"


def _write_whole(path: Path, doc: Mapping[str, Any]) -> None:
    """Whole, flushed, then renamed over `path` (only ever over a file this
    module read and checked a moment before, under the lock)."""
    text = _dump(doc)
    temp = _temp_name(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(temp, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                # Windows: a scanner or an editor holding the old file for
                # a moment. Brief retries, then the honest error.
                if attempt == 4:
                    raise
                time.sleep(0.05)
    except OSError as exc:
        _drop(temp)                  # ours: the half-step that did not happen
        raise PluginListError(f"cannot save {path}: {exc}", path) from exc


def _create_once(path: Path, doc: Mapping[str, Any]) -> None:
    """Write `doc` at `path` whole, refusing an existing file: a temporary
    name of this writer's own, then a rename that refuses an existing name
    (Windows' rename does; elsewhere a hard link, as camera_record does)."""
    if path.exists():
        raise PluginListError(f"{path} is already there; it is never "
                              f"replaced", path)
    text = _dump(doc)
    temp = _temp_name(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(temp, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            os.rename(temp, path)
        else:
            os.link(temp, path)
            os.remove(temp)
    except FileExistsError as exc:
        _drop(temp)
        raise PluginListError(f"{path} is already there; it is never "
                              f"replaced", path) from exc
    except OSError as exc:
        _drop(temp)
        raise PluginListError(f"cannot save {path}: {exc}", path) from exc


def _drop(temp: Path) -> None:
    """Remove this writer's own temporary file, if it is still there."""
    try:
        os.remove(temp)
    except OSError:
        pass


def change_base_set(home: Any, default_plugins: Sequence[str],
                    change: Callable[[BaseSet], None]) -> BaseSet:
    """Re-read the base set under the lock, let `change` alter it, write it
    whole. A damaged or newer file is refused and left as it is, and so is
    a base set under ~/.council beside today's Typhon (side_problem) —
    before anything, its lock file included, is made there."""
    path = Path(home) / BASE_SET_FILE
    problem = side_problem(path, "the app-wide base set")
    if problem:
        raise PluginListError(problem, path)
    with _locked(path):
        base = read_base_set(home, default_plugins)
        change(base)
        base.plugins = list(dict.fromkeys(base.plugins))
        base.settings = {k: check_settings(v, f"{k}'s default settings")
                         for k, v in base.settings.items() if v}
        _write_whole(path, base.as_doc())
        base.written = True
        return base


def make_project(project_dir: Any, base: BaseSet, home: Any) -> ProjectList:
    """Create a project's list: a COPY of the base set as it is now (Q1), no
    additions, nothing off, no overrides, and the plugin home it was made
    with, home-relative. Created exclusively: an existing file is never
    replaced (PluginListError). Beside today's Typhon, a plugin home under
    ~/.council is refused (side_problem), as the build refuses it."""
    path = project_path(project_dir)
    problem = side_problem(home, "this project's plugin home")
    if problem:
        raise PluginListError(problem, path)
    made = ProjectList(path=path, plugin_home=home_text(home),
                       base_set=list(base.plugins),
                       made=time.strftime("%Y-%m-%d %H:%M:%S"))
    _create_once(path, made.as_doc())
    return made


def change_project(project_dir: Any,
                   change: Callable[[ProjectList], None]) -> ProjectList:
    """Re-read the project's list under the lock, let `change` alter it,
    write it whole. A project with no list is refused (it is made once,
    by make_project)."""
    path = project_path(project_dir)
    with _locked(path):
        project = read_project(project_dir)
        if project is None:
            raise PluginListError(f"{path} does not exist — the project's "
                                  f"plugin list is made when the project is",
                                  path)
        change(project)
        project.added = list(dict.fromkeys(project.added))
        project.off = list(dict.fromkeys(project.off))
        project.settings = {k: check_settings(v, f"{k}'s settings")
                            for k, v in project.settings.items() if v}
        _write_whole(path, project.as_doc())
        return project


# -- the changes the Plugins window and the plugin windows make ----------
def switch_on(project_dir: Any, pid: str) -> ProjectList:
    """Turn `pid` on in this project: back on if it was switched off, else
    added."""
    def change(p: ProjectList) -> None:
        if pid in p.off:
            p.off.remove(pid)
        if pid not in p.base_set and pid not in p.added:
            p.added.append(pid)
    return change_project(project_dir, change)


def switch_off(project_dir: Any, pid: str) -> ProjectList:
    """Turn `pid` off in this project: an added plugin is taken off the
    list; a base-set plugin is listed as switched off. Its settings are
    kept, so switching it on again finds them."""
    def change(p: ProjectList) -> None:
        if pid in p.added:
            p.added.remove(pid)
        if pid in p.base_set and pid not in p.off:
            p.off.append(pid)
    return change_project(project_dir, change)


def save_project_settings(project_dir: Any, pid: str,
                          values: Mapping[str, Any]) -> ProjectList:
    """Q14: what a plugin's window saves — THIS project's overrides of
    `pid`'s settings, key by key. Other plugins' and other keys are kept."""
    clean = check_settings(values, f"{pid}'s settings")

    def change(p: ProjectList) -> None:
        mine = dict(p.settings.get(pid, {}))
        mine.update(clean)
        p.settings[pid] = mine
    return change_project(project_dir, change)


def forget_project_settings(project_dir: Any, pid: str,
                            keys: Optional[Sequence[str]] = None
                            ) -> ProjectList:
    """Drop this project's overrides of `keys` (all of `pid`'s when None):
    the project follows the base set's defaults for them again."""
    def change(p: ProjectList) -> None:
        mine = dict(p.settings.get(pid, {}))
        for key in (list(mine) if keys is None else keys):
            mine.pop(key, None)
        p.settings[pid] = mine
    return change_project(project_dir, change)


def set_default_settings(home: Any, default_plugins: Sequence[str], pid: str,
                         values: Mapping[str, Any]) -> BaseSet:
    """Change `pid`'s DEFAULT settings in the base set, key by key: every
    project that has not overridden those keys follows them."""
    clean = check_settings(values, f"{pid}'s default settings")

    def change(b: BaseSet) -> None:
        mine = dict(b.settings.get(pid, {}))
        mine.update(clean)
        b.settings[pid] = mine
    return change_base_set(home, default_plugins, change)


def make_default(project_dir: Any, home: Any, default_plugins: Sequence[str],
                 pid: str, keys: Optional[Sequence[str]] = None
                 ) -> Tuple[BaseSet, ProjectList]:
    """Q14's "make this the default for every project": this project's
    overrides of `keys` (all of `pid`'s when None) become the base set's
    defaults, and are then dropped from this project, which now follows the
    defaults — the same values — like every other project that has not
    overridden them."""
    project = read_project(project_dir)
    if project is None:
        raise PluginListError(f"{project_path(project_dir)} does not exist",
                              project_path(project_dir))
    mine = project.settings.get(pid, {})
    chosen = {k: v for k, v in mine.items() if keys is None or k in keys}
    if not chosen:
        raise PluginListError(f"this project has no settings of {pid} of its "
                              f"own to make the default")
    base = set_default_settings(home, default_plugins, pid, chosen)
    after = forget_project_settings(project_dir, pid, list(chosen))
    return base, after


def set_base_plugins(home: Any, default_plugins: Sequence[str],
                     plugins: Sequence[str]) -> BaseSet:
    """Change the base set's LIST. By Q1 it reaches projects made after
    this, not the ones that exist."""
    wanted = [str(p) for p in plugins]

    def change(b: BaseSet) -> None:
        b.plugins = wanted
    return change_base_set(home, default_plugins, change)
