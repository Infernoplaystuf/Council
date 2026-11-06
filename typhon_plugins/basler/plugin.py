"""
typhon.basler — Basler cameras: finds, opens and describes Basler cameras
through pypylon (docs/typhon_plugins_design.md 2.3).

STAGE P2: AN ADAPTER over code that stays where it is until stage P7 —
council_core/cameras.py (BaslerBackend, BaslerDevice) and
council_core/camera_settings.py (BaslerSettings, which the device hands out
as its settings; its other triggers reach the generic stop check through
the Provider.extra_stops hook) — reached through typhon_plugin_api.builtin,
so the plugin imports only typhon_plugin_api (design 7.2).

ITS SET-UP (stage P2 row 4; design 2.3, 2.4 item 15)
The Basler family's check box in a "Typhon (plugins)" project's set-up
wizard: its install guide and checks (council_core/camera_setup.py's Basler
guide and checks, which do not move) and the wizard's scan page
(council_core/basler_scan.py) — told which cameras the app holds open, and
as which camera of the rig, so it never opens them again.

pypylon is NOT in the manifest's `python`: a PC without it still runs the
plugin, whose `available()` says what to install — as the scan always has.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

import typhon_plugin_api as api
from typhon_plugin_api import builtin

#: The settings groups a Basler describes (BaslerSettings, and the capture
#: set-up's "Capture").
GROUPS = ("Exposure", "Gain", "Frame rate", "Trigger", "Capture", "Image",
          "Binning", "Status")


class BaslerPlugin:
    """Basler cameras (camera plugin)."""

    #: Its cameras' backend name, as presets key them (design 3.3).
    backend = "basler"

    #: The SDK module to use instead of pypylon's (a test's fake pylon);
    #: None is pypylon itself, imported on first use.
    sdk: Any = None

    #: The scan page's pylon and genicam modules (a test's fakes), or None:
    #: pypylon's own, imported by the scan.
    scan_sdk: Optional[Tuple[Any, Any]] = None

    #: The words on the wizard's scan button, and what it does.
    setup_scan_label = "Scan Basler cameras"
    setup_scan_tip = ("Every Basler camera this PC can see, and whether each "
                      "one will run: interface, drivers, link, pixel format, "
                      "trigger mode, and a short test grab.")

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None

    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx

    def deactivate(self) -> None:
        self.ctx = None

    def _backend(self) -> Any:
        return builtin.BaslerBackend(self.sdk)

    # -- the camera plugin (design 3.3) ------------------------------------
    def available(self) -> Tuple[bool, str]:
        backend = self._backend()
        ok = bool(backend.available())
        return ok, "" if ok else backend.install_hint

    def discover(self) -> List[Any]:
        return self._backend().discover()

    def open(self, info: Any) -> Any:
        if getattr(info, "backend", "") != self.backend:
            raise api.CameraError(f"{getattr(info, 'label', info)} is not a "
                                  f"Basler camera")
        return self._backend().open(info)

    def capabilities(self, device: Any) -> FrozenSet[str]:
        """What the opened camera says of itself, and what it confirms of
        the manifest's trigger and binning capabilities by its own nodes:
        a software trigger it can be armed for (trigger_problem for an exact
        Software run says nothing), a hardware input among its trigger
        sources (capture_options), and binning (can_bin(2) — pylon's
        emulator accepts binning and does nothing, so the run record says
        what was done, design 3.4)."""
        caps = set(api.device_capabilities(device))
        try:
            setup = api.CaptureSetup(mode=api.MODE_EXACT,
                                     source=api.SOFTWARE)
            if not device.trigger_problem(setup):
                caps.add("software_trigger")
        except Exception:                                   # noqa: BLE001
            pass
        try:
            options = device.capture_options()
            if options is not None and getattr(options, "hardware", ()):
                caps.add("hardware_trigger")
        except Exception:                                   # noqa: BLE001
            pass
        can_bin = getattr(device, "can_bin", None)
        try:
            if callable(can_bin) and can_bin(2):
                caps.add("binning")
        except Exception:                                   # noqa: BLE001
            pass
        return frozenset(caps)

    def facet(self, device: Any, name: str) -> Any:
        """A Basler's three facets, each today's BaslerDevice methods
        (typhon_plugin_api.builtin.device_facet): its software trigger
        (TriggerSoftware on the entry the run armed), its hardware trigger
        (its input lines and CoaXPress triggers, the edge counter) and its
        binning (Average only: Downsample's camera half, design 2.1). Given
        for every Basler: whether this camera can do each is its confirmed
        capabilities, and the facet's own check says why not (`problem`,
        `can_bin`), as today's camera code says it."""
        if device is not None and name in ("software_trigger",
                                           "hardware_trigger", "binning"):
            return builtin.device_facet(device, name)
        return None

    def categories(self) -> api.CategoryHints:
        return builtin.hints_for(GROUPS)

    def record_fields(self, device: Any) -> Dict[str, Any]:
        """Nothing beyond today's camera record; wired at stage P3."""
        return {}

    def review_source(self) -> Optional[api.ReviewSource]:
        """A Basler's saved frames ARE its data: no file of its own."""
        return None

    # -- its set-up (CameraSetup; design 2.4 item 15) ----------------------
    @property
    def setup_label(self) -> str:
        return builtin.setup_label(self.backend)

    def setup_guide(self, python: str) -> api.Guide:
        """pylon with CoaXPress, pypylon into THIS app's Python, the pylon
        Viewer, and the emulator for trying it without hardware."""
        return builtin.setup_guide(self.backend, python=python)

    def setup_checks(self) -> api.Readiness:
        """pypylon importable, CoaXPress support, a camera connected."""
        return builtin.setup_check(self.backend, basler_sdk=self.sdk)

    def setup_scan(self, held: Mapping[str, str]) -> Any:
        """The scan page: every Basler this PC can see and whether each one
        will run (council_core.basler_scan.scan). A camera this app holds
        open — `held` maps its key to its name in the rig — is reported,
        not opened again, and the report says which camera it is."""
        names = {str(k): str(v) for k, v in dict(held or {}).items() if k}
        pylon, genicam = self.scan_sdk or (None, None)
        report = builtin.basler_scan(pylon, genicam, held_keys=list(names))
        for cam in getattr(report, "cameras", ()) or ():
            name = names.get(str(getattr(cam, "key", "")))
            if not name:
                continue
            for check in getattr(cam, "checks", ()) or ():
                if getattr(check, "label", "") == IN_USE:
                    check.detail = (f"in use as {name} — connected in Typhon "
                                    f"right now, not re-opened.")
        return report


#: The scan's line for a camera Typhon holds open (basler_scan).
IN_USE = "In use by Typhon"
