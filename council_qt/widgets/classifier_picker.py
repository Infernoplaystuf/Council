"""
council_qt.widgets.classifier_picker — Typhon's model dropdown: the saved
classifiers listed under the name box, the choices of its "Show" filter, and
the "classified with" line, kept true to the shared classifier store.

WHY ATTACH LOOKS AFTER THEM, NOT ONLY A SCRIPT LINK
The user chose a DROPDOWN of saved models. A combobox port holds ONE value:
a link can write the text in the box (the model's name — Save as, Rename,
Import do), never the list under it, and the list is the point. So, like the
preset picker (preset_picker) and the slider (capture_review),
frame_camera.attach hands these ports to this helper, while every action
stays an ordinary script link the user can edit in the Designer: picking or
typing a name opens it (open_classifier), Save as / Rename / Delete /
Export / Import / the tags call frame_classes with the box's text.

THE NAME IN THE BOX, THE WHOLE ROW IN THE LIST
Each item's text is the classifier's NAME — what the box shows once picked,
and what every frame_classes function is handed — and the open list draws
the whole row list_classifiers gives (version, classes, marks, where it came
from, tags, when it last changed), so "frames · Barbie Capture v5" and
"frames-lab · Typhon (project lab)" read apart before one is picked.

WHAT IS TYPED IS WHAT RETURN OPENS
The boxes have no completer. Qt's inline completion turned "nig", typed for
a new model, into "night", and Return opened "night" (MEASURED with the
window active, as a user's is — the preset box had the same fault). Return
on text Qt does not find itself (another case, a stray space, a new name)
is acted on once here: " night" opens "night", "nig" makes "nig".

THE LIST IS READ WHEN IT IS ABOUT TO BE SEEN, AND AFTER EVERY PRESS
The store is shared by every app on this PC (frame_classes, WHERE IT KEEPS
THINGS): another app saves, renames and deletes classifiers this window
never hears of. So the list is read again just before it opens (a press on
the box, F4, Alt+Down), before the keys and the wheel move through it (Up,
Down, Page Up/Down; MEASURED before: after a rename Down landed on the old
name and opened it as a new, empty model), when the filter changes, once at
start — and after every press of a classifier button: each frame_classes
link writes the status line, and a failed one clears it, so a change of
that line is the sign one ran (Save as, Rename, Delete, Import change the
store; MEASURED before: the list kept the old names until opened). Never on
a timer, so an idle window costs nothing; and a read costs little, since
frame_classes parses only the files that changed. Refilling never opens a
model (the link is on textActivated, which a refill does not emit) and
never takes away a name being typed.

THE "CLASSIFIED WITH" LINE
Says which model version last classified the frames in the capture folder,
and which runs in it that does not cover (frame_classes.classified_with —
from the run records in the store; the capture folder is only read). Read
again when the folder changes (a moment after the last keystroke, so typing
a path is not a store search per key), when the review lists the folder
again (a capture's last frame on disk: the new run shows as not classified
yet), and after every classifier press — so a Rename, a Delete, an Import or
a hand-edited Classify that never fills the line leaves it true, and a
failed press that blanked it (its handler clears what its link fills) puts
the folder's own record back.

DELETE ASKS FOR A SECOND PRESS
Every saved model is one pick away in the dropdown, so the handler that
calls delete_classifier asks first: the button reads "Sure?" and the status
line says what Delete does, and only a second press within CONFIRM_MS, on
the same model, runs it — as the settings window's preset Delete does. The
handler is found by what its code calls, and wrapped on the window (the
generated window looks a handler up at the moment of the click), so a
Delete wired under another name is found and one rewired elsewhere is not.

THE TWO FILE PICKERS SAY WHAT THEY ARE FOR
A picker is an entry and a Browse button, with no label of its own, and the
library has two of them one row apart — the folder Export writes into and
the file Import reads. A file_picker shape has no placeholder property, so
attach_to writes one into each (PICKER_HINTS), with a tooltip saying what
goes there; an app without those ports is left alone.

NOTHING HERE IMPORTS frame_classes
`api` is the module, passed in by frame_camera.attach — so this file never
imports a top-level module, and frame_classes never imports Qt.
"""
from __future__ import annotations

