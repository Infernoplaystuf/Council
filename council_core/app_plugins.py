"""
council_core.app_plugins — Council's own plugins: found by their manifests,
OFF until the user switches them on, loaded only then, and kept from taking
the app down when they fail.

ONE FORMAT WITH TYPHON
A Council plugin is a folder holding `plugin.json`, in the same schema-1
format Typhon's plugins use, read by the same reader
(council_core.plugin_manifest: its `load` refuses duplicate keys, NaN, a file
over 64 KB, text that is not UTF-8). That module is Typhon's own (it came
with typhon-plugins); nothing here changes it. What differs is what a
manifest may SAY, because Council's plugins add to an app, not to a camera
rig, and that check is this module's (`check`):

    kind      "tab" — the plugin adds one tab (the only kind so far)
    id        reverse-domain; "council." is kept for the plugins that ship
              with Council
    network   "none" only: Council is offline by design
    needs     services the plugin asks Council for (council_core.
              plugin_services: "sources", "models"); a plugin naming one
              Council does not offer is listed with that reason

WHERE PLUGINS ARE (`discover`)
  * Built in: the folders of `council_plugins/` that BUILTIN_PLUGINS names.
    A folder there that is not on the list is refused, never loaded.
  * Added by the user: folders under `<app dir>/plugins/`. These are FOUND
    and listed, but not run: there is no approval step yet, so they can't
    be switched on (`Found.can_turn_on`).
Listing reads manifests only. No plugin code is imported to list it.

OFF BY DEFAULT (`enabled_ids`, `set_enabled`)
Every plugin starts off. What the user switched on is kept in
`<app dir>/plugins.json` as {"enabled": [ids]}; a change takes effect the
next time Council starts.

LOADING AND THE CONTEXT (`load_entry`, `PluginContext`)
A switched-on plugin's entry ("module:object", inside its own folder) is
imported from its folder as a package of its own, without touching
sys.path, and called with a PluginContext: the plugin's own data folder
(`<app dir>/plugin_data/<id>/`, made on first use), atomic JSON save and
load inside it, the window, and the services its manifest names in `needs`
(`service`, made on first use). A plugin reaches nothing else of Council
through it.

FAULTS (`record_fault`)
A plugin that fails to load or build is written to `<app dir>/
plugin_errors.log` with its traceback; the Qt side shows the reason on the
plugin's tab and the rest of Council carries on (council_qt.plugin_tabs).

No Qt here.
"""
from __future__ import annotations

import datetime as _dt
import importlib
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from council_core import paths
from council_core import plugin_manifest as pm

#: The plugin API this Council offers. Before 1.0 a plugin names exactly
#: this version (the same rule as Typhon's, plugin_manifest.api_problem).
API_VERSION = "0.1"

#: What a Council plugin may be.
KINDS = ("tab",)
#: Ids kept for the plugins that ship with Council.
BUILTIN_PREFIX = "council."
#: The only network a Council plugin may say it uses.
NETWORK = ("none",)
#: Services a plugin may ask for in `needs` (council_core.plugin_services:
#: "sources" = the user's documents as Connections reads them, "models" =
#: the US-origin local models). Named here so listing imports nothing.
SERVICES: frozenset = frozenset({"sources", "models"})

#: The fields a Council manifest may hold.
REQUIRED = ("schema", "id", "short", "name", "version", "api", "kind",
            "entry")
THIRD_PARTY_REQUIRED = ("author", "description", "licence")
OPTIONAL = ("needs", "python", "network", "author", "description",
            "licence")
FIELDS = frozenset(REQUIRED) | frozenset(OPTIONAL)

#: The built-in plugins: folder names under council_plugins/. A plugin is
#: built in because it is on this list, never because of where it sits.
BUILTIN_PLUGINS: Sequence[str] = ("finance", "study")

#: Where the built-in plugins ship (beside council_core in the source tree
#: and in the bundle).
BUILTIN_DIR = Path(__file__).resolve().parent.parent / "council_plugins"

SETTINGS_FILE = "plugins.json"
USER_DIR = "plugins"
DATA_DIR = "plugin_data"
ERROR_LOG = "plugin_errors.log"

#: Why a plugin from outside Council is listed but not run.
NOT_YET_APPROVED = ("plugins from outside Council are listed but not run "
                    "yet: there is no approval step for them")

_lock = threading.Lock()


