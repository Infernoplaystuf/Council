"""
council_qt.widgets.build_chat — the chat beside the Build tab's editor.

ASK mode: the model answers about the open file and changes nothing
(council_core.build_chat says what it is shown).

EDIT mode (council_core.build_edits): the model answers with edit blocks.
Each is matched against its file and shown under the reply as a diff with
Accept and Reject; "Accept all" takes the rest, "Apply" puts the accepted
ones into the files (the tab does that: into the editor, and in a session
also committed as one turn). A block that does not match is refused with the
closest text, and the model is asked once, by itself, to send it again.
A request that needs several files gets a PLAN card first, marked green,
amber or red with Council's reasons and the model's questions; nothing is
edited until the user answers or presses Go.

STOP WHEN STUCK
When an edit still does not match after the second request, or the tab's
test rounds stop (the same failure twice, or three rounds), ask_blocker asks
the model in Ask mode what it tried, the error, and what it needs from the
user; its answer is shown as "Council · stuck".

THE STEP LIST
Under each Edit-mode reply, a folded "▸ N steps": what the model was shown,
each edit it proposed and what became of it, the second request for the
blocks that did not match, what was applied and committed. Clicking a step
opens that file at that line, or shows that edit's diff.

The reply streams in as it is written; Stop ends it where it is. Enter sends,
Shift+Enter starts a new line. "Copy code" copies the last reply's last code
block. Each folder keeps its own conversation; New chat starts it again.

AGENT mode and the Model box: council_qt.widgets.build_agent.

The panel knows nothing of the editor: ``get_context`` (from the tab) says
what is open, ``chat`` calls the model, and ``host`` (the tab) reads and
applies edits, so the tests give it fakes.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional, Set
from urllib.parse import quote, unquote

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QPlainTextEdit, QTextBrowser, QVBoxLayout,
                               QWidget)

from council_core import build_chat as bc
from council_core import build_edits as be

from ..view import ViewHelpers
from .build_agent import AGENT, AgentMode, ModelPicker

#: How often a streaming reply is redrawn.
REDRAW_MS = 120
ASK, EDIT = "ask", "edit"
GO_TEXT = "Go ahead with the plan."
_MARKS = {be.GREEN: "🟢 Green", be.AMBER: "🟠 Amber", be.RED: "🔴 Red"}


class _Input(QPlainTextEdit):
    """Enter sends; Shift+Enter is a new line."""

    send = Signal()

    def keyPressEvent(self, event) -> None:                # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.send.emit()
            return
        super().keyPressEvent(event)


def _q(text: str) -> str:
    return quote(text, safe="")


class ChatPanel(ViewHelpers, QWidget):
    """The conversation, the question box, Send and Stop, and in Edit mode
    the proposed edits and the plan card."""

    def __init__(self, vault_dir: Path, chat: Callable[..., str],
                 get_context: Callable[[bool, bool], bc.Context],
                 parent: Optional[QWidget] = None, bridge=None, host=None,
                 agent=None, models_source=None):
        super().__init__(parent)
        self.bridge = bridge
        self.host = host
        self.vault_dir = Path(vault_dir)
        self.chat = chat
        self.get_context = get_context
        self.conversation: Optional[bc.Conversation] = None
        self._stop: Optional[threading.Event] = None
        self._busy = False
        self._streamed: List[str] = []
        self._pending = ""
        self.proposal: Optional[be.Proposal] = None
        self._proposal_turn = -1            # the assistant turn it belongs to
        self._request = ""                  # what the proposal is for
        self._open_steps: Set[int] = set()  # turns whose step list is open
        self._shown_diffs: Set[str] = set() # steps whose diff is shown
        self._redraw = QTimer(self)
        self._redraw.setSingleShot(True)
        self._redraw.setInterval(REDRAW_MS)
        self._redraw.timeout.connect(self.render)
        self.agent_mode = AgentMode(self, agent)
        self.models = ModelPicker(self, self.vault_dir, models_source)
        self._build()

    def _build(self) -> None:
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Chat</b>"))
        self.mode_box = QComboBox()
        self.mode_box.addItem("Ask: answers only", ASK)
        self.mode_box.addItem("Edit: proposes changes", EDIT)
        self.mode_box.addItem("Agent: plans, then works on a branch", AGENT)
        self.mode_box.currentIndexChanged.connect(self._mode_changed)
        head.addWidget(self.mode_box)
        head.addStretch(1)
        self.copy_btn = self._button(head, "Copy code", self.copy_code)
        self.new_btn = self._button(head, "New chat", self.new_chat)
        col.addLayout(head)

        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Model:"))
        self.model_box = QComboBox()
        self.model_box.addItem("Main model (as the Council)", "")
        self.model_box.activated.connect(self.models.chosen)
        mrow.addWidget(self.model_box, 1)
        col.addLayout(mrow)

        self.agent_row = QWidget()
        arow = QHBoxLayout(self.agent_row)
        arow.setContentsMargins(0, 0, 0, 0)
        arow.addWidget(QLabel("Agent:"))
        self.profile_box = QComboBox()
        arow.addWidget(self.profile_box, 1)
        self.agents_btn = self._button(arow, "Agents…", self.on_agents)
        self.project_btn = self._button(arow, "Project…", self.on_project)
        col.addWidget(self.agent_row)

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        self.view.anchorClicked.connect(self.on_link)
        col.addWidget(self.view, 1)

        review = QHBoxLayout()
        self.accept_all_btn = self._button(review, "Accept all", self.accept_all)
        self.reject_all_btn = self._button(review, "Reject all", self.reject_all)
        self.apply_btn = self._button(review, "Apply accepted", self.apply)
        self.go_btn = self._button(review, "Go", self.go)
        review.addStretch(1)
        col.addLayout(review)

        opts = QHBoxLayout()
        self.use_file = QCheckBox("Include the open file")
        self.use_file.setChecked(True)
        opts.addWidget(self.use_file)
        self.use_output = QCheckBox("Include the last run's output")
        opts.addWidget(self.use_output)
        opts.addStretch(1)
        col.addLayout(opts)

        self.input = _Input()
        self.input.setMaximumHeight(90)
        self.input.send.connect(self.send)
        col.addWidget(self.input)

        row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.send_btn = self._button(row, "Send", self.send)
        self.stop_btn = self._button(row, "Stop", self.stop)
        self.stop_btn.setEnabled(False)
        col.addLayout(row)
        self._mode_changed()

    # -- mode ---------------------------------------------------------------
    def mode(self) -> str:
        return self.mode_box.currentData() or ASK

    def set_mode(self, mode: str) -> None:
        self.mode_box.setCurrentIndex(max(0, self.mode_box.findData(mode)))

    def _mode_changed(self, *_a) -> None:
        agent = self.mode() == AGENT
        self.agent_row.setVisible(agent)
        if agent:
            self.agent_mode.fill_profiles()
            self.view.setPlaceholderText(
                "Describe a change to the folder: the council plans it, you "
                "approve the plan, and an agent makes it on a branch of its own, "
                "checking and committing each step. Your folder is not touched "
                "until you merge. The folder must be a git folder.")
            self.input.setPlaceholderText("Describe the change… (Enter sends)")
        elif self.mode() == EDIT:
            self.view.setPlaceholderText(
                "Ask for a change: \"add a --verbose flag\", \"fix the error in "
                "the output\". The model proposes edits; you accept or reject "
                "each one before anything changes.")
            self.input.setPlaceholderText("Ask for a change… (Enter sends)")
        else:
            self.view.setPlaceholderText(
                "Ask about the open file: what it does, why it fails, how to "
                "change it. The model sees the file and your selection. It "
                "answers here and changes nothing.")
            self.input.setPlaceholderText("Ask… (Enter sends, Shift+Enter for a new line)")
        self._update_buttons()

    # -- the folder's conversation -------------------------------------------
    def set_folder(self, folder: Path) -> None:
        if self._busy:
            self.stop()
        self.proposal = None
        try:
            self.conversation = bc.Conversation(self.vault_dir, str(folder))
        except OSError as exc:
            self.conversation = None
            self.status.setText(f"The chat could not be opened: {exc}")
        self.render()

    def new_chat(self) -> None:
        if self.conversation is None or self._busy:
            return
        self.proposal = None
        try:
            self.conversation.clear()
        except OSError as exc:
            self.status.setText(f"Not cleared: {exc}")
        self.render()

    def turns(self) -> List[bc.Turn]:
        return self.conversation.turns if self.conversation else []

    # -- drawing -------------------------------------------------------------
    def _steps_md(self, i: int, t: bc.Turn) -> str:
        steps = [s for s in t.steps
                 if s.get("kind") not in ("plan", "agent-plan", "agent-job")]
        if not steps:
            return ""
        arrow = "▾" if i in self._open_steps else "▸"
        out = [f"[{arrow} {len(steps)} step{'s' if len(steps) != 1 else ''}](steps:{i})"]
        if i in self._open_steps:
            for j, s in enumerate(steps):
                mark = {"ok": "✓", "no": "✗", "wait": "…"}.get(s.get("state", "ok"), "·")
                text = s.get("text", "")
                target = s.get("target", "")
                line = f"[{text}]({target})" if target else text
                out.append(f"- {mark} {line}")
                key = f"{i}:{j}"
                if s.get("diff") and key in self._shown_diffs:
                    out.append(f"\n```diff\n{s['diff']}\n```\n")
                elif s.get("diff"):
                    out[-1] += f" · [diff](diff:{key})"
        return "\n".join(out)

    def _plan_md(self, i: int, t: bc.Turn) -> str:
        card = next((s for s in t.steps if s.get("kind") == "plan"), None)
        if card is None:
            return ""
        out = [f"**Plan · {_MARKS.get(card.get('mark'), card.get('mark', ''))}**"]
        if card.get("files"):
            out.append("Files: " + ", ".join(f"`{f}`" for f in card["files"]))
        out += [f"{n}. {s.lstrip('0123456789.) ')}" for n, s in enumerate(card.get("steps", []), 1)]
        if card.get("reasons"):
            out.append("*Why this mark:* " + " ".join(card["reasons"]))
        if card.get("questions"):
            out.append("**Questions for you**")
            out += [f"- {q}" for q in card["questions"]]
        if self._plan_is_open(i):
            hint = ("Answer below, or [Go](go:) to start the edits."
                    if card.get("questions") else "[Go](go:) to start the edits, or say what to change.")
            out.append(hint)
        return "\n\n".join(out)

    def _plan_is_open(self, i: int) -> bool:
        """The plan at turn ``i`` is the newest turn, so Go can answer it."""
        turns = self.turns()
        return (i == len(turns) - 1 and turns[i].kind == "plan" and not self._busy)

    def _proposal_md(self) -> str:
        prop = self.proposal
        if prop is None:
            return ""
        edits = prop.edits()
        ok = [e for _c, e in edits if e.ok]
        out = [f"**Proposed edits** · {len(ok)} of {len(edits)} can be applied"]
        for fc, e in edits:
            b = e.block
            if not e.ok:
                out.append(f"**{b.index}. `{b.path}`: refused.** {e.error}")
                continue
            line = fc.first_line(e)
            state = {True: "✓ accepted · [Reject](reject:{n})",
                     False: "✗ rejected · [Accept](accept:{n})",
                     None: "[Accept](accept:{n}) · [Reject](reject:{n})"}[e.accepted]
            why = f": {b.reason}" if b.reason else ""
            new = "" if fc.exists else " (new file)"
            out.append(f"**{b.index}. [`{b.path}` line {line}](open:{_q(b.path)}:{line})"
                       f"{new}**{why}  \n{state.format(n=b.index)}")
            out.append(f"```diff\n{fc.diff(e)}\n```")
        return "\n\n".join(out)

    def markdown(self) -> str:
        parts: List[str] = []
        turns = self.turns()
        for i, t in enumerate(turns):
            if t.role == "user":
                if t.kind == "retry":
                    parts.append("*Council asked the model again for the edits that "
                                 "did not match.*")
                    continue
                if t.kind == "blocker":
                    why = t.content.split(": ", 1)[1].split(". Do not", 1)[0] \
                        if ": " in t.content else ""
                    parts.append(f"*Council stopped the fix ({why}) and asked the "
                                 "model what is in the way.*")
                    continue
                where = f" · {Path(t.file).name}" if t.file else ""
                mode = {EDIT: " · edit", AGENT: " · agent"}.get(t.mode, "")
                parts.append(f"**You**{where}{mode}\n\n{t.content}")
                continue
            if t.mode == AGENT:
                text = self.agent_mode.markdown(i, t)
            else:
                text = be.prose(t.content) if t.mode == EDIT else t.content
            who = "**Council · stuck**" if i and turns[i - 1].kind == "blocker" else "**Council**"
            if t.mode == AGENT:
                who = "**Council · agent**"
            block = [f"{who}\n\n{text}" if text else who]
            plan = self._plan_md(i, t)
            if plan:
                block.append(plan)
            if i == self._proposal_turn and self.proposal is not None:
                block.append(self._proposal_md())
            steps = self._steps_md(i, t)
            if steps:
                block.append(steps)
            parts.append("\n\n".join(block))
        if self._busy:
            if not (turns and turns[-1].role == "user"
                    and turns[-1].content == self._pending):
                parts.append(f"**You**\n\n{self._pending}")   # not kept yet
            parts.append("**Council**\n\n" + ("".join(self._streamed) or "…"))
        return "\n\n---\n\n".join(parts)

    def render(self) -> None:
        bar = self.view.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        keep = bar.value()
        self.view.setMarkdown(self.markdown())
        bar.setValue(bar.maximum() if (at_end or self._busy) else keep)
        self._update_buttons()

    def _update_buttons(self) -> None:
        prop = self.proposal
        pending = bool(prop and prop.pending())
        accepted = bool(prop and prop.accepted_changes())
        for b in (self.accept_all_btn, self.reject_all_btn):
            b.setVisible(prop is not None)
            b.setEnabled(pending and not self._busy)
        self.apply_btn.setVisible(prop is not None)
        self.apply_btn.setEnabled(accepted and not self._busy)
        turns = self.turns()
        plan_open = bool(turns) and (self._plan_is_open(len(turns) - 1)
                                     or self.agent_mode.plan_is_open())
        self.go_btn.setVisible(plan_open)
        self.go_btn.setEnabled(plan_open)

    # -- links -----------------------------------------------------------------
    def on_link(self, url: QUrl) -> None:
        self.follow(url.toString())

    def follow(self, link: str) -> None:
        kind, _, rest = link.partition(":")
        if kind == "steps":
            i = int(rest)
            self._open_steps ^= {i}
        elif kind == "diff":
            self._shown_diffs ^= {rest}
        elif kind == "accept":
            self.decide(int(rest), True)
            return
        elif kind == "reject":
            self.decide(int(rest), False)
            return
        elif kind == "go":
            self.go()
            return
        elif kind == "use" and rest.isdigit():
            self.agent_mode.toggle(int(rest))
            return
        elif kind == "review":
            self.agent_mode.review(rest)
            return
        elif kind == "discard":
            self.agent_mode.discard(rest)
            return
        elif kind == "open" and self.host is not None:
            path, _, line = rest.rpartition(":")
            self.host.open_location(unquote(path), int(line or 1))
            return
        self.render()

    # -- asking ----------------------------------------------------------------
    def send(self, question: Optional[str] = None, kind: str = "",
             mode: Optional[str] = None) -> None:
        """Ask. ``kind`` marks a turn the Council started: "go" (a plan's Go),
        "fix" (Fix this, or a test round), "blocker" (the rounds stopped)."""
        question = (question if question is not None else self.input.toPlainText()).strip()
        if not question or self._busy or self.conversation is None:
            return
        if self.proposal is not None and kind != "blocker":
            self.status.setText("Apply or reject the proposed edits first.")
            return
        mode = mode or self.mode()
        if mode == AGENT:
            self.agent_mode.send(question)
            return
        ctx = self.get_context(self.use_file.isChecked(), self.use_output.isChecked())
        plan_files: List[str] = []
        if mode == EDIT and self.host is not None:
            ctx.numbered = False
            ctx.root = str(self.host.edit_root())
            if kind == "go":
                plan_files = self._open_plan_files()
            ctx.others = self.host.other_files(ctx.path, plan_files)
        if not kind:
            self.input.clear()
        if kind != "blocker":
            self.proposal = None
        self._busy = True
        self._streamed = []
        self._stop = threading.Event()
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.new_btn.setEnabled(False)
        self.mode_box.setEnabled(False)
        self.status.setText("Answering…" if mode == ASK else "Working on the change…")
        conv, stop, host = self.conversation, self._stop, self.host
        self._pending = question
        if kind != "blocker":                # a pending proposal keeps its request
            self._request = question if kind != "go" else self._plan_request()

        def token(piece: str) -> None:
            self._to_ui(self._token, piece)

        def work() -> None:
            error, cmap = "", None
            try:
                if mode == EDIT and host is not None:
                    ctx.code_map, cmap = host.code_map(self._request)
                    conv.ask(question, ctx, self.chat, on_token=token,
                             should_stop=stop.is_set, system=be.EDIT_SYSTEM,
                             mode=EDIT, kind=kind)
                else:
                    conv.ask(question, ctx, self.chat, on_token=token,
                             should_stop=stop.is_set, kind=kind)
            except Exception as exc:                      # noqa: BLE001
                error = ("Stopped." if type(exc).__name__ == "GenerationCancelled"
                         else f"The model did not answer: {exc}")
            self._to_ui(self._done, error, mode, ctx, cmap)

        threading.Thread(target=work, name="build-chat", daemon=True).start()
        self.render()

    def _plan_request(self) -> str:
        """The request a plan was made for (the user turn before it)."""
        turns = self.turns()
        for t in reversed(turns[:-1]):
            if t.role == "user" and t.kind not in ("retry", "go"):
                return t.content
        return GO_TEXT

    def _open_plan_files(self) -> List[str]:
        turns = self.turns()
        if not turns:
            return []
        card = next((s for s in turns[-1].steps if s.get("kind") == "plan"), {})
        return [f.replace("(new)", "").strip() for f in card.get("files", [])]

    def _token(self, piece: str) -> None:
        self._streamed.append(piece)
        if not self._redraw.isActive():
            self._redraw.start()

    def _finish(self, error: str) -> None:
        self._busy = False
        self._redraw.stop()
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.new_btn.setEnabled(True)
        self.mode_box.setEnabled(True)
        self.profile_box.setEnabled(True)
        self.status.setText(error)
        self.render()

    def _done(self, error: str, mode: str = ASK, ctx=None, cmap=None,
              retried: Optional[List[be.Edit]] = None) -> None:
        turns = self.turns()
        if error or mode != EDIT or self.host is None or not turns or \
                turns[-1].role != "assistant":
            self._finish(error)
            return
        reply = turns[-1]
        root = Path(ctx.root) if ctx and ctx.root else self.host.edit_root()
        default = ctx.shown_path() if ctx and ctx.path else ""
        if retried is not None:
            first = self._proposal_turn
            blocks = [e.block for _c, e in (self.proposal.edits() if self.proposal else [])
                      if e.ok] + be.parse_blocks(reply.content, default)
            reply = turns[first]
        else:
            blocks = be.parse_blocks(reply.content, default)
            plan = be.parse_plan(reply.content) if not blocks else None
            reply.steps = self._shown_steps(ctx)
            if plan is not None:
                self._plan_card(reply, plan, root, cmap)
                self.conversation.save()
                self._finish("")
                return
            if not blocks:
                self.conversation.save()
                self._finish("")
                return
            self._proposal_turn = len(turns) - 1
        for n, b in enumerate(blocks, 1):
            b.index = n
        prop = be.propose(blocks, root, self._reader())
        refused = prop.refused()
        if refused and retried is None:
            self.proposal = prop
            self._ask_again(refused, ctx, cmap)
            return
        if retried:
            reply.steps.append({"kind": "retry", "state": "ok",
                                "text": f"Asked again for {len(retried)} edit(s) that did not match"})
        self.proposal = prop
        for fc, e in prop.edits():
            reply.steps.append(self._edit_step(fc, e))
        self.conversation.save()
        still = prop.refused()
        self._finish("" if not still else
                     f"{len(still)} edit(s) could not be applied; see why above.")
        if still and retried:
            # refused twice: stop asking for it again, ask what is in the way
            reply.steps.append({"kind": "stuck", "state": "no",
                                "text": f"Stopped: {len(still)} edit(s) did not match twice"})
            self.ask_blocker(f"{len(still)} of your edits could not be matched to the "
                             "file twice")

    def _reader(self):
        host = self.host

        def read(rel: str) -> Optional[str]:
            return host.read_for_edit(rel)
        return read

    def _shown_steps(self, ctx) -> List[dict]:
        steps: List[dict] = []
        if ctx is None:
            return steps
        if ctx.path:
            rel = ctx.shown_path()
            n = ctx.text.count("\n") + 1
            steps.append({"kind": "read", "text": f"Read {rel} ({n} lines)",
                          "target": f"open:{_q(rel)}:{ctx.cursor_line}"})
        for path, text in ctx.others:
            rel = ctx.shown_path(path)
            steps.append({"kind": "read", "text": f"Read {rel} ({text.count(chr(10)) + 1} lines)",
                          "target": f"open:{_q(rel)}:1"})
        if ctx.code_map:
            steps.append({"kind": "read", "text": "Read the folder's code map"})
        if ctx.output.strip():
            steps.append({"kind": "read", "text": "Read the last run's output"})
        return steps

    def _edit_step(self, fc: be.FileChange, e: be.Edit) -> dict:
        b = e.block
        if not e.ok:
            return {"kind": "edit", "state": "no", "index": b.index,
                    "text": f"Edit {b.index} to {b.path} refused: {e.error.split('. ')[0]}"}
        line = fc.first_line(e)
        why = f": {b.reason}" if b.reason else ""
        return {"kind": "edit", "state": "wait", "index": b.index, "path": b.path,
                "text": f"Proposed edit {b.index} to {b.path} line {line}{why}",
                "target": f"open:{_q(b.path)}:{line}", "diff": fc.diff(e)}

    def _ask_again(self, refused: List[be.Edit], ctx, cmap) -> None:
        conv, stop = self.conversation, self._stop
        note = be.retry_note(refused)
        self.status.setText("Some edits did not match; asking the model again…")
        self._streamed = []
        self._pending = note

        def token(piece: str) -> None:
            self._to_ui(self._token, piece)

        def work() -> None:
            error = ""
            try:
                conv.ask(note, ctx, self.chat, on_token=token,
                         should_stop=stop.is_set, system=be.EDIT_SYSTEM,
                         mode=EDIT, kind="retry")
            except Exception as exc:                      # noqa: BLE001
                error = ("Stopped." if type(exc).__name__ == "GenerationCancelled"
                         else f"The model did not answer: {exc}")
            self._to_ui(self._after_retry, error, ctx, cmap, refused)

        threading.Thread(target=work, name="build-chat-retry", daemon=True).start()
        self.render()

    def _after_retry(self, error: str, ctx, cmap, refused) -> None:
        if error:
            # keep what matched the first time
            prop, turn = self.proposal, self.turns()[self._proposal_turn]
            for fc, e in prop.edits():
                turn.steps.append(self._edit_step(fc, e))
            self.conversation.save()
            self._finish(error)
            return
        self._done("", EDIT, ctx, cmap, retried=refused)

    def _plan_card(self, turn: bc.Turn, plan: be.Plan, root: Path, cmap) -> None:
        def exists(f: str) -> bool:
            rel = be.safe_rel(root, f)
            return rel is not None and (Path(root) / rel).exists()

        def known(name: str) -> bool:
            return cmap is None or bool(cmap.find(name))
        level, reasons = be.mark(plan, exists, known)
        turn.kind = "plan"
        turn.steps.insert(0, {"kind": "plan", "mark": level, "reasons": reasons,
                              "files": plan.files, "steps": plan.steps,
                              "questions": plan.questions})

    # -- the proposal ----------------------------------------------------------
    def _edit(self, index: int) -> Optional[be.Edit]:
        if self.proposal is None:
            return None
        return next((e for _c, e in self.proposal.edits()
                     if e.block.index == index and e.ok), None)

    def decide(self, index: int, accept: bool) -> None:
        e = self._edit(index)
        if e is not None and not self._busy:
            e.accepted = accept
        self.render()

    def accept_all(self) -> None:
        if self.proposal is not None:
            self.proposal.accept_all()
        self.render()

    def reject_all(self) -> None:
        if self.proposal is not None:
            for _c, e in self.proposal.edits():
                if e.ok and e.accepted is None:
                    e.accepted = False
        self.render()

    def apply(self) -> None:
        """Put the accepted edits into the files (the tab does it) and note
        what became of each in the turn's step list."""
        prop = self.proposal
        if prop is None or self._busy or self.host is None:
            return
        for _c, e in prop.edits():
            if e.ok and e.accepted is None:
                e.accepted = False             # undecided = not applied
        turn = self.turns()[self._proposal_turn]
        try:
            steps, message = self.host.apply_edits(prop, self._request)
        except Exception as exc:                          # noqa: BLE001
            self.status.setText(f"Not applied: {exc}")
            return
        states = {e.block.index: e.accepted for _c, e in prop.edits() if e.ok}
        for s in turn.steps:
            if s.get("kind") == "edit" and s.get("index") in states:
                ok = states[s["index"]]
                s["state"] = "ok" if ok else "no"
                s["text"] = s["text"].replace("Proposed edit", "Applied edit" if ok
                                              else "Rejected edit", 1)
        turn.steps += steps
        turn.kind = "edits"
        self.proposal = None
        self._proposal_turn = -1
        self._open_steps.add(self.turns().index(turn))
        if self.conversation is not None:
            self.conversation.save()
        self.status.setText(message)
        self.render()

    def ask_blocker(self, why: str) -> None:
        """The fix stops here: ask the model, in Ask mode, what is in the way
        (council_core.build_chat.STUCK)."""
        if self._busy or self.conversation is None:
            return
        self.send(bc.STUCK.format(why=why), kind="blocker", mode=ASK)

    def go(self) -> None:
        if self.agent_mode.plan_is_open():
            self.agent_mode.go()
            return
        turns = self.turns()
        if turns and self._plan_is_open(len(turns) - 1):
            self.set_mode(EDIT)
            self.send(GO_TEXT, kind="go")

    def note(self, text: str, steps: Optional[List[dict]] = None) -> None:
        """A line from the tab under the newest reply (an undone turn)."""
        turns = self.turns()
        last = next((t for t in reversed(turns) if t.role == "assistant"), None)
        if last is not None and steps:
            last.steps += steps
            if self.conversation is not None:
                try:
                    self.conversation.save()
                except OSError:
                    pass
        self.status.setText(text)
        self.render()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    def showEvent(self, event) -> None:                    # noqa: N802
        super().showEvent(event)
        self.models.ensure()

    # -- Agent mode's agents and project ----------------------------------------
    def on_agents(self) -> None:
        from .code_dialogs import ProfileDialog
        dlg = ProfileDialog(self, self.agent_mode.profile())

        def done(code) -> None:
            if code and self.agent_mode.agent is not None:
                saved = self.agent_mode.agent.save_profile(dlg.profile())
                self.agent_mode.fill_profiles()
                self.profile_box.setCurrentIndex(max(0, next(
                    (i for i, p in enumerate(self.agent_mode._profiles)
                     if p.name == saved.name), 0)))
        dlg.finished.connect(done)
        dlg.open()
        self._dialog = dlg

    def on_project(self) -> None:
        from .code_dialogs import ProjectDialog
        project = self.agent_mode.project()
        if project is None:
            return
        dlg = ProjectDialog(self, project)

        def done(code) -> None:
            if code:
                p = dlg.project()
                p.root = project.root           # the project is the open folder's
                if self.agent_mode.agent.is_saved(project):
                    p.name = project.name       # one project per folder
                self.agent_mode.agent.save_project(p)
                self.status.setText(f"Saved the project {p.name}.")
        dlg.finished.connect(done)
        dlg.open()
        self._dialog = dlg

    def is_busy(self) -> bool:
        return self._busy

    def copy_code(self) -> str:
        """The last reply's last code block, on the clipboard."""
        for t in reversed(self.turns()):
            if t.role == "assistant":
                blocks = bc.code_blocks(t.content)
                if blocks:
                    QGuiApplication.clipboard().setText(blocks[-1])
                    self.status.setText("Code copied.")
                    return blocks[-1]
                break
        self.status.setText("The last answer has no code block.")
        return ""
