"""
council_qt.tabs.librarian — the vault root: list, preview, commit, open.

ADVANCED MODE ONLY, as in Tk. The Tk tab is built inside `if _ADVANCED_MODE:`,
and porting it unconditionally would put a "commit the entire vault to git"
button in front of every user — a behaviour change dressed as a port. The
registry asks `council_core.modes.advanced()`.

FOUR DIFFERENCES FROM THE TK TAB, ALL OF THEM DEFECTS IT HAS
- The list carries real Paths. Tk lists `p.name` and looks the file back up
  through `safe_name()`, which rewrites spaces to underscores — so every vault
  file with a space in its name is unopenable from the tab that lists it.
- Preview is capped at 64 KB, in ONE reused dialog. Tk reads the whole file and
  opens a fresh Toplevel per double-click, keeping no reference to any of them.
- The commit runs on a worker. Tk runs three git calls back to back on the UI
  thread with no timeouts, over a vault holding .chromadb and every cloned
  reference repo.
- "Nothing to commit" is reported as the success it is.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QVBoxLayout,
                               QWidget)

from council_core import librarian, paths

from .. import theme
from ..view import ViewHelpers, amp


class LibrarianActions:
    """What the Librarian tab can ask the application to do."""

    def __init__(self, vault_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()

    def entries(self) -> List[Path]:
        return librarian.entries(self.vault_dir)

    def preview(self, path: Path) -> str:
        return librarian.preview(path)

    def commit(self, message: str):
        return librarian.commit_all(self.vault_dir, message)

    def open_folder(self) -> None:
        """Reused from the Vault tab rather than written twice.

        Tk has two copies of this (Librarian and Vault Health) and they have
        already drifted: one wraps the call in try/except and shows an error,
        the other does not.
        """
        from .vault import VaultActions
        VaultActions(self.vault_dir).open_folder()


class LibrarianTab(ViewHelpers, QWidget):
    """A file list, a preview dialog, and a commit button."""

    def __init__(self, window=None, actions: Optional[LibrarianActions] = None,
                 ask_text=None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or LibrarianActions()
        self._tokens = theme.tokens("dark")
        self._paths: List[Path] = []
        self._preview_dialog: Optional[QDialog] = None
        self._busy = False
        self.ask_text = ask_text or (lambda *a, **k: None)

        self._build()
        self.refresh()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        blurb = QLabel(
            "The loose files at the top of your vault. Double-click one to "
            "read it. Commit saves the whole vault to git — including "
            "everything the app has written into it.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(blurb)

        row = QHBoxLayout()
        self._button(row, amp("⟳ Refresh"), self.refresh)
        self.commit_btn = self._button(row, amp("Commit to Git"),
                                       self.on_commit)
        self._button(row, amp("Open vault folder"), self.on_open_folder)
        row.addStretch(1)
        outer.addLayout(row)

        self.files = QListWidget()
        self.files.itemDoubleClicked.connect(self._preview_item)
        outer.addWidget(self.files, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Re-read the vault root."""
        self._paths = self.actions.entries()
        self.files.clear()
        for path in self._paths:
            item = QListWidgetItem(path.name)
            # The PATH travels with the row. Recovering it from the row's text
            # is the defect this tab exists to stop repeating.
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.files.addItem(item)
        self.status.setText(f"{len(self._paths)} file(s) in "
                            f"{self.actions.vault_dir}")

    def selected_path(self) -> Optional[Path]:
        item = self.files.currentItem()
        if item is None:
            return None
        stored = item.data(Qt.ItemDataRole.UserRole)
        return Path(stored) if stored else None

    # ------------------------------------------------------------------
    def _preview_item(self, item: QListWidgetItem) -> None:
        stored = item.data(Qt.ItemDataRole.UserRole)
        if stored:
            self.show_preview(Path(stored))

    def show_preview(self, path: Path) -> None:
        """One dialog, reused.

        Tk opens a new Toplevel per double-click and keeps no reference, so ten
        previews leave ten 800x600 windows on screen for the life of the
        session, each holding a full copy of a file.
        """
        if self._preview_dialog is None:
            self._preview_dialog = QDialog(self)
            layout = QVBoxLayout(self._preview_dialog)
            self._preview_text = QPlainTextEdit()
            self._preview_text.setReadOnly(True)
            self._preview_text.setLineWrapMode(QPlainTextEdit.NoWrap)
            layout.addWidget(self._preview_text)
            self._preview_dialog.resize(800, 600)
        self._preview_dialog.setWindowTitle(path.name)
        self._preview_text.setPlainText(self.actions.preview(path))
        self._preview_dialog.show()
        self._preview_dialog.raise_()

    # ------------------------------------------------------------------
    def on_open_folder(self) -> None:
        try:
            self.actions.open_folder()
        except Exception as exc:                          # noqa: BLE001
            # The Qt VaultActions.open_folder has no try/except of its own, so
            # a missing vault raises straight out of the button handler.
            self.status.setText(f"Could not open the vault folder: {exc}")

    def on_commit(self) -> None:
        if self._busy:
            self.status.setText("A commit is already running.")
            return
        message = self.ask_text("Commit vault", "Commit message:",
                                "vault snapshot")
        if not message:
            return
        self._busy = True
        self.commit_btn.setEnabled(False)
        self.status.setText("Committing…")

        def work() -> None:
            result = self.actions.commit(message)

            def show() -> None:
                self._busy = False
                self.commit_btn.setEnabled(True)
                self.status.setText(librarian.describe(result))
                self._report(result)

            self._to_ui(show)

        threading.Thread(target=work, name="librarian-commit",
                         daemon=True).start()

    def _report(self, result) -> None:
        """Mirror the outcome into the Council transcript, as Tk does.

        The git output goes through on SUCCESS too — it carries the SHA and the
        files-changed count, which is the receipt.
        """
        append = getattr(self.window, "append_transcript", None)
        if append is None:
            return
        try:
            append("Librarian",
                   f"{'OK' if result.ok else 'FAIL'}: "
                   f"{librarian.describe(result)}", "final")
        except Exception:                                 # noqa: BLE001
            pass


def build_librarian(window) -> QWidget:
    """Factory for the tab registry."""
    return LibrarianTab(window)
