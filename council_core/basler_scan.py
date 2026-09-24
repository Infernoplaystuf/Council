"""
council_core.basler_scan — every Basler camera this machine can see, and
everything that decides whether it will actually run in Typhon.

The setup wizard's "Scan Basler cameras". For each camera it answers: will it
work, and if not, exactly what to do. Each finding is a Check (pass / warn /
fail / info, with a fix), and they roll up into a verdict: works, works with
limits, or will not work.

WHAT IS CHECKED, AND WHY EACH ONE IS HERE
  Machine    which pylon transport layers exist (USB, GigE, CoaXPress, Camera
             Link). The pip wheel has USB and GigE only; a boA5320-150cm is
             CoaXPress and cannot appear without the pylon Software Suite's
             CXP option and the CXP-12 card's driver.
  The PC     (Windows, via pnputil's XML — not its translated text) a Basler
             USB camera or CXP card with a driver problem: pylon cannot see
             those at all, so without this the scan just says "none found".
             The CXP card's PCIe slot (a slot slower than the card returns
             incomplete frames). A GenTL path changed since the app started
             (installed CXP support, app not restarted). The card's own
             state: applet, simulated cameras, power over CXP.
  Found      interface and family from pylon's DeviceInfo, WITHOUT opening.
             A missing DeviceInfo property is the string "N/A" (measured), so
             identity comes from to_dict(), never Get*().
  Usable?    Camera Link: pylon only configures it; images come through the
             grabber maker's software. 3D (blaze, Stereo ace): depth data,
             not frames. GigE on another subnet: pylon sees it, cannot open
             it — computed from the addresses, not guessed.
  Free       IsDeviceAccessibleInfo, guarded (a TL without it, or code 0,
             means "cannot say": probed anyway, never assumed fine).
  Opens      opened as it IS: pylon's default configuration is removed first,
             because it would switch trigger mode off on open and hide the
             very state being checked.
  State      trigger mode (every selector), acquisition mode, exposure mode,
             frame-rate limit, exposure longer than Typhon's 1 s read, link
             throughput limit, test pattern, binning, the startup user set,
             the pixel format Typhon can save, the grabber's pixel format and
             applet (CXP), the CXP link against the one the camera is made
             for, USB speed, the GigE stream driver and packet size.
  Test grab  a burst run exactly as Typhon runs the camera (pylon's
             AcquireContinuousConfiguration applied, as Typhon's open does),
             capped in TOTAL time — per-frame timeouts alone let one camera
             at 0.3 fps hold the wizard for 34 s (measured). Frames arrive,
             none fail, no buffer underruns, and the camera is RELEASED
             afterwards (Close + DestroyDevice; Close alone keeps it held).

NEVER RAISES. A check that cannot run says so. Everything is behind a guard,
per transport layer and per camera, because a scan that crashes on one odd
camera tells the user nothing about the others.

Measured on pypylon 26.8 / pylon 12.3 with the camera emulator; the rules for
real in-use, GigE subnet, CoaXPress and Camera Link behaviour come from
Basler's documentation (the emulator cannot show them).
"""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from council_core.cameras import _savable

#: Typhon's grab loop waits this long per read (CaptureSession.timeout_ms).
READ_TIMEOUT_MS = 1000
#: The test grab's whole budget once frames flow...
GRAB_SECONDS = 2.0
#: ...and the longest wait for any one frame.
FRAME_WAIT_CAP_MS = 3000
#: Frames further apart than this are not test-grabbed: skipped with a
#: warning, since the live view and a capture will be that slow too.
SLOW_PERIOD_MS = 2000
#: Frames in the test burst.
BURST = 10

#: Windows' IDs for Basler hardware: USB cameras (Basler AG), and the
#: CoaXPress interface cards (Silicon Software's PCI vendor ID; now Basler).
BASLER_USB_ID = "VID_2676"
BASLER_PCI_ID = "VEN_1AE8"
#: "CXP12_X2" -> 2 links at CXP-12.
CXP_LINK = re.compile(r"CXP(\d+)_X(\d+)", re.I)
#: A PCIe card's power over CXP is cut on these states.
CXP_POWER_TRIPPED = ("Tripped", "HighCurrent", "OverCurrent")

CXP_CLASS = "BaslerGTC/Basler/CXP"
CL_CLASS = "BaslerCameraLink"
EMU_CLASS = "BaslerCamEmu"

INTERFACES = {
    "BaslerUsb": "USB3 Vision",
    "BaslerGigE": "GigE Vision",
    CXP_CLASS: "CoaXPress",
    CL_CLASS: "Camera Link",
    EMU_CLASS: "Emulated",
    "BaslerGTC/Basler/U3V": "USB3 Vision (GenTL)",
    "BaslerGTC/Basler/GEV": "GigE Vision (GenTL)",
    "BaslerGTC/Basler/GenTL_Producer_for_Basler_blaze_101_cameras": "blaze (3D)",
    "BaslerGTC/Basler/basler_xw": "Stereo ace (3D)",
    "BaslerGTC/Basler/Stereo_mini": "Stereo mini (3D)",
    "BaslerIPCam": "IP camera",
}

#: Model-name prefix -> family, from Basler's per-model tables.
FAMILIES = [
    ("a2A", "ace 2"), ("acA", "ace"), ("boA", "boost"), ("daA", "dart"),
    ("dmA", "dart"), ("puA", "pulse"), ("r2L", "racer 2 (line scan)"),
    ("raL", "racer (line scan)"), ("ruL", "runner (line scan)"),
    ("spL", "sprint (line scan)"), ("scA", "scout"), ("piA", "pilot"),
    ("avA", "aviator"), ("beA", "beat"), ("blaze", "blaze (3D)"),
    ("Stereo", "Stereo ace (3D)"), ("Emulation", "emulator"),
    ("CamEmu", "emulator"),
]

ACCESS = {0: "cannot say", 1: "free", 2: "open in another program (read-only)",
          3: "in use by another program", 4: "not reachable"}

PASS, WARN, FAIL, INFO = "pass", "warn", "fail", "info"
WORKS, LIMITS, NO = "works", "works with limits", "will not work"


@dataclass
class Check:
    level: str
    label: str
    detail: str
    fix: str = ""


@dataclass
class CameraReport:
    key: str
    model: str
    family: str
    serial: str
    interface: str
    device_class: str
    access: str
    user_name: str = ""
    ip: str = ""
    checks: List[Check] = field(default_factory=list)
    capabilities: Dict[str, Any] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        if any(c.level == FAIL for c in self.checks):
            return NO
        if any(c.level == WARN for c in self.checks):
            return LIMITS
        return WORKS

    @property
    def label(self) -> str:
        who = self.user_name or self.serial or self.key
        return f"{self.model} ({who}) · {self.interface}"


