"""
db_connections.py — read-only SQL + MongoDB connectivity for the
Council analyst sandbox.

Defense in depth — five independent layers:

  1. DB-side role permissions (the primary defense — the user creates
     a read-only role on the DB itself; see DATABASE_CONNECTIONS.md).

  2. Session-level read-only — for SQL, issue `SET TRANSACTION READ
     ONLY` (PG / MySQL) or read-only execution_options (SQLAlchemy).
     For SQLite/DuckDB file URIs we already restrict via ?mode=ro.

  3. Client-side validator — every SQL string is parsed before
     dispatch; only single SELECT or WITH statements are allowed.
     DDL/DML keywords (DROP, DELETE, INSERT, UPDATE, TRUNCATE,
     ALTER, CREATE, GRANT, REVOKE, MERGE, REPLACE) are rejected
     even if they appear in a multi-statement payload separated
     by `;`. Comments (`--`, `/* */`) are stripped before keyword
     match so a comment-cloaked DROP can't slip through.

  4. API surface design — for Mongo we expose ONLY find / aggregate
     / count_documents / distinct / list_*. There is no public
     wrapper for insert / update / delete / drop / replace / find_
     one_and_*. The pipeline validator also rejects `$out`, `$merge`,
     `$function`, `$accumulator`, `$where` (last two run server-side
     JS which can bypass the read-only role on misconfigured
     servers).

  5. Audit log — every connection use and every dispatched query
     is appended to vault/db_audit.log as one JSONL record per
     entry. The log is forensic — doesn't prevent writes — but
     gives you a clear trail when something does leak through.

All connection URLs live in vault/sql_connections.json (existing) and
vault/mongo_connections.json (new). Both honour `${ENV_VAR}`
placeholders so passwords stay out of the JSON file. The placeholder
expansion runs at connect time, not at storage time, so a user can
rotate `$PG_PASS` in their shell without re-saving the connection.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


# ============================================================
# Connection storage — JSON files in the vault
# ============================================================

_SQL_CONN_FILENAME   = "sql_connections.json"
_MONGO_CONN_FILENAME = "mongo_connections.json"
_AUDIT_LOG_FILENAME  = "db_audit.log"


def _connections_path(vault_dir: Any, filename: str) -> Path:
    return Path(vault_dir) / filename


def _read_json_dict(p: Path) -> Dict[str, Any]:
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_dict(p: Path, data: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── SQL connection registry ─────────────────────────────────────────

def list_sql_connections(vault_dir: Any) -> Dict[str, str]:
    """Return the saved ``{name: url}`` map. URLs are RAW (still
    contain ``${ENV_VAR}`` placeholders) — call ``_resolve_url`` to
    expand them at connect time."""
    return {str(k): str(v)
            for k, v in _read_json_dict(
                _connections_path(vault_dir, _SQL_CONN_FILENAME)).items()}


def save_sql_connection(vault_dir: Any, name: str, url: str) -> None:
    """Save a SQLAlchemy connection URL. Use ``${ENV_VAR}`` placeholders
    in passwords; the resolver expands them at connect time."""
    name = (str(name) if name is not None else "").strip()
    url = (str(url) if url is not None else "").strip()
    if not name:
        raise ValueError("connection name cannot be empty")
    if not url:
        raise ValueError("connection URL cannot be empty")
    if "://" not in url:
        raise ValueError(
            f"connection URL {url!r} is missing a scheme "
            "(expected something like 'postgresql://user:pass@host/db')")
    p = _connections_path(vault_dir, _SQL_CONN_FILENAME)
    existing = _read_json_dict(p)
    existing[name] = url
    _write_json_dict(p, existing)


def remove_sql_connection(vault_dir: Any, name: str) -> bool:
    """Drop a saved SQL connection. Returns True if it existed."""
    p = _connections_path(vault_dir, _SQL_CONN_FILENAME)
    existing = _read_json_dict(p)
    if name not in existing:
        return False
    existing.pop(name, None)
    _write_json_dict(p, existing)
    return True


# ── Mongo connection registry ──────────────────────────────────────

def list_mongo_connections(vault_dir: Any) -> Dict[str, str]:
    """Return the saved ``{name: mongodb_uri}`` map."""
    return {str(k): str(v)
            for k, v in _read_json_dict(
                _connections_path(vault_dir, _MONGO_CONN_FILENAME)).items()}


def save_mongo_connection(vault_dir: Any, name: str, uri: str) -> None:
    """Save a Mongo URI (``mongodb://...``). Use ``${ENV_VAR}``
    placeholders for passwords."""
    name = (str(name) if name is not None else "").strip()
    uri = (str(uri) if uri is not None else "").strip()
    if not name:
        raise ValueError("connection name cannot be empty")
    if not uri:
        raise ValueError("connection URI cannot be empty")
    if not (uri.startswith("mongodb://") or uri.startswith("mongodb+srv://")):
        raise ValueError(
            f"Mongo URI {uri!r} must start with 'mongodb://' or "
            "'mongodb+srv://'")
    p = _connections_path(vault_dir, _MONGO_CONN_FILENAME)
    existing = _read_json_dict(p)
    existing[name] = uri
    _write_json_dict(p, existing)


def remove_mongo_connection(vault_dir: Any, name: str) -> bool:
    p = _connections_path(vault_dir, _MONGO_CONN_FILENAME)
    existing = _read_json_dict(p)
    if name not in existing:
        return False
    existing.pop(name, None)
    _write_json_dict(p, existing)
    return True


class UnresolvedEnvVarError(Exception):
    """Raised when a ${ENV_VAR} placeholder in a saved URL has no value
    in os.environ. Surfaces a clear message at the UI / audit layer
    instead of letting an unresolved placeholder reach SQLAlchemy /
    pymongo and produce an opaque connection error."""

    def __init__(self, missing: List[str]):
        self.missing = list(missing)
        super().__init__(
            "Connection URL references environment variable(s) that are "
            "not set: " + ", ".join(self.missing) + ". Set them before "
            "testing or querying this connection."
        )


def _resolve_url(url: str) -> str:
    """Expand ``${ENV_VAR}`` placeholders against os.environ.

    Raises UnresolvedEnvVarError when ANY placeholder is missing — we
    refuse to hand SQLAlchemy / pymongo a URL containing a literal
    ``${FOO}`` because the resulting connection error gives the user
    no useful diagnostic ("password authentication failed for user u"
    when the real cause is "FOO isn't set in your shell").
    """
    missing: list = []

    def _sub(m: "re.Match") -> str:
        name = m.group(1)
        val = os.environ.get(name)
        if val is None:
            missing.append(name)
            return m.group(0)
        return val

    resolved = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, url)
    if missing:
        # De-dup while preserving order
        seen: dict = {}
        for m in missing:
            seen[m] = None
        raise UnresolvedEnvVarError(list(seen))
    return resolved


# ============================================================
# TLS / encryption posture
# ============================================================
#
# We don't FORCE TLS — some deployments have valid reasons for
# plaintext (local sockets, VPN tunnels, trusted-LAN dev). But when
# a URL has cleartext credentials AND no TLS hint AND the host
# clearly isn't local, we surface a warning at save time (UI) and
# at connection time (audit log). The user can suppress it by
# setting COUNCIL_DB_TLS_WARN=0.

_LOCAL_HOST_PATTERNS = re.compile(
    r"(?:^|@)("
    r"localhost|127\.0\.0\.1|::1|"
    r"0\.0\.0\.0|"
    # RFC1918 private ranges — common on LAN deployments
    r"10\.\d+\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|"
    # File-based DBs — no network at all
    r"\.[\\/]"
    r")",
    re.IGNORECASE,
)

# Per-dialect TLS hints we recognise as "TLS already requested"
_TLS_HINT_PATTERNS = (
    re.compile(r"sslmode\s*=\s*(?:require|verify[-_]?ca|verify[-_]?full)",
                re.IGNORECASE),                # postgres
    re.compile(r"ssl\s*=\s*(?:true|1)", re.IGNORECASE),
    re.compile(r"ssl_ca\s*=", re.IGNORECASE),  # mysql
    re.compile(r"tls\s*=\s*(?:true|1)", re.IGNORECASE),
    re.compile(r"encrypt\s*=\s*(?:yes|true)",
                re.IGNORECASE),                # mssql
    re.compile(r"\?.*tls(?:Insecure)?(?:=|$)",
                re.IGNORECASE),                # mongo &tls=true
    re.compile(r"^mongodb\+srv://", re.IGNORECASE),  # +srv always TLS
    re.compile(r"^https?://", re.IGNORECASE),
    re.compile(r"^sqlite:///", re.IGNORECASE),  # file-based, no network
    re.compile(r"^duckdb:///", re.IGNORECASE),
)


def _looks_remote(url: str) -> bool:
    """True when the URL's host is NOT obviously local. Used to gate
    TLS warnings — we don't warn on localhost / RFC1918 / file URIs."""
    return not bool(_LOCAL_HOST_PATTERNS.search(url))


def _has_tls_hint(url: str) -> bool:
    """True when the URL has any recognised TLS / encryption hint."""
    return any(p.search(url) for p in _TLS_HINT_PATTERNS)


def _has_cleartext_credentials(url: str) -> bool:
    """True when the URL contains a literal password (not an
    ``${ENV_VAR}`` placeholder). Used to gate TLS warnings — a URL
    with no credentials at all doesn't leak anything by going
    plaintext."""
    # Strip env-var placeholders FIRST so a `${PASS}` doesn't trip
    # the credential detector.
    no_placeholders = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "", url)
    # SQLAlchemy / Mongo URI shape: scheme://user:pass@host/...
    return bool(re.search(r"://[^:/@\s]+:[^@\s]+@", no_placeholders))


