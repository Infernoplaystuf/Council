"""
setup_wizard.py — a step-by-step "new project" wizard: validated fields,
a page that is skipped for one choice, a checkable tree, a calendar, a
summary, and a final page that does the work with a progress bar.

WHAT IT DOES
Walks through creating a project: a name and template; where it goes; who
is on the team; which components to include (skipped for the Blank
template); when it starts; a summary to check; then Create writes a
project.json into the chosen folder, showing progress. Next stays greyed
out until a page is complete (a valid name, a folder that exists, at least
one component). Back keeps everything entered.

SHOWS
  * QWizard + QWizardPage, registerField (with "*" for mandatory fields and
    a custom property for non-standard widgets), field() to read them
  * isComplete() + completeChanged for custom validation, validatePage()
    for a last check before leaving a page
  * nextId() to branch: one template skips a page
  * initializePage() to build a page from earlier answers (the summary)
  * QStandardItemModel tree in a QTreeView with checkable, auto-tristate
    parents (setAutoTristate) and reading the ticked leaves back
  * QListView over a QStringListModel with Add / Remove buttons, editable
    in place
  * QCalendarWidget with a minimum date, QTimeEdit, QComboBox with item data
  * QRegularExpressionValidator on a QLineEdit, a QFileDialog folder row
  * a commit page (setCommitPage) whose work runs step by step on a QTimer,
    driving a QProgressBar, and only then enables Finish
  * QTextBrowser showing HTML built from the answers

RUN
    python examples/qt_apps/setup_wizard.py

REQUIRES  nothing beyond PySide6
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import (QDate, QRegularExpression, QStringListModel, Qt,
                            QTime, QTimer)
from PySide6.QtGui import (QRegularExpressionValidator, QStandardItem,
                           QStandardItemModel)
from PySide6.QtWidgets import (QApplication, QCalendarWidget, QComboBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListView, QProgressBar,
                               QPushButton, QTextBrowser, QTimeEdit,
                               QTreeView, QVBoxLayout, QWizard, QWizardPage)

PAGE_INTRO, PAGE_PROJECT, PAGE_PLACE, PAGE_TEAM, PAGE_PARTS, PAGE_WHEN, \
    PAGE_SUMMARY, PAGE_CREATE = range(8)

TEMPLATES = [("Inspection station", "inspection"),
             ("Data logger", "logger"),
             ("Blank", "blank")]
COMPONENTS: Dict[str, List[str]] = {
    "Capture": ["Camera", "Trigger input", "Frame buffer"],
    "Analysis": ["Measurements", "Pass / fail rules", "Statistics"],
    "Output": ["CSV export", "Report", "Alarms"],
}


class IntroPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("New project")
        self.setSubTitle("This wizard sets up a project folder.")
        lab = QLabel("You will choose a name and template, a folder, the "
                     "team, the parts to include and a start date. Nothing "
                     "is written until the <b>Create</b> step.", wordWrap=True)
        QVBoxLayout(self).addWidget(lab)


class ProjectPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Name and template")
        self.name = QLineEdit(placeholderText="e.g. Line 3 gauging")
        self.name.setValidator(QRegularExpressionValidator(
            QRegularExpression(r"[A-Za-z0-9][A-Za-z0-9 _-]{1,39}")))
        self.template = QComboBox()
        for label, key in TEMPLATES:
            self.template.addItem(label, key)
        self.description = QLineEdit()
        f = QFormLayout(self)
        f.addRow("Name*", self.name)
        f.addRow("Template", self.template)
        f.addRow("Description", self.description)
        # "*" makes the field mandatory: Next waits until it has text.
        self.registerField("name*", self.name)
        self.registerField("template", self.template, "currentData")
        self.registerField("description", self.description)


class PlacePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Location")
        self.setSubTitle("The project folder is created inside this one.")
        self.folder = QLineEdit()
        self.folder.textChanged.connect(self.completeChanged)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.note = QLabel()
        row = QHBoxLayout()
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        v = QVBoxLayout(self)
        v.addLayout(row)
        v.addWidget(self.note)
        v.addStretch(1)
        self.registerField("folder", self.folder)

    def initializePage(self) -> None:
        if not self.folder.text():
            self.folder.setText(str(Path.home()))

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Parent folder",
                                             self.folder.text())
        if d:
            self.folder.setText(d)

    def target(self) -> Path:
        name = str(self.field("name")).strip().replace(" ", "_")
        return Path(self.folder.text()) / name

    def isComplete(self) -> bool:
        ok = Path(self.folder.text()).is_dir()
        self.note.setText(f"Will create: {self.target()}" if ok
                          else "That folder does not exist.")
        return ok

    def validatePage(self) -> bool:
        if (self.target() / "project.json").exists():
            self.note.setText("A project with that name is already there — "
                              "go back and pick another name.")
            return False
        return True


class TeamPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Team")
        self.setSubTitle("Double-click a name to edit it.")
        self.model = QStringListModel(["Operator"])
        self.list = QListView()
        self.list.setModel(self.model)
        add = QPushButton("Add")
        add.clicked.connect(self._add)
        rm = QPushButton("Remove")
        rm.clicked.connect(self._remove_selected)
        col = QVBoxLayout()
        col.addWidget(add)
        col.addWidget(rm)
        col.addStretch(1)
        row = QHBoxLayout(self)
        row.addWidget(self.list, 1)
        row.addLayout(col)

    def _add(self) -> None:
        r = self.model.rowCount()
        self.model.insertRows(r, 1)
        ix = self.model.index(r)
        self.model.setData(ix, "New member")
        self.list.setCurrentIndex(ix)
        self.list.edit(ix)

    def _remove_selected(self) -> None:
        ix = self.list.currentIndex()
        if ix.isValid():
            self.model.removeRows(ix.row(), 1)

    def members(self) -> List[str]:
        return [m.strip() for m in self.model.stringList() if m.strip()]

    def nextId(self) -> int:
        blank = self.field("template") == "blank"
        return PAGE_WHEN if blank else PAGE_PARTS


class PartsPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Components")
        self.setSubTitle("Tick a group to take all of it.")
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(["Component"])
        for group, parts in COMPONENTS.items():
            g = QStandardItem(group)
            g.setCheckable(True)
            g.setAutoTristate(True)        # parent follows its children
            for p in parts:
                child = QStandardItem(p)
                child.setCheckable(True)
                child.setCheckState(Qt.Checked)
                g.appendRow(child)
            self.model.appendRow(g)
        self.model.itemChanged.connect(lambda *_: self.completeChanged.emit())
        self.tree = QTreeView(headerHidden=True)
        self.tree.setModel(self.model)
        self.tree.expandAll()
        QVBoxLayout(self).addWidget(self.tree)

    def chosen(self) -> List[str]:
        out = []
        for r in range(self.model.rowCount()):
            g = self.model.item(r)
            for c in range(g.rowCount()):
                if g.child(c).checkState() == Qt.Checked:
                    out.append(f"{g.text()}/{g.child(c).text()}")
        return out

    def isComplete(self) -> bool:
        return bool(self.chosen())


class WhenPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Start")
        self.calendar = QCalendarWidget(gridVisible=True)
        self.calendar.setMinimumDate(QDate.currentDate())
        self.time = QTimeEdit(QTime(8, 0), displayFormat="HH:mm")
        f = QFormLayout(self)
        f.addRow(self.calendar)
        f.addRow("Start time", self.time)

    def when(self) -> str:
        return (self.calendar.selectedDate().toString(Qt.ISODate) + " "
                + self.time.time().toString("HH:mm"))


class SummaryPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Summary")
        self.setSubTitle("Check the answers. Back changes any of them.")
        self.view = QTextBrowser()
        QVBoxLayout(self).addWidget(self.view)

    def initializePage(self) -> None:
        cfg = self.wizard().config()
        rows = "".join(
            f"<tr><td><b>{html.escape(k)}</b></td><td>"
            f"{html.escape(', '.join(v) if isinstance(v, list) else str(v))}"
            f"</td></tr>" for k, v in cfg.items())
        self.view.setHtml(f"<table cellpadding=4>{rows}</table>")


class CreatePage(QWizardPage):
    """Does the work. A commit page: no Back once Create is pressed."""

    def __init__(self):
        super().__init__()
        self.setTitle("Creating the project")
        self.bar = QProgressBar()
        self.log = QLabel(wordWrap=True)
        v = QVBoxLayout(self)
        v.addWidget(self.bar)
        v.addWidget(self.log)
        v.addStretch(1)
        self.steps: List = []
        self.done = False
        self.timer = QTimer(self, interval=120)
        self.timer.timeout.connect(self._step)

    def initializePage(self) -> None:
        wiz = self.wizard()
        target = wiz.place.target()
        cfg = wiz.config()
        self.steps = [
            ("Creating the folder", lambda: target.mkdir(parents=True,
                                                         exist_ok=True)),
            ("Writing project.json", lambda: (target / "project.json")
             .write_text(json.dumps(cfg, indent=2), encoding="utf-8")),
        ] + [(f"Adding {c}", lambda: None) for c in cfg.get("components", [])]
        self.bar.setRange(0, len(self.steps))
        self.bar.setValue(0)
        self.done = False
        self.timer.start()

    def _step(self) -> None:
        i = self.bar.value()
        if i >= len(self.steps):
            self.timer.stop()
            self.done = True
            self.log.setText(f"Done: {self.wizard().place.target()}")
            self.completeChanged.emit()
            return
        text, work = self.steps[i]
        self.log.setText(text + "…")
        try:
            work()
        except OSError as exc:
            self.timer.stop()
            self.log.setText(f"Failed: {exc}")
            return
        self.bar.setValue(i + 1)

    def isComplete(self) -> bool:
        return self.done


class SetupWizard(QWizard):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("New project")
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnLastPage)
        self.resize(640, 520)
        self.project = ProjectPage()
        self.place = PlacePage()
        self.team = TeamPage()
        self.parts = PartsPage()
        self.when = WhenPage()
        self.summary = SummaryPage()
        self.create_page = CreatePage()
        for pid, page in ((PAGE_INTRO, IntroPage()),
                          (PAGE_PROJECT, self.project),
                          (PAGE_PLACE, self.place), (PAGE_TEAM, self.team),
                          (PAGE_PARTS, self.parts), (PAGE_WHEN, self.when),
                          (PAGE_SUMMARY, self.summary),
                          (PAGE_CREATE, self.create_page)):
            self.setPage(pid, page)
        self.summary.setCommitPage(True)
        self.summary.setButtonText(QWizard.CommitButton, "Create")

    def config(self) -> dict:
        blank = self.field("template") == "blank"
        return {"name": str(self.field("name")).strip(),
                "template": self.field("template"),
                "description": self.field("description"),
                "folder": str(self.place.target()),
                "team": self.team.members(),
                "components": [] if blank else self.parts.chosen(),
                "starts": self.when.when()}


def make_window(argv: Optional[List[str]] = None) -> SetupWizard:
    return SetupWizard()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    wiz = make_window()
    wiz.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
