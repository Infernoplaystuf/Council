"""
Database Grabber — a standalone, read-only database connector + exporter.

A single-window desktop app: link to a database with a guided wizard,
browse its tables/collections, and export them to CSV / JSON / Excel.
It can READ and EXPORT data; it can NEVER change or delete anything in
a database — the connectivity layer (db_connections.py) enforces that
in five independent layers.

This is the same read-only connectivity + guided-wizard code used by
the larger Council app, extracted to run on its own. When packaged with
PyInstaller (see database_grabber.spec / build-windows.bat /
build-linux.sh) it ships as a single executable that needs no
pre-installed Python on the target machine.

Storage: connections and exports live under ``~/.db_grabber`` (override
with the ``DBGRABBER_HOME`` environment variable).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Database Grabber"
APP_VERSION = "1.0.0"

# Where saved connections (sql_connections.json / mongo_connections.json),
# the audit log, and exports live. A self-contained per-user folder so the
# packaged app needs no install-time configuration.
STORAGE_DIR = Path(
    os.environ.get("DBGRABBER_HOME") or (Path.home() / ".db_grabber")
).expanduser()
EXPORTS_DIR = STORAGE_DIR / "exports"

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except Exception:  # pragma: no cover — headless
    print("Database Grabber needs a graphical display (tkinter).",
          file=sys.stderr)
    raise

import db_connections as dbc
import db_connect_wizard as wiz


# ============================================================
# Main window
# ============================================================

class DatabaseGrabberApp(tk.Tk):
    def __init__(self):
        super().__init__()
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

        self.title(f"{APP_NAME}  v{APP_VERSION}")
        self.geometry("760x580")
        self.minsize(620, 460)
        self.configure(bg="#14181f")

        self._build()
        self._refresh_list()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout ----
    def _build(self):
        bg = "#14181f"
        tk.Label(self, text="🗄  Database Grabber",
                 font=("Segoe UI", 16, "bold"),
                 bg=bg, fg="#e6edf3").pack(anchor="w", padx=14, pady=(12, 0))
        tk.Label(self,
                 text="Connect to a database, browse it, and export tables "
                      "to a file. Read-only — it can never change or delete "
                      "anything in your database.",
                 bg=bg, fg="#7ee787", wraplength=700, justify="left",
                 ).pack(anchor="w", padx=14, pady=(2, 10))

        # Toolbar
        bar = tk.Frame(self, bg=bg)
        bar.pack(fill="x", padx=14)
        ttk.Button(bar, text="➕ Connect a database (guided)…",
                   command=self._open_wizard).pack(side="left")
        ttk.Button(bar, text="🧪 Test",
                   command=self._test_selected).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="📂 Browse & Export…",
                   command=self._browse_selected).pack(side="left", padx=6)
        ttk.Button(bar, text="🗑 Remove",
                   command=self._remove_selected).pack(side="left")

        # Saved connections list
        tk.Label(self, text="Saved connections (double-click to browse):",
                 bg=bg, fg="#9aa4b2").pack(anchor="w", padx=14, pady=(12, 2))
        list_fr = tk.Frame(self, bg=bg)
        list_fr.pack(fill="both", expand=True, padx=14)
        self._list = tk.Listbox(list_fr, bg="#1b212b", fg="#e6edf3",
                                selectbackground="#2d6cdf", relief="flat",
                                font=("Consolas", 10), activestyle="none")
        self._list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_fr, command=self._list.yview)
        sb.pack(side="left", fill="y")
        self._list.configure(yscrollcommand=sb.set)
        self._list.bind("<Double-Button-1>", lambda _e: self._browse_selected())

        # Status
        self._status = tk.Label(self, text="", bg=bg, fg="#9aa4b2",
                                anchor="w", wraplength=720, justify="left")
        self._status.pack(fill="x", padx=14, pady=(6, 12))

    def _set_status(self, text, color="#9aa4b2"):
        try:
            self._status.configure(text=text, fg=color)
        except Exception:
            pass

    # ---- connection list ----
    def _refresh_list(self):
        self._list.delete(0, "end")
        self._rows = []  # (kind, name)
        try:
            for name, url in sorted(dbc.list_sql_connections(STORAGE_DIR).items()):
                self._list.insert("end", f"[sql]    {name}    {_mask(url)}")
                self._rows.append(("sql", name))
            for name, url in sorted(dbc.list_mongo_connections(STORAGE_DIR).items()):
                self._list.insert("end", f"[mongo]  {name}    {_mask(url)}")
                self._rows.append(("mongo", name))
        except Exception as exc:
            self._set_status(f"Could not read saved connections: {exc}", "#ff7b72")
        if not self._rows:
            self._set_status("No connections yet — click “Connect a database "
                             "(guided)…” to add one.")

    def _selected(self):
        sel = self._list.curselection()
        if not sel:
            return None
        return self._rows[sel[0]]

    # ---- actions ----
    def _open_wizard(self):
        wiz.open_wizard(self, STORAGE_DIR,
                        on_saved=lambda _n: self._refresh_list(),
                        log=lambda msg, tag="info": self._set_status(msg))

    def _test_selected(self):
        picked = self._selected()
        if not picked:
            self._set_status("Pick a connection in the list first.", "#d29922")
            return
        kind, name = picked
        self._set_status(f"Testing “{name}”… this can take a few seconds.")
        self.update_idletasks()
        try:
            res = (dbc.test_mongo_connection(STORAGE_DIR, name) if kind == "mongo"
                   else dbc.test_sql_connection(STORAGE_DIR, name))
        except Exception as exc:
            self._set_status(f"✗ test failed: {exc}", "#ff7b72")
            return
        if res.get("ok"):
            extra = (f"{res.get('databases', 0)} database(s)" if kind == "mongo"
                     else f"{res.get('tables', 0)} table(s), {res.get('dialect')}")
            self._set_status(f"✓ “{name}” connected — {extra}.", "#7ee787")
        else:
            miss = res.get("unresolved_env_vars")
            hint = "  (Set the environment variable first.)" if miss else ""
            self._set_status(f"✗ {res.get('error') or 'could not connect'}.{hint}",
                             "#ff7b72")

    def _remove_selected(self):
        picked = self._selected()
        if not picked:
            self._set_status("Pick a connection to remove.", "#d29922")
            return
        kind, name = picked
        if not messagebox.askyesno(
                "Remove connection",
                f"Remove the {kind} connection “{name}”?\n\n"
                "Only the saved connection details are deleted from this "
                "app. Your database is not touched.", parent=self):
            return
        if kind == "mongo":
            dbc.remove_mongo_connection(STORAGE_DIR, name)
        else:
            dbc.remove_sql_connection(STORAGE_DIR, name)
        self._refresh_list()
        self._set_status(f"Removed “{name}”.")

    def _browse_selected(self):
        picked = self._selected()
        if not picked:
            self._set_status("Pick a connection to browse.", "#d29922")
            return
        BrowserWindow(self, picked[0], picked[1])

    def _on_close(self):
        try:
            dbc.dispose_engines()
        except Exception:
            pass
        self.destroy()


# ============================================================
# Browse + Export window
# ============================================================

class BrowserWindow(tk.Toplevel):
    """Lists tables (SQL) or db.collection pairs (Mongo), previews the
    first 50 rows, and exports the full object to CSV/JSON/Excel."""

    def __init__(self, parent, kind: str, name: str):
        super().__init__(parent)
        self.kind = kind
        self.name = name
        self.title(f"{kind.upper()}: {name}")
        self.geometry("820x620")
        self.configure(bg="#14181f")

        top = tk.Frame(self, bg="#14181f")
        top.pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(top, text=f"{name}  ({kind})", bg="#14181f", fg="#e6edf3",
                 font=("Segoe UI", 11, "bold")).pack(side="left")

        list_fr = tk.Frame(self, bg="#14181f")
        list_fr.pack(fill="x", padx=8, pady=2)
        self._lst = tk.Listbox(list_fr, height=9, bg="#1b212b", fg="#e6edf3",
                               selectbackground="#2d6cdf", relief="flat",
                               font=("Consolas", 9), activestyle="none")
        self._lst.pack(side="left", fill="x", expand=True)
        sb = ttk.Scrollbar(list_fr, command=self._lst.yview)
        sb.pack(side="left", fill="y")
        self._lst.configure(yscrollcommand=sb.set)
        self._lst.bind("<<ListboxSelect>>", lambda _e: self._preview())

        exp = tk.Frame(self, bg="#14181f")
        exp.pack(fill="x", padx=8, pady=(4, 0))
        tk.Label(exp, text="Export selected →", bg="#14181f",
                 fg="#9aa4b2").pack(side="left", padx=(0, 4))
        for fmt, lbl in (("csv", "CSV"), ("json", "JSON"), ("xlsx", "Excel")):
            ttk.Button(exp, text=lbl, width=7,
                       command=lambda f=fmt: self._export(f)).pack(side="left", padx=2)
        tk.Label(exp, text="(full table, read-only — never deletes)",
                 bg="#14181f", fg="#6e7681", font=("", 8)).pack(side="left", padx=6)

        pv_lf = tk.LabelFrame(self, text="Preview (first 50 rows, read-only)",
                              bg="#14181f", fg="#9aa4b2")
        pv_lf.pack(fill="both", expand=True, padx=8, pady=6)
        self._preview_txt = tk.Text(pv_lf, wrap="none", bg="#1b212b",
                                    fg="#e6edf3", font=("Consolas", 9),
                                    relief="flat")
        self._preview_txt.pack(fill="both", expand=True, padx=4, pady=4)

        self._status = tk.Label(self, text="", bg="#14181f", fg="#9aa4b2",
                                anchor="w", wraplength=780, justify="left")
        self._status.pack(fill="x", padx=8, pady=(0, 8))

        self._populate()

    def _set_status(self, text, color="#9aa4b2"):
        try:
            self._status.configure(text=text, fg=color)
        except Exception:
            pass

    def _populate(self):
        self._lst.delete(0, "end")
        try:
            if self.kind == "mongo":
                for db_name in dbc.list_mongo_databases(STORAGE_DIR, self.name):
                    for c in dbc.list_mongo_collections(STORAGE_DIR, self.name, db_name):
                        self._lst.insert("end", f"{db_name}.{c}")
            else:
                for t in dbc.list_sql_tables(STORAGE_DIR, self.name):
                    self._lst.insert("end", t)
        except Exception as exc:
            self._set_status(f"Could not list contents: {exc}", "#ff7b72")

    def _picked(self):
        sel = self._lst.curselection()
        return self._lst.get(sel[0]) if sel else None

    def _preview(self):
        picked = self._picked()
        if not picked:
            return
        self._preview_txt.delete("1.0", "end")
        self._preview_txt.insert("end", f"loading first 50 rows of {picked}…\n")
        self.update_idletasks()
        try:
            df = self._read(picked, limit=50)
            self._preview_txt.delete("1.0", "end")
            self._preview_txt.insert(
                "end", "(no rows)\n" if df.empty
                else df.to_string(index=False) + "\n")
            self._set_status(f"previewed {picked} — {len(df)} row(s)")
        except Exception as exc:
            self._preview_txt.delete("1.0", "end")
            self._preview_txt.insert("end", f"✗ {exc}\n")
            self._set_status(f"preview failed: {exc}", "#ff7b72")

    def _read(self, picked, *, limit):
        if self.kind == "mongo":
            db_name, coll = picked.split(".", 1)
            return dbc.read_mongo_collection(STORAGE_DIR, self.name, db_name,
                                             coll, limit=limit)
        return dbc.read_sql_table(STORAGE_DIR, self.name, picked, limit=limit)

    def _export(self, fmt):
        picked = self._picked()
        if not picked:
            self._set_status("Select a table/collection first.", "#d29922")
            return
        dest = EXPORTS_DIR / dbc._safe_export_name(f"{self.name}__{picked}", fmt)
        self._set_status(f"exporting {picked} → {dest} …")
        self.update_idletasks()
        try:
            if self.kind == "mongo":
                db_name, coll = picked.split(".", 1)
                info = dbc.export_mongo_collection(STORAGE_DIR, self.name,
                                                   db_name, coll, dest, fmt=fmt)
            else:
                info = dbc.export_sql_table(STORAGE_DIR, self.name, picked,
                                            dest, fmt=fmt)
            self._set_status(f"✓ exported {info['rows']} row(s) → {info['path']}",
                             "#7ee787")
            _open_folder(Path(info["path"]).parent)
        except Exception as exc:
            self._set_status(f"✗ export failed: {exc}", "#ff7b72")


# ============================================================
# Helpers
# ============================================================

def _mask(url: str) -> str:
    """Hide an inline password in a connection URL for display."""
    import re
    return re.sub(r"(://[^:/@]+:)([^@/]+)(@)", r"\1***\3", str(url))


def _open_folder(path: Path):
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", str(path)])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def main():
    # --self-test: construct the window, pump one update, exit 0. Lets a
    # packaging build verify the bundle actually starts (all DLLs + Tk +
    # imports resolve) WITHOUT blocking forever in mainloop. Prints a
    # sentinel so an automated build check can confirm success.
    if "--self-test" in sys.argv:
        app = DatabaseGrabberApp()
        app.update_idletasks()
        app.update()
        app.destroy()
        print("SELFTEST OK")
        return
    app = DatabaseGrabberApp()
    app.mainloop()


if __name__ == "__main__":
    main()
