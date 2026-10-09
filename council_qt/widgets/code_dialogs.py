"""
council_qt.widgets.code_dialogs — the Code tab's dialogs: a project
(folder, test command, GUI checks, references), an agent profile, and the
project brief with the council's proposed additions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from council_core import agent_profiles as ap
from council_core import project as pj


class ProjectDialog(QDialog):
    """A project's name, folder, test command, GUI checks, references."""

    def __init__(self, parent=None, project: Optional[pj.Project] = None):
        super().__init__(parent)
        self.setWindowTitle("Project")
        self.resize(640, 460)
        form = QFormLayout(self)
        p = project or pj.Project("", "")
        self.name = QLineEdit(p.name)
        self.root = QLineEdit(p.root)
        self.root.setPlaceholderText("the project's folder (a git repository)")
        self.tests = QLineEdit(p.test_command)
        self.tests.setPlaceholderText(
            "e.g. python -m pytest -q tests/test_camera_tab.py — keep it fast; "
            "it runs after every step")
        self.gui = QPlainTextEdit("\n".join(f"{g.module}:{g.cls}"
                                            for g in p.gui_checks))
        self.gui.setPlaceholderText("module:Class, one per line — e.g. "
                                    "council_qt.tabs.capture:CaptureTab")
        self.refs = QPlainTextEdit("\n".join(p.references))
        self.refs.setPlaceholderText("documents and example scripts (files or "
                                     "folders), one per line")
        form.addRow("Name", self.name)
        form.addRow("Folder", self.root)
        form.addRow("Test command", self.tests)
        form.addRow("GUI checks", self.gui)
        form.addRow("References", self.refs)
        row = QHBoxLayout()
        ok = QPushButton("Save")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(ok)
        row.addWidget(cancel)
        form.addRow(row)

    def project(self) -> pj.Project:
        checks = []
        for line in self.gui.toPlainText().splitlines():
            if ":" in line:
                m, c = line.split(":", 1)
                if m.strip() and c.strip():
                    checks.append(pj.GuiCheck(m.strip(), c.strip()))
        return pj.Project(self.name.text().strip() or "project",
                          self.root.text().strip(),
                          test_command=self.tests.text().strip(),
                          gui_checks=checks,
                          references=[r.strip() for r in
                                      self.refs.toPlainText().splitlines()
                                      if r.strip()])


class ProfileDialog(QDialog):
    """An agent profile: name, instructions, model role, output, tools."""

    def __init__(self, parent=None, profile: Optional[ap.AgentProfile] = None):
        super().__init__(parent)
        self.setWindowTitle("Agent")
        self.resize(620, 520)
        p = profile or ap.AgentProfile("", "")
        form = QFormLayout(self)
        self.name = QLineEdit(p.name if not p.built_in else
                              f"{p.name} (mine)")
        self.instructions = QPlainTextEdit(p.instructions)
        self.role = QLineEdit(p.role)
        self.role.setToolTip("Which model answers for this agent: a role "
                             "from the Models tab (coder, intern, judge, …)")
        self.output = QComboBox()
        self.output.addItems(list(ap.OUTPUTS))
        self.output.setCurrentText(p.output)
        self.turns = QSpinBox()
        self.turns.setRange(5, 100)
        self.turns.setValue(p.max_turns)
        self.tool_boxes: Dict[str, QCheckBox] = {}
        tools_row = QWidget()
        tl = QVBoxLayout(tools_row)
        tl.setContentsMargins(0, 0, 0, 0)
        for t in ap.ALL_TOOLS:
            box = QCheckBox(t)
            box.setChecked(t in p.tools)
            self.tool_boxes[t] = box
            tl.addWidget(box)
        self.refs = QPlainTextEdit("\n".join(p.references))
        form.addRow("Name", self.name)
        form.addRow("Instructions", self.instructions)
        form.addRow("Model role", self.role)
        form.addRow("Hands back", self.output)
        form.addRow("Turns per step", self.turns)
        form.addRow("Tools", tools_row)
        form.addRow("Its own references", self.refs)
        row = QHBoxLayout()
        ok = QPushButton("Save")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(ok)
        row.addWidget(cancel)
        form.addRow(row)

    def profile(self) -> ap.AgentProfile:
        return ap.AgentProfile(
            "", self.name.text().strip() or "Agent",
            self.instructions.toPlainText().strip(),
            tools=[t for t, b in self.tool_boxes.items() if b.isChecked()],
            role=self.role.text().strip() or "coder",
            max_turns=self.turns.value(), output=self.output.currentText(),
            references=[r.strip() for r in self.refs.toPlainText()
                        .splitlines() if r.strip()])


class BriefDialog(QDialog):
    """The brief every job starts from, and the council's proposed
    addition, to accept or not."""

    def __init__(self, parent, vault_dir: Path, project: pj.Project):
        super().__init__(parent)
        self.vault_dir, self.project = vault_dir, project
        self.setWindowTitle(f"Brief — {project.name}")
        self.resize(820, 640)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("What every job on this project starts from:"))
        self.current = QPlainTextEdit(pj.brief(vault_dir, project))
        self.current.setReadOnly(True)
        lay.addWidget(self.current, 2)
        lay.addWidget(QLabel("The council's notes (yours to edit). A "
                             "proposed version appears here after a job:"))
        proposed = pj.proposed_brief(vault_dir, project)
        self.edit = QPlainTextEdit(proposed or pj.council_brief(vault_dir,
                                                                project))
        lay.addWidget(self.edit, 1)
        self.state = QLabel("A PROPOSED change is shown — Save accepts it."
                            if proposed else "")
        lay.addWidget(self.state)
        row = QHBoxLayout()
        save = QPushButton("Save notes")
        save.clicked.connect(self.save)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(save)
        row.addWidget(close)
        lay.addLayout(row)

    def save(self) -> None:
        pj.accept_brief(self.vault_dir, self.project, self.edit.toPlainText())
        prop = pj.project_dir(self.vault_dir, self.project) / \
            "brief_proposed.md"
        if prop.exists():
            prop.unlink()
        self.current.setPlainText(pj.brief(self.vault_dir, self.project))
        self.state.setText("Saved.")