import inspect
import re
from typing import Any, Callable, List, Optional

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (QAbstractButton, QComboBox, QLineEdit,
                               QStyledItemDelegate)

#: Where each item keeps its whole row (the item's text is the name).
ROW_ROLE = int(Qt.ItemDataRole.UserRole) + 7

#: What the box says while it is empty.
PLACEHOLDER = "Pick a saved model, or type a new name"

#: The box's tooltip: what the list is and where it lives.
TIP = ("Saved classifiers — every app on this PC shares them, each tagged "
       "with the app it came from.\nPick one to open it, or type a new name "
       "(its first class makes it). Show narrows the list: This app, an app, "
       "a project, a tag.")

#: The filter box's tooltip.
FILTER_TIP = ("Which saved models the list shows: All classifiers, This app, "
              "App: <name>, Project: <name>, Tag: <tag>, Origin unknown — or "
              "type part of a name.")

#: How long the "classified with" line waits after the folder last changed.
LINE_DELAY_MS = 300

#: How long Delete waits for its second press — as long as the settings
#: window's preset Delete (camera_settings_window.CONFIRM_MS).
CONFIRM_MS = 4000

#: The Delete button while it waits for that press (it is 72 px wide).
ASKING = "Sure?"

#: The open list is made wide enough for its rows, up to this.
MAX_POPUP_WIDTH = 900

#: Typhon's export folder and import file pickers: port -> (the hint shown
#: in the empty entry, the tooltip).
PICKER_HINTS = {
    "export_to": (
        "Folder to export into",
        "Export writes the open model here as one file, "
        "<name>-v<N>.typhon-classifier.zip; Export this app's classifiers "
        "writes everything that belongs to this app here as one bundle."),
    "import_from": (
        "Classifier .zip to import",
        "A .typhon-classifier.zip (one model) or a .typhon-classifiers.zip "
        "(a bundle). Import checks all of it first and never overwrites a "
        "model: a taken name is asked about."),
}

#: Keys that open a combobox's list.
_OPENING_KEYS = (Qt.Key.Key_F4,)
_ALT_OPENING_KEYS = (Qt.Key.Key_Down, Qt.Key.Key_Up)
#: Keys that move a combobox to another item — and so open it.
_MOVING_KEYS = (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp,
                Qt.Key.Key_PageDown)
#: ... and these too, in a box that is not editable (an editable one hands
#: them to its text).
_MOVING_KEYS_READONLY = (Qt.Key.Key_Home, Qt.Key.Key_End)


class _RowDelegate(QStyledItemDelegate):
    """Draws an item of the open list as its whole row; the box itself still
    shows the item's text, the name."""

    def initStyleOption(self, option: Any, index: Any) -> None:
        super().initStyleOption(option, index)
        row = index.data(ROW_ROLE)
        if row:
            option.text = str(row)


