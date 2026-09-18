"""
council_qt.splash — the spinning cog, in Qt.

The artwork is `council_core.splash_art`, painted through the same five-method
protocol the Designer canvas uses, so there is no drawing code here at all —
only a frameless window, a timer, and the three behaviours the Tk splash
learned the hard way.

IT IS ON TOP ONLY LONG ENOUGH TO BE SEEN
This used to be a permanent topmost. On a slow load — a big vault, a cold
model — the splash then sat over EVERY application on the desktop for the whole
load: frameless, unmovable, with nothing to click. That is indistinguishable
from a hung modal. So it raises, then drops topmost a second later.

IT CAN BE MOVED AND IT CAN BE DISMISSED
There is no title bar to grab, so dragging is wired by hand; without it the
window cannot be moved at all. Escape closes it. Both exist because a splash
that outlives its welcome must not also be a trap.

COUNCIL_NO_SPLASH=1 SKIPS IT ENTIRELY
Scripted and headless runs drive the app with its window hidden, and a
frameless always-visible window from a background process is pure nuisance — it
appears over whatever the user is actually doing. The stub has the same surface
(`pump` / `dismiss` / `close`), so no caller needs a branch.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from council_core import splash_art as art

from .widgets.painter import QtPainter

#: How long it stays above other applications.
TOPMOST_MS = 1200


def show_splash(duration_ms: int = 1800,
                on_done: Optional[Callable[[], None]] = None,
                manual: bool = False) -> Any:
    """The splash, or a stub that quietly does nothing.

    `manual=True` suppresses the auto-dismiss: the caller keeps the cog turning
    through a blocking build with `pump()` and ends it with `dismiss()`. That
    is what lets one splash cover the whole of the window's construction.
    """
    if os.environ.get("COUNCIL_NO_SPLASH", "").strip().lower() in (
            "1", "true", "yes"):
        return NoSplash(on_done)
    return SplashWindow(duration_ms=duration_ms, on_done=on_done,
                        manual=manual)


class NoSplash:
    """Does nothing, quietly, with the surface callers already use."""

    def __init__(self, on_done: Optional[Callable[[], None]] = None):
        self.on_done = on_done
        self.dismissed = False

    def pump(self) -> None:
        pass

    def dismiss(self, on_done: Optional[Callable[[], None]] = None) -> None:
        self.dismissed = True
        callback = on_done if on_done is not None else self.on_done
        if callback:
            try:
                callback()
            except Exception:                            # noqa: BLE001
                # A splash that refuses to go away because the thing it was
                # covering is unhappy is worse than either problem alone.
                pass

    def close(self) -> None:
        pass


class SplashWindow(QWidget):
    """Frameless, on top briefly, draggable, escapable."""

    def __init__(self, duration_ms: int = 1800,
                 on_done: Optional[Callable[[], None]] = None,
                 manual: bool = False, theme: str = "dark"):
        super().__init__(None,
                         Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        self.on_done = on_done
        self.manual = bool(manual)
        self.dismissed = False
        self._drag_from = None
        self._animation = art.Animation()
        self._accent, self._panel = self._colours(theme)

        self.setFixedSize(art.SIZE, art.SIZE)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self._centre()
        self.show()

        self._frames = QTimer(self)
        self._frames.timeout.connect(self._tick)
        self._frames.start(art.FRAME_MS)

        # Raise, then let go. See the module docstring.
        QTimer.singleShot(TOPMOST_MS, self._release_topmost)
        if not self.manual:
            QTimer.singleShot(duration_ms, self.dismiss)

    # ------------------------------------------------------------------
    @staticmethod
    def _colours(theme: str):
        try:
            import branding
            tokens = branding.get_theme(theme)
            return tokens["accent"], tokens["panel_bg"]
        except Exception:                                # noqa: BLE001
            # The splash must not be the thing that stops a launch.
            return "#f6c14a", "#1b1b1f"

    def _centre(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.move(available.center().x() - art.SIZE // 2,
                  available.center().y() - art.SIZE // 2)

    def _release_topmost(self) -> None:
        """Best-effort. A window that cannot drop the flag is still a window."""
        try:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
            if not self.dismissed:
                self.show()          # changing a flag re-creates the window
        except Exception:                                # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        self._animation.advance()
        self.update()

    def pump(self) -> None:
        """One frame, by hand.

        For a caller blocking the event loop while it builds the main window:
        the timer cannot fire, so the cog would freeze exactly when it is most
        needed.
        """
        if self.dismissed:
            return
        self._tick()
        QApplication.processEvents()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.fillRect(self.rect(), QColor(self._panel))
            self._animation.paint(QtPainter(painter), self._accent,
                                  self._panel)
        finally:
            painter.end()

    # ------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - \
                self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_from is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)

    def mouseReleaseEvent(self, _event) -> None:
        self._drag_from = None

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    def dismiss(self, on_done: Optional[Callable[[], None]] = None) -> None:
        """Stop, close, then hand over. Idempotent.

        Idempotent because three things can call it: the auto-dismiss timer,
        Escape, and the caller. The handover runs exactly once — it is the
        reveal, and revealing twice is at best a flicker.
        """
        if self.dismissed:
            return
        self.dismissed = True
        self._frames.stop()
        self.close()
        callback = on_done if on_done is not None else self.on_done
        if callback:
            try:
                callback()
            except Exception:                            # noqa: BLE001
                pass
