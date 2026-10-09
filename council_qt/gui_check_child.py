"""
council_qt.gui_check_child — build one widget offscreen and report it.

    python -m council_qt.gui_check_child ROOT MODULE CLASS KWARGS_JSON OUT_DIR

Run by council_core.gui_check in a child process with a time limit, from
the project's folder (a job's worktree). It:

  * sets QT_QPA_PLATFORM=offscreen and COUNCIL_NO_DIALOGS=1 — no window on
    the user's screen, no modal dialog to hang on;
  * imports MODULE from ROOT, builds CLASS(**KWARGS), shows it at a normal
    size and lets the event loop run briefly, so deferred construction
    (QTimer.singleShot) happens;
  * records every exception raised meanwhile (sys.excepthook — Qt slots
    report there) and every Qt warning;
  * writes OUT_DIR/verdict.json: ok, errors, warnings, and the widget
    tree — each widget's class, object name, text, enabled/visible and
    size, so a text-only model can check "is there a Save button, and is
    it enabled" — and OUT_DIR/screenshot.png for a person (or a vision
    model) to look at.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

MAX_WIDGETS = 400
#: Warnings the offscreen platform itself prints for every window.
OFFSCREEN_NOISE = ("propagateSizeHints",)


def _text_of(w) -> str:
    for attr in ("text", "title", "windowTitle", "placeholderText",
                 "currentText", "toolTip"):
        fn = getattr(w, attr, None)
        if callable(fn):
            try:
                v = fn()
            except Exception:                             # noqa: BLE001
                continue
            if isinstance(v, str) and v.strip():
                return v.strip()[:80]
    return ""


def tree(root) -> list:
    from PySide6.QtWidgets import QWidget
    out = []

    def walk(w, depth):
        if len(out) >= MAX_WIDGETS:
            return
        g = w.geometry()
        out.append({"depth": depth, "class": type(w).__name__,
                    "name": w.objectName(), "text": _text_of(w),
                    "enabled": w.isEnabled(), "visible": w.isVisible(),
                    "size": [g.width(), g.height()]})
        for c in w.children():
            if isinstance(c, QWidget):
                walk(c, depth + 1)
    walk(root, 0)
    return out


def main(argv) -> int:
    root, module, cls, kwargs_json, out_dir = argv[1:6]
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["COUNCIL_NO_DIALOGS"] = "1"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    verdict = {"ok": False, "errors": [], "warnings": [], "tree": [],
               "screenshot": ""}

    def finish(code=0):
        (out / "verdict.json").write_text(json.dumps(verdict),
                                          encoding="utf-8")
        sys.stdout.flush()
        os._exit(code)

    sys.path.insert(0, root)
    os.chdir(root)
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
        from PySide6.QtWidgets import QApplication
    except Exception as exc:                              # noqa: BLE001
        verdict["errors"].append(f"PySide6 is not available: {exc}")
        finish()

    def on_message(kind, _ctx, msg):
        if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg,
                    QtMsgType.QtFatalMsg) and not any(
                        n in str(msg) for n in OFFSCREEN_NOISE):
            verdict["warnings"].append(str(msg)[:300])
    qInstallMessageHandler(on_message)

    def on_exc(et, ev, tb):
        verdict["errors"].append("".join(
            traceback.format_exception(et, ev, tb))[-2000:])
    sys.excepthook = on_exc

    app = QApplication.instance() or QApplication([])
    try:
        import importlib
        mod = importlib.import_module(module)
        klass = getattr(mod, cls)
        widget = klass(**json.loads(kwargs_json or "{}"))
    except Exception:                                     # noqa: BLE001
        verdict["errors"].append(traceback.format_exc()[-3000:])
        finish()
    try:
        widget.resize(1100, 750)
        widget.show()
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        verdict["tree"] = tree(widget)
        shot = out / "screenshot.png"
        if widget.grab().save(str(shot), "PNG"):
            verdict["screenshot"] = str(shot)
    except Exception:                                     # noqa: BLE001
        verdict["errors"].append(traceback.format_exc()[-3000:])
    verdict["ok"] = not verdict["errors"]
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
