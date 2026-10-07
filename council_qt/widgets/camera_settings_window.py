"""
council_qt.widgets.camera_settings_window — every setting the connected
camera has, its area, and its presets, in one non-modal window; and one
CATEGORY of them in a window of its own (a pop-out).

Typhon's answer to Metavision Studio's settings panel and the pylon
Viewer's feature tree. frame_camera.camera_settings (a "Camera settings…"
button) opens the whole window through `open_settings`; the tabs under the
image folder (council_qt.widgets.settings_tabs) pop a category out through
`open_category`.

ONE IMPLEMENTATION, THREE VIEWS
Reading the camera, writing it through the throttle, and showing what it
took is `SettingsCore`, which the whole window, a category's pop-out and
the main window's tabs all use; the controls of one setting are
`SettingRow`; the presets list is `PresetsMixin`. A view only lays them
out. So a rule fixed here — a refused write put back, a held setting
greyed, a write held behind a restart — holds in all three.

WHAT IT SHOWS COMES FROM THE CAMERA
Every control is built from frame_camera.settings_list(): the camera's own
groups, kinds, ranges, increments, units and entries (pylon's node map, the
Metavision HAL's facilities). Nothing here knows what an EVK4 or a Basler
has, so a model with more or fewer features gets more or fewer rows, and a
range is never a number written in this file.

WHAT TOOK IS WHAT IS SHOWN
After a write the control shows what the camera REPORTS, not what was asked:
a clamped gain, a snapped exposure, a bias the sensor quantised or refused —
each also said beside the row and in the status line. A refused write puts
the control back to the camera's value.

LIVE, BUT NEVER A WRITE PER PIXEL OF A DRAG
A slider or a spin box writes through a short throttle (WRITE_MS): while it
moves, the newest value is written at most once per WRITE_MS, and the last
one is always written — so the live picture follows the drag and the camera
is not sent hundreds of values it would only overwrite. The settings shown
are read again only REFRESH_MS after the writes stop (a frame-rate limit's
range follows the exposure on a Basler; an auto mode greys out what it
owns) — a pop-out reads only its own category then.

EVERY VIEW HEARS EVERY CHANGE
Each view listens to frame_camera.on_camera_change, so a bias dragged in a
pop-out moves the same bias in the tabs and in another pop-out — the one
answer, said once by frame_camera, shown everywhere.

THE UI THREAD IS NOT HELD FOR LONG
A live setting is one write and one read-back of that one setting (camera_
settings looks up a single feature, not the node map). A setting the stream
is in the way of, a preset, the area: frame_camera stops and restarts the
stream on a worker and waits at most APPLY_WAIT_SECONDS; past that the view
says "applying…", greys itself, and the answer arrives through
on_camera_change. Measured numbers are in docs/camera_quickstart.md.

ONE SET-UP PER RUN
While capturing, what a capture refuses (the area, presets, a setting the
stream is in the way of, Reset all) is greyed out and says why; settings that
change live stay live. frame_camera refuses them anyway — this only saves
the user a refusal.

THROUGH FRAME_CAMERA ONLY
No view holds a device or a presets file: every read and write is a
frame_camera call, which keeps the run rule, the worker and the listeners in
one place. `api` is that module, handed in, so this module never imports a
top-level one and a test can give it a fake.
"""
from __future__ import annotations

import math
import os
import time
from collections import deque
from typing import (Any, Callable, Deque, Dict, List, Optional, Sequence,
                    Tuple)

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QKeySequence, QPainter, QPalette,
                           QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QCheckBox,
                               QComboBox, QDoubleSpinBox, QFileDialog,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QScrollArea,
                               QSizePolicy, QSlider, QSpinBox, QSplitter,
                               QStyle, QStyleOptionSlider, QToolButton,
                               QVBoxLayout, QWidget)

#: Throttle for a moving slider or spin box: at most one write per WRITE_MS,
#: the newest value, and always the last one.
WRITE_MS = 60

#: How long after the last write the settings shown are read again (ranges
#: and auto modes that one write can change). Restarted by every write, so a
#: drag reads the camera once, when it stops.
REFRESH_MS = 300

#: How long "Delete" waits for its second click.
CONFIRM_MS = 4000

#: Slider resolution for a setting whose range has more values than this.
SLIDER_STEPS = 1000

#: QSpinBox holds a C int; a wider integer range gets a QDoubleSpinBox with
#: no decimals (an EVK4's event-rate limit runs to 1e9, a threshold may not).
INT32 = 2 ** 31 - 1

FLOAT, INT, BOOL, CHOICE, TEXT = "float", "int", "bool", "choice", "text"

#: How many call timings a view keeps (see SettingsCore.timings).
TIMINGS_KEPT = 4096

#: The window's object name, which tests and the menu look it up by.
OBJECT_NAME = "council_camera_settings"

#: A category pop-out's object name: this, ":", and the category.
CATEGORY_OBJECT = "council_camera_category"

#: What an exported preset file is called (camera_presets.EXPORT_SUFFIX),
#: for Import's file filter — said here so this module imports no core.
EXPORT_PATTERN = "*.camera-preset.json"


# ======================================================================
# Small helpers
# ======================================================================
def show(value: Any) -> str:
    """A value for a person: floats lose read-back noise (a gain of 3 reads
    back 2.999994 on the emulator), as camera_settings.show does."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{round(value, 4):g}"
    return "" if value is None else str(value)


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _decimals(setting: Dict[str, Any]) -> int:
    """Decimals a float setting is shown with: from its increment when the
    camera reports one (ExposureTime 0.1 -> 1), else from its span."""
    if setting.get("type") == INT:
        return 0
    step = _number(setting.get("step"))
    if step and step > 0:
        return max(0, min(6, math.ceil(-math.log10(step) - 1e-9)))
    low, high = _number(setting.get("min")), _number(setting.get("max"))
    if low is None or high is None:
        return 3
    span = abs(high - low)
    return 3 if span <= 10 else 2 if span <= 1000 else 1


def _on_dark(widget: QWidget) -> bool:
    """Whether `widget` is drawn on a dark background (see _tone)."""
    holder: Optional[QWidget] = widget
    while holder is not None:
        said = holder.property("council_dark")
        if said is not None:
            return bool(said)
        holder = holder.parentWidget()
    return widget.palette().color(QPalette.ColorRole.Window).lightness() < 128


def _tone(widget: QWidget, kind: str) -> str:
    """A text colour readable on the widget's own background: a window takes
    the system palette (light or dark), the main window's tabs the app's
    teal — so the colour is chosen from the background it is drawn on.

    A view drawn in colours of its own says so on an ancestor
    ("council_dark"): under a style sheet the palette a label reports is not
    the one it is painted on (measured: a note in Typhon's teal tabs read a
    light palette and came out dark grey on teal)."""
    dark = _on_dark(widget)
    return {"bad": "#ff8a80" if dark else "#b3261e",
            "warn": "#ffd180" if dark else "#7a4f00",
            "dim": "#c8d6dc" if dark else "#5f5f5f",
            "good": "#8fe3a0" if dark else "#2e8540"}.get(kind, "")


def _short(value: Any) -> str:
    """A range end for a person: 20000000 is "20,000,000", not "2e+07"."""
    number = _number(value)
    if number is None:
        return show(value)
    if float(number).is_integer() and abs(number) >= 10000:
        return f"{int(number):,}"
    return show(float(number) if not float(number).is_integer()
                else int(number))


def range_text(setting: Dict[str, Any]) -> str:
    """The camera's own range for a number setting, with its unit and, for
    a bias, the sensor's recommended range: "-85 … 140" / "rec. -25 … 60".
    "" for a setting the camera gives no range."""
    low, high = setting.get("min"), setting.get("max")
    if _number(low) is None or _number(high) is None:
        return ""
    unit = str(setting.get("unit") or "")
    text = f"{_short(low)} … {_short(high)} {unit}".rstrip()
    rec = setting.get("recommended")
    if rec:
        text += f"\nrec. {_short(rec[0])} … {_short(rec[1])}"
    return text


def _colour(label: QLabel, colour: str) -> None:
    """Set a label's text colour — only when it changes. setStyleSheet
    re-polishes the widget even for the same sheet, and the window used to
    do it for every row on every write of a dragged slider."""
    sheet = f"color: {colour};" if colour else ""
    if label.property("council_sheet") != sheet:
        label.setProperty("council_sheet", sheet)
        label.setStyleSheet(sheet)


def _dialogs_disabled() -> bool:
    return bool(os.environ.get("COUNCIL_NO_DIALOGS"))


class Scale:
    """Slider position <-> value.

    LOGARITHMIC for a range spanning more than three decades from a positive
    minimum — an exposure of 20 µs .. 10 s — or every short exposure is
    crushed into the first pixel of the slider. Exact steps for a small
    integer range (a bias of -35..55 has 91 positions, each a value)."""

    def __init__(self, low: float, high: float, kind: str,
                 step: Optional[float]):
        self.low, self.high = float(low), float(high)
        self.kind = kind
        self.log = self.low > 0 and self.high / self.low > 1000
        span = self.high - self.low
        self.step = float(step) if step else (1.0 if kind == INT else 0.0)
        if (kind == INT and not self.log and self.step > 0
                and span / self.step <= SLIDER_STEPS):
            self.steps = max(1, int(round(span / self.step)))
            self.exact = True
        else:
            self.steps = SLIDER_STEPS
            self.exact = False

    def position(self, value: Any) -> int:
        v = _number(value)
        if v is None or self.high <= self.low:
            return 0
        v = min(max(v, self.low), self.high)
        if self.log:
            f = ((math.log(v) - math.log(self.low))
                 / (math.log(self.high) - math.log(self.low)))
        else:
            f = (v - self.low) / (self.high - self.low)
        return int(round(f * self.steps))

    def value(self, position: int) -> float:
        f = min(max(position / self.steps, 0.0), 1.0)
        if self.log:
            v = math.exp(math.log(self.low)
                         + f * (math.log(self.high) - math.log(self.low)))
        else:
            v = self.low + f * (self.high - self.low)
        if self.kind == INT:
            step = self.step or 1.0
            v = self.low + round((v - self.low) / step) * step
            return float(int(round(min(max(v, self.low), self.high))))
        return v


def took_lines(applied: Dict[str, Any],
               labels: Optional[Dict[str, str]] = None) -> List[str]:
    """What a set did, one line per setting, problems first — the "What
    took" box. `applied` is camera_settings.Applied.as_dict()."""
    labels = labels or {}
    refused, adjusted, left, done = [], [], [], []
    for change in applied.get("changes") or []:
        name = labels.get(change.get("key"), change.get("key", "?"))
        value, asked = show(change.get("value")), show(change.get("asked"))
        note = change.get("note") or ""
        if not change.get("ok", True):
            refused.append(f"✗ {name}: NOT changed — {note}")
        elif change.get("skipped"):
            left.append(f"– {name}: left at {value} — {note}")
        elif change.get("adjusted"):
            adjusted.append(f"≈ {name}: {value} (asked {asked})")
        else:
            done.append(f"✓ {name}: {value}")
    lines = refused + adjusted
    asked = applied.get("roi_asked")
    if asked:
        got = applied.get("roi")
        if got:
            snapped = " (snapped)" if list(got) != list(asked) else ""
            lines.append(f"✓ area: {', '.join(str(v) for v in got)}{snapped}")
        else:
            lines.append(f"✗ area NOT set: {applied.get('roi_error', '')}")
    return lines + left + done