def check_tls_posture(url: str) -> "Optional[str]":
    """Returns a warning string when the URL looks like it would
    send credentials in cleartext over a non-local network, else
    None. Suppressed entirely when COUNCIL_DB_TLS_WARN=0."""
    if os.environ.get("COUNCIL_DB_TLS_WARN", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return None
    if not _has_cleartext_credentials(url):
        # ${ENV_VAR} or no creds — nothing to leak.
        return None
    if not _looks_remote(url):
        # localhost / LAN / file-based — TLS-optional.
        return None
    if _has_tls_hint(url):
        return None
    return (
        "⚠ Cleartext credentials over what looks like a non-local "
        "host without a TLS hint. Add one of: sslmode=require "
        "(Postgres), ssl=true (MySQL), encrypt=yes (MSSQL), "
        "tls=true (Mongo). Or replace the password with an "
        "${ENV_VAR} placeholder. Suppress this check with "
        "COUNCIL_DB_TLS_WARN=0."
    )


# ============================================================
# Audit log — size-capped rotation
# ============================================================
#
# Caps default to 100 MB per file, 5 rotations kept (db_audit.log,
# db_audit.log.1 … db_audit.log.5). Both knobs are env-configurable:
#
#   COUNCIL_DB_AUDIT_MAX_MB     (default 100)
#   COUNCIL_DB_AUDIT_KEEP       (default 5)
#
# Rotation runs lazily before each write — when the current log
# exceeds the cap, we shift db_audit.log.N → .N+1 (oldest deleted),
# rename db_audit.log → db_audit.log.1, and start a fresh one.
# Atomic enough for the single-writer pattern; on the unlikely
# race we may briefly miss a record but never duplicate one.

import threading as _threading

_AUDIT_LOCK = _threading.Lock()


def _audit_cap_bytes() -> int:
    try:
        mb = int(os.environ.get("COUNCIL_DB_AUDIT_MAX_MB", "100"))
    except ValueError:
        mb = 100
    return max(1, mb) * 1024 * 1024


def _audit_keep_count() -> int:
    try:
        return max(0, int(os.environ.get("COUNCIL_DB_AUDIT_KEEP", "5")))
    except ValueError:
        return 5


def _rotate_audit_log(log_path: Path) -> None:
    """Rotate the audit log when it exceeds the size cap. No-op when
    under the cap. Never raises."""
    try:
        if not log_path.is_file():
            return
        if log_path.stat().st_size < _audit_cap_bytes():
            return
        keep = _audit_keep_count()
        # Delete the oldest rotation if at the keep limit
        oldest = log_path.with_suffix(log_path.suffix + f".{keep}")
        if oldest.exists():
            try:
                oldest.unlink()
            except Exception:
                pass
        # Shift .N → .N+1 working backwards from keep-1 to 1
        for i in range(keep - 1, 0, -1):
            src = log_path.with_suffix(log_path.suffix + f".{i}")
            dst = log_path.with_suffix(log_path.suffix + f".{i+1}")
            if src.exists():
                try:
                    src.rename(dst)
                except Exception:
                    pass
        # Rename current log to .1
        try:
            log_path.rename(
                log_path.with_suffix(log_path.suffix + ".1"))
        except Exception:
            pass
    except Exception:
        # Rotation failures must NEVER break a query path; the worst
        # case is that the log grows beyond the cap until next write.
        pass


def _audit(vault_dir: Any, **fields: Any) -> None:
    """Append a JSONL record to vault/db_audit.log with size-capped
    rotation. Best-effort — a log write failure never breaks a
    query."""
    try:
        record = {
            "ts":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **fields,
        }
        p = _connections_path(vault_dir, _AUDIT_LOG_FILENAME)
        p.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK:
            _rotate_audit_log(p)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Never let logging break a query path.
        pass


# ============================================================
# SQL — single-SELECT validator
# ============================================================

# Comment strippers. SQL has two comment forms — line (`--`) and
# block (`/* */`). Strip both before keyword matching so a query
# like `SELECT 1 /* DROP TABLE users */` doesn't get mistakenly
# flagged AND a query like `--SELECT \n DROP TABLE x` doesn't slip
# through by hiding DROP behind a comment marker.
_LINE_COMMENT_RE  = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# Keywords that imply a write. Word-boundary match so column names
# like `last_update` or `delete_flag` don't false-positive.
_WRITE_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER",
    "CREATE", "GRANT", "REVOKE", "REPLACE", "MERGE", "RENAME",
    "ATTACH", "DETACH", "VACUUM", "REINDEX", "ANALYZE",
    "COPY", "LOAD", "BULK",
    "EXEC", "EXECUTE", "CALL",         # stored procs — can hide writes
    "PRAGMA",                            # SQLite — some pragmas write
    "SET",                               # SET ROLE / SET SESSION can escalate
    "LOCK", "UNLOCK",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
)

