"""
council_qt.widgets.settings_tabs — Typhon's camera settings as tabs under
the image folder: Basic (the boxes beside Start), then the CONNECTED
camera's own categories, then its presets.

WHY ATTACH BUILDS THEM, NOT THE WIREFRAME
A generated app can only make the widgets its wireframe draws, and which
tabs a camera needs is known only once it is connected: an EVK4 has biases,
four filters and a display; a Basler has exposure, gain and an image
format. So the gspec draws a notebook ("settings_tabs") with its first tab,
Basic — the exposure, gain and FPS boxes, keeping their ports and links, so
Start, the FPS box's own link and every hand-edited handler work as they
did — and frame_camera.attach hands that notebook to this helper, as it
hands the preset box to PresetPicker and the slider to CaptureReviewer. It
adds the rest at run time, from settings_list() grouped by council_core.
camera_categories, and builds them again when the camera changes.

A TAB IS A GLANCE; A POP-OUT IS THE CATEGORY
Each category is a section of a tab: its most-used settings, compact, and a
Pop out button that opens the whole category in a window of its own
(camera_settings_window.open_category) — several can be open at once, each
writing live through the same throttle, so the live view shows a bias as it
is dragged. Everything here is camera_settings_window's: the rows
(SettingRow), the throttle and the answers (SettingsCore), the presets list
(PresetsMixin) — one implementation, laid out in tabs.

IN THE APP'S COLOURS
The generated widgets are drawn in the window's own colours (Typhon's teal
and white). Rows made here take the same colours from the main window's
palette, so a note under a row picks a colour readable on teal
(camera_settings_window._tone reads the background it is drawn on).

NOTHING HERE TOUCHES THE CAMERA ON A TIMER
It reads the camera when it is told something changed (on_camera_change),
and once REFRESH_MS after its own writes stop — never per tick, so the live
view pays nothing for it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

from council_core import camera_categories as cats
from council_qt.widgets import camera_settings_window as csw

#: What the runtime pages are called (tests and stylesheets find them).
PAGE_OBJECT = "council_settings_page"

#: The Presets tab's title. Always the last tab.
PRESETS_TAB = "Presets"

#: Said in a tab while no camera is connected.
NO_CAMERA = ("Connect a camera to see its settings. They come from the "
             "camera itself: an event camera's biases, filters and display; "
             "a frame camera's exposure, gain and image format — each with "
             "Pop out.")

#: The Basic tab's note while no camera is connected.
NOTE_NO_CAMERA = ("Connect a camera to see its settings — they fill the tabs "
                  "beside Basic.")


def _quietly_set(port: Any, value: Any) -> None:
    """Put `value` in a generated port's widget without it counting as a
    change: its signals are held (no on_change, no script link), and the
    port is told this is its value now — it compares each change with the
    last value it saw, and would otherwise miss the user's next one."""
    widget = getattr(port, "widget", None)
    if widget is None:
        return
    held = widget.blockSignals(True)
    try:
        port.set(value)
    except Exception:                                     # noqa: BLE001
        return
    finally:
        widget.blockSignals(held)
    raw = getattr(port, "_raw", None)
    if callable(raw):
        try:
            port._last = raw()
        except Exception:                                 # noqa: BLE001
            pass


