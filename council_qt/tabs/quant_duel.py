"""
council_qt.tabs.quant_duel — the Quant Duel, run by the Council.

    Settings   the duel's folder (a checkout of its `quant-duel` branch), its
               Python (blank: the checkout's own .venv), which nodes this PC
               runs (A reads news, B never does), On, and "run due jobs while
               the Council is open".
    Machine    where its LLM jobs run: the machine Machines & roles binds to
               the 'quant' role, or this PC (council_core.quant_duel.endpoint).
    Status     per node: last processed day, live / control / SPY equity,
               the last tuning round, news counts, what is due now.
    Actions    Run due now · Check (the duel's pre-flight list) · Replay last
               month (both nodes, news off — must end PASS) · Compare (after
               the experiment) · Open reports.

Every duel command runs as its own process on a worker thread; one at a time.
With the schedule on, a timer runs whatever is due every 15 minutes. Use it
or a systemd timer / Task Scheduler for a folder, not both (the duel's locks
stop two daily steps or LLM jobs overlapping; two news polls could, which
only wastes work). Nothing runs until On is ticked and saved.
"""
from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from council_core import node_routing as nr
from council_core import paths
from council_core import quant_duel as qd

from .. import theme
from ..view import ViewHelpers

COLUMNS = ("Node", "Last day", "Live", "Control", "SPY", "Last tuning",
           "News (headlines / scored)", "Due now")
TICK_MS = 15 * 60 * 1000


