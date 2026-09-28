"""
council_qt.tabs.dream3d — the Dream3D workbench, ported.

Two panes. Left: a chat that MIRRORS the Council transcript and sends what is
typed through the Council tab, so every pipeline command and every ordinary
question behaves exactly as it does there. Right: the pipelines in
vault/pipelines/in/, a rendering of the selected one, and the DREAM3D-NX jobs —
check env, transpile to Python, run over a folder, write a script from plain
English. Written against remaining_tabs_requirements.md §dream3d; the logic is
in council_core.dream3d, council_core.pipeline_intent and council_core.nx_ops.

DEFECTS DESIGNED OUT
  1  Sending cleared the chat box with Tk's `_set_text`, which re-disables any
     widget not on its allow-list — the box accepted no more typing for the
     rest of the session. Here it is cleared and stays editable.
  2  A refused script was reported as "saved" at a path never written.
  3  The filter catalog never expired. Check env now invalidates it.
     (Both in council_core.nx_ops.)
  4  None of the four DREAM3D-NX buttons was disabled while its subprocess ran,
     so a second click started a second run writing the same files. One flag
     covers all four, and they are disabled for the duration.

Also: the Transformation Cube button looked for its asset under APP_DIR — the
state root, not the install — and could never find it in a source run.
"""
from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit,
                               QSplitter, QVBoxLayout, QWidget)

from council_core import dream3d as dream3d_core
from council_core import nx_ops, paths

from .. import dialogs, theme
from ..view import ViewHelpers, amp
from ..widgets.transcript import TranscriptView

COUNCIL_TITLE = "⚖ Council"


