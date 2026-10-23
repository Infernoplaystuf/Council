"""
settings_dialog.py — a preferences dialog with nearly every Qt input widget,
saved to a JSON file and applied live to the main window.

WHAT IT DOES
The main window shows a sample of what the settings change. Settings…
opens the dialog: pick a page on the left, change values, and Apply shows
the effect without closing (theme, accent colour, font, sizes). OK applies
and saves, Cancel puts everything back, Restore Defaults resets the page
values. Settings are kept in a JSON file in the user's config folder
(shown in the status bar), so they survive a restart.

SHOWS
  * QDialog with a QListWidget page list driving a QStackedWidget
  * QDialogButtonBox: Ok / Cancel / Apply / RestoreDefaults, each wired
  * one dataclass of settings, read from and written to the widgets in one
    place each (to_widgets / from_widgets), and JSON on disk via
    QStandardPaths — the pattern to use instead of QSettings
  * input widgets: QLineEdit with a QRegularExpressionValidator and an
    inputMask, editable QComboBox, QFontComboBox, QSpinBox with suffix,
    QDoubleSpinBox, QSlider paired with a QSpinBox, QDial with a
    QLCDNumber, QCheckBox (incl. tristate), QRadioButton in a QButtonGroup,
    QDateEdit with a calendar popup, QTimeEdit, QKeySequenceEdit,
    QPlainTextEdit, a QToolButton with a drop-down QMenu, a colour button
    using QColorDialog, a path row using QFileDialog, QCommandLinkButton
  * QGroupBox (checkable, so it enables its children), QFormLayout,
    QGridLayout
  * applying a theme with QPalette + QApplication.setStyle("Fusion")
  * dirty tracking: Apply is enabled only when something changed

RUN
    python examples/qt_apps/settings_dialog.py

REQUIRES  nothing beyond PySide6
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import (QDate, QRegularExpression, QStandardPaths, Qt,
                            QTime)
from PySide6.QtGui import (QColor, QFont, QKeySequence, QPalette,
                           QRegularExpressionValidator)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup,
                               QCheckBox, QColorDialog, QComboBox,
                               QCommandLinkButton, QDateEdit, QDial, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFontComboBox, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QKeySequenceEdit,
                               QLabel, QLCDNumber, QLineEdit, QListWidget,
                               QMainWindow, QMenu, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton,
                               QSlider, QSpinBox, QStackedWidget, QTimeEdit,
                               QToolButton, QVBoxLayout, QWidget)

THEMES = ["System", "Light", "Dark"]


@dataclass
class Settings:
    user_name: str = "Operator"
    station_id: str = "ST-01"
    language: str = "English"
    font_family: str = "Sans Serif"
    font_size: int = 10
    theme: str = "System"
    accent: str = "#2f6fdf"
    density: int = 50                   # 0..100
    zoom: int = 100                     # %
    autosave: bool = True
    autosave_minutes: int = 5
    confirm_exit: int = 2               # Qt.CheckState as int (tristate)
    data_folder: str = ""
    start_date: str = field(default_factory=lambda:
                            QDate.currentDate().toString(Qt.ISODate))
    shift_start: str = "07:00"
    tolerance: float = 0.05
    capture_key: str = "Ctrl+Shift+C"
    notes: str = ""
    export_format: str = "CSV"

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def settings_path() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    return Path(base or Path.home()) / "qt_example_settings.json"


def read_settings(path: Optional[Path] = None) -> Settings:
    p = path or settings_path()
    try:
        return Settings.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return Settings()


def write_settings(s: Settings, path: Optional[Path] = None) -> Path:
    p = path or settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
    tmp.replace(p)                              # atomic on one drive
    return p


def apply_theme(app: QApplication, s: Settings) -> None:
    app.setStyle("Fusion")
    pal = app.style().standardPalette()
    if s.theme == "Dark":
        pal = QPalette()
        dark, mid, text = QColor(37, 37, 38), QColor(53, 53, 55), \
            QColor(230, 230, 230)
        for role, c in ((QPalette.Window, mid), (QPalette.Base, dark),
                        (QPalette.AlternateBase, mid),
                        (QPalette.Button, mid), (QPalette.Text, text),
                        (QPalette.WindowText, text),
                        (QPalette.ButtonText, text),
                        (QPalette.ToolTipBase, dark),
                        (QPalette.ToolTipText, text)):
            pal.setColor(role, c)
    pal.setColor(QPalette.Highlight, QColor(s.accent))
    app.setPalette(pal)
    app.setFont(QFont(s.font_family, s.font_size))


# ============================================================
# The dialog
# ============================================================

class SettingsDialog(QDialog):
    def __init__(self, current: Settings, on_apply, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(720, 480)
        self.original = current
        self.on_apply = on_apply
        self._accent = current.accent

        self.pages = QListWidget(maximumWidth=150)
        self.stack = QStackedWidget()
        for title, page in (("General", self._general()),
                            ("Appearance", self._appearance()),
                            ("Data", self._data()),
                            ("Shortcuts", self._shortcuts())):
            self.pages.addItem(title)
            self.stack.addWidget(page)
        self.pages.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.pages.setCurrentRow(0)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
            | QDialogButtonBox.Apply | QDialogButtonBox.RestoreDefaults)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.clicked.connect(self._clicked)

        body = QHBoxLayout()
        body.addWidget(self.pages)
        body.addWidget(self.stack, 1)
        v = QVBoxLayout(self)
        v.addLayout(body, 1)
        v.addWidget(self.buttons)

        self.to_widgets(current)
        self._watch_changes()
        self._mark_clean()

    # ---------------------------------------------------------- pages
    def _general(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.user_name = QLineEdit()
        self.user_name.setValidator(QRegularExpressionValidator(
            QRegularExpression(r"[A-Za-z][A-Za-z0-9 _.-]{0,31}")))
        f.addRow("Name", self.user_name)
        self.station_id = QLineEdit()
        self.station_id.setInputMask(">AA-99;_")     # two letters, two digits
        f.addRow("Station ID", self.station_id)
        self.language = QComboBox(editable=True)
        self.language.addItems(["English", "Deutsch", "Español", "Français"])
        f.addRow("Language", self.language)
        self.confirm_exit = QCheckBox("Ask before closing",
                                      tristate=True)
        self.confirm_exit.setToolTip("Partly checked: ask only when there "
                                     "is unsaved work")
        f.addRow(self.confirm_exit)

        self.autosave = QGroupBox("Autosave", checkable=True)
        g = QFormLayout(self.autosave)
        self.autosave_minutes = QSpinBox(minimum=1, maximum=120, suffix=" min")
        g.addRow("Every", self.autosave_minutes)
        f.addRow(self.autosave)

        self.notes = QPlainTextEdit(placeholderText="Notes for this station")
        f.addRow("Notes", self.notes)
        return w

    def _appearance(self) -> QWidget:
        w = QWidget()
        grid = QGridLayout(w)
        themes = QGroupBox("Theme")
        tv = QVBoxLayout(themes)
        self.theme_group = QButtonGroup(self)
        for i, name in enumerate(THEMES):
            rb = QRadioButton(name)
            self.theme_group.addButton(rb, i)
            tv.addWidget(rb)
        grid.addWidget(themes, 0, 0)

        fonts = QGroupBox("Font")
        ff = QFormLayout(fonts)
        self.font_family = QFontComboBox()
        ff.addRow("Family", self.font_family)
        self.font_size = QSpinBox(minimum=7, maximum=24, suffix=" pt")
        ff.addRow("Size", self.font_size)
        grid.addWidget(fonts, 0, 1)

        self.accent_btn = QPushButton()
        self.accent_btn.clicked.connect(self._pick_accent)
        grid.addWidget(QLabel("Accent colour"), 1, 0)
        grid.addWidget(self.accent_btn, 1, 1)

        # A slider and a spin box showing the same value.
        self.density = QSlider(Qt.Horizontal, minimum=0, maximum=100)
        self.density_box = QSpinBox(minimum=0, maximum=100, suffix=" %")
        self.density.valueChanged.connect(self.density_box.setValue)
        self.density_box.valueChanged.connect(self.density.setValue)
        row = QHBoxLayout()
        row.addWidget(self.density, 1)
        row.addWidget(self.density_box)
        grid.addWidget(QLabel("Density"), 2, 0)
        grid.addLayout(row, 2, 1)

        self.zoom = QDial(minimum=50, maximum=200, notchesVisible=True,
                          wrapping=False)
        self.zoom_lcd = QLCDNumber(3)
        self.zoom.valueChanged.connect(self.zoom_lcd.display)
        grid.addWidget(QLabel("Zoom %"), 3, 0)
        zr = QHBoxLayout()
        zr.addWidget(self.zoom)
        zr.addWidget(self.zoom_lcd)
        grid.addLayout(zr, 3, 1)
        grid.setRowStretch(4, 1)
        return w

    def _data(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        row = QHBoxLayout()
        self.data_folder = QLineEdit(placeholderText="(not set)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_folder)
        row.addWidget(self.data_folder, 1)
        row.addWidget(browse)
        f.addRow("Data folder", row)
        self.start_date = QDateEdit(calendarPopup=True,
                                    displayFormat="yyyy-MM-dd")
        f.addRow("Start date", self.start_date)
        self.shift_start = QTimeEdit(displayFormat="HH:mm")
        f.addRow("Shift starts", self.shift_start)
        self.tolerance = QDoubleSpinBox(minimum=0.0, maximum=10.0,
                                        decimals=3, singleStep=0.005,
                                        suffix=" mm")
        f.addRow("Tolerance", self.tolerance)

        self.export_btn = QToolButton(popupMode=QToolButton.InstantPopup)
        menu = QMenu(self.export_btn)
        for fmt in ("CSV", "Excel", "JSON"):
            menu.addAction(fmt, lambda fmt=fmt: self._set_export(fmt))
        self.export_btn.setMenu(menu)
        f.addRow("Export as", self.export_btn)

        demo = QProgressBar(value=0)
        self.tolerance.valueChanged.connect(
            lambda v: demo.setValue(int(v * 10)))
        f.addRow("Tolerance used", demo)
        return w

    def _shortcuts(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.capture_key = QKeySequenceEdit()
        f.addRow("Capture", self.capture_key)
        reset = QCommandLinkButton("Reset every setting",
                                   "Defaults on every page, not only this one")
        reset.clicked.connect(lambda: self.to_widgets(Settings()))
        f.addRow(reset)
        return w

    # ---------------------------------------------------------- values
    def to_widgets(self, s: Settings) -> None:
        self.user_name.setText(s.user_name)
        self.station_id.setText(s.station_id)
        self.language.setCurrentText(s.language)
        self.confirm_exit.setCheckState(Qt.CheckState(s.confirm_exit))
        self.autosave.setChecked(s.autosave)
        self.autosave_minutes.setValue(s.autosave_minutes)
        self.notes.setPlainText(s.notes)
        self.theme_group.button(THEMES.index(s.theme)
                                if s.theme in THEMES else 0).setChecked(True)
        self.font_family.setCurrentFont(QFont(s.font_family))
        self.font_size.setValue(s.font_size)
        self._set_accent(s.accent)
        self.density.setValue(s.density)
        self.zoom.setValue(s.zoom)
        self.zoom_lcd.display(s.zoom)
        self.data_folder.setText(s.data_folder)
        self.start_date.setDate(QDate.fromString(s.start_date, Qt.ISODate))
        self.shift_start.setTime(QTime.fromString(s.shift_start, "HH:mm"))
        self.tolerance.setValue(s.tolerance)
        self.capture_key.setKeySequence(QKeySequence(s.capture_key))
        self._set_export(s.export_format)

    def from_widgets(self) -> Settings:
        return Settings(
            user_name=self.user_name.text(),
            station_id=self.station_id.text(),
            language=self.language.currentText(),
            confirm_exit=self.confirm_exit.checkState().value,
            autosave=self.autosave.isChecked(),
            autosave_minutes=self.autosave_minutes.value(),
            notes=self.notes.toPlainText(),
            theme=THEMES[max(0, self.theme_group.checkedId())],
            font_family=self.font_family.currentFont().family(),
            font_size=self.font_size.value(),
            accent=self._accent,
            density=self.density.value(),
            zoom=self.zoom.value(),
            data_folder=self.data_folder.text(),
            start_date=self.start_date.date().toString(Qt.ISODate),
            shift_start=self.shift_start.time().toString("HH:mm"),
            tolerance=round(self.tolerance.value(), 3),
            capture_key=self.capture_key.keySequence().toString(),
            export_format=self.export_btn.text())

    # ---------------------------------------------------------- helpers
    def _set_accent(self, colour: str) -> None:
        self._accent = colour
        self.accent_btn.setText(colour)
        self.accent_btn.setStyleSheet(
            f"background:{colour}; color:white; padding:4px;")
        self._changed()

    def _pick_accent(self) -> None:
        c = QColorDialog.getColor(QColor(self._accent), self, "Accent")
        if c.isValid():
            self._set_accent(c.name())

    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Data folder",
                                             self.data_folder.text())
        if d:
            self.data_folder.setText(d)

    def _set_export(self, fmt: str) -> None:
        self.export_btn.setText(fmt)
        self._changed()

    def _watch_changes(self) -> None:
        """Every editor's change signal marks the dialog dirty."""
        for w in self.findChildren(QWidget):
            for sig in ("textChanged", "valueChanged", "currentIndexChanged",
                        "dateChanged", "timeChanged", "keySequenceChanged",
                        "toggled", "stateChanged"):
                signal = getattr(w, sig, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(self._changed)
                    break

    def _changed(self, *_: object) -> None:
        if hasattr(self, "buttons"):
            self.buttons.button(QDialogButtonBox.Apply).setEnabled(True)

    def _mark_clean(self) -> None:
        self.buttons.button(QDialogButtonBox.Apply).setEnabled(False)

    def _clicked(self, button: QAbstractButton) -> None:
        role = self.buttons.standardButton(button)
        if role == QDialogButtonBox.Apply:
            self.on_apply(self.from_widgets(), save=False)
            self._mark_clean()
        elif role == QDialogButtonBox.RestoreDefaults:
            self.to_widgets(Settings())

    def accept(self) -> None:
        self.on_apply(self.from_widgets(), save=True)
        super().accept()

    def reject(self) -> None:
        self.on_apply(self.original, save=False)    # undo any Apply
        super().reject()


# ============================================================
# The main window: shows what the settings do
# ============================================================

class MainWindow(QMainWindow):
    def __init__(self, path: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("Settings demo")
        self.resize(520, 340)
        self.path = path or settings_path()
        self.settings = read_settings(self.path)

        w = QWidget()
        v = QVBoxLayout(w)
        self.greeting = QLabel()
        self.greeting.setStyleSheet("font-size: 16pt;")
        v.addWidget(self.greeting)
        self.summary = QLabel(wordWrap=True, alignment=Qt.AlignTop)
        v.addWidget(self.summary, 1)
        open_btn = QPushButton("Settings…")
        open_btn.clicked.connect(self.edit_settings)
        v.addWidget(open_btn, alignment=Qt.AlignRight)
        self.setCentralWidget(w)
        self.apply(self.settings, save=False)

    def edit_settings(self) -> None:
        SettingsDialog(self.settings, self.apply, self).exec()

    def apply(self, s: Settings, save: bool) -> None:
        self.settings = s
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, s)
        self.greeting.setText(f"Hello, {s.user_name} at {s.station_id}")
        self.summary.setText(
            f"Theme {s.theme}, accent {s.accent}, {s.font_family} "
            f"{s.font_size} pt, zoom {s.zoom}%, density {s.density}%.\n"
            f"Autosave {'every %d min' % s.autosave_minutes if s.autosave else 'off'}"
            f"; tolerance {s.tolerance} mm; capture key {s.capture_key}; "
            f"export {s.export_format}.")
        if save:
            where = write_settings(s, self.path)
            self.statusBar().showMessage(f"Saved to {where}")


def make_window(argv: Optional[List[str]] = None) -> MainWindow:
    return MainWindow()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = make_window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
