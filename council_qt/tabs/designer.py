"""
council_qt.tabs.designer — draw a wireframe, generate a working app.

WIDGET CONSTRUCTION AND EVENT WIRING ONLY, which is the rule the Tk tab states
and very nearly keeps. Every decision is somewhere else:

    designer_scene    snapping, hit-testing, containment, undo
    designer_paint    the 26 renderers
    designer_editor   press / drag / release / escape, and the commands
    designer_form     which rows the inspector shows, and what they mean
    designer_project  new / open / save / generate / detach / review
    gui_layout        infers the grid        gui_spec    validates
    gui_emit          writes                 gui_policy  gates
    gui_runner        previews

WHAT IS DIFFERENT FROM THE TK TAB
The inspector submits only the rows the user edited, so applying to a
multi-selection no longer overwrites the shapes they never looked at.

Generate runs on a worker and reports through the bridge, same as Tk. Review
does too. Open and New take their names from the caller rather than from a
modal typed into a dialog, so the tab itself never blocks — the host supplies
`ask_text` / `ask_choice` / `confirm`, and a test supplies answers directly.
That is also what keeps this file importable with no display.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QPlainTextEdit, QSplitter, QVBoxLayout, QWidget)

from council_core import designer_form as form
from council_core import designer_project as dp
from council_core.designer_editor import Scene
from gui_shapes import PALETTE

from .. import theme
from ..view import ViewHelpers, amp
from ..widgets.designer_canvas import DesignerCanvas, in_scroll_area
from ..widgets.inspector import InspectorView


class DesignerActions:
    """What the Designer tab can ask the application to do.

    A seam, not an abstraction: every method is one call into council_core with
    the vault filled in. It exists so a test can drive the tab against a temp
    vault without a display and without a model.
    """

    def __init__(self, vault_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir or Path.home() / "council_vault")

    def project_dir(self, name: str) -> Optional[Path]:
        if not name:
            return None
        import gui_projects
        return gui_projects.project_path(name, self.vault_dir)

    def list_names(self) -> List[str]:
        return dp.list_names(self.vault_dir)

    def create(self, name: str, mode: str):
        return dp.create(name, mode, self.vault_dir)

    def open_named(self, name: str):
        return dp.open_named(name, self.vault_dir)

    def save(self, name: str, shapes: Sequence[Any]):
        return dp.save(name, shapes, self.vault_dir)

    def apply_window(self, name: str, values, shapes):
        return dp.apply_window(name, values, shapes, self.vault_dir)

    def generate(self, name: str, shapes: Sequence[Any]):
        return dp.generate(name, shapes, self.project_dir(name), self.vault_dir)

    def detach(self, name: str):
        return dp.detach(self.project_dir(name))

    def stop(self, name: str) -> bool:
        import gui_runner
        directory = self.project_dir(name)
        return bool(directory and gui_runner.stop(directory))


class DesignerTab(ViewHelpers, QWidget):
    """A palette, a canvas, an inspector and a log."""

    def __init__(self, window=None, actions: Optional[DesignerActions] = None,
                 ask_text: Optional[Callable] = None,
                 ask_choice: Optional[Callable] = None,
                 confirm: Optional[Callable] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or DesignerActions()
        self._tokens = theme.tokens("dark")
        self.project: str = ""
        self.questions: List[Any] = []
        self._busy = False
        # Supplied by the host. Defaulting to "the user cancelled" rather than
        # to a dialog keeps this file importable, and testable, with no display.
        self.ask_text = ask_text or (lambda *a, **k: None)
        self.ask_choice = ask_choice or (lambda *a, **k: None)
        self.confirm = confirm or (lambda *a, **k: False)

        self._build()
        self._refresh_status()

    # ==================================================================
    # Building
    # ==================================================================
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        blurb = QLabel(
            "Draw a wireframe: pick a widget on the left, drag it on the "
            "canvas, label it, set how it resizes. An empty Frame is a "
            "reserved space — held open at the size you drew, with no model "
            "call, so you can fill it in later. Generate writes a real "
            "multi-file app — ui/ is regenerated every time, app.py is yours "
            "and is never overwritten.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(blurb)

        bar = QHBoxLayout()
        for caption, slot in (("✨ Start with a wizard", self.on_wizard),
                              ("New", self.on_new), ("Open", self.on_open),
                              ("Save", self.on_save),
                              ("⚙ Generate", self.on_generate),
                              ("▶ Run", self.on_run), ("■ Stop", self.on_stop),
                              ("Review with Council", self.on_review),
                              ("Detach", self.on_detach)):
            self._button(bar, caption, slot)
        bar.addStretch(1)
        self.status = QLabel("no project")
        bar.addWidget(self.status)
        outer.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._palette_box())
        split.addWidget(in_scroll_area(self._make_canvas()))
        split.addWidget(self._inspector_box())
        split.setSizes([180, 1100, 260])
        outer.addWidget(split, 1)

        log_box = QGroupBox("Log / clarifications")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(150)
        log_layout.addWidget(self.log_view)
        outer.addWidget(log_box)

    def _palette_box(self) -> QWidget:
        box = QGroupBox("Widgets")
        layout = QVBoxLayout(box)
        self.palette = QListWidget()
        self._palette_keys = list(PALETTE)
        for key in self._palette_keys:
            self.palette.addItem(str((PALETTE[key] or {}).get("label") or key))
        self.palette.currentRowChanged.connect(self._pick_kind)
        layout.addWidget(self.palette, 1)
        self._button(layout, amp("Pointer"), self.on_pointer)
        return box

    def _make_canvas(self) -> DesignerCanvas:
        self.canvas = DesignerCanvas(scene=Scene())
        self.canvas.selection_changed.connect(self._show_selection)
        self.canvas.edited.connect(self._refresh_status)
        return self.canvas

    def _inspector_box(self) -> QWidget:
        box = QGroupBox("Properties")
        layout = QVBoxLayout(box)
        self.inspector = InspectorView()
        self.inspector.applied.connect(self.on_apply_props)
        layout.addWidget(self.inspector)
        return box

    # ==================================================================
    # The canvas and the inspector
    # ==================================================================
    def _pick_kind(self, row: int) -> None:
        if 0 <= row < len(self._palette_keys):
            self.canvas.scene.active_kind = self._palette_keys[row]

    def on_pointer(self) -> None:
        """Disarm. Without it the only way out of "place a widget" mode is to
        place one."""
        self.canvas.scene.active_kind = None
        self.palette.setCurrentRow(-1)

    def _selected_shapes(self) -> List[Any]:
        chosen = set(self.canvas.scene.selection)
        return [s for s in self.canvas.scene.shapes if s.id in chosen]

    def _show_selection(self) -> None:
        """Rebuild the panel for whatever is selected now.

        Nothing selected shows the WINDOW's own controls, which is the only
        place the window's title, size and colour can be edited at all.
        """
        shapes = self._selected_shapes()
        if not shapes:
            self.inspector.show_fields(self._window_fields(),
                                       empty_text="(no project open)")
            return
        fields = list(form.fields_for(shapes))
        if form.shows_single_shape_blocks(shapes):
            fields += form.port_fields(shapes[0])
            fields += form.colour_fields(shapes[0])
        banner = (f"{len(shapes)} shapes selected" if len(shapes) > 1 else "")
        self.inspector.show_fields(fields, banner=banner)

    def _window_fields(self) -> List[form.Field]:
        if not self.project:
            return []
        result = self.actions.open_named(self.project)
        if not result.ok or result.project is None:
            return []
        return form.window_fields(result.project.window,
                                  getattr(result.project, "requires", []) or [])

    def on_apply_props(self, changes: dict) -> None:
        """Apply what the panel submitted.

        Empty is the normal case — the user pressed Apply without changing
        anything — and applying nothing is correct. The Tk panel applies its
        whole form instead, which is how a multi-selection loses the labels of
        every shape but the first.
        """
        if not changes:
            return
        if self._selected_shapes():
            self.canvas._obey(self.canvas.scene.apply_props(changes))
            return
        result = self.actions.apply_window(self.project, changes,
                                           self.canvas.scene.shapes)
        self.log(result.message)
        self._refresh_status()

    # ==================================================================
    # Status and log
    # ==================================================================
    def log(self, text: str) -> None:
        for line in str(text).rstrip().splitlines():
            self.log_view.appendPlainText(line)

    def _refresh_status(self) -> None:
        name = self.project or "no project"
        dirty = " *" if getattr(self.canvas.scene, "dirty", False) else ""
        self.status.setText(f"{name}{dirty}")

    def _load(self, shapes: Sequence[Any]) -> None:
        self.canvas.scene.load(shapes)
        self.canvas.update()
        self._show_selection()
        self._refresh_status()

    # ==================================================================
    # Actions
    # ==================================================================
    def on_new(self) -> None:
        name = self.ask_text("New GUI project", "Project name:")
        if not name:
            return
        mode = self.ask_choice(
            "Import mode",
            "Linked mode lets the app import this app's analysis modules "
            "(image_stats, plot_registry, ...).",
            ["linked", "standalone"])
        if not mode:
            return
        result = self.actions.create(name, mode)
        self.log(result.message)
        if not result.ok:
            return
        self.project = name
        self._load([])

    def on_open(self) -> None:
        names = self.actions.list_names()
        if not names:
            self.log("No GUI projects yet — use New.")
            return
        name = self.ask_choice("Open GUI project", "Project:", names)
        if not name:
            return
        # Stop a preview belonging to the project we are LEAVING. Without this
        # the old app keeps running, invisibly, and Stop no longer reaches it.
        if self.project:
            self.actions.stop(self.project)
        result = self.actions.open_named(name)
        self.log(result.message)
        if not result.ok:
            return
        self.project = name
        self._load(result.shapes)

    def on_wizard(self) -> None:
        """Guided start. Wired by the host, because the wizard is its own
        dialog; without one this says so rather than doing nothing."""
        opener = getattr(self.window, "open_gui_wizard", None)
        if opener is None:
            self.log("wizard unavailable in this build")
            return
        opener(on_done=self.on_wizard_done, log=self.log,
               existing=self.actions.list_names())

    def on_wizard_done(self, result) -> None:
        applied = dp.create_from_wizard(result, self.actions.vault_dir)
        self.log(applied.message)
        if not applied.ok:
            return
        self.project = applied.name
        self._load(applied.shapes)

    def on_save(self) -> None:
        if not self.project:
            self.log("No project open.")
            return
        result = self.actions.save(self.project, self.canvas.scene.shapes)
        self.log(result.message)
        if result.ok:
            self.canvas.scene.mark_saved()
        self._refresh_status()

    def on_generate(self) -> None:
        if not self.project:
            self.log("No project open.")
            return
        self.on_save()
        shapes = list(self.canvas.scene.shapes)
        name = self.project

        def work() -> None:
            result = self.actions.generate(name, shapes)

            def show() -> None:
                self._busy = False
                for line in result.lines:
                    self.log(line)
                self.questions = list(result.questions)
                for line in dp.describe_questions(self.questions):
                    self.log(line)
                self._refresh_status()

            self._to_ui(show)

        self._start("generating…", work, name="designer-generate")

    def on_review(self) -> None:
        """Advisory critique of the generated ui/. NEVER edits code."""
        directory = self.actions.project_dir(self.project)
        prompt = dp.review_prompt(directory) if directory else ""
        if not prompt:
            self.log("Generate the project first.")
            return
        critique = getattr(self.window, "review_with_council", None)
        if critique is None:
            self.log("Council review unavailable in this build")
            return

        def work() -> None:
            try:
                text = critique(prompt)
            except Exception as exc:
                text = f"review failed: {exc!r}"

            def show() -> None:
                self._busy = False
                self.log("── Council review (advisory only) ──")
                self.log(text)
                self._refresh_status()

            self._to_ui(show)

        self._start("review…", work, name="designer-review")

    def on_run(self) -> None:
        directory = self.actions.project_dir(self.project)
        if not directory:
            self.log("No project open.")
            return
        import gui_projects
        import gui_runner
        try:
            manifest = gui_projects.load_manifest(directory)
            requires = self.actions.open_named(self.project).project.requires
        except Exception as exc:
            self.log(f"could not start preview: {exc}")
            return
        # The policy gate is ENFORCED here, not merely reported: it used to say
        # "the app will not run it" while Run launched it anyway.
        gui_runner.run_checked(directory, python_spec=manifest.python,
                               mode=manifest.mode, requires=requires,
                               log=self.log,
                               call_soon=lambda fn: self._to_ui(fn))

    def on_stop(self) -> None:
        if self.project and self.actions.stop(self.project):
            self.log("preview stopped")

    def on_detach(self) -> None:
        if not self.project:
            return
        if not self.confirm(
                "Detach project",
                "Detaching merges ui/ into the project and disables "
                "regeneration from the wireframe.\n\nThis is ONE WAY. "
                "Continue?"):
            return
        self.log(self.actions.detach(self.project).message)

    # ==================================================================
    def _start(self, status: str, work, *, name: str) -> None:
        if self._busy:
            self.log("Already working — wait for it to finish.")
            return
        self._busy = True
        self.status.setText(status)
        threading.Thread(target=work, name=name, daemon=True).start()


def build_designer(window) -> QWidget:
    """Factory for the tab registry."""
    return DesignerTab(window)
