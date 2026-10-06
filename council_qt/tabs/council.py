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
The VIEW is complete and the turn is real: `CouncilActions.send` runs
council_core.council_turn (or one Writer, on the fast path). What is not wired
yet — Look Up, Find & Chart, Defer to Vault, instructions, content style, the
per-role override, the specialist pin, History, verdict responses, Expand, and
every switch but Deliberation and Stream tokens — is shown DISABLED with a
"not available in this build yet" tooltip (_label_unavailable) rather than
looking live; docs/qt_migration/remaining_scope_2026-10-06.md says which batch
wires each.
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
from council_core import council_turn
from council_core import paths
from council_core import transcript as transcript_core

from .. import theme
from ..view import ViewHelpers, amp
from ..widgets.transcript import (MirroredTranscript, StreamView,
                                  TranscriptView)


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
    "_shown_finals",        # final texts already in the transcript this turn
    "_turn_rates",          # speaker -> tokens/s, for the tps label
    "_turn_stats_floor",    # the engine's stats seq when the turn began
)

#: What a control that does nothing yet says when hovered. It is DISABLED
#: and says this rather than being removed: a removed button is a feature
#: the user cannot tell is coming, and a live one that does nothing — or
#: answers a click with "needs the deliberation extracted" in the transcript
#: — reads as broken. docs/qt_migration/remaining_scope_2026-10-06.md lists
#: which batch wires each; its line in _label_unavailable goes when it is.
NOT_AVAILABLE = "{what} — not available in this build yet."

#: Speakers the "▶ who" label names: the personalities' display names, from
#: the one table the transcript uses, and the Judge. The orchestrator's phase
#: markers name a role ("▶ Writer — drafting answer") or a stage ("▶ Round
#: 1/2 — …"); only the first kind is an active personality.
_SPEAKERS = frozenset(council_turn.AGENT_NAMES.values()) | {"Judge"}


