"""
council_qt.tabs.diagnostics — the first real tab, and the foundation's proof.

Chosen to go first because it is the cheapest tab in the app (26 toolkit-bound
lines in the Tk build) while still exercising everything the frame provides: a
scrolling report, a button that does work off the UI thread, and a status line.
If this tab is right, the frame is right.

It also earns its place permanently: the questions it answers — which Python,
which toolkit, where the vault is, which optional features are missing and how
to install them — are the first questions asked when something is wrong on a
user's machine. The report itself is council_core.diagnostics; this file shows
it, counts it, and copies it.

WHAT IT USED TO MISS
Tk's tab is the dependency report plus "Copy report". This one listed five
package versions, had no Copy, and named the vault as "engine not loaded in
this process" — it read the path from the Tk engine module, which the Qt app
never imports. All three now come from council_core.diagnostics.
"""
from __future__ import annotations

import threading

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QVBoxLayout,
                               QWidget)

from .. import theme
from ..view import amp


def _toolkit_lines() -> list:
    """This front end's toolkit versions — here, because council_core
    imports no toolkit."""
    try:
        import PySide6
        from PySide6 import QtCore
        return [f"PySide6          : {PySide6.__version__}",
                f"Qt               : {QtCore.qVersion()}"]
    except Exception as exc:                              # noqa: BLE001
        return [f"PySide6          : unavailable ({exc!r})"]


def build_diagnostics(window) -> QWidget:
    """The tab. ``window`` is the CouncilWindow, for the bridge and status."""
    tokens = theme.tokens("dark")
    page = QWidget()
    layout = QVBoxLayout(page)

    row = QHBoxLayout()
    refresh = QPushButton(amp("⟳ Re-check"), page)
    row.addWidget(refresh)
    copy = QPushButton(amp("📋 Copy report"), page)
    row.addWidget(copy)
    row.addStretch(1)
    status = QLabel("", page)
    status.setStyleSheet(f"color: {tokens['muted_fg']};")
    row.addWidget(status)
    layout.addLayout(row)

    output = QPlainTextEdit(page)
    output.setReadOnly(True)
    output.setFont(QFont("Consolas", 10))
    layout.addWidget(output, 1)

    def run() -> None:
        """Gather on a worker, deliver through the bridge.

        Deliberately written the way every other tab will be: the worker never
        touches a widget, and the result comes back via call_on_ui. Under Tk
        this idiom was `self.after(0, ...)` from the worker, which Qt would
        silently drop."""
        window.set_status("Gathering diagnostics ...")
        refresh.setEnabled(False)
        status.setText("Checking…")

        def work() -> None:
            from council_core import diagnostics
            report = diagnostics.gather(toolkit_lines=_toolkit_lines())

            def deliver() -> None:
                output.setPlainText(report.text)
                status.setText(report.summary)
                refresh.setEnabled(True)
                window.set_status("Diagnostics ready")

            window.bridge.call_on_ui(deliver)

        threading.Thread(target=work, name="diagnostics", daemon=True).start()

    def on_copy() -> None:
        """The whole report as shown, for a bug report. Tk copies the
        dependency half only; the vault path and versions above it are the
        half a support conversation asks for first."""
        text = output.toPlainText()
        if not text.strip():
            status.setText("Nothing to copy yet.")
            return
        QApplication.clipboard().setText(text)
        status.setText("✓ Copied to clipboard")

    refresh.clicked.connect(run)
    copy.clicked.connect(on_copy)
    run()
    return page
