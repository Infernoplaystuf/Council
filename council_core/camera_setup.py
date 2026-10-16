"""
council_core.camera_setup — which camera a capture app is for, what to install
for it, and whether that is actually done yet.

The setup wizard in council_qt.widgets.camera_wizard is a view over this
module; everything it says and checks lives here, with no toolkit imported.

INSTRUCTIONS ARE NOT ENOUGH ON THEIR OWN, SO THEY COME WITH CHECKS
A page of steps tells the user what to install; it cannot tell them whether
they did it right. The failure this was built around is concrete and measured:
the pylon runtime inside the pypylon pip wheel offers USB, GigE and Camera
Emulation transport layers and NO CoaXPress one, so a boA5320-150cm can never
be found by a machine that only ran `pip install pypylon` — and nothing about
that looks wrong until the camera list comes up empty at the bench. So each
guide comes with checks that ask the installed software what it can actually
do, and say which step fixes what they find.

A CHECK HAS THREE ANSWERS, NOT TWO
True (done), False (this will stop the camera working), or None (could not
tell, or only matters for some cameras). A CoaXPress transport layer is
required for the boA5320 and irrelevant to a USB Basler, so its absence is a
warning rather than a failure; flattening that into a red cross would teach
users to ignore the crosses.

THE CHOICE IS SAVED PER PROJECT
Next to the app, as camera_setup.json. Projects are generated per machine
(the vault is not in git), so a fresh machine gets a fresh wizard — which is
the point: the machine is what has or lacks the SDK. And two apps on one
machine can be set up for different cameras.

SEVERAL CAMERA FAMILIES (typhon-plugins branch, stage P2 rows 3 and 4)
A "Typhon (plugins)" project ticks camera FAMILIES instead of choosing one
(docs/typhon_plugins_design.md 2.4 item 15; docs/typhon_multicamera_design.md
2.2): each family is a camera plugin with a set-up of its own — its guide,
its checks and, for a Basler, its scan — which the plugin host hands the
wizard as a `Family`. The same file keeps them, still FORMAT 1, as a
"cameras" list beside the one "camera" today's reader knows
(`save_families`), so a file written either way reads either way: a
single-choice file is a list of one (`load_families`), and today's Typhon
reads the first family it knows from a list. A project set up today opens
unchanged and is not asked again.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, List, Mapping, Optional, Sequence,
                    Tuple)

#: The cameras a capture app can be set up for, in the order offered.
CHOICES: Tuple[str, ...] = ("basler", "prophesee")

#: Written next to the app's app.py.
SETUP_FILE = "camera_setup.json"

#: Bump if the saved format changes; an older file is then offered again.
FORMAT = 1

#: The Python versions the Metavision SDK's bindings are built for, per
#: Prophesee's Windows installation page.
SDK_PYTHONS: Tuple[str, ...] = ("3.10", "3.11", "3.12")

#: Binding the EVK4's USB interface to WinUSB, as Prophesee's OpenEB page
#: gives it. 0x00f5 is the EVK4; 0x00f4 the EVK3; 0x00f3 the CX3 bootloader
#: mode a camera drops into during a firmware update. All three, as the page
#: says — a camera caught mid-update is otherwise unreachable.
WDI_COMMANDS: Tuple[str, ...] = (
    'wdi-simple.exe -n "EVK" -m "Prophesee" -v 0x04b4 -p 0x00f4',
    'wdi-simple.exe -n "EVK" -m "Prophesee" -v 0x04b4 -p 0x00f5',
    'wdi-simple.exe -n "EVK" -m "Prophesee" -v 0x04b4 -p 0x00f3',
)


# ======================================================================
# What to install
# ======================================================================
@dataclass(frozen=True)
class Step:
    """One instruction. `url` and `command` are optional and shown as-is."""
    text: str
    url: str = ""
    command: str = ""


@dataclass(frozen=True)
class Guide:
    key: str
    title: str
    summary: str
    steps: Tuple[Step, ...]
    #: Said once, after the steps.
    note: str = ""


def _pip(package: str, python: str) -> str:
    """The install command for THIS interpreter, not whichever `pip` is first
    on PATH.

    The app runs under whatever Python was chosen for it, and the vendor SDKs
    are built for particular versions. `pip install pypylon` in a terminal
    installs into the terminal's Python, which is very often not the one this
    app is running under — so the package is "installed" and still missing.
    """
    return f'"{python}" -m pip install {package}'


def guide(choice: str, python: Optional[str] = None,
          version: Optional[str] = None) -> Guide:
    """The installation steps for `choice`, written for `python`.

    `version` is the running Python's "major.minor" unless given.
    """
    python = python or sys.executable
    version = version or ".".join(str(v) for v in sys.version_info[:2])
    if choice == "basler":
        return Guide(
            key="basler",
            title="Basler camera (pylon)",
            summary=("For Basler frame cameras, including the boA5320-150cm "
                     "(boost series, CoaXPress)."),
            steps=(
                Step("Download the pylon Camera Software Suite for Windows "
                     "from Basler.",
                     url="https://www.baslerweb.com/en/downloads/software/"),
                Step("Run the installer. When it asks which camera interfaces "
                     "to install, include CoaXPress (CXP). The boA5320-150cm "
                     "is a CoaXPress camera: without CXP support it can never "
                     "be found, and the Python package alone does not include "
                     "it.",
                     url="https://docs.baslerweb.com/"
                         "software-installation-(windows)"),
                Step("Install the Python bindings into the Python this app "
                     "runs under:",
                     command=_pip("pypylon", python)),
                Step("Open the pylon Viewer and confirm the camera appears. "
                     "For CoaXPress, check the interface card's applet matches "
                     "the number of CXP cables actually connected."),
                Step("Close the pylon Viewer before scanning from this app. A "
                     "frame grabber can only be held by one program at a "
                     "time."),
            ),
            note=("No camera yet? Setting PYLON_CAMEMU=2 before starting this "
                  "app makes pylon present two emulated cameras, so the whole "
                  "workflow can be tried without hardware."),
        )
    if choice == "prophesee":
        supported = version in SDK_PYTHONS
        return Guide(
            key="prophesee",
            title="Prophesee EVK4 (Metavision)",
            summary="For Prophesee event cameras such as the EVK4.",
            steps=(
                Step("Get the Metavision SDK — it is not available through "
                     "pip. Either the official Windows installer (it sets up "
                     "the camera's USB driver for you, but downloading it "
                     "needs an account requested from Prophesee), or the "
                     "open-source OpenEB (no account, but you build it from "
                     "source with Visual Studio 2022 — a few hours).",
                     url="https://docs.prophesee.ai/stable/installation/"
                         "index.html"),
                Step(f"This app runs Python {version}. The SDK's Python "
                     f"bindings are built for Python "
                     f"{', '.join(SDK_PYTHONS)} and will not import under "
                     f"any other — "
                     + ("this one is supported." if supported else
                        "THIS ONE IS NOT SUPPORTED. Choose a supported "
                        "Python for this app with Run with in the GUI "
                        "Designer, or build it with --python.")),
                Step("Install the Python packages the SDK's installation "
                     "guide lists, into this same Python."),
                Step("Only if you built OpenEB — the installer does this for "
                     "you: install the EVK4's USB driver. Windows has no "
                     "built-in driver for it, and without one the camera is "
                     "simply not found, exactly as if it were unplugged. "
                     "Download wdi-simple.exe from the \"Camera Plugins\" "
                     "section of this page, then run these in a Command "
                     "Prompt opened as administrator:",
                     url="https://docs.prophesee.ai/stable/installation/"
                         "windows_openeb.html",
                     command="\n".join(WDI_COMMANDS)),
                Step("Plug the EVK4 into a USB 3 port directly, not through "
                     "a hub."),
            ),
            note=("Event cameras have no frames, no exposure and no gain. "
                  "The live view shows events binned over a short window, "
                  "and the status line reports events per window and the "
                  "event rate rather than frames per second."),
        )
    raise ValueError(f"unknown camera {choice!r}; expected one of {CHOICES}")


# ======================================================================
# Is it done?
# ======================================================================
@dataclass(frozen=True)
class Check:
    label: str
    #: True done, False will stop the camera working, None could not tell or
    #: only matters for some cameras.
    ok: Optional[bool]
    detail: str = ""
    #: What to do about it, when ok is not True.
    fix: str = ""


@dataclass
class Readiness:
    choice: str
    checks: List[Check] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Nothing FAILED. Warnings do not block."""
        return all(c.ok is not False for c in self.checks)

    @property
    def found_camera(self) -> bool:
        return any(c.label == CAMERAS_FOUND and c.ok for c in self.checks)

    def summary(self) -> str:
        failed = [c for c in self.checks if c.ok is False]
        if failed:
            # What to DO, not the name of the check: "Not ready: pypylon
            # installed" read as the opposite of what it meant.
            return f"Not ready — {failed[0].fix or failed[0].detail}"
        if self.found_camera:
            return "Ready — a camera is connected."
        return "Software is installed. Connect the camera and scan."