@dataclass
class ScanReport:
    pylon_version: str = ""
    layers: List[str] = field(default_factory=list)
    cameras: List[CameraReport] = field(default_factory=list)
    #: Machine-level findings (no CoaXPress support, a card with no camera...).
    notes: List[Check] = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> str:
        if not self.cameras:
            return "No Basler camera found — see the notes."
        counts = {v: sum(c.verdict == v for c in self.cameras)
                  for v in (WORKS, LIMITS, NO)}
        bits = [f"{n} {v}" for v, n in counts.items() if n]
        return f"{len(self.cameras)} Basler camera(s): " + ", ".join(bits) + "."


# ======================================================================
# Guards
# ======================================================================
def _s(fn: Any, *args: Any) -> Any:
    """fn(*args), or None on any failure."""
    try:
        return fn(*args)
    except Exception:                                      # noqa: BLE001
        return None


def _props(info: Any) -> Dict[str, str]:
    """Only the DeviceInfo properties pylon actually filled in.

    Get*() on a missing property returns the string "N/A" (measured) —
    truthy, so `x or fallback` never falls back. to_dict() lists only what
    is there.
    """
    got = _s(info.to_dict) if hasattr(info, "to_dict") else None
    if isinstance(got, dict):
        return {str(k): str(v) for k, v in got.items() if str(v) != "N/A"}
    out: Dict[str, str] = {}
    for key in ("DeviceClass", "FullName", "FriendlyName", "ModelName",
                "SerialNumber", "VendorName", "UserDefinedName", "TLType",
                "InterfaceID", "IpAddress", "SubnetMask", "SubnetAddress",
                "Interface", "MacAddress", "DeviceVersion"):
        available = getattr(info, f"Is{key}Available", None)
        if callable(available) and not _s(available):
            continue
        value = _s(getattr(info, f"Get{key}", lambda: None))
        if value not in (None, "", "N/A"):
            out[key] = str(value)
    return out


def camera_key(props: Dict[str, str], fallback: str) -> str:
    """Serial if pylon gave one, else FullName — never "N/A", which every
    serial-less camera would share (reproduced)."""
    for name in ("SerialNumber", "FullName"):
        value = props.get(name, "").strip()
        if value and value != "N/A":
            return value
    return fallback


def family_of(model: str) -> str:
    for prefix, family in FAMILIES:
        if model.startswith(prefix):
            return family
    return "unrecognised model"


def same_subnet(camera_ip: str, mask: str, host_ip: str) -> Optional[bool]:
    """Is a GigE camera on the host network card's subnet? None if pylon did
    not give enough to tell — never guessed from which enumeration found it
    (a blaze on the right subnet is also only found by EnumerateAllDevices)."""
    try:
        net = ipaddress.IPv4Network(f"{camera_ip}/{mask}", strict=False)
        return ipaddress.IPv4Address(host_ip) in net
    except (ValueError, TypeError):
        return None


# ======================================================================
# Node access on an open camera
# ======================================================================
class _Nodes:
    def __init__(self, cam: Any, genicam: Any):
        self.cam = cam
        self.gen = genicam

    def node(self, name: str, nodemap: Any = None) -> Any:
        """The node, or None. An absent feature is a PlaceholderParameter
        (never None); IsAvailable tells them apart."""
        try:
            nm = nodemap if nodemap is not None else self.cam.GetNodeMap()
            n = nm.GetNode(name)
            return n if n is not None and self.gen.IsAvailable(n) else None
        except Exception:                                  # noqa: BLE001
            return None

    def get(self, name: str, default: Any = None, nodemap: Any = None) -> Any:
        n = self.node(name, nodemap)
        try:
            if n is not None and self.gen.IsReadable(n):
                return n.GetValue()
        except Exception:                                  # noqa: BLE001
            pass
        return default

    def first(self, *names: str) -> Tuple[Optional[str], Any]:
        for name in names:
            value = self.get(name)
            if value is not None:
                return name, value
        return None, None

    def range(self, name: Optional[str]) -> Optional[Tuple[float, float]]:
        n = self.node(name) if name else None
        try:
            if n is not None and self.gen.IsReadable(n):
                return (n.GetMin(), n.GetMax())
        except Exception:                                  # noqa: BLE001
            pass
        return None

    def entries(self, name: str) -> List[str]:
        n = self.node(name)
        try:
            return list(n.GetSymbolics()) if n is not None else []
        except Exception:                                  # noqa: BLE001
            return []

    def set(self, name: str, value: Any) -> bool:
        n = self.node(name)
        try:
            if n is None or not self.gen.IsWritable(n):
                return False
            n.SetValue(value)
            return True
        except Exception:                                  # noqa: BLE001
            return False


def saveable(pylon: Any, fmt: str) -> Tuple[str, str]:
    """("yes" | "warn" | "no", why) for one pixel format, by the path Typhon
    takes (BaslerDevice._pixels): grab.Array when it is savable, BGR swapped
    to RGB; otherwise pylon's ImageFormatConverter. Measured, not listed: a
    tiny image of that pixel type goes the same way — and the answers match
    real grabs for all 24 formats the emulator offers."""
    try:
        pt = pylon.PixelTypeMapper.GetPylonPixelTypeByName(fmt)
    except Exception:                                      # noqa: BLE001
        pt = None
    if pt in (None, 0, getattr(pylon, "PixelType_Undefined", 0)):
        return "no", "pylon does not know this format"
    if str(fmt).startswith("BiColor"):
        return "no", "bi-color data is not a viewable image"
    try:
        image = pylon.PylonImage()
        image.Reset(pt, 8, 2)
    except Exception as exc:                               # noqa: BLE001
        return "no", f"pylon cannot hold it as an image ({type(exc).__name__})"
    arr = _s(image.GetArray)
    if arr is not None and _savable(arr):
        if arr.ndim == 2:
            if _s(getattr(pylon, "IsBayer", None), pt):
                return "warn", ("saved as the raw colour mosaic in grey — "
                                "lossless, but the pictures are not in colour")
            packed = _s(getattr(pylon, "IsPacked", None), pt)
            return "yes", (f"{arr.dtype.name} PNG, lossless"
                           + (" (unpacked on the CPU)" if packed else ""))
        if _s(getattr(pylon, "IsBGR", None), pt):
            return "yes", "RGB PNG, lossless (Typhon puts red first as it saves)"
        return "yes", "RGB PNG, lossless"
    colour = bool(_s(getattr(pylon, "IsColorImage", None), pt))
    deep = (_s(getattr(pylon, "BitDepth", None), pt) or 8) > 8
    target = "RGB8packed" if colour else ("Mono16" if deep else "Mono8")
    try:
        converter = pylon.ImageFormatConverter()
        converter.OutputPixelFormat = getattr(pylon, "PixelType_" + target)
        out = converter.Convert(image).GetArray()
    except Exception:                                      # noqa: BLE001
        out = None
    if out is not None and _savable(out):
        return "warn", (f"converted to {target.replace('packed', '')} as each "
                        f"frame is saved — extra CPU per frame"
                        + ("; 16-bit colour loses its low bits"
                           if colour and deep else ""))
    return "no", ("neither saved nor converted: Typhon stops with an error "
                  "naming the format")


