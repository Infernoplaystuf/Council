"""
council_qt.tabs.connections_dialogs — the Connections tab's dialogs.

Kept apart from the tab module because the all-tabs harness resolves every
`self.x` a module connects to against the TAB, and a dialog's own
`self.accept` is not the tab's.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog,
                               QDialogButtonBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QPushButton,
                               QRadioButton, QScrollArea, QVBoxLayout, QWidget)

from ..view import amp


class WhyNoLinksDialog(QDialog):
    """Why the graph has no links (KnowledgeGraph.why_no_links): the most
    likely reason and what to do, then what the graph read. Copy puts the
    text on the clipboard for a message to whoever helps - it holds counts
    and column / label names only, nothing from inside a document (user,
    2026-10-20: a tester's machine nobody else can see)."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Why no links?")
        # Not modal, and asked again and again: one closed is gone, not kept
        # hidden behind the tab (review, 2026-10-20)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(680, 500)
        outer = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.text.setPlainText(text)
        outer.addWidget(self.text, 1)
        note = QLabel("Counts and column / label names only - no values from your "
                      "documents and no file names - so you can send it as it is.")
        note.setWordWrap(True)
        outer.addWidget(note)
        row = QHBoxLayout()
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(self.on_copy)
        row.addWidget(self.copy_btn)
        self.message = QLabel("")
        row.addWidget(self.message, 1)
        outer.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

    def set_text(self, text: str) -> None:
        """Asked again while open: the new answer, in the same dialog."""
        self.text.setPlainText(text)
        self.message.setText("")

    def on_copy(self) -> None:
        QApplication.clipboard().setText(self.text.toPlainText())
        self.message.setText("Copied.")


class FoldersDialog(QDialog):
    """The folders the graph reads: the vault's data_in (always), and
    folders outside the vault the user added. Added folders are only READ -
    nothing in them is written, moved or copied into the vault - and
    removing one takes its documents out of the graph at the next rebuild.

    ``add(path)`` / ``remove(id)`` do the change and return an error text or
    ''; ``pick()`` asks for a folder (None = cancelled)."""

    def __init__(self, data_in: str, folders: List[dict], add: Callable[[str], str],
                 remove: Callable[[str], str], pick: Callable[[], Optional[str]],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Folders the graph reads")
        self.resize(560, 360)
        self._add, self._remove, self._pick = add, remove, pick
        outer = QVBoxLayout(self)
        intro = QLabel("The graph reads the vault's documents and any folder added here. "
                       "Added folders are only read: nothing in them is changed, moved or "
                       "copied. Rebuild after a change.")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        self.list = QListWidget()
        outer.addWidget(self.list, 1)
        first = QListWidgetItem(f"{data_in}   (the vault - always read)")
        first.setData(Qt.ItemDataRole.UserRole, None)
        self.list.addItem(first)
        for f in folders:
            self._row(f)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        row = QHBoxLayout()
        self.add_btn = QPushButton("Add a folder…")
        self.add_btn.clicked.connect(self.on_add)
        row.addWidget(self.add_btn)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.on_remove)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        outer.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)
        self.changed = False

    def _row(self, f: dict) -> None:
        text = f["path"]
        if f.get("files") is not None:
            # a Ledger the Ledger tab added: only its parent CSV is read
            text += "   (only " + ", ".join(f["files"]) + ")"
        text += "" if f.get("exists", True) else "   (not there now)"
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, f["id"])
        self.list.addItem(item)

    def on_add(self) -> None:
        path = self._pick()
        if not path:
            return
        err = self._add(path)
        if err:
            self.message.setText(err)
            return
        self.changed = True
        self.message.setText(f"Added {path}. Rebuild to read it.")
        self._row({"id": "?", "path": str(path)})

    def on_remove(self) -> None:
        item = self.list.currentItem()
        rid = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not rid or rid == "?":
            self.message.setText("Select an added folder to remove (the vault always stays).")
            return
        err = self._remove(rid)
        if err:
            self.message.setText(err)
            return
        self.changed = True
        self.list.takeItem(self.list.row(item))
        self.message.setText("Removed. Its documents leave the graph at the next rebuild; "
                             "the folder itself is not touched.")


