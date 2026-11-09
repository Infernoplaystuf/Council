"""
The Study tab: notebooks on the left, and what to do with the one picked on
the right.

A NOTEBOOK is a named Connections graph over the folders the user picked
(council_core.plugin_services.Sources): the same readers, so PDF, Word,
PowerPoint, text, Markdown, HTML and workbooks are read the way Connections
reads them, and a notebook shows in Connections' graph list too. Making one
here leaves it out of the Council's own answers.

Pages (right):
  Sources   search the notebook's passages; click one to read the cited
            lines here, or open the file.
  Review    a literature review of the notebook, or of a topic in it
            (engine.review): every sentence cites its passages, a click on a
            citation shows that passage, and what the model wrote but could
            not back with a citation is listed apart. Saved per notebook
            (<notebook>.review.json).

Work that reads files or asks a model runs on a worker thread; its result
comes back to the GUI thread through a queued signal (``_Ui``). What the
user picks (the model, the last notebook) is kept in the plugin's data
folder as settings.json. Nothing leaves the PC.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QSpinBox, QSplitter,
                               QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from .engine import common as cm
from .engine import review as rv

SETTINGS_FILE = "settings.json"
SEARCH_RESULTS = 25
ROLE = Qt.ItemDataRole.UserRole


def esc(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


class _Ui(QObject):
    """Runs a function on the GUI thread when a worker emits it."""
    call = Signal(object)

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @staticmethod
    def _run(fn: Callable[[], None]) -> None:
        fn()


class NewNotebookDialog(QDialog):
    """Name, folders, and (optionally) which file types to read."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("New notebook")
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Corrosion literature")
        self.folders = QListWidget()
        self.folders.setObjectName("study_new_folders")
        add = QPushButton("Add folder…")
        add.clicked.connect(self._add)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: [self.folders.takeItem(self.folders.row(i))
                                        for i in self.folders.selectedItems()])
        self.types = QLineEdit()
        self.types.setPlaceholderText("every type it can read (or e.g. .pdf .docx .md)")
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Folders", self.folders)
        form.addRow("", row)
        form.addRow("File types", self.types)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _add(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder to read")
        if folder:
            self.folders.addItem(folder)

    def values(self) -> Dict[str, Any]:
        return {"name": self.name.text().strip(),
                "folders": [self.folders.item(i).text()
                            for i in range(self.folders.count())],
                "suffixes": [t for t in self.types.text().replace(",", " ").split() if t]}


class SourcesPage(QWidget):
    """Search the notebook; read a passage where it is."""

    def __init__(self, tab: "StudyTab"):
        super().__init__()
        self.tab = tab
        self.query = QLineEdit()
        self.query.setObjectName("study_search")
        self.query.setPlaceholderText("Search this notebook")
        self.query.returnPressed.connect(self.search)
        go = QPushButton("Search")
        go.clicked.connect(self.search)
        self.results = QListWidget()
        self.results.setObjectName("study_results")
        self.results.setWordWrap(True)
        self.results.currentItemChanged.connect(lambda cur, _p: self.show(cur))
        self.preview = QTextBrowser()
        self.preview.setObjectName("study_preview")
        self.open_btn = QPushButton("Open file")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_file)
        top = QHBoxLayout()
        top.addWidget(self.query, 1)
        top.addWidget(go)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.results)
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.preview, 1)
        hl.addWidget(self.open_btn, 0, Qt.AlignmentFlag.AlignLeft)
        split.addWidget(holder)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        self._current = None

    def search(self) -> None:
        nid, text = self.tab.notebook_id(), self.query.text().strip()
        if not nid or not text:
            return
        self.results.clear()
        self.results.addItem("Searching…")
        sources = self.tab.sources

        def work():
            return sources.search(nid, text, SEARCH_RESULTS)
        self.tab.run(work, self._shown, "searching")

    def _shown(self, hits) -> None:
        self.results.clear()
        if not hits:
            self.results.addItem("Nothing in this notebook matches. Has it been built?")
            return
        for p in hits:
            first = " ".join(p.text.split())[:220]
            item = QListWidgetItem(f"{p.cite()}\n{first}")
            item.setData(ROLE, p)
            self.results.addItem(item)

    def show(self, item: Optional[QListWidgetItem]) -> None:
        p = item.data(ROLE) if item is not None else None
        self._current = p
        self.open_btn.setEnabled(p is not None)
        if p is None:
            self.preview.clear()
            return
        self.tab.show_passage(self.preview, p)

    def open_file(self) -> None:
        if self._current is not None:
            self.tab.open_file(self._current)


