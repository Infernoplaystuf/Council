"""
council_qt.widgets.bench_dialog — run the council benchmark and compare
runs (council_core.council_bench), from the Council Map tab.

Run: asks every question of the chosen set through the real council on a
worker thread, one line per question as it finishes; Stop ends it after the
current question. Compare: any two runs side by side — accuracy, Judge
PASS rate, calls, seconds, model loads, seconds per step.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

from council_core import council_bench, paths

from .. import theme
from ..view import ViewHelpers


def load_council(vault_dir: Path):
    """(models, problem) — the real personalities, as the Council tab
    loads them."""
    from council_core.council_turn import load_personalities
    return load_personalities(vault_dir, session_id="bench")


class BenchDialog(ViewHelpers, QDialog):
    def __init__(self, parent=None, vault_dir: Optional[Path] = None,
                 loader: Callable[[Path], Any] = load_council,
                 turn: Optional[Callable[..., Any]] = None):
        super().__init__(parent)
        self.window = parent
        self.bridge = getattr(parent, "bridge", None)
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self._loader = loader
        self._turn = turn
        self._tokens = theme.tokens("dark")
        self._busy = False
        self._stop = threading.Event()
        self.setWindowTitle("Council benchmark")
        self.resize(860, 620)
        self._build()
        self.refresh_runs()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        intro = QLabel(
            "Asks a fixed set of questions through the council and records "
            "how many model calls, how long, how often models had to load, "
            "and whether each answer was right. Run once before a change "
            "and once after, then compare. Your own questions about your "
            "vault go in .council_bench/questions.json (set: vault).")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Label:"))
        self.label_edit = QLineEdit("baseline")
        row.addWidget(self.label_edit)
        row.addWidget(QLabel("Questions:"))
        self.set_box = QComboBox()
        self.set_box.addItems(["default", "vault", "all"])
        row.addWidget(self.set_box)
        row.addWidget(QLabel("Depth:"))
        self.depth_box = QComboBox()
        for text, value in (("per question", ""), ("Auto", "auto"),
                            ("Quick", "quick"), ("Standard", "standard"),
                            ("Deep", "deep")):
            self.depth_box.addItem(text, value)
        row.addWidget(self.depth_box)
        self.run_btn = self._button(row, "Run", self.start)
        self.stop_btn = self._button(row, "Stop", self.stop)
        self.stop_btn.setEnabled(False)
        outer.addLayout(row)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        outer.addWidget(self.output, 1)

        cmp_row = QHBoxLayout()
        cmp_row.addWidget(QLabel("Compare:"))
        self.run_a = QComboBox()
        self.run_b = QComboBox()
        cmp_row.addWidget(self.run_a, 1)
        cmp_row.addWidget(QLabel("with"))
        cmp_row.addWidget(self.run_b, 1)
        self._button(cmp_row, "Compare", self.compare)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        cmp_row.addWidget(close)
        outer.addLayout(cmp_row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.status)

    # ------------------------------------------------------------------
    def refresh_runs(self) -> None:
        runs = council_bench.runs(self.vault_dir)
        for box in (self.run_a, self.run_b):
            box.clear()
            for r in runs:
                box.addItem(f"{r.get('label')} — {r.get('stamp')} "
                            f"({r.get('accuracy', 0):.0%}, "
                            f"{r.get('mean_wall_s')} s/question)", r)
        if len(runs) >= 2:
            self.run_a.setCurrentIndex(len(runs) - 2)
            self.run_b.setCurrentIndex(len(runs) - 1)

    def compare(self) -> None:
        a, b = self.run_a.currentData(), self.run_b.currentData()
        if not a or not b:
            self.status.setText("Two runs are needed to compare.")
            return
        self.output.setPlainText(council_bench.compare(a, b))

    def say(self, line: str) -> None:
        self.output.appendPlainText(line)

    def start(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._stop.clear()
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.output.clear()
        label = self.label_edit.text().strip() or "run"
        which = self.set_box.currentText()
        depth = self.depth_box.currentData() or None
        questions = council_bench.load_questions(self.vault_dir, which)
        if not questions:
            self.say("No questions in that set. Put yours in "
                     ".council_bench/questions.json.")
            self._done()
            return
        self.status.setText(f"Loading the council, then {len(questions)} "
                            "question(s)…")

        def work() -> None:
            try:
                models, problem = self._loader(self.vault_dir)
                if models is None:
                    said = f"Could not load the council: {problem}"
                    self._to_ui(lambda: self.say(said))
                    return
                summary = council_bench.run(
                    questions, models, self.vault_dir, label=label,
                    depth=depth, turn=self._turn,
                    progress=lambda line: self._to_ui(
                        lambda line=line: self.say(line)),
                    should_stop=self._stop.is_set)
                text = (f"\n{summary['correct']}/{summary['questions']} "
                        f"correct, {summary['passed']} passed by the Judge, "
                        f"{summary['mean_calls']} calls and "
                        f"{summary['mean_wall_s']} s per question, "
                        f"{summary['loads']} model loads "
                        f"({summary['load_s']} s). Saved as "
                        f"{summary['file']}.")
                self._to_ui(lambda: self.say(text))
            except Exception as exc:                      # noqa: BLE001
                said = f"The benchmark stopped: {exc!r}"
                self._to_ui(lambda: self.say(said))
            finally:
                self._to_ui(self._done)

        threading.Thread(target=work, name="council-bench",
                         daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.status.setText("Stopping after the current question…")

    def _done(self) -> None:
        self._busy = False
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText("")
        self.refresh_runs()
