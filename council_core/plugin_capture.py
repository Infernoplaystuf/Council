"""
council_core.plugin_capture — the capture set-up the core holds for a frame
camera whose camera plugin gives it none (typhon-plugins branch, P3 review).

WHY (docs/typhon_plugins_design.md 2.5, 3.6)
Today a frame camera's capture set-up — Continuous or a trigger, its
source, Downsample — is one object on its Device (`device.capture`, a
council_core.triggered.CaptureSetup), and the camera's settings provider
describes it as the "Capture" group. The built-in Basler and simulated
cameras build both. A camera plugin written against typhon_plugin_api
alone does not: the base Device's `capture` is None. Before this, such a
camera got no Downsample row, no trigger rows and no snapshot runs — while
design 2.5 says the core adds the Downsample row to EVERY frame camera, and
3.6 that a frame camera with no trigger at all still captures snapshot
runs.

WHAT IT IS
`CoreCapture(device)` stands in for such a Device in its slot (the plugin
host wraps it when a "Typhon (plugins)" project opens the camera,
PluginHost.open_camera): every attribute and call goes to the camera's own
Device, except the capture set-up, which the core holds —
  * `capture`, a CaptureSetup of its own (Continuous, Downsample Off);
  * `capture_options()`: no hardware input, Downsample by Typhon's
    averaging (the camera does not bin for Typhon);
  * Downsample: `apply_downsample`, `downsample_info`, and `read()` marking
    each frame for the core's frame stage (cameras.downsample_meta), as the
    simulated camera does;
  * `settings_provider()`: the camera's own settings with the core's
    "Capture" group after them (triggered.capture_settings: the trigger
    rows and the core's Downsample row), read and written in place, so
    presets, the settings window and a run's camera record carry it with
    no plumbing of their own, as today.
The camera keeps no trigger: an exact run is refused by the triggering
plugin's start check (no trigger facet), a snapshot from Software or the
Timer runs (frames only). A camera plugin is always handed its own Device
back (`plugin_device`), never this.

Today's Typhon makes no plugin host, so it never makes one of these.
No Qt, no pickle, no network.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

#: What CoreCapture keeps for itself; every other attribute is the
#: camera's own Device's (read and written there).
_OWN = frozenset({"_inner", "capture", "downsample_method"})

DOWNSAMPLE_HELP = (
    "Saves smaller pictures: each 2 x 2, 3 x 3 or 4 x 4 block of pixels "
    "becomes one, AVERAGED by Typhon before saving (this camera's plugin "
    "offers no binning). Off (the default) never changes the data. Kept in "
    "presets; fixed while capturing.")

#: cameras.downsample_meta, looked up at the first frame read (this module
#: loads no camera code when imported).
_DOWNSAMPLE_META: Any = None


def plugin_device(device: Any) -> Any:
    """The Device the camera's plugin opened: `device` itself, or the one a
    CoreCapture stands in for — what a camera plugin is always handed."""
    if type(device) is CoreCapture:
        return object.__getattribute__(device, "_inner")
    return device


def needs_core_capture(device: Any, kind: str) -> bool:
    """Whether the core holds the capture set-up of `device`, a camera of
    `kind`: a frame camera whose Device has none of its own."""
    return (device is not None and str(kind) == "frame"
            and getattr(device, "capture", None) is None)


class CoreCapture:
    """A frame camera's Device with the core's capture set-up (see the
    module)."""

    def __init__(self, device: Any) -> None:
        from .triggered import CaptureSetup

        object.__setattr__(self, "_inner", device)
        object.__setattr__(self, "capture", CaptureSetup())
        object.__setattr__(self, "downsample_method", "")

    # -- everything else is the camera's own ---------------------------------
    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_inner"), name, value)

    def __repr__(self) -> str:
        return f"<CoreCapture of {self._inner!r}>"

    # -- the capture set-up ----------------------------------------------------
    def capture_options(self) -> Any:
        from .triggered import CaptureOptions

        return CaptureOptions(downsample_help=DOWNSAMPLE_HELP)

    def trigger_problem(self, setup: Any) -> str:
        """Today's per-camera check (the triggering plugin asks its facets
        instead): a snapshot from Software or the Timer needs no trigger;
        anything else does, and this camera has none."""
        if getattr(setup, "snapshot", False) and \
                getattr(setup, "typhon_fires", True):
            return ""
        return ("this camera has no trigger Typhon can arm — choose "
                "Triggered: snapshot from Software or the Timer")

    def apply_downsample(self, factor: int) -> str:
        """Downsample `factor` by Typhon's averaging (this camera does not
        bin for Typhon)."""
        from . import reduce as red

        factor = max(1, min(4, int(factor)))
        self.capture.downsample = factor
        if factor <= 1:
            self.downsample_method = ""
            return "Off — every pixel as the camera sends it"
        self.downsample_method = red.TYPHON_MEAN
        return (f"{factor}x by Typhon, averaging each {factor} x {factor} "
                f"block before saving")

    def downsample_info(self) -> Dict[str, Any]:
        factor = int(getattr(self.capture, "downsample", 1) or 1)
        return {"factor": factor, "method": self.downsample_method,
                "note": ""}

    def read(self, timeout_ms: int = 1000) -> Any:
        """The camera's next frame, marked for the core's frame stage when
        Downsample is on (cameras.downsample_meta), as the simulated
        camera marks its own."""
        frame = object.__getattribute__(self, "_inner").read(timeout_ms)
        if frame is not None:
            global _DOWNSAMPLE_META
            if _DOWNSAMPLE_META is None:
                from .cameras import downsample_meta
                _DOWNSAMPLE_META = downsample_meta
            extra = _DOWNSAMPLE_META(self.capture, self.downsample_method)
            if extra:
                meta = frame.meta if isinstance(frame.meta, dict) else \
                    dict(frame.meta or {})
                meta.update(extra)
                frame.meta = meta
        return frame

    # -- its settings, with the core's Capture group ---------------------------
    def settings_provider(self) -> Any:
        inner = object.__getattribute__(self, "_inner").settings_provider()
        return CaptureSettings(inner, self)

    def settings(self) -> List[Any]:
        return self.settings_provider().describe()

    def set_setting(self, key: str, value: Any) -> Any:
        return self.settings_provider().set(key, value)

    def apply_settings(self, values: Dict[str, Any], roi: Any = None) -> Any:
        from . import camera_settings

        return camera_settings.apply(self, values, roi)



class CaptureSettings:
    """The camera's own settings provider, and the core's "Capture" group
    after its rows: the trigger rows and the core's Downsample row
    (triggered.capture_settings), read and written on the core's capture
    set-up. Everything else is the camera's own provider's. `set`, `find`
    and the rest of camera_settings.Provider's shared steps are Provider's
    own, run on this (so this module loads no camera code until used)."""

    def __init__(self, inner: Any, device: Any) -> None:
        self._inner = inner
        self._device = device

    @property
    def roi_after_group(self) -> Optional[str]:
        return getattr(self._inner, "roi_after_group", None)

    @property
    def warns(self) -> frozenset:
        return frozenset(getattr(self._inner, "warns", frozenset()))

    def _rows(self) -> List[Any]:
        from . import triggered

        device = self._device
        return list(triggered.capture_settings(device.capture,
                                               device.capture_options()))

    def describe(self) -> List[Any]:
        return list(self._inner.describe()) + self._rows()

    def describe_groups(self, groups: Iterable[str]) -> List[Any]:
        from . import triggered

        wanted = list(groups)
        out = list(self._inner.describe_groups(wanted))
        if triggered.GROUP in set(wanted):
            out += self._rows()
        return out

    def read(self, key: str) -> Any:
        from . import triggered

        if triggered.is_capture_key(key):
            device = self._device
            return triggered.read_capture(device.capture,
                                          device.capture_options(), key)
        return self._inner.read(key)

    def write(self, setting: Any, value: Any,
              batch: Mapping[str, Any]) -> None:
        from . import triggered

        if triggered.is_capture_key(setting.key):
            if setting.key == triggered.DOWNSAMPLE_KEY:
                self._device.apply_downsample(triggered.factor_of(value))
            else:
                triggered.write_capture(self._device.capture, setting.key,
                                        value)
            return
        self._inner.write(setting, value, batch)

    def held_by(self, setting: Any, batch: Mapping[str, Any]) -> str:
        from . import triggered

        if triggered.is_capture_key(setting.key):
            return ""
        return self._inner.held_by(setting, batch)

    # -- Provider's shared steps, on these rows --------------------------------
    def find(self, key: str) -> Any:
        from .camera_settings import Provider

        return Provider.find(self, key)        # type: ignore[arg-type]

    def set(self, key: str, value: Any, batch: Any = None,
            setting: Any = None) -> Any:
        from .camera_settings import Provider

        return Provider.set(self, key, value, batch=batch,  # type: ignore
                            setting=setting)

    def _safe_read(self, key: str) -> Any:
        from .camera_settings import Provider

        return Provider._safe_read(self, key)  # type: ignore[arg-type]

    # -- the rest is the camera's own provider's -------------------------------
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
