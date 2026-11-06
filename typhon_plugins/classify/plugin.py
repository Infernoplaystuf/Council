"""
typhon.classify — Classification: the classifier in a pop-out window of its
own, opened from its button on the toolbar strip, and the first plugin that
takes datasets (stage P4 row 4; Andy, 2 November 2026).

WHAT MOVED, AND WHAT DID NOT
The 32 classifier controls left the main window of "Typhon (plugins)"
(examples/gui/typhon_plugins.gspec) and are this plugin's panel
(panel.ClassifierPanel). Every press calls the classifier library
(frame_classes) exactly as today's script links do — the same function, the
same inputs, the same outputs (a test holds the panel's links to the
classic typhon.gspec's) — and the model dropdown, its filter, the
"classified with" line and Delete's second press are today's
ClassifierPicker. The library, its store and its file formats are
unchanged; frame_classes stays where it is until stage P7 (design 7.2),
reached through typhon_plugin_api.builtin.

OPTIONAL
A project that does not run this plugin (switched off in the Plugins
window) has no Classification button, imports nothing of the classifier,
and needs no sklearn: the manifest declares it, and without it the plugin
is shown as unable to run, with the reason.

DATASETS
"datasets" in its manifest's contributes: a dataset sent from the Datasets
window opens the pop-out with that dataset picked, ready to mark every frame
in it as one class or to classify them all. take_dataset only does that and
returns (the watchdog allows any plugin call 2 s); the work is started by a
press in the panel.
"""
from __future__ import annotations

from typing import Any, Optional

import typhon_plugin_api as api


class ClassifyPlugin:
    """Classification (feature plugin, one per window)."""

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None
        #: The panel once its window was first opened.
        self.panel_widget: Any = None

    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx

    def deactivate(self) -> None:
        panel, self.panel_widget = self.panel_widget, None
        if panel is not None:
            try:
                panel.close_down()
            except Exception:                             # noqa: BLE001
                pass
        self.ctx = None

    # -- the screen ----------------------------------------------------------
    def panel(self, parent: Any = None) -> Any:
        """The pop-out's inner widget, built on first open."""
        from .panel import ClassifierPanel
        self.panel_widget = ClassifierPanel(self, parent)
        return self.panel_widget

    def capture_folder(self) -> str:
        """The main window's image folder (the frames Mark, Predict and
        Classify all act on), or ""."""
        ctx = self.ctx
        folder = ctx.image_folder() if ctx is not None else None
        return str(folder) if folder else ""

    def current_frame(self) -> str:
        """The saved frame the main window's slider shows, or ""."""
        ctx = self.ctx
        frame = ctx.current_frame() if ctx is not None else None
        return str(frame) if frame else ""

    # -- datasets ------------------------------------------------------------
    def take_dataset(self, dataset: api.Dataset) -> str:
        ctx = self.ctx
        if ctx is None or not ctx.show_panel() or self.panel_widget is None:
            return "Classification could not open its window"
        self.panel_widget.use_dataset(dataset.name)
        here = len(dataset.paths)
        gone = f" ({dataset.missing} no longer on disk)" if dataset.missing \
            else ""
        return (f"'{dataset.name}' is in Classification: {here} frames"
                f"{gone}. Pick a class and press Mark dataset, or press "
                f"Classify dataset.")
