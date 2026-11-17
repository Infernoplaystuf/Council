"""
council_core.openeb_env — let Typhon find an OpenEB build by itself.

OpenEB (Prophesee's open-source Metavision SDK, the EVK4's software without
an account) is built from source on Windows. `install_openeb.bat`
(council_core/openeb_install.py) builds and installs it into a folder of its
own — C:\\openeb unless told otherwise — and writes where in a small pointer
file. Prophesee's own guide then asks the user to edit PATH,
MV_HAL_PLUGIN_PATH, HDF5_PLUGIN_PATH and PYTHONPATH by hand (Andy, 16
November 2026: make it easy). `prepare()` does that for this process only,
just before `import metavision_hal`, so nothing on the machine is changed
and a build the user set up by hand still wins.

Standard library only; never raises.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

#: Points at an OpenEB install root (it holds install\bin ...). Optional.
ENV = "COUNCIL_OPENEB_DIR"
#: The pointer file the installer writes: {"root": "C:\\openeb", ...}.
POINTER_NAME = "openeb.json"
#: Where the installer builds when not told otherwise: short, because
#: vcpkg's build folders nest deep and Windows paths stop at 260 chars.
DEFAULT_ROOT = "openeb"

_done: Dict[str, List[str]] = {}


def pointer_file() -> Path:
    """%LOCALAPPDATA%\\Council\\openeb.json (~/.council/openeb.json
    elsewhere): where the installer says it put the build."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "Council" / POINTER_NAME
    return Path.home() / ".council" / POINTER_NAME


def default_root() -> Path:
    drive = os.environ.get("SystemDrive", "C:")
    return Path(drive + "\\") / DEFAULT_ROOT if os.name == "nt" \
        else Path.home() / DEFAULT_ROOT


def find_root() -> Optional[Path]:
    """The install root of an OpenEB build: COUNCIL_OPENEB_DIR, else the
    installer's pointer file, else the default folder; None when none of
    them holds one."""
    candidates: List[Path] = []
    if os.environ.get(ENV):
        candidates.append(Path(os.environ[ENV]))
    try:
        data = json.loads(pointer_file().read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("root"):
            candidates.append(Path(str(data["root"])))
    except (OSError, ValueError):
        pass
    candidates.append(default_root())
    for root in candidates:
        if (root / "install" / "bin").is_dir():
            return root
    return None


def paths(root: Path) -> Dict[str, Path]:
    """Where the parts of an install under `root` are (the installer's
    layout: CMAKE_INSTALL_PREFIX root\\install, PYTHON3_SITE_PACKAGES
    root\\install\\site-packages)."""
    install = root / "install"
    return {"bin": install / "bin",
            "plugins": install / "lib" / "metavision" / "hal" / "plugins",
            "hdf5": install / "lib" / "hdf5" / "plugin",
            "site": install / "site-packages"}


def prepare(root: Optional[Path] = None) -> List[str]:
    """Make an OpenEB build importable in this process: its bin folder on
    PATH (and a DLL folder, Python 3.8+ on Windows), MV_HAL_PLUGIN_PATH and
    HDF5_PLUGIN_PATH set when not set already, and its site-packages on
    sys.path. Returns what it did, one line each ([] when there is no build
    or it was done before). A variable the user set is left alone."""
    try:
        root = root or find_root()
        if root is None:
            return []
        key = str(root)
        if key in _done:
            return []
        did: List[str] = []
        where = paths(root)
        if where["bin"].is_dir():
            os.environ["PATH"] = (str(where["bin"]) + os.pathsep
                                  + os.environ.get("PATH", ""))
            adder = getattr(os, "add_dll_directory", None)
            if adder is not None:
                try:
                    adder(str(where["bin"]))
                except OSError:
                    pass
            did.append(f"PATH += {where['bin']}")
        for name, part in (("MV_HAL_PLUGIN_PATH", "plugins"),
                           ("HDF5_PLUGIN_PATH", "hdf5")):
            if not os.environ.get(name) and where[part].is_dir():
                os.environ[name] = str(where[part])
                did.append(f"{name} = {where[part]}")
        if where["site"].is_dir() and str(where["site"]) not in sys.path:
            sys.path.insert(0, str(where["site"]))
            did.append(f"sys.path += {where['site']}")
        _done[key] = did
        return did
    except Exception:                                     # noqa: BLE001
        return []


def describe() -> str:
    """One line for check.bat and the Command Prompt."""
    root = find_root()
    if root is None:
        return ("OpenEB build: none found (install_openeb.bat builds one; "
                f"or set {ENV})")
    return f"OpenEB build: {root}"
