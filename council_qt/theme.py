"""
council_qt.theme — the Inferno palette, from branding.py, as Qt.

WHY THIS READS branding.py RATHER THAN RESTATING COLOURS
The Tk shell has ONE ttk.Style block (15 configure calls) and then 271
hard-coded hex literals scattered through 22,610 lines, because ttk's Windows
theme ignores style backgrounds and every coloured widget had to say its colour
itself. That is the mess this file exists to not repeat: the tokens live in
branding.THEMES, both front ends read them, and a colour appears in exactly one
place.

It also makes the light theme reachable for the first time. branding.LIGHT_THEME
has always existed; the Tk shell hard-codes get_theme("dark") and never reads
its own `_theme` attribute again.

PALETTE *AND* STYLESHEET, deliberately
QPalette is the right home for the base colours: it is what unstyled widgets
consult, it survives theme switches, and it reaches places a stylesheet does not
(the disabled colour group, selection in item views). But the native Windows
style ignores the palette for several things — tab bars, headers, and the frame
around a group box — so those get a small QSS on top. The QSS is kept short on
purpose: every rule in it is a rule that stops the platform style applying, and
an app that restyles everything stops looking like the platform it runs on.
"""
from __future__ import annotations

from typing import Dict

from PySide6.QtGui import QColor, QPalette

import branding


def tokens(name: str = "dark") -> Dict[str, str]:
    """The theme dict. One source of truth, shared with the Tk shell."""
    return branding.get_theme(name)


def palette(name: str = "dark") -> QPalette:
    """The theme as a QPalette, including the Disabled group.

    The Disabled group matters more than it looks: without it, a disabled
    button keeps the enabled text colour and the only cue that a control is
    unavailable disappears — which is precisely how ports.enable(False) is
    meant to read.
    """
    t = tokens(name)
    bg = QColor(t["bg"])
    fg = QColor(t["fg"])
    panel = QColor(t["panel_bg"])
    field = QColor(t["input_bg"])
    muted = QColor(t["muted_fg"])
    accent = QColor(t["accent"])
    selection = QColor(t["selection_bg"])

    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, bg)
    p.setColor(QPalette.ColorRole.WindowText, fg)
    p.setColor(QPalette.ColorRole.Base, field)
    p.setColor(QPalette.ColorRole.AlternateBase, panel)
    p.setColor(QPalette.ColorRole.Text, fg)
    p.setColor(QPalette.ColorRole.Button, panel)
    p.setColor(QPalette.ColorRole.ButtonText, fg)
    p.setColor(QPalette.ColorRole.ToolTipBase, panel)
    p.setColor(QPalette.ColorRole.ToolTipText, fg)
    p.setColor(QPalette.ColorRole.Highlight, selection)
    p.setColor(QPalette.ColorRole.HighlightedText, fg)
    p.setColor(QPalette.ColorRole.Link, accent)
    p.setColor(QPalette.ColorRole.PlaceholderText, muted)
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, muted)
    return p


def stylesheet(name: str = "dark") -> str:
    """The few things the platform style will not take from the palette."""
    t = tokens(name)
    return f"""
QTabWidget::pane {{
    border: 1px solid {t['border']};
    background: {t['bg']};
}}
QTabBar::tab {{
    background: {t['panel_bg']};
    color: {t['muted_fg']};
    border: 1px solid {t['border']};
    border-bottom: none;
    padding: 6px 12px;
}}
QTabBar::tab:selected {{
    background: {t['bg']};
    color: {t['fg']};
    border-top: 2px solid {t['accent']};
}}
QTabBar::tab:hover {{
    color: {t['fg']};
}}
QHeaderView::section {{
    background: {t['panel_bg']};
    color: {t['fg']};
    border: 1px solid {t['border']};
    padding: 4px;
}}
QGroupBox {{
    border: 1px solid {t['border']};
    margin-top: 8px;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    color: {t['muted_fg']};
}}
QStatusBar {{
    color: {t['muted_fg']};
    border-top: 1px solid {t['border']};
}}
QPlainTextEdit, QTextEdit, QLineEdit, QListWidget, QTreeWidget {{
    background: {t['input_bg']};
    color: {t['fg']};
    border: 1px solid {t['border']};
    selection-background-color: {t['selection_bg']};
}}
"""


def apply(app, name: str = "dark") -> None:
    """Dress a QApplication. The only call the rest of the app needs."""
    app.setStyle("Fusion")
    # Fusion, not the native Windows 11 style: the native style ignores the
    # palette for most surfaces, so a dark app under it comes out half light.
    # The Tk shell reached the same conclusion from the other side — it swaps
    # to the "clam" ttk theme for exactly this reason.
    app.setPalette(palette(name))
    app.setStyleSheet(stylesheet(name))