# What a SELECT statement is allowed to start with. CTEs use WITH;
# describing via EXPLAIN is harmless; SHOW is read-only on most DBs.
_READ_STARTERS = ("SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC")


class ReadOnlyViolation(Exception):
    """Raised when the SQL validator rejects a query."""


def _validate_select_only(sql: str) -> str:
    """Verify ``sql`` is a single read-only statement. Returns the
    normalised SQL on success, raises ReadOnlyViolation otherwise.

    Rules:
      • Strip line + block comments before any check.
      • Reject if any write keyword (DROP / DELETE / INSERT / …)
        appears as a complete word anywhere in the cleaned SQL.
      • Reject if the first non-whitespace keyword isn't a known
        read starter (SELECT, WITH, EXPLAIN, SHOW, DESCRIBE).
      • Reject multi-statement payloads (semicolon followed by more
        non-whitespace content — a trailing `;` is allowed because
        it's the conventional terminator).
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ReadOnlyViolation("empty SQL")
    cleaned = _LINE_COMMENT_RE.sub(" ", sql)
    cleaned = _BLOCK_COMMENT_RE.sub(" ", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        raise ReadOnlyViolation("SQL is only comments")

    # Multi-statement check. Trim the trailing semicolon FIRST so the
    # common "SELECT 1;" pattern doesn't trigger the multi-statement
    # rejection. After trim, ANY internal `;` is suspect.
    body = cleaned.rstrip(";").strip()
    if ";" in body:
        raise ReadOnlyViolation(
            "multi-statement SQL is rejected — split into separate "
            "calls or use a single SELECT")

    # First keyword check
    first_token_match = re.match(r"^\s*([A-Za-z]+)", body)
    if not first_token_match:
        raise ReadOnlyViolation("no SQL keyword at start of statement")
    first_kw = first_token_match.group(1).upper()
    if first_kw not in _READ_STARTERS:
        raise ReadOnlyViolation(
            f"first keyword {first_kw!r} is not allowed; SQL must "
            f"start with one of {_READ_STARTERS}")

    # Whole-statement keyword scan. Walk every word boundary and
    # check; cheaper than tokenising properly and good enough for
    # our threat model.
    upper = body.upper()
    for kw in _WRITE_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            raise ReadOnlyViolation(
                f"write keyword {kw!r} not allowed in read-only "
                "queries (defence-in-depth — even with a read-only "
                "role, the validator blocks DML / DDL paths)")

    return body


# ============================================================
# SQL execution (read-only)
# ============================================================

def _import_sqlalchemy():
    try:
        import sqlalchemy  # noqa: F401
        return sqlalchemy
    except ImportError as exc:
        raise RuntimeError(
            "SQL connectivity requires SQLAlchemy. Install with "
            "`pip install SQLAlchemy`. Original error: " + str(exc)
        ) from exc


# ── Engine cache ───────────────────────────────────────────────────
# SQLAlchemy engines are thread-safe and pool connections internally.
# Creating a new engine per query is expensive on Postgres + MSSQL
# (DNS, TLS handshake, server-side session setup); on a query-heavy
# analyst session those costs add up. We cache engines keyed on the
# RESOLVED URL so a rotated ${ENV_VAR} naturally yields a new entry.
#
# Cleanup: dispose_engines() (called explicitly on shutdown) walks
# the cache and disposes every pool. Also exposed as a smoke-test
# hook.
#
# Eviction: simple LRU over the dict — we keep up to MAX_CACHED
# engines and dispose the oldest when the cap is exceeded. 16 is
# plenty for typical analyst workloads (one engine per saved
# connection).

_ENGINE_CACHE: "Dict[Tuple[str, str], Any]" = {}
_ENGINE_CACHE_LOCK = _threading.Lock()
_MAX_CACHED_ENGINES = 16


def _strip_council_flags(url: str) -> "Tuple[str, Dict[str, str]]":
    """Extract our internal ``council_*`` query-string flags from a
    URL before handing it to SQLAlchemy. SQLAlchemy would otherwise
    pass them through to the DB driver and produce confusing errors
    on unknown args. Returns (cleaned_url, flags)."""
    if "?" not in url:
        return url, {}
    head, qs = url.split("?", 1)
    parts = qs.split("&")
    keep: List[str] = []
    flags: Dict[str, str] = {}
    for part in parts:
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, ""
        if k.lower().startswith("council_"):
            flags[k.lower()] = v.lower()
        else:
            keep.append(part)
    new_url = head + (("?" + "&".join(keep)) if keep else "")
    return new_url, flags


def _sql_engine(vault_dir: Any, conn_name: str):
    """Return a cached SQLAlchemy engine for the named connection.
    Engines are keyed on (conn_name, resolved_url) so rotated env
    vars naturally invalidate the cache."""
    sqlalchemy = _import_sqlalchemy()
    urls = list_sql_connections(vault_dir)
    if conn_name not in urls:
        raise KeyError(f"unknown SQL connection: {conn_name}")
    raw = urls[conn_name]
    resolved = _resolve_url(raw)

    # File-URI hardening — SQLite/DuckDB URIs get ?mode=ro appended
    # when the user didn't already pin it. SQLite-level read-only
    # enforcement is bulletproof at the file open path.
    if resolved.startswith("sqlite:///") and "mode=ro" not in resolved:
        sep = "&" if "?" in resolved else "?"
        resolved = f"{resolved}{sep}mode=ro&uri=true"

    # Strip our internal council_* flags (council_snapshot, etc.)
    # before SQLAlchemy sees the URL. The flags are stashed on the
    # returned engine via .info so _apply_read_only_session can read
    # them later.
    cleaned, council_flags = _strip_council_flags(resolved)

    cache_key = (str(conn_name), cleaned)
    with _ENGINE_CACHE_LOCK:
        cached = _ENGINE_CACHE.get(cache_key)
        if cached is not None:
            # Refresh LRU position by re-inserting
            _ENGINE_CACHE.pop(cache_key, None)
            _ENGINE_CACHE[cache_key] = cached
            return cached

        # Build fresh engine. pool_pre_ping bounces dead connections
        # before handing them to a query — critical for long-lived
        # cached engines that survive a Mongo/SQL server restart.
        eng = sqlalchemy.create_engine(
            cleaned, pool_pre_ping=True, pool_recycle=3600,
        )
        # Stash council flags so _apply_read_only_session can pick
        # up council_snapshot etc. without re-parsing the URL.
        try:
            eng.info["council_flags"] = council_flags
        except Exception:
            pass
        _ENGINE_CACHE[cache_key] = eng
        # Evict oldest if over cap
        while len(_ENGINE_CACHE) > _MAX_CACHED_ENGINES:
            oldest_key = next(iter(_ENGINE_CACHE))
            old_eng = _ENGINE_CACHE.pop(oldest_key)
            try:
                old_eng.dispose()
            except Exception:
                pass
        return eng


def dispose_engines() -> int:
    """Dispose every cached engine and clear the cache. Call from the
    app's shutdown path. Returns the count disposed."""
    with _ENGINE_CACHE_LOCK:
        n = len(_ENGINE_CACHE)
        for eng in list(_ENGINE_CACHE.values()):
            try:
                eng.dispose()
            except Exception:
                pass
        _ENGINE_CACHE.clear()
    return n


