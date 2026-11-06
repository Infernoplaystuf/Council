"""
council_core.camera_capabilities — what a camera can do, by name, and the one
rule that reads it off an open camera.

WHY (docs/typhon_plugins_design.md 2.4 item 4, 3.4)
Typhon asked "is this an event camera?" wherever an event camera behaves
differently: the live line's event count, the FPS box's "ms windows", the
simulated camera's settings. With camera plugins a camera is no longer one of
two brands, so the question becomes a CAPABILITY: `events` (a stream drawn as
windows), `frames`, `restartable`, ... — named things a camera can do. A
camera plugin declares the ones its cameras can have (its manifest), and the
opened camera confirms them (design 3.4: two steps). The core asks a
capability question; it never names a brand.

THE RULE FOR AN OPEN CAMERA (`of_device`)
What today's Device says of itself, read off the flags it already has — no
call to the camera, nothing written:

    frames / events   its CameraInfo.kind ("event", else frames)
    restartable       Device.restartable (an EVK4's stream is not)
    area_live         Device.area_live (an EVK4's area changes live)
    grab_failures     Device.counts_grab_failures (a Basler counts them)
    raw_recording     Device.records_raw (an event camera's .raw)

The trigger and binning capabilities (`software_trigger`, `hardware_trigger`,
`binning`) are not read here: telling them needs the camera itself (its
trigger and binning nodes), so the camera plugin confirms them when it opens
the camera, and their facets come with stage P3. A camera slot keeps what its
plugin confirmed (council_core.camera_rig.CameraSlot.capabilities); without
a plugin (today's Typhon) the slot answers from this rule.

UNKNOWN NAMES (design 3.4)
A capability a camera OFFERS that Typhon does not know is ignored (`known`);
a capability a feature NEEDS that Typhon does not know counts as missing
(`missing`), so the feature stays off with a reason.

No toolkit, no SDK, no plugin host, and no import of the camera module: a
device is read by its attributes only.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

FRAMES = "frames"
EVENTS = "events"
RESTARTABLE = "restartable"
AREA_LIVE = "area_live"
GRAB_FAILURES = "grab_failures"
RAW_RECORDING = "raw_recording"
SOFTWARE_TRIGGER = "software_trigger"
HARDWARE_TRIGGER = "hardware_trigger"
BINNING = "binning"
CAMERA_CLOCK = "camera_clock"
EVENT_STREAM = "event_stream"

#: Every capability Typhon knows (design 3.4's table): name -> (what it
#: means, the facet a feature uses it through, "" for none). Facets are
#: built at stage P3 (the triggers, binning) and with velocity (the event
#: stream); `camera_clock` is known only after the first frame.
CAPABILITIES: Dict[str, Tuple[str, str]] = {
    FRAMES: ("picture frames", ""),
    EVENTS: ("an event stream, drawn as windows", ""),
    RESTARTABLE: ("the stream may be stopped and started on one connection "
                  "(an EVK4's may not)", ""),
    AREA_LIVE: ("the camera's area can change while it streams", ""),
    GRAB_FAILURES: ("failed grabs are counted", ""),
    RAW_RECORDING: ("records its own raw file (.raw)", ""),
    SOFTWARE_TRIGGER: ("Typhon can fire it", "SoftwareTrigger"),
    HARDWARE_TRIGGER: ("it waits for an input line", "HardwareTrigger"),
    BINNING: ("the camera can average its own pixels", "Binning"),
    CAMERA_CLOCK: ("frames carry a usable camera time stamp", ""),
    EVENT_STREAM: ("raw event arrays can be passed on", "EventStream"),
}

#: The capabilities `of_device` reads off a Device's own flags.
FROM_FLAGS: Tuple[str, ...] = (FRAMES, EVENTS, RESTARTABLE, AREA_LIVE,
                               GRAB_FAILURES, RAW_RECORDING)


def known(names: Iterable[str]) -> FrozenSet[str]:
    """The names among `names` that Typhon knows; a capability a camera
    offers that Typhon does not know is ignored (design 3.4)."""
    return frozenset(str(n) for n in names if str(n) in CAPABILITIES)


def missing(needs: Iterable[str], have: Iterable[str]) -> List[str]:
    """What of `needs` (a manifest's: "a|b" means either) `have` lacks, in
    order. A needed name Typhon does not know counts as missing, so the
    feature stays off with a reason (design 3.4)."""
    held = known(have)
    out = []
    for need in needs:
        options = [part for part in str(need).split("|") if part]
        if not any(part in held for part in options):
            out.append(str(need))
    return out


def kind_of(device: Any, info: Any = None) -> str:
    """"frame" or "event": what the camera is (`info`, else the device's
    own CameraInfo) — "frame" when neither says, CameraInfo's own
    default."""
    for source in (info, getattr(device, "info", None)):
        kind = str(getattr(source, "kind", "") or "")
        if kind:
            return kind
    return "frame"


def of_device(device: Any, info: Any = None) -> FrozenSet[str]:
    """What an open camera can do, read off the flags today's Device has
    (see THE RULE above); empty for no device. `info` is the camera as
    Typhon knows it (the slot's), else the device's own."""
    if device is None:
        return frozenset()
    caps = {EVENTS if kind_of(device, info) == "event" else FRAMES}
    if getattr(device, "restartable", True):
        caps.add(RESTARTABLE)
    if getattr(device, "area_live", False):
        caps.add(AREA_LIVE)
    if getattr(device, "counts_grab_failures", False):
        caps.add(GRAB_FAILURES)
    if getattr(device, "records_raw", False):
        caps.add(RAW_RECORDING)
    return frozenset(caps)


def has(device: Any, name: str, info: Optional[Any] = None) -> bool:
    """Whether `device` can do `name` by the rule above — the question asked
    where the code asked "is it an event camera?"."""
    return name in of_device(device, info)
