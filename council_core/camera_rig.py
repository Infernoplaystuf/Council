"""
council_core.camera_rig — the rig: every camera one Typhon window runs, a
camera slot for each, and the one-camera view kept working over them.

WHERE THIS COMES FROM
The multi-camera design (docs/typhon_multicamera_design.md, 2.2, stage M1
row 1) replaces frame_camera's one module-level camera, `_LIVE = _Live()`,
with a Rig of CameraSlots; the plugin design (docs/typhon_plugins_design.md,
section 5) makes that same Rig the plugin host. This is that row's first
step: the rig and its slots BEHIND frame_camera's existing functions, with
`_LIVE` kept as a compatibility view, so every existing caller and test runs
unchanged. With one camera the result is identical to before.

WHICH FIELD GOES WHERE — DECIDED BY THE CODE'S OWN RULE
frame_camera's `_Live` class still declares every field, unchanged, in its
own file and lines: its `__init__` creates them all, and its `clear()` — what
Disconnect runs — resets the PER-CONNECTION ones. The rest survive a
Disconnect because they are about the app (the code says so at each of
them: the set-up path, the slider controller, the listeners, the drawn-box
state, the scan result). So:

  * a field `clear()` resets lives on the CameraSlot — one per camera;
  * every other field lives on the Rig — one per window.

`split_fields` reads that off the class itself (it runs `__init__`, then
`clear()` over sentinels, and sees what changed) rather than keeping a
second list here. Why: the plugin design (7.2) keeps Typhon's code where it
is while the branch lives, so a bug fix on qt-migration that adds a field to
`_Live` merges into the same lines and lands on the right side of the split
by itself — no list here to forget. A test checks every field resolves
through the view.

THE VIEW
`LiveView` is what `frame_camera._LIVE` now is. Reading or setting a
per-connection name goes to the ACTIVE slot (slot 1 in a one-camera app);
an app-level name goes to the Rig; `clear()` clears the active slot. Setting
a name `_Live` never declared raises, instead of quietly making state that
nothing reads (a plain object would have accepted it).

THE PLUGIN HOST (stage P1 row 2)
`rig.plugins` is the rig's plugin host (council_core.plugin_host), made on
first use; each slot's `plugin_state` is a plugin's per-camera state.
Today's Typhon never makes it. A "Typhon (plugins)" project (stage P2 rows
3 and 4) scans, opens and sets up its cameras through it.

CAPABILITIES (stage P2 row 1)
A slot answers what its camera can do (`capabilities`: frames or events,
restartable, ...; council_core.camera_capabilities): what the camera plugin
that opened it confirmed (`confirm`, the host's open_camera), else what the
camera says of itself. frame_camera asks it where it once asked "is it an
event camera?".

NAMES (stage P2 row 3)
A slot is called by its camera's name in the rig (cam1, cam2, ...; kept in
camera_rig.json by council_core.camera_rig_file): `rename_slot` gives it
that name when the camera connects, and `holding` says which slot has a
camera open ("in use as cam2" in the scan).

NOT YET (later rows of M1 / P1)
frame_camera still locks with its one module-level `_LOCK`; each slot has a
`lock` of its own for when the functions take a slot. Start, Stop and
Disconnect still act on the active slot only; with one slot that is the
whole rig. The 20-name `api` a slot hands the settings views is P4 row 3.

No Qt here (council_core's rule) and no import of frame_camera: the template
class is handed in.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Tuple

#: A camera's name: short and safe in a folder name (multi-camera design
#: 2.2): at most 16 characters, a-z, 0-9 and "-". Ended by \Z, not $: "$"
#: also matches before a final newline, which let a name ending in a
#: newline through (found at P2 row 3).
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,15}\Z")

#: The most cameras one window runs (multi-camera design: 2-4).
MAX_SLOTS = 4

#: What a slot's role may be (multi-camera design 2.2; used from stage M2).
ROLES = ("free", "leader", "follower")

#: Names the slot and the rig keep for themselves; `_Live` may not declare a
#: field by any of them (checked when a rig is made).
SLOT_OWN = frozenset({"id", "name", "role", "lock", "plugin_state", "clear",
                      "_reset", "confirmed", "capabilities", "confirm"})
RIG_OWN = frozenset({"slots", "active", "slot_fields", "rig_fields",
                     "add_slot", "select", "slot", "remove_slot", "names",
                     "connected", "_reset", "_lock", "plugins", "_plugins",
                     "rename_slot", "holding"})


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


def split_fields(template: type) -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """(per-connection names, app-level names) of `template`'s fields.

    `template()` must create every field and `template.clear(obj)` reset the
    per-connection ones — frame_camera._Live's contract. A field that
    `clear()` sets (even one `__init__` lacks) is per-connection; every
    other field `__init__` creates is app-level."""
    probe = template()
    declared = set(vars(probe))
    unset = _Unset()
    for name in declared:
        setattr(probe, name, unset)
    template.clear(probe)
    after = vars(probe)
    per_connection = frozenset(k for k, v in after.items() if v is not unset)
    app_level = frozenset(declared - per_connection)
    return per_connection, app_level


class CameraSlot:
    """One camera of the rig.

    Holds that camera's connection — every per-connection field the
    template declares (device, session, folder, run, job, the status-line
    state, ...) as plain attributes — plus who it is: its `id`, its `name`
    (cam1, cam2, ...), its `role`, a `lock` of its own, and `plugin_state`,
    one dictionary per plugin id (plugin design 5.1). `clear()` puts the
    per-connection fields back as Disconnect leaves them; who the slot is,
    and the plugins' state, stay."""

    def __init__(self, ident: int, name: str,
                 reset: Callable[[Any], None]) -> None:
        self.id = ident
        self.name = name
        self.role = "free"
        self.lock = threading.RLock()
        self.plugin_state: Dict[str, Dict[str, Any]] = {}
        #: What the camera plugin that opened this slot's camera confirmed
        #: it can do, with the device it was confirmed for — (device,
        #: capabilities) — or None (`capabilities`).
        self.confirmed: Optional[Tuple[Any, FrozenSet[str]]] = None
        self._reset = reset
        reset(self)

    def clear(self) -> None:
        self._reset(self)

    def confirm(self, device: Any, capabilities: Any) -> None:
        """Keep what the camera plugin that opened `device` for this slot
        confirmed it can do (plugin design 3.4); asked by `capabilities`
        while that device is the slot's."""
        self.confirmed = (device, frozenset(capabilities))

    @property
    def capabilities(self) -> FrozenSet[str]:
        """What this slot's camera can do (plugin design 3.4, 5.1): what its
        camera plugin confirmed when it opened it, else what the camera says
        of itself (council_core.camera_capabilities.of_device — today's
        Typhon, which opens cameras without the plugin host). Empty with no
        camera. Asked where the code once asked "is it an event camera?"."""
        device = getattr(self, "device", None)
        if device is None:
            return frozenset()
        confirmed = self.confirmed
        if confirmed is not None and confirmed[0] is device:
            return confirmed[1]
        from .camera_capabilities import of_device

        return of_device(device, getattr(self, "info", None))

    def __repr__(self) -> str:
        return (f"<CameraSlot {self.name} id={self.id} role={self.role} "
                f"device={'open' if getattr(self, 'device', None) is not None else 'none'}>")


