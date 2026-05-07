# ============================================================
# activation_dialog.py  —  License entry / status dialog
# ============================================================
# Three modes the same dialog handles:
#
#   1. Trial active     — informational; "I have a key" path available
#   2. Trial expired    — modal blocker until activated or dismissed to
#                          read-only mode
#   3. Licensed         — status view + Deactivate option
#
# The dialog is opened from:
#   • Startup gate when trial expired and no license
#   • Help → Activate License menu item (any time)
#   • The status badge in the Council action bar (any time)
# ============================================================

from __future__ import annotations

import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

import branding
import licensing


# ============================================================
# Public entry point
# ============================================================

def open_activation_dialog(
    parent: tk.Tk,
    vault_dir: Path,
    *,
    on_status_change: Optional[Callable[[dict], None]] = None,
    blocking: bool = False,
) -> None:
    """
    Open the license / trial status dialog.

    Args:
        parent:       host window
        vault_dir:    where license.json + .trial_started live
        on_status_change: called with the new status dict after any change
        blocking:     when True (trial-expired startup case), the user must
                       either activate or explicitly choose read-only before
                       the dialog will close.
    """
    ActivationDialog(parent, vault_dir,
                     on_status_change=on_status_change,
                     blocking=blocking)


# ============================================================
# Implementation
# ============================================================