class QuantActions:
    """What the tab asks of the application. Tests pass a runner that
    never starts a process."""

    def __init__(self, vault_dir: Optional[Path] = None,
                 runner: Optional[qd.Runner] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.runner = runner

    def settings(self) -> qd.Settings:
        return qd.load(self.vault_dir)

    def save(self, s: qd.Settings) -> None:
        qd.save(self.vault_dir, s)

    def statuses(self, s: qd.Settings) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for node in s.nodes:
            try:
                out[node] = qd.status(s, node, self.runner)
            except Exception as exc:                      # noqa: BLE001
                out[node] = exc
        return out

    def tick(self, s: qd.Settings, log: Callable[[str], None]):
        return qd.tick(s, self.runner, log=log)

    def run(self, s: qd.Settings, node: Optional[str], args, llm=False):
        return qd.run(s, node, args, llm=llm, runner=self.runner)


class QuantDuelTab(ViewHelpers, QWidget):
    def __init__(self, window=None, actions: Optional[QuantActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or QuantActions()
        self._tokens = theme.tokens("dark")
        self._busy = False
        self._build()
        self._fill_settings(self.actions.settings())
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self.on_timer)
        self._apply_schedule()
        self._shown_once = False

    # -- layout -----------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        intro = QLabel(
            "The Quant Duel asks whether news helps an LLM-tuned trading "
            "model: node A reads news, node B never does, and a month of "
            "paper trading is compared. " + qd.DISCLOSURE)
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Folder:"))
        self.root_edit = QLineEdit()
        self.root_edit.setPlaceholderText("a checkout of the quant-duel "
                                          "branch")
        row.addWidget(self.root_edit, 1)
        self._button(row, "Browse…", self.browse)
        row.addWidget(QLabel("Python:"))
        self.python_edit = QLineEdit()
        self.python_edit.setPlaceholderText("blank: the folder's .venv")
        row.addWidget(self.python_edit, 1)
        outer.addLayout(row)

        row = QHBoxLayout()
        self.node_boxes = {}
        for node in qd.NODES:
            kind = "news" if node == "A" else "no news"
            box = QCheckBox(f"Node {node} ({kind})")
            self.node_boxes[node] = box
            row.addWidget(box)
        self.enabled_box = QCheckBox("On")
        row.addWidget(self.enabled_box)
        self.schedule_box = QCheckBox("Run due jobs while the Council is "
                                      "open (every 15 min)")
        row.addWidget(self.schedule_box)
        row.addStretch(1)
        self._button(row, "Save", self.save)
        outer.addLayout(row)

        row = QHBoxLayout()
        self.machine = QLabel("")
        self.machine.setWordWrap(True)
        row.addWidget(self.machine, 1)
        self._button(row, "Machines & roles…", self.open_routing)
        outer.addLayout(row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        outer.addWidget(self.table)

        row = QHBoxLayout()
        self.buttons = [
            self._button(row, "Refresh", self.refresh),
            self._button(row, "Run due now", self.run_due),
            self._button(row, "Check", self.check),
            self._button(row, "Replay last month", self.replay),
            self._button(row, "Compare", self.compare),
            self._button(row, "Open reports", self.open_reports),
        ]
        row.addStretch(1)
        self.status = QLabel("")
        row.addWidget(self.status)
        outer.addLayout(row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        outer.addWidget(self.log, 1)

    # -- settings ---------------------------------------------------------
    def _fill_settings(self, s: qd.Settings) -> None:
        self.root_edit.setText(s.root)
        self.python_edit.setText(s.python)
        for node, box in self.node_boxes.items():
            box.setChecked(node in s.nodes)
        self.enabled_box.setChecked(s.enabled)
        self.schedule_box.setChecked(s.schedule)
        self.machine.setText(qd.where_llm_runs())

    def collect(self) -> qd.Settings:
        nodes = [n for n, b in self.node_boxes.items() if b.isChecked()]
        return qd.Settings(enabled=self.enabled_box.isChecked(),
                           root=self.root_edit.text().strip(),
                           python=self.python_edit.text().strip(),
                           nodes=nodes or list(qd.NODES),
                           schedule=self.schedule_box.isChecked())

    def save(self) -> bool:
        s = self.collect()
        why = qd.problem(s) if s.enabled else ""
        if why:
            self.status.setText(why)
            return False
        self.actions.save(s)
        self._apply_schedule()
        self.status.setText("Saved." + (" The schedule is on." if
                                        s.enabled and s.schedule else ""))
        return True

    def _apply_schedule(self) -> None:
        s = self.actions.settings()
        if s.enabled and s.schedule:
            self.timer.start()
        else:
            self.timer.stop()

    def browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Quant Duel folder",
                                                  self.root_edit.text())
        if folder:
            self.root_edit.setText(folder)

    def open_routing(self) -> None:
        from ..widgets.machine_routing import MachineRoutingDialog
        MachineRoutingDialog(self, self.actions.vault_dir).exec()
        nr.invalidate()
        self.machine.setText(qd.where_llm_runs())

    # -- work -------------------------------------------------------------
    def _say(self, text: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        self._to_ui(lambda: self.log.appendPlainText(f"{stamp}  {text}"))

    def _busy_on(self, on: bool, status: str = "") -> None:
        self._busy = on
        for b in self.buttons:
            b.setEnabled(not on)
        if status:
            self.status.setText(status)

    def _start_job(self, status: str, fn: Callable[[], Any],
              done: Optional[Callable[[Any], None]] = None) -> bool:
        if self._busy:
            self.status.setText("Busy — one duel command at a time.")
            return False
        self._busy_on(True, status)

        def work():
            try:
                result = fn()
                if done is not None:
                    self._to_ui(lambda: done(result))
            except Exception as exc:                      # noqa: BLE001
                self._say(f"{type(exc).__name__}: {exc}")
            finally:
                self._to_ui(lambda: self._busy_on(False, ""))
        # One literal name, so the window drains it on close
        # (TAB_WORKERS in tests/test_council_qt_foundation.py).
        threading.Thread(target=work, name="quant-job", daemon=True).start()
        return True

    def _settings_or_say(self) -> Optional[qd.Settings]:
        s = self.actions.settings()
        why = qd.problem(s)
        if why:
            self.status.setText(why)
            return None
        return s

    def refresh(self) -> bool:
        self.machine.setText(qd.where_llm_runs())
        s = self._settings_or_say()
        if s is None:
            return False
        return self._start_job("Reading status…",
                          lambda: self.actions.statuses(s), self.show_status)

    def show_status(self, statuses: Dict[str, Any]) -> None:
        rows = qd.status_rows(statuses)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem(str(text)))
        self.status.setText(f"Updated {dt.datetime.now():%H:%M}.")

    def run_due(self, scheduled: bool = False) -> bool:
        s = self.actions.settings()
        if not s.enabled:
            self.status.setText("Tick On and Save first.")
            return False
        if qd.problem(s):
            self.status.setText(qd.problem(s))
            return False

        def go():
            results = self.actions.tick(s, self._say)
            if not results:
                self._say("Nothing due." + (" (scheduled)" if scheduled
                                            else ""))
            return self.actions.statuses(s)
        return self._start_job("Running due jobs…", go, self.show_status)

    def on_timer(self) -> None:
        if not self._busy:
            self.run_due(scheduled=True)

    def _one(self, node: Optional[str], args, label: str) -> bool:
        s = self._settings_or_say()
        if s is None:
            return False

        def go():
            r = self.actions.run(s, node, args)
            self._say(r.line())
            for line in r.output.splitlines()[-60:]:
                self._say("    " + line)
            return r
        return self._start_job(label, go)

    def check(self) -> bool:
        node = next((n for n, b in self.node_boxes.items() if b.isChecked()),
                    "A")
        return self._one(node, ["check"], f"Checking node {node}…")

    def replay(self) -> bool:
        today = dt.date.today()
        first = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
        last = today.replace(day=1) - dt.timedelta(days=1)
        return self._one(None, ["replay", "--start", first.isoformat(),
                                "--end", last.isoformat()],
                         f"Replaying {first:%B %Y} (several minutes)…")

    def compare(self) -> bool:
        return self._one(None, ["compare"], "Comparing A and B…")

    def open_reports(self) -> None:
        s = self.actions.settings()
        if s.root:
            QDesktopServices.openUrl(QUrl.fromLocalFile(
                str(Path(s.root) / "reports")))

    def showEvent(self, event) -> None:                   # noqa: N802
        super().showEvent(event)
        if not self._shown_once:
            self._shown_once = True
            if not qd.problem(self.actions.settings()):
                self.refresh()


def build_quant_duel(window) -> QWidget:
    """Factory for the tab registry."""
    return QuantDuelTab(window)