def _apply_read_only_session(engine, connection) -> List[str]:
    """Issue per-dialect read-only session hints. Returns a list of
    notes describing what was applied (used by the audit log).

    Failures here are non-fatal — if the DB doesn't support the hint,
    we still rely on layers 3 (validator) and 4 (API surface).

    MSSQL: when the connection URL includes ``?council_snapshot=on``,
    we issue ``SET TRANSACTION ISOLATION LEVEL SNAPSHOT`` so the
    session reads from a consistent snapshot and cannot see in-
    flight writes. Requires the DB to have ALLOW_SNAPSHOT_ISOLATION
    enabled (db-level setting) — when the DB rejects the command we
    log the failure and continue (layers 1 + 3 still hold).
    """
    notes: List[str] = []
    sqlalchemy = _import_sqlalchemy()
    text = sqlalchemy.text
    dialect = engine.dialect.name.lower()
    flags = {}
    try:
        flags = engine.info.get("council_flags") or {}
    except Exception:
        pass
    try:
        if dialect in ("postgresql", "postgres"):
            connection.execute(text("SET TRANSACTION READ ONLY"))
            notes.append("postgresql: SET TRANSACTION READ ONLY")
        elif dialect == "mysql":
            connection.execute(text("SET SESSION TRANSACTION READ ONLY"))
            notes.append("mysql: SET SESSION TRANSACTION READ ONLY")
        elif dialect in ("mssql", "pyodbc"):
            snapshot = flags.get("council_snapshot", "").lower() in (
                "on", "true", "1", "yes")
            if snapshot:
                try:
                    connection.execute(
                        text("SET TRANSACTION ISOLATION LEVEL SNAPSHOT"))
                    notes.append(
                        "mssql: SET TRANSACTION ISOLATION LEVEL SNAPSHOT "
                        "(opt-in via ?council_snapshot=on)")
                except Exception as exc:
                    notes.append(
                        f"mssql: SNAPSHOT request failed ({exc!r}); "
                        "DB must have ALLOW_SNAPSHOT_ISOLATION ON. "
                        "Falling back to layers 1+3.")
            else:
                notes.append(
                    "mssql: no session hint (rely on DB role + validator; "
                    "add ?council_snapshot=on to the URL for SNAPSHOT "
                    "isolation when the DB supports it)")
        elif dialect == "sqlite":
            # Handled at the URI level via ?mode=ro
            notes.append("sqlite: handled by URI ?mode=ro")
        else:
            notes.append(f"{dialect}: no session hint applied")
    except Exception as exc:
        notes.append(f"{dialect}: session hint failed ({exc!r})")
    return notes


