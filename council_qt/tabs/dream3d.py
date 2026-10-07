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

New here: a script that carries the model stamp is marked "model-written" in
the list, and "Take over…" makes the selected one the user's — after a
confirmation, only on its Yes (council_core.script_takeover). The same can be
typed: "take over <script>".
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
from council_core import nx_ops, paths, pipeline_intent, script_takeover

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

    def model_stamped(self, pipeline) -> bool:
        """Does the pipeline's file carry the model stamp? Reads the file."""
        return script_takeover.is_stamped(pipeline.path)

    def take_over_plan(self, pipeline) -> script_takeover.Plan:
        return script_takeover.prepare(self.vault_dir, Path(pipeline.path))


class Dream3DTab(ViewHelpers, QWidget):
    """Mirrored chat on the left, pipelines and DREAM3D-NX on the right."""

    def __init__(self, window=None, actions: Optional[DreamActions] = None,
                 ask_directory: Callable = dialogs.askdirectory,
                 ask_string: Callable = dialogs.askstring,
                 open_url: Callable[[QUrl], bool] = QDesktopServices.openUrl,
                 auto_refresh: bool = True,
                 confirm: Callable[..., bool] = dialogs.confirm):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or DreamActions()
        self.ask_directory = ask_directory
        self.ask_string = ask_string
        self.open_url = open_url
        # Asks before a take-over: Yes only on a click, COUNCIL_NO_DIALOGS
        # answers No (dialogs.confirm).
        self.confirm = confirm
        self._tokens = theme.tokens("dark")
        self._pipelines: List = []
        self._stamped: List[bool] = []  # per row: carries the model stamp
        self._busy = False              # the pipeline scan
        self._nx_busy = False           # any DREAM3D-NX job (defect 4)
        self._local_chat = None

        self._build()
        self._attach_to_council()
        # The first scan waits for the tab to be SHOWN, not built. A worker
        # started in a constructor outlives a tab that is built and dropped —
        # the scan creates folders, slow on a synced drive — and its last
        # reference can go on the worker thread, which destroys the widget
        # off the GUI thread. Measured: an access violation in the tab
        # harness, which builds every tab without showing most of them.
        self._scan_on_show = bool(auto_refresh)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._scan_on_show:
            self._scan_on_show = False
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
        # Live only while the selected script carries the model stamp.
        self.take_over_btn = self._button(row, "Take over…",
                                          self.on_take_over)
        self.take_over_btn.setToolTip(
            "Run the selected model-written script as your own: removes its "
            "model stamp line, after asking you.")
        self.take_over_btn.setEnabled(False)
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
        if self._take_over_typed(text):
            return
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

    def _take_over_typed(self, text: str) -> bool:
        """A take-over ("take over <script>") typed in THIS box, with no
        Council tab to send it through (CouncilTab._take_over is the door
        when there is one). Read before the pipeline chat sees the text,
        which answers the same words from anywhere else without acting on
        them. True if handled."""
        ref = pipeline_intent.take_over_ref(text)
        if ref is None:
            return False
        try:
            plan = script_takeover.decide(self.actions.vault_dir, ref, text)
        except Exception as exc:                          # noqa: BLE001
            plan = script_takeover.Plan(
                shown=ref, refusal=f"The take-over failed: {exc!r}. Nothing "
                                   f"changed.")
        if plan is None:
            return False
        said = script_takeover.run(
            plan, self.actions.vault_dir,
            confirm=lambda title, body: self.confirm(title, body, parent=self),
            via=f"typed in the Dream3D chat: "
                f"{text.splitlines()[0][:200]!r}")
        self.transcript.append_entry("Council", said, "observation")
        self.refresh()
        return True

    # -- the picker ------------------------------------------------------
    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.refresh_btn.setEnabled(False)

        def work() -> None:
            try:
                folder, pipelines = self.actions.scan()
                # Which rows carry the model stamp: read here, off the GUI
                # thread, like the scan (a synced drive is slow).
                stamped = [self._model_stamped(pl) for pl in pipelines]
                self._to_ui(self._show_pipelines, folder, pipelines, stamped)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self.set_view, f"Pipeline scan failed: {exc!r}")
            finally:
                self._to_ui(self._scanned)

        threading.Thread(target=work, name="dream3d-scan",
                         daemon=True).start()

    def _model_stamped(self, pl) -> bool:
        """Off the GUI thread (refresh's worker): reads a file, no widget."""
        try:
            return bool(self.actions.model_stamped(pl))
        except Exception:                                 # noqa: BLE001
            return False

    def _scanned(self) -> None:
        self._busy = False
        self.refresh_btn.setEnabled(True)

    def _show_pipelines(self, folder: Path, pipelines,
                        stamped: Optional[List[bool]] = None) -> None:
        self._pipelines = list(pipelines)
        self._stamped = [bool(s) for s in (stamped or [])]
        self._stamped += [False] * (len(self._pipelines) - len(self._stamped))
        self.pipelines.blockSignals(True)
        self.pipelines.clear()
        if not self._pipelines:
            item = QListWidgetItem("(none — drop .py / .dream3d files here)")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.pipelines.addItem(item)
            self.set_view(dream3d_core.empty_message(folder))
        for pl, model in zip(self._pipelines, self._stamped):
            self.pipelines.addItem(dream3d_core.label_for(pl, model=model))
        self.pipelines.blockSignals(False)
        self.take_over_btn.setEnabled(self._selected_is_stamped())

    def selected(self):
        """The selected Pipeline, by ROW — never rebuilt from the label."""
        row = self.pipelines.currentRow()
        return self._pipelines[row] if 0 <= row < len(self._pipelines) \
            else None

    def _selected_is_stamped(self) -> bool:
        row = self.pipelines.currentRow()
        return 0 <= row < len(self._stamped) and self._stamped[row]

    def on_select(self, *_args) -> None:
        self.take_over_btn.setEnabled(self._selected_is_stamped())
        pl = self.selected()
        if pl is not None:
            self.set_view(self.actions.render(pl))

    def on_take_over(self) -> None:
        """The Take over button: the selected model-written script becomes
        the user's — after the confirmation, and only on its Yes
        (council_core.script_takeover)."""
        pl = self.selected()
        if pl is None:
            self.set_view("Select a model-written script in the list first.")
            return
        try:
            plan = self.actions.take_over_plan(pl)
        except Exception as exc:                          # noqa: BLE001
            plan = script_takeover.Plan(
                shown=pl.name, refusal=f"The take-over failed: {exc!r}. "
                                       f"Nothing changed.")
        said = script_takeover.run(
            plan, self.actions.vault_dir,
            confirm=lambda title, body: self.confirm(title, body, parent=self),
            via="the Take over button (Dream3D tab)")
        self.set_view(said)
        self.say("Council", said, "observation")
        self.refresh()

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