# ======================================================================
# The scan
# ======================================================================
def scan(pylon: Any = None, genicam: Any = None, *,
         held_keys: Iterable[str] = (), test_grab: bool = True,
         burst: int = BURST, on_open: Any = None,
         pnp: Optional[Callable[[List[str]], Optional[str]]] = None,
         gentl_changed: Optional[Callable[[], Optional[str]]] = None
         ) -> ScanReport:
    """Every Basler camera visible, and everything that decides whether it
    runs. `held_keys` are cameras Typhon itself has open: they are reported,
    not probed. `on_open(camera)` runs on each camera just after it is
    opened — for tests, to put a camera in a state (the emulator forgets its
    settings between opens). `pnp` (pnputil's XML for some arguments) and
    `gentl_changed` stand in for Windows in tests. Never raises."""
    began = time.monotonic()
    report = ScanReport()
    held = {str(k) for k in held_keys if k}
    try:
        if pylon is None or genicam is None:
            try:
                from pypylon import genicam as _genicam
                from pypylon import pylon as _pylon
            except Exception as exc:                       # noqa: BLE001
                report.notes.append(Check(
                    FAIL, "pypylon", f"cannot import pypylon: {exc}",
                    "Install it (setup step 3): pip install pypylon"))
                return report
            pylon = pylon or _pylon
            genicam = genicam or _genicam
        report.pylon_version = str(_s(getattr(pylon, "GetPylonVersionString",
                                              lambda: "")) or "")
        tlf = pylon.TlFactory.GetInstance()
        tls = _s(lambda: list(tlf.EnumerateTls() or [])) or []
        seen: Dict[str, CameraReport] = {}
        for tl_info in tls:
            _scan_layer(pylon, genicam, tlf, tl_info, report, seen, held,
                        test_grab, burst, on_open)
        report.cameras = list(seen.values())
    except Exception as exc:                               # noqa: BLE001
        report.notes.append(Check(FAIL, "Scan", f"{type(exc).__name__}: {exc}",
                                  "Scan again; if it persists, reinstall pylon."))
    # The PC is checked whatever pylon did: a camera with no driver is
    # exactly the one pylon cannot list.
    try:
        cards = _pc_checks(report, pnp or _pnputil)
        _machine_notes(report, cards, gentl_changed or _gentl_changed)
    except Exception as exc:                               # noqa: BLE001
        report.notes.append(Check(INFO, "This PC", f"not checked: {_first_line(exc)}"))
    report.seconds = time.monotonic() - began
    return report


def _machine_notes(report: ScanReport, cards: List[Dict[str, Any]],
                   gentl_changed: Callable[[], Optional[str]]) -> None:
    classes = set(report.layers)
    if report.layers and CXP_CLASS not in classes:
        report.notes.append(Check(
            FAIL if cards else WARN, "CoaXPress support",
            ("a CoaXPress card is fitted, but this pylon has no CoaXPress "
             "transport layer, so no camera on it can appear."
             if cards else
             "this pylon has no CoaXPress transport layer, so CoaXPress cameras "
             "(boost, including the boA5320-150cm; ace 2 CXP; racer 2 CXP) "
             "cannot appear."),
            "Install the pylon Software Suite with the CoaXPress (CXP) option "
            "and the CXP-12 interface card's driver (setup step 2); pip install "
            "pypylon alone never includes it. Not needed for USB or GigE "
            "cameras."))
        changed = _s(gentl_changed)
        if changed:
            report.notes.append(Check(
                WARN, "Restart needed",
                f"camera software was installed after this app started "
                f"({changed} is new on this PC's GenTL path).",
                "Close this app — and the terminal it was started from — and "
                "start it again: a program only sees the settings that existed "
                "when it started."))
    if not report.cameras:
        report.notes.append(Check(
            INFO, "Cameras", "pylon found no Basler camera.",
            "Plug the camera in (USB 3 port / GigE network card / CoaXPress "
            "card), close the pylon Viewer, and scan again."))


def _scan_layer(pylon: Any, genicam: Any, tlf: Any, tl_info: Any,
                report: ScanReport, seen: Dict[str, CameraReport],
                held: set, test_grab: bool, burst: int, on_open: Any) -> None:
    cls = str(_s(tl_info.GetDeviceClass) or "?")
    report.layers.append(cls)
    tl = _s(tlf.CreateTl, tl_info)
    if tl is None:
        return
    try:
        interfaces = [_props(i).get("InterfaceID", "?")
                      for i in (_s(lambda: list(tl.EnumerateInterfaces() or []))
                                or [])]
        found: List[Any] = list(_s(lambda: list(tl.EnumerateDevices() or [])) or [])
        # GigE: cameras on another subnet only appear here.
        if hasattr(tl, "EnumerateAllDevices"):
            names = {_props(d).get("FullName") for d in found}
            for d in _s(lambda: list(tl.EnumerateAllDevices() or [])) or []:
                if _props(d).get("FullName") not in names:
                    found.append(d)
        added = 0
        for info in found:
            try:
                cam = _camera(pylon, genicam, tlf, tl, info, cls, len(seen),
                              held, test_grab, burst, on_open)
            except Exception as exc:                       # noqa: BLE001
                # Never dropped: listed with what went wrong.
                p = _props(info)
                cam = CameraReport(
                    key=camera_key(p, f"#{len(seen) + 1}"),
                    model=p.get("ModelName", "?"),
                    family=family_of(p.get("ModelName", "")),
                    serial=p.get("SerialNumber", ""),
                    interface=INTERFACES.get(p.get("DeviceClass", cls), cls),
                    device_class=p.get("DeviceClass", cls), access="cannot say")
                cam.checks.append(Check(FAIL, "Examining", _first_line(exc),
                                        "Scan again; if it repeats, open the "
                                        "camera in the pylon Viewer."))
            if cam.key not in seen:
                seen[cam.key] = cam
                added += 1
        if cls == CXP_CLASS and interfaces and not added:
            report.notes.append(Check(
                WARN, "CoaXPress card",
                f"a CoaXPress card is installed ({', '.join(interfaces)}) but no "
                f"camera is on it.",
                "Check the CXP cables are seated and the camera has power "
                "(Power over CXP, or its own supply), wait ~5 s after power-on, "
                "and scan again."))
        if cls == CXP_CLASS:
            # After the cameras: each was opened, examined and released.
            report.notes.extend(_cxp_cards(tl, genicam, cameras_found=added))
    finally:
        _s(tlf.ReleaseTl, tl)