class ReviewPage(QWidget):
    """Write the notebook's literature review; click a citation to read it."""

    def __init__(self, tab: "StudyTab"):
        super().__init__()
        self.tab = tab
        self.review: Optional[rv.Review] = None
        self._stop = threading.Event()
        self.topic = QLineEdit()
        self.topic.setObjectName("study_topic")
        self.topic.setPlaceholderText("Topic or question (blank: the documents as a whole)")
        self.sections = QSpinBox()
        self.sections.setRange(2, 8)
        self.sections.setValue(rv.SECTIONS)
        self.sections.setSuffix(" sections")
        self.write_btn = QPushButton("Write review")
        self.write_btn.setObjectName("study_write_review")
        self.write_btn.clicked.connect(self.write)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop.set)
        self.save_btn = QPushButton("Save as Markdown…")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.save_markdown)
        top = QHBoxLayout()
        top.addWidget(self.topic, 1)
        top.addWidget(self.sections)
        top.addWidget(self.write_btn)
        top.addWidget(self.stop_btn)
        top.addWidget(self.save_btn)
        self.text = QTextBrowser()
        self.text.setObjectName("study_review")
        self.text.setOpenLinks(False)
        self.text.anchorClicked.connect(self.cite_clicked)
        self.cited = QTextBrowser()
        self.cited.setObjectName("study_review_source")
        self.open_btn = QPushButton("Open file")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(lambda: self._source and self.tab.open_file(self._source))
        self._source: Optional[rv.Source] = None
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(QLabel("Cited passage"))
        sl.addWidget(self.cited, 1)
        sl.addWidget(self.open_btn, 0, Qt.AlignmentFlag.AlignLeft)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.text)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)

    def file_name(self, nid: str) -> str:
        return f"{nid}.review.json"

    def notebook_changed(self, nid: str) -> None:
        saved = self.tab.load(self.file_name(nid)) if nid else None
        self.set_review(rv.Review.from_dict(saved) if saved else None)

    def set_review(self, review: Optional[rv.Review]) -> None:
        self.review = review
        self.save_btn.setEnabled(review is not None)
        self.cited.clear()
        self.open_btn.setEnabled(False)
        if review is None:
            self.text.setHtml("<p style='color:#888'>No review yet. Press Write "
                              "review: the model plans sections, writes each from "
                              "the notebook's passages, and every sentence it "
                              "cannot back with a citation is left out.</p>")
        else:
            self.text.setHtml(review.to_html(esc))

    def write(self) -> None:
        nid, model = self.tab.notebook_id(), self.tab.model_id()
        if not nid:
            return
        if not model:
            self.tab.status.setText("Choose a model first (a US-made model on this PC).")
            return
        try:
            chat = self.tab.models.chat(model)
        except ValueError as exc:
            self.tab.status.setText(str(exc))
            return
        self._stop.clear()
        self.write_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        sources, topic, n = self.tab.sources, self.topic.text().strip(), self.sections.value()
        label = self.tab.model.currentText()

        def work():
            return rv.write_review(chat, sources, nid, topic=topic, model=label,
                                   sections=n, on_progress=self.tab.say,
                                   should_stop=self._stop.is_set)

        def done(review):
            self._idle()
            self.tab.save(self.file_name(nid), review.to_dict())
            if nid == self.tab.notebook_id():
                self.set_review(review)
                c = review.counts()
                self.tab.status.setText(
                    f"Review written: {c['sentences']} cited sentences, "
                    f"{c['left_out']} left out as unsupported.")
            self.tab.review_changed(nid)

        self.tab.run(work, done, "writing the review", on_fail=self._idle)

    def _idle(self, *_a) -> None:
        self.write_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def cite_clicked(self, url) -> None:
        text = url.toString()
        if not text.startswith("cite:") or self.review is None:
            return
        src = self.review.sources.get(int(text[5:]))
        if src is None:
            return
        self._source = src
        self.open_btn.setEnabled(True)
        self.tab.show_passage(self.cited, src)

    def save_markdown(self) -> None:
        if self.review is None:
            return
        path, _f = QFileDialog.getSaveFileName(self, "Save the review",
                                               f"{self.review.title}.md",
                                               "Markdown (*.md)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.review.to_markdown())
            self.tab.status.setText(f"Saved {path}")


class StudyTab(QWidget):
    def __init__(self, ctx=None, *, sources=None, models=None):
        super().__init__()
        self.ctx = ctx
        self.sources = sources if sources is not None else ctx.service("sources")
        self.models = models if models is not None else ctx.service("models")
        self._ui = _Ui(self)
        self._busy = 0
        self.settings: Dict[str, Any] = (ctx.load_json(SETTINGS_FILE, {}) if ctx else {}) or {}
        self._build()
        self.refresh_notebooks()
        self.refresh_models()

    # -- the worker seam ---------------------------------------------------
    def run(self, work: Callable[[], Any], done: Callable[[Any], None],
            what: str = "", on_fail: Optional[Callable[[str], None]] = None
            ) -> threading.Thread:
        """``work()`` on a worker thread, then ``done(result)`` on the GUI
        thread; an exception is shown in the status line instead (and
        ``on_fail(reason)`` called). Stop is an exception too."""
        self._busy += 1
        if what:
            self.status.setText(what[:1].upper() + what[1:] + "…")

        def finish(ok: bool, value: Any) -> None:
            self._busy -= 1
            if ok:
                if what:
                    self.status.setText("")
                done(value)
            else:
                self.status.setText(value if value == "Stopped." else
                                    f"Stopped {what or 'working'}: {value}")
                if on_fail is not None:
                    on_fail(value)

        def body():
            try:
                value, ok = work(), True
            except cm.Stopped:
                value, ok = "Stopped.", False
            except Exception as exc:                         # noqa: BLE001
                value, ok = f"{type(exc).__name__}: {exc}"[:300], False
            self._ui.call.emit(lambda: finish(ok, value))
        thread = threading.Thread(target=body, daemon=True, name=f"study-{what}")
        thread.start()
        return thread

    def say(self, text: str) -> None:
        """A progress line from a worker (thread-safe)."""
        self._ui.call.emit(lambda: self.status.setText(text))

    # -- layout ------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        intro = QLabel("Pick the documents you want to study as a notebook. "
                       "Council reads them here on this PC, writes a literature "
                       "review that cites every claim, and makes flash cards, "
                       "quizzes, a study guide and a glossary from it.")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Model"))
        self.model = QComboBox()
        self.model.setObjectName("study_model")
        self.model.currentIndexChanged.connect(self._model_picked)
        ll.addWidget(self.model)
        ll.addWidget(QLabel("Notebooks"))
        self.notebooks = QListWidget()
        self.notebooks.setObjectName("study_notebooks")
        self.notebooks.currentItemChanged.connect(lambda *_a: self.notebook_picked())
        ll.addWidget(self.notebooks, 1)
        row = QHBoxLayout()
        self.new_btn = QPushButton("New notebook…")
        self.new_btn.clicked.connect(self.new_notebook)
        self.build_btn = QPushButton("Build / update")
        self.build_btn.setToolTip("Read the notebook's folders (only new or changed files)")
        self.build_btn.clicked.connect(self.build_notebook)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setToolTip("Removes the notebook, never your files")
        self.delete_btn.clicked.connect(self.delete_notebook)
        for b in (self.new_btn, self.build_btn, self.delete_btn):
            row.addWidget(b)
        ll.addLayout(row)
        ll.addWidget(QLabel("Documents"))
        self.documents = QListWidget()
        self.documents.setObjectName("study_documents")
        ll.addWidget(self.documents, 1)

        self.pages = QTabWidget()
        self.pages.setObjectName("study_pages")
        self.sources_page = SourcesPage(self)
        self.pages.addTab(self.sources_page, "Sources")
        self.review_page = ReviewPage(self)
        self.pages.addTab(self.review_page, "Review")

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(self.pages)
        split.setStretchFactor(1, 3)
        outer.addWidget(split, 1)
        self.status = QLabel("")
        self.status.setObjectName("study_status")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

    # -- settings and saved work --------------------------------------------
    def save_settings(self) -> None:
        if self.ctx is not None:
            self.ctx.save_json(SETTINGS_FILE, self.settings)

    def save(self, name: str, data: Any) -> None:
        """A notebook's saved work (review, study documents, practice) in the
        plugin's data folder."""
        if self.ctx is not None:
            self.ctx.save_json(name, data)

    def load(self, name: str) -> Any:
        return self.ctx.load_json(name, None) if self.ctx is not None else None

    def review_changed(self, nid: str) -> None:
        """A new review was written: pages made from it may want to know."""
        for i in range(self.pages.count()):
            page = self.pages.widget(i)
            if hasattr(page, "review_changed"):
                page.review_changed(nid)

    # -- models ------------------------------------------------------------
    def refresh_models(self) -> None:
        def done(found):
            self.model.blockSignals(True)
            self.model.clear()
            for label, mid in found:
                self.model.addItem(label, mid)
            if not found:
                self.model.addItem("(no US-made local model found)", "")
            want = self.settings.get("model", "")
            i = self.model.findData(want) if want else -1
            self.model.setCurrentIndex(max(0, i))
            self.model.blockSignals(False)
        self.run(self.models.available, done, "")

    def _model_picked(self, _i: int) -> None:
        self.settings["model"] = self.model_id()
        self.save_settings()

    def model_id(self) -> str:
        return str(self.model.currentData() or "")

    # -- notebooks ---------------------------------------------------------
    def notebook_id(self) -> str:
        item = self.notebooks.currentItem()
        return str(item.data(ROLE)) if item is not None else ""

    def refresh_notebooks(self, select: str = "") -> None:
        want = select or self.notebook_id() or self.settings.get("notebook", "")
        self.notebooks.blockSignals(True)
        self.notebooks.clear()
        try:
            listed = self.sources.notebooks()
        except Exception as exc:                             # noqa: BLE001
            listed = []
            self.status.setText(f"Could not list notebooks: {exc}")
        for nb in listed:
            label = f"{nb.name} (every document in Connections)" if nb.is_main else nb.name
            item = QListWidgetItem(label)
            item.setData(ROLE, nb.id)
            self.notebooks.addItem(item)
        pick = 0
        for i in range(self.notebooks.count()):
            if self.notebooks.item(i).data(ROLE) == want:
                pick = i
        if self.notebooks.count() > 1 and not want:
            pick = 1                     # the first notebook of the user's own
        self.notebooks.setCurrentRow(pick)
        self.notebooks.blockSignals(False)
        self.notebook_picked()

    def notebook_picked(self) -> None:
        nid = self.notebook_id()
        self.delete_btn.setEnabled(bool(nid) and nid != "main")
        if nid and self.settings.get("notebook") != nid:
            self.settings["notebook"] = nid
            self.save_settings()
        self.refresh_documents()
        for i in range(self.pages.count()):
            page = self.pages.widget(i)
            if hasattr(page, "notebook_changed"):
                page.notebook_changed(nid)

    def refresh_documents(self) -> None:
        nid = self.notebook_id()
        self.documents.clear()
        if not nid:
            return
        sources = self.sources

        def done(docs):
            if nid != self.notebook_id():
                return
            self.documents.clear()
            if not docs:
                self.documents.addItem("Nothing read yet: press Build / update.")
            for d in docs:
                text = d["display"] if d["status"] == "ok" else \
                    f"{d['display']} (not read: {d['status']})"
                self.documents.addItem(text)
        self.run(lambda: sources.documents(nid), done, "")

    def new_notebook(self) -> None:
        dlg = NewNotebookDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        try:
            nb = self.sources.create_notebook(v["name"], v["folders"], v["suffixes"])
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "New notebook", str(exc))
            return
        self.refresh_notebooks(select=nb.id)
        self.build_notebook()

    def build_notebook(self) -> None:
        nid = self.notebook_id()
        if not nid:
            return
        self.build_btn.setEnabled(False)
        sources = self.sources

        def listener(line, _fraction=None):
            self.say(str(line))

        def done(stats):
            self.build_btn.setEnabled(True)
            n = stats.get("documents", 0) if isinstance(stats, dict) else 0
            self.status.setText(f"Read the notebook: {n} document(s).")
            self.refresh_documents()

        def work():
            try:
                return sources.build(nid, listener=listener)
            finally:
                self._ui.call.emit(lambda: self.build_btn.setEnabled(True))
        self.run(work, done, "reading the notebook's files")

    def delete_notebook(self) -> None:
        nid = self.notebook_id()
        if not nid or nid == "main":
            return
        item = self.notebooks.currentItem()
        if QMessageBox.question(
                self, "Delete notebook",
                f"Delete the notebook {item.text()}? Your files are not touched; "
                f"only what Council made from them goes.") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.sources.delete_notebook(nid)
        except (KeyError, ValueError, OSError) as exc:
            QMessageBox.warning(self, "Delete notebook", str(exc))
            return
        self.settings.pop("notebook", None)
        self.save_settings()
        self.refresh_notebooks()

    # -- citations ---------------------------------------------------------
    def show_passage(self, browser: QTextBrowser, p, line: Optional[int] = None) -> None:
        """Passage ``p`` in ``browser``; with ``line``, the lines around that
        line read from the file, the line marked."""
        nid = self.notebook_id()
        sources = self.sources
        browser.setHtml(f"<b>{esc(p.cite())}</b><br><i>reading…</i>")

        def work():
            if line is not None:
                return sources.context(nid, p.path, p.locator(line), radius=4)
            # the whole passage, as it was read
            pre = f"p{p.page} " if isinstance(p.page, int) else ""
            return [(f"{pre}L{n}", t, False)
                    for n, t in zip(p.line_nos, p.text.split("\n"))]

        def done(lines):
            rows = []
            for gutter, text, hit in lines or []:
                style = " style='background:#fff3a0;color:#000'" if hit else ""
                rows.append(f"<tr><td style='color:#888;padding-right:8px'>{esc(gutter)}"
                            f"</td><td{style}>{esc(text) or '&nbsp;'}</td></tr>")
            body = "".join(rows) or f"<pre>{esc(p.text)}</pre>"
            browser.setHtml(f"<b>{esc(p.cite())}</b><table>{body}</table>")
        self.run(work, done, "")

    def open_file(self, p) -> None:
        try:
            path = self.sources.abs_path(self.notebook_id(), p.path)
        except Exception as exc:                             # noqa: BLE001
            self.status.setText(f"Could not find the file: {exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def build_tab(ctx) -> QWidget:
    """The plugin's entry: Council calls it with the plugin's context."""
    return StudyTab(ctx)
