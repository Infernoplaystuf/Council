"""
typhon.prophesee — Prophesee event cameras: finds, opens and describes
Prophesee event cameras (EVK4) through OpenEB (docs/typhon_plugins_design.md
2.3).

STAGE P2: AN ADAPTER over code that stays where it is until stage P7 —
council_core/cameras.py (EvkBackend, EvkDevice, identify),
council_core/camera_settings.py (EvkSettings and the display settings,
which the device hands out) and council_core/event_playback.py (the raw
view) — reached through typhon_plugin_api.builtin, so the plugin imports
only typhon_plugin_api (design 7.2).

THE RAW VIEW IS ITS REVIEW SOURCE (design 2.4 item 13, 3.7)
An EVK4's run records every event the sensor sent in `<run>_events.raw`
beside the PNGs, and the slider's PNG / Raw button shows it. That view is
this plugin's `review_source()`: the run file's name, the button's word, and
how it is opened (event_playback.open_raw: one pass, nothing written beside
the file). The slider's live end stays Typhon's.

ITS SET-UP (stage P2 row 4; design 2.3, 2.4 item 15)
The Prophesee family's check box in a "Typhon (plugins)" project's set-up
wizard: the Metavision SDK or OpenEB, the Python versions its bindings are
built for, the EVK4's USB driver, and checks that the SDK imports and finds
a camera (council_core/camera_setup.py's EVK guide and checks, which do not
move). It has no scan page.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import typhon_plugin_api as api
from typhon_plugin_api import builtin

#: The settings groups an EVK4 describes (EvkSettings).
GROUPS = ("Biases", "Event rate controller", "Anti-flicker",
          "Event trail filter", "Event rate activity filter", "Display",
          "Status", "Camera")

#: The run file the raw view shows: what Start records beside the PNGs
#: (frame_camera.start, Device.start_raw).
RAW_SUFFIX = "_events.raw"


def open_raw(path: Any, window_us: int, **extra: Any) -> Any:
    """A recorded .raw for the slider (event_playback.open_raw)."""
    return builtin.open_raw(path, window_us=window_us, **extra)


#: The raw view, as the slider takes it.
RAW_VIEW = api.ReviewSource(name="raw", label="Raw", suffix=RAW_SUFFIX,
                            open=open_raw, opens_while_recording=False)


class PropheseePlugin:
    """Prophesee event cameras (camera plugin)."""

    #: Its cameras' backend name, as presets key them (design 3.3).
    backend = "prophesee"

    #: The SDK module to use instead of metavision_hal (a test's fake HAL);
    #: None is metavision_hal itself, imported on first use.
    sdk: Any = None

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None

    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx

    def deactivate(self) -> None:
        self.ctx = None

    def _backend(self) -> Any:
        return builtin.EvkBackend(self.sdk)

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
                                  f"Prophesee camera")
        return self._backend().open(info)

    def capabilities(self, device: Any) -> FrozenSet[str]:
        """What the opened camera says of itself: an event stream drawn as
        windows, its area set live, its own .raw — and never restartable
        on one connection (EvkDevice.restartable). Trigger In, sync and the
        event stream for velocity come later (design 3.4, 11)."""
        return frozenset(api.device_capabilities(device))

    def facet(self, device: Any, name: str) -> Any:
        """None until the facets arrive (stage P3; the event stream with
        velocity)."""
        return None

    def categories(self) -> api.CategoryHints:
        return builtin.hints_for(GROUPS)

    def record_fields(self, device: Any) -> Dict[str, Any]:
        """Nothing beyond today's camera record; wired at stage P3 (the
        sync role and Trigger In state come with multi-camera M2)."""
        return {}

    def review_source(self) -> Optional[api.ReviewSource]:
        return RAW_VIEW

    # -- its set-up (CameraSetup; design 2.4 item 15) ----------------------
    @property
    def setup_label(self) -> str:
        return builtin.setup_label(self.backend)

    def setup_guide(self, python: str) -> api.Guide:
        """The Metavision SDK (installer or OpenEB), the Python versions its
        bindings import under, the EVK4's USB driver, a USB 3 port."""
        return builtin.setup_guide(self.backend, python=python)

    def setup_checks(self) -> api.Readiness:
        """metavision_hal importable, a camera found."""
        return builtin.setup_check(self.backend, evk_sdk=self.sdk)