def _camera(pylon: Any, genicam: Any, tlf: Any, tl: Any, info: Any, cls: str,
            n: int, held: set, test_grab: bool, burst: int,
            on_open: Any = None) -> CameraReport:
    p = _props(info)
    device_class = p.get("DeviceClass", cls)
    model = p.get("ModelName", "?")
    key = camera_key(p, f"#{n + 1}")
    access_code = 0
    vanished = ""
    fn = getattr(tl, "IsDeviceAccessibleInfo", None)
    if callable(fn):
        try:
            got = fn(info)
        except Exception as exc:                           # noqa: BLE001
            # It looks the camera up again, and RAISES for one no longer
            # there (measured): unplugged since the list was made.
            got, vanished = None, _first_line(exc)
        if isinstance(got, (tuple, list)) and len(got) > 1:
            try:
                access_code = int(got[1])
            except (TypeError, ValueError):
                access_code = 0
    cam = CameraReport(
        key=key, model=model, family=family_of(model),
        serial=p.get("SerialNumber", ""),
        interface=INTERFACES.get(device_class, f"other ({device_class})"),
        device_class=device_class, access=ACCESS.get(access_code, str(access_code)),
        user_name=p.get("UserDefinedName", ""), ip=p.get("IpAddress", ""))
    checks = cam.checks
    checks.append(Check(PASS, "Found", f"{cam.interface}, {cam.family}"
                        + (f", firmware {p['DeviceVersion']}"
                           if p.get("DeviceVersion") else "")))

    # -- can it deliver frames to Typhon at all? --------------------------
    if device_class == CL_CLASS:
        checks.append(Check(
            FAIL, "Interface", "Camera Link: pylon can only configure this "
            "camera over its serial link; images come through the frame "
            "grabber maker's own software.",
            "Use the frame grabber's software for images."))
        return cam
    if "3D" in cam.interface or "3D" in cam.family:
        checks.append(Check(
            FAIL, "Camera type", "a 3D camera: it sends depth / point-cloud "
            "data, and Typhon records 2D images.",
            "Use Basler's pylon Supplementary Package for this camera."))
        return cam
    if cam.ip:
        ok = same_subnet(cam.ip, p.get("SubnetMask", ""), p.get("Interface", ""))
        if ok is False:
            checks.append(Check(
                FAIL, "Network", f"this GigE camera ({cam.ip}) is on a different "
                "subnet from the PC's network card, so it cannot be opened.",
                "Put both on the same subnet with the pylon IP Configurator, "
                "then scan again."))
            return cam
        checks.append(Check(PASS if ok else INFO, "Network",
                            f"camera at {cam.ip}"
                            + ("" if ok else " (subnet not reported)")))
        if cam.ip.startswith("169.254."):
            checks.append(Check(
                WARN, "IP address",
                f"{cam.ip} is an automatic (link-local) address: it can change "
                f"whenever the camera or the PC restarts.",
                "Give the camera a fixed (persistent) IP in the network card's "
                "subnet with the pylon IP Configurator."))

    # -- free? ------------------------------------------------------------
    if key in held:
        checks.append(Check(PASS, "In use by Typhon",
                            "connected in Typhon right now — not re-opened."))
        return cam
    if access_code == 3:
        checks.append(Check(
            FAIL, "Free", "in use by another program.",
            "Close the pylon Viewer or other camera software. A GigE camera "
            "held by a program that crashed frees itself after ~3 s."))
        return cam
    if access_code == 4:
        checks.append(Check(FAIL, "Free", "found, but not reachable.",
                            "Check the cable, power and driver, then scan again."))
        return cam
    if access_code == 2:
        checks.append(Check(WARN, "Free", "another program has it open.",
                            "Close the pylon Viewer or other camera software."))
    elif access_code == 0:
        checks.append(Check(
            INFO, "Free",
            (f"pylon could not find it again ({vanished}) — unplugged? Opening "
             f"it to find out." if vanished else
             "pylon could not say; opening it to find out.")))
    else:
        checks.append(Check(PASS, "Free", "no other program holds it"
                            + (" (the emulator cannot show this)"
                               if device_class == EMU_CLASS else "")))

    _probe(pylon, genicam, tlf, info, cam, test_grab, burst, on_open)
    return cam


# ======================================================================
# Open, examine, test-grab, release
# ======================================================================
def _probe(pylon: Any, genicam: Any, tlf: Any, info: Any, cam: CameraReport,
           test_grab: bool, burst: int, on_open: Any = None) -> None:
    checks = cam.checks
    try:
        camera = pylon.InstantCamera(tlf.CreateDevice(info))
    except Exception as exc:                               # noqa: BLE001
        checks.append(Check(FAIL, "Opens", _first_line(exc), _open_fix(cam)))
        return
    try:
        # AS IT IS: pylon's default configuration would turn trigger mode
        # off on Open and hide exactly what is being checked.
        _s(camera.RegisterConfiguration, None,
           pylon.RegistrationMode_ReplaceAll, pylon.Cleanup_None)
        began = time.monotonic()
        try:
            camera.Open()
        except Exception as exc:                           # noqa: BLE001
            checks.append(Check(FAIL, "Opens", _first_line(exc), _open_fix(cam)))
            return
        checks.append(Check(PASS, "Opens",
                            f"in {(time.monotonic() - began) * 1000:.0f} ms"))
        try:
            if callable(on_open):
                on_open(camera)
            nodes = _Nodes(camera, genicam)
            caps = _capabilities(pylon, nodes)
            cam.capabilities = caps
            checks.extend(_state(pylon, camera, nodes, caps, cam))
            if test_grab:
                checks.extend(_test_grab(pylon, camera, nodes, caps, cam, burst))
        except Exception as exc:                           # noqa: BLE001
            # The camera STAYS in the list with what went wrong: dropping it
            # would leave the user looking for a camera the scan never shows.
            checks.append(Check(FAIL, "Examining", _first_line(exc),
                                "Scan again; if it repeats, open the camera in "
                                "the pylon Viewer to see what it reports."))
    finally:
        for step in ("StopGrabbing", "Close", "DestroyDevice"):
            _s(getattr(camera, step))
        attached = _s(camera.IsPylonDeviceAttached)
        if attached:
            checks.append(Check(WARN, "Released",
                                "pylon still holds the camera after the scan.",
                                "Close this window before connecting in Typhon."))


def _first_line(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0] if text else ''}"[:220]


def _open_fix(cam: CameraReport) -> str:
    if cam.interface == "CoaXPress":
        return ("Close the pylon Viewer (a frame grabber serves one program), "
                "check the CXP card's driver, and scan again.")
    return "Close the pylon Viewer or other camera software, and scan again."


