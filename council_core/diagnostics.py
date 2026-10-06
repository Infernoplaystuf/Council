"""
council_core.diagnostics — the Diagnostics report, without a toolkit.

The questions this answers — which Python, which toolkit, where the vault is,
which optional features are missing and the pip line for each — are the first
ones asked when something is wrong on a user's machine, and the answer is
pasted into a bug report. So it is text, built here, and a view only shows it.

WHAT THE QT TAB WAS MISSING
  * The dependency report. Tk's Diagnostics tab is mostly dependency_check's
    list of optional features, what each unlocks and how to install it; the
    Qt tab listed five package versions. Measured on the dev PC: 11 optional
    features missing (8 counting SQLAlchemy and its three drivers as one) and
    not one of them visible in Qt.
  * The vault. It read the path from the Tk engine module, which the Qt app
    never imports, so it always said "engine not loaded in this process" —
    the one line a support conversation needs. council_core.paths gives the
    same answer either way.
  * Copy. Tk has "Copy report"; a report nobody can paste is a screenshot.

NOTHING HERE IMPORTS THE ENGINE
It costs seconds and loads CUDA DLLs, and the report runs on a worker. What
the engine knows (the context window) is included only if it is already
loaded — see dependency_check.system_summary.
"""
from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

#: Package versions worth a line of their own: the ones whose version is the
#: first question when a chart or a model load fails.
PACKAGES = ("numpy", "PIL", "matplotlib", "pandas", "llama_cpp")


@dataclass
class Report:
    text: str
    available: int = 0
    missing: int = 0

    @property
    def summary(self) -> str:
        """Tk's status line, word for word."""
        return f"✓ {self.available} available  ·  ✗ {self.missing} missing"


def _versions() -> list:
    lines = []
    import importlib.util
    for name in PACKAGES:
        try:
            if importlib.util.find_spec(name) is None:
                lines.append(f"{name:<17}: not installed")
                continue
            module = __import__(name)
            version = getattr(module, "__version__", "present")
            lines.append(f"{name:<17}: {version}")
        except Exception:                                 # noqa: BLE001
            lines.append(f"{name:<17}: not installed")
    return lines


def _vault_lines() -> list:
    from . import paths
    lines = []
    try:
        vault = paths.vault_dir()
        lines.append(f"vault            : {vault}")
        lines.append(f"vault exists     : {Path(vault).exists()}")
        lines.append(f"app folder       : {paths.app_dir()}")
    except Exception as exc:                              # noqa: BLE001
        lines.append(f"vault            : could not be resolved ({exc!r})")
    return lines


def gather(toolkit: str = "PySide6 (Qt)",
           toolkit_lines: Sequence[str] = ()) -> Report:
    """The whole report. Slow-ish (imports, disk probes): call it on a
    worker. Never raises — a diagnostics panel that fails is the worst kind.

    ``toolkit_lines`` are the front end's own version lines (PySide6 / Qt):
    this package imports no toolkit, so the view that has one says which."""
    lines = [
        "Data's Inferno — diagnostics",
        "",
        f"toolkit          : {toolkit}",
        f"python           : {sys.version.split()[0]} "
        f"({platform.architecture()[0]})",
        f"executable       : {sys.executable}",
    ]
    lines += list(toolkit_lines)
    lines += _versions()
    lines.append("")
    lines += _vault_lines()
    lines.append("")
    available = missing = 0
    try:
        import dependency_check
        statuses = dependency_check.check_all()
        available = sum(1 for s in statuses if s.ok)
        missing = len(statuses) - available
        lines.append(dependency_check.render_as_text(statuses))
    except Exception as exc:                              # noqa: BLE001
        lines.append(f"dependency check unavailable: {exc!r}")
    return Report("\n".join(lines), available=available, missing=missing)
