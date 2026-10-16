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

A "TYPHON (PLUGINS)" PROJECT TICKS CAMERA FAMILIES (stage P2 rows 3 and 4)
`CameraFamiliesWizard` is the same three pages for a project that runs
camera plugins (docs/typhon_plugins_design.md 2.4 item 15): a CHECK BOX
per camera family — each a camera plugin with a set-up, handed in as
council_core.camera_setup.Family — then the install steps of every family
ticked, then each one's checks and, for a family with one (a Basler), its
scan page. With one family ticked the pages read exactly as this wizard's.
Today's Typhon keeps `CameraSetupWizard`, unchanged.
"""
from __future__ import annotations

import html
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QHBoxLayout, QLabel,
                               QPushButton, QRadioButton, QTextBrowser,
                               QVBoxLayout, QWidget, QWizard, QWizardPage)

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
    """Is the software installed — and, for a Basler, every camera it can see
    and whether each will actually run (council_core.basler_scan).

    BOTH RUN OFF THE UI THREAD and are collected on a timer, the same pull
    the live view uses: pylon walks every transport layer (GigE discovery
    alone is ~255 ms) and the scan opens each camera and test-grabs from it.
    """

    def __init__(self, checker: Callable[[str], "cs.Readiness"],
                 scanner: Optional[Callable[[], Any]] = None):
        super().__init__()
        self.checker = checker
        self.scanner = scanner or _default_scanner
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
        self.scan_btn = QPushButton("Scan Basler cameras")
        self.scan_btn.setToolTip(
            "Every Basler camera this PC can see, and whether each one will "
            "run: interface, drivers, link, pixel format, trigger mode, and "
            "a short test grab.")
        self.scan_btn.clicked.connect(self.run_scan)
        row.addWidget(self.scan_btn)
        row.addStretch(1)
        layout.addLayout(row)
        # The large field the scan fills: one block per camera, every check
        # with its fix. Scrolls; selectable so a fix can be copied.
        self.scan_view = QTextBrowser()
        self.scan_view.setOpenExternalLinks(False)
        self.scan_view.setMinimumHeight(300)
        layout.addWidget(self.scan_view, 1)

        self.readiness: Optional[cs.Readiness] = None
        self.scan_report: Any = None
        self._slot: list = []
        self._scan_slot: list = []
        self._lock = threading.Lock()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._collect)
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(POLL_MS)
        self._scan_timer.timeout.connect(self._collect_scan)
        self._worker: Optional[threading.Thread] = None
        self._scan_worker: Optional[threading.Thread] = None

    def initializePage(self) -> None:                      # noqa: N802
        basler = self.wizard().choice() == "basler"
        self.scan_btn.setVisible(basler)
        self.scan_view.setVisible(basler)
        self.run_check()
        if basler:
            self.run_scan()

    @property
    def scanning(self) -> bool:
        return self._scan_worker is not None and self._scan_worker.is_alive()

    def run_scan(self) -> None:
        if self.scanning:
            return
        self.scan_report = None
        self.scan_view.setHtml("<p><i>Scanning for Basler cameras — each one "
                               "is opened briefly and test-grabbed…</i></p>")
        self.scan_btn.setEnabled(False)

        def work() -> None:
            try:
                got = self.scanner()
            except Exception as exc:                        # noqa: BLE001
                got = exc
            with self._lock:
                self._scan_slot.append(got)

        self._scan_worker = threading.Thread(target=work, daemon=True,
                                             name="basler-scan")
        self._scan_worker.start()
        self._scan_timer.start()

    def _collect_scan(self) -> None:
        with self._lock:
            got = self._scan_slot.pop() if self._scan_slot else None
        if got is None:
            return
        self._scan_timer.stop()
        self.scan_btn.setEnabled(True)
        if isinstance(got, Exception):
            self.scan_view.setHtml(
                f"<p><b>The scan itself failed:</b> "
                f"{html.escape(type(got).__name__)}: {html.escape(str(got))}</p>")
            return
        self.scan_report = got
        self.scan_view.setHtml(render_scan(got))

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
                 python: Optional[str] = None,
                 scanner: Optional[Callable[[], Any]] = None):
        super().__init__(parent)
        self.setWindowTitle(f"{app_name} — camera setup" if app_name
                            else "Camera setup")
        # A fixed style, not the platform default: Aero draws its own frame
        # and behaves differently offscreen, which is where it is tested.
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setMinimumSize(760, 640)
        self.choose = ChoosePage(current)
        self.install = InstallPage(python or sys.executable)
        self.check = CheckPage(checker or cs.check, scanner)
        for page in (self.choose, self.install, self.check):
            self.addPage(page)

    def choice(self) -> Optional[str]:
        return self.choose.choice()


class NoQtApplication(RuntimeError):
    """This process has no QApplication to show the wizard in."""


def run_wizard(parent: Optional[QWidget], path, *, app_name: str = "",
               checker=None, python: Optional[str] = None,
               execute: Optional[Callable[[QWizard], int]] = None
               ) -> Optional[str]:
    """Show the wizard; save and return the choice if it was finished.

    Returns None when it was cancelled — the app then asks again next time,
    which is the only sensible reading of "set up once".

    `execute` exists so a test can drive the wizard without a window ever
    appearing; the app leaves it alone and gets a real modal dialog.

    Raises NoQtApplication in a process with no QApplication — a Tk build
    of the app, whose "Camera setup…" button reaches here too and whose
    process a QWizard made without one aborted outright.
    """
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        raise NoQtApplication("the camera setup wizard is part of the Qt "
                              "build of this app (run_example_gui.py "
                              "<example> --target qt)")
    wizard = CameraSetupWizard(parent, app_name=app_name,
                               current=cs.load_choice(path),
                               checker=checker, python=python)
    accepted = (execute or (lambda w: w.exec()))(wizard)
    choice = wizard.choice() if accepted else None
    if choice:
        cs.save_choice(path, choice)
    return choice


# ======================================================================
# A "Typhon (plugins)" project: camera families, ticked
# ======================================================================
class FamiliesPage(QWizardPage):
    """Page 1: a check box per camera family the project can be set up for
    (each a camera plugin with a set-up). At least one is ticked to go on;
    the simulated cameras need no set-up and are always listed."""

    def __init__(self, families: Sequence["cs.Family"],
                 current: Optional[Sequence[str]] = None):
        super().__init__()
        self.setTitle("Which cameras will you use?")
        self.setSubTitle("Tick every kind this app will use. You can change "
                         "this later with Camera setup…")
        layout = QVBoxLayout(self)
        self.buttons: Dict[str, QCheckBox] = {}
        for family in families:
            box = QCheckBox(family.title)
            box.setObjectName(f"family_{family.key}")
            self.buttons[family.key] = box
            layout.addWidget(box)
            blurb = QLabel(family.summary)
            blurb.setWordWrap(True)
            blurb.setContentsMargins(24, 0, 0, 12)
            layout.addWidget(blurb)
            box.toggled.connect(lambda _on: self.completeChanged.emit())
        self.simulated_note = QLabel(
            "The simulated cameras are always listed: no set-up, so the app "
            "can be tried before any camera arrives."
            if families else
            "No camera plugin with a set-up is on in this project. The "
            "simulated cameras are always listed.")
        self.simulated_note.setWordWrap(True)
        layout.addWidget(self.simulated_note)
        layout.addStretch(1)
        for key in current or ():
            if key in self.buttons:
                self.buttons[key].setChecked(True)

    def chosen(self) -> List[str]:
        """The ticked families, in the order offered."""
        return [k for k, box in self.buttons.items() if box.isChecked()]

    def isComplete(self) -> bool:                          # noqa: N802
        return bool(self.chosen())


class FamiliesInstallPage(InstallPage):
    """Page 2: the install steps of every family ticked — rebuilt on every
    visit. One family reads exactly as the single-choice wizard's page;
    with several, each family's steps come under its own heading, with a
    copy button of its own (a pip command and an EVK4's administrator
    commands do not go into one prompt)."""

    def __init__(self, python: str):
        super().__init__(python)
        self._family_row = QHBoxLayout()
        self.layout().insertLayout(2, self._family_row)
        self.copy_buttons: Dict[str, QPushButton] = {}
        self._family_commands: Dict[str, List[str]] = {}

    def initializePage(self) -> None:                      # noqa: N802
        chosen = self.wizard().chosen_families()
        guides = [(f, f.guide(self.python)) for f in chosen]
        for button in self.copy_buttons.values():
            self._family_row.removeWidget(button)
            button.deleteLater()
        self.copy_buttons = {}
        self._family_commands = {}
        self.setSubTitle("Do these on the computer the camera is plugged "
                         "into. The next page checks them.")
        if len(guides) == 1:
            g = guides[0][1]
            self.setTitle(f"Install the software for a {g.title}")
            self.steps.setText(render_steps(g))
            self._commands = [s.command for s in g.steps if s.command]
            self.copy_btn.setVisible(bool(self._commands))
            self.note.setText(f"<i>{html.escape(g.note)}</i>" if g.note
                              else "")
            return
        self.setTitle("Install the software for these cameras")
        parts = []
        for family, g in guides:
            parts.append(f"<h3>{html.escape(g.title)}</h3>{render_steps(g)}")
            if g.note:
                parts.append(f"<p><i>{html.escape(g.note)}</i></p>")
            commands = [s.command for s in g.steps if s.command]
            if commands:
                self._family_commands[family.key] = commands
                button = QPushButton(f"Copy the {family.label} "
                                     f"command{'s' if len(commands) > 1 else ''}")
                button.setObjectName(f"copy_{family.key}")
                button.clicked.connect(
                    lambda _checked=False, k=family.key: self.copy_family(k))
                self._family_row.addWidget(button)
                self.copy_buttons[family.key] = button
        self.steps.setText("".join(parts))
        self._commands = [c for cs_ in self._family_commands.values()
                          for c in cs_]
        self.copy_btn.setVisible(False)
        self.note.setText("")

    def copy_family(self, key: str) -> None:
        clipboard = QGuiApplication.clipboard()
        commands = self._family_commands.get(key) or []
        if clipboard is not None and commands:
            clipboard.setText("\n".join(commands))


class FamiliesCheckPage(QWizardPage):
    """Page 3: every ticked family's checks — and, for a family with a scan
    page (a Basler), its scan — each run off the UI thread and collected on
    a timer, as the single-choice wizard's Check page does. A check or scan
    from an earlier visit (Back, other ticks) is never shown on this one."""

    def __init__(self):
        super().__init__()
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
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary)
        self._row = QHBoxLayout()
        self.again_btn = QPushButton("Check again")
        self.again_btn.clicked.connect(self.run_check)
        self._row.addWidget(self.again_btn)
        self._row.addStretch(1)
        layout.addLayout(self._row)
        self._views = QVBoxLayout()
        layout.addLayout(self._views, 1)
        self.scan_buttons: Dict[str, QPushButton] = {}
        self.scan_views: Dict[str, QTextBrowser] = {}
        #: Each ticked family's Readiness, by family, once all are back.
        self.readiness: Optional[Dict[str, "cs.Readiness"]] = None
        #: Each scanning family's report (or the exception its scan raised).
        self.scan_reports: Dict[str, Any] = {}
        self._families: List["cs.Family"] = []
        self._lock = threading.Lock()
        self._slot: list = []
        self._scan_slot: list = []
        self._round = 0
        self._scan_rounds: Dict[str, int] = {}
        self._workers: List[threading.Thread] = []
        self._scanners: Dict[str, threading.Thread] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._collect)
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(POLL_MS)
        self._scan_timer.timeout.connect(self._collect_scans)

    def initializePage(self) -> None:                      # noqa: N802
        self._families = list(self.wizard().chosen_families())
        for widget in list(self.scan_buttons.values()) + \
                list(self.scan_views.values()):
            widget.setParent(None)
            widget.deleteLater()
        self.scan_buttons, self.scan_views = {}, {}
        self.scan_reports = {}
        for family in self._families:
            if family.scan is None:
                continue
            button = QPushButton(family.scan_label or f"Scan {family.label}")
            button.setObjectName(f"scan_{family.key}")
            if family.scan_tip:
                button.setToolTip(family.scan_tip)
            button.clicked.connect(
                lambda _checked=False, k=family.key: self.run_scan(k))
            self._row.insertWidget(self._row.count() - 1, button)
            view = QTextBrowser()
            view.setOpenExternalLinks(False)
            view.setMinimumHeight(300 if len(self._families) == 1 else 200)
            self._views.addWidget(view, 1)
            self.scan_buttons[family.key] = button
            self.scan_views[family.key] = view
        self.run_check()
        for key in self.scan_buttons:
            self.run_scan(key)

    # -- the checks ---------------------------------------------------------
    @property
    def checking(self) -> bool:
        return any(w.is_alive() for w in self._workers)

    def run_check(self) -> None:
        families = list(self._families)
        self._round += 1
        ticket = self._round
        self.readiness = None
        self.results.setText("Checking…")
        self.summary.setText("")
        self.again_btn.setEnabled(False)

        def work() -> None:
            got: Dict[str, Any] = {}
            for family in families:
                try:
                    got[family.key] = family.check()
                except Exception as exc:                    # noqa: BLE001
                    # A checklist that crashes is no checklist at all.
                    got[family.key] = cs.Readiness(family.key, [cs.Check(
                        "The check itself", False,
                        f"{type(exc).__name__}: {exc}",
                        "Try again; if it persists, reinstall the SDK.")])
            with self._lock:
                self._slot.append((ticket, got))

        worker = threading.Thread(target=work, daemon=True,
                                  name="camera-setup-check")
        self._workers = [w for w in self._workers if w.is_alive()] + [worker]
        worker.start()
        self._timer.start()

    def _collect(self) -> None:
        """UI thread: pick up this visit's finished checks, if they are."""
        with self._lock:
            mine = [got for ticket, got in self._slot if ticket == self._round]
            self._slot = [(t, g) for t, g in self._slot if t > self._round]
        if not mine:
            return
        self._timer.stop()
        got = mine[-1]
        self.readiness = got
        many = len(self._families) > 1
        parts, lines = [], []
        for family in self._families:
            r = got.get(family.key)
            if r is None:
                continue
            if many:
                parts.append(f"<h3>{html.escape(family.title)}</h3>")
                lines.append(f"<b>{html.escape(family.label)}: "
                             f"{html.escape(r.summary())}</b>")
            else:
                lines.append(f"<b>{html.escape(r.summary())}</b>")
            parts.append(render_checks(r))
        self.results.setText("".join(parts))
        self.summary.setText("<br>".join(lines))
        self.again_btn.setEnabled(True)

    # -- the scans ------------------------------------------------------------
    def scanning(self, key: str) -> bool:
        worker = self._scanners.get(key)
        return worker is not None and worker.is_alive()

    def run_scan(self, key: str) -> None:
        family = next((f for f in self._families if f.key == key), None)
        if family is None or family.scan is None \
                or key not in self.scan_views:
            return
        self.scan_reports.pop(key, None)
        self.scan_views[key].setHtml(
            f"<p><i>Scanning for {html.escape(family.label)} cameras — each "
            f"one is opened briefly and test-grabbed…</i></p>")
        self.scan_buttons[key].setEnabled(False)
        if self.scanning(key):
            # Still running from an earlier visit: its answer is this
            # family's, and lands here.
            self._scan_timer.start()
            return
        held = dict(self.wizard().held() or {})
        ticket = self._scan_rounds[key] = self._scan_rounds.get(key, 0) + 1

        def work() -> None:
            try:
                got = family.scan(held)
            except Exception as exc:                        # noqa: BLE001
                got = exc
            with self._lock:
                self._scan_slot.append((key, ticket, got))

        worker = threading.Thread(target=work, daemon=True,
                                  name=f"{key}-scan")
        self._scanners[key] = worker
        worker.start()
        self._scan_timer.start()

    def _collect_scans(self) -> None:
        with self._lock:
            done, self._scan_slot = self._scan_slot, []
        for key, ticket, got in done:
            if ticket != self._scan_rounds.get(key) \
                    or key not in self.scan_views:
                continue
            self.scan_buttons[key].setEnabled(True)
            self.scan_reports[key] = got
            view = self.scan_views[key]
            if isinstance(got, Exception):
                view.setHtml(
                    f"<p><b>The scan itself failed:</b> "
                    f"{html.escape(type(got).__name__)}: "
                    f"{html.escape(str(got))}</p>")
                continue
            try:
                view.setHtml(render_scan(got))
            except Exception as exc:                        # noqa: BLE001
                view.setHtml(f"<p><b>The scan's report could not be shown:"
                             f"</b> {html.escape(type(exc).__name__)}: "
                             f"{html.escape(str(exc))}</p>")
        if not any(self.scanning(k) for k in self.scan_views):
            self._scan_timer.stop()


