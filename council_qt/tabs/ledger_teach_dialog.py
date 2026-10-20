"""
council_qt.tabs.ledger_teach_dialog — "Teach from a sample workbook", the
Ledger tab's dialog over council_core/ledger_teach.py (user, 2026-10-20:
"taking a sample workbook from the folder and I line up where they match the
templates, or say data found in the images and what they are in the
template. First I want an LLM guess to be made, then I give an answer —
hopefully this will help train the Ledger and knowledge graph to work").

  1. Pick ONE workbook - from the tab's sources (files and folders, the
     Skip patterns honoured) or Browse… - and whether Council's model is
     asked where the rules stop (pick_model: a downloaded GGUF or Ollama
     model, US-origin only) or the rules only.
  2. Guess: ledger_teach.guess reads it exactly as Extract does, on a
     'ledger-teach-guess' worker, with progress and Cancel.
  3. Answer: one row per thing found, Council's guess pre-selected in its
     dropdown; the People section (initials -> full name); the part number
     and "save its shape as a part-name pattern".
  4. Save: ledger_teach.learn on a 'ledger-teach-learn' worker (it may open
     the knowledge graph); what was learned is shown here and in the
     Ledger log. "Guess again" then shows what the rules place now.

Kept apart from the tab module (ledger_dialogs.py says why). Nothing here
reads a workbook, lists a folder, asks a model or opens the knowledge graph
on the GUI thread: workers do, and hand back through Qt signals.
"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QDialog, QDialogButtonBox,
                               QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

import source_extract as se
from council_core import ledger_teach as lt
from sample_ledger import LedgerError

XLSX = "Excel workbooks (*.xlsx *.xlsm *.xls *.xlsb)"
#: Workbooks listed from the sources, at most: a share of thousands would
#: make the dropdown useless; Browse… reaches the rest.
MAX_LISTED = 500
#: The answers table's columns, and the widest each starts (the answer
#: stays in view without scrolling sideways). 'Read in the picture' shows
#: only when the workbook has pictures: what the image reader read, for a
#: thing found in one.
COLUMNS = ("Where", "Label as written", "Example value", "Council's guess", "From",
           "Your answer", "Read in the picture")
_WIDEST = (170, 190, 140, 190, 160, 230, 260)
PICTURE_COL = COLUMNS.index("Read in the picture")
ANSWER_COL = COLUMNS.index("Your answer")
#: A picture's text shown in its cell, at most (the whole of it in the tip).
PICTURE_CELL_CHARS = 80


def model_name(model_id: str) -> str:
    """A picked model in plain words: 'granite3.3:8b (Ollama)', 'x.gguf'."""
    if not model_id:
        return ""
    if model_id.startswith("ollama:"):
        return model_id.split(":", 1)[1] + " (Ollama)"
    return Path(model_id).name


def from_text(g: lt.Guess) -> str:
    """Where a guess came from, and how sure, for the From column."""
    if g.via == lt.VIA_NONE:
        return "nobody placed it"
    return f"{g.source()}, {round(100 * g.score)}% sure" if g.score else g.source()


class _AnswerBox(QComboBox):
    """A row's answer dropdown. The mouse wheel scrolls the table, not the
    answer under the pointer: a scroll through 100 rows changed answers
    nobody looked at."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):                            # noqa: N802 (Qt's name)
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class TeachDialog(QDialog):
    """Council guesses what each thing in ONE sample workbook is; the user
    answers; Save keeps the answers for the next Extract and the knowledge
    graph (see the module's docstring).

    ``guess(csv, workbook, model_id=, cancel=, progress=)`` and
    ``learn(csv, session, answers, people, part=, save_pattern=)`` are the
    Ledger tab's (LedgerActions.teach_guess / teach_learn with the tab's
    image reader, pattern and Connections vault bound); by default
    ledger_teach's own, the graph untouched. ``pick_model(parent)`` asks
    which model ('' when cancelled); ``browse(parent)`` picks one workbook
    ('' when cancelled; a file dialog by default); ``log(text)`` writes the
    Ledger log; ``on_learned(report)`` tells the tab what was saved. All of
    them are called on the GUI thread but guess and learn, which run on
    the workers."""

    _listed = Signal(object, str)          # (workbook paths, error text)
    _progressed = Signal(str, object)      # (line, fraction or None)
    _guessed = Signal(object, str)         # ((TeachSession, people list) or None, error)
    _learned = Signal(object, str)         # (LearnReport or None, error)

    def __init__(self, csv_path: str, *, sources: Sequence[str] = (),
                 prefer: Sequence[str] = (), ignore_patterns: Sequence[str] = (),
                 template_path: Optional[str] = None,
                 guess: Optional[Callable] = None, learn: Optional[Callable] = None,
                 pick_model: Optional[Callable] = None,
                 browse: Optional[Callable] = None,
                 log: Optional[Callable[[str], None]] = None,
                 on_learned: Optional[Callable] = None,
                 pattern_mask: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Teach the Ledger from a sample workbook")
        self.resize(1140, 880)
        self.csv_path = str(csv_path)
        self._guess = guess or lt.guess
        self._learn = learn or lt.learn
        if pick_model is None:
            from .ledger_dialogs import pick_model as _pick
            pick_model = _pick
        self._pick_model = pick_model
        self._browse = browse
        self._log = log or (lambda _text: None)
        self._on_learned = on_learned
        self._prefer = [str(p) for p in prefer]
        self._browsed: List[str] = []
        self._listing = False
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        #: the model the user picked ('' : not yet); the one the last
        #: guess used, which Guess again uses too
        self.model_id = ""
        self._last_model = ""
        #: what is shown: the guess being answered, the one before Save
        #: (for Guess again's 'what changed'), and the people list
        self.session: Optional[lt.TeachSession] = None
        self._before: Optional[lt.TeachSession] = None
        self._known_people: Dict[str, str] = {}
        self._rows: Dict[str, lt.TeachRow] = {}
        self._row_index: Dict[str, int] = {}
        self.answer_boxes: Dict[str, QComboBox] = {}
        self.people_edits: Dict[str, QLineEdit] = {}
        self.last_report: Optional[lt.LearnReport] = None

        self._listed.connect(self._on_listed)
        self._progressed.connect(self._on_progress)
        self._guessed.connect(self._on_guessed)
        self._learned.connect(self._on_learned_result)
        self._build(pattern_mask)
        self._list_sources(list(sources), list(ignore_patterns), template_path)
        self._refresh()

    # ── layout ──────────────────────────────────────────────────────────
    def _build(self, pattern_mask: str) -> None:
        root = QVBoxLayout(self)
        intro = QLabel(
            "Council reads one workbook the way Extract does and guesses what each thing "
            "in it is. Check its guesses, change the wrong ones, name the inspectors, and "
            "Save: the next Extract places them by itself, and Council's model learns "
            "from your answers.")
        intro.setWordWrap(True)
        root.addWidget(intro)

        pick = QGridLayout()
        pick.addWidget(QLabel("Sample workbook:"), 0, 0)
        self.workbook_choice = QComboBox()
        self.workbook_choice.setMinimumContentsLength(40)
        self.workbook_choice.setSizeAdjustPolicy(            # a long path does not widen it
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.workbook_choice.currentIndexChanged.connect(self._refresh)
        pick.addWidget(self.workbook_choice, 0, 1)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self.on_browse)
        pick.addWidget(self.browse_btn, 0, 2)
        self.use_model = QCheckBox("Ask Council's model where the rules stop")
        self.use_model.setChecked(True)
        self.use_model.setToolTip(
            "Your GGUF or a US-origin Ollama model on this computer. Only labels, short "
            "cell text and part numbers are sent to it, with your past answers as "
            "examples. Untick it to see what the Ledger's rules alone make of the "
            "workbook.")
        self.use_model.toggled.connect(self._refresh)
        pick.addWidget(self.use_model, 1, 1)
        model_row = QHBoxLayout()
        self.model_label = QLabel("You pick the model when you press Guess.")
        model_row.addWidget(self.model_label, 1)
        self.model_btn = QPushButton("Pick a model…")
        self.model_btn.clicked.connect(self.on_pick_model)
        model_row.addWidget(self.model_btn)
        pick.addLayout(model_row, 2, 1, 1, 2)
        if pattern_mask:
            pick.addWidget(QLabel(f"Part-name pattern from the tab: {pattern_mask}"), 3, 1)
        pick.setColumnStretch(1, 1)
        root.addLayout(pick)

        run = QHBoxLayout()
        self.guess_btn = QPushButton("Guess")
        self.guess_btn.setToolTip("Read the workbook and show Council's guess for "
                                  "each thing in it.")
        self.guess_btn.clicked.connect(self.on_guess)
        run.addWidget(self.guess_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Stop reading, or stop asking Council's model: what "
                                   "the rules found is still shown.")
        self.cancel_btn.clicked.connect(self.on_cancel)
        run.addWidget(self.cancel_btn)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        run.addWidget(self.progress, 3)
        run.addStretch(1)                       # the buttons keep their size when it hides
        root.addLayout(run)
        self.status = QLabel("Pick a workbook, then press Guess.")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.found_title = QLabel("What Council found")
        bold = QFont(self.found_title.font())
        bold.setBold(True)
        self.found_title.setFont(bold)
        root.addWidget(self.found_title)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(self._show_detail)
        self.table.setMinimumHeight(240)
        root.addWidget(self.table, 1)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.detail)
        self.changed_label = QLabel("")
        root.addWidget(self.changed_label)

        lower = QHBoxLayout()
        people = QGroupBox("People: who the initials stand for")
        pv = QVBoxLayout(people)
        self.people_table = QTableWidget(0, 3)
        self.people_table.setHorizontalHeaderLabels(["Initials", "Full name", "First seen"])
        self.people_table.verticalHeader().setVisible(False)
        self.people_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.people_table.horizontalHeader().setStretchLastSection(True)
        self.people_table.setColumnWidth(1, 200)
        pv.addWidget(self.people_table)
        self.people_none = QLabel("No inspectors' initials found yet. Answer \"Inspector\" "
                                  "for a row to add theirs.")
        self.people_none.setWordWrap(True)
        pv.addWidget(self.people_none)
        lower.addWidget(people, 3)

        part = QGroupBox("Part number")
        partv = QVBoxLayout(part)
        row = QHBoxLayout()
        row.addWidget(QLabel("This workbook's part:"))
        self.part_edit = QLineEdit()
        self.part_edit.setPlaceholderText("type it if Council did not find it")
        self.part_edit.textChanged.connect(self._part_changed)
        row.addWidget(self.part_edit, 1)
        partv.addLayout(row)
        self.part_how = QLabel("")
        self.part_how.setWordWrap(True)
        partv.addWidget(self.part_how)
        self.save_pattern = QCheckBox("Save its shape as this Ledger's part-name pattern")
        self.save_pattern.setToolTip(
            "Letters become @, digits #, anything else stays as it is: ABC1DE234 is "
            "@@@#@@###. Extract uses it when the tab's pattern field is empty.")
        partv.addWidget(self.save_pattern)
        partv.addStretch(1)
        lower.addWidget(part, 2)
        # the answers table takes the height; these keep to a few lines
        people.setMaximumHeight(190)
        part.setMaximumHeight(190)
        root.addLayout(lower)

        actions = QHBoxLayout()
        self.save_btn = QPushButton("Save my answers")
        self.save_btn.setToolTip(
            "Keep what your answers mean beside the parent CSV (label mappings, ignored "
            "labels, inspectors' labels, people, the pattern) and tick the knowledge "
            "graph's field rules they prove.")
        self.save_btn.clicked.connect(self.on_save)
        actions.addWidget(self.save_btn)
        self.again_btn = QPushButton("Guess again")
        self.again_btn.setToolTip("Read the same workbook again with what you saved, to "
                                  "see what the rules place now.")
        self.again_btn.clicked.connect(self.on_guess_again)
        actions.addWidget(self.again_btn)
        actions.addStretch(1)
        root.addLayout(actions)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setMaximumHeight(120)
        self.result.setPlaceholderText("What Council found, and what your answers taught "
                                       "it, shows here.")
        root.addWidget(self.result)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._part_changed("")

    # ── state ───────────────────────────────────────────────────────────
    @property
    def busy(self) -> bool:
        """A guess or a save is running."""
        return self._worker is not None

    def _refresh(self, *_args) -> None:
        if not hasattr(self, "result"):
            return                                          # still building
        idle = not self.busy
        has_rows = self.session is not None and bool(self.session.rows)
        self.workbook_choice.setEnabled(idle)
        self.browse_btn.setEnabled(idle)
        self.use_model.setEnabled(idle)
        self.model_btn.setEnabled(idle and self.use_model.isChecked())
        self.model_label.setEnabled(self.use_model.isChecked())
        self.guess_btn.setEnabled(idle and bool(self.current_workbook()))
        self.cancel_btn.setEnabled(not idle and self._cancellable)
        self.save_btn.setEnabled(idle and self.session is not None
                                 and (has_rows or bool(self.part_edit.text().strip())))
        self.again_btn.setEnabled(idle and self.last_report is not None
                                  and self.session is not None)
        self.table.setEnabled(idle)
        self.people_table.setEnabled(idle)
        self.part_edit.setEnabled(idle and self.session is not None)
        self.save_pattern.setEnabled(idle and bool(self.part_edit.text().strip()))

    _cancellable = False

    def current_workbook(self) -> str:
        data = self.workbook_choice.currentData()
        return str(data) if data else ""

    def wait(self, timeout: float = 60.0) -> bool:
        """Block until the listing, a guess or a save has shown its result
        (tests, scripts)."""
        end = time.monotonic() + timeout
        while (self.busy or self._listing) and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.01)
        QApplication.processEvents()
        return not (self.busy or self._listing)

    def _emit(self, signal, *args) -> None:
        """A worker's result to the GUI thread; a dialog already gone gets
        nothing (its signal raises once the C++ side is deleted)."""
        try:
            signal.emit(*args)
        except RuntimeError:
            pass

    def _say(self, text: str) -> None:
        try:
            self._log(text)
        except Exception as exc:                            # noqa: BLE001
            print(f"[ledger_teach_dialog] the Ledger log raised: {exc!r}", flush=True)

    # ── 1. the workbook ─────────────────────────────────────────────────
    def _list_sources(self, sources: List[str], ignore_patterns: List[str],
                      template_path: Optional[str]) -> None:
        """The workbooks in the tab's sources, listed on a worker: a folder
        on a share can take a while to walk."""
        if not sources:
            self.workbook_choice.setPlaceholderText("No sources in step 2 — Browse… for one")
            self.status.setText("No source workbooks in step 2 yet: Browse… for one "
                                "sample workbook.")
            return
        self._listing = True
        self.workbook_choice.setPlaceholderText("Looking in your sources…")
        self.status.setText("Looking for workbooks in your sources…")

        def work():
            found, err = [], ""
            try:
                from council_core.ledger_ingest import collect_sources
                kept, _skipped = collect_sources(sources, ignore_patterns, template_path)
                found = [str(p) for p in kept]
            except Exception as exc:                        # noqa: BLE001
                err = f"Could not list your sources: {exc}"
            self._emit(self._listed, found, err)
        threading.Thread(target=work, name="ledger-teach-list", daemon=True).start()

    def _on_listed(self, paths, err: str) -> None:
        self._listing = False
        paths = list(paths or [])
        self._fill_workbooks(paths)
        if err:
            self.status.setText(err + " — Browse… for one workbook.")
        elif not paths:
            self.status.setText("No workbooks in your sources (or every one is skipped): "
                                "Browse… for one.")
            self.workbook_choice.setPlaceholderText("No workbooks found — Browse… for one")
        else:
            more = (f" (the first {MAX_LISTED} are listed; Browse… for another)"
                    if len(paths) > MAX_LISTED else "")
            self.status.setText(f"{len(paths)} workbook(s) in your sources{more}. Pick one, "
                                "then press Guess.")
        self._refresh()

    def _fill_workbooks(self, listed: List[str]) -> None:
        """The dropdown: workbooks picked with Browse… first, then the
        listed ones; the choice kept, else the one selected in the tab."""
        keep = self.current_workbook()
        self.workbook_choice.blockSignals(True)
        try:
            self.workbook_choice.clear()
            for p in self._browsed + [p for p in listed[:MAX_LISTED]
                                      if p not in self._browsed]:
                path = Path(p)
                self.workbook_choice.addItem(f"{path.name}   —   {path.parent}", p)
                self.workbook_choice.setItemData(self.workbook_choice.count() - 1, p,
                                                 Qt.ItemDataRole.ToolTipRole)
            for want in [keep] + self._prefer:
                i = self.workbook_choice.findData(want) if want else -1
                if i >= 0:
                    self.workbook_choice.setCurrentIndex(i)
                    break
            else:
                self.workbook_choice.setCurrentIndex(0 if self.workbook_choice.count() else -1)
        finally:
            self.workbook_choice.blockSignals(False)
        self._refresh()

    def on_browse(self) -> None:
        if self._browse is not None:
            path = self._browse(self)
        else:
            from PySide6.QtWidgets import QFileDialog
            start = str(Path(self.current_workbook() or self.csv_path).parent)
            path, _ = QFileDialog.getOpenFileName(self, "Pick ONE sample workbook", start,
                                                  XLSX)
        if not path:
            return
        path = str(path)
        if path not in self._browsed:
            self._browsed.insert(0, path)
        listed = [str(self.workbook_choice.itemData(i))
                  for i in range(self.workbook_choice.count())]
        self._fill_workbooks(listed)
        self.workbook_choice.setCurrentIndex(self.workbook_choice.findData(path))
        self.status.setText(f"{Path(path).name} picked. Press Guess.")

    # ── 2. the guess ────────────────────────────────────────────────────
    def on_pick_model(self) -> bool:
        """Ask which model answers (every downloaded GGUF and Ollama model,
        US-origin only). False when the user cancelled."""
        picked = self._pick_model(self)
        if not picked:
            return False
        self.model_id = str(picked)
        self.model_label.setText(f"Council's model: {model_name(self.model_id)}")
        self.model_btn.setText("Change model…")
        return True

    def on_guess(self) -> None:
        workbook = self.current_workbook()
        if self.busy or not workbook:
            if not workbook:
                self.status.setText("Pick a workbook first: one from your sources, or "
                                    "Browse… for one.")
            return
        model_id = ""
        if self.use_model.isChecked():
            if not self.model_id and not self.on_pick_model():
                self.status.setText("Not guessed: no model picked. Untick \"Ask Council's "
                                    "model where the rules stop\" to guess with the rules "
                                    "only.")
                return
            model_id = self.model_id
        self._before = None
        self._start_guess(workbook, model_id)

    def on_guess_again(self) -> None:
        """The same workbook, read again with what was saved: what the
        model had to guess, the rules place now."""
        if self.busy or self.session is None:
            return
        self._before = self.session
        self._start_guess(self.session.workbook, self._last_model)

    def _start_guess(self, workbook: str, model_id: str) -> None:
        self._cancel = threading.Event()
        self._last_model = model_id
        cancel, csv, guess = self._cancel, self.csv_path, self._guess
        name = Path(workbook).name
        how = f"Council's model: {model_name(model_id)}" if model_id else "the rules only"
        self._say(f"── Teach: guessing what is in {name} ({how})")
        self.status.setText(f"Reading {name}…")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)

        def progress(line, fraction=None) -> None:
            self._emit(self._progressed, str(line), fraction)

        def work():
            got, err = None, ""
            try:
                session = guess(csv, workbook, model_id=model_id or None, cancel=cancel,
                                progress=progress)
                # the people list, for initials the user marks Inspector later
                got = (session, se.load_people(csv))
            except LedgerError as exc:
                err = str(exc)
            except Exception as exc:                        # noqa: BLE001
                err = f"failed: {exc!r}\n{traceback.format_exc()}"
            self._emit(self._guessed, got, err)
        self._cancellable = True
        self._worker = threading.Thread(target=work, name="ledger-teach-guess", daemon=True)
        self._refresh()
        self._worker.start()

    def on_cancel(self) -> None:
        """Stop the guess: the read stops at the next sheet, Council's model
        at its next question (what the rules found is kept). A workbook
        stuck opening in Excel (a sign-in or Enable Editing prompt) is let
        go by ending the hidden Excel this run started, as the tab's Cancel
        does."""
        if not self.busy or not self._cancellable:
            return
        self._cancel.set()
        try:
            from excel_open import kill_active_excels
            killed = kill_active_excels()
        except Exception:                                   # noqa: BLE001
            killed = 0
        self.status.setText("Cancelling…" + (f" (+{killed} Excel process(es) ended)"
                                             if killed else ""))
        self.cancel_btn.setEnabled(False)

    def _on_progress(self, line: str, fraction) -> None:
        if not self.busy:
            return                                          # a late heartbeat
        self.status.setText(line)
        if fraction is None or fraction <= 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(100 * fraction))

    def _on_guessed(self, got, err: str) -> None:
        self._worker = None
        self._cancellable = False
        self.progress.setVisible(False)
        before, self._before = self._before, None
        if err or got is None:
            text = err or "nothing came back"
            self.status.setText(text.splitlines()[0])
            self._say(f"Teach: {text}")
            self._refresh()
            return
        session, known = got
        self.session = session
        self._known_people = dict(known or {})
        self.last_report = None if before is None else self.last_report
        self._fill(session)
        said = session.describe()
        self._say(said)
        if before is not None:
            better = lt.improvement(before, session)
            self.result.setPlainText(f"Guess again: {better}\n\n{said}")
            self._say(f"Teach — guess again: {better}")
            self.status.setText(f"Guess again: {better}")
        else:
            self.result.setPlainText(said)
            self.status.setText(self._summary(session))
        self._refresh()

    def _summary(self, s: lt.TeachSession) -> str:
        name = Path(s.workbook).name
        if s.cancelled and not s.rows:
            return f"Cancelled before {name} was read: nothing to answer."
        n = {via: sum(1 for r in s.rows if r.guess.via == via)
             for via in (lt.VIA_RULE, lt.VIA_ALIAS, lt.VIA_MODEL, lt.VIA_NONE)}
        head = (f"Cancelled while Council's model was asked: the rules' guesses are shown. "
                if s.cancelled else "")
        if not s.rows:
            return head + (f"Nothing to answer in {name}: no labels, part number or "
                           "inspectors were found. Try another workbook.")
        return (head + f"{len(s.rows)} thing(s) found in {name}: "
                f"{n[lt.VIA_RULE] + n[lt.VIA_ALIAS]} placed by the rules, "
                f"{n[lt.VIA_MODEL]} guessed by Council's model, {n[lt.VIA_NONE]} unsure. "
                "Check the guesses, change any that are wrong, then Save.")

    # ── 3. the answers ──────────────────────────────────────────────────
    def _fill(self, s: lt.TeachSession) -> None:
        """One row per thing found, its guess pre-selected; the People
        section; the part number."""
        self.found_title.setText(f"What Council found in {Path(s.workbook).name}"
                                 + (f" ({s.hidden} more not shown)" if s.hidden else ""))
        self.table.setRowCount(0)
        self.answer_boxes.clear()
        self._rows = {r.key: r for r in s.rows}
        self._row_index = {}
        self.table.setRowCount(len(s.rows))
        for i, row in enumerate(s.rows):
            self._row_index[row.key] = i
            guessed = row.guess.text(s.item_field)
            if row.item:
                guessed += f" ({s.item_field or 'item'} {row.item})"
            example = row.example + (f"  (+{row.count - 1} more)" if row.count > 1 else "")
            read = " ".join(row.picture_text.split()) if row.picture else ""
            if len(read) > PICTURE_CELL_CHARS:
                read = read[:PICTURE_CELL_CHARS - 1].rstrip() + "…"
            tip = self._row_tip(row)
            for col, text in zip((0, 1, 2, 3, 4, PICTURE_COL),
                                 (row.where, row.label, example, guessed,
                                  from_text(row.guess), read)):
                item = QTableWidgetItem(text)
                if tip:
                    item.setToolTip(tip)
                self.table.setItem(i, col, item)
            box = _AnswerBox()
            for key, text in s.choices:
                box.addItem(text, key)
            if box.findData(row.guess.key) < 0:             # a field the list does not name
                box.addItem(guessed, row.guess.key)
            box.setCurrentIndex(box.findData(row.guess.key))
            box.setToolTip("What this really is. Council's guess is picked to begin with.")
            box.currentIndexChanged.connect(
                lambda _i, key=row.key: self._answer_changed(key))
            self.table.setCellWidget(i, ANSWER_COL, box)
            self.answer_boxes[row.key] = box
        self.table.setColumnHidden(PICTURE_COL, not any(r.picture for r in s.rows))
        self.table.resizeColumnsToContents()
        for col, most in enumerate(_WIDEST):
            self.table.setColumnWidth(col, most if col == ANSWER_COL
                                      else min(self.table.columnWidth(col), most))
        self.detail.setText("Pick a row to see its notes"
                            + (" and what the image reader read." if s.pictures else "."))
        # the People section
        self.people_table.setRowCount(0)
        self.people_edits.clear()
        for p in s.inspectors:
            self._add_person(p.initials, p.full_name, p.where)
        self.people_none.setVisible(not s.inspectors)
        # the part number
        part = s.part.value if s.part is not None else ""
        self.part_edit.setText(part)
        more = [p for p in s.parts if p != part]
        if s.part is not None:
            how = f"Found: {s.part.how}."
        else:
            how = "Not found in the workbook: type it if you know it."
        if more:
            how += " The workbook also names: " + ", ".join(more[:5]) + "."
        self.part_how.setText(how)
        self.save_pattern.setChecked(False)
        self._count_changes()

    @staticmethod
    def _row_tip(row: lt.TeachRow) -> str:
        lines = []
        if row.note:
            lines.append(row.note)
        if row.picture:
            lines.append(f"Read from {row.picture} by the image reader:\n"
                         f"{row.picture_text or '(no text)'}")
        return "\n".join(lines)

    def _show_detail(self, r: int, *_args) -> None:
        key = next((k for k, i in self._row_index.items() if i == r), None)
        row = self._rows.get(key) if key else None
        if row is None:
            return
        parts = [f"{row.label} — {row.where}"]
        if row.count > 1:
            parts.append(f"{row.count} values under or beside it")
        if row.note:
            parts.append(row.note)
        if row.initials:
            parts.append("initials: " + ", ".join(row.initials))
        text = "; ".join(parts) + "."
        if row.picture:
            text += (f"\nWhat the image reader read in {row.picture}: "
                     f"{row.picture_text or '(no text)'}")
        self.detail.setText(text)

    def _answer_changed(self, key: str) -> None:
        row, box = self._rows.get(key), self.answer_boxes.get(key)
        if row is None or box is None:
            return
        changed = box.currentData() != row.guess.key
        item = self.table.item(self._row_index[key], 1)
        if item is not None:
            font = QFont(item.font())
            font.setBold(changed)
            item.setFont(font)
        if box.currentData() == lt.ROLE_INSPECTOR:
            # the initials beside an inspector's label go to the People
            # section, to be named
            for k in row.initials or lt.initials_in(row.example):
                self._add_person(k, self._known_people.get(k, ""), row.where)
        self._count_changes()

    def _count_changes(self) -> None:
        n = sum(1 for k, box in self.answer_boxes.items()
                if k in self._rows and box.currentData() != self._rows[k].guess.key)
        self.changed_label.setText(f"{n} answer(s) changed from Council's guess."
                                   if n else "")

    def set_answer(self, key: str, choice: str) -> None:
        """Pick ``choice`` (a choice key: 'field:<name>', 'part', 'ignore'
        …) in the row's dropdown, as the user would."""
        box = self.answer_boxes[key]
        i = box.findData(choice)
        if i < 0:
            raise KeyError(f"{choice!r} is not an answer this Ledger offers")
        box.setCurrentIndex(i)

    def answers(self) -> Dict[str, str]:
        """Every row's answer: {row key: choice key}."""
        return {k: str(box.currentData()) for k, box in self.answer_boxes.items()}

    def _add_person(self, initials: str, full_name: str = "", where: str = "") -> None:
        if not initials or initials in self.people_edits:
            return
        r = self.people_table.rowCount()
        self.people_table.insertRow(r)
        self.people_table.setItem(r, 0, QTableWidgetItem(initials))
        edit = QLineEdit(full_name)
        edit.setPlaceholderText("full name (empty: not known)")
        self.people_table.setCellWidget(r, 1, edit)
        self.people_table.setItem(r, 2, QTableWidgetItem(where))
        self.people_edits[initials] = edit
        self.people_none.setVisible(False)

    def people(self) -> Dict[str, str]:
        """{initials: full name} from the People section (empty ones too:
        learn skips them)."""
        return {k: " ".join(e.text().split()) for k, e in self.people_edits.items()}

    def _part_changed(self, text: str) -> None:
        mask = se.mask_from_part(text.strip()) if text.strip() else ""
        self.save_pattern.setText(
            f"Save its shape {mask} as this Ledger's part-name pattern" if mask
            else "Save its shape as this Ledger's part-name pattern")
        self._refresh()

    # ── 4. learn ────────────────────────────────────────────────────────
    def on_save(self) -> None:
        if self.busy or self.session is None:
            return
        session, csv, learn = self.session, self.csv_path, self._learn
        answers, people = self.answers(), self.people()
        part = " ".join(self.part_edit.text().split())
        keep = self.save_pattern.isChecked() and bool(part)
        self.status.setText("Saving your answers…")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)

        def work():
            report, err = None, ""
            try:
                report = learn(csv, session, answers, people, part=part or None,
                               save_pattern=keep)
            except LedgerError as exc:
                err = str(exc)
            except Exception as exc:                        # noqa: BLE001
                err = f"failed: {exc!r}\n{traceback.format_exc()}"
            self._emit(self._learned, report, err)
        self._cancellable = False
        self._worker = threading.Thread(target=work, name="ledger-teach-learn", daemon=True)
        self._refresh()
        self._worker.start()

    def _on_learned_result(self, report, err: str) -> None:
        self._worker = None
        self.progress.setVisible(False)
        if err or report is None:
            text = err or "nothing came back"
            self.status.setText("Not saved: " + text.splitlines()[0])
            self._say(f"Teach: not saved: {text}")
            self._refresh()
            return
        self.last_report = report
        said = report.describe()
        self.result.setPlainText(said)
        self._say(said)
        if report.learned:
            self.status.setText("Saved. Press Guess again to see what the Ledger's rules "
                                "place now.")
        else:
            self.status.setText("Saved: nothing new to learn — the rules already read "
                                "every answer that way.")
        if self._on_learned is not None:
            try:
                self._on_learned(report)
            except Exception as exc:                        # noqa: BLE001
                print(f"[ledger_teach_dialog] on_learned raised: {exc!r}", flush=True)
        self._refresh()

    # ── closing ─────────────────────────────────────────────────────────
    def done(self, result: int) -> None:                    # noqa: D401 (Qt's name)
        """Closed while guessing: the guess is told to stop (a save runs to
        the end; the Ledger log says what it kept)."""
        if self.busy and self._cancellable:
            self._cancel.set()
        super().done(result)


__all__ = ["TeachDialog", "model_name", "from_text", "MAX_LISTED", "COLUMNS"]
