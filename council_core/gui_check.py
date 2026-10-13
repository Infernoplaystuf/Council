"""
council_core.gui_check — does the widget a coding job changed still build,
and what does it show?

Tests can pass while a tab fails the moment it opens. This builds the
widget offscreen in a child process (council_qt.gui_check_child, under
council_core.child_proc's time limit and memory cap) from the job's
worktree and returns:

    ok, errors      the traceback of anything that raised while it was
                    built and shown, or "" — and Qt warnings
    tree            each widget's class, name, text, enabled/visible, size
    screenshot      a PNG of it, for the user (or a vision model)

`summary()` is what a coding agent reads: the errors first, then the
widget tree as indented text, cut to a budget.
"""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_S = 90
MEMORY_MB = 3072


@dataclass
class GuiResult:
    module: str
    cls: str
    ok: bool = False
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    tree: List[Dict[str, Any]] = field(default_factory=list)
    screenshot: str = ""

    def summary(self, max_chars: int = 5000) -> str:
        head = (f"GUI CHECK {self.module}.{self.cls}: "
                + ("built and shown without errors" if self.ok
                   else "FAILED"))
        parts = [head]
        for e in self.errors:
            parts.append("ERROR:\n" + e.strip()[-1500:])
        if self.warnings:
            parts.append("Qt warnings: " + "; ".join(self.warnings[:8]))
        if self.tree:
            lines = []
            for w in self.tree:
                flags = ("" if w["enabled"] else " disabled") + \
                    ("" if w["visible"] else " hidden")
                label = f" '{w['text']}'" if w["text"] else ""
                name = f" #{w['name']}" if w["name"] else ""
                lines.append("  " * w["depth"] + f"{w['class']}{name}{label}"
                             f"{flags} {w['size'][0]}x{w['size'][1]}")
            text = "\n".join(lines)
            if len(text) > max_chars:
                text = text[:max_chars] + "\n… (more widgets)"
            parts.append("WIDGETS:\n" + text)
        if self.screenshot:
            parts.append(f"Screenshot: {self.screenshot}")
        return "\n\n".join(parts)


def check(root: Path, module: str, cls: str,
          kwargs: Optional[Dict[str, Any]] = None, *,
          out_dir: Optional[Path] = None,
          timeout: float = TIMEOUT_S, runner: Any = None) -> GuiResult:
    """Build `module`.`cls`(**kwargs) offscreen from `root`. Never raises."""
    from . import child_proc
    runner = runner or child_proc.run
    res = GuiResult(module, cls)
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(
        prefix="council-gui-"))
    out.mkdir(parents=True, exist_ok=True)
    env = child_proc.child_env({"QT_QPA_PLATFORM": "offscreen",
                                "COUNCIL_NO_DIALOGS": "1",
                                "PYTHONDONTWRITEBYTECODE": "1",
                                "PYTHONPATH": str(APP_ROOT)})
    run = runner([sys.executable, "-m", "council_qt.gui_check_child",
                  str(root), module, cls, json.dumps(kwargs or {}), str(out)],
                 cwd=str(APP_ROOT), env=env, timeout=timeout,
                 memory_limit_mb=MEMORY_MB)
    verdict_file = out / "verdict.json"
    if getattr(run, "timed_out", False):
        res.errors.append(f"the widget did not finish building within "
                          f"{timeout:.0f} s (an endless loop or a blocking "
                          "call in its constructor?)")
        return res
    if not verdict_file.exists():
        res.errors.append("the check process ended without a verdict: "
                          + (getattr(run, "stderr", "") or
                             getattr(run, "error", "") or "")[-1500:])
        return res
    try:
        v = json.loads(verdict_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        res.errors.append(f"unreadable verdict: {exc}")
        return res
    res.ok = bool(v.get("ok"))
    res.errors = list(v.get("errors") or [])
    res.warnings = list(v.get("warnings") or [])
    res.tree = list(v.get("tree") or [])
    res.screenshot = str(v.get("screenshot") or "")
    return res


__all__ = ["GuiResult", "check"]