class SettingsTabs(csw.PresetsMixin, csw.SettingsCore, QObject):
    """The main window's settings notebook, kept true to the connected
    camera.

    `api` is frame_camera; `book` the gspec's QTabWidget, whose own tabs
    (Basic) are kept as they are and never removed; `note` the Basic tab's
    note label port, or None; `boxes` the ports of the boxes beside Start
    ("exposure", "gain", "frame_rate"), greyed when the connected camera
    cannot honour them; `font` the app's font, for what is made here."""

    def __init__(self, api: Any, book: QTabWidget, note: Any = None,
                 boxes: Optional[Dict[str, Any]] = None,
                 font: Optional[QFont] = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self._init_core(api)
        self._init_presets()
        self.book = book
        #: The wireframe's own tabs (Basic): before every tab made here.
        self.fixed = book.count()
        self.note_port = note
        self.boxes = dict(boxes or {})
        self.font = QFont(font) if font is not None else QFont(book.font())
        #: The tabs made here for the camera, by title, in order.
        self.pages: Dict[str, QWidget] = {}
        #: Which section (group) each Pop out button opens, by group.
        self.pop_buttons: Dict[str, QToolButton] = {}
        #: The plan the pages were built from (camera_categories.plan).
        self.plan: List[cats.Tab] = []
        self.status_labels: List[QLabel] = []
        self.area_edit: Optional[QLineEdit] = None
        self.area_apply: Optional[QPushButton] = None
        self.area_full: Optional[QPushButton] = None
        self.sensor_label: Optional[QLabel] = None
        self.label = ""
        self.has_exposure = True
        self.has_gain = True
        #: The text of the last say(), for tests and a page made after it.
        self.said = ""
        self._dress()
        self.presets_page = self._presets_page()
        self._listen()
        # The listener goes with the window: a closed window must not keep
        # being told about a camera it no longer shows.
        book.destroyed.connect(self.close)
        self.reload()

    def close(self, *_: Any) -> None:
        try:
            self.api_remove()
        except Exception:                                 # noqa: BLE001
            pass

    def _listen(self) -> None:
        remove = self.api.on_camera_change(self.heard)
        self.api_remove = remove
        self.destroyed.connect(lambda *_a, r=remove: r())

    # ------------------------------------------------------------------
    # Looks
    # ------------------------------------------------------------------
    def _dress(self) -> None:
        bar = self.book.tabBar()
        # A FONT IN A STYLE SHEET, NOT setFont. The main window has a style
        # sheet, and under it a widget with none of its own is given its
        # parent's font when it is polished: setFont on the tab bar and the
        # pages was undone, and the tabs came out in the 9 pt default
        # (measured offscreen; the generated widgets keep Arial 11 because
        # each has a style sheet of its own). A descendant rule reaches every
        # row made here, and sets only the font — each widget keeps its own
        # look.
        self.font_rule = (f"font-family: \"{self.font.family()}\"; "
                          f"font-size: {self.font.pointSizeF():g}pt;")
        bar.setStyleSheet(f"QTabBar {{ {self.font_rule} }}")
        bar.setUsesScrollButtons(True)
        bar.setExpanding(False)
        self.book.setDocumentMode(False)
        window = self.book.window()
        window.ensurePolished()
        palette = QPalette(window.palette())
        self.bg = palette.color(QPalette.ColorRole.Window)
        self.fg = palette.color(QPalette.ColorRole.WindowText)
        for role, colour in ((QPalette.ColorRole.Window, self.bg),
                             (QPalette.ColorRole.Base, self.bg),
                             (QPalette.ColorRole.AlternateBase, self.bg),
                             (QPalette.ColorRole.WindowText, self.fg),
                             (QPalette.ColorRole.Text, self.fg),
                             (QPalette.ColorRole.ButtonText, self.fg),
                             (QPalette.ColorRole.Button, self.bg.lighter(125))):
            palette.setColor(role, colour)
        self.colours = palette

    def _new_page(self, title: str, tip: str) -> "tuple[QWidget, QVBoxLayout]":
        """A tab in the app's colours: a scrolling column for the sections,
        and a status line under it."""
        page = QWidget()
        page.setObjectName(PAGE_OBJECT)
        page.setPalette(self.colours)
        page.setAutoFillBackground(True)
        # Notes pick their colour from this (camera_settings_window._tone).
        page.setProperty("council_dark", self.bg.lightness() < 128)
        page.setFont(self.font)
        # Rows a point under the app's font, notes two: a tab is 190-270 px
        # high between the smallest window and the design size.
        size = self.font.pointSizeF()
        page.setStyleSheet(
            f"#{PAGE_OBJECT}, #{PAGE_OBJECT} QWidget {{ "
            f"font-family: \"{self.font.family()}\"; "
            f"font-size: {max(7.0, size - 1):g}pt; }} "
            f"#{PAGE_OBJECT} QLabel[council_small=\"true\"] "
            f"{{ font-size: {max(7.0, size - 2):g}pt; }} "
            f"#{PAGE_OBJECT} QLabel[council_heading=\"true\"] "
            f"{{ font-weight: bold; }}")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(2)
        scroll = QScrollArea(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setPalette(self.colours)
        inner.setAutoFillBackground(True)
        column = QVBoxLayout(inner)
        column.setContentsMargins(0, 0, 4, 0)
        column.setSpacing(4)
        scroll.setWidget(inner)
        scroll.viewport().setPalette(self.colours)
        outer.addWidget(scroll, 1)
        status = QLabel(page)
        status.setWordWrap(True)
        status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        small = QFont(self.font)
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1))
        status.setFont(small)
        status.setProperty("council_small", True)
        status.setText(self.said)
        status.setVisible(bool(self.said))
        outer.addWidget(status)
        self.status_labels.append(status)
        page.setProperty("council_tab", title)
        at = self.book.count()
        if self.presets_page_index() >= 0:
            at = self.presets_page_index()
        index = self.book.insertTab(at, page, title)
        self.book.setTabToolTip(index, tip)
        return page, column

    def presets_page_index(self) -> int:
        page = getattr(self, "presets_page", None)
        return self.book.indexOf(page) if page is not None else -1

    def _heading(self, parent: QWidget, column: QVBoxLayout, text: str,
                 group: str = "") -> None:
        """A section's title, and its Pop out button when it has a
        category to pop out."""
        row = QHBoxLayout()
        title = QLabel(text, parent)
        title.setProperty("council_heading", True)
        row.addWidget(title, 1)
        if group:
            button = QToolButton(parent)
            button.setText("Pop out ⧉")
            button.setToolTip(f"Every {group} setting in a window of its "
                              f"own, written live as you change it")
            button.setAutoRaise(False)
            button.clicked.connect(lambda _c=False, g=group: self.pop_out(g))
            row.addWidget(button)
            self.pop_buttons[group] = button
        column.addLayout(row)

    def _note(self, parent: QWidget, column: QVBoxLayout, text: str) -> QLabel:
        label = QLabel(text, parent)
        label.setWordWrap(True)
        small = QFont(self.font)
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1))
        label.setFont(small)
        label.setProperty("council_small", True)
        csw._colour(label, csw._tone(label, "dim"))
        column.addWidget(label)
        return label

    # ------------------------------------------------------------------
    # The hooks SettingsCore calls
    # ------------------------------------------------------------------
    def _show_camera(self, state: Dict[str, Any]) -> None:
        self.label = str(state.get("label") or "")
        self.has_exposure = bool(state.get("has_exposure", True)) \
            if state.get("connected") else True
        self.has_gain = bool(state.get("has_gain", True)) \
            if state.get("connected") else True

    def _related(self, groups: set) -> Optional[set]:
        """A change in a group can move the rest of its TAB (a Basler's
        frame-rate limit and the rate it reaches follow its exposure, all in
        Exposure) — not the camera: the other tabs are not read for it."""
        out = set(groups)
        for tab in self.plan:
            if out.intersection(tab.groups):
                out.update(tab.groups)
        return out

    def _remove_pages(self) -> None:
        for page in list(self.pages.values()):
            index = self.book.indexOf(page)
            if index >= 0:
                self.book.removeTab(index)
            page.deleteLater()
        self.pages = {}
        self.pop_buttons = {}
        # The Presets tab is never rebuilt: its status line is the first.
        self.status_labels = self.status_labels[:1]
        self.area_edit = self.area_apply = self.area_full = None
        self.sensor_label = None

    def _clear_form(self, message: str = "") -> None:
        current = self._current_title()
        self.rows = {}
        self._shape = None
        self.plan = []
        self._remove_pages()
        if message:
            page, column = self._new_page(cats.CAMERA_TAB,
                                          "The connected camera's settings")
            self.pages[cats.CAMERA_TAB] = page
            note = QLabel(NO_CAMERA if message == self.NO_CAMERA else message,
                          page)
            note.setWordWrap(True)
            column.addWidget(note)
            column.addStretch(1)
        self._restore_title(current)

    def _build_form(self, settings: Sequence[Dict[str, Any]],
                    groups: Sequence[str]) -> None:
        current = self._current_title()
        self.rows = {}
        self._remove_pages()
        self.plan = cats.plan(settings, groups)
        by_key = {str(s["key"]): s for s in settings}
        for tab in self.plan:
            page, column = self._new_page(tab.title, tab.tip)
            self.pages[tab.title] = page
            parent = column.parentWidget()
            before = set(self.rows)
            if tab.title == cats.CAMERA_TAB:
                # THE AREA FIRST: it is what is changed here; the readings
                # and the camera's identity under it are a glance. Last, it
                # was below the fold of a 214 px page at 1400 x 820.
                self._area_section(parent, column)
            for section in tab.sections:
                self._section(parent, column, section, by_key,
                              titled=len(tab.sections) > 1 or
                              tab.title != section.group)
            column.addStretch(1)
            # One label column across the tab's sections, so the sliders
            # line up (each section is a grid of its own).
            labels = [self.rows[k].label for k in self.rows if k not in before]
            if labels:
                width = min(csw.SettingRow.COMPACT_LABEL,
                            max(lab.sizeHint().width() for lab in labels))
                for label in labels:
                    label.setMinimumWidth(width)
        self._restore_title(current)

    def _section(self, parent: QWidget, column: QVBoxLayout,
                 section: cats.Section, by_key: Dict[str, Dict[str, Any]],
                 titled: bool) -> None:
        self._heading(parent, column, section.group if titled else "",
                      section.group)
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(1)
        for index, key in enumerate(section.shown):
            setting = by_key.get(key)
            if setting is None:
                continue
            self._keep_row(csw.SettingRow(setting, grid, index * 2, parent,
                                          compact=True))
        column.addLayout(grid)
        if section.more:
            names = [str(by_key[k].get("label") or k) for k in section.more
                     if k in by_key]
            listed = ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
            self._note(parent, column,
                       f"+{len(names)} more in Pop out: {listed}")

    def _area_section(self, parent: QWidget, column: QVBoxLayout) -> None:
        """The camera's own area, typed in sensor pixels — as the settings
        window has it. Every camera has an area, so every Camera tab has
        this, whether or not the camera describes any readings."""
        self._heading(parent, column, "Area (sensor px)")
        row = QHBoxLayout()
        self.area_edit = QLineEdit(parent)
        self.area_edit.setPlaceholderText("x, y, w, h")
        self.area_edit.setToolTip(
            "The camera's OWN area, x, y, width, height in SENSOR pixels: an "
            "EVK4 emits events only inside it, a Basler reads out only it. "
            "Snapped to what the sensor accepts. Not while capturing.")
        self.area_edit.returnPressed.connect(self.apply_area)
        row.addWidget(self.area_edit, 1)
        self.area_apply = QPushButton("Apply", parent)
        self.area_apply.clicked.connect(self.apply_area)
        row.addWidget(self.area_apply)
        self.area_full = QPushButton("Full sensor", parent)
        self.area_full.clicked.connect(self.full_sensor)
        row.addWidget(self.area_full)
        column.addLayout(row)
        self.sensor_label = self._note(parent, column, "")
        self.refresh_area()

    def _presets_page(self) -> QWidget:
        page, column = self._new_page(PRESETS_TAB,
                                      "This camera's presets — every setting "
                                      "and its area, kept in this project; "
                                      "export one to another project or PC")
        box = self._build_presets(column.parentWidget(), title="",
                                  compact=True)
        box.setFlat(True)
        column.addWidget(box)
        return page

    def _current_title(self) -> str:
        index = self.book.currentIndex()
        return self.book.tabText(index) if index >= self.fixed else ""

    def _restore_title(self, title: str) -> None:
        if not title:
            return
        for index in range(self.fixed, self.book.count()):
            if self.book.tabText(index) == title:
                self.book.setCurrentIndex(index)
                return

    def _cleared(self) -> None:
        self._clear_presets()
        self.presets_file.setText("Connect a camera to see its presets.")

    def refresh_area(self) -> None:
        if not self.connected or self.area_edit is None:
            return
        try:
            area = self.api.current_area()
        except Exception:                                 # noqa: BLE001
            return
        if not self.area_edit.hasFocus():
            self.area_edit.setText(str(area.get("area") or ""))
        if self.sensor_label is not None:
            whole = " — the whole sensor" if area.get("full") else ""
            self.sensor_label.setText(
                f"Sensor {area.get('sensor', '')}{whole}.")
        self.preset_with_area.setText(
            f"with the camera's area ({area.get('area', '')})")

    def _show_took(self, head: str, lines: List[str]) -> None:
        # Problems first (took_lines): the first of them, after the summary.
        trouble = [line for line in lines if line.startswith(("✗", "≈"))]
        if trouble:
            self.say(f"{head} {trouble[0]}", tone="warn")

    # ------------------------------------------------------------------
    # Enabled, and why not
    # ------------------------------------------------------------------
    def apply_state(self) -> None:
        on = self.connected and not self.busy
        settled = on and not self.capturing
        self._rows_state()
        self._presets_state(on, settled)
        for widget, tip in ((self.area_apply, "Set the camera's own area to "
                                              "these sensor pixels"),
                            (self.area_full, "Give the whole sensor back")):
            if widget is not None:
                widget.setToolTip(
                    "Stop the capture first — one run keeps one camera "
                    "set-up." if self.capturing and self.connected else tip)
        for widget in (self.area_edit, self.area_apply, self.area_full):
            if widget is not None:
                widget.setEnabled(settled)
        for button in self.pop_buttons.values():
            button.setEnabled(self.connected)
        self._basic_boxes()
        self._basic_note()

    def _basic_boxes(self) -> None:
        """Grey the boxes beside Start that this camera cannot honour (an
        event camera has no exposure and no gain) — and say so."""
        for name, has in (("exposure", self.has_exposure),
                          ("gain", self.has_gain)):
            widget = getattr(self.boxes.get(name), "widget", None)
            if widget is None:
                continue
            widget.setEnabled(has)
            widget.setToolTip("" if has else
                              f"This camera has no {name} — an event camera "
                              f"collects events, not light")
            self._look_off(widget, not has)

    #: What a box beside Start says for its 0 while this camera has no such
    #: setting.
    NOT_HERE = "n/a — event camera"

    def _look_off(self, widget: Any, off: bool) -> None:
        """A box that is off LOOKS off. The generated style sheet gives each
        spin box the app's colours with no :disabled rule, so the disabled
        Exposure box was the live FPS box pixel for pixel (review: 0 of
        5148 pixels differed) — only a click that did nothing, or the
        tooltip, said it was off. While off it is drawn flat on the window's
        colour, in a dim dashed outline, and says NOT_HERE for its 0."""
        if bool(widget.property("council_off")) == off:
            return                       # apply_state runs often: no repolish
        if widget.property("council_sheet") is None:
            widget.setProperty("council_sheet", widget.styleSheet())
            special = getattr(widget, "specialValueText", None)
            widget.setProperty("council_special",
                               special() if callable(special) else "")
        sheet = str(widget.property("council_sheet") or "")
        set_special = getattr(widget, "setSpecialValueText", None)
        if off:
            # By its name: the generated class is a subclass of QSpinBox
            # whose own name a type selector would have to spell. An id
            # with :disabled outranks the generated "QSpinBox#name" rule.
            name = widget.objectName()
            selector = f"#{name}" if name else "*"
            dark = self.bg.lightness() < 128
            ground = (self.bg.darker(160) if dark
                      else self.bg.darker(110)).name()
            dim = "#8fa9b2" if dark else "#8a8a8a"
            widget.setStyleSheet(
                f"{sheet}\n{selector}:disabled {{ "
                f"background-color: {ground}; color: {dim}; "
                f"border: 1px dashed {dim}; }}")
            if callable(set_special):
                set_special(self.NOT_HERE)
        else:
            widget.setStyleSheet(sheet)
            if callable(set_special):
                set_special(str(widget.property("council_special") or ""))
        widget.setProperty("council_off", off)

    # ------------------------------------------------------------------
    # The boxes beside Start show what the camera has
    # ------------------------------------------------------------------
    def _took_setting(self, out: Dict[str, Any]) -> None:
        super()._took_setting(out)
        change = dict(out.get("change") or {})
        if change.get("ok", out.get("ok", True)) and not change.get(
                "skipped"):
            self._follow_boxes([str(out.get("key") or "")])

    def _took_set(self, out: Dict[str, Any]) -> None:
        super()._took_set(out)
        self._follow_boxes(str(c.get("key")) for c in
                           (out.get("applied") or {}).get("changes") or []
                           if c.get("ok", True) and not c.get("skipped"))

    def _follow_boxes(self, keys: Any) -> None:
        """A tab, a pop-out, a preset or a reset set what a box beside Start
        also sets (frame_camera.START_BOXES): the box shows the camera's
        value now. Measured before: Exposure 3000 µs set in the Exposure
        tab, and the Basic box still said 12000 — through a capture and
        the next Start, which kept the camera's 3000 (rightly) while the
        box said otherwise; an EVK4's FPS box said 0 over a 100 ms window.

        QUIETLY: the box's change signal is held, so this is not "the user
        changed the box" (no stamp, so Start does not write it back, and
        the FPS box's own link does not write the camera again). Shown as
        the box can show it: whole µs, whole dB, whole fps — and 0, the
        box's "keep", while an auto mode owns the value."""
        owned = getattr(self.api, "START_BOXES", {})
        keys = set(keys)
        for box, names in owned.items():
            port = self.boxes.get(box)
            if port is None or not keys.intersection(names):
                continue
            value = self._box_value(box)
            if value is None:
                continue
            _quietly_set(port, value)
            told = getattr(self.api, "box_shows_camera", None)
            if callable(told):
                told(box, value)

    def _box_value(self, box: str) -> Optional[int]:
        """What the box `box` shows for the camera's setting now (rows), or
        None when the tabs do not show it."""
        def value(key: str) -> Any:
            row = self.rows.get(key)
            return None if row is None else row.setting.get("value")

        def number(key: str) -> Optional[float]:
            got = csw._number(value(key))
            return got

        def auto(key: str) -> bool:
            mode = value(key)
            return mode is not None and str(mode).strip().lower() != "off"

        if box == "exposure":
            if auto("ExposureAuto"):
                return 0
            got = number("ExposureTime")
        elif box == "gain":
            if auto("GainAuto"):
                return 0
            got = number("Gain")
        else:
            window = number("window_ms")
            if window is not None:
                return int(round(1000.0 / window)) if window > 0 else 0
            enabled = value("AcquisitionFrameRateEnable")
            if enabled is not None and not csw._same(enabled, True):
                return 0                     # free-running: the box's 0
            got = number("AcquisitionFrameRate")
        return None if got is None else int(round(got))

    def _basic_note(self) -> None:
        port = self.note_port
        if port is None:
            return
        if not self.connected:
            text = NOTE_NO_CAMERA
        else:
            titles = [t.title for t in self.plan]
            text = (f"{self.label}: its own settings are in the tabs beside "
                    f"Basic — {', '.join(titles + [PRESETS_TAB])}. Pop out "
                    f"opens a category in a window of its own.")
            if not self.has_exposure and not self.has_gain:
                text += (" No exposure or gain on an event camera: FPS sets "
                         "its picture window.")
        try:
            if port.get() != text:
                port.set(text)
        except Exception:                                 # noqa: BLE001
            pass

    def say(self, text: str, tone: str = "") -> None:
        self.said = text
        alive = []
        for label in self.status_labels:
            if not csw.alive(label):
                continue
            alive.append(label)
            csw._colour(label, csw._tone(label, tone) if tone else "")
            label.setText(text)
            label.setVisible(bool(text))
        self.status_labels = alive

    # ------------------------------------------------------------------
    # The tabs' own buttons
    # ------------------------------------------------------------------
    def pop_out(self, group: str) -> Any:
        """Open `group` in a window of its own (or bring its open one
        forward). Built but not shown under COUNCIL_NO_DIALOGS."""
        self.flush_now()
        parent = self.book.window()
        return csw.open_category(group, parent=parent,
                                 show=not csw._dialogs_disabled(),
                                 api=self.api, font=self.font)

    def apply_area(self) -> None:
        if self.area_edit is None:
            return
        out = self._call("Camera area", self.api.set_camera_area,
                         self.area_edit.text())
        if out is not None:
            self._took_other(out)

    def full_sensor(self) -> None:
        out = self._call("Full sensor", self.api.full_frame)
        if out is not None:
            self._took_other(out)

    def _dialog_parent(self) -> Optional[QWidget]:
        """Export's folder dialog and Import's file dialog belong to the
        main window. This view is a QObject, not a window: with no parent a
        dialog opens as a window of its own (its own taskbar entry, free to
        open behind Typhon) rather than over the tabs that asked."""
        return self.book.window()

    # -- for tests and the measurements ------------------------------------
    def titles(self) -> List[str]:
        return [self.book.tabText(i) for i in range(self.book.count())]

    def page(self, title: str) -> Optional[QWidget]:
        if title == PRESETS_TAB:
            return self.presets_page
        return self.pages.get(title)
