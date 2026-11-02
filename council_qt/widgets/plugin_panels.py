"""
council_qt.widgets.plugin_panels — the toolbar strip of "Typhon (plugins)":
a button per plugin with a pop-out panel, the Typhon-owned windows those
panels live in, and the Plugins window where a project switches its feature
plugins on and off (docs/typhon_plugins_design.md 3.7, 4.2; stage P4 rows 1
and 7, as far as the classifier needed them — Andy, 2 November 2026).

THE STRIP
The wireframe's `toolbar` shape (port `plugin_toolbar`) at the top of the
window. PluginStrip puts in it, left to right, one button per plugin this
project runs that has a panel (its manifest's name: "Classification"), then
"Plugins…". A plugin that is switched off, faulted or missing a package has
no button. Plugins whose controls the main window still has (WINDOW_KEEPS,
empty since the Triggering window) get none, so nothing is offered twice.

THE WINDOWS
Typhon creates each plugin's window, titles it ("Classification — Typhon
(plugins)") and asks the plugin for its panel on first open (host.
build_panel, guarded: a panel that fails switches its plugin off, said in
the status line). Closing hides the window; the next press shows the same
one, as it was. Its place and size are remembered per project in
WINDOWS_FILE beside the plugin list, written atomically; a remembered place
that is no longer on any screen is not used.

THE PLUGINS WINDOW
Each feature plugin with a tick box (ticked = this project runs it) and its
state and reason (host.states()). Unticking switches it off in this project
now (host.switch_off: its instance is deactivated, its window closed, its
button gone); ticking switches it on. Camera plugins are not listed: the
set-up wizard chooses the camera families.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                               QLabel, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

#: The project's remembered window places, beside its plugin list.
WINDOWS_FILE = "typhon_windows.json"
#: Plugins with a panel whose controls the main window still has: none
#: since the Triggering window (2 November 2026) — the trigger boxes left
#: the Basic tab; Capture now (F9) stays beside Start and Stop.
WINDOW_KEEPS: frozenset = frozenset()
#: The strip's last button.
PLUGINS_BUTTON = "Plugins…"
#: How long after the last move or resize a place is written.
SAVE_DELAY_MS = 400


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".windows_", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class PanelWindow(QWidget):
    """A plugin's pop-out window: hides on close, remembers its place."""

    def __init__(self, pid: str, title: str, remember: Callable[[str, QRect],
                                                                 Any]) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.pid = pid
        self.setWindowTitle(title)
        self._remember = remember
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.inner: Optional[QWidget] = None
        self._save = QTimer(self)
        self._save.setSingleShot(True)
        self._save.setInterval(SAVE_DELAY_MS)
        self._save.timeout.connect(self.remember_now)

    def hold(self, inner: QWidget) -> None:
        self.inner = inner
        inner.setParent(self)
        self._layout.addWidget(inner)

    def remember_now(self) -> None:
        if self.isVisible():
            try:
                self._remember(self.pid, self.geometry())
            except Exception:                             # noqa: BLE001
                pass

    def moveEvent(self, event: Any) -> None:              # noqa: N802
        super().moveEvent(event)
        self._save.start()

    def resizeEvent(self, event: Any) -> None:            # noqa: N802
        super().resizeEvent(event)
        self._save.start()

    def closeEvent(self, event: Any) -> None:             # noqa: N802
        self.remember_now()
        event.ignore()
        self.hide()