class Rig:
    """Every camera one window runs, and what belongs to the window.

    The app-level fields of the template (the slider controller, the
    canvas, the listeners, the set-up path, the scan result, ...) are plain
    attributes of the rig; `slots` are its cameras, in the order they were
    added, and `active` the one the settings act on. A new rig has one slot,
    cam1, which is active."""

    def __init__(self, template: type, *, first: str = "cam1") -> None:
        slot_fields, rig_fields = split_fields(template)
        for names, own, where in ((slot_fields, SLOT_OWN, "slot"),
                                  (rig_fields, RIG_OWN, "rig")):
            clash = sorted(names & own)
            if clash:
                raise ValueError(f"{template.__name__} declares "
                                 f"{', '.join(clash)}, which the camera "
                                 f"{where} keeps for itself")
        self._lock = threading.RLock()
        self.slot_fields = slot_fields
        self.rig_fields = rig_fields
        fresh = template()
        for name in sorted(rig_fields):
            setattr(self, name, getattr(fresh, name))
        self._reset = template.clear
        self._plugins: Any = None
        self.slots: List[CameraSlot] = []
        self.active: CameraSlot = self.add_slot(first)

    @property
    def plugins(self) -> Any:
        """The plugin host (council_core.plugin_host.PluginHost): the rig IS
        the plugin host (plugin design 5.1). Made on first use, and reading
        nothing until asked, so a window that never uses a plugin pays
        nothing for it.

        Imported by a name built at run time, on purpose: Settings > Python
        Scripts (gui_settings.scripts_in_use) follows import statements and
        constant import_module() names, and a plain import here put the
        host, the plugin lists, the manifest reader, the plugin API and
        python_envs on the list of every app that links frame_camera —
        today's Typhon among them, which never loads them — and made that
        list slower to build (P1 review, 16 October 2026). Once a plugin
        is loaded, the list shows its modules as loaded ones."""
        with self._lock:
            if self._plugins is None:
                import importlib

                host = importlib.import_module(f"{__package__}.plugin_host")
                self._plugins = host.PluginHost(self)
            return self._plugins

    # -- slots ---------------------------------------------------------
    def names(self) -> List[str]:
        return [s.name for s in self.slots]

    def slot(self, name: str) -> CameraSlot:
        for s in self.slots:
            if s.name == name:
                return s
        raise KeyError(f"no camera named {name!r} in this rig "
                       f"(have {', '.join(self.names()) or 'none'})")

    def __iter__(self) -> Iterator[CameraSlot]:
        return iter(list(self.slots))

    def __len__(self) -> int:
        return len(self.slots)

    def add_slot(self, name: Optional[str] = None) -> CameraSlot:
        """A new, empty slot (not made active). `name` defaults to the first
        free cam1, cam2, ...; a given one must be free and match NAME_RE."""
        with self._lock:
            if len(self.slots) >= MAX_SLOTS:
                raise ValueError(f"a rig runs at most {MAX_SLOTS} cameras")
            taken = set(self.names())
            if name is None:
                name = next(f"cam{i}" for i in range(1, MAX_SLOTS + 2)
                            if f"cam{i}" not in taken)
            if not NAME_RE.match(name or ""):
                raise ValueError(
                    f"{name!r} cannot name a camera: use at most 16 "
                    f"characters, a-z, 0-9 and '-', starting with a letter "
                    f"or digit")
            if name in taken:
                raise ValueError(f"a camera is already named {name!r}")
            ident = max((s.id for s in self.slots), default=0) + 1
            made = CameraSlot(ident, name, self._reset)
            self.slots.append(made)
            return made

    def select(self, name: str) -> CameraSlot:
        """Make `name` the active slot: the one per-connection names of the
        view, and so frame_camera's functions, act on."""
        with self._lock:
            chosen = self.slot(name)
            self.active = chosen
            return chosen

    def remove_slot(self, name: str) -> None:
        """Drop an EMPTY slot. A slot with a camera open, the active slot
        and the last slot are refused: nothing is closed from here."""
        with self._lock:
            target = self.slot(name)
            if len(self.slots) == 1:
                raise ValueError("a rig always keeps one camera slot")
            if target is self.active:
                raise ValueError(f"{name} is the active camera; select "
                                 f"another first")
            if getattr(target, "device", None) is not None:
                raise ValueError(f"{name} has a camera open; disconnect it "
                                 f"first")
            self.slots.remove(target)

    def rename_slot(self, old: str, new: str) -> CameraSlot:
        """Call slot `old` `new` (a camera keeps its name in the rig from one
        session to the next: council_core.camera_rig_file). Refused when
        `new` is not a safe name or another slot has it; who the slot is
        otherwise — its id, its camera, its plugins' state — is unchanged."""
        with self._lock:
            target = self.slot(old)
            if new == old:
                return target
            if not NAME_RE.match(new or ""):
                raise ValueError(
                    f"{new!r} cannot name a camera: use at most 16 "
                    f"characters, a-z, 0-9 and '-', starting with a letter "
                    f"or digit")
            if new in self.names():
                raise ValueError(f"a camera is already named {new!r}")
            target.name = new
            return target

    def connected(self) -> List[CameraSlot]:
        """The slots with a camera open, in order."""
        return [s for s in self.slots
                if getattr(s, "device", None) is not None]

    def holding(self, info: Any) -> Optional[CameraSlot]:
        """The slot that has camera `info` open — the same backend and the
        key it was found by — or None ("in use as cam2" in the scan)."""
        backend = str(getattr(info, "backend", "") or "")
        key = str(getattr(info, "key", "") or "")
        for s in self.connected():
            held = getattr(s, "info", None)
            if held is not None and key \
                    and str(getattr(held, "backend", "") or "") == backend \
                    and str(getattr(held, "key", "") or "") == key:
                return s
        return None

    def __repr__(self) -> str:
        return (f"<Rig {len(self.slots)} slot(s), active "
                f"{self.active.name}: {', '.join(self.names())}>")


