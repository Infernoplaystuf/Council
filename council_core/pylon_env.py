"""
council_core.pylon_env — making sure pypylon can see a CoaXPress camera, and
saying what it sees when it cannot (13 November 2026).

WHY. A boost (boA9344-30cm) on a CXP frame grabber worked in pylon Viewer and
Typhon listed no camera. pylon Viewer runs on the pylon Software Suite's own
runtime; Typhon runs on pypylon, whose Windows wheel has carried no CXP
transport layer since 4.0.0. pypylon reaches a CXP camera only through a
GenTL producer (*.cti) found on GENICAM_GENTL64_PATH — which the suite's
installer sets for the machine, but which a process started from an older
environment (a console or env opened before the install, an IDE, a
scheduler) never inherits.

So, before pypylon is first imported (prepare):
  * GENICAM_GENTL64_PATH as Windows has it for the machine and the user (the
    registry), joined to this process's own;
  * else the folders under Program Files\\Basler that hold a *.cti;
added to this process's GENICAM_GENTL64_PATH when not there already.
Nothing is written anywhere: only this process's environment changes.
COUNCIL_NO_GENTL_SEARCH=1 turns it off.

And report(): pypylon's version, its transport layers, the devices it
enumerates and the GenTL path — printed by check_setup.py and named in the
note Typhon shows when no Basler camera is found.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ENV = "GENICAM_GENTL64_PATH"
OFF_ENV = "COUNCIL_NO_GENTL_SEARCH"
#: How deep under Program Files\Basler a *.cti is looked for.
MAX_DEPTH = 6

_prepared: Optional[List[str]] = None


def _split(value: str) -> List[str]:
    return [p.strip().strip('"') for p in (value or "").split(os.pathsep)
            if p.strip().strip('"')]


def _registry_values() -> List[str]:
    """GENICAM_GENTL64_PATH as the machine and the user have it (what a
    freshly started console would get); [] off Windows or when unreadable."""
    if os.name != "nt":
        return []
    try:
        import winreg                                   # type: ignore
    except ImportError:
        return []
    out: List[str] = []
    keys = ((winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment"))
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _kind = winreg.QueryValueEx(key, ENV)
        except OSError:
            continue
        out += _split(os.path.expandvars(str(value)))
    return out


def _program_files() -> List[Path]:
    roots = []
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(var, "")
        if value and Path(value) not in roots:
            roots.append(Path(value))
    return roots


def cti_folders(roots: Iterable[Path]) -> List[str]:
    """The folders under each root's Basler folder that hold a *.cti (a
    GenTL producer), 64-bit ones first by name."""
    found: List[str] = []
    for root in roots:
        base = Path(root) / "Basler"
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            depth = len(Path(dirpath).relative_to(base).parts)
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            if any(f.lower().endswith(".cti") for f in filenames) and \
                    dirpath not in found:
                found.append(dirpath)
    found.sort(key=lambda p: ("64" not in p, p.lower()))
    return found


def _has_cti(folder: str) -> bool:
    try:
        return any(n.lower().endswith(".cti") for n in os.listdir(folder))
    except OSError:
        return False


def prepare(*, registry: Optional[List[str]] = None,
            roots: Optional[Iterable[Path]] = None,
            force: bool = False) -> List[str]:
    """Add what pypylon needs to find a CXP producer to THIS process's
    GENICAM_GENTL64_PATH (before pypylon is imported). Returns the folders
    added. Once per process unless `force`."""
    global _prepared
    if _prepared is not None and not force:
        return list(_prepared)
    added: List[str] = []
    if os.environ.get(OFF_ENV, "").strip() not in ("", "0"):
        _prepared = added
        return added
    now = _split(os.environ.get(ENV, ""))
    wanted = list(_registry_values() if registry is None else registry)
    if not any(_has_cti(p) for p in now + wanted):
        wanted += cti_folders(_program_files() if roots is None else roots)
    for folder in wanted:
        if folder not in now and os.path.isdir(folder):
            now.append(folder)
            added.append(folder)
    if added:
        os.environ[ENV] = os.pathsep.join(now)
    _prepared = added
    return list(added)


def _version() -> str:
    """pypylon's version (it has no __version__: the package metadata)."""
    try:
        from importlib import metadata
        return metadata.version("pypylon")
    except Exception:                                   # noqa: BLE001
        return ""


