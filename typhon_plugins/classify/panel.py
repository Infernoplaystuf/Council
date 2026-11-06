"""
typhon.classify's panel: the Classification pop-out's inner widget — every
classifier control the main window had, in the same order, plus a Dataset
row (docs/typhon_plugins_design.md 3.7, 6.3; Andy, 2 November 2026).

TODAY'S BEHAVIOUR, IN ANOTHER WINDOW
Each control is the widget its wireframe shape was (examples/gui/
typhon.gspec, shapes s24-s40 and s64-s78), with the same port name, and each
press runs the LINK its shape had: the same frame_classes function with the
same inputs, its result's keys written into the same ports, and on a failure
the ports it fills cleared and the reason said — what a generated handler
does (gui_emit_qt). A test holds LINKS to the classic wireframe's links.
The model dropdown, its filter, the "classified with" line and Delete's
second press are today's ClassifierPicker, handed these ports as the main
window's were.

WHAT THE MAIN WINDOW STILL GIVES
The image folder and the frame on screen ("Mark this frame", "Predict this
frame", "Classify all frames") are read from the main window when pressed,
through the plugin's context (image_folder, current_frame).

DATASETS
The Dataset row lists the vault's datasets. "Mark dataset" marks every frame
of the picked dataset still on disk as the class picked in Classes (one
save: frame_classes.mark_dataset); "Classify dataset" lists each one's likely
class in Predictions (frame_classes.classify_dataset). A dataset sent from the
Datasets window arrives picked here (use_dataset).

Qt widgets only (QtCore, QtGui, QtWidgets: design 3.11's rule for panels);
the classifier library and the picker come through typhon_plugin_api.builtin.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

import typhon_plugin_api as api
from typhon_plugin_api import builtin

#: Each press: (button key, what it is called, frame_classes function, the
#: ports read in order, {port written: result key}). The classic
#: typhon.gspec's classifier links, unchanged (a test compares them).
LINKS: Tuple[Tuple[str, str, str, Tuple[str, ...], Dict[str, str]], ...] = (
    ("model", "Model", "open_classifier", ("classifier_name",),
     {"classes": "classes", "classifier_name": "name",
      "classifier_status": "summary"}),
    ("open", "Open", "open_classifier", ("classifier_name",),
     {"classes": "classes", "classifier_name": "name",
      "classifier_status": "summary"}),
    ("add_class", "Add class", "add_class", ("classifier_name", "new_class"),
     {"classes": "classes", "classifier_status": "summary",
      "new_class": "cleared"}),
    ("mark", "Mark this frame", "mark_frame",
     ("classifier_name", "capture_folder", "current_frame", "classes"),
     {"classifier_status": "summary"}),
    ("remove_class", "Remove class", "remove_class",
     ("classifier_name", "classes"),
     {"classes": "classes", "classifier_status": "summary"}),
    ("train", "Train", "train", ("classifier_name",),
     {"classifier_status": "summary"}),
    ("predict", "Predict this frame", "predict_frame",
     ("classifier_name", "capture_folder", "current_frame"),
     {"classifier_status": "summary"}),
    ("classify_all", "Classify all frames", "classify_folder",
     ("classifier_name", "capture_folder"),
     {"classified_with_line": "classified_with",
      "classifier_status": "summary", "predictions": "rows"}),
    ("filter", "Filter", "list_classifiers", ("classifier_filter",),
     {"classifier_status": "summary"}),
    ("save_as", "Save as", "save_as",
     ("classifier_name", "new_name", "classifier_filter"),
     {"classes": "classes", "classifier_name": "name",
      "classifier_status": "summary", "new_name": "cleared"}),
    ("rename", "Rename", "rename_classifier",
     ("classifier_name", "new_name", "classifier_name", "classifier_filter"),
     {"classifier_name": "name", "classifier_status": "summary",
      "new_name": "cleared"}),
    ("delete", "Delete", "delete_classifier",
     ("classifier_name", "classifier_name", "classifier_filter"),
     {"classes": "classes", "classifier_name": "name",
      "classifier_status": "summary"}),
    ("export", "Export", "export_classifier",
     ("classifier_name", "export_to", "classifier_name"),
     {"classifier_status": "summary"}),
    ("export_app", "Export this app's classifiers", "export_this_app",
     ("export_to",), {"classifier_status": "summary"}),
    ("import", "Import", "import_classifier",
     ("import_from", "new_name", "classifier_filter", "classifier_name"),
     {"classes": "classes", "classifier_name": "name",
      "classifier_status": "summary", "new_name": "cleared"}),
    ("add_tag", "Add tag", "add_tag",
     ("classifier_name", "tag", "classifier_filter", "classifier_name"),
     {"classifier_status": "summary", "tag": "cleared"}),
    ("remove_tag", "Remove tag", "remove_tag",
     ("classifier_name", "tag", "classifier_filter", "classifier_name"),
     {"classifier_status": "summary"}),
)

#: The dataset presses, which the classic wireframe did not have.
DATASET_LINKS: Tuple[Tuple[str, str, str, Tuple[str, ...], Dict[str, str]],
                     ...] = (
    ("mark_dataset", "Mark dataset", "mark_dataset",
     ("classifier_name", "dataset_frames", "classes"),
     {"classifier_status": "summary"}),
    ("classify_dataset", "Classify dataset", "classify_dataset",
     ("classifier_name", "dataset_frames"),
     {"classifier_status": "summary", "predictions": "rows"}),
)

#: What the dataset box says while no dataset is picked.
NO_DATASET = "Pick a dataset"
#: The filter box's first choice, as the wireframe's default.
ALL = "All classifiers"


def _s(value: Any) -> str:
    return "" if value is None else str(value)


# ======================================================================
# Ports: what a generated window's ports are, over this panel's widgets
# ======================================================================
class Port:
    """get / set / on_change / clear over one widget, as the generated
    _WidgetPort and _ListPort behave (gui_emit_qt): a label's set fires its
    change hook, a list's value is its selection and its set fills it."""

    def __init__(self, name: str, widget: Any, kind: str) -> None:
        self.name = name
        self.widget = widget
        self.kind = kind
        self._subs: List[Callable[..., Any]] = []
        self._last: Any = self._raw()
        if kind == "entry":
            widget.textChanged.connect(lambda *_: self._fire())
        elif kind == "combobox":
            widget.currentTextChanged.connect(lambda *_: self._fire())
        elif kind == "file_picker":
            widget.entry.textChanged.connect(lambda *_: self._fire())
        elif kind == "listbox":
            widget.itemSelectionChanged.connect(lambda: self._fire())

    def _raw(self) -> Any:
        w = self.widget
        if self.kind in ("label", "entry"):
            return w.text()
        if self.kind == "combobox":
            return w.currentText()
        if self.kind == "file_picker":
            return w.entry.text()
        if self.kind == "listbox":
            return [i.text() for i in w.selectedItems()]
        return None

    def get(self) -> Any:
        return self._raw()

    def items(self) -> List[str]:
        w = self.widget
        return [w.item(i).text() for i in range(w.count())]

    def set(self, value: Any) -> None:
        w = self.widget
        if self.kind in ("label", "entry"):
            w.setText(_s(value))
        elif self.kind == "file_picker":
            w.entry.setText(_s(value))
        elif self.kind == "combobox":
            text = _s(value)
            i = w.findText(text)
            if i >= 0:
                w.setCurrentIndex(i)
            elif w.isEditable():
                w.setEditText(text)
        elif self.kind == "listbox":
            w.clear()
            for v in (value or []):
                w.addItem(_s(v))
        if self.kind == "label":
            self._fire()

    def clear(self) -> None:
        self.set([] if self.kind == "listbox" else "")

    def on_change(self, fn: Callable[..., Any]) -> None:
        self._subs.append(fn)

    def _fire(self) -> None:
        value = self._raw()
        if value == self._last:
            return
        self._last = value
        for fn in list(self._subs):
            try:
                fn(value)
            except Exception:                             # noqa: BLE001
                pass