# ======================================================================
# The manifest
# ======================================================================
def api_problem(wanted: str, have: str = API_VERSION) -> str:
    """Why a plugin needing API `wanted` cannot run on this Council, or ""."""
    match_w, match_h = pm.API_RE.match(str(wanted)), pm.API_RE.match(have)
    if not match_w or not match_h:
        return f"{wanted!r} is not an API version like \"1.0\""
    w_major, w_minor = int(match_w.group(1)), int(match_w.group(2))
    h_major, h_minor = int(match_h.group(1)), int(match_h.group(2))
    if w_major != h_major or (h_major == 0 and w_minor != h_minor) \
            or w_minor > h_minor:
        return (f"it was written for Council plugin API {wanted}, and this "
                f"Council has plugin API {have}")
    return ""


def check(doc: Mapping[str, Any], *, builtin: bool,
          folder: Optional[Path] = None) -> pm.Manifest:
    """`doc` checked as a Council plugin's manifest: a Manifest, or
    ManifestError naming the field at fault and why."""
    if not isinstance(doc, Mapping):
        raise pm.ManifestError("a manifest is one JSON object")
    unknown = sorted(set(doc) - FIELDS)
    if unknown:
        raise pm.ManifestError(
            f"it has field{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(u) for u in unknown)}, which a Council plugin "
            f"does not have", unknown[0])
    for name in REQUIRED + (() if builtin else THIRD_PARTY_REQUIRED):
        if name not in doc:
            raise pm.ManifestError(f"it has no {name!r}", name)
    schema = doc["schema"]
    if isinstance(schema, bool) or schema != pm.SCHEMA:
        raise pm.ManifestError(f"its schema is {schema!r}; Council reads "
                               f"schema {pm.SCHEMA}", "schema")
    ident = pm._text(doc, "id")
    if not pm.ID_RE.match(ident):
        raise pm.ManifestError(f"its id {ident!r} is not reverse-domain lower "
                               f"case with dots, like \"acme.budget\"", "id")
    if builtin and not ident.startswith(BUILTIN_PREFIX):
        raise pm.ManifestError(f"a built-in plugin's id starts "
                               f"{BUILTIN_PREFIX!r}; this one is {ident!r}",
                               "id")
    if not builtin and ident.startswith(BUILTIN_PREFIX):
        raise pm.ManifestError(f"ids starting {BUILTIN_PREFIX!r} are kept for "
                               f"the plugins that ship with Council", "id")
    short = pm._text(doc, "short")
    if not pm.SHORT_RE.match(short):
        raise pm.ManifestError(f"its short name {short!r} is not 2-12 "
                               f"lower-case letters or digits", "short")
    name = pm._text(doc, "name", limit=pm.MAX_TEXT).strip()
    if not name:
        raise pm.ManifestError("its name is empty", "name")
    version = pm._text(doc, "version")
    if not pm.VERSION_RE.match(version):
        raise pm.ManifestError(f"its version {version!r} is not "
                               f"major.minor.patch", "version")
    api = pm._text(doc, "api")
    if not pm.API_RE.match(api):
        raise pm.ManifestError(f"its api {api!r} is not a plugin API version "
                               f"like \"1.0\"", "api")
    kind = pm._text(doc, "kind")
    if kind not in KINDS:
        raise pm.ManifestError(f"its kind {kind!r} is not one Council has "
                               f"({', '.join(KINDS)})", "kind")
    entry = pm._text(doc, "entry")
    if not pm.ENTRY_RE.match(entry):
        raise pm.ManifestError(f"its entry {entry!r} is not module:object "
                               f"inside the plugin", "entry")
    needs = pm._strings(doc, "needs")
    for need in needs:
        if not pm.CAPABILITY_RE.match(need):
            raise pm.ManifestError(f"{need!r} in needs is not a service name",
                                   "needs")
    python = pm._strings(doc, "python")
    for requirement in python:
        if not pm.REQUIREMENT_RE.match(requirement):
            raise pm.ManifestError(f"{requirement!r} in python is not an "
                                   f"import name with optional version "
                                   f"bounds, like \"numpy>=1.24\"", "python")
    network = pm._text(doc, "network") if "network" in doc else "none"
    if network not in NETWORK:
        raise pm.ManifestError(f"its network is {network!r}; Council is "
                               f"offline, so a plugin's network can only be "
                               f"\"none\"", "network")
    texts = {k: (pm._text(doc, k, limit=pm.MAX_DESCRIPTION
                          if k == "description" else pm.MAX_TEXT)
                 if k in doc else "")
             for k in THIRD_PARTY_REQUIRED}
    if not builtin:
        for key, value in texts.items():
            if not value.strip():
                raise pm.ManifestError(f"its {key} is empty", key)
    return pm.Manifest(
        id=ident, short=short, name=name, version=version, api=api,
        kind=kind, entry=entry, needs=tuple(needs), python=tuple(python),
        network=network, folder=folder, builtin=builtin, **texts)


