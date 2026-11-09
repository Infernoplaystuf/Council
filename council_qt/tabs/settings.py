"""
council_qt.tabs.settings — Council's Settings tab.

    Online    the switch for the internet (council_core.web): OFF by default,
              and while it is off nothing in Council searches or reads the
              web. Ticking it saves at once to <app dir>/online.json; the
              search engine (DuckDuckGo, Brave with the user's key, or the
              user's own SearXNG) is saved with "Save search settings".
              COUNCIL_OFFLINE=1 greys it out, off.

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
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from council_core import app_plugins
from council_core import web

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

    def online(self) -> web.Settings:
        return web.load()

    def forced_offline(self) -> bool:
        return web.forced_offline()

    def save_online(self, settings: web.Settings) -> web.Settings:
        return web.save(settings)

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
        outer.addWidget(self._online_box())

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

    # -- online ------------------------------------------------------------
    def _online_box(self) -> QGroupBox:
        box = QGroupBox(amp("Online"))
        layout = QVBoxLayout(box)
        self.online_check = QCheckBox(amp(
            "Let Council search and read the web (Research tab)"))
        self.online_check.toggled.connect(self.on_online_toggled)
        layout.addWidget(self.online_check)
        self.online_note = QLabel("")
        self.online_note.setWordWrap(True)
        layout.addWidget(self.online_note)

        row = QHBoxLayout()
        row.addWidget(QLabel(amp("Search with")))
        self.engine_box = QComboBox()
        for key in web.ENGINES:
            self.engine_box.addItem(web.ENGINE_NAMES[key], key)
        self.engine_box.currentIndexChanged.connect(self._engine_fields)
        row.addWidget(self.engine_box)
        self.searxng_edit = QLineEdit()
        self.searxng_edit.setPlaceholderText("http://127.0.0.1:8888")
        row.addWidget(self.searxng_edit, 1)
        self.brave_edit = QLineEdit()
        self.brave_edit.setPlaceholderText("Brave Search API key")
        self.brave_edit.setEchoMode(QLineEdit.EchoMode.Password)
        row.addWidget(self.brave_edit, 1)
        self.save_search_btn = self._button(row, "Save search settings",
                                            self.on_save_search)
        layout.addLayout(row)
        self._load_online()
        return box

    def _online_call(self, name: str, *args):
        """The actions' online methods; a stand-in written before the
        Online box existed falls back to council_core.web's own."""
        fn = getattr(self.actions, name, None)
        if fn is None:
            fn = getattr(SettingsActions(), name)
        return fn(*args)

    def _load_online(self) -> None:
        s = self._online_call("online")
        forced = self._online_call("forced_offline")
        self._filling = True
        try:
            self.online_check.setChecked(s.online and not forced)
            self.online_check.setEnabled(not forced)
            i = self.engine_box.findData(s.search)
            self.engine_box.setCurrentIndex(max(0, i))
            self.searxng_edit.setText(s.searxng_url)
            self.brave_edit.setText(s.brave_key)
        finally:
            self._filling = False
        self._engine_fields()
        self._online_text(s.online and not forced, forced)

    def _online_text(self, on: bool, forced: bool = False) -> None:
        if forced:
            text = web.why_offline()
        elif on:
            text = ("Online. Research can search the web and read pages, one "
                    "at a time; no cookies or logins are used, and pages are "
                    "treated as information, never as instructions. Nothing "
                    "else in Council goes online.")
        else:
            text = ("Offline (the default). Nothing in Council reaches the "
                    "internet.")
        self.online_note.setText(amp(text))

    def _engine_fields(self, *_args) -> None:
        key = self.engine_box.currentData()
        self.searxng_edit.setVisible(key == "searxng")
        self.brave_edit.setVisible(key == "brave")

    def _form_settings(self) -> web.Settings:
        return web.Settings(online=self.online_check.isChecked(),
                            search=self.engine_box.currentData() or "duckduckgo",
                            searxng_url=self.searxng_edit.text().strip(),
                            brave_key=self.brave_edit.text().strip())

    def on_online_toggled(self, on: bool) -> None:
        if self._filling:
            return
        try:
            saved = self._online_call("online")
            saved.online = bool(on)
            self._online_call("save_online", saved)
        except Exception as exc:                          # noqa: BLE001
            self.status.setText(amp(f"Could not save: {exc}"))
            return
        self._online_text(bool(on))

    def on_save_search(self) -> None:
        try:
            self._online_call("save_online", self._form_settings())
        except Exception as exc:                          # noqa: BLE001
            self.online_note.setText(amp(f"Could not save: {exc}"))
            return
        self.online_note.setText(amp(
            f"Saved: search with "
            f"{web.ENGINE_NAMES[self.engine_box.currentData()]}."))

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