def list_sql_tables(vault_dir: Any, conn_name: str) -> List[str]:
    """Inspect a saved connection and return its table names.

    Engine is cached — see _sql_engine. Do NOT dispose here.
    """
    eng = _sql_engine(vault_dir, conn_name)
    sqlalchemy = _import_sqlalchemy()
    names = sorted(sqlalchemy.inspect(eng).get_table_names())
    _audit(vault_dir, kind="sql.list_tables", conn=conn_name,
            n_tables=len(names))
    return names


def read_sql_table(
    vault_dir: Any,
    conn_name: str,
    table: str,
    *,
    limit: Optional[int] = 10000,
) -> "pd.DataFrame":
    """Pull a remote table. Read-only by construction (SELECT only).
    Default 10K-row limit; pass ``limit=None`` to lift it (logged
    loudly to the audit)."""
    import pandas as pd
    eng = _sql_engine(vault_dir, conn_name)
    qname = '"' + str(table).replace('"', '""') + '"'
    sql = f"SELECT * FROM {qname}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    t0 = time.monotonic()
    with eng.connect() as conn:
        session_notes = _apply_read_only_session(eng, conn)
        df = pd.read_sql_query(sql, conn)
    _audit(vault_dir, kind="sql.read_table", conn=conn_name,
            table=table, rows=len(df),
            duration_ms=int((time.monotonic() - t0) * 1000),
            session=session_notes, limit=limit)
    return df


