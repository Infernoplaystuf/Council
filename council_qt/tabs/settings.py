"""
council_qt.tabs.settings — Council's Settings tab. Its first section is
Plugins: every plugin Council found, each OFF until it is ticked here.

    Plugins   one row per plugin folder: On, name, version, built in or
              added, and its status (ready, or why it cannot run). Ticking
              a row saves at once to <app dir>/plugins.json; the plugin's
              tab appears the next time Council starts (and goes when it is
              unticked). A plugin that cannot run, and every plugin from
              outside Council until there is an approval step, has its box
              greyed out with the reason beside it.

Listing reads manifests only (council_core.app_plugins.discover): no
plugin code runs in this tab. Nothing here starts a thread.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from council_core import app_plugins

from ..view import ViewHelpers, amp

COLUMNS = ("On", "Plugin", "Version", "From", "Status")


class SettingsActions:
    """What the tab does to the world, apart so a test can stand in."""

    def plugins(self) -> List[app_plugins.Found]:
        return app_plugins.discover()

    def enabled(self) -> List[str]:
        return app_plugins.enabled_ids()

    def set_enabled(self, ident: str, on: bool) -> None:
        app_plugins.set_enabled(ident, on)

    def plugins_folder(self):
        return app_plugins.user_dir()

    def open_folder(self, folder) -> None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


class SettingsTab(ViewHelpers, QWidget):
    def __init__(self, window=None, actions: Optional[SettingsActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or SettingsActions()
        self._found: List[app_plugins.Found] = []
        self._filling = False
        self._build()
        self.on_refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        box = QGroupBox(amp("Plugins"))
        layout = QVBoxLayout(box)
        intro = QLabel(amp(
            "Plugins add to Council. Every plugin starts off: tick one to "
            "switch it on. A change takes effect the next time Council "
            "starts."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTreeWidget()
        self.table.setColumnCount(len(COLUMNS))
        self.table.setHeaderLabels(list(COLUMNS))
        self.table.setRootIsDecorated(False)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.header()
        for col in range(len(COLUMNS) - 1):
            header.setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(COLUMNS) - 1,
                                    QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.currentItemChanged.connect(self.on_current_changed)
        layout.addWidget(self.table, 1)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail)

        row = QHBoxLayout()
        self._button(row, "⟳ Refresh", self.on_refresh)
        self._button(row, "📂 Open plugins folder", self.on_open_folder)
        row.addStretch(1)
        layout.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        outer.addWidget(box, 1)

    # -- the list --------------------------------------------------------
    def on_refresh(self) -> None:
        try:
            self._found = list(self.actions.plugins())
            on = set(self.actions.enabled())
        except Exception as exc:                          # noqa: BLE001
            self._found, on = [], set()
            self.status.setText(amp(f"Could not list plugins: {exc}"))
        self._filling = True
        try:
            self.table.clear()
            for index, found in enumerate(self._found):
                self.table.addTopLevelItem(self._row(index, found, on))
        finally:
            self._filling = False
        if not self._found:
            self.detail.setText(amp(
                "No plugins yet. Council's own plugins appear here as they "
                "ship."))
        else:
            self.detail.setText("")

    def _row(self, index: int, found: app_plugins.Found, on) -> QTreeWidgetItem:
        manifest = found.manifest
        item = QTreeWidgetItem(["", found.name,
                                manifest.version if manifest else "",
                                "Built in" if found.builtin else "Added",
                                "Ready" if found.can_turn_on
                                else found.problem])
        item.setData(0, Qt.ItemDataRole.UserRole, index)
        flags = item.flags() | Qt.ItemFlag.ItemIsUserCheckable
        if not found.can_turn_on:
            flags &= ~Qt.ItemFlag.ItemIsEnabled
        item.setFlags(flags)
        item.setCheckState(0, Qt.CheckState.Checked
                           if found.can_turn_on and found.id in on
                           else Qt.CheckState.Unchecked)
        item.setToolTip(4, found.problem or "Ready to switch on")
        return item

    def _found_of(self, item) -> Optional[app_plugins.Found]:
        if item is None:
            return None
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(index, int) and 0 <= index < len(self._found):
            return self._found[index]
        return None

    def on_item_changed(self, item, column: int) -> None:
        if self._filling or column != 0:
            return
        found = self._found_of(item)
        if found is None or not found.can_turn_on:
            return
        on = item.checkState(0) == Qt.CheckState.Checked
        try:
            self.actions.set_enabled(found.id, on)
        except Exception as exc:                          # noqa: BLE001
            self.status.setText(amp(f"Could not save: {exc}"))
            return
        self.status.setText(amp(
            f"{found.name} will be {'on' if on else 'off'} from the next "
            f"start of Council."))

    def on_current_changed(self, current, _previous=None) -> None:
        found = self._found_of(current)
        if found is None:
            return
        manifest = found.manifest
        lines = [found.name]
        if manifest is not None:
            if manifest.description:
                lines.append(manifest.description)
            lines.append(f"id {manifest.id} · kind {manifest.kind} · "
                         f"network {manifest.network}")
        lines.append(f"Folder: {found.folder}")
        if found.problem:
            lines.append(f"Can't be switched on: {found.problem}")
        self.detail.setText(amp("\n".join(lines)))

    def on_open_folder(self) -> None:
        self.actions.open_folder(self.actions.plugins_folder())


def build_settings(window) -> QWidget:
    """Factory for the tab registry."""
    return SettingsTab(window)
