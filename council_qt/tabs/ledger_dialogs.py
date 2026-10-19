"""
council_qt.tabs.ledger_dialogs — dialogs the Ledger tab opens.

Kept apart from the tab module because the all-tabs harness resolves every
``self.x`` reference against the TAB, and a dialog's own ``self.accept``
is not the tab's. The pattern mirrors ``connections_dialogs.py``.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

from council_core import ollama_install


class _Signals(QObject):
    progress = Signal(object)
    done = Signal(object, object)       # (final PullProgress or None, error-text or '')


class InstallVisionModelDialog(QDialog):
    """Pick a US-origin vision model from the catalog and install it.

    The dialog never talks to Ollama on the GUI thread — the ``installed``
    probe and the pull both run on a worker, and the progress callback hops
    back through a Qt signal.

    ``on_installed`` is called with the chosen model's name once the pull
    completes successfully (the Ledger tab reacts by refreshing its reader
    dropdown)."""

    def __init__(self, on_installed: Optional[Callable[[str], None]] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Install an image reader")
        self.resize(560, 420)
        self._on_installed = on_installed
        self._worker: Optional[threading.Thread] = None
        self._installed_names: List[str] = []
        self.signals = _Signals()
        self.signals.progress.connect(self._on_progress)
        self.signals.done.connect(self._on_done)

        root = QVBoxLayout(self)
        intro = QLabel(
            "Choose a US-origin vision model to install. The Council never "
            "downloads a model on its own; nothing happens until you click "
            "Install.")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.currentItemChanged.connect(self._refresh_details)
        root.addWidget(self.list, 1)

        self.details = QLabel("")
        self.details.setWordWrap(True)
        root.addWidget(self.details)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        progress_row.addWidget(self.progress, 1)
        self.progress_text = QLabel("")
        progress_row.addWidget(self.progress_text)
        root.addLayout(progress_row)

        buttons_row = QHBoxLayout()
        self.install_btn = QPushButton("Install")
        self.install_btn.setEnabled(False)
        self.install_btn.clicked.connect(self.on_install)
        buttons_row.addWidget(self.install_btn)
        buttons_row.addStretch(1)
        root.addLayout(buttons_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

        self._load_catalog()

    # ────────────────────────────────────────────────────────────── catalog

    def _load_catalog(self) -> None:
        try:
            self._installed_names = ollama_install.installed_models()
        except Exception:                                   # noqa: BLE001
            self._installed_names = []
        self.list.clear()
        for m in ollama_install.catalog():
            tag = ollama_install.is_installed(m.name, self._installed_names)
            star = " · recommended" if m.recommended else ""
            marker = "✓ installed" if tag else "not installed"
            item = QListWidgetItem(
                f"{m.label}{star}\n   {marker} · {m.download_gb:g} GB · "
                f"~{m.approx_vram_gb:g} GB VRAM")
            item.setData(Qt.ItemDataRole.UserRole, m)
            self.list.addItem(item)
        self._refresh_details()

    def _refresh_details(self, *_args) -> None:
        model = self._current_model()
        if model is None:
            self.details.setText("")
            self.install_btn.setEnabled(False)
            return
        installed = ollama_install.is_installed(model.name, self._installed_names)
        extra = " (already installed)" if installed else ""
        self.details.setText(f"{model.name}{extra} — {model.description}")
        self.install_btn.setEnabled(
            not installed and self._worker is None)
        self.install_btn.setText("Install" if not installed else "Already installed")

    def _current_model(self) -> Optional[ollama_install.VisionModel]:
        item = self.list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    # ────────────────────────────────────────────────────────────── install

    def on_install(self) -> None:
        model = self._current_model()
        if model is None or self._worker is not None:
            return
        name = model.name
        self.install_btn.setEnabled(False)
        self.install_btn.setText("Installing…")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)                 # busy until a layer sends total
        self.progress_text.setText("asking Ollama to pull the model…")

        def emit_progress(p):
            self.signals.progress.emit(p)

        def work():
            final = None
            err = ""
            try:
                final = ollama_install.pull(name, on_progress=emit_progress,
                                            timeout=1800)
            except Exception as exc:                        # noqa: BLE001
                err = str(exc)
            self.signals.done.emit(final, err)

        # Prefix must match a TAB_WORKERS entry so CouncilWindow's close
        # drain picks it up — see tests/test_council_qt_foundation.py.
        self._worker = threading.Thread(target=work, name="ledger-ollama-pull",
                                        daemon=True)
        self._worker.start()

    def _on_progress(self, p) -> None:
        if p.fraction is not None:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(100 * p.fraction))
        else:
            self.progress.setRange(0, 0)
        text = p.status or ""
        if p.total:
            mb_done = p.completed / (1024 * 1024)
            mb_total = p.total / (1024 * 1024)
            text += f"   {mb_done:.0f} / {mb_total:.0f} MB"
        self.progress_text.setText(text)

    def _on_done(self, final, err) -> None:
        self._worker = None
        self.progress.setVisible(False)
        if err:
            self.progress_text.setText(f"install failed: {err}")
            self.install_btn.setEnabled(True)
            self.install_btn.setText("Install")
            return
        model = self._current_model()
        if model is None:
            return
        if getattr(final, "status", "").lower() != "success":
            self.progress_text.setText(
                f"install ended with status: {getattr(final, 'status', '?')}")
            self.install_btn.setEnabled(True)
            self.install_btn.setText("Install")
            return
        self.progress_text.setText(f"installed {model.name}.")
        # Refresh install state across the list and tell the Ledger tab.
        self._load_catalog()
        for i in range(self.list.count()):
            item = self.list.item(i)
            m = item.data(Qt.ItemDataRole.UserRole)
            if m is not None and m.name == model.name:
                self.list.setCurrentItem(item)
                break
        if self._on_installed is not None:
            try:
                self._on_installed(model.name)
            except Exception as exc:                        # noqa: BLE001
                print(f"[ledger_dialogs] on_installed hook raised: {exc!r}",
                      flush=True)