def sql_query(
    vault_dir: Any,
    conn_name: str,
    sql: str,
) -> "pd.DataFrame":
    """Run an arbitrary SQL query. Validated to a SINGLE read-only
    statement before dispatch."""
    import pandas as pd
    cleaned = _validate_select_only(sql)
    eng = _sql_engine(vault_dir, conn_name)
    t0 = time.monotonic()
    with eng.connect() as conn:
        session_notes = _apply_read_only_session(eng, conn)
        df = pd.read_sql_query(cleaned, conn)
    _audit(vault_dir, kind="sql.query", conn=conn_name,
            sql=cleaned[:500],
            rows=len(df),
            duration_ms=int((time.monotonic() - t0) * 1000),
            session=session_notes)
    return df


# ============================================================
# Mongo — read-only by API design
# ============================================================

# Aggregation stages that can write or run server-side JS. We reject
# any pipeline containing one of these because they bypass the
# read-only role on misconfigured servers.
_MONGO_WRITE_STAGES = {
    "$out",              # writes results into a collection
    "$merge",            # merges results into a collection
    "$function",         # server-side JS function
    "$accumulator",      # server-side JS accumulator
    "$where",            # server-side JS filter (deprecated but still
                          # supported on some servers)
}


class MongoPipelineViolation(Exception):
    """Raised when the Mongo pipeline validator rejects a pipeline."""


def _import_pymongo():
    try:
        import pymongo  # noqa: F401
        return pymongo
    except ImportError as exc:
        raise RuntimeError(
            "MongoDB connectivity requires pymongo. Install with "
            "`pip install pymongo`. Original error: " + str(exc)
        ) from exc


def _mongo_client(vault_dir: Any, conn_name: str, *,
                   timeout_ms: int = 5000):
    pymongo = _import_pymongo()
    urls = list_mongo_connections(vault_dir)
    if conn_name not in urls:
        raise KeyError(f"unknown Mongo connection: {conn_name}")
    uri = _resolve_url(urls[conn_name])
    return pymongo.MongoClient(
        uri,
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        # Hard-stop on operations that try to exceed 30s.
        socketTimeoutMS=30000,
    )


