"""
council_qt.tabs.sessions — past conversations, and loading one as prior.

45 toolkit lines and the widest blast radius of phase 7: loading a session as
prior rebuilds EVERY personality, and the Tk version does that on the GUI
thread while a model call runs on a worker that writes into the transcript.

Here both halves are workers and neither touches a widget. The rebuild is the
slow one — it loads weights — and the summary is the one the Tk build gets
wrong: an AST pass over all 61 `Thread(target=...)` sites in the engine found
its summary worker is the ONLY one that calls a UI method directly.

The prior session is global state. This tab does not own the personalities, so
it hands the rebuilt set to whoever does through `on_models_changed` rather
than reaching across into another tab.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QPlainTextEdit, QVBoxLayout,
                               QWidget)

from council_core import sessions as sessions_core

from .. import theme
from ..view import ViewHelpers


class SessionsActions:
    """What the Sessions tab can ask the application to do."""

    def __init__(self, vault_dir: Optional[Path] = None, store=None,
                 models=None):
        self.vault_dir = Path(vault_dir or Path.home() / "council_vault")
        self._store = store
        self.models = models

    @property
    def verdict_path(self) -> Path:
        return self.vault_dir / "verdict_history.jsonl"

    def store(self):
        """The conversation store, opened on first use."""
        if self._store is None:
            try:
                import convo_store
                self._store = convo_store.ConvoStore(
                    self.vault_dir / "conversations")
            except Exception:                             # noqa: BLE001
                return None
        return self._store

    def listing(self):
        store = self.store()
        if store is None:
            return sessions_core.SessionListResult(
                False, "The conversation store is unavailable.")
        return sessions_core.list_sessions(store, self.verdict_path)

    def preview(self, session_id: str) -> str:
        store = self.store()
        return "" if store is None else sessions_core.preview(store, session_id)

    def rebuild(self, session_id: Optional[str]):
        return sessions_core.rebuild_with_prior(self.vault_dir, session_id)

    def ensure_summary(self, session_id: str):
        writer = getattr(self.models, "writer", None)
        store = getattr(writer, "conversation_store", None) or self.store()
        return sessions_core.ensure_summary(store, writer, session_id)

    def verdict_summary(self) -> str:
        sample, total = sessions_core.read_verdicts(self.verdict_path)
        return sessions_core.verdict_summary(total, sample)


class SessionsTab(ViewHelpers, QWidget):
    """A filtered list of past sessions, and what to do with one."""

    def __init__(self, window=None, actions: Optional[SessionsActions] = None,
                 on_models_changed: Optional[Callable] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or SessionsActions()
        self.on_models_changed = on_models_changed
        self._tokens = theme.tokens("dark")
        self._all: List[sessions_core.SessionRow] = []
        self._shown: List[sessions_core.SessionRow] = []
        self._busy = False

        self._build()
        self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        outer.addWidget(QLabel("Past Sessions (double-click to load as prior)"))

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
        self._button(buttons, "Load as Prior", self.on_load_prior)
        self._button(buttons, "Preview Session", self.on_preview)
        self._button(buttons, "Clear Prior", self.on_clear_prior)
        self._button(buttons, "Verdict History", self.on_verdict_history)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        self.prior = QLabel(sessions_core.prior_label(None))
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

    # -- the wide one ---------------------------------------------------
    def on_load_prior(self) -> None:
        session_id = self.selected_session()
        if not session_id:
            self.status.setText("Select a session to load.")
            return
        self._rebuild(session_id, f"Loading '{session_id}' as prior…")

    def on_clear_prior(self) -> None:
        self._rebuild(None, "Clearing the prior session…")

    def _rebuild(self, session_id: Optional[str], status: str) -> None:
        """Rebuild every personality against a prior session.

        On a WORKER. The Tk version does this on the GUI thread, which is a
        freeze of however long the weights take to load.
        """
        if self._busy:
            self.status.setText("Already rebuilding — wait for it to finish.")
            return
        self._busy = True
        self.status.setText(status)

        def work() -> None:
            models, problem = self.actions.rebuild(session_id)

            def show() -> None:
                self._busy = False
                if models is None:
                    self.status.setText(problem)
                    return
                self.actions.models = models
                self.prior.setText(sessions_core.prior_label(session_id))
                self.status.setText(
                    f"Prior session loaded: {session_id}" if session_id
                    else "Prior session cleared.")
                if self.on_models_changed is not None:
                    # This tab does not own the personalities. Handing them
                    # over beats reaching across into another tab's state.
                    self.on_models_changed(models, session_id)
                if session_id:
                    self._summarise(session_id)

            self._to_ui(show)

        threading.Thread(target=work, name="sessions-rebuild",
                         daemon=True).start()

    def _summarise(self, session_id: str) -> None:
        """Write the session's prose summary once, in the background.

        The Tk equivalent calls _append_transcript from inside the worker —
        the only one of the engine's 61 threads that touches a UI method
        directly.
        """
        def work() -> None:
            result = self.actions.ensure_summary(session_id)
            if result.message:
                self._to_ui(lambda: self.status.setText(result.message))

        threading.Thread(target=work, name="sessions-summary",
                         daemon=True).start()


def build_sessions(window) -> QWidget:
    """Factory for the tab registry."""
    return SessionsTab(window)
