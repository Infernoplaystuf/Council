"""
council_qt.widgets.skipped_files — "Retry skipped files (N)".

The files council_core.file_guard skips - too slow or too big to read, or
they stalled or crashed the app before - are skipped by every tab until they
change. This
button, beside the Ledger's Extract and the Connections graph's Rebuild,
says how many there are (which, in its tooltip) and takes them off the list:
the next run reads them again.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtWidgets import QPushButton

from council_core import file_guard

#: files named in the tooltip
SHOWN = 15


class RetrySkippedButton(QPushButton):
    """Off (and saying 0) while nothing is skipped. ``on_cleared(text)``
    gets what to tell the user after a click."""

    def __init__(self, on_cleared: Optional[Callable[[str], None]] = None, parent=None):
        super().__init__(parent)
        self.on_cleared = on_cleared
        self.clicked.connect(self.retry)
        self.refresh()

    def refresh(self) -> int:
        """Count the list again; the number of files skipped."""
        try:
            skipped = file_guard.entries()
        except Exception:                                 # noqa: BLE001
            skipped = []
        n = len(skipped)
        self.setText(f"Retry skipped files ({n})")
        self.setEnabled(n > 0)
        if n:
            lines = [f"{Path(e.path).name} - {e.reason}" + (f" ({e.tab})" if e.tab else "")
                     for e in skipped[:SHOWN]]
            if n > SHOWN:
                lines.append(f"…and {n - SHOWN} more")
            self.setToolTip(
                "These files were skipped because reading them took too long or too "
                "much memory, or they stalled or crashed the Council before. Every tab skips them until they "
                "change. Click to read them again on the next run:\n" + "\n".join(lines))
        else:
            self.setToolTip("No file is skipped. A file that takes longer than "
                            f"{file_guard.time_limit():g} s or more than "
                            f"{file_guard.memory_limit_mb():,} MB of memory to read, or "
                            "stalls the Council, is skipped from then on and listed here.")
        return n

    def retry(self) -> None:
        n = file_guard.clear()
        self.refresh()
        if self.on_cleared is not None:
            self.on_cleared(f"{n} skipped file(s) will be read again on the next run."
                            if n else "No file was skipped.")