CAMERAS_FOUND = "Camera connected"


def check(choice: str, *, basler_sdk: Any = None, evk_sdk: Any = None
          ) -> Readiness:
    """Ask the installed software what it can actually do.

    Enumeration can take seconds — pylon walks every transport layer — so
    call this off the UI thread. It never raises: a check that cannot run is
    reported, because a checklist that crashes is no checklist at all.
    """
    if choice == "basler":
        return Readiness(choice, _basler_checks(basler_sdk))
    if choice == "prophesee":
        return Readiness(choice, _evk_checks(evk_sdk))
    raise ValueError(f"unknown camera {choice!r}; expected one of {CHOICES}")


def _basler_checks(sdk: Any) -> List[Check]:
    from . import cameras

    backend = cameras.BaslerBackend(sdk)
    out: List[Check] = []
    try:
        pylon = backend.sdk()
    except cameras.CameraError as exc:
        return [Check("pypylon installed", False, str(exc),
                      "Install the Python bindings (step 3).")]
    out.append(Check("pypylon installed", True, _pylon_version(pylon)))

    layers = _transport_layers(pylon)
    if layers is None:
        out.append(Check("CoaXPress support", None,
                         "pylon did not report its transport layers.",
                         "If a CoaXPress camera is not found, reinstall pylon "
                         "with CoaXPress (CXP) included (step 2)."))
    else:
        names = ", ".join(n for n, _ in layers) or "none"
        has_cxp = any(_is_cxp(n, t) for n, t in layers)
        out.append(Check(
            "CoaXPress support", True if has_cxp else None,
            f"This pylon offers: {names}.",
            "" if has_cxp else
            ("Required for the boA5320-150cm and other CoaXPress cameras — "
             "install the pylon Software Suite with CoaXPress (CXP) "
             "included (step 2). Not needed for USB or GigE cameras.")))

    out.append(_cameras_check(backend, "basler"))
    return out


