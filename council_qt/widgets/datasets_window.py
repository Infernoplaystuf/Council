"""
council_qt.widgets.datasets_window — Typhon's datasets on screen: the "Add
to dataset…" dialog beside PNG / Raw, and the Datasets window (Andy, 2
November 2026: "a button near the png/Raw button that allows the user to
send images to a dataset. These data sets can then be sent to plugins.
Similar to how ORNL's Peregrine does image selection").

ADD TO DATASET
Adds the frame on screen, a stretch of its run (from frame a to frame b, as
the slider counts them in that run), or the whole run, to a dataset picked
from the list or typed as a new name. A dataset holds references to the
frames, never copies (council_core.datasets). The crop box on screen and the
camera are noted with each frame.

THE DATASETS WINDOW
The datasets on the left (frames, and how many are no longer on disk); the
picked one's frames as thumbnails on the right, which can be narrowed to one
run. Remove takes the selected frames out of the dataset (the files are not
touched); Send to hands the dataset to a plugin that takes datasets (the
classifier first; host.send_dataset); Merge, Rename and Delete act on the
dataset itself, Delete after a second press and only by moving its folder
aside. Thumbnails are read a few at a time on the UI thread's idle ticks, so
a large dataset never freezes the window.

Every dialog here can be driven without being shown (the tests, and
COUNCIL_NO_DIALOGS): each action is a method taking its answer.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QComboBox,
                               QDialog, QDialogButtonBox, QGridLayout,
                               QHBoxLayout, QInputDialog, QLabel,
                               QListWidget, QListWidgetItem, QMenu,
                               QPushButton, QRadioButton, QSpinBox,
                               QSplitter, QToolButton, QVBoxLayout, QWidget)

from council_core import datasets as ds

#: Thumbnail size, in pixels.
THUMB = 128
#: Thumbnails read per idle tick.
THUMBS_PER_TICK = 12
#: What the run filter says when it shows every run.
ALL_RUNS = "All runs"
#: How long Delete waits for its second press.
CONFIRM_MS = 4000
#: The dataset the last Add went to, offered first next time.
_LAST = {"name": ""}

PATH_ROLE = int(Qt.ItemDataRole.UserRole) + 11
NAME_ROLE = int(Qt.ItemDataRole.UserRole) + 12

THIS_FRAME, RANGE, WHOLE_RUN = "frame", "range", "run"


def _no_dialogs() -> bool:
    return bool(os.environ.get("COUNCIL_NO_DIALOGS"))


# ======================================================================
# Add to dataset
# ======================================================================
class AddToDatasetDialog(QDialog):
    """Which frames, and into which dataset."""

    def __init__(self, parent: Any = None, *, current: Any = None,
                 run_frames: Sequence[Any] = (), camera: str = "",
                 crop: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add to dataset")
        self.current = Path(str(current)) if current else None
        self.run_frames = [Path(str(p)) for p in run_frames]
        self.camera = camera
        self.crop = crop
        #: What the last add said ("" until one ran).
        self.summary = ""
        self.result_doc: Optional[Dict[str, Any]] = None
        info = ds.describe(self.current) if self.current else {}
        self.run = info.get("run", "")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Dataset — pick one, or type a new name",
                                self))
        self.name_box = QComboBox(self)
        self.name_box.setEditable(True)
        self.name_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.name_box.setCompleter(None)
        self.name_box.addItems(ds.names())
        last = _LAST["name"]
        if last and self.name_box.findText(last) >= 0:
            self.name_box.setCurrentText(last)
        elif not self.name_box.count():
            self.name_box.setEditText("")
        self.name_box.lineEdit().setPlaceholderText("A new dataset's name")
        layout.addWidget(self.name_box)

        self.choice = QButtonGroup(self)
        name = self.current.name if self.current else "no frame on screen"
        self.this_frame = QRadioButton(f"This frame ({name})", self)
        self.choice.addButton(self.this_frame)
        layout.addWidget(self.this_frame)
        n = len(self.run_frames)
        row = QHBoxLayout()
        self.range_button = QRadioButton("Frames of this run from", self)
        self.choice.addButton(self.range_button)
        self.from_box = QSpinBox(self)
        self.to_box = QSpinBox(self)
        for box in (self.from_box, self.to_box):
            box.setRange(1, max(1, n))
        here = self._index_of_current()
        self.from_box.setValue(here + 1 if here >= 0 else 1)
        self.to_box.setValue(max(1, n))
        row.addWidget(self.range_button)
        row.addWidget(self.from_box)
        row.addWidget(QLabel("to", self))
        row.addWidget(self.to_box)
        row.addWidget(QLabel(f"of {n}", self))
        row.addStretch(1)
        layout.addLayout(row)
        run = self.run or "—"
        self.whole_run = QRadioButton(f"The whole run {run} ({n} frames)",
                                      self)
        self.choice.addButton(self.whole_run)
        layout.addWidget(self.whole_run)
        self.this_frame.setChecked(True)
        if self.current is None:
            self.this_frame.setEnabled(False)
        if not self.run_frames:
            for w in (self.range_button, self.from_box, self.to_box,
                      self.whole_run):
                w.setEnabled(False)
        note = QLabel("The dataset keeps where each frame is, not a copy of "
                      "it: moving or deleting a run's folder leaves a gap "
                      "the Datasets window shows.", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.said = QLabel("", self)
        self.said.setWordWrap(True)
        layout.addWidget(self.said)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel,
                                   self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _index_of_current(self) -> int:
        if self.current is None:
            return -1
        want = os.path.normcase(os.path.abspath(str(self.current)))
        for i, p in enumerate(self.run_frames):
            if os.path.normcase(os.path.abspath(str(p))) == want:
                return i
        return -1

    def frames(self, how: str = "") -> List[Path]:
        """The frames the dialog's choice (or `how`) names."""
        how = how or (RANGE if self.range_button.isChecked() else
                      WHOLE_RUN if self.whole_run.isChecked() else THIS_FRAME)
        if how == THIS_FRAME:
            return [self.current] if self.current else []
        if how == WHOLE_RUN:
            return list(self.run_frames)
        a, b = sorted((self.from_box.value(), self.to_box.value()))
        return self.run_frames[a - 1:b]

    def add(self, name: str = "", how: str = "") -> Dict[str, Any]:
        """Add the chosen frames to dataset `name` (else the box's)."""
        name = name or self.name_box.currentText()
        doc = ds.add_frames(name, self.frames(how), camera=self.camera,
                            crop=self.crop)
        _LAST["name"] = doc["name"]
        self.result_doc = doc
        self.summary = doc["summary"]
        return doc

    def _ok(self) -> None:
        try:
            self.add()
        except Exception as exc:                          # noqa: BLE001
            self.said.setText(str(exc))
            return
        self.accept()


