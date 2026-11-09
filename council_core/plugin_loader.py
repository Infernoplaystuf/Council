"""
council_core.plugin_loader — how Typhon loads a plugin written by someone
else (docs/typhon_plugins_design.md 4.5, 4.6; stage P5 row 1).

THE BYTES THAT WERE HASHED ARE THE BYTES THAT RUN
At start-up (discovery) every file of a third-party plugin's folder is read
and hashed, and its .py files' bytes are HELD in memory from then on
(`read_folder` -> HeldPlugin). Importing it later compiles exactly those
held bytes through an importer of its own (`HeldImporter`, one per plugin
package) and never reads the disk again, never reads or writes bytecode:
a folder changed or replaced during a session takes effect at the next
start, and the sha256 a run records is of the code that ran.

THE PACKAGE ROOT
A third-party plugin is imported as `typhon_thirdparty.<name>`, where
<name> is its id with the dots turned into underscores ("acme.thermal" ->
typhon_thirdparty.acme_thermal), whatever its folder is called. Files
inside it import each other with relative imports. sys.path is never
touched.

THE RISKY-PATTERN SCAN
`scan(held)` lists the lines of its .py files that do things a user should
look at before approving (council_core.ide_trust's patterns and a few of a
plugin's own). It is a speed bump for the eye, never a fence: an approved
plugin is ordinary Python with Typhon's rights (design 4.6).

THE NETWORK TRIPWIRE
A plugin's manifest says "none" (the default), "camera-link" (a camera on
a local network) or "internet" (refused unless the user opted that plugin
in). `install_tripwire()` adds one audit hook for the process that, on a
socket connect or send, looks at the call stack for a third-party plugin's
code and refuses (PermissionError) a connection its manifest does not
allow. It is a tripwire for MISTAKES only: a camera SDK's native traffic
never reaches it, and determined code can get round it.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import ipaddress
import os
import re
import sys
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: The package every third-party plugin is imported under.
PACKAGE_ROOT = "typhon_thirdparty"
#: Most bytes one file of a plugin may hold (a model file, a table); the
#: folder as a whole is held to MAX_FILES files by discovery.
MAX_FILE_BYTES = 256 * 1024 * 1024


class LoaderError(Exception):
    """A plugin's folder could not be read, or its code imported."""


def package_name(pid: str) -> str:
    """The package plugin `pid` is imported as."""
    return f"{PACKAGE_ROOT}.{pid.replace('.', '_')}"


# ======================================================================
# Reading a folder: every file hashed, the code held
# ======================================================================
@dataclass(frozen=True)
class HeldFile:
    rel: str          # forward slashes, relative to the folder
    size: int
    sha256: str


@dataclass
class HeldPlugin:
    """A third-party plugin's folder as read at start-up: every file's
    size and sha256, and the .py files' bytes, held."""
    folder: Path
    package: str
    files: Dict[str, HeldFile] = field(default_factory=dict)
    sources: Dict[str, bytes] = field(default_factory=dict)

    def hashes(self) -> Dict[str, str]:
        """{relative name: sha256}, in name order."""
        return {rel: f.sha256 for rel, f in sorted(self.files.items())}

    def digest(self) -> str:
        """One sha256 over every file's name and sha256: what a run
        records as the code that made it."""
        h = hashlib.sha256()
        for rel, sha in self.hashes().items():
            h.update(rel.encode("utf-8") + b"\0" + sha.encode("ascii")
                     + b"\n")
        return h.hexdigest()

    def total_bytes(self) -> int:
        return sum(f.size for f in self.files.values())


def read_folder(folder: Any, pid: str) -> HeldPlugin:
    """Read and hash every file under `folder`; hold its .py files' bytes.
    LoaderError when a file cannot be read (said by its name)."""
    folder = Path(folder)
    held = HeldPlugin(folder=folder, package=package_name(pid))
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            path = Path(root, name)
            rel = path.relative_to(folder).as_posix()
            try:
                size = path.stat().st_size
                if size > MAX_FILE_BYTES:
                    raise LoaderError(f"{rel} is {size:,} bytes; a plugin's "
                                      f"file holds at most "
                                      f"{MAX_FILE_BYTES:,}")
                data = path.read_bytes()
            except OSError as exc:
                raise LoaderError(f"cannot read {rel}: {exc}") from exc
            held.files[rel] = HeldFile(rel, len(data),
                                       hashlib.sha256(data).hexdigest())
            if rel.endswith(".py"):
                held.sources[rel] = data
    return held


