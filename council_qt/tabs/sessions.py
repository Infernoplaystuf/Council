"""
council_qt.tabs.sessions — past conversations, and loading one into this one.

The list is every saved session — the Tk shell's and the Qt shell's are the
same files (council_core.conversations) — badged with how each one ended. It
used to be empty: it opened `convo_store`, a module that does not exist.

LOAD AS PRIOR, AND NOTHING ELSE, BRINGS AN OLDER SESSION IN
Memory is "This conversation only" by default (council_core.memory_scope):
follow-ups see this session's turns and nothing older. Loading a session here
is the one way an older one reaches a prompt. It is handed to the conversation
the Council tab owns (window.conversation), which applies it to the
personalities at the start of the next turn and says so in the transcript.

Nothing is rebuilt. The Tk version — and this tab until Batch 1 — rebuilt
every personality to change one field, on the GUI thread in Tk; and here the
rebuilt set was handed to a callback nobody connected, so loading a prior
changed nothing. Setting the field at the next turn's start is all it takes.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional

from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QPlainTextEdit, QVBoxLayout,
                               QWidget)

from council_core import conversations
from council_core import sessions as sessions_core
from council_core import paths

from .. import theme
from ..view import ViewHelpers


class SessionsActions:
    """What the Sessions tab can ask the application to do."""

    def __init__(self, vault_dir: Optional[Path] = None, store=None,
                 conversation=None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self._store = store
        #: This launch's conversation (the Council tab's), or None when the
        #: tab is built without one.
        self.conversation = conversation

    @property
    def verdict_path(self) -> Path:
        return self.vault_dir / "verdict_history.jsonl"

    def store(self):
        """The conversation store, opened on first use."""
        if self._store is None:
            try:
                self._store = conversations.ConversationStore(
                    self.vault_dir / "conversations")
            except Exception:                             # noqa: BLE001
                return None
        return self._store

    def listing(self):
        store = self.store()
        if store is None:
            return sessions_core.SessionListResult(
                False, "The conversation store is unavailable.")
        snapshot = (self.conversation.snapshot()
                    if self.conversation is not None else None)
        return sessions_core.list_sessions(
            store, self.verdict_path,
            current=snapshot.session_id if snapshot else None,
            prior=snapshot.prior_session_id if snapshot else None)

    def preview(self, session_id: str) -> str:
        store = self.store()
        return "" if store is None else sessions_core.preview(store, session_id)

    def prior(self) -> Optional[str]:
        if self.conversation is None:
            return None
        return self.conversation.snapshot().prior_session_id

    def load_prior(self, session_id: str) -> conversations.Outcome:
        if self.conversation is None:
            return conversations.Outcome(
                False, "There is no conversation to load it into — the "
                       "Council tab is not in this build.")
        return self.conversation.load_prior(session_id)

    def clear_prior(self) -> conversations.Outcome:
        if self.conversation is None:
            return conversations.Outcome(True, "Nothing is loaded.")
        return self.conversation.clear_prior()

    def verdict_summary(self) -> str:
        sample, total = sessions_core.read_verdicts(self.verdict_path)
        return sessions_core.verdict_summary(total, sample)


class SessionsTab(ViewHelpers, QWidget):
    """A filtered list of past sessions, and what to do with one."""

    def __init__(self, window=None, actions: Optional[SessionsActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        shared = getattr(window, "conversation", None)
        # The list is of the vault the conversation is kept in.
        self.actions = actions or SessionsActions(
            vault_dir=getattr(shared, "vault_dir", None), conversation=shared)
        if self.actions.conversation is None:
            self.actions.conversation = shared
        if self.actions.conversation is not None:
            self.actions.conversation.subscribe(self._on_conversation)
        self._tokens = theme.tokens("dark")
        self._all: List[sessions_core.SessionRow] = []
        self._shown: List[sessions_core.SessionRow] = []

        self._build()
        self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        outer.addWidget(QLabel(
            "Past Sessions (double-click to load one into this conversation)"))

        row = QHBoxLayout()
        glass = QLabel("🔍")
        glass.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(glass)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter by session id")
        # A string test over rows already in memory. The Tk filter traces onto
        # the full refresh, so every keystroke re-globs the conversations
        # directory and re-parses up to 500 verdict records.
        self.filter.textChanged.connect(self.apply_filter)
        row.addWidget(self.filter, 1)
        outer.addLayout(row)

        self.sessions = QListWidget()
        self.sessions.itemDoubleClicked.connect(lambda _item: self.on_load_prior())
        self.sessions.currentRowChanged.connect(self.on_selected)
        outer.addWidget(self.sessions, 1)

        buttons = QHBoxLayout()
        self._button(buttons, "Refresh", self.refresh)
        self.load_btn = self._button(buttons, "Load as Prior",
                                     self.on_load_prior)
        self.load_btn.setToolTip(
            "Bring this session into the current conversation: from the "
            "next question on, the council uses it as background. The only "
            "way an older session is used.")
        self._button(buttons, "Preview Session", self.on_preview)
        self._button(buttons, "Clear Prior", self.on_clear_prior)
        self._button(buttons, "Verdict History", self.on_verdict_history)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        self.prior = QLabel(sessions_core.prior_label(self.actions.prior()))
        self.prior.setWordWrap(True)
        outer.addWidget(self.prior)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.status)

        preview_box = QGroupBox("Session Preview")
        preview_layout = QVBoxLayout(preview_box)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        preview_layout.addWidget(self.preview)
        outer.addWidget(preview_box, 1)

    # ------------------------------------------------------------------
    def selected_session(self) -> Optional[str]:
        """The selected session's ID, read from the row object.

        Both Tk handlers recover it with `label.split("  [")[0].strip()` —
        presentation as the data model. Change the badge format and they break
        silently; an id containing the separator is truncated.
        """
        row = self.sessions.currentRow()
        return self._shown[row].id if 0 <= row < len(self._shown) else None

    def refresh(self) -> None:
        def work() -> None:
            result = self.actions.listing()

            def show() -> None:
                self._all = list(result.rows)
                self.status.setText(result.message)
                self.apply_filter()

            self._to_ui(show)

        threading.Thread(target=work, name="sessions-list", daemon=True).start()

    def apply_filter(self) -> None:
        self._shown = sessions_core.filter_rows(self._all, self.filter.text())
        self.sessions.clear()
        for row in self._shown:
            self.sessions.addItem(row.label)

    def on_selected(self, row: int) -> None:
        pass          # selection alone does nothing; Preview is explicit

    def on_preview(self) -> None:
        session_id = self.selected_session()
        if not session_id:
            self.status.setText("Select a session to preview.")
            return
        self.preview.setPlainText("Reading…")

        def work() -> None:
            text = self.actions.preview(session_id)
            self._to_ui(lambda: self.preview.setPlainText(text))

        threading.Thread(target=work, name="sessions-preview",
                         daemon=True).start()

    def on_verdict_history(self) -> None:
        def work() -> None:
            line = self.actions.verdict_summary()
            self._to_ui(lambda: self.status.setText(line))

        threading.Thread(target=work, name="sessions-verdicts",
                         daemon=True).start()

    # -- the prior --------------------------------------------------------
    def on_load_prior(self) -> None:
        """Hand the selected session to this launch's conversation. The
        Council transcript says it was loaded (the conversation tells it)."""
        session_id = self.selected_session()
        if not session_id:
            self.status.setText("Select a session to load.")
            return
        self.status.setText(self.actions.load_prior(session_id).message)

    def on_clear_prior(self) -> None:
        self.status.setText(self.actions.clear_prior().message)

    def _on_conversation(self, event: str, **_details) -> None:
        """Keep the prior line and the list's marks true, whoever changed
        them."""
        def show() -> None:
            self.prior.setText(sessions_core.prior_label(self.actions.prior()))
            self.refresh()

        if event in ("prior_loaded", "prior_cleared", "renamed"):
            self._to_ui(show)


def build_sessions(window) -> QWidget:
    """Factory for the tab registry."""
    return SessionsTab(window)
