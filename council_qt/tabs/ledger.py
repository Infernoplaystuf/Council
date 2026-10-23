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
                               QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget)


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
                cancel=None, assist=False, prefer=()):
        reader = self.image_reader
        if reader is None and use_images and vision_model:
            # Explicit opt-in: the user picked a vision model in the dropdown.
            from source_extract import ollama_vision_reader
            reader = ollama_vision_reader(vision_model)
        return ledger_ingest.run_extract(csv, sources, part=part, use_images=use_images,
                                         image_reader=reader, progress=progress,
                                         pattern_mask=pattern_mask or None,
                                         cancel=cancel, log=self.stall_log,
                                         assist=assist, prefer=prefer)

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

    def rerun(self, review, fill_dir, use_images, progress,
              vision_model: Optional[str] = None, cancel=None, assist=False, prefer=()):
        """Re-run with my answers (user, 2026-10-23: "look at a few values
        and then give an answer and have it rerun and fill things in more
        confidently"): the saved review applied - its accepted values go in
        the parent CSV, where the next run learns from them as worked
        answers - then Extract run again on the review's own sources, part,
        pattern and operators, and the review's answers carried into the new
        one (ledger_review.carry_answers). Returns (ApplySummary,
        ExtractSummary or None when cancelled between the two, how many
        answers were carried)."""
        from council_core import kg_excel
        meta = kg_excel._review_meta(Path(review)) or {}
        csv, sources = meta.get("csv"), list(meta.get("sources") or [])
        if not csv or not sources:
            raise LedgerError(f"{Path(review).name} does not say which parent CSV and "
                              "sources it was made from; run Extract instead")
        applied = self.apply(review, fill_dir, use_images, progress, vision_model=vision_model)
        if cancel is not None and cancel.is_set():
            return applied, None, 0
        # a saved pattern only hinted at: left out, so it is a hint again
        mask = "" if meta.get("pattern_hint") else (meta.get("pattern_mask") or "")
        extracted = self.extract(csv, sources, meta.get("part") or "", use_images, progress,
                                 pattern_mask=mask, vision_model=vision_model, cancel=cancel,
                                 assist=assist, prefer=tuple(prefer or meta.get("prefer") or ()))
        # what was said Right or Wrong is not asked again
        from council_core import ledger_review
        new, carried = getattr(extracted, "review", ""), 0
        if new and Path(new).is_file() and not ledger_ingest._same_file(new, review):
            fresh = ledger_review.load(new)
            carried = ledger_review.carry_answers(ledger_review.load(review), fresh)
            if carried:
                progress(f"{carried} answer(s) carried into the new review; filling in "
                         "the template again")
                ledger_review.write_all_filled(ledger_review.load(new), progress)
        return applied, extracted, carried

    def update(self, csv, progress, use_images, vision_model: Optional[str] = None,
               cancel=None, assist=False, prefer=()):
        """Update with new files (user, 2026-10-23: "the folders it was run on
        get more data in them I want to be able to rerun from that stage"):
        the saved Ledger's folders scanned again and only the files new or
        changed since they were read given to Extract, with its saved part,
        pattern and operators. What the Ledger holds already is pre-accepted
        again and a value other than it flagged (source_extract.decide).
        Returns (ExtractSummary, new, changed); the summary None when
        nothing is new."""
        from council_core import ledger_saved
        led = ledger_saved.load(csv)
        if led is None or not led.sources:
            raise LedgerError(f"{Path(csv).name} has no saved run to update from: run "
                              "Extract on it once first")
        files = ledger_ingest.source_files(csv, led.sources)
        new, changed, _same = ledger_saved.changed_files(led, files)
        if not new and not changed:
            return None, [], []
        st = led.settings
        from source_extract import parse_prefer
        extracted = self.extract(csv, new + changed, st.get("part") or "", use_images,
                                 progress, pattern_mask=st.get("pattern_mask") or None,
                                 vision_model=vision_model, cancel=cancel, assist=assist,
                                 prefer=tuple(prefer or parse_prefer(st.get("prefer") or "")))
        return extracted, new, changed

    def record_run(self, csv, summary, **kw):
        """The run kept in the Ledger's saved runs (council_core.ledger_saved)."""
        from council_core import ledger_saved
        return ledger_saved.record_run(csv, summary, **kw)

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
        #: the saved Ledger of the parent CSV shown (council_core.ledger_saved)
        self._saved = None
        self.runs_dialog = None
        self._build()
        self._kg_timer = QTimer(self)
        self._kg_timer.setSingleShot(True)
        self._kg_timer.setInterval(self.KG_RETRY_MS)
        self._kg_timer.timeout.connect(self._register_pending)
        self._refresh_reader_choices()
        self.refresh_saved()
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
        # in a scroll area: with Saved Ledgers on top the tab no longer fit a
        # laptop's screen
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        page.addWidget(scroll)
        outer = QVBoxLayout(body)
        outer.setContentsMargins(10, 8, 10, 8)

        # Ledgers saved with their sources and runs, named, grouped by
        # template and/or project (council_core.ledger_saved, user 2026-10-23)
        from council_core import ledger_saved
        g0 = QGroupBox("Saved Ledgers")
        grid0 = QGridLayout(g0)
        grid0.addWidget(QLabel("Saved Ledger:"), 0, 0)
        self.saved_choice = QComboBox()
        self.saved_choice.setMinimumContentsLength(36)
        self.saved_choice.setToolTip("Pick a Ledger you ran before: its template, parent "
                                     "CSV, folders and settings are filled in.")
        self.saved_choice.activated.connect(self._on_saved_picked)
        grid0.addWidget(self.saved_choice, 0, 1)
        row0 = QHBoxLayout()
        row0.addWidget(QLabel("Group by:"))
        self.group_by = QComboBox()
        for key, words in ledger_saved.GROUPS.items():
            self.group_by.addItem(words, key)
        self.group_by.currentIndexChanged.connect(lambda _i: self.refresh_saved())
        row0.addWidget(self.group_by)
        self.update_btn = self._step_button(row0, "Update with new files", self.on_update)
        self.update_btn.setToolTip(
            "Read only the files that are new or changed in this Ledger's folders since its "
            "last run. Values the Ledger already holds are accepted again; a value that "
            "differs from one you accepted is flagged for you, never written over it.")
        self.runs_btn = self._step_button(row0, "Runs…", self.on_runs_window)
        self.runs_btn.setToolTip("This Ledger's runs: their names, counts, reviews and "
                                 "filled templates")
        self.forget_saved_btn = self._step_button(row0, "Take off the list",
                                                  self.on_forget_saved)
        self.forget_saved_btn.setToolTip("Take this Ledger off the list. Nothing is deleted: "
                                         "pick its parent CSV again to bring it back.")
        row0.addStretch(1)
        grid0.addLayout(row0, 0, 2)
        grid0.addWidget(QLabel("Name:"), 1, 0)
        self.ledger_name = QLineEdit()
        self.ledger_name.setPlaceholderText("named after the template if left empty")
        self.ledger_name.editingFinished.connect(self._save_details)
        grid0.addWidget(self.ledger_name, 1, 1)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Project / program:"))
        self.ledger_project = QLineEdit()
        self.ledger_project.setPlaceholderText("optional: to group Ledgers by")
        self.ledger_project.editingFinished.connect(self._save_details)
        prow.addWidget(self.ledger_project)
        grid0.addLayout(prow, 1, 2)
        outer.addWidget(g0)

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
        # one workbook's several readings of a value (user, 2026-10-23): the
        # operator whose reading is used, else the first reading
        prefer_row = QHBoxLayout()
        prefer_row.addWidget(QLabel("Operator to prefer (optional):"))
        self.prefer_operator = QLineEdit()
        self.prefer_operator.setPlaceholderText("initials or names, e.g. JB, Alex King")
        self.prefer_operator.setToolTip(
            "When a workbook has several readings of one value (an EC response measured\n"
            "by more than one operator) and the template keeps one: the reading of the\n"
            "first operator listed here who has one is used - initials or a name, either\n"
            "matches the other (JB is Jane Brown). Left empty, or none of them\n"
            "read it: the first reading in the workbook is used. The others are set aside\n"
            "(N in the review). Values two workbooks disagree on are still yours to choose.")
        prefer_row.addWidget(self.prefer_operator, 1)
        v2.addLayout(prefer_row)
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
        # a filled copy of the template, picked on its own (user, 2026-10-23):
        # worked answers and Council's model's guide, never a source
        example_row = QHBoxLayout()
        example_row.addWidget(QLabel("Filled example (optional):"))
        self.filled_example = QLineEdit()
        self.filled_example.setReadOnly(True)
        self.filled_example.setPlaceholderText(
            "a copy of the template filled in for one sample")
        self.filled_example.setToolTip(
            "A copy of your template filled in by hand for one sample. It is not read\n"
            "as a source: its values teach which source labels are which field (for the\n"
            "same sample in the sources), and Council's model is shown what each field\n"
            "holds there and what its part number looks like. Saved with this Ledger.")
        example_row.addWidget(self.filled_example, 1)
        self.example_btn = QPushButton("Pick…")
        self.example_btn.clicked.connect(self.on_pick_filled_example)
        example_row.addWidget(self.example_btn)
        self.forget_example_btn = QPushButton("Forget")
        self.forget_example_btn.clicked.connect(lambda: self._set_filled_example(""))
        example_row.addWidget(self.forget_example_btn)
        v2.addLayout(example_row)
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
        # the template filled in per part, tinted by confidence; click a cell
        # to see its source (user, 2026-10-23)
        self.filled_btn = self._step_button(row, "Filled template…", self.on_filled_window)
        self.filled_btn.setToolTip(
            "The template filled in for one part at a time, each value tinted by how sure "
            "it is (green sure, yellow fairly, orange unsure, red disagreeing). Click a "
            "cell to see the source cell it came from, then Right or Wrong.")
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
        self._load_filled_example()
        self._load_saved()
        self._register(self.csv.text().strip())

    def _load_filled_example(self) -> None:
        """Show the filled example saved for the parent CSV."""
        if not hasattr(self, "filled_example"):
            return
        csv = self.csv.text().strip()
        path = ""
        if csv:
            from council_core import ledger_examples
            path = ledger_examples.load_filled_example(csv)
        self.filled_example.setText(path)
        self.forget_example_btn.setEnabled(bool(path))

    def _browse_filled_example(self) -> str:
        p, _ = QFileDialog.getOpenFileName(
            self, "Pick a copy of the template filled in for one sample",
            self._start_dir(), XLSX)
        return p

    def on_pick_filled_example(self) -> None:
        if not self.csv.text().strip():
            self.status.setText("Create or pick the parent CSV first: the filled example "
                                "is saved with it.")
            return
        p = self._browse_filled_example()
        if p:
            self._set_filled_example(p)

    def _set_filled_example(self, path: str) -> None:
        csv = self.csv.text().strip()
        if not csv:
            return
        from council_core import ledger_examples
        try:
            ledger_examples.save_filled_example(csv, path)
        except OSError as e:
            self._say(f"The filled example could not be saved beside {Path(csv).name}: {e}")
            return
        self._load_filled_example()
        if not path:
            self._say("Filled example forgotten for this Ledger.")
            return
        rows = []
        try:
            from sample_ledger import ParentCSV
            schema = ParentCSV(csv).schema
            rows = ledger_examples.filled_example_rows(path, schema) if schema else []
        except Exception:                                    # noqa: BLE001
            rows = []
        self._say(f"Filled example {Path(path).name}: "
                  + (f"{len(rows)} row(s) for "
                     + ", ".join(ledger_examples.filled_example_parts(
                         rows, schema.id_field)) + " — used by Extract from now on"
                     if rows else "no part number found in the template's part cell — "
                     "is it a filled copy of this Ledger's template?"))

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
        if hasattr(self, "update_btn"):
            saved = self._saved
            self.update_btn.setEnabled(saved is not None and bool(saved.sources))
            self.runs_btn.setEnabled(saved is not None and bool(saved.runs))
            self.forget_saved_btn.setEnabled(saved is not None)
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
        if hasattr(self, "filled_btn"):
            self.filled_btn.setEnabled(has_review)
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
        from source_extract import parse_prefer
        prefer = parse_prefer(self.prefer_operator.text())
        if prefer:                         # only then, as with the model
            more["prefer"] = prefer

        record = self._recorder(csv, "extract", sources=sources,
                                settings=self._settings(), model_id=more.get("assist", ""))

        def step(p):
            s = self.actions.extract(csv, sources, part, images, p, pattern_mask=mask,
                                     vision_model=vision, cancel=self._cancel, **more)
            return s, record(s)

        def done(result) -> None:
            s, saved = result
            self._show_extract(s)
            self._said_saved(saved)
        self._run("Extracting", step, done, cancellable=True)

    def _show_extract(self, s) -> None:
        """An Extract's summary in the stats, the status line and the log."""
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

    # ── saved Ledgers (council_core.ledger_saved) ────────────────────────
    def _settings(self) -> dict:
        """What a saved Ledger keeps of this run, to run it again the same way."""
        return {"part": self.part.text().strip(),
                "pattern_mask": self.pattern_mask.text().strip(),
                "prefer": self.prefer_operator.text().strip(),
                "use_images": self.use_images.isChecked()}

    def _recorder(self, csv: str, kind: str, *, sources=(), settings=None,
                  model_id: str = "", new_files: int = 0) -> Callable:
        """``record(summary)`` for the step's worker (made on the GUI thread,
        which reads the fields): the run kept in the Ledger's saved runs,
        named by Council's model when one was picked. ``record(summary,
        csv=..., new_files=...)`` when the step learns them. Returns the
        Run, None for a cancelled run, or why it was not kept - a run is made
        whether or not it can be kept."""
        template = self.template.text().strip()
        given_csv, given_new = csv, new_files

        def record(summary, csv: str = "", new_files: Optional[int] = None):
            csv = csv or given_csv
            new_files = given_new if new_files is None else new_files
            if summary is None or getattr(summary, "cancelled", False):
                return None
            namer = None
            if model_id:
                from council_core import ledger_llm, ledger_saved

                def namer(facts, fallback):
                    return ledger_saved.model_name(ledger_llm.chat_for(model_id), facts,
                                                   fallback)
            try:
                return self.actions.record_run(csv, summary, kind=kind, sources=sources,
                                               template=template, settings=settings,
                                               new_files=new_files, namer=namer)
            except Exception as e:                        # noqa: BLE001
                return f"this run was not saved: {e}"
        return record

    def _said_saved(self, saved) -> None:
        if isinstance(saved, str):
            self._say(f"(Saved Ledgers: {saved})")
        elif saved is not None:
            self._say(f"Saved as \"{saved.name}\" in this Ledger's runs (Runs… to rename it).")
        self.refresh_saved()

    def refresh_saved(self) -> None:
        """The Saved Ledger list, grouped as Group by says; the one of the
        parent CSV shown is picked."""
        from council_core import ledger_saved
        by = self.group_by.currentData() or "template"
        current = self.csv.text().strip()
        box = self.saved_choice
        box.blockSignals(True)
        box.clear()
        model = box.model()
        pick = -1
        groups = ledger_saved.group(ledger_saved.listed(), by)
        for head, ledgers in groups:
            box.addItem(head)
            model.item(box.count() - 1).setEnabled(False)
            for led in ledgers:
                last = led.last
                box.addItem(f"    {led.title}" + (f"  ·  {len(led.runs)} run(s), last "
                                                  f"{last.at[:10]}" if last else ""), led.csv)
                if current and ledger_saved.file_key(led.csv) == ledger_saved.file_key(current):
                    pick = box.count() - 1
        if not groups:
            box.addItem("(none yet: a Ledger is saved when Extract runs on it)")
            model.item(0).setEnabled(False)
        box.setCurrentIndex(pick if pick >= 0 else -1)
        box.blockSignals(False)
        self._load_saved()

    def _load_saved(self) -> None:
        """The saved Ledger of the parent CSV shown, and its name and
        project in the fields."""
        from council_core import ledger_saved
        csv = self.csv.text().strip()
        self._saved = ledger_saved.load(csv) if csv else None
        self.ledger_name.setText(self._saved.name if self._saved else "")
        self.ledger_project.setText(self._saved.project if self._saved else "")
        self.ledger_project.setCompleter(QCompleter(ledger_saved.projects(), self))
        self._refresh_enabled()

    def _on_saved_picked(self, index: int) -> None:
        csv = self.saved_choice.itemData(index)
        if csv:
            self.load_saved(str(csv))

    def load_saved(self, csv: str) -> bool:
        """A saved Ledger's template, parent CSV, sources and settings filled
        in, its last run's review as the review workbook."""
        from council_core import ledger_saved
        if self.busy:
            return False
        led = ledger_saved.load(csv)
        if led is None:
            self.status.setText(f"No saved Ledger beside {Path(csv).name}.")
            return False
        self.template.setText(led.template)
        self.csv.setText(led.csv)
        self.sources.clear()
        self.add_sources(led.sources)
        st = led.settings
        self.part.setText(str(st.get("part") or ""))
        self.pattern_mask.setText(str(st.get("pattern_mask") or ""))
        self.prefer_operator.setText(str(st.get("prefer") or ""))
        if "use_images" in st:
            self.use_images.setChecked(bool(st["use_images"]))
        last = led.last
        if last is not None and Path(last.review).is_file():
            self.review.setText(last.review)
        self._on_csv_changed()
        self.refresh_saved()
        self.status.setText(
            f"Saved Ledger \"{led.title}\": {len(led.runs)} run(s)"
            + (f", the last \"{last.name}\"" if last else "")
            + ". Update with new files reads only what was added since.")
        return True

    def _save_details(self) -> None:
        """The Name and Project / program fields, kept in the saved Ledger
        (made now when the CSV has none yet)."""
        from council_core import ledger_saved
        csv = self.csv.text().strip()
        if not csv or not Path(csv).is_file():
            return
        name, project = self.ledger_name.text().strip(), self.ledger_project.text().strip()
        led = ledger_saved.load(csv)
        if led is None:
            if not name and not project:
                return
            led = ledger_saved.SavedLedger(csv=csv, template=self.template.text().strip(),
                                           sources=self.source_paths(),
                                           settings=self._settings())
        if (led.name, led.project) == (name, project):
            return
        led.name, led.project = name, project
        try:
            ledger_saved.save(led)
        except OSError as e:
            self.status.setText(f"Not saved: {e}")
            return
        self.refresh_saved()

    def on_forget_saved(self) -> None:
        from council_core import ledger_saved
        csv = self.csv.text().strip()
        if not csv:
            return
        ledger_saved.forget(csv)
        self.refresh_saved()
        self.status.setText(f"{Path(csv).name} taken off the list of saved Ledgers; nothing "
                            "was deleted.")

    def on_update(self) -> None:
        """Update with new files (LedgerActions.update), then the filled
        template window on the new review."""
        csv = self.csv.text().strip()
        if self._saved is None:
            self.status.setText("No saved Ledger: run Extract once first.")
            return
        images = self.use_images.isChecked()
        vision = self._selected_vision_model()
        more = {}
        if self.ask_model.isChecked():
            model_id = self._pick_model(self)
            if not model_id:
                self.status.setText("Not updated: no model picked. Untick \"Ask Council's "
                                    "model about the rest\" to run without one.")
                return
            more["assist"] = model_id
        from source_extract import parse_prefer
        prefer = parse_prefer(self.prefer_operator.text())
        if prefer:
            more["prefer"] = prefer

        record = self._recorder(csv, "update", model_id=more.get("assist", ""))

        def step(p):
            s, new, changed = self.actions.update(csv, p, images, vision_model=vision,
                                                  cancel=self._cancel, **more)
            return s, new, changed, record(s, new_files=len(new) + len(changed))

        def done(result) -> None:
            s, new, changed, saved = result
            if s is None:
                last = self._saved.last if self._saved else None
                self.status.setText("Nothing new: no file in this Ledger's folders was added "
                                    "or changed since "
                                    + (f"\"{last.name}\"." if last else "its last run."))
                self._say(self.status.text())
                return
            self._show_extract(s)
            self._said_saved(saved)
            if getattr(s, "cancelled", False):
                return
            line = (f"Updated with {len(new)} new and {len(changed)} changed file(s): "
                    f"{s.pre_accepted} of {s.values} values pre-accepted, {s.to_review} "
                    "to review.")
            self._say(line)
            from council_core import ledger_records
            if not ledger_records.is_record_ledger(csv):
                self.on_filled_window()
            self.status.setText(line)
        self._run("Updating with new files", step, done, cancellable=True)

    def on_runs_window(self) -> None:
        """This Ledger's runs (ledger_runs_dialog): rename one, open its
        filled template or its review."""
        if self._saved is None:
            return
        from .ledger_runs_dialog import LedgerRunsDialog
        dlg = LedgerRunsDialog(self._saved.csv, parent=self, open_filled=self.open_run_filled,
                               opener=self.actions.open, on_renamed=self.refresh_saved)
        self.runs_dialog = dlg
        dlg.show()

    def open_run_filled(self, review: str) -> None:
        """A past run's filled template window."""
        if not Path(review).is_file():
            self.status.setText(f"That run's review is not there any more: {review}")
            return
        self.review.setText(review)
        self.on_filled_window()

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

    def on_filled_window(self, part: str = "") -> None:
        """The filled template window (ledger_review_dialog.FilledTemplateDialog)
        over the review workbook above, on ``part`` when it has one."""
        path = self.review.text().strip()
        if not path or not Path(path).is_file():
            self.status.setText("No review workbook yet: run Extract first.")
            return
        from council_core import ledger_review

        def done(review) -> None:
            from .ledger_review_dialog import FilledTemplateDialog
            if review.kind != "samples":
                self.status.setText("The filled template is for a sample Ledger; use "
                                    "Review by clicking for a table Ledger.")
                return
            dlg = FilledTemplateDialog(review, parent=self, log=self._say,
                                       opener=self.actions.open,
                                       on_saved=lambda n: self.status.setText(
                                           f"{n} answer(s) saved into the review. Apply puts "
                                           "the accepted values into the parent CSV."),
                                       on_rerun=self.on_rerun_with_answers, part=part)
            self.filled_dialog = dlg
            self._say(f"── Filled template ({Path(path).name}): "
                      f"{len(ledger_review.parts(review))} part(s)")
            dlg.show()
        self._run("Reading the review", lambda _p: ledger_review.load(path), done)

    def on_rerun_with_answers(self, review: str, part: str = "") -> bool:
        """The filled template window's "Re-run with my answers" (its answers
        already saved): Apply, then Extract again on the review's sources
        (LedgerActions.rerun), then the window again on the new review, at
        the same part. The values said Right are in the parent CSV by then,
        so the run learns from them; a new review is written, the old one
        kept. False when it did not start."""
        if self.busy:
            self.status.setText("Not re-run: wait for the current step to finish.")
            return False
        fill = self.fill_dir.text().strip()
        images = self.use_images.isChecked()
        vision = self._selected_vision_model()
        more = {"vision_model": vision} if images and vision else {}
        if self.ask_model.isChecked():
            model_id = self._pick_model(self)
            if not model_id:
                self.status.setText("Not re-run: no model picked. Untick \"Ask Council's "
                                    "model about the rest\" to run without one.")
                return False
            more["assist"] = model_id
        from source_extract import parse_prefer
        prefer = parse_prefer(self.prefer_operator.text())
        if prefer:                         # typed here: these, else the review's
            more["prefer"] = prefer
        tab_csv = self.csv.text().strip()
        record = self._recorder(tab_csv, "rerun", model_id=more.get("assist", ""))

        def step(p):
            applied, extracted, carried = self.actions.rerun(review, fill, images, p,
                                                             cancel=self._cancel, **more)
            csv = getattr(self.actions, "applied_csv", lambda _r: None)(review) or tab_csv
            saved = record(extracted, csv=csv)
            return applied, extracted, carried, csv, saved

        def done(result) -> None:
            applied, extracted, carried, csv, saved = result
            self._said_saved(saved)
            self._say(applied.describe())
            self._register(csv, again=True)
            self._kg_dirty = True
            if extracted is None:
                self.stat["problems"].setText(str(len(applied.problems)))
                self.status.setText(f"{applied.accepted} values accepted into the parent "
                                    "CSV; cancelled before Extract ran again.")
                return
            self._show_extract(extracted)
            if not getattr(extracted, "cancelled", False):
                line = (f"Re-run with your answers: {applied.accepted} values accepted into "
                        f"the parent CSV; the new review pre-accepts {extracted.pre_accepted} "
                        f"of {extracted.values} values, {extracted.to_review} held back"
                        + (f" ({carried} of them already answered by you)" if carried
                           else "") + ".")
                self._say(line)
                self.on_filled_window(part)
                self.status.setText(line)
        self._run("Re-running with your answers", step, done, cancellable=True)
        return True

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