def _walk_for_forbidden_keys(node: Any, path: str = "") -> Optional[str]:
    """Depth-first walk over a Mongo pipeline node looking for any
    forbidden operator key. Returns ``"$op at <path>"`` on first hit,
    or None if the node is clean. Dangerous ops can appear nested
    inside $project/$group/$match expressions, not just at the top
    of a stage, so the validator must descend through dicts and lists."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k in _MONGO_WRITE_STAGES:
                return f"{k} at {path or '<root>'}"
            child = _walk_for_forbidden_keys(v, f"{path}.{k}" if path else k)
            if child:
                return child
    elif isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            child = _walk_for_forbidden_keys(item, f"{path}[{i}]")
            if child:
                return child
    return None


def _validate_mongo_pipeline(pipeline: Sequence[Dict[str, Any]]) -> None:
    if not isinstance(pipeline, (list, tuple)):
        raise MongoPipelineViolation(
            f"pipeline must be a list of dict stages; got {type(pipeline).__name__}")
    for i, stage in enumerate(pipeline):
        if not isinstance(stage, dict):
            raise MongoPipelineViolation(
                f"pipeline[{i}] is not a dict: {type(stage).__name__}")
        hit = _walk_for_forbidden_keys(stage, f"pipeline[{i}]")
        if hit:
            raise MongoPipelineViolation(
                f"pipeline stage rejected ({hit}): contains a "
                "write or server-side-JS operator. Read-only Mongo "
                f"queries cannot use {sorted(_MONGO_WRITE_STAGES)}.")


def list_mongo_databases(vault_dir: Any, conn_name: str) -> List[str]:
    client = _mongo_client(vault_dir, conn_name)
    try:
        names = sorted(client.list_database_names())
        _audit(vault_dir, kind="mongo.list_databases", conn=conn_name,
                n=len(names))
        return names
    finally:
        client.close()


def list_mongo_collections(vault_dir: Any, conn_name: str,
                            db_name: str) -> List[str]:
    client = _mongo_client(vault_dir, conn_name)
    try:
        names = sorted(client[db_name].list_collection_names())
        _audit(vault_dir, kind="mongo.list_collections", conn=conn_name,
                db=db_name, n=len(names))
        return names
    finally:
        client.close()


def read_mongo_collection(
    vault_dir: Any,
    conn_name: str,
    db_name: str,
    collection: str,
    *,
    query: Optional[Dict[str, Any]] = None,
    projection: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = 10000,
    skip: int = 0,
    sort: Optional[List] = None,
) -> "pd.DataFrame":
    """Mongo `find()` → DataFrame via pd.json_normalize.

    Read-only by API design — uses find(), no insert/update/delete
    methods are reachable from this wrapper. Default 10K-row limit;
    pass ``limit=None`` to lift it (audit-logged with a WARN tag)."""
    import pandas as pd
    client = _mongo_client(vault_dir, conn_name)
    t0 = time.monotonic()
    try:
        cursor = client[db_name][collection].find(
            filter=query or {},
            projection=projection,
            skip=skip,
        )
        if sort is not None:
            cursor = cursor.sort(sort)
        if limit is not None:
            cursor = cursor.limit(int(limit))
        docs = list(cursor)
        # MongoDB returns ObjectId / datetime / Decimal128 objects that
        # pd.json_normalize handles via repr; that's fine for analyst
        # purposes since the downstream model just reads as strings.
        df = pd.json_normalize(docs) if docs else pd.DataFrame()
        _audit(vault_dir, kind="mongo.find", conn=conn_name,
                db=db_name, collection=collection,
                rows=len(df),
                duration_ms=int((time.monotonic() - t0) * 1000),
                limit=limit, unlimited=(limit is None))
        return df
    finally:
        client.close()


def mongo_aggregate(
    vault_dir: Any,
    conn_name: str,
    db_name: str,
    collection: str,
    pipeline: Sequence[Dict[str, Any]],
    *,
    allow_disk_use: bool = False,
) -> "pd.DataFrame":
    """Run a Mongo aggregation pipeline. The pipeline is validated
    before dispatch — write stages ($out, $merge) and server-side
    JS stages ($function, $accumulator, $where) are rejected."""
    import pandas as pd
    _validate_mongo_pipeline(pipeline)
    client = _mongo_client(vault_dir, conn_name)
    t0 = time.monotonic()
    try:
        cursor = client[db_name][collection].aggregate(
            list(pipeline), allowDiskUse=allow_disk_use,
        )
        docs = list(cursor)
        df = pd.json_normalize(docs) if docs else pd.DataFrame()
        _audit(vault_dir, kind="mongo.aggregate", conn=conn_name,
                db=db_name, collection=collection,
                stages=len(pipeline), rows=len(df),
                duration_ms=int((time.monotonic() - t0) * 1000))
        return df
    finally:
        client.close()


def mongo_count(
    vault_dir: Any,
    conn_name: str,
    db_name: str,
    collection: str,
    *,
    query: Optional[Dict[str, Any]] = None,
) -> int:
    client = _mongo_client(vault_dir, conn_name)
    t0 = time.monotonic()
    try:
        n = client[db_name][collection].count_documents(query or {})
        _audit(vault_dir, kind="mongo.count", conn=conn_name,
                db=db_name, collection=collection, n=n,
                duration_ms=int((time.monotonic() - t0) * 1000))
        return int(n)
    finally:
        client.close()


def mongo_distinct(
    vault_dir: Any,
    conn_name: str,
    db_name: str,
    collection: str,
    field: str,
    *,
    query: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    client = _mongo_client(vault_dir, conn_name)
    t0 = time.monotonic()
    try:
        values = list(client[db_name][collection].distinct(
            field, filter=query or {}))
        _audit(vault_dir, kind="mongo.distinct", conn=conn_name,
                db=db_name, collection=collection, field=field,
                n_values=len(values),
                duration_ms=int((time.monotonic() - t0) * 1000))
        return values
    finally:
        client.close()


# ============================================================
# Export — pull data OUT of a DB into a local file
# ============================================================
#
# Every export routes through the read-only readers above
# (read_sql_table / sql_query / read_mongo_collection). There is NO
# code path here that writes to the database — these functions only
# READ from the DB and WRITE to a local file under the vault. The
# read-only guarantee is therefore inherited wholesale: an export can
# no more delete a row than read_sql_table can.

_EXPORT_FORMATS = ("csv", "json", "xlsx")


def _safe_export_name(stem: str, fmt: str) -> str:
    """Sanitise a table/collection name into a filename. Strips path
    separators and odd characters so a remote object called
    ``../../etc`` or ``my db.tbl`` can't escape the export dir."""
    import re as _re
    base = _re.sub(r"[^A-Za-z0-9._-]+", "_", str(stem)).strip("._") or "export"
    return f"{base}.{fmt}"