class DreamActions:
    """What the Dream3D tab can ask the application to do. The nx jobs block
    for seconds to minutes; the tab calls them on workers."""

    def __init__(self, vault_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()

    def scan(self):
        return dream3d_core.scan(self.vault_dir)

    def render(self, pipeline) -> str:
        return dream3d_core.render(pipeline)

    def in_dir(self) -> Path:
        return dream3d_core.in_dir(self.vault_dir)

    def out_dir(self) -> Path:
        return dream3d_core.out_dir(self.vault_dir)

    def input_dir(self) -> Path:
        import data_index
        return Path(data_index.input_dir(self.vault_dir))

    def check_env(self) -> nx_ops.NxResult:
        return nx_ops.check_env(self.vault_dir)

    def transpile(self, pipeline) -> nx_ops.NxResult:
        return nx_ops.transpile(pipeline, self.vault_dir)

    def run_folder(self, pipeline, in_dir: Path,
                   pattern: str) -> nx_ops.NxResult:
        return nx_ops.run_folder(pipeline, in_dir, pattern, self.vault_dir)

    def write_script(self, task: str) -> nx_ops.NxResult:
        return nx_ops.write_script(task, self.vault_dir)

    def cube(self) -> Optional[Path]:
        return dream3d_core.transformation_cube()


class Dream3DTab(ViewHelpers, QWidget):
    """Mirrored chat on the left, pipelines and DREAM3D-NX on the right."""

    def __init__(self, window=None, actions: Optional[DreamActions] = None,
                 ask_directory: Callable = dialogs.askdirectory,
                 ask_string: Callable = dialogs.askstring,
                 open_url: Callable[[QUrl], bool] = QDesktopServices.openUrl,
                 auto_refresh: bool = True):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or DreamActions()
        self.ask_directory = ask_directory
        self.ask_string = ask_string
        self.open_url = open_url
        self._tokens = theme.tokens("dark")
        self._pipelines: List = []
        self._busy = False              # the pipeline scan
        self._nx_busy = False           # any DREAM3D-NX job (defect 4)
        self._local_chat = None

        self._build()
        self._attach_to_council()
        if auto_refresh:
            self.refresh()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._chat_side())
        split.addWidget(self._pipeline_side())
        split.setSizes([560, 520])
        outer.addWidget(split, 1)

    def _chat_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Chat (mirrors Council)"))
        self.transcript = TranscriptView()
        layout.addWidget(self.transcript, 1)
        self.input = QPlainTextEdit()
        self.input.setMaximumHeight(80)
        self.input.setPlaceholderText(
            "Ask the Council, or: list pipelines · show pipeline X · "
            "convert X to python")
        layout.addWidget(self.input)
        self._shortcut("Ctrl+Return", self.on_send, self.input)
        row = QHBoxLayout()
        self._button(row, "Send", self.on_send)
        hint = QLabel("(Ctrl+Enter)")
        hint.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(hint)
        row.addStretch(1)
        layout.addLayout(row)
        return panel

    def _pipeline_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Pipelines in vault/pipelines/in/"))
        self.pipelines = QListWidget()
        self.pipelines.setMaximumHeight(170)
        self.pipelines.currentRowChanged.connect(self.on_select)
        layout.addWidget(self.pipelines)

        row = QHBoxLayout()
        self.refresh_btn = self._button(row, "↻ Refresh", self.refresh)
        self._button(row, "Open in/ folder", self.on_open_in)
        self._button(row, "Open out/ folder", self.on_open_out)
        row.addStretch(1)
        layout.addLayout(row)

        tools = QHBoxLayout()
        label = QLabel("Tools:")
        label.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        tools.addWidget(label)
        self._button(tools, "🧊 Transformation Cube…", self.on_cube)
        tools.addStretch(1)
        layout.addLayout(tools)

        nx = QGroupBox(amp("DREAM3D-NX → Python"))
        nx_layout = QVBoxLayout(nx)
        top = QHBoxLayout()
        self.nx_status = QLabel("nx env: not checked")
        self.nx_status.setStyleSheet(f"color: {self._tokens['info']};")
        top.addWidget(self.nx_status, 1)
        self.run_btn = self._button(top, "▶ Run over folder…",
                                    self.on_run_folder)
        self.transpile_btn = self._button(top, "→ Python (selected)",
                                          self.on_transpile)
        self.check_btn = self._button(top, "Check env", self.on_check_env)
        nx_layout.addLayout(top)
        task = QHBoxLayout()
        task.addWidget(QLabel("Task:"))
        self.task = QLineEdit()
        self.task.setPlaceholderText("e.g. read every .dream3d and write an STL")
        self.task.returnPressed.connect(self.on_write_script)
        task.addWidget(self.task, 1)
        self.write_btn = self._button(task, "✍ Write pipeline",
                                      self.on_write_script)
        nx_layout.addLayout(task)
        layout.addWidget(nx)

        layout.addWidget(QLabel("Pipeline visualization"))
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setStyleSheet("font-family: Consolas, monospace;")
        layout.addWidget(self.view, 1)
        return panel

    # -- the Council link ----------------------------------------------
    def council(self):
        tab_for = getattr(self.window, "tab", None)
        return tab_for(COUNCIL_TITLE) if callable(tab_for) else None

    def _attach_to_council(self) -> None:
        council = self.council()
        if council is None:
            return
        if hasattr(council, "mirror"):
            council.mirror.add(self.transcript)
        if hasattr(council, "pipelines_changed"):
            council.pipelines_changed.append(self._guard(self.refresh))

    def say(self, who: str, text: str, kind: str = "final") -> None:
        """Into the shared transcript (the Council's, which mirrors here),
        or into this pane alone when there is no Council tab."""
        council = self.council()
        if council is not None and hasattr(council, "append"):
            council.append(who, text, kind)
        else:
            self.transcript.append_entry(who, text, kind)

    def on_send(self) -> None:
        """Send through the Council tab, as Tk sends through _send."""
        text = self.input.toPlainText().strip()
        if not text:
            return
        # Defect 1: cleared, and still editable afterwards.
        self.input.clear()
        council = self.council()
        if council is not None and hasattr(council, "on_send"):
            council.input.setPlainText(text)
            council.on_send()
            return
        self._send_standalone(text)

    def _send_standalone(self, text: str) -> None:
        """No Council tab (a standalone host): pipeline commands still work."""
        self.transcript.append_entry("User", text, "final")
        if self._local_chat is None:
            self._local_chat = dream3d_core.PipelineChat(
                self.actions.vault_dir,
                say=lambda w, t, k: self._to_ui(
                    self.transcript.append_entry, w, t, k),
                on_changed=lambda: self._to_ui(self.refresh))
        job = self._local_chat.plan(text)
        if job is None:
            self.transcript.append_entry(
                "Council", "The Council is not loaded here, so only pipeline "
                "commands are answered in this window.", "observation")
            return
        def work() -> None:
            try:
                job()
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self.transcript.append_entry, "Writer",
                            f"That pipeline command failed: {exc!r}", "final")

        threading.Thread(target=work, name="dream3d-chat", daemon=True).start()

    # -- the picker ------------------------------------------------------
    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.refresh_btn.setEnabled(False)

        def work() -> None:
            try:
                folder, pipelines = self.actions.scan()
                self._to_ui(self._show_pipelines, folder, pipelines)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self.set_view, f"Pipeline scan failed: {exc!r}")
            finally:
                self._to_ui(self._scanned)

        threading.Thread(target=work, name="dream3d-scan",
                         daemon=True).start()

    def _scanned(self) -> None:
        self._busy = False
        self.refresh_btn.setEnabled(True)

    def _show_pipelines(self, folder: Path, pipelines) -> None:
        self._pipelines = list(pipelines)
        self.pipelines.blockSignals(True)
        self.pipelines.clear()
        if not self._pipelines:
            item = QListWidgetItem("(none — drop .py / .dream3d files here)")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.pipelines.addItem(item)
            self.set_view(dream3d_core.empty_message(folder))
        for pl in self._pipelines:
            self.pipelines.addItem(dream3d_core.label_for(pl))
        self.pipelines.blockSignals(False)

    def selected(self):
        """The selected Pipeline, by ROW — never rebuilt from the label."""
        row = self.pipelines.currentRow()
        return self._pipelines[row] if 0 <= row < len(self._pipelines) \
            else None

    def on_select(self, *_args) -> None:
        pl = self.selected()
        if pl is not None:
            self.set_view(self.actions.render(pl))

    def set_view(self, text: str) -> None:
        self.view.setPlainText(text)

    def _open(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if not self.open_url(QUrl.fromLocalFile(str(path))):
            self.say("Council", f"Couldn't open folder: {path}", "observation")

    def on_open_in(self) -> None:
        self._open(self.actions.in_dir())

    def on_open_out(self) -> None:
        self._open(self.actions.out_dir())

    def on_cube(self) -> None:
        html = self.actions.cube()
        if html is None:
            self.say("Council", "Transformation Cube tool not found — it "
                     "ships as assets/transformation_cube.html. Re-installing "
                     "the assets/ folder should restore it.", "observation")
            return
        try:
            webbrowser.open(html.resolve().as_uri())
            self.say("Council", f"Opened Transformation Cube tool "
                     f"({html.name}) in your default browser.", "observation")
        except Exception as exc:                          # noqa: BLE001
            self.say("Council", f"Couldn't launch browser for {html}: "
                     f"{exc!r}", "observation")

    # -- DREAM3D-NX ------------------------------------------------------
    def _nx_buttons(self):
        return (self.run_btn, self.transpile_btn, self.check_btn,
                self.write_btn)

    def _nx(self, status: str, name: str, job: Callable[[], nx_ops.NxResult],
            preface: Optional[str] = None) -> None:
        """Run one nx job on a worker; one at a time across all four."""
        if self._nx_busy:
            self.nx_status.setText("nx: busy — wait for the current job")
            return
        self._nx_busy = True
        for button in self._nx_buttons():
            button.setEnabled(False)
        self.nx_status.setText(status)
        if preface:
            self.set_view(preface)

        def work() -> None:
            try:
                result = job()
            except Exception as exc:                      # noqa: BLE001
                result = nx_ops.NxResult("nx: failed", str(exc), ok=False)
            self._to_ui(self._nx_done, result)

        threading.Thread(target=work, name=f"dream3d-{name}",
                         daemon=True).start()

    def _nx_done(self, result: nx_ops.NxResult) -> None:
        self._nx_busy = False
        for button in self._nx_buttons():
            button.setEnabled(True)
        self.nx_status.setText(result.status)
        self.set_view(result.body)

    def on_check_env(self) -> None:
        self._nx("nx env: checking…", "nx-check", self.actions.check_env)

    def on_transpile(self) -> None:
        pl = self.selected()
        if pl is None:
            self.set_view("Select a pipeline in the list first.")
            return
        self._nx("nx: transpiling…", "nx-transpile",
                 lambda: self.actions.transpile(pl))

    def on_run_folder(self) -> None:
        pl = self.selected()
        if pl is None:
            self.set_view("Select a pipeline to run over a folder.")
            return
        if Path(pl.path).suffix.lower() == ".py":
            self.set_view(f"{pl.name} is a Python script — run it directly. "
                          "The folder runner drives a saved .d3dpipeline.")
            return
        if self._nx_busy:
            self.nx_status.setText("nx: busy — wait for the current job")
            return
        in_dir = self.ask_directory(
            title="Folder of input files to process",
            initialdir=str(self.actions.input_dir()), parent=self)
        if not in_dir:
            return
        pattern = self.ask_string("File pattern",
                                  "Which files? (e.g. *.dream3d)",
                                  initialvalue="*.dream3d", parent=self)
        if not pattern:
            return
        self._nx("nx: running…", "nx-run",
                 lambda: self.actions.run_folder(pl, Path(in_dir), pattern),
                 preface=f"Running {pl.name} over {pattern} in:\n  {in_dir}"
                         "\n\nThis runs in the DREAM3D-NX env and may take a "
                         "while.")

    def on_write_script(self) -> None:
        task = self.task.text().strip()
        if not task:
            self.set_view("Describe what the pipeline should do, e.g. 'read "
                          "every .dream3d and write an STL'.")
            return
        self._nx("nx: writing…", "nx-write",
                 lambda: self.actions.write_script(task))


def build_dream3d(window) -> QWidget:
    """Factory for the tab registry."""
    return Dream3DTab(window)
