"""
db_connect_wizard.py — a guided, plain-language wizard for linking a
database, aimed at NON-TECHNICAL users.

The Vault tab already has a connection panel, but it asks the user to
edit a raw connection URL (``postgresql://user:${PG_PASS}@host:5432/db``)
and to set an environment variable for the password. That's fine for an
engineer and a wall for everyone else. This wizard removes both walls:

  • Separate, plainly-labelled fields — Server address / Port /
    Database name / Sign-in name / Password — instead of one URL string.
  • A file picker for the file-based databases (SQLite / DuckDB).
  • Sensible default ports filled in per database type.
  • The connection URL is ASSEMBLED for the user, with the username and
    password correctly percent-encoded — so a password containing ``@``,
    ``:``, ``/`` or a space (which silently breaks a hand-typed URL)
    just works.
  • The password is saved on this machine by default (no environment
    variable to set). An "Advanced" toggle still offers the env-var
    route for users who want the password kept out of the file.
  • A Test step verifies the connection before it's saved.

Read-only is unchanged: the wizard only ever calls
``db_connections.save_*_connection`` / ``test_*_connection``. It opens
no new path to the database, so the five-layer read-only guarantee
(no INSERT/UPDATE/DELETE/DROP, ever) carries over untouched.

The URL-assembly logic lives in the pure, Tk-free
``build_connection_url`` so it can be unit-tested headlessly; the Tk
wizard is a thin shell over it.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Callable, Dict, Optional, Tuple

# ── Type metadata ────────────────────────────────────────────────────
# Friendly label → internal type key. The internal keys match the
# existing connection panel's type dropdown so storage is identical.
FRIENDLY_TYPES = (
    ("PostgreSQL",              "postgresql"),
    ("MySQL / MariaDB",         "mysql"),
    ("Microsoft SQL Server",    "mssql"),
    ("MongoDB",                 "mongodb"),
    ("SQLite (file on disk)",   "sqlite"),
    ("DuckDB (file on disk)",   "duckdb"),
)
_FRIENDLY_TO_KEY = {label: key for label, key in FRIENDLY_TYPES}
_KEY_TO_FRIENDLY = {key: label for label, key in FRIENDLY_TYPES}

FILE_TYPES = ("sqlite", "duckdb")
SERVER_TYPES = ("postgresql", "mysql", "mssql", "mongodb")

DEFAULT_PORTS = {
    "postgresql": 5432,
    "mysql":      3306,
    "mssql":      1433,
    "mongodb":    27017,
}

_SQL_SCHEME = {
    "postgresql": "postgresql",
    "mysql":      "mysql+pymysql",
    "mssql":      "mssql+pyodbc",
}


def _q(value: Any) -> str:
    """Percent-encode a URL component with NOTHING left safe — so a
    password like ``p@ss:w/rd`` becomes a valid URL component instead of
    corrupting the netloc."""
    return urllib.parse.quote(str(value), safe="")


def build_connection_url(
    db_type: str,
    *,
    host: str = "",
    port: "str | int" = "",
    database: str = "",
    user: str = "",
    password: str = "",
    file_path: str = "",
    env_var: str = "",
) -> Tuple[str, str]:
    """Assemble a (kind, url) pair from plain field values.

    ``kind`` is ``"sql"`` or ``"mongo"`` (which save_* function to use).
    ``url`` is a ready-to-save connection string with user/password
    percent-encoded.

    Password handling:
      • ``env_var`` set → a literal ``${ENV_VAR}`` placeholder is used
        (resolved from the environment at connect time; left unencoded
        because the resolver substitutes the raw value).
      • otherwise ``password`` is inlined, percent-encoded.

    Raises ValueError with a plain-language message on missing required
    fields, so the wizard can show it verbatim.
    """
    db_type = (db_type or "").strip().lower()

    # ── File-based: just a path, no host/credentials ────────────────
    if db_type in FILE_TYPES:
        fp = (file_path or "").strip().replace("\\", "/")
        if not fp:
            raise ValueError("Choose a database file first.")
        return ("sql", f"{db_type}:///{fp}")

    if db_type == "mongodb":
        kind, scheme = "mongo", "mongodb"
    elif db_type in _SQL_SCHEME:
        kind, scheme = "sql", _SQL_SCHEME[db_type]
    else:
        raise ValueError(f"Unknown database type: {db_type!r}")

    host = (host or "").strip()
    if not host:
        raise ValueError("Enter the server address (e.g. an IP like "
                         "10.0.0.5 or a name like db.company.com).")

    port_s = str(port).strip() or str(DEFAULT_PORTS.get(db_type, ""))

    # Password component
    if (env_var or "").strip():
        pw_part = "${" + env_var.strip() + "}"     # placeholder, unencoded
    else:
        pw_part = _q(password) if password else ""

    user_part = _q(user) if (user or "").strip() else ""

    auth = ""
    if user_part:
        auth = user_part + (":" + pw_part if pw_part else "") + "@"

    netloc = host + (f":{port_s}" if port_s else "")
    db_clean = (database or "").strip()
    db_part = f"/{_q(db_clean)}" if db_clean else "/"

    url = f"{scheme}://{auth}{netloc}{db_part}"

    # Type-specific query params
    params: Dict[str, str] = {}
    if db_type == "mssql":
        params["driver"] = "ODBC Driver 17 for SQL Server"
    if db_type == "mongodb" and db_clean:
        # The DB the user named is almost always their auth database.
        params["authSource"] = db_clean
    if params:
        url += "?" + urllib.parse.urlencode(params)

    return (kind, url)


def suggest_env_var_name(conn_name: str) -> str:
    """A tidy env-var name from a connection handle: 'sales db' →
    'SALES_DB_PASSWORD'."""
    base = "".join(c if c.isalnum() else "_" for c in (conn_name or "").upper())
    base = "_".join(filter(None, base.split("_"))) or "DB"
    return f"{base}_PASSWORD"


# ============================================================
# Tk wizard (thin shell over build_connection_url)
# ============================================================

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    _TK_OK = True
except Exception:                       # pragma: no cover — headless box
    tk = None          # type: ignore[assignment]
    ttk = messagebox = filedialog = None  # type: ignore[assignment]
    _TK_OK = False

_TkBase = tk.Toplevel if _TK_OK else object


class DatabaseConnectionWizard(_TkBase):
    """Guided, field-based connection setup. Construct with the vault
    dir and an ``on_saved`` callback (called with the connection name
    after a successful save, so the host can refresh its list)."""

    def __init__(self, parent, vault_dir,
                 on_saved: Optional[Callable[[str], None]] = None,
                 log: Optional[Callable[[str, str], None]] = None):
        if not _TK_OK:
            raise RuntimeError("DatabaseConnectionWizard requires tkinter.")
        super().__init__(parent)
        self.vault_dir = vault_dir
        self._on_saved = on_saved
        self._log = log or (lambda msg, tag="info": None)

        self.title("Connect a database")
        self.configure(bg="#1a1414")
        self.geometry("560x560")
        try:
            self.transient(parent)
        except Exception:
            pass

        self._vars: Dict[str, "tk.StringVar"] = {}
        self._advanced = tk.BooleanVar(value=False)
        self._build()
        self._on_type_change()

    # ---- layout ----
    def _row(self, parent, label, key, *, show=None, default=""):
        fr = ttk.Frame(parent)
        fr.pack(fill="x", padx=12, pady=3)
        ttk.Label(fr, text=label, width=18, anchor="w").pack(side="left")
        var = tk.StringVar(value=default)
        self._vars[key] = var
        ent = ttk.Entry(fr, textvariable=var, show=show)
        ent.pack(side="left", fill="x", expand=True)
        return fr, ent

    def _build(self):
        ttk.Label(self, text="Connect a database",
                  font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=12, pady=(12, 0))
        ttk.Label(self,
                  text="Read-only — this app can read and export data, "
                       "but can never change or delete anything in your database.",
                  foreground="#a6e3a1", wraplength=520, justify="left",
                  ).pack(anchor="w", padx=12, pady=(2, 8))

        # Type
        type_fr = ttk.Frame(self)
        type_fr.pack(fill="x", padx=12, pady=3)
        ttk.Label(type_fr, text="Database type", width=18, anchor="w").pack(side="left")
        self._type_var = tk.StringVar(value=FRIENDLY_TYPES[0][0])
        type_cb = ttk.Combobox(type_fr, textvariable=self._type_var,
                               values=[lbl for lbl, _ in FRIENDLY_TYPES],
                               state="readonly")
        type_cb.pack(side="left", fill="x", expand=True)
        type_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_type_change())

        # Connection name (always shown)
        self._row(self, "Connection name", "name", default="my_database")

        # Server fields (host/port/db/user/password) — shown for server DBs
        self._server_frame = ttk.Frame(self)
        self._server_frame.pack(fill="x")
        self._row(self._server_frame, "Server address", "host")
        self._row(self._server_frame, "Port", "port")
        self._row(self._server_frame, "Database name", "database")
        self._row(self._server_frame, "Sign-in name", "user")
        _, self._pw_entry = self._row(self._server_frame, "Password", "password", show="•")

        # File field (path + browse) — shown for SQLite/DuckDB
        self._file_frame = ttk.Frame(self)
        file_inner = ttk.Frame(self._file_frame)
        file_inner.pack(fill="x", padx=12, pady=3)
        ttk.Label(file_inner, text="Database file", width=18, anchor="w").pack(side="left")
        self._vars["file_path"] = tk.StringVar()
        ttk.Entry(file_inner, textvariable=self._vars["file_path"]).pack(
            side="left", fill="x", expand=True)
        ttk.Button(file_inner, text="Browse…", command=self._browse_file).pack(
            side="left", padx=(4, 0))

        # Advanced: env-var password storage
        adv_fr = ttk.Frame(self)
        adv_fr.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Checkbutton(
            adv_fr,
            text="Advanced: keep the password out of the saved file "
                 "(use an environment variable)",
            variable=self._advanced, command=self._on_advanced_toggle,
        ).pack(anchor="w")
        self._env_frame = ttk.Frame(self)
        self._row(self._env_frame, "Env-var name", "env_var")
        ttk.Label(self._env_frame,
                  text="Set this variable in your system before launching "
                       "the app; the password won't be stored in the file.",
                  foreground="#9399b2", wraplength=520, justify="left",
                  ).pack(anchor="w", padx=12)

        # Status line
        self._status = ttk.Label(self, text="", wraplength=520,
                                 justify="left", foreground="#9399b2")
        self._status.pack(anchor="w", padx=12, pady=(10, 0))

        # Buttons
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=12, side="bottom")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="💾 Save", command=self._save).pack(side="right", padx=6)
        ttk.Button(btns, text="🧪 Test", command=self._test).pack(side="right")

    # ---- dynamic show/hide ----
    def _current_type(self) -> str:
        return _FRIENDLY_TO_KEY.get(self._type_var.get(), "postgresql")

    def _on_type_change(self):
        t = self._current_type()
        if t in FILE_TYPES:
            self._server_frame.pack_forget()
            self._file_frame.pack(fill="x")
        else:
            self._file_frame.pack_forget()
            self._server_frame.pack(fill="x")
            # Fill the default port for the chosen type.
            self._vars["port"].set(str(DEFAULT_PORTS.get(t, "")))
        self._set_status("")

    def _on_advanced_toggle(self):
        if self._advanced.get():
            self._env_frame.pack(fill="x")
            if not self._vars["env_var"].get().strip():
                self._vars["env_var"].set(
                    suggest_env_var_name(self._vars["name"].get()))
            try:
                self._pw_entry.configure(state="disabled")
            except Exception:
                pass
        else:
            self._env_frame.pack_forget()
            try:
                self._pw_entry.configure(state="normal")
            except Exception:
                pass

    def _browse_file(self):
        t = self._current_type()
        ext = ".sqlite" if t == "sqlite" else ".duckdb"
        path = filedialog.askopenfilename(
            title="Choose a database file",
            filetypes=[(f"{t} database", f"*{ext}"), ("All files", "*.*")],
            parent=self,
        )
        if path:
            self._vars["file_path"].set(path)

    def _set_status(self, text, color="#9399b2"):
        try:
            self._status.configure(text=text, foreground=color)
        except Exception:
            pass

    # ---- assemble + actions ----
    def _assemble(self) -> Tuple[str, str, str]:
        """Return (kind, name, url) from the current fields, or raise
        ValueError with a user-facing message."""
        name = self._vars["name"].get().strip()
        if not name:
            raise ValueError("Give this connection a name.")
        t = self._current_type()
        env_var = (self._vars["env_var"].get().strip()
                   if self._advanced.get() else "")
        kind, url = build_connection_url(
            t,
            host=self._vars["host"].get(),
            port=self._vars["port"].get(),
            database=self._vars["database"].get(),
            user=self._vars["user"].get(),
            password=self._vars["password"].get(),
            file_path=self._vars["file_path"].get(),
            env_var=env_var,
        )
        return kind, name, url

    def _save_to_store(self, kind, name, url):
        import db_connections as _db
        if kind == "mongo":
            _db.save_mongo_connection(self.vault_dir, name, url)
        else:
            _db.save_sql_connection(self.vault_dir, name, url)

    def _test(self):
        try:
            kind, name, url = self._assemble()
        except ValueError as e:
            self._set_status(str(e), "#f38ba8")
            return
        # Persist first (test_* reads from the saved store), then probe.
        self._set_status("Testing… this can take a few seconds.", "#9399b2")
        self.update_idletasks()
        try:
            self._save_to_store(kind, name, url)
            import db_connections as _db
            res = (_db.test_mongo_connection(self.vault_dir, name)
                   if kind == "mongo"
                   else _db.test_sql_connection(self.vault_dir, name))
        except Exception as exc:
            self._set_status(f"Couldn't test: {exc}", "#f38ba8")
            return
        if res.get("ok"):
            extra = (f"{res.get('databases', 0)} database(s)"
                     if kind == "mongo"
                     else f"{res.get('tables', 0)} table(s), {res.get('dialect')}")
            self._set_status(f"✓ Connected — {extra}. Saved as “{name}”.",
                             "#a6e3a1")
            self._log(f"wizard: tested + saved {kind} connection {name}", "ok")
            if self._on_saved:
                self._on_saved(name)
        else:
            miss = res.get("unresolved_env_vars")
            hint = ("  (Set the environment variable first.)" if miss else "")
            self._set_status(
                f"✗ {res.get('error') or 'could not connect'}.{hint}",
                "#f38ba8")

    def _save(self):
        try:
            kind, name, url = self._assemble()
            self._save_to_store(kind, name, url)
        except ValueError as e:
            self._set_status(str(e), "#f38ba8")
            return
        except Exception as exc:
            self._set_status(f"Save failed: {exc}", "#f38ba8")
            return
        self._log(f"wizard: saved {kind} connection {name}", "ok")
        if self._on_saved:
            self._on_saved(name)
        self.destroy()


def open_wizard(parent, vault_dir,
                on_saved: Optional[Callable[[str], None]] = None,
                log: Optional[Callable[[str, str], None]] = None
                ) -> "DatabaseConnectionWizard":
    return DatabaseConnectionWizard(parent, vault_dir,
                                    on_saved=on_saved, log=log)
