"""
council_qt.tabs.connections — knowledge graphs built from the vault's documents.

Works the way Morphik's graphs do (user, 2026-10-21: "I would like the
connections tab to function very similar to morphik"): pick a graph (All
documents, or a named one made over chosen folders with New graph…), press
⟳ Build graph, and the documents are read - labelled fields, tables, Ledgers
and Collections without a model, and, when a model is chosen, every passage
of free text by that model, which names the people, organisations, projects,
parts, places and other things in it and how they relate. Then:

  * search anything the graph holds; see everything linked to it, grouped by
    how ("leads", "supplies", "← located in"), each link with its evidence: the
    file, the sheet/row or page/line, and the quoted text. Click a piece of
    evidence to read that spot here; "Open file" opens it in its own program;
  * Picture: the entity and what is around it (or the whole graph) as dots
    and arrows; click a dot to open it, double-click to centre on it;
  * Ask: a question answered from the graph's facts, each cited (the
    Council's own answers use the graphs marked for answers, through
    council_core.kg_query).

A model's links go in the graph at once, or wait for ✓ Accept, as each graph
is set (Graph settings…). Nothing here deletes or edits a document; deleting
a named graph removes only what building it made.

Rebuild, the model's reading, the label harvest, the picture's layout and
Ask run on worker threads with their own SQLite connection (a connection
belongs to the thread that made it); the view's own connection is reopened
when they finish. Listing the installed models asks Ollama over HTTP (GET
/api/tags on this PC), so it runs on a worker too — building the tab never
waits on a model server. It does still ASK it, as it is built: a test that
builds the tab stubs ConnectionsActions.local_models and pi_nodes
(test_connections_tab, and the window fixture of test_council_qt_foundation),
or it reaches the real Ollama port.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QSpinBox, QSplitter, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from council_core import kg_graphs as kgg
from council_core import knowledge_graph as kgm
from council_core import paths
from council_core.kg_query import phrase

from .. import theme
from ..view import ViewHelpers, _alive, amp
from ..widgets.kg_canvas import KnowledgeGraphCanvas, type_colour
from .connections_contents import ContentsView
from .connections_dialogs import (FieldsDialog, FoldersDialog, GraphDialog,
                                  WhyNoLinksDialog)

#: The type filter's fixed entries; the types a graph holds besides these
#: (ORGANIZATION, LOCATION … a model named) are added after a build.
TYPE_FILTERS = (("Everything", None), ("People", "PERSON"),
                ("Parts", "PART"), ("Projects", "PROJECT"))
TYPE_ICON = {"PERSON": "👤", "PART": "⚙", "PROJECT": "📁", "DOCUMENT": "📄",
             "ORGANIZATION": "🏢", "LOCATION": "📍", "EQUIPMENT": "🔧",
             "MATERIAL": "🧪", "PROCESS": "🔁", "REPORT": "📑", "EVENT": "📅",
             "CONCEPT": "💡"}
#: The model list's last entry: build without a model (labelled fields,
#: tables, Ledgers and Collections only).
NO_MODEL = ""


def type_icon(etype: str) -> str:
    return TYPE_ICON.get(etype, "◆")


def _us_origin(model: str) -> bool:
    """A model name is of US origin by council_core.local_models - the
    classification the Ledger's list and the Models tab use; an unknown
    origin is not US."""
    from council_core import local_models as lm
    return lm.maker_and_origin(str(model or ""))[1] == "US"


#: How a piece of evidence from a spreadsheet was found, in words (the
#: older methods - row, record, document, model ... - show as they always
#: have).
METHOD_WORDS = {
    "ledger": "Ledger row (accepted)", "sheet": "workbook cell",
    "picture": "picture (OCR)", "model": "model",
}
#: The tree shows this much of a big neighbourhood; the rest is counted
#: ("… and 87 more"). An inspector of 3,000 parts has 3,000 links, each
#: with a row per flaw: drawing every one froze the tab.
EVIDENCE_SHOWN = 20
RELATIONS_SHOWN = 300
MENTIONS_SHOWN = 200
#: The answer that means "a person not listed: type their name".
NEW_PERSON = "__new_person__"
#: A file at least this big is previewed on a worker thread, as every
#: workbook is: the preview of row 59,990 of a Ledger took 2.4 s on the GUI
#: thread (review, 2026-10-16).
PREVIEW_ASYNC_BYTES = 1 << 20


def method_words(method: str) -> str:
    return METHOD_WORDS.get(method, method)

ROLE_KIND = Qt.ItemDataRole.UserRole          # 'entity' | 'relation' | 'evidence' | 'mention'
ROLE_DATA = Qt.ItemDataRole.UserRole + 1


def display_name(ent: Optional[Dict[str, Any]]) -> str:
    """'PRJ-0915 — Helios Turbine Upgrade': a code with its best plain name."""
    if not ent:
        return "(gone)"
    name = ent.get("name", "")
    if ent.get("type") in ("PROJECT", "PART") and kgm.looks_like_code(name):
        plain = [a for a in ent.get("aliases", []) if not kgm.looks_like_code(a)
                 and kgm.fold(a) != kgm.fold(name)]
        if plain:
            return f"{name} — {max(plain, key=len)}"
    return name


class ConnectionsActions:
    """What the Connections tab asks of the application. Tests pass their own
    vault; nothing else here knows where the vault is."""

    def __init__(self, vault_dir: Optional[Path] = None, root: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.root = Path(root) if root else self.vault_dir / "data_in"
        #: The graph the tab shows (kg_graphs.MAIN = all documents). The
        #: Ledger tab's own ConnectionsActions stays on the main graph.
        self.graph_id = kgg.MAIN

    def open_graph(self) -> kgm.KnowledgeGraph:
        return kgg.GraphList(self.vault_dir).open(self.graph_id, self.root)

    # -- graphs (named, over chosen folders) --
    def graphs(self) -> List[kgg.GraphInfo]:
        return kgg.GraphList(self.vault_dir).all()

    def create_graph(self, values: Dict[str, Any]) -> kgg.GraphInfo:
        gl = kgg.GraphList(self.vault_dir)
        info = gl.create(values["name"], values["folders"], values.get("suffixes") or (),
                         use_in_answers=bool(values.get("use_in_answers", True)),
                         focus=str(values.get("focus") or ""))
        with gl.open(info.id, self.root) as kg:
            if values.get("model_links") == "review":
                kg.set_model_links("review")
            if values.get("embed_model"):
                kg.set_related(values["embed_model"], values.get("related_threshold"))
        return info

    def update_graph(self, gid: str, values: Dict[str, Any]) -> None:
        gl = kgg.GraphList(self.vault_dir)
        gl.update(gid, name=values.get("name") or None,
                  use_in_answers=values.get("use_in_answers"),
                  suffixes=None if gid == kgg.MAIN else values.get("suffixes"))
        with gl.open(gid, self.root) as kg:
            if kg.focus() != values.get("focus", ""):
                kg.set_focus(values.get("focus", ""))
            want = values.get("model_links", "graph")
            if (kg.model_link_status() == "suggested") != (want == "review"):
                kg.set_model_links(want)
            if "embed_model" in values and (
                    values["embed_model"] != kg.embed_model()
                    or values.get("related_threshold") != kg.related_threshold()):
                kg.set_related(values["embed_model"], values.get("related_threshold"))

    def embed_models(self) -> List[tuple]:
        """(id, label) of the embedding models on this PC (kg_related)."""
        from council_core import kg_related
        return kg_related.available()

    def find_related(self, on_progress, should_stop) -> Optional[Dict[str, Any]]:
        """Related by meaning: the graph's embedding model reads what is new
        or changed and the close pairs are suggested
        (KnowledgeGraph.find_related). None when the graph has no model."""
        from council_core import kg_related
        with self.open_graph() as kg:
            model = kg.embed_model()
            if not model:
                return None
            return kg.find_related(kg_related.make(model), model=model,
                                   on_progress=on_progress, should_stop=should_stop)

    def delete_graph(self, gid: str) -> None:
        kgg.GraphList(self.vault_dir).delete(gid)

    # -- the picture and questions --
    def contents_picture(self, document_id: str):
        """(nodes, edges, positions) of one file's contents (kg_layout)."""
        from council_core import kg_layout
        with self.open_graph() as kg:
            nodes, edges = kg_layout.contents_subgraph(kg, document_id)
        return nodes, edges, kg_layout.layout(nodes, edges)

    def picture(self, center: Optional[str], hops: int):
        """(nodes, edges, positions) to draw (council_core.kg_layout)."""
        from council_core import kg_layout
        with self.open_graph() as kg:
            nodes, edges = kg_layout.subgraph(kg, center, hops=hops)
        return nodes, edges, kg_layout.layout(nodes, edges)

    def chat_for(self, model: str):
        from council_core import kg_extract as kx
        if not model:
            return None
        return kx.gguf_chat(model) if kx.is_gguf(model) else kx.ollama_chat(model)

    def ask(self, question: str, model: str) -> Dict[str, Any]:
        """council_core.kg_query.ask on this graph, with ``model`` ('' = none:
        the cited facts only)."""
        from council_core import kg_query
        return kg_query.ask(self.vault_dir, question, self.chat_for(model),
                            graphs=[self.graph_id])

    def rebuild(self, on_progress=None, listener=None) -> Dict[str, Any]:
        """The rebuild; its progress lines also go to the terminal."""
        with self.open_graph() as kg:
            return kg.seed(on_progress=on_progress, listener=listener)

    def label_suggestions(self) -> List[Dict[str, Any]]:
        with self.open_graph() as kg:
            return kg.suggest_label_synonyms()

    def why_no_links(self) -> Dict[str, Any]:
        """KnowledgeGraph.why_no_links, with its own connection (a worker's)."""
        with self.open_graph() as kg:
            return kg.why_no_links()

    def tick_rules(self, labels) -> List[str]:
        """KnowledgeGraph.confirm_rules, with its own connection (a worker's):
        the labels ticked."""
        with self.open_graph() as kg:
            return kg.confirm_rules(labels)

    def context(self, rel_path: str, locator: Any, quote: str = ""):
        return kgm.source_context(self.root, rel_path, locator, quote=quote)

    # -- free-text suggestions (KG2) --
    def local_models(self) -> List[str]:
        """US-origin models installed in this PC's Ollama, extractor first.
        Origin is council_core.local_models', as in the Ledger's model list:
        pi_models' name prefixes called llava:7b non-US here and US there
        (review, 2026-10-19). The family Ollama reports only rejects - it
        is "llama" for zephyr or openhermes too; the name has to say US.
        An unknown origin is not US."""
        from council_core import kg_extract as kx
        from council_core import local_models as lm
        try:
            import council_engine as ce
            tags = ce._ollama_tags("http://127.0.0.1:11434") or {}
        except Exception:
            tags = {}
        names = []
        for m in tags.get("models", []):
            det = m.get("details") or {}
            name = m.get("name", "")
            if name and lm.maker_and_origin(name, str(det.get("family") or ""),
                                            det.get("families") or ())[1] == "US":
                names.append(name)
        names = sorted(names)
        best = kx.DEFAULT_EXTRACTOR
        return ([best] if best in names else []) + [n for n in names if n != best]

    def gguf_models(self) -> List[tuple]:
        """(label, path) of the GGUF files in Council's model folders that
        are not of non-US origin: Suggest links listed Ollama models only,
        so a GGUF-only PC could not use it."""
        from council_core import local_models as lm
        out = []
        for e in lm.list_local_models(include_ollama=False):
            if e.get("origin") != "non-US" and e.get("path"):
                out.append((f"GGUF: {e.get('name') or e.get('file')}", str(e["path"])))
        return out

    def pi_nodes(self) -> tuple:
        """``(usable, skipped)``: usable = (name, host url, model) of the
        registered Pi nodes whose model is US-origin; skipped = (name, model)
        of the rest. Only when remote nodes are enabled
        (COUNCIL_REMOTE_NODES=1), the engine's own opt-in. A node's model
        was used whatever it was — the real NodePrimus has only qwen2.5:3b,
        so 'and my Pi nodes' ran extraction on it; local_models() already
        offered only US-origin models for this PC. Origin is
        council_core.local_models', as everywhere else (_us_origin)."""
        try:
            import council_engine as ce
            if not ce._remote_nodes_enabled():
                return [], []
            from council_core import apothecary as apoth
            reg = apoth._ae.NodeRegistry(str(apoth.registry_path(self.vault_dir)))
            nodes = [(n.name, f"http://{n.host}:{n.ollama_port}", n.model or n.active_model)
                     for n in reg.list_nodes() if (n.model or n.active_model)]
        except Exception:
            return [], []
        usable = [x for x in nodes if _us_origin(x[2])]
        skipped = [(n, m) for n, _u, m in nodes if not _us_origin(m)]
        return usable, skipped

    def pi_workers(self) -> List[tuple]:
        """(name, host url, model) of the Pi nodes that may share the work."""
        return self.pi_nodes()[0]

    def pi_skipped(self) -> List[tuple]:
        """(name, model) of the Pi nodes left out: not a US-origin model."""
        return self.pi_nodes()[1]

    def read_text(self, model: str, use_pis: bool, on_progress, should_stop):
        """The model's part of a build: every passage of free text read by
        ``model`` (and the Pis' models, when ticked) - entities of any type
        and the links between them (KnowledgeGraph.extract_from_text). Each
        worker reads with its OWN model (a Pi has a smaller one)."""
        from council_core import kg_extract as kx
        if kx.is_gguf(model):
            name = Path(model).name
            workers = {f"this PC ({name})": (kx.gguf_chat(model), name)}
        else:
            workers = {f"this PC ({model})": (kx.ollama_chat(model), model)}
        if use_pis:
            for name, url, pmodel in self.pi_workers():
                if _us_origin(pmodel):                     # pi_workers filters too
                    workers[f"{name} ({pmodel})"] = (kx.ollama_chat(pmodel, host=url), pmodel)
        with self.open_graph() as kg:
            return kg.extract_from_text(workers, model=Path(model).name if kx.is_gguf(model)
                                        else model,
                                        on_progress=on_progress, should_stop=should_stop)

    def open_file(self, rel_path: str) -> None:
        from .vault import VaultActions
        VaultActions(self.vault_dir).open_folder(self.root / rel_path)


