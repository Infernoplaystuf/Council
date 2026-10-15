"""
typhon.basler — Basler cameras: finds, opens and describes Basler cameras
through pypylon (docs/typhon_plugins_design.md 2.3).

STAGE P1: AN EMPTY ADAPTER. The plugin host finds it by its manifest, loads
it when it is first needed and activates it; it does nothing yet. At stage
P2 it takes over its code, which stays where it is until stage P7 —
council_core/cameras.py (BaslerBackend, BaslerDevice),
council_core/camera_settings.py (BaslerSettings),
council_core/basler_scan.py and the Basler set-up guide — reached through
typhon_plugin_api, the only Typhon module a plugin imports (design 7.2).
"""
from __future__ import annotations

from typing import Optional

import typhon_plugin_api as api


class BaslerPlugin:
    """Basler cameras (camera plugin; wired at stage P2)."""

    #: Its cameras' backend name, as presets key them (design 3.3).
    backend = "basler"

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None

    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx

    def deactivate(self) -> None:
        self.ctx = None