class CameraFamiliesWizard(QWizard):
    """A "Typhon (plugins)" project's camera set-up: tick the camera
    families, install what each needs, check it is done. `families` are
    council_core.camera_setup.Family, one per camera plugin with a set-up
    (the plugin host's camera_families); `current` the families ticked
    before; `held()` the cameras this app holds open, {key: name in the
    rig}, which a scan must not open again."""

    def __init__(self, families: Sequence["cs.Family"],
                 parent: Optional[QWidget] = None, *, app_name: str = "",
                 current: Optional[Sequence[str]] = None,
                 python: Optional[str] = None,
                 held: Optional[Callable[[], Mapping[str, str]]] = None):
        super().__init__(parent)
        self.setWindowTitle(f"{app_name} — camera setup" if app_name
                            else "Camera setup")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setMinimumSize(760, 640)
        self.families = list(families)
        self.held = held or (lambda: held_names())
        self.choose = FamiliesPage(self.families, current)
        self.install = FamiliesInstallPage(python or sys.executable)
        self.check = FamiliesCheckPage()
        for page in (self.choose, self.install, self.check):
            self.addPage(page)

    def chosen(self) -> List[str]:
        """The ticked families, in the order offered."""
        return self.choose.chosen()

    def chosen_families(self) -> List["cs.Family"]:
        ticked = set(self.chosen())
        return [f for f in self.families if f.key in ticked]

    def choice(self) -> Optional[str]:
        """The first family ticked, or None (the single-choice wizard's
        question)."""
        chosen = self.chosen()
        return chosen[0] if chosen else None


