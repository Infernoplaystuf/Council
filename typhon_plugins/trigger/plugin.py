"""
typhon.trigger — Triggering: Software and Timer trigger sources, exact and
snapshot capture, the record gate and the triggers CSV
(docs/typhon_plugins_design.md 2.3).

STAGE P1: AN EMPTY ADAPTER. The plugin host finds it by its manifest, loads
it when it is first needed and activates it; it does nothing yet. At stage
P3 it takes over its code, which stays where it is until stage P7 —
council_core/triggered.py and the trigger functions of frame_camera —
reached through typhon_plugin_api, the only Typhon module a plugin imports
(design 7.2).
"""
from __future__ import annotations

from typing import Optional

import typhon_plugin_api as api


class TriggerPlugin:
    """Triggering (feature plugin; wired at stage P3)."""

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None

    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx

    def deactivate(self) -> None:
        self.ctx = None
