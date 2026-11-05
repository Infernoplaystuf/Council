"""
council_qt.plugin_tabs — the tabs Council's switched-on plugins add.

At start-up `register(window)` lists the plugins (manifests only), keeps the
ones the user switched on in Settings that can run, and adds one tab each.
Like every other tab, a plugin's tab is built the first time it is opened;
only then is the plugin's code imported (council_core.app_plugins).

A PLUGIN THAT FAILS DOES NOT TAKE COUNCIL WITH IT
Loading the plugin and calling its entry are guarded together: anything it
raises (SystemExit included) is written to plugin_errors.log and the tab
shows what went wrong instead of the plugin, with the rest of Council
untouched. An entry that returns something other than a widget is a fault
too. Listing is guarded as well, so a broken plugins folder never stops
the launch.
"""
from __future__ import annotations

from typing import Any, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from council_core import app_plugins

from .view import amp


def tab_title(found: app_plugins.Found) -> str:
    return f"🧩 {found.name}"


def fault_page(name: str, reason: str) -> QWidget:
    """What a plugin's tab shows when the plugin could not start."""
    page = QWidget()
    page.setObjectName("PluginFaultPage")
    layout = QVBoxLayout(page)
    label = QLabel(amp(
        f"{name} could not start: {reason}\n\n"
        f"The details are in {app_plugins.ERROR_LOG} in Council's folder. "
        f"You can switch the plugin off in the Settings tab."))
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    layout.addWidget(label)
    layout.addStretch(1)
    return page


def build_page(found: app_plugins.Found, window: Any = None) -> QWidget:
    """The plugin's own tab, or a page saying why it could not start."""
    manifest = found.manifest
    if manifest is None or found.problem:
        return fault_page(found.name, found.problem or "its manifest is "
                                                       "missing")
    try:
        entry = app_plugins.load_entry(manifest)
        widget = entry(app_plugins.PluginContext(manifest=manifest,
                                                 window=window))
        if not isinstance(widget, QWidget):
            raise TypeError(f"{manifest.entry} returned "
                            f"{type(widget).__name__}, not a widget")
        return widget
    except KeyboardInterrupt:
        raise
    except BaseException as exc:                          # noqa: BLE001
        reason = app_plugins.record_fault(manifest, exc, "building its tab")
        return fault_page(manifest.name, reason)


def register(window: Any, found: Optional[List[app_plugins.Found]] = None
             ) -> List[str]:
    """Add a tab for each switched-on plugin; the titles added."""
    try:
        listed = app_plugins.discover() if found is None else found
        wanted = app_plugins.switched_on(listed)
    except Exception as exc:                              # noqa: BLE001
        print(f"[plugins] could not list plugins: {exc!r}", flush=True)
        return []
    titles = []
    for item in wanted:
        if item.manifest.kind != "tab":
            continue
        title = tab_title(item)
        window.add_tab(title, lambda f=item, w=window: build_page(f, w),
                       eager=False,
                       tab_id="plugin_" + item.manifest.id.replace(".", "_"))
        titles.append(title)
    return titles
