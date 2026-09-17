"""
council_qt.tabs.diagnostics — the first real tab, and the foundation's proof.

Chosen to go first because it is the cheapest tab in the app (26 toolkit-bound
lines in the Tk build) while still exercising everything the frame provides: a
scrolling report, a button that does work off the UI thread, and a status line.
If this tab is right, the frame is right.

It also earns its place permanently: the questions it answers — which Python,
which toolkit, where the vault is, is PySide6 really the one we think — are the
first questions asked when something is wrong on a user's machine.
"""
from __future__ import annotations

import platform
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import (QHBoxLayout, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)


def _report() -> str:
    """Gathered off the UI thread — some of these touch the disk."""
    lines = [
        "Data's Inferno — diagnostics",
        "",
        f"toolkit          : PySide6 (Qt)",
        f"python           : {sys.version.split()[0]} ({platform.architecture()[0]})",
        f"executable       : {sys.executable}",
        f"platform         : {platform.platform()}",
    ]
    try:
        import PySide6
        from PySide6 import QtCore
        lines.append(f"PySide6          : {PySide6.__version__}")
        lines.append(f"Qt               : {QtCore.qVersion()}")
    except Exception as exc:                            # noqa: BLE001
        lines.append(f"PySide6          : unavailable ({exc!r})")

    for name in ("numpy", "PIL", "matplotlib", "pandas", "llama_cpp"):
        try:
            module = __import__(name)
            version = getattr(module, "__version__", "present")
            lines.append(f"{name:<17}: {version}")
        except Exception:                               # noqa: BLE001
            lines.append(f"{name:<17}: not installed")

    # Deliberately NOT `import council_gui_engine` — that import builds the
    # backend banner and costs ~4 seconds, which is a strange price for a
    # diagnostics panel to charge. If the Tk engine is already loaded in this
    # process, read the vault out of it; otherwise say so and move on.
    engine = sys.modules.get("council_gui_engine")
    vault = getattr(engine, "VAULT_DIR", None) if engine else None
    lines.append("")
    if vault:
        path = Path(str(vault))
        lines.append(f"vault            : {path}")
        lines.append(f"vault exists     : {path.exists()}")
    else:
        lines.append("vault            : engine not loaded in this process")
    return "\n".join(lines)


def build_diagnostics(window) -> QWidget:
    """The tab. ``window`` is the CouncilWindow, for the bridge and status."""
    page = QWidget()
    layout = QVBoxLayout(page)

    output = QPlainTextEdit(page)
    output.setReadOnly(True)
    layout.addWidget(output, 1)

    row = QHBoxLayout()
    refresh = QPushButton("Refresh", page)
    row.addWidget(refresh)
    row.addStretch(1)
    layout.addLayout(row)

    def run() -> None:
        """Gather on a worker, deliver through the bridge.

        Deliberately written the way every other tab will be: the worker never
        touches a widget, and the result comes back via call_on_ui. Under Tk
        this idiom was `self.after(0, ...)` from the worker, which Qt would
        silently drop."""
        window.set_status("Gathering diagnostics ...")
        refresh.setEnabled(False)

        def work() -> None:
            text = _report()

            def deliver() -> None:
                output.setPlainText(text)
                refresh.setEnabled(True)
                window.set_status("Diagnostics ready")

            window.bridge.call_on_ui(deliver)

        threading.Thread(target=work, name="diagnostics", daemon=True).start()

    refresh.clicked.connect(run)
    run()
    return page
