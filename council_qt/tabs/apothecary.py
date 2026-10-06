"""
council_qt.tabs.apothecary — the Apothecary, ported. Advanced mode, as in Tk.

A support console for Raspberry Pi inference nodes: a registry of SSH nodes,
LAN discovery, the Ollama provisioning wizard, static IP / ethernet keepalive
fixes, and a 60-second health poller that badges each node. Written against
docs/qt_migration/remaining_tabs_requirements.md §apothecary; the dialogs are
in apothecary_dialogs.py.

DEFECTS DESIGNED OUT HERE (numbered as in that document's table; 1, 2, 3, 6,
10, 12 and 13 are fixed in apothecary_engine.py / council_core.apothecary)
   4  After Discover saved a node, the list refresh cleared the selection and
      "Run the wizard now?" opened on nothing. The wizard is opened for the
      saved node BY NAME.
   5  Every health-status change rebuilt the list and dropped the user's
      selection. `refresh()` puts it back.
   7  The selected node came from splitting the row text on spaces. Each row
      carries its node's name as item data.
   8  Discover's worker wrote the log widget from the thread. See the dialogs.
   9  Test SSH, Check Ollama, Run Command and Restart Ollama ran paramiko on
      the GUI thread and froze the window for up to 30 s. All four are on
      workers now, like every other SSH action already was.
  11  Health changes never reached the Council transcript — the Tk console
      stored its ui_queue and never posted to it. They are mirrored now.

THE MONITOR IS STOPPED WITH THE TAB
`PiHealthMonitor.stop()` had no callers: Tk got away with it because the
process exits. A Qt tab can be torn down while the poller lives on, and its
next callback would land on a deleted widget. `destroyed` stops it and
unhooks the callback; `_guard` drops anything already in flight.

PASSWORDS
Node passwords are stored in plain text in vault/node_registry.json
(`council_core.apothecary.STORE_PASSWORDS`), exactly as the Tk build does.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QSplitter, QVBoxLayout,
                               QWidget)

from council_core import apothecary as apoth_core
from council_core import paths

from .. import dialogs, theme
from ..view import ViewHelpers, amp
from .apothecary_dialogs import (DiscoverDialog, InventoryDialog, LogView,
                                 NodeDialog, RegisterDialog, StaticIpDialog,
                                 WizardDialog)

RESTART_OLLAMA = ("sudo systemctl restart ollama 2>/dev/null "
                  "|| (pkill ollama; sleep 1; ollama serve &)")


class ApothecaryActions:
    """What the Apothecary tab can ask the application to do.

    Everything that SSHes blocks for seconds; the tab calls those on workers.
    """

    def __init__(self, vault_dir: Optional[Path] = None, apoth=None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.apoth = apoth or apoth_core.Apothecary(
            registry_path=str(apoth_core.registry_path(self.vault_dir)),
            store_passwords=apoth_core.STORE_PASSWORDS)

    @property
    def monitor(self):
        return self.apoth.monitor

    def reload_registry(self) -> None:
        """Re-read node_registry.json — the Pi setup writes it from its own
        registry object, so the cached copy here is stale afterwards."""
        self.apoth.registry.data = apoth_core._ae.safe_read_json(
            self.apoth.registry.path, {"nodes": []})

    def secure_node(self, name: str, approved_fingerprint=None):
        """council_core.pi_setup.setup.secure_existing_node — key login, no
        stored password, firewalled Ollama."""
        from council_core.pi_setup import setup as pi_setup
        return pi_setup.secure_existing_node(self.vault_dir, name,
                                             approved_fingerprint=approved_fingerprint)

    # -- the registry (fast; GUI thread is fine) --------------------------
    def list_nodes(self) -> List[apoth_core.NodeEntry]:
        return self.apoth.list_nodes()

    def node(self, name: str) -> Optional[apoth_core.NodeEntry]:
        try:
            return self.apoth._get(name)
        except KeyError:
            return None

    def save(self, entry, previous_name: Optional[str] = None) -> None:
        self.apoth.save_node(entry, previous_name)

    def save_discovered(self, name: str, ip: str, username: str,
                        password: str) -> None:
        self.apoth.upsert_node(apoth_core.merge_discovered(
            self.node(name), name, ip, username, password))

    def delete(self, name: str) -> None:
        self.apoth.delete_node(name)

    # -- SSH (slow; workers only) ---------------------------------------
    def discover(self, hostname: str, progress) -> Optional[str]:
        return apoth_core.discover_pi(hostname, progress_cb=progress)

    def confirm_ip(self, hostname: str, username: str, password: str,
                   ip: str, progress) -> str:
        temp = apoth_core.NodeEntry(name=hostname, host="", username=username,
                                    password=password)
        real = apoth_core.confirm_and_get_real_ip(
            self.apoth.engine, temp, ip, password or None, progress)
        try:
            temp.host = real
            rc, out, _err = self.apoth.engine.run_ssh(
                temp, "hostname", password or None, timeout_s=8)
            if rc == 0 and out.strip():
                progress(f"  Pi hostname: {out.strip().splitlines()[0]}")
        except Exception:                                 # noqa: BLE001
            pass
        return real

    def test(self, name: str, password: Optional[str]):
        return self.apoth.test(name, password_override=password)

    def check_ollama(self, name: str, password: Optional[str]):
        return self.apoth.engine.check_ollama(self.apoth._get(name), password)

    def run(self, name: str, cmd: str, password: Optional[str],
            timeout_s: int = 30):
        return self.apoth.run(name, cmd, password_override=password,
                              timeout_s=timeout_s)

    def refresh_models(self, node) -> List[str]:
        return self.apoth.refresh_installed_models(node)

    def provision(self, name: str, model: str, desktop_ip: str,
                  password: Optional[str], progress):
        return self.apoth.provision_pi(name, model, desktop_ip,
                                       password_override=password,
                                       progress_cb=progress)

    def static_ip(self, name: str, static_ip: str, gateway: str,
                  progress) -> bool:
        node = self.apoth._get(name)
        steps = apoth_core.render_steps("set_static_ip_eth", {
            "static_ip": static_ip, "gateway": gateway, "host": node.host,
            "model": "", "model_base": "", "desktop_ip": ""})
        ok, _ = self.apoth.engine.run_task_sequence(
            node, steps, node.password or None, progress_cb=progress)
        if ok:
            node.host = static_ip
            self.apoth.upsert_node(node)
        return ok

    def keepalive(self, name: str, desktop_ip: str, progress) -> bool:
        node = self.apoth._get(name)
        steps = apoth_core.render_steps("keepalive_setup", {
            "desktop_ip": desktop_ip, "host": node.host,
            "model": "", "model_base": ""})
        ok, _ = self.apoth.engine.run_task_sequence(
            node, steps, node.password or None, progress_cb=progress)
        return ok


class ApothecaryTab(ViewHelpers, QWidget):
    """Node list and actions, a detail line, and the output log."""

    def __init__(self, window=None,
                 actions: Optional[ApothecaryActions] = None,
                 ask_string: Callable = dialogs.askstring,
                 ask_yes_no: Callable = dialogs.askyesno):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or ApothecaryActions()
        self.ask_string = ask_string
        self.ask_yes_no = ask_yes_no
        self._tokens = theme.tokens("dark")
        self._busy = set()          # names of SSH actions in flight
        self.dialog = None          # the last dialog opened, for tests

        self._build()
        self.refresh()

        monitor = self.actions.monitor
        monitor.status_cb = self._on_status_change
        monitor.start()

        def teardown(*_args, _monitor=monitor) -> None:
            # Runs as the C++ object dies: touch nothing on self.
            _monitor.status_cb = None
            _monitor.stop()

        self.destroyed.connect(teardown)

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        split = QSplitter(Qt.Orientation.Vertical)

        top = QWidget()
        top_row = QHBoxLayout(top)
        top_row.setContentsMargins(0, 0, 0, 0)
        left = QVBoxLayout()
        left.addWidget(QLabel("Pi Nodes"))
        self.nodes = QListWidget()
        self.nodes.setStyleSheet("font-family: Consolas, monospace;")
        self.nodes.currentItemChanged.connect(self._on_select)
        left.addWidget(self.nodes, 1)
        top_row.addLayout(left, 1)

        buttons = QVBoxLayout()
        groups = (
            (("Refresh", self.refresh), ("Add Node", self.on_add),
             ("🔍 Discover Pi", self.on_discover), ("Edit Node", self.on_edit),
             ("Delete", self.on_delete)),
            (("Test SSH", self.on_test_ssh),
             ("Check Ollama", self.on_check_ollama),
             ("Run Command", self.on_run_command)),
            (("🍓 Set up a Pi (new or existing)…", self.on_pi_setup),
             ("🔒 Switch to key login", self.on_secure_node),
             ("🔧 Setup Pi Wizard", self.on_wizard),
             ("Set Static IP", self.on_static_ip),
             ("Fix Keepalive", self.on_keepalive),
             ("Restart Ollama", self.on_restart_ollama)),
            (("Copy Ollama URL", self.on_copy_url),
             ("📦 Model Inventory", self.on_inventory)),
        )
        for i, group in enumerate(groups):
            if i:
                buttons.addSpacing(10)
            for caption, slot in group:
                self._button(buttons, caption, slot)
        buttons.addStretch(1)
        top_row.addLayout(buttons)
        split.addWidget(top)

        bottom = QWidget()
        lower = QVBoxLayout(bottom)
        lower.setContentsMargins(0, 0, 0, 0)
        self.detail = QLabel("Select a node to see details")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"color: {self._tokens['info']};")
        lower.addWidget(self.detail)
        lower.addWidget(QLabel("Output:"))
        self.log = LogView(self._tokens)
        lower.addWidget(self.log, 1)
        split.addWidget(bottom)
        split.setSizes([320, 360])
        outer.addWidget(split, 1)

    # -- the list --------------------------------------------------------
    def refresh(self, select: Optional[str] = None) -> None:
        """Rebuild the list, keeping (or setting) the selection by NAME."""
        keep = select or self.selected_name()
        self.nodes.blockSignals(True)
        self.nodes.clear()
        current = None
        for node in self.actions.list_nodes():
            item = QListWidgetItem(apoth_core.row_label(node))
            item.setData(Qt.ItemDataRole.UserRole, node.name)
            token = apoth_core.STATUS_TOKEN.get(node.status, "muted_fg")
            item.setForeground(QColor(self._tokens[token]))
            self.nodes.addItem(item)
            if node.name == keep:
                current = item
        if current is not None:
            self.nodes.setCurrentItem(current)
        self.nodes.blockSignals(False)
        self._on_select()

    def selected_name(self) -> Optional[str]:
        item = self.nodes.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def selected_node(self):
        name = self.selected_name()
        return self.actions.node(name) if name else None

    def _on_select(self, *_args) -> None:
        node = self.selected_node()
        self.detail.setText(apoth_core.detail_line(node) if node
                            else "Select a node to see details")

    def _need_node(self, what: str = "that"):
        node = self.selected_node()
        if node is None:
            self.emit(f"Select a node first to {what}.")
        return node

    # -- output ----------------------------------------------------------
    def emit(self, text: str, error: bool = False) -> None:
        """Append to the output log. Safe from any thread."""
        self._to_ui(self._append, text, error)

    def _append(self, text: str, error: bool = False) -> None:
        self.log.append_tagged(text, apoth_core.log_tag(text, error))

    def _on_status_change(self, name: str, status: str, msg: str) -> None:
        """The monitor thread's callback — hop to the GUI thread first."""
        self._to_ui(self._status_changed, name, status, msg)

    def _status_changed(self, name: str, status: str, msg: str) -> None:
        line = f"{apoth_core.status_icon(status)} [{name}] {msg}"
        self._append(line)
        self._mirror(line)
        self.refresh()               # keeps the selection (defect 5)

    def _mirror(self, text: str) -> None:
        """Health changes into the Council transcript, if it is built."""
        tab_for = getattr(self.window, "tab", None)
        council = tab_for("⚖ Council") if callable(tab_for) else None
        if council is not None and hasattr(council, "append"):
            council.append("Apothecary", text, "final")

    # -- SSH on workers --------------------------------------------------
    def _password(self) -> Optional[str]:
        pw = self.ask_string("Password override",
                             "Password (blank = use stored):", show="*",
                             parent=self)
        return pw.strip() if pw and pw.strip() else None

    def _ssh(self, key: str, work: Callable[[], None]) -> None:
        """Run ``work`` on a worker, one of each ``key`` at a time."""
        if key in self._busy:
            self.emit(f"{key} is already running.")
            return
        self._busy.add(key)

        def run() -> None:
            try:
                work()
            except Exception as exc:                      # noqa: BLE001
                self.emit(f"✗ {exc}", True)
            finally:
                self._to_ui(self._busy.discard, key)
                self._to_ui(self.refresh)

        threading.Thread(target=run, name=f"apoth-{key.lower().replace(' ', '-')}",
                         daemon=True).start()

    def on_test_ssh(self) -> None:
        node = self._need_node("test SSH")
        if node is None:
            return
        pw, name = self._password(), node.name

        def work() -> None:
            ok, msg = self.actions.test(name, pw)
            self.emit(("✓" if ok else "✗") + f" SSH test [{name}]: {msg}",
                      not ok)

        self._ssh("SSH test", work)

    def on_check_ollama(self) -> None:
        node = self._need_node("check Ollama")
        if node is None:
            return
        pw, name = self._password(), node.name

        def work() -> None:
            ok, msg = self.actions.check_ollama(name, pw)
            self.emit(("✓" if ok else "✗") + f" Ollama [{name}]: {msg}",
                      not ok)

        self._ssh("Ollama check", work)

    def on_run_command(self) -> None:
        node = self._need_node("run a command")
        if node is None:
            return
        cmd = self.ask_string("Remote command", "Shell command:", parent=self)
        if not cmd:
            return
        pw, name = self._password(), node.name

        def work() -> None:
            rc, out, err = self.actions.run(name, cmd, pw, timeout_s=30)
            self.emit(f"rc={rc}")
            if out.strip():
                self.emit(out.strip())
            if err.strip():
                self.emit(err.strip(), True)

        self._ssh("Command", work)

    def on_restart_ollama(self) -> None:
        node = self._need_node("restart Ollama")
        if node is None:
            return
        pw, name = self._password(), node.name
        self.emit(f"Restarting Ollama on {name}...")

        def work() -> None:
            rc, _out, _err = self.actions.run(name, RESTART_OLLAMA, pw,
                                              timeout_s=20)
            self.emit(("✓" if rc == 0 else "⚠") + f" Ollama restart rc={rc}")

        self._ssh("Ollama restart", work)

    def on_keepalive(self) -> None:
        node = self._need_node("fix the keepalive")
        if node is None:
            return
        desktop = self.ask_string(
            "Desktop IP", "Your desktop's LAN IP address (the Pi pings it "
            "every 5 min to keep the ethernet link alive):",
            initialvalue=apoth_core.desktop_ip(), parent=self)
        if not desktop:
            return
        name = node.name
        self.emit(f"Installing ethernet keepalive on {name}...")

        def work() -> None:
            ok = self.actions.keepalive(name, desktop.strip(), self.emit)
            self.emit(("✓" if ok else "✗")
                      + f" Keepalive {'installed' if ok else 'FAILED'} on "
                      f"{name}", not ok)

        self._ssh("Keepalive", work)

    def on_static_ip(self) -> None:
        node = self._need_node("set a static IP")
        if node is None:
            return
        name = node.name

        def apply(static_ip: str, gateway: str) -> None:
            def work() -> None:
                ok = self.actions.static_ip(name, static_ip, gateway,
                                            self.emit)
                self.emit(("✓" if ok else "✗") + " Static IP "
                          f"{'set' if ok else 'FAILED'}: {static_ip}", not ok)
            self._ssh("Static IP", work)

        self._open(StaticIpDialog(node, apply, self))

    # -- the registry ----------------------------------------------------
    def _open(self, dialog) -> None:
        self.dialog = dialog
        dialog.open()

    def on_add(self) -> None:
        self._open(NodeDialog(None, self._save_node, self))

    def on_edit(self) -> None:
        node = self._need_node("edit it")
        if node is not None:
            self._open(NodeDialog(node, self._save_node, self))

    def _save_node(self, entry, previous: Optional[str]) -> None:
        self.actions.save(entry, previous)
        self.refresh(select=entry.name)
        renamed = f" (renamed from '{previous}')" if previous else ""
        self.emit(f"✓ Node '{entry.name}' saved{renamed}  "
                  f"[{entry.pi_model or 'unknown hardware'}]")

    def on_delete(self) -> None:
        node = self._need_node("delete it")
        if node is None:
            return
        if self.ask_yes_no("Confirm", f"Delete node '{node.name}'?",
                           parent=self):
            self.actions.delete(node.name)
            self.refresh()
            self.emit(f"Deleted '{node.name}'.")

    def on_discover(self) -> None:
        self._open(DiscoverDialog(self.actions, self._discovered, self))

    def _discovered(self, name: str, ip: str) -> None:
        self.refresh(select=name)
        self.emit(f"✓ Saved '{name}' at {ip}")
        if self.ask_yes_no("Run Setup Wizard?",
                           f"Node saved at {ip}.\n\nRun the Pi Setup Wizard "
                           "now to install Ollama?", parent=self):
            self.open_wizard(name)             # by name — defect 4

    def on_copy_url(self) -> None:
        node = self._need_node("copy its URL")
        if node is None:
            return
        url = apoth_core.ollama_url(node)
        try:
            QGuiApplication.clipboard().setText(url)
            self.emit(f"Copied: {url}")
        except Exception:                                 # noqa: BLE001
            self.emit(f"Ollama URL: {url}")

    def on_inventory(self) -> None:
        node = self._need_node("see its models")
        if node is not None:
            self._open(InventoryDialog(node, self.actions, self.emit, self))

    # -- the Pi setup (council_core.pi_setup) ---------------------------
    def on_pi_setup(self) -> None:
        from .pi_setup_dialog import PiSetupDialog
        dlg = PiSetupDialog(self, vault_dir=self.actions.vault_dir)
        dlg.finished.connect(lambda _r: (self.actions.reload_registry(), self.refresh()))
        self._open(dlg)

    def on_secure_node(self, approved_fingerprint=None) -> None:
        """Move the selected node off its stored password to the Council's
        key, and firewall its Ollama to this PC."""
        node = self._need_node("switch it to key login")
        if node is None:
            return
        name = node.name

        def work() -> None:
            from council_core.pi_setup import remote
            try:
                out = self.actions.secure_node(name, approved_fingerprint)
            except remote.HostKeyUnknown as exc:
                self._to_ui(self._confirm_secure, name, exc.fingerprint)
                return
            except Exception as exc:                      # noqa: BLE001
                self.emit(f"✗ {name}: {exc}", True)
                return
            self.emit(f"✓ {out.message}")
            self._to_ui(lambda: (self.actions.reload_registry(), self.refresh()))

        self._ssh("Secure node", work)

    def _confirm_secure(self, name: str, fingerprint: str) -> None:
        if self.ask_yes_no("Is this your Pi?",
                           f"The Council has not seen {name}'s SSH key before.\n\n"
                           f"{fingerprint}\n\nContinue only if this is your Pi.",
                           parent=self):
            self.on_secure_node(approved_fingerprint=fingerprint)

    # -- the wizard ------------------------------------------------------
    def on_wizard(self) -> None:
        node = self.selected_node()
        if node is None:
            self.emit("Add a node first (name, host/IP, SSH credentials), "
                      "select it, then click Setup Pi Wizard.")
            return
        self.open_wizard(node.name)

    def open_wizard(self, name: str) -> None:
        node = self.actions.node(name)
        if node is None:
            self.emit(f"✗ No node named '{name}'.", True)
            return

        def finished(ok: bool) -> None:
            self.refresh()
            if not ok:
                return
            fresh = self.actions.node(name) or node
            url = apoth_core.ollama_url(fresh)
            if self.ask_yes_no(
                    "Register with Council?",
                    f"✓ Pi setup complete!\n\nAdd {name} to the Council "
                    f"dispatcher?\nURL: {url}", parent=self):
                self._open(RegisterDialog(url, self.emit,
                                          apoth_core.find_launch_bat(),
                                          self))

        self._open(WizardDialog(node, self.actions, finished, self))


def build_apothecary(window) -> QWidget:
    """Factory for the tab registry."""
    return ApothecaryTab(window)
