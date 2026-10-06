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
connection is reopened when they finish.
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
from .connections_dialogs import FieldsDialog

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
}
TYPE_ICON = {"PERSON": "👤", "PART": "⚙", "PROJECT": "📁", "DOCUMENT": "📄"}

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

    def rebuild(self, on_progress=None) -> Dict[str, Any]:
        with self.open_graph() as kg:
            return kg.seed(on_progress=on_progress)

    def label_suggestions(self) -> List[Dict[str, Any]]:
        with self.open_graph() as kg:
            return kg.suggest_label_synonyms()

    def context(self, rel_path: str, locator: Any):
        return kgm.source_context(self.root, rel_path, locator)

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

    def pi_workers(self) -> List[tuple]:
        """(name, host url, model) of registered Pi nodes — only when remote
        nodes are enabled (COUNCIL_REMOTE_NODES=1), the engine's own opt-in."""
        try:
            import council_engine as ce
            if not ce._remote_nodes_enabled():
                return []
            from council_core import apothecary as apoth
            reg = apoth._ae.NodeRegistry(str(apoth.registry_path(self.vault_dir)))
            return [(n.name, f"http://{n.host}:{n.ollama_port}", n.model or n.active_model)
                    for n in reg.list_nodes() if (n.model or n.active_model)]
        except Exception:
            return []

    def suggest(self, model: str, use_pis: bool, on_progress, should_stop):
        from council_core import kg_extract as kx
        workers = {f"this PC ({model})": kx.ollama_chat(model)}
        if use_pis:
            for name, url, pmodel in self.pi_workers():
                workers[f"{name} ({pmodel})"] = kx.ollama_chat(pmodel, host=url)
        with self.open_graph() as kg:
            return kg.suggest_from_text(workers, model=f"extract:{model}",
                                        on_progress=on_progress, should_stop=should_stop)

    def open_file(self, rel_path: str) -> None:
        from .vault import VaultActions
        VaultActions(self.vault_dir).open_folder(self.root / rel_path)


