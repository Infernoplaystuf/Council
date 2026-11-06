"""
typhon.send_trigger — Send trigger: a window with a drop-down of the rig's
cameras and a button that sends one trigger to the camera picked (Andy, 5
November 2026: "a button inside of Typhon to send a trigger to a camera
selected from a drop down … a plugin so it can be optionally added").

OPTIONAL
Built in, but not in a new project's plugins (OPTIONAL_BUILTINS in
council_core/plugin_host.py): tick it in Plugins… and its "Send trigger"
button appears on the toolbar strip. Switched off, it loads nothing.

WHAT A PRESS DOES
The same as Capture now (F9) on that camera, whichever camera the main
window shows: one trigger of the triggered run in progress on it, through
the triggering plugin (context.capture_now). In an exact run the camera
exposes at once (Software or Timer source); in a snapshot run the next
frames are saved; in a record run recording starts (or stops). With no
triggered run on that camera the triggering plugin's words say what to set
up. The run's trigger file counts the press as any other.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

import typhon_plugin_api as api


class SendTriggerPlugin:
    """Send trigger (feature plugin, rig scope)."""

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None

    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx

    def deactivate(self) -> None:
        self.ctx = None

    def cameras(self) -> List[Tuple[str, str]]:
        """(slot name, what the drop-down shows) of each connected camera,
        in the rig's order."""
        ctx = self.ctx
        if ctx is None:
            return []
        out = []
        for name, handle in ctx.cameras().items():
            if not handle.connected:
                continue
            label = str(getattr(handle.info, "label", "") or "")
            out.append((str(name), f"{name} — {label}" if label else
                        str(name)))
        return out

    def active(self) -> Optional[str]:
        return self.ctx.active_camera() if self.ctx is not None else None

    def send(self, camera: str) -> str:
        """One trigger to `camera`: what happened, in words."""
        ctx = self.ctx
        if ctx is None:
            return "Send trigger is switched off."
        if not camera or camera not in ctx.cameras():
            return "Pick a connected camera first."
        said = ctx.capture_now(camera)
        if said is None:
            return ("Send trigger needs the Triggering plugin: switch it on "
                    "in Plugins….")
        return f"{camera}: {said}"

    def panel(self, parent: Any = None) -> Any:
        """The window's inner widget, built on first open."""
        from .panel import SendTriggerPanel
        return SendTriggerPanel(self, parent)
