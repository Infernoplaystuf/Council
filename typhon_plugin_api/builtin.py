"""
typhon_plugin_api.builtin — what the BUILT-IN adapter plugins reach of the
code that stays in council_core while the typhon-plugins branch lives.

WHY THIS EXISTS (docs/typhon_plugins_design.md 2.3, 7.2)
The built-in camera plugins take over today's camera code — BaslerBackend,
EvkBackend, SyntheticBackend and their devices, the raw view's playback, the
rows of the settings tabs' tables — but that code does not move into their
folders before stage P7: a fix merged from qt-migration must land in the same
file and lines on both branches. So each built-in plugin is a thin adapter,
and it reaches that code through re-exports here, keeping the rule that a
plugin imports only typhon_plugin_api (a test checks every import in
typhon_plugins/).

NOT PART OF THE API A PLUGIN FROM SOMEONE ELSE MAY USE
These names are brand code, not contract: they go at stage P7, when the
files move into the plugins' own folders, and the API frozen as 1.0 (stage
P5) does not include them. A plugin from someone else uses the names of
typhon_plugin_api itself.

Looked up at every use (module __getattr__), never kept here: importing
this loads nothing, and an adapter always reaches the code as its module
has it.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

_REEXPORTS: Dict[str, Tuple[str, str]] = {
    # the camera families' code (council_core/cameras.py)
    "BaslerBackend": ("council_core.cameras", "BaslerBackend"),
    "BaslerDevice": ("council_core.cameras", "BaslerDevice"),
    "EvkBackend": ("council_core.cameras", "EvkBackend"),
    "EvkDevice": ("council_core.cameras", "EvkDevice"),
    "SyntheticBackend": ("council_core.cameras", "SyntheticBackend"),
    "SyntheticDevice": ("council_core.cameras", "SyntheticDevice"),
    "identify": ("council_core.cameras", "identify"),
    # (the capture set-up's types, its modes and Typhon's own sources are
    # the API's own from stage P3, design 2.5: typhon_plugin_api.
    # CaptureSetup, MODE_EXACT, SOFTWARE ...)
    # THE TRIGGERING PLUGIN'S CODE (stage P3 row 2; design 2.3): the books
    # of a triggered run and the rows of its set-up (council_core/
    # triggered.py) and frame_camera's trigger functions — arming, the edge
    # count, closing the books, the words — which stay where they are
    # until P7 (design 7.2). The typhon.trigger adapter calls them with a
    # camera made of its facets, never with the Device.
    "TriggerRun": ("council_core.triggered", "TriggerRun"),
    "TRIGGERS_SUFFIX": ("council_core.triggered", "TRIGGERS_SUFFIX"),
    "trigger_settings": ("council_core.triggered", "trigger_settings"),
    "show_seconds": ("council_core.triggered", "show_seconds"),
    "arm": ("frame_camera", "_arm"),
    "disarm": ("frame_camera", "_disarm"),
    "count_from_ready": ("frame_camera", "_count_from_ready"),
    "let_frames_land": ("frame_camera", "_let_frames_land"),
    "end_triggers": ("frame_camera", "_end_triggers"),
    "capture_record": ("frame_camera", "_capture_record"),
    "trigger_timing": ("frame_camera", "_trigger_timing"),
    "trigger_start_words": ("frame_camera", "_trigger_start_words"),
    "capture_now_idle": ("frame_camera", "_capture_now_idle"),
    "STOP_TRIGGER_WAIT_SECONDS": ("frame_camera",
                                  "STOP_TRIGGER_WAIT_SECONDS"),
    "CAPTURE_KEY": ("frame_camera", "CAPTURE_KEY"),
    # the set-up written in place, as the Basic tab's boxes write it
    # (frame_camera.set_camera_setting: refused while capturing, the views
    # told)
    "set_camera_setting": ("frame_camera", "set_camera_setting"),
    # the raw view (council_core/event_playback.py)
    "open_raw": ("council_core.event_playback", "open_raw"),
    # the settings tabs' tables (council_core/camera_categories.py)
    "SHARED_TABS": ("council_core.camera_categories", "SHARED_TABS"),
    "TIPS": ("council_core.camera_categories", "TIPS"),
    "MOST_USED": ("council_core.camera_categories", "MOST_USED"),
    # each brand's set-up guide and checks (council_core/camera_setup.py)
    # and the Basler scan page (council_core/basler_scan.py), which the
    # Basler and Prophesee plugins bring into the set-up wizard (design
    # 2.3, 2.4 item 15)
    "setup_guide": ("council_core.camera_setup", "guide"),
    "setup_check": ("council_core.camera_setup", "check"),
    "setup_label": ("council_core.camera_setup", "label"),
    "basler_scan": ("council_core.basler_scan", "scan"),
}

__all__ = sorted(_REEXPORTS)


def __getattr__(name: str) -> Any:
    """The name as its module has it NOW — looked up at every use, never
    kept here, so the adapters reach exactly the code that stays in
    council_core (a test's stand-in for it included)."""
    target = _REEXPORTS.get(name)
    if target is None:
        raise AttributeError(f"typhon_plugin_api.builtin has no {name!r}")
    import importlib

    return getattr(importlib.import_module(target[0]), target[1])


def __dir__() -> Any:
    return __all__


def hints_for(groups: Any) -> Any:
    """The settings tabs' rows for `groups` — the rows of today's tables
    (council_core.camera_categories) that a built-in camera plugin's groups
    have: each shared tab cut to those groups (a tab left with none is
    dropped), and their tips and most-used keys. One table while the branch
    lives; each plugin names only which groups are its own."""
    import typhon_plugin_api as api

    wanted = [str(g) for g in groups]
    mine = set(wanted)
    shared = []
    for title, tab_groups, tip in __getattr__("SHARED_TABS"):
        kept = tuple(g for g in tab_groups if g in mine)
        if kept:
            shared.append((title, kept, tip))
    tips = {k: v for k, v in __getattr__("TIPS").items() if k in mine}
    most = {k: tuple(v) for k, v in __getattr__("MOST_USED").items()
            if k in mine}
    from types import MappingProxyType

    return api.CategoryHints(shared_tabs=tuple(shared),
                             tips=MappingProxyType(tips),
                             most_used=MappingProxyType(most))


# ======================================================================
# The facets over today's Device methods (stage P3; design 3.4, 3.11)
# ======================================================================
class DeviceFacet:
    """A facet that is today's Device methods regrouped (cameras.Device:
    the ten trigger and Downsample methods, design 2.4 item 5): each call
    is the device's own method, by today's name, so a camera's answer and
    its refusal (a CameraError) are what they were. The built-in camera
    plugins give these; a plugin from someone else implements the
    protocols of typhon_plugin_api itself."""

    def __init__(self, device: Any) -> None:
        self._device = device

    def __repr__(self) -> str:
        return f"<{type(self).__name__} of {type(self._device).__name__}>"


class _Arming(DeviceFacet):
    """What both trigger facets do: the camera's own check, arming, the
    ready question and putting the triggers back."""

    def problem(self, setup: Any) -> str:
        return str(self._device.trigger_problem(setup) or "")

    def arm(self, setup: Any) -> Any:
        return self._device.arm_trigger(setup)

    def ready(self, timeout_s: float = 0.0) -> Any:
        return self._device.trigger_ready(timeout_s)

    def disarm(self, armed: Any) -> Any:
        return self._device.disarm_trigger(armed)


class DeviceSoftwareTrigger(_Arming):
    """typhon_plugin_api.SoftwareTrigger over the device's own methods."""

    def fire(self) -> None:
        self._device.fire_trigger()


class DeviceHardwareTrigger(_Arming):
    """typhon_plugin_api.HardwareTrigger over the device's own methods
    (its inputs: the camera's CaptureOptions)."""

    def inputs(self) -> Any:
        return self._device.capture_options()

    def count_edges(self, armed: Any, seen: Any = None) -> Any:
        return self._device.count_edges(armed, seen)

    def count_from_now(self, armed: Any) -> None:
        self._device.count_from_now(armed)

    def count_to_now(self, armed: Any) -> None:
        self._device.count_to_now(armed)


class DeviceBinning(DeviceFacet):
    """typhon_plugin_api.Binning over the device's own methods (a Basler's:
    can_bin, apply_downsample, check_downsample, downsample_info)."""

    def can_bin(self, factor: int) -> bool:
        return bool(self._device.can_bin(int(factor)))

    def apply(self, factor: int) -> str:
        return str(self._device.apply_downsample(int(factor)) or "")

    def check(self, rebin: bool = False) -> str:
        return str(self._device.check_downsample(bool(rebin)) or "")

    def info(self) -> Any:
        return dict(self._device.downsample_info() or {})


#: The facet classes by capability name.
DEVICE_FACETS = {"software_trigger": DeviceSoftwareTrigger,
                 "hardware_trigger": DeviceHardwareTrigger,
                 "binning": DeviceBinning}


def device_facet(device: Any, name: str) -> Any:
    """The facet `name` over `device`'s own methods (DEVICE_FACETS), or
    None for a name with none."""
    made = DEVICE_FACETS.get(str(name))
    return made(device) if made is not None and device is not None else None