class _Live:
    """A port read from the main window when it is read (the image folder,
    the frame on screen); its change hook is told when the main window
    lists its folder again."""

    def __init__(self, name: str, read: Callable[[], Any],
                 text: bool = True) -> None:
        self.name = name
        self.widget = None
        self._read = read
        self._text = text
        self._subs: List[Callable[..., Any]] = []

    def get(self) -> Any:
        """The value now: text for the folder and the frame ("" when it
        cannot be read); a list's own value, and its error, for the
        dataset's frames (the press says why)."""
        if not self._text:
            return self._read()
        try:
            return _s(self._read())
        except Exception:                                 # noqa: BLE001
            return ""

    def set(self, value: Any) -> None:                    # read-only here
        pass

    def clear(self) -> None:
        pass

    def on_change(self, fn: Callable[..., Any]) -> None:
        self._subs.append(fn)

    def tell(self) -> None:
        for fn in list(self._subs):
            try:
                fn(self.get())
            except Exception:                             # noqa: BLE001
                pass


class Ports:
    """The panel's ports by name (attribute and item access), as a
    generated window's `self.ports`."""

    def __init__(self, ports: Sequence[Any]) -> None:
        self._by_name = {p.name: p for p in ports}
        for p in ports:
            setattr(self, p.name, p)

    def __getitem__(self, name: str) -> Any:
        return self._by_name[name]

    def __contains__(self, name: str) -> bool:
        return name in self._by_name


