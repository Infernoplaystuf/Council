"""
gui_runwith.py — the GUI Designer's "Run with:" selector.

A small self-contained widget so the designer tab only places it: the tab is
kept to thin marshalling (tests/test_gui_tab.py holds it to a line budget),
and anything computed there cannot be tested without the whole Council.

It shows and edits ONE setting — the open project's Manifest.python — and
never runs anything; the Run button does that through python_envs.preflight.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

import gui_projects as gp
import python_envs as pe


class RunWithBox(ttk.Frame):
    """Label + read-only dropdown bound to the open project's interpreter.

    ``get_dir`` returns the open project's directory (or None); ``log``
    writes a line to the designer's log."""

    def __init__(self, master, *, get_dir: Callable[[], Optional[object]],
                 log: Callable[[str], None] = print):
        super().__init__(master)
        self._get_dir, self._log = get_dir, log
        ttk.Label(self, text="Run with:").pack(side="left", padx=(0, 2))
        self.var = tk.StringVar(self, value=pe.DEFAULT_LABEL)
        self.box = ttk.Combobox(self, textvariable=self.var, width=24,
                                state="readonly", postcommand=self.fill)
        self.box.pack(side="left")
        self.box.bind("<<ComboboxSelected>>", self._picked)
        self.fill()

    def fill(self) -> None:
        self.box.configure(values=pe.choices(self.var.get()))

    def sync(self) -> None:
        """Show the open project's setting — call after open / new / wizard."""
        pdir, spec = self._get_dir(), ""
        if pdir:
            try:
                spec = gp.load_manifest(pdir).python
            except Exception:
                spec = ""
        self.var.set(pe.display(spec))

    def _picked(self, _e=None) -> None:
        choice, pdir = self.var.get(), self._get_dir()
        if not pdir:
            messagebox.showinfo("Run with", "Open or create a project first — "
                                "the choice is saved with the project.")
            self.var.set(pe.DEFAULT_LABEL)
            return
        if choice == pe.BROWSE_LABEL:
            path = filedialog.askopenfilename(
                parent=self, title="Choose the Python that runs this project",
                filetypes=[("Python", "python*.exe python python3"),
                           ("All files", "*.*")])
            if not path:
                self.sync()
                return
            spec = path
        else:
            spec = pe.spec_from_choice(choice)
        try:
            man = gp.load_manifest(pdir)
            man.python = spec
            gp.save_manifest(pdir, man)
        except Exception as exc:
            messagebox.showerror("Run with", str(exc))
        self.sync()
        self._log(f"Run with: {self.var.get()} — checked when you press Run")