# ======================================================================
# One setting
# ======================================================================
class ElidedLabel(QLabel):
    """A one-line label that ends in "…" when it is narrower than its text
    (QLabel itself only clips)."""

    def paintEvent(self, event: Any) -> None:            # noqa: N802
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, self.width())
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignLeft
                                          | Qt.AlignmentFlag.AlignVCenter),
                         text)
        painter.end()

    def minimumSizeHint(self) -> QSize:                  # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), 48), hint.height())


class RangeSlider(QSlider):
    """A slider that marks the RECOMMENDED part of its range: a bar under
    the groove from the low to the high end of what the sensor recommends
    (a bias's get_bias_recommended_range).

    An EVK4's bias may be set far outside what works well (bias_diff_on runs
    -85 … 140; -25 … 60 is recommended). The row already says so in words
    once the value is outside; the bar shows where "inside" is while the
    slider is being dragged, in a tab too narrow for the words."""

    def __init__(self, orientation: Qt.Orientation, parent: QWidget):
        super().__init__(orientation, parent)
        #: (from, to) in slider positions, or None: nothing to mark.
        self.recommended: Optional[Tuple[int, int]] = None

    def mark(self, span: Optional[Tuple[int, int]]) -> None:
        if span != self.recommended:
            self.recommended = span
            self.update()

    def paintEvent(self, event: Any) -> None:            # noqa: N802
        super().paintEvent(event)
        if self.recommended is None or self.maximum() <= self.minimum():
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                      QStyle.SubControl.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                      QStyle.SubControl.SC_SliderHandle, self)
        room = max(1, groove.width() - handle.width())
        left = groove.left() + handle.width() / 2.0

        def x(position: int) -> float:
            return left + QStyle.sliderPositionFromValue(
                self.minimum(), self.maximum(), position, room)

        start, end = sorted(self.recommended)
        painter = QPainter(self)
        painter.fillRect(QRectF(x(start), self.height() - 3.0,
                                max(2.0, x(end) - x(start)), 2.0),
                         QColor(_tone(self, "good")))
        painter.end()


