"""
council_qt.tabs.agents — the Agents tab, ported. Advanced mode, as in Tk.

What it is: a read-only board of eight optional agent subsystems, three
session toggles, a vault RAG re-index button, an agent event log, and two
embedded sub-UIs — Sage tuning and the Vault Agent. Written against
docs/qt_migration/remaining_tabs_requirements.md §agents.

DEFECTS DESIGNED OUT (numbered as in that document's table)
  1  Re-index did nothing for five minutes after startup and reported zero
     files as success. `rag_jobs.RagIndex.reindex` forces, and refuses a second
     concurrent run out loud.
  2  The Vault Agent's worker read a Tk variable. The Qt panel captures the
     role on the GUI thread (widgets/vault_agent_panel.py).
  3  Sage importable but no Sage model pinned showed a bare separator. The
     tuning panel now needs only the knowledge store, so it always shows.
  4  Two expanding siblings wasted half the tab. One vertical QSplitter.
  5  The Sage confidence boost compounded per matched term — fixed in
     sage_agent.py, where both front ends share it.
  Also: the chunk count was re-read on the GUI thread after EVERY agent event
  (and the keyword backend walks the vault to answer). It is now read on the
  worker, after a re-index, and nowhere else.

THE BOARD IS BUILT ON A WORKER
"Available" means importable, and importing vault_rag / intern_agent is real
work. The tab paints first and fills the board when the worker is done; the
toggles and the embedded panels, which depend on that answer, arrive with it.

WHAT IS NOT WIRED YET
The Qt council turn (council_core.council_turn) does not yet consult the
coder / intern / RAG switches — the Tk turn reads them at
council_gui_engine.py:18636. `toggles()` returns the frozen snapshot that turn
will take; until then they are recorded, not acted on, and the tab says so.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QGroupBox, QHBoxLayout, QLabel,
                               QPlainTextEdit, QSplitter, QTextEdit,
                               QVBoxLayout, QWidget)

from council_core import agents_status, paths, rag_jobs

from .. import theme
from ..view import ViewHelpers, amp

#: toggle attribute -> (caption, module that must be importable)
TOGGLES = (
    ("use_coder_agent", "Use Coder coding agent (self-correcting loop)",
     "coder_agent"),
    ("use_intern_agent", "Use Intern web research agent", "intern_agent"),
    ("use_rag", "Use RAG context for Writer", "vault_rag"),
)


class AgentsActions:
    """What the Agents tab can ask the application to do."""

    def __init__(self, vault_dir: Optional[Path] = None, models=None,
                 rag: Optional[rag_jobs.RagIndex] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.rag = rag or rag_jobs.for_vault(self.vault_dir)
        self._models = models
        self._problem = ""
        self._models_lock = threading.Lock()

    def availability(self) -> List[agents_status.Row]:
        return agents_status.availability()

    def models(self):
        """(personalities, problem). Loaded on first use and kept — this is
        disk and GPU work, so callers are workers."""
        from council_core import council_turn
        with self._models_lock:
            if self._models is None and not self._problem:
                self._models, self._problem = \
                    council_turn.load_personalities(self.vault_dir)
            return self._models, self._problem

    def resolve(self, role: str) -> Any:
        models, _problem = self.models()
        return getattr(models, role, None) if models is not None else None

    def sage_knowledge(self):
        import sage_agent
        return sage_agent.SageKnowledge(self.vault_dir / "sage_knowledge")


class AgentsTab(ViewHelpers, QWidget):
    """Board, toggles, re-index, log — over the Sage and Vault Agent panels."""

    def __init__(self, window=None, actions: Optional[AgentsActions] = None,
                 auto_refresh: bool = True):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or AgentsActions()
        self._tokens = theme.tokens("dark")
        self._busy = False              # the availability worker
        self._rows: List[agents_status.Row] = []
        self._panels_built = False
        self.sage_panel = None
        self.vault_panel = None
        self._build()
        if auto_refresh:
            self.refresh()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.addWidget(self._top())
        self.lower = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(self.lower)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        outer.addWidget(self.split, 1)

    def _top(self) -> QWidget:
        top = QWidget()
        layout = QVBoxLayout(top)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        status_box = QGroupBox("Agent Status")
        self.status_layout = QVBoxLayout(status_box)
        self.status_layout.addWidget(QLabel("Checking what is installed…"))
        row.addWidget(status_box, 1)

        ctrl = QGroupBox("Controls")
        ctrl_layout = QVBoxLayout(ctrl)
        self.checks = {}
        for attr, caption, _module in TOGGLES:
            box = QCheckBox(amp(caption))
            box.setEnabled(False)       # until the board says it is available
            ctrl_layout.addWidget(box)
            self.checks[attr] = box
        note = QLabel("Recorded for the session; the Qt council turn does not "
                      "read these yet.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        ctrl_layout.addWidget(note)
        buttons = QHBoxLayout()
        self.reindex_btn = self._button(buttons, "Re-index Vault Now",
                                        self.on_reindex)
        self.rag_count = QLabel("")
        buttons.addWidget(self.rag_count)
        buttons.addStretch(1)
        ctrl_layout.addLayout(buttons)
        row.addWidget(ctrl, 1)
        layout.addLayout(row)

        install = QGroupBox("Install missing dependencies")
        install_layout = QVBoxLayout(install)
        hints = QPlainTextEdit(agents_status.INSTALL_HINTS)
        hints.setReadOnly(True)
        hints.setFixedHeight(56)
        hints.setStyleSheet("font-family: Consolas, monospace; "
                            f"color: {self._tokens['accent']};")
        install_layout.addWidget(hints)
        layout.addWidget(install)

        layout.addWidget(QLabel("Agent Event Log"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(90)
        layout.addWidget(self.log, 1)
        return top

    # -- the board -------------------------------------------------------
    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True

        def work() -> None:
            try:
                rows = self.actions.availability()
                self._to_ui(lambda: self._show(rows))
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self.log_event, "agents",
                            f"Availability check error: {exc}")
            finally:
                self._to_ui(self._done)

        threading.Thread(target=work, name="agents-status",
                         daemon=True).start()

    def _done(self) -> None:
        self._busy = False

    def _show(self, rows: List[agents_status.Row]) -> None:
        self._rows = rows
        while self.status_layout.count():
            item = self.status_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for row in rows:
            label = QLabel(amp(f"{'✓' if row.available else '✗'} {row.label}"))
            label.setStyleSheet("color: " + (self._tokens["success"]
                                             if row.available
                                             else self._tokens["error"]) + ";")
            if row.problem:
                label.setToolTip(row.problem)
            self.status_layout.addWidget(label)

        defaults = agents_status.AgentToggles.defaults(rows)
        for attr, _caption, module in TOGGLES:
            box = self.checks[attr]
            box.setEnabled(agents_status.available(rows, module))
            box.setChecked(getattr(defaults, attr))
        self.reindex_btn.setEnabled(agents_status.available(rows, "vault_rag"))
        self._build_panels(rows)

    def _build_panels(self, rows: List[agents_status.Row]) -> None:
        if self._panels_built:
            return
        self._panels_built = True
        self.lower.addWidget(self._sage_side(rows))
        self.lower.addWidget(self._vault_side(rows))

    def _unavailable(self, text: str) -> QWidget:
        label = QLabel(amp(text))
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignTop)
        label.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        return label

    def _sage_side(self, rows) -> QWidget:
        if not agents_status.available(rows, "sage_agent"):
            return self._unavailable(
                "🧙 Sage not available — sage_agent.py could not be imported.")
        try:
            from ..widgets.sage_tuning import SageTuningPanel
            self.sage_panel = SageTuningPanel(
                self.actions.sage_knowledge(),
                on_changed=lambda: self.log_event("sage", "knowledge updated"))
            return self.sage_panel
        except Exception as exc:                          # noqa: BLE001
            return self._unavailable(f"🧙 Sage could not open its "
                                     f"knowledge store: {exc}")

    def _vault_side(self, rows) -> QWidget:
        if not agents_status.available(rows, "vault_agent"):
            return self._unavailable(
                "🗂  Vault Agent not available — vault_agent.py could not be "
                "imported.")
        from ..widgets.vault_agent_panel import VaultAgentPanel
        self.vault_panel = VaultAgentPanel(self.actions.resolve,
                                           self.actions.vault_dir)
        return self.vault_panel

    # -- toggles ---------------------------------------------------------
    def toggles(self) -> agents_status.AgentToggles:
        """The switches, snapshotted on the GUI thread for a worker to read."""
        return agents_status.AgentToggles(
            **{attr: box.isEnabled() and box.isChecked()
               for attr, box in self.checks.items()})

    # -- re-index --------------------------------------------------------
    def on_reindex(self) -> None:
        rag = self.actions.rag
        if rag.busy:
            self.log_event("rag_index", "A re-index is already running.")
            return
        self.reindex_btn.setEnabled(False)
        self.log_event("rag_index", "Re-indexing the vault…")
        if rag.needs_main_thread():
            # Spyder/IPython: build on the GUI thread or torch kills the kernel.
            try:
                rag.ensure()
            except Exception as exc:                      # noqa: BLE001
                self.log_event("rag_index", f"RAG index error: {exc}")
                self.reindex_btn.setEnabled(True)
                return

        def work() -> None:
            outcome = rag.reindex(force=True)
            count = rag.count()
            self._to_ui(self._reindexed, outcome, count)

        threading.Thread(target=work, name="agents-reindex",
                         daemon=True).start()

    def _reindexed(self, outcome: rag_jobs.Outcome,
                   count: Optional[int]) -> None:
        self.log_event("rag_index", outcome.message)
        if count is not None:
            self.rag_count.setText(f"Chunks indexed: {count}")
        self.reindex_btn.setEnabled(
            agents_status.available(self._rows, "vault_rag"))

    # -- the log ---------------------------------------------------------
    def log_event(self, phase: str, msg: str) -> None:
        """Append one ('agent_phase', phase, msg) event. Safe from any thread."""
        self._to_ui(self._append, phase, msg)

    def _append(self, phase: str, msg: str) -> None:
        # Tk drew ordinary phase lines in the accent red, one shade off the
        # failure red — so an error did not stand out from routine progress.
        colour = {"result": self._tokens["success"],
                  "fail": self._tokens["error"],
                  "phase": self._tokens["fg"]}[
                      agents_status.classify(phase, msg)]
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(colour))
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(agents_status.format_line(phase, msg) + "\n", fmt)
        self.log.setTextCursor(cursor)
        self.log.ensureCursorVisible()


def build_agents(window) -> QWidget:
    """Factory for the tab registry."""
    return AgentsTab(window)
