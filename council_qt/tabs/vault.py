"""
council_qt.tabs.vault — the Vault manager, ported.

THE PILOT. This tab was chosen to go first because it is the largest in the app
(491 toolkit-bound lines across 50 methods) and because it is a superset of the
mechanisms every other tab uses: two tree views, a splitter, forms bound to
variables, a file browser, a preview pane, an activity log with coloured levels,
long-running work on worker threads, and modal dialogs. If this ports cleanly,
the remaining nineteen are smaller versions of the same problem.

WHAT IS AND IS NOT HERE
The VIEW is complete: every control the Tk tab has, in the same arrangement.
The ACTIONS are split by where the logic currently lives:

  * Read-only work that needs nothing from CouncilConsole — walking the vault,
    previewing a file, copying a path, opening the folder, instant search — is
    implemented here and genuinely works.
  * Everything that runs an operation (clone, pull, index, embeddings, convert,
    deferred tasks, collections) still lives as a `_vmgr_*` method on the Tk
    shell, bound to `self` and to Tk widgets. Those are wired to the actions
    object below, whose default implementation says plainly that the logic has
    not been extracted yet rather than pretending to work.

That boundary is the phase-3 extraction line from docs/qt_full_port_scope.md,
and drawing it explicitly is the point: the Qt view is finished and reviewable
now, and each extracted action lights one button up without touching this file.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog,
                               QFrame, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                               QScrollArea, QSplitter, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import theme
from ..view import ViewHelpers, amp

#: How much of a file the preview pane reads. The Tk tab reads the head of the
#: file too — a vault holds multi-GB CSVs and previewing one whole would hang
#: the UI thread for minutes.
PREVIEW_BYTES = 64_000


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


class VaultActions:
    """What the Vault tab can ask the application to do.

    The read-only half is implemented; the operational half raises
    NotYetExtracted so a button says something true instead of doing nothing.
    A later phase replaces this object with one backed by the extracted
    `_vmgr_*` logic, and the view does not change.
    """

    class NotYetExtracted(RuntimeError):
        pass

    def __init__(self, vault_dir: Path, index=None):
        self.vault_dir = Path(vault_dir)
        self._index = index          # data_index.DataIndex, built on first use

    # -- implemented -----------------------------------------------------
    def walk(self, root: Optional[Path] = None):
        """(path, is_dir, size) for one directory level, folders first."""
        root = Path(root or self.vault_dir)
        try:
            entries = sorted(root.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return []
        out = []
        for entry in entries:
            if entry.name.startswith("."):
                continue                       # .git, .chromadb and friends
            try:
                size = 0 if entry.is_dir() else entry.stat().st_size
            except OSError:
                size = 0
            out.append((entry, entry.is_dir(), size))
        return out

    def preview(self, path: Path) -> str:
        p = Path(path)
        if p.is_dir():
            return f"{p}\n\n(folder)"
        try:
            with open(p, "rb") as fh:
                raw = fh.read(PREVIEW_BYTES)
        except OSError as exc:
            return f"cannot read {p.name}: {exc}"
        text = raw.decode("utf-8", errors="replace")
        if p.stat().st_size > len(raw):
            text += f"\n\n… truncated at {_human(len(raw))}"
        return text

    def search(self, term: str):
        """Find vault files by name or by indexed content.

        Both halves, the same as the Tk tab: this used to search filenames
        only, which is the same box and button quietly giving a different
        answer.
        """
        from council_core import vault_search
        try:
            import data_index
            in_dir = data_index.input_dir(self.vault_dir)
        except Exception:                                 # noqa: BLE001
            in_dir = self.vault_dir
        return vault_search.search_vault(term, in_dir, index=self._data_index())

    def _data_index(self):
        """The data index, built on first use and kept.

        Built lazily because opening the Vault tab should not pay for an index
        the user may never search, and because a failure here must cost the
        content half of one search, not the tab.
        """
        if self._index is None:
            try:
                import data_index
                self._index = data_index.DataIndex(
                    search_roots=[data_index.input_dir(self.vault_dir),
                                  data_index.bundled_samples_dir()],
                    write_root=data_index.output_dir(self.vault_dir))
            except Exception:                             # noqa: BLE001
                return None
        return self._index

    def open_folder(self, path: Optional[Path] = None) -> None:
        target = Path(path or self.vault_dir)
        if sys.platform.startswith("win"):
            os.startfile(str(target))          # noqa: S606 — the platform API
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])

    # -- not yet extracted ------------------------------------------------
    def _later(self, what: str):
        raise self.NotYetExtracted(
            f"{what} still lives on the Tk shell (a _vmgr_* method bound to "
            f"CouncilConsole). Extracting it is phase 3 — see "
            f"docs/qt_full_port_scope.md.")

    def import_zip(self, path, subfolder, log=None):
        from council_core import vault_import
        return vault_import.import_zip(path, vault_dir=self.vault_dir,
                                       subfolder=subfolder, log=log)

    def import_folder(self, folder, log=None):
        from council_core import vault_import
        return vault_import.import_folder(folder, vault_dir=self.vault_dir,
                                          log=log)

    def input_dir(self) -> Path:
        """Where a batch zip import lands: data_in/, the analyst's scope.

        Files extracted to the vault root are not picked up by the index, which
        is why the Tk handler goes out of its way to find this directory."""
        try:
            import data_index
            return Path(data_index.input_dir(self.vault_dir))
        except Exception:                                # noqa: BLE001
            return self.vault_dir

    def import_zip_folder(self, folder, log=None):
        from council_core import vault_import
        return vault_import.import_zip_folder(folder, input_dir=self.input_dir(),
                                              log=log)
    # EXTRACTED — this one is real. The operation lives in
    # council_core.vault_ops and the Tk shell calls the same function, so the
    # two front ends cannot drift about what indexing means.
    def vault_index(self):
        """The VaultIndex for this vault, or None.

        Mirrors the shell's lazy getter: construction failure is an answer, not
        an exception, because an unreadable vault is a normal thing to report."""
        try:
            import vault_index
            return vault_index.VaultIndex(self.vault_dir)
        except Exception as exc:                        # noqa: BLE001
            print(f"[VaultIndex] init failed: {exc!r}", file=sys.stderr)
            return None

    def build_keyword_index(self, on_progress=None):
        from council_core import vault_ops
        return vault_ops.build_keyword_index(self.vault_index(),
                                             on_progress=on_progress)

    def build_descriptions(self, on_progress=None):
        from council_core import vault_ops
        return vault_ops.build_descriptions(self.vault_index(),
                                            on_progress=on_progress)

    def starting_descriptions(self):
        from council_core import vault_ops
        return vault_ops.starting_descriptions(self.vault_index())

    def build_embeddings(self, on_progress=None):
        from council_core import vault_ops
        return vault_ops.build_embeddings(self.vault_index(),
                                          on_progress=on_progress)

    def starting_embeddings(self):
        from council_core import vault_ops
        return vault_ops.starting_embeddings(self.vault_index())

    def clone(self, url, subfolder=None, branch=None, log=None):
        from council_core import vault_ops
        return vault_ops.clone(url, vault_dir=self.vault_dir,
                               subfolder=subfolder or None,
                               branch=branch or None, log=log)

    def pull(self, subfolder, log=None):
        from council_core import vault_ops
        return vault_ops.pull(self.vault_dir, subfolder, log=log)

    def build_descriptions(self):                   self._later("Descriptions")
    def build_embeddings(self):                     self._later("Embeddings")
    def convert_mongo(self, path, csv, schema, text, scan_all=False):
        self._later("Mongo conversion")
    def build_stats(self):
        # The only one still behind the line: the shell's _build_stats_index
        # reaches into CouncilConsole's own caches, so the operation cannot be
        # called from here until that is extracted too.
        self._later("Data stats")

    # -- extracted: the store-backed data ---------------------------------
    def pending_tasks(self):
        from council_core import vault_data
        return vault_data.pending_tasks(self.vault_dir)

    def set_task_status(self, task_id, status):
        from council_core import vault_data
        return vault_data.set_task_status(self.vault_dir, task_id, status)

    def all_collections(self):
        from council_core import vault_data
        return vault_data.all_collections(self.vault_dir)

    def run_deferred(self, task_id):
        from council_core import vault_jobs
        return vault_jobs.run_deferred_task(self.vault_dir, task_id)

    def propose_members(self, name: str):
        from council_core import vault_jobs
        return vault_jobs.propose_collection_members(
            self.vault_dir, name, index=self._data_index())

    def candidate_files(self):
        from council_core import vault_jobs
        return vault_jobs.collection_candidate_files(self.vault_dir)

    def save_collection(self, name: str, files, *, renaming_from=None):
        from council_core import vault_jobs
        return vault_jobs.save_collection(self.vault_dir, name, files,
                                          renaming_from=renaming_from)

    def summarize_collection(self, name: str):
        from council_core import vault_jobs
        return vault_jobs.summarize_collection(self.vault_dir, name)

    def collection_files(self, name: str):
        """The files a collection already holds, as stored relative paths."""
        try:
            import vault_collections
            store = vault_collections.CollectionStore(self.vault_dir)
            for collection in store.all():
                if collection.name == name:
                    return list(collection.files)
        except Exception:                                 # noqa: BLE001
            pass
        return []

    def delete_collection(self, name):
        from council_core import vault_data
        return vault_data.delete_collection(self.vault_dir, name)

    def delete(self, path):
        from council_core import vault_data
        return vault_data.delete_path(path, self.vault_dir)

    def read_misses(self):
        from council_core import vault_data
        return vault_data.read_misses(self.vault_dir)

    def clear_misses(self):
        from council_core import vault_data
        return vault_data.clear_misses(self.vault_dir)

    def convert_mongo(self, selected, csv, schema, text, scan_all=False,
                      on_progress=None):
        from council_core import vault_data
        out_root = self.vault_dir / "data_in" / "converted_mongo"
        if scan_all:
            files = vault_data.find_mongo_files(self.input_dir(), out_root)
        else:
            files = [Path(str(selected).strip())]
        return vault_data.convert_mongo(files, out_root, want_csv=csv,
                                        want_schema=schema, want_text=text,
                                        on_progress=on_progress)


class VaultTab(ViewHelpers, QWidget):
    """The Vault manager as a Qt widget."""

    def __init__(self, window, actions: Optional[VaultActions] = None,
                 parent=None):
        super().__init__(parent)
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or VaultActions(_default_vault_dir())
        self._tokens = theme.tokens("dark")
        self._build()
        self.refresh_tree()
        self.on_refresh_deferred()
        self.on_refresh_collections()

    # ---------------------------------------------------------------- build
    def _build(self) -> None:
        outer = QVBoxLayout(self)

        # ── Top bar ──────────────────────────────────────────────────
        top = QHBoxLayout()
        label = QLabel("Vault:")
        label.setStyleSheet("font-weight: bold;")
        top.addWidget(label)
        path = QLabel(str(self.actions.vault_dir))
        path.setStyleSheet(f"color: {self._tokens['accent']};")
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top.addWidget(path)
        top.addStretch(1)
        for text, slot in (("RAG Misses", self.on_rag_misses),
                           ("⟳ Refresh", self.refresh_tree),
                           ("📂 Open Folder", self.on_open_folder)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            top.addWidget(button)
        outer.addLayout(top)

        # ── Instant search ───────────────────────────────────────────
        search = QHBoxLayout()
        find = QLabel("🔍 Find files:")
        find.setStyleSheet("font-weight: bold;")
        search.addWidget(find)
        self.search_edit = QLineEdit()
        self.search_edit.setFixedWidth(320)
        self.search_edit.returnPressed.connect(self.on_search)
        search.addWidget(self.search_edit)
        go = QPushButton("Search")
        go.clicked.connect(self.on_search)
        search.addWidget(go)
        hint = QLabel("by file name — instant, no model")
        hint.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        search.addWidget(hint)
        search.addStretch(1)
        outer.addLayout(search)

        # ── Main split ───────────────────────────────────────────────
        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(self._build_left())
        main.addWidget(self._build_right())
        main.setStretchFactor(0, 0)
        main.setStretchFactor(1, 1)
        main.setSizes([460, 700])
        outer.addWidget(main, 1)

    def _build_left(self) -> QWidget:
        """The control column. Scrollable, because it is taller than any
        window — the Tk version simply overflows."""
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(4, 4, 4, 4)

        layout.addWidget(self._clone_box())
        layout.addWidget(self._import_box())
        layout.addWidget(self._index_box())
        layout.addWidget(self._mongo_box())
        layout.addWidget(self._deferred_box())
        layout.addWidget(self._collections_box())
        layout.addWidget(self._stats_box())
        layout.addWidget(self._tree_box(), 1)

        scroll = QScrollArea()
        scroll.setWidget(column)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(440)
        return scroll

    # -- the boxes, in the Tk tab's order --------------------------------
    def _clone_box(self) -> QGroupBox:
        box = QGroupBox("Add GitHub Repo to Vault")
        grid = QVBoxLayout(box)
        self.url_edit = self._labelled_row(grid, "URL:", width=340)
        row = QHBoxLayout()
        self.subfolder_edit = self._inline(row, "Subfolder:", 150)
        self.branch_edit = self._inline(row, "Branch:", 90)
        row.addStretch(1)
        grid.addLayout(row)
        buttons = QHBoxLayout()
        self._button(buttons, "⬇  Clone Repo", self.on_clone)
        self._button(buttons, "🔄 Pull Updates", self.on_pull)
        buttons.addStretch(1)
        grid.addLayout(buttons)
        return box

    def _import_box(self) -> QGroupBox:
        box = QGroupBox("Import Files into Vault")
        layout = QVBoxLayout(box)

        row = QHBoxLayout()
        self.zip_edit = self._inline(row, "Zip file:", 260)
        self._button(row, "📂 Browse", lambda: self._pick_file(self.zip_edit,
                                                               "Zip archives (*.zip)"))
        row.addStretch(1)
        layout.addLayout(row)

        row = QHBoxLayout()
        self.zip_subfolder_edit = self._inline(row, "Subfolder:", 150)
        note = QLabel("(blank = zip name)")
        note.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(note)
        row.addStretch(1)
        layout.addLayout(row)

        row = QHBoxLayout()
        self._button(row, "📦 Extract Zip to Vault", self.on_import_zip)
        row.addStretch(1)
        layout.addLayout(row)

        row = QHBoxLayout()
        self.zipdir_edit = self._inline(row, "Zip folder:", 260)
        self._button(row, "📂 Browse", lambda: self._pick_dir(self.zipdir_edit))
        row.addStretch(1)
        layout.addLayout(row)

        row = QHBoxLayout()
        self._button(row, "📦 Extract ALL zips in folder", self.on_import_zip_folder)
        each = QLabel("(each zip → its own subfolder)")
        each.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(each)
        row.addStretch(1)
        layout.addLayout(row)

        row = QHBoxLayout()
        self.folder_edit = self._inline(row, "Folder:", 260)
        self._button(row, "📂 Browse", lambda: self._pick_dir(self.folder_edit))
        row.addStretch(1)
        layout.addLayout(row)

        row = QHBoxLayout()
        self._button(row, "📁 Copy Folder to Vault", self.on_import_folder)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _index_box(self) -> QGroupBox:
        box = QGroupBox(amp("🔎 Index & Vectorize"))
        layout = QVBoxLayout(box)
        blurb = QLabel("Builds three retrieval layers. Run them in order:\n"
                       "  1. Keyword — fast file walk; required for search.\n"
                       "  2. Descriptions — LLM summaries; better semantics.\n"
                       "  3. Vectors — embedding model; best for fuzzy queries.")
        blurb.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        layout.addWidget(blurb)
        self.index_status = QLabel("")
        self.index_status.setStyleSheet("color: #cba6f7;")
        layout.addWidget(self.index_status)
        row = QHBoxLayout()
        # Kept as attributes because a run disables all three — a second walk
        # over the same vault while the first is going is not a second result,
        # it is two threads writing one index.
        self._btn_keyword = self._button(row, "1. Build Keyword Index",
                                         self.on_keyword_index)
        self._btn_descriptions = self._button(row, "2. Build Descriptions",
                                              self.on_descriptions)
        self._btn_embeddings = self._button(row, "3. Build Vector Embeddings",
                                            self.on_embeddings)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _mongo_box(self) -> QGroupBox:
        box = QGroupBox("🍃 Convert Mongo BSON / JSON")
        layout = QVBoxLayout(box)
        blurb = QLabel("Flattens nested Mongo documents into clean columns a "
                       "model can read.\nSource files are only read; output "
                       "lands in data_in/converted_mongo/.")
        blurb.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        layout.addWidget(blurb)

        row = QHBoxLayout()
        self.mongo_edit = self._inline(row, "File:", 240)
        self._button(row, "📂 Browse",
                     lambda: self._pick_file(self.mongo_edit,
                                             "Mongo dumps (*.bson *.json)"))
        row.addStretch(1)
        layout.addLayout(row)

        row = QHBoxLayout()
        self.mongo_csv = QCheckBox("Clean CSV")
        self.mongo_csv.setChecked(True)
        self.mongo_schema = QCheckBox("Schema profile")
        self.mongo_schema.setChecked(True)
        self.mongo_text = QCheckBox("Text digest")
        for widget in (self.mongo_csv, self.mongo_schema, self.mongo_text):
            row.addWidget(widget)
        row.addStretch(1)
        layout.addLayout(row)

        row = QHBoxLayout()
        self._button(row, "🍃 Convert File", self.on_convert_mongo)
        self._button(row, "Convert ALL in vault",
                     lambda: self.on_convert_mongo(scan_all=True))
        self._button(row, "📂 Open Output", self.on_open_converted)
        row.addStretch(1)
        layout.addLayout(row)

        self.mongo_status = QLabel("")
        self.mongo_status.setWordWrap(True)
        self.mongo_status.setStyleSheet("color: #cba6f7;")
        layout.addWidget(self.mongo_status)
        return box

    def _deferred_box(self) -> QGroupBox:
        box = QGroupBox("📋 Deferred tasks")
        layout = QVBoxLayout(box)
        blurb = QLabel("Tasks you sent here from the Council tab "
                       "(⤓ Defer to Vault).")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        layout.addWidget(blurb)
        self.defer_tree = QTreeWidget()
        self.defer_tree.setColumnCount(2)
        self.defer_tree.setHeaderLabels(["Type", "Task"])
        self.defer_tree.setRootIsDecorated(False)
        self.defer_tree.setMaximumHeight(130)
        layout.addWidget(self.defer_tree)
        row = QHBoxLayout()
        self._button(row, "▶ Run", self.on_run_deferred)
        self._button(row, "✓ Done", lambda: self.on_set_deferred("done"))
        self._button(row, "✗ Dismiss", lambda: self.on_set_deferred("dismissed"))
        self._button(row, "⟳ Refresh", self.on_refresh_deferred)
        row.addStretch(1)
        layout.addLayout(row)
        self.defer_status = QLabel("")
        self.defer_status.setWordWrap(True)
        self.defer_status.setStyleSheet("color: #cba6f7;")
        layout.addWidget(self.defer_status)
        return box

    def _collections_box(self) -> QGroupBox:
        box = QGroupBox("📁 Collections (projects)")
        layout = QVBoxLayout(box)
        blurb = QLabel("Group disparate files that belong together. New… can "
                       "propose members from their names and contents; you "
                       "confirm.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        layout.addWidget(blurb)
        self.coll_tree = QTreeWidget()
        self.coll_tree.setColumnCount(2)
        self.coll_tree.setHeaderLabels(["Collection", "Files"])
        self.coll_tree.setRootIsDecorated(False)
        self.coll_tree.setMaximumHeight(110)
        layout.addWidget(self.coll_tree)
        row = QHBoxLayout()
        self._button(row, "➕ New…", self.on_new_collection)
        self._button(row, "✎ Edit", lambda: self.on_new_collection(edit=True))
        self._button(row, "📊 Summarize", self.on_summarize_collection)
        self._button(row, "✗ Delete", self.on_delete_collection)
        row.addStretch(1)
        layout.addLayout(row)
        # The deferred box has one of these and the collections box did not,
        # so "Summarizing…" and its result had nowhere to go but the activity
        # log — a different place from where the user is looking.
        self.coll_status = QLabel("")
        self.coll_status.setWordWrap(True)
        self.coll_status.setStyleSheet("color: #cba6f7;")
        layout.addWidget(self.coll_status)
        return box

    def _stats_box(self) -> QGroupBox:
        box = QGroupBox("📊 Data stats (precomputed)")
        row = QHBoxLayout(box)
        self._button(row, "🧮 Build / update stats", self.on_build_stats)
        note = QLabel("min/max/mean/… per column, cached")
        note.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(note)
        row.addStretch(1)
        return box

    def _tree_box(self) -> QGroupBox:
        box = QGroupBox("Vault Contents")
        layout = QVBoxLayout(box)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Name", "Size", "Type"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self.on_tree_select)
        self.tree.itemExpanded.connect(self._on_expand)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self._button(row, "👁 Preview", self.on_preview)
        self._button(row, "🗑 Delete Item", self.on_delete)
        self._button(row, "📋 Copy Path", self.on_copy_path)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _build_right(self) -> QWidget:
        split = QSplitter(Qt.Orientation.Vertical)

        preview_box = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_box)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        preview_layout.addWidget(self.preview)
        split.addWidget(preview_box)

        log_box = QGroupBox("Activity Log")
        log_layout = QVBoxLayout(log_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        log_layout.addWidget(self.log)
        split.addWidget(log_box)

        split.setSizes([420, 220])
        return split

    # -- small builders ---------------------------------------------------
    def _inline(self, layout, label: str, width: int) -> QLineEdit:
        layout.addWidget(QLabel(label))
        edit = QLineEdit()
        edit.setFixedWidth(width)
        layout.addWidget(edit)
        return edit

    def _labelled_row(self, layout, label: str, width: int) -> QLineEdit:
        row = QHBoxLayout()
        edit = self._inline(row, label, width)
        row.addStretch(1)
        layout.addLayout(row)
        return edit

    def _pick_file(self, edit: QLineEdit, filters: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file", "",
                                              f"{filters};;All files (*.*)")
        if path:
            edit.setText(path)

    def _pick_dir(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder")
        if path:
            edit.setText(path)

    # ------------------------------------------------------------- logging
    def append(self, message: str, level: str = "info") -> None:
        """The activity log. The Tk tab's three levels, plus a dim one
        for the detail under a result."""
        colour = {"ok": self._tokens["success"],
                  "err": self._tokens["error"],
                  "dim": self._tokens["muted_fg"]}.get(level,
                                                       self._tokens["accent"])
        safe = (str(message).rstrip().replace("&", "&amp;")
                .replace("<", "&lt;").replace(">", "&gt;"))
        self.log.appendHtml(f'<span style="color:{colour}">{safe}</span>')

    def _run(self, what: str, call: Callable) -> None:
        """Call an action, reporting failure in the log rather than a traceback.

        Every operational button goes through here, which is also what makes the
        not-yet-extracted ones honest: they say so, in the log, where the user is
        already looking."""
        try:
            call()
        except VaultActions.NotYetExtracted as exc:
            self.append(f"{what}: {exc}", "err")
        except Exception as exc:                        # noqa: BLE001
            self.append(f"{what} failed: {exc!r}", "err")

    # ------------------------------------------------------------ the tree
    def refresh_tree(self) -> None:
        self.tree.clear()
        root = self.actions.vault_dir
        if not root.exists():
            self.append(f"vault folder does not exist: {root}", "err")
            return
        self._fill(None, root)
        self.append(f"vault listed: {root}")

    def _fill(self, parent: Optional[QTreeWidgetItem], folder: Path) -> None:
        for path, is_dir, size in self.actions.walk(folder):
            item = QTreeWidgetItem([path.name,
                                    "" if is_dir else _human(size),
                                    "folder" if is_dir else
                                    (path.suffix.lstrip(".") or "file")])
            item.setData(0, Qt.ItemDataRole.UserRole, str(path))
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            if is_dir:
                # A placeholder child makes the expander appear without walking
                # the whole vault up front — the Tk tab walks two levels eagerly
                # and stalls on a large vault.
                item.addChild(QTreeWidgetItem(["…"]))

    def _on_expand(self, item: QTreeWidgetItem) -> None:
        if item.childCount() == 1 and item.child(0).text(0) == "…":
            item.takeChildren()
            path = item.data(0, Qt.ItemDataRole.UserRole)
            if path:
                self._fill(item, Path(path))

    def selected_path(self) -> Optional[Path]:
        items = self.tree.selectedItems()
        if not items:
            return None
        path = items[0].data(0, Qt.ItemDataRole.UserRole)
        return Path(path) if path else None

    # ----------------------------------------------------------- handlers
    def on_tree_select(self) -> None:
        path = self.selected_path()
        if path is not None:
            self.preview.setPlainText(self.actions.preview(path))

    def on_preview(self) -> None:
        path = self.selected_path()
        if path is None:
            self.append("select a file to preview", "err")
            return
        self.preview.setPlainText(self.actions.preview(path))

    def on_copy_path(self) -> None:
        path = self.selected_path()
        if path is None:
            self.append("select a file first", "err")
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(str(path))
        self.append(f"copied: {path}", "ok")

    def on_open_folder(self) -> None:
        self._run("Open folder", lambda: self.actions.open_folder())

    def on_search(self) -> None:
        term = self.search_edit.text().strip()
        if not term:
            return
        self.append(f"searching for {term!r} …")

        def work() -> None:
            result = self.actions.search(term)

            def show() -> None:
                if not result.hits:
                    self.append(result.message, "err")
                    return
                self.append(result.message, "ok")
                for path, reason in result.hits[:50]:
                    # The reason is most of the answer when the match came from
                    # inside a spreadsheet rather than from the file's name.
                    self.append(f"   {Path(path).name}   —   {reason}")
                    self.append(f"      {path}", "dim")
                if len(result.hits) > 50:
                    self.append(f"   … and {len(result.hits) - 50} more")

            self._to_ui(show)

        threading.Thread(target=work, name="vault-search", daemon=True).start()

    def on_delete(self) -> None:
        path = self.selected_path()
        if path is None:
            self.append("select an item to delete", "err")
            return
        from .. import dialogs
        if not dialogs.askyesno("Delete", f"Delete {path.name}?", parent=self):
            return
        self._run("Delete", lambda: self.actions.delete(path))

    # -- operational buttons, all still behind the extraction line --------
    def on_clone(self) -> None:
        """Clone, on a worker, reporting through the bridge.

        Validation happens on the UI thread because it is instant and its
        answer is a message, not an operation — the same order the Tk shell
        uses, and the same wording, because both read it from vault_ops."""
        from council_core import vault_ops

        url = self.url_edit.text()
        problem = vault_ops.check_clone_url(url)
        if problem:
            self.append(problem, "err")
            return
        self.append(f"Cloning {url} …")

        def work() -> None:
            result = self.actions.clone(
                url, self.subfolder_edit.text(), self.branch_edit.text(),
                log=lambda m: self._to_ui(lambda m=m: self.append(m)))
            self._to_ui(lambda: self._finish_repo(result))

        threading.Thread(target=work, name="vault-clone", daemon=True).start()

    def on_pull(self) -> None:
        """Pull the repo whose folder is selected in the tree.

        WHICH folder is a view question, so it is answered here; the pull
        itself is not, so it is not."""
        path = self.selected_path()
        if path is None:
            self.append("✗ Select a repo folder in the tree first.", "err")
            return
        subfolder = (path if path.is_dir() else path.parent).name
        self.append(f"Pulling updates for {subfolder} …")

        def work() -> None:
            result = self.actions.pull(
                subfolder, log=lambda m: self._to_ui(lambda m=m: self.append(m)))
            self._to_ui(lambda: self._finish_repo(result))

        threading.Thread(target=work, name="vault-pull", daemon=True).start()

    def _finish_repo(self, result) -> None:
        self.append(result.message, "ok" if result.ok else "err")
        if result.ok:
            self.refresh_tree()

    def on_import_zip(self) -> None:
        from council_core import vault_import
        path = self.zip_edit.text()
        problem = vault_import.check_zip(path)
        if problem:
            self.append(problem, "err")
            return
        sub = self.zip_subfolder_edit.text().strip() or Path(path.strip()).stem
        self.append(f"Extracting {Path(path.strip()).name} → vault/{sub} …")
        self._import(lambda log: self.actions.import_zip(
            path, self.zip_subfolder_edit.text(), log=log),
            clear=(self.zip_edit, self.zip_subfolder_edit))

    def on_import_zip_folder(self) -> None:
        from council_core import vault_import
        folder = self.zipdir_edit.text()
        problem = vault_import.check_folder(folder, what="zips")
        if problem:
            self.append(problem, "err")
            return
        self._import(lambda log: self.actions.import_zip_folder(folder, log=log),
                     clear=(self.zipdir_edit,))

    def on_import_folder(self) -> None:
        from council_core import vault_import
        folder = self.folder_edit.text()
        problem = vault_import.check_folder(folder)
        if problem:
            self.append(problem, "err")
            return
        src = Path(folder.strip())
        self.append(f"Copying {src.name} → vault/{src.name} …")
        self._import(lambda log: self.actions.import_folder(folder, log=log),
                     clear=(self.folder_edit,))

    def _import(self, run, clear=()) -> None:
        """The shape all three imports share: run on a worker, log every line
        as it arrives, then refresh and blank the fields the import consumed.

        The clearing happens HERE, on the UI thread. The Tk handlers used to do
        it from inside the worker — a variable written off the UI thread, which
        is undefined behaviour in both toolkits and silent in Qt."""
        def work() -> None:
            result = run(lambda m: self._to_ui(lambda m=m: self.append(m)))
            self._to_ui(lambda: self._finish_import(result, clear))

        threading.Thread(target=work, name="vault-import", daemon=True).start()

    def _finish_import(self, result, clear) -> None:
        self.append(result.message, "ok" if result.ok else "err")
        if result.ok:
            for edit in clear:
                edit.clear()
            self.refresh_tree()

    def on_keyword_index(self) -> None:
        """The first extracted operation, end to end.

        The worker calls the shared function; every update comes back through
        the bridge. Note what this method does NOT contain: any knowledge of
        what indexing is, or how to word the result. Both live in
        council_core.vault_ops, where the Tk shell reads them too."""
        from council_core import vault_ops

        self.index_status.setText("Walking vault…")
        self.append("keyword index: walking the vault …")
        self._set_index_buttons(False)

        def work() -> None:
            def on_progress(done: int, total: int, name: str) -> None:
                line = vault_ops.progress_line(done, total, name)
                self._to_ui(lambda: self.index_status.setText(line))

            try:
                result = self.actions.build_keyword_index(on_progress=on_progress)
            except VaultActions.NotYetExtracted as exc:
                self._to_ui(lambda: self._finish_index(
                    vault_ops.IndexResult(False, str(exc))))
                return
            self._to_ui(lambda: self._finish_index(result))

        threading.Thread(target=work, name="keyword-index", daemon=True).start()

    def _finish_index(self, result) -> None:
        self.index_status.setText(result.message)
        self.append(result.message, "ok" if result.ok else "err")
        self._set_index_buttons(True)
        if result.ok:
            self.refresh_tree()

    def _set_index_buttons(self, enabled: bool) -> None:
        """A second click while the walk is running would start a second walk
        over the same vault."""
        for button in (self._btn_keyword, self._btn_descriptions,
                       self._btn_embeddings):
            button.setEnabled(enabled)

    def on_descriptions(self) -> None:
        from council_core import vault_ops
        self._index_run(
            self.actions.starting_descriptions(),
            lambda p: self.actions.build_descriptions(on_progress=p),
            lambda i, total: vault_ops.describing_line(i, total),
            every=3)

    def on_embeddings(self) -> None:
        from council_core import vault_ops
        self._index_run(
            self.actions.starting_embeddings(),
            lambda p: self.actions.build_embeddings(on_progress=p),
            lambda i, total: vault_ops.embedding_line(i, total),
            every=10)

    def _index_run(self, start, run, line_for, every: int) -> None:
        """The shape all three index layers share: say what is about to happen,
        stop if there is nothing to do, then run on a worker and report.

        The throttle (`every`) is the one thing that differs between them —
        descriptions tick every 3 files, embeddings every 10 — because each
        file costs seconds rather than milliseconds."""
        self.index_status.setText(start.message)
        self.append(start.message, "ok" if start.ok else "err")
        if not start.ok or start.total == 0:
            return
        self._set_index_buttons(False)

        def work() -> None:
            def on_progress(i, total, name) -> None:
                if i % every == 0 or i == total:
                    line = line_for(i, total)
                    self._to_ui(lambda: self.index_status.setText(line))

            result = run(on_progress)
            self._to_ui(lambda: self._finish_index(result))

        threading.Thread(target=work, name="vault-index", daemon=True).start()

    def on_open_converted(self) -> None:
        self._run("Open output", lambda: self.actions.open_folder(
            self.actions.vault_dir / "data_in" / "converted_mongo"))

    def on_build_stats(self) -> None:
        self._run("Data stats", self.actions.build_stats)

    # -- deferred tasks ---------------------------------------------------
    def on_refresh_deferred(self) -> None:
        result = self.actions.pending_tasks()
        self.defer_status.setText(result.message)
        if not result.ok:
            return
        self.defer_tree.clear()
        self._defer_ids = []
        for row, task_id in zip(result.rows, result.ids):
            QTreeWidgetItem(self.defer_tree, list(row))
            self._defer_ids.append(task_id)

    def _selected_task(self):
        index = self.defer_tree.indexOfTopLevelItem(self.defer_tree.currentItem())
        ids = getattr(self, "_defer_ids", [])
        return ids[index] if 0 <= index < len(ids) else None

    def on_set_deferred(self, state: str) -> None:
        result = self.actions.set_task_status(self._selected_task(), state)
        self.defer_status.setText(result.message)
        if result.ok:
            self.on_refresh_deferred()

    def on_run_deferred(self) -> None:
        """Run the selected deferred task and file the result.

        This used to say it needed the Council tab. It does not: the run is
        pandas over the vault's data files and makes no model call at all.
        See council_core.vault_jobs.
        """
        from council_core import vault_jobs

        task_id = self._selected_task()
        task, problem = vault_jobs.check_deferred_runnable(
            self.actions.vault_dir, task_id)
        if problem:
            self.defer_status.setText(problem)
            return
        self.defer_status.setText("Running…")

        def work() -> None:
            result = self.actions.run_deferred(task_id)

            def show() -> None:
                # Refresh FIRST — it resets the status line — then say what
                # happened, so the message is not immediately overwritten.
                self.on_refresh_deferred()
                self.defer_status.setText(result.message)
                self.append(result.message, "ok" if result.ok else "err")
                if result.ok:
                    self.refresh_tree()

            self._to_ui(show)

        threading.Thread(target=work, name="vault-deferred",
                         daemon=True).start()

    # -- collections ------------------------------------------------------
    def on_refresh_collections(self) -> None:
        result = self.actions.all_collections()
        if not result.ok:
            self.append(result.message, "err")
            return
        self.coll_tree.clear()
        self._coll_names = []
        for row, name in zip(result.rows, result.ids):
            QTreeWidgetItem(self.coll_tree, list(row))
            self._coll_names.append(name)

    def _selected_collection(self):
        index = self.coll_tree.indexOfTopLevelItem(self.coll_tree.currentItem())
        names = getattr(self, "_coll_names", [])
        return names[index] if 0 <= index < len(names) else None

    def on_delete_collection(self) -> None:
        from council_core import vault_data

        from .. import dialogs
        name = self._selected_collection()
        if not name:
            self.append("Select a collection first.", "err")
            return
        if not dialogs.askyesno("Delete collection",
                                vault_data.confirm_delete_collection(name),
                                parent=self):
            return
        result = self.actions.delete_collection(name)
        self.append(result.message, "ok" if result.ok else "err")
        if result.ok:
            self.on_refresh_collections()

    def on_new_collection(self, edit: bool = False) -> None:
        """Create or edit a collection, with a Discover that proposes members.

        Discover is deterministic scoring over filenames and the data index —
        no model, despite what this method used to claim.
        """
        from .collection_dialog import CollectionDialog

        existing = self._selected_collection() if edit else None
        if edit and not existing:
            self.append("Select a collection to edit first.", "err")
            return
        dialog = CollectionDialog(self.actions, existing=existing, parent=self)
        if dialog.exec():
            self.coll_status.setText(dialog.result_message)
            self.on_refresh_collections()

    def on_summarize_collection(self) -> None:
        """Profile every file in the collection into one derived CSV."""
        name = self._selected_collection()
        if not name:
            self.append("Select a collection first.", "err")
            return
        self.coll_status.setText("Summarizing…")

        def work() -> None:
            result = self.actions.summarize_collection(name)

            def show() -> None:
                self.coll_status.setText(result.message)
                self.append(result.message, "ok" if result.ok else "err")
                if result.ok:
                    self.refresh_tree()

            self._to_ui(show)

        threading.Thread(target=work, name="vault-collection-summary",
                         daemon=True).start()

    # -- RAG misses -------------------------------------------------------
    def on_rag_misses(self) -> None:
        """What the vault does not cover. Shown in the preview pane rather than
        a popup: it is a list to read against the tree, not a modal."""
        result = self.actions.read_misses()
        if not result.ok:
            self.append(result.message, "err")
            return
        lines = [result.message, ""]
        lines += [f"{stamp}  {query}" for stamp, query in result.rows]
        self.preview.setPlainText("\n".join(lines))
        self.append(f"RAG misses: {len(result.rows)} recorded")

    # -- Mongo conversion -------------------------------------------------
    def on_convert_mongo(self, scan_all: bool = False) -> None:
        from council_core import vault_data
        problem = vault_data.check_mongo_request(
            self.mongo_edit.text(), self.mongo_csv.isChecked(),
            self.mongo_schema.isChecked(), self.mongo_text.isChecked(),
            scan_all)
        if problem:
            self.mongo_status.setText(problem)
            return
        self.mongo_status.setText("Converting…")

        def work() -> None:
            def on_progress(done, total, name):
                line = vault_data.converting_line(done, total, name)
                self._to_ui(lambda: self.mongo_status.setText(line))

            result = self.actions.convert_mongo(
                self.mongo_edit.text(), self.mongo_csv.isChecked(),
                self.mongo_schema.isChecked(), self.mongo_text.isChecked(),
                scan_all=scan_all, on_progress=on_progress)
            self._to_ui(lambda: self._finish_mongo(result))

        threading.Thread(target=work, name="mongo-convert", daemon=True).start()

    def _finish_mongo(self, result) -> None:
        self.mongo_status.setText(result.message)
        self.append(result.message, "ok" if result.ok else "err")
        if result.ok:
            self.refresh_tree()


def _default_vault_dir() -> Path:
    """Where the vault is, without importing the engine.

    council_gui_engine sets VAULT_DIR at import, and importing it costs ~4
    seconds and builds the backend banner. `council_core.paths` asks it if it
    is already loaded and otherwise derives the same answer — including the
    two environment variables, which this function's own hard-coded default
    silently ignored.
    """
    from council_core import paths
    return paths.vault_dir()


def build_vault(window) -> QWidget:
    """Factory for the tab registry."""
    return VaultTab(window)
