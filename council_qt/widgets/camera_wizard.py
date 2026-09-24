"""
council_qt.widgets.camera_wizard — "which camera, what to install, is it done?"

Three pages over council_core.camera_setup, which holds every word and every
check; this module only lays them out.

  1. Choose — Basler or Prophesee EVK4.
  2. Install — the steps for that camera, with links and the exact install
     command for the Python this app is running under.
  3. Check — asks the installed software what it can actually do, and says
     which step fixes whatever it finds. "Check again" after installing.

Finishing is never blocked. Someone setting up a machine will often finish the
wizard before the camera is even unpacked; the Check page says plainly what is
still missing, and the choice is saved either way so the app knows which
camera to look for.

THE CHECK RUNS OFF THE UI THREAD, AND IS COLLECTED, NOT PUSHED BACK
pylon enumerates every transport layer, which takes seconds on a machine with
a frame grabber. That happens on a worker; the page polls a one-slot result
on a QTimer — the same pull the live view uses — so no widget is ever touched
from the worker and no marshalling machinery is needed in a generated app
that has none.

LINKS OPEN ONLY WHEN CLICKED
The app itself fetches nothing. A link hands the URL to the system browser,
and only when the user clicks it.
"""
from __future__ import annotations

import html
import os
import sys
import threading
from typing import Callable, Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QPushButton,
                               QRadioButton, QVBoxLayout, QWidget, QWizard,
                               QWizardPage)

from council_core import camera_setup as cs

#: How often the Check page looks for a finished check.
POLL_MS = 50

#: One mark per state. The SYMBOL and WORD carry the meaning, so it survives a
#: dark theme or a colour-blind reader; colour only reinforces it.
MARKS = {
    True: ("✓", "Done", "#2da44e"),
    None: ("⚠", "Check", "#c69026"),
    False: ("✗", "Missing", "#e5534b"),
}


def dialogs_disabled() -> bool:
    """COUNCIL_NO_DIALOGS, as the generated apps and the test suite use it."""
    return bool(os.environ.get("COUNCIL_NO_DIALOGS"))


# ======================================================================
# Pages
# ======================================================================
class ChoosePage(QWizardPage):
    def __init__(self, current: Optional[str] = None):
        super().__init__()
        self.setTitle("Which camera will you use?")
        self.setSubTitle("You can change this later with Camera setup…")
        layout = QVBoxLayout(self)
        self.group = QButtonGroup(self)
        self.buttons: Dict[str, QRadioButton] = {}
        for choice in cs.CHOICES:
            g = cs.guide(choice)
            button = QRadioButton(g.title)
            button.setObjectName(f"choice_{choice}")
            self.group.addButton(button)
            self.buttons[choice] = button
            layout.addWidget(button)
            blurb = QLabel(g.summary)
            blurb.setWordWrap(True)
            blurb.setContentsMargins(24, 0, 0, 12)
            layout.addWidget(blurb)
            button.toggled.connect(lambda _on: self.completeChanged.emit())
        layout.addStretch(1)
        if current in self.buttons:
            self.buttons[current].setChecked(True)

    def choice(self) -> Optional[str]:
        for key, button in self.buttons.items():
            if button.isChecked():
                return key
        return None

    def isComplete(self) -> bool:                          # noqa: N802
        return self.choice() is not None