class PluginStrip:
    """The toolbar strip's buttons and the windows they open."""

    def __init__(self, app: Any, host: Any, toolbar: QWidget, *,
                 say: Optional[Callable[[str], Any]] = None,
                 title: str = "") -> None:
        self.app = app
        self.host = host
        self.toolbar = toolbar
        self.say = say
        self.title = title or "Typhon"
        #: The plugin buttons now on the strip, by plugin id.
        self.buttons: Dict[str, QPushButton] = {}
        self.windows: Dict[str, PanelWindow] = {}
        self.plugins_button = QPushButton(PLUGINS_BUTTON, toolbar)
        self.plugins_button.setObjectName("btn_plugins_window")
        self.plugins_button.setToolTip(
            "Which plugins this project runs: switch the classifier and the "
            "other feature plugins on or off")
        self.plugins_button.clicked.connect(self.open_plugins_window)
        self._dress(self.plugins_button)
        self._layout().addWidget(self.plugins_button)
        host.show_panel_fn = self.open
        self.refresh()

    # -- the strip ------------------------------------------------------------
    def _layout(self) -> Any:
        box = self.toolbar.layout()
        if box is None:
            from PySide6.QtWidgets import QHBoxLayout
            box = QHBoxLayout(self.toolbar)
            box.setContentsMargins(0, 0, 0, 0)
        return box

    def _dress(self, button: QPushButton) -> None:
        """Look like the window's own buttons: the generated ones carry
        their colours in a style sheet by object name, and their font."""
        model = None
        try:
            for b in self.app.findChildren(QPushButton):
                sheet = b.styleSheet()
                if b is not button and sheet.startswith("QPushButton#") \
                        and b.objectName().startswith("btn_"):
                    model = b
                    break
        except Exception:                                 # noqa: BLE001
            model = None
        if model is None:
            return
        sheet = model.styleSheet().replace(f"#{model.objectName()}",
                                           f"#{button.objectName()}", 1)
        button.setStyleSheet(sheet)
        button.setFont(model.font())

    def shown(self) -> List[str]:
        """The plugins that get a button now."""
        try:
            pids = self.host.panel_plugins()
        except Exception:                                 # noqa: BLE001
            return []
        return [p for p in pids if p not in WINDOW_KEEPS]

    def _name(self, pid: str) -> str:
        m = self.host.manifest(pid)
        return getattr(m, "name", "") or pid

    def refresh(self) -> None:
        """Buttons for the plugins that can run now; the windows of those
        that cannot are closed."""
        want = self.shown()
        for pid in list(self.buttons):
            if pid not in want:
                button = self.buttons.pop(pid)
                button.setParent(None)
                button.deleteLater()
        for pid in list(self.windows):
            if pid not in want:
                self.close_window(pid)
        box = self._layout()
        for pid in want:
            if pid in self.buttons:
                continue
            b = QPushButton(self._name(pid), self.toolbar)
            b.setObjectName(f"btn_plugin_{pid.replace('.', '_')}")
            b.setToolTip(f"Open the {self._name(pid)} window")
            b.clicked.connect(lambda _c=False, p=pid: self.open(p))
            self._dress(b)
            box.insertWidget(box.indexOf(self.plugins_button), b)
            self.buttons[pid] = b

    # -- the windows -------------------------------------------------------
    def _places_path(self) -> Optional[Path]:
        folder = getattr(self.host, "project_dir", None)
        return Path(folder) / WINDOWS_FILE if folder else None

    def _places(self) -> Dict[str, List[int]]:
        path = self._places_path()
        try:
            doc = json.loads(path.read_text(encoding="utf-8")) if path \
                else {}
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def _remember(self, pid: str, rect: QRect) -> None:
        path = self._places_path()
        if path is None:
            return
        doc = self._places()
        doc[pid] = [rect.x(), rect.y(), rect.width(), rect.height()]
        _write_json(path, doc)

    @staticmethod
    def _on_a_screen(rect: QRect) -> bool:
        for screen in QGuiApplication.screens():
            if screen.availableGeometry().intersects(rect):
                return True
        return False

    def open(self, pid: str) -> bool:
        """Show plugin `pid`'s window (built on first open); False when it
        has no panel to show — the status line says why."""
        win = self.windows.get(pid)
        if win is None:
            title = f"{self._name(pid)} — {self.title}"
            win = PanelWindow(pid, title, self._remember)
            inner = self.host.build_panel(pid, win)
            if inner is None:
                win.deleteLater()
                state, why = self.host.state(pid)
                self._say(f"{self._name(pid)} has no window to show "
                          f"({why or state})")
                self.refresh()
                return False
            win.hold(inner)
            place = self._places().get(pid)
            rect = QRect(*place) if isinstance(place, list) and \
                len(place) == 4 else None
            if rect is not None and self._on_a_screen(rect):
                win.setGeometry(rect)
            else:
                win.resize(max(420, inner.sizeHint().width()),
                           max(640, inner.sizeHint().height()))
            self.windows[pid] = win
        win.show()
        win.raise_()
        win.activateWindow()
        return True

    def close_window(self, pid: str) -> None:
        win = self.windows.pop(pid, None)
        if win is not None:
            win.remember_now()
            win.hide()
            win.deleteLater()

    def close(self) -> None:
        for pid in list(self.windows):
            self.close_window(pid)
        if getattr(self.host, "show_panel_fn", None) == self.open:
            self.host.show_panel_fn = None

    def _say(self, text: str) -> None:
        if self.say is not None:
            try:
                self.say(text)
            except Exception:                             # noqa: BLE001
                pass

    # -- the Plugins window ------------------------------------------------
    def open_plugins_window(self) -> Optional["PluginsDialog"]:
        dlg = PluginsDialog(self, self.app)
        if os.environ.get("COUNCIL_NO_DIALOGS"):
            return dlg                     # built, never waited on (tests)
        dlg.exec()
        return dlg

    def switch(self, pid: str, on: bool) -> str:
        """Switch feature plugin `pid` on or off in this project; what
        happened."""
        try:
            if on:
                self.host.switch_on(pid)
            else:
                self.close_window(pid)
                self.host.switch_off(pid)
        except Exception as exc:                          # noqa: BLE001
            return f"{self._name(pid)}: {exc}"
        self.refresh()
        state, why = self.host.state(pid)
        said = (f"{self._name(pid)} is {'on' if on else 'off'} in this "
                f"project" + (f" — {why}" if on and state not in
                              ("on", "active") else "") + ".")
        self._say(said)
        return said