def run_families_wizard(parent: Optional[QWidget], path,
                        families: Sequence["cs.Family"], *,
                        app_name: str = "", python: Optional[str] = None,
                        held: Optional[Callable[[], Mapping[str, str]]] = None,
                        execute: Optional[Callable[[QWizard], int]] = None
                        ) -> Optional[List[str]]:
    """Show a "Typhon (plugins)" project's wizard; save and return the
    families ticked if it was finished (camera_setup.save_families: still
    FORMAT 1, so today's Typhon reads the first family it knows). None when
    it was cancelled: nothing is saved, and it is asked again next time.
    Raises NoQtApplication in a process with no QApplication, as
    run_wizard does."""
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        raise NoQtApplication("the camera setup wizard is part of the Qt "
                              "build of this app (run_example_gui.py "
                              "<example> --target qt)")
    wizard = CameraFamiliesWizard(families, parent, app_name=app_name,
                                  current=cs.load_families(path),
                                  python=python, held=held)
    accepted = (execute or (lambda w: w.exec()))(wizard)
    chosen = wizard.chosen() if accepted else []
    if not chosen:
        return None
    return cs.save_families(path, chosen)


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


#: The scan's levels, marked like the checks above: symbol and word first.
SCAN_MARKS = {
    "pass": ("✓", "OK", "#2da44e"),
    "warn": ("⚠", "Warning", "#c69026"),
    "fail": ("✗", "Problem", "#e5534b"),
    "info": ("·", "Note", "#8b949e"),
}
VERDICT_MARKS = {
    "works": ("✓", "Ready to capture", "#2da44e"),
    "works with limits": ("⚠", "Works, with limits", "#c69026"),
    "will not work": ("✗", "Will not work yet", "#e5534b"),
}