class InstallPage(QWizardPage):
    """The steps for whichever camera was chosen — rebuilt on every visit,
    because Back and a different choice must not leave the old steps up."""

    def __init__(self, python: str):
        super().__init__()
        self.python = python
        layout = QVBoxLayout(self)
        self.steps = QLabel()
        self.steps.setWordWrap(True)
        self.steps.setTextFormat(Qt.TextFormat.RichText)
        self.steps.setOpenExternalLinks(True)
        self.steps.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        layout.addWidget(self.steps)
        row = QHBoxLayout()
        self.copy_btn = QPushButton("Copy install command")
        self.copy_btn.clicked.connect(self.copy_commands)
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        layout.addStretch(1)
        self._commands: list = []

    def initializePage(self) -> None:                      # noqa: N802
        choice = self.wizard().choice()
        g = cs.guide(choice, python=self.python)
        self.setTitle(f"Install the software for a {g.title}")
        self.setSubTitle("Do these on the computer the camera is plugged "
                         "into. The next page checks them.")
        self.steps.setText(render_steps(g))
        self._commands = [s.command for s in g.steps if s.command]
        self.copy_btn.setVisible(bool(self._commands))
        self.note.setText(f"<i>{html.escape(g.note)}</i>" if g.note else "")

    def copy_commands(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None and self._commands:
            clipboard.setText("\n".join(self._commands))


class CheckPage(QWizardPage):
    def __init__(self, checker: Callable[[str], "cs.Readiness"]):
        super().__init__()
        self.checker = checker
        self.setTitle("Is everything installed?")
        self.setSubTitle("This asks the installed software what it can "
                         "actually do.")
        layout = QVBoxLayout(self)
        self.results = QLabel()
        self.results.setWordWrap(True)
        self.results.setTextFormat(Qt.TextFormat.RichText)
        self.results.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.results)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        row = QHBoxLayout()
        self.again_btn = QPushButton("Check again")
        self.again_btn.clicked.connect(self.run_check)
        row.addWidget(self.again_btn)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)

        self.readiness: Optional[cs.Readiness] = None
        self._slot: list = []
        self._lock = threading.Lock()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._collect)
        self._worker: Optional[threading.Thread] = None

    def initializePage(self) -> None:                      # noqa: N802
        self.run_check()

    @property
    def checking(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def run_check(self) -> None:
        if self.checking:
            return
        choice = self.wizard().choice()
        self.readiness = None
        self.results.setText("Checking…")
        self.summary.setText("")
        self.again_btn.setEnabled(False)

        def work() -> None:
            try:
                got = self.checker(choice)
            except Exception as exc:                        # noqa: BLE001
                # A checklist that crashes is no checklist at all: report it
                # as a failed check, and keep the wizard usable.
                got = cs.Readiness(choice, [cs.Check(
                    "The check itself", False, f"{type(exc).__name__}: {exc}",
                    "Try again; if it persists, reinstall the SDK.")])
            with self._lock:
                self._slot.append(got)

        self._worker = threading.Thread(target=work, name="camera-setup-check",
                                        daemon=True)
        self._worker.start()
        self._timer.start()

    def _collect(self) -> None:
        """UI thread: pick up a finished check, if there is one."""
        with self._lock:
            got = self._slot.pop() if self._slot else None
        if got is None:
            return
        self._timer.stop()
        self.readiness = got
        self.results.setText(render_checks(got))
        self.summary.setText(f"<b>{html.escape(got.summary())}</b>")
        self.again_btn.setEnabled(True)


# ======================================================================
# The wizard
# ======================================================================
class CameraSetupWizard(QWizard):
    def __init__(self, parent: Optional[QWidget] = None, *,
                 app_name: str = "", current: Optional[str] = None,
                 checker: Optional[Callable[[str], "cs.Readiness"]] = None,
                 python: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle(f"{app_name} — camera setup" if app_name
                            else "Camera setup")
        # A fixed style, not the platform default: Aero draws its own frame
        # and behaves differently offscreen, which is where it is tested.
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setMinimumSize(640, 480)
        self.choose = ChoosePage(current)
        self.install = InstallPage(python or sys.executable)
        self.check = CheckPage(checker or cs.check)
        for page in (self.choose, self.install, self.check):
            self.addPage(page)

    def choice(self) -> Optional[str]:
        return self.choose.choice()


def run_wizard(parent: Optional[QWidget], path, *, app_name: str = "",
               checker=None, python: Optional[str] = None,
               execute: Optional[Callable[[QWizard], int]] = None
               ) -> Optional[str]:
    """Show the wizard; save and return the choice if it was finished.

    Returns None when it was cancelled — the app then asks again next time,
    which is the only sensible reading of "set up once".

    `execute` exists so a test can drive the wizard without a window ever
    appearing; the app leaves it alone and gets a real modal dialog.
    """
    wizard = CameraSetupWizard(parent, app_name=app_name,
                               current=cs.load_choice(path),
                               checker=checker, python=python)
    accepted = (execute or (lambda w: w.exec()))(wizard)
    choice = wizard.choice() if accepted else None
    if choice:
        cs.save_choice(path, choice)
    return choice


# ======================================================================
# Rendering — kept apart from the widgets so it can be tested as text
# ======================================================================
def render_steps(g: "cs.Guide") -> str:
    items = []
    for step in g.steps:
        body = html.escape(step.text)
        if step.url:
            url = html.escape(step.url, quote=True)
            body += f'<br><a href="{url}">{url}</a>'
        if step.command:
            # Line by line: rich text folds newlines into spaces, which would
            # run three driver commands together into one that fails.
            lines = "<br>".join(html.escape(line)
                                for line in step.command.splitlines())
            body += (f"<br><code style='background:rgba(127,127,127,0.18);"
                     f"padding:2px 4px'>{lines}</code>")
        items.append(f"<li style='margin-bottom:8px'>{body}</li>")
    return f"<ol>{''.join(items)}</ol>"


def render_checks(r: "cs.Readiness") -> str:
    rows = []
    for c in r.checks:
        symbol, word, colour = MARKS[c.ok]
        line = (f"<span style='color:{colour}'><b>{symbol} {word}</b></span>"
                f" &nbsp;<b>{html.escape(c.label)}</b>")
        if c.detail:
            line += f" — {html.escape(c.detail)}"
        rows.append(f"<p style='margin-bottom:2px'>{line}</p>")
        if c.fix and c.ok is not True:
            # Its own block: Qt's rich text ignores margins on an inline
            # span, so an indented fix has to be a paragraph of its own.
            rows.append(f"<p style='margin-left:22px; margin-top:0'>"
                        f"{html.escape(c.fix)}</p>")
    return "".join(rows)
