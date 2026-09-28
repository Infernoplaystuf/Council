"""
council_qt.widgets.sage_tuning — teach, correct and review the Sage.

Ported from `sage_agent.SageTuningPanel` (Tk): five sub-tabs over the Sage's
append-only knowledge store — Teach, Correct, Domains, Gaps, Knowledge.

IT TAKES THE KNOWLEDGE, NOT THE AGENT
The Tk panel takes a `SageAgent` and then only ever touches `sage.knowledge`
(`teach`, `correct` and `declare_domain` are one-line pass-throughs). Requiring
the agent meant requiring a loaded Sage MODEL, so a build with sage_agent.py
present but no "sage" personality pinned showed a bare separator and no panel
— a user could not teach the Sage anything until a model was pinned, and was
not told why. The knowledge store needs a directory, nothing else.

NO MODAL CONFIRMATIONS FOR ROUTINE WORK
Tk pops a message box after every successful add ("Fact added under X") and
for every missing field. Those are one status line each here. The one dialog
kept is Clear Gaps, which is destructive and cannot be undone.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import dialogs, theme
from ..view import ViewHelpers, amp

CONFIDENCES = ("high", "medium", "low")
CONFIDENCE_MARK = {"high": "✓", "medium": "~", "low": "?"}

#: How much of each list the panel shows — the Tk panel's limits.
MAX_DOMAINS = 20
MAX_GAPS = 40
MAX_FACTS = 50
MAX_SEARCH = 30


def _text(edit: QPlainTextEdit) -> str:
    return edit.toPlainText().strip()


def _readonly(height: Optional[int] = None) -> QPlainTextEdit:
    box = QPlainTextEdit()
    box.setReadOnly(True)
    if height:
        box.setMinimumHeight(height)
    return box


def _input(height: int) -> QPlainTextEdit:
    box = QPlainTextEdit()
    box.setFixedHeight(height)
    return box


# -- formatting, kept free of widgets so it is testable ----------------------
def format_domains(domains: List[dict]) -> str:
    return "\n".join(
        f"[{str(d.get('ts', ''))[:10]}] {d.get('domain', '')}: "
        f"{d.get('description', '')}"
        for d in domains[-MAX_DOMAINS:])


def format_gaps(gaps: List[dict]) -> str:
    if not gaps:
        return "(no gaps logged yet)"
    parts = []
    for g in reversed(gaps[-MAX_GAPS:]):
        line = f"[{str(g.get('ts', ''))[:16]}] {g.get('query', '')}"
        if g.get("reason"):
            line += f"\n  reason: {g['reason']}"
        parts.append(line)
    return "\n\n".join(parts)


def format_facts(records: List[dict]) -> str:
    if not records:
        return "(no facts yet — use the Teach tab to add some)"
    return "\n\n".join(
        f"{CONFIDENCE_MARK.get(r.get('confidence', 'medium'), '~')} "
        f"[{r.get('topic', '?')}] {r.get('fact', '')}\n"
        f"   source: {r.get('source', '?')}  ts: {str(r.get('ts', '?'))[:10]}"
        for r in records)


class SageTuningPanel(ViewHelpers, QWidget):
    """The five sub-tabs, over one SageKnowledge."""

    def __init__(self, knowledge, parent: Optional[QWidget] = None,
                 on_changed: Optional[Callable[[], None]] = None,
                 confirm: Callable[..., bool] = dialogs.askyesno):
        super().__init__(parent)
        self.knowledge = knowledge
        self.on_changed = on_changed
        self._confirm = confirm
        self._tokens = theme.tokens("dark")
        self._build()
        self.refresh_all()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        header = QHBoxLayout()
        title = QLabel(amp("🧙 Sage Tuning"))
        title.setStyleSheet("font-weight: bold;")
        header.addWidget(title)
        header.addStretch(1)
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet(f"color: {self._tokens['info']};")
        header.addWidget(self.stats_label)
        outer.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._teach_tab(), amp("📚 Teach"))
        self.tabs.addTab(self._correct_tab(), amp("✏️ Correct"))
        self.tabs.addTab(self._domains_tab(), amp("🗂 Domains"))
        self.tabs.addTab(self._gaps_tab(), amp("❓ Gaps"))
        self.tabs.addTab(self._knowledge_tab(), amp("📖 Knowledge"))
        outer.addWidget(self.tabs, 1)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(self.status)

    def _teach_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.teach_topic = QLineEdit()
        self.teach_fact = _input(80)
        self.teach_source = QLineEdit("user")
        self.teach_conf = QComboBox()
        self.teach_conf.addItems(CONFIDENCES)
        form.addRow("Topic:", self.teach_topic)
        form.addRow("Fact:", self.teach_fact)
        form.addRow("Source:", self.teach_source)
        form.addRow("Confidence:", self.teach_conf)
        row = QHBoxLayout()
        row.addStretch(1)
        self._button(row, "Add Fact", self.on_teach)
        form.addRow(row)
        return page

    def _correct_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.corr_query = _input(48)
        self.corr_wrong = _input(48)
        self.corr_right = _input(64)
        form.addRow("Query the Sage got wrong:", self.corr_query)
        form.addRow("Wrong answer (optional):", self.corr_wrong)
        form.addRow("Correct answer:", self.corr_right)
        row = QHBoxLayout()
        row.addStretch(1)
        self._button(row, "Save Correction", self.on_correct)
        form.addRow(row)
        return page

    def _domains_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.dom_name = QLineEdit()
        self.dom_desc = _input(60)
        form.addRow("Domain name:", self.dom_name)
        form.addRow("Description:", self.dom_desc)
        layout.addLayout(form)
        row = QHBoxLayout()
        row.addStretch(1)
        self._button(row, "Add Domain", self.on_domain)
        layout.addLayout(row)
        layout.addWidget(QLabel("Declared domains:"))
        self.dom_list = _readonly(90)
        layout.addWidget(self.dom_list, 1)
        return page

    def _gaps_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("Queries the Sage flagged as unknown:"))
        row.addStretch(1)
        self._button(row, "Clear Gaps", self.on_clear_gaps)
        self._button(row, "↺ Refresh", self.refresh_gaps)
        layout.addLayout(row)
        self.gaps_box = _readonly(150)
        self.gaps_box.setStyleSheet(f"color: {self._tokens['error']};")
        layout.addWidget(self.gaps_box, 1)
        hint = QLabel("Use gaps to identify what data to add via Teach or "
                      "Correct.")
        hint.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        layout.addWidget(hint)
        return page

    def _knowledge_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("Search:"))
        self.kb_search = QLineEdit()
        self.kb_search.returnPressed.connect(self.refresh_knowledge)
        row.addWidget(self.kb_search, 1)
        self._button(row, "Search", self.refresh_knowledge)
        self._button(row, "Show All", self.on_show_all)
        layout.addLayout(row)
        self.kb_box = _readonly(170)
        layout.addWidget(self.kb_box, 1)
        return page

    # -- refresh ---------------------------------------------------------
    def refresh_all(self) -> None:
        self.refresh_stats()
        self.refresh_domains()
        self.refresh_gaps()
        self.refresh_knowledge()

    def refresh_stats(self) -> None:
        s = self.knowledge.stats()
        self.stats_label.setText(
            f"facts:{s['facts']}  corrections:{s['corrections']}  "
            f"domains:{s['domains']}  gaps:{s['gaps']}")

    def refresh_domains(self) -> None:
        self.dom_list.setPlainText(format_domains(self.knowledge.get_domains()))

    def refresh_gaps(self) -> None:
        self.gaps_box.setPlainText(format_gaps(self.knowledge.get_gaps()))

    def refresh_knowledge(self, show_all: bool = False) -> None:
        query = self.kb_search.text().strip()
        if show_all or not query:
            records = self.knowledge.get_facts()[-MAX_FACTS:]
        else:
            records = [r for r in self.knowledge.search_relevant(
                           query, max_items=MAX_SEARCH)
                       if r.get("record_type") == "fact"]
        self.kb_box.setPlainText(format_facts(records))

    def on_show_all(self) -> None:
        self.refresh_knowledge(show_all=True)

    def _changed(self, message: str) -> None:
        self.status.setText(message)
        self.refresh_stats()
        if self.on_changed is not None:
            self.on_changed()

    # -- actions ---------------------------------------------------------
    def on_teach(self) -> None:
        topic = self.teach_topic.text().strip()
        fact = _text(self.teach_fact)
        if not topic or not fact:
            self.status.setText("Teach: a topic and a fact are both required.")
            return
        self.knowledge.add_fact(topic, fact,
                                self.teach_source.text().strip() or "user",
                                self.teach_conf.currentText())
        self.teach_topic.clear()
        self.teach_fact.clear()
        self.refresh_knowledge()
        self._changed(f"Fact added under '{topic}'.")

    def on_correct(self) -> None:
        query, right = _text(self.corr_query), _text(self.corr_right)
        if not query or not right:
            self.status.setText(
                "Correct: the query and the correct answer are both required.")
            return
        self.knowledge.add_correction(query, _text(self.corr_wrong), right)
        for box in (self.corr_query, self.corr_wrong, self.corr_right):
            box.clear()
        self._changed("Correction saved — the Sage will use it next time.")

    def on_domain(self) -> None:
        name = self.dom_name.text().strip()
        if not name:
            self.status.setText("Domain: a name is required.")
            return
        self.knowledge.add_domain(name, _text(self.dom_desc))
        self.dom_name.clear()
        self.dom_desc.clear()
        self.refresh_domains()
        self._changed(f"Domain '{name}' declared.")

    def on_clear_gaps(self) -> None:
        if not self._confirm("Clear Gaps",
                             "Clear all logged gaps? This cannot be undone.",
                             parent=self):
            return
        try:
            self.knowledge.gaps_path.write_text("", encoding="utf-8")
        except OSError as exc:
            self.status.setText(f"Could not clear the gaps: {exc}")
            return
        self.refresh_gaps()
        self._changed("Gaps cleared.")