#: Who is holding a camera open in this app right now — the scan must not
#: try to open it again (a second open fails on real hardware).
held_keys: Callable[[], List[str]] = lambda: []

#: The same, with each camera's name in the rig ({key: "cam2"}), for the
#: families wizard's scans ("in use as cam2"). Set by frame_camera.
held_names: Callable[[], Dict[str, str]] = lambda: {}


def _default_scanner() -> Any:
    from council_core import basler_scan

    return basler_scan.scan(held_keys=held_keys())


def _scan_line(c: Any) -> str:
    symbol, word, colour = SCAN_MARKS.get(c.level, SCAN_MARKS["info"])
    line = (f"<span style='color:{colour}'><b>{symbol} {word}</b></span>"
            f" &nbsp;<b>{html.escape(c.label)}</b>")
    if c.detail:
        line += f" — {html.escape(c.detail)}"
    out = f"<p style='margin:0 0 2px 0'>{line}</p>"
    if c.fix and c.level in ("warn", "fail"):
        out += (f"<p style='margin:0 0 4px 26px'><i>What to do:</i> "
                f"{html.escape(c.fix)}</p>")
    return out


def _capability_line(caps: Dict[str, Any]) -> str:
    bits = []
    sensor = caps.get("max") or (None, None)
    if sensor[0]:
        bits.append(f"sensor {sensor[0]}×{sensor[1]}")
    aoi = caps.get("aoi") or ()
    if aoi and aoi[0]:
        bits.append(f"area {aoi[0]}×{aoi[1]}")
    formats = caps.get("formats") or {}
    good = [f for f, (v, _) in formats.items() if v == "yes"]
    if good:
        bits.append("saves " + ", ".join(good[:6]) + ("…" if len(good) > 6 else ""))
    rng = caps.get("exposure_range")
    if rng:
        bits.append(f"exposure {rng[0]:g}–{rng[1]:g} µs")
    rng = caps.get("gain_range")
    if rng:
        bits.append(f"gain {rng[0]:g}–{rng[1]:g}")
    if caps.get("resulting_fps"):
        bits.append(f"up to {caps['resulting_fps']:.1f} fps")
    return " · ".join(bits)


def render_scan(report: Any) -> str:
    """The scan as one readable page: the machine first, then each camera
    with its verdict, every check, and what to do about each problem."""
    parts = [f"<p><b>{html.escape(report.summary())}</b>"
             + (f" &nbsp;<span style='color:#8b949e'>(pylon "
                f"{html.escape(report.pylon_version)}, "
                f"{report.seconds:.1f} s)</span>" if report.pylon_version else "")
             + "</p>"]
    for note in report.notes:
        parts.append(_scan_line(note))
    for cam in report.cameras:
        symbol, word, colour = VERDICT_MARKS.get(
            cam.verdict, VERDICT_MARKS["will not work"])
        parts.append("<hr>")
        parts.append(f"<p style='margin:4px 0'><span style='color:{colour}; "
                     f"font-size:14px'><b>{symbol} {word}</b></span> &nbsp; "
                     f"<b>{html.escape(cam.label)}</b></p>")
        caps = _capability_line(cam.capabilities)
        if caps:
            parts.append(f"<p style='margin:0 0 6px 0; color:#8b949e'>"
                         f"{html.escape(caps)}</p>")
        for c in cam.checks:
            parts.append(_scan_line(c))
    return "".join(parts)


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