class SettingRow(QObject):
    """The widgets of one setting in its group's grid: label, slider, editor
    (with the unit), a reset button, and a note under them.

    `edited(key, value)` is emitted for a USER change only — every update
    from the camera goes through `show_value` with the signals quiet.

    `compact` is the main window's tabs: narrower slider and box, and a
    one-line label, elided, rather than widening a 430 px column.

    A SETTING THE STREAM IS IN THE WAY OF IS WRITTEN ON RELEASE. Each write
    of one (a Basler's binning) stops the live view, writes and starts it
    again; a drag wrote one per throttle tick — a stream restarted a dozen
    times for one gesture. Its slider says nothing while held and writes the
    value it is let go at. A live setting writes as it moves.

    `explain_read_only`: say under a read-only row why it cannot change —
    where it sits among settings that can (a group of readings says so once,
    above them).

    `show_range`: a column after the box with the camera's own range and
    unit, and a bias's recommended range — a pop-out is where a category is
    tuned, so the limits are on the row rather than in a tooltip. Both views
    mark the recommended part of a slider (RangeSlider)."""

    edited = Signal(str, object)
    reset = Signal(str)

    #: Minimum widths: (slider, number box), roomy and compact.
    WIDTHS = {False: (120, 110), True: (56, 88)}

    #: The widest a compact row's label is drawn (longer is elided).
    COMPACT_LABEL = 150

    def __init__(self, setting: Dict[str, Any], grid: QGridLayout, row: int,
                 parent: QWidget, compact: bool = False,
                 explain_read_only: bool = False, show_range: bool = False):
        super().__init__(parent)
        self.setting = dict(setting)
        self.key = str(setting["key"])
        self.kind = str(setting.get("type") or TEXT)
        self.read_only = bool(setting.get("read_only"))
        self.compact = bool(compact)
        self.explain_read_only = bool(explain_read_only)
        #: The range column (show_range), or None.
        self.range_label: Optional[QLabel] = None
        self._quiet = False
        #: The answer to the last write, said under the row until replaced.
        self.result = ""
        self.result_tone = ""
        #: As the camera had it when connected, or None: what reset puts back.
        self.connected_value: Any = None
        self.scale: Optional[Scale] = None
        self.slider: Optional[QSlider] = None
        self.editor: Optional[QWidget] = None

        text = str(setting.get("label") or self.key)
        if self.compact and self.kind == BOOL and self.key.endswith(
                ".enabled"):
            # Under its section's heading ("Anti-flicker") the flag is
            # simply "On"; "Anti-flicker on" took three wrapped lines.
            text = "On"
        if self.compact:
            # ONE LINE, ELIDED — a wrapped label in a scrolling tab was cut
            # off at the bottom with no scroll bar (a scroll area sizes its
            # column before the wrap is known). The tooltip has it whole.
            self.label = ElidedLabel(text, parent)
            self.label.setMaximumWidth(self.COMPACT_LABEL)
        else:
            self.label = QLabel(text, parent)
        self.label.setToolTip(self._tooltip())
        grid.addWidget(self.label, row, 0)
        self._build(grid, row, parent)
        last = 3
        if show_range:
            last = 4
            self.range_label = QLabel(parent)
            self.range_label.setProperty("council_small", True)
            small = self.range_label.font()
            small.setPointSizeF(max(7.0, small.pointSizeF() - 1))
            self.range_label.setFont(small)
            _colour(self.range_label, _tone(self.range_label, "dim"))
            self.range_label.setToolTip(
                "The camera's own range for this setting"
                + (" — and, under it, the range its maker recommends"
                   if setting.get("recommended") else ""))
            grid.addWidget(self.range_label, row, 3)
            self._show_range()
        self.reset_button = QToolButton(parent)
        self.reset_button.setText("↺")
        self.reset_button.setAutoRaise(True)
        self.reset_button.clicked.connect(lambda: self.reset.emit(self.key))
        self.reset_button.setVisible(False)
        grid.addWidget(self.reset_button, row, last)
        self.note = QLabel(parent)
        self.note.setWordWrap(True)
        # Smaller than the row (a style sheet rule of the tabs says so too:
        # under a style sheet setFont alone is undone).
        self.note.setProperty("council_small", True)
        font = self.note.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        self.note.setFont(font)
        self.note.setVisible(False)
        grid.addWidget(self.note, row + 1, 1, 1, last)
        self.show_value(setting.get("value"))

    # -- building ----------------------------------------------------------
    def _tooltip(self) -> str:
        s = self.setting
        bits = [str(s.get("help") or "").strip()]
        low, high = s.get("min"), s.get("max")
        if low is not None or high is not None:
            bits.append(f"Allowed: {show(low)} … {show(high)} "
                        f"{s.get('unit') or ''}".rstrip())
        if s.get("recommended"):
            lo, hi = s["recommended"]
            bits.append(f"Recommended: {show(lo)} … {show(hi)}")
        if s.get("type") == CHOICE and s.get("choices"):
            bits.append("One of: " + ", ".join(map(str, s["choices"])))
        bits.append(f"({self.key})")
        return "\n".join(b for b in bits if b)

    def _build(self, grid: QGridLayout, row: int, parent: QWidget) -> None:
        s = self.setting
        if self.read_only or self.kind == TEXT:
            self.editor = QLabel(parent)
            self.editor.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            if not self.compact:
                # Greyed, as a reading is: nothing here can be changed.
                self.editor.setEnabled(False)
            grid.addWidget(self.editor, row, 1, 1, 2)
            return
        if self.kind == BOOL:
            box = QCheckBox(parent)
            box.toggled.connect(self._from_check)
            self.editor = box
            grid.addWidget(box, row, 1, 1, 2)
            return
        if self.kind == CHOICE:
            combo = QComboBox(parent)
            combo.addItems([str(c) for c in s.get("choices") or []])
            combo.textActivated.connect(self._from_choice)
            self.editor = combo
            grid.addWidget(combo, row, 1, 1, 2)
            return
        box = self._number_box(parent)
        self.editor = box
        low, high = _number(s.get("min")), _number(s.get("max"))
        if low is not None and high is not None and high > low:
            self.scale = Scale(low, high, self.kind, _number(s.get("step")))
            slider = RangeSlider(Qt.Orientation.Horizontal, parent)
            slider.setRange(0, self.scale.steps)
            slider.setMinimumWidth(self.WIDTHS[self.compact][0])
            slider.valueChanged.connect(self._from_slider)
            slider.sliderReleased.connect(self._released)
            self.slider = slider
            self._mark_recommended()
            grid.addWidget(slider, row, 1)
            grid.addWidget(box, row, 2)
        else:
            grid.addWidget(box, row, 1, 1, 2)

    def _mark_recommended(self) -> None:
        """The recommended part of the slider (RangeSlider), from the
        setting's own description."""
        if not isinstance(self.slider, RangeSlider) or self.scale is None:
            return
        rec = self.setting.get("recommended")
        try:
            span = ((self.scale.position(rec[0]), self.scale.position(rec[1]))
                    if rec else None)
        except (TypeError, IndexError):
            span = None
        self.slider.mark(span)

    def _show_range(self) -> None:
        if self.range_label is not None:
            text = (range_text(self.setting)
                    if self.kind in (FLOAT, INT) and not self.read_only
                    else "")
            self.range_label.setText(text)

    def _number_box(self, parent: QWidget) -> QAbstractSpinBox:
        s = self.setting
        low, high = _number(s.get("min")), _number(s.get("max"))
        step = _number(s.get("step"))
        whole = self.kind == INT and all(
            v is None or abs(v) <= INT32 for v in (low, high))
        if whole:
            box: QAbstractSpinBox = QSpinBox(parent)
            box.setRange(int(low) if low is not None else -INT32,
                         int(high) if high is not None else INT32)
            box.setSingleStep(max(1, int(step or 1)))
            box.valueChanged.connect(self._from_box)
        else:
            box = QDoubleSpinBox(parent)
            box.setDecimals(_decimals(s))
            box.setRange(low if low is not None else -1e15,
                         high if high is not None else 1e15)
            if step:
                box.setSingleStep(step)
            elif low is not None and high is not None and high > low:
                box.setSingleStep(10 ** math.floor(math.log10(
                    max((high - low) / 100.0, 1e-6))))
            box.valueChanged.connect(self._from_box)
        unit = str(s.get("unit") or "")
        if unit:
            box.setSuffix(f" {unit}")
        # Typing writes on Return or on leaving the box, never per keystroke
        # (a partial "12" of "1200" would be sent to the camera).
        box.setKeyboardTracking(False)
        box.setAccelerated(True)
        box.setMinimumWidth(self.WIDTHS[self.compact][1])
        box.setSizePolicy(QSizePolicy.Policy.Preferred,
                          QSizePolicy.Policy.Fixed)
        return box

    # -- the user's changes -----------------------------------------------
    def _typed(self, value: float) -> Any:
        return int(round(value)) if self.kind == INT else float(value)

    @property
    def live(self) -> bool:
        """Changes while the camera streams (False: the stream must stop)."""
        return bool(self.setting.get("live", True))

    def _from_slider(self, position: int) -> None:
        if self._quiet or self.scale is None:
            return
        value = self._typed(self.scale.value(position))
        self._quietly(lambda: self.editor.setValue(value))
        if not self.live and self.slider.isSliderDown():
            return                      # written on release (_released)
        self.edited.emit(self.key, value)

    def _released(self) -> None:
        """The slider let go: a setting the stream is in the way of is
        written now, once, at the value it was let go at."""
        if self._quiet or self.scale is None or self.live:
            return
        self.edited.emit(self.key,
                         self._typed(self.scale.value(self.slider.value())))

    def _from_box(self, value: float) -> None:
        if self._quiet:
            return
        value = self._typed(value)
        if self.slider is not None and self.scale is not None:
            position = self.scale.position(value)
            self._quietly(lambda: self.slider.setValue(position))
        self.edited.emit(self.key, value)

    def _from_check(self, on: bool) -> None:
        if not self._quiet:
            self.edited.emit(self.key, bool(on))

    def _from_choice(self, text: str) -> None:
        if not self._quiet:
            self.edited.emit(self.key, str(text))

    def _quietly(self, fn: Callable[[], Any]) -> None:
        was, self._quiet = self._quiet, True
        try:
            fn()
        finally:
            self._quiet = was

    # -- what the camera says ----------------------------------------------
    def busy_editing(self) -> bool:
        """The user is mid-gesture here: a slider held down, or a number
        being typed. A refresh must not snatch the value from under them."""
        if self.slider is not None and self.slider.isSliderDown():
            return True
        editor = self.editor
        if isinstance(editor, QAbstractSpinBox):
            line = editor.lineEdit()
            return editor.hasFocus() and line is not None and line.isModified()
        return False

    def update(self, setting: Dict[str, Any]) -> None:
        """A fresh description of this setting: its value, and its range
        and entries where the camera changed them (a Basler's frame-rate
        limit follows its exposure)."""
        old = self.setting
        self.setting = dict(setting)
        if isinstance(self.editor, QAbstractSpinBox) and (
                old.get("min") != setting.get("min")
                or old.get("max") != setting.get("max")):
            self._new_range()
        if isinstance(self.editor, QComboBox) and list(
                old.get("choices") or []) != list(setting.get("choices") or []):
            combo = self.editor

            def refill() -> None:
                combo.clear()
                combo.addItems([str(c) for c in setting.get("choices") or []])
            self._quietly(refill)
        self.label.setToolTip(self._tooltip())
        if old.get("recommended") != setting.get("recommended"):
            self._mark_recommended()
        if (old.get("min"), old.get("max"), old.get("recommended")) != (
                setting.get("min"), setting.get("max"),
                setting.get("recommended")):
            self._show_range()
        if not self.busy_editing():
            self.show_value(setting.get("value"))

    def _new_range(self) -> None:
        s = self.setting
        low, high = _number(s.get("min")), _number(s.get("max"))
        box = self.editor

        def apply() -> None:
            if isinstance(box, QSpinBox):
                box.setRange(int(low) if low is not None else -INT32,
                             int(high) if high is not None else INT32)
            elif isinstance(box, QDoubleSpinBox):
                box.setRange(low if low is not None else -1e15,
                             high if high is not None else 1e15)
        self._quietly(apply)
        if self.slider is not None and low is not None and high is not None \
                and high > low:
            self.scale = Scale(low, high, self.kind, _number(s.get("step")))
            self._quietly(lambda: self.slider.setRange(0, self.scale.steps))
            self._mark_recommended()

    def show_value(self, value: Any) -> None:
        """Put the camera's value in the controls, without writing it."""
        self.setting["value"] = value
        editor = self.editor

        def put() -> None:
            if isinstance(editor, QLabel):
                unit = str(self.setting.get("unit") or "")
                editor.setText(f"{show(value)} {unit}".strip())
            elif isinstance(editor, QCheckBox):
                editor.setChecked(bool(value))
            elif isinstance(editor, QComboBox):
                index = editor.findText(str(value),
                                        Qt.MatchFlag.MatchFixedString)
                if index < 0 and value is not None:
                    editor.addItem(str(value))
                    index = editor.count() - 1
                editor.setCurrentIndex(index)
            elif isinstance(editor, QAbstractSpinBox):
                number = _number(value)
                if number is not None:
                    editor.setValue(number)
                    line = editor.lineEdit()
                    if line is not None:
                        line.setModified(False)
                if self.slider is not None and self.scale is not None \
                        and number is not None:
                    self.slider.setValue(self.scale.position(number))
        self._quietly(put)

    def restore(self) -> None:
        """Back to the last value the camera reported (after a refusal)."""
        self.show_value(self.setting.get("value"))

    # -- enabled, and why not --------------------------------------------
    def set_state(self, capturing: bool, busy: bool,
                  connected_value: Any = None) -> None:
        s = self.setting
        self.connected_value = connected_value
        held = str(s.get("held") or "")
        live = bool(s.get("live", True))
        writable = not self.read_only and self.kind != TEXT
        enabled = writable and not busy and not held and not (
            capturing and not live)
        for widget in (self.slider, self.editor):
            if widget is not None and writable:
                widget.setEnabled(enabled)
        can_reset = (writable and connected_value is not None
                     and not _same(connected_value, s.get("value")))
        self.reset_button.setVisible(writable and connected_value is not None)
        self.reset_button.setEnabled(enabled and can_reset)
        self.reset_button.setToolTip(
            f"Back to {show(connected_value)} — as the camera had it when it "
            f"was connected" if connected_value is not None else "")

        notes: List[Tuple[str, str]] = []
        if self.result:
            notes.append((self.result, self.result_tone))
        if not writable and self.explain_read_only:
            why = str(s.get("help") or "").strip() or (
                "the camera reports it; it cannot be set from here")
            notes.append((f"Read only — {why}", "dim"))
        if held:
            notes.append((f"Greyed out while {held} — change that first",
                          "dim"))
        elif not live and capturing:
            # A tab's row is ~230 px of note: the short form keeps it to
            # one line (three such rows of two lines filled a 190 px page).
            notes.append(("Stop the capture to change this" if self.compact
                          else "Stop the capture to change this — one run "
                               "keeps one set-up", "dim"))
        elif not live and writable:
            notes.append(("Restarts the live view" if self.compact
                          else "Changing this restarts the live view for a "
                               "moment", "dim"))
        rec = s.get("recommended")
        value = _number(s.get("value"))
        if rec and value is not None and not rec[0] <= value <= rec[1]:
            notes.append((f"Outside the recommended {show(rec[0])} … "
                          f"{show(rec[1])}", "warn"))
        if notes:
            text, tone = notes[0]
            if len(notes) > 1:
                text = " · ".join(n for n, _ in notes)
                tone = notes[0][1] or notes[1][1]
            _colour(self.note, _tone(self.note, tone))
            self.note.setText(text)
            self.note.setVisible(True)
        else:
            self.note.setVisible(False)


