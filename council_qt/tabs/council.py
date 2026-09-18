"""
council_qt.tabs.council — the Council tab, ported.

THE APP'S FRONT DOOR, AND THE LARGEST SINGLE METHOD IN IT
`_build_council_tab` is 300 lines of Tk and the turn behind it (`_send`) is
1,166 more. This file is written against
`docs/qt_migration/phase6_port_requirements.md` section A, NOT against the Tk
source — because six of the nine confirmed defects in that tab are logic
defects that a faithful translation would carry across silently.

The five that are designed out here, rather than fixed later:

  A1  THE WORKER NEVER READS A WIDGET. The Tk worker reads thirteen Tk
      variables off the GUI thread, which survives only because Tkinter
      marshals `Variable.get()` internally. Qt does not, so the direct
      translation is a data race on every send. `options()` snapshots into a
      frozen `CouncilOptions` on the GUI thread and the worker gets the
      snapshot.

  A2  ONE TURN AT A TIME. `_send` has no re-entrancy guard and is reachable
      eight ways; two concurrent turns patch and restore `model.respond` on top
      of each other. `begin_turn()` is the only door and it refuses a second
      turn OUT LOUD — an ignored click reads as a broken button.

  A3  THE VERDICT BAR IS TIED TO A VERDICT. In Tk the fast path shows the bar
      for a turn that produced no verdict, and agreeing then stamps
      `user_agreed=True` on the last line of `verdict_history.jsonl` — an
      unrelated earlier deliberation. Here the bar is shown only with a verdict
      id in hand, and agree/disagree carry that id.

  A4  PER-TURN STATE IS RESET IN ONE PLACE. `PER_TURN_FIELDS` names every
      field a turn sets, `reset_turn()` clears exactly those, and a test
      asserts the two agree — because the Tk reset block clears four fields and
      misses two, leaving a live button holding a stale question.

  A5  INTENT DETECTION READS WHAT THE USER TYPED. The Tk code rebinds
      `user_text = augmented` and then scans it for the lead role, LaTeX and
      script keywords, so injected spreadsheet contents steer them. Here the
      typed text and the augmented text are separate values with separate
      names and never the same variable.

WHAT IS AND IS NOT HERE
The VIEW is complete. The deliberation is not extracted — that is the rest of
phase 6 — so `CouncilActions.send` raises `NotYetExtracted` and the tab says so
in the transcript, where the user is already looking. Everything that does not
need the model works: the transcript, the stream box, the toggles, the
instruction bar, the specialist pin, saving an answer, the question history.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QSplitter,
                               QTextEdit, QVBoxLayout, QWidget)

from council_core import council_options
from council_core import paths
from council_core import transcript as transcript_core

from .. import theme
from ..view import ViewHelpers, amp
from ..widgets.transcript import StreamView, TranscriptView


#: Every field a turn sets. `reset_turn()` clears exactly these, and a test
#: asserts this list and that method agree.
#:
#: A4: the Tk reset block clears four fields and misses `_expand_btn`'s enabled
#: state and `_last_fast_question`, so after one fast answer the "Expand with
#: council" button stays live holding a stale question and re-asks the wrong
#: thing. A hand-maintained reset drifts; a declared list that a test checks
#: does not.
PER_TURN_FIELDS = (
    "_last_query",          # what the user typed, before augmentation
    "_last_answer",         # the final text, for Save answer
    "_last_route",          # which route answered
    "_last_sources",        # the source chips
    "_last_verdict_id",     # A3 — the bar is shown only when this is set
    "_last_fast_question",  # A4 — what "Expand with council" would re-ask
    "_force_full_council",
)


class CouncilActions:
    """What the Council tab can ask the application to do.

    The deliberation is not extracted yet. Rather than let a button do nothing,
    `send` raises and the view reports it in the transcript — which is both
    honest and, when the extraction lands, one method to replace.
    """

    class NotYetExtracted(RuntimeError):
        pass

    def __init__(self, vault_dir: Optional[Path] = None, demo_mode: bool = False):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.demo_mode = bool(demo_mode)
        self._models = None
        self._models_problem = ""

    # -- implemented -----------------------------------------------------
    def specialists(self):
        """The registry as the pin needs it: labels to show, ids to act on.

        This replaces a specialist_names() that had been returning [] since it
        was written: it called SpecialistRegistry() with no argument, the
        constructor requires vault_dir, and a bare `except Exception` turned
        the TypeError into an empty list. An empty dropdown caused by a
        swallowed error looks exactly like an empty dropdown caused by having
        no specialists.
        """
        from council_core import specialists_ops
        return specialists_ops.load(self.vault_dir)

    def save_answer(self, text: str, path: Path) -> str:
        """Write the last answer out. Returns a line for the transcript."""
        path = Path(path)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            return f"Could not save: {exc}"
        return f"Saved to {path}"

    def question_history(self, limit: int = 40) -> List[str]:
        try:
            import convo_store
            store = convo_store.ConvoStore(self.vault_dir)
            return [turn.get("text", "") for turn in store.recent(limit)
                    if turn.get("who") == "User"]
        except Exception:                                 # noqa: BLE001
            return []

    # -- the turn --------------------------------------------------------
    def models(self):
        """(personalities, problem). Exactly one is meaningful.

        Loaded lazily and kept: building the personalities loads models from
        disk, and a tab the user never sends from should not pay for it.

        The first version of this returned the `council_engine` MODULE, on the
        assumption that the slots were attributes on it. They are not — the
        module exposes build_personalities(), which returns a dict that the Tk
        console unpacks onto itself. Every turn would have reported "No judge
        model is loaded", and no test caught it because they all inject
        stand-ins. See council_core.council_turn.Personalities.
        """
        from council_core import council_turn

        if self._models is None and not self._models_problem:
            self._models, self._models_problem = (
                council_turn.load_personalities(self.vault_dir))
        return self._models, self._models_problem

    def send(self, typed_text: str, options, *, on_event=None,
             on_token=None):
        """Run one turn through council_core.council_turn.

        Takes the TYPED text and a frozen options snapshot — never a widget and
        never the augmented text (A1, A5). Reports progress by calling
        ``on_event``; the caller decides which thread that lands on.
        """
        from council_core import council_turn

        models, problem = self.models()
        if models is None:
            return council_turn.TurnResult(False, message=problem)

        if not getattr(options, "deliberate", True):
            # The fast path: one personality, no panel, no verdict. It is a
            # real answer and it is NOT a deliberation, so it produces no
            # verdict id — which is what keeps the verdict bar honest (A3).
            return self._direct(typed_text, models, on_event=on_event)

        return council_turn.run_turn(
            typed_text, models,
            enable_tools=bool(getattr(options, "tools", False)),
            on_event=on_event,
            on_token=on_token if getattr(options, "stream", True) else None)

    def _direct(self, typed_text: str, models, *, on_event=None):
        """One personality answering directly, with no council."""
        from council_core import council_turn
        from council_core.deliberation import AgentEvent

        writer = getattr(models, "writer", None)
        if writer is None:
            return council_turn.TurnResult(
                False, message="No writer model is loaded.")
        try:
            answer = writer.respond(typed_text)
        except Exception as exc:                          # noqa: BLE001
            return council_turn.TurnResult(
                False, message=f"The answer failed: {exc!r}", error=exc)
        event = AgentEvent("Writer", "final", answer)
        if on_event is not None:
            on_event(event)
        return council_turn.TurnResult(True, answer=answer, route="direct",
                                       events=[event])

    def record_verdict_response(self, verdict_id: str, agreed: bool,
                                objection: str = ""):
        """Stamp a verdict BY ID.

        A3: the Tk version stamps the last line of verdict_history.jsonl, which
        after a direct-mode turn belongs to an unrelated earlier deliberation.
        Taking an id makes that mistake impossible to express.
        """
        raise self.NotYetExtracted(
            "recording a verdict response needs the verdict store extracted")


class CouncilTab(ViewHelpers, QWidget):
    """The transcript, the judge panel, the stream, and the input bar."""

    def __init__(self, window=None, actions: Optional[CouncilActions] = None,
                 demo_mode: bool = False):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.demo_mode = bool(demo_mode)
        self.actions = actions or CouncilActions(demo_mode=demo_mode)
        self._tokens = theme.tokens("dark")

        self._turn_active = False
        self._checkboxes = {}
        self._specialists = None
        self._opts = council_options.CouncilOptions.defaults(
            demo_mode=self.demo_mode)
        for field in PER_TURN_FIELDS:
            setattr(self, field, None)

        self._build()
        self.refresh_specialists()

    # ==================================================================
    # Layout
    # ==================================================================
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._transcript_side())
        right = self._judge_side()
        # DEMO_MODE is a single-personality Q&A: no panel, no verdicts, so the
        # right pane is not useful to show. The widgets are still CREATED, as
        # in Tk, so anything that reaches for them stays safe.
        if not self.demo_mode:
            split.addWidget(right)
            split.setSizes([700, 380])
        else:
            right.setParent(self)
            right.hide()
        outer.addWidget(split, 1)

        outer.addWidget(self._input_side())

    def _transcript_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Transcript"))
        self.transcript = TranscriptView()
        layout.addWidget(self.transcript, 1)
        return panel

    def _judge_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("Judge Panel"))
        self.judge_box = QTextEdit()
        self.judge_box.setReadOnly(True)
        layout.addWidget(self.judge_box, 1)

        layout.addWidget(self._verdict_bar())
        layout.addWidget(self._objection_panel())

        layout.addWidget(QLabel("Live Token Stream"))
        self.stream_box = StreamView()
        layout.addWidget(self.stream_box, 1)
        return panel

    def _verdict_bar(self) -> QWidget:
        self.vfb_frame = QFrame()
        row = QHBoxLayout(self.vfb_frame)
        row.setContentsMargins(0, 4, 0, 0)
        row.addWidget(QLabel("Do you agree with the verdict?"))
        self.vfb_agree = self._button(row, amp("✓ Agree"), self.on_agree)
        self.vfb_disagree = self._button(row, amp("✗ Disagree"),
                                         self.on_disagree_open)
        row.addStretch(1)
        self.vfb_frame.hide()                 # A3 — shown only with a verdict
        return self.vfb_frame

    def _objection_panel(self) -> QWidget:
        self.vfb_detail = QFrame()
        layout = QVBoxLayout(self.vfb_detail)
        layout.setContentsMargins(0, 2, 0, 0)
        prompt = QLabel("Your objection (the council will re-deliberate "
                        "with it):")
        prompt.setStyleSheet(f"color: {self._tokens['warning']};")
        layout.addWidget(prompt)
        self.objection = QPlainTextEdit()
        self.objection.setMaximumHeight(70)
        layout.addWidget(self.objection)
        row = QHBoxLayout()
        self._button(row, amp("↩ Re-deliberate with objection"),
                     self.on_disagree_submit)
        self._button(row, "Cancel", self.on_disagree_cancel)
        hint = QLabel("Ctrl+Enter to submit")
        hint.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(hint)
        row.addStretch(1)
        layout.addLayout(row)
        self._shortcut("Ctrl+Return", self.on_disagree_submit, self.objection)
        self.vfb_detail.hide()
        return self.vfb_detail

    def _input_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("Input"))
        self.input = QPlainTextEdit()
        self.input.setMaximumHeight(96)
        layout.addWidget(self.input)
        self._shortcut("Ctrl+Return", self.on_send, self.input)

        layout.addLayout(self._action_row())
        layout.addLayout(self._toggle_row(1))
        if not self.demo_mode:
            layout.addLayout(self._toggle_row(2))
        layout.addWidget(self._instruction_bar())
        layout.addWidget(self._save_panel())
        layout.addWidget(self._clarification_panel())
        layout.addLayout(self._override_row())
        return panel

    def _action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.send_btn = self._button(row, "Send  [Ctrl+Enter]", self.on_send)
        self._button(row, amp("📊 Find & Chart"), self.on_find_and_chart)
        self._button(row, amp("🔍 Look Up"), self.on_look_up)
        self._button(row, "Clear", self.on_clear_input)
        self._button(row, amp("⤓ Defer to Vault"), self.on_defer_to_vault)
        # Enabled only after a fast answer — re-asks the SAME question through
        # the full council. A4: its enabled state is per-turn and is reset in
        # reset_turn() with everything else.
        self.expand_btn = self._button(row, amp("⤢ Expand with council"),
                                       self.on_expand_with_council)
        self.expand_btn.setEnabled(False)
        self.save_btn = self._button(row, amp("💾 Save answer"),
                                     self.on_save_answer)
        self._button(row, amp("🕘 History"), self.on_history)
        self._button(row, amp("💡 What can I ask?"), self.on_examples)
        row.addStretch(1)

        self.agent_label = QLabel("")
        row.addWidget(self.agent_label)
        self.tps_label = QLabel("")
        row.addWidget(self.tps_label)
        self.status = QLabel("● idle")
        self.status.setStyleSheet(f"color: {self._tokens['success']};")
        row.addWidget(self.status)
        return row

    def _toggle_row(self, which: int) -> QHBoxLayout:
        """One toolbar row, built from council_core.council_options.

        The switches, their defaults and which of them DEMO_MODE hides are all
        described in one place, so the two front ends cannot disagree about the
        state the tab opens in — which no test would catch, because no test
        looks at a checkbox's initial value.
        """
        row = QHBoxLayout()
        if which == 2:
            label = QLabel("Personalities:")
            label.setStyleSheet(f"color: {self._tokens['muted_fg']};")
            row.addWidget(label)
        for switch in council_options.visible_switches(self.demo_mode,
                                                       row=which):
            box = QCheckBox(amp(switch.label))
            box.setChecked(getattr(self._opts, switch.key))
            box.toggled.connect(
                lambda on, key=switch.key: self.on_toggle(key, on))
            if switch.hint:
                box.setToolTip(switch.hint)
            row.addWidget(box)
            self._checkboxes[switch.key] = box
            if switch.hint and which == 2:
                hint = QLabel(f"({switch.hint})")
                hint.setStyleSheet(f"color: {self._tokens['muted_fg']};")
                row.addWidget(hint)
        row.addStretch(1)
        return row

    def _instruction_bar(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 2, 0, 0)
        label = QLabel(amp("⚡ Instruction:"))
        row.addWidget(label)
        self.inst_name = QLineEdit()
        self.inst_name.setPlaceholderText("Name (optional)")
        self.inst_name.setFixedWidth(140)
        row.addWidget(self.inst_name)
        self.inst_text = QLineEdit()
        self.inst_text.setPlaceholderText(
            "e.g. always show your working as a table")
        row.addWidget(self.inst_text, 1)
        self.inst_text.returnPressed.connect(self.on_add_instruction)
        self._button(row, "Add  [Enter]", self.on_add_instruction)
        self._button(row, amp("Manage…"), self.on_manage_instructions)
        self._button(row, amp("Content Style…"), self.on_content_style)
        self.inst_active = QLabel("")
        self.inst_active.setStyleSheet(f"color: {self._tokens['success']};")
        row.addWidget(self.inst_active)
        return box

    def _save_panel(self) -> QWidget:
        self.save_frame = QFrame()
        row = QHBoxLayout(self.save_frame)
        row.setContentsMargins(0, 4, 0, 2)
        title = QLabel(amp("💾 Save output as:"))
        row.addWidget(title)
        for label, suffix in ((amp("📄 Text file (.txt)"), ".txt"),
                              (amp("📝 Markdown (.md)"), ".md"),
                              (amp("📐 LaTeX (.tex)"), ".tex")):
            self._button(row, label,
                         lambda _=False, s=suffix: self.on_save_output(s))
        row.addStretch(1)
        self._button(row, amp("✕"), self.save_frame.hide)
        self.save_frame.hide()                # shown when output is ready
        return self.save_frame

    def _clarification_panel(self) -> QWidget:
        self.clarif_frame = QFrame()
        layout = QVBoxLayout(self.clarif_frame)
        layout.setContentsMargins(0, 2, 0, 0)
        title = QLabel(amp("🤔 A personality needs your input:"))
        layout.addWidget(title)
        self.clarif_question = QLabel("")
        self.clarif_question.setWordWrap(True)
        layout.addWidget(self.clarif_question)
        row = QHBoxLayout()
        self.clarif_answer = QLineEdit()
        row.addWidget(self.clarif_answer, 1)
        self.clarif_answer.returnPressed.connect(self.on_clarify)
        self._button(row, "Answer  [Enter]", self.on_clarify)
        self._button(row, "Skip", lambda: self.on_clarify(skip=True))
        layout.addLayout(row)
        self.clarif_frame.hide()
        return self.clarif_frame

    def _override_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel("Override: ")
        label.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(label)
        self.backend_box = QComboBox()
        self.backend_box.addItems(council_options.BACKEND_CHOICES)
        row.addWidget(self.backend_box)

        row.addSpacing(12)
        ask = QLabel("Ask:")
        ask.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(ask)
        self.specialist_box = QComboBox()
        self.specialist_box.setMinimumWidth(180)
        row.addWidget(self.specialist_box)
        row.addStretch(1)
        return row

    # -- small helpers ---------------------------------------------------
    # ==================================================================
    # The turn
    # ==================================================================
    def options(self) -> council_options.CouncilOptions:
        """A frozen snapshot of the switches, taken on the GUI thread.

        A1: this is the whole answer to the thirteen off-thread Tk variable
        reads. The worker is handed the result of this call and never sees a
        widget, so there is nothing for it to race on.
        """
        snapshot = council_options.CouncilOptions(**{
            key: box.isChecked() for key, box in self._checkboxes.items()})
        # Anything DEMO_MODE hides keeps its default rather than whatever a
        # missing checkbox would imply.
        for switch in council_options.SWITCHES:
            if switch.key not in self._checkboxes:
                setattr(snapshot, switch.key, getattr(self._opts, switch.key))
        return snapshot.effective(self.demo_mode)

    def begin_turn(self) -> bool:
        """Claim the turn, or refuse out loud.

        A2: `_send` in Tk has no guard and is reachable eight ways, and two
        concurrent turns patch and restore `model.respond` over each other.
        Refusing silently would read as a broken button, so it says so.
        """
        if self._turn_active:
            self.append("Council", "A turn is already running — wait for it to "
                                   "finish, or press Stop.", "observation")
            return False
        self._turn_active = True
        self.send_btn.setEnabled(False)
        self.set_status("● thinking", self._tokens["accent"])
        return True

    def end_turn(self) -> None:
        self._turn_active = False
        self.send_btn.setEnabled(True)
        self.set_status("● idle", self._tokens["success"])

    def reset_turn(self) -> None:
        """Clear everything the previous turn left behind.

        A4: one place, driven by PER_TURN_FIELDS, with a test that the list and
        this method agree. The Tk reset clears four fields and misses two.
        """
        for field in PER_TURN_FIELDS:
            setattr(self, field, None)
        self.expand_btn.setEnabled(False)
        self.save_frame.hide()
        self.clarif_frame.hide()
        self.hide_verdict_bar()
        self.stream_box.clear_all()

    def on_send(self) -> None:
        typed = self.input.toPlainText().strip()
        if not typed:
            return
        if not self.begin_turn():
            return
        self.reset_turn()
        self._last_query = typed
        self.append("User", typed)
        self.input.clear()

        options = self.options()          # on the GUI thread, before the worker

        def work() -> None:
            try:
                result = self.actions.send(
                    typed, options,
                    on_event=lambda ev: self._to_ui(
                        lambda ev=ev: self.on_event(ev)),
                    on_token=lambda who, tok: self._to_ui(
                        lambda who=who, tok=tok: self.on_token(who, tok)))
                if result is not None:
                    self._to_ui(lambda: self.finish_turn(result))
            except CouncilActions.NotYetExtracted as exc:
                self._to_ui(lambda: self.append("Council", str(exc),
                                                "observation"))
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(lambda: self.append("ERROR", repr(exc), "error"))
            finally:
                self._to_ui(self.end_turn)

        threading.Thread(target=work, name="council-turn", daemon=True).start()

    def on_event(self, event) -> None:
        """One progress event from the turn, always on the GUI thread."""
        kind = getattr(event, "kind", None) or (
            event[0] if isinstance(event, (tuple, list)) else "")
        if kind == "token":
            who = getattr(event, "who", "Writer")
            self.stream_box.append_token(who, getattr(event, "text", ""))
            return
        if kind == "phase":
            self.append("", getattr(event, "text", ""), "phase")
            return
        if kind == "verdict":
            # A3: the bar appears because a verdict arrived, carrying its id.
            self.show_verdict_bar(getattr(event, "verdict_id", None))
            return
        self.append(getattr(event, "who", "Council"),
                    getattr(event, "text", str(event)), "observation")

    def on_token(self, who: str, token: str) -> None:
        """One streamed token. The stream box, never the transcript.

        The transcript does not see tokens in either front end — an AST pass
        over all 285 `_append_transcript` call sites in the Tk engine confirms
        kind="token" is never passed to it.
        """
        self.stream_box.append_token(who, token)
        self.stream_box.flush()

    def finish_turn(self, result) -> None:
        """Render what the turn produced, on the GUI thread."""
        if not result.ok:
            self.append("Council", result.message or "The turn failed.",
                        "observation")
            return
        if result.answer:
            self.append("Writer", result.answer, "final")
        if result.critique:
            self.set_judge(result.critique)
        if result.route == "direct" and result.answer:
            # Only a fast answer can be expanded, and only until the next turn
            # resets it (A4).
            self._last_fast_question = self._last_query
            self.expand_btn.setEnabled(True)
        self._last_route = result.route
        # A3: the bar follows the verdict id, which a turn without a verdict
        # does not have.
        self.show_verdict_bar(result.verdict_id)

    def flush(self) -> None:
        """Called once per queue drain — see the stream box's own note."""
        self.stream_box.flush()

    # ==================================================================
    # The transcript
    # ==================================================================
    def append(self, who: str, text: str, kind: str = "final") -> None:
        """One entry, formatted by the shared policy.

        Goes through council_core.transcript, so the Council transcript, the
        Dream3D mirror and the Tk shell all say the same thing.
        """
        self.transcript.append_entry(who, text, kind)
        if transcript_core.is_final_answer(who, kind):
            self._last_answer = text
            self.save_frame.show()

    def set_status(self, text: str, colour: Optional[str] = None) -> None:
        self.status.setText(text)
        if colour:
            self.status.setStyleSheet(f"color: {colour};")

    def set_judge(self, text: str) -> None:
        self.judge_box.setPlainText(text)

    # ==================================================================
    # The verdict feedback bar
    # ==================================================================
    def show_verdict_bar(self, verdict_id: Optional[str]) -> None:
        """Show the bar — only with a verdict to attach an answer to.

        A3: the Tk build shows it on every `done`, including the direct-mode
        fast path that never produced a verdict, and then stamps the answer
        onto an unrelated earlier deliberation. No id, no bar.
        """
        if not verdict_id:
            return
        self._last_verdict_id = verdict_id
        self.vfb_frame.show()

    def hide_verdict_bar(self) -> None:
        self.vfb_frame.hide()
        self.vfb_detail.hide()

    def on_agree(self) -> None:
        self._record_verdict(True)

    def on_disagree_open(self) -> None:
        self.vfb_detail.show()
        self.objection.setFocus()

    def on_disagree_cancel(self) -> None:
        self.vfb_detail.hide()
        self.objection.clear()

    def on_disagree_submit(self) -> None:
        objection = self.objection.toPlainText().strip()
        if not objection:
            self.append("Council", "Say what you disagree with, or Cancel.",
                        "observation")
            return
        self._record_verdict(False, objection)

    def _record_verdict(self, agreed: bool, objection: str = "") -> None:
        if not self._last_verdict_id:
            # Unreachable through the bar, which is hidden without an id. Kept
            # because that invariant is the fix, and an assertion that states
            # it is cheaper than rediscovering why the bar is conditional.
            self.append("Council", "There is no verdict to respond to.",
                        "observation")
            return
        try:
            self.actions.record_verdict_response(
                self._last_verdict_id, agreed, objection)
        except CouncilActions.NotYetExtracted as exc:
            self.append("Council", str(exc), "observation")
            return
        self.hide_verdict_bar()
        self.objection.clear()

    # ==================================================================
    # The rest of the buttons
    # ==================================================================
    def on_toggle(self, key: str, on: bool) -> None:
        setattr(self._opts, key, bool(on))

    def on_clear_input(self) -> None:
        self.input.clear()

    def refresh_specialists(self) -> None:
        """Fill the Ask: pin, and say so when the registry will not load.

        A registry that fails is not the same as a registry that is empty, and
        the Tk shell's version of this cannot tell them apart either.
        """
        from council_core import specialists_ops

        self._specialists = self.actions.specialists()
        self.specialist_box.clear()
        self.specialist_box.addItems(specialists_ops.choices(self._specialists))
        if not self._specialists.ok:
            self.append("Council", self._specialists.message, "observation")

    def pinned_specialist(self) -> Optional[str]:
        """The specialist ID to force onto this query, or None for automatic.

        Resolved through the map the labels were BUILT from. The previous
        version built {name: name}, so even with entries it would have pinned
        a NAME where the resolver wants an ID.
        """
        from council_core import specialists_ops
        return specialists_ops.pinned_id(self.specialist_box.currentText(),
                                         self._specialists)

    def backend_override(self) -> Optional[str]:
        return council_options.backend_override(self.backend_box.currentText())

    def on_save_answer(self) -> None:
        if not self._last_answer:
            self.append("Council", "There is no answer to save yet.",
                        "observation")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save answer", "answer.md", "Markdown (*.md);;Text (*.txt)")
        if not path:
            return
        self.append("Council", self.actions.save_answer(self._last_answer,
                                                        Path(path)),
                    "observation")

    def on_save_output(self, suffix: str) -> None:
        if not self._last_answer:
            self.append("Council", "There is no output to save yet.",
                        "observation")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, f"Save as {suffix}", f"output{suffix}",
            f"*{suffix}")
        if not path:
            return
        self.append("Council", self.actions.save_answer(self._last_answer,
                                                        Path(path)),
                    "observation")

    def on_history(self) -> None:
        questions = self.actions.question_history()
        if not questions:
            self.append("Council", "No questions asked yet this session.",
                        "observation")
            return
        self.append("Council",
                    "Recent questions:\n" + "\n".join(f"  • {q}"
                                                      for q in questions[:20]),
                    "observation")

    def on_examples(self) -> None:
        self.append("Council", EXAMPLES, "observation")

    def on_expand_with_council(self) -> None:
        """Re-ask the last fast question through the full council.

        A4: reachable only while `_last_fast_question` is set, and
        `reset_turn()` clears both it and this button's enabled state — so it
        cannot re-ask a question from two turns ago, which is what the Tk
        version does.
        """
        if not self._last_fast_question:
            self.append("Council", "There is no fast answer to expand.",
                        "observation")
            return
        self._force_full_council = True
        self.input.setPlainText(self._last_fast_question)
        self.on_send()

    def on_clarify(self, skip: bool = False) -> None:
        self.clarif_frame.hide()
        self.clarif_answer.clear()

    # -- not extracted, and saying so ------------------------------------
    def _not_yet(self, what: str) -> None:
        self.append("Council",
                    f"{what} needs the deliberation extracted — it is the "
                    "rest of phase 6.", "observation")

    def on_find_and_chart(self) -> None:
        self._not_yet("Find & Chart")

    def on_look_up(self) -> None:
        self._not_yet("Look Up")

    def on_defer_to_vault(self) -> None:
        self._not_yet("Defer to Vault")

    def on_add_instruction(self) -> None:
        self._not_yet("Council instructions")

    def on_manage_instructions(self) -> None:
        self._not_yet("Managing instructions")

    def on_content_style(self) -> None:
        self._not_yet("Content style")


EXAMPLES = """Things you can ask:
  • which file has the Q3 invoice totals?
  • summarise sales.csv by region
  • chart revenue by month
  • what changed between the two job folders?
  • write a summary I can send to a client"""


def build_council(window) -> QWidget:
    """Factory for the tab registry."""
    import branding
    return CouncilTab(window,
                      demo_mode=bool(getattr(branding, "DEMO_MODE", False)))
