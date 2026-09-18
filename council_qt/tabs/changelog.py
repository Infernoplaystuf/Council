"""
council_qt.tabs.changelog — the repository's own commits.

Trivial in widgets, and carrying two defects worth not reproducing.

Filtering in the Tk tab re-selects row 0 and immediately runs
`git show --stat --patch` for it, SYNCHRONOUSLY on the GUI thread — so every
keystroke in the filter box spawns a subprocess. Here filtering is a string
test over the list already in memory, and fetching a commit's detail is a
separate worker call made when a selection settles.

And the tab is broken in every installed build: `.git` is not bundled, so
there is nothing for it to read. It says that plainly instead of showing git's
message about ownership and safe directories.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QPlainTextEdit, QSplitter,
                               QVBoxLayout, QWidget)

from council_core import changelog as changelog_core

from .. import theme
from ..view import ViewHelpers


class ChangelogActions:
    def __init__(self, repo: Optional[Path] = None):
        self.repo = Path(repo or Path(__file__).resolve().parents[2])

    def history(self):
        return changelog_core.history(self.repo)

    def detail(self, sha: str) -> str:
        return changelog_core.detail(self.repo, sha)

    def open_repo(self) -> None:
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(self.repo)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.repo)])
            else:
                subprocess.Popen(["xdg-open", str(self.repo)])
        except Exception:                                 # noqa: BLE001
            pass


class ChangelogTab(ViewHelpers, QWidget):
    def __init__(self, window=None, actions: Optional[ChangelogActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or ChangelogActions()
        self._tokens = theme.tokens("dark")
        self._all: List[changelog_core.Commit] = []
        self._shown: List[changelog_core.Commit] = []
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        row = QHBoxLayout()
        self._button(row, "⟳ Refresh", self.on_refresh)
        self._button(row, "📂 Open repo folder", self.actions.open_repo)
        row.addWidget(QLabel("Filter:"))
        self.filter = QLineEdit()
        self.filter.setFixedWidth(240)
        self.filter.setPlaceholderText("subject, date or sha")
        # Filtering touches the list already in memory. The Tk version spawns
        # `git show` on every keystroke.
        self.filter.textChanged.connect(self.apply_filter)
        row.addWidget(self.filter)
        row.addStretch(1)
        outer.addLayout(row)

        self.status = QLabel("Click ⟳ Refresh to load commits.")
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.status)

        split = QSplitter(Qt.Orientation.Horizontal)

        left = QGroupBox("Commits (newest first)")
        left_layout = QVBoxLayout(left)
        self.commits = QListWidget()
        self.commits.currentRowChanged.connect(self.on_selected)
        left_layout.addWidget(self.commits)
        split.addWidget(left)

        right = QGroupBox("Commit detail")
        right_layout = QVBoxLayout(right)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setLineWrapMode(QPlainTextEdit.NoWrap)
        right_layout.addWidget(self.detail)
        split.addWidget(right)

        split.setSizes([420, 560])
        outer.addWidget(split, 1)

    # ------------------------------------------------------------------
    def on_refresh(self) -> None:
        self.status.setText("Reading the repository…")

        def work() -> None:
            result = self.actions.history()

            def show() -> None:
                self._all = list(result.commits)
                self.status.setText(result.message)
                self.apply_filter()

            self._to_ui(show)

        threading.Thread(target=work, name="changelog", daemon=True).start()

    def apply_filter(self) -> None:
        """Narrow the loaded list. No subprocess, no disk, no git."""
        self._shown = changelog_core.filter_commits(self._all,
                                                    self.filter.text())
        self.commits.clear()
        for commit in self._shown:
            self.commits.addItem(commit.label)

    def on_selected(self, row: int) -> None:
        if not (0 <= row < len(self._shown)):
            return
        sha = self._shown[row].sha
        self.detail.setPlainText("Reading…")

        def work() -> None:
            text = self.actions.detail(sha)
            self._to_ui(lambda: self.detail.setPlainText(text))

        threading.Thread(target=work, name="changelog-detail",
                         daemon=True).start()


def build_changelog(window) -> QWidget:
    """Factory for the tab registry."""
    return ChangelogTab(window)
