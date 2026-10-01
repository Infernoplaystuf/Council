"""
council_qt.widgets.wiring — the Designer's Wiring group, in Qt.

What the selected button RUNS: a module, a function in it, the ports whose
values it is handed, and the ports its result fills. Until this existed the
only way to set any of that was to edit the .gspec's JSON by hand — the
Properties panel had no row for it, and the one row that looked like it
might (a button's "command") was read by nothing.

EVERY DECISION IS IN council_core.designer_wiring. Which modules to offer,
what functions each one has (read by PARSING its source — frame_camera is
never imported just to fill a dropdown), which ports can be read or filled,
what "match parameters" picks and what is wrong with a link are all answered
there, with no display. This file builds the controls and reads them back.

A GROUP OF ITS OWN, NOT ROWS IN THE INSPECTOR
The inspector is a flat list of `designer_form.Field` rows, one control each.
A link is not that shape: an ordered list with move up/down, and a variable
number of output rows each holding two controls. Forcing it into Field rows
would have meant new Field kinds that only this group uses, so it sits beside
the inspector instead — and a change to the inspector's rows (a Geometry
group, say) never has to touch this file.

NOTHING IS SAVED UNTIL APPLY, AND APPLY REFUSES A LINK THAT CANNOT GENERATE
The problems are shown as the user edits, in plain words, and Apply with any
of them on screen does nothing but say so again. A link Generate would refuse
is not written into the scene, because the next person to find it is the user
reading Generate's log and working backwards to which button it meant.

"WRITE IT WITH THE MODEL…" READS ITS SIGNATURE FROM THESE SAME ROWS
The Inputs list is the function's parameters and the Outputs rows are its
result keys and where each is shown — the person decides what goes in and
out, the model writes what happens in between. The group only builds the
request (`write_requested`) and shows CodeReviewDialog; the tab runs the job
and nothing is written until the review is accepted.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                               QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QPlainTextEdit, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from council_core import designer_wiring as wiring
from council_core.designer_scene import THEME

from ..view import amp


class OutputRow(QWidget):
    """One output: which port shows it, and which key of the result."""

    changed = Signal()
    removed = Signal(object)

    def __init__(self, ports: Sequence[str], port: str = "", key: str = "",
                 keys: Sequence[str] = (), parent: Optional[QWidget] = None,
                 whole: bool = False):
        super().__init__(parent)
        #: This row is a link's older single `output` — the port is set to
        #: the WHOLE result, not result[key]. Left blank, the key keeps
        #: meaning that (see WiringView.link).
        self.whole = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.port = QComboBox()
        self.key = QComboBox()
        self.key.setEditable(True)
        self.key.setToolTip("The key of the dict the function returns")
        drop = QPushButton("✕")
        drop.setFixedWidth(28)
        drop.setToolTip("Remove this output")
        row.addWidget(self.port, 3)
        row.addWidget(QLabel("←"))
        row.addWidget(self.key, 2)
        row.addWidget(drop)
        self.reset(ports, port, key, keys, whole)
        self.port.currentTextChanged.connect(lambda _t: self.changed.emit())
        self.key.editTextChanged.connect(lambda _t: self.changed.emit())
        drop.clicked.connect(lambda: self.removed.emit(self))

    def reset(self, ports: Sequence[str], port: str = "", key: str = "",
              keys: Sequence[str] = (), whole: bool = False) -> None:
        """Show another output in this row. Rows are REUSED across
        selections: building one (an editable combo makes a line edit and a
        completer) cost ~1 ms, which is the whole budget of a refresh."""
        self.whole = bool(whole)
        self.key.lineEdit().setPlaceholderText(
            "(whole result)" if self.whole else "")
        names = list(ports)
        if port and port not in names:
            # A link naming a port that no longer exists still SHOWS it —
            # dropping it silently would be the quietest possible edit, and
            # the problems line names it so the user can pick another.
            names.append(port)
        self.port.blockSignals(True)
        self.port.clear()
        self.port.addItems(names)
        if port:
            self.port.setCurrentText(port)
        self.port.blockSignals(False)
        self.set_keys(keys, key)

    def set_keys(self, keys: Sequence[str], keep: Optional[str] = None) -> None:
        """Offer ``keys`` as suggestions without losing what is typed."""
        typed = self.key.currentText() if keep is None else keep
        self.key.blockSignals(True)
        self.key.clear()
        self.key.addItems([k for k in keys if k])
        self.key.setCurrentText(typed or "")
        self.key.blockSignals(False)

    def value(self):
        return self.port.currentText().strip(), self.key.currentText().strip()


class WiringView(QGroupBox):
    """The Wiring group. Shown for one selected widget that can run a link."""

    #: The link to store on the shape — {} means "remove the wiring". Only
    #: emitted for a link with no problems.
    applied = Signal(dict)
    #: "Write it with the model…": {instruction, mode, inputs, outputs,
    #: function, n_best}. The tab runs it on a worker; nothing is written
    #: until the person accepts the review.
    write_requested = Signal(dict)
    #: The Stop beside it.
    write_stopped = Signal()

    #: (mode key, what the switch says).
    WRITE_MODES = (("function", "Function in logic.py (recommended)"),
                   ("handler", "Handler body (UI glue only)"))
    #: (n_best, what the switch says) — None is "from the model's size".
    CANDIDATES = ((None, "Candidates: auto"), (1, "1 candidate"),
                  (2, "2 candidates"), (3, "3 candidates"))

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("Wiring", parent)
        self._kind = "button"
        self._ports: List[wiring.PortInfo] = []
        self._mode, self._requires = "linked", []
        #: The open project's folder — its own modules (logic.py) are
        #: linkable, and read from there.
        self._project_dir: Any = None
        #: designer_wiring.local_modules(_project_dir), read per selection.
        self._local: List[str] = []
        #: The shape on show, so its note can prefill the instruction.
        self._shape_id = ""
        #: Output rows on screen, in link order — and hidden ones kept for
        #: reuse (see OutputRow.reset).
        self._rows: List[OutputRow] = []
        self._spare: List[OutputRow] = []
        #: module -> ModuleInfo for THIS selection. Each lookup reads the
        #: module's source to see whether it changed (~1 ms for
        #: frame_camera); one refresh used to make five of them.
        self._infos: Dict[str, wiring.ModuleInfo] = {}
        self._loading = False
        self._build()
        self.show_shape(None)

    # ==================================================================
    # Building
    # ==================================================================
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        layout.addWidget(QLabel("Module"))
        self.module = QComboBox()
        self.module.setEditable(True)
        layout.addWidget(self.module)
        layout.addWidget(QLabel("Function"))
        self.function = QComboBox()
        self.function.setEditable(True)
        layout.addWidget(self.function)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {THEME['subtext']};")
        layout.addWidget(self.hint)

        layout.addWidget(QLabel("Inputs — passed to it in this order"))
        self.inputs = QListWidget()
        self.inputs.setMaximumHeight(96)
        layout.addWidget(self.inputs)
        pick = QHBoxLayout()
        self.input_pick = QComboBox()
        pick.addWidget(self.input_pick, 1)
        pick.addWidget(self._small("Add", self._add_input))
        layout.addLayout(pick)
        moves = QHBoxLayout()
        for caption, slot in (("Remove", self._remove_input),
                              ("Up", lambda: self._move_input(-1)),
                              ("Down", lambda: self._move_input(1))):
            moves.addWidget(self._small(caption, slot))
        self.match_button = self._small("Match parameters",
                                        self._match_parameters)
        self.match_button.setToolTip(
            "Fill the inputs from the function's parameter names")
        moves.addWidget(self.match_button)
        layout.addLayout(moves)

        layout.addWidget(QLabel("Outputs — port ← result key"))
        self._outputs = QVBoxLayout()
        self._outputs.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._outputs)
        layout.addWidget(self._small("Add output", self._add_output),
                         0, Qt.AlignLeft)

        self.problems = QLabel()
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet(f"color: {THEME['red']};")
        layout.addWidget(self.problems)
        buttons = QHBoxLayout()
        self.apply_button = self._small("Apply wiring", self._apply)
        self.remove_button = self._small("Remove wiring", self._remove)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self._build_writer(layout)

        self.module.currentTextChanged.connect(self._module_changed)
        self.function.currentTextChanged.connect(self._function_changed)

    def _build_writer(self, layout: QVBoxLayout) -> None:
        """"Write it with the model…" — the instruction, the mode, Stop.

        Function mode reads its signature from the rows above: the Inputs
        list is the parameters, the Outputs rows are the result keys and
        where each is shown. So the person decides what goes in and out, and
        the model only writes what happens in between.
        """
        head = QLabel("Write it with the model")
        head.setStyleSheet("font-weight: bold;")
        layout.addWidget(head)
        self.instruction = QPlainTextEdit()
        self.instruction.setPlaceholderText(
            "What should it do? e.g. Count the PNG files in the chosen "
            "folder and list their names. (Prefilled from the widget's "
            "note.)")
        self.instruction.setMaximumHeight(72)
        layout.addWidget(self.instruction)
        row = QHBoxLayout()
        self.write_mode = QComboBox()
        for key, caption in self.WRITE_MODES:
            self.write_mode.addItem(caption, key)
        self.write_mode.setToolTip(
            "Function: the model writes one function into logic.py and the "
            "button is wired to it — Generate writes the tested handler. "
            "Handler body: the model writes the handler itself, only over a "
            "stub nobody has edited.")
        self.candidates = QComboBox()
        for n, caption in self.CANDIDATES:
            self.candidates.addItem(caption, n)
        self.candidates.setToolTip(
            "How many first attempts to sample. Small models vary from run "
            "to run, so auto asks a 4B model for 3 and a 14B model for 1.")
        row.addWidget(self.write_mode, 2)
        row.addWidget(self.candidates, 1)
        layout.addLayout(row)
        go = QHBoxLayout()
        self.write_button = self._small("Write it with the model…",
                                        self._write)
        self.stop_button = self._small("Stop", self.write_stopped.emit)
        self.stop_button.setEnabled(False)
        go.addWidget(self.write_button)
        go.addWidget(self.stop_button)
        go.addStretch(1)
        layout.addLayout(go)
        self.write_status = QLabel()
        self.write_status.setWordWrap(True)
        self.write_status.setStyleSheet(f"color: {THEME['subtext']};")
        layout.addWidget(self.write_status)

    def set_writing(self, busy: bool, status: str = "") -> None:
        """The writer's buttons while a job runs: Write off, Stop on."""
        self.write_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        if status or not busy:
            self.write_status.setText(status)

    def write_request(self) -> Dict[str, Any]:
        """What "Write it with the model…" sends: the instruction, the mode,
        and — for a function — the signature the rows above describe."""
        # The rows themselves, not link(): with no module picked yet a link
        # normalises to {} and would drop the outputs the user just added.
        outputs: Dict[str, str] = {}
        for row in self._rows:
            port, key = row.value()
            if port:
                outputs[port] = key
        function = (self.function.currentText().strip()
                    if self.module.currentText().strip() == "logic" else "")
        return {"instruction": self.instruction.toPlainText().strip(),
                "mode": self.write_mode.currentData() or "function",
                "inputs": self.input_names(), "outputs": outputs,
                "function": function,
                "n_best": self.candidates.currentData()}

    def _write(self) -> None:
        request = self.write_request()
        if not request["instruction"]:
            self.write_status.setText("Say what it should do first.")
            return
        self.write_requested.emit(request)

    @staticmethod
    def _small(caption: str, slot) -> QPushButton:
        button = QPushButton(amp(caption))
        button.clicked.connect(slot)
        return button

    # ==================================================================
    # Showing a shape
    # ==================================================================
    def show_shape(self, shape: Any, ports: Sequence[wiring.PortInfo] = (),
                   mode: str = "linked", requires: Sequence[str] = (),
                   project_dir: Any = None) -> None:
        """Show ``shape``'s link, or hide the group when it cannot have one.

        ``ports`` is designer_wiring.project_ports for the whole wireframe —
        computed by the caller, which already has the shapes and the
        project's port registry. ``project_dir`` makes the project's own
        modules (logic.py) linkable.
        """
        if shape is None or not wiring.linkable(getattr(shape, "kind", "")):
            self.setVisible(False)
            return
        self.setVisible(True)
        self._loading = True
        self._infos = {}
        try:
            self._kind = shape.kind
            self._ports = list(ports)
            self._mode, self._requires = mode, list(requires or [])
            self._project_dir = project_dir
            # Once per selection; every keystroke's re-check reuses it.
            self._local = wiring.local_modules(project_dir)
            if shape.id != self._shape_id:
                # A new widget: its note is the instruction to start from.
                # The same widget re-shown keeps what the user typed.
                self._shape_id = shape.id
                self.instruction.setPlainText(
                    str(getattr(shape, "note", "") or ""))
            link = wiring.normalise(getattr(shape, "script", None))
            what = shape.label or shape.kind
            self.summary.setText(
                f"{what} runs {wiring.describe(link)}" if link else
                f"{what} is not wired — it runs its handler stub. Pick a "
                f"function for it to call.")

            self.module.clear()
            self.module.addItems(wiring.linkable_modules(mode, requires,
                                                         project_dir))
            self.module.setCurrentText(link.get("module", ""))
            self._fill_functions(link.get("module", ""))
            self.function.setCurrentText(link.get("function", ""))

            readable = [p.name for p in self._ports if p.readable]
            self.input_pick.clear()
            self.input_pick.addItems(readable)
            self.inputs.clear()
            self.inputs.addItems(link.get("inputs", []))

            for row in list(self._rows):
                self._drop_row(row)
            keys = self._result_keys()
            for port, key in link.get("outputs", {}).items():
                self._add_output(port, key, keys)
            if link.get("output"):
                # The older single-output form: the WHOLE result goes to the
                # port. Shown as a row with a blank key that reads back as
                # `output` — as an ordinary row it read back as result[""],
                # which Apply (rightly) refused, so the link could not be
                # edited at all without changing what it shows.
                self._add_output(link["output"], "", keys, whole=True)
            self.remove_button.setEnabled(bool(link))
        finally:
            self._loading = False
        self._describe_function()
        self._revalidate()

    def _info(self, module: str) -> wiring.ModuleInfo:
        module = module.strip()
        if module not in self._infos:
            self._infos[module] = (
                wiring.module_info_for(module, self._project_dir) if module
                else wiring.ModuleInfo(""))
        return self._infos[module]

    # ==================================================================
    # Module and function
    # ==================================================================
    def _fill_functions(self, module: str) -> None:
        info = self._info(module)
        typed = self.function.currentText()
        self.function.blockSignals(True)
        self.function.clear()
        self.function.addItems(info.function_names)
        self.function.setCurrentText(typed)
        self.function.blockSignals(False)

    def _module_changed(self, text: str) -> None:
        if self._loading:
            return
        self._fill_functions(text.strip())
        self._describe_function()
        self._revalidate()

    def _function_changed(self, _text: str) -> None:
        if self._loading:
            return
        self._describe_function()
        self._revalidate()

    def _function_info(self) -> Optional[wiring.FunctionInfo]:
        info = self._info(self.module.currentText())
        return info.function(self.function.currentText().strip()) \
            if info.found else None

    def _result_keys(self) -> List[str]:
        fn = self._function_info()
        return list(fn.result_keys) if fn else []

    def _describe_function(self) -> None:
        """The hint: the signature, the docstring's first line, and what it
        returns — so the user sees what the inputs feed and the outputs read."""
        fn = self._function_info()
        module = self.module.currentText().strip()
        if fn is None:
            info = self._info(module) if module else None
            self.hint.setText(
                "" if info is None or info.found or not module else
                f"{module} is not a Council module, so its functions cannot "
                f"be listed — type the function name.")
            self.match_button.setEnabled(False)
        else:
            returns = (f"\nReturns: {', '.join(fn.result_keys)}"
                       if fn.result_keys else "")
            self.hint.setText(f"{fn.signature()}\n{fn.summary}{returns}")
            self.match_button.setEnabled(bool(fn.params))
        keys = self._result_keys()
        for row in self._rows:
            row.set_keys(keys)

    # ==================================================================
    # Inputs
    # ==================================================================
    def input_names(self) -> List[str]:
        return [self.inputs.item(i).text() for i in range(self.inputs.count())]

    def _add_input(self) -> None:
        name = self.input_pick.currentText().strip()
        if name:
            self.inputs.addItem(name)
            self.inputs.setCurrentRow(self.inputs.count() - 1)
            self._revalidate()

    def _remove_input(self) -> None:
        row = self.inputs.currentRow()
        if row >= 0:
            self.inputs.takeItem(row)
            self._revalidate()

    def _move_input(self, step: int) -> None:
        row = self.inputs.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.inputs.count():
            return
        item = self.inputs.takeItem(row)
        self.inputs.insertItem(target, item)
        self.inputs.setCurrentRow(target)
        self._revalidate()

    def _match_parameters(self) -> None:
        fn = self._function_info()
        if fn is None:
            return
        wired, unmatched = wiring.match_parameters(
            fn, [p.name for p in self._ports if p.readable])
        self.inputs.clear()
        self.inputs.addItems(wired)
        self._revalidate()
        if unmatched:
            self.problems.setText(
                self.problems.text() + ("\n" if self.problems.text() else "")
                + f"No port matches {', '.join(unmatched)} — add "
                  f"{'it' if len(unmatched) == 1 else 'them'} by hand.")

    # ==================================================================
    # Outputs
    # ==================================================================
    def _add_output(self, port: str = "", key: str = "",
                    keys: Optional[Sequence[str]] = None,
                    whole: bool = False) -> None:
        names = [p.name for p in self._ports if p.showable]
        port = port if isinstance(port, str) else ""   # clicked(bool)
        keys = self._result_keys() if keys is None else keys
        if self._spare:
            row = self._spare.pop(0)
            row.reset(names, port, key, keys, whole)
            # To the END of the layout, so the order on screen is the order
            # the link writes its outputs in.
            self._outputs.removeWidget(row)
        else:
            row = OutputRow(names, port, key, keys, whole=whole)
            row.changed.connect(self._revalidate)
            row.removed.connect(self._drop_output)
        self._rows.append(row)
        self._outputs.addWidget(row)
        row.setVisible(True)
        if not self._loading:
            self._revalidate()

    def _drop_output(self, row: OutputRow) -> None:
        self._drop_row(row)
        self._revalidate()

    def _drop_row(self, row: OutputRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
        row.setVisible(False)
        self._spare.append(row)

    # ==================================================================
    # Reading back
    # ==================================================================
    def link(self) -> Dict[str, Any]:
        """The link the controls describe, normalised; {} when empty."""
        outputs: Dict[str, str] = {}
        whole = ""
        for row in self._rows:
            port, key = row.value()
            if not port:
                continue
            if row.whole and not key:
                whole = port
            else:
                outputs[port] = key
        raw: Dict[str, Any] = {
            "module": self.module.currentText(),
            "function": self.function.currentText(),
            "inputs": self.input_names(), "outputs": outputs}
        if whole:
            raw["output"] = whole
        return wiring.normalise(raw)

    def current_problems(self) -> List[str]:
        found = wiring.problems(self.link(), self._ports, self._kind,
                                self._mode, self._requires,
                                info=self._info(self.module.currentText()),
                                project_dir=self._project_dir,
                                local=self._local)
        # link() keeps ONE output per port — a dict — so a second row for
        # the same port would be dropped on Apply without a word. Only the
        # rows can see it.
        ports = [row.value()[0] for row in self._rows if row.value()[0]]
        for port in sorted({p for p in ports if ports.count(p) > 1}):
            found.append(f"Output {port!r} is on more than one row — a port "
                         f"shows one result; remove the extra row.")
        return found

    def _revalidate(self) -> None:
        if self._loading:
            return
        self.problems.setText("\n".join(self.current_problems()))

    def _apply(self) -> None:
        found = self.current_problems()
        if found:
            self.problems.setText("Not applied:\n" + "\n".join(found))
            return
        self.applied.emit(self.link())

    def _remove(self) -> None:
        self.applied.emit({})


class CodeReviewDialog(QDialog):
    """What the model wrote, before anything is written: the diff, every
    gate it passed, the smoke run, the notes. Accept writes it; anything
    else leaves the project exactly as it was.

    Built from designer_codebehind.Review's fields only, so it is shown the
    same way whatever produced the review — and a test can build one with
    no model.
    """

    def __init__(self, review: Any, parent: Optional[QWidget] = None):
        super().__init__(parent)
        plan = review.plan
        where = (f"logic.py — {plan.link.get('function', '')}()"
                 if plan.mode == "function" else
                 f"handlers.py — {plan.handler}")
        self.setWindowTitle(f"Review: {plan.label or plan.widget} → {where}")
        self.resize(900, 640)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Nothing has been written yet. Read the change; Accept writes it "
            "(after a backup into .backups/) and records its fingerprint, so "
            "the Designer knows it is untouched model output until you edit "
            "it.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        split = QSplitter(Qt.Orientation.Vertical)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.diff = QPlainTextEdit(review.diff or "(no change)")
        self.diff.setReadOnly(True)
        self.diff.setFont(mono)
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.report = QPlainTextEdit("\n".join(review.report()))
        self.report.setReadOnly(True)
        split.addWidget(self.diff)
        split.addWidget(self.report)
        split.setSizes([440, 180])
        layout.addWidget(split, 1)
        buttons = QDialogButtonBox()
        self.accept_button = buttons.addButton(
            "Accept — write it", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Reject", QDialogButtonBox.ButtonRole.RejectRole)
        self.accept_button.setEnabled(bool(review.ok))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def ask_accept(review: Any, parent: Optional[QWidget] = None) -> bool:
    """Show the review modally; True only for Accept. Under
    COUNCIL_NO_DIALOGS (unattended, offscreen) the answer is "no" — a modal
    would wait for a click nobody can make, and writing code nobody read is
    the one thing this must never do."""
    from .. import dialogs
    if dialogs.disabled():
        return False
    return CodeReviewDialog(review, parent).exec() == \
        QDialog.DialogCode.Accepted