class PluginsDialog(QDialog):
    """Which feature plugins this project runs, with each one's state."""

    def __init__(self, strip: PluginStrip, parent: Any = None) -> None:
        super().__init__(parent)
        self.strip = strip
        self.setWindowTitle("Plugins")
        self.boxes: Dict[str, QCheckBox] = {}
        self.notes: Dict[str, QLabel] = {}
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Tick the plugins this project runs. A plugin that is off has "
            "no button and loads nothing.", self))
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        inner = QWidget(area)
        rows = QVBoxLayout(inner)
        host = strip.host
        for pid in self.feature_plugins():
            box = QCheckBox(strip._name(pid), inner)
            box.setObjectName(f"tick_{pid.replace('.', '_')}")
            box.setChecked(pid in host.runs())
            box.toggled.connect(lambda on, p=pid: self._toggled(p, on))
            note = QLabel("", inner)
            note.setWordWrap(True)
            note.setIndent(24)
            rows.addWidget(box)
            rows.addWidget(note)
            self.boxes[pid] = box
            self.notes[pid] = note
        rows.addStretch(1)
        area.setWidget(inner)
        layout.addWidget(area, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close,
                                   self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.resize(460, 360)
        self.show_states()

    def feature_plugins(self) -> List[str]:
        host = self.strip.host
        out = []
        for pid, _state, _why in host.states():
            m = host.manifest(pid)
            if m is not None and m.kind == "feature":
                out.append(pid)
        return out

    def show_states(self) -> None:
        host = self.strip.host
        for pid, note in self.notes.items():
            state, why = host.state(pid)
            m = host.manifest(pid)
            version = getattr(m, "version", "")
            note.setText(f"{pid} {version} — {state}"
                         + (f": {why}" if why else ""))

    def _toggled(self, pid: str, on: bool) -> None:
        self.strip.switch(pid, on)
        self.show_states()