class ConnectionsTab(ViewHelpers, QWidget):
    """Search, links with evidence, and a source preview."""

    def __init__(self, window=None, actions: Optional[ConnectionsActions] = None,
                 auto_refresh: bool = True, ask_string=None, ask_yes_no=None,
                 ask_choice=None, ask_folder=None, ask_graph=None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or ConnectionsActions()
        self._tokens = theme.tokens("dark")
        self._busy = False
        self._kg: Optional[kgm.KnowledgeGraph] = None
        self._current: Optional[str] = None
        self._selected: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        #: ⟳ Build graph: after the documents are read, the chosen model
        #: reads the free text (a Rebuild for another reason - an answer, a
        #: split - only reads the documents again).
        self._read_text_after = False
        #: ...and, the graph having an embedding model, related items are
        #: found last (kg_related).
        self._related_after = False
        self._picture_token = None
        self._asking = False
        #: 'Why no links?': its one dialog (tests reach it here), and whether
        #: its worker is still working it out.
        self._why_dialog: Optional[WhyNoLinksDialog] = None
        self._why_running = False
        from .. import dialogs as _dialogs
        self.ask_string = ask_string or _dialogs.askstring
        self.ask_yes_no = ask_yes_no or _dialogs.askyesno
        self.ask_choice = ask_choice or _dialogs.askchoice
        # askdirectory itself does not look at COUNCIL_NO_DIALOGS: offscreen
        # it would wait on a file dialog nobody can see.
        self.ask_folder = ask_folder or (lambda: None if _dialogs.disabled() else
                                         _dialogs.askdirectory(
                                             "Add a folder for the graph to read",
                                             parent=self))
        #: ask_graph(new, current settings) -> the dialog's values, or None
        self.ask_graph = ask_graph or self._graph_dialog
        self._build()
        if auto_refresh:
            self._fill_graphs()
            self._reopen()
            self.on_search()
            self.refresh_models()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        # 1. which graph, and building it
        grow = QHBoxLayout()
        grow.addWidget(QLabel("Graph"))
        self.graph_box = QComboBox()
        self.graph_box.setMinimumWidth(180)
        self.graph_box.activated.connect(lambda _i: self.on_graph_changed())
        grow.addWidget(self.graph_box)
        self.new_graph_btn = self._button(grow, "New graph…", self.on_new_graph)
        self.graph_settings_btn = self._button(grow, "Graph settings…", self.on_graph_settings)
        self.delete_graph_btn = self._button(grow, "Delete graph", self.on_delete_graph)
        grow.addSpacing(12)
        grow.addWidget(QLabel("Read the text with"))
        self.model_box = QComboBox()
        self.model_box.setToolTip("The local model that reads every passage of free text and "
                                  "names what is in it and how it connects. Without one, only "
                                  "labelled fields, tables, Ledgers and Collections are read.")
        grow.addWidget(self.model_box, 1)
        self.pis_box = QCheckBox("and my Pi nodes")
        grow.addWidget(self.pis_box)
        self.rebuild_btn = self._button(grow, "⟳ Build graph", self.on_build)
        self.stop_btn = self._button(grow, "Stop", self.on_stop_suggest)
        self.stop_btn.setEnabled(False)
        # files skipped as too slow, or that stalled the app before
        # (council_core.file_guard): read again on the next build
        from ..widgets.skipped_files import RetrySkippedButton
        self.retry_skipped_btn = RetrySkippedButton(lambda t: self.status.setText(t))
        grow.addWidget(self.retry_skipped_btn)
        outer.addLayout(grow)

        # 2. finding things, and the graph's other lists
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search a person, part, project, organisation, place "
                                       "- or any header or value in your files…")
        self.search.textChanged.connect(lambda _t: self.on_search())
        row.addWidget(self.search, 2)
        self.type_box = QComboBox()
        for title, t in TYPE_FILTERS:
            self.type_box.addItem(title, t)
        self.type_box.currentIndexChanged.connect(lambda _i: self.on_search())
        row.addWidget(self.type_box)
        self.questions_btn = self._button(row, "Questions", self.on_questions)
        self.answers_btn = self._button(row, "Your answers", self.on_answers)
        self.sources_btn = self._button(row, "Spreadsheets", self.on_sources)
        self.folders_btn = self._button(row, "Folders…", self.on_folders)
        self.fields_btn = self._button(row, "Fields…", self.on_fields)
        # Always there: most useful at 0 links (user, 2026-10-20)
        self.why_btn = self._button(row, "Why no links?", self.on_why)
        self.why_btn.setToolTip("What the graph read and the most likely reason it has no "
                                "links (or not more), with what to do. Copy it to send it on: "
                                "counts and labels only, nothing from inside a document.")
        outer.addLayout(row)

        # 3. asking the graph
        arow = QHBoxLayout()
        self.ask_edit = QLineEdit()
        self.ask_edit.setPlaceholderText("Ask the graph, e.g. 'who works on the Falcon "
                                         "retrofit?', 'how is Nordic Forge connected to "
                                         "KA-101?' or 'what was the hardness of PN-4471?'")
        self.ask_edit.returnPressed.connect(self.on_ask)
        arow.addWidget(self.ask_edit, 1)
        self.ask_btn = self._button(arow, "Ask", self.on_ask)
        outer.addLayout(arow)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        # First-run help: offered when a rebuild finds unticked field rules
        # the documents use (on_tick_suggested); hidden otherwise.
        orow = QHBoxLayout()
        self.tick_btn = self._button(orow, "Tick the suggested field rules",
                                     self.on_tick_suggested)
        self.tick_btn.setVisible(False)
        orow.addStretch(1)
        outer.addLayout(orow)
        #: The proposed field rules on offer (labels).
        self._to_tick: List[str] = []
        self.coverage = QLabel("")
        self.coverage.setWordWrap(True)
        self.coverage.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.coverage)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.results = QListWidget()
        self.results.currentItemChanged.connect(self._on_result)
        split.addWidget(self.results)

        self.views = QTabWidget()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Linked item / evidence", "Status", "Where"])
        self.tree.setColumnWidth(0, 330)
        self.tree.setColumnWidth(1, 80)
        self.tree.currentItemChanged.connect(self._on_tree)
        self.tree.itemDoubleClicked.connect(self._on_tree_double)
        self.views.addTab(self.tree, "Links")

        pic = QWidget()
        pl = QVBoxLayout(pic)
        pl.setContentsMargins(0, 4, 0, 0)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Links out"))
        self.hops_box = QSpinBox()
        self.hops_box.setRange(1, 3)
        self.hops_box.setValue(2)
        self.hops_box.valueChanged.connect(lambda _v: self.refresh_picture())
        prow.addWidget(self.hops_box)
        self.whole_btn = self._button(prow, "Whole graph", lambda: self.refresh_picture(whole=True))
        self.legend = QLabel("")
        self.legend.setWordWrap(True)
        prow.addWidget(self.legend, 1)
        pl.addLayout(prow)
        self.canvas = KnowledgeGraphCanvas()
        self.canvas.entity_clicked.connect(self._on_canvas_click)
        self.canvas.entity_activated.connect(self._on_canvas_centre)
        pl.addWidget(self.canvas, 1)
        self.views.addTab(pic, "Picture")

        ans = QSplitter(Qt.Orientation.Vertical)
        self.answer = QPlainTextEdit()
        self.answer.setReadOnly(True)
        self.answer.setPlaceholderText("Ask the graph a question (above): the answer comes "
                                       "from the graph's facts, each cited; the facts are "
                                       "listed below - click one to see where it was found.")
        ans.addWidget(self.answer)
        self.facts = QTreeWidget()
        self.facts.setColumnCount(3)
        self.facts.setHeaderLabels(["Fact / evidence", "Status", "Where"])
        self.facts.setColumnWidth(0, 330)
        self.facts.currentItemChanged.connect(self._on_tree)
        ans.addWidget(self.facts)
        ans.setSizes([200, 260])
        self.views.addTab(ans, "Answer")
        # everything fed in: file › sheet › header/label › values
        # (council_core/kg_contents.py; user, 2026-10-26)
        self.contents_view = ContentsView(lambda: self._kg)
        self.contents_view.preview_requested.connect(self._preview_cell)
        self.contents_view.draw_requested.connect(self.draw_file)
        #: the file the Picture shows the contents of, or None (entities)
        self._picture_file: Optional[str] = None
        self.views.addTab(self.contents_view, "Contents")
        self.views.currentChanged.connect(self._on_view_changed)
        split.addWidget(self.views)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.preview_title = QLabel("Select a piece of evidence to see it here.")
        self.preview_title.setWordWrap(True)
        rl.addWidget(self.preview_title)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.preview.setFont(mono)
        rl.addWidget(self.preview, 1)
        btns = QHBoxLayout()
        self.open_btn = self._button(btns, "Open file", self.on_open_file)
        self.accept_btn = self._button(btns, "✓ Accept link", lambda: self._decide("accepted"))
        self.reject_btn = self._button(btns, "✗ Reject link", lambda: self._decide("rejected"))
        btns.addStretch(1)
        rl.addLayout(btns)

        # KG3: answering a question / merging duplicates
        self.answer_row = QWidget()
        ar = QHBoxLayout(self.answer_row)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.addWidget(QLabel("It is"))
        self.answer_box = QComboBox()
        ar.addWidget(self.answer_box, 1)
        self.everywhere_box = QCheckBox("everywhere this name appears")
        ar.addWidget(self.everywhere_box)
        self.answer_btn = self._button(ar, "Answer", self.on_answer)
        rl.addWidget(self.answer_row)
        self.answer_row.setVisible(False)
        mrow = QHBoxLayout()
        self.merge_btn = self._button(mrow, "Same as…", self.on_merge)
        self.split_btn = self._button(mrow, "Split off a merged name", self.on_unmerge)
        self.forget_btn = self._button(mrow, "Forget this answer", self.on_forget_answer)
        mrow.addStretch(1)
        rl.addLayout(mrow)
        split.addWidget(right)
        split.setSizes([230, 560, 400])
        outer.addWidget(split, 1)
        self._enable_actions()

    # ------------------------------------------------------------------
    def _reopen(self) -> None:
        if self._kg is not None:
            self._kg.close()
            self._kg = None
        try:
            self._kg = self.actions.open_graph()
        except (KeyError, kgg.GraphListDamaged) as exc:
            # the graph shown was deleted (or the list damaged) elsewhere:
            # back to all documents
            if self.actions.graph_id == kgg.MAIN:
                self.status.setText(str(exc))
                return
            self.actions.graph_id = kgg.MAIN
            self._fill_graphs(kgg.MAIN)
            self.status.setText("That graph is gone; showing all documents.")
            self._reopen()
            return
        except kgm.KnowledgeGraphBusy as exc:
            # held by a rebuild elsewhere (the agent's tools, another
            # window): not damage - the buttons stay, try again shortly
            self.status.setText(f"{str(exc)[0].upper()}{str(exc)[1:]}.")
            return
        except kgm.KnowledgeGraphDamaged as exc:
            self.status.setText(str(exc))
            self.rebuild_btn.setEnabled(False)
            return
        self._show_coverage()

    def _show_coverage(self) -> None:
        kg = self._kg
        if kg is None:
            return
        cov = kg.coverage()
        counts = kg.counts()
        confirmed = len(kg.field_rules("confirmed"))
        n_q = counts["open_reviews"]
        self.questions_btn.setText(amp(f"Questions ({n_q})"))
        self._fill_types(counts["entities"])
        if cov["last_run"] is None:
            self.coverage.setText("The graph has not been built yet. Choose a model to read "
                                  "the text with, then ⟳ Build graph.")
            return
        ents = counts["entities"]
        rels = counts["relations"]
        others = sum(v for k, v in ents.items() if k not in kgm.CORE_TYPES + ("DOCUMENT",))
        model = kg.model_read()
        text = (f"{cov['documents']} document(s) read, {cov['with_facts']} gave a "
                f"link, {cov['with_mentions']} mention something. "
                f"{ents.get('PERSON', 0)} people, {ents.get('PART', 0)} parts, "
                f"{ents.get('PROJECT', 0)} projects"
                + (f", {others} other entities" if others else "") + "; "
                f"{rels.get('seeded', 0) + rels.get('accepted', 0)} links, "
                f"{rels.get('suggested', 0)} waiting for you. "
                f"Text read by a model: {model['documents']} of "
                f"{model['text_documents']} document(s).")
        held = kg.contents_counts()
        if held["files"]:
            text += (f" Contents: {held['files']} file(s), {held['cells']:,} value(s) under "
                     f"{held['fields']:,} header(s) and label(s) (the Contents tab).")
        if cov["unreadable"]:
            text += (f" NOT READ: {len(cov['unreadable'])} file(s) — "
                     + "; ".join(cov["unreadable"][:3])
                     + (" …" if len(cov["unreadable"]) > 3 else ""))
        skipped = cov.get("skipped_rows") or []
        if skipped:
            text += (f" ROWS NOT READ: {len(skipped)} — " + "; ".join(skipped[:3])
                     + (" …" if len(skipped) > 3 else ""))
        if cov.get("ledgers") or cov.get("workbooks"):
            text += (f" Spreadsheets: {cov.get('ledgers', 0)} Ledger(s), "
                     f"{cov.get('workbooks', 0)} workbook(s)")
            if cov.get("pictures"):
                text += (f", {cov.get('pictures_read', 0)} of {cov['pictures']} "
                         "picture(s) read")
            text += " (Spreadsheets… says what each gave)."
        if not confirmed:
            text += " No field labels are ticked (Fields…)."
        self.coverage.setText(text)

    def _fill_types(self, ents: Dict[str, int]) -> None:
        """The type filter: the fixed entries, then every other type this
        graph holds (a model's ORGANIZATION, LOCATION …)."""
        want = [t for t in sorted(ents) if t not in kgm.CORE_TYPES + ("DOCUMENT",)]
        have = [self.type_box.itemData(i) for i in range(len(TYPE_FILTERS),
                                                          self.type_box.count())]
        if have == want:
            return
        cur = self.type_box.currentData()
        self.type_box.blockSignals(True)
        while self.type_box.count() > len(TYPE_FILTERS):
            self.type_box.removeItem(self.type_box.count() - 1)
        for t in want:
            self.type_box.addItem(f"{type_icon(t)} {t.replace('_', ' ').title()}", t)
        i = self.type_box.findData(cur)
        self.type_box.setCurrentIndex(max(0, i))
        self.type_box.blockSignals(False)

    # ------------------------------------------------------------------
    def on_search(self) -> None:
        self.results.clear()
        kg = self._kg
        if kg is None:
            return
        text = self.search.text().strip()
        self._search_contents(text)
        etype = self.type_box.currentData()
        if text:
            hits = kg.search(text, etype)
        else:
            q = ("SELECT id, type, name FROM entities WHERE merged_into IS NULL"
                 " AND type != 'DOCUMENT'")
            args: tuple = ()
            if etype:
                q += " AND type=?"
                args = (etype,)
            hits = [dict(r) for r in kg.db.execute(q + " ORDER BY type, name LIMIT 500", args)]
        for h in hits:
            if h["type"] == "DOCUMENT":
                continue
            ent = kg.entity(h["id"])
            item = QListWidgetItem(f"{type_icon(h['type'])} {display_name(ent)}")
            item.setData(ROLE_DATA, h["id"])
            self.results.addItem(item)

    def _search_contents(self, text: str) -> None:
        """The Contents view shows what matches, and its tab says how many."""
        try:
            n = self.contents_view.search(text)
        except Exception as exc:                          # noqa: BLE001
            n = 0
            self.status.setText(f"The contents could not be searched: {exc}")
        i = self.views.indexOf(self.contents_view)
        self.views.setTabText(i, f"Contents ({n} matches)" if text and n else "Contents")

    def _preview_cell(self, path: str, locator: Any, where: str, quote: str) -> None:
        """A cell picked in the Contents view: previewed, and Open file opens it."""
        self._selected = {"kind": "cell", "data": {"path": path, "locator": locator}}
        self._preview(path, locator, where, quote)
        self._enable_actions()

    def _on_result(self, item, _prev=None) -> None:
        if item is None:
            return
        self.show_entity(item.data(ROLE_DATA))

    def show_entity(self, eid: str) -> None:
        kg = self._kg
        if kg is None:
            return
        self._current = eid
        self._picture_file = None          # the Picture follows the entity again
        self.tree.clear()
        ent = kg.entity(eid)
        if ent is None:
            return
        aliases = [a for a in ent["aliases"] if a != ent["name"]]
        self.status.setText(f"{type_icon(ent['type'])} {display_name(ent)}"
                            + (f" ({ent['type'].replace('_', ' ').lower()})"
                               if ent["type"] not in kgm.CORE_TYPES else "")
                            + (f" — {ent['description']}" if ent.get("description") else "")
                            + (f"   also written: {', '.join(aliases)}" if aliases else ""))
        groups: Dict[str, QTreeWidgetItem] = {}
        shown: Dict[str, int] = {}
        hidden: Dict[str, int] = {}
        # Only what is drawn is looked up; the rest of each kind is counted.
        for (pred, direction), k in kg.neighbor_counts(eid, include_tables=True).items():
            if k > RELATIONS_SHOWN:
                label = phrase(pred, direction)
                hidden[label] = hidden.get(label, 0) + k - RELATIONS_SHOWN
        for n in kg.neighbors(eid, evidence_limit=EVIDENCE_SHOWN,
                              relation_limit=RELATIONS_SHOWN, include_tables=True):
            label = phrase(n["predicate"], n["direction"])
            g = groups.get(label)
            if g is None:
                g = QTreeWidgetItem([label, "", ""])
                g.setFirstColumnSpanned(True)
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                self.tree.addTopLevelItem(g)
                groups[label] = g
            shown[label] = shown.get(label, 0) + 1
            other = n["other"] or {}
            icon = type_icon(other.get("type", ""))
            name = display_name(other) if other.get("type") != "DOCUMENT" else other.get("name", "")
            total = n.get("evidence_total", len(n["evidence"]))
            r_item = QTreeWidgetItem([f"{icon} {name}", n["status"], f"{total} source(s)"])
            r_item.setData(0, ROLE_KIND, "relation")
            r_item.setData(0, ROLE_DATA, n)
            if n["status"] == "suggested":
                r_item.setForeground(1, QColor(self._tokens["warning"]))
            elif n["status"] == "accepted":
                r_item.setForeground(1, QColor(self._tokens["success"]))
            g.addChild(r_item)
            for ev in n["evidence"]:
                e_item = QTreeWidgetItem([ev["quote"], method_words(ev["method"])
                                          + (" (changed since)" if ev["stale"] else ""),
                                          f"{ev.get('file', ev['path'])} · {ev['where']}"])
                e_item.setToolTip(0, ev["quote"])
                e_item.setData(0, ROLE_KIND, "evidence")
                e_item.setData(0, ROLE_DATA, ev)
                r_item.addChild(e_item)
            if total > len(n["evidence"]):
                r_item.addChild(QTreeWidgetItem(
                    [f"… and {total - len(n['evidence'])} more (all in the export)", "", ""]))
        for label, k in hidden.items():
            groups[label].addChild(QTreeWidgetItem([f"… and {k} more", "", ""]))
        n_mentions = kg.mention_count(eid)
        mentions = kg.mentions(eid, limit=MENTIONS_SHOWN)
        if mentions:
            g = QTreeWidgetItem([f"mentioned in ({n_mentions})", "", ""])
            f = g.font(0)
            f.setBold(True)
            g.setFont(0, f)
            self.tree.addTopLevelItem(g)
            for m in mentions:
                m_item = QTreeWidgetItem([m["snippet"], method_words(m["method"]),
                                          f"{m.get('file', m['path'])} · {m['where']}"])
                m_item.setToolTip(0, m["snippet"])
                m_item.setData(0, ROLE_KIND, "mention")
                m_item.setData(0, ROLE_DATA, m)
                g.addChild(m_item)
            if n_mentions > len(mentions):
                g.addChild(QTreeWidgetItem([f"… and {n_mentions - len(mentions)} more",
                                            "", ""]))
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(True)
        self._selected = None
        self._enable_actions()
        if self.views.currentWidget() is not self.tree:
            if self.views.currentIndex() == 1:
                self.refresh_picture()
            else:
                self.views.setCurrentWidget(self.tree)

    def _on_tree(self, item, _prev=None) -> None:
        if item is None:
            self._selected = None
            self._enable_actions()
            return
        kind = item.data(0, ROLE_KIND)
        data = item.data(0, ROLE_DATA)
        self._selected = {"kind": kind, "data": data} if kind else None
        self.answer_row.setVisible(kind == "question")
        if kind == "question":
            self._fill_answers(data["review"])
        if kind in ("evidence", "mention", "question"):
            self._preview(data["path"], data["locator"], data.get("where", ""),
                          data.get("quote") or data.get("snippet") or "")
        elif kind == "relation" and data.get("evidence"):
            ev = data["evidence"][0]
            self._preview(ev["path"], ev["locator"], ev["where"], ev.get("quote", ""))
        elif kind == "source":
            self._show_source(data)
        self._enable_actions()

    def _on_tree_double(self, item, _col=0) -> None:
        if item is None or item.data(0, ROLE_KIND) != "relation":
            return
        other = item.data(0, ROLE_DATA).get("other") or {}
        if other.get("id") and other.get("type") != "DOCUMENT":
            self.show_entity(other["id"])

    #: Workbooks are previewed on a worker: openpyxl streams to the cited row,
    #: which for row 90,000 of a big sheet takes seconds.
    _WORKBOOK_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".xlsb")

    def _preview(self, rel_path: str, locator: Any, where: str, quote: str = "") -> None:
        if not rel_path:
            return
        shown = self._kg.display_path(rel_path) if self._kg is not None else rel_path
        self.preview_title.setText(f"{shown} · {where}")
        self._preview_path = rel_path
        self._preview_token = token = object()
        disk = self._disk(rel_path)
        try:
            big = (Path(self.actions.root) / disk).stat().st_size >= PREVIEW_ASYNC_BYTES
        except OSError:
            big = False
        if Path(rel_path).suffix.lower() not in self._WORKBOOK_SUFFIXES and not big:
            self._show_preview(token, self.actions.context(disk, locator, quote))
            return
        self.preview.setPlainText("Reading the file…")

        def work() -> None:
            try:
                lines = self.actions.context(disk, locator, quote)
            except Exception:                             # noqa: BLE001
                lines = []
            self._to_ui(self._show_preview, token, lines)

        threading.Thread(target=work, name="kg-preview", daemon=True).start()

    def _show_preview(self, token, lines) -> None:
        if token is not getattr(self, "_preview_token", None):
            return                       # another spot was clicked meanwhile
        if not lines:
            self.preview.setPlainText("(this spot could not be read)")
            return
        width = max(len(g) for g, _t, _hit in lines)
        self.preview.setPlainText("\n".join(
            f"{'▶' if hit else ' '} {g.rjust(width)} │ {t}" for g, t, hit in lines))

    def _show_source(self, data: Dict[str, Any]) -> None:
        self._preview_path = data.get("path")
        self._preview_token = None
        self.preview_title.setText(data.get("file") or data.get("path", ""))
        body = [data.get("text", "")]
        if data.get("notes"):
            body += ["", "Notes:"] + [f"  • {n}" for n in data["notes"]]
        self.preview.setPlainText("\n".join(body))

    def _enable_actions(self) -> None:
        cur = self._kg.entity(self._current) if (self._kg and self._current) else None
        self.merge_btn.setEnabled(bool(cur and cur["type"] != "DOCUMENT"))
        self.split_btn.setEnabled(bool(cur and self._kg.merged_into_me(self._current)))
        sel = self._selected or {}
        has_file = sel.get("kind") in ("evidence", "mention", "relation", "question", "source",
                                       "cell")
        self.open_btn.setEnabled(bool(has_file and getattr(self, "_preview_path", None)))
        is_rel = sel.get("kind") == "relation"
        status = (sel.get("data") or {}).get("status") if is_rel else None
        self.accept_btn.setEnabled(is_rel and status != "accepted")
        self.reject_btn.setEnabled(is_rel and status != "rejected")
        self.forget_btn.setEnabled(sel.get("kind") == "answer" and not self._busy)

    # ------------------------------------------------------------------
    def on_open_file(self) -> None:
        path = getattr(self, "_preview_path", None)
        if not path:
            return
        try:
            self.actions.open_file(self._disk(path))
        except Exception as exc:                          # noqa: BLE001
            self.status.setText(f"Could not open {path}: {exc}")

    def _decide(self, status: str) -> None:
        sel = self._selected or {}
        if sel.get("kind") != "relation" or self._kg is None:
            return
        rel = sel["data"]
        self._kg.set_relation_status(rel["relation_id"], status)
        self.status.setText(f"Link {status}. A rebuild keeps your decision.")
        if self._current:
            self.show_entity(self._current)
        self._show_coverage()

    # ------------------------------------------------------------------
    def on_rebuild(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.rebuild_btn.setEnabled(False)
        self.status.setText("Reading the documents…")
        if self._kg is not None:          # the worker writes with its own connection
            self._kg.close()
            self._kg = None

        self._rebuild_token = token = object()

        def progress(i: int, n: int) -> None:
            pass                     # the progress lines below say more

        def line(text: str, _fraction) -> None:
            # 'reading documents: 40/200 files (20%) · x.xlsx read · 0:12',
            # and 'still working …' while nothing moves (the terminal gets
            # the same lines)
            self._to_ui(self._progress_line, token, text)

        def work() -> None:
            try:
                stats = self.actions.rebuild(progress, listener=line)
                self._to_ui(self._rebuilt, stats, None)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self._rebuilt, None, exc)

        threading.Thread(target=work, name="kg-rebuild", daemon=True).start()

    def hold_for_rebuild(self, who: str) -> bool:
        """Another tab (the Ledger's Rebuild) is about to rebuild the graph:
        let go of the store - the rebuild writes with its own connection, and
        a search here waited on its lock on the GUI thread for 5-8 s, then
        failed 'database is locked' (review, 2026-10-19) - and keep Rebuild
        off until rebuild_done_elsewhere. False when this tab is rebuilding
        already (one rebuild at a time)."""
        if self._busy:
            return False
        self._busy = True
        self.rebuild_btn.setEnabled(False)
        if self._kg is not None:
            self._kg.close()
            self._kg = None
        self.status.setText(f"The {who} tab is rebuilding the graph…")
        return True

    def graph_in_use(self) -> bool:
        """Whether this tab has the graph's store for a while - its rebuild,
        one it holds for, Suggest links, the label harvest, a tick: another
        tab that would write to it waits for it (LedgerTab._register)."""
        return self._busy

    def rebuild_done_elsewhere(self, stats: Optional[Dict[str, Any]],
                               exc: Optional[BaseException]) -> None:
        """The rebuild hold_for_rebuild waited for ended: reopen and show
        its summary, as after this tab's own."""
        self._rebuilt(stats, exc)

    def _progress_line(self, token, text: str) -> None:
        # Only while THIS rebuild runs: a line that arrives after it ended
        # (the reporter's heartbeat can fire once more) must not hide the
        # result.
        if self._busy and token is getattr(self, "_rebuild_token", None):
            self.status.setText(f"Rebuilding: {text}")

    def _rebuilt(self, stats: Optional[Dict[str, Any]], exc: Optional[BaseException]) -> None:
        self._busy = False
        self._rebuild_token = None
        self.rebuild_btn.setEnabled(True)
        self.retry_skipped_btn.refresh()
        self._reopen()
        read_text, self._read_text_after = self._read_text_after, False
        if exc is not None:
            self.stop_btn.setEnabled(False)
            self.status.setText(f"The rebuild failed: {exc}")
            return
        # The view is refreshed FIRST: show_entity writes the open item's
        # header into the status line, and the summary set before it was
        # gone at once ('⚙ 101' was all a rebuild said - review, 2026-10-19).
        self.on_search()
        if self._current:
            self.show_entity(self._current)
        from council_core import knowledge_graph as kgm
        line = kgm.rebuild_summary_line(stats)
        # The seed lists them only at 0 links or with no rule ticked yet
        if self._offer_rules(stats.get("rules_to_tick") or []):
            n = len(self._to_tick)
            line += (f" Your documents use {n} label{'s' if n != 1 else ''} the graph does "
                     "not read yet: 'Tick the suggested field rules' reads them.")
        self.status.setText(line)
        # The first few unreadable entries also go to the tooltip, where
        # the user can hover without having to scroll a dialog.
        reasons = stats.get("unreadable") or []
        if reasons:
            preview = "\n".join(reasons[:12])
            if len(reasons) > 12:
                preview += f"\n…and {len(reasons) - 12} more."
            self.status.setToolTip("Unreadable files:\n" + preview)
            # Also print the full list to the terminal so a Barbie / Pi
            # run that bypasses the UI still has it.
            print("[kg-rebuild] unreadable files:", flush=True)
            for line in reasons:
                print(f"  {line}", flush=True)
        else:
            self.status.setToolTip("")
        if read_text and not self._stop.is_set():
            self.on_suggest()                   # the model's part of ⟳ Build graph
        elif self._related_after and not self._stop.is_set():
            self.on_related()                   # the embedding model's part
        else:
            self._related_after = False
            self.stop_btn.setEnabled(False)
            self.refresh_picture()

    def refresh_models(self) -> None:
        """List the installed US-origin models and the Pi nodes, on a worker:
        it asks Ollama over HTTP, which the tab's constructor used to do on
        the GUI thread (and every test that built the tab reached the real
        Ollama port)."""

        def work() -> None:
            try:
                models = self.actions.local_models()
            except Exception:                             # noqa: BLE001
                models = []
            try:
                pis, skipped = self.actions.pi_workers(), self.actions.pi_skipped()
            except Exception:                             # noqa: BLE001
                pis, skipped = [], []
            try:
                ggufs = getattr(self.actions, "gguf_models", lambda: [])()
            except Exception:                             # noqa: BLE001
                ggufs = []
            self._to_ui(self._models_ready, models, pis, skipped, ggufs)

        threading.Thread(target=work, name="kg-models", daemon=True).start()

    def _models_ready(self, models, pis, skipped, ggufs=()) -> None:
        self.model_box.clear()
        for m in models:
            self.model_box.addItem(m, m)
        for label, path in ggufs:                    # GGUF files on this PC too
            self.model_box.addItem(label, path)
        self.model_box.addItem("No model — labelled fields and tables only", NO_MODEL)
        if not models and not ggufs:
            self.model_box.setToolTip(
                "No local model is installed, so the sentences of reports and notes are "
                "not read. Install a US-origin model yourself (gemma3:12b reads best: "
                "'ollama pull gemma3:12b') and reopen the tab.")
        self.pis_box.setEnabled(bool(pis))
        tip = (", ".join(f"{n} ({m})" for n, _u, m in pis) if pis else
               "No Pi nodes to share the work: register one (Apothecary → Set up a Pi) "
               "and enable remote nodes (COUNCIL_REMOTE_NODES=1).")
        if skipped:
            tip += "\n" + "\n".join(f"{n} skipped: {m} is not a US-origin model"
                                    for n, m in skipped)
        self.pis_box.setToolTip(tip)

    def on_build(self) -> None:
        """⟳ Build graph (Morphik's "create graph"): read the documents, then
        - with a model chosen - have it read every passage of free text.
        Both resume where a stopped build left off."""
        if self._busy:
            return
        self._stop.clear()
        self._read_text_after = bool(self.model_box.currentData())
        self._related_after = bool(self._kg is not None and self._kg.embed_model())
        self.stop_btn.setEnabled(True)
        self.on_rebuild()

    def on_suggest(self) -> None:
        """The model's part of a build on its own: every passage of free text
        read by the chosen model (and Pis, if ticked); what it finds goes in
        the graph at once, or waits for you, as the graph is set."""
        model = self.model_box.currentData()
        if self._busy or not model:
            return
        self._busy = True
        self._stop.clear()
        self.rebuild_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        use_pis = self.pis_box.isChecked()
        self.status.setText(f"Reading the text with {Path(str(model)).name}…")
        if self._kg is not None:
            self._kg.close()
            self._kg = None

        def progress(done, total, worker):
            self._to_ui(self.status.setText,
                        f"Reading the text: {done} of {total} passages (last by {worker})")

        def work() -> None:
            try:
                stats = self.actions.read_text(model, use_pis, progress, self._stop.is_set)
                self._to_ui(self._suggested, stats, None)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self._suggested, None, exc)

        threading.Thread(target=work, name="kg-read-text", daemon=True).start()

    def on_related(self) -> None:
        """Related by meaning, the last part of ⟳ Build graph: the graph's
        embedding model reads what is new or changed, and close documents and
        tables get a suggested link (KnowledgeGraph.find_related)."""
        self._related_after = False
        if self._busy:
            return
        self._busy = True
        self.rebuild_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Finding related items…")
        if self._kg is not None:
            self._kg.close()
            self._kg = None

        def progress(done, total, path):
            self._to_ui(self.status.setText,
                        f"Finding related items: read {done} of {total} new or changed "
                        f"({path})")

        def work() -> None:
            try:
                stats = self.actions.find_related(progress, self._stop.is_set)
                self._to_ui(self._related_done, stats, None)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self._related_done, None, exc)

        threading.Thread(target=work, name="kg-related", daemon=True).start()

    def _related_done(self, stats, exc) -> None:
        self._busy = False
        self.rebuild_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._reopen()
        if exc is not None:
            self.status.setText(f"Finding related items failed: {exc}")
            return
        self.on_search()
        if self._current:
            self.show_entity(self._current)
        if stats is None:
            text = "No embedding model is set for this graph (Graph settings…)."
        elif stats["stopped"]:
            text = (f"Stopped after reading {stats['embedded']} item(s) — ⟳ Build graph again "
                    "to continue.")
        else:
            text = (f"Related by meaning: {stats['links']} link(s) suggested between "
                    f"{stats['items']} documents and tables ({stats['embedded']} read now). "
                    "✓ Accept or ✗ Reject them in Links.")
        self.status.setText(text)
        self.refresh_picture()

    def on_stop_suggest(self) -> None:
        self._stop.set()
        self._read_text_after = False
        self._related_after = False
        self.status.setText("Stopping after the passages in progress — ⟳ Build graph again "
                            "to continue where it stopped.")

    def _suggested(self, stats, exc) -> None:
        self._busy = False
        self.rebuild_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._reopen()
        if exc is not None:
            self.status.setText(f"Reading the text failed: {exc}")
            return
        word = "Stopped" if stats["stopped"] else "Built"
        waits = self._kg is not None and self._kg.model_link_status() == "suggested"
        text = (f"{word}: the model read {stats['done']} of {stats['chunks']} new passage(s) "
                f"and found {stats['entities']} entities ({stats.get('new_entities', 0)} new) "
                f"and {stats['links']} link(s); {stats['rejected']} claim(s) the text does "
                "not support were left out"
                + (f"; {stats['errors']} passage(s) got no usable answer "
                   f"({stats.get('first_error', '')})" if stats["errors"] else "")
                + (". Its links wait for you (✓ Accept / ✗ Reject)." if waits else "."))
        if not stats["chunks"]:
            text = "Built. The model had read every passage already."
        # The view first: show_entity writes the open item's header into the
        # status line (as after a rebuild - review, 2026-10-19)
        self.on_search()
        if self._current:
            self.show_entity(self._current)
        self.status.setText(text)
        if self._related_after and not stats["stopped"] and not self._stop.is_set():
            self.on_related()                   # the embedding model's part
            return
        self._related_after = False
        self.refresh_picture()

    # -- graphs ------------------------------------------------------------
    def _fill_graphs(self, select: Optional[str] = None) -> None:
        try:
            graphs = self.actions.graphs()
        except kgg.GraphListDamaged as exc:
            self.status.setText(str(exc))
            graphs = [kgg.GraphInfo(kgg.MAIN, kgg.MAIN_NAME)]
        want = select or self.actions.graph_id
        self.graph_box.blockSignals(True)
        self.graph_box.clear()
        for g in graphs:
            self.graph_box.addItem(g.name, g.id)
        i = self.graph_box.findData(want)
        self.graph_box.setCurrentIndex(max(0, i))
        self.graph_box.blockSignals(False)
        self.actions.graph_id = self.graph_box.currentData() or kgg.MAIN
        self.delete_graph_btn.setEnabled(self.actions.graph_id != kgg.MAIN)

    def on_graph_changed(self) -> None:
        gid = self.graph_box.currentData() or kgg.MAIN
        if self._busy:
            self._fill_graphs(self.actions.graph_id)      # one graph at a time
            self.status.setText("The graph is busy: choose another when it finishes.")
            return
        self.actions.graph_id = gid
        self.delete_graph_btn.setEnabled(gid != kgg.MAIN)
        self._current = None
        self.tree.clear()
        self._reopen()
        self.on_search()
        self.refresh_picture()
        self.status.setText(f"Graph: {self.graph_box.currentText()}.")

    def _graph_dialog(self, new: bool, current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        from .. import dialogs as _dialogs
        if _dialogs.disabled():
            return None
        try:
            models = self.actions.embed_models()
        except Exception:                                 # noqa: BLE001
            models = []
        dlg = GraphDialog(new=new, pick=self.ask_folder, parent=self, embed_models=models,
                          **current)
        self._graph_dialog_open = dlg          # tests reach it here
        return dlg.values() if dlg.exec() == QDialog.DialogCode.Accepted else None

    def on_new_graph(self) -> None:
        if self._busy:
            return
        got = self.ask_graph(True, {})
        if not got:
            return
        try:
            info = self.actions.create_graph(got)
        except (ValueError, kgg.GraphListDamaged) as exc:
            self.status.setText(f"No graph made: {exc}")
            return
        self._fill_graphs(info.id)
        self.on_graph_changed()
        self.status.setText(f"Made the graph '{info.name}'. Choose a model and ⟳ Build graph "
                            "to read its documents.")

    def on_graph_settings(self) -> None:
        if self._busy or self._kg is None:
            return
        gid = self.actions.graph_id
        try:
            info = next(g for g in self.actions.graphs() if g.id == gid)
        except (StopIteration, kgg.GraphListDamaged):
            return
        current = {"name": info.name, "folders": list(info.folders),
                   "suffixes": list(info.suffixes), "focus": self._kg.focus(),
                   "model_links": "review" if self._kg.model_link_status() == "suggested"
                   else "graph", "use_in_answers": info.use_in_answers,
                   "is_main": info.is_main, "embed_model": self._kg.embed_model(),
                   "related_threshold": self._kg.related_threshold()}
        got = self.ask_graph(False, current)
        if not got:
            return
        if self._kg is not None:
            self._kg.close()
            self._kg = None
        try:
            self.actions.update_graph(gid, got)
        except (ValueError, KeyError, kgg.GraphListDamaged, kgm.KnowledgeGraphBusy) as exc:
            self.status.setText(f"Settings not saved: {exc}")
        else:
            changed_focus = (got.get("focus", "") != current["focus"])
            self.status.setText("Settings saved." + (
                " The model reads the text again with the new focus at the next ⟳ Build "
                "graph." if changed_focus else ""))
        self._fill_graphs(gid)
        self._reopen()
        self.on_search()

    def on_delete_graph(self) -> None:
        gid = self.actions.graph_id
        if self._busy or gid == kgg.MAIN:
            return
        name = self.graph_box.currentText()
        if not self.ask_yes_no("Delete graph?", f"Delete the graph '{name}'? What building it "
                               "made (its entities, links and your decisions on them) goes; "
                               "no document is touched.", parent=self):
            return
        if self._kg is not None:
            self._kg.close()
            self._kg = None
        try:
            self.actions.delete_graph(gid)
        except (ValueError, KeyError, OSError, kgg.GraphListDamaged) as exc:
            self.status.setText(f"Not deleted: {exc}")
            self._reopen()
            return
        self.actions.graph_id = kgg.MAIN
        self._fill_graphs(kgg.MAIN)
        self.on_graph_changed()
        self.status.setText(f"Deleted the graph '{name}'.")

    # -- the picture ---------------------------------------------------------
    def _on_view_changed(self, index: int) -> None:
        if index == 1:
            self.refresh_picture()

    def refresh_picture(self, whole: bool = False) -> None:
        """Lay out what to draw on a worker: the entity open in the tab and
        what is within the chosen number of links of it - or, with nothing
        open (or Whole graph), the best-connected entities."""
        if self.views.currentIndex() != 1 or self._busy:
            return
        if whole:
            self._picture_file = None
        center = None if whole else self._current
        hops = self.hops_box.value()
        doc = self._picture_file
        self._picture_token = token = object()

        def work() -> None:
            try:
                got, err = (self.actions.contents_picture(doc) if doc else
                            self.actions.picture(center, hops)), ""
            except Exception as exc:                      # noqa: BLE001
                got, err = None, str(exc) or exc.__class__.__name__
            self._to_ui(self._show_picture, token, got, err, center)

        threading.Thread(target=work, name="kg-picture", daemon=True).start()

    def _show_picture(self, token, got, err: str, center) -> None:
        if token is not self._picture_token:
            return
        if got is None:
            self.canvas.set_graph([], [], {}, f"The picture could not be made: {err}")
            return
        nodes, edges, pos = got
        msg = ("Nothing linked yet. ⟳ Build graph (with a model, to read the text too)."
               if center is None else "")
        self.canvas.set_graph(nodes, edges, pos, msg)
        types = self.canvas.types()
        self.legend.setText("  ".join(
            f"<span style='color:{type_colour(t)}'>●</span> {t.replace('_', ' ').lower()}"
            for t in types[:10]) + (f"  ({len(nodes)} shown)" if nodes else ""))

    def draw_file(self, document_id: str) -> None:
        """The Picture of one file's contents (the Contents tab's Draw)."""
        self._picture_file = document_id
        if self.views.currentIndex() != 1:
            self.views.setCurrentIndex(1)       # refreshes the picture
        else:
            self.refresh_picture()

    def _on_canvas_click(self, eid: str) -> None:
        if eid.startswith("c:"):
            return                  # a file's contents: double-click a file to draw it
        if eid and self._kg is not None and self._kg.entity(eid) is not None:
            self._picture_file = None
            self._current = eid
            # the links of what was clicked, without leaving the picture
            idx = self.views.currentIndex()
            self.show_entity(eid)
            self.views.setCurrentIndex(idx)

    def _on_canvas_centre(self, eid: str) -> None:
        if eid.startswith("c:file:"):
            self.draw_file(eid[len("c:file:"):])
            return
        if eid.startswith("c:"):
            return
        self._on_canvas_click(eid)
        self.refresh_picture()

    # -- asking the graph ------------------------------------------------------
    def on_ask(self) -> None:
        question = " ".join(self.ask_edit.text().split())
        if not question or self._asking:
            return
        model = self.model_box.currentData() or NO_MODEL
        self._asking = True
        self.ask_btn.setEnabled(False)
        self.views.setCurrentIndex(2)
        self.answer.setPlainText("Looking in the graph…" + (
            f" ({Path(str(model)).name} answers from what it finds)" if model else ""))
        self.facts.clear()

        def work() -> None:
            try:
                got, err = self.actions.ask(question, model), ""
            except Exception as exc:                      # noqa: BLE001
                got, err = None, str(exc) or exc.__class__.__name__
            self._to_ui(self._show_answer, question, got, err)

        threading.Thread(target=work, name="kg-ask", daemon=True).start()

    def _show_answer(self, question: str, got: Optional[Dict[str, Any]], err: str) -> None:
        self._asking = False
        self.ask_btn.setEnabled(True)
        self.facts.clear()
        if got is None:
            self.answer.setPlainText(f"Could not ask the graph: {err}")
            return
        parts = [f"Q: {question}", ""]
        if got.get("answer"):
            parts.append(got["answer"])
        elif got.get("error"):
            parts.append(f"({got['error'][0].upper()}{got['error'][1:]}.)")
        elif got.get("text"):
            parts.append("No model chosen: here is what the graph holds - the facts below.")
        self.answer.setPlainText("\n".join(parts))
        for ctx in got.get("contexts") or []:
            if ctx["entities"]:
                self._answer_facts(ctx)
            if ctx.get("rows"):                           # after the facts it names
                self._answer_rows(ctx["rows"])

    def _answer_facts(self, ctx: Dict[str, Any]) -> None:
        """The graph's facts about the entities an answer named."""
        names = ", ".join(display_name(e) for e in ctx["entities"])
        g = QTreeWidgetItem([f"About {names}", "", ""])
        g.setFirstColumnSpanned(True)
        f = g.font(0)
        f.setBold(True)
        g.setFont(0, f)
        self.facts.addTopLevelItem(g)
        for fact in ctx["facts"]:
            item = QTreeWidgetItem([
                f"[{fact.n}] {display_name(fact.subject)} — {phrase(fact.predicate, 'out')}"
                f" → {display_name(fact.object)}", fact.status, ""])
            item.setData(0, ROLE_KIND, "relation")
            item.setData(0, ROLE_DATA, {"relation_id": fact.relation_id,
                                        "status": fact.status, "evidence": fact.evidence,
                                        "other": fact.object})
            g.addChild(item)
            for ev in fact.evidence:
                e_item = QTreeWidgetItem([ev["quote"], method_words(ev["method"]),
                                          f"{ev.get('file', ev['path'])} · {ev['where']}"])
                e_item.setToolTip(0, ev["quote"])
                e_item.setData(0, ROLE_KIND, "evidence")
                e_item.setData(0, ROLE_DATA, ev)
                item.addChild(e_item)
        g.setExpanded(True)

    def _answer_rows(self, rows: List[Dict[str, Any]]) -> None:
        """The rows of the files an answer used (the Contents layer), each
        previewed when picked."""
        g = QTreeWidgetItem(["Rows of your files", "", ""])
        g.setFirstColumnSpanned(True)
        f = g.font(0)
        f.setBold(True)
        g.setFont(0, f)
        self.facts.addTopLevelItem(g)
        for r in rows:
            cells = " | ".join(f"{c['label']}: {c['text']}" for c in r["cells"])
            head = r["file"] + (f" › {r['sheet']}" if r.get("sheet") else "")
            item = QTreeWidgetItem([f"[{r['n']}] {cells}", "", f"{head} · {r['where']}"])
            item.setToolTip(0, cells)
            item.setData(0, ROLE_KIND, "evidence")
            item.setData(0, ROLE_DATA, {"path": r["path"], "locator": r["locator"],
                                        "where": r["where"], "file": r["file"],
                                        "quote": (r.get("matched") or [""])[0]})
            g.addChild(item)
        g.setExpanded(True)

    def on_fields(self) -> None:
        if self._busy or self._kg is None:
            return
        self._busy = True
        self.status.setText("Reading the labels your documents use…")

        def work() -> None:
            try:
                sugg = self.actions.label_suggestions()
            except Exception:                             # noqa: BLE001
                sugg = []
            self._to_ui(self._show_fields, sugg)

        threading.Thread(target=work, name="kg-labels", daemon=True).start()

    def _show_fields(self, suggestions) -> None:
        self._busy = False
        self.status.setText("")
        if self._kg is None:
            return
        dlg = FieldsDialog(self._kg.field_rules(), suggestions, self)
        self._fields_dialog = dlg          # tests reach it here
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.apply_fields(*dlg.choices())

    def apply_fields(self, rules: Dict[str, bool], synonyms) -> None:
        kg = self._kg
        if kg is None:
            return
        current = {r.label: s for r, s in kg.field_rules()}
        for label, on in rules.items():
            want = "confirmed" if on else "proposed"
            if current.get(label) not in (want, None):
                kg.set_rule_status(label, want)
        for label, means in synonyms:
            kg.add_label_synonym(label, means)
        self.status.setText("Fields saved. ⟳ Build graph to read the documents with them.")
        self._offer_rules([])                 # chosen one by one: nothing to offer
        self._show_coverage()

    # -- why no links (user, 2026-10-20) ---------------------------------
    def on_why(self) -> None:
        """What the graph read and why it has no links, in a dialog with
        Copy. Worked out on a worker with its own connection: it reads the
        rebuild's cache (and a CSV's header row)."""
        if self._why_running:
            return              # a double click started two workers, two dialogs (review)
        if self._busy:
            self.status.setText("The graph is busy (a rebuild, or reading labels): ask "
                                "'Why no links?' again when it finishes.")
            return
        self._why_running = True
        self.status.setText("Working out why…")

        def work() -> None:
            try:
                diag, err = self.actions.why_no_links(), ""
            except kgm.KnowledgeGraphBusy:
                diag, err = None, "the graph is being rebuilt elsewhere; ask again when it finishes"
            except Exception as exc:                      # noqa: BLE001
                diag, err = None, str(exc) or exc.__class__.__name__
            self._to_ui(self._show_why, diag, err)

        threading.Thread(target=work, name="kg-why", daemon=True).start()

    def _show_why(self, diag: Optional[Dict[str, Any]], err: str = "") -> None:
        self._why_running = False
        if diag is None:
            self.status.setText(f"Could not work out why: {err}.")
            return
        reason = diag.get("reason")
        if diag["links"]:
            self.status.setText(f"{diag['links']} link(s)." + (
                kgm._sentence(f" More could come: {reason['short']}") if reason else ""))
        elif reason is not None:
            self.status.setText(kgm._sentence(f"No links: {reason['short']}"))
        rules = diag["rules"]
        if not diag["links"] or not rules["confirmed"]:
            self._offer_rules(rules["proposed_seen"])
        # One dialog, its text brought up to date: each ask used to leave
        # another one open behind it (review, 2026-10-20). Closed, it is
        # gone (WA_DeleteOnClose) and the next ask makes a new one.
        dlg = self._why_dialog
        if dlg is not None and _alive(dlg):
            dlg.set_text(diag["text"])
        else:
            dlg = self._why_dialog = WhyNoLinksDialog(diag["text"], self)
        dlg.show()                          # not modal: the tab stays usable beside it
        dlg.raise_()
        dlg.activateWindow()

    def _offer_rules(self, labels) -> bool:
        """Offer to tick these proposed field rules (the documents use their
        labels), or stop offering (``[]``). True when on offer."""
        self._to_tick = list(labels)
        n = len(self._to_tick)
        self.tick_btn.setText(amp(f"Tick the suggested field rules ({n})"))
        self.tick_btn.setToolTip("Reads " + ", ".join(self._to_tick[:12])
                                 + (" …" if n > 12 else "") if n else "")
        self.tick_btn.setVisible(bool(n))
        return bool(n)

    def on_tick_suggested(self) -> None:
        """Tick the proposed field rules whose labels the documents use, once
        the user says yes, then rebuild. Every rule starts proposed, so a
        first rebuild of raw workbooks gave 1 link of the 25 they hold and
        nothing said why (user, 2026-10-20). Never ticked without asking."""
        labels = list(self._to_tick)
        if not labels:
            return
        if self._busy:
            self.status.setText("The graph is busy: tick the suggested field rules when it "
                                "finishes.")
            return
        shown = ", ".join(f"'{x}'" for x in labels[:10]) + (
            f" and {len(labels) - 10} more" if len(labels) > 10 else "")
        if not self.ask_yes_no(
                "Tick the suggested field rules?",
                f"Your documents use {len(labels)} label(s) the graph does not read yet: "
                f"{shown}.\n\nTick them, so the values under them become people, parts and "
                "projects, and rebuild now? You can untick any of them in Fields….",
                parent=self):
            self.status.setText("Nothing ticked. Fields… lets you choose them one by one.")
            return
        # On a worker, all in one transaction: on the GUI thread a store held
        # elsewhere froze the window ~7 s, then 'database is locked' went
        # uncaught with some rules ticked and the rest not (review,
        # 2026-10-20).
        self._busy = True
        self.tick_btn.setEnabled(False)
        self.status.setText(f"Ticking {len(labels)} field rule(s)…")

        def work() -> None:
            try:
                ticked, err = self.actions.tick_rules(labels), ""
            except kgm.KnowledgeGraphBusy:
                ticked, err = None, "busy"
            except Exception as exc:                      # noqa: BLE001
                ticked, err = None, ("busy" if kgm._busy(exc) else
                                     str(exc) or exc.__class__.__name__)
            self._to_ui(self._ticked, ticked, err)

        threading.Thread(target=work, name="kg-tick", daemon=True).start()

    def _ticked(self, ticked: Optional[List[str]], err: str = "") -> None:
        self._busy = False
        self.tick_btn.setEnabled(True)
        if ticked is None:
            # still on offer: nothing was ticked
            self.status.setText(
                "The graph is busy (being rebuilt or written elsewhere): nothing was ticked. "
                "Press 'Tick the suggested field rules' again in a moment." if err == "busy"
                else f"Nothing was ticked: {err}.")
            return
        self._offer_rules([])
        self.status.setText(f"Ticked {len(ticked)} field rule(s). Rebuilding…")
        self.on_rebuild()

    def on_questions(self) -> None:
        """The open questions (an initial that fits two people, an inferred
        name). Select one to answer it (KG3); "Your answers" lists the
        answers given, each can be forgotten."""
        if self._kg is None:
            return
        self.tree.clear()
        self._current = None
        reviews = self._kg.reviews()
        self.status.setText(f"{len(reviews)} open question(s).")
        for r in reviews:
            names = " or ".join(display_name(c) for c in r["candidates"] if c)
            if r["kind"] == "initials":
                # An inspector's initials no Ledger people list names: asked
                # once, whatever number of rows they signed.
                q = f"Who is '{r['surface']}'?" + (f" Perhaps {names}." if names else "")
                q += f"  ({r['snippet']})" if r.get("snippet") else ""
            elif r["kind"] == "ambiguous_name":
                q = f"Is '{r['surface']}' {names}?"
            else:
                q = f"'{r['surface']}' was read as {names} — right?"
            item = QTreeWidgetItem([q, r["kind"].replace("_", " "),
                                    f"{r.get('file') or r['path']} · {r['where']}"])
            item.setToolTip(0, q)
            item.setData(0, ROLE_KIND, "question")
            item.setData(0, ROLE_DATA, {"path": r["path"], "locator": r["locator"],
                                        "where": r["where"], "review": r})
            self.tree.addTopLevelItem(item)

    def on_folders(self) -> None:
        """Folders outside the vault for the graph to read as well (read
        only), kept per vault in the graph's store."""
        if self._kg is None or self._busy:
            return
        dlg = FoldersDialog(str(self.actions.root), self._kg.extra_roots(),
                            self.add_folder, self.remove_folder, self.ask_folder, self)
        self._folders_dialog = dlg          # tests reach it here
        dlg.exec()
        if dlg.changed:
            self.status.setText("Folders changed. ⟳ Build graph to read them.")

    def add_folder(self, path: str) -> str:
        """'' when added, else why not (in words)."""
        if self._kg is None:
            return "The graph is not open."
        try:
            self._kg.add_root(path)
        except ValueError as exc:
            return str(exc)
        return ""

    def remove_folder(self, rid: str) -> str:
        if self._kg is None:
            return "The graph is not open."
        try:
            self._kg.remove_root(rid)
        except KeyError:
            return "That folder is not in the list."
        return ""

    def _disk(self, rel: str) -> str:
        """What actions.context / open_file get for a document: its path
        relative to data_in, or, for an added folder's document, where it
        is on disk (they resolve it against data_in, and an absolute path
        wins)."""
        if self._kg is not None and str(rel or "").startswith("<"):
            return str(self._kg.abs_path(rel))
        return rel

    def on_sources(self) -> None:
        """The spreadsheets the graph read - each Ledger (parent CSV) and
        workbook, what reading it gave, and the ones it could not read, with
        why - in plain words. Select one for its notes."""
        if self._kg is None:
            return
        self.tree.clear()
        self._current = None
        self._selected = None
        sources = self._kg.sources()
        self.status.setText(f"{len(sources)} spreadsheet(s). Select one to see what "
                            "reading it gave.")
        groups: Dict[str, QTreeWidgetItem] = {}
        for title, key in (("Ledgers (parent CSVs: values you accepted)", "ledger"),
                           ("Workbooks", "workbook"), ("Not read", "bad")):
            g = QTreeWidgetItem([title, "", ""])
            g.setFirstColumnSpanned(True)
            f = g.font(0)
            f.setBold(True)
            g.setFont(0, f)
            groups[key] = g
        for s in sources:
            key = "bad" if s["status"] != "ok" else s["kind"]
            item = QTreeWidgetItem([s["text"], s["kind"], s.get("file", s["path"])])
            item.setToolTip(0, s["text"])
            item.setData(0, ROLE_KIND, "source")
            item.setData(0, ROLE_DATA, s)
            groups.get(key, groups["workbook"]).addChild(item)
        for g in groups.values():
            if g.childCount():
                self.tree.addTopLevelItem(g)
                g.setExpanded(True)
        self._enable_actions()

    # -- KG3 ---------------------------------------------------------------
    def _fill_answers(self, review) -> None:
        self.answer_box.clear()
        for c in review["candidates"]:
            if c:
                self.answer_box.addItem(display_name(c), c["id"])
        if review["kind"] == "initials":
            self.answer_box.addItem("Someone else — type their name…", NEW_PERSON)
            self.answer_box.addItem(f"Keep '{review['surface']}' as a person of their own",
                                    None)
        else:
            self.answer_box.addItem("Neither — a different person", None)
        self.everywhere_box.setChecked(review["kind"] in ("inferred_alias", "initials"))

    def on_answer(self) -> None:
        sel = self._selected or {}
        if sel.get("kind") != "question" or self._kg is None:
            return
        review = sel["data"]["review"]
        eid = self.answer_box.currentData()
        who = self.answer_box.currentText()
        if eid == NEW_PERSON:
            name = self.ask_string("Who is it?", f"Whose initials are '{review['surface']}'? "
                                   "Type their full name:", parent=self)
            name = " ".join(str(name or "").split())
            if not name:
                return
            try:
                eid = self._kg.add_person(name)
            except ValueError as exc:
                self.status.setText(str(exc))
                return
            who = name
        self._kg.answer_review(review["id"], eid, everywhere=self.everywhere_box.isChecked())
        self.answer_row.setVisible(False)
        self.status.setText(f"'{review['surface']}' is {who}"
                            + (" everywhere" if self.everywhere_box.isChecked() else
                               " in that document") + ". Rebuilding to apply it…")
        self.on_rebuild()

    def on_merge(self) -> None:
        """Merge another entity INTO the one shown (two spellings, one thing)."""
        if self._kg is None or not self._current:
            return
        cur = self._kg.entity(self._current)
        text = self.ask_string("Same as…", f"Which entry is the same {cur['type'].lower()} "
                               f"as {display_name(cur)}? Type its name:", parent=self)
        if not text:
            return
        hits = [h for h in self._kg.search(text, cur["type"]) if h["id"] != self._current]
        if len(hits) > 1:
            # The one entry whose name or alias IS what was typed is the one
            # meant: 'JB' (a person of their own) beside a Jill Brown whose
            # initials are also JB was '2 entries match', and nothing more
            # of the name could be typed (review, 2026-10-19).
            exact = [h for h in self._kg.search(text, cur["type"], exact=True)
                     if h["id"] != self._current]
            if len(exact) == 1:
                hits = exact
        if len(hits) != 1:
            self.status.setText(f"{len(hits)} entries match '{text}' — type more of the name.")
            return
        other = self._kg.entity(hits[0]["id"])
        if not self.ask_yes_no("Merge?", f"Treat '{display_name(other)}' as "
                               f"'{display_name(cur)}'? Its names and links move here; you "
                               "can split it off again.", parent=self):
            return
        clashes = self._kg.merge(self._current, other["id"])
        # The user's accept/reject on the merged-in entry's link is NOT copied
        # over this entry's own link (it was: a rejected duplicate link hid a
        # labelled one for good). Ask, one link at a time.
        for c in clashes:
            ent = self._kg.entity(c["object_id"] if c["subject_id"] == self._current
                                  else c["subject_id"])
            direction = "out" if c["subject_id"] == self._current else "in"
            link = f"'{phrase(c['predicate'], direction)} {display_name(ent)}'"
            if self.ask_yes_no(
                    "Same decision here?",
                    f"You {c['theirs']} {display_name(other)}'s link {link}. "
                    f"{display_name(cur)}'s own link is {c['kept']}. Make it "
                    f"{c['theirs']} too?", parent=self):
                self._kg.set_relation_status(c["relation_id"], c["theirs"])
        self.status.setText(f"'{display_name(other)}' merged into '{display_name(cur)}'.")
        self.show_entity(self._current)
        self.on_search()

    def on_unmerge(self) -> None:
        if self._kg is None or not self._current:
            return
        merged = [m for m in self._kg.merged_into_me(self._current) if m]
        if not merged:
            return
        m = merged[0]
        if len(merged) > 1:
            # Two or more entries merged in: the user picks which one (the
            # first was always the one split off).
            names = [display_name(x) for x in merged]
            pick = self.ask_choice("Split off which?", "Which merged entry should be "
                                   "its own again?", names, parent=self)
            if pick is None:
                return
            m = merged[names.index(pick)]
        if not self.ask_yes_no("Split off?", f"Make '{display_name(m)}' its own entry "
                               "again? Its links and your decisions on them go back to "
                               "it; links from labels are worked out again at the "
                               "rebuild.", parent=self):
            return
        self._kg.unmerge(m["id"])
        self.status.setText(f"'{display_name(m)}' split off. Rebuilding…")
        self.on_rebuild()

    def on_answers(self) -> None:
        """The user's own answers about names, so a wrong one can be found and
        forgotten (an answer had no undo)."""
        if self._kg is None:
            return
        self.tree.clear()
        self._current = None
        self._selected = None
        answers = self._kg.answers()
        self.status.setText(f"{len(answers)} answer(s) you gave. Select one to forget it.")
        for a in answers:
            where = "everywhere" if a["everywhere"] else (a["path"] or "one document")
            item = QTreeWidgetItem([f"'{a['surface']}' is {a['name']}", "your answer", where])
            item.setData(0, ROLE_KIND, "answer")
            item.setData(0, ROLE_DATA, a)
            self.tree.addTopLevelItem(item)
        self._enable_actions()

    def on_forget_answer(self) -> None:
        sel = self._selected or {}
        if sel.get("kind") != "answer" or self._kg is None or self._busy:
            return
        a = sel["data"]
        self._kg.forget_answer(a["key"], a["document_id"])
        self._selected = None
        self.status.setText(f"Forgot that '{a['surface']}' is {a['name']}. Rebuilding — "
                            "the question will be asked again…")
        self.on_rebuild()

    def closeEvent(self, event) -> None:          # noqa: N802 — Qt's name
        if self._kg is not None:
            self._kg.close()
            self._kg = None
        super().closeEvent(event)


def build_connections(window) -> QWidget:
    """Factory for the tab registry."""
    return ConnectionsTab(window)