class ClassifierPicker(QObject):
    """The model dropdown, its filter and the "classified with" line.

    `api` is frame_classes; `combo` the model-name port (a combobox), `show`
    the filter port (a combobox), `line` the "classified with" label port,
    `folder` the capture-folder port and `status` the classifier status
    line every frame_classes link writes. Each may be None, and each is
    looked after only if present.
    """

    def __init__(self, api: Any, combo: Any = None, show: Any = None,
                 line: Any = None, folder: Any = None,
                 parent: Optional[QObject] = None, status: Any = None):
        super().__init__(parent)
        self.api = api
        self.combo_port = combo
        self.show_port = show
        self.line_port = line
        self.folder_port = folder
        self.status_port = status
        self.combo = _combobox(combo)
        self.filter_box = _combobox(show)
        #: Counts, for the tests and the measurements.
        self.fills = 0
        self.lines = 0
        #: What the last read of the store said when it failed, else "".
        self.problem = ""
        #: The Delete that asks for a second press, once guard_deletes ran.
        self.delete_guard: Optional[_DeleteGuard] = None
        self._line_soon = QTimer(self)
        self._line_soon.setSingleShot(True)
        self._line_soon.setInterval(LINE_DELAY_MS)
        self._line_soon.timeout.connect(self.show_line)
        # After a press: once its handler has written every port it fills.
        self._pressed = QTimer(self)
        self._pressed.setSingleShot(True)
        self._pressed.setInterval(0)
        self._pressed.timeout.connect(self.read_again)
        if self.combo is not None:
            self._dress(self.combo, TIP, PLACEHOLDER)
            self.combo.setItemDelegate(_RowDelegate(self.combo))
            self.combo.installEventFilter(self)
            _activate_on_return(self.combo)
        if self.filter_box is not None:
            self._dress(self.filter_box, FILTER_TIP, "")
            self.filter_box.installEventFilter(self)
            _activate_on_return(self.filter_box)
            # A filter picked, or typed and Return: the list follows it.
            self.filter_box.textActivated.connect(lambda *_: self.refresh())
        if line is not None:
            _hook(folder, lambda *_: self._line_soon.start())
            # A handler that failed clears the line it fills; the folder's
            # own record goes back (a fresh window's status was already
            # empty, so its clearing is no change to hear).
            _hook(line, lambda value=None, *_: (
                self._pressed.start() if not value and self._folder() else
                None))
        _hook(status, lambda *_: self._pressed.start())
        self.refresh()
        self.show_line()

    # -- set-up ---------------------------------------------------------
    @staticmethod
    def _dress(combo: QComboBox, tip: str, placeholder: str) -> None:
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if combo.isEditable():
            combo.setCompleter(None)    # see WHAT IS TYPED IS WHAT RETURN OPENS
        combo.setToolTip(tip)
        if placeholder:
            if combo.isEditable() and combo.lineEdit() is not None:
                combo.lineEdit().setPlaceholderText(placeholder)
            else:
                combo.setPlaceholderText(placeholder)
        combo.setMaxVisibleItems(20)

    # -- when to read the store -------------------------------------------
    def eventFilter(self, watched: Any, event: Any) -> bool:
        """Read the store just BEFORE a list opens, or before the keys or
        the wheel move through it: the event reaches this filter first, so
        the list it acts on is the one just read. Nothing is ever consumed
        here."""
        if watched is self.combo or watched is self.filter_box:
            kind = event.type()
            if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel) or (
                    kind == QEvent.Type.KeyPress
                    and (_opens(event) or _moves(event, watched))):
                self.refresh()
        return False

    def read_again(self) -> None:
        """The list and the "classified with" line, after a press."""
        self.refresh()
        self.show_line()

    def line_soon(self, *_args: Any) -> None:
        """Read the "classified with" line again in a moment — for whatever
        else changes the folder's frames (the review listing it again)."""
        if self.line_port is not None:
            self._line_soon.start()

    def refresh(self) -> None:
        """The store again: the models the filter shows, and the filters
        that exist."""
        show = (self.filter_box.currentText()
                if self.filter_box is not None else "")
        try:
            listed = self.api.list_classifiers(show)
            self.problem = ""
        except Exception as exc:                          # noqa: BLE001
            # A store that cannot be read (a classifier_store.json naming
            # nothing, say) leaves the list empty and says why on hover;
            # the links say it in the status line when pressed.
            listed = {}
            self.problem = str(exc)
        self.fill(list(listed.get("names") or []),
                  list(listed.get("rows") or []))
        self.fill_filters(list(listed.get("filters") or []))
        if self.combo is not None:
            self.combo.setToolTip(TIP + (f"\n\nThe saved models cannot be "
                                         f"read: {self.problem}"
                                         if self.problem else ""))

    # -- the model box ----------------------------------------------------
    def fill(self, names: List[str], rows: List[str]) -> None:
        """Put the models in the box, each item its name and its whole row.
        The model shown stays when it is still listed; a name that is not
        listed — being typed, just made by Save as, or filtered out of the
        list — stays in the box exactly as it is, cursor and all."""
        combo = self.combo
        if combo is None:
            return
        self.fills += 1
        text = combo.currentText()
        line = combo.lineEdit()
        cursor = line.cursorPosition() if line is not None else 0
        blocker = QSignalBlocker(combo)
        try:
            combo.clear()
            for i, name in enumerate(names):
                row = rows[i] if i < len(rows) else name
                combo.addItem(str(name))
                combo.setItemData(i, str(row), ROW_ROLE)
                combo.setItemData(i, str(row), Qt.ItemDataRole.ToolTipRole)
            index = combo.findText(text, Qt.MatchFlag.MatchExactly) \
                if text else -1
            combo.setCurrentIndex(index)
            if index < 0 and combo.isEditable():
                combo.setEditText(text)
                if line is not None:
                    line.setCursorPosition(min(cursor, len(text)))
        finally:
            del blocker
        self._widen(combo, rows)

    @staticmethod
    def _widen(combo: QComboBox, rows: List[str]) -> None:
        """The open list as wide as its rows (up to MAX_POPUP_WIDTH), not
        the box's 232 px — a row cut there is a row nobody can read."""
        if not rows:
            return
        metrics = combo.fontMetrics()
        widest = max(metrics.horizontalAdvance(str(r)) for r in rows) + 40
        combo.view().setMinimumWidth(min(MAX_POPUP_WIDTH,
                                         max(combo.width(), widest)))

    def row_of(self, name: str) -> str:
        """The whole row the list shows for model `name` ("" if it has none)."""
        combo = self.combo
        if combo is None:
            return ""
        index = combo.findText(name, Qt.MatchFlag.MatchExactly)
        return str(combo.itemData(index, ROW_ROLE) or "") if index >= 0 else ""

    def items(self) -> List[str]:
        combo = self.combo
        return ([combo.itemText(i) for i in range(combo.count())]
                if combo is not None else [])

    # -- the filter box ---------------------------------------------------
    def fill_filters(self, filters: List[str]) -> None:
        """The filters that exist in the store; whatever the box says now
        (picked or typed) stays."""
        box = self.filter_box
        if box is None:
            return
        text = box.currentText()
        blocker = QSignalBlocker(box)
        try:
            box.clear()
            box.addItems([str(f) for f in filters])
            index = box.findText(text, Qt.MatchFlag.MatchExactly) \
                if text else -1
            box.setCurrentIndex(index)
            if index < 0 and box.isEditable():
                box.setEditText(text)
        finally:
            del blocker

    # -- the "classified with" line -----------------------------------------
    def _folder(self) -> str:
        try:
            return str(self.folder_port.get() or "").strip() \
                if self.folder_port is not None else ""
        except Exception:                                 # noqa: BLE001
            return ""

    def show_line(self) -> None:
        """Which model version last classified the capture folder's frames,
        and which runs in it that does not cover."""
        port = self.line_port
        if port is None:
            return
        self._line_soon.stop()
        self.lines += 1
        try:
            text = str(self.api.classified_with(self._folder()).get(
                "classified_with") or "")
        except Exception as exc:                          # noqa: BLE001
            text = f"Which model classified this folder cannot be read: {exc}"
        try:
            port.set(text)
        except Exception:                                 # noqa: BLE001
            pass

    # -- the status line ----------------------------------------------------
    def say(self, text: str) -> None:
        """Write the status line (where every classifier press answers)."""
        try:
            if self.status_port is not None:
                self.status_port.set(text)
        except Exception:                                 # noqa: BLE001
            pass


