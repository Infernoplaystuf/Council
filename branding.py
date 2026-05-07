# ============================================================
# branding.py  —  Centralised product identity
# ============================================================
# Edit this file (or override via environment variables) to rebrand
# the product without touching every UI string. The build script
# also reads PRODUCT_NAME and VERSION from here.
# ============================================================

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict


# ---- Identity ----------------------------------------------------------------

PRODUCT_NAME    = os.environ.get("COUNCIL_PRODUCT_NAME", "Data's Inferno")
PRODUCT_SHORT   = os.environ.get("COUNCIL_PRODUCT_SHORT", "Data's Inferno")
PRODUCT_TAGLINE = os.environ.get(
    "COUNCIL_TAGLINE",
    "A panel of AI specialists that reviews, charts, and explains your business data."
)
PRODUCT_PITCH   = os.environ.get(
    "COUNCIL_PITCH",
    "Drop in your sales, inventory, or customer data. Ask questions in plain English. "
    "Get charts, summaries, and insights from a panel of AI analysts that runs entirely "
    "on your computer — your data never leaves the machine."
)
VERSION         = "1.0.0-rc1"
COPYRIGHT       = "© 2026"
SUPPORT_EMAIL   = os.environ.get("COUNCIL_SUPPORT_EMAIL", "")
WEBSITE         = os.environ.get("COUNCIL_WEBSITE",       "")

# URL of the JSON manifest used by the auto-updater. Empty disables
# update checks entirely. Override via DI_UPDATE_MANIFEST_URL env var
# at build time (so different release channels can point at different
# manifests without rebuilding).
UPDATE_MANIFEST_URL = os.environ.get(
    "DI_UPDATE_MANIFEST_URL",
    "",   # disabled by default until you have a hosting URL
)

# URL of the activation server (the small Flask service in
# tools/license_server.py that you'll deploy somewhere). Empty falls
# back to LocalActivationServer (mints tokens locally — useful for
# development; no device-limit enforcement).
ACTIVATION_SERVER_URL = os.environ.get(
    "DI_ACTIVATION_SERVER_URL",
    "",   # set to e.g. "https://activate.datas-inferno.app" at build time
)


# ---- Asset paths -------------------------------------------------------------

ASSETS_DIR = Path(__file__).parent / "assets"
ICON_ICO   = ASSETS_DIR / "icon.ico"   # Windows
ICON_PNG   = ASSETS_DIR / "icon.png"   # Linux / fallback
ICON_ICNS  = ASSETS_DIR / "icon.icns"  # macOS
SPLASH_PNG = ASSETS_DIR / "splash.png"


# ---- Themes ------------------------------------------------------------------
# Each theme is a flat dict so ttk.Style.configure() can pull straight from it.
# Add new themes by adding a new entry to THEMES.

DARK_THEME: Dict[str, str] = {
    "name":             "dark",
    "bg":               "#1e1e2e",   # window background
    "fg":               "#cdd6f4",   # primary text
    "muted_fg":         "#7f849c",   # secondary text
    "panel_bg":         "#181825",   # darker panel/frame bg
    "input_bg":         "#11111b",   # input/text widget bg
    "border":           "#313244",
    "selection_bg":     "#45475a",
    "accent":           "#89b4fa",   # primary accent (links, focus)
    "accent_hover":     "#b4befe",
    "success":          "#a6e3a1",
    "warning":          "#fab387",
    "error":            "#f38ba8",
    "info":             "#94e2d5",
}

LIGHT_THEME: Dict[str, str] = {
    "name":             "light",
    "bg":               "#fafafa",
    "fg":               "#1e1e2e",
    "muted_fg":         "#6c7086",
    "panel_bg":         "#ffffff",
    "input_bg":         "#ffffff",
    "border":           "#cdd6f4",
    "selection_bg":     "#bac2de",
    "accent":           "#1e66f5",
    "accent_hover":     "#7287fd",
    "success":          "#40a02b",
    "warning":          "#df8e1d",
    "error":            "#d20f39",
    "info":             "#179299",
}

THEMES: Dict[str, Dict[str, str]] = {
    "dark":  DARK_THEME,
    "light": LIGHT_THEME,
}


def get_theme(name: str = "dark") -> Dict[str, str]:
    """Return a theme by name, falling back to dark on unknown."""
    return THEMES.get(name, DARK_THEME)


# ---- Window helpers ----------------------------------------------------------

def window_title(subtitle: str = "") -> str:
    """Build the window title in a consistent format."""
    if subtitle:
        return f"{PRODUCT_NAME} — {subtitle}"
    return PRODUCT_NAME


def apply_window_icon(window) -> None:
    """
    Set the platform-appropriate window icon, no-op on failure.
    Called by the GUI on startup and by every Toplevel popup.
    """
    try:
        if ICON_ICO.exists():
            window.iconbitmap(default=str(ICON_ICO))
            return
        if ICON_PNG.exists():
            import tkinter as tk
            img = tk.PhotoImage(file=str(ICON_PNG))
            window.iconphoto(True, img)
    except Exception:
        pass  # icon is cosmetic, never block startup