def _same(a: Any, b: Any) -> bool:
    x, y = _number(a), _number(b)
    if isinstance(a, bool) or isinstance(b, bool) or x is None or y is None:
        return str(a).strip().lower() == str(b).strip().lower()
    return math.isclose(x, y, rel_tol=1e-5, abs_tol=1e-6)


def add_group_boxes(layout: QVBoxLayout, parent: QWidget,
                    settings: Sequence[Dict[str, Any]],
                    groups: Sequence[str],
                    keep: Callable[["SettingRow"], None],
                    show_range: bool = False) -> None:
    """One box per group, in the camera's group order, each a grid of
    SettingRows — the whole window's form and a pop-out's. `keep` is told
    of every row made (to register it and connect its signals);
    `show_range` gives each row its range column (a pop-out)."""
    order = list(groups) + [s.get("group") for s in settings
                            if s.get("group") not in groups]
    for group in dict.fromkeys(order):
        members = [s for s in settings if s.get("group") == group]
        if not members:
            continue
        box = QGroupBox(str(group), parent)
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        grid.setVerticalSpacing(2)
        readings = all(s.get("read_only") or s.get("type") == TEXT
                       for s in members)
        first = 0
        if readings:
            # A group of readings says it once, not under every row.
            note = QLabel("Readings — the camera reports these; they cannot "
                          "be set.", box)
            note.setWordWrap(True)
            note.setEnabled(False)
            grid.addWidget(note, 0, 0, 1, 4)
            first = 1
        for index, setting in enumerate(members):
            keep(SettingRow(setting, grid, first + index * 2, box,
                            explain_read_only=not readings,
                            show_range=show_range))
        layout.addWidget(box)


# ======================================================================
# What every view does: read, write through the throttle, show what took
# ======================================================================
class SettingsCore:
    """The controller a settings VIEW is built on — a mixin for a QObject
    (it parents its timers to `self` and stops listening when `self` is
    destroyed).

    A view supplies its layout through these hooks: _build_form(settings,
    groups) and _clear_form(message) for the rows; apply_state() for what is
    enabled (calling _rows_state for the rows); say(text, tone) for the
    status line; and, where it has them, _show_camera(state), _cleared(),
    refresh_area(), fill_presets(select), select_preset(name) and
    _show_took(head, lines)."""

    #: Said in place of the rows while no camera is connected.
    NO_CAMERA = ("Connect a camera in the main window — its settings come "
                 "from the camera itself, and this window fills in when it "
                 "is connected.")

    def _init_core(self, api: Any, groups: Sequence[str] = ()) -> None:
        self.api = api
        #: The groups this view shows; () is every group the camera has.
        self.groups: Tuple[str, ...] = tuple(groups)
        self.rows: Dict[str, SettingRow] = {}
        self.connected = False
        self.capturing = False
        self.busy = False
        self.factory = ""
        self.camera_kind = ""
        self.as_connected: Dict[str, Any] = {}
        #: (label, milliseconds on the UI thread) of the latest camera calls
        #: made from here, newest last — the measurements read it. Bounded:
        #: a drag adds sixteen a second for as long as the view is open.
        self.timings: Deque[Tuple[str, float]] = deque(maxlen=TIMINGS_KEPT)
        self._shape: Optional[tuple] = None
        self._pending: Dict[str, Any] = {}
        #: Why the last call was refused, or "".
        self.last_error = ""

        self._write_timer = QTimer(self)
        self._write_timer.setSingleShot(True)
        self._write_timer.setInterval(WRITE_MS)
        self._write_timer.timeout.connect(self._flush)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(REFRESH_MS)
        self._refresh_timer.timeout.connect(self.refresh_values)
        # After a preset or a reset: everything read again once control is
        # back in the event loop, not inside the call that applied it.
        self._soon_timer = QTimer(self)
        self._soon_timer.setSingleShot(True)
        self._soon_timer.setInterval(0)
        self._soon_timer.timeout.connect(self._refresh_all)
        #: The last answer a listener call brought, so the caller that made
        #: the change does not handle the same answer a second time.
        self._last_heard: Dict[str, Any] = {}

    def _listen(self) -> None:
        remove = self.api.on_camera_change(self.heard)
        # The lambda holds the remover, not the view: a view that is
        # deleted must stop hearing about the camera.
        self.destroyed.connect(lambda *_a, r=remove: r())

    # -- the hooks a view may leave alone ---------------------------------
    def _show_camera(self, state: Dict[str, Any]) -> None:
        pass

    def _cleared(self) -> None:
        pass

    def refresh_area(self) -> None:
        pass

    def fill_presets(self, select: str = "") -> None:
        pass

    def select_preset(self, name: str) -> bool:
        return False

    def _show_took(self, head: str, lines: List[str]) -> None:
        pass

    # ------------------------------------------------------------------
    # Reading the camera
    # ------------------------------------------------------------------
    def reload(self) -> None:
        """Everything again: the camera, its settings, area and presets."""
        state = self._state()
        self.connected = bool(state.get("connected"))
        self.capturing = bool(state.get("capturing"))
        self.busy = bool(state.get("busy"))
        self.camera_kind = str(state.get("kind") or "")
        self._show_camera(state)
        if not self.connected:
            # A value still in the throttle belonged to the camera that is
            # gone; writing it to the next one would be wrong.
            self._pending.clear()
            self._write_timer.stop()
            self._clear_form(self.NO_CAMERA)
            self._cleared()
            self.factory, self.as_connected = "", {}
            self.apply_state()
            return
        try:
            defaults = self.api.camera_defaults()
        except Exception:                                 # noqa: BLE001
            defaults = {}
        self.factory = str(defaults.get("factory") or "")
        self.as_connected = dict(defaults.get("values") or {})
        self._shape = None
        self.refresh_values()
        self.refresh_area()
        self.fill_presets()
        self.apply_state()

    def _state(self) -> Dict[str, Any]:
        try:
            return dict(self.api.camera_state())
        except Exception:                                 # noqa: BLE001
            return {"connected": False}

    def _read_settings(self) -> Dict[str, Any]:
        """settings_list — of this view's groups only, when it has some."""
        if self.groups:
            return self.api.settings_list(list(self.groups))
        return self.api.settings_list()

    def refresh_values(self) -> None:
        """Every setting shown read again; rows updated in place, or the
        form rebuilt when the camera now describes a different set."""
        if not self.connected:
            return
        try:
            listed = self._timed("read the settings", self._read_settings)
        except Exception as exc:                          # noqa: BLE001
            self.say(f"Could not read the camera's settings: {exc}",
                     tone="bad")
            return
        self.capturing = bool(listed.get("capturing"))
        settings = list(listed.get("settings") or [])
        shape = tuple((s["key"], s.get("type"), bool(s.get("read_only")),
                       s.get("group")) for s in settings)
        if shape != self._shape:
            self._build_form(settings, list(listed.get("groups") or []))
            self._shape = shape
        else:
            for setting in settings:
                row = self.rows.get(setting["key"])
                if row is not None and setting["key"] not in self._pending:
                    row.update(setting)
        self.apply_state()

    def _keep_row(self, row: SettingRow) -> None:
        """A row made by _build_form: listened to, and known by key."""
        row.edited.connect(self._edited)
        row.reset.connect(self.reset_one)
        self.rows[row.key] = row

    def _rows_state(self) -> None:
        on = self.connected and not self.busy
        for row in self.rows.values():
            row.set_state(self.capturing, not on,
                          self.as_connected.get(row.key))

    # ------------------------------------------------------------------
    # Writing — every call goes through frame_camera
    # ------------------------------------------------------------------
    def _timed(self, label: str, fn: Callable[..., Any], *args: Any) -> Any:
        started = time.perf_counter()
        try:
            return fn(*args)
        finally:
            self.timings.append((label, (time.perf_counter() - started)
                                 * 1000.0))

    def _call(self, label: str, fn: Callable[..., Any],
              *args: Any) -> Optional[Dict[str, Any]]:
        """Call frame_camera; say a refusal; grey the view while a change
        that needed the stream stopped is still running ("pending").

        Returns the answer for the caller to handle — or None when there is
        nothing left to handle: a refusal (said here), a pending change
        (its answer comes through `heard`), or an answer `heard` already
        handled (frame_camera tells listeners before it returns)."""
        self._last_heard = {}
        self.last_error = ""
        try:
            out = self._timed(label, fn, *args)
        except Exception as exc:                          # noqa: BLE001
            self.last_error = str(exc)
            self.say(f"{label}: {exc}", tone="bad")
            return None
        out = dict(out or {})
        if out.get("pending"):
            self.busy = True
            self.apply_state()
            self.say(str(out.get("summary") or "Applying…"))
            return None
        if self._last_heard and all(self._last_heard.get(k) == v
                                    for k, v in out.items()):
            return None
        return out

    def _soon(self) -> None:
        self._soon_timer.start()

    def _refresh_all(self) -> None:
        self.refresh_values()
        self.refresh_area()

    def _edited(self, key: str, value: Any) -> None:
        """A user change to one control: queued, written by the throttle."""
        self._pending[key] = value
        if not self._write_timer.isActive():
            self._write_timer.start()

    def _flush(self) -> None:
        if self.busy:
            # A change that restarts the stream is still running, and
            # frame_camera refuses every other change until it is done:
            # what is queued waits for its answer (`heard` flushes it).
            return
        began = time.perf_counter()
        pending, self._pending = self._pending, {}
        items = list(pending.items())
        for index, (key, value) in enumerate(items):
            if self.busy:
                # The write before this one went to a worker ("pending"):
                # these wait for its answer rather than being refused and
                # put back — measured, a gain changed together with the
                # pixel format was lost that way.
                self._pending = dict(items[index:], **self._pending)
                break
            row = self.rows.get(key)
            label = row.setting.get("label", key) if row else key
            out = self._call(f"{label}", self.api.set_camera_setting, key,
                             value)
            if out is not None:
                self._took_setting(out)
            elif (self.last_error and row is not None
                  and key not in self._pending):
                row.restore()
        if self._pending and not self._write_timer.isActive():
            self._write_timer.start()
        self._refresh_timer.start()
        # The whole cycle on the UI thread: the call, and the view's own
        # handling of its answer (measured in docs/camera_quickstart.md).
        self.timings.append(("write cycle",
                             (time.perf_counter() - began) * 1000.0))

    def flush_now(self) -> None:
        """Write what is queued at once (tests, and closing the view)."""
        self._write_timer.stop()
        self._flush()

    def reset_one(self, key: str) -> None:
        row = self.rows.get(key)
        label = row.setting.get("label", key) if row else key
        self._pending.pop(key, None)
        out = self._call(f"Reset {label}", self.api.reset_setting, key)
        if out is not None:
            self._took_setting(out)
        self._refresh_timer.start()

    # ------------------------------------------------------------------
    # What the camera says — frame_camera.on_camera_change, UI thread
    # ------------------------------------------------------------------
    def heard(self, out: Dict[str, Any]) -> None:
        self._last_heard = dict(out)
        what = str(out.get("what") or "")
        if what in ("connected", "disconnected"):
            self.reload()
            if out.get("summary"):
                self.say(str(out["summary"]))
            return
        if what == "pending":
            # A change made elsewhere (the main window's Apply area, the
            # preset box) is restarting the stream: grey out and hold
            # what is queued, as for one made here.
            self.busy = True
            self.apply_state()
            if out.get("summary"):
                self.say(str(out["summary"]))
            return
        late = self.busy and what not in ("capturing", "stopped", "presets",
                                          "box", "streaming")
        if late:
            self.busy = False
            if self._pending and not self._write_timer.isActive():
                # Held while the stream restarted (_flush): written now.
                self._write_timer.start()
        if what == "setting":
            self._took_setting(out)
        elif what in ("settings", "preset", "reset"):
            self._took_set(out)
        elif what in ("area", "defaults"):
            self._took_other(out)
        elif what == "failed":
            self.say(str(out.get("summary") or "The change failed."),
                     tone="bad")
            self._soon()
        elif what in ("capturing", "stopped"):
            self.capturing = what == "capturing"
        elif what == "box":
            # A box beside Start wrote the camera (the FPS box: a picture
            # window, a frame-rate limit; Start: exposure and gain). Which
            # rows that moves is the camera's business, so they are read
            # again — once, after the clicks stop.
            self._refresh_timer.start()
        elif what == "streaming":
            # The live view started (or stopped) the stream. WHICH SETTINGS
            # CHANGE LIVE DEPENDS ON IT: a Basler locks its pixel format,
            # mirror and binning while it grabs. The rows were described
            # at Connect, before the live view's first tick started the
            # stream, so the tabs offered the pixel format as live — no
            # "restarts the live view", and a binning slider that wrote
            # (each write a stream restart) at every step of a drag.
            self._refresh_timer.start()
        elif what == "presets":
            self.fill_presets(select=str(out.get("name") or ""))
            if out.get("summary"):
                self.say(str(out["summary"]))
        if late:
            self._soon()
        if late or what != "setting":
            # One setting changes one row (done in _took_setting); every
            # other answer can change what is enabled anywhere.
            self.apply_state()

    def _took_setting(self, out: Dict[str, Any]) -> None:
        change = dict(out.get("change") or {})
        key = str(out.get("key") or change.get("key") or "")
        row = self.rows.get(key)
        if row is not None:
            if key not in self._pending and not row.busy_editing():
                row.show_value(change.get("value", out.get("value")))
            else:
                row.setting["value"] = change.get("value", out.get("value"))
            if not change.get("ok", True):
                row.result, row.result_tone = (
                    f"NOT changed — {change.get('note', '')}", "bad")
                row.restore()
            elif change.get("skipped"):
                row.result, row.result_tone = (str(change.get("note") or ""),
                                               "dim")
            elif change.get("adjusted"):
                row.result, row.result_tone = (
                    f"The camera made it {show(change.get('value'))} "
                    f"(asked {show(change.get('asked'))})", "warn")
            else:
                row.result, row.result_tone = "", ""
            row.set_state(self.capturing, self.busy or not self.connected,
                          self.as_connected.get(key))
            if row.kind in (BOOL, CHOICE):
                # An auto mode or an enable flag: what it owns changes now.
                self._refresh_timer.start()
        if out.get("area_moved"):
            # Binning changed the area's own numbers: the area box too.
            self.refresh_area()
        if row is None and self.groups and key:
            # A change to a setting this view does not show: not its news.
            return
        ok = bool(out.get("ok", True))
        self.say(str(out.get("summary") or ""),
                 tone="" if ok and not change.get("adjusted") else
                 ("warn" if ok else "bad"))

    def _took_set(self, out: Dict[str, Any]) -> None:
        applied = dict(out.get("applied") or {})
        labels = {k: r.setting.get("label", k) for k, r in self.rows.items()}
        lines = took_lines(applied, labels)
        head = str(out.get("summary") or "")
        self._show_took(head, lines)
        for row in self.rows.values():
            row.result, row.result_tone = "", ""
        self.say(head, tone="" if out.get("ok", True) else "warn")
        if out.get("what") == "preset" and out.get("name"):
            self.select_preset(str(out["name"]))
        self._soon()

    def _took_other(self, out: Dict[str, Any]) -> None:
        self.say(str(out.get("summary") or ""))
        if out.get("what") == "defaults":
            self._show_took(str(out.get("summary") or ""), [])
            self._soon()
        self.refresh_area()