class ActivationDialog(tk.Toplevel):

    def __init__(self, parent, vault_dir: Path, *, on_status_change=None,
                 blocking: bool = False):
        super().__init__(parent)
        self.parent       = parent
        self.vault_dir    = vault_dir
        self._cb_status   = on_status_change
        self._blocking    = blocking

        self.title(f"{branding.PRODUCT_NAME} — License")
        self.geometry("560x440")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        try: branding.apply_window_icon(self)
        except Exception: pass

        # Theme
        self._t = branding.get_theme("dark")
        self.configure(bg=self._t["bg"])

        # Centre on parent
        self.update_idletasks()
        try:
            px = parent.winfo_rootx() + (parent.winfo_width() - 560) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - 440) // 2
            self.geometry(f"+{max(0, px)}+{max(0, py)}")
        except Exception:
            pass

        # Refuse the X button while blocking — the user must pick an option
        if blocking:
            self.protocol("WM_DELETE_WINDOW", self._blocked_close)

        self._build_ui()

    # ---- UI ---------------------------------------------------------

    def _build_ui(self):
        t = self._t
        self._status = licensing.get_status(self.vault_dir)

        # Header bar
        head = tk.Frame(self, bg=t["panel_bg"])
        head.pack(fill="x")
        tk.Label(head, text=f"{branding.PRODUCT_NAME}",
                 font=("Segoe UI", 16, "bold"),
                 bg=t["panel_bg"], fg=t["fg"]
                 ).pack(side="left", padx=18, pady=10)
        tk.Label(head, text=f"v{branding.VERSION}",
                 font=("Segoe UI", 10),
                 bg=t["panel_bg"], fg=t["muted_fg"]
                 ).pack(side="left", padx=4, pady=10)

        # Body — status block + entry block
        body = tk.Frame(self, bg=t["bg"])
        body.pack(fill="both", expand=True, padx=18, pady=12)

        # Current status block
        st = self._status["status"]
        msg_color = t["success"] if st in (
            licensing.STATUS_LICENSED, licensing.STATUS_TRIAL_ACTIVE,
        ) else t["warning"]
        if st in (licensing.STATUS_TRIAL_EXPIRED,
                  licensing.STATUS_LICENSE_EXPIRED,
                  licensing.STATUS_INVALID_LICENSE):
            msg_color = t["error"]

        tk.Label(body, text="Status",
                 font=("Segoe UI", 9, "bold"),
                 bg=t["bg"], fg=t["muted_fg"]).pack(anchor="w")
        self._status_label = tk.Label(body, text=self._status["message"],
                                       font=("Segoe UI", 12),
                                       bg=t["bg"], fg=msg_color,
                                       wraplength=520, justify="left")
        self._status_label.pack(anchor="w", pady=(2, 12))

        # Detail rows for licensed / expired states
        if self._status.get("email"):
            self._kv(body, "Licensed to", self._status["email"])
        if self._status.get("plan"):
            self._kv(body, "Plan", self._status["plan"].title())
        if self._status.get("expires_at") and self._status["plan"] != licensing.PLAN_LIFETIME:
            self._kv(body, "Expires",
                     self._status["expires_at"].split("T")[0])

        # Separator
        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=10)

        # License-entry block (always visible — even licensed users
        # might want to switch keys)
        tk.Label(body, text="Enter or replace your license key",
                 font=("Segoe UI", 10, "bold"),
                 bg=t["bg"], fg=t["fg"]).pack(anchor="w")
        tk.Label(body,
                 text="Paste the license blob you received by email. "
                      "It's a single long string of letters and numbers.",
                 font=("Segoe UI", 9),
                 bg=t["bg"], fg=t["muted_fg"], wraplength=520, justify="left"
                 ).pack(anchor="w", pady=(0, 6))

        self._entry = tk.Text(body, height=4, wrap="word",
                              bg=t["input_bg"], fg=t["fg"],
                              insertbackground=t["fg"],
                              relief="flat", font=("Consolas", 9))
        self._entry.pack(fill="x")
        self._error_var = tk.StringVar(value="")
        tk.Label(body, textvariable=self._error_var,
                 bg=t["bg"], fg=t["error"], wraplength=520, justify="left",
                 font=("Segoe UI", 9)
                 ).pack(anchor="w", pady=(4, 0))

        # Footer buttons
        foot = tk.Frame(self, bg=t["bg"])
        foot.pack(fill="x", padx=18, pady=(0, 14), side="bottom")

        ttk.Button(foot, text="🌐  Buy a license",
                   command=self._open_buy_url
                   ).pack(side="left")

        # Right-side action(s)
        right = tk.Frame(foot, bg=t["bg"])
        right.pack(side="right")
        if self._status["status"] == licensing.STATUS_LICENSED:
            ttk.Button(right, text="Deactivate this machine",
                       command=self._deactivate
                       ).pack(side="left", padx=(0, 6))

        # Close vs. Continue read-only depending on blocking mode
        if self._blocking and self._status["status"] in (
            licensing.STATUS_TRIAL_EXPIRED,
            licensing.STATUS_LICENSE_EXPIRED,
        ):
            ttk.Button(right, text="Continue read-only",
                       command=self._continue_readonly
                       ).pack(side="left", padx=(0, 6))
        else:
            ttk.Button(right, text="Close", command=self.destroy
                       ).pack(side="left", padx=(0, 6))

        ttk.Button(right, text="Activate", command=self._activate
                   ).pack(side="left")

    def _kv(self, parent, label, value):
        row = tk.Frame(parent, bg=self._t["bg"])
        row.pack(fill="x", pady=1)
        tk.Label(row, text=f"{label}:", width=14, anchor="w",
                 bg=self._t["bg"], fg=self._t["muted_fg"],
                 font=("Segoe UI", 10)).pack(side="left")
        tk.Label(row, text=value, anchor="w",
                 bg=self._t["bg"], fg=self._t["fg"],
                 font=("Segoe UI", 10)).pack(side="left")

    # ---- Actions ---------------------------------------------------

    def _activate(self):
        blob = self._entry.get("1.0", "end").strip()
        if not blob:
            self._error_var.set("Paste the license blob first.")
            return
        new_status = licensing.activate(self.vault_dir, blob)
        if new_status["status"] == licensing.STATUS_LICENSED:
            messagebox.showinfo(
                "Activation successful",
                f"Welcome, {new_status.get('email','')}!\n\n"
                f"{new_status['message']}",
                parent=self,
            )
            if self._cb_status:
                try: self._cb_status(new_status)
                except Exception: pass
            self.destroy()
        else:
            self._error_var.set(new_status["message"] or "Could not activate.")

    def _deactivate(self):
        if not messagebox.askyesno(
            "Deactivate license?",
            "This removes the license from this computer. You'll need to "
            "re-paste your key if you want to use new deliberations again.\n\n"
            "Useful when moving to a new machine.\n\n"
            "Continue?",
            parent=self):
            return
        licensing.deactivate(self.vault_dir)
        new_status = licensing.get_status(self.vault_dir)
        if self._cb_status:
            try: self._cb_status(new_status)
            except Exception: pass
        self.destroy()

    def _continue_readonly(self):
        # User explicitly accepts read-only mode. The host application's
        # gate logic still allows past sessions to be viewed.
        if self._cb_status:
            try: self._cb_status(self._status)
            except Exception: pass
        self.destroy()

    def _blocked_close(self):
        messagebox.showinfo(
            "Activation required",
            "Either activate a license, click 'Continue read-only', "
            "or buy a key first.",
            parent=self)

    def _open_buy_url(self):
        url = (getattr(branding, "WEBSITE", "") or "").strip()
        if not url:
            url = "https://example.com/buy"   # placeholder
        try:
            webbrowser.open(url)
        except Exception:
            pass
