"""
council_qt.widgets.placement_review — the weekly placement review, as a
window, and the timer that runs it.

The controller (the judge's model unless model_slots.json names a
"controller") reads a week of model usage and the machines' inventories and
proposes changes (council_core.placement). This window shows the latest
review: role changes that can be applied on this PC, each with a checkbox and
nothing applied until Apply is pressed; advice for other machines; commands
for the user to run (the Council never installs or removes a model itself);
and what was rejected and why. "Run review now" runs a fresh one.

`PlacementScheduler` runs the review once a week, on a worker, a few minutes
after the app starts and then every few hours while it is open — only when
`placement.due()` says a week has passed. It never applies anything; it
leaves a line in the status bar. COUNCIL_PLACEMENT_REVIEW=0 turns it off.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QPushButton,
                               QTabWidget, QVBoxLayout, QWidget)

from council_core import paths, placement

from .. import theme
from ..view import ViewHelpers

FIRST_CHECK_MS = 5 * 60_000
CHECK_EVERY_MS = 6 * 3600_000


def gather_live(probe: bool = True):
    """The slot config and a probe of the Ollama hosts. Blocking — call it
    from a worker. Never raises; what failed is simply absent."""
    slots, statuses = None, []
    try:
        from council_core import model_slots
        slots = model_slots.current()
    except Exception:                                     # noqa: BLE001
        pass
    if probe:
        try:
            import council_engine
            statuses = list(council_engine.build_dispatcher().probe_all()
                            or [])
        except Exception:                                 # noqa: BLE001
            pass
    return slots, statuses


def run_now(vault_dir: Path, chat: Optional[Callable[..., str]] = None,
            live: Callable[[], Any] = gather_live):
    """One review, blocking. Returns placement.run_review's tuple."""
    slots, statuses = live()
    return placement.run_review(
        vault_dir, slots=slots, statuses=statuses, chat=chat,
        main_path=os.environ.get("COUNCIL_GGUF_PATH", ""))


