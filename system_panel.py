"""
system_panel.py — "System & Models" settings panel for the Council GUI.

Renders the hardware survey from ``inferno_local.cookbook`` and a
per-catalog-model fit verdict so a user can see at a glance which
models will run comfortably on their machine, which are tight, and
which won't fit. Includes a backend selector that goes through
``inferno_local.model_runner.build_runner`` so cloud backends are
refused with a clear message.

Can be opened from the host app:

    from system_panel import open_panel
    open_panel(parent=root)

Or run standalone (terminal mode, no Tk):

    python system_panel.py

Both paths share ``snapshot()`` so the same data renders identically
in the GUI panel and the CLI dump.
"""
from __future__ import annotations

import json
import platform
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional

from inferno_local import cookbook, model_runner, security
import model_catalog


# ============================================================
# Pure-data snapshot (used by GUI panel + CLI dump)
# ============================================================

def snapshot() -> Dict[str, Any]:
    """Return everything the panel renders in a single dict.

    Side-effect free apart from probing the local machine. No model
    download, no network call. Safe to call from a worker thread."""
    hw = cookbook.describe()
    vram = cookbook.primary_vram_gb()
    fits = []
    for spec in model_catalog.MODELS:
        fits.append({
            "model_id": spec.id,
            "name":     spec.name,
            "org":      spec.org,
            "size_gb":  spec.size_gb,
            "vram_gb_q4": spec.vram_gb_q4,
            "license":  spec.license,
            "verdict":  cookbook.fit_verdict(spec, vram),
            "is_default": spec.is_default,
        })
    return {"hardware": hw, "models": fits, "primary_vram_gb": vram}


# ============================================================
# CLI mode
# ============================================================

def render_cli(snap: Dict[str, Any]) -> str:
    hw = snap["hardware"]
    lines: List[str] = []
    lines.append("─── Hardware ─────────────────────────────────────")
    lines.append(f"  OS:           {hw['os']} {hw['os_release']}")
    lines.append(f"  Python:       {hw['python']}")
    lines.append(f"  CPU:          {hw['cpu']}")
    lines.append(f"  RAM:          {hw['ram_gb']} GB")
    if hw["gpus"]:
        for i, g in enumerate(hw["gpus"]):
            lines.append(f"  GPU {i}:        {g['name']}  "
                         f"{g['vram_gb_total']} GB total / "
                         f"{g['vram_gb_free']} GB free  "
                         f"(driver {g['driver']})")
    else:
        lines.append("  GPU:          (no nvidia-smi — CPU-only)")
    lines.append(f"  CUDA runtime: {hw['cuda_runtime']}")
    lines.append(f"  Models dir:   {hw['models_dir']}")
    if hw["models_on_disk"]:
        for m in hw["models_on_disk"]:
            tag = "(catalog)" if m["matched"] else "(sideload)"
            lines.append(f"    • {m['filename']:<48s} {m['size_gb']:6.2f} GB  {tag}")
    else:
        lines.append("    (no .gguf files in models dir)")
    lines.append("")
    lines.append("─── Catalog (US-origin) ──────────────────────────")
    for m in snap["models"]:
        verdict = m["verdict"]
        emoji = {"clean": "✓", "tight": "⚠", "oom": "✗", "cpu-only": "·"}[verdict["fit"]]
        star = " ★" if m["is_default"] else "  "
        lines.append(f"  {emoji}{star} {m['name']:<40s} "
                     f"{m['size_gb']:5.1f} GB  "
                     f"~{m['vram_gb_q4']:4.1f} GB VRAM  "
                     f"{m['license']}")
        lines.append(f"        {verdict['reason']}")
    return "\n".join(lines)


# ============================================================
# Tkinter panel
# ============================================================