class _DeleteGuard(QObject):
    """A Delete handler that asks first (see DELETE ASKS FOR A SECOND
    PRESS). `run` is the handler as generated (or as the user edited it)."""

    def __init__(self, picker: ClassifierPicker, run: Callable[..., Any],
                 button: Optional[QAbstractButton],
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.picker = picker
        self.run = run
        self.button = button
        self.label = button.text() if button is not None else ""
        #: The model the next press deletes ("" when none is asked about).
        self.asked = ""
        self._wait = QTimer(self)
        self._wait.setSingleShot(True)
        self._wait.timeout.connect(self.cancel)

    def press(self, *args: Any) -> Any:
        combo = self.picker.combo
        name = combo.currentText().strip() if combo is not None else ""
        if not name or (self.asked == name.casefold()
                        and self._wait.isActive()):
            # Nothing to delete (the link asks for a pick itself), or the
            # second press on the model just asked about.
            self.cancel()
            return self.run(*args)
        self.asked = name.casefold()
        self._wait.start(CONFIRM_MS)
        if self.button is not None:
            self.button.setText(ASKING)
        self.picker.say(f"Delete '{name}'? Press Delete again within "
                        f"{max(1, CONFIRM_MS // 1000)} s to move it aside "
                        f"(to .deleted in the store: moving it back "
                        f"restores it).")
        return None

    def cancel(self) -> None:
        self.asked = ""
        self._wait.stop()
        if self.button is not None:
            self.button.setText(self.label)


def guard_deletes(app: Any, picker: ClassifierPicker,
                  function: str = "delete_classifier") -> List[str]:
    """Make each handler of `app` that calls `function` ask for a second
    press (see DELETE ASKS FOR A SECOND PRESS); the handlers it wrapped.
    The button is the one whose objectName the handler is named after
    (on_<name>), and its text says that it is asking."""
    wrapped = []
    for handler in _handlers_calling(app, function):
        run = getattr(app, handler, None)
        if not callable(run):
            continue
        button = None
        if hasattr(app, "findChild"):
            button = app.findChild(QAbstractButton, handler[len("on_"):])
        guard = _DeleteGuard(picker, run, button, parent=picker)
        setattr(app, handler, guard.press)
        picker.delete_guard = guard
        wrapped.append(handler)
    return wrapped


def _handlers_calling(app: Any, function: str) -> List[str]:
    """The names of `app`'s handlers (on_*) whose code calls `function` —
    read from their source, handlers.py, which the user may have edited."""
    pattern = re.compile(rf"\b{re.escape(function)}\s*\(")
    found = []
    kind = type(app)
    for name in sorted(n for n in dir(kind) if n.startswith("on_")):
        method = getattr(kind, name, None)
        if not callable(method):
            continue
        try:
            source = inspect.getsource(method)
        except (OSError, TypeError):
            continue
        if pattern.search(source):
            found.append(name)
    return found


def _combobox(port: Any) -> Optional[QComboBox]:
    widget = getattr(port, "widget", None)
    return widget if isinstance(widget, QComboBox) else None


def _hook(port: Any, fn: Callable[..., Any]) -> None:
    """port.on_change(fn) when the port has one; a port without is left."""
    hook = getattr(port, "on_change", None)
    if callable(hook):
        try:
            hook(fn)
        except Exception:                                 # noqa: BLE001
            pass


def _qt_finds(combo: QComboBox) -> Qt.MatchFlag:
    """How Qt's own Return looks the typed text up in the list
    (QComboBoxPrivate::matchFlags): a fixed string, case-sensitive unless a
    completer that ignores case is set — with no completer, case-sensitive."""
    flags = Qt.MatchFlag.MatchFixedString
    completer = combo.completer()
    if completer is None or (completer.caseSensitivity()
                             == Qt.CaseSensitivity.CaseSensitive):
        flags |= Qt.MatchFlag.MatchCaseSensitive
    return flags


def _activate_on_return(combo: QComboBox) -> None:
    """Return on text Qt does not activate itself "activates" it, as
    picking an item does — so the box's link runs (open_classifier for a
    new model's name, list_classifiers for a typed filter) — once.

    MEASURED (PySide6 6.10, offscreen, real key presses): an editable
    QComboBox with NoInsert — which these are, so a typed name is never
    added to the list as if it were a saved model — activates on Return
    only text it finds in the list by its own flags (_qt_finds), and only
    when that is not the item it already shows (QComboBox's editingFinished,
    which comes AFTER returnPressed). A new model's name typed and Return
    did nothing, and neither did part of a name typed into Show, a listed
    name with a stray space (" night": Qt looked for it with the space), or
    the open model's own name typed again. Text Qt activates is left to Qt
    — said once; other text is acted on stripped, as the listed name it is
    when there is one, whatever its case ("NIGHT" opens "night")."""
    line = combo.lineEdit() if combo.isEditable() else None
    if line is None:
        return

    def typed() -> None:
        text = combo.currentText()
        if not text.strip():
            return
        if (combo.findText(text, _qt_finds(combo)) >= 0
                and combo.itemText(combo.currentIndex()) != text):
            return                      # Qt activates it, a moment later
        name = text.strip()
        index = combo.findText(name, Qt.MatchFlag.MatchFixedString)
        if index >= 0:
            name = combo.itemText(index)
            combo.setCurrentIndex(index)
        combo.textActivated.emit(name)

    line.returnPressed.connect(typed)


def _opens(event: Any) -> bool:
    """Whether a key press opens a combobox's list (F4, Alt+Down/Up)."""
    key = event.key()
    if key in _OPENING_KEYS:
        return True
    return (key in _ALT_OPENING_KEYS and bool(
        event.modifiers() & Qt.KeyboardModifier.AltModifier))


def _moves(event: Any, combo: Any) -> bool:
    """Whether a key press moves the box to another item (and opens it)."""
    key = event.key()
    if event.modifiers() & Qt.KeyboardModifier.AltModifier:
        return False
    return key in _MOVING_KEYS or (
        key in _MOVING_KEYS_READONLY and not combo.isEditable())


def attach_to(api: Any, ports: Any, combo: str, show: str, line: str,
              folder: str, parent: Optional[QObject] = None,
              status: str = "") -> Optional["ClassifierPicker"]:
    """A ClassifierPicker for an app whose model-name port is a DROPDOWN, or
    None (an app with a name box, such as the Barbie apps, keeps its own).
    `parent` is the app: its Delete handler is made to ask first
    (guard_deletes)."""
    model = getattr(ports, combo, None) if combo else None
    if _combobox(model) is None:
        return None
    pick: Callable[[str], Any] = (lambda name: getattr(ports, name, None)
                                  if name else None)
    label_pickers(ports)
    picker = ClassifierPicker(api, combo=model, show=pick(show),
                              line=pick(line), folder=pick(folder),
                              status=pick(status), parent=parent)
    if parent is not None:
        guard_deletes(parent, picker)
    return picker


def label_pickers(ports: Any, hints: Optional[dict] = None) -> List[str]:
    """Write each picker's hint into its empty entry, and its tooltip (see
    THE TWO FILE PICKERS SAY WHAT THEY ARE FOR); the ports it found."""
    done = []
    for name, (hint, tip) in (hints or PICKER_HINTS).items():
        widget = getattr(getattr(ports, name, None), "widget", None)
        entry = getattr(widget, "entry", None)
        if not isinstance(entry, QLineEdit):
            continue
        entry.setPlaceholderText(hint)
        widget.setToolTip(tip)
        done.append(name)
    return done
