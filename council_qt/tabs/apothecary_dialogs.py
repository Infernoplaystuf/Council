"""
council_qt.tabs.apothecary_dialogs — the Apothecary's six windows.

Discover, Add/Edit, Model Inventory, Setup Wizard, Register-with-Council and
Static IP: the six Toplevels of `apothecary_engine.ApothecaryConsole`, as
QDialogs. Each collects plain values and hands them to one core call.

EVERY WORKER TALKS TO THE WINDOW THROUGH `_to_ui`
The Tk Discover worker wrote its log widget straight from the thread and
called `update_idletasks()` off the main loop (defect 8). Tk usually survives
that; Qt does not — a worker touching a QWidget is an access violation. Here
every progress callback a worker is handed is `self._to_ui`-wrapped, so the
thread never holds a widget.

NON-BLOCKING
Dialogs are shown with `open()` (window-modal, returns at once) rather than
`exec()`, so a nested event loop never runs while a worker is posting back.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QTextEdit, QVBoxLayout, QWidget)

from council_core import apothecary as apoth_core

from .. import theme
from ..view import ViewHelpers, amp


def _password_field(text: str = "") -> QLineEdit:
    field = QLineEdit(text)
    field.setEchoMode(QLineEdit.EchoMode.Password)
    return field


def _muted(text: str, tokens) -> QLabel:
    label = QLabel(amp(text))
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {tokens['muted_fg']};")
    return label


def _heading(text: str) -> QLabel:
    label = QLabel(amp(text))
    label.setStyleSheet("font-weight: bold;")
    return label


class LogView(QTextEdit):
    """A read-only log that colours each line by the Apothecary's tag rule.

    GUI thread only — workers reach it through the owning dialog's `_to_ui`.
    """

    def __init__(self, tokens, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setStyleSheet("font-family: Consolas, monospace;")
        self._colours = {"ok": tokens["success"], "err": tokens["error"],
                         "warn": tokens["warning"], "hdr": tokens["info"],
                         "info": tokens["fg"]}

    def append_tagged(self, text: str, tag: str = "info") -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self._colours.get(tag, self._colours["info"])))
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text.rstrip() + "\n", fmt)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()


class _Dialog(ViewHelpers, QDialog):
    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._tokens = theme.tokens("dark")
        self.bridge = getattr(parent, "bridge", None)


# ============================================================
# Discover
# ============================================================

class DiscoverDialog(_Dialog):
    """Find a Pi by hostname, confirm its real address over SSH, save it.

    ``on_saved(name, ip)`` runs after a successful save — the tab uses it to
    offer the wizard FOR THAT NODE, rather than for "whatever is selected",
    which a list refresh had just cleared (defect 4).
    """

    def __init__(self, actions, on_saved: Callable[[str, str], None],
                 parent: Optional[QWidget] = None):
        super().__init__("Discover Pi", parent)
        self.actions = actions
        self.on_saved = on_saved
        self._busy = False
        self._ip: Optional[str] = None
        self.resize(560, 460)

        outer = QVBoxLayout(self)
        outer.addWidget(_heading("Auto-discover a Raspberry Pi by hostname"))
        outer.addWidget(_muted("Enter the Pi hostname and password. The "
                               "Apothecary tries mDNS, ping, ARP, then a "
                               "subnet scan.", self._tokens))
        form = QFormLayout()
        self.hostname = QLineEdit("raspberrypi")
        self.hostname.setPlaceholderText("without .local")
        self.save_as = QLineEdit()
        self.save_as.setPlaceholderText("blank = same as hostname")
        self.username = QLineEdit("pi")
        self.password = _password_field()
        form.addRow("Hostname:", self.hostname)
        form.addRow("Save as:", self.save_as)
        form.addRow("SSH username:", self.username)
        form.addRow("SSH password:", self.password)
        outer.addLayout(form)
        outer.addWidget(QLabel("Discovery log:"))
        self.log = LogView(self._tokens)
        outer.addWidget(self.log, 1)

        row = QHBoxLayout()
        self.find_btn = self._button(row, "🔍  Find Pi", self.on_find)
        self.save_btn = self._button(row, "✓  Save Node", self.on_save)
        self.save_btn.setEnabled(False)
        row.addStretch(1)
        self._button(row, "Close", self.reject)
        outer.addLayout(row)

    def _log(self, msg: str) -> None:
        self.log.append_tagged(msg, apoth_core.log_tag(msg))

    def name(self) -> str:
        return self.save_as.text().strip() or self.hostname.text().strip()

    def on_find(self) -> None:
        hostname = self.hostname.text().strip()
        if not hostname:
            self._log("✗ Enter a hostname.")
            return
        if self._busy:
            return
        user = self.username.text().strip() or "pi"
        password = self.password.text().strip()
        self._busy = True
        self._ip = None
        self.find_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.log.clear()
        post = lambda m: self._to_ui(self._log, m)        # noqa: E731

        def work() -> None:
            ip = None
            try:
                post(f"Searching for '{hostname}' on the network ...")
                found = self.actions.discover(hostname, post)
                if not found:
                    post("✗ Pi not found.")
                    post("  Check: Pi is on, ethernet connected, SSH enabled.")
                    return
                post(f"Confirming via SSH at {found} ...")
                ip = self.actions.confirm_ip(hostname, user, password,
                                             found, post)
            except Exception as exc:                      # noqa: BLE001
                post(f"✗ Discovery failed: {exc}")
            finally:
                self._to_ui(self._found, ip)

        threading.Thread(target=work, name="apoth-discover",
                         daemon=True).start()

    def _found(self, ip: Optional[str]) -> None:
        self._busy = False
        self._ip = ip
        self.find_btn.setEnabled(True)
        self.save_btn.setEnabled(ip is not None)
        if ip:
            self._log(f"\n✓ Ready to save as '{self.name()}' at {ip}")

    def on_save(self) -> None:
        if not self._ip:
            self._log("✗ Run discovery first.")
            return
        name = self.name()
        self.actions.save_discovered(name, self._ip,
                                     self.username.text().strip() or "pi",
                                     self.password.text().strip())
        self.accept()
        self.on_saved(name, self._ip)


# ============================================================
# Add / Edit
# ============================================================

class NodeDialog(_Dialog):
    """The node form. ``on_save(entry, previous_name)`` does the saving."""

    def __init__(self, existing, on_save, parent: Optional[QWidget] = None):
        super().__init__("Edit Node" if existing else "Add Node", parent)
        self.existing = existing
        self.on_save = on_save
        self.resize(520, 600)
        g = lambda attr, default="": getattr(existing, attr, default) \
            if existing else default                      # noqa: E731

        outer = QVBoxLayout(self)
        outer.addWidget(_heading("Node Configuration"))
        form = QFormLayout()
        self.name = QLineEdit(g("name"))
        self.host = QLineEdit(g("host"))
        self.port = QLineEdit(str(g("port", 22)))
        self.ollama_port = QLineEdit(str(g("ollama_port", 11434)))
        self.username = QLineEdit(g("username", "pi"))
        self.auth = QComboBox()
        self.auth.addItems(apoth_core.AUTH_METHODS)
        self.auth.setCurrentText(g("auth_method", "password"))
        self.password = _password_field(g("password"))
        self.key_path = QLineEdit(g("key_path"))
        ports = QHBoxLayout()
        ports.addWidget(self.port)
        ports.addWidget(QLabel("Ollama port:"))
        ports.addWidget(self.ollama_port)
        form.addRow("Name:", self.name)
        form.addRow("Host / IP:", self.host)
        form.addRow("SSH port:", ports)
        form.addRow("Username:", self.username)
        form.addRow("Auth method:", self.auth)
        form.addRow("Password:", self.password)
        form.addRow("Key path:", self.key_path)

        self.pi_model = QComboBox()
        self.pi_model.addItem("")
        self.pi_model.addItems(apoth_core.PI_MODELS)
        self.pi_model.setCurrentText(g("pi_model"))
        self.ai_hat = QCheckBox(amp(f"AI HAT+ attached "
                                    f"({apoth_core.AI_HAT_TOPS:g} TOPS)"))
        self.ai_hat.setChecked(bool(g("has_ai_hat", False)))
        self.role = QComboBox()
        self.role.addItems(apoth_core.COUNCIL_ROLES)
        self.role.setCurrentText(g("council_role") or "unassigned")
        self.notes = QLineEdit(g("notes"))
        form.addRow("Pi model:", self.pi_model)
        form.addRow("", self.ai_hat)
        form.addRow("Council role:", self.role)
        form.addRow("", _muted("heavy = Sage/Strategist   fast = "
                               "Intern/Peasant", self._tokens))
        form.addRow("Notes:", self.notes)
        outer.addLayout(form)

        self.hint = _muted("", self._tokens)
        outer.addWidget(self.hint)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {self._tokens['error']};")
        outer.addWidget(self.error)
        outer.addStretch(1)
        row = QHBoxLayout()
        self._button(row, "✓  Save", self.on_accept)
        row.addStretch(1)
        self._button(row, "Cancel", self.reject)
        outer.addLayout(row)

        self.pi_model.currentTextChanged.connect(self._on_pi_model)
        self._show_hint(self.pi_model.currentText())

    def _on_pi_model(self, pi_model: str) -> None:
        role = apoth_core.auto_role(pi_model)
        if role:
            self.role.setCurrentText(role)
        if "AI HAT" in pi_model:
            self.ai_hat.setChecked(True)
        self._show_hint(pi_model)

    def _show_hint(self, pi_model: str) -> None:
        recs = apoth_core.recommendations(pi_model)
        self.hint.setText("Recommended models: " + ", ".join(recs) if recs
                          else "Select a Pi model to see recommendations")

    def form(self) -> dict:
        return {"name": self.name.text(), "host": self.host.text(),
                "port": self.port.text(),
                "ollama_port": self.ollama_port.text(),
                "username": self.username.text(),
                "auth_method": self.auth.currentText(),
                "password": self.password.text(),
                "key_path": self.key_path.text(), "notes": self.notes.text(),
                "pi_model": self.pi_model.currentText(),
                "has_ai_hat": self.ai_hat.isChecked(),
                "council_role": self.role.currentText()}

    def on_accept(self) -> None:
        try:
            entry, previous = apoth_core.merge_form(self.existing, self.form())
        except apoth_core.FormError as exc:
            self.error.setText(str(exc))
            return
        self.on_save(entry, previous)
        self.accept()


# ============================================================
# Model Inventory
# ============================================================

def installed_text(models, active: str) -> str:
    if not models:
        return "  (no models recorded — click Refresh below)"
    return "\n".join(f"  {'▶ ' if m == active else '  '}{m}" for m in models)


def call_log_text(model_log) -> str:
    if not model_log:
        return "  (no calls logged yet)"
    return "\n".join(
        f"  {str(e.get('ts', ''))[:16]}  {e.get('role', '?'):<14} "
        f"{e.get('model', '?')}" for e in reversed(model_log[-50:]))


class InventoryDialog(_Dialog):
    def __init__(self, node, actions, emit: Callable[[str, bool], None],
                 parent: Optional[QWidget] = None):
        super().__init__(f"Model Inventory — {node.name}", parent)
        self.node = node
        self.actions = actions
        self.emit = emit
        self._busy = False
        self.resize(580, 500)

        outer = QVBoxLayout(self)
        outer.addWidget(_heading(
            f"{node.name}  [{node.pi_model or 'unknown'}]"
            + ("  AI HAT+" if node.has_ai_hat else "")))
        outer.addWidget(_muted(
            f"Council role: {node.council_role or 'unassigned'}  |  "
            f"Active model: {node.active_model or '—'}", self._tokens))
        outer.addWidget(_heading("INSTALLED MODELS"))
        self.installed = LogView(self._tokens)
        self.installed.setMaximumHeight(130)
        self.installed.setPlainText(installed_text(node.installed_models,
                                                   node.active_model))
        outer.addWidget(self.installed)
        outer.addWidget(_heading("RECENT CALL LOG (last 50)"))
        self.calls = LogView(self._tokens)
        self.calls.setPlainText(call_log_text(node.model_log))
        outer.addWidget(self.calls, 1)
        row = QHBoxLayout()
        self.refresh_btn = self._button(row, "↺  Refresh Model List",
                                        self.on_refresh)
        row.addStretch(1)
        self._button(row, "Close", self.reject)
        outer.addLayout(row)

    def on_refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.refresh_btn.setEnabled(False)
        self.emit(f"Refreshing model list from {self.node.name}...", False)
        node = self.node

        def work() -> None:
            try:
                models = self.actions.refresh_models(node)
                self._to_ui(self._refreshed, models, None)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self._refreshed, None, exc)

        threading.Thread(target=work, name="apoth-inventory",
                         daemon=True).start()

    def _refreshed(self, models, error) -> None:
        self._busy = False
        self.refresh_btn.setEnabled(True)
        if error is not None:
            self.emit(f"✗ {self.node.name}: could not refresh models: "
                      f"{error}", True)
            return
        self.installed.setPlainText(
            installed_text(models, self.node.active_model)
            if models else "  (no models found)")
        self.emit(f"✓ {self.node.name}: {len(models)} model(s) found", False)


# ============================================================
# Setup Wizard
# ============================================================

class WizardDialog(_Dialog):
    """Install Ollama, open it to the LAN, pull a model, fix the link.

    ``on_finished(ok)`` runs on the GUI thread when the run ends.
    """

    def __init__(self, node, actions, on_finished: Callable[[bool], None],
                 parent: Optional[QWidget] = None):
        super().__init__(f"Pi Setup Wizard — {node.name}", parent)
        self.node = node
        self.actions = actions
        self.on_finished = on_finished
        self._busy = False
        self.resize(680, 600)

        outer = QVBoxLayout(self)
        outer.addWidget(_heading(f"Setting up:  {node.name}  "
                                 f"({node.username}@{node.host})"))
        outer.addWidget(_muted(
            "This wizard will: check OS, install Ollama, configure it to "
            "accept remote connections, pull your chosen model, and fix "
            "ethernet stability.", self._tokens))
        form = QFormLayout()
        model_row = QHBoxLayout()
        self.model = QLineEdit(node.model or apoth_core.DEFAULT_MODEL)
        model_row.addWidget(self.model, 1)
        self.suggest = QComboBox()
        self.suggest.addItem("Hardware suggestions ▾")
        self.suggest.addItems(apoth_core.PI_MODELS)
        self.suggest.currentTextChanged.connect(self._on_suggest)
        model_row.addWidget(self.suggest)
        form.addRow("Model:", model_row)
        self.desktop_ip = QLineEdit(apoth_core.desktop_ip())
        form.addRow("Your desktop IP:", self.desktop_ip)
        form.addRow("", _muted("The Pi pings this every 5 min to keep the "
                               "ethernet link alive.", self._tokens))
        self.password = _password_field()
        self.password.setPlaceholderText("blank = use stored")
        form.addRow("SSH password override:", self.password)
        outer.addLayout(form)
        outer.addWidget(QLabel("Progress:"))
        self.progress = LogView(self._tokens)
        outer.addWidget(self.progress, 1)
        row = QHBoxLayout()
        self.run_btn = self._button(row, "▶  Run Full Setup", self.on_run)
        row.addStretch(1)
        self._button(row, "Close", self.reject)
        outer.addLayout(row)

    def _on_suggest(self, pi_model: str) -> None:
        recs = apoth_core.recommendations(pi_model)
        if recs:
            self.model.setText(recs[0])

    def _prog(self, msg: str, error: bool = False) -> None:
        self.progress.append_tagged(msg, apoth_core.wizard_tag(msg, error))

    def on_run(self) -> None:
        model = self.model.text().strip()
        if not model:
            self._prog("✗ Enter a model name.", True)
            return
        if self._busy:
            return
        desktop = self.desktop_ip.text().strip()
        password = self.password.text().strip() or None
        self._busy = True
        self.run_btn.setEnabled(False)
        self.progress.clear()
        name = self.node.name

        def work() -> None:
            ok = False
            try:
                ok, _msg = self.actions.provision(
                    name, model, desktop, password,
                    lambda m, e: self._to_ui(self._prog, m, e))
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self._prog, f"✗ Setup error: {exc}", True)
            finally:
                self._to_ui(self._finished, ok)

        threading.Thread(target=work, name="apoth-wizard",
                         daemon=True).start()

    def _finished(self, ok: bool) -> None:
        self._busy = False
        self.run_btn.setEnabled(True)
        self.on_finished(ok)


# ============================================================
# Register with Council
# ============================================================

class RegisterDialog(_Dialog):
    def __init__(self, url: str, emit: Callable[[str, bool], None],
                 bat_path: Optional[Path] = None,
                 parent: Optional[QWidget] = None):
        super().__init__("Register Pi with Council", parent)
        self.url = url
        self.emit = emit
        self.bat_path = bat_path
        self.resize(580, 300)

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("Add this to your launch_council.bat:"))
        line = QLineEdit(f"set COUNCIL_PI_HOSTS={url}")
        line.setReadOnly(True)
        line.setStyleSheet(f"color: {self._tokens['success']}; "
                           "font-family: Consolas, monospace;")
        outer.addWidget(line)
        outer.addWidget(_muted(
            "Multiple Pis? Comma-separate: set COUNCIL_PI_HOSTS="
            "http://192.168.1.50:11434,http://192.168.1.51:11434",
            self._tokens))
        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        row = QHBoxLayout()
        if bat_path is not None:
            self.status.setText(f"Found: {bat_path}")
            self.write_btn = self._button(row, f"Write to {bat_path.name}",
                                          self.on_write)
        else:
            self.write_btn = None
            self.status.setText("launch_council.bat was not found in the "
                                "expected locations. Add the line above "
                                "by hand.")
        row.addStretch(1)
        self._button(row, "Close", self.reject)
        outer.addLayout(row)

    def on_write(self) -> None:
        try:
            text = self.bat_path.read_text(encoding="utf-8")
            patched = apoth_core.patch_launch_bat(text, self.url)
            if patched == text and "COUNCIL_PI_HOSTS" not in text:
                # Tk wrote the file back unchanged and reported "Updated".
                self.status.setText(
                    f"{self.bat_path.name} has no COUNCIL_PI_HOSTS or "
                    "OLLAMA_MAX_LOADED_MODELS line to anchor on — add the "
                    "line above by hand.")
                return
            self.bat_path.write_text(patched, encoding="utf-8")
        except OSError as exc:
            self.status.setText(f"Could not update the .bat: {exc}")
            return
        self.emit(f"✓ Updated {self.bat_path.name} with COUNCIL_PI_HOSTS",
                  False)
        self.accept()


# ============================================================
# Static IP
# ============================================================

class StaticIpDialog(_Dialog):
    """Collects an address and a gateway; ``on_apply(ip, gateway)`` runs it."""

    def __init__(self, node, on_apply: Callable[[str, str], None],
                 parent: Optional[QWidget] = None):
        super().__init__(f"Set Static IP — {node.name}", parent)
        self.on_apply = on_apply
        outer = QVBoxLayout(self)
        outer.addWidget(_muted(
            "Assigning a static IP stops the Pi's address changing on "
            "reboot, the most common cause of lost connections.",
            self._tokens))
        form = QFormLayout()
        self.ip = QLineEdit(node.host)
        self.gateway = QLineEdit("192.168.1.1")
        form.addRow("Static IP to assign:", self.ip)
        form.addRow("Gateway:", self.gateway)
        outer.addLayout(form)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {self._tokens['error']};")
        outer.addWidget(self.error)
        row = QHBoxLayout()
        self._button(row, "Apply Static IP", self.on_accept)
        row.addStretch(1)
        self._button(row, "Cancel", self.reject)
        outer.addLayout(row)

    def on_accept(self) -> None:
        ip, gateway = self.ip.text().strip(), self.gateway.text().strip()
        if not ip or not gateway:
            self.error.setText("Fill in both fields.")
            return
        self.accept()
        self.on_apply(ip, gateway)