def _evk_checks(sdk: Any) -> List[Check]:
    from . import cameras

    backend = cameras.EvkBackend(sdk)
    out: List[Check] = []
    try:
        backend.sdk()
    except cameras.CameraError as exc:
        version = ".".join(str(v) for v in sys.version_info[:2])
        return [Check(
            "Metavision SDK installed", False, str(exc),
            f"Install the Metavision SDK (step 1). If it IS installed, its "
            f"Python bindings may be built for a different Python than "
            f"{version} (step 2).")]
    out.append(Check("Metavision SDK installed", True, "metavision_hal imports."))
    out.append(_cameras_check(backend, "prophesee"))
    return out


def _cameras_check(backend: Any, choice: str) -> Check:
    from . import cameras

    try:
        found = backend.discover()
    except cameras.CameraError as exc:
        return Check(CAMERAS_FOUND, False, str(exc),
                     "The SDK could not search for cameras; reinstall it.")
    except Exception as exc:                              # noqa: BLE001
        return Check(CAMERAS_FOUND, False, f"{type(exc).__name__}: {exc}",
                     "The SDK could not search for cameras; reinstall it.")
    if found:
        names = "; ".join(_describe(c) for c in found)
        return Check(CAMERAS_FOUND, True, names)
    fix = ("Plug the camera in and check again. For CoaXPress, confirm it "
           "appears in the pylon Viewer, then close the Viewer."
           if choice == "basler" else
           # The SDK cannot tell "no driver" from "no camera" — both are an
           # empty list — so say which is likelier once it IS plugged in.
           "Plug the EVK4 into a USB 3 port and check again. If it is "
           "already plugged in, the USB driver is the likely cause: Windows "
           "has no built-in driver for it (step 4).")
    # None, not False: "no camera plugged in YET" is the normal state while
    # someone is working through the setup, and the software may be fine.
    return Check(CAMERAS_FOUND, None, "No camera found.", fix)