class FilePicker(QWidget):
    """An entry and a Browse button: a folder (Export) or a file (Import),
    as the wireframe's file_picker shape — its `entry` is what the picker
    writes its hint into."""

    changed = Signal(str)

    def __init__(self, parent: Any = None, *, folder: bool = True,
                 pattern: str = "") -> None:
        super().__init__(parent)
        self.folder = folder
        self.pattern = pattern
        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.entry = QLineEdit(self)
        self.button = QPushButton("Browse…", self)
        self.button.clicked.connect(self.browse)
        box.addWidget(self.entry, 1)
        box.addWidget(self.button)
        self.entry.textChanged.connect(self.changed.emit)

    def browse(self) -> None:
        if os.environ.get("COUNCIL_NO_DIALOGS"):
            return
        start = self.entry.text().strip()
        if self.folder:
            got = QFileDialog.getExistingDirectory(self, "Choose a folder",
                                                   start)
        else:
            got, _ = QFileDialog.getOpenFileName(self, "Choose a file", start,
                                                 self.pattern)
        if got:
            self.entry.setText(got)


# ======================================================================
# The panel
# ======================================================================
class ClassifierPanel(QWidget):
    """Today's classifier controls, a Dataset row, and the presses' links."""

    def __init__(self, plugin: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.plugin = plugin
        self.fc = builtin.classifier_library()
        #: Each button, by its LINKS key.
        self.buttons: Dict[str, QPushButton] = {}
        #: What the last failed press said ("" after one that worked).
        self.failed = ""
        self._stop_listening: List[Callable[[], Any]] = []
        self._build()
        self._wire()
        self.picker = self._attach_picker()
        self.fill_datasets()
        ctx = plugin.ctx
        if ctx is not None:
            try:
                self._stop_listening.append(ctx.on(
                    "frames_reloaded", lambda _data: self._reloaded()))
            except Exception:                             # noqa: BLE001
                pass

    # -- building ------------------------------------------------------------
    def _button(self, key: str, text: str) -> QPushButton:
        b = QPushButton(text, self)
        b.setObjectName(f"btn_{key}")
        self.buttons[key] = b
        return b

    @staticmethod
    def _heading(text: str, parent: Any) -> QLabel:
        lbl = QLabel(text, parent)
        font = lbl.font()
        font.setBold(True)
        lbl.setFont(font)
        return lbl

    #: The most the panel asks for: the smallest window Typhon is drawn for
    #: (1400 x 820). Taller content scrolls instead of growing the window.
    MOST = QSize(1400, 800)

    def sizeHint(self) -> QSize:                          # noqa: N802
        want = self.body.sizeHint() + QSize(4, 4)
        return QSize(min(want.width(), self.MOST.width()),
                     min(want.height(), self.MOST.height()))

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setObjectName("classifier_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        self.body = QWidget(scroll)
        scroll.setWidget(self.body)
        grid = QGridLayout(self.body)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        for c in range(4):
            grid.setColumnStretch(c, 1)
        row = 0

        def add(widget: Any, r: int, c: int, span: int = 1) -> None:
            grid.addWidget(widget, r, c, 1, span)

        add(self._heading("Model — pick one, or type a new name", self),
            row, 0, 4)
        row += 1
        self.model_box = QComboBox(self)
        self.model_box.setEditable(True)
        add(self.model_box, row, 0, 3)
        add(self._button("open", "Open"), row, 3)
        row += 1
        add(QLabel("Show", self), row, 0)
        self.filter_box = QComboBox(self)
        self.filter_box.setEditable(True)
        add(self.filter_box, row, 1, 3)
        row += 1
        self.new_name = QLineEdit(self)
        self.new_name.setPlaceholderText("New name")
        add(self.new_name, row, 0)
        add(self._button("save_as", "Save as"), row, 1)
        add(self._button("rename", "Rename"), row, 2)
        add(self._button("delete", "Delete"), row, 3)
        row += 1
        self.export_to = FilePicker(self, folder=True)
        add(self.export_to, row, 0, 3)
        add(self._button("export", "Export"), row, 3)
        row += 1
        add(self._button("export_app", "Export this app's classifiers"),
            row, 0, 4)
        row += 1
        self.import_from = FilePicker(self, folder=False,
                                      pattern="Classifiers (*.zip)")
        add(self.import_from, row, 0, 3)
        add(self._button("import", "Import"), row, 3)
        row += 1
        self.tag = QLineEdit(self)
        self.tag.setPlaceholderText("Tag")
        add(self.tag, row, 0, 2)
        add(self._button("add_tag", "Add tag"), row, 2)
        add(self._button("remove_tag", "Remove tag"), row, 3)
        row += 1
        self.classified_line = QLabel("", self)
        self.classified_line.setWordWrap(True)
        add(self.classified_line, row, 0, 4)
        row += 1
        add(QLabel("New class", self), row, 0)
        self.new_class = QLineEdit(self)
        add(self.new_class, row, 1, 2)
        add(self._button("add_class", "Add class"), row, 3)
        row += 1
        add(self._heading("Classes (pick one, then mark)", self), row, 0, 4)
        row += 1
        self.classes = QListWidget(self)
        self.classes.setMinimumHeight(88)
        add(self.classes, row, 0, 4)
        row += 1
        add(self._button("mark", "Mark this frame"), row, 0, 3)
        add(self._button("remove_class", "Remove class"), row, 3)
        row += 1
        add(self._button("train", "Train"), row, 0, 2)
        add(self._button("predict", "Predict this frame"), row, 2, 2)
        row += 1
        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.setMinimumHeight(64)
        self.status.setAlignment(Qt.AlignmentFlag.AlignTop
                                 | Qt.AlignmentFlag.AlignLeft)
        self.status.setSizePolicy(QSizePolicy.Policy.Preferred,
                                  QSizePolicy.Policy.Minimum)
        add(self.status, row, 0, 4)
        row += 1
        add(self._button("classify_all", "Classify all frames"), row, 0, 4)
        row += 1
        add(self._heading("Dataset", self), row, 0, 4)
        row += 1
        self.dataset_box = QComboBox(self)
        self.dataset_box.setToolTip(
            "The vault's datasets — frames picked with Add to dataset "
            "beside PNG / Raw. Mark dataset marks every frame of it as the "
            "class picked in Classes; Classify dataset lists each one's "
            "likely class in Predictions.")
        add(self.dataset_box, row, 0, 2)
        add(self._button("mark_dataset", "Mark dataset"), row, 2)
        add(self._button("classify_dataset", "Classify dataset"), row, 3)
        row += 1
        add(self._heading("Predictions", self), row, 0, 4)
        row += 1
        self.predictions = QListWidget(self)
        self.predictions.setMinimumHeight(112)
        add(self.predictions, row, 0, 4)
        grid.setRowStretch(row, 1)

        self._image_folder = _Live("capture_folder",
                                   self.plugin.capture_folder)
        self._current = _Live("current_frame", self.plugin.current_frame)
        self.ports = Ports([
            Port("classifier_name", self.model_box, "combobox"),
            Port("classifier_filter", self.filter_box, "combobox"),
            Port("new_name", self.new_name, "entry"),
            Port("export_to", self.export_to, "file_picker"),
            Port("import_from", self.import_from, "file_picker"),
            Port("tag", self.tag, "entry"),
            Port("classified_with_line", self.classified_line, "label"),
            Port("new_class", self.new_class, "entry"),
            Port("classes", self.classes, "listbox"),
            Port("classifier_status", self.status, "label"),
            Port("predictions", self.predictions, "listbox"),
            self._image_folder, self._current,
            _Live("dataset_frames", self.dataset_frames, text=False),
        ])
        self.ports.classifier_filter.set(ALL)

    # -- the links -----------------------------------------------------------
    def _wire(self) -> None:
        links = {key: link for key, *link in LINKS + DATASET_LINKS}
        for key, button in self.buttons.items():
            what, function, inputs, outputs = links[key]
            button.clicked.connect(self._press_of(what, function, inputs,
                                                  outputs))
        model = links["model"]
        self.model_box.textActivated.connect(
            lambda *_: self.press(*model))
        flt = links["filter"]
        self.filter_box.textActivated.connect(lambda *_: self.press(*flt))

    def _press_of(self, what: str, function: str, inputs: Sequence[str],
                  outputs: Mapping) -> Callable[..., Any]:
        def pressed(*_args: Any) -> Any:
            return self.press(what, function, inputs, outputs)
        return pressed

    def press(self, what: str, function: str, inputs: Sequence[str],
              outputs: Mapping) -> Any:
        """Run one link, as a generated handler does: the function with the
        ports' values; its result's keys into the ports; on a failure the
        ports it fills cleared and the reason said."""
        try:
            fn = getattr(self.fc, function)
            result = fn(*[self.ports[name].get() for name in inputs])
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(result["error"])
            for port, key in outputs.items():
                self.ports[port].set(result[key])
            self.failed = ""
            return result
        except Exception as exc:                          # noqa: BLE001
            for port in outputs:
                try:
                    self.ports[port].clear()
                except Exception:                         # noqa: BLE001
                    pass
            self.report_error(what, exc)
            return None

    def report_error(self, what: str, exc: BaseException) -> None:
        msg = str(exc) or type(exc).__name__
        self.failed = f"{what} failed: {msg}"
        self.status.setText(self.failed)
        if os.environ.get("COUNCIL_NO_DIALOGS"):
            return
        try:
            QMessageBox.critical(self, f"{what} failed", msg)
        except Exception:                                 # noqa: BLE001
            pass

    def _attach_picker(self) -> Any:
        cp = builtin.classifier_picker()
        picker = cp.attach_to(self.fc, self.ports, "classifier_name",
                              "classifier_filter", "classified_with_line",
                              "capture_folder", parent=None,
                              status="classifier_status")
        if picker is not None:
            links = {key: link for key, *link in LINKS}
            run = self._press_of(*links["delete"])
            delete = self.buttons["delete"]
            try:
                delete.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
            guard = cp.ask_twice(picker, run, delete)
            delete.clicked.connect(lambda *_: guard.press())
        return picker

    # -- datasets ------------------------------------------------------------
    def fill_datasets(self, keep: str = "") -> None:
        keep = keep or self.dataset_name()
        ctx = self.plugin.ctx
        try:
            found = list(ctx.datasets()) if ctx is not None else []
        except Exception:                                 # noqa: BLE001
            found = []
        box = self.dataset_box
        box.blockSignals(True)
        box.clear()
        box.addItem(NO_DATASET, "")
        for name in found:
            box.addItem(name, name)
        i = box.findData(keep) if keep else 0
        box.setCurrentIndex(max(0, i))
        box.blockSignals(False)

    def dataset_name(self) -> str:
        return _s(self.dataset_box.currentData()).strip()

    def use_dataset(self, name: str) -> None:
        """Pick dataset `name` (sent from the Datasets window)."""
        self.fill_datasets(keep=name)
        self.status.setText(f"Dataset '{name}' picked: pick a class and "
                            f"press Mark dataset, or press Classify "
                            f"dataset.")

    def dataset_frames(self) -> List[str]:
        """The picked dataset's frames still on disk."""
        name = self.dataset_name()
        if not name:
            raise RuntimeError("pick a dataset first (Add to dataset beside "
                               "PNG / Raw makes one)")
        ctx = self.plugin.ctx
        if ctx is None:
            raise RuntimeError("Classification is switched off")
        return [str(p) for p in ctx.dataset(name).paths]

    # -- the main window -----------------------------------------------------
    def _reloaded(self) -> None:
        """The main window listed its folder again: the "classified with"
        line follows it."""
        self._image_folder.tell()
        if self.picker is not None:
            self.picker.line_soon()

    def showEvent(self, event: Any) -> None:              # noqa: N802
        super().showEvent(event)
        self.fill_datasets()

    def close_down(self) -> None:
        for stop in self._stop_listening:
            try:
                stop()
            except Exception:                             # noqa: BLE001
                pass
        self._stop_listening.clear()