def python_problem(requirements: Sequence[str]) -> str:
    """Why this Python cannot run a plugin needing `requirements`, or ""."""
    for text in requirements:
        name, specs = pm.requirement_parts(text)
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            return f"it needs the Python package {name!r}, which isn't installed"
        if specs:
            try:
                have = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                continue            # installed under another name: not checked
            if not pm.version_ok(have, specs):
                return f"it needs {text}, and this Python has {name} {have}"
    return ""


# ======================================================================
# Finding plugins
# ======================================================================
@dataclass
class Found:
    """One plugin folder as listed: its manifest if it could be read, and why
    it cannot be switched on ("" when it can)."""
    folder: Path
    builtin: bool
    manifest: Optional[pm.Manifest] = None
    problem: str = ""

    @property
    def id(self) -> str:
        return self.manifest.id if self.manifest else self.folder.name

    @property
    def name(self) -> str:
        return self.manifest.name if self.manifest else self.folder.name

    @property
    def can_turn_on(self) -> bool:
        return self.manifest is not None and not self.problem


def folder_name(ident: str, builtin: bool) -> str:
    """The folder a plugin with this id lives in."""
    if builtin and ident.startswith(BUILTIN_PREFIX):
        ident = ident[len(BUILTIN_PREFIX):]
    return ident.replace(".", "_")


def user_dir() -> Path:
    return paths.app_dir() / USER_DIR


def _read_one(folder: Path, builtin: bool) -> Found:
    found = Found(folder=folder, builtin=builtin)
    try:
        found.manifest = check(pm.load(folder / pm.MANIFEST_FILE),
                               builtin=builtin, folder=folder)
    except pm.ManifestError as exc:
        found.problem = str(exc)
        return found
    manifest = found.manifest
    wanted = folder_name(manifest.id, builtin)
    if folder.name != wanted:
        found.problem = (f"its folder is {folder.name!r}; a plugin with id "
                         f"{manifest.id!r} lives in {wanted!r}")
    elif not builtin:
        found.problem = NOT_YET_APPROVED
    else:
        found.problem = (api_problem(manifest.api)
                         or (f"it needs {', '.join(manifest.needs)}, which "
                             f"this Council does not offer yet"
                             if set(manifest.needs) - SERVICES else "")
                         or python_problem(manifest.python))
    return found


def _folders(root: Path) -> List[Path]:
    try:
        return sorted((p for p in root.iterdir() if p.is_dir()
                       and not p.name.startswith((".", "_"))),
                      key=lambda p: p.name.lower())
    except OSError:
        return []


def discover(*, builtin_dir: Optional[Path] = None,
             builtin_names: Optional[Sequence[str]] = None,
             extra_dir: Optional[Path] = None) -> List[Found]:
    """Every plugin folder, built-in first, each with its manifest read and
    checked. Reads manifests only; imports nothing of a plugin. Never makes a
    folder."""
    root = Path(builtin_dir) if builtin_dir is not None else BUILTIN_DIR
    names = tuple(BUILTIN_PLUGINS if builtin_names is None else builtin_names)
    out: List[Found] = []
    for folder in _folders(root):
        if folder.name in names:
            out.append(_read_one(folder, builtin=True))
        elif (folder / pm.MANIFEST_FILE).exists():
            out.append(Found(folder=folder, builtin=True,
                             problem="it sits among Council's built-in "
                                     "plugins but is not on their list, so "
                                     "it is never loaded"))
    third = Path(extra_dir) if extra_dir is not None else user_dir()
    for folder in _folders(third):
        out.append(_read_one(folder, builtin=False))
    seen: Dict[str, Found] = {}
    for found in out:
        if found.manifest is None:
            continue
        first = seen.get(found.manifest.id)
        if first is not None:
            found.problem = (f"another plugin already has the id "
                             f"{found.manifest.id!r} ({first.folder})")
        else:
            seen[found.manifest.id] = found
    return out


# ======================================================================
# On and off
# ======================================================================
def settings_path() -> Path:
    return paths.app_dir() / SETTINGS_FILE


def enabled_ids() -> List[str]:
    """The ids the user switched on. A missing or broken file means none:
    every plugin starts off."""
    try:
        doc = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ids = doc.get("enabled", []) if isinstance(doc, dict) else []
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) \
        else []