# ======================================================================
# The importer: one per plugin package, serving only the held bytes
# ======================================================================
class HeldImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Finds and loads `held.package` and its sub-modules from the bytes
    held at start-up — never from disk, never through bytecode."""

    def __init__(self, held: HeldPlugin) -> None:
        self.held = held

    # -- finding ---------------------------------------------------------
    def _source_of(self, fullname: str) -> Optional[Tuple[str, bool]]:
        """(relative file, is a package) for module `fullname`, or None."""
        package = self.held.package
        if fullname == package:
            return ("__init__.py", True)
        if not fullname.startswith(package + "."):
            return None
        rel = fullname[len(package) + 1:].replace(".", "/")
        if rel + ".py" in self.held.sources:
            return (rel + ".py", False)
        if rel + "/__init__.py" in self.held.sources:
            return (rel + "/__init__.py", True)
        return None

    def find_spec(self, fullname: str, path: Any = None,
                  target: Any = None) -> Any:
        found = self._source_of(fullname)
        if found is None:
            return None
        rel, is_package = found
        spec = importlib.machinery.ModuleSpec(
            fullname, self, origin=str(self.held.folder / rel),
            is_package=is_package)
        spec.has_location = True
        if is_package:
            # Empty: no finder but this one may look inside it, so a file
            # added to the folder after start-up is never imported from
            # disk.
            spec.submodule_search_locations = []
        return spec

    # -- loading ---------------------------------------------------------
    def create_module(self, spec: Any) -> Any:
        return None                       # the default module object

    def exec_module(self, module: Any) -> None:
        rel, _is_package = self._source_of(module.__name__) or ("", False)
        # A package with no __init__.py of its own is an empty package.
        source = self.held.sources.get(rel, b"")
        filename = str(self.held.folder / rel)
        code = compile(source, filename, "exec", dont_inherit=True)
        module.__file__ = filename
        exec(code, module.__dict__)

    def get_source(self, fullname: str) -> Optional[str]:
        found = self._source_of(fullname)
        if found is None:
            return None
        return self.held.sources.get(found[0], b"").decode("utf-8",
                                                           "replace")


_LOCK = threading.RLock()
_IMPORTERS: Dict[str, HeldImporter] = {}


def _root() -> None:
    """The package root, an empty package no finder serves from disk."""
    if PACKAGE_ROOT not in sys.modules:
        root = types.ModuleType(PACKAGE_ROOT)
        root.__path__ = []                    # nothing on disk is in it
        root.__package__ = PACKAGE_ROOT
        sys.modules[PACKAGE_ROOT] = root


def install(held: HeldPlugin) -> HeldImporter:
    """Put `held`'s importer in front of Python's finders (replacing one
    for the same package, whose modules are forgotten first)."""
    with _LOCK:
        uninstall(held.package)
        _root()
        importer = HeldImporter(held)
        _IMPORTERS[held.package] = importer
        sys.meta_path.insert(0, importer)
        return importer


def uninstall(package: str) -> None:
    """Take `package`'s importer out and forget its modules."""
    with _LOCK:
        importer = _IMPORTERS.pop(package, None)
        if importer is not None:
            try:
                sys.meta_path.remove(importer)
            except ValueError:
                pass
        for name in [n for n in sys.modules
                     if n == package or n.startswith(package + ".")]:
            sys.modules.pop(name, None)
        if not _IMPORTERS:
            sys.modules.pop(PACKAGE_ROOT, None)


def import_entry(held: HeldPlugin, module: str, attribute: str) -> Any:
    """`<package>.<module>`'s `attribute` (the manifest's entry), imported
    from the held bytes. LoaderError when it names nothing callable."""
    with _LOCK:
        if held.package not in _IMPORTERS or \
                _IMPORTERS[held.package].held is not held:
            install(held)
    kept = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        imported = importlib.import_module(f"{held.package}.{module}")
    finally:
        sys.dont_write_bytecode = kept
    factory = getattr(imported, attribute, None)
    if not callable(factory):
        raise LoaderError(f"its entry {module}:{attribute} names nothing "
                          f"callable")
    return factory