class ConnectionsTab(ViewHelpers, QWidget):
    """Search, links with evidence, and a source preview."""

    def __init__(self, window=None, actions: Optional[ConnectionsActions] = None,
                 auto_refresh: bool = True, ask_string=None, ask_yes_no=None):
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
        for n in kg.neighbors(eid):
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
            other = n["other"] or {}
            icon = TYPE_ICON.get(other.get("type", ""), "")
            name = display_name(other) if other.get("type") != "DOCUMENT" else other.get("name", "")
            r_item = QTreeWidgetItem([f"{icon} {name}", n["status"],
                                      f"{len(n['evidence'])} source(s)"])
            r_item.setData(0, ROLE_KIND, "relation")
            r_item.setData(0, ROLE_DATA, n)
            if n["status"] == "suggested":
                r_item.setForeground(1, QColor(self._tokens["warning"]))
            elif n["status"] == "accepted":
                r_item.setForeground(1, QColor(self._tokens["success"]))
            g.addChild(r_item)
            for ev in n["evidence"]:
                e_item = QTreeWidgetItem([ev["quote"], ev["method"]
                                          + (" (changed since)" if ev["stale"] else ""),
                                          f"{ev['path']} · {ev['where']}"])
                e_item.setToolTip(0, ev["quote"])
                e_item.setData(0, ROLE_KIND, "evidence")
                e_item.setData(0, ROLE_DATA, ev)
                r_item.addChild(e_item)
        mentions = kg.mentions(eid)
        if mentions:
            g = QTreeWidgetItem([f"mentioned in ({len(mentions)})", "", ""])
            f = g.font(0)
            f.setBold(True)
            g.setFont(0, f)
            self.tree.addTopLevelItem(g)
            for m in mentions:
                m_item = QTreeWidgetItem([m["snippet"], m["method"],
                                          f"{m['path']} · {m['where']}"])
                m_item.setToolTip(0, m["snippet"])
                m_item.setData(0, ROLE_KIND, "mention")
                m_item.setData(0, ROLE_DATA, m)
                g.addChild(m_item)
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
            self._preview(data["path"], data["locator"], data.get("where", ""))
        elif kind == "relation" and data.get("evidence"):
            ev = data["evidence"][0]
            self._preview(ev["path"], ev["locator"], ev["where"])
        self._enable_actions()

    def _on_tree_double(self, item, _col=0) -> None:
        if item is None or item.data(0, ROLE_KIND) != "relation":
            return
        other = item.data(0, ROLE_DATA).get("other") or {}
        if other.get("id") and other.get("type") != "DOCUMENT":
            self.show_entity(other["id"])

    def _preview(self, rel_path: str, locator: Any, where: str) -> None:
        lines = self.actions.context(rel_path, locator)
        self.preview_title.setText(f"{rel_path} · {where}")
        self._preview_path = rel_path
        if not lines:
            self.preview.setPlainText("(this spot could not be read)")
            return
        width = max(len(g) for g, _t, _hit in lines)
        self.preview.setPlainText("\n".join(
            f"{'▶' if hit else ' '} {g.rjust(width)} │ {t}" for g, t, hit in lines))

    def _enable_actions(self) -> None:
        cur = self._kg.entity(self._current) if (self._kg and self._current) else None
        self.merge_btn.setEnabled(bool(cur and cur["type"] != "DOCUMENT"))
        self.split_btn.setEnabled(bool(cur and self._kg.merged_into_me(self._current)))
        sel = self._selected or {}
        has_file = sel.get("kind") in ("evidence", "mention", "relation", "question")
        self.open_btn.setEnabled(bool(has_file and getattr(self, "_preview_path", None)))
        is_rel = sel.get("kind") == "relation"
        status = (sel.get("data") or {}).get("status") if is_rel else None
        self.accept_btn.setEnabled(is_rel and status != "accepted")
        self.reject_btn.setEnabled(is_rel and status != "rejected")

    # ------------------------------------------------------------------
    def on_open_file(self) -> None:
        path = getattr(self, "_preview_path", None)
        if not path:
            return
        try:
            self.actions.open_file(path)
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

        def progress(i: int, n: int) -> None:
            if i % 25 == 0:
                self._to_ui(self.status.setText, f"Reading the documents… {i} of {n}")

        def work() -> None:
            try:
                stats = self.actions.rebuild(progress)
                self._to_ui(self._rebuilt, stats, None)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self._rebuilt, None, exc)

        threading.Thread(target=work, name="kg-rebuild", daemon=True).start()

    def _rebuilt(self, stats: Optional[Dict[str, Any]], exc: Optional[BaseException]) -> None:
        self._busy = False
        self.rebuild_btn.setEnabled(True)
        self._reopen()
        if exc is not None:
            self.status.setText(f"The rebuild failed: {exc}")
            return
        self.status.setText(f"Rebuilt from {stats['documents']} document(s) in "
                            f"{stats.get('seconds', 0):.1f} s.")
        self.on_search()
        if self._current:
            self.show_entity(self._current)

    def refresh_models(self) -> None:
        self.model_box.clear()
        for m in self.actions.local_models():
            self.model_box.addItem(m, m)
        pis = self.actions.pi_workers()
        self.pis_box.setEnabled(bool(pis))
        self.pis_box.setToolTip(
            ", ".join(f"{n} ({m})" for n, _u, m in pis) if pis else
            "No Pi nodes to share the work: register one (Apothecary → Set up a Pi) "
            "and enable remote nodes (COUNCIL_REMOTE_NODES=1).")
        self.suggest_btn.setEnabled(self.model_box.count() > 0)

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
        self.status.setText(f"Reading free text with {model}…")
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
        name). Listed for now; answering them is the merge review (KG3)."""
        if self._kg is None:
            return
        self.tree.clear()
        self._current = None
        reviews = self._kg.reviews()
        self.status.setText(f"{len(reviews)} open question(s).")
        for r in reviews:
            names = " or ".join(display_name(c) for c in r["candidates"] if c)
            q = (f"Is '{r['surface']}' {names}?" if r["kind"] == "ambiguous_name"
                 else f"'{r['surface']}' was read as {names} — right?")
            item = QTreeWidgetItem([q, r["kind"].replace("_", " "),
                                    f"{r['path']} · {r['where']}"])
            item.setData(0, ROLE_KIND, "question")
            item.setData(0, ROLE_DATA, {"path": r["path"], "locator": r["locator"],
                                        "where": r["where"], "review": r})
            self.tree.addTopLevelItem(item)

    # -- KG3 ---------------------------------------------------------------
    def _fill_answers(self, review) -> None:
        self.answer_box.clear()
        for c in review["candidates"]:
            if c:
                self.answer_box.addItem(display_name(c), c["id"])
        self.answer_box.addItem("Neither — a different person", None)
        self.everywhere_box.setChecked(review["kind"] == "inferred_alias")

    def on_answer(self) -> None:
        sel = self._selected or {}
        if sel.get("kind") != "question" or self._kg is None:
            return
        review = sel["data"]["review"]
        eid = self.answer_box.currentData()
        self._kg.answer_review(review["id"], eid, everywhere=self.everywhere_box.isChecked())
        who = self.answer_box.currentText()
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
        self._kg.merge(self._current, other["id"])
        self.status.setText(f"'{display_name(other)}' merged into '{display_name(cur)}'.")
        self.show_entity(self._current)
        self.on_search()

    def on_unmerge(self) -> None:
        if self._kg is None or not self._current:
            return
        merged = self._kg.merged_into_me(self._current)
        if not merged:
            return
        m = merged[0]
        if not self.ask_yes_no("Split off?", f"Make '{display_name(m)}' its own entry "
                               "again? Links from labels come back apart at the next "
                               "rebuild.", parent=self):
            return
        self._kg.unmerge(m["id"])
        self.status.setText(f"'{display_name(m)}' split off. Rebuilding…")
        self.on_rebuild()

    def closeEvent(self, event) -> None:          # noqa: N802 — Qt's name
        if self._kg is not None:
            self._kg.close()
            self._kg = None
        super().closeEvent(event)


def build_connections(window) -> QWidget:
    """Factory for the tab registry."""
    return ConnectionsTab(window)