def _capabilities(pylon: Any, n: _Nodes) -> Dict[str, Any]:
    c: Dict[str, Any] = {}
    c["model"] = n.get("DeviceModelName")
    c["firmware"] = n.get("DeviceFirmwareVersion")
    c["scan_type"] = n.get("DeviceScanType")
    c["sensor"] = (n.get("SensorWidth"), n.get("SensorHeight"))
    c["max"] = (n.get("WidthMax"), n.get("HeightMax"))
    c["aoi"] = (n.get("Width"), n.get("Height"), n.get("OffsetX"),
                n.get("OffsetY"))
    c["pixel_format"] = n.get("PixelFormat")
    c["formats"] = {f: saveable(pylon, f) for f in n.entries("PixelFormat")}
    c["exposure_node"], c["exposure_us"] = n.first(
        "ExposureTime", "ExposureTimeAbs", "ExposureTimeRaw")
    c["exposure_range"] = n.range(c["exposure_node"])
    c["gain_node"], c["gain"] = n.first("Gain", "GainAbs", "GainRaw")
    c["gain_range"] = n.range(c["gain_node"])
    c["rate_enable"] = n.get("AcquisitionFrameRateEnable")
    c["rate_node"], c["rate"] = n.first("AcquisitionFrameRate",
                                        "AcquisitionFrameRateAbs")
    c["resulting_node"], c["resulting_fps"] = n.first(
        "BslResultingAcquisitionFrameRate", "ResultingFrameRate",
        "ResultingFrameRateAbs")
    link: Dict[str, Any] = {}
    for name in ("CxpLinkConfigurationStatus", "CxpLinkConfiguration",
                 "CxpLinkConfigurationPreferred", "BslUSBSpeedMode", "DeviceLinkSpeed", "GevLinkSpeed",
                 "GevSCPSPacketSize", "DeviceLinkThroughputLimitMode",
                 "DeviceLinkThroughputLimit", "PayloadSize"):
        value = n.get(name)
        if value is not None:
            link[name] = value
    c["link"] = link
    c["offsets_writable"] = all(
        n.node(a) is not None and _s(n.gen.IsWritable, n.node(a))
        for a in ("OffsetX", "OffsetY"))
    c["centred"] = [a for a in ("CenterX", "CenterY") if n.get(a)]
    c["temperature_state"] = n.first("BslTemperatureStatus",
                                     "TemperatureState")[1]
    return c


def _triggers(n: _Nodes) -> List[Tuple[str, str]]:
    """(selector, source) for every trigger selector whose mode is On."""
    on = []
    keep = n.get("TriggerSelector")
    for selector in n.entries("TriggerSelector") or [None]:
        if selector is not None and not n.set("TriggerSelector", selector):
            continue
        if n.get("TriggerMode") == "On":
            on.append((selector or "-", str(n.get("TriggerSource", "?"))))
    if keep is not None:
        n.set("TriggerSelector", keep)
    return on