def _describe(info: Any) -> str:
    label = getattr(info, "label", "") or getattr(info, "key", "?")
    if "emulation" in str(getattr(info, "model", "")).lower():
        return f"{label} (emulated)"
    return label


def _pylon_version(pylon: Any) -> str:
    fn = getattr(pylon, "GetPylonVersionString", None)
    try:
        return f"pylon runtime {fn()}" if fn else "pypylon imports."
    except Exception:                                     # noqa: BLE001
        return "pypylon imports."


def _transport_layers(pylon: Any) -> Optional[List[Tuple[str, str]]]:
    """(friendly name, TL type) for every transport layer pylon offers."""
    try:
        tls = pylon.TlFactory.GetInstance().EnumerateTls()
    except Exception:                                     # noqa: BLE001
        return None
    out = []
    for tl in tls or ():
        name = _get(tl, "GetFriendlyName") or _get(tl, "GetDeviceClass") or "?"
        kind = " ".join(filter(None, (_get(tl, "GetTLType"),
                                      _get(tl, "GetDeviceClass"),
                                      _get(tl, "GetFullName"))))
        out.append((name, kind))
    return out


def _is_cxp(name: str, kind: str) -> bool:
    text = f"{name} {kind}".lower()
    return "cxp" in text or "coaxpress" in text


def _get(obj: Any, name: str) -> str:
    fn = getattr(obj, name, None)
    try:
        return str(fn()) if fn else ""
    except Exception:                                     # noqa: BLE001
        return ""


# ======================================================================
# Remembering the answer
# ======================================================================
def setup_path(project_dir: Any) -> Path:
    return Path(project_dir) / SETUP_FILE


def load_choice(path: Any) -> Optional[str]:
    """The saved choice, or None if there is none worth trusting.

    A missing, unreadable, older-format or unknown-camera file all mean the
    same thing to the caller: ask again. None of them is an error worth
    stopping an app from starting over.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    choice = data.get("camera")
    return choice if choice in CHOICES else None


def save_choice(path: Any, choice: str) -> None:
    if choice not in CHOICES:
        raise ValueError(f"unknown camera {choice!r}; expected one of "
                         f"{CHOICES}")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written whole and then renamed, so an app killed mid-write leaves the
    # old answer or none — never half a JSON file that reads as garbage.
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps({"format": FORMAT, "camera": choice,
                                "saved": time.strftime("%Y-%m-%dT%H:%M:%S")},
                               indent=2) + "\n", encoding="utf-8")
    temp.replace(target)


def label(choice: Optional[str]) -> str:
    return {"basler": "Basler", "prophesee": "Prophesee EVK4"}.get(
        choice or "", "not set up")


# ======================================================================
# Several camera families (a "Typhon (plugins)" project)
# ======================================================================
#: Where camera_setup.json keeps the families a project ticked, in order.
FAMILIES_KEY = "cameras"

#: A family is its camera plugin's backend name: "basler", "prophesee", or a
#: third party's dotted one ("acme.thermal", plugin design 3.3).
FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")

#: The most families one set-up may tick (far more than a rig runs).
MAX_FAMILIES = 32

_TEMP_NUMBERS = itertools.count(1)


@dataclass(frozen=True)
class Family:
    """One camera family a project can be set up for: a camera plugin with
    a set-up (plugin design 2.3, 2.4 item 15). The plugin host builds it
    from the plugin's own guide, checks and scan, each guarded, so none of
    them raises; the wizard only lays them out.

    `key` is the family as camera_setup.json keeps it (the plugin's
    backend), `plugin` the plugin's id, `label` the short name said after
    the wizard ("Set up for Basler."), `title` and `summary` what its check
    box says. `guide(python)` is the install steps for that interpreter;
    `check()` asks the installed software (seconds: off the UI thread);
    `scan(held)`, when the plugin has a scan page (a Basler), lists every
    camera of the family and whether each will run, told which cameras the
    app holds open ({key: slot name}) so it never opens them again."""
    key: str
    plugin: str
    label: str
    title: str
    summary: str
    guide: Callable[[str], Guide]
    check: Callable[[], "Readiness"]
    scan: Optional[Callable[[Mapping[str, str]], Any]] = None
    scan_label: str = ""
    scan_tip: str = ""


def _read_setup(path: Any) -> Optional[dict]:
    """The file's document when it is one this module wrote (FORMAT), else
    None — ask again."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    return data


