# Database Grabber

A small, **standalone, read-only** desktop tool to connect to a database,
browse it, and **export tables/collections to CSV, JSON, or Excel**.

It can read and export data. It can **never** change, insert, or delete
anything in a database — that guarantee is enforced in five independent
layers (see *Read-only by design* below).

Supports **PostgreSQL, MySQL/MariaDB, Microsoft SQL Server, MongoDB,
SQLite, and DuckDB**.

> This branch (`Database-grabber`) is a self-contained extraction of the
> read-only database connectivity + guided wizard from the larger
> Council app. It runs on its own and packages into a single executable.

---

## For the end user (no Python required)

If someone hands you `DatabaseGrabber.exe` (Windows) or `DatabaseGrabber`
(Linux/macOS), just run it — **nothing else to install**.

1. Click **➕ Connect a database (guided)…**
2. Pick the database type, fill in the plain-language fields
   (Server address, Database name, Sign-in name, Password) — or, for
   SQLite/DuckDB, just browse to the file. Default ports are filled in
   for you.
3. Click **🧪 Test** — it tells you in plain English whether it connected.
4. Back on the main window, select the connection and click
   **📂 Browse & Export…**, pick a table/collection, and click
   **CSV / JSON / Excel**.

Exports are saved under `~/.db_grabber/exports/` and the folder opens
automatically. Saved connections live under `~/.db_grabber/` (override
with the `DBGRABBER_HOME` environment variable).

---

## Building the standalone executable

PyInstaller bundles Python, Tk, and all dependencies into one file, so
the machines you give it to need **no pre-installed Python**. You build
it **once** on each operating system you want to ship to (PyInstaller
does not cross-compile).

**Windows** — needs Python 3.10+ ([python.org](https://www.python.org/downloads/), tick *Add to PATH*):

```
build-windows.bat
```

→ produces `dist\DatabaseGrabber.exe`

**Linux / macOS** — needs Python 3.10+ and Tk
(`sudo apt install python3-venv python3-tk` on Debian/Ubuntu):

```
./build-linux.sh
```

→ produces `dist/DatabaseGrabber`

To bundle support for a given database, that database's driver must be
installed when you build (they're listed in `requirements.txt`; the
build scripts install all of them). At runtime, a missing driver simply
shows a clear "driver not installed" message rather than crashing.

---

## Running from source (for development)

```
python -m venv .venv
.venv\Scripts\activate      # Windows   (source .venv/bin/activate on *nix)
pip install -r requirements.txt
python database_grabber.py
```

Headless tests (no display needed):

```
python tests/test_grabber.py
```

---

## Read-only by design

The connection layer (`db_connections.py`) enforces read-only access in
five independent layers, so a misconfiguration in any one of them is
backstopped by the others:

1. **DB role** — the recommended setup is a read-only database account.
2. **Session** — SQL sessions issue `SET TRANSACTION READ ONLY` where
   the dialect supports it.
3. **Statement validator** — every SQL statement is checked to be a
   single read-only query; `INSERT / UPDATE / DELETE / DROP / TRUNCATE /
   ALTER` are rejected, including comment-cloaked attempts.
4. **API surface** — the code exposes only `read_*`, `list_*`,
   SELECT-only `sql_query`, read-only Mongo `find/aggregate/count/
   distinct` (with `$out`/`$merge`/`$where` blocked), and `export_*`.
   There is **no** insert/update/delete/drop function anywhere.
5. **Audit log** — every read and export is logged to
   `~/.db_grabber/db_audit.log`.

Exports route through the read-only readers and only ever **write a
local file** — there is no code path that writes back to the database.

---

## Files

| File | Purpose |
|------|---------|
| `database_grabber.py` | Standalone GUI (main window, browse + export). |
| `db_connect_wizard.py` | Guided, field-based connection wizard + the pure URL builder. |
| `db_connections.py` | Read-only SQL + Mongo connectivity, validators, exporters, audit log. |
| `database_grabber.spec` | PyInstaller build recipe. |
| `build-windows.bat` / `build-linux.sh` | One-shot build scripts. |
| `requirements.txt` | Runtime + build dependencies. |
| `tests/test_grabber.py` | Headless tests (read-only guarantee, URL assembly, export writers). |
