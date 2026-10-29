"""
council_qt.widgets.build_chat — the chat beside the Build tab's editor.

Ask mode: the model answers about the open file and changes nothing
(council_core.build_chat says what it is shown). Its reply streams into the
conversation as it is written; Stop ends it where it is. Enter sends,
Shift+Enter starts a new line. "Copy code" copies the last reply's last code
block. Each folder keeps its own conversation; New chat starts it again.

The panel knows nothing of the editor: ``get_context`` (from the tab) says
what is open, and ``chat`` (from the tab's actions) calls the model, so the
tests give it a fake one.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QTextBrowser, QVBoxLayout, QWidget)

from council_core import build_chat as bc

from ..view import ViewHelpers

#: How often a streaming reply is redrawn.
REDRAW_MS = 120


class _Input(QPlainTextEdit):
    """Enter sends; Shift+Enter is a new line."""

    send = Signal()

    def keyPressEvent(self, event) -> None:                # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.send.emit()
            return
        super().keyPressEvent(event)


class ChatPanel(ViewHelpers, QWidget):
    """The conversation, the question box, Send and Stop."""

    def __init__(self, vault_dir: Path, chat: Callable[..., str],
                 get_context: Callable[[bool, bool], bc.Context],
                 parent: Optional[QWidget] = None, bridge=None):
        super().__init__(parent)
        self.bridge = bridge
        self.vault_dir = Path(vault_dir)
        self.chat = chat
        self.get_context = get_context
        self.conversation: Optional[bc.Conversation] = None
        self._stop: Optional[threading.Event] = None
        self._busy = False
        self._streamed: List[str] = []
        self._pending = ""
        self._redraw = QTimer(self)
        self._redraw.setSingleShot(True)
        self._redraw.setInterval(REDRAW_MS)
        self._redraw.timeout.connect(self.render)
        self._build()

    def _build(self) -> None:
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        title = QLabel("<b>Ask about the code</b>")
        head.addWidget(title)
        head.addStretch(1)
        self.copy_btn = self._button(head, "Copy code", self.copy_code)
        self.new_btn = self._button(head, "New chat", self.new_chat)
        col.addLayout(head)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setPlaceholderText(
            "Ask about the open file: what it does, why it fails, how to "
            "change it. The model sees the file and your selection. It "
            "answers here and changes nothing.")
        col.addWidget(self.view, 1)

        opts = QHBoxLayout()
        self.use_file = QCheckBox("Include the open file")
        self.use_file.setChecked(True)
        opts.addWidget(self.use_file)
        self.use_output = QCheckBox("Include the last run's output")
        opts.addWidget(self.use_output)
        opts.addStretch(1)
        col.addLayout(opts)

        self.input = _Input()
        self.input.setPlaceholderText("Ask… (Enter sends, Shift+Enter for a new line)")
        self.input.setMaximumHeight(90)
        self.input.send.connect(self.send)
        col.addWidget(self.input)

        row = QHBoxLayout()
        self.status = QLabel("")
        row.addWidget(self.status, 1)
        self.send_btn = self._button(row, "Send", self.send)
        self.stop_btn = self._button(row, "Stop", self.stop)
        self.stop_btn.setEnabled(False)
        col.addLayout(row)

    # -- the folder's conversation -------------------------------------------
    def set_folder(self, folder: Path) -> None:
        if self._busy:
            self.stop()
        try:
            self.conversation = bc.Conversation(self.vault_dir, str(folder))
        except OSError as exc:
            self.conversation = None
            self.status.setText(f"The chat could not be opened: {exc}")
        self.render()

    def new_chat(self) -> None:
        if self.conversation is None or self._busy:
            return
        try:
            self.conversation.clear()
        except OSError as exc:
            self.status.setText(f"Not cleared: {exc}")
        self.render()

    # -- drawing -------------------------------------------------------------
    def markdown(self) -> str:
        parts: List[str] = []
        for t in (self.conversation.turns if self.conversation else []):
            if t.role == "user":
                where = f" · {Path(t.file).name}" if t.file else ""
                parts.append(f"**You**{where}\n\n{t.content}")
            else:
                parts.append(f"**Council**\n\n{t.content}")
        if self._busy:
            turns = self.conversation.turns if self.conversation else []
            if not (turns and turns[-1].role == "user"
                    and turns[-1].content == self._pending):
                parts.append(f"**You**\n\n{self._pending}")   # not kept yet
            parts.append("**Council**\n\n" + ("".join(self._streamed) or "…"))
        return "\n\n---\n\n".join(parts)

    def render(self) -> None:
        bar = self.view.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self.view.setMarkdown(self.markdown())
        if at_end or self._busy:
            bar.setValue(bar.maximum())

    # -- asking ----------------------------------------------------------------
    def send(self) -> None:
        question = self.input.toPlainText().strip()
        if not question or self._busy or self.conversation is None:
            return
        ctx = self.get_context(self.use_file.isChecked(), self.use_output.isChecked())
        self.input.clear()
        self._busy = True
        self._streamed = []
        self._stop = threading.Event()
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.new_btn.setEnabled(False)
        self.status.setText("Answering…")
        conv, stop = self.conversation, self._stop
        # the question shows at once, before the model's first word
        self._pending = question

        def token(piece: str) -> None:
            self._to_ui(self._token, piece)

        def work() -> None:
            error = ""
            try:
                conv.ask(question, ctx, self.chat, on_token=token,
                         should_stop=stop.is_set)
            except Exception as exc:                      # noqa: BLE001
                error = ("Stopped." if type(exc).__name__ == "GenerationCancelled"
                         else f"The model did not answer: {exc}")
            self._to_ui(self._done, error)

        threading.Thread(target=work, name="build-chat", daemon=True).start()
        self.render()

    def _token(self, piece: str) -> None:
        self._streamed.append(piece)
        if not self._redraw.isActive():
            self._redraw.start()

    def _done(self, error: str) -> None:
        self._busy = False
        self._redraw.stop()
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.new_btn.setEnabled(True)
        self.status.setText(error)
        self.render()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    def is_busy(self) -> bool:
        return self._busy

    def copy_code(self) -> str:
        """The last reply's last code block, on the clipboard."""
        for t in reversed(self.conversation.turns if self.conversation else []):
            if t.role == "assistant":
                blocks = bc.code_blocks(t.content)
                if blocks:
                    QGuiApplication.clipboard().setText(blocks[-1])
                    self.status.setText("Code copied.")
                    return blocks[-1]
                break
        self.status.setText("The last answer has no code block.")
        return ""