class SystemPanel(tk.Toplevel):
    """Standalone Toplevel — doesn't require modifying the main GUI's
    notebook. Opens via ``open_panel(parent)``. Every long-running probe
    (none yet) would go on a worker thread; right now ``snapshot()`` is
    cheap enough to call on the UI thread."""

    def __init__(self, parent: Optional[tk.Misc] = None):
        super().__init__(parent)
        self.title("System & Models — Data's Inferno")
        try:
            self.geometry("960x680")
        except Exception:
            pass
        self.minsize(720, 480)
        self._build()
        self.refresh()

    # ── layout ─────────────────────────────────────────────
    def _build(self) -> None:
        bg = "#1a1d28"
        fg = "#e6e8ee"
        self.configure(bg=bg)

        toolbar = tk.Frame(self, bg=bg)
        toolbar.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Button(toolbar, text="🔄 Refresh", command=self.refresh
                   ).pack(side="left")
        ttk.Button(toolbar, text="🧪 Test backend…",
                   command=self._open_backend_test
                   ).pack(side="left", padx=8)
        ttk.Button(toolbar, text="📋 Copy snapshot",
                   command=self._copy_snapshot
                   ).pack(side="left")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        # Hardware tab
        self._hw_tab = tk.Frame(nb, bg=bg)
        nb.add(self._hw_tab, text="Hardware")
        self._hw_text = tk.Text(self._hw_tab, bg="#0e1018", fg=fg,
                                font=("Consolas", 10), wrap="none",
                                relief="flat", padx=10, pady=10)
        self._hw_text.pack(fill="both", expand=True)

        # Catalog tab
        self._cat_tab = tk.Frame(nb, bg=bg)
        nb.add(self._cat_tab, text="Model Catalog (fit per row)")
        cols = ("verdict", "name", "org", "size", "vram", "license", "reason")
        self._cat_tree = ttk.Treeview(self._cat_tab, columns=cols,
                                       show="headings", height=16)
        for c, w in zip(cols, (70, 280, 90, 70, 80, 200, 280)):
            self._cat_tree.heading(c, text=c.upper())
            self._cat_tree.column(c, width=w, anchor="w")
        self._cat_tree.pack(fill="both", expand=True, padx=8, pady=8)

    # ── data refresh ───────────────────────────────────────
    def refresh(self) -> None:
        snap = snapshot()
        # Hardware text
        self._hw_text.config(state="normal")
        self._hw_text.delete("1.0", "end")
        self._hw_text.insert("1.0", render_cli(snap))
        self._hw_text.config(state="disabled")
        # Catalog table
        for row in self._cat_tree.get_children():
            self._cat_tree.delete(row)
        for m in snap["models"]:
            v = m["verdict"]
            emoji = {"clean": "✓ FITS", "tight": "⚠ TIGHT",
                     "oom": "✗ OOM", "cpu-only": "· CPU"}[v["fit"]]
            self._cat_tree.insert("", "end", values=(
                emoji, m["name"], m["org"],
                f"{m['size_gb']:.1f} GB",
                f"{m['vram_gb_q4']:.1f} GB",
                m["license"],
                v["reason"],
            ))
        # Cache snapshot for copy-to-clipboard
        self._last_snapshot = snap

    # ── actions ────────────────────────────────────────────
    def _copy_snapshot(self) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(
                json.dumps(self._last_snapshot, indent=2, default=str)
            )
            messagebox.showinfo("Copied",
                                "Snapshot copied to clipboard as JSON.",
                                parent=self)
        except Exception as exc:
            messagebox.showerror("Copy failed", repr(exc), parent=self)

    def _open_backend_test(self) -> None:
        """Pop a tiny modal that lets the user pick a backend dict and
        try to build a runner. Cloud backends are caught by the factory
        and surface the EgressBlocked message clearly."""
        dlg = tk.Toplevel(self)
        dlg.title("Test backend config")
        dlg.geometry("520x320")
        dlg.transient(self)
        tk.Label(dlg, text="Edit a backend config; click 'Try build'.",
                 anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        default_cfg = json.dumps({
            "backend": "llama_cpp",
            "gguf_path": "<unset>",
        }, indent=2)
        editor = tk.Text(dlg, height=10, font=("Consolas", 10))
        editor.pack(fill="both", expand=True, padx=10, pady=4)
        editor.insert("1.0", default_cfg)

        result_var = tk.StringVar(value="")
        tk.Label(dlg, textvariable=result_var, anchor="w",
                 wraplength=480, justify="left",
                 fg="#cbd1de"
                 ).pack(fill="x", padx=10, pady=(4, 8))

        def _try():
            try:
                cfg = json.loads(editor.get("1.0", "end"))
            except Exception as exc:
                result_var.set(f"JSON error: {exc!r}")
                return
            try:
                r = model_runner.build_runner(cfg)
                result_var.set(f"✓ built: {r.describe()}")
            except security.EgressBlocked as exc:
                result_var.set(f"✗ egress blocked: {exc}")
            except Exception as exc:
                result_var.set(f"✗ build failed: {exc!r}")

        ttk.Button(dlg, text="Try build", command=_try
                   ).pack(side="left", padx=10, pady=(0, 10))
        ttk.Button(dlg, text="Close", command=dlg.destroy
                   ).pack(side="right", padx=10, pady=(0, 10))


def open_panel(parent: Optional[tk.Misc] = None) -> SystemPanel:
    return SystemPanel(parent)


# ============================================================
# Standalone CLI entry
# ============================================================

if __name__ == "__main__":
    # Headless dump — fall back to CLI if Tkinter can't open a window
    # (e.g. SSH session without DISPLAY).
    snap = snapshot()
    print(render_cli(snap))