# ======================================================================
# The presets list — the whole window's and the tabs' Presets tab
# ======================================================================
class PresetsMixin:
    """This camera's presets in a list, and what can be done with them:
    apply, save as, rename, delete (two clicks), export to a file and
    import one. For a SettingsCore view; builds its widgets as attributes
    of the view, so every view says them the same way."""

    def _init_presets(self) -> None:
        self._confirm_delete = ""
        self._confirm_timer = QTimer(self)
        self._confirm_timer.setSingleShot(True)
        self._confirm_timer.setInterval(CONFIRM_MS)
        self._confirm_timer.timeout.connect(self._cancel_delete)
        #: Where the last export went and the last import came from: the
        #: folder each file dialog opens in next.
        self.export_folder = ""
        self.import_folder = ""

    def _build_presets(self, parent: QWidget, title: str = "",
                       compact: bool = False) -> QGroupBox:
        box = QGroupBox(title if title or compact else
                        "Presets — settings + area, kept in this project",
                        parent)
        #: The main window's Presets tab: a 400 px column, not a window.
        self._presets_compact = bool(compact)
        layout = QVBoxLayout(box)
        self.preset_list = QListWidget(box)
        self.preset_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.preset_list.itemDoubleClicked.connect(
            lambda _item: self.apply_preset())
        self.preset_list.currentItemChanged.connect(self._preset_chosen)
        if compact:
            # A few rows, not the whole tab: the buttons under it are what
            # a short tab must still show (a page is 190 px high at the
            # smallest window).
            self.preset_list.setMinimumHeight(48)
            self.preset_list.setMaximumHeight(72)
        layout.addWidget(self.preset_list, 0 if compact else 1)
        row = QHBoxLayout()
        self.preset_apply = QPushButton("Apply", box)
        self.preset_apply.setToolTip("Put the camera back as this preset has "
                                     "it: its settings, then its area. Not "
                                     "while capturing.")
        self.preset_apply.clicked.connect(self.apply_preset)
        row.addWidget(self.preset_apply)
        self.preset_rename = QPushButton(
            "Rename" if compact else "Rename to the name below", box)
        self.preset_rename.setToolTip("Rename the preset chosen above to the "
                                      "name typed below")
        self.preset_rename.clicked.connect(self.rename_preset)
        row.addWidget(self.preset_rename)
        self.preset_delete = QPushButton("Delete", box)
        self.preset_delete.clicked.connect(self.delete_preset)
        row.addWidget(self.preset_delete)
        layout.addLayout(row)

        save = QGridLayout()
        self.preset_name = QLineEdit(box)
        self.preset_name.setPlaceholderText("Preset name, e.g. Bird bath")
        self.preset_name.setMaxLength(60)
        self.preset_name.returnPressed.connect(self.save_preset)
        save.addWidget(self.preset_name, 0, 0)
        self.preset_save = QPushButton("Save as", box)
        self.preset_save.setToolTip("Save the camera as it is now under this "
                                    "name (a preset of the same name is "
                                    "replaced). Works while capturing.")
        self.preset_save.clicked.connect(self.save_preset)
        save.addWidget(self.preset_save, 0, 1)
        self.preset_with_area = QCheckBox("with the camera's area", box)
        self.preset_with_area.setChecked(True)
        self.preset_with_area.setToolTip(
            "Ticked: applying the preset also sets the camera's own area "
            "(sensor px). Unticked: it leaves the area as it is.")
        # In the tab, Export and Import come straight under Save as, above
        # the fold of a short page (190 px at the smallest window); the
        # area tick and the note, set once, are under them.
        below = 2 if compact else 1
        save.addWidget(self.preset_with_area, below, 0, 1, 2)
        self.preset_note = QLineEdit(box)
        self.preset_note.setPlaceholderText("Note (optional)")
        save.addWidget(self.preset_note, below + 1, 0, 1, 2)
        layout.addLayout(save)

        files = QHBoxLayout()
        self.preset_export = QPushButton("Export…", box)
        self.preset_export.setToolTip(
            "Write the preset chosen above to a file of its own "
            "(<name>.camera-preset.json) — to take to another project or "
            "PC. Never over another file.")
        self.preset_export.clicked.connect(lambda: self.export_preset())
        files.addWidget(self.preset_export)
        self.preset_import = QPushButton("Import…", box)
        self.preset_import.setToolTip(
            "Add an exported preset to this camera's presets here. Never "
            "replaces one: a name in use gets (2). Pick it to apply it.")
        self.preset_import.clicked.connect(lambda: self.import_preset())
        files.addWidget(self.preset_import)
        if compact:
            save.addLayout(files, 1, 0, 1, 2)
        else:
            layout.addLayout(files)
        self.presets_file = QLabel(box)
        self.presets_file.setWordWrap(True)
        self.presets_file.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.presets_file)
        return box

    def _clear_presets(self) -> None:
        self.preset_list.clear()
        self.presets_file.clear()

    def fill_presets(self, select: str = "") -> None:
        try:
            listed = self.api.list_presets()
        except Exception as exc:                          # noqa: BLE001
            self.presets_file.setText(f"Presets: {exc}")
            return
        keep = select or self.selected_preset()
        self.preset_list.blockSignals(True)
        try:
            self.preset_list.clear()
            details = list(listed.get("details") or [])
            for index, name in enumerate(listed.get("presets") or []):
                line = (listed.get("rows") or [name])[index] \
                    if index < len(listed.get("rows") or []) else name
                item = QListWidgetItem(str(line))
                item.setData(Qt.ItemDataRole.UserRole, str(name))
                if index < len(details):
                    item.setToolTip(_preset_tip(details[index]))
                self.preset_list.addItem(item)
                if keep and str(name).casefold() == keep.casefold():
                    self.preset_list.setCurrentItem(item)
        finally:
            self.preset_list.blockSignals(False)
        where = listed.get("file") or ""
        said = listed.get("summary") or ""
        if where and getattr(self, "_presets_compact", False):
            # A PATH CANNOT WRAP. In the tab, the whole path was the widest
            # thing on the page (507 px of a 401 px column, measured): the
            # page was laid out wider than the tab and Delete and Save as
            # were cut off at its edge. The name here, the path in the tip.
            self.presets_file.setText(
                f"{said} Kept in {os.path.basename(str(where))}, in this "
                f"app's project folder.")
            self.presets_file.setToolTip(str(where))
        else:
            self.presets_file.setText(f"{said} Kept in {where}" if where
                                      else said)
        self.apply_state()

    def selected_preset(self) -> str:
        item = self.preset_list.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    def select_preset(self, name: str) -> bool:
        for index in range(self.preset_list.count()):
            item = self.preset_list.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole)).casefold() \
                    == name.casefold():
                self.preset_list.setCurrentItem(item)
                return True
        return False

    def _preset_chosen(self, current: Any, _previous: Any = None) -> None:
        if current is not None:
            self.preset_name.setText(
                str(current.data(Qt.ItemDataRole.UserRole) or ""))
        self._cancel_delete()
        self.apply_state()

    def _presets_state(self, on: bool, settled: bool) -> None:
        """Enable the presets' controls: `on` — a camera, not busy;
        `settled` — and not capturing (applying waits for Stop)."""
        chosen = bool(self.selected_preset())
        for widget, enabled in (
                (self.preset_apply, settled and chosen),
                (self.preset_rename, on and chosen),
                (self.preset_delete, on and chosen),
                (self.preset_save, on), (self.preset_name, on),
                (self.preset_with_area, on), (self.preset_note, on),
                (self.preset_export, on and chosen),
                (self.preset_import, on)):
            widget.setEnabled(enabled)
        if self.capturing and self.connected:
            self.preset_apply.setToolTip("Stop the capture first — one run "
                                         "keeps one camera set-up.")
        else:
            self.preset_apply.setToolTip("Put the camera back as this preset "
                                         "has it: its settings, then its "
                                         "area.")

    def apply_preset(self) -> None:
        name = self.selected_preset()
        if not name:
            self.say("Choose a preset in the list first.", tone="bad")
            return
        self.flush_now()
        out = self._call(f"Preset {name!r}", self.api.apply_preset, name)
        if out is not None:
            self._took_set(out)

    def save_preset(self) -> None:
        name = self.preset_name.text().strip()
        out = self._call("Save preset", self.api.save_preset, name,
                         self.preset_with_area.isChecked(),
                         self.preset_note.text())
        if out is not None:
            self.fill_presets(select=str(out.get("name") or name))
            self.say(str(out.get("summary") or ""))

    def rename_preset(self) -> None:
        old, new = self.selected_preset(), self.preset_name.text().strip()
        if not old:
            self.say("Choose the preset to rename in the list first.",
                     tone="bad")
            return
        out = self._call("Rename preset", self.api.rename_preset, old, new)
        if out is not None:
            self.fill_presets(select=str(out.get("name") or new))
            self.say(str(out.get("summary") or ""))

    def delete_preset(self) -> None:
        """Two clicks: the first asks, the second deletes. Not a modal box —
        a dialog's nested event loop is no place for a live camera window."""
        name = self.selected_preset()
        if not name:
            return
        if self._confirm_delete != name:
            self._confirm_delete = name
            # The tab's row is narrow: the name is in the list, selected.
            self.preset_delete.setText(
                "Sure? Click again" if getattr(self, "_presets_compact",
                                               False)
                else f"Delete {name!r}? Click again")
            self._confirm_timer.start()
            return
        self._cancel_delete()
        out = self._call("Delete preset", self.api.delete_preset, name)
        if out is not None:
            self.fill_presets()
            self.say(str(out.get("summary") or ""))

    def _cancel_delete(self) -> None:
        self._confirm_delete = ""
        self._confirm_timer.stop()
        self.preset_delete.setText("Delete")

    def export_preset(self, folder: str = "") -> str:
        """Export the chosen preset into `folder` — asked for with a folder
        dialog when not given (none under COUNCIL_NO_DIALOGS); the file
        written, or ""."""
        name = self.selected_preset()
        if not name:
            self.say("Choose the preset to export in the list first.",
                     tone="bad")
            return ""
        if not folder:
            if _dialogs_disabled():
                self.say("Export: choose a folder (dialogs are disabled).",
                         tone="bad")
                return ""
            folder = QFileDialog.getExistingDirectory(
                self._dialog_parent(), f"Export preset {name!r} into",
                self.export_folder)
            if not folder:
                return ""
        out = self._call("Export preset", self.api.export_preset, name,
                         folder)
        if out is None:
            return ""
        self.export_folder = str(folder)
        self.say(str(out.get("summary") or ""))
        return str(out.get("path") or "")

    def import_preset(self, path: str = "") -> str:
        """Import an exported preset file — asked for with a file dialog
        when not given; the name it was saved under, or ""."""
        if not path:
            if _dialogs_disabled():
                self.say("Import: choose a file (dialogs are disabled).",
                         tone="bad")
                return ""
            path, _ = QFileDialog.getOpenFileName(
                self._dialog_parent(), "Import a camera preset",
                self.import_folder,
                f"Camera presets ({EXPORT_PATTERN});;JSON files (*.json)")
            if not path:
                return ""
        out = self._call("Import preset", self.api.import_preset, path)
        if out is None:
            # frame_camera told every listener (this view too) before it
            # returned: the list is filled and the answer said — `heard`
            # did it. A refusal leaves last_error.
            if self.last_error or self._last_heard.get("what") != "presets":
                return ""
            out = dict(self._last_heard)
        self.import_folder = os.path.dirname(str(path))
        name = str(out.get("name") or "")
        self.fill_presets(select=name)
        self.say(str(out.get("summary") or ""))
        return name

    def _dialog_parent(self) -> Optional[QWidget]:
        return self if isinstance(self, QWidget) else None


