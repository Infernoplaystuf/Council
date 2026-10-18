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
    # the capture set-up a frame camera's trigger is confirmed with (its
    # types join the API itself at stage P3, design 2.5)
    "CaptureSetup": ("council_core.triggered", "CaptureSetup"),
    "MODE_EXACT": ("council_core.triggered", "MODE_EXACT"),
    "SOFTWARE": ("council_core.triggered", "SOFTWARE"),
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