def set_enabled(ident: str, on: bool) -> List[str]:
    """Switch one plugin on or off for the next start; the new list."""
    with _lock:
        ids = [i for i in enabled_ids() if i != ident]
        if on:
            ids.append(ident)
        path = settings_path()
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                doc = {}
        except (OSError, ValueError):
            doc = {}
        doc["enabled"] = sorted(ids)
        write_json_atomic(path, doc)
        return doc["enabled"]


def switched_on(found: Sequence[Found]) -> List[Found]:
    """The plugins to load at start-up: switched on and able to run."""
    ids = set(enabled_ids())
    return [f for f in found if f.can_turn_on and f.manifest.id in ids]


# ======================================================================
# Files
# ======================================================================
def write_json_atomic(path: Path, data: Any) -> None:
    """`data` as JSON at `path`: the old file or the new one, never half."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows refuses the swap while another process holds the file.
            if attempt == 4:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.05 * (attempt + 1))


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,120}$")


@dataclass
class PluginContext:
    """What Council hands a plugin: its own data folder, atomic JSON in it,
    the window, and the services its manifest asked for. Nothing else of
    Council is reachable through it."""
    manifest: pm.Manifest
    window: Any = None
    root: Optional[Path] = None
    _logged: List[str] = field(default_factory=list, repr=False)
    #: name -> the service, for tests (Council makes them on first use)
    services: Dict[str, Any] = field(default_factory=dict, repr=False)

    def service(self, name: str) -> Any:
        """The service ``name`` (council_core.plugin_services), made the
        first time it is asked for. Only a service the manifest's `needs`
        names: anything else is PermissionError."""
        if name not in self.manifest.needs:
            raise PermissionError(f"{self.manifest.id} did not ask for the "
                                  f"{name!r} service in its manifest")
        if name not in self.services:
            from council_core import plugin_services
            self.services[name] = plugin_services.make(name)
        return self.services[name]

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def name(self) -> str:
        return self.manifest.name

    def data_folder(self) -> Path:
        """`<app dir>/plugin_data/<id>/`, made on first use."""
        base = self.root if self.root is not None else paths.app_dir()
        folder = Path(base) / DATA_DIR / self.manifest.id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _file(self, name: str) -> Path:
        if not _SAFE_NAME.match(name) or ".." in name:
            raise ValueError(f"{name!r} is not a plain file name")
        return self.data_folder() / name

    def save_json(self, name: str, data: Any) -> Path:
        path = self._file(name)
        write_json_atomic(path, data)
        return path

    def load_json(self, name: str, default: Any = None) -> Any:
        try:
            return json.loads(self._file(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def log(self, text: str) -> None:
        line = f"[plugin {self.manifest.id}] {text}"
        self._logged.append(line)
        print(line, flush=True)


# ======================================================================
# Loading
# ======================================================================
def _package_name(manifest: pm.Manifest) -> str:
    return "council_plugin_" + re.sub(r"\W", "_", manifest.id)


def load_entry(manifest: pm.Manifest) -> Callable[..., Any]:
    """The plugin's entry object, imported from its own folder as a package
    of its own (no sys.path change). Raises whatever the import raises."""
    if manifest.folder is None:
        raise ValueError("this manifest was not read from a folder")
    folder = Path(manifest.folder)
    package = _package_name(manifest)
    if package not in sys.modules:
        init = folder / "__init__.py"
        if init.exists():
            spec = importlib.util.spec_from_file_location(
                package, init, submodule_search_locations=[str(folder)])
        else:
            spec = importlib.machinery.ModuleSpec(package, None,
                                                  is_package=True)
            spec.submodule_search_locations = [str(folder)]
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        try:
            if spec.loader is not None:
                spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(package, None)
            raise
    module = importlib.import_module(f"{package}.{manifest.module}")
    entry = getattr(module, manifest.attribute)
    if not callable(entry):
        raise TypeError(f"{manifest.entry} is not something Council can call")
    return entry


def record_fault(manifest_or_id: Any, exc: BaseException,
                 where: str = "") -> str:
    """Write a plugin's failure to plugin_errors.log; the one-line reason."""
    ident = getattr(manifest_or_id, "id", str(manifest_or_id))
    reason = f"{type(exc).__name__}: {exc}"[:300]
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    text = "".join(traceback.format_exception(type(exc), exc,
                                              exc.__traceback__))
    try:
        log = paths.app_dir() / ERROR_LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"--- {stamp} {ident} {where}\n{text}\n")
    except OSError:
        pass
    print(f"[plugins] {ident} failed{(' ' + where) if where else ''}: "
          f"{reason}", flush=True)
    return reason