def _preset_tip(detail: Dict[str, Any]) -> str:
    lines = [str(detail.get("name") or "")]
    roi = detail.get("roi")
    lines.append("Area: " + (", ".join(str(v) for v in roi) if roi else
                             "left as it is"))
    lines.append(f"{len(detail.get('settings') or {})} settings")
    if detail.get("note"):
        lines.append(f"Note: {detail['note']}")
    if detail.get("updated"):
        lines.append(f"Saved {detail['updated']}")
    camera = detail.get("camera") or {}
    if not detail.get("own", True) and camera:
        lines.append(f"Saved on another {camera.get('model', 'camera')} "
                     f"({camera.get('serial', '')}) — applies here, but "
                     f"cannot be renamed or deleted from this one")
    return "\n".join(lines)


# ======================================================================
# The window
# ======================================================================
class CameraSettingsWindow(PresetsMixin, SettingsCore, QWidget):
    """Settings by group on the left; the camera's area, presets and what
    the last change did on the right; a status line and Reset / Camera
    defaults / Refresh / Close underneath."""

    def __init__(self, api: Any, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName(OBJECT_NAME)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._init_core(api)
        self._init_presets()
        self._build()
        self._listen()
        self.resize(1040, 720)
        self.setMinimumSize(760, 480)
        self.reload()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        head = QHBoxLayout()
        self.camera_label = QLabel(self)
        font = self.camera_label.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 1)
        self.camera_label.setFont(font)
        head.addWidget(self.camera_label)
        head.addStretch(1)
        self.state_label = QLabel(self)
        head.addWidget(self.state_label)
        outer.addLayout(head)

        split = QSplitter(Qt.Orientation.Horizontal, self)
        self.scroll = QScrollArea(split)
        self.scroll.setWidgetResizable(True)
        self.form = QWidget()
        self.form_layout = QVBoxLayout(self.form)
        self.form_layout.addStretch(1)
        self.scroll.setWidget(self.form)
        split.addWidget(self.scroll)

        side = QWidget(split)
        right = QVBoxLayout(side)
        right.setContentsMargins(0, 0, 0, 0)
        right.addWidget(self._build_area(side))
        right.addWidget(self._build_presets(side), 1)
        took = QGroupBox("What the last change did", side)
        took_layout = QVBoxLayout(took)
        self.took_box = QPlainTextEdit(took)
        self.took_box.setReadOnly(True)
        self.took_box.setMaximumHeight(110)
        self.took_box.setPlaceholderText(
            "Each setting a preset or a reset wrote, and what the camera "
            "made of it.")
        took_layout.addWidget(self.took_box)
        right.addWidget(took)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        # Stretch factors share out only what is left over; the wrapped
        # help on the right asked for so much that the settings got 70 px
        # (seen offscreen). Say the split outright.
        self.scroll.setMinimumWidth(420)
        side.setMinimumWidth(300)
        split.setSizes([600, 400])
        self.split = split
        outer.addWidget(split, 1)

        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.status)

        bar = QHBoxLayout()
        self.reset_all_button = QPushButton("Reset all (as connected)", self)
        self.reset_all_button.setToolTip(
            "Every setting back as the camera had it when it was connected "
            "— an EVK4 is opened with its sensor's default biases. The "
            "camera's area is left alone (Full sensor is for that).")
        self.reset_all_button.clicked.connect(self.reset_all)
        bar.addWidget(self.reset_all_button)
        self.defaults_button = QPushButton("Camera defaults", self)
        self.defaults_button.clicked.connect(self.load_defaults)
        self.defaults_button.setVisible(False)
        bar.addWidget(self.defaults_button)
        bar.addStretch(1)
        self.refresh_button = QPushButton("Refresh", self)
        self.refresh_button.setToolTip("Read every setting from the camera "
                                       "again")
        self.refresh_button.clicked.connect(self.reload)
        bar.addWidget(self.refresh_button)
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.close)
        bar.addWidget(self.close_button)
        outer.addLayout(bar)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)

    def _build_area(self, parent: QWidget) -> QGroupBox:
        box = QGroupBox("Camera's area (sensor px)", parent)
        layout = QGridLayout(box)
        self.area_help = QLabel(
            "The camera's OWN area: an EVK4 emits events only inside it, a "
            "Basler reads out only it — so frames, PNGs and the .raw hold "
            "only that part. Or draw a box on the live picture and press "
            "Apply area to camera in the main window.", box)
        self.area_help.setWordWrap(True)
        layout.addWidget(self.area_help, 0, 0, 1, 4)
        self.area_edit = QLineEdit(box)
        self.area_edit.setPlaceholderText("x, y, w, h")
        self.area_edit.setToolTip("x, y, width, height in SENSOR pixels — "
                                  "not the picture's. Snapped to what the "
                                  "sensor accepts.")
        self.area_edit.returnPressed.connect(self.apply_area)
        layout.addWidget(self.area_edit, 1, 0, 1, 2)
        self.area_apply = QPushButton("Apply", box)
        self.area_apply.clicked.connect(self.apply_area)
        layout.addWidget(self.area_apply, 1, 2)
        self.area_full = QPushButton("Full sensor", box)
        self.area_full.clicked.connect(self.full_sensor)
        layout.addWidget(self.area_full, 1, 3)
        self.sensor_label = QLabel(box)
        layout.addWidget(self.sensor_label, 2, 0, 1, 4)
        return box

    # ------------------------------------------------------------------
    # The hooks SettingsCore calls
    # ------------------------------------------------------------------
    def _show_camera(self, state: Dict[str, Any]) -> None:
        label = state.get("label") or ""
        title = f"Camera settings — {label}" if label else "Camera settings"
        self.setWindowTitle(title)
        self.camera_label.setText(
            f"{label} · {state.get('kind', '')}" if state.get("connected")
            else "No camera connected")

    def _cleared(self) -> None:
        self._clear_presets()
        self.area_edit.clear()
        self.sensor_label.clear()

    def _clear_form(self, message: str = "") -> None:
        self.rows = {}
        self._shape = None
        layout = self.form_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if message:
            note = QLabel(message, self.form)
            note.setWordWrap(True)
            layout.addWidget(note)
        layout.addStretch(1)

    def _build_form(self, settings: Sequence[Dict[str, Any]],
                    groups: Sequence[str]) -> None:
        self._clear_form()
        layout = self.form_layout
        layout.takeAt(layout.count() - 1)          # the stretch, re-added below
        add_group_boxes(layout, self.form, settings, groups, self._keep_row)
        if not settings:
            layout.addWidget(QLabel("This camera describes no settings.",
                                    self.form))
        elif self.rows:
            # One label column across every group, so the sliders line up.
            width = max(r.label.sizeHint().width() for r in self.rows.values())
            for row in self.rows.values():
                row.label.setMinimumWidth(width)
        layout.addStretch(1)

    def refresh_area(self) -> None:
        if not self.connected:
            return
        try:
            area = self.api.current_area()
        except Exception:                                 # noqa: BLE001
            return
        if not self.area_edit.hasFocus():
            self.area_edit.setText(str(area.get("area") or ""))
        whole = " — the whole sensor" if area.get("full") else ""
        self.sensor_label.setText(f"Sensor {area.get('sensor', '')}{whole}.")
        self.preset_with_area.setText(
            f"with the camera's area ({area.get('area', '')})")

    def _show_took(self, head: str, lines: List[str]) -> None:
        self.took_box.setPlainText("\n".join([head, ""] + lines)
                                   if lines else head)

    # ------------------------------------------------------------------
    # Enabled, and why not
    # ------------------------------------------------------------------
    def apply_state(self) -> None:
        on = self.connected and not self.busy
        settled = on and not self.capturing
        if not self.connected:
            state = "Connect a camera in the main window"
        elif self.busy:
            state = "Applying… the camera is restarting its stream"
        elif self.capturing:
            state = ("Capturing — live settings still apply; the area, "
                     "presets and settings that restart the live view wait "
                     "for Stop")
        else:
            state = "Live view — changes show at once"
        self.state_label.setText(state)
        self._rows_state()
        self._presets_state(on, settled)
        for widget, enabled in (
                (self.area_edit, settled), (self.area_apply, settled),
                (self.area_full, settled),
                (self.reset_all_button, settled and bool(self.as_connected)),
                (self.defaults_button, settled)):
            widget.setEnabled(enabled)
        self.defaults_button.setVisible(bool(self.factory))
        self.defaults_button.setToolTip(
            f"Load the camera's own factory settings ({self.factory}): every "
            f"setting and the area. The live view restarts for it."
            if self.factory else "")
        if self.capturing and self.connected:
            why = "Stop the capture first — one run keeps one camera set-up."
            for widget in (self.area_apply, self.area_full,
                           self.reset_all_button):
                widget.setToolTip(why)
        else:
            self.area_apply.setToolTip("Set the camera's own area to these "
                                       "sensor pixels")
            self.area_full.setToolTip("Give the whole sensor back")

    # ------------------------------------------------------------------
    # The window's own buttons
    # ------------------------------------------------------------------
    def reset_all(self) -> None:
        self.flush_now()
        out = self._call("Reset all", self.api.reset_camera_settings)
        if out is not None:
            self._took_set(out)

    def load_defaults(self) -> None:
        self.flush_now()
        out = self._call("Camera defaults", self.api.load_camera_defaults)
        if out is not None:
            self._took_other(out)

    def apply_area(self) -> None:
        out = self._call("Camera area", self.api.set_camera_area,
                         self.area_edit.text())
        if out is not None:
            self._took_other(out)

    def full_sensor(self) -> None:
        out = self._call("Full sensor", self.api.full_frame)
        if out is not None:
            self._took_other(out)

    def say(self, text: str, tone: str = "") -> None:
        _colour(self.status, _tone(self.status, tone) if tone else "")
        self.status.setText(text)

    # ------------------------------------------------------------------
    def closeEvent(self, event: Any) -> None:            # noqa: N802
        # A value still in the throttle is written, not lost.
        if self._pending:
            self.flush_now()
        super().closeEvent(event)


