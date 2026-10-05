"""
council_qt.widgets.preset_picker — Typhon's preset picker and camera-area
line in the MAIN window, kept true to the camera whoever changes it.

WHY ATTACH LOOKS AFTER THEM, NOT ONLY A SCRIPT LINK
A combobox port holds ONE value: a link can write the text in the box, never
the list under it — and the list is the point (this camera's presets, in
this project). And the camera's area changes from places no link of the main
window sees: the settings window, a preset applied there, a change that
finished on a worker after its caller had stopped waiting. So, like the
slider (capture_review), frame_camera.attach hands these two ports to this
listener of frame_camera.on_camera_change — which runs on the UI thread
after every change — while the gspec's links stay ordinary links the user
can edit: picking calls pick_preset, "Save preset" calls save_preset.

REFILLING THE LIST NEVER APPLIES A PRESET
The link is connected to textActivated, which only a USER pick (or Return
in the box) emits; clear() and addItems() do not. A refill here is silent by
construction, and its currentTextChanged is blocked as well, so nothing
subscribed to the port hears a list being rebuilt as a choice.

ONE BOX, TWO JOBS
The box is editable: pick a preset to apply it, or type a new name and press
Save preset. A typed name that is not a preset yet answers with a hint
(pick_preset), not an error dialog for typing.

NOTHING HERE TOUCHES THE CAMERA ON A TIMER
It reads the camera's area and the preset file only when it is told that
something changed — never per tick, so the live view pays nothing for it.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QSignalBlocker, Qt
from PySide6.QtWidgets import QComboBox

#: What the box says while nothing is picked.
PLACEHOLDER = "Pick a preset, or type a name and Save"

#: The box's tooltip: what a preset is and where it lives.
TIP = ("This camera's presets — its settings and its own area — kept in "
       "this project (camera_presets.json).\nPick one to apply it (not "
       "while capturing). Type a new name and press Save preset to save the "
       "camera as it is now.")

#: The area line before any camera is connected.
NO_CAMERA = "Camera's area: — (no camera connected)"

#: The camera changes after which the area line is read again. A single
#: setting never moves the area, and is the frequent one (a slider drag).
AREA_EVENTS = frozenset({"connected", "area", "preset", "settings", "reset",
                         "defaults", "failed"})


def area_line(state: Dict[str, Any]) -> str:
    """One line for the area label from frame_camera.current_area()."""
    area = str(state.get("area") or "")
    if not area:
        return NO_CAMERA
    if state.get("full"):
        return (f"Camera's area: {area} — the whole sensor "
                f"({state.get('sensor', '')}), sensor px")
    return f"Camera's area: {area} of {state.get('sensor', '')}, sensor px"


class PresetPicker(QObject):
    """The main window's preset box and camera-area line, kept current.

    `api` is frame_camera (passed in, so this module never imports a
    top-level module); `combo` the preset port, `area` the area-label port
    — either may be None, and each is looked after only if present.
    """

    def __init__(self, api: Any, combo: Any = None, area: Any = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.api = api
        self.combo_port = combo
        self.area_port = area
        widget = getattr(combo, "widget", None)
        self.combo: Optional[QComboBox] = (widget if isinstance(widget,
                                                                QComboBox)
                                           else None)
        #: What each refresh read, for tests and the measurements.
        self.fills = 0
        self._remove: Optional[Callable[[], None]] = None
        if self.combo is not None:
            self._dress(self.combo)
        self._remove = api.on_camera_change(self.heard)
        owner = self.combo if self.combo is not None else getattr(
            area, "widget", None)
        if owner is not None:
            # The listener goes with the window: a closed window must not
            # keep being told about a camera it no longer shows.
            owner.destroyed.connect(self.close)
        self.refresh()

    # -- set-up ---------------------------------------------------------
    @staticmethod
    def _dress(combo: QComboBox) -> None:
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setToolTip(TIP)
        if combo.isEditable() and combo.lineEdit() is not None:
            combo.lineEdit().setPlaceholderText(PLACEHOLDER)
        else:
            combo.setPlaceholderText(PLACEHOLDER)
        combo.setMaxVisibleItems(20)

    def close(self, *_: Any) -> None:
        remove, self._remove = self._remove, None
        if remove is not None:
            remove()

    # -- what the camera says -------------------------------------------
    def heard(self, out: Dict[str, Any]) -> None:
        """One change, from frame_camera.on_camera_change (UI thread)."""
        what = str(out.get("what") or "")
        if what == "disconnected":
            self.fill([])
            self.show_area()
            return
        if what == "connected":
            self.refresh()
            return
        if what == "presets":
            self.fill(list(out.get("presets") or []),
                      select=str(out.get("name") or ""))
        elif what == "preset" and out.get("ok", True):
            self.select(str(out.get("name") or ""))
        if what in AREA_EVENTS:
            self.show_area()

    def refresh(self) -> None:
        """Everything again: this camera's presets and its area."""
        self.fill(self._names())
        self.show_area()

    def _names(self) -> List[str]:
        try:
            return list(self.api.list_presets().get("presets") or [])
        except Exception:                                 # noqa: BLE001
            return []

    # -- the box ----------------------------------------------------------
    def fill(self, names: List[str], select: str = "") -> None:
        """Put `names` in the box. The text in it stays when it is still one
        of them (or `select`, after a save or a rename); otherwise the box
        is left showing its placeholder."""
        combo = self.combo
        if combo is None:
            return
        self.fills += 1
        keep = select or combo.currentText()
        blocker = QSignalBlocker(combo)
        try:
            combo.clear()
            combo.addItems([str(n) for n in names])
            index = combo.findText(keep, Qt.MatchFlag.MatchFixedString) \
                if keep else -1
            combo.setCurrentIndex(index)
            if index < 0 and combo.isEditable():
                combo.setEditText("")
        finally:
            del blocker

    def select(self, name: str) -> None:
        """Show `name` as the preset in use (after it was applied)."""
        combo = self.combo
        if combo is None or not name:
            return
        index = combo.findText(name, Qt.MatchFlag.MatchFixedString)
        if index >= 0:
            blocker = QSignalBlocker(combo)
            try:
                combo.setCurrentIndex(index)
            finally:
                del blocker

    # -- the area line ----------------------------------------------------
    def show_area(self) -> None:
        port = self.area_port
        if port is None:
            return
        try:
            # Raises when no camera is connected: that is the NO_CAMERA case.
            text = area_line(self.api.current_area())
        except Exception:                                 # noqa: BLE001
            text = NO_CAMERA
        try:
            port.set(text)
        except Exception:                                 # noqa: BLE001
            pass
