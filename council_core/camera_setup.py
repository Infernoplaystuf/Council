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
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

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