# ======================================================================
# One category in a window of its own — a pop-out
# ======================================================================
class CategoryWindow(SettingsCore, QWidget):
    """Every setting of ONE category (one settings group: "Biases",
    "Anti-flicker", "Exposure" ...) with the camera's own ranges and units,
    in a small non-modal window; several can be open at once, beside the
    main window, while the live view runs.

    The same rows, throttle and answers as the whole window (SettingsCore,
    SettingRow): a drag writes live, at most once per WRITE_MS, so the live
    view follows it; a setting the stream is in the way of is written when
    released and refused during a capture with frame_camera's message;
    a held or read-only row is greyed and says why. "Reset <category>" puts
    only this category back as connected. A preset bar picks or saves the
    camera's whole set-up (every setting + its area), as the main window's
    preset box does — a pop-out is where a set-up is tuned, so it is where
    it is saved."""

    def __init__(self, api: Any, group: str,
                 parent: Optional[QWidget] = None, font: Any = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.group = str(group)
        self.setObjectName(f"{CATEGORY_OBJECT}:{self.group}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        if font is not None:
            # The app's font, as a style sheet: a window whose parent has
            # a style sheet (Typhon's) has setFont undone when polished.
            self.setStyleSheet(
                f"QWidget {{ font-family: \"{font.family()}\"; "
                f"font-size: {max(7.0, font.pointSizeF() - 1):g}pt; }}")
        self._init_core(api, groups=(self.group,))
        self._preset_names: List[str] = []
        self._build()
        self._listen()
        self.resize(600, 460)
        self.setMinimumSize(440, 280)
        self.reload()

    @property
    def NO_CAMERA(self) -> str:                           # noqa: N802
        return (f"Connect a camera in the main window — this window shows "
                f"its {self.group} settings when it has them.")

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        head = QHBoxLayout()
        self.camera_label = QLabel(self)
        font = self.camera_label.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 1)
        self.camera_label.setFont(font)
        head.addWidget(self.camera_label)
        head.addStretch(1)
        self.state_label = QLabel(self)
        head.addWidget(self.state_label)
        outer.addLayout(head)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.form = QWidget()
        self.form_layout = QVBoxLayout(self.form)
        self.form_layout.addStretch(1)
        self.scroll.setWidget(self.form)
        outer.addWidget(self.scroll, 1)

        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.status)

        presets = QHBoxLayout()
        presets.addWidget(QLabel("Preset", self))
        self.preset_combo = QComboBox(self)
        self.preset_combo.setMinimumContentsLength(10)
        self.preset_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.preset_combo.setToolTip("This camera's presets (settings + "
                                     "area), kept in this project")
        presets.addWidget(self.preset_combo, 1)
        self.preset_apply = QPushButton("Apply", self)
        self.preset_apply.clicked.connect(self.apply_preset)
        presets.addWidget(self.preset_apply)
        self.preset_name = QLineEdit(self)
        self.preset_name.setPlaceholderText("Save as…")
        self.preset_name.setMaxLength(60)
        self.preset_name.returnPressed.connect(self.save_preset)
        presets.addWidget(self.preset_name, 1)
        self.preset_save = QPushButton("Save", self)
        self.preset_save.setToolTip(
            "Save the WHOLE camera as it is now — every setting and its "
            "area, not only this category — under this name.")
        self.preset_save.clicked.connect(self.save_preset)
        presets.addWidget(self.preset_save)
        outer.addLayout(presets)

        bar = QHBoxLayout()
        self.reset_group_button = QPushButton(
            f"Reset {self.group} (as connected)", self)
        self.reset_group_button.setToolTip(
            f"Every {self.group} setting back as the camera had it when it "
            f"was connected; the rest are left as they are.")
        self.reset_group_button.clicked.connect(self.reset_group)
        bar.addWidget(self.reset_group_button)
        bar.addStretch(1)
        self.refresh_button = QPushButton("Refresh", self)
        self.refresh_button.setToolTip(f"Read the {self.group} settings from "
                                       f"the camera again")
        self.refresh_button.clicked.connect(self.reload)
        bar.addWidget(self.refresh_button)
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.close)
        bar.addWidget(self.close_button)
        outer.addLayout(bar)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)

    # -- hooks -------------------------------------------------------------
    def _show_camera(self, state: Dict[str, Any]) -> None:
        label = state.get("label") or ""
        self.setWindowTitle(f"{self.group} — {label}" if label
                            else self.group)
        self.camera_label.setText(f"{self.group} · {label}"
                                  if state.get("connected")
                                  else f"{self.group} · no camera connected")

    def _cleared(self) -> None:
        self.preset_combo.clear()
        self._preset_names = []

    def _clear_form(self, message: str = "") -> None:
        CameraSettingsWindow._clear_form(self, message)

    def _build_form(self, settings: Sequence[Dict[str, Any]],
                    groups: Sequence[str]) -> None:
        self._clear_form()
        layout = self.form_layout
        layout.takeAt(layout.count() - 1)
        mine = [s for s in settings if s.get("group") == self.group]
        add_group_boxes(layout, self.form, mine, [self.group],
                        self._keep_row, show_range=True)
        if not mine:
            layout.addWidget(QLabel(f"This camera has no {self.group} "
                                    f"settings.", self.form))
        layout.addStretch(1)

    def fill_presets(self, select: str = "") -> None:
        try:
            names = list(self.api.list_presets().get("presets") or [])
        except Exception:                                 # noqa: BLE001
            names = []
        keep = select or self.preset_combo.currentText()
        self.preset_combo.blockSignals(True)
        try:
            self.preset_combo.clear()
            self.preset_combo.addItems([str(n) for n in names])
            index = self.preset_combo.findText(
                keep, Qt.MatchFlag.MatchFixedString) if keep else -1
            self.preset_combo.setCurrentIndex(index)
        finally:
            self.preset_combo.blockSignals(False)
        self._preset_names = names
        self.apply_state()

    def select_preset(self, name: str) -> bool:
        index = self.preset_combo.findText(name,
                                           Qt.MatchFlag.MatchFixedString)
        if index >= 0:
            self.preset_combo.setCurrentIndex(index)
        return index >= 0

    def apply_state(self) -> None:
        on = self.connected and not self.busy
        settled = on and not self.capturing
        if not self.connected:
            state = "No camera"
        elif self.busy:
            state = "Applying… the stream is restarting"
        elif self.capturing:
            state = "Capturing — live settings still apply"
        else:
            state = "Live — changes show at once"
        self.state_label.setText(state)
        self._rows_state()
        mine = {k: v for k, v in self.as_connected.items() if k in self.rows}
        self.preset_apply.setEnabled(settled and bool(self._preset_names))
        self.preset_apply.setToolTip(
            "Stop the capture first — one run keeps one camera set-up."
            if self.capturing and self.connected else
            "Put the camera back as this preset has it: its settings, then "
            "its area.")
        for widget in (self.preset_name, self.preset_save,
                       self.preset_combo):
            widget.setEnabled(on)
        self.reset_group_button.setEnabled(on and bool(mine))

    def say(self, text: str, tone: str = "") -> None:
        _colour(self.status, _tone(self.status, tone) if tone else "")
        self.status.setText(text)

    # -- the window's buttons -------------------------------------------
    def reset_group(self) -> None:
        self.flush_now()
        out = self._call(f"Reset {self.group}",
                         self.api.reset_camera_settings, self.group)
        if out is not None:
            self._took_set(out)

    def apply_preset(self) -> None:
        name = self.preset_combo.currentText().strip()
        if not name:
            self.say("Choose a preset first.", tone="bad")
            return
        self.flush_now()
        out = self._call(f"Preset {name!r}", self.api.apply_preset, name)
        if out is not None:
            self._took_set(out)

    def save_preset(self) -> None:
        name = self.preset_name.text().strip()
        out = self._call("Save preset", self.api.save_preset, name, True, "")
        if out is not None:
            self.preset_name.clear()
            self.fill_presets(select=str(out.get("name") or name))
            self.say(str(out.get("summary") or ""))

    def closeEvent(self, event: Any) -> None:            # noqa: N802
        if self._pending:
            self.flush_now()
        super().closeEvent(event)


