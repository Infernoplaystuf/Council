"""
council_core.plugin_keys — whose a key in a camera's preset is, and how a
"Typhon (plugins)" project splits a preset between the camera and its
plugins (docs/typhon_plugins_design.md 2.5, 3.9; stage P3 row 4).

THE NAMES
  * "<id>/<key>" is plugin <id>'s: a plugin's per-camera value rides in the
    camera's preset under its id ("acme.thermal/gain"), plain values only
    (bool, text, a 64-bit whole number, a finite number:
    camera_presets' rule). A camera's own keys never hold "/" (Basler's are
    GenICam names, an EVK4's "erc.rate", "display.events").
  * The built-ins keep the names that exist today, so a preset reads the
    same in today's Typhon and the plugin one, both ways (design 2.5):
    "capture.*" is the triggering plugin's, "save.downsample" the core's
    (Downsample stays core). While the code stays in place (design 7.2)
    the camera's settings provider reads and writes them in place, so a
    running owner's built-in keys go to the camera as they always did.
  * Every other key is the camera's.

A PLUGIN THAT IS NOT RUNNING — not in this project's list, not on this PC,
switched off after a fault — has its keys KEPT UNTOUCHED: never handed to
camera_settings.apply (which would report them refused, measured), never
lost when the preset is saved again, and said as kept (design 3.9: "keys
of a plugin that is not loaded are kept untouched and listed as such").

Plain data only: no Qt, no plugin code, nothing written here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

#: "<id>/<key>": a plugin id (council_core.plugin_manifest.ID_RE's form)
#: and a key of its own, not empty.
NAMESPACED_RE = re.compile(
    r"^(?P<id>[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)/(?P<key>.+)\Z",
    re.DOTALL)
#: The built-ins' keys under today's names: (prefix, the plugin that owns
#: every key starting with it). "save.downsample" is the core's: no owner.
BUILTIN_PREFIXES: Tuple[Tuple[str, str], ...] = (("capture.", "typhon.trigger"),)
#: The plugin a built-in prefix names, for the words.
TRIGGER = "typhon.trigger"


def owner(key: Any) -> Optional[str]:
    """The plugin `key` of a preset belongs to, or None for the camera's
    own (and the core's: "save.downsample")."""
    text = str(key)
    found = NAMESPACED_RE.match(text)
    if found:
        return found.group("id")
    for prefix, pid in BUILTIN_PREFIXES:
        if text.startswith(prefix):
            return pid
    return None


def namespaced(pid: str, key: str) -> str:
    """Plugin `pid`'s `key` as a preset holds it: "<id>/<key>"."""
    text = f"{pid}/{key}"
    found = NAMESPACED_RE.match(text)
    if not key or not found or found.group("id") != pid:
        raise ValueError(f"{pid!r} and {key!r} do not make a plugin's "
                         f"preset key (\"<id>/<key>\")")
    return text


def local(key: str) -> str:
    """The plugin's own name of a namespaced key ("acme.thermal/gain" ->
    "gain"); the key as it is when it is not namespaced."""
    found = NAMESPACED_RE.match(str(key))
    return found.group("key") if found else str(key)


def is_namespaced(key: Any) -> bool:
    return bool(NAMESPACED_RE.match(str(key)))


@dataclass
class Split:
    """A preset split by owner: `camera` (the keys camera_settings.apply
    is given — the camera's own, the core's, and a running built-in's under
    today's names), `plugins` (a running plugin's own values, by id, under
    its own names: handed to it), and `kept` (the keys of every plugin not
    running, by id, as the preset holds them: never applied, never
    lost)."""
    camera: Dict[str, Any] = field(default_factory=dict)
    plugins: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    kept: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def split(settings: Mapping[str, Any], running: Callable[[str], bool],
          takes: Callable[[str], bool] = lambda pid: True,
          given: Optional[Mapping[str, Any]] = None) -> Split:
    """Split `settings` (a preset's) by owner. `running(pid)`: the plugin
    runs in this window. `takes(pid)`: a running plugin takes its values
    back (its manifest contributes "presets"); one that does not keeps its
    keys as they are, like a plugin not running. `given`, when the caller
    added keys to the preset's own (defaults), is the preset as saved: only
    its keys are listed as kept."""
    out = Split()
    listed = given if given is not None else settings
    for key, value in settings.items():
        pid = owner(key)
        if pid is None:
            out.camera[key] = value
        elif not is_namespaced(key):
            # A built-in's key under today's name: its owner, running,
            # reads it in place through the camera's provider (design 2.5).
            if running(pid):
                out.camera[key] = value
            elif key in listed:
                out.kept.setdefault(pid, {})[key] = value
        elif running(pid) and takes(pid):
            out.plugins.setdefault(pid, {})[local(key)] = value
        elif key in listed:
            out.kept.setdefault(pid, {})[key] = value
    return out


def merged(new: Mapping[str, Any], old: Mapping[str, Any],
           running: Callable[[str], bool],
           gives: Callable[[str], bool]) -> Dict[str, Any]:
    """The preset to save over `old`, from `new` (the camera's settings now,
    and the running plugins' own values): the keys of every plugin that is
    not running — and of a running one that gave nothing (`gives` False) —
    are `old`'s, exactly, whatever `new` holds for them."""
    def held_back(key: str) -> bool:
        pid = owner(key)
        if pid is None:
            return False
        if not running(pid):
            return True
        return is_namespaced(key) and not gives(pid)

    out = {k: v for k, v in new.items() if not held_back(k)}
    for key, value in old.items():
        if held_back(key):
            out[key] = value
    return out