def report(sdk: Any = None) -> Dict[str, Any]:
    """What pypylon sees: version, transport layers, devices, the GenTL
    path and what prepare() added. `error` when pypylon is missing or
    enumerating failed."""
    out: Dict[str, Any] = {"version": "", "transport_layers": [],
                           "devices": [], "gentl_path": [],
                           "added": list(_prepared or []), "error": ""}
    if sdk is None:
        prepare()
        try:
            import pypylon                              # type: ignore
            from pypylon import pylon                   # type: ignore
        except Exception as exc:                        # noqa: BLE001
            out["error"] = f"pypylon is not installed: {exc}"
            out["gentl_path"] = _split(os.environ.get(ENV, ""))
            return out
        sdk = pylon
    out["version"] = _version()
    out["gentl_path"] = _split(os.environ.get(ENV, ""))
    try:
        factory = sdk.TlFactory.GetInstance()
        for tl in factory.EnumerateTls() or []:
            name = getattr(tl, "GetFriendlyName", None)
            out["transport_layers"].append(str(name() if callable(name)
                                               else tl))
        for dev in factory.EnumerateDevices() or []:
            name = getattr(dev, "GetFriendlyName", None)
            out["devices"].append(str(name() if callable(name) else dev))
    except Exception as exc:                            # noqa: BLE001
        out["error"] = f"pylon could not enumerate: {exc}"
    return out


def has_cxp(layers: Iterable[str]) -> bool:
    """Does a transport layer look like CoaXPress (or a GenTL producer,
    through which a CXP grabber's camera comes)?"""
    return any(("cxp" in l.lower() or "coaxpress" in l.lower()
                or "gentl" in l.lower() or "gtc" in l.lower())
               for l in layers)


def words(rep: Dict[str, Any], path: bool = True) -> List[str]:
    """report() as lines for a person."""
    if rep.get("error") and not rep.get("transport_layers"):
        lines = [rep["error"]]
    else:
        lines = [f"pypylon {rep.get('version') or '?'}: transport layers "
                 + (", ".join(rep["transport_layers"]) or "none"),
                 f"cameras pylon sees: {len(rep['devices'])}"
                 + (" (" + "; ".join(rep["devices"]) + ")"
                    if rep["devices"] else "")]
        if rep.get("error"):
            lines.append(rep["error"])
        if not has_cxp(rep["transport_layers"]):
            lines.append("no CoaXPress / GenTL transport layer: a camera on a "
                         "CXP frame grabber cannot be seen. Install the pylon "
                         "Software Suite with 'CXP Camera Support' ticked "
                         "(pylon Viewer can see the camera without it)")
    if not path:
        return lines
    entries = rep.get("gentl_path") or []
    lines.append(f"{ENV}: " + ("; ".join(entries) if entries else "not set"))
    if rep.get("added"):
        lines.append("added for this run: " + "; ".join(rep["added"]))
    return lines


# ======================================================================
# The whole picture, for a Command Prompt (Andy, 13 November 2026: "send
# messages in command prompt to help me diagnose things")
# ======================================================================
PREFIX = "[Typhon camera]"
QUIET_ENV = "COUNCIL_QUIET_CAMERA_CONSOLE"


def console(line: str) -> None:
    """One diagnostic line in the Command Prompt the app was started from
    (never in the way: a closed console is ignored)."""
    if os.environ.get(QUIET_ENV, "").strip() not in ("", "0"):
        return
    # stderr, not stdout: a program reading the app's printed output is not
    # disturbed; and plain ASCII, so no console code page (cp1252 turns
    # "\u00b7" into a byte a UTF-8 reader rejects) can garble or break it.
    text = f"{PREFIX} {line}".replace("\u00b7", "-").replace("\u2014", "-")
    text = text.encode("ascii", "replace").decode("ascii")
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except Exception:                                   # noqa: BLE001
        pass


