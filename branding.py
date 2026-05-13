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


# ---- Mode --------------------------------------------------------------------
# DEMO_MODE switches off everything customer-product-flavoured:
#   • Licensing / trial / activation gates (all calls return "personal use")
#   • Auto-update check (server URL is forced empty)
#   • "Buy"/"Activate" entry points in the UI
#   • Crash reporter "Email to support" button (keeps the local save option)
#
# When this branch lives at home, leave it on. When you flip a build into
# the commercial channel, set DI_DEMO_MODE=0.
DEMO_MODE = os.environ.get("DI_DEMO_MODE", "1").lower() not in ("0", "false", "no")


# ---- Identity ----------------------------------------------------------------

PRODUCT_NAME    = os.environ.get("COUNCIL_PRODUCT_NAME", "Data's Inferno")
PRODUCT_SHORT   = os.environ.get("COUNCIL_PRODUCT_SHORT", "Data's Inferno")
PRODUCT_TAGLINE = os.environ.get(
    "COUNCIL_TAGLINE",
    "An AI panel for poking at my own data."
)
PRODUCT_PITCH   = os.environ.get(
    "COUNCIL_PITCH",
    "Drop in a CSV, ask a question. A panel of AI specialists deliberates "
    "and gives you an answer you can poke at because every step is visible. "
    "Runs locally — your data stays on this machine."
)
VERSION         = "1.0.0-home"
COPYRIGHT       = "© 2026"
SUPPORT_EMAIL   = os.environ.get("COUNCIL_SUPPORT_EMAIL", "")
WEBSITE         = os.environ.get("COUNCIL_WEBSITE",       "")

# URL of the JSON manifest used by the auto-updater. Empty disables
# update checks entirely. Forced empty in DEMO_MODE so a home build
# never phones home.
UPDATE_MANIFEST_URL = "" if DEMO_MODE else os.environ.get(
    "DI_UPDATE_MANIFEST_URL", "",
)

# URL of the activation server (the small Flask service in
# tools/license_server.py). Empty falls back to LocalActivationServer
# (mints tokens locally — fine for personal use). Forced empty in
# DEMO_MODE so a home build never reaches out.
ACTIVATION_SERVER_URL = "" if DEMO_MODE else os.environ.get(
    "DI_ACTIVATION_SERVER_URL", "",
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
    # Inferno red-and-grey palette
    "bg":               "#1a1414",   # near-black with a faint warm tint
    "fg":               "#d4d4d4",   # primary text — neutral light grey
    "muted_fg":         "#7a7575",   # secondary text — warm grey
    "panel_bg":         "#231a1a",   # darker panel/frame bg
    "input_bg":         "#0f0c0c",   # input/text widget bg — deepest
    "border":           "#3a2828",
    "selection_bg":     "#4a2626",
    "accent":           "#d32f2f",   # primary accent — Mars red
    "accent_hover":     "#ff5252",   # hover — bright cherry red
    "success":          "#7ea16d",   # muted desaturated green
    "warning":          "#e0884a",   # ember orange
    "error":            "#ff5252",
    "info":             "#a98a8a",   # warm grey for informational text
}

LIGHT_THEME: Dict[str, str] = {
    "name":             "light",
    "bg":               "#f4f1f1",
    "fg":               "#1a1414",
    "muted_fg":         "#6a5b5b",
    "panel_bg":         "#ffffff",
    "input_bg":         "#ffffff",
    "border":           "#d8c8c8",
    "selection_bg":     "#f0c8c8",
    "accent":           "#b71c1c",
    "accent_hover":     "#d32f2f",
    "success":          "#5a7d4a",
    "warning":          "#b06a2a",
    "error":            "#b71c1c",
    "info":             "#6a5b5b",
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


APP_USER_MODEL_ID = "Infernoplaystuf.Council.Demo.1"


def set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> None:
    """Tell Windows our process is its own app, not a child of python.exe.

    Without this, the taskbar groups our window under whatever launched it
    (Spyder, IDLE, plain python.exe) and uses that host's icon. Setting an
    explicit AppUserModelID makes Windows treat the running interpreter as
    a distinct app and the iconbitmap/iconphoto we set on the Tk window
    actually shows up in the taskbar.

    Must be called BEFORE the Tk root is created. Safe to call on non-
    Windows platforms (no-op).
    """
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass  # AppUserModelID is cosmetic; never block startup


def apply_window_icon(window) -> None:
    """
    Set the platform-appropriate window icon, no-op on failure.
    Called by the GUI on startup and by every Toplevel popup.

    On Windows, also calls iconphoto with both the .ico and the PNG (in
    that order) — Spyder's PYTHONUNBUFFERED Python kernel sometimes
    ignores iconbitmap so the PNG path is a belt-and-suspenders fallback.
    """
    try:
        if ICON_ICO.exists():
            try:
                window.iconbitmap(default=str(ICON_ICO))
            except Exception:
                pass
        if ICON_PNG.exists():
            import tkinter as tk
            try:
                img = tk.PhotoImage(file=str(ICON_PNG))
                # Keep a reference on the window so the image isn't GC'd
                # (Tk PhotoImage requires a live Python reference).
                window._council_icon_image = img
                window.iconphoto(True, img)
            except Exception:
                pass
    except Exception:
        pass  # icon is cosmetic, never block startup