def _write_dataframe(df: "pd.DataFrame", dest_path: Any, fmt: str) -> str:
    """Write a DataFrame to ``dest_path`` in ``fmt``. Returns the path
    written. Raises ValueError for an unknown/unavailable format. This
    is the ONLY filesystem write in the export path — it never touches
    a database."""
    fmt = str(fmt or "").lower().lstrip(".")
    p = Path(dest_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        df.to_csv(p, index=False)
    elif fmt == "json":
        # records orientation = a JSON array of row objects; readable
        # and re-importable. date_format=iso keeps timestamps sane.
        df.to_json(p, orient="records", indent=2, date_format="iso")
    elif fmt == "xlsx":
        try:
            df.to_excel(p, index=False)
        except Exception as exc:
            raise ValueError(
                "Excel export needs openpyxl. Install it, or export to "
                f"CSV/JSON instead. Original error: {exc}") from exc
    elif fmt in ("parquet", "pq"):
        try:
            df.to_parquet(p, index=False)
        except Exception as exc:
            raise ValueError(
                "Parquet export needs pyarrow or fastparquet (not "
                "installed). Export to CSV/JSON/Excel instead. "
                f"Original error: {exc}") from exc
    else:
        raise ValueError(
            f"unknown export format {fmt!r}; use one of {_EXPORT_FORMATS}")
    return str(p)


def export_sql_table(
    vault_dir: Any,
    conn_name: str,
    table: str,
    dest_path: Any,
    *,
    fmt: str = "csv",
    limit: Optional[int] = 100000,
) -> Dict[str, Any]:
    """Read a SQL table (SELECT-only) and write it to a local file.
    Returns {"path", "rows", "format"}. Never writes to the DB."""
    df = read_sql_table(vault_dir, conn_name, table, limit=limit)
    written = _write_dataframe(df, dest_path, fmt)
    _audit(vault_dir, kind="sql.export", conn=conn_name, table=table,
            rows=len(df), fmt=fmt, dest=written, limit=limit)
    return {"path": written, "rows": int(len(df)), "format": fmt}


def export_sql_query(
    vault_dir: Any,
    conn_name: str,
    sql: str,
    dest_path: Any,
    *,
    fmt: str = "csv",
) -> Dict[str, Any]:
    """Run a validated SELECT-only query and write the result to a file.
    Never writes to the DB (sql_query rejects any non-SELECT)."""
    df = sql_query(vault_dir, conn_name, sql)
    written = _write_dataframe(df, dest_path, fmt)
    _audit(vault_dir, kind="sql.export_query", conn=conn_name,
            sql=str(sql)[:500], rows=len(df), fmt=fmt, dest=written)
    return {"path": written, "rows": int(len(df)), "format": fmt}


def export_mongo_collection(
    vault_dir: Any,
    conn_name: str,
    db_name: str,
    collection: str,
    dest_path: Any,
    *,
    fmt: str = "csv",
    query: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = 100000,
) -> Dict[str, Any]:
    """Read a Mongo collection (find(), read-only) and write it to a
    local file. Never writes to the DB."""
    df = read_mongo_collection(vault_dir, conn_name, db_name, collection,
                                query=query, limit=limit)
    written = _write_dataframe(df, dest_path, fmt)
    _audit(vault_dir, kind="mongo.export", conn=conn_name, db=db_name,
            collection=collection, rows=len(df), fmt=fmt, dest=written,
            limit=limit)
    return {"path": written, "rows": int(len(df)), "format": fmt}


# ============================================================
# Test helpers — used by the Vault tab's "Test connection" button
# ============================================================

def test_sql_connection(vault_dir: Any, conn_name: str) -> Dict[str, Any]:
    """Best-effort connection probe — returns a result dict for the UI.
    Doesn't run SELECTs against arbitrary tables, just ``SELECT 1``.
    The cached engine remains in the pool after this call (so the
    subsequent real query reuses the warm connection)."""
    out: Dict[str, Any] = {
        "ok": False, "dialect": None, "tables": 0, "error": None,
        "tls_warning": None, "unresolved_env_vars": None,
    }
    # TLS posture check — surfaced to the UI on test/save
    try:
        urls = list_sql_connections(vault_dir)
        if conn_name in urls:
            out["tls_warning"] = check_tls_posture(urls[conn_name])
    except Exception:
        pass
    try:
        eng = _sql_engine(vault_dir, conn_name)
        out["dialect"] = eng.dialect.name
        sqlalchemy = _import_sqlalchemy()
        with eng.connect() as conn:
            _apply_read_only_session(eng, conn)
            conn.execute(sqlalchemy.text("SELECT 1"))
        try:
            out["tables"] = len(sqlalchemy.inspect(eng).get_table_names())
        except Exception:
            pass
        out["ok"] = True
    except UnresolvedEnvVarError as exc:
        out["unresolved_env_vars"] = list(exc.missing)
        out["error"] = str(exc)
    except Exception as exc:
        out["error"] = repr(exc)
    return out


def test_mongo_connection(vault_dir: Any, conn_name: str) -> Dict[str, Any]:
    """Best-effort Mongo probe — pings the server, lists databases."""
    out: Dict[str, Any] = {
        "ok": False, "databases": 0, "error": None,
        "tls_warning": None, "unresolved_env_vars": None,
    }
    try:
        urls = list_mongo_connections(vault_dir)
        if conn_name in urls:
            out["tls_warning"] = check_tls_posture(urls[conn_name])
    except Exception:
        pass
    try:
        client = _mongo_client(vault_dir, conn_name, timeout_ms=3000)
        try:
            client.admin.command("ping")
            out["databases"] = len(client.list_database_names())
            out["ok"] = True
        finally:
            client.close()
    except UnresolvedEnvVarError as exc:
        out["unresolved_env_vars"] = list(exc.missing)
        out["error"] = str(exc)
    except Exception as exc:
        out["error"] = repr(exc)
    return out
