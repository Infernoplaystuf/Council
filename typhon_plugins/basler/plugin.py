"""
typhon.basler — Basler cameras: finds, opens and describes Basler cameras
through pypylon (docs/typhon_plugins_design.md 2.3).

STAGE P2: AN ADAPTER over code that stays where it is until stage P7 —
council_core/cameras.py (BaslerBackend, BaslerDevice) and
council_core/camera_settings.py (BaslerSettings, which the device hands out
as its settings; its other triggers reach the generic stop check through
the Provider.extra_stops hook) — reached through typhon_plugin_api.builtin,
so the plugin imports only typhon_plugin_api (design 7.2). The set-up
guide, its checks and the wizard's scan page join with the wizard's camera
pages (stage P2 row 4).

pypylon is NOT in the manifest's `python`: a PC without it still runs the
plugin, whose `available()` says what to install — as the scan always has.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Tuple

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
            setup = builtin.CaptureSetup(mode=builtin.MODE_EXACT,
                                         source=builtin.SOFTWARE)
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
        """None until the facets arrive at stage P3."""
        return None

    def categories(self) -> api.CategoryHints:
        return builtin.hints_for(GROUPS)

    def record_fields(self, device: Any) -> Dict[str, Any]:
        """Nothing beyond today's camera record; wired at stage P3."""
        return {}

    def review_source(self) -> Optional[api.ReviewSource]:
        """A Basler's saved frames ARE its data: no file of its own."""
        return None
