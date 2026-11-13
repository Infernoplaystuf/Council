"""
council_qt.widgets.add_pc — "Add a PC…" and "Models on…", opened from
Machines & roles (council_core.node_setup does the work; no Qt there).

ADD A PC
  1. A name for the other PC, and which of THIS PC's addresses it should
     answer (picked for you when you type its address).
  2. "Save setup file…": a .bat to run on the other PC as administrator —
     Ollama listening on the network, one firewall rule letting only this PC
     in, a restart, an offer to download each model, its own addresses.
  3. The other PC's address, then Test: does it answer, which models it has.
  4. "Add to the list": the machine goes into Machines & roles — enabled
     only when the test passed — and nothing is saved until Save there.

MODELS ON A MACHINE
Lists the models the other PC has (marking the US-origin ones), downloads
one US-origin model when the user presses Download (the other PC downloads
it and needs internet), and removes one after the user confirms.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout)

from council_core import local_models as lm, node_routing as nr, node_setup as ns

from .. import dialogs, theme
from ..view import ViewHelpers


class AddPcDialog(ViewHelpers, QDialog):
    """Returns (via .result_node) the machine to add, or None."""

    def __init__(self, parent=None, *, vault_dir: Path,
                 models: Sequence[str] = (), taken: Sequence[str] = (),
                 addresses: Optional[Sequence[str]] = None,
                 probe: Callable[[str], ns.Probe] = ns.probe):
        super().__init__(parent)
        self.vault_dir = Path(vault_dir)
        self._taken = set(taken)
        self._probe = probe
        self._probed: Optional[ns.Probe] = None
        self._tested_url = ""
        self.result_node: Optional[nr.Node] = None
        self.saved_path: Optional[Path] = None
        self._tokens = theme.tokens("dark")
        self.setWindowTitle("Add a PC")
        self.resize(640, 520)
        self._build(list(models),
                    list(addresses if addresses is not None
                         else ns.this_pc_addresses()))

    def _build(self, models: List[str], addresses: List[str]) -> None:
        v = QVBoxLayout(self)
        intro = QLabel(
            "Use another Windows PC's Ollama for the Council. Step 1 makes a "
            "setup file to run on that PC as administrator: Ollama listens on "
            "the network, and a firewall rule lets only THIS PC in. Step 2 "
            "tests it and adds it to the list.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        v.addWidget(intro)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("gaming-pc")
        form.addRow("Name", self.name_edit)
        self.this_pc = QComboBox()
        self.this_pc.setEditable(True)
        self.this_pc.addItems(addresses)
        form.addRow("This PC's address", self.this_pc)
        self.models_edit = QLineEdit(", ".join(models))
        self.models_edit.setPlaceholderText("gemma3:12b, llama3.1:8b")
        self.models_edit.setToolTip(
            "Models the setup file offers to download there (US-origin only). "
            "Each role bound to that PC runs its own model there.")
        form.addRow("Models it needs", self.models_edit)
        v.addLayout(form)
        row = QHBoxLayout()
        self._button(row, "1. Save setup file…", self.save_setup)
        row.addStretch(1)
        v.addLayout(row)

        form2 = QFormLayout()
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("192.168.1.31 (the setup file prints it)")
        self.address_edit.editingFinished.connect(self._pick_this_pc)
        form2.addRow("Its address", self.address_edit)
        v.addLayout(form2)
        row2 = QHBoxLayout()
        self.test_btn = self._button(row2, "2. Test", self.test)
        self.add_btn = self._button(row2, "3. Add to the list", self.add)
        row2.addStretch(1)
        v.addLayout(row2)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        v.addWidget(close)

    # ------------------------------------------------------------------
    def _models(self) -> List[str]:
        return [m.strip() for m in self.models_edit.text().split(",") if m.strip()]

    def _name(self) -> str:
        return self.name_edit.text().strip()

    def _pick_this_pc(self) -> None:
        """Typing the other PC's address picks this PC's address on the way
        there (several network cards: the one that reaches it)."""
        ip = ns.route_address(self.address_edit.text())
        if ip:
            if self.this_pc.findText(ip) < 0:
                self.this_pc.addItem(ip)
            self.this_pc.setCurrentText(ip)

    def setup_text(self) -> str:
        return ns.setup_script(self._name(), self.this_pc.currentText(),
                               self._models())

    def save_setup(self, target: Optional[str] = None) -> Optional[Path]:
        try:
            text = self.setup_text()
        except ns.SetupError as exc:
            self.status.setText(f"Not saved: {exc}")
            return None
        default = ns.setup_path(self.vault_dir, self._name())
        if target is None and not dialogs.disabled():
            target = dialogs.asksaveasfilename(
                title="Save the setup file", initialdir=str(default.parent),
                initialfile=default.name, defaultextension=".bat",
                filetypes=[("Windows batch file", "*.bat")], parent=self)
            if not target:
                return None
        path = ns.write_setup_script(Path(target) if target else default, text)
        self.saved_path = path
        self.status.setText(
            f"Saved {path}.\nCopy it to {self._name()} (USB stick or a shared "
            "folder), right-click it there and choose Run as administrator. "
            "It prints that PC's address: type it below and press Test.")
        return path

    def test(self) -> None:
        try:
            url = ns.url_for(self.address_edit.text())
        except ns.SetupError as exc:
            self.status.setText(str(exc))
            return
        self.test_btn.setEnabled(False)
        self._probed, self._tested_url = None, url
        self.status.setText(f"Asking {url}…")
        wanted = self._models()

        def work() -> None:
            found = self._probe(url)
            self._to_ui(self._tested, found, wanted)

        threading.Thread(target=work, name="add-pc-test", daemon=True).start()

    def _tested(self, found: ns.Probe, wanted: Sequence[str]) -> None:
        self._probed = found
        self.test_btn.setEnabled(True)
        self.status.setText("\n".join(ns.check(found, wanted)))

    def add(self) -> Optional[nr.Node]:
        name = self._name()
        if not nr._NAME.match(name):
            self.status.setText("Give the PC a name first.")
            return None
        if name in self._taken:
            self.status.setText(f"There is already a machine called {name}.")
            return None
        try:
            url = ns.url_for(self.address_edit.text())
        except ns.SetupError as exc:
            self.status.setText(str(exc))
            return None
        ok = bool(self._probed and self._probed.reachable
                  and nr.normalise_url(self._tested_url) == nr.normalise_url(url))
        shared = next((m for m in self._models() if not nr.check_model(m)), "")
        self.result_node = nr.Node(name, url, enabled=ok, parallel=1, model=shared)
        self.accept()
        return self.result_node


class MachineModelsDialog(ViewHelpers, QDialog):
    """The models on one machine: list, download one, remove one."""

    def __init__(self, parent=None, *, name: str, url: str,
                 probe: Callable[[str], ns.Probe] = ns.probe,
                 pull: Callable[..., str] = ns.pull_model,
                 remove: Callable[..., None] = ns.remove_model):
        super().__init__(parent)
        self.name, self.url = name, url
        self._probe, self._pull, self._remove = probe, pull, remove
        self._busy = False
        self._note = ""
        self._stop = threading.Event()
        self._tokens = theme.tokens("dark")
        self.setWindowTitle(f"Models on {name}")
        self.resize(560, 480)
        v = QVBoxLayout(self)
        self.head = QLabel(f"{name} — {url}")
        v.addWidget(self.head)
        self.list = QListWidget()
        v.addWidget(self.list, 1)
        row = QHBoxLayout()
        self._button(row, "Refresh", self.refresh)
        self.remove_btn = self._button(row, "Remove selected…", self.remove_selected)
        row.addStretch(1)
        v.addLayout(row)
        row2 = QHBoxLayout()
        self.pull_edit = QLineEdit()
        self.pull_edit.setPlaceholderText("gemma3:12b (US-origin only)")
        row2.addWidget(self.pull_edit, 1)
        self.pull_btn = self._button(row2, "Download on that PC", self.download)
        v.addLayout(row2)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        v.addWidget(close)
        self.refresh()

    def reject(self) -> None:
        self._stop.set()
        super().reject()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for b in (self.remove_btn, self.pull_btn):
            b.setEnabled(not busy)

    def refresh(self) -> None:
        self.status.setText(f"Asking {self.url}…")

        def work() -> None:
            self._to_ui(self._listed, self._probe(self.url))

        threading.Thread(target=work, name="machine-models", daemon=True).start()

    def _listed(self, found: ns.Probe) -> None:
        self.list.clear()
        if not found.reachable:
            note, self._note = self._note, ""
            self.status.setText((note + "\n" if note else "") + found.error)
            return
        for m in found.models:
            origin = lm.maker_and_origin(m)[1]
            item = QListWidgetItem(m if origin == "US" else f"{m}   (not US-origin)")
            item.setData(256, m)
            self.list.addItem(item)
        note, self._note = self._note, ""
        self.status.setText((note + "\n" if note else "") +
                            f"Ollama {found.version or '?'}, {len(found.models)} "
                            f"model(s), {found.latency_ms} ms.")

    def download(self) -> None:
        name = self.pull_edit.text().strip()
        why = nr.check_model(name) if name else "type a model name"
        if why:
            self.status.setText(f"Not downloaded: {why}.")
            return
        self._set_busy(True)
        self.status.setText(f"{self.name} is downloading {name}…")

        def progress(status: str, frac: Optional[float]) -> None:
            text = f"{name}: {status}" + (f" {frac:.0%}" if frac is not None else "")
            self._to_ui(self.status.setText, text)

        def work() -> None:
            try:
                last = self._pull(self.url, name, on_progress=progress,
                                  should_stop=self._stop.is_set)
                msg = f"{name}: {last}."
            except ns.SetupError as exc:
                msg = f"Not downloaded: {exc}"
            self._to_ui(self._pulled, msg)

        threading.Thread(target=work, name="machine-pull", daemon=True).start()

    def _pulled(self, msg: str) -> None:
        self._set_busy(False)
        self._note = msg
        self.refresh()

    def remove_selected(self, confirm: Optional[Callable[[str], bool]] = None) -> bool:
        item = self.list.currentItem()
        if item is None:
            self.status.setText("Pick a model first.")
            return False
        name = item.data(256)
        ask = confirm or (lambda text: dialogs.askyesno(
            "Remove a model", text, parent=self))
        if not ask(f"Remove {name} from {self.name}? It is deleted on that PC "
                   "and has to be downloaded again to use it."):
            return False
        try:
            self._remove(self.url, name)
        except ns.SetupError as exc:
            self.status.setText(f"Not removed: {exc}")
            return False
        self._note = f"Removed {name} from {self.name}."
        self.refresh()
        return True


__all__ = ["AddPcDialog", "MachineModelsDialog"]