class LiveView:
    """`frame_camera._LIVE`: the one-camera object, as a view over a rig.

    Per-connection names read and write the rig's ACTIVE slot; app-level
    names read and write the rig; `clear()` clears the active slot. A name
    the template never declared is refused both ways."""

    __slots__ = ("_rig",)

    def __init__(self, rig: Rig) -> None:
        object.__setattr__(self, "_rig", rig)

    def __getattr__(self, name: str) -> Any:
        rig = object.__getattribute__(self, "_rig")
        if name in rig.slot_fields:
            return getattr(rig.active, name)
        if name in rig.rig_fields:
            return getattr(rig, name)
        raise AttributeError(f"frame_camera._LIVE has no field {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        rig = object.__getattribute__(self, "_rig")
        if name in rig.slot_fields:
            setattr(rig.active, name, value)
        elif name in rig.rig_fields:
            setattr(rig, name, value)
        else:
            raise AttributeError(
                f"frame_camera._LIVE has no field {name!r}: declare it in "
                f"_Live.__init__ (and reset it in clear() if it belongs to "
                f"one connection)")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"frame_camera._LIVE's fields cannot be "
                             f"deleted ({name!r})")

    def clear(self) -> None:
        object.__getattribute__(self, "_rig").active.clear()

    def __dir__(self) -> List[str]:
        rig = object.__getattribute__(self, "_rig")
        return sorted(rig.slot_fields | rig.rig_fields | {"clear"})

    def __repr__(self) -> str:
        rig = object.__getattribute__(self, "_rig")
        return f"<frame_camera._LIVE: view over {rig!r}>"


def where(view: LiveView, name: str) -> str:
    """"slot" or "rig": where `name` of `view` lives. For tests and
    diagnostics."""
    rig = object.__getattribute__(view, "_rig")
    if name in rig.slot_fields:
        return "slot"
    if name in rig.rig_fields:
        return "rig"
    raise KeyError(name)


def rig_of(view: LiveView) -> Rig:
    """The rig behind `view`."""
    return object.__getattribute__(view, "_rig")