def _state(pylon: Any, camera: Any, n: _Nodes, c: Dict[str, Any],
           cam: CameraReport) -> List[Check]:
    out: List[Check] = []
    emulated = cam.device_class == EMU_CLASS

    # -- the pixel format Typhon will save -----------------------------------
    fmt = c["pixel_format"]
    level, why = c["formats"].get(fmt, ("no", "not among the formats offered"))
    good = [f for f, (v, _) in c["formats"].items() if v == "yes"]
    out.append(Check({"yes": PASS, "warn": WARN, "no": FAIL}[level],
                     "Pixel format", f"{fmt}: {why}",
                     "" if level == "yes" else
                     f"Set PixelFormat to one of: {', '.join(good) or 'none offered'}"
                     " (pylon Viewer), then scan again."))

    # -- controls Typhon uses ---------------------------------------------------
    exp_node, exp = c["exposure_node"], c["exposure_us"]
    if exp_node is None:
        out.append(Check(WARN, "Exposure control",
                         "this camera reports no exposure setting.",
                         "Typhon's exposure box will do nothing here."))
    else:
        rng = c["exposure_range"]
        span = f" (range {rng[0]:g}–{rng[1]:g})" if rng else ""
        long = exp is not None and exp / 1000.0 > READ_TIMEOUT_MS
        out.append(Check(
            WARN if long else PASS, "Exposure control",
            f"{exp_node} = {exp:g}{span}"
            + (" — longer than Typhon's 1 s read: frames arrive that rarely and "
               "the status line shows ~0 fps." if long else ""),
            "Shorten the exposure in Typhon." if long else ""))
    emode = n.get("ExposureMode")
    if emode not in (None, "Timed"):
        out.append(Check(
            WARN, "Exposure mode",
            f"ExposureMode = {emode}: the exposure is set by a trigger signal, "
            f"not by time, so a free-running camera's exposure is not what "
            f"Typhon shows.",
            "Enter an exposure in Typhon (it sets Timed), or set ExposureMode "
            "to Timed in the pylon Viewer and save it to the startup user set."))
    auto = n.get("ExposureAuto")
    if auto not in (None, "Off"):
        out.append(Check(INFO, "Automatic exposure",
                         f"ExposureAuto = {auto}: the camera picks its own "
                         f"exposure until one is entered in Typhon (which "
                         f"turns this off)."))
    gain_node = c["gain_node"]
    if gain_node is None:
        out.append(Check(INFO, "Gain control", "this camera reports no gain."))
    else:
        rng = c["gain_range"]
        unit = "raw units" if gain_node == "GainRaw" else "dB"
        out.append(Check(PASS, "Gain control",
                         f"{gain_node} = {c['gain']:g} {unit}"
                         + (f" (range {rng[0]:g}–{rng[1]:g})" if rng else "")))

    # -- will frames arrive? ---------------------------------------------------
    trig = _triggers(n)
    c["_stall"] = bool(trig)
    if trig:
        what = ", ".join(f"{s} from {src}" for s, src in trig)
        out.append(Check(
            WARN, "Trigger mode",
            f"the camera waits for a trigger ({what}). Typhon switches trigger "
            f"mode off when it connects, so it will free-run.",
            "Nothing needed for Typhon. To stop it coming back after a power "
            "cycle: set TriggerMode Off and save it to the startup user set."))
    else:
        out.append(Check(PASS, "Trigger mode", "off (free-running)"))
    mode = n.get("AcquisitionMode")
    if mode not in (None, "Continuous"):
        c["_stall"] = True
        out.append(Check(INFO, "Acquisition mode",
                         f"{mode}: one frame, then nothing. Typhon sets "
                         f"Continuous when it connects."))
    if c["rate_enable"] and c["rate"]:
        period_ms = 1000.0 / c["rate"]
        binding = bool(c["resulting_fps"]) and abs(
            c["resulting_fps"] - c["rate"]) <= 0.01 * c["rate"]
        slow = period_ms > READ_TIMEOUT_MS
        out.append(Check(
            WARN if slow else INFO, "Frame-rate limit",
            f"{c['rate_node']} = {c['rate']:g} fps is on"
            + (" — this limit is the camera's rate now." if binding else ".")
            + (" Slower than one frame a second: the live view will look dead."
               if slow else ""),
            "Raise it with Typhon's Frame rate box, or set 0 for as fast as the "
            "camera goes." if slow else ""))
    if c["resulting_fps"]:
        out.append(Check(INFO, "Maximum frame rate",
                         f"the camera expects {c['resulting_fps']:.1f} fps at "
                         f"this area and pixel format"))

    # -- the link ---------------------------------------------------------------
    link = c["link"]
    speed = link.get("BslUSBSpeedMode")
    if speed is not None:
        out.append(Check(
            PASS if speed == "SuperSpeed" else WARN, "USB link",
            f"{speed}" + ("" if speed == "SuperSpeed" else
                          " — USB 2 speed, so the frame rate is limited."),
            "" if speed == "SuperSpeed" else
            "Use a USB 3 port (on its own controller) and a USB 3 cable."))
    cxp = link.get("CxpLinkConfigurationStatus") or link.get("CxpLinkConfiguration")
    if cxp is not None:
        out.append(cxp_link_check(str(cxp), link.get("CxpLinkConfigurationPreferred"),
                                  cam.model))
    if link.get("DeviceLinkThroughputLimitMode") == "On":
        limit, payload = link.get("DeviceLinkThroughputLimit"), link.get("PayloadSize")
        if limit and payload:
            cap = limit / payload
            binding = bool(c["resulting_fps"]) and cap <= c["resulting_fps"] * 1.01
            out.append(Check(
                WARN if binding else INFO, "Link throughput limit",
                f"{limit / 1e6:.0f} MB/s over {payload / 1e6:.2f} MB frames — at "
                f"most {cap:.0f} fps at this area and pixel format.",
                "Set DeviceLinkThroughputLimitMode Off if this camera has the "
                "link to itself." if binding else ""))
    if cam.interface.startswith("GigE"):
        grabber = _s(camera.GetStreamGrabberNodeMap)
        kind = n.get("Type", nodemap=grabber) if grabber is not None else None
        if kind == "NoDriverAvailable":
            out.append(Check(
                FAIL, "GigE driver", "pylon has no GigE stream driver for this "
                "network card.",
                "Install the pylon Software Suite with the GigE Vision Filter "
                "Driver (or the Performance Driver for Intel network cards)."))
        elif kind == "SocketDriver":
            out.append(Check(
                WARN, "GigE driver", "Windows' socket driver: more CPU per "
                "frame, and frames are lost at high rates.",
                "Install pylon's GigE Vision Filter Driver and enable it for "
                "this network card."))
        elif kind:
            out.append(Check(PASS, "GigE driver", str(kind)))
    if "GevSCPSPacketSize" in link and cam.interface.startswith("GigE"):
        size = link["GevSCPSPacketSize"]
        out.append(Check(
            PASS if size and size > 1500 else WARN, "GigE packet size",
            f"{size} bytes" + ("" if size and size > 1500 else
                               " — standard frames; large images need many "
                               "packets and lose frames under load."),
            "" if size and size > 1500 else
            "Enable jumbo frames on the PC's network card and raise "
            "GevSCPSPacketSize (e.g. 8192)."))

    # -- things that change the picture -------------------------------------------
    tp_node, tp = n.first("TestPattern", "TestImageSelector")
    if tp not in (None, "Off"):
        if emulated:
            out.append(Check(INFO, "Test pattern",
                             f"{tp_node} = {tp} (the emulator's normal image)"))
        else:
            out.append(Check(
                FAIL, "Test pattern",
                f"{tp_node} = {tp}: pictures are generated inside the camera — "
                f"a capture would save a pattern, not the scene.",
                f"Set {tp_node} to Off."))
    active = {name: v for name in ("BinningHorizontal", "BinningVertical",
                                   "DecimationHorizontal", "DecimationVertical")
              if (v := n.get(name)) not in (None, 1)}
    if active:
        out.append(Check(
            WARN, "Binning / decimation",
            ", ".join(f"{k} {v}" for k, v in active.items())
            + ": pictures are downsampled.",
            "Set them to 1 for full resolution."))
    if not c["offsets_writable"]:
        out.append(Check(
            WARN, "Area of interest",
            "the camera centres the area"
            + (f" ({'/'.join(c['centred'])} on)" if c["centred"] else "")
            + ": it can be resized but not moved.",
            "Turn CenterX / CenterY off in the pylon Viewer to move it."
            if c["centred"] else ""))
    us_node, us = n.first("UserSetDefault", "UserSetDefaultSelector")
    if us not in (None, "Default"):
        out.append(Check(
            WARN, "Startup settings",
            f"{us_node} = {us}: every power-up reloads that set's trigger, "
            f"frame-rate, test-pattern and binning settings.",
            f"Set {us_node} to Default, or fix those settings and save them "
            f"into {us}."))

    # -- the frame grabber (CoaXPress) --------------------------------------------
    tl_nodes = _s(camera.GetTLNodeMap)
    grabber_fmt = n.get("PixelFormat", nodemap=tl_nodes) if tl_nodes is not None else None
    if grabber_fmt is not None and grabber_fmt != fmt:
        auto = n.get("AutomaticFormatControl", nodemap=tl_nodes)
        if auto is False or auto is None:
            out.append(Check(
                FAIL, "Frame grabber format",
                f"the interface card is set to {grabber_fmt}, the camera to {fmt}.",
                "Set the same pixel format on the card (Device Transport Layer) "
                "and the camera, or turn AutomaticFormatControl back on."))
    applet = (n.get("InterfaceApplet", nodemap=tl_nodes)
              if tl_nodes is not None else None)
    if applet:
        want = CXP_LINK.search(str(link.get("CxpLinkConfigurationPreferred") or ""))
        links = int(want.group(2)) if want else 1
        if links >= 2 and "Dual" in str(applet):
            out.append(Check(
                WARN, "Card applet",
                f"{applet} gives each camera ONE link; this camera is made for "
                f"{links}.",
                "Set InterfaceApplet to Acq_SingleCXP12Area (pylon Viewer: the "
                "camera's Device Transport Layer), then scan again."))
        else:
            out.append(Check(INFO, "Card applet", str(applet)))
    temp = c["temperature_state"]
    if temp not in (None, "Ok", "OK", "Normal"):
        out.append(Check(WARN, "Temperature", f"the camera reports {temp}.",
                         "Improve cooling / airflow around the camera."))
    return out


def _burst(pylon: Any, camera: Any, n: _Nodes, count: int,
           timeout_ms: int, seconds: float = GRAB_SECONDS) -> Dict[str, Any]:
    """Up to `count` frames, but never past `seconds` once frames flow: the
    timeout bounds each frame, not the burst (34 s at 0.3 fps, measured)."""
    res: Dict[str, Any] = {"ok": 0, "failed": [], "timeouts": 0,
                           "first_ms": None, "waiting": None, "array": None,
                           "underruns": None}
    stamps: List[float] = []
    began = time.monotonic()
    camera.StartGrabbingMax(count, pylon.GrabStrategy_OneByOne)
    try:
        while res["ok"] + len(res["failed"]) < count:
            if (res["first_ms"] is not None
                    and time.monotonic() - began > seconds):
                break
            # The FIRST frame gets the full cap: a stream starting up (or a
            # busy PC) can take over a second, and Typhon's own loop simply
            # waits again — a 1 s first wait failed cameras Typhon would run.
            wait = FRAME_WAIT_CAP_MS if res["first_ms"] is None else timeout_ms
            grab = camera.RetrieveResult(int(wait),
                                         pylon.TimeoutHandling_Return)
            try:
                if not grab.IsValid():             # a timeout, never None
                    res["timeouts"] += 1
                    # Asked WHILE grabbing: after StopGrabbing it reads False.
                    if n.set("AcquisitionStatusSelector", "FrameTriggerWait"):
                        res["waiting"] = n.get("AcquisitionStatus")
                    break
                if res["first_ms"] is None:
                    res["first_ms"] = (time.monotonic() - began) * 1000
                if grab.GrabSucceeded():
                    res["ok"] += 1
                    stamps.append(time.monotonic())
                    if res["array"] is None:
                        arr = grab.Array
                        res["array"] = f"{arr.dtype.name}{list(arr.shape)}"
                else:
                    res["failed"].append((_s(grab.GetErrorCode) or 0,
                                          str(_s(grab.GetErrorDescription) or "")))
            finally:
                _s(grab.Release)
    finally:
        _s(camera.StopGrabbing)
    res["seconds"] = time.monotonic() - began
    res["fps"] = ((len(stamps) - 1) / (stamps[-1] - stamps[0])
                  if len(stamps) > 2 and stamps[-1] > stamps[0] else None)
    grabber = _s(camera.GetStreamGrabberNodeMap)
    if grabber is not None:
        # Frames the camera sent while pylon had no buffer free: lost.
        res["underruns"] = n.get("Statistic_Buffer_Underrun_Count",
                                 nodemap=grabber)
    return res