class FieldsDialog(QDialog):
    """Which labels the graph reads, and which drifted labels mean the same.

    The default rules start ticked (user, 2026-10-21: a build should need
    no set-up, as Morphik's does); untick any that do not fit this vault.
    Suggested synonyms ('POC' for 'Point of Contact') come ticked only at
    confidence >= 0.85; nothing is assumed."""

    def __init__(self, rules, suggestions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fields the graph reads")
        self.resize(560, 520)
        outer = QVBoxLayout(self)
        intro = QLabel("Tick the labels that mean a person, part or project in "
                       "your documents. Only ticked labels are read.")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        body = QWidget()
        lay = QVBoxLayout(body)
        self.rule_boxes: Dict[str, QCheckBox] = {}
        for etype, title in (("PERSON", "People"), ("PART", "Parts"),
                             ("PROJECT", "Projects")):
            box = QGroupBox(title)
            bl = QVBoxLayout(box)
            for rule, status in rules:
                if rule.type != etype:
                    continue
                extra = " (another name for the project)" if rule.alias_of else \
                    " (the part it replaces)" if rule.supersedes else ""
                cb = QCheckBox(amp(rule.label + extra))
                cb.setChecked(status == "confirmed")
                self.rule_boxes[rule.label] = cb
                bl.addWidget(cb)
            lay.addWidget(box)
        self.syn_boxes: List[tuple] = []
        if suggestions:
            box = QGroupBox("Labels in your documents that may mean the same")
            bl = QVBoxLayout(box)
            for s in suggestions:
                cb = QCheckBox(amp(f"'{s['label']}' means '{s['means']}'  "
                                   f"({s['confidence']:.2f}: {s['why']})"))
                cb.setChecked(bool(s.get("precheck")))
                self.syn_boxes.append((s, cb))
                bl.addWidget(cb)
            lay.addWidget(box)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def choices(self):
        """({label: confirmed?}, [(synonym label, means)])"""
        rules = {lbl: cb.isChecked() for lbl, cb in self.rule_boxes.items()}
        syns = [(s["label"], s["means"]) for s, cb in self.syn_boxes if cb.isChecked()]
        return rules, syns


class GraphDialog(QDialog):
    """A new named graph, or a graph's settings (Morphik's create_graph and
    its prompt override): its name, the folders and file types it reads (new
    graphs only - make another graph to read other folders), what a model
    should look for in its documents, where a model's links go, and whether
    the Council's answers use it.

    ``pick()`` asks for a folder (None = cancelled). ``values()`` gives
    {name, folders, suffixes, focus, model_links ('graph' | 'review'),
    use_in_answers}."""

    def __init__(self, *, new: bool, pick: Callable[[], Optional[str]] = lambda: None,
                 name: str = "", folders: Optional[List[str]] = None,
                 suffixes: Optional[List[str]] = None, focus: str = "",
                 model_links: str = "graph", use_in_answers: bool = True,
                 is_main: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New knowledge graph" if new else "Graph settings")
        self.resize(600, 520)
        self._pick = pick
        self._new = new
        outer = QVBoxLayout(self)
        if new:
            intro = QLabel("A graph of its own over the folders you choose - inside the "
                           "vault's data_in, or anywhere else (only ever read). Build it "
                           "with ⟳ Build graph.")
            intro.setWordWrap(True)
            outer.addWidget(intro)
        form = QFormLayout()
        self.name = QLineEdit(name)
        self.name.setEnabled(not is_main)
        form.addRow("Name", self.name)
        outer.addLayout(form)
        box = QGroupBox("Folders it reads")
        bl = QVBoxLayout(box)
        self.folders = QListWidget()
        for f in folders or []:
            self.folders.addItem(f)
        if is_main:
            self.folders.addItem("data_in, the folders added in Folders…, and your Ledgers")
        bl.addWidget(self.folders)
        row = QHBoxLayout()
        self.add_btn = QPushButton("Add a folder…")
        self.add_btn.clicked.connect(self.on_add)
        row.addWidget(self.add_btn)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.on_remove)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        bl.addLayout(row)
        self.add_btn.setVisible(new)
        self.remove_btn.setVisible(new)
        tf = QFormLayout()
        self.suffixes = QLineEdit(", ".join(suffixes or []))
        self.suffixes.setPlaceholderText("every type it can read (or e.g. pdf, docx, md)")
        self.suffixes.setEnabled(not is_main)
        tf.addRow("File types", self.suffixes)
        bl.addLayout(tf)
        outer.addWidget(box)
        fbox = QGroupBox("What a model should look for")
        fl = QVBoxLayout(fbox)
        self.focus = QPlainTextEdit(focus)
        self.focus.setPlaceholderText("e.g. 'heat-treatment furnaces, the parts run in them "
                                      "and who operates them' - left empty, it looks for "
                                      "people, organisations, projects, parts, places …")
        self.focus.setMaximumHeight(80)
        fl.addWidget(self.focus)
        outer.addWidget(fbox)
        lbox = QGroupBox("Links a model finds")
        ll = QVBoxLayout(lbox)
        self.links_graph = QRadioButton("go in the graph at once (you can still reject any)")
        self.links_review = QRadioButton("wait for me to accept each one")
        (self.links_review if model_links == "review" else self.links_graph).setChecked(True)
        ll.addWidget(self.links_graph)
        ll.addWidget(self.links_review)
        outer.addWidget(lbox)
        self.use_in_answers = QCheckBox("Use this graph in the Council's answers")
        self.use_in_answers.setChecked(use_in_answers)
        outer.addWidget(self.use_in_answers)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.on_ok)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def on_add(self) -> None:
        path = self._pick()
        if path:
            self.folders.addItem(str(path))

    def on_remove(self) -> None:
        item = self.folders.currentItem()
        if item is not None:
            self.folders.takeItem(self.folders.row(item))

    def values(self) -> Dict[str, object]:
        return {"name": " ".join(self.name.text().split()),
                "folders": [self.folders.item(i).text() for i in range(self.folders.count())]
                if self._new else [],
                "suffixes": [x.strip() for x in self.suffixes.text().replace(";", ",")
                             .split(",") if x.strip()],
                "focus": " ".join(self.focus.toPlainText().split()),
                "model_links": "review" if self.links_review.isChecked() else "graph",
                "use_in_answers": self.use_in_answers.isChecked()}

    def on_ok(self) -> None:
        v = self.values()
        if self._new and not v["name"]:
            self.message.setText("Give the graph a name.")
            return
        if self._new and not v["folders"]:
            self.message.setText("Add at least one folder for it to read.")
            return
        self.accept()
