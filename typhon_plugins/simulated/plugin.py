"""
typhon.simulated — Simulated cameras: the simulated frame and event cameras
every contract suite runs against first (docs/typhon_plugins_design.md 2.3,
9.1: the reference plugin).

STAGE P2: AN ADAPTER over code that stays where it is until stage P7 —
council_core/cameras.py (SyntheticBackend, SyntheticDevice) and
council_core/camera_settings.py (SyntheticSettings, which the device hands
out as its settings) — reached through typhon_plugin_api.builtin, so the
plugin imports only typhon_plugin_api (design 7.2).

N SIMULATED CAMERAS (multi-camera design 2.10; plugin design 2.3)
`COUNCIL_SIM_CAMERAS=frame,frame,event` lists that many simulated cameras,
each with a name and serial of its own: the first of each kind keeps today's
key and serial (sim-frame SIM-1, sim-event SIM-2), and the next ones are
sim-frame-2, sim-frame-3, ... with a serial made from the key (SIM-FRAME-2).
A serial never depends on the camera's place in the list: camera_rig.json
and the presets know a camera by its serial, so numbering them in order
made today's event camera another unit (SIM-3) once a second frame camera
came before it, and gave its rig name to that frame camera (P2 review).
Unset, the plugin lists today's two exactly as SyntheticBackend lists them.
A value Typhon cannot read is said, never guessed at: the scan lists no
simulated camera and says why.
"""
from __future__ import annotations

import os
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import typhon_plugin_api as api
from typhon_plugin_api import builtin

#: The environment variable that chooses the simulated cameras.
SIM_ENV = "COUNCIL_SIM_CAMERAS"
#: The kinds a simulated camera can be.
KINDS = ("frame", "event")
#: The most simulated cameras one list may hold (a rig runs at most four;
#: tests of what a fifth does need a few more).
MAX_SIM_CAMERAS = 8

#: The settings groups a simulated camera describes (SyntheticSettings):
#: a frame camera's Basler-like ones and an event camera's EVK4-like ones.
GROUPS = ("Exposure", "Gain", "Frame rate", "Capture", "Image", "Biases",
          "Event rate controller", "Event trail filter", "Display", "Status")


class SimulatedPlugin:
    """Simulated cameras (camera plugin, the reference plugin)."""

    #: Its cameras' backend name, as presets key them (design 3.3).
    backend = "simulated"

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None

    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx

    def deactivate(self) -> None:
        self.ctx = None

    # -- the camera plugin (design 3.3) ------------------------------------
    def available(self) -> Tuple[bool, str]:
        """Always: a simulated camera needs no SDK."""
        return True, ""

    def discover(self) -> List[Any]:
        """The simulated cameras COUNCIL_SIM_CAMERAS lists, or today's two."""
        return sim_cameras()

    def open(self, info: Any) -> Any:
        if getattr(info, "backend", "") != self.backend:
            raise api.CameraError(f"{getattr(info, 'label', info)} is not a "
                                  f"simulated camera")
        if getattr(info, "kind", "") not in KINDS:
            raise api.CameraError(f"a simulated camera is a frame or an event "
                                  f"camera, not {info.kind!r}")
        return builtin.SyntheticBackend().open(info)

    def capabilities(self, device: Any) -> FrozenSet[str]:
        """What the opened camera says of itself, and a software trigger
        where it has the capture set-up a trigger is fired through (the
        frame camera: SyntheticDevice.fire_trigger)."""
        caps = set(api.device_capabilities(device))
        if getattr(device, "capture", None) is not None and \
                callable(getattr(device, "fire_trigger", None)):
            caps.add("software_trigger")
        return frozenset(caps)

    def facet(self, device: Any, name: str) -> Any:
        """The simulated FRAME camera's software trigger — SyntheticDevice's
        own (armed, it sends a frame only when one is fired, and ignores one
        fired while it is busy, as a Basler does) — through today's methods
        (typhon_plugin_api.builtin.device_facet). It has no hardware input
        and no binning: its Downsample is Typhon's averaging. An event
        camera has no facet."""
        if name == "software_trigger" and \
                getattr(device, "capture", None) is not None and \
                callable(getattr(device, "fire_trigger", None)):
            return builtin.device_facet(device, name)
        return None

    def categories(self) -> api.CategoryHints:
        return builtin.hints_for(GROUPS)

    def record_fields(self, device: Any) -> Dict[str, Any]:
        """Nothing beyond today's camera record (which already says what the
        camera is and every setting); wired at stage P3."""
        return {}

    def review_source(self) -> Optional[api.ReviewSource]:
        """A simulated camera records no file of its own."""
        return None


def sim_cameras(spec: Optional[str] = None) -> List[Any]:
    """The simulated cameras `spec` (else COUNCIL_SIM_CAMERAS) lists — or,
    unset or blank, today's two, as SyntheticBackend lists them. Raises
    CameraError naming the value when it cannot be read."""
    if spec is None:
        spec = os.environ.get(SIM_ENV, "")
    text = str(spec).strip()
    if not text:
        return builtin.SyntheticBackend().discover()
    kinds = [part.strip().lower() for part in text.split(",")]
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise api.CameraError(
            f"{SIM_ENV}={spec!r}: each entry is frame or event, separated by "
            f"commas (for example frame,frame,event) — not "
            f"{', '.join(repr(b) for b in bad)}")
    if len(kinds) > MAX_SIM_CAMERAS:
        raise api.CameraError(f"{SIM_ENV}={spec!r} lists {len(kinds)} cameras;"
                              f" at most {MAX_SIM_CAMERAS}")
    # Today's two keep today's serials (SyntheticBackend's own); every other
    # serial is made from its key, so none depends on where it is listed.
    todays = {i.key: i.serial for i in builtin.SyntheticBackend().discover()}
    out = []
    seen: Dict[str, int] = {}
    for kind in kinds:
        n = seen[kind] = seen.get(kind, 0) + 1
        key = f"sim-{kind}" if n == 1 else f"sim-{kind}-{n}"
        model = (f"Simulated {kind} camera" if n == 1
                 else f"Simulated {kind} camera {n}")
        out.append(api.CameraInfo("simulated", key, model,
                                  todays.get(key) or key.upper(),
                                  "simulated", kind))
    return out