def _test_grab(pylon: Any, camera: Any, n: _Nodes, c: Dict[str, Any],
               cam: CameraReport, count: int) -> List[Check]:
    out: List[Check] = []
    period = max((c["exposure_us"] or 0) / 1000.0 if c["exposure_node"] != "ExposureTimeRaw" else 0.0,
                 1000.0 / c["rate"] if c["rate_enable"] and c["rate"] else 0.0,
                 1000.0 / c["resulting_fps"] if c["resulting_fps"] else 0.0)
    if period > SLOW_PERIOD_MS:
        return [Check(WARN, "Test grab",
                      f"skipped: frames come {period / 1000:.1f} s apart, so "
                      f"the live view and a capture will be that slow too.",
                      "Raise the frame-rate limit (Typhon's Frame rate box) or "
                      "shorten the exposure.")]
    timeout = int(max(READ_TIMEOUT_MS, min(FRAME_WAIT_CAP_MS, 2 * period + 500)))
    try:
        if c.get("_stall"):
            # Typhon's connect applies pylon's default configuration (trigger
            # mode off, continuous acquisition); test the camera as it will run.
            config = getattr(pylon, "AcquireContinuousConfiguration", None)
            apply = getattr(config, "ApplyConfiguration", None)
            if callable(apply):
                apply(camera.GetNodeMap())
        res = _burst(pylon, camera, n, count, timeout)
    except Exception as exc:                               # noqa: BLE001
        return [Check(FAIL, "Test grab", _first_line(exc), _grab_fix(cam))]
    if not res["ok"]:
        why = ("the camera is waiting for a trigger" if res["waiting"] else
               f"{len(res['failed'])} failed frame(s)" if res["failed"] else
               "no frame and no reason given")
        out.append(Check(FAIL, "Test grab",
                         f"no good frame within {FRAME_WAIT_CAP_MS} ms: {why}.",
                         _grab_fix(cam)))
        return out
    detail = (f"{res['ok']} frame(s) in {res['seconds']:.1f} s, first after "
              f"{res['first_ms']:.0f} ms, {res['array']}")
    if res["fps"]:
        detail += f", {res['fps']:.1f} fps measured"
    if res["underruns"]:
        out.append(Check(
            WARN, "Lost buffers",
            f"{res['underruns']} frame(s) arrived with no buffer free and were "
            f"lost during the test grab.",
            "Close other programs using the CPU or the link; for GigE raise the "
            "packet size, for CoaXPress check the card's PCIe slot."))
    if res["failed"]:
        code, desc = res["failed"][0]
        out.append(Check(WARN if res["ok"] > len(res["failed"]) else FAIL,
                         "Test grab", f"{detail}; {len(res['failed'])} FAILED "
                         f"(0x{int(code) & 0xFFFFFFFF:08X} {desc})", _grab_fix(cam)))
    else:
        out.append(Check(PASS, "Test grab", detail))
    return out


# ======================================================================
# CoaXPress
# ======================================================================
def cxp_link_check(status: str, preferred: Any, model: str) -> Check:
    """The link the camera trained at against the one it is made for. Fewer
    links (a cable out, a one-port card) or a slower speed means fewer frames
    per second — measured by Basler for the boA5320-150 on its model page."""
    got, want = CXP_LINK.search(status or ""), CXP_LINK.search(str(preferred or ""))
    if not got:
        return Check(INFO, "CoaXPress link", str(status))
    speed, links = int(got.group(1)), int(got.group(2))
    here = f"{links} link(s) at CXP-{speed}"
    if not want:
        return Check(INFO, "CoaXPress link", here)
    wspeed, wlinks = int(want.group(1)), int(want.group(2))
    if links >= wlinks and speed >= wspeed:
        return Check(PASS, "CoaXPress link", f"{here}, as the camera is made for")
    detail = (f"{here}, but the camera is made for {wlinks} at CXP-{wspeed}: "
              f"fewer frames per second.")
    if str(model).startswith("boA5320-150"):
        detail += (" Full frame: 149.81 fps on 2 links; 75.71 on 1 link with "
                   "the camera's own power; 38.09 on 1 link powered over CXP.")
    return Check(WARN, "CoaXPress link", detail,
                 "Connect every CXP cable (camera connector 1 to card port 0 "
                 "first), use cables rated for this speed, and use a card with "
                 "enough ports — a 1-port card is one link. Then power-cycle "
                 "the camera and scan again.")


def _cxp_cards(tl: Any, genicam: Any, cameras_found: int) -> List[Check]:
    """Each CoaXPress card's own state, read from its interface node map:
    over-current, simulated cameras, power. Names from Basler's CXP-12
    interface documentation — UNTESTED on a card (none here), so every read
    is guarded and a missing node says nothing."""
    out: List[Check] = []
    nodes = _Nodes(None, genicam)
    for info in _s(lambda: list(tl.EnumerateInterfaces() or [])) or []:
        name = _props(info).get("InterfaceID", "CoaXPress card")
        itf = _s(tl.CreateInterface, info)
        if itf is None:
            continue
        try:
            try:
                itf.Open()
            except Exception:                              # noqa: BLE001
                continue
            nm = _s(itf.GetNodeMap)
            if nm is None:
                continue
            ports = {}
            for i in range(4):
                state = nodes.get(f"CxpPort{i}PowerState", nodemap=nm)
                if state is not None:
                    ports[i] = str(state)
            tripped = [i for i, st in ports.items() if st in CXP_POWER_TRIPPED]
            if tripped:
                out.append(Check(
                    FAIL, "CoaXPress power",
                    f"{name}: power over CXP was cut on port(s) "
                    f"{', '.join(map(str, tripped))} (over-current).",
                    "Check that cable and the camera's connector for damage, "
                    "then switch the PC off and on."))
            discovery = nodes.get("DiscoveryMethod", nodemap=nm)
            if discovery is not None and "Emulation" in str(discovery):
                out.append(Check(
                    WARN, "CoaXPress card",
                    f"{name}: DiscoveryMethod = {discovery} — the card lists "
                    f"SIMULATED cameras.",
                    "Set DiscoveryMethod to CameraDiscovery (pylon Viewer, the "
                    "card's interface), then scan again."))
            external = nodes.get("ExternalPowerPresent", nodemap=nm)
            if external is False:
                out.append(Check(
                    INFO if cameras_found else WARN, "CoaXPress power",
                    f"{name}: the card's 6-pin PCIe power is not connected, so "
                    f"it cannot power a camera over CXP.",
                    "Plug the PC's 6-pin PCIe power cable into the card, or "
                    "power the camera from its own supply."))
            bits = [f"port {i} {st}" for i, st in ports.items()]
            applet = nodes.get("InterfaceApplet", nodemap=nm)
            if applet:
                bits.insert(0, f"applet {applet}")
            if bits:
                out.append(Check(INFO, "CoaXPress card", f"{name}: " + ", ".join(bits)))
        finally:
            _s(itf.Close)
            _s(getattr(tl, "DestroyInterface", None), itf)
    return out


