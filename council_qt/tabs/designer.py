"""
council_qt.tabs.designer — draw a wireframe, generate a working app.

WIDGET CONSTRUCTION AND EVENT WIRING ONLY, which is the rule the Tk tab states
and very nearly keeps. Every decision is somewhere else:

    designer_scene    snapping, hit-testing, containment, undo
    designer_paint    the 26 renderers
    designer_editor   press / drag / release / escape, and the commands
    designer_form     which rows the inspector shows, and what they mean
    designer_project  new / open / save / generate / detach / review
    designer_examples new from a shipped example / export .gspec
    gui_layout        infers the grid        gui_spec    validates
    gui_emit          writes                 gui_policy  gates
    gui_runner        previews

WHAT IS DIFFERENT FROM THE TK TAB
The inspector submits only the rows the user edited, so applying to a
multi-selection no longer overwrites the shapes they never looked at.

Generate runs on a worker and reports through the bridge, same as Tk. Review
does too, and so does Describe it — plain English in, a wireframe on the
canvas out, through gui_describe, which checks every shape the model proposes
against the same validator Generate uses before any of it reaches the canvas.

New asks which toolkit to generate into, because it can only be asked once:
app.py is written in it and never rewritten. Open and New take their names from the caller rather than from a
modal typed into a dialog, so the tab itself never blocks — the host supplies
`ask_text` / `ask_choice` / `confirm`, and a test supplies answers directly.
That is also what keeps this file importable with no display.

EXAMPLES COME IN, AND GO BACK OUT
"New from example…" builds a shipped example (Typhon, the capture forms) into
a new project through the same pipeline run_example_gui runs, on a worker,
and opens it — so an example no longer needs a command line to be edited
here. It never replaces a project: a taken name is refused. "Export .gspec…"
writes the open design to a file the user picks, in the examples' own format,
asking before it replaces one. Both dialogs come from the host too
(`ask_example` / `ask_save_path`), for the same reasons.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QPlainTextEdit, QSplitter, QVBoxLayout, QWidget)

from council_core import designer_examples as dx
from council_core import designer_form as form
from council_core import paths
from council_core import designer_project as dp
from council_core.wizard import TOOLKITS
from council_core.designer_editor import Scene
from gui_shapes import PALETTE

from .. import theme
from ..view import ViewHelpers, amp
from ..widgets.designer_canvas import DesignerCanvas, in_scroll_area
from ..widgets.inspector import InspectorView
from ..widgets.runwith import RunWithBox


class DesignerActions:
    """What the Designer tab can ask the application to do.

    A seam, not an abstraction: every method is one call into council_core with
    the vault filled in. It exists so a test can drive the tab against a temp
    vault without a display and without a model.
    """

    def __init__(self, vault_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self._models = None

    def project_dir(self, name: str) -> Optional[Path]:
        if not name:
            return None
        import gui_projects
        return gui_projects.project_path(name, self.vault_dir)

    def list_names(self) -> List[str]:
        return dp.list_names(self.vault_dir)

    def create(self, name: str, mode: str, toolkit: str = "tk"):
        return dp.create(name, mode, self.vault_dir, toolkit)

    def toolkit_label(self, name: str) -> str:
        return dp.toolkit_label(self.project_dir(name))

    def describe(self, name: str, text: str):
        """Plain English -> DescribeResult. Blocking; a worker calls it."""
        return dp.describe(text, self.project_dir(name))

    def review(self, prompt: str) -> str:
        """The Council's critique. Blocking; a worker calls it.

        The personalities are loaded on first use and kept — loading them maps
        model files, and a user who never presses Review should not pay for
        it.
        """
        if self._models is None:
            from council_core import council_turn
            models, problem = council_turn.load_personalities(self.vault_dir)
            if models is None:
                return f"review unavailable: {problem}"
            self._models = models
        return dp.review(prompt, self._models)

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

    def example_problem(self, answers) -> str:
        return dx.problem(answers, self.vault_dir)

    def build_example(self, answers):
        """An example -> a new project. Blocking; a worker calls it."""
        return dx.build(answers, self.vault_dir)

    def export_default(self, name: str):
        return dx.export_default(self.project_dir(name))

    def export_gspec(self, name: str, shapes: Sequence[Any], dest):
        return dx.export_gspec(name, shapes, self.vault_dir, dest)

    def stop(self, name: str) -> bool:
        import gui_runner
        directory = self.project_dir(name)
        return bool(directory and gui_runner.stop(directory))


class DesignerTab(ViewHelpers, QWidget):
    """A palette, a canvas, an inspector and a log."""

    def __init__(self, window=None, actions: Optional[DesignerActions] = None,
                 ask_text: Optional[Callable] = None,
                 ask_choice: Optional[Callable] = None,
                 confirm: Optional[Callable] = None,
                 ask_example: Optional[Callable] = None,
                 ask_save_path: Optional[Callable] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or DesignerActions()
        self._tokens = theme.tokens("dark")
        self.project: str = ""
        #: "Tk" / "Qt" for the open project. Read when a project is loaded, not
        #: per status refresh: it comes off disk, and the status refreshes on
        #: every drag.
        self._toolkit: str = ""
        self.questions: List[Any] = []
        self._busy = False
        # Supplied by the host. Defaulting to "the user cancelled" rather than
        # to a dialog keeps this file importable, and testable, with no display.
        self.ask_text = ask_text or (lambda *a, **k: None)
        self.ask_choice = ask_choice or (lambda *a, **k: None)
        self.confirm = confirm or (lambda *a, **k: False)
        #: () -> ExampleAnswers or None. The "New from example" dialog.
        self.ask_example = ask_example or (lambda *a, **k: None)
        #: (title, folder, file name) -> a path, or "" for cancelled.
        self.ask_save_path = ask_save_path or (lambda *a, **k: "")

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
                              ("New", self.on_new),
                              ("New from example…", self.on_new_from_example),
                              ("Open", self.on_open),
                              ("Save", self.on_save),
                              ("Export .gspec…", self.on_export),
                              ("⚙ Generate", self.on_generate),
                              ("▶ Run", self.on_run), ("■ Stop", self.on_stop),
                              ("Review with Council", self.on_review),
                              ("Detach", self.on_detach)):
            self._button(bar, caption, slot)
        # Which Python runs the preview — per project, in its manifest. A
        # camera app needs its SDK's environment, not the Council's own.
        self.runwith = RunWithBox(lambda: self.actions.project_dir(self.project))
        self.runwith.logged.connect(self.log)
        bar.addSpacing(12)
        bar.addWidget(self.runwith)
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

        bottom = QHBoxLayout()
        bottom.addWidget(self._describe_box(), 1)
        log_box = QGroupBox("Log / clarifications")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(150)
        log_layout.addWidget(self.log_view)
        bottom.addWidget(log_box, 1)
        outer.addLayout(bottom)

    def _describe_box(self) -> QWidget:
        """Plain English in, a wireframe out. The model proposes; gui_describe
        checks every shape before any of it reaches the canvas."""
        box = QGroupBox("Describe it")
        layout = QVBoxLayout(box)
        self.describe_view = QPlainTextEdit()
        self.describe_view.setPlaceholderText(
            "e.g. A login window: a username box, a password box, and "
            "Sign in / Cancel buttons along the bottom.")
        self.describe_view.setMaximumHeight(110)
        layout.addWidget(self.describe_view)
        self._button(layout, "✨ Draw it", self.on_describe)
        return box

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
        toolkit = f" [{self._toolkit}]" if self.project and self._toolkit else ""
        dirty = " *" if getattr(self.canvas.scene, "dirty", False) else ""
        self.status.setText(f"{name}{toolkit}{dirty}")

    def _load(self, shapes: Sequence[Any]) -> None:
        self._toolkit = (self.actions.toolkit_label(self.project)
                         if self.project else "")
        self.canvas.scene.load(shapes)
        self.canvas.update()
        self._show_selection()
        # The interpreter is per PROJECT, so every path that changes which
        # project is open has to re-read it. The Tk tab calls sync() from four
        # separate places for this reason.
        self.runwith.sync()
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
        # Asked HERE because it cannot be asked later: app.py is written once,
        # in this toolkit, and Generate refuses to put the other toolkit's ui/
        # beside it. Cancel creates nothing, the same as the two above.
        toolkit = self.ask_choice(
            "Toolkit",
            "Which toolkit the generated app is written in: qt (PySide6) or "
            "tk (tkinter). This cannot be changed after the project is made.",
            list(TOOLKITS))
        if not toolkit:
            return
        result = self.actions.create(name, mode, toolkit)
        self.log(result.message)
        if not result.ok:
            return
        self._leave_project()
        self.project = name
        self._load([])

    def _leave_project(self) -> None:
        """Stop a preview belonging to the project we are LEAVING. Without
        this the old app keeps running, invisibly, and Stop no longer reaches
        it — the tab is pointing somewhere else. New and the wizard switch
        projects too, and until the real dialogs were wired New never ran at
        all, so only Open did this."""
        if self.project:
            self.actions.stop(self.project)

    def on_open(self) -> None:
        names = self.actions.list_names()
        if not names:
            self.log("No GUI projects yet — use New.")
            return
        name = self.ask_choice("Open GUI project", "Project:", names)
        if not name:
            return
        self._leave_project()
        result = self.actions.open_named(name)
        self.log(result.message)
        if not result.ok:
            return
        self.project = name
        self._load(result.shapes)

    def on_new_from_example(self) -> None:
        """Build a shipped example into a NEW project, then open it.

        The answers are checked again here, not only in the dialog: the host
        may supply them from anywhere, and a name that was free when the
        dialog opened may not be now. The build itself refuses a taken name
        as well — nothing on this path can replace a project.

        The build is a whole generate (layout, spec, seven files), so it runs
        on a worker and the tab stays usable; the new project is opened when
        it finishes.
        """
        if self._busy:
            # Before the dialog, not after it: answering four questions only
            # to be told "Already working" wastes them.
            self.log("Already working — wait for it to finish.")
            return
        answers = self.ask_example()
        if not answers:
            return
        problem = self.actions.example_problem(answers)
        if problem:
            self.log(problem)
            return

        def work() -> None:
            try:
                result = self.actions.build_example(answers)
            except Exception as exc:                     # noqa: BLE001
                result = dx.ExampleBuild(
                    lines=[f"building the example failed: {exc!r}"])

            def show() -> None:
                self._busy = False
                for line in result.lines:
                    self.log(line)
                if result.ok:
                    self._open_built(result.name)
                self._refresh_status()

            self._to_ui(show)

        self._start("building example…", work, name="designer-example")

    def _open_built(self, name: str) -> None:
        """Open a project a build just finished — unless that would throw
        away edits made while it was building. The build took seconds, and
        the user was free to keep drawing on the project that was open."""
        if getattr(self.canvas.scene, "dirty", False):
            if not self.confirm(
                    "Open the new project?",
                    f"{name} is built. The canvas"
                    f"{' (' + self.project + ')' if self.project else ''} has "
                    f"unsaved changes — open {name} anyway and lose them? "
                    f"(Save first to keep them.)"):
                self.log(f"{name} is built — Open it when you are ready.")
                return
        self._leave_project()
        opened = self.actions.open_named(name)
        self.log(opened.message)
        if not opened.ok:
            return
        self.project = name
        self._load(opened.shapes)

    def on_export(self) -> None:
        """Write the open design to a .gspec the user picks.

        Nothing is written without a path from the save dialog, and an
        existing file is replaced only after a yes — the offered destination
        for an example is its own file in examples/gui/, which is exactly the
        file a reflex click would destroy. What is written is the canvas as
        shown; everything else comes from the project.
        """
        if not self.project:
            self.log("No project open.")
            return
        folder, filename = self.actions.export_default(self.project)
        chosen = self.ask_save_path("Export .gspec", folder, filename)
        if not chosen:
            return
        dest = Path(chosen)
        if not dest.suffix:
            dest = dest.with_name(dest.name + ".gspec")
        if dest.exists() and not self.confirm(
                "Replace the file?",
                f"{dest} already exists.\n\nReplace it with the "
                f"{self.project} wireframe? The file there now is not "
                f"kept."):
            self.log(f"not exported — {dest.name} was left as it was")
            return
        # The live shapes, not scene.export()'s deep copy: this runs to the
        # end on the GUI thread, so nothing can drag them mid-write, and the
        # copy was a fifth of the handler's time on Typhon's 58 shapes
        # (profiled). export_gspec only reads them.
        result = self.actions.export_gspec(
            self.project, list(self.canvas.scene.shapes), dest)
        self.log(result.message)
        if result.ok and getattr(self.canvas.scene, "dirty", False):
            self.log("note: exported the canvas as shown — the project "
                     "itself still has unsaved changes; Save keeps them "
                     "there too.")

    def on_wizard(self) -> None:
        """Guided start.

        The wizard WRITES NOTHING — it hands back a layout and `on_wizard_done`
        applies it — so cancelling leaves no half-made project behind.

        A host may substitute its own by setting `open_gui_wizard`; that is the
        seam the tests use, and it is why this never constructs a dialog when
        one has been supplied.
        """
        opener = getattr(self.window, "open_gui_wizard", None)
        if opener is None:
            from ..wizard import open_wizard
            opener = open_wizard
        self._wizard = opener(parent=self, on_done=self.on_wizard_done,
                              log=self.log,
                              existing=self.actions.list_names())

    def on_wizard_done(self, result) -> None:
        """Apply a finished wizard.

        Always a NEW project, which is what makes the destructive `load` safe
        here: there is nothing on the canvas to destroy.
        """
        applied = dp.create_from_wizard(result, self.actions.vault_dir)
        self.log(applied.message)
        if not applied.ok:
            return
        self._leave_project()
        self.project = applied.name
        self._load(applied.shapes)
        self.canvas.scene.mark_saved()
        self._refresh_status()

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
        # A DEEP copy. The worker reads these while the user may still be
        # dragging, and a drag moves the live Shape objects in place.
        shapes = self.canvas.scene.export()
        name = self.project

        def work() -> None:
            # Anything that escapes here would leave _busy set forever, and
            # every later job would say "Already working" with nothing running.
            try:
                result = self.actions.generate(name, shapes)
            except Exception as exc:                     # noqa: BLE001
                result = dp.GenerateResult(
                    lines=[f"generate failed: {exc!r}"])

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
        # A host may supply its own; otherwise a one-round Council of the
        # coder and the writer, which is what the Tk shell ran.
        critique = (getattr(self.window, "review_with_council", None)
                    or self.actions.review)

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

    def on_describe(self) -> None:
        """Turn the description into a wireframe on this project's canvas.

        Runs on a worker: it is one to three model calls. The text is read
        HERE, on the GUI thread, and the result is applied in show() — as one
        undoable step, so a description the user does not like is one Undo
        away from the drawing they had.
        """
        text = self.describe_view.toPlainText().strip()
        if not text:
            self.log("Describe the window first — what is in it, and roughly "
                     "where.")
            return
        if not self.project:
            self.log("Open or create a project first — the description is "
                     "drawn onto its canvas, in its toolkit.")
            return
        name = self.project

        def work() -> None:
            try:
                result, failure = self.actions.describe(name, text), ""
            except Exception as exc:                     # noqa: BLE001
                result, failure = None, f"describe failed: {exc!r}"

            def show() -> None:
                self._busy = False
                self._apply_description(name, result, failure)
                self._refresh_status()

            self._to_ui(show)

        self._start("describing…", work, name="designer-describe")

    def _apply_description(self, name: str, result, failure: str) -> None:
        """Put a finished description on the canvas, or say why not."""
        if failure:
            self.log(failure)
            return
        for line in getattr(result, "notes", None) or []:
            self.log(f"note: {line}")
        if not getattr(result, "ok", False):
            self.log("Could not turn that into a wireframe:")
            for line in getattr(result, "errors", None) or []:
                self.log(f"  {line}")
            return
        if name != self.project:
            # The user opened another project while the model was thinking.
            # Drawing the description onto THAT one would be a surprise edit.
            self.log(f"The description was for {name}, which is no longer "
                     f"open — not applied.")
            return
        # A drag the result arrived in the middle of is abandoned first: the
        # confirm below is modal and swallows the mouse release, which would
        # leave the gesture armed over a scene it no longer describes.
        self.canvas._obey(self.canvas.scene.escape())
        if self.canvas.scene.shapes and not self.confirm(
                "Replace the wireframe?",
                "The canvas already has a wireframe on it. Replace it with "
                "the described one? (Undo brings it back.)"):
            self.log("Kept the current wireframe — the description was not "
                     "applied.")
            return
        self.canvas._obey(self.canvas.scene.replace_all(result.shapes))
        attempts = getattr(result, "attempts", 0)
        self.log(f"drew {len(result.shapes)} shape(s) from the description"
                 + (f" ({attempts} model call(s))" if attempts else "")
                 + " — Save to keep it, Undo to take it back.")
        title = (getattr(result, "window", None) or {}).get("title")
        if title:
            self.log(f"suggested window title: {title!r} (set it in the "
                     f"window panel — click an empty part of the canvas)")

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
    """Factory for the tab registry — the tab, with REAL dialogs.

    The tab's own defaults mean "the user cancelled", which is right for a
    test and was wrong in production: nothing supplied these, so New and Open
    did nothing at all and Detach was always declined. The wizard was the only
    way to make a project.
    """
    from .. import dialogs

    tab = DesignerTab(window)
    tab.ask_text = lambda title, prompt: dialogs.askstring(
        title, prompt, parent=tab)
    tab.ask_choice = lambda title, prompt, choices: dialogs.askchoice(
        title, prompt, choices, parent=tab)
    tab.confirm = lambda title, message: dialogs.askyesno(
        title, message, parent=tab)
    # Both answer "cancelled" under COUNCIL_NO_DIALOGS rather than opening a
    # modal nobody can close.
    from ..widgets.example_dialog import ask_example, ask_export_path
    tab.ask_example = lambda: ask_example(parent=tab,
                                          vault_dir=tab.actions.vault_dir)
    tab.ask_save_path = lambda title, folder, filename: ask_export_path(
        tab, title, folder, filename)
    return tab