# ======================================================================
# Data files: read through the context, checked against start-up's hash
# ======================================================================
def read_data(held: HeldPlugin, name: str) -> bytes:
    """Data file `name` of the plugin (relative to its folder), checked
    against the sha256 taken at start-up: a file changed or added while
    Typhon runs is refused (it is used from the next start)."""
    rel = Path(str(name)).as_posix().lstrip("/")
    if ".." in Path(rel).parts:
        raise LoaderError(f"{name!r} is outside the plugin's folder")
    known = held.files.get(rel)
    if known is None:
        raise LoaderError(f"{rel} was not in the plugin's folder when "
                          f"Typhon started; it is read from the next start")
    try:
        data = (held.folder / rel).read_bytes()
    except OSError as exc:
        raise LoaderError(f"cannot read {rel}: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != known.sha256:
        raise LoaderError(f"{rel} changed since Typhon started; the changed "
                          f"file is used from the next start")
    return data


# ======================================================================
# The risky-pattern scan (a speed bump, design 4.6 step 2)
# ======================================================================
#: A plugin's own patterns, beside council_core.ide_trust's.
PLUGIN_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"\bctypes\b", "native calls (ctypes)"),
    (r"\bos\._exit\b", "ends Typhon at once"),
    (r"\bos\.startfile\b", "opens programs or files"),
    (r"\bwebbrowser\b", "opens a web browser"),
    (r"\bimportlib\b", "dynamic imports"),
    (r"\bos\.chmod\b", "changes file permissions"),
    (r"\bhttp\.client\b|\burllib3\b|\bhttpx\b", "outbound HTTP"),
    (r"\bopen\([^)]*['\"][wa]b?\+?['\"]", "writes files directly"),
)
_PLUGIN_COMPILED = tuple((re.compile(p), label)
                         for p, label in PLUGIN_PATTERNS)


@dataclass(frozen=True)
class Risk:
    file: str
    line: int
    label: str
    snippet: str


def scan(held: HeldPlugin) -> List[Risk]:
    """Every risky line in the plugin's .py files, at most one per line."""
    from . import ide_trust

    out: List[Risk] = []
    for rel, data in sorted(held.sources.items()):
        text = data.decode("utf-8", "replace")
        seen = set()
        for hit in ide_trust.scan(text):
            seen.add(hit.line)
            out.append(Risk(rel, hit.line, hit.label, hit.snippet))
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if number in seen or not stripped or stripped.startswith("#"):
                continue
            for pattern, label in _PLUGIN_COMPILED:
                if pattern.search(line):
                    out.append(Risk(rel, number, label,
                                    stripped[:ide_trust.SNIPPET]))
                    break
    out.sort(key=lambda r: (r.file, r.line))
    return out


# ======================================================================
# The network tripwire (design 4.6 step 4)
# ======================================================================
NETWORK_NONE = "none"
NETWORK_CAMERA = "camera-link"
NETWORK_INTERNET = "internet"

#: What each loaded third-party package may reach: its manifest's network
#: field, "internet" only once the user opted it in.
_NETWORK: Dict[str, str] = {}
_HOOKED = False
#: Set by the host: told (package, address, why) when a connection is
#: refused, so the plugin is named in the status line.
on_refused: Optional[Callable[[str, Any, str], None]] = None


def allow_network(package: str, network: str) -> None:
    """Package `package` may reach what `network` allows from now on."""
    with _LOCK:
        _NETWORK[package] = network or NETWORK_NONE


def forget_network(package: str) -> None:
    with _LOCK:
        _NETWORK.pop(package, None)


def _host_of(address: Any) -> Optional[str]:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return None                         # a Unix socket path: local


def address_allowed(address: Any, network: str) -> bool:
    """May a plugin whose manifest says `network` connect to `address`?"""
    if network == NETWORK_INTERNET:
        return True
    host = _host_of(address)
    if host is None:
        return True
    host = host.split("%", 1)[0]
    if host in ("localhost", ""):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A name: only "internet" may resolve names off this PC.
        return False
    if ip.is_loopback:
        return True
    if network == NETWORK_CAMERA:
        return ip.is_private or ip.is_link_local
    return False


def _plugin_on_stack() -> Optional[str]:
    """The third-party package whose code is on the calling stack, or
    None."""
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_globals.get("__name__", "")
        if isinstance(name, str) and name.startswith(PACKAGE_ROOT + "."):
            parts = name.split(".")
            return ".".join(parts[:2])
        frame = frame.f_back
    return None


def _audit(event: str, args: Tuple[Any, ...]) -> None:
    if event not in ("socket.connect", "socket.sendto"):
        return
    if not _NETWORK:
        return
    package = _plugin_on_stack()
    if package is None:
        return
    network = _NETWORK.get(package, NETWORK_NONE)
    address = args[1] if len(args) > 1 else None
    if address_allowed(address, network):
        return
    why = (f"{package} tried to connect to {address!r}, and its manifest "
           f"allows only network {network!r}")
    fn = on_refused
    if fn is not None:
        try:
            fn(package, address, why)
        except Exception:                                 # noqa: BLE001
            pass
    raise PermissionError(why)


def install_tripwire() -> None:
    """Add the audit hook, once per process (it cannot be taken out: with
    no third-party plugin loaded it returns at once)."""
    global _HOOKED
    with _LOCK:
        if _HOOKED:
            return
        sys.addaudithook(_audit)
        _HOOKED = True
