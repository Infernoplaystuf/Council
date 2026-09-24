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
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QPushButton,
                               QRadioButton, QTextBrowser, QVBoxLayout, QWidget,
                               QWizard, QWizardPage)

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