def clean_families(families: Any) -> List[str]:
    """`families` as a list of family names, in order, each once; ValueError
    for anything that cannot be one, or for none at all."""
    if isinstance(families, str) or not isinstance(families, (list, tuple)):
        raise ValueError("the camera families are a list of names")
    out: List[str] = []
    for family in families:
        if not isinstance(family, str) or not FAMILY_RE.match(family):
            raise ValueError(f"{family!r} cannot name a camera family "
                             f"(a camera plugin's backend, such as "
                             f"{', '.join(CHOICES)})")
        if family not in out:
            out.append(family)
    if not out:
        raise ValueError("no camera family was ticked")
    if len(out) > MAX_FAMILIES:
        raise ValueError(f"{len(out)} camera families; at most "
                         f"{MAX_FAMILIES}")
    return out


def load_families(path: Any) -> Optional[List[str]]:
    """The camera families the set-up ticked, in order: the file's
    "cameras" list, or — a single-choice file, as today's Typhon writes it
    (FORMAT 1) — its one "camera" as a list of one. None when there is none
    worth trusting, which means what it means to load_choice: ask again. A
    "cameras" entry that is not a list of family names is not trusted, even
    beside a good "camera"."""
    data = _read_setup(path)
    if data is None:
        return None
    if FAMILIES_KEY in data:
        try:
            return clean_families(data[FAMILIES_KEY])
        except ValueError:
            return None
    choice = data.get("camera")
    return [choice] if choice in CHOICES else None


def save_families(path: Any, families: Sequence[str]) -> List[str]:
    """Save the families a project's set-up ticked; returns them as saved.

    Still FORMAT 1 (multi-camera design 2.2): "cameras" holds every family
    in order, and "camera" the first one today's single-choice reader knows
    (load_choice), so the file still reads there; with none it knows, there
    is no "camera" and that reader asks again. Written whole under a name
    no other writer uses, flushed, then renamed into place: a crash leaves
    the old answer or the new one, never half of either."""
    chosen = clean_families(families)
    doc: dict = {"format": FORMAT}
    known = next((f for f in chosen if f in CHOICES), None)
    if known is not None:
        doc["camera"] = known
    doc[FAMILIES_KEY] = chosen
    doc["saved"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}."
                            f"{threading.get_ident()}."
                            f"{next(_TEMP_NUMBERS)}.tmp")
    try:
        with open(temp, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(doc, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp, target)
                break
            except PermissionError:
                # Windows: a scanner holding the old file for a moment.
                if attempt == 4:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    return chosen


def families_label(chosen: Optional[Sequence[str]],
                   families: Sequence[Family] = ()) -> str:
    """"Basler", "Basler and Prophesee EVK4", "A, B and C": the families
    `chosen`, each by its Family's label, else by label() for a built-in
    one, else by its own name. "not set up" for none."""
    if not chosen:
        return "not set up"
    by_key = {f.key: f.label for f in families}
    words = [by_key.get(k) or (label(k) if k in CHOICES else k)
             for k in chosen]
    if len(words) == 1:
        return words[0]
    return f"{', '.join(words[:-1])} and {words[-1]}"