def environment_lines() -> List[str]:
    """Which Python, which environment, which pylon installs, and what is
    on the GenTL path (each entry: its producers, or why it has none)."""
    import platform
    import sys
    lines = [f"Python {platform.python_version()} at {sys.executable}"]
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        if os.environ.get(var):
            lines.append(f"environment: {var}={os.environ[var]}")
    installs = []
    for root in _program_files():
        base = root / "Basler"
        try:
            installs += [str(p) for p in sorted(base.iterdir()) if p.is_dir()]
        except OSError:
            continue
    if os.name == "nt":
        lines.append("pylon installs: " + ("; ".join(installs) if installs
                                           else "none under Program Files"
                                           "\\Basler"))
    reg = _registry_values()
    if reg:
        lines.append(f"{ENV} for the machine/user (registry): "
                     + "; ".join(reg))
    path = _split(os.environ.get(ENV, ""))
    if not path:
        lines.append(f"{ENV} in this process: not set — pypylon sees no "
                     f"GenTL producer (a CXP camera needs one)")
    for folder in path:
        try:
            ctis = [n for n in os.listdir(folder) if n.lower().endswith(".cti")]
        except OSError:
            lines.append(f"{ENV} entry {folder}: MISSING folder")
            continue
        lines.append(f"{ENV} entry {folder}: "
                     + (", ".join(ctis) if ctis else "no .cti file in it"))
    if _prepared:
        lines.append("added for this run (from the registry or Program Files): "
                     + "; ".join(_prepared))
    return lines


def probe_lines(sdk: Any = None) -> List[str]:
    """Each Basler pylon enumerates, opened and closed again: what it is,
    whether it opens (and why not), its input lines and the trigger
    sources it offers — and what that means for a trigger cable."""
    from . import cameras
    lines: List[str] = []
    backend = cameras.BaslerBackend(sdk)
    try:
        infos = backend.discover()
    except Exception as exc:                            # noqa: BLE001
        return [f"enumerating failed: {exc}"]
    if not infos:
        return ["no Basler camera enumerated (see the transport layers "
                "above). Check: camera powered and its cables in; pylon "
                "Viewer sees it; pylon Viewer CLOSED; for CXP the frame "
                "grabber's applet matches the number of CXP cables"]
    for info in infos:
        lines.append(f"camera: {info.label}")
        try:
            device = backend.open(info)
        except Exception as exc:                        # noqa: BLE001
            lines.append(f"  cannot open it: {exc}")
            continue
        try:
            try:
                lim = device.limits()
                lines.append(f"  opens: sensor {lim.width}x{lim.height}")
            except Exception as exc:                    # noqa: BLE001
                lines.append(f"  opens; size unreadable: {exc}")
            try:
                own = tuple(device.input_lines())
                levels = device.line_states(own) if own else {}
                lines.append("  input lines: " + (", ".join(
                    f"{l} {'HIGH' if levels.get(l) else 'low'}"
                    if levels.get(l) is not None else l for l in own)
                    or "none (every line set to Output?)"))
            except Exception as exc:                    # noqa: BLE001
                own = ()
                lines.append(f"  input lines unreadable: {exc}")
            try:
                offered = tuple(getattr(device.capture_options(),
                                        "hardware", ()) or ())
                lines.append("  hardware trigger sources: "
                             + (", ".join(offered) or "none"))
                if any(s.startswith("Cxp") for s in offered):
                    lines.append("  CxpTrigger*: a trigger cable into the "
                                 "frame grabber arrives here — the grabber "
                                 "must be set (in its own software) to pass "
                                 "it on")
                if not offered:
                    lines.append("  PROBLEM: no hardware trigger source — "
                                 "Triggered runs from a cable cannot work")
            except Exception as exc:                    # noqa: BLE001
                lines.append(f"  trigger sources unreadable: {exc}")
            try:
                watch = tuple(device.trigger_inputs())
                lines.append("  Typhon can watch (snapshot/record): "
                             + (", ".join(watch) or "nothing — only "
                                "Triggered: exact takes a cable trigger"))
            except Exception as exc:                    # noqa: BLE001
                lines.append(f"  watchable lines unreadable: {exc}")
        finally:
            try:
                device.close()
            except Exception:                           # noqa: BLE001
                pass
    return lines


def full_lines(sdk: Any = None, probe: bool = True) -> List[str]:
    rep = report(sdk)
    lines = environment_lines() + words(rep, path=False)
    if probe and not rep.get("error", "").startswith("pypylon is not"):
        lines += probe_lines(sdk)
    return lines


def main() -> int:
    """python -m council_core.pylon_env — everything above, printed."""
    for line in full_lines():
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