# ======================================================================
# Opening them
# ======================================================================
#: The windows, held so they are not garbage-collected shut; reused, so a
#: second press brings one forward rather than opening another. "window" is
#: the whole window; "category:<group>" a pop-out.
_HELD: Dict[str, Any] = {}


class NoQtApplication(RuntimeError):
    """This process has no QApplication: a Tk build of the app, whose
    "Camera settings…" button reaches here too. A QWidget made without one
    aborts the whole process (measured: exit 127, nothing said, a running
    capture lost with it), so it is refused first."""


def _require_qt_app() -> None:
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        raise NoQtApplication(
            "the camera settings window is part of the Qt build of this app "
            "(run_example_gui.py typhon --target qt); presets still work "
            "from the preset box")


def alive(window: Any) -> bool:
    try:
        window.isVisible()
    except RuntimeError:                                  # already deleted
        return False
    return True


def _raise(window: QWidget, show: bool) -> None:
    if show:
        window.show()
        window.raise_()
        window.activateWindow()


def open_settings(parent: Optional[QWidget] = None, show: bool = True,
                  api: Any = None) -> CameraSettingsWindow:
    """Open the camera settings window (frame_camera.camera_settings' hook),
    or bring the open one forward, read again. Non-modal: the app — its live
    view and any capture — carries on underneath. `show` False builds it
    without showing it (COUNCIL_NO_DIALOGS). Raises NoQtApplication in a
    process with no QApplication (a Tk build)."""
    _require_qt_app()
    if api is None:
        import frame_camera as api                        # noqa: PLC0415
    window = _HELD.get("window")
    if window is not None and (not alive(window) or window.api is not api):
        window = None
    if window is None:
        window = CameraSettingsWindow(api, parent)
        _HELD["window"] = window
    else:
        window.reload()
    _raise(window, show)
    return window


def open_category(group: str, parent: Optional[QWidget] = None,
                  show: bool = True, api: Any = None,
                  font: Any = None) -> CategoryWindow:
    """Pop one category of the camera's settings out into a window of its
    own, or bring that category's open one forward, read again. One window
    per category; several categories open at once. `show` False builds it
    without showing it (COUNCIL_NO_DIALOGS). `font`: the app's."""
    _require_qt_app()
    if api is None:
        import frame_camera as api                        # noqa: PLC0415
    key = f"category:{group}"
    window = _HELD.get(key)
    if window is not None and (not alive(window) or window.api is not api):
        window = None
    if window is None:
        window = CategoryWindow(api, group, parent, font=font)
        _HELD[key] = window
    else:
        window.reload()
    _raise(window, show)
    return window


def open_categories() -> List[CategoryWindow]:
    """The category pop-outs that are open (built and not deleted)."""
    return [w for k, w in _HELD.items()
            if k.startswith("category:") and alive(w)]