# ======================================================================
# The PC (Windows): drivers, the card's slot, a changed GenTL path
# ======================================================================
def _pnputil(args: List[str]) -> Optional[str]:
    """pnputil's XML for these arguments — not its text, whose labels are
    translated — or None when Windows cannot say: not Windows, an older
    pnputil without /format (it prints its usage instead), anything else.
    30-70 ms a call (measured). No console window flashes up."""
    if sys.platform != "win32":
        return None
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "pnputil.exe")
    try:
        done = subprocess.run(
            [exe if os.path.exists(exe) else "pnputil", *args, "/format", "xml"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:                                      # noqa: BLE001
        return None
    text = done.stdout.decode("utf-8", "replace")
    return text if "<PnpUtil" in text else None


def pnp_devices(xml_text: str) -> List[Dict[str, Any]]:
    """pnputil's XML as {id, name, status, problem, props}. A device's
    properties are lists of values (DEVPKEY name -> [text])."""
    start = xml_text.find("<PnpUtil")
    root = ET.fromstring(xml_text[start:] if start >= 0 else xml_text)
    out = []
    for dev in root.iter("Device"):
        fields = {child.tag: (child.text or "").strip() for child in dev
                  if child.tag != "Properties"}
        props = {p.get("Key", ""): [(v.text or "").strip() for v in p.iter("Value")]
                 for p in dev.iter("Property")}
        out.append({
            "id": dev.get("InstanceId", ""),
            "name": fields.get("DeviceDescription", ""),
            "status": fields.get("Status", ""),
            "problem": next((v for k, v in fields.items()
                             if "Problem" in k and v), ""),
            "props": props})
    return out


def pcie_check(card: Dict[str, Any]) -> Check:
    """A card in a slot slower than itself (fewer lanes, older generation)
    cannot move full frames fast enough; they come back incomplete."""
    def num(key: str) -> Optional[int]:
        try:
            return int(card["props"].get(f"DEVPKEY_PciDevice_{key}", [""])[0], 0)
        except (ValueError, IndexError, TypeError):
            return None

    name = card.get("name") or "CoaXPress card"
    speed, width = num("CurrentLinkSpeed"), num("CurrentLinkWidth")
    top_speed, top_width = num("MaxLinkSpeed"), num("MaxLinkWidth")
    if speed is None or width is None:
        return Check(INFO, "CoaXPress card", f"{name} (PCIe link not reported)")
    here = f"PCIe Gen{speed} x{width}"
    if top_speed and top_width and (speed < top_speed or width < top_width):
        return Check(
            WARN, "CoaXPress card",
            f"{name} runs at {here}, but it is made for Gen{top_speed} "
            f"x{top_width}: the slot is slower than the card, so fast full "
            f"frames can come back incomplete.",
            f"With the PC off, move the card to a slot wired for x{top_width} "
            f"(usually one connected to the CPU — see the motherboard manual).")
    return Check(PASS, "CoaXPress card", f"{name}, {here}")


def _pc_checks(report: ScanReport,
               pnp: Callable[[List[str]], Optional[str]]) -> List[Dict[str, Any]]:
    """Basler devices Windows has a driver problem with (pylon cannot see
    them at all), and the CoaXPress cards with their PCIe slot. Returns the
    cards found."""
    if sys.platform != "win32" and pnp is _pnputil:
        return []
    problems = pnp(["/enum-devices", "/connected", "/problem"])
    if problems is None:
        report.notes.append(Check(INFO, "Windows drivers",
                                  "Windows could not say (pnputil)."))
    else:
        bad = [d for d in pnp_devices(problems)
               if BASLER_USB_ID in d["id"].upper() or BASLER_PCI_ID in d["id"].upper()]
        for d in bad:
            usb = BASLER_USB_ID in d["id"].upper()
            report.notes.append(Check(
                FAIL, "Windows driver",
                f"{d['name'] or d['id']}: Windows reports a problem"
                + (f" (code {d['problem']})" if d["problem"] else "")
                + " — pylon cannot use it.",
                ("The camera is plugged in but has no working driver: install "
                 "the pylon Software Suite with USB support (pylon USB Camera "
                 "Driver), then unplug and replug the camera." if usb else
                 "The CoaXPress card's driver is not running: reinstall pylon "
                 "with CXP support, then restart the PC fully (Restart, not "
                 "Shut down — Fast Startup keeps the old driver state).")))
        if not bad:
            report.notes.append(Check(PASS, "Windows drivers",
                                      "no Basler device has a driver problem"))
    listed = pnp(["/enum-devices", "/connected", "/deviceid",
                  "PCI\\" + BASLER_PCI_ID, "/properties"])
    cards = pnp_devices(listed) if listed else []
    for card in cards:
        report.notes.append(pcie_check(card))
    return cards


def _gentl_changed() -> Optional[str]:
    """A folder on the machine's GENICAM_GENTL64_PATH that this process does
    not have — camera software installed after the app started. Programs
    only get the environment that existed when they started."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager"
                            r"\Environment") as key:
            machine, _ = winreg.QueryValueEx(key, "GENICAM_GENTL64_PATH")
    except Exception:                                      # noqa: BLE001
        return None

    def folders(value: str) -> List[str]:
        return [os.path.normcase(os.path.expandvars(p.strip()).rstrip("\\/"))
                for p in str(value).split(os.pathsep) if p.strip()]

    here = set(folders(os.environ.get("GENICAM_GENTL64_PATH", "")))
    new = [p for p in folders(machine) if p not in here]
    return new[0] if new else None


def _grab_fix(cam: CameraReport) -> str:
    if "GigE" in cam.interface:
        return ("Incomplete GigE frames: enable jumbo frames on the network card "
                "and raise GevSCPSPacketSize, or raise GevSCPD (inter-packet "
                "delay).")
    if "USB" in cam.interface:
        return ("Incomplete USB frames: use a USB 3 port on its own controller and "
                "a Basler USB 3 cable.")
    if cam.interface == "Emulated":
        return "Emulator: failures here are injected on purpose."
    return ("CoaXPress: check every CXP cable is seated, the link configuration "
            "matches the cables, and the card's applet suits this camera.")