def add_to_dataset(parent: Any, *, current: Any, run_frames: Sequence[Any],
                   camera: str = "", crop: Any = None) -> str:
    """Ask, then add; what happened ("" when cancelled)."""
    if not current and not run_frames:
        return ("Add to dataset: show a saved frame first — pick the image "
                "folder and move the slider onto a frame")
    dlg = AddToDatasetDialog(parent, current=current, run_frames=run_frames,
                             camera=camera, crop=crop)
    if _no_dialogs():
        return "Add to dataset needs its dialog (COUNCIL_NO_DIALOGS is set)"
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return ""
    return dlg.summary


# ======================================================================
# The Datasets window
# ======================================================================
class DatasetsWindow(QWidget):
    """The vault's datasets, a picked one's frames, and what to do with
    them. `host` is the plugin host (for Send to); `say` the main window's
    status line."""

    def __init__(self, host: Any = None, *,
                 say: Optional[Callable[[str], Any]] = None,
                 title: str = "Datasets") -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.host = host
        self.say_fn = say
        self.setWindowTitle(title)
        self.entries: List[Dict[str, Any]] = []
        self._pending: List[QListWidgetItem] = []
        self._asked = ""

        split = QSplitter(self)
        left = QWidget(split)
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("Datasets", left))
        self.list = QListWidget(left)
        self.list.currentItemChanged.connect(lambda *_: self.show_frames())
        lv.addWidget(self.list, 1)
        row = QGridLayout()
        self.merge_button = QPushButton("Merge into…", left)
        self.rename_button = QPushButton("Rename…", left)
        self.delete_button = QPushButton("Delete", left)
        self.merge_button.clicked.connect(lambda: self.merge_into())
        self.rename_button.clicked.connect(lambda: self.rename_to())
        self.delete_button.clicked.connect(lambda: self.delete())
        row.addWidget(self.merge_button, 0, 0)
        row.addWidget(self.rename_button, 0, 1)
        row.addWidget(self.delete_button, 1, 0, 1, 2)
        lv.addLayout(row)

        right = QWidget(split)
        rv = QVBoxLayout(right)
        top = QHBoxLayout()
        self.heading = QLabel("", right)
        top.addWidget(self.heading, 1)
        top.addWidget(QLabel("Show", right))
        self.run_box = QComboBox(right)
        self.run_box.currentIndexChanged.connect(lambda *_: self.fill_frames())
        top.addWidget(self.run_box)
        rv.addLayout(top)
        self.frames = QListWidget(right)
        self.frames.setViewMode(QListWidget.ViewMode.IconMode)
        self.frames.setIconSize(QSize(THUMB, THUMB))
        self.frames.setGridSize(QSize(THUMB + 28, THUMB + 48))
        self.frames.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.frames.setMovement(QListWidget.Movement.Static)
        self.frames.setSpacing(4)
        self.frames.setWordWrap(True)
        self.frames.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        rv.addWidget(self.frames, 1)
        bottom = QHBoxLayout()
        self.remove_button = QPushButton("Remove selected frames", right)
        self.remove_button.clicked.connect(lambda: self.remove_selected())
        self.send_button = QToolButton(right)
        self.send_button.setText("Send to")
        self.send_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self.send_menu = QMenu(self.send_button)
        self.send_menu.aboutToShow.connect(self.fill_send_menu)
        self.send_button.setMenu(self.send_menu)
        bottom.addWidget(self.remove_button)
        bottom.addStretch(1)
        bottom.addWidget(self.send_button)
        rv.addLayout(bottom)
        self.status = QLabel("", right)
        self.status.setWordWrap(True)
        rv.addWidget(self.status)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 3)
        outer = QVBoxLayout(self)
        outer.addWidget(split)
        self.resize(980, 640)

        blank = QPixmap(THUMB, THUMB)
        blank.fill(QColor("#dddddd"))
        self._blank = QIcon(blank)
        self._thumbs = QTimer(self)
        self._thumbs.setInterval(0)
        self._thumbs.timeout.connect(self._read_thumbs)
        self._confirm = QTimer(self)
        self._confirm.setSingleShot(True)
        self._confirm.timeout.connect(self._cancel_delete)
        self.refresh()

    # -- the list ------------------------------------------------------------
    def current_name(self) -> str:
        item = self.list.currentItem()
        return str(item.data(NAME_ROLE)) if item is not None else ""

    def refresh(self, keep: str = "") -> None:
        keep = keep or self.current_name()
        self.list.blockSignals(True)
        self.list.clear()
        pick = None
        for info in ds.listing():
            missing = info.get("missing") or 0
            text = f"{info['name']} — {info['frames']} frames"
            if missing:
                text += f", {missing} missing"
            if info.get("problem"):
                text += " (cannot be read)"
            item = QListWidgetItem(text, self.list)
            item.setData(NAME_ROLE, info["name"])
            if info.get("problem"):
                item.setToolTip(info["problem"])
            if info["name"].casefold() == keep.casefold():
                pick = item
        if pick is None and self.list.count():
            pick = self.list.item(0)
        if pick is not None:
            self.list.setCurrentItem(pick)
        self.list.blockSignals(False)
        self.show_frames()

    def showEvent(self, event: Any) -> None:              # noqa: N802
        super().showEvent(event)
        self.refresh()

    def show_frames(self) -> None:
        name = self.current_name()
        self.entries = []
        self.run_box.blockSignals(True)
        self.run_box.clear()
        if not name:
            self.heading.setText("No datasets yet: use Add to dataset beside "
                                 "PNG / Raw.")
            self.run_box.blockSignals(False)
            self.fill_frames()
            return
        try:
            self.entries = ds.frames(name)
        except ds.DatasetError as exc:
            self.heading.setText(str(exc))
            self.run_box.blockSignals(False)
            self.fill_frames()
            return
        runs = sorted({e.get("run") or "" for e in self.entries} - {""})
        self.run_box.addItem(ALL_RUNS, "")
        for run in runs:
            self.run_box.addItem(run, run)
        self.run_box.blockSignals(False)
        missing = sum(1 for e in self.entries if not e["exists"])
        self.heading.setText(
            f"{name}: {len(self.entries)} frames from {len(runs)} run"
            f"{'s' if len(runs) != 1 else ''}"
            + (f" — {missing} no longer on disk" if missing else ""))
        self.fill_frames()

    def fill_frames(self) -> None:
        run = str(self.run_box.currentData() or "")
        self.frames.clear()
        self._pending = []
        for e in self.entries:
            if run and e.get("run") != run:
                continue
            if e.get("run") and isinstance(e.get("frame"), int):
                text = f"{e['run']}\nframe {e['frame']}"
            else:
                text = e.get("name") or Path(e["path"]).name
            if not e["exists"]:
                text += "\n(missing)"
            item = QListWidgetItem(text, self.frames)
            item.setIcon(self._blank)
            item.setSizeHint(QSize(THUMB + 24, THUMB + 44))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter
                                  | Qt.AlignmentFlag.AlignTop)
            item.setData(PATH_ROLE, e["path"])
            item.setToolTip(e["path"])
            if e["exists"]:
                self._pending.append(item)
            else:
                item.setForeground(QColor("#999999"))
        if self._pending:
            self._thumbs.start()

    def _read_thumbs(self) -> None:
        for _ in range(THUMBS_PER_TICK):
            if not self._pending:
                self._thumbs.stop()
                return
            item = self._pending.pop(0)
            try:
                item.setIcon(QIcon(thumbnail(item.data(PATH_ROLE))))
            except RuntimeError:              # the item was cleared away
                continue

    # -- actions on frames ---------------------------------------------------
    def selected_paths(self) -> List[str]:
        return [str(i.data(PATH_ROLE)) for i in self.frames.selectedItems()]

    def remove_selected(self, paths: Optional[Sequence[str]] = None) -> str:
        name = self.current_name()
        paths = list(paths) if paths is not None else self.selected_paths()
        if not name or not paths:
            return self._tell("Pick frames to remove first.")
        try:
            said = ds.remove_frames(name, paths)["summary"]
        except ds.DatasetError as exc:
            said = str(exc)
        self.refresh(keep=name)
        return self._tell(said)

    def fill_send_menu(self) -> None:
        self.send_menu.clear()
        pids = self.receivers()
        if not pids:
            act = self.send_menu.addAction("No plugin takes datasets here")
            act.setEnabled(False)
            return
        for pid in pids:
            m = self.host.manifest(pid)
            act = self.send_menu.addAction(getattr(m, "name", "") or pid)
            act.triggered.connect(lambda _c=False, p=pid: self.send_to(p))

    def receivers(self) -> List[str]:
        if self.host is None:
            return []
        try:
            return list(self.host.dataset_plugins())
        except Exception:                                 # noqa: BLE001
            return []

    def send_to(self, pid: str) -> str:
        name = self.current_name()
        if not name:
            return self._tell("Pick a dataset to send first.")
        if self.host is None:
            return self._tell("No plugins run in this window.")
        try:
            said = self.host.send_dataset(pid, name)
        except Exception as exc:                          # noqa: BLE001
            said = str(exc)
        return self._tell(said)

    # -- actions on datasets -------------------------------------------------
    def _ask_text(self, title: str, label: str, text: str = "",
                  items: Optional[List[str]] = None) -> str:
        if _no_dialogs():
            return ""
        if items is not None:
            got, ok = QInputDialog.getItem(self, title, label, items, 0, True)
        else:
            got, ok = QInputDialog.getText(self, title, label, text=text)
        return str(got).strip() if ok else ""

    def merge_into(self, target: str = "") -> str:
        name = self.current_name()
        if not name:
            return self._tell("Pick the dataset to merge first.")
        others = [n for n in ds.names() if n.casefold() != name.casefold()]
        target = target or self._ask_text(
            "Merge into", f"Add every frame of '{name}' to the dataset:",
            items=others)
        if not target:
            return ""
        try:
            said = ds.merge(target, [name])["summary"] + \
                f" '{name}' is left as it was."
        except ds.DatasetError as exc:
            said = str(exc)
        self.refresh(keep=target)
        return self._tell(said)

    def rename_to(self, new: str = "") -> str:
        name = self.current_name()
        if not name:
            return self._tell("Pick the dataset to rename first.")
        new = new or self._ask_text("Rename", f"New name for '{name}':",
                                    text=name)
        if not new:
            return ""
        try:
            doc = ds.rename(name, new)
            said, keep = doc["summary"], doc["name"]
        except (ds.DatasetError, OSError) as exc:
            said, keep = str(exc), name
        self.refresh(keep=keep)
        return self._tell(said)

    def delete(self) -> str:
        """First press asks; a second within CONFIRM_MS on the same dataset
        moves its folder aside."""
        name = self.current_name()
        if not name:
            return self._tell("Pick the dataset to delete first.")
        if self._asked != name.casefold() or not self._confirm.isActive():
            self._asked = name.casefold()
            self._confirm.start(CONFIRM_MS)
            self.delete_button.setText("Sure?")
            return self._tell(f"Delete '{name}'? Press Delete again within "
                              f"{CONFIRM_MS // 1000} s. Its folder is moved "
                              f"aside (moving it back restores it); the "
                              f"frames are not touched.")
        self._cancel_delete()
        try:
            said = ds.delete(name)["summary"]
        except (ds.DatasetError, OSError) as exc:
            said = str(exc)
        self.refresh()
        return self._tell(said)

    def _cancel_delete(self) -> None:
        self._asked = ""
        self._confirm.stop()
        self.delete_button.setText("Delete")

    def _tell(self, text: str) -> str:
        self.status.setText(text)
        if self.say_fn is not None and text:
            try:
                self.say_fn(text)
            except Exception:                             # noqa: BLE001
                pass
        return text


def thumbnail(path: Any, size: int = THUMB) -> QPixmap:
    """A small picture of the frame, read scaled (never the whole file into
    a full-size image first)."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    full = reader.size()
    if full.isValid() and full.width() > 0 and full.height() > 0:
        reader.setScaledSize(full.scaled(size, size,
                                         Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        pix = QPixmap(size, size)
        pix.fill(QColor("#555555"))
        return pix
    return QPixmap.fromImage(image)
