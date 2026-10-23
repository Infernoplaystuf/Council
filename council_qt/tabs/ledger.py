"""
council_qt.tabs.ledger — the 📑 Ledger tab: template + source workbooks -> review
workbook -> parent CSV, without the command line.

The four steps, top to bottom, are the pipeline's own order:

  1. Template -> create (or extend) the parent CSV, or point at an existing CSV.
  2. Source workbooks: files and folders.
  3. Extract -> a review workbook, opened in Excel for checking.
  4. Apply the saved review -> the parent CSV, and optionally one filled
     template per part.

Beside step 2, "Teach from a sample workbook…" (ledger_teach_dialog.py):
Council guesses what each thing in one workbook is, the user answers, and
the answers are kept so the next Extract places them by itself.

Create, Apply and picking a parent CSV also add it to what the Connections
tab's knowledge graph reads (only that file), so a rebuild there reads it too.

Every step runs on a worker thread (a workbook of many pictures takes a while) and the buttons are
disabled until it finishes; the summary line and the log say what it found.
Everything stays on this machine: the work is council_core/ledger_ingest.py,
and the pipeline itself sample_ledger.py and source_extract.py.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QCompleter,
                               QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QPlainTextEdit,
                               QProgressBar, QPushButton, QVBoxLayout, QWidget)


class _VisionReaderCombo(QComboBox):
    """A combobox that refreshes its installed-models list each time the
    user opens the popup, instead of at construction. The owning tab
    probes Ollama on every showPopup, so a test (which runs offline and
    counts even caught connection attempts as failures) never touches
    the network unless the user actually clicks the dropdown."""

    def __init__(self, probe, parent=None):
        super().__init__(parent)
        self._probe = probe

    def showPopup(self):
        try:
            self._probe()
        except Exception:                                 # noqa: BLE001
            pass
        super().showPopup()

from council_core import ledger_ingest
from sample_ledger import LedgerError

from .. import theme
from ..view import ViewHelpers, amp

XLSX = ("Workbooks, decks and documents (*.xlsx *.xlsm *.xls *.xlsb *.pptx *.pptm *.docx "
        "*.txt *.md *.pdf)")
TEMPLATE = "Excel workbook (*.xlsx)"


class LedgerActions:
    """The tab's seam to the pipeline and the desktop: tests replace pieces
    (a stand-in image reader, no Excel, no file dialogs)."""

    def __init__(self, image_reader=None, opener: Optional[Callable] = None,
                 rebuild_kg: Optional[Callable] = None, connections=None,
                 register_kg: Optional[Callable] = None):
        self.image_reader = image_reader
        self.opener = opener or ledger_ingest.open_path
        self._rebuild_kg_override = rebuild_kg
        #: The Connections side (ConnectionsActions) whose graph this tab
        #: adds its Ledger to, rebuilds and Teach ticks field rules in;
        #: None = the user's vault (Teach then uses the Connections tab's
        #: when it is built). Tests pass one on a temp vault.
        self.connections = connections
        self._register_kg_override = register_kg

    def create_parent(self, template, csv, mode: str = "auto"):
        return ledger_ingest.create_parent(template, csv or None, mode=mode)

    def teach_records(self, csv, workbooks, progress=None, cancel=None) -> str:
        """A table Ledger's Teach (council_core.ledger_records): learn the
        user's highlights in these sample workbooks and say how the next
        Extract reads them. Worker thread."""
        from council_core import ledger_records
        return ledger_records.teach(csv, workbooks, progress=progress, cancel=cancel)

    #: Set by the tab: where an extract's stall dump goes (its log).
    stall_log: Optional[Callable[[str], None]] = None

    def extract(self, csv, sources, part, use_images, progress,
                pattern_mask: Optional[str] = None,
                vision_model: Optional[str] = None,
                cancel=None, assist=False):
        reader = self.image_reader
        if reader is None and use_images and vision_model:
            # Explicit opt-in: the user picked a vision model in the dropdown.
            from source_extract import ollama_vision_reader
            reader = ollama_vision_reader(vision_model)
        return ledger_ingest.run_extract(csv, sources, part=part, use_images=use_images,
                                         image_reader=reader, progress=progress,
                                         pattern_mask=pattern_mask or None,
                                         cancel=cancel, log=self.stall_log,
                                         assist=assist)

    def diagnose(self, file, csv, use_images, on_line,
                 vision_model: Optional[str] = None, cancel=None, assist=False):
        """One file read the way Extract reads it, in a separate process,
        with a report to send back (council_core.ledger_diagnose)."""
        from council_core import ledger_diagnose
        return ledger_diagnose.run(file, csv, use_images=use_images,
                                   vision_model=vision_model, assist=assist,
                                   on_line=on_line,
                                   should_stop=cancel.is_set if cancel is not None else None)

    def apply(self, review, fill_dir, use_images, progress,
              vision_model: Optional[str] = None):
        # Apply reads the sources again: with the vision model the extract
        # used, or a picture's values change reader (review, 2026-10-19)
        reader = self.image_reader
        if reader is None and use_images and vision_model:
            from source_extract import ollama_vision_reader
            reader = ollama_vision_reader(vision_model)
        return ledger_ingest.run_apply(review, fill_dir or None, use_images=use_images,
                                       image_reader=reader, progress=progress)

    def teach_guess(self, csv, workbook, *, model_id: Optional[str] = None,
                    use_images: bool = True, vision_model: Optional[str] = None,
                    pattern_mask: Optional[str] = None, cancel=None, progress=None):
        """Council's guess for Teach (council_core.ledger_teach.guess): the
        workbook read exactly as Extract reads it, pictures through this
        tab's image reader; Council's model ('ollama:<name>' or a GGUF
        path, US-origin only) asked where the rules stop. Worker thread."""
        from council_core import ledger_teach
        reader = self.image_reader
        if reader is None and use_images and vision_model:
            from source_extract import ollama_vision_reader
            reader = ollama_vision_reader(vision_model)
        return ledger_teach.guess(csv, workbook, model_id=model_id or None,
                                  use_images=use_images, image_reader=reader,
                                  pattern_mask=pattern_mask or None, cancel=cancel,
                                  progress=progress)

    def teach_learn(self, csv, session, answers, people=None, *, part=None,
                    save_pattern: bool = False, connections=None, confirmed=None):
        """Keep the user's Teach answers (council_core.ledger_teach.learn) -
        of the rows they confirmed - and tick the knowledge graph's field
        rules they prove, in ``connections`` (else this one's, else the
        default vault's). Worker thread: it opens the graph."""
        from council_core import ledger_teach
        vault = connections or self.connections
        if vault is None:
            from council_qt.tabs.connections import ConnectionsActions
            vault = ConnectionsActions()
        return ledger_teach.learn(csv, session, answers, people, part=part,
                                  save_pattern=save_pattern, connections_vault=vault,
                                  confirmed=confirmed)

    def open(self, path) -> None:
        self.opener(path)

    def rebuild_kg(self, progress, csv: Optional[str] = None,
                   connections=None):
        """Rebuild the knowledge graph on the user's vault — same path the
        Connections tab uses. ``progress`` is the Ledger tab's
        ``(line, fraction)`` listener; the KG's own line-by-line stream is
        forwarded to it.

        ``csv`` is the tab's parent CSV. The graph reads only data_in and
        the folders added in Connections, and a parent CSV is made beside
        its template — so it never reached the graph (review, 2026-10-19).
        When the graph does not read it yet, that one file is added
        (kg.add_ledger: the CSV and its schema/aliases, not its folder) and
        ``stats['ledger_added']`` says so.

        Tests pass a ``rebuild_kg`` override to the LedgerActions
        constructor to avoid touching the real vault or Ollama, or a
        ``connections`` (ConnectionsActions on a temp vault)."""
        if self._rebuild_kg_override is not None:
            return self._rebuild_kg_override(progress)
        connections = connections or self._connections()
        added = None
        with connections.open_graph() as kg:
            if csv and Path(csv).is_file():
                from council_core import kg_excel
                if kg_excel.is_ledger_csv(Path(csv)):
                    added = kg.add_ledger(csv)
            stats = kg.seed(listener=progress)
        if added is not None:
            stats["ledger_added"] = str(csv)
        return stats

    def _connections(self):
        if self.connections is None:
            from council_qt.tabs.connections import ConnectionsActions
            return ConnectionsActions()
        return self.connections

    def register_ledger(self, csv) -> Optional[dict]:
        """Add this Ledger to what the knowledge graph reads (kg.add_ledger:
        the parent CSV only, not its folder), so a rebuild in the
        Connections tab reads it too. Only the Ledger tab's own Rebuild used
        to add it, and a tester rebuilding in Connections saw no links
        (user, 2026-10-20). Asked after Create and Apply, and for a parent
        CSV picked in the tab - on a worker of its own (LedgerTab._register).

        Never raises - a Ledger is made whatever the graph is doing:
        ``{'csv', 'added', 'busy', 'error'}``, ``added`` False when the
        graph read it already, ``error`` why it could not be added now
        (``busy``: the graph is being rebuilt; else its store could not be
        opened). None when there is nothing to add (no CSV, not a Ledger) or
        a test stood in for the graph (a ``rebuild_kg`` override and no
        ``register_kg``)."""
        if self._register_kg_override is not None:
            return self._register_kg_override(csv)
        if self._rebuild_kg_override is not None or not csv or not Path(csv).is_file():
            return None
        import sqlite3

        from council_core import kg_excel
        from council_core import knowledge_graph as kgm
        if not kg_excel.is_ledger_csv(Path(csv)):
            return None
        out = {"csv": str(csv), "added": False, "busy": False, "error": ""}
        try:
            with self._connections().open_graph() as kg:
                out["added"] = kg.add_ledger(csv) is not None
        except kgm.KnowledgeGraphBusy:
            out["busy"] = True
        except sqlite3.OperationalError as exc:
            out["busy"] = kgm._busy(exc)
            out["error"] = "" if out["busy"] else f"its store said {exc}"
        except Exception as exc:                          # noqa: BLE001
            out["error"] = str(exc) or exc.__class__.__name__
        if out["busy"]:
            out["error"] = "the graph is being rebuilt right now"
        return out

    def applied_csv(self, review) -> Optional[str]:
        """The parent CSV a review workbook was applied to (its hidden _run
        sheet says), or None."""
        from council_core import kg_excel
        try:
            meta = kg_excel._review_meta(Path(review)) if review else None
        except Exception:                                 # noqa: BLE001
            meta = None
        return str(meta["csv"]) if meta and meta.get("csv") else None


class LedgerTab(ViewHelpers, QWidget):
    #: How often a Ledger waiting for the Connections tab to finish with the
    #: graph (a rebuild, Suggest links) asks again, in ms.
    KG_RETRY_MS = 1500

    def __init__(self, window=None, actions: Optional[LedgerActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or LedgerActions()
        #: asks which downloaded model Council's questions go to (tests
        #: replace it)
        from .ledger_dialogs import pick_model
        self._pick_model = pick_model
        try:            # an extract's stall dump lands in this tab's log
            self.actions.stall_log = lambda text: self._to_ui(self._say, text)
        except Exception:                                 # noqa: BLE001
            pass
        self._tokens = theme.tokens("dark")
        self._worker: Optional[threading.Thread] = None
        self._busy_buttons: List[QPushButton] = []
        #: Set by the Cancel button; read inside the extract loop so the
        #: next workbook is skipped. Pipelines that already hit a stuck
        #: Workbooks.Open are released by excel_open.kill_active_excels,
        #: which the button also calls.
        self._cancel = threading.Event()
        #: True from an apply's completion until the next KG rebuild —
        #: used by _refresh_enabled to highlight the Rebuild button.
        self._kg_dirty = False
        #: Skip patterns typed while there was no parent CSV to save them
        #: beside: saved when one is set.
        self._ignore_unsaved = False
        #: (parent CSV, whether its schema is beside it): looked up when the
        #: path is set, not on every keystroke (a CSV on a network share can
        #: take seconds to answer). Teach needs the schema.
        self._schema_known = ("", False)
        #: (parent CSV, the part-name patterns Teach saved beside it), read
        #: when the path is set (_load_saved_patterns): 'Forget saved
        #: pattern' is on when there are some
        self._saved_patterns = ("", [])
        #: The open Teach dialog, and how its Browse… picks one workbook
        #: (tests replace it: no file dialogs).
        self.teach_dialog = None
        self.link_dialog = None
        self._pick_workbook = self._browse_one_workbook
        #: (CSV, line) the log said about the Connections graph reading a
        #: Ledger: each said once.
        self._kg_said: set = set()
        #: Adding a Ledger to the Connections graph (_register): the CSVs
        #: whose worker is running, the ones added (or read already) this
        #: session, and the ones waiting for the Connections tab to finish.
        self._kg_running: set = set()
        self._kg_known: set = set()
        self._kg_pending: List[str] = []
        self._build()
        self._kg_timer = QTimer(self)
        self._kg_timer.setSingleShot(True)
        self._kg_timer.setInterval(self.KG_RETRY_MS)
        self._kg_timer.timeout.connect(self._register_pending)
        self._refresh_reader_choices()
        self._refresh_enabled()

    # ── layout ──────────────────────────────────────────────────────────
    def _step_button(self, layout, text: str, slot) -> QPushButton:
        """ViewHelpers' button, locked while a step runs."""
        b = self._button(layout, text, slot)
        self._busy_buttons.append(b)
        return b

    def _path_row(self, grid: QGridLayout, row: int, label: str, browse) -> QLineEdit:
        grid.addWidget(QLabel(amp(label)), row, 0)
        edit = QLineEdit()
        edit.textChanged.connect(self._refresh_enabled)
        grid.addWidget(edit, row, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(browse)
        grid.addWidget(b, row, 2)
        return edit

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        g1 = QGroupBox("1 · Template and parent CSV")
        grid = QGridLayout(g1)
        self.template = self._path_row(grid, 0, "Template workbook:", self.browse_template)
        self.csv = self._path_row(grid, 1, "Parent CSV:", self.browse_csv)
        self.csv.setPlaceholderText("made beside the template if left empty; "
                                    "or pick an existing parent CSV")
        self.csv.editingFinished.connect(self._on_csv_changed)
        row = QHBoxLayout()
        self.create_btn = self._step_button(row, "Create / extend parent CSV", self.on_create)
        # a table template whose rows are records (council_core.ledger_records):
        # found by itself when the template has no sample-ID column
        self.records_mode = QCheckBox("Each table row is a record")
        self.records_mode.setToolTip(
            "Tick when the template is a table to fill with many records (one row each), "
            "read from the tables of every workbook. Left unticked, Council decides: a "
            "table with no sample-ID column is read this way too.")
        row.addWidget(self.records_mode)
        row.addStretch(1)
        grid.addLayout(row, 2, 1)
        outer.addWidget(g1)

        g2 = QGroupBox("2 · Source workbooks")
        v2 = QVBoxLayout(g2)
        self.sources = QListWidget()
        # several at once: a table Ledger's Teach reads a sample with its
        # lookup workbook; Remove selected removes them all
        self.sources.setSelectionMode(QListWidget.ExtendedSelection)
        self.sources.setMaximumHeight(120)
        v2.addWidget(self.sources)
        row = QHBoxLayout()
        self._step_button(row, "Add files…", self.browse_sources)
        self._step_button(row, "Add folder…", self.browse_folder)
        self._step_button(row, "Remove selected", self.remove_selected)
        self._step_button(row, "Clear", self.clear_sources)
        # one workbook at a time, on a row of their own: beside the source
        # buttons the row grew wider than a laptop's screen
        one_row = QHBoxLayout()
        preview = self._step_button(one_row, "Preview a workbook…",
                                    self.on_preview_workbook)
        preview.setToolTip(
            "Dry-run against one source workbook so you can see which "
            "columns the matcher recognizes, which inspectors it finds, "
            "and which labels it can't match. Fix the labels or add a "
            "schema alias BEFORE running a long batch.")
        self.teach_btn = self._step_button(one_row, "Teach from a sample workbook…",
                                           self.on_teach_workbook)
        self.teach_btn.setToolTip(
            "Pick one workbook: Council guesses what each label, picture, part number "
            "and inspector's initials in it are, you correct the guesses, and the Ledger "
            "keeps your answers - the next Extract places them by itself, and Council's "
            "model learns from them. Needs a parent CSV with its schema (step 1).")
        self.link_btn = self._step_button(one_row, "Link by clicking…", self.on_link_window)
        self.link_btn.setToolTip(
            "The template beside a sample workbook or deck: click where a value goes, then "
            "where it comes from. For a deck, also which table each text box goes with. "
            "Save teaches the next Extract - no colours to paint.")
        one_row.addStretch(1)
        row.addWidget(QLabel("Part number (if not in them):"))
        self.part = QLineEdit()
        self.part.setFixedWidth(120)
        row.addWidget(self.part)
        self.use_images = QCheckBox("Read pasted images with")
        self.use_images.setChecked(True)
        self.use_images.toggled.connect(self._refresh_reader_enabled)
        row.addWidget(self.use_images)
        self.image_reader_choice = _VisionReaderCombo(
            probe=lambda: self._refresh_reader_choices(from_popup=True))
        self.image_reader_choice.setToolTip(
            "Which reader runs on each pasted image. Tesseract is a local "
            "OCR that reads clean text fast. Ollama vision models catch "
            "handwriting and messy tables Tesseract misses, slower.")
        row.addWidget(self.image_reader_choice)
        self._install_vision_sentinel = "__install_vision__"
        self._tesseract_sentinel = "__tesseract__"
        self.image_reader_choice.currentIndexChanged.connect(
            self._on_reader_changed)
        row.addStretch(1)
        v2.addLayout(row)
        v2.addLayout(one_row)
        # Pattern hint — what is typed is held for this session only; the
        # patterns Teach saved beside the parent CSV are offered, and the
        # newest is used, as a hint, when the field is left empty.
        mask_row = QHBoxLayout()
        mask_row.addWidget(QLabel("Part name pattern (optional):"))
        self.pattern_mask = QLineEdit()
        self.pattern_mask.setPlaceholderText(
            "@@@#@@###   — @ letter, # digit, = exact, | several")
        self.pattern_mask.setToolTip(
            "A shape used when the sample number is not labelled.\n"
            "@ matches any letter, # any digit, anything else matches itself.\n"
            "Example: @@@#@@### finds ABC1DE234 in a filename or a title - and,\n"
            "as it is loose, AB1DE234, ABC1DE23 and ABC1DE234-A or -2 too (each run\n"
            "one shorter or longer, and a short tail after a dash).\n"
            "Start with = to take it exactly as typed: =@@@#@@###.\n"
            "Several shapes: separate them with | or a comma (@@@#@@### | @@-####).\n"
            "A part of another shape is kept but never pre-accepted.\n"
            "Left empty, the pattern Teach saved for this Ledger is used as a hint\n"
            "(parts of another shape are still pre-accepted); Forget saved pattern drops it.")
        # the patterns Teach saved for this Ledger, offered as you type
        # (_load_saved_patterns)
        self._pattern_completer = QCompleter([], self.pattern_mask)
        self._pattern_completer.setCompletionMode(
            QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.pattern_mask.setCompleter(self._pattern_completer)
        mask_row.addWidget(self.pattern_mask, 1)
        # a pattern Teach saved could not be taken back, and it applied to
        # every later Extract, Preview and Teach (review, 2026-10-20)
        self.forget_pattern_btn = QPushButton("Forget saved pattern")
        self.forget_pattern_btn.setToolTip(
            "Stop using the part-name pattern Teach saved for this Ledger: with the field "
            "empty, Extract, Preview and Teach then use no pattern.")
        self.forget_pattern_btn.clicked.connect(self.on_forget_patterns)
        self.forget_pattern_btn.setEnabled(False)
        mask_row.addWidget(self.forget_pattern_btn)
        # Label matching alone is never enough on real workbooks (user,
        # 2026-10-19): Council's model is asked about what the rules left.
        self.ask_model = QCheckBox("Ask Council's model about the rest")
        self.ask_model.setToolTip(
            "After the label rules: Council's own model (your GGUF, or a US-origin "
            "Ollama model on this PC / your Pi nodes) is asked\n"
            " • which template field each unplaced label means (only labels are sent), and\n"
            " • which part a workbook is about when no part number was found (its short\n"
            "   text cells are sent; the pattern above is given as an example).\n"
            "Nothing leaves your machines. Every answer lands in the review in amber for\n"
            "you to accept; a part number not written in the workbook is dropped.\n"
            "Accepting a suggested field teaches it for the next run.")
        mask_row.addWidget(self.ask_model)
        v2.addLayout(mask_row)
        # Filename-ignore patterns — persisted next to the aliases file so a
        # user's "cover*.xlsx, agenda*.xlsx" set survives across runs.
        ignore_row = QHBoxLayout()
        ignore_row.addWidget(QLabel("Skip these files (patterns, comma-sep):"))
        self.ignore_files = QLineEdit()
        self.ignore_files.setPlaceholderText(
            "cover*.xlsx, agenda*.xlsx, meeting*.xlsx")
        self.ignore_files.setToolTip(
            "Workbook filenames (not full paths) matching ANY of these "
            "glob patterns are skipped before Excel is opened. "
            "fnmatch semantics, case-insensitive. Saved next to the "
            "parent CSV's aliases file.")
        self.ignore_files.editingFinished.connect(self._save_ignore_patterns)
        self.ignore_files.textEdited.connect(self._on_ignore_edited)
        ignore_row.addWidget(self.ignore_files, 1)
        v2.addLayout(ignore_row)
        outer.addWidget(g2)

        g3 = QGroupBox("3 · Extract and review")
        grid3 = QGridLayout(g3)
        self.review = self._path_row(grid3, 0, "Review workbook:", self.browse_review)
        row = QHBoxLayout()
        self.extract_btn = self._step_button(row, "Extract → review workbook", self.on_extract)
        # each value beside the template and source cells it links, highlighted
        # (ledger_review_dialog): the review workbook, easier to read
        self.review_btn = self._step_button(row, "Review by clicking…", self.on_review_window)
        self.review_btn.setToolTip(
            "Check the review one item at a time: the template cell the value goes in "
            "and the source cell it was read from, both highlighted. Right / Wrong / a "
            "corrected value are saved into the review workbook; then Apply.")
        self.open_btn = self._step_button(row, "Open review in Excel", self.on_open_review)
        # files skipped as too slow, or that stalled the app before
        # (council_core.file_guard): read again on the next run
        from ..widgets.skipped_files import RetrySkippedButton
        self.retry_skipped_btn = RetrySkippedButton(self._say)
        row.addWidget(self.retry_skipped_btn)
        # one file that stalls or breaks Extract, read in a separate process
        # with a report to send back (council_core.ledger_diagnose)
        self.diagnose_btn = self._step_button(row, "Diagnose a file…", self.on_diagnose)
        self.diagnose_btn.setToolTip(
            "Read ONE file the way Extract does, in a separate process, so a file "
            "that stalls or crashes cannot take the app down. Writes a report of "
            "what is inside the file, how long each step took, where it got "
            "stuck, and how it ended - send that report back. Changes nothing "
            "in your Ledger or the skip list.")
        row.addStretch(1)
        grid3.addLayout(row, 1, 1)
        outer.addWidget(g3)

        g4 = QGroupBox("4 · Apply the saved review")
        grid4 = QGridLayout(g4)
        self.fill_dir = self._path_row(grid4, 0, "Filled templates folder (optional):",
                                       self.browse_fill_dir)
        row = QHBoxLayout()
        self.apply_btn = self._step_button(row, "Apply review → parent CSV", self.on_apply)
        self.rebuild_kg_btn = self._step_button(
            row, "Rebuild knowledge graph", self.on_rebuild_kg)
        self.rebuild_kg_btn.setToolTip(
            "Rebuild the Connections knowledge graph, so a newly-applied "
            "Ledger shows up as parts, people and inspections. The parent "
            "CSV above is added to what the graph reads if it is not read "
            "yet (only that file). Same rebuild the Connections tab runs.")
        row.addStretch(1)
        grid4.addLayout(row, 1, 1)
        outer.addWidget(g4)

        g5 = QGroupBox("Summary")
        v5 = QVBoxLayout(g5)
        nums = QHBoxLayout()
        self.stat = {}
        for key, title in [("values", "Values found"), ("pre_accepted", "Pre-accepted"),
                           ("to_review", "To review"), ("images", "Images read"),
                           ("problems", "Problems")]:
            box = QVBoxLayout()
            n = QLabel("–")
            n.setStyleSheet("font-size: 18px; font-weight: bold;")
            n.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t = QLabel(title)
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.addWidget(n)
            box.addWidget(t)
            nums.addLayout(box)
            self.stat[key] = n
        v5.addLayout(nums)
        self.status = QLabel("Pick a template, or an existing parent CSV.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        v5.addWidget(self.status)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)               # busy, no percentage
        self.progress.setVisible(False)
        progress_row.addWidget(self.progress, 1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip(
            "Stop the running step after the current workbook. Also kills "
            "any hidden Excel process this run spawned, which unblocks a "
            "Workbooks.Open stuck on a modal dialog (sign-in, license, "
            "Trust Center Enable Editing).")
        self.cancel_btn.clicked.connect(self.on_cancel)
        self.cancel_btn.setEnabled(False)
        progress_row.addWidget(self.cancel_btn)
        v5.addLayout(progress_row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        v5.addWidget(self.log, 1)
        outer.addWidget(g5, 1)

    # ── state ───────────────────────────────────────────────────────────
    @property
    def busy(self) -> bool:
        return self._worker is not None

    def source_paths(self) -> List[str]:
        return [self.sources.item(i).text() for i in range(self.sources.count())]

    def _on_csv_changed(self) -> None:
        """Re-sync tab state that depends on the parent CSV path:
        ignore-pattern field is populated from the aliases file so the
        user sees their saved patterns as soon as they pick the CSV; the
        part-name patterns Teach saved are offered; Teach is enabled when
        the CSV has its schema.

        A parent CSV picked here is also added to what the Connections graph
        reads (_register): a Ledger made with an earlier build was added only
        at its next Apply, and the tester's rebuild in Connections found no
        Ledger (user, 2026-10-20)."""
        self._load_ignore_patterns()
        self._check_schema()
        self._load_saved_patterns()
        self._register(self.csv.text().strip())

    def _check_schema(self) -> None:
        """Whether the parent CSV and its schema are there (Teach needs
        both); kept for _refresh_enabled."""
        csv = self.csv.text().strip()
        ok = False
        if csv:
            try:
                from sample_ledger import schema_path_for
                ok = Path(csv).is_file() and schema_path_for(csv).is_file()
            except (OSError, ValueError):
                ok = False
        self._schema_known = (csv, ok)
        self._refresh_enabled()

    def _load_saved_patterns(self) -> None:
        """Offer the part-name patterns Teach saved for this Ledger in the
        pattern field (newest first), and say in its placeholder that the
        newest is used when the field is left empty - Extract and Teach do
        (ledger_ingest, ledger_teach)."""
        if not hasattr(self, "pattern_mask"):
            return
        csv = self.csv.text().strip()
        try:
            from source_extract import load_patterns
            saved = load_patterns(csv) if csv else []
        except Exception:                                    # noqa: BLE001
            saved = []
        self._pattern_completer.model().setStringList(saved)
        self._saved_patterns = (csv, list(saved))
        hint = "@ letter, # digit, = exact, | several"
        self.pattern_mask.setPlaceholderText(
            f"empty: this Ledger's saved pattern {saved[0]} is used as a hint   — {hint}"
            if saved else f"@@@#@@###   — {hint}")
        self._refresh_enabled()

    def on_forget_patterns(self) -> None:
        """Forget the part-name pattern(s) Teach saved for this Ledger
        (source_extract.forget_patterns); the rest of the aliases file is
        kept. A small file beside the parent CSV, written here as the Skip
        patterns are."""
        csv = self.csv.text().strip()
        if not csv or self.busy:
            return
        try:
            from source_extract import forget_patterns
            gone = forget_patterns(csv)
        except Exception as exc:                             # noqa: BLE001
            self.status.setText(f"Could not forget the saved pattern: {exc}")
            return
        self._load_saved_patterns()
        if gone:
            self._say("Forgot this Ledger's saved part-name pattern(s): " + ", ".join(gone)
                      + " — Extract, Preview and Teach use only a pattern you type now.")
            self.status.setText("Saved part-name pattern forgotten.")

    def _refresh_enabled(self) -> None:
        if not hasattr(self, "progress"):
            return                                  # still building
        idle = not self.busy
        for b in self._busy_buttons:
            b.setEnabled(idle)
        if hasattr(self, "cancel_btn"):
            # Only enabled while a step that honours it is running.
            self.cancel_btn.setEnabled(not idle and getattr(self, "_cancellable", False))
        csv = self.csv.text().strip()
        if hasattr(self, "forget_pattern_btn"):
            # this Ledger has a saved pattern (_load_saved_patterns)
            had, saved = self._saved_patterns
            self.forget_pattern_btn.setEnabled(idle and bool(csv) and had == csv
                                               and bool(saved))
        if not idle:
            return
        self.create_btn.setEnabled(bool(self.template.text().strip()))
        self.extract_btn.setEnabled(bool(csv) and bool(self.source_paths()))
        # off only for a path already looked up and found without its schema
        # (_check_schema). A path typed or pasted is looked up when Teach is
        # pressed: the button stayed off until Enter or Tab, and clicking it
        # did not move the focus (review, 2026-10-20)
        self.teach_btn.setEnabled(bool(csv) and self._schema_known != (csv, False))
        if hasattr(self, "link_btn"):
            self.link_btn.setEnabled(bool(csv) and self._schema_known != (csv, False))
        has_review = bool(self.review.text().strip())
        self.open_btn.setEnabled(has_review)
        if hasattr(self, "review_btn"):
            self.review_btn.setEnabled(has_review)
        self.apply_btn.setEnabled(has_review)
        if hasattr(self, "rebuild_kg_btn"):
            # Always clickable while idle; dirtiness only affects weight.
            font = self.rebuild_kg_btn.font()
            font.setBold(self._kg_dirty)
            self.rebuild_kg_btn.setFont(font)

    # ── image-reader dropdown ───────────────────────────────────────────
    # ── filename-ignore patterns (persisted next to aliases) ──────────
    def _typed_ignore_patterns(self) -> List[str]:
        text = self.ignore_files.text().strip()
        return [p.strip() for p in text.split(",") if p.strip()]

    def _on_ignore_edited(self, _text: str = "") -> None:
        """Typed by the user (not loaded) with no parent CSV to save them
        beside: held for the CSV that is set next, even before the field
        loses focus."""
        if not self.csv.text().strip():
            self._ignore_unsaved = bool(self._typed_ignore_patterns())

    def _save_ignore_patterns(self) -> None:
        """Persist the comma-separated patterns from the field. With no
        parent CSV yet they are held in the field and saved when one is
        set (_load_ignore_patterns) — the file lives next to it."""
        csv_path = self.csv.text().strip()
        patterns = self._typed_ignore_patterns()
        if not csv_path:
            self._ignore_unsaved = bool(patterns)
            return
        try:
            from source_extract import save_ignore_files
            save_ignore_files(csv_path, patterns)
            self._ignore_unsaved = False
        except Exception as exc:                             # noqa: BLE001
            self.status.setText(
                f"Could not save ignore patterns to the aliases file: {exc}")

    def _load_ignore_patterns(self) -> None:
        """Populate the field from the aliases file. Called when the
        parent CSV path changes (typed, Browse…, Create).

        Patterns typed before there was a CSV are saved beside it, not
        cleared: the field was emptied, or kept showing them while the
        extract read every file, because nothing had saved them (review,
        2026-10-19). Typed patterns are never dropped: the CSV's own are
        kept with them. Only TYPED ones (_ignore_unsaved): the field also
        shows the patterns loaded from the last CSV, and switching from a
        ledger that has some to one that has none wrote the first one's
        into the second's aliases file (review, 2026-10-19)."""
        if not hasattr(self, "ignore_files"):
            return
        csv_path = self.csv.text().strip()
        typed = self._typed_ignore_patterns()
        if not csv_path:
            if not getattr(self, "_ignore_unsaved", False):
                self.ignore_files.setText("")
            return
        try:
            from source_extract import load_ignore_files
            patterns = load_ignore_files(csv_path)
        except Exception:                                    # noqa: BLE001
            patterns = []
        if typed and getattr(self, "_ignore_unsaved", False):
            merged = patterns + [p for p in typed if p not in patterns]
            try:
                from source_extract import save_ignore_files
                if merged != patterns:
                    save_ignore_files(csv_path, merged)
                    self._say(f"Skip patterns saved beside {Path(csv_path).name}: "
                              + ", ".join(merged))
                self._ignore_unsaved = False
                patterns = merged
            except Exception as exc:                         # noqa: BLE001
                self.status.setText(f"Could not save the Skip patterns beside "
                                    f"{Path(csv_path).name}: {exc}")
                return                                       # the field keeps them
        self.ignore_files.blockSignals(True)
        try:
            self.ignore_files.setText(", ".join(patterns))
        finally:
            self.ignore_files.blockSignals(False)

    def _refresh_reader_choices(self, *, select: Optional[str] = None,
                                from_popup: bool = False) -> None:
        """Populate the reader dropdown with Tesseract + installed US vision
        models + an 'Install…' entry.

        The Ollama probe runs only when ``from_popup`` is true (the user
        opened the dropdown) or ``select`` is set (an install just
        finished). Construction stays offline so tests do not trip the
        offline guard just by building the tab."""
        if not hasattr(self, "image_reader_choice"):
            return
        self.image_reader_choice.blockSignals(True)
        current_data = self.image_reader_choice.currentData() if select is None else select
        try:
            self.image_reader_choice.clear()
            self.image_reader_choice.addItem("Tesseract (local, fast)",
                                             self._tesseract_sentinel)
            if from_popup or select is not None:
                try:
                    from council_core import ollama_install
                    for n in ollama_install.vision_models():   # ones that see images
                        self.image_reader_choice.addItem(f"Ollama: {n}", n)
                except Exception:                             # noqa: BLE001
                    pass
            self.image_reader_choice.insertSeparator(
                self.image_reader_choice.count())
            self.image_reader_choice.addItem("Install vision model…",
                                             self._install_vision_sentinel)
            if current_data is not None:
                idx = self.image_reader_choice.findData(current_data)
                if idx >= 0:
                    self.image_reader_choice.setCurrentIndex(idx)
        finally:
            self.image_reader_choice.blockSignals(False)
        self._refresh_reader_enabled()

    def _refresh_reader_enabled(self, *_args) -> None:
        if hasattr(self, "image_reader_choice") and hasattr(self, "use_images"):
            self.image_reader_choice.setEnabled(self.use_images.isChecked())

    def _on_reader_changed(self, _index: int) -> None:
        if not hasattr(self, "image_reader_choice"):
            return
        data = self.image_reader_choice.currentData()
        if data == self._install_vision_sentinel:
            # The 'Install…' sentinel: open the dialog, then put the
            # dropdown back on whichever model is actually in use.
            self._open_install_dialog()
            self._refresh_reader_choices(select=self._tesseract_sentinel)

    def _open_install_dialog(self) -> None:
        from .ledger_dialogs import InstallVisionModelDialog
        dlg = InstallVisionModelDialog(
            on_installed=lambda name: self._refresh_reader_choices(select=name),
            parent=self)
        dlg.exec()

    def _selected_vision_model(self) -> Optional[str]:
        if not hasattr(self, "image_reader_choice"):
            return None
        data = self.image_reader_choice.currentData()
        if data in (None, self._tesseract_sentinel, self._install_vision_sentinel):
            return None
        return str(data)

    def _say(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _run(self, title: str, step: Callable, done: Callable,
             after: Optional[Callable] = None, cancellable: bool = False) -> None:
        """Run `step(progress)` on a thread; `done(result)` on the GUI thread.
        A LedgerError is the pipeline's own plain-language message; anything
        else is shown with its traceback in the log. ``after(err)`` runs on
        the GUI thread once the step ended either way (err None on success).

        The ``self._cancel`` event is cleared at the start of every run; a
        ``cancellable`` step reads it (via a ``cancel=`` kwarg) to exit
        early. Only such a step enables Cancel: during Apply it said
        'Cancelling…' and the parent CSV was written anyway (review,
        2026-10-19)."""
        if self.busy:
            return
        self._cancel.clear()
        self._cancellable = cancellable
        self.status.setText(f"{title}…")
        self._say(f"── {title}")
        self.progress.setRange(0, 0)               # busy until a percentage is known
        self.progress.setVisible(True)

        def progress(msg: str, fraction: Optional[float] = None) -> None:
            """Every progress line (the terminal shows them too); a known
            fraction turns the busy bar into a percentage."""
            def show() -> None:
                self.status.setText(f"{title}: {msg}")
                if fraction is None or fraction <= 0:
                    # a big first workbook sat on a still 0% bar for minutes
                    # and looked hung: busy until something is done
                    self.progress.setRange(0, 0)
                else:
                    self.progress.setRange(0, 100)
                    self.progress.setValue(int(100 * fraction))
            self._to_ui(show)

        def work() -> None:
            try:
                result, err = step(progress), None
            except LedgerError as e:
                result, err = None, f"error: {e}"
            except Exception as e:                        # noqa: BLE001
                result, err = None, f"failed: {e!r}\n{traceback.format_exc()}"
            self._to_ui(lambda: self._finish(title, result, err, done, after))

        self._worker = threading.Thread(target=work, name="ledger-step", daemon=True)
        self._refresh_enabled()
        self._worker.start()

    def on_preview_workbook(self) -> None:
        """File-pick a single source workbook and show what the Ledger
        extractor would make of it — no values extracted, no review
        written. Needs a parent CSV with a schema beside it."""
        csv_path = self.csv.text().strip()
        if not csv_path:
            self.status.setText(
                "Set the parent CSV path first — the preview needs its schema.")
            return
        try:
            from sample_ledger import ParentCSV
            from source_extract import load_aliases
            ledger = ParentCSV(csv_path)
        except Exception as exc:                            # noqa: BLE001
            self.status.setText(f"Could not open {csv_path}: {exc}")
            return
        if ledger.schema is None:
            self.status.setText(
                "That parent CSV has no schema beside it — run Create first.")
            return
        p, _ = QFileDialog.getOpenFileName(
            self, "Pick ONE source workbook to preview",
            self._start_dir(), XLSX)
        if not p:
            return
        aliases, ignore = load_aliases(csv_path)
        from .ledger_dialogs import PreviewWorkbookDialog
        mask = self.pattern_mask.text().strip()
        dlg = PreviewWorkbookDialog(p, ledger.schema, aliases=aliases,
                                    pattern_mask=mask or None,
                                    csv_path=csv_path, parent=self,
                                    pick_model=self._pick_model, ignore=ignore,
                                    part=self.part.text().strip())
        dlg.exec()

    def on_teach_workbook(self) -> None:
        """Teach from a sample workbook (user, 2026-10-20: "taking a sample
        workbook from the folder and I line up where they match the
        templates ... First I want an LLM guess to be made, then I give an
        answer"): the dialog reads ONE workbook as Extract does with this
        tab's image reader and pattern, Council guesses what each thing in
        it is, the user answers, and Save keeps the answers for the next
        Extract and the knowledge graph. Needs a parent CSV with a schema.

        Window-modal and not waited for: the guess runs on the dialog's own
        worker, and no Extract or Apply can write the aliases file while
        the answers are saved."""
        csv_path = self.csv.text().strip()
        if not csv_path:
            self.status.setText("Set the parent CSV first — Teach needs its schema.")
            return
        self._check_schema()
        if not self._schema_known[1]:
            self.status.setText(
                "That parent CSV has no schema beside it — run Create first.")
            return
        from council_core import ledger_records
        if ledger_records.is_record_ledger(csv_path):
            self._teach_records(csv_path)
            return
        from .ledger_teach_dialog import TeachDialog
        images, vision = self.use_images.isChecked(), self._selected_vision_model()
        mask = self.pattern_mask.text().strip()
        actions = self.actions
        conn = self._connections_tab()
        vault = getattr(actions, "connections", None) or (
            conn.actions if conn is not None else None)
        old = self.teach_dialog
        if old is not None:
            try:
                if not old.busy:
                    old.deleteLater()
            except RuntimeError:                          # already gone
                pass
        dlg = TeachDialog(
            csv_path, sources=self.source_paths(),
            prefer=[i.text() for i in self.sources.selectedItems()],
            ignore_patterns=self._typed_ignore_patterns(),
            template_path=self.template.text().strip() or None,
            guess=lambda csv, wb, **kw: actions.teach_guess(
                csv, wb, use_images=images, vision_model=vision, pattern_mask=mask, **kw),
            learn=lambda csv, s, answers, people, **kw: actions.teach_learn(
                csv, s, answers, people, connections=vault, **kw),
            pick_model=self._pick_model, browse=self._pick_workbook,
            log=self._say, on_learned=self._after_teach, pattern_mask=mask, parent=self)
        self.teach_dialog = dlg
        self._say(f"── Teach from a sample workbook ({Path(csv_path).name})")
        dlg.open()

    def on_link_window(self) -> None:
        """The click-to-link window (ledger_link_dialog): the sources listed
        here are its samples, the selected one first. Not modal: Extract
        can run while it is open; its Save writes only the Ledger's own
        files."""
        csv_path = self.csv.text().strip()
        if not csv_path:
            self.status.setText("Set the parent CSV first — linking needs its schema.")
            return
        self._check_schema()
        if not self._schema_known[1]:
            self.status.setText("That parent CSV has no schema beside it — run Create first.")
            return
        from council_core.ledger_ingest import collect_sources
        from .ledger_link_dialog import LinkDialog
        picked = [i.text() for i in self.sources.selectedItems()]
        try:
            files, _ = collect_sources(picked + [p for p in self.source_paths()
                                                 if p not in picked],
                                       template_path=self.template.text().strip() or None)
        except Exception:                                 # noqa: BLE001
            files = []
        dlg = LinkDialog(csv_path, [str(f) for f in files[:500]], parent=self, log=self._say,
                         on_saved=lambda _said: self.status.setText(
                             "Links saved; the next Extract uses them."),
                         browse=self._pick_workbook)
        self.link_dialog = dlg
        self._say(f"── Link by clicking ({Path(csv_path).name})")
        dlg.show()

    def _teach_records(self, csv_path: str) -> None:
        """A table Ledger learns from the user's highlights: the sources
        selected in the list (a sample and, say, its lookup workbook), else
        one workbook picked now. It says what the next Extract will read."""
        picked = [i.text() for i in self.sources.selectedItems()]
        if not picked:
            one = self._pick_workbook(self)
            if not one:
                return
            picked = [one]
        teach = getattr(self.actions, "teach_records", None)
        if teach is None:
            self.status.setText("Teach is not available for a table Ledger here.")
            return
        self._say(f"── Teach from {len(picked)} sample workbook(s) ({Path(csv_path).name})")

        def done(text) -> None:
            self._say(text)
            self.status.setText("Teach read your highlights; the next Extract uses them. "
                                "The log says what it will read.")
        self._run("Reading your highlights",
                  lambda p: teach(csv_path, picked, progress=p, cancel=self._cancel), done,
                  cancellable=True)

    def _after_teach(self, report) -> None:
        """Teach saved its answers: the pattern field offers a pattern it
        saved, and the knowledge graph wants a rebuild when the Ledger's
        inspectors or people changed (kg_excel reads them)."""
        self._load_saved_patterns()
        if report.who_labels or report.people:
            self._kg_dirty = True
            self.status.setText("Teach saved your answers. Press Rebuild knowledge graph "
                                "so the graph reads the inspectors the new way.")
        else:
            self.status.setText("Teach saved your answers; the next Extract uses them.")
        self._refresh_enabled()

    def _browse_one_workbook(self, parent=None) -> str:
        """Teach's Browse…: one workbook, '' when cancelled."""
        p, _ = QFileDialog.getOpenFileName(parent or self, "Pick ONE sample workbook",
                                           self._start_dir(), XLSX)
        return p

    def on_cancel(self) -> None:
        """Signal the running step to stop. The event is checked between
        workbooks in extract(). If a Workbooks.Open is already blocking
        on a modal Excel dialog, taskkill'ing the hidden Excel process
        unblocks it — the COM call raises and the loop exits on its next
        iteration."""
        if not self.busy or not getattr(self, "_cancellable", False):
            return
        self._cancel.set()
        try:
            from excel_open import kill_active_excels
            killed = kill_active_excels()
        except Exception:                                 # noqa: BLE001
            killed = 0
        tail = f" (+{killed} Excel process{'es' if killed != 1 else ''} ended)" \
            if killed else ""
        self.status.setText(f"Cancelling…{tail}")
        self._say(f"── Cancel requested{tail}")

    def _finish(self, title, result, err, done, after=None) -> None:
        self._worker = None
        self.progress.setVisible(False)
        self.retry_skipped_btn.refresh()
        try:
            if err:
                self.status.setText(err.splitlines()[0])
                self._say(err)
            else:
                done(result)
        finally:
            if after is not None:
                after(err)
            self._refresh_enabled()

    def wait(self, timeout: float = 60.0) -> bool:
        """Block until the current step's result is shown, and the Ledger it
        made or applied is added to the Connections graph - or said why not
        (tests, scripts). A Ledger waiting for the Connections tab is not
        waited for."""
        import time
        end = time.monotonic() + timeout
        while (self.busy or self._kg_running) and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.01)
        QApplication.processEvents()
        return not (self.busy or self._kg_running)

    # ── step 1 ──────────────────────────────────────────────────────────
    def on_create(self) -> None:
        template, csv = self.template.text().strip(), self.csv.text().strip()
        # only when ticked, so an actions stand-in without the option works
        more = {"mode": "records"} if self.records_mode.isChecked() else {}

        def done(s) -> None:
            self.csv.setText(s.csv)
            self.status.setText(s.describe().splitlines()[0])
            self._say(s.describe())
            self._register(s.csv, again=True)
            self._on_csv_changed()          # setText is not editingFinished
        self._run("Reading the template",
                  lambda _p: self.actions.create_parent(template, csv, **more), done)

    # ── the Connections graph reads this Ledger (user, 2026-10-20) ──────
    def _register(self, csv, again: bool = False) -> None:
        """Add this Ledger to what the Connections graph reads
        (LedgerActions.register_ledger), on its own worker, once the step's
        result is shown: inside the step a busy store held 'Parent CSV
        created' back 6-7 s (review, 2026-10-20). While the Connections tab
        has the graph (a rebuild, Suggest links) it waits for that to finish
        and asks again every KG_RETRY_MS.

        ``again``: Create and Apply ask even when this session added it
        before (Folders… may have removed it since); a CSV merely picked is
        asked once. An actions stand-in without register_ledger adds
        nothing."""
        csv = str(csv or "").strip()
        register = getattr(self.actions, "register_ledger", None)
        if not csv or register is None or csv in self._kg_running:
            return
        if csv in self._kg_known and not again:
            return
        conn = self._connections_tab()
        if conn is not None and conn.graph_in_use():
            if csv not in self._kg_pending:
                self._kg_pending.append(csv)
            self._say_once(csv, "The Connections tab is using the graph right now (a "
                                "rebuild, or Suggest links): this Ledger is added to what it "
                                "reads once that finishes.")
            self._kg_timer.start()
            return
        self._kg_running.add(csv)

        def work() -> None:
            try:
                result = register(csv)
            except Exception as exc:                      # noqa: BLE001
                result = {"csv": csv, "added": False, "busy": False,
                          "error": str(exc) or exc.__class__.__name__}
            self._to_ui(self._registered, csv, result)

        threading.Thread(target=work, name="ledger-kg-add", daemon=True).start()

    def _register_pending(self) -> None:
        """The timer: the Ledgers waiting for the Connections tab, asked
        again (still busy: they wait on)."""
        pending, self._kg_pending = self._kg_pending, []
        for csv in pending:
            self._register(csv, again=True)

    def _registered(self, csv: str, registered: Optional[dict]) -> None:
        self._kg_running.discard(csv)
        if registered and not registered.get("busy") and not registered.get("error"):
            self._kg_known.add(csv)
        self._say_registered(registered)

    def _say_registered(self, registered: Optional[dict]) -> None:
        """Say once, in the log, that Connections now reads this Ledger -
        or why it does not yet."""
        if not registered:
            return
        csv = registered.get("csv", "")
        if registered.get("busy"):
            text = (f"Not added to the Connections graph yet ({registered['error']}): "
                    "Rebuild knowledge graph below adds it once that finishes.")
        elif registered.get("error"):
            text = f"Not added to the Connections graph: {registered['error']}."
        elif registered.get("added"):
            text = (f"The Connections graph now reads this Ledger too ({Path(csv).name}; "
                    "only that file - Connections › Folders… lists it): a rebuild there "
                    "or Rebuild knowledge graph below picks up its rows.")
        else:
            return
        self._say_once(csv, text)

    def _say_once(self, csv: str, text: str) -> None:
        if (csv, text) not in self._kg_said:
            self._kg_said.add((csv, text))
            self._say(text)

    # ── step 2 ──────────────────────────────────────────────────────────
    def add_sources(self, paths) -> None:
        have = set(self.source_paths())
        for p in paths:
            p = str(p)
            if p and p not in have:
                self.sources.addItem(p)
                have.add(p)
        self._refresh_enabled()

    def remove_selected(self) -> None:
        for item in self.sources.selectedItems():
            self.sources.takeItem(self.sources.row(item))
        self._refresh_enabled()

    def clear_sources(self) -> None:
        self.sources.clear()
        self._refresh_enabled()

    # ── step 3 ──────────────────────────────────────────────────────────
    def on_extract(self) -> None:
        csv, sources = self.csv.text().strip(), self.source_paths()
        part, images = self.part.text().strip(), self.use_images.isChecked()
        mask = self.pattern_mask.text().strip()
        vision = self._selected_vision_model()
        # only when ticked, so an actions stand-in without the option works
        more = {}
        if self.ask_model.isChecked():
            # which model: the user picks one downloaded on this computer
            model_id = self._pick_model(self)
            if not model_id:
                self.status.setText("Not extracted: no model picked. Untick "
                                    "\"Ask Council's model about the rest\" to run without one.")
                return
            more = {"assist": model_id}

        def done(s) -> None:
            self.review.setText(s.review)
            self.stat["values"].setText(str(s.values))
            self.stat["pre_accepted"].setText(str(s.pre_accepted))
            self.stat["to_review"].setText(str(s.to_review))
            self.stat["images"].setText(f"{s.images_read} / {s.images}")
            self.stat["problems"].setText(str(len(s.problems)))
            if getattr(s, "cancelled", False) and getattr(s, "read_all", False):
                # before or while Council's model was asked: the same words
                # for both, and not 'partial' (second review, 2026-10-20)
                self.status.setText(
                    "Cancelled after every workbook was read: every value the rules found "
                    "is in the review. Open it, check the amber and red rows, map the "
                    "leftover labels, save it, then Apply.")
            elif getattr(s, "cancelled", False):
                self.status.setText(
                    "Cancelled. The partial review was still written — "
                    "open it to see what landed, or clear it and run again.")
            else:
                self.status.setText(
                    "Review written. Open it, check the amber and red rows, "
                    "map the leftover labels, save it, then Apply.")
            self._say(s.describe())
        self._run("Extracting",
                  lambda p: self.actions.extract(csv, sources, part, images, p,
                                                 pattern_mask=mask,
                                                 vision_model=vision,
                                                 cancel=self._cancel, **more), done,
                  cancellable=True)

    def on_diagnose(self) -> None:
        sources = self.source_paths()
        picked = [p for p in sources if Path(p).is_file()]
        start = picked[0] if len(picked) == 1 else self._start_dir()
        file = self._browse_diagnose_file(start)
        if not file:
            return
        csv, images = self.csv.text().strip(), self.use_images.isChecked()
        vision = self._selected_vision_model()
        more = {}
        if self.ask_model.isChecked():
            model_id = self._pick_model(self)
            if not model_id:
                self.status.setText("Not diagnosed: no model picked. Untick "
                                    "\"Ask Council's model about the rest\" to run without one.")
                return
            more = {"assist": model_id}

        def line(text: str) -> None:
            self._to_ui(lambda: self._say(text))

        def done(d) -> None:
            self.status.setText(f"Diagnosis: {d.outcome}. Report: {d.report}")
            self._say(d.describe())
            try:
                self.actions.open(d.report)
            except Exception as e:                        # noqa: BLE001
                self._say(f"(could not open the report: {e})")
        self._run(f"Diagnosing {Path(file).name}",
                  lambda p: self.actions.diagnose(file, csv, images, line,
                                                  vision_model=vision,
                                                  cancel=self._cancel, **more),
                  done, cancellable=True)

    def _browse_diagnose_file(self, start: str) -> str:
        """The file to diagnose, '' when cancelled."""
        p, _ = QFileDialog.getOpenFileName(self, "Pick the file that stalls the Ledger",
                                           start, XLSX)
        return p

    def on_review_window(self) -> None:
        """The click-through review (ledger_review_dialog), over the review
        workbook above. Not modal; read on a worker (a big review takes a
        moment)."""
        path = self.review.text().strip()
        if not path or not Path(path).is_file():
            self.status.setText("No review workbook yet: run Extract first.")
            return
        from council_core import ledger_review

        def done(review) -> None:
            from .ledger_review_dialog import ReviewDialog
            needs = sum(i.needs for i in review.items)
            dlg = ReviewDialog(review, parent=self, log=self._say, opener=self.actions.open,
                               on_saved=lambda n: self.status.setText(
                                   f"{n} answer(s) saved into the review. Apply puts the "
                                   "accepted values into the parent CSV."))
            self.review_dialog = dlg
            self._say(f"── Review by clicking ({Path(path).name}): {len(review.items)} "
                      f"item(s), {needs} need a decision")
            self.status.setText("Reviewing: answers are saved into the review workbook.")
            dlg.show()
        self._run("Reading the review", lambda _p: ledger_review.load(path), done)

    def on_open_review(self) -> None:
        path = self.review.text().strip()
        try:
            self.actions.open(path)
        except Exception as e:                            # noqa: BLE001
            self.status.setText(f"could not open {path}: {e}")

    # ── step 4 ──────────────────────────────────────────────────────────
    def on_apply(self) -> None:
        review, fill = self.review.text().strip(), self.fill_dir.text().strip()
        images = self.use_images.isChecked()
        vision = self._selected_vision_model()
        more = {"vision_model": vision} if images and vision else {}
        tab_csv = self.csv.text().strip()

        def step(p):
            s = self.actions.apply(review, fill, images, p, **more)
            # the CSV the review was applied to (its _run sheet), else the
            # one this tab shows
            applied = getattr(self.actions, "applied_csv", lambda _r: None)(review)
            return s, applied or tab_csv

        def done(result) -> None:
            s, applied = result
            self.stat["problems"].setText(str(len(s.problems)))
            self.status.setText(
                f"{s.accepted} values accepted → {s.rows} rows: {s.csv_result}. "
                "Rebuild the knowledge graph (here, or in Connections) to pick up the "
                "new rows.")
            self._say(s.describe())
            self._register(applied, again=True)
            self._kg_dirty = True
        self._run("Applying the review", step, done)

    def on_rebuild_kg(self) -> None:
        """Rebuild the Connections knowledge graph; the Ledger log shows
        the breakdown line via rebuild_summary_line."""
        if self.busy:
            return
        csv = self.csv.text().strip()
        conn = self._connections_tab()
        if conn is not None and not conn.hold_for_rebuild("Ledger"):
            self.status.setText("The Connections tab is rebuilding the graph already; "
                                "its rows will be there when it finishes.")
            return
        held = {"stats": None}

        def done(stats) -> None:
            from council_core import knowledge_graph as kgm
            stats = held["stats"] = stats or {}
            if stats.get("ledger_added"):
                self._say(f"The graph now also reads {stats['ledger_added']} "
                          "(only that file; Connections › Folders… lists it).")
            line = kgm.rebuild_summary_line(stats)
            self.status.setText(line)
            self._say(line)
            self._kg_dirty = False

        def after(err) -> None:
            if conn is not None:
                conn.rebuild_done_elsewhere(held["stats"],
                                            RuntimeError(err.splitlines()[0]) if err else None)
        self._run("Rebuilding the knowledge graph",
                  lambda p: self.actions.rebuild_kg(p, csv=csv or None), done,
                  after=after)

    def _connections_tab(self):
        """The Connections tab, when it has been built (it holds the graph's
        store open while it shows it)."""
        from .connections import ConnectionsTab
        built = getattr(self.window, "_built", None) or {}
        return next((t for t in built.values() if isinstance(t, ConnectionsTab)), None)

    # ── file dialogs ────────────────────────────────────────────────────
    def _start_dir(self, edit: Optional[QLineEdit] = None) -> str:
        for t in ([edit.text()] if edit else []) + [self.csv.text(), self.template.text()]:
            if t.strip():
                return str(Path(t.strip()).parent)
        return str(Path.home())

    def browse_template(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Template workbook",
                                           self._start_dir(self.template), TEMPLATE)
        if p:
            self.template.setText(p)

    def browse_csv(self) -> None:
        p, _ = QFileDialog.getSaveFileName(
            self, "Parent CSV (an existing one, or a new name)", self._start_dir(self.csv),
            "CSV (*.csv)", options=QFileDialog.Option.DontConfirmOverwrite)
        if p:
            self.csv.setText(p)
            self._on_csv_changed()

    def browse_sources(self) -> None:
        ps, _ = QFileDialog.getOpenFileNames(self, "Source workbooks", self._start_dir(), XLSX)
        self.add_sources(ps)

    def browse_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder of source workbooks",
                                             self._start_dir())
        if d:
            self.add_sources([d])

    def browse_review(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Review workbook",
                                           self._start_dir(self.review), TEMPLATE)
        if p:
            self.review.setText(p)

    def browse_fill_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder for filled templates",
                                             self._start_dir(self.fill_dir))
        if d:
            self.fill_dir.setText(d)


def build_ledger(window) -> QWidget:
    """Factory for the council_qt tab registry."""
    return LedgerTab(window)