class PlacementReviewDialog(ViewHelpers, QDialog):
    """The latest review; Apply for role changes on this PC."""

    def __init__(self, parent=None, vault_dir: Optional[Path] = None,
                 runner: Callable[[Path], Any] = run_now):
        super().__init__(parent)
        self.window = parent
        self.bridge = getattr(parent, "bridge", None)
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self._runner = runner
        self._tokens = theme.tokens("dark")
        self._busy = False
        self.review_id = ""
        self.checked: Optional[placement.Checked] = None
        self.setWindowTitle("Weekly placement review")
        self.resize(900, 640)
        self._build()
        self.load_last()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        self.heading = QLabel("")
        self.heading.setWordWrap(True)
        outer.addWidget(self.heading)

        self.tabs = QTabWidget()
        proposal = QWidget()
        pv = QVBoxLayout(proposal)
        pv.setContentsMargins(0, 6, 0, 0)
        pv.addWidget(QLabel("Changes ready to apply — tick the ones you "
                            "want:"))
        self.apply_list = QListWidget()
        # The dark theme draws a ticked box but not an empty one, which hides
        # that there is anything to tick.
        t = self._tokens
        self.apply_list.setStyleSheet(
            f"QListWidget::indicator {{ width: 14px; height: 14px; "
            f"border: 1px solid {t['fg']}; border-radius: 2px; }}"
            f"QListWidget::indicator:checked {{ background: {t['accent']}; }}")
        pv.addWidget(self.apply_list, 1)
        self.proposal_text = QPlainTextEdit()
        self.proposal_text.setReadOnly(True)
        pv.addWidget(self.proposal_text, 2)
        self.tabs.addTab(proposal, "Proposal")
        self.report_text = QPlainTextEdit()
        self.report_text.setReadOnly(True)
        self.tabs.addTab(self.report_text, "Usage report")
        outer.addWidget(self.tabs, 1)

        row = QHBoxLayout()
        self.run_btn = self._button(row, "Run review now", self.run_review)
        self.apply_btn = self._button(row, "Apply selected", self.apply)
        self.copy_btn = self._button(row, "Copy commands", self.copy_commands)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        outer.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.status)

    # ------------------------------------------------------------------
    def load_last(self) -> None:
        last = placement.last_review(self.vault_dir)
        if last is None:
            self.heading.setText(
                "No placement review has run yet. Run one now, or the app "
                "runs one on its own once a week.")
            self._show(None, "", "", "")
            return
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last["ts"]))
        self.heading.setText(
            f"Review of {when}, by the {last.get('controller', 'judge')}'s "
            f"model." + ("  A new review is due." if
                         placement.due(self.vault_dir) else ""))
        self._show(placement.checked_from_dict(last.get("checked")),
                   last.get("report", ""), last.get("error", ""),
                   last.get("id", ""))

    def _show(self, checked: Optional[placement.Checked], report: str,
              error: str, review_id: str) -> None:
        self.checked, self.review_id = checked, review_id
        self.report_text.setPlainText(report)
        self.apply_list.clear()
        if error:
            self.proposal_text.setPlainText(
                f"The controller's review failed: {error}\n\nNothing was "
                "changed. The usage report is on the other tab.")
        elif checked is None:
            self.proposal_text.setPlainText("")
        else:
            self.proposal_text.setPlainText(checked.text())
            for c in checked.apply_now:
                where = f" on {c.machine}" if c.node else ""
                item = QListWidgetItem(
                    f"{c.role} → {c.model}{where}   ({c.reason})")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.apply_list.addItem(item)
        has_apply = bool(checked and checked.apply_now)
        self.apply_list.setVisible(has_apply)
        self.apply_btn.setEnabled(has_apply)
        self.copy_btn.setEnabled(bool(checked and checked.commands))

    # ------------------------------------------------------------------
    def selected_changes(self) -> List[placement.Change]:
        if not self.checked:
            return []
        return [c for i, c in enumerate(self.checked.apply_now)
                if self.apply_list.item(i).checkState()
                == Qt.CheckState.Checked]

    def apply(self) -> None:
        chosen = self.selected_changes()
        if not chosen:
            self.status.setText("Tick the changes to apply first.")
            return
        try:
            done = placement.apply_role_changes(self.vault_dir, chosen)
        except Exception as exc:                          # noqa: BLE001
            self.status.setText(f"Could not apply: {exc}")
            return
        if self.review_id:
            placement.log_applied(self.vault_dir, self.review_id, done)
        for i in range(self.apply_list.count()):
            item = self.apply_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self.status.setText("Applied: " + "; ".join(done) + ". The council "
                            "uses them from its next question.")

    def commands_text(self) -> str:
        if not self.checked:
            return ""
        return "\n".join(f"# on {c.machine}: {c.reason}\n{c.command}"
                         for c in self.checked.commands)

    def copy_commands(self) -> None:
        text = self.commands_text()
        if text:
            QGuiApplication.clipboard().setText(text)
            self.status.setText("Commands copied. Run them on each machine "
                                "yourself, then refresh the Apothecary.")

    def run_review(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.run_btn.setEnabled(False)
        self.status.setText("The controller is reviewing the week…")

        def work() -> None:
            try:
                result = self._runner(self.vault_dir)
                self._to_ui(lambda: self._finished(result))
            except Exception as exc:                      # noqa: BLE001
                # Formatted here: `exc` is cleared when this block ends,
                # before the lambda runs on the GUI thread.
                message = f"The review could not run: {exc}"
                self._to_ui(lambda: self.status.setText(message))
            finally:
                self._to_ui(self._done)

        threading.Thread(target=work, name="placement-review",
                         daemon=True).start()

    def _finished(self, result) -> None:
        self.load_last()
        error = result[3] if result else ""
        self.status.setText("Review failed — see the Proposal tab." if error
                            else "Review done.")

    def _done(self) -> None:
        self._busy = False
        self.run_btn.setEnabled(True)


class PlacementScheduler(ViewHelpers, QObject):
    """Runs the review when a week has passed; reports in the status bar."""

    def __init__(self, window, vault_dir: Optional[Path] = None,
                 runner: Callable[[Path], Any] = run_now,
                 first_ms: int = FIRST_CHECK_MS,
                 every_ms: int = CHECK_EVERY_MS):
        QObject.__init__(self, window)
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self._runner = runner
        self._busy = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check)
        self.timer.setInterval(every_ms)
        QTimer.singleShot(first_ms, self._start)

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("COUNCIL_PLACEMENT_REVIEW", "1").strip() != "0"

    def _start(self) -> None:
        self.check()
        self.timer.start()

    def check(self) -> bool:
        """Start a review on a worker if one is due. True when started."""
        if self._busy or not placement.due(self.vault_dir):
            return False
        self._busy = True

        def work() -> None:
            try:
                result = self._runner(self.vault_dir)
                self._to_ui(lambda: self._report(result))
            except Exception:                             # noqa: BLE001
                pass
            finally:
                self._to_ui(self._done)

        threading.Thread(target=work, name="placement-review",
                         daemon=True).start()
        return True

    def _report(self, result) -> None:
        checked, error = (result[2], result[3]) if result else (None, "")
        if error:
            text = "The weekly placement review failed — see Council Map ▸ "\
                   "Placement review."
        elif checked and (checked.apply_now or checked.advice
                          or checked.commands):
            text = "The weekly placement review proposes changes — see "\
                   "Council Map ▸ Placement review."
        else:
            text = "Weekly placement review: no changes needed."
        set_status = getattr(self.window, "set_status", None)
        if callable(set_status):
            set_status(text)

    def _done(self) -> None:
        self._busy = False


__all__ = ["PlacementReviewDialog", "PlacementScheduler", "run_now",
           "gather_live"]
