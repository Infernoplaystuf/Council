"""
council_qt.py — the Qt entry point. The Tk one (council_gui_engine.py) is
untouched and stays the shipping app until the port is finished.

    python council_qt.py

Both entry points read the same vault and the same engine, so run ONE at a time.
There is no single-instance guard in this project yet (there never was), and two
copies would mean two chromadb clients on one store and two GGUF loads.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    # Set before the first Qt import, like the Tk build does for its own
    # bootstrap: the AppUserModelID decides which taskbar button the window
    # groups under, and it has to be set before a window exists.
    import branding
    branding.set_app_user_model_id()

    from PySide6.QtWidgets import QApplication

    from council_qt import theme
    from council_qt.window import CouncilWindow

    app = QApplication(sys.argv)
    theme.apply(app, "dark")

    window = CouncilWindow()
    _register_tabs(window)
    # THE PROCESS MUST ACTUALLY EXIT. Measured while building this: quitting
    # with the bridge's pump timer still running left the interpreter alive
    # after exec() returned — a window the user closed, and a process still in
    # Task Manager. aboutToQuit fires however the app was ended (the window's X,
    # quit(), or a session logout), so the teardown hangs off that rather than
    # off one code path remembering to call it.
    app.aboutToQuit.connect(window.request_close)
    window.show()
    code = app.exec()
    window.request_close()          # idempotent; covers exec() returning early
    return code


def _register_tabs(window) -> None:
    """Every tab that has been ported, in the order the Tk shell shows them.

    A tab absent from here is simply not in this build — which is what makes the
    port shippable in phases rather than as one flip. The Tk shell already does
    the same thing for advanced mode, where six tabs are absent.
    """
    from council_qt.tabs import REGISTRY
    for title, factory, eager in REGISTRY:
        window.add_tab(title, lambda f=factory, w=window: f(w), eager=eager)


if __name__ == "__main__":
    raise SystemExit(main())