class CouncilActions:
    """What the Council tab can ask the application to do.

    `send` runs a real turn. What is still missing raises NotYetExtracted
    (recording a verdict response), and the view reports it rather than doing
    nothing — one method to replace when it lands.
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

    def question_history(self, limit: int = 40) -> Optional[List[str]]:
        """The recent questions, or None when there is no store to read.

        None is not []: "no questions asked yet" is a claim about the
        session, and with no store it is false. convo_store is not in this
        build, and nothing in Qt writes questions yet (saving is Batch 1), so
        this is None today and History is disabled (_label_unavailable).
        Measured in review: History straight after a question said "No
        questions asked yet this session."."""
        try:
            import convo_store
            store = convo_store.ConvoStore(self.vault_dir)
            return [turn.get("text", "") for turn in store.recent(limit)
                    if turn.get("who") == "User"]
        except Exception:                                 # noqa: BLE001
            return None

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
        if on_event is not None:
            # BEFORE the call, so the tab can say who is answering while it
            # waits ("▶ Writer"). A thought: progress, never a transcript line.
            on_event(AgentEvent("Writer", "thought", "Answering…"))
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

    def last_call_stats(self) -> dict:
        """council_engine.last_call_stats() — what the most recent model call
        cost (gen_tok_s, role, seq) — when the engine is loaded; {} when it is
        not. Never imports it: this is read on the GUI thread, and a turn
        that has called a model has loaded it already."""
        import sys
        engine = sys.modules.get("council_engine")
        getter = getattr(engine, "last_call_stats", None) if engine else None
        try:
            return dict(getter() or {}) if callable(getter) else {}
        except Exception:                                 # noqa: BLE001
            return {}

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
        #: what -> the widgets labelled "not available in this build yet".
        self.unavailable = {}
        self._specialists = None
        # The Profile box shows what the engine will do (it cannot be
        # changed here yet — see council_options.SWITCHES).
        self._opts = council_options.CouncilOptions.defaults(
            demo_mode=self.demo_mode,
            profile_enabled=council_options.profile_applied())
        for field in PER_TURN_FIELDS:
            setattr(self, field, None)

        # Other views that show this transcript too — the Dream3D chat adds
        # itself. Empty until one does, which costs nothing.
        self.mirror = MirroredTranscript()
        # Called (on the GUI thread) after a pipeline command created or
        # modified a pipeline, so a picker can rescan.
        self.pipelines_changed: List[Callable[[], None]] = []
        self._pipeline_chat = None
        self._model_chat = None

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
        self._label_unavailable()

    def _label_unavailable(self) -> None:
        """Disable every control that does nothing yet, and say so on hover.

        Each of these used to look live: a click either did nothing or put
        "<what> needs the deliberation extracted — it is the rest of phase 6"
        into the transcript. The handlers stay (a later batch fills them in);
        only the controls' state changes."""
        def label(what: str, *widgets, why: str = "") -> None:
            tip = NOT_AVAILABLE.format(what=what) + (f" {why}" if why else "")
            for widget in widgets:
                widget.setEnabled(False)
                widget.setToolTip(tip)
            self.unavailable.setdefault(what, []).extend(widgets)

        label("Find & Chart", self.find_chart_btn)
        label("Look Up", self.look_up_btn)
        label("Defer to Vault", self.defer_btn)
        label("Expand with council", self.expand_btn,
              why="It would re-ask the same question the same way and "
                  "repeat the answer: this build has no way yet to send a "
                  "fast question to the full council.")
        label("Council instructions", self.inst_name, self.inst_text,
              self.inst_add_btn, self.inst_manage_btn)
        label("Content style", self.content_style_btn)
        label("The per-role model override", self.backend_box,
              why="Every role answers from the model set in the Models tab.")
        label("Asking a specialist", self.specialist_box,
              why="Every question is answered without a specialist.")
        label("History", self.history_btn,
              why="Questions are not saved in this build yet.")
        label("Responding to a verdict", self.vfb_agree, self.vfb_disagree,
              self.redeliberate_btn,
              why="Verdicts are not recorded yet, so there is nothing for "
                  "an answer to attach to.")
        for switch in council_options.SWITCHES:
            box = self._checkboxes.get(switch.key)
            if box is not None and not switch.available:
                label(switch.name, box, why=switch.unavailable_why)

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
        self.redeliberate_btn = self._button(
            row, amp("↩ Re-deliberate with objection"),
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
        self.find_chart_btn = self._button(row, amp("📊 Find & Chart"),
                                           self.on_find_and_chart)
        self.look_up_btn = self._button(row, amp("🔍 Look Up"),
                                        self.on_look_up)
        self._button(row, "Clear", self.on_clear_input)
        self.defer_btn = self._button(row, amp("⤓ Defer to Vault"),
                                      self.on_defer_to_vault)
        # Meant to re-ask the last FAST question through the full council
        # (A4: per-turn state, reset in reset_turn()). Disabled and labelled
        # in _label_unavailable until a turn can be sent to the full council
        # on request — see finish_turn.
        self.expand_btn = self._button(row, amp("⤢ Expand with council"),
                                       self.on_expand_with_council)
        self.expand_btn.setEnabled(False)
        self.save_btn = self._button(row, amp("💾 Save answer"),
                                     self.on_save_answer)
        self.history_btn = self._button(row, amp("🕘 History"),
                                        self.on_history)
        self._button(row, amp("💡 What can I ask?"), self.on_examples)
        row.addStretch(1)

        # Fed by the turn's own events: who is answering now ("▶ Writer",
        # cleared when the turn ends), and how fast the models generated
        # this turn (council_engine.last_call_stats). Tk's colours.
        self.agent_label = QLabel("")
        self.agent_label.setStyleSheet("color: #a6e3a1;")
        row.addWidget(self.agent_label)
        self.tps_label = QLabel("")
        self.tps_label.setStyleSheet("color: #d32f2f;")
        self.tps_label.setToolTip("Generation speed this turn, from the "
                                  "engine's own measurement of each call.")
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
        self.inst_add_btn = self._button(row, "Add  [Enter]",
                                         self.on_add_instruction)
        self.inst_manage_btn = self._button(row, amp("Manage…"),
                                            self.on_manage_instructions)
        self.content_style_btn = self._button(row, amp("Content Style…"),
                                              self.on_content_style)
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
            # No "or press Stop": this tab has no Stop button (stopping a
            # running turn is not built in either shell yet), and pointing
            # at a control that is not there is worse than saying nothing.
            self.append("Council", "A turn is already running — wait for it "
                                   "to finish.", "observation")
            return False
        self._turn_active = True
        self.send_btn.setEnabled(False)
        self.set_status("● thinking", self._tokens["accent"])
        return True

    def end_turn(self) -> None:
        self._turn_active = False
        self.send_btn.setEnabled(True)
        self.set_status("● idle", self._tokens["success"])
        # Nobody is answering any more. The speed stays: it describes the
        # turn that just finished, which is when it is worth reading.
        self.agent_label.setText("")

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
        if self._send_command(typed):
            return
        if not self.begin_turn():
            return
        self.reset_turn()
        # Only calls made from here on describe THIS turn's speed.
        self._turn_stats_floor = self.actions.last_call_stats().get("seq") or 0
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
            # THE MESSAGE IS BOUND HERE, NOT READ LATER. Python deletes the
            # `except ... as exc` name at the end of the block, so a lambda
            # that closes over `exc` and runs later raises NameError instead of
            # showing the error. It worked only while `_to_ui` called back
            # synchronously — which is the test path, never the app's.
            except CouncilActions.NotYetExtracted as exc:
                said = str(exc)
                self._to_ui(lambda said=said: self.append("Council", said,
                                                          "observation"))
            except Exception as exc:                      # noqa: BLE001
                said = repr(exc)
                self._to_ui(lambda said=said: self.append("ERROR", said,
                                                          "error"))
            finally:
                self._to_ui(self.end_turn)

        threading.Thread(target=work, name="council-turn", daemon=True).start()

    def pipeline_chat(self):
        """The pipeline-command responder, built on first use."""
        if self._pipeline_chat is None:
            from council_core import dream3d
            self._pipeline_chat = dream3d.PipelineChat(
                self.actions.vault_dir,
                say=lambda who, text, kind: self._to_ui(
                    self.append, who, text, kind),
                on_changed=lambda: self._to_ui(self._pipelines_changed))
        return self._pipeline_chat

    def _pipelines_changed(self) -> None:
        for hook in list(self.pipelines_changed):
            hook()

    def model_chat(self):
        """The model-catalog responder ("what models can I download?")."""
        if self._model_chat is None:
            from council_core import model_chat
            self._model_chat = model_chat.ModelChat(
                say=lambda who, text, kind: self._to_ui(
                    self.append, who, text, kind))
        return self._model_chat

    def _send_command(self, typed: str) -> bool:
        """Answer app commands — pipelines, then models — without the council,
        as Tk's _send does before deliberating. True if handled.

        Deciding is fast (regexes, at most a folder scan); the answer can call
        a model, the DREAM3D-NX env or the network, so it runs on a worker.
        """
        job = None
        for responder in (self.pipeline_chat, self.model_chat):
            try:
                job = responder().plan(typed)
            except Exception:                             # noqa: BLE001
                job = None            # a broken responder must not block chat
            if job is not None:
                break
        if job is None:
            return False
        self.append("User", typed)
        self.input.clear()

        def work() -> None:
            try:
                job()
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self.append, "Writer",
                            f"That command failed: {exc!r}", "final")

        threading.Thread(target=work, name="council-command",
                         daemon=True).start()
        return True

    def on_event(self, event) -> None:
        """One progress event from the turn, always on the GUI thread."""
        kind = getattr(event, "kind", None) or (
            event[0] if isinstance(event, (tuple, list)) else "")
        if kind == "token":
            who = getattr(event, "who", "Writer")
            self.stream_box.append_token(who, getattr(event, "text", ""))
            return
        self._note_speaker(event, kind)
        self._refresh_tps()
        if kind == "phase":
            self.append("", getattr(event, "text", ""), "phase")
            return
        if kind == "thought":
            # Progress ("Generating response…"), not conversation: it moves
            # the "▶ who" label above and stays out of the transcript, as in
            # Tk's live_event handler.
            return
        if kind == "verdict":
            # A3: the bar appears because a verdict arrived, carrying its id.
            self.show_verdict_bar(getattr(event, "verdict_id", None))
            return
        if kind == "final":
            # A "final" is shown AS a final, as Tk's live_event handler does:
            # that is what makes the Writer's the answer (_last_answer, the
            # save panel). Remembered, because the TurnResult carries the
            # same text again at the end — see finish_turn.
            text = getattr(event, "text", "")
            self.append(getattr(event, "who", "Council"), text, "final")
            self._shown_finals = (self._shown_finals or set()) | {text}
            return
        self.append(getattr(event, "who", "Council"),
                    getattr(event, "text", str(event)), "observation")

    def _note_speaker(self, event, kind: str) -> None:
        """"▶ Writer" while the Writer is the one being waited on.

        Two sources, because the turn reports in two ways: the orchestrator's
        phase markers name the role BEFORE its call ("▶ Writer — drafting
        answer"), and an event from a personality names it directly. A stage
        ("▶ Round 1/2 — …") or the app itself ("Orchestrator", "Council")
        moves nothing."""
        who = getattr(event, "who", "") or ""
        if kind == "phase":
            text = (getattr(event, "text", "") or "").lstrip("▶ ").strip()
            who = text.split(" — ", 1)[0].strip()
        if who in _SPEAKERS:
            self.agent_label.setText(f"▶ {who}")

    def _refresh_tps(self) -> None:
        """Tokens/s from the engine's own measurement of each call this turn
        (last_call_stats: gen_tok_s, per role). Up to the three most recent
        speakers, as Tk's label shows them. A call from before this turn —
        its seq at or below the floor taken in on_send — is not this turn's."""
        stats = self.actions.last_call_stats()
        rate, seq = stats.get("gen_tok_s"), stats.get("seq")
        if not rate or seq is None or seq <= (self._turn_stats_floor or 0):
            return
        role = str(stats.get("role") or "")
        name = council_turn.AGENT_NAMES.get(role, role.title() or "Model")
        rates = dict(self._turn_rates or {})
        rates.pop(name, None)                  # most recent last
        rates[name] = rate
        self._turn_rates = rates
        shown = list(rates.items())[-3:]
        self.tps_label.setText(
            " · ".join(f"{who} {value:g}" for who, value in shown) + " tok/s")

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
        self._refresh_tps()
        if not result.ok:
            self.append("Council", result.message or "The turn failed.",
                        "observation")
            return
        # ONCE. The turn reports its answer twice — as the Writer's "final"
        # event while it runs, and in the result at the end — and this used
        # to render both, so every answer appeared twice in the transcript.
        # The result is shown only when no event already carried it (a turn
        # that reports no events still gets its answer on screen).
        if result.answer and result.answer not in (self._shown_finals or ()):
            self.append("Writer", result.answer, "final")
        if result.critique:
            self.set_judge(result.critique)
        if result.route == "direct" and result.answer:
            # Only a fast answer can be expanded, and only until the next turn
            # resets it (A4). The question is kept; the BUTTON stays disabled
            # (_label_unavailable): nothing in this build can send a fast
            # question to the full council yet — _force_full_council is reset
            # before the options are taken and the actions never read it, and
            # DEMO_MODE forces deliberation off — so enabling it here made a
            # click repeat the same answer.
            self._last_fast_question = self._last_query
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
        self.mirror.append_entry(who, text, kind)
        if transcript_core.is_final_answer(who, kind):
            self._last_answer = text
            self.save_frame.show()

    def receive_notice(self, who: str, text: str, kind: str = "final", *,
                       source: str = "") -> None:
        """A report from another tab — the IDE's snapshot path, the
        Librarian's commit receipt, a node rebuild — via
        CouncilWindow.append_transcript. On the GUI thread; the window
        guarantees that.

        LABELLED, AND NEVER AN ANSWER. It is the app reporting on something
        that happened elsewhere, so it says where it came from, and it is
        written as an observation whatever kind the caller passed: the callers
        say "final", and a "final" from "Writer" would become the last answer
        that Save answer writes out.
        """
        label = f"Notice from the {source} tab" if source else "Notice"
        self.append(who or "Council", f"[{label}] {text}",
                    "error" if kind == "error" else "observation")

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
        if questions is None:
            self._not_yet("History")
            return
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

    # -- not available yet, and saying so --------------------------------
    def _not_yet(self, what: str) -> None:
        """What one of these says if it is reached at all — the controls are
        disabled (_label_unavailable), so only code can get here. It used to
        say "needs the deliberation extracted — it is the rest of phase 6",
        which is the porter's to-do list, not something a user can act on."""
        self.append("Council", NOT_AVAILABLE.format(what=what),
                    "observation")

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
