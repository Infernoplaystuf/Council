"""
council_qt.tabs.models — which models fit this machine.

THE CONSTRUCTOR DOES NOT PROBE THE HARDWARE
`hardware_detect.detect()` shells out to nvidia-smi and falls back to importing
torch. Measured on this machine with the project interpreter: 5.85 SECONDS.

The Tk build gets away with calling it inline in `_build_model_finder_tab`
because every tab is constructed up front behind the splash. Qt builds tabs
lazily, so the same call here would be nearly six seconds of frozen window the
first time the user clicks the tab — with no splash to hide behind and no
indication anything is happening.

So the tab opens saying "detecting…", starts a worker, and fills in the line
and the first ranking when it answers.

EVERY CAPTION GOES THROUGH amp(), INCLUDING THE TWO RE-CAPTIONS
Three of this tab's captions contain "&", and two of them are applied at RUN
time as the upgrade banner changes. A port that escapes only the strings
present at build time fixes one of the three and looks finished.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional

from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from council_core import model_jobs
from council_core import paths

from .. import dialogs, theme
from ..view import ViewHelpers, amp


class ModelsActions:
    """What the Models tab can ask the application to do."""

    def __init__(self, vault_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()

    def detect(self):
        return model_jobs.detect_hardware()

    def find(self, hardware, task="", online=False):
        return model_jobs.find(hardware, task=task, online=online)

    def upgrade(self, hardware):
        return model_jobs.upgrade_banner(hardware)

    def download(self, row, *, on_progress=None, should_cancel=None):
        return model_jobs.download_and_switch(
            row, self.vault_dir, on_progress=on_progress,
            should_cancel=should_cancel)


class ModelsTab(ViewHelpers, QWidget):
    """Hardware, a ranked list, and one download."""

    def __init__(self, window=None, actions: Optional[ModelsActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or ModelsActions()
        self._tokens = theme.tokens("dark")
        self._hardware = None
        self._rows: List[model_jobs.ModelRow] = []
        self._upgrade = None
        self._cancel = False

        self._build()
        self.detect_hardware()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        self.hardware = QLabel(model_jobs.PENDING_HARDWARE)
        self.hardware.setStyleSheet(
            f"color: {self._tokens['accent']}; font-weight: bold;")
        outer.addWidget(self.hardware)

        note = QLabel(model_jobs.CATALOG_NOTE)
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(note)

        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.banner)

        # Built with amp(): the caption carries an ampersand, and so do the
        # two re-captions this button receives later.
        upgrade_row = QHBoxLayout()
        self.upgrade_btn = self._button(upgrade_row,
                                        model_jobs.DOWNLOAD_IDLE,
                                        self.on_download)
        self.upgrade_btn.setEnabled(False)
        upgrade_row.addStretch(1)
        outer.addLayout(upgrade_row)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Task:"))
        self.task = QLineEdit()
        self.task.setPlaceholderText("e.g. coding, long documents")
        self.task.setFixedWidth(240)
        controls.addWidget(self.task)
        self.online = QCheckBox("Also search Hugging Face (needs internet)")
        controls.addWidget(self.online)
        self.find_btn = self._button(controls, "🔎 Find Models", self.on_find)
        self._button(controls, "⬆ Suggest upgrades", self.on_suggest)
        controls.addStretch(1)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        controls.addWidget(self.status)
        outer.addLayout(controls)

        self.results = QTreeWidget()
        self.results.setColumnCount(len(model_jobs.COLUMNS))
        self.results.setHeaderLabels([heading
                                      for _key, heading in model_jobs.COLUMNS])
        self.results.setRootIsDecorated(False)
        self.results.header().setSectionResizeMode(0, QHeaderView.Stretch)
        outer.addWidget(self.results, 1)

        actions = QHBoxLayout()
        self.download_btn = self._button(actions, "⬇ Download & install",
                                         self.on_download_selected)
        self._button(actions, "📋 Copy download info", self.on_copy)
        actions.addStretch(1)
        self.progress = QLabel("")
        self.progress.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        actions.addWidget(self.progress)
        outer.addLayout(actions)

    # ------------------------------------------------------------------
    def detect_hardware(self) -> None:
        """Probe on a worker. Nearly six seconds, measured."""
        def work() -> None:
            hardware = self.actions.detect()

            def show() -> None:
                self._hardware = hardware
                self.hardware.setText(hardware.summary)
                self.on_find()          # the first ranking, now that we can
                self.on_suggest()

            self._to_ui(show)

        threading.Thread(target=work, name="model-detect", daemon=True).start()

    def selected_row(self) -> Optional[model_jobs.ModelRow]:
        index = self.results.indexOfTopLevelItem(self.results.currentItem())
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def on_find(self) -> None:
        if self._hardware is None:
            self.status.setText("Still detecting the hardware…")
            return
        task = self.task.text()
        online = self.online.isChecked()      # read on the GUI thread
        hardware = self._hardware
        self.status.setText("Ranking…")
        self.find_btn.setEnabled(False)

        def work() -> None:
            result = self.actions.find(hardware, task=task, online=online)

            def show() -> None:
                self.find_btn.setEnabled(True)
                self.status.setText(result.message)
                self.results.clear()
                self._rows = list(result.rows)
                for row in self._rows:
                    QTreeWidgetItem(self.results, list(row.cells))

            self._to_ui(show)

        threading.Thread(target=work, name="model-find", daemon=True).start()

    def on_suggest(self) -> None:
        if self._hardware is None:
            return
        hardware = self._hardware

        def work() -> None:
            text, candidate = self.actions.upgrade(hardware)

            def show() -> None:
                self._upgrade = candidate
                self.banner.setText(text)
                name = (candidate or {}).get("name") if candidate else None
                # amp() again — this is one of the two RE-captions a port that
                # escapes only build-time strings will miss.
                self.upgrade_btn.setText(amp(model_jobs.download_caption(name)))
                self.upgrade_btn.setEnabled(bool(candidate))

            self._to_ui(show)

        threading.Thread(target=work, name="model-assess", daemon=True).start()

    def on_download(self) -> None:
        """The upgrade banner's button."""
        if not self._upgrade:
            self.status.setText("There is no upgrade to download.")
            return
        self._download(model_jobs.to_row(self._upgrade))

    def on_download_selected(self) -> None:
        row = self.selected_row()
        if row is None:
            self.status.setText("Select a model in the list first.")
            return
        self._download(row)

    def _download(self, row: model_jobs.ModelRow) -> None:
        size = row.raw.get("size_gb")
        problem = model_jobs.check_space(self.actions.vault_dir, size)
        if problem:
            # Checked BEFORE the download, not discovered at 90%.
            self.status.setText(problem)
            return
        if not dialogs.askyesno(
                "Download model",
                model_jobs.confirm_download_text(row.cells[0], size),
                parent=self):
            return

        self._cancel = False
        self.download_btn.setEnabled(False)
        self.progress.setText("Starting…")

        def work() -> None:
            def progress(done, total=None, name=row.cells[0]) -> None:
                line = model_jobs.progress_line(done, total, name)
                self._to_ui(lambda: self.progress.setText(line))

            result = self.actions.download(
                row, on_progress=progress,
                should_cancel=lambda: self._cancel)

            def show() -> None:
                self.download_btn.setEnabled(True)
                self.progress.setText("")
                self.status.setText(result.message)

            self._to_ui(show)

        threading.Thread(target=work, name="model-download",
                         daemon=True).start()

    def on_copy(self) -> None:
        row = self.selected_row()
        if row is None:
            self.status.setText("Select a model in the list first.")
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(model_jobs.copy_text(row))
        self.status.setText("Copied the repo and file name.")


def build_models(window) -> QWidget:
    """Factory for the tab registry."""
    return ModelsTab(window)
