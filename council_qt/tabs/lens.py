"""
council_qt.tabs.lens — parallel critique, ported.

The smallest complete tab in the app: 26 toolkit lines in Tk, three methods,
no new dependencies. Picked first of phase 7 for exactly that reason — it is
the shortest path from "the pattern works for the Vault and the Council" to
"the pattern works for an ordinary tab".

Everything that is not a widget lives in `council_core.lens`, including the
three defects that module's header records: a checkbox for a role that cannot
exist, a "Done — n responded" that counts the roles ASKED, and an empty-input
path that returns in silence so the button looks dead.

The one thing this view adds beyond the Tk tab: a role with no model behind it
is DISABLED and says so in its tooltip, rather than being offered and then
answering "(Role not loaded)". A control that cannot work should not look like
one that can.
"""
from __future__ import annotations

import threading
from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QPlainTextEdit, QVBoxLayout, QWidget)

from council_core import lens as lens_core

from .. import theme
from ..view import ViewHelpers

#: How many role checkboxes fit on a row before wrapping. Eleven in one line
#: forces a horizontal scrollbar on a narrow window; the Tk tab packs them all
#: side by side and simply overflows.
ROLES_PER_ROW = 6


class LensActions:
    """What the Lens tab can ask the application to do."""

    def __init__(self, models=None):
        self._models = models
        self._problem = ""

    def models(self):
        """(personalities, problem). Loaded lazily and kept."""
        from council_core import council_turn

        if self._models is None and not self._problem:
            from council_core import paths
            self._models, self._problem = council_turn.load_personalities(
                paths.vault_dir())
        return self._models, self._problem

    def available_roles(self) -> Dict[str, bool]:
        models, _ = self.models()
        if models is None:
            return {role: False for role, _d in lens_core.LENS_ROLES}
        return lens_core.available_roles(models)

    def run(self, content: str, roles, *, on_result=None):
        models, problem = self.models()
        if models is None:
            return lens_core.LensResult(False, problem)
        return lens_core.run_lens(models, content, roles, on_result=on_result)


class LensTab(ViewHelpers, QWidget):
    """Roles, content, and a pane of critiques."""

    def __init__(self, window=None, actions: Optional[LensActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or LensActions()
        self._tokens = theme.tokens("dark")
        self._boxes: Dict[str, QCheckBox] = {}
        self._running = False

        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        header = QHBoxLayout()
        title = QLabel("Council Lens")
        title.setStyleSheet(
            f"color: {self._tokens['accent']}; font-weight: bold; "
            f"font-size: 11pt;")
        header.addWidget(title)
        subtitle = QLabel("Paste any content — get simultaneous parallel "
                          "critique from every role you pick.")
        subtitle.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        header.addWidget(subtitle)
        header.addStretch(1)
        outer.addLayout(header)

        outer.addWidget(self._roles_box())
        outer.addWidget(self._input_box())
        outer.addLayout(self._action_row())
        outer.addWidget(self._output_box(), 1)

    def _roles_box(self) -> QGroupBox:
        box = QGroupBox("Roles to include")
        grid = QGridLayout(box)
        available = self.actions.available_roles()
        for index, (role, default_on) in enumerate(lens_core.LENS_ROLES):
            check = QCheckBox(role.capitalize())
            usable = available.get(role, False)
            check.setChecked(default_on and usable)
            check.setEnabled(usable)
            if not usable:
                # The Tk tab offers `musician` and there is no such
                # personality anywhere, so ticking it answers "(Role not
                # loaded)" every time. A control that cannot work should not
                # look like one that can.
                check.setToolTip(
                    f"No {role} model is loaded in this build, so it has "
                    f"nothing to say.")
            grid.addWidget(check, index // ROLES_PER_ROW,
                           index % ROLES_PER_ROW)
            self._boxes[role] = check
        return box

    def _input_box(self) -> QGroupBox:
        box = QGroupBox("Content to review")
        layout = QVBoxLayout(box)
        self.input = QPlainTextEdit()
        self.input.setMinimumHeight(140)
        self.input.setPlaceholderText(
            "Paste a draft, a spec, a chunk of code…")
        self.input.textChanged.connect(self.on_content_changed)
        layout.addWidget(self.input)
        self.truncation = QLabel("")
        self.truncation.setStyleSheet(f"color: {self._tokens['warning']};")
        self.truncation.hide()
        layout.addWidget(self.truncation)
        return box

    def _action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.run_btn = self._button(row, "▶ Run Lens", self.on_run)
        self._button(row, "Clear All", self.on_clear)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(self.status)
        row.addStretch(1)
        return row

    def _output_box(self) -> QGroupBox:
        box = QGroupBox("Role critiques")
        layout = QVBoxLayout(box)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        layout.addWidget(self.output)
        return box

    # ------------------------------------------------------------------
    def selected_roles(self):
        """The ticked roles, in layout order.

        Order matters: it decides nothing about the answers, but it decides
        what a user sees first, and LENS_ROLES is the order they ticked them
        in.
        """
        return [role for role, _default in lens_core.LENS_ROLES
                if self._boxes[role].isChecked()]

    def on_content_changed(self) -> None:
        """Say when only part of the content will be read.

        The Tk tab slices to 3,000 characters silently, so a user who pastes a
        long document wonders why the back half was ignored.
        """
        note = lens_core.truncation_note(self.input.toPlainText())
        self.truncation.setText(note)
        self.truncation.setVisible(bool(note))

    def on_clear(self) -> None:
        self.input.clear()
        self.output.clear()
        self.status.setText("")

    def on_run(self) -> None:
        if self._running:
            self.status.setText("A lens run is already going.")
            return

        content = self.input.toPlainText()
        roles = self.selected_roles()
        problem = lens_core.check_request(content, roles)
        if problem:
            # The Tk version returns silently on empty content, so the button
            # looks broken rather than refusing.
            self.status.setText(problem)
            return

        self._running = True
        self.run_btn.setEnabled(False)
        self.output.clear()
        self.status.setText(f"Running {len(roles)} roles in parallel…")

        def work() -> None:
            def landed(role: str, text: str) -> None:
                self._to_ui(lambda role=role, text=text:
                            self.append_critique(role, text))

            result = self.actions.run(content, roles, on_result=landed)
            self._to_ui(lambda: self.finish(result))

        threading.Thread(target=work, name="lens-run", daemon=True).start()

    def append_critique(self, role: str, text: str) -> None:
        """One role's answer, as it lands.

        Append-only and shown immediately: on local weights the difference
        between seeing the first critique at five seconds and all of them at
        sixty is the difference between a live feature and a hung one.
        """
        self.output.appendPlainText(lens_core.format_critique(role, text))

    def finish(self, result) -> None:
        self._running = False
        self.run_btn.setEnabled(True)
        self.status.setText(result.message)


def build_lens(window) -> QWidget:
    """Factory for the tab registry."""
    return LensTab(window)
