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

#: How much of a file the preview pane reads. The Tk tab reads the head of the
#: file too — a vault holds multi-GB CSVs and previewing one whole would hang
#: the UI thread for minutes.
PREVIEW_BYTES = 64_000


def amp(text: str) -> str:
    """Escape & so Qt shows it instead of eating it as a mnemonic.

    Measured the first time this tab rendered: the group box titled
    "Index & Vectorize" came out as "Index _Vectorize", because Qt reads & in
    any button, label, tab or group-box title as "underline the next letter".
    Tk has no such rule, so EVERY caption carried across from the Tk shell is a
    candidate — and the failure is silent and cosmetic, which is how it survives
    review.
    """
    return str(text).replace("&", "&&")


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

    def __init__(self, vault_dir: Path):
        self.vault_dir = Path(vault_dir)

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

    def search(self, term: str) -> List[Path]:
        """Find vault files by NAME. The Tk tab also searches indexed CONTENT
        through data_index; that half needs the index and is left to the
        extracted actions."""
        term = (term or "").strip().lower()
        if not term:
            return []
        hits: List[Path] = []
        for base, dirs, names in os.walk(self.vault_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                if term in name.lower():
                    hits.append(Path(base) / name)
                    if len(hits) >= 500:
                        return hits
        return hits

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

    def clone(self, url, subfolder, branch):        self._later("Clone")
    def pull(self):                                 self._later("Pull updates")
    def import_zip(self, path, subfolder):          self._later("Extract zip")
    def import_zip_folder(self, folder):            self._later("Extract all zips")
    def import_folder(self, folder):                self._later("Copy folder")
    def build_keyword_index(self):                  self._later("Keyword index")
    def build_descriptions(self):                   self._later("Descriptions")
    def build_embeddings(self):                     self._later("Embeddings")
    def convert_mongo(self, path, csv, schema, text, scan_all=False):
        self._later("Mongo conversion")
    def build_stats(self):                          self._later("Data stats")
    def deferred(self):                             self._later("Deferred tasks")
    def collections(self):                          self._later("Collections")
    def delete(self, path):                         self._later("Delete item")


class VaultTab(QWidget):
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
        self._button(row, "1. Build Keyword Index", self.on_keyword_index)
        self._button(row, "2. Build Descriptions", self.on_descriptions)
        self._button(row, "3. Build Vector Embeddings", self.on_embeddings)
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
        blurb = QLabel("Group disparate files that belong together. New… lets "
                       "the council propose members; you confirm.")
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
    def _button(self, layout, text: str, slot: Callable) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(slot)
        layout.addWidget(button)
        return button

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
        """The activity log. Same three levels the Tk tab tags."""
        colour = {"ok": self._tokens["success"],
                  "err": self._tokens["error"]}.get(level, self._tokens["accent"])
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
            hits = self.actions.search(term)

            def show() -> None:
                if not hits:
                    self.append(f"no file name matches {term!r}", "err")
                    return
                self.append(f"{len(hits)} match(es):", "ok")
                for hit in hits[:50]:
                    self.append(f"   {hit}")
                if len(hits) > 50:
                    self.append(f"   … and {len(hits) - 50} more")

            self._to_ui(show)

        threading.Thread(target=work, name="vault-search", daemon=True).start()

    def _to_ui(self, fn: Callable) -> None:
        """Hand a result back to the GUI thread.

        Always through the bridge when there is one: a worker touching a widget
        is undefined behaviour in Qt exactly as it is in Tk, and Qt will not
        warn."""
        if self.bridge is not None:
            self.bridge.call_on_ui(fn)
        else:
            fn()

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
        self._run("Clone", lambda: self.actions.clone(
            self.url_edit.text(), self.subfolder_edit.text(),
            self.branch_edit.text()))

    def on_pull(self) -> None:
        self._run("Pull", self.actions.pull)

    def on_import_zip(self) -> None:
        self._run("Extract zip", lambda: self.actions.import_zip(
            self.zip_edit.text(), self.zip_subfolder_edit.text()))

    def on_import_zip_folder(self) -> None:
        self._run("Extract all zips",
                  lambda: self.actions.import_zip_folder(self.zipdir_edit.text()))

    def on_import_folder(self) -> None:
        self._run("Copy folder",
                  lambda: self.actions.import_folder(self.folder_edit.text()))

    def on_keyword_index(self) -> None:
        self._run("Keyword index", self.actions.build_keyword_index)

    def on_descriptions(self) -> None:
        self._run("Descriptions", self.actions.build_descriptions)

    def on_embeddings(self) -> None:
        self._run("Embeddings", self.actions.build_embeddings)

    def on_convert_mongo(self, scan_all: bool = False) -> None:
        self._run("Convert Mongo", lambda: self.actions.convert_mongo(
            self.mongo_edit.text(), self.mongo_csv.isChecked(),
            self.mongo_schema.isChecked(), self.mongo_text.isChecked(),
            scan_all=scan_all))

    def on_open_converted(self) -> None:
        self._run("Open output", lambda: self.actions.open_folder(
            self.actions.vault_dir / "data_in" / "converted_mongo"))

    def on_build_stats(self) -> None:
        self._run("Data stats", self.actions.build_stats)

    def on_refresh_deferred(self) -> None:
        self._run("Deferred tasks", self.actions.deferred)

    def on_run_deferred(self) -> None:
        self._run("Run deferred", self.actions.deferred)

    def on_set_deferred(self, state: str) -> None:
        self._run(f"Mark {state}", self.actions.deferred)

    def on_new_collection(self, edit: bool = False) -> None:
        self._run("Collections", self.actions.collections)

    def on_summarize_collection(self) -> None:
        self._run("Summarize collection", self.actions.collections)

    def on_delete_collection(self) -> None:
        self._run("Delete collection", self.actions.collections)

    def on_rag_misses(self) -> None:
        self._run("RAG misses", lambda: self.actions._later("RAG misses"))


def _default_vault_dir() -> Path:
    """Where the vault is, without importing the engine.

    council_gui_engine sets VAULT_DIR at import, and importing it costs ~4
    seconds and builds the backend banner. If it is already loaded, use its
    answer; otherwise use the documented default.
    """
    engine = sys.modules.get("council_gui_engine")
    vault = getattr(engine, "VAULT_DIR", None) if engine else None
    return Path(str(vault)) if vault else Path.home() / ".council" / "vault"


def build_vault(window) -> QWidget:
    """Factory for the tab registry."""
    return VaultTab(window)
