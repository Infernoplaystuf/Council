"""
council_qt.tabs.research — the Research tab: ask a question, Council searches
the web, reads the best pages and writes a short report in which every claim
cites a numbered source (council_core.web_research).

    Question  the question, the model that writes (the same US-origin lists
              as Connections; "none" lists the best passages instead) and
              Research / Stop.
    Runs      every run in the vault's data_in/research, newest first.
    Report    the run's report.md; links open in the browser.
    Sources   one row per page read, ticked when it is used. Untick a bad
              one and press "Write again" - no page is fetched again, so it
              works offline too.
    "Make a Connections graph" adds a named graph over the run's folder (the
    "All documents" graph reads it anyway, at its next build).

Nothing goes online unless Settings › Online is on (council_core.web); while
it is off the tab says so, and the Research button is greyed out.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton,
                               QSplitter, QTextBrowser, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from council_core import paths
from council_core import web
from council_core import web_research as wr

from ..view import ViewHelpers, amp

NO_MODEL = "No model (best passages only)"


class ResearchActions:
    """What the tab does to the world, apart so a test can stand in."""

    def __init__(self, vault: Optional[Path] = None):
        self._vault = Path(vault) if vault is not None else None

    def vault(self) -> Path:
        return self._vault or paths.vault_dir()

    def online(self) -> bool:
        return web.is_online()

    def why_offline(self) -> str:
        return web.why_offline()

    def models(self) -> List[tuple]:
        """(label, model id): the US-origin Ollama models, then GGUF files."""
        try:
            from .connections import ConnectionsActions
            ca = ConnectionsActions.__new__(ConnectionsActions)
            out = [(name, name) for name in ca.local_models()]
            out += list(ca.gguf_models())
            return out
        except Exception:                                 # noqa: BLE001
            return []

    def chat_for(self, model: str):
        from council_core import kg_extract as kx
        if not model:
            return None
        return kx.gguf_chat(model) if kx.is_gguf(model) else kx.ollama_chat(model)

    def runs(self) -> List[wr.Run]:
        return wr.list_runs(self.vault())

    def research(self, question: str, model: str, on_progress, should_stop) -> wr.Run:
        return wr.research(question, self.vault(), self.chat_for(model), model=model,
                           on_progress=on_progress, should_stop=should_stop)

    def rewrite(self, folder: str, model: str, left_out: List[int],
                on_progress, should_stop) -> wr.Run:
        return wr.rewrite(folder, self.chat_for(model), model=model, left_out=left_out,
                          on_progress=on_progress, should_stop=should_stop)

    def make_graph(self, run: wr.Run):
        return wr.make_graph(self.vault(), run)

    def report(self, run: wr.Run) -> str:
        try:
            return (run.path / wr.REPORT_FILE).read_text(encoding="utf-8")
        except OSError:
            return ""

    def open_folder(self, folder: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


class ResearchTab(ViewHelpers, QWidget):
    def __init__(self, window=None, actions: Optional[ResearchActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or ResearchActions()
        self._runs: List[wr.Run] = []
        self._run: Optional[wr.Run] = None
        self._busy = False
        self._stop = threading.Event()
        self._build()
        self.refresh()

    # -- building ----------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        banner = QHBoxLayout()
        self.offline_label = QLabel("")
        self.offline_label.setWordWrap(True)
        banner.addWidget(self.offline_label, 1)
        self.settings_btn = self._button(banner, "Open Settings", self.on_open_settings)
        outer.addLayout(banner)

        row = QHBoxLayout()
        self.question = QLineEdit()
        self.question.setPlaceholderText(
            "What should Council look up? e.g. how to paint flamethrower "
            "effects on Warhammer models")
        self.question.returnPressed.connect(self.on_research)
        row.addWidget(self.question, 1)
        self.model_box = QComboBox()
        self.model_box.setMinimumWidth(200)
        row.addWidget(self.model_box)
        self.research_btn = self._button(row, "🔎 Research", self.on_research)
        self.stop_btn = self._button(row, "Stop", self.on_stop)
        self.stop_btn.setEnabled(False)
        outer.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.run_list = QListWidget()
        self.run_list.currentRowChanged.connect(self.on_pick_run)
        split.addWidget(self.run_list)

        right = QSplitter(Qt.Orientation.Vertical)
        self.report = QTextBrowser()
        self.report.setOpenLinks(False)
        self.report.anchorClicked.connect(self.on_link)
        right.addWidget(self.report)

        src_panel = QWidget()
        src_layout = QVBoxLayout(src_panel)
        src_layout.setContentsMargins(0, 0, 0, 0)
        self.sources = QTreeWidget()
        self.sources.setColumnCount(3)
        self.sources.setHeaderLabels(["Use", "Source", "Link"])
        self.sources.setRootIsDecorated(False)
        self.sources.itemDoubleClicked.connect(self.on_source_double)
        src_layout.addWidget(self.sources)
        btns = QHBoxLayout()
        self.rewrite_btn = self._button(btns, "✍ Write again with ticked sources",
                                        self.on_rewrite)
        self.graph_btn = self._button(btns, "🕸 Make a Connections graph", self.on_make_graph)
        self.folder_btn = self._button(btns, "📂 Open folder", self.on_open_folder)
        btns.addStretch(1)
        src_layout.addLayout(btns)
        right.addWidget(src_panel)
        right.setSizes([500, 220])
        split.addWidget(right)
        split.setSizes([240, 760])
        outer.addWidget(split, 1)
        self._run_buttons()

    # -- state ---------------------------------------------------------------
    def refresh(self) -> None:
        online = self.actions.online()
        self.offline_label.setText(amp(
            "Online: Council can search the web for this tab. Pages are read as "
            "information only, and every claim in a report cites its page."
            if online else self.actions.why_offline()
            + " Past research below still opens, and a report can be written "
              "again from pages already saved."))
        self.settings_btn.setVisible(not online)
        self.research_btn.setEnabled(online and not self._busy)
        keep = self.model_box.currentData()
        self.model_box.clear()
        self.model_box.addItem(NO_MODEL, "")
        for label, ident in self.actions.models():
            self.model_box.addItem(label, ident)
        i = self.model_box.findData(keep) if keep else (1 if self.model_box.count() > 1 else 0)
        self.model_box.setCurrentIndex(max(0, i))
        self._fill_runs()

    def showEvent(self, event) -> None:                    # noqa: N802
        super().showEvent(event)
        if not self._busy:
            online = self.actions.online()
            self.research_btn.setEnabled(online)
            self.settings_btn.setVisible(not online)

    def _fill_runs(self, select: Optional[str] = None) -> None:
        try:
            self._runs = list(self.actions.runs())
        except Exception as exc:                          # noqa: BLE001
            self._runs = []
            self.status.setText(amp(f"Could not list past research: {exc}"))
        self.run_list.blockSignals(True)
        self.run_list.clear()
        for run in self._runs:
            item = QListWidgetItem(f"{run.started[:10]}  {run.question}")
            item.setToolTip(run.folder)
            self.run_list.addItem(item)
        self.run_list.blockSignals(False)
        row = 0
        if select:
            row = next((i for i, r in enumerate(self._runs) if r.folder == select), 0)
        if self._runs:
            self.run_list.setCurrentRow(row)
            self.on_pick_run(row)
        else:
            self._show(None)

    def on_pick_run(self, row: int) -> None:
        self._show(self._runs[row] if 0 <= row < len(self._runs) else None)

    def _show(self, run: Optional[wr.Run]) -> None:
        self._run = run
        self.sources.clear()
        if run is None:
            self.report.setMarkdown("No research yet. Type a question above and "
                                    "press Research.")
            self._run_buttons()
            return
        self.report.setMarkdown(self.actions.report(run) or "_No report was written for "
                                                            "this run._")
        for s in run.sources:
            item = QTreeWidgetItem(["", f"[{s.n}] {s.title} - {s.site}", s.url])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked if s.left_out
                               else Qt.CheckState.Checked)
            item.setData(0, Qt.ItemDataRole.UserRole, s.n)
            item.setToolTip(1, f"{s.title}\nFound by: {s.found_by}\nSaved as {s.file}")
            self.sources.addTopLevelItem(item)
        self.sources.resizeColumnToContents(0)
        self._run_buttons()

    def _run_buttons(self) -> None:
        have = self._run is not None and not self._busy
        for b in (self.rewrite_btn, self.graph_btn, self.folder_btn):
            b.setEnabled(have)

    def left_out(self) -> List[int]:
        out = []
        for i in range(self.sources.topLevelItemCount()):
            item = self.sources.topLevelItem(i)
            if item.checkState(0) != Qt.CheckState.Checked:
                out.append(int(item.data(0, Qt.ItemDataRole.UserRole)))
        return out

    # -- work -------------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.stop_btn.setEnabled(busy)
        self.research_btn.setEnabled(not busy and self.actions.online())
        self._run_buttons()

    def _start(self, fn: Callable[[], Any], done: Callable[[Any], None]) -> bool:
        if self._busy:
            self.status.setText(amp("Busy - one research job at a time."))
            return False
        self._stop.clear()
        self._set_busy(True)

        def work():
            try:
                result = fn()
                self._to_ui(lambda: done(result))
            except (web.Offline, wr.ResearchError, web.WebError) as exc:
                msg = str(exc)
                self._to_ui(lambda: self.status.setText(amp(msg)))
            except Exception as exc:                      # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                self._to_ui(lambda: self.status.setText(amp(msg)))
            finally:
                self._to_ui(lambda: self._set_busy(False))
        # One literal name, so the window drains it on close
        # (TAB_WORKERS in tests/test_council_qt_foundation.py).
        threading.Thread(target=work, name="research-job", daemon=True).start()
        return True

    def _progress(self, text: str) -> None:
        self._to_ui(lambda: self.status.setText(amp(text)))

    def on_research(self) -> None:
        question = self.question.text().strip()
        if not question:
            self.status.setText(amp("Type a question to research."))
            return
        if not self.actions.online():
            self.refresh()
            return
        model = self.model_box.currentData() or ""
        self.status.setText(amp("Starting…"))

        def done(run: wr.Run) -> None:
            self.status.setText(amp(
                f"Done: {len(run.sources)} page(s) read"
                + (f", {len(run.skipped)} skipped" if run.skipped else "")
                + f". Saved in {run.folder}"))
            self._fill_runs(select=run.folder)
        self._start(lambda: self.actions.research(question, model, self._progress,
                                                  self._stop.is_set), done)

    def on_rewrite(self) -> None:
        run = self._run
        if run is None:
            return
        model = self.model_box.currentData() or ""
        left = self.left_out()

        def done(new: wr.Run) -> None:
            self.status.setText(amp(f"Written again from {len(new.kept())} source(s)."))
            self._fill_runs(select=new.folder)
        self._start(lambda: self.actions.rewrite(run.folder, model, left, self._progress,
                                                 self._stop.is_set), done)

    def on_stop(self) -> None:
        self._stop.set()
        self.status.setText(amp("Stopping after the current step…"))

    def on_make_graph(self) -> None:
        if self._run is None:
            return
        try:
            info = self.actions.make_graph(self._run)
        except Exception as exc:                          # noqa: BLE001
            self.status.setText(amp(f"Could not make the graph: {exc}"))
            return
        self.status.setText(amp(
            f"Made the Connections graph \"{info.name}\". Open Connections and "
            f"press Build graph to read it."))

    def on_open_folder(self) -> None:
        if self._run is not None:
            self.actions.open_folder(self._run.path)

    def on_open_settings(self) -> None:
        if self.window is not None and hasattr(self.window, "show_tab"):
            self.window.show_tab("⚙ Settings")

    def on_link(self, url: QUrl) -> None:
        if url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    def on_source_double(self, item, _column: int) -> None:
        url = item.text(2)
        if url.startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(url))


def build_research(window) -> QWidget:
    """Factory for the tab registry."""
    return ResearchTab(window)
