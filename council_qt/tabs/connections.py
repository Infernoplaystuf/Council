"""
council_qt.tabs.connections — people, parts and projects, and why they link.

Search a person, part or project; see everything linked to it, grouped by how
("leads", "uses part", "superseded by"), and under each link the evidence: the
file, the sheet/row or page/line, and the quoted text. Click a piece of
evidence to read that spot of the document here, without leaving the app (a
PDF or workbook viewer cannot jump to a line); "Open file" opens it in its own
program. Double-click a linked item to go to it.

The graph lives in council_core.knowledge_graph; this view only reads it,
except for three things the user does on purpose: Rebuild (re-reads the
documents, on a worker), Fields (which labels mean a person / part / project,
and which drifted labels such as 'POC' mean the same as a confirmed one) and
Accept / Reject on a link. Nothing here deletes or edits a document.

Rebuild and the label harvest run on worker threads with their own SQLite
connection (a connection belongs to the thread that made it); the view's own
connection is reopened when they finish. Listing the installed models asks
Ollama over HTTP (GET /api/tags on this PC), so it runs on a worker too —
building the tab never waits on a model server. It does still ASK it, as it
is built: a test that builds the tab stubs ConnectionsActions.local_models
and pi_nodes (test_connections_tab, and the window fixture of
test_council_qt_foundation), or it reaches the real Ollama port.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QSplitter, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from council_core import knowledge_graph as kgm
from council_core import paths

from .. import theme
from ..view import ViewHelpers, amp
from .connections_dialogs import FieldsDialog, FoldersDialog

TYPE_FILTERS = (("Everything", None), ("People", "PERSON"),
                ("Parts", "PART"), ("Projects", "PROJECT"))

#: How a link reads from each end.
PHRASES = {
    "LEADS": ("leads", "led by"),
    "CONTACT_FOR": ("point of contact for", "point of contact"),
    "WORKS_ON": ("works on", "worked on by"),
    "OWNS": ("owns", "owned by"),
    "USES_PART": ("uses part", "used in"),
    "SUPERSEDES": ("supersedes", "superseded by"),
    "DOCUMENTED_IN": ("documents (Collection)", "in Collection of"),
    "INSPECTED": ("inspected", "inspected by"),
}
TYPE_ICON = {"PERSON": "👤", "PART": "⚙", "PROJECT": "📁", "DOCUMENT": "📄"}

#: How a piece of evidence from a spreadsheet was found, in words (the
#: older methods - row, record, document, model ... - show as they always
#: have).
METHOD_WORDS = {
    "ledger": "Ledger row (accepted)", "sheet": "workbook cell",
    "picture": "picture (OCR)",
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


def phrase(predicate: str, direction: str) -> str:
    out, inn = PHRASES.get(predicate, (predicate.lower(), predicate.lower()))
    return out if direction == "out" else inn


class ConnectionsActions:
    """What the Connections tab asks of the application. Tests pass their own
    vault; nothing else here knows where the vault is."""

    def __init__(self, vault_dir: Optional[Path] = None, root: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.root = Path(root) if root else self.vault_dir / "data_in"

    def open_graph(self) -> kgm.KnowledgeGraph:
        return kgm.KnowledgeGraph(self.vault_dir, self.root)

    def rebuild(self, on_progress=None, listener=None) -> Dict[str, Any]:
        """The rebuild; its progress lines also go to the terminal."""
        with self.open_graph() as kg:
            return kg.seed(on_progress=on_progress, listener=listener)

    def label_suggestions(self) -> List[Dict[str, Any]]:
        with self.open_graph() as kg:
            return kg.suggest_label_synonyms()

    def context(self, rel_path: str, locator: Any, quote: str = ""):
        return kgm.source_context(self.root, rel_path, locator, quote=quote)

    # -- free-text suggestions (KG2) --
    def local_models(self) -> List[str]:
        """US-origin models installed in this PC's Ollama, extractor first."""
        from council_core import kg_extract as kx
        from council_core.pi_setup import pi_models
        try:
            import council_engine as ce
            tags = ce._ollama_tags("http://127.0.0.1:11434") or {}
        except Exception:
            tags = {}
        names = [m.get("name", "") for m in tags.get("models", [])]
        names = sorted(n for n in names if n and pi_models.is_us_origin(n))
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
        offered only US-origin models for this PC."""
        from council_core.pi_setup import pi_models
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
        usable = [x for x in nodes if pi_models.is_us_origin(x[2])]
        skipped = [(n, m) for n, _u, m in nodes if not pi_models.is_us_origin(m)]
        return usable, skipped

    def pi_workers(self) -> List[tuple]:
        """(name, host url, model) of the Pi nodes that may share the work."""
        return self.pi_nodes()[0]

    def pi_skipped(self) -> List[tuple]:
        """(name, model) of the Pi nodes left out: not a US-origin model."""
        return self.pi_nodes()[1]

    def suggest(self, model: str, use_pis: bool, on_progress, should_stop):
        """Each worker reads with its OWN model (a Pi has a smaller one), and
        the run is named by the bare model: 'extract:' is added once, by the
        graph (it was added here too: 'extract:extract:gemma3:12b')."""
        from council_core import kg_extract as kx
        from council_core.pi_setup import pi_models
        if kx.is_gguf(model):
            name = Path(model).name
            workers = {f"this PC ({name})": (kx.gguf_chat(model), name)}
        else:
            workers = {f"this PC ({model})": (kx.ollama_chat(model), model)}
        if use_pis:
            for name, url, pmodel in self.pi_workers():
                if pi_models.is_us_origin(pmodel):         # pi_workers filters too
                    workers[f"{name} ({pmodel})"] = (kx.ollama_chat(pmodel, host=url), pmodel)
        with self.open_graph() as kg:
            return kg.suggest_from_text(workers, model=Path(model).name if kx.is_gguf(model)
                                        else model,
                                        on_progress=on_progress, should_stop=should_stop)

    def open_file(self, rel_path: str) -> None:
        from .vault import VaultActions
        VaultActions(self.vault_dir).open_folder(self.root / rel_path)


class ConnectionsTab(ViewHelpers, QWidget):
    """Search, links with evidence, and a source preview."""

    def __init__(self, window=None, actions: Optional[ConnectionsActions] = None,
                 auto_refresh: bool = True, ask_string=None, ask_yes_no=None,
                 ask_choice=None, ask_folder=None):
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
        self._build()
        if auto_refresh:
            self._reopen()
            self.on_search()
            self.refresh_models()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search a person, part or project…")
        self.search.textChanged.connect(lambda _t: self.on_search())
        row.addWidget(self.search, 2)
        self.type_box = QComboBox()
        for title, _t in TYPE_FILTERS:
            self.type_box.addItem(title)
        self.type_box.currentIndexChanged.connect(lambda _i: self.on_search())
        row.addWidget(self.type_box)
        self.rebuild_btn = self._button(row, "⟳ Rebuild graph", self.on_rebuild)
        self.fields_btn = self._button(row, "Fields…", self.on_fields)
        self.questions_btn = self._button(row, "Questions", self.on_questions)
        self.answers_btn = self._button(row, "Your answers", self.on_answers)
        self.sources_btn = self._button(row, "Spreadsheets", self.on_sources)
        self.folders_btn = self._button(row, "Folders…", self.on_folders)
        outer.addLayout(row)

        srow = QHBoxLayout()
        srow.addWidget(QLabel("Suggest links from free text with"))
        self.model_box = QComboBox()
        srow.addWidget(self.model_box, 1)
        self.pis_box = QCheckBox("and my Pi nodes")
        srow.addWidget(self.pis_box)
        self.suggest_btn = self._button(srow, "Suggest links", self.on_suggest)
        self.stop_btn = self._button(srow, "Stop", self.on_stop_suggest)
        self.stop_btn.setEnabled(False)
        outer.addLayout(srow)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.coverage = QLabel("")
        self.coverage.setWordWrap(True)
        self.coverage.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.coverage)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.results = QListWidget()
        self.results.currentItemChanged.connect(self._on_result)
        split.addWidget(self.results)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Linked item / evidence", "Status", "Where"])
        self.tree.setColumnWidth(0, 330)
        self.tree.setColumnWidth(1, 80)
        self.tree.currentItemChanged.connect(self._on_tree)
        self.tree.itemDoubleClicked.connect(self._on_tree_double)
        split.addWidget(self.tree)

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
        split.setSizes([230, 520, 420])
        outer.addWidget(split, 1)
        self._enable_actions()

    # ------------------------------------------------------------------
    def _reopen(self) -> None:
        if self._kg is not None:
            self._kg.close()
            self._kg = None
        try:
            self._kg = self.actions.open_graph()
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
        if cov["last_run"] is None:
            self.coverage.setText("The graph has not been built yet. Choose the "
                                  "labels it should read (Fields…), then Rebuild.")
            return
        ents = counts["entities"]
        rels = counts["relations"]
        text = (f"{cov['documents']} document(s) read, {cov['with_facts']} gave a "
                f"link, {cov['with_mentions']} mention something. "
                f"{ents.get('PERSON', 0)} people, {ents.get('PART', 0)} parts, "
                f"{ents.get('PROJECT', 0)} projects; "
                f"{rels.get('seeded', 0) + rels.get('accepted', 0)} links, "
                f"{rels.get('suggested', 0)} waiting for you.")
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
            text += " No field labels are ticked yet (Fields…)."
        self.coverage.setText(text)

    # ------------------------------------------------------------------
    def on_search(self) -> None:
        self.results.clear()
        kg = self._kg
        if kg is None:
            return
        text = self.search.text().strip()
        etype = TYPE_FILTERS[self.type_box.currentIndex()][1]
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
            item = QListWidgetItem(f"{TYPE_ICON.get(h['type'], '')} {display_name(ent)}")
            item.setData(ROLE_DATA, h["id"])
            self.results.addItem(item)

    def _on_result(self, item, _prev=None) -> None:
        if item is None:
            return
        self.show_entity(item.data(ROLE_DATA))

    def show_entity(self, eid: str) -> None:
        kg = self._kg
        if kg is None:
            return
        self._current = eid
        self.tree.clear()
        ent = kg.entity(eid)
        if ent is None:
            return
        aliases = [a for a in ent["aliases"] if a != ent["name"]]
        self.status.setText(f"{TYPE_ICON.get(ent['type'], '')} {display_name(ent)}"
                            + (f"   also written: {', '.join(aliases)}" if aliases else ""))
        groups: Dict[str, QTreeWidgetItem] = {}
        shown: Dict[str, int] = {}
        hidden: Dict[str, int] = {}
        # Only what is drawn is looked up; the rest of each kind is counted.
        for (pred, direction), k in kg.neighbor_counts(eid).items():
            if k > RELATIONS_SHOWN:
                label = phrase(pred, direction)
                hidden[label] = hidden.get(label, 0) + k - RELATIONS_SHOWN
        for n in kg.neighbors(eid, evidence_limit=EVIDENCE_SHOWN,
                              relation_limit=RELATIONS_SHOWN):
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
            icon = TYPE_ICON.get(other.get("type", ""), "")
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
        has_file = sel.get("kind") in ("evidence", "mention", "relation", "question", "source")
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
        self._reopen()
        if exc is not None:
            self.status.setText(f"The rebuild failed: {exc}")
            return
        from council_core import knowledge_graph as kgm
        self.status.setText(kgm.rebuild_summary_line(stats))
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
        self.on_search()
        if self._current:
            self.show_entity(self._current)

    def refresh_models(self) -> None:
        """List the installed US-origin models and the Pi nodes, on a worker:
        it asks Ollama over HTTP, which the tab's constructor used to do on
        the GUI thread (and every test that built the tab reached the real
        Ollama port)."""
        self.suggest_btn.setEnabled(False)

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
        self.pis_box.setEnabled(bool(pis))
        tip = (", ".join(f"{n} ({m})" for n, _u, m in pis) if pis else
               "No Pi nodes to share the work: register one (Apothecary → Set up a Pi) "
               "and enable remote nodes (COUNCIL_REMOTE_NODES=1).")
        if skipped:
            tip += "\n" + "\n".join(f"{n} skipped: {m} is not a US-origin model"
                                    for n, m in skipped)
        self.pis_box.setToolTip(tip)
        self.suggest_btn.setEnabled(self.model_box.count() > 0 and not self._busy)

    def on_suggest(self) -> None:
        """Ask the chosen model (and Pis, if ticked) for links in the free
        text. Everything it finds lands as 'suggested' for you to accept."""
        model = self.model_box.currentData()
        if self._busy or not model:
            return
        self._busy = True
        self._stop.clear()
        self.suggest_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        use_pis = self.pis_box.isChecked()
        self.status.setText(f"Reading free text with {Path(str(model)).name}…")
        if self._kg is not None:
            self._kg.close()
            self._kg = None

        def progress(done, total, worker):
            self._to_ui(self.status.setText,
                        f"Suggesting links: {done} of {total} passages (last by {worker})")

        def work() -> None:
            try:
                stats = self.actions.suggest(model, use_pis, progress, self._stop.is_set)
                self._to_ui(self._suggested, stats, None)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self._suggested, None, exc)

        threading.Thread(target=work, name="kg-suggest", daemon=True).start()

    def on_stop_suggest(self) -> None:
        self._stop.set()
        self.status.setText("Stopping after the passages in progress — run again to continue.")

    def _suggested(self, stats, exc) -> None:
        self._busy = False
        self.suggest_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._reopen()
        if exc is not None:
            self.status.setText(f"Suggesting failed: {exc}")
            return
        word = "Stopped" if stats["stopped"] else "Done"
        self.status.setText(
            f"{word}: {stats['done']} of {stats['chunks']} passages read, {stats['links']} "
            f"link(s) suggested, {stats['rejected']} rejected by the checks"
            + (f", {stats['errors']} unreadable answer(s)" if stats["errors"] else "")
            + ". Suggested links wait for you (✓ Accept / ✗ Reject).")
        self.on_search()
        if self._current:
            self.show_entity(self._current)

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
        self.status.setText("Fields saved. Rebuild to read the documents with them.")
        self._show_coverage()

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
            self.status.setText("Folders changed. Rebuild to read them.")

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
