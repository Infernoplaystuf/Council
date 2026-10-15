"""
typhon.prophesee — Prophesee event cameras: finds, opens and describes
Prophesee event cameras (EVK4) through OpenEB (docs/typhon_plugins_design.md
2.3).

STAGE P1: AN EMPTY ADAPTER. The plugin host finds it by its manifest, loads
it when it is first needed and activates it; it does nothing yet. At stage
P2 it takes over its code, which stays where it is until stage P7 —
council_core/cameras.py (EvkBackend, EvkDevice),
council_core/camera_settings.py (EvkSettings),
council_core/event_playback.py and the raw view — reached through
typhon_plugin_api, the only Typhon module a plugin imports (design 7.2).
"""
from __future__ import annotations

from typing import Optional

import typhon_plugin_api as api


class PropheseePlugin:
    """Prophesee event cameras (camera plugin; wired at stage P2)."""

    #: Its cameras' backend name, as presets key them (design 3.3).
    backend = "prophesee"

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None

    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx

    def deactivate(self) -> None:
        self.ctx = None
